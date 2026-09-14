"""
app/models/session.py — UserSession and WsTicket database models.

UserSession  — tracks refresh tokens (stored as SHA-256 hashes).
WsTicket     — single-use short-lived tickets for WebSocket auth.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Text
from app.core.database import Base


class UserSession(Base):
    """Persists refresh-token metadata. Raw token is never stored."""
    __tablename__ = "user_sessions"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    token_hash   = Column(String(64), unique=True, nullable=False, index=True)
    session_label = Column(String(255), nullable=True)   # e.g. "Chrome/Windows"
    ip_address   = Column(String(45), nullable=True)
    user_agent   = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at   = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at   = Column(DateTime, nullable=True)       # NULL == still active

    @property
    def is_active(self) -> bool:
        now = datetime.utcnow()
        return self.revoked_at is None and self.expires_at > now


class WsTicket(Base):
    """
    Single-use ticket that authorises a WebSocket connection.
    Issued by POST /api/auth/ws-ticket, consumed on WS handshake.
    """
    __tablename__ = "ws_tickets"

    id          = Column(Integer, primary_key=True, index=True)
    ticket_hash = Column(String(64), unique=True, nullable=False, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    session_id  = Column(Integer, ForeignKey("user_sessions.id", ondelete="SET NULL"),
                         nullable=True)
    scope       = Column(String(64), nullable=False, default="view")
    created_at  = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at  = Column(DateTime, nullable=False)
    used_at     = Column(DateTime, nullable=True)    # NULL == not yet consumed

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and datetime.utcnow() < self.expires_at
