# HR Internal Tool

Internal web app for the HR team. It lists Culture Index surveys, shows whether
each candidate's Culture Index report is already attached to their JazzHR
applicant profile, and uploads the missing ones.

It runs in two parts, because Culture Index blocks Render's IP:

- **Website (Render):** `app_dash_simple.py`. Login, survey list, JazzHR status
  checks, upload requests. Never contacts Culture Index.
- **Worker (SEnergy VPS, systemd service `hr-worker`):** `vps_worker.py serve`.
  Exports the survey list from Culture Index and performs the uploads, picking up
  requests within about a second. Outbound connections only.

They share state through Upstash Redis. Architecture and known issues:
[CODEBASE_DOCUMENTATION.md](CODEBASE_DOCUMENTATION.md).

## Requirements

- Python 3.11 (pinned in `.python-version` and `render.yaml`)
- Upstash Redis (`REDIS_URL`), used by both parts
- JazzHR API key (both parts); Culture Index login (worker only)

```bash
pip install -r requirements.txt
```

## Website configuration (Render environment)

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_USERNAME`, `APP_PASSWORD` | required | The single app login |
| `SECRET_KEY` | required | Flask session signing |
| `JAZZHR_API_KEY` | required | JazzHR status checks |
| `REDIS_URL` | required | Shared state |
| `KEY_PREFIX` | empty | Prefix for every Redis key; use e.g. `test:` to keep a test run off production data |
| `SESSION_COOKIE_SECURE` | `True` | Set `False` only for plain-http local runs |
| `ITEMS_PER_PAGE` | 15 | Surveys per page |
| `MAX_BATCH_UPLOAD` | 15 | Most surveys in one "Upload Selected" |
| `MAX_BACKGROUND_CHECK` | 50 | How many of the newest surveys are kept fresh in the background |
| `RECENT_SURVEY_THRESHOLD` | 1000 | Newest surveys whose status goes stale; older ones never do on their own |
| `JAZZHR_CACHE_HOURS` | 2 | When a recent survey's status counts as stale (it is still shown while rechecked) |
| `JAZZHR_CALLS_PER_MINUTE` | 60 | JazzHR budget for this app (JazzHR allows 80; the worker uses 15) |
| `BACKGROUND_CALLS_PER_MINUTE` | 35 | Ceiling for background checks inside that budget |
| `UI_POLL_INTERVAL_MS` | 3000 | How often a browser tab asks this app for changes (no Redis cost) |
| `FALLBACK_POLL_SECONDS`, `ACTIVE_POLL_SECONDS` | 300, 20 | Safety-net reads of the worker's state (the worker normally announces changes over pub/sub); the shorter one applies while a refresh or upload is in progress |
| `BACKGROUND_SCAN_MINUTES` | 10 | How often (work hours) the newest surveys are scanned for stale statuses |
| `DIAG_STATUS_CHECK` | `0` | `1` logs a `[DIAG]` line for every status check |
| `REDIS_CONNECT_TIMEOUT`, `REDIS_SOCKET_TIMEOUT`, `REDIS_HEALTH_CHECK_INTERVAL` | 5, 5, 30 | Redis connection settings |

Start command (in `render.yaml`): `gunicorn app_dash_simple:server --workers 1 --threads 8 --timeout 120`.
Keep **one worker**: the survey list, status queue and change tracker live in process memory.

## Worker configuration (`.env` next to `vps_worker.py`, or `WORKER_ENV_FILE`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CULTUREINDEX_EMAIL`, `CULTUREINDEX_PASSWORD` | required | Culture Index login |
| `JAZZHR_API_KEY` | required | Uploads |
| `REDIS_URL` | required | Shared state |
| `KEY_PREFIX` | empty | Must match the website |
| `CLIENT_ID` | `A89F5B0000` | Culture Index client account |
| `WORKER_JAZZHR_CALLS_PER_MINUTE` | 15 | JazzHR budget for uploads |
| `SIZE_LOOKUP_NEWEST`, `SIZE_LOOKUPS_PER_RUN`, `SIZE_REFRESH_DAYS` | 300, 50, 7 | Report size lookups for the newest surveys |
| `PDF_FETCH_TIMEOUT` | 5 | Report size lookup timeout (seconds) |
| `ALLOWED_PDF_DOMAINS` | empty | Extra report hosts for the download allowlist |

Service (`/etc/systemd/system/hr-worker.service`):

```
[Unit]
Description=HR tool VPS worker (Culture Index export + JazzHR uploads)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=senergyadmin
WorkingDirectory=/home/senergyadmin/hr_worker
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/home/senergyadmin/hr_worker/venv/bin/python vps_worker.py serve
Restart=always
RestartSec=10
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now hr-worker`, `systemctl status hr-worker`,
`sudo systemctl restart hr-worker` after updating the code.

The service waits on the refresh and upload queues, runs the scheduled exports
(every 30 minutes on weekdays 7:00–18:59 Central, every 3 hours otherwise),
retries once when Culture Index answers 504, and writes a heartbeat every
5 minutes. Once a minute (every 20 seconds right after a start) it also picks up
any request the queue failed to deliver. The worker never writes downloaded data
to disk; its only file output is `worker.log` (capped at about 2 MB).

## Maintenance

| Command | Where | What it does |
|---------|-------|--------------|
| `python cultureindex_client.py` | VPS | Checks whether this machine can log in to Culture Index |
| `python vps_worker.py export` | VPS | Exports surveys now (the service keeps running) |
| `journalctl -u hr-worker -n 50` or `tail ~/hr_worker/worker.log` | VPS | Worker log |
| `python rebuild_cache.py` | VPS | Rechecks every survey against JazzHR and rewrites the status store (asks: clear or resume) |

`rebuild_cache.py` writes to whatever `REDIS_URL` and `KEY_PREFIX` point at.
