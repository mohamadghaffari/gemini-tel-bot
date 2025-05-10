import json
import logging
from time import time
from google.genai import types
from supabase import create_client, Client
from .custom_types import UserSettings, HistoryTurn
from supabase.lib.client_options import ClientOptions
from .config import (
    DEFAULT_MODEL_NAME,
    SUPABASE_URL,
    SUPABASE_KEY,
    MAX_HISTORY_LENGTH_TURNS,
)

logger = logging.getLogger(__name__)

_cached_supabase_client: Client | None = None


def get_supabase_client() -> Client | None:
    """Initializes and returns the Supabase client instance (cached)."""
    global _cached_supabase_client
    if _cached_supabase_client is None:
        logger.info("Initializing Supabase client...")
        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.critical(
                "SUPABASE_URL or SUPABASE_KEY environment variables not set."
            )
            return None

        try:
            start_time = time()
            _cached_supabase_client = create_client(
                SUPABASE_URL,
                SUPABASE_KEY,
                options=ClientOptions(postgrest_client_timeout=10),
            )
            init_time = time() - start_time
            logger.info(
                f"Supabase client initialized successfully in {init_time:.4f} seconds."
            )
        except Exception as e:
            logger.critical(f"Failed to initialize Supabase client: {e}", exc_info=True)
            _cached_supabase_client = None

    return _cached_supabase_client


def get_user_settings_from_db(chat_id: int) -> UserSettings | None:
    """Fetches user settings from the database using Supabase."""
    logger.info(f"Fetching settings for {chat_id} from Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logger.error("get_user_settings_from_db failed: Supabase client not available.")
        return None

    start_time = time()
    try:
        response = (
            supabase_client.table("user_settings")
            .select("gemini_api_key, selected_model, message_count")
            .eq("chat_id", chat_id)
            .execute()
        )
        end_time = time() - start_time
        logger.info(f"Fetched settings for {chat_id} in {end_time:.4f} seconds.")

        if response.data and len(response.data) > 0:
            settings = response.data[0]
            settings["message_count"] = settings.get("message_count", 0)
            settings["selected_model"] = settings.get(
                "selected_model", DEFAULT_MODEL_NAME
            )
            logger.debug(f"Fetched settings: {settings}")
            return settings
        else:
            logger.info(
                f"No settings found for {chat_id} in Supabase, returning defaults."
            )
            return {
                "gemini_api_key": None,
                "selected_model": DEFAULT_MODEL_NAME,
                "message_count": 0,
            }
    except Exception as e:
        logger.error(
            f"Error fetching settings for {chat_id} from Supabase: {e}", exc_info=True
        )
        return None


def save_user_settings_to_db(
    chat_id: int, api_key: str | None, model_name: str, message_count: int | None = None
) -> bool:
    """Saves or updates user settings in the database using Supabase."""
    logger.info(f"Saving settings for {chat_id} to Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logger.error("Cannot save settings, Supabase client not available.")
        return False

    start_time = time()
    try:
        data_to_save = {
            "chat_id": chat_id,
            "gemini_api_key": api_key,
            "selected_model": model_name,
        }
        if message_count is not None:
            data_to_save["message_count"] = message_count

        response = supabase_client.table("user_settings").upsert(data_to_save).execute()
        end_time = time() - start_time
        logger.info(
            f"Saved settings for {chat_id} to Supabase in {end_time:.4f} seconds."
        )

        if response.data:
            logger.debug(f"Supabase upsert response data: {response.data}")
            return True
        else:
            logger.error(
                f"Supabase upsert operation returned no data for {chat_id}. Response: {response}"
            )
            return False

    except Exception as e:
        logger.error(
            f"Error saving settings for {chat_id} to Supabase: {e}", exc_info=True
        )
        return False


def get_history_from_db(chat_id: int) -> list[HistoryTurn] | None:
    """Fetches chat history content for a user from Supabase."""
    logger.info(f"Fetching history for {chat_id} from Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logger.error("get_history_from_db failed: Supabase client not available.")
        return None

    start_time = time()
    try:
        response = (
            supabase_client.table("chat_history")
            .select("role, parts_json")
            .eq("chat_id", chat_id)
            .order("turn_index")
            .execute()
        )
        end_time = time() - start_time
        logger.info(
            f"Fetched history for {chat_id} in {end_time:.4f} seconds ({len(response.data or [])} rows)."
        )

        history: list[HistoryTurn] = []
        if response.data:
            for row in response.data:
                role = row.get("role")
                parts_data_raw = row.get("parts_json")
                parts_data: list | dict | None = None

                if isinstance(parts_data_raw, str):
                    try:
                        parts_data = json.loads(parts_data_raw)
                        logger.debug(
                            f"Manually loaded parts_json string for chat_id {chat_id}, turn {row.get('turn_index')}. Type was str."
                        )
                    except json.JSONDecodeError:
                        logger.error(
                            f"Failed to decode parts_json string for chat_id {chat_id}, turn {row.get('turn_index')}."
                        )
                        continue
                elif isinstance(parts_data_raw, (list, dict)):
                    parts_data = parts_data_raw
                    logger.debug(
                        f"parts_json for chat_id {chat_id}, turn {row.get('turn_index')} is already list/dict. Type: {type(parts_data_raw)}."
                    )
                elif parts_data_raw is None:
                    logger.debug(
                        f"parts_json for chat_id {chat_id}, turn {row.get('turn_index')} is None. Skipping row for history reconstruction."
                    )
                    continue
                else:
                    logger.warning(
                        f"parts_json for chat_id {chat_id}, turn {row.get('turn_index')} is neither string, list, dict, nor None. Type: {type(parts_data_raw)}. Skipping row."
                    )
                    continue

                # Proceed with reconstruction only if parts_data is now a list
                if role is not None and isinstance(parts_data, list):
                    parts_list: list[types.Part] = []
                    for p in parts_data:
                        if isinstance(p, dict):
                            part_type = p.get("type")
                            if part_type == "text" and p.get("text") is not None:
                                parts_list.append(types.Part(text=p["text"]))
                            elif part_type == "image":
                                image_text = f"[Image: {p.get('mime_type', 'image')}]"
                                caption = p.get("caption")
                                if caption:
                                    image_text += f" (Caption: {caption})"
                                parts_list.append(types.Part(text=image_text))
                            elif part_type == "function_call" and p.get(
                                "function_call"
                            ):
                                func_call_data = p["function_call"]
                                func_name = func_call_data.get("name", "unknown")
                                parts_list.append(
                                    types.Part(text=f"[Function Call: {func_name}]")
                                )
                            elif part_type == "function_response" and p.get(
                                "function_response"
                            ):
                                func_resp_data = p["function_response"]
                                func_name = func_resp_data.get("name", "unknown")
                                parts_list.append(
                                    types.Part(text=f"[Function Response: {func_name}]")
                                )

                    if parts_list:
                        if role in ["user", "model"]:
                            history.append(types.Content(role=role, parts=parts_list))
                            logger.debug(
                                f"Reconstructed turn for {chat_id}, role {role}, {len(parts_list)} parts."
                            )
                        else:
                            logger.warning(
                                f"Skipping history row for {chat_id} with unsupported role '{role}' during reconstruction."
                            )
                    else:
                        logger.debug(
                            f"Turn for {chat_id}, role {role}, turn_index {row.get('turn_index')} had empty parts_json or no savable parts."
                        )

                elif isinstance(parts_data, dict):
                    logger.warning(
                        f"parts_json for chat_id {chat_id}, turn {row.get('turn_index')} is a dictionary, not a list. Skipping row for history reconstruction."
                    )
                else:
                    logger.error(
                        f"Unexpected state after parts_json processing for chat_id {chat_id}, turn {row.get('turn_index')}. parts_data is {type(parts_data)}."
                    )

        if MAX_HISTORY_LENGTH_TURNS > 0 and len(history) > MAX_HISTORY_LENGTH_TURNS:
            logger.warning(
                f"History length {len(history)} exceeds MAX_HISTORY_LENGTH_TURNS {MAX_HISTORY_LENGTH_TURNS} for {chat_id}. Truncating history from {len(history)} to {MAX_HISTORY_LENGTH_TURNS} turns."
            )
            history = history[-MAX_HISTORY_LENGTH_TURNS:]
            logger.debug(
                f"Truncated history for {chat_id}. Final length: {len(history)}"
            )

        return history
    except Exception as e:
        logger.error(
            f"Error fetching or reconstructing history for {chat_id} from Supabase: {e}",
            exc_info=True,
        )
        return None


def save_turn_to_db(
    chat_id: int, turn_index: int, role: str, parts: list[types.Part]
) -> bool:
    """Saves a user or model turn with a specific turn_index to chat_history using Supabase (UPSERTs)."""
    logger.info(f"Saving turn {turn_index} for {chat_id} ({role}) to Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logger.error("Cannot save turn, Supabase client not available.")
        return False

    start_time = time()
    try:
        parts_data = []
        for part in parts:
            part_dict = {}
            if hasattr(part, "text") and part.text is not None:
                part_dict["text"] = part.text
                part_dict["type"] = "text"
            elif hasattr(part, "inline_data") and part.inline_data:
                part_dict["type"] = "image"
                part_dict["mime_type"] = (
                    part.inline_data.mime_type
                    if hasattr(part.inline_data, "mime_type")
                    else "unknown"
                )
                part_dict["size"] = (
                    len(part.inline_data.data)
                    if hasattr(part.inline_data, "data")
                    and part.inline_data.data is not None
                    else 0
                )
                if hasattr(part, "caption") and part.caption is not None:
                    part_dict["caption"] = part.caption

            elif (
                hasattr(part, "function_response")
                and part.function_response is not None
            ):
                part_dict["type"] = "function_response"
                part_dict["function_response"] = {
                    "name": getattr(part.function_response, "name", None),
                    "content": getattr(part.function_response, "content", None),
                }
            elif hasattr(part, "function_call") and part.function_call is not None:
                part_dict["type"] = "function_call"
                part_dict["function_call"] = {
                    "name": getattr(part.function_call, "name", None),
                    "args": getattr(part.function_call, "args", None),
                }

            if part_dict:
                parts_data.append(part_dict)

        if not parts_data:
            logger.warning(
                f"Turn {turn_index} for {chat_id} ({role}) has no savable parts. Saving with empty parts_json."
            )

        data_to_save = {
            "chat_id": chat_id,
            "turn_index": turn_index,
            "role": role,
            "parts_json": parts_data,
        }

        response = supabase_client.table("chat_history").upsert(data_to_save).execute()

        end_time = time() - start_time
        logger.info(
            f"Saved turn {turn_index} for {chat_id} ({role}) to Supabase in {end_time:.4f} seconds."
        )

        if response.data:
            return True
        else:
            logger.error(
                f"Supabase upsert operation returned no data for turn {turn_index} for {chat_id}. Response: {response}"
            )
            return False

    except Exception as e:
        logger.error(
            f"Error saving turn {turn_index} for {chat_id} ({role}) to Supabase: {e}",
            exc_info=True,
        )
        return False


def clear_history_in_db(chat_id: int) -> bool:
    """Clears chat history for a user in Supabase."""
    logger.info(f"Clearing history for {chat_id} in Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logger.error("Cannot clear history, Supabase client not available.")
        return False

    start_time = time()
    try:
        response = (
            supabase_client.table("chat_history")
            .delete()
            .eq("chat_id", chat_id)
            .execute()
        )
        end_time = time() - start_time
        logger.info(
            f"Cleared history for {chat_id} in Supabase in {end_time:.4f} seconds. Response data: {response.data}"
        )
        return True
    except Exception as e:
        logger.error(
            f"Error clearing history for {chat_id} in Supabase: {e}", exc_info=True
        )
        return False
