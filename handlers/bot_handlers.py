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
import difflib
from handlers.base_handlers import safe_edit_message, _store_callback_payload, safe_answer

# Logger setup
logger = logging.getLogger(__name__)

def normalize_name(name: str) -> str:
    """
    Normalize category/course names for consistent comparison and sorting.
    """
    if not name:
        return ""
    return name.strip().casefold()
    
# Helper function to generate pagination keyboard
async def generate_pagination_keyboard(items_list, page, page_size, callback_pattern):
    """Generate pagination buttons for lists of items."""
    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{callback_pattern}_{page-1}")
        )
    pagination_buttons.append(
        InlineKeyboardButton("🏠 Home", callback_data="home")
    )
    if len(items_list) > page * page_size:
        pagination_buttons.append(
            InlineKeyboardButton("➡️ Next", callback_data=f"page_{callback_pattern}_{page+1}")
        )
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

        pagination_buttons = await generate_pagination_keyboard(
            items_list, page, page_size, callback_pattern
        )
        if pagination_buttons:
            keyboard.append(pagination_buttons)

        return InlineKeyboardMarkup(keyboard)

    except Exception as e:
        logger.error(
            f"Error generating keyboard for user {user_id}: {e}",
            exc_info=True,
        )
        return None


# Helper function for deleting a course
async def delete_item(user_id, item_name, items_key, db):
    """Delete an item from the user's data."""
    try:
        result = await db.users.update_one(
            {"user_id": user_id},
            {"$pull": {items_key: {"name": item_name}}},
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
        # Prefer the shared `categories` collection when present (new schema).
        if hasattr(db, 'categories') or 'categories' in getattr(db, '__dict__', {}):
            # Recursively collect this category and all descendants
            to_delete = set()
            stack = [category_name]
            while stack:
                curr = stack.pop()
                if curr in to_delete:
                    continue
                to_delete.add(curr)
                children = await db['categories'].find({"parent": curr}).to_list(length=None)
                for ch in children:
                    name = ch.get('name')
                    if name and name not in to_delete:
                        stack.append(name)
            if to_delete:
                res = await db['categories'].delete_many({"name": {"$in": list(to_delete)}})
                if getattr(res, 'deleted_count', 0) > 0:
                    logger.info(f"Category '{category_name}' and its descendants deleted for user {user_id}.")
                    return True
                else:
                    logger.warning(f"Category '{category_name}' not found in categories collection for user {user_id}.")
                    return False
            else:
                logger.warning(f"Nothing to delete for category '{category_name}'.")
                return False
        else:
            # Fallback: legacy per-user schema stored in `users` collection
            result = await db.users.update_one(
                {"user_id": user_id},
                {"$pull": {"categories": category_name, "courses": {"category": category_name}}},
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
    """Handle deletion of courses or empty categories."""
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    logger.info("[DEL-COURSE] callback data=%s", data)

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, "data", None))
        return

    try:
        if data.startswith("delete_item::"):
            # This is an empty category or a single course
            parts = data.split("::", 2)
            if len(parts) == 3:
                cat_name = urllib.parse.unquote_plus(parts[1])
                item_name = urllib.parse.unquote_plus(parts[2])
            else:
                await safe_edit_message(query, "Invalid callback data.", action_key=getattr(query, "data", None))
                return

            # If item_name is "(empty)" → delete category
            if item_name == "(empty)":
                res = await db["categories"].delete_one({"name": cat_name})
                if res.deleted_count > 0:
                    await safe_edit_message(query, f"Empty category '{cat_name}' deleted successfully! 🎉", action_key=getattr(query, "data", None))
                else:
                    await safe_edit_message(query, f"Category '{cat_name}' not found.", action_key=getattr(query, "data", None))
                return

        elif data.startswith("delete_category::"):
            # Deleting category with courses
            parts = data.split("::", 1)
            if len(parts) == 2:
                cat_name = urllib.parse.unquote_plus(parts[1])
            else:
                await safe_edit_message(query, "Invalid callback data.", action_key=getattr(query, "data", None))
                return

        else:
            await safe_edit_message(query, "Unknown deletion action.", action_key=getattr(query, "data", None))
            return

        # Delete the category or pull a course from it
        result = await db["categories"].update_one(
            {"name": cat_name},
            {"$pull": {"courses": {"name": item_name}}} if item_name != "(empty)" else {}
        )

        if result.modified_count > 0:
            await safe_edit_message(query, f"Course '{item_name}' deleted successfully from '{cat_name}'! 🎉", action_key=getattr(query, "data", None))
        else:
            # Category exists but course not found or category was empty
            cat_doc = await db["categories"].find_one({"name": cat_name})
            if cat_doc is None:
                await safe_edit_message(query, f"Category '{cat_name}' not found.", action_key=getattr(query, "data", None))
            else:
                await safe_edit_message(query, f"No course named '{item_name}' found in category '{cat_name}'.", action_key=getattr(query, "data", None))

    except Exception as e:
        logger.error(f"Error deleting course or category '{cat_name}': {e}", exc_info=True)
        await safe_edit_message(query, "An error occurred while deleting. Please try again later.", action_key=getattr(query, "data", None))
        
async def handle_cancel_delete_callback(update: Update, context: CallbackContext):
    """Handle cancel_delete_{type}::{encoded_name} and simple cancel_delete callbacks."""
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    # Accept a variety of cancel formats so all cancel buttons behave nicely.
    if data in ("cancel", "cancel_delete", "cancel_delete_all", "cancel_delete_all_data"):
        await safe_edit_message(query, "Deletion canceled.", action_key=getattr(query, "data", None))
        return

    # Normalize payload prefixes created by different flows: "cancel_delete_..." or "cancel_delete::..."
    payload = data
    for prefix in ("cancel_delete_", "cancel_delete::"):
        if payload.startswith(prefix):
            payload = payload[len(prefix):]
            break

    parts = payload.split("::", 1)
    if len(parts) == 2:
        item_type, enc_name = parts
        try:
            item_name = urllib.parse.unquote_plus(enc_name)
        except Exception:
            item_name = enc_name
        await safe_edit_message(
            query,
            f"Deletion of {item_type} '{item_name}' canceled.",
            action_key=getattr(query, "data", None),
        )
        return

    # Fallback: just show a friendly cancel message instead of an "Invalid" one.
    await safe_edit_message(query, "Deletion canceled.", action_key=getattr(query, "data", None))


async def delete_item_start(update: Update, context: CallbackContext):
    """Show every course and empty category in the DB as inline buttons."""
    db = await get_db()
    cats = await db.categories.find().to_list(length=None)

    # Sort categories safely
    cats = sorted(cats, key=lambda c: normalize_name(c.get("name")))

    all_items = []

    for cat in cats:
        category_name = (cat.get("name") or "").strip()
        courses = cat.get("courses")

        if courses:
            # Add all courses
            for crs in courses:
                course_name = (crs.get("name") or "").strip()
                all_items.append({
                    "name": course_name,
                    "category": category_name
                })
        else:
            # No courses → treat as empty folder
            all_items.append({
                "name": "(empty)",
                "category": category_name
            })

    if not all_items:
        await update.message.reply_text("No courses or categories to delete.")
        return

    keyboard = []
    for c in all_items:
        cat = urllib.parse.quote_plus(c["category"])
        name = urllib.parse.quote_plus(c["name"])
        # Display differently if it's an empty folder
        display_text = f"{c['category']} (empty)" if c["name"] == "(empty)" else f"{c['category']} → {c['name']}"
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"delete_item::{cat}::{name}"
            )
        ])

    # Always append Cancel button
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

    await update.message.reply_text(
        "Choose the course or empty category you want to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
                
async def delete_category_start(update: Update, context: CallbackContext):
    """Show a paginated list of categories for deletion.

    Uses `delete_category_page::<n>` callbacks to navigate pages.
    """
    db = await get_db()

    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        # Only list child categories (those with a parent). Parent/top-level
        # categories are managed via `/delete_parent` and should not appear
        # in the `/delete_category` flow.
        cats = await db["categories"].find({
            "$and": [
                {"parent": {"$exists": True}},
                {"parent": {"$nin": [None, ""]}}
            ]
        }).to_list(length=None)

        if not cats:
            await update.message.reply_text("No categories available to delete.")
            return

        # Sort categories by name safely
        cats = sorted(cats, key=lambda c: normalize_name(c.get("name")))

        # Default page size (can be overridden in context.bot_data)
        page_size = int(context.bot_data.get('delete_cat_page_size', 20))
        page = 1
        start = (page - 1) * page_size
        end = start + page_size
        page_cats = cats[start:end]

        keyboard = []
        for cat in page_cats:
            name = (cat.get("name") or "").strip()
            courses = cat.get("courses")
            display_name = f"{name} (empty)" if not courses else name
            encoded_name = urllib.parse.quote_plus(name)
            cb = f"delete_item::{encoded_name}::(empty)" if not courses else f"delete_category_{encoded_name}"
            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        # Pagination nav — replace non-functional "Home" with an "End" button
        nav = []
        total_pages = (len(cats) - 1) // page_size + 1 if cats else 1
        last_page = max(1, total_pages)
        # Add Previous only when applicable
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"delete_category_page::{page-1}"))
        # End button sends user to the last page
        nav.append(InlineKeyboardButton("🏁 End", callback_data=f"delete_category_page::{last_page}"))
        # Next when there are more pages after this one
        if len(cats) > end:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"delete_category_page::{page+1}"))
        if nav:
            keyboard.append(nav)

        # Cancel button
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

        await update.message.reply_text(
            "Choose a category to delete:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.exception("Error listing categories for deletion: %s", e)
        await update.message.reply_text("An error occurred. Please try again later.")


async def handle_delete_category_page(update: Update, context: CallbackContext):
    """Render a specific page of categories for deletion (callback).

    Callback format: `delete_category_page::<page>`
    """
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    try:
        _, page_s = data.split("::", 1)
        page = int(page_s)
    except Exception:
        page = 1

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.")
        return

    # Only page through child categories (exclude parents/top-level folders)
    cats = await db["categories"].find({
        "$and": [
            {"parent": {"$exists": True}},
            {"parent": {"$nin": [None, ""]}}
        ]
    }).to_list(length=None)
    cats = sorted(cats, key=lambda c: normalize_name(c.get("name")))
    page_size = int(context.bot_data.get('delete_cat_page_size', 20))
    start = (page - 1) * page_size
    end = start + page_size
    page_cats = cats[start:end]

    keyboard = []
    for cat in page_cats:
        name = (cat.get("name") or "").strip()
        courses = cat.get("courses")
        display_name = f"{name} (empty)" if not courses else name
        encoded_name = urllib.parse.quote_plus(name)
        cb = f"delete_item::{encoded_name}::(empty)" if not courses else f"delete_category_{encoded_name}"
        keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

    nav = []
    total_pages = (len(cats) - 1) // page_size + 1 if cats else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"delete_category_page::{page-1}"))
    nav.append(InlineKeyboardButton("🏁 End", callback_data=f"delete_category_page::{last_page}"))
    if len(cats) > end:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"delete_category_page::{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

    await safe_edit_message(query, "Choose a category to delete:", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
        
async def delete_parent_start(update: Update, context: CallbackContext):
    """Show top-level parent categories for deletion."""

    db = await get_db()

    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        cats = await db["categories"].find({
            "$or": [
                {"parent": {"$exists": False}},
                {"parent": None},
                {"parent": ""}
            ]
        }).to_list(length=None)

        cats = sorted(
            cats,
            key=lambda c: normalize_name(c.get("name"))
        )

        if not cats:
            await update.message.reply_text("No parent categories available to delete.")
            return

        keyboard = []

        for cat in cats:
            name = (cat.get("name") or "").strip()
            parent = cat.get("parent")

            if parent:
                display_name = f"{parent} → {name}"
            else:
                display_name = f"{name} (parent)"

            try:
                payload = {
                    "category": name,
                    "parent": parent
                }
                key = _store_callback_payload(payload)
                cb = f"delete_summary::category::{key}"
            except Exception:
                encoded_name = urllib.parse.quote_plus(name)
                encoded_parent = urllib.parse.quote_plus(parent or "")
                cb = f"delete_category::{encoded_parent}::{encoded_name}" 

            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        # Add a single Cancel button at the end
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

        await update.message.reply_text(
            "Choose a parent category to delete:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.exception("Error listing parent categories for deletion: %s", e)
        await update.message.reply_text("An error occurred. Please try again later.")
                                
async def delete_all_data_start(update: Update, context: CallbackContext):
    """Start the delete-all-data confirmation conversation."""
    # Prompt user with confirmation buttons; ConversationHandler expects DELETE_ALL state
    keyboard = [
        [InlineKeyboardButton("Yes, delete all", callback_data="confirm_delete_all")],
        [InlineKeyboardButton("No, cancel", callback_data="cancel_delete_all")],
    ]
    await update.message.reply_text(
        "Are you sure you want to delete ALL categories and courses? This cannot be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    try:
        from conversation_states import DELETE_ALL
        return DELETE_ALL
    except Exception:
        return None
# Handle confirmation of deleting all data
async def confirm_delete_all(update: Update, context: CallbackContext):
    """Confirm and delete all categories and courses."""
    query = update.callback_query
    await safe_answer(query)  # Acknowledge the callback query

    user_id = update.effective_user.id

    try:
        db = await get_db()  # Await the database connection
        if db is None:
            logger.error(f"Database connection failed for user {user_id}.")
            await safe_edit_message(query, "Error: Unable to connect to the database. Please try again later.", action_key=getattr(query, 'data', None))
            return ConversationHandler.END

        # Perform the deletion of categories (courses embedded inside will be removed)
        result = await db['categories'].delete_many({})

        if result.deleted_count > 0:
            logger.info(f"All categories deleted successfully for user {user_id}.")
            await safe_edit_message(query, "All categories and their embedded courses have been deleted. 😞", action_key=getattr(query, 'data', None))
        else:
            logger.warning(f"No categories found to delete for user {user_id}.")
            await safe_edit_message(query, "No categories found to delete. 😞", action_key=getattr(query, 'data', None))
    except Exception as e:
        logger.error(f"Error confirming delete all data for user {user_id}: {e}", exc_info=True)
        await safe_edit_message(query, "An error occurred while deleting all data. Please try again later.", action_key=getattr(query, 'data', None))
    
    return ConversationHandler.END
    
# Cancel deletion of all user data
async def cancel_delete_all_data(update: Update, context: CallbackContext) -> int:
    """Cancel the deletion of all user data."""
    await safe_answer(update.callback_query)
    await safe_edit_message(update.callback_query, "Deletion of all data has been canceled.", action_key=getattr(update.callback_query, 'data', None))
    return ConversationHandler.END

async def initiate_delete_item(update: Update, context: CallbackContext, item_type: str, item_name: str):
    """Initiate the deletion of a specific item (course or category) by asking for confirmation."""
    query = update.callback_query
    await safe_answer(query)

    # Construct the confirmation message
    confirmation_message = f"Are you sure you want to delete the {item_type} '{item_name}'? This action cannot be undone. ⚠️"
    
    # Inline keyboard for confirmation
    keyboard = [
        [InlineKeyboardButton("Yes", callback_data=f"confirm_delete_{item_type}::{urllib.parse.quote_plus(item_name)}")],
        [InlineKeyboardButton("No", callback_data=f"cancel_delete_{item_type}::{urllib.parse.quote_plus(item_name)}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Edit the message to prompt for confirmation
    await safe_edit_message(query, confirmation_message, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
