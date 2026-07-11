Project: Forward List Telegram Bot

Overview
- Purpose: A FastAPI + python-telegram-bot application that exposes a webhook endpoint and serves a Telegram bot UI for managing categories, coaches and courses. Supports inline navigation, server-side pagination, cached counts/pages (with optional Redis backing), persisted short callback refs (Redis or Mongo), UUID-based course IDs, admin delete flows, and maintenance scripts for data migration and cleanup.

Technologies
- Python 3.10+ (asyncio)
- python-telegram-bot (async Application)
- FastAPI + Uvicorn (webhook receiver)
- MongoDB (async driver via project MongoDB wrapper)
- Redis (optional — used for caching, callback persistence, and retry queue)
- UUIDs for embedded course `id` fields
- Loguru / standard logging

Key Architectural Features
- Server-side pagination: all large lists (categories/courses) are paged with DB-side aggregation and `$slice` projection to avoid loading huge arrays into memory.
- In-process caches: `_COUNT_CACHE` and `_PAGE_CACHE` provide low-latency counts and pages; when Redis is configured these caches are backed asynchronously to Redis for multi-process deployments.
- Callback refs: short callback payloads are persisted using an in-memory map, Redis (preferred), and MongoDB (fallback). TTLs are configurable via `CALLBACK_REF_TTL` and refs are rehydrated at startup so inline keyboards survive restarts.
- Retry/backoff worker: edits that hit Telegram's `RetryAfter` are queued into a Redis sorted-set and executed later by the background `start_redis_retry_worker` which implements best-effort backoff scheduling.
- UUID-safe deletions: courses are created with `id: str(uuid.uuid4())`. Delete flows use embedded `courses.id` where available to avoid ambiguous name-based deletions. Parent/category deletions are performed using _id-aware subtree deletion (avoids accidentally deleting multiple docs with identical `name`).

Quick Setup (local)
1. Create virtualenv and install deps

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
pip install -r requirements.txt
```

2. Create a `.env` (or set environment variables) with at least:
- `BOT_TOKEN` — Telegram bot token
- `MONGODB_URL` — MongoDB connection URI (mongodb+srv://... or mongodb://...)
- `MONGODB_NAME` — Database name

Optional:
- `REDIS_URL` — Redis connection URI (if present, caching and callback persistence use Redis)
- `BOT_OWNER_ID` — numeric Telegram user id for admin-only commands
- `CALLBACK_REF_TTL` — seconds to persist callback refs (default ~7 days)
- `PAGE_CACHE_TTL` — short TTL for page cache (default 30s)
- `GUI_SESSION_TTL` — inline session TTL for auto-close (default 300s)
- `LIVENESS_TOKEN` — (optional) token required to access `/health`
- `LOG_LEVEL` — INFO/DEBUG

3. Run locally with a public webhook (ngrok example):

```powershell
ngrok http 10000
# set Telegram webhook to https://<your-ngrok>.ngrok.io/<BOT_TOKEN>/
uvicorn main:app --host 0.0.0.0 --port 10000
```

Note: `main.py` starts the FastAPI app and initializes the Telegram `Application`. The server receives updates on `POST /{token}/` and forwards them to the bot application.

Running in production
- Use process manager or container orchestration to run `uvicorn main:app` (the `Procfile` suggests `worker: python3 -m bot` for alternate run styles). Ensure MongoDB and (optionally) Redis are reachable. When Redis is not configured the code initializes a synchronous MongoDB client to provide durable writes for callback refs and other durability-sensitive operations.

How deletion works (safety & UUIDs)
- Course creation: when a course is added, the handler assigns `id = str(uuid.uuid4())` and pushes it into the category document. This enables deterministic deletion by `courses.id`.
- Course deletion (Details view): the Details view stores a short callback ref that includes the course `id` (when present). The Delete flow resolves that payload and prefers deleting by `courses.id` (UUID) — falling back to name-based deletion only for legacy entries.
- Category / Parent deletion: deletion of categories and parents is performed in an _id-aware way to avoid deleting unintended documents when multiple category documents share the same `name`. The code uses a subtree collection approach and deletes only the exact `_id` documents that belong to the subtree.

Caching & Redis Backoff
- Count and page caching: `_COUNT_CACHE` (counts) and `_PAGE_CACHE` (page payloads) are in-process caches with optional Redis backing. When `_redis` is configured, cache entries are written to Redis asynchronously to allow multiple worker processes to share cached results.
- Retry/backoff: `safe_edit_message` and the Redis retry worker cooperate to enqueue edit requests that receive Telegram's `RetryAfter` error. These are stored in a Redis sorted set (`retry:queue`) keyed by execution time. `start_redis_retry_worker` polls due items and re-executes them.

Admin commands & tooling (owner only)
- `/add` - add a course
- `/courses` - list courses
- `/categories` - list categories
- `/delete_category` - delete a coach/child category and subtree
- `/delete_parent` - delete a top-level parent and its subtree
- `/delete_all_data` - destructive: deletes categories and courses (owner-only)
- `/design_cat` - reply to a photo to assign it as a banner design for a parent category
- `/remove_design` - remove a category's banner design
- Debugging endpoints/commands: see `debug_db`, logging is configured to `bot.log` via Loguru

What you can do next / Suggested improvements
- Harden admin `exec`/`eval` flows (if present) or restrict them further.
- Add explicit index creation / migration scripts for production deployments.
- Improve summary reports to show exact `_id`-based affected-document lists when preparing deletions.
- Add unit/integration tests for deletion edge-cases where multiple docs share the same `name`.

Where to look in the code
- Application entry: `main.py`
- Bot wiring & commands: `bot.py`
- Core handlers: `handlers/base_handlers.py`, `handlers/course_handlers.py`, `handlers/bot_handlers.py`
- Deletion flows: `handlers/delete_callbacks.py` (this file contains the confirm/summary flows)
- DB layer: `database/mongo_handler.py`
- Category designs: `handlers/category_design.py` (owner-only banner design assignment)
- Search handlers: `handlers/search_handlers.py`, `handlers/atlas_search.py`
- Deletion flows: `handlers/delete_callbacks.py`