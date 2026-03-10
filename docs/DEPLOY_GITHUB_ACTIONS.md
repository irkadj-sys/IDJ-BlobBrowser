# Deploy From GitHub Actions (No Local Run)

This project deploys to Azure App Service from GitHub Actions.

## 1) Create Azure App Service (Python)

Create (or reuse) App Service with:
- Runtime: Python 3.11
- OS: Linux

Enable Managed Identity on the web app.

## 2) Grant Blob permissions to the web app identity

On your storage account (or target container), assign role:
- `Storage Blob Data Contributor`

to the App Service managed identity.

## 3) Configure App Service settings

In App Service -> Configuration -> Application settings, add:
- `AZURE_STORAGE_ACCOUNT_URL` = `https://<storage-account>.blob.core.windows.net`
- `AZURE_STORAGE_CONTAINER` = `<private-container-name>`
- Optional: `ALLOWED_USER_EMAILS` = `email1,email2`
- Optional: `MAX_UPLOAD_MB` = `50`

Set Startup Command (App Service -> Configuration -> General settings):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 4) Configure App Authentication (Easy Auth)

In App Service -> Authentication:
1. Add Microsoft identity provider (Entra ID)
2. Require authentication for all requests
3. Restrict to assigned users/groups as needed

## 5) Connect GitHub Actions

In GitHub repo settings:

### Repository Variables
- `AZURE_WEBAPP_NAME` = `<your-app-service-name>`

### Repository Secrets
- `AZURE_WEBAPP_PUBLISH_PROFILE` = publish profile XML from Azure App Service

How to get publish profile:
- Azure Portal -> App Service -> Overview -> **Get publish profile**
- Copy full XML into the GitHub secret value.

## 6) Deploy

Push to `main` branch, or run workflow manually:
- GitHub -> Actions -> `Deploy to Azure App Service` -> Run workflow

## 7) Verify

1. Open `https://<app-name>.azurewebsites.net/`
2. Sign in with allowed account
3. Upload file
4. Browse list
5. Open image preview
