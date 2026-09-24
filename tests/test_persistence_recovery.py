"""
tests/test_persistence_recovery.py — M5.4 Persistence, Backup/Recovery, and Restart Recovery Tests.
"""
import os
import shutil
import tempfile
import sqlite3
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.user import User
from app.models.session import UserSession
from app.models.file import FileRecord
from app.models.audit import AuditEvent
from app.routers.auth import get_password_hash
from app.services.backup import create_backup, verify_backup, restore_backup


def test_database_persistence_across_restart():
    """
    Verifies that users, password hashes, sessions, files, and audit records
    persist accurately across database connections and simulated process restarts.
    """
    temp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(temp_dir, "test_persistence.db")
        db_url = f"sqlite:///{db_path}"

        # 1. First Process Life: Create tables and seed data
        engine1 = create_engine(db_url)
        Base.metadata.create_all(bind=engine1)
        Session1 = sessionmaker(bind=engine1)
        db1 = Session1()

        u_admin = User(username="admin_p", password_hash=get_password_hash("AdminPass123!"), role="admin")
        u_user = User(username="user_p", password_hash=get_password_hash("UserPass123!"), role="user")
        db1.add_all([u_admin, u_user])
        db1.commit()

        from datetime import timedelta, datetime
        now = datetime.utcnow()
        sess_active = UserSession(
            user_id=u_user.id,
            token_hash="active_hash_123",
            ip_address="127.0.0.1",
            expires_at=now + timedelta(days=7),
        )
        sess_revoked = UserSession(
            user_id=u_user.id,
            token_hash="revoked_hash_456",
            ip_address="127.0.0.1",
            expires_at=now + timedelta(days=7),
            revoked_at=now,
        )
        db1.add_all([sess_active, sess_revoked])

        audit = AuditEvent(action="test_persistence_event", success=True, user_id=u_admin.id, ip_address="127.0.0.1")
        db1.add(audit)
        db1.commit()

        # Simulate process termination
        db1.close()
        engine1.dispose()

        # 2. Second Process Life: Connect to existing DB file
        engine2 = create_engine(db_url)
        Session2 = sessionmaker(bind=engine2)
        db2 = Session2()

        loaded_admin = db2.query(User).filter(User.username == "admin_p").first()
        loaded_user = db2.query(User).filter(User.username == "user_p").first()
        assert loaded_admin is not None and loaded_admin.role == "admin"
        assert loaded_user is not None and loaded_user.role == "user"

        sessions = db2.query(UserSession).filter(UserSession.user_id == loaded_user.id).all()
        assert len(sessions) == 2
        active_s = next(s for s in sessions if s.token_hash == "active_hash_123")
        revoked_s = next(s for s in sessions if s.token_hash == "revoked_hash_456")
        assert active_s.is_active is True
        assert revoked_s.is_active is False

        audits = db2.query(AuditEvent).filter(AuditEvent.action == "test_persistence_event").all()
        assert len(audits) == 1
        assert audits[0].user_id == loaded_admin.id

        db2.close()
        engine2.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_uploaded_file_persistence_and_access_boundaries():
    """
    Verifies that uploaded files and their database records persist across restarts,
    retaining uploader ownership and access control boundaries.
    """
    temp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(temp_dir, "test_files.db")
        uploads_dir = os.path.join(temp_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)

        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        u1 = User(username="alice", password_hash=get_password_hash("Pass123!"), role="user")
        u2 = User(username="bob", password_hash=get_password_hash("Pass123!"), role="user")
        admin = User(username="admin_file", password_hash=get_password_hash("AdminPass123!"), role="admin")
        db.add_all([u1, u2, admin])
        db.commit()

        # Create physical file in uploads
        file_content = b"Confidential Report Content for Alice"
        file_id = "file_alice_001"
        physical_file_path = os.path.join(uploads_dir, f"{file_id}_report.txt")
        with open(physical_file_path, "wb") as f:
            f.write(file_content)

        file_rec = FileRecord(
            file_id=file_id,
            filename="report.txt",
            size=len(file_content),
            path=physical_file_path,
            content_type="text/plain",
            uploader_id=u1.id,
        )
        db.add(file_rec)
        db.commit()

        # Simulate restart
        db.close()
        engine.dispose()

        # Reopen after restart
        engine_reopened = create_engine(f"sqlite:///{db_path}")
        Session_reopened = sessionmaker(bind=engine_reopened)
        db_reopened = Session_reopened()

        rec = db_reopened.query(FileRecord).filter(FileRecord.file_id == file_id).first()
        assert rec is not None
        assert os.path.exists(rec.path)
        with open(rec.path, "rb") as f:
            assert f.read() == file_content

        # Access check helper
        from app.routers.files import _user_can_access
        alice = db_reopened.query(User).filter(User.username == "alice").first()
        bob = db_reopened.query(User).filter(User.username == "bob").first()
        admin_u = db_reopened.query(User).filter(User.username == "admin_file").first()

        assert _user_can_access(rec, alice) is True
        assert _user_can_access(rec, bob) is False
        assert _user_can_access(rec, admin_u) is True

        db_reopened.close()
        engine_reopened.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.anyio
async def test_restart_recovery_and_zero_orphan_workers():
    """
    Tests restart cleanup and verifies that new connection sessions cleanly reinitialize
    without orphan workers or resource leaks.
    """
    from unittest.mock import MagicMock
    from app.main import on_shutdown
    from app.services.state import pcs, control_manager
    from app.services.screen_track import capture_hub

    # 1. Simulate running session before restart
    worker = capture_hub.get_worker(0)
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    with worker._lock:
        worker._thread = mock_thread
        worker._stop_event.clear()
    await control_manager.acquire_control(user_id=10, username="user_restart", ws_id="ws_1")

    assert capture_hub.get_active_worker_count() == 1
    assert control_manager.get_active_controller_info()["username"] == "user_restart"

    # 2. Simulate shutdown on restart
    await on_shutdown()

    assert capture_hub.get_active_worker_count() == 0
    assert control_manager.get_active_controller_info() is None
    assert len(pcs) == 0

    # 3. Simulate new viewer session connecting after restart
    new_worker = capture_hub.get_worker(0)
    assert new_worker is not None
    assert capture_hub.get_active_worker_count() == 0  # Not running until subscribed


def test_full_backup_data_loss_and_restore_cycle():
    """
    Comprehensive backup & restore cycle:
      1. Create DB + files
      2. Produce backup archive
      3. Verify backup checksums & manifest
      4. Simulate catastrophic data loss (wipe DB and uploads)
      5. Restore from backup archive
      6. Verify 100% data fidelity
    """
    temp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(temp_dir, "prod.db")
        uploads_dir = os.path.join(temp_dir, "uploads")
        backup_dir = os.path.join(temp_dir, "backups")
        os.makedirs(uploads_dir, exist_ok=True)

        # 1. Seed database
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        admin = User(username="admin_b", password_hash=get_password_hash("AdminPass123!"), role="admin")
        db.add(admin)
        db.commit()

        # Seed upload file
        sample_data = b"State Persistence Verification Payload 2026"
        sample_path = os.path.join(uploads_dir, "sample.txt")
        with open(sample_path, "wb") as f:
            f.write(sample_data)

        file_rec = FileRecord(
            file_id="fid_999",
            filename="sample.txt",
            size=len(sample_data),
            path=sample_path,
            content_type="text/plain",
            uploader_id=admin.id,
        )
        db.add(file_rec)
        db.commit()
        db.close()
        engine.dispose()

        # 2. Create backup
        backup_archive = create_backup(backup_dir=backup_dir, db_path=db_path, uploads_dir=uploads_dir)
        assert os.path.exists(backup_archive)

        # 3. Verify backup
        verification = verify_backup(backup_archive)
        assert verification["valid"] is True
        assert verification["has_database"] is True
        assert verification["file_count"] == 1
        assert "sample.txt" in verification["manifest"]["uploads"][0]["rel_path"]

        # 4. Simulate catastrophic data loss
        os.remove(db_path)
        shutil.rmtree(uploads_dir)
        assert not os.path.exists(db_path)
        assert not os.path.exists(uploads_dir)

        # 5. Restore from backup
        restore_res = restore_backup(backup_archive, target_db_path=db_path, target_uploads_dir=uploads_dir)
        assert restore_res["success"] is True

        # 6. Verify restored data
        assert os.path.exists(db_path)
        assert os.path.exists(sample_path)
        with open(sample_path, "rb") as f:
            assert f.read() == sample_data

        engine_restored = create_engine(f"sqlite:///{db_path}")
        Session_restored = sessionmaker(bind=engine_restored)
        db_restored = Session_restored()

        restored_admin = db_restored.query(User).filter(User.username == "admin_b").first()
        assert restored_admin is not None
        restored_rec = db_restored.query(FileRecord).filter(FileRecord.file_id == "fid_999").first()
        assert restored_rec is not None
        assert restored_rec.filename == "sample.txt"

        db_restored.close()
        engine_restored.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_corrupted_backup_rejected():
    """Verifies that an archive with corrupted checksums or invalid SQLite DB is rejected."""
    temp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(temp_dir, "corrupt_test.db")
        uploads_dir = os.path.join(temp_dir, "uploads")
        backup_dir = os.path.join(temp_dir, "backups")
        os.makedirs(uploads_dir, exist_ok=True)

        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(bind=engine)
        engine.dispose()

        archive_path = create_backup(backup_dir=backup_dir, db_path=db_path, uploads_dir=uploads_dir)

        # Corrupt the archive by rewriting corrupted bytes into the zip header
        with open(archive_path, "r+b") as f:
            f.seek(0)
            f.write(b"CORRUPT_HEADER_BYTES_NOT_A_ZIP")

        res = verify_backup(archive_path)
        assert res["valid"] is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
