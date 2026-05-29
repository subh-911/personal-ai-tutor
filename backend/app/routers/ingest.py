from __future__ import annotations

from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_user_id
from app.config import settings
from app.db import get_session
from app.models import Document, DocumentChunk
from app.schemas.ingest import IngestStatus, ScrapeRequest
from app.services.ingest import ingest_parsed
from app.services.parser import UnsupportedMediaError, fetch_url, parse_html, parse_upload

router = APIRouter(prefix="/ingest", tags=["ingest"])


async def _to_status(session: AsyncSession, document: Document) -> IngestStatus:
    chunk_count = await session.scalar(
        select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document.id)
    )
    return IngestStatus(
        id=document.id,
        status=document.status,  # type: ignore[arg-type]
        chunk_count=int(chunk_count or 0),
        title=document.title,
        error=document.error,
    )


@router.post(
    "/upload",
    response_model=IngestStatus,
    summary="Upload a file for ingestion",
    description=(
        "Accepts a `multipart/form-data` upload. Supported types: `application/pdf`, `text/plain`, "
        "`text/markdown` (also `.md` filename fallback). Parsing, chunking, and embedding run "
        "synchronously; the response carries the final status."
    ),
    responses={
        413: {"description": "Upload exceeds the configured size limit."},
        415: {"description": "Unsupported file type."},
    },
)
async def upload_document(
    file: UploadFile = File(..., description="Document to ingest."),
    title: str | None = Form(None, description="Optional human-readable title; defaults to the filename or PDF metadata."),
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
) -> IngestStatus:
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"upload exceeds {settings.max_upload_bytes} bytes")

    try:
        parsed = parse_upload(filename=file.filename or "", content_type=file.content_type, data=data)
    except UnsupportedMediaError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc

    document = await ingest_parsed(
        session,
        parsed,
        source_type="upload",
        source_uri=file.filename or "uploaded",
        title=title,
        user_id=user_id,
    )
    return await _to_status(session, document)


@router.post(
    "/scrape",
    response_model=IngestStatus,
    summary="Scrape a URL for ingestion",
    description=(
        "Fetches the seed URL, extracts text via BeautifulSoup, chunks, embeds, and persists. "
        "`max_depth` is reserved for future multi-page crawling and is ignored in phase 1."
    ),
)
async def scrape_url(
    payload: ScrapeRequest,
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
) -> IngestStatus:
    try:
        html, final_url = await fetch_url(str(payload.url))
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"failed to fetch URL: {exc}") from exc

    parsed = parse_html(html, base_url=final_url)
    document = await ingest_parsed(
        session,
        parsed,
        source_type="scrape",
        source_uri=final_url,
        title=None,
        user_id=user_id,
    )
    return await _to_status(session, document)


@router.get(
    "/{ingestion_id}",
    response_model=IngestStatus,
    summary="Poll ingestion status",
    responses={404: {"description": "No ingestion with this id."}},
)
async def get_ingestion_status(
    ingestion_id: UUID,
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
) -> IngestStatus:
    # Phase 9: status polls succeed for the caller's own ingests, plus legacy
    # rows uploaded before per-user ownership existed (`user_id IS NULL`). A
    # caller polling another user's owned doc id gets 404 — collapsing 403 into
    # 404 so existence is not leaked across users.
    document = await session.scalar(
        select(Document).where(
            Document.id == ingestion_id,
            (Document.user_id == user_id) | (Document.user_id.is_(None)),
        )
    )
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ingestion not found")
    return await _to_status(session, document)
