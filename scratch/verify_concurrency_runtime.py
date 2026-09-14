"""
scratch/verify_concurrency_runtime.py — Runtime verification of multi-viewer shared capture & control arbitration.
"""
import asyncio
import threading
import time
from app.services.screen_track import ScreenTrack, capture_hub, get_active_encoder
from app.services.state import control_manager

async def run_verification():
    print("==================================================")
    print("PHASE 3 M2: RUNTIME MULTI-CLIENT VERIFICATION")
    print("==================================================")

    # 1. Verify Baseline State
    print("\n[Step 1] Baseline State:")
    worker = capture_hub.get_worker(1)
    print(f"  Capture worker running: {worker.is_running}")
    print(f"  Active capture threads in hub: {capture_hub.get_active_worker_count()}")
    print(f"  Active controller: {control_manager.get_active_controller_info()}")
    print(f"  NVENC Encoder: {get_active_encoder()}")
    assert capture_hub.get_active_worker_count() == 0, "Baseline active workers should be 0"

    # 2. Viewer A connects (1280x720, 30 FPS)
    print("\n[Step 2] Viewer A connects (1280x720):")
    track_a = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
    # Wait for first frame
    frame_a1 = await track_a.recv()
    print(f"  Viewer A received frame: {frame_a1.width}x{frame_a1.height} (PTS: {frame_a1.pts})")
    print(f"  Subscribers on Display 1: {worker.subscriber_count}")
    print(f"  Active capture threads in hub: {capture_hub.get_active_worker_count()}")
    assert worker.subscriber_count == 1
    assert capture_hub.get_active_worker_count() == 1
    assert worker.is_running is True

    # 3. Viewer B connects (1920x1080, 30 FPS) — same monitor
    print("\n[Step 3] Viewer B connects (1920x1080):")
    track_b = ScreenTrack(monitor_index=1, width=1920, height=1080, fps=30)
    frame_b1 = await track_b.recv()
    frame_a2 = await track_a.recv()
    print(f"  Viewer B received frame: {frame_b1.width}x{frame_b1.height} (PTS: {frame_b1.pts})")
    print(f"  Viewer A received frame: {frame_a2.width}x{frame_a2.height} (PTS: {frame_a2.pts})")
    print(f"  Subscribers on Display 1: {worker.subscriber_count}")
    print(f"  Active capture threads in hub: {capture_hub.get_active_worker_count()}")
    assert worker.subscriber_count == 2
    assert capture_hub.get_active_worker_count() == 1, "Must NOT spawn a 2nd capture thread for same monitor!"

    # 4. Control Arbitration Test
    print("\n[Step 4] Control Arbitration:")
    # Register mock WebSocket connections
    class MockWS:
        def __init__(self, name):
            self.name = name
            self.messages = []
        async def send_text(self, text):
            self.messages.append(text)

    ws_a = MockWS("ws_a")
    ws_b = MockWS("ws_b")
    await control_manager.register_socket("ws-a", ws_a)
    await control_manager.register_socket("ws-b", ws_b)

    # 4a. User A requests control -> GRANTED
    success_a, status_a, info_a = await control_manager.acquire_control(user_id=1, username="ViewerA", ws_id="ws-a")
    print(f"  Viewer A acquire control: success={success_a}, status={status_a}, controller={info_a['username']}")
    assert success_a is True and status_a == "granted"

    # 4b. User B requests control -> REJECTED (BUSY)
    success_b, status_b, info_b = await control_manager.acquire_control(user_id=2, username="ViewerB", ws_id="ws-b")
    print(f"  Viewer B acquire control: success={success_b}, status={status_b}, active_controller={info_b['username']}")
    assert success_b is False and status_b == "busy"

    # 4c. User A disconnects WebSocket -> Token released automatically
    print("  Viewer A disconnects socket...")
    released = await control_manager.unregister_socket("ws-a", user_id=1)
    print(f"  Released controller info: {released}")
    assert control_manager.get_active_controller_info() is None

    # 4d. User B requests control -> GRANTED
    success_b2, status_b2, info_b2 = await control_manager.acquire_control(user_id=2, username="ViewerB", ws_id="ws-b")
    print(f"  Viewer B retry acquire control: success={success_b2}, status={status_b2}, controller={info_b2['username']}")
    assert success_b2 is True and status_b2 == "granted"

    # Clean up User B control
    await control_manager.release_control(user_id=2, ws_id="ws-b")
    await control_manager.unregister_socket("ws-b", user_id=2)
    assert control_manager.get_active_controller_info() is None

    # 5. Teardown & Lifecycle Verification
    print("\n[Step 5] Teardown & Capture Thread Lifecycle:")
    # Viewer A leaves
    track_a.stop()
    print(f"  Viewer A stopped. Subscribers on Display 1: {worker.subscriber_count}, Active capture threads: {capture_hub.get_active_worker_count()}")
    assert worker.subscriber_count == 1
    assert capture_hub.get_active_worker_count() == 1

    # Viewer B leaves
    track_b.stop()
    # Wait for thread shutdown
    await asyncio.sleep(0.15)
    print(f"  Viewer B stopped. Subscribers on Display 1: {worker.subscriber_count}, Active capture threads: {capture_hub.get_active_worker_count()}")
    print(f"  Worker running: {worker.is_running}")
    assert worker.subscriber_count == 0
    assert capture_hub.get_active_worker_count() == 0
    assert worker.is_running is False

    print("\n==================================================")
    print("ALL RUNTIME MULTI-CLIENT CHECKS PASSED!")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(run_verification())
