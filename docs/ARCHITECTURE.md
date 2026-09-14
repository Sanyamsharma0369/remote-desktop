# System Architecture

This document provides a technical deep-dive into the Remote Desktop platform, detailing components, pipelines, state machines, and data flows.

---

## 1. High-Level Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                           Client Browser / PWA                          │
│                                                                         │
│  ┌──────────────────────┐  ┌───────────────────┐  ┌──────────────────┐  │
│  │   WebRTC Video Tag   │  │ WebSocket Client  │  │  REST API Client │  │
│  └──────────▲───────────┘  └─────────▲─────────┘  └────────▲─────────┘  │
└─────────────┼────────────────────────┼─────────────────────┼────────────┘
              │ WebRTC (RTP Video)     │ WSS (Control JSON)  │ HTTPS (JSON)
              │                        │                     │
┌─────────────┼────────────────────────┼─────────────────────┼────────────┐
│             │                        │                     │            │
│  ┌──────────▼───────────┐  ┌─────────▼─────────┐  ┌────────▼─────────┐  │
│  │    WebRTC Streamer   │  │   Control Server  │  │   FastAPI Core   │  │
│  │  (aiortc / PyAV H264)│  │ (State & Limiter) │  │  (Routers/Auth)  │  │
│  └──────────▲───────────┘  └─────────▲─────────┘  └────────▲─────────┘  │
│             │                        │                     │            │
│  ┌──────────▼───────────┐  ┌─────────▼─────────┐  ┌────────▼─────────┐  │
│  │  Screen Capture (MSS)│  │ Input Injection   │  │ SQLAlchemy Models│  │
│  │  & Frame Pre-scaler  │  │   (PyAutoGUI)     │  │ & SQLite Database│  │
│  └──────────────────────┘  └───────────────────┘  └──────────────────┘  │
│                                                                         │
│                             Host System                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Subsystems

### 2.1 WebRTC Video Pipeline
- **Capture Worker:** A background capture thread continuously captures monitor frames using MSS into shared memory buffers, maintaining low CPU utilization and consistent frame intervals.
- **Dynamic Scaler:** Frames are rescaled and color-converted using OpenCV based on active resolution presets (`720p`, `Balanced`, `High`, `Low`, `Native`, or custom dimensions).
- **Track Streaming:** The custom `aiortc.VideoStreamTrack` packs PyAV `VideoFrame` instances with accurate presentation timestamps (`pts`), which are encoded via software/hardware encoders (e.g. OpenH264 / NVENC) and streamed via RTP to the browser.
- **Signaling:** SDP offers/answers and ICE candidate negotiation occur over a dedicated REST signaling exchange (`/api/stream/offer`).

### 2.2 WebSocket Control Channel
- **Authentication:** WebSockets require a single-use authorization ticket generated via authenticated REST API (`/api/auth/ws-ticket`).
- **Mode State Machine:** The server enforces two distinct session modes:
  - `Screen View`: Default state. All incoming mouse/keyboard injection payloads are strictly rejected by the server.
  - `Screen Control`: Enabled explicitly by authenticated user. Unlocks mouse move, click, drag, scroll, and keyboard events.
- **Rate Limiting & Clamping:** Mouse coordinate inputs are clamped within normalized $[0.0, 1.0]$ bounds and rate-limited to prevent buffer flooding.

### 2.3 Authentication & Session Management
- **Token Architecture:**
  - `Access Token`: 15-minute JWT stored in memory by the web frontend.
  - `Refresh Token`: 7-day opaque cryptographic string stored in a secure `HttpOnly`, `SameSite=Lax/Strict` cookie.
- **Token Rotation & Revocation:** Every refresh cycle rotates the refresh token. The database stores SHA-256 hashes of valid refresh tokens. If a revoked token is presented (indicating potential theft), all active sessions for the user are immediately terminated.

---

## 3. Directory Layout

```text
backend/
├── app/
│   ├── main.py                     # Application entrypoint & middleware configuration
│   ├── core/
│   │   ├── config.py               # Pydantic Settings & environment validation
│   │   ├── database.py             # SQLAlchemy engine & session factory
│   │   ├── security.py             # Password hashing, JWT creation & sanitizers
│   │   └── windows_desktop.py      # Interactive desktop session management
│   ├── models/
│   │   ├── user.py                 # User account & credential definitions
│   │   ├── user_session.py         # Refresh token & session state tracking
│   │   ├── ws_ticket.py            # Ephemeral WebSocket ticket records
│   │   ├── audit.py                # Structured audit trail records
│   │   └── file.py                 # Persistent file metadata records (FileRecord)
│   ├── routers/
│   │   ├── auth.py                 # Login, refresh, register, sessions & ticket endpoints
│   │   ├── stream.py               # WebRTC SDP signaling & stream lifecycle
│   │   ├── control.py              # WebSocket input dispatch & mode management
│   │   ├── files.py                # Bidirectional file upload & download
│   │   ├── monitors.py             # Multi-monitor enumeration & switching
│   │   ├── power.py                # Lock, sleep, restart, shutdown endpoints
│   │   └── audit.py                # Admin audit log query endpoints
│   └── services/
│       ├── screen_track.py         # Threaded screen capture engine (ScreenTrack)
│       ├── input_service.py        # PyAutoGUI/Win32 input execution (InputService)
│       ├── audio_track.py          # aiortc AudioStreamTrack (system audio capture)
│       └── audit.py                # Centralized audit event logging service
├── deploy/
│   ├── Dockerfile                  # Production container image definition
│   ├── Caddyfile                   # Production reverse proxy template
│   ├── docker-compose.yml          # Container orchestration template
│   └── turnserver.conf.example     # Coturn STUN/TURN configuration
├── docs/                           # Architecture, security & deployment guides
├── static/                         # PWA icons, assets, and screenshots
└── tests/                          # 42 automated security and regression tests
```
