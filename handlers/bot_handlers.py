from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    CallbackContext,
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from conversation_states import DELETE_ALL, CONFIRM_DELETE, CANCEL_DELETE
import logging
from database.mongo_handler import MongoDB
from handlers.db_connection import get_db
import urllib.parse

# Logger setup
logger = logging.getLogger(__name__)

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
    db = await get_db()
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

async def handle_course_deletion(update: Update, context: CallbackContext):
    """Handle the deletion of a course."""
    query = update.callback_query
    await query.answer()
    logger.info("[DEL-COURSE] callback data=%s", query.data)

    # Support new callback formats: delete_item::{category}::{course} or delete_course::{category}::{course}
    data = query.data
    cat_name = None
    course_name = None
    if data.startswith("delete_item::") or data.startswith("delete_course::"):
        payload = data.split("::", 1)[1]
        parts = payload.split("::", 1)
        if len(parts) == 2:
            encoded_cat, encoded_course = parts
            cat_name = urllib.parse.unquote_plus(encoded_cat)
            course_name = urllib.parse.unquote_plus(encoded_course)
        else:
            course_name = urllib.parse.unquote_plus(payload)
    else:
        # fallback to old underscore format: delete_item_cat_course or delete_course_cat_course
        if data.startswith("delete_item_"):
            data_old = data.replace("delete_item_", "", 1)
        elif data.startswith("delete_course_"):
            data_old = data.replace("delete_course_", "", 1)
        else:
            data_old = data.replace("delete_item_", "", 1)
        parts_old = data_old.split('_', 1)
        if len(parts_old) == 2:
            cat_name = urllib.parse.unquote_plus(parts_old[0])
            course_name = urllib.parse.unquote_plus(parts_old[1])
        else:
            course_name = urllib.parse.unquote_plus(data_old)

    # Delete the course from the database
    db = await get_db()
    if db is None:
        await query.edit_message_text("Error: Unable to connect to the database.")
        return

    try:
        # Remove the course from the category's embedded array
        if cat_name:
            result = await db['categories'].update_one(
                {"name": cat_name},
                {"$pull": {"courses": {"name": course_name}}}
            )
            logger.info("[DEL-COURSE] pull result=%s", getattr(result, 'raw_result', result))
            if result.modified_count > 0:
                await query.edit_message_text(f"Course '{course_name}' deleted successfully from '{cat_name}'! 🎉")
            else:
                await query.edit_message_text(f"Course '{course_name}' not found in category '{cat_name}'.")
        else:
            # If category not provided, try to pull from any category that contains it
            result = await db['categories'].update_one(
                {"courses.name": course_name},
                {"$pull": {"courses": {"name": course_name}}}
            )
            logger.info("[DEL-COURSE] pull-any result=%s", getattr(result, 'raw_result', result))
            if result.modified_count > 0:
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
        db = await get_db()
        deleted = await delete_item(update.effective_user.id, item_name, 'courses', db)
    elif item_type == 'category':
        db = await get_db()
        deleted = await delete_category(update.effective_user.id, item_name, db)

    if deleted:
        await query.edit_message_text(f"{item_type.capitalize()} '{item_name}' deleted successfully! 🎉")
    else:
        await query.edit_message_text(f"Could not find {item_type} '{item_name}'. 😔")
        
async def handle_deletion_selection(update: Update, context: CallbackContext, item_type: str, items_key: str):
    """General handler for deletion selection (courses, categories)."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    db = await get_db()
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

async def delete_course_menu(update: Update, context: CallbackContext):
    """Show course names (callback buttons) so admin can pick one to delete."""
    query = update.callback_query
    await query.answer()
    cat_name = query.data.replace("del_menu_", "")
    db = await get_db()
    # fetch category document and list its courses
    category_doc = await db.categories.find_one({"name": cat_name})
    if not category_doc or not category_doc.get('courses'):
        await query.edit_message_text("Nothing to delete.")
        return

    courses = category_doc.get('courses', [])

    # include category name in callback so we can remove from correct category
    keyboard = [
        [InlineKeyboardButton(crs["name"], callback_data=f"delete_item::%s::%s" % (urllib.parse.quote_plus(cat_name), urllib.parse.quote_plus(crs['name'])))]
        for crs in courses
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data=f"category_{urllib.parse.quote_plus(cat_name)}")])
    await query.edit_message_text(
        "Choose the course you want to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
async def delete_category_start(update: Update, context: CallbackContext):
    """Show inline buttons with *all* categories that exist in DB."""
    db = await get_db()
    categories = await db.categories.find().distinct("name")          # ← real list
    if not categories:
        await update.message.reply_text("You have no categories to delete.")
        return

    keyboard = [
        [InlineKeyboardButton(cat, callback_data=f"delete_category_{cat}")]
        for cat in categories
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])
    await update.message.reply_text(
        "Choose the category you want to delete:", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def delete_item_start(update: Update, context: CallbackContext):
    """Show every course in the DB as inline buttons."""
    db = await get_db()
    # Aggregate courses across all categories and present them with category-aware callbacks
    cats = await db.categories.find().to_list(length=None)
    all_courses = []
    for cat in cats:
        for crs in cat.get('courses', []):
            all_courses.append({"name": crs.get('name'), "category": cat.get('name')})

    if not all_courses:
        await update.message.reply_text("No courses to delete.")
        return

    keyboard = [
        [InlineKeyboardButton(c['name'], callback_data=f"delete_item::%s::%s" % (urllib.parse.quote_plus(c['category']), urllib.parse.quote_plus(c['name'])))]
        for c in all_courses
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])
    await update.message.reply_text(
        "Choose the course you want to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Note: this command was previously exposed as /delete_course. It has been
    # removed from the top-level command list; deletion should be done via the
    # course UI (/courses) which presents per-course delete buttons.
    
        
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

        # Perform the deletion of categories (courses embedded inside will be removed)
        result = await db['categories'].delete_many({})

        if result.deleted_count > 0:
            logger.info(f"All categories deleted successfully for user {user_id}.")
            await query.edit_message_text("All categories and their embedded courses have been deleted. 😞")
        else:
            logger.warning(f"No categories found to delete for user {user_id}.")
            await query.edit_message_text("No categories found to delete. 😞")
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
