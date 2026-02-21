from telegram.ext import ConversationHandler, MessageHandler, CommandHandler, CallbackQueryHandler, filters, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from conversation_states import ADD_NAME, ADD_LINK, ADD_CATEGORY, ADD_PARENT, ADD_COACH
from handlers.db_connection import get_db
from pymongo.errors import PyMongoError
import logging
import re
import urllib.parse
from handlers.base_handlers import safe_edit_message

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
                MessageHandler(filters.TEXT & ~filters.COMMAND, coach_manual_entry),
                MessageHandler(filters.Regex(r'^/cancel$'), cancel),
            ],

            # Then: course name (text) — include explicit cancel matcher so /cancel always works
            ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_name),
                MessageHandler(filters.Regex(r'^/cancel$'), cancel),
            ],

            # Then: course link (text)
            ADD_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_link),
                MessageHandler(filters.Regex(r'^/cancel$'), cancel),
            ],

            # Legacy: allow selecting an arbitrary category at the end if needed
            ADD_CATEGORY:[CallbackQueryHandler(category_selected, pattern=r"^addcat_")]
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

        # If a parent was selected, save into that parent category
        if parent is not None:
            update_result = await categories_coll.update_one(
                {"name": parent},
                {"$push": {"courses": {"name": context.user_data.get('course_name'), "link": link, "coach": coach}}}
            )
            logger.info("[ADD-COURSE] saved to parent=%s result=%s", parent, getattr(update_result, 'raw_result', update_result))
            if update_result.modified_count == 0:
                await update.message.reply_text(f"Error: Parent category '{parent}' not found. Create it first.")
                return ConversationHandler.END

            await update.message.reply_text(f"Course '{context.user_data.get('course_name')}' added successfully to '{parent}'. 🎉\nLink: {link}")
            return ConversationHandler.END

        # Fallback: ask the user to pick a category (legacy behavior)
        cats = await db.categories.find().to_list(length=None)
        cats = sorted(cats, key=lambda c: (c.get('name') or '').lower())
        if not cats:
            await update.message.reply_text("No categories available. Create one first with /create_category")
            return ConversationHandler.END

        keyboard = [[InlineKeyboardButton(c['name'], callback_data=f"addcat_{urllib.parse.quote_plus(c['name'])}")] for c in cats]
        await update.message.reply_text("Pick a category for the course:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ADD_CATEGORY

    except Exception as e:
        logger.error("Error saving course link: %s", e)
        await update.message.reply_text("An error occurred while saving the course. Please try again later.")
        return ConversationHandler.END


async def parent_selected(update: Update, context: CallbackContext):
    """Callback when a parent is chosen. Presents coach choices next."""
    query = update.callback_query
    await query.answer()
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
        for child in sorted(child_cats, key=lambda c: (c.get('name') or '').lower()):
            keyboard.append([InlineKeyboardButton(child.get('name'), callback_data=f"addcoach::{urllib.parse.quote_plus(child.get('name'))}")])
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
        for coach in coaches:
            keyboard.append([InlineKeyboardButton(coach, callback_data=f"addcoach::{urllib.parse.quote_plus(coach)}")])
        keyboard.append([InlineKeyboardButton("(Enter coach name)", callback_data="addcoach::__manual__")])
        keyboard.append([InlineKeyboardButton("(No coach)", callback_data="addcoach::")])

    await safe_edit_message(query, "Choose a coach for this course (or enter one manually):", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    return ADD_COACH


async def coach_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
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
    await query.answer()

    # Extract category name from callback data using the add-flow prefix
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
        await safe_edit_message(query, msg, action_key=getattr(query, 'data', None))
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error saving course: {e}")
        await safe_edit_message(query, "An error occurred while saving the course. Please try again later.", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
        
async def add_course_category(update: Update, context: CallbackContext):
    """Save the selected category and add the course to the database."""
    query = update.callback_query
    await query.answer()

    # Support callback data formats and allow underscores in names
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

        await safe_edit_message(query, f"Course '{course_name}' added successfully to the '{category_name}' category. 🎉", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
    except PyMongoError as e:
        logger.error(f"Error adding course: {e}")
        await safe_edit_message(query, "An error occurred while adding the course. Please try again later.", action_key=getattr(query, 'data', None))
        return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel the current operation."""
    await update.message.reply_text("Operation canceled.")
    context.user_data.clear()  # Clear user data
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
