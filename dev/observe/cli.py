#!/usr/bin/env python3
"""
Playbook observability, for developing the playbook itself.

  python3 dev/observe/cli.py ingest [--days N]   transcripts -> the local store
  python3 dev/observe/cli.py derive              rebuild the rework episodes
  python3 dev/observe/cli.py report [--by-project]
  python3 dev/observe/cli.py all [--days N]      the three, in order
  python3 dev/observe/cli.py query "SELECT ..."  any read-only question

The store is one SQLite file outside every repository (see db.py).
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import episodes  # noqa: E402
import ingest  # noqa: E402


def _table(title: str, header: tuple[str, ...], rows: list[tuple]) -> str:
    cells = [header] + [tuple('' if value is None else str(value) for value in row) for row in rows]
    widths = [max(len(row[col]) for row in cells) for col in range(len(header))]
    lines = [title, '  '.join(name.ljust(width) for name, width in zip(header, widths))]
    lines += ['  '.join(value.ljust(width) for value, width in zip(row, widths)) for row in cells[1:]]
    return '\n'.join(lines) + '\n'


def _query(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql)]


def report(conn: sqlite3.Connection, by_project: bool = False) -> str:
    project = 's.project, ' if by_project else ''
    project_header = ('project',) if by_project else ()
    join = 'JOIN sessions s USING (session_id, agent_id)'
    parts = [_table('Store', ('table', 'rows'), [
        (name, conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0])
        for name in ('sessions', 'prompts', 'api_calls', 'tool_calls', 'reads', 'edits',
                     'hook_events', 'episodes')])]

    unknown = _query(conn, 'SELECT record_type, count FROM unknown_records ORDER BY count DESC')
    parts.append(_table('Transcript records not understood (0 rows means none)',
                        ('record', 'count'), unknown))

    parts.append(_table(
        'Rework episodes by where the fix landed',
        project_header + ('kind', 'cause', 'episodes', 'share %', 'unresolved', 'hunks'),
        _query(conn, f'''
            SELECT {project} e.kind, e.cause, COUNT(*),
                   ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY {project} e.kind), 1),
                   SUM(1 - e.resolved), SUM(e.fix_edits)
            FROM episodes e {join} GROUP BY {project} e.kind, e.cause
            ORDER BY {project} e.kind, COUNT(*) DESC''')))

    parts.append(_table(
        'Fix loops: what the model had seen of the file before first editing it',
        project_header + ('cause', 'target_read', 'episodes'),
        _query(conn, f'''
            SELECT {project} e.cause, e.target_read, COUNT(*)
            FROM episodes e {join} WHERE e.kind = 'fix_loop'
            GROUP BY {project} e.cause, e.target_read ORDER BY {project} e.cause, COUNT(*) DESC''')))

    parts.append(_table(
        'Files edited, by what the model had seen before the first edit',
        project_header + ('target_read', 'files', 'with rewrites', 'rewrite %', 'hunks', 'rewritten hunks'),
        _query(conn, f'''
            SELECT {project} f.target_read, COUNT(*), SUM(f.rewritten_hunks > 0),
                   ROUND(100.0 * SUM(f.rewritten_hunks > 0) / COUNT(*), 1),
                   SUM(f.hunks), SUM(f.rewritten_hunks)
            FROM edited_files f JOIN sessions s USING (session_id, agent_id)
            GROUP BY {project} f.target_read ORDER BY {project} COUNT(*) DESC''')))

    parts.append(_table(
        'Rewrites of own lines, by what happened between the write and the rewrite',
        project_header + ('trigger', 'rewrites', 'share %'),
        _query(conn, f'''
            SELECT {project} r.trigger, COUNT(*),
                   ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER ({'PARTITION BY s.project' if by_project else ''}), 1)
            FROM rewrites r JOIN tool_calls c USING (tool_use_id)
                 JOIN sessions s ON s.session_id = c.session_id AND s.agent_id = c.agent_id
            GROUP BY {project} r.trigger ORDER BY {project} COUNT(*) DESC''')))

    parts.append(_table(
        'Fix loops by kind of failure',
        project_header + ('error_class', 'episodes', 'mean attempts'),
        _query(conn, f'''
            SELECT {project} e.error_class, COUNT(*), ROUND(AVG(e.attempts), 2)
            FROM episodes e {join} WHERE e.kind = 'fix_loop'
            GROUP BY {project} e.error_class ORDER BY {project} COUNT(*) DESC''')))

    parts.append(_table(
        'Hook outcomes by playbook gate',
        ('gate', 'outcome', 'events', 'total ms'),
        _query(conn, '''
            SELECT COALESCE(gate, '(no gate tag)'), outcome, COUNT(*), SUM(duration_ms)
            FROM hook_events GROUP BY gate, outcome ORDER BY COUNT(*) DESC LIMIT 30''')))
    return '\n'.join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('ingest', 'derive', 'report', 'all', 'query'))
    parser.add_argument('sql', nargs='?', help='for `query`: one SELECT statement')
    parser.add_argument('--days', type=float, default=None,
                        help='only transcripts modified in the last N days (default: all)')
    parser.add_argument('--root', action='append', default=None,
                        help='a Claude Code `projects` directory (default: every ~/.claude*/projects)')
    parser.add_argument('--db', default=None, help='store path (default: see db.py)')
    parser.add_argument('--by-project', action='store_true')
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)
    except db.SchemaMismatch as error:
        print(error, file=sys.stderr)
        return 1
    if args.action == 'query':
        if not args.sql:
            parser.error('query needs a SELECT statement')
        conn.execute('PRAGMA query_only = ON')  # a question never changes the store
        try:
            cursor = conn.execute(args.sql)
        except sqlite3.Error as error:
            print(error, file=sys.stderr)
            return 1
        header = tuple(column[0] for column in cursor.description or ())
        print(_table('', header, [tuple(row) for row in cursor]) if header else '')
    if args.action in ('ingest', 'all'):
        result = ingest.ingest(conn, args.root, args.days)
        print(f'ingested {result.records} records; not understood: {sum(result.unknown.values())}')
    if args.action in ('derive', 'all'):
        print(f'derived {episodes.derive(conn)} episodes')
    if args.action in ('report', 'all'):
        print(report(conn, args.by_project))
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
