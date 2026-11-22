---
name: engineering-architecture
description: "Layer rules, forbidden patterns, and compatibility governance. Use for architecture decisions, when code crosses layer boundaries, when modifying a shared or public-contract artifact, or when asked about system design. Includes impact classification and breaking change strategy."
version: 2.1.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Architecture

## Layer rules

Four layers with explicit boundaries and one-way import direction:

```
presentation/ → application/ → domain/ ← infrastructure/
                                   ↑
                    common/ (importable by all, imports none)
```

Import direction is never inverted. `presentation/` never imports `infrastructure/` directly.

---

## Domain rules (`domain/`)

- Zero external dependencies — no framework, no ORM, no libraries. Native runtime utilities are not external dependencies and are permitted (Node example: `node:*` modules; nuance owned by `engineering-ddd`), except to read a value the logic decides on that varies between runs (below).
- Entities enforce their own invariants; violations throw domain exceptions co-located with the feature. Entity mechanics (private constructor, semantic static factories, value object rules) are owned by `engineering-ddd` — load it for the full pattern.
- Entities never reference DTOs, persistence models, HTTP types, or framework annotations.
- Repository interfaces live in `domain/repositories/` and return domain types only — never persistence models.
- A value the logic decides on that varies between runs (the current time, randomness that drives a branch, the environment, a remote answer) enters as an input: a parameter or a port, such as a clock. A value only stored or passed on (a generated id, an `updatedAt` stamp) may come from the native runtime. The same holds in the application layer. Proof and owner: Obligations in `engineering-governance`.

## Application layer rules (`application/`)

- Use cases orchestrate domain objects, repositories, and external ports.
- Use cases do not contain business rules — those belong in domain entities.
- Use cases do not call other use cases directly.
- Use case DTOs define internal input/output contracts — distinct from HTTP DTOs.
- Application services (`services/`) encapsulate pure logic reusable across use cases — no I/O.
- No HTTP types, framework annotations, or persistence types in this layer.

## Infrastructure layer rules (`infrastructure/`)

- Repository implementations wire to `domain/repositories/` interfaces via dependency injection (NestJS example: `provide:` in a module).
- Always map persistence models to domain types before returning from a repository. Never return a raw ORM model.
- External HTTP clients and SDKs live in `infrastructure/external/`. They translate external responses to domain types.
- Mappers in `infrastructure/mappers/` convert domain entities ↔ persistence/infra models.
- All infrastructure exceptions (DB errors, HTTP client errors) are caught and rethrown as domain exceptions at the boundary. (see `engineering-error-handling`)

## Presentation layer rules (`presentation/`)

- Controllers are thin: map HTTP request → call use case → return HTTP response.
- HTTP DTOs (`presentation/dtos/`) are distinct from use case DTOs. Never share them.
- Presentation mappers (`presentation/mappers/`) convert HTTP DTOs → use case inputs.
- Framework composition roots live in `presentation/modules/`. One composition root per feature, flat structure.
- Global concerns live in an app root; shared infrastructure providers are registered once (NestJS example: `app.module.ts` for global concerns, `shared.module.ts` for providers such as PrismaService, Logger, Redis).

## Common layer rules (`common/`)

- Cross-cutting utilities only — middleware, guards/filters/interceptors or framework equivalent, logger, pure helpers, and base exceptions when the active architecture places them here.
- No business logic. No imports from any other layer.
- **Exception placement is owned by the active arch-* skill** (`engineering-arch-hexagonal`, `engineering-arch-modular`, `engineering-arch-mvc`) — read the concrete path there. The base hierarchy itself (class names, HTTP mapping) is created once per project via `engineering-nestjs-interceptors`; feature-specific exceptions are always co-located with their entity.
- Never mix placements in the same project — follow the active arch-* skill and stay consistent.

---

## Folder creation rule

Create a folder only when its first file is added — never create empty placeholder directories upfront. Empty folders are repository noise and disappear under `git clean` (git does not track empty directories). This applies to every architecture; the directory trees in the arch-* skills are references for the finished project, not a command to `mkdir -p` every path on day one.

---

## SOLID

SOLID is enforced across all layers. See `engineering-design-patterns` for principles and language-neutral examples.

---

## Forbidden patterns

- Business logic in controllers, request handlers, or event consumers.
- Direct database or persistence calls from application or domain layers.
- Circular dependencies — resolve structurally; never use lazy references as a fix.
- God classes — single class owning multiple unrelated responsibilities.
- Anemic domain model — domain objects with no behavior, only data and getters.
- Leaking persistence models into domain or application layers.

---

## Violation responses

When the implementation requested would violate a layer rule, refuse and redirect. Do not implement the violation.

| Violation | Response |
|---|---|
| Business logic placed in a controller | Refuse. Extract to a use case. |
| Persistence/ORM call in application or domain layer | Refuse. Move to a repository implementation. |
| Persistence model type imported in a domain entity | Refuse. Define a plain domain type or interface. |
| Exception type defined outside the designated location | Refuse. Use an existing exception type or extend the base class in the correct location. |
| Relative `../../` import across layers | Refuse. Replace with the configured path alias. |
| Cross-layer import in the wrong direction | Refuse. Invert via interface and dependency injection. |

State the violation, the rule, and the correct path forward. Never implement a workaround that preserves the violation.

---

## Compatibility governance

### Impact classification

Every artifact must have one of these classifications declared in its registry entry.

| Level | Definition | Examples |
|---|---|---|
| **Internal** | Used only within its own module — change freely | private methods, internal classes |
| **Shared** | Exported and used by multiple internal modules | shared services, utility functions, internal interfaces |
| **Public Contract** | Consumed externally | REST controllers, Kafka/RabbitMQ events, public DTOs, SDKs |

### Before modifying any artifact

1. Read its registry entry to get its impact classification and consumer list.
2. Classify the change using the table below.
3. Choose a strategy before writing any code.

### Change classification

| Change | Classification |
|---|---|
| Internal refactor, optimization, private rename | Non-breaking |
| Adding optional field, new method, new endpoint | Non-breaking |
| Changing validation rules or business behavior | Potentially breaking — analyze each consumer |
| Removing field, changing type, renaming public symbol | Breaking |
| Changing event payload schema | Breaking |
| Removing event or route | Breaking |
| Changing HTTP status code or response structure | Breaking |

### Strategy for breaking changes on Public Contracts

Ask first: can this be solved without modifying the contract?

Priority order:
1. **Extension** — add without removing.
2. **Composition** — delegate to new implementation; keep existing interface.
3. **Adaptation** — wrap the new implementation behind the existing interface.
4. **Versioning** — V2 only when no compatible strategy is viable and consumers cannot tolerate the change.

Never create V2 automatically. A DTO change does not imply V2. A contract change does.

### Never

- Remove a field consumed by known consumers without a migration path.
- Change an event payload without coordinating all producers and consumers.
- Alter a route, method, or status code without checking all known clients.
- Modify a shared interface without verifying all implementations still satisfy it.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
