from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

DocumentStatusValue = Literal["processing", "completed", "failed"]


class DocumentSummary(BaseModel):
    id: UUID
    source_type: str
    source_uri: str
    title: str | None = None
    status: DocumentStatusValue
    chunk_count: int = Field(0, description="Number of chunks produced and embedded for this document.")
    created_at: datetime
