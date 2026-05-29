from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_user_id
from app.db import get_session
from app.models import Document, DocumentChunk
from app.schemas.documents import DocumentSummary

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get(
    "",
    response_model=list[DocumentSummary],
    summary="List the caller's ingested documents",
    description=(
        "Returns documents owned by the verified caller, ordered by most recent first. "
        "Each row carries the chunk count so the admin UI can render it without an N+1 fetch. "
        "Legacy documents (uploaded before per-user ownership existed) carry `user_id IS NULL` "
        "and are not returned to any caller — they remain in the corpus for retrieval only."
    ),
)
async def list_documents(
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
) -> list[DocumentSummary]:
    chunk_count = func.count(DocumentChunk.id).label("chunk_count")
    stmt = (
        select(
            Document.id,
            Document.source_type,
            Document.source_uri,
            Document.title,
            Document.status,
            Document.created_at,
            chunk_count,
        )
        .join(DocumentChunk, DocumentChunk.document_id == Document.id, isouter=True)
        .where(Document.user_id == user_id)
        .group_by(Document.id)
        .order_by(Document.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        DocumentSummary(
            id=row.id,
            source_type=row.source_type,
            source_uri=row.source_uri,
            title=row.title,
            status=row.status,
            chunk_count=int(row.chunk_count or 0),
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document the caller owns",
    description=(
        "Deletes the document and (via `ON DELETE CASCADE`) all of its chunks. "
        "Returns 404 if no document with this id exists OR the caller does not own it — "
        "the response deliberately does not distinguish, to avoid leaking existence across users."
    ),
    responses={404: {"description": "No such document, or the caller does not own it."}},
)
async def delete_document(
    document_id: UUID,
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
) -> Response:
    result = await session.execute(
        delete(Document).where(Document.id == document_id, Document.user_id == user_id)
    )
    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
