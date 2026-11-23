#!/usr/bin/env python3
"""UserPromptSubmit: remember the language the user writes in.

The text the playbook shows without a model writing it (permission dialogs, the
settings panel) follows the `language` preference; for `auto` that is the
language of the user's last clear message, then the system locale. Without this
a Portuguese speaker on an English locale gets every dialog in English.

Reads only what the user typed (`playbook_core.typed_words`: not a pasted
block, a notification or a subagent's report). Never blocks and never adds
context: any error leaves the language as it was.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core
import playbook_i18n


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)
    if (not isinstance(data, dict) or data.get('hook_event_name') != 'UserPromptSubmit'
            or data.get('agent_id') or data.get('source') not in (None, 'user')):
        sys.exit(0)
    prompt = data.get('prompt')
    if isinstance(prompt, str):
        language = playbook_i18n.detect(playbook_core.typed_words(prompt))
        if language:
            playbook_i18n.remember(language)
    sys.exit(0)


if __name__ == '__main__':
    main()
