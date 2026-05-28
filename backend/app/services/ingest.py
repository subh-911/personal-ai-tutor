from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk
from app.services.chunker import chunk_text
from app.services.embeddings import get_embedding_provider
from app.services.parser import ParsedDocument

log = logging.getLogger(__name__)

SourceType = Literal["upload", "scrape"]


class IngestionError(RuntimeError):
    pass


async def ingest_parsed(
    session: AsyncSession,
    parsed: ParsedDocument,
    *,
    source_type: SourceType,
    source_uri: str,
    title: str | None,
) -> Document:
    document = Document(
        source_type=source_type,
        source_uri=source_uri,
        title=title or parsed.title,
        status="processing",
        doc_metadata=parsed.metadata,
    )
    session.add(document)
    await session.flush()

    try:
        chunks = chunk_text(parsed.text)
        if not chunks:
            raise IngestionError("no chunks produced from source (empty document?)")

        provider = get_embedding_provider()
        vectors = await provider.embed_batch([c.content for c in chunks])

        session.add_all(
            DocumentChunk(
                document_id=document.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        document.status = "completed"
        await session.commit()
        await session.refresh(document)
        return document

    except Exception as exc:
        log.exception("ingestion failed for document %s", document.id)
        await session.rollback()
        # Re-attach a fresh failed-status row so the caller can still poll status.
        failed = await session.get(Document, document.id)
        if failed is None:
            failed = Document(
                id=document.id,
                source_type=source_type,
                source_uri=source_uri,
                title=title or parsed.title,
                status="failed",
                error=str(exc),
                doc_metadata=parsed.metadata,
            )
            session.add(failed)
        else:
            failed.status = "failed"
            failed.error = str(exc)
        await session.commit()
        await session.refresh(failed)
        return failed
