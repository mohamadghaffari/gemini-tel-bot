import logging
import re
from typing import Any

import telegramify_markdown
from google import genai
from telebot import types as telebot_types
from telebot.async_telebot import AsyncTeleBot
from telegramify_markdown.customize import get_runtime_config
from telegramify_markdown.interpreters import (
    FileInterpreter,
    InterpreterChain,
    MermaidInterpreter,
    TextInterpreter,
)
from telegramify_markdown.type import ContentTypes

from .config import DEFAULT_KEY_MESSAGE_LIMIT, DEFAULT_MODEL_NAME, GOOGLE_API_KEY
from .custom_types import UserSettings
from .db import get_supabase_client, get_user_settings_from_db, save_user_settings_to_db
from .gemini_utils import get_user_client

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

get_runtime_config().markdown_symbol.head_level_1 = "📌"
get_runtime_config().markdown_symbol.link = "🔗"


async def _try_fix_and_resend_mermaid(
    original_mermaid_code: str,
    bot_instance: AsyncTeleBot,
    original_message: telebot_types.Message,
    interpreter_chain: Any,
) -> bool:
    """
    Attempts to fix common Mermaid syntax issues and re-render.
    Returns True if successfully fixed and sent, False otherwise.
    """
    logger.info(f"Attempting to fix Mermaid syntax for chat {original_message.chat.id}")
    fixed_mermaid_code = original_mermaid_code

    # Simple fix 1: Ensure node labels are quoted if they aren't already and contain potentially problematic characters.
    # This regex finds id[label] where label is not already quoted.
    # It's a basic attempt and might not cover all cases or could be too greedy.
    # Pattern: (\w+)\[([^"\]][^\]]*)\]  -- matches simple_id[unquoted label]
    # Simpler: find any id[text] and ensure text is quoted.
    # To avoid re-quoting: id\["text"\] vs id\[text\]
    # Let's try to quote any label that isn't already starting with a quote.
    # Example: A[Label with spaces] -> A["Label with spaces"]
    # Example: B[Label-with-hyphen] -> B["Label-with-hyphen"]
    # Example: C["Already Quoted"] -> remains C["Already Quoted"]
    def quote_label(match: re.Match) -> str:
        node_id = match.group(1)
        label_content = match.group(2)
        if label_content.startswith('"') and label_content.endswith('"'):
            return f"{node_id}[{label_content}]"  # Already quoted
        # Escape existing double quotes within the label before adding new ones
        escaped_label_content = label_content.replace('"', '\\"')
        return f'{node_id}["{escaped_label_content}"]'

    # This regex looks for patterns like `node_id[anything_not_a_closing_bracket]`
    try:
        fixed_mermaid_code = re.sub(
            r"(\w+)\s*\[\s*([^\]\n]+?)\s*\]", quote_label, fixed_mermaid_code
        )
        logger.info(
            f"Mermaid code after attempting to quote labels for chat {original_message.chat.id}:\n{fixed_mermaid_code}"
        )
    except Exception as e_fix_re:
        logger.error(
            f"Error during Mermaid regex fix for chat {original_message.chat.id}: {e_fix_re}"
        )
        return False  # Don't proceed if regex itself fails

    if fixed_mermaid_code == original_mermaid_code:
        logger.info(
            f"Mermaid fix did not change the code for chat {original_message.chat.id}. No re-render attempt with this fix."
        )
        # Potentially add other fixes here in the future
        # For now, if this one fix doesn't change anything, we assume it won't help.
        return False

    try:
        logger.info(
            f"Re-rendering fixed Mermaid code for chat {original_message.chat.id}..."
        )
        fixed_boxes = await telegramify_markdown.telegramify(
            content=f"```mermaid\n{fixed_mermaid_code}\n```",  # Re-wrap in mermaid block
            interpreters_use=interpreter_chain,  # Use the same chain
            latex_escape=True,
            normalize_whitespace=True,
            max_word_count=4090,  # Should be fine for a single diagram
        )

        sent_fixed_version = False
        for item in fixed_boxes:
            if (
                item.content_type == ContentTypes.PHOTO
            ):  # Successfully rendered as an image
                logger.info(
                    f"Successfully re-rendered fixed Mermaid as PHOTO for chat {original_message.chat.id}. Sending."
                )
                await bot_instance.send_photo(
                    original_message.chat.id,
                    (item.file_name, item.file_data),
                    caption="Rendered diagram (auto-fixed attempt):",
                    parse_mode="MarkdownV2",
                )
                sent_fixed_version = True
                # If we successfully send a photo, we assume the fix worked for this diagram.
                return True  # Stop processing this failed diagram further
            elif (
                item.content_type == ContentTypes.FILE
                and item.file_name == "invalid_mermaid.txt"
            ):
                logger.warning(
                    f"Re-rendering fixed Mermaid still resulted in invalid_mermaid.txt for chat {original_message.chat.id}."
                )
                return False  # Fix didn't work
            # Handle other types if necessary, though PHOTO is the expected success for mermaid

        if sent_fixed_version:
            return True
        else:
            logger.info(
                f"Re-rendering fixed Mermaid did not produce a sendable PHOTO for chat {original_message.chat.id}."
            )
            return False

    except Exception as e_rerender:
        logger.error(
            f"Exception during re-rendering of fixed Mermaid for chat {original_message.chat.id}: {e_rerender}"
        )
        return False


async def split_and_send_message(
    message: telebot_types.Message,
    text: str,
    bot_instance: AsyncTeleBot,
    **kwargs: Any,
) -> None:

    interpreter_chain = InterpreterChain(
        [
            TextInterpreter(),
            FileInterpreter(),
            MermaidInterpreter(session=None),
        ]
    )

    try:
        boxs = await telegramify_markdown.telegramify(
            content=text,
            interpreters_use=interpreter_chain,
            latex_escape=True,
            normalize_whitespace=True,
            max_word_count=4090,
        )

        # Now process the results synchronously
        for item in boxs:
            try:
                # We can add delay here if needed using sleep
                if item.content_type == ContentTypes.TEXT:
                    await bot_instance.reply_to(
                        message, item.content, parse_mode="MarkdownV2"
                    )
                elif item.content_type == ContentTypes.PHOTO:
                    file_name_to_send = item.file_name
                    print(
                        f"Attempting to send PHOTO with filename: {file_name_to_send}"
                    )
                    await bot_instance.send_photo(
                        message.chat.id,
                        (file_name_to_send, item.file_data),
                        caption=item.caption,
                        parse_mode="MarkdownV2",
                    )
                elif item.content_type == ContentTypes.FILE:
                    file_name_to_send = item.file_name
                    print(f"Attempting to send FILE with filename: {file_name_to_send}")
                    if file_name_to_send == "invalid_mermaid.txt" and item.file_data:
                        logger.warning(
                            f"Mermaid diagram rendering failed for chat {message.chat.id}. Original code in item.file_data."
                        )
                        original_mermaid_code_bytes = item.file_data
                        try:
                            original_mermaid_code_str = (
                                original_mermaid_code_bytes.decode("utf-8")
                            )
                            # Attempt to fix and resend
                            fix_successful = await _try_fix_and_resend_mermaid(
                                original_mermaid_code_str,
                                bot_instance,
                                message,
                                interpreter_chain,
                            )
                            if fix_successful:
                                logger.info(
                                    f"Successfully fixed and resent Mermaid diagram for chat {message.chat.id}."
                                )
                                continue  # Skip sending the raw original code
                        except UnicodeDecodeError as ude:
                            logger.error(
                                f"Could not decode original mermaid code for fix attempt: {ude}"
                            )
                            # Fall through to sending original file if decode fails

                        # If fix was not successful or not attempted due to decode error, send original raw code
                        await bot_instance.reply_to(
                            message,
                            "⚠️ I tried to generate a Mermaid diagram, but it couldn't be rendered automatically (even after an auto-fix attempt). Here's the raw code:",
                            parse_mode="MarkdownV2",
                        )
                        try:
                            # Re-decode for sending, or use already decoded if available
                            raw_mermaid_to_send_str = (
                                original_mermaid_code_bytes.decode("utf-8")
                            )
                            escaped_mermaid_code = raw_mermaid_to_send_str.replace(
                                "`", "\\`"
                            )
                            await bot_instance.reply_to(
                                message,
                                f"```mermaid\n{escaped_mermaid_code}\n```",
                                parse_mode="MarkdownV2",
                            )
                        except Exception as e_decode_send_raw:
                            logger.error(
                                f"Failed to decode or send raw original mermaid code: {e_decode_send_raw}"
                            )
                            await bot_instance.send_document(
                                message.chat.id,
                                (
                                    file_name_to_send,
                                    original_mermaid_code_bytes,
                                ),  # Send original bytes
                                caption="Raw Mermaid code (failed to send as text).",
                                parse_mode="MarkdownV2",
                            )
                    else:  # Other files
                        await bot_instance.send_document(
                            message.chat.id,
                            (file_name_to_send, item.file_data),
                            caption=item.caption,
                            parse_mode="MarkdownV2",
                        )
            except Exception as send_error:
                print(f"Error sending item {item.content_type}: {send_error}")
                await bot_instance.send_message(
                    message.chat.id,
                    f"⚠️ Error processing part of the message: {send_error}",
                )

    except Exception as telegramify_error:
        print(f"Error during telegramify processing: {telegramify_error}")


async def check_db_and_settings(
    chat_id: int, message: telebot_types.Message, bot_instance: AsyncTeleBot
) -> UserSettings | None:
    """Helper to check DB availability and fetch settings, sends error replies if needed."""
    if not await get_supabase_client():
        await bot_instance.reply_to(
            message,
            "Database service is not available. Bot may not function correctly.",
        )
        logger.error(f"DB unavailable for {chat_id}.")
        return None

    user_settings = await get_user_settings_from_db(chat_id)
    if user_settings is None:
        await bot_instance.reply_to(
            message, "Error fetching your settings from the database."
        )
        logger.error(f"Failed to fetch settings for {chat_id}.")
        return None

    return user_settings


async def check_ai_client(
    chat_id: int,
    message: telebot_types.Message,
    user_settings: UserSettings,
    bot_instance: AsyncTeleBot,
) -> genai.Client | None:
    """Helper to get AI client based on settings, sends error reply if needed."""
    api_key_to_use = user_settings.get("gemini_api_key") or GOOGLE_API_KEY

    if not api_key_to_use:
        error_msg = "AI service not available. The bot's default API key (GOOGLE_API_KEY) is missing, and you haven't set your own.\n\nPlease use `/set_api_key` to provide your key."
        await bot_instance.reply_to(message, error_msg, parse_mode="Markdown")
        logger.error(f"No API key available for {chat_id}.")
        return None

    client_for_user = get_user_client(api_key_to_use)
    if client_for_user is None:
        error_msg = f"Failed to initialize AI client with the provided API key (starts with {api_key_to_use[:4]}). Please check your key using `/current_settings` or try setting it again with `/set_api_key`."
        await bot_instance.reply_to(message, error_msg, parse_mode="Markdown")
        return None

    return client_for_user


async def check_message_limit_and_increment(
    chat_id: int,
    message: telebot_types.Message,
    user_settings: UserSettings,
    bot_instance: AsyncTeleBot,
) -> bool:
    """
    Helper to check message limit for default key users and increment count.
    Returns True if allowed to proceed, False otherwise (sends message).
    """
    if user_settings.get("gemini_api_key") is None:
        current_count = user_settings.get("message_count", 0)

        if DEFAULT_KEY_MESSAGE_LIMIT <= 0:
            return True

        if current_count >= DEFAULT_KEY_MESSAGE_LIMIT:
            limit_message = f"You have reached the {DEFAULT_KEY_MESSAGE_LIMIT}-message limit for users without a custom API key.\n\nPlease set your own API key using `/set_api_key` to continue chatting without limits."
            await bot_instance.reply_to(message, limit_message, parse_mode="Markdown")
            logger.info(f"User {chat_id} hit default key message limit.")
            return False

        else:
            latest_settings = await get_user_settings_from_db(chat_id)
            if latest_settings:
                count_to_save = latest_settings.get("message_count", 0) + 1
                logger.info(
                    f"Attempting to increment message count for {chat_id} to {count_to_save}."
                )

                if await save_user_settings_to_db(
                    chat_id,
                    api_key=latest_settings.get("gemini_api_key"),
                    model_name=latest_settings.get(
                        "selected_model", DEFAULT_MODEL_NAME
                    ),
                    message_count=count_to_save,
                ):
                    logger.info(f"Message count incremented and saved for {chat_id}.")

                    messages_remaining = DEFAULT_KEY_MESSAGE_LIMIT - count_to_save
                    if DEFAULT_KEY_MESSAGE_LIMIT > 0:
                        if messages_remaining == 1:
                            warning_message = f"You have 1 message remaining with the default API key.\n\nPlease use `/set_api_key` to provide your own Gemini API key to send more messages after this one."  # Slightly rephrased for clarity
                            try:
                                await bot_instance.send_message(
                                    chat_id, warning_message, parse_mode="Markdown"
                                )
                                logger.info(
                                    f"Sent limit warning: 1 message remaining for {chat_id}."
                                )
                            except Exception as send_warn_e:
                                logger.error(
                                    f"Failed to send limit warning message to {chat_id}: {send_warn_e}"
                                )
                        elif messages_remaining == 0 and DEFAULT_KEY_MESSAGE_LIMIT > 0:
                            final_warning_message = f"This is your {DEFAULT_KEY_MESSAGE_LIMIT}th and final message using the default API key.\n\nTo send more messages, please use `/set_api_key` to provide your own Gemini API key."
                            try:
                                await bot_instance.send_message(
                                    chat_id,
                                    final_warning_message,
                                    parse_mode="Markdown",
                                )
                                logger.info(
                                    f"Sent final limit warning message to {chat_id}."
                                )
                            except Exception as send_warn_e:
                                logger.error(
                                    f"Failed to send final limit warning message to {chat_id}: {send_warn_e}"
                                )

                    return True

                else:
                    logger.error(f"Failed to save updated message count for {chat_id}.")
                    await bot_instance.reply_to(
                        message, "Error saving message count. Please try again."
                    )
                    return False
            else:
                logger.error(
                    f"Failed to refetch settings to update message count for {chat_id}."
                )
                await bot_instance.reply_to(message, "Error updating message count.")
                return False

    return True


def sanitize_filename(text: str, max_length: int = 40) -> str:
    """Cleans and truncates text to be used as a part of a filename."""
    if not text:
        return "speech_audio"
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"[-\s]+", "_", text)
    if len(text) > max_length:
        text = text[:max_length]
        if "_" in text:
            last_underscore_pos = text.rfind("_")
            if last_underscore_pos > 0:
                text = text[:last_underscore_pos]
    if not text:
        return "speech_audio"
    return text
