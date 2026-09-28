"""One queue for every JazzHR status check the Render app makes.

Before this, every page render, button and background pass started its own
checks. They duplicated each other and competed for the same 80-calls-per-minute
JazzHR budget, so the page being looked at waited behind pages nobody was viewing.

Now:
- A survey is never queued or checked twice at the same time.
- Interactive checks (a Refresh click) go first, then the page being viewed.
  Both run on the foreground threads.
- Background refreshes run on their own thread with a lower rate ceiling, so
  they can never use up the budget a person is waiting on.
"""
import heapq
import itertools
from datetime import datetime, timezone
from threading import Condition, Thread
from typing import Callable, Dict, List, Optional, Set

from cache_storage import StatusStore, parse_timestamp
from check_jazzhr_uploads import JazzHRUploadChecker, RateLimiter
from ui_state import ChangeTracker, log

PRIORITY_INTERACTIVE = 0
PRIORITY_VISIBLE = 1
PRIORITY_BACKGROUND = 2


class StatusWorker:
    def __init__(
        self,
        api_key: str,
        status_store: StatusStore,
        tracker: ChangeTracker,
        rate_limiter: RateLimiter,
        background_calls_per_minute: int,
        recent_upload_lookup: Callable[[str], Optional[datetime]],
        foreground_threads: int = 3,
        diag: bool = False,
    ):
        """recent_upload_lookup(survey_id) returns when this app last queued an upload
        for the survey (or None). A check that started before that upload finished
        must not overwrite the worker's "Uploaded" with a stale "Not Uploaded"."""
        self.status_store = status_store
        self.tracker = tracker
        self.diag = diag
        self._recent_upload_lookup = recent_upload_lookup

        self._fg_checker = JazzHRUploadChecker(api_key, before_request=rate_limiter.wait)
        self._bg_checker = JazzHRUploadChecker(
            api_key, before_request=lambda: rate_limiter.wait(limit=background_calls_per_minute)
        )

        self._foreground_threads = foreground_threads
        self._reset_queue()

    def _reset_queue(self) -> None:
        self._cond = Condition()
        self._fg_heap: List = []
        self._bg_heap: List = []
        self._counter = itertools.count()
        self._pending: Dict[str, int] = {}      # survey_id -> best queued priority
        self._surveys: Dict[str, Dict] = {}     # survey_id -> survey dict to check
        self._in_flight: Set[str] = set()
        self._errors: Dict[str, str] = {}       # survey_id -> last check error (not cached in Redis)

    def start(self) -> None:
        """Start the worker threads in the calling process with a fresh queue.

        Called from the process that serves requests, not at import: under
        gunicorn --preload the app is imported in the parent and copied into the
        worker by fork, and threads do not survive a fork.
        """
        self._reset_queue()
        for i in range(self._foreground_threads):
            Thread(target=self._run, args=(False,), daemon=True, name=f"StatusFG{i}").start()
        Thread(target=self._run, args=(True,), daemon=True, name="StatusBG").start()

    def enqueue(self, surveys: List[Dict], priority: int) -> int:
        """Queue checks. A survey already queued keeps its better priority; one
        already being checked is skipped. Returns how many were newly queued."""
        added = 0
        pushed = False
        changed = []
        with self._cond:
            for survey in surveys:
                sid = str(survey.get('surveyId', ''))
                if not sid or sid in self._in_flight:
                    continue
                current = self._pending.get(sid)
                if current is not None and current <= priority:
                    continue
                self._pending[sid] = priority
                self._surveys[sid] = survey
                # Within "visible page" priority, the most recently opened page goes
                # first (last in, first out), so pages flicked past don't hold up the
                # page the user landed on. Other priorities stay first in, first out.
                order = next(self._counter)
                entry = (priority, -order if priority == PRIORITY_VISIBLE else order, sid)
                heapq.heappush(self._bg_heap if priority == PRIORITY_BACKGROUND else self._fg_heap, entry)
                pushed = True
                if current is None:
                    added += 1
                changed.append(sid)
            # Wake the threads for upgrades too: a survey moved from the background
            # queue to the foreground queue must be picked up by a foreground thread.
            if pushed:
                self._cond.notify_all()
        if changed and priority != PRIORITY_BACKGROUND:
            self.tracker.mark_changed(changed)   # show the "Checking" marker on those cards
        return added

    def is_busy(self, survey_id: str) -> bool:
        """Queued at foreground priority or being checked right now."""
        sid = str(survey_id)
        with self._cond:
            return sid in self._in_flight or self._pending.get(sid, PRIORITY_BACKGROUND) < PRIORITY_BACKGROUND

    def last_error(self, survey_id: str) -> Optional[str]:
        return self._errors.get(str(survey_id))

    def _next(self, background: bool) -> str:
        heap = self._bg_heap if background else self._fg_heap
        with self._cond:
            while True:
                while heap:
                    priority, _, sid = heapq.heappop(heap)
                    # Skip entries superseded by a better-priority copy or already handled.
                    if self._pending.get(sid) == priority:
                        del self._pending[sid]
                        self._in_flight.add(sid)
                        return sid
                self._cond.wait()

    def _run(self, background: bool) -> None:
        checker = self._bg_checker if background else self._fg_checker
        while True:
            sid = self._next(background)
            survey = self._surveys.pop(sid, None) or {}
            try:
                self._check(checker, sid, survey, background)
            except Exception as e:
                log(f"Status worker error for {sid}: {e}", "ERROR")
            finally:
                with self._cond:
                    self._in_flight.discard(sid)
                self.tracker.mark_changed([sid])

    def _check(self, checker: JazzHRUploadChecker, sid: str, survey: Dict, background: bool) -> None:
        started = datetime.now(timezone.utc)
        first_name = (survey.get('firstName') or '').strip()
        last_name = (survey.get('lastName') or '').strip()
        try:
            result = checker.check_survey_status(
                first_name, last_name, survey.get('surveyReportUrl'), survey.get('pdfSize'),
                verbose=self.diag and not background,
            )
        except Exception as e:
            # Keep the previous status on screen; remember the error in memory only,
            # so a JazzHR outage is never stored as a status.
            self._errors[sid] = str(e)[:180]
            log(f"JazzHR error for {sid}: {e}", "ERROR")
            return

        self._errors.pop(sid, None)
        result["timestamp"] = datetime.now(timezone.utc).isoformat()

        if result["status"] != "UPLOADED" and self._upload_may_have_finished_since(sid, started):
            log(f"Skipped stale status for {sid}: an upload was queued after this check started", "WARN")
            return

        if self.diag:
            # The extra read costs a Redis command per check, so only in diagnostic mode.
            previous = self.status_store.get(sid)
            if previous and previous.get('status') != result['status']:
                log(f"Status Change: {first_name} {last_name} ({sid}) {previous.get('status')} -> {result['status']}", "WARN")
            log(f"[DIAG] {first_name} {last_name} ({sid}): {result['status']} "
                f"applicant={result.get('applicantId')} of {result.get('applicant_count', 0)}, "
                f"files={result.get('file_count', 0)}, CI size={survey.get('pdfSize')}, "
                f"matched_by={(result.get('match') or {}).get('matched_by')}", "WARN")
        self.status_store.set(sid, result)

    def _upload_may_have_finished_since(self, sid: str, started: datetime) -> bool:
        queued_at = self._recent_upload_lookup(sid)
        if queued_at is None:
            return False
        existing = self.status_store.get(sid) or {}
        written = parse_timestamp(existing.get('timestamp'))
        uploaded_after_start = existing.get('status') == 'UPLOADED' and written is not None and written >= started
        return uploaded_after_start or queued_at >= started
