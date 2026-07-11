from telegram.ext import ConversationHandler, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import logging
from conversation_states import CREATE_CAT_NAME, CREATE_CAT_PARENT
from handlers.db_connection import get_db  # Importing get_db from db_connection.py
from database.mongo_handler import MongoDB  # Import MongoDB
import re  # For URL validation
from pymongo.errors import DuplicateKeyError
import urllib.parse
import os
import uuid
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def is_uuid(s: str) -> bool:
    try:
        return bool(UUID_RE.match(str(s).strip()))
    except Exception:
        return False
from datetime import datetime, timedelta
import hashlib
import json
import time
import asyncio
from typing import Optional
# How long to persist callback refs (seconds). Default: 7 days.
CALLBACK_REF_TTL = int(os.getenv("CALLBACK_REF_TTL", str(7 * 24 * 3600)))
import math
from telegram.error import RetryAfter, BadRequest
from contextlib import asynccontextmanager

# In-memory mapping for short callback ids -> payload
CALLBACK_MAP = {}

# How long to keep an interactive inline keyboard session open (seconds)
def _parse_ttl(value, default=300):
    if value is None or str(value).strip() == "":
        return default
    s = str(value).strip()
    try:
        if s.isdigit():
            return int(s)
        m = re.match(r"^(\d+)([smhd])$", s, re.I)
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

# Reusable filter for top-level categories that handles missing, null, or empty-string parents
TOP_LEVEL_FILTER = {"$or": [{"parent": {"$exists": False}}, {"parent": None}, {"parent": ""}]}

# Basic DB timing and count helpers (ensure available early so handlers can use them)
from contextlib import asynccontextmanager

# Simple in-memory TTL cache for inexpensive totals; keyed by JSON'd filter.
_COUNT_CACHE = {}
_PAGE_CACHE = {}  # key -> (payload, expire_ts)
PAGE_CACHE_TTL = int(os.getenv("PAGE_CACHE_TTL", "30"))
# Short in-memory cache for resolved callback payloads to avoid Redis/Mongo
# round-trips for hot refs. Values: key -> (payload, expire_ts)
_CALLBACK_RESOLVE_CACHE = {}
_CALLBACK_RESOLVE_CACHE_MAX = int(os.getenv('CALLBACK_RESOLVE_CACHE_MAX', '2000'))


def _set_callback_resolve_cache(key: str, payload, ttl: int = 60):
    """Set a payload into the small in-process resolve cache with TTL
    and enforce a maximum size to avoid unbounded memory growth.
    """
    try:
        expire = time.time() + ttl
        _CALLBACK_RESOLVE_CACHE[key] = (payload, expire)
        # prune if too large: remove entries with oldest expiry first
        if len(_CALLBACK_RESOLVE_CACHE) > _CALLBACK_RESOLVE_CACHE_MAX:
            # sort by expire_ts ascending and drop oldest quarter
            items = sorted(_CALLBACK_RESOLVE_CACHE.items(), key=lambda kv: kv[1][1])
            drop = max(1, len(items) // 4)
            for k, _ in items[:drop]:
                try:
                    del _CALLBACK_RESOLVE_CACHE[k]
                except Exception:
                    pass
    except Exception:
        pass

# Tracks sessions (chat_id, message_id) that should be kept open
# when the scheduled close worker runs. Handlers that render coach
# lists set this so the session isn't closed while the user is browsing.
_SESSION_KEEP_OPEN = set()


def _set_session_keep_open(message, keep: bool = True):
    try:
        chat_id = getattr(message, 'chat', None).id if getattr(message, 'chat', None) else None
        msg_id = getattr(message, 'message_id', None)
        if chat_id is None or msg_id is None:
            return
        key = (int(chat_id), int(msg_id))
        if keep:
            _SESSION_KEEP_OPEN.add(key)
        else:
            _SESSION_KEEP_OPEN.discard(key)
    except Exception:
        pass

@asynccontextmanager
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
    """Get total count with optional caching. Prefers Redis when configured.

    coll_name is the collection attribute name on the db object (e.g. "categories").
    """
    key = f"count:{coll_name}:{json.dumps(filter_q or {}, sort_keys=True)}"
    now = time.time()
    entry = _COUNT_CACHE.get(key)
    if entry and entry[1] > now:
        return entry[0]

    # Try Redis first (best-effort)
    try:
        if _redis is not None:
            val = await _redis.get(key)
            if val is not None:
                try:
                    cnt = int(val)
                    _COUNT_CACHE[key] = (cnt, now + ttl)
                    return cnt
                except Exception:
                    pass
    except Exception:
        pass

    # Fallback: run count_documents on the collection
    try:
        coll = getattr(db, coll_name) if hasattr(db, coll_name) else db[coll_name]
        cnt = await coll.count_documents(filter_q or {})
    except Exception:
        cnt = 0

    _COUNT_CACHE[key] = (cnt, now + ttl)
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
    """Return True if `courses` contains at least one real course (not the
    placeholder '(empty') or empty dicts. Accepts list-like structures."""
    try:
        if not courses:
            return False
        for c in courses:
            if not c:
                continue
            if isinstance(c, dict):
                name = c.get('name')
            else:
                # allow legacy string entries
                name = c
            if name and str(name).strip():
                return True
    except Exception:
        return False
    return False


def _set_cached_page(key: str, payload, ttl: int = 3):
    _PAGE_CACHE[key] = (payload, time.time() + ttl)
    # best-effort Redis backing for multi-process deployments
    try:
        if _redis is not None:
            asyncio.create_task(_redis.set(key, json.dumps(payload), ex=ttl))
    except Exception:
        pass


async def _get_courses_count(db, category: str, ttl: int = 60):
    """Return the number of courses stored in a category, with in-process
    caching and optional Redis backing. This avoids repeated aggregations
    for hot categories and speeds up pagination/back-button computations.
    """
    key = f"count:category_courses:{category}"
    now = time.time()
    entry = _COUNT_CACHE.get(key)
    if entry and entry[1] > now:
        return entry[0]
    # Try Redis first (best-effort)
    try:
        if _redis is not None:
            val = await _redis.get(key)
            if val is not None:
                try:
                    cnt = int(val)
                except Exception:
                    cnt = 0
                _COUNT_CACHE[key] = (cnt, now + ttl)
                return cnt
    except Exception:
        pass

    # Fallback: aggregation to compute array size
    try:
        pipeline = [{"$match": {"name": category}}, {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}}]
        agg = await db.categories.aggregate(pipeline).to_list(length=1)
        cnt = int(agg[0].get('n', 0)) if agg else 0
    except Exception:
        cnt = 0

    _COUNT_CACHE[key] = (cnt, now + ttl)
    try:
        if _redis is not None:
            # best-effort async set
            asyncio.create_task(_redis.set(key, str(cnt), ex=ttl))
    except Exception:
        pass
    return cnt


def schedule_close_inline_message(message, delay: int = None, notice: str = "(Session closed due to inactivity)"):
    """Schedule removal of inline keyboard from a sent Message after `delay` seconds.

    This prefers editing the message to remove `reply_markup` and append a short notice.
    Runs in background via asyncio.create_task.
    """
    if delay is None:
        delay = GUI_SESSION_TTL

    async def _worker():
        await asyncio.sleep(delay)
        try:
            # If this message has been marked to keep open (e.g., coach view),
            # skip auto-closing.
            try:
                chat_id = getattr(message, 'chat', None).id if getattr(message, 'chat', None) else None
                msg_id = getattr(message, 'message_id', None)
                if chat_id is not None and msg_id is not None and (int(chat_id), int(msg_id)) in _SESSION_KEEP_OPEN:
                    return
            except Exception:
                pass

            orig = getattr(message, 'text', None) or getattr(message, 'caption', None) or ''
            # Try removing inline keyboard first
            try:
                await message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            # Then try to append a short notice so user knows it's closed
            try:
                new_text = (orig or '')
                if notice:
                    new_text = new_text + "\n\n" + notice
                # Detect if message has a photo and use edit_caption instead
                if getattr(message, 'photo', None):
                    await message.edit_caption(caption=new_text)
                else:
                    await message.edit_text(new_text)
            except Exception:
                pass
        except Exception:
            logger.error("Error in schedule_close_inline_message worker")

    try:
        asyncio.create_task(_worker())
    except Exception:
        # Environment may not support creating background tasks; ignore.
        pass
def _make_course_ref(category: str, name: str, origin_type: str, origin_page: int, origin_context: str = None, origin_context_page: int = None, course_id: str = None) -> str:
    # Compute a concrete back callback so details can always return to the
    # exact originating UI (category/coach/global) without guessing.
    page_to_use = origin_context_page or origin_page or 1
    if origin_type == 'category':
        target = origin_context or category
        back_cb = f"courses::category::{urllib.parse.quote_plus(str(target))}::{page_to_use}"
    elif origin_type == 'coach':
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
    # Use the central storage helper so refs are persisted (Redis/Mongo) as a best-effort.
    key = _store_callback_payload(payload)
    try:
        logger.debug("_make_course_ref: stored key=%s category=%s name=%s origin_type=%s origin_page=%s origin_context=%s back_cb=%s", key, category, name, origin_type, origin_page, origin_context, back_cb)
    except Exception:
        pass
    # Append an encoded back callback to the returned callback_data so the
    # Details view can use it directly without resolving the stored payload.
    try:
        enc = urllib.parse.quote_plus(back_cb)
        candidate = f"course_ref::{key}::back::{enc}"
        # Telegram callback_data must be <= 64 bytes. Don't append the
        # back token if it would exceed that limit; fall back to stored ref.
        if len(candidate.encode('utf-8')) <= 64:
            logger.debug("_make_course_ref: using inline candidate (len=%d)", len(candidate.encode('utf-8')))
            return candidate
        else:
            logger.debug("_make_course_ref: candidate too long (%d bytes), returning stored key course_ref::%s", len(candidate.encode('utf-8')), key)
            return f"course_ref::{key}"
    except Exception:
        return f"course_ref::{key}"


def _store_callback_payload(payload: dict) -> str:
    """Store an arbitrary payload and return a short key."""
    key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    CALLBACK_MAP[key] = payload
    try:
        logger.debug("_store_callback_payload: key=%s payload=%s", key, payload)
    except Exception:
        pass
    # best-effort background persist to Redis or Mongo so refs survive restarts
    try:
        # If Redis is configured, persist asynchronously as before.
        if _redis is not None:
            try:
                asyncio.create_task(_persist_callback_payload(key, payload))
            except Exception:
                # fall back to scheduling not-critical background task
                pass
        else:
            # Redis not configured -> strong durability requested: perform
            # synchronous blocking write to MongoDB so callers return only
            # after the ref is durably stored.
            try:
                _persist_callback_payload_sync(key, payload)
            except Exception:
                # If sync persist fails, still return the key (in-memory map)
                logger.exception("Synchronous persist to MongoDB failed for key=%s", key)
    except Exception:
        pass
    return key


def _persist_callback_payload_sync(key: str, payload: dict, ttl: int = 60 * 60 * 24 * 7):
    """Synchronously persist callback payload to MongoDB using pymongo.

    This blocks the current thread until the write completes and is used
    when Redis is not configured to provide stronger durability guarantees.
    """
    try:
        try:
            sync_db = MongoDB.get_sync_db()
        except Exception:
            logger.exception("_persist_callback_payload_sync: failed to get sync DB")
            return
        expire_at = datetime.utcnow() + timedelta(seconds=ttl)
        try:
            # Ensure TTL index exists (idempotent). Using sync driver.
            try:
                sync_db.callback_refs.create_index("expireAt", expireAfterSeconds=0)
            except Exception:
                pass
            sync_db.callback_refs.update_one({"_id": key}, {"$set": {"payload": payload, "expireAt": expire_at}}, upsert=True)
        except Exception:
            logger.exception("_persist_callback_payload_sync: write failed for key=%s", key)
    except Exception:
        logger.exception("_persist_callback_payload_sync: unexpected error for key=%s", key)


def _shorten_showcat_cb(path: str, page: int, from_parent: Optional[str] = None, parent_page: Optional[int] = None):
    """Return a safe callback_data for showcat views.

    Prefer the direct `showcat::{path}::{page}` when it fits; otherwise
    store a short `showcat_ref::<key>` payload. If `from_parent` and
    `parent_page` are provided, include them in the stored payload so
    back-navigation can restore the originating categories page.
    """
    try:
        # If origin metadata is provided, always persist it so Back can
        # reliably restore the originating categories page. This avoids
        # losing `parent_page` when an inline `showcat::...` callback
        # would otherwise be used.
        if from_parent is not None or parent_page is not None:
            payload = {"type": "showcat", "path": path, "page": page}
            if from_parent is not None:
                payload["from_parent"] = from_parent
            if parent_page is not None:
                payload["parent_page"] = parent_page
            try:
                key = _store_callback_payload(payload)
                return f"showcat_ref::{key}"
            except Exception:
                # Fall back to inline representation if persistence fails
                pass

        cb = f"showcat::{urllib.parse.quote_plus(path)}::{page}"
        if len(cb.encode('utf-8')) <= 64:
            return cb
        payload = {"type": "showcat", "path": path, "page": page}
        key = _store_callback_payload(payload)
        return f"showcat_ref::{key}"
    except Exception:
        return f"showcat::{urllib.parse.quote_plus(path)}::{page}"


async def _persist_callback_payload(key: str, payload: dict, ttl: int = 60 * 60 * 24 * 7):
    """Persist callback payload to Redis (preferred) or MongoDB (fallback).
    TTL defaults to 7 days.
    """
    # Try Redis
    try:
        if _redis is not None:
            import json as _json
            await _redis.set(f"callback:ref:{key}", json.dumps(payload), ex=ttl)


            return
    except Exception:
        logger.error("Failed to persist callback payload to Redis")

    # Fallback to MongoDB
    try:
        db = await get_db()
        if db is None:
            return
        from datetime import datetime, timedelta
        expire_at = datetime.utcnow() + timedelta(seconds=ttl)
        # ensure TTL index exists (idempotent)
        try:
            await db.callback_refs.create_index("expireAt", expireAfterSeconds=0)
        except Exception:
            pass
        await db.callback_refs.update_one({"_id": key}, {"$set": {"payload": payload, "expireAt": expire_at}}, upsert=True)
    except Exception:
        logger.error("Failed to persist callback payload to MongoDB")


async def _resolve_callback_payload(key: str):
    """Resolve a callback payload by checking in-memory map, then Redis, then MongoDB."""
    # Short in-process cache for recently-resolved payloads
    try:
        now = time.time()
        entry = _CALLBACK_RESOLVE_CACHE.get(key)
        if entry and entry[1] > now:
            return entry[0]
    except Exception:
        pass

    # In-memory primary map
    payload = CALLBACK_MAP.get(key)
    if payload:
        _set_callback_resolve_cache(key, payload, ttl=60)
        return payload

    # Redis
    try:
        if _redis is not None:
            val = await _redis.get(f"callback:ref:{key}")
            if val:
                import json as _json
                payload = json.loads(val)
                CALLBACK_MAP[key] = payload
                _set_callback_resolve_cache(key, payload, ttl=60)
                return payload
    except Exception:
        logger.error("Failed to read callback payload from Redis")

    # MongoDB fallback
    try:
        db = await get_db()
        if db is None:
            return None
        doc = await db.callback_refs.find_one({"_id": key})
        if doc:
            payload = doc.get('payload')
            if payload:
                CALLBACK_MAP[key] = payload
                _set_callback_resolve_cache(key, payload, ttl=60)
                return payload
    except Exception:
        logger.error("Failed to read callback payload from MongoDB")

    return None


async def _rehydrate_callback_map(limit: int = None):
    """Load recent unexpired callback refs from Mongo into in-memory map.

    This helps survive process restarts when Redis isn't configured.
    `limit` controls the maximum number of docs to load (None -> env or 10000).
    """
    try:
        cfg_limit = int(os.getenv('CALLBACK_REHYDRATE_LIMIT', '10000'))
    except Exception:
        cfg_limit = 10000
    if limit is None:
        limit = cfg_limit

    try:
        db = await get_db()
        if db is None:
            return 0


        # end _rehydrate_callback_map
        now = datetime.utcnow()
        try:
            # Ensure TTL index exists idempotently
            try:
                await db.callback_refs.create_index("expireAt", expireAfterSeconds=0)
            except Exception:
                pass
            cursor = db.callback_refs.find({"expireAt": {"$gt": now}}).limit(limit)
            docs = await cursor.to_list(length=limit)
            count = 0
            for d in docs:
                try:
                    key = d.get('_id')
                    payload = d.get('payload')
                    if key and payload:
                        CALLBACK_MAP[key] = payload
                        count += 1
                except Exception:
                    continue
            logger.debug("Rehydrated %s callback refs from MongoDB", count)
            return count
        except Exception as e:
            logger.exception("_rehydrate_callback_map: failed to load callback refs: %s", e)
            return 0
    except Exception:
        return 0


async def _reconcile_back_cb(db, back_cb: str, course_category: str = None, origin_page: int = None):
    """Verify that `back_cb` actually resolves to a non-empty courses page.
    If it doesn't, try alternate lookups (unquote, name/path swap) and
    clamp the page to an available range. Returns a possibly-modified
    `back_cb` that is more likely to show the user useful results.
    """
    try:
        if not back_cb or not isinstance(back_cb, str):
            return back_cb

        # Only reconcile category-course callbacks for now
        if back_cb.startswith('courses::category::'):
            parts = back_cb.split('::')
            if len(parts) >= 4:
                raw_cat = urllib.parse.unquote_plus(parts[2])
                try:
                    page = int(parts[3])
                except Exception:
                    page = int(origin_page or 1)

                # Evict any short-lived cached page for this category/page
                try:
                    cache_key = f"page:category:{urllib.parse.quote_plus(str(raw_cat))}:{page}:{PAGE_SIZE}"
                    _PAGE_CACHE.pop(cache_key, None)
                    if _redis is not None:
                        try:
                            asyncio.create_task(_redis.delete(cache_key))
                        except Exception:
                            pass
                except Exception:
                    pass

                # try the requested page first (fresh fetch)
                try:
                    items = await get_courses_by_category(None, raw_cat, page)
                except Exception:
                    # get_courses_by_category accepts a user_id first in some calls
                    try:
                        items = await get_courses_by_category(0, raw_cat, page)
                    except Exception:
                        items = []

                if items and _has_real_courses(items):
                    return back_cb

                # If empty, try unquoting/alternative category representations
                alternates = [raw_cat]
                try:
                    # If the stored category looks like a path, also try matching by name
                    doc = await db.categories.find_one({"$or": [{"path": raw_cat}, {"name": raw_cat}]}, projection={"name": 1, "path": 1})
                    if doc:
                        alternates.append(doc.get('name') or raw_cat)
                        alternates.append(doc.get('path') or raw_cat)
                except Exception:
                    pass

                # Try alternates and also clamp to page 1 if necessary
                for alt in alternates:
                    try:
                        # Evict cached page for alternate
                        try:
                            alt_cache = f"page:category:{urllib.parse.quote_plus(str(alt))}:{page}:{PAGE_SIZE}"
                            _PAGE_CACHE.pop(alt_cache, None)
                            if _redis is not None:
                                try:
                                    asyncio.create_task(_redis.delete(alt_cache))
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
                        new_cb = f"courses::category::{urllib.parse.quote_plus(str(alt))}::{page}"
                        return new_cb

                # try clamping to page 1 as a last-ditch
                for alt in alternates:
                    try:
                        # Evict cached page for alternate page 1
                        try:
                            alt_cache = f"page:category:{urllib.parse.quote_plus(str(alt))}:1:{PAGE_SIZE}"
                            _PAGE_CACHE.pop(alt_cache, None)
                            if _redis is not None:
                                try:
                                    asyncio.create_task(_redis.delete(alt_cache))
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
                        new_cb = f"courses::category::{urllib.parse.quote_plus(str(alt))}::1"
                        return new_cb

        # For other callback kinds, leave unchanged
        return back_cb
    except Exception:
        return back_cb


async def _get_children_flags(db, names, ttl: int = 30):
    """Return a set of names that have children. Uses Redis as a cache when available.

    `names` is an iterable of category names. The function will check Redis
    first (mget) and only query MongoDB for misses, then populate Redis for
    future hits. Returns a set of names that have children.
    """
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
                        if s in ('1', 'true', 'True'):
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
            parents = {d.get('parent') for d in docs if d.get('parent')}
        except Exception:
            parents = set()
        # populate Redis for misses
        if _redis is not None:
            for n in misses:
                key = f"cat:has_children:{urllib.parse.quote_plus(n)}"
                val = '1' if n in parents else '0'
                try:
                    await _redis.setex(key, ttl, val)
                except Exception:
                    pass
        have |= parents
    return have


async def _prefetch_category_page(category_name: str, page: int = 1, page_size: int = None):
    """Background prefetch: fetch page for `category_name` and warm caches.

    This is fire-and-forget: callers should schedule with
    `asyncio.create_task(_prefetch_category_page(...))` so UI isn't blocked.
    """
    try:
        if page_size is None:
            page_size = PAGE_SIZE
        # call the main fetcher which itself will populate _PAGE_CACHE and Redis
        await get_courses_by_category(None, category_name, page=page, page_size=page_size)
    except Exception:
        pass

# Simple in-memory debounce/rate-limit to ignore very fast repeated
# callback presses from the same user. This reduces duplicated edits and
# avoids hitting Telegram's flood limits when users rapidly navigate pages.
_LAST_CALLBACK = {}
DEFAULT_DEBOUNCE = float(os.getenv("EDIT_DEBOUNCE", "0.5"))

def _is_debounced(user_id: int, action_key: str, interval: float = None) -> bool:
    if interval is None:
        interval = DEFAULT_DEBOUNCE
    now = time.time()
    key = (user_id, action_key)
    last = _LAST_CALLBACK.get(key)
    if last and (now - last) < interval:
        return True
    _LAST_CALLBACK[key] = now
    return False
_USER_BUCKETS = {}   # user_id -> {tokens, capacity, last_refill, refill_rate}
_GLOBAL_BUCKET = {"tokens": 20.0, "capacity": 20.0, "last_refill": time.time(), "refill_rate": 5.0}

# Optional Redis-backed token buckets for multi-process deployments.
REDIS_URL = os.getenv("REDIS_URL")
_redis = None
_redis_token_script = None
if REDIS_URL:
    try:
        import redis.asyncio as redis_async
        _redis = redis_async.from_url(REDIS_URL)
        # Lua script: atomically refill tokens based on elapsed time and
        # consume if available, otherwise return required wait seconds.
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


# Public wrapper for _get_total_count - accessible from other modules
async def get_total_count(db, coll_name: str, filter_q: dict = None, ttl: int = 60):
    """Cached count_documents with optional Redis backing.

    Wraps _get_total_count as a public API for use by other handlers.
    """
    return await _get_total_count(db, coll_name, filter_q, ttl)

def _refill_bucket(bucket):
    now = time.time()
    elapsed = now - bucket.get("last_refill", now)
    if elapsed <= 0:
        return
    bucket["tokens"] = min(bucket["capacity"], bucket.get("tokens", bucket["capacity"]) + elapsed * bucket["refill_rate"])
    bucket["last_refill"] = now

async def _consume_token(user_id: int, cost: float = 1.0):
    # If Redis is configured, prefer the Redis-backed atomic token bucket.
    if _redis is not None and _redis_token_script is not None:
        try:
            now = int(time.time())
            # global first
            res = await _redis.eval(_redis_token_script, 1, 'bucket:global', now, _GLOBAL_BUCKET['capacity'], _GLOBAL_BUCKET['refill_rate'], cost)
            # res is JSON like [1,0] or [0,wait]
            import json as _json
            ok, wait = json.loads(res)
            if ok == 1:
                # now consume user bucket
                user_key = f"bucket:user:{user_id}"
                res2 = await _redis.eval(_redis_token_script, 1, user_key, now, 5.0, 1.0, cost)
                ok2, wait2 = json.loads(res2)
                if ok2 == 1:
                    METRICS['token_consumed'] += 1
                    try:
                        asyncio.create_task(_redis.incr('metrics:token_consumed'))
                    except Exception:
                        pass
                    return True, 0
                else:
                    return False, wait2
            else:
                return False, wait
        except Exception:
            # Fall back to local in-memory buckets on any Redis error
            pass

    # Refill global (in-memory fallback)
    _refill_bucket(_GLOBAL_BUCKET)
    if _GLOBAL_BUCKET["tokens"] < cost:
        needed = cost - _GLOBAL_BUCKET["tokens"]
        wait = math.ceil(needed / _GLOBAL_BUCKET["refill_rate"])
        return False, wait
    # Refill / init user bucket
    b = _USER_BUCKETS.get(user_id)
    if b is None:
        b = {"tokens": 5.0, "capacity": 5.0, "last_refill": time.time(), "refill_rate": 1.0}
        _USER_BUCKETS[user_id] = b
    _refill_bucket(b)
    if b["tokens"] < cost:
        needed = cost - b["tokens"]
        wait = math.ceil(needed / b["refill_rate"])
        return False, wait
    # consume
    _GLOBAL_BUCKET["tokens"] -= cost
    b["tokens"] -= cost
    METRICS['token_consumed'] += 1
    try:
        if _redis is not None:
            # best-effort increment
            asyncio.create_task(_redis.incr('metrics:token_consumed'))
    except Exception:
        pass
    return True, 0


# Retry queue for scheduling edit retries when Telegram returns RetryAfter
# or when tokens are temporarily exhausted.
_RETRY_QUEUE = {}  # key -> asyncio.Task

def _retry_key_for(query):
    # Use chat_id + message_id if available; fall back to callback data
    chat_id = getattr(getattr(query, 'message', None), 'chat_id', None)
    msg_id = getattr(getattr(query, 'message', None), 'message_id', None)
    if chat_id and msg_id:
        return (chat_id, msg_id)
    return getattr(query, 'data', None) or 'callback'

def _schedule_retry(query, text, reply_markup=None, action_key=None, delay=1, max_retries=3):
    key = _retry_key_for(query)
    if key in _RETRY_QUEUE:
        return

    async def _retry_loop():
        tries = 0
        wait = delay
        while tries < max_retries:
            await asyncio.sleep(wait)
            tries += 1
            try:
                await query.edit_message_text(text, reply_markup=reply_markup)
                break
            except RetryAfter as e:
                wait = int(getattr(e, 'retry_after', wait) or wait)
                logger.warning("RetryAfter while retrying; will retry in %s seconds", wait)
                continue
            except Exception as e:
                logger.error("Retry loop error: %s", e)
                break
        # cleanup
        _RETRY_QUEUE.pop(key, None)

    task = asyncio.create_task(_retry_loop())
    _RETRY_QUEUE[key] = task


# Redis-backed retry scheduling and metrics (multi-process safe)
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
            r.append({"text": btn.text, "callback_data": getattr(btn, 'callback_data', None), "url": getattr(btn, 'url', None)})
        rows.append(r)
    return rows

def _deserialize_markup(rows):
    if not rows:
        return None
    kb = []
    for row in rows:
        r = []
        for b in row:
            if b.get('url'):
                r.append(InlineKeyboardButton(b['text'], url=b['url']))
            else:
                r.append(InlineKeyboardButton(b['text'], callback_data=b.get('callback_data')))
        kb.append(r)
    return InlineKeyboardMarkup(kb)

async def _redis_schedule_retry(chat_id, message_id, text, reply_markup, execute_at: int):
    """Schedule a retry in Redis sorted set. Payload stored as JSON."""
    if _redis is None:
        return
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": _serialize_markup(reply_markup)
    }
    try:
        await _redis.zadd("retry:queue", { json.dumps(payload): execute_at })
        METRICS['retry_scheduled'] += 1
        try:
            await _redis.incr('metrics:retry_scheduled')
        except Exception:
            pass
    except Exception:
        logger.error("Failed to schedule retry in Redis")

async def schedule_retry_via_redis_or_local(query, text, reply_markup=None, delay=1):
    # Try Redis-based scheduling first
    try:
        chat_id = getattr(getattr(query, 'message', None), 'chat_id', None)
        message_id = getattr(getattr(query, 'message', None), 'message_id', None)
        when = int(time.time()) + int(delay)
        if _redis is not None and chat_id and message_id:
            await _redis_schedule_retry(chat_id, message_id, text, reply_markup, when)
            return
    except Exception:
        logger.error("schedule_retry_via_redis_or_local failed")
    # Fallback: use in-process scheduler
    _schedule_retry(query, text, reply_markup=reply_markup, delay=delay)


async def _process_redis_retry_item(application, raw_member: str):
    import json as _json
    try:
        payload = json.loads(raw_member)
        chat_id = payload.get('chat_id')
        message_id = payload.get('message_id')
        text = payload.get('text')
        reply_markup = _deserialize_markup(payload.get('reply_markup'))
        try:
            await application.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
            METRICS['retry_executed'] += 1
            try:
                await _redis.incr('metrics:retry_executed')
            except Exception:
                pass
        except Exception as e:
            # If Telegram responds with RetryAfter, reschedule
            from telegram.error import RetryAfter
            if isinstance(e, RetryAfter):
                wait = getattr(e, 'retry_after', 5)
                execute_at = int(time.time()) + int(wait)
                await _redis.zadd('retry:queue', { raw_member: execute_at })
                return
            METRICS['retry_failed'] += 1
            try:
                await _redis.incr('metrics:retry_failed')
            except Exception:
                pass
            logger.error("Retry execution failed for payload %s", payload)
    except Exception:
        logger.error("Failed to process redis retry item: %s", raw_member)


async def start_redis_retry_worker(application):
    """Background worker that executes due retry items from Redis sorted set.
    This is safe to call even if Redis is not configured; it will just return.
    """
    if _redis is None:
        logger.debug("Redis not configured; skipping redis retry worker")
        return

    async def _worker():
        logger.info("Starting Redis retry worker")
        while True:
            try:
                now = int(time.time())
                # Get due items
                members = await _redis.zrangebyscore('retry:queue', '-inf', now, start=0, num=100)
                if not members:
                    await asyncio.sleep(1)
                    continue
                for raw in members:
                    # Try to remove atomically; if removed, process
                    removed = await _redis.zrem('retry:queue', raw)
                    if removed:
                        await _process_redis_retry_item(application, raw)
                await asyncio.sleep(0)
            except Exception:
                logger.error("Redis retry worker encountered an error")
                await asyncio.sleep(2)

    asyncio.create_task(_worker())



async def safe_edit_message(query, text: str, reply_markup=None, action_key: str = None, debounce_interval: float = None):
    """Edit a CallbackQuery message safely with rate-limiting, debounce,
    and automatic retry for RetryAfter.

    Behavior:
    - Debounces rapid repeated presses per-user using `_is_debounced`.
    - Checks global and per-user token buckets; if tokens unavailable,
      schedules a retry after the estimated wait time.
    - Attempts edit; on RetryAfter, schedules a retry using the provided
      retry_after value and returns False.
    - Falls back to sending a new message if edit fails for other reasons.
    """
    try:
        user_id = getattr(query.from_user, 'id', None) or getattr(query.message, 'chat_id', None)
        key = action_key or getattr(query, 'data', None) or 'callback'
        if user_id and _is_debounced(user_id, key, debounce_interval):
            try:
                await safe_answer(query)
            except Exception:
                pass
            return False

        # Check tokens
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

        # Detect photo vs text messages: photos need edit_caption,
        # text messages need edit_message_text.
        _msg = getattr(query, "message", None)
        if _msg and getattr(_msg, "photo", None):
            await _msg.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await query.edit_message_text(text, reply_markup=reply_markup)
        return True
    except RetryAfter as e:
        wait = int(getattr(e, 'retry_after', 1) or 1)
        logger.warning("Flood control exceeded. scheduling retry in %s seconds", wait)
        await schedule_retry_via_redis_or_local(query, text, reply_markup=reply_markup, delay=wait)
        try:
            await safe_answer(query, text=f"Too many requests. Will retry in {wait}s.")
        except Exception:
            pass
        return False
    except Exception as e:
        # Handle common benign BadRequest cases specially to avoid noisy stacktraces
        msg = str(e)
        if isinstance(e, BadRequest) and ("Message is not modified" in msg or "message is not modified" in msg):
            logger.debug("Edit skipped: message not modified")
            return True
        logger.error("Error editing message: %s", e)
        try:
            _fb_msg = getattr(query, "message", None)
            if _fb_msg and getattr(_fb_msg, "photo", None):
                # Photo message: try edit_caption before giving up
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
    """Safely answer a CallbackQuery, ignoring expired/old-query errors.

    Returns True if answered (or no-op), False if ignored due to being too old.
    """
    try:
        if text is not None:
            await query.answer(text=text)
        else:
            await query.answer()
        return True
    except BadRequest as e:
        m = str(e)
        # Telegram returns BadRequest for expired callback queries — ignore those.
        if "Query is too old" in m or "query id is invalid" in m or "query id is invalid".lower() in m.lower():
            logger.debug("Ignoring expired callback query: %s", m)
            return False
        # Treat other benign BadRequest messages quietly when possible
        if "message is not modified" in m.lower():
            logger.debug("Ignoring 'message is not modified' while answering callback")
            return True
        logger.error("BadRequest when answering callback: %s", e)
        return False
    except Exception as e:
        logger.error("Unexpected error when answering callback: %s", e)
        return False

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
MAX_CATEGORY_NAME_LENGTH = 30  # Maximum allowed length for category names
PAGE_SIZE = 50  # Default number of items per page for pagination


def build_courses_page(all_courses, page: int = 1, origin_type: str = 'global', category: str = None, origin_context: str = None, origin_context_page: int = None, total_count: int = None, is_page: bool = False, store_page_ref: bool = False):
    """Builds the text and InlineKeyboardMarkup for a courses page.

    `all_courses` may be either the full list of items or, when
    `is_page=True`, already the list of items for the requested page.

    Returns (text, InlineKeyboardMarkup) or (None, None) when no items.
    """
    page_size = PAGE_SIZE
    try:
        # If the caller passed in pre-sliced page items, use them directly.
        if is_page:
            display = list(all_courses) if all_courses is not None else []
            effective_total = total_count if total_count is not None else len(display)
            start = (page - 1) * page_size
        else:
            total_len = total_count if total_count is not None else (len(all_courses) if hasattr(all_courses, '__len__') else 0)
            start = (page - 1) * page_size
            display = all_courses[start:start + page_size]
            effective_total = total_len

        if not display:
            return None, None

        logger.debug("build_courses_page called: origin_type=%s category=%s page=%s total=%s", origin_type, category, page, effective_total)

        # Compute total pages once and use it consistently to decide
        # whether to show Next / End buttons. This avoids edge cases
        # where an imprecise `effective_total` caused a Next button to
        # appear on the final page.
        try:
            total_pages = math.ceil(effective_total / page_size) if effective_total is not None else page
        except Exception:
            total_pages = page

        if origin_type == 'category' and category:
            text = f"Courses in category '{category}' (page {page}):"
        else:
            text = f"Here are the available courses (page {page}):"

        keyboard = []
        for c in display:
            try:
                # Determine course's category: prefer explicit field on item,
                # else use the `category` argument passed to this page builder.
                course_cat = c.get('category') if isinstance(c, dict) else None
                if not course_cat:
                    course_cat = category
                name = c.get('name') if isinstance(c, dict) else None
                link = c.get('link') if isinstance(c, dict) else None
                if not name:
                    logger.debug("build_courses_page: skipping course without name: %s", repr(c))
                    continue
                # details callback may omit back token if too long; _make_course_ref handles that
                # Ensure coach-origin pages pass the coach as origin_context so
                # Back returns to the coach's main course list rather than the
                # course's category.
                make_origin_ctx = origin_context
                try:
                    if origin_type == 'coach' and (not make_origin_ctx) and category:
                        # `category` argument contains the coach name when
                        # `origin_type=='coach'` (see callers), so use it as
                        # the origin_context for details refs.
                        make_origin_ctx = category
                except Exception:
                    make_origin_ctx = origin_context
                details_cb = _make_course_ref(course_cat, name, origin_type, page, make_origin_ctx, origin_context_page, c.get('id') if isinstance(c, dict) else None)
                keyboard.append([
                    InlineKeyboardButton(name, url=link),
                    InlineKeyboardButton("ℹ️ Details", callback_data=details_cb)
                ])
            except Exception as e:
                logger.error("build_courses_page: error building row for course %s: %s", repr(c), e)
                continue

    except Exception as e:
        logger.exception("build_courses_page: unexpected error: %s", e)
        return None, None

    # Pagination controls (Previous / Next)
    pagination_buttons = []
    if start > 0:
        if origin_type == 'category' and category:
            if store_page_ref:
                # store compact page payload and link to it. When the full
                # `all_courses` list is available we can populate the target
                # page items; otherwise store only the metadata so the
                # handler can fetch the page server-side on demand.
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, '__len__'):
                        start_prev = (page - 2) * page_size
                        if start_prev >= 0:
                            slice_items = all_courses[start_prev:start_prev + page_size]
                            items_to_store = []
                            for it in slice_items:
                                items_to_store.append({
                                    'name': it.get('name') if isinstance(it, dict) else str(it),
                                    'link': it.get('link') if isinstance(it, dict) else None,
                                    'category': it.get('category') if isinstance(it, dict) else category,
                                    'id': str(it.get('id')) if isinstance(it, dict) and it.get('id') is not None else None,
                                })
                    page_payload = {'type': 'courses_page', 'origin_type': origin_type, 'category': category, 'page': page-1, 'origin_context': origin_context, 'origin_context_page': origin_context_page, 'total_count': effective_total, 'page_size': page_size}
                    if items_to_store is not None:
                        page_payload['items'] = items_to_store
                    key = _store_callback_payload(page_payload)
                    prev_cb = f"courses_ref::{key}"
                except Exception:
                    prev_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page-1}"
                    if origin_context:
                        prev_cb = prev_cb + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
            else:
                prev_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page-1}"
                # preserve origin context and origin page so Prev/Next keep the
                # same parent pagination when navigating between course pages
                if origin_context:
                    prev_cb = prev_cb + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
        elif origin_type == 'coach' and category:
            prev_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page-1}"
        else:
            prev_cb = f"courses::global::{page-1}"
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=prev_cb))
    if page < total_pages:
        if origin_type == 'category' and category:
            if store_page_ref:
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, '__len__'):
                        start_next = page * page_size
                        slice_items = all_courses[start_next:start_next + page_size]
                        items_to_store = []
                        for it in slice_items:
                            items_to_store.append({
                                'name': it.get('name') if isinstance(it, dict) else str(it),
                                'link': it.get('link') if isinstance(it, dict) else None,
                                'category': it.get('category') if isinstance(it, dict) else category,
                                'id': str(it.get('id')) if isinstance(it, dict) and it.get('id') is not None else None,
                            })
                    page_payload = {'type': 'courses_page', 'origin_type': origin_type, 'category': category, 'page': page+1, 'origin_context': origin_context, 'origin_context_page': origin_context_page, 'total_count': effective_total, 'page_size': page_size}
                    if items_to_store is not None:
                        page_payload['items'] = items_to_store
                    key = _store_callback_payload(page_payload)
                    next_cb = f"courses_ref::{key}"
                except Exception:
                    next_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page+1}"
                    if origin_context:
                        next_cb = next_cb + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
            else:
                next_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page+1}"
                if origin_context:
                    next_cb = next_cb + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
        elif origin_type == 'coach' and category:
            if store_page_ref:
                try:
                    items_to_store = None
                    if not is_page and hasattr(all_courses, '__len__'):
                        start_next = page * page_size
                        slice_items = all_courses[start_next:start_next + page_size]
                        items_to_store = []
                        for it in slice_items:
                            items_to_store.append({
                                'name': it.get('name') if isinstance(it, dict) else str(it),
                                'link': it.get('link') if isinstance(it, dict) else None,
                                'category': it.get('category') if isinstance(it, dict) else category,
                                'id': str(it.get('id')) if isinstance(it, dict) and it.get('id') is not None else None,
                            })
                    page_payload = {'type': 'courses_page', 'origin_type': origin_type, 'category': category, 'page': page+1, 'origin_context': origin_context, 'origin_context_page': origin_context_page, 'total_count': effective_total, 'page_size': page_size}
                    if items_to_store is not None:
                        page_payload['items'] = items_to_store
                    key = _store_callback_payload(page_payload)
                    next_cb = f"courses_ref::{key}"
                except Exception:
                    next_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page+1}"
            else:
                next_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page+1}"
        else:
            next_cb = f"courses::global::{page+1}"
        pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=next_cb))
    if pagination_buttons:
        keyboard.append(pagination_buttons)

    # Defensive: ensure we don't exceed Telegram's inline keyboard button limits.
    # Telegram limits ~100 buttons per message; be conservative and cap at 90.
    try:
        total_buttons = sum(len(r) for r in keyboard)
    except Exception:
        total_buttons = 0
    MAX_BUTTONS = 90
    if total_buttons > MAX_BUTTONS:
        # Reduce the number of course rows shown to fit within MAX_BUTTONS.
        # Each course row typically has 2 buttons; reserve some slots for nav/breadcrumb.
        reserved = 6
        max_course_buttons = max(1, MAX_BUTTONS - reserved)
        max_course_rows = max_course_buttons // 2
        # Recompute display to the smaller size and rebuild keyboard
        display = display[:max_course_rows]
        keyboard = []
        for c in display:
            try:
                course_cat = c.get('category') if isinstance(c, dict) else None
                if not course_cat:
                    course_cat = category
                name = c.get('name') if isinstance(c, dict) else None
                link = c.get('link') if isinstance(c, dict) else None
                if not name:
                    continue
                details_cb = _make_course_ref(course_cat, name, origin_type, page, origin_context, origin_context_page, c.get('id') if isinstance(c, dict) else None)
                keyboard.append([
                    InlineKeyboardButton(name, url=link),
                    InlineKeyboardButton("ℹ️ Details", callback_data=details_cb)
                ])
            except Exception:
                continue
        # Re-add a minimal pagination row if necessary
        pagination_buttons = []
        if start > 0:
            prev_cb = f"courses::global::{page-1}" if origin_type == 'global' else prev_cb
            pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=prev_cb))
        if page < total_pages:
            next_cb = f"courses::global::{page+1}" if origin_type == 'global' else next_cb
            pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=next_cb))
        if pagination_buttons:
            keyboard.append(pagination_buttons)

    # Compute total pages for End button placement
    try:
        total_pages = math.ceil(effective_total / page_size) if effective_total is not None else page
    except Exception:
        total_pages = page

    # Prepare breadcrumb/home row: always show Home; when there are
    # multiple pages and we're not on the last page, show an End button
    # beside Home. If `category` is provided, include it as a breadcrumb
    # button as well for context.
    try:
        # Choose Home callback depending on origin: global pages go to
        # explicit `courses::global::<page>` callbacks; category pages
        # use `courses::category::<category>::<page>` so the handler can
        # unambiguously route the request.
        breadcrumb_buttons = None
        # Decide Home callback and visibility based on origin
        if origin_type == 'global':
            # Show Home for global listing only when not on the first page
            if page > 1:
                breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data=f"courses::global::1")]
            else:
                breadcrumb_buttons = None
        elif origin_type == 'coach' and category:
            # For coach-origin pages, Home should return to the coach's
            # course list page 1. Hide the Home button when already on page 1.
            if page > 1:
                breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data=f"courses::coach::{urllib.parse.quote_plus(category)}::1")]
            else:
                breadcrumb_buttons = None
        elif origin_type == 'category' and category:
            # For category-origin pages, Home should return to the category's
            # course list page 1 (keep user inside the same category). Hide
            # the Home button when already on page 1.
            if page > 1:
                breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data=f"courses::category::{urllib.parse.quote_plus(category)}::1")]
            else:
                breadcrumb_buttons = None
        else:
            # Default Home goes to the top-level categories view
            breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data="back_to_cats")]

        # If there are multiple pages and we're not on the last page, show an End button.
        if total_pages > 1 and page < total_pages:
            # build end callback depending on origin type
            if origin_type == 'category' and category:
                if store_page_ref:
                    try:
                        items_to_store = None
                        # compute items for final page only when full list is available
                        if not is_page and hasattr(all_courses, '__len__'):
                            start_end = (total_pages - 1) * page_size
                            slice_items = all_courses[start_end:start_end + page_size]
                            items_to_store = []
                            for it in slice_items:
                                items_to_store.append({
                                    'name': it.get('name') if isinstance(it, dict) else str(it),
                                    'link': it.get('link') if isinstance(it, dict) else None,
                                    'category': it.get('category') if isinstance(it, dict) else category,
                                    'id': str(it.get('id')) if isinstance(it, dict) and it.get('id') is not None else None,
                                })
                        page_payload = {'type': 'courses_page', 'origin_type': origin_type, 'category': category, 'page': total_pages, 'origin_context': origin_context, 'origin_context_page': origin_context_page, 'total_count': effective_total, 'page_size': page_size}
                        if items_to_store is not None:
                            page_payload['items'] = items_to_store
                        key = _store_callback_payload(page_payload)
                        end_cb = f"courses_ref::{key}"
                    except Exception:
                        end_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{total_pages}"
                        if origin_context:
                            end_cb = end_cb + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
                else:
                    end_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{total_pages}"
                    if origin_context:
                        end_cb = end_cb + f"::from_parent::{urllib.parse.quote_plus(str(origin_context))}::{origin_context_page or 1}"
            elif origin_type == 'global':
                end_cb = f"courses::global::{total_pages}"
            elif origin_type == 'coach' and category:
                if store_page_ref:
                    try:
                        items_to_store = None
                        if not is_page and hasattr(all_courses, '__len__'):
                            start_end = (total_pages - 1) * page_size
                            slice_items = all_courses[start_end:start_end + page_size]
                            items_to_store = []
                            for it in slice_items:
                                items_to_store.append({
                                    'name': it.get('name') if isinstance(it, dict) else str(it),
                                    'link': it.get('link') if isinstance(it, dict) else None,
                                    'category': it.get('category') if isinstance(it, dict) else category,
                                    'id': str(it.get('id')) if isinstance(it, dict) and it.get('id') is not None else None,
                                })
                        page_payload = {'type': 'courses_page', 'origin_type': origin_type, 'category': category, 'page': total_pages, 'origin_context': origin_context, 'origin_context_page': origin_context_page, 'total_count': effective_total, 'page_size': page_size}
                        if items_to_store is not None:
                            page_payload['items'] = items_to_store
                        key = _store_callback_payload(page_payload)
                        end_cb = f"courses_ref::{key}"
                    except Exception:
                        end_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{total_pages}"
                else:
                    end_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{total_pages}"
            else:
                end_cb = f"courses::global::{total_pages}"
            breadcrumb_buttons.append(InlineKeyboardButton("⏭️ End", callback_data=end_cb))

        # insert breadcrumb row (Home +/- End) when present
        if breadcrumb_buttons:
            keyboard.insert(0, breadcrumb_buttons)

        # prepend breadcrumb text for visual context when available (condensed)
        try:
            bc = " / ".join(breadcrumb)
            text = f"{bc}\n{text}"
        except Exception:
            pass
    except Exception:
        pass

    # (Breadcrumb row inserted above with Home/End when applicable)

    # Ensure a clear Back button for category-origin pages so users can
    # return to the categories listing (consistent with other views).
    try:
        if origin_type == 'category':
            # For category-origin pages: Home -> top-level categories,
            # Back -> return to the parent/topic view (origin_context if provided).
            if not any((getattr(b, 'text', '') == '🔙 Back') for row in keyboard for b in row):
                # prefer origin_context (parent path) when available
                target = origin_context or category
                if target:
                    # If origin_context points to the categories listing (special sentinel),
                    # route back to the correct categories page instead of a showcat.
                    if origin_context == 'categories':
                        back_cb = f"categories_page::{origin_context_page or 1}"
                    elif origin_context == 'back_to_cats':
                        back_cb = "back_to_cats"
                    else:
                        # Preserve the parent page when returning to the target
                        back_page = origin_context_page if origin_context_page is not None else page
                        back_cb = _shorten_showcat_cb(str(target), back_page)
                else:
                    back_cb = "back_to_cats"
                logger.debug("build_courses_page: back_cb=%s origin_context=%s origin_context_page=%s origin_type=%s category=%s page=%s", back_cb, origin_context, origin_context_page, origin_type, category, page)
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])
    except Exception:
        pass

    return text, InlineKeyboardMarkup(keyboard)

# Input Validation for Category Name
def validate_category_name(category_name: str):
    """Validates the category name."""
    if not category_name or category_name.isspace():
        return "The category name cannot be empty. Please try again! 😬"
    
    if len(category_name) < 3 or len(category_name) > MAX_CATEGORY_NAME_LENGTH:
        return f"Category name must be between 3 and {MAX_CATEGORY_NAME_LENGTH} characters."
    
    # Allow most printable characters; only reject control characters / newlines
    if any(c in category_name for c in "\r\n"):
        return "Category name cannot contain newlines or control characters."
    
    return None
    
async def help_command(update: Update, context: CallbackContext):
    """Display the help message with available commands."""
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
    """Show every category as an inline button that opens its courses."""
    # Show paginated top-level categories (page 1)
    try:
        await categories_page(update.message, context, page=1)
    except Exception as e:
        logger.error(f"Error listing categories: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")


async def createcat_page(update_or_message, context: CallbackContext, *, page: int = 1):
    """Paginated top-level categories view for the `/create_category` flow.

    Buttons use `createcat_parent::{name}` callback_data so the
    existing `handle_create_category_parent` handler can be reused.
    Accepts either a `Message` (initial call) or a `CallbackQuery`
    (callback_data: `createcat_page::{page}`).
    """
    query = getattr(update_or_message, 'callback_query', None)
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

    # Store the page for backtracing after category creation

    try:

        context.user_data['createcat_last_page'] = page

    except Exception:

        pass

    logger.debug("createcat_page invoked: page=%s is_query=%s", page, is_query)
    try:
        db = await get_db()
        logger.debug("createcat_page: db=%s", getattr(db, 'name', None))
        if db is None:
            if is_query:
                await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
            else:
                await update_or_message.reply_text("Error: Unable to connect to the database.")
            return

        # Use server-side pagination: cached count + sort + skip/limit for top-level categories
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        async with _db_timing(f"createcat_page:{page}"):
            total = await _get_total_count(db, 'categories', TOP_LEVEL_FILTER, ttl=30)
            cats = await db.categories.find(TOP_LEVEL_FILTER).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
    except Exception as e:
        logger.exception("createcat_page: exception while fetching categories")
        cats = []
        total = 0

    # cats already contains only the current page slice (server-side)
    page_cats = cats

    if not page_cats and not is_query:
        await update_or_message.reply_text("No categories available. Use /create_category to create one.")
        return

    keyboard = []
    # Provide explicit Top-level option
    keyboard.append([InlineKeyboardButton("(Top-level)", callback_data=f"createcat_parent::")])
    for cat in page_cats:
        keyboard.append([InlineKeyboardButton(cat.get('name'), callback_data=f"createcat_parent::{urllib.parse.quote_plus(cat.get('name'))}")])
    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    # Prev (left)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"createcat_page::{page-1}"))
    # Next (right)
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"createcat_page::{page+1}"))
    # End (only show when there are more pages beyond the current one)
    if total_pages > 1 and page < last_page:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"createcat_page::{last_page}"))

    if nav:
        keyboard.append(nav)

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = f"Select a parent category (page {page}/{last_page}):"
    if is_query:
        await safe_edit_message(query, title, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    else:
        await update_or_message.reply_text(title, reply_markup=reply_markup)
    return


async def children_page(update_or_message, context: CallbackContext, parent: str, *, page: int = 1):
    """Paginated child categories view for a given `parent`.

    Shows child categories of `parent` with `showcat::` callbacks so the
    user can inspect the newly created child. Accepts Message or CallbackQuery.
    """
    query = getattr(update_or_message, 'callback_query', None)
    is_query = query is not None
    if is_query:
        await safe_answer(query)
        data = query.data
        parts = data.split("::")
        # support showcat::<path>::<page> format — ignored here, page parsed below
        if len(parts) > 2:
            try:
                page = int(parts[-1])
            except Exception:
                page = page

    try:
        db = await get_db()
        if db is None:
            if is_query:
                await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
            else:
                await update_or_message.reply_text("Error: Unable to connect to the database.")
            return

        # Use server-side pagination for children; fetch only this page slice
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        async with _db_timing(f"children_page:{parent}:{page}"):
            total_children = await _get_total_count(db, 'categories', {"parent": parent}, ttl=30)
            children = await db.categories.find({"parent": parent}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
    except Exception:
        children = []
        total_children = 0

    # children already contains only the current page slice (server-side)
    sorted_children = sorted(children, key=lambda c: (c.get('name') or '').lower())
    page_size = PAGE_SIZE
    page_children = sorted_children

    if not page_children:
        if is_query:
            await safe_edit_message(query, "No subcategories available on this page.", action_key=getattr(query, 'data', None))
        else:
            await update_or_message.reply_text("No subcategories available.")
        return

    # When linking into a child category, store a short callback payload so
    # the callback_data remains under Telegram's 64-byte limit. Payload
    # includes child path and parent page info so the child can return to
    # the same parent page via its Up/Back buttons.
    # Batch-check which of the page children have their own children to
    # avoid N queries. This mirrors the optimization used in
    # `categories_page` above.
    child_names = [c.get('name') for c in page_children if c.get('name')]
    names_with_children = set()
    if child_names:
        try:
            names_with_children = await _get_children_flags(db, child_names, ttl=60)
        except Exception:
            names_with_children = set()

    keyboard = []
    # Capture a compact parent index (paths or names) to persist with
    # child callbacks. This hidden tracker lets us reconstruct the
    # parent's full index quickly on Back/Forward without extra DB hits.
    try:
        parent_index = [
            {"path": (c.get('path') if c.get('path') is not None else None), "name": (c.get('name') if c.get('name') is not None else None)}
            for c in children if isinstance(c, dict) and (c.get('path') or c.get('name'))
        ]
    except Exception:
        parent_index = []

    for child in page_children:
        child_path = child.get('path') or child.get('name')
        payload = {"type": "showcat", "path": child_path, "from_parent": parent, "parent_page": page, "parent_index": parent_index}
        key = _store_callback_payload(payload)
        # Prefetch the child's first courses page in background to
        # improve perceived responsiveness when users open it.
        try:
            if _redis is not None:
                asyncio.create_task(_prefetch_category_page(child_path, page=1))
        except Exception:
            pass
        try:
            has_children = child.get('name') in names_with_children
            courses = child.get('courses', []) if isinstance(child, dict) else []
            is_empty = (not has_children) and (not _has_real_courses(courses))
        except Exception:
            is_empty = True
        display = f"{child.get('name')}{' (empty)' if is_empty else ''}"
        keyboard.append([InlineKeyboardButton(display, callback_data=f"showcat_ref::{key}")])

    nav = []
    total_pages = (len(children) - 1) // page_size + 1 if children else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=_shorten_showcat_cb(parent, page-1)))
        # Only show Home when not already on the first page
        nav.append(InlineKeyboardButton("🏠 Home", callback_data=_shorten_showcat_cb(parent, 1)))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=_shorten_showcat_cb(parent, page+1)))
    if total_pages > 1 and page < last_page:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=_shorten_showcat_cb(parent, last_page)))
    if nav:
        keyboard.append(nav)

    # Up button to parent view
    pdoc = await db.categories.find_one({"name": parent})
    ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
    keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=_shorten_showcat_cb(ppath, page))])
    # Add Search button for child categories/coaches
    try:
        keyboard.append([InlineKeyboardButton("🔍 Search", callback_data=f"search_categories::{page}")])
    except Exception:
        pass

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = f"Subcategories of '{parent}' (page {page}/{last_page}):"
    if is_query:
        await safe_edit_message(query, title, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    else:
        await update_or_message.reply_text(title, reply_markup=reply_markup)
    return


async def categories_page(update_or_message, context: CallbackContext, *, page: int = 1):
    """Paginated top-level categories view.

    Accepts either a `Message` (from the `/categories` command) or a
    `CallbackQuery` (callback_data: `categories_page::{page}`).
    """
    query = getattr(update_or_message, 'callback_query', None)
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
                await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
            else:
                await update_or_message.reply_text("Error: Unable to connect to the database.")
            return

        # Use server-side pagination for categories listing. Fetch only
        # the minimal fields (name/path) and defer any child/course
        # checks until the user actually opens a parent (AJAX-style).
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        async with _db_timing(f"categories_page:{page}"):
            filter_q = TOP_LEVEL_FILTER
            total = await _get_total_count(db, 'categories', filter_q, ttl=30)
            proj = {"name": 1, "path": 1, "parent": 1, "_id": 1}
            raw = await db.categories.find(filter_q, proj).sort("name", 1).skip(start).limit(page_size + 1).to_list(length=page_size + 1)
        have_more = len(raw) > page_size
        cats = raw[:page_size] if have_more else raw
        logger.debug("categories_page: requested page=%s total_est=%s got=%s have_more=%s", page, total, len(cats), have_more)
        # Fallback: some data models store top-level categories with parent==None
        # or an empty string. If we got no results, try relaxed filter once.
        page_cats = cats
        if not page_cats and total == 0:
            try:
                alt_filter = TOP_LEVEL_FILTER
                async with _db_timing(f"categories_page:fallback:{page}"):
                    alt_total = await _get_total_count(db, 'categories', alt_filter, ttl=30)
                    alt_raw = await db.categories.find(alt_filter, proj).sort("name", 1).skip(start).limit(page_size + 1).to_list(length=page_size + 1)
                alt_have_more = len(alt_raw) > page_size
                alt_cats = alt_raw[:page_size] if alt_have_more else alt_raw
                logger.debug("categories_page: fallback total_est=%s got=%s", alt_total, len(alt_cats))
                if alt_cats:
                    page_cats = alt_cats
            except Exception:
                page_cats = []

        # Batch-check which of the page categories have children to avoid N queries.
        cat_names = [c.get('name') for c in page_cats if isinstance(c, dict) and c.get('name')]
        names_with_children = set()
        if cat_names:
            try:
                docs = await db.categories.find({"parent": {"$in": cat_names}}, {"parent": 1}).to_list(length=len(cat_names))
                names_with_children = {d.get('parent') for d in docs if d.get('parent')}
            except Exception:
                names_with_children = set()

        keyboard = []
        for cat in page_cats:
            cat_path = cat.get('path') or cat.get('name')
            # Persist a short ref including the current categories page so
            # returning from the category view restores the same page.
            payload = {"type": "showcat", "path": cat_path, "from_parent": "categories", "parent_page": page}
            key = _store_callback_payload(payload)
            try:
                logger.debug("categories_page: created showcat_ref key=%s path=%s parent_page=%s", key, cat_path, page)
            except Exception:
                pass

            # For top-level categories we deliberately do not append an '(empty)'
            # suffix — child/course checks are deferred until the user opens
            # the category (AJAX-style).
            display_name = cat.get('name') if isinstance(cat, dict) else str(cat)
            cb = f"showcat_ref::{key}"
            try:
                logger.debug("categories_page: button name=%r path=%r", display_name, cat_path)
            except Exception:
                pass

            # Prefetch the child's first courses page in background to
            # improve perceived responsiveness when users open it.
            try:
                if _redis is not None:
                    asyncio.create_task(_prefetch_category_page(cat_path, page=1))
            except Exception:
                pass

            keyboard.append([InlineKeyboardButton(display_name, callback_data=cb)])

    except Exception:
        try:
            logger.exception("categories_page: unexpected error building page")
        except Exception:
            pass
        if is_query:
            await safe_edit_message(query, "Error: failed to load categories.", action_key=getattr(query, 'data', None))
        else:
            await update_or_message.reply_text("Error: failed to load categories.")
        return

    nav = []
    total_pages = (total - 1) // page_size + 1 if total else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"categories_page::{page-1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"categories_page::{page+1}"))
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"categories_page::{last_page}"))
    if nav:
        keyboard.append(nav)

    # Breadcrumb / Home row: show Home only when not already on page 1
    try:
        if page > 1:
            breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data=f"categories_page::1")]
            keyboard.insert(0, breadcrumb_buttons)
    except Exception:
        pass

        # Add Search button for categories
    keyboard.append([InlineKeyboardButton("🔍 Search", callback_data=f"search_categories::{page}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = f"Tap a category to see its courses (page {page}/{last_page}):"
    if is_query:
        await safe_edit_message(query, title, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    else:
        msg = await update_or_message.reply_text(title, reply_markup=reply_markup)
        try:
            schedule_close_inline_message(msg)
        except Exception:
            pass
    return


async def debug_db(update: Update, context: CallbackContext):
    """Owner-only debug command returning basic DB diagnostics."""
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        if owner_env is None:
            await update.message.reply_text("Debug not allowed: BOT_OWNER_ID not configured.")
            return
        try:
            owner_id = int(owner_env)
        except Exception:
            owner_id = None
        user_id = getattr(update.effective_user, 'id', None)
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
        cat_count = await _get_total_count(db, 'categories', {}, ttl=60)
    except Exception as e:
        cat_count = f"error: {e}"

    sample = None
    try:
        sample = await db.categories.find_one({}, projection={'name': 1, 'parent': 1, 'path': 1, 'courses': 1})
    except Exception as e:
        sample = f"error: {e}"

    try:
        indexes = await db.categories.index_information()
    except Exception as e:
        indexes = f"error: {e}"

    msg = f"categories_count: {cat_count}\nindexes: {list(indexes.keys()) if isinstance(indexes, dict) else indexes}\nsample: {sample}"
    # Trim if too long
    if len(msg) > 4000:
        msg = msg[:3990] + '...'
    await update.message.reply_text(msg)


async def show_coach_handler(update: Update, context: CallbackContext):
    """Show courses for a selected coach. Supports coaches stored in a `coaches` collection or derived from categories/courses."""
    query = update.callback_query
    await safe_answer(query)
    raw = getattr(query, 'data', '') or ''
    # Support multiple callback formats: 'coach_<slug>' (legacy) and
    # 'coach::<slug>::<page>' (explicit). Default to page=1.
    page = 1
    page_size = PAGE_SIZE
    coach_slug = None
    try:
        if raw.startswith('coach::'):
            parts = raw.split('::')
            if len(parts) >= 2:
                coach_slug = urllib.parse.unquote_plus(parts[1])
            if len(parts) >= 3:
                try:
                    page = int(parts[2])
                except Exception:
                    page = 1
        elif raw.startswith('coach_'):
            # legacy underscore format: coach_<slug>
            try:
                coach_slug = urllib.parse.unquote_plus(raw.split('_', 1)[1])
            except Exception:
                coach_slug = raw
        else:
            # Fallback: treat entire payload as slug
            coach_slug = urllib.parse.unquote_plus(raw)
    except Exception:
        coach_slug = urllib.parse.unquote_plus(raw)

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return

    # Try to find coach by slug in dedicated collection
    coach_name = None
    try:
        if hasattr(db, 'coaches'):
            coach_doc = await db.coaches.find_one({'slug': coach_slug})
            if coach_doc:
                coach_name = coach_doc.get('name')
    except Exception:
        coach_name = None

    # Fallback: treat slug as a name
    if not coach_name:
        coach_name = urllib.parse.unquote_plus(coach_slug)

    # Collect all courses for this coach: prefer explicit 'coach' field, else category name match
    try:
        # Query only categories that could contain this coach's courses or
        # whose name matches the coach (legacy modeling). This avoids
        # fetching the entire collection into memory.
        filter_q = {"$or": [{"courses.coach": coach_name}, {"name": coach_name}]}
        # Use aggregation to unwind courses and project only the fields we need
        # This avoids transferring entire category documents when only course
        # metadata is required for coach views.
        # Paginated aggregation: count total matching coach courses, then
        # fetch only the requested page of items server-side.
        start = (page - 1) * page_size
        items_pipeline = [
            {"$match": filter_q},
            {"$unwind": "$courses"},
            {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name", "coach": "$courses.coach"}},
            {"$match": {"$or": [{"coach": coach_name}, {"category": coach_name}] }},
            {"$sort": {"name": 1}},
            {"$skip": start},
            {"$limit": page_size + 1}
        ]
        async with _db_timing(f"show_coach:{coach_name}:{page}"):
            # Try to serve a cached page for this coach first (short TTL)
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
                # cache the raw items briefly to improve UX on rapid nav
                try:
                    _set_cached_page(cache_key, items, ttl=3)
                except Exception:
                    pass
        has_more = len(items) > page_size
        coach_courses = items[:page_size]
        coach_courses = sorted(coach_courses, key=lambda c: (c.get('name') or '').lower())
        # Compute accurate total for coach by aggregating on the server when possible
        try:
            cnt_doc = await db.categories.aggregate([
                {"$match": {"courses.coach": coach_name}},
                {"$unwind": "$courses"},
                {"$match": {"courses.coach": coach_name}},
                {"$group": {"_id": None, "count": {"$sum": 1}}}
            ]).to_list(length=1)
            total_courses = int(cnt_doc[0].get('count')) if cnt_doc else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(coach_courses)))
        except Exception:
            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(coach_courses))

        text, reply_markup = build_courses_page(coach_courses, page=page, origin_type='coach', category=coach_name, origin_context=None, total_count=total_courses, is_page=True, store_page_ref=True)
        # Add Search button for this coach's courses
        try:
            kb = list(reply_markup.inline_keyboard)
            kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_courses::coach::{urllib.parse.quote_plus(str(coach_name))}::{page}")])
            reply_markup = InlineKeyboardMarkup(kb)
        except Exception:
            pass
        if text and reply_markup:
            await safe_edit_message(query, text, reply_markup=reply_markup, action_key=raw)
        else:
            await safe_edit_message(query, f"No courses found for coach '{coach_name}'.", action_key=raw)
    except Exception as e:
        logger.error("Error showing coach courses: %s", e)
        await safe_edit_message(query, "An unexpected error occurred. Please try again later.", action_key=getattr(query, 'data', None))


async def show_coach_in_category(update: Update, context: CallbackContext):
    """Handle coach selection within a specific category: coach_in_cat::{category}::{coach_slug}"""
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    # Support short stored refs for coach_in_cat (coach_in_cat_ref::<key>)
    parent_origin = None
    parent_origin_page = None
    if data.startswith("coach_in_cat_ref::"):
        key = data.split("::", 1)[1]
        payload = await _resolve_callback_payload(key)
        if not payload:
            _clear_design_pending(context)
            await safe_edit_message(query, "Reference expired. Please open the list again.", action_key=getattr(query, 'data', None))
            return
        # payload contains category, coach_slug, page and optionally from_parent/parent_page
        category = payload.get('category')
        coach_slug = payload.get('coach_slug')
        page = int(payload.get('page', 1) or 1)
        parent_origin = payload.get('from_parent')
        try:
            parent_origin_page = int(payload.get('parent_page')) if payload.get('parent_page') is not None else None
        except Exception:
            parent_origin_page = None
    else:
        parts = data.split("::")
    # Accept either: coach_in_cat::{category}::{coach_slug}
    # Or: coach_in_cat::{category}::{coach_slug}::{type_slug}
    if len(parts) < 3:
        await safe_edit_message(query, "Invalid coach callback.", action_key=getattr(query, 'data', None))
        return
    # parts[0] == 'coach_in_cat'
    category = urllib.parse.unquote_plus(parts[1])
    coach_slug = urllib.parse.unquote_plus(parts[2])
    # Support optional forms:
    #  - coach_in_cat::{category}::{coach_slug}
    #  - coach_in_cat::{category}::{coach_slug}::{type_slug}
    #  - coach_in_cat::{category}::{coach_slug}::{page}
    #  - coach_in_cat::{category}::{coach_slug}::{type_slug}::{page}
    type_slug = None
    page = 1
    if len(parts) >= 4:
        maybe = parts[3]
        # if numeric, treat as page
        try:
            page = int(maybe)
        except Exception:
            type_slug = urllib.parse.unquote_plus(maybe)
    if len(parts) >= 5:
        # treat parts[4] as page if present
        try:
            page = int(parts[4])
        except Exception:
            pass

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return

    # Resolve coach name: try coaches collection then fallback to slug-as-name
    coach_name = None
    try:
        if hasattr(db, 'coaches'):
            coach_doc = await db.coaches.find_one({'slug': coach_slug})
            if coach_doc:
                coach_name = coach_doc.get('name')
    except Exception:
        coach_name = None
    if not coach_name:
        coach_name = coach_slug

    try:
        # First, check if the coach is modeled as a child category under this category
        coach_child = await db.categories.find_one({'name': coach_name, 'parent': category})
        coach_courses = []
        if coach_child:
            # use child's embedded courses
            for crs in coach_child.get('courses', []):
                coach_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": coach_name})
            # Remember that the user is viewing this coach child category so
            # `/add` can automatically target this exact location (child category).
            try:
                # prefer storing the canonical path when available
                last_view = coach_child.get('path') or coach_child.get('name')
                context.user_data['last_viewed_category'] = last_view
                # also store explicit ids to allow id-based resolution later
                try:
                    if coach_child.get('id'):
                        context.user_data['last_viewed_category_id'] = coach_child.get('id')
                except Exception:
                    pass
                # store parent for quick resolution
                try:
                    if coach_child.get('parent'):
                        context.user_data['last_viewed_category_parent'] = coach_child.get('parent')
                except Exception:
                    pass
            except Exception:
                pass
        else:
            # Fallback: look for courses in the parent category that have a 'coach' field
            category_doc = await db.categories.find_one({'name': category})
            if not category_doc or not category_doc.get('courses'):
                await safe_edit_message(query, f"No courses found in category '{category}'.", action_key=getattr(query, 'data', None))
                return

            for crs in category_doc.get('courses', []):
                # If a type filter was provided, only include courses matching that type
                if type_slug:
                    c_type = crs.get('type') or crs.get('category_type') or crs.get('categoryType')
                    if not c_type:
                        continue
                    if urllib.parse.quote_plus(str(c_type)) != type_slug:
                        continue
                if crs.get('coach'):
                    if crs.get('coach') == coach_name:
                        coach_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": category})

        coach_courses = sorted(coach_courses, key=lambda c: (c.get('name') or '').lower())
        # Determine parent path so Home can return to the parent directory
        origin_ctx = None
        try:
            cdoc = await db.categories.find_one({"name": category})
            if cdoc:
                parent = cdoc.get('parent')
                if parent:
                    pdoc = await db.categories.find_one({"name": parent})
                    origin_ctx = pdoc.get('path') if pdoc and pdoc.get('path') else parent
        except Exception:
            origin_ctx = None

        # If this coach/category view originated from a parent (e.g., the
        # paginated categories listing), prefer using that parent_origin so
        # Back returns to the correct parent page.
        if parent_origin:
            origin_ctx = parent_origin
        # Ensure coach lists are fully paginated server-side: compute total
        # and slice the requested page, then pass `is_page=True` with
        # `total_count` so `build_courses_page` doesn't re-slice incorrectly.
        total_courses = len(coach_courses)
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        page_items = coach_courses[start:start + page_size]
        text, reply_markup = build_courses_page(page_items, page=page, origin_type='coach', category=coach_name, origin_context=origin_ctx, origin_context_page=parent_origin_page, total_count=total_courses, is_page=True, store_page_ref=True)
        # Add Search button for courses in this category
        try:
            kb = list(reply_markup.inline_keyboard)
            kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}")])
            reply_markup = InlineKeyboardMarkup(kb)
        except Exception:
            pass
        if not text:
            await safe_edit_message(query, f"No courses found for coach '{coach_name}' in '{category}'.", action_key=getattr(query, 'data', None))
            return
        try:
            _set_session_keep_open(query.message, True)
        except Exception:
            pass
        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    except Exception as e:
        logger.error("Error fetching coach courses in category: %s", e)
        await safe_edit_message(query, "An unexpected error occurred. Please try again later.", action_key=getattr(query, 'data', None))

async def showtype_handler(update: Update, context: CallbackContext):
    """
    Handle type selection inside a category.
    Expected callback format:
    showtype::{category}::{type_name}
    """
    query = update.callback_query
    await safe_answer(query)

    data = query.data.split("::")
    if len(data) < 3:
        await safe_edit_message(
            query,
            "Invalid type callback.",
            action_key=getattr(query, 'data', None)
        )
        return

    _, encoded_category, encoded_type = data[:3]
    category_name = urllib.parse.unquote_plus(encoded_category)
    type_name = urllib.parse.unquote_plus(encoded_type)

    db = await get_db()
    if db is None:
        await safe_edit_message(
            query,
            "Error: Unable to connect to the database.",
            action_key=getattr(query, 'data', None)
        )
        return

    try:
        category_doc = await db.categories.find_one({"name": category_name})
        if not category_doc or not category_doc.get("courses"):
            await safe_edit_message(
                query,
                f"No courses found in category '{category_name}'.",
                action_key=getattr(query, 'data', None)
            )
            return

        filtered_courses = []
        for crs in category_doc.get("courses", []):
            c_type = (
                crs.get("type")
                or crs.get("category_type")
                or crs.get("categoryType")
            )
            if c_type and str(c_type) == type_name:
                filtered_courses.append({
                    "name": crs.get("name"),
                    "link": crs.get("link"),
                    "category": category_name,
                })

        # Sort case-insensitive
        filtered_courses = sorted(
            filtered_courses,
            key=lambda c: (c.get("name") or "").lower()
        )

        # Determine parent so Home can return to parent directory
        origin_ctx = None
        try:
            cdoc = await db.categories.find_one({"name": category_name})
            if cdoc:
                parent = cdoc.get('parent')
                if parent:
                    pdoc = await db.categories.find_one({"name": parent})
                    origin_ctx = pdoc.get('path') if pdoc and pdoc.get('path') else parent
        except Exception:
            origin_ctx = None

        text, reply_markup = build_courses_page(
            filtered_courses,
            page=1,
            origin_type="category",
            category=category_name,
            origin_context=origin_ctx,
        )
        # Add Search button for courses in this category
        try:
            kb = list(reply_markup.inline_keyboard)
            kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category_name))}::1")])
            reply_markup = InlineKeyboardMarkup(kb)
        except Exception:
            pass

        if not text:
            await safe_edit_message(
                query,
                f"No courses found for type '{type_name}' in '{category_name}'.",
                action_key=getattr(query, 'data', None)
            )
            return

        # Keep this session open while browsing coach lists to avoid
        # scheduled session closure interfering with navigation.
        try:
            _set_session_keep_open(query.message, True)
        except Exception:
            pass
        await safe_edit_message(
            query,
            text=text,
            reply_markup=reply_markup,
            action_key=getattr(query, 'data', None)
        )

    except Exception as e:
        logger.error("Error showing type courses: %s", e)
        await safe_edit_message(
            query,
            "An unexpected error occurred. Please try again later.",
            action_key=getattr(query, 'data', None)
        )
        
def _clear_design_pending(context):
    """Pop and discard any pending category design from user_data."""
    try:
        context.user_data.pop('_pending_design', None)
        context.user_data.pop('_pending_design_key', None)
    except Exception:
        pass


async def _send_design_photo(query, context, text, reply_markup):
    """If a category design is pending in user_data, pop it and send the
design photo with text as caption and the inline keyboard; otherwise
fall back to safe_edit_message.

Includes the same debounce and token-bucket rate limiting that
safe_edit_message uses to avoid Telegram flood-control errors."""
    try:
        pending_design = context.user_data.pop('_pending_design', None)
        
        if pending_design and query.message:
            # --- Rate limiting (matches safe_edit_message) ---
            try:
                user_id = getattr(query.from_user, 'id', None) or getattr(query.message, 'chat_id', None)
                key = getattr(query, 'data', None) or 'send_photo'
                if user_id and _is_debounced(user_id, key):
                    # Restore design info so a later retry can pick it up
                    context.user_data['_pending_design'] = pending_design
                    try:
                        await safe_answer(query)
                    except Exception:
                        pass
                    return

                uid = user_id or 0
                ok, wait = await _consume_token(uid)
                if not ok:
                    # Restore design info so a later retry can pick it up
                    context.user_data['_pending_design'] = pending_design
                    context.user_data['_pending_design_key'] = pending_design_key
                    await schedule_retry_via_redis_or_local(
                        query, text, reply_markup=reply_markup, delay=wait
                    )
                    try:
                        await safe_answer(query, text=f"Too many requests. Retrying in {wait}s.")
                    except Exception:
                        pass
                    return

                # Try edit_caption first to preserve the original message.
                # This avoids message position jumping and flickering.
                try:
                    await query.message.edit_caption(caption=text, reply_markup=reply_markup)
                    return
                except Exception:
                    pass

                # Fall back to delete+send_photo if edit_caption fails
                # (e.g., file_id expired, or the photo needs to be refreshed)
                try:
                    await query.message.delete()
                    await context.bot.send_photo(
                        chat_id=query.message.chat_id,
                        photo=pending_design,
                        caption=text,
                        reply_markup=reply_markup
                    )
                    return
                except Exception:
                    pass
            except Exception:
                pass
        # Fallback: use safe_edit_message which handles both photo and text messages.
        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    except Exception:
        # Final fallback: try safe_edit_message
        try:
            await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
        except Exception:
            pass


async def showcat_handler(update: Update, context: CallbackContext):
    """Show courses in the chosen category as URL buttons."""
    keyboard = []  # always initialize (fixes UnboundLocalError)
    query = update.callback_query
    await safe_answer(query)
    # Expect callback_data forms:
    #  - showcat::{path_or_name}
    #  - showcat::{path_or_name}::{page}
    #  - showcat::{path_or_name}::from_parent::{parent_path}::{parent_page}
    raw = query.data
    try:
        logger.debug("showcat_handler: raw callback_data=%r", raw)
    except Exception:
        pass
    page_from_callback = None
    parent_origin = None
    parent_origin_page = None
    encoded = ''
    # Support short stored refs: `showcat_ref::<key>` -> resolve payload
    if raw.startswith("showcat_ref::"):
        key = raw.split("::", 1)[1]
        payload = await _resolve_callback_payload(key)
        if not payload:
            _clear_design_pending(context)
            await safe_edit_message(query, "Reference expired. Please open the list again.", action_key=getattr(query, 'data', None))
            return
        # payload may contain `path`, optional `from_parent`, `parent_page`, and hidden `parent_index`
        cat_path = payload.get('path')
        parent_origin = payload.get('from_parent')
        # parent_page may be stored as int or string; normalize to int when present
        parent_origin_page = None
        try:
            if 'parent_page' in payload and payload.get('parent_page') is not None:
                parent_origin_page = int(payload.get('parent_page'))
        except Exception:
            parent_origin_page = None
        # capture any hidden parent_index (list of {path,name}) for fast Back/Forward
        parent_index = None
        try:
            if 'parent_index' in payload and isinstance(payload.get('parent_index'), (list, tuple)):
                parent_index = payload.get('parent_index')
        except Exception:
            parent_index = None
        # child page (if stored) — preserve when provided
        page_from_callback = None
        try:
            if 'page' in payload and payload.get('page') is not None:
                page_from_callback = int(payload.get('page'))
        except Exception:
            page_from_callback = None
        # ensure downstream logic that expects `encoded` works
        try:
            encoded = urllib.parse.quote_plus(cat_path) if cat_path else ""
        except Exception:
            encoded = ""

        # If this payload is a parent-index back reference, render the parent
        # page directly from the stored `parent_index` without extra DB hits.
        try:
            if payload.get('type') == 'parent_index_back' and parent_index is not None:
                # parent name/path
                parent_name = payload.get('parent')
                page = int(payload.get('parent_page') or 1)
                page_size = int(payload.get('page_size') or PAGE_SIZE)
                total = len(parent_index)
                start = (page - 1) * page_size
                slice_items = parent_index[start:start + page_size]
                keyboard = []
                for item in slice_items:
                    # item expected to be dict {path, name} or fallback string
                    if isinstance(item, dict):
                        item_path = item.get('path') or item.get('name')
                        item_name = item.get('name') or item.get('path') or str(item_path)
                    else:
                        item_path = str(item)
                        item_name = item_path
                    # create standard showcat payloads for the child entries
                    child_payload = {"type": "showcat", "path": item_path, "from_parent": parent_name, "parent_page": page, "parent_index": parent_index}
                    key2 = _store_callback_payload(child_payload)
                    keyboard.append([InlineKeyboardButton(item_name, callback_data=f"showcat_ref::{key2}")])

                # Navigation row
                nav = []
                total_pages = (total - 1) // page_size + 1 if total else 1
                last_page = max(1, total_pages)
                if page > 1:
                    nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=_shorten_showcat_cb(parent_name, page - 1)))
                    # Only show Home when not already on the first page
                    nav.append(InlineKeyboardButton("🏠 Home", callback_data=_shorten_showcat_cb(parent_name, 1)))
                if page < last_page:
                    nav.append(InlineKeyboardButton("➡️ Next", callback_data=_shorten_showcat_cb(parent_name, page + 1)))
                # Only show End when there are pages after the current one
                if total_pages > 1 and page < last_page:
                    nav.append(InlineKeyboardButton("⏭️ End", callback_data=_shorten_showcat_cb(parent_name, last_page)))
                if nav:
                    keyboard.append(nav)

                title = f"{parent_name} — Subcategories (page {page}/{last_page}):"
                await _send_design_photo(query, context, title, InlineKeyboardMarkup(keyboard))
                return
        except Exception:
            pass
    else:
        # Handle from_parent suffix first (preserves parent page info while
        # allowing child to open at page 1)
        if "::from_parent::" in raw:
            left, right = raw.split("::from_parent::", 1)
            left_parts = left.split("::")
            encoded = left_parts[1] if len(left_parts) > 1 else ""
            # left may optionally include a page too (rare)
            if len(left_parts) > 2:
                try:
                    page_from_callback = int(left_parts[2])
                except Exception:
                    page_from_callback = None
            # parse right as parent_path::parent_page
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
    

    # Current page for this category view (used when linking to coaches)
    page = page_from_callback or 1
    # Normalize the encoded token: strip whitespace and unquote safely.
    try:
        encoded = (encoded or "").strip()
        cat_path = urllib.parse.unquote_plus(encoded)
    except Exception:
        cat_path = (encoded or "").strip()
    # Persist originating categories page into user_data so other flows
    # (e.g., add-course) can reference the page the user came from.
    try:
        if parent_origin == 'categories' and parent_origin_page is not None:
            context.user_data['last_category_page'] = int(parent_origin_page)
        else:
            # record the page we are currently viewing for convenience
            context.user_data['last_category_page'] = int(page)
    except Exception:
        pass
    db = await get_db()
    # Try multiple resolution strategies to handle encoded/unencoded and
    # legacy name/path mismatches. Try exact path, exact name, then
    # fall back to the raw encoded token as well.
    category_doc = None
    try:
        # Exact path match
        category_doc = await db.categories.find_one({"path": cat_path})
        if not category_doc:
            # Exact name match
            category_doc = await db.categories.find_one({"name": cat_path})
        if not category_doc and encoded and encoded != cat_path:
            # Try using the raw encoded token too (some callbacks send unquoted)
            category_doc = await db.categories.find_one({"path": encoded}) or await db.categories.find_one({"name": encoded})
        if not category_doc:
            # Last-ditch case-insensitive match on name (handles minor casing/whitespace)
            try:
                category_doc = await db.categories.find_one({"name": {"$regex": f"^{re.escape(cat_path)}$", "$options": "i"}})
            except Exception:
                category_doc = None
        if not category_doc:
            await safe_edit_message(query, f'Category “{cat_path}” not found.', action_key=getattr(query, 'data', None))
            return
        # Debug: which field matched
        try:
            matched_on = None
            if category_doc.get('path') == cat_path:
                matched_on = 'path'
            elif category_doc.get('name') == cat_path:
                matched_on = 'name'
            else:
                matched_on = 'fallback'
            logger.debug("showcat_handler: resolved category_doc name=%r path=%r matched_on=%s encoded=%r", category_doc.get('name'), category_doc.get('path'), matched_on, encoded)
        except Exception:
            pass
    except Exception:
        await safe_edit_message(query, f'Category “{cat_path}” not found.', action_key=getattr(query, 'data', None))
        return

    # category display name and path
    cat_name = category_doc.get('name')
    cat_path = category_doc.get('path') or cat_name
    # Store category design photo info so _send_design_photo can attach
    # it to the keyboard message (instead of sending a standalone photo).
    try:
        from handlers.category_design import get_category_design
        design_file_id = await get_category_design(db, cat_name)
        if design_file_id:
            # Always set pending design so _send_design_photo can attach it
            # (design_sent_key guard removed to fix photo disappearing on revisit)
            context.user_data['_pending_design'] = design_file_id
    except Exception:
        pass

    # Remember the last viewed category so `/add` can preselect it
    try:
        context.user_data['last_viewed_category'] = cat_path
    except Exception:
        pass
    if not category_doc:
        await safe_edit_message(query, f'Category “{cat_name}” not found.', action_key=getattr(query, 'data', None))
        return

    # Goal: for a chosen category (topic), list coaches who have courses in this category.
    # Prefer a dedicated `coaches` collection with a `topics` field; otherwise derive coaches from embedded course 'coach' fields.
    coaches = []
    try:
        # Look for coaches that explicitly list this topic
        if hasattr(db, 'coaches'):
            # find coaches whose topics array contains this category name (case-insensitive)
            # coaches collection is typically small but still limit the returned
            # list to avoid unbounded memory usage.
            coaches = await db.coaches.find({"topics": cat_name}).to_list(length=PAGE_SIZE)
    except Exception:
        coaches = []

    # If no dedicated coaches found, derive from embedded course 'coach' fields
    if not coaches:
        derived = {}
        for crs in category_doc.get('courses', []):
            coach_name = crs.get('coach')
            if coach_name:
                slug = urllib.parse.quote_plus(coach_name)
                derived[slug] = coach_name
        # If still empty, we will fallback to showing the courses directly later
        coaches = [{'name': v, 'slug': k} for k, v in derived.items()]

    # First: show any child categories (sub-categories)
    try:
        try:
            logger.debug("showcat_handler: parsed page_from_callback=%r parent_origin=%r parent_origin_page=%r encoded=%r", page_from_callback, parent_origin, parent_origin_page, encoded)
        except Exception:
            pass
        # Server-side pagination for child categories under this category
        total_children = await _get_total_count(db, 'categories', {"parent": cat_name}, ttl=60)
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        children = await db.categories.find({"parent": cat_name}).sort("name", 1).skip(start).limit(page_size).to_list(length=page_size)
        try:
            logger.debug("showcat_handler: total_children=%s page=%s start=%s fetched_children=%s", total_children, page, start, len(children) if children is not None else 0)
        except Exception:
            pass
    except Exception:
        children = []

    if children:
        # Children were fetched server-side with skip/limit — they already
        # represent the requested page slice. Avoid re-slicing here which
        # produced empty pages when page>1 (start index >= len(children)).
        # Keep deterministic ordering within the fetched page.
        sorted_children = sorted(children, key=lambda c: (c.get('name') or '').lower())
        page_children = sorted_children

        keyboard = []
        # Batch-check children existence for page_children to avoid N queries
        child_names = [c.get('name') for c in page_children if c.get('name')]
        names_with_children = set()
        if child_names:
            try:
                names_with_children = await _get_children_flags(db, child_names, ttl=60)
            except Exception:
                names_with_children = set()

        for child in page_children:
            child_path = child.get('path') or child.get('name')
            payload = {"type": "showcat", "path": child_path, "from_parent": cat_path, "parent_page": page}
            key = _store_callback_payload(payload)
            # Mark child as empty when it has neither sub-children nor courses
            try:
                has_children = child.get('name') in names_with_children
                courses = child.get('courses', []) if isinstance(child, dict) else []
                is_empty = (not has_children) and (not _has_real_courses(courses))
            except Exception:
                is_empty = True
            display = f"{child.get('name')}{' (empty)' if is_empty else ''}"
            keyboard.append([InlineKeyboardButton(display, callback_data=f"showcat_ref::{key}")])

        # Navigation row (Previous / End / Next)
        nav = []
        total_pages = (total_children - 1) // page_size + 1 if total_children else 1
        last_page = max(1, total_pages)
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=_shorten_showcat_cb(cat_path, page-1)))

        # Put End between Prev and Next; only show it when there's a later page
        if total_pages > 1 and page < last_page:
            end_btn = InlineKeyboardButton("⏭️ End", callback_data=_shorten_showcat_cb(cat_path, last_page))
            if page == 1:
                nav.insert(0, end_btn)
            else:
                nav.append(end_btn)

        if (page * page_size) < total_children:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=_shorten_showcat_cb(cat_path, page+1)))

        if nav:
            keyboard.append(nav)

        # Breadcrumb / Home row (insert at top for context)
        try:
            # Show Home only when not already on the first page; Home
            # should return to this category's subcategories page 1.
            if page > 1:
                breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data=_shorten_showcat_cb(cat_path, 1))]
                keyboard.insert(0, breadcrumb_buttons)
        except Exception:
            pass

        # add up/back button to parent or top-level at the bottom for convenience
        # If this view was opened via a stored `showcat_ref` from a parent,
        # prefer returning to the recorded parent page (`parent_origin_page`).
        if parent_origin:
            try:
                # Special sentinel 'categories' means return to the paginated
                # top-level categories view rather than trying to open a
                # category literally called 'categories'. Handle that case
                # explicitly here.
                if parent_index:
                    # Create a stored back-ref that contains the parent's
                    # compact index so we can render the parent page without
                    # additional DB round-trips.
                    back_payload = {"type": "parent_index_back", "parent": parent_origin, "parent_page": parent_origin_page or 1, "parent_index": parent_index, "page_size": PAGE_SIZE}
                    key = _store_callback_payload(back_payload)
                    back_cb = f"showcat_ref::{key}"
                else:
                    if parent_origin == 'categories':
                        back_cb = f"categories_page::{parent_origin_page or 1}"
                    else:
                        pdoc = await db.categories.find_one({"name": parent_origin})
                        ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent_origin
                        back_cb = _shorten_showcat_cb(ppath, parent_origin_page or 1)
            except Exception:
                back_cb = "back_to_cats"
            keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=back_cb)])
        else:
            parent = category_doc.get('parent')
            if parent:
                pdoc = await db.categories.find_one({"name": parent})
                ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
                keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=_shorten_showcat_cb(ppath, page))])
            else:
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
        # Add Search button for courses in this category
        try:
            keyboard.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(cat_name))}::{page}")])
        except Exception:
            pass

        await _send_design_photo(query, context, f"{cat_path} — Subcategories (page {page}/{last_page}):", InlineKeyboardMarkup(keyboard))
        return

    # If the category doc contains a nested 'types' (category_type) level, show types first
    type_keys = None
    for key in ('types', 'category_types', 'subtypes', 'category_type'):
        if category_doc.get(key):
            type_keys = key
            break

    if type_keys:
        # Build type buttons; each type entry may be a string or dict with 'name'
        types_list = category_doc.get(type_keys) or []
        keyboard = []
        for t in types_list:
            if isinstance(t, str):
                t_name = t
            elif isinstance(t, dict):
                t_name = t.get('name') or t.get('type')
            else:
                continue
            keyboard.append([InlineKeyboardButton(t_name, callback_data=f"showtype::{urllib.parse.quote_plus(cat_name)}::{urllib.parse.quote_plus(t_name)}")])
        # Back to this category view (preserve current page)
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(cat_path, page))])
        await _send_design_photo(query, context, f"{cat_name} — Select a type:", InlineKeyboardMarkup(keyboard))
        return

    # If we found coaches, show them; otherwise fall back to showing courses in this category
    if coaches:
        # Include the current category page in the coach callback so we can
        # return the user to the same page after viewing details. Use short
        # stored refs when the callback_data would exceed Telegram's 64-byte
        # limit to avoid Button_data_invalid errors.
        keyboard = []
        for coach in coaches:
            coach_name = coach.get('name')
            coach_slug = coach.get('slug') or urllib.parse.quote_plus(coach_name)
            # If we have a parent_origin, always persist the payload so we
            # can carry parent_page info; otherwise try direct callback when short.
            if parent_origin:
                payload = {"type": "coach_in_cat", "category": cat_name, "coach_slug": coach_slug, "page": page, "from_parent": parent_origin, "parent_page": parent_origin_page}
                key = _store_callback_payload(payload)
                try:
                    logger.debug("coach_in_cat: created coach_in_cat_ref key=%s category=%s coach_slug=%s page=%s from_parent=%s parent_page=%s", key, cat_name, coach_slug, page, parent_origin, parent_origin_page)
                except Exception:
                    pass
                cb = f"coach_in_cat_ref::{key}"
            else:
                cb = f"coach_in_cat::{urllib.parse.quote_plus(cat_name)}::{coach_slug}::{page}"
                try:
                    if len(cb.encode('utf-8')) > 64:
                        payload = {"type": "coach_in_cat", "category": cat_name, "coach_slug": coach_slug, "page": page}
                        key = _store_callback_payload(payload)
                        cb = f"coach_in_cat_ref::{key}"
                except Exception:
                    pass
            keyboard.append([InlineKeyboardButton(coach_name, callback_data=cb)])
        # Back to this category view (preserve current page)
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(cat_path, page))])
        await _send_design_photo(query, context, f"Coaches in '{cat_name}':", InlineKeyboardMarkup(keyboard))
        return

    # Fallback: show courses if no coaches found
    courses = category_doc.get('courses', [])
    logger.debug("showcat_handler: category=%s courses_count=%s", cat_name, len(courses))
    if not courses:
        # Offer a Back button so the user stays in the browsing flow instead
        # of being dropped out with a plain message.
        parent = category_doc.get('parent')
        keyboard = []
        # If this view was opened via a stored showcat_ref, prefer returning
        # to the recorded parent_origin (which may be the paginated categories
        # listing) using parent_origin_page when available.
        if parent_origin:
            try:
                # Special sentinel 'categories' means return to the paginated
                # top-level categories view at the recorded page.
                if parent_origin == 'categories':
                    back_cb = f"categories_page::{parent_origin_page or 1}"
                else:
                    # Otherwise treat parent_origin as a category name/path
                    try:
                        pdoc = await db.categories.find_one({"name": parent_origin})
                        ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent_origin
                    except Exception:
                        ppath = parent_origin
                    back_cb = _shorten_showcat_cb(ppath, parent_origin_page or 1)
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])
            except Exception:
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
        else:
            if parent:
                pdoc = await db.categories.find_one({"name": parent})
                ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(ppath, page))])
            else:
                keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
    # Add Search button for courses in this category
    try:
        keyboard.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(cat_name))}::{page}")])
    except Exception:
        pass

        # Provide an inline "Add course" button that starts the /add flow with
        # this category preselected. Store a short payload reference so the
        # ConversationHandler can be entered via callback without exceeding
        # Telegram callback_data limits.
        # Use _send_design_photo so even empty categories show their design banner.
        await _send_design_photo(
            query,
            context,
            f'Category “{cat_name}” is empty.\nUse /add to populate it.',
            InlineKeyboardMarkup(keyboard),
        )
        return
    # Use paginated courses view for this category. Pass parent path as
    # origin_context so Home will return to the parent directory.
    page = page_from_callback or 1
    parent = category_doc.get('parent')
    # Prefer the stored parent_origin (from a showcat_ref) so we restore
    # the exact parent page the user navigated from; otherwise fall back
    # to the category's DB parent path.
    origin_ctx = None
    if parent_origin:
        origin_ctx = parent_origin
    else:
        if parent:
            try:
                pdoc = await db.categories.find_one({"name": parent})
                origin_ctx = pdoc.get('path') if pdoc and pdoc.get('path') else parent
            except Exception:
                origin_ctx = parent

    text, reply_markup = build_courses_page(courses, page=page, origin_type='category', category=cat_name, origin_context=origin_ctx, origin_context_page=parent_origin_page, store_page_ref=True)
    if not text:
        await safe_edit_message(query, f"No courses found in '{cat_name}' on page {page}.", action_key=getattr(query, 'data', None))
        return
    # Add Search button for courses in this category
    try:
        kb = list(reply_markup.inline_keyboard)
        kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(cat_name))}::{page}")])
        reply_markup = InlineKeyboardMarkup(kb)
    except Exception:
        pass
    await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))


async def handle_back_to_cats(update: Update, context: CallbackContext):
    """Handle the Back callback and show the categories list with search button."""
    query = update.callback_query
    await safe_answer(query)
    # Delegate to the full categories_page which includes pagination, search button, etc.
    try:
        await categories_page(update, context, page=1)
    except Exception as e:
        logger.error(f"Error returning to categories: {e}")
        await safe_edit_message(query, "An unexpected error occurred. Please try again later.", action_key=getattr(query, "data", None))
async def list_courses(update: Update, context: CallbackContext):
    """List all available courses with pagination."""
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        # Build a flattened list of courses via aggregation to avoid loading
        # full category documents into memory.
        page = 1
        page_size = PAGE_SIZE
        # Paginated aggregation for unified course listing. Fetch one extra
        # item (page_size + 1) to detect whether a Next page exists without
        # performing a full collection count.
        start = (page - 1) * page_size
        items_pipeline = [
            {"$unwind": "$courses"},
            {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name", "id": "$courses.id"}},
            {"$sort": {"name": 1}},
            {"$skip": start},
            {"$limit": page_size + 1}
        ]
        async with _db_timing(f"list_courses:page:{page}"):
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
        all_courses = sorted(all_courses, key=lambda c: (c.get('name') or '').lower())
        # Compute accurate total count for global listing when possible
        try:
            cnt_doc = await db.categories.aggregate([
                {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
                {"$group": {"_id": None, "count": {"$sum": "$n"}}}
            ]).to_list(length=1)
            total_courses = int(cnt_doc[0].get('count')) if cnt_doc else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses)))
        except Exception:
            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses))
        if all_courses:
            # Build unified page UI
            text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global', origin_context=None, total_count=total_courses, is_page=True)
            if not text:
                await update.message.reply_text("No courses available.")
                return
            # Add Search button for all courses
            try:
                kb = list(reply_markup.inline_keyboard)
                kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_courses::global::{page}")])
                reply_markup = InlineKeyboardMarkup(kb)
            except Exception:
                pass
            msg = await update.message.reply_text(text, reply_markup=reply_markup)
            try:
                schedule_close_inline_message(msg)
            except Exception:
                pass
        else:
            await update.message.reply_text("No courses available.")
    except Exception as e:
        logger.error(f"Error listing courses: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")

logger.info(f"[STATE] returning {CREATE_CAT_NAME=} id={id(CREATE_CAT_NAME)}")
async def create_category(update: Update, context: CallbackContext):
    # Present existing categories as optional parents
    # Owner-only: restrict create category to bot owner
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = None
    try:
        user_id = getattr(update.effective_user, 'id', None) or (update.message.from_user.id if getattr(update, 'message', None) and getattr(update.message, 'from_user', None) else None)
    except Exception:
        user_id = None
    if owner_id is not None and user_id != owner_id:
        try:
            await update.message.reply_text("Unauthorized")
        except Exception:
            pass
        return ConversationHandler.END
    try:
        logger.info("create_category invoked: user=%s chat=%s", getattr(update, 'effective_user', None).id if getattr(update, 'effective_user', None) else None, getattr(update, 'effective_chat', None).id if getattr(update, 'effective_chat', None) else None)
    except Exception:
        logger.info("create_category invoked (unable to read user/chat)")
    db = await get_db()
    cats = []
    try:
        # Show only top-level parent categories for parent selection
        # Limit results to avoid loading the entire collection in fallback paths
        page_size = PAGE_SIZE
        cats = await db.categories.find({"parent": {"$exists": False}}).sort("name", 1).limit(page_size).to_list(length=page_size)
    except Exception:
        cats = []

    # Use paginated createcat_page for consistent viewing
    try:
        await createcat_page(update.message, context, page=1)
    except Exception as e:
        logger.exception("create_category: createcat_page failed, falling back to simple keyboard")
        # Fallback to previous behavior
        keyboard = []
        keyboard.append([InlineKeyboardButton("(Top-level)", callback_data=f"createcat_parent::")])
        for cat in cats:
            keyboard.append([InlineKeyboardButton(cat.get('name'), callback_data=f"createcat_parent::{urllib.parse.quote_plus(cat.get('name'))}")])
        await update.message.reply_text("Select a parent category (or choose Top-level):", reply_markup=InlineKeyboardMarkup(keyboard))
    return CREATE_CAT_PARENT


async def handle_create_category_parent(update: Update, context: CallbackContext):
    """Callback handler to choose a parent for a new category."""
    query = update.callback_query
    await safe_answer(query)
    # Owner-only guard for create category callbacks
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = getattr(query.from_user, 'id', None)
    if owner_id is not None and user_id != owner_id:
        await safe_edit_message(query, "Unauthorized", action_key=getattr(query, 'data', None))
        return ConversationHandler.END
    encoded = query.data.split("::", 1)[1]
    parent = urllib.parse.unquote_plus(encoded) if encoded else None
    # Store chosen parent in user_data for the following name prompt
    context.user_data['new_cat_parent'] = parent
    try:
        logger.info("handle_create_category_parent: user=%s parent=%s", update.effective_user.id if update.effective_user else None, parent)
    except Exception:
        logger.debug("handle_create_category_parent: log failed")
    if parent:
        prompt = f"Enter the new category name (parent: {parent}):"
    else:
        prompt = "Enter the new top-level category name:"
    # Ask for the name via a simple text prompt
    await query.message.reply_text(prompt)
    return CREATE_CAT_NAME


async def handle_create_category_parent_text(update: Update, context: CallbackContext):
    """Allow users to type a parent category name instead of pressing a button.

    Stores chosen parent in `context.user_data['new_cat_parent']` and prompts
    for the new category name (same as the callback-based flow).
    """
    # Owner-only guard for message-based create flows
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = None
    try:
        user_id = update.message.from_user.id if getattr(update, 'message', None) and getattr(update.message, 'from_user', None) else getattr(update.effective_user, 'id', None)
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
        context.user_data['new_cat_parent'] = parent
        prompt = f"Enter the new category name (parent: {parent}):"
    else:
        context.user_data['new_cat_parent'] = None
        prompt = "Enter the new top-level category name:"
    await update.message.reply_text(prompt)
    return CREATE_CAT_NAME


async def create_parent(update: Update, context: CallbackContext):
    """Create a top-level parent category (explicit command)."""
    # Owner-only: restrict create parent to bot owner
    try:
        owner_env = os.getenv('BOT_OWNER_ID')
        owner_id = int(owner_env) if owner_env else None
    except Exception:
        owner_id = None
    user_id = None
    try:
        user_id = getattr(update.effective_user, 'id', None) or (update.message.from_user.id if getattr(update, 'message', None) and getattr(update.message, 'from_user', None) else None)
    except Exception:
        user_id = None
    if owner_id is not None and user_id != owner_id:
        try:
            await update.message.reply_text("Unauthorized")
        except Exception:
            pass
        return ConversationHandler.END
    # mark that the new category should be top-level
    context.user_data['new_cat_parent'] = None
    await update.message.reply_text("Enter the new parent category name:")
    return CREATE_CAT_NAME
    
async def handle_category_name(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    category_name = update.message.text.strip()
    logger.info(f"[CAT-INSERT-START] name={category_name!r} uid={user_id}")
    try:
        logger.debug("handle_category_name: incoming message=%s", update.message.text if getattr(update, 'message', None) else None)
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
        logger.info(f"[CAT-DB] using database: {db.name}")
        coll = db['categories']
        logger.info(f"[CAT-INSERT] about to insert {category_name!r}")

        # Check for a chosen parent stored in user_data
        parent = context.user_data.pop('new_cat_parent', None)
        # assign a stable UUID for categories so parents/coaches can be
        # referenced by id like courses do. Keep Mongo _id untouched.
        doc = {"name": category_name, "created_by": user_id, "id": str(uuid.uuid4())}
        if parent:
            # try to resolve parent's path if present; parent is stored as
            # name for backward-compatibility. Keep parent field as name.
            parent_doc = await db.categories.find_one({"name": parent})
            parent_path = parent_doc.get('path') if parent_doc and parent_doc.get('path') else parent
            doc['parent'] = parent
            doc['path'] = f"{parent_path}/{category_name}"

        # Duplicate checks: detect same-name siblings or top-level parents
        try:
            if parent:
                # same parent + name conflict
                existing_child = await coll.find_one({"name": category_name, "parent": parent})
                if existing_child:
                    await update.message.reply_text(f"A child category named '{category_name}' already exists under '{parent}' (id: {existing_child.get('id') or existing_child.get('_id')}). Please choose a different name.")
                    return CREATE_CAT_NAME
                # warn if there are existing courses under the parent that use this name as a coach
                try:
                    coach_conflict = await _get_total_count(db, 'categories', {"name": parent, "courses.coach": category_name}, ttl=15)
                    if coach_conflict > 0:
                        await update.message.reply_text(f"Warning: There are existing courses under '{parent}' with a coach name '{category_name}'. Creating a child category with the same name may cause ambiguity. Please pick a different name.")
                        return CREATE_CAT_NAME
                except Exception:
                    pass
            else:
                # top-level parent duplicate check (name or path)
                existing_parent = await coll.find_one({"$or": [{"name": category_name}, {"path": category_name}]})
                if existing_parent:
                    await update.message.reply_text(f"A parent category named '{category_name}' already exists (id: {existing_parent.get('id') or existing_parent.get('_id')}). Please choose a different name.")
                    return CREATE_CAT_NAME
        except Exception:
            # non-fatal; proceed to attempt insert which may still fail with DuplicateKeyError
            pass

        result = await coll.insert_one(doc)
        logger.info(f"[CAT-INSERT-DONE] _id={result.inserted_id}")
        # Explicit success logs for parent vs child categories
        if not parent:
            logger.info(f"[CAT-INSERT-PARENT] Created top-level parent category '{category_name}' _id={result.inserted_id}")
        else:
            logger.info(f"[CAT-INSERT-CHILD] Created category '{category_name}' under parent '{parent}' _id={result.inserted_id}")
        await update.message.reply_text(f"Category ‘{category_name}’ saved ✔")

        # After creating, show the parent view so the user can confirm the new
        # category appears in the correct place. If top-level, show top-level
        # categories; otherwise show the parent's children list.
        try:
            # Return to the page the user was browsing when they clicked create
            last_page = context.user_data.pop('createcat_last_page', 1)
            if not parent:
                # Show top-level categories using the same paginated view as `/categories`
                # so newly-created parents behave like the categories listing.
                await categories_page(update.message, context, page=last_page)
            else:
                # Show paginated children of the parent including the newly created category
                await children_page(update.message, context, parent, page=last_page)
        except Exception:
            # Non-fatal; ignore errors when trying to display the view
            pass

        return ConversationHandler.END
    except DuplicateKeyError:
        logger.warning(f"[CAT-INSERT-DUP] category already exists: {category_name!r}")
        await update.message.reply_text(f"A category named '{category_name}' already exists. Please choose a different name.")
        return CREATE_CAT_NAME
    except Exception as exc:
        logger.error(f"[CAT-INSERT-FAIL] {exc}", exc_info=True)
        await update.message.reply_text("Save failed – check console.")
        return ConversationHandler.END

async def handle_category_selection(update: Update, context: CallbackContext):
    """List courses in the chosen category – each course button is a direct URL."""
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
    # Lazy-load first page of courses for this category instead of pulling
    # the entire `courses` array into memory. This behaves like an AJAX
    # page load: only the accessed slice is returned.
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
            f'Category “{cat_name}” is empty.\nUse /add to populate it.',
            None,
        )
        return

    keyboard = []
    for crs in items:
        try:
            keyboard.append([InlineKeyboardButton(crs.get('name'), url=crs.get('link'))])
        except Exception:
            continue

    # Pagination / Back button: compute parent path if present
    try:
        pdoc = await db.categories.find_one({"name": cat_path}, projection={"parent": 1})
        parent = pdoc.get('parent') if pdoc else None
    except Exception:
        parent = None
    if parent:
        try:
            pdoc2 = await db.categories.find_one({"name": parent}, projection={"path": 1})
            ppath = pdoc2.get('path') if pdoc2 and pdoc2.get('path') else parent
        except Exception:
            ppath = parent
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=_shorten_showcat_cb(ppath, 1))])
    else:
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])

    # Add quick Next button if more pages exist
    try:
        total_items = await _get_courses_count(db, cat_path)
        total_pages = math.ceil(total_items / page_size) if total_items > 0 else 1
        if total_pages > 1:
            keyboard.append([InlineKeyboardButton("➡️ Next", callback_data=f"courses::category::{urllib.parse.quote_plus(cat_path)}::2")])
    except Exception:
        pass

    await safe_edit_message(query, f'📚 Tap any course to open its link:', reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    
async def handle_course_selection(update: Update, context: CallbackContext):
    """Handle the selection of a course from the buttons."""
    query = update.callback_query
    await safe_answer(query)

    data = query.data
    # Support short refs: course_ref::<key> -> lookup payload in CALLBACK_MAP
    origin_type = None
    origin_page = 1
    cat_name = None
    course_name = None

    if data.startswith("course_ref::"):
        # Support appended back token: course_ref::<key>::back::<encoded_back_cb>
        rest = data[len("course_ref::"):]
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
            await safe_edit_message(query, "Reference expired. Please open the list again.", action_key=getattr(query, 'data', None))
            return

        # Debug tracing: show raw data, resolved key, appended back token and payload summary
        try:
            logger.debug("handle_course_selection: raw_query_data=%s key=%s appended_back=%s payload_back=%s payload_keys=%s origin_type=%s origin_page=%s",
                         data, key, appended_back, payload.get('back_cb'), list(payload.keys()) if isinstance(payload, dict) else None,
                         payload.get('origin_type'), payload.get('origin_page'))
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
        origin_context = payload.get('origin_context')
        try:
            origin_context_page = int(payload.get('origin_context_page')) if payload.get('origin_context_page') is not None else None
        except Exception:
            origin_context_page = None
        # Prefer an explicit appended back token, otherwise fall back to saved payload
        saved_back_cb = appended_back or payload.get('back_cb')
    else:
        # Legacy inline `course::` callback format has been removed.
        # All current course selections use persisted `course_ref::` references.
        await safe_edit_message(
            query,
            "This action used a legacy callback format which has been removed. Please reopen the list and try again.",
            action_key=getattr(query, 'data', None),
        )
        return

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return

    try:
        course = None
        if cat_name:
            # Resolve by name OR path to handle mixed payloads
            category_doc = await db.categories.find_one({"$or": [{"name": cat_name}, {"path": cat_name}]})
            if category_doc:
                for crs in category_doc.get('courses', []):
                    # Prefer id-based match when available
                    if course_id and crs.get('id') == course_id:
                        course = {"id": crs.get('id'), "name": crs.get('name'), "link": crs.get('link'), "category": category_doc.get('name')}
                        break
                    if (not course_id) and crs.get('name') == course_name:
                        course = {"id": crs.get('id'), "name": crs.get('name'), "link": crs.get('link'), "category": category_doc.get('name')}
                        break
            else:
                # If we couldn't resolve the category by name/path, fall back
                # to searching across all categories for the course so Details
                # still open even when payload carried a different token.
                try:
                    if course_id:
                        category_doc = await db.categories.find_one({"courses.id": course_id}, projection={"name": 1, "courses": 1})
                        if category_doc:
                            for crs in category_doc.get('courses', []):
                                if crs.get('id') == course_id:
                                    course = {"id": crs.get('id'), "name": crs.get('name'), "link": crs.get('link'), "category": category_doc.get('name')}
                                    break
                    else:
                        category_doc = await db.categories.find_one({"courses.name": course_name}, projection={"name": 1, "courses": 1})
                        if category_doc:
                            for crs in category_doc.get('courses', []):
                                if crs.get('name') == course_name:
                                    course = {"id": crs.get('id'), "name": crs.get('name'), "link": crs.get('link'), "category": category_doc.get('name')}
                                    break
                except Exception:
                    category_doc = None
        else:
            # Nested isolation policy: do NOT resolve course names across
            # categories. Only allow lookup by explicit course `id` (unique
            # identifier) when no category context is provided. This enforces
            # that course names are namespaced to their category and avoids
            # ambiguous cross-category matches.
            if course_id:
                category_doc = await db.categories.find_one({"courses.id": course_id}, projection={"name": 1, "courses": 1})
                if category_doc:
                    for crs in category_doc.get('courses', []):
                        if crs.get('id') == course_id:
                            course = {"id": crs.get('id'), "name": crs.get('name'), "link": crs.get('link'), "category": category_doc.get('name')}
                            break
            else:
                # Ambiguous name without category context — refuse to resolve
                # across categories. Prompt the user to open the course from
                # its category listing so the Details view is unambiguous.
                await safe_edit_message(query, "Course name is ambiguous. Open the course from its category list to view details.", action_key=getattr(query, 'data', None))
                return
                # no outer loop to break from here; continue processing

        if course:
            # Determine canonical category for this course (prefer explicit field)
            course_category = course.get('category') if isinstance(course, dict) else None
            logger.debug("handle_course_selection: origin_type=%s origin_page=%s course_category=%s cat_name=%s courses_in_doc=%s", origin_type, origin_page, course_category, cat_name, bool(course_category))
            if not course_category:
                course_category = cat_name

            # If the payload didn't include an origin_type but the handler
            # has a category context (e.g. user opened Details from a
            # category view or just added a course into a category), treat
            # it as a category-origin so we show the Coaches / All Categories
            # row instead of the global Back which can be confusing.
            if not origin_type and course_category:
                origin_type = 'category'
                origin_page = origin_page or 1
                logger.debug("handle_course_selection: inferred origin_type='category' from course_category=%s", course_category)

            # Build Back callback: for category-origin details, always return
            # to the category view (coaches list) so users see the main coach
            # menu rather than a single-course listing. For other origins,
            # prefer saved_back_cb when present, otherwise compute a sensible
            # fallback.
            if origin_type == 'category':
                # Always route back to the category's courses listing page
                # (not the broader coach/parent view). Preserve the
                # originating page when available and clamp to available
                # pages; still fall back to top-level categories if the
                # target category can't be resolved.
                back_target = course_category or cat_name or None
                # Normalize any saved_back_cb that might point to a coach
                # or parent view — prefer returning to the courses listing
                # for the course's category.
                if saved_back_cb:
                    try:
                        sb = str(saved_back_cb)
                        if sb.startswith('courses::category::'):
                            # Use saved category pagination if present
                            back_cb = sb
                        else:
                            # Map coach/showcat/back_to_cats to category courses
                            if (sb.startswith('courses::coach::') or sb.startswith('showcat') or sb.startswith('showcat_ref') or sb.startswith('categories_page') or sb == 'back_to_cats') and back_target:
                                try:
                                    pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                                    ppath = pdoc.get('path') if pdoc and pdoc.get('path') else back_target
                                except Exception:
                                    ppath = back_target
                                # try extract a page from saved token if present
                                page_to_use = None
                                try:
                                    parts = sb.split('::')
                                    if len(parts) >= 4:
                                        page_to_use = int(parts[-1])
                                except Exception:
                                    page_to_use = None
                                page_to_use = page_to_use or origin_page or 1
                                back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{page_to_use}"
                            else:
                                # Unknown saved token: prefer courses listing if we have a target
                                if back_target:
                                    try:
                                        pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                                        ppath = pdoc.get('path') if pdoc and pdoc.get('path') else back_target
                                    except Exception:
                                        ppath = back_target
                                    back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{origin_page or 1}"
                                else:
                                    back_cb = sb
                    except Exception:
                        back_cb = None

                if not back_cb:
                    # Compute page count and clamp even when the category has
                    # zero courses — we still route to the category courses
                    # page (which may display "empty") rather than the
                    # parent/coach listing.
                    back_target = back_target or 'categories'
                    try:
                        pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                        ppath = pdoc.get('path') if pdoc and pdoc.get('path') else back_target
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
                    if str(ppath).lower() == 'categories' and back_target == 'categories':
                        back_cb = f"categories_page::{clamped_page}"
                    else:
                        back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{clamped_page}"
                logger.debug("handle_course_selection: computed category back_cb=%s", back_cb)
                try:
                    # Verify and reconcile the computed back callback to avoid
                    # routing users to empty/invalid pages (auto-fix Option C).
                    back_cb = await _reconcile_back_cb(db, back_cb, course_category=course_category, origin_page=origin_page)
                    logger.debug("handle_course_selection: reconciled category back_cb=%s", back_cb)
                except Exception:
                    pass
            else:
                if saved_back_cb:
                    # Preserve saved back token for coach-origin details so
                    # Back returns to the same coach listing the user came
                    # from. Do not remap coach callbacks to category lists.
                    back_cb = str(saved_back_cb)
                else:
                    if origin_type == 'coach' and origin_page:
                        # Route coach-origin details back to the same coach
                        # listing page the user came from (preserve page).
                        coach_slug = origin_context or course_category or cat_name or ''
                        coach_slug_enc = urllib.parse.quote_plus(str(coach_slug))
                        back_cb = f"courses::coach::{coach_slug_enc}::{origin_page}"
                        logger.debug("handle_course_selection: computed back_cb=%s", back_cb)
                    elif origin_type == 'global' and origin_page:
                        back_cb = f"courses::global::{origin_page}"
                    else:
                        # default fallback: global page 1
                        back_cb = "courses::global::1"

            # Defensive normalization: if we know this course belongs to a
            # category, ensure the Back callback routes to that category's
            # courses listing (preserving page). This protects against
            # malformed or legacy saved_back_cb values that point to the
            # outer coach/parent view. Overwrite any showcat-style token so
            # users always return to the category's courses listing.
            try:
                if course_category:
                    try:
                        pdoc = await db.categories.find_one({"name": course_category}, projection={"path": 1})
                        ppath = pdoc.get('path') if pdoc and pdoc.get('path') else course_category
                    except Exception:
                        ppath = course_category
                    try:
                        page_to_use = int(origin_page or 1)
                    except Exception:
                        page_to_use = 1
                    back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{page_to_use}"
            except Exception:
                # If normalization fails, keep whatever back_cb was computed
                pass

            # Verify the computed `back_cb` actually leads to a page that
            # contains this course. If not, search the DB for the course's
            # true category and compute the page where it appears so Back
            # returns the user to a list that includes the course.
            try:
                # Only validate category-style back targets
                if back_cb and isinstance(back_cb, str) and back_cb.startswith('courses::category::') and course and course.get('name'):
                    parts = back_cb.split('::')
                    if len(parts) >= 4:
                        target_cat = urllib.parse.unquote_plus(parts[2])
                        target_page = int(parts[3]) if parts[3].isdigit() else 1
                        # Check whether the target category actually contains this course
                        try:
                            # Use aggregation to build an ordered list of course names (server-side sort)
                            pipeline = [
                                {"$match": {"$or": [{"name": target_cat}, {"path": target_cat}]}},
                                {"$unwind": "$courses"},
                                {"$project": {"name": "$courses.name", "id": "$courses.id"}},
                                {"$sort": {"name": 1}}
                            ]
                            course_list = await db.categories.aggregate(pipeline).to_list(length=500)
                            found_index = None
                            for idx, item in enumerate(course_list):
                                try:
                                    if course.get('id') and item.get('id') == course.get('id'):
                                        found_index = idx
                                        break
                                    if item.get('name') == course.get('name'):
                                        found_index = idx
                                        break
                                except Exception:
                                    continue
                            if found_index is not None:
                                computed_page = (found_index // PAGE_SIZE) + 1
                                if computed_page != target_page:
                                    # Update back_cb to the page where the course actually appears
                                    pdoc = await db.categories.find_one({"$or": [{"name": target_cat}, {"path": target_cat}]}, projection={"path": 1})
                                    ppath = pdoc.get('path') if pdoc and pdoc.get('path') else target_cat
                                    back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{computed_page}"
                                    logger.debug("handle_course_selection: adjusted back_cb to page containing course: %s", back_cb)
                            else:
                                # Course not found in the computed category — try locating it anywhere
                                try:
                                    if course.get('id'):
                                        found = await db.categories.find_one({"courses.id": course.get('id')}, projection={"name": 1})
                                    else:
                                        found = await db.categories.find_one({"courses.name": course.get('name')}, projection={"name": 1})
                                    if found:
                                        true_cat = found.get('name')
                                        # compute page by enumerating sorted course names for true_cat
                                        pipeline2 = [
                                            {"$match": {"name": true_cat}},
                                            {"$unwind": "$courses"},
                                            {"$project": {"name": "$courses.name", "id": "$courses.id"}},
                                            {"$sort": {"name": 1}}
                                        ]
                                        clist = await db.categories.aggregate(pipeline2).to_list(length=500)
                                        fidx = None
                                        for idx, item in enumerate(clist):
                                            try:
                                                if course.get('id') and item.get('id') == course.get('id'):
                                                    fidx = idx
                                                    break
                                                if item.get('name') == course.get('name'):
                                                    fidx = idx
                                                    break
                                            except Exception:
                                                continue
                                        if fidx is not None:
                                            computed_page = (fidx // PAGE_SIZE) + 1
                                            pdoc = await db.categories.find_one({"name": true_cat}, projection={"path": 1})
                                            ppath = pdoc.get('path') if pdoc and pdoc.get('path') else true_cat
                                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{computed_page}"
                                            logger.debug("handle_course_selection: located course in different category, updated back_cb=%s", back_cb)
                                except Exception:
                                    pass
                        except Exception:
                            pass
            except Exception:
                pass

            # Prepare persisted short ref for delete action (await the store helper)
            delete_payload = {
                'category': course_category,
                'id': course.get('id'),
                'name': course.get('name'),
                'origin_type': origin_type,
                'origin_page': origin_page,
                'origin_context': origin_context,
                'origin_context_page': origin_context_page,
            }
            try:
                delete_key = _store_callback_payload(delete_payload)
            except Exception:
                delete_key = None

            delete_cb = f"delete_ref::{delete_key}" if delete_key else "delete_ref::"

            # Build detail navigation. If opened from a category, remove the
            # Back button (it previously routed to the global /courses GUI)
            # and instead show a row with Coaches + All Categories.
            if origin_type == 'category':
                # Compute a back callback that returns to the category's main
                # view (coaches / subcategories) so users see the coach menu
                # rather than a single-course listing. Prefer any saved_back_cb
                # from the payload; otherwise compute a showcat callback and
                # clamp the page to the available range.
                try:
                    if saved_back_cb:
                        sb = str(saved_back_cb)
                        # Prefer returning to the category's courses listing
                        # rather than the top-level `showcat` view. If the
                        # saved token points to a showcat/categories view,
                        # translate it into the corresponding courses::category
                        # callback so Details->Back returns to the expected
                        # course listing context.
                        try:
                            if sb.startswith('courses::category::'):
                                back_cb = sb
                            else:
                                # Map showcat/ref/categories/coach tokens to the
                                # category courses listing using the course's
                                # category name/path and preserve origin_page.
                                try:
                                    pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                                    ppath = pdoc.get('path') if pdoc and pdoc.get('path') else back_target
                                except Exception:
                                    ppath = back_target
                                page_to_use = origin_page or 1
                                back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{page_to_use}"
                        except Exception:
                            back_cb = None
                except Exception:
                    back_cb = None

                if not back_cb:
                    back_target = course.get('category') or cat_name or '1'
                    try:
                        # Determine the display path for the target category
                        try:
                            # Fetch only the path and compute the courses array length
                            # via aggregation so we don't transfer the entire array
                            # payload into memory (which can be slow for large arrays).
                            pdoc = await db.categories.find_one({"name": back_target}, projection={"path": 1})
                            ppath = pdoc.get('path') if pdoc and pdoc.get('path') else back_target
                            # Aggregation to compute size of courses array efficiently
                            pipeline = [
                                {"$match": {"name": back_target}},
                                {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}}
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
                        # Prefer returning to the category main `showcat` view
                        # Return to the category's course listing page so the
                        # user remains in the same context they were browsing.
                        try:
                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{clamped_page}"
                        except Exception:
                            back_cb = f"courses::category::{urllib.parse.quote_plus(str(back_target))}::{clamped_page}"
                    except Exception:
                        back_cb = f"courses::global::{origin_page}"

                # Reconcile computed back callback before building navigation
                try:
                    back_cb = await _reconcile_back_cb(db, back_cb, course_category=course.get('category') or cat_name, origin_page=origin_page)
                except Exception:
                    pass

                # Enforce category-listing Back for category-origin details.
                # This guarantees the Back button returns the user to the
                # course listing for the course's category (Option C).
                try:
                    if course.get('category'):
                        try:
                            pdoc = await db.categories.find_one({"name": course.get('category')}, projection={"path": 1})
                            ppath = pdoc.get('path') if pdoc and pdoc.get('path') else course.get('category')
                        except Exception:
                            ppath = course.get('category')
                        candidate_cb = f"courses::category::{urllib.parse.quote_plus(str(ppath))}::{origin_page or 1}"
                        try:
                            back_cb = await _reconcile_back_cb(db, candidate_cb, course_category=course.get('category'), origin_page=origin_page)
                        except Exception:
                            back_cb = candidate_cb
                except Exception:
                    pass

                # Show Delete Course only to the configured owner (if set)
                try:
                    user_id = getattr(query.from_user, 'id', None)
                    owner_env = os.getenv('BOT_OWNER_ID')
                    try:
                        owner_id = int(owner_env) if owner_env else None
                    except Exception:
                        owner_id = None
                    if owner_id is not None and user_id != owner_id:
                        nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb)]
                    else:
                        nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb), InlineKeyboardButton("Delete Course", callback_data=delete_cb)]
                except Exception:
                    nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb), InlineKeyboardButton("Delete Course", callback_data=delete_cb)]
                extra_row = []
                try:
                    # Coaches are represented as child categories under the
                    # parent/topic. If this course's category is a coach (i.e.
                    # a child), link to its parent so the user sees all coaches.
                    if course_category:
                        parent_doc = await db.categories.find_one({"name": course_category})
                        if parent_doc:
                            parent = parent_doc.get('parent')
                            if parent:
                                pdoc = await db.categories.find_one({"name": parent})
                                ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
                                extra_row.append(InlineKeyboardButton("🏠 Coaches", callback_data=_shorten_showcat_cb(ppath, origin_page)))
                                logger.debug("handle_course_selection: coaches button -> parent=%s ppath=%s", parent, ppath)
                    # Always include All Categories button next to Coaches (or alone)
                    extra_row.append(InlineKeyboardButton("📚 All Categories", callback_data="back_to_cats"))
                except Exception:
                    # Fallback: show only All Categories
                    extra_row = [InlineKeyboardButton("All Categories", callback_data="back_to_cats")]
                # ensure extra_row is a single keyboard row
                keyboard = [nav_row, extra_row]
            else:
                # default behavior: show Back + Delete and an optional home/parent row
                try:
                    user_id = getattr(query.from_user, 'id', None)
                    owner_env = os.getenv('BOT_OWNER_ID')
                    try:
                        owner_id = int(owner_env) if owner_env else None
                    except Exception:
                        owner_id = None
                    if owner_id is not None and user_id != owner_id:
                        nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb)]
                    else:
                        nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb), InlineKeyboardButton("Delete Course", callback_data=delete_cb)]
                except Exception:
                    nav_row = [InlineKeyboardButton("🔙 Back", callback_data=back_cb), InlineKeyboardButton("Delete Course", callback_data=delete_cb)]
                extra_row = None
                try:
                    if origin_type != 'global':
                        if course_category:
                            parent_doc = await db.categories.find_one({"name": course_category})
                            if parent_doc:
                                parent = parent_doc.get('parent')
                                if parent:
                                    pdoc = await db.categories.find_one({"name": parent})
                                    ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
                                    extra_row = [InlineKeyboardButton("🏠 Coaches", callback_data=_shorten_showcat_cb(ppath, origin_page))]
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
            reply_markup = InlineKeyboardMarkup(keyboard)
            details = (
                f"📚 **Course Details**\n\n"
                f"Name: {course.get('name')}\n"
                f"Link: {course.get('link')}\n"
                f"Category: {course_category}"
            )
            try:
                logger.debug("handle_course_selection: FINAL back_cb=%s origin_type=%s origin_page=%s saved_back_cb=%s origin_context=%s course_category=%s course_id=%s",
                             back_cb, origin_type, origin_page, saved_back_cb if 'saved_back_cb' in locals() else None,
                             origin_context if 'origin_context' in locals() else None, course_category, course.get('id'))
            except Exception:
                pass
            await safe_edit_message(query, details, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
        else:
            await safe_edit_message(query, "Course not found. Please try again.", action_key=getattr(query, 'data', None))
    except Exception as e:
        logger.error(f"Error fetching course '{course_name}': {e}")
        await safe_edit_message(query, "An error occurred while fetching the course. Please try again later.", action_key=getattr(query, 'data', None))
        
# Main entry point for your bot (add handlers as needed)
async def get_courses_by_category(user_id, category, page: int = 1, page_size: int = 20):
    """Fetch courses by category with pagination."""
    db = await get_db()
    if db is None:
        return []

    try:
        # Fast path: use projection + $slice to pull only the requested
        # window of the embedded `courses` array. This avoids an
        # expensive `$unwind` over the whole array and is much faster for
        # large arrays when sorting by course name is not required.
        start = (page - 1) * page_size
        cache_key = f"page:category:{urllib.parse.quote_plus(str(category))}:{page}:{page_size}"
        # First check in-memory short cache
        cached = _get_cached_page(cache_key)
        logger.debug("get_courses_by_category: category=%s page=%s page_size=%s cache_key=%s cached_mem=%s", category, page, page_size, cache_key, bool(cached))
        if cached is not None:
            logger.debug("get_courses_by_category: returning cached page len=%d (mem)", len(cached) if hasattr(cached, '__len__') else 0)
            return cached
        # Try Redis-backed page cache (best-effort) to avoid DB read
        try:
            if _redis is not None:
                val = await _redis.get(cache_key)
                if val is not None:
                    try:
                        items = json.loads(val)
                        # warm in-memory cache and return
                        _set_cached_page(cache_key, items, ttl=PAGE_CACHE_TTL)
                        logger.debug("get_courses_by_category: returning cached page len=%d (redis)", len(items) if hasattr(items, '__len__') else 0)
                        return items
                    except Exception:
                        pass
        except Exception:
            pass

        async with _db_timing(f"get_courses_by_category:{category}:{page}"):
            try:
                # Try find_one with $slice projection (fast, single-doc read).
                # Match by either `name` or `path` because callbacks sometimes
                # encode the category `path` while other places use `name`.
                proj = {"courses": {"$slice": [start, page_size]}, "name": 1, "path": 1}
                doc = await db.categories.find_one({"$or": [{"name": category}, {"path": category}]}, projection=proj)
                if doc and isinstance(doc.get('courses'), list):
                    # Include the embedded course `id` (when present) and optional coach
                    items = [{
                        "id": (c.get('id') if isinstance(c, dict) else None),
                        "name": (c.get('name') if isinstance(c, dict) else None),
                        "link": (c.get('link') if isinstance(c, dict) else None),
                        "category": (doc.get('name') or doc.get('path')),
                        "coach": (c.get('coach') if isinstance(c, dict) else None),
                    } for c in doc.get('courses')]
                else:
                    items = []
                logger.debug("get_courses_by_category: fetched slice start=%s len=%s from category=%s", start, len(items), category)
            except Exception:
                items = []

        # Fallback: if we couldn't get items via $slice (e.g., need server-side
        # sort by course name), fall back to the unwind/skip/limit pipeline.
        if not items:
            try:
                items_pipeline = [
                    {"$match": {"$or": [{"name": category}, {"path": category}]}},
                    {"$unwind": "$courses"},
                    {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name", "id": "$courses.id", "coach": "$courses.coach"}},
                    {"$sort": {"name": 1}},
                    {"$skip": start},
                    {"$limit": page_size}
                ]
                try:
                    items = await db.categories.aggregate(items_pipeline).to_list(length=page_size)
                except Exception:
                    items = []
                logger.debug("get_courses_by_category: fallback unwind returned len=%s for category=%s", len(items), category)
            except Exception:
                items = []

        # Cache the page payload short-term to speed up rapid navigation.
        try:
            _set_cached_page(cache_key, items, ttl=PAGE_CACHE_TTL)
        except Exception:
            pass
        logger.debug("get_courses_by_category: final items_len=%s category=%s page=%s", len(items) if hasattr(items, '__len__') else 0, category, page)
        return items
    except Exception as e:
        logger.error(f"Error while fetching courses for category '{category}': {str(e)}")
        return []

async def courses_callback(update: Update, context: CallbackContext):
    """Handle the courses callback and display courses based on pagination."""
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    logger.debug("courses_callback invoked with data=%s", data)
    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return
    # pagination page size
    page_size = PAGE_SIZE

    try:
        # Support stored page refs: courses_ref::<key>
        if data.startswith('courses_ref::'):
            key = data.split('::', 1)[1]
            payload = await _resolve_callback_payload(key)
            if not payload:
                await safe_edit_message(query, "Reference expired. Please open the list again.", action_key=getattr(query, 'data', None))
                return
            # payload expected: type='courses_page', items, page, origin_type, category, origin_context, origin_context_page, total_count
            if payload.get('type') == 'courses_page':
                items = payload.get('items') or []
                page = int(payload.get('page', 1) or 1)
                origin_type = payload.get('origin_type') or 'global'
                category = payload.get('category')
                origin_ctx = payload.get('origin_context')
                origin_ctx_page = payload.get('origin_context_page')
                total_count = int(payload.get('total_count')) if payload.get('total_count') is not None else None

                # If the stored payload didn't include concrete page items, fetch them
                # server-side so the stored ref still works after restarts.
                if not items:
                    try:
                        if origin_type == 'global':
                            start = (page - 1) * page_size
                            items_pipeline = [
                                {"$unwind": "$courses"},
                                {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name"}},
                                {"$sort": {"name": 1}},
                                {"$skip": start},
                                {"$limit": page_size + 1}
                            ]
                            try:
                                items_result = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                            except Exception:
                                items_result = []
                            has_more = len(items_result) > page_size
                            items = items_result[:page_size]
                            items = sorted(items, key=lambda c: (c.get('name') or '').lower())
                            # Compute accurate total count for global listing where possible
                            try:
                                cnt_doc = await db.categories.aggregate([
                                    {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
                                    {"$group": {"_id": None, "count": {"$sum": "$n"}}}
                                ]).to_list(length=1)
                                total_count = int(cnt_doc[0].get('count')) if cnt_doc else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(items)))
                            except Exception:
                                total_count = page * page_size + 1 if has_more else ((page - 1) * page_size + len(items))

                        elif origin_type == 'category':
                            start = (page - 1) * page_size
                            items_pipeline = [
                                {"$match": {"$or": [{"name": category}, {"path": category}]}},
                                {"$unwind": "$courses"},
                                {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name"}},
                                {"$sort": {"name": 1}},
                                {"$skip": start},
                                {"$limit": page_size + 1}
                            ]
                            try:
                                items_result = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                            except Exception:
                                items_result = []
                            has_more = len(items_result) > page_size
                            items = items_result[:page_size]
                            items = sorted(items, key=lambda c: (c.get('name') or '').lower())
                            try:
                                total_count = await _get_courses_count(db, category, ttl=60)
                            except Exception:
                                total_count = page * page_size + 1 if has_more else ((page - 1) * page_size + len(items))

                        elif origin_type == 'coach':
                            coach_name = category
                            start = (page - 1) * page_size
                            items_pipeline = [
                                {"$match": {"courses.coach": coach_name}},
                                {"$unwind": "$courses"},
                                {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name", "coach": "$courses.coach"}},
                                {"$match": {"coach": coach_name}},
                                {"$sort": {"name": 1}},
                                {"$skip": start},
                                {"$limit": page_size + 1}
                            ]
                            try:
                                items_result = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                            except Exception:
                                items_result = []
                            has_more = len(items_result) > page_size
                            items = items_result[:page_size]
                            items = sorted(items, key=lambda c: (c.get('name') or '').lower())
                            try:
                                cnt_doc = await db.categories.aggregate([
                                    {"$match": {"courses.coach": coach_name}},
                                    {"$unwind": "$courses"},
                                    {"$match": {"courses.coach": coach_name}},
                                    {"$group": {"_id": None, "count": {"$sum": 1}}}
                                ]).to_list(length=1)
                                total_count = int(cnt_doc[0].get('count')) if cnt_doc else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(items)))
                            except Exception:
                                total_count = page * page_size + 1 if has_more else ((page - 1) * page_size + len(items))

                    except Exception:
                        # If server-side fetch fails, fall back to empty items so a helpful
                        # message is shown to the user rather than crashing.
                        items = []

                # rebuild the page; items are now the page slice
                text, reply_markup = build_courses_page(items, page=page, origin_type=origin_type, category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page, total_count=total_count, is_page=True, store_page_ref=False)
                if not text:
                    await safe_edit_message(query, "No courses found.", action_key=getattr(query, 'data', None))
                    return
                # Add Search button
                try:
                    kb = list(reply_markup.inline_keyboard)
                    if origin_type == 'category' and category:
                        kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}")])
                    else:
                        kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_courses::{origin_type}::{page}")])
                    reply_markup = InlineKeyboardMarkup(kb)
                except Exception:
                    pass
                await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                return

        # New format supports: courses::{category}::{page} or courses::{page} for global
        if data.startswith("courses::"):
            payload = data.replace("courses::", "", 1)
            parts = payload.split("::")

            # New explicit formats:
            #  - courses::global::<page>
            #  - courses::category::<category>::<page>
            #  - courses::coach::<coach_slug>::<page>
            # Legacy fallback: courses::<page> or courses::<category>::<page>
            try:
                if parts[0] in ("global", "category", "coach"):
                    kind = parts[0]
                    if kind == "global":
                        page = int(parts[1])
                        start = (page - 1) * page_size
                        items_pipeline = [
                            {"$unwind": "$courses"},
                            {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name"}},
                            {"$sort": {"name": 1}},
                            {"$skip": start},
                            {"$limit": page_size + 1}
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
                        all_courses = sorted(all_courses, key=lambda c: (c.get('name') or '').lower())
                        # Compute accurate total count for global listing when possible
                        try:
                            cnt_doc = await db.categories.aggregate([
                                {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
                                {"$group": {"_id": None, "count": {"$sum": "$n"}}}
                            ]).to_list(length=1)
                            total_courses = int(cnt_doc[0].get('count')) if cnt_doc else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses)))
                        except Exception:
                            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses))
                        text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global', total_count=total_courses, is_page=True)
                        if not text:
                            await safe_edit_message(query, f"No courses found on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        # Add Search button for all courses
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_courses::global::{page}")])
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return

                    if kind == "category":
                        category = urllib.parse.unquote_plus(parts[1])
                        page = int(parts[2])
                        # optional origin context suffix: ::from_parent::<origin_ctx>::<origin_page>
                        origin_ctx = None
                        origin_ctx_page = None
                        if len(parts) > 3:
                            try:
                                if parts[3] == 'from_parent' and len(parts) >= 6:
                                    origin_ctx = urllib.parse.unquote_plus(parts[4])
                                    try:
                                        origin_ctx_page = int(parts[5])
                                    except Exception:
                                        origin_ctx_page = None
                            except Exception:
                                origin_ctx = None
                        # Use aggregation to avoid loading the full category document
                        start = (page - 1) * page_size
                        # Match by either category `name` or `path` so stored
                        # callbacks that use full paths (e.g. "Sex/Kenneth Play")
                        # still resolve to the correct category document.
                        items_pipeline = [
                            {"$match": {"$or": [{"name": category}, {"path": category}]}},
                            {"$unwind": "$courses"},
                            {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name"}},
                            {"$sort": {"name": 1}},
                            {"$skip": start},
                            {"$limit": page_size + 1}
                        ]
                        try:
                            items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                        except Exception:
                            items = []
                        has_more = len(items) > page_size
                        courses = items[:page_size]
                        courses = sorted(courses, key=lambda c: (c.get('name') or '').lower())
                        # compute origin_context (parent path) — fetch parent path if we don't already have origin_ctx
                        if origin_ctx is None:
                            try:
                                # Resolve parent by matching name or path
                                pdoc = await db.categories.find_one({"$or": [{"name": category}, {"path": category}]}, projection={"parent": 1})
                                parent = pdoc.get('parent') if pdoc else None
                                if parent:
                                    pp = await db.categories.find_one({"$or": [{"name": parent}, {"path": parent}]}, projection={"path": 1})
                                    origin_ctx = pp.get('path') if pp and pp.get('path') else parent
                            except Exception:
                                origin_ctx = None
                        # Try cached count for the category when possible (faster and accurate)
                        try:
                            total_courses = await _get_courses_count(db, category, ttl=60)
                        except Exception:
                            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(courses))
                        text, reply_markup = build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page, total_count=total_courses, is_page=True, store_page_ref=True)
                        if not text:
                            await safe_edit_message(query, f"No courses found in category '{category}' on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        # Add Search button for courses in this category
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}")])
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return

                    if kind == "coach":
                        coach_slug = urllib.parse.unquote_plus(parts[1])
                        page = int(parts[2])
                        # derive coach courses using aggregation (server-side
                        # unwind + match) to avoid iterating over all categories
                        # in Python.
                        coach_name = coach_slug
                        # Fetch coach results with a small overfetch to detect Next page
                        start = (page - 1) * page_size
                        items_pipeline = [
                            {"$match": {"courses.coach": coach_name}},
                            {"$unwind": "$courses"},
                            {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name", "coach": "$courses.coach"}},
                            {"$match": {"coach": coach_name}},
                            {"$sort": {"name": 1}},
                            {"$skip": start},
                            {"$limit": page_size + 1}
                        ]
                        try:
                            items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                        except Exception:
                            items = []
                        has_more = len(items) > page_size
                        coach_courses = items[:page_size]
                        coach_courses = sorted(coach_courses, key=lambda c: (c.get('name') or '').lower())
                        # Compute accurate total for coach by aggregating when possible
                        try:
                            cnt_doc = await db.categories.aggregate([
                                {"$match": {"courses.coach": coach_name}},
                                {"$unwind": "$courses"},
                                {"$match": {"courses.coach": coach_name}},
                                {"$group": {"_id": None, "count": {"$sum": 1}}}
                            ]).to_list(length=1)
                            total_courses = int(cnt_doc[0].get('count')) if cnt_doc else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(coach_courses)))
                        except Exception:
                            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(coach_courses))
                        text, reply_markup = build_courses_page(coach_courses, page=page, origin_type='coach', category=coach_name, origin_context=None, total_count=total_courses, is_page=True, store_page_ref=True)
                        if not text:
                            await safe_edit_message(query, f"No courses found for coach '{coach_name}' on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        # Add Search button for courses by this coach
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_courses::coach::{urllib.parse.quote_plus(str(coach_name))}::{page}")])
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return
                else:
                    # legacy fallback handling
                    if len(parts) == 1:
                        page = int(parts[0])
                        # Legacy global fallback: fetch page_size+1 items to detect Next
                        start = (page - 1) * page_size
                        items_pipeline = [
                            {"$unwind": "$courses"},
                            {"$project": {"name": "$courses.name", "link": "$courses.link", "category": "$name"}},
                            {"$sort": {"name": 1}},
                            {"$skip": start},
                            {"$limit": page_size + 1}
                        ]
                        try:
                            items = await db.categories.aggregate(items_pipeline).to_list(length=page_size + 1)
                        except Exception:
                            items = []
                        has_more = len(items) > page_size
                        all_courses = items[:page_size]
                        all_courses = sorted(all_courses, key=lambda c: (c.get('name') or '').lower())
                        # Compute accurate total count for legacy global fallback when possible
                        try:
                            cnt_doc = await db.categories.aggregate([
                                {"$project": {"n": {"$size": {"$ifNull": ["$courses", []]}}}},
                                {"$group": {"_id": None, "count": {"$sum": "$n"}}}
                            ]).to_list(length=1)
                            total_courses = int(cnt_doc[0].get('count')) if cnt_doc else (page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses)))
                        except Exception:
                            total_courses = page * page_size + 1 if has_more else ((page - 1) * page_size + len(all_courses))
                        text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global', origin_context=None, total_count=total_courses, is_page=True)
                        if not text:
                            await safe_edit_message(query, f"No courses found on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        # Add Search button for all courses
                        try:
                            kb = list(reply_markup.inline_keyboard)
                            kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_courses::global::{page}")])
                            reply_markup = InlineKeyboardMarkup(kb)
                        except Exception:
                            pass
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return
                    # legacy category + page
                    category = urllib.parse.unquote_plus(parts[0])
                    try:
                        page = int(parts[1])
                    except Exception:
                        await safe_edit_message(query, "Invalid page number.", action_key=getattr(query, 'data', None))
                        return
                    # legacy category pagination may not include origin; but
                    # support optional ::from_parent::<origin_ctx>::<origin_page>
                    origin_ctx = None
                    origin_ctx_page = None
                    if len(parts) > 2:
                        # parts[2] might be 'from_parent' in legacy fallback
                        try:
                            if parts[2] == 'from_parent' and len(parts) >= 5:
                                origin_ctx = urllib.parse.unquote_plus(parts[3])
                                try:
                                    origin_ctx_page = int(parts[4])
                                except Exception:
                                    origin_ctx_page = None
                        except Exception:
                            origin_ctx = None
                    category_doc = await db.categories.find_one({"name": category})
                    if not category_doc or not category_doc.get('courses'):
                        await safe_edit_message(query, f"No courses found in category '{category}' on page {page}.", action_key=getattr(query, 'data', None))
                        return
                    courses = category_doc.get('courses', [])
                    courses = sorted(courses, key=lambda c: (c.get('name') or '').lower())
                    # If origin_ctx not provided by callback, resolve parent path
                    if origin_ctx is None:
                        try:
                            parent = category_doc.get('parent')
                            if parent:
                                pdoc = await db.categories.find_one({"name": parent})
                                origin_ctx = pdoc.get('path') if pdoc and pdoc.get('path') else parent
                        except Exception:
                            origin_ctx = None
                    text, reply_markup = build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx, origin_context_page=origin_ctx_page)
                    if not text:
                        await safe_edit_message(query, f"No courses found in category '{category}' on page {page}.", action_key=getattr(query, 'data', None))
                        return
                    # Add Search button for courses in this category
                    try:
                        kb = list(reply_markup.inline_keyboard)
                        kb.append([InlineKeyboardButton("\U0001f50d Search", callback_data=f"search_category_courses::{urllib.parse.quote_plus(str(category))}::{page}")])
                        reply_markup = InlineKeyboardMarkup(kb)
                    except Exception:
                        pass
                    await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                    return
            except Exception as e:
                logger.error(f"Error parsing courses callback: {e}")
                await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, 'data', None))
                return

        # legacy underscore format removed. Only `courses::` callbacks are supported.
        await safe_edit_message(query, "Invalid pagination callback.", action_key=getattr(query, 'data', None))
        return
    except Exception as e:
        logger.error(f"Error handling courses callback: {e}")
        await safe_edit_message(query, "An error occurred while fetching courses. Please try again later.", action_key=getattr(query, 'data', None))
