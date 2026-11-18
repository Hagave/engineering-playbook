"""
Unit tests for hooks/ledger.py: one test group per record, idempotency, the
session chain across /clear, concurrent writers, store location, and the
failure paths (the hook still exits 0 within its budget and journals once).

Payloads come from tests/unit/ledger_payloads.py (observed shapes). Every
repository is a real temporary git repository: the store key and the
validation runs' repository state are read with git.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK = os.path.join(_ROOT, 'hooks', 'ledger.py')
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import ledger  # noqa: E402
import playbook_core  # noqa: E402

from tests.unit import ledger_payloads as p  # noqa: E402

GITHUB_PAT = 'ghp' + '_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'
SID = '11111111-1111-4111-8111-111111111111'
PID = 'aaaaaaaa-0000-4000-8000-000000000001'


def git(cwd: str, *args: str) -> str:
    return subprocess.run(('git', '-C', cwd) + args, check=True, capture_output=True, text=True).stdout


def make_repo(parent: str, name: str = 'repo') -> str:
    repo = os.path.join(parent, name)
    os.makedirs(repo)
    git(repo, 'init', '-q')
    with open(os.path.join(repo, 'notes.txt'), 'w') as fh:
        fh.write('alpha\nbeta\n')
    git(repo, 'add', '-A')
    git(repo, '-c', 'user.email=t@example.com', '-c', 'user.name=t', 'commit', '-qm', 'init')
    return os.path.realpath(repo)


def hook_env(ledger_dir: str, home: str, pid: str | None = None) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith('CLAUDE')}
    env.update(PLAYBOOK_LEDGER_DIR=ledger_dir, HOME=home)
    if pid:
        env['CLAUDE_PID'] = pid
    return env


def run_hook(payload: object, env: dict) -> tuple[subprocess.CompletedProcess, float]:
    start = time.monotonic()
    done = subprocess.run((sys.executable, _HOOK), input=payload if isinstance(payload, str) else json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=10)
    return done, time.monotonic() - start


def journal_rows(home: str) -> list[dict]:
    rows = []
    hooks = os.path.join(home, '.claude', 'hooks')
    for name in sorted(os.listdir(hooks)) if os.path.isdir(hooks) else []:
        if name.startswith('.learn-'):
            rows += playbook_core.read_incidents(os.path.join(hooks, name), limit=1000)
    return rows


class LedgerCase(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix='ledger-test-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ledger_dir = os.path.join(self.dir, 'state', 'ledger')
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.home)
        self.repo = make_repo(self.dir)
        env = {'PLAYBOOK_LEDGER_DIR': self.ledger_dir}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('CLAUDE_PID', None)
        self.clock = 1_795_000_000.0

    def record(self, payload: dict, at: float | None = None) -> None:
        self.clock = at if at is not None else self.clock + 1
        ledger.record(payload, now=self.clock)

    def store(self, cwd: str | None = None) -> sqlite3.Connection:
        path = ledger.store_path(ledger.locate(cwd or self.repo))
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def rows(self, table: str, cwd: str | None = None) -> list[sqlite3.Row]:
        return self.store(cwd).execute(f'SELECT * FROM {table} ORDER BY ts').fetchall()

    def path(self, name: str) -> str:
        return os.path.join(self.repo, name)


class TestPrompts(LedgerCase):

    def test_prompt_is_stored_verbatim_with_its_ids(self):
        self.record(p.prompt(SID, self.repo, PID, 'rode o comando: echo ok'))
        [row] = self.rows('prompts')
        self.assertEqual((row['session_id'], row['prompt_id'], row['text'], row['chars']),
                         (SID, PID, 'rode o comando: echo ok', 23))
        self.assertEqual((row['permission_mode'], row['worktree'], row['redacted']), ('auto', self.repo, 0))

    def test_same_prompt_twice_is_one_row(self):
        self.record(p.prompt(SID, self.repo, PID, 'faz X'))
        self.record(p.prompt(SID, self.repo, PID, 'faz X'))
        self.assertEqual(len(self.rows('prompts')), 1)

    def test_prompt_typed_while_busy_shares_the_turn_id_and_is_kept(self):
        # Probe: a message typed during a turn arrives with that turn's prompt_id.
        self.record(p.prompt(SID, self.repo, PID, 'rode o comando: sleep 15'))
        self.record(p.prompt(SID, self.repo, PID, 'Entendi. Aguardo'))
        self.assertEqual([row['text'] for row in self.rows('prompts')],
                         ['rode o comando: sleep 15', 'Entendi. Aguardo'])

    def test_notification_turn_is_stored_not_filtered(self):
        text = p.notification('bs5', 'toolu_bg')
        self.record(p.prompt(SID, self.repo, PID, text))
        [row] = self.rows('prompts')
        self.assertEqual(row['text'], text)
        self.assertIsNone(row['command_name'])

    def test_expansion_marks_the_submitted_slash_command(self):
        self.record(p.prompt(SID, self.repo, PID, '/hello'))
        self.record(p.expansion(SID, self.repo, PID, 'hello'))
        [row] = self.rows('prompts')
        self.assertEqual((row['text'], row['expansion_type'], row['command_name'], row['command_source']),
                         ('/hello', 'slash_command', 'hello', 'projectSettings'))

    def test_expansion_first_still_makes_one_row(self):
        self.record(p.expansion(SID, self.repo, PID, 'hello'))
        self.record(p.prompt(SID, self.repo, PID, '/hello'))
        [row] = self.rows('prompts')
        self.assertEqual(row['command_name'], 'hello')

    def test_secret_in_a_prompt_is_redacted(self):
        self.record(p.prompt(SID, self.repo, PID, f'usa o token {GITHUB_PAT} no push'))
        [row] = self.rows('prompts')
        self.assertNotIn(GITHUB_PAT, row['text'])
        self.assertIn('ghp_***', row['text'])
        self.assertEqual(row['redacted'], 1)

    def test_ordinary_prose_about_tokens_is_not_altered(self):
        text = 'o token budget passou de 70%; password reset fica pra depois'
        self.record(p.prompt(SID, self.repo, PID, text))
        self.assertEqual(self.rows('prompts')[0]['text'], text)

    def test_unscanned_prompt_is_kept_and_marked(self):
        with mock.patch('secret_scan.scan_text', return_value=None):
            self.record(p.prompt(SID, self.repo, PID, 'texto'))
        [row] = self.rows('prompts')
        self.assertEqual(row['text'], 'texto')
        self.assertIsNone(row['redacted'])

    def test_oversized_prompt_keeps_its_length(self):
        text = 'x' * (ledger.MAX_TEXT_CHARS + 10)
        self.record(p.prompt(SID, self.repo, PID, text))
        [row] = self.rows('prompts')
        self.assertEqual((len(row['text']), row['chars']), (ledger.MAX_TEXT_CHARS, len(text)))


class TestFiles(LedgerCase):

    def test_read_records_range_and_disk_hash_not_content(self):
        content = 'alpha\nbeta\n'
        self.record(p.read(SID, self.repo, PID, 'toolu_r1', self.path('notes.txt'), content))
        [row] = self.rows('files')
        self.assertEqual((row['tool'], row['action'], row['path']), ('Read', 'read', self.path('notes.txt')))
        self.assertEqual(row['sha256'], ledger.file_digest(self.path('notes.txt'))[0])
        self.assertEqual((row['start_line'], row['num_lines'], row['total_lines'], row['size']), (1, 3, 3, 11))
        with open(ledger.store_path(ledger.locate(self.repo)), 'rb') as fh:
            self.assertNotIn(b'alpha\nbeta', fh.read())

    def test_edit_hashes_the_file_as_written(self):
        with open(self.path('notes.txt'), 'w') as fh:
            fh.write('alpha\ngamma\n')
        self.record(p.edit(SID, self.repo, PID, 'toolu_e1', self.path('notes.txt'), 'beta', 'gamma',
                           'alpha\nbeta\n'))
        [row] = self.rows('files')
        self.assertEqual((row['tool'], row['action'], row['user_modified']), ('Edit', 'write', 0))
        self.assertEqual(row['sha256'], ledger.file_digest(self.path('notes.txt'))[0])

    def test_write_records_create(self):
        with open(self.path('new.txt'), 'w') as fh:
            fh.write('x')
        self.record(p.write(SID, self.repo, PID, 'toolu_w1', self.path('new.txt'), 'x'))
        [row] = self.rows('files')
        self.assertEqual((row['write_type'], row['size']), ('create', 1))

    def test_notebook_edit_uses_notebook_path(self):
        with open(self.path('nb.ipynb'), 'w') as fh:
            fh.write('{}')
        self.record(p.notebook_edit(SID, self.repo, PID, 'toolu_n1', self.path('nb.ipynb')))
        [row] = self.rows('files')
        self.assertEqual((row['tool'], row['path']), ('NotebookEdit', self.path('nb.ipynb')))
        self.assertIsNotNone(row['sha256'])

    def test_notebook_read_has_no_line_range(self):
        with open(self.path('nb.ipynb'), 'w') as fh:
            fh.write('{}')
        self.record(p.read_notebook(SID, self.repo, PID, 'toolu_n0', self.path('nb.ipynb')))
        [row] = self.rows('files')
        self.assertIsNone(row['num_lines'])

    def test_subagent_read_carries_its_agent(self):
        self.record(p.read(SID, self.repo, PID, 'toolu_r2', self.path('notes.txt'), 'alpha',
                           agent=('a0000000000000001', 'Explore')))
        [row] = self.rows('files')
        self.assertEqual((row['agent_id'], row['agent_type']), ('a0000000000000001', 'Explore'))

    def test_same_tool_call_twice_is_one_row(self):
        payload = p.read(SID, self.repo, PID, 'toolu_r3', self.path('notes.txt'), 'alpha')
        self.record(payload)
        self.record(payload)
        self.assertEqual(len(self.rows('files')), 1)

    def test_missing_file_and_directory_have_no_hash(self):
        self.record(p.read(SID, self.repo, PID, 'toolu_r4', self.path('gone.txt'), ''))
        self.record(p.read(SID, self.repo, PID, 'toolu_r5', self.repo, ''))
        self.assertEqual([(row['sha256'], row['size']) for row in self.rows('files')], [(None, None)] * 2)

    def test_file_above_the_hash_cap_keeps_only_its_size(self):
        with mock.patch.object(ledger, 'MAX_HASH_BYTES', 4):
            self.record(p.read(SID, self.repo, PID, 'toolu_r6', self.path('notes.txt'), ''))
        [row] = self.rows('files')
        self.assertEqual((row['sha256'], row['size']), (None, 11))

    def test_payload_without_tool_use_id_fails_loud(self):
        payload = p.read(SID, self.repo, PID, 'x', self.path('notes.txt'), '')
        del payload['tool_use_id']
        with self.assertRaises(ValueError):
            self.record(payload)


class TestAnswers(LedgerCase):

    def test_answer_is_kept_whole(self):
        self.record(p.ask(SID, self.repo, PID, 'toolu_q1'))
        self.record(p.ask(SID, self.repo, PID, 'toolu_q1'))
        [row] = self.rows('answers')
        self.assertEqual(json.loads(row['tool_response'])['answers'], {'Which store?': 'SQLite'})
        self.assertEqual(json.loads(row['tool_input'])['questions'][0]['header'], 'Store')


class TestRuns(LedgerCase):

    def test_validation_run_records_outcome_and_repository_state(self):
        self.record(p.bash(SID, self.repo, PID, 'toolu_b1', 'python3 -m pytest -q', stdout='1 passed'))
        [row] = self.rows('runs')
        self.assertEqual((row['outcome'], row['exit_code'], row['duration_ms']), ('succeeded', None, 386))
        self.assertEqual(row['head'], git(self.repo, 'rev-parse', 'HEAD').strip())
        self.assertIsNotNone(row['tree_sha256'])
        self.assertIsNone(row['state_error'])

    def test_tree_hash_follows_tracked_and_untracked_changes(self):
        clean = ledger.repo_state(self.repo).tree_sha256
        self.assertEqual(clean, ledger.repo_state(self.repo).tree_sha256)
        with open(self.path('scratch.py'), 'w') as fh:
            fh.write('a = 1\n')
        untracked = ledger.repo_state(self.repo).tree_sha256
        with open(self.path('scratch.py'), 'w') as fh:
            fh.write('a = 2\n')
        untracked_changed = ledger.repo_state(self.repo).tree_sha256
        os.remove(self.path('scratch.py'))
        with open(self.path('notes.txt'), 'a') as fh:
            fh.write('delta\n')
        tracked = ledger.repo_state(self.repo).tree_sha256
        self.assertEqual(len({clean, untracked, untracked_changed, tracked}), 4)

    def test_repository_state_writes_nothing_into_git(self):
        with open(self.path('notes.txt'), 'a') as fh:
            fh.write('delta\n')
        os.utime(self.path('notes.txt'), (1, 1))
        index = os.path.join(self.repo, '.git', 'index')
        before = os.stat(index).st_mtime_ns
        ledger.repo_state(self.repo)
        self.assertEqual(os.stat(index).st_mtime_ns, before)

    def test_failed_run_keeps_the_exit_code_not_the_output(self):
        self.record(p.bash_failure(SID, self.repo, PID, 'toolu_b2', 'npm test',
                                   error="Exit code 1\nError: Cannot find module 'express'"))
        [row] = self.rows('runs')
        self.assertEqual((row['outcome'], row['exit_code'], row['interrupted']), ('failed', 1, 0))
        self.assertNotIn('express', json.dumps([tuple(r) for r in self.rows('runs')]))

    def test_background_run_has_no_outcome_yet(self):
        self.record(p.bash(SID, self.repo, PID, 'toolu_b3', 'pytest tests/', background_task_id='bs5'))
        [row] = self.rows('runs')
        self.assertEqual((row['outcome'], row['background_task_id']), ('background', 'bs5'))

    def test_command_that_does_not_validate_is_not_recorded(self):
        for command in ('ls -la', 'cat <<EOF\nnpm test\nEOF', 'git status'):
            payload = p.bash(SID, self.repo, PID, 'toolu_x', command)
            self.assertIsNone(ledger.recorder_for(payload), command)
        self.assertFalse(os.path.exists(self.ledger_dir))

    def test_classifier_is_the_stop_gates(self):
        for command in ('python3 tests/run_all.py', 'npm run lint', 'echo hi', 'cat <<EOF\npytest\nEOF'):
            self.assertEqual(ledger.is_validation_run(p.bash(SID, self.repo, PID, 't', command)),
                             playbook_core.is_validation_command(command), command)

    def test_run_outside_a_repository_says_so(self):
        plain = os.path.join(self.dir, 'plain')
        os.makedirs(plain)
        self.record(p.bash(SID, plain, PID, 'toolu_b4', 'pytest'))
        [row] = self.rows('runs', plain)
        self.assertEqual((row['head'], row['state_error']), (None, 'not a repository'))

    def test_repository_without_commits_says_so(self):
        empty = os.path.join(self.dir, 'empty')
        os.makedirs(empty)
        git(empty, 'init', '-q')
        self.assertEqual(ledger.repo_state(empty), ledger.RepoState(None, None, 'no HEAD'))

    def test_secret_in_a_command_is_redacted(self):
        self.record(p.bash(SID, self.repo, PID, 'toolu_b5', f'GH_TOKEN={GITHUB_PAT} npm test'))
        self.assertNotIn(GITHUB_PAT, self.rows('runs')[0]['command'])


class TestTurns(LedgerCase):

    def test_turn_records_message_and_open_background_tasks(self):
        task = p.background_task('bs5', 'sleep 20 && echo pronto')
        self.record(p.stop(SID, self.repo, PID, 'Rodando em segundo plano.', background=[task]))
        [row] = self.rows('turns')
        self.assertEqual(row['last_message'], 'Rodando em segundo plano.')
        self.assertEqual(json.loads(row['background_tasks']), [task])
        self.assertEqual((row['stop_hook_active'], json.loads(row['session_crons'])), (0, []))

    def test_duplicate_stop_is_one_row_and_a_continuation_is_another(self):
        self.record(p.stop(SID, self.repo, PID, 'feito'))
        self.record(p.stop(SID, self.repo, PID, 'feito'))
        self.record(p.stop(SID, self.repo, PID, 'feito, agora com o token', active=True))
        self.assertEqual([row['stop_hook_active'] for row in self.rows('turns')], [0, 1])


class TestSessionChain(LedgerCase):

    NEW = '22222222-2222-4222-8222-222222222222'

    def clear(self, old: str, new: str, cwd: str | None = None, gap: float = 0.1) -> None:
        self.record(p.session_end(old, cwd or self.repo, 'aaaaaaaa-0000-4000-8000-0000000000ff', 'clear'))
        self.record(p.session_start(new, cwd or self.repo, 'clear'), at=self.clock + gap)

    def session(self, session_id: str) -> sqlite3.Row:
        return self.store().execute('SELECT * FROM sessions WHERE session_id = ?', (session_id,)).fetchone()

    def test_clear_leaves_a_marker_the_next_session_claims(self):
        self.record(p.session_start(SID, self.repo, 'startup', model='claude-opus-5-5'))
        self.clear(SID, self.NEW)
        self.assertEqual(self.session(self.NEW)['inherited_from'], SID)
        self.assertEqual((self.session(SID)['end_reason'], self.session(SID)['model']), ('clear', 'claude-opus-5-5'))
        [marker] = self.store().execute('SELECT * FROM handoffs').fetchall()
        self.assertEqual(marker['claimed_by'], self.NEW)

    def test_marker_is_claimed_once(self):
        self.clear(SID, self.NEW)
        self.record(p.session_start(self.NEW, self.repo, 'clear'))
        self.record(p.session_start('33333333-3333-4333-8333-333333333333', self.repo, 'clear'))
        claimed = self.store().execute('SELECT claimed_by FROM handoffs').fetchall()
        self.assertEqual([row[0] for row in claimed], [self.NEW])
        self.assertIsNone(self.session('33333333-3333-4333-8333-333333333333')['inherited_from'])

    def test_duplicate_session_end_leaves_one_marker(self):
        self.record(p.session_end(SID, self.repo, PID, 'clear'))
        self.record(p.session_end(SID, self.repo, PID, 'clear'))
        self.assertEqual(self.store().execute('SELECT COUNT(*) FROM handoffs').fetchone()[0], 1)

    def test_only_a_cleared_start_claims(self):
        self.record(p.session_end(SID, self.repo, PID, 'clear'))
        for source in ('startup', 'resume', 'compact'):
            self.record(p.session_start(self.NEW, self.repo, source))
        self.assertIsNone(self.store().execute('SELECT claimed_by FROM handoffs').fetchone()[0])

    def test_other_exits_leave_no_marker(self):
        for reason in ('prompt_input_exit', 'resume', 'logout', 'other'):
            self.record(p.session_end(SID, self.repo, PID, reason))
        self.assertEqual(self.store().execute('SELECT COUNT(*) FROM handoffs').fetchone()[0], 0)
        self.assertEqual(self.session(SID)['end_reason'], 'other')

    def test_marker_of_another_worktree_is_not_claimed(self):
        other = os.path.join(self.dir, 'wt')
        git(self.repo, 'worktree', 'add', '-q', other)
        self.record(p.session_end(SID, os.path.realpath(other), PID, 'clear'))
        self.record(p.session_start(self.NEW, self.repo, 'clear'))
        self.assertIsNone(self.session(self.NEW)['inherited_from'])

    def test_stale_marker_is_not_claimed(self):
        self.clear(SID, self.NEW, gap=ledger.CLAIM_WINDOW_S + 1)
        self.assertIsNone(self.session(self.NEW)['inherited_from'])

    def test_the_newest_open_marker_wins_and_pid_match_is_recorded(self):
        os.environ['CLAUDE_PID'] = '4242'
        self.record(p.session_end('older', self.repo, PID, 'clear'))
        self.record(p.session_end(SID, self.repo, PID, 'clear'))
        self.record(p.session_start(self.NEW, self.repo, 'clear'))
        rows = self.store().execute('SELECT from_session, claimed_by, pid_match FROM handoffs '
                                    'ORDER BY created_at').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('older', None, None), (SID, self.NEW, 1)])

    def test_compact_restart_keeps_the_first_source(self):
        self.record(p.session_start(SID, self.repo, 'startup'))
        self.record(p.session_start(SID, self.repo, 'compact'))
        row = self.session(SID)
        self.assertEqual((row['source'], row['last_source']), ('startup', 'compact'))


class TestLocation(LedgerCase):

    def test_worktrees_share_one_store_and_rows_name_their_worktree(self):
        other = os.path.realpath(os.path.join(self.dir, 'wt'))
        git(self.repo, 'worktree', 'add', '-q', other)
        self.assertEqual(ledger.store_path(ledger.locate(self.repo)), ledger.store_path(ledger.locate(other)))
        self.record(p.prompt(SID, self.repo, PID, 'no principal'))
        self.record(p.prompt(SID, os.path.join(other, ''), 'p2', 'na worktree'))
        self.assertEqual([row['worktree'] for row in self.rows('prompts')], [self.repo, other])

    def test_subdirectory_is_the_same_store(self):
        sub = os.path.join(self.repo, 'src')
        os.makedirs(sub)
        self.assertEqual(ledger.store_path(ledger.locate(sub)), ledger.store_path(ledger.locate(self.repo)))
        self.assertEqual(ledger.locate(sub).worktree, self.repo)

    def test_another_repository_has_its_own_store(self):
        other = make_repo(self.dir, 'other')
        self.assertNotEqual(ledger.store_path(ledger.locate(self.repo)), ledger.store_path(ledger.locate(other)))

    def test_directory_outside_a_repository_has_its_own_store(self):
        plain = os.path.join(self.dir, 'plain')
        os.makedirs(plain)
        location = ledger.locate(plain)
        self.assertEqual((location.in_git, location.worktree), (False, os.path.realpath(plain)))
        self.assertNotEqual(ledger.store_path(location), ledger.store_path(ledger.locate(self.repo)))

    def test_store_lives_in_the_state_directory_owner_only(self):
        self.record(p.prompt(SID, self.repo, PID, 'oi'))
        path = ledger.store_path(ledger.locate(self.repo))
        self.assertTrue(path.startswith(self.ledger_dir + os.sep))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.ledger_dir).st_mode & 0o777, 0o700)
        self.assertEqual(self.store().execute("SELECT value FROM meta WHERE key = 'project'").fetchone()[0],
                         os.path.join(self.repo, '.git'))

    def test_default_directory_follows_xdg_state_home(self):
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': '/state'}):
            os.environ.pop('PLAYBOOK_LEDGER_DIR')
            self.assertEqual(ledger.ledger_dir(), '/state/engineering-playbook/ledger')

    def test_unrecorded_event_opens_no_store(self):
        for event in ('PreToolUse', 'Notification', 'PreCompact', 'SubagentStop'):
            ledger.record({**p.common(SID, self.repo, PID), 'hook_event_name': event})
        self.assertFalse(os.path.exists(self.ledger_dir))


class TestConcurrentWriters(LedgerCase):

    WRITERS = 8
    EVENTS = 12

    def test_concurrent_processes_write_every_row_once(self):
        """Every writer starts on a store that does not exist yet, and every
        writer also sends the same shared events, as parallel hooks would."""
        script = os.path.join(self.dir, 'writer.py')
        with open(script, 'w') as fh:
            fh.write(
                'import json, subprocess, sys\n'
                'for line in sys.stdin:\n'
                f'    done = subprocess.run([sys.executable, {_HOOK!r}], input=line, text=True)\n'
                '    assert done.returncode == 0\n')
        env = hook_env(self.ledger_dir, self.home)
        procs = []
        for writer in range(self.WRITERS):
            lines = []
            for i in range(self.EVENTS):
                lines.append(p.prompt(SID, self.repo, f'w{writer}-{i}', f'prompt {writer}-{i}'))
                lines.append(p.read(SID, self.repo, f'w{writer}-{i}', f'toolu_{writer}_{i}',
                                    self.path('notes.txt'), 'alpha'))
            lines.append(p.prompt(SID, self.repo, 'shared', 'the same prompt'))
            lines.append(p.stop(SID, self.repo, 'shared', 'the same turn'))
            proc = subprocess.Popen((sys.executable, script), stdin=subprocess.PIPE, env=env, text=True)
            proc.stdin.write(''.join(json.dumps(line) + '\n' for line in lines))
            proc.stdin.close()
            procs.append(proc)
        self.assertEqual([proc.wait(timeout=120) for proc in procs], [0] * self.WRITERS)
        conn = self.store()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM prompts').fetchone()[0],
                         self.WRITERS * self.EVENTS + 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM files').fetchone()[0], self.WRITERS * self.EVENTS)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM turns').fetchone()[0], 1)
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(journal_rows(self.home), [])

    def test_concurrent_cleared_starts_claim_one_marker(self):
        env = hook_env(self.ledger_dir, self.home)
        run_hook(p.session_end(SID, self.repo, PID, 'clear'), env)
        starts = [p.session_start(f'new-{i}', self.repo, 'clear') for i in range(6)]
        procs = [subprocess.Popen((sys.executable, _HOOK), stdin=subprocess.PIPE, env=env, text=True)
                 for _ in starts]
        for proc, payload in zip(procs, starts):
            proc.stdin.write(json.dumps(payload))
            proc.stdin.close()
        self.assertEqual([proc.wait(timeout=30) for proc in procs], [0] * len(procs))
        inherited = self.store().execute(
            'SELECT session_id FROM sessions WHERE inherited_from IS NOT NULL').fetchall()
        self.assertEqual(len(inherited), 1)
        self.assertEqual(journal_rows(self.home), [])


class TestFailurePaths(LedgerCase):
    """The hook exits 0 with no output inside its budget, and the failure is
    in the session's journal once."""

    BUDGET_S = 1.5

    def assert_clean_failure(self, payload: object, expected: str) -> None:
        env = hook_env(self.ledger_dir, self.home)
        for _ in range(2):
            done, elapsed = run_hook(payload, env)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, '', ''))
            self.assertLess(elapsed, self.BUDGET_S)
        rows = [row for row in journal_rows(self.home) if row.get('kind') == 'ledger_error']
        self.assertEqual(len(rows), 1, rows)
        self.assertIn(expected, rows[0]['error'])

    def test_unwritable_state_directory(self):
        parent = os.path.dirname(self.ledger_dir)
        os.makedirs(parent)
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        if os.access(parent, os.W_OK):
            self.skipTest('running with permissions that ignore the mode')
        self.assert_clean_failure(p.prompt(SID, self.repo, PID, 'oi'), 'PermissionError')

    def test_locked_store(self):
        self.record(p.prompt(SID, self.repo, PID, 'cria o banco'))
        holder = sqlite3.connect(ledger.store_path(ledger.locate(self.repo)), isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute('BEGIN EXCLUSIVE')
        self.assert_clean_failure(p.prompt(SID, self.repo, 'p2', 'bloqueado'), 'database is locked')
        holder.execute('ROLLBACK')

    def test_other_schema_version(self):
        self.record(p.prompt(SID, self.repo, PID, 'cria o banco'))
        conn = self.store()
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        conn.commit()
        self.assert_clean_failure(p.prompt(SID, self.repo, 'p2', 'outra versão'), 'SchemaMismatch')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM prompts').fetchone()[0], 1)

    def test_corrupt_store(self):
        path = ledger.store_path(ledger.locate(self.repo))
        os.makedirs(os.path.dirname(path))
        with open(path, 'wb') as fh:
            fh.write(b'not a database at all, just bytes' * 100)
        self.assert_clean_failure(p.prompt(SID, self.repo, PID, 'oi'), 'DatabaseError')

    def test_malformed_stdin(self):
        self.assert_clean_failure('not json', 'JSONDecodeError')

    def test_payload_that_is_not_an_object(self):
        self.assert_clean_failure('[1, 2]', 'not an object')

    def test_distinct_failures_are_each_journaled(self):
        env = hook_env(self.ledger_dir, self.home)
        run_hook('not json', env)
        run_hook('[1]', env)
        self.assertEqual(len([row for row in journal_rows(self.home) if row['kind'] == 'ledger_error']), 2)


if __name__ == '__main__':
    unittest.main()
