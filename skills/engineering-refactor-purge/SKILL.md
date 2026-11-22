---
name: engineering-refactor-purge
description: "Phase 0 of the engineering-refactor workflow. Loaded by the refactor orchestrator when Phase 0 begins. Removes dead code, unused imports, commented-out blocks, and orphan files. Does not restructure, rename, or move anything — only deletes."
version: 2.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Phase 0 — Purge

Goal: eliminate noise so Phase 1 gets a clean signal. Bugs in dirty code produce a false and inflated problem surface.

**Rule: only delete. No restructuring, no renaming, no moving — those are Phase 2 work.**

---

## Checklist

Use bash to identify targets before editing. Never read source files manually to detect dead code — use the compiler/linter and `grep`.

**1. Unused-code detection**

```bash
# TypeScript: run tsc with strict unused flags to get the canonical list
npx tsc --noEmit --noUnusedLocals --noUnusedParameters 2>&1 | grep "error TS"
```

Fix every error reported. These are the unused imports, variables, and parameters.

**2. Commented-out code blocks**

```bash
# Find files with commented-out code (lines starting with the language's line comment marker)
grep -rn '^\s*//' src --include='*.ts' | grep -vE '(TODO|FIXME|NOTE|https?:|^\s*///)' | head -50
```

Review each match. Remove lines that are commented-out code. Leave comments that explain decisions.

**3. Orphan files**

```bash
# Find files with no importers
find src -type f | while read f; do
  base=$(basename "$f")
  count=$(grep -rl "$base" src | grep -v "$f" | wc -l)
  if [ "$count" -eq 0 ]; then echo "$f"; fi
done
```

Verify each candidate before deleting. Exclude framework entry-point files, module/registration files, migrations, and seeds (TypeScript example: `main.ts`, `app.module.ts`, `*.module.ts`).

**4. console.log and debug artifacts**

```bash
grep -rn 'console\.log\|console\.error\|console\.warn\|debugger' src --include='*.ts'
```

Remove all (the pattern above is a TypeScript example). Production code uses the configured logger, not console/debug output.

---

## Completion gate

TypeScript projects: run `tsc --noEmit`. Other languages: run the equivalent compiler/linter unused-code pass. Zero unused-import and unused-variable errors allowed.

If errors remain: fix before declaring Phase 0 complete. Do not carry compiler/linter errors into Phase 1.

---

## Handoff

Write the session handoff block using the format from `engineering-refactor` Step 4. List every file modified or deleted in "Completed". Set "Violations pending" to "none — purge complete, tooling baseline not yet established."

The handoff block must end with:
```
---
**Reply "continue" to begin Phase 1 — Tooling baseline, or ask questions before proceeding.**
```

**Stop here.** Do not describe Phase 1, do not load `engineering-refactor-tooling`, do not continue. Wait for the user's reply, unless the request delegated going on (Decision ownership in `engineering-governance`). Only after they reply: load `engineering-refactor-tooling`.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
