# Personal AI Tutor — Backend Code Explained

> **Audience**: You're new to FastAPI backend development and want to understand every layer of this project — then carry those patterns into your own future projects.
>
> **How to read this**: Each section walks through one file (or group of files), explains *what* the code does, *why* it was written that way, and points out the FastAPI / Python pattern for you to remember.

---

## Table of Contents

1. [Project Overview & Mental Model](#1-project-overview--mental-model)
2. [`main.py` — App Entrypoint](#2-mainpy--app-entrypoint)
3. [`config.py` — Environment-Driven Settings](#3-configpy--environment-driven-settings)
4. [`auth.py` — JWT Authentication Dependency](#4-authpy--jwt-authentication-dependency)
5. [`db.py` — Async Database Engine & Session](#5-dbpy--async-database-engine--session)
6. [`models.py` — SQLAlchemy ORM Models](#6-modelspy--sqlalchemy-orm-models)
7. [Alembic Migrations](#7-alembic-migrations-alembicversions)
8. [`redis_client.py` — Redis Connection](#8-redis_clientpy--redis-connection)
9. [`services/session.py` — Chat Session Memory](#9-servicessessionpy--chat-session-memory)
10. [`routers/ingest.py` — Ingestion API](#10-routersingestpy--ingestion-api)
11. [`services/parser.py` — File & URL Parsing](#11-servicesparserpy--file--url-parsing)
12. [`services/chunker.py` — Text Chunking](#12-serviceschunkerpy--text-chunking)
13. [`services/embeddings.py` — Embedding Models](#13-servicesembeddingspy--embedding-models)
14. [`services/ingest.py` — Ingestion Orchestration](#14-servicesingestpy--ingestion-orchestration)
15. [`workers/ingest_worker.py` — ARQ Background Worker](#15-workersingest_workerpy--arq-background-worker)
16. [`services/retrieval.py` — Vector Similarity Search](#16-servicesretrievalpy--vector-similarity-search)
17. [`routers/chat.py` — Streaming SSE Chat Endpoint](#17-routerschatpy--streaming-sse-chat-endpoint)
18. [`agents/router.py` & `agents/graph.py` — LangGraph Orchestration](#18-agentsrouterpy--agentsgraphpy--langgraph-orchestration)
19. [`agents/llm.py` — Gemini LLM Provider](#19-agentsllmpy--gemini-llm-provider)
20. [Agent Implementations: tutor, quiz, smalltalk, state](#20-agent-implementations-tutor-quiz-smalltalk-state)
21. [`routers/documents.py` — Document Management](#21-routersdocumentspy--document-management)
22. [`routers/health.py` & `schemas/`](#22-routershealthpy--schemas)
23. [`tests/` and `end-to-end-flow.md`](#23-tests-and-end-to-end-flowmd)
24. [Summary: FastAPI Patterns Reference](#24-summary-fastapi-patterns-reference)

---

## 1. Project Overview & Mental Model

Before reading a single line of code, you need a mental model of what this project *is*.

**Personal AI Tutor** is a full-stack app where users:
1. **Ingest** documents (PDF, text, URLs) into a knowledge base.
2. **Chat** with an LLM that answers questions *grounded in their uploaded material*.

The backend is a **FastAPI** server with four responsibilities:

```
Browser ──HTTP──► FastAPI backend ──SQL──► Postgres + pgvector
                        │         ──Redis CMD──► Redis
                        │         ──HTTP──► Google Gemini API
                        │
               (ARQ Worker process, separate)
                        │
                  Heavy embedding work (HuggingFace model)
```

### The two big flows to internalise

**Flow A — Ingestion** (document → database):
```
POST /ingest/upload
   ↓ parse file (PDF/text/HTML)
   ↓ create Document row in DB (status=processing, stage=queued)
   ↓ enqueue job to ARQ worker (via Redis queue on DB 1)
   ↓ return 202 immediately (non-blocking)

ARQ Worker (background process):
   ↓ chunk text into 512-token windows (128 overlap)
   ↓ embed each chunk with HuggingFace sentence-transformers
   ↓ bulk INSERT into document_chunks with 768-d vectors
   ↓ set Document.status = "completed"
```

**Flow B — Chat** (question → streamed answer):
```
POST /chat
   ↓ verify JWT (Clerk) → user_id
   ↓ load chat history from Redis
   ↓ LangGraph graph.astream_events(...)
       ↓ router_node: Gemini classifies intent → "tutor"|"quiz"|"smalltalk"
       ↓ tutor_node (or quiz/smalltalk):
           ↓ embed query → cosine ANN search → top-4 chunks from Postgres
           ↓ astream tokens from Gemini
   ↓ forward token deltas as SSE ("data: {delta: ...}\n\n")
   ↓ append (user_msg, assistant_msg) to Redis session
   ↓ send "data: [DONE]\n\n"
```

---

## 2. `main.py` — App Entrypoint

**File**: `backend/app/main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import engine
from app.redis_client import redis as redis_client
from app.routers import chat, documents, health, ingest
from app.workers.ingest_worker import get_arq_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.arq_pool = await get_arq_pool()  # open ARQ Redis connection pool
    try:
        yield                                   # app runs here — handles requests
    finally:
        await app.state.arq_pool.aclose()       # cleanup on shutdown
        await engine.dispose()
        await redis_client.aclose()

app = FastAPI(
    title="Personal AI Tutor API",
    version="0.1.0",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(documents.router)
app.include_router(chat.router)
```

### What's happening here

**`FastAPI()` is the app factory**. You pass metadata (`title`, `version`, `description`) which shows up in the auto-generated Swagger UI at `/docs`. The `openapi_tags` list adds human-readable descriptions to each group of routes in the docs.

**`lifespan` is the startup/shutdown hook**. This is the modern FastAPI pattern (replacing the older `@app.on_event("startup")`). The `@asynccontextmanager` decorator turns a generator function into a context manager:
- Everything **before** `yield` runs at startup.
- Everything **after** `yield` (in `finally`) runs at shutdown — even if the app crashes.

Why open an ARQ pool at startup instead of per-request? Opening a TCP connection pool to Redis has a one-time cost. If we did it per-request, every ingest call would pay that cost. We store the pool on `app.state.arq_pool` — FastAPI's application-level state bag — and read it back in routes via a dependency.

**`app.include_router()`** mounts each router. Each router lives in its own file and has its own prefix (like `/ingest`). This is how FastAPI organizes routes across many files without putting everything in `main.py`.

### FastAPI pattern to remember

> **Lifespan for resources**: Use `lifespan` to open connections once at boot and close them gracefully at shutdown. Store shared resources on `app.state`.

---

## 3. `config.py` — Environment-Driven Settings

**File**: `backend/app/config.py`

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://tutor:tutor@localhost:5432/tutor"
    redis_url: str = "redis://localhost:6380/0"

    embedding_dim: int = 768
    embedding_model_name: str = "sentence-transformers/all-mpnet-base-v2"
    max_upload_bytes: int = 25 * 1024 * 1024
    scrape_timeout_seconds: float = 30.0

    google_api_key: str | None = None
    gemini_model_name: str = "gemini-2.5-flash-lite"

    session_history_turns: int = 10
    session_ttl_seconds: int = 30 * 24 * 3600  # 30 days

    clerk_jwks_url: str | None = None
    clerk_issuer: str | None = None
    dev_auth_bypass: bool = False

    arq_redis_db: int = 1  # ARQ uses DB 1; sessions use DB 0

    @property
    def arq_redis_url(self) -> str:
        # Derives ARQ URL from redis_url, swapping DB number only
        parsed = urlparse(self.redis_url)
        netloc = parsed.netloc or "localhost:6379"
        return f"{parsed.scheme or 'redis'}://{netloc}/{self.arq_redis_db}"

settings = Settings()
```

### What's happening here

**`pydantic_settings.BaseSettings`** is the standard FastAPI pattern for config. It reads values from (highest to lowest priority):
1. Environment variables
2. The `.env` file specified by `env_file`
3. The Python defaults in the class body

Field names are case-insensitive: `DATABASE_URL` in `.env` maps to `database_url`.

**`_PROJECT_ROOT = Path(__file__).resolve().parents[2]`** — `__file__` is `backend/app/config.py`. `.parents[0]` = `backend/app/`, `.parents[1]` = `backend/`, `.parents[2]` = project root. This trick ensures `.env` is found regardless of which directory the process starts from.

**`extra="ignore"`** — silently ignores any `.env` keys that don't have matching class fields. Without this, pydantic raises `ValidationError` on extra keys (e.g., frontend keys that don't belong in the backend config).

**`@property arq_redis_url`** is a computed property: derives the ARQ queue URL from `redis_url` but on DB 1. This avoids requiring operators to set two nearly-identical Redis URLs — change `redis_url`, and `arq_redis_url` follows automatically.

**`settings = Settings()`** at module level creates a singleton. Every other module does `from app.config import settings`.

### FastAPI pattern to remember

> **`pydantic_settings.BaseSettings` for config**: All env vars, defaults, and types in one place. Use `@property` for derived values. Keep a module-level `settings` singleton.

---

## 4. `auth.py` — JWT Authentication Dependency

**File**: `backend/app/auth.py`

```python
from fastapi import Header, HTTPException, status
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError, decode as jwt_decode
from app.config import settings

# Module-level lazy singleton — initialized on first real auth request
_jwk_client: PyJWKClient | None = None

def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        if not settings.clerk_jwks_url:
            raise HTTPException(500, "CLERK_JWKS_URL is not configured")
        _jwk_client = PyJWKClient(settings.clerk_jwks_url)
    return _jwk_client


async def get_user_id(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency — called automatically on every protected route."""

    # DEV BYPASS: accept any Bearer value verbatim (no verification)
    if settings.dev_auth_bypass:
        if not authorization:
            return ANONYMOUS_USER_ID
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return ANONYMOUS_USER_ID
        return value.strip()

    # PRODUCTION: verify JWT signature, issuer, expiry
    if not authorization:
        raise _credential_error()   # 401
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _credential_error()

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        payload = jwt_decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            options={"require": ["sub", "iss", "exp"]},
        )
    except (InvalidTokenError, PyJWKClientError) as exc:
        log.info("rejected JWT: %s", exc)
        raise _credential_error() from exc

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _credential_error()
    return sub  # e.g. "user_2abc123def"
```

### What's happening here

**`get_user_id` is a FastAPI dependency**. Any route declaring `user_id: str = Depends(get_user_id)` has this function called automatically before the route handler runs. If it raises `HTTPException`, FastAPI returns the error and the route handler never executes.

**`authorization: str | None = Header(default=None)`** — the `Header(...)` tells FastAPI to read from the HTTP `Authorization` header. `default=None` means a missing header gives `None` instead of a 400 error (which lets us write the "missing header → 401" check ourselves).

**`PyJWKClient`** (from `PyJWT`) fetches Clerk's published JWKS (JSON Web Key Set — the public RSA keys used to verify JWTs). It's lazy-created and cached once at module level. We don't make an HTTP call to Clerk on every request, just on first use.

**Two-mode design (`dev_auth_bypass`)**:
- **Production** (`False`): verifies JWT signature (RS256), checks `iss` (issuer matches `clerk_issuer`) and `exp` (token not expired), returns the `sub` claim (Clerk user ID like `user_2abc123def`).
- **Dev bypass** (`True`): accepts any Bearer value verbatim — useful for `curl` smoke testing. The app logs a `WARNING` on boot. **Never production.**

**Why always return 401, never 403?** If a user provides a JWT belonging to someone else's resource, you return 401 (same as unauthenticated). This prevents leaking: "this resource exists and belongs to someone else."

### FastAPI pattern to remember

> **Dependencies for cross-cutting concerns**: Auth, rate limiting, DB sessions — use `Depends(fn)`. Dependencies can raise `HTTPException` to short-circuit. They can also declare their own `Header(...)`, `Query(...)`, or `Depends(...)` parameters — FastAPI resolves them recursively.

---

## 5. `db.py` — Async Database Engine & Session

**File**: `backend/app/db.py`

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import settings

engine = create_async_engine(settings.database_url, future=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session
```

### What's happening here

**`create_async_engine`** creates a SQLAlchemy async engine backed by `asyncpg` (note `+asyncpg` in the URL). Under the hood it manages a connection pool. `future=True` enables SQLAlchemy 2.0 API style.

**`expire_on_commit=False`** is critical for async. By default, SQLAlchemy expires loaded objects after `session.commit()`, requiring a re-load on next access. In async mode, lazy loading raises a `MissingGreenlet` error because async doesn't support implicit I/O. Setting `False` means committed objects retain their in-memory values.

**`get_session`** is a yield-based FastAPI dependency. The `async with async_session_maker() as session:` context manager:
- Opens a DB session on entry.
- **Commits** automatically if no exception was raised.
- **Rolls back** if an exception was raised.
- Closes the session in all cases.

When a route uses `session: AsyncSession = Depends(get_session)`, FastAPI:
1. Calls `get_session()`, enters the `async with`.
2. Injects the session.
3. When the route exits (normal or exception), the `async with` exits and cleans up.

### FastAPI pattern to remember

> **Yield-based dependencies for resource cleanup**: Code before `yield` = setup. Code after `yield` = cleanup (runs even on exceptions). FastAPI handles this automatically.

---

## 6. `models.py` — SQLAlchemy ORM Models

**File**: `backend/app/models.py`

```python
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    doc_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Relationship: one Document → many DocumentChunks
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_per_document"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=False)
    chunk_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    document: Mapped["Document"] = relationship(back_populates="chunks")
```

### What's happening here

**`DeclarativeBase`** is SQLAlchemy 2.0's model base class. All your models inherit from `Base` — SQLAlchemy uses it to register the class-to-table mapping.

**`Mapped[T]` + `mapped_column(...)`** is the SQLAlchemy 2.0 typed column declaration. `Mapped[str]` tells Python/IDE the Python type; `mapped_column(String(16))` tells SQLAlchemy the SQL type and constraints. `Mapped[str | None]` = nullable column.

**`JSONB` vs `JSON`**: JSONB stores JSON as binary (parsed at write time), enabling indexing and operator queries. `JSON` stores raw text. Always prefer JSONB in Postgres for metadata that might be queried later.

**`server_default=func.now()`** sets `DEFAULT now()` at the **database level** — correct even for records inserted directly via SQL, bypassing the ORM.

**`onupdate=func.now()`** automatically sets `updated_at = now()` on every ORM UPDATE — a very common audit trail pattern.

**`cascade="all, delete-orphan"` + `passive_deletes=True`**: when a Document is deleted, SQLAlchemy trusts the DB's `ON DELETE CASCADE` constraint to delete all its chunks automatically (instead of loading all chunks into Python memory and deleting them one-by-one — a huge performance difference for documents with many chunks).

**The HNSW index** on `embedding`:
- `postgresql_using="hnsw"` — Hierarchical Navigable Small World graph index for fast approximate nearest-neighbor (ANN) search.
- `postgresql_ops={"embedding": "vector_cosine_ops"}` — use cosine distance.
- `m=16` — connections per node (higher = better recall, larger index). `ef_construction=64` — search width during index build (higher = better quality, slower build).

### FastAPI pattern to remember

> **SQLAlchemy 2.0 `Mapped[T]` for type safety**: Always use `passive_deletes=True` with `ON DELETE CASCADE` for performance. `server_default` = DB-level default; `default` = Python-level default (only used by ORM).

---

## 7. Alembic Migrations (`alembic/versions/`)

**Files**: `0001_baseline.py`, `0002_add_user_id_to_documents.py`, `0003_add_document_stage.py`

### What Alembic is

Alembic is the schema migration tool for SQLAlchemy. Each migration file has `upgrade()` and `downgrade()` functions. Alembic tracks the applied version in an `alembic_version` table.

```
0001_baseline                ← creates documents, document_chunks, HNSW index
    ↓ (down_revision chain)
0002_add_user_id             ← adds documents.user_id + b-tree index
    ↓
0003_add_document_stage      ← adds documents.stage column
```

### Reading `0001_baseline.py`

```python
def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")   # pgvector

    op.create_table("documents", ...)

    op.create_table("document_chunks",
        sa.Column("embedding", Vector(768), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunk_per_document"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])

    # Raw SQL — Alembic's create_index() can't express pgvector HNSW opclass syntax
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    # Note: vector extension NOT dropped — it might be used by other databases in the cluster
```

**`op.execute()` for raw SQL**: when Alembic's ORM-level helpers can't express what you need (e.g., pgvector-specific index syntax), use raw SQL. This is an escape hatch, not a code smell.

### Reading incremental migrations

```python
# 0002_add_user_id_to_documents.py
def upgrade() -> None:
    op.add_column("documents", sa.Column("user_id", sa.String(64), nullable=True))
    op.create_index("ix_documents_user_id", "documents", ["user_id"])

def downgrade() -> None:
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_column("documents", "user_id")
```

**`nullable=True` on new columns**: existing rows automatically get `NULL` for the new column — a safe, non-destructive migration. Never add a `NOT NULL` column without a `DEFAULT` to existing tables (it would fail on Postgres if there are existing rows).

### FastAPI pattern to remember

> **Never `Base.metadata.create_all()` in production**: that wipes and recreates tables. Use Alembic migrations — versioned, reversible, and applied incrementally. Every `upgrade()` needs a `downgrade()`.

---

## 8. `redis_client.py` — Redis Connection

**File**: `backend/app/redis_client.py`

```python
from redis.asyncio import Redis, from_url
from app.config import settings

redis: Redis = from_url(settings.redis_url, decode_responses=True)

async def get_redis() -> Redis:
    return redis
```

### What's happening here

`from_url(settings.redis_url, decode_responses=True)` creates a **connection pool** at module import time. Each coroutine that makes a Redis call borrows a connection from the pool, uses it, and returns it. No TCP connection is created per request.

`decode_responses=True` — critical. By default `redis-py` returns `bytes`. With this flag, all responses are decoded to `str` (UTF-8). `lrange` returns `list[str]`, not `list[bytes]`, so you can pass values directly to `json.loads` or Pydantic's `model_validate_json`.

`settings.redis_url = "redis://localhost:6380/0"` — the `/0` at the end selects logical Redis database 0. Sessions live on DB 0; ARQ job queue lives on DB 1. Same Redis container, logically separated key spaces.

### FastAPI pattern to remember

> **Module-level singleton for connection pools**: Create one pool at import time. Never create a new Redis connection per request. The library handles multiplexing.

---

## 9. `services/session.py` — Chat Session Memory

**File**: `backend/app/services/session.py`

```python
SESSION_KEY_PREFIX = "chat:user:"

class SessionStore:
    """Redis-backed sliding window of ChatMessages, keyed by (user_id, session_id)."""

    def _key(self, user_id: str, session_id: UUID) -> str:
        return f"{SESSION_KEY_PREFIX}{user_id}:session:{session_id}:messages"
        # Example: "chat:user:user_2abc123def:session:f1e2d3c4-...:messages"

    async def get_history(self, user_id: str, session_id: UUID) -> list[ChatMessage]:
        items = await self.redis.lrange(self._key(user_id, session_id), 0, -1)
        return [ChatMessage.model_validate_json(item) for item in items]

    async def append_turn(self, user_id, session_id, *, user_msg, assistant_msg) -> None:
        key = self._key(user_id, session_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, user_msg.model_dump_json(), assistant_msg.model_dump_json())
            pipe.ltrim(key, -self.max_messages, -1)  # keep only the last N messages
            pipe.expire(key, self.ttl_seconds)        # reset 30-day sliding TTL
            await pipe.execute()                      # MULTI/EXEC — atomic

    async def clear(self, user_id: str, session_id: UUID) -> None:
        await self.redis.delete(self._key(user_id, session_id))
```

### What's happening here

**The data structure**: Each conversation is a Redis **LIST**. Every element is a JSON-serialized `ChatMessage` (role + content). The list key is `chat:user:{user_id}:session:{session_id}:messages`.

**Why include `user_id` in the key?** Two users with the same `session_id` get completely separate lists. A leaked session UUID alone cannot read another user's history.

**`lrange(key, 0, -1)`**: reads the entire list. `0` = first element, `-1` = last element (Redis uses inclusive indices, and `-1` means "last").

**`ltrim(key, -max_messages, -1)`**: keeps only the last `max_messages` elements. With `session_history_turns=10`, `max_messages = 20` (10 user + 10 assistant). Messages older than the window silently fall off.

**`pipeline(transaction=True)`**: RPUSH + LTRIM + EXPIRE as one atomic `MULTI`/`EXEC` block. Without atomicity, a crash between RPUSH and LTRIM would leave the list un-trimmed; between LTRIM and EXPIRE, you'd have a list without a TTL reset.

**TTL = sliding window**: every `append_turn` resets the expiry to 30 days from *now*. Active conversations never expire; dormant ones disappear after 30 days of inactivity.

### FastAPI pattern to remember

> **Use Redis pipelining for multi-command atomicity**: When 2+ Redis commands must happen together (push + trim + expire), use `pipeline(transaction=True)` to wrap them in MULTI/EXEC.

---

## 10. `routers/ingest.py` — Ingestion API

**File**: `backend/app/routers/ingest.py`

```python
router = APIRouter(prefix="/ingest", tags=["ingest"])

@router.post(
    "/upload",
    response_model=IngestStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
    arq_pool: ArqRedis = Depends(get_arq_pool_dep),
) -> IngestStatus:
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, ...)

    try:
        parsed = parse_upload(filename=file.filename, content_type=file.content_type, data=data)
    except UnsupportedMediaError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc

    document = await create_pending_document(
        session, parsed, source_type="upload", source_uri=file.filename, user_id=user_id, ...
    )
    await arq_pool.enqueue_job("embed_document", document_id=str(document.id), text=parsed.text)
    return await _to_status(session, document)
```

### What's happening here

**`APIRouter(prefix="/ingest", tags=["ingest"])`** groups related routes. The prefix means all routes in this file are under `/ingest/...`. The tag groups them in the Swagger docs.

**Multipart form-data** — `UploadFile = File(...)` declares a file upload field. `title: str | None = Form(None)` declares an optional text form field. These use multipart encoding (`Content-Type: multipart/form-data`), which is different from JSON body. You can't mix `File()/Form()` with a Pydantic `Body()` model in the same endpoint.

**`status_code=202 ACCEPTED`** — means "I received your request and will process it, but the result isn't ready yet." Embedding a large PDF takes 5–30 seconds. With 202, the client gets the document ID immediately and polls `GET /ingest/{id}` for progress. This is the **non-blocking async work pattern**.

**Dependency injection anatomy** — this one route receives 5 parameters:
- `file`, `title` — from the HTTP multipart body
- `session` — `Depends(get_session)` — FastAPI opens a DB transaction
- `user_id` — `Depends(get_user_id)` — FastAPI runs JWT verification
- `arq_pool` — `Depends(get_arq_pool_dep)` — FastAPI reads from `app.state`

FastAPI resolves all dependencies before calling the route. If `get_user_id` raises 401, the route handler never runs.

**The two-phase design**:
1. **Route (HTTP thread)**: parse → create DB row → enqueue job → return 202.
2. **Worker (background process)**: dequeue → chunk → embed → store → update status.

**The poll endpoint** (`GET /ingest/{id}`):
```python
document = await session.scalar(
    select(Document).where(
        Document.id == ingestion_id,
        (Document.user_id == user_id) | (Document.user_id.is_(None)),
    )
)
```
The ownership filter: your documents OR legacy rows (uploaded before per-user ownership existed). If you poll someone else's document ID, you get 404 — existence is not leaked.

### FastAPI pattern to remember

> **`status_code=202` for delegated async work**: If a route enqueues a background job, return 202 and give the client an ID to poll. Never make HTTP connections wait for slow background computation.

---

## 11. `services/parser.py` — File & URL Parsing

**File**: `backend/app/services/parser.py`

```python
@dataclass
class ParsedDocument:
    text: str
    title: str | None = None
    metadata: dict = field(default_factory=dict)

class UnsupportedMediaError(ValueError):
    pass  # raised by parse_upload; caught by router → HTTP 415

def parse_pdf(data: bytes) -> ParsedDocument:
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    info = reader.metadata or {}
    title = info.title or None
    return ParsedDocument(text=text, title=title, metadata={"page_count": len(reader.pages)})

def parse_html(html: str, base_url: str) -> ParsedDocument:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()                          # strip non-content tags
    root = soup.find("main") or soup.body or soup
    text = "\n".join(line.strip() for line in root.get_text("\n").splitlines() if line.strip())
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    return ParsedDocument(text=text, title=title, metadata={"format": "html", "source_url": base_url})

async def fetch_url(url: str) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=settings.scrape_timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text, str(response.url)  # text + final URL (after redirects)

def parse_upload(*, filename: str, content_type: str | None, data: bytes) -> ParsedDocument:
    mime = (content_type or "").lower()
    lower_name = filename.lower()
    if mime == PDF_MIME or lower_name.endswith(".pdf"):
        return parse_pdf(data)
    if mime in SUPPORTED_TEXT_MIMES or lower_name.endswith((".txt", ".md", ".markdown")):
        return parse_text(data, effective_mime)
    raise UnsupportedMediaError(f"unsupported upload type: {filename!r}")
```

### What's happening here

**`ParsedDocument` is an internal dataclass** — not a Pydantic model, because it never crosses the HTTP boundary. Use dataclasses for internal value objects, Pydantic for HTTP request/response shapes.

**MIME + filename double-check** in `parse_upload`: browsers sometimes lie about `Content-Type` (e.g., a `.pdf` sent as `application/octet-stream`). Checking both MIME and filename extension ensures robust detection.

**BeautifulSoup HTML parsing**:
- `.decompose()` removes tags (and their contents) from the parse tree — without this, you'd get JS source code in your extracted text.
- `soup.find("main") or soup.body or soup` — tries semantic `<main>` element first, falls back to `<body>`, then the root. Avoids nav/header/footer noise.

**`httpx.AsyncClient`** — the async HTTP client. Used instead of synchronous `requests` because we're in an `async def`. `follow_redirects=True` follows 301/302 chains and `str(response.url)` gives the final URL (after all redirects).

**Service exception → router HTTP response pattern**: `UnsupportedMediaError` is a plain Python exception. The router catches it and wraps it in `HTTPException(415)`. This keeps services testable without HTTP infrastructure.

### FastAPI pattern to remember

> **Services raise domain exceptions; routers translate them to HTTP status codes**. Keep business logic in services (plain Python). Keep HTTP translation in routers.

---

## 12. `services/chunker.py` — Text Chunking

**File**: `backend/app/services/chunker.py`

```python
from llama_index.core.node_parser import SentenceSplitter

CHUNK_SIZE = 512       # tokens per chunk — matches all-mpnet-base-v2's max input
CHUNK_OVERLAP = 128    # tokens of overlap between adjacent chunks

@dataclass
class ChunkPayload:
    index: int
    content: str
    token_count: int

_splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
_tokenizer = get_tokenizer()  # LlamaIndex's default tokenizer (cl100k_base)

def chunk_text(text: str) -> list[ChunkPayload]:
    if not text.strip():
        return []
    nodes = _splitter.get_nodes_from_documents([LlamaDocument(text=text)])
    return [
        ChunkPayload(index=i, content=node.get_content(), token_count=_count_tokens(node.get_content()))
        for i, node in enumerate(nodes)
    ]
```

### What's happening here

**Why chunk at all?** LLMs have context windows (typically 8k–128k tokens). You can't embed an entire PDF into one prompt. Instead, you store chunks as vectors; at query time, you retrieve only the most relevant chunks to fit in the prompt.

**Why 512 tokens / 128 overlap?**
- 512 tokens ≈ a paragraph or two — enough semantic context, precise enough for retrieval.
- 128 token overlap ensures a concept split across a chunk boundary appears fully in at least one chunk.
- `all-mpnet-base-v2`'s max input is 512 tokens — chunks match the model's capacity exactly.

**`SentenceSplitter`** (LlamaIndex) respects sentence boundaries. A naive character-split would cut sentences mid-word. The splitter finds natural break points while staying under the token budget.

**Module-level `_splitter` and `_tokenizer`** — created once at import time. Tokenizer initialization loads a vocabulary file; doing it per-call would be wasteful.

### FastAPI pattern to remember

> **Module-level singletons for expensive but stateless objects**: Tokenizers, splitters — create once, reuse on every call. Safe in single-process servers.

---

## 13. `services/embeddings.py` — Embedding Models

**File**: `backend/app/services/embeddings.py`

```python
class EmbeddingProvider(Protocol):
    """Structural protocol — any class with these members satisfies it, no inheritance needed."""
    dimension: int
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

class HuggingFaceEmbeddingProvider:
    _model: SentenceTransformer | None = None
    _load_lock: Lock = Lock()  # protects concurrent first-call initialization
    dimension: int = settings.embedding_dim  # 768

    def _get_model(self) -> SentenceTransformer:
        if HuggingFaceEmbeddingProvider._model is None:
            with HuggingFaceEmbeddingProvider._load_lock:
                if HuggingFaceEmbeddingProvider._model is None:  # double-checked locking
                    model = SentenceTransformer(self.model_name)
                    actual = model.get_sentence_embedding_dimension()
                    if actual != self.dimension:
                        raise RuntimeError(f"embedding model dimension mismatch: {actual} vs {self.dimension}")
                    HuggingFaceEmbeddingProvider._model = model
        return HuggingFaceEmbeddingProvider._model

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        # model.encode() is synchronous CPU-bound — run in a thread pool
        vectors = await asyncio.to_thread(
            model.encode, texts,
            normalize_embeddings=True,  # L2-normalize for cosine similarity
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

_singleton: EmbeddingProvider = HuggingFaceEmbeddingProvider()

def get_embedding_provider() -> EmbeddingProvider:
    return _singleton
```

### What's happening here

**`Protocol` for duck typing** (PEP 544): `EmbeddingProvider` is a structural interface — any class with `dimension: int` and `async def embed_batch(...)` satisfies it. No inheritance needed. To swap to Google's embedding API, create a class matching this protocol.

**Double-checked locking**: the model is ~420MB. Two concurrent coroutines calling `embed_batch` for the first time would both see `_model is None` and both try to load it. The lock prevents the race:
1. First check (no lock) — fast path after model is loaded.
2. Acquire lock — prevents concurrent loading.
3. Second check (inside lock) — in case another thread loaded it while this one waited.

**`asyncio.to_thread(model.encode, ...)`** — `model.encode()` is synchronous CPU-bound work (neural network inference). Calling it directly in `async def` blocks the event loop and starves all other requests. `asyncio.to_thread()` runs it in a thread pool worker, keeping the event loop free to handle other requests concurrently.

**`normalize_embeddings=True`** — normalizes vectors to unit length (L2 norm = 1). With unit vectors, cosine similarity equals dot product — which is what pgvector's HNSW `vector_cosine_ops` index is optimized for.

**Dimension validation at model load time** — if `all-mpnet-base-v2` produces 768-d but the DB column is `vector(384)`, every INSERT would fail with a cryptic DB error. The explicit check at load time gives a clear, actionable error message.

### FastAPI pattern to remember

> **`asyncio.to_thread()` for CPU-bound work**: Never call heavy synchronous code (ML inference, image processing) directly in `async def`. Run it in a thread pool to keep the event loop free for other requests.

---

## 14. `services/ingest.py` — Ingestion Orchestration

**File**: `backend/app/services/ingest.py`

```python
async def create_pending_document(
    session: AsyncSession, parsed: ParsedDocument,
    *, source_type, source_uri, title, user_id=None
) -> Document:
    """Request-path: persist the Document row in processing/queued state, commit, return."""
    document = Document(
        user_id=user_id,
        source_type=source_type,
        source_uri=source_uri,
        title=title or parsed.title,
        status="processing",
        stage="queued",
        doc_metadata=parsed.metadata,
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)  # loads server-generated id, created_at, etc.
    return document


async def embed_pending_document(session, document_id: UUID, text: str) -> Document:
    """Worker-path: chunk → embed → persist, writing stage transitions for live polling."""
    document = await session.get(Document, document_id)
    try:
        document.stage = "chunking";  await session.commit()  # visible to pollers

        chunks = chunk_text(text)
        if not chunks:
            raise IngestionError("no chunks produced (empty document?)")

        document.stage = "embedding"; await session.commit()

        provider = get_embedding_provider()
        vectors = await provider.embed_batch([c.content for c in chunks])

        document.stage = "persisting"; await session.commit()

        session.add_all([
            DocumentChunk(
                document_id=document.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)  # strict=True catches length mismatch
        ])
        document.status = "completed"
        document.stage = "completed"
        await session.commit()
        await session.refresh(document)
        return document

    except Exception as exc:
        log.exception("ingestion failed for document %s", document_id)
        await session.rollback()                          # discard partial chunks
        failed = await session.get(Document, document_id) # re-load (rolled back in memory)
        failed.status = "failed"
        failed.stage = "failed"
        failed.error = str(exc)
        await session.commit()                            # always persist failure status
        return failed
```

### What's happening here

**Two-function split** maps to two processes:
- `create_pending_document` — HTTP request thread, fast (just a DB insert).
- `embed_pending_document` — ARQ worker process, slow (chunk + embed + store).

**Intermediate commits for live progress**: each stage transition commits immediately. The client polls `GET /ingest/{id}` and sees live transitions: `queued → chunking → embedding → persisting → completed`. Without intermediate commits, status would only change at the very end.

**`session.add_all([...])` for bulk insert**: far more efficient than inserting chunks one-by-one. SQLAlchemy batches them into a multi-row INSERT.

**`zip(chunks, vectors, strict=True)`** (Python 3.10+): raises `ValueError` if the two sequences have different lengths. Without `strict=True`, a silent length mismatch would store wrong vectors for wrong chunks — a subtle, hard-to-debug data corruption.

**Error recovery pattern**: rollback (discard partial work) → re-fetch the document row → mark as failed → commit the failure status. You always want to persist the failure so the client can see it when polling.

**`ingest_parsed` wrapper**: combines both halves synchronously for tests and any caller that doesn't want the queue involved.

### FastAPI pattern to remember

> **Intermediate commits for observable stage transitions**: If a background job has multiple stages, commit after each transition. Without this, pollers only see the final state.

---

## 15. `workers/ingest_worker.py` — ARQ Background Worker

**File**: `backend/app/workers/ingest_worker.py`

```python
async def embed_document(ctx: dict, *, document_id: str, text: str) -> None:
    """ARQ job function. Runs in the worker OS process, separate from the API."""
    doc_uuid = UUID(document_id)
    log.info("embed_document started", extra={"document_id": document_id})
    async with async_session_maker() as session:   # worker creates its own DB session
        await embed_pending_document(session, doc_uuid, text)
    log.info("embed_document completed", extra={"document_id": document_id})


class WorkerSettings:
    """ARQ reads this class to configure the worker process.
    Start with: `uv run arq app.workers.ingest_worker.WorkerSettings`
    """
    functions = [embed_document]   # job functions this worker handles
    redis_settings = _redis_settings()  # points at Redis DB 1
    max_tries = 1      # no automatic retry on failure
    keep_result = 300  # keep result in Redis for 5 min (debug only)


async def get_arq_pool() -> ArqRedis:
    """Called from FastAPI lifespan at startup."""
    return await create_pool(_redis_settings())


def get_arq_pool_dep(request: Request) -> ArqRedis:
    """FastAPI dependency — reads the pool stored on app.state by lifespan."""
    return request.app.state.arq_pool
```

### What's happening here

**ARQ job function signature**: `async def embed_document(ctx: dict, *, ...)`. The `ctx` argument is always passed by ARQ (contains worker-level state). All subsequent arguments are keyword-only (`*`) and must match what was passed to `arq_pool.enqueue_job("embed_document", document_id=..., text=...)`.

**`WorkerSettings` class**: ARQ reads this as configuration:
- `functions` — which Python functions this worker process handles.
- `redis_settings` — Redis DB 1 (ARQ queue, separate from sessions on DB 0).
- `max_tries = 1` — no automatic retries. If the process dies mid-job, the document row stays at whatever `stage` was last committed. Re-uploading is the recovery path.
- `keep_result = 300` — job result (success/error) stays in Redis for 5 minutes. Since status is tracked in Postgres, this is purely for debugging.

**`get_arq_pool_dep`** reads from `request.app.state.arq_pool` — the pool that the lifespan stored at startup. The `Request` object always carries `.app`, which is the FastAPI application instance.

**Why a separate pool for API vs worker?** The worker is a separate OS process — it can't share the in-process pool the API holds. The API pool is for *enqueueing* (writing job entries to Redis). The worker's pool is for *dequeueing* and *running* jobs.

**Worker creates its own DB session**: `async with async_session_maker() as session:`. The HTTP request is long gone by the time the worker picks up the job. Each process manages its own connections.

### FastAPI pattern to remember

> **`app.state` for process-level shared resources**: Open a resource once in `lifespan`, store on `app.state`, retrieve in dependencies via `request.app.state`. This is the bridge between startup and request handlers.

---

## 16. `services/retrieval.py` — Vector Similarity Search

**File**: `backend/app/services/retrieval.py`

```python
class RetrievedChunk(BaseModel):
    chunk_id: UUID
    document_id: UUID
    content: str
    similarity: float   # 1.0 = identical, 0.0 = orthogonal

async def retrieve_top_k(
    session: AsyncSession,
    query: str,
    *,
    k: int = 4,
    embedder: EmbeddingProvider | None = None,
) -> list[RetrievedChunk]:
    embedder = embedder or get_embedding_provider()
    vec = (await embedder.embed_batch([query]))[0]   # embed the user's question

    distance = DocumentChunk.embedding.cosine_distance(vec).label("distance")
    stmt = select(DocumentChunk, distance).order_by(distance).limit(k)
    rows = (await session.execute(stmt)).all()

    return [
        RetrievedChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            content=chunk.content,
            similarity=1.0 - float(dist),   # cosine distance → similarity
        )
        for chunk, dist in rows
    ]
```

### What's happening here

**RAG (Retrieval-Augmented Generation)**: before calling the LLM, we find the database chunks most semantically similar to the user's question and inject them into the prompt as context. The LLM answers from those chunks, not from its training data.

**`DocumentChunk.embedding.cosine_distance(vec)`** — pgvector's Python API generates SQL:
```sql
SELECT *, (embedding <=> '[0.1, 0.2, ...]') AS distance
FROM document_chunks
ORDER BY embedding <=> '[0.1, 0.2, ...]'
LIMIT 4;
```
The `<=>` operator is pgvector's cosine distance. The HNSW index on `embedding` makes this an approximate nearest-neighbor (ANN) search — instead of scanning all rows, it navigates a graph to find the `k` closest vectors efficiently.

**Why embed the query?** Chunks are stored as float vectors in a learned vector space. To find similar chunks, you must represent the query in the same space — embed it with the same model used during ingestion. Changing models invalidates all stored embeddings.

**`similarity = 1.0 - distance`**: cosine distance ∈ [0, 2]. We convert to similarity ∈ [-1, 1] (higher = more similar) for intuitive interpretation.

**`embedder` as optional parameter**: allows tests to inject a mock embedder without patching module globals. Service-level dependency injection.

### FastAPI pattern to remember

> **Inject dependencies at the service level too**: Services that call external providers (embedders, LLMs) should accept them as optional keyword arguments (defaulting to the production singleton). This makes them directly unit-testable.

---

## 17. `routers/chat.py` — Streaming SSE Chat Endpoint

**File**: `backend/app/routers/chat.py`

```python
@router.post("/chat")
async def chat(
    request: ChatRequest,
    user_id: str = Depends(get_user_id),
) -> StreamingResponse:
    store = get_session_store()
    session_id = request.session_id or uuid4()  # mint a new session if not provided

    return StreamingResponse(
        _token_stream(request=request, user_id=user_id, session_id=session_id, store=store),
        media_type="text/event-stream",
        headers={
            "X-Session-Id": str(session_id),   # client echoes this on follow-up requests
            "Cache-Control": "no-cache",
        },
    )
```

```python
async def _token_stream(*, request, user_id, session_id, store) -> AsyncIterator[bytes]:
    history = await store.get_history(user_id, session_id)
    lc_history = _to_lc_messages(history)   # convert ChatMessage → LangChain messages

    initial_state = {
        "messages": lc_history + [HumanMessage(content=request.message)],
        "user_score": 0,
        "context": [],
        "route": request.force_route,   # None = let router decide; "tutor"/"quiz" = skip classification
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
                continue   # filter out the router's internal classification token stream
            chunk = (event.get("data") or {}).get("chunk")
            text = getattr(chunk, "content", None) if chunk is not None else None
            if not text:
                continue
            response_buf.append(text)
            yield f"data: {json.dumps({'delta': text})}\n\n".encode()   # SSE format
    except Exception as exc:
        log.exception("chat stream failed mid-generation")
        detail = _summarise_error(exc)
        error_message = f"\n\n⚠️ **{detail}**"
        yield f"data: {json.dumps({'delta': error_message})}\n\n".encode()
    finally:
        full = "".join(response_buf)
        if full:
            await store.append_turn(         # persist conversation turn to Redis
                user_id, session_id,
                user_msg=ChatMessage(role="user", content=request.message),
                assistant_msg=ChatMessage(role="assistant", content=full),
            )
        yield b"data: [DONE]\n\n"           # SSE termination signal
```

### What's happening here

**`StreamingResponse`** sends HTTP responses without buffering. You pass it an async generator that `yield`s bytes; FastAPI sends each chunk to the client as soon as it's yielded.

**SSE wire format**:
```
data: {"delta": "Hel"}\n\n
data: {"delta": "lo"}\n\n
data: [DONE]\n\n
```
Each event is `data: <payload>\n\n`. The client's `EventSource` or `fetch`+`ReadableStream` API splits on the double newline.

**`graph.astream_events(initial_state, version="v2")`** — LangGraph's streaming API. Instead of waiting for the entire graph to complete, it emits *events* in real time. Key event types:
- `on_chat_model_stream` — a token chunk was produced by an LLM call.
- `metadata.langgraph_node` — which graph node produced it.
- `data.chunk` — a LangChain `AIMessageChunk` with a `.content` string.

**Why filter by `langgraph_node`?** The router node also calls the LLM (to classify intent). We don't want to stream `"TUTOR"` or `"QUIZ"` as if it were the answer. The filter `if node not in ("tutor", "quiz", "smalltalk"): continue` ensures only content-generating nodes stream to the client.

**Error handling in streams**: once streaming begins, HTTP headers (status code, etc.) are already sent. If the LLM fails mid-stream, we can't change the status code. So we emit the error as a delta (marked with ⚠️) — the user sees a readable message. Then `[DONE]` is always sent.

**`finally` block**: runs whether the stream completes normally or raises. Guarantees that the conversation turn is always persisted to Redis — even on partial failures. This is the canonical Python cleanup pattern.

**`_summarise_error(exc)`** converts cryptic provider errors (Gemini 429 "RESOURCE_EXHAUSTED") into human-readable, actionable messages. Translate infrastructure errors at the outermost layer.

**`X-Session-Id` response header**: if the client didn't send a session, the server minted one and echoes it back. The client stores this (e.g., in `localStorage`) and sends it on the next request to continue the conversation.

### FastAPI pattern to remember

> **`StreamingResponse` + async generator for SSE**: Return `StreamingResponse` wrapping an `async def` generator that `yield`s bytes. Use the `data: ...\n\n` SSE format. Always emit `[DONE]`. Use `finally` for cleanup that must always run.

---

## 18. `agents/router.py` & `agents/graph.py` — LangGraph Orchestration

**Files**: `backend/app/agents/router.py`, `backend/app/agents/graph.py`

### The graph structure

```python
# graph.py
def build_graph(llm: LLMProvider | None = None):
    g: StateGraph = StateGraph(TutorState)
    g.add_node("router",   make_router_node(llm))
    g.add_node("tutor",    make_tutor_node(llm))
    g.add_node("quiz",     make_quiz_node(llm))
    g.add_node("smalltalk", make_smalltalk_node(llm))
    g.add_edge(START, "router")
    g.add_conditional_edges(
        "router",
        _route_dispatch,   # reads state["route"] → returns node name
        {"tutor": "tutor", "quiz": "quiz", "smalltalk": "smalltalk"},
    )
    g.add_edge("tutor", END)
    g.add_edge("quiz", END)
    g.add_edge("smalltalk", END)
    return g.compile()

graph = build_graph()  # module-level singleton
```

```
START → router_node → [conditional dispatch on state["route"]]
                            ↓
                   tutor | quiz | smalltalk → END
```

**`StateGraph(TutorState)`**: LangGraph is a graph execution engine where each node is an async function `(state: TutorState) -> TutorState`. Nodes return a partial update dict — only changed keys.

**`add_conditional_edges`**: after `router` runs, LangGraph calls `_route_dispatch(state)`. This reads `state["route"]` and returns `"tutor"`, `"quiz"`, or `"smalltalk"`. The dict maps return values to node names.

### The router node

```python
# router.py
def make_router_node(llm: LLMProvider | None = None):
    provider = llm or get_llm_provider()

    async def router_node(state: TutorState) -> TutorState:
        # Skip classification if force_route was provided by the client
        if state.get("route") in ("tutor", "quiz", "smalltalk"):
            return {}   # empty dict = no state changes

        last_user = next(
            (m for m in reversed(state.get("messages", [])) if isinstance(m, HumanMessage)),
            None,
        )
        text = last_user.content if last_user else ""
        route = await provider.classify(str(text))
        return {"route": route}

    return router_node
```

**`make_router_node` factory pattern**: returns a closure `router_node` that captures `provider`. This is how you inject dependencies into LangGraph nodes — you can't use `Depends()` inside a graph. All four nodes (router, tutor, quiz, smalltalk) use this factory pattern, enabling test injection of mock LLM providers.

**`force_route` shortcut**: if `state["route"]` is already set (because the client sent `force_route="quiz"`), the router returns `{}` immediately — no LLM call needed. `_route_dispatch` then sends execution directly to the pre-set route, saving an API call.

**`return {}`**: LangGraph merges the returned dict into the current state. An empty dict means "no changes" — the existing `state["route"]` (set from `force_route`) is preserved.

### FastAPI pattern to remember

> **Factory functions for LangGraph node dependency injection**: Graph nodes can't use `Depends()`. Use factory functions that return node closures with the provider captured. This keeps graph nodes testable by passing mock LLMs to the factory.

---

## 19. `agents/llm.py` — Gemini LLM Provider

**File**: `backend/app/agents/llm.py`

```python
class LLMProvider(Protocol):
    async def classify(self, text: str) -> Route: ...
    async def complete(self, messages: list[BaseMessage], *, system: str | None = None) -> str: ...
    async def quiz(self, *, topic: str, context: list[RetrievedChunk]) -> str: ...

CLASSIFIER_SYSTEM = (
    "You are a routing classifier for an AI tutor. Decide which category best fits...\n"
    "  - SMALLTALK: greetings, thanks, goodbyes...\n"
    "  - QUIZ: explicit request for a knowledge check / quiz / test / MCQ.\n"
    "  - TUTOR: anything else — questions, explanations, summaries...\n"
    "Reply with exactly one word, uppercase: SMALLTALK, QUIZ, or TUTOR."
)

class GeminiLLMProvider:
    _router: ChatGoogleGenerativeAI | None = None   # temperature=0.0, for classification
    _chat: ChatGoogleGenerativeAI | None = None     # temperature=0.4, for generation
    _lock: Lock = Lock()

    def router_chat(self) -> ChatGoogleGenerativeAI:
        # Lazy double-checked-locking singleton
        if GeminiLLMProvider._router is None:
            with GeminiLLMProvider._lock:
                if GeminiLLMProvider._router is None:
                    GeminiLLMProvider._router = self._build_chat(temperature=0.0)
        return GeminiLLMProvider._router

    async def classify(self, text: str) -> Route:
        response = await self.router_chat().ainvoke(
            [SystemMessage(content=CLASSIFIER_SYSTEM), HumanMessage(content=text)]
        )
        raw = (response.content or "").strip().upper()
        if "QUIZ" in raw:       return "quiz"       # explicit quiz wins
        if "SMALLTALK" in raw:  return "smalltalk"
        return "tutor"          # default: anything substantive

    async def complete(self, messages, *, system=None) -> str:
        # Non-streaming path — for callers that don't need streaming
        prefix = [SystemMessage(content=system)] if system else []
        response = await self.chat().ainvoke(prefix + list(messages))
        return response.content or ""

_singleton: LLMProvider = GeminiLLMProvider()

def get_llm_provider() -> LLMProvider:
    return _singleton

def get_chat_model() -> ChatGoogleGenerativeAI:
    """Direct access to the Gemini client for agent nodes that use .astream()."""
    provider = _singleton
    if not isinstance(provider, GeminiLLMProvider):
        raise RuntimeError("get_chat_model requires the default GeminiLLMProvider")
    return provider.chat()
```

### What's happening here

**Two LLM clients, two temperatures**:
- `router_chat` (temperature=0.0): for classification. Zero temperature = deterministic, always picks the highest-probability token. Perfect for a single-word route decision.
- `chat` (temperature=0.4): for content generation. A small temperature allows slight variation so answers don't sound robotic.

**`ainvoke` for classification vs. `astream` for content**: classification needs the full response to parse the route label — streaming provides no value. Content generation uses `astream` so tokens surface in `graph.astream_events()` and stream to the client as SSE.

**The classifier prompt**: ends with *"Reply with exactly one word, uppercase: SMALLTALK, QUIZ, or TUTOR."* Constraining the output format makes parsing trivially reliable.

**Classifier fallback logic**: `if "QUIZ" in raw` checks anywhere in the response (handles cases where the model adds a period or explanation). QUIZ takes precedence over SMALLTALK. Anything unrecognized defaults to "tutor".

**`get_chat_model()`**: exposes the streaming-capable Gemini client directly to agent nodes. It validates that `_singleton` is actually a `GeminiLLMProvider` (not a test mock that doesn't support `.astream()`).

### FastAPI pattern to remember

> **Separate LLM configs for classification vs generation**: temperature=0 for deterministic routing, higher temperature for creative generation. This is a universal LLM application design principle.

---

## 20. Agent Implementations: tutor, quiz, smalltalk, state

**Files**: `backend/app/agents/`

### `state.py` — Shared state schema

```python
class TutorState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]  # conversation history
    user_score: int          # quiz score tracking (future feature)
    context: list[RetrievedChunk]  # chunks retrieved from Postgres for this turn
    route: Route | None      # "tutor"|"quiz"|"smalltalk"|None
    response: str | None     # final LLM response text
```

**`TypedDict, total=False`**: defines the dict shape. `total=False` makes all keys optional — each node only needs to return the keys it modified, not the full state.

**`Annotated[list[BaseMessage], add_messages]`**: the `add_messages` LangGraph annotation tells the graph how to merge message lists from different nodes. Instead of replacing the list, it *appends* — this is how conversation history accumulates across nodes.

### `tutor.py` — Retrieval-augmented tutor

```python
TUTOR_SYSTEM_TEMPLATE = (
    "You are an expert AI tutor. Answer ONLY using the numbered context snippets below.\n"
    "Rules:\n"
    "  1. If context doesn't contain the answer, reply exactly:\n"
    '     "I don\'t have enough information to answer that based on the available material."\n'
    "  2. Always cite snippet number(s) used, e.g. \"[1]\" or \"[2, 3]\".\n"
    "  3. Do not invent facts or use outside knowledge.\n"
    "  4. Format in clean Markdown.\n\n"
    "Context:\n{context}"
)

def make_tutor_node(_unused=None, *, k: int = 4):
    async def tutor_node(state: TutorState) -> TutorState:
        last_user = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
        query = str(last_user.content) if last_user else ""

        async with async_session_maker() as session:
            chunks = await retrieve_top_k(session, query, k=k)   # k=4 for tutor

        system_prompt = TUTOR_SYSTEM_TEMPLATE.format(context=_format_context(chunks))
        chat = get_chat_model()

        response_buf: list[str] = []
        async for piece in chat.astream([SystemMessage(content=system_prompt), *state["messages"]]):
            text = getattr(piece, "content", "") or ""
            if text:
                response_buf.append(text)

        response = "".join(response_buf)
        return {
            "context": chunks,
            "response": response,
            "messages": [AIMessage(content=response)],  # appended via add_messages
        }

    return tutor_node
```

**Grounding rules in the system prompt**:
- **Rule 1 — explicit refusal**: if context doesn't contain the answer, the model must say so verbatim — not hallucinate from training data.
- **Rule 2 — citations**: the model must cite `[1]`, `[2, 3]` etc. — users can verify answers against the source chunks.
- **Rule 3 — no speculation**: no outside knowledge.

**`_format_context(chunks)`** formats chunks as:
```
[1] The CAP theorem states...
[2] Raft is a consensus algorithm...
```
Numbered so the model can cite them. The `/chat` route streams these tokens to the client via `on_chat_model_stream` events.

**`k=4` for tutor, `k=2` for quiz**: tutor answers may span multiple concepts (needs more context). MCQ generation needs to be grounded in a tighter window to produce focused questions.

### `quiz.py` — MCQ generator

Same structure as tutor, but:
- Retrieves `k=2` chunks.
- Uses a different system prompt requiring structured MCQ output (Question/A/B/C/D/Answer/Explanation).
- The user message is `"Quiz me on: {topic}"` not the full history.

### `smalltalk.py` — Conversational handler

```python
SMALLTALK_SYSTEM = (
    "You are a friendly AI tutor. The student has sent a conversational message...\n"
    "Reply briefly and warmly (1–2 sentences, under 60 words).\n"
    "Hard rules:\n"
    "  - Do NOT invent facts.\n"
    "  - Do NOT reference any specific document content (no retrieval context here).\n"
)
```

Smalltalk **skips retrieval entirely** — no pgvector query. For "hi" or "thank you," you don't need to search the knowledge base. The full message history is still passed so the model responds contextually (e.g., "You're welcome!" after a tutor answer).

### FastAPI pattern to remember

> **Explicit refusal conditions in system prompts**: Make the LLM's "I don't know" condition a hard rule in the prompt, not an implicit assumption. This prevents hallucination when retrieved context is insufficient.

---

## 21. `routers/documents.py` — Document Management

**File**: `backend/app/routers/documents.py`

```python
router = APIRouter(prefix="/documents", tags=["documents"])

@router.get("", response_model=list[DocumentSummary])
async def list_documents(session=Depends(get_session), user_id=Depends(get_user_id)):
    chunk_count = func.count(DocumentChunk.id).label("chunk_count")
    stmt = (
        select(Document.id, Document.source_type, Document.title, Document.status,
               Document.stage, Document.created_at, chunk_count)
        .join(DocumentChunk, DocumentChunk.document_id == Document.id, isouter=True)
        .where(Document.user_id == user_id)   # only caller's documents
        .group_by(Document.id)
        .order_by(Document.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [DocumentSummary(id=row.id, chunk_count=int(row.chunk_count or 0), ...) for row in rows]


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: UUID, session=..., user_id=...):
    result = await session.execute(
        delete(Document).where(Document.id == document_id, Document.user_id == user_id)
    )
    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(404, "document not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

### What's happening here

**JOIN with aggregate in `list_documents`**: single SQL query that joins `documents` to `document_chunks` (outer join so 0-chunk documents still appear), counts chunks per document (`GROUP BY`), and returns everything in one round-trip. Without this join, you'd need N+1 queries.

**Ownership-only filter**: `.where(Document.user_id == user_id)` — only your documents. Legacy rows (`user_id IS NULL`) are intentionally excluded from listing (they remain available for chat retrieval but invisible in the admin UI).

**`status_code=204 NO CONTENT` for DELETE**: 204 = success with no response body. Standard for DELETE. FastAPI will not serialize a response body.

**Double-condition WHERE in DELETE**: `Document.id == document_id AND Document.user_id == user_id`. If `rowcount == 0`, you get 404 — regardless of whether the document doesn't exist or belongs to someone else. This collapses 404 and 403 into 404, preventing existence leakage across users.

**`await session.rollback()` before the 404**: the DELETE didn't commit anything (rowcount was 0), but the session may have a pending transaction state. Rolling back ensures the session is clean before we raise and hand control back to FastAPI's error handler.

**`ON DELETE CASCADE` for chunks**: defined in the DB's FK constraint. When you delete a `Document` row, Postgres automatically deletes all `document_chunks` rows with that `document_id`. No Python code needed.

### FastAPI pattern to remember

> **Collapse 403 into 404 to prevent existence leakage**: `WHERE id = X AND user_id = Y` — "not found" and "not yours" return the same 404. A caller can't probe whether IDs exist for other users.

---

## 22. `routers/health.py` & `schemas/`

### Health check (`routers/health.py`)

```python
@router.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    postgres_ok, redis_ok = await asyncio.gather(
        _check_postgres(),    # SELECT 1
        _check_redis(redis_client),  # PING
    )
    all_ok = postgres_ok and redis_ok
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if all_ok else "degraded",
        postgres=postgres_ok,
        redis=redis_ok,
    )
```

**`asyncio.gather`**: runs both checks concurrently. Sequential execution: 50ms + 50ms = 100ms. Parallel: max(50ms, 50ms) = 50ms.

**`response: Response` injection**: FastAPI can inject the raw `Response` object. Here we use it to set the status code to 503 while still returning a structured JSON body. Without this, there'd be no way to return a non-default status code from inside the function (the `status_code` on the decorator is for the success case).

**Why health endpoints?** Load balancers and orchestration tools (Kubernetes readiness probes, Docker's `healthcheck`) call `/health` to decide whether to route traffic. 503 = "alive but not ready."

### Pydantic schemas (`schemas/`)

```python
# chat.py
class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)    # required, non-empty
    session_id: UUID | None = Field(None)      # optional, auto-minted if absent
    force_route: Literal["tutor", "quiz"] | None = Field(None)  # bypass router

# ingest.py
IngestStage = Literal["queued", "chunking", "embedding", "persisting", "completed", "failed"]

class IngestStatus(BaseModel):
    id: UUID
    status: IngestStatusValue
    stage: IngestStage | None   # live progress label
    chunk_count: int = Field(0)
    title: str | None = None
    error: str | None = None

# health.py
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    postgres: bool
    redis: bool
```

**What Pydantic schemas do in FastAPI**:
1. **Validate incoming requests**: if `message` is missing or empty, FastAPI auto-returns 422 Unprocessable Entity before the route handler runs.
2. **Serialize response objects**: converts Python objects to JSON.
3. **Document the API**: FastAPI generates an OpenAPI schema from these Pydantic models, visible at `/docs`.

**`Literal["tutor", "quiz"]`**: restricts values to exactly these strings. Any other value causes a 422 before the route handler runs.

**`Field(...)`** (with `...`): marks a field as required. `Field(None)` = optional with default `None`. `Field(0)` = optional with default `0`.

**Separate `schemas/` from `models/`**: SQLAlchemy models = DB representation. Pydantic schemas = API representation. You deliberately don't expose ORM objects over HTTP — the schema gives you freedom to shape responses differently from how data is stored.

### FastAPI pattern to remember

> **Always separate ORM models from API schemas**: SQLAlchemy models are for DB operations. Pydantic schemas are for HTTP I/O. Convert between them in routers. Mixing them couples your DB schema to your API contract.

---

## 23. `tests/` and `end-to-end-flow.md`

### Test architecture (`conftest.py`)

```python
# 1. Force dev bypass OFF — tests use their own override, not the permissive bypass
settings.dev_auth_bypass = False

# 2. Auth override: accept Bearer value verbatim (no real Clerk JWT)
async def _test_get_user_id(authorization: str | None = Header(default=None)) -> str:
    if not authorization: return ANONYMOUS_USER_ID
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else ANONYMOUS_USER_ID

app.dependency_overrides[get_user_id] = _test_get_user_id

# 3. Stub ARQ pool: records enqueue calls, doesn't actually queue jobs
class _StubArqPool:
    def __init__(self): self.calls: list[dict] = []
    async def enqueue_job(self, function, *args, **kwargs):
        self.calls.append({"function": function, "kwargs": kwargs})
        return None

app.dependency_overrides[get_arq_pool_dep] = lambda: arq_pool_stub

# 4. Schema creation (once per test session, not once per test)
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _create_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

# 5. Test HTTP client (no network — talks to the ASGI app directly)
@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
```

**`app.dependency_overrides`** is FastAPI's built-in test override mechanism. You replace a dependency with a test double — all routes using `Depends(get_user_id)` now use `_test_get_user_id`. No monkeypatching needed.

**`ASGITransport`**: HTTPX can talk directly to an ASGI app without starting a server. Fast (no network stack), isolated (no port conflicts), deterministic.

**`scope="session"` fixtures**: run once for the entire test session. Schema creation is expensive — you create tables once at the start and truncate data between tests using the `db_clean` fixture.

**`_StubArqPool`**: replaces the real ARQ pool. Route tests assert: `arq_pool_stub.calls[0]["function"] == "embed_document"`. This verifies the route enqueued the right job without running a worker process.

**`seeded_chunks` fixture**: inserts test document chunks with real embeddings into Postgres. Used by agent tests to verify the full retrieval → LLM pipeline. The `sample_pdf_bytes` fixture generates a real PDF using `fpdf` so the PDF parsing code can be exercised end-to-end.

### `end-to-end-flow.md`

This file contains **actual runtime evidence** — captured logs, Redis keyspaces, Postgres queries, and SSE streams from a real running system. For example, a real chat turn trace:

```
POST /chat { message: "Explain CAP theorem", session_id: ... }
  → get_user_id() → "anon_dev_user"
  → session.get_history() → LRANGE chat:user:...:messages → [] (fresh session)
  → graph.astream_events(...)
      router_node: classify("Explain CAP theorem") → TUTOR
      tutor_node:
        embed("Explain CAP theorem") → [0.024, -0.013, ...]
        SELECT ... FROM document_chunks ORDER BY embedding <=> $1 LIMIT 4
        → chunks: similarity=0.89, 0.87, 0.82, 0.79
        astream(system_prompt + messages) →
  → SSE: data: {"delta": "The"}\n\n
  → SSE: data: {"delta": " CAP"}\n\n  (per token)
  → SSE: data: [DONE]\n\n
  → RPUSH + LTRIM + EXPIRE in Redis
```

This is your ground truth for what the system actually does. Read the e2e flow doc alongside the code to verify your mental model. The diagrams and traces often reveal subtleties that aren't obvious from reading code alone.

---

## 24. Summary: FastAPI Patterns Reference

Here's a consolidated reference of every major FastAPI pattern encountered in this codebase:

| Pattern | Where Used | Key API |
|---|---|---|
| **Lifespan for startup/shutdown** | `main.py` | `@asynccontextmanager async def lifespan(app)` |
| **Router organization** | `routers/` | `APIRouter(prefix=..., tags=[...])` + `app.include_router()` |
| **Dependency injection** | Every route | `param: Type = Depends(fn)` |
| **Yield-based resource cleanup** | `db.py` | `async def get_session() -> AsyncIterator[Session]: yield session` |
| **Settings from environment** | `config.py` | `class Settings(BaseSettings)` with `SettingsConfigDict(env_file=...)` |
| **Pydantic request/response models** | `schemas/` | `class FooRequest(BaseModel)` as route parameter type |
| **Custom status codes** | `routers/ingest.py` | `status_code=202`, `status.HTTP_413_REQUEST_ENTITY_TOO_LARGE` |
| **File + form uploads** | `routers/ingest.py` | `file: UploadFile = File(...)`, `title: str = Form(None)` |
| **StreamingResponse for SSE** | `routers/chat.py` | `StreamingResponse(async_gen, media_type="text/event-stream")` |
| **`app.state` for shared resources** | `main.py`, `workers/` | `app.state.arq_pool`, `request.app.state.arq_pool` |
| **`response: Response` for dynamic status** | `routers/health.py` | `response.status_code = 503` |
| **HTTPException for errors** | All routers | `raise HTTPException(status.HTTP_401_UNAUTHORIZED, "detail")` |
| **Exception chaining** | `routers/ingest.py` | `raise HTTPException(...) from exc` |
| **`asyncio.gather` for parallelism** | `routers/health.py` | `await asyncio.gather(check_pg(), check_redis())` |
| **`asyncio.to_thread` for CPU work** | `services/embeddings.py` | `await asyncio.to_thread(model.encode, ...)` |
| **Dependency overrides for tests** | `tests/conftest.py` | `app.dependency_overrides[dep_fn] = override_fn` |
| **`ASGITransport` for test client** | `tests/conftest.py` | `AsyncClient(transport=ASGITransport(app=app))` |
| **Singleton for connection pools** | `redis_client.py`, `db.py` | Module-level `engine`, `redis` objects |
| **Protocol for provider interfaces** | `services/embeddings.py` | `class EmbeddingProvider(Protocol):` |
| **Factory functions for DI in LangGraph** | `agents/` | `def make_tutor_node(llm=None): ... return tutor_node` |
| **Collapse 403 into 404** | `routers/documents.py`, `routers/ingest.py` | `WHERE id = X AND user_id = Y` → 404 on 0 rows |
| **Intermediate commits for progress** | `services/ingest.py` | `document.stage = "chunking"; await session.commit()` |

---

*Generated by reading every backend file in the recommended order — August 2026.*
