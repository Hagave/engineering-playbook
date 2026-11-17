"""
Unit tests for two session-start conveniences:

1. The injected context tells the model to reply in the user's language and to
   keep it after a /clear (fixing the English-after-clear regression).
2. A first-run guided-setup offer, shown once and gated by a marker file, plus
   the /playbook-config skill's instruction to localize its menu and to run the
   guided setup. A headless run (`claude -p`) neither sees nor consumes it.
3. The Codex and Antigravity adapters carry the same language instruction.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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


def _run_hook(script: str, home: str, env_extra: dict[str, str]) -> str:
    """Runs a SessionStart hook with an isolated HOME and returns its context."""
    env = {k: v for k, v in os.environ.items()
           if k not in ('CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ATTENDED')}
    env.update(HOME=home, **env_extra)
    payload = json.dumps({'session_id': 's1', 'source': 'startup', 'cwd': home})
    proc = subprocess.run([sys.executable, os.path.join(_ROOT, 'hooks', script)],
                          input=payload, capture_output=True, text=True, env=env,
                          timeout=30, check=True)
    return json.loads(proc.stdout)['hookSpecificOutput']['additionalContext']


class TestHeadlessSessionKeepsTheOffer(unittest.TestCase):
    """A `claude -p` run must not consume the one-time offer: nobody reads it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.marker = os.path.join(self._tmp.name, '.claude', 'hooks', '.onboarded')

    def test_sdk_entrypoint_is_not_attended(self) -> None:
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli'}, clear=False):
            os.environ.pop('CLAUDE_CODE_SESSION_ATTENDED', None)
            self.assertFalse(ss._attended())

    def test_attended_zero_is_not_attended(self) -> None:
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_ENTRYPOINT': 'cli',
                                          'CLAUDE_CODE_SESSION_ATTENDED': '0'}):
            self.assertFalse(ss._attended())

    def test_interactive_and_unknown_are_attended(self) -> None:
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_ENTRYPOINT': 'cli',
                                          'CLAUDE_CODE_SESSION_ATTENDED': '1'}):
            self.assertTrue(ss._attended())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CLAUDE_CODE_ENTRYPOINT', None)
            os.environ.pop('CLAUDE_CODE_SESSION_ATTENDED', None)
            self.assertTrue(ss._attended())

    def test_headless_run_leaves_the_offer_for_the_first_interactive_one(self) -> None:
        headless = _run_hook('session_start.py', self._tmp.name,
                             {'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli',
                              'CLAUDE_CODE_SESSION_ATTENDED': '0'})
        self.assertNotIn('First run', headless)
        self.assertFalse(os.path.exists(self.marker))

        interactive = _run_hook('session_start.py', self._tmp.name,
                                {'CLAUDE_CODE_ENTRYPOINT': 'cli',
                                 'CLAUDE_CODE_SESSION_ATTENDED': '1'})
        self.assertIn('First run', interactive)
        self.assertTrue(os.path.exists(self.marker))


class TestAdaptersCarryTheLanguagePolicy(unittest.TestCase):
    """Codex and Antigravity get the same user-language instruction as Claude Code."""

    def test_claude_code_context_uses_the_shared_policy(self) -> None:
        self.assertIn(playbook_core.USER_LANGUAGE_POLICY, ss.CONTEXT)

    def test_codex_session_start_injects_it(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            context = _run_hook('codex_session_start.py', home, {})
        self.assertIn(playbook_core.USER_LANGUAGE_POLICY, context)

    def test_antigravity_template_carries_it_verbatim(self) -> None:
        path = os.path.join(_ROOT, 'adapters', 'antigravity', 'AGENTS.template.md')
        with open(path, encoding='utf-8') as fh:
            self.assertIn(playbook_core.USER_LANGUAGE_POLICY, fh.read())


class TestConfigSkillLocalizationAndTour(unittest.TestCase):

    def setUp(self) -> None:
        with open(_CONFIG_SKILL, encoding='utf-8') as fh:
            self.text = fh.read()

    def test_the_menu_is_rendered_in_the_users_language(self) -> None:
        self.assertIn("language the user writes in", self.text)

    def test_the_guided_setup_section_exists_and_is_skippable(self) -> None:
        self.assertIn('## Guided setup (first run)', self.text)
        self.assertIn('skip', self.text.lower())

    def test_status_surfaces_the_security_layer_so_it_is_discoverable(self) -> None:
        self.assertIn('security layer', self.text.lower())
        self.assertIn('lab/run.py', self.text)


if __name__ == '__main__':
    unittest.main()
