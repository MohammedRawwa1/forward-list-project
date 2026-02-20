Uptime / Liveness guidance

- Use the existing root endpoint `/` as the health check (it returns a simple JSON message).

UptimeRobot (recommended simple setup)

1. Create an account at https://uptimerobot.com/ (free tier available).
2. Add a new monitor:
   - Monitor Type: "HTTP(s)"
   - Friendly Name: e.g. "Forward List Bot"
   - URL (or IP): https://your-app.example.com/  (include trailing `/` if you prefer)
   - Monitoring Interval: 5 minutes (free) or 1 minute (paid)
   - Click "Create Monitor"
3. Optional: add alert contacts (email, SMS, or integrations).

Notes and tips

- If your host (Render) suspends free services due to inactivity, an external monitor that pings the root URL will keep it awake.
- Alternatively run the included `scripts/liveness_ping.py` from a small VM or scheduled runner (it uses only stdlib).

Using the included ping script

- Run once and exit:

```bash
python scripts/liveness_ping.py --url https://forward-list-project.onrender.com/
```

- Run continuously (every 5 minutes):

```bash
python scripts/liveness_ping.py --url https://forward-list-project.onrender.com/ --loop --interval 300
```

Security

- If your endpoint becomes publicly reachable and contains sensitive handlers, consider:
  - Using basic auth or a secret path for the health endpoint, and configuring UptimeRobot headers or a signed URL.
  - Limiting allowed methods to GET for the health endpoint.