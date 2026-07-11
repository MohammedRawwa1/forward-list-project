"""
Category Design Feature — Owner Only.

Allows the bot owner to assign a thumbnail photo as a visual "design"
for a parent category. When the category is viewed, the design photo
is sent as a banner before the inline keyboard.

Commands:
  /design_cat   — Reply to a photo, then pick a parent category to design
  /remove_design — Remove a design from a parent category
"""

import logging
import urllib.parse
import os
import math

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext, CommandHandler, CallbackQueryHandler
from handlers.db_connection import get_db
from typing import Optional
from handlers.base_handlers import (
    safe_edit_message,
    safe_answer,
    _get_total_count,
    TOP_LEVEL_FILTER,
    PAGE_SIZE,
)

logger = logging.getLogger(__name__)

DESIGNS_COLLECTION = "category_designs"


# ---------------  helpers  ---------------

async def _get_design(db, category_name: str) -> Optional[str]:
    """Return the file_id of the design for `category_name`, or None."""
    try:
        doc = await db[DESIGNS_COLLECTION].find_one({"name": category_name}, projection={"file_id": 1})
        return doc.get("file_id") if doc else None
    except Exception:
        return None


async def _set_design(db, category_name: str, file_id: str):
    """Store or update the design for `category_name`."""
    try:
        await db[DESIGNS_COLLECTION].update_one(
            {"name": category_name},
            {"$set": {"file_id": file_id}},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.exception("Error saving design for '%s': %s", category_name, e)
        return False


async def _delete_design(db, category_name: str):
    """Remove the design for `category_name`."""
    try:
        res = await db[DESIGNS_COLLECTION].delete_one({"name": category_name})
        return res.deleted_count > 0
    except Exception as e:
        logger.exception("Error deleting design for '%s': %s", category_name, e)
        return False


async def _list_designed_categories(db) -> list:
    """Return a list of category names that have designs."""
    try:
        docs = await db[DESIGNS_COLLECTION].find({}, projection={"name": 1}).sort("name", 1).to_list(length=500)
        return [d["name"] for d in docs if d.get("name")]
    except Exception:
        return []


async def get_category_design(db, category_name: str) -> Optional[str]:
    """Public helper — return the file_id for a category's design, or None."""
    return await _get_design(db, category_name)


# ---------------  owner guard  ---------------

def _is_owner(update: Update) -> bool:
    """Check if the requesting user is the configured bot owner."""
    try:
        owner_env = os.getenv("BOT_OWNER_ID")
        if not owner_env:
            return False
        owner_id = int(owner_env)
        user_id = update.effective_user.id if update.effective_user else None
        return user_id == owner_id
    except Exception:
        return False


# ---------------  /design_cat  ---------------

async def design_cat_command(update: Update, context: CallbackContext):
    """Start the category design flow — owner-only, reply to a photo."""
    if not _is_owner(update):
        await update.message.reply_text("Unauthorized (owner only).")
        return

    # Must be replying to a photo
    reply = update.message.reply_to_message
    if not reply or not reply.photo:
        await update.message.reply_text(
            "Please reply to a photo with this command.\n"
            "Example: send a photo, then reply to it with /design_cat"
        )
        return

    # Get the largest photo file_id (best quality)
    try:
        file_id = reply.photo[-1].file_id
    except (IndexError, AttributeError):
        await update.message.reply_text("Could not read the photo. Try again.")
        return

    # Store file_id temporarily
    context.user_data["design_file_id"] = file_id

    # Show parent category picker
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Cannot connect to database.")
        return

    try:
        page_size = PAGE_SIZE
        page = 1
        start = (page - 1) * page_size
        total = await _get_total_count(db, "categories", TOP_LEVEL_FILTER, ttl=30)
        parents = await db.categories.find(TOP_LEVEL_FILTER).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
    except Exception:
        parents = []
        total = 0

    if not parents:
        await update.message.reply_text("No parent categories found. Create one first with /create_category.")
        context.user_data.pop("design_file_id", None)
        return

    keyboard = []
    for p in parents:
        name = p.get("name", "")
        keyboard.append([InlineKeyboardButton(name, callback_data=f"design_cat_select::{urllib.parse.quote_plus(name)}")])

    # Pagination nav
    nav = []
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"design_cat_page::{page-1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"design_cat_page::{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Cancel", callback_data="design_cat_cancel")])

    await update.message.reply_text(
        "🎨 Choose a parent category to assign this design to:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def design_cat_select_callback(update: Update, context: CallbackContext):
    """Handle parent category selection for /design_cat."""
    query = update.callback_query
    await safe_answer(query)

    if not _is_owner(update):
        await safe_edit_message(query, "Unauthorized (owner only).", action_key=query.data)
        return

    data = query.data
    parts = data.split("::")
    if len(parts) < 2:
        await safe_edit_message(query, "Invalid callback.", action_key=data)
        return

    category_name = urllib.parse.unquote_plus(parts[1])
    file_id = context.user_data.pop("design_file_id", None)

    if not file_id:
        await safe_edit_message(
            query,
            "No photo found. Please use /design_cat while replying to a photo.",
            action_key=data,
        )
        return

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Cannot connect to database.", action_key=data)
        return

    success = await _set_design(db, category_name, file_id)
    if success:
        await safe_edit_message(
            query,
            f"✅ Design assigned to '{category_name}'! 🎨\n\nThe design photo will now appear when viewing this category.",
            action_key=data,
        )
    else:
        await safe_edit_message(
            query,
            f"❌ Failed to save design for '{category_name}'. Check logs.",
            action_key=data,
        )


async def design_cat_page_callback(update: Update, context: CallbackContext):
    """Handle pagination for the /design_cat category picker."""
    query = update.callback_query
    await safe_answer(query)

    if not _is_owner(update):
        await safe_edit_message(query, "Unauthorized (owner only).", action_key=query.data)
        return

    data = query.data
    try:
        page = int(data.split("::")[1])
    except Exception:
        page = 1

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Cannot connect to database.", action_key=data)
        return

    try:
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        total = await _get_total_count(db, "categories", TOP_LEVEL_FILTER, ttl=30)
        parents = await db.categories.find(TOP_LEVEL_FILTER).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
    except Exception:
        parents = []
        total = 0

    if not parents:
        await safe_edit_message(query, "No parent categories found on this page.", action_key=data)
        return

    keyboard = []
    for p in parents:
        name = p.get("name", "")
        keyboard.append([InlineKeyboardButton(name, callback_data=f"design_cat_select::{urllib.parse.quote_plus(name)}")])

    nav = []
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"design_cat_page::{page-1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"design_cat_page::{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("Cancel", callback_data="design_cat_cancel")])

    await safe_edit_message(
        query,
        f"🎨 Choose a parent category to assign this design to (page {page}):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        action_key=data,
    )


async def design_cat_cancel_callback(update: Update, context: CallbackContext):
    """Cancel the design flow."""
    query = update.callback_query
    await safe_answer(query)
    context.user_data.pop("design_file_id", None)
    await safe_edit_message(query, "Design assignment canceled.", action_key=query.data)


# ---------------  /remove_design  ---------------

async def remove_design_command(update: Update, context: CallbackContext):
    """Show categories with designs so the owner can remove one."""
    if not _is_owner(update):
        await update.message.reply_text("Unauthorized (owner only).")
        return

    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Cannot connect to database.")
        return

    designed = await _list_designed_categories(db)
    if not designed:
        await update.message.reply_text("No categories have designs assigned yet.")
        return

    keyboard = []
    for name in designed:
        keyboard.append([
            InlineKeyboardButton(f"🗑️ {name}", callback_data=f"remove_design::{urllib.parse.quote_plus(name)}")
        ])
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="design_cat_cancel")])

    await update.message.reply_text(
        "Choose a category to remove its design:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def remove_design_callback(update: Update, context: CallbackContext):
    """Remove a design from a selected category."""
    query = update.callback_query
    await safe_answer(query)

    if not _is_owner(update):
        await safe_edit_message(query, "Unauthorized (owner only).", action_key=query.data)
        return

    data = query.data
    parts = data.split("::")
    if len(parts) < 2:
        await safe_edit_message(query, "Invalid callback.", action_key=data)
        return

    category_name = urllib.parse.unquote_plus(parts[1])

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Cannot connect to database.", action_key=data)
        return

    success = await _delete_design(db, category_name)
    if success:
        await safe_edit_message(
            query,
            f"✅ Design removed from '{category_name}'.",
            action_key=data,
        )
    else:
        await safe_edit_message(
            query,
            f"No design found for '{category_name}' or removal failed.",
            action_key=data,
        )


# ---------------  registration  ---------------

def setup_design_handlers(application):
    """Register all category design handlers."""
    # Commands
    application.add_handler(CommandHandler("design_cat", design_cat_command))
    application.add_handler(CommandHandler("remove_design", remove_design_command))

    # Callbacks
    application.add_handler(CallbackQueryHandler(design_cat_select_callback, pattern=r"^design_cat_select::"))
    application.add_handler(CallbackQueryHandler(design_cat_page_callback, pattern=r"^design_cat_page::"))
    application.add_handler(CallbackQueryHandler(design_cat_cancel_callback, pattern=r"^design_cat_cancel$"))
    application.add_handler(CallbackQueryHandler(remove_design_callback, pattern=r"^remove_design::"))
