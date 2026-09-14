"""
tests/test_encoder.py — Regression tests for NVIDIA NVENC hardware acceleration and CPU fallback.
"""
from unittest.mock import patch, MagicMock
import av
import numpy as np
import fractions
import pytest
from app.services.encoder import (
    check_nvenc_available,
    get_active_encoder,
    get_active_encoder_label,
    set_active_encoder,
    HardwareH264Encoder,
)
from app.core.config import Settings


def test_nvenc_capability_detection_caching():
    """Verify check_nvenc_available caches probe result properly."""
    with patch("app.services.encoder._NVENC_PROBE_RESULT", (True, "mocked nvenc ready")):
        ok, reason = check_nvenc_available(force_refresh=False)
        assert ok is True
        assert "mocked" in reason


def test_encoder_label_mapping():
    """Verify human-readable encoder labels."""
    with patch("app.services.encoder.get_active_encoder", return_value="h264_nvenc"):
        assert get_active_encoder_label() == "H.264 (NVIDIA NVENC)"

    with patch("app.services.encoder.get_active_encoder", return_value="libx264"):
        assert get_active_encoder_label() == "H.264 (CPU - libx264)"


def test_encoder_mode_cpu_selection():
    """When ENCODER='cpu', HardwareH264Encoder always uses libx264."""
    mock_settings = Settings(ENCODER="cpu")
    with patch("app.services.encoder.get_settings", return_value=mock_settings):
        set_active_encoder(None)
        enc = HardwareH264Encoder()
        frame = av.VideoFrame.from_ndarray(
            np.zeros((100, 100, 3), dtype=np.uint8), format="bgr24"
        )
        frame.pts = 0
        frame.time_base = fractions.Fraction(1, 30)
        
        # Test encode
        packages = list(enc._encode_frame(frame, force_keyframe=True))
        assert enc._current_encoder_name == "libx264"
        assert get_active_encoder() == "libx264"


def test_encoder_mode_auto_fallback_when_nvenc_unavailable():
    """When ENCODER='auto' and NVENC probe fails, falls back to libx264."""
    mock_settings = Settings(ENCODER="auto")
    with patch("app.services.encoder.get_settings", return_value=mock_settings):
        with patch("app.services.encoder.check_nvenc_available", return_value=(False, "No GPU")):
            set_active_encoder(None)
            enc = HardwareH264Encoder()
            frame = av.VideoFrame.from_ndarray(
                np.zeros((100, 100, 3), dtype=np.uint8), format="bgr24"
            )
            frame.pts = 0
            frame.time_base = fractions.Fraction(1, 30)
            
            _ = list(enc._encode_frame(frame, force_keyframe=True))
            assert enc._current_encoder_name == "libx264"
            assert get_active_encoder() == "libx264"


def test_encoder_runtime_encode_fallback():
    """When h264_nvenc fails during active encode loop, seamlessly falls back to CPU libx264."""
    mock_settings = Settings(ENCODER="auto")
    with patch("app.services.encoder.get_settings", return_value=mock_settings):
        with patch("app.services.encoder.check_nvenc_available", return_value=(True, "OK")):
            enc = HardwareH264Encoder()
            frame = av.VideoFrame.from_ndarray(
                np.zeros((100, 100, 3), dtype=np.uint8), format="bgr24"
            )
            frame.pts = 0
            frame.time_base = fractions.Fraction(1, 30)

            # Mock a failing nvenc codec context
            mock_failing_codec = MagicMock()
            mock_failing_codec.encode.side_effect = RuntimeError("NVENC hardware memory allocation failure")
            mock_failing_codec.width = 100
            mock_failing_codec.height = 100
            mock_failing_codec.bit_rate = 2000000

            with patch.object(enc, "_create_nvenc_context", return_value=mock_failing_codec):
                _ = list(enc._encode_frame(frame, force_keyframe=True))
                # Fallback should have switched to libx264
                assert enc._current_encoder_name == "libx264"
                assert get_active_encoder() == "libx264"


def test_api_stream_info_dynamic_encoder_reporting(client, user_token):
    """GET /api/stream/info accurately reflects the runtime encoder state."""
    headers = {"Authorization": f"Bearer {user_token}"}
    
    set_active_encoder("h264_nvenc")
    res = client.get("/api/stream/info", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["encoder"] == "h264_nvenc"
    assert data["encoder_label"] == "H.264 (NVIDIA NVENC)"

    set_active_encoder("libx264")
    res = client.get("/api/stream/info", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["encoder"] == "libx264"
    assert data["encoder_label"] == "H.264 (CPU - libx264)"
    
    # Reset
    set_active_encoder(None)
