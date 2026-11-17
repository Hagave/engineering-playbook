#!/usr/bin/env python3
"""
Engineering Playbook — Output Filter
PostToolUse hook, Bash only: shortens the output of read-only commands before
the model reads it. The command is never rewritten: it runs exactly as
written, and only what comes back is reshaped.

What the filter can and cannot change (measured on Claude Code 2.1.325):

  - `updatedToolOutput` replaces the tool output the model sees. It must keep
    the tool's shape (stdout, stderr, interrupted, ...) or Claude Code
    discards it and shows the original.
  - Bash caps stdout at 30,000 characters before any hook runs. Above that,
    Claude Code saves the whole output to a file (`persistedOutputPath`) and
    shows the model a short preview; a rewrite of such an output is ignored.
    The filter only has work to do below that cap.

Contract:

  - Never blocks, never fails a call: any error passes the original through.
  - Only stdout changes. stderr, the exit status and every other field are
    passed through as they came.
  - A filtered output is never larger than the original and ends with a
    `[playbook:filter]` line. Re-running the command with PLAYBOOK_RAW=1 in
    front of it returns the unfiltered output; nothing is saved to disk.
  - Off while the playbook is suspended. Output that another tool already
    reshaped, or a command another hook rewrote to its own program, matches
    no rule's strict format and passes through unchanged.
  - Each filtered output adds one line to a per-session ledger;
    `output_filter.py --report` prints the totals.

Rules, in order of the saving measured on real usage history: grep/rg/git grep
grouped by file, `ls -l` compacted, plain `git log` compacted. `cat` is not
filtered: its measured saving was outputs Claude Code had already cut, and a
source file must reach the model as written.
"""

import glob
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import playbook_core  # noqa: E402

STATE_DIR = os.path.expanduser('~/.claude/hooks')

RAW_ENV = 'PLAYBOOK_RAW'
# Below this, a reshaped output saves too little to be worth reading a new format.
MIN_CHARS = 1000
# A filtered output must be at least this much shorter, footer included.
MIN_GAIN = 0.10

# Redirections that only route stderr: they do not change what stdout holds.
_STDERR_REDIRECT_RE = re.compile(r'\s2>(?:&1|/dev/null)(?=\s|$)')
# Commands whose text the hook cannot see in full: substitutions run other code.
_SUBSTITUTION_RE = re.compile(r'\$\(|`')
# Commands allowed after the producer in a pipeline: they cut lines, not shape.
_LINE_CUTTERS = frozenset({'head', 'tail'})

FOOTER = ('[playbook:filter] {summary}; original {original:,} chars. For the unfiltered '
          'output, run the same command again with PLAYBOOK_RAW=1 in front of it.')


# ─── Command analysis ─────────────────────────────────────────────────────────

def output_command(command: str, cwd: str) -> tuple[list[str], str] | None:
    """The command whose output fills stdout, and the directory it ran in.

    Accepted shape: `cd <dir>` steps, then one producer, then only `head` or
    `tail`. Anything else (two producers, a pipe into a formatter, a
    substitution, a raw request) returns None and the output passes through."""
    if not command or RAW_ENV + '=' in command or _SUBSTITUTION_RE.search(command):
        return None
    segments = playbook_core.shell_segments(_STDERR_REDIRECT_RE.sub(' ', command))
    if not segments:
        return None
    workdir = cwd
    producer: list[str] | None = None
    for segment in segments:
        words = playbook_core.command_words(segment)
        if not words:
            return None
        base = _program(words)
        if producer is None and base == 'cd':
            if len(words) != 2:
                return None
            workdir = os.path.normpath(os.path.join(workdir, os.path.expanduser(words[1])))
            continue
        if producer is None:
            producer = words
            continue
        if base not in _LINE_CUTTERS:
            return None
    if producer is None or any(_redirects_stdout(w) for w in producer):
        return None
    return producer, workdir


def _program(words: list[str]) -> str:
    """The command word without its directory: `grep` for `/usr/bin/grep`."""
    return words[0].rsplit('/', 1)[-1]


def _redirects_stdout(word: str) -> bool:
    return word in ('>', '>>', '1>', '&>') or word.startswith(('>', '1>', '&>'))


# ─── grep / rg / git grep ─────────────────────────────────────────────────────

GREP_WORDS = frozenset({'grep', 'egrep', 'fgrep', 'rg'})
GREP_MAX_PER_FILE = 20
GREP_MAX_LINE = 240
# Short options that print context lines (`path-12-text`, `--` separators),
# whose path cannot be split reliably from the text: such output passes through.
_CONTEXT_SHORT_RE = re.compile(r'^-[a-zA-Z]*[ABC]')
_CONTEXT_LONG = ('--context', '--after-context', '--before-context')
_MATCH_LINE_RE = re.compile(r'^(?P<path>[^:\0]+):(?P<rest>.*)$')


def _is_grep(words: list[str]) -> bool:
    base = _program(words)
    return base in GREP_WORDS or (base == 'git' and len(words) > 1 and words[1] == 'grep')


def _prints_context(words: list[str]) -> bool:
    return any(_CONTEXT_SHORT_RE.match(w) or w.startswith(_CONTEXT_LONG) for w in words[1:])


def _clip(text: str) -> str:
    if len(text) <= GREP_MAX_LINE:
        return text
    return f'{text[:GREP_MAX_LINE]} ...[+{len(text) - GREP_MAX_LINE} chars]'


def reduce_grep(words: list[str], stdout: str, workdir: str) -> tuple[str, str] | None:
    """`path:line:text` lines grouped under their path, at most
    GREP_MAX_PER_FILE matches per file, long lines clipped.

    Output whose lines do not all read as `path:...` with an existing path
    (a single-file grep, `-l`, `--heading`, `--json`, merged error lines)
    passes through: a wrong split would show the model a file that is not there."""
    if _prints_context(words):
        return None
    groups: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        if not line:
            continue
        match = _MATCH_LINE_RE.match(line)
        if not match:
            return None
        groups.setdefault(match.group('path'), []).append(match.group('rest'))
    if not groups or not all(os.path.exists(os.path.join(workdir, p)) for p in groups):
        return None
    lines: list[str] = []
    omitted = 0
    for path, rests in groups.items():
        lines.append(f'{path} ({len(rests)})')
        lines.extend(f'  {_clip(rest)}' for rest in rests[:GREP_MAX_PER_FILE])
        if len(rests) > GREP_MAX_PER_FILE:
            extra = len(rests) - GREP_MAX_PER_FILE
            omitted += extra
            lines.append(f'  ... {extra} more in this file')
    matches = sum(len(r) for r in groups.values())
    summary = f'grep output grouped by file ({len(groups)} files, {matches} matches'
    summary += f', {omitted} over the per-file cap omitted)' if omitted else ')'
    return '\n'.join(lines), summary


# ─── ls -l ────────────────────────────────────────────────────────────────────

# Dates as ls prints them: `Nov 16 15:02`, `Nov 16  2025`, `16 nov 15:02`
# (day-first locales), and the iso time styles.
_LS_DATE = (r'(?:\S+\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4})'
            r'|\d{1,2}\s+\S+\s+(?:\d{1,2}:\d{2}|\d{4})'
            r'|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s+[-+]\d{4})?)')
_LS_ENTRY_RE = re.compile(
    r'^(?P<mode>[-dlcbpsD?][-rwxsStTl?]{9}[.+@]?)\s+\d+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+'
    r'(?P<size>\d+,\s*\d+|[\d.,]+[KMGTPEZY]?i?B?)\s+(?P<date>' + _LS_DATE + r')\s(?P<name>.+)$')
_LS_TOTAL_RE = re.compile(r'^total\s+\S+$')
# Options that drop or add a column: the entry pattern above would misread them.
_LS_SHAPE_SHORT = frozenset('goGsi')


def _is_long_ls(words: list[str]) -> bool:
    if _program(words) != 'ls':
        return False
    short = ''.join(w[1:] for w in words[1:] if w.startswith('-') and not w.startswith('--'))
    long_opts = [w for w in words[1:] if w.startswith('--')]
    if _LS_SHAPE_SHORT & set(short) or any(o.startswith(('--no-group', '--size', '--inode',
                                                          '--author', '--format=')) for o in long_opts):
        return False
    return 'l' in short


def reduce_ls(words: list[str], stdout: str, workdir: str) -> tuple[str, str] | None:
    """Long listing without the link count, the `total` lines and the `.`/`..`
    entries; owner and group become one note when every entry shares them.
    Permissions, size, date and name stay as ls printed them."""
    lines: list[str | tuple[str, str]] = []
    owners: set[str] = set()
    for line in stdout.splitlines():
        if not line or _LS_TOTAL_RE.match(line):
            if not line:
                lines.append('')
            continue
        entry = _LS_ENTRY_RE.match(line)
        if entry:
            if entry.group('name') in ('.', '..'):
                continue
            owner = f'{entry.group("owner")}:{entry.group("group")}'
            owners.add(owner)
            date = ' '.join(entry.group('date').split())
            lines.append((owner, f'{entry.group("mode")} {entry.group("size")} {date} {entry.group("name")}'))
        elif line.endswith(':'):
            lines.append(line)  # directory header (-R, or several directories)
        else:
            return None
    if not owners:
        return None
    shared = len(owners) == 1
    body = [line if isinstance(line, str) else (line[1] if shared else f'{line[0]} {line[1]}')
            for line in lines]
    summary = 'ls -l compacted (link count, total, . and .. dropped'
    summary += f'; owner:group {owners.pop()} on every entry)' if shared else ')'
    return '\n'.join(body).strip('\n'), summary


# ─── git log ──────────────────────────────────────────────────────────────────

# Options that keep git log's default (medium) format; anything else changes
# the shape (--oneline, --stat, -p, --graph, --format...) and passes through.
_GIT_LOG_OPTS_RE = re.compile(
    r'^(?:-\d+|-n\d*|--max-count=\d+|--skip=\d+|--(?:since|after|until|before|author|committer'
    r'|grep)=.*|--(?:no-merges|merges|all|first-parent|reverse|decorate|no-decorate'
    r'|branches|tags|remotes)|--)$')
_GIT_LOG_VALUE_OPTS = frozenset({'-n', '--max-count', '--skip', '--since', '--after', '--until',
                                 '--before', '--author', '--committer', '--grep'})
_GIT_COMMIT_RE = re.compile(r'^commit ([0-9a-f]{7,64})(.*)$')
_GIT_HEADER_RE = re.compile(r'^([A-Z][\w-]*):\s+(.*)$')
GIT_LOG_MAX_BODY = 6


def _is_plain_git_log(words: list[str]) -> bool:
    if _program(words) != 'git' or len(words) < 2 or words[1] != 'log':
        return False
    skip_value = False
    for word in words[2:]:
        if skip_value:
            skip_value = False
            continue
        if word in _GIT_LOG_VALUE_OPTS:
            skip_value = True
            continue
        if word.startswith('-') and not _GIT_LOG_OPTS_RE.match(word):
            return False
    return True


def _short_date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), '%a %b %d %H:%M:%S %Y %z').strftime('%Y-%m-%d')
    except ValueError:
        return value.strip()


def reduce_git_log(words: list[str], stdout: str, workdir: str) -> tuple[str, str] | None:
    """One line per commit (`sha date author: subject`, decorations and a merge
    mark kept), then up to GIT_LOG_MAX_BODY non-blank body lines."""
    commits: list[dict] = []
    for line in stdout.splitlines():
        start = _GIT_COMMIT_RE.match(line)
        if start:
            commits.append({'sha': start.group(1)[:10], 'decor': start.group(2).strip(),
                            'fields': {}, 'message': []})
            continue
        if not commits:
            return None
        commit = commits[-1]
        if line.startswith('    ') or not line.strip():
            if line.strip():
                commit['message'].append(line[4:])
            continue
        header = _GIT_HEADER_RE.match(line)
        if not header or commit['message']:
            return None
        commit['fields'][header.group(1)] = header.group(2)
    if not commits:
        return None
    out: list[str] = []
    cut = 0
    for commit in commits:
        fields = commit['fields']
        author = re.sub(r'\s*<[^>]*>$', '', fields.get('Author', '?'))
        message = commit['message'] or ['']
        head = f'{commit["sha"]} {_short_date(fields.get("Date", ""))} {author}'
        if commit['decor']:
            head += f' {commit["decor"]}'
        if 'Merge' in fields:
            head += ' [merge]'
        out.append(f'{head}: {message[0]}')
        body = message[1:]
        out.extend(f'    {line}' for line in body[:GIT_LOG_MAX_BODY])
        if len(body) > GIT_LOG_MAX_BODY:
            cut += 1
            out.append(f'    ... {len(body) - GIT_LOG_MAX_BODY} more lines')
    summary = f'git log compacted ({len(commits)} commits, emails and blank lines dropped'
    summary += f', {cut} long bodies cut)' if cut else ')'
    return '\n'.join(out), summary


# ─── Dispatch ─────────────────────────────────────────────────────────────────

REDUCERS = (
    (_is_grep, reduce_grep),
    (_is_long_ls, reduce_ls),
    (_is_plain_git_log, reduce_git_log),
)


def filter_output(command: str, response: object, cwd: str) -> str | None:
    """The filtered stdout, or None to leave the output as it came."""
    if not isinstance(response, dict):
        return None
    if response.get('persistedOutputPath') or response.get('interrupted') or response.get('isImage'):
        return None
    stdout = response.get('stdout')
    if not isinstance(stdout, str) or len(stdout) < MIN_CHARS:
        return None
    target = output_command(command, cwd)
    if target is None:
        return None
    words, workdir = target
    for applies, reduce in REDUCERS:
        if not applies(words):
            continue
        reduced = reduce(words, stdout, workdir)
        if reduced is None:
            return None
        body, summary = reduced
        filtered = f'{body}\n{FOOTER.format(summary=summary, original=len(stdout))}'
        return filtered if len(filtered) <= len(stdout) * (1 - MIN_GAIN) else None
    return None


# ─── Environment ──────────────────────────────────────────────────────────────

def _suspended(input_data: dict) -> bool:
    state = playbook_core.load_state(playbook_core.state_path(
        STATE_DIR, str(input_data.get('session_id') or ''),
        agent_id=str(input_data.get('agent_id') or '')))
    return bool(state.get('suspended'))


# ─── Ledger ───────────────────────────────────────────────────────────────────
#
# One line per filtered output, `<chars before> <chars after>`, in a file per
# session. "Before" is the stdout the model would have read, already under
# Claude Code's own cap, so the saving is never counted against raw output
# the model would not have seen anyway. Files of past sessions older than
# LEDGER_MAX_AGE are removed when a new one starts.

LEDGER_GLOB = '.filter-*.log'
LEDGER_MAX_AGE = 2 * 24 * 3600
# Rough English-text ratio, for the report only; the ledger stores characters.
CHARS_PER_TOKEN = 4


def _ledger_path(session_id: str) -> str:
    key = re.sub(r'[^\w-]', '_', session_id) or 'nosession'
    return os.path.join(STATE_DIR, f'.filter-{key}.log')


def record(session_id: str, before: int, after: int) -> None:
    path = _ledger_path(session_id)
    if not os.path.exists(path):
        cutoff = time.time() - LEDGER_MAX_AGE
        for old in glob.glob(os.path.join(STATE_DIR, LEDGER_GLOB)):
            try:
                if os.path.getmtime(old) < cutoff:
                    os.remove(old)
            except OSError:
                continue
    os.makedirs(STATE_DIR, exist_ok=True)
    # One short write in append mode: concurrent hooks of the same session
    # never interleave inside a line.
    with open(path, 'a', encoding='ascii') as fh:
        fh.write(f'{before} {after}\n')


def report() -> str:
    """The latest session's totals, for /playbook-config (Status)."""
    ledgers = glob.glob(os.path.join(STATE_DIR, LEDGER_GLOB))
    if not ledgers:
        return 'Output filter: on, nothing filtered yet'
    count = before = after = 0
    with open(max(ledgers, key=os.path.getmtime), encoding='ascii') as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                count += 1
                before += int(parts[0])
                after += int(parts[1])
    if not before:
        return 'Output filter: on, nothing filtered yet'
    return (f'Output filter: {count} outputs filtered, {before:,} -> {after:,} chars '
            f'(-{100 * (before - after) / before:.0f} %, about {(before - after) // CHARS_PER_TOKEN:,} tokens)')


def main() -> None:
    if sys.argv[1:] == ['--report']:
        print(report())
        return
    try:
        input_data = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(input_data, dict) or input_data.get('tool_name') != 'Bash':
        return
    try:
        cwd = str(input_data.get('cwd') or os.getcwd())
        tool_input = input_data.get('tool_input')
        command = str(tool_input.get('command') or '') if isinstance(tool_input, dict) else ''
        response = input_data.get('tool_response')
        stdout = filter_output(command, response, cwd)
        if stdout is None or _suspended(input_data):
            return
    except Exception:  # noqa: BLE001 — a filter must never cost the user their output
        return
    updated = dict(response)
    original = len(updated.get('stdout') or '')
    updated['stdout'] = stdout
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                             'updatedToolOutput': updated}}))
    try:
        record(str(input_data.get('session_id') or ''), original, len(stdout))
    except OSError:
        pass  # the output is already delivered; a missed ledger line costs nothing


if __name__ == '__main__':
    main()
