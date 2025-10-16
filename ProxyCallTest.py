import json
import requests
from google.oauth2 import service_account
import google.auth.transport.requests

# Replace with the path to your service account JSON key file.
SERVICE_ACCOUNT_FILE = 'D:\\PYTHON\\PythonProject1\\gen-lang-client-0602649305-1748c782f48d.json'

# This scope is used for executing external requests in Apps Script.
SCOPES = ['https://www.googleapis.com/auth/script.external_request']

# Create credentials using the service account file.
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

# Refresh credentials to get an access token.
auth_req = google.auth.transport.requests.Request()
credentials.refresh(auth_req)
access_token = credentials.token

# Set the Authorization header with the Bearer token.
headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json"
}

# Replace this with your actual GAS web app URL.
GAS_PROXY_URL = 'https://script.google.com/macros/s/AKfycbxxHg5OvtsRLRWUxOCmdPbfPF-91YdoA7TXGybLUNFkZzsl6DOmfmN5KRgh08tF0ztzMg/exec'


def test_full_payload():
    """
    Full payload test: Sends a complete JSON payload with all arguments.
    NOTE: maxOutputTokens увеличено до 1024, чтобы избежать обрыва ответа.
    """
    payload = {
        "model": "gemini-2.0-flash",
        "args": {
            "contents": [
                {
                    "parts": [
                        {"text": "Отвечай всегда на русском языке, если вопрос не содержит другого указания."},
                        {"text": "Что такое братский"}
                    ]
                }
            ],
            "generationConfig": {
                # Увеличение лимита токенов, чтобы получить полный ответ
                "maxOutputTokens": 1024,
                "temperature": 0.7,
                "topP": 0.95,
                "topK": 40
            },
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_ONLY_HIGH"
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                }
            ]

            # You can include more Gemini-supported fields here.
        }
    }
    try:
        response = requests.post(GAS_PROXY_URL, json=payload, headers=headers)
        print("=== Full Payload Test (maxOutputTokens: 1024) ===")
        print("Status Code:", response.status_code)
        try:
            data = response.json()
            # ensure_ascii=False помогает отображать кириллицу
            print("Response:", json.dumps(data, indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print("Response:", response.text)
    except Exception as e:
        print("Error during full payload test:", str(e))


def test_minimum_payload():
    """
    Minimum payload test: Sends a payload with only the required fields.
    """
    payload = {
        "model": "gemini-2.0-flash",
        "args": {
            "contents": [
                {
                    "parts": [
                        {"text": "What is AI?"}
                    ]
                }
            ]
        }
    }
    try:
        response = requests.post(GAS_PROXY_URL, json=payload, headers=headers)
        print("\n=== Minimum Payload Test ===")
        print("Status Code:", response.status_code)
        try:
            data = response.json()
            print("Response:", json.dumps(data, indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print("Response:", response.text)
    except Exception as e:
        print("Error during minimum payload test:", str(e))


if __name__ == '__main__':
    test_full_payload()
    test_minimum_payload()
