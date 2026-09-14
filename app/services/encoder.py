"""
app/services/encoder.py — Hardware Video Acceleration (NVIDIA NVENC) with CPU Fallback.

Provides:
- Safe NVIDIA / NVENC capability detection and caching.
- Dynamic H.264 encoder supporting 'h264_nvenc' and 'libx264'.
- Automatic fallback from NVENC to CPU libx264 on runtime failure.
- Active encoder telemetry reporting.
"""

from __future__ import annotations

import fractions
import logging
from typing import Iterator, Optional, Tuple
import av
import av.video.frame
from aiortc.codecs.h264 import H264Encoder, MAX_FRAME_RATE
import aiortc.codecs
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Cached capability probe result: (is_available, description)
_NVENC_PROBE_RESULT: Optional[Tuple[bool, str]] = None

# Active runtime encoder tracking: 'h264_nvenc' or 'libx264' (None if stream not active yet)
_ACTIVE_ENCODER: Optional[str] = None


def check_nvenc_available(force_refresh: bool = False) -> Tuple[bool, str]:
    """
    Safely probes whether NVIDIA NVENC hardware encoding is functional on this system.
    Caches the result to avoid recurring overhead.
    """
    global _NVENC_PROBE_RESULT
    if _NVENC_PROBE_RESULT is not None and not force_refresh:
        return _NVENC_PROBE_RESULT

    try:
        # Step 1: Check if PyAV FFmpeg has h264_nvenc codec registered
        try:
            codec = av.Codec("h264_nvenc", "w")
            if not codec:
                _NVENC_PROBE_RESULT = (False, "h264_nvenc codec not registered in PyAV/FFmpeg")
                return _NVENC_PROBE_RESULT
        except Exception as e:
            _NVENC_PROBE_RESULT = (False, f"PyAV does not support h264_nvenc: {e}")
            return _NVENC_PROBE_RESULT

        # Step 2: Try allocating and opening a real NVENC codec context with dummy frame
        ctx = av.CodecContext.create("h264_nvenc", "w")
        ctx.width = 320
        ctx.height = 240
        ctx.pix_fmt = "yuv420p"
        ctx.time_base = fractions.Fraction(1, 30)
        ctx.framerate = fractions.Fraction(30, 1)
        ctx.bit_rate = 1_000_000
        ctx.options = {
            "preset": "p1",
            "tune": "ull",
            "zerolatency": "1",
            "delay": "0",
            "forced-idr": "1",
        }
        ctx.open()

        # Step 3: Encode 1 dummy frame to verify hardware pipeline execution
        import numpy as np
        dummy_frame = av.VideoFrame.from_ndarray(
            np.zeros((240, 320, 3), dtype=np.uint8), format="bgr24"
        )
        dummy_frame.pts = 0
        dummy_frame.time_base = fractions.Fraction(1, 30)
        _ = ctx.encode(dummy_frame)

        _NVENC_PROBE_RESULT = (True, "NVIDIA NVENC hardware encoder ready")
        logger.info("NVENC hardware capability probe SUCCEEDED: %s", _NVENC_PROBE_RESULT[1])
        return _NVENC_PROBE_RESULT

    except Exception as exc:
        _NVENC_PROBE_RESULT = (False, f"NVENC initialization/probe failed: {exc}")
        logger.warning("NVENC hardware capability probe FAILED: %s", exc)
        return _NVENC_PROBE_RESULT


def get_active_encoder() -> str:
    """
    Returns the active runtime encoder if a stream is running,
    or the effective configured encoder based on hardware availability.
    """
    if _ACTIVE_ENCODER is not None:
        return _ACTIVE_ENCODER

    settings = get_settings()
    mode = (settings.ENCODER or "auto").lower()
    if mode == "cpu":
        return "libx264"
    if mode in ("auto", "nvenc"):
        nvenc_ok, _ = check_nvenc_available()
        return "h264_nvenc" if nvenc_ok else "libx264"
    return "libx264"


def set_active_encoder(name: Optional[str]) -> None:
    """Updates the active video encoder state."""
    global _ACTIVE_ENCODER
    _ACTIVE_ENCODER = name


def get_active_encoder_label() -> str:
    """Returns human-readable active encoder label for the HUD telemetry."""
    encoder = get_active_encoder()
    if encoder == "h264_nvenc":
        return "H.264 (NVIDIA NVENC)"
    return "H.264 (CPU - libx264)"


class HardwareH264Encoder(H264Encoder):
    """
    H.264 video encoder supporting NVIDIA NVENC hardware acceleration
    with automatic CPU (libx264) fallback and dynamic quality adaptation.
    """

    def __init__(self) -> None:
        super().__init__()
        self._current_encoder_name: str = "libx264"

    def _create_nvenc_context(self, frame: av.VideoFrame) -> av.VideoCodecContext:
        """Initializes an NVENC CodecContext tuned for ultra-low latency streaming."""
        ctx = av.CodecContext.create("h264_nvenc", "w")
        ctx.width = frame.width
        ctx.height = frame.height
        ctx.bit_rate = self.target_bitrate
        ctx.pix_fmt = "yuv420p"
        ctx.framerate = fractions.Fraction(MAX_FRAME_RATE, 1)
        ctx.time_base = fractions.Fraction(1, MAX_FRAME_RATE)
        ctx.options = {
            "preset": "p1",
            "tune": "ull",
            "zerolatency": "1",
            "delay": "0",
            "forced-idr": "1",
        }
        ctx.open()
        return ctx

    def _create_libx264_context(self, frame: av.VideoFrame) -> av.VideoCodecContext:
        """Initializes a software libx264 CodecContext."""
        ctx = av.CodecContext.create("libx264", "w")
        ctx.width = frame.width
        ctx.height = frame.height
        ctx.bit_rate = self.target_bitrate
        ctx.pix_fmt = "yuv420p"
        ctx.framerate = fractions.Fraction(MAX_FRAME_RATE, 1)
        ctx.time_base = fractions.Fraction(1, MAX_FRAME_RATE)
        ctx.options = {
            "level": "31",
            "tune": "zerolatency",
            "preset": "ultrafast",
        }
        ctx.profile = "Baseline"
        ctx.open()
        return ctx

    def _encode_frame(
        self, frame: av.VideoFrame, force_keyframe: bool
    ) -> Iterator[bytes]:
        settings = get_settings()
        requested_mode = (settings.ENCODER or "auto").lower()

        # Check if dimensions or bitrate changed significantly (>10%)
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate > 0.1
        ):
            self.buffer_data = b""
            self.buffer_pts = None
            self.codec = None

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
        else:
            frame.pict_type = av.video.frame.PictureType.NONE

        if self.codec is None:
            use_nvenc = False
            if requested_mode in ("auto", "nvenc"):
                nvenc_ok, reason = check_nvenc_available()
                if nvenc_ok:
                    use_nvenc = True
                else:
                    if requested_mode == "nvenc":
                        logger.warning(
                            "ENCODER='nvenc' requested but NVENC is unavailable (%s). Falling back to CPU.",
                            reason,
                        )
                    else:
                        logger.info("ENCODER='auto' -> NVENC unavailable (%s), using CPU libx264.", reason)

            if use_nvenc:
                try:
                    self.codec = self._create_nvenc_context(frame)
                    self._current_encoder_name = "h264_nvenc"
                    set_active_encoder("h264_nvenc")
                    logger.info(
                        "HardwareH264Encoder initialized: NVIDIA NVENC (%dx%d @ %d bps)",
                        frame.width,
                        frame.height,
                        self.target_bitrate,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to initialize h264_nvenc context: %s. Falling back to libx264.", exc
                    )
                    self.codec = None

            if self.codec is None:
                self.codec = self._create_libx264_context(frame)
                self._current_encoder_name = "libx264"
                set_active_encoder("libx264")
                logger.info(
                    "HardwareH264Encoder initialized: CPU libx264 (%dx%d @ %d bps)",
                    frame.width,
                    frame.height,
                    self.target_bitrate,
                )

        data_to_send = b""
        try:
            for package in self.codec.encode(frame):
                data_to_send += bytes(package)
        except Exception as encode_err:
            if self._current_encoder_name == "h264_nvenc":
                logger.error(
                    "NVENC runtime encode failed (%s). Falling back to CPU libx264.", encode_err
                )
                try:
                    self.codec = self._create_libx264_context(frame)
                    self._current_encoder_name = "libx264"
                    set_active_encoder("libx264")
                    for package in self.codec.encode(frame):
                        data_to_send += bytes(package)
                except Exception as fallback_err:
                    logger.critical("CPU libx264 fallback encode failed: %s", fallback_err)
                    raise fallback_err
            else:
                raise encode_err

        if data_to_send:
            yield from self._split_bitstream(data_to_send)


def patch_aiortc_encoder() -> None:
    """
    Patches aiortc's get_encoder and H264Encoder to use HardwareH264Encoder.
    """
    aiortc.codecs.h264.H264Encoder = HardwareH264Encoder
    orig_get_encoder = aiortc.codecs.get_encoder

    def custom_get_encoder(codec: aiortc.RTCRtpCodecParameters):
        mime_type = codec.mimeType.lower()
        if mime_type == "video/h264":
            return HardwareH264Encoder()
        return orig_get_encoder(codec)

    aiortc.codecs.get_encoder = custom_get_encoder
    logger.info("aiortc encoder patched with HardwareH264Encoder (NVENC + CPU fallback)")


# Apply the patch immediately when module is imported
patch_aiortc_encoder()
