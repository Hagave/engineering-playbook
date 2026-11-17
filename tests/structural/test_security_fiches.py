"""
Structural tests for the vulnerability fiches in
skills/engineering-security/references/ and the skills that point at them.

A fiche nobody can find is dead weight, and one missing a section breaks the
contract the testing and review skills rely on (Negative proof becomes tests,
Review signal drives the review), so both are checked here.
"""
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS = os.path.join(_ROOT, 'skills')
_SECURITY = os.path.join(_SKILLS, 'engineering-security', 'SKILL.md')
_REFERENCES = os.path.join(_SKILLS, 'engineering-security', 'references')

SECTIONS = ('## Invariant', '## Sinks', '## Control', '## Negative proof',
            '## Dynamic proof', '## Review signal')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _fiches() -> list[str]:
    return sorted(f for f in os.listdir(_REFERENCES) if f.endswith('.md'))


class TestFicheIndex(unittest.TestCase):

    def test_every_fiche_is_listed_in_the_skill(self):
        listed = set(re.findall(r'^\| `([\w-]+\.md)` \|', _read(_SECURITY), re.MULTILINE))
        self.assertEqual(listed, set(_fiches()))

    def test_fiche_count(self):
        self.assertGreaterEqual(len(_fiches()), 13)


class TestFicheShape(unittest.TestCase):

    def test_sections_present_and_in_order(self):
        for name in _fiches():
            text = _read(os.path.join(_REFERENCES, name))
            with self.subTest(fiche=name):
                positions = [text.find(s) for s in SECTIONS]
                self.assertNotIn(-1, positions, f'missing section in {name}')
                self.assertEqual(positions, sorted(positions), f'sections out of order in {name}')

    def test_classification_and_load_trigger(self):
        for name in _fiches():
            text = _read(os.path.join(_REFERENCES, name))
            with self.subTest(fiche=name):
                self.assertRegex(text, r'\bCWE-\d+|\bLLM\d{2}\b')
                self.assertIn('**Load when**', text)

    def test_negative_proof_is_a_table(self):
        for name in _fiches():
            text = _read(os.path.join(_REFERENCES, name))
            section = text.split('## Negative proof', 1)[1].split('\n## ', 1)[0]
            with self.subTest(fiche=name):
                rows = [line for line in section.splitlines() if line.startswith('|')]
                self.assertGreaterEqual(len(rows), 4, 'header, separator and at least two cases')

    def test_cross_references_resolve(self):
        for name in _fiches():
            text = _read(os.path.join(_REFERENCES, name))
            for ref in set(re.findall(r'`([\w-]+\.md)`', text)):
                with self.subTest(fiche=name, ref=ref):
                    self.assertTrue(os.path.exists(os.path.join(_REFERENCES, ref)))


class TestBehavioralFixtures(unittest.TestCase):
    """Every fiche has a review fixture in tests/behavioral/prompts/ (run with
    run_behavioral.sh, which needs the claude CLI and an API key)."""

    PROMPTS = os.path.join(_ROOT, 'tests', 'behavioral', 'prompts')

    def _fixtures(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for name in sorted(os.listdir(self.PROMPTS)):
            match = re.search(r'^Fiche: ([\w-]+)$', _read(os.path.join(self.PROMPTS, name)),
                              re.MULTILINE)
            if match:
                found[match.group(1)] = name
        return found

    def test_one_fixture_per_fiche(self):
        self.assertEqual(set(self._fixtures()), {f[:-3] for f in _fiches()})

    def test_fixture_hides_the_answer_and_lists_expectations(self):
        for name in self._fixtures().values():
            text = _read(os.path.join(self.PROMPTS, name))
            prompt, _, expected = text.partition('Expected model behavior')
            with self.subTest(fixture=name):
                self.assertTrue(expected, 'no "Expected model behavior" section')
                self.assertNotIn('Fiche:', prompt)  # the runner sends only this part
                self.assertNotRegex(prompt, r'CWE-\d+')
                must = expected.split('Must mention:', 1)
                self.assertEqual(len(must), 2, 'no "Must mention:" section')
                patterns = [line[2:] for line in must[1].splitlines() if line.startswith('- ')]
                self.assertTrue(patterns)
                for pattern in patterns:
                    re.compile(pattern)


class TestSkillsUsingTheFiches(unittest.TestCase):

    def test_testing_skill_turns_negative_proof_into_tests(self):
        text = _read(os.path.join(_SKILLS, 'engineering-testing', 'SKILL.md'))
        self.assertIn('Negative proof', text)
        self.assertIn('absence of the effect', text)

    def test_review_skill_has_closure_states_and_scope(self):
        text = _read(os.path.join(_SKILLS, 'engineering-code-review', 'SKILL.md'))
        for phrase in ('Closing a security candidate', 'Confirmed', 'Ruled out',
                       'Open proof gap', 'Security scope of a diff', 'Review signal'):
            self.assertIn(phrase, text)

    def test_review_output_examples_still_arm_the_learn_gate(self):
        """The Stop hook counts Blocker/Major bullets with context_guard._FINDING_RE;
        the new security lines must stay countable."""
        sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
        import context_guard  # noqa: PLC0415
        text = _read(os.path.join(_SKILLS, 'engineering-code-review', 'SKILL.md'))
        fmt = text.split('## Output format', 1)[1]
        for line in fmt.splitlines():
            if 'CWE-918' in line or 'proof gap:' in line:
                with self.subTest(line=line):
                    self.assertTrue(context_guard._FINDING_RE.search(line + '\n'))


if __name__ == '__main__':
    unittest.main()
