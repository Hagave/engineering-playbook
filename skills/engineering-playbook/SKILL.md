---
name: engineering-playbook
description: "Language-agnostic core engineering governance across TypeScript, Java, Go, Rust, C#, Kotlin, Ruby — invoke automatically at the start of any conversation involving software development, code writing, code review, architecture, or technical implementation. Also activates when the user says activate playbook. Loads non-negotiable rules (no duplication, no hallucination, scope discipline) and the skill routing table. Framework- and language-specific opt-in skills are loaded only when the stack dictates. Active by default unless /playbook-off was invoked this session."
version: 4.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Engineering Playbook

These rules apply for the entire session, on every response, unconditionally. They do not reset between turns.

---

## Session execution protocol

These rules govern how skills are used in every session. They apply on every response, unconditionally.

**One active skill at a time.** Load a skill only when its step begins. Loaded skills remain active for the entire session — there is no close operation. Never pre-load all skills upfront.

**Declare before acting.** Before executing any step under a loaded skill, write the header:
```
### Step N: [step description] | Skill: [skill-name]
```
This re-anchors the AI to the active skill and makes progress visible.

**Declare completion with evidence.** After each step, write the result:
```
Step N complete: [what was produced or what command output confirmed it]
```
Never proceed to the next step without this declaration. Vague completions ("done") are not accepted — cite the artifact name, the output line, or the check that passed.

**No recall from memory.** If a rule, pattern, or config value is needed, read it from the active skill or from a file in the repository. Never act on what a skill "probably says" without loading it. If uncertain, load the skill and read the relevant section before acting.

**Restate the session goal before each new response** when inside a multi-step skill execution. One sentence: what the user asked for and which step comes next.

---

## Non-negotiable rules

**No duplication.** Before creating any artifact, verify nothing equivalent exists. Follow the reuse protocol in `engineering-code-economy`. Creating a duplicate when a reusable artifact exists is a failure.

**No dead code.** Imports, variables, functions, or types that your changes made unused must be removed. Pre-existing dead code is mentioned, not fixed.

**No hallucination.** Never assume a pattern, convention, or behavior exists in this project based on general knowledge or patterns from other projects. Read the actual code. Every decision traces to: (a) an explicit requirement from the task, (b) code read directly in this repository, or (c) a playbook rule. Anything else is a hypothesis — mark it explicitly and ask before acting on it.

**No hardcoded secrets.** API keys, tokens, credentials, and passwords never appear in source code. They come from environment variables validated at startup.

**No N+1.** A database or external service call inside a loop is forbidden without explicit justification. Batch or join instead.

**No unbounded queries.** Never fetch all rows without pagination or a documented hard cap enforced in code.

**Stop and ask when ambiguous.** Never silently pick one interpretation of an ambiguous requirement. List all open questions in a single message and wait for answers before writing any code.

**No ad-hoc bug investigation.** When the user reports bugs or strange behavior in an existing project, do not read files manually and produce a bug list. Route to `engineering-refactor` — bugs in a dirty codebase cannot be reliably scoped without purging dead code and establishing a tooling baseline first. Ad-hoc investigation produces incomplete, noise-inflated results.

**Scope discipline.** Every changed line must trace directly to the user's request. Do not improve adjacent code, refactor unrelated code, or add unrequested features.

---

## Registry protocol

Before creating any artifact:
1. Check if the project has a `registries/` or `.claude/registries/` directory.
2. If it exists, read the relevant registry file for the artifact type.
3. If an equivalent already exists, reuse or extend it instead of creating a new one.

After creating, modifying, or removing any artifact:
- If registries exist: update the corresponding registry entry immediately.
- If registries do not exist: skip the registry step entirely.

**Never create registry files.** Registry setup is the user's decision. Do not create `registries/`, `.claude/`, or any project-level governance file unless the user explicitly requests it.

---

## What is always active vs. what requires configuration

**Always active (all sessions, all projects):**
The non-negotiable rules above and the registry protocol apply unconditionally to every session, every project, every response. They require no setup.

**Requires explicit configuration per session per project:**
Framework choice, architecture, database, ORM, message broker, cache, Docker, DDD, patterns — these are project-specific and must be configured at the start of each session via `/engineering-playbook` (new project) or `/engineering-refactor` (legacy project).

See the Session configuration block in `playbook-config` for session scope and carry-over rules.

---

## Skill routing

Load specific skills when the task requires them. Use the Skill tool to invoke them on demand. Load only what the current task requires — never pre-load all skills.

The playbook core is language-agnostic. Framework, language, and driver opt-in skills are loaded only when the stack dictates.

### Core governance

| When task involves | Invoke |
|---|---|
| Any code implementation | `engineering-governance` |
| Creating or reusing artifacts | `engineering-code-economy` |
| Architecture decision or layer boundaries | `engineering-architecture` |
| SOLID / naming / design patterns needed | `engineering-design-patterns` |
| DDD present or requested | `engineering-ddd` |
| Shared state, distributed ops, idempotency | `engineering-concurrency` |
| Exception paths, retry, external calls | `engineering-error-handling` |
| Data access, loops, bulk operations | `engineering-performance` |
| Auth, input handling, secrets, external data | `engineering-security` |
| Writing or reviewing tests | `engineering-testing` |
| Docker setup requested or present | `engineering-docker` |
| Implementation task | `engineering-implement` |
| New project scaffolding | `engineering-scaffold` |
| Refactoring existing code | `engineering-refactor` |
| Bug investigation or bug fixing in an existing codebase | `engineering-refactor` |
| Purge dead code during a refactor | `engineering-refactor-purge` |
| Establish the linting/tooling baseline during a refactor | `engineering-refactor-tooling` |
| Hexagonal architecture | `engineering-arch-hexagonal` |
| Modular monolith architecture | `engineering-arch-modular` |
| MVC architecture | `engineering-arch-mvc` |
| Code review | `engineering-code-review` |
| User asks for session status, settings or config / `/playbook-config` | `playbook-config` |
| User suspends rules | `playbook-off` |
| User re-activates rules | `playbook-on` |
| Extract learnings from past sessions (Learn in `/playbook-config`) | `engineering-learn` |

### Framework opt-in

| When task involves | Invoke |
|---|---|
| NestJS | `engineering-nestjs` |
| NestJS HTTP security (helmet, CORS, throttler) | `engineering-nestjs-security` |
| NestJS structured logging (nestjs-pino) | `engineering-nestjs-logger` |
| NestJS interceptors or exception filter | `engineering-nestjs-interceptors` |

### Language opt-in

Load only when the project's implementation language matches the row. The table is alphabetical — no language is privileged over another.

| When task involves | Invoke |
|---|---|
| C# code | `engineering-csharp` |
| Go code | `engineering-go` |
| Java code | `engineering-java` |
| Kotlin code | `engineering-kotlin` |
| Ruby code | `engineering-ruby` |
| Rust code | `engineering-rust` |
| TypeScript code | `engineering-typescript` |
| TypeScript linting/formatting stack (Biome, typescript-eslint) | `engineering-linting` |

### Driver opt-in

| When task involves | Invoke |
|---|---|
| Kafka integration | `engineering-kafka` |
| RabbitMQ integration | `engineering-rabbitmq` |
| Prisma present in project | `engineering-prisma` |

---

## Model routing

Two roles operate in every session. Do not hardcode model IDs — use the `haiku` alias for extraction and let the session model handle everything else:

| Role | Model |
|---|---|
| Data extraction and read-only analysis — no decisions, no code | A fast extraction model (Haiku-class): pass `model: "haiku"` to the Agent tool |
| Implementation, reasoning, architecture, all decisions | The session's default model — never delegate decisions to a smaller model |

### Delegate to the extraction model when the task is purely extractive

A task qualifies for extraction if and only if it: reads existing content, returns structured output, and requires **no judgment about what to do with the result**. The extraction output comes back to the main session to inform next steps.

| Task | Qualifies |
|---|---|
| Read and summarize log files | Yes |
| Inspect bash command output | Yes |
| Check values in config or `.env` files | Yes |
| Grep or pattern search across files | Yes |
| Count, list, or categorize items in structured data | Yes |
| Extract errors or events from JSONL session files | Yes |
| Any code generation | No — session model only |
| Architecture or design decision | No — session model only |
| Debugging requiring multi-step reasoning | No — session model only |
| Any task where output informs what to implement | No — session model only |

### How to structure the extraction subagent call

Hand over everything upfront — file paths, exact content, and the precise question. The extraction agent does not search for context; the main session provides it.

```
Agent(
  model: "haiku",
  description: "extract [specific thing] from [specific source]",
  prompt: "
    Source: [exact file path or inline content]

    Task: [single, specific extraction question]
    Output format: [list / table / count / key-value pairs — be explicit]

    Do not interpret, decide, or suggest. Extract only.
  "
)
```

**If the extraction task requires any judgment about what to look for, keep it in the session model.** A misclassified task that a small model handles poorly costs more than the session model doing it once.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
