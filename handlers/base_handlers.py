import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from pymongo.errors import DuplicateKeyError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import CallbackContext, ConversationHandler

from config import env_float, env_int
from conversation_states import CREATE_CAT_NAME, CREATE_CAT_PARENT
from handlers.db_connection import get_db

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ----------  helpers  ----------
def is_uuid(s: str) -> bool:
    try:
        return bool(UUID_RE.match(str(s).strip()))
    except Exception:
        return False


async def _resolve_callback_ref_key(db, key: str) -> dict | None:
    if not isinstance(key, str) or len(key) != 16 or not re.match(r"^[0-9a-fA-F]{16}$", key):
        return None
    try:
        existing = await db["categories"].find_one(
            {"name": key},
            projection={"_id": 1},
        )
        if existing:
            return None
    except Exception:
        return None
    return CALLBACK_MAP.get(key)


async def ensure_course_uuids(db, category_name: str, batch_limit: int = 500):
    """Ensure every embedded course in the given category doc(s) has a stable uuid.

    Mirrors the id backfill that categories/parents already get: existing uuids are
    never touched, missing ones are generated once and persisted by the course's
    _id so identical names keep distinct identities.
    """
    try:
        docs = (
            await db["categories"]
            .find({"$or": [{"name": category_name}, {"path": category_name}]}, {"_id": 1, "courses": 1})
            .to_list(length=batch_limit)
        )
    except Exception:
        logger.exception("ensure_course_uuids: failed to load category '%s'", category_name)
        return
    for doc in docs:
        doc_id = doc.get("_id")
        courses = doc.get("courses") or []
        updates = {}
        for idx, course in enumerate(courses):
            if not isinstance(course, dict):
                continue
            cid = course.get("id")
            if isinstance(cid, str) and cid.strip() and UUID_RE.match(cid.strip()):
                continue
            new_id = str(uuid.uuid4())
            updates[f"courses.{idx}.id"] = new_id
            try:
                course["id"] = new_id
            except Exception:
                pass
        if updates:
            try:
                await db["categories"].update_one({"_id": doc_id}, {"$set": updates})
            except Exception:
                logger.exception("ensure_course_uuids: failed to persist uuids for doc %s", doc_id)


async def invalidate_course_caches(category: str = None, coach: str = None):
    """Drop in-memory and redis page/count caches so deleted courses vanish from lists."""
    keys = []
    try:
        if category:
            quoted = urllib.parse.quote_plus(str(category))
            keys.extend(
                [
                    f"page:category:{quoted}:1:{PAGE_SIZE}",
                    f"count:category_courses:{category}",
                    f"count:category_courses_np:{category}",
                ],
            )
        if coach:
            keys.append(f"page:coach:{coach}:1")
        keys.append("page:global:1")
    except Exception:
        return
    for key in keys:
        try:
            _PAGE_CACHE.pop(key, None)
        except Exception:
            pass
        try:
            _COUNT_CACHE.pop(key, None)
        except Exception:
            pass
        try:
            if _redis is not None:
                _bg_task(_redis.delete(key))
        except Exception:
            pass
    # Search result sets are cached too; a delete must not leave stale rows.
    try:
        from handlers.search_handlers import invalidate_search_cache

        invalidate_search_cache()
    except Exception:
        pass


async def collect_subtree_names(
    db,
    root_name: str,
    *,
    filter_fn=None,
    batch_limit: int = 500,
    max_nodes: int = 5000,
    collection: str = "categories",
) -> set[str]:
    if filter_fn is None:

        def _default_filter(curr):
            return {"$or": [{"parent": curr}, {"path": {"$regex": f"^{re.escape(curr)}/"}}]}

        filter_fn = _default_filter

    discovered: set[str] = set()
    stack = [root_name]
    while stack:
        curr = stack.pop()
        if curr in discovered:
            continue
        discovered.add(curr)
        if len(discovered) > max_nodes:
            logger.warning(
                "Subtree traversal for '%s' exceeded %d nodes \u2014 aborting (possible cycle)",
                root_name,
                max_nodes,
            )
            raise RuntimeError(f"Subtree traversal for '{root_name}' exceeded {max_nodes} nodes (possible cycle)")
        children = await db[collection].find(filter_fn(curr), {"name": 1}).to_list(length=batch_limit)
        for ch in children:
            name = ch.get("name")
            if name and name not in discovered:
                stack.append(name)
    return discovered


CALLBACK_REF_TTL = env_int("CALLBACK_REF_TTL", 7 * 24 * 3600)

CALLBACK_MAP = {}
CALLBACK_MAP_MAX = env_int("CALLBACK_MAP_MAX", 50000)

_CALLBACK_INDEX_ENSURE_DONE = False

CALLBACK_PERSIST_CONCURRENCY = max(1, env_int("CALLBACK_PERSIST_CONCURRENCY", 10))
_persist_semaphore = asyncio.Semaphore(CALLBACK_PERSIST_CONCURRENCY)


def _parse_ttl(value, default=300):
    if value is None or str(value).strip() == "":
        return default
    s = str(value).strip()
    try:
        if s.isdigit():
            return int(s)
        m = re.match(r"^(\d+)([smhd])$", s, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            return n * mult
        return int(float(s))
    except Exception:
        return default


GUI_SESSION_TTL = _parse_ttl(os.getenv("GUI_SESSION_TTL", "300"), 300)
logger = logging.getLogger(__name__)
logger.debug("GUI_SESSION_TTL=%s seconds (env=%r)", GUI_SESSION_TTL, os.getenv("GUI_SESSION_TTL"))

TOP_LEVEL_FILTER = {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}

_COUNT_CACHE = {}
_COUNT_CACHE_MAX = 5000


# ----------  cache maintenance  ----------
def _prune_count_cache():
    now = time.time()
    expired = [k for k, (_, ts) in _COUNT_CACHE.items() if ts < now]
    for k in expired:
        try:
            del _COUNT_CACHE[k]
        except Exception:
            pass
    if len(_COUNT_CACHE) > _COUNT_CACHE_MAX:
        drop = max(1, len(_COUNT_CACHE) // 4)
        for _ in range(drop):
            try:
                _COUNT_CACHE.pop(next(iter(_COUNT_CACHE)), None)
            except StopIteration:
                break


def _prune_page_cache():
    now = time.time()
    expired = [k for k, (_, ts) in _PAGE_CACHE.items() if ts < now]
    for k in expired:
        try:
            del _PAGE_CACHE[k]
        except Exception:
            pass
    if len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
        drop = max(1, len(_PAGE_CACHE) // 4)
        for _ in range(drop):
            try:
                _PAGE_CACHE.pop(next(iter(_PAGE_CACHE)), None)
            except StopIteration:
                break


def _prune_callback_resolve_cache():
    now = time.time()
    expired = [k for k, (_, ts) in _CALLBACK_RESOLVE_CACHE.items() if ts < now]
    for k in expired:
        try:
            del _CALLBACK_RESOLVE_CACHE[k]
        except Exception:
            pass
    if len(_CALLBACK_RESOLVE_CACHE) > _CALLBACK_RESOLVE_CACHE_MAX:
        items = sorted(_CALLBACK_RESOLVE_CACHE.items(), key=lambda kv: kv[1][1])
        drop = max(1, len(items) // 4)
        for k, _ in items[:drop]:
            try:
                del _CALLBACK_RESOLVE_CACHE[k]
            except Exception:
                pass


def _prune_user_buckets():
    if len(_USER_BUCKETS) > _USER_BUCKETS_MAX:
        now = time.time()
        cutoff = now - 3600.0
        stale = [uid for uid, b in _USER_BUCKETS.items() if b.get("last_refill", 0) < cutoff]
        if len(stale) < len(_USER_BUCKETS) // 4:
            drop = max(1, len(_USER_BUCKETS) // 4)
            for uid in stale:
                try:
                    del _USER_BUCKETS[uid]
                except Exception:
                    pass
            remaining_drop = drop - len(stale)
            for _ in range(remaining_drop):
                try:
                    _USER_BUCKETS.pop(next(iter(_USER_BUCKETS)), None)
                except StopIteration:
                    break
        else:
            drop = max(1, len(stale) // 4)
            for uid in stale[:drop]:
                try:
                    del _USER_BUCKETS[uid]
                except Exception:
                    pass


_COUNT_CACHE_LOCKS = {}
_COUNT_CACHE_LOCKS_MAX = 1000
_COUNT_CACHE_LOCKS_TTL = 300
_PAGE_CACHE = {}
_PAGE_CACHE_MAX = 5000
PAGE_CACHE_TTL = env_int("PAGE_CACHE_TTL", 30)
_CALLBACK_RESOLVE_CACHE = {}
_CALLBACK_RESOLVE_CACHE_MAX = env_int("CALLBACK_RESOLVE_CACHE_MAX", 2000)


def _set_callback_resolve_cache(key: str, payload, ttl: int = 60):
    try:
        expire = time.time() + ttl
        _CALLBACK_RESOLVE_CACHE[key] = (payload, expire)
        _prune_callback_resolve_cache()
    except Exception:
        pass


# Per-user inline session activity. Each user's keyboard session "opens" when
# they interact and "closes" once THAT user has been idle for GUI_SESSION_TTL.
# Activity is tracked per user id, so one busy user never keeps another user's
# session alive (and vice versa): there is no single shared session clock, and
# the admin's session is isolated from everyone else's exactly like any other
# user's. The single GUI_SESSION_TTL env var still defines the idle window.
#
# The activity map is also the per-user "last seen" clock used to bound PTB's
# per-user data: it is kept for _SESSION_ACTIVITY_TTL (at least USER_DATA_TTL)
# and hard-capped so it can never grow without limit as users accumulate.
USER_DATA_TTL = env_int("USER_DATA_TTL", 24 * 3600)
_USER_SESSION_ACTIVITY: dict[int, float] = {}
_SESSION_ACTIVITY_TTL = max(GUI_SESSION_TTL, USER_DATA_TTL * 2)
_USER_SESSION_ACTIVITY_MAX = env_int("SESSION_ACTIVITY_MAX", 100000)


def _touch_user_session(user_id):
    """Record activity for a user, resetting their independent idle timer."""
    try:
        if user_id is None:
            return
        uid = int(user_id)
        now = time.time()
        if len(_USER_SESSION_ACTIVITY) > _USER_SESSION_ACTIVITY_MAX // 4 * 3:
            cutoff = now - _SESSION_ACTIVITY_TTL
            stale = [k for k, ts in _USER_SESSION_ACTIVITY.items() if ts < cutoff]
            for k in stale:
                try:
                    del _USER_SESSION_ACTIVITY[k]
                except Exception:
                    pass
        _USER_SESSION_ACTIVITY[uid] = now
        if len(_USER_SESSION_ACTIVITY) > _USER_SESSION_ACTIVITY_MAX:
            drop = max(1, len(_USER_SESSION_ACTIVITY) // 4)
            for _ in range(drop):
                try:
                    _USER_SESSION_ACTIVITY.pop(next(iter(_USER_SESSION_ACTIVITY)), None)
                except StopIteration:
                    break
    except Exception:
        pass


def _user_idle_seconds(user_id):
    """Seconds since the user's last recorded activity, or None if unknown."""
    try:
        if user_id is None:
            return None
        ts = _USER_SESSION_ACTIVITY.get(int(user_id))
        if ts is None:
            return None
        return time.time() - ts
    except Exception:
        return None


def _session_user_id(message):
    """Best-effort user id for a message (private chats: chat id == user id)."""
    try:
        chat = getattr(message, "chat", None)
        if chat is None:
            return None
        uid = getattr(chat, "id", None)
        return int(uid) if uid is not None else None
    except Exception:
        return None


def _event_user_id(update_or_message):
    """Resolve the acting user's id from an Update or a Message."""
    try:
        u = getattr(update_or_message, "effective_user", None)
        if u is None:
            u = getattr(update_or_message, "from_user", None)
        return getattr(u, "id", None) if u is not None else None
    except Exception:
        return None


def _set_session_keep_open(message, keep: bool = True):
    """Compatibility shim: touch (or drop) the session activity for a message's user."""
    try:
        uid = _session_user_id(message)
        if uid is None:
            return
        if keep:
            _touch_user_session(uid)
        else:
            _USER_SESSION_ACTIVITY.pop(uid, None)
    except Exception:
        pass


@asynccontextmanager
# ----------  counts & timing  ----------
async def _db_timing(name: str):
    t0 = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - t0
        try:
            logger.debug("[DB-TIME] %s %.3fs", name, elapsed)
        except Exception:
            pass


async def _get_total_count(db, coll_name: str, filter_q: dict = None, ttl: int = 60):
    key = f"count:{coll_name}:{json.dumps(filter_q or {}, sort_keys=True)}"
    now = time.time()
    entry = _COUNT_CACHE.get(key)
    if entry and entry[1] > now:
        return entry[0]

    if key not in _COUNT_CACHE_LOCKS:
        _COUNT_CACHE_LOCKS[key] = (asyncio.Lock(), time.time())
    lock, _ = _COUNT_CACHE_LOCKS[key]
    if len(_COUNT_CACHE_LOCKS) > _COUNT_CACHE_LOCKS_MAX:
        cutoff = time.time() - _COUNT_CACHE_LOCKS_TTL
        stale = [k for k, (_, created) in _COUNT_CACHE_LOCKS.items() if created < cutoff]
        for k in stale:
            try:
                del _COUNT_CACHE_LOCKS[k]
            except Exception:
                pass
    async with lock:
        entry = _COUNT_CACHE.get(key)
        if entry and entry[1] > time.time():
            return entry[0]

        try:
            if _redis is not None:
                val = await _redis.get(key)
                if val is not None:
                    try:
                        cnt = int(val)
                        _COUNT_CACHE[key] = (cnt, now + ttl)
                        _prune_count_cache()
                        return cnt
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            coll = getattr(db, coll_name) if hasattr(db, coll_name) else db[coll_name]
            cnt = await coll.count_documents(filter_q or {})
        except Exception:
            cnt = 0

        _COUNT_CACHE[key] = (cnt, now + ttl)
        _prune_count_cache()
        try:
            if _redis is not None:
                await _redis.setex(key, ttl, str(cnt))
        except Exception:
            pass
        return cnt


def _get_cached_page(key: str):
    now = time.time()
    entry = _PAGE_CACHE.get(key)
    if entry and entry[1] > now:
        return entry[0]
    return None


def _has_real_courses(courses):
    try:
        if not courses:
            return False
        for c in courses:
            if not c:
                continue
            if isinstance(c, dict):
                name = c.get("name")
            else:
                name = c
            if name and str(name).strip():
                return True
    except Exception:
        return False
    return False


def _set_cached_page(key: str, payload, ttl: int = 3):
    _PAGE_CACHE[key] = (payload, time.time() + ttl)
    _prune_page_cache()
    try:
        if _redis is not None:
            _bg_task(_redis.set(key, json.dumps(payload), ex=ttl))
    except Exception:
        pass


async def _get_courses_count(db, category: str, ttl: int = 60):
    key = f"count:category_courses:{category}"
    now = time.time()
    entry = _COUNT_CACHE.get(key)
    if entry and entry[1] > now:
        return entry[0]
    try:
        if _redis is not None:
            val = await _redis.get(key)
            if val is not None:
                try:
                    cnt = int(val)
                except Exception:
                    cnt = 0
                _COUNT_CACHE[key] = (cnt, now + ttl)
                _prune_count_cache()
                return cnt
    except Exception:
        pass

    try:
        pipeline = [{"$match": {"name": category}}, {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}}]
        agg = await db.categories.aggregate(pipeline).to_list(length=1)
        cnt = int(agg[0].get("n", 0)) if agg else 0
    except Exception:
        cnt = 0

    _COUNT_CACHE[key] = (cnt, now + ttl)
    _prune_count_cache()
    try:
        if _redis is not None:
            _bg_task(_redis.set(key, str(cnt), ex=ttl))
    except Exception:
        pass
    return cnt


async def _get_bot_courses_total(db, ttl: int = 60):
    """Total number of courses across every category in the bot (from MongoDB).

    Cached (memory + Redis) so course lists can render a 'shown/total' counter
    without re-counting the whole collection on every page.
    """
    key = "count:all_courses"
    now = time.time()
    entry = _COUNT_CACHE.get(key)
    if entry and entry[1] > now:
        return entry[0]
    try:
        if _redis is not None:
            val = await _redis.get(key)
            if val is not None:
                try:
                    cnt = int(val)
                except Exception:
                    cnt = 0
                _COUNT_CACHE[key] = (cnt, now + ttl)
                _prune_count_cache()
                return cnt
    except Exception:
        pass

    try:
        pipeline = [
            {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
            {"$group": {"_id": None, "count": {"$sum": "$n"}}},
        ]
        agg = await db.categories.aggregate(pipeline).to_list(length=1)
        cnt = int(agg[0].get("count", 0)) if agg else 0
    except Exception:
        cnt = 0

    _COUNT_CACHE[key] = (cnt, now + ttl)
    _prune_count_cache()
    try:
        if _redis is not None:
            _bg_task(_redis.set(key, str(cnt), ex=ttl))
    except Exception:
        pass
    return cnt


# ----------  message scheduling  ----------
# How often each open session re-checks whether its user has gone idle. Small
# enough to close promptly, large enough to avoid spinning: the timer is
# per-user, so continued activity merely resets that user's own deadline.
SESSION_CLOSE_CHECK_INTERVAL = max(0.5, env_float("SESSION_CLOSE_CHECK_INTERVAL", 5.0))


def schedule_close_inline_message(
    message,
    delay: int = None,
    notice: str = "(Session closed due to inactivity)",
    user_id=None,
):
    if delay is None:
        delay = GUI_SESSION_TTL

    uid = user_id if user_id is not None else _session_user_id(message)
    # Opening the session counts as activity so this user's idle clock starts now.
    _touch_user_session(uid)

    async def _worker():
        try:
            # Close only once THIS user has been idle for `delay`, re-checking
            # periodically so their own continued activity resets the deadline
            # without touching any other user's session.
            if uid is None:
                await asyncio.sleep(delay)
            else:
                while True:
                    idle = _user_idle_seconds(uid)
                    if idle is None or idle >= delay:
                        break
                    await asyncio.sleep(min(SESSION_CLOSE_CHECK_INTERVAL, max(delay - idle, 0.5)))

            orig = getattr(message, "text", None) or getattr(message, "caption", None) or ""
            try:
                await message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            try:
                new_text = orig or ""
                if notice:
                    new_text = new_text + "\n\n" + notice
                if getattr(message, "photo", None):
                    await message.edit_caption(caption=new_text)
                else:
                    await message.edit_text(new_text)
            except Exception:
                pass
        except Exception:
            logger.exception("Error in schedule_close_inline_message worker")

    try:
        _bg_task(_worker())
    except Exception:
        pass


# ----------  callback builders  ----------
def _make_course_ref(
    category: str,
    name: str,
    origin_type: str,
    origin_page: int,
    origin_context: str = None,
    origin_context_page: int = None,
    course_id: str = None,
    search_ref: str = None,
) -> str:
    page_to_use = origin_context_page or origin_page or 1
    if origin_type == "category":
        target = origin_context or category
        back_cb = f"courses::category::{urllib.parse.quote_plus(str(target))}::{page_to_use}"
    elif origin_type == "coach":
        target = origin_context or category
        back_cb = f"courses::coach::{urllib.parse.quote_plus(str(target))}::{page_to_use}"
    else:
        back_cb = f"courses::global::{page_to_use}"

    payload = {
        "category": category,
        "name": name,
        "id": course_id,
        "origin_type": origin_type,
        "origin_page": origin_page,
        "origin_context": origin_context,
        "origin_context_page": origin_context_page,
        "back_cb": back_cb,
    }
    if search_ref:
        payload["search_ref"] = search_ref
    key = _store_callback_payload(payload)
    try:
        logger.debug(
            "_make_course_ref: stored key=%s category=%s name=%s origin_type=%s origin_page=%s origin_context=%s back_cb=%s",
            key,
            category,
            name,
            origin_type,
            origin_page,
            origin_context,
            back_cb,
        )
    except Exception:
        pass
    try:
        enc = urllib.parse.quote_plus(back_cb)
        candidate = f"course_ref::{key}::back::{enc}"
        if len(candidate.encode("utf-8")) <= 64:
            logger.debug("_make_course_ref: using inline candidate (len=%d)", len(candidate.encode("utf-8")))
            return candidate
        logger.debug(
            "_make_course_ref: candidate too long (%d bytes), returning stored key course_ref::%s",
            len(candidate.encode("utf-8")),
            key,
        )
        return f"course_ref::{key}"
    except Exception:
        return f"course_ref::{key}"


def _store_callback_payload(payload: dict) -> str:
    key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode(), usedforsecurity=False).hexdigest()[:16]
    CALLBACK_MAP[key] = payload
    if len(CALLBACK_MAP) > CALLBACK_MAP_MAX:
        drop = max(1, len(CALLBACK_MAP) // 4)
        keys_to_drop = list(CALLBACK_MAP.keys())[:drop]
        for k in keys_to_drop:
            try:
                del CALLBACK_MAP[k]
            except Exception:
                pass
    try:
        logger.debug("_store_callback_payload: key=%s payload=%s", key, payload)
    except Exception:
        pass
    try:
        _bg_task(_persist_callback_payload(key, payload))
    except Exception:
        pass
    return key


def _shorten_showcat_cb(
    path: str,
    page: int,
    from_parent: str | None = None,
    parent_page: int | None = None,
    cat_id: str | None = None,
):
    try:
        if from_parent is not None or parent_page is not None or cat_id is not None:
            payload = {"type": "showcat", "path": path, "page": page}
            if from_parent is not None:
                payload["from_parent"] = from_parent
            if parent_page is not None:
                payload["parent_page"] = parent_page
            if cat_id is not None:
                payload["id"] = cat_id
            try:
                key = _store_callback_payload(payload)
                return f"showcat_ref::{key}"
            except Exception:
                pass

        cb = f"showcat::{urllib.parse.quote_plus(path)}::{page}"
        if len(cb.encode("utf-8")) <= 64:
            return cb
        payload = {"type": "showcat", "path": path, "page": page}
        key = _store_callback_payload(payload)
        return f"showcat_ref::{key}"
    except Exception:
        return f"showcat::{urllib.parse.quote_plus(path)}::{page}"


def _search_category_courses_cb(category, page: int = 1) -> str:
    try:
        cb = f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}"
        if len(cb.encode("utf-8")) <= 64:
            return cb
        payload = {"type": "search_category_courses", "category": str(category), "page": page}
        key = _store_callback_payload(payload)
        return f"search_category_courses_ref::{key}"
    except Exception:
        return f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}"


def _fit_cb(prefix: str, inline_cb: str, payload: dict) -> str:
    try:
        if len(inline_cb.encode("utf-8")) <= 64:
            return inline_cb
        key = _store_callback_payload(payload)
        return f"{prefix}_ref::{key}"
    except Exception:
        return inline_cb


def _search_courses_coach_cb(coach_name, page: int = 1) -> str:
    return _fit_cb(
        "search_courses_coach",
        f"search_courses::coach::{urllib.parse.quote_plus(str(coach_name))}::{page}",
        {"type": "search_courses_coach", "coach": str(coach_name), "page": page},
    )


def _showtype_cb(cat_name, t_name, search_ref: str = None) -> str:
    if search_ref:
        payload = {"type": "showtype", "category": str(cat_name), "type_name": str(t_name), "search_ref": search_ref}
        key = _store_callback_payload(payload)
        return f"showtype_ref::{key}"
    return _fit_cb(
        "showtype",
        f"showtype::{urllib.parse.quote_plus(str(cat_name))}::{urllib.parse.quote_plus(str(t_name))}",
        {"type": "showtype", "category": str(cat_name), "type_name": str(t_name)},
    )


def _createcat_parent_cb(name) -> str:
    return _fit_cb(
        "createcat_parent",
        f"createcat_parent::{urllib.parse.quote_plus(str(name))}",
        {"type": "createcat_parent", "category": str(name)},
    )


def _courses_home_cb(origin_type: str, category) -> str:
    inline = f"courses::{origin_type}::{urllib.parse.quote_plus(str(category))}::1"
    payload = {
        "type": "courses_page",
        "page": 1,
        "origin_type": origin_type,
        "category": str(category),
        "origin_context": None,
        "origin_context_page": None,
        "total_count": None,
        "page_size": PAGE_SIZE,
    }
    return _fit_cb("courses", inline, payload)


def _category_page_next_cb(cat_path, page: int, total_count=None) -> str:
    try:
        cb = f"courses::category::{urllib.parse.quote_plus(str(cat_path))}::{page}"
        if len(cb.encode("utf-8")) <= 64:
            return cb
        payload = {
            "type": "courses_page",
            "page": page,
            "origin_type": "category",
            "category": str(cat_path),
            "origin_context": None,
            "origin_context_page": None,
            "total_count": total_count,
            "page_size": PAGE_SIZE,
        }
        key = _store_callback_payload(payload)
        return f"courses_ref::{key}"
    except Exception:
        return f"courses::category::{urllib.parse.quote_plus(str(cat_path))}::{page}"


# ----------  callback payload store  ----------
async def _persist_callback_payload(key: str, payload: dict, ttl: int = 60 * 60 * 24 * 7):
    global _CALLBACK_INDEX_ENSURE_DONE
    try:
        if _redis is not None:
            await _redis.set(f"callback:ref:{key}", json.dumps(payload), ex=ttl)

            return
    except Exception:
        logger.exception("Failed to persist callback payload to Redis")

    try:
        async with _persist_semaphore:
            db = await get_db()
            if db is None:
                return
            expire_at = datetime.now(UTC) + timedelta(seconds=ttl)
            try:
                if not _CALLBACK_INDEX_ENSURE_DONE:
                    await db.callback_refs.create_index("expireAt", expireAfterSeconds=0)
                    _CALLBACK_INDEX_ENSURE_DONE = True
            except Exception:
                pass
            await db.callback_refs.update_one(
                {"_id": key},
                {"$set": {"payload": payload, "expireAt": expire_at}},
                upsert=True,
            )
    except Exception:
        logger.exception("Failed to persist callback payload to MongoDB")


async def _resolve_callback_payload(key: str):
    try:
        now = time.time()
        entry = _CALLBACK_RESOLVE_CACHE.get(key)
        if entry and entry[1] > now:
            return entry[0]
    except Exception:
        pass

    payload = CALLBACK_MAP.get(key)
    if payload:
        _set_callback_resolve_cache(key, payload, ttl=60)
        return payload

    try:
        if _redis is not None:
            val = await _redis.get(f"callback:ref:{key}")
            if val:
                payload = json.loads(val)
                CALLBACK_MAP[key] = payload
                _set_callback_resolve_cache(key, payload, ttl=60)
                return payload
    except Exception:
        logger.exception("Failed to read callback payload from Redis")

    try:
        db = await get_db()
        if db is None:
            return None
        doc = await db.callback_refs.find_one({"_id": key})
        if doc:
            payload = doc.get("payload")
            if payload:
                CALLBACK_MAP[key] = payload
                _set_callback_resolve_cache(key, payload, ttl=60)
                return payload
    except Exception:
        logger.exception("Failed to read callback payload from MongoDB")

    return None


async def _rehydrate_callback_map(limit: int = None):
    global _CALLBACK_INDEX_ENSURE_DONE
    cfg_limit = env_int("CALLBACK_REHYDRATE_LIMIT", 10000)
    if limit is None:
        limit = cfg_limit

    try:
        db = await get_db()
        if db is None:
            return 0

        now = datetime.now(UTC)
        try:
            try:
                await db.callback_refs.create_index("expireAt", expireAfterSeconds=0)
                _CALLBACK_INDEX_ENSURE_DONE = True
            except Exception:
                pass
            cursor = db.callback_refs.find({"expireAt": {"$gt": now}}).limit(limit)
            docs = await cursor.to_list(length=limit)
            count = 0
            for d in docs:
                try:
                    key = d.get("_id")
                    payload = d.get("payload")
                    if key and payload:
                        CALLBACK_MAP[key] = payload
                        count += 1
                except Exception:
                    continue
            logger.debug("Rehydrated %s callback refs from MongoDB", count)
            return count
        except Exception:
            logger.exception("_rehydrate_callback_map: failed to load callback refs")
            return 0
    except Exception:
        return 0


# ----------  back navigation  ----------
async def _reconcile_back_cb(db, back_cb: str, course_category: str = None, origin_page: int = None):
    try:
        if not back_cb or not isinstance(back_cb, str):
            return back_cb

        if back_cb.startswith("courses::category::"):
            parts = back_cb.split("::")
            if len(parts) >= 4:
                raw_cat = urllib.parse.unquote_plus(parts[2])
                try:
                    page = int(parts[3])
                except Exception:
                    page = int(origin_page or 1)

                try:
                    cache_key = f"page:category:{urllib.parse.quote_plus(str(raw_cat))}:{page}:{PAGE_SIZE}"
                    _PAGE_CACHE.pop(cache_key, None)
                    if _redis is not None:
                        try:
                            _bg_task(_redis.delete(cache_key))
                        except Exception:
                            pass
                except Exception:
                    pass

                try:
                    items = await get_courses_by_category(None, raw_cat, page)
                except Exception:
                    try:
                        items = await get_courses_by_category(0, raw_cat, page)
                    except Exception:
                        items = []

                if items and _has_real_courses(items):
                    return back_cb

                alternates = [raw_cat]
                try:
                    doc = await db.categories.find_one(
                        {"$or": [{"path": raw_cat}, {"name": raw_cat}]},
                        projection={"name": 1, "path": 1},
                    )
                    if doc:
                        alternates.append(doc.get("name") or raw_cat)
                        alternates.append(doc.get("path") or raw_cat)
                except Exception:
                    pass

                for alt in alternates:
                    try:
                        try:
                            alt_cache = f"page:category:{urllib.parse.quote_plus(str(alt))}:{page}:{PAGE_SIZE}"
                            _PAGE_CACHE.pop(alt_cache, None)
                            if _redis is not None:
                                try:
                                    _bg_task(_redis.delete(alt_cache))
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            items = await get_courses_by_category(0, alt, page)
                        except Exception:
                            try:
                                items = await get_courses_by_category(None, alt, page)
                            except Exception:
                                items = []
                    except Exception:
                        items = []

                    if items and _has_real_courses(items):
                        return f"courses::category::{urllib.parse.quote_plus(str(alt))}::{page}"

                for alt in alternates:
                    try:
                        try:
                            alt_cache = f"page:category:{urllib.parse.quote_plus(str(alt))}:1:{PAGE_SIZE}"
                            _PAGE_CACHE.pop(alt_cache, None)
                            if _redis is not None:
                                try:
                                    _bg_task(_redis.delete(alt_cache))
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            items = await get_courses_by_category(0, alt, 1)
                        except Exception:
                            try:
                                items = await get_courses_by_category(None, alt, 1)
                            except Exception:
                                items = []
                    except Exception:
                        items = []

                    if items and _has_real_courses(items):
                        return f"courses::category::{urllib.parse.quote_plus(str(alt))}::1"

        return back_cb
    except Exception:
        return back_cb


# ----------  children & prefetch  ----------
async def _get_children_flags(db, names, ttl: int = 30):
    if not names:
        return set()
    names = list(names)
    have = set()
    misses = []
    try:
        if _redis is not None:
            keys = [f"cat:has_children:{urllib.parse.quote_plus(n)}" for n in names]
            try:
                vals = await _redis.mget(*keys)
            except Exception:
                vals = [None] * len(keys)
            for n, v in zip(names, vals):
                if v is None:
                    misses.append(n)
                else:
                    try:
                        s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
                        if s in ("1", "true", "True"):
                            have.add(n)
                    except Exception:
                        pass
        else:
            misses = names
    except Exception:
        misses = names

    if misses:
        try:
            docs = await db.categories.find({"parent": {"$in": misses}}, {"parent": 1}).to_list(length=len(misses))
            parents = {d.get("parent") for d in docs if d.get("parent")}
        except Exception:
            parents = set()
        if _redis is not None:
            for n in misses:
                key = f"cat:has_children:{urllib.parse.quote_plus(n)}"
                val = "1" if n in parents else "0"
                try:
                    await _redis.setex(key, ttl, val)
                except Exception:
                    pass
        have |= parents
    return have


async def _prefetch_category_page(category_name: str, page: int = 1, page_size: int = None):
    try:
        if page_size is None:
            page_size = PAGE_SIZE
        await get_courses_by_category(None, category_name, page=page, page_size=page_size)
    except Exception:
        pass


_LAST_CALLBACK = {}
_LAST_CALLBACK_MAX = 10000
DEFAULT_DEBOUNCE = env_float("EDIT_DEBOUNCE", 0.2)


# ----------  rate limiting  ----------
def _is_debounced(user_id: int, action_key: str, interval: float = None) -> bool:
    if interval is None:
        interval = DEFAULT_DEBOUNCE
    now = time.time()
    key = (user_id, action_key)
    last = _LAST_CALLBACK.get(key)
    if last and (now - last) < interval:
        return True
    _LAST_CALLBACK[key] = now
    if len(_LAST_CALLBACK) > _LAST_CALLBACK_MAX // 4 * 3:
        cutoff_lc = now - 30.0
        stale_lc = [k for k, ts in _LAST_CALLBACK.items() if ts < cutoff_lc]
        for k in stale_lc:
            try:
                del _LAST_CALLBACK[k]
            except Exception:
                pass
    if len(_LAST_CALLBACK) > _LAST_CALLBACK_MAX:
        drop = max(1, len(_LAST_CALLBACK) // 4)
        for _ in range(drop):
            try:
                _LAST_CALLBACK.pop(next(iter(_LAST_CALLBACK)), None)
            except StopIteration:
                break
    return False


_USER_BUCKETS = {}
_USER_BUCKETS_MAX = 50000
# Global (process-wide) token bucket. This is the ceiling on how many callback
# edits the whole process sustains per second across ALL users, so it must be
# raised to scale past a trickle of traffic. Tunable via env; keep the refill
# rate at/under Telegram's ~30 msg/s global send limit to avoid FloodWait.
GLOBAL_BUCKET_CAPACITY = env_float("GLOBAL_BUCKET_CAPACITY", 20.0)
GLOBAL_BUCKET_REFILL_RATE = env_float("GLOBAL_BUCKET_REFILL_RATE", 5.0)
_GLOBAL_BUCKET = {
    "tokens": GLOBAL_BUCKET_CAPACITY,
    "capacity": GLOBAL_BUCKET_CAPACITY,
    "last_refill": time.time(),
    "refill_rate": GLOBAL_BUCKET_REFILL_RATE,
}
USER_BUCKET_CAPACITY = env_float("USER_BUCKET_CAPACITY", 20.0)
USER_BUCKET_REFILL_RATE = env_float("USER_BUCKET_REFILL_RATE", 5.0)

REDIS_URL = os.getenv("REDIS_URL")
_redis = None
_redis_token_script = None
if REDIS_URL:
    try:
        import redis.asyncio as redis_async

        _redis = redis_async.from_url(REDIS_URL)
        _redis_token_script = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local capacity = tonumber(ARGV[2])
        local refill = tonumber(ARGV[3])
        local cost = tonumber(ARGV[4])
        local data = redis.call('HMGET', key, 'tokens', 'last')
        local tokens = tonumber(data[1]) or capacity
        local last = tonumber(data[2]) or now
        local elapsed = now - last
        tokens = math.min(capacity, tokens + elapsed * refill)
        if tokens >= cost then
            tokens = tokens - cost
            redis.call('HMSET', key, 'tokens', tokens, 'last', now)
            redis.call('EXPIRE', key, 3600)
            return cjson.encode({1,0})
        else
            local need = cost - tokens
            local wait = math.ceil(need / refill)
            redis.call('HMSET', key, 'tokens', tokens, 'last', now)
            redis.call('EXPIRE', key, 3600)
            return cjson.encode({0,wait})
        end
        """
    except Exception:
        _redis = None
        _redis_token_script = None


async def get_total_count(db, coll_name: str, filter_q: dict = None, ttl: int = 60):
    return await _get_total_count(db, coll_name, filter_q, ttl)


def _refill_bucket(bucket):
    now = time.time()
    elapsed = now - bucket.get("last_refill", now)
    if elapsed <= 0:
        return
    bucket["tokens"] = min(
        bucket["capacity"],
        bucket.get("tokens", bucket["capacity"]) + elapsed * bucket["refill_rate"],
    )
    bucket["last_refill"] = now


async def _consume_token(user_id: int, cost: float = 1.0):
    if _redis is not None and _redis_token_script is not None:
        try:
            now = int(time.time())
            res = await _redis.eval(
                _redis_token_script,
                1,
                "bucket:global",
                now,
                _GLOBAL_BUCKET["capacity"],
                _GLOBAL_BUCKET["refill_rate"],
                cost,
            )
            ok, wait = json.loads(res)
            if ok == 1:
                user_key = f"bucket:user:{user_id}"
                res2 = await _redis.eval(
                    _redis_token_script,
                    1,
                    user_key,
                    now,
                    USER_BUCKET_CAPACITY,
                    USER_BUCKET_REFILL_RATE,
                    cost,
                )
                ok2, wait2 = json.loads(res2)
                if ok2 == 1:
                    METRICS["token_consumed"] += 1
                    try:
                        _bg_task(_redis.incr("metrics:token_consumed"))
                    except Exception:
                        pass
                    return True, 0
                return False, wait2
            return False, wait
        except Exception:
            pass

    _refill_bucket(_GLOBAL_BUCKET)
    if _GLOBAL_BUCKET["tokens"] < cost:
        needed = cost - _GLOBAL_BUCKET["tokens"]
        wait = math.ceil(needed / _GLOBAL_BUCKET["refill_rate"])
        return False, wait
    b = _USER_BUCKETS.get(user_id)
    if b is None:
        b = {
            "tokens": USER_BUCKET_CAPACITY,
            "capacity": USER_BUCKET_CAPACITY,
            "last_refill": time.time(),
            "refill_rate": USER_BUCKET_REFILL_RATE,
        }
        _USER_BUCKETS[user_id] = b
        _prune_user_buckets()
    _refill_bucket(b)
    if b["tokens"] < cost:
        needed = cost - b["tokens"]
        wait = math.ceil(needed / b["refill_rate"])
        return False, wait
    _GLOBAL_BUCKET["tokens"] -= cost
    b["tokens"] -= cost
    METRICS["token_consumed"] += 1
    try:
        if _redis is not None:
            _bg_task(_redis.incr("metrics:token_consumed"))
    except Exception:
        pass
    return True, 0


_background_tasks: set[asyncio.Task] = set()


# ----------  background tasks  ----------
def _bg_task(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t):
        _background_tasks.discard(t)
        try:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.warning(
                        "Background task raised: %s: %s",
                        type(exc).__name__,
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
        except Exception:
            logger.exception("Error inspecting background task result")

    task.add_done_callback(_on_done)
    return task


_RETRY_QUEUE = {}


# ----------  retry scheduling  ----------
def _retry_key_for(query):
    chat_id = getattr(getattr(query, "message", None), "chat_id", None)
    msg_id = getattr(getattr(query, "message", None), "message_id", None)
    if chat_id and msg_id:
        return (chat_id, msg_id)
    return getattr(query, "data", None) or "callback"


def _schedule_retry(query, text, reply_markup=None, action_key=None, delay=1, max_retries=3):
    key = _retry_key_for(query)
    if key in _RETRY_QUEUE:
        return

    async def _retry_loop():
        tries = 0
        wait = delay
        try:
            while tries < max_retries:
                await asyncio.sleep(wait)
                tries += 1
                try:
                    await query.edit_message_text(text, reply_markup=reply_markup)
                    break
                except RetryAfter as e:
                    wait = int(getattr(e, "retry_after", wait) or wait)
                    logger.warning("RetryAfter while retrying; will retry in %s seconds", wait)
                    continue
                except Exception:
                    logger.exception("Retry loop error")
                    break
        finally:
            _RETRY_QUEUE.pop(key, None)

    task = asyncio.create_task(_retry_loop())
    _RETRY_QUEUE[key] = task


METRICS = {
    "token_consumed": 0,
    "retry_scheduled": 0,
    "retry_executed": 0,
    "retry_failed": 0,
}


def _serialize_markup(reply_markup: InlineKeyboardMarkup):
    if not reply_markup:
        return None
    rows = []
    for row in reply_markup.inline_keyboard:
        r = []
        for btn in row:
            r.append(
                {
                    "text": btn.text,
                    "callback_data": getattr(btn, "callback_data", None),
                    "url": getattr(btn, "url", None),
                },
            )
        rows.append(r)
    return rows


def _deserialize_markup(rows):
    if not rows:
        return None
    kb = []
    for row in rows:
        r = []
        for b in row:
            if b.get("url"):
                r.append(InlineKeyboardButton(b["text"], url=b["url"]))
            else:
                r.append(InlineKeyboardButton(b["text"], callback_data=b.get("callback_data")))
        kb.append(r)
    return InlineKeyboardMarkup(kb)


async def _redis_schedule_retry(chat_id, message_id, text, reply_markup, execute_at: int):
    if _redis is None:
        return
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": _serialize_markup(reply_markup),
    }
    try:
        await _redis.zadd("retry:queue", {json.dumps(payload): execute_at})
        METRICS["retry_scheduled"] += 1
        try:
            await _redis.incr("metrics:retry_scheduled")
        except Exception:
            pass
    except Exception:
        logger.exception("Failed to schedule retry in Redis")


async def schedule_retry_via_redis_or_local(query, text, reply_markup=None, delay=1):
    try:
        chat_id = getattr(getattr(query, "message", None), "chat_id", None)
        message_id = getattr(getattr(query, "message", None), "message_id", None)
        when = int(time.time()) + int(delay)
        if _redis is not None and chat_id and message_id:
            await _redis_schedule_retry(chat_id, message_id, text, reply_markup, when)
            return
    except Exception:
        logger.exception("schedule_retry_via_redis_or_local failed")
    _schedule_retry(query, text, reply_markup=reply_markup, delay=delay)


async def _process_redis_retry_item(application, raw_member: str):
    try:
        payload = json.loads(raw_member)
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        text = payload.get("text")
        reply_markup = _deserialize_markup(payload.get("reply_markup"))
        try:
            await application.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            METRICS["retry_executed"] += 1
            try:
                await _redis.incr("metrics:retry_executed")
            except Exception:
                pass
        except Exception as e:
            from telegram.error import RetryAfter

            if isinstance(e, RetryAfter):
                wait = getattr(e, "retry_after", 5)
                execute_at = int(time.time()) + int(wait)
                await _redis.zadd("retry:queue", {raw_member: execute_at})
                return
            METRICS["retry_failed"] += 1
            try:
                await _redis.incr("metrics:retry_failed")
            except Exception:
                pass
            logger.exception("Retry execution failed for payload %s", payload)
    except Exception:
        logger.exception("Failed to process redis retry item: %s", raw_member)


CACHE_CLEANUP_INTERVAL = env_int("CACHE_CLEANUP_INTERVAL", 300)


# ----------  workers  ----------
def _prune_all_caches(application=None):
    _prune_count_cache()
    _prune_page_cache()
    _prune_callback_resolve_cache()

    now_ccl = time.time()
    cutoff_ccl = now_ccl - _COUNT_CACHE_LOCKS_TTL
    stale_ccl = [k for k, (_, created) in _COUNT_CACHE_LOCKS.items() if created < cutoff_ccl]
    for k in stale_ccl:
        try:
            del _COUNT_CACHE_LOCKS[k]
        except Exception:
            pass
    if len(_COUNT_CACHE_LOCKS) > _COUNT_CACHE_LOCKS_MAX:
        drop = max(1, len(_COUNT_CACHE_LOCKS) // 4)
        for _ in range(drop):
            try:
                _COUNT_CACHE_LOCKS.pop(next(iter(_COUNT_CACHE_LOCKS)), None)
            except StopIteration:
                break

    now = time.time()
    # Bound PTB's per-user data first (needs the still-present timestamps):
    # PTB never evicts user_data/chat_data on its own, so without this a bot
    # with a growing user base leaks memory indefinitely. Users idle past
    # USER_DATA_TTL have their user_data dropped. Activity is recorded for
    # every update, so this only ever evicts genuinely inactive users.
    if application is not None and USER_DATA_TTL > 0:
        cutoff_ud = now - USER_DATA_TTL
        for uid in list(_USER_SESSION_ACTIVITY.keys()):
            try:
                if _USER_SESSION_ACTIVITY.get(uid, now) < cutoff_ud:
                    application.drop_user_data(uid)
                    _USER_SESSION_ACTIVITY.pop(uid, None)
            except Exception:
                pass

    cutoff_sk = now - _SESSION_ACTIVITY_TTL
    stale_sk = [k for k, ts in _USER_SESSION_ACTIVITY.items() if ts < cutoff_sk]
    for k in stale_sk:
        try:
            del _USER_SESSION_ACTIVITY[k]
        except Exception:
            pass
    if len(_USER_SESSION_ACTIVITY) > _USER_SESSION_ACTIVITY_MAX:
        drop = max(1, len(_USER_SESSION_ACTIVITY) // 4)
        for _ in range(drop):
            try:
                _USER_SESSION_ACTIVITY.pop(next(iter(_USER_SESSION_ACTIVITY)), None)
            except StopIteration:
                break

    cutoff_lc = now - 30.0
    stale_lc = [k for k, ts in _LAST_CALLBACK.items() if ts < cutoff_lc]
    for k in stale_lc:
        try:
            del _LAST_CALLBACK[k]
        except Exception:
            pass
    if len(_LAST_CALLBACK) > _LAST_CALLBACK_MAX:
        drop = max(1, len(_LAST_CALLBACK) // 4)
        for _ in range(drop):
            try:
                _LAST_CALLBACK.pop(next(iter(_LAST_CALLBACK)), None)
            except StopIteration:
                break

    _prune_user_buckets()

    stale_rq = [k for k, v in _RETRY_QUEUE.items() if v.done()]
    for k in stale_rq:
        try:
            del _RETRY_QUEUE[k]
        except Exception:
            pass


async def start_cache_cleanup_worker(application=None):
    logger.info(
        "Starting cache cleanup worker (interval=%ds)",
        CACHE_CLEANUP_INTERVAL,
    )
    while True:
        await asyncio.sleep(CACHE_CLEANUP_INTERVAL)
        try:
            before = time.time()
            _prune_all_caches(application)
            elapsed = time.time() - before
            logger.debug("Cache cleanup completed in %.3fs", elapsed)
        except Exception:
            logger.exception("Cache cleanup worker encountered an error")


async def start_redis_retry_worker(application):
    if _redis is None:
        logger.debug("Redis not configured; skipping redis retry worker")
        return

    async def _worker():
        logger.info("Starting Redis retry worker")
        while True:
            try:
                now = int(time.time())
                members = await _redis.zrangebyscore("retry:queue", "-inf", now, start=0, num=100)
                if not members:
                    await asyncio.sleep(1)
                    continue
                for raw in members:
                    removed = await _redis.zrem("retry:queue", raw)
                    if removed:
                        await _process_redis_retry_item(application, raw)
                await asyncio.sleep(0)
            except Exception:
                logger.exception("Redis retry worker encountered an error")
                await asyncio.sleep(2)

    _bg_task(_worker())


# ----------  message editing  ----------
async def safe_edit_message(
    query,
    text: str,
    reply_markup=None,
    action_key: str = None,
    debounce_interval: float = None,
    keep_photo: bool = False,
):
    try:
        # Guarantee unique callback_data so no two buttons can light up together.
        reply_markup = _dedupe_markup(reply_markup)
        user_id = getattr(query.from_user, "id", None) or getattr(query.message, "chat_id", None)
        key = action_key or getattr(query, "data", None) or "callback"
        if user_id and _is_debounced(user_id, key, debounce_interval):
            try:
                await safe_answer(query)
            except Exception:
                pass
            return False

        uid = user_id or 0
        ok, wait = await _consume_token(uid)
        if not ok:
            logger.info("Rate limit: scheduling retry in %s seconds for key=%s", wait, key)
            await schedule_retry_via_redis_or_local(query, text, reply_markup=reply_markup, delay=wait)
            try:
                await safe_answer(query, text=f"Too many requests. Retrying in {wait}s.")
            except Exception:
                pass
            return False

        _msg = getattr(query, "message", None)
        if _msg and getattr(_msg, "photo", None):
            if not keep_photo:
                # A text view must never keep a leftover design banner. Telegram
                # cannot remove media via edit, so rebuild the message as plain text.
                try:
                    await _msg.delete()
                    await _msg.reply_text(text, reply_markup=reply_markup)
                    return True
                except Exception:
                    logger.debug(
                        "safe_edit_message: photo strip failed, falling back to caption edit",
                        exc_info=True,
                    )
            await _msg.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await query.edit_message_text(text, reply_markup=reply_markup)
        return True
    except RetryAfter as e:
        wait = int(getattr(e, "retry_after", 1) or 1)
        logger.warning("Flood control exceeded. scheduling retry in %s seconds", wait)
        await schedule_retry_via_redis_or_local(query, text, reply_markup=reply_markup, delay=wait)
        try:
            await safe_answer(query, text=f"Too many requests. Will retry in {wait}s.")
        except Exception:
            pass
        return False
    except Exception as e:
        msg = str(e)
        if isinstance(e, BadRequest) and ("Message is not modified" in msg or "message is not modified" in msg):
            logger.debug("Edit skipped: message not modified")
            return True
        logger.exception("Error editing message")
        try:
            _fb_msg = getattr(query, "message", None)
            if _fb_msg and getattr(_fb_msg, "photo", None):
                try:
                    await _fb_msg.edit_caption(caption=text, reply_markup=None)
                except Exception:
                    pass
            else:
                await query.message.reply_text(text)
        except Exception:
            pass
        return False


async def safe_answer(query, text: str = None):
    try:
        # Every button tap is activity for that user: reset their own session
        # idle timer so their keyboard stays open until THEY go inactive.
        _touch_user_session(getattr(getattr(query, "from_user", None), "id", None))
    except Exception:
        pass
    try:
        if text is not None:
            await query.answer(text=text)
        else:
            await query.answer()
        return True
    except BadRequest as e:
        m = str(e)
        if "Query is too old" in m or "query id is invalid" in m:
            logger.debug("Ignoring expired callback query: %s", m)
            return False
        if "message is not modified" in m.lower():
            logger.debug("Ignoring 'message is not modified' while answering callback")
            return True
        logger.exception("BadRequest when answering callback")
        return False
    except Exception:
        logger.exception("Unexpected error when answering callback")
        return False


MAX_CATEGORY_NAME_LENGTH = 30
PAGE_SIZE = 50


# ----------  courses page builder  ----------
def build_courses_page(
    all_courses,
    page: int = 1,
    origin_type: str = "global",
    category: str = None,
    origin_context: str = None,
    origin_context_page: int = None,
    total_count: int = None,
    is_page: bool = False,
    store_page_ref: bool = False,
    search_ref: str = None,
    overall_total: int = None,
    page_first_cursor: dict = None,
    page_last_cursor: dict = None,
):
    page_size = PAGE_SIZE
    try:
        if is_page:
            display = list(all_courses) if all_courses is not None else []
            effective_total = total_count if total_count is not None else len(display)
            start = (page - 1) * page_size
        else:
            total_len = (
                total_count if total_count is not None else (len(all_courses) if hasattr(all_courses, "__len__") else 0)
            )
            start = (page - 1) * page_size
            display = all_courses[start : start + page_size]
            effective_total = total_len

        if not display:
            return None, None

        logger.debug(
            "build_courses_page called: origin_type=%s category=%s page=%s total=%s",
            origin_type,
            category,
            page,
            effective_total,
        )

        try:
            total_pages = math.ceil(effective_total / page_size) if effective_total is not None else page
        except Exception:
            total_pages = page

        if origin_type == "category" and category:
            text = f"Courses in category '{category}' (page {page}):"
        else:
            text = f"Here are the available courses (page {page}):"

        keyboard = []
        for c in display:
            try:
                course_cat = c.get("category") if isinstance(c, dict) else None
                if not course_cat:
                    course_cat = category
                name = c.get("name") if isinstance(c, dict) else None
                link = c.get("link") if isinstance(c, dict) else None
                if not name:
                    logger.debug("build_courses_page: skipping course without name: %s", repr(c))
                    continue
                make_origin_ctx = origin_context
                try:
                    if origin_type == "coach" and (not make_origin_ctx) and category:
                        make_origin_ctx = category
                except Exception:
                    make_origin_ctx = origin_context
                details_cb = _make_course_ref(
                    course_cat,
                    name,
                    origin_type,
                    page,
                    make_origin_ctx,
                    origin_context_page,
                    c.get("id") if isinstance(c, dict) else None,
                    search_ref,
                )
                keyboard.append(
                    [InlineKeyboardButton(name, url=link), InlineKeyboardButton("ℹ️ Details", callback_data=details_cb)],
                )
            except Exception:
                logger.exception("build_courses_page: error building row for course %s", repr(c))
                continue

    except Exception:
        logger.exception("build_courses_page: unexpected error")
        return None, None

    pagination_buttons = []
    if start > 0:
        if origin_type == "category" and category:
            if store_page_ref:
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, "__len__"):
                        start_prev = (page - 2) * page_size
                        if start_prev >= 0:
                            slice_items = all_courses[start_prev : start_prev + page_size]
                            items_to_store = []
                            for it in slice_items:
                                items_to_store.append(
                                    {
                                        "name": it.get("name") if isinstance(it, dict) else str(it),
                                        "link": it.get("link") if isinstance(it, dict) else None,
                                        "category": it.get("category") if isinstance(it, dict) else category,
                                        "id": str(it.get("id"))
                                        if isinstance(it, dict) and it.get("id") is not None
                                        else None,
                                    },
                                )
                    page_payload = {
                        "type": "courses_page",
                        "origin_type": origin_type,
                        "category": category,
                        "page": page - 1,
                        "origin_context": origin_context,
                        "origin_context_page": origin_context_page,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    if items_to_store is not None:
                        page_payload["items"] = items_to_store
                    key = _store_callback_payload(page_payload)
                    prev_cb = f"courses_ref::{key}"
                except Exception:
                    prev_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page - 1}"
                    if origin_context:
                        prev_cb = (
                            prev_cb
                            + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                        )
            else:
                prev_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page - 1}"
                if origin_context:
                    prev_cb = (
                        prev_cb
                        + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                    )
        elif origin_type == "coach" and category:
            prev_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page - 1}"
        else:
            prev_cb = f"courses::global::{page - 1}"
            # Keyset "previous": query the page before this page's first item.
            if page_first_cursor is not None:
                try:
                    payload = {
                        "type": "courses_page",
                        "origin_type": "global",
                        "category": None,
                        "page": max(1, page - 1),
                        "before": page_first_cursor,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    prev_cb = f"courses_ref::{_store_callback_payload(payload)}"
                except Exception:
                    prev_cb = f"courses::global::{page - 1}"
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=prev_cb))
    if page < total_pages:
        if origin_type == "category" and category:
            if store_page_ref:
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, "__len__"):
                        start_next = page * page_size
                        slice_items = all_courses[start_next : start_next + page_size]
                        items_to_store = []
                        for it in slice_items:
                            items_to_store.append(
                                {
                                    "name": it.get("name") if isinstance(it, dict) else str(it),
                                    "link": it.get("link") if isinstance(it, dict) else None,
                                    "category": it.get("category") if isinstance(it, dict) else category,
                                    "id": str(it.get("id"))
                                    if isinstance(it, dict) and it.get("id") is not None
                                    else None,
                                },
                            )
                    page_payload = {
                        "type": "courses_page",
                        "origin_type": origin_type,
                        "category": category,
                        "page": page + 1,
                        "origin_context": origin_context,
                        "origin_context_page": origin_context_page,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    if items_to_store is not None:
                        page_payload["items"] = items_to_store
                    key = _store_callback_payload(page_payload)
                    next_cb = f"courses_ref::{key}"
                except Exception:
                    next_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page + 1}"
                    if origin_context:
                        next_cb = (
                            next_cb
                            + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                        )
            else:
                next_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page + 1}"
                if origin_context:
                    next_cb = (
                        next_cb
                        + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                    )
        elif origin_type == "coach" and category:
            if store_page_ref:
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, "__len__"):
                        start_next = page * page_size
                        slice_items = all_courses[start_next : start_next + page_size]
                        items_to_store = []
                        for it in slice_items:
                            items_to_store.append(
                                {
                                    "name": it.get("name") if isinstance(it, dict) else str(it),
                                    "link": it.get("link") if isinstance(it, dict) else None,
                                    "category": it.get("category") if isinstance(it, dict) else category,
                                    "id": str(it.get("id"))
                                    if isinstance(it, dict) and it.get("id") is not None
                                    else None,
                                },
                            )
                    page_payload = {
                        "type": "courses_page",
                        "origin_type": origin_type,
                        "category": category,
                        "page": page + 1,
                        "origin_context": origin_context,
                        "origin_context_page": origin_context_page,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    if items_to_store is not None:
                        page_payload["items"] = items_to_store
                    key = _store_callback_payload(page_payload)
                    next_cb = f"courses_ref::{key}"
                except Exception:
                    next_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page + 1}"
            else:
                next_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page + 1}"
        else:
            next_cb = f"courses::global::{page + 1}"
            # Keyset "next": continue after this page's last item.
            if page_last_cursor is not None:
                try:
                    payload = {
                        "type": "courses_page",
                        "origin_type": "global",
                        "category": None,
                        "page": page + 1,
                        "after": page_last_cursor,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    next_cb = f"courses_ref::{_store_callback_payload(payload)}"
                except Exception:
                    next_cb = f"courses::global::{page + 1}"
        pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=next_cb))
    if pagination_buttons:
        keyboard.append(pagination_buttons)

    try:
        # Build the (possibly capped) course-row keyboard. Telegram caps an inline
        # keyboard at TELEGRAM_INLINE_KEYBOARD_LIMIT buttons in one message; each
        # course row uses 2 buttons (name + Details), so the rows that fit are the
        # spare button slots divided by 2. We count only the non-course buttons
        # already in `keyboard` (pagination_prev/next at this point) so a page with
        # few nav buttons can show more course rows than a busier page -- never a
        # fixed 42.
        non_course_buttons = 0
        for _row in keyboard:
            for _b in _row:
                non_course_buttons += 1
        spare_slots = max(0, TELEGRAM_INLINE_KEYBOARD_LIMIT - non_course_buttons)
        max_course_rows = max(1, spare_slots // 2)

        capped_display = display if len(display) <= max_course_rows else display[:max_course_rows]
        course_keyboard = []
        for c in capped_display:
            try:
                course_cat = c.get("category") if isinstance(c, dict) else None
                if not course_cat:
                    course_cat = category
                name = c.get("name") if isinstance(c, dict) else None
                link = c.get("link") if isinstance(c, dict) else None
                if not name:
                    continue
                details_cb = _make_course_ref(
                    course_cat,
                    name,
                    origin_type,
                    page,
                    origin_context,
                    origin_context_page,
                    c.get("id") if isinstance(c, dict) else None,
                    search_ref,
                )
                course_keyboard.append(
                    [InlineKeyboardButton(name, url=link), InlineKeyboardButton("ℹ️ Details", callback_data=details_cb)],
                )
            except Exception:
                continue
        keyboard = course_keyboard

    except Exception:
        logger.exception("build_courses_page: unexpected error building course rows")
        return None, None

    # Pagination callbacks (Prev / Next). Keyset cursors take precedence over the
    # simple page-number form when available, matching the original behaviour.
    pagination_buttons = []
    if start > 0:
        if origin_type == "category" and category:
            if store_page_ref:
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, "__len__"):
                        start_prev = (page - 2) * page_size
                        if start_prev >= 0:
                            slice_items = all_courses[start_prev : start_prev + page_size]
                            items_to_store = []
                            for it in slice_items:
                                items_to_store.append(
                                    {
                                        "name": it.get("name") if isinstance(it, dict) else str(it),
                                        "link": it.get("link") if isinstance(it, dict) else None,
                                        "category": it.get("category") if isinstance(it, dict) else category,
                                        "id": str(it.get("id"))
                                        if isinstance(it, dict) and it.get("id") is not None
                                        else None,
                                    }
                                )
                    page_payload = {
                        "type": "courses_page",
                        "origin_type": origin_type,
                        "category": category,
                        "page": page - 1,
                        "origin_context": origin_context,
                        "origin_context_page": origin_context_page,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    if items_to_store is not None:
                        page_payload["items"] = items_to_store
                    key = _store_callback_payload(page_payload)
                    prev_cb = f"courses_ref::{key}"
                except Exception:
                    prev_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page - 1}"
                    if origin_context:
                        prev_cb = (
                            prev_cb
                            + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                        )
            else:
                prev_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page - 1}"
                if origin_context:
                    prev_cb = (
                        prev_cb
                        + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                    )
        elif origin_type == "coach" and category:
            prev_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page - 1}"
        else:
            prev_cb = f"courses::global::{page - 1}"
            if page_first_cursor is not None:
                try:
                    payload = {
                        "type": "courses_page",
                        "origin_type": "global",
                        "category": None,
                        "page": max(1, page - 1),
                        "before": page_first_cursor,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    prev_cb = f"courses_ref::{_store_callback_payload(payload)}"
                except Exception:
                    prev_cb = f"courses::global::{page - 1}"
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=prev_cb))
    if page < total_pages:
        if origin_type == "category" and category:
            if store_page_ref:
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, "__len__"):
                        start_next = page * page_size
                        slice_items = all_courses[start_next : start_next + page_size]
                        items_to_store = []
                        for it in slice_items:
                            items_to_store.append(
                                {
                                    "name": it.get("name") if isinstance(it, dict) else str(it),
                                    "link": it.get("link") if isinstance(it, dict) else None,
                                    "category": it.get("category") if isinstance(it, dict) else category,
                                    "id": str(it.get("id"))
                                    if isinstance(it, dict) and it.get("id") is not None
                                    else None,
                                }
                            )
                    page_payload = {
                        "type": "courses_page",
                        "origin_type": origin_type,
                        "category": category,
                        "page": page + 1,
                        "origin_context": origin_context,
                        "origin_context_page": origin_context_page,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    if items_to_store is not None:
                        page_payload["items"] = items_to_store
                    key = _store_callback_payload(page_payload)
                    next_cb = f"courses_ref::{key}"
                except Exception:
                    next_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page + 1}"
                    if origin_context:
                        next_cb = (
                            next_cb
                            + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                        )
            else:
                next_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page + 1}"
                if origin_context:
                    next_cb = (
                        next_cb
                        + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                    )
        elif origin_type == "coach" and category:
            next_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page + 1}"
        else:
            next_cb = f"courses::global::{page + 1}"
            if page_last_cursor is not None:
                try:
                    payload = {
                        "type": "courses_page",
                        "origin_type": "global",
                        "category": None,
                        "page": page + 1,
                        "after": page_last_cursor,
                        "total_count": effective_total,
                        "page_size": page_size,
                    }
                    next_cb = f"courses_ref::{_store_callback_payload(payload)}"
                except Exception:
                    next_cb = f"courses::global::{page + 1}"
        pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=next_cb))
    if pagination_buttons:
        keyboard.append(pagination_buttons)

    try:
        total_pages = math.ceil(effective_total / page_size) if effective_total is not None else page
    except Exception:
        total_pages = page

    try:
        breadcrumb_buttons = None
        if origin_type == "global":
            if page > 1:
                breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data="courses::global::1")]
            else:
                breadcrumb_buttons = None
        elif origin_type == "coach" and category:
            if page > 1:
                breadcrumb_buttons = [
                    InlineKeyboardButton(
                        "🏠 Home",
                        callback_data=_courses_home_cb("coach", category),
                    ),
                ]
            else:
                breadcrumb_buttons = None
        elif origin_type == "category" and category:
            if page > 1:
                breadcrumb_buttons = [
                    InlineKeyboardButton(
                        "🏠 Home",
                        callback_data=_courses_home_cb("category", category),
                    ),
                ]
            else:
                breadcrumb_buttons = None
        else:
            breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data="back_to_cats")]

        if total_pages > 1 and page < total_pages:
            if origin_type == "category" and category:
                if store_page_ref:
                    try:
                        items_to_store = None
                        if not is_page and hasattr(all_courses, "__len__"):
                            start_end = (total_pages - 1) * page_size
                            slice_items = all_courses[start_end : start_end + page_size]
                            items_to_store = []
                            for it in slice_items:
                                items_to_store.append(
                                    {
                                        "name": it.get("name") if isinstance(it, dict) else str(it),
                                        "link": it.get("link") if isinstance(it, dict) else None,
                                        "category": it.get("category") if isinstance(it, dict) else category,
                                        "id": str(it.get("id"))
                                        if isinstance(it, dict) and it.get("id") is not None
                                        else None,
                                    },
                                )
                        page_payload = {
                            "type": "courses_page",
                            "origin_type": origin_type,
                            "category": category,
                            "page": total_pages,
                            "origin_context": origin_context,
                            "origin_context_page": origin_context_page,
                            "total_count": effective_total,
                            "page_size": page_size,
                        }
                        if items_to_store is not None:
                            page_payload["items"] = items_to_store
                        key = _store_callback_payload(page_payload)
                        end_cb = f"courses_ref::{key}"
                    except Exception:
                        end_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{total_pages}"
                        if origin_context:
                            end_cb = (
                                end_cb
                                + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                            )
                else:
                    end_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{total_pages}"
                    if origin_context:
                        end_cb = (
                            end_cb
                            + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                        )
            elif origin_type == "global":
                end_cb = f"courses::global::{total_pages}"
            elif origin_type == "coach" and category:
                if store_page_ref:
                    try:
                        items_to_store = None
                        if not is_page and hasattr(all_courses, "__len__"):
                            start_end = (total_pages - 1) * page_size
                            slice_items = all_courses[start_end : start_end + page_size]
                            items_to_store = []
                            for it in slice_items:
                                items_to_store.append(
                                    {
                                        "name": it.get("name") if isinstance(it, dict) else str(it),
                                        "link": it.get("link") if isinstance(it, dict) else None,
                                        "category": it.get("category") if isinstance(it, dict) else category,
                                        "id": str(it.get("id"))
                                        if isinstance(it, dict) and it.get("id") is not None
                                        else None,
                                    },
                                )
                        page_payload = {
                            "type": "courses_page",
                            "origin_type": origin_type,
                            "category": category,
                            "page": total_pages,
                            "origin_context": origin_context,
                            "origin_context_page": origin_context_page,
                            "total_count": effective_total,
                            "page_size": page_size,
                        }
                        if items_to_store is not None:
                            page_payload["items"] = items_to_store
                        key = _store_callback_payload(page_payload)
                        end_cb = f"courses_ref::{key}"
                    except Exception:
                        end_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{total_pages}"
                else:
                    end_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{total_pages}"
            else:
                end_cb = f"courses::global::{total_pages}"
            breadcrumb_buttons.append(InlineKeyboardButton("⏭️ End", callback_data=end_cb))

        if breadcrumb_buttons:
            keyboard.insert(0, breadcrumb_buttons)

    except Exception:
        pass


    try:
        if origin_type == "category":
            if not any((getattr(b, "text", "") == "🔙 Back") for row in keyboard for b in row):
                target = origin_context or category
                if target:
                    if origin_context == "categories":
                        back_cb = f"categories_page::{origin_context_page or 1}"
                    elif origin_context == "back_to_cats":
                        back_cb = "back_to_cats"
                    else:
                        back_page = origin_context_page if origin_context_page is not None else page
                        back_cb = _shorten_showcat_cb(str(target), back_page)
                else:
                    back_cb = "back_to_cats"
                logger.debug(
                    "build_courses_page: back_cb=%s origin_context=%s origin_context_page=%s origin_type=%s category=%s page=%s",
                    back_cb,
                    origin_context,
                    origin_context_page,
                    origin_type,
                    category,
                    page,
                )
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])
    except Exception:
        pass

    # Progress counter: courses actually rendered on this page / total courses
    # indexed in the bot (read from MongoDB), e.g. "... (page 1): [47/900]".
    # Counted from the real course buttons so it stays correct however many
    # courses the page ends up showing after its own size / button limits.
    try:
        if overall_total is not None and overall_total > 0:
            rendered = sum(
                1
                for row in keyboard
                for b in row
                if getattr(b, "text", None) == "ℹ️ Details"
            )
            text = f"{text} [{rendered}/{int(overall_total)}]"
    except Exception:
        pass

    return text, InlineKeyboardMarkup(_dedupe_keyboard(keyboard))


# ----------  browse commands  ----------
async def help_command(update: Update, context: CallbackContext):
    help_message = (
        "📚 **Course Navigator Bot** — I'll help you find and manage courses organized by coaches and categories!\n\n"
        "/start - Set your name and introduce yourself\n"
        "/add - Add a new course (choose a category, then a coach, then enter details)\n"
        "/courses - Browse all your saved courses\n"
        "/categories - Browse categories and find courses by coach\n"
        "/create_category - Create a new category or parent folder\n\n"
        "🎨 **Category Designs (Owner Only)**:\n"
        "/design_cat - Reply to a photo to assign it as a banner design for a parent category\n"
        "/remove_design - Remove a category's banner design\n\n"
        "/help - Show this help message\n"
        "/cancel - Cancel whatever you're currently doing\n"
    )
    await update.message.reply_text(help_message)


async def list_categories(update: Update, context: CallbackContext):
    try:
        await categories_page(update.message, context, page=1)
    except Exception:
        logger.exception("Error listing categories")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")


# ----------  category pages  ----------
async def createcat_page(update_or_message, context: CallbackContext, *, page: int = 1):
    query = getattr(update_or_message, "callback_query", None)
    is_query = query is not None
    if is_query:
        await safe_answer(query)
        data = query.data
        try:
            logger.debug("categories_page: raw callback_data=%r", data)
        except Exception:
            pass
        parts = data.split("::")
        try:
            page = int(parts[1])
        except Exception:
            page = 1


    try:
        context.user_data["createcat_last_page"] = page

    except Exception:
        pass

    logger.debug("createcat_page invoked: page=%s is_query=%s", page, is_query)
    try:
        db = await get_db()
        logger.debug("createcat_page: db=%s", getattr(db, "name", None))
        if db is None:
            if is_query:
                await safe_edit_message(
                    query,
                    "Error: Unable to connect to the database.",
                    action_key=getattr(query, "data", None),
                )
            else:
                await update_or_message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        async with _db_timing(f"createcat_page:{page}"):
            total = await _get_total_count(db, "categories", TOP_LEVEL_FILTER, ttl=30)
            cats = (
                await db.categories.find(TOP_LEVEL_FILTER)
                .sort("name", 1)
                .skip(start)
                .limit(page_size)
                .to_list(length=page_size)
            )
    except Exception:
        logger.exception("createcat_page: exception while fetching categories")
        cats = []
        total = 0

    page_cats = cats

    if not page_cats and not is_query:
        await update_or_message.reply_text("No categories available. Use /create_category to create one.")
        return

    keyboard = []
    keyboard.append([InlineKeyboardButton("(Top-level)", callback_data="createcat_parent::")])
    for cat in page_cats:
        keyboard.append(
            [
                InlineKeyboardButton(
                    cat.get("name"),
                    callback_data=_createcat_parent_cb(cat.get("name")),
                ),
            ],
        )
    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"createcat_page::{page - 1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"createcat_page::{page + 1}"))
    if total_pages > 1 and page < last_page:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"createcat_page::{last_page}"))

    if nav:
        keyboard.append(nav)

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = f"Select a parent category (page {page}/{last_page}):"
    if is_query:
        await safe_edit_message(query, title, reply_markup=reply_markup, action_key=getattr(query, "data", None))
    else:
        await update_or_message.reply_text(title, reply_markup=reply_markup)
    return


async def children_page(update_or_message, context: CallbackContext, parent: str, *, page: int = 1):
    query = getattr(update_or_message, "callback_query", None)
    is_query = query is not None
    if is_query:
        await safe_answer(query)
        data = query.data
        parts = data.split("::")
        if len(parts) > 2:
            try:
                page = int(parts[-1])
            except Exception:
                page = 1

    try:
        db = await get_db()
        if db is None:
            if is_query:
                await safe_edit_message(
                    query,
                    "Error: Unable to connect to the database.",
                    action_key=getattr(query, "data", None),
                )
            else:
                await update_or_message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        async with _db_timing(f"children_page:{parent}:{page}"):
            await _get_total_count(db, "categories", {"parent": parent}, ttl=30)
            children = (
                await db.categories.find({"parent": parent})
                .sort("name", 1)
                .skip(start)
                .limit(page_size)
                .to_list(length=page_size)
            )
    except Exception:
        children = []

    sorted_children = sorted(children, key=lambda c: (c.get("name") or "").lower())
    page_size = PAGE_SIZE
    page_children = sorted_children

    if not page_children:
        if is_query:
            await safe_edit_message(
                query,
                "No subcategories available on this page.",
                action_key=getattr(query, "data", None),
            )
        else:
            await update_or_message.reply_text("No subcategories available.")
        return

    child_names = [c.get("name") for c in page_children if c.get("name")]
    names_with_children = set()
    if child_names:
        try:
            names_with_children = await _get_children_flags(db, child_names, ttl=60)
        except Exception:
            names_with_children = set()

    keyboard = []
    try:
        parent_index = [
            {
                "path": (c.get("path") if c.get("path") is not None else None),
                "name": (c.get("name") if c.get("name") is not None else None),
            }
            for c in children
            if isinstance(c, dict) and (c.get("path") or c.get("name"))
        ]
    except Exception:
        parent_index = []

    for child in page_children:
        child_path = child.get("path") or child.get("name")
        payload = {
            "type": "showcat",
            "path": child_path,
            "from_parent": parent,
            "parent_page": page,
            "parent_index": parent_index,
        }
        key = _store_callback_payload(payload)
        try:
            if _redis is not None:
                _bg_task(_prefetch_category_page(child_path, page=1))
        except Exception:
            pass
        try:
            has_children = child.get("name") in names_with_children
            courses = child.get("courses", []) if isinstance(child, dict) else []
            is_empty = (not has_children) and (not _has_real_courses(courses))
        except Exception:
            is_empty = True
        display = f"{child.get('name')}{' (empty)' if is_empty else ''}"
        keyboard.append([InlineKeyboardButton(display, callback_data=f"showcat_ref::{key}")])

    nav = []
    total_pages = (len(children) - 1) // page_size + 1 if children else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=_shorten_showcat_cb(parent, page - 1)))
        nav.append(InlineKeyboardButton("🏠 Home", callback_data=_shorten_showcat_cb(parent, 1)))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=_shorten_showcat_cb(parent, page + 1)))
    if total_pages > 1 and page < last_page:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=_shorten_showcat_cb(parent, last_page)))
    if nav:
        keyboard.append(nav)

    pdoc = await db.categories.find_one({"name": parent})
    ppath = pdoc.get("path") if pdoc and pdoc.get("path") else parent
    keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=_shorten_showcat_cb(ppath, page))])
    try:
        keyboard.append([InlineKeyboardButton("🔍 Search", callback_data=f"search_categories::{page}")])
    except Exception:
        pass

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = f"Subcategories of '{parent}' (page {page}/{last_page}):"
    if is_query:
        await safe_edit_message(query, title, reply_markup=reply_markup, action_key=getattr(query, "data", None))
    else:
        await update_or_message.reply_text(title, reply_markup=reply_markup)
    return


async def categories_page(update_or_message, context: CallbackContext, *, page: int = 1):
    query = getattr(update_or_message, "callback_query", None)
    is_query = query is not None
    if is_query:
        await safe_answer(query)
        data = query.data
        parts = data.split("::")
        try:
            page = int(parts[1])
        except Exception:
            page = 1

    try:
        db = await get_db()
        if db is None:
            if is_query:
                await safe_edit_message(
                    query,
                    "Error: Unable to connect to the database.",
                    action_key=getattr(query, "data", None),
                )
            else:
                await update_or_message.reply_text("Error: Unable to connect to the database.")
            return

        page_size = PAGE_SIZE
        async with _db_timing(f"categories_page:{page}"):
            filter_q = TOP_LEVEL_FILTER
            total = await _get_total_count(db, "categories", filter_q, ttl=30)
            try:
                total_pages = max(1, (total - 1) // page_size + 1) if total else 1
                if page < 1:
                    page = 1
                elif page > total_pages:
                    page = total_pages
            except Exception:
                pass
            start = (page - 1) * page_size
            proj = {"name": 1, "path": 1, "parent": 1, "_id": 1}
            raw = (
                await db.categories.find(filter_q, proj)
                .sort("name", 1)
                .skip(start)
                .limit(page_size + 1)
                .to_list(length=page_size + 1)
            )
        have_more = len(raw) > page_size
        cats = raw[:page_size] if have_more else raw
        logger.debug(
            "categories_page: requested page=%s total_est=%s got=%s have_more=%s",
            page,
            total,
            len(cats),
            have_more,
        )
        page_cats = cats
        if not page_cats and total == 0:
            try:
                alt_filter = TOP_LEVEL_FILTER
                async with _db_timing(f"categories_page:fallback:{page}"):
                    alt_total = await _get_total_count(db, "categories", alt_filter, ttl=30)
                    alt_raw = (
                        await db.categories.find(alt_filter, proj)
                        .sort("name", 1)
                        .skip(start)
                        .limit(page_size + 1)
                        .to_list(length=page_size + 1)
                    )
                alt_have_more = len(alt_raw) > page_size
                alt_cats = alt_raw[:page_size] if alt_have_more else alt_raw
                logger.debug("categories_page: fallback total_est=%s got=%s", alt_total, len(alt_cats))
                if alt_cats:
                    page_cats = alt_cats
            except Exception:
                page_cats = []

        keyboard = []
        for cat in page_cats:
            cat_path = cat.get("path") or cat.get("name")
            payload = {"type": "showcat", "path": cat_path, "from_parent": "categories", "parent_page": page}
            key = _store_callback_payload(payload)
            try:
                logger.debug("categories_page: created showcat_ref key=%s path=%s parent_page=%s", key, cat_path, page)
            except Exception:
                pass

            display_name = cat.get("name") if isinstance(cat, dict) else str(cat)
            cb = f"showcat_ref::{key}"
            try:
                logger.debug("categories_page: button name=%r path=%r", display_name, cat_path)
            except Exception:
                pass

            try:
                if _redis is not None:
                    _bg_task(_prefetch_category_page(cat_path, page=1))
            except Exception:
                pass

            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

    except Exception:
        try:
            logger.exception("categories_page: unexpected error building page")
        except Exception:
            pass
        if is_query:
            await safe_edit_message(query, "Error: failed to load categories.", action_key=getattr(query, "data", None))
        else:
            await update_or_message.reply_text("Error: failed to load categories.")
        return

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"categories_page::{page - 1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"categories_page::{page + 1}"))
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"categories_page::{last_page}"))
    if nav:
        keyboard.append(nav)

    try:
        if page > 1:
            breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data="categories_page::1")]
            keyboard.insert(0, breadcrumb_buttons)
    except Exception:
        pass

    keyboard.append([InlineKeyboardButton("🔍 Search", callback_data=f"search_categories::{page}")])

    try:
        results_row = _back_to_results_row(context)
        if results_row:
            keyboard.append(results_row)
    except Exception:
        pass

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = f"Tap a category to see its courses (page {page}/{last_page}):"
    if is_query:
        await safe_edit_message(query, title, reply_markup=reply_markup, action_key=getattr(query, "data", None))
    else:
        msg = await update_or_message.reply_text(title, reply_markup=reply_markup)
        try:
            schedule_close_inline_message(msg, user_id=_event_user_id(update_or_message))
        except Exception:
            pass
    return


# ----------  debug  ----------
async def debug_db(update: Update, context: CallbackContext):
    try:
        owner_env = os.getenv("BOT_OWNER_ID")
        if owner_env is None:
            await update.message.reply_text("Debug not allowed: BOT_OWNER_ID not configured.")
            return
        try:
            owner_id = int(owner_env)
        except Exception:
            owner_id = None
        user_id = getattr(update.effective_user, "id", None)
        if owner_id is not None and user_id != owner_id:
            await update.message.reply_text("Unauthorized")
            return
    except Exception:
        pass

    try:
        db = await get_db()
    except Exception as e:
        await update.message.reply_text(f"DB error: {e}")
        return

    try:
        cat_count = await _get_total_count(db, "categories", {}, ttl=60)
    except Exception as e:
        cat_count = f"error: {e}"

    sample = None
    try:
        sample = await db.categories.find_one({}, projection={"name": 1, "parent": 1, "path": 1, "courses": 1})
    except Exception as e:
        sample = f"error: {e}"

    try:
        indexes = await db.categories.index_information()
    except Exception as e:
        indexes = f"error: {e}"

    msg = f"categories_count: {cat_count}\nindexes: {list(indexes.keys()) if isinstance(indexes, dict) else indexes}\nsample: {sample}"
    if len(msg) > 4000:
        msg = msg[:3990] + "..."
    await update.message.reply_text(msg)


# ----------  coach views  ----------
async def show_coach_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    raw = getattr(query, "data", "") or ""
    page = 1
    page_size = PAGE_SIZE
    coach_slug = None
    try:
        if raw.startswith("coach::"):
            parts = raw.split("::")
            if len(parts) >= 2:
                coach_slug = urllib.parse.unquote_plus(parts[1])
            if len(parts) >= 3:
                try:
                    page = int(parts[2])
                except Exception:
                    page = 1
        elif raw.startswith("coach_"):
            try:
                coach_slug = urllib.parse.unquote_plus(raw.split("_", 1)[1])
            except Exception:
                coach_slug = raw
        else:
            coach_slug = urllib.parse.unquote_plus(raw)
    except Exception:
        coach_slug = urllib.parse.unquote_plus(raw)

    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return

    coach_name = None
    try:
        if hasattr(db, "coaches"):
            coach_doc = await db.coaches.find_one({"slug": coach_slug})
            if coach_doc:
                coach_name = coach_doc.get("name")
    except Exception:
        coach_name = None

    if not coach_name:
        coach_name = urllib.parse.unquote_plus(coach_slug)

    try:
        filter_q = {"$or": [{"courses.coach": coach_name}, {"name": coach_name}]}
        start = (page - 1) * page_size
        items_pipeline = [
            {"$match": filter_q},
            {"$unwind": "$courses"},
            {
                "$project": {
                    "name": "$courses.name",
                    "link": "$courses.link",
                    "category": "$name",
                    "coach": "$courses.coach",
                    "id": {"$ifNull": ["$courses.id", None]},
                },
            },
            {"$match": {"$or": [{"coach": coach_name}, {"category": coach_name}]}},
            {"$sort": {"name": 1}},
            {"$skip": start},
            {"$limit": page_size + 1},
        ]
        async with _db_timing(f"show_coach:{coach_name}:{page}"):
            cache_key = f"page:coach:{coach_name}:{page}"
            cached = _get_cached_page(cache_key)
            if cached is not None:
                items = cached
            else:
                try:
                    items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                except Exception:
                    items = []
                try:
                    _set_cached_page(cache_key, items, ttl=3)
                except Exception:
                    pass
        has_more = len(items) > page_size
        coach_courses = items[:page_size]
        coach_courses = sorted(coach_courses, key=lambda c: (c.get("name") or "").lower())
        try:
            cnt_doc = await db.categories.aggregate(
                [
                    {"$match": {"courses.coach": coach_name}},
                    {"$unwind": "$courses"},
                    {"$match": {"courses.coach": coach_name}},
                    {"$group": {"_id": None, "count": {"$sum": 1}}},
                ],
            ).to_list(length=1)
            total_courses = (
                int(cnt_doc[0].get("count"))
                if cnt_doc
                else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(coach_courses)))
            )
        except Exception:
            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(coach_courses))

        text, reply_markup = build_courses_page(
            coach_courses,
            page=page,
            origin_type="coach",
            category=coach_name,
            origin_context=None,
            total_count=total_courses,
            is_page=True,
            store_page_ref=True,
        )
        try:
            kb = list(reply_markup.inline_keyboard)
            kb.append(
                [
                    InlineKeyboardButton(
                        "\U0001f50d Search",
                        callback_data=_search_courses_coach_cb(coach_name, page),
                    ),
                ],
            )
            results_row = _back_to_results_row(context)
            if results_row:
                kb.append(results_row)
            reply_markup = InlineKeyboardMarkup(kb)
        except Exception:
            pass
        if text and reply_markup:
            await safe_edit_message(query, text, reply_markup=reply_markup, action_key=raw)
        else:
            await safe_edit_message(query, f"No courses found for coach '{coach_name}'.", action_key=raw)
    except Exception:
        logger.exception("Error showing coach courses")
        await safe_edit_message(
            query,
            "An unexpected error occurred. Please try again later.",
            action_key=getattr(query, "data", None),
        )


async def show_coach_in_category(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    parent_origin = None
    parent_origin_page = None
    search_ref = None
    if data.startswith("coach_in_cat_ref::"):
        key = data.split("::", 1)[1]
        payload = await _resolve_callback_payload(key)
        if not payload:
            _clear_design_pending(context)
            await safe_edit_message(
                query,
                "Reference expired. Please open the list again.",
                action_key=getattr(query, "data", None),
            )
            return
        category = payload.get("category")
        coach_slug = payload.get("coach_slug")
        page = int(payload.get("page", 1) or 1)
        type_slug = payload.get("type_slug")
        parent_origin = payload.get("from_parent")
        search_ref = payload.get("search_ref") or None
        try:
            parent_origin_page = int(payload.get("parent_page")) if payload.get("parent_page") is not None else None
        except Exception:
            parent_origin_page = None
    else:
        if "::" not in data:
            await safe_edit_message(query, "Invalid coach callback.", action_key=getattr(query, "data", None))
            return
        parts = data.split("::")
        if len(parts) < 3:
            await safe_edit_message(query, "Invalid coach callback.", action_key=getattr(query, "data", None))
            return
            return
        category = urllib.parse.unquote_plus(parts[1])
        coach_slug = urllib.parse.unquote_plus(parts[2])
        type_slug = None
        page = 1
        if len(parts) >= 4:
            maybe = parts[3]
            try:
                page = int(maybe)
            except Exception:
                type_slug = urllib.parse.unquote_plus(maybe)
        if len(parts) >= 5:
            try:
                page = int(parts[4])
            except Exception:
                pass

    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return

    coach_name = None
    try:
        if hasattr(db, "coaches"):
            coach_doc = await db.coaches.find_one({"slug": coach_slug})
            if coach_doc:
                coach_name = coach_doc.get("name")
    except Exception:
        coach_name = None
    if not coach_name:
        coach_name = coach_slug

    try:
        coach_child = await db.categories.find_one({"name": coach_name, "parent": category})
        coach_courses = []
        if coach_child:
            for crs in coach_child.get("courses", []):
                coach_courses.append({"name": crs.get("name"), "link": crs.get("link"), "category": coach_name})
            try:
                last_view = coach_child.get("path") or coach_child.get("name")
                context.user_data["last_viewed_category"] = last_view
                try:
                    if coach_child.get("id"):
                        context.user_data["last_viewed_category_id"] = coach_child.get("id")
                except Exception:
                    pass
                try:
                    if coach_child.get("parent"):
                        context.user_data["last_viewed_category_parent"] = coach_child.get("parent")
                except Exception:
                    pass
            except Exception:
                pass
        else:
            category_doc = await db.categories.find_one({"name": category})
            if not category_doc or not category_doc.get("courses"):
                await safe_edit_message(
                    query,
                    f"No courses found in category '{category}'.",
                    action_key=getattr(query, "data", None),
                )
                return

            for crs in category_doc.get("courses", []):
                if type_slug:
                    c_type = crs.get("type") or crs.get("category_type") or crs.get("categoryType")
                    if not c_type:
                        continue
                    if urllib.parse.quote_plus(str(c_type)) != type_slug:
                        continue
                if crs.get("coach"):
                    if crs.get("coach") == coach_name:
                        coach_courses.append({"name": crs.get("name"), "link": crs.get("link"), "category": category})

        coach_courses = sorted(coach_courses, key=lambda c: (c.get("name") or "").lower())
        origin_ctx = None
        try:
            cdoc = await db.categories.find_one({"name": category})
            if cdoc:
                parent = cdoc.get("parent")
                if parent:
                    pdoc = await db.categories.find_one({"name": parent})
                    origin_ctx = pdoc.get("path") if pdoc and pdoc.get("path") else parent
        except Exception:
            origin_ctx = None

        if parent_origin:
            origin_ctx = parent_origin
        total_courses = len(coach_courses)
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        page_items = coach_courses[start : start + page_size]
        text, reply_markup = build_courses_page(
            page_items,
            page=page,
            origin_type="coach",
            category=coach_name,
            origin_context=origin_ctx,
            origin_context_page=parent_origin_page,
            total_count=total_courses,
            is_page=True,
            store_page_ref=True,
            search_ref=search_ref,
        )
        try:
            kb = list(reply_markup.inline_keyboard)
            kb.append(
                [
                    InlineKeyboardButton(
                        "\U0001f50d Search",
                        callback_data=_search_category_courses_cb(category, page),
                    ),
                ],
            )
            results_row = _back_to_results_row(context, search_ref)
            if results_row:
                kb.append(results_row)
            reply_markup = InlineKeyboardMarkup(kb)
        except Exception:
            pass
        if not text:
            await safe_edit_message(
                query,
                f"No courses found for coach '{coach_name}' in '{category}'.",
                action_key=getattr(query, "data", None),
            )
            return
        try:
            _set_session_keep_open(query.message, True)
        except Exception:
            pass
        try:
            design = await _resolve_design_for_category_name(db, category)
        except Exception:
            design = None
        await _send_design_photo(
            query, context, text, reply_markup, force_design=design
        )
    except Exception:
        logger.exception("Error fetching coach courses in category")
        await safe_edit_message(
            query,
            "An unexpected error occurred. Please try again later.",
            action_key=getattr(query, "data", None),
        )


# ----------  types & search nav  ----------
async def showtype_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)

    raw = query.data
    category_name = ""
    type_name = ""
    search_ref = None
    if raw.startswith("showtype_ref::"):
        try:
            payload = await _resolve_callback_payload(raw.split("::", 1)[1])
            if payload:
                category_name = payload.get("category") or ""
                type_name = payload.get("type_name") or ""
                search_ref = payload.get("search_ref") or None
        except Exception:
            category_name = ""
            type_name = ""
    else:
        data = raw.split("::")
        if len(data) < 3:
            await safe_edit_message(query, "Invalid type callback.", action_key=getattr(query, "data", None))
            return
        _, encoded_category, encoded_type = data[:3]
        category_name = urllib.parse.unquote_plus(encoded_category)
        type_name = urllib.parse.unquote_plus(encoded_type)
    if not category_name or not type_name:
        await safe_edit_message(query, "Invalid type callback.", action_key=getattr(query, "data", None))
        return

    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return

    try:
        category_doc = await db.categories.find_one({"name": category_name})
        if not category_doc or not category_doc.get("courses"):
            await safe_edit_message(
                query,
                f"No courses found in category '{category_name}'.",
                action_key=getattr(query, "data", None),
            )
            return

        filtered_courses = []
        for crs in category_doc.get("courses", []):
            c_type = crs.get("type") or crs.get("category_type") or crs.get("categoryType")
            if c_type and str(c_type) == type_name:
                filtered_courses.append(
                    {
                        "name": crs.get("name"),
                        "link": crs.get("link"),
                        "category": category_name,
                    },
                )

        filtered_courses = sorted(filtered_courses, key=lambda c: (c.get("name") or "").lower())

        origin_ctx = None
        try:
            cdoc = await db.categories.find_one({"name": category_name})
            if cdoc:
                parent = cdoc.get("parent")
                if parent:
                    pdoc = await db.categories.find_one({"name": parent})
                    origin_ctx = pdoc.get("path") if pdoc and pdoc.get("path") else parent
        except Exception:
            origin_ctx = None

        text, reply_markup = build_courses_page(
            filtered_courses,
            page=1,
            origin_type="category",
            category=category_name,
            origin_context=origin_ctx,
            search_ref=search_ref,
        )
        try:
            kb = list(reply_markup.inline_keyboard)
            kb.append(
                [
                    InlineKeyboardButton(
                        "\U0001f50d Search",
                        callback_data=_search_category_courses_cb(category_name, 1),
                    ),
                ],
            )
            results_row = _back_to_results_row(context, search_ref)
            if results_row:
                kb.append(results_row)
            reply_markup = InlineKeyboardMarkup(kb)
        except Exception:
            pass

        if not text:
            await safe_edit_message(
                query,
                f"No courses found for type '{type_name}' in '{category_name}'.",
                action_key=getattr(query, "data", None),
            )
            return

        try:
            _set_session_keep_open(query.message, True)
        except Exception:
            pass
        try:
            design = await _resolve_design_for_category_name(db, category_name)
        except Exception:
            design = None
        await _send_design_photo(
            query, context, text, reply_markup, force_design=design
        )

    except Exception:
        logger.exception("Error showing type courses")
        await safe_edit_message(
            query,
            "An unexpected error occurred. Please try again later.",
            action_key=getattr(query, "data", None),
        )


# ----------  inline keyboard hygiene  ----------
def _dedupe_keyboard(keyboard):
    """Drop inline buttons whose callback_data duplicates an earlier one.

    Telegram clients cannot tell two buttons with identical callback_data
    apart, so tapping one can light up BOTH (the 'two buttons glow together'
    bug) and both send the same action. The classic trigger is a nav panel
    that uses the same helper for Previous(page-1) and Home(page 1): on page 2
    those produce byte-identical data. Keeping the first occurrence guarantees
    every button is uniquely addressable.
    """
    seen = set()
    out = []
    try:
        for row in keyboard or []:
            new_row = []
            for b in row:
                data = getattr(b, "callback_data", None)
                if data is None:
                    new_row.append(b)
                    continue
                if data in seen:
                    logger.debug("Dropping duplicate callback_data=%r (%r)", data, getattr(b, "text", None))
                    continue
                seen.add(data)
                new_row.append(b)
            if new_row:
                out.append(new_row)
    except Exception:
        return keyboard
    return out


def _dedupe_markup(reply_markup):
    """Return reply_markup with duplicate callback_data buttons removed."""
    try:
        if reply_markup is None:
            return None
        rows = getattr(reply_markup, "inline_keyboard", None)
        if not rows:
            return reply_markup
        deduped = _dedupe_keyboard(rows)
        if len(deduped) == len(rows) and all(len(a) == len(b) for a, b in zip(deduped, rows)):
            return reply_markup
        return InlineKeyboardMarkup(deduped)
    except Exception:
        return reply_markup


# ----------  category design caching / inheritance  ----------
# Cache resolved designs (incl. inherited ones) so browsing does not re-query
# on every hop and, more importantly, so a child view keeps its parent's theme
# instead of flickering the image away. Invalidated when a design is set/removed.
_DESIGN_CACHE = {}
_DESIGN_CACHE_MAX = 5000
_DESIGN_CACHE_TTL = env_int("DESIGN_CACHE_TTL", 300)


def _design_cache_lookup(key):
    """Return (found, value) so a cached `None` (no design) is distinguishable."""
    try:
        entry = _DESIGN_CACHE.get(key)
        if entry is None:
            return False, None
        value, expires = entry
        if expires > time.time():
            return True, value
        _DESIGN_CACHE.pop(key, None)
        return False, None
    except Exception:
        return False, None


def _design_cache_store(key, value):
    try:
        _DESIGN_CACHE[key] = (value, time.time() + _DESIGN_CACHE_TTL)
        if len(_DESIGN_CACHE) > _DESIGN_CACHE_MAX:
            drop = max(1, len(_DESIGN_CACHE) // 4)
            for _ in range(drop):
                try:
                    _DESIGN_CACHE.pop(next(iter(_DESIGN_CACHE)), None)
                except StopIteration:
                    break
    except Exception:
        pass


def invalidate_design_cache(category_name=None):
    """Drop cached designs. Designs change rarely, so a full clear is fine."""
    try:
        _DESIGN_CACHE.clear()
    except Exception:
        pass


async def _design_own(db, name, cid):
    """The category's OWN design (not inherited), cached under an 'own:' key.

    Kept separate from the inherited value so resolving a child never reads a
    parent's inherited theme as if it were the parent's own.
    """
    try:
        from handlers.category_design import get_category_design
    except Exception:
        return None
    try:
        key = "own:" + str(cid or name)
        found, value = _design_cache_lookup(key)
        if found:
            return value
        value = await get_category_design(db, name)
        _design_cache_store(key, value)
        return value
    except Exception:
        return None


async def _resolve_branch_design(db, category_doc) -> str | None:
    """Resolve the design that a category VIEW should display, and cache it

    at process level (`_DESIGN_CACHE`) AND at the user level so that every
    subsequent view inside the same category branch reuses the same file_id.

    For a parent category this is its own/inherited design. For a child, the
    design displayed is the child's own/inherited design (one-hop parent scope).
    The key point is that we resolve ONCE per category doc id and reuse across
    all the parent's coaches/courses/subcategory/type/empty views, so the image
    never re-sends on every pagination step.
    """
    try:
        if not category_doc:
            return None
        cid = category_doc.get("id")
        name = category_doc.get("name") or ""
        cache_key = "resolved:" + str(cid or name)
        found, value = _design_cache_lookup(cache_key)
        if found:
            return value
        own = await _design_own(db, name, cid)
        if own:
            _design_cache_store(cache_key, own)
            return own
        parent = category_doc.get("parent")
        inherited = None
        if parent:
            try:
                pdoc = await db.categories.find_one(
                    {"name": parent},
                    projection={"name": 1, "id": 1},
                )
            except Exception:
                pdoc = None
            if pdoc:
                inherited = await _design_own(db, pdoc.get("name"), pdoc.get("id"))
        _design_cache_store(cache_key, inherited)
        return inherited
    except Exception:
        return None


async def _resolve_category_design(db, category_doc):
    """Design file_id for a category, inheriting its IMMEDIATE parent's design.

    Strictly parent-scoped: a child with no design of its own shows only its
    direct parent's OWN design. There is no deeper ancestor walk, so switching
    to a different parent (or to a top-level category) resolves to None and the
    image is removed rather than a grandparent's theme leaking across branches.
    """
    try:
        if not category_doc:
            return None
    except Exception:
        return None
    try:
        key = "resolved:" + str(category_doc.get("id") or category_doc.get("name"))
        found, value = _design_cache_lookup(key)
        if found:
            return value

        own = await _design_own(db, category_doc.get("name"), category_doc.get("id"))
        if own:
            _design_cache_store(key, own)
            return own

        # One hop only: the immediate parent's own design.
        parent = category_doc.get("parent")
        inherited = None
        if parent:
            try:
                pdoc = await db.categories.find_one(
                    {"name": parent},
                    projection={"name": 1, "id": 1},
                )
            except Exception:
                pdoc = None
            if pdoc:
                inherited = await _design_own(db, pdoc.get("name"), pdoc.get("id"))

        _design_cache_store(key, inherited)
        return inherited
    except Exception:
        return None


def _clear_design_pending(context):
    try:
        context.user_data.pop("_pending_design", None)
        context.user_data.pop("_pending_design_key", None)
    except Exception:
        pass


async def _resolve_design_for_category_name(db, category_name: str) -> str | None:
    """Resolve the design (own or inherited from immediate parent) for a category

    given only its name. Used by paginated / insider views (courses_callback,
    show_coach_in_category, showtype_handler) so a view inside a designed parent
    keeps the parent's banner instead of regenning or dropping it.
    """
    try:
        if not category_name:
            return None
        cat_doc = await db.categories.find_one(
            {"$or": [{"name": category_name}, {"path": category_name}]},
            projection={"name": 1, "id": 1, "parent": 1},
        )
        if not cat_doc:
            return None
        return await _resolve_branch_design(db, cat_doc)
    except Exception:
        return None


SEARCH_NAV_TTL = 3600
# Telegram caps an inline keyboard at 100 buttons in a single message. Course rows
# use 2 buttons each (name + Details), so the max course rows on a page = the
# number of spare button slots divided by 2. We compute this dynamically from the
# actual non-course buttons on the page so pages are never forced to a fixed 42.
TELEGRAM_INLINE_KEYBOARD_LIMIT = 100

SEARCH_NAV_USER_KEY = "search_results_nav"


def _store_search_nav_ref(context, ref: str):
    try:
        context.user_data[SEARCH_NAV_USER_KEY] = {"ref": ref, "ts": time.time()}
    except Exception:
        pass


def _get_search_nav_ref(context) -> str | None:
    try:
        nav = context.user_data.get(SEARCH_NAV_USER_KEY) or {}
        ref = nav.get("ref")
        ts = nav.get("ts") or 0
        if ref and (time.time() - ts) <= SEARCH_NAV_TTL:
            return ref
    except Exception:
        pass
    return None


def _back_to_results_row(context, search_ref: str = None):
    ref = search_ref or _get_search_nav_ref(context)
    if not ref:
        return None
    try:
        return [InlineKeyboardButton("🔙 Back to Search Results", callback_data=f"back_to_results::{ref}")]
    except Exception:
        return None


async def _send_design_photo(query, context, text, reply_markup, force_design: str = None):
    """Render a category view, showing its own (or inherited) design photo.

    The design must never leak from an unrelated branch: when the view resolves
    to a design we show exactly that file_id, and when no design applies we fall
    back to plain text so a stale banner does not persist.

    Within the same category branch, the design file_id is resolved once (by the
    entry view) and stored in `context.user_data["_branch_design"]`. Every
    sub-view reuses it, so the photo is only re-captioned in place rather than
    deleted and re-sent — there is no disappear/reappear flicker while paging
    through coaches/courses/subcategories/types of a parent that holds the image.

    `force_design` overrides any pending/branch design (used when a caller
    explicitly wants a specific file_id shown).
    """
    try:
        reply_markup = _dedupe_markup(reply_markup)

        # 1) Explicit override wins.
        design = force_design

        # 2) A freshly-set pending design (from this update's own resolution)
        #    overrides the older branch design so the view always shows the right
        #    thing even when the branch design changes between taps.
        pending_design = context.user_data.pop("_pending_design", None)
        if design is None and pending_design is not None:
            design = pending_design

        # 3) Reuse the branch design that the entry view already resolved.
        if design is None:
            design = context.user_data.get("_branch_design")

        # 4) Stability: do not regenerate a banner that is already showing.
        #    If the current message already carries the design photo we want,
        #    every subsequent tap edits the caption in place instead of deleting
        #    and re-sending the same image (that is the "bounce" the user sees).
        current_photo = None
        try:
            if query.message is not None and getattr(query.message, "photo", None):
                current_photo = query.message.photo[-1].file_id
        except Exception:
            current_photo = None
        already_showing_design = design is not None and current_photo == design

        show_photo = design is not None
        # Media must be rebuilt only when switching text<->photo or to a
        # *different* photo. edit_caption can only keep the current media.
        needs_rebuild = (
            show_photo and not already_showing_design and current_photo is not None
        ) or (not show_photo and current_photo is not None)

        if needs_rebuild:
            # --- Rate limiting (matches safe_edit_message) ---
            try:
                user_id = getattr(query.from_user, "id", None) or getattr(
                    query.message, "chat_id", None
                )
                key = getattr(query, "data", None) or "send_photo"
                if user_id and _is_debounced(user_id, key):
                    if design is not None:
                        context.user_data["_pending_design"] = design
                    elif pending_design is not None:
                        context.user_data["_pending_design"] = pending_design
                    try:
                        await safe_answer(query)
                    except Exception:
                        pass
                    return

                uid = user_id or 0
                ok, wait = await _consume_token(uid)
                if not ok:
                    if design is not None:
                        context.user_data["_pending_design"] = design
                    elif pending_design is not None:
                        context.user_data["_pending_design"] = pending_design
                    await schedule_retry_via_redis_or_local(
                        query, text, reply_markup=reply_markup, delay=wait
                    )
                    try:
                        await safe_answer(
                            query, text=f"Too many requests. Retrying in {wait}s."
                        )
                    except Exception:
                        pass
                    return

                try:
                    await query.message.delete()
                    if show_photo:
                        await context.bot.send_photo(
                            chat_id=query.message.chat_id,
                            photo=design,
                            caption=text,
                            reply_markup=reply_markup,
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=text,
                            reply_markup=reply_markup,
                        )
                    return
                except Exception:
                    # If deleting the old message fails we must not leave the
                    # user on a caption-less photo; fall back to caption edit
                    # when possible, otherwise send a fresh message.
                    if show_photo and current_photo == design:
                        try:
                            await query.message.edit_caption(
                                caption=text, reply_markup=reply_markup
                            )
                            return
                        except Exception:
                            pass
                    pass
            except Exception:
                pass

        if already_showing_design:
            try:
                await query.message.edit_caption(
                    caption=text, reply_markup=reply_markup
                )
                return
            except Exception:
                pass

        # No existing message to recaption: send a new message with the design
        # (or a plain message if the branch has no design).
        if design is not None:
            try:
                chat_id = (
                    getattr(query.message, "chat_id", None)
                    or getattr(getattr(query, "message", None), "chat_id", None)
                    or getattr(query, "chat_id", None)
                )
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=design,
                    caption=text,
                    reply_markup=reply_markup,
                )
                return
            except Exception:
                pass

        await safe_edit_message(
            query, text=text, reply_markup=reply_markup, action_key=getattr(query, "data", None)
        )
    except Exception:
        try:
            await safe_edit_message(
                query,
                text=text,
                reply_markup=reply_markup,
                action_key=getattr(query, "data", None),
            )
        except Exception:
            pass


# ----------  category view  ----------
async def showcat_handler(update: Update, context: CallbackContext):
    keyboard = []
    query = update.callback_query
    await safe_answer(query)
    raw = query.data
    try:
        logger.debug("showcat_handler: raw callback_data=%r", raw)
    except Exception:
        pass
    page_from_callback = None
    parent_origin = None
    parent_origin_page = None
    encoded = ""
    search_ref = None
    cat_id = None
    if raw.startswith("showcat_ref::"):
        key = raw.split("::", 1)[1]
        payload = await _resolve_callback_payload(key)
        if not payload:
            _clear_design_pending(context)
            await safe_edit_message(
                query,
                "Reference expired. Please open the list again.",
                action_key=getattr(query, "data", None),
            )
            return
        cat_path = payload.get("path")
        try:
            if payload.get("id"):
                cat_id = str(payload.get("id"))
        except Exception:
            cat_id = None
        search_ref = payload.get("search_ref") or None
        parent_origin = payload.get("from_parent")
        parent_origin_page = None
        try:
            if "parent_page" in payload and payload.get("parent_page") is not None:
                parent_origin_page = int(payload.get("parent_page"))
        except Exception:
            parent_origin_page = None
        parent_index = None
        try:
            if "parent_index" in payload and isinstance(payload.get("parent_index"), (list, tuple)):
                parent_index = payload.get("parent_index")
        except Exception:
            parent_index = None
        page_from_callback = None
        try:
            if "page" in payload and payload.get("page") is not None:
                page_from_callback = int(payload.get("page"))
        except Exception:
            page_from_callback = None
        try:
            encoded = urllib.parse.quote_plus(cat_path) if cat_path else ""
        except Exception:
            encoded = ""

        try:
            if payload.get("type") == "parent_index_back" and parent_index is not None:
                parent_name = payload.get("parent")
                page = int(payload.get("parent_page") or 1)
                page_size = int(payload.get("page_size") or PAGE_SIZE)
                total = len(parent_index)
                start = (page - 1) * page_size
                slice_items = parent_index[start : start + page_size]
                keyboard = []
                for item in slice_items:
                    if isinstance(item, dict):
                        item_path = item.get("path") or item.get("name")
                        item_name = item.get("name") or item.get("path") or str(item_path)
                    else:
                        item_path = str(item)
                        item_name = item_path
                    child_payload = {
                        "type": "showcat",
                        "path": item_path,
                        "from_parent": parent_name,
                        "parent_page": page,
                        "parent_index": parent_index,
                    }
                    if search_ref:
                        child_payload["search_ref"] = search_ref
                    key2 = _store_callback_payload(child_payload)
                    keyboard.append([InlineKeyboardButton(item_name, callback_data=f"showcat_ref::{key2}")])

                nav = []
                total_pages = (total - 1) // page_size + 1 if total else 1
                last_page = max(1, total_pages)
                if page > 1:
                    nav.append(
                        InlineKeyboardButton("⬅️ Previous", callback_data=_shorten_showcat_cb(parent_name, page - 1)),
                    )
                    nav.append(InlineKeyboardButton("🏠 Home", callback_data=_shorten_showcat_cb(parent_name, 1)))
                if page < last_page:
                    nav.append(InlineKeyboardButton("➡️ Next", callback_data=_shorten_showcat_cb(parent_name, page + 1)))
                if total_pages > 1 and page < last_page:
                    nav.append(InlineKeyboardButton("⏭️ End", callback_data=_shorten_showcat_cb(parent_name, last_page)))
                if nav:
                    keyboard.append(nav)

                results_row = _back_to_results_row(context, search_ref)
                if results_row:
                    keyboard.append(results_row)

                # Keep the parent's theme while paging through its subcategories.
                # The parent's design is the branch design for this view, so reuse it
                # rather than re-resolving on every pagination page.
                title = f"{parent_name} — Subcategories (page {page}/{last_page}):"
                # If the subcategories view is showing a parent that has a design,
                # prefer the parent's own design as the branch design for this page.
                branch_design = None
                try:
                    branch_design = context.user_data.get("_branch_design")
                except Exception:
                    pass
                if not branch_design and parent_name:
                    try:
                        branch_design = await _resolve_branch_design(await get_db(), {"name": parent_name})
                    except Exception:
                        pass
                await _send_design_photo(
                    query,
                    context,
                    title,
                    InlineKeyboardMarkup(keyboard),
                    force_design=branch_design,
                )
                return
        except Exception:
            pass
    elif "::from_parent::" in raw:
        left, right = raw.split("::from_parent::", 1)
        left_parts = left.split("::")
        encoded = left_parts[1] if len(left_parts) > 1 else ""
        if len(left_parts) > 2:
            try:
                page_from_callback = int(left_parts[2])
            except Exception:
                page_from_callback = None
        try:
            rp = right.split("::")
            parent_origin = urllib.parse.unquote_plus(rp[0]) if rp and rp[0] else None
            if len(rp) > 1:
                try:
                    parent_origin_page = int(rp[1])
                except Exception:
                    parent_origin_page = None
        except Exception:
            parent_origin = None
            parent_origin_page = None
    else:
        parts = raw.split("::")
        try:
            logger.debug("showcat_handler: parts=%r", parts)
        except Exception:
            pass
        encoded = parts[1] if len(parts) > 1 else ""
        if len(parts) > 2:
            try:
                page_from_callback = int(parts[2])
            except Exception:
                page_from_callback = None

    page = page_from_callback or 1
    try:
        encoded = (encoded or "").strip()
        cat_path = urllib.parse.unquote_plus(encoded)
    except Exception:
        cat_path = (encoded or "").strip()
    try:
        if parent_origin == "categories" and parent_origin_page is not None:
            context.user_data["last_category_page"] = int(parent_origin_page)
        else:
            context.user_data["last_category_page"] = int(page)
    except Exception:
        pass
    db = await get_db()
    category_doc = None
    try:
        if cat_id and is_uuid(cat_id):
            try:
                category_doc = await db.categories.find_one({"id": cat_id})
            except Exception:
                category_doc = None
        if not category_doc:
            category_doc = await db.categories.find_one({"path": cat_path})
        if not category_doc:
            category_doc = await db.categories.find_one({"name": cat_path})
        if not category_doc and encoded and encoded != cat_path:
            category_doc = await db.categories.find_one({"path": encoded}) or await db.categories.find_one(
                {"name": encoded},
            )
        if not category_doc:
            try:
                category_doc = await db.categories.find_one(
                    {"name": {"$regex": f"^{re.escape(cat_path)}$", "$options": "i"}},
                )
            except Exception:
                category_doc = None
        if not category_doc:
            await safe_edit_message(query, f"Category “{cat_path}” not found.", action_key=getattr(query, "data", None))
            return
        try:
            matched_on = None
            if category_doc.get("path") == cat_path:
                matched_on = "path"
            elif category_doc.get("name") == cat_path:
                matched_on = "name"
            else:
                matched_on = "fallback"
            logger.debug(
                "showcat_handler: resolved category_doc name=%r path=%r matched_on=%s encoded=%r",
                category_doc.get("name"),
                category_doc.get("path"),
                matched_on,
                encoded,
            )
        except Exception:
            pass
    except Exception:
        await safe_edit_message(query, f"Category “{cat_path}” not found.", action_key=getattr(query, "data", None))
        return

    cat_name = category_doc.get("name")
    cat_path = category_doc.get("path") or cat_name

    try:
        context.user_data["last_viewed_category"] = cat_path
        if category_doc.get("id"):
            context.user_data["last_viewed_category_id"] = category_doc.get("id")
    except Exception:
        pass
    if not category_doc:
        await safe_edit_message(query, f"Category “{cat_name}” not found.", action_key=getattr(query, "data", None))
        return

    # Resolve the design for THIS category branch once, then reuse across every
    # coaches/courses/subcategory/type/empty view so the image never re-sends.
    # This is the single design resolution for the whole view; every sub-view
    # below reuses `context.user_data["_branch_design"]`.
    try:
        branch_design = await _resolve_branch_design(db, category_doc)
        if branch_design:
            context.user_data["_branch_design"] = branch_design
        else:
            context.user_data.pop("_branch_design", None)
    except Exception:
        pass

    coaches = []
    try:
        if hasattr(db, "coaches"):
            coaches = await db.coaches.find({"topics": cat_name}).to_list(length=PAGE_SIZE)
    except Exception:
        coaches = []

    if not coaches:
        derived = {}
        for crs in category_doc.get("courses", []):
            coach_name = crs.get("coach")
            if coach_name:
                slug = urllib.parse.quote_plus(coach_name)
                derived[slug] = coach_name
        coaches = [{"name": v, "slug": k} for k, v in derived.items()]

    try:
        try:
            logger.debug(
                "showcat_handler: parsed page_from_callback=%r parent_origin=%r parent_origin_page=%r encoded=%r",
                page_from_callback,
                parent_origin,
                parent_origin_page,
                encoded,
            )
        except Exception:
            pass
        total_children = await _get_total_count(db, "categories", {"parent": cat_name}, ttl=60)
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        children = (
            await db.categories.find({"parent": cat_name})
            .sort("name", 1)
            .skip(start)
            .limit(page_size)
            .to_list(length=page_size)
        )
        try:
            logger.debug(
                "showcat_handler: total_children=%s page=%s start=%s fetched_children=%s",
                total_children,
                page,
                start,
                len(children) if children is not None else 0,
            )
        except Exception:
            pass
    except Exception:
        children = []

    if children:
        sorted_children = sorted(children, key=lambda c: (c.get("name") or "").lower())
        page_children = sorted_children

        keyboard = []
        child_names = [c.get("name") for c in page_children if c.get("name")]
        names_with_children = set()
        if child_names:
            try:
                names_with_children = await _get_children_flags(db, child_names, ttl=60)
            except Exception:
                names_with_children = set()

        for child in page_children:
            child_path = child.get("path") or child.get("name")
            payload = {"type": "showcat", "path": child_path, "from_parent": cat_path, "parent_page": page}
            if search_ref:
                payload["search_ref"] = search_ref
            key = _store_callback_payload(payload)
            try:
                has_children = child.get("name") in names_with_children
                courses = child.get("courses", []) if isinstance(child, dict) else []
                is_empty = (not has_children) and (not _has_real_courses(courses))
            except Exception:
                is_empty = True
            display = f"{child.get('name')}{' (empty)' if is_empty else ''}"
            keyboard.append([InlineKeyboardButton(display, callback_data=f"showcat_ref::{key}")])

        nav = []
        total_pages = (total_children - 1) // page_size + 1 if total_children else 1
        last_page = max(1, total_pages)
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=_shorten_showcat_cb(cat_path, page - 1)))

        if total_pages > 1 and page < last_page:
            end_btn = InlineKeyboardButton("⏭️ End", callback_data=_shorten_showcat_cb(cat_path, last_page))
            if page == 1:
                nav.insert(0, end_btn)
            else:
                nav.append(end_btn)

        if (page * page_size) < total_children:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=_shorten_showcat_cb(cat_path, page + 1)))

        if nav:
            keyboard.append(nav)

        try:
            if page > 1:
                breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data=_shorten_showcat_cb(cat_path, 1))]
                keyboard.insert(0, breadcrumb_buttons)
        except Exception:
            pass

        if parent_origin:
            try:
                if parent_index:
                    back_payload = {
                        "type": "parent_index_back",
                        "parent": parent_origin,
                        "parent_page": parent_origin_page or 1,
                        "parent_index": parent_index,
                        "page_size": PAGE_SIZE,
                    }
                    key = _store_callback_payload(back_payload)
                    back_cb = f"showcat_ref::{key}"
                elif parent_origin == "categories":
                    back_cb = f"categories_page::{parent_origin_page or 1}"
                else:
                    pdoc = await db.categories.find_one({"name": parent_origin})
                    ppath = pdoc.get("path") if pdoc and pdoc.get("path") else parent_origin
                    back_cb = _shorten_showcat_cb(ppath, parent_origin_page or 1)
            except Exception:
                back_cb = "back_to_cats"
            keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=back_cb)])
        else:
            parent = category_doc.get("parent")
            if parent:
                pdoc = await db.categories.find_one({"name": parent})
                ppath = pdoc.get("path") if pdoc and pdoc.get("path") else parent
                keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=_shorten_showcat_cb(ppath, page))])
            else:
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
        try:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "\U0001f50d Search",
                        callback_data=_search_category_courses_cb(cat_name, page),
                    ),
                ],
            )
        except Exception:
            pass

        results_row = _back_to_results_row(context, search_ref)
        if results_row:
            keyboard.append(results_row)

        await _send_design_photo(
            query,
            context,
            f"{cat_path} — Subcategories (page {page}/{last_page}):",
            InlineKeyboardMarkup(keyboard),
            force_design=branch_design,
        )
        return

    type_keys = None
    for key in ("types", "category_types", "subtypes", "category_type"):
        if category_doc.get(key):
            type_keys = key
            break

    if type_keys:
        types_list = category_doc.get(type_keys) or []
        keyboard = []
        for t in types_list:
            if isinstance(t, str):
                t_name = t
            elif isinstance(t, dict):
                t_name = t.get("name") or t.get("type")
            else:
                continue
            keyboard.append(
                [
                    InlineKeyboardButton(
                        t_name,
                        callback_data=_showtype_cb(cat_name, t_name, search_ref),
                    ),
                ],
            )
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(cat_path, page))])
        results_row = _back_to_results_row(context, search_ref)
        if results_row:
            keyboard.append(results_row)
        await _send_design_photo(
            query,
            context,
            f"{cat_name} — Select a type:",
            InlineKeyboardMarkup(keyboard),
            force_design=branch_design,
        )
        return

    try:
        branch_design = context.user_data.get("_branch_design")
    except Exception:
        branch_design = None

    if coaches:
        keyboard = []
        for coach in coaches:
            coach_name = coach.get("name")
            coach_slug = coach.get("slug") or urllib.parse.quote_plus(coach_name)
            if parent_origin:
                payload = {
                    "type": "coach_in_cat",
                    "category": cat_name,
                    "coach_slug": coach_slug,
                    "page": page,
                    "from_parent": parent_origin,
                    "parent_page": parent_origin_page,
                }
                if search_ref:
                    payload["search_ref"] = search_ref
                key = _store_callback_payload(payload)
                try:
                    logger.debug(
                        "coach_in_cat: created coach_in_cat_ref key=%s category=%s coach_slug=%s page=%s from_parent=%s parent_page=%s",
                        key,
                        cat_name,
                        coach_slug,
                        page,
                        parent_origin,
                        parent_origin_page,
                    )
                except Exception:
                    pass
                cb = f"coach_in_cat_ref::{key}"
            else:
                cb = f"coach_in_cat::{urllib.parse.quote_plus(cat_name)}::{coach_slug}::{page}"
                try:
                    if len(cb.encode("utf-8")) > 64:
                        payload = {"type": "coach_in_cat", "category": cat_name, "coach_slug": coach_slug, "page": page}
                        if search_ref:
                            payload["search_ref"] = search_ref
                        key = _store_callback_payload(payload)
                        cb = f"coach_in_cat_ref::{key}"
                except Exception:
                    pass
            keyboard.append([InlineKeyboardButton(coach_name, callback_data=cb)])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(cat_path, page))])
        results_row = _back_to_results_row(context, search_ref)
        if results_row:
            keyboard.append(results_row)
        await _send_design_photo(
            query,
            context,
            f"Coaches in '{cat_name}':",
            InlineKeyboardMarkup(keyboard),
            force_design=branch_design,
        )
        return

    courses = category_doc.get("courses", [])
    logger.debug("showcat_handler: category=%s courses_count=%s", cat_name, len(courses))
    if not courses:
        parent = category_doc.get("parent")
        keyboard = []
        if parent_origin:
            try:
                if parent_origin == "categories":
                    back_cb = f"categories_page::{parent_origin_page or 1}"
                else:
                    try:
                        pdoc = await db.categories.find_one({"name": parent_origin})
                        ppath = pdoc.get("path") if pdoc and pdoc.get("path") else parent_origin
                    except Exception:
                        ppath = parent_origin
                    back_cb = _shorten_showcat_cb(ppath, parent_origin_page or 1)
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])
            except Exception:
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
        elif parent:
            pdoc = await db.categories.find_one({"name": parent})
            ppath = pdoc.get("path") if pdoc and pdoc.get("path") else parent
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(ppath, page))])
        else:
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
    try:
        results_row = _back_to_results_row(context, search_ref)
        if results_row:
            keyboard.append(results_row)
    except Exception:
        pass
    try:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "\U0001f50d Search",
                    callback_data=_search_category_courses_cb(cat_name, page),
                ),
            ],
        )
    except Exception:
        await _send_design_photo(
            query,
            context,
            f"Category “{cat_name}” is empty.\nUse /add to populate it.",
            InlineKeyboardMarkup(keyboard),
            force_design=branch_design,
        )
        return

    page = page_from_callback or 1
    parent = category_doc.get("parent")
    origin_ctx = None
    if parent_origin:
        origin_ctx = parent_origin
    elif parent:
        try:
            pdoc = await db.categories.find_one({"name": parent})
            origin_ctx = pdoc.get("path") if pdoc and pdoc.get("path") else parent
        except Exception:
            origin_ctx = parent

    text, reply_markup = build_courses_page(
        courses,
        page=page,
        origin_type="category",
        category=cat_name,
        origin_context=origin_ctx,
        origin_context_page=parent_origin_page,
        store_page_ref=True,
        search_ref=search_ref,
    )
    if not text:
        results_markup = None
        try:
            results_row = _back_to_results_row(context, search_ref)
            if results_row:
                results_markup = InlineKeyboardMarkup([results_row])
        except Exception:
            pass
        await safe_edit_message(
            query,
            f"No courses found in '{cat_name}' on page {page}.",
            reply_markup=results_markup,
            action_key=getattr(query, "data", None),
        )
        return
    try:
        kb = list(reply_markup.inline_keyboard)
        kb.append(
            [
                InlineKeyboardButton(
                    "\U0001f50d Search",
                    callback_data=_search_category_courses_cb(cat_name, page),
                ),
            ],
        )
        results_row = _back_to_results_row(context, search_ref)
        if results_row:
            kb.append(results_row)
        reply_markup = InlineKeyboardMarkup(kb)
    except Exception:
        pass
    await _send_design_photo(
        query,
        context,
        text,
        reply_markup,
        force_design=branch_design,
    )


# ----------  courses listing  ----------
async def handle_back_to_cats(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    try:
        await categories_page(update, context, page=1)
    except Exception:
        logger.exception("Error returning to categories")
        await safe_edit_message(
            query,
            "An unexpected error occurred. Please try again later.",
            action_key=getattr(query, "data", None),
        )


async def list_courses(update: Update, context: CallbackContext):
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        page = 1
        page_size = PAGE_SIZE
        has_more = False
        async with _db_timing(f"list_courses:page:{page}"):
            cache_key = f"page:global:{page}"
            cached = _get_cached_page(cache_key)
            if cached is not None:
                items = cached
            else:
                # Keyset first page: no O(offset) skip, and Next carries a
                # cursor so deep pages stay bounded index scans.
                items, has_more = await _fetch_global_courses_keyset(db, page_size, after=None)
                try:
                    _set_cached_page(cache_key, items, ttl=3)
                except Exception:
                    pass
        all_courses = items[:page_size]
        all_courses = sorted(all_courses, key=lambda c: (c.get("name") or "").lower())
        # Bot-wide course total (cached), used both for pagination and for the
        # 'shown/total' counter rendered on each page. If the count query fails
        # (transient DB error) fall back to a lower-bound estimate from this
        # page so pagination still works instead of collapsing to one page.
        total_courses = await _get_bot_courses_total(db)
        if not total_courses:
            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses))
        first_cursor = _course_cursor(all_courses[0]) if all_courses else None
        last_cursor = _course_cursor(all_courses[-1]) if all_courses else None
        if all_courses:
            text, reply_markup = build_courses_page(
                all_courses,
                page=page,
                origin_type="global",
                origin_context=None,
                total_count=total_courses,
                is_page=True,
                overall_total=total_courses,
                page_first_cursor=first_cursor,
                page_last_cursor=last_cursor,
            )
            if not text:
                await update.message.reply_text("No courses available.")
                return
            try:
                kb = list(reply_markup.inline_keyboard)
                kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_courses::global::{page}")])
                reply_markup = InlineKeyboardMarkup(kb)
            except Exception:
                pass
            msg = await update.message.reply_text(text, reply_markup=reply_markup)
            try:
                schedule_close_inline_message(msg, user_id=_event_user_id(update))
            except Exception:
                pass
        else:
            await update.message.reply_text("No courses available.")
    except Exception:
        logger.exception("Error listing courses")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")


logger.info("[STATE] returning CREATE_CAT_NAME=%s id=%s", CREATE_CAT_NAME, id(CREATE_CAT_NAME))


# ----------  category creation  ----------
async def create_category(update: Update, context: CallbackContext):
    try:
        owner_env = os.getenv("BOT_OWNER_ID")
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = None
    try:
        user_id = getattr(update.effective_user, "id", None) or (
            update.message.from_user.id
            if getattr(update, "message", None) and getattr(update.message, "from_user", None)
            else None
        )
    except Exception:
        user_id = None
    if owner_id is not None and user_id != owner_id:
        try:
            await update.message.reply_text("Unauthorized")
        except Exception:
            pass
        return ConversationHandler.END
    try:
        logger.info(
            "create_category invoked: user=%s chat=%s",
            getattr(update, "effective_user", None).id if getattr(update, "effective_user", None) else None,
            getattr(update, "effective_chat", None).id if getattr(update, "effective_chat", None) else None,
        )
    except Exception:
        logger.info("create_category invoked (unable to read user/chat)")
    db = await get_db()
    cats = []
    try:
        page_size = PAGE_SIZE
        cats = (
            await db.categories.find({"parent": {"$exists": False}})
            .sort("name", 1)
            .limit(page_size)
            .to_list(length=page_size)
        )
    except Exception:
        cats = []

    try:
        await createcat_page(update.message, context, page=1)
    except Exception:
        logger.exception("create_category: createcat_page failed, falling back to simple keyboard")
        keyboard = []
        keyboard.append([InlineKeyboardButton("(Top-level)", callback_data="createcat_parent::")])
        for cat in cats:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        cat.get("name"),
                        callback_data=_createcat_parent_cb(cat.get("name")),
                    ),
                ],
            )
        await update.message.reply_text(
            "Select a parent category (or choose Top-level):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return CREATE_CAT_PARENT


async def handle_create_category_parent(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    try:
        owner_env = os.getenv("BOT_OWNER_ID")
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, "id", None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, "data", None))
        return ConversationHandler.END
    raw = query.data
    parent = None
    if raw.startswith("createcat_parent_ref::"):
        try:
            payload = await _resolve_callback_payload(raw.split("::", 1)[1])
            if payload:
                parent = payload.get("category") or payload.get("category_name")
        except Exception:
            parent = None
    else:
        encoded = raw.split("::", 1)[1] if "::" in raw else ""
        parent = urllib.parse.unquote_plus(encoded) if encoded else None
    context.user_data["new_cat_parent"] = parent
    try:
        logger.info(
            "handle_create_category_parent: user=%s parent=%s",
            update.effective_user.id if update.effective_user else None,
            parent,
        )
    except Exception:
        logger.debug("handle_create_category_parent: log failed")
    if parent:
        prompt = f"Enter the new category name (parent: {parent}):"
    else:
        prompt = "Enter the new top-level category name:"
    await query.message.reply_text(prompt)
    return CREATE_CAT_NAME


async def handle_create_category_parent_text(update: Update, context: CallbackContext):
    try:
        owner_env = os.getenv("BOT_OWNER_ID")
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = None
    try:
        user_id = (
            update.message.from_user.id
            if getattr(update, "message", None) and getattr(update.message, "from_user", None)
            else getattr(update.effective_user, "id", None)
        )
    except Exception:
        user_id = None
    if owner_id is not None and user_id != owner_id:
        try:
            await update.message.reply_text("Unauthorized")
        except Exception:
            pass
        return ConversationHandler.END

    parent = update.message.text.strip() or None
    if parent:
        context.user_data["new_cat_parent"] = parent
        prompt = f"Enter the new category name (parent: {parent}):"
    else:
        context.user_data["new_cat_parent"] = None
        prompt = "Enter the new top-level category name:"
    await update.message.reply_text(prompt)
    return CREATE_CAT_NAME


async def create_parent(update: Update, context: CallbackContext):
    try:
        owner_env = os.getenv("BOT_OWNER_ID")
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = None
    try:
        user_id = getattr(update.effective_user, "id", None) or (
            update.message.from_user.id
            if getattr(update, "message", None) and getattr(update.message, "from_user", None)
            else None
        )
    except Exception:
        user_id = None
    if owner_id is not None and user_id != owner_id:
        try:
            await update.message.reply_text("Unauthorized")
        except Exception:
            pass
        return ConversationHandler.END
    context.user_data["new_cat_parent"] = None
    await update.message.reply_text("Enter the new parent category name:")
    return CREATE_CAT_NAME


async def handle_category_name(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    category_name = update.message.text.strip()
    logger.info("[CAT-INSERT-START] name=%r uid=%s", category_name, user_id)
    try:
        logger.debug(
            "handle_category_name: incoming message=%s",
            update.message.text if getattr(update, "message", None) else None,
        )
    except Exception:
        pass

    # --- single validator (allow special chars; only restrict control/newline chars) ---
    if not category_name or len(category_name) < 3 or len(category_name) > MAX_CATEGORY_NAME_LENGTH:
        await update.message.reply_text(f"Name must be 3-{MAX_CATEGORY_NAME_LENGTH} chars.")
        return CREATE_CAT_NAME
    if any(c in category_name for c in "\r\n"):
        await update.message.reply_text("Category name cannot contain newlines or control characters.")
        return CREATE_CAT_NAME

    try:
        db = await get_db()
        logger.info("[CAT-DB] using database: %s", db.name)
        coll = db["categories"]
        logger.info("[CAT-INSERT] about to insert %r", category_name)

        parent = context.user_data.pop("new_cat_parent", None)
        doc = {"name": category_name, "created_by": user_id, "id": str(uuid.uuid4())}
        if parent:
            parent_doc = await db.categories.find_one({"name": parent})
            parent_path = parent_doc.get("path") if parent_doc and parent_doc.get("path") else parent
            doc["parent"] = parent
            doc["path"] = f"{parent_path}/{category_name}"

        try:
            if parent:
                existing_child = await coll.find_one({"name": category_name, "parent": parent})
                if existing_child:
                    await update.message.reply_text(
                        f"A child category named '{category_name}' already exists under '{parent}' (id: {existing_child.get('id') or existing_child.get('_id')}). Please choose a different name.",
                    )
                    return CREATE_CAT_NAME
                try:
                    coach_conflict = await _get_total_count(
                        db,
                        "categories",
                        {"name": parent, "courses.coach": category_name},
                        ttl=15,
                    )
                    if coach_conflict > 0:
                        await update.message.reply_text(
                            f"Warning: There are existing courses under '{parent}' with a coach name '{category_name}'. Creating a child category with the same name may cause ambiguity. Please pick a different name.",
                        )
                        return CREATE_CAT_NAME
                except Exception:
                    pass
            else:
                existing_parent = await coll.find_one({"$or": [{"name": category_name}, {"path": category_name}]})
                if existing_parent:
                    await update.message.reply_text(
                        f"A parent category named '{category_name}' already exists (id: {existing_parent.get('id') or existing_parent.get('_id')}). Please choose a different name.",
                    )
                    return CREATE_CAT_NAME
        except Exception:
            pass

        result = await coll.insert_one(doc)
        logger.info("[CAT-INSERT-DONE] _id=%s", result.inserted_id)
        if not parent:
            logger.info(
                "[CAT-INSERT-PARENT] Created top-level parent category %r _id=%s",
                category_name,
                result.inserted_id,
            )
        else:
            logger.info(
                "[CAT-INSERT-CHILD] Created category %r under parent %r _id=%s",
                category_name,
                parent,
                result.inserted_id,
            )
        await update.message.reply_text(f"Category ‘{category_name}’ saved ✔")

        try:
            last_page = context.user_data.pop("createcat_last_page", 1)
            if not parent:
                await categories_page(update.message, context, page=last_page)
            else:
                await children_page(update.message, context, parent, page=last_page)
        except Exception:
            pass

        return ConversationHandler.END
    except DuplicateKeyError:
        logger.warning("[CAT-INSERT-DUP] category already exists: %r", category_name)
        await update.message.reply_text(
            f"A category named '{category_name}' already exists. Please choose a different name.",
        )
        return CREATE_CAT_NAME
    except Exception:
        logger.exception("[CAT-INSERT-FAIL]")
        await update.message.reply_text("Save failed – check console.")
        return ConversationHandler.END


# ----------  category selection  ----------
async def handle_category_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    if data.startswith("category::"):
        encoded = data.split("::", 1)[1]
        cat_path = urllib.parse.unquote_plus(encoded)
    else:
        encoded = data.replace("category_", "", 1)
        cat_path = urllib.parse.unquote_plus(encoded)
    cat_name = cat_path
    db = await get_db()
    page = 1
    page_size = PAGE_SIZE
    try:
        items = await get_courses_by_category(None, cat_path, page=page, page_size=page_size)
    except Exception:
        items = []

    if not items:
        await _send_design_photo(
            query,
            context,
            f"Category “{cat_name}” is empty.\nUse /add to populate it.",
            None,
        )
        return

    keyboard = []
    for crs in items:
        try:
            keyboard.append([InlineKeyboardButton(crs.get("name"), url=crs.get("link"))])
        except Exception:
            continue

    try:
        pdoc = await db.categories.find_one({"name": cat_path}, projection={"parent": 1})
        parent = pdoc.get("parent") if pdoc else None
    except Exception:
        parent = None
    if parent:
        try:
            pdoc2 = await db.categories.find_one({"name": parent}, projection={"path": 1})
            ppath = pdoc2.get("path") if pdoc2 and pdoc2.get("path") else parent
        except Exception:
            ppath = parent
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(ppath, 1))])
    else:
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])

    try:
        total_items = await _get_courses_count(db, cat_path)
        total_pages = math.ceil(total_items / page_size) if total_items > 0 else 1
        if total_pages > 1:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "➡️ Next",
                        callback_data=_category_page_next_cb(cat_path, 2, total_items),
                    ),
                ],
            )
    except Exception:
        pass

    await safe_edit_message(
        query,
        "📚 Tap any course to open its link:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        action_key=getattr(query, "data", None),
    )


# ----------  course selection  ----------
async def handle_course_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)

    data = query.data
    origin_type = None
    origin_page = 1
    cat_name = None
    course_name = None

    if data.startswith("course_ref::"):
        rest = data[len("course_ref::") :]
        appended_back = None
        if "::back::" in rest:
            key, enc_back = rest.split("::back::", 1)
            try:
                appended_back = urllib.parse.unquote_plus(enc_back)
            except Exception:
                appended_back = enc_back
        else:
            key = rest

        payload = await _resolve_callback_payload(key)
        logger.debug("handle_course_selection: resolved payload for key=%s -> %s", key, payload)
        if not payload:
            _clear_design_pending(context)
            await safe_edit_message(
                query,
                "Reference expired. Please open the list again.",
                action_key=getattr(query, "data", None),
            )
            return

        try:
            logger.debug(
                "handle_course_selection: raw_query_data=%s key=%s appended_back=%s payload_back=%s payload_keys=%s origin_type=%s origin_page=%s",
                data,
                key,
                appended_back,
                payload.get("back_cb"),
                list(payload.keys()) if isinstance(payload, dict) else None,
                payload.get("origin_type"),
                payload.get("origin_page"),
            )
        except Exception:
            logger.debug("handle_course_selection: debug log failed for payload tracing")
        cat_name = payload.get("category")
        course_name = payload.get("name")
        course_id = payload.get("id")
        origin_type = payload.get("origin_type")
        try:
            origin_page = int(payload.get("origin_page", 1))
        except Exception:
            origin_page = 1
        origin_context = payload.get("origin_context")
        try:
            origin_context_page = (
                int(payload.get("origin_context_page")) if payload.get("origin_context_page") is not None else None
            )
        except Exception:
            origin_context_page = None
        search_ref = payload.get("search_ref") or None
        saved_back_cb = appended_back or payload.get("back_cb")
    else:
        await safe_edit_message(
            query,
            "This action used a legacy callback format which has been removed. Please reopen the list and try again.",
            action_key=getattr(query, "data", None),
        )
        return

    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return

    try:
        course = None
        if cat_name:
            category_doc = await db.categories.find_one({"$or": [{"name": cat_name}, {"path": cat_name}]})
            if category_doc:
                for crs in category_doc.get("courses", []):
                    if course_id and crs.get("id") == course_id:
                        course = {
                            "id": crs.get("id"),
                            "name": crs.get("name"),
                            "link": crs.get("link"),
                            "category": category_doc.get("name"),
                        }
                        break
                    if (not course_id) and crs.get("name") == course_name:
                        course = {
                            "id": crs.get("id"),
                            "name": crs.get("name"),
                            "link": crs.get("link"),
                            "category": category_doc.get("name"),
                        }
                        break
            else:
                try:
                    if course_id:
                        category_doc = await db.categories.find_one(
                            {"courses.id": course_id},
                            projection={"name": 1, "courses": 1},
                        )
                        if category_doc:
                            for crs in category_doc.get("courses", []):
                                if crs.get("id") == course_id:
                                    course = {
                                        "id": crs.get("id"),
                                        "name": crs.get("name"),
                                        "link": crs.get("link"),
                                        "category": category_doc.get("name"),
                                    }
                                    break
                    else:
                        category_doc = await db.categories.find_one(
                            {"courses.name": course_name},
                            projection={"name": 1, "courses": 1},
                        )
                        if category_doc:
                            for crs in category_doc.get("courses", []):
                                if crs.get("name") == course_name:
                                    course = {
                                        "id": crs.get("id"),
                                        "name": crs.get("name"),
                                        "link": crs.get("link"),
                                        "category": category_doc.get("name"),
                                    }
                                    break
                except Exception:
                    category_doc = None
        elif course_id:
            category_doc = await db.categories.find_one(
                {"courses.id": course_id},
                projection={"name": 1, "courses": 1},
            )
            if category_doc:
                for crs in category_doc.get("courses", []):
                    if crs.get("id") == course_id:
                        course = {
                            "id": crs.get("id"),
                            "name": crs.get("name"),
                            "link": crs.get("link"),
                            "category": category_doc.get("name"),
                        }
                        break
        else:
            await safe_edit_message(
                query,
                "Course name is ambiguous. Open the course from its category list to view details.",
                action_key=getattr(query, "data", None),
            )
            return

        if course:
            course_category = course.get("category") if isinstance(course, dict) else None
            logger.debug(
                "handle_course_selection: origin_type=%s origin_page=%s course_category=%s cat_name=%s courses_in_doc=%s",
                origin_type,
                origin_page,
                course_category,
                cat_name,
                bool(course_category),
            )
            if not course_category:
                course_category = cat_name

            if not origin_type and course_category:
                origin_type = "category"
                origin_page = origin_page or 1
                logger.debug(
                    "handle_course_selection: inferred origin_type='category' from course_category=%s",
                    course_category,
                )

            if origin_type == "category":
                back_target = course_category or cat_name or None
                if saved_back_cb:
                    try:
                        sb = str(saved_back_cb)
                        if sb.startswith("courses::category::"):
                            back_cb = sb
                        elif (
                            sb.startswith("courses::coach::")
                            or sb.startswith("showcat")
                            or sb.startswith("showcat_ref")
                            or sb.startswith("categories_page")
                            or sb == "back_to_cats"
                        ) and back_target:
                            try:
                                pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                                ppath = pdoc.get("path") if pdoc and pdoc.get("path") else back_target
                            except Exception:
                                ppath = back_target
                            page_to_use = None
                            try:
                                parts = sb.split("::")
                                if len(parts) >= 4:
                                    page_to_use = int(parts[-1])
                            except Exception:
                                page_to_use = None
                            page_to_use = page_to_use or origin_page or 1
                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{page_to_use}"
                        elif back_target:
                            try:
                                pdoc = await db.categories.find_one(
                                    {"name": back_target},
                                    projection={"path": 1},
                                )
                                ppath = pdoc.get("path") if pdoc and pdoc.get("path") else back_target
                            except Exception:
                                ppath = back_target
                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{origin_page or 1}"
                        else:
                            back_cb = sb
                    except Exception:
                        back_cb = None

                if not back_cb:
                    back_target = back_target or "categories"
                    try:
                        pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                        ppath = pdoc.get("path") if pdoc and pdoc.get("path") else back_target
                    except Exception:
                        ppath = back_target
                    try:
                        total_items = await _get_courses_count(db, back_target)
                    except Exception:
                        total_items = 0
                    total_pages = math.ceil(total_items / PAGE_SIZE) if total_items > 0 else 1
                    try:
                        clamped_page = max(1, min(int(origin_page or 1), max(1, int(total_pages))))
                    except Exception:
                        clamped_page = origin_page or 1
                    if str(ppath).lower() == "categories" and back_target == "categories":
                        back_cb = f"categories_page::{clamped_page}"
                    else:
                        back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{clamped_page}"
                logger.debug("handle_course_selection: computed category back_cb=%s", back_cb)
                try:
                    back_cb = await _reconcile_back_cb(
                        db,
                        back_cb,
                        course_category=course_category,
                        origin_page=origin_page,
                    )
                    logger.debug("handle_course_selection: reconciled category back_cb=%s", back_cb)
                except Exception:
                    pass
            elif saved_back_cb:
                back_cb = str(saved_back_cb)
            elif origin_type == "coach" and origin_page:
                coach_slug = origin_context or course_category or cat_name or ""
                coach_slug_enc = urllib.parse.quote_plus(str(coach_slug))
                back_cb = f"courses::coach::{coach_slug_enc}::{origin_page}"
                logger.debug("handle_course_selection: computed back_cb=%s", back_cb)
            elif origin_type == "global" and origin_page:
                back_cb = f"courses::global::{origin_page}"
            else:
                back_cb = "courses::global::1"

            try:
                if course_category:
                    try:
                        pdoc = await db.categories.find_one({"name": course_category}, projection={"path": 1})
                        ppath = pdoc.get("path") if pdoc and pdoc.get("path") else course_category
                    except Exception:
                        ppath = course_category
                    try:
                        page_to_use = int(origin_page or 1)
                    except Exception:
                        page_to_use = 1
                    back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{page_to_use}"
            except Exception:
                pass

            try:
                if (
                    back_cb
                    and isinstance(back_cb, str)
                    and back_cb.startswith("courses::category::")
                    and course
                    and course.get("name")
                ):
                    parts = back_cb.split("::")
                    if len(parts) >= 4:
                        target_cat = urllib.parse.unquote_plus(parts[2])
                        target_page = int(parts[3]) if parts[3].isdigit() else 1
                        try:
                            pipeline = [
                                {"$match": {"$or": [{"name": target_cat}, {"path": target_cat}]}},
                                {"$unwind": "$courses"},
                                {"$project": {"name": "$courses.name", "id": "$courses.id"}},
                                {"$sort": {"name": 1}},
                            ]
                            course_list = await db.categories.aggregate(pipeline).to_list(length=500)
                            found_index = None
                            for idx, item in enumerate(course_list):
                                try:
                                    if course.get("id") and item.get("id") == course.get("id"):
                                        found_index = idx
                                        break
                                    if item.get("name") == course.get("name"):
                                        found_index = idx
                                        break
                                except Exception:
                                    continue
                            if found_index is not None:
                                computed_page = (found_index // PAGE_SIZE) + 1
                                if computed_page != target_page:
                                    pdoc = await db.categories.find_one(
                                        {"$or": [{"name": target_cat}, {"path": target_cat}]},
                                        projection={"path": 1},
                                    )
                                    ppath = pdoc.get("path") if pdoc and pdoc.get("path") else target_cat
                                    back_cb = (
                                        f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{computed_page}"
                                    )
                                    logger.debug(
                                        "handle_course_selection: adjusted back_cb to page containing course: %s",
                                        back_cb,
                                    )
                            else:
                                try:
                                    if course.get("id"):
                                        found = await db.categories.find_one(
                                            {"courses.id": course.get("id")},
                                            projection={"name": 1},
                                        )
                                    else:
                                        found = await db.categories.find_one(
                                            {"courses.name": course.get("name")},
                                            projection={"name": 1},
                                        )
                                    if found:
                                        true_cat = found.get("name")
                                        pipeline2 = [
                                            {"$match": {"name": true_cat}},
                                            {"$unwind": "$courses"},
                                            {"$project": {"name": "$courses.name", "id": "$courses.id"}},
                                            {"$sort": {"name": 1}},
                                        ]
                                        clist = await db.categories.aggregate(pipeline2).to_list(length=500)
                                        fidx = None
                                        for idx, item in enumerate(clist):
                                            try:
                                                if course.get("id") and item.get("id") == course.get("id"):
                                                    fidx = idx
                                                    break
                                                if item.get("name") == course.get("name"):
                                                    fidx = idx
                                                    break
                                            except Exception:
                                                continue
                                        if fidx is not None:
                                            computed_page = (fidx // PAGE_SIZE) + 1
                                            pdoc = await db.categories.find_one(
                                                {"name": true_cat},
                                                projection={"path": 1},
                                            )
                                            ppath = pdoc.get("path") if pdoc and pdoc.get("path") else true_cat
                                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{computed_page}"
                                            logger.debug(
                                                "handle_course_selection: located course in different category, updated back_cb=%s",
                                                back_cb,
                                            )
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception:
                pass

            delete_payload = {
                "category": course_category,
                "id": course.get("id"),
                "name": course.get("name"),
                "origin_type": origin_type,
                "origin_page": origin_page,
                "origin_context": origin_context,
                "origin_context_page": origin_context_page,
            }
            try:
                delete_key = _store_callback_payload(delete_payload)
            except Exception:
                delete_key = None

            delete_cb = f"delete_ref::{delete_key}" if delete_key else "delete_ref::"

            if origin_type == "category":
                try:
                    if saved_back_cb:
                        sb = str(saved_back_cb)
                        try:
                            if sb.startswith("courses::category::"):
                                back_cb = sb
                            else:
                                try:
                                    pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                                    ppath = pdoc.get("path") if pdoc and pdoc.get("path") else back_target
                                except Exception:
                                    ppath = back_target
                                page_to_use = origin_page or 1
                                back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{page_to_use}"
                        except Exception:
                            back_cb = None
                except Exception:
                    back_cb = None

                if not back_cb:
                    back_target = course.get("category") or cat_name or "1"
                    try:
                        try:
                            pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                            ppath = pdoc.get("path") if pdoc and pdoc.get("path") else back_target
                            pipeline = [
                                {"$match": {"name": back_target}},
                                {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
                            ]
                            try:
                                total_items = await _get_courses_count(db, back_target)
                            except Exception:
                                total_items = 0
                            total_pages = math.ceil(total_items / PAGE_SIZE) if total_items > 0 else 1
                        except Exception:
                            total_pages = origin_page or 1
                            ppath = back_target
                        try:
                            clamped_page = max(1, min(int(origin_page or 1), max(1, int(total_pages))))
                        except Exception:
                            clamped_page = origin_page or 1
                        try:
                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{clamped_page}"
                        except Exception:
                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(back_target))}::{clamped_page}"
                    except Exception:
                        back_cb = f"courses::global::{origin_page}"

                try:
                    back_cb = await _reconcile_back_cb(
                        db,
                        back_cb,
                        course_category=course.get("category") or cat_name,
                        origin_page=origin_page,
                    )
                except Exception:
                    pass

                try:
                    if course.get("category"):
                        try:
                            pdoc = await db.categories.find_one(
                                {"name": course.get("category")},
                                projection={"path": 1},
                            )
                            ppath = pdoc.get("path") if pdoc and pdoc.get("path") else course.get("category")
                        except Exception:
                            ppath = course.get("category")
                        candidate_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{origin_page or 1}"
                        try:
                            back_cb = await _reconcile_back_cb(
                                db,
                                candidate_cb,
                                course_category=course.get("category"),
                                origin_page=origin_page,
                            )
                        except Exception:
                            back_cb = candidate_cb
                except Exception:
                    pass

                try:
                    user_id = getattr(query.from_user, "id", None)
                    owner_env = os.getenv("BOT_OWNER_ID")
                    try:
                        owner_id = int(owner_env) if owner_env else None
                    except Exception:
                        owner_id = None
                    if owner_id is not None and user_id != owner_id:
                        nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb)]
                    else:
                        nav_row = [
                            InlineKeyboardButton("🔙 Back", callback_data=back_cb),
                            InlineKeyboardButton("Delete Course", callback_data=delete_cb),
                        ]
                except Exception:
                    nav_row = [
                        InlineKeyboardButton("🔙 Back", callback_data=back_cb),
                        InlineKeyboardButton("Delete Course", callback_data=delete_cb),
                    ]
                extra_row = []
                try:
                    if course_category:
                        parent_doc = await db.categories.find_one({"name": course_category})
                        if parent_doc:
                            parent = parent_doc.get("parent")
                            if parent:
                                pdoc = await db.categories.find_one({"name": parent})
                                ppath = pdoc.get("path") if pdoc and pdoc.get("path") else parent
                                extra_row.append(
                                    InlineKeyboardButton(
                                        "🏠 Coaches",
                                        callback_data=_shorten_showcat_cb(ppath, origin_page),
                                    ),
                                )
                                logger.debug(
                                    "handle_course_selection: coaches button -> parent=%s ppath=%s",
                                    parent,
                                    ppath,
                                )
                    extra_row.append(InlineKeyboardButton("📚 All Categories", callback_data="back_to_cats"))
                except Exception:
                    extra_row = [InlineKeyboardButton("All Categories", callback_data="back_to_cats")]
                keyboard = [nav_row, extra_row]
            else:
                try:
                    user_id = getattr(query.from_user, "id", None)
                    owner_env = os.getenv("BOT_OWNER_ID")
                    try:
                        owner_id = int(owner_env) if owner_env else None
                    except Exception:
                        owner_id = None
                    if owner_id is not None and user_id != owner_id:
                        nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb)]
                    else:
                        nav_row = [
                            InlineKeyboardButton("🔙 Back", callback_data=back_cb),
                            InlineKeyboardButton("Delete Course", callback_data=delete_cb),
                        ]
                except Exception:
                    nav_row = [
                        InlineKeyboardButton("🔙 Back", callback_data=back_cb),
                        InlineKeyboardButton("Delete Course", callback_data=delete_cb),
                    ]
                extra_row = None
                try:
                    if origin_type != "global":
                        if course_category:
                            parent_doc = await db.categories.find_one({"name": course_category})
                            if parent_doc:
                                parent = parent_doc.get("parent")
                                if parent:
                                    pdoc = await db.categories.find_one({"name": parent})
                                    ppath = pdoc.get("path") if pdoc and pdoc.get("path") else parent
                                    extra_row = [
                                        InlineKeyboardButton(
                                            "🏠 Coaches",
                                            callback_data=_shorten_showcat_cb(ppath, origin_page),
                                        ),
                                    ]
                                else:
                                    extra_row = [InlineKeyboardButton("🏠 Categories", callback_data="back_to_cats")]
                            else:
                                extra_row = [InlineKeyboardButton("🏠 Categories", callback_data="back_to_cats")]
                        else:
                            extra_row = [InlineKeyboardButton("🏠 Categories", callback_data="back_to_cats")]
                except Exception:
                    extra_row = None
                keyboard = [nav_row]
                if extra_row:
                    keyboard.append(extra_row)
            try:
                results_row = _back_to_results_row(context, search_ref)
                if results_row:
                    keyboard.append(results_row)
            except Exception:
                pass
            reply_markup = InlineKeyboardMarkup(keyboard)
            details = (
                f"📚 **Course Details**\n\n"
                f"Name: {course.get('name')}\n"
                f"Link: {course.get('link')}\n"
                f"Category: {course_category}"
            )
            try:
                logger.debug(
                    "handle_course_selection: FINAL back_cb=%s origin_type=%s origin_page=%s saved_back_cb=%s origin_context=%s course_category=%s course_id=%s",
                    back_cb,
                    origin_type,
                    origin_page,
                    saved_back_cb if "saved_back_cb" in locals() else None,
                    origin_context if "origin_context" in locals() else None,
                    course_category,
                    course.get("id"),
                )
            except Exception:
                pass
            await safe_edit_message(query, details, reply_markup=reply_markup, action_key=getattr(query, "data", None))
        else:
            await safe_edit_message(
                query,
                "Course not found. Please try again.",
                action_key=getattr(query, "data", None),
            )
    except Exception:
        logger.exception("Error fetching course %r", course_name)
        await safe_edit_message(
            query,
            "An error occurred while fetching the course. Please try again later.",
            action_key=getattr(query, "data", None),
        )


# ----------  course list fetch  ----------
async def get_courses_by_category(user_id, category, page: int = 1, page_size: int = 20):
    db = await get_db()
    if db is None:
        return []

    try:
        start = (page - 1) * page_size
        cache_key = f"page:category:{urllib.parse.quote_plus(str(category))}:{page}:{page_size}"
        cached = _get_cached_page(cache_key)
        logger.debug(
            "get_courses_by_category: category=%s page=%s page_size=%s cache_key=%s cached_mem=%s",
            category,
            page,
            page_size,
            cache_key,
            bool(cached),
        )
        if cached is not None:
            logger.debug(
                "get_courses_by_category: returning cached page len=%d (mem)",
                len(cached) if hasattr(cached, "__len__") else 0,
            )
            return cached
        try:
            if _redis is not None:
                val = await _redis.get(cache_key)
                if val is not None:
                    try:
                        items = json.loads(val)
                        _set_cached_page(cache_key, items, ttl=PAGE_CACHE_TTL)
                        logger.debug(
                            "get_courses_by_category: returning cached page len=%d (redis)",
                            len(items) if hasattr(items, "__len__") else 0,
                        )
                        return items
                    except Exception:
                        pass
        except Exception:
            pass

        async with _db_timing(f"get_courses_by_category:{category}:{page}"):
            try:
                await ensure_course_uuids(db, category)
            except Exception:
                pass
            try:
                proj = {"courses": {"$slice": [start, page_size]}, "name": 1, "path": 1}
                doc = await db.categories.find_one({"$or": [{"name": category}, {"path": category}]}, projection=proj)
                if doc and isinstance(doc.get("courses"), list):
                    items = [
                        {
                            "id": str(c.get("id")) if isinstance(c, dict) and c.get("id") is not None else None,
                            "name": (c.get("name") if isinstance(c, dict) else None),
                            "link": (c.get("link") if isinstance(c, dict) else None),
                            "category": (doc.get("name") or doc.get("path")),
                            "coach": (c.get("coach") if isinstance(c, dict) else None),
                        }
                        for c in doc.get("courses")
                    ]
                else:
                    items = []
                logger.debug(
                    "get_courses_by_category: fetched slice start=%s len=%s from category=%s",
                    start,
                    len(items),
                    category,
                )
            except Exception:
                items = []

        if not items:
            try:
                items_pipeline = [
                    {"$match": {"$or": [{"name": category}, {"path": category}]}},
                    {"$unwind": "$courses"},
                    {
                        "$project": {
                            "name": "$courses.name",
                            "link": "$courses.link",
                            "category": "$name",
                            "id": {"$ifNull": ["$courses.id", None]},
                            "coach": "$courses.coach",
                        },
                    },
                    {"$sort": {"name": 1}},
                    {"$skip": start},
                    {"$limit": page_size},
                ]
                try:
                    items = await db.categories.aggregate(items_pipeline).to_list(length=page_size)
                except Exception:
                    items = []
                logger.debug(
                    "get_courses_by_category: fallback unwind returned len=%s for category=%s",
                    len(items),
                    category,
                )
            except Exception:
                items = []

        try:
            _set_cached_page(cache_key, items, ttl=PAGE_CACHE_TTL)
        except Exception:
            pass
        logger.debug(
            "get_courses_by_category: final items_len=%s category=%s page=%s",
            len(items) if hasattr(items, "__len__") else 0,
            category,
            page,
        )
        return items
    except Exception:
        logger.exception("Error while fetching courses for category %r", category)
        return []


CATEGORY_COUNT_FALLBACK_TTL = 15


# ----------  pagination helpers  ----------
async def _category_courses_count(db, category: str, ttl: int = 0):
    key = None
    if ttl > 0:
        key = f"count:category_courses_np:{category}"
        now = time.time()
        entry = _COUNT_CACHE.get(key)
        if entry and entry[1] > now:
            return entry[0]
        try:
            if _redis is not None:
                val = await _redis.get(key)
                if val is not None:
                    try:
                        cnt = int(val)
                    except Exception:
                        cnt = None
                    if cnt is not None:
                        _COUNT_CACHE[key] = (cnt, now + ttl)
                        _prune_count_cache()
                        return cnt
        except Exception:
            pass
    try:
        cnt_doc = await db.categories.aggregate(
            [
                {"$match": {"$or": [{"name": category}, {"path": category}]}},
                {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
                {"$group": {"_id": None, "count": {"$sum": "$n"}}},
            ],
        ).to_list(length=1)
        cnt = int(cnt_doc[0].get("count")) if cnt_doc else 0
    except Exception:
        cnt = None
    if cnt is not None and key:
        _COUNT_CACHE[key] = (cnt, time.time() + ttl)
        _prune_count_cache()
        try:
            if _redis is not None:
                _bg_task(_redis.set(key, str(cnt), ex=ttl))
        except Exception:
            pass
    return cnt


async def _courses_origin_total(db, origin_type: str, category: str = None, fresh: bool = False):
    try:
        if origin_type == "global":
            cnt_doc = await db.categories.aggregate(
                [
                    {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
                    {"$group": {"_id": None, "count": {"$sum": "$n"}}},
                ],
            ).to_list(length=1)
            return int(cnt_doc[0].get("count")) if cnt_doc else 0
        if origin_type == "category" and category:
            if fresh:
                return await _category_courses_count(db, category)
            try:
                cached = await _get_courses_count(db, category, ttl=60)
            except Exception:
                cached = None
            if cached:
                return cached
            return await _category_courses_count(db, category, ttl=CATEGORY_COUNT_FALLBACK_TTL)
        if origin_type == "coach" and category:
            cnt_doc = await db.categories.aggregate(
                [
                    {"$match": {"courses.coach": category}},
                    {"$unwind": "$courses"},
                    {"$match": {"courses.coach": category}},
                    {"$group": {"_id": None, "count": {"$sum": 1}}},
                ],
            ).to_list(length=1)
            return int(cnt_doc[0].get("count")) if cnt_doc else 0
    except Exception:
        return None
    return None


def _course_cursor(item):
    """Sort cursor (name, id) for a course row, used by keyset pagination."""
    try:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if name is None:
            return None
        return {"name": name, "id": item.get("id")}
    except Exception:
        return None


async def _fetch_global_courses_keyset(db, page_size: int, after=None, before=None):
    """Keyset (cursor) page of all embedded courses ordered by (name, id).

    Avoids the O(offset) cost of `skip` for deep pages: `after` fetches the
    page following a cursor, `before` the page preceding it (queried
    descending, then reversed). Returns (items, has_more). Backed by the
    compound {courses.name, courses.id} index so it stays a bounded index scan.
    """
    try:
        pipeline = [
            {"$unwind": "$courses"},
            {
                "$project": {
                    "name": "$courses.name",
                    "link": "$courses.link",
                    "category": "$name",
                    "id": "$courses.id",
                },
            },
        ]
        cursor = before if before else after
        if cursor and cursor.get("name") is not None:
            if before:
                pipeline.append(
                    {
                        "$match": {
                            "$or": [
                                {"name": {"$lt": cursor["name"]}},
                                {"name": cursor["name"], "id": {"$lt": cursor.get("id")}},
                            ],
                        },
                    },
                )
            else:
                pipeline.append(
                    {
                        "$match": {
                            "$or": [
                                {"name": {"$gt": cursor["name"]}},
                                {"name": cursor["name"], "id": {"$gt": cursor.get("id")}},
                            ],
                        },
                    },
                )
        direction = -1 if before else 1
        pipeline.extend([{"$sort": {"name": direction, "id": direction}}, {"$limit": page_size + 1}])
        rows = await db.categories.aggregate(pipeline).to_list(length=page_size + 1)
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        if before:
            rows = list(reversed(rows))
        return rows, has_more
    except Exception:
        return [], False


def _clamp_courses_page(page: int, total, page_size: int):
    if total is None:
        return page
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    if page < 1:
        return 1
    if page > total_pages:
        return total_pages
    return page


async def _fetch_category_courses_page(db, category, page: int, page_size: int):
    for _attempt in range(2):
        try:
            await ensure_course_uuids(db, category)
        except Exception:
            pass
        start = (page - 1) * page_size
        items_pipeline = [
            {"$match": {"$or": [{"name": category}, {"path": category}]}},
            {"$unwind": "$courses"},
            {
                "$project": {
                    "name": "$courses.name",
                    "link": "$courses.link",
                    "category": "$name",
                    "id": {"$ifNull": ["$courses.id", None]},
                },
            },
            {"$sort": {"name": 1}},
            {"$skip": start},
            {"$limit": page_size + 1},
        ]
        try:
            items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
        except Exception:
            items = []
        has_more = len(items) > page_size
        slice_items = items[:page_size]
        slice_items = sorted(slice_items, key=lambda c: (c.get("name") or "").lower())
        if slice_items or page == 1:
            return slice_items, has_more, page
        try:
            fresh = await _courses_origin_total(db, "category", category, fresh=True)
        except Exception:
            fresh = None
        if fresh is None:
            return slice_items, has_more, page
        new_page = _clamp_courses_page(page, fresh, page_size)
        if new_page == page:
            return slice_items, has_more, page
        page = new_page
    return slice_items, has_more, page


# ----------  courses callback  ----------
async def courses_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    logger.debug("courses_callback invoked with data=%s", data)
    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, "data", None),
        )
        return
    page_size = PAGE_SIZE
    # Bot-wide course total (cached) for the 'shown/total' progress counter.
    try:
        overall_total = await _get_bot_courses_total(db)
    except Exception:
        overall_total = None

    try:
        if data.startswith("courses_ref::"):
            key = data.split("::", 1)[1]
            payload = await _resolve_callback_payload(key)
            if not payload:
                await safe_edit_message(
                    query,
                    "Reference expired. Please open the list again.",
                    action_key=getattr(query, "data", None),
                )
                return
            if payload.get("type") == "courses_page":
                items = payload.get("items") or []
                page = int(payload.get("page", 1) or 1)
                origin_type = payload.get("origin_type") or "global"
                category = payload.get("category")
                origin_ctx = payload.get("origin_context")
                origin_ctx_page = payload.get("origin_context_page")
                total_count = int(payload.get("total_count")) if payload.get("total_count") is not None else None

                if not items:
                    try:
                        if origin_type == "global":
                            total_count = await _courses_origin_total(db, "global")
                            page = _clamp_courses_page(page, total_count, page_size)
                            after_cursor = payload.get("after")
                            before_cursor = payload.get("before")
                            if after_cursor or before_cursor:
                                # Keyset navigation: bounded index scan, no O(offset) skip.
                                items, has_more = await _fetch_global_courses_keyset(
                                    db,
                                    page_size,
                                    after=after_cursor,
                                    before=before_cursor,
                                )
                            else:
                                start = (page - 1) * page_size
                                items_pipeline = [
                                    {"$unwind": "$courses"},
                                    {
                                        "$project": {
                                            "name": "$courses.name",
                                            "link": "$courses.link",
                                            "category": "$name",
                                            "id": {"$ifNull": ["$courses.id", None]},
                                        },
                                    },
                                    {"$sort": {"name": 1}},
                                    {"$skip": start},
                                    {"$limit": page_size + 1},
                                ]
                                try:
                                    items_result = await db.categories.aggregate(items_pipeline).to_list(
                                        length=page_size + 1,
                                    )
                                except Exception:
                                    items_result = []
                                has_more = len(items_result) > page_size
                                items = items_result[:page_size]
                                items = sorted(items, key=lambda c: (c.get("name") or "").lower())
                            if total_count is None:
                                total_count = (
                                    page * page_size + 1 if has_more else ((page - 1) * page_size + len(items))
                                )

                        elif origin_type == "category":
                            total_count = await _courses_origin_total(db, "category", category)
                            page = _clamp_courses_page(page, total_count, page_size)
                            items, has_more, page = await _fetch_category_courses_page(
                                db,
                                category,
                                page,
                                page_size,
                            )
                            if total_count is None:
                                total_count = (
                                    page * page_size + 1 if has_more else ((page - 1) * page_size + len(items))
                                )

                        elif origin_type == "coach":
                            coach_name = category
                            total_count = await _courses_origin_total(db, "coach", coach_name)
                            page = _clamp_courses_page(page, total_count, page_size)
                            start = (page - 1) * page_size
                            items_pipeline = [
                                {"$match": {"courses.coach": coach_name}},
                                {"$unwind": "$courses"},
                                {
                                    "$project": {
                                        "name": "$courses.name",
                                        "link": "$courses.link",
                                        "category": "$name",
                                        "coach": "$courses.coach",
                                        "id": {"$ifNull": ["$courses.id", None]},
                                    },
                                },
                                {"$match": {"coach": coach_name}},
                                {"$sort": {"name": 1}},
                                {"$skip": start},
                                {"$limit": page_size + 1},
                            ]
                            try:
                                items_result = await db.categories.aggregate(items_pipeline).to_list(
                                    length=page_size + 1,
                                )
                            except Exception:
                                items_result = []
                            has_more = len(items_result) > page_size
                            items = items_result[:page_size]
                            items = sorted(items, key=lambda c: (c.get("name") or "").lower())
                            if total_count is None:
                                total_count = (
                                    page * page_size + 1 if has_more else ((page - 1) * page_size + len(items))
                                )

                    except Exception:
                        items = []

                text, reply_markup = build_courses_page(
                    items,
                    page=page,
                    origin_type=origin_type,
                    category=category,
                    origin_context=origin_ctx,
                    origin_context_page=origin_ctx_page,
                    total_count=total_count,
                    is_page=True,
                    store_page_ref=False,
                    overall_total=overall_total,
                    page_first_cursor=_course_cursor(items[0]) if items else None,
                    page_last_cursor=_course_cursor(items[-1]) if items else None,
                )
                if not text:
                    await safe_edit_message(query, "No courses found.", action_key=getattr(query, "data", None))
                    return
                try:
                    kb = list(reply_markup.inline_keyboard)
                    if origin_type == "category" and category:
                        kb.append(
                            [
                                InlineKeyboardButton(
                                    "\U0001f50d Search",
                                    callback_data=_search_category_courses_cb(category, page),
                                ),
                            ],
                        )
                    elif origin_type == "coach" and category:
                        kb.append(
                            [
                                InlineKeyboardButton(
                                    "\U0001f50d Search",
                                    callback_data=_search_courses_coach_cb(category, page),
                                ),
                            ],
                        )
                    else:
                        kb.append(
                            [
                                InlineKeyboardButton(
                                    "\U0001f50d Search",
                                    callback_data=f"search_courses::{origin_type}::{page}",
                                ),
                            ],
                        )
                    reply_markup = InlineKeyboardMarkup(kb)
                except Exception:
                    pass
                await safe_edit_message(
                    query,
                    text=text,
                    reply_markup=reply_markup,
                    action_key=getattr(query, "data", None),
                )
                return

        if data.startswith("courses::"):
            payload = data.replace("courses::", "", 1)
            parts = payload.split("::")

            try:
                if parts[0] in ("global", "category", "coach"):
                    kind = parts[0]
                    if kind == "global":
                        page = int(parts[1])
                        total_courses = await _courses_origin_total(db, "global")
                        page = _clamp_courses_page(page, total_courses, page_size)
                        start = (page - 1) * page_size
                        items_pipeline = [
                            {"$unwind": "$courses"},
                            {
                                "$project": {
                                    "name": "$courses.name",
                                    "link": "$courses.link",
                                    "category": "$name",
                                    "id": {"$ifNull": ["$courses.id", None]},
                                },
                            },
                            {"$sort": {"name": 1}},
                            {"$skip": start},
                            {"$limit": page_size + 1},
                        ]
                        cache_key = f"page:global:{page}"
                        cached = _get_cached_page(cache_key)
                        if cached is not None:
                            items = cached
                        else:
                            try:
                                items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                            except Exception:
                                items = []
                            try:
                                _set_cached_page(cache_key, items, ttl=3)
                            except Exception:
                                pass
                        has_more = len(items) > page_size
                        all_courses = items[:page_size]
                        all_courses = sorted(all_courses, key=lambda c: (c.get("name") or "").lower())
                        if total_courses is None:
                            total_courses = (
                                page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses))
                            )
                        text, reply_markup = build_courses_page(
                            all_courses,
                            page=page,
                            origin_type="global",
                            total_count=total_courses,
                            is_page=True,
                            overall_total=overall_total,
                        )
                        if not text:
                            await safe_edit_message(
                                query,
                                f"No courses found on page {page}.",
                                action_key=getattr(query, "data", None),
                            )
                            return
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append(
                                [
                                    InlineKeyboardButton(
                                        "\U0001f50d Search",
                                        callback_data=f"search_courses::global::{page}",
                                    ),
                                ],
                            )
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        await safe_edit_message(
                            query,
                            text=text,
                            reply_markup=reply_markup,
                            action_key=getattr(query, "data", None),
                        )
                        return

                    if kind == "category":
                        category = urllib.parse.unquote_plus(parts[1])
                        page = int(parts[2])
                        origin_ctx = None
                        origin_ctx_page = None
                        if len(parts) > 3:
                            try:
                                if parts[3] == "from_parent" and len(parts) >= 6:
                                    origin_ctx = urllib.parse.unquote_plus(parts[4])
                                    try:
                                        origin_ctx_page = int(parts[5])
                                    except Exception:
                                        origin_ctx_page = None
                            except Exception:
                                origin_ctx = None
                        total_courses = await _courses_origin_total(db, "category", category)
                        page = _clamp_courses_page(page, total_courses, page_size)
                        courses, has_more, page = await _fetch_category_courses_page(
                            db,
                            category,
                            page,
                            page_size,
                        )
                        if origin_ctx is None:
                            try:
                                pdoc = await db.categories.find_one(
                                    {"$or": [{"name": category}, {"path": category}]},
                                    projection={"parent": 1},
                                )
                                parent = pdoc.get("parent") if pdoc else None
                                if parent:
                                    pp = await db.categories.find_one(
                                        {"$or": [{"name": parent}, {"path": parent}]},
                                        projection={"path": 1},
                                    )
                                    origin_ctx = pp.get("path") if pp and pp.get("path") else parent
                            except Exception:
                                origin_ctx = None
                        if total_courses is None:
                            total_courses = (
                                page * page_size + 1 if has_more else ((page - 1) * page_size + len(courses))
                            )
                        text, reply_markup = build_courses_page(
                            courses,
                            page=page,
                            origin_type="category",
                            category=category,
                            origin_context=origin_ctx,
                            origin_context_page=origin_ctx_page,
                            total_count=total_courses,
                            is_page=True,
                            store_page_ref=True,
                            overall_total=overall_total,
                        )
                        if not text:
                            await safe_edit_message(
                                query,
                                f"No courses found in category '{category}' on page {page}.",
                                action_key=getattr(query, "data", None),
                            )
                            return
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append(
                                [
                                    InlineKeyboardButton(
                                        "\U0001f50d Search",
                                        callback_data=_search_category_courses_cb(category, page),
                                    ),
                                ],
                            )
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        try:
                            design = await _resolve_design_for_category_name(db, category)
                        except Exception:
                            design = None
                        await _send_design_photo(
                            query, context, text, reply_markup, force_design=design
                        )
                        return

                    if kind == "coach":
                        coach_slug = urllib.parse.unquote_plus(parts[1])
                        page = int(parts[2])
                        coach_name = coach_slug
                        total_courses = await _courses_origin_total(db, "coach", coach_name)
                        page = _clamp_courses_page(page, total_courses, page_size)
                        start = (page - 1) * page_size
                        items_pipeline = [
                            {"$match": {"courses.coach": coach_name}},
                            {"$unwind": "$courses"},
                            {
                                "$project": {
                                    "name": "$courses.name",
                                    "link": "$courses.link",
                                    "category": "$name",
                                    "coach": "$courses.coach",
                                    "id": {"$ifNull": ["$courses.id", None]},
                                },
                            },
                            {"$match": {"coach": coach_name}},
                            {"$sort": {"name": 1}},
                            {"$skip": start},
                            {"$limit": page_size + 1},
                        ]
                        try:
                            items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                        except Exception:
                            items = []
                        has_more = len(items) > page_size
                        coach_courses = items[:page_size]
                        coach_courses = sorted(coach_courses, key=lambda c: (c.get("name") or "").lower())
                        if total_courses is None:
                            total_courses = (
                                page * page_size + 1 if has_more else ((page - 1) * page_size + len(coach_courses))
                            )
                        text, reply_markup = build_courses_page(
                            coach_courses,
                            page=page,
                            origin_type="coach",
                            category=coach_name,
                            origin_context=None,
                            total_count=total_courses,
                            is_page=True,
                            store_page_ref=True,
                            overall_total=overall_total,
                        )
                        if not text:
                            await safe_edit_message(
                                query,
                                f"No courses found for coach '{coach_name}' on page {page}.",
                                action_key=getattr(query, "data", None),
                            )
                            return
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append(
                                [
                                    InlineKeyboardButton(
                                        "\U0001f50d Search",
                                        callback_data=_search_courses_coach_cb(coach_name, page),
                                    ),
                                ],
                            )
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        try:
                            design = await _resolve_design_for_category_name(db, coach_name)
                        except Exception:
                            design = None
                        await _send_design_photo(
                            query, context, text, reply_markup, force_design=design
                        )
                        return
                else:
                    if len(parts) == 1:
                        page = int(parts[0])
                        total_courses = await _courses_origin_total(db, "global")
                        page = _clamp_courses_page(page, total_courses, page_size)
                        start = (page - 1) * page_size
                        items_pipeline = [
                            {"$unwind": "$courses"},
                            {
                                "$project": {
                                    "name": "$courses.name",
                                    "link": "$courses.link",
                                    "category": "$name",
                                    "id": {"$ifNull": ["$courses.id", None]},
                                },
                            },
                            {"$sort": {"name": 1}},
                            {"$skip": start},
                            {"$limit": page_size + 1},
                        ]
                        try:
                            items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                        except Exception:
                            items = []
                        has_more = len(items) > page_size
                        all_courses = items[:page_size]
                        all_courses = sorted(all_courses, key=lambda c: (c.get("name") or "").lower())
                        if total_courses is None:
                            total_courses = (
                                page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses))
                            )
                        text, reply_markup = build_courses_page(
                            all_courses,
                            page=page,
                            origin_type="global",
                            origin_context=None,
                            total_count=total_courses,
                            is_page=True,
                        )
                        if not text:
                            await safe_edit_message(
                                query,
                                f"No courses found on page {page}.",
                                action_key=getattr(query, "data", None),
                            )
                            return
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append(
                                [
                                    InlineKeyboardButton(
                                        "\U0001f50d Search",
                                        callback_data=f"search_courses::global::{page}",
                                    ),
                                ],
                            )
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        await safe_edit_message(
                            query,
                            text=text,
                            reply_markup=reply_markup,
                            action_key=getattr(query, "data", None),
                        )
                        return
                    category = urllib.parse.unquote_plus(parts[0])
                    try:
                        page = int(parts[1])
                    except Exception:
                        await safe_edit_message(query, "Invalid page number.", action_key=getattr(query, "data", None))
                        return
                    origin_ctx = None
                    origin_ctx_page = None
                    if len(parts) > 2:
                        try:
                            if parts[2] == "from_parent" and len(parts) >= 5:
                                origin_ctx = urllib.parse.unquote_plus(parts[3])
                                try:
                                    origin_ctx_page = int(parts[4])
                                except Exception:
                                    origin_ctx_page = None
                        except Exception:
                            origin_ctx = None
                    try:
                        await ensure_course_uuids(db, category)
                    except Exception:
                        pass
                    category_doc = await db.categories.find_one({"name": category})
                    if not category_doc or not category_doc.get("courses"):
                        await safe_edit_message(
                            query,
                            f"No courses found in category '{category}' on page {page}.",
                            action_key=getattr(query, "data", None),
                        )
                        return
                    courses = category_doc.get("courses", [])
                    courses = sorted(courses, key=lambda c: (c.get("name") or "").lower())
                    page = _clamp_courses_page(page, len(courses), page_size)
                    if origin_ctx is None:
                        try:
                            parent = category_doc.get("parent")
                            if parent:
                                pdoc = await db.categories.find_one({"name": parent})
                                origin_ctx = pdoc.get("path") if pdoc and pdoc.get("path") else parent
                        except Exception:
                            origin_ctx = None
                    text, reply_markup = build_courses_page(
                        courses,
                        page=page,
                        origin_type="category",
                        category=category,
                        origin_context=origin_ctx,
                        origin_context_page=origin_ctx_page,
                        overall_total=overall_total,
                    )
                    if not text:
                        await safe_edit_message(
                            query,
                            f"No courses found in category '{category}' on page {page}.",
                            action_key=getattr(query, "data", None),
                        )
                        return
                    try:
                        kb = list(reply_markup.inline_keyboard)
                        kb.append(
                            [
                                InlineKeyboardButton(
                                    "\U0001f50d Search",
                                    callback_data=_search_category_courses_cb(category, page),
                                ),
                            ],
                        )
                        reply_markup = InlineKeyboardMarkup(kb)
                    except Exception:
                        pass
                    try:
                        design = await _resolve_design_for_category_name(db, category)
                    except Exception:
                        design = None
                    await _send_design_photo(
                        query, context, text, reply_markup, force_design=design
                    )
                    return
            except Exception:
                logger.exception("Error parsing courses callback")
                await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
                return

        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, "data", None))
        return
    except Exception:
        logger.exception("Error handling courses callback")
        await safe_edit_message(
            query,
            "An error occurred while fetching courses. Please try again later.",
            action_key=getattr(query, "data", None),
        )
