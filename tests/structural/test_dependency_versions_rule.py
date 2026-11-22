"""
The dependency version rule in engineering-governance, and the templates it
corrected. The playbook installs the LTS line where a maintainer publishes one,
else the `latest` tag when it is stable, else the newest stable release; coupled
packages share the runtime package's version; and the installed version beats a
skill's example. These tests hold the rule and keep the code blocks that broke
under it from coming back: a bare install of the prisma CLI (its `latest` tag
was a Prisma 8 release candidate while the client stayed on 7), Joi's
`allowUnknown: false` over an environment that always holds the machine's own
variables, and a Node base image off its single LTS tag.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS = os.path.join(_ROOT, 'skills')
_FENCE_RE = re.compile(r'^```[^\n]*\n(.*?)^```', re.DOTALL | re.MULTILINE)


def _read(rel: str) -> str:
    with open(os.path.join(_SKILLS, rel), encoding='utf-8') as f:
        return f.read()


def _section(text: str, heading: str) -> str:
    match = re.search(rf'^## {re.escape(heading)}\n(.*?)(?=^## |\Z)', text, re.DOTALL | re.MULTILINE)
    if not match:
        raise AssertionError(f'section "## {heading}" not found')
    return match.group(1)


def _skill_code_blocks() -> list[tuple[str, str]]:
    found = []
    for path in sorted(glob.glob(os.path.join(_SKILLS, '**', '*.md'), recursive=True)):
        with open(path, encoding='utf-8') as f:
            found.extend((os.path.relpath(path, _SKILLS), block) for block in _FENCE_RE.findall(f.read()))
    return found


class TestDependencyVersionsRule(unittest.TestCase):

    def setUp(self) -> None:
        self.section = _section(_read('engineering-governance/SKILL.md'), 'Dependency versions')

    def test_lts_first_then_a_stable_latest_never_a_prerelease(self) -> None:
        self.assertIn('LTS line', self.section)
        self.assertIn('names a stable release', self.section)
        self.assertIn('newest stable release when it names a prerelease', self.section)
        self.assertIn('Never install a prerelease', self.section)

    def test_a_peer_range_is_honored_never_forced(self) -> None:
        self.assertIn("the package manager's resolution decides", self.section)
        self.assertIn('`--legacy-peer-deps`', self.section)

    def test_coupled_packages_share_the_runtime_version(self) -> None:
        self.assertIn('Coupled packages share one version', self.section)
        self.assertIn("the runtime package's", self.section)

    def test_the_installed_version_beats_the_skill_and_is_reported(self) -> None:
        self.assertIn('The installed version decides, not the skill', self.section)
        self.assertIn('lockfile or the manifest', self.section)
        self.assertIn('[PLAYBOOK-DEVIATION]', self.section)
        self.assertIn('version <name>@<version>:', self.section)


class TestTemplatesFollowTheRule(unittest.TestCase):

    def test_no_code_block_installs_the_prisma_cli_without_a_version(self) -> None:
        bare = re.compile(r'\badd\s+(?:-D|--save-dev)\s+(?:[\w@/.-]+\s+)*prisma(?![@\w/-])')
        for rel, block in _skill_code_blocks():
            with self.subTest(file=rel):
                self.assertNotRegex(block, bare)

    def test_the_prisma_cli_and_adapter_are_installed_at_the_client_version(self) -> None:
        for rel in ('engineering-prisma/SKILL.md', 'engineering-nestjs/SKILL.md'):
            with self.subTest(file=rel):
                text = _read(rel)
                self.assertIn('-D prisma@<the installed @prisma/client version>', text)
                self.assertIn('@prisma/adapter-pg@<the installed @prisma/client version>', text)

    def test_no_code_block_rejects_unknown_environment_variables(self) -> None:
        for rel, block in _skill_code_blocks():
            with self.subTest(file=rel):
                self.assertNotRegex(block, r'allowUnknown\s*:\s*false')

    def test_every_config_module_template_serves_only_declared_variables(self) -> None:
        templates = [(rel, b) for rel, b in _skill_code_blocks() if 'ConfigModule.forRoot' in b]
        self.assertTrue(templates)
        for rel, block in templates:
            with self.subTest(file=rel):
                self.assertIn('skipProcessEnv: true', block)

    def test_the_node_base_image_is_one_pinned_lts_tag(self) -> None:
        images = set(re.findall(r'^FROM (node:\S+)', _read('engineering-docker/references/node.md'), re.MULTILINE))
        self.assertEqual(len(images), 1, f'the stages use different Node images: {images}')
        self.assertRegex(images.pop(), r'^node:\d+-alpine$')


if __name__ == '__main__':
    unittest.main()
