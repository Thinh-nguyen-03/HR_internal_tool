from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

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
        timezone_offset: int = -360,
    ) -> dict[str, Any]:
        login_url = f"{self.config.base_url}/api/identity/Login"
        
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
            
            if response.status_code == 401:
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
    
    def get(self, endpoint: str, **kwargs) -> dict[str, Any]:
        if not self.token:
            raise CultureIndexAuthError("Not authenticated. Call login() first.")
        
        url = f"{self.config.base_url}{endpoint}"
        response = self.session.get(url, timeout=self.config.timeout, **kwargs)
        
        if response.status_code == 401:
            raise CultureIndexAuthError("Token expired or invalid")
        
        response.raise_for_status()
        return response.json()
    
    def post(self, endpoint: str, data: Optional[dict] = None, **kwargs) -> dict[str, Any]:
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
        
        endpoint = f"/api/Surveys/{client_id}/DataTable/MainSurveys"
        url = f"{self.config.base_url}{endpoint}"
        
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
        
        if response.status_code == 401:
            raise CultureIndexAuthError("Token expired or invalid")
        
        response.raise_for_status()
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