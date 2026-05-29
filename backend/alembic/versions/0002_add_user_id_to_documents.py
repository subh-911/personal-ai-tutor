"""add user_id to documents

Revision ID: 0002_add_user_id_to_documents
Revises: 0001_baseline
Create Date: 2026-05-29

Phase 9 introduces per-user ownership of ingested documents. Existing rows are
left with `user_id = NULL` (legacy / unowned) — they remain in the corpus for
retrieval but are invisible to the per-user admin list and undeletable via the
UI. Operators can claim or drop them with a manual SQL statement; see README.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_add_user_id_to_documents"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("user_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_column("documents", "user_id")
