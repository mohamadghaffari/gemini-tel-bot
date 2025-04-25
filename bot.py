# bot.py - Contains core bot logic, handlers, Supabase interaction, and AI client setup

import os
import json
import logging
import sys
import traceback
# from dotenv import load_dotenv # You can use this if your cloud provider doesn't supprts setting environmets, but make sure to not push your .env in the repo :)
from google import genai
from google.genai import types
import telebot
from telebot import types as telebot_types
from supabase import create_client, Client
import time
import google.api_core.exceptions
import telebot.util as util


# --- Configuration ---
DEFAULT_MODEL_NAME = 'models/gemini-1.5-flash-latest'
DEFAULT_KEY_MESSAGE_LIMIT = 5 # <-- Define the limit for users without a custom key
# Define a soft limit for history length for AI context window (number of turns)
MAX_HISTORY_LENGTH_TURNS = 20 # Example: Keep last 20 turns of history


# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Environment Variables for Supabase ---
# These are read directly from your cloud provider environment variables (eg: Railway)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_API_KEY = os.getenv("BOT_API_KEY")
GEMINI_BOT_DEFAULT_API_KEY = os.getenv("GEMINI_BOT_DEFAULT_API_KEY")


# --- Supabase Client Initialization ---
# Initialize the Supabase client once per process lifecycle and cache it
_cached_supabase_client: Client | None = None

def get_supabase_client() -> Client | None:
    """Initializes and returns the Supabase client instance (cached)."""
    global _cached_supabase_client
    if _cached_supabase_client is None:
        logging.info("Initializing Supabase client...")
        if not SUPABASE_URL or not SUPABASE_KEY:
            logging.critical("SUPABASE_URL or SUPABASE_KEY environment variables not set.")
            return None

        try:
            start_time = time.time()
            # Use the create_client function from supabase-py
            _cached_supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
            init_time = time.time() - start_time
            logging.info(f"Supabase client initialized successfully in {init_time:.4f} seconds.")
        except Exception as e:
            logging.critical(f"Failed to initialize Supabase client: {e}")
            _cached_supabase_client = None # Ensure it's None on failure
            logging.exception("Supabase client initialization traceback:")

    return _cached_supabase_client

# --- Database Interaction Functions (Using Supabase Client) ---
# These functions will get the cached client instance

def get_user_settings_from_db(chat_id):
    """Fetches user settings from the database using Supabase."""
    logging.info(f"Fetching settings for {chat_id} from Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logging.error("get_user_settings_from_db failed: Supabase client not available.")
        return None

    start_time = time.time()
    try:
        # --- Include message_count in select ---
        response = supabase_client.table('user_settings').select('gemini_api_key, selected_model, message_count').eq('chat_id', chat_id).execute()
        end_time = time.time() - start_time
        logging.info(f"Fetched settings for {chat_id} in {end_time:.4f} seconds. Response data: {response.data}")

        if response.data and len(response.data) > 0:
            settings = response.data[0] # Supabase returns a list of rows
            # Ensure message_count defaults to 0 if somehow not set in DB
            settings['message_count'] = settings.get('message_count', 0)
            return settings
        else:
            logging.info(f"No settings found for {chat_id} in Supabase, returning defaults.")
            # --- Include message_count default ---
            return {'gemini_api_key': None, 'selected_model': DEFAULT_MODEL_NAME, 'message_count': 0}
    except Exception as e:
        logging.error(f"Error fetching settings for {chat_id} from Supabase: {e}")
        # Log traceback for DB errors
        logging.exception("Supabase fetch settings traceback:")
        return None # Indicate fetch error


def save_user_settings_to_db(chat_id, api_key, model_name, message_count=None):
    """Saves or updates user settings in the database using Supabase."""
    # Add message_count parameter, default to None so it's not always updated
    logging.info(f"Saving settings for {chat_id} to Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logging.error("Cannot save settings, Supabase client not available.")
        return False

    start_time = time.time()
    try:
        # Prepare data for upsert
        data_to_save = {
            'chat_id': chat_id,
            # Only include fields we are explicitly setting/updating
            'gemini_api_key': api_key,
            'selected_model': model_name,
            # --- Include message_count if provided ---
        }
        if message_count is not None: # Only include if we are specifically updating it
             data_to_save['message_count'] = message_count


        # Use Supabase client's upsert method (insert or update based on primary key: chat_id)
        response = supabase_client.table('user_settings').upsert(data_to_save).execute()
        end_time = time.time() - start_time
        logging.info(f"Saved settings for {chat_id} to Supabase in {end_time:.4f} seconds. Response data: {response.data}")

        if response.data: # Upsert usually returns the saved data on success
             return True
        else:
             logging.error(f"Supabase upsert operation returned no data for {chat_id}. Response: {response}")
             # This might indicate a Supabase error or configuration issue
             return False

    except Exception as e:
        logging.error(f"Error saving settings for {chat_id} to Supabase: {e}")
        logging.exception("Supabase save settings traceback:")
        return False


def get_history_from_db(chat_id):
    """Fetches chat history content for a user from Supabase."""
    logging.info(f"Fetching history for {chat_id} from Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logging.error("get_history_from_db failed: Supabase client not available.")
        return None

    start_time = time.time()
    try:
        response = supabase_client.table('chat_history').select('role, parts_json').eq('chat_id', chat_id).order('turn_index').execute()
        end_time = time.time() - start_time
        logging.info(f"Fetched history for {chat_id} in {end_time:.4f} seconds ({len(response.data or [])} rows).")

        history = []
        if response.data:
            for row in response.data:
                role = row.get('role')
                parts_data_raw = row.get('parts_json')
                parts_data = None
                if isinstance(parts_data_raw, str):
                    try:
                        parts_data = json.loads(parts_data_raw)
                        # Log if manual load was needed
                        logging.warning(f"Manually loaded parts_json string for chat_id {chat_id}, turn {row.get('turn_index')}. Type was str.")
                    except json.JSONDecodeError:
                        logging.error(f"Failed to decode parts_json string for chat_id {chat_id}, turn {row.get('turn_index')}.")
                        continue # Skip this row if JSON is invalid
                elif isinstance(parts_data_raw, list) or isinstance(parts_data_raw, dict): # Check if it's already list/dict
                     parts_data = parts_data_raw
                else:
                     logging.warning(f"parts_json for chat_id {chat_id}, turn {row.get('turn_index')} is neither string nor list/dict. Type: {type(parts_data_raw)}. Skipping row.")
                     continue # Skip this row if data type is unexpected


                # --- Proceed with reconstruction if parts_data is now a list ---
                if role is not None and isinstance(parts_data, list):
                    parts_list = []
                    for p in parts_data:
                        if isinstance(p, dict):
                             # --- Reconstruct different part types ---
                             if p.get('type') == 'text' and p.get('text') is not None:
                                parts_list.append(types.Part(text=p['text']))
                             elif p.get('type') == 'image' and p.get('mime_type'):
                                # For images in history, provide a text placeholder.
                                # Include caption if saved in the JSON data.
                                 image_text = f"[Image: {p.get('mime_type', 'image')}]"
                                 if p.get('caption'): # Check for caption in the JSON data
                                     image_text += f" (Caption: {p['caption']})"
                                 parts_list.append(types.Part(text=image_text))
                             # Add logic here for other part types if you saved them (e.g., function_code, function_response)

                    # Only add the turn if it resulted in at least one part being reconstructed
                    # and the role is valid for AI history.
                    if parts_list:
                         if role in ['user', 'model']:
                              history.append(types.Content(role=role, parts=parts_list))
                         else:
                              logging.warning(f"Skipping history row for {chat_id} with unsupported role '{role}' during reconstruction.")

                # If parts_list is empty, this turn is skipped for history reconstruction

        # Only keep the last N turns to avoid exceeding model context window
        if MAX_HISTORY_LENGTH_TURNS > 0 and len(history) > MAX_HISTORY_LENGTH_TURNS:
             logging.warning(f"History length {len(history)} exceeds MAX_HISTORY_LENGTH_TURNS {MAX_HISTORY_LENGTH_TURNS} for {chat_id}. Truncating history.")
             # Keep only the last N turns
             history = history[-MAX_HISTORY_LENGTH_TURNS:]

        return history
    except Exception as e:
        logging.error(f"Error fetching history for {chat_id} from Supabase: {e}")
        logging.exception("Supabase fetch history traceback:")
        return None

def save_turn_to_db(chat_id, turn_index, role, parts):
    """Saves a user or model turn with a specific turn_index to chat_history using Supabase (UPSERTs)."""
    logging.info(f"Saving turn {turn_index} for {chat_id} ({role}) to Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logging.error("Cannot save turn, Supabase client not available.")
        return False

    start_time = time.time()
    try:
        parts_data = []
        for part in parts:
             part_dict = {}
             # --- Save different part types ---
             if hasattr(part, 'text') and part.text is not None:
                 part_dict['text'] = part.text
                 part_dict['type'] = 'text'
                 # Note: Caption is handled in the handler before calling this function for image parts.
                 # If a text part *also* had a caption attribute (unlikely for standard Parts), add it.
                 if hasattr(part, 'caption') and part.caption is not None:
                      part_dict['caption'] = part.caption


             elif hasattr(part, 'inline_data') and part.inline_data and hasattr(part.inline_data, 'data'):
                 # For multimodal input turns (images), save a representation of the image part
                 # We are NOT storing raw bytes here due to DB size/performance concerns for history.

                 part_dict['type'] = 'image'
                 part_dict['mime_type'] = part.inline_data.mime_type if hasattr(part.inline_data, 'mime_type') else 'unknown'
                 part_dict['size'] = len(part.inline_data.data) if hasattr(part.inline_data, 'data') and part.inline_data.data is not None else 0
                 # Get caption if it was added to the part_dict in the handler before calling this function
                 # This is the mechanism for multimodal captions.
                 if 'caption' in part_dict:
                     part_dict['caption'] = part_dict['caption']


             # Add other part types if needed (e.g., function_code, function_response)
             # Check if part has specific data for other types, e.g., function_response
             if hasattr(part, 'function_response') and part.function_response is not None:
                  part_dict['type'] = 'function_response'
                  part_dict['function_response'] = {
                      'name': part.function_response.name,
                      # Assuming response has a content field - adjust if structure is different
                      'content': getattr(part.function_response, 'content', None)
                  }
             # Add function_call if the model responded with one
             if hasattr(part, 'function_call') and part.function_call is not None:
                 part_dict['type'] = 'function_call'
                 part_dict['function_call'] = {
                     'name': part.function_call.name,
                     # Assuming args are a dict - adjust if structure is different
                     'args': getattr(part.function_call, 'args', None)
                 }



             if part_dict: # Only save if we extracted some data from the part
                 parts_data.append(part_dict)

        # If it's a model response with no savable parts (e.g., only function calls), save the turn index with empty parts_json
        # We *should* save model turns even if they have no visible text/image parts (e.g., pure tool use)
        # to maintain the correct turn sequence.
        if not parts_data and role == 'model':
             logging.warning(f"Model turn {turn_index} for {chat_id} has no savable parts, saving with empty parts_json.")
             parts_data = [] # Ensure it's an empty list
        elif not parts_data and role == 'user':
             # User turns *should* typically have parts (text or image). If not, log an error and maybe don't save.
             logging.error(f"User turn {turn_index} for {chat_id} has no savable parts, not saving.")
             return False # Don't save an empty user turn

        data_to_save = {
            'chat_id': chat_id,
            'turn_index': turn_index, # Use the calculated index
            'role': role,
            'parts_json': parts_data # Supabase client handles JSONB serialization
        }

        # --- Use Supabase client's UPSERT method ---
        # This assumes turn_index + chat_id are a unique primary key.
        # If a row with this key exists, it will be updated. If not, it will be inserted.
        # This is safer against duplicate key errors from retries.
        response = supabase_client.table('chat_history').upsert(data_to_save).execute()

        end_time = time.time() - start_time
        logging.info(f"Saved turn {turn_index} for {chat_id} ({role}) to Supabase in {end_time:.4f} seconds. Response data: {response.data}")

        if response.data: # Upsert usually returns the saved data on success
             return True
        else:
             # This might indicate a Supabase error or configuration issue
             logging.error(f"Supabase upsert operation returned no data for turn {turn_index} for {chat_id}. Response: {response}")
             return False

    except Exception as e:
        logging.error(f"Error saving turn {turn_index} for {chat_id} ({role}) to Supabase: {e}")
        logging.exception("Supabase save turn traceback:")
        return False

def clear_history_in_db(chat_id):
    logging.info(f"Clearing history for {chat_id} in Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logging.error("Cannot clear history, Supabase client not available.")
        return False

    start_time = time.time()
    try:
        response = supabase_client.table('chat_history').delete().eq('chat_id', chat_id).execute()
        end_time = time.time() - start_time
        logging.info(f"Cleared history for {chat_id} in Supabase in {end_time:.4f} seconds. Response: {response}")
        return True
    except Exception as e:
        logging.error(f"Error clearing history for {chat_id} in Supabase: {e}")
        logging.exception("Supabase clear history traceback:")
        return False


# --- Gen AI Client (Initialized on demand per process) ---
_cached_genai_clients = {}

def get_user_client(user_settings):
    logging.info("Getting user GenAI client...")
    api_key_to_use = user_settings.get('gemini_api_key')

    if api_key_to_use is None:
        api_key_to_use = os.getenv("GEMINI_BOT_DEFAULT_API_KEY")
        if not api_key_to_use:
            logging.warning("Neither user API key nor bot default key found.")
            return None

    # Check cache first
    if api_key_to_use in _cached_genai_clients:
        return _cached_genai_clients[api_key_to_use]

    start_time = time.time()
    try:
        logging.info(f"Creating new GenAI client instance for key starting with {api_key_to_use[:4]}...")
        client = genai.Client(api_key=api_key_to_use)
        _cached_genai_clients[api_key_to_use] = client
        client_time = time.time() - start_time
        logging.info(f"Created new GenAI client instance (cached) in {client_time:.4f} seconds.")
        return client
    except Exception as e:
        client_time = time.time() - start_time
        logging.error(f"Failed to create GenAI client for key starting with {api_key_to_use[:4]}... after {client_time:.4f} seconds: {e}")
        logging.exception("GenAI client creation traceback:")
        return None


def fetch_available_models_for_user(user_settings):
    logging.info("Fetching available models...")
    start_time = time.time()
    try:
        client_for_user = get_user_client(user_settings)
        if client_for_user is None:
            logging.warning("Cannot list models: No valid client available for user.")
            return None

        logging.info("Calling client_for_user.models.list()...")
        list_start_time = time.time()
        models = client_for_user.models.list()
        list_time = time.time() - list_start_time
        logging.info(f"client_for_user.models.list() completed in {list_time:.4f} seconds. Found {len(models)} raw models.")

        generative_models_info = []
        for m in models:
            if hasattr(m, 'name') and m.name:
                model_name = m.name
                # Filter based on name pattern (Gemini models usually contain 'gemini-' or start with 'models/gemini-')
                # and exclude embedding models.
                if (('gemini-' in model_name.lower() or model_name.startswith('models/gemini-'))
                    and 'embedding' not in model_name.lower()
                    and 'tunedModels/' not in model_name
                    ):


                     model_info = {'name': model_name}
                     # Add relevant capabilities/info if available on the Model object
                     if hasattr(m, 'description') and m.description:
                          model_info['description'] = m.description
                     if hasattr(m, 'input_token_limit') and m.input_token_limit is not None:
                          model_info['input_token_limit'] = m.input_token_limit
                     if hasattr(m, 'output_token_limit') and m.output_token_limit is not None:
                          model_info['output_token_limit'] = m.output_token_limit
                     if hasattr(m, 'version') and m.version:
                         model_info['version'] = m.version
                     if hasattr(m, 'supported_actions') and m.supported_actions:
                          # Format supported_actions as a list of strings
                          actions = [a for a in m.supported_actions] if isinstance(m.supported_actions, list) else m.supported_actions
                          model_info['supported_actions'] = actions


                     generative_models_info.append(model_info)

        # --- Sort models for display ---
        # Sort by name for consistency. You could sort by version if it implies recency.
        generative_models_info.sort(key=lambda x: x.get('name', ''))


        end_time = time.time() - start_time
        logging.info(f"Fetched, filtered, and sorted {len(generative_models_info)} available models with info based on name pattern in {end_time:.4f} seconds.")
        return generative_models_info # Return list of dicts with info
    except Exception as e:
        end_time = time.time() - start_time
        logging.error(f"Error listing models after {end_time:.4f} seconds: {e}")
        logging.exception("GenAI model listing traceback:")
        return None


# --- Telegram Bot Instance (Initialized on demand per process lifecycle) ---
_bot_instance = None

def get_bot_instance():
    """Initializes and returns the Telegram Bot instance (cached)."""
    global _bot_instance
    if _bot_instance is None:
        logging.info("Initializing Telegram bot instance...")
        start_time = time.time()
        bot_api_key = os.getenv("BOT_API_KEY")
        if not bot_api_key:
            logging.critical("BOT_API_KEY is not set. Cannot initialize Telegram bot.")
            return None

        _bot_instance = telebot.TeleBot(bot_api_key, parse_mode=None)
        init_time = time.time() - start_time
        logging.info(f"Telegram bot instance initialized in {init_time:.4f} seconds.")

        # --- Register Handlers HERE ---
        @_bot_instance.message_handler(commands=['start', 'help'])
        def send_welcome(message):
             if not get_supabase_client():
                  _bot_instance.reply_to(message, "Database service is not available. Bot may not function correctly.")
                  logging.error(f"/start failed for {message.chat.id}: Supabase client not available.")
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
             _bot_instance.reply_to(message, welcome_text)

        @_bot_instance.message_handler(commands=['reset'])
        def handle_reset(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} called /reset")
            if not get_supabase_client():
                 _bot_instance.reply_to(message, "Database service is not available.")
                 logging.error(f"/reset failed for {chat_id}: Supabase client not available.")
                 return

            if clear_history_in_db(chat_id):
                 _bot_instance.reply_to(message, "Chat history is cleared")


        @_bot_instance.message_handler(commands=['set_api_key'])
        def handle_set_api_key_command(message):
            chat_id = message.chat.id
            # Allow setting API key regardless of message limit
            user_temp_state[chat_id] = {'awaiting_api_key': True}

            instructions = (
                "Okay, please send me your Google Gemini API key now.\n\n"
                "You can get your API key from Google AI Studio:\n"
                "1. Go to [https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)\n"
                "2. Create a new API key (or use an existing one).\n"
                "3. Copy the key and paste it into a reply message here.\n\n"
                "*(Your API key will be stored securely and used only for your interactions with this bot. Setting a new key will reset your chat history.)*\n\n"
                "Send /cancel to abort."
            )
            _bot_instance.reply_to(message, instructions, parse_mode='Markdown')


        @_bot_instance.message_handler(commands=['cancel'])
        def handle_cancel_command(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} called /cancel")
            if user_temp_state.pop(chat_id, {}).get('awaiting_api_key'):
                 _bot_instance.reply_to(message, "Operation cancelled.")
            else:
                 _bot_instance.reply_to(message, "No active operation to cancel.")

        @_bot_instance.message_handler(commands=['clear_api_key'])
        def handle_clear_api_key(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} called /clear_api_key")

            if not get_supabase_client():
                 _bot_instance.reply_to(message, "Database service is not available.")
                 logging.error(f"/clear_api_key failed for {chat_id}: Supabase client not available.")
                 return

            user_settings = get_user_settings_from_db(chat_id)

            if user_settings is None:
                _bot_instance.reply_to(message, "Error fetching your settings from the database.")
                return

            if user_settings.get('gemini_api_key') is None:
                _bot_instance.reply_to(message, "You are already using the bot's default API key.")
                return

            bot_api_key_set = os.getenv("BOT_API_KEY") is not None
            default_client_possible = os.getenv("GEMINI_BOT_DEFAULT_API_KEY") is not None

            if not bot_api_key_set and not default_client_possible:
                 _bot_instance.reply_to(message, "The bot does not have a default API key configured. You must provide your own via /set_api_key.")
                 return

            # When clearing API key, reset message count as they are now using default
            if save_user_settings_to_db(chat_id, api_key=None, model_name=user_settings['selected_model'], message_count=0): # Keep current model, set key to NULL, reset count
                clear_history_in_db(chat_id)
                _bot_instance.reply_to(message, "Cleared your custom API key. Using the bot's default key now. Your chat history has been reset.")
                logging.info(f"User {chat_id} cleared custom API key completed.")
            else:
                _bot_instance.reply_to(message, "Failed to clear your custom API key in the database.")
                logging.error(f"/clear_api_key failed for {chat_id}: Failed to save default settings.")


        @_bot_instance.message_handler(commands=['list_models'])
        def handle_list_models(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} called /list_models")
            _bot_instance.reply_to(message, "Fetching available models...")

            if not get_supabase_client():
                 _bot_instance.reply_to(message, "Database service is not available.")
                 logging.error(f"/list_models failed for {chat_id}: Supabase client not available.")
                 return

            user_settings = get_user_settings_from_db(chat_id)
            if user_settings is None:
                 _bot_instance.reply_to(message, "Error fetching your settings from the database.")
                 return

            models_info_list = fetch_available_models_for_user(user_settings) # This now returns dicts with info

            if models_info_list is None:
                _bot_instance.reply_to(message, "Could not fetch available models with your current API key. Please check your key using /current_settings or try setting it again with /set_api_key.")
                return

            if not models_info_list:
                _bot_instance.reply_to(message, "No generative models found with your current API key.")
                return

            models_list_text = "Available Models (may vary based on your API key and region):\n\n" # Added newline

            for model_info in models_info_list:
                model_name = model_info.get('name', 'Unknown Model')
                button_text = model_name.replace("models/", "") # Display shorter name
                if len(button_text) > 30:
                     button_text = button_text[:27] + "..."

                models_list_text += f"`{button_text}`\n" # Use button text for display
                if model_info.get('description'):
                    # Truncate description if too long
                    description = model_info['description']
                    if len(description) > 200:
                         description = description[:147] + "..."
                    models_list_text += f"  Description: {description}\n"
                if model_info.get('input_token_limit') is not None:
                    models_list_text += f"  Input Tokens: {model_info['input_token_limit']}\n"
                if model_info.get('output_token_limit') is not None:
                    models_list_text += f"  Output Tokens: {model_info['output_token_limit']}\n"
                if model_info.get('supported_actions') is not None:
                    models_list_text += f"  Supported actions: {",".join(model_info['supported_actions'])}\n"
                
                models_list_text += "\n"

            models_list_text += "\nUse the /select_model command and buttons to choose a model."

            # Split long messages if necessary
            splitted_text = util.smart_split(models_list_text, chars_per_string=3000)
            for text in splitted_text:
                _bot_instance.send_message(chat_id, text)
            logging.info(f"User {chat_id} /list_models completed.")

        @_bot_instance.message_handler(commands=['select_model'])
        def handle_select_model_command(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} called /select_model")

            if not get_supabase_client():
                 _bot_instance.reply_to(message, "Database service is not available. Cannot fetch models.")
                 logging.error(f"/select_model failed for {chat_id}: Supabase client not available.")
                 return

            user_settings = get_user_settings_from_db(chat_id)
            if user_settings is None:
                 _bot_instance.reply_to(message, "Error fetching your settings from the database.")
                 return

            models_info_list = fetch_available_models_for_user(user_settings)
            if models_info_list is None:
                _bot_instance.reply_to(message, "Could not fetch available models with your current API key. Please check your key using /current_settings or try setting it again with /set_api_key.")
                return

            if not models_info_list:
                _bot_instance.reply_to(message, "No generative models found with your current API key.")
                return

            # Create inline keyboard markup
            markup = telebot_types.InlineKeyboardMarkup()
            for model_info in models_info_list:
                model_name = model_info.get('name')
                if not model_name: continue # Skip if name is missing

                # Create a button for each model
                # The callback_data will be used to identify which button was pressed
                # Limit callback_data size (max 64 bytes)
                callback_data = f"set_model:{model_name}"
                # Truncate model_name displayed on button if too long
                button_text = model_name.replace("models/", "") # Display shorter name
                if len(button_text) > 30: # Keep button text concise
                     button_text = button_text[:27] + "..."

                # Check if callback_data exceeds Telegram limit
                if len(callback_data.encode('utf-8')) > 64:
                    logging.warning(f"Callback data for model {model_name} exceeds 64 bytes ({len(callback_data.encode('utf-8'))}). Skipping button.")
                    continue # Skip this button

                markup.add(telebot_types.InlineKeyboardButton(button_text, callback_data=callback_data))

            if not markup.keyboard:
                 _bot_instance.reply_to(message, "No models available to display as buttons.")
                 logging.warning(f"No models resulted in valid buttons for {chat_id}.")
            else:
                 _bot_instance.reply_to(message, "Please select a model:", reply_markup=markup)
                 logging.info(f"Sent model selection keyboard to {chat_id}.")


        # --- Callback Query Handler for Model Selection ---
        @_bot_instance.callback_query_handler(func=lambda call: call.data.startswith('set_model:'))
        def handle_model_selection_callback(call):
            chat_id = call.message.chat.id
            # Extract the model name from the callback_data
            model_name = call.data.split(':', 1)[1]
            logging.info(f"User {chat_id} selected model via button: {model_name}")

            # Acknowledge the callback query to remove the loading state from the button
            _bot_instance.answer_callback_query(call.id, f"Setting model to {model_name}...")

            # Now, perform the same logic as setting the model
            # Ensure Supabase is available
            if not get_supabase_client():
                 _bot_instance.send_message(chat_id, "Database service is not available.")
                 logging.error(f"Callback model selection failed for {chat_id}: Supabase client not available.")
                 return

            user_settings = get_user_settings_from_db(chat_id)
            if user_settings is None:
                 _bot_instance.send_message(chat_id, "Error fetching your settings from the database.")
                 return

            if user_settings.get('selected_model') == model_name:
                 _bot_instance.send_message(chat_id, f"You are already using the model '{model_name}'.")
                 logging.info(f"User {chat_id} model selection: model already set.")
                 # Edit the message to remove the buttons even if already set
                 try:
                      _bot_instance.edit_message_text(f"Model is already '{model_name}'.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
                 except Exception as e:
                      logging.error(f"Failed to edit message after selecting existing model for {chat_id}: {e}")
                 return

            current_api_key = user_settings.get('gemini_api_key')
            # When setting a new model, reset message count as it's a fresh context for the new model
            if save_user_settings_to_db(chat_id, api_key=current_api_key, model_name=model_name, message_count=0): # Reset message count
                clear_history_in_db(chat_id)
                _bot_instance.send_message(chat_id, f"Model set to '{model_name}' successfully! Your chat history has been reset.")
                logging.info(f"User {chat_id} set model to {model_name} completed via callback.")
            else:
                _bot_instance.send_message(chat_id, "Failed to set the model in the database.")
                logging.error(f"Callback model selection failed for {chat_id}: Failed to save settings.")

            # Optional: Edit the original message to remove the buttons or update text
            try:
                 # Try to edit the message to show the result and remove buttons
                 _bot_instance.edit_message_text(f"Model set to '{model_name}'.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
            except Exception as e:
                 logging.error(f"Failed to edit message after setting model for {chat_id}: {e}")

        @_bot_instance.message_handler(commands=['current_settings'])
        def handle_current_settings(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} called /current_settings")
            if not get_supabase_client():
                 _bot_instance.reply_to(message, "Database service is not available.")
                 logging.error(f"/current_settings failed for {chat_id}: Supabase client not available.")
                 return

            user_settings = get_user_settings_from_db(chat_id)

            if user_settings is None:
                 _bot_instance.reply_to(message, "Error fetching your settings from the database.")
                 return

            api_key_status = "Using bot's default API key."
            if user_settings.get('gemini_api_key'):
                key_masked = user_settings['gemini_api_key']
                api_key_status = f"Using your custom API key: `{key_masked[:4]}...{key_masked[-4:]}`"
            else:
                 bot_api_key_set = os.getenv("BOT_API_KEY") is not None
                 default_client_possible = os.getenv("GEMINI_BOT_DEFAULT_API_KEY") is not None
                 if not bot_api_key_set and not default_client_possible:
                      api_key_status = "No API key available. Bot's default is missing, and you haven't set your own.\nPlease use /set_api_key to provide your key."

            current_model = user_settings.get('selected_model', DEFAULT_MODEL_NAME)
            current_message_count = user_settings.get('message_count', 0)

            settings_text = (
                "Your Current Settings:\n"
                f"- API Key: {api_key_status}\n"
                f"- Model: `{current_model}`\n"
                f"- Messages (Default Key): {current_message_count} / {DEFAULT_KEY_MESSAGE_LIMIT}" # Show count/limit
            )
            _bot_instance.reply_to(message, settings_text, parse_mode='Markdown')
            logging.info(f"User {chat_id} /current_settings completed.")


        # --- General Message Handlers ---

        # Helper function to check and process message flow for both text and photo
        # This function is called by the actual message handlers (handle_text_message, handle_photo_message)
        def check_and_process_message_flow(message, processing_logic_func):
            chat_id = message.chat.id
            # --- Check for interactive API key input state (handled before limit check) ---
            # This check handles the response *after* the /set_api_key command
            if user_temp_state.get(chat_id, {}).get('awaiting_api_key'):
                api_key = message.text.strip() if hasattr(message, 'text') and message.text else '' # Get text for key
                # Clear the awaiting state immediately from the in-memory dictionary
                user_temp_state.pop(chat_id, None)
                logging.info(f"Processing interactive API key input for {chat_id}")

                if not api_key:
                    _bot_instance.reply_to(message, "API key cannot be empty.")
                    return # Stop processing

                try:
                    logging.info(f"Attempting to validate API key for {chat_id}...")
                    start_time = time.time()
                    # Create a temporary client to validate the key
                    temp_client = genai.Client(api_key=api_key)
                    validate_time = time.time() - start_time
                    logging.info(f"GenAI client validation instance created in {validate_time:.4f} seconds for {chat_id}.")

                    # *** Add a small test call after client creation ***
                    logging.info(f"Attempting small test call (list models) for key starting with {api_key[:4]}...")
                    test_start_time = time.time()
                    try: # Add try-except around test call as it can fail
                         test_models = temp_client.models.list() # Use the models.list() method to test connectivity and validity
                         test_time = time.time() - test_start_time
                         logging.info(f"Small GenAI test call completed successfully in {test_time:.4f} seconds. Found {len(test_models)} models.")
                     # --- Catch specific validation errors for the test call ---
                    except google.api_core.exceptions.PermissionDenied as pd_e:
                         logging.error(f"Permission denied during test call for key starting with {api_key[:4]}...: {pd_e}")
                         logging.exception("PermissionDenied traceback:")
                         _bot_instance.reply_to(message, "Failed to validate API key: Permission Denied. Check if the key is correct and enabled for Gemini.", parse_mode='Markdown')
                         return # Stop processing on validation failure
                    except Exception as test_e:
                         logging.error(f"Failed during small GenAI test call for key starting with {api_key[:4]}...: {test_e}")
                         logging.exception("Small test call traceback:")
                         _bot_instance.reply_to(message, f"Failed to validate API key: Could not connect to AI service. Error: {test_e}", parse_mode='Markdown')
                         return # Stop processing on validation failure


                    # Ensure Supabase is available before trying to save
                    if not get_supabase_client():
                         _bot_instance.reply_to(message, "Database service is not available.")
                         logging.error(f"Interactive API key save failed for {chat_id}: Supabase client not available.")
                         return # Stop processing

                    user_settings = get_user_settings_from_db(chat_id) # Fetch again to get the latest state
                    if user_settings is None:
                         _bot_instance.reply_to(message, "Error fetching your settings before saving key.")
                         return # Stop processing

                    # Save the validated key to the database. Reset message count as they now have a custom key.
                    # Use the latest selected model from DB
                    if save_user_settings_to_db(chat_id, api_key=api_key, model_name=user_settings.get('selected_model', DEFAULT_MODEL_NAME), message_count=0): # Reset count
                         # Clear history since the underlying API key/session changes
                         clear_history_in_db(chat_id)
                         _bot_instance.reply_to(message, "Your Gemini API key has been set successfully! Your chat history has been reset.")
                         logging.info(f"User {chat_id} set a custom API key via interactive input. Completed.")
                    else:
                         _bot_instance.reply_to(message, "Failed to save your API key to the database.")
                         logging.error(f"Interactive API key save failed for {chat_id}: Failed to save to DB.")

                except Exception as e: # Catch errors during client creation itself
                    logging.error(f"User {chat_id} provided invalid API key via interactive input: {e}")
                    logging.exception("Interactive API key validation/save traceback:")
                    # This catch is for errors during genai.Client(api_key=api_key)
                    _bot_instance.reply_to(message, f"Failed to set API key: Could not initialize AI client. Check your key. Error: {e}\n\nTry /set_api_key again or /cancel.", parse_mode='Markdown')
                return # Stop processing this message (it was handled as API key input)

            # --- If NOT awaiting API key, proceed with limit check and message processing ---

            # Ensure Supabase is available for message count check and history
            if not get_supabase_client():
                 _bot_instance.reply_to(message, "Database service is not available. Cannot process message.")
                 logging.error(f"Message processing failed for {chat_id}: Supabase client not available.")
                 return

            user_settings = get_user_settings_from_db(chat_id) # Fetch settings for limit check
            if user_settings is None:
                 _bot_instance.reply_to(message, "Error fetching your settings from the database.")
                 logging.error(f"Message processing failed for {chat_id}: Error fetching settings.")
                 return

            # --- Check Message Count Limit (Only if using default key) ---
            allowed_to_proceed = True
            limit_message = None

            if user_settings.get('gemini_api_key') is None: # User is using default key
                 current_count = user_settings.get('message_count', 0)

                 # Check if limit is reached (This message will be blocked)
                 if current_count >= DEFAULT_KEY_MESSAGE_LIMIT:
                      allowed_to_proceed = False
                      limit_message = f"You have reached the {DEFAULT_KEY_MESSAGE_LIMIT}-message limit for users without a custom API key.\n\nPlease set your own API key using /set_api_key to continue chatting without limits."
                 else:
                      # User is within limit, increment the count for this message
                      # We need to refetch latest settings to get the most accurate count
                      latest_settings = get_user_settings_from_db(chat_id)
                      if latest_settings:
                           # Use the latest count + 1 for saving
                           count_to_save = latest_settings.get('message_count', 0) + 1
                           logging.info(f"Attempting to increment message count for {chat_id} to {count_to_save}.")

                           # Save updated count. This handles both insert and update.
                           if save_user_settings_to_db(chat_id, api_key=None, model_name=latest_settings.get('selected_model', DEFAULT_MODEL_NAME), message_count=count_to_save):
                               logging.info(f"Message count incremented and saved for {chat_id}.")
                               # Update the user_settings dict for potential use later in this handler (optional)
                               user_settings['message_count'] = count_to_save # Update in memory for this request
                               allowed_to_proceed = True # Allowed, count updated

                               # --- Notify user when they are approaching or at the limit ---
                               if DEFAULT_KEY_MESSAGE_LIMIT > 0: # Avoid division by zero
                                   messages_remaining = DEFAULT_KEY_MESSAGE_LIMIT - count_to_save
                                   # Notify when 1 message remaining
                                   if messages_remaining == 1:
                                        warning_message = f"You have 1 message remaining with the default API key.\n\nTo send more messages after this one, please use /set_api_key to provide your own Gemini API key."
                                        try:
                                             _bot_instance.send_message(chat_id, warning_message, parse_mode='Markdown')
                                             logging.info(f"Sent limit warning: 1 message remaining for {chat_id}.")
                                        except Exception as send_warn_e:
                                             logging.error(f"Failed to send limit warning message to {chat_id}: {send_warn_e}")
                                   # Notify on the LAST message (when messages_remaining is 0)
                                   elif messages_remaining == 0 and DEFAULT_KEY_MESSAGE_LIMIT > 0:
                                        final_warning_message = f"This is your {DEFAULT_KEY_MESSAGE_LIMIT}th and final message using the default API key.\n\nTo send more messages, please use /set_api_key to provide your own Gemini API key."
                                        try:
                                             _bot_instance.send_message(chat_id, final_warning_message, parse_mode='Markdown')
                                             logging.info(f"Sent final limit warning message to {chat_id}.")
                                        except Exception as send_warn_e:
                                             logging.error(f"Failed to send final limit warning message to {chat_id}: {send_warn_e}")


                           else:
                                logging.error(f"Failed to save updated message count for {chat_id}.")
                                allowed_to_proceed = False # Treat as not allowed due to save error
                                limit_message = "Error saving message count. Please try again."
                      else:
                           logging.error(f"Failed to refetch settings to update message count for {chat_id}.")
                           allowed_to_proceed = False
                           limit_message = "Error updating message count."


            if not allowed_to_proceed:
                 _bot_instance.reply_to(message, limit_message, parse_mode='Markdown')
                 logging.info(f"User {chat_id} hit default key message limit or encountered count update error.")
                 return # Stop processing this message

            # --- If allowed, proceed with the main processing logic ---
            # Call the specific processing function (text or photo)
            processing_logic_func(message, user_settings) # Pass message and user_settings


        # --- Update Handlers to use the new flow control ---
        # These handlers are now just entry points to the flow control helper
        @_bot_instance.message_handler(func=lambda message: message.text and not message.text.startswith('/'), content_types=['text'])
        def handle_text_message(message):
            # Delegate text message handling to the flow control function
            check_and_process_message_flow(message, process_text_message)

        @_bot_instance.message_handler(content_types=['photo'])
        def handle_photo_message(message):
            # Delegate photo message handling to the flow control function
            check_and_process_message_flow(message, process_photo_message)


        # Define the actual processing logic for text messages (called by check_and_process_message_flow)
        def process_text_message(message, user_settings):
             chat_id = message.chat.id
             user_message = message.text
             logging.info(f"Processing text message after flow check for {chat_id}: {user_message}")

             try:
                 client_for_user = get_user_client(user_settings)
                 if client_for_user is None:
                     error_msg = "AI service not available. The bot's default API key is missing or invalid, and you haven't set your own.\n\nPlease use /set_api_key to provide your key."
                     _bot_instance.reply_to(message, error_msg, parse_mode='Markdown')
                     logging.error(f"Text message processing failed for {chat_id}: AI client not available.")
                     return

                 model_for_user = user_settings.get('selected_model', DEFAULT_MODEL_NAME)
                 logging.info(f"User {chat_id} using model: {model_for_user}")

                 # --- Conversation handling with chat object ---
                 # Fetch history content from DB
                 logging.info(f"Fetching history from DB for {chat_id}...")
                 history_content = get_history_from_db(chat_id)
                 if history_content is None:
                     _bot_instance.reply_to(message, "Error fetching chat history from the database.")
                     logging.error(f"Text message processing failed for {chat_id}: Error fetching history.")
                     return

                 logging.info(f"Creating chat object with initial history length {len(history_content)}")
                 chat_start_time = time.time()
                 try:
                     # Use the correct method: client.chats.create accepts history
                     chat = client_for_user.chats.create(
                         model=model_for_user,
                         history=history_content # Pass the fetched history here
                     )
                     chat_time = time.time() - chat_start_time
                     logging.info(f"Chat object created in {chat_time:.4f} seconds.")
                 except google.api_core.exceptions.NotFound as nf_e:
                      logging.error(f"Model not found/supported for chat creation: {nf_e}")
                      logging.exception("Chat create NotFound traceback:")
                      _bot_instance.reply_to(message, f"The selected model '{model_for_user}' is not available or supported for conversations with your API key.\n\nPlease use /select_model to choose a different model.", parse_mode='Markdown')
                      return # Stop processing if model is invalid
                 except Exception as create_e:
                      logging.error(f"Error during chat creation for {chat_id}: {create_e}")
                      logging.exception("Chat create traceback:")
                      _bot_instance.reply_to(message, f"An error occurred while starting the conversation: {create_e}")
                      return # Stop processing on other chat create errors


                 # Send the current message using the chat object's send_message method
                 current_user_input_for_send = user_message # For text messages, just the string

                 logging.info(f"Calling chat.send_message for {chat_id}...")
                 send_msg_start_time = time.time()
                 try:
                     response = chat.send_message(current_user_input_for_send)
                     send_msg_time = time.time() - send_msg_start_time
                     logging.info(f"chat.send_message completed in {send_msg_time:.4f} seconds.")
                 except google.api_core.exceptions.NotFound as nf_e:
                      logging.error(f"Model not found/supported during send_message: {nf_e}")
                      logging.exception("Send message NotFound traceback:")
                      _bot_instance.reply_to(message, f"The selected model '{model_for_user}' is not available or supported for conversations with your API key.\n\nPlease use /select_model to choose a different model.", parse_mode='Markdown')
                      return # Stop processing if model is invalid
                 # --- Catch ResourceExhausted specifically ---
                 except google.api_core.exceptions.ResourceExhausted as re_e:
                      logging.error(f"Resource Exhausted error for {chat_id}: {re_e}")
                      logging.exception("ResourceExhausted traceback:")
                      # Craft a user-friendly message with the link(s)
                      error_message = f"Your request to the AI model failed due to a quota limit being reached for the selected model ('{model_for_user}')."
                      links_text = ""
                      if re_e.details:
                           try:
                                # Parse details to find help links
                                for detail in re_e.details:
                                     # Check if the detail is a dictionary and has a 'links' key
                                     if isinstance(detail, dict) and detail.get('links'):
                                          for link in detail['links']:
                                               if isinstance(link, dict) and link.get('description') and link.get('url'):
                                                    # Format as Telegram markdown link
                                                    links_text += f"\n\n[{link['description']}]({link['url']})"
                           except Exception as link_parse_e:
                                logging.error(f"Failed to parse help links from ResourceExhausted error details: {link_parse_e}")
                                # Fallback to a generic link if parsing fails
                                links_text = "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits"

                      error_message += links_text
                      _bot_instance.reply_to(message, error_message, parse_mode='Markdown', disable_web_page_preview=True) # disable preview for clean links
                      return # Stop processing on ResourceExhausted

                 except Exception as send_e:
                      logging.error(f"Error during chat.send_message for {chat_id}: {send_e}")
                      logging.exception("chat.send_message traceback:")
                      _bot_instance.reply_to(message, f"An error occurred while sending your message to the AI model: {send_e}")
                      return # Stop processing on other send errors


                 # --- Save the new turns from the chat object's history to DB ---
                 # The chat object's history property is updated after send_message
                 updated_history = chat.get_history()

                 # Find the new turns (should be the last user turn and the last model turn)
                 original_history_length = len(history_content)
                 updated_history_length = len(updated_history)

                 if updated_history_length >= original_history_length + 2: # Expect original + user + model
                     new_user_turn = updated_history[original_history_length] # The turn *after* the initial history
                     new_model_turn = updated_history[original_history_length + 1] # The turn after the new user turn
                     current_turn_index = original_history_length + 1 # Calculate index for the new user turn

                     logging.info(f"Saving new user and model turns to DB for {chat_id}. Indices {current_turn_index} and {current_turn_index + 1}")
                     # Save user turn (save only the text part to history for simplicity and context window limits)
                     save_turn_to_db(chat_id, current_turn_index, new_user_turn.role, [p for p in new_user_turn.parts if hasattr(p, 'text')])

                     # Save model turn
                     save_turn_to_db(chat_id, current_turn_index + 1, new_model_turn.role, new_model_turn.parts)

                 elif updated_history_length == original_history_length + 1:
                      logging.warning(f"Only user turn added to history for {chat_id}, model turn missing. History length: {updated_history_length}")
                      if updated_history_length > original_history_length and updated_history[original_history_length].role == 'user':
                           new_user_turn = updated_history[original_history_length]
                           current_turn_index = original_history_length + 1
                           save_turn_to_db(chat_id, current_turn_index, new_user_turn.role, [p for p in new_user_turn.parts if hasattr(p, 'text')])
                           logging.warning(f"Saved only the new user turn for {chat_id}.")
                      else:
                           logging.error(f"Unexpected state: History grew by 1 but last turn isn't user. Lengths: {original_history_length} -> {updated_history_length}")

                 else:
                     logging.warning(f"Unexpected history length after send_message for {chat_id}. Original: {original_history_length}, Updated: {updated_history_length}")


                 # --- Get response text and reply ---
                 model_response_text = ""
                 try:
                     model_response_text = response.text
                 except ValueError:
                      if response.prompt_feedback and response.prompt_feedback.block_reason:
                          block_reason_name = response.prompt_feedback.block_reason.name
                          model_response_text = f"Response blocked due to {block_reason_name}."
                          logging.warning(f"Response blocked for {chat_id}: {block_reason_name}")
                          # --- Add specific message for potential history limit ---
                          if "LENGTH" in block_reason_name.upper() or "CONTEXT" in block_reason_name.upper() or "TOO_LARGE" in block_reason_name.upper(): # Check if the reason name contains keywords related to length/context
                              model_response_text += "\n\nYour conversation history might be too long for the model. Try using /reset to start a new chat."
                          # --- End specific message ---

                      else:
                           model_response_text = "Could not get text response."
                           logging.warning(f"Could not get text response for {chat_id}.")
                 except Exception as access_text_e:
                     logging.error(f"Error accessing model response text for {chat_id}: {access_text_e}")
                     logging.exception("Accessing model response text traceback:")
                     model_response_text = "Error processing response."


                 logging.info(f"Replying to {chat_id}...")
                 try:
                      reply_start_time = time.time()
                      splitted_text = util.smart_split(model_response_text, chars_per_string=3000)
                      for text in splitted_text:
                            _bot_instance.reply_to(message, model_response_text)
                      reply_time = time.time() - reply_start_time
                      logging.info(f"Replied to {chat_id} in {reply_time:.4f} seconds.")
                 except Exception as reply_e:
                      logging.error(f"Error sending reply to {chat_id}: {reply_e}")
                      logging.exception("Reply sending traceback:")


             except Exception as e:
                 logging.error(f"Error processing text message for {chat_id}: {e}")
                 logging.exception("Text message processing traceback:")
                 error_message = "Sorry, I encountered an error processing your text request."
                 # Catch specific errors and refine message
                 if isinstance(e, google.api_core.exceptions.NotFound):
                      error_message = f"The selected model '{model_for_user}' is not available or supported. Please use /select_model to choose a different model."
                      try: _bot_instance.reply_to(message, error_message, parse_mode='Markdown') # Try to send specific error message
                      except Exception as reply_e: logging.error(f"Failed to send Not Found reply to {chat_id}: {reply_e}")
                      return # Stop processing handler
                 elif isinstance(e, google.api_core.exceptions.ResourceExhausted):
                      logging.error(f"Resource Exhausted error for {chat_id}: {e}")
                      logging.exception("ResourceExhausted traceback:")
                      # Craft a user-friendly message with the link(s)
                      error_message = f"Your request to the AI model failed due to a quota limit being reached for the selected model ('{model_for_user}')."
                      links_text = ""
                      if e.details:
                           try:
                                for detail in e.details:
                                     if isinstance(detail, dict) and detail.get('links'):
                                          for link in detail['links']:
                                               if isinstance(link, dict) and link.get('description') and link.get('url'):
                                                    links_text += f"\n\n[{link['description']}]({link['url']})"
                           except Exception as link_parse_e:
                                logging.error(f"Failed to parse help links from ResourceExhausted error details: {link_parse_e}")
                                links_text = "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits" # Fallback

                      error_message += links_text
                      _bot_instance.reply_to(message, error_message, parse_mode='Markdown', disable_web_page_preview=True)
                      return

                 elif isinstance(e, genai.types.BlockedPromptException):
                      error_message = f"Your prompt was blocked: {e}"
                 elif isinstance(e, ValueError) and "AI service not available" in str(e):
                      error_message = "AI service not available. The bot's default API key is missing or invalid, and you haven't set your own.\n\nPlease use /set_api_key to provide your key."
                      _bot_instance.reply_to(message, error_message, parse_mode='Markdown')
                      return
                 elif hasattr(e, 'response') and e.response.prompt_feedback:
                      if response.prompt_feedback.block_reason:
                           block_reason_name = response.prompt_feedback.block_reason.name
                           error_message = f"Your prompt was blocked due to {block_reason_name}."
                           if "LENGTH" in block_reason_name.upper() or "CONTEXT" in block_reason_name.upper() or "TOO_LARGE" in block_reason_name.upper():
                               error_message += "\n\nYour conversation history might be too long for the model. Try using /reset to start a new chat."

                 try:
                     _bot_instance.reply_to(message, error_message)
                 except Exception as reply_e:
                     logging.error(f"Failed to send error reply to {chat_id}: {reply_e}")


        @_bot_instance.message_handler(content_types=['photo'])
        def handle_photo_message(message):
            # Delegate to check_and_process_message
            check_and_process_message_flow(message, process_photo_message)

        # Define the actual processing logic for photo messages (called by check_and_process_message_flow)
        def process_photo_message(message, user_settings):
            chat_id = message.chat.id
            user_caption = message.caption # Get caption from original message
            logging.info(f"Processing photo message after flow check for {chat_id} with caption: {user_caption}")

            try:
                client_for_user = get_user_client(user_settings)
                if client_for_user is None:
                    error_msg = "AI service not available. The bot's default API key is missing or invalid, and you haven't set your own.\n\nPlease use /set_api_key to provide your key."
                    _bot_instance.reply_to(message, error_msg, parse_mode='Markdown')
                    logging.error(f"Photo message processing failed for {chat_id}: AI client not available.")
                    return


                model_for_user = user_settings.get('selected_model', DEFAULT_MODEL_NAME)
                logging.info(f"User {chat_id} using model: {model_for_user}")

                # --- Conversation handling with chat object ---
                # Fetch history content from DB
                logging.info(f"Fetching history from DB for {chat_id}...")
                history_content = get_history_from_db(chat_id)
                if history_content is None:
                    _bot_instance.reply_to(message, "Error fetching chat history from the database.")
                    logging.error(f"Photo message processing failed for {chat_id}: Error fetching history.")
                    return

                logging.info(f"Creating chat object with initial history length {len(history_content)}")
                chat_start_time = time.time()
                try:
                     chat = client_for_user.chats.create(
                         model=model_for_user,
                         history=history_content # Pass the fetched history here
                     )
                     chat_time = time.time() - chat_start_time
                     logging.info(f"Chat object created in {chat_time:.4f} seconds.")
                except google.api_core.exceptions.NotFound as nf_e:
                     logging.error(f"Model not found/supported for chat creation: {nf_e}")
                     logging.exception("Chat create NotFound traceback:")
                     _bot_instance.reply_to(message, f"The selected model '{model_for_user}' is not available or supported for conversations with your API key.\n\nPlease use /select_model to choose a different model.", parse_mode='Markdown')
                     return


                # Prepare current multimodal content for chat.send_message
                current_user_content_parts = []
                if user_caption:
                     current_user_content_parts.append(types.Part(text=user_caption))

                # Download the photo
                logging.info(f"Downloading photo file for {chat_id}...")
                download_start_time = time.time()
                file_id = message.photo[-1].file_id
                file_info = _bot_instance.get_file(file_id)
                downloaded_file = _bot_instance.download_file(file_info.file_path)
                download_time = time.time() - download_start_time
                logging.info(f"Downloaded photo file for {chat_id} in {download_time:.4f} seconds.")

                # Create the image part from downloaded bytes
                image_part = types.Part(
                    inline_data=types.Blob(
                        mime_type='image/jpeg', # Assuming jpeg from Telegram photos. Refine if needed.
                        data=downloaded_file # Raw bytes data
                    )
                )
                current_user_content_parts.append(image_part)


                logging.info(f"Calling chat.send_message (multimodal) for {chat_id}...")
                send_msg_start_time = time.time()
                try:
                    response = chat.send_message(current_user_content_parts) # send_message accepts list of Parts
                    send_msg_time = time.time() - send_msg_start_time
                    logging.info(f"chat.send_message completed in {send_msg_time:.4f} seconds.")
                except google.api_core.exceptions.NotFound as nf_e:
                      logging.error(f"Model not found/supported during send_message: {nf_e}")
                      logging.exception("Send message NotFound traceback:")
                      _bot_instance.reply_to(message, f"The selected model '{model_for_user}' is not available or supported for conversations with your API key.\n\nPlease use /select_model to choose a different model.", parse_mode='Markdown')
                      return
                # --- Catch ResourceExhausted specifically ---
                except google.api_core.exceptions.ResourceExhausted as re_e:
                      logging.error(f"Resource Exhausted error for {chat_id}: {re_e}")
                      logging.exception("ResourceExhausted traceback:")
                      # Craft a user-friendly message with the link(s)
                      error_message = f"Your request to the AI model failed due to a quota limit being reached for the selected model ('{model_for_user}')."
                      links_text = ""
                      if re_e.details:
                           try:
                                for detail in re_e.details:
                                     if isinstance(detail, dict) and detail.get('links'):
                                          for link in detail['links']:
                                               if isinstance(link, dict) and link.get('description') and link.get('url'):
                                                    links_text += f"\n\n[{link['description']}]({link['url']})"
                           except Exception as link_parse_e:
                                logging.error(f"Failed to parse help links from ResourceExhausted error details: {link_parse_e}")
                                links_text = "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits" # Fallback

                      error_message += links_text
                      _bot_instance.reply_to(message, error_message, parse_mode='Markdown', disable_web_page_preview=True)
                      return # Stop processing on ResourceExhausted

                except Exception as send_e:
                      logging.error(f"Error during chat.send_message for {chat_id}: {send_e}")
                      logging.exception("chat.send_message traceback:")
                      _bot_instance.reply_to(message, f"An error occurred while sending your message to the AI model: {send_e}")
                      return


                # --- Save the new turns from the chat object's history to DB ---
                updated_history = chat.get_history()
                original_history_length = len(history_content)
                updated_history_length = len(updated_history)

                if updated_history_length >= original_history_length + 2:
                    new_user_turn = updated_history[original_history_length]
                    new_model_turn = updated_history[original_history_length + 1]
                    current_turn_index = original_history_length + 1

                    logging.info(f"Saving new user and model turns to DB for {chat_id}. Indices {current_turn_index} and {current_turn_index + 1}")
                    # --- Save the *actual* user's multimodal input parts to DB ---
                    # Pass the original user message parts, including text (caption) and image.
                    # The caption needs to be added to the parts data dictionary before saving.
                    parts_to_save = []
                    for part in current_user_content_parts:
                         part_dict = {}
                         if hasattr(part, 'text') and part.text is not None:
                             part_dict['text'] = part.text
                             part_dict['type'] = 'text'
                         elif hasattr(part, 'inline_data') and part.inline_data:
                             part_dict['type'] = 'image'
                             part_dict['mime_type'] = part.inline_data.mime_type if hasattr(part.inline_data, 'mime_type') else 'unknown'
                             part_dict['size'] = len(part.inline_data.data) if hasattr(part.inline_data, 'data') and part.inline_data.data is not None else 0
                             # Add caption to the image part dictionary if it exists on the message object
                             if user_caption: # Use the caption from the message object
                                  part_dict['caption'] = user_caption

                         # Add other part types if needed (e.g., function_code, function_response)

                         if part_dict: parts_to_save.append(part_dict)

                    save_turn_to_db(chat_id, current_turn_index, new_user_turn.role, parts_to_save)


                    # Save model turn
                    save_turn_to_db(chat_id, current_turn_index + 1, new_model_turn.role, new_model_turn.parts)

                elif updated_history_length == original_history_length + 1:
                     logging.warning(f"Only user turn added to history for {chat_id}, model turn missing. History length: {updated_history_length}")
                     if updated_history_length > original_history_length and updated_history[original_history_length].role == 'user':
                          new_user_turn = updated_history[original_history_length]
                          current_turn_index = original_history_length + 1
                          # --- Save the *actual* user's multimodal input parts to DB ---
                          parts_to_save = []
                          for part in current_user_content_parts:
                               part_dict = {}
                               if hasattr(part, 'text') and part.text is not None:
                                   part_dict['text'] = part.text
                                   part_dict['type'] = 'text'
                               elif hasattr(part, 'inline_data') and part.inline_data:
                                   part_dict['type'] = 'image'
                                   part_dict['mime_type'] = part.inline_data.mime_type if hasattr(part.inline_data, 'mime_type') else 'unknown'
                                   part_dict['size'] = len(part.inline_data.data) if hasattr(part.inline_data, 'data') and part.inline_data.data is not None else 0
                                   if user_caption: part_dict['caption'] = user_caption
                               if part_dict: parts_to_save.append(part_dict)

                          save_turn_to_db(chat_id, current_turn_index, new_user_turn.role, parts_to_save)
                          logging.warning(f"Saved only the new user turn for {chat_id}.")
                     else:
                          logging.error(f"Unexpected state: History grew by 1 but last turn isn't user. Lengths: {original_history_length} -> {updated_history_length}")

                else:
                    logging.warning(f"Unexpected history length after send_message for {chat_id}. Original: {original_history_length}, Updated: {updated_history_length}")


                # --- Get response text and reply ---
                model_response_text = ""
                try:
                    model_response_text = response.text
                except ValueError:
                     if response.prompt_feedback and response.prompt_feedback.block_reason:
                         block_reason_name = response.prompt_feedback.block_reason.name
                         model_response_text = f"Response blocked due to {block_reason_name}."
                         logging.warning(f"Response blocked for {chat_id}: {block_reason_name}")
                         if "LENGTH" in block_reason_name.upper() or "CONTEXT" in block_reason_name.upper() or "TOO_LARGE" in block_reason_name.upper():
                             model_response_text += "\n\nYour conversation history might be too long for the model. Try using /reset to start a new chat."

                     else:
                          model_response_text = "Could not get text response."
                          logging.warning(f"Could not get text response for {chat_id}.")
                except Exception as access_text_e:
                    logging.error(f"Error accessing model response text for {chat_id}: {access_text_e}")
                    logging.exception("Accessing model response text traceback:")
                    model_response_text = "Error processing response."


                logging.info(f"Replying to {chat_id}...")
                try:
                     reply_start_time = time.time()
                     splitted_text = util.smart_split(model_response_text, chars_per_string=3000)
                     for text in splitted_text:
                            _bot_instance.reply_to(message, model_response_text)
                     reply_time = time.time() - reply_start_time
                     logging.info(f"Replied to {chat_id} in {reply_time:.4f} seconds.")
                except Exception as reply_e:
                     logging.error(f"Error sending reply to {chat_id}: {reply_e}")
                     logging.exception("Reply sending traceback:")


            except Exception as e:
                logging.error(f"Error processing photo message for {chat_id}: {e}")
                logging.exception("Photo message processing traceback:")
                error_message = "Sorry, I encountered an error processing the image."
                # Catch specific errors and refine message
                if isinstance(e, google.api_core.exceptions.NotFound):
                     error_message = f"The selected model '{model_for_user}' is not available or supported. Please use /select_model to choose a different model."
                     try: _bot_instance.reply_to(message, error_message, parse_mode='Markdown') # Try to send specific error message
                     except Exception as reply_e: logging.error(f"Failed to send Not Found reply to {chat_id}: {reply_e}")
                     return # Stop processing handler
                elif isinstance(e, google.api_core.exceptions.ResourceExhausted):
                      logging.error(f"Resource Exhausted error for {chat_id}: {e}")
                      logging.exception("ResourceExhausted traceback:")
                      # Craft a user-friendly message with the link(s)
                      error_message = f"Your request to the AI model failed due to a quota limit being reached for the selected model ('{model_for_user}')."
                      links_text = ""
                      if e.details:
                           try:
                                for detail in e.details:
                                     if isinstance(detail, dict) and detail.get('links'):
                                          for link in detail['links']:
                                               if isinstance(link, dict) and link.get('description') and link.get('url'):
                                                    links_text += f"\n\n[{link['description']}]({link['url']})"
                           except Exception as link_parse_e:
                                logging.error(f"Failed to parse help links from ResourceExhausted error details: {link_parse_e}")
                                links_text = "\n\nLearn more about Gemini API quotas: https://ai.google.dev/gemini-api/docs/rate-limits" # Fallback

                      error_message += links_text
                      _bot_instance.reply_to(message, error_message, parse_mode='Markdown', disable_web_page_preview=True)
                      return # Stop processing on ResourceExhausted

                elif isinstance(e, genai.types.BlockedPromptException):
                      error_message = f"Your prompt was blocked: {e}"
                elif isinstance(e, ValueError) and "AI service not available" in str(e):
                      error_message = "AI service not available. The bot's default API key is missing or invalid, and you haven't set your own.\n\nPlease use /set_api_key to provide your key."
                      _bot_instance.reply_to(message, error_message, parse_mode='Markdown')
                      return
                elif hasattr(e, 'response') and e.response.prompt_feedback:
                      if response.prompt_feedback.block_reason:
                           block_reason_name = response.prompt_feedback.block_reason.name
                           error_message = f"Your prompt was blocked due to {block_reason_name}."
                           if "LENGTH" in block_reason_name.upper() or "CONTEXT" in block_reason_name.upper() or "TOO_LARGE" in block_reason_name.upper():
                               error_message += "\n\nYour conversation history might be too long for the model. Try using /reset to start a new chat."

                try:
                     _bot_instance.reply_to(message, error_message)
                except Exception as reply_e:
                     logging.error(f"Failed to send error reply to {chat_id}: {reply_e}")


        @_bot_instance.message_handler(func=lambda message: message.text and message.text.startswith('/'))
        def handle_unknown_command(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} sent unknown command: {message.text}")
            _bot_instance.reply_to(message, "Unknown command. Use /help to see available commands.")


        @_bot_instance.message_handler(func=lambda m: True, content_types=['audio', 'document', 'animation', 'video', 'voice', 'contact', 'location', 'venue', 'game', 'invoice', 'successful_payment', 'sticker'])
        def handle_unsupported_content(message):
            chat_id = message.chat.id
            logging.info(f"User {chat_id} sent unsupported content type: {message.content_type}")
            _bot_instance.reply_to(message, "Sorry, I can currently only process text and photos.")


        return _bot_instance # Return the initialized bot instance

    else:
        # Return cached instance on subsequent calls (within the same process lifecycle)
        return _bot_instance

# --- User State Management (In-memory per Gunicorn worker process) ---
user_temp_state = {}