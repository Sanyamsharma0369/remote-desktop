"""
app/routers/health.py — Production health and readiness probe endpoints.
"""
from datetime import datetime
import logging
import os
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db, engine
from app.services.screen_track import capture_hub
from app.services.state import pcs, control_manager

router = APIRouter()
log = logging.getLogger(__name__)

APP_VERSION = "2.0.0"


@router.get("/health")
async def health_probe() -> Dict[str, Any]:
    """
    Lightweight Liveness Probe.
    Returns HTTP 200 as long as the FastAPI process is running and accepting events.
    Does not depend on external services or downstream databases.
    """
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "version": APP_VERSION,
    }


@router.get("/ready")
async def ready_probe(db: Session = Depends(get_db)) -> JSONResponse:
    """
    Deep Readiness Probe.
    Verifies that the application can actively serve traffic:
      1. Database connectivity
      2. File storage accessibility and writability
      3. Capture hub and state manager operational status
    Returns HTTP 200 when ready, or HTTP 503 when dependencies fail.
    """
    checks: Dict[str, Any] = {
        "database": "unknown",
        "storage": "unknown",
        "capture_hub": "unknown",
    }
    is_ready = True
    failure_reasons = []

    # 1. Database Check
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "connected"
    except Exception as e:
        is_ready = False
        checks["database"] = f"error: {str(e)}"
        failure_reasons.append("database unavailable")
        log.error("Readiness check failed on database probe: %s", e)

    # 2. File Storage Check
    try:
        upload_dir = "uploads"
        if not os.path.exists(upload_dir):
            os.makedirs(upload_dir, exist_ok=True)
        if os.access(upload_dir, os.W_OK):
            checks["storage"] = "writable"
        else:
            is_ready = False
            checks["storage"] = "read-only"
            failure_reasons.append("upload storage not writable")
    except Exception as e:
        is_ready = False
        checks["storage"] = f"error: {str(e)}"
        failure_reasons.append("storage check failed")
        log.error("Readiness check failed on storage probe: %s", e)

    # 3. Subsystem Metrics
    hub_stats = capture_hub.get_hub_stats()
    checks["capture_hub"] = {
        "active_workers": hub_stats["active_workers"],
        "status": "ready",
    }
    checks["active_peers"] = len(pcs)
    checks["active_controller"] = (
        control_manager.get_active_controller_info().get("username")
        if control_manager.get_active_controller_info()
        else None
    )

    response_payload = {
        "status": "ready" if is_ready else "not_ready",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "version": APP_VERSION,
        "checks": checks,
    }
    if not is_ready:
        response_payload["reasons"] = failure_reasons
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=response_payload)

    return JSONResponse(status_code=status.HTTP_200_OK, content=response_payload)
