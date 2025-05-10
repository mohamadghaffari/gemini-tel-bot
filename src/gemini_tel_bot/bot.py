import time
import logging
import telebot
from .config import BOT_API_KEY

logger = logging.getLogger(__name__)


def get_bot_instance() -> telebot.TeleBot | None:
    """Initializes and returns the Telegram Bot instance."""
    logger.info("Initializing Telegram bot instance...")
    start_time = time.time()

    if not BOT_API_KEY:
        logger.critical("BOT_API_KEY is not set. Cannot initialize Telegram bot.")
        return None

    try:
        bot_object = telebot.TeleBot(BOT_API_KEY)
        init_time = time.time() - start_time
        logger.info(f"Telegram bot instance created in {init_time:.4f} seconds.")

        return bot_object
    except Exception as e:
        logger.critical(f"Failed to create TeleBot instance: {e}", exc_info=True)
        return None
