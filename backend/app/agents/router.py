from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.agents.llm import LLMProvider, get_llm_provider
from app.agents.state import TutorState


def make_router_node(llm: LLMProvider | None = None):
    provider = llm or get_llm_provider()

    async def router_node(state: TutorState) -> TutorState:
        if state.get("route") in ("tutor", "quiz", "smalltalk"):
            return {}
        last_user = next(
            (m for m in reversed(state.get("messages", [])) if isinstance(m, HumanMessage)),
            None,
        )
        text = last_user.content if last_user else ""
        route = await provider.classify(str(text))
        return {"route": route}

    return router_node
