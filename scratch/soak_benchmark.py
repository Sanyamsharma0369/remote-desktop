"""
scratch/soak_benchmark.py — Runtime soak & memory/resource stability benchmarking harness.
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


def get_process_metrics() -> dict:
    pid = os.getpid()
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    handle = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid)
    rss_mb = 0.0
    if handle:
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            rss_mb = pmc.WorkingSetSize / (1024 * 1024)
        ctypes.windll.kernel32.CloseHandle(handle)

    return {
        "rss_mb": rss_mb,
        "threads": threading.active_count(),
    }


def get_gpu_metrics() -> dict:
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
            "gpu_mem_used_mb": float(parts[2]),
        }
    except Exception:
        return {"gpu_pct": 0.0, "encoder_pct": 0.0, "gpu_mem_used_mb": 0.0}


async def run_soak_benchmark(cycles: int = 50, soak_seconds: int = 120):
    print("=" * 70)
    print("PHASE 3 M3: RESOURCE SOAK & LONG-RUNNING STABILITY BENCHMARK")
    print("=" * 70)
    print(f"Active Encoder Detection: {get_active_encoder()}")
    print(f"PID: {os.getpid()} | Config: cycles={cycles}, soak_duration={soak_seconds}s\n")

    baseline = get_process_metrics()
    gpu_base = get_gpu_metrics()
    print(f"[Baseline] RSS: {baseline['rss_mb']:.2f} MB | Threads: {baseline['threads']} | GPU Enc: {gpu_base['encoder_pct']}%")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1: Rapid Connect / Disconnect Churn (50-100 cycles)
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n[Phase 1] Rapid Connect / Disconnect Churn ({cycles} cycles)...")
    t0 = time.monotonic()
    worker = capture_hub.get_worker(1)

    for i in range(1, cycles + 1):
        track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
        # Receive 3 frames
        for _ in range(3):
            f = await track.recv()
            assert f is not None
        track.stop()
        if i % 10 == 0:
            m = get_process_metrics()
            print(f"  Cycle {i:3d}/{cycles}: RSS={m['rss_mb']:.2f} MB, Active Threads={m['threads']}, Worker Running={worker.is_running}")

    await asyncio.sleep(0.2)
    gc.collect()
    p1_metrics = get_process_metrics()
    print(f"  --> Churn Finished in {time.monotonic() - t0:.2f}s")
    print(f"  --> Post-Churn: RSS={p1_metrics['rss_mb']:.2f} MB (Delta: {p1_metrics['rss_mb'] - baseline['rss_mb']:+.2f} MB), Threads={p1_metrics['threads']}, Workers Active={capture_hub.get_active_worker_count()}")
    assert capture_hub.get_active_worker_count() == 0, "All workers must terminate after churn"

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2: Multi-Viewer Staggered Churn & Overlap
    # ─────────────────────────────────────────────────────────────────────────
    print("\n[Phase 2] Multi-Viewer Staggered Overlap Test (4 concurrent/interleaved clients)...")
    # A connects
    t_a = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
    await t_a.recv()
    assert worker.subscriber_count == 1 and capture_hub.get_active_worker_count() == 1

    # B connects
    t_b = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
    await t_b.recv()
    assert worker.subscriber_count == 2 and capture_hub.get_active_worker_count() == 1

    # C connects
    t_c = ScreenTrack(monitor_index=1, width=960, height=540, fps=20)
    await t_c.recv()
    assert worker.subscriber_count == 3 and capture_hub.get_active_worker_count() == 1

    # B leaves
    t_b.stop()
    assert worker.subscriber_count == 2 and capture_hub.get_active_worker_count() == 1

    # D connects
    t_d = ScreenTrack(monitor_index=1, width=1280, height=800, fps=30)
    await t_d.recv()
    assert worker.subscriber_count == 3 and capture_hub.get_active_worker_count() == 1

    # A, C leave
    t_a.stop()
    t_c.stop()
    assert worker.subscriber_count == 1 and capture_hub.get_active_worker_count() == 1

    # D leaves
    t_d.stop()
    await asyncio.sleep(0.2)
    assert worker.subscriber_count == 0 and capture_hub.get_active_worker_count() == 0
    print(f"  --> Overlap Test Complete. All subscribers cleared, Active Workers={capture_hub.get_active_worker_count()}")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 3: Sustained Single-Viewer 1080p/30 FPS Soak Session
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n[Phase 3] Sustained Single-Viewer Soak Session ({soak_seconds}s @ 1080p / 30 FPS)...")
    soak_track = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
    soak_start = time.monotonic()
    last_report = soak_start
    frames_delivered = 0
    initial_soak_mem = get_process_metrics()["rss_mb"]

    while time.monotonic() - soak_start < soak_seconds:
        frame = await soak_track.recv()
        frames_delivered += 1
        now = time.monotonic()
        if now - last_report >= 15.0:
            elapsed = now - soak_start
            fps = frames_delivered / elapsed
            m = get_process_metrics()
            gpu = get_gpu_metrics()
            print(
                f"  [{elapsed:5.1f}s / {soak_seconds}s] Frames: {frames_delivered:5d} | "
                f"FPS: {fps:4.1f} | RSS: {m['rss_mb']:6.2f} MB (Delta: {m['rss_mb'] - initial_soak_mem:+5.2f} MB) | "
                f"Threads: {m['threads']} | GPU Enc: {gpu['encoder_pct']:4.1f}%"
            )
            last_report = now

    soak_track.stop()
    await asyncio.sleep(0.2)
    gc.collect()

    final_soak_metrics = get_process_metrics()
    print(f"  --> Soak Session Finished: {frames_delivered} frames delivered.")
    print(f"  --> Memory Stability: Initial={initial_soak_mem:.2f} MB, Final={final_soak_metrics['rss_mb']:.2f} MB (Diff: {final_soak_metrics['rss_mb'] - initial_soak_mem:+.2f} MB)")
    print(f"  --> Capture Workers at End: {capture_hub.get_active_worker_count()}")
    assert capture_hub.get_active_worker_count() == 0

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 4: Control-Lock Churn Test (100 iterations)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n[Phase 4] Control Arbitration Lock Churn (100 cycles)...")
    class MockWS:
        def __init__(self, id_):
            self.id = id_
        async def send_json(self, data):
            pass

    for i in range(100):
        ws_a = MockWS(f"ws-a-{i}")
        ws_b = MockWS(f"ws-b-{i}")
        await control_manager.register_socket(f"ws-a-{i}", ws_a)
        await control_manager.register_socket(f"ws-b-{i}", ws_b)

        # A acquires
        ok_a, _, _ = await control_manager.acquire_control(user_id=1, username="UserA", ws_id=f"ws-a-{i}")
        assert ok_a is True
        # B busy
        ok_b, _, _ = await control_manager.acquire_control(user_id=2, username="UserB", ws_id=f"ws-b-{i}")
        assert ok_b is False
        # A drops socket
        await control_manager.unregister_socket(f"ws-a-{i}", user_id=1)
        assert control_manager.get_active_controller_info() is None
        # B acquires
        ok_b2, _, _ = await control_manager.acquire_control(user_id=2, username="UserB", ws_id=f"ws-b-{i}")
        assert ok_b2 is True
        # B releases and leaves
        await control_manager.release_control(user_id=2, ws_id=f"ws-b-{i}")
        await control_manager.unregister_socket(f"ws-b-{i}", user_id=2)
        assert control_manager.get_active_controller_info() is None

    assert len(control_manager._connected_sockets) == 0
    assert control_manager.active_controller is None
    print("  --> Control Lock Churn Passed: 100/100 successful, 0 orphaned locks.")

    # ─────────────────────────────────────────────────────────────────────────
    # Summary
    # ─────────────────────────────────────────────────────────────────────────
    final = get_process_metrics()
    print("\n" + "=" * 70)
    print("PHASE 3 M3 BENCHMARK COMPLETE — ALL CRITERIA VERIFIED")
    print("=" * 70)
    print(f"  Baseline RSS:    {baseline['rss_mb']:.2f} MB")
    print(f"  Final Post RSS:  {final['rss_mb']:.2f} MB (Overall Change: {final['rss_mb'] - baseline['rss_mb']:+.2f} MB)")
    print(f"  Threads:         Baseline={baseline['threads']} --> Final={final['threads']}")
    print(f"  Active Workers:  {capture_hub.get_active_worker_count()}")
    print(f"  Controller:      {control_manager.get_active_controller_info()}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=50)
    parser.add_argument("--soak-seconds", type=int, default=120)
    args = parser.parse_args()
    asyncio.run(run_soak_benchmark(cycles=args.cycles, soak_seconds=args.soak_seconds))
