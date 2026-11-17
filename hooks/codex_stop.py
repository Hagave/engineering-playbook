#!/usr/bin/env python3
"""
Codex Stop + SubagentStop hook: enforces the spec-confirmation and validation gates
deferred from codex_context_guard.py. That PreToolUse hook records
spec_pending / pending_validation in session state when a code write
happens; by Stop time any spec-confirmation text is flushed to the
transcript, so this is the reliable point to verify both — mirrors
hooks/context_guard.py's _check_spec_gate / _handle_stop design.

Check order (first block wins, matching context_guard.py's _handle_stop):
1. Suspension (playbook-off) — full bypass, clears both flags.
2. stop_hook_active — a continuation after a block: the gates that already
   blocked this turn are acknowledged (never loop); the others still run.
3. Spec gate — code was written without [SPEC CONFIRMED] in this task's text.
4. Validation gate — code was written and no validation command ran after.

Turns with no code write always pass, even with no marker anywhere in the
transcript and no validation command having run.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core

_HOME = os.path.expanduser('~')
_CODEX_STATE_DIR = os.path.join(_HOME, '.codex', 'hooks')


def _read_config() -> dict:
    cfg_path = os.path.join(_HOME, '.codex', 'config.toml')
    defaults = {'governance': True, 'spec_gate': True}
    if not os.path.exists(cfg_path):
        return defaults
    try:
        with open(cfg_path, encoding='utf-8') as f:
            text = f.read()
        m = re.search(r'\[playbook\](.*?)(?=\n\[|\Z)', text, re.DOTALL)
        block = m.group(1) if m else ''
        return {
            'governance': False if re.search(r'governance\s*=\s*false', block) else defaults['governance'],
            'spec_gate': False if re.search(r'spec_gate\s*=\s*false', block) else defaults['spec_gate'],
        }
    except Exception:
        return defaults


def _read_transcript_tail(transcript_path: str) -> list[str]:
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        with open(transcript_path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        return text.split('\n')[-200:]
    except Exception:
        return []


def _block(state_file: str, state: dict, flag: str, reason: str) -> None:
    """Block the turn for the gate that reads `flag`, recording it so the
    continuation Stop acknowledges this gate only."""
    state['stop_blocked'] = [*state.get('stop_blocked', []), flag]
    playbook_core.save_state(state_file, state)
    print(json.dumps({'decision': 'block', 'reason': reason}, separators=(',', ':')))
    sys.exit(0)


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}

    session_id = str(input_data.get('session_id') or '')
    # SubagentStop: the subagent's gates are its own — state keyed by agent_id,
    # and its spec token lives in agent_transcript_path (transcript_path is
    # the parent's rollout on this event).
    agent_id = str(input_data.get('agent_id') or '')
    state_file = playbook_core.state_path(_CODEX_STATE_DIR, session_id, agent_id=agent_id)
    transcript_path = str(input_data.get('agent_transcript_path' if agent_id else 'transcript_path') or '')
    tail = _read_transcript_tail(transcript_path)

    # /playbook-off suspends the entire hook — clear pending
    # state so a later re-enable does not replay a stale block.
    if playbook_core.is_suspended(tail):
        state = playbook_core.load_state(state_file)
        state['spec_pending'] = False
        state['pending_validation'] = False
        playbook_core.save_state(state_file, state)
        sys.exit(0)

    cfg = _read_config()
    if not cfg['governance']:
        sys.exit(0)

    state = playbook_core.load_state(state_file)
    # A gate blocks a turn at most once. On the continuation after a block
    # (stop_hook_active) the gates that already blocked are acknowledged and
    # the others still run: clearing every flag let a spec block swallow a
    # pending validation that never got its turn.
    if input_data.get('stop_hook_active') is True:
        for flag in state.get('stop_blocked', []):
            state[flag] = False
    else:
        state['stop_blocked'] = []

    if cfg['spec_gate'] and state.get('spec_pending'):
        if playbook_core.has_spec_token(tail):
            state['spec_pending'] = False
            playbook_core.save_state(state_file, state)
        else:
            _block(state_file, state, 'spec_pending',
                'Engineering playbook spec gate: source code was written this turn '
                f'without {playbook_core.SPEC_TOKEN} in your response text. Emit it '
                f'(for unambiguous single-line fixes: {playbook_core.SPEC_TOKEN} — '
                'unambiguous single-line fix) and state what you implemented and '
                'why, then re-end your turn.'
            )

    if state.get('pending_validation'):
        _block(state_file, state, 'pending_validation',
            'Engineering playbook validation gate: source files were written this '
            'session but no validation command (type check, lint, tests) ran '
            'afterwards. Run the relevant validation command before finishing, or '
            'report explicitly what is blocking it.'
        )

    playbook_core.save_state(state_file, state)
    sys.exit(0)


if __name__ == '__main__':
    main()
