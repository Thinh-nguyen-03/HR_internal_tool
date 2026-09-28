"""JazzHR (Resumator API v1) client: applicant search, file listing, report
matching and report upload."""
import base64
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

import requests

from security_utils import is_safe_url

JAZZHR_BASE_URL = "https://api.resumatorapi.com/v1"

# When several JazzHR applicants share the survey taker's exact name, check at
# most this many (newest first) for an existing report.
MAX_APPLICANTS_PER_NAME = 5


class RateLimiter:
    """Rolling one-minute call limit shared by every thread that calls wait().

    JazzHR allows 80 calls per minute per API key; the default keeps a margin.
    """

    def __init__(self, calls_per_minute: int = 72, on_wait: Optional[Callable[[float], None]] = None):
        self.calls_per_minute = calls_per_minute
        self._on_wait = on_wait
        self._lock = threading.Lock()
        self._call_times: List[datetime] = []

    def wait(self, limit: Optional[int] = None) -> None:
        """Block until a call fits in the window, then record it.

        `limit` lowers the ceiling for this caller: background work passes a
        smaller number so interactive checks always find room in the window.
        """
        ceiling = min(limit or self.calls_per_minute, self.calls_per_minute)
        while True:
            with self._lock:
                now = datetime.now()
                cutoff = now - timedelta(minutes=1)
                self._call_times = [t for t in self._call_times if t >= cutoff]
                if len(self._call_times) < ceiling:
                    self._call_times.append(now)
                    return
                oldest_blocking = self._call_times[len(self._call_times) - ceiling]
                wait_time = max(60 - (now - oldest_blocking).total_seconds() + 0.1, 0.1)
            if self._on_wait:
                self._on_wait(wait_time)
            time.sleep(wait_time)


class JazzHRAPIError(Exception):
    """A JazzHR request failed. Callers must not read this as 'not found'."""


def _normalize_name(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def survey_id_from_report_url(url: str) -> Optional[str]:
    """Report URLs end in First_Last_(12345678).pdf; return the number in brackets."""
    if not url or '(' not in url:
        return None
    return url.split('/')[-1].split('_')[-1].replace('(', '').replace(')', '').replace('.pdf', '')


class JazzHRUploadChecker:
    def __init__(self, api_key: str, before_request: Optional[Callable[[], None]] = None):
        """before_request is called before every API call (used for rate limiting)."""
        self.api_key = api_key
        self.base_url = JAZZHR_BASE_URL
        self._before_request = before_request

        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=3,
            pool_block=False
        )
        self.session.mount('https://', adapter)
        self.session.headers.update({'Connection': 'keep-alive'})

    def _throttle(self) -> None:
        if self._before_request:
            self._before_request()

    def _make_request(self, endpoint: str, verbose: bool = False):
        """GET an endpoint and return the parsed JSON. Raises JazzHRAPIError on any failure."""
        self._throttle()
        url = f"{self.base_url}{endpoint}"
        if verbose:
            print(f"    [API] GET {endpoint}")

        start = time.time()
        try:
            response = self.session.get(url, params={'apikey': self.api_key}, timeout=30)
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            raise JazzHRAPIError(f"GET {endpoint} failed (HTTP {status}): {type(e).__name__}") from None
        except ValueError:
            raise JazzHRAPIError(f"GET {endpoint} returned non-JSON content") from None

        if isinstance(result, dict) and ("_error" in result or "error" in result):
            raise JazzHRAPIError(f"GET {endpoint} returned an error: {result.get('_error') or result.get('error')}")

        if verbose:
            count = len(result) if isinstance(result, list) else 1
            print(f"    [API] {response.status_code} in {time.time() - start:.2f}s, {count} item(s)")
        return result

    def search_applicants_by_name(self, first_name: str, last_name: str, verbose: bool = False) -> List[Dict]:
        """Applicants whose first and last name exactly match (case and spacing
        ignored), newest application first.

        JazzHR's name search is a loose match ("Patricia" returns every Patricia),
        and one person can have several applicant records.
        """
        full_name = f"{first_name.strip()} {last_name.strip()}"
        response = self._make_request(f"/applicants/name/{quote(full_name)}", verbose=verbose)
        applicants = response if isinstance(response, list) else [response] if response else []

        want_first, want_last = _normalize_name(first_name), _normalize_name(last_name)
        exact = [
            a for a in applicants
            if _normalize_name(a.get('first_name')) == want_first
            and _normalize_name(a.get('last_name')) == want_last
        ]
        exact.sort(key=lambda a: a.get('apply_date') or '', reverse=True)

        if verbose:
            print(f"    [SEARCH] '{full_name}': {len(applicants)} result(s), {len(exact)} exact match(es): "
                  f"{[a.get('id') for a in exact[:MAX_APPLICANTS_PER_NAME]]}")
        return exact

    def get_applicant_files(self, applicant_id: str, verbose: bool = False) -> List[Dict]:
        response = self._make_request(f"/files/applicant_id/{applicant_id}", verbose=verbose)
        files = response if isinstance(response, list) else [response] if response else []
        own_files = [f for f in files if f.get('applicant_id', '') == applicant_id]

        if verbose:
            print(f"    [FILES] {applicant_id}: {len(own_files)} file(s) "
                  f"{[f.get('filename') for f in own_files[:10]]}")
        return own_files

    def check_pdf_match(
        self,
        survey_pdf_url: str,
        survey_pdf_size: Optional[int],
        jazzhr_files: List[Dict],
        first_name: str = "",
        last_name: str = "",
        verbose: bool = False
    ) -> Optional[Dict]:
        """Find the survey's Culture Index report among one applicant's files.

        Accepted evidence, strongest first:
        1. The survey ID appears in the filename.
        2. The filename equals the Culture Index report filename.
        3. The filename carries "CultureIndex" (this tool's upload name) plus the person's name.
        4. The filename contains the person's name AND the size matches the report size.
        Size alone is never enough.
        """
        if not survey_pdf_url:
            return None

        ci_filename = survey_pdf_url.split('/')[-1]
        ci_filename_lower = ci_filename.lower()
        survey_id = survey_id_from_report_url(survey_pdf_url)
        full_name_clean = f"{first_name} {last_name}".lower() if first_name and last_name else ""

        for file_data in jazzhr_files:
            jazz_filename = (file_data.get('filename') or '').strip()
            jazz_size = int(file_data.get('file_size') or 0)
            jazz_lower = jazz_filename.lower()

            match_type = None
            if ci_filename_lower == jazz_lower or ci_filename_lower.replace('.pdf', '') == jazz_lower.replace('.pdf', ''):
                match_type = 'exact_filename'
            elif survey_id and first_name and last_name and \
                    f"{first_name}_{last_name}_({survey_id})".lower().replace(' ', '_') in jazz_lower.replace(' ', '_'):
                match_type = 'uploaded_filename_pattern'
            elif full_name_clean and full_name_clean in jazz_lower:
                match_type = 'full_name'
            elif first_name and last_name and f"{first_name.lower()}_{last_name.lower()}" in jazz_lower.replace(' ', '_'):
                match_type = 'name_pattern'
            name_match = match_type is not None

            size_match = False
            if survey_pdf_size and jazz_size:
                size_tolerance = max(2048, int(survey_pdf_size * 0.02))
                size_match = abs(jazz_size - survey_pdf_size) <= size_tolerance

            # "CultureIndex" in the filename is this tool's own upload signature.
            # Culture Index regenerates report PDFs over time, so the size fetched
            # today rarely equals the size stored at upload time; don't require it.
            is_ci_report_file = 'cultureindex' in jazz_lower.replace(' ', '').replace('_', '').replace('-', '')

            is_match = False
            if survey_id and survey_id in jazz_filename:
                match_type = 'survey_id_in_filename'
                is_match = True
            elif match_type == 'exact_filename':
                is_match = True
            elif is_ci_report_file and name_match:
                is_match = True
            elif name_match and size_match:
                # A name-only match could be a resume or cover letter; require the size too.
                is_match = True

            if is_match:
                matched_by = f'name ({match_type}) and size' if size_match else f'name ({match_type})'
                if verbose:
                    print(f"    [MATCH] {jazz_filename} matched by {matched_by}")
                return {
                    'file': file_data,
                    'matched_by': matched_by,
                    'match_type': match_type,
                    'name_match': name_match,
                    'size_match': size_match
                }

        if verbose:
            print(f"    [MATCH] no match among {len(jazzhr_files)} file(s) (CI file {ci_filename}, size {survey_pdf_size})")
        return None

    def check_survey_status(
        self,
        first_name: str,
        last_name: str,
        pdf_url: Optional[str],
        pdf_size: Optional[int],
        verbose: bool = False
    ) -> Dict:
        """Decide a survey's JazzHR status. Raises JazzHRAPIError if JazzHR could not be read.

        The report counts as uploaded if it is on ANY applicant record with the
        person's exact name. Uploads target the newest such record.
        """
        first_name = (first_name or '').strip()
        last_name = (last_name or '').strip()
        if not first_name or not last_name:
            return {"status": "MISSING_NAME", "isUploaded": False}
        if not pdf_url:
            return {"status": "NO_PDF_URL", "isUploaded": False}

        applicants = self.search_applicants_by_name(first_name, last_name, verbose=verbose)
        if not applicants:
            return {"status": "NOT_IN_JAZZHR", "isUploaded": False}

        candidates = applicants[:MAX_APPLICANTS_PER_NAME]
        file_count = 0
        for applicant in candidates:
            files = self.get_applicant_files(applicant.get('id'), verbose=verbose)
            file_count += len(files)
            match = self.check_pdf_match(pdf_url, pdf_size, files, first_name, last_name, verbose=verbose)
            if match:
                return {
                    "status": "UPLOADED",
                    "applicantId": applicant.get('id'),
                    "isUploaded": True,
                    "match": match,
                    "file_count": len(files),
                    "applicant_count": len(applicants),
                    "had_pdf_size": pdf_size is not None,
                }

        return {
            "status": "NOT_UPLOADED",
            "applicantId": candidates[0].get('id'),
            "isUploaded": False,
            "file_count": file_count,
            "applicant_count": len(applicants),
            "had_pdf_size": pdf_size is not None,
        }

    def upload_file_to_applicant(
        self,
        applicant_id: str,
        pdf_url: str,
        first_name: str,
        last_name: str,
        verbose: bool = False,
        pdf_bytes: Optional[bytes] = None,
    ) -> Dict:
        """Attach a report PDF to an applicant as First_Last_CultureIndex.pdf.

        Pass pdf_bytes when the caller already fetched the PDF; otherwise it is
        downloaded from pdf_url. Content that is not a PDF is never uploaded.
        """
        if pdf_bytes is not None:
            pdf_content = pdf_bytes
            if pdf_content[:5] != b'%PDF-':
                return {"success": False, "error": "Pre-fetched content is not a PDF"}
        else:
            is_safe, error_msg = is_safe_url(pdf_url, verbose=verbose)
            if not is_safe:
                return {"success": False, "error": f"Unsafe URL blocked: {error_msg}"}

            try:
                resp = self.session.get(pdf_url, timeout=30)
                resp.raise_for_status()
                pdf_content = resp.content
            except Exception as e:
                return {"success": False, "error": f"Failed to download PDF: {e}"}

            if pdf_content[:5] != b'%PDF-':
                ctype = resp.headers.get('Content-Type', 'unknown')
                return {"success": False, "error": f"Downloaded content is not a PDF (got {len(pdf_content)} bytes, Content-Type={ctype})"}

        if not self.api_key:
            return {"success": False, "error": "JazzHR API key is not set"}

        safe_first = ''.join(c for c in first_name if c.isalnum() or c in ' -_').strip().replace(' ', '_')
        safe_last = ''.join(c for c in last_name if c.isalnum() or c in ' -_').strip().replace(' ', '_')
        filename = f"{safe_first}_{safe_last}_CultureIndex.pdf"

        # POST /files parses ONLY a JSON body (confirmed in JazzHR's Swagger);
        # form-encoded fields are silently ignored. Errors come back as HTTP 200
        # with {"_error": "..."}.
        payload = {
            "apikey": self.api_key,
            "applicant_id": applicant_id,
            "filename": filename,
            "file_data": base64.b64encode(pdf_content).decode('utf-8'),
            "file_privacy": "0",
        }

        if verbose:
            print(f"    [UPLOAD] Sending JSON body for {filename} ({len(pdf_content)} bytes) to applicant {applicant_id}")

        try:
            self._throttle()
            resp = self.session.post(f"{self.base_url}/files", json=payload, timeout=60)
            resp.raise_for_status()
            result = resp.json()
        except requests.RequestException as e:
            error_msg = str(e)
            if getattr(e, 'response', None) is not None:
                error_msg = e.response.text[:200]
            return {"success": False, "error": f"Upload failed: {error_msg}"}
        except ValueError:
            return {"success": False, "error": "Upload failed: JazzHR returned non-JSON content"}

        if verbose:
            print(f"    [UPLOAD] Response: {result}")

        if isinstance(result, str):
            if result.lower().startswith("error"):
                return {"success": False, "error": result}
            return {"success": True, "message": result, "filename": filename}

        if isinstance(result, dict) and ("error" in result or "_error" in result):
            return {"success": False, "error": result.get("error") or result.get("_error")}

        return {
            "success": True,
            "message": f"Uploaded {filename}",
            "file_id": result.get("id") if isinstance(result, dict) else None,
            "filename": filename
        }
