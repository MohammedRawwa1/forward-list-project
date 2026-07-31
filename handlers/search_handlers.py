"""Search handlers for the Telegram bot paginated interface.

Provides a ConversationHandler-based search flow that works across
the /courses, /categories, and per-category course views. The user
clicks a 🔍 Search button, types a query, and gets paginated results
rendered using the same builders as the normal browsing views.
"""

import logging
import math
import re
import urllib.parse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from conversation_states import SEARCH_QUERY
from handlers.atlas_search import (
    execute_category_course_search,
    execute_category_search,
    execute_course_search,
)
from handlers.base_handlers import (
    PAGE_SIZE,
    _resolve_callback_payload,
    _store_callback_payload,
    build_courses_page,
    safe_answer,
    safe_edit_message,
)
from handlers.db_connection import get_db

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
        return [row for row in existing_kb if len(row) == 2 and row[0].url]
    except Exception:
        return list(existing_kb)


# ---------------  search query refs (keep callback_data <= 64 bytes)  ---------------

# Telegram limits callback_data to 64 bytes. Embedding the raw query text in
# pagination callbacks (e.g. ``search_courses_pg::<query>::<page>``) breaks
# once the query is long or non-ASCII (Arabic queries are common here): the
# payload exceeds the limit and Telegram rejects the whole results message,
# so the search appears to "not return" anything. Storing the query in the
# callback payload keeps every button compact and stable across pages.

_SEARCH_REF_PATTERN = re.compile(r"^[0-9a-fA-F]{16}$")


def _search_query_ref(query_text: str) -> str:
    """Store a search query and return a compact 16-char reference."""
    return _store_callback_payload({"type": "search_query", "q": query_text})


def _category_search_ref(query_text: str, category: str) -> str:
    """Like ``_search_query_ref`` but also carries the category scope so the
    category-courses pagination callback stays compact for long names."""
    return _store_callback_payload({"type": "search_query", "q": query_text, "category": category})


async def _resolve_search_query(raw: str):
    """Resolve a query from a callback segment.

    New-style callbacks store a 16-char ref; legacy in-flight callbacks embed
    the raw query text. Returns the resolved query, or the raw text when it
    isn't a stored ref.
    """
    if raw and len(raw) == 16 and _SEARCH_REF_PATTERN.match(raw):
        try:
            payload = await _resolve_callback_payload(raw)
            if payload and payload.get("type") == "search_query":
                q = payload.get("q")
                if q:
                    return q
        except Exception:
            pass
        # Ref-pattern segment that couldn't be resolved (stale in-flight button
        # after a restart, pruned map, Redis/Mongo miss) → let callers fall back
        # to user_data instead of searching for the raw hex key.
        return ""
    return raw


# ---------------  callback entry points  ---------------


async def search_courses_callback(update: Update, context: CallbackContext):
    """🔍 Search button clicked from global courses view.

    Callback data: search_courses::<origin_type>::<context>::<page>
    or the compact ref form for long coach names:
    search_courses_coach_ref::<key16>
    """
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    origin_type = "global"
    origin_context = ""
    origin_page = 1
    if data.startswith("search_courses_coach_ref::"):
        # Compact ref form used when the coach name exceeds the 64-byte
        # callback_data limit (long Arabic coach names).
        try:
            payload = await _resolve_callback_payload(data.split("::", 1)[1])
            if payload:
                origin_type = "coach"
                origin_context = payload.get("coach") or ""
                try:
                    origin_page = int(payload.get("page") or 1)
                except Exception:
                    origin_page = 1
        except Exception:
            origin_type = "global"
            origin_context = ""
            origin_page = 1
    else:
        parts = data.split("::")
        origin_type = parts[1] if len(parts) > 1 else "global"
        origin_context = parts[2] if len(parts) > 2 else ""
        try:
            origin_page = int(parts[3]) if len(parts) > 3 else 1
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
    or the compact ref form (long Arabic category names):
    search_category_courses_ref::<key16>
    """
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    category_name = ""
    origin_page = 1
    if data.startswith("search_category_courses_ref::"):
        # Compact ref form: category name was too long to embed inline.
        try:
            key = data.split("::", 1)[1]
            payload = await _resolve_callback_payload(key)
            if payload:
                category_name = payload.get("category") or ""
                try:
                    origin_page = int(payload.get("page") or 1)
                except Exception:
                    origin_page = 1
        except Exception:
            category_name = ""
            origin_page = 1
    else:
        parts = data.split("::")
        category_name = urllib.parse.unquote_plus(parts[1]) if len(parts) > 1 else ""
        try:
            origin_page = int(parts[2]) if len(parts) > 2 else 1
        except Exception:
            origin_page = 1

    if not category_name:
        # Stale ref (bot restart / pruned callback map): fall back to the
        # last known category for this chat so the prompt stays meaningful.
        category_name = context.user_data.get("search_category", "")

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
    keyboard = []  # defensive initialization
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        page = 1

        page_cats, total, _ = await execute_category_search(db, query_text, page=page, page_size=page_size)

        if not page_cats:
            await update.message.reply_text(
                f"No categories found matching '{query_text}'. 😕\n\n"
                "Try a different search term or use /categories to browse.",
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
            # Show parent indicator if this category has a parent
            parent_name = cat.get("parent") if isinstance(cat, dict) else None
            if parent_name:
                display_name = f"{display_name} › ({parent_name})"
            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        total_pages = max(1, math.ceil(total / page_size))
        nav = []
        search_ref = _search_query_ref(query_text)
        if page > 1:
            nav.append(
                InlineKeyboardButton("⬅️ Previous", callback_data=f"search_categories_pg::{search_ref}::{page - 1}"),
            )
        if page < total_pages:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_categories_pg::{search_ref}::{page + 1}"))
        if nav:
            keyboard.append(nav)

        # Back to /categories
        keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="back_to_cats")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        title = f"🔍 Results for '{query_text}' in all categories (page {page}/{total_pages}):"
        await update.message.reply_text(title, reply_markup=reply_markup)

    except Exception:
        logger.exception("Error searching categories")
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

        course_items, total, _ = await execute_course_search(db, query_text, page=page, page_size=page_size)

        if total == 0:
            await update.message.reply_text(
                f"No courses found matching '{query_text}'. 😕\n\n"
                "Try a different search term or use /courses to browse all courses.",
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
        existing_kb = _extract_course_rows(list(reply_markup.inline_keyboard) if reply_markup else [])
        total_pages = max(1, math.ceil(total / page_size))

        # Build the search navigation row
        search_nav = []
        search_ref = _search_query_ref(query_text)
        if page > 1:
            search_nav.append(
                InlineKeyboardButton("⬅️ Previous", callback_data=f"search_courses_pg::{search_ref}::{page - 1}"),
            )
        if page < total_pages:
            search_nav.append(
                InlineKeyboardButton("➡️ Next", callback_data=f"search_courses_pg::{search_ref}::{page + 1}"),
            )
        if search_nav:
            existing_kb.append(search_nav)

        # Back to global courses
        existing_kb.append([InlineKeyboardButton("🔙 Back to Courses", callback_data="courses::global::1")])

        # Rebuild title to show search context
        search_title = f"🔍 Results for '{query_text}' in courses (page {page}/{total_pages}):"
        await update.message.reply_text(search_title, reply_markup=InlineKeyboardMarkup(existing_kb))

    except Exception:
        logger.exception("Error searching courses")
        await update.message.reply_text("An error occurred while searching. Please try again.")


async def _perform_category_course_search(update: Update, context: CallbackContext, query_text: str, category: str):
    keyboard = []  # defensive initialization
    """Search courses by name within a specific category, return paginated results."""
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        page = 1

        course_items, total, _ = await execute_category_course_search(
            db,
            query_text,
            category,
            page=page,
            page_size=page_size,
            include_children=True,
        )

        # Also search for child categories matching the query within this parent
        child_cats_matched = []
        try:
            child_cat_results, _, _ = await execute_category_search(
                db,
                query_text,
                page=1,
                page_size=5,
                parent=category,
            )
            if child_cat_results:
                # Only include actual children (parent matches the category)
                child_cats_matched = [c for c in child_cat_results if c.get("parent") == category]
        except Exception:
            child_cats_matched = []

        # Also search for coaches matching the query within this category/children
        coach_courses_matched = []
        try:
            # Use global course search but filter by coach name match within this category's scope
            coach_items, _, _ = await execute_course_search(db, query_text, page=1, page_size=10)
            # Keep only courses whose coach matches AND are in this category or its children
            if coach_items:
                coach_courses_matched = [
                    c
                    for c in coach_items
                    if c.get("coach")
                    and query_text.lower() in c.get("coach", "").lower()
                    and (
                        c.get("category") == category
                        or c.get("category") in [cc.get("name") for cc in child_cats_matched]
                    )
                ]
        except Exception:
            coach_courses_matched = []

        if total == 0 and not child_cats_matched and not coach_courses_matched:
            await update.message.reply_text(
                f"No results found matching '{query_text}' in category '{category}' or its subcategories. 😕",
            )
            return

        # Build the results keyboard
        keyboard = []

        # Add matching child categories as navigation buttons (page 1 only)
        if page == 1:
            for child_cat in child_cats_matched[:5]:
                child_path = child_cat.get("path") or child_cat.get("name")
                payload = {"type": "showcat", "path": child_path, "from_parent": category, "parent_page": 1}
                key = _store_callback_payload(payload)
                keyboard.append(
                    [InlineKeyboardButton(f"📁 {child_cat.get('name')}", callback_data=f"showcat_ref::{key}")],
                )

        # Add matching courses by coach
        if page == 1 and coach_courses_matched:
            for c in coach_courses_matched[:5]:
                link = c.get("link")
                name = c.get("name")
                if name and link:
                    keyboard.append(
                        [
                            InlineKeyboardButton(f"👨‍🏫 {name} ({c.get('coach')})", url=link),
                        ],
                    )

        # Add matching courses
        if total > 0:
            _, reply_markup = build_courses_page(
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

            existing_kb = _extract_course_rows(list(reply_markup.inline_keyboard) if reply_markup else [])
            keyboard.extend(existing_kb)

            total_pages = max(1, math.ceil(total / page_size))

            search_nav = []
            search_ref = _category_search_ref(query_text, category)
            if page > 1:
                search_nav.append(
                    InlineKeyboardButton(
                        "⬅️ Previous",
                        callback_data=f"search_cat_courses_pg::{search_ref}::{page - 1}",
                    ),
                )
            if page < total_pages:
                search_nav.append(
                    InlineKeyboardButton(
                        "➡️ Next",
                        callback_data=f"search_cat_courses_pg::{search_ref}::{page + 1}",
                    ),
                )
            if search_nav:
                keyboard.append(search_nav)
        else:
            total_pages = 1

        search_title = f"🔍 Results for '{query_text}' in '{category}' incl. subcategories (page {page}/{total_pages}):"
        await update.message.reply_text(search_title, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception:
        logger.exception("Error searching category courses")
        await update.message.reply_text("An error occurred while searching. Please try again.")


# ---------------  pagination for search results  ---------------


async def search_courses_pagination_callback(update: Update, context: CallbackContext):
    """Handle pagination for global course search results."""
    query = update.callback_query
    await safe_answer(query)
    # Format: search_courses_pg::<ref16>::<page> (or legacy ::<query>::<page>)
    parts = query.data.split("::")
    if len(parts) < 3:
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
        return
    query_text = await _resolve_search_query(parts[1])
    if not query_text:
        query_text = context.user_data.get("last_search_query", "")
    if not query_text:
        await safe_edit_message(
            query,
            "Search query is no longer available. Please search again.",
            action_key=getattr(query, "data", None),
        )
        return
    try:
        page = int(parts[2])
    except Exception:
        page = 1

    try:
        db = await get_db()
        if db is None:
            await safe_edit_message(
                query,
                "Error: Unable to connect to the database.",
                action_key=getattr(query, "data", None),
            )
            return

        page_size = PAGE_SIZE

        course_items, total, _ = await execute_course_search(db, query_text, page=page, page_size=page_size)

        if total == 0:
            await safe_edit_message(
                query,
                f"No courses found matching '{query_text}'. 😕",
                action_key=getattr(query, "data", None),
            )
            return

        _, reply_markup = build_courses_page(
            course_items,
            page=page,
            origin_type="global",
            total_count=total,
            is_page=True,
            store_page_ref=False,
        )

        existing_kb = _extract_course_rows(list(reply_markup.inline_keyboard) if reply_markup else [])
        total_pages = max(1, math.ceil(total / page_size))

        search_nav = []
        search_ref = _search_query_ref(query_text)
        if page > 1:
            search_nav.append(
                InlineKeyboardButton("⬅️ Previous", callback_data=f"search_courses_pg::{search_ref}::{page - 1}"),
            )
        if page < total_pages:
            search_nav.append(
                InlineKeyboardButton("➡️ Next", callback_data=f"search_courses_pg::{search_ref}::{page + 1}"),
            )
        if search_nav:
            existing_kb.append(search_nav)

        existing_kb.append([InlineKeyboardButton("🔙 Back to Courses", callback_data="courses::global::1")])

        search_title = f"🔍 Results for '{query_text}' in courses (page {page}/{total_pages}):"
        await safe_edit_message(
            query,
            search_title,
            reply_markup=InlineKeyboardMarkup(existing_kb),
            action_key=getattr(query, "data", None),
        )

    except Exception:
        logger.exception("Error paginating course search")
        await safe_edit_message(
            query,
            "An error occurred while loading search results.",
            action_key=getattr(query, "data", None),
        )


async def search_categories_pagination_callback(update: Update, context: CallbackContext):
    """Handle pagination for category search results."""
    query = update.callback_query
    await safe_answer(query)
    # Format: search_categories_pg::<ref16>::<page> (or legacy ::<query>::<page>)
    parts = query.data.split("::")
    if len(parts) < 3:
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
        return
    query_text = await _resolve_search_query(parts[1])
    if not query_text:
        query_text = context.user_data.get("last_search_query", "")
    if not query_text:
        await safe_edit_message(
            query,
            "Search query is no longer available. Please search again.",
            action_key=getattr(query, "data", None),
        )
        return
    try:
        page = int(parts[2])
    except Exception:
        page = 1

    try:
        db = await get_db()
        if db is None:
            await safe_edit_message(
                query,
                "Error: Unable to connect to the database.",
                action_key=getattr(query, "data", None),
            )
            return

        page_size = PAGE_SIZE

        page_cats, total, _ = await execute_category_search(db, query_text, page=page, page_size=page_size)

        if not page_cats:
            await safe_edit_message(
                query,
                f"No categories found matching '{query_text}'. 😕",
                action_key=getattr(query, "data", None),
            )
            return

        keyboard = []
        for cat in page_cats:
            cat_path = cat.get("path") or cat.get("name")
            payload = {"type": "showcat", "path": cat_path, "from_parent": "categories", "parent_page": page}
            key = _store_callback_payload(payload)
            cb = f"showcat_ref::{key}"
            display_name = cat.get("name") if isinstance(cat, dict) else str(cat)
            # Show parent indicator if this category has a parent
            parent_name = cat.get("parent") if isinstance(cat, dict) else None
            if parent_name:
                display_name = f"{display_name} › ({parent_name})"
            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        total_pages = max(1, math.ceil(total / page_size))
        nav = []
        search_ref = _search_query_ref(query_text)
        if page > 1:
            nav.append(
                InlineKeyboardButton("⬅️ Previous", callback_data=f"search_categories_pg::{search_ref}::{page - 1}"),
            )
        if page < total_pages:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_categories_pg::{search_ref}::{page + 1}"))
        if nav:
            keyboard.append(nav)

        keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="back_to_cats")])

        await safe_edit_message(
            query,
            f"🔍 Results for '{query_text}' in all categories (page {page}/{total_pages}):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            action_key=getattr(query, "data", None),
        )

    except Exception:
        logger.exception("Error paginating category search")
        await safe_edit_message(
            query,
            "An error occurred while loading search results.",
            action_key=getattr(query, "data", None),
        )


async def search_category_courses_pagination_callback(update: Update, context: CallbackContext):
    """Handle pagination for category-specific course search results."""
    query = update.callback_query
    await safe_answer(query)
    # New format: search_cat_courses_pg::<ref16>::<page>
    # Legacy format: search_cat_courses_pg::<category_encoded>::<query>::<page>
    parts = query.data.split("::")
    category = ""
    query_text = ""
    page_raw = ""
    if len(parts) == 3 and len(parts[1]) == 16 and _SEARCH_REF_PATTERN.match(parts[1]):
        try:
            payload = await _resolve_callback_payload(parts[1])
            if payload:
                category = payload.get("category") or ""
                query_text = payload.get("q") or ""
        except Exception:
            pass
        page_raw = parts[2]
    elif len(parts) >= 4:
        category = urllib.parse.unquote_plus(parts[1])
        query_text = await _resolve_search_query(parts[2])
        page_raw = parts[3]
    else:
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
        return
    if not query_text:
        query_text = context.user_data.get("last_search_query", "")
    if not category:
        category = context.user_data.get("search_category", "")
    if not query_text or not category:
        await safe_edit_message(
            query,
            "Search context is no longer available. Please search again.",
            action_key=getattr(query, "data", None),
        )
        return
    try:
        page = int(page_raw)
    except Exception:
        page = 1

    try:
        db = await get_db()
        if db is None:
            await safe_edit_message(
                query,
                "Error: Unable to connect to the database.",
                action_key=getattr(query, "data", None),
            )
            return

        page_size = PAGE_SIZE

        course_items, total, _ = await execute_category_course_search(
            db,
            query_text,
            category,
            page=page,
            page_size=page_size,
            include_children=True,
        )

        # Also search for child categories matching the query within this parent
        child_cats_matched = []
        try:
            child_cat_results, _, _ = await execute_category_search(
                db,
                query_text,
                page=1,
                page_size=5,
                parent=category,
            )
            if child_cat_results:
                child_cats_matched = [c for c in child_cat_results if c.get("parent") == category]
        except Exception:
            child_cats_matched = []

        # Also search for coaches matching the query within this category/children
        coach_courses_matched = []
        try:
            coach_items, _, _ = await execute_course_search(db, query_text, page=1, page_size=10)
            if coach_items:
                child_cat_names = {c.get("name") for c in child_cats_matched}
                coach_courses_matched = [
                    c
                    for c in coach_items
                    if c.get("coach")
                    and query_text.lower() in c.get("coach", "").lower()
                    and (c.get("category") == category or c.get("category") in child_cat_names)
                ]
        except Exception:
            coach_courses_matched = []

        # Build the results keyboard
        keyboard = []

        # Add matching child categories as navigation buttons (page 1 only)
        if page == 1:
            for child_cat in child_cats_matched[:5]:
                child_path = child_cat.get("path") or child_cat.get("name")
                payload = {"type": "showcat", "path": child_path, "from_parent": category, "parent_page": 1}
                key = _store_callback_payload(payload)
                keyboard.append(
                    [InlineKeyboardButton(f"📁 {child_cat.get('name')}", callback_data=f"showcat_ref::{key}")],
                )

        # Add matching courses by coach (page 1 only)
        if page == 1 and coach_courses_matched:
            for c in coach_courses_matched[:5]:
                name = c.get("name")
                link = c.get("link")
                if name and link:
                    keyboard.append(
                        [
                            InlineKeyboardButton(f"👨‍🏫 {name} ({c.get('coach')})", url=link),
                        ],
                    )

        if total > 0:
            _, reply_markup = build_courses_page(
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

            existing_kb = _extract_course_rows(list(reply_markup.inline_keyboard) if reply_markup else [])
            keyboard.extend(existing_kb)

            total_pages = max(1, math.ceil(total / page_size))

            search_nav = []
            search_ref = _category_search_ref(query_text, category)
            if page > 1:
                search_nav.append(
                    InlineKeyboardButton(
                        "⬅️ Previous",
                        callback_data=f"search_cat_courses_pg::{search_ref}::{page - 1}",
                    ),
                )
            if page < total_pages:
                search_nav.append(
                    InlineKeyboardButton(
                        "➡️ Next",
                        callback_data=f"search_cat_courses_pg::{search_ref}::{page + 1}",
                    ),
                )
            if search_nav:
                keyboard.append(search_nav)
        else:
            total_pages = 1

        if not keyboard:
            await safe_edit_message(
                query,
                f"No results found matching '{query_text}' in '{category}' or its subcategories. 😕",
                action_key=getattr(query, "data", None),
            )
            return

        await safe_edit_message(
            query,
            f"🔍 Results for '{query_text}' in '{category}' incl. subcategories (page {page}/{total_pages}):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            action_key=getattr(query, "data", None),
        )

    except Exception:
        logger.exception("Error paginating category course search")
        await safe_edit_message(
            query,
            "An error occurred while loading search results.",
            action_key=getattr(query, "data", None),
        )


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
            await safe_edit_message(
                update.callback_query,
                "Search canceled.",
                action_key=getattr(update.callback_query, "data", None),
            )
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
            # Compact ref form used when the coach name exceeds the
            # 64-byte callback_data limit (long Arabic coach names).
            CallbackQueryHandler(search_courses_callback, pattern=r"^search_courses_coach_ref::"),
            CallbackQueryHandler(search_categories_callback, pattern=r"^search_categories::"),
            CallbackQueryHandler(search_category_courses_callback, pattern=r"^search_category_courses::"),
            # Compact ref form used when the category name exceeds the
            # 64-byte callback_data limit (long Arabic category names).
            CallbackQueryHandler(search_category_courses_callback, pattern=r"^search_category_courses_ref::"),
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
