import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from telebot import types as telebot_types
from telebot.async_telebot import AsyncTeleBot

from .. import handlers
from ..bot import get_bot_instance

logger = logging.getLogger(__name__)

# Global bot instance, to be managed by the lifespan context
_global_bot_instance: AsyncTeleBot | None = None
_initialization_error: bool = False


def initialize_bot_for_fastapi() -> AsyncTeleBot | None:
    """
    Initializes the bot instance and registers handlers.
    This should be called once, e.g., during FastAPI startup.
    """
    local_bot_instance: AsyncTeleBot | None = None
    initialization_failed_locally = False

    logger.info(
        "Attempting to initialize bot instance for FastAPI worker (expecting AsyncTeleBot)..."
    )
    temp_bot_instance = get_bot_instance()

    if temp_bot_instance is None:
        logger.critical(
            "Failed to create bot instance (get_bot_instance returned None). "
            "FastAPI worker cannot process requests."
        )
        initialization_failed_locally = True
        return None

    if not isinstance(temp_bot_instance, AsyncTeleBot):
        logger.critical(
            f"FastAPI expected AsyncTeleBot but received {type(temp_bot_instance)}. "
            "Webhook functionality will likely fail or be incorrect."
        )
        initialization_failed_locally = True
        return None

    try:
        logger.info("Registering handlers for FastAPI worker (with AsyncTeleBot)...")
        handlers.register_handlers(temp_bot_instance)
        logger.info("FastAPI handlers registered successfully for AsyncTeleBot.")
        local_bot_instance = temp_bot_instance
    except Exception as e:
        logger.critical(
            f"Failed to register FastAPI handlers for AsyncTeleBot: {e}", exc_info=True
        )
        initialization_failed_locally = True
        local_bot_instance = None

    if initialization_failed_locally:
        return None
    return local_bot_instance


@asynccontextmanager
async def lifespan(
    app_instance: FastAPI,
) -> AsyncIterator[None]:  # app_instance is the FastAPI app
    """
    Context manager to handle application startup and shutdown events.
    """
    global _global_bot_instance, _initialization_error  # pylint: disable=global-statement
    logger.info("FastAPI application lifespan startup event triggered.")

    bot_instance_candidate = initialize_bot_for_fastapi()
    if bot_instance_candidate is None:
        logger.error(
            "Bot initialization FAILED during FastAPI startup. Webhook will not function."
        )
        _initialization_error = True
        _global_bot_instance = None
    else:
        logger.info(
            "Bot initialized successfully during FastAPI startup. Webhook is active."
        )
        _global_bot_instance = bot_instance_candidate
        _initialization_error = False

    yield  # Application runs here

    # Shutdown logic (if any) can go here
    logger.info("FastAPI application lifespan shutdown event triggered.")
    if _global_bot_instance:
        pass
    logger.info("FastAPI application shutdown complete.")


# Initialize FastAPI app with the lifespan manager
app = FastAPI(
    title="Gemini Telegram Bot Webhook",
    description="Handles incoming Telegram updates for the Gemini Telegram Bot.",
    version="0.1.1",
    lifespan=lifespan,
)


@app.post(
    "/api/webhook",
    summary="Telegram Webhook Endpoint",
    description="Receives updates from Telegram and processes them using AsyncTeleBot.",
    tags=["telegram"],
)
async def handle_telegram_webhook(request: Request) -> Response:
    """
    Handles incoming POST requests from Telegram.
    """
    global _global_bot_instance, _initialization_error  # pylint: disable=global-statement

    if _initialization_error or _global_bot_instance is None:
        logger.error(
            "Webhook called but bot instance is not available (initialization failed or not run)."
        )
        raise HTTPException(
            status_code=503,
            detail="Bot service temporarily unavailable due to initialization error",
        )

    update_json_str: str = ""
    update_id_str: str = "N/A"
    try:
        body_bytes = await request.body()
        update_json_str = body_bytes.decode("utf-8")
        if not update_json_str:
            logger.warning("Webhook received POST request with empty body.")
            raise HTTPException(status_code=400, detail="Empty body")

        logger.debug(f"Webhook update body: {update_json_str[:500]}...")
        update = telebot_types.Update.de_json(update_json_str)
        update_id_str = str(update.update_id)
        logger.info(f"Webhook processing update ID: {update_id_str}")

        await _global_bot_instance.process_new_updates([update])

        logger.info(f"Webhook finished processing update ID: {update_id_str}")
        return Response(content="OK", media_type="text/plain")

    except json.JSONDecodeError:
        logger.error(
            f"Failed to decode webhook body as JSON: {update_json_str!r}", exc_info=True
        )
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error processing update ID {update_id_str}:")
        return Response(
            content="Processing Error (check server logs)",
            media_type="text/plain",
            status_code=200,
        )


@app.get("/", tags=["health"], summary="Health Check")
async def root() -> dict[str, str]:
    """A simple health check endpoint."""
    if not _initialization_error and _global_bot_instance:
        return {
            "message": "Gemini Telegram Bot Webhook is active and bot is initialized."
        }
    elif _initialization_error:
        return {
            "message": "Gemini Telegram Bot Webhook is active, but bot initialization FAILED."
        }
    else:
        return {
            "message": "Gemini Telegram Bot Webhook is active, but bot is not yet initialized (startup pending or issue)."
        }
