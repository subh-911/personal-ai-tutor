from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import engine
from app.redis_client import redis as redis_client
from app.routers import chat, documents, health, ingest


API_DESCRIPTION = """
Backend for the **Personal AI Tutor**.

- `health`    — service liveness + dependency check.
- `ingest`    — submit documents (upload or scrape) and poll ingestion status.
- `documents` — list and delete the caller's ingested documents.
- `chat`      — token-by-token Server-Sent Events stream.
"""

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness probe and dependency checks."},
    {"name": "ingest", "description": "Document ingestion — file upload and URL scraping."},
    {"name": "documents", "description": "Per-user knowledge base — list and delete."},
    {"name": "chat", "description": "Streaming chat completions (Server-Sent Events)."},
]


# Schema is owned by Alembic from phase 7 onwards. Run `alembic upgrade head`
# before booting the API against a fresh database. The lifespan no longer
# touches DDL.
@asynccontextmanager
async def lifespan(app: FastAPI):
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
app.include_router(documents.router)
app.include_router(chat.router)
