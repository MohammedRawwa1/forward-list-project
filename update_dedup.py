"""Drop a repeated delivery of the same Telegram update.

A Free Render instance is woken by the very delivery it has to answer, and the
cold start can outlast the ~60s Telegram waits for a 2xx. Telegram then retries
an update it already delivered, so the same update_id can arrive twice - once
held by Render's edge and forwarded after boot, once as the retry. Nothing
downstream is idempotent, so without this guard one message gets answered twice.

In-memory and bounded on purpose: retries land within seconds to minutes, and a
process restart is a new deploy, where handling an update once is correct.
"""

import time
from collections import OrderedDict

# Telegram's retry window for a webhook delivery is minutes, not hours.
TTL_SECONDS = 900.0
# Cap the map so a long-lived process cannot grow it without bound. Older ids
# are evicted first.
MAX_ENTRIES = 2048

_seen: OrderedDict = OrderedDict()
_state: dict = {
    "accepted": 0,
    "suppressed": 0,
    "last_suppressed_update_id": None,
    "last_suppressed_at": None,
}


def _evict(now: float) -> None:
    """Forget ids past the retry window, and keep the map under its cap."""
    while _seen:
        oldest_id, seen_at = next(iter(_seen.items()))
        if len(_seen) > MAX_ENTRIES or now - seen_at > TTL_SECONDS:
            _seen.pop(oldest_id, None)
        else:
            return


def mark_seen(update_id) -> bool:
    """Record update_id; return False if it was already handled recently."""
    if not isinstance(update_id, int):
        # No usable id: never drop an update over a missing field.
        return True
    now = time.time()
    _evict(now)
    if update_id in _seen:
        # Refresh the entry: the retry is proof the delivery is still live.
        _seen[update_id] = now
        _seen.move_to_end(update_id)
        _state["suppressed"] += 1
        _state["last_suppressed_update_id"] = update_id
        _state["last_suppressed_at"] = now
        return False
    _seen[update_id] = now
    _state["accepted"] += 1
    return True


def snapshot() -> dict:
    """For GET /status: how many deliveries were accepted vs suppressed."""
    return {
        "ttl_seconds": TTL_SECONDS,
        "tracked": len(_seen),
        "accepted": _state["accepted"],
        "suppressed": _state["suppressed"],
        "last_suppressed_update_id": _state["last_suppressed_update_id"],
        "last_suppressed_at": _state["last_suppressed_at"],
    }
