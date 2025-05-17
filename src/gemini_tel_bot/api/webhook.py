import json
import logging
import telebot  # type: ignore[no-any-unimported]
from typing import Callable, List
from telebot import types as telebot_types
from .. import handlers
from ..bot import get_bot_instance

logger = logging.getLogger(__name__)


_global_bot_instance: telebot.TeleBot | None = None
_initialization_error: bool = False


def initialize_bot() -> telebot.TeleBot | None:
    """Creates the bot instance and registers handlers once per worker."""
    global _global_bot_instance, _initialization_error
    if _global_bot_instance is not None or _initialization_error:
        # Already initialized or failed permanently for this worker
        return _global_bot_instance

    logger.info("Initializing bot instance for webhook worker...")
    temp_bot_instance = get_bot_instance()

    if temp_bot_instance is None:
        logger.critical(
            "Failed to create bot instance (get_bot_instance returned None). Worker cannot process requests."
        )
        _initialization_error = True
        return None

    try:
        logger.info("Registering handlers for webhook worker...")
        handlers.register_handlers(temp_bot_instance)
        logger.info("Webhook handlers registered successfully.")
        _global_bot_instance = temp_bot_instance
    except Exception as e:
        logger.critical(f"Failed to register webhook handlers: {e}", exc_info=True)
        _initialization_error = True
        _global_bot_instance = None

    return _global_bot_instance


_global_bot_instance = initialize_bot()


def app(
    environ: dict, start_response: Callable[[str, List[tuple[str, str]]], None]
) -> List[bytes]:
    """
    WSGI application entry point for Telegram webhook.
    Handles the HTTP request/response cycle and passes the body to Telebot.
    Uses the pre-initialized global bot instance.
    """
    global _global_bot_instance, _initialization_error

    status = "200 OK"
    headers = [("Content-type", "text/plain")]
    response_body = b"OK"

    if _global_bot_instance is None:
        logger.error(
            "Cannot process webhook request: Bot instance is not available (initialization failed)."
        )
        status = "500 Internal Server Error"
        response_body = b"Bot not configured or initialization failed"
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

                        # Process the update using the pre-initialized global bot instance
                        current_bot_instance.process_new_updates([update])
                        logger.info(
                            f"Webhook finished processing update ID: {update.update_id}"
                        )
                        # Keep status 200 OK

                    except json.JSONDecodeError:
                        # Log invalid JSON format received
                        logger.error(f"Failed to decode webhook body as JSON: {body!r}")
                        status = "400 Bad Request"
                        response_body = b"Invalid JSON body"
                    except Exception as e:
                        # Catch any other errors during Telebot's processing
                        logger.exception(
                            f"Error in process_new_updates for update ID {getattr(update, 'update_id', 'N/A')}:"
                        )
                        # Always return 200 OK to Telegram even if processing failed internally
                        status = "200 OK"
                        response_body = b"Processing Error (check logs)"

            except Exception as e:
                # Catch errors during reading body or initial POST processing
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
        # Catch any unexpected errors during WSGI application execution
        logger.exception("Unexpected error during WSGI request processing:")
        status = "500 Internal Server Error"
        response_body = b"Internal Server Error"

    # Ensure the response body is in bytes before sending
    if not isinstance(response_body, bytes):
        response_body = str(response_body).encode("utf-8")

    logger.debug(f"Webhook responding with status: {status}")
    # Send the HTTP response header
    start_response(status, headers)
    # Return the response body as an iterable (list containing one bytes object)
    return [response_body]
