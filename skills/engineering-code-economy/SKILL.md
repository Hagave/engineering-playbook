---
name: engineering-code-economy
description: "Rules for avoiding duplication and keeping changes minimal. Use when creating any new artifact (service, utility, class, function, DTO, interface), when asked to add a feature, or when editing existing code. Enforces reuse-first protocol and surgical change discipline."
version: 2.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Code Economy

## Reuse protocol

Before creating any artifact, execute this sequence in order:

1. **Check registries** — does an equivalent already exist?
2. **Grep the codebase** — search for the concept if the registry is absent or uncertain.
3. **Evaluate extension** — can the existing artifact be extended to cover the requirement without compromising its current design?
4. **Evaluate composition** — can two existing artifacts be composed to meet the requirement?
5. **Create new** — only if steps 1–4 yield nothing viable.

Creating a new artifact when a reusable one exists is a failure.

Applies to: helpers, formatters, validators, parsers, mappers, services, repositories, interfaces, adapters, integrations, DTOs, value objects, entities, exception types, utility functions, configuration modules.

Do not force reuse if the existing artifact has a different responsibility or would require compromising its design to accommodate the new requirement. In that case: document why a new artifact was created and register it immediately.

---

## Minimum code mandate

Write the minimum code that solves the stated problem. Nothing more.

- No features beyond what was asked.
- No abstractions for single-use code.
- No flexibility or configurability that was not requested.
- No error handling for scenarios that cannot happen — trust framework and internal guarantees.
- No design for hypothetical future requirements. Three similar lines beat a premature abstraction.

**Carve-out — playbook-mandated patterns are never over-engineering:** security headers and practices (e.g., Helmet, CORS, HSTS), idempotency keys, rate limiting (see `engineering-concurrency`), retry, circuit breaker (see `engineering-error-handling`), domain exception hierarchy, and distributed system patterns mandated by the playbook are baseline requirements. Never skip them citing "minimum code" — the mandate overrides this rule.

Self-check before delivering: "Would a senior engineer call this overcomplicated?" If yes, rewrite.

If the same result can be achieved in significantly fewer lines without sacrificing clarity, rewrite it.

---

## Surgical changes

Touch only what the task requires. Clean up only what your own changes broke.

**When editing existing code:**
- Do not improve adjacent code, comments, or formatting that was not part of the task.
- Do not refactor things that are not broken.
- Match the existing style of the file, even if you would do it differently.
- If you notice unrelated dead code or an existing problem, mention it — do not fix it.

**Orphan cleanup rule:**
- Imports, variables, or functions that YOUR changes made unused → remove them.
- Pre-existing dead code or unused symbols → mention, do not delete.

If a line cannot be justified by the task, undo it.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
