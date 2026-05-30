# Docker in the Personal AI Tutor — production-level depth

Like the Postgres doc: this isn't an intro. Each section pairs the decision we made with the alternative we rejected and the reasoning. Read top-to-bottom; later sections assume the earlier ones.

A useful framing up front: in this project we use Docker for **stateful services only** (Postgres, Redis). The Python API + worker run on the host, not in containers. That choice is deliberate and is the first thing the doc explains.

---

## 1. Why Docker at all (and why only for stateful services here)

The job containers do well is *isolating and reproducing environments*. Once you know that, the "should I containerize X?" question becomes "do I want X's environment isolated and reproducible?"

The realistic alternatives we weighed:

| Alternative | Why people pick it | Why we didn't |
|---|---|---|
| **Install Postgres + Redis natively via Homebrew** | Native performance; no Docker overhead. | One-host-only. Version drift between developer machines. `pg_upgrade` is painful. Two project-specific extensions (`pgvector`) means everyone runs a custom build. Reset state = `brew uninstall + brew install` (slow). |
| **Vagrant / VirtualBox VMs** | Full OS isolation. | Heavyweight (gigabytes per VM). Slow to boot. Mostly abandoned tech in 2026. |
| **Run everything in containers** | Ultimate isolation; deployment is one `docker compose up`. | The Python app changes every five minutes during dev. Rebuilding a container on each change kills iteration speed. The hot-reload story across Docker volumes is fragile, especially with sentence-transformers' file watcher. |
| **Cloud dev environment (Codespaces, etc.)** | Zero local install. | Network round-trip on every command. Costs money. Doesn't model the prod environment if prod isn't using Codespaces. |
| **Docker for stateful services, native for the app** (what we do) | Postgres+Redis are reproducible across all devs; the app runs natively for fast iteration. | Two execution environments to reason about. Newcomer needs to install `uv` and `node` *and* run `docker compose up`. |

We picked the hybrid. The reasoning is documented implicitly in `docker-compose.yml`:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    ...
  redis:
    image: redis:7-alpine
    ...
volumes:
  pgdata:
  redisdata:
```

Two services, both stateful. The app isn't there because containerizing a Python process whose source changes on every keystroke is a tax we don't need to pay yet.

**The principle**: containerize what changes the *environment*, not what changes the *code*. Postgres and Redis are environments. Python source is code.

---

## 2. The compose file, line by line

`docker-compose.yml` is the only Docker artifact we have. Let's walk through every line.

### 2a. The `services` block

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    container_name: tutor-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: tutor
      POSTGRES_PASSWORD: tutor
      POSTGRES_DB: tutor
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tutor -d tutor"]
      interval: 5s
      timeout: 5s
      retries: 10
```

**`image: pgvector/pgvector:pg16`** — pinning is load-bearing.
- *Not* `:latest`. `:latest` is a moving target; tomorrow's pull might be pg17 and break our migrations.
- *Not* `pgvector/pgvector` (no tag — defaults to `:latest`).
- `pg16` is a major-version tag. We're saying "any 16.x is fine" — minor releases are bug fixes within the major.
- For maximal reproducibility you'd pin to a digest: `pgvector/pgvector@sha256:abc123...`. We don't because the convenience of `pg16` is worth the risk; if a minor release ever breaks us, we'd switch to digest pinning.

**`container_name: tutor-postgres`** — fixes the name so we can `docker exec tutor-postgres ...` without looking up the auto-generated name. The cost: only one instance can run at a time. Fine for dev; you wouldn't do this in production.

**`restart: unless-stopped`** — restart on host reboot or container crash, *unless* I manually stopped it. There are four options:
| Policy | Restarts on crash | Restarts on host boot | Survives manual stop |
|---|---|---|---|
| `no` | ❌ | ❌ | n/a |
| `always` | ✅ | ✅ | restarts even if you stopped it |
| `unless-stopped` | ✅ | ✅ | respects manual stop |
| `on-failure` | ✅ (with exit code != 0) | ❌ | n/a |

We want `unless-stopped` for dev so we don't have to start Postgres after every laptop reboot.

**`environment`** — the official postgres image's init script reads `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` and creates the role + DB on first boot. Plaintext password in compose is fine for dev; in prod you'd use Docker secrets or pull from a vault.

**`ports: ["5432:5432"]`** — `host:container`. Exposes the DB on the host's port 5432. The app on the host connects via `localhost:5432`.
- The `5432:5432` symmetry is a convention. If your host already runs Postgres, change the host side: `"15432:5432"` exposes the container on host port 15432, leaving the native daemon on 5432.
- Removing this line entirely would still let other *containers* talk to Postgres via the compose network (under the service name `postgres`), but the host couldn't reach it.

### 2b. Volumes — where state survives

```yaml
volumes:
  pgdata:
  redisdata:
```

```yaml
postgres:
  volumes:
    - pgdata:/var/lib/postgresql/data
    - ./postgres/init:/docker-entrypoint-initdb.d:ro
```

Two volume mounts on the postgres service, two *kinds* of mount.

**Named volume (`pgdata:/var/lib/postgresql/data`)**: Docker-managed storage. Lives in `/var/lib/docker/volumes/<project>_pgdata/` on the host (or in the Docker Desktop VM on macOS). Survives `docker compose down`; dies on `docker compose down -v`. Fast IO because it's Docker-native.

**Bind mount (`./postgres/init:/docker-entrypoint-initdb.d:ro`)**: a host directory mapped into the container. `:ro` makes it read-only inside the container. Used here to inject our `01-extensions.sql` so Postgres runs it on first boot.

**The difference matters**:
- Named volumes are for *Docker-managed* state. Use them for DB data, app caches, anything you don't want to look at directly.
- Bind mounts are for *human-managed* files getting injected into the container. Use them for init scripts, config files, app source code in dev containers.

**macOS / Windows performance trap**: bind mounts go through a translation layer (virtiofs on modern Docker Desktop, gRPC-FUSE on older versions). Heavy IO (e.g. database data files) on a bind mount is *substantially* slower than a named volume. Always use named volumes for database storage.

### 2c. The init scripts pattern

```yaml
volumes:
  - ./postgres/init:/docker-entrypoint-initdb.d:ro
```

Plus this file on the host:

```sql
-- postgres/init/01-extensions.sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

How this works: the official `postgres` (and derivatives like `pgvector/pgvector`) image's entrypoint script runs every `.sh` / `.sql` / `.sql.gz` file in `/docker-entrypoint-initdb.d/` **once on first boot, when the data directory is empty**.

It does not re-run on subsequent boots. This is a feature, not a bug — Postgres expects the data dir to be initialised exactly once.

**The implications**:
- Adding a new init script doesn't apply to an existing data volume. You'd have to `docker compose down -v` to nuke the volume and re-run.
- This is why schema lives in Alembic, not init scripts. Init scripts are for *extensions and roles* that need to exist before Alembic can run.

Order matters because the script directory is sorted lexicographically. The `01-` prefix is a hint to future-you that order is meaningful (even if there's only one file today, the convention scales).

### 2d. Healthchecks — the underused power feature

```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U tutor -d tutor"]
  interval: 5s
  timeout: 5s
  retries: 10
```

A container's `HEALTHCHECK` is a small command Docker runs periodically inside the container. Output of `docker ps` shows `(healthy)` or `(unhealthy)` based on the result.

**Three uses**:

1. **Visual confirmation**: `docker ps` tells you at a glance if Postgres is actually ready (not just running but ready to accept connections).
2. **Dependency ordering**: another service can wait for this one to be `healthy` before starting.
3. **Restart trigger**: with `restart: unless-stopped` + healthcheck, you get auto-restart on health failure (somewhat — Docker doesn't restart on unhealthy by default; you need `autoheal` or similar).

**Healthcheck design rules**:
- Test what the service exposes, not what's inside. For Postgres: `pg_isready` (cheap, listens on the socket). Don't `SELECT 1` — that depends on the connection working.
- Keep it cheap. Healthcheck runs every `interval` seconds (5s here). A heavy query in healthcheck = constant background load.
- `retries: 10` × `interval: 5s` = container is marked `unhealthy` after ~50 seconds of failing checks. Tune based on legitimate startup time.

**For Redis we use `redis-cli ping`** — same principle, equally cheap.

### 2e. Dependency ordering (what we *don't* use here but should know)

We don't have a service that depends on another. If we did:

```yaml
services:
  api:
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    ...
```

`condition: service_started` (default) only waits for the container to *exist*. `condition: service_healthy` waits for the healthcheck to pass. **Always use `service_healthy`** for dependencies — `service_started` is almost never what you want.

This is exactly the pattern we'd use if/when we containerize the app.

### 2f. The redis service — same shape, fewer surprises

```yaml
redis:
  image: redis:7-alpine
  container_name: tutor-redis
  restart: unless-stopped
  ports:
    - "6380:6379"
  volumes:
    - redisdata:/data
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 5s
    timeout: 5s
    retries: 10
```

Three things to notice:

**`6380:6379` — the host:container port asymmetry**: container listens on Redis's default 6379, exposed to the host on 6380. We did this because macOS users often have a native `redis-server` running on 6379 from homebrew, which silently *shadows* the Docker container. After hours debugging "why is my Redis empty?" the resolution was to map to a non-default host port. Comment in `docker-compose.yml:27-29` documents this:

```yaml
ports:
  # Host port 6380 to avoid collision with a native macOS redis-server
  # (which historically shadowed this container on 6379 during phase 6
  # debugging — see end-to-end-flow.md §operational notes).
  - "6380:6379"
```

**The corollary**: every connection string in this project uses `redis://localhost:6380/...`. If you forget this and use 6379, you'll connect to whatever native daemon happens to be running. Subtle bug.

**`image: redis:7-alpine`** — alpine variants are tiny (~30 MB vs ~140 MB for the default). They use musl libc instead of glibc. For Redis specifically there are no compatibility issues; for Python apps, alpine breaks pre-built wheels and forces source compiles (you'd want `python:3.12-slim`, not `python:3.12-alpine`).

**Persistence is enabled by mounting `/data`**. Redis's default config saves an RDB snapshot every few minutes. If you wanted append-only (every write durably logged), you'd start Redis with `--appendonly yes`. RDB-only is fine for chat history (we treat it as cache anyway).

### 2g. Default network — the invisible glue

What we don't explicitly declare: a network. Docker Compose auto-creates a default bridge network named `<project>_default` and attaches every service to it.

Inside that network, services reach each other by service name:

```python
# In the running app (when containerized):
DATABASE_URL = "postgresql+asyncpg://tutor:tutor@postgres:5432/tutor"
REDIS_URL = "redis://redis:6379/0"
```

But our app runs on the *host*, not in the network. So it uses `localhost` with the exposed ports:

```bash
# .env
DATABASE_URL=postgresql+asyncpg://tutor:tutor@localhost:5432/tutor
REDIS_URL=redis://localhost:6380/0
```

If we ever containerize the app, the `.env` for the containerized version would look like the first block. Two truths coexist.

---

## 3. Container design — what we'd do when we *do* containerize the app

This is the section to consult when adding a backend `Dockerfile`. None exists today, but the conventions are well-established and you should follow them.

### 3a. Multi-stage builds

A naive Dockerfile bakes the build toolchain into the final image:

```dockerfile
# BAD: builder + runtime in one image
FROM python:3.12
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync
COPY . .
CMD ["uvicorn", "app.main:app"]
```

Result: image is ~2 GB because it carries `gcc`, `git`, and 1.5 GB of build-time dependencies.

**Multi-stage** separates build and runtime:

```dockerfile
# Builder stage: has the toolchain
FROM python:3.12-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

# Runtime stage: only what's needed to run
FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/ ./app/
ENV PATH="/app/.venv/bin:$PATH"
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Result: image is ~500 MB. Same functionality. The build toolchain stays in the discarded builder stage.

### 3b. Layer ordering for caching

Docker caches each instruction's output. If a layer's inputs don't change, the cache is reused. **Order layers from least-likely-to-change to most-likely-to-change**:

```dockerfile
# 1. Base image (changes monthly)
FROM python:3.12-slim

# 2. System deps (change rarely)
RUN apt-get update && apt-get install -y ...

# 3. Python deps (change with pyproject.toml updates)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 4. Application source (changes every commit)
COPY app/ ./app/
```

A typo fix in app code re-runs only step 4; deps don't reinstall. Reversing the order — `COPY . .` *then* `RUN uv sync` — invalidates the dep install on every code change. Multi-minute build vs sub-second build.

### 3c. The `.dockerignore` file

Same idea as `.gitignore`. Without it, `COPY . .` includes the `.git/` directory, `node_modules/`, `.venv/`, and any local cruft. Result: bigger context send + cache busts on irrelevant changes.

A minimal one for this project:

```
.git
.venv
node_modules
__pycache__
*.pyc
.pytest_cache
.env
.env.local
```

### 3d. Non-root user

```dockerfile
USER 65532:65532
```

Containers run as `root` by default. If your container is compromised, the attacker has root inside the namespace — and with bad config, can escape. Always declare a non-root user for the runtime stage.

`65532` is the convention for "nobody-ish" used by distroless. You can also create a real user:

```dockerfile
RUN useradd -m -u 1000 -s /bin/bash appuser
USER appuser
```

### 3e. Image base choices

| Base | Size | Pros | Cons |
|---|---|---|---|
| `python:3.12` | ~1 GB | Full Debian. Everything works. | Big. Slow pulls. Includes build tools you don't need at runtime. |
| `python:3.12-slim` | ~150 MB | Same Debian, minus build tools. | You'll need to apt-install anything you depend on. |
| `python:3.12-alpine` | ~50 MB | Tiny. | musl libc — pre-built wheels for numpy/scipy don't work; long source compiles. |
| `gcr.io/distroless/python3` | ~80 MB | No shell, no package manager, can't even `docker exec sh`. Minimal attack surface. | No debugging tools inside the container. Painful when something goes wrong. |

**For this project's API**: `python:3.12-slim` is the right balance — small enough, debuggable, no wheel-compile headaches for sentence-transformers.

**For something security-critical**: `distroless` is the right answer; debug from the outside.

### 3f. The worker container vs the API container

In Phase 10 we describe the worker as a separate process. If/when we containerize:

```yaml
services:
  api:
    image: tutor-api:latest
    build: ./backend
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ports:
      - "8000:8000"
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }

  worker:
    image: tutor-api:latest   # same image, different command
    build: ./backend
    command: ["arq", "app.workers.ingest_worker.WorkerSettings"]
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
```

Same image, different command. The worker loads the embedding model; the API doesn't. Both share the Python deps so building once is enough.

The cost: when the worker first runs, it downloads sentence-transformers (~420 MB). That bakes into the runtime, not the image. To avoid the cold-start, you'd pre-bake the model into the image:

```dockerfile
# In the builder stage, after uv sync
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"
```

This downloads the model at build time into the Hugging Face cache directory, which then gets copied into the runtime stage. Image grows by ~420 MB but worker boot becomes ~instant.

### 3g. Resource limits

```yaml
services:
  postgres:
    deploy:
      resources:
        limits:
          memory: 1G
          cpus: '1.0'
        reservations:
          memory: 512M
```

**Limits** are hard caps. Postgres can't allocate beyond. Important for predictability.

**Reservations** are guarantees the scheduler honours when deciding placement. Mostly meaningful in Swarm / Kubernetes.

We don't set these in our compose file because dev laptops don't need them. In production: always set limits. Without them, a leaking process can starve every other container on the host.

### 3h. Logging

By default containers log to stdout/stderr; Docker captures these into JSON files in `/var/lib/docker/containers/<id>/`. They grow unboundedly unless you configure rotation:

```yaml
services:
  postgres:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

This caps the log directory at 30 MB per container. **Always set this in production** unless you're shipping logs to an external system.

For shipping to an external system:

```yaml
logging:
  driver: "loki"
  options:
    loki-url: "http://loki:3100/loki/api/v1/push"
```

(Other drivers: `gelf`, `fluentd`, `syslog`, `awslogs`.)

We don't ship logs anywhere — dev only. `docker logs -f tutor-postgres` reads from the JSON file.

---

## 4. Performance considerations

### 4a. Volume IO performance on macOS

Docker Desktop on macOS runs containers in a Linux VM. The Linux VM doesn't have direct access to your macOS filesystem; bind mounts go through a translation layer (`virtiofs` in modern Docker Desktop, `gRPC-FUSE` on older versions). This is *slow* for I/O-heavy workloads.

**Concrete impact**:
- Postgres data on a bind mount: 3–5× slower than the same data on a named volume.
- Node `npm install` into a bind-mounted `node_modules`: 5–10× slower than into a container-internal directory.

**Rules of thumb on macOS**:
- Database data: named volume, never bind mount.
- Compiled language `target/` directories: named volume or container-internal.
- Editing source code: bind mount is fine (and necessary for hot-reload), but be aware that frequent watches can saturate the syncing layer.

In our compose, `pgdata` and `redisdata` are named volumes for exactly this reason; `./postgres/init` is a bind mount only because it's read-only and accessed exactly once at first-boot.

### 4b. Image pull times — registry caching

`docker pull pgvector/pgvector:pg16` is a multi-hundred-MB transfer. On first run, on a fresh machine, this takes minutes.

**Speedups**:
- Pull once, then `docker compose up` is local — Docker caches images by digest.
- For CI: use a registry mirror (Docker Hub has rate limits; GHCR is more permissive).
- For multi-arch builds (x86_64 + arm64), publish both — otherwise users on M-series Macs pay an emulation penalty.

### 4c. CPU emulation on arm64 Macs

Older container images are x86_64-only. Running them on M1/M2/M3/M4 Macs forces QEMU emulation — slow and sometimes broken.

`docker manifest inspect pgvector/pgvector:pg16` shows the supported platforms. `pgvector/pgvector` does publish arm64 images, so we're fine. If you ever see "the requested image's platform (linux/amd64) does not match the detected host platform (linux/arm64/v8)", that's emulation.

To force a platform on a per-run basis:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    platform: linux/amd64    # force x86_64
```

Mostly only useful for testing prod-architecture parity from a Mac.

---

## 5. Networking deep dive

### 5a. The default bridge network

When you run `docker compose up`, Compose creates a user-defined bridge network (typically named `<project>_default`). Every service joins this network and can resolve every other service by name.

```bash
docker network ls
# NAME                          DRIVER    SCOPE
# personal-ai-tutor_default     bridge    local

docker network inspect personal-ai-tutor_default
# (lists services + their IPs)
```

**Why this matters**: from one container, `redis://redis:6379/0` resolves Redis by service name. The Docker DNS sidecar (`127.0.0.11`) handles it. No need to know IPs.

From the host, you can't use `redis://redis:6379` because `redis` isn't a hostname your host knows about. That's why we expose ports — to give the host a path in.

### 5b. The `host` network mode (rarely the answer)

```yaml
network_mode: host
```

Container shares the host's network namespace. No port mapping needed. *Linux only* (silently ignored on macOS/Windows). The fastest networking option but loses the isolation property of bridge networking.

We don't use it. Almost no project should.

### 5c. Multi-host networking

Docker Compose is single-host. The moment you need services on multiple hosts, you're picking between:

- **Docker Swarm** (Docker's built-in clustering — out of fashion but still works)
- **Kubernetes** (industry default)
- **Nomad / ECS / Cloud Run / Fly.io** (various managed orchestrators)

Compose has no multi-host story. Don't try to make it one.

---

## 6. Production-grade gap — what we *don't* have

Single-host Docker Compose is excellent for dev. For production, you need most of the items below.

### 6a. Orchestration

| Concern | Compose | Kubernetes |
|---|---|---|
| Multi-host | ❌ | ✅ |
| Rolling deploys | manual | declarative |
| Self-healing | restart policies | full reconciliation loop |
| Service discovery | network DNS | service abstraction + DNS |
| Secrets | env vars | encrypted at rest, mounted at runtime |
| Config | env vars + bind-mounted files | ConfigMaps + Secrets |
| Observability | DIY | OpenTelemetry ecosystem |

When you outgrow Compose, the typical migration target is Kubernetes (often via a managed provider — EKS, GKE, AKS) or one of the "Kubernetes-but-easier" platforms (Fly.io, Railway, Render).

### 6b. Image registry + signing

Local images live in your Docker daemon. To deploy them on a different host, you need a registry.

- **Docker Hub** — easy but rate-limited; public by default.
- **GitHub Container Registry (`ghcr.io`)** — free for public, integrated with GitHub Actions.
- **AWS ECR / Google AR / Azure ACR** — cloud-native, IAM-integrated.

**Image signing** (cosign + a key pair) lets you verify at deploy time that the image you're running is the one you published. In a secure pipeline:
1. CI builds image
2. CI signs image with cosign
3. Deployer verifies signature before pulling
4. Refuses to deploy unsigned/wrong-signed images

We don't do any of this. For prod you'd need to.

### 6c. Image scanning

Static analysis on images to find known CVEs in your base layer / installed packages. Tools:

- `docker scout cves <image>` — Docker Desktop built-in.
- `trivy image <image>` — open-source, widely deployed.
- `grype <image>` — Anchore's.

Run on every build in CI; block deploy if HIGH/CRITICAL CVEs are present without an exception.

### 6d. Secrets management

```yaml
postgres:
  environment:
    POSTGRES_PASSWORD: tutor    # ← plaintext, in git
```

Plaintext secrets in compose are fine for dev where the password is `tutor`. Real production needs:

- **Docker Compose** has a `secrets:` block that reads from external files, but it's a half-measure.
- **Kubernetes**: `Secret` objects, ideally with external-secrets operator pulling from Vault / AWS Secrets Manager.
- **Vault / AWS Secrets Manager / GCP Secret Manager**: pull secrets at runtime, never bake them into images or commit to git.

The `.env` file is the cleanest dev compromise — it's `.gitignore`'d, lives only on the developer's machine.

### 6e. Backup automation

Compose doesn't back up volumes. If `docker compose down -v` runs by accident, the data is gone.

Options:
- **Docker volume backup containers** (e.g. `loomchild/volume-backup`) — copy volume contents to a tarball on a schedule.
- **In-database backups** — `pg_dump` running on a cron / Kubernetes CronJob, output to S3.
- **Storage-layer snapshots** — EBS snapshots, GCP disk snapshots, etc. Need application coordination for consistency.

We do `pg_dump` ad-hoc, no automation.

### 6f. Container runtime — when Docker isn't the answer

Docker is one of several container runtimes. Alternatives at the runtime level:
- `containerd` (the runtime Docker uses internally; can be used directly)
- `podman` (Red Hat's daemonless rootless alternative)
- `nerdctl` (a Docker-CLI-compatible frontend for containerd)

At the OCI image level they're all interoperable. You wouldn't choose differently for a small dev project; in regulated environments you might pick podman for its rootless model.

---

## 7. Challenges actually hit in this project

Documented to save the next reader some time.

### 7a. The macOS Redis port collision

A native `redis-server` running on `127.0.0.1:6379` silently shadowed our container's port mapping. The container *was* running and *was* mapping its 6379 to host 6379 — but the host port was already bound by the native daemon. The OS bound `127.0.0.1:6379` to whichever was first; the second got "Address already in use" buried in startup output and Docker just gave up the mapping.

**Symptoms**: connections to `redis://localhost:6379` worked, but they hit the native daemon, not the container. `docker exec tutor-redis redis-cli KEYS *` showed nothing. `redis-cli -h 127.0.0.1 -p 6379` showed the native daemon's data.

**The fix** was to map the container to a non-default host port:

```yaml
ports:
  - "6380:6379"   # host port 6380, container's 6379
```

And update every connection string in the codebase to use 6380. Documented in `docker-compose.yml:27` and called out in `end-to-end-flow.md`.

**The lesson**: host ports are a shared global resource. If your container "isn't getting traffic", run `lsof -i :PORT` on the host to see who's actually bound to it.

### 7b. The volume vs bind-mount confusion

Early in the project we tried `./postgres/data:/var/lib/postgresql/data` (a bind mount) for the Postgres data directory. Two problems:

1. **Slow** on macOS — every write went through virtiofs.
2. **Permission errors** — Postgres inside the container runs as UID 999 (the `postgres` user inside the official image). The host directory was owned by the developer's UID. Postgres refused to start.

We switched to a named volume (`pgdata:/var/lib/postgresql/data`), which Docker initialises with the right ownership and runs at native speed.

**The rule**: database storage → named volume. Always.

### 7c. The first-boot init scripts only run once

A new developer added a new extension to `postgres/init/02-new-extension.sql` and was confused why it didn't apply on their existing setup. Init scripts run only when the data directory is empty (first boot). Subsequent boots skip them.

**The fix** (and the rule for any future extension): add a real migration via Alembic that runs the `CREATE EXTENSION IF NOT EXISTS ...` SQL. Init scripts are *only* for the bootstrap state needed before Alembic can run.

If you really need to re-run init on an existing developer's environment:

```bash
docker compose down -v   # nukes the volume
docker compose up -d     # fresh init
uv run alembic upgrade head   # re-apply migrations
```

### 7d. `docker compose up` vs `docker-compose up` (the legacy CLI)

`docker-compose` (with the hyphen) is the legacy Python tool. `docker compose` (the plugin) is the Go reimplementation built into modern Docker Desktop. They have minor incompatibilities — older docs reference the legacy one. Always use the plugin syntax (`docker compose`) on modern systems.

### 7e. `restart: unless-stopped` doesn't auto-restart on `unhealthy`

If a container is healthy but slowly drifts into a half-broken state (memory leak, hung connection pool), the healthcheck might report unhealthy *without* the container exiting. The restart policy only triggers on container exit. The container stays in "running but unhealthy" forever.

For real auto-heal on healthcheck failure, you need:
- A sidecar like [autoheal](https://github.com/willfarrell/docker-autoheal) that watches `docker events` for `health_status: unhealthy` and triggers restart.
- Or Kubernetes, which has built-in restart-on-unhealthy via liveness probes.

We don't have this. Healthy-but-broken Postgres has never bitten us in dev.

---

## 8. Command reference (the cheat sheet you'll come back to)

Run from the repo root unless noted.

### Lifecycle

```bash
# Bring everything up (detached, build images if needed)
docker compose up -d

# Bring up only specific services
docker compose up -d postgres redis

# Tear down (containers + networks; volumes preserved)
docker compose down

# Tear down and DELETE VOLUMES (data goes with it)
docker compose down -v

# Stop without removing
docker compose stop

# Start previously-stopped containers
docker compose start

# Restart
docker compose restart postgres

# Pull newer images for the pinned tags
docker compose pull

# Rebuild (when Dockerfile or build context changes)
docker compose build --no-cache
```

### Inspecting state

```bash
# Containers + their status (compose-aware)
docker compose ps

# All containers system-wide
docker ps -a

# Images on the host
docker images

# Networks
docker network ls

# Volumes
docker volume ls

# Detail on one volume (where is it on disk?)
docker volume inspect personal-ai-tutor_pgdata

# Detail on one container (full config)
docker inspect tutor-postgres

# Resource usage (live)
docker stats

# Logs
docker logs tutor-postgres            # all
docker logs -f tutor-postgres         # follow
docker logs --tail 100 tutor-postgres # last 100 lines
docker logs --since 1h tutor-postgres # last hour
docker compose logs -f                # all services
docker compose logs -f postgres redis # specific
```

### Getting inside

```bash
# Shell into a running container
docker exec -it tutor-postgres bash

# Run a one-off command
docker exec tutor-postgres psql -U tutor -d tutor -c "SELECT 1;"

# Pipe stdin in
docker exec -i tutor-postgres psql -U tutor tutor < backup.sql

# Run a one-off container (cleaned up after)
docker run --rm -it postgres:16 bash
```

### Cleanup

```bash
# Remove stopped containers
docker container prune

# Remove unused images
docker image prune          # dangling only
docker image prune -a       # all unused

# Remove unused volumes (be careful — DATA LOSS)
docker volume prune

# Remove EVERYTHING unused
docker system prune -a --volumes

# Show what's taking space
docker system df
```

### Debugging

```bash
# Why is a container restarting?
docker inspect tutor-postgres --format '{{.State.RestartCount}} {{.State.Status}} {{.State.ExitCode}}'

# Healthcheck state
docker inspect tutor-postgres --format '{{json .State.Health}}' | jq

# Find what process is listening on a host port
lsof -i :6380

# Check if Docker can reach a service from inside another container
docker run --rm --network personal-ai-tutor_default alpine \
    sh -c "apk add --no-cache postgresql-client && pg_isready -h postgres -U tutor"

# Resource usage history (Linux only)
docker stats --no-stream
```

---

## 9. What you'd add for real production

A checklist of what's *not* in this project but should be in a prod-grade deployment:

- [ ] **Backend Dockerfile** with multi-stage build, non-root user, slim base.
- [ ] **`.dockerignore`** to keep build contexts lean.
- [ ] **CI pipeline** that builds + scans + signs + pushes images.
- [ ] **Image registry** (private; integrated with deploy tooling).
- [ ] **Resource limits + reservations** on every container.
- [ ] **Logging driver** (json-file with rotation in dev; loki/cloudwatch/datadog in prod).
- [ ] **Health checks** on every service (we have them on the data layer; we'd add them on the app once containerized).
- [ ] **Secrets** from a real secret store, not env vars.
- [ ] **Backup automation** for stateful volumes.
- [ ] **Monitoring/metrics** (cadvisor + node-exporter + Prometheus for container metrics; pgexporter / redis-exporter for service-specific).
- [ ] **Tracing** (OpenTelemetry instrumentation in the app; OTLP exporter to the collector).
- [ ] **Migration to an orchestrator** (Kubernetes or alternative) for multi-host.
- [ ] **TLS** between every hop (`postgres → app` with `sslmode=verify-full`, app → Redis with TLS, etc.).
- [ ] **Image scanning** (trivy / scout / grype) gated in CI.
- [ ] **Network policies** (Kubernetes NetworkPolicy or compose-internal segmentation).
- [ ] **Read-only root filesystem** on app containers (with explicit writable mounts for caches).

Each is a topic in itself.

---

## 10. Reading list

- **The official Docker docs**. Start with "Best practices for writing Dockerfiles" and "Overview of Docker Compose". They're terse and accurate.
- **`Container Security` (Liz Rice)** — the standard reference for how containers actually isolate (and how they don't).
- **`Kubernetes in Action` (Marko Lukša)** — when you graduate from Compose, this is the gentle on-ramp.
- **The OCI image spec** — short, technical, demystifies what "image" actually means.
- **`12 Factor App`** (12factor.net) — predates containers but is the design framework that makes apps container-friendly.
- **Docker Hub's official images repositories** (`hub.docker.com/_/postgres`, `_/redis`, etc.) — the README + Dockerfile for the image you're using is required reading. Always.
