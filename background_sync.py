import os
import json
import time
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, request, jsonify

from app_cache import get_cache_manager, log

_last_refresh_time = 0
_refresh_cooldown = 300

_KNOWN_IDS_KEY = "known_survey_ids"


def perform_survey_refresh(survey_service, jazzhr_cache, background_checker, recent_threshold):
    """Core survey refresh: reload from Culture Index, diff against current state,
    store a 'new surveys' notification in Redis, and kick off JazzHR checks.

    Shared by the hourly cron endpoint and the in-app periodic refresher so both
    paths behave identically. Returns a summary dict. Caller handles rate limiting.
    """
    cache_mgr = get_cache_manager()

    if survey_service.is_loading():
        log("Survey refresh skipped: already loading", "WARN")
        return {"status": "skipped", "reason": "load in progress"}

    old_surveys = survey_service.get_all_surveys()
    old_ids = set(str(s['surveyId']) for s in old_surveys) if old_surveys else set()

    survey_service.load_surveys(force_refresh=True)

    new_surveys = survey_service.get_all_surveys()
    new_ids = set(str(s['surveyId']) for s in new_surveys)

    # NEW-survey detection uses a PERSISTENT baseline in Redis, not the in-memory
    # survey list. A fresh process (the hourly cron, or a cold start) has an empty
    # in-memory list, so diffing against it would report every survey as "new".
    # The Redis baseline survives restarts so "new" means genuinely-unseen IDs.
    baseline = _get_known_ids(jazzhr_cache)
    if baseline is None:
        # Redis unavailable: fall back to the in-memory diff, but never treat a
        # cold start (empty old_ids) as "everything is new".
        truly_new = sorted(new_ids - old_ids) if old_ids else []
    elif not baseline:
        # No baseline recorded yet: establish it silently, don't notify.
        truly_new = []
    else:
        truly_new = sorted(new_ids - baseline)
    _set_known_ids(jazzhr_cache, new_ids)

    removed_ids = list(old_ids - new_ids)
    log(f"Survey refresh: in-mem {len(old_ids)} -> {len(new_ids)}; baseline-new={len(truly_new)}", "WARN")

    if new_surveys:
        recent_ids = [str(s['surveyId']) for s in new_surveys[:recent_threshold]]
        jazzhr_cache.set_recent_surveys(recent_ids)

    if truly_new:
        _store_notification_redis(jazzhr_cache, truly_new)

    # Cache invalidation / re-check is driven by whether THIS process's view of the
    # data changed (so the render cache and JazzHR re-check stay correct), separate
    # from the notification baseline above.
    changed = bool((new_ids - old_ids) or removed_ids)
    if changed:
        cache_mgr.on_data_refresh_start()
        cache_mgr.on_data_refresh_complete(
            new_survey_count=len(truly_new),
            new_survey_ids=truly_new
        )
        background_checker.start_checking()
    elif not cache_mgr.app_state.is_jazzhr_check_complete():
        # No survey change, but the initial JazzHR pass never finished — run it.
        background_checker.start_checking()

    return {
        "status": "success",
        "old_count": len(old_ids),
        "new_count": len(new_ids),
        "added": len(truly_new),
        "removed": len(removed_ids),
        "added_ids": truly_new[:5],
    }


def _get_known_ids(jazzhr_cache):
    """Return the persistent set of known survey IDs from Redis.

    Returns a set() when no baseline exists yet (first run), or None when Redis
    is unavailable (so the caller can fall back to the in-memory diff).
    """
    try:
        if hasattr(jazzhr_cache.cache, '_redis') and jazzhr_cache.cache._redis:
            data = jazzhr_cache.cache._redis.get(_KNOWN_IDS_KEY)
            if data is None:
                return set()
            return set(json.loads(data))
    except Exception as e:
        log(f"Failed to read known survey IDs: {e}", "WARN")
    return None


def _set_known_ids(jazzhr_cache, ids):
    try:
        if hasattr(jazzhr_cache.cache, '_redis') and jazzhr_cache.cache._redis:
            jazzhr_cache.cache._redis.set(_KNOWN_IDS_KEY, json.dumps(sorted(ids)))
    except Exception as e:
        log(f"Failed to store known survey IDs: {e}", "WARN")


def create_background_sync_blueprint(survey_service, jazzhr_cache, background_checker, recent_threshold):
    bp = Blueprint('background_sync', __name__)
    
    def require_bearer_token(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            auth_header = request.headers.get('Authorization')
            secret_token = os.getenv('BACKGROUND_JOB_SECRET')
            
            if not secret_token:
                log("BACKGROUND_JOB_SECRET not configured", "ERROR")
                return jsonify({"error": "Server misconfiguration"}), 500
            
            if len(secret_token) < 16:
                log("BACKGROUND_JOB_SECRET too short (min 16 chars)", "ERROR")
                return jsonify({"error": "Server misconfiguration"}), 500
            
            if not auth_header:
                log("Background refresh attempt without auth header", "WARN")
                return jsonify({"error": "Authorization required"}), 401
            
            if not auth_header.startswith("Bearer "):
                log("Invalid authorization format", "WARN")
                return jsonify({"error": "Invalid authorization format"}), 401
            
            provided_token = auth_header[7:]
            
            if provided_token != secret_token:
                log("Invalid bearer token provided", "WARN")
                return jsonify({"error": "Unauthorized"}), 401
            
            return f(*args, **kwargs)
        return decorated
    
    def check_rate_limit():
        global _last_refresh_time
        
        now = time.time()
        if now - _last_refresh_time < _refresh_cooldown:
            remaining = int(_refresh_cooldown - (now - _last_refresh_time))
            return False, remaining
        return True, 0
    
    @bp.route('/api/background-refresh', methods=['POST'])
    @require_bearer_token
    def background_refresh():
        global _last_refresh_time

        # Check rate limit
        allowed, retry_after = check_rate_limit()
        if not allowed:
            log(f"Background refresh rate limited, retry in {retry_after}s", "WARN")
            return jsonify({
                "status": "rate_limited",
                "retry_after_seconds": retry_after
            }), 429
        
        try:
            log("=== Background Refresh Started (cron) ===", "WARN")
            start_time = time.time()

            result = perform_survey_refresh(
                survey_service, jazzhr_cache, background_checker, recent_threshold
            )

            _last_refresh_time = time.time()
            elapsed = time.time() - start_time
            log(f"=== Background Refresh Complete in {elapsed:.1f}s ===", "WARN")

            result["duration_seconds"] = round(elapsed, 2)
            result["timestamp"] = datetime.now().isoformat()
            return jsonify(result), 200

        except Exception as e:
            log(f"Background refresh error: {e}", "ERROR")
            import traceback
            log(traceback.format_exc(), "ERROR")
            return jsonify({
                "status": "error",
                "error": "Internal server error"
            }), 500
    
    @bp.route('/api/get-notification', methods=['GET'])
    def get_notification():
        try:
            notification = _get_notification_redis(jazzhr_cache)
            if notification and notification.get("count", 0) > 0:
                return jsonify(notification), 200
            
            cache_mgr = get_cache_manager()
            notification = cache_mgr.get_notification()
            return jsonify(notification), 200
            
        except Exception as e:
            log(f"Error getting notification: {e}", "ERROR")
            return jsonify({"count": 0}), 200
    
    @bp.route('/api/acknowledge-notification', methods=['POST'])
    def acknowledge_notification():
        try:
            _clear_notification_redis(jazzhr_cache)
            
            cache_mgr = get_cache_manager()
            cache_mgr.acknowledge_notification()
            
            log("Notification acknowledged and cleared", "WARN")
            return jsonify({"status": "success"}), 200
            
        except Exception as e:
            log(f"Error acknowledging notification: {e}", "ERROR")
            return jsonify({"status": "error"}), 500
    
    return bp


def _store_notification_redis(jazzhr_cache, added_ids):
    try:
        if hasattr(jazzhr_cache.cache, '_redis') and jazzhr_cache.cache._redis:
            redis_client = jazzhr_cache.cache._redis
            notification_data = {
                "count": len(added_ids),
                "survey_ids": added_ids[:10],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "acknowledged": False
            }
            redis_client.setex(
                "new_surveys_notification",
                86400,  # 24 hour expiry
                json.dumps(notification_data)
            )
            log(f"Stored notification in Redis for {len(added_ids)} new surveys", "WARN")
    except Exception as e:
        log(f"Failed to store notification in Redis: {e}", "WARN")


def _get_notification_redis(jazzhr_cache):
    try:
        if hasattr(jazzhr_cache.cache, '_redis') and jazzhr_cache.cache._redis:
            redis_client = jazzhr_cache.cache._redis
            data = redis_client.get("new_surveys_notification")
            
            if data:
                notification = json.loads(data)
                if not notification.get("acknowledged", False):
                    return notification
    except Exception as e:
        log(f"Failed to get notification from Redis: {e}", "WARN")
    return None


def _clear_notification_redis(jazzhr_cache):
    try:
        if hasattr(jazzhr_cache.cache, '_redis') and jazzhr_cache.cache._redis:
            redis_client = jazzhr_cache.cache._redis
            redis_client.delete("new_surveys_notification")
    except Exception as e:
        log(f"Failed to clear notification from Redis: {e}", "WARN")

