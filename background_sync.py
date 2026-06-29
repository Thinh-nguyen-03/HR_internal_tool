import os
import json
import time
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, jsonify

from app_cache import get_cache_manager, log

_last_refresh_time = 0
_refresh_cooldown = 300


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

    # Reload first, then decide whether anything actually changed. We only do the
    # heavy reset (invalidate render cache, bump version, re-run JazzHR checks)
    # when the survey set changed, so a frequent idle refresh is cheap and doesn't
    # flicker the UI back into a "checking" state.
    survey_service.load_surveys(force_refresh=True)

    new_surveys = survey_service.get_all_surveys()
    new_ids = set(str(s['surveyId']) for s in new_surveys)
    added_ids = list(new_ids - old_ids)
    removed_ids = list(old_ids - new_ids)

    log(f"Survey refresh: {len(old_ids)} -> {len(new_ids)} (added {len(added_ids)}, removed {len(removed_ids)})", "WARN")

    if new_surveys:
        recent_ids = [str(s['surveyId']) for s in new_surveys[:recent_threshold]]
        jazzhr_cache.set_recent_surveys(recent_ids)

    changed = bool(added_ids or removed_ids)
    if changed:
        cache_mgr.on_data_refresh_start()
        cache_mgr.on_data_refresh_complete(
            new_survey_count=len(added_ids),
            new_survey_ids=added_ids
        )
        if len(added_ids) > 0:
            _store_notification_redis(jazzhr_cache, added_ids)
        background_checker.start_checking()
    elif not cache_mgr.app_state.is_jazzhr_check_complete():
        # No survey change, but the initial JazzHR pass never finished — run it.
        background_checker.start_checking()

    return {
        "status": "success",
        "old_count": len(old_ids),
        "new_count": len(new_ids),
        "added": len(added_ids),
        "removed": len(removed_ids),
        "added_ids": added_ids[:5],
    }


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
                "timestamp": datetime.now().isoformat(),
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

