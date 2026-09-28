"""Everything the VPS worker and the Render app exchange through Redis.

The worker (vps_worker.py) is the only side that talks to Culture Index. It
writes the survey snapshot and upload results; the Render app writes upload
requests and survey-refresh requests. Neither side connects to the other.

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
REFRESH_REQUEST_KEY = key("surveys:refresh_requested")
KNOWN_IDS_KEY = key("known_survey_ids")
NOTIFICATION_KEY = key("new_surveys_notification")
UPLOAD_QUEUE_KEY = key("uploads:queue")
WORKER_VERSION_KEY = key("worker:version")        # bumped by the worker after uploads finish

JOB_ACTIVE_TTL = 86400      # a queued job the worker never reaches expires after a day
JOB_FINISHED_TTL = 3600     # finished job records are kept an hour for display, then vanish
REFRESH_REQUEST_TTL = 3600
NOTIFICATION_TTL = 86400

JOB_QUEUED = "queued"
JOB_UPLOADING = "uploading"
JOB_DONE = "done"
JOB_FAILED = "failed"
ACTIVE_JOB_STATES = (JOB_QUEUED, JOB_UPLOADING)


def job_key(survey_id: str) -> str:
    return key(f"uploads:job:{survey_id}")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(url: Optional[str] = None) -> redis.Redis:
    """A Redis client for the shared state. Raises if REDIS_URL is missing or unreachable."""
    url = url or os.getenv('REDIS_URL')
    if not url:
        raise RuntimeError("REDIS_URL is not set; the worker and the app both need Redis")
    client = redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=int(os.getenv('REDIS_CONNECT_TIMEOUT', '5')),
        socket_timeout=int(os.getenv('REDIS_SOCKET_TIMEOUT', '5')),
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
    return job


def request_survey_refresh(client: redis.Redis) -> bool:
    """Ask the worker to export surveys at its next run. False if a request is already pending."""
    return bool(client.set(REFRESH_REQUEST_KEY, utc_now_iso(), nx=True, ex=REFRESH_REQUEST_TTL))
