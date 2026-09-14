"""
app/routers/audit.py — Audit event query endpoints (admin-only).
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.audit import AuditEvent
from app.models.user import User
from app.routers.auth import require_admin

router = APIRouter()


class AuditEventOut(BaseModel):
    id: int
    timestamp: datetime
    user_id: Optional[int]
    username: Optional[str]
    ip_address: Optional[str]
    action: str
    success: bool
    reason: Optional[str]
    session_id: Optional[int]

    model_config = {"from_attributes": True}


@router.get("/", response_model=List[AuditEventOut])
def get_audit_events(
    action: Optional[str] = Query(default=None),
    user_id: Optional[int] = Query(default=None),
    success: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(AuditEvent).order_by(AuditEvent.timestamp.desc())
    if action:
        q = q.filter(AuditEvent.action == action)
    if user_id is not None:
        q = q.filter(AuditEvent.user_id == user_id)
    if success is not None:
        q = q.filter(AuditEvent.success == success)
    return q.offset(offset).limit(limit).all()
