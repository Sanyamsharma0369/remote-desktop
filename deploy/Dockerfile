# deploy/Dockerfile — Production image for Remote Desktop FastAPI backend.
#
# IMPORTANT LINUX CONSTRAINTS:
#   This application uses Windows-specific APIs for screen capture (mss),
#   mouse/keyboard injection (pyautogui, ctypes.windll), and audio (pyaudio).
#   These components will NOT function inside a Linux container.
#
#   This Dockerfile produces an image that:
#     - starts the FastAPI/uvicorn application correctly,
#     - serves auth, API, and WebSocket endpoints,
#     - does NOT provide screen capture, input injection, or audio.
#
#   For full remote-desktop functionality, run the application directly on
#   a Windows host using:
#       python server.py
#   or:
#       uvicorn app.main:app --host 0.0.0.0 --port 9005
#
# Build context: repository root (docker-compose.yml uses context: ..)
# Usage:  docker compose up  (from the deploy/ directory)

FROM python:3.11-slim

# System dependencies:
#   - ffmpeg:  required by aiortc / PyAV for WebRTC codec operations
#   - libsm6, libxext6, libgl1: required by opencv-python on headless Linux
#   - build-essential: needed to compile some Python C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsm6 \
        libxext6 \
        libgl1 \
        build-essential \
        portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for the application process
RUN useradd --system --create-home --shell /bin/bash appuser

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/         ./app/
COPY server.py    ./
COPY static/      ./static/
COPY *.html       ./
COPY favicon.ico* manifest.json* ./

# Persistent data volume mount point
RUN mkdir -p /data /app/uploads && chown -R appuser:appuser /data /app

USER appuser

# Health check (uses favicon endpoint — lightweight, no auth required)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9005/favicon.ico')"

EXPOSE 9005

# Use environment variables for configuration (see .env.example)
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "9005", \
     "--workers", "1", \
     "--log-level", "info"]
