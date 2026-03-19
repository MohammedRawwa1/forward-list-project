from telegram.ext import ConversationHandler, MessageHandler, CommandHandler, CallbackQueryHandler, filters, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from conversation_states import ADD_NAME, ADD_LINK, ADD_CATEGORY, ADD_PARENT, ADD_COACH
from handlers.db_connection import get_db
from pymongo.errors import PyMongoError
import logging
import re
import urllib.parse
from handlers.base_handlers import safe_edit_message, safe_answer, _shorten_showcat_cb, _store_callback_payload

# Page size used only by course-related handlers (coaches/categories/courses in add flow)
COURSE_PAGE_SIZE = 50

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states are defined in conversation_states.py

async def setup_course_handlers(application):
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_course_start)],
        states={
            # First: pick a parent/top-level category
            ADD_PARENT: [CallbackQueryHandler(parent_selected, pattern=r"^addparent::")],

            # Then: pick a coach (buttons) or enter one manually (text)
            ADD_COACH: [
                CallbackQueryHandler(coach_selected, pattern=r"^addcoach::"),
                CallbackQueryHandler(addcoach_page, pattern=r"^addcoach_page::"),
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
    """Handler for the /start command."""
    user = update.message.from_user
    await update.message.reply_text(f"Hello {user.first_name}! Welcome to the bot. Type /help for available commands.")
    
# course_handlers.py
async def add_course_start(update: Update, context: CallbackContext):
    """Start add flow: prompt the user to pick a parent/top-level category.

    If no top-level parents exist, fall back to asking for the course name.
    """
    try:
        db = await get_db()
        parents = await db.categories.find({"parent": {"$exists": False}}).to_list(length=None)
        parents = sorted(parents, key=lambda c: (c.get('name') or '').lower())
    except Exception:
        parents = []

    if not parents:
        # No parents to choose from — continue with legacy flow (ask name)
        await update.message.reply_text("Enter the name of the course:")
        return ADD_NAME

    keyboard = []
    # Allow top-level (no parent) explicitly
    keyboard.append([InlineKeyboardButton("(Add to top-level)", callback_data="addparent::")])
    for p in parents:
        keyboard.append([InlineKeyboardButton(p.get('name'), callback_data=f"addparent::{urllib.parse.quote_plus(p.get('name'))}")])

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
                    update_result = await categories_coll.update_one(
                        {"name": coach, "parent": parent},
                        {"$push": {"courses": {"name": context.user_data.get('course_name'), "link": link}}}
                    )
                    logger.info("[ADD-COURSE] saved to child coach=%s under parent=%s result=%s", coach, parent, getattr(update_result, 'raw_result', update_result))
                else:
                    update_result = await categories_coll.update_one(
                        {"name": parent},
                        {"$push": {"courses": {"name": context.user_data.get('course_name'), "link": link, "coach": coach}}}
                    )

                    # Fetch and log updated category for debugging: ensure new course appears
                    try:
                        updated_cat = await categories_coll.find_one({"name": parent})
                        logger.info("[ADD-COURSE] parent=%s now has %d courses: %s", parent, len(updated_cat.get('courses', [])), [c.get('name') for c in updated_cat.get('courses', [])])
                    except Exception:
                        logger.debug("[ADD-COURSE] unable to fetch updated category %s for logging", parent)
            else:
                update_result = await categories_coll.update_one(
                    {"name": parent},
                    {"$push": {"courses": {"name": context.user_data.get('course_name'), "link": link}}}
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

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "View Category",
                    callback_data=_shorten_showcat_cb(view_cat, current_page, from_parent="categories", parent_page=current_page)
                )
            ]])

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
        cats = await db.categories.find().to_list(length=None)
        cats = sorted(cats, key=lambda c: (c.get('name') or '').lower())

        if not cats:
            await update.message.reply_text("No categories available. Create one first with /create_category")
            return ConversationHandler.END

        await addcat_page(update.message, context, page=1)
        return ADD_CATEGORY
    except Exception as e:
        logger.error("Error saving course link: %s", e)
        await update.message.reply_text("An error occurred while saving the course. Please try again later.")
        return ConversationHandler.END


async def parent_selected(update: Update, context: CallbackContext):
    """Callback when a parent is chosen. Presents coach choices next."""
    query = update.callback_query
    await safe_answer(query)
    encoded = query.data.split("::", 1)[1]
    parent = urllib.parse.unquote_plus(encoded) if encoded else None
    # store chosen parent (None means add to top-level)
    context.user_data['course_parent'] = parent

    # Prefer showing child categories as coach options when coaches are
    # modeled as category documents. This matches the user's workflow where
    # `/create_category` creates coaches.
    try:
        db = await get_db()
        # find child categories of the selected parent
        if parent:
            child_cats = await db.categories.find({"parent": parent}).to_list(length=None)
        else:
            child_cats = []
    except Exception:
        child_cats = []

    keyboard = []
    if child_cats:
        # Paginate child categories (coaches) when many
        sorted_children = sorted(child_cats, key=lambda c: (c.get('name') or '').lower())
        page = 1
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        end = start + page_size
        page_children = sorted_children[start:end]
        for child in page_children:
            keyboard.append([InlineKeyboardButton(child.get('name'), callback_data=f"addcoach::{urllib.parse.quote_plus(child.get('name'))}")])
        # Navigation row
        nav = []
        total_pages = (len(sorted_children) - 1) // page_size + 1 if sorted_children else 1
        last_page = max(1, total_pages)
        if total_pages > 1 and page < last_page:
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))
        if nav:
            keyboard.append(nav)
        # Also allow manual entry or no coach
        keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])
    else:
        # Fallback: derive coaches from existing course 'coach' fields
        try:
            cats = await db.categories.find({"$or": [{"name": parent}, {"parent": parent}] }).to_list(length=None) if parent else await db.categories.find().to_list(length=None)
            coaches_set = set()
            for c in cats:
                for crs in c.get('courses', []):
                    if crs.get('coach'):
                        coaches_set.add(crs.get('coach'))
        except Exception:
            coaches_set = set()

        coaches = sorted(list(coaches_set))
        # Paginate derived coaches when many
        page = 1
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        end = start + page_size
        page_coaches = coaches[start:end]
        for coach in page_coaches:
            keyboard.append([InlineKeyboardButton(coach, callback_data=f"addcoach::{urllib.parse.quote_plus(coach)}")])
        nav = []
        total_pages = (len(coaches) - 1) // page_size + 1 if coaches else 1
        last_page = max(1, total_pages)
        if total_pages > 1 and page < last_page:
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])

    await safe_edit_message(query, "Choose a coach for this course (or enter one manually):", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    return ADD_COACH


async def addcoach_page(update: Update, context: CallbackContext):
    """Paginated view for coach selection inside the add flow.

    Callback format: addcoach_page::{parent}::{page}
    parent may be empty string for top-level.
    """
    query = update.callback_query
    await safe_answer(query)
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
            children = await db.categories.find({"parent": parent}).to_list(length=None)
        else:
            children = []
    except Exception:
        children = []

    keyboard = []
    if children:
        sorted_children = sorted(children, key=lambda c: (c.get('name') or '').lower())
        page_size = COURSE_PAGE_SIZE
        start = (page - 1) * page_size
        end = start + page_size
        page_children = sorted_children[start:end]
        for child in page_children:
            keyboard.append([InlineKeyboardButton(child.get('name'), callback_data=f"addcoach::{urllib.parse.quote_plus(child.get('name'))}")])

        nav = []
        total_pages = (len(sorted_children) - 1) // page_size + 1 if sorted_children else 1
        last_page = max(1, total_pages)
        # Layout: Prev (left), Home (center), Next (right); End always at the end.
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page-1}"))

        # Home for add flow: go to first page for this parent
        nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::1"))

        if page < last_page:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{page+1}"))

        if total_pages > 1:
            nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcoach_page::{urllib.parse.quote_plus(parent or '')}::{last_page}"))
        if nav:
            keyboard.append(nav)

    # Always include manual/no-coach options
    keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
    keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])

    await safe_edit_message(query, "Choose a coach for this course (or enter one manually):", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    return


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
        data = query.data
        parts = data.split("::")
        try:
            page = int(parts[1])
        except Exception:
            page = 1
    context.user_data["last_category_page"] = page
    
    try:
        db = await get_db()
        cats = await db.categories.find().to_list(length=None)
    except Exception:
        cats = []

    cats = sorted(cats, key=lambda c: (c.get('name') or '').lower())
    page_size = COURSE_PAGE_SIZE
    start = (page - 1) * page_size
    end = start + page_size
    page_cats = cats[start:end]

    keyboard = [[InlineKeyboardButton(c.get('name'), callback_data=f"addcat::{urllib.parse.quote_plus(c.get('name'))}::{page}")] for c in page_cats]

    nav = []
    total_pages = (len(cats) - 1) // page_size + 1 if cats else 1
    last_page = max(1, total_pages)
    # Layout: Prev (left), Home (center), Next (right); End always at the end.
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"addcat_page::{page-1}"))

    # Home for /add (go to first page)
    nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"addcat_page::1"))

    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"addcat_page::{page+1}"))

    # End: always provide when multiple pages
    if total_pages > 1:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"addcat_page::{last_page}"))
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
        course_doc = {"name": course_name, "link": course_link}
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
        course_doc = {"name": course_name, "link": course_link}
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
async def error_handler(update, context):
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

# Handle URL parsing errors for course link input
async def handle_link_parsing_error(update: Update, context: CallbackContext):
    link = update.message.text.strip()
    logger.warning(f"Failed to parse link: {link}")
    
    # Validate the provided link format before proceeding
    url_pattern = r'^(http|https|tg|t.me):\/\/([a-zA-Z0-9\-\.]+(?:\:[0-9]+)?(?:\/[^\s]*)?(\?[^\s]*)?(#[^\s]*)?)$'
    
    if not re.match(url_pattern, link):
        await update.message.reply_text(
            f"The provided input '{link}' is not a valid URL. Please ensure you provide a correct link."
        )
    else:
        await update.message.reply_text(
            "Due to network issues, parsing of the link was unsuccessful. "
            "Please check the link's validity and try again. If this link is not essential for answering your question, feel free to proceed normally."
        )
    return ConversationHandler.END
