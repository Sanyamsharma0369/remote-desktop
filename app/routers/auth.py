"""
app/routers/auth.py — Authentication, session management, and WS ticket endpoints.

Security model:
  - Short-lived JWT access token (15 min production default) returned in body.
  - Opaque refresh token stored as SHA-256 hash in DB;
    raw token delivered in HttpOnly Secure SameSite=Strict cookie.
  - Refresh tokens rotate on every use; reuse of revoked token revokes all sessions.
  - One-time WS tickets authorise WebSocket connections without putting
    a long-lived JWT in a URL query string.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.models.session import UserSession, WsTicket
from app.services import audit as audit_svc

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)
oauth2_scheme = HTTPBearer(auto_error=False)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
log = logging.getLogger(__name__)

VALID_WS_SCOPES = {"view", "control", "clipboard", "files", "power", "signal"}


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

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

class SessionInfo(BaseModel):
    id: int
    session_label: Optional[str]
    ip_address: Optional[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: datetime

class WsTicketRequest(BaseModel):
    scope: str = "view"

class WsTicketResponse(BaseModel):
    ticket: str
    expires_in: int   # seconds

class RefreshResponse(BaseModel):
    access_token: str
    token_type: str


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hash(value: str) -> str:
    """SHA-256 hex digest. Used for refresh tokens and WS tickets."""
    return hashlib.sha256(value.encode()).hexdigest()


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    user = db.query(User).filter(User.username == username).first()
    if user and verify_password(password, user.password_hash):
        return user
    return None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key="refresh_token",
        value=raw_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path=settings.REFRESH_COOKIE_PATH,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86_400,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key="refresh_token",
        path=settings.REFRESH_COOKIE_PATH,
    )


def _create_user_session(
    db: Session,
    user_id: int,
    ip: str,
    ua: str,
) -> tuple[UserSession, str]:
    """Create a new UserSession, returning (session_obj, raw_token)."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash(raw_token)
    session = UserSession(
        user_id=user_id,
        token_hash=token_hash,
        session_label=ua[:255] if ua else None,
        ip_address=ip,
        user_agent=ua,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, raw_token


def _revoke_all_sessions(db: Session, user_id: int) -> None:
    now = datetime.utcnow()
    db.query(UserSession).filter(
        UserSession.user_id == user_id,
        UserSession.revoked_at.is_(None),
    ).update({"revoked_at": now})
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency — get current user from Bearer token
# ─────────────────────────────────────────────────────────────────────────────

def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise exc
    try:
        payload = jwt.decode(
            creds.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        username: str = payload.get("sub")
        if not username:
            raise exc
    except JWTError:
        raise exc
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise exc
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Admin access required")
    return current_user


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=Token)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    user_credentials: UserLogin,
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")
    user = authenticate_user(db, user_credentials.username, user_credentials.password)

    if not user:
        audit_svc.audit_login_failure(db, user_credentials.username, ip, ua)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Issue short-lived access token
    access_token = create_access_token({"sub": user.username})

    # Issue refresh token (raw in cookie, hash in DB)
    session, raw_refresh = _create_user_session(db, user.id, ip, ua)
    _set_refresh_cookie(response, raw_refresh)

    audit_svc.audit_login_success(db, user.id, user.username, ip, ua,
                                   session_id=session.id)
    return Token(
        access_token=access_token,
        token_type="bearer",
        username=user.username,
        role=user.role,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    refresh_token: Optional[str] = Cookie(default=None),
):
    ip = _client_ip(request)

    if not refresh_token:
        audit_svc.audit_token_refresh_failure(db, ip, "missing refresh cookie")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="No refresh token")

    token_hash = _hash(refresh_token)
    session = db.query(UserSession).filter(
        UserSession.token_hash == token_hash
    ).first()

    # Token not found — could be a forged attempt
    if session is None:
        audit_svc.audit_token_refresh_failure(db, ip, "token not found")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid refresh token")

    # Detect reuse of a revoked token — security event: revoke all sessions
    if session.revoked_at is not None:
        log.warning(
            "Refresh token reuse detected for user_id=%s — revoking all sessions",
            session.user_id,
        )
        _revoke_all_sessions(db, session.user_id)
        _clear_refresh_cookie(response)
        audit_svc.log_event(db, "refresh_token_reuse", success=False,
                            user_id=session.user_id, ip_address=ip,
                            reason="Revoked token reuse — all sessions revoked")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Session revoked")

    if datetime.utcnow() > session.expires_at:
        audit_svc.audit_token_refresh_failure(db, ip, "token expired")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Refresh token expired")

    user = db.query(User).filter(User.id == session.user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User not found")

    # Rotate refresh token — revoke old, issue new
    session.revoked_at = datetime.utcnow()
    db.commit()

    ua = request.headers.get("User-Agent", "")
    new_session, new_raw = _create_user_session(db, user.id, ip, ua)
    _set_refresh_cookie(response, new_raw)

    access_token = create_access_token({"sub": user.username})

    audit_svc.audit_token_refresh(db, user.id, ip, session_id=new_session.id)
    return RefreshResponse(access_token=access_token, token_type="bearer")


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    refresh_token: Optional[str] = Cookie(default=None),
):
    ip = _client_ip(request)
    if refresh_token:
        token_hash = _hash(refresh_token)
        db.query(UserSession).filter(
            UserSession.token_hash == token_hash,
            UserSession.revoked_at.is_(None),
        ).update({"revoked_at": datetime.utcnow()})
        db.commit()
    _clear_refresh_cookie(response)
    audit_svc.audit_logout(db, current_user.id, ip)
    return {"message": "Logged out"}


@router.post("/logout-all")
async def logout_all(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)
    _revoke_all_sessions(db, current_user.id)
    _clear_refresh_cookie(response)
    audit_svc.audit_logout_all(db, current_user.id, ip)
    return {"message": "All sessions revoked"}


@router.get("/sessions", response_model=List[SessionInfo])
async def list_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sessions = db.query(UserSession).filter(
        UserSession.user_id == current_user.id,
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > datetime.utcnow(),
    ).all()
    return [
        SessionInfo(
            id=s.id,
            session_label=s.session_label,
            ip_address=s.ip_address,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            expires_at=s.expires_at,
        )
        for s in sessions
    ]


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.user_id == current_user.id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.revoked_at = datetime.utcnow()
    db.commit()
    audit_svc.log_event(db, "session_revoked", user_id=current_user.id,
                        ip_address=_client_ip(request),
                        extra={"revoked_session_id": session_id})
    return {"message": "Session revoked"}


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def issue_ws_ticket(
    request: Request,
    body: WsTicketRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Issue a short-lived single-use ticket to authenticate a WebSocket connection.
    The raw ticket is returned to the caller; only its hash is stored in the DB.
    """
    scope = body.scope.strip().lower()
    if scope not in VALID_WS_SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"Invalid scope. Valid: {VALID_WS_SCOPES}")

    raw_ticket = secrets.token_urlsafe(32)
    ticket_hash = _hash(raw_ticket)
    ttl = settings.WEBSOCKET_TICKET_EXPIRE_SECONDS

    ws_ticket = WsTicket(
        ticket_hash=ticket_hash,
        user_id=current_user.id,
        scope=scope,
        expires_at=datetime.utcnow() + timedelta(seconds=ttl),
    )
    db.add(ws_ticket)
    db.commit()

    ip = _client_ip(request)
    audit_svc.audit_ws_ticket_issued(db, current_user.id, ip, scope)

    return WsTicketResponse(ticket=raw_ticket, expires_in=ttl)


# ─────────────────────────────────────────────────────────────────────────────
# Existing register / users / me endpoints (unchanged in behaviour)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/register", response_model=UserResponse)
def register_user(
    user_data: UserCreate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(status_code=400, detail="Username already registered")
    db_user = User(
        username=user_data.username,
        password_hash=get_password_hash(user_data.password),
        role=user_data.role,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return UserResponse(id=db_user.id, username=db_user.username, role=db_user.role)


@router.get("/users", response_model=list[UserResponse])
def get_users(current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [UserResponse(id=u.id, username=u.username, role=u.role)
            for u in db.query(User).all()]


@router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    return UserResponse(id=current_user.id, username=current_user.username,
                        role=current_user.role)
