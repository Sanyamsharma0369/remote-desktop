import logging
import asyncio
import time
import threading
from fractions import Fraction
import cv2
import mss
import numpy as np
from aiortc import VideoStreamTrack
from av import VideoFrame
from app.core.windows_desktop import attach_interactive_desktop

logger = logging.getLogger(__name__)

# Encoder label for HUD telemetry
ENCODER = "libx264"


class ScreenTrack(VideoStreamTrack):
    """
    Performance-oriented screen capture track for WebRTC remote desktop streaming.

    Architecture: A dedicated background thread continuously grabs and resizes
    frames, placing the latest one into a shared slot. The async recv() method
    reads from that slot and returns immediately, keeping the aiortc event loop
    unblocked and achieving stable 25-30 FPS delivery.
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
        attach_interactive_desktop()
        self.monitor_index = monitor_index
        self.target_width = width
        self.target_height = height
        self.fps = fps

        # Shared state between capture thread and async recv()
        self._frame_lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._latest_dims: tuple[int, int] = (width, height)

        # Timing for delivery-side FPS measurement
        self.frames_sent = 0
        self._fps_started = time.monotonic()
        self._fps_count = 0
        self._start_time: float | None = None
        self._last_frame_time: float | None = None

        # Start background capture thread
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="ScreenCaptureThread"
        )
        self._capture_thread.start()

    # ------------------------------------------------------------------
    # Background capture thread — runs independently of asyncio
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """
        Continuously grabs desktop frames, resizes them, and stores the
        latest in self._latest_frame. Runs in a daemon thread so it never
        blocks the asyncio event loop.
        """
        attach_interactive_desktop()
        sct = mss.mss()

        while self._running:
            try:
                monitors = sct.monitors
                monitor_index = max(1, min(self.monitor_index, len(monitors) - 1))
                monitor = monitors[monitor_index]

                # Capture BGRA, drop alpha channel -> BGR
                raw = sct.grab(monitor)
                image = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(
                    raw.height, raw.width, 4
                )[:, :, :3]

                source_h, source_w = image.shape[:2]
                tw, th = self.target_width, self.target_height

                if tw <= 0 or th <= 0:
                    # Native resolution mode: 100% full monitor pixels
                    rw = max(2, int(source_w) & ~1)
                    rh = max(2, int(source_h) & ~1)
                    if rw != source_w or rh != source_h:
                        image = image[:rh, :rw]
                else:
                    # Aspect-preserving downscale; never upscale
                    scale = min(tw / source_w, th / source_h, 1.0)
                    rw = max(2, int(source_w * scale) & ~1)   # ensure even (H.264)
                    rh = max(2, int(source_h * scale) & ~1)

                    if rw != source_w or rh != source_h:
                        image = cv2.resize(image, (rw, rh), interpolation=cv2.INTER_AREA)

                image = np.ascontiguousarray(image)

                with self._frame_lock:
                    self._latest_frame = image
                    self._latest_dims = (rw, rh)

            except Exception:
                logger.exception("Capture thread error — retrying")
                time.sleep(0.05)

        sct.close()

    # ------------------------------------------------------------------
    # Monitor / quality control (thread-safe writes to simple attrs)
    # ------------------------------------------------------------------

    def set_monitor(self, monitor_index: int) -> bool:
        """Switch physical capture display (1..N)."""
        if not isinstance(monitor_index, int):
            return False
        # The capture thread clamps on next iteration, so just update the attr.
        self.monitor_index = monitor_index
        logger.info("Screen capture switched to Display %d", monitor_index)
        return True

    def set_quality(self, width: int, height: int, fps: int) -> None:
        """Dynamically update target resolution and frame rate with strict bounds."""
        w = int(width)
        h = int(height)
        if w <= 0 or h <= 0:
            self.target_width = 0
            self.target_height = 0
        else:
            self.target_width = max(480, min(w, 2560))
            self.target_height = max(270, min(h, 1600))
        self.fps = max(10, min(int(fps), 30))
        logger.info(
            "ScreenTrack quality updated: target=%dx%d @ %d FPS (native=%s)",
            self.target_width,
            self.target_height,
            self.fps,
            self.target_width == 0,
        )

    def get_stats(self) -> dict:
        """Return real-time metrics of the capture and delivery pipeline."""
        with self._frame_lock:
            cur_w, cur_h = self._latest_dims
        return {
            "target_fps": self.fps,
            "target_width": self.target_width,
            "target_height": self.target_height,
            "current_width": cur_w,
            "current_height": cur_h,
            "frames_sent": self.frames_sent,
            "monitor_index": self.monitor_index,
            "is_running": self._running,
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

    # ------------------------------------------------------------------
    # aiortc PTS pacing — stays on the event loop (cheap)
    # ------------------------------------------------------------------

    async def next_timestamp(self) -> tuple[int, Fraction]:
        now = time.monotonic()
        if self._start_time is None:
            self._start_time = now
            self._last_frame_time = now

        # Pace delivery to target FPS
        target_interval = 1.0 / max(1, self.fps)
        elapsed_since_last = now - self._last_frame_time
        sleep_needed = target_interval - elapsed_since_last
        if sleep_needed > 0.001:
            await asyncio.sleep(sleep_needed)
            now = time.monotonic()

        self._last_frame_time = now
        pts = int((now - self._start_time) * 90000)
        return pts, Fraction(1, 90000)

    # ------------------------------------------------------------------
    # aiortc recv — non-blocking: reads pre-rendered frame from slot
    # ------------------------------------------------------------------

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()

        with self._frame_lock:
            image = self._latest_frame
            rw, rh = self._latest_dims

        if image is None:
            # Capture thread hasn't produced first frame yet; send black
            image = np.zeros(
                (self.target_height, self.target_width, 3), dtype=np.uint8
            )
            rw, rh = self.target_width, self.target_height

        frame = VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base

        self.frames_sent += 1
        self._fps_count += 1

        if self._fps_count % 30 == 0:
            now = time.monotonic()
            elapsed = now - self._fps_started
            delivery_fps = 30.0 / elapsed if elapsed > 0 else 0.0
            self._fps_started = now
            logger.info(
                "Delivery pipeline: %.1f FPS | Display %d (%dx%d)",
                delivery_fps,
                self.monitor_index,
                rw,
                rh,
            )

        return frame

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._running = False
        try:
            self._capture_thread.join(timeout=2.0)
        except Exception:
            pass
        super().stop()

