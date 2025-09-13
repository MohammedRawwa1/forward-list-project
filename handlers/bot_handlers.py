from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    CallbackContext,
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import logging
from database.mongo_handler import MongoDB
from handlers.db_connection import get_db

# Logger setup
logger = logging.getLogger(__name__)

# Conversation states for deleting all data
DELETE_ALL, CONFIRM_DELETE, CANCEL_DELETE = range(3, 6)

# Helper function to generate pagination keyboard
async def generate_pagination_keyboard(items_list, page, page_size, callback_pattern):
    """Generate pagination buttons for lists of items."""
    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{callback_pattern}_{page-1}"))
    pagination_buttons.append(InlineKeyboardButton("🏠 Home", callback_data="home"))
    if len(items_list) == page_size:
        pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"page_{callback_pattern}_{page+1}"))
    return pagination_buttons

# Helper function for generating the inline keyboard
async def generate_keyboard(user_id, items, callback_pattern, page=1, page_size=20):
    db = get_db()
    try:
        user = await db.users.find_one({"user_id": user_id})
        if not user or items not in user:
            return None

        items_list = user[items]
        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        paginated_items = items_list[start_index:end_index]

        keyboard = [
            [InlineKeyboardButton(item, callback_data=f"{callback_pattern}_{item}")]
            for item in paginated_items
        ]

        pagination_buttons = await generate_pagination_keyboard(items_list, page, page_size, callback_pattern)
        keyboard.append(pagination_buttons)

        return InlineKeyboardMarkup(keyboard)
    except Exception as e:
        logger.error(f"Error generating keyboard for user {user_id}: {e}", exc_info=True)
        return None

# Helper function for deleting a course
async def delete_item(user_id, item_name, items_key, db):
    """Delete an item from the user's data."""
    try:
        result = await db.users.update_one(
            {"user_id": user_id},
            {"$pull": {items_key: {"name": item_name}}}
        )
        if result.modified_count > 0:
            logger.info(f"Item '{item_name}' deleted successfully for user {user_id}.")
            return True
        else:
            logger.warning(f"Item '{item_name}' not found for user {user_id}.")
            return False
    except Exception as e:
        logger.error(f"Error deleting item '{item_name}' for user {user_id}: {e}")
        return False

async def delete_category(user_id, category_name, db):
    """Delete a category and all its associated courses."""
    try:
        result = await db.users.update_one(
            {"user_id": user_id},
            {"$pull": {"categories": category_name, "courses": {"category": category_name}}}
        )
        if result.modified_count > 0:
            logger.info(f"Category '{category_name}' deleted successfully for user {user_id}.")
            return True
        else:
            logger.warning(f"Category '{category_name}' not found for user {user_id}.")
            return False
    except Exception as e:
        logger.error(f"Error deleting category '{category_name}' for user {user_id}: {e}")
        return False

async def delete_category(user_id, category_name, db):
    """Delete a category and all its associated courses."""
    try:
        result = await db.users.update_one(
            {"user_id": user_id},
            {"$pull": {"categories": category_name, "courses": {"category": category_name}}}
        )
        if result.modified_count > 0:
            logger.info(f"Category '{category_name}' deleted successfully for user {user_id}.")
            return True
        else:
            logger.warning(f"Category '{category_name}' not found for user {user_id}.")
            return False
    except Exception as e:
        logger.error(f"Error deleting category '{category_name}' for user {user_id}: {e}")
        return False

async def handle_course_deletion(update: Update, context: CallbackContext):
    """Handle the deletion of a course."""
    query = update.callback_query
    await query.answer()

    # Extract course name from callback data
    course_name = query.data.split('_')[2]

    # Delete the course from the database
    db = await get_db()
    if db is None:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return

    try:
        collection = db['courses']
        result = await collection.delete_one({"name": course_name})
        if result.deleted_count > 0:
            await query.edit_message_text(f"Course '{course_name}' deleted successfully! 🎉")
        else:
            await query.edit_message_text(f"Course '{course_name}' not found.")
    except Exception as e:
        logger.error(f"Error deleting course '{course_name}': {e}")
        await query.edit_message_text("An error occurred while deleting the course. Please try again later.")
        
# Generic function for handling item deletion confirmation
async def handle_deletion_confirmation(update: Update, context: CallbackContext, item_type: str, item_name: str):
    query = update.callback_query
    await query.answer()

    if item_type == 'course':
        deleted = await delete_item(update.effective_user.id, item_name, 'courses', get_db())
    elif item_type == 'category':
        deleted = await delete_category(update.effective_user.id, item_name, get_db())

    if deleted:
        await query.edit_message_text(f"{item_type.capitalize()} '{item_name}' deleted successfully! 🎉")
    else:
        await query.edit_message_text(f"Could not find {item_type} '{item_name}'. 😔")
        
async def handle_deletion_selection(update: Update, context: CallbackContext, item_type: str, items_key: str):
    """General handler for deletion selection (courses, categories)."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    db = get_db()
    try:
        user = await db.users.find_one({"user_id": user_id})
        if not user or not user.get(items_key):
            await query.edit_message_text(f"You have no {item_type}s to delete.")
            return

        items = user[items_key]

        keyboard = [
            [InlineKeyboardButton(item, callback_data=f"delete_{item_type}_{item}") for item in items]
        ]

        if not keyboard:
            await query.edit_message_text(f"You have no {item_type}s to delete.")
            return

        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(f"Select the {item_type} you want to delete:", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error handling {item_type} deletion selection for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text(f"An error occurred while retrieving your {item_type}s for deletion.")

async def delete_all_data(user_id, db):
    """Delete all categories and courses for a user."""
    try:
        # Delete all categories
        await db['categories'].delete_many({})
        # Delete all courses
        await db['courses'].delete_many({})
        logger.info(f"All data deleted successfully for user {user_id}.")
        return True
    except Exception as e:
        logger.error(f"Error deleting all data: {e}")
        return False
        
# Handle the confirmation for deleting all data
async def delete_all_data_start(update: Update, context: CallbackContext) -> int:
    """Start the process of deleting all user data."""
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("Yes", callback_data="confirm_delete_all")],
        [InlineKeyboardButton("No", callback_data="cancel_delete_all")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Are you sure you want to delete all your data?", reply_markup=reply_markup)
    return DELETE_ALL

async def delete_category_start(update: Update, context: CallbackContext):
    """Start the process of deleting a category."""
    user_id = update.effective_user.id
    db = await get_db()

    try:
        # Fetch categories for the user
        user = await db.users.find_one({"user_id": user_id})
        if not user or not user.get("categories"):
            await update.message.reply_text("You have no categories to delete.")
            return

        # Display categories for deletion
        categories = user["categories"]
        keyboard = [
            [InlineKeyboardButton(category, callback_data=f"delete_category_{category}")]
            for category in categories
        ]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text("Select the category you want to delete:", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error starting category deletion for user {user_id}: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
        
async def delete_item_start(update: Update, context: CallbackContext):
    """Start the process of deleting an item."""
    user_id = update.effective_user.id
    db = await get_db()

    try:
        # Fetch items for the user
        user = await db.users.find_one({"user_id": user_id})
        if not user or not user.get("courses"):
            await update.message.reply_text("You have no items to delete.")
            return

        # Display items for deletion
        items = user["courses"]
        keyboard = [
            [InlineKeyboardButton(item["name"], callback_data=f"delete_item_{item['name']}")]
            for item in items
        ]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text("Select the item you want to delete:", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error starting item deletion for user {user_id}: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
        
# Handle confirmation of deleting all data
async def confirm_delete_all(update: Update, context: CallbackContext):
    """Confirm and delete all categories and courses."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback query

    user_id = update.effective_user.id

    try:
        db = await get_db()  # Await the database connection
        if db is None:
            logger.error(f"Database connection failed for user {user_id}.")
            await query.edit_message_text("Error: Unable to connect to the database. Please try again later.")
            return ConversationHandler.END

        # Perform the deletion
        result = await db['categories'].delete_many({})
        result = await db['courses'].delete_many({})

        if result.deleted_count > 0:
            logger.info(f"All data deleted successfully for user {user_id}.")  # Success log
            await query.edit_message_text("All categories and courses have been deleted. 😞")
        else:
            logger.warning(f"No data found to delete for user {user_id}.")  # Warning log
            await query.edit_message_text("No data found to delete. 😞")
    except Exception as e:
        logger.error(f"Error confirming delete all data for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text("An error occurred while deleting all data. Please try again later.")
    
    return ConversationHandler.END
    
# Cancel deletion of all user data
async def cancel_delete_all_data(update: Update, context: CallbackContext) -> int:
    """Cancel the deletion of all user data."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Deletion of all data has been canceled.")
    return ConversationHandler.END

async def initiate_delete_item(update: Update, context: CallbackContext, item_type: str, item_name: str):
    """Initiate the deletion of a specific item (course or category) by asking for confirmation."""
    query = update.callback_query
    await query.answer()

    # Construct the confirmation message
    confirmation_message = f"Are you sure you want to delete the {item_type} '{item_name}'? This action cannot be undone. ⚠️"
    
    # Inline keyboard for confirmation
    keyboard = [
        [InlineKeyboardButton("Yes", callback_data=f"confirm_delete_{item_type}_{item_name}")],
        [InlineKeyboardButton("No", callback_data=f"cancel_delete_{item_type}_{item_name}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Edit the message to prompt for confirmation
    await query.edit_message_text(confirmation_message, reply_markup=reply_markup)
