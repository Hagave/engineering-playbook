<!-- engineering-playbook:start -->
# Engineering Playbook (Antigravity adapter)

This workspace is governed by the Engineering Playbook. The full rule set lives in `.agents/skills/engineering-*.md` — these files are the single source of truth. This section is the always-loaded core; load the detailed skill files on demand.

## Concept mapping (read first)

The skill files were authored for Claude Code. In Antigravity, apply this mapping:

- "Invoke the Skill tool with X" / "load skill X" → **read the file** `.agents/skills/X.md` completely before acting on that topic.
- "Enforced by the hook" / "the tool call will be denied" → there is **no hook enforcement in Antigravity**. Every gate described in the skills is a mandatory protocol rule: violating it is a task failure, not a suggestion.
- `/engineering-playbook`, `/playbook-off`, `/playbook-on`, `/playbook-config` → Antigravity workflows in `.agents/workflows/` (same slash commands).
- References to `CLAUDE.md` → this workspace's `AGENTS.md`.

## Settings

The playbook clone this workspace was installed from is `@@PLAYBOOK@@`.

- **At the start of every session**, run `python3 "@@PLAYBOOK@@/scripts/playbook_config.py" session --harness antigravity` and follow the text it prints: it is the user's prose level, which a hook injects in the other harnesses.
- **To change a setting**, the user asks for it or runs `/playbook-config`. In the `playbook-config` skill, `<playbook>` is that clone path, and every call passes `--harness antigravity`.
- Only the prose level acts here. Known-failure warnings and the budgets are read by hooks, and Antigravity has none.

## Language

Reply to the user in the language they write in, and keep that language after the context is cleared (/clear) or compacted: the English in this context is addressed to you, not a cue to switch the conversation to English. The playbook's menus and prompts follow the user's language too.

## Non-negotiable rules (always active)

- **No hallucination.** Every decision traces to: (a) the task's explicit requirements, (b) code read in this repository, or (c) a rule in a loaded skill file. Anything else is a hypothesis: state it, and act on it only when the request delegated that decision (record it then); otherwise ask before acting.
- **No duplication.** Before creating any artifact, verify nothing equivalent exists (protocol: `.agents/skills/engineering-code-economy.md`).
- **Spec gate.** For any implementation task: post the checklist from `.agents/skills/engineering-implement.md`, STOP, and wait for explicit confirmation ("proceed", "ok", "sim", "go") before writing a single line of code, unless the request already delegated those decisions to you (see Decision ownership below). Emit `[SPEC CONFIRMED]` in your response before the first file write of the task.
- **Governance first.** Before writing source code, read `.agents/skills/engineering-governance.md` and its five mandatory companion skills.
- **Validate before finishing.** Never end a task with unvalidated source changes — run typecheck, lint, and relevant tests, and report their real output.
- **Decision ownership.** Act only on the decisions you own. What the request, the code or a skill fixes is implemented as it stands, and changing it is the user's call; what the request delegates ("decide and record") you decide, record and carry on without asking; anything else is the user's, asked in a single message. Never pick an interpretation silently.
- **Obligations.** Before the first write, list what the change must keep true, each with its test: every stored state it writes, with that state's invariants enforced by every writer in the codebase in its atomic step (proof: the writers interleaved); every input it accepts, with its valid domain as an allowlist, including its relation to now and to the state it refers to (proof: each boundary, both sides); every piece of work over data, with a cost bounded by what it changes or returns (proof: a test above the page); everything it declares, with a consumer, and every value the logic decides on that varies between runs (time, randomness, environment) arriving as a parameter or port (proof: tests under a fixed clock).
- **Scope discipline.** Every changed line traces to the user's request. No adjacent improvements, no unrequested features.
- **Destructive commands** (recursive deletes, force push, hard reset, `DROP`/`TRUNCATE`/unfiltered `DELETE`) require explicit user confirmation — there is no hook to catch you here.

## Skill routing

Read the matching file from `.agents/skills/` when the task involves:

| Task involves | Read |
|---|---|
| Any code implementation | `engineering-governance.md` |
| New implementation task (feature/fix) | `engineering-implement.md` |
| Creating or reusing artifacts | `engineering-code-economy.md` |
| Architecture / layer boundaries | `engineering-architecture.md` + active arch skill (`engineering-arch-hexagonal.md` / `-modular.md` / `-mvc.md`) |
| TypeScript code | `engineering-typescript.md` |
| Auth, input handling, secrets | `engineering-security.md` |
| Shared state, idempotency, rate limiting | `engineering-concurrency.md` |
| Exception paths, retry, external calls | `engineering-error-handling.md` |
| Data access, loops, bulk operations | `engineering-performance.md` |
| Tests | `engineering-testing.md` |
| Code review | `engineering-code-review.md` |
| Refactoring or bug investigation | `engineering-refactor.md` |
| New project scaffolding | `engineering-scaffold.md` |
| NestJS / Kafka / RabbitMQ / Prisma / Docker / DDD | the matching `engineering-*.md` |

Load only what the current task requires — never all files at once.

## Model routing (proportional tiers)

Antigravity exposes multiple models from different providers. Route by **capability tier relative to what is available**, never by provider name:

| Tier | Role | Examples (any provider) |
|---|---|---|
| **Frontier tier** — the strongest reasoning model available in the session | All decisions: implementation, architecture, debugging, review, anything whose output determines what gets built | Gemini 3 Pro (thinking), Claude Sonnet/Opus class, GPT frontier class |
| **Fast tier** — the cheapest/fastest model available | Pure extraction only: read and summarize logs, grep-like search results, config value checks, counting/categorizing structured data | Gemini Flash class, Claude Haiku class, small OSS models |

Rules:
- A task qualifies for the fast tier **only if** it reads existing content, returns structured output, and requires no judgment about what to do with the result. If any judgment is involved, keep it in the frontier tier — a misrouted task costs more than running it on the strong model once.
- When dispatching a subagent or secondary model, hand over everything upfront: exact paths, content, and the precise question. Fast-tier models do not search for context.
- If only one model is available, everything runs on it — the tier table degrades gracefully; the qualification criteria still decide what can be delegated when a second model appears.
- Never hardcode a model name in rules, workflows, or scripts — name the tier and let the session's model picker resolve it.
<!-- engineering-playbook:end -->
