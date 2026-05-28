from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        description="The new user turn. Server-managed history is fetched from Redis using `session_id`.",
    )
    session_id: UUID | None = Field(
        None,
        description=(
            "Optional session identifier. If omitted, the server mints a new one and returns "
            "it via the `X-Session-Id` response header; clients should echo it on follow-up "
            "requests to maintain conversation memory."
        ),
    )
    force_route: Literal["tutor", "quiz"] | None = Field(
        None,
        description=(
            "If set, bypass the Router's LLM classification and dispatch directly to the named "
            "agent. Use `tutor` to force an explanation/example, `quiz` to force a knowledge check."
        ),
    )
