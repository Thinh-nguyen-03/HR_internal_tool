# HR Internal Tool

A web application for managing Culture Index surveys and integrating with JazzHR. Enables viewing, searching, and uploading survey PDFs to JazzHR applicant profiles.

## Features

- **Survey Management**: View and search Culture Index surveys with pagination
- **JazzHR Integration**: Check upload status and upload survey PDFs to JazzHR
- **Background Processing**: Automatic status checking for surveys
- **Authentication**: Secure login system with rate limiting
- **Caching**: Redis-based caching for performance optimization

## Prerequisites

- Python 3.11+
- Redis instance (Upstash or local)
- Culture Index API credentials
- JazzHR API key

## Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Create a `.env` file in the project root with the following variables:

### Required
```env
# Authentication
APP_USERNAME=senergy_hr
APP_PASSWORD=your_password
SECRET_KEY=your_secret_key

# Culture Index API
CULTUREINDEX_EMAIL=your_email@example.com
CULTUREINDEX_PASSWORD=your_password
CLIENT_ID=A89F5B0000

# JazzHR API
JAZZHR_API_KEY=your_jazzhr_api_key

# Redis (optional - defaults to file cache)
REDIS_URL=rediss://your_redis_url
CACHE_BACKEND=redis
```

### Optional
```env
# Application Settings
ITEMS_PER_PAGE=15
POLL_INTERVAL_MS=30000
JAZZHR_CACHE_HOURS=24
MAX_BATCH_UPLOAD=15
MAX_WORKERS=4
```

## Running the Application

### Development
```bash
python app.py
```

The application will start on `http://127.0.0.1:8051`

### Production
```bash
gunicorn app:server
```

## Project Structure

```
├── app.py                      # Main Dash application
├── auth.py                     # Authentication logic
├── login_layout.py             # Login page layout
├── cultureindex_client_1.py     # Culture Index API client
├── check_jazzhr_uploads.py     # JazzHR API integration
├── cache_storage_1.py           # Caching layer (Redis/File)
├── surveys_fetch.py             # Survey data utilities
└── assets/
    ├── custom.css              # Main stylesheet
    └── login.css               # Login page styles
```

## Key Components

- **SimpleSurveyService**: Manages survey data fetching and caching from Culture Index
- **SimpleJazzHRService**: Handles JazzHR API interactions and PDF uploads
- **BackgroundJazzHRChecker**: Background worker for checking survey upload status
- **AuthManager**: User authentication and session management

## Usage

1. **Login**: Access the application and authenticate with credentials
2. **Search**: Use the search bar to find surveys by name
3. **View Status**: Survey cards display JazzHR upload status
4. **Upload**: Click "Upload" on individual surveys or select multiple and use "Upload Selected"
5. **Refresh**: Use "Refresh CI" to reload surveys or "Refresh JazzHR" to clear status cache

## Development Notes

- The application uses Dash callbacks for real-time updates
- Background workers run in separate threads to avoid blocking the UI
- Rate limiting is implemented for JazzHR API calls
- Caching reduces API calls and improves performance
- Health check endpoints available at `/health`, `/health/ready`, `/health/live`

## Troubleshooting

- **Redis Connection Issues**: Set `CACHE_BACKEND=file` to use file-based caching
- **Upload Failures**: Check JazzHR API key and applicant matching logic
- **Slow Performance**: Adjust `MAX_WORKERS` and `POLL_INTERVAL_MS` in `.env`

