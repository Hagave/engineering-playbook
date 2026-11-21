"""
The deviation reports in the texts the model and the user read: the governance
asks for the rule word for word (only a word-for-word quote can be sent), and
the settings menu offers `reports` off by default and says what it sends and
under whose GitHub account, so nobody turns it on without knowing.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as f:
        return f.read()


class TestDeviationReportsRule(unittest.TestCase):

    def test_the_governance_asks_for_the_rule_word_for_word(self) -> None:
        text = _read('skills/engineering-governance/SKILL.md')
        self.assertIn('rule: the rule quoted word for word from the skill', text)
        self.assertIn('a paraphrase stays in the local journal', text)

    def test_the_menu_offers_reports_off_by_default(self) -> None:
        text = _read('skills/playbook-config/SKILL.md')
        self.assertRegex(text, r'\| Deviation reports \| `reports` \| `on`, `off` \(default\) \|')
        self.assertIn('## Deviation reports', text)

    def test_the_menu_says_what_is_sent_and_under_whose_account(self) -> None:
        section = re.search(r'^## Deviation reports\n(.*?)(?=^## )', _read('skills/playbook-config/SKILL.md'),
                            re.DOTALL | re.MULTILINE).group(1)
        self.assertIn("with the user's own GitHub account", section)
        self.assertIn('word for word', section)
        self.assertIn('Never the project folder, code, session', section)
        self.assertIn('never turned on for the user', section)


if __name__ == '__main__':
    unittest.main()
