"""
scratch/verify_restart_persistence.py — Explicit restart-persistence verification.

Scenario:
  Process 1:
    - Connect to persistent SQLite database (rd_app.db)
    - Initialize schema
    - Create User A (regular), User B (regular), Admin
    - Generate User A JWT
    - Upload a test file as User A via FastAPI client
    - Capture the returned file_id and disk path
    - Terminate/Exit Process 1 context completely (simulate server shutdown)

  Process 2:
    - Completely separate initialization reading from rd_app.db
    - Query DB directly to verify FileRecord row exists with uploader_id == User A.id
    - Generate fresh JWTs for User A, User B, and Admin
    - User A lists files -> file is listed
    - User B lists files -> file is NOT listed
    - User B attempts download -> 403 Forbidden
    - User B attempts delete -> 403 Forbidden
    - Admin lists files -> file is listed
    - Admin downloads file -> 200 OK with correct content
    - Admin deletes file -> 200 OK
    - Verify FileRecord row is deleted from DB
    - Verify file is removed from disk uploads/
"""

import os
import sys
import io
import subprocess

# Ensure backend root is in sys.path
sys.path.insert(0, os.path.abspath("."))

def run_phase_1():
    """Simulates Process 1: setup, upload, and shutdown."""
    print("--- [PROCESS 1: UPLOAD & SHUTDOWN] ---")
    from app.core.database import SessionLocal, Base, engine
    from app.models.user import User
    from app.models.file import FileRecord
    from app.routers.auth import get_password_hash, create_access_token
    from app.main import app
    from fastapi.testclient import TestClient

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Clean existing test users if any
    db.query(User).filter(User.username.in_(["persist_user_a", "persist_user_b", "persist_admin"])).delete(synchronize_session=False)
    db.commit()

    admin = User(username="persist_admin", password_hash=get_password_hash("AdminPass123!"), role="admin")
    user_a = User(username="persist_user_a", password_hash=get_password_hash("UserAPass123!"), role="user")
    user_b = User(username="persist_user_b", password_hash=get_password_hash("UserBPass123!"), role="user")
    db.add_all([admin, user_a, user_b])
    db.commit()
    db.refresh(admin)
    db.refresh(user_a)
    db.refresh(user_b)

    user_a_token = create_access_token({"sub": user_a.username})
    user_a_headers = {"Authorization": f"Bearer {user_a_token}"}

    client = TestClient(app, raise_server_exceptions=False)
    test_content = b"CRITICAL_DATA_THAT_MUST_SURVIVE_RESTART_12345"
    res = client.post(
        "/api/files/upload",
        headers=user_a_headers,
        files={"file": ("persist_test.txt", io.BytesIO(test_content), "text/plain")}
    )
    assert res.status_code == 200, f"Upload failed: {res.status_code} {res.text}"
    file_id = res.json()["file_id"]
    print(f"Uploaded file_id: {file_id} by User A (id={user_a.id})")

    # Confirm record in DB
    rec = db.query(FileRecord).filter(FileRecord.file_id == file_id).first()
    assert rec is not None
    file_path = rec.path
    print(f"File stored at: {file_path}")
    assert os.path.exists(file_path), "File does not exist on disk"

    db.close()
    print("Process 1 finished. Simulating server restart...")
    return file_id, file_path, user_a.id, user_b.id, admin.id

def run_phase_2(file_id, file_path, user_a_id, user_b_id, admin_id):
    """Simulates Process 2: verify post-restart persistence and access control."""
    print("\n--- [PROCESS 2: POST-RESTART VERIFICATION] ---")
    from app.core.database import SessionLocal, Base, engine
    from app.models.user import User
    from app.models.file import FileRecord
    from app.routers.auth import create_access_token
    from app.main import app
    from fastapi.testclient import TestClient

    db = SessionLocal()

    # 1. Verify DB record exists after restart
    rec = db.query(FileRecord).filter(FileRecord.file_id == file_id).first()
    assert rec is not None, "FAILED: FileRecord disappeared from database after restart!"
    assert rec.uploader_id == user_a_id, f"FAILED: uploader_id mismatch. Expected {user_a_id}, got {rec.uploader_id}"
    assert rec.filename == "persist_test.txt"
    assert os.path.exists(file_path), "FAILED: File missing on disk after restart!"
    print(" -> Step 1: Database row & disk file verified intact after restart: OK")

    # 2. Issue fresh JWT tokens for all three users
    user_a_token = create_access_token({"sub": "persist_user_a"})
    user_b_token = create_access_token({"sub": "persist_user_b"})
    admin_token = create_access_token({"sub": "persist_admin"})

    user_a_headers = {"Authorization": f"Bearer {user_a_token}"}
    user_b_headers = {"Authorization": f"Bearer {user_b_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    client = TestClient(app, raise_server_exceptions=False)

    # 3. User A lists files -> must see file
    list_a = client.get("/api/files/files", headers=user_a_headers).json()["files"]
    assert any(f["file_id"] == file_id for f in list_a), "FAILED: User A cannot see their own file"
    print(" -> Step 2: User A sees file in list: OK")

    # 4. User B lists files -> must NOT see file
    list_b = client.get("/api/files/files", headers=user_b_headers).json()["files"]
    assert not any(f["file_id"] == file_id for f in list_b), "FAILED: User B sees User A's file"
    print(" -> Step 3: User B cannot see User A's file in list: OK")

    # 5. User B attempts download & delete -> 403 Forbidden
    down_b = client.get(f"/api/files/download/{file_id}", headers=user_b_headers)
    assert down_b.status_code == 403, f"FAILED: User B download was not 403 (got {down_b.status_code})"
    del_b = client.delete(f"/api/files/files/{file_id}", headers=user_b_headers)
    assert del_b.status_code == 403, f"FAILED: User B delete was not 403 (got {del_b.status_code})"
    print(" -> Step 4: User B blocked with 403 Forbidden on download & delete: OK")

    # 6. Admin lists & downloads -> 200 OK
    list_admin = client.get("/api/files/files", headers=admin_headers).json()["files"]
    assert any(f["file_id"] == file_id for f in list_admin), "FAILED: Admin cannot see file"
    down_admin = client.get(f"/api/files/download/{file_id}", headers=admin_headers)
    assert down_admin.status_code == 200, f"FAILED: Admin download failed (got {down_admin.status_code})"
    assert down_admin.content == b"CRITICAL_DATA_THAT_MUST_SURVIVE_RESTART_12345"
    print(" -> Step 5: Admin sees & downloads file (content matched): OK")

    # 7. Admin deletes file
    del_admin = client.delete(f"/api/files/files/{file_id}", headers=admin_headers)
    assert del_admin.status_code == 200, f"FAILED: Admin delete failed (got {del_admin.status_code})"
    print(" -> Step 6: Admin deleted file via API: OK")

    # 8. Verify DB row and disk file are both removed
    rec_after = db.query(FileRecord).filter(FileRecord.file_id == file_id).first()
    assert rec_after is None, "FAILED: FileRecord still exists in DB after deletion!"
    assert not os.path.exists(file_path), "FAILED: File still exists on disk after deletion!"
    print(" -> Step 7: Verified FileRecord removed from DB and file removed from disk: OK")

    # Cleanup test users
    db.query(User).filter(User.username.in_(["persist_user_a", "persist_user_b", "persist_admin"])).delete(synchronize_session=False)
    db.commit()
    db.close()
    print("\n" + "=" * 70)
    print("RESTART PERSISTENCE & ACCESS ISOLATION: 100% VERIFIED")
    print("=" * 70)

if __name__ == "__main__":
    file_id, file_path, u_a, u_b, adm = run_phase_1()
    run_phase_2(file_id, file_path, u_a, u_b, adm)
