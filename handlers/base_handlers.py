from telegram.ext import CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import logging
from conversation_states import CREATE_CAT_NAME
from handlers.db_connection import get_db  # Importing get_db from db_connection.py
from database.mongo_handler import MongoDB  # Import MongoDB
import re  # For URL validation
from pymongo.errors import DuplicateKeyError
import urllib.parse

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
MAX_CATEGORY_NAME_LENGTH = 30  # Maximum allowed length for category names
PAGE_SIZE = 10  # Default number of items per page for pagination

# Input Validation for Category Name
def validate_category_name(category_name: str):
    """Validates the category name."""
    if not category_name or category_name.isspace():
        return "The category name cannot be empty. Please try again! 😬"
    
    if len(category_name) < 3 or len(category_name) > MAX_CATEGORY_NAME_LENGTH:
        return f"Category name must be between 3 and {MAX_CATEGORY_NAME_LENGTH} characters."
    
    if not is_valid_category_name(category_name):
        return "Category name can only contain letters, numbers, spaces, and hyphens. 😓"
    
    return None
    
async def help(update: Update, context: CallbackContext):
    """Customized Help message."""
    help_message = (
        "✨ **Welcome to the Course Manager Bot!** Here's how you can use me:\n\n"
        "/start - Start the bot and receive a welcome message\n"
        "/add - Start the process of adding a new course\n"
        "/courses - View your saved courses\n"
        "/delete_course - Delete a specific course\n"
        "/delete_category - Delete a category and all its associated courses\n"
        "/delete_all_data - Deletes both courses and categories (don't use this lightly!)\n\n"
        "📚 **Category Management**:\n"
        "/categories - List all available categories\n"
        "/create_category - Create a new empty category\n\n"
        "🎨 **Course Thumbnail Management**:\n"
        "/addthumb - Add a custom thumbnail for a course\n"
        "/delthumb - Delete a custom thumbnail for a course\n\n"
        "⚙️ **Other Commands**:\n"
        "/help - Displays this help message\n"
        "/cancel - Cancel the current operation\n\n"
        "⚠️ **Important Note**: Be careful with the commands that delete categories or courses! Once deleted, they can't be recovered."
    )
    await update.message.reply_text(help_message)

async def list_categories(update: Update, context: CallbackContext):
    """Show every category as an inline button that opens its courses."""
    try:
        db = await get_db()
        categories = await db.categories.find().to_list(length=None)
        if not categories:
            await update.message.reply_text("No categories available. Use /create_category to create one.")
            return
        keyboard = [[InlineKeyboardButton(cat["name"], callback_data=f"showcat_{urllib.parse.quote_plus(cat['name'])}")] for cat in categories]
        await update.message.reply_text("Tap a category to see its courses:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error listing categories: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")

async def showcat_handler(update: Update, context: CallbackContext):
    """Show courses in the chosen category as URL buttons."""
    query = update.callback_query
    await query.answer()
    encoded = query.data.split("_", 1)[1]
    cat_name = urllib.parse.unquote_plus(encoded)
    db = await get_db()
    # Read category document and its embedded courses array
    category_doc = await db.categories.find_one({"name": cat_name})
    if not category_doc or not category_doc.get('courses'):
        await query.edit_message_text(f'Category “{cat_name}” is empty.\nUse /add to populate it.')
        return

    courses = category_doc.get('courses', [])
    # Build keyboard with course URL buttons and a Details callback beside each
    keyboard = [
        [
            InlineKeyboardButton(crs["name"], url=crs["link"]),
            InlineKeyboardButton("Details", callback_data=f"course::%s::%s" % (urllib.parse.quote_plus(cat_name), urllib.parse.quote_plus(crs["name"])))
        ]
        for crs in courses
    ]
    keyboard.append([InlineKeyboardButton("🗑 Delete a course", callback_data=f"del_menu_{urllib.parse.quote_plus(cat_name)}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
    await query.edit_message_text('📚 Tap any course to open its link:', reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_back_to_cats(update: Update, context: CallbackContext):
    """Handle the 🔙 Back callback and show the categories list."""
    query = update.callback_query
    await query.answer()
    try:
        db = await get_db()
        categories = await db.categories.find().to_list(length=None)
        if not categories:
            await query.edit_message_text("No categories available. Use /create_category to create one.")
            return
        keyboard = [[InlineKeyboardButton(cat["name"], callback_data=f"showcat_{urllib.parse.quote_plus(cat['name'])}")] for cat in categories]
        await query.edit_message_text("Tap a category to see its courses:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error returning to categories: {e}")
        await query.edit_message_text("An unexpected error occurred. Please try again later.")

async def list_courses(update: Update, context: CallbackContext):
    """List all available courses with pagination."""
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        # Build a flattened list of courses from all categories
        page = 1
        page_size = PAGE_SIZE
        cats = await db.categories.find().to_list(length=None)
        all_courses = []
        for cat in cats:
            for crs in cat.get('courses', []):
                all_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})

        if all_courses:
            start = (page - 1) * page_size
            display = all_courses[start:start + page_size]

            keyboard = [
                [
                    InlineKeyboardButton(c['name'], url=c['link']),
                    InlineKeyboardButton("Details", callback_data=f"course::%s::%s" % (urllib.parse.quote_plus(c['category']), urllib.parse.quote_plus(c['name'])))
                ]
                for c in display
            ]

            pagination_buttons = []
            if start > 0:
                pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"courses::{page-1}"))
            if len(all_courses) > start + page_size:
                pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"courses::{page+1}"))
            if pagination_buttons:
                keyboard.append(pagination_buttons)

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Here are the available courses:", reply_markup=reply_markup)
        else:
            await update.message.reply_text("No courses available.")
    except Exception as e:
        logger.error(f"Error listing courses: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")

async def list_courses_by_category(update: Update, context: CallbackContext, category_name: str, page: int = 1):
    """List courses in a specific category with pagination."""
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        # Paginate over the embedded courses array inside the category document
        page_size = PAGE_SIZE
        category_doc = await db.categories.find_one({"name": category_name})
        if not category_doc or not category_doc.get('courses'):
            await update.message.reply_text(f"No courses found in category '{category_name}'.")
            return

        courses = category_doc.get('courses', [])
        start = (page - 1) * page_size
        display = courses[start:start + page_size]

        keyboard = [
            [
                InlineKeyboardButton(course['name'], url=course['link']),
                InlineKeyboardButton("Details", callback_data=f"course::%s::%s" % (urllib.parse.quote_plus(category_name), urllib.parse.quote_plus(course['name'])))
            ]
            for course in display
        ]

        pagination_buttons = []
        if start > 0:
            pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"courses::{urllib.parse.quote_plus(category_name)}::{page-1}"))
        if len(courses) > start + page_size:
            pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"courses::{urllib.parse.quote_plus(category_name)}::{page+1}"))
        if pagination_buttons:
            keyboard.append(pagination_buttons)

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Courses in category '{category_name}':", reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Error listing courses for category '{category_name}': {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
        
# legacy underscore-format pagination handler removed; modern `courses::` callbacks are used

async def handle_categories_pagination(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    # Extract the action and page number from the callback data
    data = query.data.split('_')
    action = data[1]  # "prev" or "next"
    page = int(data[2])  # Page number

    db = await get_db()
    if db is None:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return

    try:
        collection = db['categories']
        page_size = PAGE_SIZE  # Number of categories per page

        # Fetch categories for the current page
        categories = await collection.find().skip((page - 1) * page_size).limit(page_size).to_list(length=None)

        if categories:
            # Create buttons for each category
            keyboard = [
                [InlineKeyboardButton(category['name'], callback_data=f"category_{urllib.parse.quote_plus(category['name'])}")]
                for category in categories
            ]

            # Add pagination buttons
            pagination_buttons = []
            if page > 1:
                pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"categories_prev_{page-1}"))
            if len(categories) == page_size:
                pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"categories_next_{page+1}"))
            
            if pagination_buttons:
                keyboard.append(pagination_buttons)

            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text("Here are the available categories:", reply_markup=reply_markup)
        else:
            await query.edit_message_text("No more categories available.")
    except Exception as e:
        logger.error(f"Error handling pagination: {e}")
        await query.edit_message_text("An error occurred while fetching categories. Please try again later.")

logger.info(f"[STATE] returning {CREATE_CAT_NAME=} id={id(CREATE_CAT_NAME)}")
async def create_category(update: Update, context: CallbackContext):
    await update.message.reply_text("Enter the new category name:")
    return CREATE_CAT_NAME
    
async def handle_category_name(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    category_name = update.message.text.strip()
    logger.info(f"[CAT-INSERT-START] name={category_name!r} uid={user_id}")

    # --- single validator (delete any other validate_category_name) ---
    if not category_name or len(category_name) < 3 or len(category_name) > 30:
        await update.message.reply_text("Name must be 3-30 chars.")
        return CREATE_CAT_NAME
    if not re.match(r"^[a-zA-Z0-9\s\-]+$", category_name):
        await update.message.reply_text("Only letters, numbers, space, hyphen.")
        return CREATE_CAT_NAME

    try:
        db = await MongoDB.get_db()          # yes, import MongoDB here
        logger.info(f"[CAT-DB] using database: {db.name}")
        coll = db['categories']
        logger.info(f"[CAT-INSERT] about to insert {category_name!r}")
        result = await coll.insert_one({"name": category_name, "created_by": user_id})
        logger.info(f"[CAT-INSERT-DONE] _id={result.inserted_id}")
        await update.message.reply_text(f"Category ‘{category_name}’ saved ✔")
        return ConversationHandler.END
    except DuplicateKeyError:
        logger.warning(f"[CAT-INSERT-DUP] category already exists: {category_name!r}")
        await update.message.reply_text(f"A category named '{category_name}' already exists. Please choose a different name.")
        return CREATE_CAT_NAME
    except Exception as exc:
        logger.error(f"[CAT-INSERT-FAIL] {exc}", exc_info=True)
        await update.message.reply_text("Save failed – check console.")
        return ConversationHandler.END

async def handle_category_selection(update: Update, context: CallbackContext):
    """List courses in the chosen category – each course button is a direct URL."""
    query = update.callback_query
    await query.answer()
    encoded = query.data.replace("category_", "", 1)
    cat_name = urllib.parse.unquote_plus(encoded)
    db = await get_db()
    category_doc = await db.categories.find_one({"name": cat_name})
    if not category_doc or not category_doc.get('courses'):
        await query.edit_message_text(f'Category “{cat_name}” is empty.\nUse /add to populate it.')
        return

    # every button is a url button → opens the link immediately
    courses = category_doc.get('courses', [])
    keyboard = [
        [InlineKeyboardButton(crs["name"], url=crs["link"])]
        for crs in courses
    ]
    keyboard.append([InlineKeyboardButton("🗑 Delete a course", callback_data=f"del_menu_{urllib.parse.quote_plus(cat_name)}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
    await query.edit_message_text(
        f'📚 Tap any course to open its link:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
async def handle_course_selection(update: Update, context: CallbackContext):
    """Handle the selection of a course from the buttons."""
    query = update.callback_query
    await query.answer()
    # Expect callback format: course::{category}::{course}
    data = query.data.replace("course::", "", 1)
    parts = data.split("::", 1)
    if len(parts) == 2:
        encoded_cat, encoded_course = parts
        cat_name = urllib.parse.unquote_plus(encoded_cat)
        course_name = urllib.parse.unquote_plus(encoded_course)
    else:
        cat_name = None
        course_name = urllib.parse.unquote_plus(data)

    db = await get_db()
    if db is None:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return

    try:
        course = None
        if cat_name:
            category_doc = await db.categories.find_one({"name": cat_name})
            if category_doc:
                for crs in category_doc.get('courses', []):
                    if crs.get('name') == course_name:
                        course = {"name": crs.get('name'), "link": crs.get('link'), "category": cat_name}
                        break
        else:
            # search across categories
            cats = await db.categories.find().to_list(length=None)
            for cat in cats:
                for crs in cat.get('courses', []):
                    if crs.get('name') == course_name:
                        course = {"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')}
                        break
                if course:
                    break

        if course:
            keyboard = [
                [InlineKeyboardButton("Delete Course", callback_data=f"delete_course::{urllib.parse.quote_plus(course['category'])}::{urllib.parse.quote_plus(course['name'])}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                text=f"📚 **Course Details**\n\n"
                     f"Name: {course['name']}\n"
                     f"Link: {course['link']}\n"
                     f"Category: {course['category']}",
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_text("Course not found. Please try again.")
    except Exception as e:
        logger.error(f"Error fetching course '{course_name}': {e}")
        await query.edit_message_text("An error occurred while fetching the course. Please try again later.")
        
# Main entry point for your bot (add handlers as needed)
async def get_courses_by_category(user_id, category, page: int = 1, page_size: int = 20):
    """Fetch courses by category with pagination."""
    db = await get_db()
    if db is None:
        return []

    try:
        # Read courses from the category document's embedded array and paginate
        category_doc = await db.categories.find_one({"name": category})
        if not category_doc or not category_doc.get('courses'):
            return []
        courses = category_doc.get('courses', [])
        start = (page - 1) * page_size
        return courses[start:start + page_size]
    except Exception as e:
        logger.error(f"Error while fetching courses for category '{category}': {str(e)}")
        return []

async def courses_callback(update: Update, context: CallbackContext):
    """Handle the courses callback and display courses based on pagination."""
    query = update.callback_query
    await query.answer()
    data = query.data
    db = await get_db()
    if db is None:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return

    try:
        # New format supports: courses::{category}::{page} or courses::{page} for global
        if data.startswith("courses::"):
            payload = data.replace("courses::", "", 1)
            parts = payload.split("::")
            if len(parts) == 1:
                # global page
                page = int(parts[0])
                # flatten all courses
                cats = await db.categories.find().to_list(length=None)
                all_courses = []
                for cat in cats:
                    for crs in cat.get('courses', []):
                        all_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})

                page_size = PAGE_SIZE
                start = (page - 1) * page_size
                display = all_courses[start:start + page_size]
                if not display:
                    await query.edit_message_text(f"No courses found on page {page}.")
                    return

                course_list_text = "\n".join([f"📚 {c['name']}\n{c['link']}" for c in display])
                keyboard = [
                    [
                        InlineKeyboardButton(c['name'], url=c['link']),
                        InlineKeyboardButton("Details", callback_data=f"course::%s::%s" % (urllib.parse.quote_plus(c['category']), urllib.parse.quote_plus(c['name'])))
                    ]
                    for c in display
                ]
                pagination = []
                if start > 0:
                    pagination.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"courses::{page-1}"))
                if len(all_courses) > start + page_size:
                    pagination.append(InlineKeyboardButton("➡️ Next", callback_data=f"courses::{page+1}"))
                if pagination:
                    keyboard.append(pagination)

                await query.edit_message_text(text=f"All courses (page {page}):\n\n{course_list_text}", reply_markup=InlineKeyboardMarkup(keyboard))
                return

            # category + page
            category = urllib.parse.unquote_plus(parts[0])
            try:
                page = int(parts[1])
            except Exception:
                await query.edit_message_text("Invalid page number.")
                return

            category_doc = await db.categories.find_one({"name": category})
            if not category_doc or not category_doc.get('courses'):
                await query.edit_message_text(f"No courses found in category '{category}' on page {page}.")
                return

            page_size = PAGE_SIZE
            start = (page - 1) * page_size
            courses = category_doc.get('courses', [])
            display = courses[start:start + page_size]
            if not display:
                await query.edit_message_text(f"No courses found in category '{category}' on page {page}.")
                return

            course_list_text = "\n".join([f"📚 {c['name']}\n{c['link']}" for c in display])
            keyboard = [
                [InlineKeyboardButton(c['name'], url=c['link'])]
                for c in display
            ]
            pagination = []
            if start > 0:
                pagination.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"courses::{urllib.parse.quote_plus(category)}::{page-1}"))
            if len(courses) > start + page_size:
                pagination.append(InlineKeyboardButton("➡️ Next", callback_data=f"courses::{urllib.parse.quote_plus(category)}::{page+1}"))
            if pagination:
                keyboard.append(pagination)

            await query.edit_message_text(text=f"Courses in category '{category}' (page {page}):\n\n{course_list_text}", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        # legacy underscore format removed. Only `courses::` callbacks are supported.
        await query.edit_message_text("Invalid pagination callback.")
        return
    except Exception as e:
        logger.error(f"Error handling courses callback: {e}")
        await query.edit_message_text("An error occurred while fetching courses. Please try again later.")
