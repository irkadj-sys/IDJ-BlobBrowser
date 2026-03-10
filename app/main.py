from __future__ import annotations

import base64
import binascii
import json
import os
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, Optional

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parents[1]

app = FastAPI(title="IDJ Blob Browser")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _allowed_users() -> set[str]:
    raw = _env("ALLOWED_USER_EMAILS")
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _parse_easy_auth_header(value: str) -> Dict[str, Any]:
    try:
        decoded = base64.b64decode(value)
        return json.loads(decoded)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=401, detail="Invalid auth header") from exc


def _claim_map(principal: Dict[str, Any]) -> Dict[str, str]:
    claims = principal.get("claims", [])
    mapped: Dict[str, str] = {}
    for claim in claims:
        typ = str(claim.get("typ", "")).lower()
        val = str(claim.get("val", ""))
        if typ and val:
            mapped[typ] = val
    return mapped


def _current_user(request: Request) -> Dict[str, str]:
    raw = request.headers.get("X-MS-CLIENT-PRINCIPAL", "")
    local_user = _env("LOCAL_DEV_USER_EMAIL")

    if not raw:
        if local_user:
            return {"email": local_user.lower(), "name": local_user, "oid": "local-dev"}
        raise HTTPException(status_code=401, detail="Authentication required")

    principal = _parse_easy_auth_header(raw)
    claims = _claim_map(principal)

    email = (
        claims.get("preferred_username")
        or claims.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
        or claims.get("email")
        or ""
    ).lower()
    name = (
        claims.get("name")
        or claims.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
        or email
    )
    oid = claims.get("http://schemas.microsoft.com/identity/claims/objectidentifier", "")

    if not email:
        raise HTTPException(status_code=401, detail="Unable to resolve user email")

    allowed = _allowed_users()
    if allowed and email not in allowed:
        raise HTTPException(status_code=403, detail="User is not allowed")

    return {"email": email, "name": name, "oid": oid}


def _blob_service_client() -> BlobServiceClient:
    account_url = _required_env("AZURE_STORAGE_ACCOUNT_URL")
    credential = DefaultAzureCredential()
    return BlobServiceClient(account_url=account_url, credential=credential)


def _container_client():
    container_name = _required_env("AZURE_STORAGE_CONTAINER")
    return _blob_service_client().get_container_client(container_name)


def _safe_blob_name(filename: str, folder: str) -> str:
    clean_name = Path(filename).name
    folder = folder.strip().strip("/")
    return f"{folder}/{clean_name}" if folder else clean_name


def _is_image(content_type: Optional[str], blob_name: str) -> bool:
    if content_type and content_type.lower().startswith("image/"):
        return True
    suffix = Path(blob_name).suffix.lower()
    return suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/me")
def me(request: Request):
    return _current_user(request)


@app.get("/api/files")
def list_files(request: Request, prefix: str = ""):
    _current_user(request)
    container = _container_client()

    files = []
    for blob in container.list_blobs(name_starts_with=prefix or None):
        content_type = (blob.content_settings.content_type if blob.content_settings else "") or ""
        files.append(
            {
                "name": blob.name,
                "size": blob.size,
                "last_modified": blob.last_modified.astimezone(timezone.utc).isoformat() if blob.last_modified else None,
                "content_type": content_type,
                "is_image": _is_image(content_type, blob.name),
            }
        )

    files.sort(key=lambda x: x["name"].lower())
    return {"items": files, "count": len(files)}


@app.post("/api/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(default=""),
):
    _current_user(request)

    max_mb = int(_env("MAX_UPLOAD_MB", "50") or "50")
    data = await file.read()
    size_mb = len(data) / (1024 * 1024)
    if size_mb > max_mb:
        raise HTTPException(status_code=413, detail=f"File too large. Max is {max_mb} MB")

    blob_name = _safe_blob_name(file.filename or "upload.bin", folder)
    content_type = file.content_type or "application/octet-stream"

    container = _container_client()
    container.upload_blob(
        name=blob_name,
        data=data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )

    return {"ok": True, "blob_name": blob_name, "size": len(data), "content_type": content_type}


@app.get("/api/files/{blob_path:path}")
def download_file(request: Request, blob_path: str):
    _current_user(request)
    if not blob_path:
        raise HTTPException(status_code=400, detail="Missing blob path")

    container = _container_client()
    blob = container.get_blob_client(blob_path)
    try:
        props = blob.get_blob_properties()
    except Exception as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc

    downloader = blob.download_blob()
    content_type = (props.content_settings.content_type if props.content_settings else None) or "application/octet-stream"
    return StreamingResponse(
        downloader.chunks(),
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{Path(blob_path).name}"'},
    )


@app.get("/api/images/{blob_path:path}")
def view_image(request: Request, blob_path: str):
    _current_user(request)
    if not blob_path:
        raise HTTPException(status_code=400, detail="Missing blob path")

    container = _container_client()
    blob = container.get_blob_client(blob_path)
    try:
        props = blob.get_blob_properties()
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Image not found") from exc

    content_type = (props.content_settings.content_type if props.content_settings else None) or "application/octet-stream"
    if not _is_image(content_type, blob_path):
        raise HTTPException(status_code=400, detail="Not an image")

    return StreamingResponse(blob.download_blob().chunks(), media_type=content_type)
