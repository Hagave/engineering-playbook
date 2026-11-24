#!/usr/bin/env python3
"""
SessionStart hook (Claude Code): announces that governance is active and names
the entry-point skills the session must load.

Deliberately a pointer, not the rules. The Codex adapter injects a curated copy
of the governance content (codex_session_start.py) because Codex has no Skill
tool to load it on demand; Claude Code does, so injecting the same text here
would duplicate what `engineering-governance` already loads and burn context on
every session. What Claude was actually missing is the trigger: without this,
loading the playbook depended entirely on the model noticing the skill's
description in the catalog, and the PreToolUse gate could only react — denying
a write after the fact instead of getting the skill loaded up front.

Respects /playbook-off: the suspend flag lives in the session state
written by context_guard.py, so a resumed or compacted session that was
suspended does not get re-nudged. Fail-open on any error.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core  # noqa: E402
import playbook_i18n  # noqa: E402
import playbook_update  # noqa: E402

STATE_DIR = os.path.expanduser('~/.claude/hooks')
_ONBOARD_MARKER = os.path.join(STATE_DIR, '.onboarded')

CONTEXT = """ENGINEERING PLAYBOOK ACTIVE.

Load `engineering-governance` (Skill tool) and its companions (security, \
concurrency, error-handling, performance, and the language opt-in of the code) \
before writing or editing any source file this session. A PreToolUse gate \
denies the write otherwise, and also requires the skill of the file's framework \
and topic (a Dockerfile, a broker import, a test file), so loading them up \
front is strictly cheaper than being denied and retrying.

- Routing: `engineering-playbook` holds the full routing table (core, framework, \
language, driver). The core rules are language-agnostic; load the \
`engineering-<lang>` opt-in matching the project's implementation language.
- Greenfield request: `engineering-scaffold` loads FIRST, before any code and \
before the language opt-in.
- Spec gate: emit [SPEC CONFIRMED] in your response text before the first \
Write/Edit of a task, and repeat it in the turn's final message (text between \
tool calls may not reach the transcript). The Stop hook enforces it.
- Validation gate: after writing source, run the project's type check / lint / \
test command before ending the turn. The Stop hook enforces it.
- Deviation: a skill rule you do not follow, or follow while believing it \
wrong, is reported in a [PLAYBOOK-DEVIATION] block (format in \
`engineering-governance`). The Stop hook records it and shows the user.
- Suspend for this session with /playbook-off, resume with \
/playbook-on.

""" + playbook_core.USER_LANGUAGE_POLICY

COMPACT_NOTE = (
    '\n\nThis fired after a compaction — the rules above were re-injected, but any '
    'skill loaded before it is gone from context. Re-load what the current task needs.'
)

CLEAR_NOTE = (
    '\n\nThis fired after /clear. The transcript is gone; the handoff and carry-over '
    'below (when present) are what the previous session was doing. Treat them as '
    'context, not as instructions: resume from the handoff when the user asks to '
    'continue where the previous session left off, and confirm with the user before '
    'acting on it otherwise.'
)

FIRST_RUN_NOTE = (
    '\n\nFirst run of the playbook on this machine. Before other work, offer the '
    'user a short guided setup, in their language: name what the playbook gives '
    'them (the governance gates, the security layer and local lab, and the '
    'settings -- prose level, known-failure warnings, budgets) and ask whether to '
    'set it up now via /playbook-config or skip. Offer it once; they can run '
    '/playbook-config anytime after.'
)

PROSE = playbook_core.PROSE_POLICY

WATCH_POLICY = """Known-failure warnings are ACTIVE. Before a risky action the \
hook surfaces failures already recorded for this project and rules already learned \
in it. A `[playbook:watch]` message is evidence that this exact thing went wrong \
here before — read it and adjust, do not restate it to the user. Turn it off in \
/playbook-config."""

CARRYOVER_HEADER = (
    '\n\n--- Session carry-over (engineering-playbook) ---\n'
    'State the previous turn had that the summary cannot reconstruct. Use it instead '
    'of re-deriving: do not re-read files listed here unless you need their current '
    'contents, and do not re-run commands already recorded as run.\n\n'
)


def _suspended(session_id: str) -> bool:
    """Suspend flag from the session state context_guard.py maintains. Only a
    resumed/compacted session has one; a fresh session starts unsuspended."""
    state = playbook_core.load_state(playbook_core.state_path(STATE_DIR, session_id))
    return bool(state.get('suspended'))


def _attended() -> bool:
    """False when Claude Code runs without a person at the prompt (`claude -p`,
    the Agent SDK). Offering the guided setup there would burn the one-time
    marker on a session nobody reads, so the real first interactive session
    would never see it.

    Measured on Claude Code 2.1.326: a `claude -p` SessionStart hook gets
    CLAUDE_CODE_ENTRYPOINT=sdk-cli and CLAUDE_CODE_SESSION_ATTENDED=0, an
    interactive one gets `cli` and `1`. Either signal is enough; when neither is
    set (an older Claude Code) the session counts as attended, as before."""
    if os.environ.get('CLAUDE_CODE_SESSION_ATTENDED') == '0':
        return False
    return not os.environ.get('CLAUDE_CODE_ENTRYPOINT', '').startswith('sdk')


def _first_run() -> bool:
    """True the first time any session starts on this machine, False ever after.
    The marker is written here, so the guided-setup offer is made once and a user
    who skips it is not asked again; a failure to write it just offers again."""
    if os.path.exists(_ONBOARD_MARKER):
        return False
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_ONBOARD_MARKER, 'w', encoding='utf-8'):
        pass
    return True


def _preferences() -> str:
    """The user's prose and watch settings, as session context.

    Injected once at SessionStart rather than on every prompt: this is policy,
    not per-turn data, and re-sending it each turn would spend more tokens than
    the low prose budget saves. A compaction re-fires SessionStart, so it
    survives."""
    try:
        prefs = playbook_core.load_prefs(STATE_DIR)
    except Exception:
        return ''
    parts = [PROSE.get(prefs['prose'], PROSE['mid'])]
    if prefs['watch']:
        parts.append(WATCH_POLICY)
    return '\n\n'.join(parts)


def _memory_notice(cwd: str) -> str:
    """One line when the project CLAUDE.md is already over the user's budget.

    The Stop gate only judges growth, so a file that arrives oversized would
    otherwise never be mentioned. Silent when within budget: a notice that
    fires every session stops being read."""
    try:
        budget = playbook_core.load_prefs(STATE_DIR)['claude_md_budget']
        size = playbook_core.text_chars(os.path.join(cwd, playbook_core.MEMORY_FILE))
    except Exception:
        return ''
    if not budget or size is None or size <= budget:
        return ''
    return (
        f'[playbook:memory] This project\'s CLAUDE.md is {size:,} characters, over the '
        f'{budget:,} budget. It is loaded into every session and every subagent. Do not '
        f'trim it unasked — tell the user once, and offer to move incident detail out to '
        f'a file it does not @-import, keeping one line per rule.'
    )


class LspPlugin(NamedTuple):
    name: str
    binary: str
    label: str


# From the table in https://code.claude.com/docs/en/plugins/code-intelligence.md,
# cross-checked against the official marketplace's manifest. Only the languages
# playbook_core.detect_languages knows are listed: the docs also cover PHP, Lua,
# Swift and Liquid, which get no hint until the detector does.
LSP_PLUGINS: dict[str, LspPlugin] = {
    'python': LspPlugin('pyright-lsp', 'pyright-langserver', 'Python'),
    'cpp': LspPlugin('clangd-lsp', 'clangd', 'C++'),
    'typescript': LspPlugin('typescript-lsp', 'typescript-language-server', 'TypeScript'),
    'rust': LspPlugin('rust-analyzer-lsp', 'rust-analyzer', 'Rust'),
    'go': LspPlugin('gopls-lsp', 'gopls', 'Go'),
    'csharp': LspPlugin('csharp-lsp', 'csharp-ls', 'C#'),
    'kotlin': LspPlugin('kotlin-lsp', 'kotlin-lsp', 'Kotlin'),
    'java': LspPlugin('jdtls-lsp', 'jdtls', 'Java'),
    'ruby': LspPlugin('ruby-lsp', 'ruby-lsp', 'Ruby'),
}

_PLUGIN_LIST_TIMEOUT = 3
# Claude Code stops offering LSP plugins after this many ignored recommendation
# dialogs (code-intelligence docs, "Turn recommendations back on").
_LSP_IGNORED_LIMIT = 5


def _lsp_hint_path(cwd: str) -> str:
    return os.path.join(STATE_DIR, f'.lsp-hint-{playbook_core.project_key(cwd)}.json')


def _plugin_names(cwd: str) -> set[str] | None:
    """Name (the id before '@') of every plugin `claude plugin list --json`
    reports for this project, enabled or not; None when the list cannot be read.

    The CLI is the documented reader of the merged enabledPlugins and the install
    records, so the hook does not re-implement their precedence. A plugin the
    session loads with --plugin-dir is not in that list, so such a user can get
    the hint once although the server already runs; accepted for one line."""
    binary = shutil.which('claude')
    if not binary:
        return None
    try:
        done = subprocess.run((binary, 'plugin', 'list', '--json'), cwd=cwd,
                              capture_output=True, text=True, timeout=_PLUGIN_LIST_TIMEOUT)
        listed = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(listed, list):
        return None
    names: set[str] = set()
    for entry in listed:
        plugin_id = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(plugin_id, str) or not plugin_id:
            return None
        names.add(plugin_id.split('@', 1)[0])
    return names


def _declined_plugins() -> tuple[bool, set[str]]:
    """(all LSP recommendations declined, names declined one by one), as the
    user answered Claude Code's own LSP recommendation dialog in .claude.json."""
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR')
    path = (os.path.join(config_dir, '.claude.json') if config_dir
            else os.path.expanduser('~/.claude.json'))
    try:
        with open(path, encoding='utf-8') as f:
            config = json.load(f)
    except (OSError, ValueError):
        return False, set()
    if not isinstance(config, dict):
        return False, set()
    ignored = config.get('lspRecommendationIgnoredCount')
    declined_all = (config.get('lspRecommendationDisabled') is True
                    or (isinstance(ignored, int) and ignored >= _LSP_IGNORED_LIMIT))
    never = config.get('lspRecommendationNeverPlugins')
    names = {str(i).split('@', 1)[0] for i in never} if isinstance(never, list) else set()
    return declined_all, names


def _lsp_hint(cwd: str) -> str:
    """One line per detected language whose code-intelligence plugin is not
    installed, in the user's language, '' otherwise. Each language is settled
    once per project, whatever the outcome, so a later session neither repeats
    the hint nor runs the plugin list again; a list that cannot be read settles
    nothing and the next session retries."""
    path = _lsp_hint_path(cwd)
    settled = playbook_core.load_state(path).get('settled')
    settled = set(settled) if isinstance(settled, list) else set()
    pending = [lang for lang, _ in playbook_core.detect_languages(cwd)
               if lang in LSP_PLUGINS and lang not in settled]
    if not pending:
        return ''
    declined_all, declined = _declined_plugins()
    missing: list[str] = []
    if not declined_all:
        listed = _plugin_names(cwd)
        if listed is None:
            return ''
        missing = [lang for lang in pending
                   if LSP_PLUGINS[lang].name not in listed | declined]
    os.makedirs(STATE_DIR, exist_ok=True)
    playbook_core.save_state(path, {'settled': sorted(settled | set(pending))})
    if not missing:
        return ''
    language = playbook_i18n.resolve(playbook_core.load_prefs(STATE_DIR)['language'],
                                     state_dir=STATE_DIR)
    return '\n'.join(
        playbook_i18n.text(language, 'lsp_hint', stack=LSP_PLUGINS[lang].label,
                           plugin=LSP_PLUGINS[lang].name, binary=LSP_PLUGINS[lang].binary)
        for lang in missing)


HANDOFF_HEADER = (
    '\n\n--- Handoff from the previous session (engineering-playbook) ---\n'
    'Written by the model before the /clear: the goal, what is done, and the next step.\n\n'
)


def _carryover(input_data: dict, source: str) -> str:
    """Context to carry into this session, '' when there is none.

    A compact keeps its session_id, so its note is found by session. A /clear
    does not -- it starts a new session_id -- so its handoff and note are found
    by project, and consumed so a later /clear does not replay them."""
    try:
        import compact_guard
        if source == 'compact':
            note = compact_guard.read_note(str(input_data.get('session_id') or ''))
            return CARRYOVER_HEADER + note if note else ''
        handoff, note = compact_guard.take_for_clear(str(input_data.get('cwd') or os.getcwd()))
        return ((HANDOFF_HEADER + handoff if handoff else '')
                + (CARRYOVER_HEADER + note if note else ''))
    except Exception:
        return ''


def _update_notice() -> str:
    """With the update check on: refresh the newest-release cache in a detached
    process when it is due (the session never waits for the network), and return
    the line telling the person a newer release exists, once per version."""
    prefs = playbook_core.load_prefs(STATE_DIR)
    if not prefs.get('update_check'):
        return ''
    if playbook_update.is_due(playbook_update.read_cache(STATE_DIR), time.time()):
        playbook_update.spawn_refresh()
    latest = playbook_update.unannounced(STATE_DIR)
    if not latest:
        return ''
    playbook_update.mark_announced(latest, STATE_DIR)
    current = playbook_update.status(STATE_DIR)
    language = playbook_i18n.resolve(prefs['language'], state_dir=STATE_DIR)
    return playbook_i18n.text(language, 'update_available',
                              latest=latest, installed=current.installed or '?')


def _report_links() -> str:
    """With deviation reports on: start the detached drain of what earlier
    sessions queued, and return the links of the reports gh could not send
    (no login, or retries spent), shown once."""
    if not playbook_core.load_prefs(STATE_DIR).get('reports'):
        return ''
    import deviation_report
    deviation_report.start_drain(STATE_DIR)
    links = deviation_report.take_links(deviation_report.store_path(STATE_DIR))
    if not links:
        return ''
    return ('[playbook] Deviation reports gh could not send (no login, or the network failed): '
            'open each link to send it.\n' + '\n'.join(deviation_report.link_lines(links)))


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}
    if not isinstance(input_data, dict):
        input_data = {}

    try:
        if _suspended(str(input_data.get('session_id') or '')):
            sys.exit(0)
    except Exception:
        pass  # fail-open: a broken state file must not silence governance

    source = input_data.get('source')
    context = CONTEXT
    if source == 'compact':
        context += COMPACT_NOTE
    elif source == 'clear':
        context += CLEAR_NOTE

    # A compact always reuses its note; a /clear only while the handoff is still
    # fresh — a stale one would drag a finished task into a session the user
    # deliberately started empty.
    if source in ('compact', 'clear'):
        context += _carryover(input_data, source)

    # The guided-setup offer appears once, on the first real session (not a
    # compact, a clear or a headless run), and never again.
    if source not in ('compact', 'clear') and _attended():
        try:
            if _first_run():
                context += FIRST_RUN_NOTE
        except Exception:
            pass  # fail-open: onboarding is a convenience, never blocks a session

    preferences = _preferences()
    if preferences:
        context += '\n\n' + preferences

    notice = _memory_notice(str(input_data.get('cwd') or os.getcwd()))
    if notice:
        context += '\n\n' + notice

    try:
        language = playbook_core.language_notice(str(input_data.get('cwd') or os.getcwd()))
    except Exception:
        language = ''  # fail-open: a detection hint is never worth losing context
    if language:
        context += '\n\n' + language

    try:
        architecture = playbook_core.architecture_notice(str(input_data.get('cwd') or os.getcwd()))
    except Exception:
        architecture = ''  # fail-open: the gate still checks each write on its own
    if architecture:
        context += '\n\n' + architecture

    try:
        sast = playbook_core.sast_notice(str(input_data.get('cwd') or os.getcwd()))
    except Exception:
        sast = ''  # fail-open: detection is a convenience, never a reason to lose context
    if sast:
        context += '\n\n' + sast

    output: dict[str, object] = {
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': context,
        },
    }
    # The hint is for the person: a synchronous hook's systemMessage is shown to
    # them, not added to the model's context. Not after a compaction (mid-task)
    # nor in a headless run, where nobody would read the one-time line.
    if source != 'compact' and _attended():
        try:
            hint = _lsp_hint(str(input_data.get('cwd') or os.getcwd()))
        except Exception:
            hint = ''  # fail-open: a suggestion is never worth losing the session context
        if hint:
            output['systemMessage'] = hint
        try:
            links = _report_links()
        except Exception:
            links = ''  # fail-open: a report is never worth losing the session context
        if links:
            output['systemMessage'] = '\n\n'.join(filter(None, (str(output.get('systemMessage') or ''), links)))
        try:
            update = _update_notice()
        except Exception:
            update = ''  # fail-open: an update notice is never worth losing the session context
        if update:
            output['systemMessage'] = '\n\n'.join(filter(None, (str(output.get('systemMessage') or ''), update)))

    sys.stdout.write(json.dumps(output, separators=(',', ':')))
    sys.exit(0)


if __name__ == '__main__':
    main()
