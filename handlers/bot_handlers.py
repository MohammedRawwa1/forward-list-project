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
    if len(items_list) == page_size:
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
    """Handle the deletion of a course."""
    query = update.callback_query
    await safe_answer(query)
    logger.info("[DEL-COURSE] callback data=%s", query.data)

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
        if data.startswith("delete_item_"):
            data_old = data.replace("delete_item_", "", 1)
        elif data.startswith("delete_course_"):
            data_old = data.replace("delete_course_", "", 1)
        else:
            data_old = data.replace("delete_item_", "", 1)

        parts_old = data_old.split("_", 1)
        if len(parts_old) == 2:
            cat_name = urllib.parse.unquote_plus(parts_old[0])
            course_name = urllib.parse.unquote_plus(parts_old[1])
        else:
            course_name = urllib.parse.unquote_plus(data_old)

    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return

    try:
        logger.info(
            "[DEL-COURSE] parsed cat_name=%s course_name=%s",
            cat_name,
            course_name,
        )

        if cat_name:
            result = await db["categories"].update_one(
                {"name": cat_name},
                {"$pull": {"courses": {"name": course_name}}},
            )

            logger.info("[DEL-COURSE] pull result=%s", getattr(result, "raw_result", result))

            if result.modified_count > 0:
                await safe_edit_message(
                    query,
                    f"Course '{course_name}' deleted successfully from '{cat_name}'! 🎉",
                    action_key=getattr(query, "data", None),
                )
            else:
                category_doc = await db["categories"].find_one({"name": cat_name})
                logger.warning(
                    "[DEL-COURSE] delete failed for '%s' in category '%s' — category_doc=%s",
                    course_name,
                    cat_name,
                    category_doc,
                )

                if category_doc and category_doc.get("courses"):
                    names = [c.get("name") for c in category_doc.get("courses", [])]
                    logger.info(
                        "[DEL-COURSE] available course names in category '%s': %s",
                        cat_name,
                        names,
                    )
                    close = difflib.get_close_matches(course_name, names, n=5, cutoff=0.6)
                    if close:
                        logger.info(
                            "[DEL-COURSE] close matches for '%s' in '%s': %s",
                            course_name,
                            cat_name,
                            close,
                        )

                await safe_edit_message(
                    query,
                    f"Course '{course_name}' not found in category '{cat_name}'.",
                    action_key=getattr(query, "data", None),
                )
        else:
            result = await db["categories"].update_one(
                {"courses.name": course_name},
                {"$pull": {"courses": {"name": course_name}}},
            )

            logger.info("[DEL-COURSE] pull-any result=%s", getattr(result, "raw_result", result))

            if result.modified_count > 0:
                await safe_edit_message(
                    query,
                    f"Course '{course_name}' deleted successfully! 🎉",
                    action_key=getattr(query, "data", None),
                )
            else:
                cats = await db["categories"].find().to_list(length=None)
                candidates = []
                for cat in cats:
                    for crs in cat.get("courses", []):
                        if crs.get("name") == course_name:
                            candidates.append((cat.get("name"), crs.get("name")))

                if not candidates:
                    all_names = []
                    for cat in cats:
                        for crs in cat.get("courses", []):
                            all_names.append((cat.get("name"), crs.get("name")))

                    close = [
                        (cn, nm)
                        for cn, nm in all_names
                        if difflib.get_close_matches(course_name, [nm], cutoff=0.6)
                    ]
                    logger.warning(
                        "[DEL-COURSE] no exact candidates; fuzzy close matches: %s",
                        close,
                    )
                else:
                    logger.info(
                        "[DEL-COURSE] exact candidates found (unexpected): %s",
                        candidates,
                    )

                await safe_edit_message(
                    query,
                    f"Course '{course_name}' not found.",
                    action_key=getattr(query, "data", None),
                )

    except Exception as e:
        logger.error(f"Error deleting course '{course_name}': {e}")
        await safe_edit_message(
            query,
            "An error occurred while deleting the course. Please try again later.",
            action_key=getattr(query, "data", None),
        )


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
    """Show every course in the DB as inline buttons."""

    db = await get_db()

    cats = await db.categories.find().to_list(length=None)

    # Sort categories safely
    cats = sorted(
        cats,
        key=lambda c: normalize_name(c.get("name"))
    )

    all_courses = []

    for cat in cats:
        category_name = (cat.get("name") or "").strip()

        for crs in cat.get("courses", []):
            course_name = (crs.get("name") or "").strip()

            all_courses.append({
                "name": course_name,
                "category": category_name
            })

    # Sort courses alphabetically
    all_courses = sorted(
        all_courses,
        key=lambda c: normalize_name(c.get("name"))
    )

    if not all_courses:
        await update.message.reply_text("No courses to delete.")
        return

    keyboard = []

    for c in all_courses:
        cat = urllib.parse.quote_plus(c["category"])
        name = urllib.parse.quote_plus(c["name"])

        keyboard.append([
            InlineKeyboardButton(
                c["name"],
                callback_data=f"delete_item::{cat}::{name}"
            )
        ])

    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

    await update.message.reply_text(
        "Choose the course you want to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )    

async def delete_category_start(update: Update, context: CallbackContext):
    """Show categories with delete buttons."""
    db = await get_db()

    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        cats = await db["categories"].find().to_list(length=None)

        # Normalize and sort categories safely
        cats = sorted(
            cats,
            key=lambda c: normalize_name(c.get("name"))
        )

        if not cats:
            await update.message.reply_text("No coach categories available to delete.")
            return

        keyboard = []

        for cat in cats:
            name = (cat.get("name") or "").strip()

            is_parent = not cat.get("parent")
            display_name = f"{name} {'(parent)' if is_parent else ''}".strip()

            try:
                payload = {"category": name, "name": name}
                key = _store_callback_payload(payload)
                cb = f"delete_summary::category::{key}"
            except Exception:
                cb = f"delete_category_{urllib.parse.quote_plus(name)}"

            keyboard.append(
                [InlineKeyboardButton(display_name, callback_data=cb)]
            )

        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

        await update.message.reply_text(
            "Choose a category to delete (parents are marked):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.exception("Error listing categories for deletion: %s", e)
        await update.message.reply_text("An error occurred. Please try again later.")

async def delete_parent_start(update: Update, context: CallbackContext):
    """Show top-level parent categories for deletion."""

    db = await get_db()

    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        cats = await db["categories"].find(
            {"parent": {"$exists": False}}
        ).to_list(length=None)

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

            keyboard.append([
                InlineKeyboardButton(
                    name,
                    callback_data=f"delete_category_{urllib.parse.quote_plus(name)}"
                )
            ])

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
