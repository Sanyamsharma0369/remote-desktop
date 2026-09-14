"""
app/core/config.py — Application settings with production-safety validation.
"""
from __future__ import annotations
from functools import lru_cache
from typing import List, Optional
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    # ── Server ────────────────────────────────────────────────────────────
    APP_ENV: str = "development"
    HOST: str = "0.0.0.0"
    PORT: int = 9005
    DEBUG: bool = False

    # ── Origins ───────────────────────────────────────────────────────────
    # Comma-separated lists, parsed by the validator below
    PUBLIC_ORIGIN: str = ""
    ALLOWED_ORIGINS: str = "http://localhost:9005,http://127.0.0.1:9005"
    ALLOWED_WS_ORIGINS: str = "http://localhost:9005,http://127.0.0.1:9005"

    # ── Auth / JWT ────────────────────────────────────────────────────────
    SECRET_KEY: str = "CHANGE_ME_generate_with_secrets_token_hex_32"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Refresh tokens ────────────────────────────────────────────────────
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── Cookie options ────────────────────────────────────────────────────
    COOKIE_SECURE: bool = False      # Must be True when behind HTTPS
    COOKIE_SAMESITE: str = "lax"
    REFRESH_COOKIE_PATH: str = "/api/auth/refresh"

    # ── Session / control limits ──────────────────────────────────────────
    CONTROL_SESSION_EXPIRE_MINUTES: int = 10
    WEBSOCKET_TICKET_EXPIRE_SECONDS: int = 60
    MAX_CONCURRENT_CONTROL_SESSIONS_PER_DEVICE: int = 1

    # ── WebSocket message limits ──────────────────────────────────────────
    MAX_WS_MESSAGE_BYTES: int = 65_536
    MAX_MOUSE_EVENTS_PER_SECOND: int = 60
    MAX_CONTROL_EVENTS_PER_SECOND: int = 120

    # ── Audit ─────────────────────────────────────────────────────────────
    AUDIT_LOG_RETENTION_DAYS: int = 90

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "sqlite:///./rd_app.db"

    # ── Streaming ─────────────────────────────────────────────────────────
    CAPTURE_FPS: int = 30
    MONITOR_INDEX: int = 1
    ENCODER: str = "auto"

    # ── Keyboard security flags ───────────────────────────────────────────
    ALLOW_ALT_F4: bool = False
    ALLOW_CTRL_SHIFT_ESC: bool = False

    # ── Initial admin bootstrap ───────────────────────────────────────────
    # Set this env var ONCE to seed the first admin account.
    # Once any admin exists in the database, this setting is ignored.
    # NEVER set to "admin123" or any weak value.
    INITIAL_ADMIN_PASSWORD: Optional[str] = None


    # ── Derived helpers (populated by validators) ─────────────────────────
    allowed_origins_list: List[str] = []
    allowed_ws_origins_list: List[str] = []

    # ─────────────────────────────────────────────────────────────────────
    @field_validator("ALLOWED_ORIGINS", "ALLOWED_WS_ORIGINS", mode="before")
    @classmethod
    def strip_origins(cls, v: str) -> str:
        # Normalize whitespace; actual parsing done in model_validator
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def parse_and_validate(self) -> "Settings":
        # Parse comma-separated origin strings into lists
        self.allowed_origins_list = [
            o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()
        ]
        self.allowed_ws_origins_list = [
            o.strip() for o in self.ALLOWED_WS_ORIGINS.split(",") if o.strip()
        ]

        if self.is_production:
            self._validate_production()

        return self

    def _validate_production(self) -> None:
        """Raise ValueError for any obviously insecure production configuration."""
        errors: list[str] = []

        # Weak / default secret key check
        weak_secrets = {
            "CHANGE_ME_generate_with_secrets_token_hex_32",
            "changeme", "secret", "your_secret_here", "",
        }
        if self.SECRET_KEY in weak_secrets or len(self.SECRET_KEY) < 32:
            errors.append(
                "SECRET_KEY is weak or default. Generate with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )

        # Production must use HTTPS
        if self.PUBLIC_ORIGIN and not self.PUBLIC_ORIGIN.startswith("https://"):
            errors.append("PUBLIC_ORIGIN must use https:// in production.")

        # Secure cookies required over HTTPS
        if not self.COOKIE_SECURE:
            errors.append("COOKIE_SECURE must be true in production (requires HTTPS).")

        # No wildcard CORS
        if "*" in self.ALLOWED_ORIGINS:
            errors.append("ALLOWED_ORIGINS must not contain '*' in production.")

        # Origins should match PUBLIC_ORIGIN in production
        if self.PUBLIC_ORIGIN:
            for origin in self.allowed_origins_list:
                if "localhost" in origin or "127.0.0.1" in origin:
                    errors.append(
                        f"ALLOWED_ORIGINS contains local address '{origin}' "
                        "which is unsafe for public production deployment."
                    )

        if errors:
            raise ValueError(
                "Production configuration errors:\n" +
                "\n".join(f"  • {e}" for e in errors)
            )

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
