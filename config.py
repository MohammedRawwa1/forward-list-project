import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    TELEGRAM_APP_ID = os.getenv("TELEGRAM_APP_ID")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    LOGS_CHANNEL = os.getenv("LOGS_CHANNEL")
    BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")
    MONGODB_URL = os.getenv("MONGODB_URL")
    MONGODB_NAME = os.getenv("MONGODB_NAME")

    # Note: env vars validated at startup in main.py instead of import-time
    pass

    MAX_DOWNLOAD_SIZE = int(os.getenv("MAX_DOWNLOAD_SIZE", 10737418240))  # Default to 10GB
    DOWNLOAD_LOCATION = os.path.join(os.path.dirname(__file__), "downloads")
    TG_MAX_SIZE = 2040108421  # Set Telegram max size
    CHUNK_SIZE = 1024 * 6  # 6 KB, adjust if needed for efficiency
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # Default logging level (INFO or DEBUG)
