"""add stage column to documents

Revision ID: 0003_add_document_stage
Revises: 0002_add_user_id_to_documents
Create Date: 2026-05-30

Phase 10 introduces stage-level ingestion progress. The ARQ worker writes the
stage on each transition (queued → chunking → embedding → persisting →
completed/failed); the UI polls `/ingest/{id}` and renders the value as a live
progress label. The existing `status` enum (processing/completed/failed) is
unchanged; `stage` is purely additive and nullable for backward compat with rows
ingested before this column existed.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_add_document_stage"
down_revision: Union[str, None] = "0002_add_user_id_to_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("stage", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "stage")
