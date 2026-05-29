from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from app.agents.llm import LLMProvider
from app.agents.quiz import make_quiz_node
from app.agents.router import make_router_node
from app.agents.smalltalk import make_smalltalk_node
from app.agents.state import TutorState
from app.agents.tutor import make_tutor_node


def _route_dispatch(state: TutorState) -> str:
    route = state.get("route")
    return route if route in ("tutor", "quiz", "smalltalk") else "tutor"


def build_graph(llm: LLMProvider | None = None):
    g: StateGraph = StateGraph(TutorState)
    g.add_node("router", make_router_node(llm))
    g.add_node("tutor", make_tutor_node(llm))
    g.add_node("quiz", make_quiz_node(llm))
    g.add_node("smalltalk", make_smalltalk_node(llm))
    g.add_edge(START, "router")
    g.add_conditional_edges(
        "router",
        _route_dispatch,
        {"tutor": "tutor", "quiz": "quiz", "smalltalk": "smalltalk"},
    )
    g.add_edge("tutor", END)
    g.add_edge("quiz", END)
    g.add_edge("smalltalk", END)
    return g.compile()


graph = build_graph()


async def ainvoke_graph(
    user_text: str,
    *,
    history: list[BaseMessage] | None = None,
    user_score: int = 0,
    force_route: str | None = None,
) -> TutorState:
    initial: TutorState = {
        "messages": (history or []) + [HumanMessage(content=user_text)],
        "user_score": user_score,
        "context": [],
        "route": force_route,
        "response": None,
    }
    return await graph.ainvoke(initial)
