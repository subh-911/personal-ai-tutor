import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agents.graph import graph
from app.auth import get_user_id
from app.config import settings
from app.schemas.chat import ChatMessage, ChatRequest
from app.services.session import SessionStore, get_session_store

router = APIRouter(tags=["chat"])


SSE_DESCRIPTION = """
Streaming chat endpoint backed by the LangGraph orchestrator (Router → Tutor | Quiz)
and a Redis-backed session memory.

**Request body**: `{"message": "<user turn>", "session_id": "<uuid>" (optional), "force_route": "tutor"|"quiz" (optional)}`.

**Session lifecycle**:
- If `session_id` is omitted, the server mints a fresh UUID and returns it in the
  `X-Session-Id` response header. Clients echo this id on follow-up requests to
  maintain history.
- Server keeps the last 10 turn-pairs (20 messages) per session in Redis with a
  rolling 30-day TTL; older messages fall off via `LTRIM`.

**Response**: `text/event-stream` carrying **per-token** Gemini deltas:

```
data: {"delta": "Hel"}

data: {"delta": "lo"}

data: {"delta": " world"}

data: [DONE]
```

Tokens are streamed live from `gemini-2.5-flash` via LangGraph's
`astream_events(version="v2")`. Only chunks produced by the `tutor`, `quiz`, and
`smalltalk` nodes are forwarded; the Router's classifier output is filtered out
so its single-word route decision never leaks into the assistant response.
"""


def _to_lc_messages(messages: list[ChatMessage]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in messages:
        if m.role == "user":
            out.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
        else:
            out.append(SystemMessage(content=m.content))
    return out


async def _token_stream(
    *,
    request: ChatRequest,
    user_id: str,
    session_id: UUID,
    store: SessionStore,
) -> AsyncIterator[bytes]:
    history = await store.get_history(user_id, session_id)
    lc_history = _to_lc_messages(history)

    initial_state = {
        "messages": lc_history + [HumanMessage(content=request.message)],
        "user_score": 0,
        "context": [],
        "route": request.force_route,
        "response": None,
    }

    response_buf: list[str] = []
    error_message: str | None = None
    try:
        async for event in graph.astream_events(initial_state, version="v2"):
            if event.get("event") != "on_chat_model_stream":
                continue
            metadata = event.get("metadata") or {}
            node = metadata.get("langgraph_node")
            if node not in ("tutor", "quiz", "smalltalk"):
                continue
            chunk = (event.get("data") or {}).get("chunk")
            text = getattr(chunk, "content", None) if chunk is not None else None
            if not text:
                continue
            response_buf.append(text)
            yield f"data: {json.dumps({'delta': text})}\n\n".encode()
    except Exception as exc:
        log.exception("chat stream failed mid-generation")
        # Distill provider errors into something a human can read in the UI.
        detail = _summarise_error(exc)
        error_message = f"\n\n⚠️ **{detail}**"
        yield f"data: {json.dumps({'delta': error_message})}\n\n".encode()
    finally:
        full = "".join(response_buf)
        if error_message:
            full = (full + error_message).strip()
        if full:
            await store.append_turn(
                user_id,
                session_id,
                user_msg=ChatMessage(role="user", content=request.message),
                assistant_msg=ChatMessage(role="assistant", content=full),
            )
        yield b"data: [DONE]\n\n"


def _summarise_error(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()
    if "resource_exhausted" in low or "quota" in low or "429" in msg:
        # The Gemini free tier caps `gemini-2.5-flash` AND `gemini-2.5-flash-lite`
        # at 20 requests/day each. Each chat turn costs at least 2 requests
        # (router classifier + node), so the 20/day budget covers ~10 turns.
        model = settings.gemini_model_name
        return (
            f"The Gemini API is rate-limiting requests for model `{model}` "
            "(free tier: 20 requests / day, per-minute caps too). Options: wait for "
            "the daily window to reset (resets midnight Pacific), enable billing on "
            "the Google AI Studio project for the same key, or set a different "
            "GOOGLE_API_KEY in `.env` from a separate AI Studio project to get a "
            "fresh daily quota."
        )
    if "api key" in low or "permission" in low or "401" in msg or "403" in msg:
        return "The LLM rejected the request (auth/permission). Check GOOGLE_API_KEY in .env."
    if "timeout" in low or "timed out" in low:
        return "The LLM call timed out. Try again, or switch to a smaller model."
    # Default: include the exception type + a short head of the message.
    head = msg.splitlines()[0][:240] if msg else "(no detail)"
    return f"Backend error ({name}): {head}"


@router.post(
    "/chat",
    summary="Streaming chat completion (token-level, session-aware)",
    description=SSE_DESCRIPTION,
    responses={
        200: {
            "description": "Server-Sent Events stream of Gemini token deltas. Response carries `X-Session-Id`.",
            "content": {"text/event-stream": {}},
            "headers": {
                "X-Session-Id": {
                    "description": "UUID of the conversation session — store and echo on follow-up requests.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            },
        }
    },
)
async def chat(
    request: ChatRequest,
    user_id: str = Depends(get_user_id),
) -> StreamingResponse:
    store = get_session_store()
    session_id = request.session_id or uuid4()

    return StreamingResponse(
        _token_stream(request=request, user_id=user_id, session_id=session_id, store=store),
        media_type="text/event-stream",
        headers={
            "X-Session-Id": str(session_id),
            "Cache-Control": "no-cache",
        },
    )
