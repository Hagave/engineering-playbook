---
name: engineering-learn
description: "Mines the hook failure journal and real Claude Code session logs for the active project, extracts failure→success patterns, and writes specific corrections back into the project CLAUDE.md. Run it from /playbook-config (Learn) after a problematic session or periodically to keep playbook rules grounded in real project behavior."
version: 2.0.0
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->

# Engineering Learn

Offline failure analysis. Reads real session logs, finds what went wrong and what fixed it, writes actionable corrections into the project so the same mistake cannot repeat next session.

Two inputs, in order of cost: the hook failure journal (pre-extracted, cheap, start here) and the raw session transcripts (expensive, for anything the journal cannot explain).

---

## Two blocks in CLAUDE.md, two owners

This skill writes `<!-- engineering-learn:start -->`…`<!-- engineering-learn:end -->` — a mined, regenerated block.

The turn-end learn gate writes `<!-- engineering-learn:live -->`…`<!-- /engineering-learn:live -->` — rules distilled in the moment a correction happened, one line each.

**Never regenerate, reorder or delete the `:live` block.** It holds evidence this skill cannot recover: what the user said was wrong, at the moment they said it. When mining promotes a `:live` rule into the mined block (same correction, now confirmed by log evidence), remove only that one line from `:live` and say so in the summary.

---

## Step 0 — Read the failure journal

`learn_guard.py` records every failed tool call, every repeated failure and every user correction as it happens. This is already-extracted signal — read it before touching raw transcripts.

```bash
python3 - <<'EOF'
import glob, json, os, collections

cwd = os.getcwd()
rows = []
for path in glob.glob(os.path.expanduser('~/.claude/hooks/.learn-*.jsonl')):
    with open(path) as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            # The journal is keyed by session, not by project — filter here.
            if row.get('cwd') and row['cwd'] != cwd:
                continue
            rows.append(row)

rows.sort(key=lambda r: r.get('ts', 0))
print(f'{len(rows)} incidents for {cwd}')
print(collections.Counter(r.get('kind') for r in rows))
for r in rows[-40:]:
    kind = r.get('kind')
    if kind == 'correction':
        print(f"[correction] {r.get('prompt', '')}")
    else:
        print(f"[{kind}] {r.get('tool')} x{r.get('count', 1)}: {r.get('error', '')[:160]}")
        print(f"            input: {r.get('input', '')[:160]}")
EOF
```

`correction` and `repeat_failure` rows are the high-value ones — each already has a user or the hook asserting that something was wrong. Single `tool_error` rows are raw material: only a cluster of them around the same file, command or assumption is a pattern.

If the journal is empty, the hook has not run in this project yet. Continue to Step 1 and mine the transcripts directly.

---

## Step 1 — Locate log files

Session logs live in different places per harness. Check the sources in order and use the first one that has files.

**Claude Code** — project-scoped directory derived from the working directory:

```bash
# Replace / with - and prepend -
# Example: /home/user/projects/myapp → -home-user-projects-myapp
cwd=$(pwd)
hash=$(echo "$cwd" | sed 's|/|-|g')
log_dir="$HOME/.claude/projects/$hash"
ls "$log_dir"/*.jsonl 2>/dev/null | sort -t/ -k1 | tail -5
```

**Google Antigravity** — conversation-scoped, not project-scoped. Brain directories are per-conversation, so filter transcripts by workspace path:

```bash
cwd=$(pwd)
for base in "$HOME/.gemini/antigravity" "$HOME/.gemini/antigravity-cli"; do
  [ -d "$base/brain" ] || continue
  grep -l "$cwd" "$base"/brain/*/.system_generated/logs/*.jsonl 2>/dev/null
done | tail -5
```

If no source has `.jsonl` files, report it and stop — there is nothing to analyze.

**Schema note:** the Step 2 extraction script targets the Claude Code entry schema (`type: assistant/user`, `tool_result` blocks). Antigravity transcripts serialize the same concepts (user requests, agent responses, tool calls, errors) with a different entry shape — before running Step 2 against Antigravity logs, read the first lines of one transcript and adapt the extraction keys. Steps 3–5 (correlation, corrections, idempotent write) are schema-independent and apply unchanged.

---

## Step 2 — Extract failure events

Run this script. It reads the 5 most recent session files and extracts tool calls where the result was an error, plus the assistant turn that immediately followed:

```bash
python3 << 'PYEOF'
import json, os, glob, sys

cwd = os.getcwd()
hash_path = cwd.replace('/', '-')
log_dir = os.path.expanduser(f"~/.claude/projects/{hash_path}")
files = sorted(glob.glob(f"{log_dir}/*.jsonl"))[-5:]

if not files:
    print("No session logs found.")
    sys.exit(0)

failures = []

for fpath in files:
    entries = []
    with open(fpath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                pass

    # Build ordered turn list: user + assistant entries only
    turns = [e for e in entries if e.get('type') in ('user', 'assistant')]

    for i, turn in enumerate(turns):
        if turn.get('type') != 'user':
            continue
        msg = turn.get('message', {})
        content = msg.get('content', [])
        if not isinstance(content, list):
            continue

        # Find tool_result blocks with is_error=true
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') != 'tool_result':
                continue
            if not block.get('is_error'):
                continue

            # Get the tool call from the preceding assistant turn
            tool_name = 'unknown'
            tool_input = {}
            if i > 0 and turns[i-1].get('type') == 'assistant':
                prev_msg = turns[i-1].get('message', {})
                for b in prev_msg.get('content', []):
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        tool_name = b.get('name', 'unknown')
                        tool_input = b.get('input', {})

            # Get error content
            err_content = block.get('content', '')
            if isinstance(err_content, list):
                err_content = ' '.join(
                    c.get('text', '') for c in err_content if isinstance(c, dict)
                )

            # Get the next assistant turn (the fix attempt)
            next_assistant_text = ''
            for j in range(i+1, min(i+4, len(turns))):
                if turns[j].get('type') == 'assistant':
                    next_msg = turns[j].get('message', {})
                    for b in next_msg.get('content', []):
                        if isinstance(b, dict) and b.get('type') == 'text':
                            next_assistant_text = b.get('text', '')[:300]
                    break

            failures.append({
                'session': os.path.basename(fpath),
                'tool': tool_name,
                'input': str(tool_input)[:200],
                'error': str(err_content)[:200],
                'fix_attempt': next_assistant_text,
            })

print(f"Found {len(failures)} failure events across {len(files)} sessions.\n")
for i, f in enumerate(failures[:30], 1):
    print(f"[{i}] Tool: {f['tool']}")
    print(f"    Input:   {f['input']}")
    print(f"    Error:   {f['error']}")
    print(f"    Fix:     {f['fix_attempt'][:200]}")
    print()
PYEOF
```

If 0 failures are found, report "No failures detected in recent sessions" and stop.

---

## Step 3 — Correlate failures to corrections

For each failure, identify whether the session recovered:

- **File path error** (Read/Edit on a path that doesn't exist): look for a subsequent successful Read/Edit on a different path for the same file name. Learning: `<filename> is at <correct-path>, not <wrong-path>`.
- **Bash command error** (exit code ≠ 0): look for a subsequent Bash with different arguments that succeeded. Learning: "use `<correct-command>` not `<failed-command>`".
- **Repeated failure** (same tool+input failed more than once): Learning: "do not attempt `<input>` — always fails in this project".
- **Permission rejection** (user denied a tool call): Learning: "do not auto-run `<command>` — show to user and wait for approval".

Group learnings by category:
- `File path corrections`
- `Command patterns`
- `Search scope`
- `Permission restrictions`

Discard failures with no recovery correlation — they are noise.

---

## Step 4 — Write corrections

Check if the project has a `CLAUDE.md` at the working directory root. If not, check parent directories up to two levels.

**If `CLAUDE.md` exists:** write the learnings block using idempotent markers — if the markers already exist, replace only the content between them. Leave every other part of the file, including the `:live` block, byte-for-byte untouched:

```markdown
<!-- engineering-learn:start -->
## Learned patterns
*Auto-generated by Learn (`/playbook-config`) — do not edit manually. Re-run to update.*

### File path corrections
- `<filename>` → `<correct-path>` (was guessed as `<wrong-path>`)

### Command patterns
- Use `<correct-form>` — `<failed-form>` fails in this project

### Search scope
- Search in `<correct-dir>` not `<wrong-dir>`

### Permission restrictions
- Do not auto-run `<command>` — present to user for manual approval
<!-- engineering-learn:end -->
```

**If no `CLAUDE.md` exists:** do not create one. Display the block above with the actual patterns found and ask the user: "No CLAUDE.md found in this project. Paste this block into your project's CLAUDE.md to persist these learnings, or run Learn in `/playbook-config` again after creating the file."

Never create project-level files without explicit user confirmation — rule owned by the registry protocol in `engineering-playbook`. For this skill specifically: a `CLAUDE.md` created without confirmation can conflict with skill rules and be accidentally committed to version control.

After writing (or displaying), print a summary: N patterns found, file written or displayed, categories covered.

---

## What this skill does NOT do

- Does not analyze sessions from other projects.
- Does not modify playbook skill files — only project-level `CLAUDE.md`.
- Does not invent patterns. Every learning must trace to a real failure→success sequence found in the journal or the logs.
- Does not rewrite or delete the `<!-- engineering-learn:live -->` block — only removes individual lines it has promoted into the mined block.
- Does not delete journal files. They are session-scoped and trimmed by the hook.
- Does not write generic advice. "Always read before editing" is not a learned pattern — it is already a playbook rule.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
