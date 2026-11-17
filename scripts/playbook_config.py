#!/usr/bin/env python3
"""
Engineering Playbook — preference CLI.

Backs the /playbook-config menu. The settings live behind a script rather than
behind instructions to edit JSON directly because
a preference written by hand is a preference written wrong: this validates the
value, reports what changed, and never leaves a half-written file behind.

Preferences live in the user's own state directory, not in the plugin. The
plugin ships the defaults; each user's choices stay on their machine.

Usage:
  playbook_config.py show [--json] [--harness claude|codex|antigravity]
  playbook_config.py get <key>
  playbook_config.py set <key> <value> [--harness claude|codex|antigravity]
  playbook_config.py session [--harness claude|codex|antigravity]

`--harness` (default `claude`) limits `show` and `set` to the settings that
harness can act on: a setting nothing reads there is not offered, rather than
accepted and silently ignored. `session` prints the policy text a harness
without a SessionStart hook (Antigravity) is told to read at session start.

`show --json` is what the Claude Code settings panel (hooks/config_panel.tsx)
draws from: each setting with its label, value and the choices it offers, and
the panel's own strings (`ui`), all in the language the `language` setting
resolves to, so the panel holds no list of keys, values or text of its own.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hooks'))
import playbook_core  # noqa: E402
import playbook_i18n  # noqa: E402

STATE_DIR = os.path.expanduser('~/.claude/hooks')

HARNESSES = ('claude', 'codex', 'antigravity')

# Which harness acts on which setting, and why the others cannot:
# - prose: Claude Code and Codex inject it at SessionStart; Antigravity has no
#   hooks, so its AGENTS.md tells the agent to read `session` at session start.
# - watch: a warning that does not block. Codex's PreToolUse accepts only allow
#   or deny, and Antigravity has no hooks, so neither can show one.
# - claude_md_budget: judged by the Stop hook from the sizes PreToolUse records
#   on writes to CLAUDE.md; only Claude Code reads CLAUDE.md as its memory.
# - context_budget: needs the token counts only Claude Code's transcript has.
# - language: only Claude Code's settings panel draws fixed text; elsewhere the
#   model writes the menu itself, in the language the user writes in.
HARNESS_SETTINGS = {
    'claude': ('prose', 'watch', 'claude_md_budget', 'context_budget', 'language'),
    'codex': ('prose',),
    'antigravity': ('prose',),
}

DESCRIPTIONS = {
    'prose': 'response verbosity (low | mid | high)',
    'watch': 'known-failure warnings before risky actions (on | off)',
    'claude_md_budget': 'characters the project CLAUDE.md may grow to (0 = off)',
    'context_budget': 'context tokens past which to wrap up and /clear (0 = off)',
    'language': 'language of the settings menu (auto | en | pt-br | es)',
}


def effect_note(key: str, value: object, language: str = 'en') -> str:
    """When a just-set preference takes effect, and what the user will notice,
    in `language`.

    Per key because each one is read at a different moment: prose only by the
    SessionStart hook, watch by PreToolUse (warnings) and SessionStart (the
    instruction to the model), the budgets by context_guard on every call. One
    generic line told the user a prose change was already in force; it was not."""
    say = playbook_i18n.text
    if key == 'prose':
        return say(language, 'effect_prose')
    if key == 'watch':
        return say(language, 'effect_watch_on' if value else 'effect_watch_off')
    if key in ('claude_md_budget', 'context_budget'):
        if not value:
            return say(language, f'effect_{key}_off')
        return say(language, f'effect_{key}', value=playbook_i18n.number(value, language))
    if key == 'language':
        shown = playbook_i18n.LANGUAGE_LABELS.get(str(value), str(value))
        return say(language, 'effect_language', value=shown)
    return say(language, 'effect_generic')


# The values the panel offers as buttons. `set` accepts any valid value; these
# are the common ones. Each is (value passed to `set`, label shown). A label
# that is a key of playbook_i18n.STRINGS ('on', 'off') is shown translated.
CHOICES: dict[str, list[tuple[str, str]]] = {
    'prose': [(level, level) for level in playbook_core.PROSE_LEVELS],
    'watch': [('on', 'on'), ('off', 'off')],
    'claude_md_budget': [('0', 'off'), ('20000', '20k'), ('40000', '40k'), ('80000', '80k')],
    'context_budget': [('0', 'off'), ('250000', '250k'), ('400000', '400k'),
                       ('550000', '550k'), ('800000', '800k')],
    'language': list(playbook_i18n.LANGUAGE_LABELS.items()),
}

_TRANSLATED_CHOICES = ('on', 'off')


def _as_choice(value: object) -> str:
    """A stored value in the form CHOICES spells it (`True` -> 'on')."""
    if isinstance(value, bool):
        return 'on' if value else 'off'
    return str(value)


def _choice_label(label: str, language: str) -> str:
    return playbook_i18n.text(language, label) if label in _TRANSLATED_CHOICES else label


def snapshot(prefs: dict, harness: str = 'claude', environ: dict | None = None) -> dict:
    """Every setting `harness` acts on, as the panel draws it, plus the panel's
    own strings: everything in the language `prefs['language']` resolves to.
    Settings come in the order DESCRIPTIONS lists them."""
    language = playbook_i18n.resolve(prefs['language'], environ)
    return {
        'language': language,
        'language_name': playbook_i18n.LANGUAGE_NAMES[language],
        'ui': playbook_i18n.ui(language),
        'settings': [
            {
                'key': key,
                'label': playbook_i18n.text(language, f'label_{key}'),
                'description': description,
                'help': playbook_i18n.text(language, f'help_{key}'),
                'value': _as_choice(prefs[key]),
                'default': _as_choice(playbook_core.PREFS_DEFAULTS[key]),
                'choices': [{'value': value, 'label': _choice_label(label, language)}
                            for value, label in CHOICES[key]],
            }
            for key, description in DESCRIPTIONS.items() if key in HARNESS_SETTINGS[harness]
        ],
    }


def _render(prefs: dict, harness: str = 'claude') -> str:
    rows = []
    for key, description in DESCRIPTIONS.items():
        if key not in HARNESS_SETTINGS[harness]:
            continue
        value = prefs[key]
        shown = ('on' if value else 'off') if isinstance(value, bool) else value
        default = playbook_core.PREFS_DEFAULTS[key]
        default_shown = ('on' if default else 'off') if isinstance(default, bool) else default
        mark = '' if value == default else '  (default: %s)' % default_shown
        rows.append(f'  {key:<17} {str(shown):<6} {description}{mark}')
    return '\n'.join(rows)


def session_policy(prefs: dict) -> str:
    """The policy text a session starts with, for a harness that reads it
    itself instead of having a SessionStart hook inject it."""
    return playbook_core.PROSE_POLICY.get(prefs['prose'], playbook_core.PROSE_POLICY['mid'])


def _take_harness(argv: list[str]) -> tuple[list[str], str | None]:
    """`argv` without `--harness <name>`, and the name ('claude' when absent,
    None when it names no harness)."""
    if '--harness' not in argv:
        return argv, 'claude'
    at = argv.index('--harness')
    name = argv[at + 1].lower() if at + 1 < len(argv) else ''
    return argv[:at] + argv[at + 2:], name if name in HARNESSES else None


def main(argv: list[str]) -> int:
    argv, harness = _take_harness(argv)
    if harness is None:
        print(f'--harness must be one of {", ".join(HARNESSES)}', file=sys.stderr)
        return 2
    action = (argv[0] if argv else 'show').lower()

    if action == 'show' and '--json' in argv[1:]:
        print(json.dumps(snapshot(playbook_core.load_prefs(STATE_DIR), harness)))
        return 0

    if action == 'show':
        print('[playbook:config] current preferences')
        print(_render(playbook_core.load_prefs(STATE_DIR), harness))
        return 0

    if action == 'session':
        print(session_policy(playbook_core.load_prefs(STATE_DIR)))
        return 0

    if action == 'get':
        if len(argv) < 2:
            print('usage: playbook_config.py get <key>', file=sys.stderr)
            return 2
        prefs = playbook_core.load_prefs(STATE_DIR)
        key = argv[1].lower()
        if key not in prefs:
            print(f'unknown preference {key!r}', file=sys.stderr)
            return 2
        print(prefs[key])
        return 0

    if action == 'set':
        if len(argv) < 3:
            print('usage: playbook_config.py set <key> <value>', file=sys.stderr)
            return 2
        if argv[1].lower() in DESCRIPTIONS and argv[1].lower() not in HARNESS_SETTINGS[harness]:
            print(f'[playbook:config] {argv[1].lower()} has no effect in {harness}: nothing '
                  'there reads it. Settings it acts on: '
                  + ', '.join(HARNESS_SETTINGS[harness]), file=sys.stderr)
            return 1
        prefs, error = playbook_core.set_pref(STATE_DIR, argv[1], ' '.join(argv[2:]))
        if error:
            print(f'[playbook:config] {error}', file=sys.stderr)
            return 1
        key = argv[1].lower()
        value = prefs[key]
        shown = ('on' if value else 'off') if isinstance(value, bool) else value
        print(f'[playbook:config] {key} = {shown}')
        print(effect_note(key, value, playbook_i18n.resolve(prefs['language'])))
        return 0

    print(f'unknown action {action!r} — use show, get, set or session', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
