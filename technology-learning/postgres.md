# Postgres in the Personal AI Tutor — production-level depth

This is not a Postgres tutorial. It's a guided tour of how this project uses Postgres — *every* decision is paired with the alternative we rejected and why. Read this in the order it's laid out; later sections build on earlier ones.

---

## 1. Why Postgres at all (and why pgvector)

Picking a database is mostly picking what you *won't* run. The shortlist for this project was:

| Option | Why we considered it | Why we didn't pick it |
|---|---|---|
| **SQLite** | Zero ops, runs in-process, fast for single-user. | No real concurrency, no pgvector, no JSONB indexing, no logical replication. Once two users hit `/chat` concurrently it falls over. |
| **MySQL** | Cheap to host, ubiquitous. | No first-class vector type, JSON support is weaker than JSONB, no `RETURNING` until 8.0 (and even then it's anaemic). Loses on the LLM workload because every vector retrieval would need a separate store. |
| **Dedicated vector DB (Pinecone, Weaviate, Qdrant, Milvus)** | Purpose-built for ANN search; usually fast. | Forces you to run *two* stores. Every `/chat` turn would need a Postgres lookup for chunk metadata *and* a vector-DB lookup for the embedding. JOINs across stores become application-level fan-out. Doubles the ops surface for no benefit at our scale. |
| **Postgres + pgvector** | One store for relational + vector. JOINs between metadata and embeddings are trivial. Single transaction across both. | First-class but you're responsible for picking the right ANN index (HNSW vs IVFFlat) and tuning its parameters. Embeddings are big (768 floats × 4 bytes = ~3 KB/chunk uncompressed); the table grows fast. |

We picked Postgres + pgvector. Concretely: `pgvector/pgvector:pg16` (`docker-compose.yml:3`) gives us Postgres 16 with the extension baked in.

The decision is documented in `ARCHITECTURE.md` §4:

> Postgres + pgvector is the single source of truth for both relational data (documents, chunks, ingestion jobs, future user/session tables) and vector embeddings. Using one store instead of a dedicated vector DB keeps the deployment surface tiny and makes joins between metadata and embeddings trivial.

**The principle**: prefer fewer stateful services. Every new service in the data plane is another thing to back up, monitor, patch, and reason about under failure.

---

## 2. How this project uses Postgres

Three logical concerns live in a single Postgres instance:

### 2a. Document metadata (`documents` table)

One row per ingestion. Columns capture *what* was ingested and *who owns it*:

```python
# backend/app/models.py
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
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
```

### 2b. Chunks + embeddings (`document_chunks` table)

One row per chunk produced by the splitter. The `embedding` column is a fixed-width `vector(768)` from pgvector:

```python
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
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
```

### 2c. Schema bootstrap via init scripts + Alembic

`postgres/init/01-extensions.sql` runs once on first boot of the data volume:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

After that, Alembic owns the schema. The current head is `0003_add_document_stage` (Phase 10), running on top of `0001_baseline` and `0002_add_user_id_to_documents`.

The chat endpoint *reads* from Postgres for retrieval but doesn't write to it — chat state lives in Redis. So Postgres traffic in the running system is:
- ingest: ~10 inserts per document (1 doc row + N chunk rows)
- chat retrieval: 1 ANN query per turn, returning top-K chunks
- admin: list + delete operations

---

## 3. Schema design considerations (what's load-bearing)

Each decision below is the *production* one — the version you'd defend in a review.

### 3a. UUIDs for primary keys, not bigserial

```python
id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
```

**Why UUIDs**:
- Client-mintable. The API can hand back the `id` *before* the row is committed, useful for the async ingest flow where the frontend needs the id to start polling.
- No enumeration attacks. A bigserial leaks how many documents have ever been ingested; a UUID doesn't.
- Stable across DB migrations or merges (no clashing sequences).

**Why not just `int`**:
- Insert hotspots. If you bulk-insert with a sequence, multiple workers contend on the same B-tree leaf at the high end. UUIDs spread inserts across the tree.

**Caveat**: UUIDs are 16 bytes vs 8 for bigint. Every secondary index that includes the PK pays this cost. At our row counts this is irrelevant; at 10⁹ rows, it matters.

**Production refinement**: use `uuid_generate_v7()` (time-ordered UUIDv7) if your Postgres is recent enough — gets you UUID benefits *and* B-tree locality. We use v4 because pgvector's docker image was on Postgres 16 when this project started.

### 3b. `TIMESTAMP(timezone=True)` always, never `TIMESTAMP` (a.k.a. `timestamptz`)

```python
created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
```

`timestamp without time zone` is a footgun. It stores the wall-clock as-given and forgets what offset produced it. Two services in different TZs writing to the same column produce silently incoherent data.

`timestamptz` stores everything as UTC internally and converts on read using the session's `TimeZone` setting. **Always pick `timestamptz`**. This is one of those rare "always do X" rules.

`server_default=func.now()` evaluates `now()` on the DB side, not the Python side — guarantees consistency even if multiple app processes have skewed clocks.

`onupdate=func.now()` on `updated_at`: SQLAlchemy fires the update at flush time, not via a Postgres trigger. If you want bulletproof `updated_at` you'd add a trigger; for an app-internal field this is fine.

### 3c. `Text` vs `VARCHAR(N)` — when each is right

```python
source_uri: Mapped[str] = mapped_column(Text, nullable=False)    # URLs, no sensible length cap
title: Mapped[str | None] = mapped_column(Text)                  # human-typed, varies wildly
status: Mapped[str] = mapped_column(String(16), nullable=False)  # closed enum, ~10 chars max
user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # Clerk format
```

In Postgres, `VARCHAR(N)`, `VARCHAR`, and `TEXT` are *the same on-disk representation* (TOAST'd if large). Length checks on `VARCHAR(N)` are enforced at insert time but cost no storage.

**Picking between them**:
- `Text` for anything user-typed or unbounded (titles, URLs, error messages, content).
- `String(N)` for enums, identifiers, fixed-format tokens — gives you a cheap sanity check that protects against bad inserts.

Don't use `String(N)` thinking it saves storage. It doesn't.

### 3d. JSONB, never JSON

```python
doc_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
```

- `JSON` stores the raw text. Every read re-parses it. Can't be indexed for key lookups.
- `JSONB` stores a binary representation. Slightly more expensive to write (it has to canonicalise + sort keys), but cheaper to read, and supports GIN indexing for `?`, `@>`, etc.

**Rule**: if you don't have a hard reason to preserve whitespace/order, pick `JSONB`. We've never needed `JSON` here.

**On the schema-vs-JSONB axis**: every field you put in JSONB is a field you can't index efficiently and can't constrain. Use JSONB for *open-ended* extension data (e.g. `doc_metadata: {"format": "pdf", "page_count": 7}` — pdf-specific). Use columns for anything queryable.

A common mistake: putting a `status` field inside JSONB because it "felt flexible". Now every `WHERE status = 'foo'` query has to deserialise JSONB on every row. We avoided that — `Document.status` is its own column.

### 3e. Foreign keys with `ON DELETE CASCADE`

```python
document_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
)
```

When the user deletes a document via the admin UI, the chunks must go too. Three places this could be enforced:
- **Application code**: `await session.delete(document); for c in chunks: await session.delete(c)`. Fragile (forget one place and you leak rows), slow (N+1 deletes), and doesn't protect against a direct SQL delete.
- **ORM cascade only** (`relationship(cascade="all, delete-orphan")`): SQLAlchemy issues child DELETEs in order. Same problems — only enforced when going through the ORM.
- **DB-level `ON DELETE CASCADE`**: the FK constraint guarantees referential integrity *no matter who issues the parent delete* (app, psql, another service).

**We use all three**, but the DB-level cascade is the one that actually matters. The ORM-level `passive_deletes=True` tells SQLAlchemy "I know the DB will handle the cascade — don't issue child DELETEs". This is what makes the route's one-line `delete(Document).where(...)` work atomically.

Verify it's in place — this query saved us during Phase 9:

```bash
docker exec tutor-postgres psql -U tutor -d tutor -c \
  "SELECT confdeltype FROM pg_constraint WHERE conname = 'document_chunks_document_id_fkey';"
# Expected: 'c' — CASCADE. 'n' would be NO ACTION, 'r' RESTRICT, 'a' SET NULL.
```

`confdeltype = 'c'` means the constraint is *actually* CASCADE in the running DB, not just in your model definition.

### 3f. Unique constraints inside a parent

```python
UniqueConstraint("document_id", "chunk_index", name="uq_chunk_per_document"),
```

`(document_id, chunk_index)` is unique, but `chunk_index` alone isn't (chunk 0 of doc A and chunk 0 of doc B coexist). This is the standard "uniqueness within a partition" pattern.

Subtle but important: this constraint also gives us a B-tree index on `(document_id, chunk_index)`. So queries like:

```sql
SELECT * FROM document_chunks
WHERE document_id = '…' ORDER BY chunk_index;
```

are index-driven, no separate index needed.

### 3g. Nullable columns convey intent

```python
user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
```

`user_id` is nullable because pre-Phase-9 rows existed before ownership was a concept; backfilling them to a fake owner would have attributed them wrongly. `NULL` means "legacy / unowned".

`stage` is nullable because pre-Phase-10 rows existed before the column did. The Alembic migration adds the column with `NULL` for existing rows.

**Both nullables are load-bearing semantics, not laziness.** When you make a column nullable, write down *what NULL means*. Future you will thank present you.

The flip side: `nullable=False` everywhere by default unless you have a real reason. NULLs propagate weirdly in SQL (`NULL = NULL` is `NULL`, not `TRUE`), break aggregations, and confuse `WHERE` clauses.

---

## 4. Indexing — what we have and why

Indexes are the single biggest performance lever in a transactional DB. Get them right and queries are fast; get them wrong and you've doubled your write cost for no benefit.

### 4a. B-tree indexes — the default

```python
document_id: Mapped[uuid.UUID] = mapped_column(..., index=True)
user_id: Mapped[str | None] = mapped_column(..., index=True)
```

`index=True` in SQLAlchemy creates a default B-tree. B-tree handles equality (`=`), range (`<`, `<=`, `>`, `>=`), and ordering (`ORDER BY indexed_col`). For our access patterns:

- `WHERE document_id = '…'` — used by chunk fetches.
- `WHERE user_id = '…'` — used by `/documents` list.
- `ORDER BY created_at DESC` — used by `/documents` for sort.

We don't have an explicit `created_at` index because the table is small and the query already filters by `user_id` first. **If `/documents` ever paginates over millions of rows, the right index would be a composite `(user_id, created_at DESC)` so the index *itself* satisfies the sort.**

A useful mental model: a composite index `(a, b)` can serve queries that filter on `a` alone, or `a AND b`, but **not** `b` alone. Order matters. Pick the leading column as the one with highest cardinality in your typical filters.

### 4b. HNSW index for vector search

```python
Index(
    "ix_document_chunks_embedding_hnsw",
    "embedding",
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_with={"m": 16, "ef_construction": 64},
),
```

HNSW (Hierarchical Navigable Small World) is an Approximate Nearest Neighbor index. It builds a graph where nearby vectors are connected; queries do a greedy walk from a random entry point toward the query vector.

**Why HNSW over IVFFlat** (the other pgvector option):
- HNSW is *probabilistic perfect*: at small datasets it's essentially exact and fast.
- IVFFlat needs the dataset to be representative before you build the index — you cluster centroids, then query against the nearest few clusters. If your data shifts after build, recall drops.
- HNSW is robust to data drift.
- HNSW builds slower and uses more memory, but our corpus is small (~thousands of chunks), so we don't feel it.

**Parameters that matter**:

| Param | Default | Meaning | When to change |
|---|---|---|---|
| `m` | 16 | Max graph degree (connections per node) | Higher → better recall, more memory + slower build. 16 is the standard. Bump to 32 for >1M rows. |
| `ef_construction` | 64 | Search width during build | Higher → better-quality graph, slower build. We use 64 (default). |
| `ef_search` | 40 (default at query time) | Search width during retrieval | **Tunable at query time** — `SET LOCAL hnsw.ef_search = N`. Trade recall for latency *per query*. |

`vector_cosine_ops` tells the index to use cosine distance. Our embeddings are normalised (`SentenceTransformer.encode(normalize_embeddings=True)` in `backend/app/services/embeddings.py`), so cosine and dot-product are equivalent — but the operator class has to match what your query uses (`<=>` for cosine, `<->` for L2, `<#>` for inner product).

**To verify which operator class an index uses**:

```sql
SELECT i.indexname, am.amname AS index_type, opc.opcname AS operator_class
FROM pg_indexes i
JOIN pg_class c ON c.relname = i.indexname
JOIN pg_index idx ON idx.indexrelid = c.oid
JOIN pg_am am ON am.oid = c.relam
JOIN pg_opclass opc ON opc.oid = idx.indclass[0]
WHERE i.indexname = 'ix_document_chunks_embedding_hnsw';
```

### 4c. Indexes you *should not* add

- `index=True` on every column. Every index doubles your write cost and triples your storage. Add indexes when query plans need them, not preemptively.
- Indexes on JSONB columns without expression specificity. A GIN index on the *whole* JSONB column works but is heavy; prefer indexes on extracted expressions like `(doc_metadata->>'format')` if you know what you're querying.
- Low-cardinality B-trees (e.g. on `status` when only three values exist). The optimiser usually skips them. If you really need them, consider partial indexes: `CREATE INDEX … ON documents(user_id) WHERE status = 'completed'`.

### 4d. How indexes are issued in our migrations

Look at `backend/alembic/versions/0001_baseline.py:90`:

```python
op.create_index(
    "ix_document_chunks_embedding_hnsw",
    "document_chunks",
    ["embedding"],
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_with={"m": 16, "ef_construction": 64},
)
```

HNSW index *build* takes minutes-to-hours on millions of rows. **In production you'd use `CREATE INDEX CONCURRENTLY`** which doesn't lock writes:

```python
op.create_index(..., postgresql_concurrently=True)
```

Alembic supports this but the migration must be split into its own transaction (no other DDL in the same migration). We don't use CONCURRENTLY in our baseline because at our scale it doesn't matter; for a real prod system, you'd switch.

---

## 5. Performance — concrete levers

### 5a. EXPLAIN ANALYZE — your starting point

For any slow query, the first thing you do is:

```sql
EXPLAIN (ANALYZE, BUFFERS) <your query>;
```

`ANALYZE` runs the query; `BUFFERS` shows shared buffer hits vs reads. Look for:
- `Seq Scan` where you expected `Index Scan` — your index isn't being used.
- `Rows Removed by Filter: <large number>` — the optimiser fetched too much.
- `Sort … Method: external merge Disk: …` — sort spilled to disk; `work_mem` is too small.

For pgvector specifically:

```sql
EXPLAIN ANALYZE
SELECT id, content, embedding <=> $1::vector AS distance
FROM document_chunks
ORDER BY embedding <=> $1::vector
LIMIT 5;
```

You want to see `Index Scan using ix_document_chunks_embedding_hnsw`. If you see `Seq Scan`, the index isn't being used — usually because the operator (`<=>`, `<->`, `<#>`) doesn't match the operator class.

### 5b. Connection pooling — asyncpg pool vs PgBouncer

```python
# backend/app/db.py
engine = create_async_engine(settings.database_url, future=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

SQLAlchemy + asyncpg gives you an *in-process* connection pool. Default size is 5 connections per process. This works for a single API process talking to a single Postgres.

**It breaks down when**:
- You run multiple API replicas. Postgres `max_connections` defaults to 100. Five replicas × 5 connections × 2 (idle + active) = 50, fine. Twenty replicas blow past it.
- You scale workers. Each ARQ worker process opens its own pool (see `app/workers/ingest_worker.py::async_session_maker`). 5 workers × 5 conns is another 25.

**The production fix is PgBouncer** (or pgcat) — a connection pooler in front of Postgres. It multiplexes thousands of client connections onto a small pool of real Postgres connections, in three modes:

| Mode | What it does | Compatibility |
|---|---|---|
| **session** | Client gets a real Postgres connection for the duration of their connection. | Same as no pooler. |
| **transaction** | Connection is held only for the duration of a transaction. | Breaks `SET LOCAL`, advisory locks, prepared statements unless you're careful. |
| **statement** | Connection released after each statement. | Breaks transactions. Don't use unless you understand exactly why. |

asyncpg has a wrinkle: it uses prepared statements by default, which interact poorly with PgBouncer transaction mode. The fix:

```python
create_async_engine(
    settings.database_url,
    connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
)
```

If you scale this project, you'd add PgBouncer between the API/worker pods and Postgres, and disable asyncpg's statement cache.

### 5c. Vacuum, autovacuum, MVCC — what you can't ignore

Postgres uses MVCC: every row update writes a *new* row version; the old version is marked obsolete but not immediately removed. Autovacuum reclaims those dead rows.

If autovacuum can't keep up:
- Tables bloat (disk usage grows much faster than row count).
- Indexes bloat (index scans get slower).
- Eventually you hit transaction-id wraparound — the database stops accepting writes.

**For our workload** (write-mostly via ingestion, read-heavy via chat retrieval), this won't show up at small scale. But two things to monitor in production:

```sql
SELECT relname, n_live_tup, n_dead_tup,
       n_dead_tup::float / NULLIF(n_live_tup, 0) AS bloat_ratio,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC;
```

`bloat_ratio > 0.2` means autovacuum is falling behind for that table. Tune `autovacuum_vacuum_scale_factor` (default 0.2 = vacuum when 20% of rows are dead).

```sql
SELECT datname, age(datfrozenxid) AS xid_age FROM pg_database;
```

`xid_age` close to 2 billion is an emergency. Schedule manual `VACUUM FREEZE` if you're approaching it.

### 5d. work_mem, shared_buffers, effective_cache_size

These are the three knobs that affect everything. Defaults are conservative (designed to run on 1-CPU 1-GB VMs).

| Setting | Default | What it does | Reasonable production value |
|---|---|---|---|
| `shared_buffers` | 128 MB | Postgres's own buffer cache | 25% of RAM |
| `effective_cache_size` | 4 GB | Hint to planner about OS cache | 50–75% of RAM |
| `work_mem` | 4 MB | Per-sort / per-hash memory | 16–64 MB (× concurrent ops) |
| `maintenance_work_mem` | 64 MB | For index builds, vacuum | 1–2 GB if you can spare it |

`work_mem` is per *operation*, not per connection. A query with 5 sorts and 200 connections could use 1 GB of `work_mem`. Tune carefully.

In docker-compose you'd inject these via:

```yaml
postgres:
  command:
    - postgres
    - -c
    - shared_buffers=512MB
    - -c
    - effective_cache_size=1536MB
    - -c
    - work_mem=16MB
```

We don't tune these in the local dev compose; defaults are fine for a single-user laptop.

### 5e. pg_stat_statements — slow-query log without the log

The single most useful Postgres extension after `pgvector`. Tracks every query's plan, total time, mean time, and rows.

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

Then:

```sql
SELECT query, calls, mean_exec_time, total_exec_time, rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

Tells you which queries are eating the database's wall-clock time. We don't enable it in dev but you'd want it on every prod instance.

---

## 6. Migrations with Alembic

Schema changes go through Alembic. The `alembic/` directory is committed; the `versions/` subdirectory holds one Python file per migration, chained by `down_revision`.

### 6a. Why Alembic over raw SQL

Three alternatives:

| Approach | Why people pick it | Why we don't |
|---|---|---|
| Raw `.sql` files committed to repo | Simple, no Python needed. | No down migrations. No "current schema version" tracking. No transactionally-applied DDL chaining. |
| `Base.metadata.create_all()` at boot | Trivial to set up. | Drift between model and DB is undetectable. Can't evolve schemas — DDL only runs on empty DBs. |
| Alembic | Versioned, reversible, idiomatic with SQLAlchemy. | Verbose; you have to write each migration by hand (autogen is unreliable for nontrivial changes). |

We use Alembic because we need to evolve the schema *while preserving data* (Phase 9 added `user_id` to existing rows, Phase 10 added `stage`). `create_all` would have wiped the table.

Look at our chain:

```
0001_baseline                   # initial table + HNSW index
   ↓
0002_add_user_id_to_documents   # Phase 9: add user_id column + index
   ↓
0003_add_document_stage         # Phase 10: add stage column
```

Each migration declares its parent via `down_revision`. The `alembic_version` table in the DB records the currently applied head.

### 6b. The migration file structure

```python
# backend/alembic/versions/0003_add_document_stage.py
revision: str = "0003_add_document_stage"
down_revision: Union[str, None] = "0002_add_user_id_to_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("stage", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "stage")
```

Two functions: `upgrade()` and `downgrade()`. `op.<verb>` is the Alembic API for cross-DB DDL (most ops also have a `postgresql_*` keyword for PG-specific tuning).

**Always write `downgrade()`**. You won't usually use it in prod, but local dev iterates fast — being able to bounce between revisions saves hours.

### 6c. The commands you'll use

```bash
# Run all pending migrations
uv run alembic upgrade head

# Run the next single migration
uv run alembic upgrade +1

# Roll back one
uv run alembic downgrade -1

# Roll back to a specific revision
uv run alembic downgrade 0002_add_user_id_to_documents

# Inspect current applied version
uv run alembic current

# Inspect what's pending
uv run alembic heads

# Show the full chain
uv run alembic history --verbose

# Generate a new migration scaffold
uv run alembic revision -m "add foo column to bar"

# Auto-detect changes vs models (USE WITH CARE; verify output manually)
uv run alembic revision --autogenerate -m "..."
```

### 6d. Stamping vs upgrading — the "I already have a populated DB" case

If you have a database that already has the schema (e.g. you ran `Base.metadata.create_all` historically) but no `alembic_version` row, running `upgrade head` will fail because Alembic tries to re-apply the baseline.

**The fix is `alembic stamp`**:

```bash
uv run alembic stamp head
```

This writes the head revision into `alembic_version` without running any SQL. The DB is now "at head" in Alembic's eyes.

We use this exactly once per environment — at the cutover from "create_all on boot" to "Alembic-managed". After that, only `upgrade` is used.

### 6e. Data migrations vs schema migrations

The migrations we have are all *schema* migrations: they change the structure, not the data.

When you need to migrate *data* — e.g. backfilling a new column based on existing rows — you do it inside the migration:

```python
def upgrade() -> None:
    op.add_column("documents", sa.Column("normalized_title", sa.Text, nullable=True))
    op.execute("""
        UPDATE documents SET normalized_title = lower(trim(title));
    """)
    op.alter_column("documents", "normalized_title", nullable=False)
```

Three steps in one migration: add nullable, backfill, set NOT NULL. **This is the safe pattern** because:
- Adding NOT NULL on an existing table fails unless every row has a value.
- Trying to set NOT NULL and backfill in one step risks half-done state.

**For very large tables**, `UPDATE` of a whole table holds a long lock. You'd batch it instead:

```python
op.execute("""
    DO $$
    BEGIN
        LOOP
            UPDATE documents
            SET normalized_title = lower(trim(title))
            WHERE id IN (
                SELECT id FROM documents WHERE normalized_title IS NULL LIMIT 10000
            );
            EXIT WHEN NOT FOUND;
            COMMIT;
        END LOOP;
    END $$;
""")
```

We haven't hit this scale in the project, but it's the canonical pattern.

### 6f. Migration locks — the silent killer

`ALTER TABLE … ADD COLUMN` takes an `ACCESS EXCLUSIVE` lock — every read, write, and other DDL on that table blocks until it finishes. Usually fast (milliseconds), unless you also add `DEFAULT some_value`, which forces a table rewrite.

The rule:
- `ADD COLUMN … NULL` — fast, safe.
- `ADD COLUMN … NOT NULL DEFAULT 'x'` — table rewrite, can take *hours* on big tables, locks the whole time.

**The safe path** for adding a NOT NULL with default on a large table:
1. `ADD COLUMN … NULL DEFAULT 'x'`. (Fast — only metadata changes; no row touched.)
2. Backfill `WHERE col IS NULL` in batches.
3. `ALTER COLUMN … SET NOT NULL`. (Fast on Postgres ≥ 12 if all rows have values.)

Our migrations don't trigger this trap (all our added columns are nullable). But it's the most common production migration disaster.

### 6g. Verifying migrations against the live DB

After running `upgrade head`, verify the structure matches your expectations:

```bash
# Show table structure
docker exec tutor-postgres psql -U tutor -d tutor -c "\d documents"

# Show all indexes on a table
docker exec tutor-postgres psql -U tutor -d tutor -c "\d+ documents"

# Show all constraints
docker exec tutor-postgres psql -U tutor -d tutor -c \
  "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'documents'::regclass;"

# Current Alembic head in the DB
docker exec tutor-postgres psql -U tutor -d tutor -c "SELECT version_num FROM alembic_version;"
```

---

## 7. Transactions, isolation, and async sessions

Every SQLAlchemy `AsyncSession` is backed by exactly one transaction at a time. Understanding this prevents subtle bugs.

### 7a. The transaction lifecycle

```python
async with async_session_maker() as session:
    document = Document(...)
    session.add(document)
    await session.flush()    # emits INSERT, gets id, holds lock
    # ... more work ...
    await session.commit()   # actually persists; releases locks
```

- `flush()` sends SQL to the DB but doesn't commit. Other transactions can't see the changes yet. **Useful for getting auto-generated ids (UUIDs from `default=uuid4`, sequence-generated ids) inside the same transaction**.
- `commit()` ends the transaction. Changes are visible. Locks are released.
- `rollback()` undoes everything since the last commit.

If you forget `commit()`, the transaction is rolled back when the session context exits. Many "why is my data not saved?" bugs are missing commits.

### 7b. The phase 10 stage-transition pattern

```python
# backend/app/services/ingest.py
document.stage = "chunking"
await session.commit()
chunks = chunk_text(text)

document.stage = "embedding"
await session.commit()
vectors = await provider.embed_batch(...)
```

**Why we commit between stages**: a polling client reading `document.stage` from another session can only see committed state. If we held one big transaction across all stages, the client would only see `queued` → `completed`, missing every intermediate value.

Each `commit()` releases the row lock briefly. Another transaction could intervene. For our use case (worker is the only writer to this row mid-ingest), that's fine. If multiple writers competed, you'd want explicit row locking (`SELECT … FOR UPDATE`).

### 7c. Isolation levels

Postgres defaults to `READ COMMITTED`: each statement sees the state at the moment it starts. Two statements in the same transaction can see different snapshots.

For most workloads this is fine. When it matters:
- **`REPEATABLE READ`** — the whole transaction sees one snapshot. Use for consistency reports.
- **`SERIALIZABLE`** — transactions appear to execute in some order, even concurrently. Costly; usually overkill.

Set per-transaction:

```python
await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
```

We don't change isolation in this project. Default `READ COMMITTED` plus the right indexes plus single-writer-per-row gets us where we need to go.

### 7d. The `expire_on_commit=False` setting

```python
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

By default, SQLAlchemy expires all attributes after commit — the next read goes back to the DB. That makes sense for sync sessions where you might reuse them. For async, where sessions are short-lived and you've already returned data to a route handler, expiry causes "DetachedInstanceError" headaches.

`expire_on_commit=False` keeps attributes loaded after commit. This is the right default for FastAPI/async.

---

## 8. Backup, replication, and disaster recovery

This project doesn't implement these — it's a dev tool — but you should know the shape.

### 8a. Backup strategies, ranked

| Strategy | What it does | When to use |
|---|---|---|
| `pg_dump` | Logical backup (SQL statements). Slow to restore on big DBs but portable across PG versions. | Dev snapshots, small DBs, version migrations. |
| `pg_basebackup` | Physical backup (binary files). Fast restore. Same PG version only. | Cold backups of medium DBs. |
| WAL archiving + `pg_basebackup` | Point-in-time recovery (PITR). | Any production system. |
| Snapshot at storage layer (EBS, etc.) | Cloud-fast. Needs `pg_start_backup`/`pg_stop_backup` for consistency. | Cloud-managed setups. |
| Managed (RDS, Cloud SQL, Neon) | Provider handles all of the above. | Anything you don't want to operate yourself. |

For a dev DB:

```bash
# Dump
docker exec tutor-postgres pg_dump -U tutor tutor > backup.sql

# Restore (into a fresh DB)
docker exec -i tutor-postgres psql -U tutor tutor < backup.sql
```

### 8b. Replication

- **Streaming replication**: a hot standby continuously replays WAL from the primary. Async by default (small data-loss window on primary failure); synchronous if you can afford the latency.
- **Logical replication**: replicate specific tables to a different schema, version, or system. Useful for zero-downtime upgrades.

You'd configure these in `postgresql.conf` and `pg_hba.conf`. Out of scope for a dev project, but worth knowing the vocabulary.

### 8c. PITR (point-in-time recovery)

The actual production answer for backups. WAL files are continuously archived to S3/GCS; a `pg_basebackup` runs nightly. To restore to "2 AM yesterday":
1. Restore the most recent basebackup before 2 AM.
2. Replay WAL up to the target timestamp.

Tools like `pgBackRest`, `WAL-G`, or `Barman` automate this.

---

## 9. Challenges we actually hit in this project

Documented for future reference — these are the bumps, not the polished narrative.

### 9a. Test override silently always returned anonymous

Phase 9's `_test_get_user_id` override was declared as:

```python
async def _test_get_user_id(authorization: str | None = None) -> str:
```

Without `Header(default=None)`, FastAPI treated `authorization` as a query parameter and never injected the request header. Every test that sent `Bearer user_alice` got `ANONYMOUS_USER_ID` instead. The fix:

```python
async def _test_get_user_id(authorization: str | None = Header(default=None)) -> str:
```

This isn't directly a Postgres issue, but the *symptom* was Postgres queries returning the wrong user's data. **Lesson**: when DB queries return unexpected rows, the bug is often *upstream* in the dep injection or auth layer, not in the SQL.

### 9b. `DEV_AUTH_BYPASS=1` in local `.env` made auth tests false-pass

Same kind of failure mode — environment state leaking into test assumptions. Fix in `backend/tests/conftest.py`:

```python
settings.dev_auth_bypass = False  # force off for the test session
```

**Lesson**: tests should be hermetic w.r.t. local `.env`. If your local config changes test outcomes, the tests are wrong.

### 9c. `ASGITransport` doesn't trigger lifespan events

The FastAPI test client used in this project (`httpx.ASGITransport`) does **not** invoke the `lifespan` startup/shutdown handlers by default. So `app.state.arq_pool` was never set in tests, and any route depending on it would fail.

We bypass this by overriding the dep entirely in tests rather than depending on lifespan state. This is the cleaner test-design choice anyway — overrides give you a known stub instead of a real (but flaky) connection.

### 9d. Tests share a single Postgres DB with the running app

The test fixtures `TRUNCATE`-on-each-test for isolation. If the app is running against the same DB during a test run, you'll get sporadic conflicts and the dev DB will get nuked.

In dev, just be aware. In production CI, you'd spin up an ephemeral Postgres (testcontainers, or compose's `--profile test`) so tests run against a fresh DB. We don't do this yet.

### 9e. The vector dimension is fixed at table-creation time

```python
embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=False)
```

`settings.embedding_dim = 768` is baked into the column definition at migration time. If you switch embedding models from `all-mpnet-base-v2` (768) to `text-embedding-3-small` (1536), the *column* has to be migrated:

```python
op.alter_column("document_chunks", "embedding", type_=Vector(1536))
```

And then you must **re-embed every chunk** — old vectors are dimensionally incompatible. Plan around this. Embedding choice is a load-bearing decision; switching is not trivial.

---

## 10. Command reference (the cheat sheet you'll come back to)

Live in `backend/` for the alembic and asyncpg commands; from the repo root for the docker exec commands.

### Schema inspection

```bash
# Tables in the DB
docker exec tutor-postgres psql -U tutor -d tutor -c "\dt"

# Table structure
docker exec tutor-postgres psql -U tutor -d tutor -c "\d+ documents"

# Indexes on a table
docker exec tutor-postgres psql -U tutor -d tutor -c "\di documents*"

# All constraints on a table
docker exec tutor-postgres psql -U tutor -d tutor -c \
  "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'documents'::regclass;"

# Extensions installed
docker exec tutor-postgres psql -U tutor -d tutor -c "\dx"

# Size of every table
docker exec tutor-postgres psql -U tutor -d tutor -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC;"
```

### Alembic

```bash
uv run alembic current               # what version is the DB at?
uv run alembic heads                 # what versions exist in code?
uv run alembic upgrade head          # apply pending
uv run alembic downgrade -1          # roll back one
uv run alembic stamp head            # mark DB at head without running SQL
uv run alembic history --verbose     # show the full chain
uv run alembic revision -m "msg"     # scaffold a new migration
```

### Performance debugging

```sql
-- 20 slowest queries by total time
SELECT query, calls, mean_exec_time, total_exec_time
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;

-- Most-accessed tables
SELECT relname, seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del
FROM pg_stat_user_tables ORDER BY (seq_scan + idx_scan) DESC LIMIT 20;

-- Bloat estimate
SELECT relname, n_dead_tup, n_dead_tup::float / NULLIF(n_live_tup, 0) AS bloat
FROM pg_stat_user_tables ORDER BY bloat DESC LIMIT 20;

-- Active connections + what they're doing
SELECT pid, usename, application_name, state, query_start, query
FROM pg_stat_activity WHERE state != 'idle' ORDER BY query_start;

-- Locks (find what's blocking what)
SELECT pid, relation::regclass, mode, granted FROM pg_locks WHERE NOT granted;

-- Vacuum manually if you must (don't in normal ops)
VACUUM (ANALYZE) documents;
```

### Vector queries

```sql
-- Nearest 5 chunks by cosine distance
SELECT id, content, embedding <=> '[0.1, 0.2, ...]'::vector AS distance
FROM document_chunks
ORDER BY embedding <=> '[0.1, 0.2, ...]'::vector
LIMIT 5;

-- Same, but tune the recall/latency knob for this query only
SET LOCAL hnsw.ef_search = 100;
SELECT ...

-- Verify the HNSW index is being used
EXPLAIN ANALYZE SELECT id FROM document_chunks ORDER BY embedding <=> '...' LIMIT 5;
```

### Backups

```bash
docker exec tutor-postgres pg_dump -U tutor tutor > backup-$(date +%F).sql
docker exec -i tutor-postgres psql -U tutor tutor < backup-2026-05-30.sql
```

---

## 11. What you'd add for real production

A checklist of what's *not* in this project but should be in a prod-grade deployment:

- [ ] **PgBouncer** in front of Postgres with transaction-mode pooling.
- [ ] **`pg_stat_statements`** enabled in `postgresql.conf`.
- [ ] **Slow query log** (`log_min_duration_statement = 200ms`).
- [ ] **Autovacuum tuning** (lower scale factors on hot tables).
- [ ] **`work_mem`, `shared_buffers`, `effective_cache_size`** tuned to the host's RAM.
- [ ] **CONCURRENTLY** on every index creation.
- [ ] **Migration safety** — split column-add + backfill + set-NOT-NULL into three migrations on big tables.
- [ ] **Streaming replica** with `synchronous_commit = remote_write`.
- [ ] **WAL archiving** to object storage; nightly basebackups; tested PITR.
- [ ] **Monitoring** — pgexporter to Prometheus, alerts on `xid_age`, dead tuple ratio, replication lag, slow queries.
- [ ] **Connection limits** — `max_connections` matched to PgBouncer pool sizing.
- [ ] **TLS** between app and Postgres (`sslmode=verify-full` on the client side).
- [ ] **A standby region** if you need DR.
- [ ] **Per-environment isolation** — separate DBs for prod / staging / CI, not shared schemas.

Each is a topic in itself. Knowing the list is enough to scope a production-readiness review.

---

## 12. Reading list

If you want to go deeper, in priority order:

- **The Postgres docs themselves**. Specifically chapters 11 (indexes), 14 (performance tips), 25 (backup), 27 (high availability). They're better than 90% of books on the subject.
- **`Designing Data-Intensive Applications`** (Kleppmann) — chapters 3 (storage engines) and 7 (transactions) give you the conceptual model.
- **`PostgreSQL Internals`** (Bruce Momjian's slide decks, free online) — how MVCC actually works.
- **`Use the Index, Luke`** (free site by Markus Winand) — best resource on B-tree indexes.
- **pgvector's README** — keep up with HNSW parameter recommendations as the project evolves.
