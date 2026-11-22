---
name: engineering-governance
description: "Governance rules for any code implementation task. Use when implementing a feature, fix, or change: defines who owns each decision (and so when to ask), prohibited behaviors, context/clear ownership, and the validation checklist that must pass before every response. Activate for any coding task."
version: 7.3.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Governance

## Mandatory companion skills

Before generating the implementation checklist (engineering-implement Step 6), load these skills if not already loaded. They are unconditional — they apply to every implementation task, in every language, regardless of how the task is described:

- `engineering-security` — auth, input handling, secrets, external data
- `engineering-concurrency` — shared state, distributed ops, idempotency, rate limiting
- `engineering-error-handling` — exception paths, retry, circuit breaker, external calls
- `engineering-performance` — data access patterns, N+1, unbounded queries, bulk ops
- the language opt-in for the code you write, when the playbook has one: the Language opt-in table of `engineering-playbook` lists them (`engineering-typescript`, `engineering-python`, `engineering-go`, `engineering-java` and the rest), and the skill gate names the one a file needs — type safety, idioms, build and test tooling

Do not wait for a task description to match a keyword before loading these. Load all of them before the checklist step — the checklist is where security, concurrency, and error-handling constraints must appear as explicit items, not as an afterthought during implementation.

The skill gate enforces this list and extends it to what each written file calls for: the framework skill its nearest manifest names (`@nestjs/core` in `package.json` → `engineering-nestjs`), the architecture skill of the structure the file sits under (`domain/`, `application/` and `infrastructure/` side by side → `engineering-arch-hexagonal`), and the skill of the topic it touches (a Dockerfile → `engineering-docker`, a message-broker import → `engineering-rabbitmq` or `engineering-kafka`, a test file → `engineering-testing`), whether or not the scaffold loaded it. A setup skill is required again whenever its topic is touched, in any session.

---

## Spec gate

Before any Write or Edit of a source file — any language the project is written in, not just the one you expect (TypeScript, Python, Go, Java, Rust, C#, Kotlin, Ruby, PHP, Swift, Scala, Dart, Elixir, C/C++, shell, and the rest) — emit the literal token `[SPEC CONFIRMED]` in your response text, and repeat it in the turn's final message. The Stop hook enforces this — the turn cannot end until the token is found. Shell commands that write source files (`sed -i`, `tee`, redirection) are gated the same way. Docs and config files are not hook-gated, but the workflow discipline in `engineering-implement` still applies to them.

**When to emit:**
- After the user confirms the implementation checklist (engineering-implement STOP step).
- When every decision the task needs is fixed or delegated (see Decision ownership), with no confirmation to wait for: emit as `[SPEC CONFIRMED] — decisions delegated by the request.` and record the decisions you took.
- For trivial single-line fixes where the spec is self-evident: emit immediately as `[SPEC CONFIRMED] — unambiguous single-line fix.`
- For bug fixes with no ambiguity: emit as `[SPEC CONFIRMED] — fix is scoped and unambiguous.`

**When not to emit:**
- Do not emit preemptively before reading context and extracting requirements.
- Do not emit while an open decision is unanswered.

**Token scope — per task, per turn:** the hook accepts the token only if it appears in assistant text emitted after the latest user message. Emit it, then write code in the same turn. A token emitted before an intervening user message no longer satisfies the gate — re-emit it when the user grants continuation of the same task.

**Why the final message too:** text written between tool calls is not always recorded in the transcript the Stop hook reads (measured: 7 of 15 such tokens missing in one session), so a token that only appeared there is invisible and the turn is blocked. The turn's final message reaches the hook with the Stop event itself. Put the token there as well, with what was implemented.

---

## Hook-enforced gates

Four additional gates are enforced mechanically by the plugin hook — they cannot be satisfied by intending to comply:

- **Skill gate:** writing any source file, in any language, is denied until this skill (`engineering-governance`) and every skill the file calls for — the companions, its language opt-in, its framework, its architecture, its topic — have been loaded via the Skill tool in the current session. A topic file that is not source (a Dockerfile, a compose file, a Prisma schema) needs its topic skill. The deny names each missing skill and the reason; load them all, then retry. A `/clear` starts a new session, so they are loaded again after it.
- **Validation gate (Stop):** ending the turn is blocked if source files were written and no validation command (tsc, lint, tests) ran afterwards. Run the validation checklist below, or report explicitly what is blocking it.
- **Review gate (pre-push):** `git push` is denied until what it publishes has been reviewed via the `engineering-code-review` skill. A commit of exactly what the review read in the working tree is covered; a file changed after the read is code no review has read, and the gate names it so only it is reviewed again. Only the user can skip it, by running the push themselves or by starting Claude Code with `PLAYBOOK_SKIP_REVIEW=1`; nothing typed in the chat lifts it, you can never waive it for yourself, and never suggest skipping it as a way past your own findings.
- **Destructive-command gate:** `rm -r`, `git reset --hard`, force push, `git clean`, mass delete and destructive SQL need the user's approval, and you cannot approve it yourself.
  - **Manual:** call the tool; Claude Code's permission dialog asks the user and states what the command destroys; it runs only if they approve.
  - **Auto:** the gate denies without a dialog and queues the command as `#n`. Do not stop or wait: carry on with the rest of the approved work, and in your next message say in plain words what `#n` removes, why, and what happens if it is kept, and ask whether it may run. In Claude Code the user may answer with the button above the chat: the playbook then runs the command itself, or keeps a declined deletion in the Temporary data panel, and tells you which; do not run it again either way. The user may also approve by writing it ("pode apagar", "pode remover", "yes, delete it"; `#n` narrows it to that one); the hook then tells you which, and you run that exact command again, once. Never write the approval words yourself or ask the user to type a fixed phrase.
  - **Elsewhere** (other permission modes, a subagent, a cloud session) the gate denies: show the user the exact command and what it destroys, and ask them to run it themselves.
  - An `rm` of literal absolute paths inside this session's scratchpad is exempt.
  - **Temporary data** (Claude Code): what a command makes and leaves behind goes to the project's Temporary data panel once your turn ends, for the user to remove: a git worktree, a named Docker container, volume or network, and what a lone `mktemp` printed. Something else you made that should go and the hook cannot see (a folder named by a variable, `$(mktemp -d)`), keep there yourself in the message where you ask the user about it: `python3 <playbook>/scripts/temporary_data.py register --project <the folder Claude Code started in> --cwd <where the command runs> --command '<the deletion>' --what '<what it is>' [--target <its absolute path>]`, `<playbook>` being two levels above this skill's base directory. Only a deletion is accepted. Never write the Temporary data folder or the queue yourself: the gate denies it.
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

## Decision ownership

Every decision a task needs has one owner, and you act only on the decisions you own. Settle the owner before acting:

- **Fixed:** the request, the repository or a loaded skill already decides it. A requirement the user stated, a public contract or a convention the code already has, a skill rule. Implement it as it stands. Changing it is never your decision, whatever the request delegates: ask the user, or, for a skill rule, report a `[PLAYBOOK-DEVIATION]` block. When nobody can be asked, keep it and say in your final message why you believe it is wrong.
- **Delegated:** the request hands it to you, in general ("decide on your own and record it", "use your judgment", "don't ask me") or for an area ("pick the database"). Decide it, record the decision and its reason where the request says or in your final message, and carry on. Do not ask, and do not ask for confirmation of a plan or checklist that holds only fixed and delegated decisions: the delegation is that confirmation.
- **Open:** neither fixed nor delegated. It is the user's. Ask before acting on it.

An ambiguous requirement, missing information, an unclear scope, and two valid paths with different trade-offs are open decisions, unless the request delegated them. A delegation covers what it names, and a general one covers every open decision, never a fixed one. A delegation never lifts a hook gate.

Any step in a skill that stops to ask or waits for confirmation (the implementation checklist, the scaffold table, a refactor phase) applies this rule: its open decisions go to the user, and its delegated ones are yours to decide and record.

When you ask, put every open decision in a single message, never one question per round. After the answers, say in a few lines how you read them and proceed. Ask again only when an answer opens a new decision.

---

## Dependency versions

- **Which release.** A runtime, base image or package goes in at its LTS line when its maintainer publishes one (Node.js, Java, .NET, a Linux distribution). Otherwise it goes in at the registry's `latest` tag when that tag names a stable release, and at the newest stable release when it names a prerelease. Never install a prerelease (`-rc`, `-beta`, `-alpha`, `-next`, `-canary`, `-dev`) unasked. A bare install follows the tag, so read the tag first (`npm view <name> dist-tags`, or the registry's equivalent) and name the version when the tag is a prerelease. When another package's peer range rules the tag out, the package manager's resolution decides (it steps down to a release that fits); never force the tag past it (`--force`, `--legacy-peer-deps`).
- **Coupled packages share one version.** A CLI, the runtime client it generates for and their adapters (an ORM's CLI, client and driver adapters; a framework's core and its platform packages) are installed at one version: the runtime package's. Each at its own tag can mix majors, as when an ORM tags a release candidate of its CLI as `latest` while its client stays on the previous major.
- **The installed version decides, not the skill.** Before writing code against a dependency the project already has, read its version from the lockfile or the manifest and check the API of that version: the installed type definitions, its changelog or its documentation. When that version contradicts a skill's example, follow the version and report the rule in a `[PLAYBOOK-DEVIATION]` block whose `why` starts with `version <name>@<version>:` and says what changed.

---

## Reporting a deviation from a rule

A skill rule you do not follow, or follow while you believe it is wrong, is evidence the playbook needs. Report it every time, whatever the reason: the rule is wrong for the case, two rules conflict, or the task rules it out. Write one block per rule in your response text, each marker alone on its line:

```
[PLAYBOOK-DEVIATION]
skill: engineering-concurrency
rule: the rule quoted word for word from the skill, then its section
action: deviated
instead: what you did in its place
why: the reason, and the evidence for it
[/PLAYBOOK-DEVIATION]
```

- `action: deviated` — you did not follow the rule. `instead` is required.
- `action: followed` — you followed the rule but believe it is wrong or conflicts with another one. `instead` says what you think the rule should say.
- The Stop hook records every block in a journal outside the project, with the version of the skill you read, and shows the user one line per block. Say it in your final message too, in plain words: the block is the record, and the user still has to read what you did differently.
- Quote the rule exactly, copied from the skill, never paraphrased and never mixed with project names. When the user turned deviation reports on (`reports` in `/playbook-config`), the hook sends the maintainer only a quote it finds word for word in the skill's text; a paraphrase stays in the local journal and the playbook never learns of it.
- A deviation does not lift a hook gate.

---

## Before implementing

If a simpler approach exists, surface it explicitly before starting. Push back when the requested approach is more complex than the problem warrants. Do not silently implement the complex path.

Choosing between valid interpretations of the task is an open decision unless the request delegated it (Decision ownership): present them, or, under a delegation, pick one and record why. Never pick one silently.

For tasks with more than one step, state the plan first:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Confirm the plan with the user unless the request delegated it. Weak success criteria ("make it work") are an open decision too.

---

## Obligations

Before the first write of a change, post its obligations: what the change must keep true, and the test that proves it. The checklist of `engineering-implement` opens with this table; a change without that checklist posts the table alone. One row per item the change touches:

| The change | Obligation | Proof |
|---|---|---|
| writes a stored state (a row, a counter, a balance, a status) | the invariants of that state, and every writer of it in the codebase, existing and new, enforcing all of them in the same atomic step as its write | a test that runs the writers interleaved and asserts the invariants afterwards |
| accepts an input (a field, a parameter, a message, a file) | its valid domain as an allowlist: type, range, size, and its relation to now and to the state it refers to. Everything outside it is rejected. A domain defined outside the project (a document number, a plate, a currency or protocol code) is read from its current source, never from memory | a test on each boundary of the domain, on both sides |
| runs work over data (a query, a loop, a write of a collection, an external call) | a cost bounded by what the work changes or returns (the page, the batch, the changed children), never by the total size of a table, an aggregate or a response; where input sets the size, a hard cap enforced in code | a test above that size (an aggregate with many children, a table larger than a page) asserting the work stays bounded: rows written, queries issued, items read |
| declares or uses something (a package, an export, a config key, a port; the clock, randomness, the environment, the network, files) | everything declared has a consumer in the change; a value the logic decides on that varies between runs (the current time, randomness that drives a branch, the environment, a remote answer) reaches the domain and application layers as an input, a parameter or a port. A value only stored or passed on (a generated id, an `updatedAt` stamp) may come from the native runtime | domain and application tests run with a fixed clock and a fake for every port; the project's unused-dependency or unused-export check, when it has one, reports nothing new |

- **Invariants belong to the state, not to the flow.** A limit one flow checks binds every flow that writes the same state, so find the writers by searching the codebase for writes to that state, not by the flows the task names. A limit on one operation bounds an aggregate (per holder, per period); its window is a domain decision (Decision ownership).
- **A row without its proof is not done.** The proof is a test in the change. `engineering-code-review` checks the same obligations against the diff.

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

- Taking a decision you do not own (Decision ownership): assuming a requirement the request did not state, or changing one it fixed.
- Inventing behavior not requested.
- Creating features outside the stated scope.
- Modifying artifacts not referenced in the task.
- Making architecture decisions without justification.
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
- [ ] Every decision was taken by its owner: fixed ones as they stand, delegated ones decided and recorded, open ones answered by the user.
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

If a checklist item cannot pass because an open decision is unanswered: stop, report what is blocking, and ask. Do not deliver a partial response as if it were complete.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
