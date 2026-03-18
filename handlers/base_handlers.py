from telegram.ext import CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, CallbackContext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import logging
from conversation_states import CREATE_CAT_NAME, CREATE_CAT_PARENT
from handlers.db_connection import get_db  # Importing get_db from db_connection.py
from database.mongo_handler import MongoDB  # Import MongoDB
import re  # For URL validation
from pymongo.errors import DuplicateKeyError
import urllib.parse
import os
from datetime import datetime, timedelta
import hashlib
import json
import time
import asyncio
import os
# How long to persist callback refs (seconds). Default: 7 days.
CALLBACK_REF_TTL = int(os.getenv("CALLBACK_REF_TTL", str(7 * 24 * 3600)))
import math
from telegram.error import RetryAfter, BadRequest

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
logger.info("GUI_SESSION_TTL=%s seconds (env=%r)", GUI_SESSION_TTL, os.getenv("GUI_SESSION_TTL"))


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
def _make_course_ref(category: str, name: str, origin_type: str, origin_page: int, origin_context: str = None) -> str:
    # Compute a concrete back callback so details can always return to the
    # exact originating UI (category/coach/global) without guessing.
    if origin_type == 'category':
        target = origin_context or category
        back_cb = f"courses::category::{urllib.parse.quote_plus(str(target))}::{origin_page}"
    elif origin_type == 'coach':
        target = origin_context or category
        back_cb = f"courses::coach::{urllib.parse.quote_plus(str(target))}::{origin_page}"
    else:
        back_cb = f"courses::global::{origin_page}"

    payload = {
        "category": category,
        "name": name,
        "origin_type": origin_type,
        "origin_page": origin_page,
        "origin_context": origin_context,
        "back_cb": back_cb,
    }
    # Use the central storage helper so refs are persisted (Redis/Mongo) as a best-effort.
    key = _store_callback_payload(payload)
    # Append an encoded back callback to the returned callback_data so the
    # Details view can use it directly without resolving the stored payload.
    try:
        enc = urllib.parse.quote_plus(back_cb)
        candidate = f"course_ref::{key}::back::{enc}"
        # Telegram callback_data must be <= 64 bytes. Don't append the
        # back token if it would exceed that limit; fall back to stored ref.
        if len(candidate.encode('utf-8')) <= 64:
            return candidate
        else:
            logger.debug("_make_course_ref: back token omitted due to length (%d bytes)", len(candidate.encode('utf-8')))
            return f"course_ref::{key}"
    except Exception:
        return f"course_ref::{key}"


def _store_callback_payload(payload: dict) -> str:
    """Store an arbitrary payload and return a short key."""
    key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    CALLBACK_MAP[key] = payload
    # best-effort background persist to Redis or Mongo so refs survive restarts
    try:
        asyncio.create_task(_persist_callback_payload(key, payload))
    except Exception:
        pass
    return key


async def _persist_callback_payload(key: str, payload: dict, ttl: int = 60 * 60 * 24 * 7):
    """Persist callback payload to Redis (preferred) or MongoDB (fallback).
    TTL defaults to 7 days.
    """
    # Try Redis
    try:
        if _redis is not None:
            import json as _json
            await _redis.set(f"callback:ref:{key}", _json.dumps(payload), ex=ttl)


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
    # In-memory first
    payload = CALLBACK_MAP.get(key)
    if payload:
        return payload

    # Redis
    try:


        if _redis is not None:
            val = await _redis.get(f"callback:ref:{key}")
            if val:
                import json as _json
                payload = _json.loads(val)
                # repopulate in-memory cache for speed
                CALLBACK_MAP[key] = payload
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
                return payload
    except Exception:
        logger.error("Failed to read callback payload from MongoDB")

    return None

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

def _refill_bucket(bucket):
    now = time.time()
    elapsed = now - bucket.get("last_refill", now)
    if elapsed <= 0:
        return
    bucket["tokens"] = min(bucket["capacity"], bucket.get("tokens", bucket["capacity"]) + elapsed * bucket["refill_rate"])
    bucket["last_refill"] = now

def _consume_token(user_id: int, cost: float = 1.0):
    # If Redis is configured, prefer the Redis-backed atomic token bucket.
    if _redis is not None and _redis_token_script is not None:
        try:
            now = int(time.time())
            # global first
            res = _redis.eval(_redis_token_script, 1, 'bucket:global', now, _GLOBAL_BUCKET['capacity'], _GLOBAL_BUCKET['refill_rate'], cost)
            # res is JSON like [1,0] or [0,wait]
            import json as _json
            ok, wait = _json.loads(res)
            if ok == 1:
                # now consume user bucket
                user_key = f"bucket:user:{user_id}"
                res2 = _redis.eval(_redis_token_script, 1, user_key, now, 5.0, 1.0, cost)
                ok2, wait2 = _json.loads(res2)
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
        import json as _json
        await _redis.zadd("retry:queue", { _json.dumps(payload): execute_at })
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
        payload = _json.loads(raw_member)
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
        logger.info("Redis not configured; skipping redis retry worker")
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
        ok, wait = _consume_token(uid)
        if not ok:
            logger.info("Rate limit: scheduling retry in %s seconds for key=%s", wait, key)
            await schedule_retry_via_redis_or_local(query, text, reply_markup=reply_markup, delay=wait)
            try:
                await safe_answer(query, text=f"Too many requests. Retrying in {wait}s.")
            except Exception:
                pass
            return False

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


def build_courses_page(all_courses, page: int = 1, origin_type: str = 'global', category: str = None, origin_context: str = None):
    """Builds the text and InlineKeyboardMarkup for a courses page.

    Returns (text, InlineKeyboardMarkup) or (None, None) when no items.
    """
    if not all_courses:
        return None, None
    page_size = PAGE_SIZE
    start = (page - 1) * page_size
    display = all_courses[start:start + page_size]
    if not display:
        return None, None

    logger.debug("build_courses_page called: origin_type=%s category=%s page=%s total=%s", origin_type, category, page, len(all_courses) if hasattr(all_courses, '__len__') else 'unknown')
    if origin_type == 'category' and category:
        text = f"Courses in category '{category}' (page {page}):"
        breadcrumb = ["Home", "categories", category]
    elif origin_type == 'coach' and category is None:
        text = f"Courses (page {page}):"
        breadcrumb = ["Home", "coaches"]
    else:
        text = f"Here are the available courses (page {page}):"
        breadcrumb = ["Home", "courses"]

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
            details_cb = _make_course_ref(course_cat, name, origin_type, page)
            keyboard.append([
                InlineKeyboardButton(name, url=link),
                InlineKeyboardButton("ℹ️ Details", callback_data=details_cb)
            ])
        except Exception as e:
            logger.error("build_courses_page: error building row for course %s: %s", repr(c), e)
            continue

    # Pagination controls (Previous / Next)
    pagination_buttons = []
    if start > 0:
        if origin_type == 'category' and category:
            prev_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page-1}"
        elif origin_type == 'coach' and category:
            prev_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{page-1}"
        else:
            prev_cb = f"courses::global::{page-1}"
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=prev_cb))
    if len(all_courses) > start + page_size:
        if origin_type == 'category' and category:
            next_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{page+1}"
        elif origin_type == 'coach' and category:
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
        display = all_courses[start:start + max_course_rows]
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
                details_cb = _make_course_ref(course_cat, name, origin_type, page)
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
        if len(all_courses) > start + len(display):
            next_cb = f"courses::global::{page+1}" if origin_type == 'global' else next_cb
            pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=next_cb))
        if pagination_buttons:
            keyboard.append(pagination_buttons)

    # Compute total pages for End button placement
    try:
        total_pages = math.ceil(len(all_courses) / page_size)
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
        if origin_type == 'global':
            home_cb = f"courses::global::1"
        elif origin_type == 'coach' and category:
            home_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::1"
        else:
            # Default Home goes to the top-level categories view
            home_cb = "back_to_cats"
        breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data=home_cb)]
        if total_pages > 1 and page < total_pages:
            # build end callback depending on origin type
            if origin_type == 'category' and category:
                end_cb = f"courses::category::{urllib.parse.quote_plus(category)}::{total_pages}"
            elif origin_type == 'global':
                end_cb = f"courses::global::{total_pages}"
            elif origin_type == 'coach' and category:
                end_cb = f"courses::coach::{urllib.parse.quote_plus(category)}::{total_pages}"
            else:
                end_cb = f"courses::global::{total_pages}"
            breadcrumb_buttons.append(InlineKeyboardButton("⏭️ End", callback_data=end_cb))
        # insert breadcrumb row (Home +/- End); omit the current category button
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
                    back_cb = f"showcat::{urllib.parse.quote_plus(str(target))}"
                else:
                    back_cb = "back_to_cats"
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
    
async def help(update: Update, context: CallbackContext):
    """Customized Help message."""
    help_message = (
        "✨ **Welcome to the Course Manager Bot!** Here's how you can use me:\n\n"
        "/start - Start the bot and receive a welcome message\n"
        "/add - Start the process of adding a new course\n"
        "/courses - View your saved courses\n"
        "/delete_course - Delete a specific course\n"
        "/delete_category - Delete a coach/child category and its courses (not parent folders)\n"
        "/delete_all_data - Deletes both courses and categories (don't use this lightly!)\n\n"
        "📚 **Category Management**:\n"
        "/categories - List all available categories\n"
        "/create_category - Create a new empty category\n\n"
        "🎨 **Course Thumbnail Management**:\n"
        "/addthumb - Add a custom thumbnail for a course\n"
        "/delthumb - Delete a custom thumbnail for a course\n\n"
        "⚙️ **Other Commands**:\n"
        "/help - Displays this help message\n"
        "/cancel - Cancel the current operation\n\n"
        "⚠️ **Important Note**: Be careful with the commands that delete categories or courses! Once deleted, they can't be recovered."
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

        cats = await db.categories.find({"parent": {"$exists": False}}).to_list(length=None)
    except Exception:
        cats = []

    cats = sorted(cats, key=lambda c: (c.get('name') or '').lower())
    page_size = PAGE_SIZE
    start = (page - 1) * page_size
    end = start + page_size
    page_cats = cats[start:end]

    if not page_cats and not is_query:
        await update_or_message.reply_text("No categories available. Use /create_category to create one.")
        return

    keyboard = []
    # Provide explicit Top-level option
    keyboard.append([InlineKeyboardButton("(Top-level)", callback_data=f"createcat_parent::")])
    for cat in page_cats:
        keyboard.append([InlineKeyboardButton(cat.get('name'), callback_data=f"createcat_parent::{urllib.parse.quote_plus(cat.get('name'))}")])

    nav = []
    total_pages = (len(cats) - 1) // page_size + 1 if cats else 1
    last_page = max(1, total_pages)
    # Prev (left)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"createcat_page::{page-1}"))
    # Home (center)
    nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"createcat_page::1"))
    # Next (right)
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"createcat_page::{page+1}"))
    # End (always rightmost when multiple pages)
    if total_pages > 1:
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

        children = await db.categories.find({"parent": parent}).to_list(length=None)
    except Exception:
        children = []

    children = sorted(children, key=lambda c: (c.get('name') or '').lower())
    page_size = PAGE_SIZE
    start = (page - 1) * page_size
    end = start + page_size
    page_children = children[start:end]

    if not page_children:
        if is_query:
            await safe_edit_message(query, "No subcategories available on this page.", action_key=getattr(query, 'data', None))
        else:
            await update_or_message.reply_text("No subcategories available.")
        return

    keyboard = [[InlineKeyboardButton(child.get('name'), callback_data=f"showcat::{urllib.parse.quote_plus(child.get('path') or child.get('name'))}::{page}")] for child in page_children]

    nav = []
    total_pages = (len(children) - 1) // page_size + 1 if children else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"showcat::{urllib.parse.quote_plus(parent)}::{page-1}"))
    nav.append(InlineKeyboardButton("🏠 Home", callback_data=f"showcat::{urllib.parse.quote_plus(parent)}::1"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"showcat::{urllib.parse.quote_plus(parent)}::{page+1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"showcat::{urllib.parse.quote_plus(parent)}::{last_page}"))
    if nav:
        keyboard.append(nav)

    # Up button to parent view
    pdoc = await db.categories.find_one({"name": parent})
    ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
    keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=f"showcat::{urllib.parse.quote_plus(ppath)}::{page}")])

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

        cats = await db.categories.find({"parent": {"$exists": False}}).to_list(length=None)
    except Exception:
        cats = []

    cats = sorted(cats, key=lambda c: (c.get('name') or '').lower())
    page_size = PAGE_SIZE
    start = (page - 1) * page_size
    end = start + page_size
    page_cats = cats[start:end]

    if not page_cats:
        if is_query:
            await safe_edit_message(query, "No categories available on this page.", action_key=getattr(query, 'data', None))
        else:
            await update_or_message.reply_text("No categories available. Use /create_category to create one.")
        return

    keyboard = [[InlineKeyboardButton(cat.get('name'), callback_data=f"showcat::{urllib.parse.quote_plus(cat.get('path') or cat.get('name'))}")] for cat in page_cats]

    nav = []
    total_pages = (len(cats) - 1) // page_size + 1 if cats else 1
    last_page = max(1, total_pages)
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"categories_page::{page-1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"categories_page::{page+1}"))
        nav.append(InlineKeyboardButton("⏭️ End", callback_data=f"categories_page::{last_page}"))
    if nav:
        keyboard.append(nav)

    # Breadcrumb / Home row
    try:
        breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data="back_to_cats")]
        keyboard.insert(0, breadcrumb_buttons)
    except Exception:
        pass

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


async def list_coaches(update: Update, context: CallbackContext):
    """Redirect /coaches to the categories view so users browse: categories -> coaches -> courses."""
    # Present categories first (so coaches are shown inside a category)
    await list_categories(update, context)


async def show_coach_handler(update: Update, context: CallbackContext):
    """Show courses for a selected coach. Supports coaches stored in a `coaches` collection or derived from categories/courses."""
    query = update.callback_query
    await safe_answer(query)
    encoded = query.data.split("_", 1)[1]
    coach_slug = urllib.parse.unquote_plus(encoded)

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
        cats = await db.categories.find().to_list(length=None)
        coach_courses = []
        for cat in cats:
            for crs in cat.get('courses', []):
                if crs.get('coach'):
                    if crs.get('coach') == coach_name:
                        coach_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})
                else:
                    # legacy: category name represents coach
                    if (cat.get('name') or '') == coach_name:
                        coach_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})

        # Sort and render using existing helper
        coach_courses = sorted(coach_courses, key=lambda c: (c.get('name') or '').lower())
        text, reply_markup = build_courses_page(coach_courses, page=1, origin_type='coach', category=coach_name, origin_context=None)
        if not text:
            await safe_edit_message(query, f"No courses found for coach '{coach_name}'.", action_key=getattr(query, 'data', None))
            return
        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    except Exception as e:
        logger.error("Error showing coach courses: %s", e)
        await safe_edit_message(query, "An unexpected error occurred. Please try again later.", action_key=getattr(query, 'data', None))


async def show_coach_in_category(update: Update, context: CallbackContext):
    """Handle coach selection within a specific category: coach_in_cat::{category}::{coach_slug}"""
    query = update.callback_query
    await safe_answer(query)
    data = query.data
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

        text, reply_markup = build_courses_page(coach_courses, page=page, origin_type='category', category=category, origin_context=origin_ctx)
        if not text:
            await safe_edit_message(query, f"No courses found for coach '{coach_name}' in '{category}'.", action_key=getattr(query, 'data', None))
            return
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

        if not text:
            await safe_edit_message(
                query,
                f"No courses found for type '{type_name}' in '{category_name}'.",
                action_key=getattr(query, 'data', None)
            )
            return

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
        
async def showcat_handler(update: Update, context: CallbackContext):
    """Show courses in the chosen category as URL buttons."""
    query = update.callback_query
    await safe_answer(query)
    # Expect callback_data: showcat::{path_or_name} or showcat::{path_or_name}::{page}
    parts = query.data.split("::")
    encoded = parts[1] if len(parts) > 1 else ""
    # If a page suffix was included, parts[2] will contain it — keep it available
    page_from_callback = None
    if len(parts) > 2:
        try:
            page_from_callback = int(parts[2])
        except Exception:
            page_from_callback = None
    # Current page for this category view (used when linking to coaches)
    page = page_from_callback or 1
    cat_path = urllib.parse.unquote_plus(encoded)
    db = await get_db()
    # Try to resolve by `path` first, then by `name` for legacy docs
    category_doc = await db.categories.find_one({"path": cat_path})
    if not category_doc:
        category_doc = await db.categories.find_one({"name": cat_path})
    if not category_doc:
        await safe_edit_message(query, f'Category “{cat_path}” not found.', action_key=getattr(query, 'data', None))
        return

    # category display name and path
    cat_name = category_doc.get('name')
    cat_path = category_doc.get('path') or cat_name
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
            coaches = await db.coaches.find({"topics": cat_name}).to_list(length=None)
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
        children = await db.categories.find({"parent": cat_name}).to_list(length=None)
    except Exception:
        children = []

    if children:
        # Paginate child categories when there are many.
        # Use any page parsed earlier from the callback (page_from_callback)
        page = page_from_callback or 1

        # sort children deterministically
        sorted_children = sorted(children, key=lambda c: (c.get('name') or '').lower())
        page_size = PAGE_SIZE
        start = (page - 1) * page_size
        end = start + page_size
        page_children = sorted_children[start:end]

        keyboard = []
        for child in page_children:
            child_path = child.get('path') or child.get('name')
            keyboard.append([InlineKeyboardButton(child.get('name'), callback_data=f"showcat::{urllib.parse.quote_plus(child_path)}::{page}")])

        # Navigation row (Previous / End / Next)
        nav = []
        total_pages = (len(sorted_children) - 1) // page_size + 1 if sorted_children else 1
        last_page = max(1, total_pages)
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"showcat::{urllib.parse.quote_plus(cat_path)}::{page-1}"))

        # Put End between Prev and Next; if on first page, place End at the left
        if total_pages > 1:
            end_btn = InlineKeyboardButton("⏭️ End", callback_data=f"showcat::{urllib.parse.quote_plus(cat_name)}::{last_page}")
            if page == 1:
                nav.insert(0, end_btn)
            else:
                nav.append(end_btn)

        if len(sorted_children) > end:
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"showcat::{urllib.parse.quote_plus(cat_path)}::{page+1}"))

        if nav:
            keyboard.append(nav)

        # Breadcrumb / Home row (insert at top for context)
        try:
            breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data="back_to_cats")]
            keyboard.insert(0, breadcrumb_buttons)
        except Exception:
            pass

        # add up/back button to parent or top-level at the bottom for convenience
        parent = category_doc.get('parent')
        if parent:
            pdoc = await db.categories.find_one({"name": parent})
            ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
            keyboard.append([InlineKeyboardButton("🔙 Up", callback_data=f"showcat::{urllib.parse.quote_plus(ppath)}::{page}")])
        else:
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])

        await safe_edit_message(query, f"{cat_path} — Subcategories (page {page}/{last_page}):", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
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
        # Back to this category view
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"showcat::{urllib.parse.quote_plus(cat_path)}")])
        await safe_edit_message(query, f"{cat_name} — Select a type:", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
        return

    # If we found coaches, show them; otherwise fall back to showing courses in this category
    if coaches:
        # Include the current category page in the coach callback so we can
        # return the user to the same page after viewing details.
        keyboard = [[InlineKeyboardButton(coach.get('name'), callback_data=f"coach_in_cat::{urllib.parse.quote_plus(cat_name)}::{coach.get('slug') or urllib.parse.quote_plus(coach.get('name'))}::{page}")] for coach in coaches]
        # Back to this category view
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"showcat::{urllib.parse.quote_plus(cat_path)}")])
        await safe_edit_message(query, f"Coaches in '{cat_name}':", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
        return

    # Fallback: show courses if no coaches found
    courses = category_doc.get('courses', [])
    logger.info("showcat_handler: category=%s courses_count=%s", cat_name, len(courses))
    if not courses:
        # Offer a Back button so the user stays in the browsing flow instead
        # of being dropped out with a plain message.
        parent = category_doc.get('parent')
        keyboard = []
        if parent:
            pdoc = await db.categories.find_one({"name": parent})
            ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"showcat::{urllib.parse.quote_plus(ppath)}")])
        else:
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])

        await safe_edit_message(
            query,
            f'Category “{cat_name}” is empty.\nUse /add to populate it.',
            reply_markup=InlineKeyboardMarkup(keyboard),
            action_key=getattr(query, 'data', None),
        )
        return
    courses = sorted(courses, key=lambda c: (c.get('name') or '').lower())
    # Use paginated courses view for this category. Pass parent path as
    # origin_context so Home will return to the parent directory.
    page = page_from_callback or 1
    parent = category_doc.get('parent')
    origin_ctx = None
    if parent:
        try:
            pdoc = await db.categories.find_one({"name": parent})
            origin_ctx = pdoc.get('path') if pdoc and pdoc.get('path') else parent
        except Exception:
            origin_ctx = parent

    text, reply_markup = build_courses_page(courses, page=page, origin_type='category', category=cat_name, origin_context=origin_ctx)
    if not text:
        await safe_edit_message(query, f"No courses found in '{cat_name}' on page {page}.", action_key=getattr(query, 'data', None))
        return
    await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))


async def handle_back_to_cats(update: Update, context: CallbackContext):
    """Handle the 🔙 Back callback and show the categories list."""
    query = update.callback_query
    await safe_answer(query)
    try:
        db = await get_db()
        # list only top-level categories (no parent)
        categories = await db.categories.find({"parent": {"$exists": False}}).to_list(length=None)
        # Ensure deterministic, case-insensitive A→Z ordering for display
        categories = sorted(categories, key=lambda c: (c.get('name') or '').lower())
        if not categories:
            await safe_edit_message(query, "No categories available. Use /create_category to create one.", action_key=getattr(query, 'data', None))
            return
        keyboard = [
            [InlineKeyboardButton(cat["name"], callback_data=f"showcat::{urllib.parse.quote_plus(cat.get('path') or cat.get('name'))}")]
            for cat in categories
        ]
        await safe_edit_message(query, "Tap a category to see its courses:", reply_markup=InlineKeyboardMarkup(keyboard), action_key=getattr(query, 'data', None))
    except Exception as e:
        logger.error(f"Error returning to categories: {e}")
        await safe_edit_message(query, "An unexpected error occurred. Please try again later.", action_key=getattr(query, 'data', None))

async def list_courses(update: Update, context: CallbackContext):
    """List all available courses with pagination."""
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        # Build a flattened list of courses from all categories
        page = 1
        page_size = PAGE_SIZE
        cats = await db.categories.find().to_list(length=None)
        all_courses = []
        for cat in cats:
            for crs in cat.get('courses', []):
                all_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})

        # Sort all courses case-insensitively A→Z for deterministic ordering
        all_courses = sorted(all_courses, key=lambda c: (c.get('name') or '').lower())

        if all_courses:
            # Build unified page UI
            text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global', origin_context=None)
            if not text:
                await update.message.reply_text("No courses available.")
                return
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

async def list_courses_by_category(update: Update, context: CallbackContext, category_name: str, page: int = 1):
    """List courses in a specific category with pagination."""
    db = await get_db()
    if db is None:
        await update.message.reply_text("Error: Unable to connect to the database.")
        return

    try:
        # Paginate over the embedded courses array inside the category document
        page_size = PAGE_SIZE
        category_doc = await db.categories.find_one({"name": category_name})
        if not category_doc or not category_doc.get('courses'):
            await update.message.reply_text(f"No courses found in category '{category_name}'.")
            return

        courses = category_doc.get('courses', [])
        # Ensure deterministic, case-insensitive A→Z ordering for pagination/display
        courses = sorted(courses, key=lambda c: (c.get('name') or '').lower())
        start = (page - 1) * page_size
        display = courses[start:start + page_size]

        keyboard = [
            [
                InlineKeyboardButton(course['name'], url=course.get('link')),
                InlineKeyboardButton("ℹ️ Details", callback_data=_make_course_ref(category_name, course['name'], 'category', page))
            ]
            for course in display
        ]

        pagination_buttons = []
        if start > 0:
            prev_cb = f"courses::category::{urllib.parse.quote_plus(category_name)}::{page-1}"
            pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=prev_cb))
        if len(courses) > start + page_size:
            next_cb = f"courses::category::{urllib.parse.quote_plus(category_name)}::{page+1}"
            pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=next_cb))
        if pagination_buttons:
            keyboard.append(pagination_buttons)
        # Compute total pages and add breadcrumb row with Home and End when applicable
        try:
            total_pages = math.ceil(len(courses) / page_size)
        except Exception:
            total_pages = page

        try:
            breadcrumb_buttons = [InlineKeyboardButton("🏠 Home", callback_data="back_to_cats")]
            if total_pages > 1 and page < total_pages:
                end_cb = f"courses::category::{urllib.parse.quote_plus(category_name)}::{total_pages}"
                breadcrumb_buttons.append(InlineKeyboardButton("⏭️ End", callback_data=end_cb))
            breadcrumb_buttons.append(InlineKeyboardButton(category_name, callback_data=f"showcat::{urllib.parse.quote_plus(category_name)}"))
            keyboard.insert(0, breadcrumb_buttons)
        except Exception:
            pass

        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await update.message.reply_text(f"Courses in category '{category_name}' (page {page}):", reply_markup=reply_markup)
        try:
            schedule_close_inline_message(msg)
        except Exception:
            pass
        
    except Exception as e:
        logger.error(f"Error listing courses for category '{category_name}': {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
        
# legacy underscore-format pagination handler removed; modern `courses::` callbacks are used

async def handle_categories_pagination(update: Update, context: CallbackContext):
    query = update.callback_query
    await safe_answer(query)

    # Extract the action and page number from the callback data
    # For this deployment we prefer a full (non-paginated) categories list.
    # Redirect to the full `list_categories` view instead of DB-side pagination.
    await list_categories(update, context)
    return

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return

    try:
        collection = db['categories']

        # Use DB-side pagination: fetch page_size+1 items starting at the
        # requested offset. This avoids loading the entire collection into
        # memory (which caused lag) while still allowing arbitrary page
        # numbers. We sort by name for deterministic ordering.
        page_size = PAGE_SIZE
        start = (page - 1) * page_size

        # Fetch one extra document as a lookahead to decide whether a "Next"
        # button is needed.
        cursor = collection.find().sort("name", 1).skip(start).limit(page_size + 1)
        results = await cursor.to_list(length=None)

        if not results:
            # If no results for this page, inform the user (they may have
            # navigated past the end).
            await safe_edit_message(query, "No categories available.", action_key=getattr(query, 'data', None))
            return

        display = results[:page_size]

        keyboard = [
            [InlineKeyboardButton(category['name'], callback_data=f"category_{urllib.parse.quote_plus(category['name'])}")]
            for category in display
        ]

        pagination_buttons = []
        if start > 0:
            pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"categories_prev_{page-1}"))
        if len(results) > page_size:
            pagination_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"categories_next_{page+1}"))
        if pagination_buttons:
            keyboard.append(pagination_buttons)

        reply_markup = InlineKeyboardMarkup(keyboard)
        await safe_edit_message(query, "Here are the available categories:", reply_markup=reply_markup, action_key=getattr(query, 'data', None))
    except Exception as e:
        logger.error(f"Error handling pagination: {e}")
        await safe_edit_message(query, "An error occurred while fetching categories. Please try again later.", action_key=getattr(query, 'data', None))

logger.info(f"[STATE] returning {CREATE_CAT_NAME=} id={id(CREATE_CAT_NAME)}")
async def create_category(update: Update, context: CallbackContext):
    # Present existing categories as optional parents
    db = await get_db()
    cats = []
    try:
        # Show only top-level parent categories for parent selection
        cats = await db.categories.find({"parent": {"$exists": False}}).sort("name", 1).to_list(length=None)
    except Exception:
        cats = []

    # Use paginated createcat_page for consistent viewing
    try:
        await createcat_page(update.message, context, page=1)
    except Exception:
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
    encoded = query.data.split("::", 1)[1]
    parent = urllib.parse.unquote_plus(encoded) if encoded else None
    # Store chosen parent in user_data for the following name prompt
    context.user_data['new_cat_parent'] = parent
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
    # mark that the new category should be top-level
    context.user_data['new_cat_parent'] = None
    await update.message.reply_text("Enter the new parent category name:")
    return CREATE_CAT_NAME
    
async def handle_category_name(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    category_name = update.message.text.strip()
    logger.info(f"[CAT-INSERT-START] name={category_name!r} uid={user_id}")

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
        doc = {"name": category_name, "created_by": user_id}
        if parent:
            # try to resolve parent's path if present
            parent_doc = await db.categories.find_one({"name": parent})
            parent_path = parent_doc.get('path') if parent_doc and parent_doc.get('path') else parent
            doc['parent'] = parent
            doc['path'] = f"{parent_path}/{category_name}"

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
            if not parent:
                # Show top-level categories using paginated view
                await createcat_page(update.message, context, page=1)
            else:
                # Show paginated children of the parent including the newly created category
                await children_page(update.message, context, parent, page=1)
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
    db = await get_db()
    # resolve by path then name
    category_doc = await db.categories.find_one({"path": cat_path})
    if not category_doc:
        category_doc = await db.categories.find_one({"name": cat_path})
    if not category_doc or not category_doc.get('courses'):
        await safe_edit_message(query, f'Category “{cat_name}” is empty.\nUse /add to populate it.', action_key=getattr(query, 'data', None))
        return

    # every button is a url button → opens the link immediately
    courses = category_doc.get('courses', [])
    keyboard = [
        [InlineKeyboardButton(crs["name"], url=crs["link"])]
        for crs in courses
    ]
    # Delete is only available from the course Details view.
    parent = category_doc.get('parent')
    if parent:
        pdoc = await db.categories.find_one({"name": parent})
        ppath = pdoc.get('path') if pdoc and pdoc.get('path') else parent
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"showcat::{urllib.parse.quote_plus(ppath)}")])
    else:
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_cats")])
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
        if not payload:
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
        origin_type = payload.get("origin_type")
        try:
            origin_page = int(payload.get("origin_page", 1))
        except Exception:
            origin_page = 1
        # Prefer an explicit appended back token, otherwise fall back to saved payload
        saved_back_cb = appended_back or payload.get('back_cb')
    else:
        # Expect callback format: course::{category}::{course}
        data = data.replace("course::", "", 1)
        # Extract optional origin info appended as `::from::{origin_type}::{page}`
        if "::from::" in data:
            data, from_part = data.rsplit("::from::", 1)
            try:
                origin_type, origin_page_s = from_part.split("::", 1)
                origin_page = int(origin_page_s)
            except Exception:
                origin_type = None
                origin_page = 1

        parts = data.split("::", 1)
        if len(parts) == 2:
            encoded_cat, encoded_course = parts
            cat_name = urllib.parse.unquote_plus(encoded_cat)
            course_name = urllib.parse.unquote_plus(encoded_course)
        else:
            cat_name = None
            course_name = urllib.parse.unquote_plus(data)

    db = await get_db()
    if db is None:
        await safe_edit_message(query, "Error: Unable to connect to the database.", action_key=getattr(query, 'data', None))
        return

    try:
        course = None
        if cat_name:
            category_doc = await db.categories.find_one({"name": cat_name})
            if category_doc:
                for crs in category_doc.get('courses', []):
                    if crs.get('name') == course_name:
                        course = {"name": crs.get('name'), "link": crs.get('link'), "category": cat_name}
                        break
        else:
            # search across categories
            cats = await db.categories.find().to_list(length=None)
            for cat in cats:
                for crs in cat.get('courses', []):
                    if crs.get('name') == course_name:
                        course = {"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')}
                        break
                if course:
                    break

        if course:
            # Determine canonical category for this course (prefer explicit field)
            course_category = course.get('category') if isinstance(course, dict) else None
            logger.debug("handle_course_selection: origin_type=%s origin_page=%s course_category=%s cat_name=%s", origin_type, origin_page, course_category, cat_name)
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

            # Build Back callback: prefer explicit saved back_cb, otherwise
            # compute from origin_type/origin_page as a fallback.
            if saved_back_cb:
                back_cb = saved_back_cb
            else:
                if origin_type == 'category' and origin_page:
                    back_target = course_category or cat_name or '1'
                    back_cb = f"courses::category::{urllib.parse.quote_plus(str(back_target))}::{origin_page}"
                    logger.debug("handle_course_selection: computed back_cb=%s", back_cb)
                elif origin_type == 'coach' and origin_page:
                    back_target = course_category or cat_name or '1'
                    back_cb = f"courses::coach::{urllib.parse.quote_plus(str(back_target))}::{origin_page}"
                    logger.debug("handle_course_selection: computed back_cb=%s", back_cb)
                elif origin_type == 'global' and origin_page:
                    back_cb = f"courses::global::{origin_page}"
                else:
                    # default fallback: global page 1
                    back_cb = "courses::global::1"

            # Prepare persisted short ref for delete action (await the store helper)
            delete_payload = {
                'category': course_category,
                'name': course.get('name'),
                'origin_type': origin_type,
                'origin_page': origin_page,
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
                # Compute a back callback that returns to the course list for
                # this course's category (use saved back token when present).
                back_cb = None
                try:
                    if saved_back_cb:
                        back_cb = saved_back_cb
                except Exception:
                    back_cb = None
                if not back_cb:
                    back_target = course.get('category') or cat_name or '1'
                    try:
                        back_cb = f"courses::category::{urllib.parse.quote_plus(str(back_target))}::{origin_page}"
                    except Exception:
                        back_cb = f"courses::global::{origin_page}"

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
                                extra_row.append(InlineKeyboardButton("🏠 Coaches", callback_data=f"showcat::{urllib.parse.quote_plus(ppath)}"))
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
                                    extra_row = [InlineKeyboardButton("🏠 Coaches", callback_data=f"showcat::{urllib.parse.quote_plus(ppath)}")]
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
        # Read courses from the category document's embedded array and paginate
        category_doc = await db.categories.find_one({"name": category})
        if not category_doc or not category_doc.get('courses'):
            return []
        courses = category_doc.get('courses', [])
        # Sort courses case-insensitively A→Z for deterministic pagination
        courses = sorted(courses, key=lambda c: (c.get('name') or '').lower())
        start = (page - 1) * page_size
        return courses[start:start + page_size]
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

    try:
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
                        # flatten all courses
                        cats = await db.categories.find().to_list(length=None)
                        all_courses = []
                        for cat in cats:
                            for crs in cat.get('courses', []):
                                all_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})
                        all_courses = sorted(all_courses, key=lambda c: (c.get('name') or '').lower())
                        text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global')
                        if not text:
                            await safe_edit_message(query, f"No courses found on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return

                    if kind == "category":
                        category = urllib.parse.unquote_plus(parts[1])
                        page = int(parts[2])
                        category_doc = await db.categories.find_one({"name": category})
                        if not category_doc or not category_doc.get('courses'):
                            await safe_edit_message(query, f"No courses found in category '{category}' on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        courses = category_doc.get('courses', [])
                        courses = sorted(courses, key=lambda c: (c.get('name') or '').lower())
                        # compute origin_context (parent path) so Home returns to parent
                        origin_ctx = None
                        try:
                            parent = category_doc.get('parent')
                            if parent:
                                pdoc = await db.categories.find_one({"name": parent})
                                origin_ctx = pdoc.get('path') if pdoc and pdoc.get('path') else parent
                        except Exception:
                            origin_ctx = None
                        text, reply_markup = build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx)
                        if not text:
                            await safe_edit_message(query, f"No courses found in category '{category}' on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return

                    if kind == "coach":
                        coach_slug = urllib.parse.unquote_plus(parts[1])
                        page = int(parts[2])
                        # derive coach courses similar to show_coach_handler
                        cats = await db.categories.find().to_list(length=None)
                        coach_courses = []
                        coach_name = coach_slug
                        for cat in cats:
                            for crs in cat.get('courses', []):
                                if crs.get('coach') == coach_name:
                                    coach_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})
                        coach_courses = sorted(coach_courses, key=lambda c: (c.get('name') or '').lower())
                        text, reply_markup = build_courses_page(coach_courses, page=page, origin_type='coach', category=coach_name, origin_context=None)
                        if not text:
                            await safe_edit_message(query, f"No courses found for coach '{coach_name}' on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return
                else:
                    # legacy fallback handling
                    if len(parts) == 1:
                        page = int(parts[0])
                        cats = await db.categories.find().to_list(length=None)
                        all_courses = []
                        for cat in cats:
                            for crs in cat.get('courses', []):
                                all_courses.append({"name": crs.get('name'), "link": crs.get('link'), "category": cat.get('name')})
                        all_courses = sorted(all_courses, key=lambda c: (c.get('name') or '').lower())
                        text, reply_markup = build_courses_page(all_courses, page=page, origin_type='global', origin_context=None)
                        if not text:
                            await safe_edit_message(query, f"No courses found on page {page}.", action_key=getattr(query, 'data', None))
                            return
                        await safe_edit_message(query, text=text, reply_markup=reply_markup, action_key=getattr(query, 'data', None))
                        return
                    # legacy category + page
                    category = urllib.parse.unquote_plus(parts[0])
                    try:
                        page = int(parts[1])
                    except Exception:
                        await safe_edit_message(query, "Invalid page number.", action_key=getattr(query, 'data', None))
                        return
                    category_doc = await db.categories.find_one({"name": category})
                    if not category_doc or not category_doc.get('courses'):
                        await safe_edit_message(query, f"No courses found in category '{category}' on page {page}.", action_key=getattr(query, 'data', None))
                        return
                    courses = category_doc.get('courses', [])
                    courses = sorted(courses, key=lambda c: (c.get('name') or '').lower())
                    origin_ctx = None
                    try:
                        parent = category_doc.get('parent')
                        if parent:
                            pdoc = await db.categories.find_one({"name": parent})
                            origin_ctx = pdoc.get('path') if pdoc and pdoc.get('path') else parent
                    except Exception:
                        origin_ctx = None
                    text, reply_markup = build_courses_page(courses, page=page, origin_type='category', category=category, origin_context=origin_ctx)
                    if not text:
                        await safe_edit_message(query, f"No courses found in category '{category}' on page {page}.", action_key=getattr(query, 'data', None))
                        return
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
