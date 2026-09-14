"""
tests/test_concurrency.py — Automated concurrency, shared capture lifecycle, and control arbitration tests.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch, MagicMock
import pytest
from app.services.screen_track import ScreenTrack, capture_hub, MonitorCaptureWorker
from app.services.state import ControlArbitrationManager, pcs


@pytest.mark.anyio
async def test_shared_capture_hub_lifecycle():
    """Verify exactly 1 capture thread per monitor shared across N viewers, and 0 when idle."""
    # Ensure capture hub starts clean
    worker = capture_hub.get_worker(1)
    
    # Mock mss.mss to avoid actual screen grabbing during unit test
    mock_sct = MagicMock()
    mock_sct.monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}, {"left": 0, "top": 0, "width": 1920, "height": 1080}]
    mock_grab = MagicMock()
    mock_grab.bgra = b"\x00" * (1920 * 1080 * 4)
    mock_grab.width = 1920
    mock_grab.height = 1080
    mock_sct.grab.return_value = mock_grab

    with patch("mss.mss", return_value=mock_sct):
        # 1. Zero viewers
        assert worker.subscriber_count == 0

        # 2. First viewer subscribes
        track1 = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        assert worker.subscriber_count == 1
        assert worker.is_running is True
        assert capture_hub.get_active_worker_count() == 1

        # 3. Second viewer subscribes on same monitor
        track2 = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
        assert worker.subscriber_count == 2
        # Worker thread count remains 1 for this monitor
        assert capture_hub.get_active_worker_count() == 1

        # 4. First viewer disconnects/stops
        track1.stop()
        assert worker.subscriber_count == 1
        assert worker.is_running is True

        # 5. Second viewer disconnects/stops
        track2.stop()
        assert worker.subscriber_count == 0
        # Allow small window for thread loop to exit
        time.sleep(0.1)
        assert worker.is_running is False
        assert capture_hub.get_active_worker_count() == 0


@pytest.mark.anyio
async def test_control_arbitration_exclusive_locking():
    """Verify exclusive control token: User A acquires, User B rejected, User A releases, User B acquires."""
    mgr = ControlArbitrationManager()

    # 1. User A acquires control
    success, status, ctrl = await mgr.acquire_control(user_id=1, username="user_a", ws_id="ws-1")
    assert success is True
    assert status == "granted"
    assert ctrl["username"] == "user_a"
    assert mgr.is_user_controlling(user_id=1, ws_id="ws-1") is True
    assert mgr.is_user_controlling(user_id=2, ws_id="ws-2") is False

    # 2. User B attempts to acquire control -> rejected
    success, status, ctrl = await mgr.acquire_control(user_id=2, username="user_b", ws_id="ws-2")
    assert success is False
    assert status == "busy"
    assert ctrl["username"] == "user_a"
    assert mgr.is_user_controlling(user_id=2, ws_id="ws-2") is False

    # 3. User A releases control
    released, info = await mgr.release_control(user_id=1, ws_id="ws-1")
    assert released is True
    assert info["username"] == "user_a"
    assert mgr.get_active_controller_info() is None

    # 4. User B now acquires control successfully
    success, status, ctrl = await mgr.acquire_control(user_id=2, username="user_b", ws_id="ws-2")
    assert success is True
    assert status == "granted"
    assert ctrl["username"] == "user_b"
    assert mgr.is_user_controlling(user_id=2, ws_id="ws-2") is True


@pytest.mark.anyio
async def test_control_arbitration_disconnect_cleanup():
    """Verify disconnecting WebSocket automatically releases control token."""
    mgr = ControlArbitrationManager()

    # User A connects and acquires control
    await mgr.register_socket("ws-user-a", MagicMock())
    success, status, _ = await mgr.acquire_control(user_id=10, username="user_a", ws_id="ws-user-a")
    assert success is True
    assert mgr.get_active_controller_info()["username"] == "user_a"

    # User A socket disconnects
    released = await mgr.unregister_socket("ws-user-a", user_id=10)
    assert released is not None
    assert released["username"] == "user_a"
    assert mgr.get_active_controller_info() is None

    # User B can immediately acquire control
    success, status, _ = await mgr.acquire_control(user_id=20, username="user_b", ws_id="ws-user-b")
    assert success is True
    assert status == "granted"


@pytest.mark.anyio
async def test_admin_takeover_preemption():
    """Verify admin user can preempt/take over control token from regular user."""
    mgr = ControlArbitrationManager()

    # User A acquires control
    await mgr.acquire_control(user_id=1, username="regular_user", ws_id="ws-1", is_admin=False)
    assert mgr.get_active_controller_info()["username"] == "regular_user"

    # Admin requests takeover
    success, status, ctrl = await mgr.acquire_control(
        user_id=99, username="admin_user", ws_id="ws-admin", is_admin=True, is_admin_takeover=True
    )
    assert success is True
    assert status == "granted"
    assert ctrl["username"] == "admin_user"
    assert mgr.is_user_controlling(user_id=99, ws_id="ws-admin") is True
    assert mgr.is_user_controlling(user_id=1, ws_id="ws-1") is False


def test_stream_info_multi_client_telemetry(client, user_token):
    """Verify GET /api/stream/info includes active controller and capture workers count."""
    headers = {"Authorization": f"Bearer {user_token}"}
    res = client.get("/api/stream/info", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "active_peers" in data
    assert "active_controller" in data
    assert "capture_workers_active" in data
