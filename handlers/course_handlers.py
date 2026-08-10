import logging
import urllib.parse
import uuid

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
from conversation_states import ADD_CATEGORY, ADD_COACH, ADD_LINK, ADD_NAME, ADD_PARENT
from handlers.base_handlers import (
    _fit_cb,
    _resolve_callback_payload,
    _shorten_showcat_cb,
    _store_callback_payload,
    get_total_count,
    safe_answer,
    safe_edit_message,
)
from handlers.db_connection import get_db

COURSE_PAGE_SIZE = 50


# ---------------  compact callbacks (keep callback_data <= 64 bytes)  ---------------


def _addcoach_cb(name) -> str:
    return _fit_cb(
        "addcoach",
        f"addcoach::{urllib.parse.quote_plus(str(name))}",
        {"type": "addcoach", "coach": str(name)},
    )


def _addcoach_page_cb(parent, page: int) -> str:
    p = parent or ""
    return _fit_cb(
        "addcoach_page",
        f"addcoach_page::{urllib.parse.quote_plus(p)}::{page}",
        {"type": "addcoach_page", "parent": p, "page": page},
    )


def _addparent_cb(name, page: int) -> str:
    return _fit_cb(
        "addparent",
        f"addparent::{urllib.parse.quote_plus(str(name))}::{page}",
        {"type": "addparent", "category": str(name), "category_name": str(name), "page": page},
    )


def _addcat_cb(name, page: int) -> str:
    return _fit_cb(
        "addcat",
        f"addcat::{urllib.parse.quote_plus(str(name))}::{page}",
        {"type": "addcat", "category": str(name), "page": page},
    )


logger = logging.getLogger(__name__)


# ----------  registration  ----------
async def setup_course_handlers(application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("add", add_course_start)],
            states={
                ADD_PARENT: [
                    CallbackQueryHandler(parent_selected, pattern=r"^addparent::"),
                    CallbackQueryHandler(parent_selected, pattern=r"^addparent_ref::"),
                    CallbackQueryHandler(addparent_page, pattern=r"^addparent_page::"),
                ],
                ADD_COACH: [
                    CallbackQueryHandler(coach_selected, pattern=r"^addcoach::"),
                    CallbackQueryHandler(coach_selected, pattern=r"^addcoach_ref::"),
                    CallbackQueryHandler(addcoach_page, pattern=r"^addcoach_page::"),
                    CallbackQueryHandler(addcoach_page, pattern=r"^addcoach_page_ref::"),
                    CallbackQueryHandler(addparent_page, pattern=r"^addparent_page::"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, coach_manual_entry),
                ],
                ADD_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_name),
                ],
                ADD_LINK: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_link),
                ],
                ADD_CATEGORY: [CallbackQueryHandler(category_selected, pattern=r"^addcat")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            name="add_course_conv",
            persistent=False,
        ),
    )


# ----------  entry commands  ----------
async def start(update: Update, context: CallbackContext):
    user = update.message.from_user
    name = user.first_name or "there"
    await update.message.reply_text(
        f"👋 Welcome **{name}** to the Course Manager Bot! 🎉\n\n"
        f"I'll help you organize and manage your courses. Here's what I can do:\n\n"
        f"📚 **Browse** — Use /categories or /courses to explore\n"
        f"➕ **Add** — Use /add to add new courses\n"
        f"🔍 **Search** — Look for courses and categories\n"
        f"🗑️ **Manage** — Delete courses, categories, or parents\n\n"
        f"Type /help anytime to see all available commands. 😊",
    )


async def add_course_start(update: Update, context: CallbackContext):
    keyboard = []
    user_id = None
    try:
        user_id = (
            update.message.from_user.id
            if getattr(update, "message", None) and getattr(update.message, "from_user", None)
            else getattr(update.effective_user, "id", None)
        )
    except Exception:
        user_id = None
    if not is_owner(user_id):
        try:
            await update.message.reply_text("⛔ Only the bot owner can run this command.")
        except Exception:
            logger.warning("Failed to send owner-only guard message (user may have blocked bot)")
        return ConversationHandler.END

    try:
        db = await get_db()
    except Exception:
        db = None

    last_viewed = None
    try:
        last_viewed = context.user_data.pop("last_viewed_category", None)
        if not last_viewed:
            last_viewed = context.user_data.pop("last_viewed_category_id", None)
    except Exception:
        last_viewed = None

    if last_viewed and db is not None:
        try:
            query_q = {"$or": [{"name": last_viewed}, {"path": last_viewed}, {"id": last_viewed}]}
            parent_doc = await db.categories.find_one(query_q)
            if parent_doc:
                if parent_doc.get("parent"):
                    context.user_data["course_parent"] = parent_doc.get("parent")
                    context.user_data["course_coach"] = parent_doc.get("name")
                    coach_name = parent_doc.get("name")
                    coach_parent = parent_doc.get("parent")
                    if coach_parent:
                        await update.message.reply_text(
                            f"Adding a course inside coach '{coach_name}' under parent '{coach_parent}'.\nEnter the course name:",
                        )
                    else:
                        await update.message.reply_text(
                            f"Adding a course inside '{coach_name}' (coach).\nEnter the course name:",
                        )
                    return ADD_NAME
                parent_name = parent_doc.get("name")
                context.user_data["course_parent"] = parent_name
                try:
                    page_size = COURSE_PAGE_SIZE
                    start = 0
                    child_cats = (
                        await db.categories.find({"parent": parent_name})
                        .sort("name", 1)
                        .skip(start)
                        .limit(page_size)
                        .to_list(length=page_size)
                    )
                    keyboard = []
                    if child_cats:
                        for child in child_cats:
                            keyboard.append(
                                [
                                    InlineKeyboardButton(
                                        child.get("name"),
                                        callback_data=_addcoach_cb(child.get("name")),
                                    ),
                                ],
                            )
                        keyboard.append(
                            [InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")],
                        )
                        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])
                        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
                        await update.message.reply_text(
                            f"Choose a coach for new course under '{parent_name}':",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                        )
                        return ADD_COACH
                except Exception:
                    context.user_data.pop("course_parent", None)
            else:
                pass
        except Exception:
            context.user_data.pop("course_parent", None)

    try:
        total = (
            await get_total_count(
                db,
                "categories",
                {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]},
                ttl=15,
            )
            if db is not None
            else 0
        )
        page = 1
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        parents = (
            await db.categories.find({"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]})
            .sort("name", 1)
            .skip(start)
            .limit(page_size)
            .to_list(length=page_size)
            if db is not None
            else []
        )
    except Exception:
        total = 0
        parents = []

    if not parents:
        await update.message.reply_text("Enter the name of the course:")
        return ADD_NAME

    keyboard = []
    keyboard.append([InlineKeyboardButton("(Add to top-level)", callback_data="addparent::")])
    for p in parents:
        display = f"{p.get('name')}"
        keyboard.append(
            [InlineKeyboardButton(display, callback_data=_addparent_cb(p.get("name"), 1))],
        )

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addparent_page::{page - 1}"))
    if page > 1:
        nav.append(InlineKeyboardButton("🏠 Home", callback_data="addparent_page::1"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addparent_page::{page + 1}"))
    if total_pages > 1 and page < last_page:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addparent_page::{last_page}"))
    if nav:
        keyboard.append(nav)

    await update.message.reply_text(
        "Choose a parent category for the new course:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADD_PARENT


# ----------  add_course_name  ----------
async def add_course_name(update: Update, context: CallbackContext):
    logger.info("[ADD] add_course_name called by %s", update.effective_user.id)
    name = update.message.text.strip()
    logger.info("[ADD] name received: %r", name)
    if not name:
        await update.message.reply_text("Name can’t be empty – try again.")
        return ADD_NAME

    context.user_data["course_name"] = name
    await update.message.reply_text("Please enter the course link (it should start with http:// or https://).")
    return ADD_LINK


async def add_course_link(update: Update, context: CallbackContext):
    link = update.message.text.strip()

    if not is_valid_url(link):
        await update.message.reply_text("❗️ Invalid URL. Please provide a valid link (http:// or https://).")
        return ADD_LINK

    context.user_data["course_link"] = link
    logger.info("[ADD] Course link received: %s", link)

    parent = context.user_data.get("course_parent")
    coach = context.user_data.get("course_coach")

    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("❗️ Could not connect to the database. Try again later.")
            return ConversationHandler.END
        categories_coll = db["categories"]
        view_cat = None
        if parent is not None:
            if coach:
                child_doc = await db["categories"].find_one({"name": coach, "parent": parent})
                if child_doc:
                    course_doc = {"id": str(uuid.uuid4()), "name": context.user_data.get("course_name"), "link": link}
                    update_result = await categories_coll.update_one(
                        {"name": coach, "parent": parent},
                        {"$push": {"courses": course_doc}},
                    )
                    logger.info(
                        "[ADD-COURSE] saved to child coach=%s under parent=%s result=%s",
                        coach,
                        parent,
                        getattr(update_result, "raw_result", update_result),
                    )
                else:
                    course_doc = {
                        "id": str(uuid.uuid4()),
                        "name": context.user_data.get("course_name"),
                        "link": link,
                        "coach": coach,
                    }
                    update_result = await categories_coll.update_one(
                        {"name": parent},
                        {"$push": {"courses": course_doc}},
                    )

                    try:
                        updated_cat = await categories_coll.find_one({"name": parent})
                        logger.info(
                            "[ADD-COURSE] parent=%s now has %d courses: %s",
                            parent,
                            len(updated_cat.get("courses", [])),
                            [c.get("name") for c in updated_cat.get("courses", [])],
                        )
                    except Exception:
                        logger.debug("[ADD-COURSE] unable to fetch updated category %s for logging", parent)
            else:
                course_doc = {"id": str(uuid.uuid4()), "name": context.user_data.get("course_name"), "link": link}
                update_result = await categories_coll.update_one({"name": parent}, {"$push": {"courses": course_doc}})
            logger.info(
                "[ADD-COURSE] saved to parent=%s result=%s",
                parent,
                getattr(update_result, "raw_result", update_result),
            )
            if update_result.modified_count == 0:
                await update.message.reply_text(f"Error: Parent category '{parent}' not found. Create it first.")
                return ConversationHandler.END

            if coach:
                child_doc = await db["categories"].find_one({"name": coach, "parent": parent})
            else:
                child_doc = None

            if coach and child_doc:
                view_cat = coach
            else:
                view_cat = parent
        if view_cat:
            current_page = context.user_data.get("last_category_page", 1)

            kb_buttons = []
            kb_buttons.append(
                InlineKeyboardButton(
                    f'View "{view_cat}"',
                    callback_data=_shorten_showcat_cb(
                        view_cat,
                        current_page,
                        from_parent="categories",
                        parent_page=current_page,
                    ),
                ),
            )
            if coach and child_doc:
                try:
                    kb_buttons.append(
                        InlineKeyboardButton(
                            f'View Parent "{parent}"',
                            callback_data=_shorten_showcat_cb(
                                parent,
                                current_page,
                                from_parent="categories",
                                parent_page=current_page,
                            ),
                        ),
                    )
                except Exception:
                    pass
            kb_rows = []
            for i in range(0, len(kb_buttons), 2):
                kb_rows.append(kb_buttons[i : i + 2])
            kb = InlineKeyboardMarkup(kb_rows)

            await update.message.reply_text(
                f"Course '{context.user_data.get('course_name')}' added successfully to '{parent}'. 🎉\nLink: {link}",
                reply_markup=kb,
            )
            return ConversationHandler.END
        await update.message.reply_text(
            f"Course '{context.user_data.get('course_name')}' added successfully to '{parent}'. 🎉\nLink: {link}",
        )
        return ConversationHandler.END
    except Exception:
        logger.exception("Error saving course link")
        try:
            await update.message.reply_text("An error occurred while saving the course. Check bot logs for details.")
        except Exception:
            logger.debug("Failed to send error feedback to user after save failure")
        return ConversationHandler.END


# ----------  pickers  ----------
async def parent_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END
    raw = query.data
    parent = None
    origin_page = None
    if raw.startswith("addparent_ref::"):
        key = raw.split("::", 1)[1]
        payload = await _resolve_callback_payload(key)
        if not payload:
            await safe_edit_message(
                query,
                "Reference expired. Please reopen the category and try again.",
                action_key=getattr(query, "data", None),
            )
            return ConversationHandler.END
        parent = payload.get("category") or payload.get("category_name")
        try:
            if payload.get("page"):
                origin_page = int(payload.get("page"))
        except Exception:
            origin_page = None
    elif raw.startswith("addparent::"):
        parts = raw.split("::")
        if len(parts) >= 2 and parts[1] != "":
            parent = urllib.parse.unquote_plus(parts[1])
        if len(parts) >= 3:
            try:
                origin_page = int(parts[2])
            except Exception:
                origin_page = None
    else:
        encoded = query.data.split("::", 1)[1] if "::" in query.data else ""
        parent = urllib.parse.unquote_plus(encoded) if encoded else None

    context.user_data["course_parent"] = parent
    if origin_page:
        context.user_data["last_category_page"] = origin_page

    try:
        db = await get_db()
        if parent:
            child_count = await get_total_count(db, "categories", {"parent": parent}, ttl=15)
            page_size = COURSE_PAGE_SIZE
            start = (1 - 1) * page_size
            child_cats = (
                await db.categories.find({"parent": parent})
                .sort("name", 1)
                .skip(start)
                .limit(page_size)
                .to_list(length=page_size)
            )
            sorted_children = sorted(child_cats, key=lambda c: (c.get("name") or "").lower())
        else:
            child_count = 0
            child_cats = []
            sorted_children = []
    except Exception:
        child_cats = []

    keyboard = []
    if child_cats:
        sorted_children = sorted(child_cats, key=lambda c: (c.get("name") or "").lower())
        page = 1
        page_size = COURSE_PAGE_SIZE
        page_children = sorted_children
        for child in page_children:
            display = f"{child.get('name')}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        display, callback_data=_addcoach_cb(child.get("name"))
                    )
                ],
            )
        nav = []
        total_pages = (child_count - 1) // page_size + 1 if child_count else 1
        last_page = max(1, total_pages)
        if total_pages > 1:
            if page == 1:
                if page < last_page:
                    nav.append(
                        InlineKeyboardButton(
                            "➡️ Next",
                            callback_data=_addcoach_page_cb(parent, page + 1),
                        ),
                    )
                    nav.append(
                        InlineKeyboardButton(
                            "⏭️ End",
                            callback_data=_addcoach_page_cb(parent, last_page),
                        ),
                    )
            elif page < last_page:
                nav.append(
                    InlineKeyboardButton(
                        "⬅️ Previous",                            callback_data=_addcoach_page_cb(parent, page - 1),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "🏠 Home",                            callback_data=_addcoach_page_cb(parent, 1),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "⏭️ End",
                        callback_data=_addcoach_page_cb(parent, last_page),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "➡️ Next",
                        callback_data=_addcoach_page_cb(parent, page + 1),
                    ),
                )
            elif page > 1:
                nav.append(
                    InlineKeyboardButton(
                        "⬅️ Previous",                            callback_data=_addcoach_page_cb(parent, page - 1),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "🏠 Home",                            callback_data=_addcoach_page_cb(parent, 1),
                    ),
                )
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])
    else:
        page = 1
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        try:
            filter_q = {"$or": [{"name": parent}, {"parent": parent}]} if parent else {}

            count_pipeline = [
                {"$match": filter_q},
                {"$unwind": "$courses"},
                {"$match": {"courses.coach": {"$exists": True, "$ne": ""}}},
                {"$group": {"_id": "$courses.coach"}},
                {"$count": "count"},
            ]
            try:
                from handlers.base_handlers import _redis

                coach_cache_key = f"coach_count:dst:{parent or ''}"
                cached_total = None
                if _redis is not None:
                    try:
                        val = await _redis.get(coach_cache_key)
                        if val is not None:
                            cached_total = int(val)
                    except Exception:
                        pass
                if cached_total is not None:
                    total_coaches = cached_total
                else:
                    cnt_res = await db.categories.aggregate(count_pipeline).to_list(length=1)
                    total_coaches = int(cnt_res[0].get("count")) if cnt_res else 0
                    if _redis is not None:
                        try:
                            await _redis.setex(coach_cache_key, 30, str(total_coaches))
                        except Exception:
                            pass
            except Exception:
                cnt_res = await db.categories.aggregate(count_pipeline).to_list(length=1)
                total_coaches = int(cnt_res[0].get("count")) if cnt_res else 0

            pipeline = [
                {"$match": filter_q},
                {"$unwind": "$courses"},
                {"$match": {"courses.coach": {"$exists": True, "$ne": ""}}},
                {"$group": {"_id": "$courses.coach"}},
                {"$sort": {"_id": 1}},
                {"$skip": start},
                {"$limit": page_size},
            ]
            docs = await db.categories.aggregate(pipeline).to_list(length=page_size)
            page_coaches = [d.get("_id") for d in docs if d and d.get("_id")]
        except Exception:
            page_coaches = []
            total_coaches = 0

        for coach in page_coaches:
            keyboard.append([InlineKeyboardButton(coach, callback_data=_addcoach_cb(coach))])

        nav = []
        total_pages = (total_coaches - 1) // page_size + 1 if total_coaches else 1
        last_page = max(1, total_pages)
        if total_pages > 1:
            if page == 1:
                if page < last_page:
                    nav.append(
                        InlineKeyboardButton(
                            "➡️ Next",
                            callback_data=_addcoach_page_cb(parent, page + 1),
                        ),
                    )
                    nav.append(
                        InlineKeyboardButton(
                            "⏭️ End",
                            callback_data=_addcoach_page_cb(parent, last_page),
                        ),
                    )
            elif page < last_page:
                nav.append(
                    InlineKeyboardButton(
                        "⬅️ Previous",                            callback_data=_addcoach_page_cb(parent, page - 1),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "🏠 Home",                            callback_data=_addcoach_page_cb(parent, 1),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "⏭️ End",
                        callback_data=_addcoach_page_cb(parent, last_page),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "➡️ Next",
                        callback_data=_addcoach_page_cb(parent, page + 1),
                    ),
                )
            elif page > 1:
                nav.append(
                    InlineKeyboardButton(
                        "⬅️ Previous",                            callback_data=_addcoach_page_cb(parent, page - 1),
                    ),
                )
                nav.append(
                    InlineKeyboardButton(
                        "🏠 Home",                            callback_data=_addcoach_page_cb(parent, 1),
                    ),
                )
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])
    try:
        if parent:
            parent_page = context.user_data.get("last_category_page", 1)
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"addparent_page::{parent_page}")])
    except Exception:
        pass

    await safe_edit_message(
        query,
        "Choose a coach for this course (or enter one manually):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        action_key=getattr(query, "data", None),
    )
    return ADD_COACH


async def addcoach_page(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END
    data = query.data
    parent = None
    page = 1
    if data.startswith("addcoach_page_ref::"):
        try:
            payload = await _resolve_callback_payload(data.split("::", 1)[1])
            if payload:
                parent = payload.get("parent") or None
                try:
                    page = int(payload.get("page") or 1)
                except Exception:
                    page = 1
        except Exception:
            parent = None
            page = 1
    else:
        parts = data.split("::")
        if len(parts) < 3:
            await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
            return None
        parent_enc = parts[1]
        try:
            page = int(parts[2])
        except Exception:
            page = 1
        parent = urllib.parse.unquote_plus(parent_enc) if parent_enc else None
    context.user_data["last_coach_page"] = page

    try:
        db = await get_db()
        if parent:
            total_children = await get_total_count(db, "categories", {"parent": parent}, ttl=15)
            page_size = COURSE_PAGE_SIZE
            start = (page - 1) * page_size
            children = (
                await db.categories.find({"parent": parent})
                .sort("name", 1)
                .skip(start)
                .limit(page_size)
                .to_list(length=page_size)
            )
        else:
            total_children = 0
            children = []
    except Exception:
        children = []

    keyboard = []
    if children:
        sorted_children = sorted(children, key=lambda c: (c.get("name") or "").lower())
        page_size = COURSE_PAGE_SIZE
        total_pages = (total_children - 1) // page_size + 1 if total_children else 1
        last_page = max(1, total_pages)

        for child in sorted_children:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        child.get("name"),
                        callback_data=_addcoach_cb(child.get("name")),
                    ),
                ],
            )

        nav = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    "⬅️ Previous",
                    callback_data=_addcoach_page_cb(parent, page - 1),
                ),
            )
        if page > 1:
            nav.append(
                InlineKeyboardButton(
                    "🏠 Home",
                    callback_data=_addcoach_page_cb(parent, 1),
                ),
            )
        if page < last_page:
            nav.append(
                InlineKeyboardButton(
                    "➡️ Next",
                    callback_data=_addcoach_page_cb(parent, page + 1),
                ),
            )
        if total_pages > 1 and page < last_page:
            nav.append(
                InlineKeyboardButton(
                    "⏭️ End",
                    callback_data=_addcoach_page_cb(parent, last_page),
                ),
            )
        if nav:
            keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
    keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])

    try:
        if parent:
            parent_page = context.user_data.get("last_category_page", 1)
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"addparent_page::{parent_page}")])
    except Exception:
        pass

    await safe_edit_message(
        query,
        "Choose a coach for this course (or enter one manually):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        action_key=getattr(query, "data", None),
    )
    return ADD_COACH


async def addparent_page(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END
    data = query.data
    parts = data.split("::")
    try:
        page = int(parts[1])
    except Exception:
        page = 1
    context.user_data["last_category_page"] = page

    try:
        db = await get_db()
        total = await get_total_count(
            db,
            "categories",
            {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]},
            ttl=15,
        )
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        parents = (
            await db.categories.find({"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]})
            .sort("name", 1)
            .skip(start)
            .limit(page_size)
            .to_list(length=page_size)
        )
    except Exception:
        total = 0
        parents = []

    keyboard = []
    keyboard.append([InlineKeyboardButton("(Add to top-level)", callback_data="addparent::")])
    for p in parents:
        display = f"{p.get('name')}"
        keyboard.append(
            [
                InlineKeyboardButton(
                    display,
                    callback_data=_addparent_cb(p.get("name"), page),
                ),
            ],
        )

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if total_pages > 1:
        if page == 1:
            if page < last_page:
                nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addparent_page::{page + 1}"))
                nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addparent_page::{last_page}"))
        elif page < last_page:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addparent_page::{page - 1}"))
            nav.append(InlineKeyboardButton("🏠 Home", callback_data="addparent_page::1"))
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addparent_page::{last_page}"))
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addparent_page::{page + 1}"))
        elif page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addparent_page::{page - 1}"))
            nav.append(InlineKeyboardButton("🏠 Home", callback_data="addparent_page::1"))
    if nav:
        keyboard.append(nav)

    await safe_edit_message(
        query,
        f"Choose a parent category for the new course (page {page}/{last_page}):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        action_key=getattr(query, "data", None),
    )
    return ADD_PARENT


async def addcat_page(update_or_message, context: CallbackContext, *, page: int = 1):
    query = getattr(update_or_message, "callback_query", None)
    is_query = query is not None
    if is_query:
        await safe_answer(query)
        user_id = getattr(query.from_user, "id", None)
        if not is_owner(user_id):
            await safe_edit_message(
                query,
                "⛔ Only the bot owner can run this command.",
                action_key=getattr(query, "data", None),
            )
            return ConversationHandler.END
        data = query.data
        parts = data.split("::")
        try:
            page = int(parts[1])
        except Exception:
            page = 1
    context.user_data["last_category_page"] = page

    try:
        db = await get_db()
        total = await get_total_count(db, "categories", {}, ttl=15)
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        cats = await db.categories.find({}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
    except Exception:
        total = 0
        cats = []

    page_cats = cats

    keyboard = []
    for c in page_cats:
        display = c.get("name")
        keyboard.append(
            [InlineKeyboardButton(display, callback_data=_addcat_cb(c.get("name"), page))],
        )

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if total_pages > 1:
        if page == 1:
            if page < last_page:
                nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcat_page::{page + 1}"))
                nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcat_page::{last_page}"))
        elif page < last_page:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcat_page::{page - 1}"))
            nav.append(InlineKeyboardButton("🏠 Home", callback_data="addcat_page::1"))
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcat_page::{last_page}"))
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcat_page::{page + 1}"))
        elif page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcat_page::{page - 1}"))
            nav.append(InlineKeyboardButton("🏠 Home", callback_data="addcat_page::1"))
    if nav:
        keyboard.append(nav)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_query:
        await safe_edit_message(
            query,
            f"Pick a category for the course (page {page}/{last_page}):",
            reply_markup=reply_markup,
            action_key=getattr(query, "data", None),
        )
    else:
        await update_or_message.reply_text(
            f"Pick a category for the course (page {page}/{last_page}):",
            reply_markup=reply_markup,
        )
    return None


# ----------  selections  ----------
async def coach_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END
    raw = query.data
    coach = None
    if raw.startswith("addcoach_ref::"):
        try:
            payload = await _resolve_callback_payload(raw.split("::", 1)[1])
            if payload:
                coach = payload.get("coach") or payload.get("category")
        except Exception:
            coach = None
    else:
        encoded = raw.split("::", 1)[1] if "::" in raw else ""
        if encoded == "__manual__":
            await query.message.reply_text("Send the coach name (text):")
            return ADD_COACH
        coach = urllib.parse.unquote_plus(encoded) if encoded else None
    context.user_data["course_coach"] = coach
    await query.message.reply_text("Enter the name of the course:")
    return ADD_NAME


async def coach_manual_entry(update: Update, context: CallbackContext):
    coach = update.message.text.strip()
    if not coach:
        await update.message.reply_text("Coach name cannot be empty — try again.")
        return ADD_COACH
    context.user_data["course_coach"] = coach
    await update.message.reply_text("Enter the name of the course:")
    return ADD_NAME


async def category_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END

    raw = query.data
    category_name = None
    origin_page = None
    if raw.startswith("addcat_ref::"):
        try:
            payload = await _resolve_callback_payload(raw.split("::", 1)[1])
            if payload:
                category_name = payload.get("category") or payload.get("category_name")
                try:
                    origin_page = int(payload.get("page")) if payload.get("page") else None
                except Exception:
                    origin_page = None
        except Exception:
            category_name = None
            origin_page = None
    elif raw.startswith("addcat::"):
        parts = raw.split("::")
        if len(parts) >= 2:
            category_name = urllib.parse.unquote_plus(parts[1])
        if len(parts) >= 3:
            try:
                origin_page = int(parts[2])
            except Exception:
                origin_page = None
    else:
        encoded = query.data.split("_", 1)
        if len(encoded) < 2:
            await safe_edit_message(query, "Invalid category callback format.", action_key=getattr(query, "data", None))
            return ConversationHandler.END
        category_name = urllib.parse.unquote_plus(encoded[1])

    course_name = context.user_data.get("course_name")
    course_link = context.user_data.get("course_link")

    if not course_name or not course_link:
        await safe_edit_message(
            query,
            "Error: Course data is missing. Please try again.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END

    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END

    try:
        categories_coll = db["categories"]
        coach = context.user_data.get("course_coach")
        course_doc = {"id": str(uuid.uuid4()), "name": course_name, "link": course_link}
        if coach:
            course_doc["coach"] = coach
        update_result = await categories_coll.update_one({"name": category_name}, {"$push": {"courses": course_doc}})
        logger.info("[ADD-COURSE] update_result=%s", getattr(update_result, "raw_result", update_result))

        if update_result.modified_count == 0:
            logger.warning("[ADD-COURSE] Category not found: %s", category_name)
            await safe_edit_message(
                query,
                f"Error: Category '{category_name}' not found. Create it first.",
                action_key=getattr(query, "data", None),
            )
            return ConversationHandler.END

        msg = (
            f"Course '{course_name}' added successfully to the '{category_name}' category. 🎉\n"
            f"Course Link: {course_link}"
        )
        try:
            view_page = origin_page or 1
            try:
                payload = {
                    "type": "showcat",
                    "path": category_name,
                    "from_parent": "categories",
                    "parent_page": view_page,
                }
                key = _store_callback_payload(payload)
                cb = f"showcat_ref::{key}"
            except Exception:
                cb = _shorten_showcat_cb(category_name, view_page, from_parent="categories", parent_page=view_page)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("View Category", callback_data=cb)]])
            await safe_edit_message(query, msg, reply_markup=kb, action_key=getattr(query, "data", None))
        except Exception:
            await safe_edit_message(query, msg, action_key=getattr(query, "data", None))
        return ConversationHandler.END

    except Exception:
        logger.exception("Error saving course")
        await safe_edit_message(
            query,
            "An error occurred while saving the course. Please try again later.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END


# ----------  conversation end  ----------
async def cancel(update: Update, context: CallbackContext) -> int:
    try:
        if getattr(update, "message", None) is not None:
            reply_to = getattr(update.message, "reply_to_message", None)
            if reply_to is not None and getattr(reply_to, "message_id", None) is not None:
                try:
                    await reply_to.edit_text("Operation canceled.")
                except Exception:
                    await update.message.reply_text("Operation canceled.")
            else:
                await update.message.reply_text("Operation canceled.")
        elif getattr(update, "callback_query", None) is not None:
            cq = update.callback_query
            try:
                await cq.answer()
            except Exception:
                pass
            try:
                await safe_edit_message(cq, "Operation canceled.", action_key=getattr(cq, "data", None))
            except Exception:
                try:
                    await cq.message.reply_text("Operation canceled.")
                except Exception:
                    pass
    except Exception:
        pass
    try:
        if context and getattr(context, "user_data", None) is not None:
            context.user_data.clear()
    except Exception:
        logger.warning("Failed to clear conversation user_data; potential memory leak", exc_info=True)
    return ConversationHandler.END


# ----------  validation & errors  ----------
def is_valid_url(url: str):
    if not url or len(url) > 2048:
        return False
    if any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


async def course_error_handler(update, context):
    try:
        err = getattr(context, "error", context)
        logger.error("Error: %s", err)
        if update is None:
            return
        if getattr(update, "message", None) is not None:
            await update.message.reply_text("An unexpected error occurred. Please try again later.")
        elif getattr(update, "callback_query", None) is not None:
            cq = update.callback_query
            try:
                await cq.answer()
            except Exception:
                pass
            try:
                await safe_edit_message(
                    cq,
                    "An unexpected error occurred. Please try again later.",
                    action_key=getattr(cq, "data", None),
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Error in error_handler")
