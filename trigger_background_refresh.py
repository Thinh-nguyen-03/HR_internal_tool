import os
import sys
import requests

def main():
    app_url = os.getenv('APP_URL')
    secret = os.getenv('BACKGROUND_JOB_SECRET')
    
    if not app_url or not secret:
        print("ERROR: APP_URL and BACKGROUND_JOB_SECRET must be set", file=sys.stderr)
        sys.exit(1)
    
    url = f"{app_url}/api/background-refresh"
    headers = {
        'Authorization': f'Bearer {secret}',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(url, headers=headers, timeout=120)
        response.raise_for_status()
        print(f"Success: {response.json()}")
        sys.exit(0)
    except requests.exceptions.RequestException as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()

