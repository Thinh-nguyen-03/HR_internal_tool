"""In-process state that browser tabs poll: what changed, and the new-surveys badge.

Tabs poll this every few seconds. It lives in memory (the app runs one worker),
so polling costs no Redis commands; background threads update it.
"""
from datetime import datetime
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
