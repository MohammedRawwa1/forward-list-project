"""Search handlers for the Telegram bot paginated interface.

Provides a ConversationHandler-based search flow that works across
the /courses, /categories, and per-category course views. The user
clicks a 🔍 Search button, types a query, and gets paginated results
rendered using the same builders as the normal browsing views.

Search results are now "reachable in both directions":

- A course search (/courses) also surfaces matching *categories* as
  navigation buttons, so a query that matches a category (not a course)
  is still reachable from /courses.
- A category search (/categories) also surfaces matching *courses* as
  direct URL buttons, so a query that matches a course is reachable
  from /categories too.
- Results are deduplicated (same course/category appearing multiple
  times is collapsed), and every result button carries a compact
  ``search_ref`` so any deep view (category/coach/type/course details)
  opened from the results can offer a "🔙 Back to Results" button that
  re-renders the exact results page the user was browsing.
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


def _search_results_ref(mode: str, query_text: str, page: int = 1, category: str = None) -> str:
    """Store a "search results" payload and return a compact 16-char reference.

    Deep views (category/coach/type/course details) reached from search
    results embed this ref so their "🔙 Back to Results" button can
    re-render the exact results page the user was browsing.
    """
    payload = {"type": "search_results", "mode": mode, "q": query_text, "page": int(page or 1)}
    if category:
        payload["category"] = category
    return _store_callback_payload(payload)


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


# ---------------  normalization / dedup helpers  ---------------


def _course_key(c):
    """Stable dedup key for a course result.

    Prefers the unique course id; falls back to name+link when no id is
    present so the same course (possibly embedded under several categories)
    collapses into a single result.
    """
    try:
        cid = c.get("id")
        if cid:
            return ("id", str(cid))
        return ("course", c.get("name"), c.get("link"))
    except Exception:
        return ("raw", repr(c))


def _dedupe_courses(items) -> list:
    """Collapse duplicate course results.

    The same course may be embedded under several categories (parent +
    children) or returned by multiple pipelines; dedupe by id (when present)
    falling back to name+link so users don't see the same course many times.
    """
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
    """Collapse duplicate category results by name (and path when present)."""
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
    """Build a single category result row (showcat_ref button).

    Embeds `search_ref` in the stored payload so the category view opened
    from search results can offer "Back to Results".
    """
    cat_path = cat.get("path") or cat.get("name")
    payload = {"type": "showcat", "path": cat_path, "from_parent": "categories", "parent_page": page}
    if search_ref:
        payload["search_ref"] = search_ref
    key = _store_callback_payload(payload)
    cb = f"showcat_ref::{key}"
    display_name = cat.get("name") if isinstance(cat, dict) else str(cat)
    # Show parent indicator if this category has a parent
    parent_name = cat.get("parent") if isinstance(cat, dict) else None
    if parent_name:
        display_name = f"{display_name} › ({parent_name})"
    return [InlineKeyboardButton(display_name, callback_data=cb)]


def _courses_back_ref(kind: str, name: str, page: int) -> str:
    """Compact '🔙 Back to Results' callback for long category/coach names.

    ``courses::category::<name>::<page>`` / ``courses::coach::<name>::<page>``
    exceed Telegram's 64-byte callback_data limit for long (Arabic) names,
    which would make Telegram reject the ENTIRE results message (the search
    appears to "stall"). Instead, store the origin as a ``courses_ref::<key>``
    payload — the existing ``courses_callback`` ``courses_ref::`` path already
    resolves it and re-renders the pre-search course list, fetching the items
    server-side when needed.
    """
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
        # Storage failed — fall back to the inline form. Note: if the inline
        # callback is oversized, Telegram rejects the whole message (storage
        # failure here is practically unreachable, so this only guards the
        # rare path).
        quoted = urllib.parse.quote_plus(str(name))
        if kind == "category":
            return f"courses::category::{quoted}::{int(page or 1)}"
        return f"courses::coach::{quoted}::{int(page or 1)}"


def _origin_back_row(context, mode: str, category: str = None):
    """Build a '🔙 Back to Results' row that returns to the pre-search page.

    The search entry callbacks store where the user was before searching
    (``search_origin_type`` / ``search_origin_context`` / ``search_origin_page``
    in user_data). This returns to that exact page so the user doesn't lose
    their place in the category/coach/global listing. Returns None when no
    origin info is available (e.g. stale state after a restart) so callers
    fall back to their static back buttons.
    """
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
            # Search launched from the /categories listing
            back_cb = f"categories_page::{origin_page}"
        elif mode == "category_courses":
            # Search launched from a category's course list. The category name
            # is always stored raw in this path (search_category / the builder's
            # category param), so quote directly — never unquote, which would
            # corrupt names containing a literal '+' (e.g. "C++ Programming").
            cat = (category or ud.get("search_category") or "").strip()
            if not cat:
                return None
            inline = f"courses::category::{urllib.parse.quote_plus(cat)}::{origin_page}"
            # Long (Arabic) category names exceed the 64-byte callback_data
            # limit — use a stored ref so the results message never gets
            # rejected by Telegram (see _courses_back_ref).
            if len(inline.encode("utf-8")) <= 64:
                back_cb = inline
            else:
                back_cb = _courses_back_ref("category", cat, origin_page)
        elif origin_type == "coach" and origin_context:
            # Search launched from a coach's course list. search_origin_context
            # is ALWAYS stored raw (search_courses_callback decodes the inline
            # form before persisting it), so quote directly — never unquote,
            # which would corrupt names containing a literal '+' (unquote_plus
            # maps '+' to space).
            coach = str(origin_context)
            inline = f"courses::coach::{urllib.parse.quote_plus(coach)}::{origin_page}"
            if len(inline.encode("utf-8")) <= 64:
                back_cb = inline
            else:
                back_cb = _courses_back_ref("coach", coach, origin_page)
        else:
            # Search launched from the global /courses listing
            back_cb = f"courses::global::{origin_page}"
        return [InlineKeyboardButton("🔙 Back to Results", callback_data=back_cb)]
    except Exception:
        return None


async def _fetch_slice(fetch_fn, db, query_text, offset, count, page_size, **kwargs):
    """Fetch items ``[offset, offset + count)`` from a paged search function.

    The search functions in ``atlas_search`` page internally (page/page_size),
    so to draw a slice that may straddle a page boundary we fetch the page
    containing ``offset`` and, when needed, the next page too. This lets the
    merged search-results builders paginate two entity types as one stream.
    """
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
    """Compute (offset, count) per segment for a merged paginated view.

    `seg_totals` holds the total item count of each result segment, in the
    display order they are merged (e.g. categories then courses). Returns a
    list of (offset, count) tuples — one per segment — describing which
    slice of each segment belongs on the requested page. This lets the
    search builders paginate two entity types as a single stream.
    """
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
    """Fetch merged, paginated category search results.

    Matching categories, matching courses, and coach-matched courses are
    merged into ONE paginated stream (courses first as URL buttons, then
    coach-matched courses as URL buttons, then category navigation buttons
    — preserving the original page-1 layout), so every entity type is
    reachable from /categories and all cross-entity results are fully
    paginated instead of capped at 5 on page 1. Results are deduped per
    slice. Also remembers the results ref in user_data.
    """
    page_size = PAGE_SIZE

    # Count all entity types so the merged pagination window is accurate
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
    # Clamp stale pagination (e.g. a "Next" click after the result set shrank)
    # to the last valid page so we never render an out-of-range empty page.
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages

    # Merged stream: matching courses, then coach courses, then categories
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

    # Normalize: drop coach matches that are already in the course results on
    # this page so a course whose name AND coach both match isn't shown twice.
    if coach_courses_matched and matched_courses:
        course_keys = {_course_key(c) for c in matched_courses}
        coach_courses_matched = [c for c in coach_courses_matched if _course_key(c) not in course_keys]

    search_ref = _search_results_ref("categories", query_text, page)
    _store_search_nav_ref(context, search_ref)

    keyboard = []
    # Matching courses as direct URL buttons first (reachable from /categories)
    for crs in matched_courses:
        name = crs.get("name")
        link = crs.get("link")
        if name and link:
            keyboard.append([InlineKeyboardButton(f"🔗 {name}", url=link)])

    # Coach-matched courses as direct URL buttons (reachable from /categories by coach)
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

    # Start a brand-new search from the results page
    keyboard.append([InlineKeyboardButton("🆕 New Search", callback_data="search_new::categories")])

    # Back to where the user was before searching (exact /categories page),
    # falling back to the canonical categories entry when origin info is stale.
    origin_row = _origin_back_row(context, "categories")
    if origin_row:
        keyboard.append(origin_row)
    else:
        keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="back_to_cats")])

    title = f"🔍 Results for '{query_text}' (categories, courses & coaches, page {page}/{total_pages}):"
    return title, keyboard, total


async def _build_course_search_results(db, query_text: str, page: int, context: CallbackContext = None):
    """Fetch merged, paginated global course search results.

    Matching courses, matching categories, and coach-matched courses are
    merged into ONE paginated stream (categories first as navigation
    buttons, then coach-matched courses as URL buttons, then course rows —
    preserving the original page-1 layout), so every entity type is
    reachable from /courses and all cross-entity results are fully
    paginated instead of capped at 5 on page 1. Every row/button carries a
    search_ref for Back to Results. Also remembers the results ref.
    """
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
    # Clamp stale pagination (e.g. a "Next" click after the result set shrank)
    # to the last valid page so we never render an out-of-range empty page.
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages

    # Merged stream: matching categories, then coach courses, then courses
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

    # Normalize: drop coach matches that are already in the course results on
    # this page so a course whose name AND coach both match isn't shown twice.
    if coach_courses_matched and course_items:
        course_keys = {_course_key(c) for c in course_items}
        coach_courses_matched = [c for c in coach_courses_matched if _course_key(c) not in course_keys]

    search_ref = _search_results_ref("courses", query_text, page)
    _store_search_nav_ref(context, search_ref)
    keyboard = []

    # Matching categories as navigation buttons first (reachable from /courses)
    for cat in matched_cats:
        keyboard.append(_category_result_button(cat, page=1, search_ref=search_ref))

    # Coach-matched courses as direct URL buttons (reachable from /courses by coach)
    for c in coach_courses_matched:
        link = c.get("link")
        name = c.get("name")
        if name and link:
            keyboard.append([InlineKeyboardButton(f"👨‍🏫 {name} ({c.get('coach')})", url=link)])

    # Course rows rendered by the standard builder (nav stripped)
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

    # Build the search navigation row
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

    # Start a brand-new search from the results page
    keyboard.append([InlineKeyboardButton("🆕 New Search", callback_data="search_new::courses")])

    # Back to where the user was before searching (coach or global page),
    # falling back to the global courses entry when origin info is stale.
    origin_row = _origin_back_row(context, "courses")
    if origin_row:
        keyboard.append(origin_row)
    else:
        keyboard.append([InlineKeyboardButton("🔙 Back to Courses", callback_data="courses::global::1")])

    # Rebuild title to show search context
    title = f"🔍 Results for '{query_text}' (courses, categories & coaches, page {page}/{total_pages}):"
    return title, keyboard, total


async def _build_category_course_search_results(db, query_text: str, category: str, page: int, context: CallbackContext = None):
    """Fetch merged, paginated category-scoped course search results.

    Child categories, coach-matching courses, and course results are merged
    into ONE paginated stream (in the original page-1 order), so the
    cross-entity blocks are fully paginated instead of capped at 5 on page
    1. Results are deduped per slice. Every navigation/course button carries
    a search_ref for Back to Results.
    """
    page_size = PAGE_SIZE

    # Count all three segments so the merged pagination window is accurate
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
    # Clamp stale pagination (e.g. a "Next" click after the result set shrank)
    # to the last valid page so we never render an out-of-range empty page.
    total_pages = max(1, math.ceil(total / page_size))
    if page > total_pages:
        page = total_pages

    # Merged stream: child categories, then coach courses, then course results
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

    # Normalize: drop coach matches that are already in the course results on
    # this page so a course whose name AND coach both match isn't shown twice.
    if coach_courses_matched and course_items:
        course_keys = {_course_key(c) for c in course_items}
        coach_courses_matched = [c for c in coach_courses_matched if _course_key(c) not in course_keys]

    # Persist the category so the results page's "🆕 New Search" button can
    # re-prompt within the right category even after a stale-ref re-render.
    try:
        if context is not None:
            context.user_data["search_category"] = category
    except Exception:
        pass

    search_ref = _search_results_ref("category_courses", query_text, page, category=category)
    _store_search_nav_ref(context, search_ref)

    # Build the results keyboard
    keyboard = []

    # Add matching child categories as navigation buttons
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

    # Add matching courses by coach
    for c in coach_courses_matched:
        link = c.get("link")
        name = c.get("name")
        if name and link:
            keyboard.append(
                [
                    InlineKeyboardButton(f"👨‍🏫 {name} ({c.get('coach')})", url=link),
                ],
            )

    # Add matching courses
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

    # Start a brand-new search from the results page
    keyboard.append([InlineKeyboardButton("🆕 New Search", callback_data="search_new::category_courses")])

    # Back to the category's course list the search was launched from,
    # falling back to the category's first page when origin info is stale.
    origin_row = _origin_back_row(context, "category_courses", category=category)
    if origin_row:
        keyboard.append(origin_row)
    else:
        # Same 64-byte guard as _origin_back_row: long (Arabic) category names
        # must use a stored ref or Telegram rejects the whole results message.
        back_cb = _courses_back_ref("category", category, 1)
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])

    title = f"🔍 Results for '{query_text}' in '{category}' incl. subcategories & coaches (page {page}/{total_pages}):"
    return title, keyboard, total

# ---------------  callback entry points  ---------------


async def search_courses_callback(update: Update, context: CallbackContext):
    """🔍 Search button clicked from global courses view.

    Callback data forms:
      search_courses::global::<page>
      search_courses::coach::<name>::<page>        (name percent-encoded)
      search_courses::coach::<page>                (name-less; treated as global)
      search_courses::<page>                       (legacy global)
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
        # Inline forms:
        #   search_courses::global::<page>
        #   search_courses::coach::<name>::<page>
        # plus name-less / legacy variants (page in segment 2):
        #   search_courses::coach::<page>   (stored-ref pages, name lost)
        #   search_courses::<page>          (legacy global)
        parts = data.split("::")
        raw_type = parts[1] if len(parts) > 1 else ""
        origin_context = ""
        if raw_type == "global" or raw_type.isdigit():
            # global form (or legacy page-only form)
            origin_type = "global"
            if raw_type.isdigit():
                origin_page = int(raw_type)
            else:
                try:
                    origin_page = int(parts[2]) if len(parts) > 2 else 1
                except Exception:
                    origin_page = 1
        elif len(parts) >= 4:
            # coach form with name: search_courses::coach::<name>::<page>.
            # The name is percent-encoded (built with quote_plus) — decode it
            # now so search_origin_context is ALWAYS stored raw, letting
            # _origin_back_row rebuild the callback without guessing.
            origin_type = "coach"
            origin_context = urllib.parse.unquote_plus(parts[2])
            try:
                origin_page = int(parts[3])
            except Exception:
                origin_page = 1
            if not origin_context:
                # Malformed/empty coach name — normalize to a global origin so
                # the stored state stays consistent (back row falls to global).
                origin_type = "global"
        else:
            # name-less form: search_courses::coach::<page> — the coach scope
            # is lost, so treat it as a global origin (page in segment 2).
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

    # Guard against whitespace-only names from malformed/stale callbacks
    category_name = (category_name or "").strip()

    if not category_name:
        # Still unknown — this button is stale (e.g. the category was removed
        # or /cancel cleared the state). Fall back to a global course search
        # instead of prompting to search an empty (broken) category scope.
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
    """🆕 New Search button on a search results page.

    Callback data: search_new::<mode> where mode is one of
    ``courses`` / ``categories`` / ``category_courses``. Re-enters the
    search prompt so the user can type a brand-new query without leaving
    the current view. Origin info (the pre-search page) is preserved so
    "Back to Results" on the fresh results still returns to where the
    user started.
    """
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
            # Stale button after the category context was cleared (e.g. /cancel
            # or a fresh chat): fall back to a global course search instead of
            # prompting to search an empty (broken) category scope.
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
    """Process the user's search query text."""
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
            # Stale state (category context lost): fall back to a global course
            # search so the typed query still produces useful results instead of
            # a broken empty-scope search. Record the effective mode so the
            # saved last-search state matches what actually ran.
            mode = "courses"
            await update.message.reply_text(
                "⚠️ The category context is no longer available — I'll search all courses instead.",
            )
            await _perform_course_search(update, context, query_text)
        else:
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
    """Search all courses by name across all categories, return paginated results."""
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
    """Search courses by name within a specific category, return paginated results."""
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
    """Handle the '🔙 Back to Results' button on deep views opened from search results.

    Callback data: back_to_results::<ref16>. Resolves the stored search
    results payload (mode/query/category/page) and re-renders the exact
    results page the user was browsing.
    """
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
            # 🆕 New Search button on search results pages (re-prompts for a query)
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
