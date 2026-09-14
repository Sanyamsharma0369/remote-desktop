"""
scratch/profile_pipeline.py — Phase 3 M1 Resource Profiling & Frame Pipeline Breakdown Harness.

Measures:
1. Idle server footprint (0 viewers): Process CPU, RSS RAM, Threads, Handles, GPU Util, Video Encode %.
2. Active 1080p @ 30 FPS NVENC stream: Per-stage frame latencies (MSS, Conversion, Resize, PyAV, NVENC, RTP).
3. Stage percentiles (Avg, P50, P95, Max) and % time distribution.
4. Resource impact comparison.
"""
from __future__ import annotations

import time
import subprocess
import threading
import os
import ctypes
from ctypes import wintypes
import mss
import cv2
import av
import fractions
import numpy as np
from app.services.encoder import HardwareH264Encoder, set_active_encoder, check_nvenc_available
from app.core.windows_desktop import attach_interactive_desktop
from app.core.config import Settings
from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
# Windows Native Process Telemetry Helpers
# ─────────────────────────────────────────────────────────────────────────────

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

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetProcessHandleCount.restype = wintypes.BOOL

kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL


def get_process_rss_mb() -> float:
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def get_process_handle_count() -> int:
    try:
        handle = kernel32.GetCurrentProcess()
        count = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            return count.value
    except Exception:
        pass
    return 0


def get_gpu_telemetry() -> tuple[float, float, float]:
    """Returns (gpu_util_pct, enc_util_pct, mem_mb)."""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True
        )
        parts = [float(x.strip()) for x in res.stdout.strip().split(",")]
        return parts[0], parts[1], parts[2]
    except Exception:
        return 0.0, 0.0, 0.0


def get_process_cpu_time() -> float:
    """Returns total kernel + user process CPU time in seconds."""
    creation = FILETIME()
    exit_t = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    handle = kernel32.GetCurrentProcess()
    if kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t), ctypes.byref(kernel), ctypes.byref(user)):
        k_time = (kernel.dwHighDateTime << 32) + kernel.dwLowDateTime
        u_time = (user.dwHighDateTime << 32) + user.dwLowDateTime
        return (k_time + u_time) / 10_000_000.0
    return time.process_time()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Idle Profile (0 Viewers)
# ─────────────────────────────────────────────────────────────────────────────

def profile_idle_state(duration_seconds: float = 6.0) -> dict:
    print("\n=======================================================")
    print(f"MEASURING IDLE BASELINE (0 Viewers, duration={duration_seconds}s)")
    print("=======================================================")

    t0_wall = time.perf_counter()
    t0_cpu = get_process_cpu_time()
    
    samples_rss = []
    samples_gpu = []
    samples_enc = []
    samples_threads = []
    samples_handles = []

    iterations = int(duration_seconds / 0.5)
    for _ in range(iterations):
        time.sleep(0.5)
        samples_rss.append(get_process_rss_mb())
        samples_threads.append(threading.active_count())
        samples_handles.append(get_process_handle_count())
        gpu_u, enc_u, _ = get_gpu_telemetry()
        samples_gpu.append(gpu_u)
        samples_enc.append(enc_u)

    t1_wall = time.perf_counter()
    t1_cpu = get_process_cpu_time()

    elapsed_wall = t1_wall - t0_wall
    elapsed_cpu = t1_cpu - t0_cpu
    cpu_pct = (elapsed_cpu / elapsed_wall) * 100 if elapsed_wall > 0 else 0.0

    results = {
        "duration_s": elapsed_wall,
        "cpu_time_s": elapsed_cpu,
        "cpu_pct": cpu_pct,
        "rss_mb_avg": float(np.mean(samples_rss)),
        "rss_mb_peak": float(np.max(samples_rss)),
        "threads_avg": float(np.mean(samples_threads)),
        "handles_avg": float(np.mean(samples_handles)),
        "gpu_util_avg": float(np.mean(samples_gpu)),
        "gpu_enc_avg": float(np.mean(samples_enc)),
    }

    print(f"Idle Profile Results:")
    print(f"  • Process CPU %:        {results['cpu_pct']:.2f}% ({results['cpu_time_s']:.3f}s over {results['duration_s']:.2f}s)")
    print(f"  • RSS Memory:           {results['rss_mb_avg']:.1f} MB (peak: {results['rss_mb_peak']:.1f} MB)")
    print(f"  • Active Threads:       {results['threads_avg']:.0f}")
    print(f"  • Process Handles:      {results['handles_avg']:.0f}")
    print(f"  • GPU 3D Util %:        {results['gpu_util_avg']:.1f}%")
    print(f"  • GPU Video Encode %:   {results['gpu_enc_avg']:.1f}%")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 2. Active 1080p @ 30 FPS Stream Profile & Pipeline Breakdown
# ─────────────────────────────────────────────────────────────────────────────

def profile_active_stream_pipeline(
    num_frames: int = 240,
    target_w: int = 1920,
    target_h: int = 1080,
    fps: int = 30,
    bitrate: int = 4_000_000
) -> dict:
    print("\n=======================================================")
    print(f"MEASURING ACTIVE 1080p @ 30 FPS STREAM ({num_frames} frames)")
    print("=======================================================")

    attach_interactive_desktop()
    mock_settings = Settings(ENCODER="auto")
    with patch("app.services.encoder.get_settings", return_value=mock_settings):
        set_active_encoder(None)
        encoder = HardwareH264Encoder()
        encoder.target_bitrate = bitrate

        # High-resolution stage timing records (in milliseconds)
        timing_mss_grab: list[float] = []
        timing_conversion: list[float] = []
        timing_resize: list[float] = []
        timing_pyav_frame: list[float] = []
        timing_nvenc_encode: list[float] = []
        timing_rtp_packetize: list[float] = []
        timing_total_pipeline: list[float] = []

        total_bytes = 0
        total_packets = 0

        # Prime MSS instance
        sct = mss.mss()
        monitors = sct.monitors
        monitor_index = 1 if len(monitors) > 1 else 0
        monitor = monitors[monitor_index]

        # Warm up encoder with 1 frame
        raw = sct.grab(monitor)
        warm_img = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)[:, :, :3]
        warm_resized = cv2.resize(warm_img, (target_w, target_h), interpolation=cv2.INTER_AREA)
        warm_frame = av.VideoFrame.from_ndarray(warm_resized, format="bgr24")
        warm_frame.pts = 0
        warm_frame.time_base = fractions.Fraction(1, fps)
        _ = encoder.encode(warm_frame, force_keyframe=True)

        print(f"Active encoder selected: {encoder._current_encoder_name}")
        print(f"Capturing from monitor: {monitor['width']}x{monitor['height']} -> Target: {target_w}x{target_h} @ {fps} FPS")

        gpu_samples = []
        enc_samples = []
        rss_samples = []

        t0_wall = time.perf_counter()
        t0_cpu = get_process_cpu_time()

        frame_interval = 1.0 / fps
        next_frame_time = time.perf_counter()

        for i in range(1, num_frames + 1):
            pipe_start = time.perf_counter_ns()

            # ── Stage A: MSS Screen Capture ──
            t_a0 = time.perf_counter_ns()
            raw_frame = sct.grab(monitor)
            t_a1 = time.perf_counter_ns()
            timing_mss_grab.append((t_a1 - t_a0) / 1_000_000.0)

            # ── Stage B: BGRA Buffer to BGR NumPy ──
            t_b0 = time.perf_counter_ns()
            img_bgr = np.frombuffer(raw_frame.bgra, dtype=np.uint8).reshape(
                raw_frame.height, raw_frame.width, 4
            )[:, :, :3]
            t_b1 = time.perf_counter_ns()
            timing_conversion.append((t_b1 - t_b0) / 1_000_000.0)

            # ── Stage C: Resize to target resolution ──
            t_c0 = time.perf_counter_ns()
            sh, sw = img_bgr.shape[:2]
            if (sw, sh) != (target_w, target_h):
                img_resized = cv2.resize(img_bgr, (target_w, target_h), interpolation=cv2.INTER_AREA)
            else:
                img_resized = img_bgr
            img_contiguous = np.ascontiguousarray(img_resized)
            t_c1 = time.perf_counter_ns()
            timing_resize.append((t_c1 - t_c0) / 1_000_000.0)

            # ── Stage D: PyAV VideoFrame Allocation & PTS ──
            t_d0 = time.perf_counter_ns()
            v_frame = av.VideoFrame.from_ndarray(img_contiguous, format="bgr24")
            v_frame.pts = i * int(90000 / fps)
            v_frame.time_base = fractions.Fraction(1, 90000)
            t_d1 = time.perf_counter_ns()
            timing_pyav_frame.append((t_d1 - t_d0) / 1_000_000.0)

            # ── Stage E & F: NVENC Hardware Encode ──
            t_e0 = time.perf_counter_ns()
            force_key = (i % (fps * 2) == 0)
            # Isolate encode call
            packages = encoder._encode_frame(v_frame, force_keyframe=force_key)
            # Execute generator / get NAL packages
            nal_list = list(packages)
            t_e1 = time.perf_counter_ns()
            timing_nvenc_encode.append((t_e1 - t_e0) / 1_000_000.0)

            # ── Stage G: RTP Packetization (STAP-A / FU-A) ──
            t_g0 = time.perf_counter_ns()
            rtp_packets = encoder._packetize(nal_list)
            t_g1 = time.perf_counter_ns()
            timing_rtp_packetize.append((t_g1 - t_g0) / 1_000_000.0)

            pipe_end = time.perf_counter_ns()
            timing_total_pipeline.append((pipe_end - pipe_start) / 1_000_000.0)

            for p in rtp_packets:
                total_bytes += len(p)
                total_packets += 1

            if i % 30 == 0:
                g_u, e_u, _ = get_gpu_telemetry()
                gpu_samples.append(g_u)
                enc_samples.append(e_u)
                rss_samples.append(get_process_rss_mb())

            # Real-time 30 FPS pacing
            next_frame_time += frame_interval
            sleep_time = next_frame_time - time.perf_counter()
            if sleep_time > 0.001:
                time.sleep(sleep_time)

        sct.close()

        t1_wall = time.perf_counter()
        t1_cpu = get_process_cpu_time()

        elapsed_wall = t1_wall - t0_wall
        elapsed_cpu = t1_cpu - t0_cpu
        achieved_fps = num_frames / elapsed_wall
        cpu_pct = (elapsed_cpu / elapsed_wall) * 100 if elapsed_wall > 0 else 0.0
        kbps = (total_bytes * 8) / (elapsed_wall * 1000)

        def calc_stats(arr: list[float]) -> dict:
            return {
                "avg": float(np.mean(arr)),
                "p50": float(np.percentile(arr, 50)),
                "p95": float(np.percentile(arr, 95)),
                "max": float(np.max(arr)),
                "min": float(np.min(arr)),
            }

        stats_mss = calc_stats(timing_mss_grab)
        stats_conv = calc_stats(timing_conversion)
        stats_resize = calc_stats(timing_resize)
        stats_pyav = calc_stats(timing_pyav_frame)
        stats_nvenc = calc_stats(timing_nvenc_encode)
        stats_rtp = calc_stats(timing_rtp_packetize)
        stats_total = calc_stats(timing_total_pipeline)

        results = {
            "num_frames": num_frames,
            "target_fps": fps,
            "achieved_fps": achieved_fps,
            "duration_s": elapsed_wall,
            "cpu_time_s": elapsed_cpu,
            "cpu_pct": cpu_pct,
            "bitrate_kbps": kbps,
            "total_packets": total_packets,
            "rss_mb_avg": float(np.mean(rss_samples)) if rss_samples else get_process_rss_mb(),
            "rss_mb_peak": float(np.max(rss_samples)) if rss_samples else get_process_rss_mb(),
            "gpu_util_avg": float(np.mean(gpu_samples)) if gpu_samples else 0.0,
            "gpu_enc_avg": float(np.mean(enc_samples)) if enc_samples else 0.0,
            "threads_count": threading.active_count(),
            "handles_count": get_process_handle_count(),
            "stage_stats": {
                "mss_grab": stats_mss,
                "conversion": stats_conv,
                "resize": stats_resize,
                "pyav_frame": stats_pyav,
                "nvenc_encode": stats_nvenc,
                "rtp_packetize": stats_rtp,
                "total_pipeline": stats_total,
            }
        }

        return results


# ─────────────────────────────────────────────────────────────────────────────
# 3. Execution & Summary Generation
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ok, desc = check_nvenc_available()
    print(f"Hardware capability: NVENC ok={ok} ({desc})")

    # 1. Idle profiling
    idle_res = profile_idle_state(duration_seconds=5.0)

    # 2. Active 1080p @ 30 FPS stream profiling (240 frames = 8 seconds @ 30fps)
    active_res = profile_active_stream_pipeline(num_frames=240, target_w=1920, target_h=1080, fps=30, bitrate=4_000_000)

    print("\n" + "=" * 80)
    print("PHASE 3 M1 — COMPREHENSIVE RESOURCE COMPARISON TABLE")
    print("=" * 80)
    print(f"{'State':<32} | {'Process CPU %':<14} | {'RSS RAM':<12} | {'Threads':<8} | {'GPU 3D %':<9} | {'Video Encode %':<14} | {'FPS':<6}")
    print("-" * 105)
    print(f"{'Idle (0 Viewers)':<32} | {idle_res['cpu_pct']:<13.2f}% | {idle_res['rss_mb_avg']:<9.1f} MB | {idle_res['threads_avg']:<8.0f} | {idle_res['gpu_util_avg']:<8.1f}% | {idle_res['gpu_enc_avg']:<13.1f}% | {'—':<6}")
    print(f"{'Active 1080p/30 NVENC (1 Viewer)':<32} | {active_res['cpu_pct']:<13.2f}% | {active_res['rss_mb_avg']:<9.1f} MB | {active_res['threads_count']:<8.0f} | {active_res['gpu_util_avg']:<8.1f}% | {active_res['gpu_enc_avg']:<13.1f}% | {active_res['achieved_fps']:<6.1f}")

    stages = active_res["stage_stats"]
    total_avg = stages["total_pipeline"]["avg"]

    print("\n" + "=" * 80)
    print("FRAME PIPELINE STAGE LATENCY BREAKDOWN (1080p @ 30 FPS NVENC)")
    print("=" * 80)
    print(f"{'Stage':<28} | {'Avg (ms)':<9} | {'P50 (ms)':<9} | {'P95 (ms)':<9} | {'Max (ms)':<9} | {'% of Pipeline':<14}")
    print("-" * 88)

    stage_names = [
        ("A. MSS screen grab", "mss_grab"),
        ("B. BGRA->BGR conversion", "conversion"),
        ("C. cv2.resize downscale", "resize"),
        ("D. PyAV VideoFrame alloc", "pyav_frame"),
        ("E. NVENC hardware encode", "nvenc_encode"),
        ("F. RTP packetization", "rtp_packetize"),
    ]

    for label, key in stage_names:
        st = stages[key]
        pct = (st["avg"] / total_avg) * 100 if total_avg > 0 else 0.0
        print(f"{label:<28} | {st['avg']:<9.3f} | {st['p50']:<9.3f} | {st['p95']:<9.3f} | {st['max']:<9.3f} | {pct:<13.1f}%")

    print("-" * 88)
    tot = stages["total_pipeline"]
    print(f"{'TOTAL PIPELINE / FRAME':<28} | {tot['avg']:<9.3f} | {tot['p50']:<9.3f} | {tot['p95']:<9.3f} | {tot['max']:<9.3f} | {'100.0%':<14}")
    print("=" * 80)

    # Frame budget analysis (30 FPS budget = 33.33ms)
    frame_budget_ms = 1000.0 / 30.0
    headroom_ms = frame_budget_ms - tot['avg']
    headroom_pct = (headroom_ms / frame_budget_ms) * 100
    print(f"\nReal-Time Frame Budget (30 FPS = {frame_budget_ms:.2f} ms/frame):")
    print(f"  • Average frame processing time: {tot['avg']:.2f} ms")
    print(f"  • Real-time processing headroom:  {headroom_ms:.2f} ms ({headroom_pct:.1f}% idle headroom per frame interval)")
