#!/usr/bin/env python3
"""
UserPromptSubmit hook: reinforces governance rules on every user prompt.
Fail-open if governance disabled.
"""

import json
import os
import re
import sys

_HOME = os.path.expanduser('~')


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


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    if not _governance_enabled():
        sys.exit(0)

    sys.stdout.write(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'UserPromptSubmit',
            'additionalContext': (
                'Engineering Playbook is ACTIVE (full rules injected at SessionStart). '
                "No hallucination: every action traces to the user's request, code read "
                'in the repo, or a loaded skill rule. Scope discipline: do not add '
                'unrequested changes. For non-trivial tasks, load engineering-implement '
                'before writing code and emit [SPEC CONFIRMED] in your response text '
                'before the first Write/Edit.'
            ),
        },
    }, separators=(',', ':')))
    sys.exit(0)


if __name__ == '__main__':
    main()
