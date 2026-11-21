# Docker for Node.js

**Load when** the project's runtime is Node.js (TypeScript or JavaScript). The rules are in `engineering-docker`; this file is how they are written for Node: the stages, the install commands and the development entrypoint. The templates are checked by a real build and run (`tests/e2e/test_docker_node_template.py`).

## Dockerfile

| Stage | Purpose |
|---|---|
| `base` | Node version, WORKDIR, system deps, package files only |
| `deps` | Every dependency, development tools included — cached until the lockfile changes |
| `development` | Local dev: non-root, bind-mount compatible, hot-reload entrypoint |
| `builder` | Compiles the source, then prunes development dependencies |
| `migrate` | Projects with migrations only: the migration tool and the migrations, run once per deploy |
| `production` | Minimal runtime: the build output and production dependencies only |

### Stage: base

```dockerfile
FROM node:26-alpine AS base
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package*.json ./
```

If Prisma is present, also copy the config and the schema, which `prisma generate` and the `migrate` stage need:

```dockerfile
COPY prisma.config.ts ./
COPY src/infrastructure/database/prisma ./src/infrastructure/database/prisma/
```

- Use `node:<lts>-alpine`, the newest LTS line (Active LTS), in both `FROM` lines. Never `node:latest`, and Debian only when a native addon requires glibc.
- `libc6-compat` is required on Alpine for Node.js native binaries.
- Copy only `package*.json` here: `COPY . .` at this stage invalidates the cache on every file change.

### Stage: deps

```dockerfile
FROM base AS deps
RUN npm ci --ignore-scripts
```

- `npm ci`, not `npm install`: a reproducible install from the lockfile.
- `--ignore-scripts`: lifecycle scripts of untrusted packages do not run at install time.

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

- Create the directories and set their owner before switching to the non-root user (Alpine's `node`, UID/GID 1000).
- The entrypoint installs dependencies, generates the Prisma client and starts the dev server. Migrations run in the `migrate` service, as in production.

### Stage: builder

```dockerfile
FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npx prisma generate
RUN npm run build
RUN npm prune --omit=dev
```

- Without Prisma, drop the `npx prisma generate` line: it exits non-zero when Prisma is not installed.
- Copy `node_modules` from `deps`; do not install again.
- `COPY . .` belongs here: this is where the source enters.
- `npm prune --omit=dev` removes the development dependencies from what the `production` stage copies. Whether a migration CLI survives depends on the package manager and on the packages: npm keeps `prisma` because `@prisma/client` declares it as a peer, while `pnpm prune --prod` removes it. Rely on neither: migrations run in the `migrate` stage.

### Stage: migrate (projects with migrations)

```dockerfile
FROM base AS migrate
COPY --from=deps /app/node_modules ./node_modules
RUN ./node_modules/.bin/prisma version
USER node
CMD ["./node_modules/.bin/prisma", "migrate", "deploy"]
```

- Built from `deps`, which still has the development dependencies: the migration CLI is a development dependency, and the pruned `builder` output does not have it.
- `prisma version` fetches the schema engine at build time, as root. `--ignore-scripts` skipped that download at install, and the non-root user cannot write to `node_modules` at run time.
- The `migrate` service of the compose file builds this target and runs it to completion before the app starts (`engineering-docker`, Migrations).
- With another migration tool, keep the stage and change the command.

### Stage: production

```dockerfile
FROM node:26-alpine AS production
ARG ENVIRONMENT=production
ENV NODE_ENV=${ENVIRONMENT}
WORKDIR /app
RUN apk add --no-cache libc6-compat dumb-init
RUN addgroup -g 1001 nodejs && \
    adduser -S -u 1001 -G nodejs app
COPY --from=builder --chown=app:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=app:nodejs /app/dist ./dist
COPY --from=builder --chown=app:nodejs /app/package.json ./package.json
USER app
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "dist/main.js"]
```

- Start fresh from `node:26-alpine`; never inherit from `builder`, which has the source, the tooling and the development dependencies.
- `dumb-init` is PID 1: it forwards signals and reaps zombies. Without it, `SIGTERM` on stop does not reach Node.js.
- A dedicated non-root user with a fixed UID/GID (1001), never the built-in `node` user.
- Copy only `dist/`, `node_modules/` and `package.json`; never `src/` or `tsconfig.json`.
- Every `COPY --from=builder` line carries `--chown`.

### Forbidden in a Node Dockerfile

| Pattern | Reason |
|---|---|
| `RUN npm install` in an image | Not reproducible; use `npm ci` |
| `CMD ["node", "dist/main"]` without an init | PID 1 does not forward signals; shutdown is not graceful |
| `prisma migrate deploy` (or any migration) in the `production` CMD | Every replica would migrate at boot, and whether the CLI is still in the pruned image depends on the package manager (`engineering-docker`, Migrations) |

## Development app service

The development compose runs the `development` target with the source bind-mounted:

```yaml
app:
  build:
    context: .
    target: development
  env_file:
    - .env
  environment: *database
  depends_on:
    postgres:
      condition: service_healthy
    migrate:
      condition: service_completed_successfully
  ports:
    - '${PORT:-3000}:3000'
  volumes:
    - .:/app
    - /app/node_modules
    - /app/dist
    - /app/src/infrastructure/database/prisma/generated
```

- The anonymous volumes keep `node_modules`, `dist` and the generated client from being overwritten by the bind mount.

## .dockerignore additions

```
node_modules
dist
coverage
.nyc_output
*.test.ts
*.spec.ts
```

## Development entrypoint

`docker/entrypoint-development.sh`:

```bash
#!/bin/sh
set -e

echo "Installing/updating dependencies..."
npm install

echo "Generating Prisma client..."
npx prisma generate

echo "Starting development server..."
exec npm run start:dev
```

- Without Prisma, drop the two Prisma lines: they exit non-zero when Prisma is not installed.
- `set -e` stops at the first error, and `exec` on the last command lets signals reach the app.
- Commit it executable: `git update-index --chmod=+x docker/entrypoint-development.sh`.
