"""
app/services/screen_track.py — Broadcast Screen Capture Hub and Multi-Client WebRTC Tracks.

Architecture:
- SharedCaptureHub: Exactly 1 background capture worker thread per physical monitor.
- MonitorCaptureWorker: Reference-counted capture loop that starts on the first viewer
  subscription and automatically terminates when 0 subscribers remain.
- ScreenTrack: Individual aiortc VideoStreamTrack per connected viewer. Pulls raw frames
  from the shared capture worker, performs aspect-preserving scaling to the viewer's
  chosen quality profile, and delivers with independent PTS pacing and NVENC encoding.
"""

from __future__ import annotations

import logging
import asyncio
import time
import threading
from fractions import Fraction
from typing import Dict, Set, Optional, Tuple

import cv2
import mss
import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

from app.core.windows_desktop import attach_interactive_desktop
from app.services.encoder import get_active_encoder, get_active_encoder_label

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared Capture Hub & Reference-Counted Monitor Workers
# ─────────────────────────────────────────────────────────────────────────────

class MonitorCaptureWorker:
    """
    Dedicated capture worker for a single physical monitor.
    Spawns exactly 1 background capture thread while active subscribers > 0.
    """

    def __init__(self, monitor_index: int) -> None:
        self.monitor_index = monitor_index
        self._subscribers: Set[ScreenTrack] = set()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Latest raw screen capture state (unscaled BGR)
        self._latest_raw_frame: Optional[np.ndarray] = None
        self._latest_dims: Tuple[int, int] = (0, 0)
        self._frame_seq: int = 0
        self._active_backend: str = "mss"

    def subscribe(self, track: ScreenTrack) -> None:
        with self._lock:
            self._subscribers.add(track)
            subscriber_count = len(self._subscribers)

            if self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set():
                logger.info(
                    "CaptureWorker for Display %d added subscriber (total: %d)",
                    self.monitor_index,
                    subscriber_count,
                )
                return

            if self._thread is not None and self._thread.is_alive():
                self._stop_event.set()
                self._thread.join(timeout=0.3)

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._capture_loop,
                daemon=True,
                name=f"CaptureWorker-Display{self.monitor_index}",
            )
            self._thread.start()
            logger.info(
                "CaptureWorker for Display %d STARTED (subscribers: %d)",
                self.monitor_index,
                subscriber_count,
            )

    def unsubscribe(self, track: ScreenTrack) -> None:
        with self._lock:
            self._subscribers.discard(track)
            subscriber_count = len(self._subscribers)
            if subscriber_count == 0:
                self._stop_event.set()
                logger.info(
                    "CaptureWorker for Display %d STOPPING (0 subscribers remaining)",
                    self.monitor_index,
                )

    def get_latest_frame(self) -> Tuple[Optional[np.ndarray], Tuple[int, int], int]:
        """Thread-safe read of latest unscaled desktop frame."""
        with self._lock:
            return self._latest_raw_frame, self._latest_dims, self._frame_seq

    @property
    def is_running(self) -> bool:
        with self._lock:
            return not self._stop_event.is_set() and (self._thread is not None and self._thread.is_alive())

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def backend_name(self) -> str:
        with self._lock:
            return self._active_backend

    def _capture_loop(self) -> None:
        attach_interactive_desktop()
        dxgi_cam = None
        sct = None
        backend = "mss"

        try:
            import dxcam
            output_idx = max(0, self.monitor_index - 1)
            dxgi_cam = dxcam.create(device_idx=0, output_idx=output_idx, output_color="BGR")
            backend = "dxgi"
            logger.info("CaptureWorker for Display %d initialized with DXGI backend", self.monitor_index)
        except Exception as e:
            logger.info("CaptureWorker for Display %d DXGI unavailable (%s) — falling back to MSS", self.monitor_index, e)
            dxgi_cam = None
            backend = "mss"

        if dxgi_cam is None:
            sct = mss.mss()

        while not self._stop_event.is_set():
            try:
                if dxgi_cam is not None:
                    image = dxgi_cam.grab()
                    if image is None:
                        # Frame unchanged or not ready; yield briefly
                        time.sleep(0.005)
                        continue
                else:
                    monitors = sct.monitors
                    m_idx = max(1, min(self.monitor_index, len(monitors) - 1))
                    monitor = monitors[m_idx]
                    raw = sct.grab(monitor)
                    # BGRA -> BGR
                    image = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(
                        raw.height, raw.width, 4
                    )[:, :, :3]
                    image = np.ascontiguousarray(image)

                with self._lock:
                    self._latest_raw_frame = image
                    self._latest_dims = (image.shape[1], image.shape[0])
                    self._frame_seq += 1
                    self._active_backend = backend

                # Pacing sleep: 10ms for smooth ~60 FPS cap
                time.sleep(0.010)

            except Exception:
                logger.exception("CaptureWorker Display %d loop error — retrying", self.monitor_index)
                time.sleep(0.05)

        if dxgi_cam is not None:
            try:
                dxgi_cam.release()
                del dxgi_cam
            except Exception:
                pass

        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass

        with self._lock:
            if threading.current_thread() == self._thread:
                self._latest_raw_frame = None
                self._thread = None
        logger.info("CaptureWorker for Display %d TERMINATED cleanly (backend: %s)", self.monitor_index, backend)


class SharedCaptureHub:
    """Registry of active monitor capture workers."""

    def __init__(self) -> None:
        self._workers: Dict[int, MonitorCaptureWorker] = {}
        self._hub_lock = threading.RLock()

    def get_worker(self, monitor_index: int) -> MonitorCaptureWorker:
        with self._hub_lock:
            if monitor_index not in self._workers:
                self._workers[monitor_index] = MonitorCaptureWorker(monitor_index)
            return self._workers[monitor_index]

    def get_active_worker_count(self) -> int:
        with self._hub_lock:
            return sum(1 for w in self._workers.values() if w.is_running)

    def get_hub_stats(self) -> Dict[str, Any]:
        with self._hub_lock:
            active_count = sum(1 for w in self._workers.values() if w.is_running)
            return {
                "active_workers": active_count,
                "monitors": {
                    idx: {
                        "running": w.is_running,
                        "subscribers": w.subscriber_count,
                        "dims": w._latest_dims,
                        "backend": w.backend_name,
                    }
                    for idx, w in self._workers.items()
                },
            }


# Singleton capture hub
capture_hub = SharedCaptureHub()


# ─────────────────────────────────────────────────────────────────────────────
# Per-Viewer ScreenTrack (VideoStreamTrack)
# ─────────────────────────────────────────────────────────────────────────────

def get_encoder_name() -> str:
    return get_active_encoder()

ENCODER = "h264_nvenc"


class ScreenTrack(VideoStreamTrack):
    """
    Independent WebRTC video track for a connected viewer.
    Subscribes to the shared CaptureWorker for its display, downscales to its
    requested resolution profile, and paces timestamps independently.
    """

    kind = "video"

    def __init__(
        self,
        monitor_index: int = 1,
        width: int = 1280,
        height: int = 800,
        fps: int = 30,
    ):
        super().__init__()
        self.monitor_index = monitor_index
        self.target_width = width
        self.target_height = height
        self.fps = fps

        # Subscribe to shared capture worker
        self._worker = capture_hub.get_worker(self.monitor_index)
        self._worker.subscribe(self)

        # Per-viewer scaling cache
        self._last_frame_seq = -1
        self._cached_resized_frame: Optional[np.ndarray] = None
        self._current_dims: Tuple[int, int] = (width, height)

        # Timing and delivery-side metrics
        self.frames_sent = 0
        self._fps_started = time.monotonic()
        self._fps_count = 0
        self._start_time: Optional[float] = None
        self._last_frame_time: Optional[float] = None
        self._stopped = False
        self._running = True

    # ─────────────────────────────────────────────────────────────────────────
    # Monitor & Quality Controls
    # ─────────────────────────────────────────────────────────────────────────

    def set_monitor(self, monitor_index: int) -> bool:
        if not isinstance(monitor_index, int):
            return False
        if monitor_index == self.monitor_index:
            return True

        # Switch worker subscription
        self._worker.unsubscribe(self)
        self.monitor_index = monitor_index
        self._worker = capture_hub.get_worker(self.monitor_index)
        self._worker.subscribe(self)
        self._last_frame_seq = -1
        self._cached_resized_frame = None
        logger.info("ScreenTrack switched subscription to Display %d", monitor_index)
        return True

    def set_quality(self, width: int, height: int, fps: int) -> None:
        w = int(width)
        h = int(height)
        if w <= 0 or h <= 0:
            self.target_width = 0
            self.target_height = 0
        else:
            self.target_width = max(480, min(w, 2560))
            self.target_height = max(270, min(h, 1600))
        self.fps = max(10, min(int(fps), 30))
        self._cached_resized_frame = None
        logger.info(
            "ScreenTrack quality updated: target=%dx%d @ %d FPS (native=%s)",
            self.target_width,
            self.target_height,
            self.fps,
            self.target_width == 0,
        )

    def get_stats(self) -> dict:
        return {
            "target_fps": self.fps,
            "target_width": self.target_width,
            "target_height": self.target_height,
            "current_width": self._current_dims[0],
            "current_height": self._current_dims[1],
            "frames_sent": self.frames_sent,
            "monitor_index": self.monitor_index,
            "is_running": not self._stopped,
            "worker_running": self._worker.is_running,
        }

    @staticmethod
    def list_monitors() -> list[dict]:
        attach_interactive_desktop()
        with mss.mss() as sct:
            return [
                {
                    "index": index,
                    "width": monitor["width"],
                    "height": monitor["height"],
                    "left": monitor["left"],
                    "top": monitor["top"],
                    "label": f"Display {index}",
                }
                for index, monitor in enumerate(sct.monitors[1:], start=1)
            ]

    # ─────────────────────────────────────────────────────────────────────────
    # Frame Delivery & PTS Pacing
    # ─────────────────────────────────────────────────────────────────────────

    async def next_timestamp(self) -> tuple[int, Fraction]:
        now = time.monotonic()
        if self._start_time is None:
            self._start_time = now
            self._last_frame_time = now

        target_interval = 1.0 / max(1, self.fps)
        elapsed_since_last = now - self._last_frame_time
        sleep_needed = target_interval - elapsed_since_last
        if sleep_needed > 0.001:
            await asyncio.sleep(sleep_needed)
            now = time.monotonic()

        self._last_frame_time = now
        pts = int((now - self._start_time) * 90000)
        return pts, Fraction(1, 90000)

    async def recv(self) -> VideoFrame:
        if self._stopped:
            raise MediaStreamError("ScreenTrack stopped")

        pts, time_base = await self.next_timestamp()

        raw_frame, (source_w, source_h), seq = self._worker.get_latest_frame()

        if raw_frame is None:
            # Worker has not captured first frame yet
            rw = self.target_width if self.target_width > 0 else 1280
            rh = self.target_height if self.target_height > 0 else 720
            image = np.zeros((rh, rw, 3), dtype=np.uint8)
            self._current_dims = (rw, rh)
        elif seq == self._last_frame_seq and self._cached_resized_frame is not None:
            # Frame unchanged; use cached resized version
            image = self._cached_resized_frame
        else:
            # Process fresh frame according to viewer's resolution request
            tw, th = self.target_width, self.target_height
            if tw <= 0 or th <= 0:
                # Native mode: ensure even dimensions
                rw = max(2, int(source_w) & ~1)
                rh = max(2, int(source_h) & ~1)
                if rw != source_w or rh != source_h:
                    image = raw_frame[:rh, :rw]
                else:
                    image = raw_frame
            else:
                scale = min(tw / source_w, th / source_h, 1.0)
                rw = max(2, int(source_w * scale) & ~1)
                rh = max(2, int(source_h * scale) & ~1)
                if rw != source_w or rh != source_h:
                    image = cv2.resize(raw_frame, (rw, rh), interpolation=cv2.INTER_AREA)
                else:
                    image = raw_frame

            image = np.ascontiguousarray(image)
            self._cached_resized_frame = image
            self._last_frame_seq = seq
            self._current_dims = (rw, rh)

        frame = VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base

        self.frames_sent += 1
        self._fps_count += 1

        if self._fps_count % 60 == 0:
            now = time.monotonic()
            elapsed = now - self._fps_started
            delivery_fps = 60.0 / elapsed if elapsed > 0 else 0.0
            self._fps_started = now
            logger.info(
                "Track Delivery: %.1f FPS | Display %d (%dx%d)",
                delivery_fps,
                self.monitor_index,
                self._current_dims[0],
                self._current_dims[1],
            )

        return frame

    # ─────────────────────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────────────────────

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            self._running = False
            self._worker.unsubscribe(self)
            super().stop()
            logger.info("ScreenTrack stopped for Display %d", self.monitor_index)
