import os
import secrets
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.database import SessionLocal, Base, engine, get_db
from app.models.user import User
# Import all models so Base.metadata knows about every table
from app.models import UserSession, WsTicket, AuditEvent  # noqa: F401
from app.routers import auth, stream, control, files, monitors, power, audit as audit_router

from app.core.windows_desktop import attach_interactive_desktop
attach_interactive_desktop()

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Database bootstrap ───────────────────────────────────────────────────────
# create_all is additive — existing tables are never dropped
Base.metadata.create_all(bind=engine)

db = SessionLocal()
try:
    from app.routers.auth import get_password_hash
    if not db.query(User).filter(User.role == "admin").first():
        admin = User(
            username="admin",
            password_hash=get_password_hash("admin123"),
            role="admin",
        )
        db.add(admin)
        db.commit()
        log.info("Default admin user created. CHANGE THE PASSWORD.")
finally:
    db.close()

# ── Security Headers Middleware ──────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        # CSP: same-origin scripts/styles, WebSocket (ws/wss), WebRTC
        csp_ws = "wss:" if settings.is_production else "ws: wss:"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; "
            f"script-src 'self' 'unsafe-inline'; "
            f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            f"font-src 'self' https://fonts.gstatic.com; "
            f"connect-src 'self' {csp_ws}; "
            f"img-src 'self' data:; "
            f"media-src 'self' blob:; "
            f"frame-ancestors 'none';"
        )
        return response


# ── App factory ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="Remote Desktop API",
    version="2.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url=None,
)

# Rate limiter
from app.routers.auth import limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Security headers (before CORS so headers are always present)
app.add_middleware(SecurityHeadersMiddleware)

# CORS — uses parsed list from config
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(16))

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(auth.router,          prefix="/api/auth",         tags=["Auth"])
app.include_router(stream.router,        prefix="",                  tags=["Stream"])
app.include_router(control.router,       prefix="",                  tags=["Control"])
app.include_router(files.router,         prefix="/api/files",        tags=["Files"])
app.include_router(monitors.router,      prefix="/api/monitors",     tags=["Monitors"])
app.include_router(power.router,         prefix="/api/power",        tags=["Power"])
app.include_router(audit_router.router,  prefix="/api/audit-events", tags=["Audit"])

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Frontend routes (HTML file serving) ─────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def root():
    return FileResponse("index.html")

@app.get("/login", response_class=HTMLResponse)
@app.get("/login.html", response_class=HTMLResponse)
async def get_login():
    return FileResponse("login.html")

@app.get("/register", response_class=HTMLResponse)
@app.get("/register.html", response_class=HTMLResponse)
async def get_register():
    return FileResponse("register.html")

@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/dashboard.html", response_class=HTMLResponse)
async def get_dashboard():
    return FileResponse("dashboard.html")

@app.get("/viewer", response_class=HTMLResponse)
@app.get("/viewer.html", response_class=HTMLResponse)
@app.get("/webrtc_viewer.html", response_class=HTMLResponse)
async def get_viewer():
    return FileResponse("webrtc_viewer.html")

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

@app.get("/api/my_device")
async def get_my_device():
    import platform, socket
    return {
        "name": "This Device",
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "status": "online",
    }

@app.get("/favicon.ico")
async def favicon():
    if os.path.exists("favicon.ico"):
        return FileResponse("favicon.ico")
    return Response(status_code=204)

@app.get("/manifest.json")
async def manifest():
    if os.path.exists("manifest.json"):
        return FileResponse("manifest.json")
    return Response(status_code=404)
