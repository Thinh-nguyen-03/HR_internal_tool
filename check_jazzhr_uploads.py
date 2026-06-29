import os
import sys
import json
import requests
import time
from typing import Dict, List, Optional
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from security_utils import is_safe_url
except ImportError:
    # Fallback if security_utils not available (for backwards compatibility)
    def is_safe_url(url: str, verbose: bool = False):
        """Fallback - no validation"""
        return True, None

class JazzHRUploadChecker:
    def __init__(self, api_key: str, surveys_file: str = "culture_index_surveys.json"):
        self.api_key = api_key
        self.surveys_file = surveys_file
        self.base_url = "https://api.resumatorapi.com/v1"
        
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=3,
            pool_block=False
        )
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        self.session.headers.update({'Connection': 'keep-alive'})
        
        self.api_call_count = 0
        self.api_call_times = []
        self.search_times = []
        self.file_check_times = []
        
    def _make_request(self, endpoint: str, params: Optional[Dict] = None, verbose: bool = False) -> Optional[Dict]:
        url = f"{self.base_url}{endpoint}"
        
        if params is None:
            params = {}
        params['apikey'] = self.api_key
        
        from urllib.parse import urlencode
        clean_params = {k: v for k, v in params.items() if k != 'apikey'}
        full_url = f"{url}?{urlencode(params)}"
        
        if verbose:
            print(f"\n    [API] Endpoint: {endpoint}")
            print(f"    [API] Parameters: {clean_params}")
            print(f"    [API] Full URL: {full_url[:200]}..." if len(full_url) > 200 else f"    [API] Full URL: {full_url}")
        
        start_time = time.time()
        try:
            request_start = time.time()
            if verbose:
                print(f"    [API] Sending GET request at {time.strftime('%H:%M:%S')}...")
            
            connect_start = time.time()
            response = self.session.get(url, params=params, timeout=30)
            connect_time = time.time() - connect_start
            
            response_received = time.time()
            if verbose:
                print(f"    [API] Connection established: {connect_time:.3f}s")
                print(f"    [API] Response received in {response_received - request_start:.2f}s")
                print(f"    [API] Status code: {response.status_code}")
                content_length = response.headers.get('Content-Length', 'chunked')
                print(f"    [API] Response headers: Content-Type={response.headers.get('Content-Type', 'unknown')}, Content-Length={content_length}")
                if content_length == 'chunked':
                    print(f"    [API] Response is chunked (streaming)")
            
            response.raise_for_status()
            
            parse_start = time.time()
            result = response.json()
            parse_time = time.time() - parse_start
            
            elapsed = time.time() - start_time
            self.api_call_count += 1
            self.api_call_times.append(elapsed)
            
            if verbose:
                print(f"    [API] JSON parsed in {parse_time:.3f}s")
                print(f"    [API] Response type: {type(result).__name__}")
                if isinstance(result, list):
                    print(f"    [API] Response is a list with {len(result)} items")
                    if len(result) > 0:
                        print(f"    [API] First item keys: {list(result[0].keys()) if isinstance(result[0], dict) else 'N/A'}")
                        if isinstance(result[0], dict):
                            first_item_sample = {k: str(v)[:50] for k, v in list(result[0].items())[:5]}
                            print(f"    [API] First item sample: {first_item_sample}")
                elif isinstance(result, dict):
                    print(f"    [API] Response is a dict with keys: {list(result.keys())}")
                    if len(result) > 0:
                        first_key = list(result.keys())[0]
                        print(f"    [API] First key '{first_key}': {str(result[first_key])[:100]}")
                print(f"    [API] Total time: {elapsed:.2f}s")
            
            return result
        except requests.RequestException as e:
            elapsed = time.time() - start_time
            self.api_call_count += 1
            if verbose:
                print(f"\n    [ERROR] API request failed after {elapsed:.2f}s")
                print(f"    [ERROR] URL: {full_url[:200]}")
                print(f"    [ERROR] Exception: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        error_body = e.response.text[:500]
                        print(f"    [ERROR] Response body: {error_body}")
                    except:
                        pass
            return None
    
    def search_applicant_by_name(self, first_name: str, last_name: str, verbose: bool = False) -> Optional[Dict]:
        start_time = time.time()
        first_name = first_name.strip()
        last_name = last_name.strip()
        full_name = f"{first_name} {last_name}"
        
        if verbose:
            print(f"    [SEARCH] Searching for applicant: {full_name}")
        
        from urllib.parse import quote
        encoded_name = quote(full_name)
        endpoint = f"/applicants/name/{encoded_name}"
        
        search_start = time.time()
        response = self._make_request(endpoint, params=None, verbose=verbose)
        search_time = time.time() - search_start
        
        if verbose:
            print(f"    [SEARCH] Search API call completed in {search_time:.2f}s")
        
        if not response:
            elapsed = time.time() - start_time
            self.search_times.append(elapsed)
            if verbose:
                print(f"    [SEARCH] No response received")
            return None
        
        parse_start = time.time()
        applicants = response if isinstance(response, list) else [response] if response else []
        
        if verbose:
            print(f"    [SEARCH] API returned {len(applicants)} applicant(s)")
            if len(applicants) == 0:
                print(f"    [SEARCH] No applicant found in API response")
            elif len(applicants) == 1:
                applicant = applicants[0]
                app_first = applicant.get('first_name', '')
                app_last = applicant.get('last_name', '')
                applicant_id = applicant.get('id', 'unknown')
                print(f"    [SEARCH] Found applicant: '{app_first} {app_last}' (ID: {applicant_id})")
                if app_first.lower().strip() == first_name.lower().strip() and app_last.lower().strip() == last_name.lower().strip():
                    print(f"    [SEARCH] Exact name match confirmed")
                else:
                    print(f"    [SEARCH] WARNING: Name mismatch! Expected '{first_name} {last_name}', got '{app_first} {app_last}'")
            else:
                print(f"    [SEARCH] WARNING: API returned {len(applicants)} applicants (expected 1)")
                for idx, applicant in enumerate(applicants[:5]):
                    app_first = applicant.get('first_name', '')
                    app_last = applicant.get('last_name', '')
                    applicant_id = applicant.get('id', 'unknown')
                    print(f"    [SEARCH]   Applicant {idx+1}: '{app_first} {app_last}' (ID: {applicant_id})")
        
        elapsed = time.time() - start_time
        self.search_times.append(elapsed)
        
        if len(applicants) > 0:
            matched_applicant = applicants[0]
            if verbose:
                print(f"    [SEARCH] Total search time: {elapsed:.2f}s")
            return matched_applicant
        
        if verbose:
            print(f"    [SEARCH] No applicant found (total time: {elapsed:.2f}s)")
        return None
    
    def get_applicant_files(self, applicant_id: str, expected_name: str = None, verbose: bool = False) -> List[Dict]:
        if verbose:
            print(f"    [FILES] Fetching files for applicant: {applicant_id}")
            if expected_name:
                print(f"    [FILES] Expected applicant name: {expected_name}")
        
        endpoint = f"/files/applicant_id/{applicant_id}"
        
        files_start = time.time()
        response = self._make_request(endpoint, params=None, verbose=verbose)
        files_time = time.time() - files_start
        
        if verbose:
            print(f"    [FILES] Files API call completed in {files_time:.2f}s")
        
        if not response:
            if verbose:
                print(f"    [FILES] No response received")
            return []
        
        files = response if isinstance(response, list) else [response]
        
        if verbose:
            print(f"    [FILES] Raw API response: {len(files)} file(s)")
            print(f"    [FILES] Requested applicant_id: {applicant_id}")
            if len(files) > 0:
                print(f"    [FILES] Sample file structure (first file keys): {list(files[0].keys()) if isinstance(files[0], dict) else 'N/A'}")
        
        filtered_files = []
        mismatched_files = []
        for idx, file_data in enumerate(files):
            file_applicant_id = file_data.get('applicant_id', '')
            filename = file_data.get('filename', 'unknown')
            
            if verbose and idx < 10:
                print(f"    [FILES] File {idx+1}: '{filename}' -> applicant_id in response: '{file_applicant_id}' (matches: {file_applicant_id == applicant_id})")
            
            if file_applicant_id == applicant_id:
                filtered_files.append(file_data)
            else:
                mismatched_files.append(file_data)
        
        if verbose:
            print(f"    [FILES] Filtering results:")
            print(f"    [FILES]   Matching files: {len(filtered_files)}")
            print(f"    [FILES]   Mismatched files: {len(mismatched_files)}")
            if mismatched_files:
                print(f"    [FILES] WARNING: Found {len(mismatched_files)} files with different applicant_id!")
                print(f"    [FILES]   Expected applicant_id: {applicant_id}")
                for mf in mismatched_files[:5]:
                    mf_id = mf.get('applicant_id', 'MISSING')
                    mf_name = mf.get('filename', 'unknown')
                    print(f"    [FILES]   Mismatched: '{mf_name}' -> applicant_id: '{mf_id}'")
            print(f"    [FILES] Returning {len(filtered_files)} file(s) for applicant {applicant_id}")
            
            for idx, file_data in enumerate(filtered_files[:5]):
                filename = file_data.get('filename', 'unknown')
                file_size = file_data.get('file_size', 'unknown')
                file_app_id = file_data.get('applicant_id', 'unknown')
                print(f"    [FILES]   File {idx+1}: {filename} ({file_size} bytes, applicant_id: {file_app_id})")
            if len(filtered_files) > 5:
                print(f"    [FILES]   ... and {len(filtered_files) - 5} more files")
        
        return filtered_files
    
    def check_pdf_match(
        self, 
        survey_pdf_url: str, 
        survey_pdf_size: Optional[int], 
        jazzhr_files: List[Dict], 
        first_name: str = "", 
        last_name: str = "", 
        verbose: bool = False
    ) -> Optional[Dict]:
        """
        Match Culture Index PDF with JazzHR files using strict criteria.
        
        Matching Strategy (from most to least reliable):
        1. Exact filename match + size verification
        2. Full name pattern + size verification (REQUIRED)
        3. Size-only match (as fallback when filename unavailable)
        """
        start_time = time.time()
        
        if not survey_pdf_url:
            if verbose:
                print(f"    [MATCH] No PDF URL provided")
            return None
        
        ci_filename = survey_pdf_url.split('/')[-1] if survey_pdf_url else None
        
        if verbose:
            print(f"    [MATCH] CI filename: {ci_filename}")
            print(f"    [MATCH] CI file size: {survey_pdf_size} bytes")
            print(f"    [MATCH] Person: {first_name} {last_name}")
            print(f"    [MATCH] Checking {len(jazzhr_files)} JazzHR files...")
        
        ci_basename = None
        if ci_filename:
            ci_basename = ci_filename.replace('.pdf', '').split('_(')[0]
        
        first_initial = first_name[0].lower() if first_name else ''
        last_initial = last_name[0].lower() if last_name else ''
        full_name_clean = f"{first_name} {last_name}".lower() if first_name and last_name else ""
        
        if verbose and ci_basename:
            print(f"    [MATCH] CI basename: {ci_basename}")
            if first_initial and last_initial:
                print(f"    [MATCH] Initials: {first_initial.upper()}{last_initial.upper()}")
        
        for idx, file_data in enumerate(jazzhr_files):
            if verbose and idx < 5:
                jazz_filename = file_data.get('filename', 'unknown')
                jazz_size = file_data.get('file_size', 'unknown')
                print(f"    [MATCH] Checking file {idx+1}/{len(jazzhr_files)}: {jazz_filename} ({jazz_size} bytes)")
            
            jazz_filename = (file_data.get('filename') or '').strip()
            jazz_size = int(file_data.get('file_size', 0))
            jazz_lower = jazz_filename.lower()
            
            name_match = False
            match_type = None
            
            if ci_filename:
                ci_filename_lower = ci_filename.lower()
                if ci_filename_lower == jazz_lower or ci_filename_lower.replace('.pdf', '') == jazz_lower.replace('.pdf', ''):
                    name_match = True
                    match_type = 'exact_filename'
            
            survey_id = survey_pdf_url.split('/')[-1].split('_')[-1].replace('(', '').replace(')', '').replace('.pdf', '') if '(' in survey_pdf_url else None
            if not name_match and survey_id and first_name and last_name:
                expected_pattern = f"{first_name}_{last_name}_({survey_id})".lower().replace(' ', '_')
                if expected_pattern in jazz_lower.replace(' ', '_'):
                    name_match = True
                    match_type = 'uploaded_filename_pattern'
            
            # Strict full name matching (stronger than initials to avoid false positives)
            if not name_match and first_name and last_name and full_name_clean:
                # Check for full name in filename
                if full_name_clean in jazz_lower:
                    name_match = True
                    match_type = 'full_name'
                # Check for "FirstName_LastName" pattern
                elif f"{first_name.lower()}_{last_name.lower()}" in jazz_lower.replace(' ', '_'):
                    name_match = True
                    match_type = 'name_pattern'
            
            size_match = False
            if survey_pdf_size and jazz_size:
                size_diff = abs(jazz_size - survey_pdf_size)
                size_tolerance = max(2048, int(survey_pdf_size * 0.02))
                if jazz_size == survey_pdf_size or size_diff <= size_tolerance:
                    size_match = True
            
            # STRICT MATCHING: Require BOTH name AND size match for confirmation
            # Only exception: exact filename match (most reliable indicator)
            is_match = False
            if match_type == 'exact_filename' and name_match:
                # Exact filename match is highly reliable, size is optional but recommended
                if size_match or not survey_pdf_size:
                    is_match = True
            elif match_type in ['uploaded_filename_pattern', 'full_name', 'name_pattern']:
                # For other name matches, REQUIRE size verification to avoid false positives
                if size_match and survey_pdf_size:
                    is_match = True
            elif size_match and survey_pdf_size:
                # Size-only match acceptable if very close (within tolerance)
                is_match = True
            
            if is_match:
                elapsed = time.time() - start_time
                self.file_check_times.append(elapsed)
                
                if name_match and size_match:
                    matched_by = f'name ({match_type}) and size'
                elif size_match:
                    matched_by = 'size (exact bytes)'
                else:
                    matched_by = f'name ({match_type})'
                
                if verbose:
                    print(f"    [MATCH] MATCH FOUND! Matched by: {matched_by}")
                    print(f"    [MATCH] Matching file: {file_data.get('filename')}")
                    print(f"    [MATCH] Matching took {elapsed:.3f}s")
                
                return {
                    'file': file_data,
                    'matched_by': matched_by,
                    'match_type': match_type,
                    'name_match': name_match,
                    'size_match': size_match
                }
        
        elapsed = time.time() - start_time
        self.file_check_times.append(elapsed)
        if verbose:
            print(f"    [MATCH] No match found after checking {len(jazzhr_files)} files ({elapsed:.3f}s)")
        return None
    
    def upload_file_to_applicant(
        self, 
        applicant_id: str, 
        pdf_url: str, 
        first_name: str, 
        last_name: str,
        verbose: bool = False
    ) -> Dict:
        import base64
        
        # SECURITY: Validate URL before downloading (SSRF protection)
        is_safe, error_msg = is_safe_url(pdf_url, verbose=verbose)
        if not is_safe:
            error = f"Unsafe URL blocked: {error_msg}"
            if verbose:
                print(f"    [UPLOAD] {error}")
            return {"success": False, "error": error}
        
        try:
            if verbose:
                print(f"    [UPLOAD] Downloading PDF from {pdf_url[:60]}...")
            
            resp = self.session.get(pdf_url, timeout=30)
            resp.raise_for_status()
            pdf_content = resp.content
            
            if verbose:
                print(f"    [UPLOAD] Downloaded {len(pdf_content)} bytes")
        except Exception as e:
            return {"success": False, "error": f"Failed to download PDF: {e}"}
        
        file_data = base64.b64encode(pdf_content).decode('utf-8')
        
        safe_first = ''.join(c for c in first_name if c.isalnum() or c in ' -_').strip().replace(' ', '_')
        safe_last = ''.join(c for c in last_name if c.isalnum() or c in ' -_').strip().replace(' ', '_')
        filename = f"{safe_first}_{safe_last}_CultureIndex.pdf"
        
        try:
            if not self.api_key:
                return {"success": False, "error": "API key is None or empty in JazzHRUploadChecker"}
            
            url = f"{self.base_url}/files"
            
            payload = {
                "apikey": self.api_key,
                "applicant_id": applicant_id,
                "filename": filename,
                "file_data": file_data,
                "file_privacy": "0"
            }
            
            if verbose:
                print(f"    [UPLOAD] Uploading {filename} to applicant {applicant_id}...")
                print(f"    [UPLOAD] URL: {url}")
                print(f"    [UPLOAD] Sending as form data with keys: {list(payload.keys())}")

            resp = self.session.post(url, data=payload, timeout=60)
            resp.raise_for_status()
            
            result = resp.json()
            
            if verbose:
                print(f"    [UPLOAD] Response: {result}")
            
            if isinstance(result, str):
                if result.lower().startswith("error"):
                    return {"success": False, "error": result}
                return {"success": True, "message": result, "filename": filename}
            
            if isinstance(result, dict) and ("error" in result or "_error" in result):
                error_msg = result.get("error") or result.get("_error", str(result))
                return {"success": False, "error": error_msg}
            
            return {
                "success": True, 
                "message": f"Uploaded {filename}",
                "file_id": result.get("id") if isinstance(result, dict) else None,
                "filename": filename
            }
            
        except requests.RequestException as e:
            error_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_msg = e.response.text[:200]
                except:
                    pass
            return {"success": False, "error": f"Upload failed: {error_msg}"}
    
    def check_all_surveys(self, start_idx: Optional[int] = None, end_idx: Optional[int] = None) -> Dict:
        overall_start_time = time.time()
        
        print("Loading surveys...")
        load_start = time.time()
        if not os.path.exists(self.surveys_file):
            print(f"ERROR: Survey file not found: {self.surveys_file}")
            return {"error": "Survey file not found"}
        
        try:
            with open(self.surveys_file, 'r', encoding='utf-8') as f:
                all_surveys = json.load(f)
        except json.JSONDecodeError as e:
            print(f"ERROR: JSON file is corrupted or invalid")
            print(f"ERROR: {e}")
            print(f"ERROR: Error at line {e.lineno}, column {e.colno}")
            
            try:
                with open(self.surveys_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    error_line = e.lineno - 1
                    start_line = max(0, error_line - 3)
                    end_line = min(len(lines), error_line + 4)
                    print(f"\nContext around error (lines {start_line+1}-{end_line}):")
                    for i in range(start_line, end_line):
                        marker = ">>> " if i == error_line else "    "
                        print(f"{marker}{i+1}: {lines[i].rstrip()}")
            except Exception as read_err:
                print(f"Could not read file for context: {read_err}")
            
            return {"error": f"JSON decode error: {e}"}
        except Exception as e:
            print(f"ERROR: Failed to load surveys file: {e}")
            return {"error": str(e)}
        
        load_time = time.time() - load_start
        print(f"Loaded surveys in {load_time:.2f}s")
        
        total_count = len(all_surveys)
        start = start_idx if start_idx is not None else 0
        end = end_idx if end_idx is not None else total_count
        
        if start < 0:
            start = 0
        if end > total_count:
            end = total_count
        if start >= end:
            print(f"ERROR: Invalid range. Start ({start}) must be less than end ({end})")
            return {"error": "Invalid range"}
        
        surveys = all_surveys[start:end]
        
        range_info = ""
        if start_idx is not None or end_idx is not None:
            range_info = f" (surveys {start+1}-{end} of {total_count})"
        
        print(f"Loaded {len(all_surveys)} total surveys")
        print(f"Checking {len(surveys)} surveys{range_info}")
        print("\nChecking JazzHR for uploaded Culture Index PDFs...\n")
        
        stats = {
            'total': len(all_surveys),
            'checked_in_range': len(surveys),
            'checked': 0,
            'found_in_jazzhr': 0,
            'pdf_uploaded': 0,
            'pdf_not_uploaded': 0,
            'not_found_in_jazzhr': 0,
            'no_pdf_url': 0,
            'range_start': start + 1,
            'range_end': end
        }
        
        processing_start_time = time.time()
        
        for idx, survey in enumerate(surveys, 1):
            survey_start_time = time.time()
            actual_idx = start + idx
            first_name = survey.get('firstName', '').strip()
            last_name = survey.get('lastName', '').strip()
            survey_id = survey.get('surveyId')
            pdf_url = survey.get('surveyReportUrl')
            pdf_size = survey.get('pdfSize')
            
            if not first_name or not last_name:
                survey['jazzhrStatus'] = 'MISSING_NAME'
                continue
            
            if not pdf_url:
                survey['jazzhrStatus'] = 'NO_PDF_URL'
                stats['no_pdf_url'] += 1
                continue
            
            stats['checked'] += 1
            
            print(f"[{actual_idx}/{total_count}] Checking {first_name} {last_name}...")
            print(f"  [STEP 1] Starting check at {time.strftime('%H:%M:%S')}")
            
            search_start = time.time()
            applicant = self.search_applicant_by_name(first_name, last_name, verbose=True)
            search_elapsed = time.time() - search_start
            
            if not applicant:
                elapsed = time.time() - survey_start_time
                print(f"  [RESULT] Not found in JazzHR")
                print(f"  [TIMING] Search: {search_elapsed:.2f}s | Total: {elapsed:.2f}s\n")
                survey['jazzhrStatus'] = 'NOT_IN_JAZZHR'
                survey['jazzhrApplicantId'] = None
                stats['not_found_in_jazzhr'] += 1
                continue
            
            applicant_id = applicant.get('id')
            applicant_name = f"{applicant.get('first_name', '')} {applicant.get('last_name', '')}"
            stats['found_in_jazzhr'] += 1
            print(f"  [STEP 2] Applicant found: {applicant_id} ({applicant_name})")
            
            files_start = time.time()
            files = self.get_applicant_files(applicant_id, expected_name=applicant_name, verbose=True)
            files_elapsed = time.time() - files_start
            
            if not files:
                elapsed = time.time() - survey_start_time
                print(f"  [RESULT] Found in JazzHR, but no files")
                print(f"  [TIMING] Search: {search_elapsed:.2f}s | Files: {files_elapsed:.2f}s | Total: {elapsed:.2f}s\n")
                survey['jazzhrStatus'] = 'NO_FILES'
                survey['jazzhrApplicantId'] = applicant_id
                survey['jazzhrFileCount'] = 0
                stats['pdf_not_uploaded'] += 1
                continue
            
            print(f"  [STEP 3] Checking PDF match...")
            match_start = time.time()
            match = self.check_pdf_match(pdf_url, pdf_size, files, first_name, last_name, verbose=True)
            match_elapsed = time.time() - match_start
            
            elapsed = time.time() - survey_start_time
            if match:
                print(f"  [RESULT] PDF UPLOADED (matched by {match['matched_by']})")
                print(f"  [TIMING] Search: {search_elapsed:.2f}s | Files: {files_elapsed:.2f}s | Match: {match_elapsed:.3f}s | Total: {elapsed:.2f}s\n")
                survey['jazzhrStatus'] = 'CULTURE INDEX COMPLETE'
                survey['jazzhrApplicantId'] = applicant_id
                survey['jazzhrFileCount'] = len(files)
                survey['jazzhrMatchedFile'] = {
                    'filename': match['file'].get('filename'),
                    'file_id': match['file'].get('id'),
                    'file_size': match['file'].get('file_size'),
                    'date_loaded': match['file'].get('date_loaded'),
                    'matched_by': match['matched_by']
                }
                stats['pdf_uploaded'] += 1
            else:
                print(f"  [RESULT] Found in JazzHR, but PDF not uploaded ({len(files)} files)")
                print(f"  [TIMING] Search: {search_elapsed:.2f}s | Files: {files_elapsed:.2f}s | Match: {match_elapsed:.3f}s | Total: {elapsed:.2f}s\n")
                survey['jazzhrStatus'] = 'PDF_NOT_UPLOADED'
                survey['jazzhrApplicantId'] = applicant_id
                survey['jazzhrFileCount'] = len(files)
                stats['pdf_not_uploaded'] += 1
            
            if idx % 10 == 0:
                time.sleep(0.5)
            
            if idx % 50 == 0:
                elapsed_so_far = time.time() - processing_start_time
                avg_per_survey = elapsed_so_far / idx
                remaining = len(surveys) - idx
                estimated_remaining = avg_per_survey * remaining
                print(f"\n  [PROGRESS] Processed {idx}/{len(surveys)} surveys in {elapsed_so_far:.1f}s")
                print(f"  [PROGRESS] Average: {avg_per_survey:.2f}s per survey")
                print(f"  [PROGRESS] Estimated time remaining: {estimated_remaining/60:.1f} minutes\n")
        
        processing_time = time.time() - processing_start_time
        
        print(f"\nSaving updated surveys to {self.surveys_file}...")
        save_start = time.time()
        with open(self.surveys_file, 'w', encoding='utf-8') as f:
            json.dump(all_surveys, f, indent=2, ensure_ascii=False)
        save_time = time.time() - save_start
        print(f"Saved in {save_time:.2f}s")
        
        overall_time = time.time() - overall_start_time
        
        stats['performance'] = {
            'total_time': overall_time,
            'load_time': load_time,
            'processing_time': processing_time,
            'save_time': save_time,
            'api_calls': self.api_call_count,
            'avg_api_call_time': sum(self.api_call_times) / len(self.api_call_times) if self.api_call_times else 0,
            'max_api_call_time': max(self.api_call_times) if self.api_call_times else 0,
            'min_api_call_time': min(self.api_call_times) if self.api_call_times else 0,
            'avg_search_time': sum(self.search_times) / len(self.search_times) if self.search_times else 0,
            'avg_file_check_time': sum(self.file_check_times) / len(self.file_check_times) if self.file_check_times else 0,
            'avg_per_survey': processing_time / len(surveys) if len(surveys) > 0 else 0
        }
        
        print("\nDone!")
        return stats

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Check if Culture Index PDFs have been uploaded to JazzHR',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check all surveys
  python check_jazzhr_uploads.py
  
  # Check 150 surveys starting from survey 1
  python check_jazzhr_uploads.py --start 1 --limit 150
  
  # Check surveys 250-400
  python check_jazzhr_uploads.py --start 250 --end 400
  
  # Check 300 surveys starting from survey 500
  python check_jazzhr_uploads.py --start 500 --limit 300
  
  # Check from survey 100 to the end
  python check_jazzhr_uploads.py --start 100

Note: Survey numbers use 1-based indexing (survey 1 is the first survey)
        """
    )
    parser.add_argument('--file', default='culture_index_surveys.json', 
                       help='Path to surveys JSON file')
    parser.add_argument('--api-key', help='JazzHR API key (or set JAZZHR_API_KEY env var)')
    parser.add_argument('--start', type=int, metavar='N',
                       help='Start at survey N (1-based indexing, default: 1)')
    parser.add_argument('--end', type=int, metavar='N',
                       help='End at survey N (1-based indexing, exclusive)')
    parser.add_argument('--limit', type=int, metavar='N',
                       help='Check N surveys from start position')
    
    args = parser.parse_args()
    
    api_key = args.api_key or os.getenv('JAZZHR_API_KEY')
    
    if not api_key:
        print("ERROR: JazzHR API key not provided")
        print("Set JAZZHR_API_KEY environment variable or use --api-key")
        return 1
    
    if args.end is not None and args.limit is not None:
        print("ERROR: Cannot use both --end and --limit at the same time")
        return 1
    
    start_idx = None
    end_idx = None
    
    if args.start is not None:
        if args.start < 1:
            print("ERROR: --start must be >= 1")
            return 1
        start_idx = args.start - 1
        
        if args.limit is not None:
            if args.limit <= 0:
                print("ERROR: --limit must be > 0")
                return 1
            end_idx = start_idx + args.limit
        elif args.end is not None:
            if args.end <= args.start:
                print("ERROR: --end must be greater than --start")
                return 1
            end_idx = args.end      
    elif args.end is not None:
        if args.end < 1:
            print("ERROR: --end must be >= 1")
            return 1
        start_idx = 0
        end_idx = args.end
    elif args.limit is not None:
        if args.limit <= 0:
            print("ERROR: --limit must be > 0")
            return 1
        start_idx = 0
        end_idx = args.limit
    
    checker = JazzHRUploadChecker(api_key=api_key, surveys_file=args.file)
    stats = checker.check_all_surveys(start_idx=start_idx, end_idx=end_idx)
    
    if 'error' in stats:
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
