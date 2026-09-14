import subprocess
import platform
import logging
from fastapi import APIRouter, Depends, Request
from app.routers.auth import require_admin
from app.models.user import User
from app.core.database import get_db
from sqlalchemy.orm import Session
from app.models.audit import AuditEvent
from app.routers.auth import limiter

router = APIRouter()
log = logging.getLogger(__name__)

def log_power_audit(db: Session, user_id: int, action: str, ip_address: str):
    audit = AuditEvent(
        user_id=user_id,
        action=f"power_{action}",
        ip_address=ip_address,
        details=f"Triggered power action: {action}"
    )
    db.add(audit)
    db.commit()

@router.post("/lock")
@limiter.limit("5/minute")
async def power_lock(request: Request, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        log_power_audit(db, current_user.id, "lock", request.client.host)
        return {"success": True}
    except Exception as e:
        log.error(f"Lock error: {e}")
        return {"success": False, "error": str(e)}

@router.post("/shutdown")
@limiter.limit("5/minute")
async def power_shutdown(request: Request, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/s", "/t", "0"])
        log_power_audit(db, current_user.id, "shutdown", request.client.host)
        return {"success": True}
    except Exception as e:
        log.error(f"Shutdown error: {e}")
        return {"success": False, "error": str(e)}

@router.post("/restart")
@limiter.limit("5/minute")
async def power_restart(request: Request, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/r", "/t", "0"])
        log_power_audit(db, current_user.id, "restart", request.client.host)
        return {"success": True}
    except Exception as e:
        log.error(f"Restart error: {e}")
        return {"success": False, "error": str(e)}

@router.post("/hibernate")
@limiter.limit("5/minute")
async def power_hibernate(request: Request, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/h"])
        log_power_audit(db, current_user.id, "hibernate", request.client.host)
        return {"success": True}
    except Exception as e:
        log.error(f"Hibernate error: {e}")
        return {"success": False, "error": str(e)}

@router.post("/sleep")
@limiter.limit("5/minute")
async def power_sleep(request: Request, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "Sleep", "0", "0"])
        log_power_audit(db, current_user.id, "sleep", request.client.host)
        return {"success": True}
    except Exception as e:
        log.error(f"Sleep error: {e}")
        return {"success": False, "error": str(e)}
