# Codex Adapter

> **Experimental.** The playbook is built for and tested on Claude Code. This
> adapter reuses its skills and Python hooks, but covers fewer gates (see the
> Codex parity notes in the README), is tested less and may break when Codex
> changes.

The Codex adapter wraps the original `engineering-*` skill catalog for Codex CLI. It exposes the same skills as the Claude Code plugin and adds lifecycle hooks that enforce the playbook governance model.

## Install

### Recommended (manual / dev workflow)

Use `scripts/install-codex.sh` when you are iterating on the playbook from this
repository and want every edit to take effect on the next Codex run:

```bash
# Snapshot mode (default) — cache is a copy of the repo at install time;
# edits do NOT propagate. Re-run to refresh, or use --refresh.
./scripts/install-codex.sh

# Dev mode — replaces the cache's hooks/ and skills/ with symlinks to the
# repo, so edits take effect on the next Codex run without reinstalling.
./scripts/install-codex.sh --dev

# Custom plugin name (useful when running multiple variants side by side)
./scripts/install-codex.sh --plugin playbook-dev

# Uninstall (--plugin first if it was installed under a custom name)
./scripts/install-codex.sh --plugin playbook-dev --uninstall
```

The script always overwrites the installed `plugin.json` and `hooks.json` from
the repo, so all lifecycle events stay wired (`SessionStart`,
`UserPromptSubmit`, `PreToolUse`, `Stop`, `SubagentStop`, `PreCompact`, `PostCompact`). The
script also verifies the installed `hooks.json` and refuses to proceed if
any event is missing.

### Plugin marketplace (`codex plugin add`)

The playbook is not in Codex's official plugin registry yet (soon). These
steps use Codex's plugin commands with a clone of this repository as a local
marketplace.

1. Add the local marketplace (single `SOURCE` argument — verified against Codex 0.147.0; the marketplace name comes from `.agents/plugins/marketplace.json`'s own `name` field, not a CLI argument):

  ```bash
   codex plugin marketplace add /path/to/engineering-playbook
   ```

2. Install and enable the plugin:

   ```bash
   codex plugin add playbook@playbook
   ```

3. Run Codex normally. On the first interactive invocation, Codex shows a hook review screen. Choose "Trust all and continue" to persist the hook hashes.

   For non-interactive automation, Codex silently skips untrusted hooks unless the first run is done with:

   ```bash
   codex exec --dangerously-bypass-hook-trust "prompt"
   ```

   There is no native auto-trust config key in Codex 0.147.0. The bypass flag does not persist trust; automation either needs the flag per invocation or a one-time interactive trust step after hook hashes change.

## What You Get

- All 40 `engineering-*` skills are registered explicitly in `.codex-plugin/plugin.json` (one path per skill, matching `.claude-plugin/plugin.json`'s array) — not a bare directory string, which is not guaranteed to be expanded by every Codex version. Verified against a live Codex 0.147.0 session: all 40 appear in Codex's own `<skills_instructions>` block.
- SessionStart injects a curated subset of `engineering-playbook/SKILL.md` and `engineering-governance/SKILL.md` (non-negotiable rules, skill routing table, spec-gate mechanics, validation checklist) — not a paraphrase, and not the full files either. Codex's hook contract truncates `additionalContext` above a default ~2500-token budget (confirmed at [developers.openai.com/codex/hooks](https://developers.openai.com/codex/hooks)); injecting both files in full was verified to get silently middle-truncated, dropping exactly the routing table and spec-gate section. `.codex-plugin/hooks.json` raises this hook's `additionalContextLimit` to 6000 tokens and `hooks/codex_session_start.py` extracts only the sections that apply to Codex (see the file's docstring for what was dropped and why) — verified against a live session to land with zero truncation at 10486 chars / ~2621 tokens.
- UserPromptSubmit reinforcement on every prompt (no-hallucination, scope discipline, spec-gate and validation-gate reminders).
- PreToolUse: records spec-pending and pending-validation state for `Write`, `Edit`, `MultiEdit`, `ApplyPatch` on source files, and destructive-command / commit-attribution block for `Bash` (the attribution block also denies a `git push` that would publish a commit carrying attribution; with no PostToolUse in this adapter, the push is its only check on history). All hooks are Python (`hooks/codex_*.py`) and import the checks and patterns directly from `hooks/playbook_core.py` — the same module `hooks/context_guard.py` uses for Claude Code — instead of re-implementing them or shelling out to a subprocess. It does not deny writes for spec/validation reasons itself — see Stop gate below.
- Stop gate, two independent checks, first one that fails wins:
  - **Spec gate**: blocks if a code write happened without `[SPEC CONFIRMED]` in the response text. Mirrors `hooks/context_guard.py`'s `_check_spec_gate`: the token is assistant text emitted in the same turn as the write, not flushed to the transcript until the turn's tool calls resolve, so a PreToolUse check would false-positive deny a write that just confirmed the spec — this is the only point where the spec gate denies.
  - **Validation gate**: blocks if a code write happened and no validation command (`tsc`, `lint`, `test`, etc. — `playbook_core.VALIDATION_RE`, shared with Claude Code) ran afterward.
  - Session state (`~/.codex/hooks/.state-<session_id>.json`) carries both signals from PreToolUse to Stop.
  - Subagents: a subagent's hook calls carry its parent's `session_id` (captured live on Codex 0.153.0), so it gets its own state (`.state-<session_id>-<agent_id>.json`) and its gates run at `SubagentStop` against `agent_transcript_path` — its writes never block the parent's turn, and its validation never clears the parent's gate.
- Suspension: stating that you are invoking `playbook-off` in your response text suspends every check above (destructive block, spec gate, validation gate) for the rest of the session, mirroring Claude Code's `/playbook-off`. Resume with `playbook-on` or `engineering-playbook`. Detected via a raw transcript scan for the skill names (Codex has no Skill tool to detect a formal invocation the way `context_guard.py` does for Claude) — see the "Known limitations" note in `hooks/playbook_core.py`'s `is_suspended`.
- PreCompact/PostCompact: acknowledges Codex's own compaction (`trigger: manual` or `auto`) via `systemMessage`. Does not attempt to reimplement `context_guard.py`'s token-percentage `[playbook:context]` warn/block — Codex's documented hook contract exposes no token or usage counts to any hook, so there is no data to build that on. SessionStart already re-fires after compaction (`source: "compact"`), so governance content is re-injected for free; `codex_compact.py` just surfaces that it happened.
- Kill switch and granular toggles through `~/.codex/config.toml`.

## Configuration

Governance defaults to ON. To disable the whole adapter:

```toml
[playbook]
governance = false
```

To disable only the PreToolUse spec gate:

```toml
[playbook]
spec_gate = false
```

To disable only the destructive-command block:

```toml
[playbook]
destructive_block = false
```

The validation gate and suspension detection are not covered by a config
toggle yet — disable them for a session with `playbook-off`, or
persistently with `governance = false`.

## Known conflicts

### A third-party CLI registers a Claude Code hook into Codex's `~/.codex/hooks.json` (Aug 2026)

**Symptom:** every Codex PreToolUse on a `Bash` tool logs

```
PreToolUse hook (failed)
error: PreToolUse hook returned updatedInput without permissionDecision:allow
```

**Source:** a third-party command-rewriting CLI whose Codex installer writes
its Claude Code hook processor (`<tool> hook claude`) into Codex's global
`~/.codex/hooks.json`. Claude Code's hook I/O contract differs from Codex's:
Claude accepts `updatedInput` alone as a silent rewrite signal, Codex 0.147.0
requires `permissionDecision` alongside `updatedInput`. Codex therefore
rejects the payload with the message above.

**How `scripts/install-codex.sh` defends against this:** every install/refresh scans `~/.codex/hooks.json` for `<tool> hook (claude|cursor|gemini|copilot)` entries and prints a stderr warning naming the offending line. The warning is non-fatal so the install still completes; the user is responsible for removing the bad hook.

**How to fix (pick one):**

1. Remove the entry from `~/.codex/hooks.json` manually. It looks like:
   ```json
   {
     "matcher": "Bash",
     "hooks": [
       {"type": "command", "command": "<tool> hook claude"}
     ]
   }
   ```
2. Run that tool's own uninstall for its Codex integration.
3. To keep the tool for Claude Code only, leave its Claude Code installation (`~/.claude/settings.json`) and keep Codex's `~/.codex/hooks.json` Codex-native.

Why the playbook detects but does not auto-fix: a foreign-hook entry can be intentional (a user with both Claude Code and Codex installed may mean it for the former). The playbook gives a loud warning and lets the user choose.

## Limitations

- Codex CLI does not expose slash commands for installed skills, so users invoke skills through the normal Codex prompt/tool surface instead of `/engineering-*`.
- SessionStart hooks from other plugins can race with this adapter. The `engineering-scaffold` skill description is written to be the authority on pre-scaffold questions: the user chooses the framework/runtime, the default is `none`, and generic alternative framings (Runtime / Persistence / Entity) are forbidden. If a model weights competing context higher than the skill, the failure mode is the same as any Codex skill failure — the model defaults to general backend knowledge instead of the skill content. The `codex_stop.py` spec gate is the only reliable enforcement: it blocks the turn if a code write happened without `[SPEC CONFIRMED]` in the response text, so the model cannot end a turn having produced code that bypassed the checklist.
- Skills beyond `engineering-playbook` and `engineering-governance` are not injected at SessionStart (only listed by name in the routing table those two carry). Codex has no on-demand Skill tool, so the model must read `skills/<name>/SKILL.md` directly when the routing table points to one — there is no mechanical enforcement that it does so, unlike the spec gate.
- Suspension detection (`playbook-off`/`-on`) and the `[SPEC CONFIRMED]` scan are a substring scan of the transcript tail, not a structured per-entry parse the way `context_guard.py` does for Claude (which detects an actual Skill-tool invocation). Codex's transcript entry schema for a file read has not been confirmed, so this remains an approximation. It is not a naive raw-text scan, though: `playbook_core._drop_non_expressive_lines` first excludes `response_item` entries with `payload.role == "developer"` (SessionStart/UserPromptSubmit's own injected text) and entries whose type is `world_state`/`turn_context`/`session_meta`/`event_msg` (Codex's own bookkeeping — `world_state` was found live to re-serialize the skill catalog, including `playbook-off`'s own name). This exclusion was added after a live session showed the unfiltered scan self-triggering on the adapter's own instructions and internal state — both `[SPEC CONFIRMED]` and `playbook-off` appear in that injected/bookkeeping content as literal strings. Remaining risk: the skill name or spec token appearing in a genuinely kept line (a real assistant/user message) for an unrelated reason.
- No auto-compact / context-percentage tracking. `context_guard.py`'s `[playbook:context]` warn-at-70%/block-at-85% relies on per-turn token usage numbers from Claude's transcript. Codex's documented hook contract (PreCompact, PostCompact, Stop, all of them) does not expose token or usage counts to hooks at all — confirmed via official docs, not just unobserved. There is currently no data path to build an equivalent for Codex.
- `--dangerously-bypass-hook-trust` is still needed for non-interactive runs until hashes are persisted through the interactive hook review screen, or a native auto-trust setting is added to the Codex CLI.
- SessionEnd is not used because Codex clamps its timeout to 3 seconds.
- PreToolUse/Stop firing correctly on a real tool call has not been verified against a live authenticated Codex session — every `codex exec` run in this environment 401s at the model API before any tool call happens. SessionStart, UserPromptSubmit, PreCompact, and PostCompact are verified live (direct invocation and/or a real `codex exec` run); PreToolUse and Stop are only verified via `tests/test_codex_behavior.sh`'s direct hook invocations, which predate confirmation of Codex's real PreToolUse/Stop payload shape from official docs — the confirmed field names (`session_id`, `transcript_path`, `tool_name`, `tool_input`, `tool_use_id`, `hook_event_name`, `model`, `permission_mode`) match what the hooks already assume, which is a good sign, but the gates themselves have not fired against a real tool call.

## Next Phases

- Opencode adapter.
- Agnostic skill extraction shared across Claude Code, Codex, and Opencode.
