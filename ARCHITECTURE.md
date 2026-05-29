# Personal AI Tutor — Architecture

## 1. Overview

A self-hosted AI tutor that ingests user-supplied learning material (uploaded files or scraped web pages), indexes it for semantic retrieval, and serves a streaming chat interface backed by an LLM. This document describes the **skeleton** that everything else hangs off — infrastructure, the FastAPI backend with documented endpoints, the Next.js frontend, and the integration test that proves the backend can reach Postgres and Redis.

```
┌──────────────┐    HTTP/SSE     ┌──────────────────┐    SQL     ┌────────────────────────┐
│  Next.js UI  │ ──────────────▶ │  FastAPI backend │ ─────────▶ │ Postgres + pgvector    │
│   :3000      │                 │       :8000      │            │     :5432              │
└──────────────┘                 │                  │ ─────────▶ ┌────────────────────────┐
                                 └──────────────────┘   resp     │ Redis 7                │
                                                                 │   host :6380           │
                                                                 └────────────────────────┘
```

## 2. Repository layout

```
personal-ai-tutor/
├── docker-compose.yml          # Postgres + pgvector, Redis
├── .env.example                # DATABASE_URL, REDIS_URL
├── ARCHITECTURE.md             # this file
├── README.md                   # quick-start
├── postgres/init/              # mounted into the postgres container; runs on first boot
│   └── 01-extensions.sql       # CREATE EXTENSION vector
├── backend/                    # FastAPI service
│   ├── pyproject.toml          # uv-managed deps
│   ├── alembic/                # Alembic migrations (env.py is async; baseline = 0001_baseline)
│   ├── alembic.ini             # script_location = alembic; URL resolved from settings
│   ├── app/
│   │   ├── main.py             # app factory, router wiring, OpenAPI metadata (schema owned by Alembic from phase 7)
│   │   ├── config.py           # pydantic-settings (DB / Redis / embedding dim / upload limits)
│   │   ├── db.py               # async SQLAlchemy engine + session dep
│   │   ├── redis_client.py     # redis.asyncio client + dep
│   │   ├── models.py           # SQLAlchemy Base + Document + DocumentChunk (pgvector Vector)
│   │   ├── schemas/            # pydantic request/response models
│   │   ├── routers/            # health, ingest, chat
│   │   ├── services/           # parser / chunker / embeddings / ingest / retrieval / session
│   │   └── agents/             # LangGraph: state, llm, router, tutor, quiz, graph
│   └── tests/
│       ├── conftest.py         # httpx client + db_clean + redis_clean + sample_pdf_bytes + seeded_chunks
│       ├── test_health.py
│       ├── test_ingest.py      # end-to-end PDF ingestion test
│       ├── test_agents.py      # graph routing + retrieval + force_route + chat-route smoke
│       └── test_chat_sessions.py # SSE streaming + Redis session-cache assertions
└── frontend/                   # Next.js 16 (App Router) + React 19 + Tailwind 4
    ├── app/chat/page.tsx       # chat page (server shell + client island)
    ├── components/             # Chat, MessageList, Message (react-markdown), ChatInput
    ├── lib/sse.ts              # fetch-based SSE async generator
    ├── e2e/chat.spec.ts        # Playwright end-to-end tests
    └── playwright.config.ts    # auto-starts uvicorn + next dev as webServers
```

## 3. Runtime topology

| Process     | Where           | Port | How to start                                  |
|-------------|-----------------|------|-----------------------------------------------|
| Postgres    | docker compose  | 5432 | `docker compose up -d postgres`               |
| Redis       | docker compose  | host 6380 → container 6379 | `docker compose up -d redis`     |
| FastAPI     | host (uvicorn)  | 8000 | `cd backend && uv run uvicorn app.main:app --reload` |
| Next.js     | host (npm)      | 3000 | `cd frontend && npm run dev`                  |

The backend runs on the host (not in compose) so the dev loop is as fast as possible — no rebuilds for code changes. Only the stateful services live in compose.

## 4. Data stores

**Postgres + pgvector** is the single source of truth for both relational data (documents, chunks, ingestion jobs, future user/session tables) and vector embeddings. Using one store instead of a dedicated vector DB keeps the deployment surface tiny and makes joins between metadata and embeddings trivial. The `vector` extension is enabled by `postgres/init/01-extensions.sql`, which Postgres runs once on first boot of an empty data volume.

**Redis** holds ephemeral state. As of phase 3, that's per-session chat history (LIST per session, 30-day TTL, last 10 turn-pairs kept — see §4d). Future uses: SSE fan-out across replicas, rate limiting, and the eventual ingestion job queue. We keep it out of Postgres so we can blow away cache state without touching durable data.

## 4a. Database schema

Two tables, defined in `backend/app/models.py`. **Schema is owned by Alembic** (phase 7) — the baseline revision `0001_baseline` (in `backend/alembic/versions/`) creates the `vector` extension, both tables, the FK + unique constraint, the b-tree FK index, and the HNSW ANN index. Run `alembic upgrade head` before booting the app against a fresh database; for an existing DB, `alembic stamp head` records the baseline without re-issuing DDL.

```
┌─────────────────────────┐         ┌──────────────────────────────┐
│ documents               │         │ document_chunks              │
├─────────────────────────┤  1   *  ├──────────────────────────────┤
│ id            UUID  PK  │◀────────│ document_id   UUID  FK CASCADE│
│ source_type   TEXT      │         │ id            UUID  PK       │
│ source_uri    TEXT      │         │ chunk_index   INT            │
│ title         TEXT      │         │ content       TEXT           │
│ status        TEXT      │         │ token_count   INT            │
│ error         TEXT      │         │ embedding     vector(768)    │
│ doc_metadata  JSONB     │         │ chunk_metadata JSONB         │
│ created_at    TIMESTAMP │         │ created_at    TIMESTAMP      │
│ updated_at    TIMESTAMP │         │ UNIQUE (document_id, chunk_index) │
└─────────────────────────┘         └──────────────────────────────┘
```

`documents` — one row per ingestion.
- `source_type` — `'upload'` or `'scrape'`.
- `source_uri` — filename for uploads, final URL for scrapes (after redirects).
- `status` — `'processing' | 'completed' | 'failed'`. Lifecycle: starts `processing`, ends either `completed` (chunks + embeddings persisted) or `failed` (with `error` populated; partial chunks rolled back).
- `doc_metadata` — extractor-specific metadata (e.g. `{"format": "pdf", "page_count": 7}`).

`document_chunks` — one row per chunk produced by the splitter.
- `chunk_index` — 0-based ordinal within the document; `UNIQUE (document_id, chunk_index)` guarantees stable ordering.
- `embedding` — `pgvector` `vector(N)` column. `N = settings.embedding_dim` (768 in phase 1). Changing the dimension later requires a migration; the column is fixed-width.
- `ON DELETE CASCADE` — re-ingesting a source by deleting its `Document` wipes its chunks atomically.

**ANN index** (phase 6): `ix_document_chunks_embedding_hnsw` is an HNSW index on `embedding` using `vector_cosine_ops` with `m=16, ef_construction=64`. Declared on the SQLAlchemy model AND issued explicitly in the Alembic baseline migration (phase 7 replaced the lifespan `CREATE INDEX IF NOT EXISTS` with a migration-managed index). Cosine distance is what `retrieve_top_k` already uses, so the index plugs into the existing query plan without code changes at call sites.

## 4b. Ingestion pipeline

```
   route                   services.parser              services.chunker              services.embeddings        services.ingest
   ─────                   ───────────────              ────────────────              ───────────────────        ───────────────
   POST /ingest/upload ──▶ parse_pdf / parse_text   ──▶ chunk_text                ──▶ StubEmbedding.embed_batch ──▶ persist Document + DocumentChunks
   POST /ingest/scrape ──▶ fetch_url → parse_html   ──▶ (SentenceSplitter 512/128)    (deterministic 768-d)         (single transaction, status=completed)
```

- **Parser** (`backend/app/services/parser.py`) — `pypdf` for PDFs, raw UTF-8 decode for TXT / MD, `httpx` + `BeautifulSoup(lxml)` for HTML (strips `script` / `style` / `noscript`, prefers `<main>` over `<body>`).
- **Chunker** (`backend/app/services/chunker.py`) — LlamaIndex `SentenceSplitter(chunk_size=512, chunk_overlap=128)`. Parameters chosen for dense study material (UPSC guides, distributed-systems texts): small chunks keep retrieval precise; generous overlap preserves cross-paragraph context.
- **Embedder** (`backend/app/services/embeddings.py`) — `EmbeddingProvider` Protocol with `HuggingFaceEmbeddingProvider` (phase 5) wrapping `sentence-transformers/all-mpnet-base-v2`. The model is lazy-loaded into a class-level singleton on first use (~420 MB cached to `~/.cache/huggingface/`). `model.encode(...)` is synchronous, so each call is wrapped in `asyncio.to_thread(...)` to keep the event loop free. Output is 768-d, normalized — matches the existing `vector(768)` pgvector column.
- **Orchestrator** (`backend/app/services/ingest.py`) — single-transaction `ingest_parsed()`. On exception: rollback the chunks, then write a `status='failed'` row carrying the error so the caller can still poll `/ingest/{id}`.

## 4c. Agents & orchestration (LangGraph)

The `/chat` endpoint drives a compiled LangGraph state machine that lives in `backend/app/agents/`. Three nodes:

- **Router** (`agents/router.py`) — calls Gemini 2.5 Flash (`temperature=0`) with a strict system prompt demanding one of `TUTOR` / `QUIZ` and writes `state["route"]`. Does not produce an assistant message. If the caller pre-sets `state["route"]` (via `ChatRequest.force_route` → `ainvoke_graph(..., force_route=...)`), the Router preserves it and skips the LLM call — used by the frontend's "Give me an example" / "Test my knowledge" buttons to dispatch deterministically.
- **Tutor** (`agents/tutor.py`) — retrieves the top-`k` pgvector chunks, formats them as a numbered context block, and calls `ChatGoogleGenerativeAI.astream(...)` with a **strict-grounding** system prompt. The prompt forces the model to (a) cite snippet numbers in square brackets, (b) refuse to answer when the context is insufficient (returns the literal fallback sentence), and (c) never use outside knowledge. Streaming chunks surface to the route via `astream_events`.
- **Quiz** (`agents/quiz.py`) — retrieves a smaller amount of grounding context (top-2) and calls Gemini with a **structured-MCQ** system prompt that pins the output to seven lines: `Question:`, `A) … D)`, `Answer:`, `Explanation:`. The route streams the chunks as they're generated.

Routing is a conditional edge from `router` to either `tutor` or `quiz`; both leaves terminate. See [`DIAGRAMS.md`](./DIAGRAMS.md) for Mermaid renderings of the node flow, tutor internals, state-threading table, and request lifecycle.

State (`agents/state.py`) is a `TypedDict` with five fields:
- `messages: list[BaseMessage]` (LangChain message types, accumulated via the standard `add_messages` reducer)
- `user_score: int` — threaded but **not mutated** in phase 2; grading lives in phase 3
- `context: list[RetrievedChunk]` — populated by Tutor / Quiz
- `route: "tutor" | "quiz" | None` — set by Router
- `response: str | None` — final assistant text

The LLM is abstracted behind `LLMProvider` (`agents/llm.py`) — three methods (`classify`, `complete`, `quiz`). Phase 5 ships `GeminiLLMProvider` (Google Gemini 2.5 Flash via `langchain-google-genai`). Two lazy `ChatGoogleGenerativeAI` instances live behind the provider: a `temperature=0` router and a `temperature=0.4` tutor/quiz model. The Tutor and Quiz nodes additionally call `.astream(...)` directly so per-token chunks flow through LangGraph's event stream and out to SSE clients.

Retrieval (`services/retrieval.py`) — `retrieve_top_k(session, query, k=4)` embeds the query via `HuggingFaceEmbeddingProvider`, then runs `SELECT … ORDER BY embedding <=> $1 LIMIT k` against `document_chunks` using pgvector's `cosine_distance()` SQLAlchemy operator. As of phase 6 the query is served by the HNSW index on `embedding` (`vector_cosine_ops`) — see §4a.

The `/chat` route streams **per-token** SSE via `graph.astream_events(version="v2")`, filtering for `on_chat_model_stream` events emitted from the `tutor` or `quiz` nodes (Router classification chunks are deliberately filtered out). See §6 for the protocol details.

## 4d. Session memory (Redis)

Short-term conversation memory is owned by the server. Each chat session is one Redis LIST at key `chat:user:{user_id}:session:{session_id}:messages` (phase 6 user-scoped key — phase 3 used `chat:session:{session_id}:messages` and is no longer accepted; rolling 30-day TTL retires the old keys naturally). Each element is a JSON-encoded `ChatMessage` (`role` + `content`). The store is wrapped in `app/services/session.py::SessionStore` with three operations:

- `get_history(user_id, session_id) -> list[ChatMessage]` — `LRANGE … 0 -1`.
- `append_turn(user_id, session_id, user_msg, assistant_msg)` — atomic `RPUSH` + `LTRIM` + `EXPIRE` inside a `MULTI/EXEC` pipeline. Either all three apply or none do.
- `clear(user_id, session_id)` — `DEL`.

**Sliding window**: keep the last 10 turn-pairs (20 messages); older entries fall off via `LTRIM key -20 -1`. Configurable via `session_history_turns` (default 10).

**TTL**: 30 days, refreshed on every write. Configurable via `session_ttl_seconds`.

**User scoping & access control** (phase 6 → phase 8): the route resolves `user_id` from an `Authorization: Bearer <jwt>` header via the `app/auth.py::get_user_id` FastAPI dependency. Phase 8 replaces the permissive-UUID parser with **Clerk JWT verification** — every request without a valid Clerk-signed JWT receives `401`. The `sub` claim (a Clerk user id like `user_2abc123def`, a string — not a UUID) becomes the first component of the Redis key, so two callers presenting different JWTs but sharing a `session_id` get fully separate Redis namespaces. This is now real access control, not just scoping. The `DEV_AUTH_BYPASS=1` env var re-enables the permissive parser for ad-hoc curl smoke testing only (warning logged on boot; never set in production).

**Session id round-trip**:
- Request: `session_id` is **optional**. If present, the server resumes that conversation; if absent, the server mints a fresh `uuid4`.
- Response: the chosen session id is **always** returned in the `X-Session-Id` header (HTTP, not SSE — clients read it before consuming the stream). Echo it on follow-up requests to maintain history.

**Request body** (post phase 3): `{ "message": "<user turn>", "session_id": "<uuid>"?, "force_route": "tutor" | "quiz"? }`. The phase-2 `messages: list` field is gone — the client only sends the new user turn; the server merges history from Redis before invoking the graph. The phase-4 `force_route` field lets the client bypass the Router's classifier and dispatch directly to Tutor or Quiz.

**Write ordering**: `read history → invoke graph → on success, append turn → stream`. The append happens after a successful graph run, so a graph failure never pollutes the session log. Streaming runs from the in-memory response text, so there is no race between writing and streaming.

## 5. API surface

OpenAPI 3.1 spec is served at `/openapi.json`; Swagger UI at `/docs`; ReDoc at `/redoc`. Phase 8: every route under the `chat` and `ingest` tags **requires** `Authorization: Bearer <clerk-jwt>` — unsigned-in requests get `401`. Only `/health` (and Swagger/ReDoc itself) remains world-readable. The optional `DEV_AUTH_BYPASS=1` env var on the backend re-enables permissive Bearer-as-user-id parsing for ad-hoc smoke testing — see [Authentication (Clerk)](./README.md#authentication-clerk) in the README.

| Method | Path                       | Tag     | Purpose                                                      | Status            |
|--------|----------------------------|---------|--------------------------------------------------------------|-------------------|
| GET    | `/health`                  | health  | Liveness + Postgres + Redis dependency check.                | implemented       |
| POST   | `/ingest/upload`           | ingest  | Upload a PDF / TXT / MD file; parse → chunk → embed → save.  | implemented       |
| POST   | `/ingest/scrape`           | ingest  | Fetch a URL with httpx, extract text via BeautifulSoup.      | implemented       |
| GET    | `/ingest/{ingestion_id}`   | ingest  | Poll ingestion status + chunk count.                         | implemented       |
| POST   | `/chat`                    | chat    | Session-aware. **Token-level** SSE from Gemini; `X-Session-Id` header on response. | implemented |

Ingestion runs synchronously inside the request handler — the response carries the final status (`completed` or `failed`) and the chunk count. Moving to a Redis-backed worker is a later follow-up; the API contract is forward-compatible.

## 6. Streaming protocol (`POST /chat`)

The response is `text/event-stream`. Each chunk arrives as one SSE event whose `data` field is a JSON object `{"delta": "<string>"}`. The stream terminates with a literal `data: [DONE]` event.

```
data: {"delta": "Hello "}

data: {"delta": "world "}

data: [DONE]
```

Phase 5 streaming is **token-level**, driven by LangGraph's `graph.astream_events(initial_state, version="v2")`. The route filters the event stream to `event["event"] == "on_chat_model_stream"` where `event["metadata"]["langgraph_node"]` is `"tutor"` or `"quiz"`, then forwards each chunk's `.content` as a `delta` event. Router classification chunks (single-word `TUTOR` / `QUIZ`) are deliberately filtered out so the route decision never leaks into the assistant message.

After the stream completes (the `finally` block runs even if the client disconnects mid-stream), the route writes the *accumulated* response text to Redis via `SessionStore.append_turn()`, then emits the closing `data: [DONE]` event.

Phase 3 introduced session memory on top of this same SSE contract — see §4d. The session id round-trips via the `X-Session-Id` HTTP response header (clients read it once, before reading the stream).

## 7. Local development

**Prerequisites**
- Docker Desktop (or another Docker daemon) with Compose v2.
- [uv](https://github.com/astral-sh/uv) ≥ 0.5 (install via `pipx install uv`).
- Node 20+.
- A Google AI Studio API key for Gemini (free tier is fine). Get one at <https://aistudio.google.com/> and set `GOOGLE_API_KEY` in `.env`.

**Bring everything up**
```bash
cp .env.example .env                     # then add GOOGLE_API_KEY
docker compose up -d

# Backend (first `uv sync` pulls ~700 MB of torch + transformers;
# first chat / embed call downloads ~420 MB of sentence-transformers
# weights to ~/.cache/huggingface/)
cd backend
uv sync
uv run uvicorn app.main:app --reload     # http://localhost:8000/docs

# Frontend
cd ../frontend
npm install
npm run dev                               # http://localhost:3000
```

**Run the integration test**
```bash
cd backend
uv run pytest -v
```

The tests exercise the real FastAPI app in-process via `httpx.ASGITransport` against the real Postgres + Redis from compose. They do **not** mock the data layer or the embedder. LLM tests are gated on `GOOGLE_API_KEY` — without a key they skip cleanly via the `requires_llm` marker; with one, they exercise live Gemini. Coverage:

- `test_health.py` — `/health` reports both data stores reachable.
- `test_ingest.py` — uploads an fpdf2-generated PDF, asserts `/ingest/upload` returns `status='completed'`, then queries the DB directly and verifies (a) a `documents` row was created with the right metadata, (b) `document_chunks` rows are contiguous and non-empty, (c) the stored embedding is a 768-dim float vector with non-zero norm. Also covers the 415 (unsupported type) and 404 (unknown id) paths.
- `test_ingest_hf.py` (phase 5) — uploads synthesised editorial-style "daily news analysis" text, then asserts the stored embedding is a real semantic vector: ~unit norm, **not** equal to the deterministic-hash vector the phase-1 stub would have produced, and that two paraphrased sentences cluster materially closer in cosine space than an unrelated one. This is the proof that the embedding brain transplant landed.
- `test_agents.py` *(requires `GOOGLE_API_KEY`)* — drives the LangGraph orchestrator end-to-end through Gemini: Router dispatches concept questions to the Tutor and quiz triggers to the Quiz; Tutor retrieves chunks from pgvector and grounds the answer; `user_score` round-trips; `messages` accumulate; `force_route` bypasses the LLM classifier; `POST /chat` returns valid SSE.
- `test_chat_sessions.py` *(requires `GOOGLE_API_KEY`)* — consumes the `/chat` SSE stream via `httpx.AsyncClient.stream(...)`, asserts multiple `delta` events + terminating `[DONE]`, and verifies Redis side-effects: the user + assistant pair is appended to `chat:user:{user_id}:session:{id}:messages`, history is resumed across requests, the LIST is trimmed to the 20-message cap on overflow, and every new request without `session_id` gets a fresh UUID via the `X-Session-Id` header. The new `test_chat_isolates_history_between_users` proves two callers presenting different Bearer tokens but sharing a `session_id` get separate Redis namespaces.

The `db_clean` fixture `TRUNCATE`s `documents` + `document_chunks` before each test that touches the DB; the `seeded_chunks` fixture builds on it to insert a known mini-corpus (CAP / Raft / CRDTs) for retrieval assertions. The `redis_clean` fixture deletes all `chat:user:*` (plus the legacy `chat:session:*`) keys before and after every chat-session test for isolation. A session-scoped autouse `_hf_warmup` fixture loads the sentence-transformers model once so no single test eats the first-call download.

The Playwright suite (`frontend/e2e/`) runs against the live Next.js dev server but **mocks the backend HTTP layer** — `chat.spec.ts` mocks the SSE stream from `/api/backend/chat`, and `admin.spec.ts` mocks `/api/backend/ingest/scrape`. This keeps the UI suite deterministic and decoupled from LLM availability or external URLs.

## 8. What's deliberately not here yet

| Concern                    | Will live in                                          |
|---------------------------- |-------------------------------------------------------|
| Per-user document ownership in Postgres | add `documents.user_id` (the Clerk user id) and filter retrieval by it, so users can't read each other's ingested corpus. Phase 8 gates the ingest endpoints behind auth but the corpus itself is still shared. |
| Gemini cost / quota guardrails | rate-limit middleware + per-session token-spend tracking in Redis |
| Embedding on GPU / MPS     | sentence-transformers picks up MPS automatically on Apple Silicon, but no fallback path / batch tuning yet |
| Prompt-eval suite          | offline evaluation harness for Tutor citation accuracy and Quiz format compliance |
| Tuning the HNSW search-time `ef_search` parameter for the recall/latency knob (phase 6 added the index with default build params) | runtime `SET LOCAL hnsw.ef_search = N` in `retrieve_top_k` |
| Grading + difficulty adaptation | `agents/quiz.py` mutating `state.user_score`; persisted per-session |
| Background ingestion       | move `ingest_parsed` behind a Redis-backed worker (Arq) |
| Multi-page scraping        | honour `ScrapeRequest.max_depth` (currently ignored)  |
| CI                         | `.github/workflows/`                                  |

Each of these slots in without restructuring; the skeleton is designed to be additive.
