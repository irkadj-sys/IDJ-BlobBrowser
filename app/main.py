from __future__ import annotations

import base64
import binascii
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobBlock, BlobSasPermissions, BlobServiceClient, ContentSettings, generate_blob_sas
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
    clean = _normalize_path(folder)
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


def _can_create_subfolder(scope: Dict[str, Any], parent_folder: str) -> bool:
    if scope["is_admin"]:
        return True
    parent = _normalize_path(parent_folder)
    own = _normalize_path(scope["own_folder"])
    if not parent or not own:
        return False
    return parent == own or parent.startswith(f"{own}/")


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


def _clone_content_settings(source: Optional[ContentSettings]) -> ContentSettings:
    if not source:
        return ContentSettings(content_type="application/octet-stream")
    return ContentSettings(
        content_type=source.content_type or "application/octet-stream",
        content_encoding=source.content_encoding,
        content_language=source.content_language,
        content_disposition=source.content_disposition,
        cache_control=source.cache_control,
        content_md5=source.content_md5,
    )


def _is_folder_marker(blob_name: str) -> bool:
    marker_name = Path(_normalize_path(blob_name)).name.lower()
    return marker_name in {".folder", ".keep"}


def _max_upload_mb() -> int:
    return int(_env("MAX_UPLOAD_MB", "2048") or "2048")


def _max_upload_bytes() -> int:
    value = _max_upload_mb()
    return value * 1024 * 1024 if value > 0 else 0


def _max_chunk_bytes() -> int:
    value = int(_env("UPLOAD_CHUNK_MB", "8") or "8")
    return value * 1024 * 1024 if value > 0 else 8 * 1024 * 1024


def _upload_concurrency() -> int:
    value = int(_env("UPLOAD_CONCURRENCY", "4") or "4")
    return value if value > 0 else 4


def _sas_expiry_minutes() -> int:
    value = int(_env("SAS_UPLOAD_EXPIRY_MINUTES", "20") or "20")
    return value if value > 0 else 20


def _storage_account_name() -> str:
    account_url = _required_env("AZURE_STORAGE_ACCOUNT_URL")
    host = urlparse(account_url).netloc
    account_name = host.split(".")[0]
    if not account_name:
        raise RuntimeError("Unable to resolve storage account name from AZURE_STORAGE_ACCOUNT_URL")
    return account_name


def _validate_total_file_size(file_size_bytes: int) -> None:
    max_bytes = _max_upload_bytes()
    if max_bytes > 0 and file_size_bytes > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"File too large. Max is {max_mb} MB")


def _chunk_block_id(index: int) -> str:
    return base64.b64encode(f"{index:08d}".encode("ascii")).decode("ascii")


def _build_blob_write_sas_url(blob_name: str) -> tuple[str, str]:
    service = _blob_service_client()
    container_name = _required_env("AZURE_STORAGE_CONTAINER")
    account_name = _storage_account_name()
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=5)
    expiry = now + timedelta(minutes=_sas_expiry_minutes())
    delegation_key = service.get_user_delegation_key(key_start_time=start, key_expiry_time=expiry)
    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=container_name,
        blob_name=blob_name,
        user_delegation_key=delegation_key,
        permission=BlobSasPermissions(create=True, write=True),
        start=start,
        expiry=expiry,
    )
    blob_url = service.get_container_client(container_name).get_blob_client(blob_name).url
    sas_url = f"{blob_url}?{sas_token}"
    return sas_url, expiry.astimezone(timezone.utc).isoformat()


def _folder_sort_key(folder_path: str) -> tuple[str, ...]:
    return tuple(part.lower() for part in _normalize_path(folder_path).split("/"))


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


@app.get("/api/config")
def config(request: Request):
    _current_user(request)
    return {
        "max_upload_mb": _max_upload_mb(),
        "upload_chunk_mb": _max_chunk_bytes() // (1024 * 1024),
        "upload_concurrency": _upload_concurrency(),
    }


@app.get("/api/folders")
def list_folders(request: Request):
    user = _current_user(request)
    scope = _access_scope(user)
    container = _container_client()

    discovered: set[str] = set()
    for blob in container.list_blobs():
        name = _normalize_path(blob.name)
        if not name or not _can_read_blob(scope, name):
            continue
        parts = name.split("/")
        if len(parts) <= 1:
            continue
        for idx in range(1, len(parts)):
            discovered.add("/".join(parts[:idx]))

    for folder in [scope["own_folder"], scope["shared_folder"]]:
        if folder:
            discovered.add(folder)

    folders = sorted(discovered, key=_folder_sort_key)

    items = [
        {
            "name": _normalize_path(folder),
            "label": Path(_normalize_path(folder)).name or _normalize_path(folder),
            "depth": max(len(_normalize_path(folder).split("/")) - 1, 0),
            "parent": "/".join(_normalize_path(folder).split("/")[:-1]),
            "can_upload": scope["is_admin"] or _path_allowed_for_prefixes(folder, scope["writable_folders"]),
            "can_create_subfolder": _can_create_subfolder(scope, folder),
        }
        for folder in folders
    ]
    return {"items": items, "count": len(items)}


@app.get("/api/files")
def list_files(request: Request, prefix: str = "", direct_only: bool = True):
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
            if _is_folder_marker(blob.name):
                continue
            if direct_only:
                full_name = _normalize_path(blob.name)
                if azure_prefix:
                    relative = full_name[len(azure_prefix) :]
                    if not relative or "/" in relative:
                        continue
                elif "/" in full_name:
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

    container = _container_client()
    uploaded = []
    for file in files:
        data = await file.read()
        _validate_total_file_size(len(data))

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


@app.post("/api/upload/sas")
def upload_file_sas(
    request: Request,
    folder: str = Form(default=""),
    original_name: str = Form(...),
    file_size: int = Form(default=0),
):
    user = _current_user(request)
    scope = _access_scope(user)
    upload_folder = _normalize_path(folder)
    if not upload_folder:
        raise HTTPException(status_code=400, detail="Folder is required")
    if not _can_write_folder(scope, upload_folder):
        raise HTTPException(status_code=403, detail="You can only upload to your own folder or shared folder")

    if file_size > 0:
        _validate_total_file_size(file_size)

    blob_name = _safe_blob_name(original_name or "upload.bin", upload_folder)
    try:
        sas_url, expires_on = _build_blob_write_sas_url(blob_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to create upload SAS") from exc

    return {"ok": True, "blob_name": blob_name, "upload_url": sas_url, "expires_on": expires_on}


@app.post("/api/upload/chunked")
async def upload_file_chunk(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(default=""),
    original_name: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    file_size: int = Form(default=0),
):
    user = _current_user(request)
    scope = _access_scope(user)
    upload_folder = _normalize_path(folder)
    if not upload_folder:
        raise HTTPException(status_code=400, detail="Folder is required")
    if not _can_write_folder(scope, upload_folder):
        raise HTTPException(status_code=403, detail="You can only upload to your own folder or shared folder")
    if total_chunks <= 0:
        raise HTTPException(status_code=400, detail="Invalid total_chunks")
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk_index")

    if file_size > 0:
        _validate_total_file_size(file_size)

    chunk_data = await file.read()
    if len(chunk_data) > _max_chunk_bytes():
        max_chunk_mb = _max_chunk_bytes() // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"Chunk too large. Max chunk is {max_chunk_mb} MB")

    blob_name = _safe_blob_name(original_name or file.filename or "upload.bin", upload_folder)
    block_id = _chunk_block_id(chunk_index)
    container = _container_client()
    blob = container.get_blob_client(blob_name)
    blob.stage_block(block_id=block_id, data=chunk_data)
    return {"ok": True, "blob_name": blob_name, "chunk_index": chunk_index, "total_chunks": total_chunks}


@app.post("/api/upload/chunked/complete")
def complete_chunked_upload(
    request: Request,
    folder: str = Form(default=""),
    original_name: str = Form(...),
    total_chunks: int = Form(...),
    file_size: int = Form(default=0),
    content_type: str = Form(default="application/octet-stream"),
):
    user = _current_user(request)
    scope = _access_scope(user)
    upload_folder = _normalize_path(folder)
    if not upload_folder:
        raise HTTPException(status_code=400, detail="Folder is required")
    if not _can_write_folder(scope, upload_folder):
        raise HTTPException(status_code=403, detail="You can only upload to your own folder or shared folder")
    if total_chunks <= 0:
        raise HTTPException(status_code=400, detail="Invalid total_chunks")

    if file_size > 0:
        _validate_total_file_size(file_size)

    blob_name = _safe_blob_name(original_name or "upload.bin", upload_folder)
    block_list = [BlobBlock(block_id=_chunk_block_id(index)) for index in range(total_chunks)]
    container = _container_client()
    blob = container.get_blob_client(blob_name)
    blob.commit_block_list(
        block_list,
        content_settings=ContentSettings(content_type=content_type or "application/octet-stream"),
    )
    props = blob.get_blob_properties()
    return {"ok": True, "blob_name": blob_name, "size": props.size, "content_type": content_type}


@app.post("/api/folders/create")
def create_folder(
    request: Request,
    parent_folder: str = Form(...),
    folder_name: str = Form(...),
):
    user = _current_user(request)
    scope = _access_scope(user)

    parent = _normalize_path(parent_folder)
    raw_name = folder_name.strip()
    if not parent:
        raise HTTPException(status_code=400, detail="Parent folder is required")
    if not raw_name:
        raise HTTPException(status_code=400, detail="Folder name is required")
    if "/" in raw_name or "\\" in raw_name:
        raise HTTPException(status_code=400, detail="Folder name cannot include slashes")

    if not _can_read_blob(scope, parent):
        raise HTTPException(status_code=403, detail="You cannot access the parent folder")
    if not _can_create_subfolder(scope, parent):
        raise HTTPException(status_code=403, detail="You can only create folders under your own account folder")

    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", raw_name).strip().strip(".")
    if not safe_name:
        raise HTTPException(status_code=400, detail="Folder name has no valid characters")

    new_folder = _normalize_path(f"{parent}/{safe_name}")
    marker_blob = f"{new_folder}/.folder"

    container = _container_client()
    try:
        container.upload_blob(
            name=marker_blob,
            data=b"",
            overwrite=True,
            content_settings=ContentSettings(content_type="application/octet-stream"),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to create folder") from exc

    return {"ok": True, "folder_path": new_folder}


@app.post("/api/move")
def move_file(
    request: Request,
    source_path: str = Form(...),
    target_folder: str = Form(...),
):
    user = _current_user(request)
    scope = _access_scope(user)
    source_name = _normalize_path(source_path)
    destination_folder = _normalize_path(target_folder)
    if not source_name:
        raise HTTPException(status_code=400, detail="Source file is required")
    if not destination_folder:
        raise HTTPException(status_code=400, detail="Target folder is required")
    if not _can_read_blob(scope, source_name):
        raise HTTPException(status_code=403, detail="You cannot move this file")
    if not _can_write_folder(scope, destination_folder):
        raise HTTPException(status_code=403, detail="You cannot move files into this folder")

    target_name = _safe_blob_name(Path(source_name).name, destination_folder)
    if source_name.lower() == target_name.lower():
        return {"ok": True, "moved": False, "source_path": source_name, "target_path": target_name}

    container = _container_client()
    source_blob = container.get_blob_client(source_name)
    target_blob = container.get_blob_client(target_name)

    try:
        source_props = source_blob.get_blob_properties()
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Source file not found") from exc

    try:
        source_stream = source_blob.download_blob(max_concurrency=4)
        target_blob.upload_blob(
            data=source_stream.chunks(),
            overwrite=True,
            content_settings=_clone_content_settings(source_props.content_settings),
            metadata=source_props.metadata,
        )
        source_blob.delete_blob()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to move file") from exc

    return {"ok": True, "moved": True, "source_path": source_name, "target_path": target_name}


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
