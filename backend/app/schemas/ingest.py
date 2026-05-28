from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl


class ScrapeRequest(BaseModel):
    url: HttpUrl = Field(..., description="URL to scrape and ingest as a document source.")
    max_depth: int = Field(
        1,
        ge=0,
        le=5,
        description="Reserved for future multi-page crawling. Ignored in phase 1 — only the seed URL is fetched.",
    )


IngestStatusValue = Literal["processing", "completed", "failed"]


class IngestStatus(BaseModel):
    id: UUID
    status: IngestStatusValue
    chunk_count: int = Field(0, description="Number of chunks produced and embedded for this document.")
    title: str | None = None
    error: str | None = None
