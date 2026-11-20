"""
Structural tests for hooks/data/skill_requirements.json, the table the skill
gate reads. A row naming a skill that does not exist would deny every matching
write for good, a regex that does not compile would silently never fire, and a
language opt-in left out of the table would never be required.
"""
import json
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS = os.path.join(_ROOT, 'skills')
_TABLE = os.path.join(_ROOT, 'hooks', 'data', 'skill_requirements.json')

# A language opt-in says so in its description ("load when Go is the
# implementation language"); a framework opt-in says "is the chosen framework".
# Matching the phrase, not a list of names, catches a language added later.
_OPT_IN_RE = re.compile(
    r'\*\*Opt-in for [^*]+:\*\* load (?:only )?when (?:\S+ is the implementation language|the project is)')


def _table() -> dict:
    with open(_TABLE, encoding='utf-8') as f:
        return json.load(f)


def _every_skill_named(table: dict) -> list[str]:
    names = list(table['companions'])
    names += [row['skill'] for row in table['languages'].values()]
    names += [row['skill'] for row in table['frameworks']]
    names += [row['skill'] for row in table['topics']]
    names += [row['when_framework'] for row in table['topics'] if 'when_framework' in row]
    return names


class TestSkillRequirementsTable(unittest.TestCase):

    def setUp(self) -> None:
        self.table = _table()

    def test_every_named_skill_exists(self) -> None:
        for name in _every_skill_named(self.table):
            with self.subTest(skill=name):
                self.assertTrue(os.path.isfile(os.path.join(_SKILLS, name, 'SKILL.md')),
                                f'{name} is required by the table but has no SKILL.md')

    def test_every_language_opt_in_is_in_the_table(self) -> None:
        listed = {row['skill'] for row in self.table['languages'].values()}
        for name in sorted(os.listdir(_SKILLS)):
            path = os.path.join(_SKILLS, name, 'SKILL.md')
            if not os.path.isfile(path):
                continue
            with open(path, encoding='utf-8') as f:
                head = f.read(1200)
            if _OPT_IN_RE.search(head):
                with self.subTest(skill=name):
                    self.assertIn(name, listed)

    def test_every_pattern_compiles(self) -> None:
        for row in self.table['topics']:
            for pattern in row.get('patterns', ()):
                with self.subTest(skill=row['skill'], pattern=pattern):
                    re.compile(pattern)

    def test_when_framework_names_a_framework_row(self) -> None:
        frameworks = {row['skill'] for row in self.table['frameworks']}
        for row in self.table['topics']:
            if 'when_framework' in row:
                self.assertIn(row['when_framework'], frameworks)

    def test_every_topic_has_a_trigger(self) -> None:
        for row in self.table['topics']:
            with self.subTest(skill=row['skill']):
                self.assertTrue(row.get('paths') or row.get('imports') or row.get('patterns'))

    def test_companions_match_the_governance_list(self) -> None:
        with open(os.path.join(_SKILLS, 'engineering-governance', 'SKILL.md'), encoding='utf-8') as f:
            content = f.read()
        section = re.search(r'## Mandatory companion skills(.*?)(?=\n## |\Z)', content, re.DOTALL)
        self.assertIsNotNone(section)
        listed = re.findall(r'^- `(engineering-[\w-]+)`', section.group(1), re.MULTILINE)
        self.assertEqual(sorted(listed), sorted(self.table['companions']))

    def test_extensions_are_unique_across_languages(self) -> None:
        seen: dict[str, str] = {}
        for language, row in self.table['languages'].items():
            for ext in row['extensions']:
                with self.subTest(ext=ext):
                    self.assertNotIn(ext, seen, f'{ext} is listed for {seen.get(ext)} and {language}')
                    seen[ext] = language


if __name__ == '__main__':
    unittest.main()
