"""
tests/test_matrix_validation.py — Cross-configuration matrix validation (DXGI/MSS x NVENC/CPU) & multi-monitor tests.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from app.services.screen_track import ScreenTrack, capture_hub
from app.services.encoder import HardwareH264Encoder, check_nvenc_available, get_active_encoder


@pytest.mark.anyio
async def test_matrix_mss_cpu_fallback():
    """Verify Quadrant 4: MSS Capture + CPU (libx264) Encoder."""
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

    mock_settings = MagicMock()
    mock_settings.CAPTURE_BACKEND = "mss"
    mock_settings.ENCODER = "cpu"

    with patch("app.services.screen_track.get_settings", return_value=mock_settings), \
         patch("app.services.encoder.get_settings", return_value=mock_settings), \
         patch("mss.mss", return_value=mock_sct):

        track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        worker = capture_hub.get_worker(1)
        assert worker.subscriber_count == 1

        frame = await track.recv()
        assert frame is not None
        assert frame.width == 1280
        assert frame.height == 720

        # Verify encoder can process frame in CPU mode
        encoder = HardwareH264Encoder()
        payloads, timestamp = encoder.encode(frame)
        assert isinstance(payloads, list)

        track.stop()
        await asyncio.sleep(0.1)
        assert worker.subscriber_count == 0


@pytest.mark.anyio
async def test_matrix_dxgi_nvenc():
    """Verify Quadrant 1: DXGI Capture + NVENC Encoder (mocked/real)."""
    mock_cam = MagicMock()
    mock_cam.grab.return_value = np.zeros((1080, 1920, 3), dtype=np.uint8)

    mock_settings = MagicMock()
    mock_settings.CAPTURE_BACKEND = "dxgi"
    mock_settings.ENCODER = "nvenc"

    with patch("app.services.screen_track.get_settings", return_value=mock_settings), \
         patch("dxcam.create", return_value=mock_cam):

        track = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
        worker = capture_hub.get_worker(1)
        assert worker.subscriber_count == 1

        frame = await track.recv()
        assert frame is not None
        assert frame.width == 1920
        assert frame.height == 1080
        assert worker.backend_name == "dxgi"

        track.stop()
        await asyncio.sleep(0.1)
        assert worker.subscriber_count == 0


@pytest.mark.anyio
async def test_multi_monitor_subscription_switching():
    """
    Verify dynamic monitor switching between Display 1 and Display 2:
    - Display 1 worker started on subscription
    - Track switches to Display 2 -> Display 1 worker terminates (0 subscribers)
    - Display 2 worker starts and runs
    - Track stops -> Display 2 worker terminates (0 subscribers)
    """
    mock_sct = MagicMock()
    mock_sct.monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},
    ]
    mock_grab = MagicMock()
    mock_grab.bgra = b"\x00" * (1920 * 1080 * 4)
    mock_grab.width = 1920
    mock_grab.height = 1080
    mock_sct.grab.return_value = mock_grab

    mock_settings = MagicMock()
    mock_settings.CAPTURE_BACKEND = "mss"

    with patch("app.services.screen_track.get_settings", return_value=mock_settings), \
         patch("mss.mss", return_value=mock_sct):

        w1 = capture_hub.get_worker(1)
        w2 = capture_hub.get_worker(2)

        # 1. Connect to Display 1
        track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        assert w1.subscriber_count == 1
        assert w2.subscriber_count == 0
        assert capture_hub.get_active_worker_count() == 1

        # 2. Switch to Display 2
        switched = track.set_monitor(2)
        assert switched is True
        assert w1.subscriber_count == 0
        assert w2.subscriber_count == 1
        await asyncio.sleep(0.1)
        assert w1.is_running is False
        assert w2.is_running is True
        assert capture_hub.get_active_worker_count() == 1

        # 3. Disconnect track
        track.stop()
        await asyncio.sleep(0.1)
        assert w1.subscriber_count == 0
        assert w2.subscriber_count == 0
        assert capture_hub.get_active_worker_count() == 0


@pytest.mark.anyio
async def test_dxgi_failure_fallback_to_mss():
    """Verify transparent fallback to MSS when DXGI raises COMError or exception."""
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

    mock_settings = MagicMock()
    mock_settings.CAPTURE_BACKEND = "auto"

    with patch("app.services.screen_track.get_settings", return_value=mock_settings), \
         patch("dxcam.create", side_effect=RuntimeError("DXGI_ERROR_UNSUPPORTED")), \
         patch("mss.mss", return_value=mock_sct):

        track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        worker = capture_hub.get_worker(1)
        assert worker.subscriber_count == 1

        frame = await track.recv()
        assert frame is not None
        assert worker.backend_name == "mss"

        track.stop()
        await asyncio.sleep(0.1)
        assert worker.subscriber_count == 0


@pytest.mark.anyio
async def test_linux_container_runtime_matrix():
    """Verify Linux container runtime behavior: DXGI unavailable -> MSS, windll no-op, CPU fallback."""
    from fractions import Fraction
    import av
    from app.core.windows_desktop import attach_interactive_desktop

    # 1. Non-Windows desktop attachment no-op
    with patch("platform.system", return_value="Linux"):
        attach_interactive_desktop()

    # 2. Linux Capture Fallback: DXGI -> MSS
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

    mock_settings = MagicMock()
    mock_settings.CAPTURE_BACKEND = "auto"
    mock_settings.ENCODER = "auto"

    with patch("app.services.screen_track.get_settings", return_value=mock_settings), \
         patch("dxcam.create", side_effect=ModuleNotFoundError("No module named 'dxcam'")), \
         patch("mss.mss", return_value=mock_sct), \
         patch("platform.system", return_value="Linux"):

        track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        worker = capture_hub.get_worker(1)
        frame = await track.recv()
        assert frame is not None
        assert worker.backend_name == "mss"
        track.stop()
        await asyncio.sleep(0.1)
        assert worker.subscriber_count == 0

    # 3. Linux Encoder Fallback: libx264
    with patch("app.services.encoder.check_nvenc_available", return_value=(False, "No GPU")):
        encoder = HardwareH264Encoder()
        test_img = np.zeros((720, 1280, 3), dtype=np.uint8)
        vframe = av.VideoFrame.from_ndarray(test_img, format="bgr24")
        vframe.pts = 0
        vframe.time_base = Fraction(1, 90000)
        payloads, _ = encoder.encode(vframe)
        assert encoder._current_encoder_name == "libx264"
        assert len(payloads) > 0

