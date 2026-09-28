"""Everything the VPS worker and the Render app exchange through Redis.

The worker (vps_worker.py) is the only side that talks to Culture Index. It
writes the survey snapshot, refresh progress and upload results, and announces
changes on a pub/sub channel; the Render app writes upload and survey-refresh
requests. Neither side connects to the other.

Set KEY_PREFIX (for example "test:") to keep a test run away from production keys.
"""
import base64
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import redis

KEY_PREFIX = os.getenv('KEY_PREFIX', '')


def key(name: str) -> str:
    return f"{KEY_PREFIX}{name}"


SNAPSHOT_KEY = key("surveys:snapshot")            # base64(gzip(json list of surveys))
SNAPSHOT_META_KEY = key("surveys:meta")           # json {hash, count, changed_at, checked_at}
REFRESH_QUEUE_KEY = key("surveys:refresh_queue")  # the worker blocks on this and the upload queue
REFRESH_STATUS_KEY = key("surveys:refresh_status")  # json: the latest requested refresh and its stage
KNOWN_IDS_KEY = key("known_survey_ids")
NOTIFICATION_KEY = key("new_surveys_notification")
UPLOAD_QUEUE_KEY = key("uploads:queue")
UPLOAD_PENDING_KEY = key("uploads:pending")        # set of survey IDs with an unfinished upload job
WORKER_VERSION_KEY = key("worker:version")        # bumped by the worker after uploads finish
HEARTBEAT_KEY = key("worker:heartbeat")           # the worker's last "still alive" time
EVENTS_CHANNEL = key("hr:events")                 # pub/sub: the worker tells the website what changed

JOB_ACTIVE_TTL = 86400      # a queued job the worker never reaches expires after a day
JOB_FINISHED_TTL = 3600     # finished job records are kept an hour for display, then vanish
REFRESH_STATUS_TTL = 86400
REFRESH_ACTIVE_TIMEOUT = 15 * 60   # a requested/running refresh older than this is treated as abandoned
HEARTBEAT_TTL = 3600
HEARTBEAT_INTERVAL = 300
NOTIFICATION_TTL = 86400

REFRESH_REQUESTED = "requested"
REFRESH_RUNNING = "running"
REFRESH_DONE = "done"
REFRESH_FAILED = "failed"

JOB_QUEUED = "queued"
JOB_UPLOADING = "uploading"
JOB_DONE = "done"
JOB_FAILED = "failed"
ACTIVE_JOB_STATES = (JOB_QUEUED, JOB_UPLOADING)


def job_key(survey_id: str) -> str:
    return key(f"uploads:job:{survey_id}")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(url: Optional[str] = None, socket_timeout: Optional[float] = None) -> redis.Redis:
    """A Redis client for the shared state. Raises if REDIS_URL is missing or unreachable.

    Pass a socket_timeout longer than any blocking call (BLPOP) or None for a
    pub/sub connection that waits indefinitely.
    """
    url = url or os.getenv('REDIS_URL')
    if not url:
        raise RuntimeError("REDIS_URL is not set; the worker and the app both need Redis")
    client = redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=int(os.getenv('REDIS_CONNECT_TIMEOUT', '5')),
        socket_timeout=socket_timeout if socket_timeout is not None else int(os.getenv('REDIS_SOCKET_TIMEOUT', '5')),
        health_check_interval=int(os.getenv('REDIS_HEALTH_CHECK_INTERVAL', '30')),
        retry_on_timeout=True,
    )
    client.ping()
    return client


def encode_snapshot(surveys: List[Dict]) -> Tuple[str, str]:
    """Return (payload, content_hash). The hash covers the survey data only, so an
    unchanged list always hashes the same and is not rewritten."""
    raw = json.dumps(surveys, sort_keys=True, separators=(',', ':')).encode('utf-8')
    content_hash = hashlib.sha256(raw).hexdigest()
    payload = base64.b64encode(gzip.compress(raw, compresslevel=6)).decode('ascii')
    return payload, content_hash


def decode_snapshot(payload: str) -> List[Dict]:
    return json.loads(gzip.decompress(base64.b64decode(payload)).decode('utf-8'))


def parse_json(value: Optional[str]) -> Optional[Dict]:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def enqueue_upload(client: redis.Redis, survey_id: str, first_name: str, last_name: str, pdf_url: str) -> Tuple[bool, str]:
    """Queue one upload for the worker. Returns (queued, message).

    One job per survey at a time: a survey that is already queued or uploading is
    refused. A finished (done or failed) job may be queued again.
    """
    job = {
        "survey_id": str(survey_id),
        "first_name": first_name,
        "last_name": last_name,
        "pdf_url": pdf_url,
        "state": JOB_QUEUED,
        "requested_at": utc_now_iso(),
    }
    encoded = json.dumps(job)
    jk = job_key(survey_id)

    if not client.set(jk, encoded, nx=True, ex=JOB_ACTIVE_TTL):
        existing = parse_json(client.get(jk)) or {}
        if existing.get("state") in ACTIVE_JOB_STATES:
            return False, "Upload already queued"
        client.set(jk, encoded, ex=JOB_ACTIVE_TTL)

    client.sadd(UPLOAD_PENDING_KEY, str(survey_id))
    client.lpush(UPLOAD_QUEUE_KEY, json.dumps({"survey_id": str(survey_id)}))
    return True, "Queued"


def get_jobs(client: redis.Redis, survey_ids: List[str]) -> Dict[str, Dict]:
    """Job records for the given surveys (one MGET). Surveys without a job are left out."""
    if not survey_ids:
        return {}
    values = client.mget([job_key(sid) for sid in survey_ids])
    jobs = {}
    for sid, value in zip(survey_ids, values):
        job = parse_json(value)
        if job:
            jobs[str(sid)] = job
    return jobs


def set_job_state(client: redis.Redis, survey_id: str, job: Dict, state: str, **fields) -> Dict:
    job = dict(job, state=state, updated_at=utc_now_iso(), **fields)
    ttl = JOB_ACTIVE_TTL if state in ACTIVE_JOB_STATES else JOB_FINISHED_TTL
    client.set(job_key(survey_id), json.dumps(job), ex=ttl)
    if state not in ACTIVE_JOB_STATES:
        client.srem(UPLOAD_PENDING_KEY, str(survey_id))
    publish_event(client, "upload", survey_id=str(survey_id), state=state)
    return job


def publish_event(client: redis.Redis, event_type: str, **fields) -> None:
    """Tell the website something changed. Best effort: the website also polls as a fallback."""
    try:
        client.publish(EVENTS_CHANNEL, json.dumps(dict(fields, type=event_type)))
    except redis.RedisError:
        pass


def refresh_is_active(status: Optional[Dict]) -> bool:
    if not status or status.get("state") not in (REFRESH_REQUESTED, REFRESH_RUNNING):
        return False
    try:
        requested = datetime.fromisoformat(status.get("requested_at", ""))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - requested).total_seconds() < REFRESH_ACTIVE_TIMEOUT


def set_refresh_status(client: redis.Redis, status: Dict) -> Dict:
    client.set(REFRESH_STATUS_KEY, json.dumps(status), ex=REFRESH_STATUS_TTL)
    publish_event(client, "refresh", status=status)
    return status


def request_survey_refresh(client: redis.Redis) -> Tuple[bool, Dict]:
    """Ask the worker to export surveys now. Returns (requested, status); a refresh
    already requested or running is not requested again."""
    current = parse_json(client.get(REFRESH_STATUS_KEY))
    if refresh_is_active(current):
        return False, current
    status = set_refresh_status(client, {
        "id": os.urandom(6).hex(),
        "state": REFRESH_REQUESTED,
        "requested_at": utc_now_iso(),
    })
    client.lpush(REFRESH_QUEUE_KEY, status["id"])
    return True, status
