from time import time
import json
import logging
import telebot
from google import genai
from typing import Callable
from telebot import types as telebot_types
from google.genai import types as genai_types
from google.genai.errors import ClientError, ServerError
from google.api_core.exceptions import (
    NotFound,
    GoogleAPIError,
    PermissionDenied,
    ResourceExhausted,
)
from config import DEFAULT_MODEL_NAME
from custom_types import UserSettings
from db import (
    get_history_from_db,
    save_turn_to_db,
    get_supabase_client,
    get_user_settings_from_db,
    save_user_settings_to_db,
    clear_history_in_db,
)
from helpers import (
    split_and_send_message,
    check_db_and_settings,
    check_ai_client,
    check_message_limit_and_increment,
)

logger = logging.getLogger(__name__)

user_temp_state: dict[int, dict] = {}


def _handle_ai_interaction(
    message: telebot_types.Message,
    user_settings: UserSettings,
    ai_client: genai.Client,
    user_input_parts: list[genai_types.Part],
    bot_instance: telebot.TeleBot,
):
    """
    Handles the core AI chat interaction: fetching history, creating chat,
    sending message, saving new turns, getting response, and sending reply.
    """
    chat_id = message.chat.id
    model_for_user = user_settings.get("selected_model", DEFAULT_MODEL_NAME)
    try:
        waiting_animation = bot_instance.send_animation(chat_id, animation='BAACAgQAAxkBAAICLmgNboExi8JGRByeLZgG33L2a0W6AALWGwACJbhpUMP9FGIy5-vmNgQ')
        history_content = get_history_from_db(chat_id)
        if history_content is None:
            bot_instance.reply_to(
                message, "Error fetching chat history from the database."
            )
            logger.error(
                f"AI interaction failed for {chat_id}: Error fetching history."
            )
            return

        chat = None
        try:
            chat = ai_client.chats.create(model=model_for_user, history=history_content)
            logger.info(
                f"Chat object created for {chat_id} with model {model_for_user}."
            )
        except NotFound as nf_e:
            logger.error(
                f"Model not found/supported for chat creation ({model_for_user}) for {chat_id}: {nf_e}",
                exc_info=True,
            )
            error_message = f"The selected model `{model_for_user}` is not available or supported for conversations with your API key.\n\nPlease use `/select_model` to choose a different model."
            bot_instance.reply_to(message, error_message, parse_mode="Markdown")
            return
        except Exception as create_e:
            logger.error(
                f"Error during chat creation for {chat_id} with model {model_for_user}: {create_e}",
                exc_info=True,
            )
            bot_instance.reply_to(
                message,
                f"An error occurred while starting the conversation with model `{model_for_user}`: {create_e}",
            )
            return

        logger.info(f"Calling chat.send_message for {chat_id}...")
        response = None
        try:
            response = chat.send_message(user_input_parts)
            logger.info(f"chat.send_message completed for {chat_id}.")

        except ClientError as client_error:
            logger.error(
                f"ClientError during chat.send_message for {chat_id}: {client_error}",
                exc_info=True,
            )
            status_code = client_error.code
            response_json = client_error.details

            user_facing_error = (
                f"An AI communication error occurred (Code: {status_code})."
            )
            links_text = ""

            if status_code == 429:
                user_facing_error = f"Your request failed due to a quota limit being reached for the selected model (`{model_for_user}`)."
                try:
                    if (
                        isinstance(response_json, dict)
                        and "error" in response_json
                        and "details" in response_json["error"]
                    ):
                        for detail in response_json["error"]["details"]:
                            if (
                                isinstance(detail, dict)
                                and detail.get("@type")
                                == "type.googleapis.com/google.rpc.Help"
                                and detail.get("links")
                            ):
                                for link in detail["links"]:
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
                                for link in detail["links"]:
                                    if (
                                        isinstance(link, dict)
                                        and link.get("description")
                                        and link.get("url")
                                    ):
                                        links_text += f"\n\n[{link['description']}]({link['url']})"
                                        break
                                break
                    else:
                        logger.warning(
                            f"429 error details not in expected dict/list format for {chat_id}. Details: {response_json}"
                        )

                except Exception as link_parse_e:
                    logger.error(
                        f"Failed to parse help links from 429 error details for {chat_id}: {link_parse_e}",
                        exc_info=True,
                    )
                    links_text = "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits"

                user_facing_error += links_text

            elif status_code == 400:
                error_detail_message = (
                    response_json.get("error", {}).get("message")
                    if isinstance(response_json, dict)
                    else str(response_json)
                )
                error_status_field = (
                    response_json.get("error", {}).get("status")
                    if isinstance(response_json, dict)
                    else None
                )

                if error_status_field == "BLOCKED" or (
                    error_detail_message
                    and (
                        "blocked" in error_detail_message.lower()
                        or "safety" in error_detail_message.lower()
                    )
                ):
                    user_facing_error = f"Your input was blocked by safety filters."
                    if (
                        isinstance(response_json, dict)
                        and "error" in response_json
                        and "details" in response_json["error"]
                    ):
                        safety_details = response_json["error"]["details"]
                        if safety_details:
                            try:
                                for detail in safety_details:
                                    if (
                                        isinstance(detail, dict)
                                        and detail.get("@type")
                                        == "type.googleapis.com/google.rpc.BadRequest"
                                        and detail.get("fieldViolations")
                                    ):
                                        violation_messages = [
                                            v.get("description", v.get("field", ""))
                                            for v in detail["fieldViolations"]
                                            if isinstance(v, dict)
                                        ]
                                        if violation_messages:
                                            user_facing_error += f" Reason(s): {', '.join(violation_messages)}"
                            except Exception as safety_detail_parse_e:
                                logger.error(
                                    f"Failed to parse safety violation details for {chat_id}: {safety_detail_parse_e}",
                                    exc_info=True,
                                )
                    elif isinstance(response_json, list):
                        pass

                    if error_detail_message and (
                        "LENGTH" in error_detail_message.upper()
                        or "CONTEXT" in error_detail_message.upper()
                        or "TOO_LARGE" in error_detail_message.upper()
                    ):
                        user_facing_error += "\n\nYour conversation history might be too long for the model. Try using `/reset` to start a new chat."  # Added Markdown to command

                else:
                    user_facing_error = f"Bad request to the AI model. Message: {error_detail_message or 'N/A'}"

            disable_preview = True if status_code == 429 and links_text else False
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
            user_facing_error = f"The AI service encountered a server error (Code: {server_error.code}). Please try again later."
            bot_instance.reply_to(message, user_facing_error)
            return

        except (NotFound, ResourceExhausted) as api_core_error:
            logger.error(
                f"Specific Google API Core Error during chat.send_message for {chat_id}: {api_core_error}",
                exc_info=True,
            )
            if isinstance(api_core_error, NotFound):
                error_message = f"The selected model `{model_for_user}` is not available or supported for conversations with your API key.\n\nPlease use `/select_model` to choose a different model."
            elif isinstance(api_core_error, ResourceExhausted):
                error_message = (
                    f"Your request failed due to a quota limit being reached\\c."
                )
                error_message += "\n\nLearn more about Gemini API quotas\\: https://ai.google.dev/gemini-api/docs/rate-limits"
            else:
                error_message = f"An AI service error occurred: {api_core_error}"

            bot_instance.reply_to(
                message,
                error_message,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        except GoogleAPIError as api_error:
            logger.error(
                f"General GoogleAPIError fallback during chat.send_message for {chat_id}: {api_error}",
                exc_info=True,
            )
            bot_instance.reply_to(
                message, f"A general AI service error occurred: {api_error}"
            )
            return

        except Exception as send_e:
            logger.error(
                f"Truly unexpected Error during chat.send_message for {chat_id}: {send_e}",
                exc_info=True,
            )
            bot_instance.reply_to(
                message, "An unexpected internal error occurred during AI interaction."
            )
            return

        if chat and response:
            updated_history = chat.get_history()
            original_history_length = len(history_content)
            updated_history_length = len(updated_history)

            if updated_history_length >= original_history_length + 2:
                new_user_turn = updated_history[-2]
                new_model_turn = updated_history[-1]
                user_turn_db_index = original_history_length
                model_turn_db_index = original_history_length + 1

                logger.info(
                    f"Saving new turns to DB for {chat_id}. User index {user_turn_db_index}, Model index {model_turn_db_index}"
                )
                save_turn_to_db(
                    chat_id, user_turn_db_index, new_user_turn.role, user_input_parts
                )
                save_turn_to_db(
                    chat_id,
                    model_turn_db_index,
                    new_model_turn.role,
                    new_model_turn.parts,
                )

            elif updated_history_length > original_history_length:
                logger.warning(
                    f"History grew by less than 2 turns for {chat_id}. Original: {original_history_length}, Updated: {updated_history_length}. Attempting to save user turn if present."
                )
                if (
                    updated_history[-1].role == "user"
                    and updated_history_length == original_history_length + 1
                ):
                    new_user_turn = updated_history[-1]
                    user_turn_db_index = original_history_length
                    save_turn_to_db(
                        chat_id,
                        user_turn_db_index,
                        new_user_turn.role,
                        user_input_parts,
                    )
                    logger.warning(f"Saved only the new user turn for {chat_id}.")
                else:
                    logger.warning(
                        f"Unexpected state: History grew by {updated_history_length - original_history_length} but last turn isn't user or growth isn't 1. Not saving turns."
                    )
            else:
                logger.warning(
                    f"History did not grow after send_message for {chat_id}. Nothing to save."
                )

            model_response_text = ""
            if response and hasattr(response, "text") and response.text is not None:
                model_response_text = response.text
            elif response and hasattr(response, "candidates") and response.candidates:
                logger.info(
                    f"Response for {chat_id} has candidates but no simple .text. Attempting to extract text parts."
                )
                text_parts = [
                    p.text
                    for c in response.candidates
                    for p in c.content.parts
                    if hasattr(p, "text") and p.text is not None
                ]
                if text_parts:
                    model_response_text = "".join(text_parts)
                    logger.info(f"Extracted text from parts for {chat_id}.")
                else:
                    tool_call_parts = [
                        p.function_call
                        for c in response.candidates
                        for p in c.content.parts
                        if hasattr(p, "function_call") and p.function_call is not None
                    ]
                    tool_response_parts = [
                        p.function_response
                        for c in response.candidates
                        for p in c.content.parts
                        if hasattr(p, "function_response")
                        and p.function_response is not None
                    ]

                    if tool_call_parts:
                        model_response_text = "Model wants to call a function:\n" + "\n".join(
                            [
                                f"- `{tc.name}`(`{json.dumps(tc.args) if tc.args else ''}`)"
                                for tc in tool_call_parts
                            ]
                        )
                        logger.info(f"Formatted tool calls for {chat_id}.")
                    elif tool_response_parts:
                        model_response_text = "Model received a function response."
                        logger.info(f"Model received function response for {chat_id}.")
                    else:
                        model_response_text = "Received a non-text response without recognizable parts. Please check logs if unexpected."
                        logger.warning(
                            f"Non-text, non-tool response received for {chat_id}. Response object: {response}"
                        )

            else:
                model_response_text = "Could not get a text response from the model."
                logger.warning(
                    f"Could not get text response for {chat_id}. Response object: {response}"
                )

            logger.info(f"Replying to {chat_id}...")
            split_and_send_message(message, model_response_text, bot_instance)
            # Remove progressing text after message is sent
            bot_instance.delete_message(waiting_animation.chat.id, waiting_animation.message_id)

        else:
            logger.error(
                f"AI interaction completed without raising exception but response object is missing/invalid for {chat_id}."
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
            f"Error preparing or downloading photo for {chat_id}: {outer_e}", exc_info=True
        )
        bot_instance.reply_to(
            message, "Sorry, I encountered an error processing the image."
        )


def process_user_message(
    message: telebot_types.Message,
    process_logic_func: Callable[
        [telebot_types.Message, UserSettings, genai.Client, telebot.TeleBot], None
    ],
    bot_instance: telebot.TeleBot,
):
    """
    Wrapper function to handle common checks (DB, settings, AI client, message limit,
    API key input state) before calling the specific message processing logic.
    Calls process_logic_func(message, user_settings, ai_client) if checks pass.
    """
    chat_id = message.chat.id

    if bot_instance is None:
        logger.error(
            f"Bot instance is None in _process_user_message for {chat_id}. Cannot send replies."
        )
        return

    if user_temp_state.pop(chat_id, {}).get("awaiting_api_key"):
        api_key = (
            message.text.strip() if hasattr(message, "text") and message.text else ""
        )
        logger.info(f"Processing interactive API key input for {chat_id}")

        if not api_key:
            bot_instance.reply_to(
                message, "API key cannot be empty\\.", parse_mode="MarkdownV2"
            )
            logger.warning(f"Empty API key provided by user {chat_id}.")
            return

        try:
            logger.info(f"Attempting to validate API key for {chat_id}...")
            temp_client = genai.Client(api_key=api_key)
            logger.info(
                f"Performing small test call for key starting with {api_key[:4]}..."
            )
            temp_client.models.list()
            logger.info(f"API key validation successful for {chat_id}.")

            db_client = get_supabase_client()
            if not db_client:
                bot_instance.reply_to(
                    message,
                    "Database service is not available\\. Cannot save API key\\.",
                    parse_mode="MarkdownV2",
                )
                logger.error(f"DB unavailable for API key save for {chat_id}.")
                return

            user_settings = get_user_settings_from_db(chat_id)
            if user_settings is None:
                bot_instance.reply_to(
                    message,
                    "Error fetching your settings before saving key\\.",
                    parse_mode="MarkdownV2",
                )
                logger.error(f"Failed to fetch settings before key save for {chat_id}.")
                return

            if save_user_settings_to_db(
                chat_id,
                api_key=api_key,
                model_name=user_settings.get("selected_model", DEFAULT_MODEL_NAME),
                message_count=0,
            ):
                clear_history_in_db(chat_id)
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
                logger.error(
                    f"Interactive API key save failed for {chat_id}: Failed to save to "
                )

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

    user_settings = check_db_and_settings(chat_id, message, bot_instance)
    if user_settings is None:
        return

    if not check_message_limit_and_increment(
        chat_id, message, user_settings, bot_instance
    ):
        return

    ai_client = check_ai_client(chat_id, message, user_settings, bot_instance)
    if ai_client is None:
        return

    # --- If all checks pass, call the specific processing logic ---
    process_logic_func(message, user_settings, ai_client, bot_instance)


def process_text_message(
    message: telebot_types.Message,
    user_settings: UserSettings,
    ai_client: genai.Client,
    bot_instance: telebot.TeleBot,
):
    """Processes a text message and interacts with the AI."""
    chat_id = message.chat.id
    user_message = message.text
    logger.info(f"Processing text message for {chat_id}: {user_message}")

    if bot_instance is None:
        logger.error(f"Bot instance is None in process_text_message for {chat_id}.")
        return

    user_input_parts: list[genai_types.Part] = [genai_types.Part(text=user_message)]

    _handle_ai_interaction(
        message, user_settings, ai_client, user_input_parts, bot_instance
    )
    logger.info(f"Text message processing completed for {chat_id}.")


def process_photo_message(
    message: telebot_types.Message,
    user_settings: UserSettings,
    ai_client: genai.Client,
    bot_instance: telebot.TeleBot,
):
    """Processes a photo message (with or without caption) and interacts with the AI."""
    chat_id = message.chat.id
    user_caption = message.caption
    logger.info(f"Processing photo message for {chat_id} with caption: {user_caption}")

    if bot_instance is None:
        logger.error(
            f"Bot instance is None in process_photo_message for {chat_id}. Cannot process photo."
        )
        return

    try:
        user_input_parts: list[genai_types.Part] = []
        if user_caption:
            user_input_parts.append(genai_types.Part(text=user_caption))

        logger.info(f"Downloading photo file for {chat_id}...")
        # Get the largest photo size (last element in the list)
        if not message.photo:
            logger.error(
                f"Photo message {message.message_id} has no photo parts for {chat_id}."
            )
            # bot_instance.reply_to is called by _process_user_message's checks or _handle_ai_interaction if AI fails.
            # If we reach here, it's likely a rare structure issue in the message object.
            # Rely on outer exception handler or add specific reply here if needed.
            raise ValueError("Photo message has no photo parts.")

        file_id = message.photo[-1].file_id

        file_info = bot_instance.get_file(file_id)
        downloaded_file_bytes = bot_instance.download_file(file_info.file_path)
        mime_type = "image/jpeg"
        if file_info and file_info.file_path and "." in file_info.file_path:
            ext = file_info.file_path.rsplit(".", 1)[-1].lower()
            if ext in ["png", "gif", "webp"]:
                mime_type = f"image/{ext}"

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
        # If bot_instance.instance was available at the start, reply. If not, we logged the error earlier.
        if bot_instance is not None:
            bot_instance.reply_to(
                message, "Sorry, I encountered an error processing the image."
            )
