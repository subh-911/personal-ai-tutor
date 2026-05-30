from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db import async_session_maker
from app.models import Document, DocumentChunk

pytestmark = pytest.mark.usefixtures("db_clean")


async def test_upload_pdf_enqueues_embed_job(
    client: AsyncClient, sample_pdf_bytes: bytes, arq_pool_stub
) -> None:
    # Phase 10: the route's contract is "persist + enqueue". It returns 202
    # immediately with status="processing", stage="queued"; chunks land later
    # via the worker. Chunk-level assertions live in test_workers.py.
    files = {"file": ("sample.pdf", sample_pdf_bytes, "application/pdf")}
    response = await client.post("/ingest/upload", files=files, data={"title": "Phase 1 sample"})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "processing", body
    assert body["stage"] == "queued", body
    assert body["chunk_count"] == 0, body
    assert body["title"] == "Phase 1 sample"
    doc_id = body["id"]

    # The row is persisted before the route returns so the polling client has
    # something to GET against.
    async with async_session_maker() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        assert len(documents) == 1
        document = documents[0]
        assert str(document.id) == doc_id
        assert document.source_type == "upload"
        assert document.status == "processing"
        assert document.stage == "queued"
        assert document.title == "Phase 1 sample"
        assert document.doc_metadata.get("format") == "pdf"
        # No chunks yet — the worker is what writes them.
        chunks_present = await session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        assert chunks_present == 0

    # Worker enqueue happened exactly once with the parsed text payload.
    assert len(arq_pool_stub.calls) == 1
    call = arq_pool_stub.calls[0]
    assert call["function"] == "embed_document"
    assert call["kwargs"]["document_id"] == doc_id
    assert isinstance(call["kwargs"]["text"], str)
    assert len(call["kwargs"]["text"]) > 0


async def test_upload_rejects_unsupported_mime(client: AsyncClient) -> None:
    files = {"file": ("evil.bin", b"\x00\x01\x02", "application/octet-stream")}
    response = await client.post("/ingest/upload", files=files)
    assert response.status_code == 415, response.text


async def test_get_ingestion_status_returns_404_for_unknown_id(client: AsyncClient) -> None:
    response = await client.get("/ingest/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
