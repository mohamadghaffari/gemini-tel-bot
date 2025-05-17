import logging
from time import time
from typing import Any
from google import genai
from .custom_types import UserSettings
from .config import GEMINI_BOT_DEFAULT_API_KEY
from google.api_core.exceptions import PermissionDenied

logger = logging.getLogger(__name__)

COMMON_MODELS_TO_SHOW = [
    "gemini-2.5-flash-preview-04-17",
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.0-flash",
    "gemini-2.0-flash-preview-image-generation",  # Image generation
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",  # Alias for gemini-1.5-flash
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
    "gemini-1.5-pro-latest",  # Alias for gemini-1.5-pro
    # "imagen-3.0-generate-002", # Image generation
    "veo-2.0-generate-001",  # Video generation
    # "gemini-2.0-flash-live-001", # Primarily voice/video, but often supports text
    # Older, but still common chat models:
    "gemini-1.0-pro",
    "gemini-pro",  # Alias for gemini-1.0-pro
]

_cached_genai_clients: dict[str, genai.Client] = {}


def get_user_client(api_key_to_use: str | None) -> genai.Client | None:
    """Gets or initializes the GenAI client instance for a given API key (cached)."""
    if api_key_to_use is None:
        logger.warning("Attempted to get AI client with None API key.")
        return None

    if api_key_to_use in _cached_genai_clients:
        logger.debug(
            f"Using cached GenAI client for key starting with {api_key_to_use[:4]}..."
        )
        return _cached_genai_clients[api_key_to_use]

    start_time = time()
    try:
        logger.info(
            f"Creating new GenAI client instance for key starting with {api_key_to_use[:4]}..."
        )
        client = genai.Client(api_key=api_key_to_use)
        _cached_genai_clients[api_key_to_use] = client
        client_time = time() - start_time
        logger.info(
            f"Created new GenAI client instance (cached) in {client_time:.4f} seconds."
        )
        return client
    except Exception as e:
        client_time = time() - start_time
        logger.error(
            f"Failed to create GenAI client for key starting with {api_key_to_use[:4]}... after {client_time:.4f} seconds: {e}",
            exc_info=True,
        )
        return None


def fetch_available_models_for_user(
    user_settings: UserSettings,
) -> list[dict[str, Any]] | None:
    """Fetches and filters available generative models for a user's API key."""
    logger.info("Fetching available models...")
    start_time = time()
    try:
        api_key_to_use = user_settings.get("gemini_api_key")
        if not api_key_to_use:
            api_key_to_use = GEMINI_BOT_DEFAULT_API_KEY

        if not api_key_to_use:
            logger.warning("Cannot list models: No valid API key available for user.")
            return None

        client_for_user = get_user_client(api_key_to_use)
        if client_for_user is None:
            logger.warning(
                "Cannot list models: Failed to initialize client for user's key."
            )
            return None

        logger.info("Calling client_for_user.models.list()...")
        list_start_time = time()
        models_list_raw = list(client_for_user.models.list())
        list_time = time() - list_start_time
        logger.info(
            f"client_for_user.models.list() completed in {list_time:.4f} seconds. Found {len(models_list_raw)} raw models."
        )

        generative_models_info: list[dict[str, Any]] = []
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

                model_info = {"name": model_name}
                model_info["description"] = description
                model_info["input_token_limit"] = getattr(m, "input_token_limit", None)
                model_info["output_token_limit"] = getattr(
                    m, "output_token_limit", None
                )
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
                else:
                    model_info["supported_actions"] = []

                generative_models_info.append(model_info)

        generative_models_info.sort(key=lambda x: x.get("name", ""))

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
