from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocumentChunk
from app.services.embeddings import EmbeddingProvider, get_embedding_provider


class RetrievedChunk(BaseModel):
    chunk_id: UUID
    document_id: UUID
    content: str
    similarity: float


async def retrieve_top_k(
    session: AsyncSession,
    query: str,
    *,
    k: int = 4,
    embedder: EmbeddingProvider | None = None,
) -> list[RetrievedChunk]:
    embedder = embedder or get_embedding_provider()
    vec = (await embedder.embed_batch([query]))[0]

    distance = DocumentChunk.embedding.cosine_distance(vec).label("distance")
    stmt = select(DocumentChunk, distance).order_by(distance).limit(k)
    rows = (await session.execute(stmt)).all()

    return [
        RetrievedChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            content=chunk.content,
            similarity=1.0 - float(dist),
        )
        for chunk, dist in rows
    ]
