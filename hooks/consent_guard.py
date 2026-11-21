#!/usr/bin/env python3
"""UserPromptSubmit: the user's answer to the destructive commands queued in auto mode.

In auto mode the destructive-command gate denies without a dialog and queues
the command (playbook_core.consent_request); the model asks the user in its
message. This hook reads the user's own words from the payload's `prompt`
field, never from the transcript, and an explicit consent ("pode apagar",
"yes, delete it") moves the queued commands to approved. The model is told
which ones, and the gate lets each exact command through once.

Only the user's typed words count: a prompt the payload marks as not typed by
the user (`source` other than `user`), a notification, and pasted blocks are
left out. A message that both approves and refuses approves nothing.

Never blocks: it only adds context, and any error leaves the queue as it was.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core  # noqa: E402

STATE_DIR = os.path.expanduser('~/.claude/hooks')


def _shown(entry: dict) -> str:
    return f"#{entry.get('id')} `{playbook_core.shown_command(str(entry.get('command') or ''))}`"


def _context(answer: playbook_core.ConsentAnswer) -> str:
    if answer.ambiguous:
        waiting = ', '.join(_shown(entry) for entry in answer.still_pending)
        return ("[playbook:consent] The user's message reads as an answer about the queued "
                'removals, but nothing was approved: it also refuses, names a number not '
                'shown to them, or came before you listed the removals in a finished message. '
                f'List them and ask which may run, by number: {waiting}.')
    if not answer.approved:
        return ''
    lines = [
        '[playbook:consent] The user approved, in this message, the removals queued '
        'earlier. Run each one now, exactly as written; each runs once:',
        *(f'- {_shown(entry)}' for entry in answer.approved),
    ]
    if answer.still_pending:
        lines.append('Still waiting for the user: '
                     + ', '.join(_shown(entry) for entry in answer.still_pending) + '.')
    return '\n'.join(lines)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict) or data.get('hook_event_name') != 'UserPromptSubmit':
        sys.exit(0)
    session_id = str(data.get('session_id') or '')
    source = data.get('source')
    if not session_id or data.get('agent_id') or source not in (None, 'user'):
        sys.exit(0)
    try:
        words = playbook_core.typed_words(str(data.get('prompt') or ''))
        text = _context(playbook_core.consent_answer(STATE_DIR, session_id, words))
    except Exception:
        sys.exit(0)  # an error approves nothing: the queue stays as it was
    if text:
        sys.stdout.write(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'UserPromptSubmit', 'additionalContext': text}},
            separators=(',', ':')))
    sys.exit(0)


if __name__ == '__main__':
    main()
