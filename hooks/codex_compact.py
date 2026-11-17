#!/usr/bin/env python3
"""
Codex PreCompact/PostCompact hook: acknowledges chat compaction without
attempting to reimplement Claude Code's percentage-based context tracking.

hooks/context_guard.py's [playbook:context] warn/block at 70%/85% relies on
per-turn token usage numbers read from Claude's transcript. Codex's
documented hook contract (developers.openai.com/codex/hooks) does not
expose token or usage counts to any hook — PreCompact/PostCompact receive
only session_id, transcript_path, cwd, hook_event_name, turn_id, trigger,
and permission_mode. There is no data this hook could use to reimplement
that feature, so it does not try to.

What Codex's own hook contract does give: PreCompact/PostCompact fire
around Codex's own compaction (trigger: "manual" or "auto"), and
SessionStart re-fires afterward with source == "compact" — meaning
codex_session_start.py's governance content is already re-injected for
free after any compaction. This hook only surfaces that compaction
happened, via systemMessage, so the model is aware.

PreCompact intentionally never returns continue: false — blocking Codex's
own compaction decision is out of scope and not something this adapter has
grounds to judge.

Reads ~/.codex/config.toml for [playbook] governance = false to disable.
Fail-open.
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
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}

    if not _governance_enabled():
        sys.exit(0)

    event = input_data.get('hook_event_name') or ''
    trigger = input_data.get('trigger') or 'unknown'

    if event == 'PreCompact':
        message = f'[playbook:compact] Compaction starting (trigger: {trigger}).'
    else:
        message = (
            f'[playbook:compact] Compaction complete (trigger: {trigger}). '
            'Engineering playbook governance has been re-primed via SessionStart.'
        )

    sys.stdout.write(json.dumps({'systemMessage': message}, separators=(',', ':')))
    sys.exit(0)


if __name__ == '__main__':
    main()
