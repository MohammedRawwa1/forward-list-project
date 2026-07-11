from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)
from handlers.base_handlers import (
    help_command,
    list_courses,
    list_categories,
    categories_page,
    createcat_page,
    create_category,
    create_parent,
    handle_create_category_parent,
    handle_create_category_parent_text,
    courses_callback,
    showtype_handler,
    showcat_handler,
    handle_course_selection,
    handle_category_name,
    handle_category_selection,
    handle_back_to_cats,
    show_coach_handler,
    show_coach_in_category,
    debug_db,
)
from handlers.course_handlers import (
    setup_course_handlers,
    addcoach_page,
    addcat_page,
    course_error_handler,
    cancel,
)
from handlers.bot_handlers import (
    delete_category_start,
    handle_delete_category_page,
    handle_delete_parent_page,
    delete_parent_start,
    handle_course_deletion,
    handle_cancel_delete_callback,
    delete_all_data_start,
    confirm_delete_all,
    cancel_delete_all_data,
)
from conversation_states import CREATE_CAT_NAME, CREATE_CAT_PARENT, DELETE_ALL
from handlers.delete_callbacks import handle_category_deletion, handle_item_deletion
from handlers.delete_callbacks import handle_delete_ref, handle_delete_confirm, handle_delete_summary
from handlers.category_design import setup_design_handlers

# Search handlers
from handlers.search_handlers import (
    get_search_conversation_handler,
    search_courses_pagination_callback,
    search_categories_pagination_callback,
    search_category_courses_pagination_callback,
)
from dotenv import load_dotenv
import logging
import os

load_dotenv()

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=log_level,
)
logger = logging.getLogger(__name__)


# ---------- helpers ----------


# ---------- application factory ----------

async def create_application():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN environment variable is not set")
    application = Application.builder().token(bot_token).build()
    return application


def init_sync_mongo():
    """Initialize synchronous pymongo client for strong-durability persisted writes.

    This should be called at startup when Redis is not configured so
    that blocking writes to MongoDB succeed from synchronous code paths.
    """
    mongo_uri = os.getenv("MONGODB_URL")
    db_name = os.getenv("MONGODB_NAME")
    if not mongo_uri or not db_name:
        logger.warning("init_sync_mongo: MONGODB_URL or MONGODB_NAME not set; skipping sync init")
        return
    try:
        from database.mongo_handler import MongoDB
        MongoDB.initialize_sync(mongo_uri, db_name)
        logger.info("Synchronous MongoDB client initialized (strong durability enabled)")
    except Exception:
        logger.exception("Failed to initialize synchronous MongoDB client")


# ---------- register handlers ----------
async def setup_handlers(application: Application):
    if not application:
        logger.error("Application is not initialised.")
        return

    # ---------- commands ----------
    # /start is now handled by a ConversationHandler in setup_course_handlers
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("courses", list_courses))
    application.add_handler(CommandHandler("categories", list_categories))
    application.add_handler(CommandHandler("debug_db", debug_db))
    application.add_handler(CommandHandler("delete_category", delete_category_start))
    application.add_handler(CommandHandler("delete_parent", delete_parent_start))
    # Note: `delete_category_start` not present; command removed until handler is added.
    # Global /cancel handler is registered after conversations so ConversationHandler
    # fallbacks get first chance to handle the command.
    # Note: `create_parent` is handled via the ConversationHandler below

    # ---------- callbacks ----------
    # `del_menu_` callback handler not present; skip registration.
    application.add_handler(
        CallbackQueryHandler(confirm_delete_all, pattern="^confirm_delete_all$")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_delete_all_data, pattern="^cancel_delete_all$")
    )
    # Note: handle_categories_pagination handler isn't registered because
    # pagination now uses categories_page::<page> format instead.
    application.add_handler(
        CallbackQueryHandler(courses_callback, pattern=r"^courses::")
    )
    application.add_handler(
        CallbackQueryHandler(addcoach_page, pattern=r"^addcoach_page::")
    )
    application.add_handler(
        CallbackQueryHandler(addcat_page, pattern=r"^addcat_page::")
    )
    application.add_handler(
        CallbackQueryHandler(categories_page, pattern=r"^categories_page::")
    )
    application.add_handler(
        CallbackQueryHandler(createcat_page, pattern=r"^createcat_page::")
    )
    application.add_handler(
        CallbackQueryHandler(handle_category_selection, pattern=r"^category_")
    )
    application.add_handler(
        CallbackQueryHandler(handle_category_selection, pattern=r"^category::")
    )
    # Register the more specific coach-in-category handler before the generic coach handler
    application.add_handler(
        CallbackQueryHandler(show_coach_in_category, pattern=r"^coach_in_cat::")
    )
    application.add_handler(
        CallbackQueryHandler(show_coach_handler, pattern=r"^coach_")
    )
    application.add_handler(
        CallbackQueryHandler(showtype_handler, pattern=r"^showtype::")
    )
    application.add_handler(
        CallbackQueryHandler(handle_course_selection, pattern=r"^course_")
    )
    application.add_handler(
        CallbackQueryHandler(handle_course_selection, pattern=r"^course::")
    )
    application.add_handler(
        CallbackQueryHandler(handle_course_selection, pattern=r"^course_ref::")
    )
    # confirm/cancel per-item handlers
    # Per-item confirm handler not implemented; keep cancel handler which exists
    application.add_handler(
        CallbackQueryHandler(
            handle_cancel_delete_callback,
            pattern=r"^cancel_delete",
        )
    )
    application.add_handler(
        CallbackQueryHandler(handle_back_to_cats, pattern=r"^back_to_cats$")
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_course_deletion,
            pattern=r"^delete_course_",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_course_deletion,
            pattern=r"^delete_course::",
        )
    )

    # ---------- conversations ----------
    await setup_course_handlers(application)
    
    # ---------- search (before global cancel so active search gets priority) ----------
    try:
        application.add_handler(get_search_conversation_handler())
    except Exception:
        logger.exception("Failed to register search conversation handler")

    # Register a global /cancel after conversations are registered so that
    # ConversationHandler fallbacks handle /cancel first when active.
    application.add_handler(CommandHandler("cancel", cancel))

    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("create_category", create_category), CommandHandler("create_parent", create_parent)],
            allow_reentry=True,
            states={
                CREATE_CAT_PARENT: [
                    CallbackQueryHandler(handle_create_category_parent, pattern=r"^createcat_parent::"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_create_category_parent_text),
                ],
                CREATE_CAT_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        handle_category_name,
                    )
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )

    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("delete_all_data", delete_all_data_start)],
            states={
                DELETE_ALL: [
                    CallbackQueryHandler(
                        confirm_delete_all,
                        pattern="^confirm_delete_all$",
                    ),
                    CallbackQueryHandler(
                        cancel_delete_all_data,
                        pattern="^cancel_delete_all$",
                    ),
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )

    # ---------- deletion (last so not shadowed) ----------
    # Register page handler before the generic delete handler so
    # `delete_category_page::N` isn't captured by the generic pattern.
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_category_page,
            pattern=r"^delete_category_page::\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            handle_delete_parent_page,
            pattern=r"^delete_parent_page::\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_category_deletion,
            pattern=r"^delete_category_",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_item_deletion,
            pattern=r"^delete_item_",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_item_deletion,
            pattern=r"^delete_item::",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_ref,
            pattern=r"^delete_ref::",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_confirm,
            pattern=r"^delete_confirm::",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_summary,
            pattern=r"^delete_summary::",
        )
    )
    application.add_handler(
        CallbackQueryHandler(showcat_handler, pattern=r"^showcat::[^:]+::\d+$")
    )
    # Support short stored refs for showcat links (avoids long callback_data)
    application.add_handler(
        CallbackQueryHandler(showcat_handler, pattern=r"^showcat_ref::")
    )
    application.add_handler(
        CallbackQueryHandler(show_coach_in_category, pattern=r"^coach_in_cat_ref::")
    )
    # Generic showcat handler (catch-all) registered after the paged pattern
    application.add_handler(
        CallbackQueryHandler(showcat_handler, pattern=r"^showcat::")
    )

    # ---------- category designs ----------
    try:
        setup_design_handlers(application)
    except Exception:
        logger.exception("Failed to register design handlers")

    # ---------- search pagination handlers ----------
    application.add_handler(
        CallbackQueryHandler(search_courses_pagination_callback, pattern=r"^search_courses_pg::")
    )
    application.add_handler(
        CallbackQueryHandler(search_categories_pagination_callback, pattern=r"^search_categories_pg::")
    )
    application.add_handler(
        CallbackQueryHandler(search_category_courses_pagination_callback, pattern=r"^search_cat_courses_pg::")
    )

    # ---------- error handler ----------
    application.add_error_handler(course_error_handler)
