"""
Structural test for the subagents the plugin ships (agents/*.md).

An agent definition with broken frontmatter is not an error anywhere: the
harness skips it and the model simply never sees it offered.
"""
import re
import unittest
from pathlib import Path

_AGENTS = Path(__file__).resolve().parents[2] / 'agents'
_MODELS = {'haiku', 'sonnet', 'opus', 'fable', 'inherit'}


def _frontmatter(path: Path) -> dict:
    match = re.match(r'---\n(.*?)\n---\n', path.read_text(encoding='utf-8'), re.DOTALL)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip().strip('"')
    return fields


class TestAgentDefinitions(unittest.TestCase):

    def test_every_agent_has_valid_frontmatter(self):
        files = sorted(_AGENTS.glob('*.md'))
        self.assertTrue(files, msg='agents/ ships no agent')
        for path in files:
            with self.subTest(agent=path.name):
                fields = _frontmatter(path)
                self.assertEqual(fields.get('name'), path.stem)
                self.assertTrue(fields.get('description'))
                self.assertIn(fields.get('model'), _MODELS)

    def test_runner_is_cheap_readonly_and_skips_project_memory(self):
        fields = _frontmatter(_AGENTS / 'runner.md')
        self.assertEqual(fields['model'], 'haiku')
        self.assertEqual(fields['omitClaudeMd'], 'true')
        tools = {tool.strip() for tool in fields['tools'].split(',')}
        self.assertFalse(tools & {'Write', 'Edit', 'MultiEdit', 'Agent'})


if __name__ == '__main__':
    unittest.main()
