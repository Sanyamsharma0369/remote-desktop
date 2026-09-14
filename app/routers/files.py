"""
app/routers/files.py — Authenticated file upload/download/delete.

Ownership model:
  - Each uploaded file records the uploader's user ID.
  - Normal users can only list/download/delete their own files.
  - Admin users can list/download/delete ALL files.

Memory safety:
  - Files are written in chunks; the full file is never loaded into RAM.
  - The 100 MB limit is enforced during streaming; uploads are rejected
    before disk write completes if they exceed the limit.
  - Partial files are cleaned up on upload failure.

NOTE: File metadata is stored in-process memory (file_metadata dict).
      This means metadata does not survive a server restart. For a
      production deployment, move this to a database table. This is
      intentionally documented as a known limitation of the current
      stabilization release.
"""
import os
import uuid
import logging
from fastapi import APIRouter, File, UploadFile, Depends, HTTPException
from fastapi.responses import FileResponse
from app.routers.auth import get_current_user
from app.models.user import User

router = APIRouter()
log = logging.getLogger(__name__)

UPLOAD_DIR = "uploads"
MAX_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
CHUNK_SIZE = 64 * 1024              # 64 KB per chunk

# MIME types allowed for upload — extend as needed
ALLOWED_TYPES = {
    # Images
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
    # Documents
    "application/pdf",
    "text/plain", "text/csv",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # Archives
    "application/zip", "application/x-7z-compressed", "application/x-tar",
    "application/gzip",
    # Media
    "audio/mpeg", "audio/wav", "video/mp4", "video/webm",
}

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# In-memory metadata store. Key: file_id (str UUID).
# Known limitation: does not survive process restart.
file_metadata: dict[str, dict] = {}


def _user_can_access(meta: dict, user: User) -> bool:
    """Return True if this user is allowed to read/delete the file."""
    if user.role == "admin":
        return True
    return meta.get("uploader_id") == user.id


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    # ── MIME type validation ──
    if file.content_type not in ALLOWED_TYPES:
        log.warning(
            "Blocked upload: MIME type '%s' from user '%s'",
            file.content_type, current_user.username,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"File type '{file.content_type}' is not allowed. "
                "Accepted types: images, documents, archives, and media."
            ),
        )

    # ── Sanitize filename (prevent path traversal) ──
    safe_filename = os.path.basename(file.filename or "")
    if not safe_filename:
        safe_filename = f"upload_{uuid.uuid4().hex[:8]}"

    file_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{safe_filename}")

    # ── Chunked write with size enforcement ──
    total_bytes = 0
    try:
        with open(file_path, "wb") as fp:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_SIZE_BYTES:
                    # Enforce limit before completing the write
                    fp.close()
                    os.remove(file_path)
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File too large (> {MAX_SIZE_BYTES // (1024*1024)} MB). "
                            "Upload rejected."
                        ),
                    )
                fp.write(chunk)
    except HTTPException:
        raise  # already cleaned up above
    except Exception as e:
        # Clean up partial file on unexpected error
        if os.path.exists(file_path):
            os.remove(file_path)
        log.error("Upload failed for user '%s': %s", current_user.username, e)
        raise HTTPException(status_code=500, detail="Upload failed unexpectedly.")

    file_metadata[file_id] = {
        "file_id": file_id,
        "filename": safe_filename,
        "size": total_bytes,
        "path": file_path,
        "content_type": file.content_type,
        "uploader_id": current_user.id,         # ownership
        "uploader_username": current_user.username,
    }

    log.info(
        "File uploaded: '%s' (%d bytes) by '%s' (id=%d)",
        safe_filename, total_bytes, current_user.username, current_user.id,
    )
    return {"success": True, "filename": safe_filename, "file_id": file_id}


@router.get("/files")
async def list_files(current_user: User = Depends(get_current_user)):
    """Return only files this user is allowed to see."""
    visible = [
        meta for meta in file_metadata.values()
        if _user_can_access(meta, current_user)
    ]
    return {"files": visible}


@router.get("/download/{file_id}")
async def download_file(
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    meta = file_metadata.get(file_id)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    if not _user_can_access(meta, current_user):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(meta["path"]):
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(path=meta["path"], filename=meta["filename"])


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    meta = file_metadata.get(file_id)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    if not _user_can_access(meta, current_user):
        raise HTTPException(status_code=403, detail="Access denied")
    file_metadata.pop(file_id, None)
    if os.path.exists(meta["path"]):
        os.remove(meta["path"])
    return {"success": True}
