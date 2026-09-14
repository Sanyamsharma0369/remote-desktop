from fastapi import FastAPI, Response, Query, WebSocket, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
import mss
import io
from PIL import Image
import asyncio
from urllib.parse import parse_qs
# --- User management imports ---
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
import os
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt
from typing import Optional
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
oauth2_scheme = HTTPBearer()

# --- WebRTC Dependencies ---
import cv2
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack
from av import VideoFrame, AudioFrame
import mss
import pyaudio
import queue
import threading
import logging
import pyautogui
import pyperclip
import json
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
import secrets
from fastapi.responses import RedirectResponse
from fastapi.responses import FileResponse
import subprocess

# Screen capture track
class ScreenTrack(VideoStreamTrack):
    def __init__(self, fps=15):
        super().__init__()
        self.fps = fps
        self.frame_time = 1 / max(1, fps)
        self.frame_count = 0
        self._monitor_index = 1
        self.sct = mss.mss()
        try:
            self.monitor = self.sct.monitors[1]
        except (IndexError, KeyError):
            self.monitor = self.sct.monitors[0]
            self._monitor_index = 0
        self.width = self.monitor['width']
        self.height = self.monitor['height']
        logging.info(f"ScreenTrack initialized with resolution: {self.width}x{self.height}")

    def set_monitor(self, index: int):
        monitors = self.sct.monitors
        if 0 <= index < len(monitors):
            self._monitor_index = index
            self.monitor = monitors[index]
            self.width = self.monitor['width']
            self.height = self.monitor['height']
            logging.info(f"ScreenTrack monitor set to index {index} ({self.width}x{self.height})")

    async def recv(self):
        await asyncio.sleep(self.frame_time)
        try:
            sct_img = self.sct.grab(self.monitor)
            h, w = sct_img.height, sct_img.width
            h_even, w_even = h - (h % 2), w - (w % 2)
            img = np.array(sct_img)[:h_even, :w_even]
            frame = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
            cv2.putText(
                frame,
                f"Display {self._monitor_index} ({w_even}x{h_even}) | Frame: {self.frame_count}",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
                cv2.LINE_AA
            )
        except Exception as e:
            logging.error(f"Screen capture failed: {e}. Sending test color frame.")
            color = [(255,0,0), (0,255,0), (0,0,255)][self.frame_count % 3]
            h, w = getattr(self, 'height', 1080), getattr(self, 'width', 1920)
            h_even, w_even = h - (h % 2), w - (w % 2)
            frame = np.full((h_even, w_even, 3), color, dtype=np.uint8)
            cv2.putText(
                frame,
                f"CAPTURE ERROR: {e}",
                (20, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2,
                cv2.LINE_AA
            )
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        ts = await self.next_timestamp()
        video_frame.pts, video_frame.time_base = ts
        self.frame_count += 1
        return video_frame

# Audio capture track
class AudioTrack(AudioStreamTrack):
    def __init__(self, sample_rate=48000, channels=2):
        super().__init__()
        self.sample_rate = sample_rate
        self.channels = channels
        self.samples_per_frame = int(sample_rate * 0.02)  # 20ms frames
        self.frame_count = 0
        try:
            self.audio = pyaudio.PyAudio()
            self.stream = None
            self.audio_queue = queue.Queue(maxsize=100)
            self.is_capturing = False
            self.audio_available = True
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
                    audio_data = self.audio_queue.get(timeout=1)
                    if audio_data is None:
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
        if self.is_capturing and self.audio_available:
            try:
                audio_array = np.frombuffer(in_data, dtype=np.float32)
                self.audio_queue.put(audio_array)
            except Exception as e:
                logging.error(f"Audio callback error: {e}")
        return (None, pyaudio.paContinue)

    async def recv(self):
        try:
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
            audio_data = self.audio_queue.get_nowait()
            audio_int16 = (audio_data * 32767).astype(np.int16)
            audio_frame = AudioFrame.from_ndarray(
                audio_int16.reshape(-1, self.channels),
                format='s16',
                layout='stereo'
            )
            ts = await self.next_timestamp()
            audio_frame.pts, audio_frame.time_base = ts
            audio_frame.sample_rate = self.sample_rate
            self.frame_count += 1
            return audio_frame
        except queue.Empty:
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

def set_video_bitrate(sdp, bitrate=5000):
    lines = sdp.split('\n')
    for i, line in enumerate(lines):
        if line.startswith('m=video'):
            lines.insert(i+1, f'b=AS:{bitrate}')
            break
    return '\n'.join(lines)

from fastapi import Request
from fastapi.responses import JSONResponse
from aiortc import RTCPeerConnection, RTCSessionDescription

app = FastAPI()

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(16))

# --- JWT Configuration ---
SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# --- Pydantic Models ---
class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

class UserLogin(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    username: str
    role: str

class UserResponse(BaseModel):
    id: int
    username: str
    role: str

# --- Database setup ---
DATABASE_URL = "sqlite:///./users.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")  # 'admin' or 'user'

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_admin_if_not_exists():
    db = SessionLocal()
    if not db.query(User).filter(User.role == "admin").first():
        admin = User(
            username="admin",
            password_hash=pwd_context.hash("admin123"),
            role="admin"
        )
        db.add(admin)
        db.commit()
    db.close()

if not os.path.exists("./users.db"):
    Base.metadata.create_all(bind=engine)
    create_admin_if_not_exists()

# --- JWT Functions ---
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def authenticate_user(db: Session, username: str, password: str):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return False
    if not verify_password(password, user.password_hash):
        return False
    return user

def get_current_user(token: HTTPAuthorizationCredentials = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user

def require_admin(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

# --- Authentication Endpoints ---
@app.post("/login", response_model=Token)
def login(user_credentials: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_credentials.username, user_credentials.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "username": user.username,
        "role": user.role
    }

@app.post("/register", response_model=UserResponse)
def register_user(user_data: UserCreate, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.username == user_data.username).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )
    hashed_password = get_password_hash(user_data.password)
    db_user = User(
        username=user_data.username,
        password_hash=hashed_password,
        role=user_data.role
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return UserResponse(id=db_user.id, username=db_user.username, role=db_user.role)

@app.get("/users", response_model=list[UserResponse])
def get_users(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [UserResponse(id=user.id, username=user.username, role=user.role) for user in users]

@app.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    return UserResponse(id=current_user.id, username=current_user.username, role=current_user.role)

@app.get("/screen", response_class=Response)
def get_screen(scale: float = Query(1.0, ge=0.1, le=1.0)):
    """
    Capture the screen and return as JPEG. Optional scale parameter to resize image.
    """
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # Primary monitor
        sct_img = sct.grab(monitor)
        img = Image.frombytes('RGB', sct_img.size, sct_img.rgb)
        if scale != 1.0:
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        jpeg_bytes = buf.getvalue()
    return Response(content=jpeg_bytes, media_type="image/jpeg")

@app.websocket("/ws/screen")
async def websocket_screen(websocket: WebSocket):
    # Parse query parameters for scale, quality, and fps
    query = parse_qs(websocket.url.query)
    try:
        scale = float(query.get('scale', [1.0])[0])
        quality = int(query.get('quality', [80])[0])
        quality = max(10, min(quality, 95))  # Clamp between 10 and 95
        fps = float(query.get('fps', [15])[0])
        fps = max(1, min(fps, 60))  # Clamp between 1 and 60
        frame_delay = 1.0 / fps
    except Exception:
        scale = 1.0
        quality = 80
        frame_delay = 1.0 / 15
    await websocket.accept()
    try:
        while True:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                sct_img = sct.grab(monitor)
                img = Image.frombytes('RGB', sct_img.size, sct_img.rgb)
                if scale != 1.0:
                    new_size = (int(img.width * scale), int(img.height * scale))
                    img = img.resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                jpeg_bytes = buf.getvalue()
            await websocket.send_bytes(jpeg_bytes)
            await asyncio.sleep(frame_delay)
    except Exception:
        await websocket.close() 

@app.post("/offer")
async def offer(request: Request):
    params = await request.json()
    logging.info(f"Received offer: {params.get('type')} (SDP length: {len(params.get('sdp',''))})")
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    pc = RTCPeerConnection()
    pcs.add(pc)
    fps = int(params.get("fps", 15))
    audio_enabled = params.get("audio", True)
    logging.info(f"Setting up ScreenTrack with FPS={fps}")
    pc.addTrack(ScreenTrack(fps=fps))
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
    sdp = set_video_bitrate(pc.localDescription.sdp, bitrate=5000)
    return JSONResponse({
        "sdp": sdp,
        "type": pc.localDescription.type
    }) 

@app.websocket("/ws/control")
async def websocket_control(websocket: WebSocket):
    await websocket.accept()
    # Send screen resolution on connect
    try:
        sct = mss.mss()
        monitor = sct.monitors[1]
        await websocket.send_json({"action": "screeninfo", "width": monitor['width'], "height": monitor['height']})
    except Exception as e:
        await websocket.send_json({"action": "screeninfo", "width": 1920, "height": 1080})
    
    try:
        while True:
            data = await websocket.receive_text()
            try:
                data = json.loads(data)
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
                    await websocket.send_json({"action": "clipboard_data", "text": text})
                elif action == 'set_fps':
                    new_fps = int(data.get('fps', 15))
                    # Find the current peer connection and update its ScreenTrack FPS
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
                elif action == 'ping':
                    # Keepalive ping - no action needed
                    pass
                    
            except Exception as e:
                logging.error(f"Control event error: {e}")
    except Exception as e:
        logging.error(f"WebSocket control error: {e}")
    finally:
        await websocket.close() 

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    try:
        with open("dashboard.html", "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse(content="Dashboard file not found", status_code=404)

@app.get("/viewer", response_class=HTMLResponse)
def get_viewer():
    try:
        with open("webrtc_viewer.html", "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse(content="Viewer file not found", status_code=404)

@app.get("/login", response_class=HTMLResponse)
def get_login():
    try:
        with open("login.html", "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse(content="Login file not found", status_code=404)

# NOTE: Duplicate POST /login and POST /register routes were removed.
# The proper JWT-based versions above (lines ~348 and ~368) handle authentication.

@app.post("/power/lock")
async def power_lock(request: Request):
    try:
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/power/shutdown")
async def power_shutdown(request: Request):
    try:
        subprocess.Popen(["shutdown", "/s", "/t", "0"])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/power/restart")
async def power_restart(request: Request):
    try:
        subprocess.Popen(["shutdown", "/r", "/t", "0"])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/power/hibernate")
async def power_hibernate(request: Request):
    try:
        subprocess.Popen(["shutdown", "/h"])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/power/sleep")
async def power_sleep(request: Request):
    try:
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "Sleep", "0", "0"])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

# Mount static files at /static to avoid shadowing API routes
app.mount("/static", StaticFiles(directory=os.path.dirname(__file__), html=True), name="static") 

@app.get("/favicon.ico")
def favicon():
    return FileResponse("favicon.ico")

@app.get("/", response_class=HTMLResponse)
def root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse(content="Index file not found", status_code=404)

@app.get("/api/my_device")
def get_my_device():
    import platform, socket
    return {
        "name": "This Device",
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "status": "online"
    }

@app.get("/register", response_class=HTMLResponse)
def get_register():
    try:
        with open("register.html", "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse(content="Register file not found", status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=9005, reload=True) 