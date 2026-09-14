"""
app/services/state.py — Global concurrency, active peer connections, and control arbitration.

Thread-safe and async-safe tracking for:
- Active WebRTC peer connections (pcs)
- Connected control WebSockets
- Exclusive control arbitration token (at most 1 active controller at a time)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Global set of active WebRTC peer connections
pcs: Set[Any] = set()


class ControlArbitrationManager:
    """
    Manages exclusive control token arbitration across concurrent viewers.
    - Multiple viewers can stream video in View Mode.
    - Exactly ONE viewer can hold Screen Control at any given time.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # active_controller: {"user_id": int, "username": str, "ws_id": str, "acquired_at": float, "last_active_at": float}
        self.active_controller: Optional[Dict[str, Any]] = None
        # Map of ws_id -> WebSocket instance for broadcasting state changes
        self._connected_sockets: Dict[str, WebSocket] = {}

    async def register_socket(self, ws_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._connected_sockets[ws_id] = websocket

    async def unregister_socket(self, ws_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Unregisters a WebSocket and automatically releases control if the disconnecting socket was the controller.
        Returns the released controller info if a release occurred, or None.
        """
        released_info = None
        async with self._lock:
            self._connected_sockets.pop(ws_id, None)
            if self.active_controller and (
                self.active_controller.get("ws_id") == ws_id
                or (user_id is not None and self.active_controller.get("user_id") == user_id)
            ):
                released_info = self.active_controller
                self.active_controller = None
                logger.info(
                    "Control token released due to WebSocket disconnect for user '%s' (ws_id=%s)",
                    released_info.get("username"),
                    ws_id,
                )

        if released_info:
            await self._broadcast_controller_change(None)

        return released_info

    async def acquire_control(
        self,
        user_id: int,
        username: str,
        ws_id: str,
        is_admin: bool = False,
        is_admin_takeover: bool = False,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Attempts to acquire the exclusive control token.
        Returns (success: bool, status_or_reason: str, controller_info: dict | None)
        """
        now = time.monotonic()
        async with self._lock:
            if self.active_controller is None:
                self.active_controller = {
                    "user_id": user_id,
                    "username": username,
                    "ws_id": ws_id,
                    "acquired_at": now,
                    "last_active_at": now,
                }
                logger.info("Control token ACQUIRED by user '%s' (ws_id=%s)", username, ws_id)
                controller_snapshot = dict(self.active_controller)
            elif self.active_controller["user_id"] == user_id:
                # Same user refreshing or re-asserting control on this connection
                self.active_controller["ws_id"] = ws_id
                self.active_controller["last_active_at"] = now
                logger.info("Control token RETAINED by user '%s' (ws_id=%s)", username, ws_id)
                return True, "granted", dict(self.active_controller)
            elif is_admin and is_admin_takeover:
                # Admin preemption / takeover
                old_controller = self.active_controller
                self.active_controller = {
                    "user_id": user_id,
                    "username": username,
                    "ws_id": ws_id,
                    "acquired_at": now,
                    "last_active_at": now,
                }
                logger.warning(
                    "Admin '%s' PREEMPTED control token from user '%s'",
                    username,
                    old_controller.get("username"),
                )
                controller_snapshot = dict(self.active_controller)
            else:
                # Control is currently busy
                return False, "busy", dict(self.active_controller)

        await self._broadcast_controller_change(username)
        return True, "granted", controller_snapshot

    async def release_control(
        self, user_id: int, ws_id: Optional[str] = None
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
        """
        Releases the control token if held by the requesting user.
        """
        released_info = None
        async with self._lock:
            if self.active_controller and self.active_controller["user_id"] == user_id:
                if ws_id is None or self.active_controller.get("ws_id") == ws_id:
                    released_info = self.active_controller
                    self.active_controller = None
                    logger.info("Control token RELEASED by user '%s'", released_info.get("username"))

        if released_info:
            await self._broadcast_controller_change(None)
            return True, released_info

        return False, None

    def get_active_controller_info(self) -> Optional[Dict[str, Any]]:
        """Non-blocking snapshot of current controller."""
        if self.active_controller:
            return {
                "user_id": self.active_controller["user_id"],
                "username": self.active_controller["username"],
                "acquired_at": self.active_controller["acquired_at"],
            }
        return None

    def is_user_controlling(self, user_id: int, ws_id: Optional[str] = None) -> bool:
        if not self.active_controller:
            return False
        if self.active_controller["user_id"] != user_id:
            return False
        if ws_id and self.active_controller.get("ws_id") != ws_id:
            return False
        return True

    def touch_activity(self, user_id: int) -> None:
        if self.active_controller and self.active_controller["user_id"] == user_id:
            self.active_controller["last_active_at"] = time.monotonic()

    async def _broadcast_controller_change(self, active_controller_username: Optional[str]) -> None:
        """Notifies all connected control WebSockets about controller state change."""
        payload = {
            "type": "controller_changed",
            "active_controller": active_controller_username,
        }
        stale_ids = []
        for ws_id, ws in list(self._connected_sockets.items()):
            try:
                await ws.send_json(payload)
            except Exception:
                stale_ids.append(ws_id)

        for stale in stale_ids:
            self._connected_sockets.pop(stale, None)


# Singleton arbitration manager
control_manager = ControlArbitrationManager()
