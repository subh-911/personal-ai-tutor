"""Phase 9: per-user CRUD over the documents table.

The `_override_auth` autouse fixture (conftest.py) installs a permissive
`get_user_id` override that treats the Bearer value as the caller's id. To
simulate cross-user isolation we just send different Bearer strings.

To exercise the real (un-overridden) auth gate for the unauth-401 test we
temporarily pop the override and put it back via a fixture.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.auth import get_user_id
from app.db import async_session_maker
from app.main import app
from app.models import Document, DocumentChunk
from app.services.embeddings import get_embedding_provider

pytestmark = pytest.mark.usefixtures("db_clean")


async def _seed_doc(*, user_id: str | None, content: str, title: str) -> str:
    embedder = get_embedding_provider()
    [vec] = await embedder.embed_batch([content])
    async with async_session_maker() as session:
        doc = Document(
            user_id=user_id,
            source_type="upload",
            source_uri=f"{title}.txt",
            title=title,
            status="completed",
            doc_metadata={"seeded": True},
        )
        session.add(doc)
        await session.flush()
        session.add(
            DocumentChunk(
                document_id=doc.id,
                chunk_index=0,
                content=content,
                token_count=len(content.split()),
                embedding=vec,
            )
        )
        await session.commit()
        return str(doc.id)


async def test_unauthenticated_list_returns_401(client: AsyncClient) -> None:
    # Temporarily lift the dependency override so the real `get_user_id` runs.
    override = app.dependency_overrides.pop(get_user_id)
    try:
        response = await client.get("/documents")
    finally:
        app.dependency_overrides[get_user_id] = override
    assert response.status_code == 401, response.text


async def test_list_isolates_documents_by_owner(client: AsyncClient) -> None:
    alice_id = await _seed_doc(user_id="user_alice", content="alice's notes", title="Alice doc")
    await _seed_doc(user_id="user_bob", content="bob's notes", title="Bob doc")
    await _seed_doc(user_id=None, content="legacy unowned", title="Legacy doc")

    response = await client.get("/documents", headers={"Authorization": "Bearer user_alice"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1, body
    assert body[0]["id"] == alice_id
    assert body[0]["title"] == "Alice doc"
    assert body[0]["chunk_count"] == 1


async def test_delete_own_document_cascades_chunks(client: AsyncClient) -> None:
    doc_id = await _seed_doc(user_id="user_alice", content="alice's notes", title="Alice doc")

    response = await client.delete(
        f"/documents/{doc_id}", headers={"Authorization": "Bearer user_alice"}
    )
    assert response.status_code == 204, response.text

    async with async_session_maker() as session:
        doc = await session.get(Document, doc_id)
        assert doc is None
        # CASCADE check: chunks for the deleted doc must be gone too.
        chunk_rows = (
            await session.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == doc_id)
            )
        ).all()
        assert chunk_rows == []


async def test_delete_other_users_document_returns_404(client: AsyncClient) -> None:
    alice_doc_id = await _seed_doc(
        user_id="user_alice", content="alice's notes", title="Alice doc"
    )
    response = await client.delete(
        f"/documents/{alice_doc_id}",
        headers={"Authorization": "Bearer user_bob"},
    )
    assert response.status_code == 404, response.text

    # Confirm the row + its chunk are still there.
    async with async_session_maker() as session:
        doc = await session.get(Document, alice_doc_id)
        assert doc is not None
        assert doc.user_id == "user_alice"


async def test_delete_nonexistent_document_returns_404(client: AsyncClient) -> None:
    response = await client.delete(
        f"/documents/{uuid4()}", headers={"Authorization": "Bearer user_alice"}
    )
    assert response.status_code == 404, response.text
