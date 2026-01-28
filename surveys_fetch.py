import os
import sys
import json
import csv
import re
import requests
from io import StringIO
from datetime import datetime
from typing import List, Dict, Any, Optional
from cultureindex_client import CultureIndexClient, CultureIndexAuthError

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
    """Format phone number to international format (E.164) if valid."""
    if not phone_str or not phone_str.strip() or not PHONENUMBERS_AVAILABLE:
        return phone_str
    
    # Try US format first, then fallback to auto-detection
    for region in ["US", None]:
        try:
            parsed = phonenumbers.parse(phone_str, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        except NumberParseException:
            continue
    
    return phone_str


def convert_to_central_date(date_str: Optional[str]) -> Optional[str]:
    """Convert ISO timestamp to Central Time MM/DD/YYYY format."""
    if not date_str:
        return None
    
    try:
        if date_str.endswith('Z'):
            date_str = date_str.replace('Z', '+00:00')
        
        dt = datetime.fromisoformat(date_str)
        
        # Ensure timezone aware
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            except (TypeError, AttributeError):
                import pytz
                dt = pytz.UTC.localize(dt)
        
        # Convert to Central Time
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
    """Fetch PDF file size using HTTP Range header (efficient, no full download)."""
    if not pdf_url or not pdf_url.strip():
        return None
    
    try:
        response = session.get(
            pdf_url,
            headers={'Range': 'bytes=0-0'},
            allow_redirects=True,
            timeout=10,
            stream=True
        )
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
    """
    Extract survey report URLs from Culture Index CSV export.
    Handles BOM characters and various column naming formats.
    """
    survey_urls = {}
    
    try:
        csv_reader = csv.DictReader(StringIO(csv_data))
        
        # Find relevant columns (handle BOM and formatting variations)
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
    """Transform raw survey data into standardized format."""
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
    """Batch fetch all surveys from Culture Index API and save to JSON file."""
    email = os.getenv("CULTUREINDEX_EMAIL")
    password = os.getenv("CULTUREINDEX_PASSWORD")
    client_id = "A89F5B0000"
    
    if not email or not password:
        return 1
    
    client = CultureIndexClient()
    
    try:
        client.login(email=email, password=password)
        all_surveys = client.get_all_surveys(client_id=client_id)
        csv_data = client.export_surveys_csv(client_id=client_id)
        survey_urls = get_survey_urls_from_csv(csv_data)
        
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        filtered_surveys = []
        for survey in all_surveys:
            survey_id = str(survey.get('surveyId', ''))
            survey_url = survey_urls.get(survey_id)
            
            pdf_size_info = None
            if survey_url:
                pdf_size_info = get_pdf_size(survey_url, session)
            
            filtered_surveys.append(extract_survey_info(survey, survey_url, pdf_size_info))
        
        output_file = "culture_index_surveys.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(filtered_surveys, f, indent=2, ensure_ascii=False)
        
        return 0
        
    except CultureIndexAuthError:
        return 1
    except Exception:
        return 1

if __name__ == "__main__":
    sys.exit(fetch_all_surveys())

