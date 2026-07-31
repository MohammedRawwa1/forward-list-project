import logging
import urllib.parse
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, ConversationHandler

from config import is_owner
from handlers import base_handlers
from handlers.db_connection import get_db

# Logger setup
logger = logging.getLogger(__name__)


async def handle_cancel_delete_callback(update: Update, context: CallbackContext):
    """Handle cancel_delete_{type}::{encoded_name} and simple cancel_delete callbacks."""
    query = update.callback_query
    await base_handlers.safe_answer(query)
    data = query.data
    # Accept a variety of cancel formats so all cancel buttons behave nicely.
    if data in ("cancel", "cancel_delete", "cancel_delete_all", "cancel_delete_all_data"):
        await base_handlers.safe_edit_message(query, "Deletion canceled.", action_key=getattr(query, "data", None))
        return

    # Normalize payload prefixes created by different flows: "cancel_delete_..." or "cancel_delete::..."
    payload = data
    for prefix in ("cancel_delete_", "cancel_delete::"):
        if payload.startswith(prefix):
            payload = payload[len(prefix) :]
            break

    parts = payload.split("::", 1)
    if len(parts) == 2:
        item_type, enc_name = parts
        try:
            item_name = urllib.parse.unquote_plus(enc_name)
        except Exception:
            item_name = enc_name
        await base_handlers.safe_edit_message(
            query,
            f"Deletion of {item_type} '{item_name}' canceled.",
            action_key=getattr(query, "data", None),
        )
        return

    # Fallback: just show a friendly cancel message instead of an "Invalid" one.
    await base_handlers.safe_edit_message(query, "Deletion canceled.", action_key=getattr(query, "data", None))


async def delete_category_start(update: Update, context: CallbackContext):
    keyboard = []  # defensive initialization
    """Show a paginated list of categories for deletion.

    Uses `delete_category_page::<n>` callbacks to navigate pages.
    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    # Owner-only: restrict delete UI
    user_id = getattr(update.message.from_user, "id", None)
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Only the bot owner can run this command.")
        return

    db = await get_db()

    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        # Only list child categories (those with a parent). Parent/top-level
        # categories are managed via `/delete_parent` and should not appear
        # in the `/delete_category` flow. Use DB-side pagination.
        filter_q = {"$and": [{"parent": {"$exists": True}}, {"parent": {"$nin": [None, ""]}}]}
        # Default page size (can be overridden in context.bot_data)
        page_size = int(context.bot_data.get("delete_cat_page_size", 20))
        page = 1
        total = await base_handlers.get_total_count(db, "categories", filter_q, ttl=15)
        start = (page - 1) * page_size
        cursor = db["categories"].find(filter_q).sort("name", 1).skip(start).limit(page_size)
        page_cats = await cursor.to_list(length=page_size)
        if not page_cats:
            await update.message.reply_text("No categories available to delete.")
            return

        # Ensure categories on this page have stable UUIDs
        for c in page_cats:
            if not c.get("id"):
                try:
                    new_id = str(uuid.uuid4())
                    await db["categories"].update_one({"_id": c.get("_id")}, {"$set": {"id": new_id}})
                    c["id"] = new_id
                except Exception:
                    pass

        keyboard = []
        for cat in page_cats:
            name = (cat.get("name") or "").strip()
            parent = (cat.get("parent") or "").strip() if cat.get("parent") else None
            # Show parent context to avoid ambiguous choices: "Parent → Child"
            if parent:
                display_name = f"{parent} → {name}"
            else:
                display_name = f"{name} (no parent)"

            # Persist a small payload containing the category id so deletion
            # resolves by id rather than name. Fall back to name-based cb.
            try:
                payload = {"category": name, "id": cat.get("id"), "parent": parent, "path": cat.get("path")}
                key = base_handlers._store_callback_payload(payload)
                cb = f"delete_category_{key}"
            except Exception:
                encoded_name = urllib.parse.quote_plus(name)
                cb = f"delete_category_{encoded_name}"

            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        # Pagination nav: Prev (left), Home (center when not on page 1), Next (right), End always at the end
        nav = []
        total_pages = (total - 1) // page_size + 1 if total else 1
        last_page = max(1, total_pages)
        # Desired order: Next (left), Home (center when applicable), End, Previous (right-most)
        if page < last_page:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"delete_category_page::{page + 1}"))

        # Home (center) — show only when not on page 1 (appears starting page 2)
        if page > 1:
            nav.append(InlineKeyboardButton("🏠 Home", callback_data="delete_category_page::1"))

        # End button sends user to the last page (keep near right)
        if total_pages > 1 and page < last_page:
            nav.append(InlineKeyboardButton("🏁 End", callback_data=f"delete_category_page::{last_page}"))

        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"delete_category_page::{page - 1}"))
        if nav:
            keyboard.append(nav)
        # Cancel button
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

        await update.message.reply_text(
            "Choose a category to delete:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception:
        logger.exception("Error listing categories for deletion")
        await update.message.reply_text("An error occurred. Please try again later.")


async def handle_delete_category_page(update: Update, context: CallbackContext):
    """Render a specific page of categories for deletion (callback).

    Callback format: `delete_category_page::<page>`
    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    query = update.callback_query
    await base_handlers.safe_answer(query)
    # Owner-only guard for delete pagination callbacks
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await base_handlers.safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    data = query.data
    try:
        _, page_s = data.split("::", 1)
        page = int(page_s)
    except Exception:
        page = 1

    db = await get_db()
    if db is None:
        await base_handlers.safe_edit_message(query, "Error: Unable to connect to the database.")
        return

    # Only page through child categories (exclude parents/top-level folders)
    filter_q = {"$and": [{"parent": {"$exists": True}}, {"parent": {"$nin": [None, ""]}}]}
    page_size = int(context.bot_data.get("delete_cat_page_size", 20))
    total = await base_handlers.get_total_count(db, "categories", filter_q, ttl=15)
    start = (page - 1) * page_size
    cursor = db["categories"].find(filter_q).sort("name", 1).skip(start).limit(page_size)
    page_cats = await cursor.to_list(length=page_size)

    # Ensure categories on this page have stable UUIDs
    for c in page_cats:
        if not c.get("id"):
            try:
                new_id = str(uuid.uuid4())
                await db["categories"].update_one({"_id": c.get("_id")}, {"$set": {"id": new_id}})
                c["id"] = new_id
            except Exception:
                pass

    keyboard = []
    for cat in page_cats:
        name = (cat.get("name") or "").strip()
        parent = (cat.get("parent") or "").strip() if cat.get("parent") else None
        if parent:
            display_name = f"{parent} → {name}"
        else:
            display_name = f"{name} (no parent)"
        try:
            payload = {"category": name, "id": cat.get("id"), "parent": parent, "path": cat.get("path")}
            key = base_handlers._store_callback_payload(payload)
            cb = f"delete_category_{key}"
        except Exception:
            encoded_name = urllib.parse.quote_plus(name)
            cb = f"delete_category_{encoded_name}"
        keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    # Desired order: Next (left), Home (center when applicable), End, Previous (right-most)
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"delete_category_page::{page + 1}"))

    # Home (center) — show only when not on page 1 (appears starting page 2)
    if page > 1:
        nav.append(InlineKeyboardButton("🏠 Home", callback_data="delete_category_page::1"))

    if total_pages > 1 and page < last_page:
        nav.append(InlineKeyboardButton("🏁 End", callback_data=f"delete_category_page::{last_page}"))

    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"delete_category_page::{page - 1}"))

    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

    await base_handlers.safe_edit_message(
        query,
        "Choose a category to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        action_key=getattr(query, "data", None),
    )


async def delete_parent_start(update: Update, context: CallbackContext):
    """Show top-level parent categories for deletion with pagination.

    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    # Owner-only: restrict delete UI
    user_id = getattr(update.message.from_user, "id", None)
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Only the bot owner can run this command.")
        return

    db = await get_db()

    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        page = 1
        page_size = 20  # smaller page for delete UI
        start = (page - 1) * page_size
        total = await base_handlers.get_total_count(db, "categories", base_handlers.TOP_LEVEL_FILTER, ttl=15)
        cats = (
            await db["categories"]
            .find(base_handlers.TOP_LEVEL_FILTER, {"name": 1, "parent": 1})
            .sort("name", 1)
            .skip(start)
            .limit(page_size)
            .to_list(length=page_size)
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
                payload = {"category": name, "parent": parent}
                key = base_handlers._store_callback_payload(payload)
                cb = f"delete_summary::category::{key}"
            except Exception:
                encoded_name = urllib.parse.quote_plus(name)
                # Fallback: use underscore-style single-parameter callback so
                # the registered `handle_category_deletion` can handle it.
                cb = f"delete_category_{encoded_name}"

            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        # Build pagination nav BEFORE sending the message
        nav = []
        total_pages = max(1, (total - 1) // page_size + 1) if total else 1
        last_page = max(1, total_pages)
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"delete_parent_page::{page - 1}"))
        if page < last_page:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"delete_parent_page::{page + 1}"))
        if nav:
            keyboard.append(nav)

        # Add a single Cancel button at the end
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

        await update.message.reply_text(
            "Choose a parent category to delete:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception:
        logger.exception("Error listing parent categories for deletion")
        await update.message.reply_text("An error occurred. Please try again later.")


async def handle_delete_parent_page(update: Update, context: CallbackContext):
    """Handle pagination for the delete-parent flow (callback: delete_parent_page::<page>).

    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    query = update.callback_query
    await base_handlers.safe_answer(query)
    # Owner-only guard
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await base_handlers.safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    data = query.data
    try:
        page = int(data.split("::")[1])
    except Exception:
        page = 1

    db = await get_db()
    if db is None:
        await base_handlers.safe_edit_message(query, "Error: Unable to connect to the database.")
        return

    try:
        page_size = 20
        start = (page - 1) * page_size
        total = await base_handlers.get_total_count(db, "categories", base_handlers.TOP_LEVEL_FILTER, ttl=15)
        cats = (
            await db["categories"]
            .find(base_handlers.TOP_LEVEL_FILTER, {"name": 1, "parent": 1})
            .sort("name", 1)
            .skip(start)
            .limit(page_size)
            .to_list(length=page_size)
        )

        if not cats:
            await base_handlers.safe_edit_message(query, "No parent categories found on this page.", action_key=data)
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
                payload = {"category": name, "parent": parent}
                key = base_handlers._store_callback_payload(payload)
                cb = f"delete_summary::category::{key}"
            except Exception:
                encoded_name = urllib.parse.quote_plus(name)
                cb = f"delete_category_{encoded_name}"
            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

        nav = []
        total_pages = max(1, (total - 1) // page_size + 1) if total else 1
        last_page = max(1, total_pages)
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"delete_parent_page::{page - 1}"))
        if page < last_page:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"delete_parent_page::{page + 1}"))
        if nav:
            keyboard.append(nav)

        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_delete")])

        await base_handlers.safe_edit_message(
            query,
            f"Choose a parent category to delete (page {page}):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            action_key=data,
        )
    except Exception:
        logger.exception("Error handling delete parent page")


async def delete_all_data_start(update: Update, context: CallbackContext):
    """Start the delete-all-data confirmation conversation.

    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    # Owner-only: restrict destructive action to bot owner
    user_id = getattr(update.message.from_user, "id", None)
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Only the bot owner can run this command.")
        return None

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
    await base_handlers.safe_answer(query)  # Acknowledge the callback query

    # Owner-only guard
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await base_handlers.safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return ConversationHandler.END

    try:
        db = await get_db()  # Await the database connection
        if db is None:
            logger.error("Database connection failed for user %s.", user_id)
            await base_handlers.safe_edit_message(
                query,
                "Error: Unable to connect to the database. Please try again later.",
                action_key=getattr(query, "data", None),
            )
            return ConversationHandler.END

        # Perform the deletion of categories (courses embedded inside will be removed)
        result = await db["categories"].delete_many({})

        if result.deleted_count > 0:
            logger.info("All categories deleted successfully for user %s.", user_id)
            await base_handlers.safe_edit_message(
                query,
                "All categories and their embedded courses have been deleted. 😞",
                action_key=getattr(query, "data", None),
            )
        else:
            logger.warning("No categories found to delete for user %s.", user_id)
            await base_handlers.safe_edit_message(
                query,
                "No categories found to delete. 😞",
                action_key=getattr(query, "data", None),
            )
    except Exception:
        logger.exception("Error confirming delete all data for user %s", user_id)
        await base_handlers.safe_edit_message(
            query,
            "An error occurred while deleting all data. Please try again later.",
            action_key=getattr(query, "data", None),
        )

    return ConversationHandler.END


# Cancel deletion of all user data
async def cancel_delete_all_data(update: Update, context: CallbackContext) -> int:
    """Cancel the deletion of all user data."""
    await base_handlers.safe_answer(update.callback_query)
    await base_handlers.safe_edit_message(
        update.callback_query,
        "Deletion of all data has been canceled.",
        action_key=getattr(update.callback_query, "data", None),
    )
    return ConversationHandler.END
