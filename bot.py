from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from handlers.base_handlers import (
    help,
    list_courses,
    list_coaches,
    list_categories,
    create_category,
    create_parent,
    handle_create_category_parent,
    get_courses_by_category,
    courses_callback,
    handle_categories_pagination,
    showtype_handler,
    showcat_handler,
    handle_course_selection,
    handle_category_name,
    handle_category_selection,
    handle_back_to_cats,
    show_coach_handler,
    show_coach_in_category,
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
    cancel,
)

from handlers.bot_handlers import (
    generate_pagination_keyboard,
    generate_keyboard,
    delete_item,
    delete_category,
    handle_course_deletion,
    handle_cancel_delete_callback,
    delete_item_start,
    delete_all_data_start,
    confirm_delete_all,
    cancel_delete_all_data,
    initiate_delete_item,
)

from conversation_states import (
    ADD_NAME,
    ADD_LINK,
    ADD_CATEGORY,
    CREATE_CAT_NAME,
    CREATE_CAT_PARENT,
    DELETE_ALL,
    CONFIRM_DELETE,
    CANCEL_DELETE,
    MAX_CATEGORY_NAME_LENGTH,
)

from handlers.delete_callbacks import handle_category_deletion, handle_item_deletion
from handlers.delete_callbacks import handle_delete_ref
from handlers.custom_thumbnail import add_thumb, del_thumb, setup_thumbnail_handlers

from dotenv import load_dotenv
import logging
import os
import re

load_dotenv()

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=log_level,
)
logger = logging.getLogger(__name__)


# ---------- helpers ----------
def is_valid_category_name(category_name: str):
    return bool(re.match(r"^[a-zA-Z0-9\s\-]+$", category_name))


# ---------- application factory ----------

async def create_application():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN environment variable is not set")
    application = Application.builder().token(bot_token).build()
    return application


# ---------- register handlers ----------
async def setup_handlers(application: Application):
    if not application:
        logger.error("Application is not initialised.")
        return

    # ---------- commands ----------
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help))
    application.add_handler(CommandHandler("courses", list_courses))
    application.add_handler(CommandHandler("categories", list_categories))
    # Note: `delete_category_start` not present; command removed until handler is added.
    application.add_handler(CommandHandler("addthumb", add_thumb))
    application.add_handler(CommandHandler("delthumb", del_thumb))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("create_parent", create_parent))

    # ---------- callbacks ----------
    # `del_menu_` callback handler not present; skip registration.
    application.add_handler(
        CallbackQueryHandler(confirm_delete_all, pattern="^confirm_delete_all$")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_delete_all_data, pattern="^cancel_delete_all$")
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_categories_pagination,
            pattern=r"^categories_(prev|next)_\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(courses_callback, pattern=r"^courses::")
    )
    application.add_handler(
        CallbackQueryHandler(handle_category_selection, pattern=r"^category_")
    )
    application.add_handler(
        CallbackQueryHandler(handle_category_selection, pattern=r"^category::")
    )
    application.add_handler(
        CallbackQueryHandler(show_coach_handler, pattern=r"^coach_")
    )
    application.add_handler(
        CallbackQueryHandler(show_coach_in_category, pattern=r"^coach_in_cat::")
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

    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("create_category", create_category), CommandHandler("create_parent", create_parent)],
            states={
                CREATE_CAT_PARENT: [
                    CallbackQueryHandler(handle_create_category_parent, pattern=r"^createcat_parent::")
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
        CallbackQueryHandler(showcat_handler, pattern=r"^showcat::")
    )

    # ---------- thumbnails ----------
    await setup_thumbnail_handlers(application)

    # ---------- error handler ----------
    application.add_error_handler(course_error_handler)
