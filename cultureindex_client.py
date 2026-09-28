"""Culture Index portal client, CSV parsing and report-size lookup.

The portal endpoints used here are the ones the portal's own web UI calls; they
are not a documented public API.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, quote

import requests
from requests.adapters import HTTPAdapter, Retry

from security_utils import is_safe_url

try:
    import phonenumbers
    from phonenumbers import NumberParseException
    PHONENUMBERS_AVAILABLE = True
except ImportError:
    PHONENUMBERS_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_BASE_URL = "https://portal.cultureindex.com"
DEFAULT_TIMEOUT = (10, 30)
# The export is built on demand by Culture Index and has taken over a minute to
# start responding; it runs in the background worker, so it can wait.
EXPORT_TIMEOUT = (10, 180)
RETRY_STATUSES = (429, 500, 502, 503, 504)
USER_AGENT = "cultureindex-api-client/1.0"

logging.basicConfig(
    stream=sys.stdout,
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cultureindex")


class CultureIndexAuthError(Exception):
    pass


@dataclass(frozen=True)
class CultureIndexConfig:
    base_url: str = DEFAULT_BASE_URL
    timeout: tuple[int, int] = DEFAULT_TIMEOUT


def _raise_for_status(response: requests.Response) -> None:
    """401 means the token is stale (callers re-login on this message). 403 is
    usually Culture Index's Azure Front Door firewall rejecting this server's IP,
    which no amount of re-authenticating will fix."""
    if response.status_code == 401:
        raise CultureIndexAuthError("Token expired or invalid")
    if response.status_code == 403:
        body = response.text[:200].strip()
        raise CultureIndexAuthError(f"Access forbidden (HTTP 403): {body}")
    response.raise_for_status()


class CultureIndexClient:
    def __init__(
        self,
        config: Optional[CultureIndexConfig] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config or CultureIndexConfig()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        adapter = HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=0.6,
                status_forcelist=RETRY_STATUSES,
                # POSTs (login, CSV export) are not retried automatically: a stalled
                # export would otherwise hold a run for minutes, and the next cron run retries anyway.
                allowed_methods=frozenset(["GET"]),
                raise_on_status=False,
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.token: Optional[str] = None

    def login(
        self,
        email: str,
        password: str,
        remember: bool = True,
        timezone_offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """Authenticate with Culture Index and store the bearer token on the session."""
        login_url = f"{self.config.base_url}/api/identity/Login"

        if timezone_offset is None:
            try:
                now = datetime.now(timezone.utc).astimezone()
                timezone_offset = int(now.utcoffset().total_seconds() // 60)
            except Exception:
                timezone_offset = 0

        payload = {
            "username": email,
            "password": password,
            "remember": remember,
            "timezoneOffset": timezone_offset,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = self.session.post(
                login_url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
            )

            if response.status_code == 400:
                error_message = "Bad Request (400): Invalid request parameters"
                log.error(f"Bad Request (400) - Response body: {response.text[:500]}")
                try:
                    validation_msg = response.json().get("errors", {}).get("ValidationException")
                    if isinstance(validation_msg, list) and validation_msg:
                        error_message = f"Authentication failed: {validation_msg[0]}"
                    elif isinstance(validation_msg, str):
                        error_message = f"Authentication failed: {validation_msg}"
                except (ValueError, AttributeError):
                    pass
                raise CultureIndexAuthError(error_message)
            if response.status_code == 401:
                raise CultureIndexAuthError("Invalid credentials")
            _raise_for_status(response)

            data = response.json()
            if "token" not in data:
                raise CultureIndexAuthError("No token in response")

            self.token = data["token"]
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
            return data

        except requests.RequestException as e:
            raise CultureIndexAuthError(f"Login request failed: {e}")

    def is_authenticated(self) -> bool:
        return self.token is not None

    def download_report_pdf(self, survey_report_url: str, timeout: int = 30) -> bytes:
        """Download the real report PDF for a survey.

        The CSV's surveyReportUrl (https://surveys.cultureindex.com/r/<token>/<file>.pdf)
        has at times served an HTML viewer instead of the PDF. The portal endpoint
        /api/reports/survey/<token>/<file>.pdf?version=1 returns the PDF itself.
        Anything that does not start with the PDF signature is rejected.
        """
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")

        parsed = urlparse(survey_report_url)
        parts = [p for p in parsed.path.split('/') if p]

        if len(parts) >= 3 and parts[0] == 'r':
            token, filename = parts[1], parts[2]
        elif len(parts) >= 5 and parts[:3] == ['api', 'reports', 'survey']:
            token, filename = parts[3], parts[4]
        else:
            raise ValueError(f"Unexpected survey report URL format: {survey_report_url}")

        pdf_url = f"{self.config.base_url}/api/reports/survey/{token}/{quote(filename)}?version=1"
        response = self.session.get(pdf_url, timeout=timeout, allow_redirects=True)
        _raise_for_status(response)

        content = response.content
        if content[:5] != b'%PDF-':
            ctype = response.headers.get('Content-Type', 'unknown')
            raise ValueError(
                f"Report endpoint did not return a PDF "
                f"(Content-Type={ctype}, {len(content)} bytes) for {pdf_url}"
            )
        return content

    def export_surveys_csv(
        self,
        client_id: str,
        from_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        show_confidential: bool = False,
        show_hidden: bool = False,
        sort_by: str = 'surveyDate',
        sort_direction: int = 2,
    ) -> str:
        """Export all surveys as CSV. This is the only portal call that includes
        the report URL. sort_direction 2 = newest first."""
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")

        payload = {
            "andClause": "or",
            "Confidential": str(show_confidential).lower(),
            "ShowHidden": str(show_hidden).lower(),
            "DateRanges": [{
                "DateFieldName": "SurveyDate",
                "FromDt": from_date.isoformat() if from_date else None,
                "EndDt": end_date.isoformat() if end_date else None
            }],
            "start": 0,
            "length": 0,
            "columnToSortBy": sort_by,
            "direction": sort_direction,
            "searchValues": [
                {"key": "andClause", "value": "or"},
                {"key": "Confidential", "value": str(show_confidential).lower()},
                {"key": "ShowHidden", "value": str(show_hidden).lower()},
                {"key": "DateRanges", "value": "[object Object]"}
            ],
            "export": True
        }

        url = f"{self.config.base_url}/api/Surveys/{client_id}/DataTable/MainSurveys"
        response = self.session.post(
            url,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
            },
            json=payload,
            timeout=EXPORT_TIMEOUT
        )
        log.warning(f"CSV export response: status={response.status_code}, size={len(response.content)} bytes")
        _raise_for_status(response)
        return response.text


def format_phone_number(phone_str: Optional[str]) -> Optional[str]:
    """Format a phone number in international style if it parses as valid."""
    if not phone_str or not phone_str.strip() or not PHONENUMBERS_AVAILABLE:
        return phone_str

    for region in ["US", None]:
        try:
            parsed = phonenumbers.parse(phone_str, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        except NumberParseException:
            continue

    return phone_str


def parse_surveys_csv(csv_data: str) -> List[Dict]:
    """Map the CSV export's columns to survey dicts. Rows without a survey ID are skipped."""
    surveys = []
    reader = csv.DictReader(StringIO(csv_data))

    col_map = {}
    for col in reader.fieldnames or []:
        clean = col.replace('﻿', '').replace('ï»¿', '').strip()
        col_map[clean] = col

    for row in reader:
        def get_val(key):
            if key in col_map:
                return (row.get(col_map[key]) or '').strip()
            for k, v in col_map.items():
                if key.lower() in k.lower():
                    return (row.get(v) or '').strip()
            return ''

        survey_id = get_val('Survey Id') or get_val('SurveyId')
        if not survey_id:
            continue

        surveys.append({
            "surveyId": survey_id,
            "firstName": get_val('First Name') or get_val('FirstName'),
            "lastName": get_val('Last Name') or get_val('LastName'),
            "email": get_val('Email'),
            "phoneNumber": format_phone_number(get_val('Phone Number') or get_val('PhoneNumber')),
            "traitPattern": get_val('Trait Pattern') or get_val('TraitPattern'),
            "surveyDate": get_val('Survey Date') or get_val('SurveyDate') or None,
            "position": get_val('Positions Applied To') or get_val('PositionsAppliedTo') or get_val('Position'),
            "surveyReportUrl": get_val('Survey Report URL') or get_val('SurveyReportURL'),
        })

    return surveys


def fetch_pdf_sizes(urls: Dict[str, str], timeout: int = 5, max_workers: int = 8) -> Dict[str, int]:
    """Look up report PDF sizes with a one-byte Range request. Returns {survey_id: bytes}
    for the ones that answered with a real PDF; others are left out.

    Only PDF responses count. A report URL that serves an HTML viewer would
    otherwise record the viewer's size and break size-based matching.
    """
    def get_size(survey_id: str, url: str):
        is_safe, error_msg = is_safe_url(url, verbose=False)
        if not is_safe:
            log.error(f"Blocked unsafe URL for survey {survey_id}: {error_msg}")
            return survey_id, None

        try:
            with requests.get(url, headers={'Range': 'bytes=0-0', 'User-Agent': 'Mozilla/5.0'},
                              timeout=timeout, stream=True, allow_redirects=True) as resp:
                resp.raise_for_status()
                if 'pdf' not in resp.headers.get('Content-Type', '').lower():
                    return survey_id, None
                content_range = resp.headers.get('Content-Range')
                if content_range:
                    match = re.search(r'/(\d+)', content_range)
                    if match:
                        return survey_id, int(match.group(1))
                size = resp.headers.get('Content-Length')
                return survey_id, int(size) if size else None
        except Exception:
            return survey_id, None

    results = {}
    if not urls:
        return results
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(get_size, sid, url) for sid, url in urls.items() if url]
        for future in as_completed(futures):
            survey_id, size = future.result()
            if size:
                results[survey_id] = size
    return results


def main():
    """Login check: `python cultureindex_client.py` tells you whether this machine can reach Culture Index."""
    email = os.getenv("CULTUREINDEX_EMAIL")
    password = os.getenv("CULTUREINDEX_PASSWORD")
    if not email or not password:
        print("Set CULTUREINDEX_EMAIL and CULTUREINDEX_PASSWORD")
        return 1

    try:
        CultureIndexClient().login(email=email.strip(), password=password.strip())
        print("Culture Index login OK")
        return 0
    except CultureIndexAuthError as e:
        print(f"Culture Index login failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
