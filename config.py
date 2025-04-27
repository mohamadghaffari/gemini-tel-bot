import os

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
    "CgACAgQAAxkBAAEW9c9oDYLeAvr4V20O1J2EbCjyomoqdAACfhoAAuQMcFBgfwXG6g6DFDYE"
)
