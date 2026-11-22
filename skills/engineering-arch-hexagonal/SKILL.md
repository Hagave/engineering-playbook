---
name: engineering-arch-hexagonal
description: "Hexagonal architecture (ports and adapters) directory structure and dependency rules, language-agnostic. Load when the user chose hexagonal architecture during project scaffolding, or when working on a project that already uses this structure."
version: 2.1.2
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Hexagonal Architecture

```
src/
├── main.<ext>                    ← entry point
├── config/                       ← configuration and env validation
│
├── domain/                       ← zero external dependencies
│   ├── entities/
│   │   └── <feature>/
│   │       ├── <feature>.entity        ← private constructor + static factory (see engineering-ddd)
│   │       ├── <feature>.exceptions    ← domain exceptions scoped to this entity
│   │       └── <name>.vo               ← value objects co-located with entity
│   ├── services/                  ← domain service interfaces (DDD only)
│   │   └── <name>.interface            ← ex: IPasswordHasher, IEmailSender
│   └── repositories/
│       └── <feature>/
│           └── <name>.repository       ← interface only, never implementation
│
├── application/                  ← orchestration, depends only on domain/
│   ├── usecases/
│   │   └── <feature>/
│   │       ├── <use-case>.usecase
│   │       ├── <use-case>.dto          ← internal input/output DTOs (not HTTP DTOs)
│   │       └── <use-case>.formatter    ← response formatter (when needed)
│   └── services/
│       └── <name>.service              ← pure logic reusable across use cases (no I/O)
│
├── infrastructure/               ← concrete implementations, depends on domain/ + external libs
│   ├── repositories/
│   │   ├── <feature>/
│   │   │   └── <impl>-<name>.repository    ← implements domain/repositories/ interface
│   │   └── in-memory/
│   │       └── <name>-in-memory.repository
│   ├── external/
│   │   ├── <provider>/
│   │   │   └── <client>                ← HTTP client or third-party SDK
│   │   ├── <name>.client
│   │   └── http-retry.helper
│   ├── database/
│   │   └── <orm-or-driver>/            ← persistence adapter chosen in scaffold
│   │       ├── <connection>.client
│   │       ├── schema or migrations
│   │       ├── migrations/
│   │       └── models or entities
│   ├── cache/
│   │   ├── redis.client
│   │   └── <name>-cache.client
│   ├── services/
│   │   └── <name>                      ← domain service implementations (IPasswordHasher, etc.)
│   └── mappers/
│       └── <name>.mapper               ← converts domain entities ↔ persistence/infra models
│
├── presentation/                  ← HTTP entry point, depends on application/
│   ├── controllers/ or handlers/
│   │   └── v1/
│   │       └── <feature>.controller or handler
│   ├── dtos/
│   │   └── <feature>/
│   │       └── <name>.dto              ← HTTP request/response DTOs
│   ├── mappers/
│   │   └── <name>.mapper               ← converts HTTP DTO → use case input
│   └── modules/ or composition root/
│       ├── app module                  ← root module (global concerns only)
│       ├── shared module               ← global providers (persistence, logger, cache)
│       └── <feature> module            ← one group per feature, flat (no subfolders)
│
└── common/                        ← cross-cutting concerns — no business logic, importable by any layer
    ├── decorators/
    ├── exceptions/                     ← DomainException base hierarchy (see engineering-error-handling)
    │   └── domain.exception            ← base class + derived exceptions
    ├── filters/                        ← maps domain exceptions → HTTP responses
    ├── guards/
    ├── interceptors/
    │   ├── trace-id
    │   └── transform-response
    ├── logger/
    │   ├── logger.interface             ← contract only
    │   └── <framework>-logger          ← concrete logger via framework/language skill
    └── utils/                          ← pure helpers (retry, circuit-breaker, concurrent-runner)
```

---

## Dependency rules

The layer contract — import direction, domain purity, repository wiring, forbidden patterns — is owned by `engineering-architecture`. Load it alongside this skill; this file owns only the concrete directory placement for hexagonal projects.

---

## Port contract rule

Ports are contracts, not implementations. In languages that support interfaces, use an interface (e.g. TypeScript `interface`, Go `interface`, Java `interface`) rather than an abstract class with method bodies or instance state. In languages without interfaces, model ports as function types or behavior-free abstract contracts.

**TypeScript example:** Ports (`domain/repositories/` and `domain/services/`) must be TypeScript `interface`, never `abstract class`.

**Why this matters:** An `abstract class` is a valid TypeScript class — it can accumulate method bodies, instance state, and business logic. Nothing prevents a future developer from adding an `if` or a calculation inside it. When that happens, the port is no longer a pure contract: it carries behavior that the domain should not own.

```typescript
// wrong — abstract class used as a port
export abstract class IUserRepository {
  abstract findById(id: string): Promise<User | null>;
  // nothing stops someone from adding this later:
  // async findOrFail(id: string) { const u = await this.findById(id); if (!u) throw ...; }
}

// correct — interface is a compile-time-only contract, zero runtime weight
export interface IUserRepository {
  findById(id: string): Promise<User | null>;
  create(user: User): Promise<void>;
}
```

The concrete adapter implements the interface:
```typescript
// infrastructure/repositories/user/prisma-user.repository.ts
export class PrismaUserRepository implements IUserRepository { ... }
```

DI registration in the chosen framework stays the same; NestJS example:
```typescript
{ provide: 'IUserRepository', useClass: PrismaUserRepository }
// or use the interface as a string token: inject via @Inject('IUserRepository')
```

**During audit:** Flag every `abstract class` found in `domain/repositories/` or `domain/services/` as a violation. Present the conversion to the developer — the migration is mechanical (change keyword, remove `abstract` from method signatures) — but let them decide. If they choose to keep the abstract class, document the risk and add a comment to the class warning against adding method bodies.

---

## Folder creation rule

Create a folder only when its first file is added — rule owned by `engineering-architecture`. The complete directory tree in this skill is a reference for what the finished project looks like, not a command to `mkdir -p` every path on day one.

---

## Env validation

Environment validation lives in `config/` and must fail fast at boot — no silent `undefined` values at runtime. The validation library is owned by `engineering-security`. For TypeScript/NestJS projects, this skill uses Joi via `@nestjs/config` and owns the file template:

```typescript
// src/config/env.validation.ts
import Joi from 'joi';

export const envValidationSchema = Joi.object({
  NODE_ENV: Joi.string().valid('development', 'production', 'test').required(),
  PORT: Joi.number().default(3000),
  DATABASE_URL: Joi.string().uri().required(),
});
```

Registered in the composition root (NestJS `AppModule`):

```typescript
ConfigModule.forRoot({
  isGlobal: true,
  skipProcessEnv: true,
  validationSchema: envValidationSchema,
})
```

The schema sees the whole `process.env`, so it never sets `allowUnknown: false`; `skipProcessEnv: true` serves only the declared variables (rules owned by `engineering-nestjs`, Configuration).

If env variables are missing or invalid, the process exits at boot with a descriptive error — no silent `undefined` values at runtime.

---

## Key conventions

- Entity constructors are private. Instantiation goes through semantic static factory methods. Load `engineering-ddd` for the full entity pattern.
- **Exception placement — two levels, both required:**
  - **Base hierarchy** (`DomainException`, `BusinessRuleException`, `NotFoundException`, `UnauthorizedException`, etc.) → `common/exceptions/`. Cross-cutting, used by the exception filter in `common/filters/`. Created once per project via the framework-specific interceptor/exception skill (e.g. `engineering-nestjs-interceptors`).
  - **Entity-specific domain exceptions** (`UserAlreadyExistsException`, `InvalidEmailException`, etc.) → `domain/entities/<feature>/<feature>.exceptions`. Scoped to a single entity's invariants — pure domain, no framework dependency.
- Value objects are co-located with their entity.
- Use case DTOs (`application/usecases/<feature>/<use-case>.dto.ts`) are distinct from HTTP DTOs (`presentation/dtos/`). Never share them.
- Presentation mappers convert HTTP DTOs → use case inputs. Infrastructure mappers convert domain entities ↔ persistence models.
- The composition root lives in `presentation/modules/` (or the framework's equivalent). One group per feature, flat.
- The shared module registers global providers. Never re-declare them in feature modules.
- Guards, filters, and interceptors are cross-cutting concerns — they live in `common/`, not in `infrastructure/`. `common/` is importable by any layer.
- Logger contract lives in `common/logger/` (for TypeScript: `logger.interface.ts` with `ILogger` + `LOGGER_TOKEN`). The concrete implementation is selected by the framework/language skill. Use cases and domain services depend on the contract, never a concrete logger.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
