import logging
from time import time

from google import genai
from google.api_core.exceptions import ClientError as APICoreClientError
from google.api_core.exceptions import PermissionDenied
from google.genai.pagers import AsyncPager
from google.genai.types import Model as GenAiModel

from .config import DEFAULT_MODEL_NAME, GOOGLE_API_KEY
from .custom_types import ModelInfo, UserSettings

logger = logging.getLogger(__name__)

COMMON_MODELS_TO_SHOW = [
    "gemini-4-argon",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]

EXCLUDED_MODEL_SUBSTRINGS = (
    "embedding",
    "aqa",
    "tts",
    "image",
    "imagen",
    "transcribe",
    "robotics",
    "computer-use",
    "customtools",
    "live",
    "lyria",
    "veo",
    "vision",
)

RETIRED_MODEL_PREFIXES = (
    "gemini-1.0",
    "gemini-1.5",
    "gemini-2.0",
    "gemini-2.5",
    "gemini-pro-vision",
    "gemma-3-",
)


def normalize_or_fallback_model_name(model_name: str | None) -> str:
    """Normalizes a model name and falls back to DEFAULT_MODEL_NAME if retired."""
    if not model_name:
        return DEFAULT_MODEL_NAME
    base_name = model_name.replace("models/", "").strip()
    if not base_name or any(
        base_name.startswith(prefix) for prefix in RETIRED_MODEL_PREFIXES
    ):
        logger.info(
            f"Model '{model_name}' is retired or empty; falling back to '{DEFAULT_MODEL_NAME}'."
        )
        return DEFAULT_MODEL_NAME
    return model_name


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
            model_name = getattr(m, "name", "") or ""
            description = getattr(m, "description", "") or ""

            # Extract the base model name (e.g., "gemini-3.8-flash")
            base_model_name = model_name.split("/")[-1]
            lower_name = base_model_name.lower()

            is_tuned = model_name.startswith("tunedModels/")
            is_excluded = any(sub in lower_name for sub in EXCLUDED_MODEL_SUBSTRINGS)
            is_retired = any(
                base_model_name.startswith(prefix) for prefix in RETIRED_MODEL_PREFIXES
            )

            actions = getattr(m, "supported_actions", None)
            action_strings: list[str] = []
            if actions:
                try:
                    action_strings = [str(a) for a in actions if str(a)]
                except Exception as e:
                    logger.error(
                        f"Failed to convert supported_actions to strings for {model_name}: {e}"
                    )
                    action_strings = ["<Error converting actions>"]

            supports_generate = (
                not action_strings or "generateContent" in action_strings
            )
            is_gemini_or_gemma = lower_name.startswith(("gemini-", "gemma-"))

            if (
                model_name
                and not is_tuned
                and not is_excluded
                and (
                    base_model_name in COMMON_MODELS_TO_SHOW
                    or (is_gemini_or_gemma and not is_retired and supports_generate)
                )
            ):
                logger.debug(
                    f"  -> Keeping model: {model_name} (base: {base_model_name})"
                )

                model_info: ModelInfo = {
                    "name": model_name,
                    "description": description,
                    "input_token_limit": getattr(m, "input_token_limit", None),
                    "output_token_limit": getattr(m, "output_token_limit", None),
                    "supported_actions": action_strings,
                }
                generative_models_info.append(model_info)

        if not generative_models_info:
            raw_names = [getattr(m, "name", "") for m in models_list_raw]
            logger.warning(
                f"No models matched filter from {len(models_list_raw)} raw models ({raw_names[:10]}). Using fallback curated list."
            )
            for fallback_name in [
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.5-flash",
                "gemini-3.1-pro-preview",
                "gemini-3-flash-preview",
                "gemini-4-argon",
                "gemini-flash-latest",
                "gemini-pro-latest",
            ]:
                generative_models_info.append(
                    {
                        "name": f"models/{fallback_name}",
                        "description": f"Google {fallback_name}",
                        "input_token_limit": 1048576,
                        "output_token_limit": 65536,
                        "supported_actions": ["generateContent"],
                    }
                )

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
