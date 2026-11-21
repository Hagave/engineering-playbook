---
name: playbook-config
description: "The playbook's settings menu: response prose level, known-failure warnings, cost budgets, menu language, session status, learning from past failures and the red-lab-playbook (security scans of the user's own app in an isolated local copy). Use when the user says /playbook-config, asks for the playbook's settings, status or configuration, wants shorter or longer answers, wants warnings about past mistakes on or off, asks the playbook to learn from this project's failures, or a prompt names a red-lab-playbook step."
version: 3.5.0
user-invocable: true
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Playbook Config

One menu for everything the user can set or ask the playbook for. `/playbook-on` and `/playbook-off` stay separate commands.

`<playbook>` below is the playbook clone: two levels above this skill's base directory in Claude Code and Codex, and in Antigravity the path the Settings section of `AGENTS.md` names. Every setting is read and written through `python3 <playbook>/scripts/playbook_config.py`, never by editing a file by hand: the script validates the value and writes atomically.

Every `playbook_config.py` call passes `--harness`: `claude` in Claude Code, `codex` in Codex, `antigravity` in Antigravity. `show` then lists only the settings that harness acts on, and `set` refuses the others, because nothing there would read them. In Codex and Antigravity that leaves the prose level alone.

A user who asks for a change outside the menu ("shorter answers", "set prose to low") gets the same `set` call; there is no need to open the menu first.

---

## The menu

Show one screen at a time. Every screen lists its entries with a one-line explanation of what each does, and ends with **Back** (except the main menu) and **Exit**. Typing `exit` at any screen leaves the menu. Change only what the user picks; never set a value they did not choose.

Present a screen with the AskUserQuestion tool where it exists (Claude Code): one question, its options the screen's entries, each option's description the explanation. AskUserQuestion takes at most four options; when a screen has more, drop **Exit** from it and keep **Back** (the user can still type `exit`). In a harness without that tool, print the screen as a numbered list and wait for the user's answer.

Render every screen — its title, the option labels and their explanations — and your own replies in the language the user writes in; a user writing Portuguese gets the menu in Portuguese. Keep the setting keys (`prose`, `watch`, `claude_md_budget`, `context_budget`, `language`), the command names (`/playbook-config`, `/playbook-off`) and the `playbook_config.py` arguments exactly as written — translate only what the user reads.

### Main menu

| Entry | What it does |
|---|---|
| Settings | Change how the playbook behaves: prose level, warnings, cost budgets. |
| Status | Show the session's configuration, the current settings and the output filter's totals. |
| Learn | Mine this project's failure journal and sessions for repeated mistakes and write rules for them into the project `CLAUDE.md`. |
| red-lab-playbook | Attack the user's own app in an isolated local copy of its Docker Compose stack. Opens the *red-lab-playbook* screen. |
| Exit | Leave the menu. |

### Settings

Read the current values first: `python3 <playbook>/scripts/playbook_config.py show --harness <harness>`. Show only the entries it lists, each with its current value; the rows below that it does not list do not exist in this harness.

| Entry | Key | Values | What it does |
|---|---|---|---|
| Prose level | `prose` | `low`, `mid` (default), `high` | How much explanation a response carries. See *Prose levels*. |
| Warnings and reminders | | | Opens a screen with the two settings below. |
| Cost budgets | | | Opens a screen with the two budgets below. |
| Language | `language` | `auto` (default, follows the system locale), `en`, `pt-br`, `es` | Language of the fixed text in Claude Code's numbered settings menu. Replies always follow the language the user writes in. |
| Deviation reports | `reports` | `on`, `off` (default) | Sends the maintainer each deviation a model reports from a skill rule. See *Deviation reports*. |

Warnings and reminders screen:

| Entry | Key | Values | What it does |
|---|---|---|---|
| Known-failure warnings | `watch` | `on`, `off` (default) | Before a write or a command that touches something that already failed in this project, show the recorded failure. See *Known-failure warnings*. |
| Temporary data panel | `temporary_data` | `on` (default), `off` | In auto mode, a deletion the user declines on the button above the chat is kept in this project's Temporary data panel (`/temporary-data`) to remove later; off, it is only dropped. Claude Code only. |

Cost budgets screen:

| Entry | Key | Values | What it does |
|---|---|---|---|
| CLAUDE.md budget | `claude_md_budget` | characters, `0` = off (default 40000) | Blocks the end of a turn that grew the project `CLAUDE.md` past this size. The file is re-sent to every session and every subagent. |
| Context budget | `context_budget` | `off`, `550k` (default), `650k`, `850k`, or a custom token count the user types (e.g. `700k`) | Past this much context, tells the model once per multiple to finish the step, write the handoff and ask for `/clear`. Never blocks. |

Selecting an entry opens a screen with its values (plus **Back**). Apply the choice with:

```bash
python3 <playbook>/scripts/playbook_config.py set <key> <value> --harness <harness>
```

Report the script's output lines as they come: the second one says when the change takes effect and what the user will notice, which differs per setting (prose waits for the next session; the budgets apply at once). Do not add your own timing claim. A value the script rejects is reported as rejected; do not retry with a guessed value.

### Status

1. Print the session configuration block from *Session configuration block*, when the session has one.
2. Print the settings: `python3 <playbook>/scripts/playbook_config.py show --harness <harness>`.
3. Print the output filter's totals: `python3 <playbook>/hooks/output_filter.py --report`, its single line as it comes. When the file does not exist (an adapter without hooks), skip it. The figure counts only what the model would have read: Claude Code already cuts Bash output above 30,000 characters.
4. Name the security layer, so the user knows it is there: the always-on gates (secret scan, destructive-command confirmation, pre-push code review), the per-vulnerability fiches in `engineering-security`, static analysis when `semgrep`/`opengrep` is installed, and the red-lab-playbook, which scans the project's own app in an isolated copy of its stack (main menu → red-lab-playbook).

Then show the Status screen's entries: **Back**, **Exit**.

### Learn

Load the `engineering-learn` skill and follow it. It reads this project's failure journal and session logs, extracts repeated failure patterns, and writes the corrections into the project `CLAUDE.md` (asking before creating that file). When it finishes, return to the main menu.

### red-lab-playbook

The local security lab: OWASP ZAP and sqlmap scan the user's own app inside an isolated copy of its Docker Compose stack, on an internal network the scanner cannot leave. The screen opens with that sentence, so the user knows what it is before choosing, then lists the steps below (plus **Back**). Claude Code's numbered menu sends the chosen step as a prompt naming it (`red-lab-playbook step "<step>"`); run that step the same way.

Every step runs `python3 <playbook>/lab/run.py` from the project root with Bash, output in view. Never point it at anything but the project's own compose services: the script refuses any other target, and there is no override.

| Step | What to do |
|---|---|
| `setup` | Run `--setup`. It checks Docker and Compose, downloads the pinned ZAP image (about 1.2 GB, 3.8 GB on disk) and builds the sqlmap image. Say the size before starting; it can take minutes. |
| `passive` | Run `python3 <playbook>/lab/run.py` with no `--service`: it finds the compose file (listing them if there is more than one, to pick with `--compose-file`) and scans every service built from the project, each isolated. To scan one, add `--service <svc> --port <port>` (and a start path, default `/`). ZAP only reads responses; nothing is attacked. |
| `active` | As `passive`, then add `--active`: ZAP sends attack payloads to the isolated copy. Say so before running. |
| `sqlmap` | As `passive`, plus ask for one endpoint with the parameter to test (`/items?id=1`). Run `--sqlmap --service <svc> --port <port> --endpoint <path>`. |
| `uninstall` | Say which images it removes (the ZAP and sqlmap scanner images) and that `setup` downloads them again, confirm, then run `--uninstall`. |

When the project has no compose file, or `--setup` never ran, say what is missing and offer the step that fixes it. Report the script's findings as it prints them, with the fiche each one points to in `engineering-security`; exit code 0 with no findings means none were found in what was reached, not that the app is safe.

---

## Guided setup (first run)

The SessionStart hook offers this once, on the first run on the machine (a first-run line in the session context). When the user accepts, walk them through the playbook in their language, and let them stop at any point:

1. In two or three lines, say what the playbook gives them: the governance gates (spec, validation, review, secret and destructive-command gates), the security layer (fiches, SAST when installed, the red-lab-playbook), and that every setting below is optional with a sensible default.
2. Walk the **Settings** entries one at a time (prose level, known-failure warnings, the two budgets, and in Claude Code the menu language and deviation reports), each with its default, applying only what they choose — exactly as the menu does. For the language, offer the one they are writing in: `auto` follows the system locale, which is often English on a machine whose user writes in another language.
3. Offer **Status** so they see the result, then leave.

If the user skips, say that `/playbook-config` reopens this anytime, and do nothing else. Never block their first task on it.

---

## Prose levels

Every level forbids the same filler; the difference is what substance is allowed.

| Level | What the response contains |
|---|---|
| `low` | The result, plus only the reasoning that changes a decision. One or two sentences per point. Code and command output are not narrated. |
| `mid` | What was done and why that approach, including trade-offs that would change the user's decision. One short paragraph for anything non-obvious. |
| `high` | Full reasoning: alternatives considered, trade-offs, failure modes, how to verify, grounded in `file:line`, command output or documentation. |

No level permits preamble, restating the request, recapping steps the user just watched, or flattery. It reduces output tokens only; input context is governed by the context guard.

## Deviation reports

Off by default, and never turned on for the user: only their own choice in this menu or in the guided setup turns it on. When they ask about it, say exactly what it does before they choose:

- When a model reports a deviation from a skill rule (`[PLAYBOOK-DEVIATION]`), the hook opens a GitHub issue on the playbook's repository **with the user's own GitHub account**, through `gh` when it is installed and logged in, or shows a pre-filled link for them to send.
- It sends the skill, its version and section, the rule only as a quote found word for word in the skill's text, the action, the playbook version, the harness and the model, and for a version deviation the `name@version` pair. Never the project folder, code, session, `instead` or reasoning.
- A rule someone already reported gets a +1 reaction instead of a second issue. At most five issues a day; sending never delays a turn.
- Turning it off stops sending at once; what was recorded stays in the local journal.

## Known-failure warnings

Retrieval, not supervision: at the moment of a write or a shell command, the `PreToolUse` hook reads the failure journal `learn_guard` writes and the rules inside `<!-- engineering-learn:live -->` in the project `CLAUDE.md`. A `[playbook:watch]` message is something that already went wrong here; it warns and never blocks, once per session per warning.

- Treat a warning as evidence and adjust the action. Do not restate it to the user as news.
- No warning means no recorded failure matches, not that the action is safe.
- A wrong warning is fixed in `CLAUDE.md`, not by turning the warnings off.

---

## Session configuration block

The block records the stack chosen in this session by `engineering-scaffold` (new project) or `engineering-refactor` (existing project). Render it exactly in this shape, and refer to it for the rest of the session instead of re-reading skill files.

```
╔══════════════════════════════════════════════════════════════════════╗
║          Engineering Playbook — Active                               ║
╠══════════════════════════════════════════════════════════════════════╣
║ Mode         │ [New project / Refactor]                              ║
║ Framework    │ [NestJS / Express / Django / FastAPI / ...]           ║
║ HTTP         │ [Express / Fastify / uvicorn / gunicorn / —]          ║
║ Architecture │ [Hexagonal / Modular / MVC]                           ║
║ DDD          │ [yes / no]                                            ║
║ ORM          │ [Prisma / TypeORM / none]                             ║
║ Database     │ [PostgreSQL / MySQL / —]                              ║
║ Broker       │ [Kafka / RabbitMQ / none]                             ║
║ Cache        │ [Redis / none]                                        ║
║ Docker       │ [yes / no]                                            ║
║ Logger       │ [structured logging / built-in / none]                ║
║ Security     │ [headers + CORS + rate limiting / none]               ║
║ Interceptors │ [logging, trace-id, response, filter / none]          ║
║ Patterns     │ [SOLID, Clean Code, ...]                              ║
╚══════════════════════════════════════════════════════════════════════╝
```

Fill each field from the choices made in this session; `—` where a field does not apply; for Interceptors list only the selected ones or `none`.

- The block is session- and project-scoped. A new session starts with none, whatever an earlier session configured.
- With no configuration this session, do not infer one: say there is none and that it is set when a project is scaffolded or refactored.
- When the user changes a choice mid-session, update the block and show it again; the new block replaces the old one.
- With a block in place, apply skill rules silently: raise a rule only when a request would break it or the user asks. Reason from the block's fields, not from defaults.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
