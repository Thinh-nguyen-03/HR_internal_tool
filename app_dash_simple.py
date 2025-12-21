import os
import sys
import time
import json
from datetime import datetime, timedelta
from threading import RLock, Thread, Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import requests
from dash import Dash, html, dcc, Input, Output, State, callback, ctx, ALL
import dash
from dotenv import load_dotenv

from cultureindex_client import CultureIndexClient
from surveys_fetch import format_phone_number, get_survey_urls_from_csv
from check_jazzhr_uploads import JazzHRUploadChecker
from cache_storage import create_cache, SmartJazzHRCache

load_dotenv()

# CONFIGURATION 
ITEMS_PER_PAGE = int(os.getenv('ITEMS_PER_PAGE', '15'))
MAX_BATCH_UPLOAD = int(os.getenv('MAX_BATCH_UPLOAD', '15'))
MAX_BACKGROUND_CHECK = int(os.getenv('MAX_BACKGROUND_CHECK', '250'))  # Only check latest N surveys in background
RECENT_SURVEY_THRESHOLD = int(os.getenv('RECENT_SURVEY_THRESHOLD', '1000'))  # Latest N surveys have normal TTL, older ones are permanent
CLIENT_ID = os.getenv('CLIENT_ID', 'A89F5B0000')
JAZZHR_CACHE_HOURS = int(os.getenv('JAZZHR_CACHE_HOURS', '24'))

# Upload Retry Configuration
UPLOAD_MAX_RETRIES = int(os.getenv('UPLOAD_MAX_RETRIES', '3'))
UPLOAD_RETRY_DELAY_BASE = int(os.getenv('UPLOAD_RETRY_DELAY_BASE', '2'))
UPLOAD_RETRY_DELAY_MAX = int(os.getenv('UPLOAD_RETRY_DELAY_MAX', '10'))

# UI Polling Intervals (milliseconds)
POLL_INTERVAL_MS = int(os.getenv('POLL_INTERVAL_MS', '3000'))
UPLOAD_INTERVAL_MS = int(os.getenv('UPLOAD_INTERVAL_MS', '1000'))

# PDF Fetch Configuration
PDF_FETCH_TIMEOUT = int(os.getenv('PDF_FETCH_TIMEOUT', '5'))

# LOGGING
def log(message: str, level: str = "INFO"):
    """Log with levels: ERROR, WARN, PERF, INFO"""
    if level not in ["ERROR", "WARN", "PERF"]:
        return
    
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{timestamp}] [{level}] {message}"
    print(line, flush=True)
    try:
        with open("app_simple_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} - [{level}] {message}\n")
    except:
        pass

# UTILITIES
def parse_csv_date(date_str: Optional[str]) -> Optional[str]:
    """Parse date from CSV - already in MM/DD/YYYY format."""
    if not date_str or not date_str.strip():
            return None
    return date_str.strip()

# SURVEY SERVICE
class SimpleSurveyService:
    """Survey service that uses CSV export as primary data source."""
    
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
            return self._client
        
        email = os.getenv("CULTUREINDEX_EMAIL")
        password = os.getenv("CULTUREINDEX_PASSWORD")
        
        if not email or not password:
            raise ValueError("Missing CULTUREINDEX_EMAIL or CULTUREINDEX_PASSWORD")
        
        self._client = CultureIndexClient()
        self._client.login(email=email, password=password)
        return self._client
    
    def _parse_csv_surveys(self, csv_data: str) -> List[Dict]:
        """Parse CSV data into list of survey dictionaries."""
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
        """Load all surveys from CSV."""
        with self._lock:
            if self._is_loaded and not force_refresh:
                return self._all_surveys
            if self._is_loading:
                return self._all_surveys
            self._is_loading = True
        
        try:
            log("Downloading fresh CSV data...", "WARN")
            start = time.time()
            client = self._get_client()
            csv_data = client.export_surveys_csv(client_id=self.client_id)
            surveys = self._parse_csv_surveys(csv_data)
            
            with self._lock:
                self._all_surveys = surveys
                self._is_loaded = True
                self._is_loading = False
            
            log(f"Loaded {len(surveys)} surveys from CSV in {time.time()-start:.1f}s", "WARN")
            return surveys
            
        except Exception as e:
            log(f"Failed to load surveys: {e}", "ERROR")
            with self._lock:
                self._is_loading = False
            return self._all_surveys
    
    def is_loaded(self) -> bool:
        return self._is_loaded
    
    def get_all_surveys(self) -> List[Dict]:
        return self._all_surveys
    
    def get_page(self, page_num: int) -> Tuple[List[Dict], int]:
        """Get a page of surveys."""
        total = len(self._all_surveys)
        start_idx = (page_num - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        return self._all_surveys[start_idx:end_idx], total
    
    def search_surveys(self, query: str, limit: int = 100) -> List[Dict]:
        """Search surveys by name."""
        if not query or len(query) < 2:
            return []
        
        query_lower = query.lower().strip()
        matches = []
        
        for survey in self._all_surveys:
            full_name = f"{survey.get('firstName', '')} {survey.get('lastName', '')}".lower()
            if query_lower in full_name:
                matches.append(survey)
                if len(matches) >= limit:
                    break
        
        return matches
    
    def clear_cache(self):
        with self._lock:
            self._all_surveys = []
            self._is_loaded = False

# JAZZHR SERVICE
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
    
    def _wait_for_rate_limit(self):
        with self._lock:
            now = datetime.now()
            cutoff = now - timedelta(minutes=1)
            self._call_times = [t for t in self._call_times if t >= cutoff]
            
            if len(self._call_times) >= int(self._rate_limit * 0.9):
                wait_time = 60 - (now - self._call_times[0]).total_seconds() + 0.5
                if wait_time > 0:
                    time.sleep(wait_time)
                    self._call_times = []
            
            self._call_times.append(datetime.now())
    
    def check_one_survey(self, survey: Dict, pdf_url: str, pdf_size: Optional[int]) -> Dict:
        survey_id = str(survey.get('surveyId', ''))
        
        cached = self.cache.get(survey_id)
        if cached:
            if cached.get('status') == 'NO_PDF_URL' and pdf_url:
                pass  # Re-check
            elif cached.get('status') == 'NOT_UPLOADED' and pdf_size and not cached.get('had_pdf_size'):
                pass  # Re-check
            else:
                return cached
        
        first_name = survey.get('firstName', '').strip()
        last_name = survey.get('lastName', '').strip()
        
        if not first_name or not last_name:
            result = {"status": "MISSING_NAME", "isUploaded": False}
            self.cache.set(survey_id, result)
            return result
        
        if not pdf_url:
            result = {"status": "NO_PDF_URL", "isUploaded": False}
            self.cache.set(survey_id, result)
            return result
        
        try:
            checker = self._get_checker()
            
            self._wait_for_rate_limit()
            applicant = checker.search_applicant_by_name(first_name, last_name, verbose=False)
            
            if not applicant:
                result = {"status": "NOT_IN_JAZZHR", "isUploaded": False}
                self.cache.set(survey_id, result)
                return result
            
            applicant_id = applicant.get('id')
            
            self._wait_for_rate_limit()
            files = checker.get_applicant_files(applicant_id, verbose=False)
            
            match = checker.check_pdf_match(pdf_url, pdf_size, files, first_name, last_name, verbose=False)
            
            if match:
                result = {
                    "status": "UPLOADED",
                    "applicant_id": applicant_id,
                    "isUploaded": True,
                    "matched_file": match['file'].get('filename'),
                    "file_count": len(files),
                    "had_pdf_size": pdf_size is not None
                }
            else:
                result = {
                    "status": "NOT_UPLOADED",
                    "applicant_id": applicant_id,
                    "isUploaded": False,
                    "file_count": len(files),
                    "had_pdf_size": pdf_size is not None
                }
            
            self.cache.set(survey_id, result)
            return result
            
        except Exception as e:
            log(f"JazzHR error for {survey_id}: {e}", "ERROR")
            return {"status": "ERROR", "isUploaded": False, "error": str(e)}
    
    def check_surveys_batch(self, surveys: List[Dict], urls: Dict[str, str], pdf_sizes: Dict[str, int]) -> Dict[str, Dict]:
        """Check JazzHR status for multiple surveys in parallel."""
        results = {}
        to_check = []
        
        for survey in surveys:
            survey_id = str(survey.get('surveyId', ''))
            cached = self.cache.get(survey_id)
            if cached:
                results[survey_id] = cached
            else:
                to_check.append(survey)
        
        if not to_check:
            return results
        
        def check_one(survey):
            survey_id = str(survey.get('surveyId', ''))
            url = urls.get(survey_id)
            size = pdf_sizes.get(survey_id)
            return survey_id, self.check_one_survey(survey, url, size)
        
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

    # Errors that should NOT be retried (permanent failures)
    # These are hardcoded as they represent API contract violations, not configurable behavior
    NON_RETRYABLE_ERRORS = [
        "401",  # Unauthorized - bad API key
        "403",  # Forbidden - no permission
        "404",  # Not found - bad applicant ID
        "apikey not set",
        "invalid api key",
        "applicant_id was not set",
        "file already exists",
    ]
    
    def _is_retryable_error(self, error_msg: str) -> bool:
        """Check if an error is worth retrying."""
        if not error_msg:
            return True
        error_lower = error_msg.lower()
        for non_retryable in self.NON_RETRYABLE_ERRORS:
            if non_retryable.lower() in error_lower:
                return False
        return True
    
    def upload_pdf_to_jazzhr(self, survey: Dict, pdf_url: str, applicant_id: str) -> Dict:
        """
        Upload a single PDF to JazzHR with automatic retry.
        
        Retry policy (configurable via .env):
        - Max attempts: UPLOAD_MAX_RETRIES (default: 3)
        - Exponential backoff: UPLOAD_RETRY_DELAY_BASE * 2^attempt (capped at UPLOAD_RETRY_DELAY_MAX)
        - Does NOT retry on auth errors, 404s, or other permanent failures
        - Returns immediately on success
        """
        survey_id = str(survey.get('surveyId', ''))
        checker = self._get_checker()
        first_name = survey.get('firstName', '').strip()
        last_name = survey.get('lastName', '').strip()
        
        last_error = None
        
        # Use module-level config constants
        max_retries = UPLOAD_MAX_RETRIES
        delay_base = UPLOAD_RETRY_DELAY_BASE
        delay_max = UPLOAD_RETRY_DELAY_MAX
        
        for attempt in range(max_retries):
            try:
                self._wait_for_rate_limit()
                
                result = checker.upload_file_to_applicant(
                    applicant_id=applicant_id,
                    pdf_url=pdf_url,
                    first_name=first_name,
                    last_name=last_name,
                    verbose=True
                )
                
                if result.get('success'):
                    # Success - clear cache and return
                    self.cache.delete(survey_id)
                    self.cache.save()
                    return result
                
                # Check if error is retryable
                error_msg = result.get('error', '')
                if not self._is_retryable_error(error_msg):
                    log(f"Upload failed with non-retryable error: {error_msg}", "ERROR")
                    return result  # Don't retry permanent failures
                
                last_error = error_msg
                
            except Exception as e:
                last_error = str(e)
                log(f"Upload exception on attempt {attempt + 1}: {e}", "ERROR")
            
            # Retry logic (only if not last attempt)
            if attempt < max_retries - 1:
                delay = min(
                    delay_base * (2 ** attempt),
                    delay_max
                )
                time.sleep(delay)
        
        # All retries exhausted
        log(f"Upload failed after {max_retries} attempts for {survey_id}: {last_error}", "ERROR")
        return {
            'success': False,
            'error': f"Failed after {max_retries} attempts: {last_error}",
            'survey_id': survey_id
        }

# PDF SIZE FETCHER
def fetch_pdf_sizes(urls: Dict[str, str], pdf_cache, max_workers: int = 8) -> Dict[str, int]:
    """Fetch PDF sizes from URLs with caching."""
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
                    pdf_cache.set(survey_id, {'size': size})
            except:
                pass
    
    if results:
        pdf_cache.save()
    
    return results

# BACKGROUND JAZZHR CHECKER
class BackgroundJazzHRChecker:
    """Background checker for JazzHR status - only checks latest surveys."""
    
    def __init__(self, survey_service, jazzhr_service, pdf_cache):
        self.survey_service = survey_service
        self.jazzhr_service = jazzhr_service
        self.pdf_cache = pdf_cache
        self._check_thread = None
        self._stop_flag = False
        self._lock = RLock()
    
    def start_checking(self):
        with self._lock:
            if self._check_thread and self._check_thread.is_alive():
                return
            self._stop_flag = False
            self._check_thread = Thread(target=self._check_worker, daemon=True, name="JazzHRChecker")
            self._check_thread.start()
    
    def stop_checking(self):
        self._stop_flag = True
    
    def _check_worker(self):
        try:
            # Wait for surveys to load
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
            
            # Only check latest MAX_BACKGROUND_CHECK surveys - older ones use cache
            surveys_to_check = all_surveys[:MAX_BACKGROUND_CHECK]
            total_surveys = len(all_surveys)
            
            log(f"JazzHR Checker: Checking {len(surveys_to_check)} of {total_surveys} surveys (older surveys use cache)", "WARN")
            
            batch_size = ITEMS_PER_PAGE
            for i in range(0, len(surveys_to_check), batch_size):
                if self._stop_flag:
                    break
                
                batch = surveys_to_check[i:i + batch_size]
                urls = {str(s['surveyId']): s.get('surveyReportUrl') for s in batch}
                urls_to_check = {sid: url for sid, url in urls.items() if url}
                pdf_sizes = fetch_pdf_sizes(urls_to_check, self.pdf_cache)
                
                self.jazzhr_service.check_surveys_batch(batch, urls, pdf_sizes)
                
                if i + batch_size < len(surveys_to_check):
                    time.sleep(1)
            
            log(f"JazzHR Checker: Completed checking {len(surveys_to_check)} surveys", "WARN")
            
        except Exception as e:
            log(f"JazzHR Checker error: {e}", "ERROR")

# SERVICE INITIALIZATION
survey_service = SimpleSurveyService(client_id=CLIENT_ID, items_per_page=ITEMS_PER_PAGE)

# Create caches using the storage abstraction (supports file or Redis via CACHE_BACKEND env var)
jazzhr_cache_backend = create_cache("jazzhr_status", ttl_hours=JAZZHR_CACHE_HOURS, cache_file="jazzhr_status_cache.json")
pdf_size_cache = create_cache("pdf_sizes", ttl_hours=168, cache_file="pdf_sizes_cache.json")

# Smart JazzHR cache: recent surveys (latest 2000) have normal TTL, older ones are permanent
jazzhr_cache = SmartJazzHRCache(jazzhr_cache_backend, recent_threshold=RECENT_SURVEY_THRESHOLD)

jazzhr_api_key = os.getenv('JAZZHR_API_KEY')
if not jazzhr_api_key:
    log("ERROR: JAZZHR_API_KEY not found!", "ERROR")
else:
    log(f"JazzHR API key loaded: {jazzhr_api_key[:10]}...", "WARN")

jazzhr_service = SimpleJazzHRService(api_key=jazzhr_api_key, cache=jazzhr_cache, max_workers=6)
background_checker = BackgroundJazzHRChecker(survey_service, jazzhr_service, pdf_size_cache)

# Load surveys on startup
log("Starting app - loading surveys from CSV...", "WARN")

def _background_csv_load():
    try:
        survey_service.load_surveys(force_refresh=True)
        # Update smart cache with recent survey IDs (for TTL handling)
        all_surveys = survey_service.get_all_surveys()
        recent_ids = [str(s['surveyId']) for s in all_surveys[:RECENT_SURVEY_THRESHOLD]]
        jazzhr_cache.set_recent_surveys(recent_ids)
        log(f"Set {len(recent_ids)} surveys as 'recent' (24h TTL), older surveys use permanent cache", "WARN")
        background_checker.start_checking()
    except Exception as e:
        log(f"Background CSV load error: {e}", "ERROR")

Thread(target=_background_csv_load, daemon=True, name="CSVLoader").start()

# DASH APP
app = Dash(__name__, suppress_callback_exceptions=True)
app.title = "Culture Index - HR Tool"
server = app.server  # Expose Flask server for health checks

if not os.path.exists("assets"):
    os.makedirs("assets")

# =============================================================================
# HEALTH CHECK ENDPOINT
# =============================================================================

@server.route('/health')
def health_check():
    """
    Health check endpoint for monitoring and load balancers.
    Returns JSON with system status.
    
    Usage: GET /health
    Returns: {"status": "healthy/degraded/unhealthy", ...}
    """
    from flask import jsonify
    
    try:
        # Check Redis cache
        redis_healthy = jazzhr_cache.ping()
        redis_status = jazzhr_cache.is_healthy()
        
        # Check PDF cache
        pdf_cache_healthy = pdf_size_cache.ping() if hasattr(pdf_size_cache, 'ping') else True
        
        # Check survey service
        surveys_loaded = survey_service.is_loaded()
        survey_count = len(survey_service.get_all_surveys()) if surveys_loaded else 0
        
        # Determine overall status
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
def readiness_check():
    """
    Readiness check - returns 200 only when app is fully ready to serve requests.
    Used by Kubernetes/cloud platforms to know when to route traffic.
    """
    from flask import jsonify
    
    if not survey_service.is_loaded():
        return jsonify({"ready": False, "reason": "Surveys not loaded"}), 503
    
    if not jazzhr_cache.ping():
        return jsonify({"ready": False, "reason": "Redis not connected"}), 503
    
    return jsonify({"ready": True})

@server.route('/health/live')
def liveness_check():
    """
    Liveness check - returns 200 if app is running (even if degraded).
    Used by Kubernetes/cloud platforms to know if app needs restart.
    """
    from flask import jsonify
    return jsonify({"alive": True, "timestamp": datetime.now().isoformat()})

# LAYOUT
app.layout = html.Div([
    html.Div([
        # Header
        html.Div([
            html.Div([
                html.Img(src="/assets/SENERGY-Logo_Icon-Yellow.png", className="header-logo"),
            ], className="header-left"),
            
            html.Div([
                html.Div("HR INTERNAL TOOL [v4.0]", className="header-title"),
            ], className="header-center"),
            
            html.Div([
                html.Div(id="upload-status", className="upload-status"),
                html.Button("Upload Selected", id="upload-btn", className="upload-btn", n_clicks=0, disabled=True),
                html.Button("Refresh JazzHR", id="refresh-jazzhr-btn", className="refresh-btn", n_clicks=0),
                html.Button("Refresh CI", id="refresh-btn", className="refresh-btn", n_clicks=0),
            ], className="header-right"),
        ], className="header"),
        
        # Main Content
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
                                placeholder="Search by name...",
                                debounce=True,
                                className="search-input"
                            ),
                            html.Button("Clear", id="clear-search-btn", className="clear-search-btn", n_clicks=0),
                        ], className="search-container"),
                        
                        html.Div([
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
                
                html.Div(id="surveys-container", className="surveys-list"),
                
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
    
    # State stores
    dcc.Store(id="current-page", data=1),
    dcc.Store(id="search-query", data=""),
    dcc.Store(id="current-surveys-data", data=[]),
    dcc.Store(id="uploadable-ids", data=[]),
    dcc.Store(id="upload-queue", data=[]),
    dcc.Store(id="upload-results", data={}),
    dcc.Store(id="cache-version", data=0),
    dcc.Store(id="refresh-trigger", data=0),
    
    # Intervals (configurable via .env)
    dcc.Interval(id="poll-interval", interval=POLL_INTERVAL_MS, n_intervals=0),
    dcc.Interval(id="upload-interval", interval=UPLOAD_INTERVAL_MS, n_intervals=0, disabled=True),
])

# HELPER FUNCTION - BUILD SURVEY DISPLAY
def build_survey_display(surveys: List[Dict], jazzhr_results: Dict, pdf_sizes: Dict) -> Tuple[List, List, List]:
    """Build survey display elements. Returns (survey_items, uploadable_ids, surveys_data)."""
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
        applicant_id = jazzhr.get('applicant_id')
        
        is_uploadable = (status == "NOT_UPLOADED" and applicant_id and url)
        
        if is_uploadable:
            uploadable_ids.append(survey_id)
        
        surveys_data.append({
            "surveyId": survey_id,
            "firstName": s.get('firstName', ''),
            "lastName": s.get('lastName', ''),
            "applicant_id": applicant_id,
            "pdf_url": url
        })
        
        # Build status indicator
        if status is None:
            status_indicator = html.Div([html.Span("Checking...", className="status-text")], className="status-pending")
        elif is_uploaded:
            status_indicator = html.Div([html.Span("Uploaded", className="status-text")], className="status-uploaded")
        elif status == "NOT_UPLOADED":
            status_indicator = html.Div([html.Span("Not Uploaded", className="status-text")], className="status-missing")
        elif status == "NOT_IN_JAZZHR":
            status_indicator = html.Div([html.Span("Not in Jazz", className="status-text")], className="status-not-found")
        elif status == "MISSING_NAME":
            status_indicator = html.Div([html.Span("No Name", className="status-text")], className="status-pending")
        elif status == "NO_PDF_URL":
            status_indicator = html.Div([html.Span("No URL", className="status-text")], className="status-pending")
        elif status == "ERROR":
            status_indicator = html.Div([html.Span("Error", className="status-text")], className="status-error")
        else:
            status_indicator = html.Div([html.Span("Unknown", className="status-text")], className="status-pending")
        
        pdf_size_mb = pdf_size / (1024 * 1024) if pdf_size else None
        full_name = f"{s.get('firstName', '')} {s.get('lastName', '')}".strip() or "Unknown"
        position = s.get('position', '').strip()
        trait_pattern = s.get('traitPattern', 'N/A')
        
        # Build card
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
                ], className="survey-upload-container")
            )
        
        survey_items.append(html.Div(card_children, className="survey-item"))
    
    return survey_items, uploadable_ids, surveys_data

# CALLBACK 1: Main Display (triggered by page/search/refresh changes)
@callback(
    [Output("surveys-container", "children"),
     Output("page-info", "children"),
     Output("prev-page-btn", "disabled"),
     Output("next-page-btn", "disabled"),
     Output("last-updated", "children"),
     Output("current-surveys-data", "data"),
     Output("uploadable-ids", "data"),
     Output("loading-indicator", "style"),
     Output("cache-version", "data")],
    [Input("current-page", "data"),
     Input("search-query", "data"),
     Input("refresh-trigger", "data"),
     Input("poll-interval", "n_intervals")],
    [State("cache-version", "data")],
    prevent_initial_call=False
)
def display_surveys(page, search_query, refresh_trigger, n_intervals, last_cache_version):
    """Display surveys - pure rendering, no side effects."""
    try:
        start_time = time.time()
        triggered_id = ctx.triggered_id
        is_loaded = survey_service.is_loaded()
        
        # Check if loaded - show loading message if not ready
        if not is_loaded:
            return (
                [html.Div("Loading surveys...", className="empty-message")],
                "Loading...", True, True, "Loading data...",
                [], [], {"display": "block"}, 0
            )
        
        # For interval, check if we should skip this update
        if triggered_id == "poll-interval":
            current_version = jazzhr_cache.get_version()
            
            # If this is the first render after loading (last was 0 or None), allow it
            # Otherwise, only update if cache actually changed
            if last_cache_version is not None and last_cache_version != 0:
                if current_version == last_cache_version:
                    return (dash.no_update,) * 9
        
        # Get surveys
        if search_query and len(search_query) >= 2:
            all_results = survey_service.search_surveys(search_query, limit=100)
            total_count = len(all_results)
            start_idx = (page - 1) * ITEMS_PER_PAGE
            surveys = all_results[start_idx:start_idx + ITEMS_PER_PAGE]
        else:
            surveys, total_count = survey_service.get_page(page)
        
        if not surveys:
            msg = f"No results for '{search_query}'" if search_query else "No surveys found."
            return (
                [html.Div(msg, className="empty-message")],
                "0 results", True, True, f"Updated: {datetime.now().strftime('%I:%M:%S %p')}",
                [], [], {"display": "none"}, jazzhr_cache.get_version()
            )
        
        # Get JazzHR status - use cache-only on interval triggers to avoid slowdown
        urls = {str(s['surveyId']): s.get('surveyReportUrl') for s in surveys}
        
        # Only fetch PDF sizes and do JazzHR checks if NOT an interval trigger
        # Background checker handles JazzHR updates
        if triggered_id == "poll-interval":
            # Fast path: cache-only for interval updates
            pdf_sizes = {}
            jazzhr_results = {}
            for s in surveys:
                survey_id = str(s['surveyId'])
                cached = jazzhr_cache.get(survey_id)
                if cached:
                    jazzhr_results[survey_id] = cached
                else:
                    # Default to checking status for uncached items
                    jazzhr_results[survey_id] = {"status": None, "isUploaded": False}
        else:
            # Slow path: full checks for user-initiated actions (page change, search, refresh)
            urls_to_check = {sid: url for sid, url in urls.items() if url}
            pdf_sizes = fetch_pdf_sizes(urls_to_check, pdf_size_cache)
            jazzhr_results = jazzhr_service.check_surveys_batch(surveys, urls, pdf_sizes)
        
        # Build display
        survey_items, uploadable_ids, surveys_data = build_survey_display(surveys, jazzhr_results, pdf_sizes)
        
        total_pages = max(1, (total_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        
        if search_query:
            page_info = f"Page {page} of {total_pages} ({total_count} results for '{search_query}')"
        else:
            page_info = f"Page {page} of {total_pages} ({total_count:,} surveys)"
        
        elapsed = time.time() - start_time
        updated_text = f"Updated: {datetime.now().strftime('%I:%M:%S %p')} ({elapsed:.1f}s) [Cache: {jazzhr_cache.get_count()}]"
        
        current_version = jazzhr_cache.get_version()
        
        # If cache version is still 0, use a timestamp-based version instead
        # This ensures we don't get stuck in a loop
        if current_version == 0:
            current_version = int(time.time() * 1000) % 1000000  # millisecond timestamp mod 1M
        
        return (
            survey_items, page_info,
            page <= 1, page >= total_pages,
            updated_text, surveys_data, uploadable_ids,
            {"display": "none"}, current_version
        )
    except Exception as e:
        log(f"ERROR in display_surveys: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        return (
            [html.Div(f"Error loading surveys: {str(e)}", className="empty-message")],
            "Error", True, True, "Error",
            [], [], {"display": "none"}, 0
        )

# CALLBACK 2: Pagination (prev/next buttons)
@callback(
    Output("current-page", "data"),
    [Input("prev-page-btn", "n_clicks"),
     Input("next-page-btn", "n_clicks")],
    [State("current-page", "data")],
    prevent_initial_call=True
)
def handle_pagination(prev_clicks, next_clicks, current_page):
    """Handle pagination buttons."""
    triggered_id = ctx.triggered_id
    
    if triggered_id == "prev-page-btn":
        return max(1, current_page - 1)
    elif triggered_id == "next-page-btn":
        return current_page + 1
    
    return dash.no_update

# CALLBACK 3: Search (input and clear button)
@callback(
    [Output("search-query", "data"),
     Output("current-page", "data", allow_duplicate=True),
     Output("search-input", "value")],
    [Input("search-input", "value"),
     Input("clear-search-btn", "n_clicks")],
    prevent_initial_call=True
)
def handle_search(search_value, clear_clicks):
    """Handle search input and clear button."""
    triggered_id = ctx.triggered_id
    
    if triggered_id == "clear-search-btn":
        return "", 1, ""
    elif triggered_id == "search-input":
        return search_value or "", 1, dash.no_update
    
    return dash.no_update, dash.no_update, dash.no_update

# CALLBACK 4: Refresh CI Button
@callback(
    [Output("refresh-trigger", "data", allow_duplicate=True),
     Output("search-query", "data", allow_duplicate=True),
     Output("current-page", "data", allow_duplicate=True),
     Output("search-input", "value", allow_duplicate=True)],
    Input("refresh-btn", "n_clicks"),
    State("refresh-trigger", "data"),
    prevent_initial_call=True
)
def handle_refresh_ci(n_clicks, current_trigger):
    """Handle Refresh CI button - reload CSV."""
    if not n_clicks:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    survey_service.clear_cache()
    background_checker.stop_checking()
    
    def _reload():
        survey_service.load_surveys(force_refresh=True)
        background_checker.start_checking()
    
    Thread(target=_reload, daemon=True).start()
    
    return current_trigger + 1, "", 1, ""

# CALLBACK 5: Refresh JazzHR Button
@callback(
    Output("refresh-trigger", "data", allow_duplicate=True),
    Input("refresh-jazzhr-btn", "n_clicks"),
    [State("current-surveys-data", "data"),
     State("refresh-trigger", "data")],
    prevent_initial_call=True
)
def handle_refresh_jazzhr(n_clicks, surveys_data, current_trigger):
    """Handle Refresh JazzHR button - clear cache for visible surveys."""
    if not n_clicks:
        return dash.no_update
    
    
    cleared = 0
    for s in surveys_data:
        survey_id = str(s.get("surveyId", ""))
        if jazzhr_cache.get(survey_id):
            jazzhr_cache.delete(survey_id)
            cleared += 1
    
    if cleared > 0:
        jazzhr_cache.save()
    
    return current_trigger + 1

# CALLBACK 6: Select All Checkbox
@callback(
    Output({"type": "survey-checkbox", "index": ALL}, "value"),
    Input("select-all-checkbox", "value"),
    [State("uploadable-ids", "data"),
     State({"type": "survey-checkbox", "index": ALL}, "id")],
    prevent_initial_call=True
)
def handle_select_all(select_all_value, uploadable_ids, checkbox_ids):
    """Handle select all checkbox."""
    if not checkbox_ids:
        return []
    
    if "all" in select_all_value:
        return [[id_dict["index"]] if id_dict["index"] in uploadable_ids else [] for id_dict in checkbox_ids]
    else:
        return [[] for _ in checkbox_ids]

# CALLBACK 7: Selection Count
@callback(
    [Output("upload-btn", "disabled"),
     Output("selection-count", "children")],
    Input({"type": "survey-checkbox", "index": ALL}, "value"),
    prevent_initial_call=True
)
def update_selection_count(checkbox_values):
    """Update selection count and upload button state."""
    selected = [v[0] for v in checkbox_values if v]
    count = len(selected)
    
    if count == 0:
        return True, ""
    elif count > MAX_BATCH_UPLOAD:
        return True, f"({count} selected - max {MAX_BATCH_UPLOAD})"
    else:
        return False, f"({count} selected)"

# CALLBACK 8: Upload Selected Button
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
    """Handle Upload Selected button."""
    if not n_clicks or existing_queue:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    selected_ids = [v[0] for v in checkbox_values if v]
    
    if not selected_ids:
        return [], {}, True, ""
    
    if len(selected_ids) > MAX_BATCH_UPLOAD:
        return [], {}, True, f"Max {MAX_BATCH_UPLOAD} at a time"
    
    queue = []
    for survey_id in selected_ids:
        survey_data = next((s for s in surveys_data if s["surveyId"] == survey_id), None)
        if survey_data and survey_data.get("applicant_id") and survey_data.get("pdf_url"):
            queue.append({
                "survey_id": str(survey_id),
                "firstName": survey_data["firstName"],
                "lastName": survey_data["lastName"],
                "applicant_id": survey_data["applicant_id"],
                "pdf_url": survey_data["pdf_url"]
            })
    
    if not queue:
        return [], {}, True, "No uploadable surveys"
    
    return queue, {}, False, f"Uploading 0/{len(queue)}..."

# CALLBACK 9: Single Upload Button
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
    """Handle individual upload button clicks."""
    # Get which button was clicked
    triggered = ctx.triggered
    if not triggered or not triggered[0]:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    trigger_prop_id = triggered[0].get("prop_id", "")
    if ".n_clicks" not in trigger_prop_id:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    # Don't start new upload if one is in progress
    if existing_queue:
        return dash.no_update, dash.no_update, dash.no_update, "Upload in progress"
    
    # Parse button index
    try:
        prop_id_without_suffix = trigger_prop_id.replace(".n_clicks", "")
        triggered_id_dict = json.loads(prop_id_without_suffix)
        survey_id = str(triggered_id_dict.get("index"))
    except:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    if not survey_id:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    # Verify click happened (not just initialization)
    has_click = any(c and c > 0 for c in (n_clicks_list or []))
    if not has_click:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    # Find survey data
    survey_data = next((s for s in surveys_data if str(s.get("surveyId")) == survey_id), None)
    if not survey_data or not survey_data.get("applicant_id") or not survey_data.get("pdf_url"):
        return dash.no_update, dash.no_update, dash.no_update, "Survey data not found"
    
    queue = [{
        "survey_id": survey_id,
        "firstName": survey_data["firstName"],
        "lastName": survey_data["lastName"],
        "applicant_id": survey_data["applicant_id"],
        "pdf_url": survey_data["pdf_url"]
    }]
    
    name = f"{survey_data.get('firstName', '')} {survey_data.get('lastName', '')}".strip()
    return queue, {}, False, f"Uploading {name}..."

# CALLBACK 10: Process Upload Queue
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
    """Process upload queue one item at a time."""
    # Skip first interval (prevents stale queue processing)
    if n_intervals is None or n_intervals <= 1:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update
    
    if not queue:
        # Done
        if results:
            success_count = sum(1 for r in results.values() if r.get("success"))
            fail_count = len(results) - success_count
            msg = f"Done: {success_count} uploaded"
            if fail_count > 0:
                msg += f", {fail_count} failed"
            return [], results, True, msg, refresh_trigger + 1
        return [], results, True, "", dash.no_update
    
    # Process first item
    current = queue[0]
    remaining = queue[1:]
    survey_id = str(current["survey_id"])
    
    survey_data = {
        "surveyId": survey_id,
        "firstName": current["firstName"],
        "lastName": current["lastName"]
    }
    
    try:
        
        result = jazzhr_service.upload_pdf_to_jazzhr(
            survey=survey_data,
            pdf_url=current["pdf_url"],
            applicant_id=current["applicant_id"]
        )
        
        if not result.get("success"):
            log(f"Upload failed: {survey_id} - {result.get('error')}", "ERROR")
        
        results[survey_id] = result
    except Exception as e:
        log(f"Upload exception: {survey_id} - {e}", "ERROR")
        results[survey_id] = {"success": False, "error": str(e)}
    
    completed = len(results)
    total = completed + len(remaining)
    
    return remaining, results, False, f"Uploading {completed}/{total}...", dash.no_update

if __name__ == "__main__":
    log("Starting Dash app on port 8051...", "WARN")
    app.run(debug=False, port=8051)
