---
name: engineering-implement
description: "Step-by-step implementation workflow. Use when starting any implementation task: feature, fix, or new artifact. Guides through reading context, extracting requirements, resolving ambiguities, reuse search, implementing, validating, and updating registries."
version: 4.1.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Implementation Workflow

## Step 1 — Read context

Load only the registries relevant to the task:

- Creating or modifying a service? Read `registries/services.md`.
- Touching data access? Read `registries/repositories.md`.
- Modifying an event? Read `registries/events.md`.
- Modifying an existing artifact? Check its registry entry for impact classification and consumers.

Do not load registries that are not relevant to the task.

**New-project gate (mandatory, before Step 2):** if the user request is to **create, scaffold, bootstrap, or start a new backend project** — including any CRUD / API / REST / "make me a X in <language>" / "novo projeto em <language>" request, or a request on an empty / near-empty working directory — load `engineering-scaffold` NOW. Its mandatory pre-scaffold questions ARE the spec for new-project tasks; do not generate a separate checklist from this skill. The skill's table is the only valid pre-scaffold checklist (do not invent Persistence / Entity or other categories beyond the table). When the user has answered the questions, resume here at Step 8 (Validate) with the answers as the spec — skip Steps 2–7 of this skill.

**Spec source rule (non-negotiable):** the spec is **the user's answer to `engineering-scaffold`'s questions in THIS session** — nothing else. Prior session memory, agentmemory recall (`~/.codex/hooks/agentmemory/recall.mjs` output), the previous attempt's rollout summary, code patterns recalled from past runs, and any other cached context are **NOT the spec**. They may inform your reasoning about the working directory or project history, but they MUST NOT be used as the implementation template. Concretely:
- If `agentmemory/recall.mjs` surfaces a previous attempt like "CRUD Todo in-memory com node:http + tsx", treat that as a hint that the user has tried before — not as the spec for the current request. The current request is what the user typed NOW. If the current request fits a backend scaffold, you MUST present the questions regardless of what previous attempts did.
- If the user's current request is a new CRUD/API/scaffold and the working directory has artifacts from a previous attempt (e.g., stale `server.ts`, a package manifest without the selected framework), the spec is still the answered `engineering-scaffold` table — present the questions, ignore the stale artifacts (or note them to the user, but do not let them drive the implementation).
- Do NOT emit `[SPEC CONFIRMED]` before the user has answered the questions. Emitting `[SPEC CONFIRMED]` from prior memory or from your own inference is the exact failure mode this rule prevents.

Triggers that require the gate (non-exhaustive): "create / build / scaffold / start / bootstrap / make / generate / new" + a backend-shaped noun ("CRUD", "API", "REST", "service", "microservice", "backend", "server"), OR an empty working directory with a backend-shaped request. When in doubt, load `engineering-scaffold` — its description's MANDATORY trigger is the authority.

Triggers that DO NOT require the gate: editing / refactoring / bug-fixing / reviewing an existing codebase (route to `engineering-refactor` instead). Reading existing files for context is not a scaffold action.

## Step 2 — Extract requirements

List explicitly:
- What must be implemented (stated requirements from the task).
- What constraints apply (implicit requirements from the codebase, playbook skills, or registries).

Before proceeding to Step 3, confirm the mandatory companion skills defined in `engineering-governance` are loaded. The checklist in Step 6 must reflect their constraints.

## Step 3 — Settle who owns each decision

List everything that is unclear, missing, or has more than one valid interpretation, and give each item its owner per Decision ownership in `engineering-governance`: fixed, delegated or open.
Decide and record the delegated ones now, and get the open ones answered BEFORE the spec checklist is generated and BEFORE the spec token is emitted — never after.

If open decisions exist → go to Step 4.
If none → go to Step 5.

## Step 4 — Ask

Ask the open decisions as Decision ownership in `engineering-governance` says: all in a single message, then proceed on the answers.

## Step 5 — Reuse search

For every artifact you are about to create, execute the reuse protocol in `engineering-code-economy`, then document the reuse decision: what was found, and why it was reused, extended, or replaced. The result feeds the "(if not exists)" items in the Step 6 checklist.

## Step 6 — Generate implementation checklist

Before writing any code, produce a task-specific checklist and post it as the first item in the response. This checklist guides both user and Claude — nothing in it should be skipped silently.

The checklist opens with the obligations table from `engineering-governance` (Obligations): every state the task writes with its invariants and all its writers, and every input with its valid domain. Each proof in that table is a test item of the checklist.

**Ordering rule:** items must be listed in dependency order — the order you will actually implement them. An artifact that depends on another must come after it. No backtracking allowed: if item N depends on item M, M must appear before N in the list.

For hexagonal architecture, the canonical dependency order is:

1. Domain layer: entities, value objects, domain exceptions
2. Application layer: use case interfaces, use cases
3. Domain interfaces: repository/service contracts (IXRepository, IXService)
4. Infrastructure layer: repository implementations, external adapters
5. HTTP surface: DTOs (body, query, params), controller, route, Swagger decorators
6. Cross-cutting concerns triggered by this task: auth guard, rate limiting, idempotency
7. Tests: unit tests first (use cases), then integration/e2e (HTTP endpoints)
8. Registry: always last

Adapt the order for architectures other than hexagonal — the principle is the same: dependencies first.

Example for a "user registration" endpoint in hexagonal architecture:

```
- [ ] User — domain entity (if not exists)
- [ ] DuplicateEmailException — domain exception
- [ ] IUserRepository — domain interface (if not exists)
- [ ] RegisterUserUseCase — application use case
- [ ] PrismaUserRepository — infrastructure implementation (if not exists)
- [ ] RegisterUserDto — body, @IsEmail, @MaxLength
- [ ] POST /users — controller, @ApiCreatedResponse, @ApiBadRequestResponse, @ApiConflictResponse
- [ ] Unit test: RegisterUserUseCase (success, duplicate email)
- [ ] Integration test: POST /users (201, 400, 409, 429)
- [ ] Registry: add PrismaUserRepository, RegisterUserUseCase
```

Generate the actual checklist for the current task — never use this example verbatim. Mark items `[x]` as they are completed. Strike through (`~~item~~`) items that turn out to be unnecessary rather than removing them.

**Skill lifecycle for multi-layer tasks:**

When the checklist spans multiple layer groups (domain → application → infrastructure → HTTP), load the skill for the current group at the start of that group and do not pre-load skills for later groups. Do not plan context resets between layer groups — the context guard measures usage and decides when one is needed (see `engineering-governance` → Context management).

When the guard asks for a handoff during an implementation task, use this as the content of the `[HANDOFF]` … `[/HANDOFF]` block:

```
## Implementation handoff — [layer group] complete

Goal: [one-line restatement of what the user asked for]
Checklist items completed: [list the [ x ] items]
Checklist items remaining: [list the [ ] items in dependency order]
Active session config: architecture=[...], ORM=[...], DDD=[yes/no]
Next group: [name] — load skill [skill-name] and start with [first item]
```

## STOP — Spec confirmation required

Post the checklist from Step 6 and wait for explicit user confirmation before proceeding.
Accepted signals: "proceed", "go", "ok", "sim", "pode ir", or clear equivalent.
Any other response = treat as a correction or addition to the checklist. Revise and re-post.
Do not infer permission from silence or from the user describing the task further.
Do not write any code before this confirmation is received.

The request may already give that confirmation: when it delegates the decisions (Decision ownership in `engineering-governance`) and every checklist item is fixed or delegated, post the checklist with the decisions you took and proceed in the same turn. A delegation is permission the user stated, not one you inferred.

When confirmation is received, emit `[SPEC CONFIRMED]` in your response text before the first Write or Edit call of the task, and repeat it in the turn's final message — token semantics and hook enforcement are defined in the spec gate section of `engineering-governance`.

## Step 7 — Implement

**Scope discipline (non-negotiable, the Claude-Code-equivalent behavior contract):**

The spec — your Step 6 checklist or the answered `engineering-scaffold` table — defines EXACTLY what to implement. The implementation must:

1. **Implement ONLY the items the user explicitly enabled.** If `Message broker` = none, do NOT produce a Kafka/RabbitMQ producer, broker module, broker imports, or broker docker service. If `Cache` = none, do NOT produce a Redis client, cache module, or cache docker service. If `DDD?` = no, do NOT produce entity factories, props pattern, or domain service interfaces — ORM entities serve as the data model directly. If `HTTP security?` = no, NO security middleware, NO rate limiting, NO CSRF protection. If `Global interceptors?` = no, NO trace-id, NO response-transform, NO exception filter middleware. If `Docker?` = no, do NOT generate a Dockerfile or docker-compose.

2. **Do NOT add features not in the spec.** No idempotency middleware, no event publishing, no extra endpoints beyond CRUD, no extra layers beyond what the chosen architecture dictates. A CRUD request does NOT automatically justify Redis, Kafka, Throttler, exception filters, validators, or DDD patterns. Add them only if the user opted in via `engineering-scaffold`.

3. **Do NOT promote a `none` answer to `yes` based on the request sounding "production-ready".** "CRUD" with all yes/no defaults at `no` produces a minimal CRUD — nothing more. Do not second-guess the user.

4. **The chosen architecture's directory tree shows what files exist when FULLY built — it is NOT a checklist of files to create for a minimal CRUD.** A hexagonal project with `Message broker = none` and `Cache = none` does NOT need the full `infrastructure/broker/` and `infrastructure/cache/` trees. Implement the entities/use-cases/adapters needed for the user's request, not every directory the architecture skill references.

5. **Treat the answered `engineering-scaffold` table as the single source of truth for what to implement.** If the implementation conflicts with the table (e.g. user said `Message broker = none` but the code imports a Kafka client), the implementation is wrong — not the table.

If the implementation produces code outside the spec, the Stop hook's `[SPEC CONFIRMED]` gate will block the turn. This is not a model preference; it is a hard rule from `engineering-governance`.

**If this task involves creating a new project:** follow the initialization sequence in `engineering-scaffold` (CLI generators first, single dependency install, version verification, tooling, boilerplate cleanup) and the framework-specific scaffold skill corresponding to the user's chosen framework before writing any domain code.

| Framework / runtime | Scaffold skill |
|---|---|
| NestJS | `engineering-nestjs` |
| Django | future `engineering-django` (not shipped yet) |
| Other | none currently ships — use the chosen framework's own generator and this playbook's generic scaffold rules |

*(This path is now reached only when the user answered `engineering-scaffold`'s questions explicitly. The new-project gate at Step 1 routes the model to `engineering-scaffold` BEFORE this checklist is generated, so by the time the model reaches Step 7 here the stack questions have already been answered and the implementation order is fixed: chosen framework, chosen Architecture, chosen ORM, chosen database, etc. Do NOT generate a parallel checklist for a different framework than the one selected in scaffold.)*

**For all other tasks:**

Apply all skills loaded for this task.
Do not exceed the stated scope.

**Skill examples are illustrative, not prescriptive.** Code examples in skills demonstrate the pattern for a given artifact type. Never implement a method, field, exception class, or interface member shown in an example unless it has a direct consumer in the current task scope. A repository example that shows `findById`, `findByEmail`, and `save` does not mean "implement all three" — implement only the methods called by the use cases in scope.
Do not make unrequested changes to adjacent code.
Do not introduce abstractions not required by the task.

## Step 8 — Validate

Run the validation checklist from the `engineering-governance` skill.
All items must pass before proceeding to Step 9.
If a checklist item cannot pass, stop and report what is blocking.

## Step 9 — Update registries

Update registries per the registry protocol in `engineering-playbook`. For every entry touched, additionally:
- Verify impact classification is set correctly.
- Verify consumers are listed accurately.

This step is mandatory. Proceed to Step 10 only after it is complete.

## Step 10 — Respond

Summarize:
- What was implemented and why each decision was made.
- Which registries were updated and what changed.
- Any risks, caveats, or recommended follow-up items.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
