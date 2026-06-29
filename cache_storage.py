import os
import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, List, Optional, Any
from urllib.parse import urlparse, urlunparse

from app_cache import log

CACHE_ENTRY_VERSION = 1

def validate_cache_entry(entry: Dict) -> bool:
    """Validate cache entry structure and version."""
    if not isinstance(entry, dict):
        return False
    
    if '_cache_version' not in entry:
        return False
    
    if entry.get('_cache_version') != CACHE_ENTRY_VERSION:
        return False
    
    if 'timestamp' not in entry:
        return False
    
    required_fields = {'status', 'isUploaded'}
    if not required_fields.issubset(entry.keys()):
        return False
    
    return True


class CacheBackend(ABC):
    """Abstract base for cache implementations."""
    @abstractmethod
    def get(self, key: str) -> Optional[Dict]:
        pass
    
    @abstractmethod
    def get_permanent(self, key: str) -> Optional[Dict]:
        pass
    
    @abstractmethod
    def set(self, key: str, value: Dict, permanent: bool = False):
        pass
    
    @abstractmethod
    def delete(self, key: str):
        pass
    
    @abstractmethod
    def save(self):
        pass
    
    @abstractmethod
    def get_count(self) -> int:
        pass
    
    @abstractmethod
    def get_version(self) -> int:
        pass
    
    @abstractmethod
    def get_all_keys(self) -> List[str]:
        pass

class FileCache(CacheBackend):
    """JSON file-based cache with TTL support (for development/single-worker)."""
    
    def __init__(self, cache_file: str, ttl_hours: int = 24):
        self.cache_file = cache_file
        self.ttl_hours = ttl_hours
        self._cache = {}
        self._lock = RLock()
        self._version = 0
        self._hits = 0
        self._misses = 0
        self._load()
    
    def _load(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    for key, value in data.items():
                        if 'timestamp' in value and isinstance(value['timestamp'], str):
                            value['timestamp'] = datetime.fromisoformat(value['timestamp'])
                        self._cache[key] = value
        except Exception:
            pass  # Silent fail on load
    
    def save(self):
        try:
            with self._lock:
                data = {}
                for key, value in self._cache.items():
                    serialized = value.copy()
                    if 'timestamp' in serialized and isinstance(serialized['timestamp'], datetime):
                        serialized['timestamp'] = serialized['timestamp'].isoformat()
                    data[key] = serialized
            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass  # Silent fail on save
    
    def get(self, key: str) -> Optional[Dict]:
        with self._lock:
            item = self._cache.get(key)
            if item:
                # Validate cache entry
                if not validate_cache_entry(item):
                    self._misses += 1
                    del self._cache[key]
                    return None
                
                if item.get('_permanent'):
                    self._hits += 1
                    return item
                timestamp = item.get('timestamp', datetime.min)
                age = datetime.now() - timestamp
                if age < timedelta(hours=self.ttl_hours):
                    self._hits += 1
                    return item
            self._misses += 1
            return None
    
    def get_batch(self, keys: List[str]) -> Dict[str, Optional[Dict]]:
        """Batch get with TTL validation."""
        result = {}
        now = datetime.now()
        ttl_delta = timedelta(hours=self.ttl_hours)
        with self._lock:
            for key in keys:
                item = self._cache.get(key)
                if item:
                    if item.get('_permanent'):
                        result[key] = item
                    else:
                        timestamp = item.get('timestamp', datetime.min)
                        if now - timestamp < ttl_delta:
                            result[key] = item
                        else:
                            result[key] = None
                else:
                    result[key] = None
        return result
    
    def get_permanent(self, key: str) -> Optional[Dict]:
        with self._lock:
            return self._cache.get(key)
    
    def set(self, key: str, value: Dict, permanent: bool = False):
        with self._lock:
            value = value.copy()
            value['timestamp'] = datetime.now()
            value['_cache_version'] = CACHE_ENTRY_VERSION
            if permanent:
                value['_permanent'] = True
            self._cache[key] = value
            self._version += 1
        return True
    
    def delete(self, key: str):
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._version += 1
    
    def get_count(self) -> int:
        return len(self._cache)
    
    def get_version(self) -> int:
        return self._version
    
    def get_all_keys(self) -> List[str]:
        with self._lock:
            return list(self._cache.keys())
    
    def ping(self) -> bool:
        return True
    
    def get_stats(self) -> Dict:
        """Get cache statistics including hit/miss rates."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "total_requests": total,
            "hit_rate_percent": round(hit_rate, 2)
        }
    
    def is_healthy(self) -> Dict:
        stats = self.get_stats()
        return {
            "connected": True,
            "type": "file",
            "cache_file": self.cache_file,
            "item_count": len(self._cache),
            "stats": stats
        }

def mask_redis_url(url: str) -> str:
    """Mask username/password in Redis URL for safe logging (redis://user:pass@host -> redis://***:***@host)."""
    try:
        parsed = urlparse(url)
        masked_netloc = parsed.hostname or 'unknown'
        if parsed.port:
            masked_netloc = f"{masked_netloc}:{parsed.port}"
        if parsed.username or parsed.password:
            masked_netloc = f"***:***@{masked_netloc}"
        
        masked = urlunparse((
            parsed.scheme,
            masked_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))
        return masked
    except Exception:
        return "redis://***"


class RedisCache(CacheBackend):
    """
    Distributed cache for production (multi-worker safe).
    Features: connection pooling, auto-reconnect, graceful degradation, timeout protection.
    """
    
    def __init__(self, prefix: str, ttl_hours: int = 24, redis_url: str = None):
        self.prefix = prefix
        self.ttl_hours = ttl_hours
        self.ttl_seconds = ttl_hours * 3600
        self._version = 0
        self._lock = RLock()
        self._redis = None
        self._is_connected = False
        self._last_error = None
        self._connection_attempts = 0
        self._hits = 0
        self._misses = 0
        
        self.MAX_RETRIES = int(os.getenv('REDIS_MAX_RETRIES', '2'))
        self.CONNECT_TIMEOUT = int(os.getenv('REDIS_CONNECT_TIMEOUT', '5'))
        self.SOCKET_TIMEOUT = int(os.getenv('REDIS_SOCKET_TIMEOUT', '5'))
        self.MAX_CONNECTIONS = int(os.getenv('REDIS_MAX_CONNECTIONS', '50'))
        self.HEALTH_CHECK_INTERVAL = int(os.getenv('REDIS_HEALTH_CHECK_INTERVAL', '30'))
        
        try:
            import redis as redis_module
            self._redis_module = redis_module
        except ImportError:
            raise ImportError("Redis package not installed. Run: pip install redis")
        
        self.redis_url = redis_url or os.getenv('REDIS_URL', 'redis://localhost:6379')
        self._connect()
    
    def _connect(self) -> bool:
        """Establish Redis connection with retry limit."""
        if self._connection_attempts >= self.MAX_RETRIES:
            return False
        
        try:
            self._connection_attempts += 1
            
            # Create connection with robust settings
            self._redis = self._redis_module.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=self.CONNECT_TIMEOUT,
                socket_timeout=self.SOCKET_TIMEOUT,
                socket_keepalive=True,
                health_check_interval=self.HEALTH_CHECK_INTERVAL,
                max_connections=self.MAX_CONNECTIONS,
                retry_on_timeout=True
            )
            
            self._redis.ping()
            self._is_connected = True
            self._last_error = None
            self._connection_attempts = 0
            return True
            
        except Exception as e:
            self._is_connected = False
            self._last_error = str(e)
            return False
    
    def _ensure_connected(self) -> bool:
        if self._is_connected and self._redis:
            return True
        self._connection_attempts = 0
        return self._connect()
    
    def _safe_operation(self, operation_name: str, operation, default=None):
        """Execute Redis operation with graceful error handling (returns default on failure).

        Failures degrade gracefully to `default`, but they are no longer silent:
        connection loss is logged once on the connected->disconnected transition,
        and unexpected exceptions (which may be real bugs, not infra) are always
        logged so they don't masquerade as cache misses.
        """
        if not self._ensure_connected():
            return default

        try:
            return operation()
        except self._redis_module.ConnectionError as e:
            if self._is_connected:
                log(f"Redis connection lost during '{operation_name}': {e}", "ERROR")
            self._is_connected = False
            self._last_error = str(e)
            return default
        except self._redis_module.TimeoutError as e:
            self._last_error = str(e)
            log(f"Redis timeout during '{operation_name}'", "WARN")
            return default
        except Exception as e:
            self._last_error = str(e)
            log(f"Unexpected Redis error during '{operation_name}': {e}", "ERROR")
            return default
    
    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"
    
    def ping(self) -> bool:
        def do_ping():
            return self._redis.ping()
        return self._safe_operation("ping", do_ping, default=False) or False
    
    def get(self, key: str) -> Optional[Dict]:
        def do_get():
            data = self._redis.get(self._key(key))
            if data:
                try:
                    item = json.loads(data)
                    # Validate cache entry
                    if not validate_cache_entry(item):
                        self._redis.delete(self._key(key))
                        with self._lock:
                            self._misses += 1
                        return None
                    with self._lock:
                        self._hits += 1
                    return item
                except (json.JSONDecodeError, TypeError):
                    self._redis.delete(self._key(key))
                    with self._lock:
                        self._misses += 1
                    return None
            with self._lock:
                self._misses += 1
            return None
        return self._safe_operation("get", do_get, default=None)
    
    def get_batch(self, keys: List[str]) -> Dict[str, Optional[Dict]]:
        if not keys:
            return {}
        
        def do_mget():
            prefixed_keys = [self._key(k) for k in keys]
            values = self._redis.mget(prefixed_keys)
            result = {}
            for key, data in zip(keys, values):
                if data:
                    try:
                        result[key] = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = None
                else:
                    result[key] = None
            return result
        
        return self._safe_operation("mget", do_mget, default={})
    
    def get_permanent(self, key: str) -> Optional[Dict]:
        return self.get(key)
    
    def set(self, key: str, value: Dict, permanent: bool = False):
        def do_set():
            value_copy = value.copy()
            value_copy['timestamp'] = datetime.now().isoformat()
            value_copy['_cache_version'] = CACHE_ENTRY_VERSION
            data = json.dumps(value_copy)
            
            if permanent:
                result = self._redis.set(self._key(key), data)
            else:
                result = self._redis.setex(self._key(key), self.ttl_seconds, data)
            
            with self._lock:
                self._version += 1
            return result
        
        success = self._safe_operation("set", do_set, default=False)
        return success or False
    
    def delete(self, key: str):
        def do_delete():
            self._redis.delete(self._key(key))
            with self._lock:
                self._version += 1
            return True
        
        self._safe_operation("delete", do_delete, default=False)
    
    def save(self):
        pass
    
    def get_count(self) -> int:
        """Get cached item count using SCAN (non-blocking)."""
        def do_count():
            count = 0
            cursor = 0
            pattern = f"{self.prefix}:*"
            
            while True:
                cursor, keys = self._redis.scan(cursor, match=pattern, count=100)
                count += len([k for k in keys if ':perm:' not in k])
                if cursor == 0:
                    break
            
            return count
        return self._safe_operation("get_count", do_count, default=0) or 0
    
    def get_version(self) -> int:
        return self._version
    
    def get_all_keys(self) -> List[str]:
        """Get all cache keys using SCAN (non-blocking)."""
        def do_keys():
            all_keys = []
            cursor = 0
            pattern = f"{self.prefix}:*"
            prefix_len = len(self.prefix) + 1
            
            while True:
                cursor, keys = self._redis.scan(cursor, match=pattern, count=100)
                all_keys.extend([k[prefix_len:] for k in keys if ':perm:' not in k])
                if cursor == 0:
                    break
            
            return all_keys
        return self._safe_operation("get_all_keys", do_keys, default=[]) or []
    
    def get_stats(self) -> Dict:
        """Get cache statistics including hit/miss rates."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "total_requests": total,
                "hit_rate_percent": round(hit_rate, 2)
            }
    
    def is_healthy(self) -> Dict:
        stats = self.get_stats()
        return {
            "connected": self._is_connected,
            "last_error": self._last_error,
            "prefix": self.prefix,
            "ttl_hours": self.ttl_hours,
            "stats": stats
        }


def create_cache(name: str, ttl_hours: int = 24, cache_file: str = None) -> CacheBackend:
    """Create cache backend based on CACHE_BACKEND env var (redis or file)."""
    backend = os.getenv('CACHE_BACKEND', 'file').lower()
    
    if backend == 'redis':
        return RedisCache(prefix=name, ttl_hours=ttl_hours)
    else:
        file_path = cache_file or f"{name}_cache.json"
        return FileCache(cache_file=file_path, ttl_hours=ttl_hours)


class SmartJazzHRCache:
    """
    Intelligent cache with dynamic TTL:
    - Recent surveys (configurable threshold): Respect TTL for frequent updates
    - Old surveys: Permanent cache (historical data unlikely to change)
    """
    
    def __init__(self, cache: CacheBackend, recent_threshold: int = 2000):
        self.cache = cache
        self.recent_threshold = recent_threshold
        self._recent_survey_ids = set()
    
    def set_recent_surveys(self, survey_ids: List[str]):
        self._recent_survey_ids = set(str(sid) for sid in survey_ids[:self.recent_threshold])
    
    def is_recent(self, survey_id: str) -> bool:
        return str(survey_id) in self._recent_survey_ids
    
    def get(self, survey_id: str) -> Optional[Dict]:
        survey_id = str(survey_id)
        if self.is_recent(survey_id):
            return self.cache.get(survey_id)
        else:
            return self.cache.get_permanent(survey_id)
    
    def get_batch(self, survey_ids: List[str]) -> Dict[str, Optional[Dict]]:
        if not survey_ids:
            return {}
        str_ids = [str(sid) for sid in survey_ids]
        return self.cache.get_batch(str_ids)
    
    def set(self, survey_id: str, value: Dict):
        survey_id = str(survey_id)
        is_permanent = not self.is_recent(survey_id)
        success = self.cache.set(survey_id, value, permanent=is_permanent)
        return success
    
    def delete(self, survey_id: str):
        self.cache.delete(str(survey_id))
    
    def save(self):
        self.cache.save()
    
    def get_count(self) -> int:
        return self.cache.get_count()
    
    def get_version(self) -> int:
        return self.cache.get_version()
    
    def ping(self) -> bool:
        if hasattr(self.cache, 'ping'):
            return self.cache.ping()
        return True  # FileCache is always "healthy"
    
    def is_healthy(self) -> Dict:
        if hasattr(self.cache, 'is_healthy'):
            return self.cache.is_healthy()
        return {"connected": True, "type": "file"}

