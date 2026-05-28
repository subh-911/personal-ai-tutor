from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.llm import get_chat_model
from app.agents.state import TutorState
from app.db import async_session_maker
from app.services.retrieval import RetrievedChunk, retrieve_top_k


TUTOR_SYSTEM_TEMPLATE = (
    "You are an expert AI tutor. Answer the student's question using ONLY the numbered "
    "context snippets below. Follow these rules strictly:\n"
    "  1. If the context does not contain enough information to answer, reply exactly:\n"
    '     "I don\'t have enough information to answer that based on the available material."\n'
    "  2. Always cite the snippet number(s) you used in square brackets, e.g. \"[1]\" or "
    "\"[2, 3]\".\n"
    "  3. Do not invent facts or use outside knowledge. Do not speculate.\n"
    "  4. Format the answer in clean Markdown — use headings, bullet lists, and fenced "
    "code blocks where they aid clarity.\n\n"
    "Context:\n{context}"
)


def _format_context(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(no context retrieved)"
    return "\n".join(f"[{i}] {c.content}" for i, c in enumerate(chunks, start=1))


def make_tutor_node(_unused=None, *, k: int = 4):
    async def tutor_node(state: TutorState) -> TutorState:
        messages = state.get("messages", [])
        last_user = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)),
            None,
        )
        query = str(last_user.content) if last_user else ""

        async with async_session_maker() as session:
            chunks = await retrieve_top_k(session, query, k=k)

        system_prompt = TUTOR_SYSTEM_TEMPLATE.format(context=_format_context(chunks))
        chat = get_chat_model()

        # astream() makes the chunks visible to graph.astream_events(version="v2")
        # so the /chat route can forward each token to the client as SSE.
        response_buf: list[str] = []
        async for piece in chat.astream([SystemMessage(content=system_prompt), *messages]):
            text = getattr(piece, "content", "") or ""
            if text:
                response_buf.append(text)

        response = "".join(response_buf)
        return {
            "context": chunks,
            "response": response,
            "messages": [AIMessage(content=response)],
        }

    return tutor_node
