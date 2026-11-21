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
        # An English locale: `auto` would otherwise follow the machine running the tests.
        with mock.patch.object(config, 'STATE_DIR', self.state), \
                mock.patch.dict(os.environ, {'LANG': 'en_US.UTF-8'}, clear=True), \
                contextlib.redirect_stdout(out):
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

    def test_the_context_budget_offers_off_550k_650k_850k_and_a_custom_value(self):
        _, out = self._run('show', '--json')
        settings = {s['key']: s for s in json.loads(out)['settings']}
        budget = settings['context_budget']
        self.assertEqual([(c['value'], c['label']) for c in budget['choices']],
                         [('0', 'off'), ('550000', '550k'), ('650000', '650k'), ('850000', '850k')])
        self.assertEqual((budget['value'], budget['default']), ('550000', '550000'))
        self.assertTrue(budget['custom'])
        self.assertEqual([k for k, s in settings.items() if s['custom']], ['context_budget'])

    def test_a_custom_context_budget_is_accepted_and_shown_as_set(self):
        code, _ = self._run('set', 'context_budget', '700k')
        self.assertEqual(code, 0)
        _, out = self._run('show', '--json')
        budget = next(s for s in json.loads(out)['settings'] if s['key'] == 'context_budget')
        self.assertEqual(budget['value'], '700000')

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



class TestLanguage(unittest.TestCase):
    """The panel draws fixed text, so the model's "reply in the user's
    language" never reached it: the menu stayed English for a user writing
    Portuguese. The `language` setting picks the text it is drawn in."""

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state)

    def _run(self, *argv: str, lang_env: str = 'en_US.UTF-8') -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {'LANG': lang_env}
        with mock.patch.object(config, 'STATE_DIR', self.state), \
                mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = config.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _show(self, lang_env: str = 'en_US.UTF-8') -> dict:
        return json.loads(self._run('show', '--json', lang_env=lang_env)[1])

    def test_auto_is_the_default_and_follows_the_locale(self):
        self.assertEqual(self._show('en_US.UTF-8')['language'], 'en')
        self.assertEqual(self._show('pt_BR.UTF-8')['language'], 'pt-br')
        self.assertEqual(self._show('es_ES.UTF-8')['language'], 'es')
        self.assertEqual(self._show('C')['language'], 'en')
        self.assertEqual(self._show('de_DE.UTF-8')['language'], 'en')

    def test_a_chosen_language_wins_over_the_locale(self):
        self.assertEqual(self._run('set', 'language', 'pt-BR')[0], 0)
        shown = self._show('en_US.UTF-8')
        self.assertEqual(shown['language'], 'pt-br')
        self.assertEqual(shown['language_name'], 'Brazilian Portuguese')
        self.assertEqual(shown['ui']['exit'], 'Sair')
        prose = next(s for s in shown['settings'] if s['key'] == 'prose')
        self.assertEqual(prose['label'], 'Nível de prosa')
        watch = next(s for s in shown['settings'] if s['key'] == 'watch')
        self.assertEqual([c['label'] for c in watch['choices']], ['ligado', 'desligado'])
        # The values passed to `set` never change with the language.
        self.assertEqual([c['value'] for c in watch['choices']], ['on', 'off'])

    def test_set_language_accepts_the_usual_spellings_and_rejects_others(self):
        for spelling in ('pt-BR', 'pt_br', 'PT-BR', 'es', 'en', 'auto'):
            with self.subTest(spelling=spelling):
                self.assertEqual(self._run('set', 'language', spelling)[0], 0)
        code, _, err = self._run('set', 'language', 'klingon')
        self.assertEqual(code, 1)
        self.assertIn('language must be one of', err)

    def test_the_effect_line_of_a_set_is_in_the_chosen_language(self):
        self._run('set', 'language', 'pt-br')
        _, out, _ = self._run('set', 'context_budget', '250k')
        self.assertEqual(out.splitlines()[1], config.effect_note('context_budget', 250000, 'pt-br'))
        self.assertIn('250.000', out)

    def test_setting_the_language_reports_it_in_that_language(self):
        _, out, _ = self._run('set', 'language', 'es')
        self.assertIn('Español', out.splitlines()[1])
        self.assertIn('En vigor ahora', out.splitlines()[1])

    def test_a_corrupt_stored_language_reads_as_auto(self):
        with open(os.path.join(self.state, '.playbook-prefs.json'), 'w') as f:
            json.dump({'language': 'xx'}, f)
        self.assertEqual(config.playbook_core.load_prefs(self.state)['language'], 'auto')

    def test_language_is_a_claude_code_setting_only(self):
        code, _, err = self._run('set', 'language', 'pt-br', '--harness', 'codex')
        self.assertEqual(code, 1)
        self.assertIn('no effect in codex', err)


class TestTranslations(unittest.TestCase):
    """A string missing in one language would fall back to English mid-menu."""

    def test_every_string_exists_in_every_language(self):
        i18n = config.playbook_i18n
        for key, entry in i18n.STRINGS.items():
            with self.subTest(key=key):
                self.assertEqual(set(entry), set(i18n.LANGUAGE_NAMES))
                self.assertTrue(all(value.strip() for value in entry.values()))

    def test_every_setting_has_a_label_and_help(self):
        for key in config.DESCRIPTIONS:
            with self.subTest(key=key):
                self.assertIn(f'label_{key}', config.playbook_i18n.STRINGS)
                self.assertIn(f'help_{key}', config.playbook_i18n.STRINGS)

    def test_placeholders_match_across_languages(self):
        import string
        for key, entry in config.playbook_i18n.STRINGS.items():
            fields = {lang: {f for _, f, _, _ in string.Formatter().parse(text) if f}
                      for lang, text in entry.items()}
            with self.subTest(key=key):
                self.assertEqual(len({frozenset(f) for f in fields.values()}), 1)

    def test_the_lab_entry_says_what_it_is_in_every_language(self):
        for language in config.playbook_i18n.LANGUAGE_NAMES:
            with self.subTest(language=language):
                label = config.playbook_i18n.text(language, 'lab')
                self.assertTrue(label.startswith('red-lab-playbook: '))


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

    def test_the_numbered_menu_has_a_digit_for_every_claude_code_setting(self):
        # hooks/config_panel.tsx MAX_SETTING_ROWS: digits 1-9 hold the setting
        # rows plus Status, Learn, red-lab-playbook and Diff panel. A group is
        # one row; a sixth row would silently fall off the menu.
        rows = {config._group_of(key) or key for key in config.HARNESS_SETTINGS['claude']}
        self.assertLessEqual(len(rows), 5)

    def test_every_grouped_setting_reaches_its_group_screen(self):
        shown = config.snapshot(config.playbook_core.load_prefs(self.state), 'claude')
        groups = {group['key'] for group in shown['groups']}
        for setting in shown['settings']:
            with self.subTest(key=setting['key']):
                self.assertTrue(setting['group'] == '' or setting['group'] in groups)
        self.assertEqual({s['key'] for s in shown['settings'] if s['group'] == 'budgets'},
                         {'claude_md_budget', 'context_budget'})

    def test_deviation_reports_are_off_by_default_and_offered_on_claude_code_only(self):
        self.assertIs(config.playbook_core.PREFS_DEFAULTS['reports'], False)
        self.assertIs(config.playbook_core.load_prefs(self.state)['reports'], False)
        self.assertIn('reports', config.HARNESS_SETTINGS['claude'])
        for harness in ('codex', 'antigravity'):
            with self.subTest(harness=harness):
                self.assertNotIn('reports', config.HARNESS_SETTINGS[harness])

    def test_reports_turn_on_and_off_with_the_effect_said(self):
        language = config.playbook_i18n.resolve('auto')
        code, out, _ = self._run('set', 'reports', 'on')
        self.assertEqual(code, 0)
        self.assertIn('reports = on', out)
        self.assertIn(config.effect_note('reports', True, language), out)
        self.assertIs(config.playbook_core.load_prefs(self.state)['reports'], True)
        code, out, _ = self._run('set', 'reports', 'off')
        self.assertIn(config.effect_note('reports', False, language), out)
        self.assertIs(config.playbook_core.load_prefs(self.state)['reports'], False)

    def test_every_setting_is_mapped_to_its_harnesses(self):
        for harness, keys in config.HARNESS_SETTINGS.items():
            with self.subTest(harness=harness):
                self.assertTrue(set(keys) <= set(config.DESCRIPTIONS))
        self.assertEqual(set(config.HARNESS_SETTINGS), set(config.HARNESSES))


if __name__ == '__main__':
    unittest.main()
