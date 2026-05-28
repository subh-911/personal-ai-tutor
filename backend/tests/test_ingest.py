from __future__ import annotations

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.db import async_session_maker
from app.models import Document, DocumentChunk

pytestmark = pytest.mark.usefixtures("db_clean")


async def test_upload_pdf_creates_chunks_with_embeddings(
    client: AsyncClient, sample_pdf_bytes: bytes
) -> None:
    files = {"file": ("sample.pdf", sample_pdf_bytes, "application/pdf")}
    response = await client.post("/ingest/upload", files=files, data={"title": "Phase 1 sample"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed", body
    assert body["chunk_count"] >= 1
    assert body["title"] == "Phase 1 sample"
    doc_id = body["id"]

    async with async_session_maker() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        assert len(documents) == 1
        document = documents[0]
        assert str(document.id) == doc_id
        assert document.source_type == "upload"
        assert document.status == "completed"
        assert document.title == "Phase 1 sample"
        assert document.doc_metadata.get("format") == "pdf"
        assert document.doc_metadata.get("page_count", 0) >= 1

        chunks = (
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document.id)
                    .order_by(DocumentChunk.chunk_index)
                )
            )
            .scalars()
            .all()
        )
        assert len(chunks) == body["chunk_count"]
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        assert all(c.content.strip() for c in chunks)
        assert all(c.token_count > 0 for c in chunks)

        first_embedding = np.asarray(chunks[0].embedding)
        assert first_embedding.shape == (settings.embedding_dim,)
        assert np.issubdtype(first_embedding.dtype, np.floating)
        assert np.linalg.norm(first_embedding) > 0  # not all zeros


async def test_upload_rejects_unsupported_mime(client: AsyncClient) -> None:
    files = {"file": ("evil.bin", b"\x00\x01\x02", "application/octet-stream")}
    response = await client.post("/ingest/upload", files=files)
    assert response.status_code == 415, response.text


async def test_get_ingestion_status_returns_404_for_unknown_id(client: AsyncClient) -> None:
    response = await client.get("/ingest/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
