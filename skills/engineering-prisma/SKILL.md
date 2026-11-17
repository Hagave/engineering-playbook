---
name: engineering-prisma
description: "**Opt-in for Prisma:** load only when Prisma is the chosen database access layer. Prisma-specific rules for schema structure, PrismaService, and configuration. Invoke when @prisma/client or prisma is present in the project dependencies, when writing or modifying Prisma schemas, PrismaService, migrations, or any database access code that uses Prisma."
version: 2.1.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Prisma

These rules apply only when Prisma is present in the project. Do not apply them to projects using other ORMs.

---

## Configuration (Prisma 7)

Prisma 7 uses a `prisma.config.ts` at the project root — not the legacy `prisma/` directory at root level.

```typescript
// prisma.config.ts (root)
// dotenv/config and process.env are intentional here — Prisma CLI runs outside NestJS at generate/migrate time
import 'dotenv/config';
import { defineConfig } from 'prisma/config';

export default defineConfig({
  schema: 'src/infrastructure/database/prisma',
  migrations: {
    path: 'src/infrastructure/database/prisma/migrations',
  },
  datasource: {
    url: process.env['DATABASE_URL'],
  },
});
```

---

## Schema structure

### After `prisma init`

`prisma init` generates a single `prisma/schema.prisma` at the project root — that location and structure are both wrong. Fix immediately:

1. Move the directory: `prisma/` → `src/infrastructure/database/prisma/`
2. Update `prisma.config.ts` to point to the new path.
3. Create the `models/` subdirectory for model files.
4. Leave `schema.prisma` with generator + datasource only — no models.

### Correct structure

```
src/infrastructure/database/prisma/
├── schema.prisma          ← generator + datasource ONLY — no model blocks here
├── migrations/
├── generated/             ← Prisma Client output (gitignored)
└── models/
    ├── user.prisma        ← one file per entity or bounded context
    ├── account.prisma
    └── order.prisma
```

**Wrong — model inside `schema.prisma`:**

```prisma
// schema.prisma  ← WRONG
generator client { ... }
datasource db { ... }

model User {          // ← never here
  id    String @id
  email String @unique
}
```

**Correct — `schema.prisma` has only generator + datasource:**

```prisma
// schema.prisma
generator client {
  provider     = "prisma-client"
  output       = "./generated/prisma"
  moduleFormat = "cjs"  // CJS required for NestJS runtime compatibility even when tsconfig uses nodenext moduleResolution
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")  // required even with prisma.config.ts — tools running without the config file need a fallback
  schemas  = ["app", "rag"]
}
```

```prisma
// models/user.prisma
model User {
  id        String   @id
  email     String   @unique
  password  String
  name      String
  createdAt DateTime @default(now()) @map("created_at")

  @@map("users")
  @@schema("app")
}
```

Prisma resolves all `models/*.prisma` files automatically because `prisma.config.ts` points to the directory — no manual import needed.

One file per entity or bounded context. Name matches the domain concept: `user.prisma`, `account.prisma`, `order.prisma` — not `models.prisma` or `schema.prisma`.

---

## PrismaService

Always use `PrismaPg` adapter. Never instantiate `PrismaClient` with a raw connection string in the constructor — always delegate to `ConfigService`.

```typescript
import { Injectable, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaPg } from '@prisma/adapter-pg';
import { PrismaClient } from './generated/prisma/client';

@Injectable()
export class PrismaService
  extends PrismaClient
  implements OnModuleInit, OnModuleDestroy
{
  constructor(config: ConfigService) {
    const databaseUrl = config.getOrThrow<string>('DATABASE_URL');

    const adapter = new PrismaPg({ connectionString: databaseUrl });

    super({ adapter });
  }

  async onModuleInit(): Promise<void> {
    await this.$connect();
  }

  async onModuleDestroy(): Promise<void> {
    await this.$disconnect();
  }
}
```

`PrismaService` is registered once in `shared.module.ts` as a global provider. Never re-provide or re-import it in feature modules.

---

## Mappers

Never return a Prisma model type from a repository. Always map to a domain entity before returning.

Infrastructure mappers (`infrastructure/mappers/<name>.mapper.ts`) are responsible for:
- Prisma model → domain entity
- Domain entity → Prisma create/update input

```typescript
// infrastructure/mappers/user.mapper.ts
export class UserMapper {
  static toDomain(raw: UserModel): User {
    // reconstitute() bypasses business rules intentionally — loading from persistence is not a business action
    return User.reconstitute({ id: raw.id, email: Email.reconstitute(raw.email), name: raw.name });
  }

  static toPrisma(user: User): Prisma.UserCreateInput {
    return { id: user.id, email: user.email.value, name: user.name };
  }
}
```

---

## Error handling

**Never do a read-before-write to check existence.** A `findUnique` followed by `update` or `delete` is always wrong: it doubles the round-trips, adds latency, and introduces a race condition window between the two queries. Prisma raises `PrismaClientKnownRequestError` with code `P2025` when a write operation targets a record that does not exist — catch it and map to a domain exception.

```typescript
// wrong — two queries, one unnecessary
const user = await this.prisma.user.findUnique({ where: { id } });
if (!user) throw new UserNotFoundError(id);
return await this.prisma.user.update({ where: { id }, data });

// correct — one query; error mapping goes through the centralized
// mapPrismaError (see "Prisma exception filter" below — never inline)
try {
  return await this.prisma.user.update({ where: { id }, data });
} catch (e) {
  mapPrismaError(e, 'user');
}
```

**Prisma error codes to map at repository boundaries:**

Exception class names are owned by `engineering-nestjs-interceptors` — this table must always match `mapPrismaError` below:

| Code | Meaning | Map to |
|---|---|---|
| `P2025` | Record not found (update/delete target) | `DomainNotFoundException` |
| `P2002` | Unique constraint violation | `ConflictException` |
| `P2003` | Foreign key constraint failure | `DomainNotFoundException` (referenced record not found) |
| `P2034` | Transaction conflict (serialization failure) | Retry, then `ConflictException` |

Never let `PrismaClientKnownRequestError` propagate beyond the repository — map it to a domain exception before returning. Unknown codes re-throw as-is; the global exception filter handles them.

---

## Prisma exception filter

Centralizes all `PrismaClientKnownRequestError` mapping in a single function at `infrastructure/database/prisma/prisma-exception.filter.ts`. Never do inline try/catch for Prisma error codes inside individual repositories — each repository would duplicate the same switch/if logic.

```typescript
// infrastructure/database/prisma/prisma-exception.filter.ts
import { Prisma } from './generated/prisma/client.js';
import { ConflictException } from '@common/exceptions/conflict.exception.js';
import { DomainNotFoundException } from '@common/exceptions/not-found.exception.js';

export function mapPrismaError(e: unknown, entity?: string): never {
  if (e instanceof Prisma.PrismaClientKnownRequestError) {
    switch (e.code) {
      case 'P2002':
        throw new ConflictException(`${entity ?? 'record'} already exists`);
      case 'P2025':
        throw new DomainNotFoundException(`${entity ?? 'record'} not found`);
      case 'P2003':
        throw new DomainNotFoundException(`referenced ${entity ?? 'record'} not found`);
      default:
        throw e;
    }
  }
  throw e;
}
```

> **Prisma 7 — namespace import required.** `PrismaClientKnownRequestError` is not a named export from the generated client. It lives under the `Prisma` namespace: `import { Prisma } from './generated/prisma/client.js'` then `Prisma.PrismaClientKnownRequestError`. Importing it directly as a named export will fail at runtime with `undefined`.

Usage in every repository method that writes to the database:

```typescript
import { mapPrismaError } from '@infrastructure/database/prisma/prisma-exception.filter';

async save(user: User): Promise<void> {
  try {
    await this.prisma.user.create({ data: UserMapper.toPrisma(user) });
  } catch (e) {
    mapPrismaError(e, 'user');
  }
}
```

> **`create()` not `upsert()` for `save()`:** Use `create()` when the operation is "insert a new record." `upsert()` is for "insert or update" semantics and makes P2025 unreachable in this context — Prisma only throws P2025 for `update`, `updateMany`, `delete`, and `deleteMany`. Using `upsert` in a `save()` that is always a first-time insert is misleading and produces dead error-handling branches.

The function always throws — the `never` return type ensures TypeScript does not require a `return` after the call site.

---

## Migrations

- Always generate migrations via CLI: `prisma migrate dev --name <description>`
- Never write migration files manually.
- Never edit a migration that has already been applied — it causes state drift and checksum failure.
- Use `prisma migrate dev --create-only` to generate without applying when manual SQL edits are needed before running.
- `ensure-indexes.sql` is for custom indexes not expressible in Prisma schema — run separately as part of the deployment process.

---

## VS Code extension

Add `"Prisma.prisma"` to `.vscode/extensions.json` if not already present. The extension provides schema syntax highlighting, autocompletion, and format-on-save for `.prisma` files.

```json
{
  "recommendations": [
    "biomejs.biome",
    "dbaeumer.vscode-eslint",
    "Prisma.prisma"
  ]
}
```

---

## Generated client

- The `generated/` directory must be in `.gitignore`.
- Import `PrismaClient` only from `./generated/prisma/client` — never from `@prisma/client` directly in application code.
- Run `prisma generate` after schema changes before building or running tests.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
