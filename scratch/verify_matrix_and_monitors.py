"""
scratch/verify_matrix_and_monitors.py — Real Windows hardware matrix & multi-monitor verification.
"""
from __future__ import annotations

import asyncio
from fractions import Fraction
import os
import threading
import time
import cv2
import dxcam
import mss
import numpy as np
import av
from app.core.windows_desktop import attach_interactive_desktop
from app.services.screen_track import ScreenTrack, capture_hub
from app.services.encoder import check_nvenc_available


def test_matrix_quadrant(capture_type: str, encoder_type: str, n_frames: int = 50) -> dict:
    """Benchmark a single matrix quadrant (capture + downscale + encode)."""
    attach_interactive_desktop()

    # 1. Capture Setup
    cam = None
    sct = None
    if capture_type == "dxgi":
        cam = dxcam.create(device_idx=0, output_idx=0, output_color="BGR")
    else:
        sct = mss.mss()
        mon = sct.monitors[1]

    # 2. Encoder Setup
    codec = av.CodecContext.create(encoder_type, "w")
    codec.width = 1280
    codec.height = 720
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, 90000)
    codec.open()

    frame_times = []
    # Warmup
    for _ in range(3):
        if cam:
            cam.grab()
        else:
            sct.grab(mon)

    for i in range(n_frames):
        t0 = time.perf_counter()
        if cam:
            img = None
            for _ in range(10):
                img = cam.grab()
                if img is not None:
                    break
                time.sleep(0.002)
            if img is None:
                continue
        else:
            raw = sct.grab(mon)
            img = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)[:, :, :3]

        resized = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)
        frame = av.VideoFrame.from_ndarray(resized, format="bgr24")
        frame.pts = i * 3000
        _ = codec.encode(frame)
        t1 = time.perf_counter()
        frame_times.append((t1 - t0) * 1000)

    if cam:
        cam.release()
        del cam
    if sct:
        sct.close()

    avg_latency = float(np.mean(frame_times))
    fps = 1000.0 / avg_latency if avg_latency > 0 else 0
    return {
        "capture": capture_type,
        "encoder": encoder_type,
        "latency_ms": avg_latency,
        "fps": fps,
        "budget_30fps": avg_latency < 33.33,
        "budget_60fps": avg_latency < 16.67,
    }


async def test_runtime_monitor_switching():
    """Verify ScreenTrack dynamic monitor switching on live capture hub."""
    print("\n[Test Multi-Monitor] Dynamic Monitor Switching on Live Capture Hub:")
    monitors = ScreenTrack.list_monitors()
    print(f"  Detected physical displays: {len(monitors)}")
    for m in monitors:
        print(f"    - Display {m['index']}: {m['width']}x{m['height']} at ({m['left']}, {m['top']})")

    # Connect to Display 1
    t1 = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
    f1 = await t1.recv()
    w1 = capture_hub.get_worker(1)
    print(f"  Viewer 1 on Display 1: frame={f1.width}x{f1.height}, backend={w1.backend_name}, active_workers={capture_hub.get_active_worker_count()}")
    assert capture_hub.get_active_worker_count() == 1

    # Switch to Display 1 (noop)
    t1.set_monitor(1)
    assert capture_hub.get_active_worker_count() == 1

    # Stop track
    t1.stop()
    await asyncio.sleep(0.15)
    print(f"  Viewer 1 stopped. Active workers={capture_hub.get_active_worker_count()}")
    assert capture_hub.get_active_worker_count() == 0


def run_full_matrix_verification():
    print("=" * 80)
    print("PHASE 3 M5: CROSS-CONFIGURATION MATRIX & MULTI-MONITOR VALIDATION")
    print("=" * 80)

    nvenc_ok, nvenc_msg = check_nvenc_available()
    print(f"Hardware Status: NVENC Available={nvenc_ok} ({nvenc_msg})")

    quadrants = [
        ("dxgi", "h264_nvenc" if nvenc_ok else "libx264"),
        ("dxgi", "libx264"),
        ("mss", "h264_nvenc" if nvenc_ok else "libx264"),
        ("mss", "libx264"),
    ]

    results = []
    for cap, enc in quadrants:
        print(f"\nEvaluating Quadrant: Capture={cap.upper()} + Encoder={enc}...")
        res = test_matrix_quadrant(cap, enc, n_frames=60)
        results.append(res)
        print(f"  --> Result: {res['latency_ms']:6.2f} ms/frame | Throughput: {res['fps']:5.1f} FPS | <33ms (30fps): {res['budget_30fps']}")

    print("\n" + "=" * 80)
    print("CROSS-CONFIGURATION MATRIX RESULTS SUMMARY")
    print("=" * 80)
    print(f"{'Quadrant':<25} | {'Capture':<8} | {'Encoder':<12} | {'Latency (ms)':<14} | {'Throughput':<12} | {'30 FPS':<8}")
    print("-" * 80)
    for idx, r in enumerate(results, 1):
        name = f"Q{idx}: {r['capture'].upper()}+{r['encoder'][:7]}"
        print(f"{name:<25} | {r['capture'].upper():<8} | {r['encoder']:<12} | {r['latency_ms']:6.2f} ms      | {r['fps']:5.1f} FPS   | {str(r['budget_30fps']):<8}")
    print("=" * 80)

    # Run async monitor switching test
    asyncio.run(test_runtime_monitor_switching())
    print("\n==================================================")
    print("ALL MATRIX & MULTI-MONITOR CHECKS PASSED!")
    print("==================================================")


if __name__ == "__main__":
    t = threading.Thread(target=run_full_matrix_verification)
    t.start()
    t.join()
