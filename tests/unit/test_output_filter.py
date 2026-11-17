"""
Unit tests for hooks/output_filter.py: which commands are filtered, the grep
reduction, and the hook contract (shape kept, stderr untouched, off while
suspended, silent on bad input).

The hook runs as a subprocess with HOME pointed at a temporary directory, so
the real session state is never read.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK = os.path.join(_ROOT, 'hooks', 'output_filter.py')
_spec = importlib.util.spec_from_file_location('output_filter', _HOOK)
of = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(of)

sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402


class _RepoCase(unittest.TestCase):
    """A temporary tree with two source files that grep output can name."""

    def setUp(self):
        self.cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cwd)
        os.makedirs(os.path.join(self.cwd, 'src', 'orders'))
        for name in ('src/orders/order-service.ts', 'src/orders/order-repository.ts'):
            open(os.path.join(self.cwd, name), 'w').close()

    def grep_output(self, per_file: int = 30, text: str = '  const total = order.amount;') -> str:
        lines = []
        for path in ('src/orders/order-service.ts', 'src/orders/order-repository.ts'):
            lines += [f'{path}:{n}:{text}' for n in range(1, per_file + 1)]
        return '\n'.join(lines) + '\n'

    def response(self, stdout: str, **extra) -> dict:
        return {'stdout': stdout, 'stderr': '', 'interrupted': False, 'isImage': False,
                'noOutputExpected': False, **extra}


class TestOutputCommand(_RepoCase):

    def test_producer_and_workdir(self):
        self.assertEqual(of.output_command('grep -rn x src', self.cwd), (['grep', '-rn', 'x', 'src'], self.cwd))
        words, workdir = of.output_command('cd src && rg -n x', self.cwd)
        self.assertEqual((words[0], workdir), ('rg', os.path.join(self.cwd, 'src')))

    def test_line_cutters_and_stderr_routing_are_allowed(self):
        for command in ('grep -rn x src | head -50', 'grep -rn x src 2>&1 | tail -n 5',
                        'grep -rn x src 2>/dev/null', 'sudo grep -rn x src'):
            with self.subTest(command=command):
                self.assertEqual(of.output_command(command, self.cwd)[0][0], 'grep')

    def test_anything_else_passes_through(self):
        for command in ('PLAYBOOK_RAW=1 grep -rn x src', 'export PLAYBOOK_RAW=1; grep -rn x .',
                        'grep -rn x src | wc -l', 'grep -rn x src | sort', 'grep a x; grep b y',
                        'grep -rn x $(pwd)', 'grep -rn x `pwd`', 'grep -rn x src > out.txt',
                        'grep -rn x src >out.txt', 'cat log | grep x', 'cd', ''):
            with self.subTest(command=command):
                self.assertIsNone(of.output_command(command, self.cwd))


class TestGrepReduction(_RepoCase):

    def test_grouped_by_file_and_capped(self):
        out = of.filter_output('grep -rn total src', self.response(self.grep_output()), self.cwd)
        self.assertIsNotNone(out)
        self.assertIn('src/orders/order-service.ts (30)\n  1:  const total', out)
        self.assertIn('... 10 more in this file', out)
        self.assertIn('20 over the per-file cap omitted', out)
        self.assertIn('PLAYBOOK_RAW=1', out.splitlines()[-1])
        self.assertLess(len(out), len(self.grep_output()) * (1 - of.MIN_GAIN))

    def test_long_lines_are_clipped(self):
        stdout = self.grep_output(per_file=3, text='x' * 2000)
        out = of.filter_output('rg -n x', self.response(stdout), self.cwd)
        self.assertIn(f'...[+{2000 - of.GREP_MAX_LINE + 2} chars]', out)

    def test_git_grep_is_grep(self):
        self.assertIsNotNone(of.filter_output('git grep -n total', self.response(self.grep_output()), self.cwd))

    def test_output_that_does_not_read_as_path_lines_passes_through(self):
        cases = {
            'unknown path': self.grep_output().replace('src/orders/', 'nowhere/'),
            'single file, no path prefix': '\n'.join(f'key{n}: value' for n in range(200)),
            'files only (-l)': '\n'.join(['src/orders/order-service.ts'] * 100),
            'merged error line': self.grep_output() + 'grep: missing: No such file or directory\n',
        }
        for name, stdout in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(of.filter_output('grep -rn total src', self.response(stdout), self.cwd))

    def test_context_options_pass_through(self):
        for option in ('-C3', '-A', '-rnB2', '--context=2', '--after-context=1'):
            with self.subTest(option=option):
                self.assertIsNone(of.filter_output(f'grep {option} total src',
                                                   self.response(self.grep_output()), self.cwd))

    def test_workdir_from_cd_resolves_the_paths(self):
        stdout = self.grep_output().replace('src/orders/', 'orders/')
        self.assertIsNotNone(of.filter_output('cd src && grep -rn total .', self.response(stdout), self.cwd))
        self.assertIsNone(of.filter_output('grep -rn total .', self.response(stdout), self.cwd))


def _ls_output(entries: int = 30, owner: str = 'dev dev', date: str = 'Nov 16 15:43') -> str:
    lines = [f'total {entries * 8}',
             f'drwxrwxr-x  4 {owner}  4096 {date} .',
             f'drwxrwxr-x 17 {owner}  4096 {date} ..']
    lines += [f'-rw-rw-r--  1 {owner} {1000 + n:>5} {date} module_{n}.py' for n in range(entries)]
    lines.append(f'lrwxrwxrwx  1 {owner}    11 {date} current -> module_0.py')
    return '\n'.join(lines) + '\n'


class TestLsReduction(_RepoCase):

    def test_columns_dropped_and_shared_owner_noted(self):
        out = of.filter_output('ls -la', self.response(_ls_output()), self.cwd)
        self.assertIn('-rw-rw-r-- 1000 Nov 16 15:43 module_0.py\n', out)
        self.assertIn('lrwxrwxrwx 11 Nov 16 15:43 current -> module_0.py', out)
        self.assertNotIn('total ', out)
        self.assertNotIn(' .\n', out)
        self.assertIn('owner:group dev:dev on every entry', out.splitlines()[-1])

    def test_mixed_owners_stay_on_each_line(self):
        stdout = _ls_output() + '-rw-r--r--  1 root root 5000 Nov 16 15:43 root_file\n'
        out, summary = of.reduce_ls(['ls', '-la'], stdout, self.cwd)  # gain too small to apply
        self.assertNotIn('on every entry', summary)
        self.assertIn('root:root -rw-r--r-- 5000 Nov 16 15:43 root_file', out)
        self.assertIn('dev:dev -rw-rw-r-- 1000', out)

    def test_locales_time_styles_and_recursive_headers(self):
        for date in ('nov 16 15:43', '16 nov 15:43', '2026-11-16 15:43', 'Nov 16  2025'):
            with self.subTest(date=date):
                self.assertIsNotNone(of.filter_output('ls -la', self.response(_ls_output(date=date)), self.cwd))
        recursive = 'src:\n' + _ls_output() + '\nsrc/orders:\n' + _ls_output()
        out = of.filter_output('ls -laR src', self.response(recursive), self.cwd)
        self.assertIn('src/orders:', out)

    def test_other_shapes_pass_through(self):
        big = _ls_output()
        for command in ('ls', 'ls -a', 'ls -lai', 'ls -ls', 'ls -lo', 'ls -lg', 'ls -l --format=long'):
            with self.subTest(command=command):
                self.assertIsNone(of.filter_output(command, self.response(big), self.cwd))
        self.assertIsNone(of.filter_output('ls -la', self.response(big + 'garbage line\n'), self.cwd))


def _sha(n: int) -> str:
    return f'{n:x}'.ljust(40, 'a')


def _git_log_output(commits: int = 12, body_lines: int = 3) -> str:
    out = []
    for n in range(commits):
        decor = ' (HEAD -> main, origin/main)' if n == 0 else ''
        out.append(f'commit {_sha(n)}{decor}')
        if n == 1:
            out.append('Merge: abc1234 def5678')
        out += ['Author: Dev Person <dev.person@example.com>',
                'Date:   Mon Nov 16 15:39:20 2026 -0300', '',
                f'    feat(orders): change number {n}', '    ']
        out += [f'    body line {k} of commit {n}' for k in range(body_lines)]
        out.append('')
    return '\n'.join(out)


class TestGitLogReduction(_RepoCase):

    def test_one_header_line_per_commit_with_body(self):
        out = of.filter_output('git log -n 12', self.response(_git_log_output()), self.cwd)
        self.assertIn(f'{_sha(0)[:10]} 2026-11-16 Dev Person (HEAD -> main, origin/main): feat(orders): change number 0', out)
        self.assertIn(f'{_sha(1)[:10]} 2026-11-16 Dev Person [merge]: feat(orders): change number 1', out)
        self.assertIn('    body line 2 of commit 3', out)
        self.assertNotIn('example.com', out)

    def test_long_bodies_are_cut(self):
        out = of.filter_output('git log', self.response(_git_log_output(commits=4, body_lines=20)), self.cwd)
        self.assertIn(f'    ... {20 - of.GIT_LOG_MAX_BODY} more lines', out)
        self.assertIn('4 long bodies cut', out)

    def test_other_formats_pass_through(self):
        big = self.response(_git_log_output())
        for command in ('git log --oneline', 'git log --stat', 'git log -p', 'git log --graph',
                        'git log --format=%H', 'git -C x log', 'git show'):
            with self.subTest(command=command):
                self.assertIsNone(of.filter_output(command, big, self.cwd))
        for command in ('git log -5', 'git log --author dev main..HEAD -- src', 'git log --since=2.weeks --no-merges'):
            with self.subTest(command=command):
                self.assertIsNotNone(of.filter_output(command, big, self.cwd))
        self.assertIsNone(of.filter_output('git log', self.response('noise\n' + _git_log_output()), self.cwd))


class TestSkipRules(_RepoCase):

    def test_outputs_the_filter_must_leave_alone(self):
        big = self.grep_output()
        cases = {
            'persisted by Claude Code': self.response(big, persistedOutputPath='/x', persistedOutputSize=9),
            'interrupted': self.response(big, interrupted=True),
            'image': self.response(big, isImage=True),
            'small': self.response(self.grep_output(per_file=2)),
            'not a dict': big,
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(of.filter_output('grep -rn total src', response, self.cwd))

    def test_too_little_gain_passes_through(self):
        # One match per file, short paths: grouping saves almost nothing.
        os.makedirs(os.path.join(self.cwd, 'f'))
        lines = []
        for n in range(80):
            open(os.path.join(self.cwd, 'f', str(n)), 'w').close()
            lines.append(f'f/{n}:1:a match line of moderate length here')
        self.assertIsNone(of.filter_output('grep -rn match f', self.response('\n'.join(lines)), self.cwd))

    def test_unknown_producer_passes_through(self):
        self.assertIsNone(of.filter_output('ls -la', self.response(self.grep_output()), self.cwd))


class TestHookContract(_RepoCase):

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)

    def run_hook(self, payload) -> subprocess.CompletedProcess:
        env = {**os.environ, 'HOME': self.home}
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, _HOOK], input=text, capture_output=True,
                              text=True, timeout=10, env=env)

    def payload(self, **extra) -> dict:
        return {'hook_event_name': 'PostToolUse', 'session_id': 'filter-test', 'cwd': self.cwd,
                'tool_name': 'Bash', 'tool_input': {'command': 'grep -rn total src'},
                'tool_response': self.response(self.grep_output(), stderr='warning: x'), **extra}

    def test_rewrites_stdout_and_keeps_the_rest(self):
        result = self.run_hook(self.payload())
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'PostToolUse')
        updated = out['updatedToolOutput']
        self.assertEqual(set(updated), set(self.payload()['tool_response']))
        self.assertEqual(updated['stderr'], 'warning: x')
        self.assertIn('[playbook:filter]', updated['stdout'])

    def test_a_command_another_hook_rewrote_passes_through(self):
        # A PreToolUse hook of another tool that rewrites `grep ...` into its
        # own program: the producer is that program, which no rule knows.
        payload = self.payload(tool_input={'command': 'othertool grep -rn total src'})
        self.assertEqual(self.run_hook(payload).stdout, '')

    def test_off_while_the_playbook_is_suspended(self):
        state_dir = os.path.join(self.home, '.claude', 'hooks')
        os.makedirs(state_dir)
        playbook_core.save_state(playbook_core.state_path(state_dir, 'filter-test'), {'suspended': True})
        self.assertEqual(self.run_hook(self.payload()).stdout, '')

    def test_ledger_counts_what_the_model_would_have_read(self):
        before = len(self.payload()['tool_response']['stdout'])
        self.run_hook(self.payload())
        out = json.loads(self.run_hook(self.payload()).stdout)
        after = len(out['hookSpecificOutput']['updatedToolOutput']['stdout'])
        report = subprocess.run([sys.executable, _HOOK, '--report'], capture_output=True, text=True,
                                timeout=10, cwd=self.cwd, env={**os.environ, 'HOME': self.home}).stdout
        self.assertIn(f'2 outputs filtered, {2 * before:,} -> {2 * after:,} chars', report)

    def test_report_before_anything_is_filtered(self):
        report = subprocess.run([sys.executable, _HOOK, '--report'], capture_output=True, text=True,
                                timeout=10, cwd=self.cwd, env={**os.environ, 'HOME': self.home}).stdout
        self.assertIn('nothing filtered yet', report)

    def test_old_ledgers_are_removed_when_a_session_starts(self):
        state_dir = os.path.join(self.home, '.claude', 'hooks')
        os.makedirs(state_dir)
        old = os.path.join(state_dir, '.filter-old-session.log')
        with open(old, 'w') as fh:
            fh.write('100 50\n')
        os.utime(old, (0, 0))
        self.run_hook(self.payload())
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(os.path.join(state_dir, '.filter-filter-test.log')))

    def test_silent_on_other_tools_and_bad_input(self):
        for payload in ('not json', '[]', self.payload(tool_name='Read'),
                        self.payload(tool_response='text'), self.payload(tool_input='x')):
            with self.subTest(payload=str(payload)[:40]):
                result = self.run_hook(payload)
                self.assertEqual((result.returncode, result.stdout), (0, ''))


if __name__ == '__main__':
    unittest.main()
