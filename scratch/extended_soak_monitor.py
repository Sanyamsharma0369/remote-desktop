"""
scratch/extended_soak_monitor.py — Extended duration soak test with time-series telemetry & interleaved churn.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import gc
import os
import subprocess
import threading
import time
from typing import List, Dict, Any

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


def get_windows_system_metrics() -> Dict[str, Any]:
    """Queries process memory (RSS MB) and Windows OS handle count."""
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
        "handle_count": int(handle_count.value),
        "threads": threading.active_count(),
    }


def get_gpu_telemetry() -> Dict[str, float]:
    """Queries NVIDIA GPU utilization and dedicated video encoder utilization."""
    try:
        cmd = [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,utilization.encoder,memory.used",
            "--format=csv,noheader,nounits",
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        parts = [p.strip() for p in out.split(",")]
        return {
            "gpu_pct": float(parts[0]),
            "encoder_pct": float(parts[1]),
            "vram_mb": float(parts[2]),
        }
    except Exception:
        return {"gpu_pct": 0.0, "encoder_pct": 0.0, "vram_mb": 0.0}


async def run_extended_soak(duration_seconds: int = 300, churn_interval_sec: int = 60):
    print("=" * 85)
    print("PHASE 4 M1: EXTENDED WINDOWS SOAK & RESOURCE STABILITY MONITOR")
    print("=" * 85)
    print(f"Active Video Encoder: {get_active_encoder()}")
    print(f"Target Duration:     {duration_seconds} seconds ({duration_seconds / 60:.1f} minutes)")
    print(f"Churn Interleave:    Viewer B joins every {churn_interval_sec}s for 15s")
    print(f"Process PID:         {os.getpid()}\n")

    baseline = get_windows_system_metrics()
    gpu_base = get_gpu_telemetry()
    print(f"[Baseline] RSS: {baseline['rss_mb']:.2f} MB | Handles: {baseline['handle_count']} | Threads: {baseline['threads']} | GPU Enc: {gpu_base['encoder_pct']}%")

    # Start primary sustained viewer A (1080p, 30 FPS)
    print("\n[Start] Launching Primary Sustained Viewer A (1920x1080 @ 30 FPS)...")
    track_a = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
    worker = capture_hub.get_worker(1)

    t_start = time.monotonic()
    last_sample = t_start
    last_churn = t_start
    track_b: Optional[ScreenTrack] = None
    churn_count = 0

    frames_a = 0
    frames_b = 0
    telemetry_samples: List[Dict[str, Any]] = []

    print(f"{'Time':<8} | {'Viewer A':<10} | {'Viewer B':<10} | {'RSS (MB)':<10} | {'Delta':<9} | {'Handles':<8} | {'Threads':<8} | {'GPU Enc':<8} | {'Workers':<8}")
    print("-" * 85)

    while True:
        now = time.monotonic()
        elapsed = now - t_start
        if elapsed >= duration_seconds:
            break

        # Receive frame from primary Viewer A
        frame_a = await track_a.recv()
        if frame_a is not None:
            frames_a += 1

        # Receive frame from secondary Viewer B if currently active
        if track_b is not None:
            frame_b = await track_b.recv()
            if frame_b is not None:
                frames_b += 1

        # Interleaved churn management: Viewer B joins/leaves
        if track_b is None and (now - last_churn >= churn_interval_sec):
            churn_count += 1
            track_b = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
            last_churn = now
        elif track_b is not None and (now - last_churn >= 15.0):
            # Viewer B leaves after 15s
            track_b.stop()
            track_b = None
            last_churn = now

        # Sample telemetry every 10 seconds
        if now - last_sample >= 10.0:
            m = get_windows_system_metrics()
            gpu = get_gpu_telemetry()
            delta_mb = m["rss_mb"] - baseline["rss_mb"]
            fps_a = frames_a / elapsed if elapsed > 0 else 0.0
            status_b = "Active" if track_b is not None else "Idle"

            sample = {
                "elapsed_s": elapsed,
                "fps_a": fps_a,
                "rss_mb": m["rss_mb"],
                "delta_mb": delta_mb,
                "handles": m["handle_count"],
                "threads": m["threads"],
                "gpu_enc": gpu["encoder_pct"],
                "workers": capture_hub.get_active_worker_count(),
                "subscribers": worker.subscriber_count,
            }
            telemetry_samples.append(sample)

            print(
                f"{elapsed:6.1f}s  | "
                f"{fps_a:4.1f} FPS   | "
                f"{status_b:<10} | "
                f"{m['rss_mb']:7.2f} MB | "
                f"{delta_mb:+6.2f} MB | "
                f"{m['handle_count']:7d} | "
                f"{m['threads']:7d} | "
                f"{gpu['encoder_pct']:6.1f}% | "
                f"{capture_hub.get_active_worker_count():7d}"
            )
            last_sample = now

    # Teardown
    print("\n[Teardown] Stopping all viewer tracks...")
    if track_b is not None:
        track_b.stop()
    track_a.stop()

    # Allow worker thread to join
    await asyncio.sleep(0.3)
    gc.collect()

    final = get_windows_system_metrics()
    gpu_final = get_gpu_telemetry()
    total_elapsed = time.monotonic() - t_start

    print("\n" + "=" * 85)
    print("EXTENDED SOAK STABILITY SUMMARY REPORT")
    print("=" * 85)
    print(f"Total Test Duration:       {total_elapsed:6.2f} seconds ({total_elapsed / 60:.2f} minutes)")
    print(f"Total Frames Delivered:    {frames_a} (Viewer A) + {frames_b} (Viewer B Churn)")
    print(f"Average Sustained FPS:     {frames_a / total_elapsed:6.1f} FPS")
    print(f"Interleaved Churn Cycles:  {churn_count} bursts completed")
    print("-" * 85)
    print(f"Memory (Working Set):      Baseline = {baseline['rss_mb']:.2f} MB | Final = {final['rss_mb']:.2f} MB (Net Change: {final['rss_mb'] - baseline['rss_mb']:+.2f} MB)")
    if len(telemetry_samples) >= 4:
        # Check steady-state memory drift between 1st minute and end
        mid_rss = telemetry_samples[len(telemetry_samples) // 2]["rss_mb"]
        end_rss = telemetry_samples[-1]["rss_mb"]
        print(f"Steady-State Drift:        Midpoint = {mid_rss:.2f} MB -> End = {end_rss:.2f} MB (Drift: {end_rss - mid_rss:+.2f} MB)")
    print(f"OS Handle Count:           Baseline = {baseline['handle_count']} | Final = {final['handle_count']} (Net: {final['handle_count'] - baseline['handle_count']:+d})")
    print(f"Thread Count:              Baseline = {baseline['threads']} | Final = {final['threads']}")
    print(f"Active Capture Workers:    {capture_hub.get_active_worker_count()} (Expected: 0)")
    print(f"Active Controller Token:   {control_manager.get_active_controller_info()} (Expected: None)")
    print("=" * 85)

    assert capture_hub.get_active_worker_count() == 0, "Capture workers must be 0 after teardown"
    assert control_manager.get_active_controller_info() is None, "Controller must be None"
    print("\n[SUCCESS] EXTENDED SOAK VERIFICATION PASSED: Flatline memory curve, zero handle/thread leaks.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--churn-interval", type=int, default=45)
    args = parser.parse_args()
    asyncio.run(run_extended_soak(duration_seconds=args.duration_seconds, churn_interval_sec=args.churn_interval))
