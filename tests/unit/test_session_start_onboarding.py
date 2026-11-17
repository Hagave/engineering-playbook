"""
Unit tests for two session-start conveniences:

1. The injected context tells the model to reply in the user's language and to
   keep it after a /clear (fixing the English-after-clear regression).
2. A first-run guided-setup offer, shown once and gated by a marker file, plus
   the /playbook-config skill's instruction to localize its menu and to run the
   guided setup.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402  (session_start imports it at module load)

_spec = importlib.util.spec_from_file_location(
    'session_start', os.path.join(_ROOT, 'hooks', 'session_start.py'))
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

_CONFIG_SKILL = os.path.join(_ROOT, 'skills', 'playbook-config', 'SKILL.md')


class TestLanguageInstruction(unittest.TestCase):

    def test_context_tells_the_model_to_use_the_users_language(self) -> None:
        self.assertIn('language they write in', ss.CONTEXT)

    def test_context_blocks_the_english_after_clear_regression(self) -> None:
        # The English context must not read as a cue to switch the chat to English.
        self.assertIn('not a cue to switch', ss.CONTEXT)
        self.assertIn('/clear', ss.CONTEXT)


class TestFirstRunOffer(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved = (ss.STATE_DIR, ss._ONBOARD_MARKER)
        ss.STATE_DIR = self._tmp.name
        ss._ONBOARD_MARKER = os.path.join(self._tmp.name, '.onboarded')
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        ss.STATE_DIR, ss._ONBOARD_MARKER = self._saved

    def test_first_call_offers_then_marks_so_the_next_is_quiet(self) -> None:
        self.assertTrue(ss._first_run())
        self.assertTrue(os.path.exists(ss._ONBOARD_MARKER))
        self.assertFalse(ss._first_run())

    def test_the_offer_points_at_playbook_config_and_is_skippable(self) -> None:
        self.assertIn('/playbook-config', ss.FIRST_RUN_NOTE)
        self.assertIn('skip', ss.FIRST_RUN_NOTE.lower())


class TestConfigSkillLocalizationAndTour(unittest.TestCase):

    def setUp(self) -> None:
        with open(_CONFIG_SKILL, encoding='utf-8') as fh:
            self.text = fh.read()

    def test_the_menu_is_rendered_in_the_users_language(self) -> None:
        self.assertIn("language the user writes in", self.text)

    def test_the_guided_setup_section_exists_and_is_skippable(self) -> None:
        self.assertIn('## Guided setup (first run)', self.text)
        self.assertIn('skip', self.text.lower())


if __name__ == '__main__':
    unittest.main()
