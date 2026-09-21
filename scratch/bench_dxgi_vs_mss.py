"""
scratch/bench_dxgi_vs_mss.py — Empirical comparison benchmark: MSS vs DXGI Desktop Duplication.
"""
from __future__ import annotations

import gc
import threading
import time
from fractions import Fraction
import cv2
import dxcam
import mss
import numpy as np
import av
from app.core.windows_desktop import attach_interactive_desktop
from app.services.encoder import get_active_encoder


def benchmark_pipeline(n_frames: int = 100):
    print("=" * 75)
    print("PHASE 3 M4: EMPIRICAL BENCHMARK — MSS (BitBlt) vs DXGI Desktop Duplication")
    print("=" * 75)
    print(f"Active Hardware Encoder: {get_active_encoder()}")
    print(f"Sample Size: {n_frames} frames per method\n")

    attach_interactive_desktop()

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Raw Screen Capture Alone
    # ─────────────────────────────────────────────────────────────────────────
    print("[Test 1] Raw Screen Capture Latency (1920x1200 native):")

    # 1A. MSS Raw Capture
    sct = mss.mss()
    mon = sct.monitors[1]
    mss_times = []
    for _ in range(n_frames):
        t0 = time.perf_counter()
        raw = sct.grab(mon)
        img = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)[:, :, :3]
        img = np.ascontiguousarray(img)
        t1 = time.perf_counter()
        mss_times.append((t1 - t0) * 1000)
    sct.close()

    mss_avg = np.mean(mss_times)
    mss_p95 = np.percentile(mss_times, 95)
    mss_fps = 1000.0 / mss_avg if mss_avg > 0 else 0
    print(f"  MSS (BitBlt) Capture:      {mss_avg:6.2f} ms/frame | p95: {mss_p95:6.2f} ms | Throughput: {mss_fps:5.1f} FPS")

    # 1B. DXGI Raw Capture
    cam = dxcam.create(device_idx=0, output_idx=0)
    dxgi_times = []
    # Warmup
    for _ in range(5):
        cam.grab()

    for _ in range(n_frames):
        t0 = time.perf_counter()
        img = cam.grab()
        t1 = time.perf_counter()
        if img is not None:
            dxgi_times.append((t1 - t0) * 1000)
    cam.release()
    del cam

    dxgi_avg = np.mean(dxgi_times)
    dxgi_p95 = np.percentile(dxgi_times, 95)
    dxgi_fps = 1000.0 / dxgi_avg if dxgi_avg > 0 else 0
    print(f"  DXGI Desktop Duplication: {dxgi_avg:6.2f} ms/frame | p95: {dxgi_p95:6.2f} ms | Throughput: {dxgi_fps:5.1f} FPS")
    speedup = mss_avg / dxgi_avg if dxgi_avg > 0 else 1.0
    print(f"  --> Raw Capture Speedup:   {speedup:.2f}x faster with DXGI ({mss_avg - dxgi_avg:+.2f} ms savings per frame)\n")

    # ─────────────────────────────────────────────────────────────────────────
    # 2. End-to-End Pipeline: Capture + Downscale (720p) + NVENC Encode
    # ─────────────────────────────────────────────────────────────────────────
    print("[Test 2] Full Pipeline: Capture -> Resize (1280x720) -> NVENC H.264 Encode:")

    encoder_name = get_active_encoder()

    # 2A. MSS Pipeline
    sct = mss.mss()
    codec_mss = av.CodecContext.create(encoder_name, "w")
    codec_mss.width = 1280
    codec_mss.height = 720
    codec_mss.pix_fmt = "yuv420p"
    codec_mss.time_base = Fraction(1, 90000)
    codec_mss.open()

    mss_pipe_times = []
    for i in range(n_frames):
        t0 = time.perf_counter()
        raw = sct.grab(mon)
        img = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)[:, :, :3]
        resized = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)
        frame = av.VideoFrame.from_ndarray(resized, format="bgr24")
        frame.pts = i * 3000
        packets = codec_mss.encode(frame)
        t1 = time.perf_counter()
        mss_pipe_times.append((t1 - t0) * 1000)
    sct.close()

    mss_pipe_avg = np.mean(mss_pipe_times)
    mss_pipe_fps = 1000.0 / mss_pipe_avg

    # 2B. DXGI Pipeline
    cam = dxcam.create(device_idx=0, output_idx=0)
    codec_dxgi = av.CodecContext.create(encoder_name, "w")
    codec_dxgi.width = 1280
    codec_dxgi.height = 720
    codec_dxgi.pix_fmt = "yuv420p"
    codec_dxgi.time_base = Fraction(1, 90000)
    codec_dxgi.open()

    dxgi_pipe_times = []
    for i in range(n_frames):
        t0 = time.perf_counter()
        img = cam.grab()
        if img is None:
            continue
        resized = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)
        frame = av.VideoFrame.from_ndarray(resized, format="bgr24")
        frame.pts = i * 3000
        packets = codec_dxgi.encode(frame)
        t1 = time.perf_counter()
        dxgi_pipe_times.append((t1 - t0) * 1000)
    cam.release()
    del cam

    dxgi_pipe_avg = np.mean(dxgi_pipe_times)
    dxgi_pipe_fps = 1000.0 / dxgi_pipe_avg

    print(f"  MSS Pipeline:  {mss_pipe_avg:6.2f} ms/frame | Throughput: {mss_pipe_fps:5.1f} FPS")
    print(f"  DXGI Pipeline: {dxgi_pipe_avg:6.2f} ms/frame | Throughput: {dxgi_pipe_fps:5.1f} FPS")
    print(f"  --> Pipeline Speedup: {mss_pipe_avg / dxgi_pipe_avg:.2f}x ({mss_pipe_avg - dxgi_pipe_avg:+.2f} ms savings per frame)")

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Native 1:1 Pipeline (No CPU Resize)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n[Test 3] Native 1:1 Mode: DXGI Native (1920x1200) -> NVENC H.264 Encode:")
    cam = dxcam.create(device_idx=0, output_idx=0)
    codec_native = av.CodecContext.create(encoder_name, "w")
    codec_native.width = 1920
    codec_native.height = 1200
    codec_native.pix_fmt = "yuv420p"
    codec_native.time_base = Fraction(1, 90000)
    codec_native.open()

    dxgi_native_times = []
    for i in range(n_frames):
        t0 = time.perf_counter()
        img = cam.grab()
        if img is None:
            continue
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        frame.pts = i * 3000
        packets = codec_native.encode(frame)
        t1 = time.perf_counter()
        dxgi_native_times.append((t1 - t0) * 1000)
    cam.release()
    del cam

    native_avg = np.mean(dxgi_native_times)
    native_fps = 1000.0 / native_avg
    print(f"  DXGI + NVENC Native 1:1: {native_avg:6.2f} ms/frame | Throughput: {native_fps:5.1f} FPS")

    print("\n" + "=" * 75)
    print("BENCHMARK SUMMARY TABLE")
    print("=" * 75)
    print(f"{'Pipeline':<35} | {'Latency (ms)':<14} | {'Throughput (FPS)':<16} | {'Budget (30 FPS)':<15}")
    print("-" * 75)
    print(f"{'MSS + Resize + NVENC':<35} | {mss_pipe_avg:6.2f} ms      | {mss_pipe_fps:5.1f} FPS        | {mss_pipe_avg < 33.33}")
    print(f"{'DXGI + Resize + NVENC':<35} | {dxgi_pipe_avg:6.2f} ms      | {dxgi_pipe_fps:5.1f} FPS        | {dxgi_pipe_avg < 33.33}")
    print(f"{'DXGI + Native 1:1 + NVENC':<35} | {native_avg:6.2f} ms      | {native_fps:5.1f} FPS        | {native_avg < 33.33}")
    print("=" * 75)


if __name__ == "__main__":
    t = threading.Thread(target=benchmark_pipeline, args=(100,))
    t.start()
    t.join()
