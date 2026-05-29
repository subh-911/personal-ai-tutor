from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor env_file to the project root so values load regardless of which
# directory the process is started from (e.g. `cd backend && uvicorn ...`).
# This file lives at backend/app/config.py — project root is two parents up.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_PROJECT_ROOT / ".env"), extra="ignore")

    database_url: str = "postgresql+asyncpg://tutor:tutor@localhost:5432/tutor"
    redis_url: str = "redis://localhost:6380/0"

    embedding_dim: int = 768
    embedding_model_name: str = "sentence-transformers/all-mpnet-base-v2"
    max_upload_bytes: int = 25 * 1024 * 1024
    scrape_timeout_seconds: float = 30.0

    google_api_key: str | None = None
    gemini_model_name: str = "gemini-2.5-flash-lite"

    session_history_turns: int = 10
    session_ttl_seconds: int = 30 * 24 * 3600

    # Phase 8 — IdP integration. JWKS is fetched lazily on first auth'd request
    # and cached for the process lifetime. Issuer should be the Clerk frontend
    # API URL (the same domain JWKS is hosted under).
    clerk_jwks_url: str | None = None
    clerk_issuer: str | None = None

    # Dev-only escape hatch: when set, `get_user_id` falls back to the pre-phase-8
    # permissive Bearer parser (any string accepted, no signature verification).
    # NEVER enable in production. The app logs a warning on boot when this is on.
    dev_auth_bypass: bool = False


settings = Settings()
