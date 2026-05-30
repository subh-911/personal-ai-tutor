from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db import async_session_maker
from app.models import Document, DocumentChunk
from app.services.ingest import create_pending_document
from app.services.parser import ParsedDocument
from app.workers.ingest_worker import embed_document

pytestmark = pytest.mark.usefixtures("db_clean")


SAMPLE_TEXT = (
    "The CAP theorem states that any networked shared-data system can provide "
    "at most two of three guarantees: consistency, availability, and partition "
    "tolerance. Real systems must choose between AP and CP behaviours during "
    "partitions. The PACELC extension further notes that even in the absence "
    "of partitions there is a trade-off between latency and consistency, "
    "which guides the design of replication protocols.\n\n"
    "Consensus protocols such as Paxos and Raft provide replicated state "
    "machines with strong consistency under crash-stop failures. Raft "
    "simplifies Paxos by separating leader election, log replication, and "
    "safety, making it easier to implement correctly."
)


async def _seed_pending(session_maker=async_session_maker) -> str:
    """Persist a queued Document the way the route would, returning its id."""
    parsed = ParsedDocument(
        text=SAMPLE_TEXT,
        title="distributed systems primer",
        metadata={"format": "text", "test": True},
    )
    async with session_maker() as session:
        document = await create_pending_document(
            session,
            parsed,
            source_type="upload",
            source_uri="test.txt",
            title="distributed systems primer",
            user_id="test_anon",
        )
        return str(document.id)


async def test_embed_document_writes_chunks_and_marks_completed() -> None:
    document_id = await _seed_pending()

    await embed_document({}, document_id=document_id, text=SAMPLE_TEXT)

    async with async_session_maker() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        assert document.status == "completed"
        assert document.stage == "completed"
        assert document.error is None

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
        assert len(chunks) >= 1
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        first_embedding = np.asarray(chunks[0].embedding)
        assert first_embedding.shape == (settings.embedding_dim,)
        assert np.linalg.norm(first_embedding) > 0


async def test_embed_document_marks_failed_on_provider_error(monkeypatch) -> None:
    document_id = await _seed_pending()

    class _RaisingProvider:
        dimension = settings.embedding_dim

        async def embed_batch(self, texts):  # noqa: ARG002
            raise RuntimeError("simulated embedding failure")

    # The worker's embed_pending_document calls `get_embedding_provider()` from
    # within `app.services.ingest`; patch *that* binding so the worker sees the
    # raising stub.
    monkeypatch.setattr(
        "app.services.ingest.get_embedding_provider",
        lambda: _RaisingProvider(),
    )

    await embed_document({}, document_id=document_id, text=SAMPLE_TEXT)

    async with async_session_maker() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        assert document.status == "failed"
        assert document.stage == "failed"
        assert "simulated embedding failure" in (document.error or "")

        chunks_present = await session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        assert chunks_present == 0, "failed ingest must not leave partial chunks behind"


async def test_embed_document_advances_stage_before_embedding(monkeypatch) -> None:
    """Proves the pollable contract: stage is committed *before* the embedding
    step runs, so a client polling /ingest/{id} sees `embedding` while the
    encode is in flight (not just `chunking` followed by `completed`).
    """
    document_id = await _seed_pending()
    observed_stage_during_embed: list[str | None] = []

    class _StageObservingProvider:
        dimension = settings.embedding_dim

        async def embed_batch(self, texts):
            async with async_session_maker() as session:
                document = await session.get(Document, document_id)
                observed_stage_during_embed.append(document.stage if document else None)
            # Return zero vectors of the right dim so the rest of the pipeline succeeds.
            return [[0.0] * settings.embedding_dim for _ in texts]

    monkeypatch.setattr(
        "app.services.ingest.get_embedding_provider",
        lambda: _StageObservingProvider(),
    )

    await embed_document({}, document_id=document_id, text=SAMPLE_TEXT)

    assert observed_stage_during_embed == ["embedding"], (
        "worker must commit stage='embedding' before invoking the embedder, "
        f"observed: {observed_stage_during_embed}"
    )

    async with async_session_maker() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        assert document.status == "completed"
        assert document.stage == "completed"
