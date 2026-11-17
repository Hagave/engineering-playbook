"""
Tests for the static analysis delegation: detection of an installed engine,
the command the session context hands the model, and the validation pattern
that lets a scan clear the Stop gate.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402


def _which(installed: set[str]):
    return lambda name: f'/usr/bin/{name}' if name in installed else None


class TestSastNotice(unittest.TestCase):

    def setUp(self):
        self.project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.project)

    def _notice(self, installed: set[str]) -> str:
        with mock.patch('shutil.which', _which(installed)):
            return playbook_core.sast_notice(self.project)

    def test_nothing_installed_says_nothing(self):
        self.assertEqual(self._notice(set()), '')

    def test_semgrep_without_local_rules_uses_the_registry_with_metrics_off(self):
        notice = self._notice({'semgrep'})
        self.assertIn('semgrep scan --metrics=off --error --severity ERROR --config p/default', notice)
        self.assertIn('nosemgrep', notice)

    def test_local_rules_win_over_the_registry(self):
        open(os.path.join(self.project, '.semgrep.yml'), 'w').close()
        notice = self._notice({'semgrep'})
        self.assertIn('--config .semgrep.yml', notice)
        self.assertNotIn('p/default', notice)

    def test_opengrep_needs_a_local_config(self):
        self.assertIn('no local rule config', self._notice({'opengrep'}))
        os.mkdir(os.path.join(self.project, '.semgrep'))
        notice = self._notice({'opengrep'})
        self.assertIn('opengrep scan --error --severity ERROR --config .semgrep', notice)
        self.assertNotIn('--metrics', notice)

    def test_semgrep_is_preferred_when_both_are_installed(self):
        self.assertIn('`semgrep` is installed', self._notice({'semgrep', 'opengrep'}))


class TestSastClearsValidation(unittest.TestCase):

    def test_scan_counts_as_validation(self):
        for command in ('semgrep scan --metrics=off --error --config p/default src/a.ts',
                        'opengrep scan --config .semgrep src/', 'semgrep ci'):
            with self.subTest(command=command):
                self.assertTrue(playbook_core.VALIDATION_RE.search(command))

    def test_version_check_does_not(self):
        self.assertIsNone(playbook_core.VALIDATION_RE.search('semgrep --version'))


if __name__ == '__main__':
    unittest.main()
