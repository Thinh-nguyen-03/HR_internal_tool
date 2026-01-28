from datetime import datetime
from typing import Dict, List, Tuple, Optional

from dash import html, dcc


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


def build_status_indicator(status: Optional[str], is_uploaded: bool) -> html.Div:
    if status is None:
        return html.Div(
            [html.Span("Checking...", className="status-text")],
            className="status-pending"
        )
    elif is_uploaded:
        return html.Div(
            [html.Span("Uploaded", className="status-text")],
            className="status-uploaded"
        )
    elif status == "NOT_UPLOADED":
        return html.Div(
            [html.Span("Not Uploaded", className="status-text")],
            className="status-missing"
        )
    elif status == "NOT_IN_JAZZHR":
        return html.Div(
            [html.Span("Not in Jazz", className="status-text")],
            className="status-not-found"
        )
    elif status == "MISSING_NAME":
        return html.Div(
            [html.Span("No Name", className="status-text")],
            className="status-pending"
        )
    elif status == "NO_PDF_URL":
        return html.Div(
            [html.Span("No URL", className="status-text")],
            className="status-pending"
        )
    elif status == "ERROR":
        return html.Div(
            [html.Span("Error", className="status-text")],
            className="status-error"
        )
    else:
        return html.Div(
            [html.Span("Unknown", className="status-text")],
            className="status-pending"
        )


def build_survey_card(
    survey: Dict,
    jazzhr_result: Dict,
    pdf_size: Optional[int]
) -> Tuple[html.Div, bool, Dict]:
    survey_id = str(survey.get('surveyId', ''))
    url = survey.get('surveyReportUrl')
    
    status = jazzhr_result.get('status')
    is_uploaded = jazzhr_result.get('isUploaded', False)
    applicant_id = jazzhr_result.get('applicantId')
    
    is_uploadable = (status == "NOT_UPLOADED" and applicant_id and url)
    
    survey_data = {
        "surveyId": survey_id,
        "firstName": survey.get('firstName', ''),
        "lastName": survey.get('lastName', ''),
        "applicantId": applicant_id,
        "pdf_url": url
    }
    
    status_indicator = build_status_indicator(status, is_uploaded)
    
    pdf_size_mb = pdf_size / (1024 * 1024) if pdf_size else None
    
    full_name = f"{survey.get('firstName', '')} {survey.get('lastName', '')}".strip() or "Unknown"
    position = survey.get('position', '').strip()
    trait_pattern = survey.get('traitPattern', 'N/A')
    
    card_children = [
        html.Div([
            html.Div([
                html.Span(full_name, className="survey-name"),
                html.Span(" | ", className="survey-separator") if position else None,
                html.Span(position, className="survey-position") if position else None,
                html.Span(trait_pattern, className="trait-badge"),
                html.Span(survey.get("surveyDate", "N/A"), className="survey-date"),
            ], className="survey-name-row"),
            status_indicator,
        ], className="survey-item-header"),
        
        html.Div([
            html.Div([
                html.Span("EMAIL", className="info-label"),
                html.Span(survey.get("email", "N/A"), className="info-value")
            ], className="info-field"),
            html.Div([
                html.Span("PHONE", className="info-label"),
                html.Span(survey.get("phoneNumber", "N/A"), className="info-value")
            ], className="info-field"),
            html.Div([
                html.Span("SURVEY ID", className="info-label"),
                html.Span(survey_id, className="info-value")
            ], className="info-field"),
            html.Div([
                html.Span("REPORT", className="info-label"),
                html.A(
                    f"View PDF ({pdf_size_mb:.2f} MB)" if pdf_size_mb else "View PDF",
                    href=url or "#",
                    target="_blank",
                    className="report-link"
                ) if url else html.Span("N/A", className="info-value"),
            ], className="info-field"),
        ], className="survey-info"),
        
        html.Div([
            html.Span(
                format_time_ago(jazzhr_result.get('timestamp')) if jazzhr_result.get('timestamp') else "Never checked",
                className="last-checked-value"
            ),
            html.Button(
                "Refresh Status",
                id={"type": "refresh-single-btn", "index": survey_id},
                n_clicks=0,
                className="refresh-single-btn",
                title="Check JazzHR status for this profile"
            ),
        ], className="survey-footer"),
    ]
    
    checkbox = dcc.Checklist(
        id={"type": "survey-checkbox", "index": survey_id},
        options=[{"label": "", "value": survey_id}],
        value=[],
        className="survey-checkbox"
    ) if is_uploadable else html.Div(className="survey-checkbox-placeholder")
    
    card = html.Div([
        checkbox,
        html.Div(card_children, className="survey-content")
    ], className="survey-card")
    
    return card, is_uploadable, survey_data


def build_survey_display(
    surveys: List[Dict],
    jazzhr_results: Dict,
    pdf_sizes: Dict
) -> Tuple[List[html.Div], List[str], List[Dict]]:
    survey_items = []
    uploadable_ids = []
    surveys_data = []
    
    for survey in surveys:
        survey_id = str(survey.get('surveyId', ''))
        jazzhr = jazzhr_results.get(survey_id, {})
        pdf_size = pdf_sizes.get(survey_id)
        
        card, is_uploadable, survey_data = build_survey_card(survey, jazzhr, pdf_size)
        
        survey_items.append(card)
        surveys_data.append(survey_data)
        
        if is_uploadable:
            uploadable_ids.append(survey_id)
    
    return survey_items, uploadable_ids, surveys_data


def build_loading_result(message: str = "Loading surveys...") -> Tuple:
    return (
        [html.Div(message, className="empty-message")],
        "Loading...",
        True,  # prev disabled
        True,  # next disabled
        "Loading data...",
        [],    # surveys_data
        [],    # uploadable_ids
        {"display": "block"},  # loading indicator
        ""     # upload status (no update)
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
        ""     # upload status (no update)
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
        ""     # upload status
    )


def build_notification_banner(count: int, timestamp_str: str = None) -> Tuple[html.Div, Dict]:
    if count <= 0:
        return "", {"display": "none"}
    
    try:
        if timestamp_str:
            dt = datetime.fromisoformat(timestamp_str)
            time_str = dt.strftime("%I:%M %p")
        else:
            time_str = "recently"
    except:
        time_str = "recently"
    
    plural = "s" if count != 1 else ""
    
    message = html.Div([
        html.Span(f"{count} new survey{plural} detected at {time_str}. "),
        html.Strong("Click here to refresh!", style={"textDecoration": "underline"})
    ])
    
    style = {
        "display": "flex",
        "alignItems": "center",
        "justifyContent": "center",
        "padding": "12px 24px",
        "cursor": "pointer"
    }
    
    return message, style

