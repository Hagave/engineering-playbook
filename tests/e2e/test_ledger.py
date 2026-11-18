"""
End to end: the ledger hook as Claude Code runs it, one process per event,
fed the event sequence the probe recorded (session start, prompts, file
tools, a validation run that passes and one that fails, a background run and
its notification turn, a slash command, /clear, the next session, exit),
then read back with the developer CLI.

The project is a real git repository and a real worktree of it; the test
also proves nothing in either tree, .git included, was touched.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK = os.path.join(_ROOT, 'hooks', 'ledger.py')
_CLI = os.path.join(_ROOT, 'dev', 'observe', 'ledger.py')
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import ledger  # noqa: E402

from tests.unit import ledger_payloads as p  # noqa: E402
from tests.unit.test_ledger import hook_env, journal_rows, make_repo, git  # noqa: E402

OLD = '0ld00000-0000-4000-8000-00000000000a'
NEW = '0e000000-0000-4000-8000-00000000000b'
SUB = ('a0000000000000001', 'Explore')


def tree_snapshot(root: str) -> dict[str, tuple[int, int]]:
    snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            snapshot[os.path.relpath(path, root)] = (info.st_size, info.st_mtime_ns)
    return snapshot


class TestLedgerEndToEnd(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix='ledger-e2e-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = make_repo(self.dir)
        self.worktree = os.path.realpath(os.path.join(self.dir, 'wt'))
        git(self.repo, 'worktree', 'add', '-q', self.worktree)
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.home)
        self.ledger_dir = os.path.join(self.dir, 'state')
        self.env = hook_env(self.ledger_dir, self.home, pid='31337')
        patcher = mock.patch.dict(os.environ, {'PLAYBOOK_LEDGER_DIR': self.ledger_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        repo, wt = self.repo, self.worktree
        notes, new, nb = (os.path.join(repo, name) for name in ('notes.txt', 'new.txt', 'nb.ipynb'))
        with open(notes, 'w') as fh:
            fh.write('alpha\ngamma\n')
        with open(new, 'w') as fh:
            fh.write('x')
        with open(nb, 'w') as fh:
            fh.write('{"cells": []}')
        t1, t2, t3, t4, t5, t6 = (f'aaaaaaaa-0000-4000-8000-00000000000{i}' for i in range(1, 7))
        self.sequence = [
            p.session_start(OLD, repo, 'startup', model='claude-opus-5-5'),
            p.prompt(OLD, repo, t1, 'implementa o ledger e roda os testes'),
            p.read(OLD, repo, t1, 'toolu_read', notes, 'alpha\nbeta\n'),
            p.edit(OLD, repo, t1, 'toolu_edit', notes, 'beta', 'gamma', 'alpha\nbeta\n'),
            p.write(OLD, repo, t1, 'toolu_write', new, 'x'),
            p.read_notebook(OLD, repo, t1, 'toolu_nbread', nb),
            p.notebook_edit(OLD, repo, t1, 'toolu_nbedit', nb),
            p.read(OLD, repo, t1, 'toolu_subread', notes, 'alpha\ngamma\n', agent=SUB),
            p.bash(OLD, repo, t1, 'toolu_ls', 'ls -la'),
            p.bash(OLD, repo, t1, 'toolu_pass', 'python3 -m pytest -q | tail -3', stdout='3 passed'),
            p.bash_failure(OLD, repo, t1, 'toolu_fail', 'python3 -m pytest -q'),
            p.ask(OLD, repo, t1, 'toolu_ask'),
            p.stop(OLD, repo, t1, 'Feito; um teste falhou e está aberto.'),
            p.prompt(OLD, repo, t2, 'rode em segundo plano: npm test'),
            p.bash(OLD, repo, t2, 'toolu_bg', 'npm test', background_task_id='bg0000001'),
            p.stop(OLD, repo, t2, 'Rodando em segundo plano.',
                   background=[p.background_task('bg0000001', 'npm test')]),
            p.prompt(OLD, repo, t3, p.notification('bg0000001', 'toolu_bg')),
            p.stop(OLD, repo, t3, 'Terminou com código 0.'),
            p.prompt(OLD, repo, t4, 'rode o comando: sleep 15'),
            p.prompt(OLD, repo, t4, 'Entendi. Aguardo'),
            p.stop(OLD, repo, t4, 'Terminou.'),
            p.prompt(OLD, wt, t5, '/hello'),
            p.expansion(OLD, wt, t5, 'hello'),
            p.stop(OLD, wt, t5, 'ok'),
            p.session_end(OLD, wt, 'aaaaaaaa-0000-4000-8000-0000000000ff', 'clear'),
            p.session_start(NEW, wt, 'clear'),
            p.prompt(NEW, wt, t6, 'continua de onde parou'),
            p.session_end(NEW, wt, t6, 'prompt_input_exit'),
        ]

    def replay(self) -> None:
        for payload in self.sequence:
            done = subprocess.run((sys.executable, _HOOK), input=json.dumps(payload), capture_output=True,
                                  text=True, env=self.env, timeout=10)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, '', ''), payload['hook_event_name'])

    def store(self) -> sqlite3.Connection:
        path = ledger.store_path(ledger.locate(self.repo))
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        self.addCleanup(conn.close)
        return conn

    def cli(self, *args: str) -> str:
        done = subprocess.run((sys.executable, _CLI) + args + ('--project', self.worktree), capture_output=True,
                              text=True, env=self.env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_recorded_sequence(self):
        before = (tree_snapshot(self.repo), tree_snapshot(self.worktree))
        self.replay()
        self.replay()
        self.assertEqual((tree_snapshot(self.repo), tree_snapshot(self.worktree)), before)
        self.assertEqual(journal_rows(self.home), [])
        stores = {name for name in os.listdir(self.ledger_dir) if not name.endswith(('-wal', '-shm'))}
        self.assertEqual(stores, {os.path.basename(ledger.store_path(ledger.locate(self.repo)))})
        conn = self.store()

        sessions = conn.execute('SELECT session_id, worktree, source, inherited_from, end_reason, host_pid '
                                'FROM sessions ORDER BY started_at').fetchall()
        self.assertEqual(sessions, [(OLD, self.repo, 'startup', None, 'clear', 31337),
                                    (NEW, self.worktree, 'clear', OLD, 'prompt_input_exit', 31337)])
        self.assertEqual(conn.execute('SELECT from_session, worktree, claimed_by, pid_match FROM handoffs').fetchall(),
                         [(OLD, self.worktree, NEW, 1)])

        prompts = conn.execute('SELECT session_id, text, command_name FROM prompts ORDER BY ts').fetchall()
        self.assertEqual(len(prompts), 7)
        self.assertEqual([row[1] for row in prompts if row[0] == OLD][3:5], ['rode o comando: sleep 15',
                                                                             'Entendi. Aguardo'])
        self.assertIn(('/hello', 'hello'), [(row[1], row[2]) for row in prompts])

        files = conn.execute('SELECT tool_use_id, action, agent_type, sha256 FROM files ORDER BY ts').fetchall()
        self.assertEqual([row[:3] for row in files], [
            ('toolu_read', 'read', None), ('toolu_edit', 'write', None), ('toolu_write', 'write', None),
            ('toolu_nbread', 'read', None), ('toolu_nbedit', 'write', None), ('toolu_subread', 'read', 'Explore')])
        self.assertEqual(files[0][3], ledger.file_digest(os.path.join(self.repo, 'notes.txt'))[0])

        runs = conn.execute('SELECT tool_use_id, outcome, exit_code, head, tree_sha256 FROM runs ORDER BY ts').fetchall()
        head = git(self.repo, 'rev-parse', 'HEAD').strip()
        self.assertEqual([row[:4] for row in runs], [('toolu_pass', 'succeeded', None, head),
                                                    ('toolu_fail', 'failed', 1, head),
                                                    ('toolu_bg', 'background', None, head)])
        self.assertEqual(len({row[4] for row in runs}), 1)

        self.assertEqual(conn.execute('SELECT COUNT(*) FROM answers').fetchone()[0], 1)
        turns = conn.execute('SELECT last_message, background_tasks FROM turns ORDER BY ts').fetchall()
        self.assertEqual(len(turns), 5)
        self.assertEqual(json.loads(turns[1][1])[0]['status'], 'running')

        listing = self.cli('sessions')
        self.assertIn(OLD, listing)
        self.assertIn(NEW, listing)
        shown = self.cli('show', OLD[:8])
        self.assertIn(f'handoff claimed by {NEW}', shown)
        self.assertIn('harness', shown)
        self.assertIn('slash command', shown)
        self.assertIn('failed (exit 1)', shown)


if __name__ == '__main__':
    unittest.main()
