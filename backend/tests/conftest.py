import os
from collections.abc import AsyncIterator
from io import BytesIO
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fpdf import FPDF
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db import async_session_maker, engine
from app.main import app
from app.models import Base, Document, DocumentChunk
from app.redis_client import redis as redis_client
from app.services.embeddings import get_embedding_provider
from app.services.session import SESSION_KEY_PREFIX


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_llm: test crosses the LLM boundary (Gemini) and is skipped when "
        "GOOGLE_API_KEY is unset.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("GOOGLE_API_KEY"):
        return
    skip_llm = pytest.mark.skip(reason="GOOGLE_API_KEY not set; skipping LLM-bound test")
    for item in items:
        if "requires_llm" in item.keywords:
            item.add_marker(skip_llm)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _create_schema() -> AsyncIterator[None]:
    from app.main import HNSW_INDEX_SQL

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(HNSW_INDEX_SQL)
    yield


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _hf_warmup(_create_schema) -> AsyncIterator[None]:
    # Pre-loads the sentence-transformers model into the singleton so individual
    # tests don't pay the first-call download cost. No-op on subsequent runs (the
    # model lives in ~/.cache/huggingface/).
    await get_embedding_provider().embed_batch(["warmup"])
    yield


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_clean() -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE documents, document_chunks RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture
async def redis_clean() -> AsyncIterator[None]:
    # Phase 6: chat keys are namespaced under chat:user:. Defensively sweep the
    # old phase-3 chat:session:* prefix too in case any stale fixture data sits there.
    patterns = (f"{SESSION_KEY_PREFIX}*", "chat:session:*")
    for pattern in patterns:
        async for key in redis_client.scan_iter(match=pattern):
            await redis_client.delete(key)
    yield
    for pattern in patterns:
        async for key in redis_client.scan_iter(match=pattern):
            await redis_client.delete(key)


SAMPLE_PARAGRAPHS = [
    (
        "The Indian Constitution, adopted on 26 November 1949 and effective from 26 January 1950, "
        "establishes a parliamentary form of government with a federal structure that carries strong "
        "unitary features. The Preamble declares India a sovereign, socialist, secular, democratic "
        "republic, and the basic structure doctrine articulated in Kesavananda Bharati (1973) limits "
        "Parliament's amending power over its core features."
    ),
    (
        "Fundamental Rights enumerated in Part III are justiciable, while the Directive Principles of "
        "State Policy in Part IV are non-justiciable but fundamental to governance. The relationship "
        "between the two has evolved through cases such as Champakam Dorairajan, Golak Nath, and "
        "Minerva Mills, culminating in a harmonious-construction approach."
    ),
    (
        "In distributed systems, the CAP theorem states that any networked shared-data system can "
        "provide at most two of the following three guarantees: consistency, availability, and "
        "partition tolerance. Real systems must choose between AP and CP behaviours during partitions, "
        "and the PACELC extension further notes that even in the absence of partitions there is a "
        "trade-off between latency and consistency."
    ),
    (
        "Consensus protocols such as Paxos and Raft provide replicated state machines with strong "
        "consistency under crash-stop failures. Raft simplifies Paxos by separating leader election, "
        "log replication, and safety, making it easier to implement correctly. Production systems like "
        "etcd and Consul build on Raft to coordinate cluster state."
    ),
    (
        "Eventual consistency models, by contrast, prioritise availability and partition tolerance, "
        "accepting temporary divergence between replicas in exchange for low write latency. Conflict-free "
        "replicated data types (CRDTs) and vector clocks are common tools for reconciling concurrent "
        "updates without coordination."
    ),
    (
        "The interplay between governance design and distributed systems is more than incidental: both "
        "domains grapple with how to reach durable agreement among partially-trusting actors under "
        "uncertainty. Studying them side by side surfaces the trade-offs between centralised "
        "decisiveness and decentralised resilience."
    ),
]


@pytest.fixture(scope="session")
def sample_pdf_bytes() -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, "Phase 1 sample document")
    pdf.ln(10)
    pdf.set_font("Helvetica", size=11)
    for paragraph in SAMPLE_PARAGRAPHS:
        pdf.multi_cell(0, 6, paragraph)
        pdf.ln(3)
    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()


SEED_CHUNK_TEXTS = [
    "The CAP theorem states that a networked shared-data system can provide at most two of "
    "consistency, availability, and partition tolerance simultaneously.",
    "Raft is a consensus algorithm that separates leader election, log replication, and "
    "safety to coordinate replicated state machines under crash-stop failures.",
    "Conflict-free replicated data types (CRDTs) reconcile concurrent updates across "
    "replicas without coordination, enabling eventual consistency.",
]


@pytest_asyncio.fixture
async def seeded_chunks(db_clean) -> dict[str, UUID | list[UUID]]:
    embedder = get_embedding_provider()
    vectors = await embedder.embed_batch(SEED_CHUNK_TEXTS)

    document_id = uuid4()
    chunk_ids: list[UUID] = []

    async with async_session_maker() as session:
        document = Document(
            id=document_id,
            source_type="upload",
            source_uri="phase2-seed.txt",
            title="Phase 2 seed corpus",
            status="completed",
            doc_metadata={"format": "text", "seeded": True},
        )
        session.add(document)
        await session.flush()

        for i, (chunk_text, vec) in enumerate(zip(SEED_CHUNK_TEXTS, vectors, strict=True)):
            chunk = DocumentChunk(
                document_id=document_id,
                chunk_index=i,
                content=chunk_text,
                token_count=len(chunk_text.split()),
                embedding=vec,
            )
            session.add(chunk)
            await session.flush()
            chunk_ids.append(chunk.id)

        await session.commit()

    return {"document_id": document_id, "chunk_ids": chunk_ids}


# Re-export to silence "unused import" warnings while keeping the imports visible above.
__all__ = [
    "client",
    "db_clean",
    "redis_clean",
    "sample_pdf_bytes",
    "seeded_chunks",
    "async_session_maker",
]
