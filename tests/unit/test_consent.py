"""The auto-mode consent queue: which words approve a queued destructive
command, and how the queue keeps each approval to one exact command, once."""
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402

APPROVES = [
    'pode apagar', 'Pode apagar sim', 'ok, pode remover', 'pode deletar', 'pode tirar',
    'pode excluir', 'pode limpar', 'podem apagar', 'pode sim apagar', 'pode já remover',
    'apaga isso', 'apague tudo', 'remova esses', 'tira isso', 'exclua os dois',
    'sim, apague', 'sim pode remover', 'puedes borrar', 'borra eso',
    'yes, delete it', 'you can remove them', 'delete it', 'remove both', 'you may delete',
]
REFUSES = [
    'não pode apagar', 'nao apague isso', 'não, não pode remover ainda',
    "don't delete it", 'do not remove them', 'no, never delete that',
]
NEITHER = [
    'remova o import que sobrou', 'vamos remover o código morto depois', 'pode rodar os testes',
    'pode apagar?', 'deletar é perigoso', 'the remove button', 'continue', 'sim',
    'apagar a pasta foi um erro',
]


class TestReadConsent(unittest.TestCase):

    def test_approving_words(self):
        for text in APPROVES:
            with self.subTest(text=text):
                reading = playbook_core.read_consent(text)
                self.assertTrue(reading.approves)
                self.assertFalse(reading.refuses)

    def test_refusing_words(self):
        for text in REFUSES:
            with self.subTest(text=text):
                reading = playbook_core.read_consent(text)
                self.assertFalse(reading.approves)
                self.assertTrue(reading.refuses)

    def test_words_that_neither_approve_nor_refuse(self):
        for text in NEITHER:
            with self.subTest(text=text):
                reading = playbook_core.read_consent(text)
                self.assertFalse(reading.approves or reading.refuses)

    def test_numbers_named(self):
        self.assertEqual(playbook_core.read_consent('pode apagar o #2 e o #5').ids, frozenset({2, 5}))


class TestConsentQueue(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def _request(self, command: str) -> int:
        return playbook_core.consent_request(self.dir, 's1', command, 'delete_tree')

    def _answer(self, text: str, turn_ended: bool = True) -> playbook_core.ConsentAnswer:
        if turn_ended:
            playbook_core.consent_mark_turn_end(self.dir, 's1')
        return playbook_core.consent_answer(self.dir, 's1', text)

    def _take(self, command: str, use: bool = True) -> bool:
        return playbook_core.consent_take(self.dir, 's1', command, use=use)

    def test_numbers_are_stable_per_command(self):
        self.assertEqual(self._request('rm -rf a'), 1)
        self.assertEqual(self._request('rm -rf b'), 2)
        self.assertEqual(self._request('rm -rf a'), 1)

    def test_an_approval_runs_the_exact_command_once(self):
        self._request('rm -rf a')
        self.assertFalse(self._take('rm -rf a'))
        self.assertEqual([e['id'] for e in self._answer('pode apagar').approved], [1])
        self.assertFalse(self._take('rm -rf a '))
        self.assertTrue(self._take('rm -rf a', use=False))
        self.assertTrue(self._take('rm -rf a'))
        self.assertFalse(self._take('rm -rf a'))

    def test_an_approval_covers_only_what_was_queued_before_it(self):
        self._request('rm -rf a')
        self._answer('pode apagar')
        self._request('rm -rf b')
        self.assertFalse(self._take('rm -rf b'))

    def test_an_approval_covers_only_what_a_finished_turn_could_have_shown(self):
        self._request('rm -rf a')
        answer = self._answer('pode apagar', turn_ended=False)
        self.assertTrue(answer.ambiguous)
        self.assertFalse(self._take('rm -rf a'))
        playbook_core.consent_mark_turn_end(self.dir, 's1')
        self._request('rm -rf b')
        answer = self._answer('pode apagar', turn_ended=False)
        self.assertEqual([e['id'] for e in answer.approved], [1])
        self.assertEqual([e['id'] for e in answer.still_pending], [2])

    def test_a_named_number_narrows_it(self):
        self._request('rm -rf a')
        self._request('rm -rf b')
        answer = self._answer('pode apagar o #2')
        self.assertEqual([e['id'] for e in answer.approved], [2])
        self.assertEqual([e['id'] for e in answer.still_pending], [1])
        self.assertFalse(self._take('rm -rf a'))

    def test_an_unknown_number_or_a_mixed_message_approves_nothing(self):
        self._request('rm -rf a')
        for text in ('pode apagar o #9', 'pode apagar, mas não apague isso'):
            with self.subTest(text=text):
                answer = self._answer(text)
                self.assertTrue(answer.ambiguous)
                self.assertEqual(answer.approved, [])
                self.assertFalse(self._take('rm -rf a'))

    def test_words_with_nothing_queued_change_nothing(self):
        self.assertEqual(self._answer('pode apagar'), playbook_core.ConsentAnswer([], [], False))

    def test_sessions_do_not_share_a_queue(self):
        self._request('rm -rf a')
        playbook_core.consent_answer(self.dir, 's2', 'pode apagar')
        self.assertFalse(playbook_core.consent_take(self.dir, 's2', 'rm -rf a'))
        self.assertFalse(self._take('rm -rf a'))

    def test_the_queue_is_bounded(self):
        for n in range(playbook_core.CONSENT_PENDING_MAX + 5):
            self._request(f'rm -rf d{n}')
        self._answer('pode apagar')
        self.assertFalse(self._take('rm -rf d0'))
        self.assertTrue(self._take(f'rm -rf d{playbook_core.CONSENT_PENDING_MAX + 4}'))

    def test_a_corrupt_queue_file_approves_nothing(self):
        with open(playbook_core.consent_path(self.dir, 's1'), 'w') as f:
            f.write('{not json')
        self.assertFalse(self._take('rm -rf a'))
        self.assertEqual(self._request('rm -rf a'), 1)

    def test_malformed_entries_do_not_crash(self):
        with open(playbook_core.consent_path(self.dir, 's1'), 'w') as f:
            f.write('{"next_id": "x", "pending": [{"id": "abc", "key": 1, "ts": "now"}, 7],'
                    ' "approved": "nope", "turn_end": "later"}')
        self.assertEqual(self._request('rm -rf a'), 1)
        self.assertTrue(self._answer('pode apagar').approved)


class TestConsentQueueIsTheUsers(unittest.TestCase):

    def test_calls_naming_the_queue_files(self):
        queue = '/home/u/.claude/hooks/.consent-abc.json'
        for tool, tool_input in (('Bash', {'command': f'cat {queue}'}),
                                 ('Bash', {'command': 'ls ~/.claude/hooks/.consent-*'}),
                                 ('Write', {'file_path': queue}),
                                 ('Edit', {'file_path': queue}),
                                 ('NotebookEdit', {'notebook_path': queue})):
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertTrue(playbook_core.touches_consent_queue(tool, tool_input))
        self.assertFalse(playbook_core.touches_consent_queue('Bash', {'command': 'ls ~/.claude'}))
        self.assertFalse(playbook_core.touches_consent_queue('Write', {'file_path': '/a/consent.md'}))


if __name__ == '__main__':
    unittest.main()
