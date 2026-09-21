"""
scratch/test_dxgi.py — Test DXGI Desktop Duplication capture & cleanup.
"""
import time
import dxcam
from app.core.windows_desktop import attach_interactive_desktop

def test_dxgi():
    print("Attaching interactive desktop...")
    attach_interactive_desktop()

    print("Creating dxcam instance...")
    cam = dxcam.create(device_idx=0, output_idx=0)
    print("Grabbing 30 frames...")
    t0 = time.perf_counter()
    for i in range(30):
        frame = cam.grab()
        if i == 0 and frame is not None:
            print(f"Frame shape: {frame.shape}, dtype: {frame.dtype}")
    t1 = time.perf_counter()
    elapsed = t1 - t0
    fps = 30 / elapsed if elapsed > 0 else 0
    latency_ms = (elapsed * 1000) / 30
    print(f"DXGI Performance: {fps:.1f} FPS | {latency_ms:.2f} ms/frame")

    print("Cleaning up cam...")
    cam.release()
    del cam
    print("Done!")

if __name__ == "__main__":
    test_dxgi()
