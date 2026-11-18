#!/usr/bin/env python3
"""
Engineering Playbook — Compact Guard
PreCompact hook: writes the session's carry-over note before the transcript is
summarized away.

Why this exists: compaction is lossy by construction, and the parts it loses
first are the operational ones — which files this task already touched, which
validation command this project actually uses, which gates are still armed,
which skills were loaded. The summary keeps the narrative and drops the state,
so the turn after a compact re-derives all of it, burning the context the
compact just freed.

The note is written to disk, not to the summarizer. session_start.py re-injects
it on the SessionStart that follows a compact, so the carry-over survives
regardless of what the summary kept.

/clear is the escape the context guard actually recommends — a compact does not
reliably get the session back under the threshold, dropping the transcript does.
A /clear starts a NEW session_id (measured: every `SessionStart:clear` transcript
carries an id of its own), so a note keyed by session is unreachable from it.
Every note is therefore also written under a project key, next to the handoff the
model writes itself, and a /clear consumes both exactly once.

Blocking contract: never blocks. A PreCompact hook that fails or stalls
interrupts the compaction it was supposed to protect.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core  # noqa: E402
import context_guard  # noqa: E402  (transcript parsers; see module docstring)

STATE_DIR = os.path.expanduser('~/.claude/hooks')

MAX_NOTE_CHARS = 6_000
MAX_PROMPTS = 3
MAX_FILES = 20
MAX_COMMANDS = 10
# A /clear picks up the project handoff only while it is this recent. Generous on
# purpose: the handoff is consumed on first read, so the window only has to cover
# a user who steps away between the warning and the /clear -- not guard against
# the same note being replayed into every later session.
FRESH_SECONDS = 86_400
PROMPT_SCAN_MAX_BYTES = 32 * 1024 * 1024  # above this, prompts fall back to the tail scan


def carryover_path(session_id: str, agent_id: str = '') -> str:
    path = playbook_core.state_path(STATE_DIR, session_id, agent_id=agent_id)
    return path.replace('.state-', '.carryover-').replace('.json', '.md')


def project_carryover_path(cwd: str) -> str:
    return os.path.join(STATE_DIR, f'.carryover-project-{playbook_core.project_key(cwd)}.md')


def handoff_path(cwd: str) -> str:
    return os.path.join(STATE_DIR, f'.handoff-{playbook_core.project_key(cwd)}.md')


def _loads(line: str) -> dict:
    try:
        obj = json.loads(line)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ('git', '-C', cwd) + args,
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else ''
    except Exception:
        return ''


def _prompt_text(obj: dict) -> str:
    """Text of a user message, with credential-shaped fragments redacted.

    Redacted here rather than at the call sites so every path into the note is
    covered: the note is written to disk and re-injected into a later session,
    so a token pasted into a prompt would outlive the session that leaked it."""
    content = obj.get('message', {}).get('content')
    if isinstance(content, str):
        return playbook_core.scrub_secrets(content).strip()
    if isinstance(content, list):
        return playbook_core.scrub_secrets(' '.join(
            b.get('text', '') for b in content
            if isinstance(b, dict) and b.get('type') == 'text'
        )).strip()
    return ''


def _harvest_prompts(transcript: str) -> list[str]:
    """The task statement plus the most recent instructions, from a full-file
    scan rather than the tail.

    Deliberately not tail-scoped, unlike everything else here: in a long session
    the original request — the one thing a summary must not lose — sits far
    behind the tail window, while the tail holds only tool traffic. Streamed
    line by line and capped by file size so a very large transcript degrades to
    the tail scan instead of stalling the compaction."""
    first: list[str] = []
    recent: list[str] = []
    try:
        if os.path.getsize(transcript) > PROMPT_SCAN_MAX_BYTES:
            return []
        with open(transcript, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"user"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not context_guard._is_user_prompt(obj):
                    continue
                text = _prompt_text(obj)
                if not text:
                    continue
                if not first:
                    first.append(text[:400])
                    continue
                recent.append(text[:400])
                if len(recent) > MAX_PROMPTS:
                    recent.pop(0)
    except Exception:
        return first + recent
    return first + [p for p in recent if p not in first]


def _harvest(transcript: str | None) -> tuple[list[str], list[str], list[str]]:
    """Prompts (full scan), plus written files and shell commands from the
    transcript tail.

    Files and commands are tail-scoped on purpose: the carry-over describes what
    the session is doing now, not everything it ever did. An exhaustive history
    would be both larger than the summary it supplements and less useful."""
    files: list[str] = []
    commands: list[str] = []
    if not transcript or not os.path.exists(transcript):
        return [], files, commands
    prompts = _harvest_prompts(transcript)
    if not prompts:
        prompts = [
            _prompt_text(obj) for obj in (
                _loads(line) for line in context_guard._read_tail_lines(transcript)
            ) if context_guard._is_user_prompt(obj) and _prompt_text(obj)
        ][-MAX_PROMPTS:]

    for line in context_guard._read_tail_lines(transcript):
        obj = _loads(line)
        if obj.get('type') != 'assistant':
            continue
        content = obj.get('message', {}).get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'tool_use':
                continue
            name = block.get('name')
            inp = block.get('input') or {}
            if not isinstance(inp, dict):
                continue
            if name in context_guard.IMPL_TOOLS and inp.get('file_path'):
                files.append(str(inp['file_path']))
            elif name == 'Bash' and inp.get('command'):
                commands.append(playbook_core.scrub_secrets(str(inp['command']))[:120])

    def _tail_unique(values: list[str], limit: int) -> list[str]:
        seen: dict[str, None] = {}
        for value in reversed(values):
            seen.setdefault(value, None)
            if len(seen) >= limit:
                break
        return list(reversed(list(seen)))

    return prompts, _tail_unique(files, MAX_FILES), _tail_unique(commands, MAX_COMMANDS)


def build_note(transcript: str | None, state: dict, cwd: str, trigger: str = '') -> str:
    prompts, files, commands = _harvest(transcript)
    branch = _git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD')
    head = _git(cwd, 'rev-parse', '--short', 'HEAD')
    dirty = [l for l in _git(cwd, 'status', '--short').splitlines() if l.strip()]

    lines = [
        '# Carry-over (engineering-playbook)',
        f'Captured {time.strftime("%Y-%m-%d %H:%M")}'
        + (f' before {trigger} compaction' if trigger else '')
        + f' — cwd `{cwd}`.',
        '',
    ]
    if prompts:
        lines += ['## What the user asked (original request first)']
        lines += [f'{i}. {p}' for i, p in enumerate(prompts, 1)] + ['']
    if files:
        lines += ['## Files written this session'] + [f'- `{f}`' for f in files] + ['']
    if commands:
        lines += ['## Commands already run'] + [f'- `{c}`' for c in commands] + ['']

    skills = state.get('skills')
    if isinstance(skills, list) and skills:
        lines += ['## Skills loaded before the compact',
                  '- ' + ', '.join(f'`{s}`' for s in skills[-12:]),
                  'Re-load whichever the remaining work needs — a compact drops them from context.',
                  '']

    gates = []
    if state.get('spec_pending'):
        gates.append(f'`{playbook_core.SPEC_TOKEN}` still owed for code written this turn')
    if state.get('pending_validation'):
        gates.append('validation command still owed for source written this session')
    if state.get('learn_pending'):
        gates.append(f'`{playbook_core.LEARNED_TOKEN}` still owed — a correction was not distilled')
    reviewed = state.get('reviewed_head')
    if reviewed and head and not reviewed.startswith(head):
        gates.append('pre-push review is stale — HEAD moved since the last review')
    if gates:
        lines += ['## Gates still armed'] + [f'- {g}' for g in gates] + ['']

    lines += ['## Repo state',
              f'- branch `{branch or "?"}` at `{head or "?"}`',
              f'- {len(dirty)} uncommitted path(s)' if dirty else '- working tree clean',
              '']
    return '\n'.join(lines)[:MAX_NOTE_CHARS]


def write_note(session_id: str, agent_id: str, transcript: str | None,
               state: dict, cwd: str, trigger: str = '') -> str:
    note = build_note(transcript, state, cwd, trigger)
    path = carryover_path(session_id, agent_id)
    with open(path, 'w') as f:
        f.write(note)
    if not agent_id:
        # A subagent's context is discarded at its stop; only the main session
        # has a transcript a /clear can drop.
        with open(project_carryover_path(cwd), 'w') as f:
            f.write(note)
    return path


def write_handoff(cwd: str, text: str) -> str:
    """Persist the model's own handoff for the /clear that follows. Scrubbed
    for the same reason prompts are: it is re-injected into a later session."""
    path = handoff_path(cwd)
    with open(path, 'w') as f:
        f.write(playbook_core.scrub_secrets(text).strip()[:MAX_NOTE_CHARS])
    return path


def _take(path: str, max_age: float) -> str:
    """Read and delete: the note is for the ONE session that resumes the work.
    Left in place, it would be re-injected into every later /clear of the project."""
    try:
        fresh = (time.time() - os.path.getmtime(path)) <= max_age
        with open(path) as f:
            text = f.read()[:MAX_NOTE_CHARS] if fresh else ''
        os.remove(path)
        return text
    except Exception:
        return ''


def take_for_clear(cwd: str, max_age: float = FRESH_SECONDS) -> tuple[str, str]:
    """(handoff, carry-over) for the project, each '' when absent or stale.
    Both are consumed — a stale one is deleted too, it will never be fresh again."""
    return _take(handoff_path(cwd), max_age), _take(project_carryover_path(cwd), max_age)


def read_note(session_id: str, agent_id: str = '', max_age: float | None = None) -> str:
    """The stored note, or '' when absent or older than max_age seconds."""
    path = carryover_path(session_id, agent_id)
    try:
        if max_age is not None and (time.time() - os.path.getmtime(path)) > max_age:
            return ''
        with open(path) as f:
            return f.read()[:MAX_NOTE_CHARS]
    except Exception:
        return ''


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)

    session_id = str(data.get('session_id') or '')
    agent_id = str(data.get('agent_id') or '')
    cwd = str(data.get('cwd') or os.getcwd())
    transcript = data.get('transcript_path')
    trigger = str(data.get('trigger') or '')

    try:
        state_file = playbook_core.state_path(STATE_DIR, session_id, agent_id=agent_id)
        state = playbook_core.load_state(state_file)
        if state.get('suspended'):
            sys.exit(0)
        path = write_note(session_id, agent_id, transcript, state, cwd, trigger)
        sys.stdout.write(f'[playbook:carryover] saved → {path}')
    except Exception:
        pass  # fail-open: never interrupt a compaction
    sys.exit(0)


if __name__ == '__main__':
    main()
