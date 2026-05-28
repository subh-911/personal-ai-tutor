from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.db import engine
from app.models import Base
from app.redis_client import redis as redis_client
from app.routers import chat, health, ingest


# Idempotent ANN index on the embedding column. `Base.metadata.create_all` only
# materialises indexes when it creates a new table, so for already-existing
# `document_chunks` (the common case after phase 5) we need to issue the DDL
# directly. CREATE INDEX IF NOT EXISTS makes this safe to run on every boot.
HNSW_INDEX_SQL = text(
    "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw "
    "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)"
)

API_DESCRIPTION = """
Backend for the **Personal AI Tutor**.

- `health` — service liveness + dependency check.
- `ingest` — submit documents (upload or scrape) and poll ingestion status.
- `chat`   — token-by-token Server-Sent Events stream.
"""

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness probe and dependency checks."},
    {"name": "ingest", "description": "Document ingestion — file upload and URL scraping."},
    {"name": "chat", "description": "Streaming chat completions (Server-Sent Events)."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(HNSW_INDEX_SQL)
    yield
    await engine.dispose()
    await redis_client.aclose()


app = FastAPI(
    title="Personal AI Tutor API",
    version="0.1.0",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(chat.router)
