---
name: engineering-code-review
description: "Code review workflow and severity classification. Use when asked to review a diff, PR, or code change, and when asked to review, survey or audit existing code (a module, a route prefix, a folder): the audit mode lists the scope with a script, then runs one pass per dimension and backs every finding with evidence. Runs before every push. Loads relevant skills based on what the code touches and outputs findings classified as Blocker, Major, or Minor."
version: 4.1.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->


# Code Review

Code review applies all relevant skills to the diff and actively checks for violations in every layer. The routing table loads skills for detailed rules; the active checklist below drives what to look for.

---

## When this runs

On request, and automatically before every push — the PreToolUse hook denies `git push` until the current `HEAD` has been reviewed. The review is pinned to the commit: committing again after a review re-arms the gate, because the new commit is code no review has read.

Scope for a pre-push review is `git diff <upstream>...HEAD` — everything the push would publish, not just the last commit — and everything that diff reaches: the pre-push review runs in **audit mode** (below), seeded with that diff.

A review the user asks for runs the **Process** below when its target is a diff or a PR, and **audit mode** when its target is existing code: a module, a route prefix, a folder.

**Bypass.** Only the user can skip it: by running the push themselves, or by starting Claude Code with `PLAYBOOK_SKIP_REVIEW=1` in the environment. Nothing typed in the chat lifts the gate. Never skip it on your own judgment and never suggest the bypass as a way past your own findings.

**Blockers block the push.** Report them and fix them. Do not push with an open Blocker unless the user, after seeing it, says to.

**Findings feed the learn loop.** When a review reports Blocker or Major findings in code written this session, the Stop hook arms the learn gate: the turn cannot end until you name the defect *class* behind the findings — not each finding — write the rule that would have prevented it into the project `CLAUDE.md`, and emit `[LEARNED]`. Fixing a finding is not the same as not repeating it. If the findings share no common cause, say so with `[LEARNED] none — one-off findings`.

---

## Process

1. Read the full diff before forming any opinion.
2. Identify change type: feature / fix / refactor / config / infrastructure / test.
3. Load skills based on what the diff touches:

| Diff contains | Invoke skill |
|---|---|
| Code in the project's implementation language | `engineering-<lang>` (see the language opt-in table in `engineering-playbook`) |
| Architecture changes, layer crossings | `engineering-architecture` |
| Data access, loops, bulk operations | `engineering-performance` |
| Auth, input handling, secrets, external data | `engineering-security` |
| Exception paths, retry, external calls | `engineering-error-handling` |
| Test files | `engineering-testing` |
| Public Contract modifications | `engineering-architecture` (compatibility section) |
| DDD entities, value objects, domain services, use cases | `engineering-ddd` |
| NestJS code (controllers, modules, DI, DTOs, guards) | `engineering-nestjs` |
| New artifacts or possible duplication | `engineering-code-economy` |
| Class design, naming, SOLID principles | `engineering-design-patterns` |
| Concurrent state, idempotency, distributed operations | `engineering-concurrency` |
| Prisma queries, mappers, schema access | `engineering-prisma` |

4. Run the **active review checklist** below against every file in the diff.
5. Run the **repeated-logic sweep** (checklist section *Repeated logic*) over the diff as a whole and against the codebase. Step 4 goes file by file and cannot see two files doing the same thing.
6. For each finding: record file, line, violated rule (with skill reference), what is wrong, what to do instead. A security finding also carries its CWE and, when confirmed, the source-to-sink trace (see *Closing a security candidate*).
7. Check registries: were new artifacts added without registry entries? Were removed artifacts unregistered? Were Public Contracts modified without compatibility analysis?

---

## Audit mode

An audit reviews existing code and everything it reaches. Its goal is every defect in that scope, each one with evidence. A short list that reads as complete is the failure this mode exists to prevent: the scope is listed by a script and walked one question at a time, so nothing depends on remembering to look.

1. **Surface.** List the scope before judging anything:

   ```bash
   python3 <playbook>/audit/surface.py --root <dir> --diff <base>             # before a push
   python3 <playbook>/audit/surface.py --root <dir> --route-prefix /orders    # on request: a route prefix
   python3 <playbook>/audit/surface.py --root <dir> --path src/orders         # on request: a folder or file
   ```

   `<playbook>` is the root of the playbook, two levels above this skill's base directory. `<base>` is the left side of the range the push publishes: the branch's upstream, or the default branch (`origin/HEAD`) for a branch pushed for the first time — the same range the push gate names. `<dir>` is the folder that holds every repository in scope, a front end in a sibling repository included. The output lists the routes, the files that serve them, the write/transaction/lock sites, every caller of the routes, the screens that use those callers, and the recent commits.
   - Exit `3`: the seed matched nothing. Before a push, review the diff with the **Process** above instead; on request, tell the user and ask for another seed.
   - Exit `2`: the seed is invalid; fix it.
   - Every `unknown` entry and every `limits` line goes into the report as written: it is scope this audit did not cover.
2. **Dimensions.** `references/audit-dimensions.json` (next to this file) lists the dimensions. Run them in that order, each as its own pass: its one `question`, asked of every item of the surface fields it `walks`, with the checklist sections it names. Do not fold one dimension into another, and do not end a pass at its first finding: a pass ends when every item it walks was examined or is listed as not examined, with the reason.
3. **Evidence.** Every finding has the shape of `finding_schema` in the same file: the defect's `file:line`, and evidence that is either `reproduced` (the request or the screen steps, expected against actual) or a `code-trace` (the `file:line` steps from the entry point to the effect). A suspicion with neither is not a finding: it goes under the pass's not-examined items, with what would settle it.
4. **Verification.** Try to reproduce every `code-trace` finding the way the project's CLAUDE.md says to exercise the application (a request per role to its local instance, a screen); when it says nothing, the reproduction is a test in the project's own suite. A finding the attempt refutes is dropped. One that cannot be reproduced stays, marked as seen in code, with its trace. Hostile payloads from the `engineering-security` fiches stay under that skill's rules: negative tests, and the lab for dynamic proof.
5. **Report.** The findings by severity in the **Output format** below, then the **Coverage** block. Findings outside the scope and findings that depend on a business rule are listed apart and not fixed.

Before a push, the audit is the review: its `code-checklist` dimension covers the files of the diff, and step 7 of the **Process** (registries) still applies.

---

## Active review checklist

Work through every section. Mark each finding immediately — do not batch.

### Layer boundary violations

- Use case imports a concrete infrastructure class (`PrismaXRepository`, `RedisClient`, etc.) — must import the interface token only.
- Use case imports presentation-layer types (`Request`, `Response`, `HttpException`, HTTP DTOs).
- Use case calls another use case directly — use cases must be orchestrated by a controller or event consumer, not by each other.
- Domain entity or value object imports a framework, ORM, or library — domain layer must have zero external dependencies.
- Repository implementation returns a raw ORM model type — must always map to a domain entity before returning.
- Controller imports domain repositories, infrastructure services, or Prisma directly — controllers call use cases only.
- `forwardRef` used anywhere — indicates a circular dependency that must be resolved structurally, never worked around.
- Relative `../../` import crossing layer or module boundaries — use the configured path alias (`@domain/*`, `@infrastructure/*`, etc.).
- Infrastructure exception (DB error, HTTP client error) not caught and rethrown as a domain exception at the boundary. (see `engineering-error-handling`)

### Domain layer

- Entity constructor is not `private` — must be private; all instantiation goes through factory methods.
- Factory method named `create()` — must be a semantic business action: `register()`, `activate()`, `suspend()`.
- Public setter on entity props — state changes must go through named domain methods.
- Business rule (invariant check, default assignment, role logic) inside use case — belongs in the entity.
- Value object constructor is public — must be private; construction goes through a static factory. (see `engineering-ddd`)
- Value object returns `null` or `undefined` on validation failure — must throw a domain exception; invalid VOs must never exist. (see `engineering-ddd`)
- Persistence mapper calls a business factory (`User.register()`) to reconstruct from DB row — must call `User.reconstitute()`.
- Persistence mapper accesses a VO directly instead of its unwrapped value (e.g., `toPrisma` receives the VO object rather than `user.email.value`).

### Application layer

- Use case returns a raw persistence/ORM model type (e.g., Prisma model) — must return a domain entity or an output DTO.
- Use case calls ID generation, hashing, or any other infrastructure utility directly (e.g., `randomUUID()`, `bcrypt`) — belongs in the entity factory or a domain service implementation. (see `engineering-ddd`)
- Multiple repository writes for related entities without a transaction — if the second write fails, the first orphans data.
- Use case contains `if/else` chains that enforce business invariants — belongs in the entity.

### Infrastructure layer

- Repository implementation has business logic or domain rule enforcement — its only job is query and map.
- Mapper `toDomain` calls `Email.create(raw.email)` (validates) instead of `Email.reconstitute(raw.email)` (bypasses) — loading from persistence is not a business action. (see `engineering-ddd`)

### Presentation layer

- Controller method contains business logic beyond HTTP ↔ use case mapping.
- DTO passed directly into a use case — must map to the use case's internal input DTO at the controller boundary.
- DTO validation missing on any field (NestJS: `class-validator` decorators).
- Length cap missing on user-facing text input fields (NestJS: `@MaxLength`).
- API docs metadata absent, or success/error responses missing on any public endpoint (NestJS/Swagger: `@ApiTags`, `@ApiOkResponse`, etc.).
- POST endpoint that queries (does not create) returns the wrong status or lacks a documented success response (NestJS: `@HttpCode(HttpStatus.OK)` and `@ApiOkResponse`).

### NestJS-specific

- `REQUEST` scope on a service without explicit justification — default must be `DEFAULT` (singleton).
- Global providers (`PrismaService`, `Logger`, `Redis`) re-provided or re-imported in a feature module. (see `engineering-prisma`)
- Mutable instance variable on a service (e.g., `private results: T[] = []`) — singletons are shared across all requests; this is a race condition.
- `console.log` anywhere in production code — use the injected structured logger.
- `process.env` accessed outside the config validation module — inject `ConfigService`.
- `@Inject` applied to a concrete class instead of an interface injection token.

### Concurrency and atomicity

- Read-modify-write that is not atomic: load entity → mutate → save without optimistic lock (`version` field) or pessimistic lock (`SELECT FOR UPDATE`).
- Idempotency implemented with `GET` + conditional `SET` — there is a race window; the correct pattern is `SET NX` atomic claim before execution.
- Redis rate limiting using `GET` + conditional `SET` — use `INCR` + `EXPIRE on first increment`.
- Distributed lock released with a plain `DEL` — must use a Lua check-and-delete to avoid releasing another holder's lock.
- State-mutating POST endpoint without idempotency handling when called by external clients.
- Retryable operation (consumer handler, webhook receiver, job) whose second execution is not a no-op — no idempotency key, no natural unique constraint, no state check.
- Multi-step write where a failure between steps leaves the system in a state no reader can interpret — the steps must share a transaction, or the operation must be replayable to completion.
- Write to the database and a side effect (event published, email sent, file written) in the same operation with no outbox, no compensation, and no ordering guarantee — a crash between them silently diverges the two systems.
- Transaction held open across a network call to another service.

### Performance and N+1

- Loop that makes a DB, Redis, or HTTP call per iteration — must batch the I/O outside the loop.
- Database/ORM query inside a loop without batching (e.g., Prisma query) — N+1 query.
- Database/ORM query fetching all fields or unnecessary relations when only a subset is needed.
- Any endpoint that returns a list without pagination or a configurable limit.
- Pagination applied in memory: the full collection is fetched and then sliced, filtered, sorted or counted in application code — `LIMIT`/`OFFSET` (or cursor) and `ORDER BY` must reach the database.
- Aggregation (count, sum, max, group) computed by loading rows and reducing them in application code.
- Unbounded read into memory: a whole table, a whole file, or an unbounded external response materialized before processing — stream or page it.

### Reuse before creation

- New function, class, DTO, constant or utility that duplicates something already in the codebase — search before accepting any new artifact. (see `engineering-code-economy`)
- A hand-rolled implementation of something the project's existing dependencies already provide.
- A second way to do something the codebase already has one way to do (two HTTP clients, two date helpers, two error-mapping paths).

### Repeated logic

For every function, method or block of logic the diff adds or changes, search for the same behavior elsewhere: first in the rest of the diff, then in the codebase. Search by what the code does, not by what it is called — grep the distinctive calls, operators, literals and field names, because a duplicate rarely shares the identifier.

- Two artifacts in the same diff implement the same computation or rule (two classes that each total a list of amounts, two services that each normalize the same identifier) — extract one function and call it from both.
- Logic in the diff repeats logic that already exists under another name, in another module or in another layer — call the existing one; if it is private to its owner, extract it and make both depend on the extraction.
- The same sequence of steps written more than once with only a type, field or constant changing — one parameterized function.
- The same validation, mapping, guard clause or error translation written inline in more than one caller.
- The same literal, regex or threshold, with the same meaning, defined in more than one place — one named constant.

Report it only when the occurrences encode the same rule and would have to change together. Code that looks alike but answers to different reasons to change stays separate — merging it couples two things that will diverge (see the minimum code mandate in `engineering-code-economy`).

Each finding lists every occurrence as `file:line` and names where the shared code should live: the lowest layer every caller is already allowed to depend on. An extraction that makes a caller import across a layer boundary is not a fix.

### Dead code

- Function, class, export, constant or file that nothing references after this change.
- Code left unreachable by the change (branch that can no longer be taken, condition that is now always true or always false).
- Commented-out code block, or a `TODO`/`FIXME` left as the only implementation of a stated requirement.
- Parameter, field or config key that is passed and never read.
- Feature flag or branch whose other side was deleted.
- Import, dependency or test helper that no longer resolves to anything used.

### Comments and layout

The rule is in `engineering-governance` (*Writing code*): a comment states an accepted or known trade-off, or marks a palliative fix to be redone. Check every comment the diff adds.

- Comment that describes what the code does, or says a name again in prose.
- Doc block added to a member whose name and signature already say it.
- Comment that narrates history: a date, a ticket or item number, what the code used to do, who asked for it — that text belongs in the commit message.
- Comment that explains code which a rename or a small restructure would make plain.
- Hunk that only re-wraps lines, re-orders imports or changes quote style by hand — the project's formatter owns layout.

### Algorithmic cost

- Nested iteration over collections that both grow with input size — build a lookup map instead of scanning the inner collection per outer item.
- Repeated linear scans of the same collection where one pass, or one index built up front, would do.
- Sort, regex compilation, or object construction inside a loop that does not depend on the loop variable.
- String concatenation in a loop over a large collection.
- Work whose cost grows with total dataset size when it only needs to grow with the page or batch being processed.

### Information disclosure

- Sensitive data (credentials, tokens, PII, raw user input) logged at any level.
- Internal error detail reaching the client: stack trace, ORM/driver message, SQL text, file path, or the name of an internal service in an API response.
- Domain entity or persistence model serialized straight into a response, exposing fields the endpoint never meant to publish (password hash, internal flags, soft-delete markers, other tenants' identifiers).
- Error message that distinguishes "user not found" from "wrong password", or otherwise confirms the existence of a resource to someone not authorized to know.
- Debug or diagnostic output left enabled: `console.log`/`print`/`println!`/`fmt.Println` or equivalent in production code — use the injected structured logger. (language-agnostic; the NestJS-specific form is listed above)
- Secret, key, connection string or internal hostname committed to a tracked file, including test fixtures and `.env.example`.

### Security
- Missing auth check/guard on an endpoint that reads or mutates user-owned or privileged data.
- Raw SQL query using string interpolation or concatenation — must use parameterized queries.
- Auth endpoint or resource-intensive operation without rate limiting.
- Hardcoded secret, API key, or credential anywhere in code.
- The diff touches a sink listed in the `engineering-security` fiche table: read that fiche and check its **Review signal** list against the diff.
- A security control was added or changed without the negative tests from its fiche (see `engineering-testing`).

**Security scope of a diff.** Besides the lines it adds, a diff is in scope for security when it does any of these:
- makes a pre-existing sink newly reachable from input (a new route to an old raw query, a new caller of an unchecked path builder);
- weakens a shared guard, validator, sanitizer or policy (a broader pattern, an added exception, a check moved behind a flag);
- removes or narrows a control (a deleted guard, a disabled verification, a raised limit).

Pre-existing issues the diff neither touches nor reaches stay out of scope.

**Closing a security candidate.** Every candidate ends in exactly one of three states:
- **Confirmed.** Give the path from the input source to the sink as `file:line` steps, with no control on it. Report it as a Blocker.
- **Ruled out.** Name the control that holds on every path from input to the sink, with its `file:line`. "The framework or ORM handles it" rules nothing out unless it says where and how.
- **Open proof gap.** Neither of the above could be shown, for example because the path runs through code or configuration that is not in the repository. Report it under Major as `proof gap:`, with what is missing and what would settle it. Never drop it silently.

### Language and naming

- Dynamic/loose-typed value in production code (TypeScript: `any`; test files may have an override).
- Missing explicit return type on a public method, exported function, or callback.
- Async exception/result ignored — not awaited, handled, or explicitly ignored (JavaScript/TypeScript: floating promise). (see `engineering-typescript`)
- Await/blocking I/O inside a loop over many items — does not parallelize (JavaScript/TypeScript example: `await` inside `Array.map`; use explicit parallel execution when appropriate). (see `engineering-typescript`)
- Unsafe non-null assertion or unchecked unwrap (TypeScript: `!`).
- Magic number or string literal — declare a named constant.
- Boolean variable without `is`/`has`/`can`/`should`/`was` prefix.
- Variable named after its type instead of its content (`data`, `list`, `obj`, `result` without context).
- Method named with a vague or missing verb (`process()`, `handle()`, `doStuff()`).
- Abbreviation in an identifier that is not a universally known domain term (`HTTP`, `DTO`, `ID`, `URL`, `API`).

---

## Finding severity

| Severity | Definition | Examples |
|---|---|---|
| **Blocker** | Must be fixed before merge | Security vulnerability, broken contract, data loss risk, incorrect behavior, missing auth, race condition on shared state |
| **Major** | Should be fixed before merge | N+1 query, layer boundary violation, missing test coverage for critical path, unhandled error path, mutable singleton state, business rule implemented in more than one place, comment outside the two allowed reasons |
| **Minor** | Can be addressed in follow-up | Style issue, naming improvement, non-critical optimization, optional test improvement, repeated mechanical helper with no business rule in it |

---

## Output format

```
## Blockers
- `path/to/file:42` — [skill] — [what is wrong] — [fix]
- `path/to/file:42` — engineering-security — CWE-918 — [what is wrong] — trace: `routes/a.ts:10` → `fetcher.ts:42` — [fix]

## Major
- ...
- `path/to/file:42` — engineering-security — proof gap: [what could not be shown] — [what would settle it]

## Minor
- ...

## Registry (only if the project has a `registries/` directory)
- [ ] registries/services.md — add entry for NewService
- [ ] registries/contracts.md — update PayloadDto (field added)

## Coverage (audit mode only)
- authorization — examined 7 routes; not examined: `GET /orders/export` (needs a role the seed data lacks)
- ... one line per dimension
- surface unknown: `api/legacy.go:12` — route declarations in a stack the surface reader does not parse (go)
- surface limits: mount and global prefixes are not applied ...
```

If a section has no findings: write "none". In audit mode, a finding marked seen in code says so after its location: `path/to/file:42` (seen in code).

---

## Rules

- Do not approve with open blockers.
- Do not classify style issues as blockers.
- Do not comment on code outside the review scope: the diff, or in audit mode the surface. A repeated-logic finding is anchored on the diff line and may cite an occurrence outside the diff as evidence; duplication between two places the diff does not touch is out of scope.
- Do not suggest refactoring unrelated to the change under review.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
