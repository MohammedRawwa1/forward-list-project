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
    execute_coach_course_search,
    execute_course_search,
)
from handlers.base_handlers import (
    PAGE_SIZE,
    _resolve_callback_payload,
    _store_callback_payload,
    _store_search_nav_ref,
    build_courses_page,
    safe_answer,
    safe_edit_message,
)
from handlers.db_connection import get_db

logger = logging.getLogger(__name__)


# ---------------  helper: extract only course rows from build_courses_page keyboard  ---------------


def _extract_course_rows(existing_kb: list) -> list:
    if not existing_kb:
        return []
    try:
        return [row for row in existing_kb if len(row) == 2 and row[0].url]
    except Exception:
        return list(existing_kb)


# ---------------  search query refs (keep callback_data <= 64 bytes)  ---------------


_SEARCH_REF_PATTERN = re.compile(r"^[0-9a-fA-F]{16}$")


def _search_query_ref(query_text: str) -> str:
    return _store_callback_payload({"type": "search_query", "q": query_text})


def _category_search_ref(query_text: str, category: str) -> str:
    return _store_callback_payload({"type": "search_query", "q": query_text, "category": category})


def _search_results_ref(mode: str, query_text: str, page: int = 1, category: str = None) -> str:
    payload = {"type": "search_results", "mode": mode, "q": query_text, "page": int(page or 1)}
    if category:
        payload["category"] = category
    return _store_callback_payload(payload)


async def _resolve_search_query(raw: str):
    if raw and len(raw) == 16 and _SEARCH_REF_PATTERN.match(raw):
        try:
            payload = await _resolve_callback_payload(raw)
            if payload and payload.get("type") == "search_query":
                q = payload.get("q")
                if q:
                    return q
        except Exception:
            pass
        return ""
    return raw


# ---------------  normalization / dedup helpers  ---------------


def _course_key(c):
    try:
        cid = c.get("id")
        if cid:
            return ("id", str(cid))
        return ("course", c.get("name"), c.get("link"))
    except Exception:
        return ("raw", repr(c))


def _dedupe_courses(items) -> list:
    seen = set()
    out = []
    for c in items or []:
        key = _course_key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _dedupe_categories(items) -> list:
    seen = set()
    out = []
    for c in items or []:
        try:
            name = c.get("name") if isinstance(c, dict) else str(c)
            key = ("cat", name, c.get("path") if isinstance(c, dict) else None)
        except Exception:
            key = ("cat", str(c))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _category_result_button(cat, page: int, search_ref: str = None) -> list:
    cat_path = cat.get("path") or cat.get("name")
    payload = {"type": "showcat", "path": cat_path, "from_parent": "categories", "parent_page": page}
    if search_ref:
        payload["search_ref"] = search_ref
    key = _store_callback_payload(payload)
    cb = f"showcat_ref::{key}"
    display_name = cat.get("name") if isinstance(cat, dict) else str(cat)
    parent_name = cat.get("parent") if isinstance(cat, dict) else None
    if parent_name:
        display_name = f"{display_name} › ({parent_name})"
    return [InlineKeyboardButton(display_name, callback_data=cb)]


def _courses_back_ref(kind: str, name: str, page: int) -> str:
    try:
        key = _store_callback_payload(
            {
                "type": "courses_page",
                "origin_type": kind,
                "category": str(name),
                "page": int(page or 1),
                "origin_context": None,
                "origin_context_page": None,
                "total_count": None,
            },
        )
        return f"courses_ref::{key}"
    except Exception:
        quoted = urllib.parse.quote_plus(str(name))
        if kind == "category":
            return f"courses::category::{quoted}::{int(page or 1)}"
        return f"courses::coach::{quoted}::{int(page or 1)}"


def _origin_back_row(context, mode: str, category: str = None):
    try:
        ud = context.user_data
        has_origin = any(
            k in ud
            for k in ("search_origin_type", "search_origin_context", "search_origin_page", "search_category")
        )
        if not has_origin:
            return None
        origin_type = ud.get("search_origin_type", "global")
        origin_context = ud.get("search_origin_context") or ""
        try:
            origin_page = int(ud.get("search_origin_page") or 1)
        except Exception:
            origin_page = 1
    except Exception:
        return None
    try:
        if mode == "categories":
            back_cb = f"categories_page::{origin_page}"
            label = "🔙 Back to Categories"
        elif mode == "category_courses":
            cat = (category or ud.get("search_category") or "").strip()
            if not cat:
                return None
            inline = f"courses::category::{urllib.parse.quote_plus(cat)}::{origin_page}"
            if len(inline.encode("utf-8")) <= 64:
                back_cb = inline
            else:
                back_cb = _courses_back_ref("category", cat, origin_page)
            label = "🔙 Back to Category"
        elif origin_type == "coach" and origin_context:
            coach = str(origin_context)
            inline = f"courses::coach::{urllib.parse.quote_plus(coach)}::{origin_page}"
            if len(inline.encode("utf-8")) <= 64:
                back_cb = inline
            else:
                back_cb = _courses_back_ref("coach", coach, origin_page)
            label = "🔙 Back to Coach"
        else:
            back_cb = f"courses::global::{origin_page}"
            label = "🔙 Back to Courses"
        return [InlineKeyboardButton(label, callback_data=back_cb)]
    except Exception:
        return None


async def _fetch_slice(fetch_fn, db, query_text, offset, count, page_size, **kwargs):
    if count <= 0:
        return []
    page = offset // page_size + 1
    within = offset % page_size
    items, _total, _have_more = await fetch_fn(db, query_text, page=page, page_size=page_size, **kwargs)
    out = list(items[within:])
    needed = count - len(out)
    if needed > 0:
        more, _, _ = await fetch_fn(db, query_text, page=page + 1, page_size=page_size, **kwargs)
        out.extend(more[:needed])
    return out[:count]


def _merged_slices(page: int, page_size: int, seg_totals) -> list:
    start = (page - 1) * page_size
    end = start + page_size
    out = []
    cursor = 0
    for seg_total in seg_totals:
        seg_start = max(0, min(seg_total, start - cursor))
        seg_end = max(0, min(seg_total, end - cursor))
        out.append((seg_start, seg_end - seg_start))
        cursor += seg_total
    return out


# ---------------  shared search-result builders  ---------------


async def _build_category_search_results(db, query_text: str, page: int, context: CallbackContext = None):
    page_size = PAGE_SIZE

    try:
        course_total = (await execute_course_search(db, query_text, page=1, page_size=1))[1]
    except Exception:
        course_total = 0
    try:
        coach_total = (await execute_coach_course_search(db, query_text, page=1, page_size=1))[1]
    except Exception:
        coach_total = 0
    try:
        cat_total = (await execute_category_search(db, query_text, page=1, page_size=1))[1]
    except Exception:
        cat_total = 0
    total = course_total + coach_total + cat_total
    if total == 0:
        return None, None, 0
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages

    (course_off, course_count), (coach_off, coach_count), (cat_off, cat_count) = _merged_slices(
        page,
        page_size,
        [course_total, coach_total, cat_total],
    )

    matched_courses = []
    if course_count > 0:
        try:
            matched_courses = _dedupe_courses(
                await _fetch_slice(execute_course_search, db, query_text, course_off, course_count, page_size)
            )
        except Exception:
            matched_courses = []

    coach_courses_matched = []
    if coach_count > 0:
        try:
            coach_courses_matched = _dedupe_courses(
                await _fetch_slice(execute_coach_course_search, db, query_text, coach_off, coach_count, page_size)
            )
        except Exception:
            coach_courses_matched = []

    page_cats = []
    if cat_count > 0:
        try:
            page_cats = _dedupe_categories(
                await _fetch_slice(execute_category_search, db, query_text, cat_off, cat_count, page_size)
            )
        except Exception:
            page_cats = []

    if coach_courses_matched and matched_courses:
        course_keys = {_course_key(c) for c in matched_courses}
        coach_courses_matched = [c for c in coach_courses_matched if _course_key(c) not in course_keys]

    search_ref = _search_results_ref("categories", query_text, page)
    _store_search_nav_ref(context, search_ref)

    keyboard = []
    for crs in matched_courses:
        name = crs.get("name")
        link = crs.get("link")
        if name and link:
            keyboard.append([InlineKeyboardButton(f"🔗 {name}", url=link)])

    for c in coach_courses_matched:
        link = c.get("link")
        name = c.get("name")
        if name and link:
            keyboard.append([InlineKeyboardButton(f"👨‍🏫 {name} ({c.get('coach')})", url=link)])

    for cat in page_cats:
        keyboard.append(_category_result_button(cat, page, search_ref))

    nav = []
    q_ref = _search_query_ref(query_text)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_categories_pg::{q_ref}::{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"search_categories_pg::{q_ref}::{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🆕 New Search", callback_data="search_new::categories")])

    origin_row = _origin_back_row(context, "categories")
    if origin_row:
        keyboard.append(origin_row)
    else:
        keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="back_to_cats")])

    title = f"🔍 Results for '{query_text}' (categories, courses & coaches, page {page}/{total_pages}):"
    return title, keyboard, total


async def _build_course_search_results(db, query_text: str, page: int, context: CallbackContext = None):
    page_size = PAGE_SIZE

    try:
        cat_total = (await execute_category_search(db, query_text, page=1, page_size=1))[1]
    except Exception:
        cat_total = 0
    try:
        coach_total = (await execute_coach_course_search(db, query_text, page=1, page_size=1))[1]
    except Exception:
        coach_total = 0
    try:
        course_total = (await execute_course_search(db, query_text, page=1, page_size=1))[1]
    except Exception:
        course_total = 0
    total = cat_total + coach_total + course_total
    if total == 0:
        return None, None, 0
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages

    (cat_off, cat_count), (coach_off, coach_count), (course_off, course_count) = _merged_slices(
        page,
        page_size,
        [cat_total, coach_total, course_total],
    )

    matched_cats = []
    if cat_count > 0:
        try:
            matched_cats = _dedupe_categories(
                await _fetch_slice(execute_category_search, db, query_text, cat_off, cat_count, page_size)
            )
        except Exception:
            matched_cats = []

    coach_courses_matched = []
    if coach_count > 0:
        try:
            coach_courses_matched = _dedupe_courses(
                await _fetch_slice(execute_coach_course_search, db, query_text, coach_off, coach_count, page_size)
            )
        except Exception:
            coach_courses_matched = []

    course_items = []
    if course_count > 0:
        try:
            course_items = _dedupe_courses(
                await _fetch_slice(execute_course_search, db, query_text, course_off, course_count, page_size)
            )
        except Exception:
            course_items = []

    if coach_courses_matched and course_items:
        course_keys = {_course_key(c) for c in course_items}
        coach_courses_matched = [c for c in coach_courses_matched if _course_key(c) not in course_keys]

    search_ref = _search_results_ref("courses", query_text, page)
    _store_search_nav_ref(context, search_ref)
    keyboard = []

    for cat in matched_cats:
        keyboard.append(_category_result_button(cat, page=1, search_ref=search_ref))

    for c in coach_courses_matched:
        link = c.get("link")
        name = c.get("name")
        if name and link:
            keyboard.append([InlineKeyboardButton(f"👨‍🏫 {name} ({c.get('coach')})", url=link)])

    if course_items:
        _, reply_markup = build_courses_page(
            course_items,
            page=page,
            origin_type="global",
            origin_context=None,
            total_count=course_total,
            is_page=True,
            store_page_ref=False,
            search_ref=search_ref,
        )
        existing_kb = _extract_course_rows(list(reply_markup.inline_keyboard) if reply_markup else [])
        keyboard.extend(existing_kb)

    search_nav = []
    q_ref = _search_query_ref(query_text)
    if page > 1:
        search_nav.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"search_courses_pg::{q_ref}::{page - 1}"),
        )
    if page < total_pages:
        search_nav.append(
            InlineKeyboardButton("➡️ Next", callback_data=f"search_courses_pg::{q_ref}::{page + 1}"),
        )
    if search_nav:
        keyboard.append(search_nav)

    keyboard.append([InlineKeyboardButton("🆕 New Search", callback_data="search_new::courses")])

    origin_row = _origin_back_row(context, "courses")
    if origin_row:
        keyboard.append(origin_row)
    else:
        keyboard.append([InlineKeyboardButton("🔙 Back to Courses", callback_data="courses::global::1")])

    title = f"🔍 Results for '{query_text}' (courses, categories & coaches, page {page}/{total_pages}):"
    return title, keyboard, total


async def _build_category_course_search_results(db, query_text: str, category: str, page: int, context: CallbackContext = None):
    page_size = PAGE_SIZE

    try:
        child_total = (await execute_category_search(db, query_text, page=1, page_size=1, parent=category))[1]
    except Exception:
        child_total = 0
    try:
        coach_total = (await execute_coach_course_search(db, query_text, category, page=1, page_size=1))[1]
    except Exception:
        coach_total = 0
    try:
        course_total = (await execute_category_course_search(
            db,
            query_text,
            category,
            page=1,
            page_size=1,
            include_children=True,
        ))[1]
    except Exception:
        course_total = 0
    total = child_total + coach_total + course_total
    if total == 0:
        return None, None, 0
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages

    (child_off, child_count), (coach_off, coach_count), (course_off, course_count) = _merged_slices(
        page,
        page_size,
        [child_total, coach_total, course_total],
    )

    child_cats_matched = []
    if child_count > 0:
        try:
            child_cats_matched = _dedupe_categories(
                await _fetch_slice(
                    execute_category_search,
                    db,
                    query_text,
                    child_off,
                    child_count,
                    page_size,
                    parent=category,
                )
            )
        except Exception:
            child_cats_matched = []

    coach_courses_matched = []
    if coach_count > 0:
        try:
            coach_courses_matched = _dedupe_courses(
                await _fetch_slice(
                    execute_coach_course_search,
                    db,
                    query_text,
                    coach_off,
                    coach_count,
                    page_size,
                    category=category,
                )
            )
        except Exception:
            coach_courses_matched = []

    course_items = []
    if course_count > 0:
        try:
            course_items = _dedupe_courses(
                await _fetch_slice(
                    execute_category_course_search,
                    db,
                    query_text,
                    course_off,
                    course_count,
                    page_size,
                    category=category,
                    include_children=True,
                )
            )
        except Exception:
            course_items = []

    if coach_courses_matched and course_items:
        course_keys = {_course_key(c) for c in course_items}
        coach_courses_matched = [c for c in coach_courses_matched if _course_key(c) not in course_keys]

    try:
        if context is not None:
            context.user_data["search_category"] = category
    except Exception:
        pass

    search_ref = _search_results_ref("category_courses", query_text, page, category=category)
    _store_search_nav_ref(context, search_ref)

    keyboard = []

    for child_cat in child_cats_matched:
        child_path = child_cat.get("path") or child_cat.get("name")
        payload = {
            "type": "showcat",
            "path": child_path,
            "from_parent": category,
            "parent_page": 1,
            "search_ref": search_ref,
        }
        key = _store_callback_payload(payload)
        keyboard.append(
            [InlineKeyboardButton(f"📁 {child_cat.get('name')}", callback_data=f"showcat_ref::{key}")],
        )

    for c in coach_courses_matched:
        link = c.get("link")
        name = c.get("name")
        if name and link:
            keyboard.append(
                [
                    InlineKeyboardButton(f"👨‍🏫 {name} ({c.get('coach')})", url=link),
                ],
            )

    if course_items:
        _, reply_markup = build_courses_page(
            course_items,
            page=page,
            origin_type="category",
            category=category,
            origin_context="categories",
            origin_context_page=1,
            total_count=course_total,
            is_page=True,
            store_page_ref=False,
            search_ref=search_ref,
        )

        existing_kb = _extract_course_rows(list(reply_markup.inline_keyboard) if reply_markup else [])
        keyboard.extend(existing_kb)

    search_nav = []
    q_ref = _category_search_ref(query_text, category)
    if page > 1:
        search_nav.append(
            InlineKeyboardButton(
                "⬅️ Previous",
                callback_data=f"search_cat_courses_pg::{q_ref}::{page - 1}",
            ),
        )
    if page < total_pages:
        search_nav.append(
            InlineKeyboardButton(
                "➡️ Next",
                callback_data=f"search_cat_courses_pg::{q_ref}::{page + 1}",
            ),
        )
    if search_nav:
        keyboard.append(search_nav)

    keyboard.append([InlineKeyboardButton("🆕 New Search", callback_data="search_new::category_courses")])

    origin_row = _origin_back_row(context, "category_courses", category=category)
    if origin_row:
        keyboard.append(origin_row)
    else:
        back_cb = _courses_back_ref("category", category, 1)
        keyboard.append([InlineKeyboardButton("🔙 Back to Category", callback_data=back_cb)])

    title = f"🔍 Results for '{query_text}' in '{category}' incl. subcategories & coaches (page {page}/{total_pages}):"
    return title, keyboard, total

# ---------------  callback entry points  ---------------


async def search_courses_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    origin_type = "global"
    origin_context = ""
    origin_page = 1
    if data.startswith("search_courses_coach_ref::"):
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
        raw_type = parts[1] if len(parts) > 1 else ""
        origin_context = ""
        if raw_type == "global" or raw_type.isdigit():
            origin_type = "global"
            if raw_type.isdigit():
                origin_page = int(raw_type)
            else:
                try:
                    origin_page = int(parts[2]) if len(parts) > 2 else 1
                except Exception:
                    origin_page = 1
        elif len(parts) >= 4:
            origin_type = "coach"
            origin_context = urllib.parse.unquote_plus(parts[2])
            try:
                origin_page = int(parts[3])
            except Exception:
                origin_page = 1
            if not origin_context:
                origin_type = "global"
        else:
            origin_type = "global"
            try:
                origin_page = int(parts[2]) if len(parts) > 2 else 1
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
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    category_name = ""
    origin_page = 1
    if data.startswith("search_category_courses_ref::"):
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
        category_name = context.user_data.get("search_category", "")

    category_name = (category_name or "").strip()

    if not category_name:
        context.user_data["search_mode"] = "courses"
        await safe_edit_message(
            query,
            "🔍 The category is no longer available — searching ALL courses. Enter your query (or /cancel to cancel):",
            action_key=getattr(query, "data", None),
        )
        return SEARCH_QUERY

    context.user_data["search_category"] = category_name
    context.user_data["search_origin_page"] = origin_page
    context.user_data["search_mode"] = "category_courses"

    await safe_edit_message(
        query,
        f"🔍 Please enter your search query to find courses in '{category_name}' (or /cancel to cancel):",
        action_key=getattr(query, "data", None),
    )
    return SEARCH_QUERY


async def search_new_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split("::")
    mode = parts[1] if len(parts) > 1 and parts[1] else "courses"
    context.user_data["search_mode"] = mode

    if mode == "categories":
        prompt = "🔍 Please enter your search query to find categories (or /cancel to cancel):"
    elif mode == "category_courses":
        category = (context.user_data.get("search_category") or "").strip()
        if not category:
            mode = "courses"
            context.user_data["search_mode"] = mode
            prompt = (
                "🔍 The category is no longer available — searching ALL courses. "
                "Enter your query (or /cancel to cancel):"
            )
        else:
            prompt = f"🔍 Please enter your search query to find courses in '{category}' (or /cancel to cancel):"
    else:
        prompt = "🔍 Please enter your search query to find courses (or /cancel to cancel):"

    await safe_edit_message(query, prompt, action_key=getattr(query, "data", None))
    return SEARCH_QUERY


# ---------------  text input handler  ---------------


async def handle_search_input(update: Update, context: CallbackContext):
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("Search query cannot be empty. Please try again or /cancel.")
        return SEARCH_QUERY

    mode = context.user_data.get("search_mode", "courses")

    if mode == "categories":
        await _perform_category_search(update, context, query_text)
    elif mode == "category_courses":
        category = (context.user_data.get("search_category") or "").strip()
        if not category:
            mode = "courses"
            await update.message.reply_text(
                "⚠️ The category context is no longer available — I'll search all courses instead.",
            )
            await _perform_course_search(update, context, query_text)
        else:
            await _perform_category_course_search(update, context, query_text, category)
    else:
        await _perform_course_search(update, context, query_text)

    context.user_data["last_search_query"] = query_text
    context.user_data["last_search_mode"] = mode
    context.user_data.pop("search_mode", None)
    return ConversationHandler.END


# ---------------  search implementations  ---------------


async def _perform_category_search(update: Update, context: CallbackContext, query_text: str):
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        title, keyboard, _ = await _build_category_search_results(db, query_text, page=1, context=context)

        if title is None or not keyboard:
            await update.message.reply_text(
                f"No categories found matching '{query_text}'. 😕\n\n"
                "Try a different search term or use /categories to browse.",
            )
            return

        await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception:
        logger.exception("Error searching categories")
        await update.message.reply_text("An error occurred while searching. Please try again.")


async def _perform_course_search(update: Update, context: CallbackContext, query_text: str):
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        title, keyboard, _ = await _build_course_search_results(db, query_text, page=1, context=context)

        if title is None or not keyboard:
            await update.message.reply_text(
                f"No courses found matching '{query_text}'. 😕\n\n"
                "Try a different search term or use /courses to browse all courses.",
            )
            return

        await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception:
        logger.exception("Error searching courses")
        await update.message.reply_text("An error occurred while searching. Please try again.")


async def _perform_category_course_search(update: Update, context: CallbackContext, query_text: str, category: str):
    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("Error: Unable to connect to the database.")
            return

        title, keyboard, _ = await _build_category_course_search_results(db, query_text, category, page=1, context=context)

        if title is None or not keyboard:
            await update.message.reply_text(
                f"No results found matching '{query_text}' in category '{category}' or its subcategories. 😕",
            )
            return

        await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception:
        logger.exception("Error searching category courses")
        await update.message.reply_text("An error occurred while searching. Please try again.")


# ---------------  pagination for search results  ---------------


async def search_courses_pagination_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
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

        title, keyboard, total = await _build_course_search_results(db, query_text, page, context=context)

        if title is None or not keyboard:
            await safe_edit_message(
                query,
                f"No courses found matching '{query_text}'. 😕",
                action_key=getattr(query, "data", None),
            )
            return

        await safe_edit_message(
            query,
            title,
            reply_markup=InlineKeyboardMarkup(keyboard),
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
    query = update.callback_query
    await safe_answer(query)
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

        title, keyboard, total = await _build_category_search_results(db, query_text, page, context=context)

        if title is None or not keyboard:
            await safe_edit_message(
                query,
                f"No categories found matching '{query_text}'. 😕",
                action_key=getattr(query, "data", None),
            )
            return

        await safe_edit_message(
            query,
            title,
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
    query = update.callback_query
    await safe_answer(query)
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

        title, keyboard, total = await _build_category_course_search_results(db, query_text, category, page, context=context)

        if title is None or not keyboard:
            await safe_edit_message(
                query,
                f"No results found matching '{query_text}' in '{category}' or its subcategories. 😕",
                action_key=getattr(query, "data", None),
            )
            return

        await safe_edit_message(
            query,
            title,
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


# ---------------  back to results  ---------------


async def back_to_results_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split("::")
    if len(parts) < 2:
        await safe_edit_message(query, "Invalid callback.", action_key=getattr(query, "data", None))
        return
    key = parts[1]
    payload = await _resolve_callback_payload(key)
    if not payload or payload.get("type") != "search_results":
        await safe_edit_message(
            query,
            "Search results are no longer available. Please search again.",
            action_key=getattr(query, "data", None),
        )
        return
    mode = payload.get("mode", "courses")
    query_text = payload.get("q", "")
    category = payload.get("category")
    try:
        page = int(payload.get("page") or 1)
    except Exception:
        page = 1
    if not query_text:
        await safe_edit_message(
            query,
            "Search query is no longer available. Please search again.",
            action_key=getattr(query, "data", None),
        )
        return

    try:
        db = await get_db()
        if db is None:
            await safe_edit_message(
                query,
                "Error: Unable to connect to the database.",
                action_key=getattr(query, "data", None),
            )
            return

        if mode == "categories":
            title, keyboard, _ = await _build_category_search_results(db, query_text, page, context=context)
        elif mode == "category_courses":
            if not category:
                await safe_edit_message(
                    query,
                    "The category for these results is no longer available. Please search again.",
                    action_key=getattr(query, "data", None),
                )
                return
            title, keyboard, _ = await _build_category_course_search_results(db, query_text, category, page, context=context)
        else:
            title, keyboard, _ = await _build_course_search_results(db, query_text, page, context=context)

        if title is None or not keyboard:
            await safe_edit_message(
                query,
                "No results found for your search anymore. Please search again.",
                action_key=getattr(query, "data", None),
            )
            return

        await safe_edit_message(
            query,
            title,
            reply_markup=InlineKeyboardMarkup(keyboard),
            action_key=getattr(query, "data", None),
        )

    except Exception:
        logger.exception("Error returning to search results")
        await safe_edit_message(
            query,
            "An error occurred while loading search results.",
            action_key=getattr(query, "data", None),
        )


# ---------------  cancel handler  ---------------


async def search_cancel(update: Update, context: CallbackContext):
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
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(search_courses_callback, pattern=r"^search_courses::"),
            CallbackQueryHandler(search_courses_callback, pattern=r"^search_courses_coach_ref::"),
            CallbackQueryHandler(search_categories_callback, pattern=r"^search_categories::"),
            CallbackQueryHandler(search_category_courses_callback, pattern=r"^search_category_courses::"),
            CallbackQueryHandler(search_category_courses_callback, pattern=r"^search_category_courses_ref::"),
            CallbackQueryHandler(search_new_callback, pattern=r"^search_new::"),
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
