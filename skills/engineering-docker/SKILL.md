---
name: engineering-docker
description: "Docker and docker-compose rules for any language. Invoke when Docker is part of the project setup: creating a Dockerfile, docker-compose, or .dockerignore. Covers multi-stage builds, production security (non-root user, an init process, minimal images), migrations as their own step, and compose composition based on project infrastructure choices. Per-ecosystem templates live in references/."
version: 3.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->

# Docker

Never generate Docker files speculatively — only when Docker is selected during project setup.

The rules below hold for every language. How a stage is written (base image, install command, build command) depends on the ecosystem: read the reference for the project's runtime before writing the Dockerfile.

| Runtime | Reference |
|---|---|
| Node.js (TypeScript, JavaScript) | `references/node.md` |

For a runtime with no reference, apply the rules with the ecosystem's own build tool and minimal runtime image, and say in the final message that the playbook had no template for it.

| Step | Description | Status |
|---|---|---|
| 1 | Dockerfile (multi-stage, per the rules below and the ecosystem reference) | — |
| 2 | docker-compose.yml (services matching project infra choices; a `migrate` service when the project has migrations) | — |
| 3 | .dockerignore | — |
| 4 | Development entrypoint, when the ecosystem reference has one | — |

---

## Dockerfile — rules

Single-stage Dockerfiles are forbidden: they ship development dependencies, source code and build tooling into the production image.

| Stage | Purpose |
|---|---|
| dependencies | The dependency manifest and lockfile only, so the layer stays cached until they change |
| build | The toolchain: compiles or packages the source into the artifact |
| development | Optional, local only: non-root, works with a bind-mounted source tree |
| `migrate` | Only when the project has migrations: the migration tool and the migrations, run once per deploy |
| `production` | A fresh minimal runtime image with the artifact and its runtime dependencies only |

- **Pinned base images.** A version tag, never `latest`. Prefer a minimal image (alpine, slim, distroless) unless a native library needs another one.
- **Reproducible installs** from the lockfile, with install-time scripts disabled where the ecosystem allows it.
- **Manifests before source.** Copy the dependency manifest and lockfile first and the source after, so a source change does not reinstall dependencies.
- **The production stage starts fresh.** Never inherit from the build stage: it has the source, the compiler and the development dependencies. Copy only the artifact and what it needs at run time.
- **A non-root user** with a fixed UID/GID in production.
- **PID 1 forwards signals.** Use an init process (`dumb-init`, `tini`) as the entrypoint, unless the runtime is documented to handle `SIGTERM` and reap children as PID 1. Use the exec form for `CMD`/`ENTRYPOINT`, so the process gets the signals.
- **No secrets in the image.** Never `ENV` or `ARG` for a secret: both stay in the layer history. A credential the build needs goes through a build secret (`RUN --mount=type=secret`).

### Forbidden Dockerfile patterns

| Pattern | Reason |
|---|---|
| `FROM <image>:latest` | Unpinned — breaks reproducibility |
| Single-stage build | Ships development dependencies, source and tooling into production |
| `COPY . .` before installing dependencies | Invalidates the dependency cache on every source change |
| Running as root in production | A container escape is root on the host |
| `ENV SECRET=value` or `ARG SECRET` | Secrets end up in the image layer history |
| A migration in the production `CMD` or `ENTRYPOINT` | See Migrations below |

---

## Migrations

A schema migration runs as its own step, once per deploy, before the new version of the app starts:
- in compose, a one-shot `migrate` service built from the `migrate` target, which the app waits for with `condition: service_completed_successfully`;
- in an orchestrator, a job, an init container, or a release phase.

It never runs in the `CMD` or `ENTRYPOINT` of the production image, for three reasons:
1. **Every replica would migrate at boot**, racing each other.
2. **A failed migration crash-loops the app** instead of stopping the deploy.
3. **The production image would have to carry the migration tool.** When the tool is a development dependency, whether it survives the pruning depends on the package manager. Where it is gone, the container fails at start.

The image that runs the migration must contain the tool: the `migrate` target is built from a stage that has it, never from the pruned runtime. The command is the project's own tool: the Migration tool row of the language opt-in (Flyway, Liquibase, EF Core, golang-migrate, goose, sqlx-cli, refinery, Rails, Prisma, Alembic and the rest).

---

## docker-compose — composition rules

Include only the services the project actually uses.

| User choice | Include service |
|---|---|
| Database: PostgreSQL | `postgres` |
| Database: MySQL | `mysql` |
| Cache: Redis | `redis` |
| The project has migrations | `migrate` |
| Always | `app` |

### Services

```yaml
x-database: &database
  DATABASE_URL: postgresql://${POSTGRESQL_USER:-postgres}:${POSTGRESQL_PASSWORD:-postgres}@postgres:5432/${POSTGRESQL_NAME:-postgres}

services:
  postgres:
    image: postgres:17-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRESQL_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRESQL_PASSWORD:-postgres}
      POSTGRES_DB: ${POSTGRESQL_NAME:-postgres}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ['CMD-SHELL', 'pg_isready -U ${POSTGRESQL_USER:-postgres}']
      interval: 5s
      timeout: 5s
      retries: 10

  migrate:
    build:
      context: .
      target: migrate
    environment: *database
    depends_on:
      postgres:
        condition: service_healthy
    restart: 'no'

  app:
    build:
      context: .
      target: production
    restart: unless-stopped
    environment: *database
    depends_on:
      postgres:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully

volumes:
  postgres_data:
```

- `env_file: .env` holds secrets; `environment:` holds derived values, such as a connection string built from sibling service names. The anchor keeps the `migrate` and `app` values identical.
- `depends_on` with `condition: service_healthy` keeps the app and the migration from starting before the database is ready; `service_completed_successfully` keeps the app from starting before the migration finished.
- The development app service (target `development`, source bind-mounted, published port) is in the ecosystem reference.
- With pgvector: `image: pgvector/pgvector:pg17`.

### Redis service

```yaml
redis:
  image: redis:7-alpine
  restart: unless-stopped
  volumes:
    - redis_data:/data
  healthcheck:
    test: ['CMD', 'redis-cli', 'ping']
    interval: 10s
    timeout: 5s
    retries: 5
```

### Volumes, networks and credentials

- All persistent data uses named volumes. Never bind-mount a database data directory.
- Services reach each other by service name on the compose project's network. Publish a port only for what the host needs to reach.
- Credentials use `${VAR:-default}`. Never hardcode passwords. `.env` holds the real values and is git-ignored; `.env.example` documents the variables without values.

### Runtime hardening

Apply this to the production app container, in a compose file used for production-like runs or in the orchestrator manifest:

```yaml
app:
  read_only: true
  tmpfs:
    - /tmp
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true
```

- `read_only: true` — the app writes only to `tmpfs` or to volumes it owns, so a compromised process cannot drop binaries or rewrite its own code. The development compose may skip it, because hot reload writes to the source tree.
- `cap_drop: [ALL]` — a server listening on a port above 1024 needs no Linux capabilities. Add back only a named capability, with the reason in a comment.
- `no-new-privileges:true` — setuid binaries inside the image cannot raise privileges.
- Kubernetes uses the same settings under `securityContext`: `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `runAsNonRoot: true`.

### Forbidden compose patterns

| Pattern | Reason |
|---|---|
| `network_mode: host` | Bypasses network isolation |
| `privileged: true` | Gives the container every capability and the host's devices — an escape is root on the host |
| Mounting `/var/run/docker.sock` | Control of the Docker daemon is root on the host |
| `cap_add: [ALL]` or `SYS_ADMIN` | Same as privileged in practice |
| `pid: host` / `ipc: host` | Shares the host's process or IPC namespace |
| Hardcoded passwords | Secrets in version control |
| Missing healthchecks on infrastructure services | App starts before DB is ready |
| `depends_on` without `condition: service_healthy` | Same as above |
| `image: postgres:latest` | Unpinned — breaks reproducibility |
| Bind-mounting DB data directory | Permissions and ownership issues across platforms |

---

## .dockerignore

```
.git
.env
*.log
*.md
```

Add the ecosystem's build output, dependency directory and test files, as listed in its reference: what the build stage produces must never enter the build context from the host.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
