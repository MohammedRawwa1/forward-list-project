from telegram.ext import CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import logging
from conversation_states import CREATE_CAT_NAME
from handlers.db_connection import get_db  # Importing get_db from db_connection.py
from database.mongo_handler import MongoDB  # Import MongoDB
import re  # For URL validation

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
MAX_CATEGORY_NAME_LENGTH = 30  # Maximum allowed length for category names

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
        keyboard = [[InlineKeyboardButton(cat["name"], callback_data=f"showcat_{cat['name']}")] for cat in categories]
        await update.message.reply_text("Tap a category to see its courses:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error listing categories: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")

async def showcat_handler(update: Update, context: CallbackContext):
    """Show courses in the chosen category as URL buttons."""
    query = update.callback_query
    await query.answer()
    cat_name = query.data.split("_", 1)[1]
    db = await get_db()
    courses = await db.courses.find({"category": cat_name}).to_list(length=None)
    if not courses:
        await query.edit_message_text(f'Category “{cat_name}” is empty.\nUse /add to populate it.')
        return
    keyboard = [[InlineKeyboardButton(crs["name"], url=crs["link"])] for crs in courses]
    keyboard.append([InlineKeyboardButton("🗑 Delete a course", callback_data=f"del_menu_{cat_name}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
    await query.edit_message_text('📚 Tap any course to open its link:', reply_markup=InlineKeyboardMarkup(keyboard))

async def list_courses(update: Update, context: CallbackContext):
    """List all available courses with pagination."""
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        collection = db['courses']
        page = 1  # Default page number
        page_size = 5  # Number of courses per page

        # Fetch courses for the current page
        courses = await collection.find().skip((page - 1) * page_size).limit(page_size).to_list(length=None)

        if courses:
            # Create buttons for each course
            keyboard = [
                [InlineKeyboardButton(course['name'], callback_data=f"course_{course['name']}")]
                for course in courses
            ]

            # Add pagination buttons
            pagination_buttons = []
            if page > 1:
                pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"courses_prev_{page-1}"))
            if len(courses) == page_size:
                pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"courses_next_{page+1}"))
            
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
        collection = db['courses']
        page_size = 5  # Number of courses per page

        # Fetch courses for the current page
        courses = await collection.find({"category": category_name}).skip((page - 1) * page_size).limit(page_size).to_list(length=None)

        if courses:
            # Create buttons for each course
            keyboard = [
                [InlineKeyboardButton(course['name'], callback_data=f"course_{course['name']}")]
                for course in courses
            ]

            # Add pagination buttons
            pagination_buttons = []
            if page > 1:
                pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"courses_{category_name}_{page-1}"))
            if len(courses) == page_size:
                pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"courses_{category_name}_{page+1}"))
            
            if pagination_buttons:
                keyboard.append(pagination_buttons)

            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(f"Courses in category '{category_name}':", reply_markup=reply_markup)
        else:
            await update.message.reply_text(f"No courses found in category '{category_name}'.")
    except Exception as e:
        logger.error(f"Error listing courses for category '{category_name}': {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
        
async def handle_courses_pagination(update: Update, context: CallbackContext):
    """Handle pagination for listing courses within a category."""
    query = update.callback_query
    await query.answer()

    # Extract the category name, action, and page number from the callback data
    data = query.data.split('_')
    category_name = data[1]  # The category name
    action = data[2]  # "prev" or "next"
    page = int(data[3])  # Page number

    db = await get_db()
    if db is None:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return

    try:
        collection = db['courses']
        page_size = 5  # Number of courses per page

        # Fetch courses for the current page
        courses = await collection.find({"category": category_name}).skip((page - 1) * page_size).limit(page_size).to_list(length=None)

        if courses:
            # Create buttons for each course
            keyboard = [
                [InlineKeyboardButton(course['name'], callback_data=f"course_{course['name']}")]
                for course in courses
            ]

            # Add pagination buttons
            pagination_buttons = []
            if page > 1:
                pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"courses_{category_name}_{page-1}"))
            if len(courses) == page_size:
                pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"courses_{category_name}_{page+1}"))
            
            if pagination_buttons:
                keyboard.append(pagination_buttons)

            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(f"Courses in category '{category_name}':", reply_markup=reply_markup)
        else:
            await query.edit_message_text(f"No more courses found in category '{category_name}'.")
    except Exception as e:
        logger.error(f"Error handling pagination for category '{category_name}': {e}")
        await query.edit_message_text("An error occurred while fetching courses. Please try again later.")

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
        page_size = 5  # Number of categories per page

        # Fetch categories for the current page
        categories = await collection.find().skip((page - 1) * page_size).limit(page_size).to_list(length=None)

        if categories:
            # Create buttons for each category
            keyboard = [
                [InlineKeyboardButton(category['name'], callback_data=f"category_{category['name']}")]
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
    except Exception as exc:
        logger.error(f"[CAT-INSERT-FAIL] {exc}", exc_info=True)
        await update.message.reply_text("Save failed – check console.")
        return ConversationHandler.END

async def handle_category_selection(update: Update, context: CallbackContext):
    """List courses in the chosen category – each course button is a direct URL."""
    query = update.callback_query
    await query.answer()
    cat_name = query.data.replace("category_", "")

    db = await get_db()
    courses = await db.courses.find({"category": cat_name}).to_list(length=None)
    if not courses:
        await query.edit_message_text(f'Category “{cat_name}” is empty.\nUse /add to populate it.')
        return

    # every button is a url button → opens the link immediately
    keyboard = [
        [InlineKeyboardButton(crs["name"], url=crs["link"])]
        for crs in courses
    ]
    keyboard.append([InlineKeyboardButton("🗑 Delete a course", callback_data=f"del_menu_{cat_name}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
    await query.edit_message_text(
        f'📚 Tap any course to open its link:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
async def handle_course_selection(update: Update, context: CallbackContext):
    """Handle the selection of a course from the buttons."""
    query = update.callback_query
    await query.answer()

    # Extract course name from callback data
    course_name = query.data.split('_')[1]

    # Fetch the course details from the database
    db = await get_db()
    if db is None:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return

    try:
        collection = db['courses']
        course = await collection.find_one({"name": course_name})
        if course:
            # Display course details and options (e.g., delete)
            keyboard = [
                [InlineKeyboardButton("Delete Course", callback_data=f"delete_course_{course_name}")]
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
    db = await get_database()  # Use helper function to get DB connection
    if not db:
        return []

    try:
        user = await db.users.find_one({"user_id": user_id})
        courses = user.get("courses", [])
        
        # Filter courses by category
        filtered_courses = [course for course in courses if course.get("category") == category]

        # Apply pagination
        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        paginated_courses = filtered_courses[start_index:end_index]

        return paginated_courses
    except Exception as e:
        logger.error(f"Error while fetching courses for user {user_id} in category '{category}': {str(e)}")
        return []

async def courses_callback(update: Update, context: CallbackContext):
    """Handle the courses callback and display courses based on pagination."""
    query = update.callback_query
    await query.answer()

    # Split the callback data to extract category and page
    data = query.data.split('_')
    if data[0] == "courses":
        category = data[1]  # The course category (e.g., "Programming")
        page = int(data[2])  # The page number (e.g., 1, 2, 3, etc.)

        # Fetch the courses from the database based on the category and page
        db = await get_db()
        if db is None:
            await query.edit_message_text("Error: Unable to connect to the database.")
            return

        try:
            collection = db['courses']
            courses = await collection.find({"category": category}).to_list(length=10)  # Fetch 10 courses per page
            if courses:
                # Build a message with the course details
                course_list_text = "\n".join([f"📚 {course['name']}\n{course['link']}" for course in courses])
                
                # Build the keyboard for course selection
                keyboard = [
                    [InlineKeyboardButton(course['name'], callback_data=f"course_{course['name']}")]
                    for course in courses
                ]

                # Add pagination buttons if needed
                if page > 1:
                    keyboard.append([InlineKeyboardButton("⬅️ Previous", callback_data=f"courses_{category}_{page-1}")])
                if len(courses) == 10:  # If there are more courses to show
                    keyboard.append([InlineKeyboardButton("➡️ Next", callback_data=f"courses_{category}_{page+1}")])

                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    text=f"Courses in category '{category}':\n\n{course_list_text}",
                    reply_markup=reply_markup
                )
            else:
                await query.edit_message_text(
                    text=f"No courses found in category '{category}' on page {page}.",
                    reply_markup=None
                )
        except Exception as e:
            logger.error(f"Error fetching courses for category '{category}': {e}")
            await query.edit_message_text("An error occurred while fetching courses. Please try again later.")
