"""Owner-only `/update` flow: rename a course title and/or edit its link.

The flow mirrors `/add` (choose parent, then drill into the coach/child that
holds the course, then choose the course), then shows a "file renamer"-style
dialog with the current name and link rendered in Markdown. The owner can edit
either field (keeping the other) and confirm with ✅ Okay or abort with ❌ Cancel.

Field writes deliberately use MongoDB PUT/PATCH semantics against the document
that actually holds the course — which may be a top-level parent, a coach, or a
deeper child category — located by the course uuid (`courses.id`) so the update
lands on exactly one embedded document:
  * PATCH — only one field changed → `$set` just that field.
  * PUT   — both fields changed   → `$set` the whole embedded document,
            preserving every other key it carries.
"""

import logging
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

from config import is_owner
from conversation_states import (
    UPDATE_COURSE,
    UPDATE_DIALOG,
    UPDATE_DOC,
    UPDATE_LINK,
    UPDATE_NAME,
    UPDATE_PARENT,
)
from handlers.base_handlers import (
    COURSE_PAGE_SIZE,
    TOP_LEVEL_FILTER,
    _dedupe_markup,
    _fetch_category_courses_page,
    _fit_cb,
    _get_courses_count,
    _get_total_count,
    _resolve_callback_payload,
    _store_callback_payload,
    invalidate_course_caches,
    safe_answer,
    safe_edit_message,
)
from handlers.db_connection import get_db

logger = logging.getLogger(__name__)

# Inline keyboards are also byte-bounded (~10 KB serialized). Keep the picker
# rows well under that even when category names run long.
DOC_PAGE_SIZE = 40
# Displayed course/category names are truncated so a full page of long names
# still fits Telegram's reply_markup limit; the callback always carries the id.
MAX_LABEL_LEN = 60

# ---------------  id detection  ---------------
def _looks_like_uuid(token: str) -> bool:
    """True when a callback token looks like a course uuid (36 chars, hex+dashes)."""
    try:
        if len(token) != 36:
            return False
        parts = token.split("-")
        return len(parts) == 5 and all(all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)
    except Exception:
        return False


# ---------------  callback builders (keep callback_data <= 64 bytes)  ---------------
def _upd_parent_cb(name, page: int, cat_id: str = None) -> str:
    inline = f"update_parent::{urllib.parse.quote_plus(str(name))}::{page}"
    payload = {"type": "update_parent", "name": str(name), "page": page}
    if cat_id:
        inline = f"{inline}::{urllib.parse.quote_plus(str(cat_id))}"
        payload["id"] = str(cat_id)
    return _fit_cb("update_parent", inline, payload)


def _upd_doc_cb(name, path, page: int) -> str:
    inline = f"update_doc::{urllib.parse.quote_plus(str(name))}::{page}"
    payload = {"type": "update_doc", "name": str(name), "path": str(path), "page": page}
    return _fit_cb("update_doc", inline, payload)


def _upd_courses_cb(path, page: int) -> str:
    inline = f"update_courses::{urllib.parse.quote_plus(str(path))}::{page}"
    payload = {"type": "update_courses", "path": str(path), "page": page}
    return _fit_cb("update_courses", inline, payload)


def _upd_course_cb(course_id, name, doc, page: int) -> str:
    # Always carry id + name + owning doc in a stored ref: the callback token is
    # then unambiguous (a bare name can look like anything) and stays tiny.
    payload = {"type": "update_course", "id": course_id, "name": name, "doc": doc, "page": page}
    key = _store_callback_payload(payload)
    return f"update_course_ref::{key}"


# ---------------  markup helpers  ---------------
def _label(value, limit: int = MAX_LABEL_LEN) -> str:
    s = "" if value is None else str(value)
    if len(s) > limit:
        s = s[: max(1, limit - 1)] + "…"
    return s


def _md_code(value) -> str:
    """Render a value as an inline Markdown code span (strips backticks)."""
    s = "" if value is None else str(value)
    s = s.replace("`", "'").strip()
    return f"`{s}`"


def _md_plain(value) -> str:
    """Escape a value for Markdown body text (outside a code span)."""
    s = "" if value is None else str(value)
    for ch in ("\\", "*", "_", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


# ---------------  browse stack  ---------------
def _browse(context) -> dict:
    state = context.user_data.get("update_browse")
    if not isinstance(state, dict):
        state = {"stack": []}
        context.user_data["update_browse"] = state
    state.setdefault("stack", [])
    return state


def _current_doc(context):
    stack = _browse(context).get("stack") or []
    return stack[-1] if stack else None


def _reset(context):
    for k in ("update_browse", "update_draft", "update_msg"):
        try:
            context.user_data.pop(k, None)
        except Exception:
            pass


# ---------------  screens  ---------------
async def _parent_list_keyboard(db, page: int):
    total = await _get_total_count(db, "categories", TOP_LEVEL_FILTER, ttl=15)
    page = max(1, int(page or 1))
    start = (page - 1) * DOC_PAGE_SIZE
    parents = (
        await db.categories.find(TOP_LEVEL_FILTER).sort("name", 1).skip(start).limit(DOC_PAGE_SIZE).to_list(length=DOC_PAGE_SIZE)
    )
    keyboard = []
    for p in parents:
        keyboard.append(
            [InlineKeyboardButton(_label(p.get("name")), callback_data=_upd_parent_cb(p.get("name"), page, p.get("id")))],
        )
    total_pages = max(1, (total - 1) // DOC_PAGE_SIZE + 1) if total else 1
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"update_parent_page::{page - 1}"))
        nav.append(InlineKeyboardButton("🏠 Home", callback_data="update_parent_page::1"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"update_parent_page::{page + 1}"))
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"update_parent_page::{total_pages}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="update_cancel")])
    text = f"✏️ *Update a course*\n\nChoose the parent category (page {page}/{total_pages}):"
    return text, _dedupe_markup(InlineKeyboardMarkup(keyboard))


async def _doc_view_keyboard(db, context, page: int):
    doc = _current_doc(context)
    if not doc:
        return None, None
    name = doc.get("name")
    path = doc.get("path") or name
    page = max(1, int(page or 1))
    total = await _get_total_count(db, "categories", {"parent": name}, ttl=15)
    start = (page - 1) * DOC_PAGE_SIZE
    children = (
        await db.categories.find({"parent": name}).sort("name", 1).skip(start).limit(DOC_PAGE_SIZE).to_list(length=DOC_PAGE_SIZE)
    )
    keyboard = []
    for c in children:
        keyboard.append(
            [InlineKeyboardButton(_label(c.get("name")), callback_data=_upd_doc_cb(c.get("name"), c.get("path") or c.get("name"), 1))],
        )
    total_pages = max(1, (total - 1) // DOC_PAGE_SIZE + 1) if total else 1
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"update_doc_page::{page - 1}"))
        nav.append(InlineKeyboardButton("🏠 Home", callback_data="update_doc_page::1"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"update_doc_page::{page + 1}"))
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"update_doc_page::{total_pages}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("📚 Courses here", callback_data=_upd_courses_cb(path, 1))])
    up = [InlineKeyboardButton("🔙 Up", callback_data="update_up")]
    if len(_browse(context).get("stack") or []) <= 1:
        up.append(InlineKeyboardButton("🏠 Parents", callback_data="update_parent_page::1"))
    keyboard.append(up)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="update_cancel")])
    if children:
        text = f"✏️ *Update a course*\n\nIn `{_md_code(name)}` — pick a coach/child, or list its courses (page {page}/{total_pages}):"
    else:
        text = f"✏️ *Update a course*\n\nIn `{_md_code(name)}` — no sub-categories. List its courses:"
    return text, _dedupe_markup(InlineKeyboardMarkup(keyboard))


async def _course_list_keyboard(db, context, page: int):
    doc = _current_doc(context)
    if not doc:
        return None, None
    name = doc.get("name")
    page = max(1, int(page or 1))
    try:
        total = await _get_courses_count(db, name)
    except Exception:
        total = 0
    try:
        items, has_more, _ = await _fetch_category_courses_page(db, name, page, COURSE_PAGE_SIZE)
    except Exception:
        items, has_more = [], False
    real = [c for c in items if isinstance(c, dict) and (c.get("name") or "").strip()]
    keyboard = []
    for c in real:
        keyboard.append(
            [
                InlineKeyboardButton(
                    _label(c.get("name")),
                    callback_data=_upd_course_cb(c.get("id"), c.get("name"), name, page),
                ),
            ],
        )
    total_pages = max(1, (total - 1) // COURSE_PAGE_SIZE + 1) if total else 1
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"update_course_page::{page - 1}"))
        nav.append(InlineKeyboardButton("🏠 Home", callback_data="update_course_page::1"))
    if has_more or page < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"update_course_page::{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="update_back_to_doc")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="update_cancel")])
    if not real:
        text = f"✏️ *Update a course*\n\nNo courses found in `{_md_code(name)}`."
    else:
        shown = len(real)
        text = (
            f"✏️ *Update a course*\n\nCourses in `{_md_code(name)}` "
            f"({(page - 1) * COURSE_PAGE_SIZE + 1}-{(page - 1) * COURSE_PAGE_SIZE + shown} of {total}):"
        )
    return text, _dedupe_markup(InlineKeyboardMarkup(keyboard))


def _dialog_text(draft) -> str:
    name = draft.get("name")
    link = draft.get("link")
    cat = draft.get("category")
    lines = [
        "✏️ *Update Course*",
        "",
        f"📝 Name: {_md_code(name)}",
        f"🔗 Link: {_md_code(link)}",
        f"📂 Stored in: {_md_code(cat)}",
    ]
    if name != draft.get("original_name"):
        lines.append(f"↩️ Original name: {_md_code(draft.get('original_name'))}")
    if link != draft.get("original_link"):
        lines.append(f"↩️ Original link: {_md_code(draft.get('original_link'))}")
    lines.append("")
    lines.append("_Tap a field to edit it, then press ✅ Okay. Fields you don't change are kept._")
    return "\n".join(lines)


def _dialog_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Edit Name", callback_data="update_edit_name"), InlineKeyboardButton("🔗 Edit Link", callback_data="update_edit_link")],
            [InlineKeyboardButton("✅ Okay", callback_data="update_ok"), InlineKeyboardButton("❌ Cancel", callback_data="update_cancel")],
        ],
    )


async def _edit_dialog(context, text, reply_markup, parse_mode="Markdown"):
    """Edit the tracked dialog message (used after a text reply updates a field)."""
    loc = context.user_data.get("update_msg") or {}
    chat_id = loc.get("chat_id")
    message_id = loc.get("message_id")
    if not chat_id or not message_id:
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=_dedupe_markup(reply_markup),
            parse_mode=parse_mode,
        )
        return True
    except Exception as e:
        if "Message is not modified" in str(e):
            return True
        logger.exception("Failed to edit update dialog message")
        return False


# ---------------  data helpers  ---------------
async def _find_course(db, course_id, name, doc_name):
    """Locate the embedded course dict and the doc that holds it.

    Returns (holder_doc, course, status) where status is ok | ambiguous | not_found.
    A uuid match is exact; the name fallback only resolves when exactly one course
    in the doc carries that name (duplicate names are reported as ambiguous so the
    caller can refuse and tell the user, rather than guessing).
    """
    coll = db["categories"]
    if course_id:
        try:
            doc = await coll.find_one({"courses.id": course_id}, projection={"name": 1, "parent": 1, "courses": 1})
        except Exception:
            doc = None
        if doc:
            for c in doc.get("courses") or []:
                if isinstance(c, dict) and c.get("id") == course_id:
                    return doc, c, "ok"
    try:
        doc = await coll.find_one({"name": doc_name}, projection={"name": 1, "parent": 1, "courses": 1})
    except Exception:
        doc = None
    if doc:
        matches = [c for c in (doc.get("courses") or []) if isinstance(c, dict) and c.get("name") == name]
        if len(matches) == 1:
            return doc, matches[0], "ok"
        if len(matches) > 1:
            return doc, None, "ambiguous"
    return None, None, "not_found"


async def _apply_course_update(db, course_id, old_name, doc_name, new_name, new_link):
    """Write the new fields with PUT (both changed) or PATCH (one changed).

    Returns (status, holder_name) where status is one of:
    put | patch | unchanged | not_found | ambiguous | error.
    """
    coll = db["categories"]
    holder, current, status = await _find_course(db, course_id, old_name, doc_name)
    if status != "ok" or holder is None or current is None:
        return status, (holder or {}).get("name")

    holder_name = holder.get("name")
    name_changed = new_name != (current.get("name") or "")
    link_changed = new_link != (current.get("link") or "")
    if not name_changed and not link_changed:
        return "unchanged", holder_name

    # _find_course already proved the match is unique (uuid exact, or a single
    # name match), so the positional update can only touch that one element.
    if course_id:
        match = {"_id": holder.get("_id"), "courses.id": course_id}
    else:
        match = {"_id": holder.get("_id"), "courses.name": current.get("name")}

    if name_changed and link_changed:
        # PUT: replace the whole embedded document, preserving extra keys.
        new_doc = dict(current)
        new_doc["name"] = new_name
        new_doc["link"] = new_link
        update = {"$set": {"courses.$": new_doc}}
        mode = "put"
    else:
        # PATCH: touch only the field that changed.
        ops = {}
        if name_changed:
            ops["courses.$.name"] = new_name
        if link_changed:
            ops["courses.$.link"] = new_link
        update = {"$set": ops}
        mode = "patch"

    try:
        res = await coll.update_one(match, update)
    except Exception:
        logger.exception("Failed to apply course update for id=%s", course_id)
        return "error", holder_name
    if getattr(res, "modified_count", 0) == 0:
        return "error", holder_name
    return mode, holder_name


async def _invalidate(db, holder_name):
    try:
        await invalidate_course_caches(category=holder_name, coach=holder_name)
    except Exception:
        logger.debug("cache invalidation after update failed", exc_info=True)


# ---------------  registration  ---------------
async def setup_update_handlers(application):
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("update", update_start)],
            allow_reentry=True,
            states={
                UPDATE_PARENT: [
                    CallbackQueryHandler(update_parent_selected, pattern=r"^update_parent::"),
                    CallbackQueryHandler(update_parent_selected, pattern=r"^update_parent_ref::"),
                    CallbackQueryHandler(update_parent_page, pattern=r"^update_parent_page::"),
                ],
                UPDATE_DOC: [
                    CallbackQueryHandler(update_doc_selected, pattern=r"^update_doc::"),
                    CallbackQueryHandler(update_doc_selected, pattern=r"^update_doc_ref::"),
                    CallbackQueryHandler(update_doc_page, pattern=r"^update_doc_page::"),
                    CallbackQueryHandler(update_show_courses, pattern=r"^update_courses::"),
                    CallbackQueryHandler(update_show_courses, pattern=r"^update_courses_ref::"),
                    CallbackQueryHandler(update_up, pattern=r"^update_up$"),
                    CallbackQueryHandler(update_parent_page, pattern=r"^update_parent_page::"),
                    CallbackQueryHandler(update_cancel, pattern=r"^update_cancel$"),
                ],
                UPDATE_COURSE: [
                    CallbackQueryHandler(update_course_selected, pattern=r"^update_course::"),
                    CallbackQueryHandler(update_course_selected, pattern=r"^update_course_ref::"),
                    CallbackQueryHandler(update_course_page, pattern=r"^update_course_page::"),
                    CallbackQueryHandler(update_back_to_doc, pattern=r"^update_back_to_doc$"),
                    CallbackQueryHandler(update_cancel, pattern=r"^update_cancel$"),
                ],
                UPDATE_DIALOG: [
                    CallbackQueryHandler(update_edit_name, pattern=r"^update_edit_name$"),
                    CallbackQueryHandler(update_edit_link, pattern=r"^update_edit_link$"),
                    CallbackQueryHandler(update_confirm, pattern=r"^update_ok$"),
                    CallbackQueryHandler(update_cancel, pattern=r"^update_cancel$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, update_dialog_hint),
                ],
                UPDATE_NAME: [
                    CallbackQueryHandler(update_cancel, pattern=r"^update_cancel$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, update_name_received),
                ],
                UPDATE_LINK: [
                    CallbackQueryHandler(update_cancel, pattern=r"^update_cancel$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, update_link_received),
                ],
            },
            fallbacks=[CommandHandler("cancel", update_cancel_command)],
            name="update_course_conv",
            persistent=False,
            conversation_timeout=600,
        ),
    )


# ---------------  entry  ---------------
async def update_start(update: Update, context: CallbackContext):
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Only the bot owner can run this command.")
        return ConversationHandler.END

    _reset(context)
    try:
        db = await get_db()
    except Exception:
        db = None
    if db is None:
        await update.message.reply_text("❗️ Could not connect to the database. Try again later.")
        return ConversationHandler.END

    try:
        text, markup = await _parent_list_keyboard(db, 1)
    except Exception:
        logger.exception("update_start: failed to build parent list")
        await update.message.reply_text("An error occurred while loading categories. Try again later.")
        return ConversationHandler.END
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    return UPDATE_PARENT


# ---------------  owner guard + payload resolution  ---------------
async def _guard(query) -> bool:
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    if not is_owner(user_id):
        await safe_edit_message(query, "⛔ Only the bot owner can run this command.", action_key=getattr(query, "data", None))
        return False
    return True


async def _resolve(raw: str, prefix: str):
    """Resolve a `prefix_ref::<key>` payload (inline forms are parsed by callers)."""
    if raw.startswith(f"{prefix}_ref::"):
        try:
            return await _resolve_callback_payload(raw.split("::", 1)[1])
        except Exception:
            return None
    return None


# ---------------  parent picker  ---------------
async def update_parent_page(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
        return ConversationHandler.END
    try:
        page = int(query.data.split("::")[-1])
    except Exception:
        page = 1
    text, markup = await _parent_list_keyboard(db, page)
    _browse(context)["stack"] = []
    await safe_edit_message(query, text, reply_markup=markup, action_key=getattr(query, "data", None), parse_mode="Markdown")
    return UPDATE_PARENT


async def update_parent_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    raw = query.data
    name = None
    cat_id = None
    if raw.startswith("update_parent_ref::"):
        payload = await _resolve(raw, "update_parent")
        if not payload:
            await safe_edit_message(query, "Reference expired. Run /update again.", action_key=raw)
            return ConversationHandler.END
        name = payload.get("name")
        cat_id = payload.get("id")
    else:
        parts = raw.split("::")
        name = urllib.parse.unquote_plus(parts[1]) if len(parts) >= 2 else None
        if len(parts) >= 4 and parts[3]:
            cat_id = urllib.parse.unquote_plus(parts[3])
    if not name:
        await safe_edit_message(query, "Invalid selection.", action_key=raw)
        return UPDATE_PARENT

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=raw)
        return ConversationHandler.END
    path = name
    try:
        doc = await db.categories.find_one(
            ({"id": cat_id} if cat_id else {"name": name}),
            projection={"name": 1, "path": 1, "id": 1},
        )
        if doc:
            name = doc.get("name") or name
            path = doc.get("path") or name
    except Exception:
        doc = None
    _browse(context)["stack"] = [{"name": name, "path": path, "id": cat_id}]
    text, markup = await _doc_view_keyboard(db, context, 1)
    await safe_edit_message(query, text, reply_markup=markup, action_key=raw, parse_mode="Markdown")
    return UPDATE_DOC


# ---------------  doc drill  ---------------
async def update_doc_page(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
        return ConversationHandler.END
    try:
        page = int(query.data.split("::")[-1])
    except Exception:
        page = 1
    text, markup = await _doc_view_keyboard(db, context, page)
    if text is None:
        text, markup = await _parent_list_keyboard(db, 1)
        await safe_edit_message(query, text, reply_markup=markup, action_key=getattr(query, "data", None), parse_mode="Markdown")
        return UPDATE_PARENT
    await safe_edit_message(query, text, reply_markup=markup, action_key=getattr(query, "data", None), parse_mode="Markdown")
    return UPDATE_DOC


async def update_doc_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    raw = query.data
    name = None
    path = None
    if raw.startswith("update_doc_ref::"):
        payload = await _resolve(raw, "update_doc")
        if not payload:
            await safe_edit_message(query, "Reference expired. Run /update again.", action_key=raw)
            return ConversationHandler.END
        name = payload.get("name")
        path = payload.get("path")
    else:
        parts = raw.split("::")
        name = urllib.parse.unquote_plus(parts[1]) if len(parts) >= 2 else None
    if not name:
        await safe_edit_message(query, "Invalid selection.", action_key=raw)
        return UPDATE_DOC
    _browse(context)["stack"].append({"name": name, "path": path or name, "id": None})
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=raw)
        return ConversationHandler.END
    text, markup = await _doc_view_keyboard(db, context, 1)
    await safe_edit_message(query, text, reply_markup=markup, action_key=raw, parse_mode="Markdown")
    return UPDATE_DOC


async def update_up(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    stack = _browse(context)["stack"]
    if len(stack) > 1:
        stack.pop()
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
        return ConversationHandler.END
    text, markup = await _doc_view_keyboard(db, context, 1)
    if text is None:
        text, markup = await _parent_list_keyboard(db, 1)
        await safe_edit_message(query, text, reply_markup=markup, action_key=getattr(query, "data", None), parse_mode="Markdown")
        return UPDATE_PARENT
    await safe_edit_message(query, text, reply_markup=markup, action_key=getattr(query, "data", None), parse_mode="Markdown")
    return UPDATE_DOC


# ---------------  course list  ---------------
async def update_show_courses(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    raw = query.data
    if raw.startswith("update_courses_ref::"):
        payload = await _resolve(raw, "update_courses")
        path = (payload or {}).get("path")
        # keep the current doc but align its path if the payload carries one
        doc = _current_doc(context)
        if doc and path:
            doc["path"] = path
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=raw)
        return ConversationHandler.END
    text, markup = await _course_list_keyboard(db, context, 1)
    if text is None:
        await safe_edit_message(query, "Open a category first.", action_key=raw)
        return UPDATE_DOC
    await safe_edit_message(query, text, reply_markup=markup, action_key=raw, parse_mode="Markdown")
    return UPDATE_COURSE


async def update_course_page(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    try:
        page = int(query.data.split("::")[-1])
    except Exception:
        page = 1
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
        return ConversationHandler.END
    text, markup = await _course_list_keyboard(db, context, page)
    if text is None:
        await safe_edit_message(query, "Open a category first.", action_key=getattr(query, "data", None))
        return UPDATE_DOC
    await safe_edit_message(query, text, reply_markup=markup, action_key=getattr(query, "data", None), parse_mode="Markdown")
    return UPDATE_COURSE


async def update_back_to_doc(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
        return ConversationHandler.END
    text, markup = await _doc_view_keyboard(db, context, 1)
    if text is None:
        await safe_edit_message(query, "Session expired. Run /update again.", action_key=getattr(query, "data", None))
        return ConversationHandler.END
    await safe_edit_message(query, text, reply_markup=markup, action_key=getattr(query, "data", None), parse_mode="Markdown")
    return UPDATE_DOC


async def update_course_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    raw = query.data
    course_id = None
    name = None
    doc_name = None
    if raw.startswith("update_course_ref::"):
        payload = await _resolve(raw, "update_course")
        if not payload:
            await safe_edit_message(query, "Reference expired. Run /update again.", action_key=raw)
            return ConversationHandler.END
        course_id = payload.get("id")
        name = payload.get("name")
        doc_name = payload.get("doc")
    else:
        parts = raw.split("::")
        token = parts[1] if len(parts) >= 2 else ""
        # id tokens are uuids (unquoted); name fallbacks are quote_plus-encoded
        if token and _looks_like_uuid(token):
            course_id = token
        else:
            name = urllib.parse.unquote_plus(token) if token else None
        doc = _current_doc(context)
        doc_name = (doc or {}).get("name")
    if not doc_name:
        doc = _current_doc(context)
        doc_name = (doc or {}).get("name")

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=raw)
        return ConversationHandler.END
    holder, course, status = await _find_course(db, course_id, name, doc_name)
    if status == "ambiguous":
        await safe_edit_message(
            query,
            "Several courses share that name in this category. Open the course list and try again.",
            action_key=raw,
        )
        return ConversationHandler.END
    if not course:
        await safe_edit_message(query, "Course not found. Run /update again.", action_key=raw)
        return ConversationHandler.END

    draft = {
        "id": course.get("id"),
        "name": course.get("name"),
        "link": course.get("link"),
        "original_name": course.get("name"),
        "original_link": course.get("link"),
        "category": (holder or {}).get("name") or doc_name,
        "doc_name": (holder or {}).get("name") or doc_name,
    }
    context.user_data["update_draft"] = draft
    context.user_data["update_msg"] = {
        "chat_id": getattr(getattr(query, "message", None), "chat_id", None),
        "message_id": getattr(getattr(query, "message", None), "message_id", None),
    }
    await safe_edit_message(query, _dialog_text(draft), reply_markup=_dialog_markup(), action_key=raw, parse_mode="Markdown")
    return UPDATE_DIALOG


# ---------------  dialog actions  ---------------
async def update_edit_name(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    draft = context.user_data.get("update_draft") or {}
    context.user_data["update_msg"] = {
        "chat_id": getattr(getattr(query, "message", None), "chat_id", None),
        "message_id": getattr(getattr(query, "message", None), "message_id", None),
    }
    text = (
        "✏️ Send the new *name* for the course.\n\n"
        f"Current: {_md_code(draft.get('name'))}\n\n"
        "Type the new title as a text message."
    )
    await safe_edit_message(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="update_cancel")]]),
        action_key=query.data,
        parse_mode="Markdown",
    )
    return UPDATE_NAME


async def update_edit_link(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    draft = context.user_data.get("update_draft") or {}
    context.user_data["update_msg"] = {
        "chat_id": getattr(getattr(query, "message", None), "chat_id", None),
        "message_id": getattr(getattr(query, "message", None), "message_id", None),
    }
    text = (
        "🔗 Send the new *link* for the course.\n\n"
        f"Current: {_md_code(draft.get('link'))}\n\n"
        "It must start with http:// or https://."
    )
    await safe_edit_message(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="update_cancel")]]),
        action_key=query.data,
        parse_mode="Markdown",
    )
    return UPDATE_LINK


async def update_name_received(update: Update, context: CallbackContext):
    draft = context.user_data.get("update_draft")
    if not draft:
        await update.message.reply_text("Session expired. Run /update again.")
        return ConversationHandler.END
    new_name = (update.message.text or "").strip()
    if not new_name:
        await update.message.reply_text("Name can't be empty — send a new name, or /cancel.")
        return UPDATE_NAME
    draft["name"] = new_name
    await _edit_dialog(context, _dialog_text(draft), _dialog_markup())
    return UPDATE_DIALOG


async def update_link_received(update: Update, context: CallbackContext):
    from handlers.course_handlers import is_valid_url

    draft = context.user_data.get("update_draft")
    if not draft:
        await update.message.reply_text("Session expired. Run /update again.")
        return ConversationHandler.END
    new_link = (update.message.text or "").strip()
    if not is_valid_url(new_link):
        await update.message.reply_text("❗️ Invalid URL. It must start with http:// or https://. Send it again, or /cancel.")
        return UPDATE_LINK
    draft["link"] = new_link
    await _edit_dialog(context, _dialog_text(draft), _dialog_markup())
    return UPDATE_DIALOG


async def update_dialog_hint(update: Update, context: CallbackContext):
    await update.message.reply_text("Tap ✏️ Edit Name or 🔗 Edit Link first, then press ✅ Okay (or /cancel).")
    return UPDATE_DIALOG


async def update_confirm(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    if not await _guard(query):
        return ConversationHandler.END
    draft = context.user_data.get("update_draft")
    if not draft:
        await safe_edit_message(query, "Session expired. Run /update again.", action_key=query.data)
        return ConversationHandler.END
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=query.data)
        return ConversationHandler.END

    status, holder_name = await _apply_course_update(
        db,
        draft.get("id"),
        draft.get("original_name"),
        draft.get("doc_name"),
        draft.get("name"),
        draft.get("link"),
    )
    if status in ("put", "patch"):
        await _invalidate(db, holder_name)
        verb = "renamed and re-linked" if status == "put" else ("renamed" if draft.get("name") != draft.get("original_name") else "re-linked")
        msg = (
            "✅ *Course updated*\n\n"
            f"📝 {_md_code(draft.get('name'))}\n"
            f"🔗 {_md_code(draft.get('link'))}\n"
            f"📂 {_md_plain(holder_name)}\n\n"
            f"_({verb}, saved to `{holder_name}`)_"
        )
        _reset(context)
        await safe_edit_message(query, msg, action_key=query.data, parse_mode="Markdown")
        return ConversationHandler.END
    if status == "unchanged":
        _reset(context)
        await safe_edit_message(query, "No changes to save — the name and link are unchanged.", action_key=query.data)
        return ConversationHandler.END
    if status == "ambiguous":
        await safe_edit_message(
            query,
            "Cannot update: several courses share that name in this category. Open the course list and try again.",
            action_key=query.data,
        )
        return ConversationHandler.END
    await safe_edit_message(query, "⚠️ Could not save the update. The course may have been moved or deleted.", action_key=query.data)
    return ConversationHandler.END


async def update_cancel(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    _reset(context)
    await safe_edit_message(query, "✏️ Update cancelled.", action_key=query.data)
    return ConversationHandler.END


async def update_cancel_command(update: Update, context: CallbackContext):
    _reset(context)
    try:
        await update.message.reply_text("✏️ Update cancelled.")
    except Exception:
        pass
    return ConversationHandler.END


async def update_stale_callback(update: Update, context: CallbackContext):
    """Fallback for /update buttons whose conversation is no longer active."""
    query = update.callback_query
    await safe_answer(query)
    await safe_edit_message(query, "This update session expired. Run /update again.", action_key=getattr(query, "data", None))
