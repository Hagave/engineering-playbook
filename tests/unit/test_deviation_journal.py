"""
Unit tests for the deviation block reader and journal helpers in playbook_core:
parse_deviations, skill_version, deviation_journal_path and the trim of
append_incident. The Stop hook that records and announces them is driven in
tests/e2e/test_hook.py (TestDeviationJournal).
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core


def _block(**fields: str) -> str:
    body = ''.join(f'{name}: {value}\n' for name, value in fields.items())
    return f'{playbook_core.DEVIATION_OPEN}\n{body}{playbook_core.DEVIATION_CLOSE}'


_COMPLETE = {
    'skill': 'engineering-concurrency',
    'rule': 'Idempotency: store key -> response in Redis',
    'action': 'deviated',
    'instead': 'key and fingerprint in the same DB transaction',
    'why': 'Redis outside the transaction loses the record when the DB write fails',
}


class TestParseDeviations(unittest.TestCase):

    def test_a_complete_block_is_read_field_by_field(self) -> None:
        found = playbook_core.parse_deviations('Done.\n\n' + _block(**_COMPLETE) + '\n\nNext step.')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].fields, _COMPLETE)
        self.assertEqual(found[0].incomplete, ())

    def test_every_block_of_the_turn_is_read_in_order(self) -> None:
        second = {**_COMPLETE, 'skill': 'engineering-nestjs-security', 'rule': 'trust proxy'}
        found = playbook_core.parse_deviations(_block(**_COMPLETE) + '\ntext\n' + _block(**second))
        self.assertEqual([d.fields['skill'] for d in found],
                         ['engineering-concurrency', 'engineering-nestjs-security'])

    def test_a_mention_inside_a_sentence_is_not_a_block(self) -> None:
        text = (f'Write the block between {playbook_core.DEVIATION_OPEN} and '
                f'{playbook_core.DEVIATION_CLOSE} when a rule is wrong.')
        self.assertEqual(playbook_core.parse_deviations(text), [])

    def test_a_block_left_open_is_not_read(self) -> None:
        text = f'{playbook_core.DEVIATION_OPEN}\nskill: engineering-docker\nrule: x\n'
        self.assertEqual(playbook_core.parse_deviations(text), [])

    def test_a_line_that_starts_no_field_continues_the_previous_one(self) -> None:
        text = (f'{playbook_core.DEVIATION_OPEN}\nskill: engineering-docker\nrule: migrate in CMD\n'
                'action: deviated\ninstead: a separate migration step\nwhy: the image ran\n'
                f'migrations on every replica start\n{playbook_core.DEVIATION_CLOSE}')
        found = playbook_core.parse_deviations(text)
        self.assertEqual(found[0].fields['why'], 'the image ran migrations on every replica start')

    def test_the_skill_name_is_read_without_plugin_prefix_or_backticks(self) -> None:
        found = playbook_core.parse_deviations(
            _block(**{**_COMPLETE, 'skill': '`engineering-playbook:engineering-concurrency`'}))
        self.assertEqual(found[0].fields['skill'], 'engineering-concurrency')

    def test_list_markers_and_field_case_are_accepted(self) -> None:
        text = (f'{playbook_core.DEVIATION_OPEN}\n- Skill: engineering-docker\n- Rule: x\n'
                f'- Action: Followed\n- Why: y\n{playbook_core.DEVIATION_CLOSE}')
        found = playbook_core.parse_deviations(text)
        self.assertEqual(found[0].fields['action'], 'followed')
        self.assertEqual(found[0].incomplete, ())

    def test_missing_fields_are_named(self) -> None:
        found = playbook_core.parse_deviations(_block(skill='engineering-docker', action='deviated'))
        self.assertEqual(set(found[0].incomplete), {'rule', 'why', 'instead'})

    def test_instead_is_optional_when_the_rule_was_followed(self) -> None:
        fields = {k: v for k, v in _COMPLETE.items() if k != 'instead'}
        found = playbook_core.parse_deviations(_block(**{**fields, 'action': 'followed'}))
        self.assertEqual(found[0].incomplete, ())

    def test_an_action_outside_the_format_is_incomplete(self) -> None:
        found = playbook_core.parse_deviations(_block(**{**_COMPLETE, 'action': 'ignored'}))
        self.assertEqual(found[0].incomplete, ('action',))
        self.assertEqual(found[0].fields['action'], 'ignored')

    def test_a_credential_in_a_field_is_redacted(self) -> None:
        found = playbook_core.parse_deviations(
            _block(**{**_COMPLETE, 'why': 'the call used password=hunter2hunter2 in the URL'}))
        self.assertNotIn('hunter2hunter2', found[0].fields['why'])

    def test_a_field_is_capped(self) -> None:
        found = playbook_core.parse_deviations(_block(**{**_COMPLETE, 'why': 'x' * 10_000}))
        self.assertEqual(len(found[0].fields['why']), playbook_core.DEVIATION_FIELD_MAX_CHARS)


class TestSkillVersion(unittest.TestCase):

    def test_a_playbook_skill_reports_its_version(self) -> None:
        with open(os.path.join(_ROOT, 'skills', 'engineering-governance', 'SKILL.md'), encoding='utf-8') as f:
            expected = next(line.split(':', 1)[1].strip() for line in f if line.startswith('version:'))
        self.assertEqual(playbook_core.skill_version('engineering-governance'), expected)

    def test_an_unknown_skill_has_no_version(self) -> None:
        self.assertEqual(playbook_core.skill_version('engineering-cobol'), '')

    def test_a_name_with_a_path_is_not_read(self) -> None:
        self.assertEqual(playbook_core.skill_version('../skills/engineering-governance'), '')
        self.assertEqual(playbook_core.skill_version(''), '')


class TestDeviationJournalFile(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix='deviation_')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_the_journal_is_one_file_in_the_state_directory(self) -> None:
        saved = os.environ.pop(playbook_core.DEVIATION_JOURNAL_ENV, None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, playbook_core.DEVIATION_JOURNAL_ENV, saved)
        self.assertEqual(playbook_core.deviation_journal_path(self.dir),
                         os.path.join(self.dir, '.deviations.jsonl'))

    def test_the_environment_moves_the_journal(self) -> None:
        target = os.path.join(self.dir, 'elsewhere.jsonl')
        saved = os.environ.get(playbook_core.DEVIATION_JOURNAL_ENV)
        os.environ[playbook_core.DEVIATION_JOURNAL_ENV] = target
        self.addCleanup(lambda: os.environ.pop(playbook_core.DEVIATION_JOURNAL_ENV, None)
                        if saved is None else os.environ.__setitem__(playbook_core.DEVIATION_JOURNAL_ENV, saved))
        self.assertEqual(playbook_core.deviation_journal_path('/unused'), target)

    def test_the_journal_keeps_its_own_limit(self) -> None:
        path = os.path.join(self.dir, 'journal.jsonl')
        limit = 5
        for i in range(limit * 2 + 1):
            playbook_core.append_incident(path, {'n': i}, max_lines=limit)
        with open(path) as f:
            kept = [json.loads(line)['n'] for line in f]
        self.assertEqual(kept, list(range(limit + 1, limit * 2 + 1)))


class TestTheRuleMatchesTheReader(unittest.TestCase):
    """The format the model is taught is the one the hook reads: a field renamed
    in the skill and not in the reader would record every block as incomplete."""

    def setUp(self) -> None:
        with open(os.path.join(_ROOT, 'skills', 'engineering-governance', 'SKILL.md'), encoding='utf-8') as f:
            self.governance = f.read()

    def test_the_governance_example_is_a_complete_block(self) -> None:
        found = playbook_core.parse_deviations(self.governance)
        self.assertEqual(len(found), 1, 'governance must show exactly one example block')
        self.assertEqual(found[0].incomplete, ())
        self.assertEqual(set(found[0].fields), set(playbook_core.DEVIATION_FIELDS))

    def test_the_governance_names_every_action(self) -> None:
        for action in playbook_core.DEVIATION_ACTIONS:
            with self.subTest(action=action):
                self.assertIn(f'`action: {action}`', self.governance)

    def test_the_session_context_points_to_the_block(self) -> None:
        with open(os.path.join(_ROOT, 'hooks', 'session_start.py'), encoding='utf-8') as f:
            self.assertIn(playbook_core.DEVIATION_OPEN, f.read())


if __name__ == '__main__':
    unittest.main()
