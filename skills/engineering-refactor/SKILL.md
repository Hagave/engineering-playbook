---
name: engineering-refactor
description: "Refactoring and bug-fixing session for existing TypeScript/NestJS codebases. Invoke when the user wants to improve, restructure, or clean up existing code — including: 'refatora esse projeto', 'refactor this', 'clean up the code', 'improve code quality', 'fix the architecture', 'apply patterns to the existing code'. Also invoke when the user reports bugs or strange behavior in an existing project — 'existe alguns bugs', 'tem bugs', 'comportamento estranho', 'something is broken', 'there are bugs', 'fix this bug', 'I found a bug' — because bugs in a dirty codebase cannot be reliably scoped without first purging dead code and establishing a tooling baseline. Do NOT invoke for new project scaffolding. Executes in phases: purge → tooling → structure → bug fix."
version: 6.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Refactoring Session Orchestrator

Phases run in fixed order. Each phase is a separate skill loaded JIT, executed to completion, then closed. Never hold two phase skills in context simultaneously.

---

## Step 0 — Reconnaissance

Use bash commands for enumeration and grep for pattern detection. Read files only for config. Do not read source files individually during reconnaissance — use grep to extract what you need.

**Run these commands in sequence:**

```bash
# 1. Count and list all source files
find src -type f | wc -l
find src -type d | sort

# 2. Config files (read each; use the project's actual files)
cat <package-manifest>     # package.json, go.mod, Cargo.toml, etc.
cat <language-config>      # tsconfig.json, etc.
cat <linter-config>        # biome.json, eslint.config.*, etc.
cat docker-compose.yml 2>/dev/null

# 3. Architecture signals
find src -type f | grep -E '(entity|value-object|domain-error|use-case|repository|controller|module)' | head -30

# 4. Quality gaps (patterns below are TypeScript examples)
grep -rn ': any' src --include='*.ts' | wc -l
grep -rn 'as any' src --include='*.ts' | wc -l
grep -rn '// ' src --include='*.ts' | grep -v '///' | wc -l

# 5. Import style (relative vs alias) — TypeScript example
grep -rn "from '\.\." src --include='*.ts' | wc -l

# 6. Dead code indicators — TypeScript example
grep -rn 'console\.log' src --include='*.ts' | wc -l
```

If TypeScript, extract the `paths` alias map from `tsconfig.json`. In other languages, record the equivalent module alias configuration. All phase skills need it.

Build the detection table. Mark each signal: ✓ confirmed, ? ambiguous, ✗ absent.

| Signal | Detected | Assessment |
|---|---|---|
| Framework | NestJS / Node / other | version, HTTP provider |
| Architecture | Hexagonal / Modular / MVC / flat | folder names, layer separation |
| DDD | yes / partial / no | entity props pattern, value objects, domain errors |
| ORM | Prisma / TypeORM / Drizzle / none | |
| Message broker | Kafka / RabbitMQ / none | |
| Cache | Redis / in-memory / none | |
| Language strictness | strict / partial / off | list every missing flag |
| Linter/formatter | Biome+ESLint / language standard / none | type-aware and unused-code rules set to error? |
| Test runner | Vitest / Jest / language standard / none | coverage configured? |
| Logger | structured logger / built-in / none | structured JSON? |
| HTTP security | helmet + throttler / partial / none | what is missing |
| Interceptors | list present | trace-id, response-transform, exception-filter |
| Docker | multi-stage / single-stage / none | |
| Package manager | npm / pnpm / yarn / bun | from lockfile — flag dual lockfiles |
| Path aliases | yes / no | list configured aliases or "none" |

---

## Step 1 — Configure target state

Show confirmed (✓) values for verification. Ask only about ✗ absent or ? ambiguous:

| Question | Options |
|---|---|
| Target architecture | Hexagonal / Modular / MVC |
| DDD target | yes / no |
| ORM (if absent or changing) | Prisma / TypeORM / Drizzle / none |
| Message broker | Kafka / RabbitMQ / none |
| Cache | Redis / in-memory / none |
| Logger | structured logger / keep existing |
| HTTP security | helmet + CORS + throttler / keep / none |
| Global interceptors | trace-id / response-transform / exception-filter |
| Docker | yes / no |
| Preserve public API contracts | yes / no |
| Incremental or full rewrite | incremental / full |
| Package manager | resolve if ambiguous |

**Tooling (always flag, regardless of ✓):** In TypeScript projects, if Biome or ESLint is present, tell the user Phase 1 will audit and strengthen it — not preserve it. If absent, recommend Biome with the reason: single-pass lint + format, catches `any` and floating promises that manual review misses.

**Never ask the user to enumerate bugs or list specific issues.** Bug motivation is context, not input. Bugs are Phase 2+ work items, identified after the codebase is clean.

---

## Step 2 — Confirm and show execution plan

Display session config via the Session configuration block in `playbook-config`.

Then show the fixed execution plan:

```
Execution plan (fixed order):

Phase 0 — Purge  [engineering-refactor-purge]
  Remove unused imports, variables, functions, types.
  Remove commented-out code blocks.
  Delete orphan files (verified by grep before deletion).
  Rule: nothing is restructured or renamed — only deleted.

Phase 1 — Tooling baseline  [engineering-refactor-tooling]
  Audit existing linter config → apply delta to meet standard.
  Enable strict language/type flags.
  Configure path aliases if absent.
  Run linter → produce full violation list (Phase 2 backlog).

Phase 2+ — Structural  [JIT per sub-phase]
  Architecture → DDD → ORM → NestJS → Security → Logger → Interceptors → Docker → Tests
  Bugs reported by the user are addressed here, after codebase is clean.

Confirm to begin Phase 0?
```

Do not touch any file before confirmation.

---

## Step 3 — Phase execution

After confirmation, load the phase skill and execute it. **One sub-phase at a time — never chain.** When a sub-phase completes:

1. Write the full handoff block (Step 4 format — no abbreviation).
2. Post the "Reply continue" prompt.
3. Stop. Do not load or describe the next sub-phase.
4. Wait for the user to reply, unless the request delegated going on (Decision ownership in `engineering-governance`).
5. Only after the user replies: load the next skill. Context resets are not part of the phase protocol — the context guard decides when one is needed (see `engineering-governance` → Context management).

**Chaining sub-phases without stopping is a protocol violation.** Writing "Next: Phase 2c, 2d, 2e" and continuing is the wrong behavior — it skips the user checkpoint and the handoff that records state. Treat the boundary as a hard stop.

**Phase sequence:**

| Phase | Load skill | Condition |
|---|---|---|
| 0 — Purge | `engineering-refactor-purge` | always, first |
| 1 — Tooling | `engineering-refactor-tooling` | always, second |
| 2 — Architecture | `engineering-arch-hexagonal` / `engineering-arch-modular` / `engineering-arch-mvc` | always |
| 2 — DDD | `engineering-ddd` | DDD: yes |
| 2 — ORM | `engineering-prisma` | Prisma |
| 2 — Message broker | `engineering-kafka` / `engineering-rabbitmq` | if chosen |
| 2 — NestJS | `engineering-nestjs` | NestJS |
| 2 — Security | `engineering-nestjs-security` | if selected |
| 2 — Logger | `engineering-nestjs-logger` | nestjs-pino |
| 2 — Interceptors | `engineering-nestjs-interceptors` | if selected |
| 2 — Docker | `engineering-docker` | Docker: yes |
| 2 — Tests | `engineering-testing` | test runner configured |

---

## Step 4 — Handoff block format

Write at the end of every sub-phase. When the context guard asks for a handoff during a refactor, this block is the content of the `[HANDOFF]` … `[/HANDOFF]` block:

```
## Session handoff — [Phase name] complete

Goal: [one-line restatement of what the user asked for]
Phase completed: [name]

### Session config (carry forward — no omissions)
Architecture:     
DDD:              
ORM:              
Message broker:   
Cache:            
Logger:           
HTTP security:    
Interceptors:     
Docker:           
Package manager:  
Test runner:      
Path aliases:     

### Completed (do not redo)
[bullet list of what was actually done]

### Violations pending
[full list or "none"]

### Next
Phase [N+1] — [name]: [brief description of first task]

---
**Reply "continue" to begin [Phase N+1 name], or ask questions before proceeding.**
```

Every value explicit. "Same as before" is not valid. Violations pending must be the complete list — it is the next phase's backlog.

The closing prompt ("Reply 'continue'...") is mandatory — it is the only thing that tells the user they need to take an action. Never end a phase with a description alone. The literal "continue" signal is intentional and specific to this phase gate — the broader spec-confirmation signals from `engineering-implement` do not apply here.

---

## Step 5 — Refactoring rules (active in all phases)

**Scope:** Refactor only what was agreed. Mention unrelated issues, do not fix them.
**Path aliases:** Every file touched in Phase 2+: convert relative imports to the configured alias. Non-negotiable unless the user explicitly declines.
**No feature additions:** Refactoring and feature work are separate commits.
**Incremental:** One file or module at a time. The language type-checker passes before moving to the next (TypeScript: `tsc --noEmit`).
**Registry:** After any rename, move, or delete, update registries per the registry protocol in `engineering-playbook`.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
