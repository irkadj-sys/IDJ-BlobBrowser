# Azure Setup (Simple Secure Mode)

This project assumes:
- Existing **private** Blob container
- Manual user access management
- Non-commercial family usage

## 1) App Service
1. Create App Service (Linux or Windows) for Python app.
2. Enable **System Assigned Managed Identity**.
3. Deploy this app code.

## 2) Blob access for app
Grant the App Service managed identity role on storage account or container:
- `Storage Blob Data Contributor`

## 3) App authentication (Easy Auth)
1. In App Service -> Authentication:
2. Add identity provider: **Microsoft** (Entra ID)
3. Require authentication for all requests.
4. Configure app registration / enterprise app.
5. Limit access to invited/approved users in Entra.

## 4) Storage hardening
- Keep container private (no anonymous public access).
- Prefer Entra + RBAC (already used here).
- Do not use storage account keys in app code.

## 5) App settings
Set App Service configuration values:
- `AZURE_STORAGE_ACCOUNT_URL`
- `AZURE_STORAGE_CONTAINER`
- Optional: `ALLOWED_USER_EMAILS`
- Optional: `MAX_UPLOAD_MB`

## 6) Validation checklist
1. Anonymous user gets auth challenge.
2. Non-approved user cannot enter app (Entra assignment or allow-list block).
3. Approved user can upload/list/open files.
4. Blob container is still private from direct anonymous web access.
