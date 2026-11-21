"""
End-to-end tests for hooks/context_guard.py (plus the Codex adapter's
subagent isolation, which shares its state keying via playbook_core).

Each test spawns context_guard.py as a subprocess with the real hook stdin
contract: session_id + transcript_path + hook_event_name. State files are
keyed by session_id and cleaned up in tearDown.

Blocking contract asserted here (Claude Code hook semantics):
- PreToolUse deny → exit 0 + {"hookSpecificOutput": {"permissionDecision": "deny"}}
- Stop block      → exit 0 + {"decision": "block"}
- Warning         → exit 0 + {"systemMessage": ...} without a deny decision
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK = os.path.join(_ROOT, 'hooks', 'context_guard.py')
_PYTHON = sys.executable
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import context_guard  # noqa: E402  -- constants only; the hook runs as a subprocess


# ── Helpers ───────────────────────────────────────────────────────────────────

# Deviation reports (hooks/deviation_report.py) read the user's real preference
# and may start `gh`: every hook this module runs gets its own report store and
# a `gh` that only fails, so no test can send a report or spend the one-time
# notice that reports exist.
_REPORT_SANDBOX = tempfile.mkdtemp(prefix='pb_report_sandbox_')


def _fake_gh(bin_dir: str, log: str, issues: str = '[]', exit_code: int = 0) -> None:
    """A `gh` on PATH that records each call (argv, and the body file's text)
    in `log` and answers as gh documents: JSON for `issue list --json`, the
    issue URL for `issue create`."""
    os.makedirs(bin_dir, exist_ok=True)
    script = os.path.join(bin_dir, 'gh')
    with open(script, 'w') as f:
        f.write(f"""#!{_PYTHON}
import json, sys
argv = sys.argv[1:]
body = ''
if '--body-file' in argv:
    body = open(argv[argv.index('--body-file') + 1], encoding='utf-8').read()
with open({log!r}, 'a') as out:
    out.write(json.dumps({{'argv': argv, 'body': body}}) + '\\n')
if {exit_code}:
    sys.stderr.write('error connecting to api.github.com')
    sys.exit({exit_code})
if argv[:2] == ['issue', 'list']:
    print({issues!r})
elif argv[:2] == ['issue', 'create']:
    print('https://github.com/o/r/issues/77')
""")
    os.chmod(script, 0o755)


_fake_gh(os.path.join(_REPORT_SANDBOX, 'bin'), os.path.join(_REPORT_SANDBOX, 'gh.log'), exit_code=1)
_HOOK_ENV = {
    'PLAYBOOK_CONTEXT_LIMIT': '200000',
    'PLAYBOOK_REPORT_STORE': os.path.join(_REPORT_SANDBOX, 'reports.json'),
    'PLAYBOOK_REPORT_REPO': 'o/r',
    'PATH': os.path.join(_REPORT_SANDBOX, 'bin') + os.pathsep + os.environ.get('PATH', ''),
}

def _state_path(session_id: str) -> str:
    return os.path.expanduser(f'~/.claude/hooks/.state-{session_id}.json')


def _call(session_id: str, transcript: str, tool_name: str, tool_input: dict,
          event: str = 'PreToolUse', extra: dict | None = None) -> subprocess.CompletedProcess:
    payload = {
        'session_id': session_id,
        'transcript_path': transcript,
        'hook_event_name': event,
        'tool_name': tool_name,
        'tool_input': tool_input,
    }
    if extra:
        payload.update(extra)
    return subprocess.run(
        [_PYTHON, _HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **_HOOK_ENV},
    )


def _out(result: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}


def _decision(result: subprocess.CompletedProcess) -> str:
    return _out(result).get('hookSpecificOutput', {}).get('permissionDecision', '')


def _msg(result: subprocess.CompletedProcess) -> str:
    out = _out(result)
    return out.get('systemMessage', '') or out.get('reason', '')


def _al(content) -> str:
    return json.dumps({'type': 'assistant', 'message': {'content': content}}) + '\n'


def _ul(text: str) -> str:
    return json.dumps({'type': 'user', 'message': {'content': text}}) + '\n'


def _tool_use(name: str, tool_input: dict) -> str:
    block = {'type': 'tool_use', 'name': name, 'input': tool_input}
    return json.dumps({'type': 'assistant', 'message': {'content': [block]}}) + '\n'


def _skill(name: str) -> str:
    return _tool_use('Skill', {'skill': name})


def _skill_call(use_id: str, name: str) -> str:
    """A Skill call as Claude Code records it: with the id its result names."""
    block = {'type': 'tool_use', 'id': use_id, 'name': 'Skill', 'input': {'skill': name}}
    return json.dumps({'type': 'assistant', 'message': {'content': [block]}}) + '\n'


def _skill_result(use_id: str, name: str, failed: bool) -> str:
    text = (f'<tool_use_error>Unknown skill: {name}</tool_use_error>' if failed
            else f'Launching skill: {name}')
    block = {'type': 'tool_result', 'tool_use_id': use_id, 'content': text}
    if failed:
        block['is_error'] = True
    return json.dumps({'type': 'user', 'message': {'content': [block]}}) + '\n'


def _usage(tokens: int) -> str:
    return json.dumps({
        'type': 'assistant',
        'message': {
            'usage': {
                'input_tokens': tokens,
                'cache_creation_input_tokens': 0,
                'cache_read_input_tokens': 0,
            }
        }
    }) + '\n'


def _governed() -> str:
    """Governance plus every skill the write gate can require of a fixture's
    source file without a manifest or a topic: the companions and each language
    opt-in. A test about another gate loads them all, so only that gate denies."""
    with open(os.path.join(_ROOT, 'hooks', 'data', 'skill_requirements.json')) as f:
        table = json.load(f)
    names = ['engineering-governance', *table['companions'],
             *(row['skill'] for row in table['languages'].values())]
    return ''.join(_skill(n) for n in names)


# Standard preamble: governance skill loaded + user confirmation + spec token.
_READY = _governed() + _ul('ok, proceed') + _al('[SPEC CONFIRMED] implementing')


class _HookTest(unittest.TestCase):

    def setUp(self):
        self.session_id = f'pbtest-{uuid.uuid4().hex[:12]}'
        self.dir = tempfile.mkdtemp(prefix='pb_hook_')
        self.transcript = os.path.join(self.dir, 'session.jsonl')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        p = _state_path(self.session_id)
        if os.path.exists(p):
            os.remove(p)

    def _write(self, content: str) -> None:
        with open(self.transcript, 'w') as f:
            f.write(content)

    def _state(self) -> dict:
        try:
            with open(_state_path(self.session_id)) as f:
                return json.load(f)
        except Exception:
            return {}

    def _seed_state(self, **values) -> None:
        state = self._state()
        state['session_file'] = self.transcript
        state.update(values)
        with open(_state_path(self.session_id), 'w') as f:
            json.dump(state, f)

    def _pre_only(self, tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
        """PreToolUse alone: the tool was announced and never ran."""
        return _call(self.session_id, self.transcript, tool_name, tool_input)

    def _pre(self, tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
        """PreToolUse, plus the PostToolUse that follows it in a real session when
        the call was not denied. The deferred gates arm on the second event,
        because only that one means the tool actually ran."""
        result = self._pre_only(tool_name, tool_input)
        if _decision(result) != 'deny':
            _call(self.session_id, self.transcript, tool_name, tool_input,
                  event='PostToolUse', extra={'tool_response': {'success': True}})
        return result

    def _stop(self, active: bool = False) -> subprocess.CompletedProcess:
        return _call(self.session_id, self.transcript, '', {},
                     event='Stop', extra={'stop_hook_active': active})

    def _assert_denied(self, r, marker: str) -> None:
        self.assertEqual(r.returncode, 0, f'deny must exit 0, stderr: {r.stderr}')
        self.assertEqual(_decision(r), 'deny')
        self.assertIn(marker, _msg(r))

    def _assert_allowed(self, r) -> None:
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(_decision(r), 'deny')

    def _assert_stop_blocked(self, marker: str) -> None:
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(_out(r).get('decision'), 'block', f'expected Stop block, got {r.stdout!r}')
        self.assertIn(marker, _out(r).get('reason', ''))

    def _assert_stop_not_blocked_for(self, marker: str) -> None:
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(marker, _out(r).get('reason', ''))


# ── Spec gate ─────────────────────────────────────────────────────────────────

class TestSpecGate(_HookTest):

    def test_write_allowed_prewrite_but_stop_blocks_when_token_absent(self):
        # The spec token is same-turn text, invisible at PreToolUse — the write is
        # graced through and the gate fires at Stop, where the text is flushed.
        self._write(_governed() + _al('let me implement this'))
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self._assert_allowed(r)
        self._assert_stop_blocked('spec-gate')

    def test_edit_allowed_prewrite_but_stop_blocks_when_token_absent(self):
        self._write(_governed() + _al('analyzing'))
        r = self._pre('Edit', {'file_path': 'src/user.ts', 'old_string': 'x', 'new_string': 'y'})
        self._assert_allowed(r)
        self._assert_stop_blocked('spec-gate')

    def test_token_only_in_the_message_that_ended_the_turn_confirms(self):
        self._write(_governed() + _al('let me implement this'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'}))
        self._pre('Bash', {'command': 'npm test'})
        r = _call(self.session_id, self.transcript, '', {}, event='Stop', extra={
            'stop_hook_active': False, 'last_assistant_message': '[SPEC CONFIRMED] — added the User class.'})
        self.assertNotEqual(_out(r).get('decision') if r.stdout.strip() else None, 'block', r.stdout)

    def test_a_final_message_without_the_token_still_blocks(self):
        self._write(_governed() + _al('let me implement this'))
        self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        r = _call(self.session_id, self.transcript, '', {}, event='Stop', extra={
            'stop_hook_active': False, 'last_assistant_message': 'Added the User class.'})
        self.assertIn('spec-gate', _out(r).get('reason', ''))

    def test_write_allowed_when_token_in_current_turn(self):
        self._write(_READY)
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self._assert_allowed(r)

    def test_multi_write_turn_stays_allowed(self):
        """tool_use entries after the token must not re-block the second Write."""
        self._write(
            _READY
            + _tool_use('Write', {'file_path': 'src/a.ts'})
            + json.dumps({'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]}}) + '\n'
        )
        r = self._pre('Write', {'file_path': 'src/b.ts', 'content': 'y'})
        self._assert_allowed(r)

    def test_token_from_previous_task_blocks_at_stop(self):
        # Token confirmed task A, but a newer user message opened task B with no
        # fresh confirmation → Stop blocks.
        self._write(
            _governed()
            + _al('[SPEC CONFIRMED] — confirmed task A')
            + _ul('ok, now also do task B')
            + _al('implementing task B')
        )
        self._assert_allowed(self._pre('Write', {'file_path': 'b.ts', 'content': 'y'}))
        self._assert_stop_blocked('spec-gate')

    def test_user_typed_token_blocks_at_stop(self):
        # A user-typed token does not satisfy the gate — only assistant text does.
        self._write(
            _governed()
            + _al('let me implement this')
            + _ul('[SPEC CONFIRMED] I typed this')
        )
        self._assert_allowed(self._pre('Write', {'file_path': 'x.ts', 'content': 'x'}))
        self._assert_stop_blocked('spec-gate')

    def test_bash_sed_inplace_on_ts_blocks_at_stop_without_token(self):
        """The spec gate must not be bypassable via shell file writes."""
        self._write(_governed() + _al('no token'))
        self._assert_allowed(self._pre('Bash', {'command': "sed -i 's/a/b/' src/user.ts"}))
        self._assert_stop_blocked('spec-gate')

    def test_bash_redirect_to_ts_blocks_at_stop_without_token(self):
        self._write(_governed() + _al('no token'))
        self._assert_allowed(self._pre('Bash', {'command': 'echo "x" > src/user.ts'}))
        self._assert_stop_blocked('spec-gate')

    def test_bash_sed_inplace_allowed_with_token(self):
        self._write(_READY)
        r = self._pre('Bash', {'command': "sed -i 's/a/b/' src/user.ts"})
        self._assert_allowed(r)

    def test_read_not_gated(self):
        self._write(_al('no spec token here'))
        r = self._pre('Read', {'file_path': 'src/user.ts'})
        self._assert_allowed(r)

    def test_plain_bash_not_gated(self):
        self._write(_al('no spec token here'))
        r = self._pre('Bash', {'command': 'ls -la'})
        self._assert_allowed(r)

    def test_skill_tool_not_gated(self):
        self._write(_al('no spec token here'))
        r = self._pre('Skill', {'skill': 'engineering-governance', 'args': ''})
        self._assert_allowed(r)

    def test_token_present_does_not_block_at_stop(self):
        # The former false-positive case: the model confirmed the spec in the same
        # turn as the write. At Stop the token is flushed → no spec block (only the
        # separate validation gate may fire).
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._assert_stop_not_blocked_for('spec-gate')

    def test_spec_stop_block_does_not_loop(self):
        # After one spec block, the continuation Stop does not block for spec
        # again; the validation still pending gets its one block, and the next
        # continuation passes.
        self._write(_governed() + _al('no token'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._assert_stop_blocked('spec-gate')
        r = self._stop(active=True)
        self.assertEqual(_out(r).get('decision'), 'block')
        self.assertIn('playbook:validate', _out(r).get('reason', ''))
        r = self._stop(active=True)
        self.assertNotEqual(_out(r).get('decision'), 'block')

    def test_spec_confirmed_after_graced_write_passes_at_stop(self):
        # Graced write with no visible token, then the token flushes into the
        # transcript before Stop (the real deny→retry recovery). Stop must not
        # block for spec.
        self._write(_governed() + _al('let me implement'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._write(
            _governed()
            + _ul('go')
            + _al('[SPEC CONFIRMED] implemented the class')
        )
        self._assert_stop_not_blocked_for('spec-gate')

    def test_token_in_the_final_message_passes_when_mid_turn_text_was_lost(self):
        # Measured: text written between tool calls is not always recorded in the
        # transcript. The turn below holds only tool calls until the final message;
        # the token there is what the Stop hook sees, and it must be enough.
        self._write(_governed() + _ul('go ahead')
                    + _tool_use('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._write(_governed() + _ul('go ahead')
                    + _tool_use('Write', {'file_path': 'src/user.ts', 'content': 'x'})
                    + _al('[SPEC CONFIRMED] wrote the user class'))
        self._assert_stop_not_blocked_for('spec-gate')

    def test_spec_block_tells_the_model_to_use_the_final_message(self):
        self._write(_governed() + _ul('go ahead') + _al('no token'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        r = self._stop()
        self.assertIn('final message', _out(r).get('reason', ''))


# ── Skill-load gate ───────────────────────────────────────────────────────────

class TestSkillGate(_HookTest):

    def test_code_write_denied_without_governance_skill(self):
        self._write(_ul('go') + _al('[SPEC CONFIRMED] implementing'))
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self._assert_denied(r, 'skill-gate')

    def test_code_write_allowed_with_governance_skill(self):
        self._write(_READY)
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self._assert_allowed(r)

    def test_markdown_write_not_gated_at_all(self):
        """Gates apply to source code only — docs and config writes pass freely."""
        self._write(_al('no token, no governance skill'))
        r = self._pre('Write', {'file_path': 'README.md', 'content': '# Title'})
        self._assert_allowed(r)

    def test_python_write_is_gated(self):
        """Python is source like any other language.

        This test asserted the opposite while CODE_EXT_RE listed seven
        ecosystems: every language outside that list — Python included, which is
        what the playbook's own hooks are written in — silently had no skill
        gate, no spec gate and no validation gate."""
        self._write(_al('no token, no governance skill'))
        r = self._pre('Write', {'file_path': 'script.py', 'content': 'x = 1'})
        self._assert_denied(r, 'skill-gate')

    def test_shell_script_write_is_gated(self):
        self._write(_al('no token, no governance skill'))
        r = self._pre('Write', {'file_path': 'scripts/install.sh', 'content': 'echo hi'})
        self._assert_denied(r, 'skill-gate')

    # ── What the written file calls for (hooks/data/skill_requirements.json) ──

    def _pre_in_project(self, tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
        """A call whose project is the test's own directory, so the manifests and
        topic files the test writes there are the ones the gate reads."""
        return _call(self.session_id, self.transcript, tool_name, tool_input, extra={'cwd': self.dir})

    def test_governance_alone_no_longer_opens_a_source_write(self):
        self._write(_skill('engineering-governance') + _ul('go') + _al('[SPEC CONFIRMED] implementing'))
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self._assert_denied(r, 'skill-gate')
        for name in ('engineering-security (every source write)', 'engineering-performance',
                     'engineering-typescript (typescript file src/user.ts)'):
            self.assertIn(name, _msg(r))
        self.assertNotIn('engineering-governance has not', _msg(r))

    def test_a_dockerfile_needs_the_docker_skill_but_not_governance(self):
        self._write(_ul('add a Dockerfile') + _al('writing it'))
        write = ('Write', {'file_path': os.path.join(self.dir, 'Dockerfile'), 'content': 'FROM node:22'})
        r = self._pre_in_project(*write)
        self._assert_denied(r, 'engineering-docker (Dockerfile matches Dockerfile)')
        self.assertNotIn('engineering-governance', _msg(r))
        with open(self.transcript, 'a') as f:   # a transcript only grows
            f.write(_skill('engineering-playbook:engineering-docker') + _al('loaded'))
        self._assert_allowed(self._pre_in_project(*write))

    def test_the_project_framework_is_required(self):
        with open(os.path.join(self.dir, 'package.json'), 'w') as f:
            json.dump({'dependencies': {'@nestjs/core': '^11.0.0'}}, f)
        self._write(_READY)
        write = ('Write', {'file_path': os.path.join(self.dir, 'src', 'app.service.ts'),
                           'content': 'export class AppService {}'})
        self._assert_denied(self._pre_in_project(*write), 'engineering-nestjs (@nestjs/core in package.json)')
        with open(self.transcript, 'a') as f:   # a transcript only grows
            f.write(_skill('engineering-nestjs'))
        self._assert_allowed(self._pre_in_project(*write))

    def _docker_write(self) -> tuple[str, dict]:
        return ('Write', {'file_path': os.path.join(self.dir, 'Dockerfile'), 'content': 'FROM node:24'})

    def test_a_skill_call_the_harness_rejected_loads_nothing(self):
        """Regression: an "Unknown skill" call was counted as loaded, so a write
        passed the gate with rules that were never read."""
        name = 'engineering-playbook:engineering-docker'
        self._write(_ul('add a Dockerfile') + _skill_call('toolu_1', name)
                    + _skill_result('toolu_1', name, failed=True))
        self._assert_denied(self._pre_in_project(*self._docker_write()), 'engineering-docker')

    def test_a_skill_call_counts_once_its_result_succeeds(self):
        name = 'engineering-playbook:engineering-docker'
        self._write(_ul('add a Dockerfile') + _skill_call('toolu_1', name))
        self._assert_denied(self._pre_in_project(*self._docker_write()), 'engineering-docker')
        with open(self.transcript, 'a') as f:   # a transcript only grows
            f.write(_skill_result('toolu_1', name, failed=False))
        self._assert_allowed(self._pre_in_project(*self._docker_write()))

    def test_a_result_recorded_before_its_call_still_pairs(self):
        """The assistant record carrying the call can be flushed after its result."""
        name = 'engineering-playbook:engineering-docker'
        self._write(_ul('add a Dockerfile') + _skill_result('toolu_1', name, failed=False)
                    + _skill_call('toolu_1', name))
        self._assert_allowed(self._pre_in_project(*self._docker_write()))

    def test_a_failed_result_recorded_before_its_call_still_loads_nothing(self):
        name = 'engineering-playbook:engineering-docker'
        self._write(_ul('add a Dockerfile') + _skill_result('toolu_1', name, failed=True)
                    + _skill_call('toolu_1', name))
        self._assert_denied(self._pre_in_project(*self._docker_write()), 'engineering-docker')

    def test_a_rejected_governance_call_does_not_open_the_gate(self):
        name = 'engineering-playbook:engineering-governance'
        self._write(_skill_call('toolu_1', name) + _skill_result('toolu_1', name, failed=True)
                    + _ul('go') + _al('[SPEC CONFIRMED] implementing'))
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self.assertIn('engineering-governance has not been loaded', _msg(r))

    def test_a_topic_import_is_required_in_any_language(self):
        self._write(_READY)
        write = ('Write', {'file_path': os.path.join(self.dir, 'consumer.py'),
                           'content': 'import pika\n'})
        self._assert_denied(self._pre_in_project(*write), 'engineering-rabbitmq (consumer.py imports pika)')

    def test_a_skill_loaded_this_turn_counts_before_its_record_is_on_disk(self):
        """The assistant record carrying the Skill call is flushed after the next
        tool's hook reads the transcript; the Skill's PostToolUse is the first
        place the load is visible."""
        self._write(_ul('add a Dockerfile') + _al('loading'))
        _call(self.session_id, self.transcript, 'Skill', {'skill': 'engineering-docker'},
              event='PostToolUse', extra={'tool_response': {'success': True}})
        self._assert_allowed(self._pre_in_project(
            'Write', {'file_path': os.path.join(self.dir, 'compose.yaml'), 'content': 'services: {}'}))

    def test_suspended_session_skips_gates(self):
        self._write(_skill('playbook-off') + _al('rules suspended, prototyping'))
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self._assert_allowed(r)

    def test_resumed_session_reenforces_gates(self):
        self._write(
            _skill('playbook-off')
            + _skill('playbook-on')
            + _al('no token, no governance')
        )
        r = self._pre('Write', {'file_path': 'src/user.ts', 'content': 'class User {}'})
        self._assert_denied(r, 'skill-gate')


# ── Suspension: /playbook-off = full pass-through ──────────────────

class TestSuspensionFullBypass(_HookTest):
    """/playbook-off must stand the whole hook down — context lock,
    destructive/commit blocks, and the Stop validation gate — until
    /playbook-on. Regression for: 'turned playbook off but the
    context lock kept blocking every tool call in the same session'."""

    def test_suspended_bypasses_context_hard_block(self):
        self._write(_skill('playbook-off') + _usage(180_000))  # 90%
        r = self._pre('Read', {'file_path': 'x.ts'})
        self._assert_allowed(r)

    def test_suspend_releases_an_already_locked_session(self):
        # Session locks first (no suspension), then the user turns the playbook
        # off — the very next call must be allowed despite the sticky lock.
        # Transcripts are append-only, so the off skill lands AFTER the locking
        # usage entry (this is the real live-session sequence).
        locked = _usage(180_000)
        self._write(locked)
        self._assert_denied(self._pre('Read', {'file_path': 'x.ts'}), 'playbook:context')
        self._write(locked + _skill('playbook-off') + _usage(180_000))
        self._assert_allowed(self._pre('Read', {'file_path': 'x.ts'}))

    def test_suspended_bypasses_agent_spawn_lock(self):
        # The exact live symptom: Agent spawns blocked by the context lock.
        self._write(_skill('playbook-off') + _usage(180_000))
        r = self._pre('Agent', {'description': 'search', 'prompt': 'find x'})
        self._assert_allowed(r)

    def test_suspended_bypasses_destructive_command(self):
        # User chose "off = off total": safety blocks also lift while suspended.
        self._write(_skill('playbook-off') + _al('prototyping'))
        r = self._pre('Bash', {'command': 'rm -rf /tmp/scratch'})
        self._assert_allowed(r)

    def test_suspended_stop_does_not_require_validation(self):
        self._write(_skill('playbook-off') + _al('rules suspended'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        r = self._stop()
        self.assertNotEqual(_out(r).get('decision'), 'block')

    def test_resume_reenforces_context_lock(self):
        self._write(
            _skill('playbook-off')
            + _skill('playbook-on')
            + _usage(180_000)
        )
        r = self._pre('Read', {'file_path': 'x.ts'})
        self._assert_denied(r, 'playbook:context')

    def test_suspended_write_arms_no_gate_state(self):
        """Off means off on BOTH tool events. PreToolUse has always stood down
        while suspended; the deferred gates arm at PostToolUse now, so that
        event has to stand down too or the flags are written behind a hook the
        user switched off."""
        self._write(_skill('playbook-off'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        state = self._state()
        self.assertFalse(state.get('pending_validation'))
        self.assertFalse(state.get('spec_pending'))


# ── Stop hook: pending validation ─────────────────────────────────────────────

class TestStopValidation(_HookTest):

    def test_stop_blocked_after_code_write_without_validation(self):
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(_out(r).get('decision'), 'block')
        self.assertIn('validate', _out(r).get('reason', ''))

    def test_write_that_never_ran_does_not_arm_the_gates(self):
        """PreToolUse alone is an announcement, not a write.

        A call denied by a layer outside this plugin -- the permission prompt,
        the harness's own safety classifier, a tool error -- reaches PreToolUse
        and never reaches PostToolUse. Arming on the first event made the session
        demand a validation run for zero bytes on disk, and that block was
        observed on a turn in which nothing had been written."""
        self._write(_READY)
        self._assert_allowed(self._pre_only('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertNotIn('validate', _out(r).get('reason', ''))
        self.assertNotIn('spec-gate', _out(r).get('reason', ''))

    def test_heredoc_mentioning_a_validation_command_does_not_clear_the_gate(self):
        """Writing a doc that quotes `npm test` validates nothing."""
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._assert_allowed(self._pre('Bash', {
            'command': "cat > docs/ci.md <<'EOF'\nRun npm test before pushing.\nEOF"}))
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(_out(r).get('decision'), 'block')
        self.assertIn('validate', _out(r).get('reason', ''))

    def test_block_reason_warns_that_the_response_may_be_dropped(self):
        """A blocked Stop turns the response just written into an intermediate
        message, which a client rendering only the final one drops silently."""
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        reason = _out(self._stop()).get('reason', '')
        self.assertIn('final message', reason)

    def test_stop_allowed_after_validation_command(self):
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._assert_allowed(self._pre('Bash', {'command': 'npm test'}))
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(_out(r).get('decision'), 'block')

    def test_stop_not_blocked_twice_in_a_row(self):
        """stop_hook_active means we already blocked this turn — the gate that
        did is acknowledged, never looped on."""
        self._write(_READY)
        self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'})
        r = self._stop()
        self.assertEqual(_out(r).get('decision'), 'block')
        self.assertIn('playbook:validate', _out(r).get('reason', ''))
        r = self._stop(active=True)
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(_out(r).get('decision'), 'block')

    def test_stop_allowed_when_no_code_written(self):
        self._write(_al('just chatting'))
        self._assert_allowed(self._pre('Read', {'file_path': 'src/user.ts'}))
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(_out(r).get('decision'), 'block')

    def test_markdown_write_does_not_require_validation(self):
        self._write(_ul('go') + _al('[SPEC CONFIRMED] docs'))
        self._assert_allowed(self._pre('Write', {'file_path': 'notes.md', 'content': '# x'}))
        r = self._stop()
        self.assertNotEqual(_out(r).get('decision'), 'block')


# ── Shell edit notice ─────────────────────────────────────────────────────────

class TestShellEditNotice(_HookTest):

    SED = {'command': "sed -i 's/a/b/' src/user.ts"}

    def setUp(self):
        super().setUp()
        self._write(_READY)

    def _notice(self, r) -> str:
        return _out(r).get('hookSpecificOutput', {}).get('additionalContext', '') if r.stdout.strip() else ''

    def test_first_shell_edit_of_a_turn_is_noted_to_the_model_only(self):
        r = self._pre('Bash', self.SED)
        self._assert_allowed(r)
        self.assertIn('[playbook:edit]', self._notice(r))
        self.assertNotIn('systemMessage', _out(r))

    def test_noted_once_per_turn(self):
        self._pre('Bash', self.SED)
        self.assertEqual(self._notice(self._pre('Bash', self.SED)), '')
        self._pre('Bash', {'command': 'npm test'})
        self._stop()
        self.assertIn('[playbook:edit]', self._notice(self._pre('Bash', self.SED)))

    def test_a_note_displaced_by_the_context_advisory_is_not_spent(self):
        self._write(_READY + _usage(150_000))
        displaced = self._pre('Bash', self.SED)
        self.assertIn('[playbook:context]', displaced.stdout)
        self.assertNotIn('[playbook:edit]', displaced.stdout)
        with open(_state_path(self.session_id)) as f:
            self.assertFalse(json.load(f).get('shell_edit_noted'))

    def test_each_shell_form_that_writes_code_is_noted(self):
        for command in ("sed -i 's/a/b/' src/user.ts",
                        "cat > src/user.ts <<'EOF'\nexport class User {}\nEOF",
                        "printf 'x = 1\n' >> scripts/run.py",
                        "python3 - <<'EOF'\nopen('src/a.py', 'w').write('x = 1')\nEOF"):
            with self.subTest(command=command[:20]):
                self._stop()
                self.assertIn('[playbook:edit]', self._notice(self._pre('Bash', {'command': command})))

    def test_the_edit_tool_a_read_and_a_doc_write_are_not_noted(self):
        for tool, tool_input in (('Edit', {'file_path': 'src/user.ts', 'old_string': 'a', 'new_string': 'b'}),
                                 ('Bash', {'command': 'cat src/user.ts | head -20'}),
                                 ('Bash', {'command': "sed -i 's/a/b/' README.md"}),
                                 ('Bash', {'command': 'npm test'})):
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertEqual(self._notice(self._pre(tool, tool_input)), '')


# ── Subagent isolation ────────────────────────────────────────────────────────

class TestDeviationJournal(_HookTest):
    """A [PLAYBOOK-DEVIATION] block in the turn's text is recorded in the
    deviation journal and shown to the user, once, on every way a turn ends."""

    _BLOCK = ('[PLAYBOOK-DEVIATION]\n'
              'skill: engineering-concurrency\n'
              'rule: Idempotency: store key -> response in Redis\n'
              'action: deviated\n'
              'instead: key and fingerprint in the same DB transaction\n'
              'why: Redis outside the transaction loses the record when the DB write fails\n'
              '[/PLAYBOOK-DEVIATION]')

    def setUp(self):
        super().setUp()
        self.journal = os.path.join(self.dir, 'deviations.jsonl')
        saved = os.environ.get('PLAYBOOK_DEVIATION_JOURNAL')
        os.environ['PLAYBOOK_DEVIATION_JOURNAL'] = self.journal
        self.addCleanup(lambda: os.environ.pop('PLAYBOOK_DEVIATION_JOURNAL', None) if saved is None
                        else os.environ.__setitem__('PLAYBOOK_DEVIATION_JOURNAL', saved))
        self.agent_id = f'a{uuid.uuid4().hex[:16]}'
        self.addCleanup(lambda: os.path.exists(p := _state_path(f'{self.session_id}-{self.agent_id}'))
                        and os.remove(p))

    def _records(self) -> list[dict]:
        if not os.path.exists(self.journal):
            return []
        with open(self.journal) as f:
            return [json.loads(line) for line in f]

    def _final_stop(self, message: str, active: bool = False) -> subprocess.CompletedProcess:
        return _call(self.session_id, self.transcript, '', {}, event='Stop',
                     extra={'stop_hook_active': active, 'last_assistant_message': message,
                            'cwd': self.dir})

    def test_a_block_in_the_final_message_is_recorded_and_shown(self):
        self._write(_skill('engineering-concurrency') + _ul('add idempotency'))
        r = self._final_stop('Done, with one deviation.\n\n' + self._BLOCK)
        self.assertNotEqual(_out(r).get('decision'), 'block', r.stdout)
        notice = _out(r).get('systemMessage', '')
        self.assertIn('[playbook:deviation]', notice)
        self.assertIn('engineering-concurrency', notice)
        self.assertIn('deviated: Idempotency', notice)
        self.assertIn(self.journal, notice)
        [record] = self._records()
        self.assertEqual(record['skill'], 'engineering-concurrency')
        self.assertEqual(record['action'], 'deviated')
        self.assertEqual(record['instead'], 'key and fingerprint in the same DB transaction')
        self.assertTrue(record['skill_loaded'])
        self.assertRegex(record['skill_version'], r'^\d+\.\d+\.\d+$')
        self.assertEqual((record['session_id'], record['agent_id'], record['cwd']),
                         (self.session_id, '', self.dir))
        self.assertEqual(record['incomplete'], [])

    def test_a_turn_without_a_block_records_nothing(self):
        self._write(_ul('hi'))
        r = self._final_stop('Hello.')
        self.assertNotIn('systemMessage', _out(r))
        self.assertEqual(self._records(), [])

    def test_a_skill_not_loaded_or_unknown_is_flagged(self):
        self._write(_ul('go'))
        block = self._BLOCK.replace('engineering-concurrency', 'engineering-cobol')
        notice = _out(self._final_stop(block)).get('systemMessage', '')
        self.assertIn('engineering-cobol (not a playbook skill)', notice)
        [record] = self._records()
        self.assertEqual((record['skill_version'], record['skill_loaded']), ('', False))

    def test_a_blocked_turn_still_shows_it_and_the_continuation_records_it_once(self):
        self._write(_governed() + _ul('go') + _al('no token'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/user.ts', 'content': 'x'}))
        self._pre('Bash', {'command': 'npm test'})
        blocked = self._final_stop('Wrote it.\n\n' + self._BLOCK)
        self.assertEqual(_out(blocked).get('decision'), 'block', blocked.stdout)
        self.assertIn('spec-gate', _out(blocked).get('reason', ''))
        self.assertIn('[playbook:deviation]', _out(blocked).get('systemMessage', ''))
        again = self._final_stop('[SPEC CONFIRMED] wrote it.\n\n' + self._BLOCK, active=True)
        self.assertNotIn('systemMessage', _out(again))
        self.assertEqual(len(self._records()), 1)

    def test_a_subagent_block_is_recorded_from_its_own_transcript(self):
        agent_transcript = os.path.join(self.dir, 'agent.jsonl')
        with open(agent_transcript, 'w') as f:
            f.write(_ul('implement the consumer') + _al(self._BLOCK))
        self._write(_ul('dispatch it'))
        r = _call(self.session_id, self.transcript, '', {}, event='SubagentStop', extra={
            'agent_id': self.agent_id, 'agent_type': 'general-purpose',
            'agent_transcript_path': agent_transcript, 'stop_hook_active': False})
        self.assertIn(f'subagent {self.agent_id}', _out(r).get('systemMessage', ''))
        [record] = self._records()
        self.assertEqual(record['agent_id'], self.agent_id)

    def test_a_suspended_playbook_records_nothing(self):
        self._write(_skill('playbook-off') + _ul('prototype'))
        r = self._final_stop(self._BLOCK)
        self.assertNotIn('systemMessage', _out(r))
        self.assertEqual(self._records(), [])


class TestDeviationReports(unittest.TestCase):
    """With `reports` on, the Stop hook queues a reportable deviation and the
    detached drain sends it through `gh`; without gh the user gets a link; with
    `reports` off nothing leaves and the user is told once that reports exist.
    HOME is isolated: the preference, the store and the journal are this test's."""

    _QUOTE = 'The same key with another fingerprint is refused with `422`, with no effect'
    _PRIVATE = ('/srv/acme-bank/ledger', 'AcmeLedgerService', 'transfer-v2 flow')
    _DRAIN_TIMEOUT_S = 20.0

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='pb_reports_')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.state_dir = os.path.join(self.home, '.claude', 'hooks')
        os.makedirs(self.state_dir)
        self.store = os.path.join(self.state_dir, '.deviation-reports.json')
        self.gh_log = os.path.join(self.home, 'gh.log')
        self.bin = os.path.join(self.home, 'bin')
        os.makedirs(self.bin)
        os.symlink(shutil.which('git') or '/usr/bin/git', os.path.join(self.bin, 'git'))
        self.transcript = os.path.join(self.home, 'session.jsonl')
        self.session_id = f'pbtest-{uuid.uuid4().hex[:12]}'

    def _prefs(self, **values: object) -> None:
        with open(os.path.join(self.state_dir, '.playbook-prefs.json'), 'w') as f:
            json.dump(values, f)

    def _block(self, rule: str) -> str:
        return ('[PLAYBOOK-DEVIATION]\n'
                f'skill: engineering-concurrency\nrule: Idempotency — {rule}\naction: deviated\n'
                f'instead: a cache key inside {self._PRIVATE[1]}\n'
                f'why: the {self._PRIVATE[2]} at {self._PRIVATE[0]} needs it\n'
                '[/PLAYBOOK-DEVIATION]')

    def _env(self, with_gh: bool) -> dict[str, str]:
        if with_gh:
            _fake_gh(self.bin, self.gh_log)
        env = {k: v for k, v in os.environ.items() if not k.startswith('PLAYBOOK_REPORT')}
        env.update({'HOME': self.home, 'PATH': self.bin, 'PLAYBOOK_CONTEXT_LIMIT': '200000',
                    'PLAYBOOK_REPORT_REPO': 'o/r', 'CLAUDE_CODE_SESSION_ATTENDED': '1',
                    'PLAYBOOK_DEVIATION_JOURNAL': os.path.join(self.home, 'deviations.jsonl')})
        return env

    def _stop(self, message: str, with_gh: bool = True) -> dict:
        with open(self.transcript, 'a') as f:
            f.write(_ul('add idempotency'))
            f.write(json.dumps({'type': 'assistant', 'message': {
                'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': message}]}}) + '\n')
        payload = {'session_id': self.session_id, 'transcript_path': self.transcript,
                   'hook_event_name': 'Stop', 'stop_hook_active': False,
                   'last_assistant_message': message, 'cwd': self._PRIVATE[0]}
        r = subprocess.run([_PYTHON, _HOOK], input=json.dumps(payload), capture_output=True,
                           text=True, env=self._env(with_gh))
        self.assertEqual(r.returncode, 0, r.stderr)
        return _out(r)

    def _gh_calls(self, until: str = '') -> list[dict]:
        deadline = time.monotonic() + (self._DRAIN_TIMEOUT_S if until else 0)
        while True:
            calls = []
            if os.path.exists(self.gh_log):
                with open(self.gh_log) as f:
                    calls = [json.loads(line) for line in f]
            if not until or any(c['argv'][:2] == until.split() for c in calls) or time.monotonic() > deadline:
                return calls
            time.sleep(0.2)

    def test_on_with_gh_the_drain_opens_an_issue_with_no_project_data(self):
        self._prefs(reports=True)
        notice = self._stop('Done.\n\n' + self._block(self._QUOTE)).get('systemMessage', '')
        self.assertIn('[playbook:deviation]', notice)
        calls = self._gh_calls(until='issue create')
        create = next((c for c in calls if c['argv'][:2] == ['issue', 'create']), None)
        self.assertIsNotNone(create, f'the detached drain never created the issue: {calls}')
        self.assertEqual(create['argv'][create['argv'].index('--repo') + 1], 'o/r')
        sent = json.dumps(calls)
        self.assertIn('The same key with another fingerprint is refused with 422', create['body'])
        self.assertIn('claude-opus-5-5', create['body'])
        for value in (*self._PRIVATE, 'cache key', self.session_id, self.home):
            with self.subTest(value=value):
                self.assertNotIn(value, sent)

    def test_on_without_gh_the_notice_carries_a_prefilled_link(self):
        self._prefs(reports=True)
        notice = self._stop(self._block(self._QUOTE), with_gh=False).get('systemMessage', '')
        self.assertIn('https://github.com/o/r/issues/new?', notice)
        self.assertIn('deviation-report', urllib.parse.unquote_plus(notice))
        for value in (*self._PRIVATE, 'cache key'):
            with self.subTest(value=value):
                self.assertNotIn(value, urllib.parse.unquote_plus(notice))
        self.assertEqual(self._gh_calls(), [])

    def test_a_paraphrased_rule_is_recorded_but_never_sent(self):
        self._prefs(reports=True)
        notice = self._stop(self._block('keys must not be reused with a different body')).get('systemMessage', '')
        self.assertIn('[playbook:deviation]', notice)
        self.assertNotIn('issues/new', notice)
        time.sleep(1)
        self.assertEqual(self._gh_calls(), [])
        self.assertFalse(os.path.exists(self.store) and json.load(open(self.store)).get('pending'))

    def test_off_by_default_nothing_leaves_and_the_user_is_told_once(self):
        first = self._stop(self._block(self._QUOTE)).get('systemMessage', '')
        self.assertIn('/playbook-config can turn them on', first)
        second = self._stop(self._block(self._QUOTE + '.')).get('systemMessage', '')
        self.assertNotIn('/playbook-config can turn them on', second)
        time.sleep(1)
        self.assertEqual(self._gh_calls(), [])

    def test_session_start_shows_the_links_gh_could_not_send_once(self):
        self._prefs(reports=True)
        import deviation_report
        report = deviation_report.build_report(
            {'skill': 'engineering-concurrency', 'rule': self._QUOTE, 'action': 'deviated', 'why': 'x'},
            '4.0.0', 'claude-opus-5-5', 'claude', '4.1.0')
        with deviation_report._locked(self.store) as store:
            store['needs_link'] = [report]
        start = os.path.join(_ROOT, 'hooks', 'session_start.py')
        payload = json.dumps({'session_id': self.session_id, 'source': 'startup', 'cwd': self.home})
        runs = [subprocess.run([_PYTHON, start], input=payload, capture_output=True, text=True,
                               env=self._env(with_gh=True)) for _ in range(2)]
        self.assertIn('https://github.com/o/r/issues/new?', _out(runs[0]).get('systemMessage', ''))
        self.assertNotIn('issues/new', _out(runs[1]).get('systemMessage', ''))


class TestSubagentIsolation(_HookTest):
    """A subagent's hook events (Agent / Workflow) arrive with the parent's
    session_id AND transcript_path — only agent_id tells them apart (captured
    from Claude Code 2.1.266). Regression for: the parent's Stop blocked with
    spec-gate on turns where only background subagents wrote code."""

    def setUp(self):
        super().setUp()
        self.agent_id = f'a{uuid.uuid4().hex[:16]}'
        self.agent_transcript = os.path.join(self.dir, 'agent.jsonl')

    def tearDown(self):
        super().tearDown()
        p = _state_path(f'{self.session_id}-{self.agent_id}')
        if os.path.exists(p):
            os.remove(p)

    def _write_agent(self, content: str) -> None:
        with open(self.agent_transcript, 'w') as f:
            f.write(content)

    def _agent_pre(self, tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
        extra = {'agent_id': self.agent_id, 'agent_type': 'general-purpose'}
        result = _call(self.session_id, self.transcript, tool_name, tool_input, extra=extra)
        if _decision(result) != 'deny':
            _call(self.session_id, self.transcript, tool_name, tool_input,
                  event='PostToolUse',
                  extra={**extra, 'tool_response': {'success': True}})
        return result

    def _agent_stop(self, active: bool = False) -> dict:
        return _out(_call(self.session_id, self.transcript, '', {}, event='SubagentStop', extra={
            'agent_id': self.agent_id,
            'agent_type': 'general-purpose',
            'agent_transcript_path': self.agent_transcript,
            'stop_hook_active': active,
        }))

    def _assert_parent_stop_passes(self) -> None:
        r = self._stop()
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(_out(r).get('decision'), 'block', f'parent Stop blocked: {r.stdout!r}')

    # ── A subagent loads governance for itself ────────────────────────────────

    def _agent_skill(self, name: str, response: dict) -> None:
        _call(self.session_id, self.transcript, 'Skill', {'skill': name}, event='PostToolUse',
              extra={'agent_id': self.agent_id, 'agent_type': 'general-purpose', 'tool_response': response})

    def test_subagent_that_loaded_governance_may_write(self):
        """The parent never loaded it, and the subagent's Skill call is not in
        the transcript its events carry. Regression: three subagents could not
        save a script after loading the skill and retrying."""
        self._write(_ul('measure the sessions') + _al('delegating'))
        self._write_agent(_ul('write the script'))
        write = ('Write', {'file_path': 'scripts/measure.py', 'content': 'x = 1'})
        self._assert_denied(self._agent_pre(*write), 'skill-gate')
        self._agent_skill('engineering-playbook:engineering-governance', {'success': True})
        # Governance alone no longer opens it: the companions are required too,
        # and a subagent's loads are only visible through its PostToolUse.
        denied = self._agent_pre(*write)
        self._assert_denied(denied, 'engineering-security')
        for name in ('engineering-security', 'engineering-concurrency',
                     'engineering-error-handling', 'engineering-performance',
                     'engineering-python'):
            self._agent_skill(f'engineering-playbook:{name}', {'success': True})
        self._assert_allowed(self._agent_pre(*write))

    def test_subagent_load_does_not_open_the_gate_for_the_parent(self):
        self._write(_ul('measure the sessions') + _al('delegating'))
        self._agent_skill('engineering-governance', {'success': True})
        self._assert_denied(self._pre('Write', {'file_path': 'src/a.py', 'content': 'x = 1'}), 'skill-gate')

    def test_another_skill_or_a_failed_load_does_not_open_the_gate(self):
        self._write(_ul('measure the sessions') + _al('delegating'))
        self._agent_skill('engineering-testing', {'success': True})
        self._agent_skill('engineering-governance', {'is_error': True, 'error': 'unknown skill'})
        self._assert_denied(
            self._agent_pre('Write', {'file_path': 'scripts/measure.py', 'content': 'x = 1'}), 'skill-gate')

    # ── Parent is not charged for subagent work ───────────────────────────────

    def test_subagent_write_does_not_arm_parent_spec_gate(self):
        self._write(_governed() + _ul('how much disk is left?') + _al('running du'))
        self._write_agent(_ul('implement the parser') + _al('writing it'))
        self._assert_allowed(self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'}))
        self._assert_allowed(self._agent_pre('Bash', {'command': 'cargo test'}))
        self._assert_parent_stop_passes()

    def test_subagent_write_does_not_arm_parent_validation_gate(self):
        self._write(_READY)
        self._write_agent(_ul('implement the parser') + _al('[SPEC CONFIRMED] writing it'))
        self._assert_allowed(self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'}))
        self._assert_parent_stop_passes()

    # ── Parent gates are not loosened ─────────────────────────────────────────

    def test_parent_write_without_token_still_blocks(self):
        # Even with a confirmed, validated subagent running alongside.
        self._write(_governed() + _ul('add the handler') + _al('writing it'))
        self._write_agent(_ul('implement the parser') + _al('[SPEC CONFIRMED] writing it'))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'}))
        self._assert_allowed(self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'}))
        self._assert_allowed(self._agent_pre('Bash', {'command': 'cargo test'}))
        self._assert_stop_blocked('spec-gate')

    def test_subagent_validation_started_after_the_parents_write_clears_it(self):
        """Ultra review of 4.0.0: the runner agent is shipped to run test
        suites and type checks cheaply, yet the parent's Stop still blocked
        with "no validation command ran" after delegating to it."""
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'}))
        self._assert_allowed(self._agent_pre('Bash', {'command': 'cargo test'}))
        self._assert_parent_stop_passes()

    def test_subagent_validation_started_before_the_parents_write_does_not_clear_it(self):
        # A run already going when the parent wrote may not have seen the write.
        self._write(_READY)
        extra = {'agent_id': self.agent_id, 'agent_type': 'runner', 'tool_use_id': 'toolu_run1'}
        bash = {'command': 'cargo test'}
        self._assert_allowed(_call(self.session_id, self.transcript, 'Bash', bash, extra=extra))
        self._assert_allowed(self._pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'}))
        _call(self.session_id, self.transcript, 'Bash', bash, event='PostToolUse',
              extra={**extra, 'tool_response': {'success': True}})
        self._assert_stop_blocked('validate')

    def test_a_parent_write_after_the_subagents_validation_rearms_it(self):
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'}))
        self._assert_allowed(self._agent_pre('Bash', {'command': 'cargo test'}))
        time.sleep(0.01)  # the second write lands strictly after the run started
        self._assert_allowed(self._pre('Write', {'file_path': 'src/handler.rs', 'content': 'y'}))
        self._assert_stop_blocked('validate')

    def test_a_subagent_command_that_is_not_a_validation_does_not_clear_it(self):
        self._write(_READY)
        self._assert_allowed(self._pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'}))
        self._assert_allowed(self._agent_pre('Bash', {'command': 'ls src'}))
        self._assert_stop_blocked('validate')

    # ── Subagent gates run at SubagentStop, on the subagent's transcript ──────

    def test_subagent_spec_gate_reads_its_own_transcript(self):
        # Token only in the parent's transcript: the subagent never confirmed.
        self._write(_READY)
        self._write_agent(_ul('implement the parser') + _al('writing it'))
        self._assert_allowed(self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'}))
        out = self._agent_stop()
        self.assertEqual(out.get('decision'), 'block', out)
        self.assertIn('spec-gate', out.get('reason', ''))
        out = self._agent_stop(active=True)
        self.assertIn('validate', out.get('reason', ''))
        self.assertNotEqual(self._agent_stop(active=True).get('decision'), 'block')

    def test_subagent_token_in_its_own_transcript_passes_spec_gate(self):
        # Token only in the subagent's transcript: confirmed, parent never was.
        self._write(_governed() + _ul('run the plan') + _al('dispatching'))
        self._write_agent(_ul('implement the parser') + _al('[SPEC CONFIRMED] writing it'))
        self._assert_allowed(self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'}))
        self.assertNotIn('spec-gate', self._agent_stop().get('reason', ''))

    def test_subagent_validation_gate_at_subagent_stop(self):
        self._write(_READY)
        self._write_agent(_ul('implement the parser') + _al('[SPEC CONFIRMED] writing it'))
        self._assert_allowed(self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'}))
        out = self._agent_stop()
        self.assertEqual(out.get('decision'), 'block', out)
        self.assertIn('validate', out.get('reason', ''))
        self._assert_allowed(self._agent_pre('Bash', {'command': 'cargo test'}))
        self.assertNotEqual(self._agent_stop().get('decision'), 'block')


# ── Codex adapter: subagent isolation ─────────────────────────────────────────

_CODEX_GUARD = os.path.join(_ROOT, 'hooks', 'codex_context_guard.py')
_CODEX_STOP = os.path.join(_ROOT, 'hooks', 'codex_stop.py')
_NO_TOKEN = _ul('go') + _al('writing it')
_TOKEN = _ul('go') + _al('[SPEC CONFIRMED] writing it')


class TestCodexSubagentIsolation(unittest.TestCase):
    """Same leak in the Codex adapter. Captured from Codex 0.153.0: a
    subagent's PreToolUse carries the parent's session_id with its OWN
    transcript_path plus agent_id; SubagentStop carries agent_id and
    agent_transcript_path. HOME is isolated so ~/.codex/hooks stays clean."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='pb_codex_')
        self.session_id = f'pbtest-{uuid.uuid4().hex[:12]}'
        self.agent_id = f'pbagent-{uuid.uuid4().hex[:12]}'
        self.parent_tr = os.path.join(self.home, 'parent.jsonl')
        self.agent_tr = os.path.join(self.home, 'agent.jsonl')

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _run(self, script: str, payload: dict) -> dict:
        r = subprocess.run([_PYTHON, script], input=json.dumps(payload), capture_output=True,
                           text=True, env={**os.environ, 'HOME': self.home})
        self.assertEqual(r.returncode, 0, r.stderr)
        return _out(r)

    def _transcripts(self, parent: str, agent: str) -> None:
        for path, text in ((self.parent_tr, parent), (self.agent_tr, agent)):
            with open(path, 'w') as f:
                f.write(text)

    def _parent_pre(self, tool_name: str, tool_input: dict) -> dict:
        return self._run(_CODEX_GUARD, {
            'session_id': self.session_id, 'transcript_path': self.parent_tr,
            'hook_event_name': 'PreToolUse', 'tool_name': tool_name, 'tool_input': tool_input,
        })

    def _agent_pre(self, tool_name: str, tool_input: dict) -> dict:
        return self._run(_CODEX_GUARD, {
            'session_id': self.session_id, 'transcript_path': self.agent_tr,
            'agent_id': self.agent_id, 'agent_type': 'default',
            'hook_event_name': 'PreToolUse', 'tool_name': tool_name, 'tool_input': tool_input,
        })

    def _parent_stop(self, active: bool = False) -> dict:
        return self._run(_CODEX_STOP, {
            'session_id': self.session_id, 'transcript_path': self.parent_tr,
            'hook_event_name': 'Stop', 'stop_hook_active': active,
        })

    def test_a_spec_block_does_not_swallow_the_validation_gate(self):
        """Release review of 4.0.0: the continuation Stop cleared every flag,
        so a spec block let a pending validation go without ever running."""
        self._transcripts(parent=_NO_TOKEN, agent=_NO_TOKEN)
        self._parent_pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'})
        self.assertIn('spec gate', self._parent_stop().get('reason', ''))
        self.assertIn('validation gate', self._parent_stop(active=True).get('reason', ''))
        self.assertNotEqual(self._parent_stop(active=True).get('decision'), 'block')

    def _agent_stop(self) -> dict:
        return self._run(_CODEX_STOP, {
            'session_id': self.session_id, 'transcript_path': self.parent_tr,
            'agent_transcript_path': self.agent_tr, 'agent_id': self.agent_id, 'agent_type': 'default',
            'hook_event_name': 'SubagentStop', 'stop_hook_active': False,
        })

    def test_subagent_write_does_not_arm_parent_spec_gate(self):
        self._transcripts(parent=_NO_TOKEN, agent=_NO_TOKEN)
        self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'})
        self._agent_pre('Bash', {'command': 'cargo test'})
        self.assertNotEqual(self._parent_stop().get('decision'), 'block')

    def test_subagent_write_does_not_arm_parent_validation_gate(self):
        self._transcripts(parent=_TOKEN, agent=_TOKEN)
        self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'})
        self.assertNotEqual(self._parent_stop().get('decision'), 'block')

    def test_parent_write_without_token_still_blocks(self):
        # Pre-fix the leak loosened the parent too: the confirmed subagent's
        # write overwrote spec_pending with False and its cargo test cleared
        # the parent's pending_validation.
        self._transcripts(parent=_NO_TOKEN, agent=_TOKEN)
        self._parent_pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'})
        self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'})
        self._agent_pre('Bash', {'command': 'cargo test'})
        out = self._parent_stop()
        self.assertEqual(out.get('decision'), 'block', out)
        self.assertIn('spec gate', out.get('reason', ''))

    def test_subagent_validation_does_not_clear_parent_validation_gate(self):
        self._transcripts(parent=_TOKEN, agent=_TOKEN)
        self._parent_pre('Write', {'file_path': 'src/handler.rs', 'content': 'x'})
        self._agent_pre('Bash', {'command': 'cargo test'})
        out = self._parent_stop()
        self.assertEqual(out.get('decision'), 'block', out)
        self.assertIn('validation gate', out.get('reason', ''))

    def test_subagent_gates_run_at_subagent_stop_on_its_own_transcript(self):
        self._transcripts(parent=_TOKEN, agent=_NO_TOKEN)
        self._agent_pre('Write', {'file_path': 'src/parser.rs', 'content': 'x'})
        out = self._agent_stop()
        self.assertEqual(out.get('decision'), 'block', out)
        self.assertIn('spec gate', out.get('reason', ''))
        self._transcripts(parent=_NO_TOKEN, agent=_TOKEN)
        self.assertIn('validation gate', self._agent_stop().get('reason', ''))
        self._agent_pre('Bash', {'command': 'cargo test'})
        self.assertNotEqual(self._agent_stop().get('decision'), 'block')


# ── Destructive commands ──────────────────────────────────────────────────────

class TestDestructiveCommands(_HookTest):

    def setUp(self):
        super().setUp()
        self._write(_READY)

    def test_rm_rf_denied(self):
        r = self._pre('Bash', {'command': 'rm -rf /tmp/test-dir'})
        self._assert_denied(r, 'safety')

    def test_git_push_force_denied(self):
        r = self._pre('Bash', {'command': 'git push --force origin main'})
        self._assert_denied(r, 'safety')

    def test_git_push_force_refspec_denied(self):
        r = self._pre('Bash', {'command': 'git push origin +main'})
        self._assert_denied(r, 'safety')

    def test_git_reset_hard_denied(self):
        r = self._pre('Bash', {'command': 'git reset --hard HEAD~1'})
        self._assert_denied(r, 'safety')

    def test_git_clean_fd_denied(self):
        r = self._pre('Bash', {'command': 'git clean -fd'})
        self._assert_denied(r, 'safety')

    def test_find_delete_denied(self):
        r = self._pre('Bash', {'command': "find . -name '*.pyc' -delete"})
        self._assert_denied(r, 'safety')

    def test_drop_table_denied(self):
        r = self._pre('Bash', {'command': 'psql -c "DROP TABLE users"'})
        self._assert_denied(r, 'safety')

    def test_delete_without_where_denied(self):
        r = self._pre('Bash', {'command': 'DELETE FROM orders'})
        self._assert_denied(r, 'safety')

    def test_ls_passes(self):
        self._assert_allowed(self._pre('Bash', {'command': 'ls -la'}))

    def test_npm_test_passes(self):
        self._assert_allowed(self._pre('Bash', {'command': 'npm test'}))

    def test_git_status_passes(self):
        self._assert_allowed(self._pre('Bash', {'command': 'git status'}))

    def test_git_push_without_force_is_not_a_safety_block(self):
        """Only a force push is destructive.

        A plain push can still be denied by the review gate — a different
        gate with a different reason. This asserts the destructive check
        specifically stays out of it."""
        result = self._pre('Bash', {'command': 'git push origin main'})
        self.assertNotIn('playbook:safety', _msg(result))

    def test_delete_with_where_passes(self):
        self._assert_allowed(self._pre('Bash', {'command': 'DELETE FROM users WHERE id = $1'}))


# ── Commit attribution ────────────────────────────────────────────────────────

class TestCommitAttribution(_HookTest):

    def setUp(self):
        super().setUp()
        self._write(_READY)

    def test_commit_with_co_authored_claude_denied(self):
        cmd = 'git commit -m "feat: add feature\n\nCo-Authored-By: Claude Sonnet"'
        r = self._pre('Bash', {'command': cmd})
        self._assert_denied(r, 'commit')

    def test_normal_commit_passes(self):
        r = self._pre('Bash', {'command': 'git commit -m "feat: add user registration"'})
        self._assert_allowed(r)

    def test_commit_co_authored_case_insensitive_denied(self):
        cmd = 'git commit -m "fix\n\nco-authored-by: CLAUDE code"'
        r = self._pre('Bash', {'command': cmd})
        self._assert_denied(r, 'commit')

    def test_pull_request_body_with_attribution_denied(self):
        """The commit is not the only place a trailer reaches the team: on a
        single-commit PR the forge pre-fills the body from the commit message."""
        cmd = ('gh pr create --title "fix: x" '
               '--body "fix: x\n\nCo-Authored-By: Claude Opus 5 (1M context)"')
        r = self._pre('Bash', {'command': cmd})
        self._assert_denied(r, 'commit')

    def test_generated_with_claude_code_in_pr_body_denied(self):
        cmd = ('gh pr create --body "does x\n\n'
               '🤖 Generated with [Claude Code](https://claude.com/claude-code)"')
        r = self._pre('Bash', {'command': cmd})
        self._assert_denied(r, 'commit')

    def test_attribution_inside_an_interpreter_payload_next_to_a_commit_is_denied(self):
        """This used to pass on the theory that the payload is foreign source,
        not the message. It is exactly how a trailer shipped: the script in the
        same command wrote the message the commit then read. With a real commit
        in the command, the whole command is the message's provenance."""
        cmd = ('python3 -c \'TRAILER = "Co-Authored-By: Claude"\' '
               '&& git commit -m "fix: x"')
        r = self._pre('Bash', {'command': cmd})
        self._assert_denied(r, 'commit')

    def test_interpreter_heredoc_writing_the_commit_message_file_is_denied(self):
        """The incident, reduced: a python heredoc appends the trailer to a
        file, and `git commit -F` reads that file in the same command."""
        cmd = ("python3 -I - msg.md <<'EOF'\n"
               "import sys\n"
               "s = 'fix: x'\n"
               "s += '\\n\\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\\n'\n"
               "open(sys.argv[1], 'w').write(s)\n"
               "EOF\n"
               "git commit -q -F msg.md")
        r = self._pre_only('Bash', {'command': cmd})
        self._assert_denied(r, 'commit')

    def test_a_script_quoting_the_line_without_a_commit_passes(self):
        cmd = 'python3 -c \'TRAILER = "Co-Authored-By: Claude"\''
        self._assert_allowed(self._pre_only('Bash', {'command': cmd}))

    def test_a_commit_command_mentioned_only_inside_a_script_passes(self):
        """A script that merely names `git commit` is not a commit."""
        cmd = ("python3 - <<'EOF'\n"
               "print('git commit -m x')\n"
               "print('Co-Authored-By: Claude')\n"
               "EOF")
        self._assert_allowed(self._pre_only('Bash', {'command': cmd}))

    def test_noreply_address_under_another_name_is_denied(self):
        cmd = 'git commit -m "fix\n\nCo-Authored-By: Assistant <noreply@anthropic.com>"'
        self._assert_denied(self._pre_only('Bash', {'command': cmd}), 'commit')

    def test_generated_by_claude_in_pr_body_is_denied(self):
        cmd = 'gh pr create --title x --body "does x\n\nGenerated by Claude"'
        self._assert_denied(self._pre_only('Bash', {'command': cmd}), 'commit')

    def test_git_global_config_option_does_not_hide_the_commit(self):
        cmd = 'git -c core.editor=true commit -m "fix\n\nCo-Authored-By: Claude"'
        self._assert_denied(self._pre_only('Bash', {'command': cmd}), 'commit')


# ── Context limit ─────────────────────────────────────────────────────────────

class TestContextLimit(_HookTest):

    def test_50_pct_passes_silently(self):
        self._write(_usage(100_000))
        r = self._pre('Read', {'file_path': 'test.ts'})
        self._assert_allowed(r)
        self.assertEqual(r.stdout.strip(), '', 'No output expected below warn threshold')

    def test_75_pct_warns_does_not_deny(self):
        self._write(_usage(150_000))
        r = self._pre('Read', {'file_path': 'test.ts'})
        self._assert_allowed(r)

    # A warning is advisory. It used to be `or`-ed straight into the gate result,
    # which made every gate below it -- destructive, attribution, push review,
    # skill -- go silent from the moment a session crossed the warn threshold,
    # and the warning re-fires on every call, so the silence lasted the rest of
    # the session. That is how AI attribution reached a published pull request.
    def test_warning_does_not_silence_the_destructive_gate(self):
        self._write(_usage(150_000))  # 75%: warn, not lock
        r = self._pre_only('Bash', {'command': 'rm -rf build/'})
        self._assert_denied(r, 'playbook:safety')

    def test_warning_does_not_silence_the_attribution_gate(self):
        self._write(_usage(150_000))
        r = self._pre_only('Bash', {
            'command': 'git commit -m "fix\n\nCo-Authored-By: Claude Opus 5"'})
        self._assert_denied(r, 'playbook:commit')

    def test_warning_does_not_silence_the_skill_gate(self):
        self._write(_usage(150_000) + _ul('add the handler') + _al('[SPEC CONFIRMED] writing'))
        r = self._pre_only('Write', {'file_path': 'src/user.ts', 'content': 'x'})
        self._assert_denied(r, 'skill-gate')

    def test_the_warning_still_reaches_the_model_when_nothing_is_gated(self):
        self._write(_usage(150_000))
        r = self._pre_only('Read', {'file_path': 'test.ts'})
        self._assert_allowed(r)
        self.assertIn('playbook:context', _msg(r))

    def test_a_lock_still_short_circuits_every_gate(self):
        """At the hard threshold nothing may run, so the lock wins the call."""
        self._write(_usage(180_000))
        r = self._pre_only('Bash', {'command': 'rm -rf build/'})
        self._assert_denied(r, 'HARD STOP')
        self.assertIn('playbook:context', _msg(r))

    def test_87_pct_denies(self):
        self._write(_usage(174_000))
        r = self._pre('Read', {'file_path': 'test.ts'})
        self._assert_denied(r, 'HARD STOP')

    def test_deny_message_asks_for_a_handoff_and_clear(self):
        self._write(_usage(180_000))
        r = self._pre('Read', {'file_path': 'test.ts'})
        self.assertIn(context_guard.HANDOFF_OPEN, _msg(r))
        self.assertIn('/clear', _msg(r))

    def test_warning_asks_to_finish_then_hand_off(self):
        self._write(_usage(150_000))
        r = self._pre_only('Read', {'file_path': 'test.ts'})
        self.assertIn('finish', _msg(r))
        self.assertIn(context_guard.HANDOFF_OPEN, _msg(r))
        self.assertIn('/clear', _msg(r))

    def test_lock_persists_for_next_call(self):
        self._write(_usage(180_000))
        self._pre('Read', {'file_path': 'test.ts'})
        self._write(_usage(180_000))  # still high
        r = self._pre('Read', {'file_path': 'test.ts'})
        self._assert_denied(r, 'playbook:context')

    def test_lock_releases_once_usage_drops(self):
        self._write(_usage(180_000))
        self._pre('Read', {'file_path': 'test.ts'})
        self._write(_usage(80_000))  # 40% — compacted
        r = self._pre('Read', {'file_path': 'test.ts'})
        self._assert_allowed(r)


# ── Session isolation ─────────────────────────────────────────────────────────

class TestSessionIsolation(unittest.TestCase):
    """Two concurrent sessions must not share lock state."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='pb_iso_')
        self.sid_a = f'pbtest-{uuid.uuid4().hex[:12]}'
        self.sid_b = f'pbtest-{uuid.uuid4().hex[:12]}'
        self.tr_a = os.path.join(self.dir, 'a.jsonl')
        self.tr_b = os.path.join(self.dir, 'b.jsonl')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        for sid in (self.sid_a, self.sid_b):
            p = _state_path(sid)
            if os.path.exists(p):
                os.remove(p)

    def test_lock_in_session_a_does_not_block_session_b(self):
        with open(self.tr_a, 'w') as f:
            f.write(_usage(180_000))  # 90% — locks A
        with open(self.tr_b, 'w') as f:
            f.write(_usage(20_000))   # 10% — healthy B
        r_a = _call(self.sid_a, self.tr_a, 'Read', {'file_path': 'x.ts'})
        self.assertEqual(_decision(r_a), 'deny')
        r_b = _call(self.sid_b, self.tr_b, 'Read', {'file_path': 'x.ts'})
        self.assertNotEqual(_decision(r_b), 'deny')


# ── Malformed input ───────────────────────────────────────────────────────────

class TestMalformedInput(unittest.TestCase):
    """context_guard.py exits 0 gracefully for malformed or empty input."""

    def _raw(self, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [_PYTHON, _HOOK],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
        )

    def test_empty_stdin_exits_zero(self):
        self.assertEqual(self._raw('').returncode, 0)

    def test_invalid_json_exits_zero(self):
        self.assertEqual(self._raw('not json at all').returncode, 0)

    def test_non_dict_json_exits_zero(self):
        self.assertEqual(self._raw('[1, 2, 3]').returncode, 0)

    def test_missing_tool_name_exits_zero(self):
        r = self._raw(json.dumps({'session_id': 'x', 'tool_input': {}}))
        self.assertEqual(r.returncode, 0)
        p = _state_path('x')
        if os.path.exists(p):
            os.remove(p)


if __name__ == '__main__':
    unittest.main(verbosity=2)
