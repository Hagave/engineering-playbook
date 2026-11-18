"""
Unit tests for hooks/context_guard.py.

Each test calls individual check functions directly, without subprocess overhead.
_save_state is mocked in tests that exercise state-writing paths to avoid
polluting ~/.claude/hooks/ with test artifacts.
"""
import importlib.util
import json
import os
import sys
import tempfile
import shutil
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK = os.path.join(_ROOT, 'hooks', 'context_guard.py')

spec = importlib.util.spec_from_file_location('context_guard', _HOOK)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402


class TestCheckContext(unittest.TestCase):

    def test_none_pct_returns_none(self):
        self.assertIsNone(cg._check_context(None, {}))

    def test_below_warn_returns_none(self):
        self.assertIsNone(cg._check_context(0.50, {}))

    def test_just_below_warn_returns_none(self):
        self.assertIsNone(cg._check_context(0.699, {}))

    def test_at_warn_returns_warning_not_block(self):
        result = cg._check_context(cg.WARN_PCT, {})
        self.assertIsNotNone(result)
        self.assertFalse(result['block'])
        self.assertIn('playbook:context', result['msg'])

    def test_warning_reaches_the_model_once_per_crossing(self):
        state = {}
        self.assertTrue(cg._check_context(cg.WARN_PCT, state)['to_model'])
        self.assertFalse(cg._check_context(0.75, state)['to_model'])
        self.assertIsNone(cg._check_context(0.40, state))  # compacted: re-arms
        self.assertTrue(cg._check_context(cg.WARN_PCT, state)['to_model'])

    def test_between_warn_and_block_returns_warning(self):
        result = cg._check_context(0.80, {})
        self.assertIsNotNone(result)
        self.assertFalse(result['block'])

    def test_at_block_returns_block(self):
        state = {}
        with mock.patch.object(cg, '_save_state'):
            result = cg._check_context(cg.BLOCK_PCT, state)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])
        self.assertIn('HARD STOP', result['msg'])
        self.assertTrue(state.get('locked'))

    def test_above_block_sets_lock_ts(self):
        state = {}
        with mock.patch.object(cg, '_save_state'):
            cg._check_context(0.90, state)
        self.assertIn('lock_ts', state)

    def test_warn_msg_contains_percentage(self):
        result = cg._check_context(0.75, {})
        self.assertIn('75%', result['msg'])

    def test_no_threshold_recommends_compact_as_the_exit(self):
        """A compact that falls short of the threshold leaves the guard blocking;
        the only exit the guard names is /clear."""
        with mock.patch.object(cg, '_save_state'):
            for pct in (cg.WARN_PCT, cg.BLOCK_PCT):
                msg = cg._check_context(pct, {})['msg']
                self.assertIn('/clear', msg)
                self.assertIn(cg.HANDOFF_OPEN, msg)
                self.assertIn('Do not recommend /compact', msg)

    def test_lock_message_asks_for_the_handoff(self):
        msg = cg._check_lock({'locked': True, 'lock_ts': time.time()}, 0.90)['msg']
        self.assertIn(cg.HANDOFF_OPEN, msg)
        self.assertIn('/clear', msg)


class TestHandoffCapture(unittest.TestCase):

    def test_turn_text_stops_at_the_latest_prompt(self):
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False) as f:
            for entry in (
                {'type': 'assistant', 'message': {'content': 'earlier [HANDOFF]x[/HANDOFF]'}},
                {'type': 'user', 'message': {'content': 'continue'}},
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'now'}]}},
            ):
                f.write(json.dumps(entry) + '\n')
        try:
            self.assertEqual(cg._turn_assistant_text(f.name), 'now')
        finally:
            os.remove(f.name)

    def test_handoff_regex_spans_lines(self):
        text = 'intro\n[HANDOFF]\ngoal: a\nnext: b\n[/HANDOFF]\nrun /clear'
        self.assertEqual(cg.HANDOFF_RE.findall(text), ['goal: a\nnext: b\n'])

    def test_an_inline_mention_of_the_markers_is_not_a_handoff(self):
        text = 'The block goes between `[HANDOFF]` and `[/HANDOFF]`, see the README.'
        self.assertEqual(cg.HANDOFF_RE.findall(text), [])


class TestCheckLock(unittest.TestCase):

    def test_not_locked_returns_none(self):
        self.assertIsNone(cg._check_lock({}, 0.50))

    def test_locked_with_high_pct_blocks(self):
        result = cg._check_lock({'locked': True}, 0.90)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])
        self.assertIn('playbook:context', result['msg'])

    def test_locked_with_none_pct_stays_locked(self):
        result = cg._check_lock({'locked': True, 'lock_ts': time.time()}, None)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    def test_locked_drops_below_unlock_threshold(self):
        state = {'locked': True}
        with mock.patch.object(cg, '_save_state'):
            result = cg._check_lock(state, cg.UNLOCK_PCT - 0.01)
        self.assertIsNone(result)
        self.assertFalse(state.get('locked'))

    def test_locked_old_timestamp_auto_unlocks(self):
        state = {'locked': True, 'lock_ts': time.time() - 7300}
        with mock.patch.object(cg, '_save_state'):
            result = cg._check_lock(state, None)
        self.assertIsNone(result)
        self.assertFalse(state.get('locked'))

    def test_locked_recent_timestamp_stays_locked(self):
        state = {'locked': True, 'lock_ts': time.time() - 100}
        result = cg._check_lock(state, None)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])


class TestCheckDestructive(unittest.TestCase):

    def _assert_blocked(self, cmd):
        result = cg._check_destructive(cmd)
        self.assertIsNotNone(result, f'Expected block for: {cmd!r}')
        self.assertTrue(result['block'])
        self.assertIn('playbook:safety', result['msg'])

    def _assert_passes(self, cmd):
        self.assertIsNone(cg._check_destructive(cmd), f'Expected pass for: {cmd!r}')

    def test_rm_rf_blocked(self):
        self._assert_blocked('rm -rf /tmp/test')

    def test_rm_r_blocked(self):
        self._assert_blocked('rm -r dir/')

    def test_rm_Rf_blocked(self):
        self._assert_blocked('rm -Rf /some/path')

    def test_rm_long_recursive_blocked(self):
        self._assert_blocked('rm --recursive dir/')

    def test_rm_flags_after_path_blocked(self):
        self._assert_blocked('rm dir/ -rf')

    def test_git_push_force_long_blocked(self):
        self._assert_blocked('git push --force origin main')

    def test_git_push_force_short_blocked(self):
        self._assert_blocked('git push -f')

    def test_git_push_force_refspec_blocked(self):
        self._assert_blocked('git push origin +main')

    def test_git_reset_hard_blocked(self):
        self._assert_blocked('git reset --hard HEAD~1')

    def test_git_branch_D_blocked(self):
        self._assert_blocked('git branch -D old-branch')

    def test_git_checkout_dash_dash_blocked(self):
        self._assert_blocked('git checkout -- .')

    def test_git_restore_dot_blocked(self):
        self._assert_blocked('git restore .')

    def test_git_clean_fd_blocked(self):
        self._assert_blocked('git clean -fd')

    def test_git_clean_force_blocked(self):
        self._assert_blocked('git clean --force')

    def test_find_delete_blocked(self):
        self._assert_blocked("find . -name '*.pyc' -delete")

    def test_xargs_rm_blocked(self):
        self._assert_blocked("find . -name '*.tmp' | xargs rm")

    def test_drop_table_blocked(self):
        self._assert_blocked('DROP TABLE users')

    def test_truncate_blocked(self):
        self._assert_blocked('TRUNCATE orders')

    def test_delete_without_where_blocked(self):
        self._assert_blocked('DELETE FROM users')

    def test_delete_without_where_multiline_blocked(self):
        self._assert_blocked('psql -c "\nDELETE FROM users\n"')

    def test_delete_with_where_passes(self):
        self._assert_passes('DELETE FROM users WHERE id = $1')

    def test_delete_with_where_multiline_passes(self):
        self._assert_passes('psql -c "DELETE FROM users\nWHERE id = $1"')

    def test_ls_passes(self):
        self._assert_passes('ls -la')

    def test_git_status_passes(self):
        self._assert_passes('git status')

    def test_git_push_no_force_passes(self):
        self._assert_passes('git push origin main')

    def test_npm_install_passes(self):
        self._assert_passes('npm install vitest --save-dev')

    def test_tsc_passes(self):
        self._assert_passes('tsc --noEmit')

    def test_rm_without_recursive_passes(self):
        self._assert_passes('rm single-file.txt')

    def test_rm_hyphenated_filename_passes(self):
        self._assert_passes('rm my-Report.txt')

    def test_git_clean_dry_run_only_passes(self):
        self._assert_passes('git clean -n')


class TestCheckAiAttribution(unittest.TestCase):

    def test_non_commit_returns_none(self):
        self.assertIsNone(cg._check_ai_attribution('ls -la'))

    def test_git_log_returns_none(self):
        self.assertIsNone(cg._check_ai_attribution('git log --oneline'))

    def test_commit_without_attribution_passes(self):
        cmd = 'git commit -m "feat: add user registration"'
        self.assertIsNone(cg._check_ai_attribution(cmd))

    def test_commit_with_co_authored_claude_blocks(self):
        cmd = 'git commit -m "fix\n\nCo-Authored-By: Claude Sonnet"'
        result = cg._check_ai_attribution(cmd)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])
        self.assertIn('playbook:commit', result['msg'])

    def test_commit_attribution_case_insensitive(self):
        cmd = 'git commit -m "fix\n\nco-authored-by: CLAUDE"'
        result = cg._check_ai_attribution(cmd)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    def test_commit_co_authored_lowercase_claude(self):
        cmd = 'git commit -m "refactor\n\nCo-Authored-By: claude code"'
        result = cg._check_ai_attribution(cmd)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    # A pull request body is read by the same team as the commit, and on a
    # single-commit PR the forge pre-fills it FROM the commit message. Covering
    # only `git commit` let the trailer ship anyway.
    def test_gh_pr_create_with_co_author_blocks(self):
        cmd = 'gh pr create --title x --body "fix\n\nCo-Authored-By: Claude Opus 5 (1M context)"'
        result = cg._check_ai_attribution(cmd)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    def test_az_repos_pr_create_with_co_author_blocks(self):
        cmd = 'az repos pr create --description "fix\n\nCo-Authored-By: Claude Opus 5"'
        self.assertIsNotNone(cg._check_ai_attribution(cmd))

    def test_human_co_author_passes(self):
        cmd = 'git commit -m "fix\n\nCo-Authored-By: Maria Silva <m@example.com>"'
        self.assertIsNone(cg._check_ai_attribution(cmd))

    def test_user_environment_exception(self):
        cmd = 'git commit -m "fix\n\nCo-Authored-By: Claude"'
        with mock.patch.dict(os.environ, {playbook_core.ATTRIBUTION_ALLOW_ENV: '1'}):
            self.assertIsNone(cg._check_ai_attribution(cmd))


class TestPushRevisions(unittest.TestCase):
    """What a push publishes, read from its arguments (the words after `push`)."""

    def test_no_refspec_is_the_current_branch(self):
        self.assertEqual(playbook_core.push_revisions([]), ['HEAD'])
        self.assertEqual(playbook_core.push_revisions(['-u', 'origin']), ['HEAD'])

    def test_src_dst_publishes_src(self):
        self.assertEqual(playbook_core.push_revisions(['origin', 'abc123:refs/heads/x']), ['abc123'])

    def test_forced_refspec_drops_the_plus(self):
        self.assertEqual(playbook_core.push_revisions(['origin', '+feat']), ['feat'])

    def test_deletions_publish_nothing(self):
        self.assertEqual(playbook_core.push_revisions(['origin', ':old']), [])
        self.assertEqual(playbook_core.push_revisions(['origin', '--delete', 'old']), [])

    def test_bare_colon_pushes_every_matching_branch(self):
        # `:` has no src, as `:dst` has none, but it publishes, it deletes nothing.
        self.assertEqual(playbook_core.push_revisions(['origin', ':']), ['--branches'])
        self.assertEqual(playbook_core.push_revisions(['origin', '+:']), ['--branches'])

    def test_all_and_mirror_and_tags(self):
        self.assertEqual(playbook_core.push_revisions(['--all', 'origin']), ['--branches'])
        self.assertEqual(playbook_core.push_revisions(['--mirror', 'origin']), ['--branches', '--tags'])
        self.assertEqual(playbook_core.push_revisions(['origin', '--tags']), ['--tags'])

    def test_tag_keyword_names_a_tag(self):
        self.assertEqual(playbook_core.push_revisions(['origin', 'tag', 'v1.2.0']),
                         ['refs/tags/v1.2.0'])

    def test_glob_refspec(self):
        self.assertEqual(playbook_core.push_revisions(['origin', 'refs/heads/feat/*:refs/heads/feat/*']),
                         ['--glob=refs/heads/feat/*'])

    def test_option_value_is_not_a_refspec(self):
        self.assertEqual(playbook_core.push_revisions(['-o', 'ci.skip', 'origin', 'main']), ['main'])

    def test_run_time_argument_is_unresolvable(self):
        self.assertIsNone(playbook_core.push_revisions(['origin', '$BRANCH']))

    def test_redirections_are_not_refspecs(self):
        """`git push origin main 2>&1 | tail` is cut at `&`, leaving `2>`."""
        for args in (['origin', 'main', '2>'], ['origin', 'main', '2>/dev/null'],
                     ['origin', 'main', '>', 'log.txt']):
            with self.subTest(args=args):
                self.assertEqual(playbook_core.push_revisions(args), ['main'])

    def test_dry_run_publishes_nothing(self):
        self.assertEqual(playbook_core.push_revisions(['--dry-run', 'origin', 'main']), [])
        self.assertEqual(playbook_core.push_revisions(['-n', 'origin', 'main']), [])

    def test_dry_run_in_a_short_flag_bundle_or_an_abbreviation(self):
        """git 2.47 takes `-fn`, `-nf`, `-un` and `--dr`/`--dry` as a dry run."""
        for flag in ('-fn', '-nf', '-un', '-vn', '-nofoo', '--dr', '--dry', '--dry-ru'):
            with self.subTest(flag=flag):
                self.assertEqual(playbook_core.push_revisions([flag, 'origin', 'main']), [])

    def test_n_as_the_value_of_a_bundled_push_option_is_not_a_dry_run(self):
        """`-on` and `-fon` send the push option `n`; the push still happens."""
        for flag in ('-on', '-fon'):
            with self.subTest(flag=flag):
                self.assertEqual(playbook_core.push_revisions([flag, 'origin', 'main']), ['main'])

    def test_a_later_no_dry_run_turns_the_dry_run_off(self):
        """`git push -n --no-dry-run` publishes; so do the abbreviations git takes."""
        for args in (['-n', '--no-dry-run', 'origin', 'main'],
                     ['--dry-run', '--no-dry-run', 'origin', 'main'],
                     ['-n', '--no-dr', 'origin', 'main'], ['--dr', '--no-dry', 'origin', 'main'],
                     ['-fn', 'origin', 'main', '--no-dry-run']):
            with self.subTest(args=args):
                self.assertEqual(playbook_core.push_revisions(args), ['main'])
        self.assertEqual(playbook_core.push_revisions(['--no-dry-run', '-n', 'origin', 'main']), [])

    def test_porcelain_is_not_a_dry_run(self):
        self.assertEqual(playbook_core.push_revisions(['--porcelain', 'origin', 'main']), ['main'])

    def test_a_run_time_word_in_a_dry_run_is_unresolvable(self):
        """The word may expand to `--no-dry-run`."""
        self.assertIsNone(playbook_core.push_revisions(['-n', 'origin', '$FLAGS']))

    def test_glab_mr_create_with_co_author_blocks(self):
        cmd = 'glab mr create --description "fix\n\nco-authored-by: claude"'
        self.assertIsNotNone(cg._check_ai_attribution(cmd))

    def test_generated_with_claude_code_line_blocks(self):
        cmd = 'gh pr create --body "does x\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"'
        self.assertIsNotNone(cg._check_ai_attribution(cmd))

    def test_pr_create_without_attribution_passes(self):
        self.assertIsNone(cg._check_ai_attribution('gh pr create --title x --body "fix: y"'))

    def test_git_dash_c_commit_is_still_a_commit(self):
        cmd = 'git -C /srv/app commit -m "fix\n\nCo-Authored-By: Claude"'
        self.assertIsNotNone(cg._check_ai_attribution(cmd))

    def test_reading_history_is_not_publishing(self):
        for cmd in ('git log --grep="Co-Authored-By: Claude"',
                    'gh pr view 12', 'grep -r "Co-Authored-By: Claude" .'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(cg._check_ai_attribution(cmd))


# ── Transcript helpers ─────────────────────────────────────────────────────────

def _assistant_line(content) -> str:
    return json.dumps({'type': 'assistant', 'message': {'content': content}}) + '\n'


def _user_line(content: str) -> str:
    return json.dumps({'type': 'user', 'message': {'content': content}}) + '\n'


def _tool_use_line(name: str, tool_input: dict) -> str:
    block = {'type': 'tool_use', 'name': name, 'input': tool_input}
    return json.dumps({'type': 'assistant', 'message': {'content': [block]}}) + '\n'


def _tool_result_line() -> str:
    block = {'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}
    return json.dumps({'type': 'user', 'message': {'content': [block]}}) + '\n'


def _write_transcript(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False)
    f.write(content)
    f.close()
    return f.name


class TestCheckSpecGate(unittest.TestCase):

    def _gate(self, content: str):
        path = _write_transcript(content)
        try:
            return cg._check_spec_gate(path)
        finally:
            os.unlink(path)

    # ── No transcript ─────────────────────────────────────────────────────────

    def test_no_transcript_returns_none(self):
        self.assertIsNone(cg._check_spec_gate(None))

    def test_nonexistent_transcript_returns_none(self):
        self.assertIsNone(cg._check_spec_gate('/nonexistent/path.jsonl'))

    # ── Token after the latest user message ───────────────────────────────────

    def test_token_in_current_turn_passes(self):
        content = _user_line('ok, proceed') + _assistant_line('[SPEC CONFIRMED] implementing')
        self.assertIsNone(self._gate(content))

    def test_token_survives_tool_use_entries_in_same_turn(self):
        """Multi-Write turns: tool_use entries after the token must not re-block."""
        content = (
            _user_line('go')
            + _assistant_line('[SPEC CONFIRMED] — spec confirmed')
            + _tool_use_line('Write', {'file_path': 'a.ts'})
            + _tool_result_line()
            + _tool_use_line('Write', {'file_path': 'b.ts'})
        )
        self.assertIsNone(self._gate(content))

    def test_token_only_transcript_passes(self):
        self.assertIsNone(self._gate(_assistant_line('[SPEC CONFIRMED] — unambiguous fix')))

    # ── Token absent or stale ─────────────────────────────────────────────────

    def test_no_token_blocks(self):
        result = self._gate(_assistant_line('let me implement this'))
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])
        self.assertIn('spec-gate', result['msg'])
        self.assertIn('[SPEC CONFIRMED]', result['msg'])

    def test_token_before_newer_user_message_blocks(self):
        """Per-task enforcement: a token from a previous task does not carry over."""
        content = (
            _assistant_line('[SPEC CONFIRMED] — confirmed task A')
            + _user_line('ok, now also do task B')
            + _assistant_line('implementing task B')
        )
        result = self._gate(content)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    def test_user_message_with_token_does_not_unblock(self):
        content = (
            _assistant_line('let me implement this')
            + _user_line('[SPEC CONFIRMED] I typed this myself')
        )
        result = self._gate(content)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    def test_no_assistant_messages_blocks(self):
        content = _user_line('implement this please') + 'not valid json\n'
        result = self._gate(content)
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    # ── Content as list of blocks ─────────────────────────────────────────────

    def test_content_as_list_of_text_blocks_passes(self):
        blocks = [
            {'type': 'text', 'text': '[SPEC CONFIRMED] — spec confirmed'},
            {'type': 'text', 'text': 'here is the implementation plan'},
        ]
        self.assertIsNone(self._gate(_assistant_line(blocks)))

    def test_content_as_list_without_token_blocks(self):
        blocks = [{'type': 'text', 'text': 'implementing now'}]
        result = self._gate(_assistant_line(blocks))
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])

    # ── Block message content ─────────────────────────────────────────────────

    def test_block_msg_includes_single_line_fix_hint(self):
        result = self._gate(_assistant_line('no token here'))
        self.assertIn('single-line fix', result['msg'])


class TestIsUserPrompt(unittest.TestCase):
    """Harness-injected content must never count as a user prompt."""

    def _obj(self, content):
        return {'type': 'user', 'message': {'content': content}}

    def test_plain_string_is_prompt(self):
        self.assertTrue(cg._is_user_prompt(self._obj('please fix this bug')))

    def test_text_block_is_prompt(self):
        self.assertTrue(cg._is_user_prompt(self._obj([{'type': 'text', 'text': 'do it'}])))

    def test_tool_result_is_not_prompt(self):
        content = [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]
        self.assertFalse(cg._is_user_prompt(self._obj(content)))

    def test_tool_result_with_injected_text_is_not_prompt(self):
        content = [
            {'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'},
            {'type': 'text', 'text': '<system-reminder>note</system-reminder>'},
        ]
        self.assertFalse(cg._is_user_prompt(self._obj(content)))

    def test_system_reminder_text_is_not_prompt(self):
        self.assertFalse(cg._is_user_prompt(
            self._obj('<system-reminder>hook feedback</system-reminder>')))

    def test_task_notification_is_not_prompt(self):
        self.assertFalse(cg._is_user_prompt(
            self._obj([{'type': 'text', 'text': '<task-notification>done</task-notification>'}])))

    def test_assistant_entry_is_not_prompt(self):
        self.assertFalse(cg._is_user_prompt(
            {'type': 'assistant', 'message': {'content': 'hi'}}))

    def test_meta_entry_is_not_prompt(self):
        self.assertFalse(cg._is_user_prompt(
            {'type': 'user', 'isMeta': True, 'message': {'content': 'meta'}}))


class TestBashCodeWrite(unittest.TestCase):
    """BASH_CODE_WRITE_RE catches shell commands that write source files."""

    def _writes(self, cmd):
        return bool(cg.BASH_CODE_WRITE_RE.search(cmd))

    def test_redirect_to_ts_detected(self):
        self.assertTrue(self._writes('echo "export {}" > src/user.ts'))

    def test_append_to_js_detected(self):
        self.assertTrue(self._writes('cat header.txt >> dist/bundle.js'))

    def test_tee_to_ts_detected(self):
        self.assertTrue(self._writes('echo x | tee src/index.ts'))

    def _gated(self, cmd):
        # `sed -i` is read per simple command (bash_write_targets), not by the
        # line regex, so it is checked through the predicate every gate uses.
        return playbook_core.is_code_write('Bash', {'command': cmd}, cmd, _ROOT)

    def test_sed_inplace_on_ts_detected(self):
        self.assertTrue(self._gated("sed -i 's/foo/bar/' src/user.ts"))

    def test_redirects_are_read_as_the_shell_reads_them(self):
        """Release review of 4.0.0: the redirect and tee targets came from the
        raw text."""
        for cmd in ('git commit -m "rename a.py -> b.py"', 'echo "x > y.py"',
                    'echo x | tee out.py.log', 'echo x >&2', 'cmd 2>&1 | tee build.log'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self._gated(cmd))
        for cmd in ('echo x | tee b.txt src/a.py', 'echo x>src/a.py', 'echo x 2>src/a.py',
                    'echo x &> src/a.py', 'printf x >| src/a.py'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._gated(cmd))

    def test_redirects_the_command_hands_on_are_still_read(self):
        """The forms the whole-line pattern matched by accident: inside a
        shell's -c script, run by find -exec, as eval's arguments, and as a
        command line an interpreter one-liner runs."""
        for cmd in ('bash -c "echo x > src/a.py"', "find . -exec sh -c 'echo x > src/a.py' \\;",
                    'eval "echo x > src/a.py"',
                    'python3 -c "import os; os.system(\'echo x > src/a.py\')"',
                    'echo x | xargs tee src/a.py', 'echo "unbalanced > src/a.py'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._gated(cmd))

    def test_heredoc_via_redirect_detected(self):
        self.assertTrue(self._writes("cat > src/user.ts << 'EOF'\nclass User {}\nEOF"))

    def test_plain_grep_on_ts_passes(self):
        self.assertFalse(self._writes('grep -n "class" src/user.ts'))

    def test_redirect_to_log_passes(self):
        self.assertFalse(self._writes('npm test > /tmp/out.log'))

    def test_redirect_of_ts_output_to_txt_passes(self):
        self.assertFalse(self._writes('grep foo src/user.ts > /tmp/hits.txt'))

    def test_sed_without_inplace_passes(self):
        self.assertFalse(self._gated("sed 's/foo/bar/' src/user.ts"))

    def test_grep_flag_after_sed_is_not_sed_inplace(self):
        # Measured false positive: the `-i` belonged to grep, a later command.
        self.assertFalse(self._gated(
            'sed -n 60,125p hooks/session_start.py; '
            'grep -n -i "takes effect" scripts/playbook_config.py hooks/playbook_core.py'))


class TestCodeWriteDestination(unittest.TestCase):
    """A write counts as project source only when it lands somewhere the project KEEPS.

    The gates used to decide from the EXTENSION alone, so a measuring probe dropped in
    the repo's git-ignored `scratch/` and a throwaway parser in the session scratchpad
    under /tmp both armed the spec and validation gates. Destination now decides, and it
    fails CLOSED: anything unresolvable is still treated as source.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.repo = os.path.realpath(self._dir)
        os.makedirs(os.path.join(self.repo, 'src'), exist_ok=True)
        os.makedirs(os.path.join(self.repo, 'scratch'), exist_ok=True)
        with open(os.path.join(self.repo, '.gitignore'), 'w') as fh:
            fh.write('scratch/\n')
        import subprocess
        for args in (['init', '-q'], ['add', '-A'],
                     ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'base']):
            subprocess.run(['git', '-C', self.repo] + args,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def _write(self, path):
        return playbook_core.is_code_write('Write', {'file_path': path}, '', self.repo)

    def _bash(self, cmd):
        return playbook_core.is_code_write('Bash', {}, cmd, self.repo)

    # — the gate must still fire on real source —

    def test_source_in_the_project_counts(self):
        self.assertTrue(self._write(os.path.join(self.repo, 'src', 'user.ts')))

    def test_relative_source_in_the_project_counts(self):
        self.assertTrue(self._write('src/user.ts'))

    def test_bash_redirect_to_project_source_counts(self):
        self.assertTrue(self._bash('echo x > src/user.ts'))

    def test_source_outside_the_project_still_counts(self):
        """Editing another repo's code is real work; it just cannot be validated here.

        The path is deliberately NOT under a temp root — that is the only outside-the-
        project location the exemption covers.
        """
        self.assertTrue(self._write('/opt/other-repo/src/user.ts'))

    # — and must stop firing on throwaway destinations —

    def test_git_ignored_path_in_the_project_is_not_source(self):
        self.assertFalse(self._write(os.path.join(self.repo, 'scratch', 'probe.py')))

    def test_relative_git_ignored_path_is_not_source(self):
        self.assertFalse(self._write('scratch/probe.py'))

    def test_bash_heredoc_into_ignored_path_is_not_source(self):
        self.assertFalse(self._bash("cat > scratch/probe.py <<'PY'\nprint(1)\nPY"))

    def test_session_scratchpad_under_tmp_is_not_source(self):
        pad = os.path.join(tempfile.gettempdir(), 'claude-1', 'scratchpad', 'Program.cs')
        self.assertFalse(self._write(pad))

    def test_bash_write_into_tmp_is_not_source(self):
        self.assertFalse(self._bash('cat > %s/parse.cs <<EOF\nx\nEOF' % tempfile.gettempdir()))

    def test_literal_cd_then_write_resolves_against_that_directory(self):
        pad = os.path.join(tempfile.gettempdir(), 'pad-literal')
        os.makedirs(pad, exist_ok=True)
        self.assertFalse(self._bash('cd %s && cat > Program.cs <<EOF\nx\nEOF' % pad))

    # — fail closed —

    def test_unresolvable_variable_path_counts_as_source(self):
        """`cd $SP && cat > x.cs`: the destination cannot be read, so the gate fires."""
        self.assertTrue(self._bash('cd $SP && cat > Program.cs <<EOF\nx\nEOF'))

    def test_dollar_in_the_path_counts_as_source(self):
        self.assertTrue(self._bash('echo x > $OUT/user.ts'))

    def test_non_source_extension_never_counts(self):
        self.assertFalse(self._write(os.path.join(self.repo, 'docs', 'note.md')))
        self.assertFalse(self._bash('npm test > out.log'))

    def test_outside_a_git_repository_project_paths_still_count(self):
        """No git means no ignore list; a source path must keep arming the gates."""
        plain = os.path.realpath(tempfile.mkdtemp(dir=os.path.dirname(self.repo)))
        try:
            self.assertTrue(playbook_core.is_code_write(
                'Write', {'file_path': os.path.join(plain, 'a.ts')}, '', plain))
        finally:
            import shutil
            shutil.rmtree(plain, ignore_errors=True)


class TestLanguageCoverage(unittest.TestCase):
    """Every language the playbook governs must be visible to the gates.

    The core rules went language-agnostic; the guards did not follow. A language
    missing from CODE_EXT_RE silently disables the skill gate, the spec gate and
    the validation gate for that whole ecosystem, and a language missing from
    VALIDATION_RE makes the Stop gate unclearable. Add a row here whenever a new
    engineering-<lang> opt-in skill ships.
    """

    LANGUAGES = {
        'typescript': ('src/user.ts', 'npm test'),
        'go': ('internal/user.go', 'go test ./...'),
        'java': ('src/main/java/User.java', 'mvn -q test'),
        'rust': ('src/user.rs', 'cargo test'),
        'csharp': ('src/User.cs', 'dotnet test'),
        'kotlin': ('src/User.kt', './gradlew test'),
        'ruby': ('app/user.rb', 'rspec'),
        'python': ('hooks/guard.py', 'python3 -m pytest'),
        'php': ('src/User.php', 'composer test'),
        'swift': ('Sources/User.swift', 'swift test'),
        'scala': ('src/User.scala', 'sbt test'),
        'dart': ('lib/user.dart', 'dart test'),
        'elixir': ('lib/user.ex', 'mix test'),
        'c': ('src/user.c', 'make test'),
        'cpp': ('src/user.cpp', 'ctest --test-dir build'),
        'shell': ('scripts/install.sh', 'shellcheck scripts/install.sh'),
    }

    def test_source_file_counts_as_code_write(self):
        for lang, (path, _) in self.LANGUAGES.items():
            with self.subTest(lang=lang):
                self.assertTrue(cg.CODE_EXT_RE.search(path))

    def test_validation_command_clears_the_gate(self):
        for lang, (_, cmd) in self.LANGUAGES.items():
            with self.subTest(lang=lang):
                self.assertTrue(cg.VALIDATION_RE.search(cmd))

    def test_shell_write_to_source_is_not_a_bypass(self):
        for lang, (path, _) in self.LANGUAGES.items():
            with self.subTest(lang=lang):
                self.assertTrue(cg.BASH_CODE_WRITE_RE.search(f'cat > {path} << EOF'))

    def test_non_source_files_still_pass(self):
        for path in ('README.md', 'notes.txt', 'data.json', 'out.log', 'archive.gob'):
            with self.subTest(path=path):
                self.assertFalse(cg.CODE_EXT_RE.search(path))


class TestDestructiveFalsePositives(unittest.TestCase):
    """The destructive guard read as intermittent because it matched text that
    was never going to run: a bare SQL keyword anywhere in a command, and the
    body of a heredoc that only writes a file."""

    DANGEROUS = 'rm -' + 'rf /'

    def test_data_heredoc_body_is_not_executed_shell(self):
        command = f"cat > docs/runbook.md <<'DOCEOF'\nNever run {self.DANGEROUS} here.\nDOCEOF"
        self.assertIsNone(playbook_core.check_destructive(command))

    def test_interpreter_heredoc_body_is_not_executed_shell(self):
        command = f"python3 - <<'PYEOF'\nPATTERN = '{self.DANGEROUS}'\nPYEOF"
        self.assertIsNone(playbook_core.check_destructive(command))

    def test_shell_heredoc_body_is_still_inspected(self):
        command = f"bash <<'SHEOF'\n{self.DANGEROUS}tmp/x\nSHEOF"
        self.assertIsNotNone(playbook_core.check_destructive(command))

    def test_mentioning_a_sql_keyword_is_not_running_it(self):
        keyword = 'TRUN' + 'CATE'
        self.assertIsNone(playbook_core.check_destructive(f'grep -rn {keyword} migrations/'))
        self.assertIsNotNone(
            playbook_core.check_destructive(f'psql -c "{keyword} TABLE sessions;"'))

    def test_a_real_destructive_command_is_still_blocked(self):
        self.assertIsNotNone(playbook_core.check_destructive(self.DANGEROUS + 'var/data'))

    def test_authorization_phrases_are_not_questions(self):
        authorized = ('pode apagar a pasta build', 'go ahead and delete it',
                      'você pode remover esse arquivo', 'eu autorizo apagar',
                      'confirmo o reset', 'I confirm the deletion')
        not_authorized = ('o que acontece se apagar?', 'never delete the database',
                          'delete is destructive')
        for text in authorized:
            with self.subTest(text=text):
                self.assertTrue(playbook_core.DESTRUCTIVE_AUTHORIZED_RE.search(text))
        for text in not_authorized:
            with self.subTest(text=text):
                self.assertFalse(playbook_core.DESTRUCTIVE_AUTHORIZED_RE.search(text))

    def test_deleting_inside_the_session_scratchpad_needs_no_consent(self):
        """Decided for 4.0.0: what the session made in its own scratchpad is
        disposable; anything else, or any path that could leave it, still asks."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        os.makedirs(os.path.join(root, 'old'))
        os.symlink(tempfile.gettempdir(), os.path.join(root, 'link'))
        rm = 'r' + 'm -rf '

        def blocked(command):
            return playbook_core.check_destructive(command, command, '.',
                                                   disposable_root=root) is not None

        for command in (rm + f'{root}/old', rm + root):
            with self.subTest(command=command):
                self.assertFalse(blocked(command))
        for command in (rm + f'{root}/link/', rm + f'{root}/../x', rm + f'{root}/*',
                        f'cd {root} && {rm}old', rm + f'{root}/old /var/data',
                        'sudo ' + rm + f'{root}/old', rm + f'{root}/old && git reset --hard'):
            with self.subTest(command=command):
                self.assertTrue(blocked(command))
        self.assertIsNotNone(playbook_core.check_destructive(rm + f'{root}/old'))

    def test_the_scratchpad_is_this_session_only(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        mine = os.path.join(tmp, f'claude-{os.getuid()}', 'proj', 'sid-1', 'scratchpad')
        os.makedirs(mine)
        os.makedirs(os.path.join(tmp, f'claude-{os.getuid()}', 'proj', 'sid-2', 'scratchpad'))
        with mock.patch.object(cg.tempfile, 'gettempdir', return_value=tmp), \
                mock.patch.object(cg, '_SESSION_ID', 'sid-1'):
            self.assertEqual(cg._session_scratchpad(), mine)
        with mock.patch.object(cg.tempfile, 'gettempdir', return_value=tmp), \
                mock.patch.object(cg, '_SESSION_ID', ''):
            self.assertEqual(cg._session_scratchpad(), '')

    def test_a_general_approval_does_not_authorize_destruction(self):
        """Release review of 4.0.0: each of these unlocked rm -rf for the turn."""
        for text in ('pode rodar os testes', 'pode executar', 'pode seguir com o plano',
                     'pode prosseguir', 'eu autorizo', 'confirmo', 'assumo o risco',
                     'I confirm the spec looks right', 'i authorize it'):
            with self.subTest(text=text):
                self.assertFalse(playbook_core.DESTRUCTIVE_AUTHORIZED_RE.search(text))

    def test_review_waiver_names_the_review(self):
        for text in ('pode pushar sem review', 'skip review', 'pula o review',
                     'no review needed'):
            with self.subTest(text=text):
                self.assertTrue(playbook_core.REVIEW_BYPASS_RE.search(text))
        for text in ('no review comments so far', 'pode pushar', 'pode dar push',
                     'push direto', 'deixa assim', 'deixa passar'):
            with self.subTest(text=text):
                self.assertFalse(playbook_core.REVIEW_BYPASS_RE.search(text))


class TestInlinePayloadFalsePositives(unittest.TestCase):
    """An inline interpreter payload is another language's source, not shell
    about to run -- the case strip_heredoc_bodies already documents, one surface
    further. Measured live: a `python3 -c` one-liner whose string literal merely
    contained a push command tripped the review gate, on a command that pushed
    nothing at all."""

    def _pushes(self, command):
        return bool(playbook_core.GIT_PUSH_RE.search(
            playbook_core.strip_inline_payloads(command)))

    def test_single_quoted_payload_mentioning_a_push_is_not_a_push(self):
        self.assertFalse(self._pushes('python3 -c \'CASES = ["git push --force"]\''))

    def test_double_quoted_payload_is_stripped(self):
        self.assertFalse(self._pushes('node -e "console.log(\'git push\')"'))

    def test_dollar_quoted_payload_is_stripped(self):
        self.assertFalse(self._pushes("python3 -c $'P = \"git push\"\\nprint(P)'"))

    def test_payload_after_other_flags_is_stripped(self):
        self.assertFalse(self._pushes("python3 -X utf8 -c 'P = \"git push\"'"))

    def test_shell_payload_is_still_inspected(self):
        """`bash -c` really is shell about to run; its payload must be kept."""
        self.assertTrue(self._pushes("bash -c 'git push --force'"))

    def test_a_real_push_outside_the_payload_still_matches(self):
        self.assertTrue(self._pushes("python3 -c 'print(1)' && git push"))

    def test_command_without_a_payload_is_untouched(self):
        for command in ('node --check a.js', 'python3 -m pytest tests/', 'git status'):
            with self.subTest(command=command):
                self.assertEqual(playbook_core.strip_inline_payloads(command), command)

    def test_skipping_stops_at_a_shell_separator(self):
        """The interpreter's own flags and their arguments may be skipped on the
        way to -c, but not a whole new command: `bash -c` after `&&` is shell
        about to run and must survive."""
        self.assertTrue(self._pushes("python3 -X utf8 s.py && bash -c 'git push'"))


class TestScanSkills(unittest.TestCase):

    def _skill_line(self, name: str) -> str:
        return _tool_use_line('Skill', {'skill': name})

    def test_governance_skill_sets_gov_loaded(self):
        path = _write_transcript(self._skill_line('engineering-governance'))
        state = {}
        try:
            suspended = cg._scan_skills(path, state)
        finally:
            os.unlink(path)
        self.assertFalse(suspended)
        self.assertTrue(state.get('gov_loaded'))

    def test_namespaced_skill_name_recognized(self):
        path = _write_transcript(self._skill_line('engineering-playbook:engineering-governance'))
        state = {}
        try:
            cg._scan_skills(path, state)
        finally:
            os.unlink(path)
        self.assertTrue(state.get('gov_loaded'))

    def test_playbook_off_sets_suspended(self):
        path = _write_transcript(self._skill_line('playbook-off'))
        state = {}
        try:
            suspended = cg._scan_skills(path, state)
        finally:
            os.unlink(path)
        self.assertTrue(suspended)

    def test_playbook_on_after_off_resumes(self):
        content = (
            self._skill_line('playbook-off')
            + self._skill_line('playbook-on')
        )
        path = _write_transcript(content)
        state = {}
        try:
            suspended = cg._scan_skills(path, state)
        finally:
            os.unlink(path)
        self.assertFalse(suspended)

    def test_slash_command_invocation_recognized(self):
        line = _user_line('<command-name>engineering-governance</command-name>')
        path = _write_transcript(line)
        state = {}
        try:
            cg._scan_skills(path, state)
        finally:
            os.unlink(path)
        self.assertTrue(state.get('gov_loaded'))

    def test_prose_mention_does_not_count(self):
        line = _assistant_line('we should probably load engineering-governance later')
        path = _write_transcript(line)
        state = {}
        try:
            cg._scan_skills(path, state)
        finally:
            os.unlink(path)
        self.assertFalse(state.get('gov_loaded'))

    def test_incremental_scan_picks_up_appended_lines(self):
        path = _write_transcript(_assistant_line('starting'))
        state = {}
        try:
            cg._scan_skills(path, state)
            self.assertFalse(state.get('gov_loaded'))
            with open(path, 'a') as f:
                f.write(self._skill_line('engineering-governance'))
            cg._scan_skills(path, state)
            self.assertTrue(state.get('gov_loaded'))
        finally:
            os.unlink(path)


class TestCheckSkillGate(unittest.TestCase):

    def test_gov_loaded_passes(self):
        self.assertIsNone(cg._check_skill_gate({'gov_loaded': True}))

    def test_gov_not_loaded_blocks(self):
        result = cg._check_skill_gate({})
        self.assertIsNotNone(result)
        self.assertTrue(result['block'])
        self.assertIn('skill-gate', result['msg'])
        self.assertIn('engineering-governance', result['msg'])


class TestValidationRe(unittest.TestCase):

    def _matches(self, cmd):
        return bool(cg.VALIDATION_RE.search(cmd))

    def test_npm_test_matches(self):
        self.assertTrue(self._matches('npm test'))

    def test_npm_run_lint_matches(self):
        self.assertTrue(self._matches('npm run lint:check'))

    def test_tsc_noemit_matches(self):
        self.assertTrue(self._matches('tsc --noEmit'))

    def test_vitest_matches(self):
        self.assertTrue(self._matches('npx vitest run'))

    def test_pytest_matches(self):
        self.assertTrue(self._matches('python3 -m pytest tests/'))

    def test_git_status_does_not_match(self):
        self.assertFalse(self._matches('git status'))

    def test_npm_install_does_not_match(self):
        self.assertFalse(self._matches('npm install'))

    def test_monorepo_and_framework_runners_match(self):
        """Measured in real sessions (#60): each ran validation and the Stop
        gate did not see it."""
        for cmd in ('pnpm -F web test', 'pnpm --filter=web test', 'npm --workspace=api run lint',
                    'turbo run test', 'npm run type-check', 'npm run format:check',
                    'prettier --check .', 'next lint', 'expo lint', 'cargo nextest run'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._matches(cmd))
        for cmd in ('pnpm -F web add zod', 'prettier --write .', 'next dev', 'turbo run dev'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self._matches(cmd))

    # Syntax-only checks. A machine with no linter installed has nothing else to
    # offer, and without these the Stop gate has no clearable command at all:
    # every turn that writes blocks, the model runs the only check it has, the
    # regex does not see it, and the next write blocks again. Observed on a
    # project whose whole toolchain was python3 + bash.
    def test_py_compile_matches(self):
        self.assertTrue(self._matches('python3 -m py_compile src/a.py'))

    def test_compileall_matches(self):
        self.assertTrue(self._matches('python3 -m compileall src/'))

    def test_bash_syntax_check_matches(self):
        self.assertTrue(self._matches('bash -n scripts/install.sh'))

    def test_bash_syntax_check_with_leading_flags_matches(self):
        self.assertTrue(self._matches('bash -x -n scripts/install.sh'))

    def test_other_shells_syntax_check_matches(self):
        for cmd in ('sh -n x.sh', 'zsh -n x.zsh', 'dash -n x.sh', 'ksh -n x.ksh'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._matches(cmd))

    def test_interpreter_syntax_checks_match(self):
        for cmd in ('node --check a.js', 'ruby -c a.rb', 'php -l a.php', 'perl -c a.pl'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._matches(cmd))

    # `sh` inside another word is not a shell: there is no word boundary in the
    # middle of `ssh`, and -n on an unrelated command is not a syntax check.
    def test_ssh_dash_n_does_not_match(self):
        self.assertFalse(self._matches('ssh -n host uptime'))

    def test_unrelated_dash_n_does_not_match(self):
        for cmd in ('rsync -n src dst', 'echo -n hi', 'ls -n', 'bash install.sh', 'bash install.sh -n', './deploy.sh -n'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self._matches(cmd))


class TestContextLimit(unittest.TestCase):
    """Adaptive per-model window resolution."""

    def setUp(self):
        # Neutralize any ambient PLAYBOOK_CONTEXT_LIMIT so model resolution runs.
        self._env = mock.patch.dict(os.environ, {'PLAYBOOK_CONTEXT_LIMIT': ''})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _limit(self, model, alias=''):
        with mock.patch.object(cg, '_settings_model_alias', return_value=alias):
            return cg._context_limit(model)

    def test_opus_4_8_standard_is_one_m(self):
        """Opus 4.6+ ships 1M as its STANDARD window — no "[1m]" tag needed."""
        self.assertEqual(self._limit('claude-opus-4-8'), cg.ONE_M_LIMIT)

    def test_opus_5_standard_is_one_m(self):
        """Regression: opus-5 matched no 1M pattern and fell back to 200k,
        hard-blocking real 1M sessions at ~17% of their actual window."""
        self.assertEqual(self._limit('claude-opus-5'), cg.ONE_M_LIMIT)

    def test_opus_5_without_model_in_settings_is_still_one_m(self):
        """The 200k fallback demanded a "[1m]" marker from the settings
        cascade; a session that picks its model via /model has none."""
        self.assertEqual(self._limit('claude-opus-5', alias=''), cg.ONE_M_LIMIT)

    def test_sonnet_4_6_standard_is_one_m(self):
        self.assertEqual(self._limit('claude-sonnet-4-6'), cg.ONE_M_LIMIT)

    def test_sonnet_5_standard_is_one_m(self):
        self.assertEqual(self._limit('claude-sonnet-5'), cg.ONE_M_LIMIT)

    def test_fable_5_standard_is_one_m(self):
        self.assertEqual(self._limit('claude-fable-5'), cg.ONE_M_LIMIT)

    def test_mythos_standard_is_one_m(self):
        self.assertEqual(self._limit('claude-mythos-5'), cg.ONE_M_LIMIT)

    def test_haiku_is_default(self):
        self.assertEqual(self._limit('claude-haiku-4-5-20251001'), cg.DEFAULT_CONTEXT_LIMIT)

    def test_legacy_opus_4_5_is_default(self):
        """Legacy Opus (<=4.5) stays at 200k unless "[1m]"-tagged."""
        self.assertEqual(self._limit('claude-opus-4-5-20251101'), cg.DEFAULT_CONTEXT_LIMIT)

    def test_legacy_sonnet_4_5_is_default(self):
        self.assertEqual(self._limit('claude-sonnet-4-5-20250929'), cg.DEFAULT_CONTEXT_LIMIT)

    def test_legacy_opus_under_1m_alias_is_one_m(self):
        self.assertEqual(self._limit('claude-opus-4-5-20251101', alias='opus[1m]'), cg.ONE_M_LIMIT)

    def test_haiku_under_1m_alias_stays_default(self):
        """Haiku has no 1M variant — a 1M alias must not widen its window."""
        self.assertEqual(self._limit('claude-haiku-4-5-20251001', alias='opus[1m]'),
                         cg.DEFAULT_CONTEXT_LIMIT)

    def test_1m_marker_in_legacy_model_string_is_one_m(self):
        self.assertEqual(self._limit('claude-opus-4-5[1m]', alias=''), cg.ONE_M_LIMIT)

    def test_unknown_model_is_default(self):
        self.assertEqual(self._limit('some-future-model', alias=''), cg.DEFAULT_CONTEXT_LIMIT)

    def test_empty_model_is_default(self):
        self.assertEqual(self._limit(''), cg.DEFAULT_CONTEXT_LIMIT)

    def test_env_override_wins_over_model(self):
        with mock.patch.dict(os.environ, {'PLAYBOOK_CONTEXT_LIMIT': '750000'}):
            self.assertEqual(cg._context_limit('claude-haiku-4-5-20251001'), 750_000)

    def test_invalid_env_override_falls_back_to_model(self):
        with mock.patch.dict(os.environ, {'PLAYBOOK_CONTEXT_LIMIT': 'not-a-number'}):
            with mock.patch.object(cg, '_settings_model_alias', return_value='opus[1m]'):
                self.assertEqual(cg._context_limit('claude-opus-4-8'), cg.ONE_M_LIMIT)


class TestCalibrate(unittest.TestCase):
    """Usage the window has already held proves the real window is that large."""

    def test_below_limit_unchanged(self):
        self.assertEqual(cg._calibrate(200_000, 150_000), 200_000)

    def test_at_limit_unchanged(self):
        self.assertEqual(cg._calibrate(200_000, 200_000), 200_000)

    def test_above_default_snaps_to_one_m(self):
        self.assertEqual(cg._calibrate(200_000, 260_000), cg.ONE_M_LIMIT)

    def test_above_one_m_snaps_to_two_m(self):
        self.assertEqual(cg._calibrate(cg.ONE_M_LIMIT, 1_200_000), 2_000_000)

    def test_above_all_tiers_returns_total(self):
        self.assertEqual(cg._calibrate(200_000, 6_000_000), 6_000_000)


class TestMeasureContext(unittest.TestCase):

    def _measure(self, model, total):
        usage = {'input_tokens': total, 'cache_creation_input_tokens': 0,
                 'cache_read_input_tokens': 0}
        line = json.dumps({'type': 'assistant',
                           'message': {'model': model, 'usage': usage}}) + '\n'
        path = _write_transcript(line)
        try:
            with mock.patch.dict(os.environ, {'PLAYBOOK_CONTEXT_LIMIT': ''}):
                return cg._measure_context(path)
        finally:
            os.unlink(path)

    def test_none_transcript_returns_none(self):
        self.assertIsNone(cg._measure_context(None))

    def test_current_gen_model_uses_one_m_limit(self):
        """Opus 4.6+/Sonnet 4.6+ get 1M as their standard window, no alias needed."""
        with mock.patch.object(cg, '_settings_model_alias', return_value=''):
            pct, limit = self._measure('claude-opus-4-8', 500_000)
        self.assertEqual(limit, cg.ONE_M_LIMIT)
        self.assertAlmostEqual(pct, 0.5)

    def test_legacy_model_uses_default_limit(self):
        with mock.patch.object(cg, '_settings_model_alias', return_value=''):
            pct, limit = self._measure('claude-sonnet-4-5-20250929', 100_000)
        self.assertEqual(limit, cg.DEFAULT_CONTEXT_LIMIT)
        self.assertAlmostEqual(pct, 0.5)

    def test_output_tokens_count_toward_the_window(self):
        """The next request replays this turn's output as input — a reading that
        omits it reports a figure the session has already exceeded."""
        usage = {'input_tokens': 100_000, 'cache_creation_input_tokens': 0,
                 'cache_read_input_tokens': 500_000, 'output_tokens': 32_000}
        line = json.dumps({'type': 'assistant',
                           'message': {'model': 'claude-opus-5', 'usage': usage}}) + '\n'
        path = _write_transcript(line)
        try:
            with mock.patch.dict(os.environ, {'PLAYBOOK_CONTEXT_LIMIT': ''}), \
                 mock.patch.object(cg, '_settings_model_alias', return_value=''):
                pct, limit = cg._measure_context(path)
        finally:
            os.unlink(path)
        self.assertEqual(limit, cg.ONE_M_LIMIT)
        self.assertAlmostEqual(pct, 0.632)

    def test_usage_beyond_default_never_hard_blocks(self):
        """A resumed 1M session read as standard must not sit at >100%."""
        with mock.patch.object(cg, '_settings_model_alias', return_value=''):
            pct, limit = self._measure('claude-haiku-4-5-20251001', 300_000)
        self.assertEqual(limit, cg._calibrate(cg.DEFAULT_CONTEXT_LIMIT, 300_000))
        self.assertLess(pct, cg.BLOCK_PCT)


class TestTokenScanWindow(unittest.TestCase):
    """The token scan is bounded to TAIL_BYTES, and both the token and the user
    prompt can fall outside it.

    Hit in production: a turn whose tool output alone exceeded the window was
    blocked for not confirming a spec it had confirmed. Absence of evidence is
    fail-open, but only when the window truncated the history — a transcript
    that fits entirely inside it and has no token really has no token."""

    def _transcript(self, body: str) -> str:
        handle, path = tempfile.mkstemp(suffix='.jsonl')
        with os.fdopen(handle, 'w') as f:
            f.write(body)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def _assistant(self, text: str) -> str:
        return json.dumps({'type': 'assistant', 'message': {'content': text}}) + '\n'

    def _user(self, text: str) -> str:
        return json.dumps({'type': 'user', 'message': {'content': text}}) + '\n'

    def _padding(self, size: int) -> str:
        """Tool traffic, not assistant text — the kind of volume that evicts a
        turn from the window without carrying the token itself."""
        block = {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'content': 'x' * 2000}]}}
        line = json.dumps(block) + '\n'
        return line * (size // len(line) + 1)

    def test_token_in_window_passes(self):
        path = self._transcript(self._user('do it') + self._assistant(cg.SPEC_TOKEN))
        self.assertTrue(cg._token_after_last_prompt(path, cg.SPEC_TOKEN))

    def test_prompt_without_token_blocks(self):
        path = self._transcript(self._user('do it') + self._assistant('no token here'))
        self.assertFalse(cg._token_after_last_prompt(path, cg.SPEC_TOKEN))

    def test_short_transcript_without_either_still_blocks(self):
        path = self._transcript(self._assistant('no token, no prompt'))
        self.assertFalse(cg._token_after_last_prompt(path, cg.SPEC_TOKEN))

    def test_a_turn_evicted_by_tool_output_is_not_treated_as_unconfirmed(self):
        path = self._transcript(
            self._user('do it')
            + self._assistant(cg.SPEC_TOKEN)
            + self._padding(cg.TAIL_BYTES + 50_000)
        )
        self.assertGreater(os.path.getsize(path), cg.TAIL_BYTES)
        self.assertTrue(cg._token_after_last_prompt(path, cg.SPEC_TOKEN),
                        'a confirmed turn pushed out of the window must not block')

    def test_the_same_rule_applies_to_the_learn_token(self):
        path = self._transcript(
            self._user('that is wrong')
            + self._assistant(cg.LEARNED_TOKEN + ' rule written')
            + self._padding(cg.TAIL_BYTES + 50_000)
        )
        self.assertTrue(cg._token_after_last_prompt(path, cg.LEARNED_TOKEN))


class TestStateDurability(unittest.TestCase):

    def test_state_write_is_atomic(self):
        """A truncated state file silently resets every gate in the session."""
        directory = tempfile.mkdtemp(prefix='pb_state_')
        try:
            path = os.path.join(directory, '.state-x.json')
            playbook_core.save_state(path, {'gov_loaded': True})
            playbook_core.save_state(path, {'gov_loaded': True, 'spec_pending': True})
            self.assertEqual(playbook_core.load_state(path).get('spec_pending'), True)
            leftovers = [n for n in os.listdir(directory) if n.startswith('.state-tmp-')]
            self.assertEqual(leftovers, [], 'temp files must not survive a write')
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)


class TestCarryoverRedaction(unittest.TestCase):

    def test_a_secret_in_a_prompt_never_reaches_the_note(self):
        """The note is written to disk and re-injected later — a token pasted
        into a prompt must not outlive the session that leaked it."""
        import importlib.util as _util
        spec_cg = _util.spec_from_file_location(
            'compact_guard', os.path.join(_ROOT, 'hooks', 'compact_guard.py'))
        compact_guard = _util.module_from_spec(spec_cg)
        spec_cg.loader.exec_module(compact_guard)
        secret = 'sk-live-' + 'do-not-persist'
        entry = {'type': 'user', 'message': {'content': f'deploy with api_key={secret}'}}
        self.assertNotIn(secret, compact_guard._prompt_text(entry))


class TestConstants(unittest.TestCase):

    def test_block_pct_above_warn_pct(self):
        self.assertGreater(cg.BLOCK_PCT, cg.WARN_PCT)

    def test_unlock_pct_below_warn_pct(self):
        self.assertLess(cg.UNLOCK_PCT, cg.WARN_PCT)

    def test_impl_tools_contains_write_edit(self):
        self.assertIn('Write', cg.IMPL_TOOLS)
        self.assertIn('Edit', cg.IMPL_TOOLS)
        self.assertIn('MultiEdit', cg.IMPL_TOOLS)

    def test_spec_token_is_correct(self):
        self.assertEqual(cg.SPEC_TOKEN, '[SPEC CONFIRMED]')

    def test_learned_token_is_correct(self):
        self.assertEqual(cg.LEARNED_TOKEN, '[LEARNED]')

    def test_warn_pct_matches_the_native_autocompact_threshold(self):
        """Claude Code's own auto-compact fires at CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
        (default: window - 13k, i.e. ~99%). The playbook warn must not sit above
        the configured trigger, or it can never fire before compaction does."""
        self.assertLessEqual(cg.WARN_PCT, 0.70)
        self.assertGreaterEqual(cg.BLOCK_PCT, 0.85)

    def test_context_limit_env_override_respected(self):
        with mock.patch.dict(os.environ, {'PLAYBOOK_CONTEXT_LIMIT': '123456'}):
            spec2 = importlib.util.spec_from_file_location('cg2', _HOOK)
            cg2 = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(cg2)
        self.assertEqual(cg2.CONTEXT_LIMIT, 123_456)


if __name__ == '__main__':
    unittest.main(verbosity=2)
