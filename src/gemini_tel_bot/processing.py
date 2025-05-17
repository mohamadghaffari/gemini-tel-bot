from time import time
import json
import logging
import telebot
from google import genai
from typing import Callable, Dict, Any
from telebot import types as telebot_types
from google.genai import types as genai_types
from google.genai.errors import ClientError, ServerError
from google.api_core.exceptions import (
    NotFound,
    GoogleAPIError,
    PermissionDenied,
    ResourceExhausted,
)
from .config import DEFAULT_MODEL_NAME, LOADING_ANIMATION_FILE_ID
from .custom_types import UserSettings
from .db import (
    get_history_from_db,
    save_turn_to_db,
    get_user_settings_from_db,
    save_user_settings_to_db,
    clear_history_in_db,
)
from .helpers import (
    split_and_send_message,
    check_db_and_settings,
    check_ai_client,
    check_message_limit_and_increment,
)

logger = logging.getLogger(__name__)

user_temp_state: dict[int, Dict[str, Any]] = {}
ChatObject = Any


def _handle_ai_interaction(
    message: telebot_types.Message,
    user_settings: UserSettings,
    ai_client: genai.Client,
    user_input_parts: list[genai_types.Part],
    bot_instance: telebot.TeleBot,
) -> None:
    """
    Handles the core AI chat interaction: fetching history, creating chat,
    sending message, saving new turns, getting response, and sending reply.
    """
    processing_completed_successfully = False
    chat_id = message.chat.id
    model_for_user = user_settings.get("selected_model", DEFAULT_MODEL_NAME)

    waiting_animation: telebot_types.Message | None = None

    try:
        waiting_animation = bot_instance.send_animation(
            chat_id,
            animation=LOADING_ANIMATION_FILE_ID,
            caption="Working my magic... please wait a moment. ✨",
        )
        history_content: list[genai_types.Content] | None = get_history_from_db(chat_id)
        if history_content is None:
            bot_instance.reply_to(
                message, "Error fetching chat history from the database."
            )
            logger.error(
                f"AI interaction failed for {chat_id}: Error fetching history."
            )
            return

        chat: ChatObject | None = None
        try:
            history_for_api: list[genai_types.Content | genai_types.ContentDict] | None
            # Model name for chats.create usually does not need "models/" prefix
            effective_model_name = model_for_user.replace("models/", "")
            if history_content is not None:
                history_for_api = history_content  # type: ignore[assignment]
            else:
                history_for_api = None

            chat = ai_client.chats.create(model=model_for_user, history=history_for_api)
            logger.info(
                f"Chat object created for {chat_id} with model {effective_model_name}."
            )
        except NotFound as nf_e:  # This is google.api_core.exceptions.NotFound
            logger.error(
                f"Model not found/supported for chat creation ({effective_model_name}) for {chat_id}: {nf_e}",
                exc_info=True,
            )
            error_message = f"The selected model `{effective_model_name}` is not available or supported for conversations with your API key.\n\nPlease use `/select_model` to choose a different model."
            bot_instance.reply_to(message, error_message, parse_mode="Markdown")
            return
        except Exception as create_e:  # Broader exception for chat creation
            logger.error(
                f"Error during chat creation for {chat_id} with model {effective_model_name}: {create_e}",
                exc_info=True,
            )
            bot_instance.reply_to(
                message,
                f"An error occurred while starting the conversation with model `{effective_model_name}`: {str(create_e)[:200]}",  # Limit error message length
            )
            return

        if chat is None:  # Should not happen if no exception, but a good safeguard
            logger.error(
                f"Chat object is None after ai_client.chats.create for {chat_id}"
            )
            bot_instance.reply_to(message, "Failed to initialize chat session.")
            return

        logger.info(f"Calling chat.send_message for {chat_id}...")
        response: genai_types.GenerateContentResponse | None = None
        try:
            list_of_parts_for_api: list[genai_types.Part] = user_input_parts
            response = chat.send_message(user_input_parts)  # type: ignore[arg-type]
            logger.info(f"chat.send_message completed for {chat_id}.")

        except ClientError as client_error:
            logger.error(
                f"ClientError during chat.send_message for {chat_id}: {client_error}",
                exc_info=True,
            )
            status_code = client_error.code if hasattr(client_error, "code") else "N/A"
            response_json = (
                client_error.details if hasattr(client_error, "details") else None
            )

            user_facing_error = (
                f"An AI communication error occurred (Code: {status_code})."
            )
            links_text = ""

            if status_code == 429:
                user_facing_error = f"Your request failed due to a quota limit being reached for the selected model (`{model_for_user}`)."
                try:
                    # Safely parse links from response_json
                    if (
                        isinstance(response_json, dict)
                        and "error" in response_json
                        and "details" in response_json["error"]
                    ):
                        error_details_list = response_json["error"]["details"]
                        if isinstance(error_details_list, list):
                            for detail in error_details_list:
                                if (
                                    isinstance(detail, dict)
                                    and detail.get("@type")
                                    == "type.googleapis.com/google.rpc.Help"
                                    and detail.get("links")
                                ):
                                    links_list = detail["links"]
                                    if isinstance(links_list, list):
                                        for link in links_list:
                                            if (
                                                isinstance(link, dict)
                                                and link.get("description")
                                                and link.get("url")
                                            ):
                                                links_text += f"\n\n[{link['description']}]({link['url']})"
                                                break
                                        break
                    elif isinstance(response_json, list):
                        for detail in response_json:
                            if (
                                isinstance(detail, dict)
                                and detail.get("@type")
                                == "type.googleapis.com/google.rpc.Help"
                                and detail.get("links")
                            ):
                                links_list = detail["links"]
                                if isinstance(links_list, list):
                                    for link in links_list:
                                        if (
                                            isinstance(link, dict)
                                            and link.get("description")
                                            and link.get("url")
                                        ):
                                            links_text += f"\n\n[{link['description']}]({link['url']})"
                                            break
                                    break
                    if not links_text:
                        logger.warning(
                            f"429 error details not in expected dict/list format or no links found for {chat_id}. Details: {response_json}"
                        )
                        links_text = "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits"
                except Exception as link_parse_e:
                    logger.error(
                        f"Failed to parse help links from 429 error details for {chat_id}: {link_parse_e}",
                        exc_info=True,
                    )
                    links_text = "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits"
                user_facing_error += links_text

            elif status_code == 400:
                error_detail_message = ""
                error_status_field = None
                if isinstance(response_json, dict) and "error" in response_json:
                    error_obj = response_json["error"]
                    if isinstance(error_obj, dict):
                        error_detail_message = error_obj.get(
                            "message", str(response_json)
                        )
                        error_status_field = error_obj.get("status")
                    else:
                        error_detail_message = str(response_json)
                else:
                    error_detail_message = str(response_json)

                is_blocked = error_status_field == "BLOCKED" or (
                    error_detail_message
                    and (
                        "blocked" in error_detail_message.lower()
                        or "safety" in error_detail_message.lower()
                    )
                )

                if is_blocked:
                    user_facing_error = "Your input or the model's response was blocked by safety filters."
                    if (
                        isinstance(response_json, dict)
                        and "error" in response_json
                        and isinstance(response_json["error"], dict)
                        and "details" in response_json["error"]
                    ):
                        safety_details_list = response_json["error"]["details"]
                        if isinstance(safety_details_list, list):
                            try:
                                for detail_item in safety_details_list:
                                    if (
                                        isinstance(detail_item, dict)
                                        and detail_item.get("@type")
                                        == "type.googleapis.com/google.rpc.BadRequest"
                                        and detail_item.get("fieldViolations")
                                    ):
                                        violations = detail_item["fieldViolations"]
                                        if isinstance(violations, list):
                                            violation_messages = [
                                                v.get("description", v.get("field", ""))
                                                for v in violations
                                                if isinstance(v, dict)
                                            ]
                                            if violation_messages:
                                                user_facing_error += f" Reason(s): {', '.join(violation_messages)}"
                                        break
                            except Exception as safety_parse_e:
                                logger.error(
                                    f"Failed to parse safety violation details for {chat_id}: {safety_parse_e}",
                                    exc_info=True,
                                )
                    elif isinstance(response_json, list):
                        pass

                    if error_detail_message and (
                        "LENGTH" in error_detail_message.upper()
                        or "CONTEXT" in error_detail_message.upper()
                        or "TOO_LARGE" in error_detail_message.upper()
                    ):
                        user_facing_error += "\n\nYour conversation history or input might be too long for the model. Try using `/reset`."
                else:
                    user_facing_error = f"Bad request to the AI model. Message: {error_detail_message or 'N/A'}"

            disable_preview = bool(status_code == 429 and links_text)
            user_facing_error += (
                "\n\nUse a different model by using  /select_model command\\."
            )
            split_and_send_message(
                message,
                user_facing_error,
                bot_instance,
                disable_web_page_preview=disable_preview,
            )
            return

        except ServerError as server_error:
            logger.error(
                f"ServerError during chat.send_message for {chat_id}: {server_error}",
                exc_info=True,
            )
            user_facing_error = f"The AI service encountered a server error (Code: {server_error.code if hasattr(server_error, 'code') else 'N/A'}). Please try again later."
            bot_instance.reply_to(message, user_facing_error)
            return

        except (
            NotFound,
            ResourceExhausted,
        ) as api_core_error:  # From google.api_core.exceptions
            logger.error(
                f"Specific Google API Core Error during chat.send_message for {chat_id}: {api_core_error}",
                exc_info=True,
            )
            if isinstance(api_core_error, NotFound):
                error_message = f"The selected model `{model_for_user}` is not available or supported.\n\nPlease use `/select_model`."
            elif isinstance(api_core_error, ResourceExhausted):
                error_message = "Your request failed due to a quota limit being reached for the selected model."  # Removed extra \c
                error_message += "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits"  # Removed extra \:
            else:
                error_message = (
                    f"An AI service error occurred: {str(api_core_error)[:200]}"
                )
            bot_instance.reply_to(
                message,
                error_message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        except (
            GoogleAPIError
        ) as api_error:  # General fallback for google.api_core.exceptions
            logger.error(
                f"General GoogleAPIError fallback during chat.send_message for {chat_id}: {api_error}",
                exc_info=True,
            )
            bot_instance.reply_to(
                message,
                f"A general AI service error occurred: {str(api_error)[:200]}",  # Limit error message
            )
            return

        except Exception as send_e:  # Ultimate fallback for send_message
            logger.error(
                f"Truly unexpected Error during chat.send_message for {chat_id}: {send_e}",
                exc_info=True,
            )
            bot_instance.reply_to(
                message, "An unexpected internal error occurred during AI interaction."
            )
            return

        # --- Process successful response ---
        if chat and response:  # chat is ChatObject, response is GenerateContentResponse
            updated_history: list[genai_types.Content] = chat.get_history()
            original_history_length = len(history_content)
            updated_history_length = len(updated_history)

            if updated_history_length >= original_history_length + 2:
                new_user_turn_content = updated_history[-2]
                new_model_turn_content = updated_history[-1]
                user_turn_db_index = original_history_length
                model_turn_db_index = original_history_length + 1

                logger.info(
                    f"Saving new turns to DB for {chat_id}. User index {user_turn_db_index}, Model index {model_turn_db_index}"
                )
                save_turn_to_db(
                    chat_id,
                    user_turn_db_index,
                    new_user_turn_content.role,
                    user_input_parts,
                )
                save_turn_to_db(
                    chat_id,
                    model_turn_db_index,
                    new_model_turn_content.role,
                    new_model_turn_content.parts,
                )

            elif updated_history_length > original_history_length:
                logger.warning(
                    f"History grew by less than 2 turns for {chat_id}. Original: {original_history_length}, Updated: {updated_history_length}. Attempting to save user turn if present."
                )
                if (
                    updated_history[-1].role == "user"
                    and updated_history_length == original_history_length + 1
                ):
                    # new_user_turn_content = updated_history[-1] # This is Content
                    user_turn_db_index = original_history_length
                    save_turn_to_db(
                        chat_id,
                        user_turn_db_index,
                        updated_history[-1].role,
                        user_input_parts,
                    )
                    logger.warning(f"Saved only the new user turn for {chat_id}.")
                else:
                    logger.warning(
                        f"Unexpected state: History grew by {updated_history_length - original_history_length} but conditions not met for saving partial turn. Last role: {updated_history[-1].role if updated_history else 'N/A'}"
                    )
            else:
                logger.warning(
                    f"History did not grow after send_message for {chat_id}. Nothing to save."
                )

            model_response_text = ""
            if response.text is not None:
                model_response_text = response.text
            elif response.candidates:
                logger.info(
                    f"Response for {chat_id} has no direct .text. Attempting to extract text parts from candidates."
                )
                extracted_texts_from_candidates: list[str] = []
                tool_calls_from_candidates: list[genai_types.FunctionCall] = []
                tool_responses_from_candidates: list[genai_types.FunctionResponse] = []

                for candidate_item in response.candidates:
                    if candidate_item.content and candidate_item.content.parts:
                        for part_item in candidate_item.content.parts:
                            if part_item.text is not None:
                                extracted_texts_from_candidates.append(part_item.text)
                            elif part_item.function_call is not None:
                                tool_calls_from_candidates.append(
                                    part_item.function_call
                                )
                            elif part_item.function_response is not None:
                                tool_responses_from_candidates.append(
                                    part_item.function_response
                                )

                if extracted_texts_from_candidates:
                    model_response_text = "".join(extracted_texts_from_candidates)
                    logger.info(f"Extracted text from candidate parts for {chat_id}.")
                elif tool_calls_from_candidates:
                    model_response_text = "Model wants to call a function:\n" + "\n".join(
                        [
                            f"- `{tc.name}(`{json.dumps(tc.args) if tc.args else ''}`)"
                            for tc in tool_calls_from_candidates
                        ]
                    )
                    logger.info(f"Formatted tool calls for {chat_id}.")
                elif tool_responses_from_candidates:
                    model_response_text = "Model received a function response."
                    logger.info(f"Model received function response for {chat_id}.")
                else:  # If no text, no tool calls, no tool responses found in parts
                    model_response_text = "Received a non-text response from candidates without recognizable parts."
                    logger.warning(
                        f"Non-text, non-tool response from candidates for {chat_id}. Candidates: {response.candidates}"
                    )
            else:  # If response has no .text and no .candidates
                model_response_text = "Could not get a valid response from the model."
                logger.warning(
                    f"No .text or .candidates in response for {chat_id}. Response object: {response}"
                )
            logger.info(f"Replying to {chat_id}...")
            split_and_send_message(message, model_response_text, bot_instance)
            if waiting_animation:  # Ensure it was created
                bot_instance.delete_message(
                    waiting_animation.chat.id, waiting_animation.message_id
                )
            processing_completed_successfully = True

        else:
            logger.error(
                f"AI interaction completed without raising exception but chat or response object is missing/invalid for {chat_id}."
            )
            bot_instance.reply_to(
                message, "An internal error occurred after AI interaction."
            )

    except Exception as outer_e:
        logger.error(
            f"Unexpected outer error during AI interaction processing for {chat_id}: {outer_e}",
            exc_info=True,
        )
        bot_instance.reply_to(
            message, "An unexpected error occurred during processing."
        )
        logger.error(
            f"Error preparing or downloading photo for {chat_id}: {outer_e}",
            exc_info=True,
        )
        bot_instance.reply_to(
            message,
            "Sorry, I encountered an error processing the image.",
        )

    finally:
        try:
            if (
                waiting_animation
                and hasattr(waiting_animation, "message_id")
                and waiting_animation.message_id is not None
                and not processing_completed_successfully
            ):
                bot_instance.delete_message(
                    waiting_animation.chat.id, waiting_animation.message_id
                )
        except Exception as delete_e:
            logger.warning(
                f"Failed to delete 'Processing' message for chat {chat_id}: {delete_e}"
            )


def process_user_message(
    message: telebot_types.Message,
    process_logic_func: Callable[
        [telebot_types.Message, UserSettings, genai.Client, telebot.TeleBot], None
    ],
    bot_instance: telebot.TeleBot,
) -> None:
    """
    Wrapper function to handle common checks (DB, settings, AI client, message limit,
    API key input state) before calling the specific message processing logic.
    Calls process_logic_func(message, user_settings, ai_client) if checks pass.
    """
    chat_id = message.chat.id

    if bot_instance is None:
        logger.error(
            f"Bot instance is None in process_user_message for {chat_id}. Cannot send replies."
        )
        return

    if user_temp_state.pop(chat_id, {}).get("awaiting_api_key"):
        api_key_text = (
            message.text.strip()
            if message.text and isinstance(message.text, str)
            else ""
        )
        logger.info(f"Processing interactive API key input for {chat_id}")

        if not api_key_text:
            bot_instance.reply_to(
                message, "API key cannot be empty\\.", parse_mode="MarkdownV2"
            )
            logger.warning(f"Empty API key provided by user {chat_id}.")
            return

        try:
            logger.info(f"Attempting to validate API key for {chat_id}...")
            # Create a temporary client with the new key for validation
            temp_client = genai.Client(api_key=api_key_text)
            logger.info(
                f"Performing small test call for key starting with {api_key_text[:4]}..."
            )
            list(temp_client.models.list())
            logger.info(f"API key validation successful for {chat_id}.")

            user_settings_before_save = get_user_settings_from_db(chat_id)
            if user_settings_before_save is None:  # Check if fetching settings failed
                bot_instance.reply_to(
                    message,
                    "Error fetching your settings before saving key\\.",
                    parse_mode="MarkdownV2",
                )
                logger.error(f"Failed to fetch settings before key save for {chat_id}.")
                return  # Exit if settings can't be fetched

            if save_user_settings_to_db(
                chat_id,
                api_key=api_key_text,  # Use the validated api_key_text
                model_name=user_settings_before_save.get(
                    "selected_model", DEFAULT_MODEL_NAME
                ),
                message_count=0,  # Reset message count on new key
            ):
                clear_history_in_db(chat_id)  # Clear history on successful key change
                bot_instance.reply_to(
                    message,
                    "Your Gemini API key has been set successfully\! Your chat history has been reset\\.",
                    parse_mode="MarkdownV2",
                )
                logger.info(
                    f"User {chat_id} set a custom API key via interactive input. Completed."
                )
            else:
                bot_instance.reply_to(
                    message,
                    "Failed to save your API key to the database\\.",
                    parse_mode="MarkdownV2",
                )
                logger.error(f"Interactive API key save failed for {chat_id}")

        except PermissionDenied as pd_e:
            logger.error(
                f"Permission denied validating API key for {chat_id}: {pd_e}",
                exc_info=True,
            )
            bot_instance.reply_to(
                message,
                "Failed to validate API key\: Permission Denied\\. Check if the key is correct and enabled for the Gemini API\\.",
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(
                f"Error validating or saving API key for {chat_id}: {e}", exc_info=True
            )
            bot_instance.reply_to(
                message,
                f"Failed to set API key\: Could not initialize AI client or connect to service\\. Check your key\\. Error\: {e}\n\nTry `/set_api_key` again or `/cancel`\\.",
                parse_mode="MarkdownV2",
            )
        return

    # --- Regular Message Processing Flow (if not awaiting API key) ---
    current_user_settings = check_db_and_settings(chat_id, message, bot_instance)
    if current_user_settings is None:
        return

    if not check_message_limit_and_increment(
        chat_id, message, current_user_settings, bot_instance
    ):
        return

    ai_client_instance = check_ai_client(
        chat_id, message, current_user_settings, bot_instance
    )
    if ai_client_instance is None:
        return

    # --- If all checks pass, call the specific processing logic ---
    process_logic_func(message, current_user_settings, ai_client_instance, bot_instance)


def process_text_message(
    message: telebot_types.Message,
    user_settings: UserSettings,
    ai_client: genai.Client,
    bot_instance: telebot.TeleBot,
) -> None:
    """Processes a text message and interacts with the AI."""
    chat_id = message.chat.id
    user_message_text = (
        message.text if message.text and isinstance(message.text, str) else ""
    )
    logger.info(f"Processing text message for {chat_id}: {user_message_text[:50]}...")

    if not user_message_text:
        logger.warning(f"Empty text message received from {chat_id}.")
        bot_instance.reply_to(message, "Please send some text to chat!")
        return

    user_input_parts: list[genai_types.Part] = [
        genai_types.Part(text=user_message_text)
    ]
    _handle_ai_interaction(
        message, user_settings, ai_client, user_input_parts, bot_instance
    )
    logger.info(f"Text message processing completed for {chat_id}.")


def process_photo_message(
    message: telebot_types.Message,
    user_settings: UserSettings,
    ai_client: genai.Client,
    bot_instance: telebot.TeleBot,
) -> None:
    """Processes a photo message (with or without caption) and interacts with the AI."""
    chat_id = message.chat.id
    user_caption = (
        message.caption if message.caption and isinstance(message.caption, str) else ""
    )
    logger.info(
        f"Processing photo message for {chat_id} with caption: {user_caption[:50]}..."
    )

    if bot_instance is None:
        logger.error(f"Bot instance is None in process_photo_message for {chat_id}.")
        return

    try:
        user_input_parts: list[genai_types.Part] = []
        if user_caption:
            user_input_parts.append(genai_types.Part(text=user_caption))

        logger.info(f"Downloading photo file for {chat_id}...")
        if not message.photo:
            logger.error(
                f"Photo message {message.message_id} has no photo parts for {chat_id}."
            )
            bot_instance.reply_to(
                message,
                "It seems there was an issue with the photo you sent. Please try again.",
            )
            return

        file_id = message.photo[-1].file_id  # Get largest photo (last in list)

        file_info = bot_instance.get_file(file_id)
        if (
            not file_info.file_path
        ):  # Telegram should always provide file_path for a valid file_id
            logger.error(
                f"Could not get file_path for photo {file_id} for chat {chat_id}"
            )
            bot_instance.reply_to(
                message,
                "Sorry, I couldn't retrieve the photo information from Telegram.",
            )
            return

        downloaded_file_bytes = bot_instance.download_file(str(file_info.file_path))

        mime_type = "image/jpeg"  # Default
        # Safely extract extension
        if "." in file_info.file_path:  # Check added previously
            ext_candidate = file_info.file_path.rsplit(".", 1)[-1].lower()
            if ext_candidate in [
                "png",
                "gif",
                "webp",
                "jpeg",
                "jpg",
            ]:
                mime_type = f"image/{ext_candidate.replace('jpg', 'jpeg')}"

        image_part = genai_types.Part(
            inline_data=genai_types.Blob(
                mime_type=mime_type, data=downloaded_file_bytes
            )
        )
        user_input_parts.append(image_part)

        _handle_ai_interaction(
            message, user_settings, ai_client, user_input_parts, bot_instance
        )
        logger.info(f"Photo message processing completed for {chat_id}.")

    except Exception as e:
        logger.error(
            f"Error preparing or downloading photo for {chat_id}: {e}", exc_info=True
        )
        if bot_instance is not None:
            bot_instance.reply_to(
                message, "Sorry, I encountered an error processing the image."
            )
