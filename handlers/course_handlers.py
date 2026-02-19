from telegram.ext import ConversationHandler, MessageHandler, CommandHandler, CallbackQueryHandler, filters, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from conversation_states import ADD_NAME, ADD_LINK, ADD_CATEGORY
from handlers.db_connection import get_db
from pymongo.errors import PyMongoError
import logging
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
NAME, LINK, CATEGORY = range(3)

async def setup_course_handlers(application):
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_course_start)],
        states={
            ADD_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_name)],
            ADD_LINK:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_link)],
            ADD_CATEGORY:[CallbackQueryHandler(category_selected, pattern=r"^category_")]
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
    await update.message.reply_text("Enter the name of the course:")
    return ADD_NAME

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

    # Check if there are categories available
    try:
        db = await get_db()
        cats = await db.categories.find().to_list(length=None)
    except Exception as e:
        logger.error("DB error in add_course_link: %s", e)
        await update.message.reply_text("❗️ Could not connect to the database. Try again later.")
        return ConversationHandler.END

    if not cats:
        await update.message.reply_text("No categories available. Create one first with /create_category")
        return ConversationHandler.END

    # Send category selection keyboard
    keyboard = [[InlineKeyboardButton(c['name'], callback_data=f"category_{c['name']}")] for c in cats]
    await update.message.reply_text(
        "Pick a category for the course:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADD_CATEGORY
        
async def category_selected(update: Update, context: CallbackContext):
    """Save the selected category and add the course to the database."""
    query = update.callback_query
    await query.answer()

    # Extract category name from callback data (allow underscores in names)
    category_name = query.data.split('_', 1)[1]

    # Get course data from user context
    course_name = context.user_data.get('course_name')
    course_link = context.user_data.get('course_link')

    if not course_name or not course_link:
        await query.edit_message_text("Error: Course data is missing. Please try again.")
        return ConversationHandler.END

    # Connect to the database
    db = await get_db()
    if not db:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return ConversationHandler.END

    try:
        # Save course to the database
        collection = db['courses']
        await collection.insert_one({
            "name": course_name,
            "link": course_link,
            "category": category_name
        })

        # Send a confirmation message
        await query.edit_message_text(
            f"Course '{course_name}' added successfully to the '{category_name}' category. 🎉\n"
            f"Course Link: {course_link}"
        )
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error saving course: {e}")
        await query.edit_message_text("An error occurred while saving the course. Please try again later.")
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
    if not db:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return ConversationHandler.END

    try:
        # Insert the course into the database
        collection = db['courses']
        await collection.insert_one({
            "name": course_name,
            "link": course_link,
            "category": category_name,
        })
        await query.edit_message_text(f"Course '{course_name}' added successfully to the '{category_name}' category. 🎉")
        return ConversationHandler.END
    except PyMongoError as e:
        logger.error(f"Error adding course: {e}")
        await query.edit_message_text("An error occurred while adding the course. Please try again later.")
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
async def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Error: {context.error}")
    await update.message.reply_text("An unexpected error occurred. Please try again later.")

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
