#!/usr/bin/env python3
"""
Opt-in reports of playbook deviations to the playbook's maintainer.

With the `reports` setting on (off by default, /playbook-config), a deviation
block the Stop hook records is also offered to the maintainer as a GitHub
issue. What leaves the machine is fixed by construction, not by a filter:

- the rule only as a word-for-word quote found in the installed skill's text,
  which is already public; a paraphrase stays in the local journal only;
- the skill, its version and section, the action, the playbook version, the
  harness and the model, and for a deviation whose `why` names a dependency
  version (`version <name>@<version>:`) that pair alone;
- never the project folder, the session, `instead`, the rest of `why`, code.

The issue is created with the user's own GitHub account through `gh`; without
`gh`, or without a login, the user gets a pre-filled link instead. A rule
already reported (same skill, version and quote) gets a +1 reaction rather
than a second issue. Sending never runs inside a hook: the Stop hook queues,
and `deviation_report.py drain`, started detached, sends.

Usage:
  deviation_report.py drain [--state-dir <dir>]
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows: no advisory lock; two drains at once can send a report twice
    fcntl = None  # type: ignore[assignment]

STATE_DIR = os.path.expanduser('~/.claude/hooks')
REPORT_REPO = 'Hagave/engineering-playbook'
REPORT_REPO_ENV = 'PLAYBOOK_REPORT_REPO'
STORE_ENV = 'PLAYBOOK_REPORT_STORE'
TITLE_PREFIX = '[deviation-report]'
MIN_QUOTE_CHARS = 30
DAILY_CREATE_CAP = 5
MAX_PENDING = 50
MAX_ATTEMPTS = 5
RETRY_BASE_S = 600
# A drain that dies after `gh issue create` and before recording it leaves the
# report leased; past the lease it is sent again, and the fingerprint search
# turns it into a +1 only once GitHub has indexed the first issue. Accepted: a
# rare duplicate issue costs less than a report lost to a crash.
LEASE_S = 300
GH_TIMEOUT_S = 20
GH_EXIT_AUTH = 4
SENT_KEPT = 500

_PLUGIN_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            '.claude-plugin', 'plugin.json')
_QUOTE_SPLIT_RE = re.compile(r'\s+[—–-]\s+|\s*[:;]\s+|["“”«»]|\(|\)')
_MARKUP_RE = re.compile(r'[*_`]')
_SPACE_RE = re.compile(r'\s+')
_HEADING_RE = re.compile(r'^(#{2,4})\s+(.+?)\s*$', re.MULTILINE)
_VERSION_WHY_RE = re.compile(r'^\s*version\s+(@?[\w.-]+(?:/[\w.-]+)?)@(\d+(?:\.\d+){0,2})\s*:', re.IGNORECASE)
_ISSUE_URL_RE = re.compile(r'https://github\.com/[\w.-]+/[\w.-]+/issues/(\d+)')

Runner = Callable[..., subprocess.CompletedProcess]


def store_path(state_dir: str = STATE_DIR) -> str:
    return os.environ.get(STORE_ENV) or os.path.join(state_dir, '.deviation-reports.json')


def report_repo() -> str:
    return os.environ.get(REPORT_REPO_ENV) or REPORT_REPO


def playbook_version() -> str:
    try:
        with open(_PLUGIN_JSON, encoding='utf-8') as f:
            version = json.load(f).get('version')
    except (OSError, ValueError):
        return ''
    return version if isinstance(version, str) else ''


# ─── What may leave the machine ──────────────────────────────────────────────

def _normalize(text: str) -> str:
    return _SPACE_RE.sub(' ', _MARKUP_RE.sub('', text)).strip().lower()


def _skill_text(skill: str) -> str:
    if not re.fullmatch(r'[\w-]+', skill or ''):
        return ''
    try:
        with open(os.path.join(playbook_core._SKILLS_ROOT, skill, 'SKILL.md'), encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def quote_in_skill(skill: str, rule: str) -> tuple[str, str] | None:
    """(section heading, quote) for the longest piece of `rule` that the skill
    states word for word, ignoring markup and spacing; None when no piece of at
    least MIN_QUOTE_CHARS is found. The quote is returned as `rule` wrote it,
    normalized: it is the skill's text, so it carries nothing of the project."""
    text = _skill_text(skill)
    if not text:
        return None
    displays = {_SPACE_RE.sub(' ', _MARKUP_RE.sub('', p)).strip(' .,') for p in _QUOTE_SPLIT_RE.split(rule or '')}
    for display in sorted(displays, key=len, reverse=True):
        if len(display) < MIN_QUOTE_CHARS:
            break
        piece = display.lower()
        if piece not in _normalize(text):
            continue
        for heading_match, next_match in _sections(text):
            body = text[heading_match.end():next_match.start() if next_match else len(text)]
            if piece in _normalize(body):
                return heading_match.group(2).strip(), display
        return '', display
    return None


def _sections(text: str) -> list[tuple[re.Match[str], re.Match[str] | None]]:
    headings = list(_HEADING_RE.finditer(text))
    return list(zip(headings, [*headings[1:], None]))


def build_report(fields: Mapping[str, str], skill_version: str, model: str, harness: str,
                 version: str) -> dict[str, str] | None:
    """The fixed fields a deviation is reported with, or None when it cannot be
    reported without sending text that is not the skill's own."""
    skill = fields.get('skill', '')
    action = fields.get('action', '')
    if not skill_version or action not in playbook_core.DEVIATION_ACTIONS:
        return None
    found = quote_in_skill(skill, fields.get('rule', ''))
    if not found:
        return None
    section, quote = found
    dependency = _VERSION_WHY_RE.match(fields.get('why', ''))
    report = {
        'skill': skill,
        'skill_version': skill_version,
        'section': section,
        'rule': quote,
        'action': action,
        'reason': 'version' if dependency else 'other',
        'dependency': f'{dependency.group(1)}@{dependency.group(2)}' if dependency else '',
        'playbook_version': version,
        'harness': harness,
        'model': model if re.fullmatch(r'[\w.\[\]-]{1,80}', model or '') else '',
    }
    report['fingerprint'] = fingerprint(report)
    return report


def fingerprint(report: Mapping[str, str]) -> str:
    key = '\n'.join((report['skill'], report['skill_version'], report['rule']))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]


def issue_title(report: Mapping[str, str]) -> str:
    where = f' — {report["section"]}' if report['section'] else ''
    return f'{TITLE_PREFIX} {report["skill"]} {report["skill_version"]}{where}'[:200]


def issue_body(report: Mapping[str, str]) -> str:
    rows = [
        ('skill', f'{report["skill"]} {report["skill_version"]}'),
        ('section', report['section'] or '(not found)'),
        ('action', report['action']),
        ('reason', report['reason'] + (f' ({report["dependency"]})' if report['dependency'] else '')),
        ('playbook', report['playbook_version'] or '(unknown)'),
        ('harness', report['harness']),
        ('model', report['model'] or '(unknown)'),
    ]
    table = '\n'.join(f'| {name} | {value} |' for name, value in rows)
    return (
        'A model reported a deviation from this rule (the quote is the skill\'s own text):\n\n'
        f'> {report["rule"]}\n\n| field | value |\n|---|---|\n{table}\n\n'
        f'fingerprint: `{report["fingerprint"]}`\n\n'
        'Sent by the opt-in deviation report (`reports` in /playbook-config). It carries no '
        'project data: no folder, code, session or reasoning. A +1 on this issue is another '
        'user meeting the same rule.\n'
    )


def new_issue_url(report: Mapping[str, str], repo: str | None = None) -> str:
    query = urllib.parse.urlencode({'title': issue_title(report), 'body': issue_body(report)})
    return f'https://github.com/{repo or report_repo()}/issues/new?{query}'


# ─── Store: queue, results, daily cap ────────────────────────────────────────

def _empty_store() -> dict:
    return {'pending': [], 'needs_link': [], 'sent': {}, 'day': '', 'created_today': 0, 'hinted': False}


def _load(path: str) -> dict:
    store = _empty_store()
    try:
        with open(path, encoding='utf-8') as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return store
    if isinstance(stored, dict):
        for key, default in store.items():
            if isinstance(stored.get(key), type(default)):
                store[key] = stored[key]
    return store


@contextmanager
def _locked(path: str) -> Iterator[dict]:
    """The store, loaded under an exclusive lock and saved atomically on exit.
    Held only for local reads and writes, never across a network call."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path + '.lock', 'a') as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            store = _load(path)
            yield store
            store['sent'] = dict(list(store['sent'].items())[-SENT_KEPT:])
            playbook_core.save_state(path, store)
        finally:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _known(store: dict, fp: str) -> bool:
    return (fp in store['sent'] or any(r.get('fingerprint') == fp for r in store['pending'])
            or any(r.get('fingerprint') == fp for r in store['needs_link']))


def enqueue(path: str, reports: list[dict[str, str]], gh_available: bool) -> list[dict[str, str]]:
    """Queue the reports not seen before. With `gh` on the PATH they wait for
    the drain; without it they are returned at once, for the caller to show
    their links, and marked as linked."""
    shown: list[dict[str, str]] = []
    with _locked(path) as store:
        for report in reports:
            if _known(store, report['fingerprint']):
                continue
            if gh_available and len(store['pending']) < MAX_PENDING:
                store['pending'].append({**report, 'attempts': 0, 'next_try': 0, 'lease': 0})
            else:
                store['sent'][report['fingerprint']] = 'linked'
                shown.append(report)
    return shown


def take_links(path: str) -> list[dict[str, str]]:
    """The reports the drain could not send, to show as links once."""
    with _locked(path) as store:
        links = store['needs_link']
        store['needs_link'] = []
        for report in links:
            store['sent'][report['fingerprint']] = 'linked'
    return links


def hint_once(path: str) -> bool:
    """True the first time only: the one notice that reports exist and are off."""
    with _locked(path) as store:
        if store['hinted']:
            return False
        store['hinted'] = True
    return True


def _clean(report: Mapping[str, object]) -> dict[str, str]:
    return {k: str(v) for k, v in report.items() if k not in ('attempts', 'next_try', 'lease')}


# ─── Sending ─────────────────────────────────────────────────────────────────

class GhError(Exception):
    def __init__(self, message: str, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def _gh(run: Runner, *args: str, body: str | None = None) -> str:
    """stdout of `gh <args>`; raises GhError. Exit 4 is gh's documented code for
    a command that needs authentication (`gh help exit-codes`)."""
    body_file = ''
    if body is not None:
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write(body)
            body_file = f.name
    try:
        argv = ['gh', *args] + (['--body-file', body_file] if body_file else [])
        done = run(argv, capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GhError(f'gh {args[0]} did not run: {error}') from error
    finally:
        if body_file:
            os.unlink(body_file)
    if done.returncode != 0:
        # `gh issue create` can exit non-zero after creating the issue (a failed
        # attachment); its URL on stdout still means the issue exists.
        if args[:2] == ('issue', 'create') and _ISSUE_URL_RE.search(done.stdout or ''):
            return done.stdout
        raise GhError(f'gh {" ".join(args[:2])} exited {done.returncode}: {(done.stderr or "").strip()[:300]}',
                      auth=done.returncode == GH_EXIT_AUTH)
    return done.stdout


def send(report: Mapping[str, str], repo: str, run: Runner) -> str:
    """'created' or 'reacted'. A rule another user already reported gets a +1
    reaction on that issue (idempotent per user) instead of a second issue."""
    found = _gh(run, 'issue', 'list', '--repo', repo, '--state', 'all', '--limit', '1',
                '--search', f'{report["fingerprint"]} in:body', '--json', 'number')
    try:
        issues = json.loads(found or '[]')
    except ValueError as error:
        raise GhError(f'gh issue list printed no JSON: {found[:200]!r}') from error
    if isinstance(issues, list) and issues and isinstance(issues[0], dict) and isinstance(issues[0].get('number'), int):
        _gh(run, 'api', '--method', 'POST', f'repos/{repo}/issues/{issues[0]["number"]}/reactions',
            '-f', 'content=+1')
        return 'reacted'
    _gh(run, 'issue', 'create', '--repo', repo, '--title', issue_title(report), body=issue_body(report))
    return 'created'


def _lease(path: str, now: float) -> list[dict]:
    with _locked(path) as store:
        today = time.strftime('%Y-%m-%d', time.localtime(now))
        if store['day'] != today:
            store['day'], store['created_today'] = today, 0
        budget = DAILY_CREATE_CAP - store['created_today']
        taken = []
        for report in store['pending']:
            if len(taken) >= budget:
                break
            if report.get('next_try', 0) <= now and report.get('lease', 0) <= now:
                report['lease'] = now + LEASE_S
                taken.append(dict(report))
    return taken


def _record(path: str, fp: str, outcome: str | None, error: GhError | None, now: float) -> None:
    with _locked(path) as store:
        report = next((r for r in store['pending'] if r.get('fingerprint') == fp), None)
        if report is None:
            return
        if outcome:
            store['pending'].remove(report)
            store['sent'][fp] = outcome
            store['created_today'] += outcome == 'created'
            return
        report['attempts'] = report.get('attempts', 0) + 1
        report['lease'] = 0
        if (error and error.auth) or report['attempts'] >= MAX_ATTEMPTS:
            store['pending'].remove(report)
            store['needs_link'].append(_clean(report))
        else:
            report['next_try'] = now + RETRY_BASE_S * 2 ** (report['attempts'] - 1)


def drain(path: str, repo: str | None = None, run: Runner = subprocess.run,
          now: Callable[[], float] = time.time) -> dict[str, int]:
    """Send what is due. A report that fails is retried with backoff; one that
    fails MAX_ATTEMPTS times, or any report when gh has no login, moves to the
    links the next session start shows."""
    counts = {'created': 0, 'reacted': 0, 'failed': 0}
    leased = _lease(path, now())
    for index, report in enumerate(leased):
        try:
            outcome = send(report, repo or report_repo(), run)
        except GhError as error:
            counts['failed'] += 1
            # Without a login every other call fails the same way: stop and give links.
            for failed in leased[index:] if error.auth else [report]:
                _record(path, failed['fingerprint'], None, error, now())
            if error.auth:
                break
            continue
        counts[outcome] += 1
        _record(path, report['fingerprint'], outcome, None, now())
    return counts


def gh_available() -> bool:
    return shutil.which('gh') is not None


def start_drain(state_dir: str = STATE_DIR) -> None:
    """Run the drain detached: the hook that queued never waits on the network."""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), 'drain', '--state-dir', state_dir],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass  # the queue keeps the report; the next session start drains it


def link_lines(reports: list[dict[str, str]]) -> list[str]:
    return [f'- {r["skill"]} {r["skill_version"]}: {new_issue_url(r)}' for r in reports]


def main(argv: list[str]) -> int:
    if argv[:1] != ['drain'] or len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != '--state-dir'):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    state_dir = argv[2] if len(argv) == 3 else STATE_DIR
    if not playbook_core.load_prefs(state_dir).get('reports'):
        return 0
    print(json.dumps(drain(store_path(state_dir))))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
