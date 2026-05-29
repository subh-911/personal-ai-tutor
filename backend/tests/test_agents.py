from __future__ import annotations

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import func, select

from app.agents.graph import ainvoke_graph
from app.db import async_session_maker
from app.models import DocumentChunk


pytestmark = [pytest.mark.usefixtures("db_clean"), pytest.mark.requires_llm]


async def test_router_sends_concept_question_to_tutor() -> None:
    state = await ainvoke_graph("Explain the CAP theorem in distributed systems.")

    assert state["route"] == "tutor"
    assert state["response"], "tutor must produce a response"
    assert state["response"].startswith("[stub-tutor]")
    # The last message in the accumulated history is the assistant turn.
    assert isinstance(state["messages"][-1], AIMessage)


async def test_router_sends_quiz_request_to_quiz() -> None:
    state = await ainvoke_graph("Quiz me on Raft consensus.")

    assert state["route"] == "quiz"
    response = state["response"] or ""
    assert "Question:" in response
    for letter in ("A)", "B)", "C)", "D)"):
        assert letter in response, f"quiz response missing option {letter!r}"


async def test_router_sends_greeting_to_smalltalk() -> None:
    state = await ainvoke_graph("Hi")

    assert state["route"] == "smalltalk"
    response = (state["response"] or "").strip()
    assert response, "smalltalk node must produce a response"
    # The Tutor's strict-grounding refusal sentence must NOT appear here —
    # that wording is reserved for substantive un-grounded questions.
    assert "I don't have enough information" not in response


async def test_smalltalk_skips_retrieval(seeded_chunks) -> None:
    # The seeded corpus would let the Tutor retrieve real chunks; smalltalk should
    # not invoke retrieval at all. We verify by checking that `state["context"]`
    # remains empty after a greeting, even with retrievable material in the DB.
    state = await ainvoke_graph("Hello there!")

    assert state["route"] == "smalltalk"
    assert state.get("context") == [], "smalltalk node must not invoke retrieval"


async def test_tutor_retrieves_from_pgvector(seeded_chunks) -> None:
    state = await ainvoke_graph("Explain the CAP theorem and partition tolerance.")

    assert state["route"] == "tutor"
    context = state["context"]
    assert len(context) >= 1, "tutor must retrieve at least one chunk"

    seeded_doc_id = seeded_chunks["document_id"]
    for chunk in context:
        assert chunk.document_id == seeded_doc_id, (
            "retrieved chunk does not belong to the seeded document — retrieval may be looking elsewhere"
        )
        assert isinstance(chunk.similarity, float)
        assert chunk.content.strip()

    # Independent sanity check: the DB really has the chunks we think it does.
    async with async_session_maker() as session:
        count = await session.scalar(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == seeded_doc_id
            )
        )
    assert count == len(seeded_chunks["chunk_ids"]) == 3


async def test_user_score_round_trips_unchanged() -> None:
    state = await ainvoke_graph("Explain Raft.", user_score=7)
    # Phase 2 contract: score is threaded but not mutated by any node.
    assert state["user_score"] == 7


async def test_history_accumulates_assistant_message() -> None:
    history = [
        HumanMessage(content="Hi, I want to learn distributed systems."),
        AIMessage(content="Great — where shall we start?"),
    ]
    state = await ainvoke_graph("Explain Raft.", history=history)

    messages = state["messages"]
    # 2 from history + 1 new HumanMessage + 1 new AIMessage from tutor.
    assert len(messages) == 4
    assert isinstance(messages[-2], HumanMessage)
    assert messages[-2].content == "Explain Raft."
    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == state["response"]


async def test_force_route_tutor_skips_router_classifier() -> None:
    # "Hello" alone wouldn't naturally route to quiz; force_route=tutor is the positive control.
    state = await ainvoke_graph("Hello", force_route="tutor")
    assert state["route"] == "tutor"
    assert state["response"], "tutor should have produced a response"
    assert state["response"].startswith("[stub-tutor]")


async def test_force_route_quiz_overrides_classifier() -> None:
    # "Explain CAP theorem" would route to tutor by default; force_route=quiz must override.
    state = await ainvoke_graph("Explain CAP theorem", force_route="quiz")
    assert state["route"] == "quiz"
    response = state["response"] or ""
    assert "Question:" in response, "force_route=quiz should bypass the LLM classifier"


async def test_chat_route_runs_graph_end_to_end(client: AsyncClient, redis_clean) -> None:
    response = await client.post(
        "/chat",
        json={"message": "Explain Raft consensus."},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "X-Session-Id" in response.headers or "x-session-id" in response.headers

    body = response.text
    assert "data: " in body
    assert "\"delta\"" in body
    assert body.rstrip().endswith("data: [DONE]")
