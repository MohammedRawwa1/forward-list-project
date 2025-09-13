from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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
    handle_categories_pagination,   # <-- NEW
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
from handlers.delete_callbacks import handle_category_deletion, handle_item_deletion
from handlers.custom_thumbnail import add_thumb, del_thumb, setup_thumbnail_handlers
from database.mongo_handler import MongoDB
from dotenv import load_dotenv
import logging
import os
import asyncio
import re

load_dotenv()

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=log_level)
logger = logging.getLogger(__name__)

# ----------  helpers  ----------
def is_valid_category_name(category_name: str):
    return bool(re.match(r"^[a-zA-Z0-9\s\-]+$", category_name))

# ----------  application factory  ----------
async def create_application():
    try:
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token:
            raise ValueError("BOT_TOKEN environment variable is not set")

        mongo_uri = os.getenv("MONGODB_URL")
        db_name   = os.getenv("MONGODB_NAME")
        if not mongo_uri or not db_name:
            raise ValueError("MONGODB_URL and MONGODB_NAME must be set")

        application = Application.builder().token(bot_token).build()
        await MongoDB.initialize(mongo_uri, db_name)
        return application
    except Exception as e:
        logger.error(f"Failed to create application: {e}")
        raise

# ----------  register everything  ----------
async def setup_handlers(application: Application):
    if not application:
        logger.error("Application is not initialised.")
        return

    # ----------  commands  ----------
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help))
    application.add_handler(CommandHandler("courses", list_courses))
    application.add_handler(CommandHandler("categories", list_categories))
    application.add_handler(CommandHandler("delete_category", delete_category_start))
    application.add_handler(CommandHandler("delete_course",   delete_item_start))
    application.add_handler(CommandHandler("addthumb", add_thumb))
    application.add_handler(CommandHandler("delthumb", del_thumb))
    application.add_handler(CommandHandler("add", add_course_start))
    application.add_handler(CommandHandler("cancel", cancel))
    
    # ----------  callbacks  ----------
    application.add_handler(CallbackQueryHandler(handle_deletion_confirmation, pattern="handle_deletion"))
    application.add_handler(CallbackQueryHandler(handle_deletion_selection, pattern="handle_delete_selection"))
    application.add_handler(CallbackQueryHandler(confirm_delete_all, pattern="confirm_delete_all"))
    application.add_handler(CallbackQueryHandler(handle_category_deletion, pattern=r"^delete_category_"))
    application.add_handler(CallbackQueryHandler(handle_item_deletion, pattern=r"^delete_item_"))
    application.add_handler(CallbackQueryHandler(cancel_delete_all_data, pattern="cancel_delete_all"))
    application.add_handler(CallbackQueryHandler(initiate_delete_item, pattern="^delete_item_"))
    application.add_handler(CallbackQueryHandler(handle_courses_pagination, pattern=r"^courses_(prev|next)_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_categories_pagination, pattern=r"^categories_(prev|next)_\d+$"))  # NEW
    application.add_handler(CallbackQueryHandler(courses_callback, pattern=r"^courses_"))
    application.add_handler(CallbackQueryHandler(handle_category_selection, pattern=r"^category_"))
    application.add_handler(CallbackQueryHandler(handle_course_selection, pattern=r"^course_"))
    application.add_handler(CallbackQueryHandler(handle_course_deletion, pattern=r"^delete_course_"))

    # ----------  conversations  ----------
    await setup_course_handlers(application)

    # create category
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("create_category", create_category)],
        states={CATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_category_name)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # add course
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_course_start)],
        states={
            NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_name)],
            LINK:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_link)],
            CATEGORY: [CallbackQueryHandler(category_selected, pattern=r"^category_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # delete all data
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("delete_all_data", delete_all_data_start)],
        states={
            DELETE_ALL: [
                CallbackQueryHandler(confirm_delete_all, pattern="^confirm_delete_all$"),
                CallbackQueryHandler(cancel_delete_all_data, pattern="^cancel_delete_all$")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))

    # ----------  thumbnails  ----------
    await setup_thumbnail_handlers(application)

    # ----------  errors  ----------
    application.add_error_handler(course_error_handler)

# ----------  run  ----------
async def main():
    try:
        application = await create_application()
        await setup_handlers(application)

        webhook_url = os.getenv("WEBHOOK_URL")
        if webhook_url:
            await application.bot.set_webhook(webhook_url)

        await application.run_webhook(port=10000, webhook_path="/webhook")
    except Exception as e:
        logger.error(f"Failed to start application: {e}")

if __name__ == "__main__":
    asyncio.run(main())
