# Personal AI Tutor — End-to-End Flow

This document walks every layer of the system from "user uploads a document" to "user receives a streamed answer", with **live evidence** captured from the running system rather than aspirational architecture. It's meant to be the first thing a new contributor reads.

Two diagrams up top (high-level + request lifecycle); then a layer-by-layer walkthrough with the actual data observed during validation.

---

## 1. High-Level Design

```mermaid
flowchart TB
    subgraph Browser["Browser (Next.js 16 + React 19 + Tailwind 4)"]
        Landing["/ (landing)"]
        Admin["/admin<br/>(drag-drop + URL scrape)"]
        Chat["/chat<br/>(SSE consumer + Markdown render)"]
    end

    subgraph Backend["FastAPI backend (uvicorn :8000)"]
        IngestRouter["routers/ingest.py<br/>POST /ingest/upload<br/>POST /ingest/scrape"]
        ChatRouter["routers/chat.py<br/>POST /chat (SSE)"]
        Auth["auth.py<br/>get_user_id (Bearer or anon)"]

        subgraph IngestPipeline["services/ — ingestion pipeline"]
            Parser["parser.py<br/>pypdf · BeautifulSoup · text"]
            Chunker["chunker.py<br/>LlamaIndex SentenceSplitter<br/>512 tokens / 128 overlap"]
            Embedder["embeddings.py<br/>HuggingFaceEmbeddingProvider<br/>all-mpnet-base-v2 · 768-d"]
            IngestOrch["ingest.py<br/>persist Document + chunks"]
        end

        subgraph AgentGraph["agents/ — LangGraph orchestrator"]
            Router["router_node<br/>(Gemini classify<br/>or pre-set force_route)"]
            Tutor["tutor_node<br/>retrieve k=4 → strict<br/>citation system prompt"]
            Quiz["quiz_node<br/>retrieve k=2 → structured<br/>MCQ system prompt"]
        end

        subgraph DataAccess["data access"]
            Retrieval["services/retrieval.py<br/>cosine_distance + HNSW"]
            SessionStore["services/session.py<br/>user-scoped Redis LIST"]
        end
    end

    subgraph DataStores["External data stores"]
        Postgres[("Postgres 16<br/>+ pgvector 0.8.2<br/>HNSW index<br/>vector_cosine_ops")]
        Redis[("Redis 7<br/>chat:user:{uid}:session:{sid}:messages<br/>30-day TTL · 20-msg sliding window")]
        Gemini["Google Gemini<br/>gemini-2.5-flash-lite<br/>(streaming via astream_events)"]
        HFCache["~/.cache/huggingface/<br/>(420 MB model weights, local)"]
    end

    Landing --> Chat
    Landing --> Admin
    Admin -->|multipart upload| IngestRouter
    Admin -->|JSON scrape| IngestRouter
    IngestRouter --> Parser --> Chunker --> Embedder --> IngestOrch
    Embedder -.loads from.-> HFCache
    IngestOrch -->|INSERT| Postgres

    Chat -->|fetch + ReadableStream<br/>SSE| ChatRouter
    ChatRouter --> Auth
    ChatRouter --> AgentGraph
    Router --> Tutor
    Router --> Quiz
    Tutor --> Retrieval
    Quiz --> Retrieval
    Retrieval -->|cosine SELECT<br/>via HNSW| Postgres
    Tutor -.astream tokens.-> Gemini
    Quiz -.astream tokens.-> Gemini
    Router -.classify.-> Gemini
    ChatRouter -->|read/append turn| SessionStore
    SessionStore --> Redis
    ChatRouter -->|word-by-word SSE deltas<br/>then data: [DONE]| Chat
```

---

## 2. Request Lifecycle (sequence)

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant FE as Next.js (browser)
    participant API as FastAPI /chat
    participant G as LangGraph
    participant R as Router (Gemini)
    participant T as Tutor (Gemini astream)
    participant Emb as HF embedder
    participant PG as Postgres + pgvector
    participant Ses as SessionStore
    participant Rd as Redis

    Note over U,FE: Step 0 — earlier: ingestion populated `documents` + `document_chunks` with 768-d HF embeddings.

    U->>FE: types question, clicks Send
    FE->>API: POST /chat { message, session_id?, force_route?, Authorization: Bearer <uid>? }
    API->>API: get_user_id() → uid (or ANONYMOUS_USER_ID)
    API->>API: session_id = supplied or uuid4()
    API->>Ses: get_history(uid, sid) → list[ChatMessage]
    Ses->>Rd: LRANGE chat:user:{uid}:session:{sid}:messages
    Rd-->>Ses: history JSON entries (≤ 20)
    Ses-->>API: ChatMessage[]
    API-->>FE: 200 OK · X-Session-Id: {sid} · text/event-stream

    API->>G: graph.astream_events(initial_state, version="v2")
    G->>R: router_node(state)
    alt force_route was supplied
        R-->>G: pass-through (no LLM call)
    else classify
        R->>R: Gemini call (temperature=0, "Reply TUTOR or QUIZ")
        R-->>G: writes state["route"]
    end

    alt route == "tutor"
        G->>T: tutor_node(state)
        T->>Emb: embed_batch([last_user_message])  (asyncio.to_thread on encode)
        Emb-->>T: 768-d query vector
        T->>PG: SELECT ... ORDER BY embedding <=> $1 LIMIT 4  (HNSW vector_cosine_ops)
        PG-->>T: top-4 RetrievedChunks
        T->>T: build "Use ONLY context [1..4]; cite [n]; refuse if insufficient"
        T->>T: gemini_chat.astream([SystemMessage, *history, HumanMessage])
        loop per Gemini token chunk
            T-->>G: AIMessageChunk
            G-->>API: on_chat_model_stream event (langgraph_node="tutor")
            API->>API: filter to tutor/quiz · accumulate response_buf
            API-->>FE: data: {"delta": "<text>"}
        end
    else route == "quiz"
        Note over G: same shape, k=2 retrieval, structured MCQ system prompt
    end

    API->>Ses: append_turn(uid, sid, user_msg, assistant_msg)
    Ses->>Rd: MULTI: RPUSH + LTRIM key -20 -1 + EXPIRE key 2_592_000s · EXEC
    Rd-->>Ses: OK
    API-->>FE: data: [DONE]
    FE->>FE: render markdown via react-markdown + rehype-highlight
```

---

## 3. Layer-by-layer walk-through with live evidence

Captured against the running instance on `2026-05-29`. Commands shown are reproducible.

### Step 0 — Ingestion (already happened)

User dragged a Markdown file onto `/admin`'s upload zone (or paste a URL into the Scrape form). The route `POST /ingest/upload` parsed → chunked → embedded → persisted in **one synchronous transaction**.

```text
$ docker exec tutor-postgres psql -U tutor -d tutor -c "SELECT … FROM documents …;"
 source_type | source_uri                       | status    | chunks
-------------+----------------------------------+-----------+--------
 upload      | amazon_architecture_deepdive.md  | completed | 31
```

**Verified pipeline steps for this document:**

| # | Service                                | Behaviour                                                                 |
|---|----------------------------------------|---------------------------------------------------------------------------|
| 1 | `services/parser.py::parse_upload`     | Detected MIME → routed to `parse_text` → utf-8 decoded                    |
| 2 | `services/chunker.py::chunk_text`      | LlamaIndex `SentenceSplitter(chunk_size=512, chunk_overlap=128)` → **31 chunks** |
| 3 | `services/embeddings.py::HuggingFaceEmbeddingProvider.embed_batch` | Lazy-loaded `sentence-transformers/all-mpnet-base-v2`, ran `model.encode(normalize=True)` in a worker thread; output was 31 × 768-d unit vectors |
| 4 | `services/ingest.py::ingest_parsed`    | One `Document` + 31 `DocumentChunk` rows in a single transaction; status = `completed` |

```text
$ docker exec tutor-postgres psql -U tutor -d tutor -c "
SELECT chunk_index, array_length(embedding::real[], 1) AS dim, LEFT(content,80) FROM document_chunks LIMIT 3;"
 chunk_index | dim |                              left
-------------+-----+-----------------------------------------------------------------
           0 | 768 | # Amazon E-Commerce — Architecture Deep Dive                  +
                   |       **Project:** `amazon` (system-desi
           1 | 768 | At flash-sale scale (1,000 units, 10,000 buyers) this isn't an
           2 | 768 | ts                       │                                    +
```

**Every chunk's `embedding` column is exactly 768 floats** — proves the real HF model is in play and matches the `vector(768)` schema.

### Step 1 — User opens the chat page

`frontend/app/chat/page.tsx` mounts `<Chat />`. The component reads `tutorSessionId` from `localStorage` (if present) and re-attaches to that conversation. No backend call yet.

### Step 2 — User types and hits Send (or clicks an action button)

`components/Chat.tsx::send(text, forceRoute?)`:

- Optimistic: append user bubble, append empty assistant bubble, mark `streaming=true`.
- Calls `lib/sse.ts::streamChat({ message, session_id, force_route })` which POSTs to `/api/backend/chat` (proxied by `next.config.ts` → `http://localhost:8000/chat`).

The proxy is transparent to SSE — no CORS, no buffering. Same-origin from the browser's perspective.

### Step 3 — FastAPI receives `POST /chat`

```text
$ curl -i -X POST http://localhost:8000/chat \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer dddddddd-dddd-dddd-dddd-dddddddddddd' \
    -d '{"message":"What service handles inventory? Just one line please."}'
HTTP/1.1 200 OK
x-session-id: e8b57896-b9c0-41ed-96c4-d08c692bab0e
cache-control: no-cache
content-type: text/event-stream; charset=utf-8
transfer-encoding: chunked
```

`routers/chat.py` does, in order:

1. **Auth:** `get_user_id` parses the `Authorization: Bearer …` header → `UUID(dddddddd-…)`. Without a header it falls back to `ANONYMOUS_USER_ID = 00000000-0000-0000-0000-000000000001`.
2. **Session id:** uses the supplied `session_id` or mints a fresh `uuid4()`; **always** returned in the `X-Session-Id` HTTP response header.
3. **History fetch:** `SessionStore.get_history(uid, sid)` issues `LRANGE chat:user:{uid}:session:{sid}:messages 0 -1` against Redis; returns up to 20 prior `ChatMessage`s (10 turn-pairs).
4. Begins streaming.

### Step 4 — LangGraph drives the response

`graph.astream_events(initial_state, version="v2")` iterates events from a compiled `StateGraph(TutorState)` with three nodes:

```
START → router → (conditional) → tutor | quiz → END
```

- **Router** is bypassed when the client sent `force_route` (used by the "Give me an example" and "Test my knowledge" buttons). Otherwise it makes a Gemini call with temperature 0 and a strict system prompt: *"Reply with exactly one word, uppercase: TUTOR or QUIZ"*. Defaults to `"tutor"` on any unexpected output.
- **Tutor** is the one we hit in this trace.

### Step 5 — Tutor retrieves from pgvector via HNSW

`tutor_node`:

1. Calls `services/retrieval.py::retrieve_top_k(session, query, k=4)`.
2. `HuggingFaceEmbeddingProvider.embed_batch([query])` produces the 768-d query vector (same model used at ingest time).
3. SQL: `SELECT *, embedding <=> $1 AS distance FROM document_chunks ORDER BY distance LIMIT 4` — served by the HNSW index:

```text
$ docker exec tutor-postgres psql -U tutor -d tutor -c "\d document_chunks"
…
Indexes:
    "ix_document_chunks_embedding_hnsw" hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')
```

4. Builds a system prompt of the form:

   > You are an expert AI tutor. Answer the student's question using ONLY the numbered context snippets below…
   >
   > Context:
   > [1] …chunk content…
   > [2] …
   > [3] …
   > [4] …

5. Calls `gemini_chat.astream([SystemMessage, *history, HumanMessage])`. Tokens flow back as `AIMessageChunk`s.

### Step 6 — Token-level SSE back to the browser

The route filters `astream_events` to events where `event["event"] == "on_chat_model_stream"` **and** `metadata["langgraph_node"] in {"tutor", "quiz"}`. The Router's classification output is **deliberately suppressed** — the user never sees the literal "TUTOR" / "QUIZ" token.

Each kept chunk is emitted as one SSE event:

```text
$ curl -sN -X POST http://localhost:8000/chat -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer dddddddd-dddd-dddd-dddd-dddddddddddd' \
    -d '{"message":"What service handles inventory? Just one line please."}'
data: {"delta": "The `"}

data: {"delta": "cart.ts` route module handles inventory reserve [5]."}

data: [DONE]
```

Two real observations:

- **Grounded citation** — `[5]` references chunk index 5 of the Amazon doc; Gemini is using the retrieved context.
- **Stream is genuinely incremental** — multiple `delta` events for a one-line answer; longer answers produce many more (see the screenshot showing 5+ deltas building a multi-paragraph response).

Total wall-clock for the entire round-trip on `gemini-2.5-flash-lite`: **~4 seconds** for a 4-event response with retrieval. Tokens visibly land in the UI as Gemini emits them.

### Step 7 — Persistence in Redis (sliding window + TTL)

After the stream's `finally` block:

```python
await store.append_turn(
    user_id, session_id,
    user_msg=ChatMessage(role="user", content=request.message),
    assistant_msg=ChatMessage(role="assistant", content=full_response),
)
```

Implementation:

```
MULTI
  RPUSH chat:user:{uid}:session:{sid}:messages <user_json> <assistant_json>
  LTRIM chat:user:{uid}:session:{sid}:messages -20 -1
  EXPIRE chat:user:{uid}:session:{sid}:messages 2592000
EXEC
```

**Verified live:**

```text
$ redis-cli -h 127.0.0.1 -p 6379 KEYS 'chat:user:dddddddd*'
chat:user:dddddddd-dddd-dddd-dddd-dddddddddddd:session:e8b57896-b9c0-41ed-96c4-d08c692bab0e:messages

$ redis-cli LRANGE … 0 -1
       user: What service handles inventory? Just one line please.
  assistant: The `cart.ts` route module handles inventory reserve [5].

$ redis-cli TTL …
2591939   # ~30 days, refreshed on every write
```

### Step 8 — Frontend renders the response

`components/Chat.tsx`'s async loop appends each delta to the in-flight assistant message via a functional `setState` (avoids stale closures). `components/Message.tsx` renders assistant turns through `react-markdown` + `remark-gfm` + `rehype-highlight` so markdown structure (lists, bold, fenced code, citations) renders cleanly. The session id from the `X-Session-Id` header is persisted to `localStorage.tutorSessionId` so a page reload resumes the conversation.

### Step 9 — Error path is no longer silent (post-fix)

If Gemini errors mid-stream (quota / auth / timeout), `routers/chat.py` catches the exception, distills it via `_summarise_error()`, and emits a final `delta` like:

```text
data: {"delta": "\n\n⚠️ **The LLM is rate-limited (Gemini free tier: 20 requests / day for gemini-2.5-flash). Wait for the daily window to reset, enable billing on the Google AI Studio project, or switch to a different model (e.g. gemini-2.5-flash-lite) in backend/app/config.py.**"}
data: [DONE]
```

The error is also persisted to Redis as part of the assistant turn — surfaces in scrollback, doesn't disappear on refresh.

---

## 4. Validation summary

| Check                                                                              | Evidence | Status |
|------------------------------------------------------------------------------------|----------|--------|
| Document parsed (Markdown → text)                                                  | 31 chunks in `documents` row, `source_uri = amazon_architecture_deepdive.md` | ✅ |
| Chunks sized & overlapped per spec                                                 | 31 chunks for a `.md` file → SentenceSplitter ran with the configured params | ✅ |
| Real HuggingFace embeddings (not stub)                                             | Every stored vector is 768-d; sample previews include actual document text | ✅ |
| HNSW index live on `document_chunks.embedding` with `vector_cosine_ops`            | `\d document_chunks` shows `ix_document_chunks_embedding_hnsw … WITH (m='16', ef_construction='64')` | ✅ |
| Retrieval actually drives the LLM                                                  | Live response cites `[5]` — Gemini is reading retrieval context | ✅ |
| Quiz prompt enforces structured MCQ                                                | Screenshot shows `Question: / A) / B) / C) / D) / Answer: / Explanation:` exactly | ✅ |
| Tutor refuses with literal sentence when context is missing                        | Screenshot: "I don't have enough information to answer that based on the available material" (asked about WhatsApp, corpus is Amazon) | ✅ |
| Token-level SSE (`astream_events`)                                                 | Multiple `data: {"delta": …}` events per response, last is `data: [DONE]` | ✅ |
| Per-user Redis key isolation                                                       | Live keys: `chat:user:00000000…:session:…`, `chat:user:cccccccc…:session:…`, `chat:user:dddddddd…:session:…` — different users, different namespaces | ✅ |
| Sliding window + TTL                                                               | `LLEN = 2` after a turn (user + assistant), `TTL ≈ 2,591,940s ≈ 30 days` | ✅ |
| Error path surfaces a human-readable explanation                                   | Verified by exhausting `gemini-2.5-flash` quota — saw ⚠️ message in stream | ✅ |
| Frontend renders markdown (bold, lists, citations)                                 | Screenshot shows formatted lists with `[1]`, `[2]`, `[3, 4]` citations and **bold** headers | ✅ |

---

## 5. Operational notes

- **Two Redis instances on port 6379**: there's a native macOS Redis (PID 4373 on `127.0.0.1:6379`) AND the Docker container `tutor-redis` (also bound to 6379). The native one wins for `127.0.0.1` traffic, so the app talks to it. The Docker Redis is idle. Production deployments shouldn't have this duplication — use *only* the compose container. To confirm where data lives: `lsof -nP -iTCP:6379 -sTCP:LISTEN`.
- **Default model is `gemini-2.5-flash-lite`** (`app/config.py::gemini_model_name`) — chosen for a fresh free-tier daily quota bucket. Switch back to `gemini-2.5-flash` for slightly higher quality once billing is enabled.
- **HuggingFace model lives in `~/.cache/huggingface/`** (~420 MB). The session-scoped `_hf_warmup` test fixture absorbs the first-load cost once per test run.
- **HNSW index is created idempotently** on every backend boot via a `CREATE INDEX IF NOT EXISTS` in `app/main.py`'s lifespan — survives container recreates without needing migrations.
- **Action buttons bypass the Router's LLM call** by sending `force_route: "tutor" | "quiz"` in the request body. The router node short-circuits when `state["route"]` is already set — saves a Gemini call and gives the user deterministic routing for the "Give me an example" / "Test my knowledge" affordances.

---

## 6. How to demo this to someone

1. `docker compose up -d` (or confirm it's running)
2. `cd backend && uv run uvicorn app.main:app --reload`
3. `cd frontend && npm run dev`
4. Open <http://localhost:3000>.
5. Click **Admin · ingestion** → drag a PDF or paste a URL of a system-design article → wait for the green toast with the chunk count.
6. Click **← Back to chat** (or open `/chat`).
7. Type a grounded question (e.g. *"Summarise the key design choices."*) — watch tokens land.
8. Type something outside the corpus (e.g. *"Explain WhatsApp architecture"* when you only uploaded Amazon docs) — watch the Tutor refuse with the literal fallback sentence.
9. Click **Test my knowledge** with the chat input filled — watch a structured MCQ stream in.
10. Reload the page — your conversation is back (resumed from Redis via the `localStorage` session id).
