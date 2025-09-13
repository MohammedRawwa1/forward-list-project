import logging
import os
import json
import asyncio
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from bot import create_application, setup_handlers
from telegram import Update, error as telegram_error  # Import telegram_error
from telegram.ext import Application
from dotenv import load_dotenv
from database.mongo_handler import MongoDB
from loguru import logger

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger.add("bot.log", rotation="10 MB", level="INFO", format="{time} {level} {message}")

# Initialize FastAPI app
app = FastAPI()

# Global application object
application: Application = None
bot_token = os.getenv("BOT_TOKEN")

if not bot_token:
    raise ValueError("BOT_TOKEN environment variable is not set")

# Initialize MongoDB
async def initialize_db():
    mongo_uri = os.getenv("MONGODB_URL")
    db_name = os.getenv("MONGODB_NAME")
    if not mongo_uri or not db_name:
        raise ValueError("MONGODB_URL and MONGODB_NAME must be set in the environment variables.")
    await MongoDB.initialize(mongo_uri, db_name)

# Set up webhook with retry logic
async def send_with_backoff(send_func, *args, **kwargs):
    """Send a request with exponential backoff."""
    retries = 0
    max_retries = 5
    delay = 1  # Initial delay in seconds

    while retries < max_retries:
        try:
            await send_func(*args, **kwargs)
            return
        except telegram_error.RetryAfter as e:  # Use telegram_error.RetryAfter
            logger.warning(f"Attempt {retries + 1} failed: {e}")
            retries += 1
            if retries < max_retries:
                backoff_time = e.retry_after  # Use the retry_after value from the exception
                logger.info(f"Retrying after {backoff_time} seconds...")
                await asyncio.sleep(backoff_time)
        except Exception as e:
            logger.error(f"Error: {e}")
            break

    if retries >= max_retries:
        logger.error("Max retries reached. Giving up.")
        raise Exception("Max retries reached. Giving up.")

async def set_webhook_with_backoff(application: Application, url: str):
    """Set webhook with exponential backoff."""
    await send_with_backoff(application.bot.set_webhook, url)
    
async def send_message_with_backoff(application: Application, chat_id: int, text: str):
    """Sends a message with exponential backoff if rate-limited."""
    await send_with_backoff(application.bot.send_message, chat_id, text)

# main.py
async def global_error_handler(update: object, context: object) -> None:
    logger.error("⚠️  global_error_handler caught: %s", context.error)
    logger.error("Update: %s", update)

# 2.  inside startup_event, AFTER application exists
@app.on_event("startup")
async def startup_event():
    global application
    await initialize_db()
    # >>>>>> indexes go here <<<<<<
    await MongoDB.ensure_indexes('categories')
    await MongoDB.ensure_indexes('courses')
    # >>>>>> end indexes <<<<<<
    application = await create_application()
    await application.initialize()
    await setup_handlers(application)
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        raise ValueError("WEBHOOK_URL environment variable is not set")
    await set_webhook_with_backoff(application, webhook_url)
    # 3.  register it here
    application.add_error_handler(global_error_handler)
    
@app.post("/{token}/")
async def webhook(token: str, request: Request):
    """Handles the incoming webhook from Telegram."""
    if token != os.getenv("BOT_TOKEN"):
        raise HTTPException(status_code=400, detail="Invalid token")

    json_str = await request.body()
    update = Update.de_json(json.loads(json_str), application.bot)
    await application.process_update(update)
    return {"status": "ok"}
    
async def retry_send_message():
    try:
        # Your code to send messages
        await bot.send_message(chat_id, "Message text")
    except telegram_error.RetryAfter as e:  # Use telegram_error.RetryAfter
        # Wait for the time specified in the exception
        await asyncio.sleep(e.retry_after)
        await retry_send_message()
        
# Root endpoint
@app.get("/")
async def root():
    return {"message": "Hello, this is the root path."}
    
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))  # Use environment variable PORT or default to 10000
    uvicorn.run("main:app", host="0.0.0.0", port=port)
