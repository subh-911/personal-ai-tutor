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

# Phase 10 — finer-grained pipeline stage. `queued` is written by the route
# right after the Document row is created and before the ARQ worker picks it
# up; the worker advances the stage on each transition. `completed`/`failed`
# mirror the terminal `status` values for UI convenience.
IngestStage = Literal[
    "queued", "chunking", "embedding", "persisting", "completed", "failed"
]


class IngestStatus(BaseModel):
    id: UUID
    status: IngestStatusValue
    stage: IngestStage | None = Field(
        None,
        description=(
            "Live pipeline stage. Set to `queued` immediately on enqueue and "
            "advanced by the ARQ worker on each transition. Null for rows "
            "ingested before phase 10."
        ),
    )
    chunk_count: int = Field(0, description="Number of chunks produced and embedded for this document.")
    title: str | None = None
    error: str | None = None
