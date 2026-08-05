# FastAPI in the Personal AI Tutor — production-level depth

Same shape as the Postgres, Redis, and Docker docs: each decision paired with the alternative we rejected, and concrete file:line references back into the codebase.

A useful framing up front: FastAPI is *not* just a web framework. It's a dependency injection system built on top of Starlette (ASGI) with Pydantic for data validation, async-first from day one, and auto-generated OpenAPI docs. The first thing to internalise is "FastAPI's `Depends()` system is the backbone — once you understand how dependencies resolve, you understand the whole framework."

This project uses FastAPI for two distinct things:
1. **Synchronous request handling** — parsing, validating, routing HTTP requests to service functions, serialising JSON responses.
2. **Streaming responses** — delivering LLM tokens to the frontend token-by-token over Server-Sent Events (SSE) without buffering.

Both of these are done in `async def` route handlers, backed by `asyncio`.

---

## 1. Why FastAPI at all (and why not the alternatives)

The shortlist for "what Python web framework should this project use?":

| Option | Why we considered it | Why we didn't pick it |
|---|---|---|
| **Flask** | Familiar. Huge ecosystem. Simple mental model. | Synchronous by default. Every request blocks a thread. Running asyncio inside Flask requires `asyncio.run()` hacks that destroy concurrency. No native dependency injection, no Pydantic integration, no auto-docs. |
| **Django** | Batteries included. ORM built-in. Admin UI. | Sync-first; Django Channels needed for async. The ORM is Django-specific — doesn't compose with SQLAlchemy. Much more opinionated than we need. Heavy for a microservice. |
| **Sanic** | Async from the start. Fast. | Smaller ecosystem. Less mature dependency injection. Pydantic is third-party. Documentation is thinner. |
| **aiohttp** | Mature async. Production-grade. | Lower-level than FastAPI. No dependency injection built in. No Pydantic integration. You'd build what FastAPI already gives you. |
| **Litestar (Starlite)** | Performance-oriented, async. | Less community momentum at project start. FastAPI had more real-world production examples to copy from. |
| **FastAPI** | Async-first. Native Pydantic. Dependency injection. Auto-docs (Swagger + ReDoc). Starlette underneath (battle-tested ASGI). Fast enough for our throughput. | Not the absolute fastest (that would be Litestar or raw Starlette). DI overuse can make call graphs opaque if abused. |

We picked FastAPI. The decision is implicit in `pyproject.toml` and documented in `ARCHITECTURE.md §3`:

> FastAPI was chosen for its async-first design (ASGI via Starlette), native Pydantic integration for request/response validation, dependency injection for auth and DB session management, and auto-generated OpenAPI docs at `/docs`.

**The principle**: pick the framework whose primitives match your problem. We needed async (streaming), type-safe I/O (Pydantic), and dependency injection (auth, DB sessions). FastAPI gives all three without bolt-ons.

---

## 2. How this project uses FastAPI

Four broad concerns:

### 2a. The app factory and lifespan — `app/main.py`

```python
# backend/app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import engine
from app.redis_client import redis as redis_client
from app.workers.ingest_worker import get_arq_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP: runs once before the app accepts its first request
    app.state.arq_pool = await get_arq_pool()
    try:
        yield               # app accepts requests here
    finally:
        # SHUTDOWN: runs when the process receives SIGTERM or Ctrl+C
        await app.state.arq_pool.aclose()
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

**`lifespan`** is the modern startup/shutdown pattern. The `@asynccontextmanager` decorator turns a generator function into an async context manager:

- Code **before** `yield` → runs at startup, once.
- Code **after** `yield` (in `finally`) → runs at shutdown, once, even if the server is killed mid-request.

The older pattern was `@app.on_event("startup")` and `@app.on_event("shutdown")` decorators. The `lifespan` pattern is now the recommended way because it keeps startup and shutdown logic together in one function, which is easier to reason about.

**`app.state`** is FastAPI's per-process key-value store. You can attach any object to it:

```python
app.state.arq_pool = await get_arq_pool()   # attached in lifespan
```

And read it back from inside any request via `request.app.state.arq_pool` (or via a dependency — more on this in §4).

Why open the ARQ pool at startup and store it on `app.state`? Because opening a Redis TCP connection pool costs time (~5–10 ms). If we opened it per-request, every ingest endpoint call would pay that cost. One pool opened once, shared across all requests.

**`app.include_router()`** mounts each `APIRouter`. A router is just a mini-app that groups related routes:

```python
# backend/app/routers/ingest.py
router = APIRouter(prefix="/ingest", tags=["ingest"])

@router.post("/upload", ...)
async def upload_document(...): ...

@router.get("/{id}", ...)
async def get_status(...): ...
```

The `prefix="/ingest"` means all routes in this file get the `/ingest` prefix. The `tags=["ingest"]` groups them under a single section in the Swagger UI. This is how you organise routes across many files without one huge `main.py`.

---

### 2b. Settings — `app/config.py`

```python
# backend/app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://tutor:tutor@localhost:5432/tutor"
    redis_url: str = "redis://localhost:6380/0"
    embedding_dim: int = 768
    google_api_key: str | None = None
    gemini_model_name: str = "gemini-2.5-flash-lite"
    session_history_turns: int = 10
    session_ttl_seconds: int = 30 * 24 * 3600
    clerk_jwks_url: str | None = None
    dev_auth_bypass: bool = False
    arq_redis_db: int = 1

    @property
    def arq_redis_url(self) -> str:
        parsed = urlparse(self.redis_url)
        return f"{parsed.scheme}://{parsed.netloc}/{self.arq_redis_db}"

settings = Settings()
```

`pydantic_settings.BaseSettings` reads values in order:
1. **Environment variables** (highest priority)
2. **`.env` file** (resolved from `_PROJECT_ROOT / ".env"`)
3. **Python class defaults** (lowest priority)

Field names are case-insensitive: `DATABASE_URL=...` in `.env` maps to `database_url`.

`extra="ignore"` silently ignores any env vars that don't match class fields. Without it, a key like `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (a frontend key) in the `.env` would raise a `ValidationError`.

`settings = Settings()` creates a module-level singleton. Every other module imports it as `from app.config import settings`. This keeps configuration centralised.

`@property arq_redis_url` is a computed property: derives the ARQ queue URL from `redis_url` but on DB 1. This is the right place for computed config — not hardcoded in multiple places.

---

### 2c. Route handlers and HTTP semantics — `app/routers/`

#### Anatomy of a FastAPI route

```python
@router.post(
    "/upload",
    response_model=IngestStatus,        # Pydantic model → response JSON shape + OpenAPI schema
    status_code=status.HTTP_202_ACCEPTED, # default success status code
    summary="Upload a file for ingestion",
    description="...",
    responses={
        202: {"description": "Job accepted."},
        413: {"description": "Upload exceeds size limit."},
        415: {"description": "Unsupported file type."},
    },
)
async def upload_document(
    file: UploadFile = File(...),        # multipart file upload
    title: str | None = Form(None),      # multipart form field
    session: AsyncSession = Depends(get_session),  # DB session dependency
    user_id: str = Depends(get_user_id),           # auth dependency
    arq_pool: ArqRedis = Depends(get_arq_pool_dep), # ARQ pool dependency
) -> IngestStatus:
    ...
```

**Every parameter is declared with a source**:
- `File(...)` → multipart file upload body
- `Form(None)` → multipart form field
- `Depends(fn)` → dependency injection (FastAPI calls `fn` and injects its return value)
- No annotation → FastAPI looks for a JSON body field (Pydantic model or primitive)
- `Header(...)`, `Query(...)`, `Path(...)` → HTTP header, query string, URL path segment

**FastAPI validates before calling the handler**: if `file` is missing, FastAPI returns 422 Unprocessable Entity before your function runs. If `Depends(get_user_id)` raises `HTTPException(401)`, your function never runs. This is validation as a first-class citizen.

**`response_model=IngestStatus`** does two things:
1. Tells FastAPI to serialise the return value through this Pydantic model (filtering out extra fields, applying validators).
2. Generates the OpenAPI schema for the response body, visible at `/docs`.

#### HTTP status codes are explicit

```python
# 202: async work accepted, result not yet ready
@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)

# 204: success, no response body (DELETE)
@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)

# 503: service degraded (set dynamically, not on decorator)
async def health(response: Response):
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(...)
```

The `status_code` on the decorator is the "success" code. To set it dynamically (e.g., 503 when dependencies are down while still returning a body), inject `response: Response` and set `response.status_code` inside the handler.

#### Raising HTTP errors

```python
raise HTTPException(
    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    detail=f"upload exceeds {settings.max_upload_bytes} bytes"
)
```

`HTTPException` is FastAPI's way to abort with a specific HTTP status code. FastAPI catches it and returns `{"detail": "..."}` as the response body. The `raise ... from exc` pattern (exception chaining) preserves the original exception as `__cause__` for debugging.

The alternative (returning `Response(status_code=413)` explicitly) works but loses the ability to return a body. Use `HTTPException` for client errors.

#### Multipart vs JSON bodies

```python
# JSON body (the default):
async def chat(request: ChatRequest): ...

# Multipart form-data:
async def upload(file: UploadFile = File(...), title: str = Form(None)): ...
```

You can't mix `File()/Form()` with a Pydantic JSON body in the same endpoint — multipart and `application/json` are different content types. If you need both structured data and a file, put everything in multipart form fields.

---

## 3. The dependency injection system — the most important concept

FastAPI's dependency injection (`Depends()`) is what makes the codebase composable and testable. It's worth understanding deeply.

### 3a. How `Depends()` works

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session   # the value after yield is what gets injected

async def get_user_id(authorization: str | None = Header(default=None)) -> str:
    ...
    return user_id

@router.post("/upload")
async def upload_document(
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
):
    ...
```

FastAPI resolves the dependency graph **before** calling the route handler:

1. FastAPI sees `Depends(get_session)` → calls `get_session()` → enters the `async with` → injects the yielded session.
2. FastAPI sees `Depends(get_user_id)` → calls `get_user_id(authorization=<header value>)` → injects the returned user id.
3. If any dependency raises `HTTPException` → route handler is never called, error response is returned.
4. After the handler finishes (normal or exception) → `get_session`'s `finally` block runs (session cleanup).

This is dependency injection in the classical sense: the caller declares *what* it needs, not *how* to get it.

### 3b. The yield-based dependency for resource cleanup

```python
# backend/app/db.py
async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session
```

The `yield` turns `get_session` into a context manager dependency. Code before `yield` runs when the dependency is first resolved (before the route handler). Code in `finally` (or after `yield`) runs after the route handler completes — even if the handler raises an exception.

This is the canonical pattern for resource management in FastAPI:

```python
async def get_resource() -> AsyncIterator[Resource]:
    resource = await open_resource()
    try:
        yield resource
    finally:
        await resource.close()
```

You never have to manually close the resource in the route handler. FastAPI handles it.

### 3c. Dependencies can have their own dependencies

Dependencies compose recursively:

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session

async def get_user_id(authorization: str | None = Header(default=None)) -> str:
    ...
    return user_id

# get_document itself depends on get_session and get_user_id
async def get_owned_document(
    document_id: UUID,
    session: AsyncSession = Depends(get_session),
    user_id: str = Depends(get_user_id),
) -> Document:
    doc = await session.get(Document, document_id)
    if not doc or doc.user_id != user_id:
        raise HTTPException(404)
    return doc

# The route just depends on get_owned_document
@router.delete("/{document_id}")
async def delete_document(doc: Document = Depends(get_owned_document)):
    await session.delete(doc)
    ...
```

FastAPI builds the dependency graph automatically and caches resolved values within a single request (same `get_session` call is shared if two dependencies both need it).

### 3d. `app.state` — for process-level shared resources

For resources opened once at startup (not per-request), the pattern is:

```python
# In lifespan (startup):
app.state.arq_pool = await get_arq_pool()

# In a dependency (per-request):
def get_arq_pool_dep(request: Request) -> ArqRedis:
    return request.app.state.arq_pool

# In a route:
async def upload_document(arq_pool: ArqRedis = Depends(get_arq_pool_dep)):
    await arq_pool.enqueue_job(...)
```

The `Request` object in the dependency gives you access to `request.app`, which is the FastAPI application instance. `request.app.state` is the same `app.state` you set in `lifespan`.

This is the bridge between "resources opened once at startup" and "per-request dependency injection."

### 3e. Dependency overrides in tests

The most powerful feature of the DI system for testing:

```python
# In conftest.py
async def _test_get_user_id(authorization: str | None = Header(default=None)) -> str:
    """No JWT verification — accept Bearer value verbatim for tests."""
    if not authorization: return ANONYMOUS_USER_ID
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ANONYMOUS_USER_ID

class _StubArqPool:
    def __init__(self): self.calls = []
    async def enqueue_job(self, function, *args, **kwargs):
        self.calls.append({"function": function, "kwargs": kwargs})

app.dependency_overrides[get_user_id] = _test_get_user_id
app.dependency_overrides[get_arq_pool_dep] = lambda: arq_pool_stub
```

`app.dependency_overrides` is a dict that maps `{original_dep: override_dep}`. FastAPI uses the override everywhere `Depends(original_dep)` appears — you never need to monkeypatch or mock at the module level.

This is the cleanest test isolation pattern in any Python web framework.

---

## 4. Pydantic — FastAPI's data layer

FastAPI integrates deeply with Pydantic for request validation and response serialisation.

### 4a. Request models

```python
# backend/app/schemas/chat.py
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)           # required, must be non-empty
    session_id: UUID | None = Field(None)             # optional UUID
    force_route: Literal["tutor", "quiz"] | None = Field(None)  # constrained string
```

When a route declares a Pydantic model as a parameter:

```python
async def chat(request: ChatRequest): ...
```

FastAPI:
1. Reads the JSON request body.
2. Passes it to `ChatRequest(**json_data)`.
3. If validation fails → 422 Unprocessable Entity with error details.
4. If validation passes → your handler gets a fully validated `ChatRequest` object.

**Validation happens before your code runs**. You don't write `if not request.message: return 400`. FastAPI handles it.

### 4b. Response models

```python
@router.post("/upload", response_model=IngestStatus)
async def upload_document(...) -> IngestStatus:
    document = await create_pending_document(...)
    return IngestStatus(id=document.id, status=document.status, ...)
```

`response_model=IngestStatus` tells FastAPI:
1. **Filter** the return value through the Pydantic model (extra fields are dropped, validators applied).
2. **Serialise** to JSON.
3. **Document** the response shape in OpenAPI.

**Why not just return `document` (an ORM object)?** Because SQLAlchemy models aren't Pydantic models — they can't be serialised to JSON. More importantly, ORM models expose your internal DB schema. If you add an internal field to the ORM model, it shouldn't automatically appear in the API response. The Pydantic schema layer gives you control.

### 4c. `Literal` for constrained values

```python
IngestStage = Literal["queued", "chunking", "embedding", "persisting", "completed", "failed"]
force_route: Literal["tutor", "quiz"] | None = Field(None)
```

`Literal` restricts the value to a specific set of strings. If the client sends `force_route="tutor_mode"`, FastAPI returns 422 before your handler runs. This is better than `if route not in ("tutor", "quiz"): raise 422` — the validation is in the schema, not the business logic.

### 4d. `Field()` for metadata and constraints

```python
session_id: UUID | None = Field(
    None,
    description="Optional session identifier. If omitted, the server mints a new one...",
)
message: str = Field(..., min_length=1)
chunk_count: int = Field(0, description="Number of chunks produced.")
```

`Field(...)` (with `...` as first arg) = required. `Field(None)` = optional, default is `None`. The `description` appears in the OpenAPI schema — visible in Swagger UI without reading code.

---

## 5. Async — the foundation everything builds on

FastAPI is ASGI (Asynchronous Server Gateway Interface). Unlike WSGI (Flask, Django), ASGI processes one request across its lifecycle without blocking a thread. This enables:

- **Concurrent I/O**: while waiting for Postgres, another request is served.
- **Streaming**: tokens can be sent to the client character-by-character without buffering.

### 5a. `async def` route handlers

```python
@router.post("/upload")
async def upload_document(...) -> IngestStatus:
    data = await file.read()           # I/O: reads file from network
    parsed = parse_upload(...)         # CPU: synchronous, fast
    document = await create_pending_document(session, ...)  # I/O: Postgres INSERT
    await arq_pool.enqueue_job(...)    # I/O: Redis LPUSH
    return await _to_status(session, document)  # I/O: Postgres SELECT
```

Every `await` is a suspension point: FastAPI can process another request while this one waits. This is why FastAPI handles many concurrent requests with a single Python thread — it never blocks waiting for I/O.

**Important gotcha**: `async def` does NOT make synchronous code async. If you call a synchronous CPU-bound function (like `model.encode()`) directly in `async def`, it blocks the event loop and starves all other requests:

```python
# BAD: blocks the event loop during model inference
async def embed(texts):
    vectors = model.encode(texts)   # synchronous, takes 500ms → blocks everything
    return vectors

# GOOD: runs in a thread pool, event loop stays free
async def embed(texts):
    vectors = await asyncio.to_thread(model.encode, texts)
    return vectors
```

`asyncio.to_thread()` submits the synchronous function to a thread pool worker. The event loop is free to process other requests while the CPU-bound work runs on a different OS thread.

### 5b. `asyncio.gather()` for parallel I/O

```python
# backend/app/routers/health.py
postgres_ok, redis_ok = await asyncio.gather(
    _check_postgres(),       # SELECT 1 — might take 10ms
    _check_redis(client),    # PING — might take 5ms
)
```

Without `gather`: sequential execution — total 15ms.
With `gather`: parallel execution — total max(10ms, 5ms) = 10ms.

`asyncio.gather()` runs multiple coroutines concurrently and waits for all to finish. The return value is a tuple/list of results in the same order as the inputs.

Use `gather` whenever you have multiple independent I/O operations that can proceed in parallel.

### 5c. `StreamingResponse` for SSE

```python
# backend/app/routers/chat.py
async def _token_stream(...) -> AsyncIterator[bytes]:
    async for event in graph.astream_events(initial_state, version="v2"):
        if event["event"] == "on_chat_model_stream":
            text = event["data"]["chunk"].content
            yield f"data: {json.dumps({'delta': text})}\n\n".encode()
    yield b"data: [DONE]\n\n"

@router.post("/chat")
async def chat(...) -> StreamingResponse:
    return StreamingResponse(
        _token_stream(...),
        media_type="text/event-stream",
        headers={"X-Session-Id": str(session_id), "Cache-Control": "no-cache"},
    )
```

`StreamingResponse` takes an async generator that `yield`s bytes. FastAPI sends each yielded chunk to the client immediately over the TCP connection, without buffering the entire response.

**Server-Sent Events (SSE) wire format**:
```
data: {"delta": "The"}\n\n
data: {"delta": " CAP"}\n\n
data: [DONE]\n\n
```

Each event is `data: <payload>\n\n`. The double newline terminates the event. The client's `EventSource` API (or `fetch` + `ReadableStream`) parses this format natively.

**Why SSE instead of WebSockets?**

| SSE | WebSockets |
|---|---|
| Unidirectional (server → client) | Bidirectional |
| Works over standard HTTP/1.1 | Requires upgrade handshake |
| Auto-reconnect built into browser | Must implement reconnect yourself |
| Simpler server: just yield bytes | Complex: manage connection state |

For our use case (server streams tokens to client, client sends one message at a time), SSE is simpler and sufficient.

---

## 6. Authentication as a dependency — `app/auth.py`

```python
# backend/app/auth.py
_jwk_client: PyJWKClient | None = None  # lazy singleton — one HTTP call to Clerk, ever

def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(settings.clerk_jwks_url)
    return _jwk_client

async def get_user_id(authorization: str | None = Header(default=None)) -> str:
    if settings.dev_auth_bypass:
        # Dev mode: accept any Bearer value verbatim
        scheme, _, value = (authorization or "").partition(" ")
        return value.strip() or ANONYMOUS_USER_ID

    if not authorization:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(401, ...)

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        payload = jwt_decode(token, signing_key.key, algorithms=["RS256"],
                            issuer=settings.clerk_issuer,
                            options={"require": ["sub", "iss", "exp"]})
    except (InvalidTokenError, PyJWKClientError) as exc:
        raise HTTPException(401, ...) from exc

    return payload["sub"]   # e.g. "user_2abc123def"
```

**Why authentication as a FastAPI dependency (not middleware)?**

Middleware runs on *every* request. The health check at `/health` should work without auth — it's called by Docker and load balancers that don't have tokens. If auth were middleware, you'd need special-case logic to exclude public routes.

As a dependency, auth is opt-in per-route: routes that declare `user_id: str = Depends(get_user_id)` require auth. Routes without it (like `/health`) are public by default.

**`Header(default=None)`**: FastAPI reads from the HTTP `Authorization` header. `default=None` means a missing header gives `None` instead of a 400 error — which lets us write the "missing header → 401" logic ourselves with the right response.

**JWKS caching**: `PyJWKClient` fetches Clerk's public keys once and caches them. The module-level `_jwk_client` singleton means we make one HTTP call to Clerk's JWKS endpoint per app process lifetime, not per request.

**Dev bypass**: `settings.dev_auth_bypass=True` accepts any Bearer value verbatim. This lets `curl` smoke testing work without real Clerk JWTs. The app logs a `WARNING` on boot — you can't accidentally leave it on unnoticed.

---

## 7. Database integration — `app/db.py` and SQLAlchemy async

```python
# backend/app/db.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = create_async_engine(settings.database_url, future=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session
```

**`create_async_engine`** with `postgresql+asyncpg://...` uses `asyncpg` as the Postgres driver. `asyncpg` is a pure-async Postgres driver — unlike `psycopg2` (sync), it never blocks the event loop.

**`expire_on_commit=False`**: after `session.commit()`, SQLAlchemy normally expires all ORM objects (to force a re-read on next access). In async, lazy attribute loading raises `MissingGreenlet`. Setting `expire_on_commit=False` keeps committed objects' values in memory. This is mandatory for async SQLAlchemy.

**`get_session` as a yield dependency**: the `async with async_session_maker() as session:` context manager auto-commits on clean exit and auto-rollbacks on exception. Combined with FastAPI's dependency lifecycle:

```
request arrives
  → FastAPI calls get_session()
  → enters async with → session opened
    → route handler runs
    → (normal exit) session.commit() called implicitly
    → session closed
  OR
    → (exception raised) session.rollback() called implicitly
    → session closed
  → FastAPI returns the response
```

You never manually manage transactions in route handlers — the dependency handles it.

**Using the session in routes**:

```python
async def upload_document(session: AsyncSession = Depends(get_session)):
    # Add a row
    doc = Document(...)
    session.add(doc)
    await session.commit()
    await session.refresh(doc)  # load server-generated values (id, created_at)
    return doc

    # Query rows
    result = await session.scalar(select(Document).where(Document.id == doc_id))

    # Delete
    result = await session.execute(delete(Document).where(...))
```

**Never `session_maker()` directly in routes** — always `Depends(get_session)`. The dependency ensures cleanup.

---

## 8. Schema migrations with Alembic

```
backend/
├── alembic.ini              # points to alembic/ folder and database URL
└── alembic/
    ├── env.py               # async Alembic setup (runs migrations using asyncpg)
    └── versions/
        ├── 0001_baseline.py
        ├── 0002_add_user_id_to_documents.py
        └── 0003_add_document_stage.py
```

Alembic is the migration tool for SQLAlchemy. Each migration file has:

```python
revision: str = "0002_add_user_id_to_documents"
down_revision: str = "0001_baseline"   # chains the migrations in order

def upgrade() -> None:
    op.add_column("documents", sa.Column("user_id", sa.String(64), nullable=True))
    op.create_index("ix_documents_user_id", "documents", ["user_id"])

def downgrade() -> None:
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_column("documents", "user_id")
```

**The migration chain**: `0001 → 0002 → 0003`. Alembic tracks the applied revision in a `alembic_version` table in Postgres. Running `alembic upgrade head` applies all unapplied migrations in order.

**Why not `Base.metadata.create_all()`?** That recreates all tables from scratch — fine for tests (`tests/conftest.py` uses it), catastrophic for production (drops existing data). Migrations are additive and reversible.

**Why `nullable=True` on new columns?** Adding a `NOT NULL` column to an existing table without a `DEFAULT` fails on Postgres if there are existing rows. Always add new columns as nullable first, then backfill data, then (optionally) add the NOT NULL constraint.

**Running migrations**:
```bash
cd backend
uv run alembic upgrade head     # apply all pending migrations
uv run alembic downgrade -1     # revert last migration
uv run alembic current          # show which revision is applied
uv run alembic history          # show migration chain
```

**Async Alembic** (`alembic/env.py`): because the app uses `asyncpg`, Alembic's `env.py` is configured to run migrations asynchronously using `asyncio.run()` and `AsyncConnection`. This is a separate config from the norm — the default Alembic `env.py` is sync.

---

## 9. Workers and background jobs — ARQ integration

```python
# backend/app/workers/ingest_worker.py
async def embed_document(ctx: dict, *, document_id: str, text: str) -> None:
    """ARQ job: dequeued by the worker process, runs off the API thread."""
    async with async_session_maker() as session:
        await embed_pending_document(session, UUID(document_id), text)

class WorkerSettings:
    functions = [embed_document]
    redis_settings = _redis_settings()   # Redis DB 1
    max_tries = 1
    keep_result = 300

async def get_arq_pool() -> ArqRedis:
    return await create_pool(_redis_settings())

def get_arq_pool_dep(request: Request) -> ArqRedis:
    return request.app.state.arq_pool
```

**Why a background worker?** PDF embedding takes 5–30 seconds. If the route handler did it synchronously, the HTTP request would hang. The 202 pattern:
1. Route: parse → create DB row → enqueue → return 202.
2. Worker: dequeue → chunk → embed → store → update status.

The client polls `GET /ingest/{id}` until `status == "completed"`.

**ARQ uses Redis DB 1** — separate from chat sessions on DB 0. Same Redis instance, different logical DB:
```python
@property
def arq_redis_url(self) -> str:
    parsed = urlparse(self.redis_url)
    return f"{parsed.scheme}://{parsed.netloc}/1"  # DB 1 for ARQ
```

This ensures `arq:*` job keys never collide with `chat:user:*` session keys.

**The worker is a separate OS process**, started independently:
```bash
uv run arq app.workers.ingest_worker.WorkerSettings
```

It connects to the same Postgres and Redis but is completely independent from the FastAPI process. The API enqueues jobs; the worker dequeues and runs them.

---

## 10. OpenAPI / Swagger auto-documentation

FastAPI generates interactive API docs at `/docs` (Swagger UI) and `/redoc` (ReDoc). This happens automatically — you don't write a spec.

What drives the generated docs:

| Source | What it affects |
|---|---|
| `FastAPI(title=..., version=..., description=...)` | Docs top-level metadata |
| `openapi_tags = [{name, description}]` | Route group descriptions |
| `@router.post(..., summary=..., description=...)` | Endpoint descriptions |
| `response_model=FooModel` | Response body schema |
| `status_code=202, responses={413: {...}}` | Status codes and their descriptions |
| `Field(description=...)` on Pydantic fields | Per-field docs in request/response schemas |
| `Depends(get_user_id)` → `Header(...)` | Documents required headers |

**The `responses` dict** adds non-success status codes to the docs:
```python
@router.post("/upload",
    responses={
        202: {"description": "Job accepted and queued."},
        413: {"description": "Upload exceeds the configured size limit."},
        415: {"description": "Unsupported file type."},
    }
)
```

This is documentation only — FastAPI doesn't validate that your handler actually returns 413. You're documenting a contract.

---

## 11. Testing FastAPI applications

### 11a. The test client without a running server

```python
# backend/tests/conftest.py
from httpx import ASGITransport, AsyncClient
from app.main import app

@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
```

`ASGITransport` lets HTTPX talk directly to the ASGI app in-process, with no network stack. Fast, isolated, no port conflicts.

### 11b. Dependency overrides for clean isolation

```python
# Override auth — no real JWT needed in tests
async def _test_get_user_id(authorization: str | None = Header(default=None)) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ANONYMOUS_USER_ID

app.dependency_overrides[get_user_id] = _test_get_user_id

# Override ARQ pool — no real Redis worker needed
class _StubArqPool:
    def __init__(self): self.calls = []
    async def enqueue_job(self, function, *args, **kwargs):
        self.calls.append({"function": function, "kwargs": kwargs})

app.dependency_overrides[get_arq_pool_dep] = lambda: arq_pool_stub
```

Tests that send `Authorization: Bearer user-alice` will have `get_user_id` return `"user-alice"` — a simple, controllable identity without a real Clerk JWT.

Tests that call `POST /ingest/upload` will see `arq_pool_stub.calls[0]["function"] == "embed_document"` — verifying the route enqueued the job without running a real worker.

### 11c. Test fixtures for DB and Redis isolation

```python
@pytest_asyncio.fixture
async def db_clean():
    # Truncate before each test
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE documents, document_chunks RESTART IDENTITY CASCADE"))
    yield

@pytest_asyncio.fixture
async def redis_clean():
    # Sweep all session keys before and after each test
    async for key in redis_client.scan_iter(match="chat:user:*"):
        await redis_client.delete(key)
    yield
    async for key in redis_client.scan_iter(match="chat:user:*"):
        await redis_client.delete(key)
```

Each test gets a clean DB and Redis state. Tests that need data use the `seeded_chunks` fixture which inserts real embeddings into Postgres.

### 11d. Schema creation for tests (vs migrations for production)

```python
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _create_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # OK for tests
    yield
```

Tests use `create_all` (creates tables from ORM models). Production uses Alembic migrations. This is intentional: tests get a clean schema every session; production gets incremental, reversible migrations. The `scope="session"` ensures schema is created once per pytest session, not once per test.

---

## 12. Patterns used across this project — quick reference

Every decision in this project maps to a general FastAPI pattern. Here's the consolidated list:

### Route organisation
```python
router = APIRouter(prefix="/ingest", tags=["ingest"])
app.include_router(router)
```
One `APIRouter` per concern area. `prefix` avoids repeating `/ingest` on every route. `tags` groups routes in Swagger.

### Resource lifecycle
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.shared_resource = await open_it()
    yield
    await app.state.shared_resource.close()
```
Open once, store on `app.state`, close in `finally`.

### Per-request resources
```python
async def get_resource():
    resource = await acquire()
    try:
        yield resource
    finally:
        await release(resource)
```
Yield-based dependency ensures cleanup even on exceptions.

### Auth guard
```python
async def get_current_user(token: str = Header(...)) -> User:
    user = verify(token)
    if not user:
        raise HTTPException(401)
    return user

@router.get("/protected")
async def protected_route(user: User = Depends(get_current_user)):
    ...
```

### Async I/O
```python
# Parallel DB + cache reads
result, cached = await asyncio.gather(db_query(), cache_read())

# CPU-bound work off the event loop
vectors = await asyncio.to_thread(model.encode, texts)
```

### Streaming responses
```python
async def stream_generator() -> AsyncIterator[bytes]:
    async for chunk in llm.astream(prompt):
        yield f"data: {chunk}\n\n".encode()
    yield b"data: [DONE]\n\n"

return StreamingResponse(stream_generator(), media_type="text/event-stream")
```

### Test isolation
```python
app.dependency_overrides[real_dep] = test_dep  # in conftest.py
```

---

## 13. Things FastAPI does not do (and what to use instead)

FastAPI is not a batteries-included framework. Knowing what it *doesn't* handle helps you plan:

| Problem | What to reach for |
|---|---|
| **DB schema migrations** | Alembic (what we use) |
| **Background jobs** | ARQ, Celery, RQ, or native `asyncio.create_task()` for in-process |
| **Rate limiting** | `slowapi` (wraps `limits` for FastAPI) or Redis INCR + EXPIRE |
| **WebSockets** | FastAPI has native WebSocket support; for fan-out use a pub/sub backend (Redis, Kafka) |
| **File serving** | `StaticFiles` mount or an upstream CDN |
| **Admin UI** | Not included; `SQLAdmin` is a third-party option |
| **Session cookies** | `itsdangerous` + a custom middleware; Starlette has `SessionMiddleware` |
| **Task scheduling (cron)** | APScheduler, `arq` with delayed jobs, or a separate cron container |

**The principle**: FastAPI gives you the request/response lifecycle, dependency injection, and serialisation. Everything else — background work, caching, scheduling, auth providers — is your responsibility to integrate.

---

*Generated by reading every backend file in recommended order, referencing the Postgres, Redis, and Docker notes for format consistency — August 2026.*
