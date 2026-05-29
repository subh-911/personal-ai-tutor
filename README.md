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
cp .env.example .env            # see "Redis port" note below if you already had a .env
docker compose up -d            # starts Postgres+pgvector (5432) and Redis (host 6380)
```

> **Redis port (phase 7).** The Docker container exposes Redis on host port **`6380`** (mapped to the container's `6379` internally). This avoids collision with any native macOS `redis-server` running on `6379` — historically that native instance silently shadowed the container and chat data was written to the wrong Redis. If you have a pre-existing `.env` from before phase 7, **regenerate it** from `.env.example` so `REDIS_URL` points at `localhost:6380`.

## Database migrations

Schema is owned by **Alembic** (`backend/alembic/`). The lifespan no longer creates tables — `alembic upgrade head` is required before booting against a fresh database.

```bash
cd backend
uv sync
uv run alembic upgrade head     # fresh database → creates documents, document_chunks, HNSW index
```

If you already have a populated database from phase 6 (with `documents` / `document_chunks` already present), **stamp** it at the baseline instead — non-destructive, just records the current revision so future migrations apply on top:

```bash
uv run alembic stamp head
```

Validate the migration applies cleanly to an empty DB at any time:

```bash
docker exec tutor-postgres psql -U tutor -d postgres -c "CREATE DATABASE tutor_migration_test"
DATABASE_URL="postgresql+asyncpg://tutor:tutor@localhost:5432/tutor_migration_test" \
  uv run alembic upgrade head
docker exec tutor-postgres psql -U tutor -d tutor_migration_test -c "\d document_chunks"
docker exec tutor-postgres psql -U tutor -d postgres -c "DROP DATABASE tutor_migration_test"
```

## Run the stack (three terminals)

**Backend** (`http://localhost:8000`, Swagger UI at `/docs`):

```bash
cd backend
uv sync
uv run alembic upgrade head     # idempotent; safe to re-run
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

## Authentication (Clerk)

Phase 8 wires real identity in front of the API. The frontend uses **Clerk** for sign-in / sign-up; the backend verifies the Clerk-issued JWT against Clerk's published JWKS on every `/chat` and `/ingest/*` request. Requests without a valid token receive `401 Unauthorized`.

**Setup**

1. Create a Clerk application at <https://dashboard.clerk.com>.
2. Frontend keys — copy `.env.local.example` to `.env.local` in `frontend/` and fill:
   - `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (from Clerk → API Keys)
   - `CLERK_SECRET_KEY` (same page)
3. Backend keys — in the root `.env`, fill:
   - `CLERK_JWKS_URL` — your application's JWKS endpoint, typically `https://<your-frontend-api>.clerk.accounts.dev/.well-known/jwks.json`
   - `CLERK_ISSUER` — your application's Frontend API URL, typically `https://<your-frontend-api>.clerk.accounts.dev`

**Behaviour**

- Unsigned-in visitors hitting `/chat` or `/admin` are redirected to `/sign-in`. The Clerk-hosted flow handles email/password, OAuth, MFA, etc.
- Authenticated requests carry `Authorization: Bearer <jwt>`. The backend's `get_user_id` dependency verifies the signature (RS256, JWKS-cached), checks issuer + expiry, and extracts the `sub` claim — a Clerk user id like `user_2abc123def`.
- Session memory is keyed by the *verified* user id: `chat:user:user_2abc123def:session:<uuid>:messages`. Two callers presenting different JWTs but sharing a session UUID get completely separate Redis namespaces.

**Dev escape hatch: `DEV_AUTH_BYPASS=1`**

For backend-only smoke testing (curl, agent tests outside the full stack), set `DEV_AUTH_BYPASS=1` in `.env`. The auth dependency reverts to permissive Bearer-as-user-id parsing — any Bearer value is accepted verbatim as the user id, and missing headers fall back to a fixed anonymous string. The backend logs a `WARNING` on boot whenever bypass is enabled. **Never set this in production.** The Playwright e2e suite (`npm run test:e2e`) uses this flag automatically via `playwright.config.ts` so the hermetic test environment doesn't need real Clerk credentials.

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

Drives a real browser against the live stack. The chat specs mock the SSE stream and the admin specs mock the ingestion responses, so the suite is independent of LLM quota / network reach. Phase 8: Playwright launches both servers with auth bypass (`DEV_AUTH_BYPASS=1` on the backend, `NEXT_PUBLIC_TEST_DISABLE_AUTH=1` on the frontend) so no real Clerk credentials are needed for the test suite.

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
