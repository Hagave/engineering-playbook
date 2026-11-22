"""
Unit tests for hooks/deviation_report.py: what a deviation report may carry,
the queue and its daily cap, and the `gh` calls that send it. The Stop and
SessionStart hooks that queue and show reports are driven in
tests/e2e/test_hook.py (TestDeviationReports).

The privacy guarantee is that the report is built only from the skill's own
text and fixed fields, so the negative tests put project data in every field a
model writes and assert none of it reaches the report, the issue or the link.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import deviation_report  # noqa: E402

_SKILL = 'engineering-concurrency'
# Word for word from engineering-concurrency, section "Idempotency".
_QUOTE = 'The same key with another fingerprint is refused with `422`, with no effect'
_PROJECT = ('/home/alice/acme-bank', 'AcmeLedgerService', 'transfer-v2', 'acme-internal')


def _fields(**overrides: str) -> dict[str, str]:
    return {
        'skill': _SKILL,
        'rule': f'Idempotency — {_QUOTE}',
        'action': 'deviated',
        'instead': f'a cache key in {_PROJECT[1]}',
        'why': f'the {_PROJECT[2]} flow at {_PROJECT[0]} needs it',
        **overrides,
    }


def _report(**overrides: str) -> dict[str, str]:
    report = deviation_report.build_report(_fields(**overrides), '4.0.0', 'claude-opus-5-5', 'claude', '4.1.0')
    assert report is not None
    return report


class TestQuoteInSkill(unittest.TestCase):

    def test_a_word_for_word_quote_is_found_with_its_section(self) -> None:
        self.assertEqual(deviation_report.quote_in_skill(_SKILL, f'Idempotency — {_QUOTE}'),
                         ('Idempotency', 'The same key with another fingerprint is refused with 422, with no effect'))

    def test_spacing_case_and_markup_do_not_matter(self) -> None:
        found = deviation_report.quote_in_skill(_SKILL, '**the same key  with ANOTHER fingerprint is refused**')
        self.assertIsNotNone(found)

    def test_a_paraphrase_is_not_a_quote(self) -> None:
        self.assertIsNone(deviation_report.quote_in_skill(
            _SKILL, 'reusing a key with a different body must be rejected with 422'))

    def test_only_the_quoted_piece_leaves_when_the_rule_mixes_in_project_names(self) -> None:
        rule = f'{_QUOTE} — but {_PROJECT[1]} in {_PROJECT[0]} needs a cache'
        section, quote = deviation_report.quote_in_skill(_SKILL, rule)
        self.assertEqual(section, 'Idempotency')
        for value in _PROJECT:
            self.assertNotIn(value, quote)

    def test_a_short_piece_is_not_enough(self) -> None:
        self.assertIsNone(deviation_report.quote_in_skill(_SKILL, 'Idempotency'))

    def test_an_unknown_skill_or_a_path_as_skill_quotes_nothing(self) -> None:
        self.assertIsNone(deviation_report.quote_in_skill('engineering-cobol', _QUOTE))
        self.assertIsNone(deviation_report.quote_in_skill('../engineering-concurrency', _QUOTE))


class TestBuildReport(unittest.TestCase):

    def test_the_report_has_exactly_the_fixed_fields(self) -> None:
        self.assertEqual(set(_report()), {
            'skill', 'skill_version', 'section', 'rule', 'action', 'reason', 'dependency',
            'playbook_version', 'harness', 'model', 'fingerprint'})

    def test_no_project_data_reaches_the_report_the_issue_or_the_link(self) -> None:
        report = _report()
        sent = '\n'.join((json.dumps(report), deviation_report.issue_title(report),
                          deviation_report.issue_body(report),
                          urllib.parse.unquote_plus(deviation_report.new_issue_url(report))))
        for value in _PROJECT:
            with self.subTest(value=value):
                self.assertNotIn(value, sent)
        self.assertNotIn('cache key', sent)
        self.assertNotIn('needs it', sent)

    def test_a_paraphrased_rule_is_not_reported(self) -> None:
        self.assertIsNone(deviation_report.build_report(
            _fields(rule='keys must not be reused across bodies'), '4.0.0', '', 'claude', ''))

    def test_a_skill_the_playbook_does_not_have_is_not_reported(self) -> None:
        self.assertIsNone(deviation_report.build_report(_fields(), '', '', 'claude', ''))

    def test_an_action_outside_the_format_is_not_reported(self) -> None:
        self.assertIsNone(deviation_report.build_report(_fields(action='ignored'), '4.0.0', '', 'claude', ''))

    def test_a_version_deviation_sends_only_the_package_and_its_version(self) -> None:
        for why, dependency in (('version prisma@8.0.0: no prisma generate in /home/alice', 'prisma@8.0.0'),
                                ('version @nestjs/config@12.1.1: Standard Schema', '@nestjs/config@12.1.1')):
            with self.subTest(dependency=dependency):
                report = _report(why=why)
                self.assertEqual((report['reason'], report['dependency']), ('version', dependency))
                self.assertNotIn('/home/alice', json.dumps(report))

    def test_any_other_why_is_reason_other_with_no_dependency(self) -> None:
        self.assertEqual((_report()['reason'], _report()['dependency']), ('other', ''))

    def test_a_model_name_outside_the_expected_shape_is_dropped(self) -> None:
        report = deviation_report.build_report(_fields(), '4.0.0', 'my model at /home/alice', 'claude', '')
        self.assertEqual(report['model'], '')

    def test_the_fingerprint_is_the_skill_version_and_quote(self) -> None:
        self.assertEqual(_report()['fingerprint'], _report(why='another reason', action='followed')['fingerprint'])
        self.assertNotEqual(_report()['fingerprint'],
                            deviation_report.build_report(_fields(), '4.0.1', '', 'claude', '')['fingerprint'])

    def test_the_body_carries_the_fingerprint_the_search_looks_for(self) -> None:
        report = _report()
        self.assertIn(report['fingerprint'], deviation_report.issue_body(report))
        self.assertTrue(deviation_report.issue_title(report).startswith(deviation_report.TITLE_PREFIX))


class _StoreTest(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix='pb_report_')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.store = os.path.join(self.dir, 'reports.json')

    def _store(self) -> dict:
        with open(self.store) as f:
            return json.load(f)

    def _reports(self, count: int) -> list[dict[str, str]]:
        return [{**_report(), 'fingerprint': f'fp{i:02d}'} for i in range(count)]


class TestQueue(_StoreTest):

    def test_a_report_is_queued_once(self) -> None:
        report = _report()
        deviation_report.enqueue(self.store, [report], gh_available=True)
        deviation_report.enqueue(self.store, [report], gh_available=True)
        self.assertEqual(len(self._store()['pending']), 1)

    def test_without_gh_the_report_is_returned_for_its_link_and_never_again(self) -> None:
        report = _report()
        self.assertEqual(deviation_report.enqueue(self.store, [report], gh_available=False), [report])
        self.assertEqual(deviation_report.enqueue(self.store, [report], gh_available=False), [])
        self.assertEqual(self._store()['sent'][report['fingerprint']], 'linked')

    def test_the_queue_is_bounded(self) -> None:
        shown = deviation_report.enqueue(self.store, self._reports(deviation_report.MAX_PENDING + 2), True)
        self.assertEqual(len(self._store()['pending']), deviation_report.MAX_PENDING)
        self.assertEqual(len(shown), 2)

    def test_the_off_hint_is_given_once(self) -> None:
        self.assertTrue(deviation_report.hint_once(self.store))
        self.assertFalse(deviation_report.hint_once(self.store))

    def test_links_are_taken_once(self) -> None:
        with deviation_report._locked(self.store) as store:
            store['needs_link'] = [_report()]
        self.assertEqual(len(deviation_report.take_links(self.store)), 1)
        self.assertEqual(deviation_report.take_links(self.store), [])

    def test_a_corrupt_store_starts_empty(self) -> None:
        with open(self.store, 'w') as f:
            f.write('{not json')
        self.assertEqual(len(deviation_report.enqueue(self.store, [_report()], gh_available=False)), 1)


class _FakeGh:
    """Stands in for `gh`, answering each subcommand as gh documents it: JSON
    on stdout for `issue list --json`, the new issue's URL for `issue create`,
    exit 4 when a command needs authentication (`gh help exit-codes`)."""

    def __init__(self, existing: int | None = None, fail: str = '', create_exit: int = 0) -> None:
        self.existing, self.fail, self.create_exit = existing, fail, create_exit
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []

    def __call__(self, argv: list[str], **_: object) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if '--body-file' in argv:
            with open(argv[argv.index('--body-file') + 1], encoding='utf-8') as f:
                self.bodies.append(f.read())
        if self.fail == 'auth':
            return subprocess.CompletedProcess(argv, 4, '', 'To get started with GitHub CLI, please run: gh auth login')
        if self.fail == 'network':
            return subprocess.CompletedProcess(argv, 1, '', 'error connecting to api.github.com')
        if argv[1:3] == ['issue', 'list']:
            found = [{'number': self.existing}] if self.existing else []
            return subprocess.CompletedProcess(argv, 0, json.dumps(found), '')
        if argv[1:3] == ['issue', 'create']:
            return subprocess.CompletedProcess(argv, self.create_exit,
                                               'https://github.com/o/r/issues/77\n', '')
        return subprocess.CompletedProcess(argv, 0, '{}', '')


class TestDrain(_StoreTest):

    def _drain(self, gh: _FakeGh, now: float = 1_000_000.0) -> dict[str, int]:
        return deviation_report.drain(self.store, 'o/r', run=gh, now=lambda: now)

    def test_a_new_rule_opens_an_issue_with_the_fixed_body(self) -> None:
        report = _report()
        deviation_report.enqueue(self.store, [report], True)
        gh = _FakeGh()
        self.assertEqual(self._drain(gh)['created'], 1)
        create = gh.calls[-1]
        self.assertEqual(create[:5], ['gh', 'issue', 'create', '--repo', 'o/r'])
        self.assertEqual(create[create.index('--title') + 1], deviation_report.issue_title(report))
        self.assertEqual(gh.bodies, [deviation_report.issue_body(report)])
        self.assertEqual(self._store()['sent'][report['fingerprint']], 'created')
        self.assertEqual(self._store()['pending'], [])

    def test_the_search_looks_for_the_fingerprint_in_every_state(self) -> None:
        report = _report()
        deviation_report.enqueue(self.store, [report], True)
        gh = _FakeGh()
        self._drain(gh)
        search = gh.calls[0]
        self.assertEqual(search[1:3], ['issue', 'list'])
        self.assertIn(f'{report["fingerprint"]} in:body', search)
        self.assertEqual(search[search.index('--state') + 1], 'all')

    def test_a_rule_already_reported_gets_a_reaction_not_a_second_issue(self) -> None:
        deviation_report.enqueue(self.store, [_report()], True)
        gh = _FakeGh(existing=42)
        self.assertEqual(self._drain(gh)['reacted'], 1)
        self.assertEqual(gh.calls[-1][:5], ['gh', 'api', '--method', 'POST', 'repos/o/r/issues/42/reactions'])
        self.assertIn('content=+1', gh.calls[-1])
        self.assertFalse(any(c[1:3] == ['issue', 'create'] for c in gh.calls))

    def test_a_create_that_exits_non_zero_after_printing_the_url_counts_as_created(self) -> None:
        deviation_report.enqueue(self.store, [_report()], True)
        self.assertEqual(self._drain(_FakeGh(create_exit=1))['created'], 1)
        self.assertEqual(self._store()['pending'], [])

    def test_no_login_moves_every_due_report_to_the_links(self) -> None:
        deviation_report.enqueue(self.store, self._reports(3), True)
        gh = _FakeGh(fail='auth')
        self._drain(gh)
        self.assertEqual(len(gh.calls), 1, 'one auth failure is enough to stop trying')
        self.assertEqual(len(self._store()['needs_link']), 3)
        self.assertEqual(self._store()['pending'], [])

    def test_a_network_failure_retries_with_backoff_then_gives_a_link(self) -> None:
        deviation_report.enqueue(self.store, [_report()], True)
        now = 1_000_000.0
        for attempt in range(1, deviation_report.MAX_ATTEMPTS):
            self._drain(_FakeGh(fail='network'), now)
            [pending] = self._store()['pending']
            self.assertEqual(pending['attempts'], attempt)
            self.assertEqual(pending['next_try'], now + deviation_report.RETRY_BASE_S * 2 ** (attempt - 1))
            self.assertEqual(self._drain(_FakeGh(), now)['created'], 0, 'not due before its backoff')
            now = pending['next_try']
        self._drain(_FakeGh(fail='network'), now)
        self.assertEqual(self._store()['pending'], [])
        self.assertEqual(len(self._store()['needs_link']), 1)

    def test_at_most_the_daily_cap_of_issues_is_opened(self) -> None:
        deviation_report.enqueue(self.store, self._reports(deviation_report.DAILY_CREATE_CAP + 1), True)
        self.assertEqual(self._drain(_FakeGh())['created'], deviation_report.DAILY_CREATE_CAP)
        self.assertEqual(len(self._store()['pending']), 1)
        self.assertEqual(self._drain(_FakeGh())['created'], 0, 'the cap holds for the rest of the day')
        self.assertEqual(self._drain(_FakeGh(), now=1_000_000.0 + 86_400)['created'], 1)

    def test_a_report_leased_by_one_drain_is_not_taken_by_another(self) -> None:
        deviation_report.enqueue(self.store, [_report()], True)
        self.assertEqual(len(deviation_report._lease(self.store, 1_000_000.0)), 1)
        self.assertEqual(deviation_report._lease(self.store, 1_000_001.0), [])
        self.assertEqual(len(deviation_report._lease(self.store, 1_000_000.0 + deviation_report.LEASE_S + 1)), 1)

    def test_gh_that_cannot_run_is_a_failure_not_a_crash(self) -> None:
        deviation_report.enqueue(self.store, [_report()], True)

        def missing(argv: list[str], **_: object) -> subprocess.CompletedProcess:
            raise FileNotFoundError('gh')

        self.assertEqual(deviation_report.drain(self.store, 'o/r', run=missing)['failed'], 1)
        self.assertEqual(self._store()['pending'][0]['attempts'], 1)


class TestDrainCommand(_StoreTest):

    def test_the_drain_sends_nothing_while_reports_are_off(self) -> None:
        with mock.patch.object(deviation_report, 'drain') as drain:
            self.assertEqual(deviation_report.main(['drain', '--state-dir', self.dir]), 0)
        drain.assert_not_called()

    def test_a_malformed_command_is_refused(self) -> None:
        self.assertEqual(deviation_report.main(['send']), 2)
        self.assertEqual(deviation_report.main(['drain', '--other', 'x']), 2)


if __name__ == '__main__':
    unittest.main()
