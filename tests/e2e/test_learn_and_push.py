"""
End-to-end tests for the three hooks added on top of the context guard:

  learn_guard.py    — failure capture (PostToolUse) and correction detection
                      (UserPromptSubmit), plus the Stop-side learn gate it arms
                      in context_guard.py.
  compact_guard.py  — the carry-over note and the model's handoff, re-injected
                      by session_start.py after a compaction or a /clear.
  review gate       — context_guard.py denying an unreviewed `git push`.

Same contract as test_hook.py: every hook exits 0, a PreToolUse deny is a
permissionDecision payload, a Stop block is {"decision": "block"}.

The review gate shells out to git, so its tests build a real repository with a
real upstream — a mocked one would not exercise the fail-open paths (no repo,
nothing ahead of upstream) that decide whether the gate is safe to ship.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GUARD = os.path.join(_ROOT, 'hooks', 'context_guard.py')
_LEARN = os.path.join(_ROOT, 'hooks', 'learn_guard.py')
_COMPACT = os.path.join(_ROOT, 'hooks', 'compact_guard.py')
_SESSION_START = os.path.join(_ROOT, 'hooks', 'session_start.py')
_PYTHON = sys.executable

sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402
import playbook_i18n  # noqa: E402

_HOOK_DIR = os.path.expanduser('~/.claude/hooks')


def _run(script: str, payload: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_PYTHON, script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, 'PLAYBOOK_CONTEXT_LIMIT': '200000', **(env or {})},
    )


def _out(result: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}


def _ul(text: str) -> str:
    return json.dumps({'type': 'user', 'origin': {'kind': 'human'}, 'message': {'content': text}}) + '\n'


def _al(text: str) -> str:
    return json.dumps({'type': 'assistant', 'message': {'content': text}}) + '\n'


def _skill(name: str) -> str:
    block = {'type': 'tool_use', 'name': 'Skill', 'input': {'skill': name}}
    return json.dumps({'type': 'assistant', 'message': {'content': [block]}}) + '\n'


# Turn fixtures, shaped after the entries Claude Code records in a session
# transcript: what closes a turn, what starts one the user did not, and what it
# injects into a turn still running.

def _entry(**fields) -> str:
    return json.dumps(fields) + '\n'


def _typed(text: str, prompt_id: str = 'p-user', source: str = 'typed') -> str:
    return _entry(type='user', promptId=prompt_id, origin={'kind': 'human'}, promptSource=source,
                  turnOrigin='human', message={'role': 'user', 'content': text})


def _typed_mid_turn(text: str) -> str:
    return _entry(type='attachment', attachment={
        'type': 'queued_command', 'commandMode': 'prompt', 'origin': {'kind': 'human'},
        'humanTurn': True, 'prompt': text})


class _Base(unittest.TestCase):

    def setUp(self):
        self.session_id = f'pbtest-{uuid.uuid4().hex[:12]}'
        self.dir = tempfile.mkdtemp(prefix='pb_learn_')
        self.transcript = os.path.join(self.dir, 'session.jsonl')
        self._write('')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        for path in (self.state_path, self.journal_path, self.carryover_path):
            if os.path.exists(path):
                os.remove(path)

    @property
    def state_path(self) -> str:
        return os.path.join(_HOOK_DIR, f'.state-{self.session_id}.json')

    @property
    def journal_path(self) -> str:
        return os.path.join(_HOOK_DIR, f'.learn-{self.session_id}.jsonl')

    @property
    def carryover_path(self) -> str:
        return os.path.join(_HOOK_DIR, f'.carryover-{self.session_id}.md')

    def _write(self, content: str) -> None:
        with open(self.transcript, 'w') as f:
            f.write(content)

    def _state(self) -> dict:
        return playbook_core.load_state(self.state_path)

    def _set_state(self, **values) -> None:
        state = self._state()
        state['session_file'] = self.transcript
        state.update(values)
        playbook_core.save_state(self.state_path, state)

    def _learn(self, event: str, **payload) -> subprocess.CompletedProcess:
        return _run(_LEARN, {
            'session_id': self.session_id,
            'transcript_path': self.transcript,
            'hook_event_name': event,
            'cwd': self.dir,
            **payload,
        })

    def _stop(self, active: bool = False, final: str | None = None) -> subprocess.CompletedProcess:
        payload = {
            'session_id': self.session_id,
            'transcript_path': self.transcript,
            'hook_event_name': 'Stop',
            'cwd': self.dir,
            'stop_hook_active': active,
        }
        if final is not None:
            payload['last_assistant_message'] = final
        return _run(_GUARD, payload)


# ── Failure capture ───────────────────────────────────────────────────────────

class TestFailureCapture(_Base):

    def _fail(self, command: str = 'npm run buidl') -> subprocess.CompletedProcess:
        return self._learn(
            'PostToolUse',
            tool_name='Bash',
            tool_input={'command': command},
            tool_response={'is_error': True, 'content': 'command not found'},
        )

    def test_failed_tool_call_is_journaled(self):
        self._fail()
        rows = playbook_core.read_incidents(self.journal_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'tool_error')
        self.assertEqual(rows[0]['cwd'], self.dir)

    def test_successful_tool_call_journals_nothing(self):
        self._learn('PostToolUse', tool_name='Bash', tool_input={'command': 'ls'},
                    tool_response={'stdout': 'ok', 'stderr': ''})
        self.assertFalse(os.path.exists(self.journal_path))

    def test_single_failure_does_not_arm_the_gate(self):
        # Normal exploration. Blocking on one failed call would make the gate noise.
        self._fail()
        self.assertFalse(self._state().get('learn_pending'))

    def test_repeated_identical_failure_arms_the_gate(self):
        for _ in range(3):  # learn_guard.REPEAT_FAILURE_THRESHOLD
            self._fail()
        self.assertTrue(self._state().get('learn_pending'))
        kinds = [r['kind'] for r in playbook_core.read_incidents(self.journal_path)]
        self.assertIn('repeat_failure', kinds)

    def test_secrets_are_scrubbed_before_reaching_the_journal(self):
        self._learn('PostToolUse', tool_name='Bash',
                    tool_input={'command': 'curl -H "authorization: Bearer sk-live-42" x'},
                    tool_response={'is_error': True, 'content': 'api_key=sk-live-99 rejected'})
        raw = open(self.journal_path).read()
        self.assertNotIn('sk-live-42', raw)
        self.assertNotIn('sk-live-99', raw)

    def test_suspended_session_captures_nothing(self):
        self._set_state(suspended=True)
        self._fail()
        self.assertFalse(os.path.exists(self.journal_path))


# ── Correction detection ──────────────────────────────────────────────────────

class TestCorrectionDetection(_Base):

    def test_correction_arms_the_gate_and_injects_the_protocol(self):
        result = self._learn('UserPromptSubmit', prompt='não é isso, você assumiu errado')
        context = _out(result)['hookSpecificOutput']['additionalContext']
        self.assertIn(playbook_core.LEARNED_TOKEN, context)
        self.assertTrue(self._state().get('learn_pending'))

    def test_ordinary_prompt_is_not_a_correction(self):
        result = self._learn('UserPromptSubmit', prompt='e se o valor estiver errado, o que acontece?')
        self.assertEqual(result.stdout.strip(), '')
        self.assertFalse(self._state().get('learn_pending'))

    def test_english_correction_is_detected(self):
        self._learn('UserPromptSubmit', prompt="that's wrong, not what I asked for")
        self.assertTrue(self._state().get('learn_pending'))

    def test_text_the_user_did_not_type_is_not_a_correction(self):
        """A subagent report, a notification and local command output reach the
        hook as prompts. One per form: each quotes a correction phrase."""
        for prompt in (
            '<agent-message from="a1b2c3">\n[Subagent hand-back] the earlier claim is wrong; that\'s incorrect',
            '<task-notification>\n<summary>build still failing</summary>\n</task-notification>',
            '<system-reminder>\nreverte isso\n</system-reminder>',
            '<local-command-stdout>undo that</local-command-stdout>',
            '[SYSTEM NOTIFICATION - NOT USER INPUT]\nyou assumed the wrong file',
        ):
            with self.subTest(prompt=prompt[:24]):
                result = self._learn('UserPromptSubmit', prompt=prompt)
                self.assertEqual(result.stdout.strip(), '')
                self.assertFalse(self._state().get('learn_pending'))

    def test_a_pasted_block_is_not_the_users_words(self):
        pasted = '<pasted_content id="ab12">\nstep 3: reverte o deploy; não é isso\n</pasted_content id="ab12">'
        result = self._learn('UserPromptSubmit', prompt=f'segue o runbook\n\n{pasted}')
        self.assertEqual(result.stdout.strip(), '')
        self.assertFalse(self._state().get('learn_pending'))

    def test_an_unclosed_pasted_block_runs_to_the_end(self):
        self._learn('UserPromptSubmit', prompt='veja o log\n<pasted_content id="ab12">\nstill failing')
        self.assertFalse(self._state().get('learn_pending'))

    def test_a_correction_typed_around_a_pasted_block_still_counts(self):
        pasted = '<pasted_content id="ab12">\nTraceback ...\n</pasted_content id="ab12">'
        self._learn('UserPromptSubmit', prompt=f'não é isso, olha o erro:\n{pasted}')
        self.assertTrue(self._state().get('learn_pending'))

    def test_state_claims_the_transcript_so_the_guard_does_not_reset_it(self):
        # context_guard resets state whenever session_file does not match the
        # live transcript. A flag written before the first tool call must survive.
        self._learn('UserPromptSubmit', prompt='reverte isso')
        self.assertEqual(self._state().get('session_file'), self.transcript)


# ── Learn gate (Stop) ─────────────────────────────────────────────────────────

class TestLearnGate(_Base):

    def test_the_token_only_in_the_message_that_ended_the_turn_clears_the_gate(self):
        self._write(_ul('não é isso'))
        self._set_state(learn_pending=True)
        result = self._stop(final=f'{playbook_core.LEARNED_TOKEN} the rule is in CLAUDE.md')
        self.assertNotEqual(_out(result).get('decision') if result.stdout.strip() else None, 'block')
        self.assertFalse(self._state().get('learn_pending'))

    def test_stop_blocks_until_the_lesson_is_distilled(self):
        self._write(_ul('isso está errado') + _al('fixed it'))
        self._set_state(learn_pending=True, learn_reason='[playbook:learn] distill it')
        result = self._stop()
        self.assertEqual(_out(result).get('decision'), 'block')
        self.assertIn('playbook:learn', _out(result).get('reason', ''))

    def test_learned_token_clears_the_gate(self):
        self._write(_ul('isso está errado')
                    + _al(f'{playbook_core.LEARNED_TOKEN} hooks.json needs a restart to register'))
        self._set_state(learn_pending=True)
        self.assertNotEqual(_out(self._stop()).get('decision'), 'block')
        self.assertFalse(self._state().get('learn_pending'))

    def test_token_from_a_previous_turn_does_not_clear_it(self):
        self._write(_al(f'{playbook_core.LEARNED_TOKEN} old lesson') + _ul('isso está errado'))
        self._set_state(learn_pending=True)
        self.assertEqual(_out(self._stop()).get('decision'), 'block')

    def test_gate_does_not_loop_after_blocking_once(self):
        self._write(_ul('isso está errado') + _al('no token here'))
        self._set_state(learn_pending=True)
        self.assertEqual(_out(self._stop()).get('decision'), 'block')
        self.assertNotEqual(_out(self._stop(active=True)).get('decision'), 'block')

    def test_suspension_releases_the_gate(self):
        self._write(_skill('playbook-off') + _ul('isso está errado'))
        self._set_state(learn_pending=True)
        self.assertNotEqual(_out(self._stop()).get('decision'), 'block')


# ── Carry-over ────────────────────────────────────────────────────────────────

class TestCarryover(_Base):

    def setUp(self):
        super().setUp()
        import compact_guard
        self.project_note = compact_guard.project_carryover_path(self.dir)
        self.handoff = compact_guard.handoff_path(self.dir)

    def tearDown(self):
        for path in (self.project_note, self.handoff):
            if os.path.exists(path):
                os.remove(path)
        super().tearDown()

    def _precompact(self) -> subprocess.CompletedProcess:
        return _run(_COMPACT, {
            'session_id': self.session_id,
            'transcript_path': self.transcript,
            'hook_event_name': 'PreCompact',
            'trigger': 'auto',
            'cwd': self.dir,
        })

    def _session_start(self, source: str, session_id: str | None = None) -> str:
        result = _run(_SESSION_START, {
            'session_id': session_id or self.session_id,
            'source': source,
            'cwd': self.dir,
        })
        return _out(result)['hookSpecificOutput']['additionalContext']

    def _after_clear(self) -> str:
        """A /clear starts a NEW session_id -- measured on real transcripts."""
        return self._session_start('clear', f'pbtest-{uuid.uuid4().hex[:12]}')

    def test_note_keeps_the_original_request(self):
        self._write(_ul('build the payment webhook') + _al('ok') * 3
                    + _ul('now add retries'))
        self._precompact()
        note = open(self.carryover_path).read()
        self.assertIn('build the payment webhook', note)
        self.assertIn('now add retries', note)

    def test_note_records_armed_gates(self):
        self._write(_ul('do the thing'))
        self._set_state(pending_validation=True)
        self._precompact()
        self.assertIn('validation command still owed', open(self.carryover_path).read())

    def test_session_start_reinjects_the_note_after_a_compact(self):
        self._write(_ul('build the payment webhook'))
        self._precompact()
        self.assertIn('build the payment webhook', self._session_start('compact'))

    def test_a_cleared_session_gets_the_note_despite_its_new_session_id(self):
        self._write(_ul('build the payment webhook'))
        self._precompact()
        self.assertIn('build the payment webhook', self._after_clear())

    def test_stop_saves_the_handoff_block(self):
        self._write(_ul('build the payment webhook')
                    + _al('Done with the parser.\n[HANDOFF]\nNext: wire retries in '
                          'src/webhook.ts\n[/HANDOFF]\nRun /clear.'))
        self._stop()
        self.assertIn('wire retries in src/webhook.ts', open(self.handoff).read())

    def test_a_handoff_only_in_the_message_that_ended_the_turn_is_saved(self):
        self._write(_ul('build the payment webhook'))
        self._stop(final='Parser done.\n[HANDOFF]\nNext: wire retries in src/webhook.ts\n[/HANDOFF]\nRun /clear.')
        self.assertIn('wire retries in src/webhook.ts', open(self.handoff).read())

    def test_an_inline_mention_of_the_markers_is_not_saved(self):
        self._write(_ul('how does it work?')
                    + _al('The block goes between `[HANDOFF]` and `[/HANDOFF]`.'))
        self._stop()
        self.assertFalse(os.path.exists(self.handoff))

    def test_a_handoff_from_an_earlier_turn_is_not_captured_again(self):
        self._write(_al('[HANDOFF]\nold task\n[/HANDOFF]') + _ul('something else') + _al('ok'))
        self._stop()
        self.assertFalse(os.path.exists(self.handoff))

    def test_a_turn_blocked_by_a_gate_still_saves_its_handoff(self):
        """A gate block ends the turn on a stop_hook_active Stop that returns early,
        so the capture must run before any gate can exit."""
        self._write(_ul('build it') + _al('[HANDOFF]\nnext: the retries\n[/HANDOFF]'))
        self._set_state(pending_validation=True)
        self._stop()
        self.assertIn('next: the retries', open(self.handoff).read())

    def test_clear_injects_the_handoff_and_consumes_it(self):
        self._write(_ul('build it') + _al('[HANDOFF]\nnext: the retries\n[/HANDOFF]'))
        self._stop()
        self.assertIn('next: the retries', self._after_clear())
        self.assertFalse(os.path.exists(self.handoff))
        self.assertNotIn('next: the retries', self._after_clear())

    def test_stale_handoff_is_not_reinjected_into_a_cleared_session(self):
        import compact_guard
        self._write(_ul('build it') + _al('[HANDOFF]\nnext: the retries\n[/HANDOFF]'))
        self._stop()
        old = time.time() - compact_guard.FRESH_SECONDS - 60
        os.utime(self.handoff, (old, old))
        self.assertNotIn('next: the retries', self._after_clear())

    def test_startup_never_carries_a_note(self):
        self._write(_ul('build it') + _al('[HANDOFF]\nnext: the retries\n[/HANDOFF]'))
        self._stop()
        self._precompact()
        context = self._session_start('startup')
        self.assertNotIn('next: the retries', context)
        self.assertNotIn('build it', context)


# ── Review gate (pre-push) ────────────────────────────────────────────────────

class _RepoBase(_Base):
    """A real repository with a real bare upstream, `main` pushed once."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.dir, 'repo')
        self.remote = os.path.join(self.dir, 'remote.git')
        self._git_init()

    def _git(self, *args, cwd=None):
        return subprocess.run(
            ('git', '-C', cwd or self.repo) + args,
            capture_output=True, text=True,
        )

    def _git_init(self):
        subprocess.run(('git', 'init', '-q', '--bare', self.remote), capture_output=True)
        subprocess.run(('git', 'init', '-q', '-b', 'main', self.repo), capture_output=True)
        self._git('config', 'user.email', 'test@example.com')
        self._git('config', 'user.name', 'test')
        self._commit('README.md', 'first')
        self._git('remote', 'add', 'origin', self.remote)
        self._git('push', '-q', '-u', 'origin', 'main')

    def _commit(self, name: str, text: str):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        self._git('add', '-A')
        self._git('commit', '-q', '-m', text)

    def _push(self, command: str = 'git push origin main', env=None):
        return _run(_GUARD, {
            'session_id': self.session_id,
            'transcript_path': self.transcript,
            'hook_event_name': 'PreToolUse',
            'cwd': self.repo,
            'tool_name': 'Bash',
            'tool_input': {'command': command},
        }, env=env)

    def _denied(self, result) -> bool:
        return _out(result).get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'


class TestReviewGate(_RepoBase):

    def test_unreviewed_commit_is_denied(self):
        self._commit('src/app.ts', 'unreviewed')
        result = self._push()
        self.assertTrue(self._denied(result))
        self.assertIn('review-gate', _out(result).get('systemMessage', ''))

    def test_review_of_the_current_head_allows_the_push(self):
        self._commit('src/app.ts', 'reviewed')
        self._write(_skill('engineering-code-review'))
        self._push()  # the scan that records the reviewed HEAD
        self.assertFalse(self._denied(self._push()))

    def test_a_commit_after_the_review_re_arms_the_gate(self):
        self._commit('src/app.ts', 'reviewed')
        self._write(_skill('engineering-code-review'))
        self._push()
        self._commit('src/extra.ts', 'sneaked in')
        self.assertTrue(self._denied(self._push()))

    def test_a_waiver_typed_in_the_chat_does_not_allow_the_push(self):
        self._commit('src/app.ts', 'unreviewed')
        for waiver in ('pode pushar direto, sem review', 'skip review', 'no review needed'):
            with self.subTest(waiver=waiver):
                self._write(_ul(waiver))
                result = self._push()
                self.assertTrue(self._denied(result))
                self.assertIn('Nothing written in the chat', _out(result).get('systemMessage', ''))

    def test_model_cannot_write_its_own_bypass(self):
        # The phrase only counts in the user's message; assistant text must not clear it.
        self._commit('src/app.ts', 'unreviewed')
        self._write(_ul('push it') + _al('the user said skip review'))
        self.assertTrue(self._denied(self._push()))

    def test_environment_bypass_allows_the_push(self):
        self._commit('src/app.ts', 'unreviewed')
        self.assertFalse(self._denied(self._push(env={'PLAYBOOK_SKIP_REVIEW': '1'})))

    def test_dry_run_is_not_gated(self):
        self._commit('src/app.ts', 'unreviewed')
        self.assertFalse(self._denied(self._push('git push --dry-run origin main')))
        self.assertFalse(self._denied(self._push('git push -q -n origin main')))

    def test_a_publishing_push_chained_with_a_dry_run_is_denied(self):
        """The dry run is read per push: the other push of the command still publishes."""
        self._commit('src/app.ts', 'unreviewed')
        for command in ('git push --dry-run origin main && git push origin main',
                        'git push origin main; git push --dry-run origin main'):
            with self.subTest(command=command):
                result = self._push(command)
                self.assertTrue(self._denied(result))
                self.assertIn('review-gate', _out(result).get('systemMessage', ''))

    def test_porcelain_push_is_gated(self):
        """`--porcelain` changes the output; the push still happens."""
        self._commit('src/app.ts', 'unreviewed')
        self.assertTrue(self._denied(self._push('git push --porcelain origin main')))

    def test_a_dry_run_inside_another_program_is_read_per_push(self):
        """The forms the whole-command `--dry-run` match exempted by accident."""
        self._commit('src/app.ts', 'unreviewed')
        for command in ('echo main | xargs git push --dry-run origin',
                        "sh -c 'git push --dry-run origin main'",
                        'eval git push --dry-run origin main'):
            with self.subTest(command=command):
                self.assertFalse(self._denied(self._push(command)))
        for command in ('echo main | xargs git push --dry-run origin && git push origin main',
                        "sh -c 'git push --dry-run origin main && git push origin main'",
                        'eval "git push --dry-run origin main; git push origin main"',
                        r'find . -maxdepth 0 -exec git push --dry-run origin main \;',
                        'watch git push --dry-run origin main',
                        'flock /tmp/l git push --dry-run origin main'):
            with self.subTest(command=command):
                self.assertTrue(self._denied(self._push(command)))

    def test_push_that_only_deletes_is_not_gated(self):
        self._commit('src/app.ts', 'unreviewed')
        self.assertFalse(self._denied(self._push('git push origin --delete old-branch')))
        self.assertFalse(self._denied(self._push('git push origin :old-branch')))

    def test_delete_mixed_with_a_publishing_push_is_gated(self):
        self._commit('src/app.ts', 'unreviewed')
        self.assertTrue(self._denied(self._push('git push origin --delete old && git push origin main')))

    def test_nothing_ahead_of_upstream_is_not_gated(self):
        self.assertFalse(self._denied(self._push()))

    def test_outside_a_repository_the_gate_fails_open(self):
        result = _run(_GUARD, {
            'session_id': self.session_id,
            'transcript_path': self.transcript,
            'hook_event_name': 'PreToolUse',
            'cwd': self.dir,
            'tool_name': 'Bash',
            'tool_input': {'command': 'git push origin main'},
        })
        self.assertFalse(self._denied(result))

    def test_suspension_releases_the_gate(self):
        self._commit('src/app.ts', 'unreviewed')
        self._write(_skill('playbook-off'))
        self.assertFalse(self._denied(self._push()))


class TestReviewGateAcrossWorktrees(_RepoBase):
    """The session's directory is the main checkout; the push runs in another
    worktree of the same repository."""

    def setUp(self):
        super().setUp()
        self.worktree = os.path.join(self.dir, 'wt')
        self._git('worktree', 'add', '-q', '-b', 'feat', self.worktree)

    def _commit_in(self, root: str, name: str, text: str):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        self._git('add', '-A', cwd=root)
        self._git('commit', '-q', '-m', text, cwd=root)

    def _review(self):
        self._write(_skill('engineering-code-review'))
        self._push('git status')

    def _push_from_worktree(self, env=None):
        return self._push(f'cd {self.worktree} && git push origin feat', env=env)

    def test_a_commit_in_the_worktree_after_the_review_is_denied(self):
        self._commit_in(self.worktree, 'src/app.ts', 'reviewed')
        self._review()
        self._commit_in(self.worktree, 'src/extra.ts', 'sneaked in')
        result = self._push_from_worktree()
        self.assertTrue(self._denied(result))
        self.assertIn('review-gate', _out(result).get('systemMessage', ''))

    def test_the_reviewed_worktree_is_allowed_when_only_the_session_directory_moved(self):
        self._commit_in(self.worktree, 'src/app.ts', 'reviewed')
        self._review()
        self._commit('src/unrelated.ts', 'made elsewhere')
        self.assertFalse(self._denied(self._push_from_worktree()))

    def test_a_branch_no_review_contains_is_denied_while_head_is_reviewed(self):
        self._commit_in(self.worktree, 'src/app.ts', 'never reviewed')
        self._git('worktree', 'remove', '--force', self.worktree)
        self._commit('src/app.ts', 'reviewed')
        self._review()
        self.assertFalse(self._denied(self._push()))
        self.assertTrue(self._denied(self._push('git push origin feat')))

    def test_a_push_whose_directory_is_computed_fails_closed(self):
        # The attribution and secret gates deny this push before the review
        # gate runs, so the review gate is called on its own here.
        self._commit_in(self.worktree, 'src/app.ts', 'reviewed')
        self._review()
        self.assertTrue(self._denied(self._push('cd "$WT" && git push origin feat')))
        spec = importlib.util.spec_from_file_location('context_guard_review', _GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        guard._CWD = self.repo
        for command in ('cd "$WT" && git push origin feat', 'git push origin "$BRANCH"',
                        f'cd {self.dir}/not-yet && git push origin feat'):
            verdict = guard._check_push_gate(self._state(), command)
            self.assertTrue(verdict and verdict['block'], command)
            self.assertIn('could not determine', verdict['msg'])
        self.assertIsNone(guard._check_push_gate(
            self._state(), f'cd {self.worktree} && git push origin feat'))


class TestUnknownPushOption(_RepoBase):
    """A push option outside the closed set the gates read is denied by each
    of the three push gates, naming the word; the plain spelling passes."""

    UNKNOWN = ('git push -fn origin main', 'git push --dr origin main',
               'git push -n --no-dry-run origin main', 'git push -d --no-delete origin main',
               'git push --mir origin', 'git push --future-flag origin main')

    def _msg(self, result) -> str:
        return _out(result).get('systemMessage', '')

    def test_the_attribution_gate_denies_it(self):
        self._commit('src/app.ts', 'clean')
        self._write(_skill('engineering-code-review'))
        for command in self.UNKNOWN:
            with self.subTest(command=command):
                result = self._push(command)
                self.assertTrue(self._denied(result))
                self.assertIn('[playbook:commit]', self._msg(result))
                self.assertIn('does not read the option', self._msg(result))
        self.assertFalse(self._denied(self._push('git push -q -n origin main')))

    def test_the_secrets_gate_denies_it(self):
        self._commit('src/app.ts', 'clean')
        self._write(_skill('engineering-code-review'))
        allow = {playbook_core.ATTRIBUTION_ALLOW_ENV: '1'}
        result = self._push('git push --dr origin main', env=allow)
        self.assertTrue(self._denied(result))
        self.assertIn('[playbook:secrets]', self._msg(result))
        self.assertIn('`--dr`', self._msg(result))
        self.assertFalse(self._denied(self._push('git push --dry-run origin main', env=allow)))

    def test_the_review_gate_denies_it(self):
        # The attribution and secret gates deny first through the hook, so the
        # review gate is called on its own.
        self._commit('src/app.ts', 'unreviewed')
        spec = importlib.util.spec_from_file_location('context_guard_review', _GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        guard._CWD = self.repo
        for command in self.UNKNOWN:
            with self.subTest(command=command):
                verdict = guard._check_push_gate(self._state(), command)
                self.assertTrue(verdict and verdict['block'])
                self.assertIn('[playbook:review-gate]', verdict['msg'])
                self.assertIn('does not read the option', verdict['msg'])
        self.assertIsNone(guard._check_push_gate(self._state(), 'git push --dry-run origin main'))


class TestAttributionInHistory(_RepoBase):
    """The publication point. Every way a message reaches a remote -- a file a
    script wrote, an amend or cherry-pick reusing an old message, a commit made
    outside the session, the PR body a forge pre-fills from one commit --
    crosses a push, so the push reads the commits themselves, not the command.

    Each test reviews first, so a denial can only come from attribution."""

    # Split so this file never contains the literal strings the gates match on.
    TRAILER = 'Co-Authored' + '-By: Claude Opus 5.5 <noreply@anthropic.com>'

    def setUp(self):
        super().setUp()
        self._write(_skill('engineering-code-review'))

    def _commit_message(self, name: str, message: str):
        path = os.path.join(self.repo, name)
        with open(path, 'w') as f:
            f.write(name)
        self._git('add', '-A')
        self._git('commit', '-q', '-m', message)

    def _attributed(self, name: str = 'src/app.ts'):
        os.makedirs(os.path.join(self.repo, 'src'), exist_ok=True)
        self._commit_message(name, f'feat: x\n\n{self.TRAILER}')

    def _msg(self, result) -> str:
        return _out(result).get('systemMessage', '')

    def _post(self, command: str, env=None):
        return _run(_GUARD, {
            'session_id': self.session_id,
            'transcript_path': self.transcript,
            'hook_event_name': 'PostToolUse',
            'cwd': self.repo,
            'tool_name': 'Bash',
            'tool_input': {'command': command},
            'tool_response': {'success': True},
        }, env=env)

    def _pre_bash(self, command: str):
        return self._push(command)

    # ── push: the gate that has to hold ──

    def test_push_of_a_commit_with_attribution_is_denied(self):
        self._attributed()
        result = self._push()
        self.assertTrue(self._denied(result))
        self.assertIn('playbook:commit', self._msg(result))
        self.assertIn('feat: x', self._msg(result))

    def test_attribution_below_a_clean_head_is_still_denied(self):
        """A cherry-picked or rebased commit under later clean work."""
        self._attributed()
        self._commit('src/later.ts', 'feat: later, clean')
        self.assertTrue(self._denied(self._push()))

    def test_explicit_refspec_is_what_gets_checked(self):
        """`<sha>:refs/heads/x` publishes that sha, not HEAD."""
        self._attributed()
        dirty = self._git('rev-parse', 'HEAD').stdout.strip()
        self._git('checkout', '-q', '-b', 'clean', 'origin/main')
        self._commit('src/clean.ts', 'feat: clean')
        self._write(_skill('engineering-code-review'))
        self._push('git push origin clean')  # records the review of this HEAD
        self.assertFalse(self._denied(self._push('git push origin clean')))
        self.assertTrue(self._denied(self._push(f'git push origin {dirty}:refs/heads/x')))

    def _attributed_tag(self, name: str = 'v1.0'):
        self._git('tag', '-a', name, '-m', 'release\n\n' + self.TRAILER)

    def test_annotated_tag_with_attribution_is_denied_with_tags(self):
        """The tag message was a documented gap until 2026-11-16."""
        self._commit('src/app.ts', 'feat: clean')
        self._attributed_tag()
        self._write(_skill('engineering-code-review'))
        self._push()  # records the review of this HEAD
        for command in ('git push origin --tags', 'git push origin v1.0',
                        'git push --follow-tags origin main'):
            with self.subTest(command=command):
                result = self._push(command)
                self.assertTrue(self._denied(result))
                self.assertIn('tag v1.0', self._msg(result))

    def test_clean_annotated_tag_passes(self):
        self._commit('src/app.ts', 'feat: clean')
        self._git('tag', '-a', 'v1.0', '-m', 'release')
        self._write(_skill('engineering-code-review'))
        self._push()
        self.assertFalse(self._denied(self._push('git push origin --tags')))

    def test_push_through_a_global_option_is_still_checked(self):
        self._attributed()
        self.assertTrue(self._denied(self._push('git -c push.default=current push')))

    def test_a_dry_run_elsewhere_in_the_command_does_not_exempt_the_push(self):
        self._attributed()
        self.assertTrue(self._denied(
            self._push('git push --dry-run origin main && git push origin main')))

    def test_porcelain_push_is_still_checked(self):
        """`--porcelain` changes the output; the push still happens."""
        self._attributed()
        self.assertTrue(self._denied(self._push('git push --porcelain origin main')))

    def test_redirected_clean_push_passes(self):
        self._commit('src/app.ts', 'feat: clean')
        self._push()
        self.assertFalse(self._denied(self._push('git push origin main 2>&1 | tail -3')))

    def test_clean_push_passes(self):
        self._commit('src/app.ts', 'feat: clean')
        self._push()  # records the review of this HEAD
        self.assertFalse(self._denied(self._push()))

    def test_commits_already_on_the_remote_are_not_rechecked(self):
        """Published history is not this push's to fix."""
        self._attributed()
        self._git('push', '-q', 'origin', 'main')
        self._commit('src/next.ts', 'feat: next')
        self._push()
        self.assertFalse(self._denied(self._push()))

    def test_delete_only_push_passes(self):
        self._attributed()
        self.assertFalse(self._denied(self._push('git push origin --delete old-branch')))

    def test_unresolvable_refspec_fails_closed(self):
        self._commit('src/app.ts', 'feat: clean')
        result = self._push('git push origin "$BRANCH"')
        self.assertTrue(self._denied(result))
        self.assertIn('could not determine', self._msg(result))

    def test_user_environment_exception_allows_it(self):
        self._attributed()
        self._push()
        result = self._push(env={'PLAYBOOK_ALLOW_AI_ATTRIBUTION': '1'})
        self.assertNotIn('playbook:commit', self._msg(result))

    def test_the_exception_cannot_be_set_from_the_command(self):
        self._attributed()
        self.assertTrue(self._denied(self._push('PLAYBOOK_ALLOW_AI_ATTRIBUTION=1 git push')))

    # ── right after the commit: the early warning ──

    def test_commit_from_a_file_with_attribution_is_flagged_after_it_runs(self):
        """The message came from a file; nothing in the command shows it."""
        self._attributed()
        result = self._post('git commit -q -F msg.md')
        self.assertEqual(_out(result).get('decision'), 'block')
        self.assertIn('playbook:commit', _out(result).get('reason', ''))

    def test_clean_commit_is_not_flagged(self):
        self._commit('src/app.ts', 'feat: clean')
        self.assertNotEqual(_out(self._post('git commit -q -F msg.md')).get('decision'), 'block')

    def test_non_history_command_does_not_read_history(self):
        self._attributed()
        self.assertNotEqual(_out(self._post('git status')).get('decision'), 'block')

    # ── message and body files named by the command ──

    def _file(self, name: str, text: str) -> None:
        with open(os.path.join(self.repo, name), 'w') as f:
            f.write(text)

    def test_commit_message_file_with_attribution_is_denied_before_the_commit(self):
        self._file('msg.txt', f'fix: x\n\n{self.TRAILER}\n')
        for command in ('git commit -F msg.txt', 'git commit --file=msg.txt',
                        'git commit --template msg.txt'):
            with self.subTest(command=command):
                self.assertTrue(self._denied(self._pre_bash(command)))

    def test_pr_body_file_with_attribution_is_denied(self):
        self._file('body.md', f'does x\n\n{self.TRAILER}\n')
        self.assertTrue(self._denied(self._pre_bash('gh pr create --title x --body-file body.md')))

    def test_clean_message_file_passes(self):
        self._file('msg.txt', 'fix: x\n')
        self.assertFalse(self._denied(self._pre_bash('git commit -F msg.txt')))


# ── Destructive-command consent: the harness's permission dialog ──────────────

class TestDestructiveConsentByDialog(_Base):
    """Consent to a destructive command is the user's answer to the harness's
    permission dialog, never text read from the transcript. The dialog is
    raised only where it is known to reach the user; everywhere else the
    command is denied and handed to the user to run.

    Payloads follow the shape a live Claude Code session sent a plugin hook
    (auto mode, PreToolUse Bash)."""

    DESTRUCTIVE = 'rm -rf build/'
    DENY_TAIL = 'ask the user to run the command themselves'

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.dir, 'home')
        self.tmp = os.path.join(self.dir, 'tmp')
        os.makedirs(os.path.join(self.home, '.claude', 'hooks'))
        os.makedirs(self.tmp)
        self._language('en')

    def _language(self, language: str) -> None:
        with open(os.path.join(self.home, '.claude', 'hooks', '.playbook-prefs.json'), 'w') as f:
            json.dump({'language': language}, f)

    def _bash(self, command: str, mode: object = 'auto', env: dict | None = None,
              **extra) -> subprocess.CompletedProcess:
        payload = {
            'session_id': self.session_id,
            'cwd': self.dir,
            'prompt_id': 'p-user',
            'effort': {'level': 'high'},
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': command, 'description': 'Remove the build output'},
            'tool_use_id': 'toolu_019FFZDbQDfBhkQX2FpJVTHu',
            'transcript_path': self.transcript,
            **extra,
        }
        if mode is not None:
            payload['permission_mode'] = mode
        return _run(_GUARD, payload, env={'HOME': self.home, 'TMPDIR': self.tmp,
                                          'CLAUDE_CODE_REMOTE': '', **(env or {})})

    def _decision(self, result) -> str | None:
        return _out(result).get('hookSpecificOutput', {}).get('permissionDecision')

    def _ask_reason(self, language: str, effect: str, command: str) -> str:
        return playbook_i18n.text(language, 'destructive_ask',
                                  effect=playbook_i18n.text(language, f'destroys_{effect}'),
                                  command=command)

    def _assert_denied_to_the_user(self, result) -> None:
        self.assertEqual(self._decision(result), 'deny')
        self.assertIn(self.DENY_TAIL, _out(result)['hookSpecificOutput']['permissionDecisionReason'])

    def test_each_dialog_mode_asks_with_the_exact_shape(self):
        for mode in ('default', 'auto'):
            with self.subTest(mode=mode):
                self.assertEqual(_out(self._bash(self.DESTRUCTIVE, mode)), {'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'ask',
                    'permissionDecisionReason': self._ask_reason('en', 'delete_tree', self.DESTRUCTIVE),
                }})

    def test_the_reason_is_in_the_configured_language(self):
        for language in ('pt-br', 'es'):
            with self.subTest(language=language):
                self._language(language)
                reason = _out(self._bash(self.DESTRUCTIVE))['hookSpecificOutput'][
                    'permissionDecisionReason']
                self.assertEqual(reason, self._ask_reason(language, 'delete_tree', self.DESTRUCTIVE))
                self.assertIn(f'`{self.DESTRUCTIVE}`', reason)

    def test_the_reason_says_what_each_command_destroys(self):
        for command, effect in (('git reset --hard HEAD~1', 'reset_hard'),
                                ('git clean -fdx', 'clean'),
                                ('git stash drop', 'stash_drop'),
                                ('psql -c "DROP TABLE users;"', 'sql'),
                                ('x=rm; $x -rf build', 'dynamic')):
            with self.subTest(command=command):
                self.assertEqual(
                    _out(self._bash(command))['hookSpecificOutput']['permissionDecisionReason'],
                    self._ask_reason('en', effect, command))

    def test_modes_outside_the_set_deny(self):
        for mode in ('acceptEdits', 'plan', 'dontAsk', 'bypassPermissions'):
            with self.subTest(mode=mode):
                self._assert_denied_to_the_user(self._bash(self.DESTRUCTIVE, mode))

    def test_an_unknown_mode_value_denies(self):
        for mode in ('manual', 'yolo', '', 7, ['auto']):
            with self.subTest(mode=mode):
                self._assert_denied_to_the_user(self._bash(self.DESTRUCTIVE, mode))

    def test_a_payload_without_a_mode_denies(self):
        self._assert_denied_to_the_user(self._bash(self.DESTRUCTIVE, None))

    def test_a_subagent_payload_denies(self):
        for mode in ('default', 'auto'):
            with self.subTest(mode=mode):
                self._assert_denied_to_the_user(self._bash(
                    self.DESTRUCTIVE, mode, agent_id='a1b2c3', agent_type='general-purpose'))

    def test_a_cloud_session_denies(self):
        self._assert_denied_to_the_user(self._bash(self.DESTRUCTIVE, env={'CLAUDE_CODE_REMOTE': 'true'}))

    def test_the_gate_needs_no_transcript(self):
        missing = os.path.join(self.dir, 'never-written.jsonl')
        self.assertEqual(self._decision(self._bash(self.DESTRUCTIVE, transcript_path=missing)), 'ask')
        self.assertEqual(self._decision(self._bash(self.DESTRUCTIVE, None, transcript_path=missing)),
                         'deny')

    def test_consent_typed_in_the_chat_lifts_nothing(self):
        self._write(_typed('pode apagar a pasta build') + _typed_mid_turn('go ahead and delete it'))
        self.assertEqual(self._decision(self._bash(self.DESTRUCTIVE)), 'ask')
        self._assert_denied_to_the_user(self._bash(self.DESTRUCTIVE, 'bypassPermissions'))

    def test_another_gates_deny_outranks_the_dialog(self):
        command = ('rm -rf build && git commit -m "fix: x\n\n'
                   'Co-Authored-By: Claude <noreply@anthropic.com>"')
        result = self._bash(command)
        self.assertEqual(self._decision(result), 'deny')
        self.assertNotIn('playbook:safety', _out(result)['hookSpecificOutput']['permissionDecisionReason'])

    def test_deleting_inside_the_session_scratchpad_still_needs_nothing(self):
        scratchpad = os.path.join(self.tmp, f'claude-{os.getuid()}', 'proj', self.session_id,
                                  'scratchpad')
        os.makedirs(os.path.join(scratchpad, 'old'))
        for mode in ('auto', None):
            with self.subTest(mode=mode):
                self.assertIsNone(self._decision(self._bash(f'rm -rf {scratchpad}/old', mode)))

    def test_writing_about_a_dangerous_command_is_not_running_it(self):
        command = "cat > docs/rollback.md <<'DOCEOF'\nNever run rm -rf / here.\nDOCEOF"
        self.assertIsNone(self._decision(self._bash(command)))

    def test_a_shell_heredoc_is_still_inspected(self):
        command = "bash <<'SHEOF'\nrm -rf /tmp/whatever\nSHEOF"
        self.assertEqual(self._decision(self._bash(command)), 'ask')
        self._assert_denied_to_the_user(self._bash(command, 'dontAsk'))


# ── Review findings feed the learn loop ───────────────────────────────────────

class TestReviewFeedsLearning(_Base):

    REVIEW_OUTPUT = (
        'Review of the staged diff.\n\n'
        '## Blockers\n'
        '- `src/user.repository.ts:42` — [engineering-performance] — query inside a loop — batch it\n'
        '- `src/auth.controller.ts:18` — [engineering-security] — token logged — redact it\n\n'
        '## Major\nnone\n\n## Minor\nnone\n'
    )

    def test_findings_arm_the_learn_gate(self):
        self._write(_ul('review and push') + _al(self.REVIEW_OUTPUT))
        self._set_state(review_turn=True, reviewed_head='abc1234')
        result = self._stop()
        self.assertEqual(_out(result).get('decision'), 'block')
        self.assertIn('playbook:learn', _out(result).get('reason', ''))

    def test_findings_only_in_the_message_that_ended_the_turn_arm_the_gate(self):
        self._write(_ul('review and push'))
        self._set_state(review_turn=True, reviewed_head='abc1234')
        result = self._stop(final=self.REVIEW_OUTPUT)
        self.assertIn('playbook:learn', _out(result).get('reason', ''))

    def test_findings_on_disk_and_in_the_event_are_counted_once(self):
        self._write(_ul('review and push') + _al(self.REVIEW_OUTPUT))
        self._set_state(review_turn=True, reviewed_head='abc1234')
        self.assertIn('reported 2 ', _out(self._stop(final=self.REVIEW_OUTPUT)).get('reason', ''))

    def test_findings_are_journaled_for_engineering_learn(self):
        self._write(_ul('review and push') + _al(self.REVIEW_OUTPUT))
        self._set_state(review_turn=True, reviewed_head='abc1234')
        self._stop()
        kinds = [r['kind'] for r in playbook_core.read_incidents(self.journal_path)]
        self.assertIn('review_finding', kinds)

    def test_a_clean_review_arms_nothing(self):
        self._write(_ul('review and push')
                    + _al('## Blockers\nnone\n\n## Major\nnone\n\n## Minor\nnone\n'))
        self._set_state(review_turn=True, reviewed_head='abc1234')
        self.assertNotEqual(_out(self._stop()).get('decision'), 'block')

    def test_the_gate_clears_once_the_rule_is_written(self):
        self._write(_ul('review and push') + _al(self.REVIEW_OUTPUT)
                    + _al(f'{playbook_core.LEARNED_TOKEN} batch repository reads in loops'))
        self._set_state(review_turn=True, reviewed_head='abc1234')
        self.assertNotEqual(_out(self._stop()).get('decision'), 'block')


class TestHeredocIsNotACommand(_Base):
    """A heredoc body is what a command writes, not what it runs.

    Both cases below denied a real commit of this feature before the gates
    learned the difference: a gate inspects the command, and reads the body only
    where the body itself is the subject — a git commit message."""

    # Split so this file never contains the literal strings the gates match on;
    # otherwise reading the test file trips the gate the test is about.
    PUSH = 'git ' + 'push'
    ATTRIBUTION = 'Co-Authored' + '-By: Claude <noreply@anthropic.com>'

    def _bash(self, command: str):
        return _run(_GUARD, {
            'session_id': self.session_id, 'transcript_path': self.transcript,
            'hook_event_name': 'PreToolUse', 'cwd': self.dir,
            'tool_name': 'Bash', 'tool_input': {'command': command},
        })

    def _denied(self, result) -> bool:
        return _out(result).get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'

    def _message(self, result) -> str:
        return _out(result).get('systemMessage', '')

    def test_commit_message_mentioning_a_push_is_not_a_push(self):
        command = (
            "git commit -F - <<'MEOF'\n"
            "feat(hooks): review gate\n\n"
            f"{self.PUSH} is denied until HEAD has been reviewed.\n"
            "MEOF"
        )
        self.assertNotIn('review-gate', self._message(self._bash(command)))

    def test_a_script_quoting_an_attribution_line_is_not_a_commit(self):
        command = f"python3 - <<'PEOF'\nprint({self.ATTRIBUTION!r})\nPEOF"
        self.assertNotIn('playbook:commit', self._message(self._bash(command)))

    def test_a_real_commit_attribution_is_still_blocked(self):
        command = f"git commit -F - <<'MEOF'\nfeat: x\n\n{self.ATTRIBUTION}\nMEOF"
        result = self._bash(command)
        self.assertTrue(self._denied(result))
        self.assertIn('playbook:commit', self._message(result))


# ── Preferences: prose, agent cap, watch ──────────────────────────────────────

class _PrefsBase(_Base):
    """Preferences live in the shared state dir, so each test installs its own and
    restores whatever was there — these tests must not rewrite the developer's
    real settings."""

    PREFS = os.path.join(_HOOK_DIR, '.playbook-prefs.json')

    def setUp(self):
        super().setUp()
        self._saved = None
        if os.path.exists(self.PREFS):
            with open(self.PREFS) as f:
                self._saved = f.read()

    def tearDown(self):
        if self._saved is None:
            if os.path.exists(self.PREFS):
                os.remove(self.PREFS)
        else:
            with open(self.PREFS, 'w') as f:
                f.write(self._saved)
        super().tearDown()

    def _set(self, **values) -> None:
        with open(self.PREFS, 'w') as f:
            json.dump({**playbook_core.PREFS_DEFAULTS, **values}, f)


class TestProseLevel(_PrefsBase):

    def _context(self) -> str:
        result = _run(_SESSION_START, {'session_id': self.session_id, 'source': 'startup'})
        return _out(result)['hookSpecificOutput']['additionalContext']

    def test_default_is_mid(self):
        self._set()
        self.assertIn('Prose budget: MID', self._context())

    def test_low_is_injected(self):
        self._set(prose='low')
        context = self._context()
        self.assertIn('Prose budget: LOW', context)
        self.assertNotIn('Prose budget: HIGH', context)

    def test_high_is_injected(self):
        self._set(prose='high')
        self.assertIn('Prose budget: HIGH', self._context())

    def test_every_level_forbids_ceremony(self):
        for level in playbook_core.PROSE_LEVELS:
            with self.subTest(level=level):
                self._set(prose=level)
                self.assertIn('preamble', self._context().lower())

    def test_corrupt_preferences_fall_back_to_defaults(self):
        with open(self.PREFS, 'w') as f:
            f.write('{not json at all')
        self.assertIn('Prose budget: MID', self._context())


class TestKnownFailureWatch(_PrefsBase):

    RULE = 'prisma-user.repository.ts needs reconstitute(), never create()'

    def _claude_md(self, body: str) -> None:
        with open(os.path.join(self.dir, 'CLAUDE.md'), 'w') as f:
            f.write('# Project\n\n<!-- engineering-learn:live -->\n'
                    f'- {body}\n<!-- /engineering-learn:live -->\n')

    def _write_file(self, path: str):
        return _run(_GUARD, {
            'session_id': self.session_id, 'transcript_path': self.transcript,
            'hook_event_name': 'PreToolUse', 'cwd': self.dir,
            'tool_name': 'Edit', 'tool_input': {'file_path': path},
        })

    def _message(self, result) -> str:
        return _out(result).get('systemMessage', '')

    def test_off_by_default(self):
        self._set()
        self._claude_md(self.RULE)
        self.assertNotIn('playbook:watch', self._message(self._write_file('src/prisma-user.repository.ts')))

    def test_warns_about_a_rule_matching_the_target(self):
        self._set(watch=True)
        self._claude_md(self.RULE)
        result = self._write_file('src/prisma-user.repository.ts')
        message = self._message(result)
        self.assertIn('playbook:watch', message)
        self.assertIn('reconstitute', message)
        # The model is the one about to repeat it: a systemMessage alone only
        # reaches the user (release review of 4.0.0).
        context = _out(result).get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('playbook:watch', context)

    def test_does_not_warn_about_an_unrelated_file(self):
        self._set(watch=True)
        self._claude_md(self.RULE)
        self.assertNotIn('playbook:watch', self._message(self._write_file('src/billing.service.ts')))

    def test_warning_never_blocks(self):
        self._set(watch=True)
        self._claude_md(self.RULE)
        result = self._write_file('src/prisma-user.repository.ts')
        self.assertNotEqual(
            _out(result).get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')

    def test_the_same_rule_fires_once_per_session(self):
        self._set(watch=True)
        self._claude_md(self.RULE)
        self.assertIn('playbook:watch', self._message(self._write_file('src/prisma-user.repository.ts')))
        self.assertNotIn('playbook:watch', self._message(self._write_file('src/prisma-user.repository.ts')))

    def test_a_project_without_claude_md_is_silent(self):
        self._set(watch=True)
        self.assertNotIn('playbook:watch', self._message(self._write_file('src/anything.ts')))


if __name__ == '__main__':
    unittest.main()
