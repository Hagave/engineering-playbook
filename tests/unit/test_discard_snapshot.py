"""
A discard of uncommitted tracked changes is made recoverable instead of asked
about: `snapshot_before_discard` keeps them under refs/playbook/snapshots/ and
the hook asks only for what is still destructive. Real git repositories, real
`git stash apply` as the proof that the work comes back.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK = os.path.join(_ROOT, 'hooks', 'context_guard.py')

sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core

spec = importlib.util.spec_from_file_location('context_guard', _HOOK)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

_GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}


def git(repo: str, *args: str) -> str:
    done = subprocess.run(('git', '-C', repo) + args, capture_output=True, text=True,
                          env=_GIT_ENV, check=True)
    return done.stdout


def make_repo(parent: str, name: str) -> str:
    repo = os.path.join(parent, name)
    os.makedirs(repo)
    git(repo, 'init', '-q')
    for path, text in (('a.txt', 'a\n'), ('b.txt', 'b\n')):
        with open(os.path.join(repo, path), 'w') as handle:
            handle.write(text)
    git(repo, 'add', '.')
    git(repo, 'commit', '-q', '-m', 'init')
    return repo


def write(repo: str, path: str, text: str) -> None:
    with open(os.path.join(repo, path), 'w') as handle:
        handle.write(text)


def read(repo: str, path: str) -> str:
    with open(os.path.join(repo, path)) as handle:
        return handle.read()


class SnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = unittest.mock.patch.dict(os.environ, _GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = make_repo(self.tmp, 'repo')


class TestSnapshotBeforeDiscard(SnapshotTestCase):

    def test_checkout_discard_is_saved_and_comes_back_with_stash_apply(self) -> None:
        write(self.repo, 'a.txt', 'my work\n')
        snapshots = playbook_core.snapshot_before_discard('git checkout -- a.txt', self.repo)
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0].ref.startswith('refs/playbook/snapshots/'))
        git(self.repo, 'checkout', '--', 'a.txt')
        self.assertEqual(read(self.repo, 'a.txt'), 'a\n')
        git(self.repo, 'stash', 'apply', snapshots[0].sha)
        self.assertEqual(read(self.repo, 'a.txt'), 'my work\n')

    def test_snapshot_does_not_move_the_working_tree_or_index(self) -> None:
        write(self.repo, 'a.txt', 'staged\n')
        git(self.repo, 'add', 'a.txt')
        write(self.repo, 'b.txt', 'unstaged\n')
        before = git(self.repo, 'status', '--porcelain')
        playbook_core.snapshot_before_discard('git restore b.txt', self.repo)
        self.assertEqual(git(self.repo, 'status', '--porcelain'), before)
        self.assertEqual(read(self.repo, 'b.txt'), 'unstaged\n')
        self.assertEqual(git(self.repo, 'stash', 'list'), '')

    def test_staged_and_unstaged_changes_are_both_kept(self) -> None:
        write(self.repo, 'a.txt', 'staged\n')
        git(self.repo, 'add', 'a.txt')
        write(self.repo, 'b.txt', 'unstaged\n')
        snapshots = playbook_core.snapshot_before_discard('git reset --hard', self.repo)
        git(self.repo, 'reset', '--hard')
        git(self.repo, 'stash', 'apply', snapshots[0].sha)
        self.assertEqual(read(self.repo, 'a.txt'), 'staged\n')
        self.assertEqual(read(self.repo, 'b.txt'), 'unstaged\n')

    def test_a_clean_repository_has_nothing_to_lose(self) -> None:
        self.assertEqual(playbook_core.snapshot_before_discard('git checkout -- a.txt', self.repo), [])
        self.assertEqual(git(self.repo, 'for-each-ref', 'refs/playbook/'), '')

    def test_untracked_files_are_neither_kept_nor_touched(self) -> None:
        write(self.repo, 'new.txt', 'untracked\n')
        self.assertEqual(playbook_core.snapshot_before_discard('git checkout -- a.txt', self.repo), [])
        self.assertEqual(read(self.repo, 'new.txt'), 'untracked\n')

    def test_each_form_of_discard_is_read(self) -> None:
        for command in ('git checkout -- a.txt', 'git restore a.txt', 'git restore --worktree a.txt',
                        'git restore -W a.txt', 'git reset --hard', 'git reset --hard HEAD',
                        'git -c core.pager=cat restore a.txt', 'sh -c "git checkout -- a.txt"',
                        'cd {repo} && git restore a.txt', 'git checkout HEAD -- a.txt',
                        'git checkout main -- a.txt', 'git checkout .', 'git checkout -f',
                        'git switch --discard-changes main', 'echo "git checkout HEAD -- a.txt" | sh',
                        'sh <<< "git checkout HEAD -- a.txt"'):
            with self.subTest(command=command):
                write(self.repo, 'a.txt', 'work\n')
                snapshots = playbook_core.snapshot_before_discard(
                    command.format(repo=self.repo), self.repo)
                self.assertEqual(len(snapshots), 1)
                git(self.repo, 'checkout', '--', 'a.txt')

    def test_a_repository_named_by_dash_C_is_the_one_saved(self) -> None:
        other = make_repo(self.tmp, 'other')
        write(other, 'a.txt', 'other work\n')
        snapshots = playbook_core.snapshot_before_discard(f'git -C {other} checkout -- a.txt', self.repo)
        self.assertEqual([s.repo for s in snapshots], [os.path.realpath(other)])

    def test_a_repository_named_by_a_variable_is_resolved(self) -> None:
        other = make_repo(self.tmp, 'other')
        write(other, 'a.txt', 'other work\n')
        command = f'C={other}; P=src; git -C $C checkout -- $P/a.txt'
        snapshots = playbook_core.snapshot_before_discard(command, self.repo)
        self.assertEqual([s.repo for s in snapshots], [os.path.realpath(other)])

    def test_two_repositories_are_each_saved(self) -> None:
        other = make_repo(self.tmp, 'other')
        write(self.repo, 'a.txt', 'one\n')
        write(other, 'a.txt', 'two\n')
        command = f'git -C {self.repo} checkout -- a.txt; git -C {other} checkout -- a.txt'
        self.assertEqual(len(playbook_core.snapshot_before_discard(command, self.tmp)), 2)

    def test_a_repository_decided_at_run_time_is_not_snapshotted(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        for command in ('git -C "$(pwd)" checkout -- a.txt',
                        'git -C $UNKNOWN checkout -- a.txt',
                        'C=/one; C=/two; git -C $C checkout -- a.txt',
                        f'cd "$(echo {self.repo})" && git checkout -- a.txt',
                        'find . -execdir git checkout -- a.txt \\;',
                        'git --git-dir=.git checkout -- a.txt'):
            with self.subTest(command=command):
                self.assertIsNone(playbook_core.snapshot_before_discard(command, self.repo))
        self.assertEqual(git(self.repo, 'for-each-ref', 'refs/playbook/'), '')

    def test_a_reset_to_another_commit_is_not_snapshotted(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        for command in ('git reset --hard HEAD~1', 'git reset --hard origin/main',
                        'git reset --hard abc1234', 'git reset --hard HEAD@{1}',
                        'git reset --hard -q main'):
            with self.subTest(command=command):
                self.assertIsNone(playbook_core.snapshot_before_discard(command, self.repo))
        self.assertEqual(git(self.repo, 'for-each-ref', 'refs/playbook/'), '')

    def test_a_reset_to_head_is_snapshotted(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        for command in ('git reset --hard', 'git reset --hard HEAD', 'git reset --hard --quiet HEAD'):
            with self.subTest(command=command):
                self.assertEqual(len(playbook_core.snapshot_before_discard(command, self.repo)), 1)

    def test_a_directory_that_is_not_a_repository_is_not_snapshotted(self) -> None:
        plain = os.path.join(self.tmp, 'plain')
        os.makedirs(plain)
        self.assertIsNone(playbook_core.snapshot_before_discard('git checkout -- a.txt', plain))

    def test_a_repository_with_no_commit_is_not_snapshotted(self) -> None:
        empty = os.path.join(self.tmp, 'empty')
        os.makedirs(empty)
        git(empty, 'init', '-q')
        self.assertIsNone(playbook_core.snapshot_before_discard('git reset --hard', empty))

    def test_more_repositories_than_the_cap_is_not_snapshotted(self) -> None:
        repos = [make_repo(self.tmp, f'r{n}') for n in range(playbook_core._SNAPSHOT_REPOS_MAX + 1)]
        command = '; '.join(f'git -C {r} checkout -- a.txt' for r in repos)
        self.assertIsNone(playbook_core.snapshot_before_discard(command, self.tmp))

    def test_only_the_newest_snapshots_are_kept(self) -> None:
        kept = playbook_core._SNAPSHOT_KEPT
        with unittest.mock.patch.object(playbook_core.time, 'time', side_effect=iter(
                range(1_700_000_000, 1_700_000_000 + kept + 5))):
            for n in range(kept + 5):
                write(self.repo, 'a.txt', f'v{n}\n')
                playbook_core.snapshot_before_discard('git checkout -- a.txt', self.repo)
        refs = git(self.repo, 'for-each-ref', '--format=%(refname)', 'refs/playbook/snapshots/').split()
        self.assertEqual(len(refs), kept)
        stamps = sorted(int(ref.rsplit('/', 1)[1].split('-')[0]) for ref in refs)
        self.assertEqual(stamps[0], (1_700_000_000 + 5) * 1000)
        self.assertEqual(stamps[-1], (1_700_000_000 + kept + 4) * 1000)

    def test_a_snapshot_never_overwrites_one_with_the_same_name(self) -> None:
        write(self.repo, 'a.txt', 'first\n')
        with unittest.mock.patch.object(playbook_core.time, 'time', return_value=1_700_000_000):
            first = playbook_core.snapshot_before_discard('git checkout -- a.txt', self.repo)
            write(self.repo, 'a.txt', 'second\n')
            second = playbook_core.snapshot_before_discard('git checkout -- a.txt', self.repo)
        self.assertIsNone(second)
        ref = git(self.repo, 'for-each-ref', '--format=%(objectname)', 'refs/playbook/snapshots/')
        self.assertEqual(ref.strip(), first[0].sha)


class TestRecoverDiscards(SnapshotTestCase):

    def _recover(self, command: str):
        with unittest.mock.patch.object(cg, '_CWD', self.repo):
            return cg._recover_discards(command, command, {'language': 'en'})

    def test_a_pure_discard_asks_nothing_and_tells_the_model_where_it_is(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        verdict, notice = self._recover('git checkout -- a.txt')
        self.assertIsNone(verdict)
        self.assertIn('stash apply', notice['msg'])
        self.assertTrue(notice['to_model'])
        sha = git(self.repo, 'for-each-ref', '--format=%(objectname)', 'refs/playbook/snapshots/').strip()
        self.assertIn(sha[:12], notice['msg'])

    def test_a_clean_discard_asks_nothing_and_says_nothing(self) -> None:
        self.assertEqual(self._recover('git checkout -- a.txt'), (None, None))

    def test_another_destructive_part_keeps_the_question_and_saves_nothing(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        for tail in ('rm -rf build', 'git clean -fd', 'git push --force', 'git branch -D x'):
            with self.subTest(tail=tail):
                verdict, notice = self._recover(f'git checkout -- a.txt && {tail}')
                self.assertIsNotNone(verdict)
                self.assertNotEqual(verdict['effect'], 'discard_changes')
                self.assertIsNone(notice)
        self.assertEqual(git(self.repo, 'for-each-ref', 'refs/playbook/'), '')

    def test_a_script_the_command_runs_is_still_read_in_full(self) -> None:
        script = os.path.join(self.repo, 'wipe.sh')
        write(self.repo, 'wipe.sh', '#!/bin/sh\nrm -rf build\n')
        os.chmod(script, 0o755)
        write(self.repo, 'a.txt', 'work\n')
        verdict, notice = self._recover('git checkout -- a.txt; ./wipe.sh')
        self.assertEqual(verdict['effect'], 'delete_tree')
        self.assertIsNone(notice)

    def test_an_unsnapshottable_discard_keeps_its_original_question(self) -> None:
        verdict, notice = self._recover('git -C "$(pwd)" checkout -- a.txt')
        self.assertEqual(verdict['effect'], 'discard_changes')
        self.assertIsNone(notice)

    def test_the_notice_follows_the_language_of_the_user(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        with unittest.mock.patch.object(cg, '_CWD', self.repo):
            _, notice = cg._recover_discards('git restore a.txt', 'git restore a.txt',
                                             {'language': 'pt-br'})
        self.assertIn('salvas', notice['msg'])


class TestHookEndToEnd(SnapshotTestCase):
    """The real hook process, as Claude Code calls it."""

    def _pretool(self, command: str, mode: str = 'default') -> dict:
        home = os.path.join(self.tmp, 'home')
        os.makedirs(home, exist_ok=True)
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': self.repo,
                   'session_id': 'snapshot-test', 'permission_mode': mode,
                   'hook_event_name': 'PreToolUse'}
        done = subprocess.run([sys.executable, '-I', _HOOK, 'pretool'], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30, check=False,
                              env={**_GIT_ENV, 'HOME': home})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else {}

    def test_a_discard_of_changes_no_longer_reaches_the_user_as_a_question(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        for mode in ('default', 'auto'):
            with self.subTest(mode=mode):
                out = self._pretool('git checkout -- a.txt', mode)
                decision = out.get('hookSpecificOutput', {}).get('permissionDecision')
                self.assertNotIn(decision, ('ask', 'deny'))
                self.assertIn('stash apply', out['hookSpecificOutput']['additionalContext'])

    def test_a_discard_beside_a_clean_is_still_asked(self) -> None:
        write(self.repo, 'a.txt', 'work\n')
        out = self._pretool('git checkout -- a.txt && git clean -fd')
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'ask')

    def test_a_clean_alone_is_still_asked(self) -> None:
        out = self._pretool('git clean -fd')
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'ask')


if __name__ == '__main__':
    unittest.main()
