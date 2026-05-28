from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.llm import get_chat_model
from app.agents.state import TutorState
from app.db import async_session_maker
from app.services.retrieval import RetrievedChunk, retrieve_top_k


QUIZ_SYSTEM_TEMPLATE = (
    "You are a quiz designer. Generate EXACTLY ONE multiple-choice question grounded in "
    "the context snippets below.\n\n"
    "Output format (use EXACTLY, no extra prose, no leading blank line):\n"
    "Question: <one-sentence question>\n"
    "A) <option A>\n"
    "B) <option B>\n"
    "C) <option C>\n"
    "D) <option D>\n"
    "Answer: <single letter A-D>\n"
    "Explanation: <one sentence>\n\n"
    "Rules:\n"
    "  1. Exactly one option is correct; the other three are plausible distractors.\n"
    "  2. The question must test understanding, not memorisation.\n"
    "  3. Use only information that appears in the context. If the context is empty, "
    "     produce a generic question about the requested topic and flag in the "
    "     Explanation that no grounded material was available.\n\n"
    "Context:\n{context}"
)


def _format_context(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(no context retrieved)"
    return "\n".join(f"[{i}] {c.content}" for i, c in enumerate(chunks, start=1))


def make_quiz_node(_unused=None, *, k: int = 2):
    async def quiz_node(state: TutorState) -> TutorState:
        messages = state.get("messages", [])
        last_user = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)),
            None,
        )
        topic = str(last_user.content) if last_user else ""

        async with async_session_maker() as session:
            chunks = await retrieve_top_k(session, topic, k=k)

        system_prompt = QUIZ_SYSTEM_TEMPLATE.format(context=_format_context(chunks))
        chat = get_chat_model()

        response_buf: list[str] = []
        async for piece in chat.astream(
            [SystemMessage(content=system_prompt), HumanMessage(content=f"Quiz me on: {topic}")]
        ):
            text = getattr(piece, "content", "") or ""
            if text:
                response_buf.append(text)

        response = "".join(response_buf)
        return {
            "context": chunks,
            "response": response,
            "messages": [AIMessage(content=response)],
        }

    return quiz_node
