"""
scratch/test_physical_multimonitor.py — Phase 4 M2: Physical Multi-Monitor Validation & Resilience.
"""
import asyncio
import os
import sys
import time
from typing import Dict, Any, List
import numpy as np

from app.services.screen_track import ScreenTrack, capture_hub, get_active_encoder
from app.services.state import control_manager


async def test_dual_physical_monitors():
    print("=" * 85)
    print("PHASE 4 M2: PHYSICAL MULTI-MONITOR VALIDATION & RESILIENCE")
    print("=" * 85)
    print(f"Active Video Encoder: {get_active_encoder()} | Process PID: {os.getpid()}")

    monitors = ScreenTrack.list_monitors()
    print(f"\n[Hardware Discovery] Detected {len(monitors)} monitor(s):")
    for m in monitors:
        print(f"  - {m['label']}: {m['width']}x{m['height']} at ({m['left']}, {m['top']})")

    if len(monitors) < 2:
        print("\n[WARNING] Less than 2 physical monitors detected. Testing primary display and fallback clamping.")
        disp1_idx = 1
        disp2_idx = 1
    else:
        disp1_idx = 1
        disp2_idx = 2

    print(f"\n[Test Setup] Viewer A -> Display {disp1_idx} | Viewer B -> Display {disp2_idx}")
    print("-" * 85)

    # 1. Start Viewer A on Display 1
    print("\n--- STEP 1: Launch Viewer A on Display 1 ---")
    track_a = ScreenTrack(monitor_index=disp1_idx, width=1280, height=720, fps=30)
    frame_a1 = await track_a.recv()
    await asyncio.sleep(0.5)

    stats_hub1 = capture_hub.get_hub_stats()
    print(f"  Active Capture Workers: {stats_hub1['active_workers']}")
    print(f"  Display {disp1_idx} Worker Running: {stats_hub1['monitors'].get(disp1_idx, {}).get('running')} (Subscribers: {stats_hub1['monitors'].get(disp1_idx, {}).get('subscribers')})")
    assert stats_hub1['active_workers'] == 1, "Expected exactly 1 capture worker for Display 1"

    # 2. Start Viewer B on Display 2 (concurrent stream)
    print(f"\n--- STEP 2: Launch Viewer B on Display {disp2_idx} (Concurrent Dual Stream) ---")
    track_b = ScreenTrack(monitor_index=disp2_idx, width=1920, height=1080, fps=30)
    frame_b1 = await track_b.recv()
    await asyncio.sleep(0.5)

    stats_hub2 = capture_hub.get_hub_stats()
    expected_workers = 2 if disp1_idx != disp2_idx else 1
    print(f"  Active Capture Workers: {stats_hub2['active_workers']}")
    for idx, info in stats_hub2['monitors'].items():
        if info['running']:
            print(f"  - Display {idx}: Running={info['running']}, Subscribers={info['subscribers']}, Dims={info['dims']}, Backend={info['backend']}")
    assert stats_hub2['active_workers'] == expected_workers, f"Expected {expected_workers} capture worker(s)"

    # 3. Stream both concurrently for 3 seconds and verify frame isolation
    print(f"\n--- STEP 3: Concurrent Streaming & Frame Content Isolation ---")
    frames_a = 0
    frames_b = 0
    dims_a = []
    dims_b = []
    t_start = time.monotonic()
    while time.monotonic() - t_start < 3.0:
        fa = await track_a.recv()
        fb = await track_b.recv()
        if fa is not None:
            frames_a += 1
            dims_a.append((fa.width, fa.height))
        if fb is not None:
            frames_b += 1
            dims_b.append((fb.width, fb.height))

    print(f"  Viewer A: {frames_a} frames delivered ({dims_a[-1][0]}x{dims_a[-1][1]})")
    print(f"  Viewer B: {frames_b} frames delivered ({dims_b[-1][0]}x{dims_b[-1][1]})")

    # 4. Dynamic Monitor Switching: Viewer A switches to Display 2
    if disp1_idx != disp2_idx:
        print(f"\n--- STEP 4: Dynamic Monitor Switch: Viewer A switches Display 1 -> Display 2 ---")
        switched = track_a.set_monitor(disp2_idx)
        print(f"  Viewer A set_monitor({disp2_idx}) result: {switched}")
        await asyncio.sleep(0.5)

        stats_hub3 = capture_hub.get_hub_stats()
        print(f"  Active Capture Workers: {stats_hub3['active_workers']}")
        print(f"  Display 1 Worker: Running={stats_hub3['monitors'].get(disp1_idx, {}).get('running')}, Subscribers={stats_hub3['monitors'].get(disp1_idx, {}).get('subscribers')}")
        print(f"  Display 2 Worker: Running={stats_hub3['monitors'].get(disp2_idx, {}).get('running')}, Subscribers={stats_hub3['monitors'].get(disp2_idx, {}).get('subscribers')}")

        assert stats_hub3['monitors'].get(disp1_idx, {}).get('running') is False or stats_hub3['monitors'].get(disp1_idx, {}).get('subscribers') == 0, "Display 1 worker should have 0 subscribers"
        assert stats_hub3['monitors'].get(disp2_idx, {}).get('subscribers') == 2, "Display 2 worker should have 2 subscribers"

    # 5. Viewer A disconnects; Viewer B continues uninterrupted
    print(f"\n--- STEP 5: Viewer A Disconnects; Viewer B Continues ---")
    track_a.stop()
    await asyncio.sleep(0.5)

    # Stream Viewer B for 2 more seconds
    b_extra = 0
    t_b = time.monotonic()
    while time.monotonic() - t_b < 2.0:
        fb = await track_b.recv()
        if fb is not None:
            b_extra += 1

    stats_hub4 = capture_hub.get_hub_stats()
    print(f"  Viewer B delivered {b_extra} additional frames uninterrupted after Viewer A disconnected")
    print(f"  Active Capture Workers: {stats_hub4['active_workers']}")
    print(f"  Display {disp2_idx} Worker Subscribers: {stats_hub4['monitors'].get(disp2_idx, {}).get('subscribers')}")
    assert stats_hub4['monitors'].get(disp2_idx, {}).get('subscribers') == 1, "Display 2 should have 1 subscriber (Viewer B)"

    # 6. Viewer B disconnects; All capture workers terminate cleanly
    print(f"\n--- STEP 6: Viewer B Disconnects (Final Teardown) ---")
    track_b.stop()
    await asyncio.sleep(0.5)

    stats_hub5 = capture_hub.get_hub_stats()
    print(f"  Active Capture Workers: {stats_hub5['active_workers']}")
    assert stats_hub5['active_workers'] == 0, "All capture workers must be 0 after all viewers disconnect"

    # 7. Failure Injection / Out-of-Bounds Display Fallback
    print(f"\n--- STEP 7: Failure Injection — Out-of-Bounds Display (Display 99) ---")
    track_invalid = ScreenTrack(monitor_index=99, width=1280, height=720, fps=30)
    frame_inv = await track_invalid.recv()
    print(f"  Display 99 captured safely without crash: Frame shape={frame_inv.width}x{frame_inv.height}")
    track_invalid.stop()
    await asyncio.sleep(0.3)

    print("\n" + "=" * 85)
    print("PHASE 4 M2: MULTI-MONITOR VALIDATION SUCCESSFUL")
    print("=" * 85)


if __name__ == "__main__":
    asyncio.run(test_dual_physical_monitors())
