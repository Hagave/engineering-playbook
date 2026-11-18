"""
Unit tests for the once-per-project code-intelligence hint in
hooks/session_start.py: when the project's detected language has an official
language-server plugin that `claude plugin list --json` does not report, the
user is told once, through systemMessage, in the configured language.

The plugin-list fixtures copy the shape `claude plugin list --json` printed on
Claude Code 2.1.327 (an install of typescript-lsp into a scratch config dir, and
a session-only plugin); only the paths are made generic.

The opt-in test at the end (PLAYBOOK_CLAUDE_IT=1) installs typescript-lsp from
the official marketplace into a scratch CLAUDE_CONFIG_DIR and reads it back
through the real CLI. It needs `claude` on PATH and network access.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'session_start', os.path.join(_ROOT, 'hooks', 'session_start.py'))
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

_TS_INSTALLED = [{
    'id': 'typescript-lsp@claude-plugins-official',
    'version': '1.0.0',
    'scope': 'user',
    'enabled': True,
    'installPath': '/cfg/plugins/cache/claude-plugins-official/typescript-lsp/1.0.0',
    'installedAt': '2026-11-18T18:52:40.663Z',
    'lastUpdated': '2026-11-18T18:52:40.663Z',
    'projectEnabled': False,
}]
_SESSION_ONLY = [{
    'id': 'engineering-playbook@inline',
    'version': '4.1.0',
    'scope': 'session',
    'enabled': True,
    'installPath': '/plugins/engineering-playbook',
}]


def _listed(entries: object, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=('claude', 'plugin', 'list', '--json'), returncode=returncode,
        stdout=json.dumps(entries, indent=2), stderr='')


class _Base(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = os.path.join(tmp.name, 'state')
        self.config = os.path.join(tmp.name, 'config')
        self.project = os.path.join(tmp.name, 'project')
        os.makedirs(self.config)
        os.makedirs(self.project)
        patches = (
            mock.patch.object(ss, 'STATE_DIR', self.state),
            mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.config, 'LANG': 'C'}),
            mock.patch.object(ss.shutil, 'which', return_value='/usr/bin/claude'),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.run_mock = mock.patch.object(ss.subprocess, 'run', return_value=_listed([]))
        self.run = self.run_mock.start()
        self.addCleanup(self.run_mock.stop)

    def _touch(self, name: str) -> None:
        open(os.path.join(self.project, name), 'w').close()

    def _claude_json(self, config: dict) -> None:
        with open(os.path.join(self.config, '.claude.json'), 'w', encoding='utf-8') as f:
            json.dump(config, f)


class TestHintShown(_Base):

    def test_detected_language_without_its_plugin_names_plugin_and_binary(self) -> None:
        self._touch('tsconfig.json')
        self.run.return_value = _listed(_SESSION_ONLY)
        hint = ss._lsp_hint(self.project)
        self.assertIn('typescript-lsp', hint)
        self.assertIn('typescript-language-server', hint)
        self.assertIn('/plugin install typescript-lsp@claude-plugins-official', hint)

    def test_the_plugin_list_is_read_for_the_project_directory(self) -> None:
        self._touch('go.mod')
        ss._lsp_hint(self.project)
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ('/usr/bin/claude', 'plugin', 'list', '--json'))
        self.assertEqual(kwargs['cwd'], self.project)

    def test_every_language_in_a_polyglot_root_gets_its_own_line(self) -> None:
        self._touch('go.mod')
        self._touch('Cargo.toml')
        lines = ss._lsp_hint(self.project).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(any('gopls-lsp' in line for line in lines))
        self.assertTrue(any('rust-analyzer-lsp' in line for line in lines))

    def test_every_language_the_detector_knows_has_a_plugin(self) -> None:
        detected = {lang for lang, _ in playbook_core._LANGUAGE_MARKERS}
        self.assertEqual(detected, set(ss.LSP_PLUGINS))


class TestHintSilent(_Base):

    def test_silent_when_the_plugin_is_installed(self) -> None:
        self._touch('tsconfig.json')
        self.run.return_value = _listed(_TS_INSTALLED)
        self.assertEqual(ss._lsp_hint(self.project), '')

    def test_silent_when_the_plugin_is_installed_but_disabled(self) -> None:
        self._touch('tsconfig.json')
        self.run.return_value = _listed([dict(_TS_INSTALLED[0], enabled=False)])
        self.assertEqual(ss._lsp_hint(self.project), '')

    def test_silent_when_the_plugin_comes_from_another_marketplace(self) -> None:
        self._touch('tsconfig.json')
        self.run.return_value = _listed([dict(_TS_INSTALLED[0], id='typescript-lsp@team')])
        self.assertEqual(ss._lsp_hint(self.project), '')

    def test_silent_and_no_cli_call_for_an_unknown_language(self) -> None:
        self._touch('README.md')
        self.assertEqual(ss._lsp_hint(self.project), '')
        self.run.assert_not_called()

    def test_silent_for_a_detected_language_without_a_plugin(self) -> None:
        self._touch('tsconfig.json')
        with mock.patch.dict(ss.LSP_PLUGINS, clear=True):
            self.assertEqual(ss._lsp_hint(self.project), '')
        self.run.assert_not_called()

    def test_silent_when_all_lsp_recommendations_were_disabled(self) -> None:
        self._touch('tsconfig.json')
        self._claude_json({'lspRecommendationDisabled': True})
        self.assertEqual(ss._lsp_hint(self.project), '')

    def test_silent_after_five_ignored_recommendations(self) -> None:
        self._touch('tsconfig.json')
        self._claude_json({'lspRecommendationIgnoredCount': 5})
        self.assertEqual(ss._lsp_hint(self.project), '')

    def test_silent_for_a_plugin_the_user_said_never_to(self) -> None:
        self._touch('go.mod')
        self._touch('tsconfig.json')
        self._claude_json({'lspRecommendationNeverPlugins': ['typescript-lsp@claude-plugins-official']})
        hint = ss._lsp_hint(self.project)
        self.assertNotIn('typescript-lsp', hint)
        self.assertIn('gopls-lsp', hint)


class TestUnreadablePluginList(_Base):
    """A list the hook cannot read is unknown: no hint, and nothing settled."""

    def _assert_unknown(self) -> None:
        self._touch('tsconfig.json')
        self.assertEqual(ss._lsp_hint(self.project), '')
        self.assertFalse(os.path.exists(ss._lsp_hint_path(self.project)))

    def test_no_claude_on_path(self) -> None:
        with mock.patch.object(ss.shutil, 'which', return_value=None):
            self._assert_unknown()

    def test_cli_fails(self) -> None:
        self.run.return_value = _listed([], returncode=1)
        self._assert_unknown()

    def test_cli_times_out(self) -> None:
        self.run.side_effect = subprocess.TimeoutExpired('claude', ss._PLUGIN_LIST_TIMEOUT)
        self._assert_unknown()

    def test_output_is_not_json(self) -> None:
        self.run.return_value = subprocess.CompletedProcess((), 0, 'No plugins installed.', '')
        self._assert_unknown()

    def test_output_is_not_a_list(self) -> None:
        self.run.return_value = _listed({'installed': []})
        self._assert_unknown()

    def test_an_entry_without_an_id(self) -> None:
        self.run.return_value = _listed([{'version': '1.0.0'}])
        self._assert_unknown()

    def test_the_next_session_retries(self) -> None:
        self.run.return_value = _listed([], returncode=1)
        self._assert_unknown()
        self.run.return_value = _listed([])
        self.assertIn('typescript-lsp', ss._lsp_hint(self.project))


class TestOncePerProject(_Base):

    def test_shown_once_then_silent_without_another_cli_call(self) -> None:
        self._touch('tsconfig.json')
        self.assertIn('typescript-lsp', ss._lsp_hint(self.project))
        self.run.reset_mock()
        self.assertEqual(ss._lsp_hint(self.project), '')
        self.run.assert_not_called()

    def test_an_active_plugin_settles_the_language_too(self) -> None:
        self._touch('tsconfig.json')
        self.run.return_value = _listed(_TS_INSTALLED)
        ss._lsp_hint(self.project)
        self.run.reset_mock()
        ss._lsp_hint(self.project)
        self.run.assert_not_called()

    def test_another_project_still_gets_its_hint(self) -> None:
        self._touch('tsconfig.json')
        ss._lsp_hint(self.project)
        other = os.path.join(os.path.dirname(self.project), 'other')
        os.makedirs(other)
        open(os.path.join(other, 'tsconfig.json'), 'w').close()
        self.assertIn('typescript-lsp', ss._lsp_hint(other))

    def test_a_language_added_later_gets_its_own_hint(self) -> None:
        self._touch('tsconfig.json')
        ss._lsp_hint(self.project)
        self._touch('go.mod')
        hint = ss._lsp_hint(self.project)
        self.assertIn('gopls-lsp', hint)
        self.assertNotIn('typescript-lsp', hint)

    def test_the_flag_lives_in_the_state_dir_not_the_project(self) -> None:
        self._touch('tsconfig.json')
        ss._lsp_hint(self.project)
        self.assertTrue(ss._lsp_hint_path(self.project).startswith(self.state + os.sep))
        self.assertEqual(sorted(os.listdir(self.project)), ['tsconfig.json'])


class TestHintLanguage(_Base):

    def _hint_in(self, language: str) -> str:
        playbook_core.set_pref(self.state, 'language', language)
        self._touch('tsconfig.json')
        return ss._lsp_hint(self.project)

    def test_english(self) -> None:
        self.assertIn('Shown once per project', self._hint_in('en'))

    def test_brazilian_portuguese(self) -> None:
        self.assertIn('Aviso único por projeto', self._hint_in('pt-br'))

    def test_spanish(self) -> None:
        self.assertIn('ejecuta', self._hint_in('es'))

    def test_auto_follows_the_locale(self) -> None:
        with mock.patch.dict(os.environ, {'LANG': 'pt_BR.UTF-8'}):
            self.assertIn('Aviso único por projeto', self._hint_in('auto'))


class TestHookOutput(unittest.TestCase):
    """The hint reaches the user through systemMessage, never the model's context."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.project = os.path.join(tmp.name, 'project')
        os.makedirs(self.project)
        open(os.path.join(self.project, 'tsconfig.json'), 'w').close()
        bin_dir = os.path.join(tmp.name, 'bin')
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, 'claude')
        with open(fake, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\necho "[]"\n')
        os.chmod(fake, 0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ATTENDED',
                                 'CLAUDE_CONFIG_DIR')}
        self.env.update(HOME=self.home, PATH=f'{bin_dir}{os.pathsep}{self.env.get("PATH", "")}',
                        LANG='C')

    def _run(self, source: str, **env: str) -> dict:
        payload = json.dumps({'session_id': 'lsp1', 'source': source, 'cwd': self.project})
        proc = subprocess.run([sys.executable, os.path.join(_ROOT, 'hooks', 'session_start.py')],
                              input=payload, capture_output=True, text=True,
                              env={**self.env, **env}, timeout=30, check=True)
        return json.loads(proc.stdout)

    def test_startup_puts_the_hint_in_system_message_only(self) -> None:
        out = self._run('startup')
        self.assertIn('typescript-lsp', out.get('systemMessage', ''))
        self.assertNotIn('typescript-lsp', out['hookSpecificOutput']['additionalContext'])

    def test_second_session_is_silent(self) -> None:
        self._run('startup')
        self.assertNotIn('systemMessage', self._run('startup'))

    def test_headless_run_neither_shows_nor_consumes_it(self) -> None:
        self.assertNotIn('systemMessage', self._run('startup', CLAUDE_CODE_ENTRYPOINT='sdk-cli'))
        self.assertIn('typescript-lsp', self._run('startup').get('systemMessage', ''))

    def test_compaction_neither_shows_nor_consumes_it(self) -> None:
        self.assertNotIn('systemMessage', self._run('compact'))
        self.assertIn('typescript-lsp', self._run('startup').get('systemMessage', ''))


class TestCodexAdapterStaysSilent(unittest.TestCase):

    def test_codex_session_start_has_no_lsp_hint(self) -> None:
        with open(os.path.join(_ROOT, 'hooks', 'codex_session_start.py'), encoding='utf-8') as f:
            source = f.read()
        self.assertNotIn('lsp', source.lower())
        self.assertNotIn('session_start', source.replace('codex_session_start', ''))


@unittest.skipUnless(os.environ.get('PLAYBOOK_CLAUDE_IT') == '1',
                     'opt-in: PLAYBOOK_CLAUDE_IT=1 (needs claude on PATH and network)')
class TestRealPluginList(unittest.TestCase):
    """Installs typescript-lsp into a scratch config dir and reads it back through
    the real `claude plugin list --json`, before and after the install."""

    def test_real_cli_reports_the_install_and_silences_the_hint(self) -> None:
        claude = shutil.which('claude')
        self.assertIsNotNone(claude, 'claude is not on PATH')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = os.path.join(tmp.name, 'config')
        project = os.path.join(tmp.name, 'project')
        state = os.path.join(tmp.name, 'state')
        os.makedirs(config)
        os.makedirs(project)
        open(os.path.join(project, 'tsconfig.json'), 'w').close()
        env = {**os.environ, 'CLAUDE_CONFIG_DIR': config}
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': config}), \
                mock.patch.object(ss, 'STATE_DIR', state):
            self.assertEqual(ss._plugin_names(project), set())
            for args in (('plugin', 'marketplace', 'add', 'anthropics/claude-plugins-official'),
                         ('plugin', 'install', 'typescript-lsp@claude-plugins-official',
                          '--scope', 'user')):
                subprocess.run((claude, *args), cwd=project, env=env, capture_output=True,
                               text=True, timeout=300, check=True)
            self.assertIn('typescript-lsp', ss._plugin_names(project))
            self.assertEqual(ss._lsp_hint(project), '')


if __name__ == '__main__':
    unittest.main()
