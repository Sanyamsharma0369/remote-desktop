import cv2
import numpy as np
import mss
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack
from aiortc.contrib.media import MediaBlackhole
from av import VideoFrame, AudioFrame
import asyncio
from aiohttp import web
import aiohttp_cors
import logging
import pyautogui
from aiohttp import WSMsgType
import pyperclip
import os
import uuid
import aiofiles
from pathlib import Path
import pyaudio
import wave
import threading
import queue
import subprocess

# Screen capture track
class ScreenTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, monitor_index: int = 1, width: int = 1280, height: int = 720, fps: int = 30):
        super().__init__()
        self.monitor_index = monitor_index
        self.target_width = width
        self.target_height = height
        self.fps = fps
        self.frame_count = 0
        self.sct = mss.mss()
        logging.info(f"ScreenTrack initialized with target resolution: {width}x{height} @ {fps} FPS")

    def set_monitor(self, index: int):
        monitors = self.sct.monitors
        if 1 <= index < len(monitors):
            self.monitor_index = index
            logging.info(f"ScreenTrack monitor set to index {index}")

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        try:
            monitors = self.sct.monitors
            index = max(1, min(self.monitor_index, len(monitors) - 1))
            monitor = monitors[index]

            sct_img = self.sct.grab(monitor)
            image = np.asarray(sct_img)[:, :, :3]  # BGRA -> BGR
            image = np.ascontiguousarray(image)

            source_height, source_width = image.shape[:2]
            scale = min(self.target_width / source_width, self.target_height / source_height, 1.0)
            resized_w = int(source_width * scale)
            resized_h = int(source_height * scale)
            resized_w -= resized_w % 2
            resized_h -= resized_h % 2
            resized_w = max(2, resized_w)
            resized_h = max(2, resized_h)

            if resized_w != source_width or resized_h != source_height:
                image = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_AREA)
                image = np.ascontiguousarray(image)

            video_frame = VideoFrame.from_ndarray(image, format="bgr24")
            video_frame.pts = pts
            video_frame.time_base = time_base
            self.frame_count += 1
            return video_frame
        except Exception as e:
            logging.error(f"Screen capture failed: {e}")
            raise

# Audio capture track
class AudioTrack(AudioStreamTrack):
    def __init__(self, sample_rate=48000, channels=2):
        super().__init__()
        self.sample_rate = sample_rate
        self.channels = channels
        self.samples_per_frame = int(sample_rate * 0.02)  # 20ms frames
        self.frame_count = 0
        
        # Initialize PyAudio with error handling
        try:
            self.audio = pyaudio.PyAudio()
            self.stream = None
            self.audio_queue = queue.Queue(maxsize=100)
            self.is_capturing = False
            self.audio_available = True
            
            # Start audio capture in a separate thread
            self.capture_thread = threading.Thread(target=self._capture_audio, daemon=True)
            self.capture_thread.start()
            
            logging.info(f"AudioTrack initialized with {sample_rate}Hz, {channels} channels")
        except Exception as e:
            logging.error(f"Failed to initialize audio: {e}")
            self.audio_available = False
            self.audio = None
            self.stream = None
            self.audio_queue = None
            self.is_capturing = False

    def _capture_audio(self):
        """Capture audio from system in a separate thread"""
        if not self.audio_available:
            logging.warning("Audio not available, running in silence mode")
            return
            
        try:
            self.stream = self.audio.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.samples_per_frame,
                stream_callback=self._audio_callback
            )
            self.is_capturing = True
            self.stream.start_stream()
            
            while self.is_capturing:
                try:
                    # Keep the thread alive
                    audio_data = self.audio_queue.get(timeout=1)
                    if audio_data is None:  # Stop signal
                        break
                except queue.Empty:
                    continue
                    
        except Exception as e:
            logging.error(f"Audio capture error: {e}")
            self.audio_available = False
        finally:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except:
                    pass
            if self.audio:
                try:
                    self.audio.terminate()
                except:
                    pass

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Callback for audio capture"""
        if self.is_capturing and self.audio_available:
            try:
                # Convert to numpy array
                audio_array = np.frombuffer(in_data, dtype=np.float32)
                self.audio_queue.put(audio_array)
            except Exception as e:
                logging.error(f"Audio callback error: {e}")
        return (None, pyaudio.paContinue)

    async def recv(self):
        """Return audio frame for WebRTC"""
        try:
            # If audio is not available, return silence
            if not self.audio_available or not self.audio_queue:
                silence = np.zeros(self.samples_per_frame * self.channels, dtype=np.int16)
                audio_frame = AudioFrame.from_ndarray(
                    silence.reshape(-1, self.channels),
                    format='s16',
                    layout='stereo'
                )
                ts = await self.next_timestamp()
                audio_frame.pts, audio_frame.time_base = ts
                audio_frame.sample_rate = self.sample_rate
                return audio_frame
            
            # Get audio data from queue
            audio_data = self.audio_queue.get_nowait()
            
            # Convert to 16-bit PCM for WebRTC
            audio_int16 = (audio_data * 32767).astype(np.int16)
            
            # Create audio frame
            audio_frame = AudioFrame.from_ndarray(
                audio_int16.reshape(-1, self.channels),
                format='s16',
                layout='stereo'
            )
            
            # Set timestamp
            ts = await self.next_timestamp()
            audio_frame.pts, audio_frame.time_base = ts
            audio_frame.sample_rate = self.sample_rate
            
            self.frame_count += 1
            return audio_frame
            
        except queue.Empty:
            # No audio data available, return silence
            silence = np.zeros(self.samples_per_frame * self.channels, dtype=np.int16)
            audio_frame = AudioFrame.from_ndarray(
                silence.reshape(-1, self.channels),
                format='s16',
                layout='stereo'
            )
            ts = await self.next_timestamp()
            audio_frame.pts, audio_frame.time_base = ts
            audio_frame.sample_rate = self.sample_rate
            return audio_frame
        except Exception as e:
            logging.error(f"Audio recv error: {e}")
            # Return silence on error
            silence = np.zeros(self.samples_per_frame * self.channels, dtype=np.int16)
            audio_frame = AudioFrame.from_ndarray(
                silence.reshape(-1, self.channels),
                format='s16',
                layout='stereo'
            )
            ts = await self.next_timestamp()
            audio_frame.pts, audio_frame.time_base = ts
            audio_frame.sample_rate = self.sample_rate
            return audio_frame

    def stop(self):
        """Stop audio capture"""
        self.is_capturing = False
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except:
                pass
        if self.audio:
            try:
                self.audio.terminate()
            except:
                pass

pcs = set()

# File transfer storage
UPLOAD_DIR = Path("uploads")
DOWNLOAD_DIR = Path("downloads")
UPLOAD_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

# File transfer tracking
file_transfers = {}

def set_video_bitrate(sdp, bitrate=5000):
    # bitrate in kbps
    lines = sdp.split('\n')
    for i, line in enumerate(lines):
        if line.startswith('m=video'):
            lines.insert(i+1, f'b=AS:{bitrate}')
            break
    return '\n'.join(lines)

async def offer(request):
    params = await request.json()
    logging.info(f"Received offer: {params.get('type')} (SDP length: {len(params.get('sdp',''))})")
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    pc = RTCPeerConnection()
    pcs.add(pc)
    fps = int(params.get("fps", 15))
    audio_enabled = params.get("audio", True)
    logging.info(f"Setting up ScreenTrack with FPS={fps}")
    pc.addTrack(ScreenTrack(fps=fps))
    
    # Add audio track if enabled
    if audio_enabled:
        try:
            audio_track = AudioTrack()
            pc.addTrack(audio_track)
            logging.info("Audio track added to peer connection")
        except Exception as e:
            logging.error(f"Failed to add audio track: {e}")
    
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    logging.info("Sent answer to client.")
    # SDP munging for higher video bitrate
    sdp = set_video_bitrate(pc.localDescription.sdp, bitrate=5000)
    return web.json_response({
        "sdp": sdp,
        "type": pc.localDescription.type
    })

async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()

async def control_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    # Send screen resolution on connect
    try:
        sct = mss.mss()
        monitor = sct.monitors[1]
        await ws.send_json({"action": "screeninfo", "width": monitor['width'], "height": monitor['height']})
    except Exception as e:
        await ws.send_json({"action": "screeninfo", "width": 1920, "height": 1080})
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = msg.json()
                action = data.get('action')
                if action == 'mousemove':
                    x, y = data['x'], data['y']
                    pyautogui.moveTo(x, y)
                elif action == 'mousedown':
                    button = data.get('button', 'left')
                    pyautogui.mouseDown(button=button)
                elif action == 'mouseup':
                    button = data.get('button', 'left')
                    pyautogui.mouseUp(button=button)
                elif action == 'click':
                    button = data.get('button', 'left')
                    pyautogui.click(button=button)
                elif action == 'scroll':
                    pyautogui.scroll(data.get('amount', 0))
                elif action == 'keydown':
                    key = data['key']
                    pyautogui.keyDown(key)
                elif action == 'keyup':
                    key = data['key']
                    pyautogui.keyUp(key)
                elif action == 'clipboard_set':
                    pyperclip.copy(data.get('text', ''))
                elif action == 'clipboard_get':
                    text = pyperclip.paste()
                    await ws.send_json({"action": "clipboard_data", "text": text})
                elif action == 'set_fps':
                    new_fps = int(data.get('fps', 15))
                    # Find the current peer connection and update its ScreenTrack FPS
                    # This is a simplified approach; in production, track ScreenTrack per session
                    for pc in pcs:
                        for sender in pc.getSenders():
                            track = sender.track
                            if isinstance(track, ScreenTrack):
                                track.fps = new_fps
                                track.frame_time = 1 / new_fps
                                logging.info(f"Updated ScreenTrack FPS to {new_fps}")
                elif action == 'set_monitor':
                    idx = int(data.get('index', 1))
                    for pc in pcs:
                        for sender in pc.getSenders():
                            track = sender.track
                            if isinstance(track, ScreenTrack):
                                track.set_monitor(idx)
                                logging.info(f"Updated ScreenTrack to monitor {idx}")
                elif action == 'set_audio':
                    audio_enabled = data.get('enabled', True)
                    logging.info(f"Audio setting updated: {audio_enabled}")
                    # Note: Audio track management would be more complex in a production system
                    # For now, we just log the preference
            except Exception as e:
                logging.error(f"Control event error: {e}")
        elif msg.type == WSMsgType.ERROR:
            logging.error(f'WebSocket connection closed with exception {ws.exception()}')
    return ws

# File transfer endpoints
async def upload_file(request):
    """Handle file upload from client to server"""
    try:
        reader = await request.multipart()
        field = await reader.next()
        
        if not field:
            return web.json_response({"error": "No file provided"}, status=400)
        
        filename = field.filename
        if not filename:
            return web.json_response({"error": "No filename provided"}, status=400)
        
        # Generate unique filename
        file_id = str(uuid.uuid4())
        file_ext = Path(filename).suffix
        safe_filename = f"{file_id}{file_ext}"
        file_path = UPLOAD_DIR / safe_filename
        
        # Save file
        with open(file_path, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        
        file_transfers[file_id] = {
            "original_name": filename,
            "path": str(file_path),
            "size": file_path.stat().st_size,
            "uploaded_at": asyncio.get_event_loop().time()
        }
        
        logging.info(f"File uploaded: {filename} -> {file_id}")
        return web.json_response({
            "file_id": file_id,
            "filename": filename,
            "size": file_path.stat().st_size
        })
        
    except Exception as e:
        logging.error(f"Upload error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def download_file(request):
    """Handle file download from server to client"""
    try:
        file_id = request.match_info['file_id']
        
        if file_id not in file_transfers:
            return web.json_response({"error": "File not found"}, status=404)
        
        file_info = file_transfers[file_id]
        file_path = Path(file_info["path"])
        
        if not file_path.exists():
            return web.json_response({"error": "File not found on disk"}, status=404)
        
        response = web.FileResponse(
            path=file_path,
            filename=file_info["original_name"]
        )
        return response
        
    except Exception as e:
        logging.error(f"Download error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def list_files(request):
    """List available files for download"""
    try:
        files = []
        for file_id, file_info in file_transfers.items():
            files.append({
                "file_id": file_id,
                "filename": file_info["original_name"],
                "size": file_info["size"],
                "uploaded_at": file_info["uploaded_at"]
            })
        
        return web.json_response({"files": files})
        
    except Exception as e:
        logging.error(f"List files error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def delete_file(request):
    """Delete a file"""
    try:
        file_id = request.match_info['file_id']
        
        if file_id not in file_transfers:
            return web.json_response({"error": "File not found"}, status=404)
        
        file_info = file_transfers[file_id]
        file_path = Path(file_info["path"])
        
        if file_path.exists():
            file_path.unlink()
        
        del file_transfers[file_id]
        
        return web.json_response({"message": "File deleted successfully"})
        
    except Exception as e:
        logging.error(f"Delete file error: {e}")
        return web.json_response({"error": str(e)}, status=500)

# Power control endpoints
async def power_shutdown(request):
    try:
        subprocess.Popen(["shutdown", "/s", "/t", "0"])
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})

async def power_restart(request):
    try:
        subprocess.Popen(["shutdown", "/r", "/t", "0"])
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})

async def power_hibernate(request):
    try:
        subprocess.Popen(["shutdown", "/h"])
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})

async def power_sleep(request):
    try:
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})

async def power_lock(request):
    try:
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})

async def root_handler(request):
    """Handle root path requests"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>WebRTC Remote Desktop Server</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background: #f0f0f0; }
            .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .endpoint { background: #f5f5f5; padding: 10px; margin: 10px 0; border-radius: 5px; }
            .method { font-weight: bold; color: #0066cc; }
            .app-link { display: inline-block; background: #0066cc; color: white; padding: 15px 25px; margin: 10px; text-decoration: none; border-radius: 5px; font-weight: bold; }
            .app-link:hover { background: #0052a3; }
            .section { margin: 30px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🖥️ WebRTC Remote Desktop Server</h1>
            <p>Server is running successfully on port 9003</p>
            
            <div class="section">
                <h2>🚀 Remote Desktop Applications</h2>
                <a href="/viewer" class="app-link">�� WebRTC Viewer</a>
            </div>
            
            <div class="section">
                <h2>🔧 API Endpoints:</h2>
                <div class="endpoint">
                    <span class="method">POST</span> /offer - WebRTC offer endpoint
                </div>
                <div class="endpoint">
                    <span class="method">POST</span> /upload - File upload
                </div>
                <div class="endpoint">
                    <span class="method">GET</span> /files - List uploaded files
                </div>
                <div class="endpoint">
                    <span class="method">GET</span> /download/{file_id} - Download file
                </div>
                <div class="endpoint">
                    <span class="method">DELETE</span> /files/{file_id} - Delete file
                </div>
                <div class="endpoint">
                    <span class="method">POST</span> /power/shutdown - Shutdown system
                </div>
                <div class="endpoint">
                    <span class="method">POST</span> /power/restart - Restart system
                </div>
                <div class="endpoint">
                    <span class="method">POST</span> /power/hibernate - Hibernate system
                </div>
                <div class="endpoint">
                    <span class="method">POST</span> /power/sleep - Sleep system
                </div>
                <div class="endpoint">
                    <span class="method">POST</span> /power/lock - Lock workstation
                </div>
                <div class="endpoint">
                    <span class="method">WS</span> /ws/control - WebSocket control
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

async def favicon_handler(request):
    """Handle favicon requests"""
    # Return a simple 1x1 transparent PNG
    favicon_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x00\x00\x02\x00\x01\xe5\x27\xde\xfc\x00\x00\x00\x00IEND\xaeB`\x82'
    return web.Response(body=favicon_data, content_type='image/png')

async def viewer_handler(request):
    """Serve the WebRTC viewer interface"""
    try:
        with open('webrtc_viewer.html', 'r', encoding='utf-8') as f:
            content = f.read()
        return web.Response(text=content, content_type='text/html')
    except FileNotFoundError:
        return web.Response(text="Viewer file not found", status=404)

async def dashboard_handler(request):
    """Serve the dashboard interface"""
    try:
        with open('dashboard.html', 'r', encoding='utf-8') as f:
            content = f.read()
        return web.Response(text=content, content_type='text/html')
    except FileNotFoundError:
        return web.Response(text="Dashboard file not found", status=404)

async def login_handler(request):
    """Serve the login interface"""
    try:
        with open('login.html', 'r', encoding='utf-8') as f:
            content = f.read()
        return web.Response(text=content, content_type='text/html')
    except FileNotFoundError:
        return web.Response(text="Login file not found", status=404)

async def static_file_handler(request):
    """Serve static files (CSS, JS)"""
    filename = request.match_info['filename']
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Determine content type based on file extension
        if filename.endswith('.css'):
            content_type = 'text/css'
        elif filename.endswith('.js'):
            content_type = 'application/javascript'
        else:
            content_type = 'text/plain'
            
        return web.Response(text=content, content_type=content_type)
    except FileNotFoundError:
        return web.Response(text=f"File {filename} not found", status=404)

app = web.Application()
app.router.add_get("/", root_handler)
app.router.add_get("/favicon.ico", favicon_handler)
app.router.add_get("/viewer", viewer_handler)
app.router.add_get("/{filename:.*\\.(css|js)$}", static_file_handler)
app.router.add_post("/offer", offer)
app.router.add_post("/upload", upload_file)
app.router.add_get("/download/{file_id}", download_file)
app.router.add_get("/files", list_files)
app.router.add_delete("/files/{file_id}", delete_file)
app.router.add_post("/power/shutdown", power_shutdown)
app.router.add_post("/power/restart", power_restart)
app.router.add_post("/power/hibernate", power_hibernate)
app.router.add_post("/power/sleep", power_sleep)
app.router.add_post("/power/lock", power_lock)
app.on_shutdown.append(on_shutdown)
app.router.add_route('GET', '/ws/control', control_ws)

# Enable CORS for all routes (must be after all routes are registered)
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
    )
})
for route in list(app.router.routes()):
    cors.add(route)

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    web.run_app(app, host='0.0.0.0', port=9005) 