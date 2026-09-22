"""
scratch/investigate_handles_and_pacing.py — Validates handle count stability across 10 teardown cycles and concurrent viewer delivery FPS.
"""
import asyncio
import ctypes
from ctypes import wintypes
import gc
import os
import threading
import time
from typing import Dict, Any, List

from app.services.screen_track import ScreenTrack, capture_hub, get_active_encoder
from app.services.state import control_manager


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def get_metrics() -> Dict[str, Any]:
    pid = os.getpid()
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    handle = ctypes.windll.kernel32.OpenProcess(0x0410 | 0x0400, False, pid)
    rss_mb = 0.0
    handle_count = wintypes.DWORD(0)

    if handle:
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            rss_mb = pmc.WorkingSetSize / (1024 * 1024)
        ctypes.windll.kernel32.GetProcessHandleCount(handle, ctypes.byref(handle_count))
        ctypes.windll.kernel32.CloseHandle(handle)

    return {
        "rss_mb": rss_mb,
        "handles": int(handle_count.value),
        "threads": threading.active_count(),
        "workers": capture_hub.get_active_worker_count(),
    }


async def test_repeated_teardown_cycles(num_cycles: int = 10):
    print("=" * 80)
    print(f"PART 1: REPEATED TEARDOWN HANDLE & MEMORY STABILITY ({num_cycles} Full Cycles)")
    print("=" * 80)
    print(f"Encoder: {get_active_encoder()} | PID: {os.getpid()}")

    base = get_metrics()
    print(f"[Baseline T0] Handles: {base['handles']} | RSS: {base['rss_mb']:.2f} MB | Threads: {base['threads']} | Workers: {base['workers']}\n")

    history: List[Dict[str, Any]] = []

    print(f"{'Cycle':<8} | {'State':<12} | {'Handles':<10} | {'Delta T0':<10} | {'RSS (MB)':<12} | {'Threads':<8} | {'Workers':<8}")
    print("-" * 80)

    for c in range(1, num_cycles + 1):
        # 1. Connect
        track = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
        # 2. Stream 30 frames
        for _ in range(30):
            await track.recv()

        m_active = get_metrics()

        # 3. Disconnect and full teardown
        track.stop()
        await asyncio.sleep(0.3)
        gc.collect()

        m_idle = get_metrics()
        history.append(m_idle)

        print(
            f"Cycle {c:<2}  | "
            f"{'Teardown':<12} | "
            f"{m_idle['handles']:<10d} | "
            f"{m_idle['handles'] - base['handles']:+10d} | "
            f"{m_idle['rss_mb']:7.2f} MB   | "
            f"{m_idle['threads']:<8d} | "
            f"{m_idle['workers']:<8d}"
        )

    print("-" * 80)
    # Analysis
    post_init_handles = [h["handles"] for h in history[1:]]  # Cycle 2 to 10
    min_h, max_h = min(post_init_handles), max(post_init_handles)
    h_drift = history[-1]["handles"] - history[0]["handles"]
    rss_drift = history[-1]["rss_mb"] - history[0]["rss_mb"]

    print(f"\n[Handle Analysis]")
    print(f"  Cycle 1 Idle Handles (Post-Init): {history[0]['handles']}")
    print(f"  Cycle 10 Idle Handles:            {history[-1]['handles']}")
    print(f"  Post-Init Handle Range:           {min_h} - {max_h} (Spread: {max_h - min_h})")
    print(f"  Handle Drift across Cycles 2->10: {history[-1]['handles'] - history[1]['handles']:+d} handles")
    print(f"  Memory Drift across Cycles 1->10: {rss_drift:+.2f} MB")
    print("=" * 80)


async def viewer_consumer(track: ScreenTrack, duration: float, name: str) -> Dict[str, Any]:
    t_start = time.monotonic()
    frames = 0
    while time.monotonic() - t_start < duration:
        frame = await track.recv()
        if frame is not None:
            frames += 1
    total_time = time.monotonic() - t_start
    return {
        "name": name,
        "frames": frames,
        "duration": total_time,
        "fps": frames / total_time if total_time > 0 else 0.0,
    }


async def test_concurrent_pacing(duration: float = 15.0):
    print("\n" + "=" * 80)
    print(f"PART 2: CONCURRENT VIEWER DELIVERY PACING (True Parallel WebRTC Consumers)")
    print("=" * 80)

    track_a = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
    track_b = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)

    print(f"[Starting] Viewer A (1080p@30) and Viewer B (720p@30) concurrently for {duration:.1f}s...")
    res_a, res_b = await asyncio.gather(
        viewer_consumer(track_a, duration, "Viewer A (1080p)"),
        viewer_consumer(track_b, duration, "Viewer B (720p)"),
    )

    track_a.stop()
    track_b.stop()
    await asyncio.sleep(0.3)
    gc.collect()

    print(f"  {res_a['name']}: {res_a['frames']} frames in {res_a['duration']:.2f}s -> {res_a['fps']:.2f} FPS")
    print(f"  {res_b['name']}: {res_b['frames']} frames in {res_b['duration']:.2f}s -> {res_b['fps']:.2f} FPS")
    print("=" * 80)


async def main():
    await test_repeated_teardown_cycles(num_cycles=10)
    await test_concurrent_pacing(duration=15.0)


if __name__ == "__main__":
    asyncio.run(main())
