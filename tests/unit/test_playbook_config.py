"""
Unit tests for scripts/playbook_config.py — the CLI behind /playbook-config.

The line printed after `set` is the user's only signal of what changed and
when. It used to say every preference was "already in force for the hooks";
prose is read only at SessionStart, so a prose change did nothing visible and
the user had no way to know why.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    'playbook_config', os.path.join(_ROOT, 'scripts', 'playbook_config.py'))
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)


class TestEffectNote(unittest.TestCase):

    def test_prose_waits_for_the_next_session(self):
        note = config.effect_note('prose', 'low')
        self.assertIn('next session', note)
        self.assertNotIn('In force now', note)

    def test_watch_says_warnings_start_or_stop_now(self):
        self.assertIn('Warnings start now', config.effect_note('watch', True))
        self.assertIn('Warnings stop now', config.effect_note('watch', False))

    def test_budgets_are_in_force_now_with_the_value(self):
        self.assertIn('40,000', config.effect_note('claude_md_budget', 40000))
        self.assertIn('250,000', config.effect_note('context_budget', 250000))

    def test_zero_budget_reads_as_off(self):
        self.assertIn('off', config.effect_note('claude_md_budget', 0))
        self.assertIn('off', config.effect_note('context_budget', 0))


class TestSetPrintsTheEffect(unittest.TestCase):

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state)

    def _run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(config, 'STATE_DIR', self.state), contextlib.redirect_stdout(out):
            code = config.main(list(argv))
        return code, out.getvalue()

    def test_set_prose_prints_value_and_when_it_applies(self):
        code, out = self._run('set', 'prose', 'low')
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            '[playbook:config] prose = low',
            config.effect_note('prose', 'low'),
        ])

    def test_set_writes_through_a_rename_and_leaves_no_temp_file(self):
        with mock.patch.object(config.playbook_core.os, 'replace',
                               wraps=config.playbook_core.os.replace) as replace:
            self._run('set', 'watch', 'on')
        replace.assert_called_once()
        self.assertEqual(os.listdir(self.state), ['.playbook-prefs.json'])

    def test_show_json_lists_every_setting_with_valid_choices(self):
        code, out = self._run('show', '--json')
        self.assertEqual(code, 0)
        settings = json.loads(out)['settings']
        self.assertEqual([s['key'] for s in settings], list(config.DESCRIPTIONS))
        prose = settings[0]
        self.assertEqual(prose['value'], 'mid')
        self.assertEqual([c['value'] for c in prose['choices']], ['low', 'mid', 'high'])
        # The panel shows `help` under each setting's name: none may be blank.
        self.assertTrue(all(s['help'].strip() for s in settings))

    def test_every_choice_the_panel_offers_is_accepted_by_set(self):
        for key, choices in config.CHOICES.items():
            for value, _label in choices:
                with self.subTest(key=key, value=value):
                    code, _ = self._run('set', key, value)
                    self.assertEqual(code, 0)

    def test_show_json_reflects_a_set(self):
        self._run('set', 'watch', 'on')
        _, out = self._run('show', '--json')
        watch = next(s for s in json.loads(out)['settings'] if s['key'] == 'watch')
        self.assertEqual(watch['value'], 'on')
        self.assertEqual(watch['default'], 'off')

    def test_set_context_budget_reports_the_parsed_value(self):
        code, out = self._run('set', 'context_budget', '250k')
        self.assertEqual(code, 0)
        self.assertIn('250,000', out)



class TestHarness(unittest.TestCase):
    """Only the settings a harness acts on are offered there: one nothing
    reads would be accepted and silently ignored."""

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state)

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(config, 'STATE_DIR', self.state), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = config.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _keys(self, *argv: str) -> list[str]:
        return [s['key'] for s in json.loads(self._run('show', '--json', *argv)[1])['settings']]

    def test_claude_code_is_the_default_and_offers_everything(self):
        self.assertEqual(self._keys(), list(config.DESCRIPTIONS))
        self.assertEqual(self._keys('--harness', 'claude'), list(config.DESCRIPTIONS))

    def test_codex_and_antigravity_offer_the_prose_level_only(self):
        for harness in ('codex', 'antigravity'):
            with self.subTest(harness=harness):
                self.assertEqual(self._keys('--harness', harness), ['prose'])
                self.assertNotIn('watch', self._run('show', '--harness', harness)[1])

    def test_set_refuses_a_setting_the_harness_does_not_read(self):
        code, out, err = self._run('set', 'watch', 'on', '--harness', 'antigravity')
        self.assertEqual(code, 1)
        self.assertIn('no effect in antigravity', err)
        self.assertFalse(os.listdir(self.state))  # nothing written

    def test_set_prose_works_in_every_harness(self):
        for harness in config.HARNESSES:
            with self.subTest(harness=harness):
                self.assertEqual(self._run('set', 'prose', 'high', '--harness', harness)[0], 0)

    def test_unknown_harness_is_an_error(self):
        self.assertEqual(self._run('show', '--harness', 'vim')[0], 2)

    def test_session_prints_the_prose_policy(self):
        self._run('set', 'prose', 'low')
        code, out, _ = self._run('session', '--harness', 'antigravity')
        self.assertEqual(code, 0)
        self.assertIn('Prose budget: LOW', out)

    def test_every_setting_is_mapped_to_its_harnesses(self):
        for harness, keys in config.HARNESS_SETTINGS.items():
            with self.subTest(harness=harness):
                self.assertTrue(set(keys) <= set(config.DESCRIPTIONS))
        self.assertEqual(set(config.HARNESS_SETTINGS), set(config.HARNESSES))


if __name__ == '__main__':
    unittest.main()
