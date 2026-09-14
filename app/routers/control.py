"""
app/routers/control.py — Hardened WebSocket control endpoint.

Security additions (Phase 3):
  - Origin validation against ALLOWED_WS_ORIGINS before accept.
  - Single-use WS ticket authentication (hash lookup, mark used on accept).
  - Per-message byte size limit.
  - Allowlisted event types (unknown types dropped, not crashed).
  - Server-side Screen View / Screen Control gating unchanged.
  - Mouse/keyboard rate-limiting counters.
  - release_all_keys() on disconnect/error.
  - Audit logging for accepted/rejected connections and mode transitions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Optional

import mss
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import is_safe_event
from app.models.session import WsTicket
from app.models.user import User
from app.services.input_service import InputService
from app.services.state import pcs
from app.services.screen_track import ScreenTrack
from app.services import audit as audit_svc

router = APIRouter()
log = logging.getLogger(__name__)

# ─── Quality / resolution limits (unchanged) ────────────────────────────────
MAX_WIDTH  = 2560
MAX_HEIGHT = 1600
MAX_FPS    = 30
MIN_WIDTH  = 480
MIN_HEIGHT = 270
MIN_FPS    = 10

QUALITY_PRESETS = {
    "low":      {"width": 854,  "height": 480,  "fps": 20, "bitrate": 1_200_000},
    "balanced": {"width": 1280, "height": 800,  "fps": 30, "bitrate": 4_000_000},
    "high":     {"width": 1920, "height": 1200, "fps": 30, "bitrate": 8_000_000},
    "native":   {"width": 0,    "height": 0,    "fps": 30, "bitrate": 12_000_000},
}

# ─── Allowlisted WebSocket event types ──────────────────────────────────────
_ALLOWED_ACTIONS = frozenset({
    "set_control_mode",
    "release_all_keys",
    "mousemove", "mousedown", "mouseup", "click", "scroll",
    "keydown", "keyup",
    "clipboard_push", "clipboard_pull",
    "set_quality", "set_fps", "set_monitor",
    "ping",
})

_INPUT_ACTIONS = frozenset({
    "mousemove", "mousedown", "mouseup", "click", "scroll",
    "keydown", "keyup", "clipboard_push", "clipboard_pull",
})


def validated_quality(width: int, height: int, fps: int) -> tuple[int, int, int]:
    width  = max(MIN_WIDTH,  min(int(width),  MAX_WIDTH))
    height = max(MIN_HEIGHT, min(int(height), MAX_HEIGHT))
    fps    = max(MIN_FPS,    min(int(fps),    MAX_FPS))
    width  -= width  % 2
    height -= height % 2
    return width, height, fps


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _origin_allowed(origin: Optional[str]) -> bool:
    """Return True if the WebSocket Origin header is on the allowlist."""
    if not origin:
        # Allow missing Origin in dev (same-origin browser connections)
        return not settings.is_production
    allowed = settings.allowed_ws_origins_list
    return origin.rstrip("/") in [o.rstrip("/") for o in allowed]


def _validate_and_consume_ticket(
    db: Session, raw_ticket: str, required_scope: str
) -> Optional[User]:
    """
    Look up the ticket by hash, verify it is unused/unexpired, mark it used,
    and return the owning User.  Returns None if invalid.
    """
    ticket_hash = _hash(raw_ticket)
    ticket = db.query(WsTicket).filter(WsTicket.ticket_hash == ticket_hash).first()

    if ticket is None or not ticket.is_valid:
        return None

    # Scope check: ticket must cover requested scope
    if ticket.scope != required_scope and ticket.scope != "signal":
        return None

    # Consume the ticket (single-use)
    ticket.used_at = datetime.utcnow()
    db.commit()

    user = db.query(User).filter(User.id == ticket.user_id).first()
    return user


@router.websocket("/ws/control")
async def websocket_control(
    websocket: WebSocket,
    ticket: Optional[str] = Query(default=None),
):
    db = SessionLocal()
    remote_ip: str = (
        websocket.client.host if websocket.client else "unknown"
    )
    authed_user: Optional[User] = None

    try:
        # ── 1. Origin validation ─────────────────────────────────────────
        origin = websocket.headers.get("origin")
        if not _origin_allowed(origin):
            log.warning("WS rejected: bad origin '%s' from %s", origin, remote_ip)
            audit_svc.audit_ws_rejected(db, remote_ip,
                                         f"bad origin: {origin}")
            await websocket.close(code=4003, reason="Origin not allowed")
            return

        # ── 2. Ticket authentication ─────────────────────────────────────
        if not ticket:
            log.warning("WS rejected: no ticket from %s", remote_ip)
            audit_svc.audit_ws_rejected(db, remote_ip, "missing ticket")
            await websocket.close(code=4001, reason="Authentication required")
            return

        authed_user = _validate_and_consume_ticket(db, ticket, required_scope="view")
        if authed_user is None:
            log.warning("WS rejected: invalid/expired ticket from %s", remote_ip)
            audit_svc.audit_ws_rejected(db, remote_ip,
                                         "invalid or expired ticket")
            await websocket.close(code=4001, reason="Invalid ticket")
            return

        # ── 3. Accept connection ─────────────────────────────────────────
        await websocket.accept()
        log.info("WS accepted for user '%s' from %s", authed_user.username, remote_ip)
        audit_svc.audit_ws_accepted(db, authed_user.id, remote_ip, "view")

        # ── 4. Send initial screen info ──────────────────────────────────
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                await websocket.send_json({
                    "action": "screeninfo",
                    "width": monitor["width"],
                    "height": monitor["height"],
                })
        except Exception as exc:
            log.error("Error sending screeninfo: %s", exc)
            await websocket.send_json(
                {"action": "screeninfo", "width": 1920, "height": 1080}
            )

        # ── 5. Per-connection state ──────────────────────────────────────
        control_enabled = False
        last_mouse_ts = 0.0
        mouse_event_count = 0
        mouse_window_start = time.monotonic()
        control_event_count = 0
        control_window_start = time.monotonic()

        max_msg_bytes = settings.MAX_WS_MESSAGE_BYTES
        max_mouse_rps = settings.MAX_MOUSE_EVENTS_PER_SECOND
        max_ctrl_rps  = settings.MAX_CONTROL_EVENTS_PER_SECOND
        control_expire_secs = settings.CONTROL_SESSION_EXPIRE_MINUTES * 60
        # How often to check inactivity when idle (must be < control_expire_secs)
        _IDLE_CHECK_INTERVAL = min(30.0, control_expire_secs / 2)
        last_control_ts = time.monotonic()  # reset on each control input

        # ── 6. Message loop ──────────────────────────────────────────────
        while True:
            # Use a timeout so we can enforce inactivity timeouts even
            # when no messages arrive (e.g., viewer is watching but not typing).
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=_IDLE_CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                # No message arrived — check inactivity if control mode is on
                if control_enabled:
                    idle_secs = time.monotonic() - last_control_ts
                    if idle_secs >= control_expire_secs:
                        log.info(
                            "Control session inactivity timeout (%.0fs) for user '%s' — "
                            "reverting to view mode",
                            idle_secs, authed_user.username,
                        )
                        control_enabled = False
                        InputService.release_all_keys()
                        audit_svc.audit_control_end(db, authed_user.id, remote_ip)
                        try:
                            await websocket.send_json({
                                "type": "control_mode",
                                "enabled": False,
                                "reason": "inactivity_timeout",
                            })
                        except Exception:
                            pass
                continue  # resume waiting for the next message

            # Size limit
            if len(raw.encode()) > max_msg_bytes:
                log.warning("WS oversized message (%d bytes) from %s",
                            len(raw), remote_ip)
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("WS malformed JSON from %s", remote_ip)
                continue

            action = msg.get("action") or msg.get("type")
            if not action or action not in _ALLOWED_ACTIONS:
                # Silently drop unknown events
                continue

            # ── Rate limiting ────────────────────────────────────────────
            now = time.monotonic()
            if action == "mousemove":
                window = now - mouse_window_start
                if window >= 1.0:
                    mouse_event_count = 0
                    mouse_window_start = now
                mouse_event_count += 1
                if mouse_event_count > max_mouse_rps:
                    continue  # drop excess mouse events silently

            if action in _INPUT_ACTIONS and action != "mousemove":
                window = now - control_window_start
                if window >= 1.0:
                    control_event_count = 0
                    control_window_start = now
                control_event_count += 1
                if control_event_count > max_ctrl_rps:
                    continue

            # ── Control mode toggle ──────────────────────────────────────
            if action == "set_control_mode":
                control_enabled = bool(msg.get("enabled"))
                log.info("Control mode -> %s for user '%s'",
                         control_enabled, authed_user.username)
                if not control_enabled:
                    InputService.release_all_keys()
                    audit_svc.audit_control_end(db, authed_user.id, remote_ip)
                else:
                    last_control_ts = time.monotonic()  # reset inactivity on enable
                    audit_svc.audit_control_start(db, authed_user.id, remote_ip)
                await websocket.send_json({
                    "type": "control_mode",
                    "enabled": control_enabled,
                })
                continue

            if action == "release_all_keys":
                InputService.release_all_keys()
                continue

            # ── Server-side gating ───────────────────────────────────────
            if action in _INPUT_ACTIONS and not control_enabled:
                await websocket.send_json({
                    "type": "control_denied",
                    "message": "Screen Control mode is disabled.",
                })
                continue

            # ── Dispatch ─────────────────────────────────────────────────
            if action in _INPUT_ACTIONS and control_enabled:
                last_control_ts = time.monotonic()  # reset inactivity timer
            try:
                if action == "mousemove":
                    InputService.move_to(
                        x=msg.get("x", 0),
                        y=msg.get("y", 0),
                        x_ratio=msg.get("x_ratio"),
                        y_ratio=msg.get("y_ratio"),
                        monitor_index=msg.get("monitor", 1),
                    )
                elif action == "mousedown":
                    InputService.mouse_down(msg.get("button", "left"))
                elif action == "mouseup":
                    InputService.mouse_up(msg.get("button", "left"))
                elif action == "click":
                    InputService.click(msg.get("button", "left"))
                elif action == "scroll":
                    InputService.scroll(msg.get("amount", 0))

                elif action in ("keydown", "keyup"):
                    InputService.handle_keyboard(msg)

                elif action == "clipboard_push":
                    text = msg.get("text", "")
                    if len(text) > 65_536:
                        await websocket.send_json(
                            {"type": "clipboard_ack", "success": False,
                             "error": "Clipboard payload too large"}
                        )
                        continue
                    InputService.set_host_clipboard(text)
                    await websocket.send_json({
                        "type": "clipboard_ack",
                        "success": True,
                        "length": len(text),
                    })

                elif action == "clipboard_pull":
                    text = InputService.get_host_clipboard()
                    await websocket.send_json({
                        "type": "clipboard_data",
                        "text": text,
                        "length": len(text),
                    })

                elif action == "set_quality":
                    preset_name = msg.get("preset")
                    width  = msg.get("width")
                    height = msg.get("height")
                    fps    = int(msg.get("fps", 30))

                    cfg = None
                    if width is not None and height is not None:
                        w, h, f = validated_quality(width, height, fps)
                        cfg = {"width": w, "height": h, "fps": f}
                    elif preset_name in QUALITY_PRESETS:
                        cfg = dict(QUALITY_PRESETS[preset_name])
                        if cfg["width"] > 0 and cfg["height"] > 0:
                            cfg["width"], cfg["height"], cfg["fps"] = validated_quality(
                                cfg["width"], cfg["height"], cfg.get("fps", fps)
                            )
                        else:
                            cfg["fps"] = max(MIN_FPS, min(cfg.get("fps", fps), MAX_FPS))

                    if cfg:
                        for pc in pcs:
                            for sender in pc.getSenders():
                                if isinstance(sender.track, ScreenTrack):
                                    sender.track.set_quality(
                                        cfg["width"], cfg["height"], cfg["fps"]
                                    )
                        await websocket.send_json({
                            "type": "quality_changed",
                            "preset": preset_name or "custom",
                            "config": cfg,
                        })

                elif action == "set_fps":
                    new_fps = max(MIN_FPS, min(int(msg.get("fps", 15)), MAX_FPS))
                    for pc in pcs:
                        for sender in pc.getSenders():
                            if isinstance(sender.track, ScreenTrack):
                                sender.track.fps = new_fps

                elif action == "set_monitor":
                    idx = int(msg.get("index", 1))
                    for pc in pcs:
                        for sender in pc.getSenders():
                            if isinstance(sender.track, ScreenTrack):
                                sender.track.set_monitor(idx)

                elif action == "ping":
                    pass  # keepalive — no reply needed

            except Exception as exc:
                log.error("Control event error (action=%s): %s", action, exc)

    except WebSocketDisconnect:
        log.info("WS disconnected (user=%s)",
                 authed_user.username if authed_user else "unauthenticated")
    except Exception as exc:
        log.error("WS error: %s", exc)
    finally:
        # Always release all keys/buttons on disconnect
        try:
            InputService.release_all_keys()
        except Exception:
            pass
        if authed_user and control_enabled if "control_enabled" in dir() else False:
            try:
                audit_svc.audit_control_end(db, authed_user.id, remote_ip)
            except Exception:
                pass
        db.close()
        try:
            await websocket.close()
        except Exception:
            pass
