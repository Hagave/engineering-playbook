"""
Every shipped Python file compiles, and the Codex SessionStart hook runs.

A string literal broken by an apostrophe in hooks/codex_session_start.py
reached develop with the suite green: nothing in tests/run_all.py imported
that hook (only tests/test_codex_behavior.sh runs it, and CI does not run that
script). Compiling every file catches that class for every hook a test does
not happen to import.
"""
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SHIPPED_DIRS = ('hooks', 'scripts', 'lab', 'adapters')


def _python_files() -> list[str]:
    files = []
    for top in _SHIPPED_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, top)):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            files += [os.path.join(dirpath, f) for f in filenames if f.endswith('.py')]
    return files


class TestPythonSources(unittest.TestCase):

    def test_every_shipped_python_file_compiles(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        files = _python_files()
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=os.path.relpath(path, _ROOT)):
                py_compile.compile(path, cfile=os.path.join(out, 'x.pyc'), doraise=True)

    def test_no_tracked_file_is_one_git_ignores(self):
        # Bytecode committed before `*.pyc` was ignored carried the absolute
        # path of the machine that compiled it.
        done = subprocess.run(('git', '-C', _ROOT, 'ls-files', '-ci', '--exclude-standard'),
                              capture_output=True, text=True)
        if done.returncode != 0:
            self.skipTest('not a git checkout')
        self.assertEqual(done.stdout.split(), [])

    def test_codex_session_start_runs_and_carries_the_prose_level(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        prefs_dir = os.path.join(home, '.claude', 'hooks')
        os.makedirs(prefs_dir)
        with open(os.path.join(prefs_dir, '.playbook-prefs.json'), 'w') as f:
            json.dump({'prose': 'low'}, f)
        done = subprocess.run(
            (sys.executable, os.path.join(_ROOT, 'hooks', 'codex_session_start.py')),
            input='{}', capture_output=True, text=True, env={**os.environ, 'HOME': home})
        self.assertEqual(done.returncode, 0, done.stderr)
        context = json.loads(done.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('Prose budget: LOW', context)


if __name__ == '__main__':
    unittest.main()
