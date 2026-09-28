"""JazzHR status store in Redis.

Statuses are kept indefinitely and carry the time they were checked. A stale
status is still shown (almost always still right) while a fresh check runs in
the background, so cards don't fall back to "Checking" when a status ages.
"""
import json
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import redis

from shared_state import key, utc_now_iso

CACHE_ENTRY_VERSION = 1
STATUS_KEY_PREFIX = key("jazzhr_status:")


def validate_cache_entry(entry) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get('_cache_version') == CACHE_ENTRY_VERSION
        and 'timestamp' in entry
        and {'status', 'isUploaded'}.issubset(entry)
    )


def parse_timestamp(value) -> Optional[datetime]:
    """Parse a stored timestamp as an aware UTC datetime. Entries without a
    timezone were written by the Render app, which runs on UTC."""
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status_key(survey_id: str) -> str:
    return f"{STATUS_KEY_PREFIX}{survey_id}"


class StatusStore:
    def __init__(self, client: redis.Redis, recent_threshold: int = 1000, stale_hours: float = 2):
        """Statuses of the newest `recent_threshold` surveys go stale after
        `stale_hours`; older surveys' statuses never go stale on their own."""
        self.client = client
        self.recent_threshold = recent_threshold
        self.stale_hours = stale_hours
        self._recent_survey_ids = set()

    def set_recent_surveys(self, survey_ids: Iterable[str]) -> None:
        self._recent_survey_ids = set(str(sid) for sid in list(survey_ids)[:self.recent_threshold])

    def is_recent(self, survey_id: str) -> bool:
        return str(survey_id) in self._recent_survey_ids

    def get(self, survey_id: str) -> Optional[Dict]:
        return self.get_batch([survey_id]).get(str(survey_id))

    def get_batch(self, survey_ids: List[str]) -> Dict[str, Optional[Dict]]:
        """Statuses for many surveys in one MGET. Missing or malformed entries come back as None."""
        ids = [str(sid) for sid in survey_ids]
        if not ids:
            return {}
        result = {}
        for sid, raw in zip(ids, self.client.mget([_status_key(sid) for sid in ids])):
            try:
                entry = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                entry = None
            result[sid] = entry if validate_cache_entry(entry) else None
        return result

    def set(self, survey_id: str, value: Dict) -> None:
        entry = dict(value)
        entry.setdefault('timestamp', utc_now_iso())
        entry['_cache_version'] = CACHE_ENTRY_VERSION
        self.client.set(_status_key(str(survey_id)), json.dumps(entry))

    def is_stale(self, survey_id: str, entry: Optional[Dict]) -> bool:
        if not entry:
            return True
        if not self.is_recent(survey_id):
            return False
        checked = parse_timestamp(entry.get('timestamp'))
        if checked is None:
            return True
        return (datetime.now(timezone.utc) - checked).total_seconds() > self.stale_hours * 3600

    def clear_all(self) -> int:
        """Delete every status entry (used by rebuild_cache.py option 1)."""
        deleted = 0
        for k in self.client.scan_iter(match=f"{STATUS_KEY_PREFIX}*", count=500):
            self.client.delete(k)
            deleted += 1
        return deleted

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False
