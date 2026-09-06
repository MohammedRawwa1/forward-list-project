import asyncio
import json
import logging
import os
import re
import signal
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, CallbackContext, TypeHandler

import logging_config  # noqa: F401
from bot import create_application, setup_handlers
from database.mongo_handler import MongoDB
from handlers.base_handlers import _bg_task, start_cache_cleanup_worker, start_redis_retry_worker
from logging_config import configure_uvicorn_loggers

logger = logging.getLogger(__name__)

application: Application = None
bot_token = os.getenv("BOT_TOKEN")

LIVENESS_TOKEN = os.getenv("LIVENESS_TOKEN")

if not bot_token:
    msg = "BOT_TOKEN environment variable is not set"
    raise ValueError(msg)


# ---------- helpers ----------


def _redact_tokens(text: str) -> str:
    """Strip any bot token (current and previously-issued) from a log string."""
    return re.sub(r"bot\d+:[A-Za-z0-9_-]{30,}", "<token>", text)


def _resolve_webhook_url() -> Optional[str]:
    """Resolve the webhook URL, enforcing that WEBHOOK_URL and BOT_TOKEN match.

    Both env vars must be updated together whenever the token is rotated:
    if WEBHOOK_URL is set but does not contain the current BOT_TOKEN, startup
    fails fast with a clear error — Telegram would keep posting updates to the
    OLD token path and this deployment would never receive any updates (the
    bot would be silently deaf).

    When WEBHOOK_URL is not set, RENDER_EXTERNAL_URL + BOT_TOKEN is used,
    which is always in sync automatically.
    """
    explicit = os.getenv("WEBHOOK_URL")
    if explicit:
        url = explicit.rstrip("/") + "/"
        if bot_token not in url:
            msg = (
                "WEBHOOK_URL does not contain the current BOT_TOKEN. Both env "
                "vars must be updated together after a token rotation: Telegram "
                "would keep posting updates to the OLD token path and this "
                "deployment would never receive updates. Update WEBHOOK_URL to "
                "end with the path of the current BOT_TOKEN, or unset WEBHOOK_URL "
                "to auto-derive the URL from RENDER_EXTERNAL_URL."
            )
            logger.error(msg)
            raise ValueError(msg)
        return url

    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        return f"{render_url.rstrip('/')}/{bot_token}/"

    logger.warning("Neither WEBHOOK_URL nor RENDER_EXTERNAL_URL is set; webhook auto-registration will be skipped.")
    return None


async def _register_webhook(url: str) -> bool:
    api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    payload: dict = {"url": url, "max_connections": 100}
    secret_token = os.getenv("TELEGRAM_SECRET_TOKEN")
    if secret_token:
        payload["secret_token"] = secret_token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(api_url, json=payload)
            data = resp.json()
            redacted_url = _redact_tokens(url)
            if data.get("ok"):
                logger.info("Webhook successfully registered -> %s", redacted_url)
                return True
            logger.error("Telegram setWebhook failed: %s (url=%s)", data, redacted_url)
            return False
    except Exception:
        logger.exception("Failed to call Telegram setWebhook")
        return False


# ---------- DB ----------
async def initialize_db():
    mongo_uri = os.getenv("MONGODB_URL")
    db_name = os.getenv("MONGODB_NAME")

    if not mongo_uri or not db_name:
        msg = "MONGODB_URL and MONGODB_NAME must be set"
        raise ValueError(msg)

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
    if application is None:
        raise HTTPException(status_code=503, detail="Bot still starting up")
    json_str = await request.body()
    if len(json_str) > 1_000_000:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        payload = json.loads(json_str)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail="Invalid JSON") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    update = Update.de_json(payload, application.bot)
    if update is None:
        raise HTTPException(status_code=400, detail="Invalid update payload")
    await application.process_update(update)
    return {"status": "ok"}


# ---------- lifespan (startup + shutdown) ----------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global application

    # ---------------------------- startup ----------------------------
    await initialize_db()
    try:
        from handlers.base_handlers import _rehydrate_callback_map

        try:
            await _rehydrate_callback_map()
        except Exception:
            logger.exception("Failed to rehydrate callback refs on startup")
    except Exception:
        logger.debug("Rehydrate helper not available; skipping")
    try:
        if not os.getenv("REDIS_URL"):
            from bot import init_sync_mongo

            try:
                init_sync_mongo()
            except Exception:
                logger.exception("Failed to initialize sync mongo client on startup")
    except Exception:
        logger.exception("Error while attempting sync mongo init check")
    logger.info("Index creation is managed manually")

    try:
        db = await MongoDB.get_db()
        await db.coaches.create_index("topics")
        logger.info("Index on coaches.topics created unconditionally")
    except Exception:
        logger.debug("Could not create coaches.topics index (best-effort)")

    try:
        await MongoDB.ensure_uuid_indexes()
    except Exception:
        logger.exception("ensure_uuid_indexes failed (best-effort)")

    application = await create_application()
    await application.initialize()
    await setup_handlers(application)

    application.add_error_handler(global_error_handler)
    application.add_handler(TypeHandler(Update, echo_update), group=-1)

    try:
        _bg_task(start_cache_cleanup_worker())
    except Exception:
        logger.exception("Failed to start cache cleanup worker")

    try:
        _bg_task(start_redis_retry_worker(application))
    except Exception:
        logger.exception("Failed to start redis retry worker")

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
            "Set WEBHOOK_URL or RENDER_EXTERNAL_URL env vars.",
        )

    configure_uvicorn_loggers()

    loop = asyncio.get_running_loop()

    def _log_signal(sig):
        logger.warning("Received shutdown signal: %s", sig)

    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: _log_signal("SIGTERM"))
        loop.add_signal_handler(signal.SIGINT, lambda: _log_signal("SIGINT"))
    except NotImplementedError:
        logger.info("Signal handlers not supported on this platform; skipping registration.")

    yield

    # ---------------------------- shutdown ----------------------------
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


app = FastAPI(lifespan=lifespan)


# ---------- webhook endpoints ----------


@app.post("/webhook")
@app.post("/webhook/")
async def webhook_fallback(request: Request):
    secret_token = os.getenv("TELEGRAM_SECRET_TOKEN")
    if secret_token:
        hdr = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if hdr != secret_token:
            raise HTTPException(status_code=401, detail="Invalid secret token")
    return await _process_telegram_update(request)


@app.post("/{token}/")
async def webhook(token: str, request: Request):
    if token != bot_token:
        raise HTTPException(status_code=400, detail="Invalid token")
    return await _process_telegram_update(request)


@app.get("/")
async def root():
    return {"message": "Bot is running"}


@app.get("/health")
async def health(request: Request):
    if LIVENESS_TOKEN:
        hdr = request.headers.get("X-LIVENESS-TOKEN")
        if hdr != LIVENESS_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")

    mongo_ok = False
    try:
        db = await MongoDB.get_db()
        await db.command("ping")
        mongo_ok = True
    except Exception:
        mongo_ok = False

    if not mongo_ok:
        return JSONResponse(
            {"status": "degraded", "mongo": "disconnected"},
            status_code=503,
        )
    return JSONResponse({"status": "ok", "mongo": "connected"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, workers=1, log_config=None)
