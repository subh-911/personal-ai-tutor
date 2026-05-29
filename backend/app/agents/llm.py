from __future__ import annotations

from threading import Lock
from typing import Literal, Protocol

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import settings
from app.services.retrieval import RetrievedChunk


Route = Literal["tutor", "quiz", "smalltalk"]


CLASSIFIER_SYSTEM = (
    "You are a routing classifier for an AI tutor. Decide which category best fits the "
    "student's message:\n"
    "  - SMALLTALK: greetings, thanks, goodbyes, or other conversational messages that "
    "do not ask for any subject-matter content (e.g. \"hi\", \"hello\", \"thank you\", "
    "\"good morning\", \"how are you\").\n"
    "  - QUIZ: an explicit request for a knowledge check / quiz / test / MCQ.\n"
    "  - TUTOR: anything else — questions, requests for explanations, summaries, examples, "
    "or definitions of substantive material.\n"
    "Reply with exactly one word, uppercase: SMALLTALK, QUIZ, or TUTOR. No punctuation, "
    "no explanation."
)


class LLMProvider(Protocol):
    async def classify(self, text: str) -> Route: ...
    async def complete(self, messages: list[BaseMessage], *, system: str | None = None) -> str: ...
    async def quiz(self, *, topic: str, context: list[RetrievedChunk]) -> str: ...


def _build_quiz_prompt(topic: str, context: list[RetrievedChunk]) -> str:
    ctx = "\n".join(f"[{i}] {c.content}" for i, c in enumerate(context, start=1)) or "(none)"
    return (
        "You are a quiz designer. Generate exactly ONE multiple-choice question grounded in the context.\n"
        "Output format (use EXACTLY, no extra prose, no leading blank line):\n"
        "Question: <one sentence question>\n"
        "A) <option A>\n"
        "B) <option B>\n"
        "C) <option C>\n"
        "D) <option D>\n"
        "Answer: <single letter A-D>\n"
        "Explanation: <one sentence>\n\n"
        "Rules:\n"
        "  1. Exactly one option is correct; the other three are plausible distractors.\n"
        "  2. The question tests understanding, not memorisation.\n"
        "  3. Use only information that appears in the context.\n\n"
        f"Topic: {topic}\n\n"
        f"Context:\n{ctx}"
    )


class GeminiLLMProvider:
    """Google Gemini via `langchain-google-genai`. Builds two lazy chat instances:
    one with low temperature for routing, one with a touch of variability for
    tutor/quiz generation.
    """

    _router: ChatGoogleGenerativeAI | None = None
    _chat: ChatGoogleGenerativeAI | None = None
    _lock: Lock = Lock()

    def _build_chat(self, *, temperature: float) -> ChatGoogleGenerativeAI:
        if not settings.google_api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Add it to `.env` (see `.env.example`) or "
                "export it before starting the backend."
            )
        return ChatGoogleGenerativeAI(
            model=settings.gemini_model_name,
            google_api_key=settings.google_api_key,
            temperature=temperature,
        )

    def router_chat(self) -> ChatGoogleGenerativeAI:
        if GeminiLLMProvider._router is None:
            with GeminiLLMProvider._lock:
                if GeminiLLMProvider._router is None:
                    GeminiLLMProvider._router = self._build_chat(temperature=0.0)
        return GeminiLLMProvider._router

    def chat(self) -> ChatGoogleGenerativeAI:
        if GeminiLLMProvider._chat is None:
            with GeminiLLMProvider._lock:
                if GeminiLLMProvider._chat is None:
                    GeminiLLMProvider._chat = self._build_chat(temperature=0.4)
        return GeminiLLMProvider._chat

    async def classify(self, text: str) -> Route:
        response = await self.router_chat().ainvoke(
            [SystemMessage(content=CLASSIFIER_SYSTEM), HumanMessage(content=text)]
        )
        raw = (response.content or "").strip().upper()
        # Permissive parse: QUIZ takes precedence (an explicit "quiz me" message
        # that happens to also include a greeting is still a quiz request), then
        # SMALLTALK, and tutor is the default for anything substantive or
        # unrecognised. The default-to-tutor branch preserves the strict-grounding
        # behaviour for the bulk of educational messages.
        if "QUIZ" in raw:
            return "quiz"
        if "SMALLTALK" in raw:
            return "smalltalk"
        return "tutor"

    async def complete(self, messages: list[BaseMessage], *, system: str | None = None) -> str:
        # Non-streaming path. The graph drives streaming via `chat().astream(...)` inside
        # the tutor / quiz nodes; this remains for any non-graph caller.
        prefix: list[BaseMessage] = [SystemMessage(content=system)] if system else []
        response = await self.chat().ainvoke(prefix + list(messages))
        return response.content or ""

    async def quiz(self, *, topic: str, context: list[RetrievedChunk]) -> str:
        prompt = _build_quiz_prompt(topic, context)
        response = await self.chat().ainvoke(
            [SystemMessage(content=prompt), HumanMessage(content=f"Generate the quiz on: {topic}")]
        )
        return response.content or ""


_singleton: LLMProvider = GeminiLLMProvider()


def get_llm_provider() -> LLMProvider:
    return _singleton


def get_chat_model() -> ChatGoogleGenerativeAI:
    """Direct access to the streaming-capable Gemini client.

    Used by tutor / quiz nodes which call `.astream(...)` so their tokens surface
    in `graph.astream_events(version="v2")` for SSE delivery.
    """
    provider = _singleton
    if not isinstance(provider, GeminiLLMProvider):  # pragma: no cover
        raise RuntimeError("get_chat_model requires the default GeminiLLMProvider")
    return provider.chat()
