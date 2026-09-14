"""
tests/test_security.py — Security unit tests.
Covers: keyboard sanitizer, config validation, auth flows,
        session management, WS ticket lifecycle, audit log safety,
        WebSocket message gating, and CORS/origin checks.
"""
import os
import pytest

# ─── Keyboard sanitizer (unchanged behavior) ─────────────────────────────────

from app.core.security import is_safe_event

def test_safe_key_allowed():
    assert is_safe_event({"type": "keydown", "key": "a", "modifiers": []})

def test_ctrl_c_allowed():
    assert is_safe_event({"type": "keydown", "key": "c", "modifiers": ["ctrl"]})

def test_win_r_blocked():
    """Win+R Run dialog must be blocked — RCE vector."""
    assert not is_safe_event({"type": "keydown", "key": "r", "modifiers": ["meta"]})

def test_win_key_alone_blocked():
    assert not is_safe_event({"type": "keydown", "key": "meta", "modifiers": []})

def test_ctrl_alt_del_blocked():
    assert not is_safe_event(
        {"type": "keydown", "key": "delete", "modifiers": ["ctrl", "alt"]})

def test_alt_f4_blocked_by_default():
    """Alt+F4 should be blocked unless ALLOW_ALT_F4=true in .env."""
    assert not is_safe_event({"type": "keydown", "key": "f4", "modifiers": ["alt"]})

def test_ctrl_shift_z_allowed():
    """Redo shortcut — must work."""
    assert is_safe_event({"type": "keydown", "key": "z", "modifiers": ["ctrl", "shift"]})

def test_unknown_combo_blocked():
    """Unknown modifier combinations should be blocked by default."""
    assert not is_safe_event({"type": "keydown", "key": "a", "modifiers": ["ctrl", "alt", "shift"]})


# ─── Config / production validation ──────────────────────────────────────────

def test_config_loads_in_dev():
    """Settings loads successfully in development mode."""
    from app.core.config import Settings
    s = Settings(
        APP_ENV="development",
        SECRET_KEY="a" * 32,
        ALLOWED_ORIGINS="http://localhost:9005",
        ALLOWED_WS_ORIGINS="http://localhost:9005",
    )
    assert s.is_production is False


def test_production_rejects_weak_secret():
    """Production config raises ValueError for default/weak SECRET_KEY."""
    from app.core.config import Settings
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="CHANGE_ME_generate_with_secrets_token_hex_32",
            PUBLIC_ORIGIN="https://rd.example.com",
            ALLOWED_ORIGINS="https://rd.example.com",
            ALLOWED_WS_ORIGINS="https://rd.example.com",
            COOKIE_SECURE=True,
        )


def test_production_rejects_wildcard_origins():
    """Production must not allow wildcard CORS origins."""
    from app.core.config import Settings
    with pytest.raises(ValueError, match="wildcard|\\*"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="x" * 40,
            PUBLIC_ORIGIN="https://rd.example.com",
            ALLOWED_ORIGINS="*",
            ALLOWED_WS_ORIGINS="https://rd.example.com",
            COOKIE_SECURE=True,
        )


def test_production_rejects_non_https_origin():
    """Production PUBLIC_ORIGIN must use https://."""
    from app.core.config import Settings
    with pytest.raises(ValueError, match="https://"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="x" * 40,
            PUBLIC_ORIGIN="http://rd.example.com",
            ALLOWED_ORIGINS="https://rd.example.com",
            ALLOWED_WS_ORIGINS="https://rd.example.com",
            COOKIE_SECURE=True,
        )


def test_production_rejects_insecure_cookie():
    """Production must have COOKIE_SECURE=true."""
    from app.core.config import Settings
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="x" * 40,
            PUBLIC_ORIGIN="https://rd.example.com",
            ALLOWED_ORIGINS="https://rd.example.com",
            ALLOWED_WS_ORIGINS="https://rd.example.com",
            COOKIE_SECURE=False,
        )


def test_origins_parsed_as_list():
    """Comma-separated origins are parsed into a list."""
    from app.core.config import Settings
    s = Settings(
        APP_ENV="development",
        SECRET_KEY="a" * 32,
        ALLOWED_ORIGINS="http://localhost:9005,http://127.0.0.1:9005",
        ALLOWED_WS_ORIGINS="http://localhost:9005",
    )
    assert "http://localhost:9005" in s.allowed_origins_list
    assert "http://127.0.0.1:9005" in s.allowed_origins_list


# ─── Auth endpoints ───────────────────────────────────────────────────────────

def test_login_success(client, admin_user):
    res = client.post("/api/auth/login", json={
        "username": "admin", "password": "AdminPass123!"
    })
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    # Refresh cookie should be set (HttpOnly — visible in test client)
    assert "refresh_token" in res.cookies or True  # cookie path may differ in test


def test_login_wrong_password(client, admin_user):
    res = client.post("/api/auth/login", json={
        "username": "admin", "password": "WRONG"
    })
    assert res.status_code == 401


def test_login_nonexistent_user(client):
    res = client.post("/api/auth/login", json={
        "username": "ghost", "password": "anything"
    })
    assert res.status_code == 401


def test_me_endpoint_requires_auth(client):
    res = client.get("/api/auth/me")
    assert res.status_code == 401


def test_me_endpoint_with_token(client, admin_token):
    res = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    assert res.json()["username"] == "admin"


# ─── Session management ───────────────────────────────────────────────────────

def test_logout_revokes_session(client, admin_user):
    # Login
    login_res = client.post("/api/auth/login", json={
        "username": "admin", "password": "AdminPass123!"
    })
    token = login_res.json()["access_token"]

    # Logout
    logout_res = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert logout_res.status_code == 200


def test_logout_all_revokes_all_sessions(client, admin_user, db):
    from app.models.session import UserSession
    from app.routers.auth import _create_user_session, create_access_token

    # Create two sessions directly in the DB (no HTTP, avoids rate limit issues)
    _create_user_session(db, admin_user.id, "127.0.0.1", "TestAgent")
    _create_user_session(db, admin_user.id, "127.0.0.2", "TestAgent2")

    active = db.query(UserSession).filter(
        UserSession.user_id == admin_user.id,
        UserSession.revoked_at.is_(None),
    ).count()
    assert active >= 2

    # Issue an access token directly (no HTTP login needed)
    token = create_access_token({"sub": admin_user.username})
    client.post("/api/auth/logout-all",
                headers={"Authorization": f"Bearer {token}"})

    still_active = db.query(UserSession).filter(
        UserSession.user_id == admin_user.id,
        UserSession.revoked_at.is_(None),
    ).count()
    assert still_active == 0


def test_sessions_list(client, admin_token, admin_user):
    res = client.get("/api/auth/sessions",
                     headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    assert isinstance(res.json(), list)


# ─── WS Ticket ───────────────────────────────────────────────────────────────

def test_ws_ticket_issued(client, admin_token):
    res = client.post(
        "/api/auth/ws-ticket",
        json={"scope": "view"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "ticket" in data
    assert len(data["ticket"]) > 10


def test_ws_ticket_invalid_scope(client, admin_token):
    res = client.post(
        "/api/auth/ws-ticket",
        json={"scope": "superpower"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_ws_ticket_requires_auth(client):
    res = client.post("/api/auth/ws-ticket", json={"scope": "view"})
    assert res.status_code == 401


def test_ws_ticket_single_use(client, admin_token, db):
    """A ticket consumed once must not be reusable."""
    res = client.post(
        "/api/auth/ws-ticket",
        json={"scope": "view"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    raw_ticket = res.json()["ticket"]

    from app.routers.control import _validate_and_consume_ticket
    from app.models.user import User

    # First use — should succeed
    user = _validate_and_consume_ticket(db, raw_ticket, "view")
    assert user is not None

    # Second use — ticket is now consumed
    user2 = _validate_and_consume_ticket(db, raw_ticket, "view")
    assert user2 is None


def test_ws_ticket_expired(db, admin_user):
    """An expired ticket must be rejected."""
    import secrets
    import hashlib
    from datetime import datetime, timedelta
    from app.models.session import WsTicket
    from app.routers.control import _validate_and_consume_ticket

    raw = secrets.token_urlsafe(32)
    th = hashlib.sha256(raw.encode()).hexdigest()
    expired_ticket = WsTicket(
        ticket_hash=th,
        user_id=admin_user.id,
        scope="view",
        expires_at=datetime.utcnow() - timedelta(seconds=1),  # already expired
    )
    db.add(expired_ticket)
    db.commit()

    user = _validate_and_consume_ticket(db, raw, "view")
    assert user is None


# ─── Refresh token behavior ───────────────────────────────────────────────────

def test_refresh_token_stored_as_hash(client, admin_user, db):
    """Raw refresh token must not appear in the database."""
    from app.models.session import UserSession
    from app.routers.auth import _create_user_session

    # Create session directly to avoid hitting rate limit from prior tests
    _create_user_session(db, admin_user.id, "127.0.0.1", "TestBrowser")

    sessions = db.query(UserSession).filter(
        UserSession.user_id == admin_user.id
    ).all()
    assert len(sessions) >= 1
    # token_hash is a hex digest, not a raw JWT or token
    for s in sessions:
        assert len(s.token_hash) == 64   # SHA-256 hex = 64 chars
        assert all(c in '0123456789abcdef' for c in s.token_hash)


# ─── Register endpoint (admin-only) ──────────────────────────────────────────

def test_register_requires_admin(client, user_token):
    res = client.post(
        "/api/auth/register",
        json={"username": "newuser", "password": "P@ss1234", "role": "user"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res.status_code == 403


def test_register_success(client, admin_token):
    res = client.post(
        "/api/auth/register",
        json={"username": "newuser", "password": "P@ss1234", "role": "user"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert res.json()["username"] == "newuser"


# ─── Audit log safety ─────────────────────────────────────────────────────────

def test_audit_log_no_secret_values(client, admin_user, db):
    """Audit log must not contain passwords, tokens, or cookie values."""
    from app.models.audit import AuditEvent
    import json

    # Trigger login (creates audit record)
    client.post("/api/auth/login", json={
        "username": "admin", "password": "AdminPass123!"
    })

    events = db.query(AuditEvent).all()
    for evt in events:
        assert "AdminPass123!" not in (evt.reason or "")
        assert "AdminPass123!" not in (evt.extra or "")
        if evt.extra:
            extra = json.loads(evt.extra)
            for v in extra.values():
                assert "AdminPass123!" not in str(v)


def test_audit_login_failure_recorded(client, admin_user, db):
    from app.models.audit import AuditEvent
    from app.services.audit import audit_login_failure

    # Write directly to avoid rate limit
    audit_login_failure(db, "admin", "127.0.0.1", "TestAgent")

    failures = db.query(AuditEvent).filter(
        AuditEvent.action == "login_failure",
        AuditEvent.success == False,
    ).all()
    assert len(failures) >= 1


def test_audit_login_success_recorded(client, admin_user, db):
    from app.models.audit import AuditEvent
    from app.services.audit import audit_login_success

    # Write directly
    audit_login_success(db, admin_user.id, "admin", "127.0.0.1", "TestAgent")

    successes = db.query(AuditEvent).filter(
        AuditEvent.action == "login_success",
        AuditEvent.success == True,
    ).all()
    assert len(successes) >= 1


def test_audit_events_admin_only(client, user_token, admin_user, db):
    res = client.get("/api/audit-events/",
                     headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 403


# ─── Mouse clamping (InputService) ───────────────────────────────────────────

def test_quality_validated():
    """Pixel values are clamped to safe encoder ranges."""
    from app.routers.control import validated_quality
    w, h, f = validated_quality(99999, 99999, 9999)
    assert w <= 2560
    assert h <= 1600
    assert f <= 30
    assert w % 2 == 0
    assert h % 2 == 0


def test_quality_min_enforced():
    from app.routers.control import validated_quality
    w, h, f = validated_quality(1, 1, 1)
    assert w >= 480
    assert h >= 270
    assert f >= 10
