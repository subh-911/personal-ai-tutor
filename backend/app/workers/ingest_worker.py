from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Request

from app.config import settings
from app.db import async_session_maker
from app.services.ingest import embed_pending_document

log = logging.getLogger(__name__)


async def embed_document(ctx: dict[str, Any], *, document_id: str, text: str) -> None:
    """ARQ job: pick up a Document row already persisted in `queued` state, run
    chunking + embedding + persistence, and write stage transitions back to the
    DB so the polling client sees live progress.

    Idempotency: with `max_tries=1` (see WorkerSettings) we do not retry
    automatically. If the process dies mid-run, the row is left at whatever
    stage was last committed; a re-upload is the recovery path.
    """
    doc_uuid = UUID(document_id)
    log.info("embed_document started", extra={"document_id": document_id})
    async with async_session_maker() as session:
        await embed_pending_document(session, doc_uuid, text)
    log.info("embed_document completed", extra={"document_id": document_id})


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.arq_redis_url)


class WorkerSettings:
    """Entry point for the `arq` CLI.

    Run with: `uv run arq app.workers.ingest_worker.WorkerSettings`
    """

    functions = [embed_document]
    redis_settings = _redis_settings()
    max_tries = 1
    # How long completed job results sit in Redis before expiring. We don't
    # surface job results to clients (status lives in Postgres), so this is
    # essentially just a debugging convenience.
    keep_result = 300


async def get_arq_pool() -> ArqRedis:
    """Open a fresh ARQ pool. Called from the FastAPI lifespan on startup."""
    return await create_pool(_redis_settings())


def get_arq_pool_dep(request: Request) -> ArqRedis:
    """FastAPI dep: hand the request-scoped route the shared pool stored on
    `app.state.arq_pool`. Tests override this to inject a stub queue.
    """
    return request.app.state.arq_pool
