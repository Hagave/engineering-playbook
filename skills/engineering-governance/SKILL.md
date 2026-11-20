---
name: engineering-governance
description: "Governance rules for any code implementation task. Use when implementing a feature, fix, or change: defines when to stop and ask, prohibited behaviors, context/clear ownership, and the validation checklist that must pass before every response. Activate for any coding task."
version: 5.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Governance

## Mandatory companion skills

Before generating the implementation checklist (engineering-implement Step 6), load these skills if not already loaded. They are unconditional — they apply to every implementation task, in every language, regardless of how the task is described:

- `engineering-security` — auth, input handling, secrets, external data
- `engineering-concurrency` — shared state, distributed ops, idempotency, rate limiting
- `engineering-error-handling` — exception paths, retry, circuit breaker, external calls
- `engineering-performance` — data access patterns, N+1, unbounded queries, bulk ops
- the language opt-in for the code you write, when the playbook has one (`engineering-typescript`, `engineering-go`, `engineering-java`, `engineering-kotlin`, `engineering-csharp`, `engineering-rust`, `engineering-ruby`) — type safety, idioms, build and test tooling

Do not wait for a task description to match a keyword before loading these. Load all of them before the checklist step — the checklist is where security, concurrency, and error-handling constraints must appear as explicit items, not as an afterthought during implementation.

The skill gate enforces this list and extends it to what each written file calls for: the framework skill its nearest manifest names (`@nestjs/core` in `package.json` → `engineering-nestjs`) and the skill of the topic it touches (a Dockerfile → `engineering-docker`, a message-broker import → `engineering-rabbitmq` or `engineering-kafka`, a test file → `engineering-testing`), whether or not the scaffold loaded it. A setup skill is required again whenever its topic is touched, in any session.

---

## Spec gate

Before any Write or Edit of a source file — any language the project is written in, not just the one you expect (TypeScript, Python, Go, Java, Rust, C#, Kotlin, Ruby, PHP, Swift, Scala, Dart, Elixir, C/C++, shell, and the rest) — emit the literal token `[SPEC CONFIRMED]` in your response text, and repeat it in the turn's final message. The Stop hook enforces this — the turn cannot end until the token is found. Shell commands that write source files (`sed -i`, `tee`, redirection) are gated the same way. Docs and config files are not hook-gated, but the workflow discipline in `engineering-implement` still applies to them.

**When to emit:**
- After the user confirms the implementation checklist (engineering-implement STOP step).
- For trivial single-line fixes where the spec is self-evident: emit immediately as `[SPEC CONFIRMED] — unambiguous single-line fix.`
- For bug fixes with no ambiguity: emit as `[SPEC CONFIRMED] — fix is scoped and unambiguous.`

**When not to emit:**
- Do not emit preemptively before reading context and extracting requirements.
- Do not emit while ambiguities remain unresolved.

**Token scope — per task, per turn:** the hook accepts the token only if it appears in assistant text emitted after the latest user message. Emit it, then write code in the same turn. A token emitted before an intervening user message no longer satisfies the gate — re-emit it when the user grants continuation of the same task.

**Why the final message too:** text written between tool calls is not always recorded in the transcript the Stop hook reads (measured: 7 of 15 such tokens missing in one session), so a token that only appeared there is invisible and the turn is blocked. The turn's final message reaches the hook with the Stop event itself. Put the token there as well, with what was implemented.

---

## Hook-enforced gates

Four additional gates are enforced mechanically by the plugin hook — they cannot be satisfied by intending to comply:

- **Skill gate:** writing any source file, in any language, is denied until this skill (`engineering-governance`) and every skill the file calls for — the companions, its language opt-in, its framework, its topic — have been loaded via the Skill tool in the current session. A topic file that is not source (a Dockerfile, a compose file, a Prisma schema) needs its topic skill. The deny names each missing skill and the reason; load them all, then retry. A `/clear` starts a new session, so they are loaded again after it.
- **Validation gate (Stop):** ending the turn is blocked if source files were written and no validation command (tsc, lint, tests) ran afterwards. Run the validation checklist below, or report explicitly what is blocking it.
- **Review gate (pre-push):** `git push` is denied until what it publishes has been reviewed via the `engineering-code-review` skill. A commit of exactly what the review read in the working tree is covered; a file changed after the read is code no review has read, and the gate names it so only it is reviewed again. Only the user can skip it, by running the push themselves or by starting Claude Code with `PLAYBOOK_SKIP_REVIEW=1`; nothing typed in the chat lifts it, you can never waive it for yourself, and never suggest skipping it as a way past your own findings.
- **Destructive-command gate:** `rm -r`, `git reset --hard`, force push, `git clean`, mass delete and destructive SQL go to the user through Claude Code's permission dialog, which states what the command destroys: when the task needs one, call the tool and the harness asks the user; it runs only if they approve that dialog. Never ask the user for a confirmation phrase — nothing typed in the chat approves it, and you cannot approve it yourself. Where no dialog can reach the user (permission modes other than Manual and auto, a subagent, a cloud session), the gate denies the command: show the user the exact command and what it destroys, and ask them to run it themselves. An `rm` of literal absolute paths inside this session's scratchpad is exempt.
- **Learn gate (Stop):** when the user's message says the previous turn was wrong, when the same call has failed repeatedly, or when a review reported Blocker/Major findings in code you wrote, ending the turn is blocked until you emit `[LEARNED]`. To clear it: name the premise you got wrong in one line, say what is actually true and what proves it, and persist the rule in the project `CLAUDE.md` inside the `<!-- engineering-learn:live -->` block (ask before creating `CLAUDE.md` itself). If the signal was a false positive, emit `[LEARNED] none — <reason>`. Do not emit the token without the rule; the gate exists to produce the rule, not the token.

All gates are suspended while `playbook-off` is active.

---

## Context management

The context-guard hook is the **single source of truth** for context health. It resolves the active model's real window — including the 1M beta — and emits `[playbook:context]` at 70% (warn) and 85% (hard block).

- Do not recommend `/clear` on your own judgment. Token count is the only trigger, and only the hook measures it reliably.
- Do not relay Claude Code's native "context low" / auto-compact prompts. That is a separate accounting that can misread the window (e.g. counting a 200k window on a 1M-beta session) and raise false alarms — a long session or a large scaffold is not, by itself, a reason to reset context.
- The exit is always `/clear`. Never recommend `/compact`, in any situation: a compact does not reliably bring the session back under the threshold, and one that falls short leaves the hook blocking; `/clear` always does, and the handoff carries the task across it.
- **Warn (70%):** do not load new skills or start new work. Finish the task in progress, if any. Then end the turn with a handoff block, and tell the user to run `/clear` and ask to resume where the session left off.
- **Hard block (85%):** stop calling tools — they are denied — and do not try to finish. Write the handoff block right away, including any code generated this turn that is not yet on disk, then give the same `/clear` instruction.
- **Handoff block:** response text between a `[HANDOFF]` line and a `[/HANDOFF]` line, each marker alone on its line (an inline mention is not captured) — the goal as the user stated it, what is done, what is pending with the exact next step, decisions and constraints already agreed, files involved, and any gate still owed. It is text, not a tool call, so it gets out even under the hard block. The Stop hook saves it next to the automatic carry-over, and the `SessionStart` after `/clear` re-injects both once.
- Neither you nor the hook can run `/clear` — only the user can.

---

## When to stop and ask

Stop and ask before implementing when any of the following is true:

- A requirement is ambiguous.
- Required information is absent from the request, the code, or the registries.
- The scope of the change is unclear.
- The task touches a Public Contract artifact — verify compatibility before proceeding.
- Two valid implementation paths exist with meaningfully different trade-offs.

Send all questions in a single message. Never send multiple rounds of one question each.

After receiving answers: summarize your understanding before starting implementation. Confirm before proceeding.

---

## Before implementing

If a simpler approach exists, surface it explicitly before starting. Push back when the requested approach is more complex than the problem warrants. Do not silently implement the complex path.

If multiple valid interpretations of the task exist, present them — do not pick one silently.

For tasks with more than one step, state the plan first:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Confirm the plan before proceeding. Weak success criteria ("make it work") require clarification before implementation begins.

---

## Writing code

**Comments.** A comment goes in for exactly two reasons:

- a trade-off that was accepted or is known: what was given up, and why;
- a palliative fix that is meant to be redone: what the proper fix is.

Nothing else is a comment. Not what the code does, not a name said again in prose, not a doc block on every member, and not history — a date, a ticket, what the code used to do, who asked for it. History belongs in the commit message. Code that needs a sentence to be understood is renamed or restructured until it does not.

**Edits.** Change project code with the edit tool, not with a script run in the shell (`sed -i`, a heredoc that rewrites the file, an interpreter one-liner). The edit tool checks each change against the file as it was read; a script writes from memory. When a change has several parts, send the edits together in one response. A script is for one mechanical change repeated across many files, such as a rename.

**Layout.** Line breaks, wrapping, quote style, import order and trailing commas belong to the project's formatter and its lint autofix. Write the code and let the tooling lay it out. Never spend an edit re-wrapping a line or re-ordering imports by hand.

If the project has no formatter or autofix wired, so that a layout failure reaches you as work, fix what blocks the task and say so to the user once, in the final message: adopting one removes that work for good. Do not install or configure one unasked.

---

## Prohibited behaviors

- Assuming requirements not stated.
- Inventing behavior not requested.
- Creating features outside the stated scope.
- Modifying artifacts not referenced in the task.
- Making architecture decisions without justification.
- Implementing before clarifying ambiguities.
- Silently choosing the complex path when a simpler one exists.
- Recommending `/clear` without a `[playbook:context]` signal from the hook in the current turn.
- Recommending `/compact`, for any reason.
- Delivering without running the validation checklist.
- Manually creating migration files or editing lockfiles (package-lock.json, yarn.lock, pnpm-lock.yaml) — always use the CLI. Manual authoring causes checksum mismatches.
- Editing a migration that has already been applied — it causes state drift and checksum failures. Always generate a new migration instead; see `engineering-prisma` for the ORM-specific approach.

---

## Validation checklist

Run before every response. Every item must be checked explicitly.

- [ ] All stated requirements were addressed.
- [ ] All ambiguities were resolved before implementation — none were assumed.
- [ ] No hypothesis was acted on without confirmation.
- [ ] No skill rule was violated.
- [ ] No Public Contract was modified without compatibility analysis.
- [ ] No artifact was created without first verifying reuse was not possible.
- [ ] No artifact was duplicated.
- [ ] Compatibility was preserved, or breaking changes were explicitly identified and reported.
- [ ] No changes were made outside the stated scope.
- [ ] Every changed line traces directly to the user's request.
- [ ] Pre-existing dead code or unrelated issues were mentioned, not fixed.
- [ ] If the project has registries: all registries affected by this change were updated.
- [ ] Code compiles without TypeScript errors (`tsc --noEmit` passes).
- [ ] Linter passes with no new errors introduced by this change (`lint:check` passes).
- [ ] If the session context reports `[playbook:sast]`: the static analysis scan of the changed files ran, and every finding was fixed or reported (see "Static analysis" in `engineering-security`).
- [ ] All tests relevant to changed code pass.
- [ ] Tests required by the implementation checklist were written and pass.
- [ ] No new public method or use case path was left without test coverage.
- [ ] If the project is NestJS: every public endpoint added or modified has Swagger decorators (rule owned by `engineering-nestjs`).

If a checklist item cannot pass due to missing information or unresolved ambiguity: stop, report what is blocking, and ask. Do not deliver a partial response as if it were complete.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
