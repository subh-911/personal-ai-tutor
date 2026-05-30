from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk
from app.services.chunker import chunk_text
from app.services.embeddings import get_embedding_provider
from app.services.parser import ParsedDocument

log = logging.getLogger(__name__)

SourceType = Literal["upload", "scrape"]


class IngestionError(RuntimeError):
    pass


async def create_pending_document(
    session: AsyncSession,
    parsed: ParsedDocument,
    *,
    source_type: SourceType,
    source_uri: str,
    title: str | None,
    user_id: str | None = None,
) -> Document:
    """Phase 10 request-path half: persist the Document row in `processing/queued`
    state and commit before the worker picks the job up. The route returns this
    row's id immediately; the worker advances `stage` afterward.
    """
    document = Document(
        user_id=user_id,
        source_type=source_type,
        source_uri=source_uri,
        title=title or parsed.title,
        status="processing",
        stage="queued",
        doc_metadata=parsed.metadata,
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)
    return document


async def embed_pending_document(
    session: AsyncSession,
    document_id: UUID,
    text: str,
) -> Document:
    """Phase 10 worker-path half: chunk, embed, persist, marking stage transitions
    along the way so a poller sees live progress. Caller owns the session — the
    worker constructs its own; callers using the legacy `ingest_parsed` wrapper
    share theirs. Each stage transition commits independently.
    """
    document = await session.get(Document, document_id)
    if document is None:
        raise IngestionError(f"document {document_id} not found")

    try:
        document.stage = "chunking"
        await session.commit()

        chunks = chunk_text(text)
        if not chunks:
            raise IngestionError("no chunks produced from source (empty document?)")

        document.stage = "embedding"
        await session.commit()

        provider = get_embedding_provider()
        vectors = await provider.embed_batch([c.content for c in chunks])

        document.stage = "persisting"
        await session.commit()

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
        document.stage = "completed"
        await session.commit()
        await session.refresh(document)
        return document

    except Exception as exc:
        log.exception("ingestion failed for document %s", document_id)
        await session.rollback()
        failed = await session.get(Document, document_id)
        # The row was committed by `create_pending_document`, so it must still
        # exist on this rollback path; the defensive recreate branch is gone.
        assert failed is not None, "document row vanished mid-ingest"
        failed.status = "failed"
        failed.stage = "failed"
        failed.error = str(exc)
        await session.commit()
        await session.refresh(failed)
        return failed


async def ingest_parsed(
    session: AsyncSession,
    parsed: ParsedDocument,
    *,
    source_type: SourceType,
    source_uri: str,
    title: str | None,
    user_id: str | None = None,
) -> Document:
    """Backward-compat wrapper: runs the request-path persist and the worker-path
    embed back-to-back in the caller's session. Used by tests and any direct
    sync caller that doesn't want to involve the queue.
    """
    document = await create_pending_document(
        session,
        parsed,
        source_type=source_type,
        source_uri=source_uri,
        title=title,
        user_id=user_id,
    )
    return await embed_pending_document(session, document.id, parsed.text)
