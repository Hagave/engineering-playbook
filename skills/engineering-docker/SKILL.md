---
name: engineering-docker
description: "Docker and docker-compose rules. Invoke when Docker is part of the project setup: creating a Dockerfile, docker-compose, or .dockerignore. Covers multi-stage build structure, production security (non-root user, dumb-init, minimal layers), and compose composition based on project infrastructure choices."
version: 2.2.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->

# Docker

Never generate Docker files speculatively — only when Docker is selected during project setup.

| Step | Description | Status |
|---|---|---|
| 1 | Dockerfile (multi-stage: base → deps → development → builder → production) | — |
| 2 | docker-compose.yml (services matching project infra choices) | — |
| 3 | .dockerignore | — |
| 4 | docker/entrypoint-development.sh (With Prisma or Without Prisma variant) | — |

---

## Dockerfile — multi-stage structure

Single-stage Dockerfiles are forbidden — they ship devDependencies, source code, and build tooling into the production image.

| Stage | Purpose |
|---|---|
| `base` | Node version, WORKDIR, system deps, package files only |
| `deps` | Dependency installation — cached until `package.json` changes |
| `development` | Local dev: non-root, bind-mount compatible, hot-reload entrypoint |
| `builder` | Compiles source, prunes devDeps — produces the production artifact |
| `production` | Minimal runtime: dist + production deps only, no source |

### Stage: base

```dockerfile
FROM node:22-alpine AS base
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package*.json ./
```

If Prisma is present, also copy config and schema — required for `prisma generate` in builder:

```dockerfile
COPY prisma.config.ts ./
COPY src/infrastructure/database/prisma ./src/infrastructure/database/prisma/
```

- Use `node:<lts>-alpine`. Never `node:latest` or Debian unless a native addon explicitly requires glibc.
- `libc6-compat` is required on Alpine for Node.js native binaries.
- Copy only `package*.json` here — `COPY . .` at this stage cache-busts on every file change.

### Stage: deps

```dockerfile
FROM base AS deps
RUN npm ci --ignore-scripts
```

- `npm ci` not `npm install` — reproducible install from lockfile.
- `--ignore-scripts` — prevents lifecycle scripts from untrusted packages from executing at install time.

### Stage: development

```dockerfile
FROM base AS development
RUN mkdir -p /app/dist /app/node_modules /app/src/infrastructure/database/prisma/generated && \
    chown -R node:node /app
USER node
COPY --chown=node:node docker/entrypoint-development.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 3000
ENTRYPOINT ["/entrypoint.sh"]
```

- Create directories and set ownership before switching to non-root (Alpine's `node` user, GID/UID 1000).
- The entrypoint script handles `npm install`, `prisma generate`, migrations, and `start:dev`. Keep the Dockerfile clean.

### Stage: builder

**With Prisma:**

```dockerfile
FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npx prisma generate
RUN npm run build
RUN npm prune --omit=dev
```

**Without Prisma:**

```dockerfile
FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build
RUN npm prune --omit=dev
```

- Copy `node_modules` from `deps` — do not reinstall.
- `COPY . .` here is correct — this is where source enters.
- `npm prune --omit=dev` after build removes devDependencies from what gets copied to production.
- Never include `npx prisma generate` if Prisma is not in the project — it exits non-zero.

### Stage: production

**Common block (always):**

```dockerfile
FROM node:22-alpine AS production
ARG ENVIRONMENT=production
ENV NODE_ENV=${ENVIRONMENT}
WORKDIR /app
RUN apk add --no-cache libc6-compat dumb-init
RUN addgroup -g 1001 nodejs && \
    adduser -S -u 1001 -G nodejs nestjs
COPY --from=builder --chown=nestjs:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=nestjs:nodejs /app/dist ./dist
COPY --from=builder --chown=nestjs:nodejs /app/package.json ./package.json
```

**With Prisma — additional COPY + CMD:**

```dockerfile
COPY --from=builder --chown=nestjs:nodejs /app/prisma.config.ts ./prisma.config.ts
COPY --from=builder --chown=nestjs:nodejs /app/src/infrastructure/database/prisma ./src/infrastructure/database/prisma
USER nestjs
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["dumb-init", "--"]
CMD ["sh", "-c", "./node_modules/.bin/prisma migrate deploy && exec node dist/main.js"]
```

**Without Prisma:**

```dockerfile
USER nestjs
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "dist/main.js"]
```

- Start fresh from `node:22-alpine` — never inherit from `builder` (it has source, tooling, devDeps).
- `dumb-init` is PID 1: forwards signals correctly and reaps zombies. Without it, `SIGTERM` on stop does not reach Node.js.
- Dedicated non-root user with fixed GID/UID 1001. Never use the built-in `node` user in production.
- Copy only `dist/`, `node_modules/`, `package.json`. Never copy `src/` or `tsconfig.json`.
- All `COPY --from=builder` lines must include `--chown=nestjs:nodejs`.
- `EXPOSE 8080` — production port, distinct from dev 3000.
- Prisma only: `prisma migrate deploy` runs before app starts.

### Forbidden Dockerfile patterns

| Pattern | Reason |
|---|---|
| `FROM node:latest` | Unpinned — breaks reproducibility |
| Single-stage build | Ships devDeps, source, and build tooling into production |
| `RUN npm install` | Non-reproducible; use `npm ci` |
| `COPY . .` in `base` or `deps` | Cache-busts on any source file change |
| Running as root in production | Container escape = root on host |
| `ENV SECRET=value` or `ARG SECRET` | Secrets end up in image layer history |
| `CMD ["node", "dist/main"]` without dumb-init | PID 1 does not forward signals; ungraceful shutdown |

---

## docker-compose — composition rules

Include only the services the project actually uses.

| User choice | Include service |
|---|---|
| Database: PostgreSQL | `postgres` |
| Database: MySQL | `mysql` |
| Cache: Redis | `redis` |
| Always | `app` |

### App service

```yaml
app:
  build:
    context: .
    dockerfile: Dockerfile
    target: development
  container_name: <project>-app
  restart: unless-stopped
  env_file:
    - .env
  environment:
    DATABASE_URL: postgresql://${POSTGRESQL_USER:-postgres}:${POSTGRESQL_PASSWORD:-postgres}@postgres:5432/${POSTGRESQL_NAME:-postgres}
    POSTGRESQL_HOST: postgres
    POSTGRESQL_PORT: 5432
    REDIS_URL: redis://redis:6379
  depends_on:
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy
  ports:
    - '${PORT:-3000}:3000'
  volumes:
    - .:/app
    - /app/node_modules
    - /app/dist
    - /app/src/infrastructure/database/prisma/generated
  networks:
    - <project>-network
```

- `env_file: .env` for secrets. `environment:` for derived values (connection strings combining sibling service host/port).
- `depends_on` with `condition: service_healthy` — app never starts before dependencies are ready.
- Anonymous volumes for `node_modules`, `dist`, and `generated` — must NOT be overwritten by the bind mount.

### PostgreSQL service

```yaml
postgres:
  image: postgres:17-alpine
  container_name: <project>-postgres
  restart: unless-stopped
  environment:
    POSTGRES_USER: ${POSTGRESQL_USER:-postgres}
    POSTGRES_PASSWORD: ${POSTGRESQL_PASSWORD:-postgres}
    POSTGRES_DB: ${POSTGRESQL_NAME:-postgres}
  ports:
    - '${POSTGRESQL_PORT:-5432}:5432'
  volumes:
    - postgres_data:/var/lib/postgresql/data
  healthcheck:
    test: ['CMD-SHELL', 'pg_isready -U ${POSTGRESQL_USER:-postgres}']
    interval: 10s
    timeout: 5s
    retries: 5
  networks:
    - <project>-network
```

If the project uses pgvector: `image: pgvector/pgvector:pg17`.

### Redis service

```yaml
redis:
  image: redis:7-alpine
  container_name: <project>-redis
  restart: unless-stopped
  ports:
    - '${REDIS_PORT:-6379}:6379'
  volumes:
    - redis_data:/data
  healthcheck:
    test: ['CMD', 'redis-cli', 'ping']
    interval: 10s
    timeout: 5s
    retries: 5
  networks:
    - <project>-network
```

### Volumes and networks

```yaml
volumes:
  postgres_data:
    driver: local
  redis_data:
    driver: local

networks:
  <project>-network:
    driver: bridge
```

- All persistent data uses named volumes — never bind-mount database data directories.
- Named networks provide DNS resolution by container name (`postgres`, `redis`) — the default bridge network does not.

### Credentials

Pattern: `${VAR:-default}`. Never hardcode passwords. `.env` contains actual values; `.env.example` documents required vars without values.

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
- `cap_drop: [ALL]` — a Node.js server on port 8080 needs no Linux capabilities. Add back only a named capability, with the reason in a comment.
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
node_modules
dist
.git
.env
*.log
coverage
.nyc_output
*.md
*.test.ts
*.spec.ts
```

---

## Entrypoint script (development)

`docker/entrypoint-development.sh`:

**With Prisma:**

```bash
#!/bin/sh
set -e

echo "Installing/updating dependencies..."
npm install

echo "Generating Prisma client..."
npx prisma generate

echo "Running migrations..."
npx prisma migrate deploy

echo "Starting development server..."
exec npm run start:dev
```

**Without Prisma:**

```bash
#!/bin/sh
set -e

echo "Installing/updating dependencies..."
npm install

echo "Starting development server..."
exec npm run start:dev
```

- `set -e` — exit immediately on any error.
- `exec` on the final command — replaces the shell so signals reach the app directly.
- Never include Prisma commands if Prisma is not in the project — exits non-zero.
- Commit with execute permission: `git update-index --chmod=+x docker/entrypoint-development.sh`.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
