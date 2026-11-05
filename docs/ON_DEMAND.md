# On-demand: what to expect from a Free instance

This bot runs on a **Free** Render web service and the instance is deliberately
*not* kept awake: no pinger, no keep-warm loop, no scheduled workflow. Here is
what that means in practice.

## How the first message after an idle period is served

1. Render spins a Free web service down after **15 minutes** without inbound
   traffic. A spun-down service consumes no instance hours.
2. The next HTTP request or WebSocket connection **wakes** it. Render holds that
   connection while the container boots - "about one minute" in Render's docs.
3. Telegram waits roughly 60 seconds for a `2xx` on a webhook POST, so the
   delivery that triggers the wake is often recorded as failed
   (`Read timeout expired`). It is **not lost**: it stays queued and Telegram
   retries with backoff.
4. The retry reaches the now-running instance and is handled. Expect the reply
   to the first message after an idle period to arrive about a minute late,
   occasionally only on the retry.

The update that woke the service and Telegram's retry can both reach the app, so
duplicate deliveries are dropped by `update_dedup.py`. `GET /status` reports the
counts under `update_dedup` (`accepted` vs `suppressed`).

## Do not keep it warm

- One always-on Free service uses roughly **730 of the 750 instance-hours** a
  workspace gets per month. Two or three exhaust the pool, and Render then
  suspends **every** Free service in the workspace until the 1st of the next
  month - worse than an occasional cold start.
- A Render **health check path is not a keep-alive**. Render's probes run
  against *running* instances only ("every few seconds, Render sends health
  checks to each running web service"), so they neither notice a spun-down
  service nor wake it. The path configured here (`/health/live`) is there to
  gate deploys and to restart an instance that stops answering; it adds no
  traffic of its own.
- Pinging `/health/live` from an external monitor or a GitHub Actions cron is
  inbound traffic, so it keeps the instance awake however it is labelled.

## Reading `/status`

`GET /status` (requires `STATUS_TOKEN`, otherwise it answers 404) explains why
the bot is late:

- `uptime_seconds` - a small number means the container just woke up.
- `webhook_registration` / `registered_by_this_process` - where this process
  told Telegram to deliver.
- `webhook[].pending_update_count` - updates Telegram is still holding
  for delivery; a number that stays above zero means deliveries are failing.
- `webhook[].last_error_message` - the last delivery error Telegram
  recorded.
- `update_dedup` - accepted vs suppressed duplicate deliveries.

## When a bot stops waking

Every "no reply" looks the same from Telegram, but `getWebhookInfo` (or `GET
/status`) says which one it is:

| Signal | Meaning | Fix |
| --- | --- | --- |
| `getWebhookInfo` answers **401** | the bot was deleted or its token revoked | set `BOT_TOKEN`/`API_CREDENTIALS` to the current BotFather token; until then no update can be delivered and no ping can wake the service |
| `last_error_message`: **429 Too Many Requests** | Render's edge refused parallel deliveries while the container booted | `WEBHOOK_MAX_CONNECTIONS=1` (now the default) |
| `last_error_message`: **Read timeout expired** | the cold start outlasted Telegram's ~60s wait | expected: the retry is served, and `update_dedup` drops the duplicate |
| webhook `url` host differs from this service | a stale `PUBLIC_URL`/`WEBHOOK_URL` pinned an old host | remove the pin; `RENDER_EXTERNAL_URL` now wins |
| `pending_update_count` > 0 | Telegram is holding updates it could not deliver | the waker below knocks, or wait for Telegram's retry |

### The on-demand waker

`scripts/wake_check.py`, run every 5 minutes by `.github/workflows/wake.yml`,
asks Telegram whether anything is pending or recently failed, and **only then**
requests `/health/live` to wake the container. An idle bot is never touched, so
it consumes no instance-hours until there is actually a message waiting.

Configure per repository (Settings -> Secrets and variables -> Actions):

- secret `BOT_TOKEN` - the bot's current token, matching the one Render runs
- variable `SERVICE_URL` - e.g. `https://your-service.onrender.com`

Run it by hand from the Actions tab (`wake-on-demand` -> *Run workflow*) to see
what Telegram reports. GitHub's scheduler is best-effort: a 5-minute cron can
run late, and scheduled workflows stop after 60 days without repository
activity.

