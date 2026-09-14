"""
scratch/bench_encoders.py — Benchmark and verify NVENC vs CPU libx264 hardware encoding.
"""
import time
import subprocess
import os
import ctypes
from ctypes import wintypes
import av
import fractions
import numpy as np
from app.services.encoder import HardwareH264Encoder, set_active_encoder, check_nvenc_available
from app.core.config import Settings
from unittest.mock import patch


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


def get_process_memory_mb() -> float:
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def get_gpu_encoder_utilization() -> tuple[float, float, float]:
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
    except Exception as e:
        return 0.0, 0.0, 0.0


def benchmark_mode(mode_name: str, num_frames: int = 180, width: int = 1920, height: int = 1080, target_bitrate: int = 4_000_000):
    print(f"\n=======================================================")
    print(f"BENCHMARKING MODE: {mode_name.upper()} ({width}x{height} @ {target_bitrate // 1000} kbps, {num_frames} frames)")
    print(f"=======================================================")

    mock_settings = Settings(ENCODER=mode_name)
    
    with patch("app.services.encoder.get_settings", return_value=mock_settings):
        set_active_encoder(None)
        encoder = HardwareH264Encoder()
        encoder.target_bitrate = target_bitrate

        # Warm up
        dummy_frame = av.VideoFrame.from_ndarray(
            np.zeros((height, width, 3), dtype=np.uint8), format="bgr24"
        )
        dummy_frame.pts = 0
        dummy_frame.time_base = fractions.Fraction(1, 30)
        _ = encoder.encode(dummy_frame, force_keyframe=True)

        print(f"Active encoder selected: {encoder._current_encoder_name}")
        
        gpu_util_before, enc_util_before, mem_before = get_gpu_encoder_utilization()

        total_bytes = 0
        total_packets = 0
        cpu_t0 = time.process_time()
        t0 = time.perf_counter()

        for i in range(1, num_frames + 1):
            # Create synthetic frame with dynamic motion
            img = np.full((height, width, 3), (i * 2) % 255, dtype=np.uint8)
            img[100:400, 100:400] = (i * 10) % 255
            frame = av.VideoFrame.from_ndarray(img, format="bgr24")
            frame.pts = i * 3000
            frame.time_base = fractions.Fraction(1, 90000)

            # Force keyframe every 60 frames
            force_key = (i % 60 == 0)
            packets, ts = encoder.encode(frame, force_keyframe=force_key)
            for p in packets:
                total_bytes += len(p)
                total_packets += 1

        t1 = time.perf_counter()
        cpu_t1 = time.process_time()
        
        elapsed_wall = t1 - t0
        elapsed_cpu = cpu_t1 - cpu_t0
        fps = num_frames / elapsed_wall
        kbps = (total_bytes * 8) / (elapsed_wall * 1000)
        cpu_pct = (elapsed_cpu / elapsed_wall) * 100 if elapsed_wall > 0 else 0.0
        ram_mb = get_process_memory_mb()
        gpu_util_after, enc_util_after, mem_after = get_gpu_encoder_utilization()

        print(f"Results for {mode_name.upper()}:")
        print(f"  • Active Codec:        {encoder._current_encoder_name}")
        print(f"  • Frames Encoded:      {num_frames} in {elapsed_wall:.3f}s ({fps:.1f} FPS)")
        print(f"  • RTP Packets Sent:    {total_packets} ({total_bytes / 1024:.1f} KB)")
        print(f"  • Effective Bitrate:   {kbps:.1f} kbps (target: {target_bitrate // 1000} kbps)")
        print(f"  • Process CPU Work:    {elapsed_cpu:.3f}s CPU time ({cpu_pct:.1f}% equivalent core utilization)")
        print(f"  • Process RAM:         {ram_mb:.1f} MB")
        print(f"  • GPU Video Encode %:  {enc_util_after:.1f}%")
        print(f"  • GPU 3D Util %:       {gpu_util_after:.1f}%")
        print(f"  • GPU VRAM Used:       {mem_after:.0f} MB")

        return {
            "mode": mode_name,
            "codec": encoder._current_encoder_name,
            "fps": fps,
            "bitrate_kbps": kbps,
            "cpu_time_s": elapsed_cpu,
            "cpu_pct": cpu_pct,
            "ram_mb": ram_mb,
            "gpu_enc_pct": enc_util_after,
            "gpu_mem_mb": mem_after,
        }


if __name__ == "__main__":
    ok, desc = check_nvenc_available()
    print(f"NVENC Capability Probe: ok={ok}, desc='{desc}'")

    nvenc_results = benchmark_mode("auto", num_frames=180, width=1920, height=1080, target_bitrate=4_000_000)
    cpu_results = benchmark_mode("cpu", num_frames=180, width=1920, height=1080, target_bitrate=4_000_000)

    print("\n=======================================================")
    print("COMPARISON SUMMARY: NVENC vs CPU (libx264)")
    print("=======================================================")
    print(f"{'Metric':<25} | {'NVIDIA NVENC (auto)':<20} | {'CPU (libx264)':<20}")
    print("-" * 72)
    print(f"{'Active Codec':<25} | {nvenc_results['codec']:<20} | {cpu_results['codec']:<20}")
    print(f"{'Encoding Throughput':<25} | {nvenc_results['fps']:.1f} FPS{'':<14} | {cpu_results['fps']:.1f} FPS")
    print(f"{'Process CPU Time':<25} | {nvenc_results['cpu_time_s']:.3f}s{'':<16} | {cpu_results['cpu_time_s']:.3f}s")
    print(f"{'Process CPU % (norm)':<25} | {nvenc_results['cpu_pct']:.1f}%{'':<16} | {cpu_results['cpu_pct']:.1f}%")
    print(f"{'GPU Video Encode %':<25} | {nvenc_results['gpu_enc_pct']:.1f}%{'':<16} | {cpu_results['gpu_enc_pct']:.1f}%")
    print(f"{'RAM Usage':<25} | {nvenc_results['ram_mb']:.1f} MB{'':<13} | {cpu_results['ram_mb']:.1f} MB")
    print(f"{'Bitrate Output':<25} | {nvenc_results['bitrate_kbps']:.1f} kbps{'':<10} | {cpu_results['bitrate_kbps']:.1f} kbps")
