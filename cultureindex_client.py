from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse, quote

import requests
from requests.adapters import HTTPAdapter, Retry

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_BASE_URL = "https://portal.cultureindex.com"
DEFAULT_TIMEOUT = (10, 30)
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

class CultureIndexAPIError(Exception):
    pass

@dataclass(frozen=True)
class CultureIndexConfig:
    base_url: str = DEFAULT_BASE_URL
    timeout: tuple[int, int] = DEFAULT_TIMEOUT

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
                allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "PATCH"]),
                raise_on_status=False,
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        self.token: Optional[str] = None
        self.user_info: Optional[dict[str, Any]] = None
    
    def login(
        self,
        email: str,
        password: str,
        remember: bool = True,
        timezone_offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """Authenticate with Culture Index API and store bearer token."""
        login_url = f"{self.config.base_url}/api/identity/Login"
        
        # Auto-detect timezone offset if not provided
        if timezone_offset is None:
            try:
                now = datetime.now(timezone.utc).astimezone()
                local_offset = int(now.utcoffset().total_seconds() // 60)
                timezone_offset = local_offset
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
            log.warning(f"Login attempt: username={email}, timezoneOffset={timezone_offset}, remember={remember}")
            response = self.session.post(
                login_url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
            )
            
            # Parse validation errors from API response
            if response.status_code == 400:
                error_message = "Bad Request (400): Invalid request parameters"
                try:
                    error_body = response.text[:500]
                    log.error(f"Bad Request (400) - Response body: {error_body}")
                    try:
                        error_json = response.json()
                        if "errors" in error_json:
                            validation_errors = error_json.get("errors", {})
                            if "ValidationException" in validation_errors:
                                validation_msg = validation_errors["ValidationException"]
                                if isinstance(validation_msg, list) and validation_msg:
                                    error_message = f"Authentication failed: {validation_msg[0]}"
                                elif isinstance(validation_msg, str):
                                    error_message = f"Authentication failed: {validation_msg}"
                    except (ValueError, KeyError, TypeError):
                        pass
                except Exception:
                    pass
                raise CultureIndexAuthError(error_message)
            elif response.status_code == 401:
                raise CultureIndexAuthError("Invalid credentials")
            elif response.status_code == 403:
                raise CultureIndexAuthError("Access forbidden")
            
            response.raise_for_status()
            data = response.json()
            
            if "token" not in data:
                raise CultureIndexAuthError("No token in response")
            
            self.token = data["token"]
            self.session.headers.update({
                "Authorization": f"Bearer {self.token}"
            })
            self.user_info = self._extract_user_info_from_response(data)
            
            return data
            
        except requests.RequestException as e:
            raise CultureIndexAuthError(f"Login request failed: {e}")
    
    def _extract_user_info_from_response(self, data: dict) -> dict[str, Any]:
        return {
            "token": data.get("token"),
            "authenticated": True,
            "login_time": datetime.now(timezone.utc).isoformat(),
        }
    
    def download_report_pdf(self, survey_report_url: str, timeout: int = 30) -> bytes:
        """Download the real report PDF for a survey.

        The CSV export's surveyReportUrl
        (https://surveys.cultureindex.com/r/<token>/<file>.pdf) now serves an
        HTML viewer SPA, not a PDF. The actual PDF is served by the authenticated
        portal endpoint
        (https://portal.cultureindex.com/api/reports/survey/<token>/<file>.pdf?version=1),
        which redirects through an /authorize step that this logged-in session
        (bearer token + login cookies) can satisfy. Returns the raw PDF bytes.
        """
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")

        parsed = urlparse(survey_report_url)
        parts = [p for p in parsed.path.split('/') if p]

        # Supported shapes:
        # - Viewer URL from CSV: /r/<token>/<filename>
        # - Portal PDF URL:      /api/reports/survey/<token>/<filename>
        if len(parts) >= 3 and parts[0] == 'r':
            token, filename = parts[1], parts[2]
        elif len(parts) >= 5 and parts[:3] == ['api', 'reports', 'survey']:
            token, filename = parts[3], parts[4]
        else:
            raise ValueError(f"Unexpected survey report URL format: {survey_report_url}")

        pdf_url = f"{self.config.base_url}/api/reports/survey/{token}/{quote(filename)}?version=1"
        response = self.session.get(pdf_url, timeout=timeout, allow_redirects=True)

        if response.status_code == 401:
            raise CultureIndexAuthError("Token expired or invalid")
        response.raise_for_status()

        content = response.content
        if content[:5] != b'%PDF-':
            ctype = response.headers.get('Content-Type', 'unknown')
            raise ValueError(
                f"Report endpoint did not return a PDF "
                f"(Content-Type={ctype}, {len(content)} bytes) for {pdf_url}"
            )
        return content

    def get(self, endpoint: str, **kwargs) -> dict[str, Any]:
        """Make authenticated GET request to Culture Index API."""
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")
        
        url = f"{self.config.base_url}{endpoint}"
        response = self.session.get(url, timeout=self.config.timeout, **kwargs)
        
        if response.status_code == 401:
            raise CultureIndexAuthError("Token expired or invalid")
        
        response.raise_for_status()
        return response.json()
    
    def post(self, endpoint: str, data: Optional[dict] = None, **kwargs) -> dict[str, Any]:
        """Make authenticated POST request to Culture Index API."""
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")
        
        url = f"{self.config.base_url}{endpoint}"
        response = self.session.post(
            url,
            json=data,
            timeout=self.config.timeout,
            **kwargs
        )
        
        if response.status_code == 401:
            raise CultureIndexAuthError("Token expired or invalid")
        
        response.raise_for_status()
        return response.json()
    
    def get_surveys(
        self,
        client_id: str,
        start: int = 0,
        length: int = 150,
        sort_by: str = "surveyDate",
        direction: int = 2,
        confidential: str = "false",
        show_hidden: str = "false",
    ) -> dict[str, Any]:
        """Fetch paginated survey data for a client (direction: 1=asc, 2=desc)."""
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")
        
        endpoint = f"/api/Surveys/{client_id}/DataTable/MainSurveys"
        
        payload = {
            "andClause": "or",
            "Confidential": confidential,
            "ShowHidden": show_hidden,
            "DateRanges": [
                {
                    "DateFieldName": "SurveyDate",
                    "FromDt": None,
                    "EndDt": None,
                }
            ],
            "columnToSortBy": sort_by,
            "direction": direction,
            "includeCount": True,
            "length": length,
            "start": start,
        }
        
        return self.post(endpoint, data=payload)
    
    def get_all_surveys(
        self,
        client_id: str,
        max_surveys: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Fetch all surveys for a client (queries count first, then fetches in single request)."""
        # Get total count first
        response = self.get_surveys(
            client_id=client_id,
            start=0,
            length=1,
        )
        
        records_total = response.get("recordsTotal", 0)
        if records_total == 0:
            return []
        
        fetch_count = records_total
        if max_surveys:
            fetch_count = min(max_surveys, records_total)
        
        # Fetch all at once
        response = self.get_surveys(
            client_id=client_id,
            start=0,
            length=fetch_count,
        )
        
        return response.get("data", [])
    
    def get_survey_batch(
        self,
        client_id: str,
        start: int,
        batch_size: int = 500,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch survey batch and return (surveys, total_count)."""
        response = self.get_surveys(
            client_id=client_id,
            start=start,
            length=batch_size,
        )
        
        surveys = response.get("data", [])
        total = response.get("recordsTotal", 0)
        return surveys, total
    
    def is_authenticated(self) -> bool:
        return self.token is not None
    
    def export_surveys_csv(
        self,
        client_id: str,
        from_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        show_confidential: bool = False,
        show_hidden: bool = False,
        sort_by: str = 'surveyDate',
        sort_direction: int = 2
    ) -> str:
        """Export surveys as CSV (includes survey report URLs not available in JSON API)."""
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")
        
        log.warning(f"Preparing CSV export request for client {client_id}")
        
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
        
        endpoint = f"/api/Surveys/{client_id}/DataTable/MainSurveys"
        url = f"{self.config.base_url}{endpoint}"
        
        log.warning(f"Sending CSV export POST request to {endpoint}")
        response = self.session.post(
            url,
            headers={
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
            },
            json=payload,
            timeout=self.config.timeout
        )
        
        log.warning(f"CSV export response: status={response.status_code}, size={len(response.content)} bytes")
        
        if response.status_code == 401:
            raise CultureIndexAuthError("Token expired or invalid")
        
        response.raise_for_status()
        log.warning(f"CSV export successful, returning {len(response.text)} characters")
        return response.text

def main():
    email = os.getenv("CULTUREINDEX_EMAIL")
    password = os.getenv("CULTUREINDEX_PASSWORD")
    
    if not email or not password:
        log.error("Please set CULTUREINDEX_EMAIL and CULTUREINDEX_PASSWORD environment variables")
        return 1
    
    try:
        client = CultureIndexClient()
        client.login(email=email, password=password)
        log.info("Process completed successfully")
        return 0
        
    except CultureIndexAuthError as e:
        log.error("Process failed: Authentication error - %s", e)
        return 1
    except Exception as e:
        log.error("Process failed: Unexpected error - %s", e)
        return 1

if __name__ == "__main__":
    sys.exit(main())
