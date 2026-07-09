"""
Search handlers for the Telegram bot paginated interface.

Provides a ConversationHandler-based search flow that works across
the /courses, /categories, and per-category course views. The user
clicks a 🔍 Search button, types a query, and gets paginated results
rendered using the same builders as the normal browsing views.
"""

import logging
import math
import urllib.parse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    CallbackContext,
)

from conversation_states import SEARCH_QUERY
from handlers.db_connection import get_db
from handlers.base_handlers import (
    safe_edit_message,
    safe_answer,
    build_courses_page,
    _store_callback_payload,
    _shorten_showcat_cb,
    _get_cached_page,
    _set_cached_page,
    _db_timing,
    _has_real_courses,
    PAGE_SIZE,
    _redis,
)

from handlers.atlas_search import (
    execute_category_search,
    execute_course_search,
    execute_category_course_search,
)

logger = logging.getLogger(__name__)

# ---------------  helper: extract only course rows from build_courses_page keyboard  ---------------

def _extract_course_rows(existing_kb: list) -> list:
    """Filter out breadcrumb, pagination, and back-button rows from
    `build_courses_page` output, keeping only the actual course rows.

    Course rows have exactly 2 buttons where the first button has a URL
    (the course name/link). All other rows (Home, ⏭️ End, ⬅️ Previous,
    ➡️ Next, 🔙 Back) are stripped so search-specific nav can be added.
    """
    if not existing_kb:
        return []
    try:
        return [
            row for row in existing_kb
            if len(row) == 2 and row[0].url
        ]
    except Exception:
        return list(existing_kb)


# ---------------  callback entry points  ---------------

async def search_courses_callback(update: Update, context: CallbackContext):
    """🔍 Search button clicked from global courses view.

    Callback data: search_courses::<origin_type>::<context>::<page>
    """
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split("::")
    # parts[0] == 'search_courses', parts[1] == origin_type, parts[2] == context, parts[3] == page
    origin_type = parts[1] if len(parts) > 1 else "global"
    origin_context = parts[2] if len(parts) > 2 else ""
    origin_page = parts[3] if len(parts) > 3 else "1"
    try:
        origin_page = int(origin_page)
    except Exception:
        origin_page = 1

    context.user_data["search_origin_type"] = origin_type
    context.user_data["search_origin_context"] = origin_context
    context.user_data["search_origin_page"] = origin_page
    context.user_data["search_mode"] = "courses"

    await safe_edit_message(
        query,
        "🔍 Please enter your search query to find courses (or /cancel to cancel):",
        action_key=getattr(query, "data", None),
    )
    return SEARCH_QUERY


async def search_categories_callback(update: Update, context: CallbackContext):
    """🔍 Search button clicked from categories view.

    Callback data: search_categories::<page>
    """
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split("::")
    origin_page = parts[1] if len(parts) > 1 else "1"
    try:
        origin_page = int(origin_page)
    except Exception:
        origin_page = 1

    context.user_data["search_origin_page"] = origin_page
    context.user_data["search_mode"] = "categories"

    await safe_edit_message(
        query,
        "🔍 Please enter your search query to find categories (or /cancel to cancel):",
        action_key=getattr(query, "data", None),
    )
    return SEARCH_QUERY


async def search_category_courses_callback(update: Update, context: CallbackContext):
    """🔍 Search button clicked from a specific category's course list.

    Callback data: search_category_courses::<category_name>::<page>
    """
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split("::")
    category_name = urllib.parse.unquote_plus(parts[1]) if len(parts) > 1 else ""
    origin_page = parts[2] if len(parts) > 2 else "1"
    try:
        origin_page = int(origin_page)
    except Exception:
        origin_page = 1

    context.user_data["search_category"] = category_name
    context.user_data["search_origin_page"] = origin_page
    context.user_data["search_mode"] = "category_courses"

    await safe_edit_message(
        query,
        f"🔍 Please enter your search query to find courses in '{category_name}' (or /cancel to cancel):",
        action_key=getattr(query, "data", None),
    )
    return SEARCH_QUERY


# ---------------  text input handler  ---------------

async def handle_search_input(update: Update, context: CallbackContext):
    """Process the user's search query text."""
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("Search query cannot be empty. Please try again or /cancel.")
        return SEARCH_QUERY

    mode = context.user_data.get("search_mode", "courses")

    if mode == "categories":
        await _perform_category_search(update, context, query_text)
    elif mode == "category_courses":
        category = context.user_data.get("search_category", "")
        await _perform_category_course_search(update, context, query_text, category)
    else:
        # Default: global course search
        await _perform_course_search(update, context, query_text)

    # Clear search state but remember last search so user can refine
    context.user_data["last_search_query"] = query_text
    context.user_data["last_search_mode"] = mode
    context.user_data.pop("search_mode", None)
    return ConversationHandler.END


# ---------------  search implementations  ---------------

async def _perform_category_search(update: Update, context: CallbackContext, query_text: str):
    """Search categories by name, return paginated results."""
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        page = 1

        page_cats, total, have_more = await execute_category_search(
            db, query_text, page=page, page_size=page_size
        )

        if not page_cats:
            await update.message.reply_text(
                f"No categories found matching '{query_text}'. 😕\n\n"
                "Try a different search term or use /categories to browse."
            )
            return

        # Build the same paginated keyboard as categories_page
        keyboard = []
        for cat in page_cats:
            cat_path = cat.get("path") or cat.get("name")
            payload = {"type": "showcat", "path": cat_path, "from_parent": "categories", "parent_page": page}
            key = _store_callback_payload(payload)
            cb = f"showcat_ref::{key}"
            display_name = cat.get("name") if isinstance(cat, dict) else str(cat)
            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        total_pages = max(1, math.ceil(total / page_size))
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_categories_pg::{query_text}::{page-1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_categories_pg::{query_text}::{page+1}"))
        if nav:
            keyboard.append(nav)

        # Back to /categories
        keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="back_to_cats")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        title = f"🔍 Results for '{query_text}' in categories (page {page}/{total_pages}):"
        await update.message.reply_text(title, reply_markup=reply_markup)

    except Exception as e:
        logger.exception("Error searching categories: %s", e)
        await update.message.reply_text("An error occurred while searching. Please try again.")


async def _perform_course_search(update: Update, context: CallbackContext, query_text: str):
    """Search all courses by name across all categories, return paginated results."""
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        page = 1

        course_items, total, have_more = await execute_course_search(
            db, query_text, page=page, page_size=page_size
        )

        if total == 0:
            await update.message.reply_text(
                f"No courses found matching '{query_text}'. 😕\n\n"
                "Try a different search term or use /courses to browse all courses."
            )
            return

        # Render using the standard courses page builder
        text, reply_markup = build_courses_page(
            course_items,
            page=page,
            origin_type="global",
            origin_context=None,
            total_count=total,
            is_page=True,
            store_page_ref=False,
        )

        if text is None:
            await update.message.reply_text("No courses found matching your query. 😕")
            return

        # Strip non-course rows from the builder's output, keeping only
        # actual course entries. Then add search-specific navigation.
        existing_kb = _extract_course_rows(
            list(reply_markup.inline_keyboard) if reply_markup else []
        )
        total_pages = max(1, math.ceil(total / page_size))

        # Build the search navigation row
        search_nav = []
        if page > 1:
            search_nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_courses_pg::{query_text}::{page-1}"))
        if page < total_pages:
            search_nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_courses_pg::{query_text}::{page+1}"))
        if search_nav:
            existing_kb.append(search_nav)

        # Back to global courses
        existing_kb.append([InlineKeyboardButton("🔙 Back to Courses", callback_data="courses::global::1")])

        # Rebuild title to show search context
        search_title = f"🔍 Results for '{query_text}' in courses (page {page}/{total_pages}):"
        await update.message.reply_text(search_title, reply_markup=InlineKeyboardMarkup(existing_kb))

    except Exception as e:
        logger.exception("Error searching courses: %s", e)
        await update.message.reply_text("An error occurred while searching. Please try again.")


async def _perform_category_course_search(update: Update, context: CallbackContext, query_text: str, category: str):
    """Search courses by name within a specific category, return paginated results."""
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        page = 1

        course_items, total, have_more = await execute_category_course_search(
            db, query_text, category, page=page, page_size=page_size
        )

        if total == 0:
            await update.message.reply_text(
                f"No courses found matching '{query_text}' in category '{category}'. 😕"
            )
            return

        text, reply_markup = build_courses_page(
            course_items,
            page=page,
            origin_type="category",
            category=category,
            origin_context="categories",
            origin_context_page=1,
            total_count=total,
            is_page=True,
            store_page_ref=False,
        )

        if text is None:
            await update.message.reply_text(
                f"No courses found matching '{query_text}' in category '{category}'. 😕"
            )
            return

        existing_kb = _extract_course_rows(
            list(reply_markup.inline_keyboard) if reply_markup else []
        )
        total_pages = max(1, math.ceil(total / page_size))

        search_nav = []
        if page > 1:
            search_nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_cat_courses_pg::{urllib.parse.quote_plus(category)}::{query_text}::{page-1}"))
        if page < total_pages:
            search_nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_cat_courses_pg::{urllib.parse.quote_plus(category)}::{query_text}::{page+1}"))
        if search_nav:
            existing_kb.append(search_nav)

        search_title = f"🔍 Results for '{query_text}' in '{category}' (page {page}/{total_pages}):"
        await update.message.reply_text(search_title, reply_markup=InlineKeyboardMarkup(existing_kb))

    except Exception as e:
        logger.exception("Error searching category courses: %s", e)
        await update.message.reply_text("An error occurred while searching. Please try again.")


# ---------------  pagination for search results  ---------------

async def search_courses_pagination_callback(update: Update, context: CallbackContext):
    """Handle pagination for global course search results."""
    query = update.callback_query
    await safe_answer(query)
    # Format: search_courses_pg::<query>::<page>
    parts = query.data.split("::")
    if len(parts) < 3:
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
        return
    query_text = parts[1]
    try:
        page = int(parts[2])
    except Exception:
        page = 1

    try:
        db = await get_db()
        if db is None:
            await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
            return

        page_size = PAGE_SIZE

        course_items, total, have_more = await execute_course_search(
            db, query_text, page=page, page_size=page_size
        )

        text, reply_markup = build_courses_page(
            course_items,
            page=page,
            origin_type="global",
            total_count=total,
            is_page=True,
            store_page_ref=False,
        )

        existing_kb = _extract_course_rows(
            list(reply_markup.inline_keyboard) if reply_markup else []
        )
        total_pages = max(1, math.ceil(total / page_size))

        search_nav = []
        if page > 1:
            search_nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_courses_pg::{query_text}::{page-1}"))
        if page < total_pages:
            search_nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_courses_pg::{query_text}::{page+1}"))
        if search_nav:
            existing_kb.append(search_nav)

        existing_kb.append([InlineKeyboardButton("🔙 Back to Courses", callback_data="courses::global::1")])

        search_title = f"🔍 Results for '{query_text}' in courses (page {page}/{total_pages}):"
        await safe_edit_message(query, search_title, reply_markup=InlineKeyboardMarkup(existing_kb), action_key=getattr(query, "data", None))

    except Exception as e:
        logger.exception("Error paginating course search: %s", e)
        await safe_edit_message(query, "An error occurred while loading search results.", action_key=getattr(query, "data", None))


async def search_categories_pagination_callback(update: Update, context: CallbackContext):
    """Handle pagination for category search results."""
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split("::")
    if len(parts) < 3:
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
        return
    query_text = parts[1]
    try:
        page = int(parts[2])
    except Exception:
        page = 1

    try:
        db = await get_db()
        if db is None:
            await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
            return

        page_size = PAGE_SIZE

        page_cats, total, have_more = await execute_category_search(
            db, query_text, page=page, page_size=page_size
        )

        keyboard = []
        for cat in page_cats:
            cat_path = cat.get("path") or cat.get("name")
            payload = {"type": "showcat", "path": cat_path, "from_parent": "categories", "parent_page": page}
            key = _store_callback_payload(payload)
            cb = f"showcat_ref::{key}"
            display_name = cat.get("name") if isinstance(cat, dict) else str(cat)
            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        total_pages = max(1, math.ceil(total / page_size))
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_categories_pg::{query_text}::{page-1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_categories_pg::{query_text}::{page+1}"))
        if nav:
            keyboard.append(nav)

        keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="back_to_cats")])

        await safe_edit_message(
            query,
            f"🔍 Results for '{query_text}' in categories (page {page}/{total_pages}):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            action_key=getattr(query, "data", None),
        )

    except Exception as e:
        logger.exception("Error paginating category search: %s", e)
        await safe_edit_message(query, "An error occurred while loading search results.", action_key=getattr(query, "data", None))


async def search_category_courses_pagination_callback(update: Update, context: CallbackContext):
    """Handle pagination for category-specific course search results."""
    query = update.callback_query
    await safe_answer(query)
    # Format: search_cat_courses_pg::<category_encoded>::<query>::<page>
    parts = query.data.split("::")
    if len(parts) < 4:
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
        return
    category = urllib.parse.unquote_plus(parts[1])
    query_text = parts[2]
    try:
        page = int(parts[3])
    except Exception:
        page = 1

    try:
        db = await get_db()
        if db is None:
            await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
            return

        page_size = PAGE_SIZE

        course_items, total, have_more = await execute_category_course_search(
            db, query_text, category, page=page, page_size=page_size
        )

        text, reply_markup = build_courses_page(
            course_items,
            page=page,
            origin_type="category",
            category=category,
            origin_context="categories",
            origin_context_page=1,
            total_count=total,
            is_page=True,
            store_page_ref=False,
        )

        existing_kb = _extract_course_rows(
            list(reply_markup.inline_keyboard) if reply_markup else []
        )
        total_pages = max(1, math.ceil(total / page_size))

        search_nav = []
        if page > 1:
            search_nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_cat_courses_pg::{urllib.parse.quote_plus(category)}::{query_text}::{page-1}"))
        if page < total_pages:
            search_nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_cat_courses_pg::{urllib.parse.quote_plus(category)}::{query_text}::{page+1}"))
        if search_nav:
            existing_kb.append(search_nav)

        await safe_edit_message(
            query,
            f"🔍 Results for '{query_text}' in '{category}' (page {page}/{total_pages}):",
            reply_markup=InlineKeyboardMarkup(existing_kb),
            action_key=getattr(query, "data", None),
        )

    except Exception as e:
        logger.exception("Error paginating category course search: %s", e)
        await safe_edit_message(query, "An error occurred while loading search results.", action_key=getattr(query, "data", None))


# ---------------  cancel handler  ---------------

async def search_cancel(update: Update, context: CallbackContext):
    """Cancel the search operation."""
    context.user_data.pop("search_mode", None)
    context.user_data.pop("search_category", None)
    context.user_data.pop("search_origin_type", None)
    context.user_data.pop("search_origin_context", None)
    context.user_data.pop("search_origin_page", None)

    try:
        if update.callback_query:
            await safe_edit_message(update.callback_query, "Search canceled.", action_key=getattr(update.callback_query, "data", None))
        elif update.message:
            await update.message.reply_text("Search canceled.")
    except Exception:
        pass

    return ConversationHandler.END


# ---------------  conversation handler  ---------------

def get_search_conversation_handler() -> ConversationHandler:
    """Return the ConversationHandler for the search flow."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(search_courses_callback, pattern=r"^search_courses::"),
            CallbackQueryHandler(search_categories_callback, pattern=r"^search_categories::"),
            CallbackQueryHandler(search_category_courses_callback, pattern=r"^search_category_courses::"),
        ],
        states={
            SEARCH_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", search_cancel),
            CallbackQueryHandler(search_cancel, pattern=r"^search_cancel$"),
        ],
        name="search_conversation",
        persistent=False,
    )
