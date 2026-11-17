"""
End-to-end tests for the secret gate wiring in hooks/context_guard.py: the hook
runs as a subprocess with the real stdin contract, against a throwaway git
repository passed as `cwd`.

Fake secrets are assembled at run time; see tests/unit/test_secret_scan.py.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid

from tests.e2e.test_hook import _READY, _call, _decision, _out, _state_path, _ul

GITHUB_PAT = 'ghp' + '_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'
GENERIC_KEY_LINE = 'api' + '_key: "Zx9Kq2Lm8Np4Rt6Vw3Yb"'


class TestSecretGate(unittest.TestCase):

    def setUp(self):
        self.session_id = f'pbtest-{uuid.uuid4().hex[:12]}'
        self.dir = tempfile.mkdtemp(prefix='pb_secret_')
        self.repo = os.path.join(self.dir, 'repo')
        subprocess.run(('git', 'init', '-q', '-b', 'main', self.repo), capture_output=True)
        self.transcript = os.path.join(self.dir, 'session.jsonl')
        with open(self.transcript, 'w') as fh:
            fh.write(_READY)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        state = _state_path(self.session_id)
        if os.path.exists(state):
            os.remove(state)

    def _pre(self, tool_name: str, tool_input: dict) -> subprocess.CompletedProcess:
        return _call(self.session_id, self.transcript, tool_name, tool_input,
                     extra={'cwd': self.repo})

    def test_write_adding_a_token_is_denied(self):
        result = self._pre('Write', {'file_path': os.path.join(self.repo, 'src', 'a.ts'),
                                     'content': f'export const t = "{GITHUB_PAT}";\n'})
        self.assertEqual(_decision(result), 'deny')
        self.assertNotIn(GITHUB_PAT, result.stdout)

    def test_possible_secret_reaches_the_model_without_a_deny(self):
        result = self._pre('Write', {'file_path': os.path.join(self.repo, 'config.yml'),
                                     'content': GENERIC_KEY_LINE + '\n'})
        self.assertEqual(_decision(result), '')
        context = _out(result).get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('[playbook:secrets]', context)

    def test_bash_heredoc_into_a_tracked_file_is_denied(self):
        command = f"cat > notes.md <<'EOF'\ntoken {GITHUB_PAT}\nEOF"
        self.assertEqual(_decision(self._pre('Bash', {'command': command})), 'deny')

    def test_suspended_playbook_does_not_check(self):
        with open(self.transcript, 'a') as fh:
            fh.write(_ul('/playbook-off'))
            fh.write(json.dumps({'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Skill',
                 'input': {'skill': 'playbook-off'}}]}}) + '\n')
        result = self._pre('Write', {'file_path': os.path.join(self.repo, 'a.ts'),
                                     'content': f'"{GITHUB_PAT}"'})
        self.assertNotEqual(_decision(result), 'deny')


if __name__ == '__main__':
    unittest.main()
