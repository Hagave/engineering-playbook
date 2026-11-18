"""
Unit tests for dev/observe: the local observability store built from Claude
Code transcripts (ingestion, rework episodes, packaging).

The transcript records below copy the shapes of real ones (keys read from
real sessions with CLI 2.1.326-2.1.327); they prove the reader, not that a
later CLI still writes these shapes. That is what `unknown_records` and a real
`cli.py all` run are for.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'dev', 'observe'))
import changes  # noqa: E402
import cli  # noqa: E402
import db  # noqa: E402
import episodes  # noqa: E402
import ingest  # noqa: E402
import shell  # noqa: E402

SESSION = 'sess-1'
CWD = '/work/app'


class Transcript:
    """Builds one session's JSONL the way Claude Code writes it."""

    def __init__(self, agent_id: str = '') -> None:
        self.lines: list[dict] = []
        self.clock = 0
        self.agent_id = agent_id
        self.calls = 0

    def _base(self, kind: str) -> dict:
        self.clock += 1
        record = {'type': kind, 'uuid': f'u{self.agent_id}{self.clock}', 'sessionId': SESSION,
                  'cwd': CWD, 'timestamp': f'2026-01-01T00:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z'}
        if self.agent_id:
            record['agentId'] = self.agent_id
        return record

    def prompt(self, text: str, source: str = 'typed') -> None:
        record = self._base('user')
        record.update(promptSource=source, message={'role': 'user', 'content': text})
        self.lines.append(record)

    def call(self, tool: str, tool_input: dict, result: object = None, text: str = 'ok',
             is_error: bool = False, **extra: object) -> str:
        self.calls += 1
        call_id = f'toolu_{self.agent_id}{self.calls}'
        asked = self._base('assistant')
        asked.update(thinkingDurationMs=700, toolInputStreamMs=50, effort='high', requestRef='req-1', message={
            'id': f'msg_{self.agent_id}{self.calls}', 'model': 'claude-test',
            'usage': {'input_tokens': 2, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 10,
                      'output_tokens': 40, 'output_tokens_details': {'thinking_tokens': 15}},
            'content': [{'type': 'tool_use', 'id': call_id, 'name': tool, 'input': tool_input}]})
        answered = self._base('user')
        block = {'type': 'tool_result', 'tool_use_id': call_id, 'content': text}
        if is_error:
            block['is_error'] = True
        answered['message'] = {'role': 'user', 'content': [block]}
        if result is not None:
            answered['toolUseResult'] = result
        answered.update(extra)
        self.lines += [asked, answered]
        return call_id

    # The four things a session does to files.

    def read(self, path: str, start: int = 1, lines: int = 100, total: int = 100) -> str:
        return self.call('Read', {'file_path': path}, {'type': 'text', 'file': {
            'filePath': path, 'startLine': start, 'numLines': lines, 'totalLines': total}})

    def create(self, path: str, lines: int = 20) -> str:
        return self.call('Write', {'file_path': path}, {
            'type': 'create', 'filePath': path, 'content': '\n'.join(['x'] * lines), 'structuredPatch': []})

    def edit(self, path: str, at: int, old: int = 3, new: int = 3) -> str:
        return self.call('Edit', {'file_path': path}, {'filePath': path, 'structuredPatch': [
            {'oldStart': at, 'oldLines': old, 'newStart': at, 'newLines': new, 'lines': []}]})

    def bash(self, command: str, text: str = 'ok', exit_code: int = 0, **result: object) -> str:
        shaped = {'stdout': text, 'stderr': '', 'interrupted': False, **result}
        if exit_code:
            return self.call('Bash', {'command': command}, f'Error: Exit code {exit_code}\n{text}',
                             text=f'Exit code {exit_code}\n{text}', is_error=True)
        return self.call('Bash', {'command': command}, shaped, text=text)

    def write_to(self, root: str, project: str = 'proj') -> str:
        folder = os.path.join(root, project)
        name = f'{SESSION}.jsonl'
        if self.agent_id:
            folder = os.path.join(folder, SESSION, 'subagents')
            name = f'agent-{self.agent_id}.jsonl'
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, 'w') as handle:
            handle.writelines(json.dumps(line) + '\n' for line in self.lines)
        return path


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, '.claude-test', 'projects')
        self.db_path = os.path.join(self.tmp, 'state', 'observe.db')
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def load(self, *transcripts: Transcript) -> ingest.Ingester:
        for transcript in transcripts:
            transcript.write_to(self.root)
        return ingest.ingest(self.conn, [self.root])

    def one(self, sql: str, *args: object):
        return self.conn.execute(sql, args).fetchone()

    def count(self, table: str) -> int:
        return self.one(f'SELECT COUNT(*) FROM {table}')[0]

    def episodes(self, *transcripts: Transcript) -> list:
        self.load(*transcripts)
        episodes.derive(self.conn)
        return self.conn.execute('SELECT * FROM episodes ORDER BY ts_start').fetchall()


class BashReadsTests(unittest.TestCase):
    def reads(self, command: str) -> list[tuple]:
        return [(r.path, r.start_line, r.end_line, r.full) for r in shell.bash_reads(command, CWD) or []]

    def test_cat_is_a_whole_file(self):
        self.assertEqual(self.reads('cat src/a.py'), [('/work/app/src/a.py', None, None, True)])

    def test_sed_print_range(self):
        self.assertEqual(self.reads('sed -n 10,40p src/a.py'), [('/work/app/src/a.py', 10, 40, False)])

    def test_head_count(self):
        self.assertEqual(self.reads('head -n 30 src/a.py'), [('/work/app/src/a.py', 1, 30, False)])

    def test_grep_over_a_file_is_a_partial_view(self):
        self.assertEqual(self.reads('grep -n "a|b" src/a.py'), [('/work/app/src/a.py', None, None, False)])

    def test_recursive_grep_names_no_file(self):
        self.assertEqual(self.reads('grep -rn foo src/a.py'), [])

    def test_cd_moves_the_base(self):
        self.assertEqual(self.reads('cd /other/repo && cat lib/b.ts | head -5'),
                         [('/other/repo/lib/b.ts', None, None, True)])

    def test_sed_in_place_is_not_a_read(self):
        self.assertEqual(self.reads("sed -i 's/a/b/' src/a.py"), [])

    def test_variable_path_is_skipped(self):
        self.assertEqual(self.reads('cat $FILE'), [])

    def test_untokenizable_command_is_reported_not_empty(self):
        self.assertIsNone(shell.bash_reads('cat "unterminated', CWD))


class ValidationKeyTests(unittest.TestCase):
    def test_recognised_checks(self):
        for command, key in (
            ('python3 -m pytest -q tests/unit', 'python -m pytest'),
            ('FOO=1 timeout 60 python3 tests/run_all.py unit 2>&1 | tail -3', 'python tests/run_all.py'),
            ('npm run test -- --watch=false', 'npm test'),
            ('pnpm lint', 'pnpm lint'),
            ('yarn e2e', 'yarn e2e'),
            ('npx tsc --noEmit', 'tsc'),
            ('./mvnw -q verify', 'mvnw'),
            ('cd api && go test ./...', 'go test'),
            ('cargo clippy --all-targets', 'cargo clippy'),
            ('ruff check hooks', 'ruff check'),
            ('bash tests/test_codex_behavior.sh', 'sh tests/test_codex_behavior.sh'),
        ):
            with self.subTest(command=command):
                self.assertEqual(shell.validation_key(command), key)

    def test_a_mention_is_not_a_run(self):
        for command in ('grep tsc package.json', 'echo jest', 'cat eslint.config.js',
                        'git log --grep=pytest', 'python3 scripts/latest.py', 'ruff format .'):
            with self.subTest(command=command):
                self.assertIsNone(shell.validation_key(command))

    def test_a_rerun_with_other_flags_is_the_same_check(self):
        self.assertEqual(shell.validation_key('python3 -m pytest -q'),
                         shell.validation_key('python3 -m pytest tests/unit/test_a.py -x'))


class OutputVerdictTests(unittest.TestCase):
    def test_failure_lines(self):
        for tail in ('Ran 12 tests in 0.1s\n\nFAILED (failures=2)', 'Found 3 errors.', '2 failed, 10 passed',
                     '[ERROR] BUILD FAILURE', 'Tests run: 5, Failures: 1, Errors: 0',
                     'src/a.ts(3,1): error TS2322: nope', 'npm ERR! code 1'):
            with self.subTest(tail=tail):
                self.assertTrue(shell.output_failed(tail))

    def test_zero_counts_and_passes_are_not_failures(self):
        for tail in ('Ran 12 tests in 0.1s\n\nOK', '0 errors, 0 warnings', '10 passed, 0 failed',
                     'All checks passed!', 'Tests run: 5, Failures: 0, Errors: 0', '', None):
            with self.subTest(tail=tail):
                self.assertFalse(shell.output_failed(tail))


class ErrorClassTests(unittest.TestCase):
    def test_one_per_class(self):
        for text, expected in (
            ('SyntaxError: invalid syntax', 'syntax'),
            ("ModuleNotFoundError: No module named 'x'", 'import_or_name'),
            ("error TS2322: Type 'a' is not assignable", 'type'),
            ('AssertionError: 1 != 2', 'assertion'),
            ('subprocess.TimeoutExpired: timed out after 5', 'timeout'),
            ('Traceback (most recent call last):\n  ValueError: bad', 'runtime'),
            ('something nobody classified', 'unknown'),
        ):
            with self.subTest(text=text):
                self.assertEqual(shell.error_class(text), expected)

    def test_a_linter_failure_is_lint_whatever_it_prints(self):
        self.assertEqual(shell.error_class('F401 imported but unused\nFound 1 error.', 'ruff check'), 'lint')


class ChangeClassTests(unittest.TestCase):
    def kind(self, *lines: str) -> str:
        return changes.classify(list(lines)).kind

    def test_one_per_class(self):
        for expected, lines in (
            (changes.CONSTRUCTION, ['+def a():', '+    return 1']),
            (changes.CONSTRUCTION, ['-def a():', '+def a():', '+    """Doc."""', ' context']),
            (changes.DELETION, ['-x = 1', '-y = 2']),
            (changes.REFLOW, ['-call(a,', '-     b)', '+call(a, b)']),
            (changes.COMMENT, ['-# the old reason', '+# the new reason', ' x = 1']),
            (changes.COMMENT, ['-x = compute()', '+# why this is computed here', '+x = compute()', '-# stale']),
            (changes.CODE_SMALL, ['-aim = body.GetWeaponAim()', '+aim = body.GetAim()']),
            (changes.CODE_MEDIUM, ['-return a + b', '+return (a + b) * scale if scale else 0']),
            (changes.CODE_LARGE, ['-return a', '+if a is None:', '+    raise ValueError("a")',
                                  '+total = sum(v.amount for v in a if v.open)', '+return total / len(a)']),
            (changes.UNKNOWN, []),
        ):
            with self.subTest(lines=lines):
                self.assertEqual(self.kind(*lines), expected)

    def test_added_lines_are_split_into_code_and_comment(self):
        change = changes.classify(['+# why', '+// also why', '+x = 1', '+', '+    y = 2  # trailing is code'])
        self.assertEqual((change.added_code, change.added_comment), (2, 2))

    def test_a_huge_hunk_is_large_without_being_aligned(self):
        lines = ['-' + ' '.join(f'a{i}' for i in range(1500)), '+' + ' '.join(f'b{i}' for i in range(1500))]
        change = changes.classify(lines)
        self.assertEqual((change.kind, change.tokens_changed), (changes.CODE_LARGE, 1500))

    def test_small_change_counts_its_tokens(self):
        self.assertEqual(changes.classify(['-toast.showWarn(a)', '+toast.showWarning(a)']).tokens_changed, 1)


class IngestTests(StoreCase):
    def test_tool_call_with_its_result(self):
        t = Transcript()
        t.prompt('add the endpoint')
        call_id = t.bash('python3 -m pytest -q', text='boom', exit_code=1)
        self.load(t)
        row = self.one('SELECT * FROM tool_calls WHERE tool_use_id = ?', call_id)
        self.assertEqual((row['tool'], row['kind'], row['validation'], row['is_error'], row['exit_code']),
                         ('Bash', 'bash', 'python -m pytest', 1, 1))
        self.assertIn('boom', row['error_head'])
        self.assertLess(row['ts_call'], row['ts_result'])
        self.assertEqual(self.one('SELECT source, text FROM prompts')['text'], 'add the endpoint')

    def test_usage_is_stored_once_per_response_and_thinking_per_block(self):
        t = Transcript()
        t.read('/work/app/a.py')
        second_block = dict(t.lines[0], uuid='u-second', thinkingDurationMs=300)
        t.lines.insert(1, second_block)
        self.load(t)
        self.assertEqual(self.count('api_calls'), 1)
        self.assertEqual(self.one('SELECT output_tokens, thinking_tokens, effort FROM api_calls')[:],
                         (40, 15, 'high'))
        self.assertEqual(self.one('SELECT SUM(thinking_ms) FROM api_blocks')[0], 1000)

    def test_read_ranges(self):
        t = Transcript()
        whole = t.read('/work/app/a.py')
        part = t.read('/work/app/b.py', start=40, lines=20, total=300)
        t.bash('sed -n 5,9p src/c.py')
        self.load(t)
        self.assertEqual(self.one('SELECT start_line, end_line, full FROM reads WHERE tool_use_id = ?', whole)[:],
                         (1, 100, 1))
        self.assertEqual(self.one('SELECT start_line, end_line, full FROM reads WHERE tool_use_id = ?', part)[:],
                         (40, 59, 0))
        self.assertEqual(self.one("SELECT path, start_line, end_line FROM reads WHERE via = 'bash'")[:],
                         ('/work/app/src/c.py', 5, 9))

    def test_edits_from_the_edit_tool_a_created_file_and_bash(self):
        t = Transcript()
        t.edit('/work/app/a.py', at=10, old=2, new=5)
        t.create('/work/app/new.py', lines=7)
        t.bash("sed -i 's/a/b/' c.py", bashEditDiff={'files': [{'filePath': '/work/app/c.py', 'hunks': [
            {'oldStart': 3, 'oldLines': 1, 'newStart': 3, 'newLines': 1, 'lines': []}]}]})
        self.load(t)
        rows = {row['path']: row for row in self.conn.execute('SELECT * FROM edits')}
        self.assertEqual((rows['/work/app/a.py']['old_lines'], rows['/work/app/a.py']['new_lines']), (2, 5))
        self.assertEqual((rows['/work/app/new.py']['old_lines'], rows['/work/app/new.py']['new_lines']), (0, 7))
        self.assertEqual(rows['/work/app/c.py']['via'], 'bash')

    def test_each_hunk_records_what_it_changed(self):
        t = Transcript()
        t.call('Edit', {'file_path': '/work/app/a.py'}, {'filePath': '/work/app/a.py', 'structuredPatch': [
            {'oldStart': 3, 'oldLines': 1, 'newStart': 3, 'newLines': 1,
             'lines': ['-aim = body.GetWeaponAim()', '+aim = body.GetAim()']},
            {'oldStart': 9, 'oldLines': 1, 'newStart': 9, 'newLines': 2,
             'lines': ['-# old', '+# new', '+# and more']}]})
        t.call('Write', {'file_path': '/work/app/new.py'}, {
            'type': 'create', 'filePath': '/work/app/new.py', 'structuredPatch': [],
            'content': '# header\nx = 1\ny = 2'})
        self.load(t)
        rows = [tuple(row) for row in self.conn.execute(
            'SELECT change, tokens_changed, added_code, added_comment FROM edits ORDER BY path, hunk')]
        self.assertEqual(rows, [('code_small', 1, 1, 0), ('comment', 0, 0, 2), ('construction', 0, 2, 1)])

    def test_hook_outcomes_and_a_denied_call(self):
        t = Transcript()
        call_id = t.call('Bash', {'command': 'rm -rf build'}, 'Error: PreToolUse:Bash hook error',
                         text='PreToolUse:Bash hook error: [playbook:safety] denied', is_error=True,
                         toolDenialKind='permission-rule',
                         permissionDecision={'decision': 'reject', 'source': 'hook'})
        for attachment in (
            {'type': 'hook_system_message', 'content': '[playbook:safety] denied', 'hookName': 'PreToolUse:Bash',
             'toolUseID': call_id, 'hookEvent': 'PreToolUse'},
            {'type': 'hook_blocking_error', 'hookName': 'Stop', 'toolUseID': 'x', 'hookEvent': 'Stop',
             'blockingError': {'blockingError': '[playbook:validate] no validation ran', 'command': 'c'}},
            {'type': 'hook_success', 'hookName': 'PostToolUse:Bash', 'toolUseID': call_id,
             'hookEvent': 'PostToolUse', 'content': '', 'stdout': '', 'stderr': '', 'exitCode': 0,
             'command': 'python3 hooks/x.py', 'durationMs': 61},
            {'type': 'skill_listing', 'content': 'not a hook'},
        ):
            record = t._base('attachment')
            record['attachment'] = attachment
            t.lines.append(record)
        self.load(t)
        self.assertEqual(self.one('SELECT denial, permission FROM tool_calls')[:],
                         ('permission-rule', 'hook:reject'))
        hooks = {row['outcome']: row for row in self.conn.execute('SELECT * FROM hook_events')}
        self.assertEqual(set(hooks), {'system_message', 'blocking_error', 'success'})
        self.assertEqual(hooks['blocking_error']['gate'], 'validate')
        self.assertEqual(hooks['success']['duration_ms'], 61)

    def test_subagent_transcript_is_its_own_agent(self):
        main, sub = Transcript(), Transcript(agent_id='abc123')
        main.read('/work/app/a.py')
        sub.read('/work/app/b.py')
        del sub.lines[0]['agentId']  # the file name still says whose it is
        self.load(main, sub)
        self.assertEqual({row['agent_id'] for row in self.conn.execute('SELECT agent_id FROM sessions')},
                         {'', 'abc123'})

    def test_unknown_shapes_are_counted_not_dropped(self):
        t = Transcript()
        t.read('/work/app/a.py')
        t.lines.append({'type': 'a-record-from-the-future', 'payload': 1})
        path = t.write_to(self.root)
        with open(path, 'a') as handle:
            handle.write('{not json}\n')
        result = ingest.ingest(self.conn, [self.root])
        self.assertEqual(dict(result.unknown), {'a-record-from-the-future': 1, '<bad json>': 1})
        self.assertEqual(self.count('unknown_records'), 2)
        self.assertEqual(self.count('tool_calls'), 1)

    def test_a_known_record_in_an_unexpected_shape_does_not_stop_the_run(self):
        t = Transcript()
        t.read('/work/app/a.py')
        t.lines[0]['thinkingDurationMs'] = 'soon'
        t.read('/work/app/b.py')
        result = self.load(t)
        self.assertEqual(dict(result.unknown), {'assistant: unexpected shape': 1})
        self.assertEqual(self.count('reads'), 2)

    def test_local_commands_are_not_requests(self):
        t = Transcript()
        t.prompt('<command-name>/clear</command-name>', source='typed')
        self.load(t)
        self.assertEqual(self.one('SELECT source FROM prompts')[0], 'local-command')

    def test_second_run_adds_nothing_and_an_append_adds_only_the_new(self):
        t = Transcript()
        t.read('/work/app/a.py')
        path = t.write_to(self.root)
        self.assertEqual(ingest.ingest(self.conn, [self.root]).records, 2)
        self.assertEqual(ingest.ingest(self.conn, [self.root]).records, 0)
        more = Transcript()
        more.clock, more.calls = t.clock, t.calls
        more.read('/work/app/b.py')
        with open(path, 'a') as handle:
            handle.write(json.dumps(more.lines[0]) + '\n')
            handle.write(json.dumps(more.lines[1])[:40])  # a line still being written
        self.assertEqual(ingest.ingest(self.conn, [self.root]).records, 1)
        self.assertIsNone(self.one('SELECT ts_result FROM tool_calls ORDER BY ts_call DESC')[0])
        with open(path, 'a') as handle:
            handle.write(json.dumps(more.lines[1])[40:] + '\n')
        self.assertEqual(ingest.ingest(self.conn, [self.root]).records, 1)
        self.assertEqual(self.count('tool_calls'), 2)
        self.assertIsNotNone(self.one('SELECT ts_result FROM tool_calls ORDER BY ts_call DESC')[0])

    def test_credentials_are_scrubbed_before_storage(self):
        t = Transcript()
        credential = 'hunter' + '2' * 6
        t.bash(f'curl -H "Authorization: Bearer {credential}" https://example.test')
        t.prompt(f'use password={credential} for the fixture')
        self.load(t)
        stored = self.one('SELECT command FROM tool_calls')[0] + self.one('SELECT text FROM prompts')[0]
        self.assertNotIn(credential, stored)


class QueryTests(StoreCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, os.path.join(_ROOT, 'dev', 'observe', 'cli.py'),
                               '--db', self.db_path, *args], capture_output=True, text=True, check=False)

    def test_query_prints_rows_and_cannot_write(self):
        t = Transcript()
        t.read('/work/app/a.py')
        self.load(t)
        self.conn.close()
        answer = self.run_cli('query', 'SELECT tool, kind FROM tool_calls')
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertIn('Read', answer.stdout)
        refused = self.run_cli('query', 'DELETE FROM tool_calls')
        self.assertEqual(refused.returncode, 1)
        self.conn = db.connect(self.db_path)
        self.assertEqual(self.count('tool_calls'), 1)


class ReportTests(StoreCase):
    def build(self) -> None:
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py')
        for lines in (['-x = 1', '+x = 2'], ['-# old reason', '+# new reason']):
            t.call('Edit', {'file_path': '/work/app/svc.py'}, {'filePath': '/work/app/svc.py', 'structuredPatch': [
                {'oldStart': 3, 'oldLines': 1, 'newStart': 3, 'newLines': 1, 'lines': lines}]})
        self.load(t)
        episodes.derive(self.conn)

    def test_rewrites_are_reported_as_a_rate_over_the_hunks_edited(self):
        self.build()
        text = cli.report(self.conn)
        row = next(line.split() for line in text.splitlines() if line.startswith('code_small'))
        self.assertEqual(row[1:4], ['1', '50.0', '33.33'])   # 1 of 2 rewrites; 3 hunks edited
        self.assertIn('all rewrites', text)

    def test_by_project_report_names_each_project(self):
        self.build()
        text = cli.report(self.conn, by_project=True)
        self.assertIn('proj', text)
        self.assertIn('per 100 hunks edited', text)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_store_is_private_to_its_owner(self):
        path = os.path.join(self.tmp, 'state', 'observe.db')
        db.connect(path).close()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)

    def test_default_path_is_outside_any_repository(self):
        env = {'XDG_STATE_HOME': os.path.join(self.tmp, 'xdg')}
        saved = {key: os.environ.pop(key, None) for key in ('PLAYBOOK_OBSERVE_DB', 'XDG_STATE_HOME')}
        os.environ.update(env)
        try:
            self.assertEqual(db.default_path(), os.path.join(self.tmp, 'xdg', 'engineering-playbook', 'observe.db'))
        finally:
            os.environ.pop('XDG_STATE_HOME')
            os.environ.update({key: value for key, value in saved.items() if value is not None})

    def test_another_schema_version_is_refused(self):
        path = os.path.join(self.tmp, 'observe.db')
        conn = db.connect(path)
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(db.SchemaMismatch):
            db.connect(path)


TEST = 'python3 -m pytest -q'
FAIL = {'text': 'AssertionError: 1 != 2', 'exit_code': 1}


class FixLoopTests(StoreCase):
    def one_episode(self, t: Transcript):
        found = self.episodes(t)
        self.assertEqual(len(found), 1, [tuple(row) for row in found])
        return found[0]

    def test_fix_on_lines_the_model_wrote(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py', lines=30)
        t.bash(TEST, **FAIL)
        t.edit('/work/app/svc.py', at=10)
        t.bash(TEST)
        episode = self.one_episode(t)
        self.assertEqual((episode['kind'], episode['cause'], episode['target_read'], episode['resolved'],
                          episode['error_class'], episode['validation']),
                         ('fix_loop', 'own_lines', 'created', 1, 'assertion', 'python -m pytest'))

    def test_fix_elsewhere_in_a_file_it_edited(self):
        t = Transcript()
        t.prompt('build it')
        t.read('/work/app/svc.py', lines=50, total=400)
        t.edit('/work/app/svc.py', at=10)
        t.bash(TEST, **FAIL)
        t.edit('/work/app/svc.py', at=200)
        t.bash(TEST)
        episode = self.one_episode(t)
        self.assertEqual((episode['cause'], episode['target_read']), ('own_file_other_lines', 'partial'))

    def test_fix_in_a_file_it_had_only_read(self):
        t = Transcript()
        t.prompt('build it')
        t.read('/work/app/repo.py')
        t.create('/work/app/svc.py')
        t.bash(TEST, **FAIL)
        t.edit('/work/app/repo.py', at=5)
        t.bash(TEST)
        episode = self.one_episode(t)
        self.assertEqual((episode['cause'], episode['target_read']), ('untouched_file_read', 'full'))

    def test_fix_in_a_file_it_never_looked_at(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py')
        t.bash(TEST, **FAIL)
        t.edit('/work/app/consumer.py', at=5)
        t.edit('/work/app/svc.py', at=3)
        t.bash(TEST)
        episode = self.one_episode(t)
        self.assertEqual((episode['cause'], episode['target_read'], episode['fix_edits']),
                         ('untouched_file_unread', 'none', 2))
        self.assertEqual(json.loads(episode['details'])['scopes'],
                         {'untouched_file_unread': 1, 'own_lines': 1})

    def test_failure_read_from_piped_output(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py')
        t.bash(TEST + ' 2>&1 | tail -5', text='Ran 3 tests\n\nFAILED (failures=1)')
        t.edit('/work/app/svc.py', at=3)
        t.bash(TEST + ' 2>&1 | tail -5', text='Ran 3 tests\n\nOK')
        episode = self.one_episode(t)
        self.assertEqual(json.loads(episode['details'])['failure_seen_by'], 'output')
        self.assertEqual(episode['resolved'], 1)

    def test_every_failed_rerun_is_an_attempt(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py')
        t.bash(TEST, **FAIL)
        t.edit('/work/app/svc.py', at=3)
        t.bash(TEST, **FAIL)
        t.edit('/work/app/svc.py', at=3)
        t.bash(TEST)
        self.assertEqual(self.one_episode(t)['attempts'], 2)

    def test_unresolved_when_the_request_ends(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py')
        t.bash(TEST, **FAIL)
        t.edit('/work/app/svc.py', at=3)
        t.prompt('never mind, do the other thing')
        self.assertEqual(self.one_episode(t)['resolved'], 0)

    def test_a_failure_nobody_edited_after_is_not_rework(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py')
        t.bash(TEST, **FAIL)
        t.bash(TEST)
        self.assertEqual(self.episodes(t), [])

    def test_a_denied_validation_is_not_a_failure(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/svc.py')
        t.call('Bash', {'command': TEST}, 'Error: hook', text='hook error', is_error=True,
               toolDenialKind='permission-rule')
        t.edit('/work/app/svc.py', at=3)
        t.bash(TEST)
        self.assertEqual([row['kind'] for row in self.episodes(t)], ['re_edit'])

    def test_scratch_files_are_not_rework(self):
        t = Transcript()
        t.prompt('build it')
        t.bash(TEST, **FAIL)
        t.edit('/tmp/notes.py', at=3)
        t.bash(TEST)
        self.assertEqual(self.episodes(t), [])


class ReEditTests(StoreCase):
    def test_rewriting_own_lines_in_the_same_request(self):
        t = Transcript()
        t.prompt('build it')
        t.read('/work/app/svc.py')
        t.edit('/work/app/svc.py', at=10, old=3, new=6)
        t.edit('/work/app/svc.py', at=12, old=2, new=2)
        t.edit('/work/app/svc.py', at=11, old=1, new=1)
        found = self.episodes(t)
        self.assertEqual([(row['kind'], row['cause'], row['fix_edits'], row['target_read']) for row in found],
                         [('re_edit', 'rewrite_without_failure', 2, 'full')])

    def test_every_edited_file_is_counted_with_what_was_read_first(self):
        t = Transcript()
        t.prompt('build it')
        t.read('/work/app/whole.py')
        t.edit('/work/app/whole.py', at=10)
        t.read('/work/app/part.py', lines=30, total=900)
        t.edit('/work/app/part.py', at=10)
        t.edit('/work/app/part.py', at=10)
        t.edit('/work/app/blind.py', at=10)
        t.create('/work/app/new.py')
        self.episodes(t)
        rows = {os.path.basename(row['path']): (row['target_read'], row['hunks'], row['rewritten_hunks'])
                for row in self.conn.execute('SELECT * FROM edited_files')}
        self.assertEqual(rows, {'whole.py': ('full', 1, 0), 'part.py': ('partial', 2, 1),
                                'blind.py': ('none', 1, 0), 'new.py': ('created', 1, 0)})

    def test_each_rewrite_records_what_came_between(self):
        t = Transcript()
        t.prompt('build it')
        t.create('/work/app/a.py')
        t.edit('/work/app/a.py', at=3)                       # straight after the write
        t.read('/work/app/b.py')
        t.edit('/work/app/a.py', at=3)                       # after only a read
        t.bash('ls src')
        t.bash(TEST, text='AssertionError', exit_code=1)
        t.read('/work/app/b.py')
        t.edit('/work/app/a.py', at=3)                       # the failed check outranks the rest
        t.bash(TEST)
        t.edit('/work/app/a.py', at=3)                       # after a check that passed
        self.load(t)
        episodes.derive(self.conn)
        triggers = [row['trigger'] for row in self.conn.execute(
            'SELECT r.trigger FROM rewrites r JOIN tool_calls c USING (tool_use_id) ORDER BY c.ts_call')]
        self.assertEqual(triggers, ['nothing', 'read', 'check_failed', 'check_passed'])

    def test_a_later_request_changing_the_same_lines_is_new_work(self):
        t = Transcript()
        t.prompt('build it')
        t.edit('/work/app/svc.py', at=10)
        t.prompt('now make it async')
        t.edit('/work/app/svc.py', at=10)
        self.assertEqual(self.episodes(t), [])

    def test_lines_moved_by_an_edit_above_are_still_the_models(self):
        t = Transcript()
        t.prompt('build it')
        t.edit('/work/app/svc.py', at=100, old=3, new=3)     # writes 100-102
        t.edit('/work/app/svc.py', at=10, old=1, new=21)     # pushes them to 120-122
        t.edit('/work/app/svc.py', at=121, old=1, new=1)     # rewrites one of them
        t.edit('/work/app/svc.py', at=101, old=1, new=1)     # where they used to be: not the model's
        found = self.episodes(t)
        self.assertEqual([(row['kind'], row['fix_edits']) for row in found], [('re_edit', 1)])

    def test_hunks_of_one_edit_are_not_rewrites_of_each_other(self):
        t = Transcript()
        t.prompt('build it')
        t.call('Edit', {'file_path': '/work/app/svc.py'}, {'filePath': '/work/app/svc.py', 'structuredPatch': [
            {'oldStart': 10, 'oldLines': 1, 'newStart': 10, 'newLines': 30, 'lines': []},
            {'oldStart': 20, 'oldLines': 2, 'newStart': 49, 'newLines': 2, 'lines': []}]})
        t.edit('/work/app/svc.py', at=200)                   # far from both: no rewrite
        t.edit('/work/app/svc.py', at=50, old=1, new=1)      # inside the second hunk as written
        self.episodes(t)
        self.assertEqual(self.one('SELECT hunks, rewritten_hunks FROM edited_files')[:], (4, 1))

    def test_prose_is_rewritten_by_design(self):
        t = Transcript()
        t.prompt('write the notes')
        t.edit('/work/app/NOTES.md', at=10)
        t.edit('/work/app/NOTES.md', at=10)
        self.assertEqual(self.episodes(t), [])


class PackagingTests(unittest.TestCase):
    """The tool is for developing the playbook: nothing shipped may reach it."""

    def test_no_manifest_or_hook_registration_names_it(self):
        for name in ('.claude-plugin/plugin.json', '.claude-plugin/marketplace.json',
                     '.codex-plugin/plugin.json', '.codex-plugin/hooks.json', 'hooks/hooks.json', 'install.sh'):
            with self.subTest(name=name), open(os.path.join(_ROOT, name)) as handle:
                self.assertNotIn('dev/observe', handle.read())

    def test_release_archive_leaves_it_out(self):
        if subprocess.run(['git', 'rev-parse', '--git-dir'], cwd=_ROOT, capture_output=True, check=False).returncode:
            self.skipTest('not a git checkout')
        listing = subprocess.run(
            'git archive --worktree-attributes --format=tar HEAD | tar -t', shell=True, cwd=_ROOT,
            capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertIn('hooks/hooks.json', listing)
        self.assertEqual([name for name in listing if name.startswith('dev/')], [])


if __name__ == '__main__':
    unittest.main()
