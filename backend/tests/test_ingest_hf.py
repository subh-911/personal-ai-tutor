"""Phase-5 integration test for the brain transplant: prove that ingestion now uses
the real sentence-transformers `all-mpnet-base-v2` model and persists 768-d vectors
to Postgres. Does NOT depend on Gemini (no LLM contact) so it runs without an API key.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import settings
from app.db import async_session_maker
from app.models import Document, DocumentChunk
from app.services.embeddings import HuggingFaceEmbeddingProvider, get_embedding_provider
from app.workers.ingest_worker import embed_document


pytestmark = pytest.mark.usefixtures("db_clean")


# Synthesised daily-news-analysis content in the editorial style of The Hindu /
# The Indian Express. Intentionally written here (not committed as a fixture) so
# the test is self-contained and reproducible.
NEWS_ANALYSIS_TEXT = """\
Daily News Analysis — 28 May 2026

Editorial: The Reserve Bank of India's latest Monetary Policy Committee minutes
reveal a sharper internal debate over the trajectory of the repo rate than the
six-to-zero headline vote suggested. Members flagged sticky food inflation, a
softening rural demand picture, and the lagged impact of rate cuts as competing
concerns that will shape the August review.

International Relations: The Quad foreign ministers' meeting in New Delhi
reaffirmed the grouping's emphasis on a free and open Indo-Pacific, with a
specific work-stream on critical and emerging technologies. Analysts note that
the inclusion of secure undersea-cable infrastructure marks an expansion of the
grouping's traditional maritime-security focus into digital-resilience terrain.

Polity: A Constitution Bench of the Supreme Court reserved judgment on petitions
challenging the constitutional validity of the Electoral Bond Scheme's amended
rules. The petitioners argued that the scheme's opacity violates the voter's
right to information under Article 19(1)(a) — a doctrinal thread running back to
Union of India v. Association for Democratic Reforms (2002).

Economy: India's Q4 FY26 GDP print of 7.4% exceeded street expectations of 7.1%,
driven largely by gross fixed capital formation and a recovery in private final
consumption expenditure. Net exports continued to drag, reflecting persistent
weakness in the West Asian and European markets.

Science & Tech: ISRO confirmed that the SSLV-D6 mission successfully placed two
earth-observation satellites in their target sun-synchronous orbits. The launch
brings the SSLV programme's success rate above the threshold required for
transferring the platform to private industry consortia, a transition first
flagged in the FY24 Union Budget.
"""


def _hash_vector(text: str, dim: int) -> list[float]:
    """The exact deterministic-hash recipe the phase-1 stub used. Re-implemented
    here purely so this test can prove the *stored* vector differs from what the
    stub would have produced for the same text — i.e. the real HF model is in play.
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = np.random.default_rng(seed)
    vec = rng.uniform(-1.0, 1.0, size=dim).astype(np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


async def test_real_hf_model_used_for_ingested_document(
    client: AsyncClient, arq_pool_stub
) -> None:
    files = {
        "file": (
            "news-analysis-2026-05-28.txt",
            NEWS_ANALYSIS_TEXT.encode("utf-8"),
            "text/plain",
        ),
    }
    response = await client.post(
        "/ingest/upload",
        files=files,
        data={"title": "Daily News Analysis — 28 May 2026"},
    )

    # Phase 10: the route hands off to the ARQ worker. Drive the worker inline
    # here to keep the brain-transplant assertion (real HF model, not stub-hash)
    # working end-to-end without standing up a real worker process in tests.
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "processing", body
    assert body["stage"] == "queued", body

    assert len(arq_pool_stub.calls) == 1
    call = arq_pool_stub.calls[0]
    assert call["function"] == "embed_document"
    await embed_document(
        {},
        document_id=call["kwargs"]["document_id"],
        text=call["kwargs"]["text"],
    )

    async with async_session_maker() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        assert len(documents) == 1
        document = documents[0]
        assert document.source_type == "upload"
        assert document.title == "Daily News Analysis — 28 May 2026"
        assert document.doc_metadata.get("format") == "text"

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
        assert chunks, "no chunks persisted"

        first = chunks[0]
        assert first.content.strip()
        embedding = np.asarray(first.embedding, dtype=np.float64)
        assert embedding.shape == (settings.embedding_dim,) == (768,)
        norm = float(np.linalg.norm(embedding))
        assert norm > 0.0
        # Real all-mpnet-base-v2 vectors are normalised; check we're close to unit norm.
        assert 0.9 < norm < 1.1, f"expected ~unit-norm vector, got |v| = {norm}"

        # Prove the brain transplant landed: a real semantic embedding is NOT the
        # deterministic hash vector that the phase-1 stub would have produced.
        stub_vec = np.asarray(_hash_vector(first.content, settings.embedding_dim))
        assert not np.allclose(embedding, stub_vec, atol=1e-4), (
            "stored vector matches the deterministic hash of the chunk text — "
            "the embedding provider is still the phase-1 stub"
        )


async def test_hf_embeddings_carry_semantic_structure() -> None:
    """Direct check on the embedder: semantically related sentences should sit closer
    in cosine space than unrelated ones. Threshold is loose (>0.4) because we're proving
    the provider does *some* semantic clustering, not benchmarking the model.
    """
    provider = get_embedding_provider()
    assert isinstance(provider, HuggingFaceEmbeddingProvider)

    a, b, c = await provider.embed_batch(
        [
            "The Reserve Bank of India held the repo rate at 6.5% on inflation concerns.",
            "RBI kept its policy rate unchanged at 6.5 percent because of sticky inflation.",
            "Mango is the national fruit of India.",
        ]
    )
    va, vb, vc = np.asarray(a), np.asarray(b), np.asarray(c)

    def cos(x: np.ndarray, y: np.ndarray) -> float:
        return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))

    sim_related = cos(va, vb)
    sim_unrelated = cos(va, vc)

    assert sim_related > 0.4, f"related sentences too far apart: cos={sim_related:.3f}"
    assert sim_related > sim_unrelated + 0.1, (
        f"related ({sim_related:.3f}) should be measurably closer than unrelated "
        f"({sim_unrelated:.3f})"
    )
