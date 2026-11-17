#!/usr/bin/env python3
"""
Codex PreToolUse hook — unified guard:
- Write/Edit/MultiEdit/ApplyPatch/Bash writing a source file: records
  spec-pending and pending-validation state for codex_stop.py to verify
  (never denies here for spec/validation reasons — see below).
- Bash: destructive-command block and commit-attribution block, using the
  same checks hooks/context_guard.py runs for Claude Code (playbook_core).
- Suspension: if the transcript shows playbook-off was the most
  recent of the suspend/resume skill names, every check below is bypassed —
  mirrors context_guard.py's "off means off" full pass-through.

Reads ~/.codex/config.toml for [playbook] governance / spec_gate /
destructive_block toggles. Fail-open on errors.

The spec gate itself is NOT enforced here. hooks/context_guard.py documents
why: [SPEC CONFIRMED] is assistant TEXT emitted in the same turn as the
guarded write, and that turn's message is not flushed to the transcript
until its tool calls resolve — a PreToolUse read cannot see a token the
model just emitted, so checking here produces a false-positive deny on the
very write that just confirmed the spec. This hook only records that a code
write happened without a marker visible yet; codex_stop.py re-checks after
the text is flushed and blocks only if it is still missing. The validation
gate (pending_validation) follows the same defer-to-Stop shape for
consistency, though it has no flush-lag reason to — a validation command is
a separate tool call, always visible by the time it matters.

Codex hook I/O contract (codex-rs hooks/src/engine/output_parser.rs,
parse_pre_tool_use):

  Allow is `{"continue": true}`: no decision field at all is the implicit
  allow; `permissionDecision: allow` and `decision: approve` are rejected
  as unsupported.

  Deny is `hookSpecificOutput.permissionDecision: "deny"` with a non-empty
  `permissionDecisionReason` (the legacy form is `decision: "block"` with a
  `reason`). A top-level `reason` without a decision is "reason without
  decision": the output is marked invalid and the tool call RUNS. Every
  deny this hook emitted had that shape until the 4.0.0 release review, so
  every gate was advisory on Codex. tests/structural/test_hook_contract.py
  pins both shapes. A PreToolUse hook always exits 0; the old "exit 2 with
  the reason on stderr" contract was removed in Codex 0.147.0.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core


def _emit(permission_decision: str, reason: str | None = None) -> None:
    """Write the PreToolUse verdict in the only shapes Codex acts on (see the
    module docstring): a deny with its reason, or the bare allow."""
    payload: dict = {'continue': True}
    if permission_decision == 'deny':
        payload = {'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason or 'Denied by the engineering playbook.',
        }}
    sys.stdout.write(json.dumps(payload, separators=(',', ':')))


# Codex has no consent read: its rollout's user-message shape is unverified, and
# a misread would let the model's own text lift the block. The deny says so
# instead of promising the lift the Claude Code hook gives.
_CODEX_NO_LIFT = (
    'On Codex the block does not lift in the session: ask the user to run the '
    'command themselves.'
)

_HOME = os.path.expanduser('~')
_CODEX_STATE_DIR = os.path.join(_HOME, '.codex', 'hooks')


def _read_config() -> dict:
    cfg_path = os.path.join(_HOME, '.codex', 'config.toml')
    defaults = {'governance': True, 'spec_gate': True, 'destructive_block': True}
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
            'destructive_block': False if re.search(r'destructive_block\s*=\s*false', block) else defaults['destructive_block'],
        }
    except Exception:
        return defaults


def _find_latest_transcript(transcript_path: str) -> str | None:
    # No fallback. Real Codex PreToolUse payloads always carry transcript_path
    # (confirmed via a live capture), and the previous fallback — newest
    # *.jsonl under ~/.codex/sessions — resolved to an unrelated earlier
    # session whenever it did fire. is_suspended() then read a stale
    # playbook-off out of that foreign transcript and silently
    # disabled every gate. No transcript means no evidence: governance stays
    # on. codex_stop.py already behaves this way.
    return transcript_path if transcript_path and os.path.exists(transcript_path) else None


def _read_transcript_tail(transcript_path: str | None) -> list[str]:
    """Returns the last 200 raw lines. Schema-agnostic beyond the role check
    in playbook_core._drop_developer_lines: Codex's full transcript entry
    shape has not been verified against Claude Code's, so this does not
    reuse context_guard.py's structured per-entry parsing. Known
    limitation — see docs/adapters/codex.md."""
    if not transcript_path:
        return []
    try:
        with open(transcript_path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        return text.split('\n')[-200:]
    except Exception:
        return []


def _is_code_write(tool_name: str, tool_input: dict) -> bool:
    # Extension AND destination (playbook_core.is_code_write): a write into a
    # git-ignored path or the session scratchpad is not project source, and used to
    # arm the gates anyway. Fails closed on an unresolvable path.
    command = str(tool_input.get('command') or '') if tool_name == 'Bash' else ''
    return playbook_core.is_code_write(tool_name, tool_input, command, os.getcwd())


def main() -> None:
    playbook_core.start_hook_clock()
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}

    cfg = _read_config()
    if not cfg['governance']:
        _emit('allow')
        sys.exit(0)

    tool_name = input_data.get('tool_name') or input_data.get('tool') or input_data.get('toolName') or ''
    tool_input = input_data.get('tool_input') or input_data.get('args') or input_data.get('arguments') or input_data.get('input') or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = str(input_data.get('session_id') or '')
    # A subagent's calls carry its parent's session_id (captured, Codex 0.153.0);
    # agent_id keeps its gate state apart. Its transcript_path is already its
    # own rollout, so the spec pre-check below reads the right transcript.
    agent_id = str(input_data.get('agent_id') or '')
    transcript_path = _find_latest_transcript(input_data.get('transcript_path') or '')
    tail = _read_transcript_tail(transcript_path)

    # /playbook-off suspends the entire hook — full pass-through,
    # same as context_guard.py's "off means off".
    if playbook_core.is_suspended(tail):
        _emit('allow')
        sys.exit(0)

    # --- Spec gate + validation gate: record pending state, never deny here (see module docstring) ---
    if cfg['spec_gate'] and _is_code_write(tool_name, tool_input):
        state_file = playbook_core.state_path(_CODEX_STATE_DIR, session_id, agent_id=agent_id)
        state = playbook_core.load_state(state_file)
        state['spec_pending'] = not playbook_core.has_spec_token(tail)
        state['pending_validation'] = True
        playbook_core.save_state(state_file, state)
    elif tool_name == 'Bash' and playbook_core.VALIDATION_RE.search(
            playbook_core.strip_inline_payloads(
                playbook_core.strip_heredoc_bodies(str(tool_input.get('command') or '')))):
        state_file = playbook_core.state_path(_CODEX_STATE_DIR, session_id, agent_id=agent_id)
        state = playbook_core.load_state(state_file)
        state['pending_validation'] = False
        playbook_core.save_state(state_file, state)

    # --- Destructive / commit-attribution block for Bash ---
    if cfg['destructive_block'] and tool_name == 'Bash':
        command = str(tool_input.get('command') or '')
        # Heredoc bodies and inline interpreter payloads are data or foreign
        # source, not shell about to run. Attribution reads the raw command,
        # because a script in that same command is often what writes the
        # message; the push check reads the history the push would publish.
        # Codex has no PostToolUse here, so the push is the only history check.
        runnable = playbook_core.strip_inline_payloads(
            playbook_core.strip_heredoc_bodies(command))
        cwd = str(input_data.get('cwd') or os.getcwd())
        result = (playbook_core.check_destructive(runnable, command, cwd)
                  or playbook_core.check_lab_scanner(runnable, command, cwd)
                  or playbook_core.check_ai_attribution(command, cwd)
                  or playbook_core.check_secret_bash(command, cwd)
                  or playbook_core.check_commit_secrets(runnable, cwd)
                  or playbook_core.check_push_attribution(runnable, cwd)
                  or playbook_core.check_push_secrets(runnable, cwd))
        if result:
            _emit('deny', result['msg'].replace(playbook_core.AUTHORIZATION_HINT, _CODEX_NO_LIFT))
            sys.exit(0)

    # --- Secrets added by an editing tool (only a deny counts here) ---
    if tool_name in playbook_core.SECRET_WRITE_TOOLS:
        cwd = str(input_data.get('cwd') or os.getcwd())
        result = playbook_core.check_secret_write(tool_name, tool_input, cwd)
        if result and result.get('block'):
            _emit('deny', result['msg'])
            sys.exit(0)

    _emit('allow')
    sys.exit(0)


if __name__ == '__main__':
    main()
