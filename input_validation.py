"""Sanitizers for user input from the survey list UI."""
import re
from typing import Any, Optional, Tuple


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


def validate_page_number(page: Any) -> Tuple[bool, int, Optional[str]]:
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
