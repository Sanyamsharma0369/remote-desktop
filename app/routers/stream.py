import logging
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from aiortc import RTCPeerConnection, RTCSessionDescription
from app.services.screen_track import ScreenTrack
from app.services.audio_track import AudioTrack
from app.services.encoder import get_active_encoder, get_active_encoder_label
from app.services.state import pcs
from app.routers.auth import get_current_user
from app.models.user import User

router = APIRouter()
log = logging.getLogger(__name__)


def add_video_bitrate_to_sdp(sdp: str, kbps: int = 4000) -> str:
    """Inject application-level bandwidth constraints into SDP offer/answer."""
    lines = sdp.splitlines()
    output = []
    inserted = False
    for line in lines:
        output.append(line)
        if not inserted and line.startswith("m=video"):
            output.append(f"b=AS:{kbps}")
            output.append(f"b=TIAS:{kbps * 1000}")
            inserted = True
    return "\r\n".join(output) + "\r\n"


async def _cleanup_peer_connection(pc: RTCPeerConnection, username: str = "unknown"):
    """Safely stop all tracks and close peer connection without resource leaks."""
    try:
        pcs.discard(pc)
        for sender in list(pc.getSenders()):
            if sender.track and hasattr(sender.track, "stop"):
                try:
                    sender.track.stop()
                except Exception as e:
                    log.warning("Error stopping track on cleanup: %s", e)
        await pc.close()
        log.info("WebRTC connection cleaned up for user '%s' (active peers: %d)", username, len(pcs))
    except Exception as exc:
        log.warning("Error during peer connection cleanup: %s", exc)


@router.post("/offer")
async def offer(request: Request, current_user: User = Depends(get_current_user)):
    params = await request.json()
    log.info(
        "Received WebRTC offer: %s (SDP length: %d) from user '%s'",
        params.get("type"), len(params.get("sdp", "")), current_user.username,
    )
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    
    pc = RTCPeerConnection()
    pcs.add(pc)
    
    @pc.on("iceconnectionstatechange")
    async def on_iceconnectionstatechange():
        log.info(
            "WebRTC ICE state -> %s for user '%s' (active peers: %d)",
            pc.iceConnectionState, current_user.username, len(pcs),
        )
        if pc.iceConnectionState in ("failed", "closed"):
            await _cleanup_peer_connection(pc, current_user.username)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        log.info(
            "WebRTC connection state -> %s for user '%s' (active peers: %d)",
            pc.connectionState, current_user.username, len(pcs),
        )
        if pc.connectionState in ("failed", "closed"):
            await _cleanup_peer_connection(pc, current_user.username)

    fps = int(params.get("fps", 30))
    width = int(params.get("width", 1280))
    height = int(params.get("height", 800))
    audio_enabled = params.get("audio", True)
    bitrate_kbps = int(params.get("bitrate_kbps", 4000))
    bitrate_kbps = max(500, min(bitrate_kbps, 15000))
    
    log.info("Setting up ScreenTrack with %dx%d @ FPS=%d for '%s'", width, height, fps, current_user.username)
    screen_track = ScreenTrack(width=width, height=height, fps=fps)
    pc.addTrack(screen_track)
    
    if audio_enabled:
        try:
            audio_track = AudioTrack()
            pc.addTrack(audio_track)
            log.info("Audio track added to peer connection for '%s'", current_user.username)
        except Exception as e:
            log.error("Failed to add audio track: %s", e)
            
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    
    sdp = add_video_bitrate_to_sdp(pc.localDescription.sdp, kbps=bitrate_kbps)
    
    return JSONResponse({
        "sdp": sdp,
        "type": pc.localDescription.type
    })


@router.get("/info")
async def get_stream_info(current_user: User = Depends(get_current_user)):
    """Provides encoder info and active stream telemetry for the viewer HUD."""
    encoder = get_active_encoder()
    return {
        "encoder": encoder,
        "encoder_label": get_active_encoder_label(),
        "active_peers": len(pcs),
    }


@router.get("/stats")
async def get_stream_stats(current_user: User = Depends(get_current_user)):
    """Returns active tracks metrics and encoder telemetry."""
    track_stats = []
    for pc in list(pcs):
        for sender in pc.getSenders():
            if isinstance(sender.track, ScreenTrack):
                track_stats.append(sender.track.get_stats())

    return {
        "active_peers": len(pcs),
        "tracks": track_stats,
        "encoder": get_active_encoder(),
    }
