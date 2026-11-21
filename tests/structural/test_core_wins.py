"""
The core wins: no code template in an opt-in skill (framework, language,
driver) may contradict a core rule. Each row of _CORE_FORBIDS names the core
text that forbids a pattern; the test checks the core still says it, then looks
for the pattern in every fenced code block of every opt-in skill and its
references. A template that contradicts the core is copied by the model as the
more specific rule, which is how a forgeable client IP and a relaxed CSP
reached two projects.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS = os.path.join(_ROOT, 'skills')

_CORE_FORBIDS = [
    {
        'name': 'trusting every proxy hop',
        'core': ('engineering-security/SKILL.md', 'set to that hop, not `true`'),
        'pattern': re.compile(r"""trustProxy\s*:\s*true\b|['"]trust proxy['"]\s*,\s*true\b"""),
        'sample': "new FastifyAdapter({ trustProxy: true })\napp.set('trust proxy', true);",
    },
    {
        'name': "'unsafe-inline' or 'unsafe-eval' for scripts",
        'core': ('engineering-security/references/xss.md',
                 'CSP without `unsafe-inline` and `unsafe-eval` for scripts'),
        'pattern': re.compile(r"""(?i)script-?src\b[^;\n\]]*unsafe-(?:inline|eval)"""),
        'sample': "scriptSrc: [`'self'`, `'unsafe-inline'`],\n\"script-src 'self' 'unsafe-eval'\"",
    },
]

_OPT_IN_SECTIONS = ('### Framework opt-in', '### Language opt-in', '### Driver opt-in')
_FENCE_RE = re.compile(r'^```[^\n]*\n(.*?)^```', re.DOTALL | re.MULTILINE)


def _read(rel: str) -> str:
    with open(os.path.join(_SKILLS, rel), encoding='utf-8') as f:
        return f.read()


def _opt_in_skills() -> list[str]:
    """Every skill the routing table of engineering-playbook lists as an opt-in."""
    routing = _read('engineering-playbook/SKILL.md')
    names: set[str] = set()
    for heading in _OPT_IN_SECTIONS:
        start = routing.index(heading)
        end = routing.find('\n#', start + len(heading))
        section = routing[start:end if end != -1 else None]
        names.update(re.findall(r'\|\s*`(engineering-[\w-]+)`\s*\|', section))
    return sorted(names)


def _code_blocks(skill: str) -> list[tuple[str, str]]:
    paths = [os.path.join(_SKILLS, skill, 'SKILL.md'),
             *sorted(glob.glob(os.path.join(_SKILLS, skill, 'references', '*.md')))]
    blocks: list[tuple[str, str]] = []
    for path in paths:
        with open(path, encoding='utf-8') as f:
            text = f.read()
        rel = os.path.relpath(path, _SKILLS)
        blocks.extend((rel, block) for block in _FENCE_RE.findall(text))
    return blocks


class TestCoreWins(unittest.TestCase):

    def test_the_tie_break_is_written_in_the_routing_skill(self) -> None:
        self.assertIn('**The core wins.**', _read('engineering-playbook/SKILL.md'))

    def test_the_opt_in_set_is_read_from_the_routing_table(self) -> None:
        skills = _opt_in_skills()
        for expected in ('engineering-nestjs-security', 'engineering-go', 'engineering-prisma'):
            self.assertIn(expected, skills)
        for skill in skills:
            with self.subTest(skill=skill):
                self.assertTrue(os.path.isfile(os.path.join(_SKILLS, skill, 'SKILL.md')))

    def test_each_forbidden_pattern_is_still_forbidden_by_the_core(self) -> None:
        for row in _CORE_FORBIDS:
            path, text = row['core']
            with self.subTest(rule=row['name']):
                self.assertIn(text, _read(path), f'{path} no longer states the rule this test enforces')

    def test_each_pattern_catches_its_sample(self) -> None:
        for row in _CORE_FORBIDS:
            with self.subTest(rule=row['name']):
                self.assertEqual(len(row['pattern'].findall(row['sample'])), 2)

    def test_no_opt_in_template_contradicts_the_core(self) -> None:
        for skill in _opt_in_skills():
            for rel, block in _code_blocks(skill):
                for row in _CORE_FORBIDS:
                    match = row['pattern'].search(block)
                    with self.subTest(file=rel, rule=row['name']):
                        self.assertIsNone(
                            match, f'{rel} has {match.group(0)!r} in a code block: {row["name"]} '
                            f'contradicts {row["core"][0]}' if match else '')


if __name__ == '__main__':
    unittest.main()
