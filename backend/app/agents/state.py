from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.services.retrieval import RetrievedChunk


Route = Literal["tutor", "quiz"]


class TutorState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    user_score: int
    context: list[RetrievedChunk]
    route: Route | None
    response: str | None
