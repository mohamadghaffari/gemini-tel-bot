import json
import logging
import handlers
from bot import get_bot_instance
from telebot import types as telebot_types

logger = logging.getLogger(__name__)
_handlers_registered = False


def register_handlers_if_needed(bot_instance):
    global _handlers_registered
    if not _handlers_registered:
        try:
            logger.info("Registering handlers for webhook worker...")
            handlers.register_handlers(bot_instance)
            _handlers_registered = True
            logger.info("Webhook handlers registered successfully.")
        except Exception as e:
            logger.critical(f"Failed to register webhook handlers: {e}", exc_info=True)


# The WSGI application callable
def app(environ: dict, start_response):
    """
    WSGI application entry point for Telegram webhook.
    Handles the HTTP request/response cycle and passes the body to Telebot.
    """
    status = "200 OK"
    headers = [("Content-type", "text/plain")]
    response_body = b"OK"

    try:
        # Only process POST requests for Telegram updates
        if environ.get("REQUEST_METHOD") == "POST":
            try:
                # Get content length from headers
                content_length = int(environ.get("CONTENT_LENGTH", 0))
                # Read the request body from the WSGI input stream
                body = environ["wsgi.input"].read(content_length)

                if not body:
                    logger.warning("Received POST request with empty body.")
                    status = "400 Bad Request"
                    response_body = b"Empty body"
                else:
                    # Get or initialize the bot instance for this worker process.
                    # This call ensures the bot object is ready and handlers are registered.
                    telegram_bot = get_bot_instance()
                    if telegram_bot:
                        register_handlers_if_needed(telegram_bot)

                    if telegram_bot is None:
                        logger.critical(
                            "Telegram bot instance could not be initialized. BOT_API_KEY missing?"
                        )
                        status = "500 Internal Server Error"
                        response_body = b"Bot not configured"
                    else:
                        try:
                            # Decode the body and convert to Telebot Update object
                            update = telebot_types.Update.de_json(body.decode("utf-8"))
                            # Process the update using Telebot's method
                            telegram_bot.process_new_updates([update])

                        except json.JSONDecodeError:
                            # Log invalid JSON format received
                            logger.error("Failed to decode webhook body as JSON.")
                            status = "400 Bad Request"
                            response_body = b"Invalid JSON body"
                        except Exception as e:
                            # Catch any other errors during Telebot's processing
                            logger.exception("Error processing Telegram update:")
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

    # Send the HTTP response header
    start_response(status, headers)
    # Return the response body as an iterable (list containing one bytes object)
    return [response_body]
