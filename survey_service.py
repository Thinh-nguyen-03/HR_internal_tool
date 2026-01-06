import os
import re
import sys
import time
from datetime import datetime
from threading import RLock, Thread
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from cultureindex_client import CultureIndexClient, CultureIndexAuthError
from surveys_fetch import format_phone_number, convert_to_central_date, get_survey_urls_from_csv

from cache_manager import ThreadSafeCache, handle_errors

# Logging function with file output
def log_debug(message):
    """Log with immediate flush to ensure visibility"""
    try:
        print(message, flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        # Also write to file
        try:
            with open("debug_log.txt", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} - {message}\n")
                f.flush()
        except:
            pass
    except Exception as e:
        try:
            sys.stderr.write(f"LOGGING ERROR: {e}\n")
            sys.stderr.flush()
        except:
            pass


class SurveyService:
    def __init__(self, client_id: str, items_per_page: int = 15):
        self.client_id = client_id
        self.items_per_page = items_per_page
        
        self.page_cache = {}  # {page_num: [surveys]}
        self.survey_urls_cache = None  # {survey_id: url}
        self.pdf_sizes_cache = ThreadSafeCache(max_size=500)
        
        self.client_cache = {"client": None, "last_login": None}
        
        self.page_cache_lock = RLock()
        self.urls_cache_lock = RLock()
        self.client_cache_lock = RLock()
        
        # Async PDF size fetching
        self.pdf_size_updates = {}  # {survey_id: size_info}
        self.pdf_size_checking = set()  # {survey_id} - surveys currently being checked
        self.pdf_size_lock = RLock()
        self.pdf_size_executor = None
        self.pdf_sizes_cache_lock = RLock()  # Lock for pdf_sizes_cache access
    
    @handle_errors(default_return=None, error_prefix="[AUTH] ")
    def get_authenticated_client(self) -> Optional[CultureIndexClient]:
        email = os.getenv("CULTUREINDEX_EMAIL")
        password = os.getenv("CULTUREINDEX_PASSWORD")
        
        if not email or not password:
            raise Exception("Environment variables not set")
        
        with self.client_cache_lock:
            if self.client_cache["client"] and self.client_cache["client"].is_authenticated():
                return self.client_cache["client"]
            
            client = CultureIndexClient()
            client.login(email=email, password=password)
            self.client_cache["client"] = client
            
            return client
    
    @handle_errors(default_return=([], 0), error_prefix="[FETCH] ")
    def fetch_page(self, page_num: int, force_refresh: bool = False) -> Tuple[List[Dict], int]:
        fetch_start = time.time()
        log_debug(f"[FETCH_PAGE] Fetching page {page_num} (force_refresh={force_refresh})")
        
        cache_check_start = time.time()
        with self.page_cache_lock:
            if not force_refresh and page_num in self.page_cache:
                total = self.page_cache.get('_total', 0)
                elapsed = time.time() - fetch_start
                log_debug(f"[FETCH_PAGE] Page {page_num} found in cache in {time.time() - cache_check_start:.3f}s (total: {elapsed:.3f}s)")
                return self.page_cache[page_num], total
        
        log_debug(f"[FETCH_PAGE] Page {page_num} not in cache, fetching from API")
        
        auth_start = time.time()
        client = self.get_authenticated_client()
        if not client:
            log_debug(f"[FETCH_PAGE] Failed to get authenticated client in {time.time() - auth_start:.3f}s")
            return [], 0
        log_debug(f"[FETCH_PAGE] Got authenticated client in {time.time() - auth_start:.3f}s")
        
        start_idx = (page_num - 1) * self.items_per_page
        api_start = time.time()
        batch, total_count = client.get_survey_batch(
            client_id=self.client_id,
            start=start_idx,
            batch_size=self.items_per_page
        )
        api_time = time.time() - api_start
        log_debug(f"[FETCH_PAGE] API call completed in {api_time:.3f}s (got {len(batch)} surveys)")
        
        process_start = time.time()
        surveys = []
        for s in batch:
            surveys.append({
                "surveyId": s.get("surveyId"),
                "firstName": s.get("firstName", ""),
                "lastName": s.get("lastName", ""),
                "email": s.get("email", ""),
                "phoneNumber": format_phone_number(s.get("phoneNumber", "")),
                "traitPattern": s.get("traitPattern", ""),
                "surveyTakerId": s.get("surveyTakerId"),
                "surveyDate": convert_to_central_date(s.get("surveyDate")),
                "surveyReportUrl": None,
                "pdfSizeMB": None,
                "jazzhrStatus": None,
                "jazzhrApplicantId": None,
                "isUploaded": False,
            })
        process_time = time.time() - process_start
        log_debug(f"[FETCH_PAGE] Processed {len(surveys)} surveys in {process_time:.3f}s")
        
        cache_save_start = time.time()
        with self.page_cache_lock:
            self.page_cache[page_num] = surveys
            self.page_cache['_total'] = total_count
        cache_save_time = time.time() - cache_save_start
        log_debug(f"[FETCH_PAGE] Saved to cache in {cache_save_time:.3f}s")
        
        total_time = time.time() - fetch_start
        log_debug(f"[FETCH_PAGE] Page {page_num} fetch completed in {total_time:.3f}s total")
        
        return surveys, total_count
    
    @handle_errors(default_return={}, error_prefix="[URLS] ")
    def load_survey_urls(self, force_refresh: bool = False) -> Dict[str, str]:
        url_load_start = time.time()
        with self.urls_cache_lock:
            if not force_refresh and self.survey_urls_cache is not None:
                log_debug(f"[URLS] Using cached URLs ({len(self.survey_urls_cache)} URLs) in {time.time() - url_load_start:.3f}s")
                return self.survey_urls_cache
            
            log_debug(f"[URLS] Loading survey URLs from CSV export...")
            step1 = time.time()
            client = self.get_authenticated_client()
            if not client:
                log_debug(f"[URLS] Failed to get authenticated client in {time.time() - step1:.3f}s")
                return self.survey_urls_cache if self.survey_urls_cache is not None else {}
            log_debug(f"[URLS] Got authenticated client in {time.time() - step1:.3f}s")
            
            step2 = time.time()
            
            # Try to export surveys, retry once if token expired
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    csv_data = client.export_surveys_csv(client_id=self.client_id)
                    break  # Success, exit retry loop
                except CultureIndexAuthError as auth_error:
                    if "Token expired or invalid" in str(auth_error) and attempt < max_retries - 1:
                        log_debug("[URLS] Token expired, re-authenticating...")
                        # Clear the client cache to force re-authentication
                        with self.client_cache_lock:
                            self.client_cache["client"] = None
                        client = self.get_authenticated_client()
                        log_debug("[URLS] Re-authentication successful, retrying request...")
                    else:
                        raise  # Re-raise if not token expiry or last attempt
            
            log_debug(f"[URLS] CSV export completed in {time.time() - step2:.3f}s (size: {len(csv_data)} chars)")
            
            step3 = time.time()
            self.survey_urls_cache = get_survey_urls_from_csv(csv_data)
            elapsed = time.time() - url_load_start
            log_debug(f"[URLS] Loaded {len(self.survey_urls_cache)} survey URLs in {time.time() - step3:.3f}s (total: {elapsed:.3f}s)")
            return self.survey_urls_cache
    
    def preload_urls_background(self):
        def _preload():
            try:
                log_debug("Pre-loading survey URLs in background thread...")
                self.load_survey_urls(force_refresh=False)
                log_debug("Background URL pre-loading complete!")
            except Exception as e:
                log_debug(f"Background preload failed (non-critical): {e}")
        
        thread = Thread(target=_preload, daemon=True, name="URLPreloader")
        thread.start()
        return thread
    
    @handle_errors(default_return={}, error_prefix="[URLS] ")
    def enrich_surveys_with_urls(self, survey_ids: List[str], force_refresh: bool = False) -> Dict[str, str]:
        url_cache = self.load_survey_urls(force_refresh=force_refresh)
        return {sid: url_cache.get(sid) for sid in survey_ids if url_cache.get(sid)}
    
    def get_cached_urls_only(self, survey_ids: List[str]) -> Dict[str, str]:
        """Get URLs from cache only (fast, non-blocking). Returns empty dict if cache not loaded yet."""
        with self.urls_cache_lock:
            if self.survey_urls_cache is None:
                return {}
            return {sid: self.survey_urls_cache.get(sid) for sid in survey_ids if self.survey_urls_cache.get(sid)}
    
    def start_url_loading_async(self, survey_ids: List[str], force_refresh: bool = False):
        """Start loading URLs asynchronously (non-blocking)."""
        log_debug(f"[URLS] Starting async URL loading (force_refresh={force_refresh}) for {len(survey_ids)} surveys...")
        def _load():
            try:
                load_start = time.time()
                self.load_survey_urls(force_refresh=force_refresh)
                log_debug(f"[URLS] Async URL loading completed in {time.time() - load_start:.3f}s")
            except Exception as e:
                log_debug(f"[URLS] Error loading URLs async: {e}")
                import traceback
                traceback.print_exc()
        
        thread = Thread(target=_load, daemon=True, name="URLLoader")
        thread.start()
        log_debug(f"[URLS] Async URL loading thread started")
    
    @handle_errors(default_return=None, error_prefix="[PDF] ")
    def get_pdf_size(self, pdf_url: str, session: requests.Session) -> Optional[Dict]:
        if not pdf_url or not pdf_url.strip():
            return None
        
        # Try HEAD request first (faster, no body transfer)
        try:
            response = session.head(pdf_url, allow_redirects=True, timeout=5, stream=True)
            response.raise_for_status()
            content_length = response.headers.get('Content-Length')
            response.close()
            if content_length:
                file_size = int(content_length)
                return {
                    'size': file_size,
                    'size_kb': file_size / 1024,
                    'size_mb': file_size / (1024 * 1024)
                }
        except Exception as e:
            # Continue to fallback method
            pass
        
        # Fallback to Range request if HEAD doesn't provide size
        try:
            headers = {'Range': 'bytes=0-0'}
            response = session.get(pdf_url, headers=headers, allow_redirects=True, timeout=8, stream=True)
            response.raise_for_status()
            
            file_size = 0
            content_range = response.headers.get('Content-Range')
            if content_range:
                match = re.search(r'/(\d+)', content_range)
                if match:
                    file_size = int(match.group(1))
            else:
                file_size = int(response.headers.get('Content-Length', 0))
            
            response.close()
            
            if file_size > 0:
                return {
                    'size': file_size,
                    'size_kb': file_size / 1024,
                    'size_mb': file_size / (1024 * 1024)
                }
        except Exception as e:
            # Size fetch failed
            pass
        
        return None
    
    def _initialize_pdf_executor(self):
        if self.pdf_size_executor is None:
            self.pdf_size_executor = ThreadPoolExecutor(
                max_workers=10,
                thread_name_prefix="PDFSize"
            )
    
    def start_pdf_size_checks_async(self, survey_urls: Dict[str, str]):
        """Start PDF size checks asynchronously (non-blocking)."""
        if not survey_urls:
            return
        
        log_debug(f"[PDF] Starting async PDF size checks for {len(survey_urls)} surveys...")
        pdf_start = time.time()
        self._initialize_pdf_executor()
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        log_debug(f"[PDF] Initialized executor and session in {time.time() - pdf_start:.3f}s")
        
        def fetch_one_size(survey_id: str, url: str):
            try:
                cached_size = self.pdf_sizes_cache.get(survey_id)
                if cached_size:
                    with self.pdf_size_lock:
                        self.pdf_size_updates[survey_id] = cached_size
                        self.pdf_size_checking.discard(survey_id)
                    return
                
                size_info = self.get_pdf_size(url, session)
                if size_info:
                    self.pdf_sizes_cache.set(survey_id, size_info)
                    with self.pdf_size_lock:
                        self.pdf_size_updates[survey_id] = size_info
                    log_debug(f"PDF size fetched for survey {survey_id}: {size_info.get('size_mb', 0):.2f} MB")
                else:
                    log_debug(f"Failed to fetch PDF size for survey {survey_id} from URL: {url[:80]}...")
                    # Don't store None - leave it unset so we can retry later if needed
                    with self.pdf_size_lock:
                        self.pdf_size_updates.pop(survey_id, None)
            except Exception as e:
                log_debug(f"Error fetching PDF size for survey {survey_id}: {e}")
                # Don't store None - leave it unset so we can retry later if needed
                with self.pdf_size_lock:
                    self.pdf_size_updates.pop(survey_id, None)
            finally:
                with self.pdf_size_lock:
                    self.pdf_size_checking.discard(survey_id)
        
        submitted = 0
        with self.pdf_size_lock:
            for survey_id, url in survey_urls.items():
                if url and survey_id not in self.pdf_size_checking:
                    self.pdf_size_checking.add(survey_id)
                    self.pdf_size_executor.submit(fetch_one_size, survey_id, url)
                    submitted += 1
        
        log_debug(f"[PDF] Submitted {submitted} PDF size checks to executor in {time.time() - pdf_start:.3f}s")
    
    def get_pdf_size_updates(self, survey_ids: set) -> Dict:
        """Get PDF size updates for the given survey IDs."""
        with self.pdf_size_lock:
            return {
                sid: self.pdf_size_updates[sid]
                for sid in survey_ids
                if sid in self.pdf_size_updates
            }
    
    def get_cached_pdf_sizes(self, survey_urls: Dict[str, str]) -> Dict:
        """Get PDF sizes from cache only (fast, synchronous)."""
        results = {}
        for survey_id, url in survey_urls.items():
            if url:
                cached_size = self.pdf_sizes_cache.get(survey_id)
                if cached_size:
                    results[survey_id] = cached_size
        return results
    
    @handle_errors(default_return={}, error_prefix="[PDF] ")
    def fetch_pdf_sizes_parallel(self, survey_urls: Dict[str, str], max_workers: int = 10) -> Dict:
        """Legacy synchronous method - kept for backwards compatibility."""
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        results = {}
        
        def fetch_one_size(survey_id: str, url: str):
            cached_size = self.pdf_sizes_cache.get(survey_id)
            if cached_size:
                return survey_id, cached_size
            
            size_info = self.get_pdf_size(url, session)
            if size_info:
                self.pdf_sizes_cache.set(survey_id, size_info)
                return survey_id, size_info
            return survey_id, None
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fetch_one_size, sid, url): sid 
                for sid, url in survey_urls.items() 
                if url
            }
            
            for future in as_completed(futures):
                try:
                    survey_id, size_info = future.result()
                    if size_info:
                        results[survey_id] = size_info
                except Exception as e:
                    log_debug(f"Error fetching PDF size: {e}")
        
        return results
    
    def clear_page_cache(self):
        with self.page_cache_lock:
            self.page_cache.clear()
    
    def update_page_cache(self, page_num: int, surveys: List[Dict]):
        with self.page_cache_lock:
            self.page_cache[page_num] = surveys

