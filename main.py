import logging
import os
import json
import uvicorn
import asyncio
import signal
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, TypeHandler, CallbackContext
from dotenv import load_dotenv
from loguru import logger
import httpx

from bot import create_application, setup_handlers
from handlers.base_handlers import start_redis_retry_worker
from database.mongo_handler import MongoDB

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger.add("bot.log", rotation="10 MB", level="INFO")

app = FastAPI()

application: Application = None
bot_token = os.getenv("BOT_TOKEN")

# Optional token for an authenticated liveness probe. If set, the health
# endpoint requires the header `X-LIVENESS-TOKEN: <token>`.
LIVENESS_TOKEN = os.getenv("LIVENESS_TOKEN")

if not bot_token:
    raise ValueError("BOT_TOKEN environment variable is not set")


# ---------- helpers ----------

def _resolve_webhook_url() -> Optional[str]:
    """Determine the public webhook URL the bot should register with Telegram.

    Priority:
    1. ``WEBHOOK_URL`` env var (fully custom)
    2. ``RENDER_EXTERNAL_URL`` (set by Render platform) + ``/<bot_token>/``
    3. Fall back to ``None`` (skip auto-registration)
    """
    explicit = os.getenv("WEBHOOK_URL")
    if explicit:
        return explicit.rstrip("/") + "/"

    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        return f"{render_url.rstrip('/')}/{bot_token}/"

    logger.warning(
        "Neither WEBHOOK_URL nor RENDER_EXTERNAL_URL is set; "
        "webhook auto-registration will be skipped."
    )
    return None


async def _register_webhook(url: str) -> bool:
    """Call Telegram ``setWebhook`` with *url* and log the result."""
    api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    payload: dict = {"url": url, "max_connections": 100}
    # If a secret token is configured, include it so Telegram sends it back
    # in the X-Telegram-Bot-Api-Secret-Token header with every update.
    secret_token = os.getenv("TELEGRAM_SECRET_TOKEN")
    if secret_token:
        payload["secret_token"] = secret_token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(api_url, json=payload)
            data = resp.json()
            if data.get("ok"):
                logger.info("Webhook successfully registered -> %s", url)
                return True
            logger.error(
                "Telegram setWebhook failed: %s (url=%s)", data, url
            )
            return False
    except Exception as exc:
        logger.exception("Failed to call Telegram setWebhook: %s", exc)
        return False


# ---------- DB ----------
async def initialize_db():
    mongo_uri = os.getenv("MONGODB_URL")
    db_name = os.getenv("MONGODB_NAME")

    if not mongo_uri or not db_name:
        raise ValueError("MONGODB_URL and MONGODB_NAME must be set")

    await MongoDB.initialize(mongo_uri, db_name)


# ---------- global error ----------
async def global_error_handler(update: object, context: object) -> None:
    logger.error("Global error: %s", context.error)
    logger.error("Update: %s", update)


async def echo_update(update: Update, context: CallbackContext):
    logger.info(
        "RAW update %s | user=%s chat=%s",
        update.update_id,
        update.effective_user.id if update.effective_user else None,
        update.effective_chat.id if update.effective_chat else None,
    )


# ---------- low-level update processing ----------

async def _process_telegram_update(request: Request) -> dict:
    """Deserialize a Telegram ``Update`` from *request* and pass it through
    the application handler chain.  Returns a JSON-serialisable dict."""
    if application is None:
        # Startup hasn't finished yet — Render cold start. Return a
        # transient error so Telegram retries after a short delay.
        raise HTTPException(status_code=503, detail="Bot still starting up")
    json_str = await request.body()
    update = Update.de_json(json.loads(json_str), application.bot)
    await application.process_update(update)
    return {"status": "ok"}


# ---------- startup ----------
@app.on_event("startup")
async def startup_event():
    global application

    await initialize_db()
    # Rehydrate any persisted callback refs so inline buttons keep working
    # across restarts when Redis is not configured.
    try:
        from handlers.base_handlers import _rehydrate_callback_map
        try:
            await _rehydrate_callback_map()
        except Exception:
            logger.exception("Failed to rehydrate callback refs on startup")
    except Exception:
        # best-effort: skip if import fails
        logger.debug("Rehydrate helper not available; skipping")
    # If Redis is not configured, initialize a synchronous pymongo client
    # so synchronous code paths can perform blocking durable writes.
    try:
        if not os.getenv('REDIS_URL'):
            from bot import init_sync_mongo
            try:
                init_sync_mongo()
            except Exception:
                logger.exception("Failed to initialize sync mongo client on startup")
    except Exception:
        logger.exception("Error while attempting sync mongo init check")
    # Index creation managed manually; automatic ensure_indexes disabled.
    logger.info("Skipping automatic ensure_indexes (manual index management)")

    application = await create_application()
    await application.initialize()
    await setup_handlers(application)

    application.add_error_handler(global_error_handler)
    application.add_handler(TypeHandler(Update, echo_update), group=-1)

    # Start Redis-backed retry worker (if Redis configured)
    try:
        asyncio.create_task(start_redis_retry_worker(application))
    except Exception:
        logger.exception("Failed to start redis retry worker")

    # Auto-register webhook with Telegram so the correct URL is always set
    # after a deploy or restart.
    wh_url = _resolve_webhook_url()
    if wh_url:
        try:
            await _register_webhook(wh_url)
        except Exception:
            logger.exception("Failed to auto-register webhook on startup")
    else:
        logger.info(
            "Webhook URL could not be determined; "
            "skipping auto-registration. "
            "Set WEBHOOK_URL or RENDER_EXTERNAL_URL env vars."
        )

    # Register signal handlers to log shutdown signals (helps debug platform-initiated stops)
    loop = asyncio.get_event_loop()
    def _log_signal(sig):
        logger.warning("Received shutdown signal: {}", sig)
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: _log_signal('SIGTERM'))
        loop.add_signal_handler(signal.SIGINT, lambda: _log_signal('SIGINT'))
    except NotImplementedError:
        # add_signal_handler may not be available on all platforms (e.g., Windows)
        logger.info("Signal handlers not supported on this platform; skipping registration.")


@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully shutdown the Telegram `Application` and close DB connections."""
    global application
    logger.info("Shutdown event triggered; attempting graceful stop of bot application")
    try:
        if application is not None:
            try:
                await application.shutdown()
            except Exception:
                logger.exception("Error during application.shutdown()")
            try:
                await application.stop()
            except Exception:
                logger.exception("Error during application.stop()")
    except Exception:
        logger.exception("Failed to gracefully stop application")
    try:
        await MongoDB.close()
    except Exception:
        logger.exception("Error closing MongoDB connection during shutdown")


# ---------- webhook endpoints ----------
# NOTE: /webhook fallback routes must be defined BEFORE /{token}/ so they
# take priority.  If defined after, FastAPI would match /webhook/ against
# /{token}/ first (with token="webhook") and reject it with 400.

@app.post("/webhook")
@app.post("/webhook/")
async def webhook_fallback(request: Request):
    """Secondary webhook endpoint at the plain ``/webhook`` path.

    Some deployments use a reverse-proxy or platform-level routing that
    forwards Telegram updates to ``/webhook`` instead of ``/<token>/``.
    This endpoint accepts those requests and processes them identically.
    Both ``/webhook`` and ``/webhook/`` are handled to avoid redirect issues.
    """
    # Optional: verify via X-Telegram-Bot-Api-Secret-Token header if configured.
    secret_token = os.getenv("TELEGRAM_SECRET_TOKEN")
    if secret_token:
        hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if hdr != secret_token:
            raise HTTPException(status_code=401, detail="Invalid secret token")
    return await _process_telegram_update(request)


@app.post("/{token}/")
async def webhook(token: str, request: Request):
    """Primary webhook endpoint — path includes the bot token for verification."""
    if token != bot_token:
        raise HTTPException(status_code=400, detail="Invalid token")
    return await _process_telegram_update(request)


@app.get("/")
async def root():
    return {"message": "Bot is running"}


@app.get("/health")
async def health(request: Request):
    """Liveness endpoint. If `LIVENESS_TOKEN` is set, caller must provide
    header `X-LIVENESS-TOKEN` with the same value.
    """
    if LIVENESS_TOKEN:
        hdr = request.headers.get("X-LIVENESS-TOKEN")
        if hdr != LIVENESS_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, workers=1)
