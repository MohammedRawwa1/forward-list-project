#!/usr/bin/env python3
"""Knock on the service only when Telegram actually has something to deliver.

A Free Render instance wakes on inbound traffic, so it never needs to be kept
awake - but when a cold start outlasts Telegram's wait, deliveries fail and are
retried on a schedule nobody controls. This asks Telegram what is stuck and only
then knocks, which costs no instance-hours while everything is idle.

Env:
  BOT_TOKEN              required - the bot whose deliveries to inspect
  SERVICE_URL            required - the service's public base URL
  LOOKBACK_MINUTES       optional (30) - knock if the last delivery error is newer
  WAKE_TIMEOUT_SECONDS   optional (120) - a cold start can take about a minute
  WAKE_ATTEMPTS          optional (3)

Exit 0 when there was nothing to do or the service answered; exit 1 when the
token is rejected or the service never answered, so the workflow shows it.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def fetch(url, timeout):
    """GET a URL, returning (status, body[:300], elapsed) without raising."""
    # SERVICE_URL comes from a repository variable, so refuse anything that is not
    # HTTP(S) instead of handing urllib a file: or custom scheme.
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return None, f"refusing non-HTTP(S) URL: {url}", 0.0
    req = urllib.request.Request(url, headers={"User-Agent": "on-demand-waker/1.0"})
    started = time.monotonic()
    try:
        # The scheme is validated above; bandit only sees the call.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode("utf-8", "replace"), time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - timeouts and DNS failures are expected here
        return None, f"{type(exc).__name__}: {exc}", time.monotonic() - started


def notice(message):
    print(f"::notice::{message}")


def warn(message):
    print(f"::warning::{message}")


def fail(message):
    print(f"::error::{message}")


def main() -> int:
    token = (os.environ.get("BOT_TOKEN") or "").strip()
    base = (os.environ.get("SERVICE_URL") or "").strip().rstrip("/")
    lookback = float(os.environ.get("LOOKBACK_MINUTES", "30")) * 60
    timeout = float(os.environ.get("WAKE_TIMEOUT_SECONDS", "120"))
    attempts = max(1, int(os.environ.get("WAKE_ATTEMPTS", "3")))

    if not token or not base:
        notice("BOT_TOKEN secret or SERVICE_URL variable is not set for this repository - skipping, nothing was woken.")
        return 0

    status, body, _ = fetch(f"https://api.telegram.org/bot{token}/getWebhookInfo", 20)
    if status != 200:
        try:
            description = json.loads(body).get("description") or body
        except ValueError:
            description = body
        if status == 401:
            fail(
                "Telegram rejected BOT_TOKEN (401): the bot was deleted or its token revoked. Nothing can be "
                "delivered to it, so no ping can wake the service. Set BOT_TOKEN to the current BotFather token."
            )
        else:
            fail(f"getWebhookInfo failed: HTTP {status} {description}")
        return 1

    info = json.loads(body).get("result") or {}
    webhook_url = info.get("url") or ""
    pending = int(info.get("pending_update_count") or 0)
    last_error = info.get("last_error_message")
    last_error_at = info.get("last_error_date") or 0
    error_age = (time.time() - last_error_at) if last_error_at else None

    webhook_host = urllib.parse.urlsplit(webhook_url).hostname
    service_host = urllib.parse.urlsplit(base).hostname

    print(f"webhook host : {webhook_host or '(none - no delivery target)'}")
    print(f"service host : {service_host}")
    print(f"pending      : {pending}")
    if last_error:
        print(f"last error   : {last_error} ({int(error_age or 0)}s ago)")
    else:
        print("last error   : none")

    if not webhook_host:
        fail(
            "Telegram has no webhook registered for this bot, so no delivery will ever wake the service. "
            "Knocking anyway so the app can register itself on boot."
        )
    elif webhook_host != service_host:
        warn(
            f"Telegram delivers to {webhook_host}, but SERVICE_URL is {service_host}. Waking {service_host} lets "
            "the app re-register itself; if the mismatch persists, remove the stale PUBLIC_URL/WEBHOOK_URL pin."
        )

    recent_error = error_age is not None and error_age < lookback
    if pending <= 0 and not recent_error:
        notice("Nothing pending and no recent delivery error - not knocking, so the instance stays spun down.")
        return 0

    reason = f"{pending} pending update(s)" if pending > 0 else f"delivery failed {int(error_age or 0)}s ago ({last_error})"
    print(f"\nknocking on {base}/health/live because of {reason}")

    status = None
    body = ""
    elapsed = 0.0
    for attempt in range(1, attempts + 1):
        status, body, elapsed = fetch(base + "/health/live", timeout)
        print(f"  attempt {attempt}/{attempts}: status={status} in {elapsed:.1f}s body={body[:160]!r}")
        if status == 200:
            notice(f"woke {service_host} in {elapsed:.1f}s ({reason})")
            return 0
        if attempt < attempts:
            time.sleep(20)

    if status == 404 and "not found" in (body or "").lower():
        fail(
            f"{base} does not exist (404 from Render's edge). SERVICE_URL is wrong - point it at the "
            "service's current onrender.com hostname."
        )
    else:
        fail(
            f"{service_host} did not answer /health/live after {attempts} attempts "
            f"(last: status={status}, {body[:120]!r}). If the Render dashboard shows the service suspended, "
            "no ping can wake it: that needs a plan change or the monthly quota reset."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
