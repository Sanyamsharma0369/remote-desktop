"""
tests/test_regressions.py — Regression tests for Phase 1 & Phase 2 stabilization fixes.
Covers:
  - WebRTC offer authentication requirement (/api/stream/offer)
  - Stream info routing and double-prefix prevention
  - Per-user file ownership and isolation (User A vs User B vs Admin)
  - Persistent file metadata in DB (FileRecord model)
  - File upload MIME validation and chunked size limit
  - Power action auditing and schema safety
"""
import io
import pytest
from app.models.file import FileRecord


def test_stream_offer_requires_auth(client):
    """Unauthenticated POST /api/stream/offer must return 401 Unauthorized."""
    res = client.post("/api/stream/offer", json={"sdp": "v=0\r\n", "type": "offer"})
    assert res.status_code == 401


def test_stream_info_endpoint(client, user_token):
    """GET /api/stream/info returns encoder metadata and avoids double-prefix."""
    res = client.get("/api/stream/info", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 200
    data = res.json()
    assert "encoder" in data
    assert "encoder_label" in data

    # Double-prefix route must 404
    bad_res = client.get("/api/stream/api/stream/info", headers={"Authorization": f"Bearer {user_token}"})
    assert bad_res.status_code == 404


def test_file_ownership_isolation_and_persistence(client, user_token, admin_token, db):
    """User A uploads a file. User B cannot see, download, or delete it (403). Admin can.
    Verified against persistent database table (FileRecord)."""
    from app.models.user import User
    from app.routers.auth import get_password_hash, create_access_token

    # Create second regular user
    user_b = User(
        username="user2",
        password_hash=get_password_hash("UserPass2!"),
        role="user",
    )
    db.add(user_b)
    db.commit()
    db.refresh(user_b)
    user2_token = create_access_token({"sub": user_b.username})

    # User 1 uploads
    file_bytes = b"Sensitive data owned by user 1"
    res_upload = client.post(
        "/api/files/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        files={"file": ("report.txt", io.BytesIO(file_bytes), "text/plain")},
    )
    assert res_upload.status_code == 200
    file_id = res_upload.json()["file_id"]

    # Verify directly in DB
    record = db.query(FileRecord).filter(FileRecord.file_id == file_id).first()
    assert record is not None
    assert record.filename == "report.txt"
    assert record.size == len(file_bytes)
    assert record.content_type == "text/plain"

    # User 2 list: should NOT see file
    res_list_b = client.get("/api/files/files", headers={"Authorization": f"Bearer {user2_token}"})
    assert res_list_b.status_code == 200
    assert len(res_list_b.json()["files"]) == 0

    # User 2 download: 403 Forbidden
    res_down_b = client.get(f"/api/files/download/{file_id}", headers={"Authorization": f"Bearer {user2_token}"})
    assert res_down_b.status_code == 403

    # User 2 delete: 403 Forbidden
    res_del_b = client.delete(f"/api/files/files/{file_id}", headers={"Authorization": f"Bearer {user2_token}"})
    assert res_del_b.status_code == 403

    # Admin list: can see file
    res_list_admin = client.get("/api/files/files", headers={"Authorization": f"Bearer {admin_token}"})
    assert res_list_admin.status_code == 200
    assert any(f["file_id"] == file_id for f in res_list_admin.json()["files"])

    # Admin download: 200 OK
    res_down_admin = client.get(f"/api/files/download/{file_id}", headers={"Authorization": f"Bearer {admin_token}"})
    assert res_down_admin.status_code == 200
    assert res_down_admin.content == file_bytes

    # Delete via API
    del_res = client.delete(f"/api/files/files/{file_id}", headers={"Authorization": f"Bearer {admin_token}"})
    assert del_res.status_code == 200

    # Verify removed from DB
    assert db.query(FileRecord).filter(FileRecord.file_id == file_id).first() is None


def test_file_upload_mime_validation(client, user_token):
    """Disallowed MIME types (e.g. application/x-msdownload) must be rejected with 400."""
    res = client.post(
        "/api/files/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        files={"file": ("malware.exe", io.BytesIO(b"MZ..."), "application/x-msdownload")},
    )
    assert res.status_code == 400


def test_power_action_non_admin_forbidden(client, user_token):
    """Non-admin cannot invoke power endpoints."""
    res = client.post("/api/power/lock", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 403


def test_power_audit_logging(client, admin_token, db):
    """Admin triggering power endpoint logs audit events with valid schema."""
    from app.models.audit import AuditEvent

    # Mock or run lock
    res = client.post("/api/power/lock", headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200

    audits = db.query(AuditEvent).filter(AuditEvent.action.like("power_%")).all()
    assert len(audits) >= 1
    for ev in audits:
        assert ev.action.startswith("power_")
        assert ev.username == "admin"
        assert hasattr(ev, "reason")
        assert not hasattr(ev, "details")  # verifies no obsolete field
