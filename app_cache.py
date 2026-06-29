import time
from datetime import datetime
from threading import RLock, Lock
from typing import Dict, List, Optional, Tuple, Any
from flask import session


def log(message: str, level: str = "INFO") -> None:
    if level not in ["ERROR", "WARN", "PERF"]:
        return
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{level}] {message}", flush=True)


class AppState:
    _RK_VERSION = 'app:data_version'
    _RK_JAZZHR_DONE = 'app:jazzhr_check_complete'
    _RK_UI_SIGNAL = 'app:ui_signal'
    _RK_LAST_REFRESH = 'app:last_data_refresh'

    def __init__(self):
        self._lock = RLock()
        self._redis = None

        self._data_version = 0
        self._jazzhr_initial_check_complete = False
        self._background_check_needs_signal = False
        self._last_data_refresh: Optional[datetime] = None
        self._new_surveys_count = 0
        self._new_surveys_ids: List[str] = []
        self._notification_acknowledged = True

    def set_redis_client(self, redis_client) -> None:
        with self._lock:
            self._redis = redis_client
            try:
                val = redis_client.get(self._RK_VERSION)
                if val is not None:
                    self._data_version = int(val)
            except Exception:
                pass

    def _redis_get(self, key: str) -> Optional[str]:
        try:
            val = self._redis.get(key)
            if val is None:
                return None
            return val.decode() if isinstance(val, bytes) else str(val)
        except Exception:
            return None

    def increment_version(self) -> int:
        with self._lock:
            if self._redis:
                try:
                    val = self._redis.incr(self._RK_VERSION)
                    self._data_version = int(val)
                    return self._data_version
                except Exception:
                    pass
            self._data_version += 1
            return self._data_version

    def get_version(self) -> int:
        with self._lock:
            if self._redis:
                val = self._redis_get(self._RK_VERSION)
                if val is not None:
                    self._data_version = int(val)
            return self._data_version

    def set_jazzhr_check_complete(self, complete: bool) -> None:
        with self._lock:
            self._jazzhr_initial_check_complete = complete
            if self._redis:
                try:
                    if complete:
                        self._redis.setex(self._RK_JAZZHR_DONE, 86400, '1')
                    else:
                        self._redis.delete(self._RK_JAZZHR_DONE)
                except Exception:
                    pass
            if complete:
                log("JazzHR initial checks marked complete", "WARN")

    def is_jazzhr_check_complete(self) -> bool:
        with self._lock:
            if self._redis:
                val = self._redis_get(self._RK_JAZZHR_DONE)
                if val is not None:
                    self._jazzhr_initial_check_complete = (val == '1')
                    return self._jazzhr_initial_check_complete
            return self._jazzhr_initial_check_complete

    def request_ui_signal(self) -> None:
        with self._lock:
            self._background_check_needs_signal = True
            if self._redis:
                try:
                    self._redis.setex(self._RK_UI_SIGNAL, 300, '1')
                except Exception:
                    pass

    def consume_ui_signal(self) -> bool:
        with self._lock:
            if self._redis:
                try:
                    pipe = self._redis.pipeline(transaction=True)
                    pipe.get(self._RK_UI_SIGNAL)
                    pipe.delete(self._RK_UI_SIGNAL)
                    results = pipe.execute()
                    if results[0] is not None:
                        self._background_check_needs_signal = False
                        return True
                    return False
                except Exception:
                    pass
            if self._background_check_needs_signal:
                self._background_check_needs_signal = False
                return True
            return False

    def record_data_refresh(self) -> None:
        with self._lock:
            self._last_data_refresh = datetime.now()
            if self._redis:
                try:
                    self._redis.setex(self._RK_LAST_REFRESH, 86400, self._last_data_refresh.isoformat())
                except Exception:
                    pass

    def get_last_refresh(self) -> Optional[datetime]:
        with self._lock:
            if self._redis:
                val = self._redis_get(self._RK_LAST_REFRESH)
                if val:
                    try:
                        return datetime.fromisoformat(val)
                    except Exception:
                        pass
            return self._last_data_refresh

    def set_new_surveys_notification(self, count: int, survey_ids: List[str]) -> None:
        with self._lock:
            self._new_surveys_count = count
            self._new_surveys_ids = survey_ids[:10]
            self._notification_acknowledged = False
            log(f"New surveys notification set: {count} surveys", "WARN")

    def get_notification(self) -> Dict:
        with self._lock:
            if self._notification_acknowledged or self._new_surveys_count == 0:
                return {"count": 0}
            return {
                "count": self._new_surveys_count,
                "survey_ids": self._new_surveys_ids,
                "timestamp": self._last_data_refresh.isoformat() if self._last_data_refresh else None,
                "acknowledged": False
            }

    def acknowledge_notification(self) -> None:
        with self._lock:
            self._notification_acknowledged = True
            self._new_surveys_count = 0
            self._new_surveys_ids = []
            log("Notification acknowledged", "WARN")

    def reset_for_refresh(self) -> None:
        with self._lock:
            self._jazzhr_initial_check_complete = False
            self._background_check_needs_signal = False
            if self._redis:
                try:
                    self._redis.delete(self._RK_JAZZHR_DONE)
                    self._redis.delete(self._RK_UI_SIGNAL)
                    val = self._redis.incr(self._RK_VERSION)
                    self._data_version = int(val)
                except Exception:
                    self._data_version += 1
            else:
                self._data_version += 1
            log(f"App state reset for refresh (version {self._data_version})", "WARN")


class SessionCache:
    def __init__(self, ttl_seconds: int = 30):
        self._lock = RLock()
        self._cache: Dict[str, Dict] = {}  # session_id -> cache data
        self._ttl_seconds = ttl_seconds
    
    def _get_session_id(self) -> str:
        try:
            if 'cache_session_id' not in session:
                import uuid
                session['cache_session_id'] = str(uuid.uuid4())
            return session['cache_session_id']
        except RuntimeError:
            return "no_session"
    
    def get(self, search_query: str, page: int, data_version: int) -> Optional[Tuple]:
        """Get cached render result if valid."""
        session_id = self._get_session_id()
        cache_key = f"{session_id}:{search_query}:{page}:{data_version}"
        
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry:
                # Check TTL
                age = (datetime.now() - entry['timestamp']).total_seconds()
                if age < self._ttl_seconds:
                    return entry['result']
                else:
                    # Expired
                    del self._cache[cache_key]
            return None
    
    def set(self, search_query: str, page: int, data_version: int, result: Tuple) -> None:
        """Store render result in cache."""
        session_id = self._get_session_id()
        cache_key = f"{session_id}:{search_query}:{page}:{data_version}"
        
        with self._lock:
            self._cache[cache_key] = {
                'result': result,
                'timestamp': datetime.now()
            }
            
            # Cleanup old entries (keep max 100 per cleanup)
            self._cleanup_if_needed()
    
    def invalidate_session(self) -> None:
        session_id = self._get_session_id()
        
        with self._lock:
            keys_to_delete = [k for k in self._cache.keys() if k.startswith(f"{session_id}:")]
            for key in keys_to_delete:
                del self._cache[key]
            
            if keys_to_delete:
                log(f"Invalidated {len(keys_to_delete)} cache entries for session", "WARN")
    
    def invalidate_all(self) -> None:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            if count > 0:
                log(f"Invalidated all {count} render cache entries", "WARN")
    
    def _cleanup_if_needed(self) -> None:
        if len(self._cache) < 200:
            return
        
        now = datetime.now()
        keys_to_delete = []
        
        for key, entry in self._cache.items():
            age = (now - entry['timestamp']).total_seconds()
            if age > self._ttl_seconds:
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            del self._cache[key]


class CallbackLock:
    def __init__(self):
        self._lock = Lock()
        self._holder = None
    
    def try_acquire(self, holder_id: str = None) -> bool:
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._holder = holder_id
        return acquired
    
    def release(self) -> None:
        self._holder = None
        try:
            self._lock.release()
        except RuntimeError:
            pass  # Already released
    
    def is_locked(self) -> bool:
        return self._lock.locked()


class CacheManager:
    def __init__(self):
        self.app_state = AppState()
        self.session_cache = SessionCache(ttl_seconds=30)
        self.callback_lock = CallbackLock()
        
        self._last_displayed_surveys: List[str] = []
        self._display_lock = RLock()
    
    def on_data_refresh_start(self) -> None:
        self.app_state.reset_for_refresh()
        self.session_cache.invalidate_all()
        log("Cache manager: refresh started, caches invalidated", "WARN")
    
    def on_data_refresh_complete(self, new_survey_count: int = 0, new_survey_ids: List[str] = None) -> None:
        self.app_state.record_data_refresh()
        self.app_state.increment_version()
        
        if new_survey_count > 0 and new_survey_ids:
            self.app_state.set_new_surveys_notification(new_survey_count, new_survey_ids)
        
        log(f"Cache manager: refresh complete, version={self.app_state.get_version()}", "WARN")
    
    def on_jazzhr_check_complete(self) -> None:
        self.app_state.set_jazzhr_check_complete(True)
        self.app_state.request_ui_signal()
        self.app_state.increment_version()
        log("Cache manager: JazzHR checks complete, UI signal requested", "WARN")
    
    def on_logout(self) -> None:
        self.session_cache.invalidate_session()
        log("Cache manager: session cache cleared on logout", "WARN")
    
    def get_cached_result(self, search_query: str, page: int, is_refresh_trigger: bool) -> Optional[Tuple]:
        if is_refresh_trigger:
            return None
        
        if not self.app_state.is_jazzhr_check_complete():
            return None
        
        return self.session_cache.get(
            search_query or "",
            page,
            self.app_state.get_version()
        )
    
    def cache_result(self, search_query: str, page: int, result: Tuple) -> None:
        if not self.app_state.is_jazzhr_check_complete():
            return
        
        self.session_cache.set(
            search_query or "",
            page,
            self.app_state.get_version(),
            result
        )
    
    def track_displayed_surveys(self, survey_ids: List[str]) -> None:
        with self._display_lock:
            if self._last_displayed_surveys and self._last_displayed_surveys != survey_ids:
                disappeared = set(self._last_displayed_surveys) - set(survey_ids)
                appeared = set(survey_ids) - set(self._last_displayed_surveys)
                if disappeared:
                    log(f"Surveys DISAPPEARED: {list(disappeared)[:5]}", "WARN")
                if appeared:
                    log(f"Surveys APPEARED: {list(appeared)[:5]}", "WARN")
            self._last_displayed_surveys = survey_ids.copy()
    
    def set_redis_client(self, redis_client) -> None:
        self.app_state.set_redis_client(redis_client)

    def check_ui_signal(self) -> bool:
        return self.app_state.consume_ui_signal()

    def get_notification(self) -> Dict:
        return self.app_state.get_notification()

    def acknowledge_notification(self) -> None:
        self.app_state.acknowledge_notification()


cache_manager = CacheManager()

def get_cache_manager() -> CacheManager:
    return cache_manager

