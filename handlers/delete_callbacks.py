from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
from database.mongo_handler import MongoDB
import re
import urllib.parse
import logging
from handlers.base_handlers import safe_edit_message, _resolve_callback_payload, safe_answer
import json
logger = logging.getLogger(__name__)

# ----------  delete category  ----------
async def handle_category_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    # everything after "delete_category_"
    cat = query.data.split("_", 2)[2]
    cat = urllib.parse.unquote_plus(cat)
    db = await MongoDB.get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return

    # Recursively collect this category and all descendants, then delete them.
    try:
        to_delete = set()
        stack = [cat]
        while stack:
            curr = stack.pop()
            if curr in to_delete:
                continue
            to_delete.add(curr)
            # Match child categories by explicit parent field or by path prefix
            children = await db['categories'].find({
                "$or": [
                    {"parent": curr},
                    {"path": {"$regex": f'^{re.escape(curr)}/'}}
                ]
            }).to_list(length=None)
            for ch in children:
                name = ch.get('name')
                if name and name not in to_delete:
                    stack.append(name)
        if to_delete:
            res = await db['categories'].delete_many({"name": {"$in": list(to_delete)}})
            await safe_edit_message(query, f"Deleted {getattr(res, 'deleted_count', 0)} categories (including '{cat}'). ✅", action_key=getattr(query, 'data', None))
        else:
            await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, 'data', None))
    except Exception as e:
        logger.exception("Error deleting category '%s': %s", cat, e)
        await safe_edit_message(query, "An error occurred while deleting the category.", action_key=getattr(query, 'data', None))

# ----------  delete single item  ----------
async def handle_item_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
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
    await safe_answer(query)
    data = query.data
    key = data.split("::", 1)[1] if "::" in data else data
    payload = await _resolve_callback_payload(key)
    if not payload:
        await safe_edit_message(query, "Reference expired. Please reopen the list and try again.", action_key=getattr(query, 'data', None))
        return
    # Show a confirmation menu offering actions: delete course, delete category, delete parent (if available)
    cat = payload.get('category')
    item = payload.get('name')
    try:
        db = await MongoDB.get_db()
    except Exception:
        db = None

    # Only offer deleting the single course from Details view. Other destructive
    # actions (category/parent) are available through separate admin commands.
    buttons = [InlineKeyboardButton("🗑️ Delete course", callback_data=f"delete_confirm::course::{key}"),
               InlineKeyboardButton("Cancel", callback_data=f"cancel_delete::{key}")]

    # Layout: two columns where sensible
    kb = []
    # place first two actions side-by-side if possible
    if len(buttons) >= 2:
        kb.append(buttons[0:2])
        for b in buttons[2:]:
            kb.append([b])
    else:
        for b in buttons:
            kb.append([b])

    await safe_edit_message(query, f"Delete options for '{item}' (category: {cat}):", reply_markup=InlineKeyboardMarkup(kb), action_key=getattr(query, 'data', None))


async def handle_delete_confirm(update: Update, context: CallbackContext):
    """Perform the confirmed delete action: course, category, or parent.

    Callback format: delete_confirm::{action}::{key}
    """
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    parts = data.split("::", 2)
    if len(parts) != 3:
        await safe_edit_message(query, "Invalid delete confirmation callback.", action_key=getattr(query, 'data', None))
        return
    _, action, key = parts
    payload = await _resolve_callback_payload(key)
    if not payload:
        await safe_edit_message(query, "Reference expired. Please reopen the list and try again.", action_key=getattr(query, 'data', None))
        return

    cat = payload.get('category')
    item = payload.get('name')

    try:
        db = await MongoDB.get_db()
        if db is None:
            await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
            return
    except Exception:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return

    try:
        if action == 'course':
            # remove single course from its category
            if not cat:
                await safe_edit_message(query, "Cannot determine course category. Aborting.", action_key=getattr(query, 'data', None))
                return
            res = await db['categories'].update_one({"name": cat}, {"$pull": {"courses": {"name": item}}})
            if res.modified_count:
                await safe_edit_message(query, f"Course '{item}' deleted from category '{cat}'. ✅", action_key=getattr(query, 'data', None))
            else:
                await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, 'data', None))
            return

        elif action == 'category':
            if not cat:
                await safe_edit_message(query, "Cannot determine category to delete. Aborting.", action_key=getattr(query, 'data', None))
                return
            # Recursively collect this category and all descendants, then delete them.
            try:
                to_delete = set()
                stack = [cat]
                while stack:
                    curr = stack.pop()
                    if curr in to_delete:
                        continue
                    to_delete.add(curr)
                    children = await db['categories'].find({
                        "$or": [
                            {"parent": curr},
                            {"path": {"$regex": f'^{re.escape(curr)}/'}}
                        ]
                    }).to_list(length=None)
                    for ch in children:
                        name = ch.get('name')
                        if name and name not in to_delete:
                            stack.append(name)
                if to_delete:
                    res = await db['categories'].delete_many({"name": {"$in": list(to_delete)}})
                    await safe_edit_message(query, f"Deleted {getattr(res, 'deleted_count', 0)} categories (including '{cat}'). ✅", action_key=getattr(query, 'data', None))
                else:
                    await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, 'data', None))
            except Exception as e:
                logger.exception("Error deleting category '%s': %s", cat, e)
                await safe_edit_message(query, "An error occurred while deleting the category.", action_key=getattr(query, 'data', None))
            return

        elif action == 'parent':
            if not cat:
                await safe_edit_message(query, "Cannot determine parent to delete. Aborting.", action_key=getattr(query, 'data', None))
                return
            # find parent of this category
            cat_doc = await db['categories'].find_one({"name": cat})
            parent_name = cat_doc.get('parent') if cat_doc else None
            if not parent_name:
                await safe_edit_message(query, "Parent not found. ❌", action_key=getattr(query, 'data', None))
                return
            # Recursively collect parent and all descendants, then delete them
            to_delete = set()
            stack = [parent_name]
            try:
                while stack:
                    curr = stack.pop()
                    if curr in to_delete:
                        continue
                    to_delete.add(curr)
                    # Match child categories by explicit parent field or by path prefix
                    children = await db['categories'].find({
                        "$or": [
                            {"parent": curr},
                            {"path": {"$regex": f'^{re.escape(curr)}/'}}
                        ]
                    }).to_list(length=None)
                    for ch in children:
                        name = ch.get('name')
                        if name and name not in to_delete:
                            stack.append(name)
                if to_delete:
                    res = await db['categories'].delete_many({"name": {"$in": list(to_delete)}})
                    await safe_edit_message(query, f"Parent '{parent_name}' and {res.deleted_count - 1 if getattr(res, 'deleted_count', 0) else 0} descendant categories deleted. ✅", action_key=getattr(query, 'data', None))
                else:
                    await safe_edit_message(query, "Nothing to delete. ❌", action_key=getattr(query, 'data', None))
            except Exception as e:
                logger.exception("Error during recursive parent deletion: %s", e)
                await safe_edit_message(query, "An error occurred while deleting parent and descendants.", action_key=getattr(query, 'data', None))
            return

        else:
            await safe_edit_message(query, "Unknown delete action.", action_key=getattr(query, 'data', None))
            return

    except Exception as e:
        logger.error("[DEL-CONFIRM] error performing delete: %s", e, exc_info=True)
        await safe_edit_message(query, "An error occurred while performing delete. Please try again later.", action_key=getattr(query, 'data', None))
        return


async def handle_delete_summary(update: Update, context: CallbackContext):
    """Show a pre-delete summary (counts of categories and courses) before confirming.

    Callback format: delete_summary::{action}::{key}
    action: 'category' or 'parent'
    """
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    parts = data.split("::", 2)
    if len(parts) != 3:
        await safe_edit_message(query, "Invalid delete summary callback.", action_key=getattr(query, 'data', None))
        return
    _, action, key = parts
    payload = await _resolve_callback_payload(key)
    if not payload:
        await safe_edit_message(query, "Reference expired. Please reopen the list and try again.", action_key=getattr(query, 'data', None))
        return

    cat = payload.get('category')
    try:
        db = await MongoDB.get_db()
    except Exception:
        db = None

    if action == 'category':
        if not cat:
            await safe_edit_message(query, "Cannot determine category to summarize. Aborting.", action_key=getattr(query, 'data', None))
            return
        # collect category + descendants
        try:
            to_delete = set()
            stack = [cat]
            while stack:
                curr = stack.pop()
                if curr in to_delete:
                    continue
                to_delete.add(curr)
                children = await db['categories'].find({
                    "$or": [
                        {"parent": curr},
                        {"path": {"$regex": f'^{re.escape(curr)}/'}}
                    ]
                }).to_list(length=None)
                for ch in children:
                    name = ch.get('name')
                    if name and name not in to_delete:
                        stack.append(name)

            # count categories and courses
            cat_count = len(to_delete)
            course_count = 0
            for name in to_delete:
                doc = await db['categories'].find_one({"name": name})
                if doc:
                    course_count += len(doc.get('courses', []))

            # Prepare preview of affected category names (truncate to first 10)
            preview_limit = 10
            entries = []
            for n in to_delete:
                try:
                    doc = await db['categories'].find_one({"name": n})
                    cnt = len(doc.get('courses', [])) if doc else 0
                except Exception:
                    cnt = 0
                entries.append((n, cnt))
            # Sort by course count ascending, then name A→Z
            entries_sorted = sorted(entries, key=lambda x: (x[1], x[0].lower()))
            preview_entries = entries_sorted[:preview_limit]
            remaining = max(0, len(entries_sorted) - len(preview_entries))
            preview_lines = "\n".join(f"- {name} ({cnt} course{'s' if cnt!=1 else ''})" for name, cnt in preview_entries) if preview_entries else "(none)"

            msg = (
                f"You are about to delete category '{cat}' and {cat_count - 1 if cat_count>0 else 0} descendant categories,\n"
                f"removing {course_count} course(s) in total.\n\n"
                f"Affected categories (showing {len(preview_entries)}):\n{preview_lines}"
                + (f"\n... and {remaining} more" if remaining else "")
                + "\n\nProceed?"
            )

            kb = [
                [InlineKeyboardButton("Yes, delete", callback_data=f"delete_confirm::category::{key}")],
                [InlineKeyboardButton("Cancel", callback_data=f"cancel_delete::{key}")],
            ]
            await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(kb), action_key=getattr(query, 'data', None))
            return
        except Exception as e:
            logger.exception("Error building category delete summary: %s", e)
            await safe_edit_message(query, "Failed to prepare delete summary. Try again.", action_key=getattr(query, 'data', None))
            return

    if action == 'parent':
        if not cat:
            await safe_edit_message(query, "Cannot determine parent to summarize. Aborting.", action_key=getattr(query, 'data', None))
            return
        try:
            cat_doc = await db['categories'].find_one({"name": cat})
            parent_name = cat_doc.get('parent') if cat_doc else None
            if not parent_name:
                await safe_edit_message(query, "Parent not found. ❌", action_key=getattr(query, 'data', None))
                return

            to_delete = set()
            stack = [parent_name]
            while stack:
                curr = stack.pop()
                if curr in to_delete:
                    continue
                to_delete.add(curr)
                children = await db['categories'].find({
                    "$or": [
                        {"parent": curr},
                        {"path": {"$regex": f'^{re.escape(curr)}/'}}
                    ]
                }).to_list(length=None)
                for ch in children:
                    name = ch.get('name')
                    if name and name not in to_delete:
                        stack.append(name)

            cat_count = len(to_delete)
            course_count = 0
            for name in to_delete:
                doc = await db['categories'].find_one({"name": name})
                if doc:
                    course_count += len(doc.get('courses', []))

            # Prepare preview of affected category names (truncate to first 10)
            preview_limit = 10
            entries = []
            for n in to_delete:
                try:
                    doc = await db['categories'].find_one({"name": n})
                    cnt = len(doc.get('courses', [])) if doc else 0
                except Exception:
                    cnt = 0
                entries.append((n, cnt))
            # Sort by course count ascending, then name A→Z
            entries_sorted = sorted(entries, key=lambda x: (x[1], x[0].lower()))
            preview_entries = entries_sorted[:preview_limit]
            remaining = max(0, len(entries_sorted) - len(preview_entries))
            preview_lines = "\n".join(f"- {name} ({cnt} course{'s' if cnt!=1 else ''})" for name, cnt in preview_entries) if preview_entries else "(none)"

            msg = (
                f"You are about to delete parent '{parent_name}' and {cat_count - 1 if cat_count>0 else 0} descendant categories,\n"
                f"removing {course_count} course(s) in total.\n\n"
                f"Affected categories (showing {len(preview_entries)}):\n{preview_lines}"
                + (f"\n... and {remaining} more" if remaining else "")
                + "\n\nProceed?"
            )

            kb = [
                [InlineKeyboardButton("Yes, delete", callback_data=f"delete_confirm::parent::{key}")],
                [InlineKeyboardButton("Cancel", callback_data=f"cancel_delete::{key}")],
            ]
            await safe_edit_message(query, msg, reply_markup=InlineKeyboardMarkup(kb), action_key=getattr(query, 'data', None))
            return
        except Exception as e:
            logger.exception("Error building parent delete summary: %s", e)
            await safe_edit_message(query, "Failed to prepare delete summary. Try again.", action_key=getattr(query, 'data', None))
            return

    await safe_edit_message(query, "Unknown summary action.", action_key=getattr(query, 'data', None))
