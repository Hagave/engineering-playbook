#!/usr/bin/env python3
"""
Engineering Playbook — Learn Guard
PostToolUse + UserPromptSubmit hook: captures what went wrong while it is still
cheap to capture, and arms the turn-end learn gate when the failure is the kind
that must produce a durable rule.

Two tiers, because they have very different costs:

  1. Silent capture (PostToolUse). Every failed tool call is appended to a
     session-scoped JSONL journal. Costs zero context — nothing is injected into
     the conversation — and gives Learn (/playbook-config) a pre-extracted corpus
     instead of making it re-mine raw transcripts.

  2. Armed gate (UserPromptSubmit, and repeated identical failures). A user
     message that rejects the previous turn is the single highest-signal
     evidence that the model acted on a wrong premise. That arms
     state['learn_pending'], which context_guard's Stop handler blocks on until
     the model emits LEARNED_TOKEN. Repeated identical failures arm it too: the
     same call failing three times is a wrong premise the model has not noticed.

Deliberately NOT armed by a single failed tool call. A failed grep, a missing
path, a first-try compile error are normal exploration — blocking a turn on each
one would make the gate noise and train the model to emit the token reflexively.

Blocking contract: neither event ever blocks. UserPromptSubmit emits
additionalContext; PostToolUse stays silent. The only blocking point is Stop,
owned by context_guard.py.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core  # noqa: E402

STATE_DIR = os.path.expanduser('~/.claude/hooks')

# Identical failure repeated this many times = a premise the model is not
# revising on its own. Two is still plausible as a retry with a real fix
# attempt in between; three is a loop.
REPEAT_FAILURE_THRESHOLD = 3

# Hard cap on turn-end blocks per session. The gate exists to make the model
# learn, not to trap a session whose user keeps rephrasing.
MAX_LEARN_BLOCKS = 3

MAX_FIELD = 300

LEARN_PROTOCOL = """[playbook:learn] Correction signal detected in the user's message \
— the previous turn was wrong or built on a wrong premise.

Before ending this turn:
1. Name the premise in one line: what you assumed, what is actually true, and \
the evidence that settles it (file, command output, doc).
2. Persist it. If the project has a CLAUDE.md, append the rule inside the \
`<!-- engineering-learn:live -->` block (create the block if absent). Do NOT \
create CLAUDE.md itself without asking. One imperative line, specific to this \
project — a rule that would have prevented this exact failure.
3. Emit {token} in your response text.

The Stop gate blocks this turn until {token} appears. If this was a false \
positive — the user was not correcting you — emit `{token} none — <reason>` and \
move on. Do not restate the correction back to the user; fix the work."""

REPEAT_PROTOCOL = """[playbook:learn] The same call failed {count} times: {tool} \
— {error}

Stop retrying. The premise behind this call is wrong, not the syntax. Before \
ending this turn, state what you assumed, verify it against the repo or the \
tool's own help output, and emit {token} with the corrected rule. \
Escape hatch if the failure is environmental and not a premise: \
`{token} none — <reason>`."""


def _state_file(session_id: str, agent_id: str) -> str:
    return playbook_core.state_path(STATE_DIR, session_id, agent_id=agent_id)


def _journal_file(session_id: str, agent_id: str) -> str:
    return playbook_core.journal_path(STATE_DIR, session_id, agent_id=agent_id)


def _truncate(value: object) -> str:
    return playbook_core.scrub_secrets(str(value))[:MAX_FIELD]


def _error_text(tool_response: object) -> str:
    """The failure message for a tool result, or '' when it succeeded.

    Shapes differ per tool: structured tools carry is_error/error, Bash carries
    interrupted plus a shell-level status, and some tools return a bare string
    starting with "Error:". Unknown shapes read as success — over-reporting
    would fill the journal with noise that dilutes the real failures."""
    if isinstance(tool_response, str):
        return tool_response[:MAX_FIELD] if tool_response.startswith('Error') else ''
    if not isinstance(tool_response, dict):
        return ''
    if tool_response.get('is_error') or tool_response.get('isError'):
        return _truncate(tool_response.get('content') or tool_response.get('error') or 'error')
    if tool_response.get('error'):
        return _truncate(tool_response['error'])
    if tool_response.get('success') is False:
        return _truncate(tool_response.get('message') or 'failed')
    if tool_response.get('interrupted'):
        return 'interrupted'
    # Shell results report status as exitCode, code or returnCode depending on
    # which path produced them. `code` is only a status when the response is
    # shell-shaped — elsewhere it is just as likely to be an error code field.
    is_shell = 'stdout' in tool_response or 'stderr' in tool_response
    for key in ('exit_code', 'exitCode', 'returnCode') + (('code',) if is_shell else ()):
        code = tool_response.get(key)
        if isinstance(code, int) and code != 0:
            return _truncate(f'exit {code}: {tool_response.get("stderr", "")}')
    return ''


def _signature(tool_name: str, tool_input: object) -> str:
    """Identity of a call for repeat detection: the tool plus the argument that
    decides what it acts on. Keyed on the argument rather than the whole input
    so a retried command with an added flag still counts as the same attempt."""
    key = ''
    if isinstance(tool_input, dict):
        for field in ('command', 'file_path', 'path', 'pattern', 'url'):
            if tool_input.get(field):
                key = str(tool_input[field])
                break
    return f'{tool_name}:{key[:120]}'


def _load(session_id: str, agent_id: str, transcript: str | None) -> tuple[str, dict]:
    path = _state_file(session_id, agent_id)
    state = playbook_core.load_state(path)
    # context_guard resets state whenever session_file does not match the live
    # transcript. Claiming it here keeps a learn flag written before the first
    # tool call from being wiped by that reset.
    if transcript and not state.get('session_file'):
        state['session_file'] = transcript
    return path, state


def _emit_context(text: str, event: str) -> None:
    sys.stdout.write(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': event,
            'additionalContext': text,
        },
    }, separators=(',', ':')))


def _handle_post_tool_use(data: dict, path: str, state: dict, journal: str) -> None:
    cwd = str(data.get('cwd') or '')
    tool_name = str(data.get('tool_name') or '')
    error = _error_text(data.get('tool_response'))
    if not error:
        return

    tool_input = data.get('tool_input')
    playbook_core.append_incident(journal, {
        'ts': time.time(),
        'kind': 'tool_error',
        'cwd': cwd,
        'tool': tool_name,
        'input': _truncate(tool_input),
        'error': error,
    })

    signature = _signature(tool_name, tool_input)
    counts = state.get('fail_counts')
    if not isinstance(counts, dict):
        counts = {}
    counts[signature] = int(counts.get(signature, 0)) + 1
    # Bounded: a long session must not grow an unbounded map in the state file.
    if len(counts) > 50:
        counts = dict(list(counts.items())[-50:])
    state['fail_counts'] = counts

    if counts[signature] >= REPEAT_FAILURE_THRESHOLD and not state.get('learn_pending'):
        state['learn_pending'] = True
        state['learn_reason'] = REPEAT_PROTOCOL.format(
            count=counts[signature],
            tool=tool_name,
            error=error[:200],
            token=playbook_core.LEARNED_TOKEN,
        )
        playbook_core.append_incident(journal, {
            'ts': time.time(),
            'kind': 'repeat_failure',
            'cwd': cwd,
            'tool': tool_name,
            'input': _truncate(tool_input),
            'error': error,
            'count': counts[signature],
        })
    playbook_core.save_state(path, state)


def _handle_user_prompt(data: dict, path: str, state: dict, journal: str) -> None:
    prompt = playbook_core.typed_words(str(data.get('prompt') or ''))
    if not playbook_core.CORRECTION_RE.search(prompt):
        return

    playbook_core.append_incident(journal, {
        'ts': time.time(),
        'kind': 'correction',
        'cwd': str(data.get('cwd') or ''),
        'prompt': _truncate(prompt),
    })
    state['learn_pending'] = True
    state['learn_reason'] = LEARN_PROTOCOL.format(token=playbook_core.LEARNED_TOKEN)
    playbook_core.save_state(path, state)
    _emit_context(state['learn_reason'], 'UserPromptSubmit')


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)

    session_id = str(data.get('session_id') or '')
    agent_id = str(data.get('agent_id') or '')
    transcript = data.get('transcript_path')
    event = data.get('hook_event_name') or ''

    try:
        path, state = _load(session_id, agent_id, transcript)
        # /playbook-off suspends the learn loop with everything else.
        if state.get('suspended'):
            sys.exit(0)
        journal = _journal_file(session_id, agent_id)
        capped = state.get('learn_blocks', 0) >= MAX_LEARN_BLOCKS

        if event == 'PostToolUse' and not capped:
            _handle_post_tool_use(data, path, state, journal)
        elif event == 'UserPromptSubmit' and not capped:
            _handle_user_prompt(data, path, state, journal)
    except Exception:
        pass  # fail-open: capture must never break a tool call or a prompt
    sys.exit(0)


if __name__ == '__main__':
    main()
