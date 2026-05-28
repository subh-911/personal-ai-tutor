# personal-ai-tutor

A self-hosted, full-stack AI tutor. Ingest documents (PDF / TXT / Markdown / URLs), then chat with a LangGraph orchestrator that routes between a Tutor agent (retrieval-augmented explanations) and a Quiz agent (knowledge checks). Conversation history is server-managed in Redis; documents and embeddings live in Postgres + pgvector.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design, [`DIAGRAMS.md`](./DIAGRAMS.md) for the LangGraph flow, and [`tasks_summary`](./tasks_summary) for the phase-by-phase build log.

## Prerequisites

- **Docker** + **Compose v2** (Docker Desktop on macOS / Windows, native on Linux).
- **[uv](https://github.com/astral-sh/uv)** — Python package manager. Install with `pipx install uv` (or `brew install uv`).
- **Node 20+** and **npm**.
- **Google AI Studio API key** for Gemini. Get one at <https://aistudio.google.com/> and put it in `.env` as `GOOGLE_API_KEY=...`. Without it, the backend boots and ingestion still works, but `/chat` requests fail at the LLM call.

## One-time setup

```bash
git clone <this repo>
cd personal-ai-tutor
cp .env.example .env
docker compose up -d            # starts Postgres+pgvector and Redis
```

## Run the stack (three terminals)

**Backend** (`http://localhost:8000`, Swagger UI at `/docs`):

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

**Frontend** (`http://localhost:3000`):

```bash
cd frontend
npm install
npm run dev
```

**Open the app**: <http://localhost:3000> → click **Open chat**.

Try:
- Type *"Explain CAP theorem"* → **Send** — the Router classifies it as a concept question and the Tutor agent answers with retrieval-grounded text.
- Type *"Raft consensus"* → **Test my knowledge** — bypasses the Router and forces the Quiz agent to generate an MCQ.
- Reload the page — your `session_id` is persisted to `localStorage` and the backend resumes the conversation from Redis.

## Admin UI

There's a small Admin · Ingestion page for feeding documents into the corpus without leaving the browser:

1. Open <http://localhost:3000/admin> (or click **Admin · ingestion** from the landing page).
2. **Upload files** — drag-and-drop PDFs, plain-text, or Markdown files onto the dashed zone (or click it to pick from disk). A per-file progress bar shows upload progress; a success toast reports the chunk count when ingestion finishes.
3. **Scrape a URL** — paste a public article URL (e.g. a system-design write-up) and click **Scrape**. The backend fetches the page, extracts the main text via BeautifulSoup, and runs the full ingestion pipeline. A success or error toast appears when the call returns.

Both flows talk to the existing `POST /ingest/upload` and `POST /ingest/scrape` endpoints; nothing else is required.

## Sample data via the API

If you'd rather skip the UI, the same routes accept plain `curl`:

```bash
curl -F "file=@/path/to/study-material.pdf" -F "title=My notes" \
     http://localhost:8000/ingest/upload

# or scrape a URL
curl -X POST http://localhost:8000/ingest/scrape \
     -H 'Content-Type: application/json' \
     -d '{"url": "https://example.com/article"}'
```

## Authentication (lightweight)

`/chat` accepts an optional `Authorization: Bearer <user-uuid>` header. The value is used to namespace Redis session keys (`chat:user:<user-uuid>:session:<session-uuid>:messages`) so a leaked session id alone can't read another user's history. Requests without a header fall back to a fixed anonymous user UUID — fine for single-user local dev. Real auth (JWT/OAuth) slots in by replacing the `app/auth.py::get_user_id` dependency.

## Tests

**Backend integration tests** (Postgres + Redis must be running):

```bash
cd backend
uv run pytest -v
```

Covers `/health`, ingestion (PDF + HF-embedding integration tests, mime rejection, status polling), agents (router dispatch, pgvector retrieval, force-route override, history accumulation, user-scoped session isolation), and the chat-session SSE + Redis assertions. Tests that cross the LLM boundary skip cleanly when `GOOGLE_API_KEY` is unset.

**Frontend end-to-end** (Postgres + Redis must be running; Playwright auto-starts backend + frontend):

```bash
cd frontend
npx playwright install chromium    # one-time
npm run test:e2e
```

Drives a real browser against the live stack. The chat specs mock the SSE stream and the admin specs mock the ingestion responses, so the suite is independent of LLM quota / network reach.

## Reset state

```bash
docker compose down -v     # wipes Postgres + Redis volumes
```

## Where to look next

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — components, data stores, agents layer, session memory, SSE protocol.
- [`DIAGRAMS.md`](./DIAGRAMS.md) — Mermaid renderings of the LangGraph flow and request lifecycle.
- <http://localhost:8000/docs> — interactive OpenAPI spec for every backend endpoint.
- `tasks_summary` — what landed in each build phase, plus what's deliberately deferred.

## Project layout

```
personal-ai-tutor/
├── backend/        # FastAPI + LangGraph + pgvector + Redis
├── frontend/       # Next.js (App Router) + Tailwind + Playwright
├── postgres/init/  # pgvector extension bootstrap
├── docker-compose.yml
├── ARCHITECTURE.md
├── DIAGRAMS.md
├── README.md
└── tasks_summary
```
