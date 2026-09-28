# Culture Index to JazzHR HR Tool: Codebase Documentation

Last updated: 2026-09-28

## 1. Where this code comes from

This folder started as the production code from
https://github.com/Thinh-nguyen-03/HR_internal_tool (last commit 2026-07-02),
which Render deploys. Section 11 lists what changed since. File names match the
repo so the folder can be pushed back. The folder itself is not a git repository.

**Status (2026-09-28):** the website/worker split is built and tested locally
(worker against live Culture Index and Upstash under `KEY_PREFIX=test:`, website
in a real browser; uploads tested with only the final JazzHR POST stubbed). Not
yet pushed or deployed. Production still runs the repo version. See section 13
for the cutover steps.

## 2. What the tool does

1. Exports every Culture Index survey (name, email, phone, trait pattern, survey
   date, positions, report URL).
2. For each survey, finds the candidate in JazzHR by exact name and checks
   whether their Culture Index report is already attached.
3. Shows the surveys as cards with a status, and uploads missing reports one at
   a time or in batches.
4. Shows a "New Surveys" count when new surveys appear.

## 3. Architecture

Culture Index's firewall (Azure Front Door) returns HTTP 403 to Render for every
request, including report PDFs, while the SEnergy VPS is allowed. So the work is split:

```
VPS worker (cron, outbound only)          Upstash Redis                     Render website
  export: log in to Culture Index  ─────> surveys:snapshot + surveys:meta ──> SnapshotWatcher (1 MGET/min)
          export CSV, sizes                known_survey_ids                    survey list in memory
                                           new_surveys_notification ───────>  "New Surveys" badge
  uploads: take queued jobs        <───── uploads:queue, uploads:job:{id} <── Upload buttons
           re-check JazzHR,
           fetch PDF, upload       ─────> jazzhr_status:{id} = UPLOADED
                                           uploads:job:{id} = done/failed
                                           worker:version (INCR)   ───────>  open pages re-render
  (reads surveys:refresh_requested) <──── "Refresh Surveys" button
                                           jazzhr_status:{id}  <───────────  StatusWorker (JazzHR checks)
```

Neither side connects to the other. The VPS accepts no incoming connections.

## 4. Files

| File | Runs on | Purpose |
|------|---------|---------|
| `app_dash_simple.py` | Render | Entry point (`server`). Survey store, snapshot watcher, routes, login gate, layout, callbacks. |
| `status_worker.py` | Render | The single queue for all JazzHR status checks (priorities, de-duplication, background ceiling). |
| `ui_state.py` | Render | In-memory change tracker and badge state that browser tabs poll; `log()`. |
| `vps_worker.py` | VPS | `export` and `uploads` modes, run by cron. |
| `rebuild_cache.py` | VPS | Manual full recheck of every survey. |
| `shared_state.py` | both | Every Redis key, the snapshot format, the job queue helpers, `KEY_PREFIX`. |
| `cache_storage.py` | both | `StatusStore`: JazzHR statuses in Redis, kept indefinitely with their check time. |
| `check_jazzhr_uploads.py` | both | JazzHR client: exact-name search, files, report matching, status, upload, `RateLimiter`. |
| `cultureindex_client.py` | VPS | Culture Index login, CSV export, report PDF download, CSV parsing, report size lookup. |
| `auth.py`, `login_layout.py` | Render | Single-user login, lockout, login page. |
| `survey_display.py` | Render | Relative times, loading/error/empty list states. |
| `security_utils.py`, `input_validation.py` | both / Render | Download URL allowlist; search and page sanitizing. |
| `assets/` | Render | CSS and logo. |
| `render.yaml`, `Procfile`, `.python-version`, `requirements.txt` | | Deployment. |

## 5. The worker (`vps_worker.py`)

Cron schedule (Central time): `export` every 30 min in work hours (Mon–Fri
7:00–18:59) and every 3 hours otherwise; `uploads` every 2 min in work hours.
A file lock makes an overlapping run of the same mode skip itself.

**export**
1. Logs in, exports the CSV, parses it (in memory; the CSV is dropped at once).
2. One MGET reads the previous snapshot, its meta, the known-ID baseline, the
   pending notification and the refresh flag.
3. Report sizes are carried forward from the previous snapshot; only new
   surveys and sizes older than 7 days among the newest 300 are looked up
   (at most 50 per run, only real PDF responses count).
4. If the content hash is unchanged, only `checked_at` in the meta is updated.
   Otherwise one transaction writes the snapshot, meta and known IDs, and adds
   genuinely new IDs to the notification (merged with any unclicked one; the
   first ever run records a baseline silently).
5. Clears the refresh flag if one was set.

**uploads**
1. One pipeline reads the refresh flag and pops the first job. A set flag runs an export first.
2. For each job: marks it `uploading`; re-checks JazzHR (the page may be stale,
   and this picks the current target record); if already uploaded, marks it done
   without uploading; otherwise fetches the PDF (portal endpoint, fallback to the
   CSV report URL, both PDF-checked), uploads it, writes an `UPLOADED` status
   directly, and marks the job done or failed. PDFs are never kept, not even
   for the one retry.
3. Increments `worker:version` if any job was processed.

## 6. The website (`app_dash_simple.py`)

- **Survey list:** `SnapshotWatcher` reads `surveys:meta`, `worker:version` and
  the notification in one MGET every minute in work hours, every 5 minutes
  otherwise, and every 15 seconds for 15 minutes after an upload is queued. It
  downloads the snapshot only when the hash changes. New surveys don't reshuffle
  an open page; the badge shows them and clicking it shows page 1.
- **Rendering:** every render reads the page's statuses and job records (two
  MGETs) and draws complete cards, including Upload buttons and checkboxes.
  There is no app-wide lock and no render cache; renders from memory take
  milliseconds. Checkbox selections live in a store, so re-renders keep them.
- **Change tracking:** tabs poll `ChangeTracker` in memory every 3 seconds (no
  Redis) and re-render only when a survey on their page changed, or when the
  worker finished uploads.
- **Status checks (`StatusWorker`):** one queue, three foreground threads and one
  background thread. Priorities: Refresh clicks, then the page being viewed,
  then background. A survey is never queued or checked twice at once. Background
  checks have their own lower rate ceiling (35 of the app's 60 calls/minute) so
  they can't take the budget someone is waiting on. A failed check keeps the
  previous status on screen and is shown as "Last check failed" (not stored).
  A check that raced an upload never overwrites the worker's `UPLOADED`.
- **Stale statuses** are shown while being rechecked. The page being viewed
  queues its own stale ones; a background scan every 10 minutes (work hours)
  keeps the newest `MAX_BACKGROUND_CHECK` fresh. "Checking" only appears for a
  survey that has never been checked.
- **Uploads** are queued with `shared_state.enqueue_upload` (one job per survey
  at a time). Cards show Queued, Uploading, then the worker's result.

## 7. Report match rules (`check_pdf_match`)

A JazzHR file on the applicant counts as the report when, in order: the survey
ID is in the filename; the filename equals the Culture Index report filename;
the filename contains "CultureIndex" (this tool's upload name) and the person's
name; or the filename contains the name **and** the size is within max(2 KB, 2%)
of the report size. Size alone never counts. Up to 5 applicant records with the
exact name are checked; uploads go to the newest.

## 8. Status values

| Status | Card label | Meaning |
|--------|-----------|---------|
| `UPLOADED` | Uploaded | Report found on one of the person's applicant records |
| `NOT_UPLOADED` | Not Uploaded | Person found, report missing (uploadable) |
| `NOT_IN_JAZZHR` | Not in Jazz | No applicant with that exact name |
| `MISSING_NAME` | No Name | Survey has no first or last name |
| `NO_PDF_URL` | No URL | Survey has no report URL |
| none yet | Checking | Never checked |

## 9. Redis keys and command budget

| Key | Written by | Lifetime |
|-----|-----------|----------|
| `surveys:snapshot`, `surveys:meta` | worker | permanent |
| `known_survey_ids` | worker | permanent |
| `new_surveys_notification` | worker (cleared by badge click) | 24 h |
| `surveys:refresh_requested` | website | until the worker runs, max 1 h |
| `uploads:queue` | website push, worker pop | |
| `uploads:job:{surveyId}` | both | 24 h while active, 1 h after finishing |
| `worker:version` | worker | permanent |
| `jazzhr_status:{surveyId}` | both | permanent (freshness from its timestamp) |
| `login_attempts:{username}` | website | 5 min |

All keys take the `KEY_PREFIX` prefix. Legacy keys from the previous version
(`app:*`, `pdf_sizes:*`, `upload_lock:*`) are no longer used and can be deleted.

Upstash free plan: 500K commands/month, 10 MB per request, 256 MB storage. The
snapshot is about 600 KB (compressed, base64). Expected use is roughly
30K/month for the worker and 30–60K/month for the website plus status writes;
browser polling costs nothing.

## 10. HTTP routes (website)

| Route | Access | Purpose |
|-------|--------|---------|
| `/` | login | The app |
| `/login`, `/logout` | public | Login page (form submits through a Dash callback), logout |
| `/health`, `/health/ready`, `/health/live` | public | Probes (no data) |
| `/health/detailed` | login | Redis, snapshot meta, survey count |
| `/_dash-update-component` | login, except the login callback | Every Dash callback |

## 11. Change history

### 2026-09-28: website/worker split and responsiveness

- Culture Index work moved to `vps_worker.py`; Render no longer needs Culture
  Index credentials. The hourly Render cron, `/api/background-refresh`,
  `background_sync.py`, `trigger_background_refresh.py` and `app_cache.py` are gone.
- One status queue with priorities, de-duplication and a background ceiling
  replaces per-render check threads, the background checker thread and the
  synchronous "Refresh JazzHR" re-check.
- Statuses no longer expire; stale ones are shown while rechecked.
- Status updates re-render whole cards (Upload buttons and checkboxes appear
  when a status arrives); selections survive re-renders.
- Tabs poll an in-memory version instead of a one-shot Redis flag, so every tab
  updates and polling costs no Redis commands.
- The app-wide display lock and the per-session render cache are removed.
- The "New Surveys" badge no longer re-downloads or empties the list.
- Uploads are queued jobs done by the worker, which re-checks JazzHR first and
  writes `UPLOADED` directly, so a stale check can't make a card offer Upload again.
- Timestamps are timezone-aware UTC (the worker and Render run in different time zones).
- Culture Index POST requests are no longer retried automatically.
- The file cache backend was removed; Redis is required. `KEY_PREFIX` allows
  isolated test runs.
- Found and fixed during testing: the CSV export needs a longer read timeout
  (it took over 30 s to start); a status upgraded from background to visible
  priority didn't wake the foreground threads (checks stalled); the first upload
  after a deploy wasn't noticed (missing `worker:version` key read as "not read
  yet"); the watcher now wakes immediately when an upload is queued; survey order
  is made deterministic so an unchanged export isn't rewritten; `POLL_INTERVAL_MS`
  renamed `UI_POLL_INTERVAL_MS` because the existing `.env` sets the old name to
  30 s; Dash pinned below 4 (Dash 4 checkbox markup hides the per-card checkboxes
  under the existing CSS; the CSS now handles both).

### 2026-09-25: fixes and cleanup

Exact-name JazzHR matching across duplicate applicant records; JazzHR failures no
longer cached as "Not in Jazz"; no size-only matches; Upload only on "Not
Uploaded" cards; logged-out access limited to the login callback; shared rate
limiter; report download fallback; `rebuild_cache.py` rewritten; constant-time
secret comparison; unused code, scripts, logs and the candidate CSV removed.
Pre-cleanup backup: `/home/senergyadmin/Culture_Index_tool_backup_20260925.tar.gz`
(mode 600; contains `.env` secrets and the candidate CSV).

## 12. Known issues

1. **Culture Index blocks Render,** which is why the worker exists. The VPS's IP
   could be blocked the same way; the portal would not be affected. Official API
   access or an allowlist from Culture Index remains the long-term fix.
2. Automated use of the portal's internal API may not be allowed by Culture
   Index's terms.
3. Status matching depends on names. A candidate whose name differs between
   Culture Index and JazzHR shows "Not in Jazz".
4. Search keeps only printable ASCII, so accented names cannot be searched.
5. The JazzHR budget is split by configuration (website 60, worker 15, rebuild
   30 per minute); running `rebuild_cache.py` during work hours can exceed
   JazzHR's 80/minute and slow everything down.

## 13. Cutover checklist

1. **VPS worker:** a folder with this code, a Python 3.11 virtual environment
   (`pip install -r requirements.txt`), and a `.env` with the worker settings
   (README). Run `python vps_worker.py export` once by hand, then add the three
   cron lines.
2. **Missing statuses:** the newest ~1,000 surveys have no production status
   (their old 2-hour entries expired after Render was blocked on 2026-09-25).
   They fill in as pages are viewed and through the background scan; to fill them
   all at once, run `python rebuild_cache.py` (option 2, resume) on the VPS outside
   work hours.
3. **Render:** push to GitHub; in the Render environment keep `REDIS_URL`,
   `JAZZHR_API_KEY`, `APP_USERNAME`, `APP_PASSWORD`, `SECRET_KEY`; remove
   `CULTUREINDEX_EMAIL`, `CULTUREINDEX_PASSWORD`, `BACKGROUND_JOB_SECRET`,
   `CACHE_BACKEND`, `POLL_INTERVAL_MS`; delete the `survey-sync-hourly` cron
   service; confirm the start command uses one worker.
4. **Afterwards:** the legacy keys `app:*`, `pdf_sizes:*` and `upload_lock:*`
   can be deleted from Upstash.

