"""VPS worker: does all Culture Index work and hands the results to the Render app.

Render's IP is blocked by Culture Index, so this runs from the VPS on cron. It
only makes outbound connections (Culture Index, JazzHR, Upstash Redis) and
accepts none. Downloaded CSVs and PDFs live in memory only and are dropped as
soon as they have been used; nothing is written to disk except this worker's
capped log.

    python vps_worker.py export     Export surveys and publish the snapshot (only writes if it changed)
    python vps_worker.py uploads    Process queued uploads, and run an export if one was requested

Configuration comes from the environment or a .env next to this file (override
with WORKER_ENV_FILE). See README.md for the cron lines.
"""
import fcntl
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.getenv('WORKER_ENV_FILE') or os.path.join(HERE, '.env'))

# Imported after the env file is loaded: shared_state reads KEY_PREFIX at import time.
import shared_state as ss  # noqa: E402
from cache_storage import StatusStore, parse_timestamp  # noqa: E402
from check_jazzhr_uploads import JazzHRUploadChecker, RateLimiter  # noqa: E402
from cultureindex_client import CultureIndexClient, CultureIndexAuthError, fetch_pdf_sizes, parse_surveys_csv  # noqa: E402

CLIENT_ID = os.getenv('CLIENT_ID', 'A89F5B0000')
SIZE_LOOKUP_NEWEST = int(os.getenv('SIZE_LOOKUP_NEWEST', '300'))
SIZE_LOOKUPS_PER_RUN = int(os.getenv('SIZE_LOOKUPS_PER_RUN', '50'))
SIZE_REFRESH_DAYS = int(os.getenv('SIZE_REFRESH_DAYS', '7'))
PDF_FETCH_TIMEOUT = int(os.getenv('PDF_FETCH_TIMEOUT', '5'))
WORKER_JAZZHR_CALLS_PER_MINUTE = int(os.getenv('WORKER_JAZZHR_CALLS_PER_MINUTE', '15'))
UPLOAD_ATTEMPTS = 2

NON_RETRYABLE_ERRORS = (
    "401", "403", "404", "apikey not set", "invalid api key",
    "applicant_id was not set", "file already exists", "invalid data", "not a pdf",
)

log = logging.getLogger("worker")


def setup_logging() -> None:
    """Survey ID, name, status and errors only; capped at about 2 MB on disk."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(os.path.join(HERE, 'worker.log'), maxBytes=1_000_000, backupCount=1)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(stream_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


class RunLock:
    """Non-blocking exclusive lock so overlapping cron runs of the same job skip instead of piling up."""

    def __init__(self, name: str):
        self.path = os.path.join(HERE, f".{name}.lock")
        self._fh = None

    def __enter__(self) -> bool:
        self._fh = open(self.path, 'w')
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __exit__(self, *exc) -> None:
        self._fh.close()


def ci_login() -> CultureIndexClient:
    email = (os.getenv('CULTUREINDEX_EMAIL') or '').strip()
    password = (os.getenv('CULTUREINDEX_PASSWORD') or '').strip()
    if not email or not password:
        raise RuntimeError("CULTUREINDEX_EMAIL and CULTUREINDEX_PASSWORD must be set")
    client = CultureIndexClient()
    client.login(email=email, password=password)
    return client


def _survey_sort_key(survey: Dict):
    """Newest survey date first, then highest survey ID."""
    try:
        date = datetime.strptime(survey.get('surveyDate') or '', '%m/%d/%Y').date().toordinal()
    except ValueError:
        date = 0
    survey_id = survey.get('surveyId') or ''
    return (date, int(survey_id) if survey_id.isdigit() else 0, survey_id)


def _carry_forward_sizes(surveys: List[Dict], previous: List[Dict]) -> None:
    """Report sizes ride along in the snapshot, so they are only looked up for
    new surveys and refreshed weekly for the newest ones."""
    now = datetime.now(timezone.utc)
    previous_sizes = {s.get('surveyId'): (s.get('pdfSize'), s.get('pdfSizeCheckedAt')) for s in previous}

    to_look_up = []
    for index, survey in enumerate(surveys):
        size, checked_at = previous_sizes.get(survey['surveyId'], (None, None))
        survey['pdfSize'] = size
        survey['pdfSizeCheckedAt'] = checked_at
        if index >= SIZE_LOOKUP_NEWEST or not survey.get('surveyReportUrl'):
            continue
        checked = parse_timestamp(checked_at) if checked_at else None
        if checked is None or now - checked > timedelta(days=SIZE_REFRESH_DAYS):
            to_look_up.append(survey)

    to_look_up = to_look_up[:SIZE_LOOKUPS_PER_RUN]
    if not to_look_up:
        return
    sizes = fetch_pdf_sizes({s['surveyId']: s['surveyReportUrl'] for s in to_look_up}, timeout=PDF_FETCH_TIMEOUT)
    checked_at = now.isoformat()
    for survey in to_look_up:
        survey['pdfSizeCheckedAt'] = checked_at
        if survey['surveyId'] in sizes:
            survey['pdfSize'] = sizes[survey['surveyId']]
    log.info(f"Report sizes: looked up {len(to_look_up)}, found {len(sizes)}")


def run_export(client, reason: str) -> None:
    with RunLock("export") as acquired:
        if not acquired:
            log.info("Export already running; skipped")
            return

        ci = ci_login()
        csv_text = ci.export_surveys_csv(client_id=CLIENT_ID)
        surveys = parse_surveys_csv(csv_text)
        del csv_text
        if not surveys:
            raise RuntimeError("Culture Index export returned no surveys")
        # Culture Index returns same-day surveys in varying order; a fixed order keeps
        # the page stable and the content hash unchanged when nothing changed.
        surveys.sort(key=_survey_sort_key, reverse=True)

        # One MGET for everything the export needs to compare against.
        meta_raw, prev_payload, known_raw, notification_raw, refresh_flag = client.mget(
            ss.SNAPSHOT_META_KEY, ss.SNAPSHOT_KEY, ss.KNOWN_IDS_KEY, ss.NOTIFICATION_KEY, ss.REFRESH_REQUEST_KEY
        )
        previous = ss.decode_snapshot(prev_payload) if prev_payload else []
        del prev_payload
        _carry_forward_sizes(surveys, previous)
        del previous

        payload, content_hash = ss.encode_snapshot(surveys)
        meta = ss.parse_json(meta_raw) or {}
        now = ss.utc_now_iso()

        if meta.get('hash') == content_hash:
            meta['checked_at'] = now
            client.set(ss.SNAPSHOT_META_KEY, json.dumps(meta))
            log.info(f"Export ({reason}): {len(surveys)} surveys, unchanged")
        else:
            new_ids = [s['surveyId'] for s in surveys]
            # No baseline yet (first run): record one silently instead of flagging every survey as new.
            baseline = set(json.loads(known_raw)) if known_raw else None
            truly_new = [sid for sid in new_ids if sid not in baseline] if baseline else []

            pipe = client.pipeline(transaction=True)
            pipe.set(ss.SNAPSHOT_KEY, payload)
            pipe.set(ss.SNAPSHOT_META_KEY, json.dumps({
                "hash": content_hash, "count": len(surveys), "changed_at": now, "checked_at": now,
            }))
            pipe.set(ss.KNOWN_IDS_KEY, json.dumps(sorted(new_ids)))
            if truly_new:
                # Add to any notification nobody has clicked yet, rather than replacing it.
                existing = ss.parse_json(notification_raw) or {}
                pending = [] if existing.get('acknowledged') else existing.get('survey_ids_all', existing.get('survey_ids', []))
                all_new = list(dict.fromkeys(truly_new + list(pending)))
                pipe.set(ss.NOTIFICATION_KEY, json.dumps({
                    "count": len(all_new), "survey_ids": all_new[:10], "survey_ids_all": all_new,
                    "timestamp": now, "acknowledged": False,
                }), ex=ss.NOTIFICATION_TTL)
            pipe.execute()
            log.info(f"Export ({reason}): {len(surveys)} surveys, snapshot updated, {len(truly_new)} new")
        del payload

        if refresh_flag:
            client.delete(ss.REFRESH_REQUEST_KEY)


def _is_retryable(error: str) -> bool:
    error = (error or '').lower()
    return not any(marker in error for marker in NON_RETRYABLE_ERRORS)


class Uploader:
    """Processes upload jobs. The Culture Index login happens once, and only if there is work."""

    def __init__(self, client):
        self.client = client
        self.status_store = StatusStore(client)
        limiter = RateLimiter(calls_per_minute=WORKER_JAZZHR_CALLS_PER_MINUTE)
        self.checker = JazzHRUploadChecker(os.getenv('JAZZHR_API_KEY'), before_request=limiter.wait)
        self._ci: Optional[CultureIndexClient] = None

    def _fetch_pdf(self, pdf_url: str) -> Optional[bytes]:
        """The portal report endpoint first (one re-login on an expired token). None
        means fall back to downloading the CSV report URL, which is PDF-checked too."""
        for attempt in range(2):
            try:
                if self._ci is None:
                    self._ci = ci_login()
                return self._ci.download_report_pdf(pdf_url)
            except CultureIndexAuthError as e:
                if "Token expired" in str(e) and attempt == 0:
                    self._ci = None
                    continue
                log.warning(f"Report endpoint failed, using report URL instead: {e}")
                return None
            except Exception as e:
                log.warning(f"Report endpoint failed, using report URL instead: {e}")
                return None
        return None

    def process(self, survey_id: str) -> None:
        job = ss.parse_json(self.client.get(ss.job_key(survey_id)))
        if not job or job.get('state') != ss.JOB_QUEUED:
            return
        first, last, pdf_url = job.get('first_name', ''), job.get('last_name', ''), job.get('pdf_url')
        job = ss.set_job_state(self.client, survey_id, job, ss.JOB_UPLOADING)
        name = f"{first} {last}".strip()

        try:
            # Re-check JazzHR right before uploading: the Render page may be stale,
            # and this also picks the current target applicant record.
            status = self.checker.check_survey_status(first, last, pdf_url, None)
        except Exception as e:
            ss.set_job_state(self.client, survey_id, job, ss.JOB_FAILED, error=f"JazzHR check failed: {e}"[:200])
            log.error(f"Upload {survey_id} ({name}): JazzHR check failed: {e}")
            return

        status['timestamp'] = ss.utc_now_iso()
        if status['status'] == 'UPLOADED':
            self.status_store.set(survey_id, status)
            ss.set_job_state(self.client, survey_id, job, ss.JOB_DONE, message="Already in JazzHR")
            log.info(f"Upload {survey_id} ({name}): already in JazzHR, nothing uploaded")
            return
        if status['status'] != 'NOT_UPLOADED':
            ss.set_job_state(self.client, survey_id, job, ss.JOB_FAILED, error=f"Cannot upload: {status['status']}")
            log.info(f"Upload {survey_id} ({name}): cannot upload, status {status['status']}")
            return

        applicant_id = status['applicantId']
        result = {}
        for attempt in range(UPLOAD_ATTEMPTS):
            pdf_bytes = self._fetch_pdf(pdf_url)
            result = self.checker.upload_file_to_applicant(
                applicant_id=applicant_id, pdf_url=pdf_url,
                first_name=first, last_name=last, pdf_bytes=pdf_bytes,
            )
            # The PDF is never kept, not even for a retry; a retry downloads it again.
            pdf_bytes = None
            if result.get('success') or not _is_retryable(result.get('error', '')):
                break

        if result.get('success'):
            self.status_store.set(survey_id, {
                "status": "UPLOADED",
                "applicantId": applicant_id,
                "isUploaded": True,
                "match": {
                    "matched_by": "uploaded by this tool",
                    "match_type": "tool_upload",
                    "file": {"filename": result.get('filename'), "id": result.get('file_id')},
                },
                "file_count": status.get('file_count', 0) + 1,
                "applicant_count": status.get('applicant_count', 1),
                "had_pdf_size": False,
                "timestamp": ss.utc_now_iso(),
            })
            ss.set_job_state(self.client, survey_id, job, ss.JOB_DONE, message=f"Uploaded {result.get('filename')}")
            log.info(f"Upload {survey_id} ({name}): uploaded to applicant {applicant_id}")
        else:
            ss.set_job_state(self.client, survey_id, job, ss.JOB_FAILED, error=str(result.get('error'))[:200])
            log.error(f"Upload {survey_id} ({name}): failed: {result.get('error')}")


def run_uploads(client) -> None:
    with RunLock("uploads") as acquired:
        if not acquired:
            log.info("Uploads already running; skipped")
            return

        pipe = client.pipeline(transaction=False)
        pipe.get(ss.REFRESH_REQUEST_KEY)
        pipe.rpop(ss.UPLOAD_QUEUE_KEY)
        refresh_flag, queued = pipe.execute()

        if refresh_flag:
            try:
                run_export(client, reason="requested")
            except Exception as e:
                log.error(f"Requested export failed: {e}")

        uploader = None
        processed = 0
        while queued:
            survey_id = (ss.parse_json(queued) or {}).get('survey_id')
            if survey_id:
                uploader = uploader or Uploader(client)
                try:
                    uploader.process(str(survey_id))
                except Exception as e:
                    log.error(f"Upload {survey_id}: unexpected error: {e}")
                processed += 1
            queued = client.rpop(ss.UPLOAD_QUEUE_KEY)

        if processed:
            client.incr(ss.WORKER_VERSION_KEY)
            log.info(f"Processed {processed} upload job(s)")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ('export', 'uploads'):
        print(__doc__)
        return 2
    setup_logging()
    try:
        client = ss.connect()
        if sys.argv[1] == 'export':
            run_export(client, reason="scheduled")
        else:
            run_uploads(client)
        return 0
    except Exception as e:
        log.error(f"{sys.argv[1]} run failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
