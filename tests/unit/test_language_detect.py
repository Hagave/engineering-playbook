"""
Unit tests for the language-detection hint in hooks/playbook_core.py:
detect_languages reads the project-root marker files and language_notice turns
them into the SessionStart `[playbook:language]` hint. One marker per playbook
language, plus the polyglot and empty cases.
"""
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402


class TestDetectLanguages(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix='lang_')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _touch(self, name: str) -> None:
        open(os.path.join(self.dir, name), 'w').close()

    def test_each_marker_identifies_its_language(self) -> None:
        cases = {
            'tsconfig.json': 'typescript',
            'Cargo.toml': 'rust',
            'go.mod': 'go',
            'app.csproj': 'csharp',
            'build.gradle.kts': 'kotlin',
            'pom.xml': 'java',
            'Gemfile': 'ruby',
        }
        for marker, lang in cases.items():
            with self.subTest(marker=marker):
                d = tempfile.mkdtemp(prefix='lang1_')
                self.addCleanup(shutil.rmtree, d, True)
                open(os.path.join(d, marker), 'w').close()
                self.assertEqual(playbook_core.detect_languages(d), [(lang, marker)])

    def test_a_project_without_a_marker_is_silent(self) -> None:
        self.assertEqual(playbook_core.detect_languages(self.dir), [])
        self.assertEqual(playbook_core.language_notice(self.dir), '')

    def test_the_notice_names_the_language_and_its_opt_in(self) -> None:
        self._touch('go.mod')
        notice = playbook_core.language_notice(self.dir)
        self.assertIn('[playbook:language]', notice)
        self.assertIn('go.mod', notice)
        self.assertIn('engineering-go', notice)

    def test_a_polyglot_root_lists_every_language_and_opt_in(self) -> None:
        self._touch('go.mod')
        self._touch('Cargo.toml')
        found = dict(playbook_core.detect_languages(self.dir))
        self.assertIn('go', found)
        self.assertIn('rust', found)
        notice = playbook_core.language_notice(self.dir)
        self.assertIn('engineering-go', notice)
        self.assertIn('engineering-rust', notice)

    def test_kotlin_kts_is_detected_from_its_own_marker(self) -> None:
        # A Gradle project with both files: Kotlin is read from its .kts marker,
        # not confused with the Java build.gradle also present.
        self._touch('build.gradle.kts')
        self._touch('build.gradle')
        found = dict(playbook_core.detect_languages(self.dir))
        self.assertEqual(found.get('kotlin'), 'build.gradle.kts')


if __name__ == '__main__':
    unittest.main()
