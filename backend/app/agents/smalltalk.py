from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.llm import get_chat_model
from app.agents.state import TutorState


SMALLTALK_SYSTEM = (
    "You are a friendly AI tutor. The student has sent a conversational message "
    "(a greeting, thanks, goodbye, or other small talk) — not a substantive question.\n\n"
    "Reply briefly and warmly (1–2 sentences). If the message is a greeting or "
    "first contact, mention what you can help with in this conversation: explain "
    "concepts from documents the student has uploaded, generate quiz questions "
    "from that material, and point them to the Admin page if they want to add "
    "more sources.\n\n"
    "Hard rules:\n"
    "  - Do NOT invent facts.\n"
    "  - Do NOT reference any specific document content (you have no retrieval "
    "context here — substantive questions go to the Tutor node, not this one).\n"
    "  - Keep your reply under 60 words.\n"
    "  - Do NOT begin with the literal phrase \"I don't have enough information\" — "
    "that wording is reserved for substantive un-grounded questions, not for "
    "small talk."
)


def make_smalltalk_node(_unused=None):
    async def smalltalk_node(state: TutorState) -> TutorState:
        # Pass the full message history so the model can be context-aware
        # ("thank you" after a tutor answer should acknowledge naturally),
        # but skip retrieval entirely — small talk has no grounding need.
        messages = state.get("messages", [])
        chat = get_chat_model()

        response_buf: list[str] = []
        async for piece in chat.astream([SystemMessage(content=SMALLTALK_SYSTEM), *messages]):
            text = getattr(piece, "content", "") or ""
            if text:
                response_buf.append(text)

        response = "".join(response_buf)
        return {
            "response": response,
            "messages": [AIMessage(content=response)],
        }

    return smalltalk_node
