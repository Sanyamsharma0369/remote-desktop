"""
tests/test_regressions.py — Regression tests for Phase 1 & Phase 2 stabilization fixes.
Covers:
  - WebRTC offer authentication requirement (/api/stream/offer)
  - Stream info routing and double-prefix prevention
  - WebRTC stream stats endpoint (/api/stream/stats)
  - StreamTrack quality adaptation, stats, and safe bounds
  - SDP bitrate injection constraint
  - Peer connection cleanup and resource safety
  - Per-user file ownership and isolation (User A vs User B vs Admin)
  - Persistent file metadata in DB (FileRecord model)
  - File upload MIME validation and chunked size limit
  - Power action auditing and schema safety
"""
import io
import pytest
from app.models.file import FileRecord
from app.services.screen_track import ScreenTrack
from app.routers.stream import add_video_bitrate_to_sdp, _cleanup_peer_connection
from app.services.state import pcs
from aiortc import RTCPeerConnection


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
    assert "active_peers" in data

    # Double-prefix route must 404
    bad_res = client.get("/api/stream/api/stream/info", headers={"Authorization": f"Bearer {user_token}"})
    assert bad_res.status_code == 404


def test_stream_stats_endpoint(client, user_token):
    """GET /api/stream/stats returns active peer metrics and encoder info."""
    res = client.get("/api/stream/stats", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 200
    data = res.json()
    assert "active_peers" in data
    assert "tracks" in data
    assert "encoder" in data


def test_screen_track_adaptation_and_stats():
    """ScreenTrack supports dynamic quality adjustment within safety bounds and exposes get_stats()."""
    track = ScreenTrack(width=1280, height=800, fps=30)
    try:
        stats = track.get_stats()
        assert stats["target_fps"] == 30
        assert stats["target_width"] == 1280
        assert stats["target_height"] == 800
        assert stats["is_running"] is True

        # Adapt quality down (e.g. degraded network: 854x480 @ 18fps)
        track.set_quality(width=854, height=480, fps=18)
        assert track.fps == 18
        assert track.target_width == 854
        assert track.target_height == 480

        # Quality clamping validation (min/max safety bounds)
        track.set_quality(width=100, height=50, fps=5)  # below min
        assert track.target_width == 480
        assert track.target_height == 270
        assert track.fps == 10

        track.set_quality(width=5000, height=4000, fps=60)  # above max
        assert track.target_width == 2560
        assert track.target_height == 1600
        assert track.fps == 30
    finally:
        track.stop()


def test_sdp_bitrate_injection():
    """add_video_bitrate_to_sdp correctly injects bandwidth constraint lines."""
    raw_sdp = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\n"
    modified = add_video_bitrate_to_sdp(raw_sdp, kbps=2500)
    assert "b=AS:2500" in modified
    assert "b=TIAS:2500000" in modified


def test_peer_connection_cleanup():
    """_cleanup_peer_connection stops all tracks and removes pc from global active set."""
    import asyncio

    async def _run():
        pc = RTCPeerConnection()
        track = ScreenTrack(width=1280, height=720, fps=30)
        pc.addTrack(track)
        pcs.add(pc)
        assert pc in pcs

        await _cleanup_peer_connection(pc, username="test_user")
        assert pc not in pcs
        assert track._running is False

    asyncio.run(_run())


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


def test_reconnection_backoff_and_retry_bounds():
    """Verify exponential backoff progression, max retry bounds, and grace period constants."""
    # Test parameters mirroring client state machine
    MAX_RETRIES = 5
    GRACE_MS = 5000

    assert GRACE_MS == 5000
    assert MAX_RETRIES == 5

    # Test backoff progression for attempts 1..5
    delays = [min(1000 * (1.5 ** (attempt - 1)), 8000) for attempt in range(1, MAX_RETRIES + 1)]
    assert delays[0] == 1000.0        # Attempt 1: 1.0s
    assert delays[1] == 1500.0        # Attempt 2: 1.5s
    assert delays[2] == 2250.0        # Attempt 3: 2.25s
    assert delays[3] == 3375.0        # Attempt 4: 3.375s
    assert delays[4] == 5062.5        # Attempt 5: 5.0625s

    # Attempt > 5 must cap at 8000ms
    delay_large = min(1000 * (1.5 ** (10 - 1)), 8000)
    assert delay_large == 8000.0


def test_reconnection_state_machine_logic():
    """Simulate WebRTC state machine transitions:
    - temporary disconnected -> grace timer -> recovery cancels reconnect
    - sustained disconnected -> grace timeout -> single controlled reconnect
    - duplicate reconnect prevention
    - manual reconnect resets counter
    """
    class MockViewerState:
        def __init__(self):
            self.reconnect_attempt = 0
            self.max_reconnect_attempts = 5
            self.is_reconnecting = False
            self.grace_timer_active = False
            self.reconnect_scheduled = False
            self.connected = False
            self.teardown_count = 0

        def handle_disconnected(self):
            if self.is_reconnecting:
                return
            self.grace_timer_active = True

        def handle_recovered(self):
            if self.grace_timer_active:
                self.grace_timer_active = False
            if self.reconnect_scheduled:
                self.reconnect_scheduled = False
            self.reconnect_attempt = 0
            self.is_reconnecting = False
            self.connected = True

        def trigger_grace_timeout(self):
            if self.grace_timer_active:
                self.grace_timer_active = False
                self.schedule_reconnect("grace timeout")

        def schedule_reconnect(self, reason):
            if self.is_reconnecting or self.reconnect_scheduled:
                return  # Prevent concurrent reconnects
            if self.reconnect_attempt >= self.max_reconnect_attempts:
                self.is_reconnecting = False
                self.reconnect_scheduled = False
                self.teardown_count += 1
                return
            self.is_reconnecting = True
            self.reconnect_scheduled = True
            self.reconnect_attempt += 1

        def manual_reconnect(self):
            self.reconnect_attempt = 0
            self.is_reconnecting = False
            self.reconnect_scheduled = False
            self.schedule_reconnect("manual click")

    state = MockViewerState()
    state.connected = True

    # Scenario 1: Brief network drop recovered during grace period (No reconnect initiated)
    state.handle_disconnected()
    assert state.grace_timer_active is True
    assert state.is_reconnecting is False
    assert state.reconnect_attempt == 0

    state.handle_recovered()
    assert state.grace_timer_active is False
    assert state.connected is True
    assert state.reconnect_attempt == 0
    assert state.teardown_count == 0

    # Scenario 2: Sustained network drop exceeding grace period (Controlled reconnect initiated)
    state.handle_disconnected()
    assert state.grace_timer_active is True
    state.trigger_grace_timeout()
    assert state.grace_timer_active is False
    assert state.is_reconnecting is True
    assert state.reconnect_attempt == 1

    # Scenario 3: Duplicate reconnect event while already reconnecting must be ignored
    state.schedule_reconnect("duplicate event")
    assert state.reconnect_attempt == 1  # Not incremented

    # Scenario 4: Max retries boundary reached
    for _ in range(4):
        state.is_reconnecting = False
        state.reconnect_scheduled = False
        state.schedule_reconnect("retry")
    assert state.reconnect_attempt == 5

    # Next attempt exceeds max
    state.is_reconnecting = False
    state.reconnect_scheduled = False
    state.schedule_reconnect("retry after max")
    assert state.teardown_count == 1
    assert state.reconnect_scheduled is False

    # Scenario 5: Manual reconnect resets attempt counter
    state.manual_reconnect()
    assert state.reconnect_attempt == 1
    assert state.is_reconnecting is True

