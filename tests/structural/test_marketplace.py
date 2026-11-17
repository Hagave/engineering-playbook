"""
Structural tests for the repository's own Claude Code marketplace
(.claude-plugin/marketplace.json), the install path the README gives users.

The entry must name the plugin exactly as its manifest does, or
`claude plugin install engineering-playbook@engineering-playbook` fails with
"not found in marketplace". The version lives in plugin.json only: Claude Code
reads it first, and a second copy in the entry is one the release would have
to keep in step by hand.
"""
import json
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(*parts: str) -> dict:
    with open(os.path.join(_ROOT, *parts), encoding='utf-8') as fh:
        return json.load(fh)


class TestMarketplace(unittest.TestCase):

    def setUp(self) -> None:
        self.marketplace = _load('.claude-plugin', 'marketplace.json')
        self.plugin = _load('.claude-plugin', 'plugin.json')

    def test_lists_this_plugin_from_the_repository_root(self) -> None:
        entries = self.marketplace['plugins']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], self.plugin['name'])
        self.assertEqual(entries[0]['source'], './')

    def test_the_version_lives_in_plugin_json_only(self) -> None:
        self.assertNotIn('version', self.marketplace['plugins'][0])
        self.assertRegex(self.plugin['version'], r'^\d+\.\d+\.\d+$')

    def test_has_a_name_and_an_owner(self) -> None:
        self.assertEqual(self.marketplace['name'], 'engineering-playbook')
        self.assertTrue(self.marketplace['owner']['name'])

    def test_the_readme_install_line_names_this_marketplace(self) -> None:
        with open(os.path.join(_ROOT, 'README.md'), encoding='utf-8') as fh:
            readme = fh.read()
        self.assertIn('claude plugin install engineering-playbook --marketplace '
                      'Hagave/engineering-playbook', readme)
        self.assertIn(f'/plugin install {self.plugin["name"]}@{self.marketplace["name"]}', readme)


if __name__ == '__main__':
    unittest.main()
