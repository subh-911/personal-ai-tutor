# Redis in the Personal AI Tutor — production-level depth

Same shape as the Postgres and Docker docs: each decision paired with the alternative we rejected, and concrete file:line references back into the codebase.

A useful framing up front: Redis is *not* a generic key-value store. It's a data-structure server with optional persistence. The first thing to internalise is "Redis gives you typed primitives — lists, hashes, sorted sets, streams — operating on an in-memory dataset". Everything else (caching, queueing, leaderboards, pub/sub) is a use case people built on top of those primitives.

This project uses Redis for two distinct things:
1. **Chat session memory** — sliding window of per-session messages with sliding TTL (since Phase 3).
2. **ARQ task queue** — async ingestion jobs (since Phase 10).

Both live in the same Redis instance, separated by logical DBs (`0` for sessions, `1` for ARQ).

---

## 1. Why Redis at all (and why not the alternatives)

The shortlist for "where does chat session memory live?":

| Option | Why we considered it | Why we didn't pick it |
|---|---|---|
| **In-process Python dict** | Trivial. Zero ops. | Dies on every API restart. Doesn't share across replicas. Memory unbounded. Not even a real option. |
| **Postgres** (we already run it) | Already operational. No new service. TTL via `DELETE WHERE expires_at < now()` cron. | Wrong shape: every chat turn is two writes; we'd hammer the DB write path with conversational state that doesn't need durability. No native TTL. Sliding window via app-level LTRIM equivalent costs a query+delete cycle. |
| **Memcached** | Faster than Redis on raw GET/SET. | Strings-only. No LIST type → we'd serialise the whole history per turn and rewrite. No persistence at all (which actually doesn't matter for chat, but kills the ARQ queue use case). |
| **DynamoDB / Cosmos** | Managed, scales. | Cloud-only. Latency 5–10× higher than local Redis. Costs per-request. Overkill for a single-user dev tool. |
| **Cassandra** | Survives anything. | Heavyweight. Hours to learn the data model right. Wrong workload — Cassandra wants high write throughput across many nodes; we have one node and bursts. |
| **Redis** | Right primitives (LIST, LTRIM, EXPIRE). Sub-ms latency. Trivially operates with one container. Has Pub/Sub and Streams for future fan-out. | Memory-bound (everything fits in RAM). Single-threaded command execution (a CPU bottleneck for absurd throughputs). Persistence is a second-class story compared to Postgres. |

We picked Redis. The decision is documented implicitly across:
- `ARCHITECTURE.md` §4: *"Redis holds ephemeral state ... per-session chat history (LIST per session, 30-day TTL, last 10 turn-pairs kept) on DB 0. Phase 10 added the ARQ ingestion job queue on DB 1."*
- `backend/app/services/session.py:23-27`: docstring spells out the LIST + LRANGE + LTRIM + EXPIRE design.

Then in Phase 10 we needed a task queue. The shortlist there:

| Option | Why we considered it | Why we didn't pick it |
|---|---|---|
| **Celery + RabbitMQ** | Industry standard. Mature. | Adds RabbitMQ as a *third* stateful service. Celery itself is sync-first; async support is bolt-on. |
| **Celery + Redis** | Reuses our Redis. | Still sync-first; impedance mismatch with our `redis.asyncio` codebase. Heavy. |
| **Dramatiq** | Lighter than Celery. | Same broker-agnostic story but you still pick one; doesn't solve the async-native problem. |
| **ARQ + Redis** | Async-native (matches the codebase). Reuses our Redis (no new service). Lightweight (one module, ~3k LOC). | Less feature-rich than Celery (no chained tasks, no rich result backend). Smaller community. Good enough for our use case. |

We picked ARQ. The `arq` keyspace lives on Redis DB 1, intentionally separated from chat state on DB 0.

**The principle**: pick the primitives that match your access patterns. Redis's LIST type is what chat session memory wants; Redis's sorted-set primitive is what ARQ uses internally for delayed-execution queues. Don't pick a tool that *can* do what you need; pick one whose *primitives* are what you need.

---

## 2. How this project uses Redis

### 2a. The shared client

```python
# backend/app/redis_client.py
from redis.asyncio import Redis, from_url

from app.config import settings

redis: Redis = from_url(settings.redis_url, decode_responses=True)


async def get_redis() -> Redis:
    return redis
```

Module-level `redis` is a singleton shared across the whole app. `from_url` internally creates a connection pool — calls don't open a fresh TCP connection per command.

**`decode_responses=True`** is load-bearing: it tells the client to decode replies as UTF-8 strings instead of bytes. Without it, every `lrange` returns `list[bytes]` and you'd be doing `[v.decode() for v in items]` everywhere. Pay the decoding cost at the client; never let bytes leak into the app layer.

`settings.redis_url` resolves to `redis://localhost:6380/0` from `.env`. The `/0` at the end selects logical DB 0. (Phase 10 added a computed `arq_redis_url` that points at the same host with DB 1 — see §8 below.)

### 2b. Chat session memory — `services/session.py`

The whole file is ~65 lines. Here's the key shape and the writes:

```python
# backend/app/services/session.py:17
SESSION_KEY_PREFIX = "chat:user:"

class SessionStore:
    def _key(self, user_id: str, session_id: UUID) -> str:
        return f"{SESSION_KEY_PREFIX}{user_id}:session:{session_id}:messages"

    async def get_history(self, user_id, session_id) -> list[ChatMessage]:
        items = await self.redis.lrange(self._key(user_id, session_id), 0, -1)
        return [ChatMessage.model_validate_json(item) for item in items]

    async def append_turn(self, user_id, session_id, *, user_msg, assistant_msg) -> None:
        key = self._key(user_id, session_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, user_msg.model_dump_json(), assistant_msg.model_dump_json())
            pipe.ltrim(key, -self.max_messages, -1)
            pipe.expire(key, self.ttl_seconds)
            await pipe.execute()
```

**Three commands, atomic via MULTI/EXEC**:
1. `RPUSH` — append both messages to the right end of the list.
2. `LTRIM key -max_messages -1` — keep only the last N messages, drop everything before.
3. `EXPIRE` — refresh the TTL to 30 days from now (sliding window).

`pipeline(transaction=True)` wraps these in `MULTI`/`EXEC` so they execute as one atomic step. If the connection dies between `RPUSH` and `LTRIM`, the whole transaction is discarded (more on Redis "transactions" in §6).

**The key shape encodes ownership in the key path itself**: `chat:user:{user_id}:session:{session_id}:messages`. A leaked session UUID alone doesn't unlock anything because the user_id must also be known. Phase 8 made `user_id` the verified Clerk user id, so the key is tied to authenticated identity.

**Legacy keys**: pre-Phase-6 keys had the shape `chat:session:{session_id}:messages` (no user). They're abandoned but don't need a migration — the 30-day TTL retires them naturally.

### 2c. ARQ task queue — `app/workers/ingest_worker.py`

ARQ writes its own keys; we just configure it:

```python
# backend/app/workers/ingest_worker.py
class WorkerSettings:
    functions = [embed_document]
    redis_settings = _redis_settings()
    max_tries = 1
    keep_result = 300
```

Behind the scenes, ARQ uses Redis primitives:

| ARQ concept | Redis primitive | Key shape |
|---|---|---|
| Queue (ready jobs) | Sorted Set | `arq:queue` |
| Job payload | String (msgpack) | `arq:job:{job_id}` |
| In-progress lock | String with TTL | `arq:in-progress:{job_id}` |
| Result | String with TTL | `arq:result:{job_id}` |
| Health check | String | `arq:queue:health-check` |

The sorted set's *score* is the scheduled execution time as a Unix timestamp. A worker `ZRANGEBYSCORE arq:queue -inf <now>` to atomically grab the next ready job. Delayed jobs (`enqueue_job(..., _defer_by=60)`) get a future score and are skipped until their time comes.

This is the canonical Redis queue pattern. Sidekiq, BullMQ, Resque all do variants of it.

You can verify the keyspace shape:

```bash
docker exec tutor-redis redis-cli -n 1 KEYS 'arq:*'
# arq:queue:health-check
# arq:result:abc123...
```

---

## 3. Data types deep dive — what's actually in your toolbox

Redis has ~10 first-class data types. You should know roughly what each one is, even if you only use two or three. The signal is in matching the type to the access pattern.

### 3a. STRING — the swiss army knife

The most overused type. People reach for STRING when they should reach for HASH or LIST.

**When STRING is right**:
- Atomic counters (`INCR`, `DECR`) — rate limits, click counters.
- Locks (`SET key value NX EX 30` — set if not exists, expire in 30s).
- Cached blobs (a JSON-serialised response with `SETEX key 60 <json>`).
- Single scalar values.

**When STRING is wrong**:
- Storing a Python dict by JSON-encoding the whole thing → use a HASH instead so you can update individual fields without serialising/deserialising the whole blob.
- Storing a list by JSON-encoding it → use a LIST so you can `RPUSH` / `LTRIM` without rewriting.

Atomic counter example (we don't use this in the project but it's the canonical idiom):

```python
# Increment per-user request count for rate limiting
count = await redis.incr(f"ratelimit:{user_id}:requests")
if count == 1:
    await redis.expire(f"ratelimit:{user_id}:requests", 60)  # 1-minute window
if count > 100:
    raise HTTPException(429, "rate limited")
```

The first `INCR` creates the key at 1; subsequent `INCR`s atomically bump. `EXPIRE` only on first creation gives you a tumbling window.

### 3b. LIST — what we use for chat history

Doubly-linked list internally. O(1) push/pop at either end, O(N) for indexed access in the middle.

**Operations we use**:
- `RPUSH key v1 v2` — append to right end.
- `LRANGE key 0 -1` — read whole list.
- `LTRIM key -20 -1` — keep only last 20 elements.

**Operations to know**:
- `LPUSH` / `LPOP` / `RPOP` — push/pop at either end.
- `BLPOP key 30` — blocking pop with 30s timeout (turns LIST into a simple queue).
- `LLEN` — current length.

**The chat history pattern in this project** is canonical for "sliding window of recent items":
- `RPUSH` to append.
- `LTRIM key -N -1` to cap at N most recent.
- `EXPIRE` to refresh the window.

All three in one pipeline so they happen atomically.

**When LIST is wrong**:
- If you need to look up by key — use HASH.
- If you need ordered-by-score retrieval — use SORTED SET.
- If you need durability stronger than periodic snapshots — use Postgres or a Stream.

### 3c. HASH — fields under one key

A HASH is a key with multiple fields, each holding a value. Think Python `dict`.

```python
await redis.hset("session:abc", mapping={"user_id": "alice", "turn_count": "0", "started_at": "..."})
await redis.hincrby("session:abc", "turn_count", 1)   # atomic increment of one field
turn_count = await redis.hget("session:abc", "turn_count")
```

**Why this is better than STRING-of-JSON for structured data**:
- `HINCRBY` lets you increment one field atomically without re-serialising.
- `HGET` reads one field — small payload.
- Memory-efficient under a certain size threshold (Redis uses `ziplist` encoding for small HASHes).

**We don't use HASH in this project**, but it's the right answer for things like:
- Per-user counters (`hash:user:{id}` with fields `logins`, `last_seen`, `tokens_used`).
- Session metadata distinct from the message list.

### 3d. SET — unique unordered

Membership tests in O(1). No duplicates.

```python
await redis.sadd("seen:doc_ids", "doc-1", "doc-2")
exists = await redis.sismember("seen:doc_ids", "doc-1")  # True
intersection = await redis.sinter("seen:doc_ids", "other:doc_ids")
```

**Right when**:
- "Have we processed this id before?" (idempotency keys with TTL).
- Tag-style relationships ("users in cohort A": `SADD cohort:a user-1 user-2`).
- Set algebra (intersection, union, difference).

We don't use SET here, but it's the right primitive for the "stuck-job reaper" idea in our deferred list — `SADD jobs:in-flight {job_id}` on dispatch, `SREM` on completion, periodic `SMEMBERS` to find rows that never completed.

### 3e. SORTED SET (ZSET) — ordered by score

A SET where each element also has a floating-point score; reads return elements in score order.

```python
# Leaderboard
await redis.zincrby("scores:weekly", 10, "alice")
top_10 = await redis.zrevrange("scores:weekly", 0, 9, withscores=True)

# Time-ordered queue
await redis.zadd("jobs", {job_id: time.time() + delay_seconds})
ready_jobs = await redis.zrangebyscore("jobs", "-inf", time.time(), num=10)
```

**This is ARQ's queue primitive**. The "due time" is the score; workers grab jobs whose score is in the past.

ZSETs underpin almost every "ordered by X" Redis feature: leaderboards, time-ordered queues, sliding window rate limiters, recently-active-users lists.

### 3f. STREAM — durable append-only log

Added in Redis 5.0. A STREAM is an append-only log of entries, each with an auto-generated id and a payload of field-value pairs. Consumers track their own position.

```python
await redis.xadd("events", {"type": "ingest_started", "doc_id": "abc"})
events = await redis.xread({"events": "$"}, block=5000)  # block 5s for new
```

**STREAM vs LIST**:
- LIST is a queue or stack; once popped, the message is gone.
- STREAM is a log; multiple consumers can read independently, replay from any point.
- STREAM has consumer groups (the `XGROUP`/`XREADGROUP` commands) for load-balancing across workers with acknowledgement and retry.

**Why we don't use STREAM**: ARQ uses ZSET (the older idiom) and works fine for our scale. If we were building a queue from scratch today, STREAM with consumer groups would be the right choice — it's the substrate Kafka-esque tools like Redpanda model their semantics after.

### 3g. The other types (HyperLogLog, Bitmap, Geo)

Brief mention so you know they exist:
- **HyperLogLog** (`PFADD`, `PFCOUNT`) — approximate cardinality counting. Tracks "number of unique items" in fixed memory (~12KB regardless of true count). Useful for "unique daily users".
- **Bitmap** (`SETBIT`, `BITCOUNT`) — a STRING with bit-level operations. Useful for "did user X do action Y today?" at scale.
- **Geo** (`GEOADD`, `GEORADIUS`) — sorted set with geo-distance metrics baked in.

You'll probably never use these. Knowing they exist saves you from building bad approximations.

---

## 4. Persistence — what survives a restart

Redis is in-memory, but optional persistence lets you survive restarts. There are three modes:

### 4a. RDB (snapshots)

Periodically forks the process and dumps memory to a binary file. Configurable schedule:

```
save 3600 1      # snapshot if at least 1 key changed in the last 3600s
save 300 100     # ...or at least 100 keys in 300s
save 60 10000    # ...or at least 10000 keys in 60s
```

**Pros**: tiny on-disk format, fast to load on restart. Easy to back up (just copy the file).

**Cons**: data loss window between snapshots — if you crash 59 minutes into the 60-minute interval, you lose 59 minutes of writes.

**Our compose enables RDB by default** (the official `redis:7-alpine` image runs with `save` enabled). Acceptable for chat history (we treat it as cache) but not for a payment system.

### 4b. AOF (append-only file)

Every write command is appended to a file. On restart, Redis replays the log.

```
appendonly yes
appendfsync everysec   # fsync every second; balance of durability and speed
```

**Pros**: configurable durability — `appendfsync always` gives you per-write durability (slow). `everysec` is the practical default (≤1s window of loss).

**Cons**: larger files than RDB. Slower restart on big datasets (replay all commands).

**Modern best practice** is to enable both: RDB for fast restart, AOF for the durability tail. Redis 7 added "RDB preamble in AOF" for fast restart with safe tail.

### 4c. No persistence

```
save ""             # disable RDB
appendonly no       # disable AOF
```

Pure cache mode. Restart loses everything. Sometimes the right answer — if you're caching results that can be recomputed, persistence is wasted I/O.

For a session store, "no persistence" is borderline acceptable because chat history is regeneratable from Postgres (well — *we* don't store it in Postgres, so for *us* it's not). For our project, the default RDB is good enough.

### 4d. The persistence config you'd run in production

```yaml
# docker-compose.yml addition for our redis service
redis:
  image: redis:7-alpine
  command: >
    redis-server
    --save 3600 1
    --save 300 100
    --appendonly yes
    --appendfsync everysec
    --maxmemory 1gb
    --maxmemory-policy allkeys-lru
  volumes:
    - redisdata:/data
```

We don't do this today; the default config is fine for dev. The `maxmemory` + eviction policy is the *one* thing you should add before going to prod (see §5 below).

---

## 5. Memory management — Redis's only real cliff

Redis is in-memory. When you run out, **writes start failing** (default behaviour, `maxmemory-policy noeviction`). This is the most common production Redis outage cause.

### 5a. `maxmemory` — set it, always

```
maxmemory 1gb
```

Caps the memory Redis will use. Without it, Redis grows until the host OOMs and the kernel kills it. With it, Redis enforces an eviction policy when it hits the cap.

### 5b. `maxmemory-policy` — what to do when full

| Policy | What it does | When right |
|---|---|---|
| `noeviction` (default) | Reject writes. Reads succeed. | Never. Default is wrong for almost every use. |
| `allkeys-lru` | Evict least-recently-used among all keys. | Cache use cases. |
| `allkeys-lfu` | Evict least-frequently-used. | Hot key sets where access frequency matters more than recency. |
| `volatile-lru` | LRU among keys with TTL set. Reject when no candidates. | Mixed durable + cache state. |
| `volatile-ttl` | Evict shortest-TTL first. | When TTL roughly reflects importance. |
| `allkeys-random` | Random eviction. | Rare; faster than LRU when you don't care. |

**For chat history with TTL**: `volatile-lru` is appropriate. Keys all have TTL; if memory pressure hits, the rarely-used sessions go first. The 30-day TTL is the upper bound; LRU catches the long tail.

**For ARQ jobs**: `noeviction` is correct — losing a queued job is a bug. ARQ's keys are short-lived (jobs complete in seconds or fail), so memory pressure shouldn't accumulate from the queue.

This creates a conflict if both live in the same Redis. In our project, both DBs share the *same* maxmemory and *same* policy. For real production, you'd either:
- Run two Redis instances (one for sessions with LRU, one for queue with noeviction).
- Accept that the queue might be evicted under extreme pressure and design retries on top.

### 5c. Monitoring memory

```bash
docker exec tutor-redis redis-cli INFO memory
# used_memory:1234567
# used_memory_peak:2345678
# used_memory_rss:3456789           ← Resident set size from OS
# mem_fragmentation_ratio:1.5       ← RSS / used_memory; should be ~1.0–1.5
```

**`mem_fragmentation_ratio > 1.5`** means Redis is holding more RAM than it strictly needs because malloc gave it fragmented chunks. Restart eventually fixes it; `MEMORY PURGE` (Redis ≥ 4) can sometimes help live.

**`used_memory_peak`** is the historical high. Useful for sizing.

### 5d. Per-key memory analysis

```bash
docker exec tutor-redis redis-cli --bigkeys
# (scans the DB, reports the biggest key of each type)

docker exec tutor-redis redis-cli MEMORY USAGE 'chat:user:user_alice:session:abc:messages'
# Returns bytes consumed by that one key
```

Finds outliers. If one user's chat history is 100 MB while everyone else's is 100 KB, something is wrong.

### 5e. The Big Key problem

A single key holding many MB is a Redis anti-pattern:
- It blocks the single command thread during read/write.
- It blocks replication (each write is a single replicated command).
- It blocks eviction (can't evict a slice of a key, only the whole thing).

**Our chat LISTs are bounded by `LTRIM` to 20 messages**, each a few KB at most → 80 KB max per session. Safe.

ARQ payloads carry parsed document text — up to a few MB per job. Those keys are short-lived (job runs, key deleted) so they don't accumulate.

The rule: cap your value sizes, or use a different store.

---

## 6. Pipelines, transactions, Lua scripts — atomicity in Redis

Redis is single-threaded for command execution. Two commands from two clients can't interleave at the Redis side. But your *client* can be interrupted between commands. Atomicity at the application level needs explicit primitives.

### 6a. Pipelines — batching for performance

```python
async with self.redis.pipeline() as pipe:
    pipe.rpush(key, msg1)
    pipe.ltrim(key, -20, -1)
    pipe.expire(key, 86400)
    await pipe.execute()
```

A pipeline batches multiple commands into one network round-trip. Each command is queued client-side, sent in one packet, responses come back in one packet.

**This is purely a performance optimization** — the commands are not atomic. Another client's commands can interleave between them on the server. Don't confuse "pipeline" with "transaction".

The latency win is significant for chatty workloads. Three commands without pipeline = 3 RTTs. With pipeline = 1 RTT. On a network with 1ms RTT, that's 3ms → 1ms — substantial when you're aiming for sub-10ms total operation latency.

### 6b. Transactions (`MULTI`/`EXEC`)

```python
async with self.redis.pipeline(transaction=True) as pipe:
    pipe.rpush(key, msg1, msg2)
    pipe.ltrim(key, -20, -1)
    pipe.expire(key, 86400)
    await pipe.execute()
```

`transaction=True` wraps the pipeline in `MULTI`/`EXEC`. Now the commands execute as one atomic unit at the server: no other client's commands can interleave.

This is what `services/session.py:50` uses. Critical because if the trim or expire failed without the push succeeding (or vice versa), the invariant "every session key has TTL" would break.

**The big surprise** if you're coming from SQL: **Redis transactions don't roll back on error**. If `RPUSH` succeeds but the subsequent `LTRIM` fails due to a typo (wrong key type), the `RPUSH` is *not* undone. Redis transactions are atomic-in-execution (commands run together) but not transactional-in-recovery (no rollback on partial failure).

The takeaway: validate your commands client-side before issuing them. Don't rely on the transaction to clean up.

### 6c. Optimistic locking with `WATCH`

```python
async with self.redis.pipeline(transaction=True) as pipe:
    while True:
        try:
            await pipe.watch(key)
            current = await pipe.get(key)
            new_value = compute_new(current)
            pipe.multi()
            pipe.set(key, new_value)
            await pipe.execute()
            break
        except WatchError:
            # Key changed between WATCH and EXEC; retry.
            continue
```

`WATCH key` tells Redis "if `key` is modified by anyone before my EXEC, abort the transaction". This gives you compare-and-swap semantics.

Useful for "update if value matches" patterns where multiple writers compete. We don't use it; our writes per session-key are single-threaded by the request structure.

### 6d. Lua scripts (`EVAL`) — the right primitive for complex atomicity

For any logic more involved than "a few commands together":

```python
INCREMENT_IF_LESS_THAN = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if current < limit then
    return redis.call('INCR', KEYS[1])
else
    return -1
end
"""

result = await redis.eval(INCREMENT_IF_LESS_THAN, 1, "counter:abc", "100")
```

Lua scripts run server-side as a single atomic operation. The entire script is one "command" from Redis's view; nothing interleaves.

**When to use Lua**:
- Multi-step logic with conditional branches (the example above).
- Rate limiters (the classic "sliding window log" rate limiter).
- Anything you'd write as "GET → check → SET" with race conditions.

**When *not* to use Lua**:
- Anything long-running. Lua scripts block the server's single command thread; a 200ms Lua script blocks every other client for 200ms.
- Anything with external state. Lua can't talk to other systems.

We don't use Lua in this project. We don't need to.

---

## 7. Pub/Sub and Streams — when you need fan-out

Two related but different primitives.

### 7a. Pub/Sub (the older idiom)

```python
# Subscriber
pubsub = redis.pubsub()
await pubsub.subscribe("events:ingest")
async for message in pubsub.listen():
    if message["type"] == "message":
        handle(message["data"])

# Publisher
await redis.publish("events:ingest", json.dumps({"doc_id": "abc", "status": "completed"}))
```

**Properties**:
- Fire-and-forget. If no subscriber is connected, the message vanishes.
- No persistence. No replay.
- Synchronous fan-out. Publisher blocks until all subscribers have received (kind of — sort of — Redis docs are fuzzy on this).

**Useful for**:
- Cache invalidation broadcasts ("user X's profile changed, drop your cached copy").
- Trivially-cheap notifications where loss is acceptable.

**Not useful for**:
- Anything that must be processed exactly once.
- Anything with multiple worker replicas needing to share load.

### 7b. Streams (the modern answer)

Already touched on in §3f. STREAMs give you:
- Persistence (entries stay until you `XDEL` or the stream is trimmed).
- Replay from any position.
- Consumer groups with per-consumer acknowledgement and reassignment of stuck messages.

**When you'd reach for Streams in this project**: future SSE fan-out across multiple API replicas. Today one replica owns each chat session via Redis LIST; if we scaled to N replicas, we'd want messages produced by replica A to also reach the WebSocket on replica B. Streams (or Pub/Sub) is the fan-out layer.

We don't need either yet — single API replica handles everything.

---

## 8. Logical databases (DB 0..15)

Redis supports up to 16 logical databases by default, selected via `SELECT N`. They're independent keyspaces sharing the same Redis instance — same memory pool, same persistence file, but separate key namespaces and `FLUSHDB`.

### 8a. The "don't use logical DBs" advice (and when it's wrong)

You'll find blog posts saying "never use multiple Redis DBs, just use key prefixes". The reasoning:
- Redis Cluster doesn't support multiple DBs (cluster mode forces DB 0).
- Some monitoring tools assume DB 0.
- Visual debugging tools (RedisInsight) handle prefixes as well as DBs.

**Where this advice is right**: if you're building for horizontal scale via Redis Cluster from day one, stick to DB 0 and use prefixes.

**Where it's wrong**: in this project, two completely different consumers (chat sessions, ARQ queue) wanted complete isolation. Logical DBs give us:
- `FLUSHDB` against DB 1 wipes the queue without touching sessions.
- `redis-cli -n 0 KEYS '*'` shows only session keys; `-n 1 KEYS '*'` shows only queue keys.
- The ARQ library and our SessionStore can use the same connection pool but operate on disjoint keyspaces with zero collision risk.

When we eventually scale to Cluster, we'll have to consolidate via key prefixes. For now, DB 0 + DB 1 is the cleaner separation.

### 8b. How we configure it

```bash
# .env
REDIS_URL=redis://localhost:6380/0          # sessions
```

```python
# backend/app/config.py
arq_redis_db: int = 1                        # ARQ

@property
def arq_redis_url(self) -> str:
    parsed = urlparse(self.redis_url)
    netloc = parsed.netloc or "localhost:6379"
    return f"{parsed.scheme or 'redis'}://{netloc}/{self.arq_redis_db}"
```

`arq_redis_url` parses `redis_url` to extract host:port, then rebuilds the DSN with DB 1. One env var (`REDIS_URL`) drives both consumers; they always point at the same instance and different DBs.

### 8c. Verifying the isolation

```bash
docker exec tutor-redis redis-cli -n 0 KEYS '*' | head -5
# chat:user:user_alice:session:...
# chat:user:user_demo:session:...

docker exec tutor-redis redis-cli -n 1 KEYS '*' | head -5
# arq:result:abc...
# arq:queue:health-check
```

Disjoint. Confirms the design.

---

## 9. Replication, Sentinel, Cluster — the HA path

A single Redis is a single point of failure. Production needs at least one replica; serious production needs automated failover or sharding.

### 9a. Master-replica replication

Asynchronous by default. The master streams writes to one or more replicas; replicas serve reads.

```
# In redis.conf on the replica
replicaof master-host 6379
```

**Properties**:
- **Async**: master commits + responds *before* replica confirms. Failure window: master crashes after acking but before replicating → that write is lost.
- **Read-only by default on replicas**: prevents accidental writes to the wrong node.
- **Replication lag**: usually <1s, can grow under load. Reading from a replica gives slightly stale data.

For read-heavy workloads (think analytics dashboards on top of session data), replicas spread load.

### 9b. Sentinel — automated failover

A separate set of "sentinel" processes monitor the master + replicas. If the master is down, they vote on a new master and reconfigure replicas.

**Properties**:
- **HA without sharding**: data still fits on one node; one node is "active" at a time.
- **Quorum-based**: typically 3 sentinels for 1 master + 2 replicas.
- **Clients use Sentinel-aware drivers** to discover the current master.

**This is where most "production Redis" deployments land**. Adequate for most use cases; introduces ops complexity but not data-model complexity.

### 9c. Redis Cluster — sharding

When one node can't hold the dataset, Cluster shards across N nodes by hashing the key into one of 16384 slots.

**Constraints introduced**:
- Multi-key operations only work if all keys hash to the same slot. Use **hash tags** (`{user:abc}:profile`, `{user:abc}:sessions`) to force co-location.
- No multi-DB. Cluster forces DB 0.
- More complex client logic (clients track the slot-to-node map and follow redirects).

For our project, Cluster is overkill. The dataset (chat history + queue) fits in low GB. Sentinel-with-replicas is the realistic upgrade path.

### 9d. Alternatives — Redis-compatible drop-ins

| Tool | Why it exists | Tradeoffs |
|---|---|---|
| **KeyDB** | Multi-threaded Redis fork. | Higher throughput on multi-core. Drop-in replacement. Less momentum than the original. |
| **Dragonfly** | Multi-threaded, modern implementation. | Same goal as KeyDB. Aggressive single-binary "10× faster" marketing. Maturity concern. |
| **Valkey** | Open-source Redis fork after Redis Labs changed the license. | Promoted as the OSS continuation. Same code today; diverging over time. |

We use vanilla Redis. If we hit single-thread CPU limits we'd evaluate KeyDB. We won't hit them.

---

## 10. Performance — what to watch

### 10a. Latency

Redis is single-threaded; long commands block everything. The `SLOWLOG` keeps the slowest commands.

```bash
docker exec tutor-redis redis-cli SLOWLOG GET 10
# Shows the 10 slowest commands recorded
```

Threshold for "slow" is configurable; default is 10ms. If anything is in here, find out why.

**The latency commands** (Redis ≥ 2.8):
```bash
docker exec tutor-redis redis-cli LATENCY DOCTOR
# Holistic latency analysis with recommendations
```

**Common latency culprits**:
- `KEYS *` in production. Scans the entire keyspace, blocking everything. **Use `SCAN` instead.**
- Large `MGET` / `MSET` of thousands of keys.
- Big values (multi-MB strings/lists).
- Lua scripts that do too much.
- Persistence: `BGSAVE` forks the process; fork is slow on large datasets. AOF rewrite has similar fork cost.

### 10b. The `KEYS` vs `SCAN` distinction

```python
# BAD — blocks Redis until complete
keys = await redis.keys("chat:user:*")

# GOOD — incremental, non-blocking
keys = []
async for key in redis.scan_iter(match="chat:user:*"):
    keys.append(key)
```

`KEYS` is O(N) over the whole keyspace, locks Redis until done. On a million-key DB, that's seconds of total blocking.

`SCAN` returns a cursor + batch; you iterate. Each batch is fast. No blocking.

We use `SCAN` in `backend/tests/conftest.py:111`:

```python
async for key in redis_client.scan_iter(match=pattern):
    await redis_client.delete(key)
```

**Rule**: never `KEYS` in production code. Tests get a pass because they target small datasets and need pattern matching for cleanup.

### 10c. Connection pooling

`redis.asyncio.from_url()` builds a pool under the hood. Default pool size is unbounded; in practice tracked by `redis-cli CLIENT LIST | wc -l`.

For tuning:

```python
redis: Redis = from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=20,
)
```

You almost never need to tune this for single-instance deployments. Becomes important when you have many app replicas and Redis hits the `maxclients` limit (default 10000).

### 10d. Persistence latency

`BGSAVE` and AOF rewrite both call `fork()`. On Linux, fork uses copy-on-write, so memory cost is initially minimal — but every write to a dirty page during the snapshot allocates a new page. On a write-heavy 10GB Redis, a snapshot might burn an additional 2-5GB transiently.

**Symptoms of fork issues**:
- Periodic latency spikes timed with snapshot cadence.
- Out-of-memory errors during snapshots (not during steady-state).

**Mitigations**:
- Allocate enough host RAM for 2× the dataset.
- Disable RDB if AOF is sufficient (one fork cost, not two).
- Schedule snapshots off-peak.

### 10e. `INFO` and `MONITOR`

```bash
docker exec tutor-redis redis-cli INFO          # everything
docker exec tutor-redis redis-cli INFO memory   # one section
docker exec tutor-redis redis-cli INFO stats    # cmd counts, hits/misses
```

`INFO` is your first diagnostic. Key sections:
- `memory` — what's in RAM.
- `persistence` — last save time, AOF rewrite status.
- `stats` — `keyspace_hits` / `keyspace_misses` (your effective hit rate).
- `clients` — connected client count.
- `replication` — master/replica status and lag.

```bash
docker exec tutor-redis redis-cli MONITOR
# (streams every command in real-time; KILLS performance under load)
```

`MONITOR` is a debugging tool only. Never leave it open against production.

---

## 11. ARQ deep dive — how Phase 10's queue actually works

Since this project uses ARQ as its task queue, worth understanding the mechanism.

### 11a. Enqueue path

```python
await arq_pool.enqueue_job("embed_document", document_id="...", text="...")
```

ARQ:
1. Generates a job id (`uuid4`).
2. Serializes args + kwargs with `pickle` (or JSON if configured).
3. `SET arq:job:{job_id} <serialized>` with a TTL.
4. `ZADD arq:queue {job_id} {execution_time}` — adds to the sorted set with score = when-to-run.

Now the job is durable in Redis until either a worker grabs it or the TTL expires.

### 11b. Dequeue path

A worker process polls (with blocking) the sorted set:

```
ZRANGEBYSCORE arq:queue -inf <now> LIMIT 0 1
```

Returns the oldest ready job. Worker then:
1. Atomically removes it from the queue (`ZREM`).
2. Sets `arq:in-progress:{job_id}` with a TTL (the heartbeat).
3. Reads `arq:job:{job_id}` for the payload.
4. Runs the function.
5. On success: writes result to `arq:result:{job_id}`, deletes in-progress.
6. On failure: writes error result, retries up to `max_tries` (which is `1` in our config).

### 11c. The locking pattern

`arq:in-progress:{job_id}` is a lease. While present (TTL not expired), no other worker picks up that job. If the worker crashes before completing, the TTL eventually expires and another worker can retry.

**Our `max_tries=1`** means a crash → no retry → row stuck in `processing` forever (until manual intervention or a future "stuck-job reaper" cleanup). Documented in `ARCHITECTURE.md §8` as a deferred item.

### 11d. Verifying queue state during dev

```bash
# Show all queued jobs (sorted by exec time)
docker exec tutor-redis redis-cli -n 1 ZRANGE arq:queue 0 -1 WITHSCORES

# Show a specific job's payload (if you have the id)
docker exec tutor-redis redis-cli -n 1 GET arq:job:abc123

# How many jobs in flight?
docker exec tutor-redis redis-cli -n 1 KEYS 'arq:in-progress:*'

# Total keys in DB 1 (queue health)
docker exec tutor-redis redis-cli -n 1 DBSIZE
```

---

## 12. Challenges we actually hit in this project

### 12a. The macOS Redis port collision

Already discussed in `docker.md §7a`. Summary: a native `redis-server` running on `127.0.0.1:6379` shadowed the container. Fix was mapping the container to host port `6380`.

**The Redis-specific lesson**: `docker exec tutor-redis redis-cli` always hits the container directly (talks via the docker socket). `redis-cli -h localhost -p 6380` hits the container via the host port. `redis-cli` with no args defaults to `127.0.0.1:6379` — which on macOS is the native daemon, *not* our container.

If you ever see "Redis is empty even though I just wrote to it":
1. Run `lsof -i :6379` and `lsof -i :6380` to see who's bound where.
2. Run `docker exec tutor-redis redis-cli DBSIZE` to check the container.
3. Run `redis-cli DBSIZE` (with no args) to check the native daemon.
4. They will be different.

### 12b. The Phase 6→8 key namespace migration

Phase 3 keys: `chat:session:{session_id}:messages` (no user concept).
Phase 6 keys: `chat:user:{user_id}:session:{session_id}:messages` (per-user namespacing).
Phase 8 just made `user_id` a Clerk-verified string rather than a UUID — same key shape.

**No migration was written**. The old `chat:session:*` keys are abandoned; the 30-day TTL retires them naturally. After 30 days from the Phase 6 cutover, no `chat:session:*` keys remain.

The test fixture defensively sweeps both prefixes:

```python
# backend/tests/conftest.py:109
patterns = (f"{SESSION_KEY_PREFIX}*", "chat:session:*")
for pattern in patterns:
    async for key in redis_client.scan_iter(match=pattern):
        await redis_client.delete(key)
```

The legacy pattern is still in the cleanup list. We could drop it once we're confident no environment still has those keys; it costs essentially nothing to keep.

**The lesson**: TTL is your migration tool. If you can wait for natural expiry, you don't need to write data migrations for cache-like state.

### 12c. The `_StubArqPool` test fixture

ARQ's pool talks to a real Redis. Test isolation would require either spinning up a real Redis-per-test or mocking the pool.

We chose mocking. In `backend/tests/conftest.py`:

```python
class _StubArqPool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_job(self, function: str, *args, **kwargs):
        self.calls.append({"function": function, "args": args, "kwargs": kwargs})
        return None
```

Autouse fixture installs this stub in place of `get_arq_pool_dep`. Route tests can assert "this request enqueued the right job" without needing a real worker.

The worker function itself (`embed_document`) is tested separately by calling it directly (`test_workers.py`), bypassing the queue entirely.

**The lesson**: don't try to test an entire async queue end-to-end in unit tests. Test the *enqueue contract* on one side and the *job logic* on the other side. The queue itself is library code; trust it.

### 12d. The `decode_responses=True` default

Without it, every Redis read returns `bytes`. We forgot to set this once during Phase 3 development; spent an hour debugging "why is `json.loads(history[0])` failing with TypeError". Fix is one constructor arg:

```python
redis: Redis = from_url(settings.redis_url, decode_responses=True)
```

**The lesson**: pay the encoding cost at the client edge once, don't smear `.decode()` calls everywhere.

---

## 13. Production gap — what's missing

What we don't have but a prod-grade Redis deployment would:

- [ ] **`maxmemory` + eviction policy**. The single most important config.
- [ ] **AOF enabled** with `appendfsync everysec` for the queue's durability tail.
- [ ] **Master + replica + Sentinel** for HA.
- [ ] **TLS** (`rediss://`) on the wire.
- [ ] **AUTH** (Redis 6+ supports ACLs with per-user permissions; older versions use a single password).
- [ ] **Monitoring**: redis-exporter to Prometheus, dashboards on memory, ops/sec, hit rate, replica lag.
- [ ] **Backup automation**: ship RDB to S3 nightly + AOF tail continuously.
- [ ] **Slowlog threshold tuning** + alerts on any entries.
- [ ] **Client-side circuit breaker** so a hung Redis doesn't propagate latency into every request handler.
- [ ] **Network policies** to ensure only the app and worker can reach Redis.
- [ ] **Separation of cache and queue** into two Redis instances (different eviction policies).
- [ ] **Connection limits** matched to client pool sizing × replicas.

---

## 14. Command reference (the cheat sheet)

### Lifecycle / inspection

```bash
# Hit the container directly (always works)
docker exec -it tutor-redis redis-cli

# Or via the mapped host port
redis-cli -h localhost -p 6380

# Switch DB (after connecting)
> SELECT 0           # sessions
> SELECT 1           # ARQ queue

# Or via the CLI flag
redis-cli -n 1 INFO memory

# Server info
> INFO              # all sections
> INFO memory       # one section
> CLIENT LIST       # connected clients
> DBSIZE            # number of keys in current DB
> CONFIG GET maxmemory
> CONFIG SET maxmemory 1gb     # live config change (not persisted to redis.conf)
```

### Key inspection

```bash
> TYPE chat:user:alice:session:abc:messages       # list
> TTL  chat:user:alice:session:abc:messages       # seconds until expiry, -1 = no TTL, -2 = doesn't exist
> PTTL key                                         # same but in milliseconds
> EXPIRE key 3600                                  # set TTL
> PERSIST key                                      # remove TTL
> EXISTS key1 key2 key3                            # count of keys that exist
> OBJECT ENCODING key                              # internal encoding (ziplist, hashtable, etc.)
> DEBUG OBJECT key                                 # full internals
```

### Data type basics

```bash
# String
> SET k v EX 60        # SET with TTL
> GET k
> INCR counter
> INCRBY counter 5

# List
> RPUSH list a b c
> LRANGE list 0 -1
> LTRIM list -2 -1     # keep last 2
> LLEN list
> LPOP list

# Hash
> HSET h field value
> HGET h field
> HGETALL h
> HINCRBY h counter 1

# Set
> SADD s a b c
> SMEMBERS s
> SISMEMBER s a
> SCARD s              # count

# Sorted set
> ZADD z 1 a 2 b 3 c
> ZRANGE z 0 -1 WITHSCORES
> ZRANGEBYSCORE z 1 2
> ZRANGEREV z 0 0      # max score

# Stream
> XADD events * type ingest_started doc_id abc
> XRANGE events - +
> XLEN events
```

### Scanning (non-blocking)

```bash
# Scan keys
> SCAN 0 MATCH 'chat:*' COUNT 100
# (returns next cursor + batch; iterate until cursor=0)

# Scan within types
> HSCAN hash 0
> SSCAN set 0
> ZSCAN zset 0
```

### Debugging

```bash
> SLOWLOG GET 10             # 10 slowest commands
> SLOWLOG RESET
> LATENCY DOCTOR             # latency analysis
> LATENCY HISTORY event-name

> MONITOR                    # stream all commands; HUGE perf hit, use briefly only

> CLIENT LIST                # who's connected, what they're doing
> CLIENT KILL ADDR 1.2.3.4:5678   # disconnect a specific client
```

### Persistence + backup

```bash
> BGSAVE                     # async snapshot in background
> LASTSAVE                   # unix timestamp of last successful save
> BGREWRITEAOF               # rewrite AOF (compact log)

# Backup the RDB file
docker cp tutor-redis:/data/dump.rdb ./redis-backup-$(date +%F).rdb

# Restore (replace the running file; requires restart)
docker compose stop redis
docker cp ./redis-backup-2026-05-30.rdb tutor-redis:/data/dump.rdb
docker compose start redis
```

### Cleanup

```bash
> FLUSHDB                    # delete every key in current DB (PROD: never)
> FLUSHALL                   # nuke all DBs (PROD: definitely never)
> DEL key1 key2              # delete specific keys
```

### Memory analysis

```bash
> MEMORY USAGE key           # bytes for one key
> MEMORY STATS               # internal allocator stats
> MEMORY DOCTOR              # holistic check
redis-cli --bigkeys          # scan for largest key per type
redis-cli --memkeys          # similar, deeper
```

### Pub/Sub

```bash
> SUBSCRIBE channel
> PSUBSCRIBE pattern.*
> PUBLISH channel message
> PUBSUB CHANNELS            # active channels
> PUBSUB NUMSUB              # subscriber counts
```

### Project-specific peeks

```bash
# All session keys for one user
docker exec tutor-redis redis-cli -n 0 KEYS 'chat:user:user_alice:*'

# Inspect one session's message log
docker exec tutor-redis redis-cli -n 0 LRANGE 'chat:user:user_alice:session:...:messages' 0 -1

# Check what ARQ has queued
docker exec tutor-redis redis-cli -n 1 ZRANGE arq:queue 0 -1 WITHSCORES

# In-flight job count
docker exec tutor-redis redis-cli -n 1 EVAL "return #redis.call('KEYS', 'arq:in-progress:*')" 0
```

---

## 15. Reading list

In rough priority order:

- **The Redis docs themselves**. Every command page has a complexity annotation (O(1), O(log N), O(N)) — internalise these; they're how you predict performance.
- **The Redis source code's `redis.conf`** — every comment in the default config file is worth reading once. Better than any third-party guide.
- **`Redis in Action` (Carlson)** — older but the patterns chapters (rate limiters, locks, leaderboards) age well.
- **Redis University** (university.redis.com) — free, official, surprisingly good.
- **`Designing Data-Intensive Applications` (Kleppmann)**, chapter 5 (replication) and 11 (stream processing) — the conceptual frame for Sentinel and Streams.
- **The ARQ source** (`github.com/python-arq/arq`) — small, readable, demystifies how queue libraries are built on Redis primitives.
- **Antirez's blog archive** — the original Redis author's writing. Gold for understanding the design choices.
