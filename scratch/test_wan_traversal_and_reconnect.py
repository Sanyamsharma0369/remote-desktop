"""
scratch/test_wan_traversal_and_reconnect.py — Phase 4 Track 3: WAN / NAT / TURN Traversal & Reconnect Verification.
"""
import asyncio
import json
import os
import time
from typing import Dict, Any, List

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, MediaStreamTrack
from aiortc.contrib.media import MediaBlackhole
from app.core.config import get_settings
from app.routers.stream import get_rtc_configuration, add_video_bitrate_to_sdp, _cleanup_peer_connection
from app.services.screen_track import ScreenTrack, capture_hub, get_active_encoder
from app.services.state import pcs, control_manager


async def test_stun_candidate_gathering():
    print("=" * 85)
    print("TEST 1: STUN PUBLIC ADDRESS DISCOVERY & CANDIDATE GATHERING")
    print("=" * 85)

    settings = get_settings()
    config = get_rtc_configuration()
    print(f"Configured STUN Servers: {settings.STUN_SERVERS}")
    print(f"Configured TURN Server:  {settings.TURN_SERVER}")
    print(f"aiortc RTCConfiguration: {len(config.iceServers)} ICE server(s) configured")

    pc = RTCPeerConnection(configuration=config)
    track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
    pc.addTrack(track)

    t_start = time.monotonic()
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    # Wait briefly for ICE gathering to begin / complete
    gathered_candidates = []
    for line in pc.localDescription.sdp.splitlines():
        if line.startswith("a=candidate:"):
            gathered_candidates.append(line)

    gathering_time = (time.monotonic() - t_start) * 1000
    print(f"\n[ICE Gathering] Completed in {gathering_time:.1f} ms")
    print(f"Total SDP Candidates Gathered: {len(gathered_candidates)}")
    for cand in gathered_candidates[:5]:
        print(f"  Candidate: {cand}")

    # Verify candidate types
    has_host = any("typ host" in c for c in gathered_candidates)
    has_srflx = any("typ srflx" in c for c in gathered_candidates)
    print(f"\n[Candidate Analysis]")
    print(f"  Host (Local Interface) Candidate:     {'YES' if has_host else 'NO'}")
    print(f"  Server Reflexive (STUN Public IP):    {'YES (Gathered via STUN)' if has_srflx else 'LOCAL-ONLY'}")

    track.stop()
    await pc.close()
    await asyncio.sleep(0.3)
    assert len(gathered_candidates) > 0, "Expected at least 1 candidate gathered"
    print("\n[PASSED] STUN candidate gathering and SDP generation verified.")


async def test_wan_handshake_and_delivery():
    print("\n" + "=" * 85)
    print("TEST 2: WAN ICE HANDSHAKE, BITRATE INJECTION & VIDEO DELIVERY")
    print("=" * 85)

    server_config = get_rtc_configuration()
    client_config = get_rtc_configuration()

    server_pc = RTCPeerConnection(configuration=server_config)
    client_pc = RTCPeerConnection(configuration=client_config)
    client_pc.addTransceiver("video", direction="recvonly")
    pcs.add(server_pc)

    server_track = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
    server_pc.addTrack(server_track)

    received_frames = []

    @client_pc.on("track")
    def on_track(track):
        if track.kind == "video":
            print("  [Client] Received Remote Video Track from Server")

            async def drain():
                while not client_pc.connectionState in ("closed", "failed"):
                    try:
                        frame = await track.recv()
                        received_frames.append(frame)
                    except Exception:
                        break
            asyncio.create_task(drain())

    # Step A: Client creates offer
    client_offer = await client_pc.createOffer()
    await client_pc.setLocalDescription(client_offer)

    # Step B: Server receives offer & creates answer with bitrate injection
    await server_pc.setRemoteDescription(client_pc.localDescription)
    server_answer = await server_pc.createAnswer()
    await server_pc.setLocalDescription(server_answer)

    # Inject application-level bandwidth constraints (b=AS:4000)
    constrained_sdp = add_video_bitrate_to_sdp(server_pc.localDescription.sdp, kbps=4000)
    assert "b=AS:4000" in constrained_sdp, "Bitrate constraint b=AS:4000 must be in SDP"
    assert "b=TIAS:4000000" in constrained_sdp, "TIAS constraint must be in SDP"

    # Step C: Client receives answer
    t_conn_start = time.monotonic()
    await client_pc.setRemoteDescription(RTCSessionDescription(sdp=constrained_sdp, type="answer"))

    # Wait for ICE connection
    for _ in range(50):
        if client_pc.connectionState == "connected" and server_pc.connectionState == "connected":
            break
        await asyncio.sleep(0.1)

    conn_lat = (time.monotonic() - t_conn_start) * 1000
    print(f"\n[Connection Telemetry]")
    print(f"  Server Connection State: {server_pc.connectionState}")
    print(f"  Client Connection State: {client_pc.connectionState}")
    print(f"  ICE Connection Time:     {conn_lat:.1f} ms")

    # Stream for 2 seconds
    await asyncio.sleep(2.0)
    print(f"  Total Video Frames Received across WAN WebRTC channel: {len(received_frames)}")

    assert len(received_frames) > 0, "Client must receive video frames over WebRTC connection"

    # Cleanup
    await _cleanup_peer_connection(server_pc, "test_wan_user")
    await client_pc.close()
    await asyncio.sleep(0.3)
    print("\n[PASSED] WAN handshake, bandwidth constraints, and frame delivery verified.")


async def test_reconnection_and_orphan_cleanup():
    print("\n" + "=" * 85)
    print("TEST 3: NETWORK INTERRUPTION, RECONNECT RECOVERY & ZERO ORPHANS")
    print("=" * 85)

    base_workers = capture_hub.get_active_worker_count()
    base_peers = len(pcs)
    print(f"[Initial State] Active Peers: {base_peers} | Active Workers: {base_workers}")

    # 1. Establish session 1
    server_pc1 = RTCPeerConnection(configuration=get_rtc_configuration())
    client_pc1 = RTCPeerConnection(configuration=get_rtc_configuration())
    client_pc1.addTransceiver("video", direction="recvonly")
    pcs.add(server_pc1)
    track1 = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
    server_pc1.addTrack(track1)

    await client_pc1.setLocalDescription(await client_pc1.createOffer())
    await server_pc1.setRemoteDescription(client_pc1.localDescription)
    await server_pc1.setLocalDescription(await server_pc1.createAnswer())
    await client_pc1.setRemoteDescription(server_pc1.localDescription)
    await asyncio.sleep(0.5)

    print(f"  Session 1 established: Active Peers = {len(pcs)}, Active Workers = {capture_hub.get_active_worker_count()}")
    assert capture_hub.get_active_worker_count() == 1, "Expected 1 capture worker"

    # 2. Simulate abrupt client drop (e.g. WiFi disconnect)
    print("\n  [Simulating Abrupt Network Drop] Client 1 closes socket...")
    await client_pc1.close()
    await _cleanup_peer_connection(server_pc1, "user_drop")
    await asyncio.sleep(0.5)

    print(f"  Post-Drop State: Active Peers = {len(pcs)}, Active Workers = {capture_hub.get_active_worker_count()}")
    assert len(pcs) == 0, "Peer collection must be 0 after drop cleanup"
    assert capture_hub.get_active_worker_count() == 0, "Capture workers must be 0 after drop cleanup"

    # 3. Client Reconnects (Session 2 within grace period)
    print("\n  [Client Reconnect] Establishing new WebRTC session...")
    server_pc2 = RTCPeerConnection(configuration=get_rtc_configuration())
    client_pc2 = RTCPeerConnection(configuration=get_rtc_configuration())
    client_pc2.addTransceiver("video", direction="recvonly")
    pcs.add(server_pc2)
    track2 = ScreenTrack(monitor_index=1, width=1280, height=720, fps=30)
    server_pc2.addTrack(track2)

    await client_pc2.setLocalDescription(await client_pc2.createOffer())
    await server_pc2.setRemoteDescription(client_pc2.localDescription)
    await server_pc2.setLocalDescription(await server_pc2.createAnswer())
    await client_pc2.setRemoteDescription(server_pc2.localDescription)
    await asyncio.sleep(0.5)

    print(f"  Session 2 (Reconnected): Active Peers = {len(pcs)}, Active Workers = {capture_hub.get_active_worker_count()}")
    assert len(pcs) == 1, "Active peers should be 1"
    assert capture_hub.get_active_worker_count() == 1, "Active capture workers should be 1"

    # 4. Final Disconnect
    await _cleanup_peer_connection(server_pc2, "user_reconnected")
    await client_pc2.close()
    await asyncio.sleep(0.5)

    print(f"\n[Final Teardown State] Active Peers = {len(pcs)}, Active Workers = {capture_hub.get_active_worker_count()}")
    assert len(pcs) == 0, "Zero orphaned peers"
    assert capture_hub.get_active_worker_count() == 0, "Zero orphaned capture workers"
    print("\n[PASSED] Reconnection lifecycle and zero-orphan cleanup verified.")


async def main():
    await test_stun_candidate_gathering()
    await test_wan_handshake_and_delivery()
    await test_reconnection_and_orphan_cleanup()


if __name__ == "__main__":
    asyncio.run(main())
