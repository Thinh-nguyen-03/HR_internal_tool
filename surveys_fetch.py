import os
import sys
import json
import csv
import re
import requests
from io import StringIO
from datetime import datetime
from typing import List, Dict, Any, Optional
from cultureindex_client_1 import CultureIndexClient, CultureIndexAuthError

try:
    import phonenumbers
    from phonenumbers import NumberParseException
    PHONENUMBERS_AVAILABLE = True
except ImportError:
    PHONENUMBERS_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo  # pyright: ignore[reportMissingImports]
    except ImportError:
        import pytz
        ZoneInfo = pytz.timezone

def format_phone_number(phone_str: Optional[str]) -> Optional[str]:
    if not phone_str or not phone_str.strip():
        return phone_str
    
    if not PHONENUMBERS_AVAILABLE:
        return phone_str
    
    try:
        parsed = phonenumbers.parse(phone_str, "US")
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except NumberParseException:
        pass
    
    try:
        parsed = phonenumbers.parse(phone_str, None)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except NumberParseException:
        pass
    
    return phone_str

def convert_to_central_date(date_str: Optional[str]) -> Optional[str]:
    if not date_str:
        return None
    
    try:
        if date_str.endswith('Z'):
            date_str = date_str.replace('Z', '+00:00')
        
        dt = datetime.fromisoformat(date_str)
        
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            except (TypeError, AttributeError):
                import pytz
                dt = pytz.UTC.localize(dt)
        
        try:
            central_tz = ZoneInfo("America/Chicago")
            central_dt = dt.astimezone(central_tz)
        except (TypeError, AttributeError):
            import pytz
            central_tz = pytz.timezone("America/Chicago")
            central_dt = dt.astimezone(central_tz)
        
        return central_dt.strftime("%m/%d/%Y")
    except (ValueError, AttributeError, TypeError):
        return None

def get_pdf_size(pdf_url: str, session: requests.Session) -> Optional[Dict[str, Any]]:
    if not pdf_url or not pdf_url.strip():
        return None
    
    try:
        headers = {'Range': 'bytes=0-0'}
        response = session.get(pdf_url, headers=headers, allow_redirects=True, timeout=10, stream=True)
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
        
        return {
            'size': file_size,
            'size_kb': file_size / 1024,
            'size_mb': file_size / (1024 * 1024)
        }
    except Exception:
        return None

def get_survey_urls_from_csv(csv_data: str) -> Dict[str, str]:
    survey_urls = {}
    
    try:
        csv_reader = csv.DictReader(StringIO(csv_data))
        
        survey_id_col = None
        survey_url_col = None
        
        if csv_reader.fieldnames:
            for col in csv_reader.fieldnames:
                clean_col = col.replace('ï»¿', '').replace('"', '').strip()
                if 'Survey Id' in clean_col or 'SurveyId' in clean_col:
                    survey_id_col = col
                if 'Survey Report URL' in clean_col or 'SurveyReportURL' in clean_col:
                    survey_url_col = col
        
        if not survey_id_col or not survey_url_col:
            return {}
        
        for row in csv_reader:
            survey_id = row.get(survey_id_col, '').strip()
            survey_url = row.get(survey_url_col, '').strip()
            
            if survey_id and survey_url:
                survey_urls[survey_id] = survey_url
    except Exception:
        pass
    
    return survey_urls

def extract_survey_info(
    survey: Dict[str, Any],
    survey_url: Optional[str] = None,
    pdf_size_info: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "surveyId": survey.get("surveyId"),
        "traitPattern": survey.get("traitPattern"),
        "surveyTakerId": survey.get("surveyTakerId"),
        "firstName": survey.get("firstName"),
        "lastName": survey.get("lastName"),
        "email": survey.get("email"),
        "phoneNumber": format_phone_number(survey.get("phoneNumber")),
        "surveyDate": convert_to_central_date(survey.get("surveyDate")),
        "surveyReportUrl": survey_url,
        "pdfSize": pdf_size_info.get('size') if pdf_size_info else None,
        "pdfSizeKB": pdf_size_info.get('size_kb') if pdf_size_info else None,
        "pdfSizeMB": pdf_size_info.get('size_mb') if pdf_size_info else None,
    }

def fetch_all_surveys():
    email = os.getenv("CULTUREINDEX_EMAIL")
    password = os.getenv("CULTUREINDEX_PASSWORD")
    client_id = "A89F5B0000"
    
    if not email or not password:
        print("ERROR: Set CULTUREINDEX_EMAIL and CULTUREINDEX_PASSWORD environment variables")
        return 1
    
    client = CultureIndexClient()
    
    try:
        print("Step 1: Logging in...")
        client.login(email=email, password=password)
        print("✓ Login successful")
        
        print("\nStep 2: Fetching all surveys...")
        all_surveys = client.get_all_surveys(client_id=client_id)
        print(f"✓ Fetched {len(all_surveys)} surveys")
        
        print("\nStep 3: Fetching survey URLs from CSV export...")
        csv_data = client.export_surveys_csv(client_id=client_id)
        survey_urls = get_survey_urls_from_csv(csv_data)
        print(f"✓ Extracted {len(survey_urls)} survey URLs")
        
        print("\nStep 4: Checking PDF file sizes...")
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        filtered_surveys = []
        for idx, survey in enumerate(all_surveys, 1):
            survey_id = str(survey.get('surveyId', ''))
            survey_url = survey_urls.get(survey_id)
            
            pdf_size_info = None
            if survey_url:
                pdf_size_info = get_pdf_size(survey_url, session)
            
            filtered_surveys.append(extract_survey_info(survey, survey_url, pdf_size_info))
            
            if idx % 100 == 0:
                print(f"  Processed {idx}/{len(all_surveys)} surveys...")
        
        print(f"✓ Processed all {len(filtered_surveys)} surveys")
        
        output_file = "culture_index_surveys.json"
        print(f"\nStep 5: Saving to {output_file}...")
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(filtered_surveys, f, indent=2, ensure_ascii=False)
        
        print(f"✓ Saved to {output_file}")
        
        surveys_with_urls = sum(1 for s in filtered_surveys if s.get('surveyReportUrl'))
        surveys_with_sizes = sum(1 for s in filtered_surveys if s.get('pdfSize'))
        
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"Total surveys: {len(filtered_surveys)}")
        print(f"Surveys with URLs: {surveys_with_urls}")
        print(f"Surveys with PDF sizes: {surveys_with_sizes}")
        
        trait_patterns = set(s.get('traitPattern') for s in filtered_surveys if s.get('traitPattern'))
        if trait_patterns:
            print(f"Trait patterns: {', '.join(sorted(trait_patterns)[:10])}...")
        
        return 0
        
    except CultureIndexAuthError as e:
        print(f"Authentication error: {e}")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(fetch_all_surveys())
