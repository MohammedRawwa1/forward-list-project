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


def _parse_origin_page(value, default: int = 1) -> int:
    try:
        page = int(value)
        return page if page >= 1 else default
    except Exception:
        return default


async def _course_doc_by_id(db, course_id: str, projection=None):
    """Locate the single category doc holding the course with this uuid."""
    try:
        return await db["categories"].find_one({"courses.id": course_id}, projection)
    except Exception:
        logger.exception("_course_doc_by_id failed for course_id=%s", course_id)
        return None


async def _delete_course_by_id(db, course_id: str):
    """Delete exactly the course matching this uuid, wherever it lives."""
    return await db["categories"].update_one(
        {"courses.id": course_id},
        {"$pull": {"courses": {"id": course_id}}},
    )


class _NoopResult:
    modified_count = 0


async def _delete_course_guarded(db, category: str, item: str):
    """Name-based fallback for legacy courses without a uuid.

    Only deletes when exactly one course in the target doc has that name —
    refuses to fire when duplicate-named courses exist (that's the bug this
    guards against, since $pull would remove every match at once).
    Returns (result, deleted_doc_name).
    """
    holder = None
    try:
        holder = await db["categories"].find_one({"name": category}, projection={"name": 1, "courses": 1})
    except Exception:
        holder = None
    if not holder:
        try:
            holder = await db["categories"].find_one({"courses.name": item}, projection={"name": 1, "courses": 1})
        except Exception:
            holder = None
    if not holder:
        return _NoopResult(), None
    matches = [c for c in (holder.get("courses") or []) if isinstance(c, dict) and c.get("name") == item]
    if len(matches) != 1:
        logger.warning(
            "[DEL] refusing ambiguous name-based delete: %d courses named '%s' in '%s'",
            len(matches),
            item,
            holder.get("name"),
        )
        return _NoopResult(), None
    holder_name = holder.get("name")
    res = await db["categories"].update_one(
        {"name": holder_name},
        {"$pull": {"courses": {"name": item}}},
    )
    if getattr(res, "modified_count", 0):
        return res, holder_name
    return res, None


async def _after_course_delete(category: str = None, coach: str = None):
    try:
        from handlers.base_handlers import invalidate_course_caches

        await invalidate_course_caches(category=category, coach=coach)
    except Exception:
        logger.debug("invalidate_course_caches unavailable", exc_info=True)

logger = logging.getLogger(__name__)
BATCH_LIMIT = 500


# ----------  delete category  ----------
async def handle_category_deletion(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
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

    try:
        payload = None
        try:
            payload = await _resolve_callback_payload(cat)
        except Exception:
            payload = None
        if not payload:
            try:
                payload = await _resolve_callback_ref_key(db, cat)
            except Exception:
                payload = None

        cat_doc = None
        if isinstance(payload, dict):
            payload_cat_id = payload.get("category_id") or payload.get("id")
            if payload_cat_id and is_uuid(str(payload_cat_id)):
                try:
                    cat_doc = await db["categories"].find_one(
                        {"id": str(payload_cat_id)},
                        projection={"path": 1, "id": 1, "name": 1},
                    )
                except Exception:
                    cat_doc = None
            if not cat_doc and payload.get("category"):
                try:
                    cat_doc = await db["categories"].find_one(
                        {"$or": [{"path": payload.get("category")}, {"name": payload.get("category")}]},
                        projection={"path": 1, "id": 1, "name": 1},
                    )
                except Exception:
                    cat_doc = None

        if not cat_doc:
            cat_doc = await db["categories"].find_one(
                {"$or": [{"path": cat}, {"name": cat}]},
                projection={"_id": 1, "path": 1, "id": 1, "name": 1},
            )

        if cat_doc and cat_doc.get("name"):
            cat = cat_doc.get("name")
        if cat_doc and cat_doc.get("path"):
            base_path = cat_doc.get("path")
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
            root_id = cat_doc.get("_id") if cat_doc else None
            to_delete = await collect_subtree_names(
                db,
                cat,
                batch_limit=BATCH_LIMIT,
            )
            if to_delete:
                all_docs = (
                    await db["categories"]
                    .find({"name": {"$in": list(to_delete)}}, {"_id": 1, "parent": 1})
                    .to_list(length=len(to_delete) * BATCH_LIMIT)
                )
                ids_to_remove = set()
                if root_id:
                    ids_to_remove.add(root_id)
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
    query = update.callback_query
    await safe_answer(query)
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
            res = await _delete_course_by_id(db, course_id)
        elif cat and item:
            res, _deleted_cat = await _delete_course_guarded(db, cat, item)
        else:
            await safe_edit_message(query, "Cannot determine course to delete.", action_key=data)
            return
        if getattr(res, "modified_count", 0):
            await _after_course_delete(category=cat)
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
    user_id = getattr(query.from_user, "id", None)
    if not is_owner(user_id):
        await safe_edit_message(
            query,
            "⛔ Only the bot owner can run this command.",
            action_key=getattr(query, "data", None),
        )
        return
    logger.info("[DEL-ITEM] callback data=%s", query.data)
    data = query.data
    db = await MongoDB.get_db()

    if data.startswith("delete_item_ref::"):
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
        item_id = payload.get("id")
        cat_filter = {"id": cat_id} if cat_id else {"name": cat}
        if item_id:
            res = await _delete_course_by_id(db, item_id)
            if not getattr(res, "modified_count", 0):
                res = await db["categories"].update_one(cat_filter, {"$pull": {"courses": {"name": item}}})
        else:
            res = await db["categories"].update_one(cat_filter, {"$pull": {"courses": {"name": item}}})
        if res.modified_count:
            await _after_course_delete(category=cat)
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
            cat_filter = {"id": cat_raw} if is_uuid(cat_raw) else {"name": cat_raw}
            res = await db["categories"].update_one(cat_filter, {"$pull": {"courses": {"name": item}}})
            if res.modified_count:
                await _after_course_delete(category=cat_raw)
                await safe_edit_message(
                    query,
                    f"Course ‘{item}’ deleted from category ‘{cat_raw}’. ✅",
                    action_key=getattr(query, "data", None),
                )
                return
            await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, "data", None))
            return

    item = data.split("_", 2)[2] if "_" in data else data
    item = urllib.parse.unquote_plus(item)
    holder = await db["categories"].find_one({"courses.name": item}, projection={"name": 1, "courses": 1})
    if not holder:
        await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, "data", None))
        return
    matches = [c for c in (holder.get("courses") or []) if isinstance(c, dict) and c.get("name") == item]
    if len(matches) != 1:
        await safe_edit_message(
            query,
            f"Multiple courses named ‘{item}’ exist. Delete each from its category list instead. ❌",
            action_key=getattr(query, "data", None),
        )
        return
    res = await db["categories"].update_one({"name": holder.get("name")}, {"$pull": {"courses": {"name": item}}})
    if res.modified_count:
        await _after_course_delete(category=holder.get("name"))
        await safe_edit_message(query, f"Course ‘{item}’ deleted. ✅", action_key=getattr(query, "data", None))
    else:
        await safe_edit_message(query, "Course not found. ❌", action_key=getattr(query, "data", None))


# ----------  delete confirmation  ----------
async def handle_delete_confirm(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
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
    payload_id = payload.get("category_id") or payload.get("id")

    async def _resolve_cat_filter():
        """Resolve a category filter by uuid first, falling back to name + _id anchor."""
        if payload_id and is_uuid(str(payload_id)):
            doc = await db["categories"].find_one({"id": str(payload_id)}, projection={"_id": 1, "name": 1})
            if doc:
                return {"_id": doc.get("_id")}, doc.get("name") or cat
        doc = await db["categories"].find_one({"name": cat}, projection={"_id": 1, "name": 1})
        if doc:
            return {"_id": doc.get("_id")}, doc.get("name") or cat
        return {"name": cat}, cat

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
            if not cat:
                await safe_edit_message(
                    query,
                    "Cannot determine course category. Aborting.",
                    action_key=getattr(query, "data", None),
                )
                return
            course_id = payload.get("id")
            if course_id:
                res = await _delete_course_by_id(db, course_id)
            else:
                res, _ = await _delete_course_guarded(db, cat, item)

            if res.modified_count:
                await _after_course_delete(category=cat)
                try:
                    cat_doc = await db["categories"].find_one({"name": cat})
                    courses = cat_doc.get("courses", []) if cat_doc else []
                    all_courses = [
                        {
                            "name": c.get("name"),
                            "link": c.get("link"),
                            "category": cat,
                            "id": str(c.get("id")) if c.get("id") is not None else None,
                        }
                        for c in courses
                        if isinstance(c, dict)
                    ]
                    all_courses = sorted(all_courses, key=lambda c: (c.get("name") or "").lower())
                    from handlers.base_handlers import build_courses_page

                    try:
                        page = int(payload.get("origin_page", 1))
                    except Exception:
                        page = 1
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
                    "Cannot determine category to delete. Its reference may have expired. ❌",
                    action_key=getattr(query, "data", None),
                )
                return
            try:
                cat_filter, cat_name = await _resolve_cat_filter()
                cat = cat_name
                root_doc = await db["categories"].find_one(cat_filter, projection={"_id": 1, "name": 1})
                root_id = root_doc.get("_id") if root_doc else None
                to_delete = await collect_subtree_names(
                    db,
                    cat,
                    batch_limit=BATCH_LIMIT,
                )
                if to_delete:
                    all_docs = (
                        await db["categories"]
                        .find({"name": {"$in": list(to_delete)}}, {"_id": 1, "parent": 1})
                        .to_list(length=len(to_delete) * BATCH_LIMIT)
                    )
                    ids_to_remove = set()
                    if root_id:
                        ids_to_remove.add(root_id)
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
                    "Cannot determine parent to delete. Its reference may have expired. ❌",
                    action_key=getattr(query, "data", None),
                )
                return
            cat_filter, cat_name = await _resolve_cat_filter()
            cat = cat_name
            cat_doc = await db["categories"].find_one(cat_filter)
            parent_name = cat_doc.get("parent") if cat_doc else None
            if not parent_name:
                await safe_edit_message(query, "Parent not found. ❌", action_key=getattr(query, "data", None))
                return
            try:
                parent_doc = await db["categories"].find_one(
                    {"name": parent_name},
                    projection={"_id": 1, "name": 1},
                )
                parent_root_id = parent_doc.get("_id") if parent_doc else None
                to_delete = await collect_subtree_names(
                    db,
                    parent_name,
                    batch_limit=BATCH_LIMIT,
                )
                if to_delete:
                    all_docs = (
                        await db["categories"]
                        .find({"name": {"$in": list(to_delete)}}, {"_id": 1, "parent": 1})
                        .to_list(length=len(to_delete) * BATCH_LIMIT)
                    )
                    ids_to_remove = set()
                    if parent_root_id:
                        ids_to_remove.add(parent_root_id)
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


# ----------  delete summary  ----------
async def handle_delete_summary(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
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
    payload_id = payload.get("category_id") or payload.get("id")
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
            cat_filter = {"id": str(payload_id)} if payload_id and is_uuid(str(payload_id)) else {"name": cat}
            cat_doc = await db["categories"].find_one(cat_filter, projection={"courses": 1, "name": 1})
            if cat_doc and cat_doc.get("name"):
                cat = cat_doc.get("name")
            if cat_doc is None:
                await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, "data", None))
                return

            has_courses = bool(cat_doc.get("courses"))
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

            to_delete = await collect_subtree_names(
                db,
                cat,
                batch_limit=BATCH_LIMIT,
            )

            cat_count = len(to_delete)
            docs = (
                await db["categories"]
                .find({"name": {"$in": list(to_delete)}}, projection={"name": 1, "courses": 1})
                .to_list(length=cat_count)
            )
            doc_map = {d.get("name"): d for d in docs}
            course_count = sum(len(d.get("courses", [])) for d in docs)

            preview_limit = 10
            entries = []
            for n in to_delete:
                try:
                    cnt = len(doc_map.get(n, {}).get("courses", []))
                except Exception:
                    cnt = 0
                entries.append((n, cnt))
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
            cat_filter = {"id": str(payload_id)} if payload_id and is_uuid(str(payload_id)) else {"name": cat}
            cat_doc = await db["categories"].find_one(cat_filter)
            if cat_doc is None:
                await safe_edit_message(query, "Category not found. ❌", action_key=getattr(query, "data", None))
                return
            if cat_doc and cat_doc.get("name"):
                cat = cat_doc.get("name")
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
            docs = (
                await db["categories"]
                .find({"name": {"$in": list(to_delete)}}, projection={"name": 1, "courses": 1})
                .to_list(length=cat_count)
            )
            doc_map = {d.get("name"): d for d in docs}
            course_count = sum(len(d.get("courses", [])) for d in docs)

            preview_limit = 10
            entries = []
            for n in to_delete:
                try:
                    cnt = len(doc_map.get(n, {}).get("courses", []))
                except Exception:
                    cnt = 0
                entries.append((n, cnt))
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
