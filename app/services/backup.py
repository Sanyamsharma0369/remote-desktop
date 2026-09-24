"""
app/services/backup.py — Production database and uploads backup/restore engine.

Provides atomic backup creation, SHA256 integrity verification, and safe restoration
for both SQLite disk databases and the /app/uploads persistence directory.
"""
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from typing import Dict, Any, Optional

from app.core.config import settings

log = logging.getLogger(__name__)
APP_VERSION = "2.0.0"


def _compute_sha256(filepath: str) -> str:
    """Computes SHA256 hex digest of a file in 64KB chunks."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _get_sqlite_path_from_url(db_url: str) -> Optional[str]:
    """Extracts local file path from a sqlite:/// URL."""
    if not db_url.startswith("sqlite:"):
        return None
    cleaned = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    if not cleaned or cleaned == ":memory:":
        return None
    return os.path.abspath(cleaned)


def create_backup(
    backup_dir: str = "backups",
    db_path: Optional[str] = None,
    uploads_dir: str = "uploads",
) -> str:
    """
    Creates an atomic backup archive containing:
      1. Consistent snapshot of the SQLite database using sqlite3 online backup API
      2. Complete uploads directory tree
      3. Cryptographic manifest.json with SHA256 checksums and timestamps
    """
    os.makedirs(backup_dir, exist_ok=True)
    timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"rd_backup_{timestamp_str}.zip"
    backup_zip_path = os.path.join(backup_dir, backup_filename)

    # Determine database path
    resolved_db_path = db_path
    if not resolved_db_path:
        resolved_db_path = _get_sqlite_path_from_url(settings.DATABASE_URL)

    with tempfile.TemporaryDirectory() as temp_dir:
        staging_dir = os.path.join(temp_dir, "staging")
        os.makedirs(staging_dir, exist_ok=True)
        manifest: Dict[str, Any] = {
            "version": APP_VERSION,
            "backup_timestamp": datetime.utcnow().isoformat() + "Z",
            "database": None,
            "uploads": [],
        }

        # 1. Backup Database (if file exists)
        if resolved_db_path and os.path.exists(resolved_db_path):
            staged_db_path = os.path.join(staging_dir, "database.sqlite")
            # Use SQLite backup API for live consistency
            src_conn = sqlite3.connect(resolved_db_path)
            dst_conn = sqlite3.connect(staged_db_path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

            db_checksum = _compute_sha256(staged_db_path)
            manifest["database"] = {
                "rel_path": "database.sqlite",
                "sha256": db_checksum,
                "size_bytes": os.path.getsize(staged_db_path),
            }

        # 2. Backup Uploads
        staged_uploads_dir = os.path.join(staging_dir, "uploads")
        os.makedirs(staged_uploads_dir, exist_ok=True)
        if os.path.exists(uploads_dir):
            for root, _, files in os.walk(uploads_dir):
                for file_name in files:
                    src_file = os.path.join(root, file_name)
                    rel_to_uploads = os.path.relpath(src_file, uploads_dir)
                    dst_file = os.path.join(staged_uploads_dir, rel_to_uploads)
                    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                    shutil.copy2(src_file, dst_file)

                    manifest["uploads"].append({
                        "rel_path": os.path.join("uploads", rel_to_uploads).replace("\\", "/"),
                        "sha256": _compute_sha256(dst_file),
                        "size_bytes": os.path.getsize(dst_file),
                    })

        # 3. Write manifest.json
        manifest_path = os.path.join(staging_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2)

        # 4. Create ZIP archive
        with zipfile.ZipFile(backup_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(staging_dir):
                for file_name in files:
                    abs_path = os.path.join(root, file_name)
                    arc_name = os.path.relpath(abs_path, staging_dir).replace("\\", "/")
                    zf.write(abs_path, arc_name)

    log.info("Backup created successfully at: %s", backup_zip_path)
    return os.path.abspath(backup_zip_path)


def verify_backup(backup_zip_path: str) -> Dict[str, Any]:
    """
    Validates a backup archive:
      - Archive integrity & structure
      - manifest.json presence and format
      - SHA256 checksums of every item against manifest
      - SQLite PRAGMA integrity_check on the database
    """
    if not os.path.exists(backup_zip_path):
        return {"valid": False, "error": f"File not found: {backup_zip_path}"}

    try:
        with zipfile.ZipFile(backup_zip_path, "r") as zf:
            namelist = zf.namelist()
            if "manifest.json" not in namelist:
                return {"valid": False, "error": "Missing manifest.json in archive"}

            manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))

            with tempfile.TemporaryDirectory() as temp_dir:
                zf.extractall(temp_dir)

                # Verify Database
                db_info = manifest_data.get("database")
                if db_info:
                    extracted_db = os.path.join(temp_dir, db_info["rel_path"])
                    if not os.path.exists(extracted_db):
                        return {"valid": False, "error": f"Database file missing: {db_info['rel_path']}"}
                    calc_sha = _compute_sha256(extracted_db)
                    if calc_sha != db_info["sha256"]:
                        return {
                            "valid": False,
                            "error": f"Database SHA256 mismatch (expected {db_info['sha256']}, got {calc_sha})",
                        }
                    # SQLite integrity check
                    conn = sqlite3.connect(extracted_db)
                    try:
                        cursor = conn.cursor()
                        res = cursor.execute("PRAGMA integrity_check").fetchone()
                        if not res or res[0] != "ok":
                            return {"valid": False, "error": f"SQLite integrity check failed: {res}"}
                    finally:
                        conn.close()

                # Verify Uploads
                for item in manifest_data.get("uploads", []):
                    extracted_file = os.path.join(temp_dir, item["rel_path"])
                    if not os.path.exists(extracted_file):
                        return {"valid": False, "error": f"Upload file missing: {item['rel_path']}"}
                    calc_sha = _compute_sha256(extracted_file)
                    if calc_sha != item["sha256"]:
                        return {
                            "valid": False,
                            "error": f"File SHA256 mismatch on {item['rel_path']}",
                        }

            return {
                "valid": True,
                "manifest": manifest_data,
                "file_count": len(manifest_data.get("uploads", [])),
                "has_database": bool(db_info),
            }
    except Exception as exc:
        return {"valid": False, "error": str(exc)}


def restore_backup(
    backup_zip_path: str,
    target_db_path: Optional[str] = None,
    target_uploads_dir: str = "uploads",
) -> Dict[str, Any]:
    """
    Restores database and uploaded files from a verified backup archive.
    """
    verification = verify_backup(backup_zip_path)
    if not verification["valid"]:
        raise ValueError(f"Cannot restore invalid backup: {verification.get('error')}")

    resolved_db_path = target_db_path
    if not resolved_db_path:
        resolved_db_path = _get_sqlite_path_from_url(settings.DATABASE_URL)

    with tempfile.TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(backup_zip_path, "r") as zf:
            zf.extractall(temp_dir)

        manifest = verification["manifest"]

        # 1. Restore Database
        db_info = manifest.get("database")
        if db_info and resolved_db_path:
            os.makedirs(os.path.dirname(os.path.abspath(resolved_db_path)), exist_ok=True)
            extracted_db = os.path.join(temp_dir, db_info["rel_path"])
            shutil.copy2(extracted_db, resolved_db_path)
            log.info("Database restored to: %s", resolved_db_path)

        # 2. Restore Uploads
        os.makedirs(target_uploads_dir, exist_ok=True)
        extracted_uploads = os.path.join(temp_dir, "uploads")
        if os.path.exists(extracted_uploads):
            for root, _, files in os.walk(extracted_uploads):
                for file_name in files:
                    src_file = os.path.join(root, file_name)
                    rel_to_uploads = os.path.relpath(src_file, extracted_uploads)
                    dst_file = os.path.join(target_uploads_dir, rel_to_uploads)
                    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                    shutil.copy2(src_file, dst_file)
            log.info("Uploads restored to: %s", target_uploads_dir)

    return {
        "success": True,
        "restored_timestamp": datetime.utcnow().isoformat() + "Z",
        "backup_manifest": manifest,
    }
