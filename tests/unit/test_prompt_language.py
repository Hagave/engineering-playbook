"""
`auto` follows the language the user writes in, not the system locale: a
Portuguese speaker on an English locale got every dialog in English.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HOOK = os.path.join(_ROOT, 'hooks', 'prompt_language.py')
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_i18n

PT = [
    'O pb está bloqueando coisas que não deveria.',
    ('Aplica a allow rule do desligar.sh no global e vamos corrigir esses comandos que o pb '
     'está bloqueando, porque ele não pode travar o fluxo.'),
    'Eu quero que ele salve tudo antes de descartar e depois restaure para o usuário.',
    'Isso foi a segunda vez que acontece. Ele disse que o gate também bloqueou',
]
ES = [
    'Quiero que el hook guarde todo antes de descartar, y que también avise al usuario.',
    '¿Cómo puede el gate pedir permiso cuando no hay nada que perder?',
    'Necesito que los comandos no se bloqueen por falsos positivos, pero con un aviso.',
]
EN = [
    'Why is the playbook blocking commands that it should not block?',
    'Please fix the gate so that it does not ask when the change can be restored.',
    'What is the cost of this and how can we measure it with the real logs?',
]


class TestDetect(unittest.TestCase):

    def test_each_language_is_recognised(self) -> None:
        for expected, samples in (('pt-br', PT), ('es', ES), ('en', EN)):
            for sample in samples:
                with self.subTest(sample=sample):
                    self.assertEqual(playbook_i18n.detect(sample), expected)

    def test_a_message_that_says_nothing_settles_nothing(self) -> None:
        for sample in ('', 'sim', 'ok', 'Sim.', 'git checkout -- a.txt', '/playbook-config',
                       'src/main/java/Foo.java', '1 2 3'):
            with self.subTest(sample=sample):
                self.assertIsNone(playbook_i18n.detect(sample))

    def test_a_mixed_message_without_a_clear_lead_settles_nothing(self) -> None:
        mixed = ('Isso não é o que você quer, mas the gate is not what you can do with this '
                 'that.')
        self.assertIsNone(playbook_i18n.detect(mixed))

    def test_only_the_first_chars_are_read(self) -> None:
        long_prompt = 'x ' * 10_000 + ' '.join(PT)
        self.assertIsNone(playbook_i18n.detect(long_prompt))


class TestRememberAndResolve(unittest.TestCase):

    def setUp(self) -> None:
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)

    def test_a_remembered_language_comes_back(self) -> None:
        playbook_i18n.remember('pt-br', self.state)
        self.assertEqual(playbook_i18n.remembered(self.state), 'pt-br')

    def test_a_language_the_playbook_does_not_offer_is_not_kept(self) -> None:
        playbook_i18n.remember('fr', self.state)
        playbook_i18n.remember('auto', self.state)
        self.assertIsNone(playbook_i18n.remembered(self.state))

    def test_an_unreadable_or_corrupt_file_reads_as_nothing(self) -> None:
        path = os.path.join(self.state, playbook_i18n._REMEMBERED_FILE)
        for body in ('', 'not json', '[]', '{"language": 5}', '{"language": "fr"}'):
            with self.subTest(body=body):
                with open(path, 'w') as handle:
                    handle.write(body)
                self.assertIsNone(playbook_i18n.remembered(self.state))

    def test_a_directory_that_cannot_be_written_loses_the_convenience_only(self) -> None:
        blocked = os.path.join(self.state, 'file')
        with open(blocked, 'w') as handle:
            handle.write('x')
        playbook_i18n.remember('pt-br', os.path.join(blocked, 'inside'))

    def test_an_explicit_preference_wins_over_what_was_remembered(self) -> None:
        playbook_i18n.remember('pt-br', self.state)
        self.assertEqual(playbook_i18n.resolve('es', state_dir=self.state), 'es')

    def test_auto_follows_the_language_the_user_wrote_in_before_the_locale(self) -> None:
        playbook_i18n.remember('pt-br', self.state)
        with unittest.mock.patch.dict(os.environ, {'LC_ALL': 'en_US.UTF-8'}):
            self.assertEqual(playbook_i18n.resolve('auto', state_dir=self.state), 'pt-br')

    def test_auto_falls_back_to_the_locale_when_nothing_was_remembered(self) -> None:
        with unittest.mock.patch.dict(os.environ, {'LC_ALL': 'es_ES.UTF-8'}):
            self.assertEqual(playbook_i18n.resolve('auto', state_dir=self.state), 'es')

    def test_a_caller_that_passes_an_environment_asks_for_the_locale_alone(self) -> None:
        playbook_i18n.remember('pt-br', self.state)
        self.assertEqual(playbook_i18n.resolve('auto', {'LANG': 'en_US.UTF-8'}, self.state), 'en')


class TestHook(unittest.TestCase):

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.file = os.path.join(self.home, '.claude', 'hooks', playbook_i18n._REMEMBERED_FILE)

    def _run(self, payload: object) -> subprocess.CompletedProcess:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, '-I', _HOOK], input=body, capture_output=True,
                              text=True, timeout=15, check=False,
                              env={**os.environ, 'HOME': self.home})

    def _prompt(self, prompt: str, **extra: object) -> subprocess.CompletedProcess:
        return self._run({'hook_event_name': 'UserPromptSubmit', 'prompt': prompt, **extra})

    def test_a_typed_portuguese_prompt_is_remembered(self) -> None:
        done = self._prompt(PT[1])
        self.assertEqual((done.returncode, done.stdout), (0, ''))
        self.assertEqual(playbook_i18n.remembered(os.path.dirname(self.file)), 'pt-br')

    def test_the_language_follows_the_user_when_it_changes(self) -> None:
        self._prompt(PT[1])
        self._prompt(EN[1])
        self.assertEqual(playbook_i18n.remembered(os.path.dirname(self.file)), 'en')

    def test_a_short_prompt_keeps_the_language_as_it_was(self) -> None:
        self._prompt(PT[1])
        self._prompt('sim')
        self.assertEqual(playbook_i18n.remembered(os.path.dirname(self.file)), 'pt-br')

    def test_what_the_user_did_not_write_settles_nothing(self) -> None:
        pasted = '<pasted_content id="1">' + EN[1] + ' ' + EN[2] + '</pasted_content>'
        for payload in (
                {'prompt': PT[1], 'agent_id': 'sub-1'},
                {'prompt': PT[1], 'source': 'hook'},
                {'prompt': '<task-notification>' + PT[1]},
                {'prompt': '<system-reminder>' + PT[1]},
                {'prompt': 5},
                {'prompt': PT[1], 'hook_event_name': 'Stop'}):
            with self.subTest(payload=payload):
                self._run({'hook_event_name': 'UserPromptSubmit', **payload})
                self.assertFalse(os.path.exists(self.file))
        self._prompt(pasted)
        self.assertFalse(os.path.exists(self.file))

    def test_a_pasted_block_does_not_outvote_what_the_user_wrote(self) -> None:
        pasted = '<pasted_content id="1">' + ' '.join(EN) + '</pasted_content>'
        self._prompt(PT[1] + ' ' + pasted)
        self.assertEqual(playbook_i18n.remembered(os.path.dirname(self.file)), 'pt-br')

    def test_malformed_input_never_fails_the_prompt(self) -> None:
        for body in ('', 'not json', '[]', 'null'):
            with self.subTest(body=body):
                self.assertEqual(self._run(body).returncode, 0)


class TestRegistration(unittest.TestCase):

    def test_the_hook_runs_on_every_prompt(self) -> None:
        with open(os.path.join(_ROOT, 'hooks', 'hooks.json')) as handle:
            hooks = json.load(handle)['hooks']['UserPromptSubmit']
        commands = [h['command'] for group in hooks for h in group['hooks']]
        self.assertTrue(any('prompt_language.py' in command for command in commands))


if __name__ == '__main__':
    unittest.main()
