import logging
import re
from typing import Any

import telegramify_markdown
from google import genai
from telebot import types as telebot_types
from telebot.async_telebot import AsyncTeleBot
from telegramify_markdown.config import get_runtime_config
from telegramify_markdown.content import ContentType, File, Photo, Text
from telegramify_markdown.entity import MessageEntity as TgMdEntity
from telegramify_markdown.entity import utf16_len

from .config import DEFAULT_KEY_MESSAGE_LIMIT, DEFAULT_MODEL_NAME, GOOGLE_API_KEY
from .custom_types import UserSettings
from .db import get_supabase_client, get_user_settings_from_db, save_user_settings_to_db
from .gemini_utils import get_user_client

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

get_runtime_config().markdown_symbol.heading_level_1 = "📌"
get_runtime_config().markdown_symbol.link = "🔗"

_DRAFT_TEXT_LIMIT = 4096


def to_telebot_entities(
    entities: list[TgMdEntity] | None,
) -> list[telebot_types.MessageEntity] | None:
    """Converts telegramify-markdown MessageEntity objects into pyTelegramBotAPI MessageEntity objects."""
    if not entities:
        return None
    return [telebot_types.MessageEntity(**e.to_dict()) for e in entities]


def _tail_by_utf16(text: str, limit: int) -> str:
    """Return the tail of text within limit UTF-16 code units without splitting surrogate pairs."""
    total = 0
    for i in range(len(text) - 1, -1, -1):
        total += 2 if ord(text[i]) > 0xFFFF else 1
        if total > limit:
            return text[i + 1 :]
    return text


async def stream_draft_update(
    bot_instance: AsyncTeleBot,
    chat_id: int,
    draft_id: int,
    markdown_text: str,
    message_thread_id: int | None = None,
) -> bool:
    """
    Streams an ephemeral draft message update to Telegram using sendMessageDraft.
    Pass empty markdown_text="" to display Telegram's native 'Thinking…' placeholder.
    Returns True if successful, False if draft streaming is unsupported or failed.
    """
    try:
        if not markdown_text:
            await bot_instance.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text="",
                message_thread_id=message_thread_id,
            )
            return True

        plain_text, md_entities = telegramify_markdown.convert(
            markdown_text, latex_escape=True
        )
        if not plain_text:
            plain_text = markdown_text
            md_entities = []

        if utf16_len(plain_text) > _DRAFT_TEXT_LIMIT:
            plain_text = _tail_by_utf16(plain_text, _DRAFT_TEXT_LIMIT)
            telebot_entities = None
        else:
            telebot_entities = to_telebot_entities(md_entities)

        await bot_instance.send_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            text=plain_text,
            entities=telebot_entities,
            message_thread_id=message_thread_id,
        )
        return True
    except Exception as e:
        logger.debug(
            f"send_message_draft failed or unsupported for chat {chat_id}: {e}"
        )
        return False


async def _try_fix_and_resend_mermaid(
    original_mermaid_code: str,
    bot_instance: AsyncTeleBot,
    original_message: telebot_types.Message,
) -> bool:
    """
    Attempts to fix common Mermaid syntax issues and re-render.
    Returns True if successfully fixed and sent, False otherwise.
    """
    logger.info(f"Attempting to fix Mermaid syntax for chat {original_message.chat.id}")
    fixed_mermaid_code = original_mermaid_code

    def quote_label(match: re.Match[str]) -> str:
        node_id = match.group(1)
        label_content = match.group(2)
        if label_content.startswith('"') and label_content.endswith('"'):
            return f"{node_id}[{label_content}]"  # Already quoted
        escaped_label_content = label_content.replace('"', '\\"')
        return f'{node_id}["{escaped_label_content}"]'

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
        return False

    if fixed_mermaid_code == original_mermaid_code:
        logger.info(
            f"Mermaid fix did not change the code for chat {original_message.chat.id}. No re-render attempt with this fix."
        )
        return False

    try:
        logger.info(
            f"Re-rendering fixed Mermaid code for chat {original_message.chat.id}..."
        )
        fixed_boxes = await telegramify_markdown.telegramify(
            content=f"```mermaid\n{fixed_mermaid_code}\n```",
            latex_escape=True,
            render_mermaid=True,
            max_message_length=4090,
        )

        for item in fixed_boxes:
            if isinstance(item, Photo):
                logger.info(
                    f"Successfully re-rendered fixed Mermaid as PHOTO for chat {original_message.chat.id}. Sending."
                )
                await bot_instance.send_photo(
                    original_message.chat.id,
                    (item.file_name, item.file_data),
                    caption="Rendered diagram (auto-fixed attempt):",
                )
                return True
            elif isinstance(item, File) and item.file_name == "invalid_mermaid.txt":
                logger.warning(
                    f"Re-rendering fixed Mermaid still resulted in invalid_mermaid.txt for chat {original_message.chat.id}."
                )
                return False

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
    try:
        boxes = await telegramify_markdown.telegramify(
            content=text,
            latex_escape=True,
            render_mermaid=True,
            max_message_length=4090,
        )

        for item in boxes:
            try:
                if isinstance(item, Text) or item.content_type == ContentType.TEXT:
                    assert isinstance(item, Text)
                    await bot_instance.reply_to(
                        message,
                        item.text,
                        entities=to_telebot_entities(item.entities),
                    )
                elif isinstance(item, Photo) or item.content_type == ContentType.PHOTO:
                    assert isinstance(item, Photo)
                    file_name_to_send = item.file_name
                    logger.debug(
                        f"Attempting to send PHOTO with filename: {file_name_to_send}"
                    )
                    await bot_instance.send_photo(
                        message.chat.id,
                        (file_name_to_send, item.file_data),
                        caption=item.caption_text or None,
                        caption_entities=to_telebot_entities(item.caption_entities),
                    )
                elif isinstance(item, File) or item.content_type == ContentType.FILE:
                    assert isinstance(item, File)
                    file_name_to_send = item.file_name
                    logger.debug(
                        f"Attempting to send FILE with filename: {file_name_to_send}"
                    )
                    if file_name_to_send == "invalid_mermaid.txt" and item.file_data:
                        logger.warning(
                            f"Mermaid diagram rendering failed for chat {message.chat.id}. Original code in item.file_data."
                        )
                        original_mermaid_code_bytes = item.file_data
                        try:
                            original_mermaid_code_str = (
                                original_mermaid_code_bytes.decode("utf-8")
                            )
                            fix_successful = await _try_fix_and_resend_mermaid(
                                original_mermaid_code_str,
                                bot_instance,
                                message,
                            )
                            if fix_successful:
                                logger.info(
                                    f"Successfully fixed and resent Mermaid diagram for chat {message.chat.id}."
                                )
                                continue
                        except UnicodeDecodeError as ude:
                            logger.error(
                                f"Could not decode original mermaid code for fix attempt: {ude}"
                            )

                        await bot_instance.reply_to(
                            message,
                            "⚠️ I tried to generate a Mermaid diagram, but it couldn't be rendered automatically (even after an auto-fix attempt). Here's the raw code:",
                        )
                        try:
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
                                ),
                                caption="Raw Mermaid code (failed to send as text).",
                            )
                    else:
                        await bot_instance.send_document(
                            message.chat.id,
                            (file_name_to_send, item.file_data),
                            caption=item.caption_text or None,
                            caption_entities=to_telebot_entities(item.caption_entities),
                        )
            except Exception as send_error:
                logger.error(f"Error sending item {item.content_type}: {send_error}")
                await bot_instance.send_message(
                    message.chat.id,
                    f"⚠️ Error processing part of the message: {send_error}",
                )

    except Exception as telegramify_error:
        logger.error(f"Error during telegramify processing: {telegramify_error}")
        try:
            await bot_instance.reply_to(message, text[:4090])
        except Exception as fallback_err:
            logger.error(f"Fallback reply_to also failed: {fallback_err}")


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
                            warning_message = "You have 1 message remaining with the default API key.\n\nPlease use `/set_api_key` to provide your own Gemini API key to send more messages after this one."
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
