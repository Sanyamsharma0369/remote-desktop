"""
tests/test_observability.py — Regression tests for Health Probes, Structured Logging, and Graceful Shutdown.
"""
import asyncio
from unittest.mock import MagicMock, patch
import pytest
from fastapi import status


def test_health_probe_success(client):
    """Liveness probe /api/health returns HTTP 200 without requiring authentication or external services."""
    res = client.get("/api/health")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["status"] == "ok"
    assert data["version"] == "2.0.0"
    assert "timestamp" in data


def test_ready_probe_healthy(client):
    """Readiness probe /api/ready returns HTTP 200 and details when all subsystems are operational."""
    res = client.get("/api/ready")
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["status"] == "ready"
    assert "checks" in data
    assert data["checks"]["database"] == "connected"
    assert data["checks"]["storage"] == "writable"
    assert data["checks"]["capture_hub"]["status"] == "ready"
    assert "active_peers" in data["checks"]


def test_ready_probe_database_failure(client, monkeypatch):
    """Readiness probe /api/ready returns HTTP 503 if database connectivity fails."""
    from sqlalchemy.orm import Session
    def mock_execute(self, *args, **kwargs):
        raise Exception("Database connection lost")

    monkeypatch.setattr(Session, "execute", mock_execute)
    res = client.get("/api/ready")
    assert res.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    data = res.json()
    assert data["status"] == "not_ready"
    assert "error: Database connection lost" in data["checks"]["database"]
    assert "database unavailable" in data["reasons"]


def test_ready_probe_storage_unwritable(client, monkeypatch):
    """Readiness probe /api/ready returns HTTP 503 if storage directory is read-only or unwritable."""
    import os
    original_access = os.access

    def mock_access(path, mode):
        if "uploads" in str(path) and mode == os.W_OK:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", mock_access)

    res = client.get("/api/ready")
    assert res.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    data = res.json()
    assert data["status"] == "not_ready"
    assert data["checks"]["storage"] == "read-only"
    assert "upload storage not writable" in data["reasons"]


def test_request_id_propagation_and_generation(client):
    """StructuredLoggingMiddleware propagates client X-Request-ID or auto-generates one."""
    # 1. Client supplies X-Request-ID
    custom_id = "test-custom-trace-id-abc123"
    res1 = client.get("/api/health", headers={"X-Request-ID": custom_id})
    assert res1.status_code == 200
    assert res1.headers.get("X-Request-ID") == custom_id

    # 2. Client does not supply X-Request-ID -> server generates 16-hex char ID
    res2 = client.get("/api/health")
    assert res2.status_code == 200
    generated_id = res2.headers.get("X-Request-ID")
    assert generated_id is not None
    assert len(generated_id) == 16


@pytest.mark.anyio
async def test_graceful_shutdown_lifecycle():
    """
    Tests that on_shutdown gracefully terminates active WebRTC peers,
    clears control session locks, stops capture workers, and behaves idempotently.
    """
    from app.main import on_shutdown
    from app.services.state import pcs, control_manager
    from app.services.screen_track import capture_hub

    # 1. Setup mock peer connection
    mock_pc = MagicMock()
    mock_pc.close = MagicMock(return_value=asyncio.sleep(0.001))
    pcs.add(mock_pc)
    assert len(pcs) == 1

    # 2. Setup mock active controller
    await control_manager.acquire_control(user_id=1, username="testuser_obs", ws_id="ws_1")
    assert control_manager.get_active_controller_info()["username"] == "testuser_obs"

    # 3. Setup mock capture worker
    worker = capture_hub.get_worker(0)
    worker._is_running = True

    # 4. Invoke shutdown handler
    await on_shutdown()

    # 5. Assert all components cleaned up
    assert len(pcs) == 0
    assert control_manager.get_active_controller_info() is None
    assert capture_hub.get_active_worker_count() == 0

    # 6. Test idempotency (calling shutdown a 2nd time should complete cleanly)
    await on_shutdown()
    assert len(pcs) == 0


def test_structured_logging_secret_redaction(client, caplog):
    """Verifies that access logs do not expose credentials, tokens, or sensitive parameters."""
    import logging
    with caplog.at_level(logging.INFO):
        client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "SuperSecretPassword123!"},
        )
    # Check that plain password is never printed in middleware log lines
    assert "SuperSecretPassword123!" not in caplog.text

