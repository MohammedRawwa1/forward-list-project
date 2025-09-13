from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, error as telegram_error
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters
)
from handlers.base_handlers import (
    help,
    list_courses,
    list_categories,
    create_category,
    get_courses_by_category,
    courses_callback,
    handle_courses_pagination,
    handle_course_selection,
    handle_category_name,
    handle_category_selection
)
from handlers.course_handlers import (
    setup_course_handlers,
    start,
    add_course_start,
    add_course_name,
    add_course_link,
    add_course_category,
    category_selected,
    error_handler as course_error_handler,
    handle_link_parsing_error,
    cancel
)
from handlers.bot_handlers import (
    generate_pagination_keyboard,
    generate_keyboard,
    delete_item,
    delete_category,
    handle_course_deletion,
    handle_deletion_confirmation,
    handle_deletion_selection,
    delete_all_data,
    delete_all_data_start,
    delete_item_start,
    delete_category_start,
    confirm_delete_all,
    cancel_delete_all_data,
    initiate_delete_item
)
from conversation_states import (
    NAME, LINK, CATEGORY, CATEGORY_NAME,
    DELETE_ALL, CONFIRM_DELETE, CANCEL_DELETE,
    MAX_CATEGORY_NAME_LENGTH
)
from handlers.custom_thumbnail import add_thumb, del_thumb, setup_thumbnail_handlers
from handlers.constants import MAX_CATEGORY_NAME_LENGTH, CONFIRM_DELETE_ALL_DATA, CANCEL_DELETE_ALL_DATA
from database.mongo_handler import MongoDB
from handlers.db_connection import get_db
from dotenv import load_dotenv
import logging
import os
import asyncio
import re

load_dotenv()  # Load environment variables from .env file

# Load logging configuration
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=log_level)
logger = logging.getLogger(__name__)  # Use __name__ for logging
# Helper function to validate category names
def is_valid_category_name(category_name: str):
    """Validate if a category name is valid."""
    # Allow letters, numbers, spaces, and hyphens
    return bool(re.match(r"^[a-zA-Z0-9\s\-]+$", category_name))

async def create_application():
    try:
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token:
            raise ValueError("BOT_TOKEN environment variable is not set")

        mongo_uri = os.getenv("MONGODB_URL")
        db_name = os.getenv("MONGODB_NAME")
        if not mongo_uri or not db_name:
            raise ValueError("MONGODB_URL and MONGODB_NAME environment variables are not set")

        # Build the application instance
        application = Application.builder().token(bot_token).build()

        # Initialize MongoDB asynchronously
        await MongoDB.initialize(mongo_uri, db_name)
        return application
    except Exception as e:
        logger.error(f"Failed to create application: {e}")
        raise
async def setup_handlers(application: Application):
    if not application:
        logger.error("Application is not initialized.")
        return

    # Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help))
    application.add_handler(CommandHandler("courses", list_courses))
    application.add_handler(CommandHandler("categories", list_categories))
    application.add_handler(CommandHandler("addthumb", add_thumb))
    application.add_handler(CommandHandler("delthumb", del_thumb))
    application.add_handler(CommandHandler("add", add_course_start))

    # CallbackQuery Handlers for deleting items
    application.add_handler(CallbackQueryHandler(handle_deletion_confirmation, pattern="handle_deletion"))
    application.add_handler(CallbackQueryHandler(handle_deletion_selection, pattern="handle_delete_selection"))
    application.add_handler(CallbackQueryHandler(confirm_delete_all, pattern="confirm_delete_all"))
    application.add_handler(CallbackQueryHandler(cancel_delete_all_data, pattern="cancel_delete_all"))
    application.add_handler(CallbackQueryHandler(initiate_delete_item, pattern="^delete_item_"))
    application.add_handler(CallbackQueryHandler(handle_courses_pagination, pattern=r"^courses_(prev|next)_\d+$"))
    application.add_handler(CallbackQueryHandler(courses_callback, pattern=r"^courses_"))
    application.add_handler(CallbackQueryHandler(handle_category_selection, pattern=r"^category_"))
    application.add_handler(CallbackQueryHandler(handle_course_selection, pattern=r"^course_"))
    application.add_handler(CallbackQueryHandler(handle_course_deletion, pattern=r"^delete_course_"))

    # Setup course-related handlers
    await setup_course_handlers(application)

    # Add new category_conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("create_category", create_category)],
        states={
            CATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_category_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # Add course conversation handler
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_course_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_name)],
            LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_link)],
            CATEGORY: [CallbackQueryHandler(category_selected, pattern=r"^category_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # Delete all data conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("delete_all_data", delete_all_data_start)],
        states={
            DELETE_ALL: [
                CallbackQueryHandler(confirm_delete_all, pattern="^confirm_delete_all$"),
                CallbackQueryHandler(cancel_delete_all_data, pattern="^cancel_delete_all$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)]  # Add cancel as a fallback
    ))

    # Setup thumbnail-related handlers
    await setup_thumbnail_handlers(application)

    # Error_handler
    application.add_error_handler(course_error_handler)

async def main():
    try:
        # Create the application
        application = await create_application()

        # Set up handlers
        await setup_handlers(application)

        # Set the webhook
        webhook_url = os.getenv("WEBHOOK_URL")
        if webhook_url:
            await application.bot.set_webhook(webhook_url)

        # Run the application using webhook
        await application.run_webhook(port=10000, webhook_path="/webhook")

    except Exception as e:
        logger.error(f"Failed to start application: {e}")

if __name__ == "__main__":
    # Run the main function within an event loop
    asyncio.run(main())
