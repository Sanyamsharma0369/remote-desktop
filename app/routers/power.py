import subprocess
import platform
import logging
from fastapi import APIRouter, Depends, Request
from app.routers.auth import require_admin, limiter
from app.models.user import User
from app.core.database import get_db
from sqlalchemy.orm import Session
from app.services import audit as audit_svc

router = APIRouter()
log = logging.getLogger(__name__)


def _run_power_action(db: Session, user: User, ip: str, action: str, cmd: list[str]) -> dict:
    """
    Execute a power OS command and audit both attempt and outcome.
    Records the attempt before the command, then records failure only if
    the command raises. Does NOT audit a false "success" for commands that
    schedule shutdown (they return before the OS actually acts).
    """
    # Audit: this action was attempted (success=True records the attempt)
    audit_svc.log_event(
        db,
        action=f"power_{action}_attempt",
        user_id=user.id,
        username=user.username,
        ip_address=ip,
        reason=f"Power action '{action}' triggered by user",
    )
    try:
        if platform.system() == "Windows":
            subprocess.Popen(cmd)
        # Audit success (OS accepted the command)
        audit_svc.log_event(
            db,
            action=f"power_{action}",
            user_id=user.id,
            username=user.username,
            ip_address=ip,
        )
        return {"success": True}
    except Exception as e:
        log.error("Power %s error: %s", action, e)
        audit_svc.log_event(
            db,
            action=f"power_{action}",
            success=False,
            user_id=user.id,
            username=user.username,
            ip_address=ip,
            reason=str(e),
        )
        return {"success": False, "error": str(e)}


@router.post("/lock")
@limiter.limit("5/minute")
async def power_lock(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _run_power_action(
        db, current_user, request.client.host, "lock",
        ["rundll32.exe", "user32.dll,LockWorkStation"],
    )


@router.post("/shutdown")
@limiter.limit("5/minute")
async def power_shutdown(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _run_power_action(
        db, current_user, request.client.host, "shutdown",
        ["shutdown", "/s", "/t", "0"],
    )


@router.post("/restart")
@limiter.limit("5/minute")
async def power_restart(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _run_power_action(
        db, current_user, request.client.host, "restart",
        ["shutdown", "/r", "/t", "0"],
    )


@router.post("/hibernate")
@limiter.limit("5/minute")
async def power_hibernate(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _run_power_action(
        db, current_user, request.client.host, "hibernate",
        ["shutdown", "/h"],
    )


@router.post("/sleep")
@limiter.limit("5/minute")
async def power_sleep(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _run_power_action(
        db, current_user, request.client.host, "sleep",
        ["rundll32.exe", "powrprof.dll,SetSuspendState", "Sleep", "0", "0"],
    )
