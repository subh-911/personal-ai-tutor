from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://tutor:tutor@localhost:5432/tutor"
    redis_url: str = "redis://localhost:6379/0"

    embedding_dim: int = 768
    embedding_model_name: str = "sentence-transformers/all-mpnet-base-v2"
    max_upload_bytes: int = 25 * 1024 * 1024
    scrape_timeout_seconds: float = 30.0

    google_api_key: str | None = None
    gemini_model_name: str = "gemini-2.5-flash-lite"

    session_history_turns: int = 10
    session_ttl_seconds: int = 30 * 24 * 3600


settings = Settings()
