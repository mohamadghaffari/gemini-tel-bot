import sys
import logging
from config import BOT_MODE
from dotenv import load_dotenv
from bot import get_bot_instance
import handlers

try:
    load_dotenv()
    print("Attempted to load environment variables from .env")
except ImportError:
    print(
        "Warning: python-dotenv not installed. Cannot load .env file.", file=sys.stderr
    )
    pass  # Continue execution, environment variables will be read from the system

log_level = logging.DEBUG if BOT_MODE == "polling" else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if BOT_MODE == "polling":
    logging.getLogger("telebot").setLevel(logging.DEBUG)
    logging.getLogger("google.api_core").setLevel(logging.DEBUG)
    logging.getLogger("google.genai").setLevel(logging.DEBUG)
    logger.debug("Debug logging enabled.")

from bot import get_bot_instance

if __name__ == "__main__":
    logger.info(f"Starting application in {BOT_MODE} mode.")

    if BOT_MODE == "polling":
        # This block runs ONLY when run.py is executed directly AND BOT_MODE is 'polling'

        logger.info("Initializing bot for polling...")
        telegram_bot = get_bot_instance()

        if telegram_bot:
            handlers.register_handlers(telegram_bot)
            logger.info("Bot instance created. Starting polling setup...")
            try:
                webhook_info = telegram_bot.get_webhook_info()
                if webhook_info.url:
                    logger.warning(
                        f"Existing webhook found: {webhook_info.url}. Deleting it to start polling."
                    )
                    telegram_bot.delete_webhook()
                    logger.info("Webhook deleted successfully.")
                else:
                    logger.info("No active webhook found.")

            except Exception as e:
                # Catch potential errors during get_webhook_info or delete_webhook
                logger.error(f"Error checking/deleting webhook: {e}", exc_info=True)
                # Continue polling even if webhook deletion fails, might still work depending on Telegram state

            logger.info("Starting bot polling...")
            try:
                # Start the polling loop
                telegram_bot.infinity_polling()
            except Exception as e:
                logger.critical(f"Bot polling failed: {e}", exc_info=True)
                sys.exit(1)
        else:
            logger.critical("Failed to get bot instance. Cannot start polling.")
            sys.exit(1)

    elif BOT_MODE == "webhook":
        # In webhook mode, the WSGI server (like Gunicorn/uvicorn) will import
        # and run the 'app' from api/webhook.py.
        # api/webhook.py must call get_bot_instance() itself when a request arrives.
        # This script doesn't do anything further in this mode.
        logger.info(
            "Running in webhook mode. Relying on WSGI server to import api/webhook.py and handle requests."
        )
        # The process running this script will likely just wait for the WSGI server.
        pass  # Exit the if name == "main": block
    else:
        logger.critical(
            f"Invalid BOT_MODE specified: '{BOT_MODE}'. Must be 'polling' or 'webhook'."
        )
        sys.exit(1)
