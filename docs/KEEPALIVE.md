# Keeping a Free Render instance awake

A bot on Render's Free plan goes deaf when the instance sleeps. This is the
short version of why, what actually fixes it, and what it costs.

## What Render does

From [Deploy for Free](https://render.com/docs/free):

- A Free **web service** spins down after **15 minutes without inbound traffic**
  (HTTP requests and WebSocket messages count; outbound calls do not).
- It spins back up on the next HTTP request or WebSocket connection, and that
  takes **about one minute** — measured 25 s on this service.
- Free web services get **750 instance-hours per workspace per calendar month**,
  shared by every service in the workspace. Exhausting that suspends all of them
  until the 1st of the next month.
- The filesystem is ephemeral and Render may restart a Free service at any time.

Telegram's webhook deadline is far shorter than a cold start, so the delivery
that wakes the service fails, `getWebhookInfo` records a `last_error_message`,
and only the retry is served. That is the "deaf" behaviour, and no amount of
startup-speed work removes it — the instance has to stay awake.

## Why an external pinger, not a self-ping

`KEEP_WARM=1` makes the app request its own public `/health/live`. It may work,
but it is not guaranteed: Render documents *inbound* traffic as the trigger and
does not say whether a request originating from the service itself is counted,
proxied differently, or excluded. Do not build on it.

`.github/workflows/keepalive.yml` pings from GitHub's runners instead, so the
traffic is unambiguously external. `GET /status` reports both counters under
`health_checks`, which is how you tell whether the external pinger is really
reaching the service:

```json
"health_checks": {
  "self": 3, "self_last_at": 1772000000.0,
  "external": 41, "external_last_at": 1772000600.0,
  "external_last_user_agent": "curl/8.5.0"
}
```

`external_last_at` should be within the last ~10-15 minutes. If it is stale, the
workflow is not running (see the caveats below) and the bot will sleep.

## Setup

1. Deploy this commit so `/health/live` exists on the running service. Until
   then the endpoint answers 405 and the ping is useless.
2. Point the repo variable at the service's health endpoint:

   ```bash
   gh variable set KEEP_ALIVE_URL --body https://<service>.onrender.com/health/live
   ```

   The value must be a route your app serves — **not** `robots.txt`. While a
   Free service is asleep, Render answers `/robots.txt` itself with a
   disallow-all page and never wakes the service, so that path looks healthy
   forever while the bot sleeps.
3. Run the workflow once from the Actions tab (`keep-alive` → *Run workflow*) and
   confirm it reports `HTTP 200`.
4. Optional: set Render's **Health Check Path** to `/health/live` in the
   dashboard. It is a real route that never touches MongoDB, so a slow database
   cannot make Render restart-loop an instance that is actually fine.

### Any external monitor works

The workflow only needs a scheduled GET. A free uptime monitor (UptimeRobot,
cron-job.org, Better Stack, a cron on any other host) pointed at the same
`/health/live` URL every 5–10 minutes does the same job, and some of them are
more punctual than GitHub's scheduler. Do not point two of them at the same
service; one is enough.

## What it costs

An always-on Free service uses roughly **730 of the 750** monthly instance-hours
for the whole workspace. So:

- Keep **one** service warm, not three. Three warm services exhaust the quota
  around day 10 and Render suspends every Free service in the workspace.
- Or narrow the pings to your active hours, e.g. every 10 minutes from 06:00 to
  23:00 UTC: `- cron: "*/10 6-22 * * *"` uses ~370 h and leaves room for the
  others to cold-start when you are not using them.
- If a bot must be up all the time, that is what a paid instance is for: paid
  services are not spun down, so no pinger and no quota arithmetic.

## Caveats

- **Scheduler delay.** GitHub's `schedule` is best-effort and can run several
  minutes late; the 10-minute cadence leaves 5 minutes of slack under Render's
  15-minute window, which is usually enough but is not a guarantee.
- **60-day disable.** GitHub disables scheduled workflows in repositories with
  no activity for 60 days. If pings stop, check the Actions tab.
- **Deploys and restarts still happen.** Render may restart a Free service at
  will; wake-up handling (fast startup, ack-first webhook, `/health/live`) is
  what keeps a message from being lost when that happens.
- **Instance-hour accounting is Render's.** Check
  [the free-tier docs](https://render.com/docs/free) before relying on the
  750-hour number; plans and limits change.
