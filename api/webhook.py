import json
import logging
import sys
import traceback
from telebot import types as telebot_types
from bot import get_bot_instance, user_temp_state

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def app(environ, start_response):
    """
    WSGI application entry point for Telegram webhook.
    Handles the HTTP request/response cycle and passes the body to Telebot.
    """
    status = '200 OK'
    headers = [('Content-type', 'text/plain')]
    response_body = b'OK'

    try:
        if environ['REQUEST_METHOD'] == 'POST':
            content_length = int(environ.get('CONTENT_LENGTH', 0))
            body = environ['wsgi.input'].read(content_length)

            if not body:
                 logging.warning("Received POST request with empty body.")
                 status = '400 Bad Request'
                 response_body = b'Empty body'
            else:
                # Get or initialize the bot instance (cached)
                telegram_bot = get_bot_instance()

                if telegram_bot is None:
                    logging.critical("Telegram bot instance could not be initialized. BOT_API_KEY missing?")
                    status = '500 Internal Server Error'
                    response_body = b'Bot not configured'
                else:
                    try:
                        # --- Usage of telebot_types ---
                        # This requires the 'from telebot import types as telebot_types' import above
                        update = telebot_types.Update.de_json(body.decode('utf-8'))

                        telegram_bot.process_new_updates([update])

                    except Exception as e:
                        logging.exception("Error processing Telegram update:")
                        status = '200 OK' # Keep 200 for Telegram, log the error
                        response_body = b'Processing Error (check logs)'

        else:
            status = '200 OK' # Or '405 Method Not Allowed'
            response_body = b'This is a Telegram bot webhook endpoint. Please send POST requests.'
            headers = [('Content-type', 'text/plain')]

    except Exception as e:
        logging.exception("Unexpected error during WSGI request processing:")
        status = '500 Internal Server Error'
        response_body = b'Internal Server Error'

    start_response(status, headers)
    return [response_body]