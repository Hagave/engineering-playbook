# Engineering Playbook

> Deterministic, evidence-based engineering governance for AI coding agents.
> Built for Claude Code. Codex CLI and Google Antigravity adapters are experimental.

[![License: MIT + Commons Clause](https://img.shields.io/badge/license-MIT%20%2B%20Commons%20Clause-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-4.0.1-blueviolet.svg)](.claude-plugin/plugin.json)
[![Harnesses](https://img.shields.io/badge/harnesses-Claude_Code%20%7C%20Codex%20CLI%20%7C%20Antigravity-2ea44f.svg)](#supported-harnesses)
[![Skills](https://img.shields.io/badge/skills-40-orange.svg)](skills/)
[![PRs target develop](https://img.shields.io/badge/PRs-target%20%60develop%60-blueviolet.svg)](#contributing)

The Engineering Playbook is a multi-harness plugin catalog that turns an AI
coding agent into a disciplined pair-programmer. It ships 40
`engineering-*` skills, lifecycle hooks, and a self-contained governance model
that prevents the most common failure modes of agentic coding: duplication,
hallucination, architectural drift, partial implementations, and unbounded
queries.

The playbook is built for and tested on Claude Code. The same skills also run
in Codex CLI and Google Antigravity through **experimental** adapters, sharing
one source of truth in [`skills/`](skills/) — see
[Supported harnesses](#supported-harnesses) for what that means.
The repository is source-available under MIT with the Commons Clause: free to
use, including at work, and to modify; not to be sold. See [License](#license).

---

## Why

| Problem | Solution |
| --- | --- |
| Duplication | Registry system + reuse protocol enforced before every artifact creation |
| Architectural violations | Layer rules with explicit violation responses — refuses to implement the wrong path |
| Hallucination | Every decision traces to: the task, code read in the repo, or a playbook rule — nothing else |
| Partial implementations | Hard spec gate in `engineering-implement` — no code written before explicit user sign-off on the checklist |
| Context loss | Non-negotiable rules anchored in every skill via identical prefix blocks — KV cache hits across sessions |
| Context overflow | At 70% the model finishes the task and writes a handoff; at 85% every tool call is denied. Either way the exit is `/clear` + "resume", never `/compact` |
| Destructive operations | Hook intercepts `rm -r*`, `git push --force`, `git reset --hard`, `DROP TABLE`, and similar — requires explicit user confirmation |
| Leaked secrets | Hook scans what a write adds, what a commit records and what a push publishes with gitleaks' rules — tokens with a known shape are denied |
| Repeated errors | `engineering-learn` mines real session logs and writes specific corrections back into the project |
| Token waste | Skills loaded on demand — only what the current task requires |

---

## Supported harnesses

| Harness | Status | Manifest | Install | Hooks |
| --- | --- | --- | --- | --- |
| **Claude Code** | Supported | [`.claude-plugin/plugin.json`](.claude-plugin/plugin.json) | `claude plugin install` from this repository's marketplace ([below](#claude-code)) | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PreCompact`, `Stop`, `SubagentStop` |
| **Codex CLI** | Experimental | [`.codex-plugin/plugin.json`](.codex-plugin/plugin.json) | `scripts/install-codex.sh` OR `codex plugin add` ([below](#codex-cli)) | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `Stop`, `SubagentStop`, `PreCompact`, `PostCompact` |
| **Google Antigravity** | Experimental | [`adapters/antigravity/`](adapters/antigravity/) | `adapters/antigravity/install.sh <project>` ([below](#google-antigravity)) | Optional git-hook enforcement (`--git-hooks`) |

**Claude Code is the target harness.** Every gate, the settings menu and the
learn loop are designed and verified there first. The Codex CLI and Google
Antigravity adapters are **experimental**: they reuse the same skills (and
Codex the same Python hooks, `hooks/playbook_core.py`), so they get rules and
fixes for free, but they cover fewer gates, are tested less, and may break when
their harness changes. Antigravity has no hook mechanism at all; its optional
git hooks are the only enforcement it gets.

All adapters share the same `skills/` directory. The plugin manifests
are the only fork points: each harness has its own manifest format and hook
contract, but the engineering rules are identical.

---

## Quick install

### Claude Code

```bash
claude plugin install engineering-playbook --marketplace Hagave/engineering-playbook
```

That one-step form needs Claude Code 2.1.292 or later. On any version, or
inside a session:

```
/plugin marketplace add Hagave/engineering-playbook
/plugin install engineering-playbook@engineering-playbook
```

No clone is needed. To receive new releases automatically, turn on
auto-update once: `/plugin` → Marketplaces → `engineering-playbook` →
Enable auto-update (it is off by default for marketplaces outside Anthropic's).
Without it, `claude plugin update engineering-playbook@engineering-playbook`
updates by hand. A release reaches users when its version changes in
`.claude-plugin/plugin.json`, which happens only on a merge into `main`.

### Codex CLI

```bash
# 1. Clone the repository
git clone https://github.com/Hagave/engineering-playbook.git ~/.engineering-playbook

# 2. Install via the marketplace (recommended)
~/.engineering-playbook/scripts/install-codex.sh --dev

# OR install directly through the Codex CLI:
codex plugin marketplace add /path/to/engineering-playbook
codex plugin add playbook@playbook
```

### Google Antigravity

```bash
# 1. Clone the repository (skip if already cloned)
git clone https://github.com/Hagave/engineering-playbook.git ~/.engineering-playbook

# 2. Run the installer against the target workspace
~/.engineering-playbook/adapters/antigravity/install.sh /path/to/your-project

# recommended — adds mechanical enforcement via git hooks:
~/.engineering-playbook/adapters/antigravity/install.sh --git-hooks --ci /path/to/your-project
```

---

## Detailed installation

### Claude Code

The Claude Code plugin lives in [`.claude-plugin/`](.claude-plugin/). The
repository is also its own marketplace (`.claude-plugin/marketplace.json`), so
`claude plugin install` copies it into Claude Code's plugin cache and updates
it from there. The plugin registers a single
`hooks.json` that wires `hooks/session_start.py` to `SessionStart`,
`hooks/context_guard.py` to `PreToolUse`, `PostToolUse`, `Stop` and
`SubagentStop`, `hooks/learn_guard.py` to `UserPromptSubmit` and `PostToolUse`,
`hooks/output_filter.py` to `PostToolUse` for Bash, and `hooks/compact_guard.py`
to `PreCompact`. The same file loads one interface module,
`hooks/config_panel.tsx` (the `/playbook-config` menu and gate toasts), which
holds no rules. Denials use the Claude Code hook contract
(`permissionDecision: "deny"`), so blocks are enforced at the harness level —
the model cannot bypass them. State is scoped per session (`session_id`), so
concurrent sessions in the same project never interfere; a subagent gets its
own state (`session_id` + `agent_id`) and is gated at `SubagentStop`, so its
writes never block the parent session's turn.

**Pin a version:** `claude plugin marketplace add Hagave/engineering-playbook#v4.0.1`.

**For a team:** commit the marketplace and the plugin to the project's
`.claude/settings.json`; each teammate still accepts the folder's trust dialog
and runs the install once (`--scope project`):

```json
{
  "extraKnownMarketplaces": {
    "engineering-playbook": {
      "source": { "source": "github", "repo": "Hagave/engineering-playbook" }
    }
  },
  "enabledPlugins": { "engineering-playbook@engineering-playbook": true }
}
```

**Developing the playbook:** load a clone instead, which replaces an installed
copy of the same name for that session:

```bash
git clone https://github.com/Hagave/engineering-playbook.git ~/.engineering-playbook
claude --plugin-dir ~/.engineering-playbook
```

**Auto-activate on every session** (optional — otherwise the playbook activates
when a session involves code, or when you say "activate playbook"):

```bash
echo '\nThe engineering-playbook plugin is active. Apply its governance rules to all engineering and coding tasks in this environment.' >> ~/.claude/CLAUDE.md
```

### Codex CLI

> **Experimental.** See [Supported harnesses](#supported-harnesses).

The Codex adapter wraps the original `engineering-*` skill catalog for
Codex CLI. It exposes the same skills as the Claude Code plugin and adds
lifecycle hooks that enforce the playbook governance model.

**Recommended (manual / dev workflow)** — use `scripts/install-codex.sh`
when you are iterating on the playbook from this repository and want every
edit to take effect on the next Codex run:

```bash
# Snapshot mode (default) — cache is a copy of the repo at install time;
# edits do NOT propagate. Re-run to refresh, or use --refresh.
./scripts/install-codex.sh

# Dev mode — replaces the cache's hooks/ and skills/ with symlinks to the
# repo, so edits take effect on the next Codex run without reinstalling.
./scripts/install-codex.sh --dev

# Custom plugin name (useful when running multiple variants side by side)
./scripts/install-codex.sh --plugin playbook-dev

# Uninstall (use --plugin first if it was installed under a custom name)
./scripts/install-codex.sh --plugin playbook-dev --uninstall
```

The script always overwrites the installed `plugin.json` and `hooks.json`
from the repo, so all lifecycle events stay wired (`SessionStart`,
`UserPromptSubmit`, `PreToolUse`, `Stop`, `SubagentStop`, `PreCompact`, `PostCompact`). The
script also verifies the installed `hooks.json` and refuses to proceed if any
event is missing.

**Official (`codex plugin add`)** — Codex's plugin registry is the
production install path:

```bash
# 1. Register the marketplace (single SOURCE argument; the marketplace name
#    comes from .agents/plugins/marketplace.json's own "name" field).
codex plugin marketplace add /path/to/engineering-playbook

# 2. Install and enable the plugin.
codex plugin add playbook@playbook
```

On the first interactive invocation, Codex shows a hook review screen.
Choose **"Trust all and continue"** to persist the hook hashes.

For non-interactive automation, Codex silently skips untrusted hooks unless
the first run is done with:

```bash
codex exec --dangerously-bypass-hook-trust "prompt"
```

There is no native auto-trust config key in Codex 0.147.0. The bypass flag
does not persist trust; automation either needs the flag per invocation or a
one-time interactive trust step after hook hashes change.

**What you get** — All 40 skills are registered explicitly
in [`.codex-plugin/plugin.json`](.codex-plugin/plugin.json) (one path per
skill, matching `.claude-plugin/plugin.json`'s array), not a bare directory
string, which is not guaranteed to be expanded by every Codex version. The
`SessionStart` hook injects a curated subset of `engineering-playbook` and
`engineering-governance` rules into Codex's `additionalContext` budget
(`hooks.json` raises it to 6000 tokens). The `Stop` hook runs two
independent gates — spec gate and validation gate — that share the same
patterns with Claude Code via `hooks/playbook_core.py`.

For the full design notes, hooks contract, and configuration toggles, see
[`docs/adapters/codex.md`](docs/adapters/codex.md).

### Google Antigravity

> **Experimental.** See [Supported harnesses](#supported-harnesses).

The Antigravity adapter installs the playbook into a Google Antigravity
workspace. The skills in `skills/` remain the single source of truth — this
adapter transforms and copies them; it never forks their content.

```bash
./adapters/antigravity/install.sh /path/to/your-project

# with mechanical enforcement via git hooks (recommended):
./adapters/antigravity/install.sh --git-hooks --ci /path/to/your-project
```

What it produces in the target workspace:

| Artifact | Purpose |
| --- | --- |
| `AGENTS.md` (marker block) | Always-loaded core: non-negotiables, concept mapping, skill routing table, proportional model routing. Idempotent — re-running replaces the block, preserving anything else in the file. |
| `.agents/skills/*.md` | All 40 skills, YAML frontmatter stripped (Antigravity skills are plain markdown). |
| `.agents/workflows/*.md` | Slash commands: `/engineering-playbook`, `/playbook-on`, `/playbook-off`, `/playbook-config`. |
| `.git/hooks/commit-msg` + `pre-commit` (with `--git-hooks`) | Mechanical enforcement independent of the IDE: blocks AI co-author attribution through the playbook's own check (`scripts/check_commit_message.py`; where git runs the hook without `python3` 3.10+ or the clone, the same rule, rendered into the hook at install time, still blocks); runs `tsc --noEmit` + lint before every commit. `git commit --no-verify` skips both once, and no environment variable does: a hook inherits the environment of the `git commit` the agent runs. Foreign hooks are never overwritten. |
| `.github/workflows/engineering-playbook.yml` (with `--ci`) | The same two checks on every pull request, where nothing local can skip them: AI attribution in the PR's commits or body, and `tsc --noEmit` + lint. The attribution rule is rendered from `hooks/playbook_core.py` at install time. Make its `policy` check required in branch protection. A foreign workflow with the same name is never overwritten. |

Re-run the installer after updating skills in this repository.

**Limitation:** Antigravity has no IDE hook mechanism, so the context guard,
spec gate, skill gate, and destructive-command blocking apply as mandatory
protocol rules rather than mechanical blocks. With `--git-hooks`, the
validation gate (typecheck + lint before every commit) and the AI-attribution
guard are enforced mechanically at the git level. Design decisions and full
limitations: [`adapters/antigravity/README.md`](adapters/antigravity/README.md).

---

## Reference

### Per-project setup

**1. Create a `CLAUDE.md`** in the project root with the project manifest:

```markdown
# Project Manifest

**Stack:** NestJS 11, TypeScript 5.7, PostgreSQL 17, Redis 7
**Package manager:** npm
**Test runner:** Vitest
**Dev command:** npm run start:dev
**Test command:** npm test
**Lint command:** npm run lint
**Build command:** npm run build
**Path aliases:** @application/*, @domain/*, @infrastructure/*, @common/*
**Architecture:** Hexagonal
**Active skills:** nestjs, kafka
```

Fill in only the fields that apply. The agent reads this at the start of each
session to avoid re-asking for project context.

**2. Create registries** (empty to start — the agent populates them as work
progresses):

```bash
mkdir registries
```

**3. Docker setup** — ask for it in words, which loads `engineering-docker`
(or let `engineering-nestjs` trigger it automatically at Step 13 during
scaffolding).
Never generate before `package.json` is finalized.

The skill generates four files in order:

| File | Purpose |
| --- | --- |
| `Dockerfile` | Multi-stage build: `base -> deps -> development -> builder -> production` |
| `docker-compose.yml` | App + infra services (PostgreSQL, Redis) with healthchecks and named volumes |
| `.dockerignore` | Excludes `node_modules`, `dist`, `.env`, test files, logs |
| `docker/entrypoint-development.sh` | Runs `npm install`, Prisma generate/migrate, then `start:dev` on container start |

**Development — start everything:**

```bash
docker compose up
```

The `app` service targets the `development` stage. The entrypoint script
handles dependency installation and migrations automatically on each start.
Source code is bind-mounted so hot-reload works without rebuilding the image.

**Production image:**

```bash
docker build --target production -t <project>:latest .
```

The `production` stage ships only `dist/` + production `node_modules` under a
dedicated non-root user (`nestjs:nodejs`). `dumb-init` is PID 1 so `SIGTERM`
reaches Node.js correctly on container stop.

### Session governance

The playbook ships five hook scripts, active whenever the plugin is loaded —
no configuration required. `hooks/session_start.py` runs at `SessionStart`
(including after a compaction, a `/clear`, and on resume) and announces that governance is
active, naming the skills the session must load before writing code; it stays
silent while `/playbook-off` is in effect. It injects a pointer,
not the rules themselves — unlike the Codex adapter, Claude Code has the Skill
tool and loads the rules on demand. `hooks/context_guard.py` is registered for
`PreToolUse` (before every tool call), `PostToolUse`, `Stop` (end of turn) and
`SubagentStop` (end of a subagent's run) and does the enforcing. Denials use the
Claude Code hook contract (`permissionDecision: "deny"`), so blocks are
enforced at the harness level — the model cannot bypass them. State is
scoped per session (`session_id`), so concurrent sessions in the same project
never interfere; subagents are scoped per `session_id` + `agent_id`.

`hooks/learn_guard.py` runs at `UserPromptSubmit` and `PostToolUse` and closes
the loop between a mistake and a rule. Every failed tool call is appended to a
session-scoped JSONL journal next to the state file — zero context cost, and
Learn (`/playbook-config`) reads it instead of re-mining raw transcripts. A user
message that rejects the previous turn, or the same call failing three times,
arms the **learn gate**: `Stop` is blocked until the model emits `[LEARNED]`
with the premise it got wrong and persists the rule in the project
`CLAUDE.md`. Capped at three blocks per session so a false positive costs one
turn, not a session.

`hooks/compact_guard.py` runs at `PreCompact` and writes the **carry-over
note** — the original request, files written, commands already run, skills
loaded, gates still armed, repo state — to disk before the transcript is
summarized away. `session_start.py` re-injects it on the `SessionStart` that
follows. The Stop hook refreshes the same note at every turn end above
`WARN_PCT`, so it also exists for the `/clear` the context guard recommends.
A `/clear` starts a new `session_id`, so for that path the note is stored under a
project key next to the model's own `[HANDOFF]` block, and the `SessionStart:clear`
that follows injects both once and deletes them.

The `PreToolUse` guard's **destructive-command block** (`rm -r`, `git reset
--hard`, force push, `git clean`, mass delete, destructive SQL) lifts for the
turn in which the user authorizes the operation in their own words, naming the
act — "pode apagar", "confirmo o reset", "go ahead and delete it". A general
approval ("pode seguir", "pode rodar os testes", "I confirm") is not consent.
The authorization is re-read from the user's current message on every call, so
it never outlives the turn that granted it, negations ("nunca apague isso") do
not count as consent, and the model cannot authorize itself. An `rm` of literal
absolute paths inside the session's own scratchpad needs no consent: it deletes
only what the session made there. Two false-positive sources were also removed: a heredoc body
written by `cat`, `tee` or a non-shell interpreter is data, not shell about to
run, and is no longer scanned; and the SQL patterns now require statement shape
(`TRUNCATE TABLE x;`) instead of matching the bare keyword anywhere in a
command, so documenting or grepping for one is no longer treated as executing
it.

The `PreToolUse` guard also enforces the **review gate**: `git push` is denied
until the current `HEAD` has been reviewed through `engineering-code-review`.
The review is pinned to the commit, so committing after a review re-arms the
gate. Only the user can waive it — in their own words ("pode pushar sem
review", "skip review") or with `PLAYBOOK_SKIP_REVIEW=1`. The model cannot
waive it for itself: the bypass is read from the user's message only. The gate
fails open outside a git repository and when nothing is ahead of upstream.
Blocker and Major findings from a review feed straight back into the learn loop:
they are journaled, and the learn gate holds the turn open until the defect
class behind them is written down as a rule.

**Language coverage.** The gates recognise source in every ecosystem the
playbook governs plus the common remainder — TypeScript/JavaScript, Python,
Ruby, PHP, Go, Java, Kotlin, Scala, Swift, Dart, Rust, C#, F#, Elixir, Erlang,
Clojure, Haskell, Lua, Zig, C/C++, Objective-C and shell. A language missing
from that set silently has no skill gate, no spec gate and no validation gate,
so `tests/unit/test_context_guard.py::TestLanguageCoverage` pins one source path
and one validation command per ecosystem; add a row there when a new
`engineering-<lang>` opt-in ships.

Auto-compaction itself is a harness setting, not a hook, and it already lands
where the guard wants it. Measured on a real session: compaction fired at
702,592 tokens on a 1M window (~70%) and brought the session back to 86,294 —
an 88% reduction. That trigger is the harness's own
`window × (1 - precomputeBufferFraction)`, resolved per model.

Do **not** try to move it with `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` in
`settings.json`. It was measured as inert: with the override set to `55`,
compaction still fired at ~70% rather than the 55% (550k) it asks for. An env
var that silently does nothing is worse than no setting at all, because it
reads as control that is not there. The guard's `WARN_PCT` (70%) is aligned
with the real trigger. The playbook never recommends `/compact`: its exit is
`/clear` plus the handoff (see Context limits). While the harness's
auto-compaction stays enabled, the PreCompact carry-over note still covers it.

**Output filter.** `hooks/output_filter.py` (PostToolUse, Bash only) shortens
the output of read-only commands before the model reads it. It groups
`grep`/`rg`/`git grep` matches under their file (capped per file, long lines
clipped), drops the link count, `total` and `.`/`..` from `ls -l` and states a
shared owner once, and turns plain `git log` into one line per commit with
the body capped. `cat` is never filtered: a source file must reach the model as
written. The command runs exactly as written. Only stdout is
reshaped, never stderr, and only when the result is at least 10% shorter. A
filtered output ends with a `[playbook:filter]` line; re-running the command
with `PLAYBOOK_RAW=1` in front returns it unfiltered, so nothing is saved to
disk. Outputs Claude Code already moved to a file (over 30,000 characters) and
the Read tool are left alone: Edit needs the exact text Read returned. Output
another tool already reshaped matches no rule and passes through unchanged.
The `/playbook-config` menu shows the session's
totals (`hooks/output_filter.py --report`), counted against what the model
would have read, not against raw output Claude Code had already cut.

### User preferences

Four behaviours are user-tunable. The plugin ships the defaults; each user's
choices are written to `~/.claude/hooks/.playbook-prefs.json` by
`scripts/playbook_config.py` — nothing is hardcoded to one machine, and nothing
personal travels with the plugin.

| `/playbook-config` entry | Preference | Default | Acts in | What it does |
|---|---|---|---|---|
| Prose level | `prose` | `mid` | Claude Code, Codex, Antigravity | Response verbosity budget, read at session start: applies from the next session. |
| Known-failure warnings | `watch` | `off` | Claude Code | Known-failure warnings before risky actions. |
| Context budget | `context_budget` | `off` | Claude Code | Context size past which the session is told to wrap up, hand off and `/clear`. |
| CLAUDE.md budget | `claude_md_budget` | `40000` | Claude Code | Characters the project `CLAUDE.md` may grow to before the turn end is blocked. |

In Claude Code the menu is numbered, above the prompt: type a number and Enter.
In Codex and Antigravity the `playbook-config` skill presents it (`/playbook-config`
in Antigravity), or the user just asks ("shorter answers"). Every path goes
through `scripts/playbook_config.py set --harness <harness>`, which validates
the value, says when it takes effect, and refuses a setting the harness has
nothing to read it with: a warning that does not block cannot be shown by
Codex's PreToolUse (it only allows or denies), the budgets need Claude Code's
Stop hook and token counts, and Antigravity has no hooks at all. Codex gets the
prose level from its SessionStart hook; Antigravity's `AGENTS.md` tells the
agent to run `playbook_config.py session --harness antigravity` at session
start.

**Prose** is a budget for what the response may spend tokens on, not a style.
Every level forbids the same filler — preamble, restating the request,
recapping steps the user just watched, flattery. `low` gives the result and
only decision-changing reasoning; `mid` adds the why and the trade-offs;
`high` adds alternatives, failure modes and grounded citations (`file:line`,
command output, documentation). Output tokens only — input context is the
context guard's job.

**Known-failure watch** is retrieval, not supervision. A background agent
cannot interrupt or inject context into the main one, so a literal watcher is
not implementable in this harness and would arrive a step late even if it were.
Instead, the hook that already runs before every tool call looks up two evidence
sources against what is about to be touched: the failure journal (repeated
failures, user corrections, review findings) and the rules the learn gate wrote
into the project `CLAUDE.md`. It warns, never blocks, and each rule fires once
per session — a warning repeated every turn stops being read. Deduplication
uses a stable digest rather than `hash()`, which Python randomises per process.

**CLAUDE.md budget** exists because the file is not read once: it is re-sent to
every session and to every subagent, and the learn gate appends to it. The
`Stop` hook blocks a turn that **grew** a `CLAUDE.md` past the budget, and asks
for one line per rule with the incident moved to a file `CLAUDE.md` does not
`@`-import. It judges growth, not size — a project that arrives over budget stays
editable, and shrinking the file never trips the gate; `SessionStart` mentions an
already-oversized file once instead. Only `Write`/`Edit` are tracked, so a
`CLAUDE.md` rewritten through the shell is not seen.

#### Cheap delegation

`agents/runner.md` ships a subagent (`engineering-playbook:runner`) pinned to the
cheapest model, without the project `CLAUDE.md` (`omitClaudeMd`) and without
write tools. It is for commands whose output only needs reporting back — test
suites, builds, linters, log scans, waiting on a long job — so that neither the
raw output nor the wait is paid at the main model's price. It runs the exact
command it is given and quotes failures verbatim; it does not diagnose, and since
it has not read the project rules the caller must state anything that matters.
A validation command it starts after the caller's last code write counts for the
caller's Stop validation gate; one already running when the caller wrote does not.

Two costs people reach for a hook to fix are already harness settings, and the
plugin deliberately does not reimplement them: `CLAUDE_CODE_SUBAGENT_MODEL` sets
the model for every subagent whose call and definition name none, and the
built-in `Explore` and `Plan` agents already skip `CLAUDE.md`.

**Codex parity:** the learn loop, the carry-over note, the review gate and the
destructive-command authorization bypass are Claude Code only for now. The Codex
guard shares `check_destructive`, so it does get the false-positive fixes
(heredoc bodies, SQL statement shape) — but not the bypass, because scoping an
authorization to the turn it was given in requires locating the latest user
message, and Codex's transcript schema for that is not confirmed. A session-wide
scan would be the easy version and the wrong one: an authorization from an hour
earlier would clear a block on something the user never saw. Until the schema is
confirmed, Codex keeps the stricter behaviour of always blocking. The shared primitives live in `hooks/playbook_core.py`
(`CORRECTION_RE`, `REVIEW_BYPASS_RE`, `GIT_PUSH_RE`, the journal helpers), but
the Codex adapter's transcript and tool-result schemas differ and are not
confirmed for these events, so the adapter guards have not been wired to them.

#### Context limits

| Threshold | Behavior |
| --- | --- |
| >= 70% | Warning injected — no new skills or new work; finish the task in progress, write the handoff, tell the user to `/clear` and ask to resume |
| >= 85% | Hard block — all tool calls denied; write the handoff immediately (including code not yet on disk) and tell the user to `/clear` |

Those percentages protect the window. They do not protect the bill: every call
re-reads the whole context, so cost grows with context size times call count, and
on a 1M window the first warning arrives at 700k tokens. `context_budget` is the
cost-side threshold — an absolute token count (`250k`), off by default. Past it,
the model is told once per multiple of the budget (1x, 2x, ...) to finish the step
in progress, write the handoff and ask for `/clear`. It never blocks a tool call,
and subagents are exempt. The trade is real: a `/clear` keeps only what the
handoff and the carry-over hold.

The handoff is a block in the model's response text between a `[HANDOFF]`
line and a `[/HANDOFF]` line — goal, what is done, next step, agreed decisions, files, gates still owed. It
is text rather than a file write so it still gets out when every tool call is
denied. The Stop hook saves it; after `/clear`, `session_start.py` injects it
together with the automatic carry-over, and the user asks to resume.

`/compact` is never recommended, by the hook or by any skill: a compaction does
not reliably land below the threshold, and one that falls short leaves the
guard blocking. The
lock does still release if usage drops below 60% by any means.

The window is resolved **per turn from the model actually in use**, not a
global constant. The guard reads the turn's model from the transcript and
maps it to its window: Opus 4.6+, Sonnet 4.6+ and Fable 5 have a 1,000,000-token
window as standard; Haiku and older models have 200,000, and older Opus/Sonnet
models reach 1,000,000 only under the 1M beta (`[1m]`). The `[1m]` marker is read
from the `model` field in your settings cascade, since the transcript records
the base model id with the beta suffix stripped. Switching models mid-session
(`/model`) re-resolves the window on the next turn — Haiku never inherits a 1M
window even under a 1M alias.

As a safety net, if observed usage ever exceeds the resolved limit (an
unknown model, an undetected beta, or a resumed long session), the limit
snaps up to the next tier so the guard never hard-blocks on tokens the
window has already held.

To pin a fixed window regardless of model — an explicit override — set the
env var in `~/.claude/settings.json`:

```json
{ "env": { "PLAYBOOK_CONTEXT_LIMIT": "1000000" } }
```

#### Spec gate (Stop)

Writing a source file in any governed language (see *Language coverage*) via
Write/Edit — or via shell (`sed -i`, `tee`, redirection, `cp`/`mv` into the
project) — requires the
model to emit `[SPEC CONFIRMED]` in its response text after the latest user
message. This enforces the STOP step of `engineering-implement`: no code
before explicit user sign-off, re-confirmed per task.

The check runs at turn end (Stop), not before the write. The token is
assistant text emitted in the same turn as the write, and Claude Code does
not flush a turn's message to the transcript until its tool calls resolve —
so a pre-write check cannot see a token the model just emitted and would
deny even when the spec was confirmed (this was the dominant false-positive
in real sessions). Deferring to Stop, where the text is flushed, blocks a
turn that wrote code without confirming while never firing on the same-turn
flush lag.

Text written between tool calls is not always recorded in the transcript at
all (measured: 7 of 15 such tokens missing in one session), so the skills ask
for the token in the turn's final message as well, which is always recorded
before Stop runs.

#### Skill gate

Writing a source file is denied until `engineering-governance` has been
loaded via the Skill tool in the session. This converts the most common
failure mode observed in POC validation (skills never loaded, causing
non-conformant scaffolds) into a mechanical block.

#### Validation gate (Stop)

Ending the turn is blocked once if source files were written and no
validation command (tsc, lint, tests) ran afterwards. At Stop the spec gate
is checked first, then the validation gate; `stop_hook_active` breaks any
loop after a single block.

Both the spec gate and skill gate are suspended while
`/playbook-off` is active. Context and safety checks always run.

#### Destructive operations

The hook denies `Bash` commands matching these patterns and requires explicit
user confirmation before proceeding:

- `rm -r*` / `rm --recursive` (any recursive remove)
- `git push --force` / `git push -f` / force-push refspec (`git push origin +main`)
- `git reset --hard`, forced branch deletion (`-D`, `-df`, `--delete --force`), `git clean -f*`
- `git checkout --` / `git restore <path>` (except `--staged` alone)
- `git stash clear|drop`, `git reflog expire|delete`, `git update-ref -d`, `git filter-branch|filter-repo`
- `find ... -delete`, `... | xargs rm`, `rsync --delete`, `shred`, `truncate -s 0`
- `DROP TABLE`, `TRUNCATE`
- `DELETE FROM` without a `WHERE` clause (including multiline)

The patterns also run on the command as the shell reads it — quotes around the
command word removed (`'rm' -rf`), wrappers dropped (`command`, `env`, `sudo`),
git's global options dropped (`git -C . reset --hard`), and each `$(...)` read
as a command of its own — so respelling a command does not get it past the
check. Two more cases are denied the same way:

- a command whose **program is decided at run time** (`$x -rf build`,
  `eval "$CMD"`, `bash -c "$CMD"`): nothing can read what it runs. A variable
  directory with a literal program (`$JAVA_HOME/bin/java`) is fine.
- **recursive deletion in an interpreter one-liner** (`shutil.rmtree(`,
  `fs.rmSync(..., {recursive: true})`, `FileUtils.rm_rf`, `rmtree(`).

The corpus of evasion attempts and the gaps left open on purpose are in
`tests/unit/test_gate_evasion.py`.

#### Commit attribution

AI attribution never reaches project history or a pull request: a
`Co-Authored-By` trailer naming Claude, Anthropic, Gemini, Antigravity or
Copilot, the `noreply@anthropic.com` address, or a "Generated with/by Claude"
line. The Antigravity adapter's `commit-msg` hook applies the same rule through
`scripts/check_commit_message.py`. It is checked at three points:

- **Before the command** (`git commit`/`merge`/`tag`/`notes`, `gh pr|issue`,
  `glab mr`, `az repos pr`): the raw command, including any script in it that
  writes the message, plus any message or body file it names (`-F`, `--file`,
  `--template`, `--body-file`, `--description @file`).
- **After a command that writes history** (`commit`, `commit-tree`, `merge`,
  `pull`, `cherry-pick`, `rebase`, `revert`, `am`): the unpublished commits on
  `HEAD` are read, and the model is told to reword them before anything else.
- **At `git push`**: every commit the push would publish (from its refspecs,
  `--all`, `--mirror`, `--tags`, else `HEAD`) that no remote-tracking ref has,
  and the message of every annotated tag it sends (named, `--tags`, or
  pointing at a published commit, as `--follow-tags` sends it).
  This is the check every path crosses -- amend, cherry-pick, commits made
  outside the session, and the PR body a forge pre-fills from a single commit
  -- so it fails closed: a push whose repository or refspecs are built at run
  time is denied with the reason.

The only exception is `PLAYBOOK_ALLOW_AI_ATTRIBUTION=1` in the environment
Claude Code runs in. Setting it inside a command does not reach the hook. The
hooks only see commands the agent runs; a push from your own terminal is not
checked. The Antigravity `commit-msg` hook ignores the variable, because a git
hook inherits it from the `git commit` command that sets it; skip that hook
once with `git commit --no-verify`.

#### Secrets

The hook ships gitleaks' rules (v8.30.1, MIT, converted to Python in
`hooks/data/secret_rules.json`; no gitleaks install needed). Rules with a
distinctive token shape (AWS, GitHub, GitLab, Stripe, Slack, private keys and
about a hundred more) **deny**; keyword-anchored rules (`api_key = ...`, JWTs)
only **warn** the model, because they fire on placeholders. Checked at:

- **A write** (Write/Edit/MultiEdit/NotebookEdit, and Bash that writes a file):
  only the lines the call adds, and only where git would track them. A secret
  in a git-ignored `.env` is the recommended practice, not a leak.
- **`git commit`**: the added lines of the staged diff (the diff against `HEAD`
  with `-a` or a `git add` earlier in the command). Fails open; the push is the
  backstop.
- **`git push`**: every commit no remote has, including a secret added and
  removed again before the push. Fails closed.

A deny shows a redacted prefix and a fingerprint, never the value. False
positives are the user's to allow: add the fingerprint line to
`.playbook/secrets-allow` in the repository (`#` starts a comment). The hook
denies the agent writing that file, and inline `gitleaks:allow` markers are not
honored, since the agent could write them. To re-sync the rules with a newer
gitleaks release, see `scripts/sync_secret_rules.py`.

#### Local validation lab

`lab/run.py` scans the project's **own** application, running on this
machine, with OWASP ZAP baseline (passive). Nothing else can be targeted.

```bash
python3 lab/run.py --setup                       # checks Docker (Compose 2.20+), downloads the pinned ZAP image, about 1.2 GB (3.8 GB on disk)
python3 lab/run.py --service app --port 8080     # from the project directory
python3 lab/run.py --uninstall                   # removes the image
```

**How a run works:**

- **Isolated copy.** The lab renders the project's compose configuration into its own compose project. That copy has no published ports, no fixed names, subnets or addresses, and fresh volumes, so the running dev stack is not touched.
- **Internal networks.** Every network of the copy is `internal`, so no service of it reaches this computer (a database on `host.docker.internal`), the local network or the internet. A service that needs the internet to start does not start in the lab. The scanner joins its own internal network, whose only other member is the target service.
- **Host access.** A service that is privileged, adds capabilities beyond a small safe set, uses devices or GPUs, shares the host's namespaces (`pid`, `ipc`, `uts`, `userns_mode`, `cgroup`), uses a `network_mode`, or reaches containers outside the lab is refused.
- **Target.** The target is a compose service plus a port and a path, never a URL.
- **Checks before start.** Both the rendered and the Compose-resolved configuration are checked. A failed check refuses the run with exit code 2.
- **Bind mounts.** A project that mounts folders of this computer is refused, with the list of mounts. `--allow-bind-mounts` scans anyway.
- **Nothing kept.** The report is printed and deleted. The lab's containers, network, volumes and built images are removed at the end of every run, also on error or Ctrl-C. A removal that fails is reported with the command that finishes it.

The hook denies scanners and scanner containers (`nmap`, `nuclei`, `sqlmap`,
`ffuf`, ZAP and others) run outside the lab. There is no override. The opt-in
end-to-end test runs with `PLAYBOOK_LAB_IT=1`.

#### Static analysis

The playbook bundles no SAST engine. When `semgrep` or `opengrep` is on the
PATH, `SessionStart` names it in the session context with the exact command, and
a scan of the changed files becomes part of validation. The command uses
`--metrics=off`, `--error` and `--severity ERROR`, and either the project's own
`.semgrep.yml` / `.semgrep/` or, for semgrep only, `p/default`. A scan counts
as a validation command for the Stop gate.

The hook does not run the scan itself. A scan takes seconds, the Stop hook has
ten, and a hook that times out lets everything through. Findings are handled as
review candidates. The agent never adds `nosemgrep`. A scan that cannot run
(no network, timeout) is reported and does not block.

Review fixtures: `tests/behavioral/prompts/06`–`18` hold one intentionally
vulnerable diff per vulnerability fiche, each with the findings the review must
name. Run them with `bash tests/behavioral/run_behavioral.sh`; this needs the
`claude` CLI and an API key, and results are probabilistic.

### Registries

Registries are project-local files that catalog every artifact: services,
repositories, controllers, utilities, adapters, events, interfaces, DTOs.

The agent reads the relevant registry before creating any artifact (to check
if one already exists) and updates it after any creation, modification, or
deletion. Creating a duplicate when a reusable artifact exists in the registry
is treated as a failure.

### Extending the plugin

**Adding a new skill** (e.g., GraphQL, gRPC):

1. Create `skills/engineering-graphql/SKILL.md` with the rules.
2. Add `"./skills/engineering-graphql"` to the `skills` array in
   `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`.
3. Add a row to the routing table in `skills/engineering-playbook/SKILL.md`.

**Adding a new registry type:**

1. Create `registries/your-type.md` in the project following the format of
   existing registry files.

**Modifying the governance hook:**

Edit `hooks/context_guard.py`. The thresholds (`WARN_PCT`, `BLOCK_PCT`,
`UNLOCK_PCT`) are defined at the top of the file; destructive patterns
(`DESTRUCTIVE_PATTERNS`), the correction and review-bypass phrase sets
(`CORRECTION_RE`, `REVIEW_BYPASS_RE`) and the journal helpers live in
`hooks/playbook_core.py`, shared with the Codex adapter. After editing, run the
test suites:

```bash
python3 tests/run_all.py          # unit, structural and e2e
claude plugin test .              # the hooks/config_panel.tsx module (needs the claude CLI)
```

---

## What you get

The catalog is split into seven categories. Every skill is a single
`SKILL.md` file under [`skills/`](skills/). `engineering-playbook` is the entry
point: the non-negotiable rules and the routing table that loads the others.

### Architecture

- `engineering-architecture` — layers, SOLID, forbidden patterns
- `engineering-arch-hexagonal` — hexagonal layer rules and file placement
- `engineering-arch-modular` — modular monolith architecture rules
- `engineering-arch-mvc` — MVC architecture rules
- `engineering-design-patterns` — SOLID, naming, structural patterns
- `engineering-ddd` — domain entities, value objects, aggregates, repositories

### Code quality

- `engineering-code-economy` — reuse protocol, minimum code, surgical changes
- `engineering-code-review` — review workflow, severity classification; security candidates close as confirmed (source-to-sink trace), ruled out (named control at `file:line`) or open proof gap
- `engineering-governance` — when to ask, prohibited behaviors, validation
  checklist
- `engineering-linting` — Biome + typescript-eslint config and rules
- `engineering-performance` — N+1, unbounded queries, complexity, event loop
- `engineering-security` — input validation, injection, secrets, auth, HTTP; 13 vulnerability fiches in `references/` (invariant, sinks, control, the hostile inputs that become negative tests, review signals), read on demand
- `engineering-testing` — test types, coverage, test doubles, vitest config
- `engineering-typescript` — type safety, async patterns, code style

### Languages

Opt-in, loaded for the project's implementation language; the core rules are
language-agnostic.

- `engineering-typescript` — listed above under code quality
- `engineering-java`, `engineering-kotlin` — JVM ecosystem, build tools, ORMs,
  idiomatic layering
- `engineering-go`, `engineering-rust`, `engineering-csharp`, `engineering-ruby`
  — each language's frameworks, data access, test runner and idioms

### Concurrency and reliability

- `engineering-concurrency` — race conditions, distributed locks, idempotency,
  rate limiting
- `engineering-error-handling` — exception hierarchy, retry, circuit breaker,
  timeouts

### Frameworks

- `engineering-nestjs` — modules, controllers, DI, DTOs, config, logging
  (13-step scaffold)
- `engineering-nestjs-security` — helmet, CORS, throttler, ValidationPipe,
  `main.ts` bootstrap
- `engineering-nestjs-logger` — `nestjs-pino` setup and structured logging rules
- `engineering-nestjs-interceptors` — TraceIdInterceptor,
  TransformResponseInterceptor, HttpExceptionFilter
- `engineering-prisma` — schema, migrations, transactions, error handling
- `engineering-docker` — Dockerfile, docker-compose, `.dockerignore`, entrypoint

### Messaging

- `engineering-kafka` — event contracts, producers, consumers, DLQ, schema
  evolution
- `engineering-rabbitmq` — exchanges, manual ack, DLQ, retry, schema evolution

### Workflow and onboarding

- `engineering-implement` — implementation workflow with hard spec gate
- `engineering-refactor` — refactoring workflow (orchestrator + JIT phase
  sub-skills)
- `engineering-refactor-purge` — dead code and dependency removal phase
- `engineering-refactor-tooling` — tooling baseline phase (biome, tsc, vitest)
- `engineering-scaffold` — new project scaffolding questions and checklist
- `engineering-learn` — mines session logs for failure -> success patterns,
  writes to project `CLAUDE.md` (run it from `/playbook-config` → Learn)
- `playbook-on` — re-activates rules after suspension
- `playbook-off` — suspends rules for exploratory sessions
- `playbook-config` — settings menu: prose level, known-failure warnings,
  cost budgets, session status, learn

---

## Usage

### Slash commands

| Command | Effect |
| --- | --- |
| `/playbook-off` | Suspends all governance rules for the session (prototyping, exploration) |
| `/playbook-on` | Re-activates rules after a suspension |
| `/playbook-config` | Settings menu: prose level, known-failure warnings, cost budgets; session status; Learn (mines this project's failures and writes rules to `CLAUDE.md`). In Claude Code, typing it opens a numbered menu above the prompt (`hooks/config_panel.tsx`): type a number and Enter (0 goes back or exits). Settings change with no model call; Status and Learn hand the request to the model, since both need the session's context. |

Codex CLI does not expose slash commands for installed skills — invoke the
corresponding skill by name in the prompt or via the Codex skill surface.
Antigravity exposes `/engineering-playbook`, `/playbook-on`, `/playbook-off` and
`/playbook-config` as workflows under `.agents/workflows/`.

### Upgrading from 3.x

4.0 renames the session commands and folds the single-setting ones into
`/playbook-config`:

| 3.x | 4.0 |
| --- | --- |
| `/engineering-playbook-off` | `/playbook-off` |
| `/engineering-playbook-on` | `/playbook-on` |
| `/engineering-playbook-status` | `/playbook-config` → Status |
| `/engineering-prose <level>` | `/playbook-config` → prose, or `/playbook-config prose <level>` |
| `/engineering-watch <on\|off>` | `/playbook-config` → watch |
| `/engineering-learn` | `/playbook-config` → Learn |

Re-running the Antigravity installer removes the 3.x workflows and skills from
`.agents/`; the Claude Code and Codex installs replace the plugin as a whole.

### How skills are routed

The master `engineering-playbook` skill contains a routing table. When the
agent identifies what the current task involves, it loads the relevant
sub-skills on demand; nothing is pre-loaded. `engineering-governance` brings
its companion skills (security, concurrency, error handling, performance and
the language opt-in) along when an implementation starts.

| Task | Skill loaded |
| --- | --- |
| Any code implementation | `engineering-governance` |
| Creating or reusing artifacts | `engineering-code-economy` |
| Architecture decision | `engineering-architecture` |
| Code in the project's language | `engineering-<lang>` (`typescript`, `java`, `kotlin`, `go`, `rust`, `csharp`, `ruby`) |
| Data access, loops, bulk operations | `engineering-performance` |
| Auth, secrets, external input | `engineering-security` |
| Shared state, distributed ops, idempotency | `engineering-concurrency` |
| Exception paths, retry, external calls | `engineering-error-handling` |
| Tests | `engineering-testing` |
| Code review | `engineering-code-review` |
| New implementation task | `engineering-implement` |
| Refactoring or bug investigation | `engineering-refactor` |
| New project scaffolding | `engineering-scaffold` |
| NestJS | `engineering-nestjs` |
| NestJS HTTP security | `engineering-nestjs-security` |
| NestJS structured logging | `engineering-nestjs-logger` |
| NestJS interceptors / exception filter | `engineering-nestjs-interceptors` |
| Kafka | `engineering-kafka` |
| RabbitMQ | `engineering-rabbitmq` |
| Prisma | `engineering-prisma` |
| Docker | `engineering-docker` |
| DDD | `engineering-ddd` |
| Hexagonal architecture | `engineering-arch-hexagonal` |
| Modular monolith | `engineering-arch-modular` |
| MVC | `engineering-arch-mvc` |
| SOLID / naming / design patterns | `engineering-design-patterns` |
| Extract session learnings | `engineering-learn` |

### Implementation workflow

When `engineering-implement` loads, the flow is:

1. **Read** — read all relevant files.
2. **Clarify** — list open questions in a single message.
3. **Spec checklist** — produce a checklist of every file that will be touched,
   what changes, and what tests cover it.
4. **STOP** — post the checklist and wait for explicit confirmation (`"proceed"`,
   `"ok"`, `"sim"`, `"go"`) before writing a single line of code.
5. **Build** — implement exactly the checklist, nothing more.
6. **Validate** — run lint, type check, and tests.
7. **Register** — update registry entries.

No code is written before step 4 confirmation.

### Session learning

After a session with errors or corrections, open `/playbook-config` and pick
**Learn**.

The agent will:

1. Read the failure journal the hooks keep for this project, then the session
   logs at `~/.claude/projects/<project-path-slug>/*.jsonl`.
2. Extract every tool call that returned an error and the fix that followed.
3. Correlate patterns: wrong file paths, failed commands, wrong search scope,
   rejected tool calls.
4. Write specific corrections into the project `CLAUDE.md` with idempotent
   markers — re-running always replaces, never duplicates.

---

## Contributing

Contributions are welcome. By participating in this project, you agree to
abide by its [Code of Conduct](CODE_OF_CONDUCT.md).

The skill catalog, hook implementations, and adapter scripts are the
canonical artifacts. Improvements should arrive as pull requests against
`develop`. Each PR should keep a single logical scope and not mix unrelated
changes — the playbook is reviewed at the rule level, and a noisy diff makes
rule-by-rule review impossible.

**Branch flow and protection.** `develop` and `main` take changes only through
pull requests: no direct push, no force push, no deletion, and the `tests`
workflow must pass on the branch brought up to date. A pull request from
anyone but the owner needs the owner's approval (`.github/CODEOWNERS`); the
owner's own pull requests merge without one, but still as pull requests.
Only the owner creates, pushes to or merges into any branch, whatever access
anyone else is given (Dependabot's own branches excepted, so it can open its
pull requests), and auto-merge is off: every merge is the owner's.
`main` accepts pull requests from `develop` only. A merge into `main` tags
`v<version>` from the plugin manifests and publishes a GitHub release, so the
version bump belongs in the `develop` → `main` pull request; release tags
cannot be moved or deleted. The rules live in `.github/rulesets/` and are
applied with `python3 scripts/apply_repo_protection.py --apply`. Report
vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).

For skill additions, follow the **Extending the plugin** section above.
For harness changes, update the corresponding adapter (`adapters/<harness>/`)
and the **[Supported harnesses](#supported-harnesses)** table.

## Code of Conduct

This project follows the [Contributor Covenant, version 2.1](CODE_OF_CONDUCT.md).
Instances of abusive, harassing, or otherwise unacceptable behavior may be
reported to the community leaders responsible for enforcement at
`hagavepro@gmail.com`.

## License

MIT with the [Commons Clause](https://commonsclause.com/) condition. The
[LICENSE](LICENSE) file is the binding text; in short:

- **Allowed:** using the playbook for any work, including paid work at a
  for-profit company; reading, modifying, customizing and extending it;
  sharing it, changed or not, free of charge, with this license kept.
- **Not allowed:** selling it, or selling a product or service whose value
  comes entirely or substantially from it: a paid copy, a paid hosted
  version, paid support or consulting for the playbook itself.

Because of that condition the license is source-available rather than open
source in the OSI sense. Third-party parts keep their own licenses: the secret
rules converted from gitleaks are MIT, with their notice in
`hooks/data/LICENSE.gitleaks`. Contributions are accepted under the same
LICENSE.

## Links

- Repository: <https://github.com/Hagave/engineering-playbook>
- Issues: <https://github.com/Hagave/engineering-playbook/issues>
- Codex adapter docs: [`docs/adapters/codex.md`](docs/adapters/codex.md)
- Antigravity adapter docs: [`adapters/antigravity/README.md`](adapters/antigravity/README.md)
- Contributor Covenant v2.1: <https://www.contributor-covenant.org/version/2/1/code_of_conduct/>

## What this does not replace

- Code formatters and linters (Biome, typescript-eslint) — the playbook
  configures these but does not run them.
- CI/CD pipeline checks.
- Human peer review — this is a first-pass tool, not a substitute.
- Automated test suites.
