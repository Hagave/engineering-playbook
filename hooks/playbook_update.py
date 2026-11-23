#!/usr/bin/env python3
"""Engineering Playbook: the installed version, the newest release, and the update.

A release reaches an installed plugin only when its owner pulls it (Claude Code
leaves auto-update of a third-party marketplace off), so nothing told anyone a
release existed. This module answers three questions:

- what version is installed (`installed_version`, from the plugin manifest);
- what is the newest published release (`refresh` asks GitHub at most once per
  `TTL_S`, keeps the answer in the state directory, and never raises: a network
  that is down costs a cached "unknown", not a slow or failed session);
- how to update (`apply_update` runs the `claude plugin` commands and reports
  the outcome in words the user reads).

The check is a plain GET of the public releases API: no identifier of the user,
the machine or the project is sent. `update_check off` in /playbook-config
switches the check off.

Run as a script, `playbook_update.py --refresh` refreshes the cache and exits:
the SessionStart hook starts it detached, so a session never waits for the
network.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from typing import NamedTuple

REPO = 'Hagave/engineering-playbook'
LATEST_URL = f'https://api.github.com/repos/{REPO}/releases/latest'
PLUGIN_ID = 'engineering-playbook@engineering-playbook'
MARKETPLACE = 'engineering-playbook'

TTL_S = 12 * 3600
RETRY_S = 3600
FETCH_TIMEOUT_S = 3
APPLY_TIMEOUT_S = 120
_MAX_BODY = 256 * 1024
_MESSAGE_SHOWN_CHARS = 400
CACHE_FILE = '.playbook-update.json'

# A stable release only (`vMAJOR.MINOR.PATCH`): a prerelease tag is never offered.
_TAG_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')

STATE_DIR = os.path.expanduser('~/.claude/hooks')
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Status(NamedTuple):
    installed: str | None
    latest: str | None
    available: bool


def parse_version(text: object) -> tuple[int, int, int] | None:
    match = _TAG_RE.match(text.strip()) if isinstance(text, str) else None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def installed_version(root: str | None = None) -> str | None:
    """The version the plugin manifest at `root` declares, None when unreadable."""
    for manifest in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        try:
            with open(os.path.join(root or PLUGIN_ROOT, manifest), encoding='utf-8') as handle:
                version = json.load(handle).get('version')
        except (OSError, ValueError, AttributeError):
            continue
        if parse_version(version):
            return str(version)
    return None


def _cache_path(state_dir: str) -> str:
    return os.path.join(state_dir, CACHE_FILE)


def read_cache(state_dir: str = STATE_DIR) -> dict:
    try:
        with open(_cache_path(state_dir), encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(state_dir: str, data: dict) -> None:
    """Atomic, best effort: a state directory that cannot be written loses a
    cached answer, never a session."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=state_dir, delete=False, suffix='.tmp') as tmp:
            json.dump(data, tmp)
        os.replace(tmp.name, _cache_path(state_dir))
    except OSError:
        pass


def fetch_latest(timeout: float = FETCH_TIMEOUT_S) -> str | None:
    """The newest stable release tag without its `v`, None when it cannot be
    read (offline, rate limited, an answer that is not a stable tag)."""
    request = urllib.request.Request(LATEST_URL, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'engineering-playbook-update-check',
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            tag = json.loads(response.read(_MAX_BODY)).get('tag_name')
    except (OSError, ValueError, AttributeError):
        return None
    return tag.lstrip('v') if parse_version(tag) else None


def is_due(cache: dict, now: float) -> bool:
    """Whether the cache is old enough to ask again: `TTL_S` after an answer,
    `RETRY_S` after a failure."""
    checked = cache.get('checked_at')
    if not isinstance(checked, (int, float)):
        return True
    window = TTL_S if cache.get('ok') else RETRY_S
    return now - checked >= window


def refresh(state_dir: str = STATE_DIR, now: float | None = None, force: bool = False,
            fetch: Callable[[], str | None] | None = None) -> dict:
    """Ask for the newest release when the cache is due (or `force`), keep the
    answer, and return the cache. A failed ask keeps the last known release."""
    moment = time.time() if now is None else now
    cache = read_cache(state_dir)
    if not force and not is_due(cache, moment):
        return cache
    latest = (fetch or fetch_latest)()
    fresh = {**cache, 'checked_at': moment, 'ok': bool(latest)}
    if latest:
        fresh['latest'] = latest
    _write_cache(state_dir, fresh)
    return fresh


def status(state_dir: str = STATE_DIR, root: str | None = None) -> Status:
    """What the cache knows, without any network: the panel and the session
    start read this."""
    cache = read_cache(state_dir)
    installed = installed_version(root)
    latest = cache.get('latest') if parse_version(cache.get('latest')) else None
    newer = bool(latest and installed and parse_version(latest) > parse_version(installed))
    return Status(installed, latest, newer)


def unannounced(state_dir: str = STATE_DIR, root: str | None = None) -> str | None:
    """The newer release the user has not been told about yet, None otherwise."""
    current = status(state_dir, root)
    if current.available and read_cache(state_dir).get('announced') != current.latest:
        return current.latest
    return None


def mark_announced(version: str, state_dir: str = STATE_DIR) -> None:
    _write_cache(state_dir, {**read_cache(state_dir), 'announced': version})


def spawn_refresh() -> None:
    """Refresh the cache in a process of its own, detached: a session never
    waits for the network."""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), '--refresh'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


class Outcome(NamedTuple):
    ok: bool
    text: str


# A plugin loaded from a folder (`--plugin-dir`) or an installer's clone is not
# one the marketplace tracks: the update commands answer "not found" for it.
_NOT_TRACKED_HINT = ('this copy was not installed from the marketplace, so there is nothing for '
                     '`claude plugin update` to move: run the installer again, or pull the '
                     'folder it was loaded from')


def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return 124, f'still running after {int(timeout)}s; stopped'
    except OSError as error:
        return 127, str(error)
    return done.returncode, (done.stdout.strip() or done.stderr.strip())


def apply_update(timeout: float = APPLY_TIMEOUT_S,
                 run: Callable[[list[str], float], tuple[int, str]] = _run) -> Outcome:
    """Refresh the marketplace, then update the plugin. The outcome's text is
    what the CLI said, cut; `ok` is False for any failure, with the reason."""
    code, said = run(['claude', 'plugin', 'marketplace', 'update', MARKETPLACE], timeout)
    if code != 0:
        return Outcome(False, _shorten(said) or f'marketplace update failed (exit {code})')
    code, said = run(['claude', 'plugin', 'update', PLUGIN_ID, '--json'], timeout)
    outcome = _json_line(said)
    if code != 0 or outcome.get('outcome') == 'failed':
        reason = str(outcome.get('message') or said or f'exit {code}')
        if outcome.get('failureCode') == 'not_found':
            reason = f'{reason}: {_NOT_TRACKED_HINT}'
        return Outcome(False, _shorten(reason))
    return Outcome(True, _shorten(str(outcome.get('message') or said or 'updated')))


def _json_line(text: str) -> dict:
    for line in reversed(text.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _shorten(text: str) -> str:
    return text if len(text) <= _MESSAGE_SHOWN_CHARS else text[:_MESSAGE_SHOWN_CHARS] + '…'


def main(argv: list[str]) -> int:
    if '--refresh' in argv:
        refresh()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
