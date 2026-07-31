import logging
import re
import urllib.parse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from config import is_owner
from database.mongo_handler import MongoDB
from handlers.base_handlers import (
    _resolve_callback_payload,
    _resolve_callback_ref_key,
    collect_subtree_names,
    is_uuid,
    safe_answer,
    safe_edit_message,
)

logger = logging.getLogger(__name__)
# Batch limit to avoid loading very large result sets into memory
BATCH_LIMIT = 500


# ----------  delete category  ----------
async def handle_category_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    # Owner-only guard for category deletion (fail-closed)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    # everything after "delete_category_"
    cat_parts = query.data.split("_", 2)
    if len(cat_parts) < 3:
        await safe_edit_message(query, "Invalid category deletion callback.", action_key=getattr(query, "data", None))
        return
    cat = urllib.parse.unquote_plus(cat_parts[2])
    db = await MongoDB.get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return

    # Recursively collect this category and all descendants, then delete them.
    try:
        # Prefer id/path-based resolution to avoid removing same-named categories
        # in unrelated parts of the tree. Fall back to name-based deletion only
        # when path/id information cannot be resolved.
        payload = None
        try:
            # Use the central helper to check whether `cat` is a stored callback ref
            # (16-char hex key) or a real category name. This prevents a false-positive
            # edge case where a category named with 16 hex characters would be
            # incorrectly resolved as a CALLBACK_MAP key.
            payload = await _resolve_callback_ref_key(db, cat)
        except Exception:
            payload = None

        cat_doc = None
        if payload and isinstance(payload, dict) and payload.get("category"):
            # If payload supplies an explicit category id, use it
            try:
                cat_id = payload.get("category_id") or payload.get("id")
                if cat_id:
                    cat_doc = await db["categories"].find_one(
                        {"id": cat_id},
                        projection={"path": 1, "id": 1, "name": 1},
                    )
            except Exception:
                cat_doc = None

        # If no payload or id-based resolve, try to find by path or name as before
        if not cat_doc:
            cat_doc = await db["categories"].find_one(
                {"$or": [{"path": cat}, {"name": cat}]},
                projection={"path": 1, "id": 1, "name": 1},
            )

        if cat_doc and cat_doc.get("path"):
            base_path = cat_doc.get("path")
            # find all documents whose path equals the base_path or starts with base_path + '/'
            docs = (
                await db["categories"]
                .find(
                    {"$or": [{"path": base_path}, {"path": {"$regex": f"^{re.escape(base_path)}/"}}]},
                    {"_id": 1, "id": 1, "name": 1, "path": 1},
                )
                .to_list(length=BATCH_LIMIT)
            )
            if docs:
                ids = [d.get("_id") for d in docs if d.get("_id")]
                try:
                    res = await db["categories"].delete_many({"_id": {"$in": ids}})
                    label = f" (id: {cat_doc.get('id')})" if cat_doc and cat_doc.get("id") else ""
                    await safe_edit_message(
                        query,
                        f"Deleted {getattr(res, 'deleted_count', 0)} categories (including '{cat}'){label}. ✅",
                        action_key=getattr(query, "data", None),
                    )
                except Exception:
                    logger.exception("Error deleting by _id list")
                    await safe_edit_message(
                        query,
                        "Failed to delete categories. ❌",
                        action_key=getattr(query, "data", None),
                    )
            else:
                await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, "data", None))
        else:
            # Fallback: no path available — preserve existing behavior but
            # limit the recursive discovery to explicit parent links and
            # delete only the discovered subtree names.
            to_delete = await collect_subtree_names(
                db,
                cat,
                batch_limit=BATCH_LIMIT,
            )
            if to_delete:
                # Collect _ids by fetching docs and filtering by subtree relationship.
                # This avoids deleting unrelated categories that share names (name-based over-match).
                # Use a single $in query to avoid N+1 round-trips.
                all_docs = (
                    await db["categories"]
                    .find({"name": {"$in": list(to_delete)}}, {"_id": 1, "parent": 1})
                    .to_list(length=len(to_delete) * BATCH_LIMIT)
                )
                ids_to_remove = set()
                for d in all_docs:
                    parent = d.get("parent")
                    # Only include if this doc is the root category or its parent is in the subtree
                    if d.get("name") == cat or parent in to_delete:
                        _id = d.get("_id")
                        if _id:
                            ids_to_remove.add(_id)
                if ids_to_remove:
                    res = await db["categories"].delete_many({"_id": {"$in": list(ids_to_remove)}})
                    await safe_edit_message(
                        query,
                        f"Deleted {getattr(res, 'deleted_count', 0)} categories (including '{cat}'). ✅",
                        action_key=getattr(query, "data", None),
                    )
                else:
                    await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, "data", None))
            else:
                await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, "data", None))
    except Exception:
        logger.exception("Error deleting category '%s'", cat)
        await safe_edit_message(
            query,
            "An error occurred while deleting the category.",
            action_key=getattr(query, "data", None),
        )


# ----------  delete course from details view  ----------
async def handle_delete_ref(update: Update, context: CallbackContext):
    """Delete a course from the Details view (callback: delete_ref::<key>).

    Resolves the stored payload (which carries the course ``id`` and its
    category) and removes the course by embedded ``id`` when available,
    falling back to name-based deletion for legacy entries.
    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    query = update.callback_query
    await safe_answer(query)
    # Owner-only guard (fail-closed)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    data = query.data
    if not data.startswith("delete_ref::"):
        await safe_edit_message(query, "Invalid delete callback.", action_key=getattr(query, "data", None))
        return
    key = data.split("::", 1)[1]
    payload = await _resolve_callback_payload(key)
    if not payload:
        await safe_edit_message(
            query,
            "Reference expired. Please reopen the list and try again.",
            action_key=getattr(query, "data", None),
        )
        return
    cat = payload.get("category")
    item = payload.get("name")
    course_id = payload.get("id")
    try:
        db = await MongoDB.get_db()
        if db is None:
            await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=data)
            return
        if course_id:
            res = await db["categories"].update_one(
                {"courses.id": course_id},
                {"$pull": {"courses": {"id": course_id}}},
            )
        elif cat and item:
            res = await db["categories"].update_one(
                {"name": cat},
                {"$pull": {"courses": {"name": item}}},
            )
        else:
            await safe_edit_message(query, "Cannot determine course to delete.", action_key=data)
            return
        if getattr(res, "modified_count", 0):
            await safe_edit_message(
                query,
                f"Course '\u2018{item}\u2019 deleted from category '\u2018{cat}\u2019. \u2705",
                action_key=data,
            )
        else:
            await safe_edit_message(query, "Course not found. \u274c", action_key=data)
    except Exception:
        logger.exception("Error deleting course via delete_ref")
        await safe_edit_message(query, "An error occurred while deleting the course.", action_key=data)


# ----------  delete single item  ----------
async def handle_item_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    # Owner-only guard for item deletion (fail-closed)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    logger.info("[DEL-ITEM] callback data=%s", query.data)
    # Support new format: delete_item::category::course or legacy delete_item_{course}
    data = query.data
    db = await MongoDB.get_db()

    if data.startswith("delete_item_ref::"):
        # Stored payload reference (preferred) — resolves to exact category id
        key = data.split("::", 1)[1]
        payload = await _resolve_callback_payload(key)
        if not payload:
            await safe_edit_message(
                query,
                "Reference expired. Please reopen the list and try again.",
                action_key=getattr(query, "data", None),
            )
            return
        cat = payload.get("category")
        item = payload.get("name")
        cat_id = payload.get("category_id")
        if cat_id:
            res = await db["categories"].update_one({"id": cat_id}, {"$pull": {"courses": {"name": item}}})
        else:
            res = await db["categories"].update_one({"name": cat}, {"$pull": {"courses": {"name": item}}})
        if res.modified_count:
            await safe_edit_message(
                query,
                f"Course ‘{item}’ deleted from category ‘{cat}’. ✅",
                action_key=getattr(query, "data", None),
            )
            return
        await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, "data", None))
        return

    if data.startswith("delete_item::"):
        payload = data.replace("delete_item::", "", 1)
        parts = payload.split("::", 1)
        if len(parts) == 2:
            cat_raw = urllib.parse.unquote_plus(parts[0])
            item = urllib.parse.unquote_plus(parts[1])
            # If the category token looks like a UUID, prefer id-based deletion
            if is_uuid(cat_raw):
                res = await db["categories"].update_one({"id": cat_raw}, {"$pull": {"courses": {"name": item}}})
            else:
                res = await db["categories"].update_one({"name": cat_raw}, {"$pull": {"courses": {"name": item}}})
            if res.modified_count:
                await safe_edit_message(
                    query,
                    f"Course ‘{item}’ deleted from category ‘{cat_raw}’. ✅",
                    action_key=getattr(query, "data", None),
                )
                return
            await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, "data", None))
            return

    # legacy underscore-style fallback: pull from any category that contains the course
    item = data.split("_", 2)[2] if "_" in data else data
    item = urllib.parse.unquote_plus(item)
    res = await db["categories"].update_one({"courses.name": item}, {"$pull": {"courses": {"name": item}}})
    if res.modified_count:
        await safe_edit_message(query, f"Course ‘{item}’ deleted. ✅", action_key=getattr(query, "data", None))
    else:
        await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, "data", None))


async def handle_delete_confirm(update: Update, context: CallbackContext):
    """Perform the confirmed delete action: course, category, or parent.

    Callback format: delete_confirm::{action}::{key}
    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    query = update.callback_query
    await safe_answer(query)
    # Owner-only guard for confirmed deletes (fail-closed)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    data = query.data
    parts = data.split("::", 2)
    if len(parts) != 3:
        await safe_edit_message(query, "Invalid delete confirmation callback.", action_key=getattr(query, "data", None))
        return
    _, action, key = parts
    payload = await _resolve_callback_payload(key)
    if not payload:
        await safe_edit_message(
            query,
            "Reference expired. Please reopen the list and try again.",
            action_key=getattr(query, "data", None),
        )
        return

    cat = payload.get("category")
    item = payload.get("name")

    try:
        db = await MongoDB.get_db()
        if db is None:
            await safe_edit_message(
                query,
                "Error: Unable to connect to the database.",
                action_key=getattr(query, "data", None),
            )
            return
    except Exception:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return

    try:
        if action == "course":
            # remove single course from its category; prefer id-based deletion
            if not cat:
                await safe_edit_message(
                    query,
                    "Cannot determine course category. Aborting.",
                    action_key=getattr(query, "data", None),
                )
                return
            course_id = payload.get("id")
            if course_id:
                # delete by embedded course id when available
                res = await db["categories"].update_one(
                    {"courses.id": course_id},
                    {"$pull": {"courses": {"id": course_id}}},
                )
            else:
                # fallback to name-based deletion for legacy entries
                res = await db["categories"].update_one({"name": cat}, {"$pull": {"courses": {"name": item}}})

            if res.modified_count:
                # After deletion, show the updated courses list for the category
                try:
                    # Re-fetch the category document to get the updated courses
                    cat_doc = await db["categories"].find_one({"name": cat})
                    courses = cat_doc.get("courses", []) if cat_doc else []
                    # Normalize to list of dicts with name/link/category for the page builder
                    all_courses = [{"name": c.get("name"), "link": c.get("link"), "category": cat} for c in courses]
                    # Ensure deterministic ordering
                    all_courses = sorted(all_courses, key=lambda c: (c.get("name") or "").lower())
                    # Import build_courses_page from handlers.base_handlers to render
                    from handlers.base_handlers import build_courses_page

                    # origin_page from payload may be present; default to 1
                    try:
                        page = int(payload.get("origin_page", 1))
                    except Exception:
                        page = 1
                    # Clamp page to available range
                    page_size = 20
                    try:
                        from handlers.base_handlers import PAGE_SIZE

                        page_size = PAGE_SIZE
                    except Exception:
                        pass
                    total_pages = max(1, (len(all_courses) - 1) // page_size + 1) if all_courses else 1
                    page = min(page, total_pages)

                    text, reply_markup = build_courses_page(
                        all_courses,
                        page=page,
                        origin_type="category",
                        category=cat,
                        origin_context=payload.get("origin_context"),
                        origin_context_page=payload.get("origin_context_page"),
                    )
                    if text and reply_markup:
                        await safe_edit_message(
                            query,
                            text,
                            reply_markup=reply_markup,
                            action_key=getattr(query, "data", None),
                        )
                    else:
                        await safe_edit_message(
                            query,
                            f"Course '{item}' deleted from category '{cat}'. ✅\n\nNo courses remain in this category.",
                            action_key=getattr(query, "data", None),
                        )
                except Exception:
                    logger.exception("Error while rendering updated courses after delete")
                    await safe_edit_message(
                        query,
                        f"Course '{item}' deleted from category '{cat}'. ✅",
                        action_key=getattr(query, "data", None),
                    )
            else:
                await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, "data", None))
            return

        if action == "category":
            if not cat:
                await safe_edit_message(
                    query,
                    "Cannot determine category to delete. Aborting.",
                    action_key=getattr(query, "data", None),
                )
                return
            # Recursively collect this category and all descendants, then delete them.
            try:
                to_delete = await collect_subtree_names(
                    db,
                    cat,
                    batch_limit=BATCH_LIMIT,
                )
                if to_delete:
                    # Delete by _id to avoid over-deleting unrelated categories with the same name
                    # Use a single $in query to avoid N+1 round-trips.
                    all_docs = (
                        await db["categories"]
                        .find({"name": {"$in": list(to_delete)}}, {"_id": 1, "parent": 1})
                        .to_list(length=len(to_delete) * BATCH_LIMIT)
                    )
                    ids_to_remove = set()
                    for d in all_docs:
                        parent = d.get("parent")
                        if d.get("name") == cat or parent in to_delete:
                            _id = d.get("_id")
                            if _id:
                                ids_to_remove.add(_id)
                    if ids_to_remove:
                        res = await db["categories"].delete_many({"_id": {"$in": list(ids_to_remove)}})
                        await safe_edit_message(
                            query,
                            f"Deleted {getattr(res, 'deleted_count', 0)} categories (including '{cat}'). ✅",
                            action_key=getattr(query, "data", None),
                        )
                    else:
                        await safe_edit_message(
                            query,
                            "Category not found. ❌",
                            action_key=getattr(query, "data", None),
                        )
                else:
                    await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, "data", None))
            except Exception:
                logger.exception("Error deleting category '%s'", cat)
                await safe_edit_message(
                    query,
                    "An error occurred while deleting the category.",
                    action_key=getattr(query, "data", None),
                )
            return

        if action == "parent":
            if not cat:
                await safe_edit_message(
                    query,
                    "Cannot determine parent to delete. Aborting.",
                    action_key=getattr(query, "data", None),
                )
                return
            # find parent of this category
            cat_doc = await db["categories"].find_one({"name": cat})
            parent_name = cat_doc.get("parent") if cat_doc else None
            if not parent_name:
                await safe_edit_message(query, "Parent not found. ❌", action_key=getattr(query, "data", None))
                return
            # Recursively collect parent and all descendants, then delete them
            try:
                to_delete = await collect_subtree_names(
                    db,
                    parent_name,
                    batch_limit=BATCH_LIMIT,
                )
                if to_delete:
                    # Delete by _id to avoid over-deleting unrelated categories with the same name
                    # Use a single $in query to avoid N+1 round-trips.
                    all_docs = (
                        await db["categories"]
                        .find({"name": {"$in": list(to_delete)}}, {"_id": 1, "parent": 1})
                        .to_list(length=len(to_delete) * BATCH_LIMIT)
                    )
                    ids_to_remove = set()
                    for d in all_docs:
                        parent = d.get("parent")
                        if d.get("name") == parent_name or parent in to_delete:
                            _id = d.get("_id")
                            if _id:
                                ids_to_remove.add(_id)
                    if ids_to_remove:
                        res = await db["categories"].delete_many({"_id": {"$in": list(ids_to_remove)}})
                        deleted_count = getattr(res, "deleted_count", 0)
                        await safe_edit_message(
                            query,
                            f"Parent '{parent_name}' and {deleted_count - 1 if deleted_count else 0} descendant categories deleted. ✅",
                            action_key=getattr(query, "data", None),
                        )
                    else:
                        await safe_edit_message(query, "Nothing to delete. ❌", action_key=getattr(query, "data", None))
                else:
                    await safe_edit_message(query, "Nothing to delete. ❌", action_key=getattr(query, "data", None))
            except Exception:
                logger.exception("Error during recursive parent deletion")
                await safe_edit_message(
                    query,
                    "An error occurred while deleting parent and descendants.",
                    action_key=getattr(query, "data", None),
                )
            return

        await safe_edit_message(query, "Unknown delete action.", action_key=getattr(query, "data", None))
        return

    except Exception:
        logger.exception("[DEL-CONFIRM] error performing delete")
        await safe_edit_message(
            query,
            "An error occurred while performing delete. Please try again later.",
            action_key=getattr(query, "data", None),
        )
        return


async def handle_delete_summary(update: Update, context: CallbackContext):
    """Show a pre-delete summary (counts of categories and courses) before confirming.

    Callback format: delete_summary::{action}::{key}
    action: 'category' or 'parent'
    SECURITY: Owner-only — uses fail-closed is_owner() helper.
    """
    query = update.callback_query
    await safe_answer(query)
    # Owner-only guard for delete summary (fail-closed)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    data = query.data
    parts = data.split("::", 2)
    if len(parts) != 3:
        await safe_edit_message(query, "Invalid delete summary callback.", action_key=getattr(query, "data", None))
        return
    _, action, key = parts
    payload = await _resolve_callback_payload(key)
    if not payload:
        await safe_edit_message(
            query,
            "Reference expired. Please reopen the list and try again.",
            action_key=getattr(query, "data", None),
        )
        return

    cat = payload.get("category")
    try:
        db = await MongoDB.get_db()
    except Exception:
        db = None

    if action == "category":
        if not cat:
            await safe_edit_message(
                query,
                "Cannot determine category to summarize. Aborting.",
                action_key=getattr(query, "data", None),
            )
            return
        try:
            # Fast-path: if the selected category has no courses and no child categories,
            # show a quick confirm message without scanning the whole subtree.
            cat_doc = await db["categories"].find_one({"name": cat}, projection={"courses": 1})
            if cat_doc is None:
                await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, "data", None))
                return

            has_courses = bool(cat_doc.get("courses"))
            # Check for any child categories (either explicit parent or path prefix).
            child_exists = await db["categories"].find_one(
                {"$or": [{"parent": cat}, {"path": {"$regex": f"^{re.escape(cat)}/"}}]},
                projection={"_id": 1},
            )

            if not has_courses and not child_exists:
                msg = f"Category '{cat}' is empty. Delete it?"
                kb = [
                    [InlineKeyboardButton("Yes, delete", callback_data=f"delete_confirm::category::{key}")],
                    [InlineKeyboardButton("Cancel", callback_data=f"cancel_delete::{key}")],
                ]
                await safe_edit_message(
                    query,
                    msg,
                    reply_markup=InlineKeyboardMarkup(kb),
                    action_key=getattr(query, "data", None),
                )
                return

            # Otherwise, fall back to existing behavior: collect category + descendants
            to_delete = await collect_subtree_names(
                db,
                cat,
                batch_limit=BATCH_LIMIT,
            )

            # count categories and courses (fetch docs once for efficiency)
            cat_count = len(to_delete)
            # fetch exactly the number of docs we expect to summarize (project minimal fields)
            docs = (
                await db["categories"]
                .find({"name": {"$in": list(to_delete)}}, projection={"name": 1, "courses": 1})
                .to_list(length=cat_count)
            )
            doc_map = {d.get("name"): d for d in docs}
            course_count = sum(len(d.get("courses", [])) for d in docs)

            # Prepare preview of affected category names (truncate to first 10)
            preview_limit = 10
            entries = []
            for n in to_delete:
                try:
                    cnt = len(doc_map.get(n, {}).get("courses", []))
                except Exception:
                    cnt = 0
                entries.append((n, cnt))
            # Sort by course count ascending, then name A→Z
            entries_sorted = sorted(entries, key=lambda x: (x[1], x[0].lower()))
            preview_entries = entries_sorted[:preview_limit]
            remaining = max(0, len(entries_sorted) - len(preview_entries))
            preview_lines = (
                "\n".join(f"- {name} ({cnt} course{'s' if cnt != 1 else ''})" for name, cnt in preview_entries)
                if preview_entries
                else "(none)"
            )

            msg = (
                f"You are about to delete category '{cat}' and {cat_count - 1 if cat_count > 0 else 0} descendant categories,\n"
                f"removing {course_count} course(s) in total.\n\n"
                f"Affected categories (showing {len(preview_entries)}):\n{preview_lines}"
                + (f"\n... and {remaining} more" if remaining else "")
                + "\n\nProceed?"
            )

            kb = [
                [InlineKeyboardButton("Yes, delete", callback_data=f"delete_confirm::category::{key}")],
                [InlineKeyboardButton("Cancel", callback_data=f"cancel_delete::{key}")],
            ]
            await safe_edit_message(
                query,
                msg,
                reply_markup=InlineKeyboardMarkup(kb),
                action_key=getattr(query, "data", None),
            )
            return
        except Exception:
            logger.exception("Error building category delete summary")
            await safe_edit_message(
                query,
                "Failed to prepare delete summary. Try again.",
                action_key=getattr(query, "data", None),
            )
            return

    if action == "parent":
        if not cat:
            await safe_edit_message(
                query,
                "Cannot determine parent to summarize. Aborting.",
                action_key=getattr(query, "data", None),
            )
            return
        try:
            cat_doc = await db["categories"].find_one({"name": cat})
            parent_name = cat_doc.get("parent") if cat_doc else None
            if not parent_name:
                await safe_edit_message(query, "Parent not found. ❌", action_key=getattr(query, "data", None))
                return

            to_delete = await collect_subtree_names(
                db,
                parent_name,
                batch_limit=BATCH_LIMIT,
            )

            cat_count = len(to_delete)
            # Bulk-fetch documents for all affected categories to avoid N database calls
            docs = (
                await db["categories"]
                .find({"name": {"$in": list(to_delete)}}, projection={"name": 1, "courses": 1})
                .to_list(length=cat_count)
            )
            doc_map = {d.get("name"): d for d in docs}
            course_count = sum(len(d.get("courses", [])) for d in docs)

            # Prepare preview of affected category names (truncate to first 10)
            preview_limit = 10
            entries = []
            for n in to_delete:
                try:
                    cnt = len(doc_map.get(n, {}).get("courses", []))
                except Exception:
                    cnt = 0
                entries.append((n, cnt))
            # Sort by course count ascending, then name A→Z
            entries_sorted = sorted(entries, key=lambda x: (x[1], x[0].lower()))
            preview_entries = entries_sorted[:preview_limit]
            remaining = max(0, len(entries_sorted) - len(preview_entries))
            preview_lines = (
                "\n".join(f"- {name} ({cnt} course{'s' if cnt != 1 else ''})" for name, cnt in preview_entries)
                if preview_entries
                else "(none)"
            )

            msg = (
                f"You are about to delete parent '{parent_name}' and {cat_count - 1 if cat_count > 0 else 0} descendant categories,\n"
                f"removing {course_count} course(s) in total.\n\n"
                f"Affected categories (showing {len(preview_entries)}):\n{preview_lines}"
                + (f"\n... and {remaining} more" if remaining else "")
                + "\n\nProceed?"
            )

            kb = [
                [InlineKeyboardButton("Yes, delete", callback_data=f"delete_confirm::parent::{key}")],
                [InlineKeyboardButton("Cancel", callback_data=f"cancel_delete::{key}")],
            ]
            await safe_edit_message(
                query,
                msg,
                reply_markup=InlineKeyboardMarkup(kb),
                action_key=getattr(query, "data", None),
            )
            return
        except Exception:
            logger.exception("Error building parent delete summary")
            await safe_edit_message(
                query,
                "Failed to prepare delete summary. Try again.",
                action_key=getattr(query, "data", None),
            )
            return

    await safe_edit_message(query, "Unknown summary action.", action_key=getattr(query, "data", None))
