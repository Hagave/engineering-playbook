#!/usr/bin/env python3
"""
Engineering Playbook — the answer from the button above the chat, and the
Temporary data panel.

Backs hooks/temporary_data.tsx, which draws what `show` returns and calls the
other actions on a press; every rule and every string the person reads is
here, so the mod holds none of its own.

Usage:
  temporary_data.py show    --session <id> --project <dir>
  temporary_data.py approve --session <id> --project <dir> --id <n>
  temporary_data.py decline --session <id> --project <dir> --id <n>
  temporary_data.py remove  --project <dir> --item <item id>

Each prints one JSON object. `approve`, `decline` and `remove` answer
`{"text": ..., "note": ..., "is_error": ...}`: `text` for the person, `note`
for the model (English, it reads the session), empty when it needs none.

`approve` runs the queued command exactly as the gate stored it, in the
directory the model's shell was in. The gate queued it only after every other
gate let it through, so nothing else is asked here. A command the queue holds
cut (`is_cut`) is never run from the button: the person answers in the chat.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hooks'))
import playbook_core  # noqa: E402
import playbook_i18n  # noqa: E402

STATE_DIR = os.path.expanduser('~/.claude/hooks')
# Under the mod's ten-minute process limit, so a slow deletion is reported
# by this script instead of cut off with nothing said.
RUN_TIMEOUT_S = 540
ERROR_SHOWN_CHARS = 300
PENDING_SHOWN = 3
UI_KEYS = ('tmp_waiting', 'tmp_hint', 'tmp_approve', 'tmp_decline', 'tmp_title', 'tmp_help',
           'tmp_close', 'tmp_empty', 'tmp_yes', 'tmp_no')


class UsageError(Exception):
    pass


def _options(argv: list[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    for at in range(0, len(argv) - 1, 2):
        name = argv[at]
        if not name.startswith('--'):
            raise UsageError(f'expected an option, got {name!r}')
        options[name[2:]] = argv[at + 1]
    if len(argv) % 2:
        raise UsageError(f'option {argv[-1]!r} has no value')
    return options


# The session id names the queue file: anything but these characters could
# point it elsewhere (`../`).
_SESSION_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,128}')


def _required(options: dict[str, str], *names: str) -> list[str]:
    missing = [name for name in names if not options.get(name)]
    if missing:
        raise UsageError('missing ' + ', '.join(f'--{name}' for name in missing))
    if 'session' in names and not _SESSION_ID_RE.fullmatch(options['session']):
        raise UsageError(f"--session is not a session id: {options['session']!r}")
    if 'project' in names and not os.path.isabs(options['project']):
        raise UsageError(f"--project must be an absolute path: {options['project']!r}")
    return [options[name] for name in names]


def _effect_text(language: str, effect: object) -> str:
    key = f'destroys_{effect}'
    return playbook_i18n.text(language, key) if key in playbook_i18n.STRINGS else str(effect)


def _language() -> str:
    return playbook_i18n.resolve(playbook_core.load_prefs(STATE_DIR)['language'])


def show(session_id: str, project_dir: str) -> dict:
    language = _language()
    say = playbook_i18n.text
    pending = playbook_core.consent_pending(STATE_DIR, session_id)
    items = playbook_core.temporary_data_items(STATE_DIR, project_dir)
    return {
        'ui': {key: say(language, key) for key in UI_KEYS},
        'pending': [{
            'id': entry.get('id'),
            'text': say(language, 'tmp_row', id=entry.get('id'),
                        effect=_effect_text(language, entry.get('effect')),
                        command=playbook_core.shown_command(str(entry.get('command') or ''))),
            'cut': say(language, 'tmp_cut', id=entry.get('id')) if entry.get('is_cut') else '',
        } for entry in pending[:PENDING_SHOWN]],
        'more': (say(language, 'tmp_more', count=len(pending) - PENDING_SHOWN)
                 if len(pending) > PENDING_SHOWN else ''),
        'open': say(language, 'tmp_open', count=len(items)) if items else '',
        'items': [{
            'id': item['id'],
            'text': say(language, 'tmp_item', effect=_effect_text(language, item.get('effect')),
                        command=playbook_core.shown_command(item['command']),
                        when=time.strftime('%Y-%m-%d %H:%M', time.localtime(item['ts']))),
            'confirm': say(language, 'tmp_confirm', cwd=item['cwd']),
            'command': playbook_core.shown_command(item['command']),
        } for item in items],
    }


def _run(command: str, cwd: str) -> tuple[int, str]:
    """Run `command` the way the Bash tool would, in `cwd`; its exit code and
    the end of what it wrote to stderr."""
    if not os.path.isdir(cwd):
        return 1, f'{cwd} no longer exists'
    try:
        ran = subprocess.run(['bash', '-c', command], cwd=cwd, capture_output=True, text=True,
                             timeout=RUN_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f'still running after {RUN_TIMEOUT_S}s; stopped'
    return ran.returncode, ran.stderr.strip()[-ERROR_SHOWN_CHARS:]


def approve(session_id: str, project_dir: str, number: int) -> dict:
    language = _language()
    say = playbook_i18n.text
    waiting = next((one for one in playbook_core.consent_pending(STATE_DIR, session_id)
                    if one.get('id') == number), None)
    if waiting is not None and waiting.get('is_cut'):
        return {'text': say(language, 'tmp_cut', id=number), 'note': '', 'is_error': True}
    entry = playbook_core.consent_decide(STATE_DIR, session_id, number)
    if entry is None:
        return {'text': say(language, 'tmp_gone'), 'note': '', 'is_error': False}
    command = str(entry.get('command') or '')
    code, error = _run(command, str(entry.get('cwd') or project_dir))
    if code != 0:
        return {
            'text': f"{say(language, 'tmp_ran', id=number)} "
                    f"{say(language, 'tmp_failed', code=code, error=error)}",
            'note': (f'[playbook:consent] The user approved #{number} with the button above the '
                     f'chat and the playbook ran it, but it failed (exit {code}): {error}. Do not '
                     'run it again yourself.'),
            'is_error': True,
        }
    return {
        'text': say(language, 'tmp_ran', id=number),
        'note': (f'[playbook:consent] The user approved #{number} with the button above the chat, '
                 f'and the playbook ran it (exit 0): `{playbook_core.shown_command(command)}`. Do '
                 'not run it again.'),
        'is_error': False,
    }


def decline(session_id: str, project_dir: str, number: int) -> dict:
    language = _language()
    say = playbook_i18n.text
    entry = playbook_core.consent_decide(STATE_DIR, session_id, number)
    if entry is None:
        return {'text': say(language, 'tmp_gone'), 'note': '', 'is_error': False}
    effect = str(entry.get('effect') or '')
    keep = (effect in playbook_core.DELETION_EFFECTS
            and playbook_core.load_prefs(STATE_DIR)['temporary_data']
            and not entry.get('is_cut'))
    if keep:
        playbook_core.temporary_data_add(
            STATE_DIR, project_dir, command=str(entry.get('command') or ''),
            cwd=str(entry.get('cwd') or project_dir), effect=effect, origin=session_id)
        return {'text': say(language, 'tmp_kept', id=number),
                'note': (f'[playbook:consent] The user declined #{number} with the button above '
                         'the chat. It was kept in the Temporary data panel, where the user '
                         'removes it when they choose: do not run it, and do not ask again.'),
                'is_error': False}
    return {'text': say(language, 'tmp_dropped', id=number),
            'note': (f'[playbook:consent] The user declined #{number} with the button above the '
                     'chat: do not run it, and carry on without it.'),
            'is_error': False}


def remove(project_dir: str, item_id: str) -> dict:
    language = _language()
    say = playbook_i18n.text
    item = next((one for one in playbook_core.temporary_data_items(STATE_DIR, project_dir)
                 if one['id'] == item_id), None)
    if item is None:
        return {'text': say(language, 'tmp_gone'), 'note': '', 'is_error': False}
    code, error = _run(item['command'], item['cwd'])
    if code != 0:
        return {'text': say(language, 'tmp_failed', code=code, error=error), 'note': '',
                'is_error': True}
    playbook_core.temporary_data_drop(STATE_DIR, project_dir, item_id)
    return {'text': say(language, 'tmp_removed'), 'note': '', 'is_error': False}


def main(argv: list[str]) -> int:
    action = argv[0] if argv else ''
    try:
        options = _options(argv[1:])
        if action == 'show':
            session_id, project_dir = _required(options, 'session', 'project')
            answer = show(session_id, project_dir)
        elif action in ('approve', 'decline'):
            session_id, project_dir, number = _required(options, 'session', 'project', 'id')
            if not number.isdigit():
                raise UsageError(f'--id must be a number, got {number!r}')
            answer = (approve if action == 'approve' else decline)(
                session_id, project_dir, int(number))
        elif action == 'remove':
            project_dir, item_id = _required(options, 'project', 'item')
            answer = remove(project_dir, item_id)
        else:
            raise UsageError(f'unknown action {action!r}: use show, approve, decline or remove')
    except (UsageError, ValueError) as error:
        print(f'[playbook:temporary-data] {error}', file=sys.stderr)
        return 2
    print(json.dumps(answer))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
