from __future__ import annotations

import base64
import binascii
import json
import os
import re
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

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


def _admin_users() -> set[str]:
    raw = _env("ADMIN_USER_EMAILS")
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _shared_folder_name() -> str:
    value = _normalize_path(_env("SHARED_FOLDER_NAME", "shared"))
    return value or "shared"


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").strip("/")


def _sanitize_folder_name(value: str) -> str:
    normalized = _normalize_path(value).lower()
    return re.sub(r"[^a-z0-9._-]", "_", normalized)


def _user_folder_name(email: str) -> str:
    local_part = email.split("@", 1)[0]
    return _sanitize_folder_name(local_part)


def _prefix_for_folder(folder: str) -> str:
    clean = _normalize_path(folder).lower()
    return f"{clean}/" if clean else ""


def _path_allowed_for_prefixes(blob_path: str, prefixes: Iterable[str]) -> bool:
    normalized = _normalize_path(blob_path).lower()
    for prefix in prefixes:
        clean_prefix = _normalize_path(prefix).lower()
        if not clean_prefix:
            continue
        if normalized == clean_prefix or normalized.startswith(f"{clean_prefix}/"):
            return True
    return False


def _is_admin(email: str) -> bool:
    admins = _admin_users()
    return bool(admins and email.lower() in admins)


def _access_scope(user: Dict[str, str]) -> Dict[str, Any]:
    email = user["email"].lower()
    own_folder = _user_folder_name(email)
    shared_folder = _shared_folder_name()
    admin = _is_admin(email)

    readable_folders = [] if admin else [own_folder, shared_folder]
    writable_folders = [] if admin else [own_folder, shared_folder]
    return {
        "is_admin": admin,
        "own_folder": own_folder,
        "shared_folder": shared_folder,
        "readable_folders": readable_folders,
        "writable_folders": writable_folders,
    }


def _can_read_blob(scope: Dict[str, Any], blob_path: str) -> bool:
    if scope["is_admin"]:
        return True
    return _path_allowed_for_prefixes(blob_path, scope["readable_folders"])


def _can_write_folder(scope: Dict[str, Any], folder: str) -> bool:
    if scope["is_admin"]:
        return True
    return _path_allowed_for_prefixes(folder, scope["writable_folders"])


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
    folder = _normalize_path(folder)
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
    user = _current_user(request)
    scope = _access_scope(user)
    return {
        **user,
        "is_admin": scope["is_admin"],
        "own_folder": scope["own_folder"],
        "shared_folder": scope["shared_folder"],
    }


@app.get("/api/folders")
def list_folders(request: Request):
    user = _current_user(request)
    scope = _access_scope(user)
    container = _container_client()

    discovered: set[str] = set()
    for blob in container.list_blobs():
        name = _normalize_path(blob.name)
        if not name or "/" not in name:
            continue
        top_level = name.split("/", 1)[0]
        if scope["is_admin"] or _path_allowed_for_prefixes(top_level, scope["readable_folders"]):
            discovered.add(top_level)

    if scope["is_admin"]:
        folders = sorted(discovered)
        for folder in [scope["own_folder"], scope["shared_folder"]]:
            if folder and folder not in folders:
                folders.append(folder)
        folders.sort()
    else:
        folders = []
        for folder in [scope["own_folder"], scope["shared_folder"]]:
            if folder and folder not in folders:
                folders.append(folder)
        for folder in sorted(discovered):
            if folder not in folders:
                folders.append(folder)

    items = [
        {
            "name": folder,
            "can_upload": scope["is_admin"] or _path_allowed_for_prefixes(folder, scope["writable_folders"]),
        }
        for folder in folders
    ]
    return {"items": items, "count": len(items)}


@app.get("/api/files")
def list_files(request: Request, prefix: str = ""):
    user = _current_user(request)
    scope = _access_scope(user)
    container = _container_client()
    requested_prefix = _normalize_path(prefix)

    list_prefixes: list[Optional[str]]
    if requested_prefix:
        if not _can_read_blob(scope, requested_prefix):
            raise HTTPException(status_code=403, detail="You cannot access this folder")
        list_prefixes = [requested_prefix]
    elif scope["is_admin"]:
        list_prefixes = [None]
    else:
        list_prefixes = scope["readable_folders"]

    files = []
    seen_names: set[str] = set()
    for list_prefix in list_prefixes:
        azure_prefix = _prefix_for_folder(list_prefix or "")
        for blob in container.list_blobs(name_starts_with=azure_prefix or None):
            if blob.name in seen_names:
                continue
            if not _can_read_blob(scope, blob.name):
                continue
            seen_names.add(blob.name)
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
    files: list[UploadFile] = File(...),
    folder: str = Form(default=""),
):
    user = _current_user(request)
    scope = _access_scope(user)
    upload_folder = _normalize_path(folder)
    if not upload_folder:
        raise HTTPException(status_code=400, detail="Folder is required")
    if not _can_write_folder(scope, upload_folder):
        raise HTTPException(status_code=403, detail="You can only upload to your own folder or shared folder")

    max_mb = int(_env("MAX_UPLOAD_MB", "50") or "50")
    container = _container_client()
    uploaded = []
    for file in files:
        data = await file.read()
        size_mb = len(data) / (1024 * 1024)
        if size_mb > max_mb:
            raise HTTPException(status_code=413, detail=f"{file.filename}: File too large. Max is {max_mb} MB")

        blob_name = _safe_blob_name(file.filename or "upload.bin", upload_folder)
        content_type = file.content_type or "application/octet-stream"
        container.upload_blob(
            name=blob_name,
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        uploaded.append({"blob_name": blob_name, "size": len(data), "content_type": content_type})

    return {"ok": True, "uploaded": uploaded, "count": len(uploaded)}


@app.get("/api/files/{blob_path:path}")
def download_file(request: Request, blob_path: str):
    user = _current_user(request)
    scope = _access_scope(user)
    if not blob_path:
        raise HTTPException(status_code=400, detail="Missing blob path")
    if not _can_read_blob(scope, blob_path):
        raise HTTPException(status_code=403, detail="You cannot access this file")

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
    user = _current_user(request)
    scope = _access_scope(user)
    if not blob_path:
        raise HTTPException(status_code=400, detail="Missing blob path")
    if not _can_read_blob(scope, blob_path):
        raise HTTPException(status_code=403, detail="You cannot access this image")

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
