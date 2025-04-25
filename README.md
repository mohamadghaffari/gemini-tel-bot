# 🤖 Gemini Telegram Bot

A Telegram bot built with Python that allows users to chat with Google Gemini models, supports multimodal input (text and photos), maintains conversation history, and provides user-specific configuration options via commands and buttons.

## ✨ Features

*   **🤖 AI Chat:** Interact with Google Gemini models (`gemini-1.5-flash-latest` by default or user-selected).
*   **📝📸 Multimodal Input:** Send text messages or photos with captions to the bot.
*   **💾 Conversation History:** The bot remembers previous turns (text and photo captions/placeholders) to maintain context in the conversation (history length is limited by model context window and a soft limit in the code).
*   **🔑 User API Keys:** Users can set and use their own Google Gemini API key using `/set_api_key` for potentially higher limits or access to models available to their key.
*   **🌍 Bot's Default Key:** A default API key can be configured for users who don't provide their own (limited to a certain number of messages).
*   **⚙️ Model Selection:** Users can easily select a different available Gemini model using the `/select_model` command and inline keyboard buttons.
*   **📊 Current Settings:** View active API key status, chosen model, and default key message count using `/current_settings`.
*   **↩️ Chat Reset:** Clear conversation history and reset settings to default using `/reset`.

## 📝 Important Notes & Context

*   **🧪 Not Production Ready:** This project is primarily for testing and demonstration purposes, focusing on the integration of the Google Gemini API (`google-genai` SDK), a database (Supabase), and Telegram (`pyTelegramBotAPI`) in a webhooks-based deployment environment (Railway). It may lack production-level features, robustness, security considerations (beyond basic API key handling), and comprehensive error handling for all edge cases.
*   **💡 Project Origin:** This bot was initially developed as a practical exercise and demonstration following the **GDG Berlin "Using AI to help solve problems, both big and small!"** event, aiming to explore and give Google Gemini's coding capabilities a try through iterative development and debugging.
*   **Limited Multimodal History:** While the bot accepts photos and saves a representation of the image part and caption to history, the AI model (due to current API capabilities and the stateless webhook architecture) might not be able to "re-see" the image content from previous turns. Conversation context from image turns will primarily be based on the saved caption and an `[Image: ...]` text placeholder.
*   **Free Tier Limitations:** Deployment on free tiers (like Railway's free tier and Supabase's free tier) comes with limitations (usage credits, database size, cold starts, potentially limited concurrency) that can affect performance and reliability under heavy load.

## 🚀 Deployment on Railway

This bot is designed to be deployed on a PaaS (Platform as a Service) like Railway using Gunicorn to serve the webhook endpoint.
You can use any PaaS service as there are not much Railyway configs in the project.

1.  **Prerequisites:**
    *   A Git repository hosting your code.
    *   A [Railway](https://railway.app/) account.
    *   A [Supabase](https://supabase.com/) account.
    *   A [Telegram Bot Token](https://core.telegram.org/bots#6-botfather).
    *   A [Google AI Studio / Google Cloud Project](https://aistudio.google.com/app/apikey) to get a Gemini API Key.

2.  **Supabase Database Setup:**
    *   In your Supabase project dashboard, go to the "Table editor".
    *   **Manually create the `user_settings` table:**
        *   `chat_id`: `BIGINT` (Set as Primary Key)
        *   `gemini_api_key`: `TEXT` (Allow NULLs)
        *   `selected_model`: `TEXT` (Set a Default Value of `'models/gemini-1.5-flash-latest'`)
        *   `message_count`: `INT` (Set a Default Value of `0`, Not Nullable)
    *   **Manually create the `chat_history` table:**
        *   `chat_id`: `BIGINT`
        *   `turn_index`: `INT`
        *   `role`: `TEXT`
        *   `parts_json`: `JSONB`
        *   Define a **composite Primary Key** on both `chat_id` and `turn_index`.
    *   **(Optional but Recommended):** Configure Row Level Security (RLS) on your tables if you plan to use a public `anon` Supabase key (though the code uses the `service_role` key by default, which bypasses RLS).

3.  **Railway Project Setup:**
    *   Log in to Railway and create a new project.
    *   Link your project to your Git repository.
    *   Railway will detect the `Procfile` and `requirements.txt`.

4.  **Configure Environment Variables on Railway:**
    *   In your Railway project's "Variables" tab, add the following environment variables exactly as named:
        *   `BOT_API_KEY`: Your Telegram bot token obtained from @BotFather.
        *   `SUPABASE_URL`: Your Supabase Project URL (found in Supabase project settings -> API).
        *   `SUPABASE_KEY`: Your Supabase **Service Role Key** (found in Supabase project settings -> API -> Project API keys. **Use the `service_role` key, not the `anon` key**, as the bot is a trusted backend).
        *   `GEMINI_BOT_DEFAULT_API_KEY`: (Optional) A Google Gemini API key to use for users who don't set their own. If not provided, the bot will only work for users who use `/set_api_key`.

5.  **Code Structure:**
    Ensure your project files are organized correctly in your Git repository:
    ```
    your-repo-root/
    ├── .env               # For local testing (optional for Railway)
    ├── requirements.txt   # Generated by `pip freeze`
    ├── Procfile           # `web: gunicorn api.webhook:app --bind 0.0.0.0:$PORT`
    ├── bot.py             # Your main bot logic file
    └── api/               # Directory for the entry point
        └── webhook.py     # Minimal WSGI entry point (from previous steps)
    ```

6.  **Push Code & Deploy:**
    *   Ensure your `requirements.txt` includes `gunicorn`, `supabase`, and pins the desired `google-genai` version (e.g., `google-genai==1.11.0`), generated via `pip freeze` locally after installing the exact version.
    *   Ensure `bot.py` contains the latest code from our conversation.
    *   Ensure `api/webhook.py` contains the WSGI `app` definition.
    *   Commit and push all your code changes to your Git repository.
    *   Railway should automatically build and deploy the service. Monitor the build logs for errors.

7.  **Set Telegram Webhook:**
    *   Once the Railway service is successfully deployed, get its public domain URL from the Railway dashboard.
    *   Tell Telegram to send updates to your service. Open a browser or use `curl` to make a GET request to the Telegram Bot API:
        ```
        https://api.telegram.org/bot<YOUR_BOT_API_KEY>/setWebhook?url=<YOUR_RAILWAY_SERVICE_URL>/api/webhook
        ```
        Replace `<YOUR_BOT_API_KEY>` and `<YOUR_RAILWAY_SERVICE_URL>`. You should see a JSON response indicating success.

8.  **Monitor Logs:** Use the logging interface in the Railway dashboard to monitor your service's logs in real-time for any errors or informational messages from your bot as you interact with it.

## 🎮 Usage

Start a chat with your bot on Telegram.

*   Send text messages or photos with captions to chat with the AI.
*   Use commands:
    *   `/start` or `/help`: Show welcome message and commands list.
    *   `/reset`: Clear your chat history and reset settings to default.
    *   `/set_api_key`: Begin the interactive process to set your personal Gemini API key.
    *   `/clear_api_key`: Stop using your custom key and revert to the bot's default key (if available).
    *   `/list_models`: List models available with your current API key.
    *   `/select_model`: Show inline buttons to select a model easily.
    *   `/current_settings`: Display your current API key status, selected model, and default key message count.

## 🐛 Debugging

*   **Railway Logs:** The primary tool for debugging deployment and runtime errors. Check them first.
*   **Telegram Webhook Info:** Use `https://api.telegram.org/bot<YOUR_BOT_API_KEY>/getWebhookInfo` to check if Telegram is successfully reaching your Railway URL and the pending update count.
*   **Database Issues:** If logs indicate database problems, verify your Supabase environment variables (`SUPABASE_URL`, `SUPABASE_KEY`) and ensure the `user_settings` and `chat_history` tables are created correctly in your Supabase dashboard.
*   **AI API Issues:** If logs show errors related to the Google GenAI API, verify your API key (`GEMINI_BOT_DEFAULT_API_KEY` or the user's key) and ensure the selected model is valid and supported for `generateContent`.