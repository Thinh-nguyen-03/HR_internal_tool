import os
import socket
import ipaddress
from urllib.parse import urlparse
from typing import Optional, Tuple


ALLOWED_SCHEMES = ['https']

ALLOWED_DOMAINS = [
    'portal.cultureindex.com',
    'cultureindex.com',
    'www.cultureindex.com',
    'surveys.cultureindex.com'  # Survey PDF URLs
]

# Additional domains via ALLOWED_PDF_DOMAINS environment variable
EXTRA_ALLOWED_DOMAINS = os.getenv('ALLOWED_PDF_DOMAINS', '').split(',')
ALLOWED_DOMAINS.extend([d.strip() for d in EXTRA_ALLOWED_DOMAINS if d.strip()])


def is_safe_url(url: str, verbose: bool = False) -> Tuple[bool, Optional[str]]:
    """
    Validate URL to prevent SSRF (Server-Side Request Forgery) attacks.
    
    Checks: HTTPS-only, domain allowlist, resolves to public IP.
    Returns: (is_safe, error_message)
    """
    if not url:
        return False, "URL is empty"
    
    try:
        parsed = urlparse(url)
        
        if parsed.scheme not in ALLOWED_SCHEMES:
            error = f"Unsafe URL scheme '{parsed.scheme}' (only {ALLOWED_SCHEMES} allowed)"
            if verbose:
                print(f"[SECURITY] {error}: {url}")
            return False, error
        
        hostname = parsed.hostname
        if not hostname:
            error = "URL has no hostname"
            if verbose:
                print(f"[SECURITY] {error}: {url}")
            return False, error
        
        hostname_lower = hostname.lower()
        if hostname_lower not in [d.lower() for d in ALLOWED_DOMAINS]:
            error = f"Domain '{hostname}' not in allowlist"
            if verbose:
                print(f"[SECURITY] {error}: {url}")
                print(f"[SECURITY] Allowed domains: {ALLOWED_DOMAINS}")
            return False, error
        
        # Verify hostname resolves to public IP (blocks internal network access)
        try:
            ip_str = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip_str)
            
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                error = f"URL resolves to internal IP: {ip_str}"
                if verbose:
                    print(f"[SECURITY] {error}: {url}")
                return False, error
            
            if ip_obj.is_multicast or ip_obj.is_reserved:
                error = f"URL resolves to reserved IP: {ip_str}"
                if verbose:
                    print(f"[SECURITY] {error}: {url}")
                return False, error
            
            if verbose:
                print(f"[SECURITY] URL validated: {hostname} -> {ip_str}")
        
        except socket.gaierror as e:
            error = f"Cannot resolve hostname '{hostname}': {e}"
            if verbose:
                print(f"[SECURITY] {error}")
            return False, error
        
        except ValueError as e:
            error = f"Invalid IP address for hostname '{hostname}': {e}"
            if verbose:
                print(f"[SECURITY] {error}")
            return False, error
        
        return True, None
    
    except Exception as e:
        error = f"URL validation error: {e}"
        if verbose:
            print(f"[SECURITY] {error}: {url}")
        return False, error


def validate_url_or_raise(url: str, verbose: bool = False) -> None:
    """Validate URL and raise ValueError if unsafe."""
    is_safe, error_msg = is_safe_url(url, verbose=verbose)
    if not is_safe:
        raise ValueError(f"Unsafe URL blocked: {error_msg}")


def get_safe_url_info() -> dict:
    """Get URL validation configuration (for debugging/health checks)."""
    return {
        "allowed_schemes": ALLOWED_SCHEMES,
        "allowed_domains": ALLOWED_DOMAINS,
        "extra_domains_from_env": EXTRA_ALLOWED_DOMAINS
    }

