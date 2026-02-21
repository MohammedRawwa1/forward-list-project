import logging
import os
import json
import uvicorn
import asyncio
import signal

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application, TypeHandler, CallbackContext
from dotenv import load_dotenv
from loguru import logger

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


# ---------- startup ----------
@app.on_event("startup")
async def startup_event():
    global application

    await initialize_db()
    await MongoDB.ensure_indexes("categories")

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


# ---------- webhook ----------
@app.post("/{token}/")
async def webhook(token: str, request: Request):
    if token != bot_token:
        raise HTTPException(status_code=400, detail="Invalid token")

    json_str = await request.body()
    update = Update.de_json(json.loads(json_str), application.bot)

    await application.process_update(update)

    return {"status": "ok"}


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
