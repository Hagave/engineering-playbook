#!/usr/bin/env python3
"""
Inspect the continuity ledger the hooks write (hooks/ledger.py), for the
data-quality review before anything reads it.

  python3 dev/observe/ledger.py stores                 every store on this machine
  python3 dev/observe/ledger.py sessions [--project D] this project's sessions
  python3 dev/observe/ledger.py show SESSION [--project D]
  python3 dev/observe/ledger.py query "SELECT ..." [--project D]

--project defaults to the current directory; any worktree of a repository
opens the same store. Read-only: nothing here writes to a store.
"""
import argparse
import datetime
import glob
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_HERE)), 'hooks'))
import ledger  # noqa: E402
import playbook_core  # noqa: E402
from cli import _table  # noqa: E402

PREVIEW = 100


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.execute('PRAGMA query_only = ON')
    return conn


def _project_store(project: str) -> str:
    return ledger.store_path(ledger.locate(project))


def _when(ts: float | None) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S') if ts else ''


def _preview(text: str | None) -> str:
    flat = ' '.join((text or '').split())
    return flat if len(flat) <= PREVIEW else flat[:PREVIEW] + '...'


def prompt_kind(text: str, command_name: str | None) -> str:
    """How the reader classifies a prompt. The payload says nothing about who
    sent it: a typed slash command is the only positive mark, and harness text
    is recognised by the playbook's one definition of it (NON_PROMPT_RE)."""
    if command_name:
        return 'slash command'
    if playbook_core.NON_PROMPT_RE.match(text):
        return 'harness'
    return 'unmarked'


def stores() -> str:
    rows = []
    for path in sorted(glob.glob(os.path.join(ledger.ledger_dir(), '*.db'))):
        conn = _open(path)
        meta = dict(conn.execute('SELECT key, value FROM meta'))
        counts = [conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in ('sessions', 'prompts', 'files', 'runs', 'turns')]
        conn.close()
        rows.append((os.path.basename(path), meta.get('schema_version'), meta.get('project'), *counts))
    return _table('Ledger stores', ('file', 'schema', 'project', 'sessions', 'prompts', 'files', 'runs',
                                    'turns'), rows)


def sessions(conn: sqlite3.Connection) -> str:
    rows = conn.execute('''
        SELECT s.session_id, s.worktree, s.started_at, s.source, s.inherited_from, s.ended_at, s.end_reason,
               (SELECT COUNT(*) FROM prompts p WHERE p.session_id = s.session_id),
               (SELECT COUNT(*) FROM files f WHERE f.session_id = s.session_id),
               (SELECT COUNT(*) FROM runs r WHERE r.session_id = s.session_id),
               (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.session_id)
        FROM sessions s ORDER BY COALESCE(s.started_at, s.ended_at)''').fetchall()
    return _table('Sessions', ('session', 'worktree', 'started', 'source', 'inherited from', 'ended',
                               'reason', 'prompts', 'files', 'runs', 'turns'),
                  [(r[0], r[1], _when(r[2]), r[3], r[4] or '', _when(r[5]), r[6], *r[7:]) for r in rows])


def show(conn: sqlite3.Connection, session: str) -> str:
    match = conn.execute('SELECT session_id FROM sessions WHERE session_id LIKE ? UNION '
                         'SELECT session_id FROM prompts WHERE session_id LIKE ?',
                         (session + '%', session + '%')).fetchall()
    if len(match) != 1:
        return f'{len(match)} sessions match {session!r}\n'
    session_id = match[0][0]
    events: list[tuple] = []
    for ts, prompt_id, text, chars, redacted, command in conn.execute(
            'SELECT ts, prompt_id, text, chars, redacted, command_name FROM prompts WHERE session_id = ?',
            (session_id,)):
        events.append((ts, 'prompt', prompt_kind(text, command), prompt_id[:8],
                       f'[{chars} chars{", redacted" if redacted else ""}] {_preview(text)}'))
    for ts, prompt_id, response in conn.execute(
            'SELECT ts, prompt_id, tool_response FROM answers WHERE session_id = ?', (session_id,)):
        events.append((ts, 'answer', '', (prompt_id or '')[:8], _preview(response)))
    for ts, prompt_id, agent, tool, action, path, sha, lines in conn.execute(
            '''SELECT ts, prompt_id, agent_type, tool, action, path, sha256,
                      CASE WHEN start_line IS NULL THEN '' ELSE start_line || '+' || num_lines || '/' || total_lines END
               FROM files WHERE session_id = ?''', (session_id,)):
        events.append((ts, f'file {action}', agent or '', (prompt_id or '')[:8],
                       f'{tool} {path} {lines} {(sha or "no hash")[:12]}'))
    for ts, prompt_id, agent, outcome, code, head, tree, error, command in conn.execute(
            '''SELECT ts, prompt_id, agent_type, outcome, exit_code, head, tree_sha256, state_error, command
               FROM runs WHERE session_id = ?''', (session_id,)):
        state = error or f'{(head or "")[:10]} {(tree or "")[:12]}'
        events.append((ts, 'run', agent or '', (prompt_id or '')[:8],
                       f'{outcome}{"" if code is None else f" (exit {code})"} at {state}: {_preview(command)}'))
    for ts, prompt_id, message, tasks in conn.execute(
            'SELECT ts, prompt_id, last_message, background_tasks FROM turns WHERE session_id = ?',
            (session_id,)):
        open_tasks = '' if tasks in (None, '[]') else f' [background: {_preview(tasks)}]'
        events.append((ts, 'turn', '', prompt_id[:8], _preview(message) + open_tasks))
    events.sort(key=lambda event: event[0])
    head = conn.execute('SELECT source, inherited_from, end_reason FROM sessions WHERE session_id = ?',
                        (session_id,)).fetchone() or (None, None, None)
    handoff = conn.execute('SELECT claimed_by, pid_match FROM handoffs WHERE from_session = ?',
                           (session_id,)).fetchone()
    title = (f'Session {session_id}: source {head[0]}, inherited from {head[1] or "-"}, ended {head[2] or "-"}'
             + (f', handoff claimed by {handoff[0] or "nobody"} (pid match {handoff[1]})' if handoff else ''))
    return _table(title, ('time', 'record', 'kind', 'prompt', 'what'),
                  [(_when(e[0]),) + e[1:] for e in events])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('stores', 'sessions', 'show', 'query'))
    parser.add_argument('arg', nargs='?', help='the session id (or a prefix) for show; the SELECT for query')
    parser.add_argument('--project', default=os.getcwd(), help='a directory of the project (default: cwd)')
    args = parser.parse_args(argv)
    if args.action == 'stores':
        print(stores())
        return 0
    path = _project_store(args.project)
    if not os.path.exists(path):
        print(f'no ledger for {args.project} ({path})', file=sys.stderr)
        return 1
    conn = _open(path)
    if args.action == 'sessions':
        print(sessions(conn))
    elif not args.arg:
        parser.error(f'{args.action} needs an argument')
    elif args.action == 'show':
        print(show(conn, args.arg))
    else:
        try:
            cursor = conn.execute(args.arg)
        except sqlite3.Error as error:
            print(error, file=sys.stderr)
            return 1
        header = tuple(column[0] for column in cursor.description or ())
        print(_table('', header, [tuple(row) for row in cursor]) if header else '')
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
