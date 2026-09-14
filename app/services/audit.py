"""
app/services/audit.py — Structured security event logging.

Rules:
- Never log passwords, JWTs, refresh tokens, WS tickets,
  clipboard contents, or file content.
- Always log user ID (not username alone), IP, action, success, reason.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import Optional, Any, Dict

from sqlalchemy.orm import Session
from app.models.audit import AuditEvent

log = logging.getLogger("audit")


def log_event(
    db: Session,
    action: str,
    *,
    success: bool = True,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    reason: Optional[str] = None,
    session_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write a structured audit record to the database and to the audit logger.
    Call from within a request context that already has a DB session.
    """
    event = AuditEvent(
        timestamp=datetime.utcnow(),
        user_id=user_id,
        username=username,
        ip_address=ip_address,
        user_agent=user_agent,
        action=action,
        success=success,
        reason=reason,
        session_id=session_id,
        extra=json.dumps(extra) if extra else None,
    )
    try:
        db.add(event)
        db.commit()
    except Exception as exc:
        db.rollback()
        log.error("Failed to write audit event: %s", exc)

    # Also emit to standard logger (useful with log aggregators)
    level = logging.INFO if success else logging.WARNING
    log.log(
        level,
        "AUDIT action=%s success=%s user_id=%s ip=%s reason=%s",
        action,
        success,
        user_id,
        ip_address,
        reason,
    )


# ── Convenience wrappers for common events ─────────────────────────────────

def audit_login_success(db, user_id, username, ip, ua, session_id=None):
    log_event(db, "login_success", user_id=user_id, username=username,
              ip_address=ip, user_agent=ua, session_id=session_id)

def audit_login_failure(db, username, ip, ua, reason="bad credentials"):
    log_event(db, "login_failure", success=False, username=username,
              ip_address=ip, user_agent=ua, reason=reason)

def audit_logout(db, user_id, ip, session_id=None):
    log_event(db, "logout", user_id=user_id, ip_address=ip, session_id=session_id)

def audit_logout_all(db, user_id, ip):
    log_event(db, "logout_all", user_id=user_id, ip_address=ip)

def audit_token_refresh(db, user_id, ip, session_id=None):
    log_event(db, "token_refresh", user_id=user_id, ip_address=ip,
              session_id=session_id)

def audit_token_refresh_failure(db, ip, reason):
    log_event(db, "token_refresh_failure", success=False,
              ip_address=ip, reason=reason)

def audit_ws_accepted(db, user_id, ip, scope):
    log_event(db, "ws_accepted", user_id=user_id, ip_address=ip,
              extra={"scope": scope})

def audit_ws_rejected(db, ip, reason):
    log_event(db, "ws_rejected", success=False, ip_address=ip, reason=reason)

def audit_control_start(db, user_id, ip):
    log_event(db, "control_start", user_id=user_id, ip_address=ip)

def audit_control_end(db, user_id, ip):
    log_event(db, "control_end", user_id=user_id, ip_address=ip)

def audit_ws_ticket_issued(db, user_id, ip, scope):
    log_event(db, "ws_ticket_issued", user_id=user_id, ip_address=ip,
              extra={"scope": scope})

def audit_power_action(db, user_id, ip, action_name):
    log_event(db, "power_action", user_id=user_id, ip_address=ip,
              extra={"power": action_name})

def audit_file_upload(db, user_id, ip, filename):
    log_event(db, "file_upload", user_id=user_id, ip_address=ip,
              extra={"filename": filename})

def audit_file_delete(db, user_id, ip, file_id):
    log_event(db, "file_delete", user_id=user_id, ip_address=ip,
              extra={"file_id": file_id})
