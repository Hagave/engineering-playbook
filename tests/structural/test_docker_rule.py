"""
engineering-docker states its rules for every language and keeps each
ecosystem's templates in references/. The Node template once ran
`prisma migrate deploy` in the production CMD and both evaluated projects
copied it; their production images did not start. These tests hold the rule
that replaced it and keep the core free of one ecosystem's commands.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS = os.path.join(_ROOT, 'skills')
_DOCKER = os.path.join(_SKILLS, 'engineering-docker')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _blocks(text: str, language: str) -> list[str]:
    found: list[str] = []
    current: list[str] | None = None
    tag = ''
    for line in text.splitlines(keepends=True):
        fence = re.match(r'^```(\S*)\s*$', line)
        if fence and current is None:
            current, tag = [], fence.group(1)
        elif fence:
            if tag == language:
                found.append(''.join(current))
            current = None
        elif current is not None:
            current.append(line)
    return found


class TestDockerRule(unittest.TestCase):

    def setUp(self) -> None:
        self.core = _read(os.path.join(_DOCKER, 'SKILL.md'))

    def test_the_core_runs_migrations_as_their_own_step(self) -> None:
        self.assertIn('## Migrations', self.core)
        self.assertIn('never runs in the `CMD` or `ENTRYPOINT` of the production image', self.core)
        [compose] = [b for b in _blocks(self.core, 'yaml') if 'x-database' in b]
        self.assertRegex(compose, r'(?m)^  migrate:\n(?:    .*\n)*?      target: migrate')
        self.assertIn('condition: service_completed_successfully', compose)

    def test_no_skill_migrates_in_a_production_command(self) -> None:
        """A CMD or ENTRYPOINT that migrates is allowed only in a `migrate` stage
        declared in the same block."""
        offenders = []
        for path in sorted(glob.glob(os.path.join(_SKILLS, '**', '*.md'), recursive=True)):
            for block in _blocks(_read(path), 'dockerfile'):
                stage = ''
                for line in block.splitlines():
                    found = re.match(r'(?i)^\s*FROM\s+\S+(?:\s+AS\s+(\S+))?', line)
                    if found:
                        stage = (found.group(1) or '').lower()
                    elif re.match(r'(?i)^\s*(CMD|ENTRYPOINT)\b', line) and re.search(r'(?i)migrat', line) \
                            and stage != 'migrate':
                        offenders.append(f'{os.path.relpath(path, _SKILLS)}: {line.strip()}')
        self.assertEqual(offenders, [])

    def test_the_core_names_no_ecosystem_command(self) -> None:
        for token in ('npm ', 'pnpm', 'npx', 'node:', 'dist/main', 'node_modules', 'nestjs', 'dumb-init", "--"'):
            with self.subTest(token=token):
                self.assertFalse(token in self.core, f'the core names {token!r}; it belongs in a reference')

    def test_every_reference_is_listed_and_every_listed_reference_exists(self) -> None:
        listed = set(re.findall(r'`(references/[\w.-]+\.md)`', self.core))
        present = {os.path.relpath(p, _DOCKER) for p in glob.glob(os.path.join(_DOCKER, 'references', '*.md'))}
        self.assertTrue(present)
        self.assertEqual(listed, present)

    def test_the_node_reference_builds_migrate_from_a_stage_with_the_cli(self) -> None:
        node = _read(os.path.join(_DOCKER, 'references', 'node.md'))
        [migrate] = [b for b in _blocks(node, 'dockerfile') if re.search(r'(?m)^FROM \S+ AS migrate$', b)]
        self.assertIn('COPY --from=deps', migrate)
        self.assertNotIn('--from=builder', migrate)


if __name__ == '__main__':
    unittest.main()
