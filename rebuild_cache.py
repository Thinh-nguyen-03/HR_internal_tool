import os
import sys
import json
import time
import re
import requests
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict

# Load environment
load_dotenv()

# Import app modules
from cultureindex_client_1 import CultureIndexClient
from check_jazzhr_uploads import JazzHRUploadChecker
from cache_storage_1 import create_cache, SmartJazzHRCache, CACHE_ENTRY_VERSION

# Configuration
CLIENT_ID = os.getenv('CLIENT_ID', 'A89F5B0000')
JAZZHR_API_KEY = os.getenv('JAZZHR_API_KEY')
JAZZHR_CACHE_HOURS = int(os.getenv('JAZZHR_CACHE_HOURS', '2'))
RECENT_SURVEY_THRESHOLD = int(os.getenv('RECENT_SURVEY_THRESHOLD', '1000'))
BATCH_SIZE = 50
PDF_FETCH_TIMEOUT = int(os.getenv('PDF_FETCH_TIMEOUT', '5'))

def log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")

def parse_csv_date(date_str):
    """Parse CSV date field."""
    if not date_str or not date_str.strip():
        return None
    return date_str.strip()

def extract_survey_id_from_url(url):
    """Extract survey ID from PDF URL if present.
    Pattern: .../Name_(ID).pdf
    """
    if not url:
        return None
    # Look for pattern like (12345678) or _(12345678).pdf
    match = re.search(r'\((\d+)\)', url)
    if match:
        return match.group(1)
    return None

def parse_csv_surveys(csv_data):
    """Parse CSV survey data to extract surveys with PDF URLs."""
    import csv
    from io import StringIO
    
    surveys = []
    try:
        csv_reader = csv.DictReader(StringIO(csv_data))
        
        col_map = {}
        if csv_reader.fieldnames:
            for col in csv_reader.fieldnames:
                col_lower = col.lower().replace(' ', '').replace('_', '')
                col_map[col_lower] = col
        
        def get_val(preferred_key):
            key_variants = [preferred_key, preferred_key.replace(' ', ''), preferred_key.replace(' ', '_')]
            for variant in key_variants:
                variant_lower = variant.lower().replace(' ', '').replace('_', '')
                if variant_lower in col_map:
                    return row.get(col_map[variant_lower], '').strip()
            return ''
        
        for row in csv_reader:
            survey_id = get_val('Survey ID') or get_val('SurveyID')
            survey_url = get_val('Survey Report URL') or get_val('SurveyReportURL')
            
            # If survey ID is missing, try to extract it from the PDF URL
            if not survey_id and survey_url:
                survey_id = extract_survey_id_from_url(survey_url)
            
            surveys.append({
                "surveyId": survey_id,
                "firstName": get_val('First Name') or get_val('FirstName'),
                "lastName": get_val('Last Name') or get_val('LastName'),
                "email": get_val('Email Address') or get_val('EmailAddress') or get_val('Email'),
                "phoneNumber": get_val('Phone Number') or get_val('PhoneNumber'),
                "traitPattern": get_val('Trait Pattern') or get_val('TraitPattern'),
                "surveyDate": parse_csv_date(get_val('Survey Date') or get_val('SurveyDate')),
                "position": get_val('Positions Applied To') or get_val('PositionsAppliedTo') or get_val('Position'),
                "surveyReportUrl": survey_url,
            })
    except Exception as e:
        print(f"ERROR parsing CSV: {e}")
    
    return surveys

def check_one_survey_jazzhr(checker, survey, pdf_url, pdf_size):
    """Check one survey's JazzHR status.
    Returns cache format matching app.py SimpleJazzHRService.check_one_survey()
    """
    first_name = survey.get('firstName', '')
    last_name = survey.get('lastName', '')
    survey_id = str(survey.get('surveyId', ''))
    
    if not first_name or not last_name:
        return {"status": "MISSING_NAME", "isUploaded": False}
    
    if not pdf_url:
        return {"status": "NO_PDF_URL", "isUploaded": False}
    
    try:
        # Search for applicant
        applicant = checker.search_applicant_by_name(first_name, last_name)
        
        if not applicant:
            return {"status": "NOT_IN_JAZZHR", "isUploaded": False}
        
        applicant_id = applicant.get('id')
        
        # Get applicant files
        files = checker.get_applicant_files(applicant_id)
        
        # Check for PDF match
        match = checker.check_pdf_match(
            survey_pdf_url=pdf_url,
            survey_pdf_size=pdf_size,
            jazzhr_files=files,
            first_name=first_name,
            last_name=last_name
        )
        
        if match:
            return {
                "status": "UPLOADED",
                "applicantId": applicant_id,
                "isUploaded": True,
                "match": match,
                "file_count": len(files),
                "had_pdf_size": pdf_size is not None
            }
        else:
            return {
                "status": "NOT_UPLOADED",
                "applicantId": applicant_id,
                "isUploaded": False,
                "file_count": len(files),
                "had_pdf_size": pdf_size is not None
            }
        
    except Exception as e:
        log(f"JazzHR error for {survey_id}: {e}", "ERROR")
        return {"status": "ERROR", "isUploaded": False, "error": str(e)}

def fetch_pdf_sizes(urls: Dict[str, str], pdf_cache, max_workers: int = 8) -> Dict[str, int]:
    """Fetch PDF sizes for multiple URLs in parallel."""
    sizes = {}
    
    def fetch_size(survey_id, url):
        try:
            cached = pdf_cache.get(survey_id)
            if cached and 'size' in cached:
                return survey_id, cached['size']
            
            response = requests.head(url, timeout=PDF_FETCH_TIMEOUT, allow_redirects=True)
            size = int(response.headers.get('Content-Length', 0))
            
            if size > 0:
                pdf_cache.set(survey_id, {'size': size})
            
            return survey_id, size
        except:
            return survey_id, None
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_size, sid, url): sid for sid, url in urls.items()}
        for future in as_completed(futures):
            try:
                survey_id, size = future.result()
                if size:
                    sizes[survey_id] = size
            except:
                pass
    
    return sizes

def main():
    print("JazzHR Cache Rebuild Script")
    print()
    
    if not JAZZHR_API_KEY:
        print("ERROR: JAZZHR_API_KEY not found in environment!")
        sys.exit(1)
    
    # Step 1: Initialize caches
    log("Initializing cache backends...")
    jazzhr_cache_backend = create_cache(
        "jazzhr_status",
        ttl_hours=JAZZHR_CACHE_HOURS,
        cache_file="jazzhr_status_cache.json"
    )
    pdf_size_cache = create_cache(
        "pdf_sizes",
        ttl_hours=168,
        cache_file="pdf_sizes_cache.json"
    )
    jazzhr_cache = SmartJazzHRCache(jazzhr_cache_backend, recent_threshold=RECENT_SURVEY_THRESHOLD)
    
    # Step 2: Ask if user wants to clear cache or resume
    print()
    print("Cache Options:")
    print("  1. Clear all cache and start fresh")
    print("  2. Resume - skip already cached surveys (recommended if interrupted)")
    print()
    choice = input("Choose option (1 or 2, default=2): ").strip()
    
    if choice == "1":
        log("Clearing existing cache entries...")
        try:
            # Clear JazzHR cache
            if hasattr(jazzhr_cache_backend, '_redis') and jazzhr_cache_backend._redis:
                # Redis backend - delete all jazzhr_status keys
                pattern = jazzhr_cache_backend._key('*')
                redis_client = jazzhr_cache_backend._redis
                deleted_jazzhr = 0
                log("  Clearing JazzHR cache entries...")
                for key in redis_client.scan_iter(match=pattern, count=100):
                    redis_client.delete(key)
                    deleted_jazzhr += 1
                    if deleted_jazzhr % 500 == 0:
                        log(f"  Deleted {deleted_jazzhr} JazzHR entries...")
                log(f"Cleared {deleted_jazzhr} JazzHR cache entries from Redis")
                
                # Also clear PDF sizes cache
                pdf_pattern = pdf_size_cache._key('*')
                deleted_pdf = 0
                log("  Clearing PDF size cache entries...")
                for key in redis_client.scan_iter(match=pdf_pattern, count=100):
                    redis_client.delete(key)
                    deleted_pdf += 1
                    if deleted_pdf % 500 == 0:
                        log(f"  Deleted {deleted_pdf} PDF entries...")
                log(f"Cleared {deleted_pdf} PDF size cache entries from Redis")
            else:
                # File backend - clear in-memory caches and save
                jazzhr_cache_backend._cache = {}
                jazzhr_cache_backend.save()
                pdf_size_cache._cache = {}
                pdf_size_cache.save()
                log(f"Cleared file-based caches")
        except Exception as e:
            log(f"Warning: Could not clear all caches: {e}")
            import traceback
            traceback.print_exc()
    else:
        log("Resume mode: Will skip already cached surveys")
    
    # Step 3: Fetch all surveys from Culture Index
    log("Connecting to Culture Index...")
    ci_client = CultureIndexClient()
    
    # Authenticate with Culture Index
    log("Authenticating with Culture Index...")
    ci_username = os.getenv('CULTUREINDEX_EMAIL')
    ci_password = os.getenv('CULTUREINDEX_PASSWORD')
    
    if not ci_username or not ci_password:
        print("ERROR: CULTUREINDEX_EMAIL and CULTUREINDEX_PASSWORD must be set in .env file!")
        sys.exit(1)
    
    try:
        ci_client.login(ci_username, ci_password)
        log("Authentication successful")
    except Exception as e:
        print(f"ERROR: Failed to authenticate with Culture Index: {e}")
        sys.exit(1)
    
    log("Fetching all surveys via CSV export (this may take 30-60 seconds)...")
    start_time = time.time()
    
    # Use CSV export like the main app does (includes PDF URLs)
    csv_data = ci_client.export_surveys_csv(client_id=CLIENT_ID)
    csv_time = time.time() - start_time
    log(f"CSV downloaded in {csv_time:.1f}s, size: {len(csv_data)} bytes")
    
    # Parse CSV
    log("Parsing CSV data...")
    parse_start = time.time()
    surveys = parse_csv_surveys(csv_data)
    parse_time = time.time() - parse_start
    
    if not surveys:
        log("ERROR: No surveys found in CSV!")
        sys.exit(1)
    
    log(f"Found {len(surveys)} surveys (parsed in {parse_time:.1f}s)")
    
    # Debug: Show first survey structure
    if surveys:
        first = surveys[0]
        log(f"Sample survey fields: surveyId={first.get('surveyId')}, firstName={first.get('firstName')}, lastName={first.get('lastName')}, surveyReportUrl={first.get('surveyReportUrl')[:50] if first.get('surveyReportUrl') else 'None'}...")
    print()
    
    # Step 4: Initialize JazzHR checker
    log("Initializing JazzHR checker...")
    jazzhr_checker = JazzHRUploadChecker(JAZZHR_API_KEY)
    
    # Step 5: Check which surveys are already cached (if resuming)
    if choice != "1":
        log("Checking which surveys are already cached...")
        cached_survey_ids = set()
        for survey in surveys:
            survey_id = str(survey.get('surveyId', '')).strip()
            if survey_id:
                cached = jazzhr_cache.get(survey_id)
                if cached and cached.get('_cache_version') == CACHE_ENTRY_VERSION:
                    cached_survey_ids.add(survey_id)
        
        already_cached = len(cached_survey_ids)
        need_processing = len(surveys) - already_cached
        log(f"Found {already_cached} already cached, {need_processing} need processing")
        print()
    else:
        cached_survey_ids = set()
        need_processing = len(surveys)
    
    # Step 6: Process surveys in batches
    log(f"Processing {need_processing} surveys in batches of {BATCH_SIZE}...")
    print()
    
    total_processed = 0
    total_cached = 0
    total_skipped = 0
    total_errors = 0
    batch_count = (len(surveys) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for batch_num in range(batch_count):
        start_idx = batch_num * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, len(surveys))
        batch = surveys[start_idx:end_idx]
        
        log(f"Batch {batch_num + 1}/{batch_count}: Processing surveys {start_idx + 1}-{end_idx}...")
        
        batch_start = time.time()
        
        # Get PDF sizes for batch
        urls = {str(s['surveyId']): s.get('surveyReportUrl') for s in batch}
        urls_to_check = {sid: url for sid, url in urls.items() if url}
        pdf_sizes = fetch_pdf_sizes(urls_to_check, pdf_size_cache)
        
        # Check each survey
        for survey in batch:
            survey_id = str(survey.get('surveyId', '')).strip()
            
            # Skip surveys without ID
            if not survey_id:
                log(f"  WARNING: Survey missing ID, skipping: {survey}")
                total_errors += 1
                total_processed += 1
                continue
            
            # Skip if already cached (resume mode)
            if survey_id in cached_survey_ids:
                total_skipped += 1
                total_processed += 1
                continue
            
            url = survey.get('surveyReportUrl')
            pdf_size = pdf_sizes.get(survey_id)
            
            try:
                # Check JazzHR status
                result = check_one_survey_jazzhr(jazzhr_checker, survey, url, pdf_size)
                
                # Cache the result with survey ID as key
                jazzhr_cache.set(survey_id, result)
                total_cached += 1
                
            except Exception as e:
                log(f"  Error checking survey {survey_id}: {e}")
                total_errors += 1
            
            total_processed += 1
        
        batch_time = time.time() - batch_start
        log(f"  Completed in {batch_time:.1f}s ({len(batch)/batch_time:.1f} surveys/sec)")
        
        # Small delay between batches to avoid rate limiting
        if batch_num < batch_count - 1:
            time.sleep(1)
    
    # Step 7: Summary
    print()
    print("CACHE REBUILD COMPLETE")
    log(f"Total surveys processed: {total_processed}")
    if total_skipped > 0:
        log(f"Skipped (already cached): {total_skipped}")
    log(f"Successfully cached: {total_cached}")
    log(f"Errors: {total_errors}")
    if total_processed > 0:
        log(f"Success rate: {(total_cached/total_processed*100):.1f}%")
    print()
    log("Cache is now ready! You can restart the main application.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user.")
        print("Progress has been saved. Run the script again and choose option 2 (Resume) to continue.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

