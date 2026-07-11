from telegram.ext import ConversationHandler, MessageHandler, CommandHandler, CallbackQueryHandler, filters, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from conversation_states import ADD_NAME, ADD_LINK, ADD_CATEGORY, ADD_PARENT, ADD_COACH
from handlers.db_connection import get_db
from pymongo.errors import PyMongoError
import logging
import re
import urllib.parse
import os
from handlers.base_handlers import safe_edit_message, safe_answer, _shorten_showcat_cb, _store_callback_payload, _resolve_callback_payload, get_total_count, _get_total_count
import uuid

# Page size used only by course-related handlers (coaches/categories/courses in add flow)
COURSE_PAGE_SIZE = 50


async def _compute_category_page(db, category_name, page_size=COURSE_PAGE_SIZE):
    """Compute the 1-based page number where `category_name` appears among
    top-level categories sorted A→Z. Returns 1 when not found.
    """
    try:
        # Find the matching top-level category doc first
        doc = await db.categories.find_one({"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}], "$or": [{"name": category_name}, {"path": category_name}]}, projection={"name": 1, "path": 1})
        if not doc:
            return 1
        key = doc.get('name') or doc.get('path') or ''
        # Count how many top-level categories sort before this one (lexicographic by name/path)
        count = await _get_total_count(db, 'categories', {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}], "$or": [{"name": {"$lt": key}}, {"name": {"$exists": False}, "path": {"$lt": key}}]}, ttl=15)
        return (count // page_size) + 1
    except Exception:
        return 1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states are defined in conversation_states.py

async def setup_course_handlers(application):
    # /start: simple welcome message using the user's Telegram name
    application.add_handler(CommandHandler("start", start))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_course_start)],
        states={
            # First: pick a parent/top-level category
            ADD_PARENT: [
                CallbackQueryHandler(parent_selected, pattern=r"^addparent::"),
                CallbackQueryHandler(addparent_page, pattern=r"^addparent_page::")
            ],

            # Then: pick a coach (buttons) or enter one manually (text)
            ADD_COACH: [
                CallbackQueryHandler(coach_selected, pattern=r"^addcoach::"),
                CallbackQueryHandler(addcoach_page, pattern=r"^addcoach_page::"),
                # Allow navigating back to parent pages while in the coach-selection state
                CallbackQueryHandler(addparent_page, pattern=r"^addparent_page::"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, coach_manual_entry),
            ],

            # Then: course name (text) — include explicit cancel matcher so /cancel always works
            ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_name),
            ],

            # Then: course link (text)
            ADD_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_link),
            ],

            # Legacy: allow selecting an arbitrary category at the end if needed
            ADD_CATEGORY:[CallbackQueryHandler(category_selected, pattern=r"^addcat")]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="add_course_conv",
        persistent=False
    ))

async def start(update: Update, context: CallbackContext):
    """Handler for the /start command — welcomes the user with their Telegram name."""
    user = update.message.from_user
    name = user.first_name or "there"
    await update.message.reply_text(
        f"👋 Welcome **{name}** to the Course Manager Bot! 🎉\n\n"
        f"I'll help you organize and manage your courses. Here's what I can do:\n\n"
        f"📚 **Browse** — Use /categories or /courses to explore\n"
        f"➕ **Add** — Use /add to add new courses\n"
        f"🔍 **Search** — Look for courses and categories\n"
        f"🗑️ **Manage** — Delete courses, categories, or parents\n\n"
        f"Type /help anytime to see all available commands. 😊"
    )
    
# course_handlers.py
async def add_course_start(update: Update, context: CallbackContext):
    """Start add flow: prompt the user to pick a parent/top-level category.

    If no top-level parents exist, fall back to asking for the course name.
    """
    keyboard = []  # ensure keyboard is always initialized (fixes UnboundLocalError)
    # Owner-only: restrict /add to configured bot owner
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = None
    try:
        user_id = update.message.from_user.id if getattr(update, 'message', None) and getattr(update.message, 'from_user', None) else getattr(update.effective_user, 'id', None)
    except Exception:
        user_id = None
    if owner_id is not None and user_id != owner_id:
        try:
            await update.message.reply_text("Unauthorized")
        except Exception:
            pass
        return ConversationHandler.END

    try:
        db = await get_db()
    except Exception:
        db = None

    # If the user recently viewed a category (or coach), preselect it as the parent for /add.
    last_viewed = None
    try:
        last_viewed = context.user_data.pop('last_viewed_category', None)
        if not last_viewed:
            # fall back to stored id if available
            last_viewed = context.user_data.pop('last_viewed_category_id', None)
    except Exception:
        last_viewed = None

    if last_viewed and db is not None:
        try:
            # Resolve by name/path/id to get the canonical category doc
            query_q = {"$or": [{"name": last_viewed}, {"path": last_viewed}, {"id": last_viewed}]}
            parent_doc = await db.categories.find_one(query_q)
            if parent_doc:
                # If the resolved doc has a parent, it is a child category (coach)
                if parent_doc.get('parent'):
                    # The user is viewing a coach child — preselect the parent and coach
                    context.user_data['course_parent'] = parent_doc.get('parent')
                    context.user_data['course_coach'] = parent_doc.get('name')
                    # Mention the coach and its parent in plain text (not breadcrumb)
                    coach_name = parent_doc.get('name')
                    coach_parent = parent_doc.get('parent')
                    if coach_parent:
                        await update.message.reply_text(f"Adding a course inside coach '{coach_name}' under parent '{coach_parent}'.\nEnter the course name:")
                    else:
                        await update.message.reply_text(f"Adding a course inside '{coach_name}' (coach).\nEnter the course name:")
                    return ADD_NAME
                else:
                    # It's a top-level parent — preselect it and show coach-selection UI
                    parent_name = parent_doc.get('name')
                    context.user_data['course_parent'] = parent_name
                    try:
                        child_count = await get_total_count(db, 'categories', {"parent": parent_name}, ttl=15)
                        page_size = COURSE_PAGE_SIZE
                        start = 0
                        child_cats = await db.categories.find({"parent": parent_name}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
                        keyboard = []
                        if child_cats:
                            for child in child_cats:
                                keyboard.append([InlineKeyboardButton(child.get('name'), callback_data=f"addcoach::{urllib.parse.quote_plus(child.get('name'))}")])
                            keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
                            keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])
                            # Back button returns to categories listing
                            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
                            await update.message.reply_text(f"Choose a coach for new course under '{parent_name}':", reply_markup=InlineKeyboardMarkup(keyboard))
                            return ADD_COACH
                    except Exception:
                        context.user_data.pop('course_parent', None)
            else:
                # resolved doc not found — fall back to default UI
                pass
        except Exception:
            # ignore and continue to default add flow
            context.user_data.pop('course_parent', None)

    # Default behavior: list top-level parents for selection
    try:
        # Use server-side pagination for top-level parents
        total = await get_total_count(db, 'categories', {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}, ttl=15) if db is not None else 0
        page = 1
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        parents = await db.categories.find({"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size) if db is not None else []
    except Exception:
        total = 0
        parents = []

    if not parents:
        # No parents to choose from — continue with legacy flow (ask name)
        await update.message.reply_text("Enter the name of the course:")
        return ADD_NAME

    keyboard = []
    # Allow top-level (no parent) explicitly
    keyboard.append([InlineKeyboardButton("(Add to top-level)", callback_data="addparent::")])
    for p in parents:
        # Keep the add flow fast: do not check emptiness here to avoid extra DB calls.
        display = f"{p.get('name')}"
        keyboard.append([InlineKeyboardButton(display, callback_data=f"addparent::{urllib.parse.quote_plus(p.get('name'))}::1")])

    # Navigation row
    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addparent_page::{page-1}"))
    # Home for add flow: go to first page (only show on later pages)
    if page > 1:
        nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addparent_page::1"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addparent_page::{page+1}"))
    if total_pages > 1 and page < last_page:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addparent_page::{last_page}"))
    if nav:
        keyboard.append(nav)

    await update.message.reply_text("Choose a parent category for the new course:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_PARENT

# ----------  add_course_name  ----------
async def add_course_name(update: Update, context: CallbackContext):
    logger.info("[ADD] add_course_name called by %s", update.effective_user.id)
    name = update.message.text.strip()
    logger.info("[ADD] name received: %r", name)
    if not name:
        await update.message.reply_text("Name can’t be empty – try again.")
        return ADD_NAME

    context.user_data['course_name'] = name
    await update.message.reply_text("Please enter the course link (it should start with http:// or https://).")
    return ADD_LINK


async def add_course_link(update: Update, context: CallbackContext):
    link = update.message.text.strip()

    # Validate URL strictly (allow only http/https)
    if not is_valid_url(link):
        await update.message.reply_text("❗️ Invalid URL. Please provide a valid link (http:// or https://).")
        return ADD_LINK

    context.user_data['course_link'] = link
    logger.info(f"[ADD] Course link received: {link}")

    # Determine where to save the course: prefer an explicit parent chosen earlier
    parent = context.user_data.get('course_parent')
    coach = context.user_data.get('course_coach')

    try:
        db = await get_db()
        if db is None:
            await update.message.reply_text("❗️ Could not connect to the database. Try again later.")
            return ConversationHandler.END
        categories_coll = db['categories']
        view_cat = None
        # If a parent was selected, save into that parent category
        if parent is not None:
            # If a coach was selected and there exists a child category for that coach,
            # save the course inside that child (coach) category document. Otherwise
            # save into the parent and tag with the coach field.
            if coach:
                child_doc = await db['categories'].find_one({"name": coach, "parent": parent})
                if child_doc:
                    course_doc = {
                        "id": str(uuid.uuid4()),
                        "name": context.user_data.get('course_name'),
                        "link": link
                    }
                    # child coach category: push course into coach's document
                    update_result = await categories_coll.update_one(
                        {"name": coach, "parent": parent},
                        {"$push": {"courses": course_doc}}
                    )
                    logger.info("[ADD-COURSE] saved to child coach=%s under parent=%s result=%s", coach, parent, getattr(update_result, 'raw_result', update_result))
                else:
                    # parent category: include coach field on the course
                    course_doc = {
                        "id": str(uuid.uuid4()),
                        "name": context.user_data.get('course_name'),
                        "link": link,
                        "coach": coach
                    }
                    update_result = await categories_coll.update_one(
                        {"name": parent},
                        {"$push": {"courses": course_doc}}
                    )

                    # Fetch and log updated category for debugging: ensure new course appears
                    try:
                        updated_cat = await categories_coll.find_one({"name": parent})
                        logger.info("[ADD-COURSE] parent=%s now has %d courses: %s", parent, len(updated_cat.get('courses', [])), [c.get('name') for c in updated_cat.get('courses', [])])
                    except Exception:
                        logger.debug("[ADD-COURSE] unable to fetch updated category %s for logging", parent)
            else:
                # parent without coach: push course_doc without coach field
                course_doc = {
                    "id": str(uuid.uuid4()),
                    "name": context.user_data.get('course_name'),
                    "link": link
                }
                update_result = await categories_coll.update_one(
                    {"name": parent},
                    {"$push": {"courses": course_doc}}
                )
            logger.info("[ADD-COURSE] saved to parent=%s result=%s", parent, getattr(update_result, 'raw_result', update_result))
            if update_result.modified_count == 0:
                await update.message.reply_text(f"Error: Parent category '{parent}' not found. Create it first.")
                return ConversationHandler.END

            # Offer a quick button to view the category where the course was added
            if coach:
                child_doc = await db['categories'].find_one({"name": coach, "parent": parent})
            else:
                child_doc = None

            if coach and child_doc:
                view_cat = coach
            else:
                view_cat = parent
        if view_cat:
            current_page = context.user_data.get("last_category_page", 1)

            # Show navigation buttons after adding course
            kb_buttons = []
            # Button to view where the course was added (coach or parent)
            kb_buttons.append(InlineKeyboardButton(
                f"View \"{view_cat}\"",
                callback_data=_shorten_showcat_cb(view_cat, current_page, from_parent="categories", parent_page=current_page)
            ))
            # If we added inside a coach (child category), also offer a quick button to the parent
            if coach and child_doc:
                try:
                    kb_buttons.append(InlineKeyboardButton(
                        f"View Parent \"{parent}\"",
                        callback_data=_shorten_showcat_cb(parent, current_page, from_parent="categories", parent_page=current_page)
                    ))
                except Exception:
                    pass
            # Arrange buttons in rows of up to 2
            kb_rows = []
            for i in range(0, len(kb_buttons), 2):
                kb_rows.append(kb_buttons[i:i+2])
            kb = InlineKeyboardMarkup(kb_rows)

            await update.message.reply_text(
                f"Course '{context.user_data.get('course_name')}' added successfully to '{parent}'. 🎉\nLink: {link}",
                reply_markup=kb
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                f"Course '{context.user_data.get('course_name')}' added successfully to '{parent}'. 🎉\nLink: {link}"
            )
            return ConversationHandler.END
    except Exception as e:
        logger.exception("Error saving course link")
        # Provide a clearer message to the user and include a short hint to check logs
        try:
            await update.message.reply_text("An error occurred while saving the course. Check bot logs for details.")
        except Exception:
            pass
        return ConversationHandler.END


async def parent_selected(update: Update, context: CallbackContext):
    """Callback when a parent is chosen. Presents coach choices next."""
    query = update.callback_query
    await safe_answer(query)
    # Owner-only guard for add flow callbacks
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, 'id', None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
    raw = query.data
    parent = None
    origin_page = None
    if raw.startswith("addparent_ref::"):
        # Resolve stored payload reference created by empty-category Add button
        key = raw.split("::", 1)[1]
        payload = await _resolve_callback_payload(key)
        if not payload:
            await safe_edit_message(query, "Reference expired. Please reopen the category and try again.", action_key=getattr(query, 'data', None))
            return ConversationHandler.END
        parent = payload.get('category') or payload.get('category_name')
    elif raw.startswith("addparent::"):
        parts = raw.split("::")
        # parts -> ['addparent', '<name>' (optional), '<page>' (optional)]
        if len(parts) >= 2 and parts[1] != "":
            parent = urllib.parse.unquote_plus(parts[1])
        if len(parts) >= 3:
            try:
                origin_page = int(parts[2])
            except Exception:
                origin_page = None
    else:
        # fallback
        encoded = query.data.split('::', 1)[1] if '::' in query.data else ''
        parent = urllib.parse.unquote_plus(encoded) if encoded else None

    # store chosen parent (None means add to top-level)
    context.user_data['course_parent'] = parent
    # remember the page we came from to allow returning later
    if origin_page:
        context.user_data['last_category_page'] = origin_page

    # Prefer showing child categories as coach options when coaches are
    # modeled as category documents. This matches the user's workflow where
    # `/create_category` creates coaches.
    try:
        db = await get_db()
        # find child categories of the selected parent
        if parent:
            # Use server-side pagination: count + sort + skip/limit
            child_count = await get_total_count(db, 'categories', {"parent": parent}, ttl=15)
            page_size = COURSE_PAGE_SIZE
            start = (1 - 1) * page_size
            child_cats = await db.categories.find({"parent": parent}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
            sorted_children = sorted(child_cats, key=lambda c: (c.get('name') or '').lower())
        else:
            child_count = 0
            child_cats = []
            sorted_children = []
    except Exception:
        child_cats = []
        sorted_children = []

    keyboard = []
    if child_cats:
        # Paginate child categories (coaches) when many
        sorted_children = sorted(child_cats, key=lambda c: (c.get('name') or '').lower())
        page = 1
        page_size = COURSE_PAGE_SIZE
        # Use DB-provided page slice (already limited)
        page_children = sorted_children
        for child in page_children:
            # Skip emptiness checks to keep pagination responsive in the add flow.
            display = f"{child.get('name')}"
            keyboard.append([InlineKeyboardButton(display, callback_data=f"addcoach::{urllib.parse.quote_plus(child.get('name'))}")])
        # Navigation row — follow desired ordering rules
        nav = []
        total_pages = (child_count - 1) // page_size + 1 if child_count else 1
        last_page = max(1, total_pages)
        if total_pages > 1:
            if page == 1:
                # First page: Next, End (if there are more pages)
                if page < last_page:
                    nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))
                    nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
            elif page < last_page:
                # Middle pages: Prev, Home, End, Next
                nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page-1}"))
                nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::1"))
                nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
                nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))
            else:
                # Last page: Previous and Home only
                if page > 1:
                    nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page-1}"))
                    nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::1"))
        if nav:
            keyboard.append(nav)
        # Also allow manual entry or no coach
        keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])
    else:
        # Fallback: derive coaches from existing course 'coach' fields using DB-side
        # aggregation to avoid pulling a huge distinct list into memory.
        page = 1
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        try:
            filter_q = {"$or": [{"name": parent}, {"parent": parent}]} if parent else {}

            # Count distinct coaches
            count_pipeline = [
                {"$match": filter_q},
                {"$unwind": "$courses"},
                {"$match": {"courses.coach": {"$exists": True, "$ne": ""}}},
                {"$group": {"_id": "$courses.coach"}},
                {"$count": "count"}
            ]
            # Try Redis cache for coach-distinct count (keyed by parent)
            try:
                from handlers.base_handlers import _redis
                import json as _json
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
                    total_coaches = int(cnt_res[0].get('count')) if cnt_res else 0
                    # Cache for 30 seconds (coach lists are stable)
                    if _redis is not None:
                        try:
                            await _redis.setex(coach_cache_key, 30, str(total_coaches))
                        except Exception:
                            pass
            except Exception:
                cnt_res = await db.categories.aggregate(count_pipeline).to_list(length=1)
                total_coaches = int(cnt_res[0].get('count')) if cnt_res else 0

            # Fetch one page of distinct coach names (sorted A→Z)
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
            page_coaches = [d.get('_id') for d in docs if d and d.get('_id')]
        except Exception:
            page_coaches = []
            total_coaches = 0

        for coach in page_coaches:
            keyboard.append([InlineKeyboardButton(coach, callback_data=f"addcoach::{urllib.parse.quote_plus(coach)}")])

        nav = []
        total_pages = (total_coaches - 1) // page_size + 1 if total_coaches else 1
        last_page = max(1, total_pages)
        if total_pages > 1:
            if page == 1:
                if page < last_page:
                    nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))
                    nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
            elif page < last_page:
                nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page-1}"))
                nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::1"))
                nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
                nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))
            else:
                if page > 1:
                    nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page-1}"))
                    nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::1"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])
    # Ensure Back button is present immediately (so page 1 shows it too)
    try:
        if parent:
            parent_page = context.user_data.get('last_category_page', 1)
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"addparent_page::{parent_page}")])
    except Exception:
        pass

    await safe_edit_message(query, "Choose a coach for this course (or enter one manually):", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    return ADD_COACH


async def addcoach_page(update: Update, context: CallbackContext):
    """Paginated view for coach selection inside the add flow.

    Callback format: addcoach_page::{parent}::{page}
    parent may be empty string for top-level.
    """
    query = update.callback_query
    await safe_answer(query)
    # Owner guard for add flow pagination
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, 'id', None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
    data = query.data
    parts = data.split("::")
    if len(parts) < 3:
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, 'data', None))
        return
    parent_enc = parts[1]
    try:
        page = int(parts[2])
    except Exception:
        page = 1
    context.user_data["last_coach_page"] = page
    parent = urllib.parse.unquote_plus(parent_enc) if parent_enc else None

    try:
        db = await get_db()
        if parent:
            total_children = await get_total_count(db, 'categories', {"parent": parent}, ttl=15)
            page_size = COURSE_PAGE_SIZE
            start = (page - 1) * page_size
            children = await db.categories.find({"parent": parent}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
        else:
            total_children = 0
            children = []
    except Exception:
        children = []

    keyboard = []
    if children:
        # `children` is already a DB-side page (skip/limit). Sort locally to be deterministic
        sorted_children = sorted(children, key=lambda c: (c.get('name') or '').lower())
        # Use the DB count for total pages (not the length of this page)
        page_size = COURSE_PAGE_SIZE
        total_pages = (total_children - 1) // page_size + 1 if total_children else 1
        last_page = max(1, total_pages)

        # Build coach rows from the current DB page
        for child in sorted_children:
            keyboard.append([InlineKeyboardButton(child.get('name'), callback_data=f"addcoach::{urllib.parse.quote_plus(child.get('name'))}")])

        nav = []
        # Prev
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page-1}"))
        # Home (center) — show only when not on first page
        if page > 1:
            nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::1"))
        # Next
        if page < last_page:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))
        # End
        if total_pages > 1 and page < last_page:
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
        if nav:
            keyboard.append(nav)

    # Always include manual/no-coach options
    keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
    keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])

    # Append a Back button (bottom-most) when a parent context exists
    try:
        if parent:
            parent_page = context.user_data.get('last_category_page', 1)
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"addparent_page::{parent_page}")])
    except Exception:
        pass

    await safe_edit_message(query, "Choose a coach for this course (or enter one manually):", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    return ADD_COACH


async def addparent_page(update: Update, context: CallbackContext):
    """Paginated view for top-level parent selection inside the add flow.

    Callback format: addparent_page::{page}
    """
    query = update.callback_query
    await safe_answer(query)
    # Owner guard for add parent pagination
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, 'id', None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
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
        total = await get_total_count(db, 'categories', {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}, ttl=15)
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        parents = await db.categories.find({"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
    except Exception:
        total = 0
        parents = []

    keyboard = []
    keyboard.append([InlineKeyboardButton("(Add to top-level)", callback_data="addparent::")])
    for p in parents:
        display = f"{p.get('name')}"
        keyboard.append([InlineKeyboardButton(display, callback_data=f"addparent::{urllib.parse.quote_plus(p.get('name'))}::{page}")])

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if total_pages > 1:
        if page == 1:
            # First page: Next, End
            if page < last_page:
                nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addparent_page::{page+1}"))
                nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addparent_page::{last_page}"))
        elif page < last_page:
            # Middle pages: Prev, Home, End, Next
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addparent_page::{page-1}"))
            nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addparent_page::1"))
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addparent_page::{last_page}"))
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addparent_page::{page+1}"))
        else:
            # Last page: Previous and Home only
            if page > 1:
                nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addparent_page::{page-1}"))
                nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addparent_page::1"))
    if nav:
        keyboard.append(nav)

    await safe_edit_message(query, f"Choose a parent category for the new course (page {page}/{last_page}):", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    return ADD_PARENT


async def addcat_page(update_or_message, context: CallbackContext, *, page: int = 1):
    """Paginated categories selection for the add-course fallback.

    This function supports being called with a CallbackQuery (update.callback_query)
    where `update_or_message.data` contains `addcat_page::{page}` or with
    a Message context (initial call) where we pass page param explicitly.
    """
    # Normalize to callback query if present
    query = getattr(update_or_message, 'callback_query', None)
    is_query = query is not None
    if is_query:
        await safe_answer(query)
        # Owner guard for add cat pagination
        try:
            owner_env = os.getenv('BOT_OWNER_ID')
            owner_id = int(owner_env) if owner_env else None
        except Exception:
            owner_id = None
        user_id = getattr(query.from_user, 'id', None)
        if owner_id is not None and user_id != owner_id:
            await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
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
        # Use server-side pagination: get total count and fetch only the page slice
        total = await get_total_count(db, 'categories', {}, ttl=15)
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        cats = await db.categories.find({}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
    except Exception:
        total = 0
        cats = []

    page_cats = cats

    # Batch-check which categories on this page have children to avoid N queries
    cat_names = [c.get('name') for c in page_cats if c.get('name')]
    parent_has_children = {}
    if cat_names:
        try:
            docs = await db.categories.find({"parent": {"$in": cat_names}}, {"parent": 1}).to_list(length=len(cat_names))
            parents_with_children = {d.get('parent') for d in docs if d.get('parent')}
            parent_has_children = {name: (name in parents_with_children) for name in cat_names}
        except Exception:
            parent_has_children = {name: False for name in cat_names}

    keyboard = []
    for c in page_cats:
        try:
            has_children = parent_has_children.get(c.get('name'))
            courses = c.get('courses', []) if isinstance(c, dict) else []
            is_empty = (not has_children) and (not _has_real_courses(courses))
        except Exception:
            is_empty = True
        display = c.get('name')
        keyboard.append([InlineKeyboardButton(display, callback_data=f"addcat::{urllib.parse.quote_plus(c.get('name'))}::{page}")])

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    # Layout: Prev (left), Home (center), Next (right); End always at the end.
    if total_pages > 1:
        if page == 1:
            # First page: Next, End
            if page < last_page:
                nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcat_page::{page+1}"))
                nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcat_page::{last_page}"))
        elif page < last_page:
            # Middle pages: Prev, Home, End, Next
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcat_page::{page-1}"))
            nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcat_page::1"))
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcat_page::{last_page}"))
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcat_page::{page+1}"))
        else:
            # Last page: Previous and Home only
            if page > 1:
                nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcat_page::{page-1}"))
                nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcat_page::1"))
    if nav:
        keyboard.append(nav)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_query:
        await safe_edit_message(query, f"Pick a category for the course (page {page}/{last_page}):", reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    else:
        # called from a Message flow (initial display)
        await update_or_message.reply_text(f"Pick a category for the course (page {page}/{last_page}):", reply_markup=reply_markup)
    return


async def coach_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    # Owner guard for coach selection inside add flow
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, 'id', None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
    encoded = query.data.split("::", 1)[1]
    if encoded == "__manual__":
        # Ask for manual entry
        await query.message.reply_text("Send the coach name (text):")
        return ADD_COACH

    coach = urllib.parse.unquote_plus(encoded) if encoded else None
    context.user_data['course_coach'] = coach
    # Proceed to ask for course name
    await query.message.reply_text("Enter the name of the course:")
    return ADD_NAME


async def coach_manual_entry(update: Update, context: CallbackContext):
    coach = update.message.text.strip()
    if not coach:
        await update.message.reply_text("Coach name cannot be empty — try again.")
        return ADD_COACH
    context.user_data['course_coach'] = coach
    await update.message.reply_text("Enter the name of the course:")
    return ADD_NAME
        
async def category_selected(update: Update, context: CallbackContext):
    """Save the selected category and add the course to the database."""
    query = update.callback_query
    await safe_answer(query)
    # Owner guard for final category selection in add flow
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, 'id', None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
        return ConversationHandler.END

    # Support both legacy `addcat_<name>` and new `addcat::<name>::<page>` formats
    raw = query.data
    category_name = None
    origin_page = None
    if raw.startswith("addcat::"):
        parts = raw.split("::")
        # parts -> ['addcat', '<name>', '<page>' (optional)]
        if len(parts) >= 2:
            category_name = urllib.parse.unquote_plus(parts[1])
        if len(parts) >= 3:
            try:
                origin_page = int(parts[2])
            except Exception:
                origin_page = None
    else:
        # legacy underscore format
        encoded = query.data.split('_', 1)[1]
        category_name = urllib.parse.unquote_plus(encoded)

    # Get course data from user context
    course_name = context.user_data.get('course_name')
    course_link = context.user_data.get('course_link')

    if not course_name or not course_link:
        await safe_edit_message(query, "Error: Course data is missing. Please try again.", action_key=getattr(query, 'data', None))
        return ConversationHandler.END

    # Connect to the database
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return ConversationHandler.END

    try:
        # Save course inside the category document (push into categories.courses array)
        categories_coll = db['categories']
        coach = context.user_data.get('course_coach')
        course_doc = {
            "id": str(uuid.uuid4()),
            "name": course_name,
            "link": course_link
        }
        if coach:
            course_doc['coach'] = coach
        update_result = await categories_coll.update_one(
            {"name": category_name},
            {"$push": {"courses": course_doc}}
        )
        # Log the update result for debugging
        logger.info("[ADD-COURSE] update_result=%s", getattr(update_result, 'raw_result', update_result))

        if update_result.modified_count == 0:
            # Category not found
            logger.warning("[ADD-COURSE] Category not found: %s", category_name)
            await safe_edit_message(query, f"Error: Category '{category_name}' not found. Create it first.", action_key=getattr(query, 'data', None))
            return ConversationHandler.END

        # Send a confirmation message
        msg = (
            f"Course '{course_name}' added successfully to the '{category_name}' category. 🎉\n"
            f"Course Link: {course_link}"
        )
        # Add a button so the user can view the updated category immediately
        try:
            # If we know the originating categories page, open that page; otherwise default to 1
            view_page = origin_page or 1
            try:
                payload = {"type": "showcat", "path": category_name, "from_parent": "categories", "parent_page": view_page}
                key = _store_callback_payload(payload)
                cb = f"showcat_ref::{key}"
            except Exception:
                cb = _shorten_showcat_cb(category_name, view_page, from_parent="categories", parent_page=view_page)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("View Category", callback_data=cb)]])
            await safe_edit_message(query, msg, reply_markup=kb, action_key=getattr(query, 'data', None))
        except Exception:
            await safe_edit_message(query, msg, action_key=getattr(query, 'data', None))
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error saving course: {e}")
        await safe_edit_message(query, "An error occurred while saving the course. Please try again later.", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
        
async def add_course_category(update: Update, context: CallbackContext):
    """Save the selected category and add the course to the database."""
    query = update.callback_query
    await safe_answer(query)
    # Owner guard for final add flow callback
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, 'id', None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
        return ConversationHandler.END

    # Support both legacy `addcat_<name>` and new `addcat::<name>::<page>` formats
    raw = query.data
    category_name = None
    origin_page = None
    if raw.startswith("addcat::"):
        parts = raw.split("::")
        if len(parts) >= 2:
            category_name = urllib.parse.unquote_plus(parts[1])
        if len(parts) >= 3:
            try:
                origin_page = int(parts[2])
            except Exception:
                origin_page = None
    else:
        category_name = query.data.split('_', 1)[1]
    course_name = context.user_data.get("course_name")
    course_link = context.user_data.get("course_link")

    db = await get_db()  # Await the database connection
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return ConversationHandler.END

    try:
        # Push the course into the category document (embedded array)
        categories_coll = db['categories']
        coach = context.user_data.get('course_coach')
        course_doc = {
            "id": str(uuid.uuid4()),
            "name": course_name,
            "link": course_link
        }
        if coach:
            course_doc['coach'] = coach
        upd = await categories_coll.update_one(
            {"name": category_name},
            {"$push": {"courses": course_doc}}
        )
        logger.info("[ADD-COURSE-alt] update_result=%s", getattr(upd, 'raw_result', upd))
        if upd.modified_count == 0:
            await safe_edit_message(query, f"Error: Category '{category_name}' not found. Create it first.", action_key=getattr(query, 'data', None))
            return ConversationHandler.END

        try:
            view_page = origin_page or 1
            try:
                payload = {"type": "showcat", "path": category_name, "from_parent": "categories", "parent_page": view_page}
                key = _store_callback_payload(payload)
                cb = f"showcat_ref::{key}"
            except Exception:
                cb = _shorten_showcat_cb(category_name, view_page, from_parent="categories", parent_page=view_page)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("View Category", callback_data=cb)]])
            await safe_edit_message(query, f"Course '{course_name}' added successfully to the '{category_name}' category. 🎉", reply_markup=kb, action_key=getattr(query, 'data', None))
        except Exception:
            await safe_edit_message(query, f"Course '{course_name}' added successfully to the '{category_name}' category. 🎉", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
    except PyMongoError as e:
        logger.error(f"Error adding course: {e}")
        await safe_edit_message(query, "An error occurred while adding the course. Please try again later.", action_key=getattr(query, 'data', None))
        return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel the current operation."""
    try:
        if getattr(update, 'message', None) is not None:
            # If the user issued /cancel as a reply to a bot message (e.g., an inline confirm),
            # prefer editing that message to remove buttons and show the cancellation.
            reply_to = getattr(update.message, 'reply_to_message', None)
            if reply_to is not None and getattr(reply_to, 'message_id', None) is not None:
                try:
                    await reply_to.edit_text("Operation canceled.")
                except Exception:
                    await update.message.reply_text("Operation canceled.")
            else:
                await update.message.reply_text("Operation canceled.")
        elif getattr(update, 'callback_query', None) is not None:
            cq = update.callback_query
            try:
                await cq.answer()
            except Exception:
                pass
            try:
                await safe_edit_message(cq, "Operation canceled.", action_key=getattr(cq, 'data', None))
            except Exception:
                try:
                    await cq.message.reply_text("Operation canceled.")
                except Exception:
                    pass
    except Exception:
        pass
    # Clear any stored conversation data
    try:
        if context and getattr(context, 'user_data', None) is not None:
            context.user_data.clear()
    except Exception:
        pass
    return ConversationHandler.END

# Utility function to check valid URL format
def is_valid_url(url: str):
    """Check if the URL is valid."""
    url_pattern = r'^(https?:\/\/)([A-Za-z0-9\-._~%]+)(:[0-9]+)?(\/[^\s]*)?$'
    return re.match(url_pattern, url) is not None

# Global error handler
async def course_error_handler(update, context):
    """Global error handler — safe when update or update.message is None."""
    try:
        err = getattr(context, 'error', context)
        logger.error(f"Error: {err}")
        # Try to reply to user if possible
        if update is None:
            return
        # Message-based update
        if getattr(update, 'message', None) is not None:
            await update.message.reply_text("An unexpected error occurred. Please try again later.")
        # CallbackQuery-based update
        elif getattr(update, 'callback_query', None) is not None:
            cq = update.callback_query
            try:
                await cq.answer()
            except Exception:
                pass
            try:
                await safe_edit_message(cq, "An unexpected error occurred. Please try again later.", action_key=getattr(cq, 'data', None))
            except Exception:
                pass
    except Exception:
        logger.exception("Error in error_handler")
