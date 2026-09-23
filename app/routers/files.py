"""
app/routers/files.py — Authenticated file upload/download/delete with persistent DB metadata.

Ownership model:
  - Each uploaded file records the uploader's user ID in the database.
  - Normal users can only list/download/delete their own files.
  - Admin users can list/download/delete ALL files.

Memory safety:
  - Files are written in chunks; the full file is never loaded into RAM.
  - The 100 MB limit is enforced during streaming; uploads are rejected
    before disk write completes if they exceed the limit.
  - Partial files are cleaned up on upload failure.

Persistence:
  - File metadata is persisted in the database via the FileRecord model,
    retaining ownership and metadata across server restarts.
"""
import os
import uuid
import logging
from fastapi import APIRouter, File, UploadFile, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.routers.auth import get_current_user
from app.models.user import User
from app.models.file import FileRecord

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


def _user_can_access(record: FileRecord, user: User) -> bool:
    """Return True if this user is allowed to read/delete the file."""
    if user.role == "admin":
        return True
    return record.uploader_id == user.id


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
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

    # ── Persist file metadata in DB ──
    record = FileRecord(
        file_id=file_id,
        filename=safe_filename,
        size=total_bytes,
        path=file_path,
        content_type=file.content_type,
        uploader_id=current_user.id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    log.info(
        "File uploaded: '%s' (%d bytes) by '%s' (id=%d)",
        safe_filename, total_bytes, current_user.username, current_user.id,
    )
    return {"success": True, "filename": safe_filename, "file_id": file_id}


@router.get("")
@router.get("/")
@router.get("/list")
@router.get("/files")
async def list_files(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return only files this user is allowed to see from DB."""
    if current_user.role == "admin":
        records = db.query(FileRecord).all()
    else:
        records = db.query(FileRecord).filter(FileRecord.uploader_id == current_user.id).all()

    return {"files": [r.to_dict() for r in records]}


@router.get("/download/{file_id}")
async def download_file(
    file_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = db.query(FileRecord).filter(FileRecord.file_id == file_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    if not _user_can_access(record, current_user):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(record.path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(path=record.path, filename=record.filename)


@router.delete("/{file_id}")
@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = db.query(FileRecord).filter(FileRecord.file_id == file_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    if not _user_can_access(record, current_user):
        raise HTTPException(status_code=403, detail="Access denied")

    file_path = record.path
    db.delete(record)
    db.commit()

    if os.path.exists(file_path):
        os.remove(file_path)
    return {"success": True}
