"""HR tool web app (Render): survey list, JazzHR status, upload requests.

This app never talks to Culture Index (Render's IP is blocked there). The VPS
worker (vps_worker.py) exports the survey list into Redis and performs uploads;
this app reads the list, checks JazzHR status itself, and queues uploads.

Runs as ONE gunicorn worker: the survey list, change tracker and status queue
live in process memory.
"""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import dash
from dash import Dash, html, dcc, Input, Output, State, callback, ctx, ALL, ClientsideFunction, clientside_callback
from dotenv import load_dotenv
from flask import redirect, request, jsonify
from flask_login import current_user

load_dotenv()

import shared_state as ss  # noqa: E402  (reads KEY_PREFIX, so after load_dotenv)
from auth import AuthManager  # noqa: E402
from cache_storage import StatusStore  # noqa: E402
from check_jazzhr_uploads import RateLimiter  # noqa: E402
from input_validation import sanitize_search_query, validate_page_number  # noqa: E402
from login_layout import create_login_layout  # noqa: E402
from status_worker import StatusWorker, PRIORITY_BACKGROUND, PRIORITY_INTERACTIVE, PRIORITY_VISIBLE  # noqa: E402
from survey_display import build_loading_result, build_error_result, build_empty_result, format_time_ago  # noqa: E402
from ui_state import ChangeTracker, NotificationState, RefreshState, log  # noqa: E402

ITEMS_PER_PAGE = int(os.getenv('ITEMS_PER_PAGE', '15'))
MAX_BATCH_UPLOAD = int(os.getenv('MAX_BATCH_UPLOAD', '15'))
MAX_BACKGROUND_CHECK = int(os.getenv('MAX_BACKGROUND_CHECK', '50'))
RECENT_SURVEY_THRESHOLD = int(os.getenv('RECENT_SURVEY_THRESHOLD', '1000'))
JAZZHR_CACHE_HOURS = float(os.getenv('JAZZHR_CACHE_HOURS', '2'))
JAZZHR_CALLS_PER_MINUTE = int(os.getenv('JAZZHR_CALLS_PER_MINUTE', '60'))
BACKGROUND_CALLS_PER_MINUTE = int(os.getenv('BACKGROUND_CALLS_PER_MINUTE', '35'))
POLL_INTERVAL_MS = int(os.getenv('UI_POLL_INTERVAL_MS', '3000'))
# The worker announces changes over pub/sub; this poll is only the safety net.
FALLBACK_POLL_SECONDS = int(os.getenv('FALLBACK_POLL_SECONDS', '300'))
ACTIVE_POLL_SECONDS = int(os.getenv('ACTIVE_POLL_SECONDS', '20'))
BACKGROUND_SCAN_MINUTES = int(os.getenv('BACKGROUND_SCAN_MINUTES', '10'))
DIAG_STATUS_CHECK = os.getenv('DIAG_STATUS_CHECK', '0') == '1'

CENTRAL_TZ = ZoneInfo("America/Chicago")


def _build_id() -> str:
    """Identifies the deployed code. Open tabs compare it once a minute
    (assets/version_check.js) and reload after a deploy, because a page loaded
    before a deploy keeps calling the old callbacks."""
    configured = os.getenv('RENDER_GIT_COMMIT') or os.getenv('APP_BUILD_ID')
    if configured:
        return configured[:12]
    here = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256()
    for folder in (here, os.path.join(here, 'assets')):
        for name in sorted(os.listdir(folder)):
            if name.endswith(('.py', '.js', '.css')):
                with open(os.path.join(folder, name), 'rb') as f:
                    digest.update(f.read())
    return digest.hexdigest()[:12]


BUILD_ID = _build_id()
WORK_DAYS = range(0, 5)
WORK_HOURS = range(7, 19)


def is_work_hours() -> bool:
    now = datetime.now(CENTRAL_TZ)
    return now.weekday() in WORK_DAYS and now.hour in WORK_HOURS


def friendly_time(value: Optional[str]) -> Optional[str]:
    """ "9:30 AM" today, "Sep 28, 9:30 AM" on other days (Central time)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(CENTRAL_TZ)
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == datetime.now(CENTRAL_TZ).date():
        return clock
    return f"{local.strftime('%b')} {local.day}, {clock}"


class SurveyStore:
    """The survey list from the latest worker snapshot, newest first."""

    def __init__(self):
        self._lock = Lock()
        self._surveys: List[Dict] = []
        self._by_id: Dict[str, Dict] = {}
        self.meta: Dict = {}
        self.loaded = False

    @property
    def content_hash(self) -> Optional[str]:
        return self.meta.get('hash') if self.loaded else None

    def replace(self, surveys: List[Dict], meta: Dict) -> None:
        by_id = {str(s['surveyId']): s for s in surveys}
        with self._lock:
            self._surveys = surveys
            self._by_id = by_id
            self.meta = meta
            self.loaded = True

    def update_meta(self, meta: Dict) -> None:
        with self._lock:
            self.meta = meta

    def all(self) -> List[Dict]:
        return self._surveys

    def get(self, survey_id: str) -> Optional[Dict]:
        return self._by_id.get(str(survey_id))

    def page(self, page_num: int, search_query: str) -> Tuple[List[Dict], int]:
        surveys = self._surveys
        if search_query and len(search_query) >= 2:
            query = search_query.lower()
            surveys = [s for s in surveys
                       if query in f"{s.get('firstName', '')} {s.get('lastName', '')}".lower()][:100]
        start = (page_num - 1) * ITEMS_PER_PAGE
        return surveys[start:start + ITEMS_PER_PAGE], len(surveys)


class RecentUploads:
    """When this app last queued an upload per survey (kept 24 hours)."""

    def __init__(self):
        self._lock = Lock()
        self._queued_at: Dict[str, datetime] = {}

    def record(self, survey_id: str) -> None:
        with self._lock:
            self._queued_at[str(survey_id)] = datetime.now(timezone.utc)

    def get(self, survey_id: str) -> Optional[datetime]:
        with self._lock:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            self._queued_at = {sid: t for sid, t in self._queued_at.items() if t >= cutoff}
            return self._queued_at.get(str(survey_id))

    def any_since(self, seconds: int) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        with self._lock:
            return any(t >= cutoff for t in self._queued_at.values())


redis_client = ss.connect()
status_store = StatusStore(redis_client, recent_threshold=RECENT_SURVEY_THRESHOLD, stale_hours=JAZZHR_CACHE_HOURS)
tracker = ChangeTracker()
notifications = NotificationState()
refresh_state = RefreshState()
survey_store = SurveyStore()
recent_uploads = RecentUploads()

jazzhr_api_key = os.getenv('JAZZHR_API_KEY')
if not jazzhr_api_key:
    log("JAZZHR_API_KEY not found!", "ERROR")

status_worker = StatusWorker(
    api_key=jazzhr_api_key,
    status_store=status_store,
    tracker=tracker,
    rate_limiter=RateLimiter(
        calls_per_minute=JAZZHR_CALLS_PER_MINUTE,
        on_wait=lambda seconds: log(f"JazzHR rate limit reached, waiting {seconds:.1f}s", "WARN"),
    ),
    background_calls_per_minute=BACKGROUND_CALLS_PER_MINUTE,
    recent_upload_lookup=recent_uploads.get,
    diag=DIAG_STATUS_CHECK,
)


class SnapshotWatcher:
    """Mirrors the worker's shared state into this process.

    The worker announces changes over pub/sub (EventListener), which wakes this
    watcher at once. On its own it ticks every 5 minutes as a safety net, or every
    20 seconds while a refresh or an upload queued here is in progress. One MGET
    per tick.
    """

    _UNREAD = object()   # distinct from None, which means the key doesn't exist yet

    def __init__(self):
        self._last_worker_version = self._UNREAD
        self._last_scan = 0.0
        self._wake = Event()
        self._tick_lock = Lock()
        self._thread: Optional[Thread] = None
        self.last_tick_at: Optional[str] = None
        self.last_error: Optional[str] = None

    def wake(self) -> None:
        """Switch to the fast upload pace now instead of after the current wait."""
        self._wake.set()

    def start(self) -> None:
        self._wake = Event()
        self._tick_lock = Lock()
        self._thread = Thread(target=self._loop, daemon=True, name="SnapshotWatcher")
        self._thread.start()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def ensure_running(self) -> None:
        """Restart the watcher thread if it has died after starting in this process."""
        ensure_background_threads()
        if not self.is_alive():
            log("Snapshot watcher was not running; restarting it", "ERROR")
            self.start()

    def load_now(self) -> None:
        """Load the snapshot from a page request when the list is still empty, so a
        stuck or dead watcher thread can never leave the site on "Waiting"."""
        self.ensure_running()
        if survey_store.loaded:
            return
        try:
            self.tick()
        except Exception as e:
            self.last_error = str(e)[:200]
            log(f"Snapshot load from a page request failed: {e}", "ERROR")

    def _interval(self) -> int:
        if refresh_state.is_active() or recent_uploads.any_since(15 * 60):
            return ACTIVE_POLL_SECONDS
        return FALLBACK_POLL_SECONDS

    def _loop(self) -> None:
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:
                    self.last_error = str(e)[:200]
                    log(f"Snapshot watcher error: {e}", "ERROR")
                self._wake.wait(self._interval())
                self._wake.clear()
        finally:
            log("Snapshot watcher thread stopped", "ERROR")

    def tick(self) -> None:
        with self._tick_lock:
            self._tick()
        self.last_tick_at = datetime.now(timezone.utc).isoformat()

    def _tick(self) -> None:
        meta_raw, worker_version, notification_raw, refresh_raw, heartbeat = redis_client.mget(
            ss.SNAPSHOT_META_KEY, ss.WORKER_VERSION_KEY, ss.NOTIFICATION_KEY,
            ss.REFRESH_STATUS_KEY, ss.HEARTBEAT_KEY,
        )
        refresh_state.set_status(ss.parse_json(refresh_raw))
        refresh_state.set_heartbeat(heartbeat)
        meta = ss.parse_json(meta_raw)
        list_changed = False

        if meta and meta.get('hash') != survey_store.content_hash:
            payload = redis_client.get(ss.SNAPSHOT_KEY)
            if payload:
                first_load = not survey_store.loaded
                surveys = ss.decode_snapshot(payload)
                survey_store.replace(surveys, meta)
                status_store.set_recent_surveys([s['surveyId'] for s in surveys])
                tracker.mark_list_changed()
                if first_load:
                    tracker.mark_all()
                list_changed = True
                log(f"Survey snapshot loaded: {len(surveys)} surveys (changed {meta.get('changed_at')})", "WARN")
        elif meta:
            if meta.get('checked_at') != survey_store.meta.get('checked_at'):
                survey_store.update_meta(meta)
                tracker.mark_all()   # refresh the "last checked" line on open pages

        notifications.set(ss.parse_json(notification_raw))

        if self._last_worker_version is not self._UNREAD and worker_version != self._last_worker_version:
            tracker.mark_all()   # the worker finished uploads; open pages re-render
        self._last_worker_version = worker_version

        scan_due = time.monotonic() - self._last_scan >= BACKGROUND_SCAN_MINUTES * 60
        if survey_store.loaded and is_work_hours() and (list_changed or scan_due):
            self._last_scan = time.monotonic()
            self.background_scan()

    def background_scan(self) -> None:
        """Queue background refreshes for the newest surveys whose status is missing or stale."""
        newest = survey_store.all()[:MAX_BACKGROUND_CHECK]
        statuses = status_store.get_batch([s['surveyId'] for s in newest])
        stale = [s for s in newest if status_store.is_stale(s['surveyId'], statuses.get(str(s['surveyId'])))]
        if stale:
            queued = status_worker.enqueue(stale, PRIORITY_BACKGROUND)
            log(f"Background scan: {len(stale)} of the newest {len(newest)} need a check ({queued} newly queued)", "WARN")


snapshot_watcher = SnapshotWatcher()


class EventListener:
    """Receives the worker's pub/sub events so progress, finished uploads and new
    surveys show up within seconds. Missed events are covered by the watcher's poll."""

    PING_SECONDS = 300   # detects a silently dropped connection; each ping is one Redis command

    def start(self) -> None:
        Thread(target=self._loop, daemon=True, name="EventListener").start()

    def _loop(self) -> None:
        while True:
            try:
                client = ss.connect(socket_timeout=None)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(ss.EVENTS_CHANNEL)
                last_ping = time.monotonic()
                while True:
                    message = pubsub.get_message(timeout=30)
                    if message and message.get("type") == "message":
                        self._handle(ss.parse_json(message.get("data")) or {})
                    if time.monotonic() - last_ping >= self.PING_SECONDS:
                        pubsub.ping()
                        last_ping = time.monotonic()
            except Exception as e:
                log(f"Event listener error: {e}; reconnecting in 10s", "ERROR")
                time.sleep(10)

    def _handle(self, event: Dict) -> None:
        kind = event.get("type")
        if kind == "refresh":
            refresh_state.set_status(event.get("status"))
        elif kind == "upload" and event.get("survey_id"):
            tracker.mark_changed([event["survey_id"]])
        elif kind == "snapshot":
            snapshot_watcher.wake()


event_listener = EventListener()

_background_lock = Lock()
_background_pid: Optional[int] = None


def ensure_background_threads() -> None:
    """Start the status and snapshot threads once per process, in the process
    that handles requests. Starting them at import breaks under gunicorn
    --preload: the import runs in the parent, the worker is a fork of it, and
    threads don't survive the fork."""
    global _background_pid
    if _background_pid == os.getpid():
        return
    with _background_lock:
        if _background_pid == os.getpid():
            return
        status_worker.start()
        snapshot_watcher.start()
        event_listener.start()
        _background_pid = os.getpid()
        log(f"Background threads started in process {os.getpid()}", "WARN")

app = Dash(__name__, suppress_callback_exceptions=True, update_title=None)
app.title = "Culture Index - HR Tool"
server = app.server
auth_manager = AuthManager(server, redis_client=redis_client)


@server.route('/health')
def health_check():
    if not survey_store.loaded:
        snapshot_watcher.load_now()
    redis_ok = status_store.ping()
    status, code = ("ok", 200) if redis_ok and survey_store.loaded else (("degraded", 200) if redis_ok else ("error", 503))
    return jsonify({"status": status, "timestamp": datetime.now(timezone.utc).isoformat()}), code


@server.route('/health/detailed')
def health_check_detailed():
    if not current_user.is_authenticated:
        return jsonify({"error": "Authentication required"}), 401
    return jsonify({
        "redis": status_store.ping(),
        "surveys_loaded": survey_store.loaded,
        "survey_count": len(survey_store.all()),
        "snapshot": survey_store.meta,
        "process_id": os.getpid(),
        "watcher_alive": snapshot_watcher.is_alive(),
        "watcher_last_tick": snapshot_watcher.last_tick_at,
        "watcher_last_error": snapshot_watcher.last_error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@server.route('/health/ready')
def readiness_check():
    if not survey_store.loaded:
        return jsonify({"ready": False, "reason": "No survey snapshot yet"}), 503
    if not status_store.ping():
        return jsonify({"ready": False, "reason": "Redis not connected"}), 503
    return jsonify({"ready": True})


@server.route('/health/live')
def liveness_check():
    return jsonify({"alive": True})


@server.route('/version')
def version():
    response = jsonify({"build": BUILD_ID})
    response.headers['Cache-Control'] = 'no-store'
    return response


@server.route('/login')
def login():
    if current_user.is_authenticated:
        return redirect('/')
    return app.index()


@server.route('/logout')
def logout():
    auth_manager.logout()
    return redirect('/login')


PUBLIC_PATH_PREFIXES = (
    '/login', '/logout',
    '/health', '/version',
    '/assets/', '/_dash-component-suites/',
    '/_dash-layout', '/_dash-dependencies', '/_reload-hash', '/_favicon.ico',
)


@server.before_request
def start_background_threads():
    ensure_background_threads()
    return None


@server.before_request
def require_login():
    if current_user.is_authenticated or request.path.startswith(PUBLIC_PATH_PREFIXES):
        return None

    if request.path.startswith('/_dash-update-component'):
        # Logged-out visitors may only run the login callback. Every other
        # callback returns candidate data or triggers uploads.
        body = request.get_json(silent=True) or {}
        if 'login-url.pathname' in str(body.get('output', '')):
            return None
        return jsonify({"error": "Authentication required"}), 401

    return redirect('/login?next=' + request.path)


def serve_layout():
    if not current_user.is_authenticated:
        return create_login_layout()

    return html.Div([
        html.Div([
            html.Div([
                html.Div([
                    html.Img(src="/assets/SENERGY-Logo_Icon-Yellow.png", className="header-logo"),
                    html.Div(
                        id="new-surveys-banner",
                        children=[
                            html.Span("New Surveys: ", className="badge-label"),
                            html.Span("0", className="badge-count")
                        ],
                        className="surveys-badge no-new-surveys",
                        n_clicks=0
                    ),
                ], className="header-left"),
                html.Div([
                    html.Button("Upload Selected", id="upload-btn", className="upload-btn", n_clicks=0, disabled=True),
                    html.Button("Refresh JazzHR", id="refresh-jazzhr-btn", className="refresh-btn", n_clicks=0,
                                title="Re-check JazzHR for every survey on this page"),
                    html.Button("Refresh Surveys", id="refresh-surveys-btn", className="refresh-btn", n_clicks=0,
                                title="Ask the worker to fetch the latest survey list from Culture Index"),
                    html.Div(className="header-separator"),
                    html.A("Logout", href="/logout", className="refresh-btn", style={"textDecoration": "none", "display": "flex", "alignItems": "center"}),
                ], className="header-right"),
            ], className="header"),

            html.Div([
                html.Div([
                    html.Div([
                        html.Div([
                            html.Span("SURVEYS", className="panel-label"),
                            html.Span(id="selection-count", className="selection-count"),

                            html.Div([
                                dcc.Input(
                                    id="search-input",
                                    type="text",
                                    placeholder="Search by name",
                                    debounce=False,
                                    className="search-input"
                                ),
                                html.Button("Clear", id="clear-search-btn", className="clear-search-btn", n_clicks=0),
                            ], className="search-container"),

                            html.Div([
                                html.Div(id="upload-status", className="upload-status"),
                                dcc.Checklist(
                                    id="select-all-checkbox",
                                    options=[{"label": " Select All Uploadable", "value": "all"}],
                                    value=[],
                                    className="select-all-checkbox"
                                ),
                            ], className="select-all-container"),
                        ], className="panel-header"),

                        html.Div(id="loading-indicator", className="loading-indicator", style={"display": "none"}),
                    ], className="surveys-header"),

                    html.Div([
                        html.Div([
                            html.Span(id="rp-title", className="refresh-progress__title"),
                            html.Span(id="rp-elapsed", className="refresh-progress__elapsed"),
                        ], className="refresh-progress__head"),
                        html.Div(html.Div(id="rp-fill", className="refresh-progress__fill"), className="refresh-progress__track"),
                    ], id="refresh-progress", className="refresh-progress is-hidden"),

                    html.Div([
                        html.Div([
                            html.Span(id="jp-title", className="refresh-progress__title"),
                            html.Span(id="jp-count", className="refresh-progress__elapsed"),
                        ], className="refresh-progress__head"),
                        html.Div(html.Div(id="jp-fill", className="refresh-progress__fill"), className="refresh-progress__track"),
                    ], id="jazzhr-progress", className="refresh-progress is-hidden"),

                    html.Div([
                        html.Div([
                            html.Button("<- Previous", id="prev-page-btn-top", className="pagination-btn", n_clicks=0),
                            html.Div(id="page-info-top", className="page-info"),
                            html.Button("Next ->", id="next-page-btn-top", className="pagination-btn", n_clicks=0),
                        ], className="pagination-center"),
                    ], className="pagination-controls pagination-controls--top"),

                    dcc.Loading(
                        id="surveys-loading",
                        type="default",
                        # Renders from memory take milliseconds; only show the bar for slow ones
                        # so status updates don't flash it.
                        delay_show=500,
                        custom_spinner=html.Div([
                            html.Div(html.Div(className="progress-fill"), className="progress-track"),
                            html.Span("Working", className="progress-label"),
                        ], className="progress-wrap"),
                        children=html.Div(id="surveys-container", className="surveys-list"),
                    ),

                    html.Div([
                        html.Div([
                            html.Button("<- Previous", id="prev-page-btn", className="pagination-btn", n_clicks=0),
                            html.Div(id="page-info", className="page-info"),
                            html.Button("Next ->", id="next-page-btn", className="pagination-btn", n_clicks=0),
                        ], className="pagination-center"),
                        html.Div(id="last-updated", className="last-updated"),
                    ], className="pagination-controls"),
                ], className="panel surveys-panel full-width"),
            ], className="main-content"),
        ], className="app-container"),

        dcc.Store(id="current-page", data=1),
        dcc.Store(id="search-query", data=""),
        dcc.Store(id="selected-ids", data=[]),
        dcc.Store(id="current-surveys-data", data=[]),
        dcc.Store(id="uploadable-ids", data=[]),
        dcc.Store(id="render-trigger", data=0),
        dcc.Store(id="seen-version", data=0),
        dcc.Store(id="notification-data", data={"count": 0}),
        dcc.Store(id="refresh-data", data={}),
        dcc.Store(id="jazzhr-batch", data=None),
        dcc.Store(id="jazzhr-progress-data", data={}),

        # Polls this process's memory only (no Redis). Dash pauses it in hidden tabs.
        dcc.Interval(id="poll-interval", interval=POLL_INTERVAL_MS, n_intervals=0),
        # Animates the refresh progress card in the browser; enabled only while it shows.
        dcc.Interval(id="progress-tick", interval=1000, n_intervals=0, disabled=True),
        dcc.Interval(id="jazzhr-progress-tick", interval=1000, n_intervals=0, disabled=True),
    ])


app.layout = serve_layout


def _status_indicator(entry: Optional[Dict]) -> html.Div:
    if not entry:
        text, class_name = "Checking", "status-checking"
    else:
        status = entry.get('status')
        text, class_name = {
            "NOT_UPLOADED": ("Not Uploaded", "status-missing"),
            "NOT_IN_JAZZHR": ("Not in Jazz", "status-not-found"),
            "MISSING_NAME": ("No Name", "status-pending"),
            "NO_PDF_URL": ("No URL", "status-pending"),
            "ERROR": ("Error", "status-error"),
        }.get(status, ("Unknown", "status-pending"))
        if entry.get('isUploaded') or status == "UPLOADED":
            text, class_name = "Uploaded", "status-uploaded"
    return html.Div([html.Span(text, className="status-text")], className=class_name)


def _card_action(job: Optional[Dict], busy: bool, last_error: Optional[str]) -> Tuple[str, List]:
    """The small status line in a card's footer: upload progress, a running check, or the last error."""
    def parts(kind, message, detail=None):
        content = []
        if kind in ("refreshing", "uploading", "queued"):
            content.append(html.Span(className="card-action-spinner"))
        content.append(html.Span(message, className="card-action-message"))
        if detail:
            content.append(html.Span(detail, className="card-action-detail"))
        return f"card-action-status card-action-status--{kind}", content

    state = (job or {}).get('state')
    if state == ss.JOB_QUEUED:
        return parts("queued", "Queued for upload")
    if state == ss.JOB_UPLOADING:
        return parts("uploading", "Uploading")
    if state == ss.JOB_FAILED:
        return parts("error", "Upload failed", job.get('error'))
    if busy:
        return parts("refreshing", "Checking JazzHR")
    if last_error:
        return parts("error", "Last check failed", last_error)
    return "card-action-status", []


def _footer_time(entry: Optional[Dict], job: Optional[Dict]) -> str:
    """ "Uploaded 3 minutes ago" right after an upload from this tool, otherwise
    when JazzHR was last checked. The status already says Uploaded, so a finished
    upload needs no separate message."""
    def ago(value):
        text = format_time_ago(value)
        return text[0].lower() + text[1:] if text else text

    if job and job.get('state') == ss.JOB_DONE and str(job.get('message', '')).startswith('Uploaded'):
        return f"Uploaded {ago(job.get('updated_at'))}"
    if entry and entry.get('timestamp'):
        return f"Checked {ago(entry['timestamp'])}"
    return "Never checked"


def build_card(survey: Dict, entry: Optional[Dict], job: Optional[Dict], busy: bool,
               last_error: Optional[str], selected: bool) -> Tuple[html.Div, bool]:
    survey_id = str(survey['surveyId'])
    url = survey.get('surveyReportUrl')
    job_state = (job or {}).get('state')

    # Offer Upload only when the report is known to be missing and no upload for it
    # is queued, running or just finished.
    is_uploadable = bool(
        entry and entry.get('status') == "NOT_UPLOADED" and entry.get('applicantId') and url
        and job_state not in (ss.JOB_QUEUED, ss.JOB_UPLOADING, ss.JOB_DONE)
    )

    pdf_size = survey.get('pdfSize')
    pdf_size_mb = pdf_size / (1024 * 1024) if pdf_size else None
    full_name = f"{survey.get('firstName', '')} {survey.get('lastName', '')}".strip() or "Unknown"
    position = (survey.get('position') or '').strip()
    action_class, action_children = _card_action(job, busy, last_error)

    card_children = [
        html.Div([
            html.Div([
                html.Span(full_name, className="survey-name"),
                html.Span(" | ", className="survey-separator") if position else None,
                html.Span(position, className="survey-position") if position else None,
                html.Span(survey.get('traitPattern') or 'N/A', className="trait-badge"),
                html.Span(survey.get("surveyDate") or "N/A", className="survey-date"),
            ], className="survey-name-row"),
            _status_indicator(entry),
        ], className="survey-item-header"),

        html.Div([
            html.Div([html.Span("EMAIL", className="info-label"), html.Span(survey.get("email") or "N/A", className="info-value")], className="info-field"),
            html.Div([html.Span("PHONE", className="info-label"), html.Span(survey.get("phoneNumber") or "N/A", className="info-value")], className="info-field"),
            html.Div([html.Span("SURVEY ID", className="info-label"), html.Span(survey_id, className="info-value")], className="info-field"),
            html.Div([
                html.Span("REPORT", className="info-label"),
                html.A(f"View PDF ({pdf_size_mb:.2f} MB)" if pdf_size_mb else "View PDF", href=url, target="_blank", className="report-link") if url else html.Span("N/A", className="info-value"),
            ], className="info-field"),
        ], className="survey-info"),

        html.Div([
            html.Span(_footer_time(entry, job), className="last-checked-value"),
            html.Div(action_children, className=action_class),
            html.Button(
                "Refresh Status",
                id={"type": "refresh-single-btn", "index": survey_id},
                n_clicks=0,
                className="refresh-single-btn",
                title="Check JazzHR status for this profile"
            ),
            html.Button("Upload", id={"type": "upload-single-btn", "index": survey_id}, n_clicks=0,
                        className="upload-single-btn") if is_uploadable else None,
        ], className="survey-footer"),
    ]

    if is_uploadable:
        card_children.append(html.Div([
            dcc.Checklist(id={"type": "survey-checkbox", "index": survey_id},
                          options=[{"label": "", "value": survey_id}],
                          value=[survey_id] if selected else [],
                          className="survey-checkbox-overlay")
        ], className="survey-checkbox-overlay-container"))

    return html.Div(card_children, className="survey-item"), is_uploadable


def queue_upload(survey_id: str) -> Tuple[bool, str]:
    survey = survey_store.get(survey_id)
    if not survey or not survey.get('surveyReportUrl'):
        return False, "Survey not found"
    queued, message = ss.enqueue_upload(
        redis_client, survey_id, survey.get('firstName', ''), survey.get('lastName', ''), survey['surveyReportUrl']
    )
    if queued:
        recent_uploads.record(survey_id)
        tracker.mark_changed([survey_id])
        snapshot_watcher.wake()
        log(f"Upload queued for {survey_id}", "WARN")
    return queued, message


def _triggered_index() -> Optional[str]:
    """The pattern-matching id index of the button that fired, if it was really clicked."""
    triggered = ctx.triggered[0] if ctx.triggered else {}
    if not triggered.get('value') or '.n_clicks' not in triggered.get('prop_id', ''):
        return None
    try:
        return str(json.loads(triggered['prop_id'].rsplit('.', 1)[0]).get('index'))
    except (ValueError, AttributeError):
        return None


@callback(
    [Output("surveys-container", "children"),
     Output("page-info", "children"),
     Output("prev-page-btn", "disabled"),
     Output("next-page-btn", "disabled"),
     Output("last-updated", "children"),
     Output("current-surveys-data", "data"),
     Output("uploadable-ids", "data"),
     Output("loading-indicator", "style"),
     Output("seen-version", "data"),
     Output("page-info-top", "children"),
     Output("prev-page-btn-top", "disabled"),
     Output("next-page-btn-top", "disabled")],
    [Input("current-page", "data"),
     Input("search-query", "data"),
     Input("render-trigger", "data")],
    State("selected-ids", "data"),
    prevent_initial_call=False
)
def display_surveys(page, search_query, render_trigger, selected_ids):
    result = _render_surveys(page, search_query, selected_ids)
    # The top navigator mirrors the bottom one: page info, previous/next disabled.
    return result + (result[1], result[2], result[3])


def _render_surveys(page, search_query, selected_ids):
    # Read the version before the data: any change after this point makes the
    # next poll re-render again, so nothing is missed.
    version = tracker.version
    is_valid, page, _ = validate_page_number(page)
    search_query = sanitize_search_query(search_query or "", max_length=100)

    if not survey_store.loaded:
        snapshot_watcher.load_now()
    if not survey_store.loaded:
        return build_loading_result("Waiting for the survey list from the worker") + (version,)

    surveys, total_count = survey_store.page(page, search_query)
    if not surveys:
        return build_empty_result(search_query) + (version,)

    survey_ids = [str(s['surveyId']) for s in surveys]
    try:
        statuses = status_store.get_batch(survey_ids)
        jobs = ss.get_jobs(redis_client, survey_ids)
    except Exception as e:
        log(f"Redis read failed while rendering: {e}", "ERROR")
        return build_error_result("Unable to read statuses right now. Please try again shortly.") + (version,)

    stale = [s for s in surveys if status_store.is_stale(s['surveyId'], statuses.get(str(s['surveyId'])))]
    if stale:
        status_worker.enqueue(stale, PRIORITY_VISIBLE)

    selected = set(selected_ids or [])
    cards, uploadable_ids = [], []
    for survey in surveys:
        sid = str(survey['surveyId'])
        card, uploadable = build_card(
            survey, statuses.get(sid), jobs.get(sid), status_worker.is_busy(sid),
            status_worker.last_error(sid), sid in selected,
        )
        cards.append(card)
        if uploadable:
            uploadable_ids.append(sid)

    total_pages = max(1, (total_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    if search_query:
        page_info = f"Page {page} of {total_pages} ({total_count} results for '{search_query}')"
    else:
        page_info = f"Page {page} of {total_pages} ({total_count:,} surveys)"

    meta = survey_store.meta
    checked = friendly_time(meta.get('checked_at') or meta.get('changed_at'))
    updated = f"Last checked {checked}" if checked else ""

    return (cards, page_info, page <= 1, page >= total_pages, updated,
            survey_ids, uploadable_ids, {"display": "none"}, version)


@callback(
    [Output("render-trigger", "data", allow_duplicate=True),
     Output("notification-data", "data"),
     Output("refresh-data", "data"),
     Output("jazzhr-progress-data", "data")],
    Input("poll-interval", "n_intervals"),
    [State("seen-version", "data"),
     State("current-surveys-data", "data"),
     State("render-trigger", "data"),
     State("notification-data", "data"),
     State("refresh-data", "data"),
     State("jazzhr-batch", "data"),
     State("jazzhr-progress-data", "data")],
    prevent_initial_call=True
)
def poll(_n, seen_version, page_ids, render_trigger, notification_shown, refresh_shown, jazzhr_batch, jazzhr_shown):
    notification = notifications.get()
    notification_out = notification if notification != notification_shown else dash.no_update
    refresh_out = _refresh_payload(refresh_shown)
    jazzhr_out = _jazzhr_payload(jazzhr_batch, jazzhr_shown)

    seen_version = seen_version or 0
    if tracker.version == seen_version:
        return dash.no_update, notification_out, refresh_out, jazzhr_out
    page_ids = page_ids or []
    needs_render = tracker.page_changed_since(page_ids, seen_version) or (
        not page_ids and tracker.list_version > seen_version
    )
    return ((render_trigger or 0) + 1 if needs_render else dash.no_update), notification_out, refresh_out, jazzhr_out


def _jazzhr_payload(batch_id: Optional[str], shown: Optional[Dict]):
    """Progress of this tab's last "Refresh JazzHR" click, or no_update if unchanged."""
    progress = status_worker.batch_progress(batch_id)
    if not progress:
        return dash.no_update if not shown else {}
    if shown and {k: v for k, v in shown.items() if k != "server_ms"} == progress:
        return dash.no_update
    return dict(progress, server_ms=int(time.time() * 1000))


def _refresh_payload(shown: Optional[Dict]):
    """The progress card's data, or no_update if the tab already has it. server_ms
    lets the browser correct for its own clock."""
    view = refresh_state.view()
    if shown and {k: v for k, v in shown.items() if k != "server_ms"} == view:
        return dash.no_update
    return dict(view, server_ms=int(time.time() * 1000))


clientside_callback(
    ClientsideFunction(namespace="refreshProgress", function_name="renderJazzhr"),
    [Output("jazzhr-progress", "className"),
     Output("jp-title", "children"),
     Output("jp-count", "children"),
     Output("jp-fill", "style"),
     Output("jazzhr-progress-tick", "disabled")],
    [Input("jazzhr-progress-tick", "n_intervals"),
     Input("jazzhr-progress-data", "data")],
)


clientside_callback(
    ClientsideFunction(namespace="refreshProgress", function_name="render"),
    [Output("refresh-progress", "className"),
     Output("rp-title", "children"),
     Output("rp-elapsed", "children"),
     Output("rp-fill", "style"),
     Output("progress-tick", "disabled")],
    [Input("progress-tick", "n_intervals"),
     Input("refresh-data", "data")],
)


@callback(
    [Output("login-url", "pathname"),
     Output("login-error-message", "children"),
     Output("login-error-message", "style")],
    [Input("login-submit-btn", "n_clicks"),
     Input("login-username-input", "n_submit"),
     Input("login-password-input", "n_submit")],
    [State("login-username-input", "value"),
     State("login-password-input", "value")],
    prevent_initial_call=True
)
def handle_login_callback(n_clicks, username_submit, password_submit, username, password):
    if current_user.is_authenticated:
        return "/", "", {"display": "none"}
    if not username or not password:
        return dash.no_update, "Please enter both username and password", {"display": "block"}
    success, message = auth_manager.attempt_login(username, password)
    if success:
        return "/", "", {"display": "none"}
    return dash.no_update, message, {"display": "block"}


@callback(
    Output("current-page", "data"),
    [Input("prev-page-btn", "n_clicks"),
     Input("next-page-btn", "n_clicks"),
     Input("prev-page-btn-top", "n_clicks"),
     Input("next-page-btn-top", "n_clicks")],
    State("current-page", "data"),
    prevent_initial_call=True
)
def handle_pagination(*args):
    current_page = args[-1]
    if ctx.triggered_id in ("prev-page-btn", "prev-page-btn-top"):
        return max(1, (current_page or 1) - 1)
    if ctx.triggered_id in ("next-page-btn", "next-page-btn-top"):
        return (current_page or 1) + 1
    return dash.no_update


@callback(
    [Output("search-query", "data"),
     Output("current-page", "data", allow_duplicate=True),
     Output("search-input", "value")],
    [Input("search-input", "value"),
     Input("clear-search-btn", "n_clicks")],
    prevent_initial_call=True
)
def handle_search(search_value, clear_clicks):
    if ctx.triggered_id == "clear-search-btn":
        return "", 1, ""
    return sanitize_search_query(search_value or "", max_length=100), 1, dash.no_update


@callback(
    [Output("new-surveys-banner", "children"),
     Output("new-surveys-banner", "className")],
    Input("notification-data", "data"),
    prevent_initial_call=False
)
def show_notification_banner(notification):
    count = int((notification or {}).get("count") or 0)
    if count <= 0:
        return [html.Span("New Surveys: ", className="badge-label"), html.Span("0", className="badge-count")], "surveys-badge no-new-surveys"
    time_str = friendly_time(notification.get("timestamp")) or "recently"
    return [
        html.Span("New Surveys: ", className="badge-label"),
        html.Span(str(count), className="badge-count pulse"),
        html.Span(f" ({time_str})", className="badge-time"),
    ], "surveys-badge has-new-surveys"


@callback(
    [Output("current-page", "data", allow_duplicate=True),
     Output("search-query", "data", allow_duplicate=True),
     Output("search-input", "value", allow_duplicate=True),
     Output("render-trigger", "data", allow_duplicate=True),
     Output("notification-data", "data", allow_duplicate=True)],
    Input("new-surveys-banner", "n_clicks"),
    [State("notification-data", "data"),
     State("render-trigger", "data")],
    prevent_initial_call=True
)
def handle_banner_click(n_clicks, notification, render_trigger):
    """The new surveys are already in memory (the watcher loaded them); just
    clear the badge and show page 1."""
    if not n_clicks or not (notification or {}).get("count"):
        return (dash.no_update,) * 5
    try:
        redis_client.delete(ss.NOTIFICATION_KEY)
    except Exception as e:
        log(f"Could not clear the notification in Redis: {e}", "ERROR")
    notifications.set(None)
    return 1, "", "", (render_trigger or 0) + 1, {"count": 0}


@callback(
    [Output("render-trigger", "data", allow_duplicate=True),
     Output("jazzhr-batch", "data"),
     Output("jazzhr-progress-data", "data", allow_duplicate=True)],
    Input("refresh-jazzhr-btn", "n_clicks"),
    [State("current-surveys-data", "data"),
     State("render-trigger", "data")],
    prevent_initial_call=True
)
def handle_refresh_jazzhr(n_clicks, page_ids, render_trigger):
    """Re-check every survey on this page; the JazzHR progress bar follows the batch."""
    if not n_clicks:
        return dash.no_update, dash.no_update, dash.no_update
    surveys = [s for s in (survey_store.get(sid) for sid in (page_ids or [])) if s]
    if not surveys:
        return dash.no_update, dash.no_update, dash.no_update
    batch_id = status_worker.start_batch([s['surveyId'] for s in surveys])
    queued = status_worker.enqueue(surveys, PRIORITY_INTERACTIVE)
    log(f"Refresh JazzHR: {queued} of {len(surveys)} surveys queued", "WARN")
    return (render_trigger or 0) + 1, batch_id, _jazzhr_payload(batch_id, None)


@callback(
    Output("render-trigger", "data", allow_duplicate=True),
    Input({"type": "refresh-single-btn", "index": ALL}, "n_clicks"),
    State("render-trigger", "data"),
    prevent_initial_call=True
)
def handle_refresh_single(_clicks, render_trigger):
    survey_id = _triggered_index()
    survey = survey_store.get(survey_id) if survey_id else None
    if not survey:
        return dash.no_update
    status_worker.enqueue([survey], PRIORITY_INTERACTIVE)
    return (render_trigger or 0) + 1


@callback(
    [Output("upload-status", "children", allow_duplicate=True),
     Output("refresh-data", "data", allow_duplicate=True)],
    Input("refresh-surveys-btn", "n_clicks"),
    State("refresh-data", "data"),
    prevent_initial_call=True
)
def handle_refresh_surveys(n_clicks, refresh_shown):
    """Queue a refresh; the progress card shows the rest. A click while one is
    already running just keeps showing that one."""
    if not n_clicks:
        return dash.no_update, dash.no_update
    try:
        _, status = ss.request_survey_refresh(redis_client)
    except Exception as e:
        log(f"Survey refresh request failed: {e}", "ERROR")
        return "Could not request a survey refresh. Please try again.", dash.no_update
    refresh_state.set_status(status)
    return "", _refresh_payload(refresh_shown)


@callback(
    Output("selected-ids", "data"),
    Input({"type": "survey-checkbox", "index": ALL}, "value"),
    [State({"type": "survey-checkbox", "index": ALL}, "id"),
     State("selected-ids", "data")],
    prevent_initial_call=True
)
def track_selection(values, checkbox_ids, selected_ids):
    """Selections are kept per survey, so re-rendering a page (for a status update)
    or switching pages doesn't lose them."""
    page_ids = {cid["index"] for cid in (checkbox_ids or [])}
    checked = {v[0] for v in (values or []) if v}
    updated = (set(selected_ids or []) - page_ids) | checked
    return sorted(updated)


@callback(
    [Output("upload-btn", "disabled"),
     Output("selection-count", "children")],
    Input("selected-ids", "data"),
    prevent_initial_call=False
)
def update_selection_count(selected_ids):
    count = len(selected_ids or [])
    if count == 0:
        return True, ""
    if count > MAX_BATCH_UPLOAD:
        return True, f"({count} selected - max {MAX_BATCH_UPLOAD})"
    return False, f"({count} selected)"


@callback(
    Output({"type": "survey-checkbox", "index": ALL}, "value"),
    Input("select-all-checkbox", "value"),
    [State("uploadable-ids", "data"),
     State({"type": "survey-checkbox", "index": ALL}, "id")],
    prevent_initial_call=True
)
def handle_select_all(select_all_value, uploadable_ids, checkbox_ids):
    select = "all" in (select_all_value or [])
    uploadable = set(uploadable_ids or [])
    return [[cid["index"]] if select and cid["index"] in uploadable else [] for cid in (checkbox_ids or [])]


@callback(
    [Output("selected-ids", "data", allow_duplicate=True),
     Output("select-all-checkbox", "value"),
     Output("upload-status", "children", allow_duplicate=True),
     Output("render-trigger", "data", allow_duplicate=True)],
    Input("upload-btn", "n_clicks"),
    [State("selected-ids", "data"),
     State("render-trigger", "data")],
    prevent_initial_call=True
)
def handle_upload_selected(n_clicks, selected_ids, render_trigger):
    selected = list(selected_ids or [])
    if not n_clicks or not selected:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    if len(selected) > MAX_BATCH_UPLOAD:
        return dash.no_update, dash.no_update, f"Max {MAX_BATCH_UPLOAD} at a time", dash.no_update

    queued = 0
    for survey_id in selected:
        try:
            ok, _ = queue_upload(survey_id)
            queued += ok
        except Exception as e:
            log(f"Could not queue upload for {survey_id}: {e}", "ERROR")
    message = f"{queued} upload(s) queued" + (f", {len(selected) - queued} skipped" if queued < len(selected) else "")
    return [], [], message, (render_trigger or 0) + 1


@callback(
    [Output("upload-status", "children", allow_duplicate=True),
     Output("render-trigger", "data", allow_duplicate=True)],
    Input({"type": "upload-single-btn", "index": ALL}, "n_clicks"),
    State("render-trigger", "data"),
    prevent_initial_call=True
)
def handle_single_upload(_clicks, render_trigger):
    survey_id = _triggered_index()
    if not survey_id:
        return dash.no_update, dash.no_update
    try:
        _, message = queue_upload(survey_id)
    except Exception as e:
        log(f"Could not queue upload for {survey_id}: {e}", "ERROR")
        message = "Could not queue the upload. Please try again."
    return message, (render_trigger or 0) + 1


if __name__ == "__main__":
    app.run(debug=False, port=8051)
