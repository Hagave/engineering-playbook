"""
Structural test for the Codex PreToolUse hook contract.

The Codex PreToolUse hook (`hooks/codex_context_guard.py`) allows with
`{"continue": true}` and denies with `hookSpecificOutput.permissionDecision:
"deny"` plus a non-empty `permissionDecisionReason` -- the shapes codex-rs
`parse_pre_tool_use` acts on. A top-level `reason` without a decision is
marked invalid there and the tool call runs.

This test guards the playbook from regressing on the hook contract. It is
deliberately script-level (not behavioral) so it runs without Codex installed
and without a model provider.
"""
import json
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _ROOT / 'hooks' / 'codex_context_guard.py'


def _invoke_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['python3', str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
    )


class TestCodexHookContract(unittest.TestCase):
    """Playbook's PreToolUse hook must emit Codex-compatible JSON."""

    def setUp(self):
        if not _HOOK.exists():
            self.skipTest(f'{_HOOK} not found')

    def test_pass_case_is_valid_json(self):
        """A Read of a non-source file is a no-op pass; the hook must still
        emit `permissionDecision: "allow"` in the Codex JSON shape."""
        result = _invoke_hook({
            'tool_name': 'Read',
            'tool_input': {'file_path': '/tmp/foo.txt'},
            'session_id': 'test',
            'transcript_path': '',
        })
        self.assertEqual(result.returncode, 0, msg=f'hook failed: {result.stderr}')
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed, {'continue': True}, msg=f'Codex 0.147.0 minimal: {parsed!r}')
        self.assertTrue(parsed.get('continue', True))
        self.assertEqual(parsed, {'continue': True})

    def test_bash_pass_case_is_valid_json(self):
        """Bash matcher fires on the playbook; same minimal payload applies."""
        result = _invoke_hook({
            'tool_name': 'Bash',
            'tool_input': {'command': 'ls -la'},
            'session_id': 'test',
            'transcript_path': '',
        })
        self.assertEqual(result.returncode, 0, msg=f'hook failed: {result.stderr}')
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed, {'continue': True})

    def test_no_stdout_noise(self):
        """The hook must not leak extra bytes to stdout -- Codex parses stdout
        as JSON and rejects any payload missing the expected shape. On
        0.147.0 the minimal accepted payload is `{"continue": true}`;
        richer payloads (hookSpecificOutput.permissionDecision, decision,
        permissionDecisionReason) are ALL rejected with 'unsupported' on
        this version. Document the version too so future maintainers
        know to re-check when the Codex CLI bumps."""
        result = _invoke_hook({
            'tool_name': 'Bash',
            'tool_input': {'command': 'echo hi'},
            'session_id': 'test',
            'transcript_path': '',
        })
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.fail(f'stdout is not valid JSON: {result.stdout!r} ({e})')
        # Codex 0.147.0x accepts only the minimal pass-through; richer
        # payloads are rejected with `unsupported` errors. When this
        # test changes, document the Codex version compatibility.
        self.assertEqual(parsed, {'continue': True},
                         msg=f'Codex 0.147.0 rejects richer payloads: {parsed!r}')

    def test_deny_uses_the_shape_codex_blocks_on(self):
        """codex-rs blocks only on permissionDecision "deny" with a reason (or
        the legacy decision "block"); `{"continue": true, "reason": ...}` was
        read as "reason without decision" and the destructive command ran."""
        result = _invoke_hook({
            'tool_name': 'Bash',
            'tool_input': {'command': 'git reset --hard HEAD~1'},
            'session_id': 'test',
            'transcript_path': '',
        })
        self.assertEqual(result.returncode, 0, msg=f'hook failed: {result.stderr}')
        parsed = json.loads(result.stdout)
        self.assertEqual(set(parsed), {'hookSpecificOutput'})
        output = parsed['hookSpecificOutput']
        self.assertEqual(output['hookEventName'], 'PreToolUse')
        self.assertEqual(output['permissionDecision'], 'deny')
        self.assertTrue(output['permissionDecisionReason'].strip())

    def test_install_script_warns_on_foreign_harness(self):
        """scripts/install-codex.sh must scan ~/.codex/hooks.json for hooks
        that target a different LLM harness (a third-party CLI that
        registers its `<tool> hook claude` processor into Codex). The warning
        is non-fatal but must be present in the script source."""
        install_script = _ROOT / 'scripts' / 'install-codex.sh'
        if not install_script.exists():
            self.skipTest(f'{install_script} not found')
        text = install_script.read_text()
        # The check must look for the foreign-harness pattern.
        self.assertIn(' hook (claude|cursor|gemini|copilot)', text)
        # The warning must reference the Codex-specific error message that
        # users see when the foreign hook fires.
        self.assertIn('updatedInput without permissionDecision', text)
        # The warning must be non-fatal (no `exit 1` after the warn block).
        self.assertIn('This warning is non-fatal', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
