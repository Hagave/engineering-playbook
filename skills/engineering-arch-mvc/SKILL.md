---
name: engineering-arch-mvc
description: "MVC architecture directory structure and dependency rules, language-agnostic. Load when the user chose MVC during project scaffolding, or when working on a small/medium project with a flat feature module structure."
version: 2.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# MVC Architecture

Simplest structure. Suitable for small APIs, internal tools, or projects with limited business complexity. One file per concern within a flat feature module.

```
src/
├── main.<ext>                    ← entry point
├── config/                       ← configuration and env validation
│
├── modules/
│   └── <feature>/
│       ├── <feature>.controller or handler ← routes + input parsing
│       ├── <feature>.service        ← business logic + orchestration
│       ├── <feature>.repository     ← data access (extract when service grows)
│       ├── <feature>.entity         ← ORM entity
│       ├── dto/
│       │   ├── <name>.request.dto
│       │   └── <name>.response.dto
│       └── <feature> module
│
├── database/
│   └── <orm-or-driver>/         ← persistence adapter chosen in scaffold
│       ├── <connection>.client
│       ├── schema or migrations
│       ├── migrations/
│       └── models or entities
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
controller → service → repository (optional)
                ↑
            common/ (available to all, imports none)
```

- Controller calls service. Service calls repository. No layer skipping.
- Services must not import other feature modules' services directly. If cross-module logic is needed, extract to a shared service under `common/` or use events (event-driven rationale owned by `engineering-design-patterns`).
- `common/` is importable by all modules. It imports nothing from `modules/`.

---

## Key conventions

- Feature modules are flat — no subdirectory nesting within a module.
- Repository is optional at first; extract it from the service when data access logic grows.
- When a service starts handling more than one distinct responsibility, split into two services — or migrate to Modular Monolith.
- The composition root at the root registers all feature modules and global concerns. No shared module needed unless infrastructure is shared across features.
- MVC does not use entity factories, props pattern, or domain service interfaces. If those patterns are requested, or if DDD was selected during scaffold, use Hexagonal + DDD instead — never apply DDD patterns inside MVC.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
