# IDJ-BlobBrowser

Private family file manager for Azure Blob Storage.

## Scope (v1)
- Secure sign-in with Microsoft Entra ID (manual allow-list access)
- Upload files to a private Azure Blob container
- Browse files
- View images in browser

## Security model (simple + secure)
- App Service Authentication (Easy Auth) with Microsoft Entra ID
- Blob container remains private (no anonymous access)
- App uses Managed Identity to access Blob Storage
- Optional app-level email allow-list via `ALLOWED_USER_EMAILS`

## Run locally

1. Create virtual env and install deps:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Copy env file:

```powershell
Copy-Item .env.example .env
```

3. Set environment variables in your shell or `.env` loader of choice.

4. Start app:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000

## Required environment variables
- `AZURE_STORAGE_ACCOUNT_URL` (example: `https://<account>.blob.core.windows.net`)
- `AZURE_STORAGE_CONTAINER` (your existing private container)

## Optional environment variables
- `ALLOWED_USER_EMAILS` comma-separated emails for explicit app allow-list
- `LOCAL_DEV_USER_EMAIL` only for local testing when Easy Auth header is unavailable
- `MAX_UPLOAD_MB` upload limit (default `50`)

## Deploy (App Service)
- Azure setup/security baseline: [docs/AZURE_SETUP.md](docs/AZURE_SETUP.md)
- GitHub Actions deployment: [docs/DEPLOY_GITHUB_ACTIONS.md](docs/DEPLOY_GITHUB_ACTIONS.md)
