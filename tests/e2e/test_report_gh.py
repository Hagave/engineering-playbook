"""
Real run of the deviation report against GitHub through the real `gh`.

Opt-in: set PLAYBOOK_REPORT_IT=1 and PLAYBOOK_REPORT_REPO=<owner>/<repo>, a
repository you own and may fill with a test issue (never the playbook's own).
It needs `gh` logged in with access to that repository. It is skipped
otherwise.

The unit tests feed the drain a stand-in for `gh`; this proves gh itself
behaves as they assume: `issue create --body-file` opens the issue and prints
its URL, `issue list --search "<fingerprint> in:body" --json number` finds it
once GitHub has indexed it, and `api .../reactions` adds the +1 a second user's
report becomes. The issue is closed at the end.
"""
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
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import deviation_report  # noqa: E402

_REPO = os.environ.get('PLAYBOOK_REPORT_REPO', '')
_INDEX_TIMEOUT_S = 90.0
_QUOTE = 'The same key with another fingerprint is refused with `422`, with no effect'


def _gh(*args: str) -> str:
    done = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=60, check=False)
    if done.returncode != 0:
        raise AssertionError(f'gh {" ".join(args)} failed ({done.returncode}): {done.stderr}')
    return done.stdout


@unittest.skipUnless(os.environ.get('PLAYBOOK_REPORT_IT') == '1' and _REPO,
                     'opt-in: PLAYBOOK_REPORT_IT=1 and PLAYBOOK_REPORT_REPO=<owner>/<repo> (needs gh logged in)')
class TestReportThroughRealGh(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix='pb_report_it_')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # A version no real skill has, so each run reports a rule nobody reported.
        self.report = deviation_report.build_report(
            {'skill': 'engineering-concurrency', 'rule': _QUOTE, 'action': 'deviated', 'why': 'it'},
            f'0.0.0-it.{uuid.uuid4().hex[:8]}', 'claude-opus-5-5', 'claude', 'it')
        self.assertIsNotNone(self.report)

    def _drain_as_a_new_user(self) -> dict[str, int]:
        store = os.path.join(self.dir, f'{uuid.uuid4().hex}.json')
        deviation_report.enqueue(store, [self.report], gh_available=True)
        return deviation_report.drain(store, _REPO)

    def _issue(self) -> int | None:
        found = json.loads(_gh('issue', 'list', '--repo', _REPO, '--state', 'all', '--search',
                               f'{self.report["fingerprint"]} in:body', '--json', 'number,body'))
        return found[0]['number'] if found else None

    def test_one_issue_then_a_reaction(self) -> None:
        self.assertEqual(self._drain_as_a_new_user(), {'created': 1, 'reacted': 0, 'failed': 0})

        deadline = time.monotonic() + _INDEX_TIMEOUT_S
        number = self._issue()
        while number is None and time.monotonic() < deadline:
            time.sleep(5)
            number = self._issue()
        self.assertIsNotNone(number, 'the issue never appeared in the search the drain relies on')
        self.addCleanup(_gh, 'issue', 'close', str(number), '--repo', _REPO)

        issue = json.loads(_gh('issue', 'view', str(number), '--repo', _REPO, '--json', 'title,body'))
        self.assertEqual(issue['title'], deviation_report.issue_title(self.report))
        self.assertIn(self.report['fingerprint'], issue['body'])

        self.assertEqual(self._drain_as_a_new_user(), {'created': 0, 'reacted': 1, 'failed': 0})
        reactions = json.loads(_gh('api', f'repos/{_REPO}/issues/{number}/reactions'))
        self.assertIn('+1', [r.get('content') for r in reactions])


if __name__ == '__main__':
    unittest.main()
