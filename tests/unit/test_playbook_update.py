"""
The installed version, the newest release, the update, and the notices that
tell a person about them (hooks/playbook_update.py and the three places that
use it: the settings script, the session start, the Stop hook's waiting line).
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core
import playbook_update


def _load(name: str, *parts: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = _load('playbook_config', 'scripts', 'playbook_config.py')
session_start = _load('session_start', 'hooks', 'session_start.py')
temporary_data = _load('temporary_data', 'scripts', 'temporary_data.py')
context_guard = _load('context_guard', 'hooks', 'context_guard.py')

NOW = 1_800_000_000.0


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, True)
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, '.claude-plugin'))
        self.manifest('4.2.1')

    def manifest(self, version: object) -> None:
        with open(os.path.join(self.root, '.claude-plugin', 'plugin.json'), 'w') as handle:
            json.dump({'name': 'engineering-playbook', 'version': version}, handle)


class TestVersions(Case):

    def test_a_stable_release_tag_parses_with_or_without_the_v(self) -> None:
        self.assertEqual(playbook_update.parse_version('v4.2.1'), (4, 2, 1))
        self.assertEqual(playbook_update.parse_version('4.10.0'), (4, 10, 0))

    def test_anything_else_is_not_a_version(self) -> None:
        for text in ('4.2', '4.2.1-rc1', 'v4.2.1-beta.2', '4.2.1.1', 'latest', '', ' ', None, 5, ['4.2.1']):
            with self.subTest(text=text):
                self.assertIsNone(playbook_update.parse_version(text))

    def test_the_installed_version_is_the_manifests(self) -> None:
        self.assertEqual(playbook_update.installed_version(self.root), '4.2.1')

    def test_an_unreadable_or_invalid_manifest_has_no_version(self) -> None:
        self.manifest('not-a-version')
        self.assertIsNone(playbook_update.installed_version(self.root))
        self.assertIsNone(playbook_update.installed_version(os.path.join(self.root, 'missing')))

    def test_the_real_manifest_of_this_repository_has_a_version(self) -> None:
        self.assertIsNotNone(playbook_update.installed_version(_ROOT))


class TestFetchLatest(unittest.TestCase):

    def _answer(self, body: bytes):
        return mock.patch.object(playbook_update.urllib.request, 'urlopen',
                                 return_value=mock.MagicMock(__enter__=lambda s: io.BytesIO(body),
                                                             __exit__=lambda *a: False))

    def test_a_stable_tag_is_returned_without_its_v(self) -> None:
        with self._answer(b'{"tag_name": "v4.3.0"}'):
            self.assertEqual(playbook_update.fetch_latest(), '4.3.0')

    def test_a_prerelease_a_bad_body_or_a_failure_is_unknown(self) -> None:
        for body in (b'{"tag_name": "v4.3.0-rc1"}', b'{}', b'[]', b'null', b'not json', b''):
            with self.subTest(body=body), self._answer(body):
                self.assertIsNone(playbook_update.fetch_latest())
        for error in (urllib.error.URLError('down'), TimeoutError(), ConnectionResetError()):
            with self.subTest(error=error), mock.patch.object(
                    playbook_update.urllib.request, 'urlopen', side_effect=error):
                self.assertIsNone(playbook_update.fetch_latest())

    def test_the_request_is_a_plain_get_that_names_no_one(self) -> None:
        with self._answer(b'{"tag_name": "v4.3.0"}') as opened:
            playbook_update.fetch_latest()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, playbook_update.LATEST_URL)
        self.assertIsNone(request.data)
        sent = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(set(sent), {'accept', 'user-agent'})
        self.assertNotIn(os.path.expanduser('~'), ' '.join(sent.values()))


class TestRefresh(Case):

    def test_a_due_cache_asks_and_keeps_the_answer(self) -> None:
        asked = mock.Mock(return_value='4.3.0')
        cache = playbook_update.refresh(self.state, NOW, fetch=asked)
        self.assertEqual((cache['latest'], cache['ok'], cache['checked_at']), ('4.3.0', True, NOW))
        asked.assert_called_once()
        self.assertEqual(playbook_update.read_cache(self.state)['latest'], '4.3.0')

    def test_a_fresh_cache_is_not_asked_again_until_the_ttl(self) -> None:
        playbook_update.refresh(self.state, NOW, fetch=lambda: '4.3.0')
        asked = mock.Mock(return_value='4.4.0')
        playbook_update.refresh(self.state, NOW + playbook_update.TTL_S - 1, fetch=asked)
        asked.assert_not_called()
        playbook_update.refresh(self.state, NOW + playbook_update.TTL_S, fetch=asked)
        asked.assert_called_once()

    def test_a_failure_is_retried_sooner_and_keeps_the_last_known_release(self) -> None:
        playbook_update.refresh(self.state, NOW, fetch=lambda: '4.3.0')
        later = NOW + playbook_update.TTL_S
        failed = playbook_update.refresh(self.state, later, fetch=lambda: None)
        self.assertEqual((failed['latest'], failed['ok']), ('4.3.0', False))
        asked = mock.Mock(return_value='4.4.0')
        playbook_update.refresh(self.state, later + playbook_update.RETRY_S - 1, fetch=asked)
        asked.assert_not_called()
        playbook_update.refresh(self.state, later + playbook_update.RETRY_S, fetch=asked)
        asked.assert_called_once()

    def test_force_asks_whatever_the_cache_says(self) -> None:
        playbook_update.refresh(self.state, NOW, fetch=lambda: '4.3.0')
        asked = mock.Mock(return_value='4.4.0')
        playbook_update.refresh(self.state, NOW + 1, force=True, fetch=asked)
        asked.assert_called_once()

    def test_an_unwritable_state_directory_loses_the_answer_not_the_call(self) -> None:
        blocked = os.path.join(self.state, 'file')
        with open(blocked, 'w') as handle:
            handle.write('x')
        cache = playbook_update.refresh(os.path.join(blocked, 'inside'), NOW, fetch=lambda: '4.3.0')
        self.assertEqual(cache['latest'], '4.3.0')

    def test_a_corrupt_cache_reads_as_empty(self) -> None:
        for body in ('', 'x', '[]', '5'):
            with self.subTest(body=body):
                with open(os.path.join(self.state, playbook_update.CACHE_FILE), 'w') as handle:
                    handle.write(body)
                self.assertEqual(playbook_update.read_cache(self.state), {})


class TestStatus(Case):

    def _known(self, latest: str) -> None:
        playbook_update.refresh(self.state, NOW, fetch=lambda: latest)

    def test_a_newer_release_is_available(self) -> None:
        self._known('4.3.0')
        current = playbook_update.status(self.state, self.root)
        self.assertEqual((current.installed, current.latest, current.available), ('4.2.1', '4.3.0', True))

    def test_the_same_or_an_older_release_is_not(self) -> None:
        for latest in ('4.2.1', '4.2.0', '3.9.9'):
            with self.subTest(latest=latest):
                self._known(latest)
                self.assertFalse(playbook_update.status(self.state, self.root).available)

    def test_versions_compare_as_numbers_not_text(self) -> None:
        self.manifest('4.9.0')
        self._known('4.10.0')
        self.assertTrue(playbook_update.status(self.state, self.root).available)

    def test_nothing_known_is_not_an_update(self) -> None:
        current = playbook_update.status(self.state, self.root)
        self.assertEqual((current.latest, current.available), (None, False))

    def test_an_unreadable_manifest_never_claims_an_update(self) -> None:
        self._known('4.3.0')
        self.assertFalse(playbook_update.status(self.state, os.path.join(self.root, 'missing')).available)

    def test_a_version_is_announced_once(self) -> None:
        self._known('4.3.0')
        self.assertEqual(playbook_update.unannounced(self.state, self.root), '4.3.0')
        playbook_update.mark_announced('4.3.0', self.state)
        self.assertIsNone(playbook_update.unannounced(self.state, self.root))
        self._known_again = playbook_update.refresh(self.state, NOW + 1, force=True, fetch=lambda: '4.4.0')
        self.assertEqual(playbook_update.unannounced(self.state, self.root), '4.4.0')


class TestApplyUpdate(unittest.TestCase):

    def _runs(self, *answers: tuple[int, str]):
        queue = list(answers)
        seen: list[list[str]] = []

        def run(argv: list[str], _timeout: float) -> tuple[int, str]:
            seen.append(argv)
            return queue.pop(0)
        return run, seen

    def test_the_marketplace_is_refreshed_then_the_plugin_updated(self) -> None:
        run, seen = self._runs((0, 'ok'), (0, json.dumps({'outcome': 'updated', 'message': 'Updated to 4.3.0'})))
        outcome = playbook_update.apply_update(run=run)
        self.assertEqual(outcome, playbook_update.Outcome(True, 'Updated to 4.3.0'))
        self.assertEqual(seen[0], ['claude', 'plugin', 'marketplace', 'update', 'engineering-playbook'])
        self.assertEqual(seen[1], ['claude', 'plugin', 'update', 'engineering-playbook@engineering-playbook', '--json'])

    def test_a_failed_marketplace_refresh_stops_there(self) -> None:
        run, seen = self._runs((1, 'Marketplace not found'))
        outcome = playbook_update.apply_update(run=run)
        self.assertEqual(outcome, playbook_update.Outcome(False, 'Marketplace not found'))
        self.assertEqual(len(seen), 1)

    def test_a_plugin_the_marketplace_does_not_track_says_why(self) -> None:
        failure = json.dumps({'outcome': 'failed', 'message': 'Plugin "x" not found', 'failureCode': 'not_found'})
        run, _ = self._runs((0, 'ok'), (1, failure))
        outcome = playbook_update.apply_update(run=run)
        self.assertFalse(outcome.ok)
        self.assertIn('not installed from the marketplace', outcome.text)

    def test_a_failed_outcome_with_exit_zero_is_still_a_failure(self) -> None:
        run, _ = self._runs((0, 'ok'), (0, json.dumps({'outcome': 'failed', 'message': 'boom'})))
        self.assertEqual(playbook_update.apply_update(run=run), playbook_update.Outcome(False, 'boom'))

    def test_a_timeout_or_a_missing_cli_is_a_failure_with_a_reason(self) -> None:
        for code, said in ((124, 'still running after 120s; stopped'), (127, 'No such file: claude')):
            with self.subTest(code=code):
                run, _ = self._runs((code, said))
                outcome = playbook_update.apply_update(run=run)
                self.assertEqual((outcome.ok, outcome.text), (False, said))

    def test_a_long_message_is_cut(self) -> None:
        run, _ = self._runs((1, 'x' * 5000))
        self.assertLessEqual(len(playbook_update.apply_update(run=run).text), 401)

    @unittest.skipUnless(shutil.which('claude'), 'needs the claude CLI')
    def test_the_real_cli_answers_the_shape_the_parser_reads(self) -> None:
        # A plugin that cannot exist: nothing is updated, and the CLI's own
        # failure line is the contract `apply_update` reads.
        done = subprocess.run(['claude', 'plugin', 'update', 'no-such-plugin-xyz@no-such-market', '--json'],
                              capture_output=True, text=True, timeout=60, check=False)
        line = playbook_update._json_line(done.stdout or done.stderr)
        self.assertEqual(line.get('outcome'), 'failed')
        self.assertIn('failureCode', line)
        self.assertIsInstance(line.get('message'), str)


class TestSettingsScript(Case):

    def setUp(self) -> None:
        super().setUp()
        for target in (config, session_start):
            patcher = mock.patch.object(target, 'STATE_DIR', self.state)
            patcher.start()
            self.addCleanup(patcher.stop)
        with open(playbook_core.prefs_path(self.state), 'w') as handle:
            json.dump({'language': 'en'}, handle)

    def test_update_check_is_a_setting_in_the_updates_group_on_by_default(self) -> None:
        self.assertIs(playbook_core.PREFS_DEFAULTS['update_check'], True)
        self.assertIn('update_check', config.HARNESS_SETTINGS['claude'])
        self.assertEqual(config.GROUPS['updates'], ('update_check', 'reports'))
        for harness in ('codex', 'antigravity'):
            self.assertNotIn('update_check', config.HARNESS_SETTINGS[harness])

    def test_the_snapshot_carries_the_version_and_the_update(self) -> None:
        playbook_update.refresh(self.state, NOW, fetch=lambda: '99.0.0')
        shown = config.snapshot(playbook_core.load_prefs(self.state))
        self.assertEqual(shown['version'], playbook_update.installed_version())
        self.assertEqual(shown['update'], {'available': True, 'latest': '99.0.0'})
        self.assertIn('update_now', shown['ui'])
        self.assertEqual({g['key'] for g in shown['groups']} & {'updates'}, {'updates'})

    def test_update_check_turns_off_and_on_with_the_effect_said(self) -> None:
        for value, key in (('off', 'effect_update_check_off'), ('on', 'effect_update_check_on')):
            with self.subTest(value=value):
                prefs, error = playbook_core.set_pref(self.state, 'update_check', value)
                self.assertIsNone(error)
                self.assertIs(prefs['update_check'], value == 'on')
                self.assertEqual(config.effect_note('update_check', value == 'on'),
                                 playbook_core.playbook_i18n.text('en', key))

    def test_a_check_says_whether_a_newer_release_exists(self) -> None:
        with mock.patch.object(playbook_update, 'fetch_latest', return_value='99.0.0'), \
                mock.patch.object(playbook_update, 'PLUGIN_ROOT', self.root):
            answer = config.update_answer('check', 'en')
        self.assertEqual((answer['is_error'], answer['reload']), (False, False))
        self.assertIn('99.0.0', answer['text'])
        with mock.patch.object(playbook_update, 'fetch_latest', return_value='4.2.1'), \
                mock.patch.object(playbook_update, 'PLUGIN_ROOT', self.root):
            self.assertIn('Up to date', config.update_answer('check', 'en')['text'])

    def test_a_copy_newer_than_the_latest_release_says_so_instead_of_calling_itself_the_newest(self) -> None:
        with mock.patch.object(playbook_update, 'fetch_latest', return_value='4.2.0'), \
                mock.patch.object(playbook_update, 'PLUGIN_ROOT', self.root):
            answer = config.update_answer('check', 'en')
        self.assertFalse(answer['is_error'])
        self.assertIn('newer than the latest release (4.2.0)', answer['text'])
        self.assertNotIn('Up to date', answer['text'])

    def test_a_check_that_cannot_reach_github_says_so_instead_of_repeating_an_old_answer(self) -> None:
        playbook_update.refresh(self.state, NOW, fetch=lambda: '4.2.1')
        with mock.patch.object(playbook_update, 'fetch_latest', return_value=None):
            answer = config.update_answer('check', 'en')
        self.assertTrue(answer['is_error'])
        self.assertIn('Could not reach GitHub', answer['text'])

    def test_an_update_that_worked_asks_for_the_reload_and_has_a_manual_text(self) -> None:
        with mock.patch.object(playbook_update, 'apply_update',
                               return_value=playbook_update.Outcome(True, 'Updated')):
            answer = config.update_answer('apply', 'en')
        self.assertEqual((answer['is_error'], answer['reload']), (False, True))
        self.assertIn('/reload-plugins', answer['manual_text'])

    def test_an_update_that_failed_names_the_reason_and_reloads_nothing(self) -> None:
        with mock.patch.object(playbook_update, 'apply_update',
                               return_value=playbook_update.Outcome(False, 'not found')):
            answer = config.update_answer('apply', 'pt-br')
        self.assertEqual((answer['is_error'], answer['reload']), (True, False))
        self.assertIn('falhou', answer['text'])
        self.assertIn('not found', answer['text'])

    def test_the_update_action_prints_json_for_the_panel(self) -> None:
        with mock.patch.object(config, 'update_answer', return_value={
                'text': 't', 'manual_text': 'm', 'is_error': False, 'reload': False}), \
                mock.patch('builtins.print') as shown:
            self.assertEqual(config.main(['update', 'check', '--json']), 0)
        self.assertEqual(json.loads(shown.call_args.args[0])['text'], 't')
        self.assertEqual(config.main(['update']), 2)
        self.assertEqual(config.main(['update', 'sideways']), 2)


class TestSessionStartNotice(Case):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(session_start, 'STATE_DIR', self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, value in ((playbook_update, self.state),):
            self.addCleanup(setattr, target, 'STATE_DIR', target.STATE_DIR)
        with open(playbook_core.prefs_path(self.state), 'w') as handle:
            json.dump({'language': 'en'}, handle)
        playbook_update.refresh(self.state, NOW, fetch=lambda: '99.0.0')
        root = mock.patch.object(playbook_update, 'PLUGIN_ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)

    def _notice(self) -> str:
        with mock.patch.object(playbook_update, 'spawn_refresh') as spawn:
            text = session_start._update_notice()
        self.spawned = spawn.called
        return text

    def test_a_newer_release_is_said_once_and_then_not_again(self) -> None:
        first = self._notice()
        self.assertIn('99.0.0', first)
        self.assertIn('4.2.1', first)
        self.assertEqual(self._notice(), '')

    def test_the_notice_is_in_the_users_language(self) -> None:
        with open(playbook_core.prefs_path(self.state), 'w') as handle:
            json.dump({'language': 'pt-br'}, handle)
        self.assertIn('está disponível', self._notice())

    def test_with_the_check_off_nothing_is_said_and_nothing_is_asked(self) -> None:
        with open(playbook_core.prefs_path(self.state), 'w') as handle:
            json.dump({'language': 'en', 'update_check': False}, handle)
        self.assertEqual(self._notice(), '')
        self.assertFalse(self.spawned)

    def test_the_network_is_asked_from_a_detached_process_and_only_when_due(self) -> None:
        with mock.patch.object(playbook_update.time, 'time', return_value=NOW + 1):
            self._notice()
        self.assertFalse(self.spawned)
        with mock.patch.object(playbook_update.time, 'time', return_value=NOW), \
                mock.patch.object(session_start.time, 'time', return_value=NOW + playbook_update.TTL_S + 1):
            self._notice()
        self.assertTrue(self.spawned)

    def test_no_newer_release_says_nothing(self) -> None:
        playbook_update.refresh(self.state, NOW + 5, force=True, fetch=lambda: '4.2.1')
        self.assertEqual(self._notice(), '')


class TestFailureText(unittest.TestCase):

    def test_a_command_that_fails_with_output_only_on_stdout_says_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            code, said = temporary_data._run('echo "nothing to commit"; exit 1', folder)
        self.assertEqual((code, said), (1, 'nothing to commit'))

    def test_stderr_wins_over_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(temporary_data._run('echo out; echo err >&2; exit 2', folder), (2, 'err'))

    def test_a_silent_failure_says_that_it_wrote_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(temporary_data, '_language', return_value='en'):
            code, said = temporary_data._run('exit 1', folder)
        self.assertEqual((code, said), (1, 'the command wrote nothing'))


class TestWaitingNotice(Case):

    def setUp(self) -> None:
        super().setUp()
        for name, value in (('STATE_DIR', self.state), ('_SESSION_ID', 'sess-1'), ('_AGENT_ID', '')):
            patcher = mock.patch.object(context_guard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with open(playbook_core.prefs_path(self.state), 'w') as handle:
            json.dump({'language': 'en'}, handle)

    def test_a_queued_command_is_shown_to_the_person_once(self) -> None:
        playbook_core.consent_request(self.state, 'sess-1', 'rm -rf build', 'delete_tree', self.root)
        first = context_guard._waiting_notice()
        self.assertIn('#1', first)
        self.assertIn('rm -rf build', first)
        self.assertEqual(context_guard._waiting_notice(), '')

    def test_a_second_command_is_shown_without_repeating_the_first(self) -> None:
        playbook_core.consent_request(self.state, 'sess-1', 'rm -rf a', 'delete_tree', self.root)
        context_guard._waiting_notice()
        playbook_core.consent_request(self.state, 'sess-1', 'rm -rf b', 'delete_tree', self.root)
        again = context_guard._waiting_notice()
        self.assertIn('rm -rf b', again)
        self.assertNotIn('rm -rf a', again)

    def test_the_pending_command_is_still_pending_after_it_was_shown(self) -> None:
        playbook_core.consent_request(self.state, 'sess-1', 'rm -rf build', 'delete_tree', self.root)
        context_guard._waiting_notice()
        self.assertEqual(len(playbook_core.consent_pending(self.state, 'sess-1')), 1)

    def test_nothing_queued_or_a_subagent_says_nothing(self) -> None:
        self.assertEqual(context_guard._waiting_notice(), '')
        playbook_core.consent_request(self.state, 'sess-1', 'rm -rf build', 'delete_tree', self.root)
        with mock.patch.object(context_guard, '_AGENT_ID', 'sub-1'):
            self.assertEqual(context_guard._waiting_notice(), '')

    def test_the_line_follows_the_users_language(self) -> None:
        with open(playbook_core.prefs_path(self.state), 'w') as handle:
            json.dump({'language': 'pt-br'}, handle)
        playbook_core.consent_request(self.state, 'sess-1', 'rm -rf build', 'delete_tree', self.root)
        self.assertIn('Aguardando a sua resposta', context_guard._waiting_notice())


if __name__ == '__main__':
    unittest.main()
