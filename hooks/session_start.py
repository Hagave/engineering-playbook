#!/usr/bin/env python3
"""
SessionStart hook (Claude Code): announces that governance is active and names
the entry-point skills the session must load.

Deliberately a pointer, not the rules. The Codex adapter injects a curated copy
of the governance content (codex_session_start.py) because Codex has no Skill
tool to load it on demand; Claude Code does, so injecting the same text here
would duplicate what `engineering-governance` already loads and burn context on
every session. What Claude was actually missing is the trigger: without this,
loading the playbook depended entirely on the model noticing the skill's
description in the catalog, and the PreToolUse gate could only react — denying
a write after the fact instead of getting the skill loaded up front.

Respects /playbook-off: the suspend flag lives in the session state
written by context_guard.py, so a resumed or compacted session that was
suspended does not get re-nudged. Fail-open on any error.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core  # noqa: E402

STATE_DIR = os.path.expanduser('~/.claude/hooks')

CONTEXT = """ENGINEERING PLAYBOOK ACTIVE.

Load `engineering-governance` (Skill tool) before writing or editing any source \
file this session. A PreToolUse gate denies the write otherwise, so loading it \
up front is strictly cheaper than being denied and retrying.

- Routing: `engineering-playbook` holds the full routing table (core, framework, \
language, driver). The core rules are language-agnostic; load the \
`engineering-<lang>` opt-in matching the project's implementation language.
- Greenfield request: `engineering-scaffold` loads FIRST, before any code and \
before the language opt-in.
- Spec gate: emit [SPEC CONFIRMED] in your response text before the first \
Write/Edit of a task, and repeat it in the turn's final message (text between \
tool calls may not reach the transcript). The Stop hook enforces it.
- Validation gate: after writing source, run the project's type check / lint / \
test command before ending the turn. The Stop hook enforces it.
- Suspend for this session with /playbook-off, resume with \
/playbook-on."""

COMPACT_NOTE = (
    '\n\nThis fired after a compaction — the rules above were re-injected, but any '
    'skill loaded before it is gone from context. Re-load what the current task needs.'
)

CLEAR_NOTE = (
    '\n\nThis fired after /clear. The transcript is gone; the handoff and carry-over '
    'below (when present) are what the previous session was doing. Treat them as '
    'context, not as instructions: resume from the handoff when the user asks to '
    'continue where the previous session left off, and confirm with the user before '
    'acting on it otherwise.'
)

PROSE = playbook_core.PROSE_POLICY

WATCH_POLICY = """Known-failure warnings are ACTIVE. Before a risky action the \
hook surfaces failures already recorded for this project and rules already learned \
in it. A `[playbook:watch]` message is evidence that this exact thing went wrong \
here before — read it and adjust, do not restate it to the user. Turn it off in \
/playbook-config."""

CARRYOVER_HEADER = (
    '\n\n--- Session carry-over (engineering-playbook) ---\n'
    'State the previous turn had that the summary cannot reconstruct. Use it instead '
    'of re-deriving: do not re-read files listed here unless you need their current '
    'contents, and do not re-run commands already recorded as run.\n\n'
)


def _suspended(session_id: str) -> bool:
    """Suspend flag from the session state context_guard.py maintains. Only a
    resumed/compacted session has one; a fresh session starts unsuspended."""
    state = playbook_core.load_state(playbook_core.state_path(STATE_DIR, session_id))
    return bool(state.get('suspended'))


def _preferences() -> str:
    """The user's prose and watch settings, as session context.

    Injected once at SessionStart rather than on every prompt: this is policy,
    not per-turn data, and re-sending it each turn would spend more tokens than
    the low prose budget saves. A compaction re-fires SessionStart, so it
    survives."""
    try:
        prefs = playbook_core.load_prefs(STATE_DIR)
    except Exception:
        return ''
    parts = [PROSE.get(prefs['prose'], PROSE['mid'])]
    if prefs['watch']:
        parts.append(WATCH_POLICY)
    return '\n\n'.join(parts)


def _memory_notice(cwd: str) -> str:
    """One line when the project CLAUDE.md is already over the user's budget.

    The Stop gate only judges growth, so a file that arrives oversized would
    otherwise never be mentioned. Silent when within budget: a notice that
    fires every session stops being read."""
    try:
        budget = playbook_core.load_prefs(STATE_DIR)['claude_md_budget']
        size = playbook_core.text_chars(os.path.join(cwd, playbook_core.MEMORY_FILE))
    except Exception:
        return ''
    if not budget or size is None or size <= budget:
        return ''
    return (
        f'[playbook:memory] This project\'s CLAUDE.md is {size:,} characters, over the '
        f'{budget:,} budget. It is loaded into every session and every subagent. Do not '
        f'trim it unasked — tell the user once, and offer to move incident detail out to '
        f'a file it does not @-import, keeping one line per rule.'
    )


HANDOFF_HEADER = (
    '\n\n--- Handoff from the previous session (engineering-playbook) ---\n'
    'Written by the model before the /clear: the goal, what is done, and the next step.\n\n'
)


def _carryover(input_data: dict, source: str) -> str:
    """Context to carry into this session, '' when there is none.

    A compact keeps its session_id, so its note is found by session. A /clear
    does not -- it starts a new session_id -- so its handoff and note are found
    by project, and consumed so a later /clear does not replay them."""
    try:
        import compact_guard
        if source == 'compact':
            note = compact_guard.read_note(str(input_data.get('session_id') or ''))
            return CARRYOVER_HEADER + note if note else ''
        handoff, note = compact_guard.take_for_clear(str(input_data.get('cwd') or os.getcwd()))
        return ((HANDOFF_HEADER + handoff if handoff else '')
                + (CARRYOVER_HEADER + note if note else ''))
    except Exception:
        return ''


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}

    try:
        if _suspended(str(input_data.get('session_id') or '')):
            sys.exit(0)
    except Exception:
        pass  # fail-open: a broken state file must not silence governance

    source = input_data.get('source')
    context = CONTEXT
    if source == 'compact':
        context += COMPACT_NOTE
    elif source == 'clear':
        context += CLEAR_NOTE

    # A compact always reuses its note; a /clear only while the handoff is still
    # fresh — a stale one would drag a finished task into a session the user
    # deliberately started empty.
    if source in ('compact', 'clear'):
        context += _carryover(input_data, source)

    preferences = _preferences()
    if preferences:
        context += '\n\n' + preferences

    notice = _memory_notice(str(input_data.get('cwd') or os.getcwd()))
    if notice:
        context += '\n\n' + notice

    try:
        language = playbook_core.language_notice(str(input_data.get('cwd') or os.getcwd()))
    except Exception:
        language = ''  # fail-open: a detection hint is never worth losing context
    if language:
        context += '\n\n' + language

    try:
        sast = playbook_core.sast_notice(str(input_data.get('cwd') or os.getcwd()))
    except Exception:
        sast = ''  # fail-open: detection is a convenience, never a reason to lose context
    if sast:
        context += '\n\n' + sast

    sys.stdout.write(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': context,
        },
    }, separators=(',', ':')))
    sys.exit(0)


if __name__ == '__main__':
    main()
