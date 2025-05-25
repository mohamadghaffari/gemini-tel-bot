import asyncio
import logging
import sys

from gemini_tel_bot import handlers
from gemini_tel_bot.bot import get_bot_instance
from gemini_tel_bot.config import BOT_MODE

log_level = logging.DEBUG if BOT_MODE == "polling" else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if BOT_MODE == "polling":
    # These specific logger level adjustments are only for polling mode
    logging.getLogger("telebot").setLevel(logging.DEBUG)
    logging.getLogger("google.api_core").setLevel(logging.DEBUG)
    logging.getLogger("google.genai").setLevel(logging.DEBUG)
    logger.debug("Debug logging enabled for polling mode in cli.py.")


async def main() -> None:
    """Main function to run the bot in polling mode."""
    logger.info(f"Starting application via CLI in {BOT_MODE} mode.")

    if BOT_MODE == "polling":
        logger.info("Initializing bot for polling...")
        telegram_bot = get_bot_instance()

        if telegram_bot:
            handlers.register_handlers(telegram_bot)
            logger.info("Bot instance created. Starting polling setup...")
            try:
                webhook_info = await telegram_bot.get_webhook_info()
                if webhook_info.url:
                    logger.warning(
                        f"Existing webhook found: {webhook_info.url}. Deleting it to start polling."
                    )
                    await telegram_bot.delete_webhook()
                    logger.info("Webhook deleted successfully.")
                else:
                    logger.info("No active webhook found.")

            except Exception as e:
                logger.error(f"Error checking/deleting webhook: {e}", exc_info=True)
                # Continue polling even if webhook deletion fails

            logger.info("Starting bot polling...")
            try:
                await telegram_bot.polling(non_stop=True)
            except Exception as e:
                logger.critical(f"Bot polling failed: {e}", exc_info=True)
                sys.exit(1)
        else:
            logger.critical("Failed to get bot instance. Cannot start polling.")
            sys.exit(1)
    elif BOT_MODE == "webhook":
        logger.info(
            "CLI invoked in webhook mode. This script is intended for polling mode. "
            "For webhook, ensure your WSGI server (e.g., Gunicorn) is configured to use "
            "gemini_tel_bot.api.webhook:app."
        )
    else:
        logger.critical(
            f"Invalid BOT_MODE specified via CLI: '{BOT_MODE}'. Must be 'polling' or 'webhook'."
        )
        sys.exit(1)


def start_bot_polling() -> None:
    """Synchronous entry point to run the bot in polling mode via asyncio.run."""
    logger.info("Synchronous entry point start_bot_polling() called.")
    asyncio.run(main())


if __name__ == "__main__":
    start_bot_polling()
