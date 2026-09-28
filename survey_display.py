"""Small Dash helpers for the survey list: relative times and the loading/error/empty states.

The state builders return the first eight outputs of display_surveys; the caller
appends the seen-version value.
"""
from datetime import datetime
from typing import Tuple

from dash import html


def format_time_ago(timestamp_str: str) -> str:
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


def build_loading_result(message: str = "Loading surveys") -> Tuple:
    return (
        [html.Div(message, className="empty-message")],
        "Loading",
        True,  # prev disabled
        True,  # next disabled
        "Loading data",
        [],    # surveys_data
        [],    # uploadable_ids
        {"display": "block"},  # loading indicator
    )


def build_error_result(message: str = "Unable to load surveys") -> Tuple:
    return (
        [html.Div(message, className="empty-message")],
        "Error",
        True,  # prev disabled
        True,  # next disabled
        "Unable to load surveys",
        [],    # surveys_data
        [],    # uploadable_ids
        {"display": "none"},  # loading indicator
    )


def build_empty_result(search_query: str = None) -> Tuple:
    msg = f"No results for '{search_query}'" if search_query else "No surveys found."
    return (
        [html.Div(msg, className="empty-message")],
        "0 results",
        True,  # prev disabled
        True,  # next disabled
        f"Updated: {datetime.now().strftime('%I:%M:%S %p')}",
        [],    # surveys_data
        [],    # uploadable_ids
        {"display": "none"},  # loading indicator
    )
