import os

try:
    from dotenv import load_dotenv

    env_path = load_dotenv(verbose=True, override=True)
    if env_path:
        print(f"INFO: Loaded environment variables from: {env_path}")
    else:
        print("DEBUG: No .env file found.")
except ImportError:
    print("DEBUG: python-dotenv not installed, skipping .env file load.")
    pass

# --- Env variables ---
BOT_MODE = os.getenv(
    "BOT_MODE", "webhook"
).lower()  # Default to webhook for safety in production
BOT_API_KEY = os.getenv("BOT_API_KEY")

# The bot's default operational key, loaded from the GOOGLE_API_KEY environment variable.
# This will be imported and used by other modules if a user-specific key isn't provided.
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# --- Model Configuration ---
DEFAULT_MODEL_NAME: str = os.getenv("DEFAULT_MODEL_NAME", "gemini-1.5-flash-latest")
VOICE_MODEL: str = "gemini-2.5-flash-preview-tts"
IMAGE_GENERATION_MODEL: str = "models/imagen-3.0-generate-002"

MAX_HISTORY_LENGTH_TURNS: int = int(os.getenv("MAX_HISTORY_LENGTH_TURNS", "20"))

# --- Usage Limits (if applicable) ---
# DEFAULT_KEY_MESSAGE_LIMIT: This limit applies if the bot is operating
# using the GOOGLE_API_KEY (via the GOOGLE_API_KEY Python variable from this config) as its default key.
# Set to 0 or negative for no limit.
DEFAULT_KEY_MESSAGE_LIMIT: int = int(os.getenv("DEFAULT_KEY_MESSAGE_LIMIT", "10"))

LOADING_ANIMATION_FILE_ID: str = os.getenv(
    "LOADING_ANIMATION_FILE_ID",
    "BAACAgQAAxkBAAIHpGgQtb7K66BEXtOAo4v3R9TBH1XRAALWGwACJbhpUF_ZfF4mEh3HNgQ",
)
# OpenWeatherMap API Key (Optional, for the weather tool feature)
OPEN_WEATHER_API_KEY = os.getenv("OPEN_WEATHER_API_KEY")
