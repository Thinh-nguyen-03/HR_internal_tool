import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CENTRAL_TZ = ZoneInfo("America/Chicago")
from threading import RLock, Thread, Lock, Event
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import requests
from dash import Dash, html, dcc, Input, Output, State, callback, ctx, ALL
import dash
from dotenv import load_dotenv
from flask import redirect, request, session, jsonify
from flask_login import current_user, logout_user
from flask_wtf.csrf import CSRFProtect

from cultureindex_client import CultureIndexClient, CultureIndexAuthError
from surveys_fetch import format_phone_number
from check_jazzhr_uploads import JazzHRUploadChecker
from cache_storage import create_cache, SmartJazzHRCache
from auth import AuthManager
from login_layout import create_login_layout
from security_utils import is_safe_url, validate_url_or_raise
from input_validation import sanitize_search_query, validate_survey_id, validate_page_number

# New modular imports for cleaner architecture
from app_cache import get_cache_manager, CacheManager
from background_sync import create_background_sync_blueprint, perform_survey_refresh
from survey_display import (
    build_loading_result, build_error_result, 
    build_empty_result, build_notification_banner
)

load_dotenv()

ITEMS_PER_PAGE = int(os.getenv('ITEMS_PER_PAGE', '15'))
MAX_BATCH_UPLOAD = int(os.getenv('MAX_BATCH_UPLOAD', '15'))
MAX_BACKGROUND_CHECK = int(os.getenv('MAX_BACKGROUND_CHECK', '50'))
RECENT_SURVEY_THRESHOLD = int(os.getenv('RECENT_SURVEY_THRESHOLD', '1000'))
CLIENT_ID = os.getenv('CLIENT_ID', 'A89F5B0000')
JAZZHR_CACHE_HOURS = int(os.getenv('JAZZHR_CACHE_HOURS', '2'))

UPLOAD_MAX_RETRIES = int(os.getenv('UPLOAD_MAX_RETRIES', '3'))
UPLOAD_RETRY_DELAY_BASE = int(os.getenv('UPLOAD_RETRY_DELAY_BASE', '2'))
UPLOAD_RETRY_DELAY_MAX = int(os.getenv('UPLOAD_RETRY_DELAY_MAX', '10'))

POLL_INTERVAL_MS = int(os.getenv('POLL_INTERVAL_MS', '5000'))
UPLOAD_INTERVAL_MS = int(os.getenv('UPLOAD_INTERVAL_MS', '1000'))
PDF_FETCH_TIMEOUT = int(os.getenv('PDF_FETCH_TIMEOUT', '5'))
DIAG_STATUS_CHECK = os.getenv('DIAG_STATUS_CHECK', '0') == '1'
SURVEY_REFRESH_INTERVAL_MIN = int(os.getenv('SURVEY_REFRESH_INTERVAL_MIN', '10'))


def log(message: str, level: str = "INFO") -> None:
    if level not in ["ERROR", "WARN", "PERF"]:
        return
    
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{timestamp}] [{level}] {message}"
    print(line, flush=True)
    try:
        with open("app_simple_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} - [{level}] {message}\n")
    except Exception:
        pass


def get_user_friendly_error(error: Exception, context: str = "operation") -> str:
    import traceback
    
    detailed_error = f"{context} failed: {str(error)}\n{traceback.format_exc()}"
    log(detailed_error, "ERROR")
    
    error_messages = {
        "loading surveys": "Unable to load survey data. Please try refreshing the page.",
        "uploading file": "Upload failed. Please try again or contact support if the issue persists.",
        "connecting": "Unable to connect to the service. Please try again later.",
        "saving": "Unable to save changes. Please try again.",
        "authentication": "Authentication failed. Please check your credentials.",
    }
    
    return error_messages.get(context, "An error occurred. Please try again or contact support if the issue persists.")


def parse_csv_date(date_str: Optional[str]) -> Optional[str]:
    if not date_str or not date_str.strip():
        return None
    return date_str.strip()


class SimpleSurveyService:
    def __init__(self, client_id: str, items_per_page: int = 20):
        self.client_id = client_id
        self.items_per_page = items_per_page
        self._client = None
        self._all_surveys = []
        self._is_loaded = False
        self._is_loading = False
        self._lock = RLock()
    
    def _get_client(self) -> CultureIndexClient:
        if self._client and self._client.is_authenticated():
            log("Using existing authenticated client", "WARN")
            return self._client
        
        email = os.getenv("CULTUREINDEX_EMAIL")
        password = os.getenv("CULTUREINDEX_PASSWORD")
        
        if not email or not password:
            raise ValueError("Missing CULTUREINDEX_EMAIL or CULTUREINDEX_PASSWORD")
        
        email = email.strip()
        password = password.strip()
        
        log(f"Authenticating with Culture Index for {email[:3]}***", "WARN")
        self._client = CultureIndexClient()
        self._client.login(email=email, password=password)
        log("Authentication successful", "WARN")
        return self._client
    
    def _parse_csv_surveys(self, csv_data: str) -> List[Dict]:
        import csv
        from io import StringIO
        
        surveys = []
        try:
            csv_reader = csv.DictReader(StringIO(csv_data))
            
            col_map = {}
            if csv_reader.fieldnames:
                for col in csv_reader.fieldnames:
                    clean = col.replace('\ufeff', '').replace('\u00ef\u00bb\u00bf', '').strip()
                    col_map[clean] = col
            
            for row in csv_reader:
                def get_val(key):
                    if key in col_map:
                        return row.get(col_map[key], '').strip()
                    for k, v in col_map.items():
                        if key.lower() in k.lower():
                            return row.get(v, '').strip()
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
                    "surveyDate": parse_csv_date(get_val('Survey Date') or get_val('SurveyDate')),
                    "position": get_val('Positions Applied To') or get_val('PositionsAppliedTo') or get_val('Position'),
                    "surveyReportUrl": get_val('Survey Report URL') or get_val('SurveyReportURL'),
                })
                
        except Exception as e:
            log(f"CSV parse error: {e}", "ERROR")
        
        return surveys
    
    def load_surveys(self, force_refresh: bool = False) -> List[Dict]:
        with self._lock:
            if self._is_loaded and not force_refresh:
                return self._all_surveys
            
            if self._is_loading:
                log("Another thread is loading, returning current data", "WARN")
                return self._all_surveys
            
            self._is_loading = True
        
        try:
            log(f"Starting survey load (force_refresh={force_refresh})", "WARN")
            start = time.time()
            
            log("Getting authenticated client...", "WARN")
            client = self._get_client()
            
            log(f"Downloading CSV data for client {self.client_id}...", "WARN")
            csv_start = time.time()
            
            # Try to export surveys, retry once if token expired
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    csv_data = client.export_surveys_csv(client_id=self.client_id)
                    break  # Success, exit retry loop
                except CultureIndexAuthError as auth_error:
                    if "Token expired or invalid" in str(auth_error) and attempt < max_retries - 1:
                        log("Token expired, re-authenticating...", "WARN")
                        self._client = None
                        client = self._get_client()
                        log("Re-authentication successful, retrying request...", "WARN")
                    else:
                        raise
            
            log(f"CSV download completed in {time.time()-csv_start:.1f}s, size: {len(csv_data)} bytes", "WARN")
            
            log("Parsing CSV data...", "WARN")
            parse_start = time.time()
            surveys = self._parse_csv_surveys(csv_data)
            log(f"CSV parsing completed in {time.time()-parse_start:.1f}s, found {len(surveys)} surveys", "WARN")
            
            with self._lock:
                self._all_surveys = surveys
                self._is_loaded = True
                self._is_loading = False
            
            log(f"Survey load complete: {len(surveys)} surveys in {time.time()-start:.1f}s", "WARN")
            return surveys
            
        except Exception as e:
            log(f"Failed to load surveys: {e}", "ERROR")
            with self._lock:
                self._is_loading = False
            raise
    
    def is_loaded(self) -> bool:
        return self._is_loaded
    
    def get_all_surveys(self) -> List[Dict]:
        return self._all_surveys
    
    def get_page(self, page_num: int) -> Tuple[List[Dict], int]:
        total = len(self._all_surveys)
        start_idx = (page_num - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        return self._all_surveys[start_idx:end_idx], total
    
    def search_surveys(self, query: str, limit: int = 100) -> List[Dict]:
        if not query or len(query) < 2:
            return []
        
        query_sanitized = sanitize_search_query(query, max_length=100)
        if not query_sanitized or len(query_sanitized) < 2:
            return []
        
        query_lower = query_sanitized.lower().strip()
        matches = []
        
        for survey in self._all_surveys:
            full_name = f"{survey.get('firstName', '')} {survey.get('lastName', '')}".lower()
            if query_lower in full_name:
                matches.append(survey)
                if len(matches) >= limit:
                    break
        
        return matches
    
    def clear_cache(self) -> None:
        with self._lock:
            self._all_surveys = []
            self._is_loaded = False
    
    def is_loading(self) -> bool:
        return self._is_loading
    
    def start_loading(self) -> None:
        with self._lock:
            self._is_loading = True

class SimpleJazzHRService:
    def __init__(self, api_key: str, cache, max_workers: int = 6):
        self.api_key = api_key
        self.cache = cache
        self.max_workers = max_workers
        self._checker = None
        self._lock = RLock()
        self._call_times = []
        self._rate_limit = 80
    
    def _get_checker(self) -> JazzHRUploadChecker:
        if self._checker is None:
            self._checker = JazzHRUploadChecker(api_key=self.api_key)
        return self._checker
    
    def _wait_for_rate_limit(self) -> None:
        wait_time = 0
        with self._lock:
            now = datetime.now()
            cutoff = now - timedelta(minutes=1)
            self._call_times = [t for t in self._call_times if t >= cutoff]
            
            if len(self._call_times) >= int(self._rate_limit * 0.9):
                wait_time = 60 - (now - self._call_times[0]).total_seconds() + 0.5
                if wait_time > 0:
                    self._call_times = []
        
        if wait_time > 0:
            log(f"Rate limit hit, waiting {wait_time:.1f}s", "WARN")
            time.sleep(wait_time)
        
        with self._lock:
            self._call_times.append(datetime.now())
    
    def check_one_survey(self, survey: Dict, pdf_url: str, pdf_size: Optional[int], force_diag: bool = False) -> Dict:
        survey_id = str(survey.get('surveyId', ''))
        diag = DIAG_STATUS_CHECK or force_diag

        cached = self.cache.get(survey_id)
        if cached and not force_diag:
            if cached.get('status') == 'NO_PDF_URL' and pdf_url:
                pass
            elif cached.get('status') == 'NOT_UPLOADED' and pdf_size and not cached.get('had_pdf_size'):
                pass
            else:
                return cached
        
        first_name = survey.get('firstName', '').strip()
        last_name = survey.get('lastName', '').strip()
        
        if not first_name or not last_name:
            result = {"status": "MISSING_NAME", "isUploaded": False, "timestamp": datetime.now().isoformat()}
            self.cache.set(survey_id, result)
            return result
        
        if not pdf_url:
            result = {"status": "NO_PDF_URL", "isUploaded": False, "timestamp": datetime.now().isoformat()}
            self.cache.set(survey_id, result)
            return result
        
        try:
            checker = self._get_checker()
            
            self._wait_for_rate_limit()
            applicant = checker.search_applicant_by_name(first_name, last_name, verbose=diag)

            if not applicant:
                if diag:
                    log(f"[DIAG] === {first_name} {last_name} === NO applicant returned by search -> NOT_IN_JAZZHR", "WARN")
                result = {"status": "NOT_IN_JAZZHR", "isUploaded": False, "timestamp": datetime.now().isoformat()}
                self.cache.set(survey_id, result)
                return result

            applicant_id = applicant.get('id')
            if diag:
                log(f"[DIAG] === {first_name} {last_name} === applicant id={applicant_id} keys={list(applicant.keys())}", "WARN")

            self._wait_for_rate_limit()
            files = checker.get_applicant_files(applicant_id, verbose=diag)

            match = checker.check_pdf_match(pdf_url, pdf_size, files, first_name, last_name, verbose=diag)
            if diag:
                ci_filename = pdf_url.split('/')[-1] if pdf_url else None
                log(f"[DIAG] {first_name} {last_name}: CI filename={ci_filename} CI size={pdf_size} -> {len(files)} matched file(s) -> {match.get('matched_by') if match else 'NO MATCH'}", "WARN")
            
            if match:
                result = {
                    "status": "UPLOADED",
                    "applicantId": applicant_id,
                    "isUploaded": True,
                    "match": match,
                    "file_count": len(files),
                    "had_pdf_size": pdf_size is not None,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                result = {
                    "status": "NOT_UPLOADED",
                    "applicantId": applicant_id,
                    "isUploaded": False,
                    "file_count": len(files),
                    "had_pdf_size": pdf_size is not None,
                    "timestamp": datetime.now().isoformat()
                }
            
            if cached:
                old_status = cached.get('status')
                new_status = result['status']
                if old_status != new_status:
                    name = f"{first_name} {last_name}"
                    if old_status == 'UPLOADED' and new_status == 'NOT_UPLOADED':
                        log(f"Status Change: {name} ({survey_id}) UPLOADED -> NOT_UPLOADED (potential false positive or file removed)", "WARN")
                    elif old_status == 'NOT_UPLOADED' and new_status == 'UPLOADED':
                        log(f"Status Change: {name} ({survey_id}) NOT_UPLOADED -> UPLOADED (file was uploaded or previous false negative)", "WARN")
                    elif old_status in ['NOT_IN_JAZZHR', 'ERROR'] and new_status in ['UPLOADED', 'NOT_UPLOADED']:
                        log(f"Status Change: {name} ({survey_id}) {old_status} -> {new_status}", "WARN")
            
            self.cache.set(survey_id, result)
            return result
            
        except Exception as e:
            log(f"JazzHR error for {survey_id}: {e}", "ERROR")
            return {"status": "ERROR", "isUploaded": False, "error": str(e), "timestamp": datetime.now().isoformat()}
    
    def check_surveys_batch(self, surveys: List[Dict], urls: Dict[str, str], pdf_sizes: Dict[str, int], force_diag: bool = False) -> Dict[str, Dict]:
        results = {}
        to_check = []

        for survey in surveys:
            survey_id = str(survey.get('surveyId', ''))
            cached = self.cache.get(survey_id)
            if cached and not force_diag:
                results[survey_id] = cached
            else:
                to_check.append(survey)

        if not to_check:
            return results

        def check_one(survey):
            survey_id = str(survey.get('surveyId', ''))
            url = urls.get(survey_id)
            size = pdf_sizes.get(survey_id)
            return survey_id, self.check_one_survey(survey, url, size, force_diag=force_diag)
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(check_one, s): s for s in to_check}
            for future in as_completed(futures):
                try:
                    survey_id, result = future.result()
                    results[survey_id] = result
                except Exception as e:
                    survey = futures[future]
                    survey_id = str(survey.get('surveyId', ''))
                    results[survey_id] = {"status": "ERROR", "isUploaded": False, "error": str(e)}
        
        self.cache.save()
        return results

    NON_RETRYABLE_ERRORS = [
        "401",
        "403",
        "404",
        "apikey not set",
        "invalid api key",
        "applicant_id was not set",
        "file already exists",
        "invalid data",
    ]
    
    def _is_retryable_error(self, error_msg: str) -> bool:
        if not error_msg:
            return True
        error_lower = error_msg.lower()
        for non_retryable in self.NON_RETRYABLE_ERRORS:
            if non_retryable.lower() in error_lower:
                return False
        return True
    
    def upload_pdf_to_jazzhr(self, survey: Dict, pdf_url: str, applicant_id: str) -> Dict:
        survey_id = str(survey.get('surveyId', ''))
        checker = self._get_checker()
        first_name = survey.get('firstName', '').strip()
        last_name = survey.get('lastName', '').strip()

        redis_client = None
        lock_key = None
        if hasattr(self.cache.cache, '_redis') and self.cache.cache._redis:
            redis_client = self.cache.cache._redis
            lock_key = f"upload_lock:{survey_id}"
            acquired = redis_client.set(lock_key, '1', nx=True, ex=60)
            if not acquired:
                log(f"Upload for {survey_id} skipped - another worker is already processing it", "WARN")
                return {'success': False, 'error': 'Upload already in progress', 'survey_id': survey_id}

        try:
            return self._do_upload(survey_id, checker, first_name, last_name, pdf_url, applicant_id)
        finally:
            if redis_client and lock_key:
                try:
                    redis_client.delete(lock_key)
                except Exception:
                    pass

    def _do_upload(self, survey_id: str, checker, first_name: str, last_name: str, pdf_url: str, applicant_id: str) -> Dict:
        last_error = None
        max_retries = UPLOAD_MAX_RETRIES
        delay_base = UPLOAD_RETRY_DELAY_BASE
        delay_max = UPLOAD_RETRY_DELAY_MAX

        # Fetch the real PDF via the authenticated Culture Index client. The
        # public surveyReportUrl serves an HTML viewer SPA; only the portal
        # report endpoint (reachable with our login session) returns the PDF.
        try:
            ci_client = survey_service._get_client()
            pdf_bytes = ci_client.download_report_pdf(pdf_url)
            log(f"Fetched real PDF for {survey_id}: {len(pdf_bytes)} bytes", "WARN")
        except Exception as e:
            log(f"Failed to fetch report PDF for {survey_id}: {e}", "ERROR")
            return {'success': False, 'error': f"Could not fetch report PDF: {e}", 'survey_id': survey_id}

        for attempt in range(max_retries):
            try:
                self._wait_for_rate_limit()

                result = checker.upload_file_to_applicant(
                    applicant_id=applicant_id,
                    pdf_url=pdf_url,
                    first_name=first_name,
                    last_name=last_name,
                    verbose=True,
                    pdf_bytes=pdf_bytes,
                )
                
                if result.get('success'):
                    self.cache.delete(survey_id)
                    self.cache.save()
                    return result
                
                error_msg = result.get('error', '')
                if not self._is_retryable_error(error_msg):
                    log(f"Upload failed with non-retryable error: {error_msg}", "ERROR")
                    return result
                
                last_error = error_msg
                
            except Exception as e:
                last_error = str(e)
                log(f"Upload exception on attempt {attempt + 1}: {e}", "ERROR")
            
            if attempt < max_retries - 1:
                delay = min(
                    delay_base * (2 ** attempt),
                    delay_max
                )
                time.sleep(delay)
        
        log(f"Upload failed after {max_retries} attempts for {survey_id}: {last_error}", "ERROR")
        return {
            'success': False,
            'error': f"Failed after {max_retries} attempts: {last_error}",
            'survey_id': survey_id
        }

def fetch_pdf_sizes(urls: Dict[str, str], pdf_cache, max_workers: int = 8) -> Dict[str, int]:
    if not urls:
        return {}
    
    results = {}
    to_fetch = {}
    
    for survey_id, url in urls.items():
        cached = pdf_cache.get(survey_id)
        if cached and 'size' in cached:
            results[survey_id] = cached['size']
        else:
            to_fetch[survey_id] = url
    
    if not to_fetch:
        return results
    
    def get_size(survey_id: str, url: str) -> Tuple[str, Optional[int], Optional[str]]:
        is_safe, error_msg = is_safe_url(url, verbose=False)
        if not is_safe:
            log(f"Blocked unsafe URL for survey {survey_id}: {error_msg}", "ERROR")
            return survey_id, None, f"Unsafe URL: {error_msg}"
        
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        try:
            resp = session.get(url, headers={'Range': 'bytes=0-0'}, timeout=PDF_FETCH_TIMEOUT, stream=True, allow_redirects=True)
            resp.raise_for_status()
            
            content_range = resp.headers.get('Content-Range')
            if content_range:
                import re
                match = re.search(r'/(\d+)', content_range)
                if match:
                    return survey_id, int(match.group(1)), None
            
            size = resp.headers.get('Content-Length')
            if size:
                return survey_id, int(size), None
            
            return survey_id, None, "No size header"
        except Exception as e:
            return survey_id, None, str(e)[:80]
        finally:
            session.close()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(get_size, sid, url): sid for sid, url in to_fetch.items()}
        for future in as_completed(futures):
            try:
                survey_id, size, error = future.result()
                if size:
                    results[survey_id] = size
                    pdf_cache.set(survey_id, {'size': size, 'status': 'pdf_size', 'isUploaded': False})
            except:
                pass
    
    if results:
        pdf_cache.save()
    
    return results


class BackgroundJazzHRChecker:
    """
    Background thread that proactively checks JazzHR upload status for surveys.
    Prioritizes first page for better UX, then processes remaining surveys in batches.
    """
    def __init__(self, survey_service, jazzhr_service, pdf_cache):
        self.survey_service = survey_service
        self.jazzhr_service = jazzhr_service
        self.pdf_cache = pdf_cache
        self._check_thread = None
        self._stop_flag = False
        self._lock = RLock()
    
    def start_checking(self) -> None:
        with self._lock:
            if self._check_thread and self._check_thread.is_alive():
                return
            self._stop_flag = False
            self._check_thread = Thread(target=self._check_worker, daemon=True, name="JazzHRChecker")
            self._check_thread.start()
    
    def stop_checking(self) -> None:
        self._stop_flag = True
    
    def _check_worker(self) -> None:
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                start_time = time.time()
                checks_performed = 0
                api_calls = 0
                
                log(f"JazzHR Checker: Starting (attempt {retry_count + 1}/{max_retries})", "WARN")
                
                max_wait = 60
                waited = 0
                while not self.survey_service.is_loaded() and waited < max_wait:
                    if self._stop_flag:
                        return
                    time.sleep(1)
                    waited += 1
                
                all_surveys = self.survey_service.get_all_surveys()
                if not all_surveys:
                    return
                
                total_surveys = len(all_surveys)
                first_page_count = ITEMS_PER_PAGE
                
                first_page_surveys = all_surveys[:first_page_count]
                first_page_need_check = []
                for s in first_page_surveys:
                    survey_id = str(s['surveyId'])
                    if not self.jazzhr_service.cache.get(survey_id):
                        first_page_need_check.append(s)
                
                log(f"Background Thread: First page has {len(first_page_surveys)} surveys, {len(first_page_need_check)} need checking", "WARN")
                
                if first_page_need_check:
                    log(f"JazzHR Checker: Prioritizing first page ({len(first_page_need_check)} uncached)", "WARN")
                    urls = {str(s['surveyId']): s.get('surveyReportUrl') for s in first_page_need_check}
                    urls_to_check = {sid: url for sid, url in urls.items() if url}
                    pdf_sizes = fetch_pdf_sizes(urls_to_check, self.pdf_cache)
                    self.jazzhr_service.check_surveys_batch(first_page_need_check, urls, pdf_sizes)
                    checks_performed += len(first_page_need_check)
                    api_calls += len(first_page_need_check) * 2
                    log(f"JazzHR Checker: First page checks complete", "WARN")
                
                # Mark initial checks as complete - using cache manager
                cache_mgr = get_cache_manager()
                cache_mgr.app_state.set_jazzhr_check_complete(True)
                log(f"JazzHR Checker: Initial checks complete, UI can now cache results", "WARN")
                
                remaining_surveys = all_surveys[first_page_count:MAX_BACKGROUND_CHECK]
                checked_count = len(first_page_need_check) if first_page_need_check else 0
                
                if remaining_surveys:
                    log(f"JazzHR Checker: Checking {len(remaining_surveys)} remaining surveys (of {total_surveys} total, older surveys use cache)", "WARN")
                    
                    batch_size = ITEMS_PER_PAGE
                    total_batches = (len(remaining_surveys) + batch_size - 1) // batch_size
                    log(f"Background Thread: Processing {total_batches} batches of {batch_size} surveys each", "WARN")
                    
                    for i in range(0, len(remaining_surveys), batch_size):
                        if self._stop_flag:
                            log(f"Background Thread: Stop flag set, terminating", "WARN")
                            break
                        
                        batch_num = (i // batch_size) + 1
                        batch = remaining_surveys[i:i + batch_size]
                        log(f"Background Thread: Processing batch {batch_num}/{total_batches} ({len(batch)} surveys)", "WARN")
                        
                        urls = {str(s['surveyId']): s.get('surveyReportUrl') for s in batch}
                        urls_to_check = {sid: url for sid, url in urls.items() if url}
                        pdf_sizes = fetch_pdf_sizes(urls_to_check, self.pdf_cache)
                        
                        batch_to_check = [s for s in batch if str(s['surveyId']) not in [str(cs['surveyId']) for cs in first_page_need_check]]
                        self.jazzhr_service.check_surveys_batch(batch, urls, pdf_sizes)
                        checked_count += len(batch)
                        checks_performed += len(batch_to_check)
                        api_calls += len(batch_to_check) * 2
                        
                        if i + batch_size < len(remaining_surveys):
                            time.sleep(1)
                
                total_time = time.time() - start_time
                avg_time_per_check = (total_time / checks_performed) if checks_performed > 0 else 0
                
                log(f"JazzHR Checker: Completed checking {checked_count} surveys", "WARN")
                log(f"Performance: {checks_performed} checks in {total_time:.2f}s ({avg_time_per_check:.2f}s avg), {api_calls} API calls", "PERF")
                
                # Log cache statistics
                if hasattr(jazzhr_cache.cache, 'get_stats'):
                    jazzhr_stats = jazzhr_cache.cache.get_stats()
                    log(f"JazzHR Cache: {jazzhr_stats['hits']} hits, {jazzhr_stats['misses']} misses, {jazzhr_stats['hit_rate_percent']}% hit rate", "PERF")
                
                if hasattr(pdf_size_cache, 'get_stats'):
                    pdf_stats = pdf_size_cache.get_stats()
                    log(f"PDF Cache: {pdf_stats['hits']} hits, {pdf_stats['misses']} misses, {pdf_stats['hit_rate_percent']}% hit rate", "PERF")
                
                # Signal UI to refresh with new JazzHR status - using cache manager
                cache_mgr = get_cache_manager()
                cache_mgr.on_jazzhr_check_complete()
                log(f"JazzHR Checker: Completed, signaling UI update", "WARN")
                
                # Success - exit retry loop
                break
                
            except Exception as e:
                retry_count += 1
                log(f"JazzHR Checker error (attempt {retry_count}/{max_retries}): {e}", "ERROR")
                
                if retry_count < max_retries:
                    retry_delay = 5 * retry_count
                    log(f"JazzHR Checker: Retrying in {retry_delay}s...", "WARN")
                    time.sleep(retry_delay)
                else:
                    log(f"JazzHR Checker: Failed after {max_retries} attempts", "ERROR")


class PeriodicRefresher:
    """In-app timer that re-runs the survey refresh on a fixed interval, so new
    Culture Index submissions surface within minutes instead of waiting for the
    hourly cron. Uses the same perform_survey_refresh() core as the cron endpoint.
    Safe with a single web worker; the daemon thread lives for the worker's life.
    """
    def __init__(self, survey_service, jazzhr_cache, background_checker, recent_threshold, interval_seconds):
        self.survey_service = survey_service
        self.jazzhr_cache = jazzhr_cache
        self.background_checker = background_checker
        self.recent_threshold = recent_threshold
        self.interval = max(60, interval_seconds)
        self._thread = None
        self._stop = Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, daemon=True, name="PeriodicRefresher")
        self._thread.start()
        log(f"Periodic survey refresher started (every {self.interval}s)", "WARN")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            try:
                perform_survey_refresh(
                    self.survey_service, self.jazzhr_cache,
                    self.background_checker, self.recent_threshold
                )
            except Exception as e:
                log(f"Periodic refresh error: {e}", "ERROR")


survey_service = SimpleSurveyService(client_id=CLIENT_ID, items_per_page=ITEMS_PER_PAGE)

jazzhr_cache_backend = create_cache("jazzhr_status", ttl_hours=JAZZHR_CACHE_HOURS, cache_file="jazzhr_status_cache.json")
pdf_size_cache = create_cache("pdf_sizes", ttl_hours=168, cache_file="pdf_sizes_cache.json")
jazzhr_cache = SmartJazzHRCache(jazzhr_cache_backend, recent_threshold=RECENT_SURVEY_THRESHOLD)

jazzhr_api_key = os.getenv('JAZZHR_API_KEY')
if not jazzhr_api_key:
    log("ERROR: JAZZHR_API_KEY not found!", "ERROR")
else:
    log("JazzHR API key loaded successfully", "WARN")

jazzhr_service = SimpleJazzHRService(api_key=jazzhr_api_key, cache=jazzhr_cache, max_workers=6)
background_checker = BackgroundJazzHRChecker(survey_service, jazzhr_service, pdf_size_cache)

periodic_refresher = PeriodicRefresher(
    survey_service, jazzhr_cache, background_checker,
    recent_threshold=RECENT_SURVEY_THRESHOLD,
    interval_seconds=SURVEY_REFRESH_INTERVAL_MIN * 60,
)
periodic_refresher.start()

log("App module loaded - surveys will load on first request", "WARN")

app = Dash(__name__, suppress_callback_exceptions=True, update_title=None)
app.title = "Culture Index - HR Tool"
server = app.server

# Initialize CSRF Protection
csrf = CSRFProtect(server)

# Configure CSRF to exempt Dash endpoints
server.config['WTF_CSRF_CHECK_DEFAULT'] = False  # Disable default CSRF checking
log("CSRF protection configured (Dash endpoints exempted)", "WARN")

# Register background sync blueprint (for automated hourly data refresh)
background_sync_bp = create_background_sync_blueprint(
    survey_service=survey_service,
    jazzhr_cache=jazzhr_cache,
    background_checker=background_checker,
    recent_threshold=RECENT_SURVEY_THRESHOLD
)
server.register_blueprint(background_sync_bp)
csrf.exempt(background_sync_bp)  # Exempt API endpoints from CSRF
log("Background sync API endpoints registered", "WARN")

# Get Redis client for rate limiting (if using Redis cache)
redis_client = None
if hasattr(jazzhr_cache_backend, '_redis') and jazzhr_cache_backend._redis:
    redis_client = jazzhr_cache_backend._redis
    log("Using Redis for distributed rate limiting", "WARN")
else:
    log("Using in-memory rate limiting (single worker only)", "WARN")

auth_manager = AuthManager(server, redis_client=redis_client)

get_cache_manager().set_redis_client(redis_client)
if redis_client:
    log("AppState backed by Redis — signals shared across all workers", "WARN")
else:
    log("AppState using in-memory fallback — single worker only", "WARN")

if not os.path.exists("assets"):
    os.makedirs("assets")

@server.route('/health')
@csrf.exempt
def health_check():
    from flask import jsonify
    
    try:
        redis_healthy = jazzhr_cache.ping()
        surveys_loaded = survey_service.is_loaded()
        
        if redis_healthy and surveys_loaded:
            status = "ok"
            http_status = 200
        elif redis_healthy or surveys_loaded:
            status = "degraded"
            http_status = 200
        else:
            status = "error"
            http_status = 503
        
        return jsonify({
            "status": status,
            "timestamp": datetime.now().isoformat()
        }), http_status
    except Exception:
        return jsonify({
            "status": "error",
            "timestamp": datetime.now().isoformat()
        }), 503

@server.route('/health/detailed')
def health_check_detailed():
    from flask import jsonify
    from auth import require_auth
    
    if not current_user.is_authenticated:
        return jsonify({"error": "Authentication required"}), 401
    
    try:
        redis_healthy = jazzhr_cache.ping()
        redis_status = jazzhr_cache.is_healthy()
        pdf_cache_healthy = pdf_size_cache.ping() if hasattr(pdf_size_cache, 'ping') else True
        surveys_loaded = survey_service.is_loaded()
        survey_count = len(survey_service.get_all_surveys()) if surveys_loaded else 0
        
        if redis_healthy and surveys_loaded:
            status = "healthy"
        elif redis_healthy or surveys_loaded:
            status = "degraded"
        else:
            status = "unhealthy"
        
        return jsonify({
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "components": {
                "redis": {
                    "healthy": redis_healthy,
                    "details": redis_status
                },
                "pdf_cache": {
                    "healthy": pdf_cache_healthy
                },
                "survey_service": {
                    "healthy": surveys_loaded,
                    "survey_count": survey_count
                }
            },
            "config": {
                "items_per_page": ITEMS_PER_PAGE,
                "max_batch_upload": MAX_BATCH_UPLOAD,
                "recent_survey_threshold": RECENT_SURVEY_THRESHOLD,
                "cache_backend": os.getenv('CACHE_BACKEND', 'file')
            }
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@server.route('/health/ready')
@csrf.exempt
def readiness_check():
    from flask import jsonify
    
    if not survey_service.is_loaded():
        return jsonify({"ready": False, "reason": "Surveys not loaded"}), 503
    
    if not jazzhr_cache.ping():
        return jsonify({"ready": False, "reason": "Redis not connected"}), 503
    
    return jsonify({"ready": True})

@server.route('/health/live')
@csrf.exempt
def liveness_check():
    from flask import jsonify
    return jsonify({"alive": True, "timestamp": datetime.now().isoformat()})

@server.route('/api/test-notification', methods=['POST'])
@csrf.exempt
def test_notification():
    """Test endpoint to trigger a fake notification for development/testing."""
    count = request.args.get('count', 3, type=int)
    cache_mgr = get_cache_manager()
    cache_mgr.app_state.set_new_surveys_notification(
        count=count,
        survey_ids=[f"TEST{i:03d}" for i in range(count)]
    )
    log(f"Test notification created with {count} fake surveys", "WARN")
    return jsonify({"status": "notification created", "count": count}), 200

@server.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect('/')
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        success, message = auth_manager.attempt_login(username, password)
        
        if success:
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect('/')
        else:
            return app.index(error_message=message)
    
    return app.index()

@server.route('/logout')
def logout():
    cache_mgr = get_cache_manager()
    cache_mgr.on_logout()
    
    auth_manager.logout()
    return redirect('/login')

@server.before_request
def handle_csrf_for_dash():
    """Exempt all Dash endpoints from CSRF protection."""
    # Dash uses these endpoints for callbacks and updates
    dash_endpoints = ['/_dash-update-component', '/_dash-layout', '/_dash-dependencies', '/_reload-hash']
    if any(request.path.startswith(ep) for ep in dash_endpoints):
        # Skip CSRF validation for Dash internal endpoints
        return None
    return None

@server.before_request
def require_login():
    allowed_routes = [
        '/login', '/logout', 
        '/health', '/health/ready', '/health/live',
        '/api/background-refresh',
        '/api/get-notification',
        '/api/acknowledge-notification',
        '/api/test-notification',
        '/_dash-layout', '/_dash-dependencies', '/_dash-update-component', '/_reload-hash'
    ]
    
    if request.path.startswith('/assets/') or request.path.startswith('/_dash-component-suites/'):
        return None
    
    for route in allowed_routes:
        if request.path.startswith(route):
            return None
    
    if not current_user.is_authenticated:
        return redirect('/login?next=' + request.path)

def serve_layout():
    if not current_user.is_authenticated:
        return create_login_layout()
    
    return html.Div([
        html.Div([
            html.Div([
                html.Div([
                    html.Img(src="/assets/SENERGY-Logo_Icon-Yellow.png", className="header-logo"),
                    html.Div(
                        id="new-surveys-banner",
                        children=[
                            html.Span("New Surveys: ", className="badge-label"),
                            html.Span("0", className="badge-count")
                        ],
                        className="surveys-badge no-new-surveys",
                        n_clicks=0
                    ),
                ], className="header-left"),
                html.Div([
                    html.Button("Upload Selected", id="upload-btn", className="upload-btn", n_clicks=0, disabled=True),
                    html.Button("Refresh JazzHR", id="refresh-jazzhr-btn", className="refresh-btn", n_clicks=0),
                    html.Div(className="header-separator"),
                    html.A("Logout", href="/logout", className="refresh-btn", style={"textDecoration": "none", "display": "flex", "alignItems": "center"}),
                ], className="header-right"),
            ], className="header"),
        
        html.Div([
            html.Div([
                html.Div([
                    html.Div([
                        html.Span("SURVEYS", className="panel-label"),
                        html.Span(id="selection-count", className="selection-count"),
                        
                        html.Div([
                            dcc.Input(
                                id="search-input",
                                type="text",
                                placeholder="Search by name",
                                debounce=False,
                                className="search-input"
                            ),
                            html.Button("Clear", id="clear-search-btn", className="clear-search-btn", n_clicks=0),
                        ], className="search-container"),
                        
                        html.Div([
                            html.Div(id="upload-status", className="upload-status"),
                            dcc.Checklist(
                                id="select-all-checkbox",
                                options=[{"label": " Select All Uploadable", "value": "all"}],
                                value=[],
                                className="select-all-checkbox"
                            ),
                        ], className="select-all-container"),
                    ], className="panel-header"),
                    
                    html.Div(id="loading-indicator", className="loading-indicator", style={"display": "none"}),
                ], className="surveys-header"),
                
                dcc.Loading(
                    id="surveys-loading",
                    type="default",
                    custom_spinner=html.Div([
                        html.Div(html.Div(className="progress-fill"), className="progress-track"),
                        html.Span("Working", className="progress-label"),
                    ], className="progress-wrap"),
                    children=html.Div(id="surveys-container", className="surveys-list"),
                ),
                
                html.Div([
                    html.Div([
                        html.Button("<- Previous", id="prev-page-btn", className="pagination-btn", n_clicks=0),
                        html.Div(id="page-info", className="page-info"),
                        html.Button("Next ->", id="next-page-btn", className="pagination-btn", n_clicks=0),
                    ], className="pagination-center"),
                    html.Div(id="last-updated", className="last-updated"),
                ], className="pagination-controls"),
            ], className="panel surveys-panel full-width"),
        ], className="main-content"),
        
    ], className="app-container"),
    
    dcc.Store(id="current-page", data=1),
    dcc.Store(id="search-query", data=""),
    dcc.Store(id="current-surveys-data", data=[]),
    dcc.Store(id="uploadable-ids", data=[]),
    dcc.Store(id="upload-queue", data=[]),
    dcc.Store(id="upload-results", data={}),
    dcc.Store(id="card-action-state", data={}),
    dcc.Store(id="refresh-trigger", data=0),
    dcc.Store(id="background-check-signal", data=0),
    dcc.Store(id="notification-data", data={"count": 0}),
    
    dcc.Interval(id="upload-interval", interval=UPLOAD_INTERVAL_MS, n_intervals=0, disabled=True),
    dcc.Interval(id="background-signal-interval", interval=3000, n_intervals=0),
    # Smart polling: Dash Interval auto-pauses when tab is inactive (browser optimization)
    dcc.Interval(id="notification-check-interval", interval=30000, n_intervals=0),
    ])

app.layout = serve_layout

def format_time_ago(timestamp_str: str) -> str:
    """Convert timestamp to user-friendly relative time (e.g., '2 minutes ago')."""
    try:
        if isinstance(timestamp_str, str):
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        else:
            timestamp = timestamp_str
        
        now = datetime.now(timestamp.tzinfo) if timestamp.tzinfo else datetime.now()
        diff = now - timestamp
        
        seconds = diff.total_seconds()
        
        if seconds < 60:
            return "Just now"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = int(seconds / 86400)
            return f"{days} day{'s' if days != 1 else ''} ago"
    except:
        return "Unknown"


def build_survey_display_local(surveys: List[Dict], jazzhr_results: Dict, pdf_sizes: Dict) -> Tuple[List, List, List]:
    survey_items = []
    uploadable_ids = []
    surveys_data = []
    
    for s in surveys:
        survey_id = str(s.get('surveyId', ''))
        url = s.get('surveyReportUrl')
        pdf_size = pdf_sizes.get(survey_id)
        jazzhr = jazzhr_results.get(survey_id, {})
        
        status = jazzhr.get('status')
        is_uploaded = jazzhr.get('isUploaded', False)
        applicant_id = jazzhr.get('applicantId')
        
        is_uploadable = (status == "NOT_UPLOADED" and applicant_id and url)
        
        if is_uploadable:
            uploadable_ids.append(survey_id)
        
        surveys_data.append({
            "surveyId": survey_id,
            "firstName": s.get('firstName', ''),
            "lastName": s.get('lastName', ''),
            "applicantId": applicant_id,
            "pdf_url": url
        })
        
        status_indicator = _status_indicator_component(jazzhr, survey_id)
        
        pdf_size_mb = pdf_size / (1024 * 1024) if pdf_size else None
        full_name = f"{s.get('firstName', '')} {s.get('lastName', '')}".strip() or "Unknown"
        position = s.get('position', '').strip()
        trait_pattern = s.get('traitPattern', 'N/A')
        
        card_children = [
            html.Div([
                html.Div([
                    html.Span(full_name, className="survey-name"),
                    html.Span(" | ", className="survey-separator") if position else None,
                    html.Span(position, className="survey-position") if position else None,
                    html.Span(trait_pattern, className="trait-badge"),
                    html.Span(s.get("surveyDate", "N/A"), className="survey-date"),
                ], className="survey-name-row"),
                status_indicator,
            ], className="survey-item-header"),
            
            html.Div([
                html.Div([html.Span("EMAIL", className="info-label"), html.Span(s.get("email", "N/A"), className="info-value")], className="info-field"),
                html.Div([html.Span("PHONE", className="info-label"), html.Span(s.get("phoneNumber", "N/A"), className="info-value")], className="info-field"),
                html.Div([html.Span("SURVEY ID", className="info-label"), html.Span(survey_id, className="info-value")], className="info-field"),
                html.Div([
                    html.Span("REPORT", className="info-label"),
                    html.A(f"View PDF ({pdf_size_mb:.2f} MB)" if pdf_size_mb else "View PDF", href=url or "#", target="_blank", className="report-link") if url else html.Span("N/A", className="info-value"),
                ], className="info-field"),
            ], className="survey-info"),
            
            html.Div([
                html.Span(
                    format_time_ago(jazzhr.get('timestamp')) if jazzhr.get('timestamp') else "Never checked",
                    className="last-checked-value"
                ),
                html.Div(
                    id={"type": "card-action-status", "index": survey_id},
                    className="card-action-status"
                ),
                html.Button(
                    "Refresh Status",
                    id={"type": "refresh-single-btn", "index": survey_id},
                    n_clicks=0,
                    className="refresh-single-btn",
                    title="Check JazzHR status for this profile"
                ),
            ], className="survey-footer"),
        ]
        
        if is_uploadable:
            card_children.append(
                html.Div([
                    dcc.Checklist(id={"type": "survey-checkbox", "index": survey_id}, options=[{"label": "", "value": survey_id}], value=[], className="survey-checkbox-overlay")
                ], className="survey-checkbox-overlay-container")
            )
        
        show_upload_btn = (applicant_id and url and status != "NOT_IN_JAZZHR")
        if show_upload_btn:
            card_children.append(
                html.Div([
                    html.Button("Upload", id={"type": "upload-single-btn", "index": survey_id}, n_clicks=0, className="upload-single-btn")
                ], id={"type": "survey-upload-container", "index": survey_id}, className="survey-upload-container")
            )
        
        survey_items.append(html.Div(card_children, className="survey-item"))
    
    return survey_items, uploadable_ids, surveys_data

_active_uploads = set()
_active_uploads_lock = Lock()
_completed_uploads = {}

def _status_indicator_parts(jazzhr_result: Dict) -> Tuple[List, str]:
    status = (jazzhr_result or {}).get('status')
    is_uploaded = (jazzhr_result or {}).get('isUploaded', False)

    if status is None:
        return [html.Span("Checking", className="status-text")], "status-pending"
    if is_uploaded or status == "UPLOADED":
        return [html.Span("Uploaded", className="status-text")], "status-uploaded"
    if status == "NOT_UPLOADED":
        return [html.Span("Not Uploaded", className="status-text")], "status-missing"
    if status == "NOT_IN_JAZZHR":
        return [html.Span("Not in Jazz", className="status-text")], "status-not-found"
    if status == "MISSING_NAME":
        return [html.Span("No Name", className="status-text")], "status-pending"
    if status == "NO_PDF_URL":
        return [html.Span("No URL", className="status-text")], "status-pending"
    if status == "ERROR":
        return [html.Span("Error", className="status-text")], "status-error"
    return [html.Span("Unknown", className="status-text")], "status-pending"

def _status_indicator_component(jazzhr_result: Dict, survey_id: str):
    children, class_name = _status_indicator_parts(jazzhr_result)
    return html.Div(
        children,
        id={"type": "survey-status-indicator", "index": survey_id},
        className=class_name
    )

def _card_action_state(current_state, survey_id, kind, message, detail=None):
    state = dict(current_state or {})
    entry = {
        "kind": kind,
        "message": message,
        "timestamp": datetime.now().isoformat()
    }
    if detail:
        entry["detail"] = str(detail)[:180]
    state[str(survey_id)] = entry
    return state

def _card_action_states(current_state, entries):
    state = dict(current_state or {})
    now = datetime.now().isoformat()
    for survey_id, kind, message, detail in entries:
        entry = {
            "kind": kind,
            "message": message,
            "timestamp": now
        }
        if detail:
            entry["detail"] = str(detail)[:180]
        state[str(survey_id)] = entry
    return state

def _upload_result_card_entry(survey_id, result):
    if result.get("success"):
        return (survey_id, "success", "Uploaded", None)
    error = result.get("error") or "Failed"
    return (survey_id, "error", "Failed", error)

def _clear_stale_upload_states(current_state, active_ids, result_ids):
    state = dict(current_state or {})
    active_ids = {str(sid) for sid in active_ids}
    result_ids = {str(sid) for sid in result_ids}

    for survey_id, entry in list(state.items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") in {"queued", "uploading"} and survey_id not in active_ids and survey_id not in result_ids:
            del state[survey_id]

    return state

@callback(
    [Output("surveys-container", "children"),
     Output("page-info", "children"),
     Output("prev-page-btn", "disabled"),
     Output("next-page-btn", "disabled"),
     Output("last-updated", "children"),
     Output("current-surveys-data", "data"),
     Output("uploadable-ids", "data"),
     Output("loading-indicator", "style"),
     Output("upload-status", "children")],
    [Input("current-page", "data"),
     Input("search-query", "data"),
     Input("refresh-trigger", "data"),
     Input("background-check-signal", "data")],
    prevent_initial_call=False
)
def display_surveys(page, search_query, refresh_trigger, background_signal):
    cache_mgr = get_cache_manager()
    
    # Validate and sanitize inputs
    is_valid, page, error_msg = validate_page_number(page)
    if not is_valid:
        log(f"Invalid page number: {error_msg}", "WARN")
        page = 1
    
    if search_query:
        search_query = sanitize_search_query(search_query, max_length=100)
    
    try:
        triggered_id = ctx.triggered_id
    except (AttributeError, RuntimeError):
        triggered_id = None
    
    if triggered_id == "background-check-signal":
        return (dash.no_update,) * 9
    
    is_refresh_trigger = (triggered_id == "refresh-trigger")
    
    # Check cache (session-based, respects JazzHR check completion)
    if survey_service.is_loaded() and not survey_service.is_loading():
        cached = cache_mgr.get_cached_result(search_query, page, is_refresh_trigger)
        if cached:
            return cached
    
    # Try to acquire callback lock (non-blocking)
    if not cache_mgr.callback_lock.try_acquire("display_surveys"):
        return build_loading_result("Loading surveys")
    
    try:
        start_time = time.time()
        
        if page < 1:
            page = 1
        
        if not survey_service.is_loaded():
            if survey_service.is_loading():
                return build_loading_result("Refreshing surveys from Culture Index")
            
            log(f"First request - loading surveys (trigger={triggered_id})", "WARN")
            try:
                survey_service.load_surveys(force_refresh=False)
                all_surveys = survey_service.get_all_surveys()
                if all_surveys:
                    recent_ids = [str(s['surveyId']) for s in all_surveys[:RECENT_SURVEY_THRESHOLD]]
                    jazzhr_cache.set_recent_surveys(recent_ids)
                    log(f"Worker initialized with {len(all_surveys)} surveys", "WARN")
                    background_checker.start_checking()
            except Exception as e:
                user_error = get_user_friendly_error(e, "loading surveys")
                return build_error_result(user_error)
        
        if not survey_service.is_loaded():
            if survey_service.is_loading():
                return build_loading_result("Refreshing surveys from Culture Index")
            return build_error_result("Failed to load survey data")
        
        if search_query and len(search_query) >= 2:
            all_results = survey_service.search_surveys(search_query, limit=100)
            total_count = len(all_results)
            start_idx = (page - 1) * ITEMS_PER_PAGE
            surveys = all_results[start_idx:start_idx + ITEMS_PER_PAGE]
        else:
            surveys, total_count = survey_service.get_page(page)
        
        if not surveys:
            result = build_empty_result(search_query)
            cache_mgr.cache_result(search_query, page, result)
            return result
        
        urls = {str(s['surveyId']): s.get('surveyReportUrl') for s in surveys}
        
        if is_refresh_trigger:
            log(f"Refresh trigger: force-checking {len(surveys)} surveys", "WARN")
            urls_to_check = {sid: url for sid, url in urls.items() if url}
            pdf_sizes = fetch_pdf_sizes(urls_to_check, pdf_size_cache)
            jazzhr_results = jazzhr_service.check_surveys_batch(surveys, urls, pdf_sizes)
        else:
            pdf_sizes = {}
            survey_ids = [str(s['surveyId']) for s in surveys]
            cached_batch = jazzhr_cache.get_batch(survey_ids)
            
            jazzhr_results = {}
            uncached_surveys = []
            
            for survey in surveys:
                survey_id = str(survey['surveyId'])
                cached = cached_batch.get(survey_id)
                if cached:
                    jazzhr_results[survey_id] = cached
                else:
                    jazzhr_results[survey_id] = {"status": None, "isUploaded": False}
                    uncached_surveys.append(survey)
            
            if uncached_surveys:
                def check_uncached():
                    try:
                        urls_uncached = {str(s['surveyId']): s.get('surveyReportUrl') for s in uncached_surveys}
                        urls_to_check = {sid: url for sid, url in urls_uncached.items() if url}
                        pdf_sizes_uncached = fetch_pdf_sizes(urls_to_check, pdf_size_cache)
                        jazzhr_service.check_surveys_batch(uncached_surveys, urls_uncached, pdf_sizes_uncached)
                        cache_mgr.on_jazzhr_status_update()
                    except Exception as e:
                        log(f"Error checking uncached surveys: {e}", "ERROR")
                
                Thread(target=check_uncached, daemon=True).start()
        
        try:
            survey_items, uploadable_ids, surveys_data = build_survey_display_local(surveys, jazzhr_results, pdf_sizes)
        except Exception as e:
            user_error = get_user_friendly_error(e, "loading surveys")
            survey_items = [html.Div(user_error, className="empty-message")]
            uploadable_ids = []
            surveys_data = []
        
        if not survey_items:
            survey_items = [html.Div("No surveys to display", className="empty-message")]
        
        total_pages = max(1, (total_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        if search_query:
            page_info = f"Page {page} of {total_pages} ({total_count} results for '{search_query}')"
        else:
            page_info = f"Page {page} of {total_pages} ({total_count:,} surveys)"
        
        elapsed = time.time() - start_time
        elapsed_ms = elapsed * 1000
        updated_text = f"Updated: {datetime.now().strftime('%I:%M:%S %p')} ({elapsed_ms:.0f}ms)"
        
        current_survey_ids = [str(s.get('surveyId')) for s in surveys]
        cache_mgr.track_displayed_surveys(current_survey_ids)
        
        upload_status_msg = "" if triggered_id in ["refresh-trigger", "background-check-signal"] else dash.no_update
        
        result = (
            survey_items, page_info,
            page <= 1, page >= total_pages,
            updated_text, surveys_data, uploadable_ids,
            {"display": "none"},
            upload_status_msg
        )
        
        cache_mgr.cache_result(search_query, page, result)
        
        if elapsed_ms > 500:
            log(f"Display callback took {elapsed_ms:.0f}ms", "WARN")
        
        return result
        
    except Exception as e:
        user_error = get_user_friendly_error(e, "loading surveys")
        return build_error_result(user_error)
    finally:
        cache_mgr.callback_lock.release()

@callback(
    [Output("login-url", "pathname"),
     Output("login-error-message", "children"),
     Output("login-error-message", "style")],
    [Input("login-submit-btn", "n_clicks"),
     Input("login-username-input", "n_submit"),
     Input("login-password-input", "n_submit")],
    [State("login-username-input", "value"),
     State("login-password-input", "value")],
    prevent_initial_call=True
)
def handle_login_callback(n_clicks, username_submit, password_submit, username, password):
    if not current_user.is_authenticated:
        if username and password:
            success, message = auth_manager.attempt_login(username, password)
            if success:
                return "/", "", {"display": "none"}
            else:
                return dash.no_update, message, {"display": "block"}
        else:
            return dash.no_update, "Please enter both username and password", {"display": "block"}
    return "/", "", {"display": "none"}

@callback(
    Output("current-page", "data"),
    [Input("prev-page-btn", "n_clicks"),
     Input("next-page-btn", "n_clicks")],
    [State("current-page", "data")],
    prevent_initial_call=True
)
def handle_pagination(prev_clicks, next_clicks, current_page):
    triggered_id = ctx.triggered_id
    
    if triggered_id == "prev-page-btn":
        return max(1, current_page - 1)
    elif triggered_id == "next-page-btn":
        return current_page + 1
    
    return dash.no_update

@callback(
    [Output("search-query", "data"),
     Output("current-page", "data", allow_duplicate=True),
     Output("search-input", "value")],
    [Input("search-input", "value"),
     Input("search-input", "n_submit"),
     Input("clear-search-btn", "n_clicks")],
    prevent_initial_call=True
)
def handle_search(search_value, n_submit, clear_clicks):
    triggered_id = ctx.triggered_id
    
    if triggered_id == "clear-search-btn":
        return "", 1, ""
    elif triggered_id in ["search-input", "search-input.n_submit"]:
        sanitized_value = sanitize_search_query(search_value or "", max_length=100)
        return sanitized_value, 1, dash.no_update
    
    return dash.no_update, dash.no_update, dash.no_update

@callback(
    [Output("refresh-trigger", "data", allow_duplicate=True),
     Output("upload-status", "children", allow_duplicate=True)],
    Input("refresh-jazzhr-btn", "n_clicks"),
    [State("current-page", "data"),
     State("search-query", "data"),
     State("refresh-trigger", "data")],
    prevent_initial_call=True
)
def handle_refresh_jazzhr(n_clicks, current_page, search_query, current_trigger):
    if not n_clicks:
        return dash.no_update, dash.no_update
    
    log("Refresh JazzHR button clicked - clearing cache for 30 surveys (2 pages)", "WARN")
    
    # Get 30 surveys starting from current page
    if current_page is None:
        current_page = 1
    
    surveys_to_clear = []
    if search_query and len(search_query) >= 2:
        all_results = survey_service.search_surveys(search_query, limit=100)
        start_idx = (current_page - 1) * ITEMS_PER_PAGE
        surveys_to_clear = all_results[start_idx:start_idx + 30]
    else:
        all_surveys = survey_service.get_all_surveys()
        start_idx = (current_page - 1) * ITEMS_PER_PAGE
        surveys_to_clear = all_surveys[start_idx:start_idx + 30]
    
    cleared = 0
    for s in surveys_to_clear:
        survey_id = str(s.get("surveyId", ""))
        if jazzhr_cache.get(survey_id):
            jazzhr_cache.delete(survey_id)
            cleared += 1
    
    log(f"Cleared JazzHR cache for {cleared} surveys (out of {len(surveys_to_clear)} total)", "WARN")
    
    if cleared > 0:
        jazzhr_cache.save()
        log("JazzHR cache saved to storage", "WARN")
    
    log("Triggering UI refresh to re-check surveys", "WARN")
    return current_trigger + 1, "Refreshing JazzHR status"

_last_individual_refresh = {}
_individual_refresh_lock = RLock()

@callback(
    [Output("refresh-trigger", "data", allow_duplicate=True),
     Output("upload-status", "children", allow_duplicate=True)],
    Input({"type": "refresh-single-btn", "index": ALL}, "n_clicks"),
    [State({"type": "refresh-single-btn", "index": ALL}, "id"),
     State("refresh-trigger", "data"),
     State("current-surveys-data", "data")],
    prevent_initial_call=True
)
def handle_refresh_single(n_clicks_list, button_ids, current_trigger, surveys_data):
    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update, dash.no_update
    
    triggered_prop = ctx.triggered[0]['prop_id']
    triggered_value = ctx.triggered[0].get('value')
    
    if '.n_clicks' not in triggered_prop:
        return dash.no_update, dash.no_update
    
    if triggered_value is None or triggered_value == 0:
        return dash.no_update, dash.no_update
    
    import json
    try:
        triggered_id_str = triggered_prop.split('.')[0]
        button_id = json.loads(triggered_id_str)
        survey_id = button_id.get('index')
        
        if survey_id:
            import time
            current_time = time.time()
            with _individual_refresh_lock:
                last_refresh_time = _last_individual_refresh.get(survey_id, 0)
                if current_time - last_refresh_time < 2.0:
                    log(f"Individual refresh for survey {survey_id} ignored (debounced, last refresh {current_time - last_refresh_time:.1f}s ago)", "WARN")
                    return dash.no_update, dash.no_update
                _last_individual_refresh[survey_id] = current_time
            
            log(f"Individual refresh for survey {survey_id} - checking this survey only", "WARN")
            
            if jazzhr_cache.get(survey_id):
                jazzhr_cache.delete(survey_id)
                jazzhr_cache.save()
            
            survey = None
            if surveys_data:
                for s in surveys_data:
                    if str(s.get('surveyId')) == str(survey_id):
                        survey = s
                        break
            
            if survey:
                first_name = survey.get('firstName', '')
                last_name = survey.get('lastName', '')
                pdf_url = survey.get('pdf_url')
                
                def check_single():
                    try:
                        if pdf_url:
                            pdf_sizes = fetch_pdf_sizes({survey_id: pdf_url}, pdf_size_cache)
                        else:
                            pdf_sizes = {}
                        
                        jazzhr_service.check_surveys_batch([survey], {survey_id: pdf_url}, pdf_sizes, force_diag=True)
                        log(f"Completed individual check for survey {survey_id}", "WARN")
                        
                        cache_mgr = get_cache_manager()
                        cache_mgr.on_jazzhr_status_update()
                    except Exception as e:
                        log(f"Error checking survey {survey_id}: {e}", "ERROR")
                        jazzhr_cache.set(survey_id, {
                            "status": "ERROR",
                            "isUploaded": False,
                            "error": str(e),
                            "timestamp": datetime.now().isoformat()
                        })
                        cache_mgr = get_cache_manager()
                        cache_mgr.on_jazzhr_status_update()
                
                Thread(target=check_single, daemon=True).start()
                
                return dash.no_update, dash.no_update
            else:
                log(f"Survey {survey_id} not found in current page data", "WARN")
                return dash.no_update, dash.no_update
    except Exception as e:
        log(f"Error in individual refresh: {e}", "ERROR")
    
    return dash.no_update, dash.no_update

@callback(
    Output("card-action-state", "data", allow_duplicate=True),
    Input({"type": "refresh-single-btn", "index": ALL}, "n_clicks"),
    [State({"type": "refresh-single-btn", "index": ALL}, "id"),
     State("current-surveys-data", "data"),
     State("card-action-state", "data")],
    prevent_initial_call=True
)
def show_refresh_single_card_status(n_clicks_list, button_ids, surveys_data, card_action_state):
    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update
    
    triggered_prop = ctx.triggered[0]["prop_id"]
    triggered_value = ctx.triggered[0].get("value")
    
    if ".n_clicks" not in triggered_prop or not triggered_value:
        return dash.no_update
    
    try:
        triggered_id_str = triggered_prop.split(".")[0]
        button_id = json.loads(triggered_id_str)
        survey_id = str(button_id.get("index"))
    except Exception:
        return dash.no_update
    
    survey = next((s for s in (surveys_data or []) if str(s.get("surveyId")) == survey_id), None)
    if not survey:
        return _card_action_state(
            card_action_state,
            survey_id,
            "error",
            "Not Found",
            f"Unable to check survey {survey_id}"
        )
    
    full_name = f"{survey.get('firstName', '')} {survey.get('lastName', '')}".strip() or f"survey {survey_id}"
    return _card_action_state(
        card_action_state,
        survey_id,
        "refreshing",
        "Checking",
        full_name
    )

def _jazzhr_status_label(result):
    status = (result or {}).get("status")
    if (result or {}).get("isUploaded") or status == "UPLOADED":
        return "Uploaded"
    if status == "NOT_UPLOADED":
        return "Not Uploaded"
    if status == "NOT_IN_JAZZHR":
        return "Not In JazzHR"
    if status == "MISSING_NAME":
        return "Missing Name"
    if status == "NO_PDF_URL":
        return "No PDF URL"
    if status == "ERROR":
        return "Error"
    return "Status updated"

@callback(
    [Output({"type": "card-action-status", "index": ALL}, "children"),
     Output({"type": "card-action-status", "index": ALL}, "className")],
    Input("card-action-state", "data"),
    State({"type": "card-action-status", "index": ALL}, "id"),
    prevent_initial_call=False
)
def render_card_action_status(card_action_state, status_ids):
    if not status_ids:
        return [], []
    
    state = card_action_state or {}
    children = []
    classes = []
    
    for status_id in status_ids:
        survey_id = str(status_id.get("index"))
        entry = state.get(survey_id)
        
        if not entry:
            children.append("")
            classes.append("card-action-status")
            continue
        
        kind = entry.get("kind", "info")
        message = entry.get("message", "")
        detail = entry.get("detail")
        class_name = f"card-action-status card-action-status--{kind}"
        
        content = []
        if kind in ("refreshing", "uploading", "queued"):
            content.append(html.Span(className="card-action-spinner"))
        content.append(html.Span(message, className="card-action-message"))
        if detail:
            content.append(html.Span(detail, className="card-action-detail"))
        
        children.append(content)
        classes.append(class_name)
    
    return children, classes

@callback(
    Output("card-action-state", "data", allow_duplicate=True),
    Input("background-check-signal", "data"),
    State("card-action-state", "data"),
    prevent_initial_call=True
)
def finalize_refreshing_card_actions(background_signal, card_action_state):
    state = card_action_state or {}
    if not state:
        return dash.no_update
    
    updated_state = dict(state)
    changed = False
    
    for survey_id, entry in state.items():
        if not isinstance(entry, dict) or entry.get("kind") != "refreshing":
            continue
        
        result = jazzhr_cache.get(str(survey_id))
        if not result:
            continue
        
        if result.get("status") == "ERROR":
            updated_state[str(survey_id)] = {
                "kind": "error",
                "message": "Failed",
                "detail": result.get("error", "Unable to check JazzHR"),
                "timestamp": datetime.now().isoformat()
            }
        else:
            updated_state[str(survey_id)] = {
                "kind": "success",
                "message": "Refreshed",
                "detail": _jazzhr_status_label(result),
                "timestamp": datetime.now().isoformat()
            }
        changed = True
    
    return updated_state if changed else dash.no_update

@callback(
    Output("background-check-signal", "data"),
    Input("background-signal-interval", "n_intervals"),
    State("background-check-signal", "data"),
    prevent_initial_call=True
)
def monitor_background_check(n_intervals, current_signal):
    try:
        # Ensure we always return a valid integer
        if current_signal is None or not isinstance(current_signal, (int, float)):
            current_signal = 0
        
        cache_mgr = get_cache_manager()
        
        if cache_mgr.check_ui_signal():
            return int(current_signal) + 1
        
        return dash.no_update
    except Exception as e:
        log(f"Error in monitor_background_check: {e}", "ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", "ERROR")
        return 0  # Return valid integer instead of dash.no_update on error


@callback(
    Output("notification-data", "data"),
    Input("notification-check-interval", "n_intervals"),
    prevent_initial_call=False
)
def check_for_new_surveys(n_intervals):
    try:
        cache_mgr = get_cache_manager()
        
        try:
            if hasattr(jazzhr_cache.cache, '_redis') and jazzhr_cache.cache._redis:
                import json
                redis_client = jazzhr_cache.cache._redis
                data = redis_client.get("new_surveys_notification")
                
                if data:
                    notification = json.loads(data)
                    if not notification.get("acknowledged", False):
                        return notification
        except Exception as e:
            log(f"Error checking Redis notification: {e}", "ERROR")
        
        notification = cache_mgr.get_notification()
        if notification is None:
            return {"count": 0}
        return notification
    except Exception as e:
        log(f"Fatal error in check_for_new_surveys: {e}", "ERROR")
        return {"count": 0}


@callback(
    [Output("new-surveys-banner", "children"),
     Output("new-surveys-banner", "className")],
    Input("notification-data", "data"),
    prevent_initial_call=False
)
def show_notification_banner(notification):
    try:
        # Validate input
        if not notification or not isinstance(notification, dict):
            notification = {"count": 0}
        
        # Ensure count is a valid number
        count = notification.get("count", 0)
        if not isinstance(count, (int, float)):
            count = 0
        count = int(count)
        
        if count > 0:
            timestamp_str = notification.get("timestamp", "")
            try:
                if timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str)
                    # Treat naive timestamps as UTC, then show in Central Time.
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    time_str = dt.astimezone(CENTRAL_TZ).strftime("%I:%M %p")
                else:
                    time_str = "recently"
            except Exception:
                time_str = "recently"
            
            plural = "s" if count != 1 else ""
            content = [
                html.Span("New Surveys: ", className="badge-label"),
                html.Span(str(count), className="badge-count pulse"),
                html.Span(f" ({time_str})", className="badge-time")
            ]
            return content, "surveys-badge has-new-surveys"
        else:
            content = [
                html.Span("New Surveys: ", className="badge-label"),
                html.Span("0", className="badge-count")
            ]
            return content, "surveys-badge no-new-surveys"
    except Exception as e:
        log(f"Error in show_notification_banner: {e}", "ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", "ERROR")
        # Return safe default
        content = [
            html.Span("New Surveys: ", className="badge-label"),
            html.Span("0", className="badge-count")
        ]
        return content, "surveys-badge no-new-surveys"


@callback(
    Output("refresh-trigger", "data", allow_duplicate=True),
    Input("new-surveys-banner", "n_clicks"),
    State("refresh-trigger", "data"),
    State("notification-data", "data"),
    prevent_initial_call=True
)
def handle_banner_click(n_clicks, current_trigger, notification):
    if not n_clicks or n_clicks == 0:
        return dash.no_update
    
    # Only refresh if there are actually new surveys
    count = notification.get("count", 0) if notification else 0
    if count == 0:
        return dash.no_update
    
    cache_mgr = get_cache_manager()
    
    log("User clicked notification banner - starting reload process", "WARN")
    
    # Clear the notification FIRST (before reload starts)
    cache_mgr.acknowledge_notification()
    
    # Delete notification from Redis completely (safer than update - avoids race conditions)
    redis_deleted = False
    try:
        if hasattr(jazzhr_cache.cache, '_redis') and jazzhr_cache.cache._redis:
            redis_client = jazzhr_cache.cache._redis
            
            # Try deleting up to 3 times to ensure it succeeds
            for attempt in range(3):
                result = redis_client.delete("new_surveys_notification")
                if result > 0:
                    redis_deleted = True
                    log(f"Redis notification deleted successfully on attempt {attempt + 1}", "WARN")
                    break
                elif attempt < 2:
                    log(f"Redis delete attempt {attempt + 1} failed, retrying...", "WARN")
                    time.sleep(0.1)
            
            if not redis_deleted:
                log("Redis notification delete failed after 3 attempts - may reappear on reload", "ERROR")
    except Exception as e:
        log(f"Error deleting Redis notification: {e}", "ERROR")
    
    # Clear surveys and stop background checker
    survey_service.clear_cache()
    background_checker.stop_checking()
    
    cache_mgr.on_data_refresh_start()
    log("Survey cache cleared after banner click", "WARN")
    
    def _reload():
        try:
            log("Background reload thread started from banner click", "WARN")
            survey_service.load_surveys(force_refresh=True)
            all_surveys = survey_service.get_all_surveys()
            log(f"Reload complete, got {len(all_surveys)} surveys", "WARN")
            
            if all_surveys:
                recent_ids = [str(s['surveyId']) for s in all_surveys[:RECENT_SURVEY_THRESHOLD]]
                jazzhr_cache.set_recent_surveys(recent_ids)
                log(f"Updated recent surveys cache with {len(recent_ids)} IDs", "WARN")
            
            cache_mgr.on_data_refresh_complete()
            log("Cache manager notified of refresh completion", "WARN")
            
            log("Starting background JazzHR checker", "WARN")
            background_checker.start_checking()
            log("Banner click refresh process fully completed", "WARN")
        except Exception as e:
            log(f"Banner click refresh error: {e}", "ERROR")
            import traceback
            traceback.print_exc()
    
    Thread(target=_reload, daemon=True, name="BannerClickRefreshThread").start()
    log("Reload thread spawned from banner click, returning to UI", "WARN")
    
    return current_trigger + 1


@callback(
    Output({"type": "survey-checkbox", "index": ALL}, "value"),
    Input("select-all-checkbox", "value"),
    [State("uploadable-ids", "data"),
     State({"type": "survey-checkbox", "index": ALL}, "id")],
    prevent_initial_call=True
)
def handle_select_all(select_all_value, uploadable_ids, checkbox_ids):
    if not checkbox_ids:
        return []
    
    if "all" in select_all_value:
        return [[id_dict["index"]] if id_dict["index"] in uploadable_ids else [] for id_dict in checkbox_ids]
    else:
        return [[] for _ in checkbox_ids]

@callback(
    [Output("upload-btn", "disabled"),
     Output("selection-count", "children")],
    Input({"type": "survey-checkbox", "index": ALL}, "value"),
    prevent_initial_call=True
)
def update_selection_count(checkbox_values):
    selected = [v[0] for v in checkbox_values if v]
    count = len(selected)
    
    if count == 0:
        return True, ""
    elif count > MAX_BATCH_UPLOAD:
        return True, f"({count} selected - max {MAX_BATCH_UPLOAD})"
    else:
        return False, f"({count} selected)"

@callback(
    [Output("upload-queue", "data", allow_duplicate=True),
     Output("upload-results", "data", allow_duplicate=True),
     Output("upload-interval", "disabled", allow_duplicate=True),
     Output("upload-status", "children", allow_duplicate=True)],
    Input("upload-btn", "n_clicks"),
    [State({"type": "survey-checkbox", "index": ALL}, "value"),
     State("current-surveys-data", "data"),
     State("upload-queue", "data")],
    prevent_initial_call=True
)
def handle_upload_selected(n_clicks, checkbox_values, surveys_data, existing_queue):
    global _active_uploads, _completed_uploads
    
    if not n_clicks or existing_queue:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    selected_ids = [v[0] for v in checkbox_values if v]
    
    if not selected_ids:
        return [], {}, True, ""
    
    if len(selected_ids) > MAX_BATCH_UPLOAD:
        return [], {}, True, f"Max {MAX_BATCH_UPLOAD} at a time"
    
    with _active_uploads_lock:
        conflicting = [sid for sid in selected_ids if str(sid) in _active_uploads or str(sid) in _completed_uploads]
        if conflicting:
            return dash.no_update, dash.no_update, dash.no_update, "Some surveys already uploading"
    
    queue = []
    for survey_id in selected_ids:
        survey_data = next((s for s in surveys_data if s["surveyId"] == survey_id), None)
        if survey_data and survey_data.get("applicantId") and survey_data.get("pdf_url"):
            queue.append({
                "survey_id": str(survey_id),
                "firstName": survey_data["firstName"],
                "lastName": survey_data["lastName"],
                "applicant_id": survey_data["applicantId"],
                "pdf_url": survey_data["pdf_url"]
            })
    
    if not queue:
        return [], {}, True, "No uploadable surveys"
    
    return queue, {}, False, f"Uploading 0/{len(queue)}"

@callback(
    Output("card-action-state", "data", allow_duplicate=True),
    Input("upload-btn", "n_clicks"),
    [State({"type": "survey-checkbox", "index": ALL}, "value"),
     State("current-surveys-data", "data"),
     State("upload-queue", "data"),
     State("card-action-state", "data")],
    prevent_initial_call=True
)
def show_upload_selected_card_status(n_clicks, checkbox_values, surveys_data, existing_queue, card_action_state):
    if not n_clicks or existing_queue:
        return dash.no_update
    
    selected_ids = [v[0] for v in (checkbox_values or []) if v]
    if not selected_ids or len(selected_ids) > MAX_BATCH_UPLOAD:
        return dash.no_update
    
    card_entries = []
    for survey_id in selected_ids:
        survey_data = next((s for s in (surveys_data or []) if s["surveyId"] == survey_id), None)
        if survey_data and survey_data.get("applicantId") and survey_data.get("pdf_url"):
            full_name = f"{survey_data.get('firstName', '')} {survey_data.get('lastName', '')}".strip()
            card_entries.append((str(survey_id), "queued", "Queued", full_name))
    
    if not card_entries:
        return dash.no_update
    
    return _card_action_states(card_action_state, card_entries)

@callback(
    [Output("upload-queue", "data", allow_duplicate=True),
     Output("upload-results", "data", allow_duplicate=True),
     Output("upload-interval", "disabled", allow_duplicate=True),
     Output("upload-status", "children", allow_duplicate=True)],
    Input({"type": "upload-single-btn", "index": ALL}, "n_clicks"),
    [State("current-surveys-data", "data"),
     State("upload-queue", "data")],
    prevent_initial_call=True
)
def handle_single_upload(n_clicks_list, surveys_data, existing_queue):
    global _active_uploads, _completed_uploads
    
    triggered = ctx.triggered
    if not triggered or not triggered[0]:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    trigger_prop_id = triggered[0].get("prop_id", "")
    if ".n_clicks" not in trigger_prop_id:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    if existing_queue:
        return dash.no_update, dash.no_update, dash.no_update, "Upload in progress"
    
    try:
        prop_id_without_suffix = trigger_prop_id.replace(".n_clicks", "")
        triggered_id_dict = json.loads(prop_id_without_suffix)
        survey_id = str(triggered_id_dict.get("index"))
    except:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    if not survey_id:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    has_click = any(c and c > 0 for c in (n_clicks_list or []))
    if not has_click:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    with _active_uploads_lock:
        if survey_id in _active_uploads or survey_id in _completed_uploads:
            log(f"Blocked duplicate upload for {survey_id}", "WARN")
            return dash.no_update, dash.no_update, dash.no_update, "Upload already in progress"
    
    log(f"Queueing upload for {survey_id}", "WARN")
    
    survey_data = next((s for s in surveys_data if str(s.get("surveyId")) == survey_id), None)
    if not survey_data or not survey_data.get("applicantId") or not survey_data.get("pdf_url"):
        return dash.no_update, dash.no_update, dash.no_update, "Survey data not found"
    
    queue = [{
        "survey_id": survey_id,
        "firstName": survey_data["firstName"],
        "lastName": survey_data["lastName"],
        "applicant_id": survey_data["applicantId"],
        "pdf_url": survey_data["pdf_url"]
    }]
    
    name = f"{survey_data.get('firstName', '')} {survey_data.get('lastName', '')}".strip()
    return queue, {}, False, f"Uploading {name}"

@callback(
    Output("card-action-state", "data", allow_duplicate=True),
    Input({"type": "upload-single-btn", "index": ALL}, "n_clicks"),
    [State("current-surveys-data", "data"),
     State("upload-queue", "data"),
     State("card-action-state", "data")],
    prevent_initial_call=True
)
def show_single_upload_card_status(n_clicks_list, surveys_data, existing_queue, card_action_state):
    triggered = ctx.triggered
    if not triggered or not triggered[0]:
        return dash.no_update
    
    trigger_prop_id = triggered[0].get("prop_id", "")
    if ".n_clicks" not in trigger_prop_id:
        return dash.no_update

    triggered_value = triggered[0].get("value")
    if not triggered_value or triggered_value <= 0:
        return dash.no_update
    
    try:
        prop_id_without_suffix = trigger_prop_id.replace(".n_clicks", "")
        triggered_id_dict = json.loads(prop_id_without_suffix)
        survey_id = str(triggered_id_dict.get("index"))
    except Exception:
        return dash.no_update
    
    if existing_queue:
        return dash.no_update
    
    survey_data = next((s for s in (surveys_data or []) if str(s.get("surveyId")) == survey_id), None)
    if not survey_data or not survey_data.get("applicantId") or not survey_data.get("pdf_url"):
        return _card_action_state(card_action_state, survey_id, "error", "Unavailable", "Survey data not found")
    
    name = f"{survey_data.get('firstName', '')} {survey_data.get('lastName', '')}".strip()
    return _card_action_state(card_action_state, survey_id, "uploading", "Uploading", name)

@callback(
    [Output("upload-queue", "data", allow_duplicate=True),
     Output("upload-results", "data", allow_duplicate=True),
     Output("upload-interval", "disabled", allow_duplicate=True),
     Output("upload-status", "children", allow_duplicate=True),
     Output("refresh-trigger", "data", allow_duplicate=True)],
    Input("upload-interval", "n_intervals"),
    [State("upload-queue", "data"),
     State("upload-results", "data"),
     State("refresh-trigger", "data")],
    prevent_initial_call=True
)
def process_upload_queue(n_intervals, queue, results, refresh_trigger):
    global _active_uploads, _completed_uploads
    
    if n_intervals is None or n_intervals <= 1:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    if not queue:
        if results:
            with _active_uploads_lock:
                _active_uploads.clear()
                _completed_uploads.clear()
            
            success_count = sum(1 for r in results.values() if r.get("success"))
            fail_count = len(results) - success_count
            msg = f"Done: {success_count} uploaded"
            if fail_count > 0:
                msg += f", {fail_count} failed"
            return [], results, True, msg, dash.no_update
        return [], results, True, "", dash.no_update
    
    current = queue[0]
    remaining = queue[1:]
    survey_id = str(current["survey_id"])
    
    with _active_uploads_lock:
        if survey_id in _completed_uploads:
            log(f"Skipping {survey_id} - already completed globally", "WARN")
            result = _completed_uploads[survey_id]
            if survey_id not in results:
                results[survey_id] = result
            
            if len(remaining) == 0:
                success_count = sum(1 for r in results.values() if r.get("success"))
                fail_count = len(results) - success_count
                msg = f"Done: {success_count} uploaded"
                if fail_count > 0:
                    msg += f", {fail_count} failed"
                _active_uploads.clear()
                _completed_uploads.clear()
                return remaining, results, True, msg, dash.no_update
            
            return remaining, results, False, f"Processing {len(results)}/{len(results)+len(remaining)}", dash.no_update
        
        if survey_id in _active_uploads:
            log(f"Skipping {survey_id} - actively processing", "WARN")
            return dash.no_update, dash.no_update, dash.no_update, "Processing", dash.no_update
        
        _active_uploads.add(survey_id)
    
    survey_data = {
        "surveyId": survey_id,
        "firstName": current["firstName"],
        "lastName": current["lastName"]
    }
    
    log(f"Starting upload process for {survey_id}", "WARN")
    try:
        result = jazzhr_service.upload_pdf_to_jazzhr(
            survey=survey_data,
            pdf_url=current["pdf_url"],
            applicant_id=current["applicant_id"]
        )
        
        log(f"Upload completed for {survey_id}: success={result.get('success')}", "WARN")
        
        if not result.get("success"):
            log(f"Upload failed: {survey_id} - {result.get('error')}", "ERROR")
        
        with _active_uploads_lock:
            _completed_uploads[survey_id] = result
            _active_uploads.discard(survey_id)
        
        results[survey_id] = result
    except Exception as e:
        log(f"Upload exception: {survey_id} - {e}", "ERROR")
        import traceback
        traceback.print_exc()
        result = {"success": False, "error": str(e)}
        
        with _active_uploads_lock:
            _completed_uploads[survey_id] = result
            _active_uploads.discard(survey_id)
        
        results[survey_id] = result
    
    completed = len(results)
    total = completed + len(remaining)

    if not remaining:
        with _active_uploads_lock:
            _active_uploads.clear()
            _completed_uploads.clear()

        success_count = sum(1 for r in results.values() if r.get("success"))
        fail_count = len(results) - success_count
        msg = f"Done: {success_count} uploaded"
        if fail_count > 0:
            msg += f", {fail_count} failed"

        return remaining, results, True, msg, dash.no_update
    
    return remaining, results, False, f"Uploading {completed}/{total}", dash.no_update

@callback(
    Output("card-action-state", "data", allow_duplicate=True),
    [Input("upload-queue", "data"),
     Input("upload-results", "data")],
    State("card-action-state", "data"),
    prevent_initial_call=True
)
def show_upload_card_progress(queue, results, card_action_state):
    if not queue and not results:
        cleared_state = _clear_stale_upload_states(card_action_state, set(), set())
        return cleared_state if cleared_state != (card_action_state or {}) else dash.no_update
    
    results = results or {}
    card_entries = []
    active_ids = {str(item.get("survey_id")) for item in (queue or [])}
    result_ids = {str(survey_id) for survey_id in results.keys()}
    
    for survey_id, result in results.items():
        card_entries.append(_upload_result_card_entry(survey_id, result))
    
    for index, item in enumerate(queue or []):
        survey_id = str(item.get("survey_id"))
        if survey_id in results:
            continue
        name = f"{item.get('firstName', '')} {item.get('lastName', '')}".strip()
        if index == 0:
            card_entries.append((survey_id, "uploading", "Uploading", name))
        else:
            card_entries.append((survey_id, "queued", "Queued", name))
    
    if not card_entries:
        cleared_state = _clear_stale_upload_states(card_action_state, active_ids, result_ids)
        return cleared_state if cleared_state != (card_action_state or {}) else dash.no_update
    
    cleaned_state = _clear_stale_upload_states(card_action_state, active_ids, result_ids)
    return _card_action_states(cleaned_state, card_entries)

@callback(
    [Output({"type": "survey-status-indicator", "index": ALL}, "children"),
     Output({"type": "survey-status-indicator", "index": ALL}, "className")],
    [Input("upload-results", "data"),
     Input("background-check-signal", "data")],
    [State({"type": "survey-status-indicator", "index": ALL}, "id"),
     State({"type": "survey-status-indicator", "index": ALL}, "children"),
     State({"type": "survey-status-indicator", "index": ALL}, "className")],
    prevent_initial_call=True
)
def update_status_indicator_after_events(upload_results, background_signal, status_ids, current_children, current_classes):
    if not status_ids:
        return dash.no_update, dash.no_update

    children = []
    classes = []
    changed = False
    upload_results = upload_results or {}
    current_children = current_children or [dash.no_update] * len(status_ids)
    current_classes = current_classes or [dash.no_update] * len(status_ids)

    for idx, status_id in enumerate(status_ids):
        survey_id = str(status_id.get("index"))
        upload_result = upload_results.get(survey_id)

        if upload_result and upload_result.get("success"):
            children.append([html.Span("Uploaded", className="status-text")])
            classes.append("status-uploaded")
            changed = True
            continue

        cached_status = jazzhr_cache.get(survey_id)
        if cached_status:
            next_children, next_class = _status_indicator_parts(cached_status)
            children.append(next_children)
            classes.append(next_class)
            changed = True
            continue

        children.append(current_children[idx] if idx < len(current_children) else dash.no_update)
        classes.append(current_classes[idx] if idx < len(current_classes) else dash.no_update)

    if not changed:
        return dash.no_update, dash.no_update

    return children, classes

@callback(
    Output({"type": "survey-upload-container", "index": ALL}, "style"),
    Input("upload-results", "data"),
    State({"type": "survey-upload-container", "index": ALL}, "id"),
    prevent_initial_call=True
)
def hide_upload_button_after_success(results, upload_container_ids):
    if not results or not upload_container_ids:
        return dash.no_update

    styles = []
    changed = False

    for container_id in upload_container_ids:
        survey_id = str(container_id.get("index"))
        result = results.get(survey_id)

        if result and result.get("success"):
            styles.append({"display": "none"})
            changed = True
        else:
            styles.append(dash.no_update)

    return styles if changed else dash.no_update

if __name__ == "__main__":
    log("Starting Dash app on port 8051...", "WARN")
    app.run(debug=False, port=8051)
