"""In-process state that browser tabs poll: what changed, and the new-surveys badge.

Tabs poll this every few seconds. It lives in memory (the app runs one worker),
so polling costs no Redis commands; background threads update it.
"""
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Iterable, Optional


def log(message: str, level: str = "INFO") -> None:
    if level not in ("ERROR", "WARN", "PERF"):
        return
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{level}] {message}", flush=True)


class ChangeTracker:
    """A version counter plus the version at which each survey last changed.

    Each tab remembers the version it last rendered. A tab re-renders only when a
    survey on its page changed since then, or when mark_all() was called.
    """

    def __init__(self):
        self._lock = Lock()
        self._version = 0
        self._all_version = 0
        self._list_version = 0
        self._survey_versions: Dict[str, int] = {}

    @property
    def version(self) -> int:
        return self._version

    @property
    def list_version(self) -> int:
        return self._list_version

    def mark_changed(self, survey_ids: Iterable[str]) -> None:
        with self._lock:
            self._version += 1
            for sid in survey_ids:
                self._survey_versions[str(sid)] = self._version

    def mark_all(self) -> None:
        """Every open page should re-render (for example after uploads finished)."""
        with self._lock:
            self._version += 1
            self._all_version = self._version

    def mark_list_changed(self) -> None:
        """A new survey list arrived. Pages don't re-render for this on their own
        (the list would shift under the user); the new-surveys badge covers it."""
        with self._lock:
            self._version += 1
            self._list_version = self._version

    def page_changed_since(self, survey_ids: Iterable[str], seen_version: int) -> bool:
        with self._lock:
            if self._all_version > seen_version:
                return True
            return any(self._survey_versions.get(str(sid), 0) > seen_version for sid in survey_ids)


class NotificationState:
    """The latest new-surveys notification, mirrored from Redis by the snapshot watcher."""

    def __init__(self):
        self._lock = Lock()
        self._notification: Optional[Dict] = None

    def set(self, notification: Optional[Dict]) -> None:
        with self._lock:
            self._notification = notification if notification and not notification.get("acknowledged") else None

    def get(self) -> Dict:
        """Only what the badge shows; the survey ID lists stay server-side."""
        with self._lock:
            if not self._notification:
                return {"count": 0}
            return {"count": self._notification.get("count", 0), "timestamp": self._notification.get("timestamp")}


_REFRESH_STAGE_ORDER = {"requested": 0, "running": 1, "done": 2, "failed": 2}


def _epoch_ms(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class RefreshState:
    """The latest survey refresh (from pub/sub events and the fallback poll) and the
    worker's heartbeat, shaped for the progress bar in the browser."""

    def __init__(self):
        self._lock = Lock()
        self._status: Optional[Dict] = None
        self._heartbeat: Optional[str] = None

    def set_status(self, status: Optional[Dict]) -> None:
        if not status:
            return
        with self._lock:
            current = self._status
            # The poll can read an older stage than an event that just arrived; never step back.
            if current and current.get("id") == status.get("id") and \
                    _REFRESH_STAGE_ORDER.get(status.get("state"), 0) < _REFRESH_STAGE_ORDER.get(current.get("state"), 0):
                return
            self._status = status

    def set_heartbeat(self, heartbeat: Optional[str]) -> None:
        with self._lock:
            self._heartbeat = heartbeat

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._status and self._status.get("state") in ("requested", "running"))

    def view(self) -> Dict:
        """Times as epoch milliseconds so the browser needn't parse ISO strings."""
        with self._lock:
            s = self._status or {}
            return {
                "id": s.get("id"),
                "state": s.get("state"),
                "attempt": s.get("attempt", 1),
                "requested_ms": _epoch_ms(s.get("requested_at")),
                "started_ms": _epoch_ms(s.get("started_at")),
                "finished_ms": _epoch_ms(s.get("finished_at")),
                "new_count": s.get("new_count"),
                "changed": s.get("changed"),
                "error": s.get("error"),
                "heartbeat_ms": _epoch_ms(self._heartbeat),
            }
