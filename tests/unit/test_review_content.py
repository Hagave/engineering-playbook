"""
Unit tests for the review gate's content credit in hooks/playbook_core.py:
which `git diff` forms print the working tree, and the snapshot of the working
tree a review read. The snapshot runs real git on a temporary repository.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core


class TestReadsWorkingTree(unittest.TestCase):

    def test_forms_that_print_the_working_tree(self):
        for args in ([], ['HEAD'], ['--stat'], ['--', 'src'], ['--stat', '--', 'src/a.ts'],
                     ['HEAD', '--', 'src'], ['origin/main'], ['-U5', 'HEAD']):
            with self.subTest(args=args):
                self.assertTrue(playbook_core.reads_working_tree(args))

    def test_forms_that_print_commits_or_the_index_only(self):
        for args in (['--cached'], ['--staged', 'HEAD'], ['origin/main...HEAD'], ['A..B'],
                     ['HEAD~1', 'HEAD'], ['--no-index', 'a', 'b'], ['a.ts', 'b.ts']):
            with self.subTest(args=args):
                self.assertFalse(playbook_core.reads_working_tree(args))


class TestWorkingTreeBlobs(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='pb_tree_')
        self._git('init', '-q', '-b', 'main')
        self._git('config', 'user.email', 'test@example.com')
        self._git('config', 'user.name', 'test')
        self._put('kept.ts', 'base')
        self._put('gone.ts', 'base')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'base')

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _git(self, *args: str) -> str:
        return subprocess.run(('git', '-C', self.repo) + args, capture_output=True,
                              text=True, check=True).stdout.strip()

    def _put(self, name: str, text: str) -> None:
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)

    def test_modified_untracked_and_deleted_paths_are_listed(self):
        self._put('kept.ts', 'changed')
        self._put('src/new.ts', 'untracked')
        os.remove(os.path.join(self.repo, 'gone.ts'))
        blobs = playbook_core.working_tree_blobs(self.repo)
        self.assertEqual(blobs['kept.ts'], self._git('hash-object', 'kept.ts'))
        self.assertEqual(blobs['src/new.ts'], self._git('hash-object', 'src/new.ts'))
        self.assertEqual(blobs['gone.ts'], '')

    def test_hashing_writes_nothing_to_the_object_store(self):
        self._put('src/new.ts', 'untracked')
        blob = playbook_core.working_tree_blobs(self.repo)['src/new.ts']
        missing = subprocess.run(('git', '-C', self.repo, 'cat-file', '-e', blob),
                                 capture_output=True, check=False)
        self.assertNotEqual(missing.returncode, 0)

    def test_ignored_files_are_left_out(self):
        self._put('.gitignore', 'build/\n')
        self._put('build/out.js', 'generated')
        self.assertNotIn('build/out.js', playbook_core.working_tree_blobs(self.repo))

    def test_a_symlink_is_never_matched(self):
        os.symlink('kept.ts', os.path.join(self.repo, 'link.ts'))
        self.assertEqual(playbook_core.working_tree_blobs(self.repo)['link.ts'], '?')

    def test_too_many_changed_paths_give_no_snapshot(self):
        self._put('a.ts', '1')
        self._put('b.ts', '2')
        with mock.patch.object(playbook_core, 'SNAPSHOT_MAX_PATHS', 1):
            self.assertIsNone(playbook_core.working_tree_blobs(self.repo))

    def test_a_clean_tree_is_an_empty_snapshot(self):
        self.assertEqual(playbook_core.working_tree_blobs(self.repo), {})


if __name__ == '__main__':
    unittest.main()
