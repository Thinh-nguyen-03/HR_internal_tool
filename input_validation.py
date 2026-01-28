import re
from typing import Optional, Tuple


def validate_survey_id(survey_id: str) -> Tuple[bool, Optional[str]]:
    """Validate survey ID (alphanumeric, hyphens, underscores; 1-100 chars)."""
    if not survey_id:
        return False, "Survey ID is empty"
    
    if not isinstance(survey_id, str):
        return False, "Survey ID must be a string"
    
    survey_id = survey_id.strip()
    
    if len(survey_id) < 1:
        return False, "Survey ID is too short"
    
    if len(survey_id) > 100:
        return False, "Survey ID is too long (max 100 characters)"
    
    if not re.match(r'^[A-Za-z0-9_-]+$', survey_id):
        return False, "Survey ID contains invalid characters"
    
    return True, None


def validate_applicant_id(applicant_id: str) -> Tuple[bool, Optional[str]]:
    """Validate JazzHR applicant ID (alphanumeric, hyphens; 1-50 chars)."""
    if not applicant_id:
        return False, "Applicant ID is empty"
    
    if not isinstance(applicant_id, str):
        return False, "Applicant ID must be a string"
    
    applicant_id = applicant_id.strip()
    
    if len(applicant_id) < 1:
        return False, "Applicant ID is too short"
    
    if len(applicant_id) > 50:
        return False, "Applicant ID is too long (max 50 characters)"
    
    if not re.match(r'^[A-Za-z0-9-]+$', applicant_id):
        return False, "Applicant ID contains invalid characters"
    
    return True, None


def sanitize_search_query(query: str, max_length: int = 100) -> str:
    """Remove control chars, limit length, normalize whitespace."""
    if not query:
        return ""
    
    if not isinstance(query, str):
        query = str(query)
    
    # Keep only printable ASCII characters
    query = ''.join(char for char in query if 32 <= ord(char) <= 126)
    query = query.strip()
    query = re.sub(r'\s+', ' ', query)
    
    if len(query) > max_length:
        query = query[:max_length]
    
    return query


def validate_page_number(page: any) -> Tuple[bool, int, Optional[str]]:
    """Validate page number (1-10000). Returns (is_valid, sanitized_page, error)."""
    if page is None:
        return True, 1, None
    
    try:
        page_int = int(page)
    except (ValueError, TypeError):
        return False, 1, "Page number must be a valid integer"
    
    if page_int < 1:
        return False, 1, "Page number must be at least 1"
    
    if page_int > 10000:
        return False, 1, "Page number too large (max 10000)"
    
    return True, page_int, None


def validate_email(email: str) -> Tuple[bool, Optional[str]]:
    """Basic email validation (RFC 5321 format, 3-254 chars)."""
    if not email:
        return False, "Email is empty"
    
    if not isinstance(email, str):
        return False, "Email must be a string"
    
    email = email.strip()
    
    if len(email) < 3 or len(email) > 254:
        return False, "Email length invalid"
    
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        return False, "Invalid email format"
    
    return True, None


def validate_name(name: str, field_name: str = "Name") -> Tuple[bool, Optional[str]]:
    """Validate person name (letters, spaces, hyphens, apostrophes, periods; 1-100 chars)."""
    if not name:
        return False, f"{field_name} is empty"
    
    if not isinstance(name, str):
        return False, f"{field_name} must be a string"
    
    name = name.strip()
    
    if len(name) < 1 or len(name) > 100:
        return False, f"{field_name} length invalid"
    
    if not re.match(r"^[A-Za-z\s\-'.]+$", name):
        return False, f"{field_name} contains invalid characters"
    
    return True, None


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """Sanitize filename (remove path traversal, dangerous chars, limit length)."""
    if not filename:
        return "unnamed"
    
    filename = filename.replace('\\', '_').replace('/', '_')
    filename = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
    
    if filename.startswith('.'):
        filename = 'file' + filename
    
    if len(filename) > max_length:
        parts = filename.rsplit('.', 1)
        if len(parts) == 2:
            name, ext = parts
            max_name_len = max_length - len(ext) - 1
            filename = name[:max_name_len] + '.' + ext
        else:
            filename = filename[:max_length]
    
    return filename or "unnamed"


def validate_integer_param(value: any, param_name: str, min_val: int = None, max_val: int = None) -> Tuple[bool, Optional[int], Optional[str]]:
    """Validate integer parameter with optional range check."""
    if value is None:
        return False, None, f"{param_name} is required"
    
    try:
        int_val = int(value)
    except (ValueError, TypeError):
        return False, None, f"{param_name} must be a valid integer"
    
    if min_val is not None and int_val < min_val:
        return False, None, f"{param_name} must be at least {min_val}"
    
    if max_val is not None and int_val > max_val:
        return False, None, f"{param_name} must be at most {max_val}"
    
    return True, int_val, None
