#!/usr/bin/env python3
"""
SessionStart hook: injects a curated subset of the engineering-playbook
governance content (non-negotiable rules + skill routing table + spec-gate
mechanics) into the Codex system prompt, instead of a paraphrase. Codex has
no Skill-tool equivalent that loads these files on demand the way Claude
Code does, so this hook is the only guaranteed delivery path for the rules.

Curated, not verbatim: injecting both SKILL.md files in full (~18.5k chars)
was verified against a real Codex 0.147.0 session to get silently
middle-truncated by Codex's default ~2500-token additionalContextLimit
(confirmed via developers.openai.com/codex/hooks) — the dropped middle was
exactly the skill routing table and the spec-gate section, i.e. the two
things this hook exists to deliver. .codex-plugin/hooks.json now sets
additionalContextLimit explicitly on this hook (6000 tokens, well above
what is injected below) so a future addition here does not silently regress
into truncation again. Still curated rather than verbatim on top of that:
this hook extracts only the sections that matter for a Codex session (by ##
header, so it tracks the source files instead of duplicating prose) and
drops sections that are Claude Code-specific and do not apply here anyway
(Context management describes Claude's own /compact token accounting, which
Codex's hook contract has no data to reimplement — see codex_compact.py;
Hook-enforced gates describes a Skill-tool-gated "skill gate" mechanic Codex
has no Skill tool to enforce; Model routing delegates to a Haiku subagent
alias that is a Claude Code-specific mechanism).

Reads ~/.codex/config.toml for [playbook] governance = false to disable.
Fail-open.
"""

import json
import os
import re
import sys

_HOME = os.path.expanduser('~')
_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS_ROOT = os.path.join(_HERE, '..', 'skills')

sys.path.insert(0, _HERE)
import playbook_core  # noqa: E402

# The user's preferences, shared with the Claude Code plugin (one file for
# every harness, written by scripts/playbook_config.py).
_PREFS_DIR = os.path.join(_HOME, '.claude', 'hooks')

_FRONTMATTER_RE = re.compile(r'\A---\n.*?\n---\n', re.DOTALL)
_SECTION_RE = re.compile(r'(?m)^## .+')

# Sections kept per skill, by header text (must match "## <header>" in the
# source file exactly). Order here is the order they are emitted in.
# Everything not listed is dropped — see module docstring for why.
_KEEP_SECTIONS = {
    'engineering-playbook': ('Non-negotiable rules', 'Skill routing'),
    'engineering-governance': ('Mandatory companion skills', 'Spec gate', 'Decision ownership',
                               'Validation checklist'),
}


def _governance_enabled() -> bool:
    cfg_path = os.path.join(_HOME, '.codex', 'config.toml')
    if not os.path.exists(cfg_path):
        return True
    try:
        with open(cfg_path, encoding='utf-8') as f:
            text = f.read()
        m = re.search(r'\[playbook\][^[]*?governance\s*=\s*(false|true)', text)
        return m.group(1) == 'true' if m else True
    except Exception:
        return True


def _sections(body: str) -> dict[str, str]:
    """Split a skill body into {header_text: section_text_including_header}.
    Content before the first "## " header is keyed under '' (the preamble:
    the RULE-ANCHOR block and the skill's opening paragraph)."""
    matches = list(_SECTION_RE.finditer(body))
    result = {'': body[:matches[0].start()] if matches else body}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        result[m.group().removeprefix('## ').strip()] = body[m.start():end].rstrip()
    return result


def _curated_skill(name: str) -> str:
    path = os.path.join(_SKILLS_ROOT, name, 'SKILL.md')
    with open(path, encoding='utf-8') as f:
        text = f.read()
    body = _FRONTMATTER_RE.sub('', text, count=1).strip()
    sections = _sections(body)
    parts = [sections['']] + [sections[h] for h in _KEEP_SECTIONS[name] if h in sections]
    # Strip trailing "---" horizontal-rule separators from each section —
    # kept sections are not necessarily adjacent in the source, so a
    # separator meant to lead into a dropped section would otherwise be
    # stranded here.
    cleaned = (re.sub(r'\n*---\s*\Z', '', p.strip()) for p in parts)
    return '\n\n---\n\n'.join(cleaned)


def _prose_policy() -> str:
    """The user's prose level, as Claude Code's SessionStart injects it.
    Fail-open to the default level: a broken preference file must not take
    the governance preamble down with it."""
    try:
        level = playbook_core.load_prefs(_PREFS_DIR)['prose']
    except Exception:
        level = playbook_core.PREFS_DEFAULTS['prose']
    return playbook_core.PROSE_POLICY.get(level, playbook_core.PROSE_POLICY['mid'])


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}

    if not _governance_enabled():
        sys.exit(0)

    source = input_data.get('source') or ''

    codex_notes = '\n'.join([
        '--- Codex adapter notes ---',
        'This session runs the engineering-playbook Codex adapter. The rules above are a curated subset of the same governance content the Claude Code plugin loads via the Skill tool for every implementation task — curated to fit within Codex\'s hook context budget (see module docstring for what was dropped and why).',
        'The remaining engineering-* skills, and the sections trimmed from the two above, are plain files under skills/<name>/SKILL.md — Codex has no slash commands or on-demand Skill tool for this catalog. Read the relevant file directly before applying a skill\'s rules; do not act on what a skill "probably says".',
        "Spec gate: emit the literal token [SPEC CONFIRMED] in your response text before the first Write/Edit/MultiEdit/ApplyPatch of a task, and repeat it in the turn's final message (see the Spec gate section above). The Stop hook enforces this at end of turn.",
        'Validation gate: after writing source code, run the relevant type check / lint / test command before ending the turn (see the Validation checklist section above) — the Stop hook blocks otherwise.',
        'To suspend governance for this session: state that you are invoking playbook-off (mirrors /playbook-off in Claude Code). Resume with playbook-on or engineering-playbook. This is detected by scanning the transcript, so state it explicitly rather than just changing behavior silently.',
        'To disable governance persistently: add [playbook]\\ngovernance = false\\n to ~/.codex/config.toml. To re-enable: remove that block or set governance = true.',
        'Settings: the user changes them by asking, or with the playbook-config skill (skills/playbook-config/SKILL.md, `--harness codex`). Only the prose level acts in Codex; it applies from the next session.',
    ])
    if source == 'compact':
        codex_notes += (
            '\nThis SessionStart fired after a compaction (source: compact) — the governance rules above were just re-injected. '
            'Anything not captured in your own response text before compaction may be lost; re-derive it from the repo if needed.'
        )

    try:
        body = '\n\n---\n\n'.join(_curated_skill(name) for name in _KEEP_SECTIONS)
        preamble = (f'{body}\n\n{codex_notes}\n\n'
                    f'{playbook_core.USER_LANGUAGE_POLICY}\n\n{_prose_policy()}')
    except Exception:
        preamble = '\n'.join([
            'ENGINEERING PLAYBOOK ACTIVE (Codex adapter).',
            'engineering-playbook or engineering-governance SKILL.md could not be read — governance rules are unavailable this session. Read skills/engineering-playbook/SKILL.md and skills/engineering-governance/SKILL.md directly before implementing.',
            playbook_core.USER_LANGUAGE_POLICY,
        ])

    sys.stdout.write(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': preamble,
        },
    }, separators=(',', ':')))
    sys.exit(0)


if __name__ == '__main__':
    main()
