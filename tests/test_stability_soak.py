"""
tests/test_stability_soak.py — Automated stability, churn, and lifecycle soak tests.
"""
from __future__ import annotations

import asyncio
import gc
import threading
import time
from unittest.mock import MagicMock, patch
import pytest
from app.services.screen_track import ScreenTrack, capture_hub
from app.services.state import ControlArbitrationManager


@pytest.mark.anyio
async def test_rapid_connect_disconnect_churn():
    """
    Verify 50 sequential connect -> fetch frame -> disconnect cycles.
    Confirms reference counts and active worker threads return to 0 after every cycle
    without leaking threads or memory references.
    """
    mock_sct = MagicMock()
    mock_sct.monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
    ]
    mock_grab = MagicMock()
    mock_grab.bgra = b"\x00" * (1920 * 1080 * 4)
    mock_grab.width = 1920
    mock_grab.height = 1080
    mock_sct.grab.return_value = mock_grab

    with patch("mss.mss", return_value=mock_sct):
        worker = capture_hub.get_worker(1)
        initial_threads = threading.active_count()

        for cycle in range(50):
            track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
            assert worker.subscriber_count == 1
            assert capture_hub.get_active_worker_count() == 1

            # Fetch a frame
            frame = await track.recv()
            assert frame is not None

            # Disconnect
            track.stop()
            assert worker.subscriber_count == 0

        # Allow brief moment for final worker thread to join
        await asyncio.sleep(0.1)
        assert worker.is_running is False
        assert capture_hub.get_active_worker_count() == 0
        gc.collect()
        
        # Verify thread count hasn't ballooned
        assert abs(threading.active_count() - initial_threads) <= 1


@pytest.mark.anyio
async def test_multi_viewer_staggered_churn():
    """
    Verify complex overlapping lifecycles across 4 viewers:
    A joins -> B joins -> C joins -> B leaves -> D joins -> A leaves -> C leaves -> D leaves.
    Verifies worker stays alive continuously while subscribers >= 1 and stops only at 0.
    """
    mock_sct = MagicMock()
    mock_sct.monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
    ]
    mock_grab = MagicMock()
    mock_grab.bgra = b"\x00" * (1920 * 1080 * 4)
    mock_grab.width = 1920
    mock_grab.height = 1080
    mock_sct.grab.return_value = mock_grab

    with patch("mss.mss", return_value=mock_sct):
        worker = capture_hub.get_worker(1)
        assert worker.subscriber_count == 0

        # 1. Viewer A connects
        track_a = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        assert worker.subscriber_count == 1
        assert worker.is_running is True

        # 2. Viewer B connects
        track_b = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
        assert worker.subscriber_count == 2
        assert capture_hub.get_active_worker_count() == 1

        # 3. Viewer C connects
        track_c = ScreenTrack(monitor_index=1, width=960, height=540, fps=20)
        assert worker.subscriber_count == 3
        assert capture_hub.get_active_worker_count() == 1

        # 4. Viewer B departs
        track_b.stop()
        assert worker.subscriber_count == 2
        assert worker.is_running is True

        # 5. Viewer D connects
        track_d = ScreenTrack(monitor_index=1, width=1280, height=800, fps=30)
        assert worker.subscriber_count == 3
        assert worker.is_running is True

        # 6. Viewer A departs
        track_a.stop()
        assert worker.subscriber_count == 2
        assert worker.is_running is True

        # 7. Viewer C departs
        track_c.stop()
        assert worker.subscriber_count == 1
        assert worker.is_running is True

        # 8. Final Viewer D departs
        track_d.stop()
        assert worker.subscriber_count == 0
        await asyncio.sleep(0.1)
        assert worker.is_running is False
        assert capture_hub.get_active_worker_count() == 0


@pytest.mark.anyio
async def test_control_token_heavy_churn():
    """
    Verify 100 iterations of rapid concurrent control contention, preemption, and disconnects.
    Proves zero lock leaks or orphaned controller references.
    """
    mgr = ControlArbitrationManager()

    class MockWS:
        def __init__(self, id_):
            self.id = id_
            self.sent = []
        async def send_json(self, data):
            self.sent.append(data)

    for i in range(100):
        ws_a = MockWS(f"ws-a-{i}")
        ws_b = MockWS(f"ws-b-{i}")
        await mgr.register_socket(f"ws-a-{i}", ws_a)
        await mgr.register_socket(f"ws-b-{i}", ws_b)

        # User A acquires
        ok, status, _ = await mgr.acquire_control(user_id=1, username="user_a", ws_id=f"ws-a-{i}")
        assert ok is True
        assert status == "granted"

        # User B rejected
        ok, status, _ = await mgr.acquire_control(user_id=2, username="user_b", ws_id=f"ws-b-{i}")
        assert ok is False
        assert status == "busy"

        # User A disconnects
        released = await mgr.unregister_socket(f"ws-a-{i}", user_id=1)
        assert released is not None
        assert mgr.get_active_controller_info() is None

        # User B now acquires
        ok, status, _ = await mgr.acquire_control(user_id=2, username="user_b", ws_id=f"ws-b-{i}")
        assert ok is True
        assert status == "granted"

        # User B releases normally
        released_ok, _ = await mgr.release_control(user_id=2, ws_id=f"ws-b-{i}")
        assert released_ok is True
        await mgr.unregister_socket(f"ws-b-{i}", user_id=2)
        assert mgr.get_active_controller_info() is None

    assert len(mgr._connected_sockets) == 0
    assert mgr.active_controller is None


@pytest.mark.anyio
async def test_capture_worker_crash_recovery():
    """Verify capture worker gracefully survives transient grab errors."""
    mock_sct = MagicMock()
    mock_sct.monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
    ]
    # First call raises an exception, second call succeeds
    mock_grab = MagicMock()
    mock_grab.bgra = b"\x00" * (1920 * 1080 * 4)
    mock_grab.width = 1920
    mock_grab.height = 1080
    mock_sct.grab.side_effect = [RuntimeError("Transient DirectX/GDI error"), mock_grab]

    with patch("mss.mss", return_value=mock_sct):
        worker = capture_hub.get_worker(1)
        track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        
        # Wait a moment for worker to catch error and retry
        await asyncio.sleep(0.1)
        frame = await track.recv()
        assert frame is not None
        assert worker.is_running is True

        track.stop()
        await asyncio.sleep(0.1)
        assert worker.is_running is False
