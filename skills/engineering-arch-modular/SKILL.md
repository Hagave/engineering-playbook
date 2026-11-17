---
name: engineering-arch-modular
description: "Modular monolith architecture directory structure and dependency rules, language-agnostic. Load when the user chose modular monolith during project scaffolding, or when working on a project structured around bounded-context modules."
version: 2.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Modular Monolith Architecture

Each module is a self-contained bounded context. Maintains separation between HTTP, business logic, and data access within each module, without the full ports-and-adapters formalism of hexagonal.

```
src/
├── main.<ext>                    ← entry point
├── config/                       ← configuration and env validation
│
├── modules/
│   └── <feature>/
│       ├── controllers/ or handlers/
│       │   └── <feature>.controller or handler ← routes, input parsing
│       ├── services/
│       │   └── <feature>.service    ← business logic; may split into multiple if needed
│       ├── repositories/
│       │   └── <feature>.repository ← data access; one class, one responsibility
│       ├── dtos/
│       │   ├── <name>.request.dto
│       │   └── <name>.response.dto
│       ├── entities/
│       │   └── <feature>.entity     ← ORM entity or lightweight domain object
│       ├── mappers/
│       │   └── <feature>.mapper     ← entity ↔ DTO conversions
│       └── <feature> module         ← registers all providers for this bounded context
│
├── shared/
│   ├── database/
│   │   └── <orm-or-driver>/         ← persistence adapter chosen in scaffold
│   │       ├── <connection>.client
│   │       ├── schema or migrations
│   │       ├── migrations/
│   │       └── models or entities
│   ├── cache/
│   │   ├── redis.client
│   │   └── <name>-cache.client
│   └── shared module                ← global providers (persistence, logger, cache)
│
└── common/
    ├── decorators/
    ├── exceptions/
    ├── filters/
    ├── guards/
    ├── interceptors/
    ├── logger/
    │   ├── logger.interface         ← contract only
    │   └── <framework>-logger       ← concrete logger via framework/language skill
    └── utils/
```

---

## Dependency rules

```
controller → service → repository
                ↑
         shared/ + common/ (available to all modules, import none of them)
```

- Controllers depend on services. Services depend on repositories.
- Modules never access another module's repository directly — cross-module communication goes through a service interface or events (event-driven rationale owned by `engineering-design-patterns`).
- `shared/` provides infrastructure (DB, cache). `common/` provides utilities. Neither imports from `modules/`.
- Each module is registered independently. The shared module is global — never re-declare its providers.

---

## Key conventions

- Each bounded context lives in its own folder under `modules/`. No sub-nesting within a module.
- Service is the owner of business logic. When a service grows beyond two distinct responsibilities, split into two services — or consider migrating to hexagonal.
- Mappers live inside each module (`modules/<feature>/mappers/`) — not in a global mappers folder.
- The shared module registers global providers once. Feature modules import it when needed.
- If DDD is requested alongside modular monolith: apply entity factory pattern (`engineering-ddd`) within `entities/`. Repository interfaces go in `repositories/` as an interface + implementation in the same module.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
