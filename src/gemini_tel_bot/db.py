import json
import logging
from time import time
from google.genai import types as genai_types
from supabase import create_client, Client
from .custom_types import UserSettings, HistoryTurn
from typing import Any
from supabase.lib.client_options import SyncClientOptions
from .config import (
    DEFAULT_MODEL_NAME,
    SUPABASE_URL,
    SUPABASE_KEY,
    MAX_HISTORY_LENGTH_TURNS,
)

logger = logging.getLogger(__name__)
_cached_supabase_client: Client | None = None


def get_supabase_client() -> Client | None:
    """Initializes and returns the Supabase client instance (cached).
    Assumes database tables are already created.
    """
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
                options=SyncClientOptions(postgrest_client_timeout=10),
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
            settings_data = response.data[0]
            settings: UserSettings = {
                "gemini_api_key": settings_data.get("gemini_api_key"),
                "selected_model": settings_data.get(
                    "selected_model", DEFAULT_MODEL_NAME
                ),
                "message_count": settings_data.get("message_count", 0),
            }
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
    except Exception as e:  # Could be PostgrestAPIError if table doesn't exist
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
        data_to_save: dict[str, Any] = {
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
        elif hasattr(response, "error") and response.error:
            logger.error(f"Supabase upsert error for {chat_id}: {response.error}")
            return False
        else:
            logger.warning(
                f"Supabase upsert operation returned no data for {chat_id} but no error. Response: {response}"
            )
            return True
    except Exception as e:  # Could be PostgrestAPIError if table doesn't exist
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
            .select("role, parts_json, turn_index")
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
            for row_idx, row in enumerate(response.data):
                role = row.get("role")
                parts_data_raw = row.get("parts_json")
                turn_index_from_db = row.get("turn_index", f"unknown_row_{row_idx}")
                parts_data_intermediate: list[dict[str, Any]] | None = None

                if isinstance(parts_data_raw, str):
                    try:
                        loaded_json = json.loads(parts_data_raw)
                        if isinstance(loaded_json, list) and all(
                            isinstance(item, dict) for item in loaded_json
                        ):
                            parts_data_intermediate = loaded_json
                        else:
                            logger.warning(
                                f"Decoded parts_json for chat {chat_id}, turn {turn_index_from_db} is not a list of dicts: {type(loaded_json)}"
                            )
                    except json.JSONDecodeError:
                        logger.error(
                            f"Failed to decode parts_json string for chat {chat_id}, turn {turn_index_from_db}."
                        )
                        continue
                elif isinstance(parts_data_raw, list) and all(
                    isinstance(item, dict) for item in parts_data_raw
                ):
                    parts_data_intermediate = parts_data_raw
                elif parts_data_raw is None:
                    logger.debug(
                        f"parts_json for chat {chat_id}, turn {turn_index_from_db} is None. Assuming empty parts."
                    )
                    parts_data_intermediate = []
                else:
                    logger.warning(
                        f"parts_json for chat {chat_id}, turn {turn_index_from_db} is of unexpected type or structure: {type(parts_data_raw)}. Skipping."
                    )
                    continue

                if role is not None and parts_data_intermediate is not None:
                    reconstructed_parts: list[genai_types.Part] = []
                    for p_dict in parts_data_intermediate:
                        part_type = p_dict.get("type")
                        if part_type == "text" and "text" in p_dict:
                            reconstructed_parts.append(
                                genai_types.Part(text=str(p_dict["text"]))
                            )
                        elif part_type == "image":
                            image_text = f"[Image: {p_dict.get('mime_type', 'image')}]"
                            caption = p_dict.get("caption")
                            if caption:
                                image_text += f" (Caption: {caption})"
                            reconstructed_parts.append(
                                genai_types.Part(text=image_text)
                            )
                        elif part_type == "function_call" and "function_call" in p_dict:
                            fc_data = p_dict["function_call"]
                            if isinstance(fc_data, dict):
                                reconstructed_parts.append(
                                    genai_types.Part(
                                        function_call=genai_types.FunctionCall(
                                            name=str(fc_data.get("name")),
                                            args=fc_data.get("args"),
                                        )
                                    )
                                )
                        elif (
                            part_type == "function_response"
                            and "function_response" in p_dict
                        ):
                            fr_data = p_dict["function_response"]
                            if isinstance(fr_data, dict):
                                reconstructed_parts.append(
                                    genai_types.Part(
                                        function_response=genai_types.FunctionResponse(
                                            name=str(fr_data.get("name")),
                                            response=fr_data.get("response"),
                                        )
                                    )
                                )
                    if role in ["user", "model"]:
                        history.append(
                            genai_types.Content(role=role, parts=reconstructed_parts)
                        )
                    else:
                        logger.warning(
                            f"Skipping history row for {chat_id}, turn {turn_index_from_db} with unsupported role '{role}'."
                        )
                elif role is None:
                    logger.warning(
                        f"Skipping turn for {chat_id}, turn_index {turn_index_from_db} due to missing role."
                    )

        if MAX_HISTORY_LENGTH_TURNS > 0 and len(history) > MAX_HISTORY_LENGTH_TURNS:
            logger.info(
                f"History length {len(history)} exceeds MAX_HISTORY_LENGTH_TURNS {MAX_HISTORY_LENGTH_TURNS} for {chat_id}. Truncating."
            )
            history = history[-MAX_HISTORY_LENGTH_TURNS:]
        return history
    except Exception as e:  # Could be PostgrestAPIError if table doesn't exist
        logger.error(
            f"Error fetching or reconstructing history for {chat_id} from Supabase: {e}",
            exc_info=True,
        )
        return None


def save_turn_to_db(
    chat_id: int,
    turn_index: int,
    role: str | None,
    parts: list[genai_types.Part] | None,
) -> bool:
    """Saves a user or model turn to chat_history using Supabase (UPSERTs)."""
    logger.info(f"Saving turn {turn_index} for {chat_id} ({role}) to Supabase...")
    supabase_client = get_supabase_client()
    if not supabase_client:
        logger.error("Cannot save turn, Supabase client not available.")
        return False

    start_time = time()
    parts_data_to_save: list[dict[str, Any]] = []

    if parts is not None:
        for part_object in parts:
            part_dict: dict[str, Any] = {}
            if part_object.text is not None:
                part_dict["text"] = part_object.text
                part_dict["type"] = "text"
            elif part_object.inline_data is not None:
                part_dict["type"] = "image"
                part_dict["mime_type"] = part_object.inline_data.mime_type
                part_dict["data_placeholder"] = (
                    f"Image data ({part_object.inline_data.mime_type})"
                )
            elif part_object.function_response is not None:
                part_dict["type"] = "function_response"
                part_dict["function_response"] = {
                    "name": part_object.function_response.name,
                    "response": part_object.function_response.response,
                }
            elif part_object.function_call is not None:
                part_dict["type"] = "function_call"
                part_dict["function_call"] = {
                    "name": part_object.function_call.name,
                    "args": part_object.function_call.args,
                }
            elif part_object.file_data is not None:
                part_dict["type"] = "file_data"
                part_dict["file_data"] = {
                    "mime_type": part_object.file_data.mime_type,
                    "file_uri": part_object.file_data.file_uri,
                }
            if part_dict:
                parts_data_to_save.append(part_dict)
    else:
        logger.warning(
            f"Turn {turn_index} for {chat_id} ({role}) has None for parts. Saving with empty parts_json."
        )

    try:
        data_to_save = {
            "chat_id": chat_id,
            "turn_index": turn_index,
            "role": role,
            "parts_json": json.dumps(parts_data_to_save),
        }
        response = supabase_client.table("chat_history").upsert(data_to_save).execute()
        end_time = time() - start_time
        logger.info(
            f"Saved turn {turn_index} for {chat_id} ({role}) to Supabase in {end_time:.4f} seconds."
        )

        if response.data:
            return True
        elif hasattr(response, "error") and response.error:
            logger.error(
                f"Supabase upsert error for turn {turn_index}, chat {chat_id}: {response.error}"
            )
            return False
        else:
            logger.warning(
                f"Supabase upsert for turn {turn_index}, chat {chat_id} returned no data but no error. Response: {response}"
            )
            return True
    except Exception as e:  # Could be PostgrestAPIError if table doesn't exist
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
        if hasattr(response, "error") and response.error:
            logger.error(f"Supabase delete error for chat {chat_id}: {response.error}")
            return False
        return True
    except Exception as e:  # Could be PostgrestAPIError if table doesn't exist
        logger.error(
            f"Error clearing history for {chat_id} in Supabase: {e}", exc_info=True
        )
        return False
