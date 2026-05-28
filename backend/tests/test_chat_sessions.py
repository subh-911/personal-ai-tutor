from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.auth import ANONYMOUS_USER_ID
from app.config import settings
from app.redis_client import redis as redis_client
from app.schemas.chat import ChatMessage
from app.services.session import SESSION_KEY_PREFIX, get_session_store


pytestmark = [pytest.mark.usefixtures("redis_clean"), pytest.mark.requires_llm]


def _session_key(session_id: str | UUID, user_id: UUID = ANONYMOUS_USER_ID) -> str:
    return f"{SESSION_KEY_PREFIX}{user_id}:session:{session_id}:messages"


async def _consume_sse(response) -> tuple[list[str], list[str]]:
    """Read an SSE stream into (delta payloads, raw data lines)."""
    deltas: list[str] = []
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        data_lines.append(payload)
        if payload == "[DONE]":
            continue
        deltas.append(json.loads(payload)["delta"])
    return deltas, data_lines


async def test_chat_stream_returns_multiple_chunks_and_caches_session(client: AsyncClient) -> None:
    async with client.stream(
        "POST", "/chat", json={"message": "Explain CAP theorem."}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        session_id = response.headers["x-session-id"]
        UUID(session_id)  # must be a valid UUID

        deltas, data_lines = await _consume_sse(response)

    assert len(deltas) >= 2, f"expected multiple delta events, got {len(deltas)}"
    assert data_lines[-1] == "[DONE]"

    streamed_text = "".join(deltas)

    items = await redis_client.lrange(_session_key(session_id), 0, -1)
    assert len(items) == 2, f"expected user + assistant in Redis, got {len(items)}"

    user_msg = ChatMessage.model_validate_json(items[0])
    assistant_msg = ChatMessage.model_validate_json(items[1])
    assert user_msg.role == "user"
    assert user_msg.content == "Explain CAP theorem."
    assert assistant_msg.role == "assistant"
    assert assistant_msg.content.strip() == streamed_text.strip()

    ttl = await redis_client.ttl(_session_key(session_id))
    assert 0 < ttl <= settings.session_ttl_seconds


async def test_chat_resumes_history_from_redis(client: AsyncClient) -> None:
    session_id = uuid4()
    store = get_session_store()
    await store.append_turn(
        ANONYMOUS_USER_ID,
        session_id,
        user_msg=ChatMessage(role="user", content="Hi, I want to learn Raft."),
        assistant_msg=ChatMessage(role="assistant", content="Great — let's begin with leader election."),
    )

    async with client.stream(
        "POST",
        "/chat",
        json={"session_id": str(session_id), "message": "Quiz me on Raft."},
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-session-id"] == str(session_id)
        deltas, _ = await _consume_sse(response)

    streamed = "".join(deltas)
    assert "Question:" in streamed, "quiz request should route through Quiz agent"

    items = await redis_client.lrange(_session_key(session_id), 0, -1)
    assert len(items) == 4, "pre-seeded turn + new turn should give 4 messages"

    seeded_user = ChatMessage.model_validate_json(items[0])
    seeded_assistant = ChatMessage.model_validate_json(items[1])
    new_user = ChatMessage.model_validate_json(items[2])
    new_assistant = ChatMessage.model_validate_json(items[3])

    assert seeded_user.content == "Hi, I want to learn Raft."
    assert seeded_assistant.content.startswith("Great")
    assert new_user.content == "Quiz me on Raft."
    assert "Question:" in new_assistant.content


async def test_chat_trims_to_max_messages(client: AsyncClient) -> None:
    session_id = uuid4()
    key = _session_key(session_id)

    # Pre-seed 22 messages (2 more than the 20-message cap).
    seeded = [
        ChatMessage(
            role=("user" if i % 2 == 0 else "assistant"),
            content=f"seeded-{i}",
        ).model_dump_json()
        for i in range(22)
    ]
    await redis_client.rpush(key, *seeded)
    await redis_client.expire(key, settings.session_ttl_seconds)

    async with client.stream(
        "POST",
        "/chat",
        json={"session_id": str(session_id), "message": "Tell me about CRDTs."},
    ) as response:
        assert response.status_code == 200
        # Drain so the request completes and the route's append/trim runs.
        await _consume_sse(response)

    items = await redis_client.lrange(key, 0, -1)
    max_messages = settings.session_history_turns * 2
    assert len(items) == max_messages, f"LTRIM should cap at {max_messages}, got {len(items)}"

    # Oldest pre-seeded messages should have fallen off the front.
    first = ChatMessage.model_validate_json(items[0])
    assert first.content != "seeded-0", "the oldest pre-seed should have been trimmed"

    # The last two entries are the new user + assistant turns we just sent.
    assert ChatMessage.model_validate_json(items[-2]).content == "Tell me about CRDTs."
    assert ChatMessage.model_validate_json(items[-1]).role == "assistant"


async def test_chat_assigns_session_id_when_omitted(client: AsyncClient) -> None:
    async with client.stream("POST", "/chat", json={"message": "Explain Raft."}) as r1:
        sid1 = r1.headers["x-session-id"]
        await _consume_sse(r1)

    async with client.stream("POST", "/chat", json={"message": "Explain CAP theorem."}) as r2:
        sid2 = r2.headers["x-session-id"]
        await _consume_sse(r2)

    UUID(sid1)
    UUID(sid2)
    assert sid1 != sid2, "each request without a session_id must get a fresh one"


async def test_chat_isolates_history_between_users(client: AsyncClient) -> None:
    """Two callers sharing the same `session_id` but presenting different Bearer
    tokens must NOT see each other's history. Proves the user-scoping in the
    Redis key actually scopes.
    """
    shared_session = uuid4()
    user_a = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    user_b = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    async with client.stream(
        "POST",
        "/chat",
        headers={"Authorization": f"Bearer {user_a}"},
        json={"session_id": str(shared_session), "message": "Tell me about Raft."},
    ) as response:
        assert response.status_code == 200
        await _consume_sse(response)

    async with client.stream(
        "POST",
        "/chat",
        headers={"Authorization": f"Bearer {user_b}"},
        json={"session_id": str(shared_session), "message": "Tell me about CAP."},
    ) as response:
        assert response.status_code == 200
        await _consume_sse(response)

    items_a = await redis_client.lrange(_session_key(shared_session, user_id=user_a), 0, -1)
    items_b = await redis_client.lrange(_session_key(shared_session, user_id=user_b), 0, -1)

    assert len(items_a) == 2, "user A should own exactly their own user+assistant pair"
    assert len(items_b) == 2, "user B should own exactly their own user+assistant pair"

    user_a_msg = ChatMessage.model_validate_json(items_a[0])
    user_b_msg = ChatMessage.model_validate_json(items_b[0])
    assert user_a_msg.content == "Tell me about Raft."
    assert user_b_msg.content == "Tell me about CAP."

    # The anonymous fallback key MUST stay empty — neither call lacked a header.
    anon_items = await redis_client.lrange(_session_key(shared_session), 0, -1)
    assert anon_items == []
