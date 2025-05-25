import asyncio
import logging
import os
import re
from typing import Any, Callable, Coroutine

from google import genai
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.api_core.exceptions import (
    ClientError,
    GoogleAPIError,
    NotFound,
    PermissionDenied,
    ResourceExhausted,
    ServerError,
)
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from telebot import types as telebot_types
from telebot.async_telebot import AsyncTeleBot

from .config import DEFAULT_MODEL_NAME, GOOGLE_API_KEY, LOADING_ANIMATION_FILE_ID
from .custom_types import AIInteractionContext, UserSettings
from .db import get_history_from_db, save_turn_to_db
from .helpers import (
    check_db_and_settings,
    check_message_limit_and_increment,
    split_and_send_message,
)
from .multi_tool_agent.agent import TelegramBotAgent, get_or_create_agent

logger = logging.getLogger(__name__)

# Regex to find URLs in text
URL_REGEX = r"(?:(?:https?|ftp):\/\/|www\.)(?:\([-A-Z0-9+&@#\/%?=~_|$!:,.;]*\)|[-A-Z0-9+&@#\/%?=~_|$!:,.;])*(?:\([-A-Z0-9+&@#\/%?=~_|$!:,.;]*\)|[A-Z0-9+&@#\/%?=~_|$])"

# --- ADK Session Management ---
_adk_session_service: InMemorySessionService = InMemorySessionService()  # type: ignore[no-any-unimported]

# Module-level client for google.genai.
_default_genai_client_instance: genai.Client | None = None
_current_client_api_key_for_default_client: str | None = (
    "__INITIAL_UNSET__"  # Sentinel to force first init
)


async def _ensure_default_genai_client(
    api_key_to_use: str | None,
) -> genai.Client | None:
    """
    Ensures a default genai.Client instance is created with the appropriate API key.
    This client is intended to be implicitly picked up by ADK Agent's internal
    GenerativeModel instantiations if no other client context is found.
    Returns the client instance, or None if creation failed.
    """
    global _default_genai_client_instance, _current_client_api_key_for_default_client

    key_for_client_init = (
        api_key_to_use if api_key_to_use is not None else GOOGLE_API_KEY
    )

    if not key_for_client_init:
        logger.error(
            "Cannot create default genai.Client: No API key available (user key was None, and bot's GOOGLE_API_KEY from config is also not set)."
        )
        _default_genai_client_instance = None
        _current_client_api_key_for_default_client = None
        return None

    if (
        _default_genai_client_instance is not None
        and _current_client_api_key_for_default_client == key_for_client_init
    ):
        return _default_genai_client_instance

    try:
        prev_key_log = (
            _current_client_api_key_for_default_client
            if _current_client_api_key_for_default_client != "__INITIAL_UNSET__"
            else "initial"
        )
        logger.info(
            f"Creating/Recreating default genai.Client. New key source: {'user-provided' if api_key_to_use else 'bot-default'}. Previous client key: {prev_key_log}"
        )
        _default_genai_client_instance = genai.Client(api_key=key_for_client_init)
        _current_client_api_key_for_default_client = key_for_client_init
        logger.info(
            f"Successfully created default genai.Client with key ending ...{key_for_client_init[-4:] if key_for_client_init and len(key_for_client_init) > 3 else ''}"
        )
        return _default_genai_client_instance
    except Exception as e:
        logger.error(
            f"Failed to create default genai.Client with key ending ...{key_for_client_init[-4:] if key_for_client_init and len(key_for_client_init) > 3 else ''}: {e}",
            exc_info=True,
        )
        _default_genai_client_instance = None
        _current_client_api_key_for_default_client = None  # Mark as failed
        return None


def _extract_urls(text: str) -> list[str]:
    """Extracts all URLs from a given text."""
    if not text:
        return []
    return re.findall(URL_REGEX, text, re.IGNORECASE)


async def _process_urls_directly(
    message: telebot_types.Message,
    bot_instance: AsyncTeleBot,
    active_genai_client: genai.Client | None,
    model_for_agent: str,
    user_turn_content: genai_types.Content,
    waiting_animation: telebot_types.Message | None,
    caption_updated_for_tool: bool,
    chat_id: int,
    urls_found: list[str],  # Should be non-empty if this function is called
) -> tuple[bool, bool]:  # (handled_fully, caption_updated)
    """
    Attempts to process the user's message directly using the URL tool if URLs are present.
    Returns True if the message was fully handled (response sent, DB updated), False otherwise.
    Also returns the updated state of caption_updated_for_tool.
    """
    logger.info(
        f"URLs found for chat {chat_id}: {urls_found}. Attempting direct processing."
    )

    if not active_genai_client:
        logger.warning(
            f"URL Tool ({chat_id}): active_genai_client (default client for URL tool) is None. Direct URL tool might fail or skip."
        )
        # Depending on strictness, could return False here, but let's allow attempt if model supports
        # and let the API call fail if client is truly unusable.

    if waiting_animation and not caption_updated_for_tool:
        try:
            await bot_instance.edit_message_caption(
                caption="Accessing information from URLs... 🔗",
                chat_id=chat_id,
                message_id=waiting_animation.message_id,
            )
            caption_updated_for_tool = True
        except Exception as e_caption_url:
            logger.warning(
                f"Failed to edit 'Thinking...' animation caption for URL processing: {e_caption_url}"
            )

    supported_url_models = [
        "gemini-2.5-flash-preview-05-20",
        "gemini-2.5-pro-preview-05-06",
        "gemini-2.0-flash",
        "gemini-2.0-flash-live-001",
    ]

    if model_for_agent not in supported_url_models:
        logger.warning(
            f"Model {model_for_agent} not in list of known URL context supported models. Skipping direct URL processing."
        )
        return False, caption_updated_for_tool  # Not handled, fall through to ADK agent

    try:
        if not active_genai_client:
            logger.error(
                f"URL Tool ({chat_id}): active_genai_client is None. Cannot proceed with URL processing."
            )
            raise ValueError("Default genai.Client not available for URL tool.")

        url_tool = genai_types.Tool(url_context=genai_types.UrlContext())
        url_processing_config = genai_types.GenerateContentConfig(
            tools=[url_tool], response_mime_type="text/plain"
        )

        direct_response = await asyncio.to_thread(
            active_genai_client.models.generate_content,
            model=f"models/{model_for_agent}",
            contents=user_turn_content,  # Use the passed user_turn_content
            config=url_processing_config,
        )

        response_text_from_url_tool = ""
        agent_response_content_for_db_url_tool: genai_types.Content | None = None

        if direct_response.candidates:
            first_candidate = direct_response.candidates[0]
            if first_candidate.content:
                agent_response_content_for_db_url_tool = first_candidate.content
                if first_candidate.content.parts:
                    for part in first_candidate.content.parts:
                        if part.text:
                            response_text_from_url_tool += part.text

        if not response_text_from_url_tool:
            response_text_from_url_tool = (
                "I looked at the URL(s) but couldn't extract a text summary."
            )

        if (
            agent_response_content_for_db_url_tool
            and not agent_response_content_for_db_url_tool.role
        ):
            agent_response_content_for_db_url_tool.role = "model"

        logger.info(
            f"Direct URL processing successful for chat {chat_id}. Response: '{response_text_from_url_tool[:100]}...'"
        )

        # Save turns and send response

        if agent_response_content_for_db_url_tool:
            # Re-fetch history to get the correct index for the model turn,
            # as the user turn was saved just before _handle_ai_interaction
            history_before_model_save = await get_history_from_db(chat_id)
            model_turn_idx_for_url = (
                len(history_before_model_save)
                if history_before_model_save is not None
                else 0
            )

            if not await save_turn_to_db(
                chat_id,
                turn_index=model_turn_idx_for_url,
                role=agent_response_content_for_db_url_tool.role,
                parts=agent_response_content_for_db_url_tool.parts,
            ):
                logger.error(
                    f"Failed to save agent response (URL direct) to DB for {chat_id} at index {model_turn_idx_for_url}"
                )

        await split_and_send_message(message, response_text_from_url_tool, bot_instance)

        if waiting_animation:
            try:
                await bot_instance.delete_message(chat_id, waiting_animation.message_id)
            except Exception as del_e:
                logger.warning(
                    f"Failed to delete waiting animation (URL direct): {del_e}"
                )
        return True, caption_updated_for_tool  # Handled fully

    except Exception as e_direct_url:
        logger.error(
            f"Error during direct URL processing for chat {chat_id}: {e_direct_url}",
            exc_info=True,
        )
        await split_and_send_message(
            message,
            "Sorry, I had trouble processing the content from the URL(s). I'll try to respond based on your text alone.",
            bot_instance,
        )
        # Not fully handled, fall through to ADK agent.
        return False, caption_updated_for_tool


async def _handle_ai_interaction_error(
    e: Exception,
    message: telebot_types.Message,
    bot_instance: AsyncTeleBot,
    chat_id: int,
    model_for_agent: str,
) -> None:
    """Handles logging and sending user-facing messages for errors during AI interaction."""
    logger.error(
        f"AI Interaction Error for chat {chat_id} (Model: {model_for_agent}): {type(e).__name__} - {str(e)}",
        exc_info=True,
    )
    error_message_to_send = "I encountered an unexpected problem. I've logged the details. Please try again later."

    match e:
        case genai_errors.ClientError():
            specific_log_msg = (
                f"GenAI ClientError for chat {chat_id} "
                f"(Status: {e.status if hasattr(e, 'status') else 'N/A'}, "
                f"Code: {e.code if hasattr(e, 'code') else 'N/A'}): "
                f"{e.message if hasattr(e, 'message') else str(e)}"
            )
            logger.error(specific_log_msg)

            msg_lower = (
                str(e.message).lower() if hasattr(e, "message") and e.message else ""
            )
            status_str = (
                str(e.status).lower() if hasattr(e, "status") and e.status else ""
            )

            is_quota_error = (
                (hasattr(e, "code") and e.code == 429)
                or "resource_exhausted" in status_str
                or (
                    "quota" in msg_lower
                    and ("rate_limit" in msg_lower or "exceeded" in msg_lower)
                )
            )

            if is_quota_error:
                if (
                    "free quota tier" in msg_lower
                    or "doesn't have a free quota tier" in msg_lower
                ):
                    error_message_to_send = (
                        f"The selected model (`{model_for_agent}`) may require a paid API key, "
                        "but the current API key appears to be on a free tier. \n\n"
                        "Please consider the following:\n"
                        "1. If you're using your own API key (set via /set_api_key), ensure it has billing enabled and supports this model. "
                        "You can check your Google AI Studio or Google Cloud console.\n"
                        "2. Select a model that is available on the free tier (using /select_model).\n"
                        "3. If you are using the bot's default key, the bot admin may need to check its configuration."
                    )
                else:
                    error_message_to_send = (
                        "The API quota has been exceeded. \n"
                        "If you're using your own API key (set via `/set_api_key`), please check your usage limits in your Google Cloud/AI Studio console. "
                        "Otherwise, the bot's overall limit might have been reached; please try again later."
                    )
            elif (
                (hasattr(e, "code") and e.code == 403)
                or "permission_denied" in status_str
                or "permission denied" in msg_lower
            ):
                error_message_to_send = "I don't have permission to perform this action. This might be due to the API key."
                if (
                    "api_key_invalid" in msg_lower
                    or "api_key_service_blocked" in msg_lower
                ):
                    error_message_to_send = "The API key is invalid or blocked. Please check your key or contact the bot admin if you are using the default key."
            elif (
                (hasattr(e, "code") and e.code == 404)
                or "not_found" in status_str
                or "not found" in msg_lower
            ):
                error_message_to_send = f"The AI model (`{model_for_agent}`) or a required resource could not be found. You can check available models with `/list_models` or try selecting a different one."
            elif "api key not valid" in msg_lower or "api_key_invalid" in msg_lower:
                error_message_to_send = "The API key being used is not valid. If you set one with `/set_api_key`, please verify it. Otherwise, contact the bot admin."
            elif "billing account" in msg_lower:
                error_message_to_send = "There seems to be an issue with the billing account associated with the API key. If you're using your own key, please check your Google Cloud/AI Studio console. Bot admin may need to check theirs if you're on default."
            elif (
                (hasattr(e, "code") and e.code == 400)
                or "invalid_argument" in status_str
                or "invalid argument" in msg_lower
            ):
                error_message_to_send = f"There was an issue with the request sent to the AI service (Invalid Argument): {e.message[:100] if hasattr(e, 'message') and e.message else str(e)[:100]}"
            else:
                error_message_to_send = f"An AI service client error occurred: {e.message[:150] if hasattr(e, 'message') and e.message else str(e)[:150]}"

        case genai_errors.ServerError():
            logger.error(
                f"GenAI ServerError for chat {chat_id} (Status: {e.status if hasattr(e, 'status') else 'N/A'}): {e.message if hasattr(e, 'message') else str(e)}"
            )
            error_message_to_send = f"The AI service is currently unavailable or encountered a server error (Status: {e.status if hasattr(e, 'status') else 'N/A'}). Please try again later."

        case genai_errors.APIError():  # Catch other genai_errors
            logger.error(
                f"GenAI APIError for chat {chat_id} (Status: {e.status if hasattr(e, 'status') else 'N/A'}): {e.message if hasattr(e, 'message') else str(e)}"
            )
            error_message_to_send = f"An unexpected AI service API error occurred (Status: {e.status if hasattr(e, 'status') else 'N/A'}). Please try again."

        # google.api_core.exceptions
        case PermissionDenied():
            logger.error(f"Google API Core Permission denied for chat {chat_id}: {e}")
            error_message_to_send = (
                "A general permission error occurred with a Google service."
            )
        case ResourceExhausted():
            logger.error(f"Google API Core Resource exhausted for chat {chat_id}: {e}")
            error_message_to_send = "A general resource limit was reached with a Google service. This might be related to API quotas."
        case NotFound():
            logger.error(f"Google API Core Not Found for chat {chat_id}: {e}")
            error_message_to_send = (
                "A required Google service or resource was not found."
            )
        case (
            ClientError()
        ):  # This is the aliased google.api_core.exceptions.ClientError
            logger.error(
                f"A Google API Core ClientError occurred for chat {chat_id}: {e}"
            )
            error_message_to_send = (
                "A client-side error occurred with a Google service."
            )
        case (
            GoogleAPIError()
        ):  # This is the aliased google.api_core.exceptions.GoogleAPIError
            logger.error(
                f"A Google API Core GoogleAPIError occurred for chat {chat_id}: {e}"
            )
            error_message_to_send = "A general Google API error occurred."
        case _:  # Default case for any other exceptions
            # Already logged with exc_info by the caller of this helper
            pass  # error_message_to_send already has a default

    await split_and_send_message(message, error_message_to_send, bot_instance)


async def _process_adk_events_and_get_response(  # type: ignore[no-any-unimported]
    runner: Runner,
    user_id_for_agent: str,
    session_id_for_agent: str,
    adk_content_for_user_turn: genai_types.Content,
    bot_instance: AsyncTeleBot,
    waiting_animation: telebot_types.Message | None,
    initial_caption_updated_for_tool: bool,
    chat_id: int,
) -> tuple[str, genai_types.Content | None, dict | None, dict | None, bool, bool]:
    """
    Iterates through ADK runner events, processes tool calls/responses,
    and extracts the final agent response.

    Returns:
        - response_text_from_agent (str)
        - agent_response_content_for_db (genai_types.Content | None)
        - audio_file_to_send (dict | None)
        - image_file_to_send (dict | None)
        - processing_completed_successfully (bool)
        - caption_updated_for_tool (bool)
    """
    response_text_from_agent = "Agent did not provide a response."
    agent_response_content_for_db: genai_types.Content | None = None
    audio_file_to_send: dict | None = None
    image_file_to_send: dict | None = None
    processing_completed_successfully = False
    caption_updated_for_tool = initial_caption_updated_for_tool

    async for event in runner.run_async(
        user_id=user_id_for_agent,
        session_id=session_id_for_agent,
        new_message=adk_content_for_user_turn,
    ):
        logger.debug(
            f"ADK Event Loop for {chat_id} - Received event: {event}"
        )  # ADDED LOG
        if (
            not caption_updated_for_tool
            and event.content
            and event.content.parts
            and waiting_animation
        ):
            for part in event.content.parts:
                if part.function_call:
                    tool_name = part.function_call.name
                    new_caption = None
                    # Match against the names LLM uses (wrapper names)
                    if tool_name == "generate_speech":
                        new_caption = "Generating audio with Text-to-Speech... 🎤"
                    elif tool_name == "generate_image":
                        new_caption = "Creating image with Imagen... 🎨"
                    elif tool_name == "get_weather":
                        new_caption = "Fetching weather information... 🌦️"

                    if new_caption:
                        try:
                            await bot_instance.edit_message_caption(
                                caption=new_caption,
                                chat_id=chat_id,
                                message_id=waiting_animation.message_id,
                            )
                            caption_updated_for_tool = True
                            break
                        except Exception as e_caption:
                            logger.warning(
                                f"Failed to edit 'Thinking...' animation caption: {e_caption}"
                            )
                    if caption_updated_for_tool or new_caption:
                        break

        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_response:
                    tool_name = part.function_response.name
                    response_data = part.function_response.response
                    if isinstance(response_data, dict):
                        if response_data.get("status") == "success":
                            match tool_name:
                                case "generate_speech":
                                    audio_file_to_send = {
                                        "file_path": response_data.get("file_path"),
                                        "mime_type": response_data.get("mime_type"),
                                    }
                                    logger.info(
                                        f"ADK Speech tool successful for {chat_id}. File: {audio_file_to_send.get('file_path')}"
                                    )
                                case "generate_image":
                                    image_file_to_send = {
                                        "file_path": response_data.get("file_path"),
                                        "mime_type": response_data.get("mime_type"),
                                    }
                                    logger.info(
                                        f"ADK Image tool successful for {chat_id}. File: {image_file_to_send.get('file_path')}"
                                    )
                                # Add other successful tool cases here if needed
                                case _:
                                    logger.info(
                                        f"Tool '{tool_name}' completed successfully with response: {response_data}"
                                    )

                        elif response_data.get("status") == "error":
                            tool_error_message = response_data.get(
                                "message", "Unknown tool error"
                            )
                            logger.error(
                                f"ADK Tool '{tool_name}' failed for {chat_id}: {tool_error_message}. Full response: {response_data}"
                            )
                            current_llm_text = "".join(
                                p.text
                                for p in event.content.parts
                                if hasattr(p, "text") and p.text
                            ).strip()
                            if (
                                not current_llm_text
                                or "sorry" in current_llm_text.lower()
                                or "apologize" in current_llm_text.lower()
                            ):
                                response_text_from_agent = (
                                    f"Tool Error: {tool_error_message}"
                                )
                            else:
                                response_text_from_agent = f"{current_llm_text}\n\nTool Error : {tool_error_message}"

        logger.debug(
            f"ADK Event Loop for {chat_id} - Checking if event is final_response. Event: {event}"
        )
        if event.is_final_response():
            logger.debug(f"Final ADK Event for {chat_id}: {event}")
            current_final_text = ""
            if event.content:
                logger.debug(f"Final ADK Event content for {chat_id}: {event.content}")
                if event.content.parts:
                    logger.debug(
                        f"Final ADK Event parts for {chat_id}: {event.content.parts}"
                    )
                    for i, part_item in enumerate(event.content.parts):
                        logger.debug(
                            f"Final ADK Event part {i} for {chat_id}: {part_item}"
                        )
                        if hasattr(part_item, "text") and part_item.text:
                            current_final_text += part_item.text
                else:
                    logger.debug(f"Final ADK Event for {chat_id} has no parts.")
            else:
                logger.debug(f"Final ADK Event for {chat_id} has no content.")

            logger.debug(
                f"Extracted current_final_text for {chat_id}: '{current_final_text}'"
            )
            logger.debug(
                f"response_text_from_agent before final logic for {chat_id}: '{response_text_from_agent}'"
            )

            if (
                response_text_from_agent == "Agent did not provide a response."
                or not response_text_from_agent.strip()
                or "Tool Error" not in response_text_from_agent
            ):
                response_text_from_agent = current_final_text
                logger.debug(
                    f"Set response_text_from_agent to current_final_text for {chat_id}: '{response_text_from_agent}'"
                )
            elif (
                current_final_text.strip()
                and current_final_text.strip() not in response_text_from_agent
            ):
                if not (
                    (
                        "failed" in response_text_from_agent.lower()
                        or "error" in response_text_from_agent.lower()
                    )
                    and (
                        "sorry" in current_final_text.lower()
                        or "apologize" in current_final_text.lower()
                    )
                ):
                    if "Tool Error" in response_text_from_agent and current_final_text:
                        response_text_from_agent += "\nLLM: " + current_final_text
                    elif current_final_text:
                        response_text_from_agent += "\n" + current_final_text
                    logger.debug(
                        f"Appended current_final_text to response_text_from_agent for {chat_id}: '{response_text_from_agent}'"
                    )

            logger.debug(
                f"Final response_text_from_agent for {chat_id}: '{response_text_from_agent}'"
            )

            agent_response_content_for_db = (
                event.content
                if event.content
                and event.content.parts  # Ensure parts exist if content exists
                else genai_types.Content(
                    role="model",
                    parts=[
                        genai_types.Part(
                            text=(
                                response_text_from_agent
                                if response_text_from_agent
                                else ""
                            )
                        )
                    ],  # Ensure part has text
                )
            )
            if (
                not agent_response_content_for_db.role
            ):  # Should be set if from event.content
                agent_response_content_for_db.role = "model"

            # Ensure parts list is not empty if agent_response_content_for_db was constructed
            if not agent_response_content_for_db.parts:
                agent_response_content_for_db.parts = [
                    genai_types.Part(
                        text=(
                            response_text_from_agent if response_text_from_agent else ""
                        )
                    )
                ]

            logger.info(
                f"ADK Agent final response for {chat_id}: '{response_text_from_agent[:100]}...'"
            )
            processing_completed_successfully = True
            break

    if not processing_completed_successfully:
        logger.error(
            f"ADK Agent interaction did not complete successfully for {chat_id} (no final_response event)."
        )
        if (
            not response_text_from_agent
            or response_text_from_agent == "Agent did not provide a response."
        ):
            response_text_from_agent = "Sorry, I encountered an issue while processing your request and didn't get a final answer."

        agent_response_content_for_db = genai_types.Content(
            role="model", parts=[genai_types.Part(text=response_text_from_agent)]
        )

    return (
        response_text_from_agent,
        agent_response_content_for_db,
        audio_file_to_send,
        image_file_to_send,
        processing_completed_successfully,
        caption_updated_for_tool,
    )


async def _setup_ai_interaction_context(
    message: telebot_types.Message,
    user_settings: UserSettings,
    user_input_parts: list[genai_types.Part],
    bot_instance: AsyncTeleBot,
) -> AIInteractionContext | None:
    """
    Performs initial setup for AI interaction, including user identification,
    API key and model configuration, client and agent instantiation, and ADK runner setup.
    Returns a dictionary context or None if a critical setup step fails,
    handling sending a reply to the user in case of failure.
    """
    chat_id = message.chat.id
    if message.from_user is None:
        logger.error(f"Cannot process message for {chat_id} as from_user is None.")
        await bot_instance.reply_to(
            message, "Sorry, I couldn't identify you. Please try again."
        )
        return None

    user_id_for_agent = str(message.from_user.id)
    session_id_for_agent = str(chat_id)

    model_from_settings = user_settings.get("selected_model", DEFAULT_MODEL_NAME)
    model_for_agent = model_from_settings.replace("models/", "")
    effective_api_key = user_settings.get("gemini_api_key")

    text_for_url_check = ""
    if user_input_parts:
        for part_item in user_input_parts:
            if hasattr(part_item, "text") and part_item.text:
                text_for_url_check += part_item.text + " "
                break
    urls_found = _extract_urls(text_for_url_check.strip())

    active_genai_client = await _ensure_default_genai_client(effective_api_key)
    if not active_genai_client:
        error_msg = "My AI brain isn't configured correctly. Could not initialize the AI client."
        if effective_api_key is None and not GOOGLE_API_KEY:
            error_msg += " The bot admin needs to set the GOOGLE_API_KEY environment variable, or you can set your personal API key using `/set_api_key`."
        elif effective_api_key:
            error_msg += f" Please check the API key you provided (ending with ...{effective_api_key[-4:] if effective_api_key and len(effective_api_key) > 3 else 'N/A'}). It might be invalid or lack permissions."
        elif not GOOGLE_API_KEY:
            error_msg += " The bot admin needs to set the GOOGLE_API_KEY environment variable for the bot's default operation."
        else:
            error_msg += " There was an issue initializing with the bot's default API key. Please contact the bot admin or check the key."
        await split_and_send_message(message, error_msg, bot_instance)
        return None

    agent_instance: TelegramBotAgent | None = get_or_create_agent(
        chat_id=chat_id, api_key=effective_api_key, model_name=model_for_agent
    )
    if not agent_instance:
        logger.error(f"Failed to get or create agent for chat_id {chat_id}")
        await bot_instance.reply_to(
            message,
            "Sorry, I couldn't initialize my thinking process. Please try again later.",
        )
        return None

    runner = Runner(
        app_name="TelegramGeminiBot",
        agent=agent_instance.root_agent,
        session_service=_adk_session_service,
    )

    try:
        current_session = await _adk_session_service.get_session(
            app_name=runner.app_name,
            user_id=user_id_for_agent,
            session_id=session_id_for_agent,
        )
        if not current_session:
            await _adk_session_service.create_session(
                app_name=runner.app_name,
                user_id=user_id_for_agent,
                session_id=session_id_for_agent,
            )
    except Exception as e_sess_ensure:
        logger.error(
            f"Error explicitly ensuring ADK session for {chat_id} (user: {user_id_for_agent}, session: {session_id_for_agent}): {e_sess_ensure}",
            exc_info=True,
        )
        await bot_instance.reply_to(
            message,
            "Sorry, I had trouble preparing our conversation context. Please try again.",
        )
        return None

    return AIInteractionContext(
        chat_id=chat_id,
        user_id_for_agent=user_id_for_agent,
        session_id_for_agent=session_id_for_agent,
        model_for_agent=model_for_agent,
        urls_found=urls_found,
        active_genai_client=active_genai_client,
        runner=runner,
    )


async def _finalize_interaction_and_send_response(
    message: telebot_types.Message,
    bot_instance: AsyncTeleBot,
    chat_id: int,
    response_text_from_agent: str,
    agent_response_content_for_db: genai_types.Content | None,
    audio_file_to_send: dict | None,
    image_file_to_send: dict | None,
) -> None:
    """
    Saves the agent's turn to the database and sends the response (text, audio, image) to the user.
    """
    # --- Save model's turn to DB ---
    current_history_after_adk = await get_history_from_db(chat_id)  # Get latest state
    model_turn_index = (
        len(current_history_after_adk) if current_history_after_adk is not None else 0
    )  # Next index for model response

    if agent_response_content_for_db:
        if not await save_turn_to_db(
            chat_id,
            turn_index=model_turn_index,
            role=agent_response_content_for_db.role,
            parts=agent_response_content_for_db.parts,
        ):
            logger.error(
                f"Failed to save final agent response to DB for {chat_id} at index {model_turn_index}"
            )
    else:
        logger.warning(f"No agent_response_content_for_db to save for {chat_id}")

    # --- Send response text to user ---
    if (
        response_text_from_agent and response_text_from_agent.strip()
    ):  # Check if it's not just whitespace
        await split_and_send_message(message, response_text_from_agent, bot_instance)
    else:
        # This case means the LLM genuinely returned no text, and no tool error occurred to populate a message.
        logger.info(
            f"Agent returned empty text response for chat {chat_id}. Sending a generic reply."
        )
        await split_and_send_message(
            message,
            "I received your message, but I don't have a specific text response for that.",
            bot_instance,
        )

    # --- Send any generated files (audio/image) ---
    logger.debug(f"Finalizing: audio_file_to_send = {audio_file_to_send}")
    if audio_file_to_send and audio_file_to_send.get("file_path"):
        audio_path = audio_file_to_send["file_path"]
        try:
            logger.info(f"Attempting to send audio file: {audio_path}")
            with open(audio_path, "rb") as audio:
                await bot_instance.send_voice(
                    chat_id, audio, caption="Here's the audio:"
                )
            logger.info(f"Successfully sent voice message for {chat_id}")
            try:
                os.remove(audio_path)
                logger.info(f"Deleted temp audio: {audio_path}")
            except Exception as e_del_audio:
                logger.error(f"Error deleting temp audio {audio_path}: {e_del_audio}")
        except FileNotFoundError:
            logger.error(f"Audio file not found: {audio_path}")
            await bot_instance.reply_to(
                message, "Sorry, I generated audio but couldn't find the file."
            )
        except Exception as e_send_audio:
            logger.error(
                f"Failed to send audio for {chat_id}: {e_send_audio}",
                exc_info=True,
            )
            await bot_instance.reply_to(
                message, "Sorry, I had trouble sending the audio."
            )

    logger.debug(f"Finalizing: image_file_to_send = {image_file_to_send}")
    if image_file_to_send and image_file_to_send.get("file_path"):
        image_path = image_file_to_send["file_path"]
        try:
            logger.info(f"Attempting to send image file: {image_path}")
            with open(image_path, "rb") as image_f:
                await bot_instance.send_photo(
                    chat_id, image_f, caption="Here's the image:"
                )
            logger.info(f"Successfully sent image for {chat_id}")
            try:
                os.remove(image_path)
                logger.info(f"Deleted temp image: {image_path}")
            except Exception as e_del_image:
                logger.error(f"Error deleting temp image {image_path}: {e_del_image}")
        except FileNotFoundError:
            logger.error(f"Image file not found: {image_path}")
            await bot_instance.reply_to(
                message, "Sorry, I generated an image but couldn't find the file."
            )
        except Exception as e_send_image:
            logger.error(
                f"Failed to send image for {chat_id}: {e_send_image}",
                exc_info=True,
            )
            await bot_instance.reply_to(
                message, "Sorry, I had trouble sending the image."
            )


async def _handle_ai_interaction(
    message: telebot_types.Message,
    user_settings: UserSettings,
    user_input_parts: list[genai_types.Part],
    bot_instance: AsyncTeleBot,
) -> None:
    """
    Handles the core AI chat interaction using ADK Agent:
    fetching history (via agent's tool), creating/getting agent,
    sending message via agent, saving new turns, getting response, and sending reply.
    """
    context = await _setup_ai_interaction_context(
        message, user_settings, user_input_parts, bot_instance
    )
    if context is None:
        return

    chat_id = context["chat_id"]
    user_id_for_agent = context["user_id_for_agent"]
    session_id_for_agent = context["session_id_for_agent"]
    model_for_agent = context["model_for_agent"]
    urls_found = context["urls_found"]
    active_genai_client = context["active_genai_client"]
    runner = context["runner"]

    response_text_from_agent = "Agent did not provide a response."
    agent_response_content_for_db: genai_types.Content | None = None
    audio_file_to_send: dict | None = None
    image_file_to_send: dict | None = None
    processing_completed_successfully = False
    waiting_animation: telebot_types.Message | None = None
    caption_updated_for_tool: bool = False

    try:
        waiting_animation = await bot_instance.send_animation(
            chat_id,
            animation=LOADING_ANIMATION_FILE_ID,
            caption="Thinking... please wait. ✨",
        )

        adk_content_for_user_turn = genai_types.Content(
            role="user", parts=user_input_parts
        )

        if urls_found:
            handled_by_url_tool, caption_updated_for_tool = (
                await _process_urls_directly(
                    message=message,
                    bot_instance=bot_instance,
                    active_genai_client=active_genai_client,
                    model_for_agent=model_for_agent,
                    user_turn_content=adk_content_for_user_turn,
                    waiting_animation=waiting_animation,
                    caption_updated_for_tool=caption_updated_for_tool,
                    chat_id=chat_id,
                    urls_found=urls_found,
                )
            )
            if handled_by_url_tool:
                return

        logger.debug(
            f"Calling agent runner for {chat_id} | UserID: {user_id_for_agent} | SessionID: {session_id_for_agent}"
        )

        (
            response_text_from_agent,
            agent_response_content_for_db,
            audio_file_to_send,
            image_file_to_send,
            processing_completed_successfully,
            caption_updated_for_tool,
        ) = await _process_adk_events_and_get_response(
            runner=runner,
            user_id_for_agent=user_id_for_agent,
            session_id_for_agent=session_id_for_agent,
            adk_content_for_user_turn=adk_content_for_user_turn,
            bot_instance=bot_instance,
            waiting_animation=waiting_animation,
            initial_caption_updated_for_tool=caption_updated_for_tool,
            chat_id=chat_id,
        )
        await _finalize_interaction_and_send_response(
            message=message,
            bot_instance=bot_instance,
            chat_id=chat_id,
            response_text_from_agent=response_text_from_agent,
            agent_response_content_for_db=agent_response_content_for_db,
            audio_file_to_send=audio_file_to_send,
            image_file_to_send=image_file_to_send,
        )

    except (
        genai_errors.ClientError,
        genai_errors.ServerError,
        genai_errors.APIError,
        PermissionDenied,  # google.api_core.exceptions
        ResourceExhausted,  # google.api_core.exceptions
        NotFound,  # google.api_core.exceptions
        ClientError,  # aliased google.api_core.exceptions.ClientError
        GoogleAPIError,  # aliased google.api_core.exceptions.GoogleAPIError
    ) as e:
        await _handle_ai_interaction_error(
            e, message, bot_instance, chat_id, model_for_agent
        )
    except Exception as e:  # Catch-all for any other unexpected errors
        # Use the same handler, it has a default message for unknown exceptions
        await _handle_ai_interaction_error(
            e, message, bot_instance, chat_id, model_for_agent
        )

    if waiting_animation:
        try:
            await bot_instance.delete_message(chat_id, waiting_animation.message_id)
        except Exception as del_e:
            logger.warning(
                f"Failed to delete waiting animation for chat {chat_id}: {del_e}"
            )

    if processing_completed_successfully:
        logger.info(f"AI interaction for chat {chat_id} completed successfully.")


async def process_user_message(
    message: telebot_types.Message,
    content_processor: Callable[
        [telebot_types.Message, AsyncTeleBot],
        Coroutine[Any, Any, list[genai_types.Part] | None],
    ],
    bot_instance: AsyncTeleBot,
) -> None:
    chat_id = message.chat.id
    logger.info(
        f"Processing user message for chat_id: {chat_id}, content_type: {message.content_type}"
    )

    user_settings = await check_db_and_settings(chat_id, message, bot_instance)
    if user_settings is None:  # check_db_and_settings handles sending a message
        return

    if not await check_message_limit_and_increment(
        chat_id, message, user_settings, bot_instance
    ):
        return  # check_message_limit_and_increment handles sending a message

    # Process the message content (text, photo, etc.) into Gemini Parts
    user_input_parts = await content_processor(message, bot_instance)
    if (
        user_input_parts is None
    ):  # content_processor should handle replies for invalid content
        logger.warning(
            f"Content processor returned None for chat {chat_id}, type {message.content_type}. No AI interaction will occur."
        )
        return

    # Save the user's valid turn to the database BEFORE calling the AI
    # This ensures the user's message is recorded even if AI interaction fails later.
    current_history_before_ai = await get_history_from_db(chat_id)
    user_turn_index = len(current_history_before_ai) if current_history_before_ai else 0

    # Assuming user_input_parts is always for a 'user' role here
    if not await save_turn_to_db(chat_id, user_turn_index, "user", user_input_parts):
        logger.error(
            f"Failed to save user turn to DB for {chat_id} at index {user_turn_index} BEFORE AI interaction."
        )

    try:
        await _handle_ai_interaction(
            message, user_settings, user_input_parts, bot_instance
        )
    except Exception as e_interaction_wrapper:
        logger.error(
            f"Critical error caught in `process_user_message` wrapping `_handle_ai_interaction` for {chat_id}: {e_interaction_wrapper}",
            exc_info=True,
        )
        try:
            await bot_instance.reply_to(
                message,
                "A critical error occurred while I was trying to think. I've noted it down. Please try your request again.",
            )
        except Exception as e_reply_critical:
            logger.error(
                f"Failed to send critical error reply to {chat_id}: {e_reply_critical}"
            )


async def process_text_message(
    message: telebot_types.Message, bot_instance: AsyncTeleBot
) -> list[genai_types.Part] | None:
    chat_id = message.chat.id
    user_text = message.text

    if not user_text or user_text.isspace():
        logger.info(f"Empty text message from {chat_id}. Ignoring.")
        try:
            await bot_instance.reply_to(
                message, "Please send some text for me to process!"
            )
        except Exception as e:
            logger.error(f"Failed to send 'empty text' reply to {chat_id}: {e}")
        return None  # Indicate no valid parts to process

    logger.info(f"Text message from {chat_id}: '{user_text[:50]}...'")
    return [genai_types.Part(text=user_text)]


async def process_photo_message(
    message: telebot_types.Message, bot_instance: AsyncTeleBot
) -> list[genai_types.Part] | None:
    chat_id = message.chat.id
    user_caption = message.caption if message.caption else ""
    logger.info(
        f"Photo message from {chat_id}. Caption: '{user_caption[:50]}...'"
        if user_caption
        else f"Photo message from {chat_id} (no caption)."
    )

    if not message.photo:  # Should not happen if content_type is 'photo' but good check
        logger.warning(
            f"Photo message from {chat_id} has no 'photo' attribute despite content_type. Skipping."
        )
        try:
            await bot_instance.reply_to(
                message,
                "There seems to be an issue with the photo you sent. Please try again.",
            )
        except Exception as e:
            logger.error(f"Failed to send 'no photo attribute' reply to {chat_id}: {e}")
        return None

    try:
        # Get the largest available photo
        photo_to_download = message.photo[-1]
        file_info = await bot_instance.get_file(photo_to_download.file_id)

        if not file_info.file_path:
            logger.error(
                f"Could not get file_path for photo from {chat_id} (file_id: {photo_to_download.file_id})"
            )
            await bot_instance.reply_to(
                message,
                "Sorry, I couldn't get the details to download the photo. Please try sending it again.",
            )
            return None

        downloaded_file_bytes = await bot_instance.download_file(file_info.file_path)

        # Determine MIME type from file extension if possible, default to jpeg
        mime_type = "image/jpeg"  # Default
        file_extension_match = re.search(r"\.(\w+)$", file_info.file_path)
        if file_extension_match:
            ext = file_extension_match.group(1).lower()
            if ext == "png":
                mime_type = "image/png"
            elif ext in ["jpg", "jpeg"]:
                mime_type = "image/jpeg"
            elif ext == "webp":
                mime_type = "image/webp"
            elif ext == "heic":
                mime_type = "image/heic"
            elif ext == "heif":
                mime_type = "image/heif"
            else:
                logger.warning(
                    f"Unknown photo extension '{ext}' for chat {chat_id}, defaulting to {mime_type}."
                )

        logger.info(
            f"Downloaded photo for {chat_id}, size: {len(downloaded_file_bytes)} bytes, Determined MIME type: {mime_type}"
        )

        image_part = genai_types.Part(
            inline_data=genai_types.Blob(
                mime_type=mime_type, data=downloaded_file_bytes
            )
        )

        parts_for_gemini: list[genai_types.Part] = []
        if user_caption:  # Add caption as a text part if it exists
            parts_for_gemini.append(genai_types.Part(text=user_caption))
        parts_for_gemini.append(image_part)  # Add the image part

        return parts_for_gemini

    except Exception as e:
        logger.error(f"Error processing photo for {chat_id}: {e}", exc_info=True)
        try:
            await bot_instance.reply_to(
                message,
                "Sorry, I encountered an error while trying to process the photo. Please try sending it again.",
            )
        except Exception as e_reply:  # If replying also fails
            logger.error(
                f"Failed to send error reply for photo processing to {chat_id}: {e_reply}"
            )
        return None
