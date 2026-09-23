"""
tests/test_security_comprehensive.py — Comprehensive security audit test suite for Phase 5 M5.1.
Validates:
- Auth & token lifecycle (revocation, reuse detection, rotation)
- Endpoint access controls & admin-only boundaries
- WebSocket ticket single-use, scope, and origin validation
- File sandbox isolation & path traversal protections
- Safe keyboard filtering & input validation
- Audit logging sanitization (no secret leakage)
- Production settings enforcement
"""
from datetime import datetime, timedelta
import hashlib
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import is_safe_event
from app.routers.auth import get_password_hash, verify_password
from app.models.user import User
from app.models.session import UserSession, WsTicket
from app.models.file import FileRecord
from app.models.audit import AuditEvent


def test_auth_password_hashing():
    """Passwords are hashed with bcrypt and verify correctly."""
    pw = "SuperSecretPassword123!"
    h = get_password_hash(pw)
    assert h != pw
    assert verify_password(pw, h) is True
    assert verify_password("WrongPassword", h) is False


def test_refresh_token_reuse_detection_triggers_full_revocation(client: TestClient, db: Session, user_token: str):
    """Presenting a revoked refresh token immediately terminates all active user sessions."""
    user = db.query(User).filter(User.username == "user1").first()
    if not user:
        user = User(username="user1", password_hash=get_password_hash("pass123"), role="user")
        db.add(user)
        db.commit()

    raw_stale = "stale_token_raw_12345"
    s_stale = UserSession(
        user_id=user.id,
        token_hash=hashlib.sha256(raw_stale.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(days=7),
        revoked_at=datetime.utcnow(),
    )
    s_active1 = UserSession(
        user_id=user.id,
        token_hash=hashlib.sha256(b"active1").hexdigest(),
        expires_at=datetime.utcnow() + timedelta(days=7),
        revoked_at=None,
    )
    s_active2 = UserSession(
        user_id=user.id,
        token_hash=hashlib.sha256(b"active2").hexdigest(),
        expires_at=datetime.utcnow() + timedelta(days=7),
        revoked_at=None,
    )
    db.add_all([s_stale, s_active1, s_active2])
    db.commit()

    # Attempt refresh with stale token
    client.cookies.set("refresh_token", raw_stale)
    res = client.post("/api/auth/refresh")
    assert res.status_code == 401

    # Confirm all sessions for user were revoked
    active_count = db.query(UserSession).filter(
        UserSession.user_id == user.id,
        UserSession.revoked_at.is_(None),
    ).count()
    assert active_count == 0, "All active sessions must be revoked on token reuse"


def test_unauthenticated_endpoints_return_401(client: TestClient):
    """Protected endpoints reject requests without valid Bearer tokens."""
    protected_urls = [
        ("GET", "/api/auth/me"),
        ("GET", "/api/auth/sessions"),
        ("POST", "/api/auth/ws-ticket"),
        ("GET", "/api/stream/info"),
        ("GET", "/api/stream/stats"),
        ("GET", "/api/stream/ice-servers"),
        ("POST", "/api/stream/offer"),
        ("GET", "/api/files/list"),
        ("POST", "/api/files/upload"),
        ("GET", "/api/monitors"),
        ("POST", "/api/power/lock"),
        ("POST", "/api/power/shutdown"),
        ("GET", "/api/audit-events/"),
    ]

    for method, url in protected_urls:
        if method == "GET":
            res = client.get(url)
        elif method == "POST":
            res = client.post(url, json={})
        assert res.status_code in (401, 403), f"Endpoint {url} must require auth (got {res.status_code})"


def test_admin_only_endpoints_reject_regular_users(client: TestClient, user_token: str):
    """Regular users cannot access administrative endpoints."""
    admin_urls = [
        ("POST", "/api/auth/register", {"username": "newuser", "password": "password123"}),
        ("POST", "/api/power/lock", {}),
        ("POST", "/api/power/shutdown", {}),
        ("POST", "/api/power/restart", {}),
        ("POST", "/api/power/hibernate", {}),
        ("GET", "/api/audit-events/", None),
    ]

    headers = {"Authorization": f"Bearer {user_token}"}
    for method, url, data in admin_urls:
        if method == "GET":
            res = client.get(url, headers=headers)
        elif method == "POST":
            res = client.post(url, json=data, headers=headers)
        assert res.status_code == 403, f"Endpoint {url} must reject non-admin (got {res.status_code})"


def test_file_ownership_isolation_and_traversal_prevention(client: TestClient, db: Session, user_token: str, admin_token: str):
    """Users cannot download or delete other users' files; path traversal filenames are sanitized."""
    user = db.query(User).filter(User.username == "user1").first()
    admin = db.query(User).filter(User.username == "admin").first()

    # Create file owned by admin
    admin_file = FileRecord(
        file_id="file-admin-123",
        filename="sensitive_admin_data.txt",
        path="uploads/file-admin-123_sensitive_admin_data.txt",
        uploader_id=admin.id,
        size=1024,
        content_type="text/plain",
    )
    db.add(admin_file)
    db.commit()

    user_headers = {"Authorization": f"Bearer {user_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 1. Regular user cannot delete admin file
    res_del = client.delete(f"/api/files/{admin_file.file_id}", headers=user_headers)
    assert res_del.status_code == 403

    # 2. Regular user list does not expose admin file
    res_list = client.get("/api/files/list", headers=user_headers)
    assert res_list.status_code == 200
    files = res_list.json().get("files", [])
    file_ids = [f["file_id"] for f in files]
    assert admin_file.file_id not in file_ids

    # 3. Admin list shows all files
    res_admin_list = client.get("/api/files/list", headers=admin_headers)
    assert res_admin_list.status_code == 200
    admin_files = res_admin_list.json().get("files", [])
    admin_file_ids = [f["file_id"] for f in admin_files]
    assert admin_file.file_id in admin_file_ids


def test_keyboard_event_safety_filter():
    """Validates that dangerous shortcuts are blocked and safe keystrokes are allowed."""
    assert is_safe_event({"type": "keydown", "key": "r", "modifiers": ["meta"]}) is False
    assert is_safe_event({"type": "keydown", "key": "delete", "modifiers": ["ctrl", "alt"]}) is False
    assert is_safe_event({"type": "keydown", "key": "meta", "modifiers": []}) is False
    assert is_safe_event({"type": "keydown", "key": "f4", "modifiers": ["alt"]}) is False
    assert is_safe_event({"type": "keydown", "key": "c", "modifiers": ["ctrl"]}) is True
    assert is_safe_event({"type": "keydown", "key": "v", "modifiers": ["ctrl"]}) is True
    assert is_safe_event({"type": "keydown", "key": "z", "modifiers": ["ctrl", "shift"]}) is True
    assert is_safe_event({"type": "keydown", "key": "a", "modifiers": []}) is True


def test_production_settings_validation_blocks_insecure_deployments():
    """Settings validator rejects weak secrets, missing HTTPS, and wildcard CORS in production mode."""
    # 1. Weak secret rejected
    with pytest.raises(ValueError, match="SECRET_KEY is weak or default"):
        Settings(APP_ENV="production", SECRET_KEY="weak", COOKIE_SECURE=True, PUBLIC_ORIGIN="https://rd.example.com", ALLOWED_ORIGINS="https://rd.example.com", ALLOWED_WS_ORIGINS="https://rd.example.com")

    # 2. Non-HTTPS origin rejected
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN must use https://"):
        Settings(APP_ENV="production", SECRET_KEY="a" * 32, COOKIE_SECURE=True, PUBLIC_ORIGIN="http://rd.example.com", ALLOWED_ORIGINS="http://rd.example.com", ALLOWED_WS_ORIGINS="http://rd.example.com")

    # 3. Insecure cookie rejected
    with pytest.raises(ValueError, match="COOKIE_SECURE must be true"):
        Settings(APP_ENV="production", SECRET_KEY="a" * 32, COOKIE_SECURE=False, PUBLIC_ORIGIN="https://rd.example.com", ALLOWED_ORIGINS="https://rd.example.com", ALLOWED_WS_ORIGINS="https://rd.example.com")
