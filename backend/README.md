# PayPilot — Backend (Phase 1)

FastAPI service that establishes a secure PayPal **Sandbox** OAuth 2.0
connection. This phase implements authentication only.

## Setup (Windows / VS Code)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # then fill in your Sandbox credentials
```

Get Sandbox credentials from the
[PayPal Developer Dashboard](https://developer.paypal.com/dashboard/applications/sandbox).

| Variable               | Description                   | Default   |
| ---------------------- | ----------------------------- | --------- |
| `PAYPAL_CLIENT_ID`     | Sandbox app client ID         | —         |
| `PAYPAL_CLIENT_SECRET` | Sandbox app secret            | —         |
| `PAYPAL_ENVIRONMENT`   | `sandbox`                     | `sandbox` |

## Run

```powershell
uvicorn app.main:app --reload
```

## Test authentication

```powershell
curl http://127.0.0.1:8000/auth/health
```

Expected: `{"paypal_authenticated": true, "token_length": <number>}`.
The access token and client secret are never returned or logged.
