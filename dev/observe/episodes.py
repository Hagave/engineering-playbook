"""
Rework episodes, derived from the ingested tool calls, with the facts that
were true when each one happened.

Two kinds:

  fix_loop   a validation command failed, the model edited, and the same
             validation ran again (until it passed or the request ended)
  re_edit    the model rewrote lines it had written earlier in the same
             request, with no failing validation in between

For each episode the store records what can be observed, not an opinion about
the cause:

  cause        where the fix landed
                 own_lines               on lines the model wrote this session
                 own_file_other_lines    in a file it had edited, elsewhere
                 untouched_file_read     in a file it had only read
                 untouched_file_unread   in a file it had never looked at
                 new_file                in a file the fix created
                 rewrite_without_failure (re_edit only)
  target_read  what the model had seen of that file before it first edited it
               (full / partial / none / created)
  error_class  the kind of failure the validation output showed

Read together they separate "wrote it wrong with the file in front of it" from
"never looked at the file the fix needed".
"""
import bisect
import collections
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field

import shell

# Prompt sources that are not a person asking for something: a request runs
# from one human prompt to the next, and these arrive in the middle of one.
MACHINE_PROMPT_SOURCES = frozenset({'agent-brief', 'task-notification', 'system', 'hook',
                                    'local-command'})

# Scratch files are rewritten by design; they are not the project's code.
SCRATCH_MARKERS = ('/tmp/', '/.claude/', '/.work/', '/scratchpad/')
PROSE_SUFFIXES = ('.md', '.txt', '.rst')

# Most specific first: an episode with fixes of several scopes is filed under
# the one that says most about what was missing.
_SCOPE_PRIORITY = ('untouched_file_unread', 'untouched_file_read', 'own_file_other_lines',
                   'own_lines', 'new_file')


# What came between writing some lines and rewriting them, strongest signal
# first: a rewrite after a failed check and a read is filed under the check.
TRIGGERS = ('check_failed', 'command_failed', 'check_passed', 'command', 'read', 'other')
NO_TRIGGER = 'nothing'   # the rewrite followed the write with no call in between


def _verdict(call: sqlite3.Row) -> str | None:
    """'failed' / 'passed' for a call that ran to its result, else None."""
    if call['denial'] or call['ts_result'] is None:
        return None  # never ran, or its result is not in the store yet
    by_exit = bool(call['is_error']) and call['exit_code'] is not None
    if by_exit or shell.output_failed(call['result_tail']):
        return 'failed'
    return None if call['is_error'] else 'passed'


def _scratch(path: str) -> bool:
    return any(marker in path for marker in SCRATCH_MARKERS)


@dataclass
class _Written:
    start: int
    end: int       # exclusive
    request: int


@dataclass
class _FixLoop:
    key: str
    ts_start: float
    error_head: str
    attempts: int = 1
    edits_before: int = 0
    seen_by: str = 'exit_code'
    scopes: list[tuple[str, str]] = field(default_factory=list)   # (scope, path)


class _SessionWalk:
    """One session (or one subagent) replayed in order."""

    def __init__(self, session_id: str, agent_id: str) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.seen: dict[str, str] = {}                 # path -> 'full' | 'partial'
        self.written: dict[str, list[_Written]] = collections.defaultdict(list)
        self.first_edit_read: dict[str, str] = {}
        self.hunks: collections.Counter[str] = collections.Counter()
        self.rewritten: collections.Counter[str] = collections.Counter()
        self.events: list[str] = []                    # non-edit calls, in order
        self.events_at_edit: dict[str, int] = {}       # path -> len(events) at its last edit
        self.rewrite_rows: dict[tuple[str, str, int], str] = {}
        self.loop: _FixLoop | None = None
        self.loop_last_ts = 0.0
        self.rewrites: dict[str, list[float]] = collections.defaultdict(list)
        self.request = -1
        self.edit_calls_in_request = 0
        self.episodes: list[tuple] = []

    # ── per event ────────────────────────────────────────────────────────────

    def note_reads(self, rows: list[sqlite3.Row]) -> None:
        for row in rows:
            if row['full'] or self.seen.get(row['path']) == 'full':
                self.seen[row['path']] = 'full'
            else:
                self.seen[row['path']] = 'partial'

    def note_event(self, category: str) -> None:
        self.events.append(category)

    def note_edits(self, ts: float, rows: list[sqlite3.Row]) -> None:
        """One edit call. Its hunks are all in the line numbers the file had
        before the call, so every hunk is classified against what was written
        before it, and only then are the earlier ranges moved."""
        self.edit_calls_in_request += 1
        by_path: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
        for row in rows:
            if not _scratch(row['path']):
                by_path[row['path']].append(row)
        for path, hunks in by_path.items():
            earlier = self.written[path]
            created = (not earlier and path not in self.seen
                       and all(h['old_start'] == 0 and h['old_lines'] == 0 for h in hunks))
            self.first_edit_read.setdefault(path, 'created' if created else self.seen.get(path, 'none'))
            spans = [(h['old_start'], h['old_start'] + max(h['old_lines'], 1)) for h in hunks]
            for hunk, (old_start, old_end) in zip(hunks, spans):
                overlapping = [w for w in earlier if w.start < old_end and old_start < w.end]
                if created:
                    scope = 'new_file'
                elif overlapping:
                    scope = 'own_lines'
                elif earlier:
                    scope = 'own_file_other_lines'
                elif path in self.seen:
                    scope = 'untouched_file_read'
                else:
                    scope = 'untouched_file_unread'
                self.hunks[path] += 1
                # Rewriting lines written earlier in this same request. Prose
                # (notes, docs) is rewritten by design and is not counted.
                rewrite = (any(w.request == self.request for w in overlapping)
                           and not path.endswith(PROSE_SUFFIXES))
                if rewrite:
                    self.rewritten[path] += 1
                    between = set(self.events[self.events_at_edit.get(path, 0):])
                    self.rewrite_rows[(hunk['tool_use_id'], path, hunk['hunk'])] = next(
                        (name for name in TRIGGERS if name in between), NO_TRIGGER)
                if self.loop is not None:
                    self.loop.scopes.append((scope, path))
                    self.loop_last_ts = ts
                elif rewrite:
                    self.rewrites[path].append(ts)
            # Lines below a hunk move by what it added or removed.
            for written in earlier:
                shift = sum(h['new_lines'] - h['old_lines']
                            for h, (_, old_end) in zip(hunks, spans) if old_end <= written.start)
                written.start += shift
                written.end += shift
            earlier.extend(_Written(h['new_start'], h['new_start'] + max(h['new_lines'], 1), self.request)
                           for h in hunks)
            if created:
                self.seen[path] = 'full'  # the model wrote every line of it
            self.events_at_edit[path] = len(self.events)

    def note_validation(self, call: sqlite3.Row, key: str) -> None:
        verdict = _verdict(call)
        if verdict is None:
            return
        failed, passed = verdict == 'failed', verdict == 'passed'
        by_exit = bool(call['is_error']) and call['exit_code'] is not None
        ts = call['ts_result'] or call['ts_call']
        if self.loop is not None and self.loop.key == key:
            if not self.loop.scopes:
                if passed:
                    self.loop = None  # passed on a plain re-run: nothing was fixed
                return
            self.loop_last_ts = ts
            if passed:
                self._close_loop(resolved=True)
            elif failed:
                self.loop.attempts += 1
            return
        if failed:
            self._close_loop(resolved=False)
            self.loop = _FixLoop(key, call['ts_call'], call['error_head'] or call['result_tail'] or '',
                                 edits_before=self.edit_calls_in_request,
                                 seen_by='exit_code' if by_exit else 'output')
            self.loop_last_ts = ts

    def new_request(self, index: int) -> None:
        self._close_loop(resolved=False)
        self._flush_rewrites()
        self.request = index
        self.edit_calls_in_request = 0

    def finish(self) -> None:
        self._close_loop(resolved=False)
        self._flush_rewrites()

    # ── episodes ─────────────────────────────────────────────────────────────

    def _close_loop(self, resolved: bool) -> None:
        loop, self.loop = self.loop, None
        if loop is None or not loop.scopes:
            return  # a failure nobody edited after is not rework
        by_scope = {scope: path for scope, path in reversed(loop.scopes)}
        cause = next(scope for scope in _SCOPE_PRIORITY if scope in by_scope)
        target = by_scope[cause]
        files = sorted({path for _, path in loop.scopes})
        target_read = self.first_edit_read.get(target, 'none')
        details = {
            'files': files,
            'scopes': dict(collections.Counter(scope for scope, _ in loop.scopes)),
            'edits_before_failure': loop.edits_before,
            'failure_seen_by': loop.seen_by,
            'only_tests': all(shell.is_test_path(path) for path in files),
        }
        self.episodes.append((
            self.session_id, self.agent_id, 'fix_loop', loop.ts_start, self.loop_last_ts,
            loop.key, loop.attempts, int(resolved), shell.error_class(loop.error_head, loop.key),
            cause, target_read, len(loop.scopes), json.dumps(details, sort_keys=True)))

    def _flush_rewrites(self) -> None:
        for path, stamps in self.rewrites.items():
            self.episodes.append((
                self.session_id, self.agent_id, 're_edit', stamps[0], stamps[-1], None, 0, 1, None,
                'rewrite_without_failure', self.first_edit_read.get(path, 'none'), len(stamps),
                json.dumps({'files': [path]}, sort_keys=True)))
        self.rewrites.clear()


def _grouped(conn: sqlite3.Connection, table: str, session_id: str, agent_id: str) -> dict[str, list]:
    rows = conn.execute(
        f'SELECT t.* FROM {table} t JOIN tool_calls c USING (tool_use_id) '
        'WHERE c.session_id = ? AND c.agent_id = ?', (session_id, agent_id)).fetchall()
    grouped: dict[str, list] = collections.defaultdict(list)
    for row in rows:
        grouped[row['tool_use_id']].append(row)
    return grouped


def derive(conn: sqlite3.Connection) -> int:
    """Rebuild the episodes table from scratch. Returns how many it holds."""
    conn.execute('DELETE FROM episodes')
    conn.execute('DELETE FROM edited_files')
    conn.execute('DELETE FROM rewrites')
    # A project whose check has a name of its own (a wrapper script) is not
    # recognised by its command word: name it here, matched on the command text.
    extra = os.environ.get('PLAYBOOK_OBSERVE_VALIDATION_RE')
    custom = re.compile(extra) if extra else None
    sessions = conn.execute('SELECT session_id, agent_id FROM sessions').fetchall()
    for session in sessions:
        session_id, agent_id = session['session_id'], session['agent_id']
        boundaries = [row['ts'] for row in conn.execute(
            'SELECT ts, source FROM prompts WHERE session_id = ? AND agent_id = ? ORDER BY ts',
            (session_id, agent_id)) if row['source'] not in MACHINE_PROMPT_SOURCES]
        reads = _grouped(conn, 'reads', session_id, agent_id)
        edits = _grouped(conn, 'edits', session_id, agent_id)
        walk = _SessionWalk(session_id, agent_id)
        calls = conn.execute(
            'SELECT * FROM tool_calls WHERE session_id = ? AND agent_id = ? AND ts_call IS NOT NULL '
            'ORDER BY ts_call', (session_id, agent_id)).fetchall()
        for call in calls:
            index = bisect.bisect_right(boundaries, call['ts_call']) - 1
            if index != walk.request:
                walk.new_request(index)
            call_id = call['tool_use_id']
            # A failed or denied call showed the model nothing and changed nothing.
            if call_id in reads and not call['denial'] and not (call['kind'] == 'read' and call['is_error']):
                walk.note_reads(reads[call_id])
            if call_id in edits:
                walk.note_edits(call['ts_call'], sorted(edits[call_id], key=lambda row: row['hunk']))
            key = call['validation']
            if not key and custom and call['kind'] == 'bash' and custom.search(call['command'] or ''):
                key = 'custom'
            if key:
                walk.note_validation(call, key)
            if call_id not in edits:
                if key and _verdict(call):
                    walk.note_event(f'check_{_verdict(call)}')
                elif call['kind'] == 'read' or call_id in reads:
                    walk.note_event('read')
                elif call['kind'] == 'bash':
                    walk.note_event('command_failed' if call['is_error'] else 'command')
                else:
                    walk.note_event('other')
        walk.finish()
        conn.executemany('INSERT INTO rewrites (tool_use_id, path, hunk, trigger) VALUES (?, ?, ?, ?)',
                         [(*key, trigger) for key, trigger in walk.rewrite_rows.items()])
        conn.executemany(
            'INSERT INTO edited_files (session_id, agent_id, path, target_read, hunks, rewritten_hunks) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            [(session_id, agent_id, path, walk.first_edit_read[path], count, walk.rewritten[path])
             for path, count in walk.hunks.items()])
        conn.executemany(
            'INSERT INTO episodes (session_id, agent_id, kind, ts_start, ts_end, validation, attempts, '
            'resolved, error_class, cause, target_read, fix_edits, details) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', walk.episodes)
    conn.commit()
    return conn.execute('SELECT COUNT(*) FROM episodes').fetchone()[0]
