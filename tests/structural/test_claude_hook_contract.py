"""
Structural test for the Claude Code hook contract.

Mirrors test_hook_contract.py (Codex side). Guards two failure modes that are
invisible at runtime — a hook that is registered but does nothing looks exactly
like a hook that decided to allow:

  1. hooks/hooks.json referencing a script that does not exist, so the event
     silently never fires.
  2. session_start.py emitting something Claude Code cannot read as
     additionalContext, so governance is never announced.

Script-level, so it runs without Claude Code installed and without a provider.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_JSON = _ROOT / 'hooks' / 'hooks.json'
_SESSION_START = _ROOT / 'hooks' / 'session_start.py'

sys.path.insert(0, str(_ROOT / 'hooks'))
import playbook_core  # noqa: E402


def _invoke(script: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['python3', str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
    )


class TestHooksJson(unittest.TestCase):
    """Every registered hook must resolve to a script that exists."""

    def setUp(self):
        self.config = json.loads(_HOOKS_JSON.read_text())

    def test_lifecycle_events_registered(self):
        self.assertEqual(
            set(self.config['hooks']),
            {'SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
             'PreCompact', 'Stop', 'SubagentStop'},
        )

    def test_every_command_points_at_an_existing_script(self):
        for event, entries in self.config['hooks'].items():
            for entry in entries:
                for hook in entry['hooks']:
                    with self.subTest(event=event, command=hook['command']):
                        script = hook['command'].split('${CLAUDE_PLUGIN_ROOT}/')[-1]
                        script = script.rstrip('"').strip()
                        self.assertTrue(
                            (_ROOT / script).exists(),
                            msg=f'{script} registered for {event} but missing',
                        )


class TestSessionStartContract(unittest.TestCase):
    """session_start.py must emit readable additionalContext, or stay silent."""

    def setUp(self):
        if not _SESSION_START.exists():
            self.skipTest(f'{_SESSION_START} not found')

    def _context(self, payload: dict) -> str:
        result = _invoke(_SESSION_START, payload)
        self.assertEqual(result.returncode, 0, msg=f'hook failed: {result.stderr}')
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed['hookSpecificOutput']['hookEventName'], 'SessionStart')
        return parsed['hookSpecificOutput']['additionalContext']

    def test_startup_announces_the_entry_point_skills(self):
        context = self._context({'session_id': 'contract-startup', 'source': 'startup'})
        # The whole point of the hook: name what has to be loaded, before the
        # PreToolUse gate has to deny a write to say the same thing.
        self.assertIn('engineering-governance', context)
        self.assertIn('engineering-playbook', context)
        self.assertIn('engineering-scaffold', context)
        self.assertIn(playbook_core.SPEC_TOKEN, context)

    def test_compact_reinjects_with_a_compaction_note(self):
        context = self._context({'session_id': 'contract-compact', 'source': 'compact'})
        self.assertIn('compaction', context)

    def test_malformed_stdin_still_announces(self):
        """Fail-open: a payload the hook cannot parse must not silence it."""
        result = subprocess.run(
            ['python3', str(_SESSION_START)],
            input='not json', capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn('engineering-governance', result.stdout)

    def test_suspended_session_stays_silent(self):
        session_id = 'contract-suspended'
        state_file = playbook_core.state_path(
            os.path.expanduser('~/.claude/hooks'), session_id,
        )
        playbook_core.save_state(state_file, {'suspended': True})
        try:
            result = _invoke(_SESSION_START, {'session_id': session_id, 'source': 'resume'})
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '', msg='/playbook-off must mean off')
        finally:
            os.remove(state_file)


if __name__ == '__main__':
    unittest.main()
