from telegram import Update
from telegram.ext import CallbackContext
from database.mongo_handler import MongoDB
import logging
logger = logging.getLogger(__name__)

# ----------  delete category  ----------
async def handle_category_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    cat = query.data.split("_", 2)[2]          # everything after "delete_category_"
    db = await MongoDB.get_db()
    res = await db['categories'].delete_one({"name": cat})
    await db['courses'].delete_many({"category": cat})   # cascade
    if res.deleted_count:
        await query.edit_message_text(f"Category ‘{cat}’ and all its courses deleted. ✅")
    else:
        await query.edit_message_text("Category not found. ❌")

# ----------  delete single item  ----------
async def handle_item_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    item = query.data.split("_", 2)[2]         # everything after "delete_item_"
    db = await MongoDB.get_db()
    res = await db['courses'].delete_one({"name": item})
    if res.deleted_count:
        await query.edit_message_text(f"Course ‘{item}’ deleted. ✅")
    else:
        await query.edit_message_text("Course not found. ❌")
