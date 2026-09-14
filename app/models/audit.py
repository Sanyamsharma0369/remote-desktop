"""
app/models/audit.py — Structured audit / security event log.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from app.core.database import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id           = Column(Integer, primary_key=True, index=True)
    timestamp    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_id      = Column(Integer, nullable=True, index=True)   # NULL for pre-auth events
    username     = Column(String(255), nullable=True)
    ip_address   = Column(String(45), nullable=True)
    user_agent   = Column(Text, nullable=True)
    action       = Column(String(64), nullable=False, index=True)  # e.g. "login_success"
    success      = Column(Boolean, nullable=False, default=True)
    reason       = Column(Text, nullable=True)                  # human-readable detail
    session_id   = Column(Integer, nullable=True)               # correlation
    extra        = Column(Text, nullable=True)                   # JSON blob for extra fields
