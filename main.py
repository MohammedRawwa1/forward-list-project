import logging
import os
import json
import uvicorn

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, TypeHandler, CallbackContext
from dotenv import load_dotenv
from loguru import logger

from bot import create_application, setup_handlers
from database.mongo_handler import MongoDB

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger.add("bot.log", rotation="10 MB", level="INFO")

app = FastAPI()

application: Application = None
bot_token = os.getenv("BOT_TOKEN")

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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, workers=1)
