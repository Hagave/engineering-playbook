# Google Antigravity adapter

> **Experimental.** The playbook is built for and tested on Claude Code. This
> adapter covers the skills only (plus optional git hooks), is tested less and
> may break when Antigravity changes.

Installs the Engineering Playbook into a [Google Antigravity](https://antigravity.google) workspace. The skills in `skills/` remain the single source of truth — this adapter transforms and copies them; it never forks their content.

## Install

```bash
./adapters/antigravity/install.sh /path/to/your-project

# with mechanical enforcement via git hooks (recommended):
./adapters/antigravity/install.sh --git-hooks --ci /path/to/your-project
```

What it produces in the target workspace:

| Artifact | Purpose |
|---|---|
| `AGENTS.md` (marker block) | Always-loaded core: non-negotiables, concept mapping, skill routing table, proportional model routing. Idempotent — re-running replaces the block, preserving anything else in the file. |
| `.agents/skills/engineering-*.md` | All skills, YAML frontmatter stripped (Antigravity skills are plain markdown). |
| `.agents/workflows/*.md` | Slash commands: `/engineering-playbook`, `/playbook-on`, `/playbook-off`, `/playbook-config`. |
| `.git/hooks/commit-msg` + `pre-commit` (with `--git-hooks`) | Mechanical enforcement independent of the IDE: blocks AI co-author attribution through the playbook's own check (`scripts/check_commit_message.py`; where git runs the hook without `python3` 3.10+ or the clone, the same rule, rendered into the hook at install time, still blocks); runs `tsc --noEmit` + lint before every commit. `git commit --no-verify` skips both once, and no environment variable does: a hook inherits the environment of the `git commit` the agent runs. Foreign hooks are never overwritten. |
| `.github/workflows/engineering-playbook.yml` (with `--ci`) | The same two checks on every pull request, where nothing local can skip them: AI attribution in the PR's commits or body, and `tsc --noEmit` + lint. The attribution rule is rendered from `hooks/playbook_core.py` at install time. Make its `policy` check required in branch protection. A foreign workflow with the same name is never overwritten. |

Re-run the installer after updating skills in this repository.

## Design decisions

**AGENTS.md as the anchor, not `.agents/rules/`.** Sources disagree on the rules directory name (`.agent/rules/` vs `.agents/rules/`), while root `AGENTS.md` is officially supported (Antigravity v1.20.3+) and read by other tools too. The core block therefore lives in `AGENTS.md`.

**Skills copied verbatim (minus frontmatter), mapped by one preamble.** The skill files reference Claude Code concepts (Skill tool, hooks, CLAUDE.md). Instead of rewriting 34 files per target — which would drift — the AGENTS.md core carries a concept-mapping section that translates those references for Antigravity. One mapping, zero forks.

**Proportional model routing.** Antigravity offers models from multiple providers (Gemini, Claude, OSS). The routing rule is expressed as capability tiers — frontier tier for decisions, fast tier for pure extraction — with the qualification criteria deciding what may be delegated. No model names are hardcoded; the table degrades gracefully to a single-model session.

## Known limitations

- **No IDE-level hook enforcement.** Antigravity has no PreToolUse/Stop hook mechanism, so the context guard, spec gate, skill gate, and destructive-command blocking cannot intercept individual tool calls — the AGENTS.md core declares them as mandatory protocol rules. The **validation gate and attribution guard, however, are mechanically enforced at the git level** with `--git-hooks`: a commit cannot land with a broken typecheck/lint or with AI co-author attribution, unless they are skipped with `git commit --no-verify`, which the agent can run as well. Install with `--ci` and make the workflow's `policy` check required in branch protection: that check runs on the server, where no local flag reaches it.
- **No context-usage guard.** Context-reset discipline depends on the harness; the skills' handoff-block instructions still apply.
- **`engineering-learn` requires schema adaptation per run.** The skill now discovers logs from both harnesses — Claude Code (`~/.claude/projects/<slug>/*.jsonl`) and Antigravity (`~/.gemini/antigravity*/brain/<conversation-id>/.system_generated/logs/*.jsonl`, filtered by workspace path since brain dirs are per-conversation). Antigravity's JSONL entry schema differs from Claude Code's, so the skill instructs the agent to inspect one transcript and adapt the extraction keys before running Step 2; the correlation and write steps are schema-independent.
