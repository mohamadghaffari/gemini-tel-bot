import telebot
import asyncio
import logging
from google import genai
import telegramify_markdown
from custom_types import UserSettings
from gemini_utils import get_user_client
from telebot import types as telebot_types
from telegramify_markdown.type import ContentTypes
from telegramify_markdown.customize import get_runtime_config
from config import (
    DEFAULT_KEY_MESSAGE_LIMIT,
    DEFAULT_MODEL_NAME,
    GEMINI_BOT_DEFAULT_API_KEY,
)
from db import get_supabase_client, get_user_settings_from_db, save_user_settings_to_db
from telegramify_markdown.interpreters import (
    TextInterpreter,
    FileInterpreter,
    MermaidInterpreter,
    InterpreterChain,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

get_runtime_config().markdown_symbol.head_level_1 = "📌"
get_runtime_config().markdown_symbol.link = "🔗"


def split_and_send_message(
    message: telebot_types.Message, text: str, bot_instance: telebot.TeleBot, **kwargs
):

    interpreter_chain = InterpreterChain(
        [
            TextInterpreter(),
            FileInterpreter(),
            MermaidInterpreter(session=None),
        ]
    )

    try:
        boxs = asyncio.run(
            telegramify_markdown.telegramify(
                content=text,
                interpreters_use=interpreter_chain,
                latex_escape=True,
                normalize_whitespace=True,
                max_word_count=4090,
            )
        )

        # Now process the results synchronously
        for item in boxs:
            try:
                # We can add delay here if needed using sleep
                if item.content_type == ContentTypes.TEXT:
                    bot_instance.reply_to(
                        message, item.content, parse_mode="MarkdownV2"
                    )
                elif item.content_type == ContentTypes.PHOTO:
                    file_name_to_send = item.file_name
                    print(
                        f"Attempting to send PHOTO with filename: {file_name_to_send}"
                    )
                    bot_instance.send_photo(
                        message.chat.id,
                        (file_name_to_send, item.file_data),
                        caption=item.caption,
                        parse_mode="MarkdownV2",
                    )
                elif item.content_type == ContentTypes.FILE:
                    file_name_to_send = item.file_name
                    print(f"Attempting to send FILE with filename: {file_name_to_send}")
                    bot_instance.send_document(
                        message.chat.id,
                        (file_name_to_send, item.file_data),
                        caption=item.caption,
                        parse_mode="MarkdownV2",
                    )
            except Exception as send_error:
                print(f"Error sending item {item.content_type}: {send_error}")
                bot_instance._bot_instance.send_message(
                    message.chat.id,
                    f"⚠️ Error processing part of the message: {send_error}",
                )

    except Exception as telegramify_error:
        print(f"Error during telegramify processing: {telegramify_error}")


def check_db_and_settings(
    chat_id: int, message: telebot_types.Message, bot_instance: telebot.TeleBot
) -> UserSettings | None:
    """Helper to check DB availability and fetch settings, sends error replies if needed."""
    if not get_supabase_client():
        bot_instance.reply_to(
            message,
            "Database service is not available. Bot may not function correctly.",
        )
        logger.error(f"DB unavailable for {chat_id}.")
        return None

    user_settings = get_user_settings_from_db(chat_id)
    if user_settings is None:
        bot_instance.reply_to(
            message, "Error fetching your settings from the database."
        )
        logger.error(f"Failed to fetch settings for {chat_id}.")
        return None

    return user_settings


def check_ai_client(
    chat_id: int,
    message: telebot_types.Message,
    user_settings: UserSettings,
    bot_instance: telebot.TeleBot,
) -> genai.Client | None:
    """Helper to get AI client based on settings, sends error reply if needed."""
    api_key_to_use = user_settings.get("gemini_api_key") or GEMINI_BOT_DEFAULT_API_KEY

    if not api_key_to_use:
        error_msg = "AI service not available. The bot's default API key is missing, and you haven't set your own.\n\nPlease use `/set_api_key` to provide your key."
        bot_instance.reply_to(message, error_msg, parse_mode="Markdown")
        logger.error(f"No API key available for {chat_id}.")
        return None

    client_for_user = get_user_client(api_key_to_use)
    if client_for_user is None:
        error_msg = f"Failed to initialize AI client with the provided API key (starts with {api_key_to_use[:4]}). Please check your key using `/current_settings` or try setting it again with `/set_api_key`."
        bot_instance.reply_to(message, error_msg, parse_mode="Markdown")
        return None

    return client_for_user


def check_message_limit_and_increment(
    chat_id: int,
    message: telebot_types.Message,
    user_settings: UserSettings,
    bot_instance: telebot.TeleBot,
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
            bot_instance.reply_to(message, limit_message, parse_mode="Markdown")
            logger.info(f"User {chat_id} hit default key message limit.")
            return False

        else:
            latest_settings = get_user_settings_from_db(chat_id)
            if latest_settings:
                count_to_save = latest_settings.get("message_count", 0) + 1
                logger.info(
                    f"Attempting to increment message count for {chat_id} to {count_to_save}."
                )

                if save_user_settings_to_db(
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
                                bot_instance.send_message(
                                    message, warning_message, parse_mode="Markdown"
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
                                bot_instance.send_message(
                                    message,
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
                    bot_instance.reply_to(
                        message, "Error saving message count. Please try again."
                    )
                    return False
            else:
                logger.error(
                    f"Failed to refetch settings to update message count for {chat_id}."
                )
                bot_instance.reply_to(message, "Error updating message count.")
                return False

    return True
