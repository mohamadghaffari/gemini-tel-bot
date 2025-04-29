import os
from dotenv import load_dotenv

env_path = load_dotenv(verbose=True, override=False)
if env_path:
    print(f"Loaded environment variables from:{env_path}")
else:
    print("No .env file found or python-dotenv not installed.")

# --- Env variables ---
BOT_MODE = os.getenv(
    "BOT_MODE", "webhook"
).lower()  # Default to webhook for safety in production
BOT_API_KEY = os.getenv("BOT_API_KEY")
GEMINI_BOT_DEFAULT_API_KEY = os.getenv("GEMINI_BOT_DEFAULT_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# --- Model Configuration ---
DEFAULT_MODEL_NAME: str = "models/gemini-1.5-flash-latest"
MAX_HISTORY_LENGTH_TURNS: int = 20

# --- Usage Limits (if applicable) ---
DEFAULT_KEY_MESSAGE_LIMIT: int = 5

LOADING_ANIMATION_FILE_ID: str = (
    "BAACAgQAAxkBAAIHpGgQtb7K66BEXtOAo4v3R9TBH1XRAALWGwACJbhpUF_ZfF4mEh3HNgQ"
)