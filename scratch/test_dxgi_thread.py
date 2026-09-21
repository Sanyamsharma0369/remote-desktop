"""
scratch/test_dxgi_thread.py — Test thread desktop attachment + DXGI duplication.
"""
import ctypes
import os
import sys
import threading
import time

def run():
    from app.core.windows_desktop import attach_interactive_desktop
    ok = attach_interactive_desktop()
    print(f"attach_interactive_desktop returned: {ok}")

    import dxcam
    try:
        cam = dxcam.create(device_idx=0, output_idx=0)
        print("Camera created successfully!")
        f = cam.grab()
        print("Frame grabbed:", f.shape if f is not None else None)
        cam.release()
    except Exception as e:
        print("DXCam error:", type(e), e)

if __name__ == "__main__":
    t = threading.Thread(target=run)
    t.start()
    t.join()
