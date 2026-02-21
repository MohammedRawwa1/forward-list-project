from telegram import Update
from telegram.ext import CallbackContext
from database.mongo_handler import MongoDB
import urllib.parse
import logging
from handlers.base_handlers import safe_edit_message, _resolve_callback_payload
import json
logger = logging.getLogger(__name__)

# ----------  delete category  ----------
async def handle_category_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    # everything after "delete_category_"
    cat = query.data.split("_", 2)[2]
    cat = urllib.parse.unquote_plus(cat)
    db = await MongoDB.get_db()
    res = await db['categories'].delete_one({"name": cat})
    # courses are embedded in `categories` so deleting the category removes them
    if res.deleted_count:
        await safe_edit_message(query, f"Category ‘{cat}’ and all its courses deleted. ✅", action_key=getattr(query, 'data', None))
    else:
        await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, 'data', None))

# ----------  delete single item  ----------
async def handle_item_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    logger.info("[DEL-ITEM] callback data=%s", query.data)
    # Support new format: delete_item::category::course or legacy delete_item_{course}
    data = query.data
    db = await MongoDB.get_db()

    if data.startswith("delete_item::"):
        payload = data.replace("delete_item::", "", 1)
        parts = payload.split("::", 1)
        if len(parts) == 2:
            cat = urllib.parse.unquote_plus(parts[0])
            item = urllib.parse.unquote_plus(parts[1])
            # remove from category embedded array
            res = await db['categories'].update_one({"name": cat}, {"$pull": {"courses": {"name": item}}})
            if res.modified_count:
                await safe_edit_message(query, f"Course ‘{item}’ deleted from category ‘{cat}’. ✅", action_key=getattr(query, 'data', None))
                return
            else:
                await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, 'data', None))
                return

    # legacy underscore-style fallback: pull from any category that contains the course
    item = data.split("_", 2)[2] if "_" in data else data
    item = urllib.parse.unquote_plus(item)
    res = await db['categories'].update_one({"courses.name": item}, {"$pull": {"courses": {"name": item}}})
    if res.modified_count:
        await safe_edit_message(query, f"Course ‘{item}’ deleted. ✅", action_key=getattr(query, 'data', None))
    else:
        await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, 'data', None))


async def handle_delete_ref(update: Update, context: CallbackContext):
    """Handle delete_ref::<key> callbacks by resolving the payload from CALLBACK_MAP."""
    query = update.callback_query
    await query.answer()
    data = query.data
    key = data.split("::", 1)[1] if "::" in data else data
    payload = await _resolve_callback_payload(key)
    if not payload:
        await safe_edit_message(query, "Reference expired. Please reopen the list and try again.", action_key=getattr(query, 'data', None))
        return

    cat = payload.get('category')
    item = payload.get('name')
    # attempt to remove
    try:
        db = await MongoDB.get_db()
        if db is None:
            await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
            return
        res = await db['categories'].update_one({"name": cat}, {"$pull": {"courses": {"name": item}}})
    except Exception as e:
        logger.error("[DEL-REF] DB error: %s", e, exc_info=True)
        await safe_edit_message(query, "An error occurred while deleting the course. Please try again later.", action_key=getattr(query, 'data', None))
        return
    if res.modified_count:
        await safe_edit_message(query, f"Course ‘{item}’ deleted from category ‘{cat}’. ✅", action_key=getattr(query, 'data', None))
    else:
        await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, 'data', None))
