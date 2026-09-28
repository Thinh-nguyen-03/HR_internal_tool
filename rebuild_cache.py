"""Recheck every survey against JazzHR and rewrite the status store.

Run by hand on the VPS (it needs Culture Index access): `python rebuild_cache.py`.
It writes to the Redis that REDIS_URL and KEY_PREFIX point at, so with the
production settings it rewrites production statuses. Option 2 (resume) skips
surveys that already have a status.

It shares JazzHR's 80-calls-per-minute budget with the running app, so it
defaults to 30 calls a minute (REBUILD_CALLS_PER_MINUTE). About 7,000 surveys
take several hours; stop with Ctrl+C and resume with option 2.
"""
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import shared_state as ss  # noqa: E402
from cache_storage import StatusStore  # noqa: E402
from check_jazzhr_uploads import JazzHRUploadChecker, RateLimiter  # noqa: E402
from cultureindex_client import CultureIndexClient, parse_surveys_csv  # noqa: E402

CLIENT_ID = os.getenv('CLIENT_ID', 'A89F5B0000')
JAZZHR_API_KEY = os.getenv('JAZZHR_API_KEY')
REBUILD_CALLS_PER_MINUTE = int(os.getenv('REBUILD_CALLS_PER_MINUTE', '30'))
PROGRESS_EVERY = 50


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def main():
    if not JAZZHR_API_KEY:
        print("ERROR: JAZZHR_API_KEY not found in environment")
        sys.exit(1)
    ci_username = os.getenv('CULTUREINDEX_EMAIL')
    ci_password = os.getenv('CULTUREINDEX_PASSWORD')
    if not ci_username or not ci_password:
        print("ERROR: CULTUREINDEX_EMAIL and CULTUREINDEX_PASSWORD must be set")
        sys.exit(1)

    client = ss.connect()
    status_store = StatusStore(client)
    log(f"Redis key prefix: '{ss.KEY_PREFIX}'")

    print()
    print("  1. Clear all statuses and start fresh")
    print("  2. Resume - skip surveys that already have a status (recommended if interrupted)")
    choice = input("Choose option (1 or 2, default=2): ").strip()
    if choice == "1":
        log(f"Cleared {status_store.clear_all()} status entries")

    log("Logging in to Culture Index and exporting surveys...")
    ci_client = CultureIndexClient()
    ci_client.login(ci_username.strip(), ci_password.strip())
    surveys = parse_surveys_csv(ci_client.export_surveys_csv(client_id=CLIENT_ID))
    if not surveys:
        log("ERROR: No surveys found in CSV")
        sys.exit(1)
    log(f"Found {len(surveys)} surveys")

    # Report sizes come from the worker's snapshot when there is one.
    payload = client.get(ss.SNAPSHOT_KEY)
    sizes = {s['surveyId']: s.get('pdfSize') for s in ss.decode_snapshot(payload)} if payload else {}

    to_check = surveys
    if choice != "1":
        existing = {}
        for start in range(0, len(surveys), 500):
            existing.update(status_store.get_batch([s['surveyId'] for s in surveys[start:start + 500]]))
        to_check = [s for s in surveys if not existing.get(s['surveyId'])]
        log(f"{len(surveys) - len(to_check)} already have a status, {len(to_check)} to check")

    rate_limiter = RateLimiter(calls_per_minute=REBUILD_CALLS_PER_MINUTE)
    checker = JazzHRUploadChecker(JAZZHR_API_KEY, before_request=rate_limiter.wait)

    cached = errors = 0
    start_time = time.time()
    for index, survey in enumerate(to_check, 1):
        survey_id = survey['surveyId']
        try:
            result = checker.check_survey_status(
                survey.get('firstName', ''), survey.get('lastName', ''),
                survey.get('surveyReportUrl'), sizes.get(survey_id)
            )
        except Exception as e:
            # Left without a status so the app checks it again instead of trusting a failure.
            log(f"  Error checking {survey_id}: {e}")
            errors += 1
            continue
        result["timestamp"] = ss.utc_now_iso()
        status_store.set(survey_id, result)
        cached += 1
        if index % PROGRESS_EVERY == 0:
            log(f"{index} of {len(to_check)} checked")

    print()
    log(f"Done in {(time.time() - start_time) / 60:.1f} min: {cached} saved, {errors} errors")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Run again and choose option 2 to resume.")
        sys.exit(1)
