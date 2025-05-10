import logging
import telebot
from telebot import types as telebot_types
from .config import (
    DEFAULT_KEY_MESSAGE_LIMIT,
    DEFAULT_MODEL_NAME,
    GEMINI_BOT_DEFAULT_API_KEY,
)
from .db import (
    get_supabase_client,
    clear_history_in_db,
    get_user_settings_from_db,
    save_user_settings_to_db,
)
from telegramify_markdown import standardize
from .gemini_utils import fetch_available_models_for_user
from .helpers import split_and_send_message, check_db_and_settings, check_ai_client
from .processing import (
    process_user_message,
    process_text_message,
    process_photo_message,
    user_temp_state,
)

logger = logging.getLogger(__name__)

CALLBACK_SET_MODEL_PREFIX = "set_model:"


def register_handlers(bot_instance: telebot.TeleBot):
    """
    Registers all Telegram command, message, and callback handlers
    with the provided bot instance.
    """
    logger.info("Registering handlers...")

    def send_welcome(message: telebot_types.Message, bot_for_reply: telebot.TeleBot):
        """Sends the welcome/help message."""
        chat_id = message.chat.id
        logger.info(f"Handling /start or /help for {chat_id}")

        if not get_supabase_client():
            bot_for_reply.reply_to(message, "Warning: Database connection issue.")
            logger.warning(f"DB unavailable during welcome for {chat_id}.")

        welcome_text = (
            "Hello! I'm a bot powered by Google Gemini...\n\n"
            "You can chat with me by sending text or photos (with captions).\n"
            "I remember our conversation history (up to model limits).\n\n"
            "Available commands:\n"
            "/start or /help - Show this message.\n"
            "/reset - Clear the current chat history.\n"
            "/set_api_key - Set your personal Gemini API key.\n"
            "/clear_api_key - Use the bot's default API key (if available).\n"
            "/list_models - List models available with your current API key.\n"
            "/select_model - Choose a model using buttons.\n"
            "/current_settings - Show your active API key status and model.\n\n"
            "Note: If you set a new API key or model, your chat history will be reset."
        )
        try:
            bot_for_reply.reply_to(message, welcome_text)
            logger.info(f"Sent welcome message to {chat_id}.")
        except Exception as e:
            logger.error(
                f"Failed to send welcome message to {chat_id}: {e}", exc_info=True
            )

    def handle_reset(message: telebot_types.Message, bot_for_reply: telebot.TeleBot):
        """Handles the /reset command to clear chat history."""
        chat_id = message.chat.id
        logger.info(f"User {chat_id} called /reset")

        # Check DB connection first
        if not get_supabase_client():
            try:
                bot_for_reply.reply_to(
                    message, "Database service is not available. Cannot clear history."
                )
            except Exception as e:
                logger.error(
                    f"Failed to send DB unavailable message during /reset to {chat_id}: {e}"
                )
            logger.error(f"/reset failed for {chat_id}: Supabase client not available.")
            return

        # Attempt to clear history
        if clear_history_in_db(chat_id):
            try:
                bot_for_reply.reply_to(message, "Chat history cleared.")
                logger.info(f"User {chat_id} /reset completed.")
            except Exception as e:
                logger.error(f"Failed to send /reset confirmation to {chat_id}: {e}")
        else:
            try:
                bot_for_reply.reply_to(
                    message, "Failed to clear your chat history in the database."
                )
            except Exception as e:
                logger.error(f"Failed to send /reset failure message to {chat_id}: {e}")
            # clear_history_in_db already logs the DB error
            logger.error(f"/reset failed for {chat_id}: DB operation failed.")

    def handle_set_api_key_command(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles the /set_api_key command, prompting user for their key."""
        chat_id = message.chat.id
        logger.info(f"User {chat_id} called /set_api_key")

        user_temp_state[chat_id] = {"awaiting_api_key": True}

        instructions = (
            "Okay, please send me your Google Gemini API key now. \n"
            "You can get your API key from Google AI Studio:\n"
            "1. Go to [https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)\n"
            "2. Create a new API key \\(or use an existing one\\)\\. \n"
            "3. Copy the key and paste it into a reply message here\\. \n"
            "*(Your API key will be stored securely and used only for your interactions. Setting a new key resets chat history and message count)* \n"
            "Send `/cancel` to abort."
        )
        try:
            bot_for_reply.reply_to(
                message,
                standardize(instructions),
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
            logger.info(f"Sent set_api_key instructions to {chat_id}.")
        except Exception as e:
            logger.error(
                f"Failed to send set_api_key instructions to {chat_id}: {e}",
                exc_info=True,
            )
            # If instructions fail, maybe clear the state?
            user_temp_state.pop(chat_id, None)

    def handle_cancel_command(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles the /cancel command to abort the API key setting process."""
        chat_id = message.chat.id
        logger.info(f"User {chat_id} called /cancel")

        reply_text = "No active operation to cancel\\."
        # Access imported user_temp_state
        if user_temp_state.pop(chat_id, {}).get("awaiting_api_key"):
            reply_text = "Operation cancelled \\(Set API key\\)\\."
            logger.info(f"API key input cancelled for {chat_id}.")

        try:
            bot_for_reply.reply_to(message, reply_text, parse_mode="MarkdownV2")
        except Exception as e:
            logger.error(
                f"Failed to send /cancel confirmation to {chat_id}: {e}", exc_info=True
            )

    def handle_clear_api_key(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles the /clear_api_key command to revert to using the bot's default key."""
        chat_id = message.chat.id
        logger.info(f"User {chat_id} called /clear_api_key")

        user_settings = check_db_and_settings(chat_id, message, bot_for_reply)
        if user_settings is None:
            return  # Helper already sent message

        if user_settings.get("gemini_api_key") is None:
            try:
                bot_for_reply.reply_to(
                    message,
                    "You are already using the bot's default API key\\.",
                    parse_mode="MarkdownV2",
                )
                logger.info(f"User {chat_id} already using default key.")
            except Exception as e:
                logger.error(
                    f"Failed to send clear_api_key 'already default' message to {chat_id}: {e}"
                )
            return

        if not GEMINI_BOT_DEFAULT_API_KEY:
            try:
                bot_for_reply.reply_to(
                    message,
                    "The bot does not have a default API key configured\\. You must provide your own via `/set_api_key`\\.",
                    parse_mode="MarkdownV2",
                )
                logger.warning(
                    f"User {chat_id} tried to clear key, but bot has no default."
                )
            except Exception as e:
                logger.error(
                    f"Failed to send clear_api_key 'no default' message to {chat_id}: {e}"
                )
            return

        if save_user_settings_to_db(
            chat_id,
            api_key=None,
            model_name=user_settings.get("selected_model", DEFAULT_MODEL_NAME),
            message_count=0,
        ):
            clear_history_in_db(chat_id)
            reply_text = "Cleared your custom API key\\. Using the bot's default key now\\. Your chat history has been reset\\."
            log_level = logging.INFO
            log_msg = f"User {chat_id} cleared custom API key completed."
        else:
            reply_text = "Failed to clear your custom API key in the database\\."
            log_level = logging.ERROR
            log_msg = (
                f"/clear_api_key failed for {chat_id}: Failed to save default settings."
            )

        try:
            bot_for_reply.reply_to(message, reply_text, parse_mode="MarkdownV2")
            logger.log(log_level, log_msg)
        except Exception as e:
            logger.error(
                f"Failed to send clear_api_key final message to {chat_id}: {e}"
            )

    def handle_list_models(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles /list_models command to list available generative models."""
        chat_id = message.chat.id
        logger.info(f"User {chat_id} called /list_models")

        user_settings = check_db_and_settings(chat_id, message, bot_for_reply)
        if not user_settings:
            return

        ai_client = check_ai_client(chat_id, message, user_settings, bot_for_reply)
        if not ai_client:
            # Send note only if AI client check failed (helper handles initial reply)
            bot_for_reply.send_message(
                chat_id, "Note: Could not fetch model list...", parse_mode="MarkdownV2"
            )
            return

        try:
            bot_for_reply.send_message(
                chat_id, "Fetching available models (this might take a moment)..."
            )
        except Exception as e:
            logger.error(f"Failed to send 'Fetching models' message to {chat_id}: {e}")
            # Proceed anyway, but log the error

        models_info_list = fetch_available_models_for_user(user_settings)

        if models_info_list is None:
            try:
                bot_for_reply.send_message(
                    chat_id,
                    "Could not fetch available models with your current API key\\. Check `/current_settings` or try `/set_api_key`\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception as e:
                logger.error(
                    f"Failed to send list_models 'fetch failed' message to {chat_id}: {e}"
                )
            return

        if not models_info_list:
            try:
                bot_for_reply.send_message(
                    chat_id,
                    "No generative models found with your current API key\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception as e:
                logger.error(
                    f"Failed to send list_models 'no models found' message to {chat_id}: {e}"
                )
            return

        static_header = "Available Models \\(may vary based on API key/region\\):\n\n"
        models_list_text = static_header
        for model_info in models_info_list:
            model_name = model_info.get("name", "Unknown Model")
            display_name = model_name.replace("models/", "")
            models_list_text += f"💬 **Model name**: `{display_name}`\n"
            description = model_info.get("description")
            if description:
                models_list_text += f"📝 **Description**: ```{description}```\n"
            input_tokens = model_info.get("input_token_limit")
            if input_tokens is not None:
                models_list_text += f"⬇️ **Input Tokens**: {input_tokens}\n"
            output_tokens = model_info.get("output_token_limit")
            if output_tokens is not None:
                models_list_text += f"⬆️ **Output Tokens**: {output_tokens}\n"

            models_list_text += "\n"

        models_list_text += "Use `/select_model` to choose one\\."

        split_and_send_message(message, models_list_text, bot_instance=bot_for_reply)
        logger.info(
            f"User {chat_id} /list_models completed, sent {len(models_info_list)} models."
        )

    def handle_select_model_command(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles /select_model command, presenting models as inline buttons."""
        chat_id = message.chat.id
        logger.info(f"User {chat_id} called /select_model")

        user_settings = check_db_and_settings(chat_id, message, bot_for_reply)
        if not user_settings:
            return
        ai_client = check_ai_client(chat_id, message, user_settings, bot_for_reply)
        if not ai_client:
            return

        try:
            bot_for_reply.send_message(
                chat_id, "Fetching available models to display as buttons..."
            )
        except Exception as e:
            logger.error(f"Failed to send 'Fetching models' message to {chat_id}: {e}")

        models_info_list = fetch_available_models_for_user(user_settings)

        if models_info_list is None:
            try:
                bot_for_reply.reply_to(
                    message,
                    "Could not fetch available models with your current API key\\. Check `/current_settings` or try `/set_api_key`\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception as e:
                logger.error(
                    f"Failed to send select_model 'fetch failed' message to {chat_id}: {e}"
                )
            return

        if not models_info_list:
            try:
                bot_for_reply.reply_to(
                    message,
                    "No generative models found with your current API key to select from.",
                )
            except Exception as e:
                logger.error(
                    f"Failed to send select_model 'no models' message to {chat_id}: {e}"
                )
            return

        # Create inline keyboard markup
        markup = telebot_types.InlineKeyboardMarkup()
        buttons_added = 0
        for model_info in models_info_list:
            model_name = model_info.get("name")
            if not model_name:
                continue

            callback_data = f"{CALLBACK_SET_MODEL_PREFIX}{model_name}"
            button_text = model_name.replace("models/", "")
            if len(button_text) > 30:
                button_text = button_text[:27] + "..."

            if len(callback_data.encode("utf-8")) > 64:
                logger.warning(
                    f"Callback data for model {model_name} exceeds 64 bytes. Skipping button."
                )
                continue

            markup.add(
                telebot_types.InlineKeyboardButton(
                    button_text, callback_data=callback_data
                )
            )
            buttons_added += 1

        if buttons_added == 0:
            try:
                bot_for_reply.reply_to(
                    message,
                    "No models available to display as buttons (possibly due to length limits)\\.",
                    parse_mode="MarkdownV2",
                )
                logger.warning(f"No models resulted in valid buttons for {chat_id}.")
            except Exception as e:
                logger.error(
                    f"Failed to send select_model 'no valid buttons' message to {chat_id}: {e}"
                )
        else:
            try:
                bot_for_reply.reply_to(
                    message, "Please select a model:", reply_markup=markup
                )
                logger.info(f"Sent model selection keyboard to {chat_id}.")
            except Exception as e:
                logger.error(
                    f"Failed to send select_model keyboard to {chat_id}: {e}",
                    exc_info=True,
                )

    def handle_model_selection_callback(
        call: telebot_types.CallbackQuery, bot_for_reply: telebot.TeleBot
    ):
        """Handles the callback when a user selects a model from the inline keyboard."""
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        try:
            model_name = call.data[len(CALLBACK_SET_MODEL_PREFIX) :]
        except IndexError:
            logger.error(f"Invalid callback data received: {call.data}")
            try:
                bot_for_reply.answer_callback_query(
                    call.id, "Error: Invalid selection data."
                )
            except Exception:
                pass  # Ignore if answering fails
            return

        logger.info(f"User {chat_id} selected model via button: {model_name}")

        # Answer the callback query quickly
        try:
            bot_for_reply.answer_callback_query(
                call.id, f"Setting model to {model_name}..."
            )
        except Exception as e:
            logger.warning(
                f"Failed to answer callback query {call.id} for {chat_id}: {e}"
            )

        user_settings = get_user_settings_from_db(chat_id)
        if user_settings is None:
            try:
                # Edit the original message to show the error
                bot_for_reply.edit_message_text(
                    "Error fetching your settings\\. Cannot set model\\.",
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=None,
                    parse_mode="MarkdownV2",
                )
            except Exception as e:
                logger.error(
                    f"Failed to edit message for settings fetch error during callback for {chat_id}: {e}"
                )
            return

        # Check if model is already selected
        current_model = user_settings.get("selected_model", DEFAULT_MODEL_NAME)
        if current_model == model_name:
            response_text = f"Model is already set to `{model_name}`\\."
            logger.info(f"User {chat_id} model selection: model already set.")
        else:
            current_api_key = user_settings.get("gemini_api_key")
            if save_user_settings_to_db(
                chat_id, api_key=current_api_key, model_name=model_name, message_count=0
            ):
                clear_history_in_db(chat_id)
                response_text = f"Model set to `{model_name}` successfully\\! Your chat history has been reset\\."
                logger.info(
                    f"User {chat_id} set model to {model_name} completed via callback."
                )
            else:
                response_text = "Failed to set the model in the database\\."
                logger.error(
                    f"Callback model selection failed for {chat_id}: Failed to save settings."
                )

        # Edit the original message to show the result and remove the keyboard
        try:
            bot_for_reply.edit_message_text(
                response_text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            # Log error, but user already got answer_callback_query hopefully
            logger.error(
                f"Failed to edit message after model selection for {chat_id}: {e}",
                exc_info=True,
            )

    def handle_current_settings(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles /current_settings command to display user's current configuration."""
        chat_id = message.chat.id
        logger.info(f"User {chat_id} called /current_settings")

        user_settings = check_db_and_settings(chat_id, message, bot_for_reply)
        if user_settings is None:
            return  # Helper already sent message

        api_key_status = "Using bot's default API key"
        custom_api_key = user_settings.get("gemini_api_key")
        if custom_api_key:
            key_masked = f"{custom_api_key[:4]}...{custom_api_key[-4:]}"
            api_key_status = f"Using your custom API key: `{key_masked}`"
        elif not GEMINI_BOT_DEFAULT_API_KEY:
            api_key_status = "No API key available. Bot's default is missing, and you haven't set your own.\nPlease use `/set_api_key` to provide your key."

        current_model = user_settings.get("selected_model", DEFAULT_MODEL_NAME)
        current_message_count = user_settings.get("message_count", 0)

        settings_text = (
            "*Your Current Settings*:\n"
            f"API Key: {api_key_status}\n"
            f"Model: `{current_model}`\n"
        )

        # Add message count info only if using default key and limit is enabled
        if (
            user_settings.get("gemini_api_key") is None
            and DEFAULT_KEY_MESSAGE_LIMIT > 0
        ):
            settings_text += f"Messages Used \(Default Key\): {current_message_count} / {DEFAULT_KEY_MESSAGE_LIMIT}\n"
            if current_message_count >= DEFAULT_KEY_MESSAGE_LIMIT:
                settings_text += "  \(Limit reached\. Use `/set_api_key` for unlimited messages\.\)\n"

        try:
            bot_for_reply.reply_to(message, settings_text, parse_mode="MarkdownV2")
            logger.info(f"User {chat_id} /current_settings completed\.")
        except Exception as e:
            logger.error(
                f"Failed to send current settings to {chat_id}: {e}", exc_info=True
            )

    def handle_unknown_command(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles commands that are not recognized."""
        chat_id = message.chat.id
        logger.warning(f"User {chat_id} sent unknown command: {message.text}")
        try:
            bot_for_reply.reply_to(
                message,
                "Unknown command\\. Use `/help` to see available commands\\.",
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(f"Failed to send unknown command message to {chat_id}: {e}")

    def handle_unsupported_content(
        message: telebot_types.Message, bot_for_reply: telebot.TeleBot
    ):
        """Handles content types not explicitly supported."""
        chat_id = message.chat.id
        logger.warning(
            f"User {chat_id} sent unsupported content type: {message.content_type}"
        )
        try:
            bot_for_reply.reply_to(
                message, "Sorry, I can currently only process text and photos."
            )
        except Exception as e:
            logger.error(
                f"Failed to send unsupported content message to {chat_id}: {e}"
            )

    # --- Register Handlers using Decorators on Wrappers ---
    @bot_instance.message_handler(commands=["start", "help"])
    def welcome_wrapper(message):
        send_welcome(message, bot_instance)

    @bot_instance.message_handler(commands=["reset"])
    def reset_wrapper(message):
        handle_reset(message, bot_instance)

    @bot_instance.message_handler(commands=["set_api_key"])
    def set_api_key_wrapper(message):
        handle_set_api_key_command(message, bot_instance)

    @bot_instance.message_handler(commands=["cancel"])
    def cancel_wrapper(message):
        handle_cancel_command(message, bot_instance)

    @bot_instance.message_handler(commands=["clear_api_key"])
    def clear_api_key_wrapper(message):
        handle_clear_api_key(message, bot_instance)

    @bot_instance.message_handler(commands=["list_models"])
    def list_models_wrapper(message):
        handle_list_models(message, bot_instance)

    @bot_instance.message_handler(commands=["select_model"])
    def select_model_wrapper(message):
        handle_select_model_command(message, bot_instance)

    @bot_instance.message_handler(commands=["current_settings"])
    def current_settings_wrapper(message):
        handle_current_settings(message, bot_instance)

    # --- Text & Photo Message Handlers ---
    @bot_instance.message_handler(
        func=lambda message: message.text and not message.text.startswith("/"),
        content_types=["text"],
    )
    def text_message_wrapper(message):
        process_user_message(message, process_text_message, bot_instance)

    @bot_instance.message_handler(content_types=["photo"])
    def photo_message_wrapper(message):
        process_user_message(message, process_photo_message, bot_instance)

    # --- Callback Query Handler ---
    @bot_instance.callback_query_handler(
        func=lambda call: call.data.startswith(CALLBACK_SET_MODEL_PREFIX)
    )
    def model_selection_wrapper(call):
        handle_model_selection_callback(call, bot_instance)

    # --- Catch-all Handler ---
    @bot_instance.message_handler(
        func=lambda message: message.text and message.text.startswith("/")
    )
    def unknown_command_wrapper(message):
        handle_unknown_command(message, bot_instance)

    UNSUPPORTED_CONTENT_TYPES = [
        "audio",
        "document",
        "animation",
        "video",
        "voice",
        "contact",
        "location",
        "venue",
        "game",
        "invoice",
        "successful_payment",
        "sticker",
        "video_note",
        "poll",
        "dice",
        "new_chat_members",
        "left_chat_member",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        "pinned_message",
        "web_app_data",
    ]

    @bot_instance.message_handler(
        func=lambda m: True, content_types=UNSUPPORTED_CONTENT_TYPES
    )
    def unsupported_content_wrapper(message):
        handle_unsupported_content(message, bot_instance)

    logger.info("All handlers registered.")
