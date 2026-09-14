import os
import shutil
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

# Temporary in-memory store for file metadata (could be moved to DB later)
file_metadata = {}

@router.post("/upload")
async def upload_file(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    # ── MIME type validation ──
    if file.content_type not in ALLOWED_TYPES:
        log.warning(f"Blocked upload: MIME type '{file.content_type}' from user '{current_user.username}'")
        raise HTTPException(
            status_code=400,
            detail=f"File type '{file.content_type}' is not allowed. "
                   f"Accepted types: images, documents, archives, and media."
        )

    # ── Read content with size check ──
    content = await file.read()
    if len(content) > MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(content) / (1024*1024):.1f} MB). "
                   f"Maximum allowed size is {MAX_SIZE_BYTES / (1024*1024):.0f} MB."
        )

    # ── Sanitize filename (prevent path traversal) ──
    safe_filename = os.path.basename(file.filename)
    if not safe_filename:
        safe_filename = f"upload_{uuid.uuid4().hex[:8]}"

    file_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_DIR, f"{file_id}_{safe_filename}")

    with open(file_path, "wb") as buffer:
        buffer.write(content)

    file_metadata[file_id] = {
        "file_id": file_id,
        "filename": safe_filename,
        "size": len(content),
        "path": file_path,
        "content_type": file.content_type,
    }

    log.info(f"File uploaded: {safe_filename} ({len(content)} bytes) by {current_user.username}")
    return {"success": True, "filename": safe_filename, "file_id": file_id}

@router.get("/files")
async def list_files(current_user: User = Depends(get_current_user)):
    return {"files": list(file_metadata.values())}

@router.get("/download/{file_id}")
async def download_file(file_id: str, current_user: User = Depends(get_current_user)):
    meta = file_metadata.get(file_id)
    if not meta or not os.path.exists(meta["path"]):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=meta["path"], filename=meta["filename"])

@router.delete("/files/{file_id}")
async def delete_file(file_id: str, current_user: User = Depends(get_current_user)):
    meta = file_metadata.pop(file_id, None)
    if meta and os.path.exists(meta["path"]):
        os.remove(meta["path"])
        return {"success": True}
    raise HTTPException(status_code=404, detail="File not found")
