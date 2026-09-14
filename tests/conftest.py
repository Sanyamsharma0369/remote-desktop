"""
tests/conftest.py — Shared pytest fixtures.
"""
import os

# ── MUST be set before ANY app module is imported ────────────────────────────
os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "test_secret_key_that_is_long_enough_32chars!!"
os.environ["ALLOWED_ORIGINS"] = "http://localhost:9005"
os.environ["ALLOWED_WS_ORIGINS"] = "http://localhost:9005"
os.environ["COOKIE_SECURE"] = "false"
os.environ["DATABASE_URL"] = "sqlite://"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── Create an in-memory test engine and patch app.core.database ──────────────
import app.core.database as _db_module
from sqlalchemy.pool import StaticPool

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # single shared connection for in-memory SQLite
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Patch the module-level engine and sessionmaker BEFORE Base.metadata operations
_db_module.engine = test_engine
_db_module.SessionLocal = TestingSessionLocal

# Now import Base (which is tied to the patched engine via get_db)
from app.core.database import Base, get_db

# Create all tables in the in-memory DB
Base.metadata.create_all(bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Import app AFTER patching ────────────────────────────────────────────────
from app.main import app

app.dependency_overrides[get_db] = override_get_db

# ── Disable rate limiting in tests ──────────────────────────────────────────
from app.routers import auth as auth_module

_original_limit = auth_module.limiter.limit

def _noop_limit(*args, **kwargs):
    def decorator(func):
        return func
    return decorator

auth_module.limiter.limit = _noop_limit


@pytest.fixture(autouse=True)
def reset_db():
    """Drop and recreate all tables between each test for isolation."""
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield


@pytest.fixture
def db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def admin_user(db):
    from app.models.user import User
    from app.routers.auth import get_password_hash
    user = User(
        username="admin",
        password_hash=get_password_hash("AdminPass123!"),
        role="admin",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def regular_user(db):
    from app.models.user import User
    from app.routers.auth import get_password_hash
    user = User(
        username="user1",
        password_hash=get_password_hash("UserPass123!"),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def admin_token(admin_user):
    """Issue a JWT directly — avoids the rate-limited /api/auth/login endpoint."""
    from app.routers.auth import create_access_token
    return create_access_token({"sub": admin_user.username})


@pytest.fixture
def user_token(regular_user):
    from app.routers.auth import create_access_token
    return create_access_token({"sub": regular_user.username})
