"""
scratch/test_linux_docker_matrix.py — Phase 4 Track 4: Docker & Linux Environment Compatibility & Fallback Matrix.
"""
import asyncio
from fractions import Fraction
import os
import platform
import sys
from unittest.mock import MagicMock, patch
import numpy as np

from app.core.windows_desktop import attach_interactive_desktop
from app.services.screen_track import ScreenTrack, capture_hub
from app.services.encoder import (
    HardwareH264Encoder,
    check_nvenc_available,
    get_active_encoder,
    get_active_encoder_label,
)
from av import VideoFrame


async def test_linux_simulated_runtime():
    print("=" * 85)
    print("PHASE 4 TRACK 4: LINUX & DOCKER RUNTIME FALLBACK MATRIX VALIDATION")
    print("=" * 85)

    # 1. Test Windows-Desktop Attachment on Linux
    print("\n--- TEST 1: Windows Subsystem Attachment on Non-Windows ---")
    with patch("platform.system", return_value="Linux"):
        # Must be safe no-op without attempting ctypes.windll calls
        attach_interactive_desktop()
        print("  [PASSED] attach_interactive_desktop() cleanly no-ops when platform != Windows")

    # 2. Test Linux Capture Fallback: DXGI -> MSS
    print("\n--- TEST 2: Linux Screen Capture (DXGI Unavailable -> MSS Fallback) ---")
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

    # On Linux, dxcam raises ModuleNotFoundError or DXGI unsupported
    with patch("app.services.screen_track.get_settings", return_value=mock_settings), \
         patch("dxcam.create", side_effect=ModuleNotFoundError("No module named 'dxcam' (Linux)")), \
         patch("mss.mss", return_value=mock_sct), \
         patch("platform.system", return_value="Linux"):

        track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        worker = capture_hub.get_worker(1)
        assert worker.subscriber_count == 1

        frame = await track.recv()
        assert frame is not None
        assert frame.width == 1280
        assert frame.height == 720
        assert worker.backend_name == "mss"
        print(f"  [PASSED] Linux capture successfully established via MSS fallback (dims: {frame.width}x{frame.height})")

        track.stop()
        await asyncio.sleep(0.2)
        assert worker.subscriber_count == 0

    # 3. Test Linux Encoder Fallback: NVENC vs CPU (libx264)
    print("\n--- TEST 3: Linux Encoding Matrix (libx264 vs NVENC Container Passthrough) ---")

    # Scenario A: Standard Linux Container (No NVIDIA GPU -> CPU libx264)
    with patch("app.services.encoder.check_nvenc_available", return_value=(False, "No GPU")):
        encoder_cpu = HardwareH264Encoder()
        test_img = np.zeros((720, 1280, 3), dtype=np.uint8)
        vframe = VideoFrame.from_ndarray(test_img, format="bgr24")
        vframe.pts = 0
        vframe.time_base = Fraction(1, 90000)
        payloads, _ = encoder_cpu.encode(vframe)
        print(f"  Scenario A (No GPU): Fallback to '{encoder_cpu._current_encoder_name}' (libx264 CPU) -> {len(payloads)} packet(s)")
        assert encoder_cpu._current_encoder_name == "libx264"
        assert len(payloads) > 0

    # Scenario B: NVIDIA Container Runtime (NVIDIA GPU Passthrough Available)
    with patch("app.services.encoder.check_nvenc_available", return_value=(True, "GPU detected")):
        encoder_nv = HardwareH264Encoder()
        test_img = np.zeros((720, 1280, 3), dtype=np.uint8)
        vframe = VideoFrame.from_ndarray(test_img, format="bgr24")
        vframe.pts = 0
        vframe.time_base = Fraction(1, 90000)
        payloads, _ = encoder_nv.encode(vframe)
        print(f"  Scenario B (GPU Passthrough): Active encoder is '{encoder_nv._current_encoder_name}' -> {len(payloads)} packet(s)")
        assert encoder_nv._current_encoder_name == "h264_nvenc"

    # 4. Test Clean Worker Teardown under Linux Environment
    print("\n--- TEST 4: Zero-Orphan Worker Invariant on Linux Environment ---")
    assert capture_hub.get_active_worker_count() == 0, "Zero capture workers after teardown"
    print("  [PASSED] Zero orphaned capture workers or threads.")

    print("\n" + "=" * 85)
    print("PHASE 4 TRACK 4: LINUX & DOCKER RUNTIME MATRIX VERIFICATION COMPLETE")
    print("=" * 85)


if __name__ == "__main__":
    asyncio.run(test_linux_simulated_runtime())
