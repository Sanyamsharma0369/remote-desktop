import logging
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from aiortc import RTCPeerConnection, RTCSessionDescription
from app.services.screen_track import ScreenTrack, ENCODER
from app.services.audio_track import AudioTrack
from app.services.state import pcs
from app.routers.auth import get_current_user
from app.models.user import User

router = APIRouter()
log = logging.getLogger(__name__)

def add_video_bitrate_to_sdp(sdp: str, kbps: int = 4000) -> str:
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

@router.post("/offer")
async def offer(request: Request):
    params = await request.json()
    log.info(f"Received offer: {params.get('type')} (SDP length: {len(params.get('sdp',''))})")
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    
    pc = RTCPeerConnection()
    pcs.add(pc)
    
    @pc.on("iceconnectionstatechange")
    async def on_iceconnectionstatechange():
        if pc.iceConnectionState == "failed" or pc.iceConnectionState == "closed":
            await pc.close()
            pcs.discard(pc)
            log.info("Peer connection closed.")

    fps = int(params.get("fps", 30))
    width = int(params.get("width", 1280))
    height = int(params.get("height", 800))
    audio_enabled = params.get("audio", True)
    
    log.info(f"Setting up ScreenTrack with {width}x{height} @ FPS={fps}")
    pc.addTrack(ScreenTrack(width=width, height=height, fps=fps))
    
    if audio_enabled:
        try:
            audio_track = AudioTrack()
            pc.addTrack(audio_track)
            log.info("Audio track added to peer connection")
        except Exception as e:
            log.error(f"Failed to add audio track: {e}")
            
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    
    sdp = add_video_bitrate_to_sdp(pc.localDescription.sdp, kbps=4000)
    
    return JSONResponse({
        "sdp": sdp,
        "type": pc.localDescription.type
    })

@router.get("/api/stream/info")
async def get_stream_info(current_user: User = Depends(get_current_user)):
    """Provides encoder info for the viewer HUD."""
    encoder_labels = {
        "h264_nvenc": "H.264 (NVIDIA NVENC)",
        "libx264": "H.264 (CPU - libx264)"
    }
    return {
        "encoder": ENCODER,
        "encoder_label": encoder_labels.get(ENCODER, "H.264 (CPU - libx264)")
    }

