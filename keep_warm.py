"""Keep this Free Render web service awake - opt-in via KEEP_WARM.

A Free web service spins down after 15 minutes without inbound HTTP traffic and
takes about a minute to come back (Render's free-tier docs). Telegram gives a
webhook far less time than that, so the first delivery after a spin-down is
lost and only its retry is served.

Setting KEEP_WARM=1 makes this process request its own public /health/live every
KEEP_WARM_INTERVAL_SECONDS (default 600, comfortably below the 15-minute idle
threshold). The request arrives through Render's edge like any other inbound
request, so the instance never reaches the sleeping state.

Cost: the service then runs continuously - roughly 730 of the 750 instance-hours
a workspace gets per month. Enable it on ONE service only: exhausting that quota
suspends every Free service in the workspace until the 1st of the next month.
"""

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 600.0
# Never ping faster than this, even if the env var asks for it: the point is to
# stay under Render's idle window, not to generate traffic.
MIN_INTERVAL_SECONDS = 60.0
PING_TIMEOUT_SECONDS = 20.0

_state: dict[str, Any] = {
    "enabled": False,
    "url": None,
    "interval_seconds": None,
    "started_at": None,
    "pings": 0,
    "failures": 0,
    "last_status": None,
    "last_ok_at": None,
    "last_error": None,
}
_task: asyncio.Task | None = None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _public_url() -> str | None:
    """The URL to ping: KEEP_WARM_URL, else this service's public base + health."""
    explicit = (os.getenv("KEEP_WARM_URL") or "").strip()
    if explicit:
        return explicit
    base = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not base:
        return None
    return f"{base.rstrip('/')}/health/live"


def _interval() -> float:
    try:
        value = float(os.getenv("KEEP_WARM_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, value)


async def _loop(url: str, interval: float) -> None:
    # The first ping goes out quickly: it proves the URL works while the
    # instance is definitely awake, instead of finding out 10 minutes later.
    await asyncio.sleep(min(interval, 30.0))
    while True:
        try:
            async with httpx.AsyncClient(timeout=PING_TIMEOUT_SECONDS, follow_redirects=True) as client:
                resp = await client.get(url)
            _state["pings"] += 1
            _state["last_status"] = resp.status_code
            _state["last_ok_at"] = time.time()
            _state["last_error"] = None
            logger.debug("Keep-warm ping %s -> %s", url, resp.status_code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed ping is the interesting case: if the service cannot
            # reach itself, it will spin down again.
            _state["failures"] += 1
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Keep-warm ping %s failed: %s", url, _state["last_error"])
        await asyncio.sleep(interval)


def start() -> asyncio.Task | None:
    """Start the loop when KEEP_WARM is set; otherwise do nothing at all."""
    global _task
    if _task is not None and not _task.done():
        return _task
    if not _truthy(os.getenv("KEEP_WARM")):
        _state["enabled"] = False
        return None
    url = _public_url()
    if not url:
        logger.warning(
            "KEEP_WARM is set but no public URL is known; set KEEP_WARM_URL "
            "(or PUBLIC_URL / RENDER_EXTERNAL_URL) to enable it"
        )
        return None
    interval = _interval()
    _state.update(
        {
            "enabled": True,
            "url": url,
            "interval_seconds": interval,
            "started_at": time.time(),
        }
    )
    _task = asyncio.create_task(_loop(url, interval))
    logger.info("Keep-warm enabled: GET %s every %.0fs", url, interval)
    return _task


def stop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None


def snapshot() -> dict[str, Any]:
    """Current keep-warm state for /status; contains no credentials."""
    out = dict(_state)
    out["running"] = _task is not None and not _task.done()
    return out
