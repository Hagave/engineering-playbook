---
name: engineering-refactor-tooling
description: "Phase 1 of the engineering-refactor workflow. Loaded by the refactor orchestrator when Phase 1 begins. Establishes the strictest linting baseline: TypeScript strict flags, Biome + ESLint audit, path alias configuration. Produces the violation list that Phase 2 uses as its structural backlog."
version: 2.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Phase 1 — Tooling Baseline

Goal: establish the strictest configuration the project can support before touching structure. The violation list produced here is the complete Phase 2 backlog.

Load `engineering-typescript` and `engineering-linting` for the detailed standards reference.

---

## Checklist

**1. Strict language flags**

Open the language configuration (TypeScript: `tsconfig.json`) and enable the strict flag set defined in `engineering-typescript` for TypeScript projects (canonical list lives there — do not maintain a copy here).

Run the language type-checker (TypeScript: `tsc --noEmit`). Fix all new errors before proceeding to item 2.

**2. Linter audit**

If Biome or ESLint is already present:
- Load `engineering-linting`.
- Compare the existing config rule by rule against the standard.
- Produce a delta: every rule that is absent or weaker (e.g., `warn` instead of `error`, rule `off` when standard requires it).
- Present the delta to the user. Apply after confirmation.
- Do not preserve a non-conformant config.

If absent:
- In TypeScript projects, install Biome + typescript-eslint per `engineering-linting` in full. In other languages, install the language's standard linter/formatter.

The critical rules that must be `error` (not `warn`, not `off`) are defined in `engineering-linting` — audit against that list, not a copy maintained here.

**3. Path aliases**

If module alias mappings are absent (TypeScript: `paths` in `tsconfig.json`):
- Define aliases matching the project structure: `@domain`, `@application`, `@infrastructure`, `@shared` (or equivalent detected during Step 0).
- All Phase 2 file edits depend on this being in place.

If aliases exist but source files use relative imports:
- Document in the Phase 2 backlog: every file touched in Phase 2 must convert relative imports to the alias form.

**4. Run linters — collect violation list**

Run the configured linters (TypeScript example: `biome check src` and/or `eslint src`). Do not auto-fix. Collect every violation.

This list is the Phase 2 structural backlog. Every violation is a work item. Organize by file, not by rule.

**5. Test runner**

If no spec files exist and `--passWithNoTests` is active:
- Ask the user: establish test baseline before Phase 2, or defer tests to after structural work?
- Do not force-migrate without confirmation.

---

## Completion gate

- TypeScript: `tsc --noEmit` passes with strict flags enabled; other languages: the equivalent strict check passes.
- Linter runs without config errors.
- Full violation list documented.

---

## Handoff

Write the session handoff block using the format from `engineering-refactor` Step 4. The "Violations pending" section must contain the complete linter output organized by file — this is the Phase 2 backlog. Do not summarize or truncate it.

The handoff block must end with:
```
---
**Reply "continue" to begin Phase 2 — [first sub-phase name], or ask questions before proceeding.**
```

**Stop here.** Do not load any Phase 2 skill, do not describe Phase 2 steps, do not continue. Wait for the user's reply, unless the request delegated going on (Decision ownership in `engineering-governance`). Only after they reply: load the first Phase 2 skill per the sub-phase table in `engineering-refactor`.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
