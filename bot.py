import logging
import os

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

import logging_config  # noqa: F401 — configures root logger (console + file) on import
from config import env_float, env_int
from conversation_states import CREATE_CAT_NAME, CREATE_CAT_PARENT, DELETE_ALL
from handlers.base_handlers import (
    categories_page,
    courses_callback,
    create_category,
    create_parent,
    createcat_page,
    debug_db,
    handle_back_to_cats,
    handle_category_name,
    handle_category_selection,
    handle_course_selection,
    handle_create_category_parent,
    handle_create_category_parent_text,
    help_command,
    list_categories,
    list_courses,
    show_coach_handler,
    show_coach_in_category,
    showcat_handler,
    showtype_handler,
)
from handlers.bot_handlers import (
    cancel_delete_all_data,
    confirm_delete_all,
    delete_all_data_start,
    delete_category_start,
    delete_parent_start,
    handle_cancel_delete_callback,
    handle_delete_category_page,
    handle_delete_parent_page,
)
from handlers.category_design import setup_design_handlers
from handlers.course_handlers import (
    addcat_page,
    addcoach_page,
    cancel,
    course_error_handler,
    setup_course_handlers,
)
from handlers.delete_callbacks import (
    handle_category_deletion,
    handle_delete_confirm,
    handle_delete_ref,
    handle_delete_summary,
    handle_item_deletion,
)

# Search handlers
from handlers.search_handlers import (
    get_search_conversation_handler,
    search_categories_pagination_callback,
    search_category_courses_pagination_callback,
    search_courses_pagination_callback,
)

logger = logging.getLogger(__name__)


# ---------- helpers ----------


# ---------- application factory ----------


async def create_application():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        msg = "BOT_TOKEN environment variable is not set"
        raise ValueError(msg)
    try:
        from telegram.ext import AIORateLimiter

        # All AIORateLimiter limits are env-configurable so deployments can
        # tune them without code changes. Defaults are deliberately relaxed:
        # the per-chat (group) limit is raised from 18/60s to 120/60s (2 API
        # calls/s per chat = ~60 clicks/min, far beyond human pagination speed)
        # because each pagination click = answer + edit = 2 API calls and an
        # aggressive group limit throttles rapid search pagination. A non-zero
        # value is kept (rather than disabling it) so one fast user cannot
        # consume the shared global budget and throttle other users.
        overall_max_rate = env_float("RATE_LIMIT_OVERALL_MAX_RATE", 30.0)
        overall_time_period = env_float("RATE_LIMIT_OVERALL_TIME_PERIOD", 1.0)
        group_max_rate = env_float("RATE_LIMIT_GROUP_MAX_RATE", 120.0)
        group_time_period = env_float("RATE_LIMIT_GROUP_TIME_PERIOD", 60.0)
        max_retries = env_int("RATE_LIMIT_MAX_RETRIES", 5)

        rate_limiter = AIORateLimiter(
            overall_max_rate=overall_max_rate,
            overall_time_period=overall_time_period,
            group_max_rate=group_max_rate,
            group_time_period=group_time_period,
            max_retries=max_retries,
        )
        application = Application.builder().token(bot_token).rate_limiter(rate_limiter).build()
        logger.info(
            "AIORateLimiter enabled (%.0f/s global, %.0f/%ss per-chat, max_retries=%d)",
            overall_max_rate,
            group_max_rate,
            group_time_period,
            max_retries,
        )
    except Exception:
        # aiolimiter may not be installed; fall back to unthrottled
        logger.warning("Failed to create AIORateLimiter; falling back to unthrottled")
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
    # /start is handled in setup_course_handlers (plain CommandHandler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("courses", list_courses))
    application.add_handler(CommandHandler("categories", list_categories))
    application.add_handler(CommandHandler("debug_db", debug_db))
    application.add_handler(CommandHandler("delete_category", delete_category_start))
    application.add_handler(CommandHandler("delete_parent", delete_parent_start))
    # Global /cancel handler is registered after conversations so ConversationHandler
    # fallbacks get first chance to handle the command.
    # Note: `create_parent` is handled via the ConversationHandler below

    # ---------- callbacks ----------
    application.add_handler(CallbackQueryHandler(confirm_delete_all, pattern="^confirm_delete_all$"))
    application.add_handler(CallbackQueryHandler(cancel_delete_all_data, pattern="^cancel_delete_all$"))
    application.add_handler(CallbackQueryHandler(courses_callback, pattern=r"^courses::"))
    # Stored page refs (courses_ref::<key>) generated by build_courses_page when
    # store_page_ref=True; also used for long category paths in quick-Next buttons.
    application.add_handler(CallbackQueryHandler(courses_callback, pattern=r"^courses_ref::"))
    application.add_handler(CallbackQueryHandler(addcoach_page, pattern=r"^addcoach_page::"))
    # Compact ref form for long Arabic parent names in the add-coach flow
    application.add_handler(CallbackQueryHandler(addcoach_page, pattern=r"^addcoach_page_ref::"))
    application.add_handler(CallbackQueryHandler(addcat_page, pattern=r"^addcat_page::"))
    application.add_handler(CallbackQueryHandler(categories_page, pattern=r"^categories_page::"))
    application.add_handler(CallbackQueryHandler(createcat_page, pattern=r"^createcat_page::"))
    application.add_handler(CallbackQueryHandler(handle_category_selection, pattern=r"^category_"))
    application.add_handler(CallbackQueryHandler(handle_category_selection, pattern=r"^category::"))
    # Register the more specific coach-in-category handler before the generic coach handler
    application.add_handler(CallbackQueryHandler(show_coach_in_category, pattern=r"^coach_in_cat::"))
    application.add_handler(CallbackQueryHandler(show_coach_handler, pattern=r"^coach_"))
    application.add_handler(CallbackQueryHandler(showtype_handler, pattern=r"^showtype::"))
    # Compact ref form (long Arabic category/type names)
    application.add_handler(CallbackQueryHandler(showtype_handler, pattern=r"^showtype_ref::"))
    application.add_handler(CallbackQueryHandler(handle_course_selection, pattern=r"^course_"))
    application.add_handler(CallbackQueryHandler(handle_course_selection, pattern=r"^course::"))
    application.add_handler(CallbackQueryHandler(handle_course_selection, pattern=r"^course_ref::"))
    # per-item cancel handler (confirm is handled in the deletion section below)
    application.add_handler(
        CallbackQueryHandler(
            handle_cancel_delete_callback,
            pattern=r"^cancel_delete",
        ),
    )
    application.add_handler(CallbackQueryHandler(handle_back_to_cats, pattern=r"^back_to_cats$"))

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
            entry_points=[
                CommandHandler("create_category", create_category),
                CommandHandler("create_parent", create_parent),
            ],
            allow_reentry=True,
            states={
                CREATE_CAT_PARENT: [
                    CallbackQueryHandler(handle_create_category_parent, pattern=r"^createcat_parent::"),
                    # Compact ref form (long Arabic parent names)
                    CallbackQueryHandler(handle_create_category_parent, pattern=r"^createcat_parent_ref::"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_create_category_parent_text),
                ],
                CREATE_CAT_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        handle_category_name,
                    ),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            conversation_timeout=600,  # auto-end after 10 minutes of inactivity
        ),
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
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            conversation_timeout=300,  # auto-end after 5 minutes of inactivity
        ),
    )

    # ---------- deletion (last so not shadowed) ----------
    # Register page handler before the generic delete handler so
    # `delete_category_page::N` isn't captured by the generic pattern.
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_category_page,
            pattern=r"^delete_category_page::\d+$",
        ),
    )

    application.add_handler(
        CallbackQueryHandler(
            handle_delete_parent_page,
            pattern=r"^delete_parent_page::\d+$",
        ),
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_category_deletion,
            pattern=r"^delete_category_",
        ),
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_item_deletion,
            pattern=r"^delete_item_",
        ),
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_item_deletion,
            pattern=r"^delete_item::",
        ),
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_confirm,
            pattern=r"^delete_confirm::",
        ),
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_summary,
            pattern=r"^delete_summary::",
        ),
    )
    # Details-view "Delete Course" button (delete_ref::<key> payload ref)
    application.add_handler(
        CallbackQueryHandler(
            handle_delete_ref,
            pattern=r"^delete_ref::",
        ),
    )
    application.add_handler(CallbackQueryHandler(showcat_handler, pattern=r"^showcat::[^:]+::\d+$"))
    # Support short stored refs for showcat links (avoids long callback_data)
    application.add_handler(CallbackQueryHandler(showcat_handler, pattern=r"^showcat_ref::"))
    application.add_handler(CallbackQueryHandler(show_coach_in_category, pattern=r"^coach_in_cat_ref::"))
    # Generic showcat handler (catch-all) registered after the paged pattern
    application.add_handler(CallbackQueryHandler(showcat_handler, pattern=r"^showcat::"))

    # ---------- category designs ----------
    try:
        setup_design_handlers(application)
    except Exception:
        logger.exception("Failed to register design handlers")

    # ---------- search pagination handlers ----------
    application.add_handler(CallbackQueryHandler(search_courses_pagination_callback, pattern=r"^search_courses_pg::"))
    application.add_handler(
        CallbackQueryHandler(search_categories_pagination_callback, pattern=r"^search_categories_pg::"),
    )
    application.add_handler(
        CallbackQueryHandler(search_category_courses_pagination_callback, pattern=r"^search_cat_courses_pg::"),
    )

    # ---------- error handler ----------
    application.add_error_handler(course_error_handler)
