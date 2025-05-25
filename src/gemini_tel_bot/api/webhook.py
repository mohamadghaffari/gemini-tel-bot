import asyncio
import json
import logging
from typing import Callable

from telebot import types as telebot_types
from telebot.async_telebot import AsyncTeleBot

from .. import handlers
from ..bot import get_bot_instance

logger = logging.getLogger(__name__)


_global_bot_instance: AsyncTeleBot | None = None
_initialization_error: bool = False


def initialize_bot() -> AsyncTeleBot | None:
    """Creates the bot instance and registers handlers once per worker.
    This webhook module assumes it will work with an AsyncTeleBot instance.
    """
    global _global_bot_instance, _initialization_error
    if _global_bot_instance is not None or _initialization_error:
        return _global_bot_instance

    logger.info(
        "Initializing bot instance for webhook worker (expecting AsyncTeleBot)..."
    )
    temp_bot_instance = get_bot_instance()

    if temp_bot_instance is None:
        logger.critical(
            "Failed to create bot instance (get_bot_instance returned None). Worker cannot process requests."
        )
        _initialization_error = True
        return None

    if not isinstance(temp_bot_instance, AsyncTeleBot):
        logger.critical(
            f"Webhook initialize_bot expected AsyncTeleBot but received {type(temp_bot_instance)}. "
            f"Webhook functionality will likely fail or be incorrect. Assigning anyway to satisfy linter for now."
        )
        _initialization_error = True
        _global_bot_instance = temp_bot_instance
        return _global_bot_instance

    try:
        logger.info("Registering handlers for webhook worker (with AsyncTeleBot)...")
        handlers.register_handlers(temp_bot_instance)
        logger.info("Webhook handlers registered successfully for AsyncTeleBot.")
        _global_bot_instance = temp_bot_instance
    except Exception as e:
        logger.critical(
            f"Failed to register webhook handlers for AsyncTeleBot: {e}", exc_info=True
        )
        _initialization_error = True
        _global_bot_instance = None

    return _global_bot_instance


_global_bot_instance = initialize_bot()


def app(
    environ: dict, start_response: Callable[[str, list[tuple[str, str]]], None]
) -> list[bytes]:
    """
    WSGI application entry point for Telegram webhook.
    Handles the HTTP request/response cycle and passes the body to Telebot.
    Uses the pre-initialized global bot instance.
    """
    global _global_bot_instance, _initialization_error

    status = "200 OK"
    headers = [("Content-type", "text/plain")]
    response_body = b"OK"

    if _global_bot_instance is None or not isinstance(
        _global_bot_instance, AsyncTeleBot
    ):
        logger.error(
            f"Cannot process webhook request: AsyncTeleBot instance is not available. Type: {type(_global_bot_instance)}"
        )
        status = "500 Internal Server Error"
        response_body = b"Bot not configured for async webhook or initialization failed"
        start_response(status, headers)
        return [response_body]

    current_bot_instance = _global_bot_instance
    try:
        if environ.get("REQUEST_METHOD") == "POST":
            logger.debug("Webhook received POST request.")
            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
                body = environ["wsgi.input"].read(content_length)
                logger.debug(f"Webhook read body ({content_length} bytes).")

                if not body:
                    logger.warning("Received POST request with empty body.")
                    status = "400 Bad Request"
                    response_body = b"Empty body"
                else:
                    try:
                        update_json_str = body.decode("utf-8")
                        logger.debug(f"Webhook update body: {update_json_str[:200]}...")
                        update = telebot_types.Update.de_json(update_json_str)
                        logger.info(f"Webhook processing update ID: {update.update_id}")

                        async def process_update_async(
                            bot: AsyncTeleBot, upd: telebot_types.Update
                        ) -> None:
                            await bot.process_new_updates([upd])

                        asyncio.run(process_update_async(current_bot_instance, update))

                        logger.info(
                            f"Webhook finished processing update ID: {update.update_id}"
                        )
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode webhook body as JSON: {body!r}")
                        status = "400 Bad Request"
                        response_body = b"Invalid JSON body"
                    except Exception as e:
                        logger.exception(
                            f"Error in async processing for update ID {getattr(update, 'update_id', 'N/A')}:"
                        )
                        status = "200 OK"
                        response_body = b"Processing Error (check logs)"

            except Exception as e:
                logger.exception("Error during POST request body processing:")
                status = "500 Internal Server Error"
                response_body = b"Internal Server Error reading request"

        else:
            # Handle non-POST requests gracefully
            status = "405 Method Not Allowed"
            headers = [("Content-type", "text/plain"), ("Allow", "POST")]
            response_body = (
                b"This is a Telegram bot webhook endpoint. Please send POST requests."
            )
            logger.warning(
                f"Received non-POST request: {environ.get('REQUEST_METHOD')}"
            )

    except Exception as e:
        logger.exception("Unexpected error during WSGI request processing:")
        status = "500 Internal Server Error"
        response_body = b"Internal Server Error"

    if not isinstance(response_body, bytes):
        response_body = str(response_body).encode("utf-8")

    logger.debug(f"Webhook responding with status: {status}")
    start_response(status, headers)

    return [response_body]
