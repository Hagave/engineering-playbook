"""The answer from the button above the chat, and the Temporary data panel
(scripts/temporary_data.py, the store in hooks/playbook_core.py).

Commands here run for real, in temporary folders: `approve` and `remove`
exist to run them.
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

_SPEC = importlib.util.spec_from_file_location(
    'temporary_data', os.path.join(_ROOT, 'scripts', 'temporary_data.py'))
temporary_data = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(temporary_data)

SESSION = 'sess-1'


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tempfile.mkdtemp()
        self.project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, True)
        self.addCleanup(shutil.rmtree, self.project, True)
        patcher = mock.patch.object(temporary_data, 'STATE_DIR', self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._prefs({'language': 'en'})

    def _prefs(self, prefs: dict) -> None:
        with open(playbook_core.prefs_path(self.state), 'w') as f:
            json.dump(prefs, f)

    def _queue(self, command: str, effect: str = 'delete_tree') -> int:
        return playbook_core.consent_request(self.state, SESSION, command, effect, self.project)

    def _build(self) -> str:
        build = os.path.join(self.project, 'build')
        os.makedirs(os.path.join(build, 'out'))
        return build

    def _pending_ids(self) -> list:
        return [entry['id'] for entry in playbook_core.consent_pending(self.state, SESSION)]


class TestApprove(_Case):
    def test_runs_the_exact_command_in_the_shell_folder_once(self):
        build = self._build()
        number = self._queue('rm -rf build')
        answer = temporary_data.approve(SESSION, self.project, number)
        self.assertFalse(answer['is_error'])
        self.assertFalse(os.path.exists(build))
        self.assertIn(f'approved #{number}', answer['note'])
        self.assertIn('Do not run it again', answer['note'])
        self.assertEqual(self._pending_ids(), [])
        again = temporary_data.approve(SESSION, self.project, number)
        self.assertEqual(again['note'], '')

    def test_a_failure_is_reported_to_both(self):
        number = self._queue('rm -r "$UNSET_FOR_THE_TEST"')
        answer = temporary_data.approve(SESSION, self.project, number)
        self.assertTrue(answer['is_error'])
        self.assertIn('failed', answer['note'])

    def test_a_cut_command_is_never_run_and_stays_waiting(self):
        long_command = 'rm -rf build ' + ' '.join(['x'] * 1200)
        build = self._build()
        number = self._queue(long_command)
        answer = temporary_data.approve(SESSION, self.project, number)
        self.assertTrue(answer['is_error'])
        self.assertTrue(os.path.exists(build))
        self.assertEqual(self._pending_ids(), [number])

    def test_a_number_answered_in_the_chat_is_gone(self):
        number = self._queue('rm -rf build')
        playbook_core.consent_mark_turn_end(self.state, SESSION)
        playbook_core.consent_answer(self.state, SESSION, 'pode apagar')
        answer = temporary_data.approve(SESSION, self.project, number)
        self.assertEqual(answer['note'], '')

    def test_the_chat_cannot_approve_what_the_button_declined(self):
        number = self._queue('rm -rf build')
        playbook_core.consent_mark_turn_end(self.state, SESSION)
        temporary_data.decline(SESSION, self.project, number)
        answer = playbook_core.consent_answer(self.state, SESSION, 'pode apagar')
        self.assertEqual(answer.approved, [])


class TestDecline(_Case):
    def test_a_deletion_is_kept_for_the_project(self):
        build = self._build()
        number = self._queue('rm -rf build')
        answer = temporary_data.decline(SESSION, self.project, number)
        self.assertTrue(os.path.exists(build))
        self.assertIn('Temporary data', answer['note'])
        items = playbook_core.temporary_data_items(self.state, self.project)
        self.assertEqual([(i['command'], i['cwd'], i['effect']) for i in items],
                         [('rm -rf build', self.project, 'delete_tree')])
        self.assertEqual(self._pending_ids(), [])

    def test_what_is_not_a_deletion_is_dropped(self):
        for effect in ('reset_hard', 'force_push', 'discard_changes', 'stash_drop', 'sql'):
            with self.subTest(effect=effect):
                number = self._queue(f'echo {effect}', effect)
                answer = temporary_data.decline(SESSION, self.project, number)
                self.assertIn('do not run it', answer['note'])
        self.assertEqual(playbook_core.temporary_data_items(self.state, self.project), [])

    def test_with_the_panel_off_a_deletion_is_dropped(self):
        self._prefs({'language': 'en', 'temporary_data': False})
        number = self._queue('rm -rf build')
        temporary_data.decline(SESSION, self.project, number)
        self.assertEqual(playbook_core.temporary_data_items(self.state, self.project), [])

    def test_items_are_per_project(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        temporary_data.decline(SESSION, self.project, self._queue('rm -rf build'))
        self.assertEqual(playbook_core.temporary_data_items(self.state, other), [])


class TestRemove(_Case):
    def _keep(self) -> str:
        return playbook_core.temporary_data_add(
            self.state, self.project, command='rm -rf build', cwd=self.project,
            effect='delete_tree', origin=SESSION)['id']

    def test_removes_from_disk_and_from_the_panel(self):
        build = self._build()
        answer = temporary_data.remove(self.project, self._keep())
        self.assertFalse(answer['is_error'])
        self.assertFalse(os.path.exists(build))
        self.assertEqual(playbook_core.temporary_data_items(self.state, self.project), [])

    def test_a_failure_keeps_the_item(self):
        item = playbook_core.temporary_data_add(
            self.state, self.project, command='false', cwd=self.project,
            effect='delete_tree', origin=SESSION)
        answer = temporary_data.remove(self.project, item['id'])
        self.assertTrue(answer['is_error'])
        self.assertEqual(len(playbook_core.temporary_data_items(self.state, self.project)), 1)

    def test_an_item_id_cannot_name_another_file(self):
        with self.assertRaises(ValueError):
            playbook_core.temporary_data_drop(self.state, self.project, '../../prefs')

    def test_a_file_that_is_not_an_item_is_ignored(self):
        folder = playbook_core.temporary_data_dir(self.state, self.project)
        os.makedirs(folder)
        with open(os.path.join(folder, 'a1b2c3d4e5f6.json'), 'w') as f:
            json.dump({'id': 'a1b2c3d4e5f6'}, f)
        with open(os.path.join(folder, 'b1b2c3d4e5f6.json'), 'w') as f:
            json.dump({'id': 'b1b2c3d4e5f6', 'command': 'x', 'cwd': '/', 'ts': 'yesterday'}, f)
        self.assertEqual(playbook_core.temporary_data_items(self.state, self.project), [])
        self.assertEqual(temporary_data.show(SESSION, self.project)['items'], [])


class TestShow(_Case):
    def test_lists_three_waiting_and_counts_the_rest_in_the_language(self):
        self._prefs({'language': 'pt-br'})
        for index in range(5):
            os.makedirs(os.path.join(self.project, f'build{index}'))
            self._queue(f'rm -rf build{index}')
        temporary_data.decline(SESSION, self.project, 5)
        view = temporary_data.show(SESSION, self.project)
        self.assertEqual([entry['id'] for entry in view['pending']], [1, 2, 3])
        self.assertEqual(view['more'], '+1 na fila')
        self.assertEqual(view['open'], 'Temporary data (1)')
        self.assertEqual(view['ui']['tmp_approve'], 'Aprovar')
        self.assertIn('apaga arquivos e diretórios recursivamente', view['pending'][0]['text'])

    def test_a_row_says_how_long_it_has_been_waiting(self):
        self._build()
        self._queue('rm -rf build')
        queued = playbook_core.consent_pending(self.state, SESSION)[0]['ts']
        for seconds, shown in ((5, '(queued <1 min ago)'), (90, '(queued 1 min ago)'),
                               (59 * 60, '(queued 59 min ago)'), (3 * 3600 + 5, '(queued 3 h ago)'),
                               (2 * 86400 + 5, '(queued 2 d ago)')):
            with self.subTest(seconds=seconds), mock.patch.object(
                    temporary_data.time, 'time', return_value=queued + seconds):
                self.assertTrue(temporary_data.show(SESSION, self.project)['pending'][0]['text']
                                .endswith(shown))

    def test_the_age_follows_the_language(self):
        self._build()
        self._queue('rm -rf build')
        for language, expected in (('pt-br', 'na fila há'), ('es', 'en cola hace')):
            with self.subTest(language=language):
                self._prefs({'language': language})
                self.assertIn(expected, temporary_data.show(SESSION, self.project)['pending'][0]['text'])

    def test_a_row_with_no_readable_time_shows_no_age(self):
        self.assertEqual(temporary_data._queued_for('en', {'ts': 'yesterday'}), '')
        self.assertEqual(temporary_data._queued_for('en', {}), '')
        self.assertEqual(temporary_data._queued_for('en', {'ts': True}), '')


class TestSuperseded(_Case):
    """A queued removal whose targets are all gone was resolved another way."""

    def _gone(self, command: str, effect: str = 'delete_tree') -> bool:
        number = self._queue(command, effect)
        temporary_data.show(SESSION, self.project)
        return number not in self._pending_ids()

    def test_a_deletion_of_what_is_already_gone_leaves_the_queue(self):
        for command in ('rm -rf build', 'rm -r build/out', 'rm -rf -- build', 'sudo rm -rf build',
                        'rm -rf build cache', f'rm -rf {os.path.join("/", "no", "such", "dir")}',
                        'rm -rf ./build/../build', 'rm -rf build && rm -rf cache'):
            with self.subTest(command=command):
                self.assertTrue(self._gone(command))

    def test_a_removed_worktree_leaves_the_queue(self):
        wt = os.path.join(self.project, 'wt')
        self.assertTrue(self._gone(f'git worktree remove --force {wt}', 'worktree_remove'))
        self.assertTrue(self._gone('git worktree remove ../no-such-worktree', 'worktree_remove'))

    def test_one_target_still_there_keeps_it_waiting(self):
        self._build()
        for command in ('rm -rf build', 'rm -rf build cache', 'rm -rf cache build', 'rm -rf -- build'):
            with self.subTest(command=command):
                self.assertFalse(self._gone(command))

    def test_dot_dot_through_a_link_is_read_the_way_the_shell_reads_it(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        os.makedirs(os.path.join(base, 'sub', 'inner'))
        os.makedirs(os.path.join(base, 'marker'))
        os.symlink(os.path.join(base, 'sub', 'inner'), os.path.join(self.project, 'link'))
        self.assertFalse(self._gone('rm -rf link/../../marker'))

    def test_a_dangling_symlink_still_counts_as_there(self):
        os.symlink(os.path.join(self.project, 'nowhere'), os.path.join(self.project, 'link'))
        self.assertFalse(self._gone('rm -rf link'))

    def test_what_the_words_do_not_name_is_never_superseded(self):
        for command in ('rm -rf $BUILD_DIR', 'rm -rf "$(pwd)/build"', 'rm -rf build/*', 'rm -rf bu?ld',
                        'rm -rf ~/no-such-thing-for-the-test/*', 'rm -rf build[12]',
                        'find . -name build -exec rm -rf {} +', 'ls build | xargs rm -rf',
                        'cd build && rm -rf out', 'rm -rf build && echo done',
                        'git -C other worktree remove wt', 'rm -rf'):
            with self.subTest(command=command):
                self.assertFalse(self._gone(command))

    def test_only_a_deletion_can_be_superseded(self):
        for effect in ('reset_hard', 'force_push', 'clean', 'sql'):
            with self.subTest(effect=effect):
                self.assertFalse(self._gone('rm -rf build', effect))

    def test_a_cut_command_stays(self):
        self.assertFalse(self._gone('rm -rf build ' + ' '.join(['x'] * 1200)))

    def test_a_queue_entry_with_no_folder_is_never_superseded(self):
        playbook_core.consent_request(self.state, SESSION, 'rm -rf build', 'delete_tree', '')
        playbook_core.consent_request(self.state, SESSION, 'rm -rf cache', 'delete_tree', 'relative')
        self.assertEqual(playbook_core.consent_drop_superseded(self.state, SESSION), [])
        self.assertEqual(len(self._pending_ids()), 2)

    def test_pressing_approve_on_one_runs_nothing_and_says_why(self):
        number = self._queue('rm -rf build')
        answer = temporary_data.approve(SESSION, self.project, number)
        self.assertFalse(answer['is_error'])
        self.assertIn('already gone', answer['text'])
        self.assertIn('nothing ran', answer['note'])
        self.assertEqual(self._pending_ids(), [])

    def test_an_approve_that_came_after_the_target_returned_still_runs(self):
        build = self._build()
        number = self._queue('rm -rf build')
        answer = temporary_data.approve(SESSION, self.project, number)
        self.assertFalse(os.path.exists(build))
        self.assertIn('ran it', answer['note'])

    def test_only_the_superseded_ones_are_dropped(self):
        self._build()
        gone = self._queue('rm -rf cache')
        kept = self._queue('rm -rf build')
        self.assertEqual([e['id'] for e in playbook_core.consent_drop_superseded(self.state, SESSION)], [gone])
        self.assertEqual(self._pending_ids(), [kept])

    def test_the_stop_notice_does_not_announce_one(self):
        import context_guard as cg
        self._queue('rm -rf cache')
        self._build()
        kept = self._queue('rm -rf build')
        with mock.patch.object(cg, 'STATE_DIR', self.state), \
                mock.patch.object(cg, '_SESSION_ID', SESSION), mock.patch.object(cg, '_AGENT_ID', ''):
            notice = cg._waiting_notice()
        self.assertIn(f'#{kept}', notice)
        self.assertNotIn('cache', notice)

    def test_the_prompt_hook_tells_the_model_what_it_took_out(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        state = os.path.join(home, '.claude', 'hooks')
        os.makedirs(state)
        number = playbook_core.consent_request(state, SESSION, 'rm -rf cache', 'delete_tree', self.project)
        ran = subprocess.run(
            [sys.executable, os.path.join(_ROOT, 'hooks', 'consent_guard.py')],
            input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': SESSION,
                              'prompt': 'continue'}),
            capture_output=True, text=True, env={**os.environ, 'HOME': home}, check=False)
        context = json.loads(ran.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn(f'#{number} `rm -rf cache`', context)
        self.assertIn('already gone', context)
        self.assertEqual(playbook_core.consent_pending(state, SESSION), [])


class TestCreatedTemporaries(unittest.TestCase):
    def setUp(self) -> None:
        self.cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cwd, True)

    def _made(self, command: str, stdout: str = '', scratch: str = '') -> list:
        return [(one.what_key, one.name, one.command, one.target_kind)
                for one in playbook_core.created_temporaries(command, stdout, self.cwd, scratch)]

    def test_a_worktree_is_read_with_its_path_resolved(self):
        sub = os.path.join(self.cwd, 'repo')
        expected = os.path.join(self.cwd, 'wt')
        for command in ('git worktree add ../wt', 'git worktree add -b feat ../wt main',
                        f'git -C {sub} worktree add {expected}'):
            with self.subTest(command=command):
                made = self._made(f'cd {sub} && {command}')
                self.assertEqual(made, [('tmp_what_worktree', expected,
                                         f'git worktree remove --force {expected}', 'path')])

    def test_named_docker_objects_are_read(self):
        self.assertEqual(self._made('docker run -d --name pg -e X=1 postgres:17'),
                         [('tmp_what_container', 'pg', 'docker rm -f pg', 'container')])
        self.assertEqual(self._made('sudo docker run --name=pg postgres')[0][1], 'pg')
        self.assertEqual(self._made('docker volume create data')[0][2], 'docker volume rm data')
        self.assertEqual(self._made('docker network create lab')[0][2], 'docker network rm lab')

    def test_what_removes_itself_or_is_not_named_is_not_read(self):
        for command in ('docker run --rm --name pg postgres', 'docker run postgres',
                        'docker run --name "$N" postgres', 'git worktree add "$WT"',
                        'git worktree list', 'D=$(mktemp -d); echo "$D"', 'echo mktemp -d'):
            with self.subTest(command=command):
                self.assertEqual(self._made(command, '/tmp/tmp.abc\n'), [])

    def test_a_lone_mktemp_is_read_from_its_output(self):
        self.assertEqual(self._made('mktemp -d', '/tmp/tmp.abc\n'),
                         [('tmp_what_tempdir', '/tmp/tmp.abc', 'rm -rf /tmp/tmp.abc', 'path')])
        self.assertEqual(self._made('mktemp', '/tmp/tmp.f\n')[0][2], 'rm -f /tmp/tmp.f')
        self.assertEqual(self._made('mktemp -d', 'not a path'), [])

    def test_the_session_scratchpad_is_left_out(self):
        self.assertEqual(self._made('git worktree add scratch/wt', scratch=self.cwd), [])


class TestCreatedInThePanel(_Case):
    def _note(self, command: str, stdout: str = '') -> int:
        made = playbook_core.created_temporaries(command, stdout, self.project)
        return playbook_core.temporary_data_note_created(self.state, self.project, made, SESSION)

    def test_shown_once_the_turn_ends_and_kept_once_per_target(self):
        wt = os.path.join(self.project, 'wt')
        os.makedirs(wt)
        self.assertEqual(self._note('git worktree add wt'), 1)
        self.assertEqual(self._note('git worktree add wt'), 0)
        self.assertEqual(temporary_data.show(SESSION, self.project)['items'], [])
        self.assertEqual(len(temporary_data.show('another-session', self.project)['items']), 1)
        playbook_core.consent_mark_turn_end(self.state, SESSION)
        items = temporary_data.show(SESSION, self.project)['items']
        self.assertEqual(len(items), 1)
        self.assertIn(f'git worktree {wt}', items[0]['text'])
        self.assertIn('made by Claude', items[0]['text'])

    def test_an_item_whose_target_is_gone_leaves_the_panel(self):
        wt = os.path.join(self.project, 'wt')
        os.makedirs(wt)
        self._note('git worktree add wt')
        playbook_core.consent_mark_turn_end(self.state, SESSION)
        os.rmdir(wt)
        self.assertEqual(temporary_data.show(SESSION, self.project)['items'], [])
        self.assertEqual(playbook_core.temporary_data_items(self.state, self.project), [])


class TestTheHookKeepsWhatACommandMade(unittest.TestCase):
    """hooks/context_guard.py run as Claude Code runs it, on PostToolUse."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, 'home')
        self.project = os.path.join(self.root, 'proj')
        os.makedirs(os.path.join(self.home, '.claude', 'hooks'))
        os.makedirs(os.path.join(self.project, 'sub'))
        self.transcript = os.path.join(self.root, 't.jsonl')
        open(self.transcript, 'w').close()

    def _post(self, command: str, cwd: str, prefs: dict | None = None) -> None:
        state = os.path.join(self.home, '.claude', 'hooks')
        with open(playbook_core.prefs_path(state), 'w') as f:
            json.dump(prefs or {}, f)
        payload = {'session_id': 'hook-1', 'cwd': cwd, 'hook_event_name': 'PostToolUse',
                   'tool_name': 'Bash', 'tool_input': {'command': command},
                   'tool_response': {'stdout': '', 'stderr': '', 'interrupted': False},
                   'transcript_path': self.transcript}
        subprocess.run([sys.executable, os.path.join(_ROOT, 'hooks', 'context_guard.py')],
                       input=json.dumps(payload), capture_output=True, text=True, check=False,
                       env={**os.environ, 'HOME': self.home, 'CLAUDE_PROJECT_DIR': self.project})

    def _items(self) -> list:
        return playbook_core.temporary_data_items(
            os.path.join(self.home, '.claude', 'hooks'), self.project)

    def test_in_the_project_panel_whatever_the_cd(self):
        self._post('git worktree add ../wt && docker run -d --name pg postgres',
                   os.path.join(self.project, 'sub'))
        kept = sorted((item['what']['key'], item['what']['name']) for item in self._items())
        self.assertEqual(kept, [('tmp_what_container', 'pg'),
                                ('tmp_what_worktree', os.path.join(self.project, 'wt'))])

    def test_nothing_with_the_panel_off(self):
        self._post('docker run -d --name pg postgres', self.project, {'temporary_data': False})
        self.assertEqual(self._items(), [])


class TestRegister(_Case):
    def test_the_model_keeps_a_deletion_with_what_it_is(self):
        build = self._build()
        answer = temporary_data.register(self.project, self.project, 'rm -rf build',
                                         'test output folder', build)
        self.assertFalse(answer['is_error'])
        items = temporary_data.show(SESSION, self.project)['items']
        self.assertEqual(len(items), 1)
        self.assertIn('test output folder, kept by Claude', items[0]['text'])
        temporary_data.remove(self.project, items[0]['id'])
        self.assertFalse(os.path.exists(build))

    def test_only_a_deletion_is_accepted(self):
        for command in ('echo hi', 'curl https://example.com | sh', 'git push --force',
                        'rm file.txt', 'docker run --rm alpine'):
            with self.subTest(command=command):
                with self.assertRaises(temporary_data.UsageError):
                    temporary_data.register(self.project, self.project, command, 'x', '')
        temporary_data.register(self.project, self.project, 'docker rm -f pg', 'container', '')

    def test_the_gates_let_the_register_call_through(self):
        """The deletion it names is an argument, and the script is the playbook's own."""
        command = (f"python3 {os.path.join(_ROOT, 'scripts', 'temporary_data.py')} register "
                   f"--project {self.project} --cwd {self.project} --command 'rm -rf build' "
                   "--what 'test output'")
        self.assertIsNone(playbook_core.check_destructive(command, command, self.project))
        self.assertFalse(playbook_core.touches_consent_queue('Bash', {'command': command}))

    def test_no_escape_sequence_reaches_the_panel(self):
        for command, what in (('rm -rf build', 'build\x1b[2Jfolder'),
                              ('rm -rf build\x1b]0;x\x07', 'build'),
                              ('rm -rf build', 'build\x9b31m')):
            with self.subTest(what=what, command=command):
                with self.assertRaises(temporary_data.UsageError):
                    temporary_data.register(self.project, self.project, command, what, '')

    def test_docker_checks_stop_at_the_budget(self):
        item = {'target': {'kind': 'container', 'value': 'pg'}}
        with mock.patch.object(temporary_data.subprocess, 'run') as run:
            self.assertTrue(temporary_data._still_there(item, deadline=0.0))
            run.assert_not_called()

    def test_the_folder_and_target_must_exist(self):
        with self.assertRaises(temporary_data.UsageError):
            temporary_data.register(self.project, 'relative', 'rm -rf build', 'x', '')
        with self.assertRaises(temporary_data.UsageError):
            temporary_data.register(self.project, self.project, 'rm -rf build', 'x',
                                    os.path.join(self.project, 'missing'))


class TestArguments(unittest.TestCase):
    def test_a_session_id_cannot_point_elsewhere(self):
        with mock.patch('sys.stderr'):
            self.assertEqual(temporary_data.main(
                ['show', '--session', '../../x', '--project', '/tmp']), 2)

    def test_the_project_must_be_absolute(self):
        with mock.patch('sys.stderr'):
            self.assertEqual(temporary_data.main(
                ['show', '--session', 's', '--project', 'rel']), 2)


class TestTheModelCannotTouchTheStores(unittest.TestCase):
    def test_writes_and_commands_naming_temporary_data_are_flagged(self):
        folder = os.path.expanduser('~/.claude/hooks/temporary-data/x/a1b2c3d4e5f6.json')
        self.assertTrue(playbook_core.touches_consent_queue('Write', {'file_path': folder}))
        self.assertTrue(playbook_core.touches_consent_queue(
            'Bash', {'command': f'cat > {folder} <<EOF\n{{}}\nEOF'}))
        self.assertFalse(playbook_core.touches_consent_queue('Write', {'file_path': '/w/src/a.py'}))


class TestSetting(unittest.TestCase):
    def test_on_by_default_and_settable(self):
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        self.assertTrue(playbook_core.load_prefs(state)['temporary_data'])
        prefs, error = playbook_core.set_pref(state, 'temporary_data', 'off')
        self.assertIsNone(error)
        self.assertFalse(playbook_core.load_prefs(state)['temporary_data'])


if __name__ == '__main__':
    unittest.main()
