import logging
from time import time
from typing import Any

from google import genai
from google.api_core.exceptions import ClientError as APICoreClientError
from google.api_core.exceptions import PermissionDenied
from google.genai.pagers import AsyncPager
from google.genai.types import Model as GenAiModel

from .config import GOOGLE_API_KEY
from .custom_types import ModelInfo, UserSettings

logger = logging.getLogger(__name__)

COMMON_MODELS_TO_SHOW = [
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
    "gemini-1.0-pro",
    # "gemma-3-1b-it",
    # "gemma-3-4b-it",
    # "gemma-3-12b-it",
    # "gemma-3-27b-it",
    # "gemma-3n-e4b-it",
]

# Cache for genai.Client instances. Key is the API key string.
# None value means client creation failed for that key and shouldn't be retried immediately.
_cached_genai_clients: dict[str, genai.Client | None] = {}


def _create_genai_client(api_key: str) -> genai.Client | None:
    """Helper to create a genai.Client instance. Expects a non-empty api_key."""
    if not api_key:  # Should not happen if called correctly, but as a safeguard.
        logger.error("_create_genai_client called with an empty or None API key.")
        return None
    try:
        client = genai.Client(api_key=api_key)
        logger.info(
            f"Successfully created genai.Client with API key ending ...{api_key[-4:] if len(api_key) > 3 else '****'}"
        )
        return client
    except (PermissionDenied, APICoreClientError) as e:
        logger.error(
            f"Permission denied or client error creating genai.Client with key ...{api_key[-4:] if len(api_key) > 3 else '****'}: {e}"
        )
    except Exception as e:
        logger.error(
            f"Unexpected error creating genai.Client with key ...{api_key[-4:] if len(api_key) > 3 else '****'}: {e}",
            exc_info=True,
        )
    return None


def get_user_client(user_provided_api_key: str | None) -> genai.Client | None:
    """Gets or creates a genai.Client for the given API key (or default GOOGLE_API_KEY from config if None)."""

    key_for_client_operations: str | None
    log_key_source_description: str

    if user_provided_api_key is not None:
        key_for_client_operations = user_provided_api_key
        log_key_source_description = f"user-provided key ending ...{key_for_client_operations[-4:] if len(key_for_client_operations) > 3 else '****'}"
    else:
        key_for_client_operations = GOOGLE_API_KEY  # From config (which is from env)
        log_key_source_description = "bot's default GOOGLE_API_KEY from config"

    if not key_for_client_operations:
        logger.warning(
            f"get_user_client: No API key available ({log_key_source_description} resolved to None/empty)."
        )
        return None

    # Check cache using the actual key string that will be used for client creation
    if key_for_client_operations not in _cached_genai_clients:
        logger.info(
            f"get_user_client: No cached client for {log_key_source_description}. Attempting to create."
        )
        client_instance = _create_genai_client(key_for_client_operations)
        _cached_genai_clients[key_for_client_operations] = (
            client_instance  # Cache instance or None if creation failed
        )

    client = _cached_genai_clients.get(key_for_client_operations)
    if client is None:
        # This means it was cached as None (creation failed previously) or key_for_client_operations was somehow not set (should be caught above)
        logger.warning(
            f"get_user_client: Client for {log_key_source_description} is None (creation may have failed previously or key is invalid)."
        )
    return client


async def fetch_available_models_for_user(
    user_settings: UserSettings,
) -> list[ModelInfo] | None:
    """Fetches and filters available generative models for a user's API key asynchronously."""
    logger.info("Fetching available models asynchronously...")
    start_time = time()
    try:
        api_key_to_use = user_settings.get("gemini_api_key")
        if not api_key_to_use:
            api_key_to_use = GOOGLE_API_KEY

        if not api_key_to_use:
            logger.warning("Cannot list models: No valid API key available for user.")
            return None

        client_for_user = get_user_client(api_key_to_use)
        if client_for_user is None:
            logger.warning(
                "Cannot list models: Failed to initialize client for user's key."
            )
            return None

        logger.info("Calling client_for_user.aio.models.list()...")
        list_start_time = time()

        models_iterator: AsyncPager[GenAiModel] = (
            await client_for_user.aio.models.list()
        )
        models_list_raw: list[GenAiModel] = []
        async for model_obj in models_iterator:
            models_list_raw.append(model_obj)

        list_time = time() - list_start_time
        logger.info(
            f"client_for_user.aio.models.list() completed in {list_time:.4f} seconds. Found {len(models_list_raw)} raw models."
        )

        generative_models_info: list[ModelInfo] = []
        logger.debug("Filtering raw models:")
        for m in models_list_raw:
            model_name = getattr(
                m, "name", ""
            )  # Full name e.g., "models/gemini-1.5-pro-latest"
            description = getattr(m, "description", "")

            # Extract the base model name (e.g., "gemini-1.5-pro-latest")
            base_model_name = model_name.split("/")[-1]

            # Specific exclusions based on keywords in the full model name
            is_embedding = "embedding" in model_name.lower()
            is_aqa = "aqa" in model_name.lower()  # Attributed Question Answering models
            is_tuned = model_name.startswith("tunedModels/")  # User-tuned models

            # Check if the base model name is in our curated list and not an excluded type
            if (
                model_name
                and base_model_name in COMMON_MODELS_TO_SHOW
                and not is_embedding
                and not is_aqa
                and not is_tuned
            ):
                logger.debug(
                    f"  -> Keeping model from curated list: {model_name} (base: {base_model_name})"
                )

                model_info: ModelInfo = {
                    "name": model_name,
                    "description": description,
                    "input_token_limit": getattr(m, "input_token_limit", None),
                    "output_token_limit": getattr(m, "output_token_limit", None),
                    "supported_actions": [],  # Default to empty list
                }
                actions = getattr(m, "supported_actions", None)
                if actions:
                    try:
                        action_strings = [str(a) for a in actions if str(a)]
                        model_info["supported_actions"] = action_strings
                    except Exception as e:
                        logger.error(
                            f"Failed to convert supported_actions to strings for {model_name}: {e}"
                        )
                        model_info["supported_actions"] = ["<Error converting actions>"]

                generative_models_info.append(model_info)

        generative_models_info.sort(key=lambda x: x["name"])

        end_time = time() - start_time
        logger.info(
            f"Fetched, filtered, and sorted {len(generative_models_info)} available models in {end_time:.4f} seconds."
        )
        return generative_models_info

    except PermissionDenied as pd_e:
        logger.error(f"Permission denied when listing models: {pd_e}", exc_info=True)
        return None
    except Exception as e:
        end_time = time() - start_time
        logger.error(
            f"Error listing models after {end_time:.4f} seconds: {e}", exc_info=True
        )
        return None
