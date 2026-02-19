from telegram import Update
from telegram.ext import CallbackContext
from database.mongo_handler import MongoDB
import urllib.parse
import logging
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
        await query.edit_message_text(f"Category ‘{cat}’ and all its courses deleted. ✅")
    else:
        await query.edit_message_text("Category not found. ❌")

# ----------  delete single item  ----------
async def handle_item_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
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
                await query.edit_message_text(f"Course ‘{item}’ deleted from category ‘{cat}’. ✅")
                return
            else:
                await query.edit_message_text("Course not found. ❌")
                return

    # legacy underscore-style fallback: pull from any category that contains the course
    item = data.split("_", 2)[2] if "_" in data else data
    item = urllib.parse.unquote_plus(item)
    res = await db['categories'].update_one({"courses.name": item}, {"$pull": {"courses": {"name": item}}})
    if res.modified_count:
        await query.edit_message_text(f"Course ‘{item}’ deleted. ✅")
    else:
        await query.edit_message_text("Course not found. ❌")
