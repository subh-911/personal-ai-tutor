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

Schema is owned by **Alembic** (`backend/alembic/`). The lifespan no longer creates tables — `alembic upgrade head` is required before booting against a fresh database. The current head is `0002_add_user_id_to_documents` (phase 9), which adds the nullable `documents.user_id` column + a b-tree index for the per-user document list.

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

**Worker** (Phase 10 — runs the heavy embedding work off the API thread):

```bash
cd backend
uv run arq app.workers.ingest_worker.WorkerSettings
```

The worker process loads the sentence-transformers model lazily on the first
job; expect a ~30 s cold-start the first time you ingest after restarting the
worker, then steady-state speed thereafter. It uses Redis DB **1** (sessions
live in DB **0**) so the queue and chat state never collide. Inspect with:

```bash
redis-cli -p 6380 -n 1 KEYS 'arq:*'   # in-flight jobs
docker logs -f tutor-redis            # broker events
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
2. **Upload files** — drag-and-drop PDFs, plain-text, or Markdown files onto the dashed zone (or click it to pick from disk). A two-phase progress bar shows the wire upload first, then the worker's live stage (`chunking → embedding → saving`); a success toast reports the chunk count when ingestion finishes.
3. **Scrape a URL** — paste a public article URL (e.g. a system-design write-up) and click **Scrape**. The backend fetches the page, extracts the main text via BeautifulSoup, and enqueues the chunk + embed work onto the ARQ worker. The toast morphs through stage labels (`Fetching → Chunking → Embedding → Saving`) and lands on success or failure.

Both flows talk to `POST /ingest/upload` and `POST /ingest/scrape`. Phase 10 made these **202 Accepted** routes — they return immediately with a `processing/queued` row, and the client polls `GET /ingest/{id}` until the worker reports `completed` or `failed`. The **Your knowledge base** table below shows live stage badges (`Queued → Chunking → Embedding → Saving → Ready`) per row.

### Knowledge base management

Phase 9 added a **Your knowledge base** table below the upload + scrape cards. It lists everything you've ingested under your Clerk identity — title, source type, chunk count, and date added — sorted most-recent-first. The list auto-refreshes after a successful upload or scrape, and a manual **Refresh** button is available on the card header.

Each row carries a **Delete** action that fires a sonner confirmation toast (Cancel / Delete). Confirming sends `DELETE /documents/{id}` to the backend; the row disappears optimistically and the document's chunks are removed by the existing `ON DELETE CASCADE` on `document_chunks.document_id`. If the delete fails (network, expired JWT), the row is restored and an error toast appears. Dismissing the confirmation toast without choosing does nothing.

**Visibility rules**:
- You only see documents whose `documents.user_id` matches your verified Clerk user id.
- Documents ingested before phase 9 carry `user_id IS NULL` (legacy / unowned). They remain in the corpus and are still retrievable by the chat tutor, but they are invisible in this table and cannot be deleted via the UI.
- Retrieval is still corpus-wide (across users + legacy). Phase 10+ will filter retrieval by `user_id` so the tutor can't ground answers in another user's material.

**Operator escape hatch** for legacy rows — run against the Postgres container:

```sql
-- Claim legacy rows for a specific user (so they appear in that user's table):
UPDATE documents SET user_id = 'user_2abc123def' WHERE user_id IS NULL;

-- Or drop them entirely (chunks cascade):
DELETE FROM documents WHERE user_id IS NULL;
```

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
