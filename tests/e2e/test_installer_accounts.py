"""
End-to-end: install.sh installs the plugin into several Claude Code accounts
with the real `claude` CLI.

Opt-in: set PLAYBOOK_INSTALL_IT=1. It needs the `claude` CLI on PATH. It runs
under a temporary HOME with two accounts (~/.claude and ~/.claude-work), and
installs from this checkout as a local marketplace, so nothing reaches GitHub
and the real accounts are not touched.

The structural tests feed install.sh a fake `claude`; this test proves the real
CLI accepts the calls and records the install in each account's own config
directory, which is the reason the feature exists.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INSTALL = os.path.join(_ROOT, 'install.sh')
_PLUGIN_ID = 'engineering-playbook@engineering-playbook'


@unittest.skipUnless(os.environ.get('PLAYBOOK_INSTALL_IT') == '1',
                     'opt-in: set PLAYBOOK_INSTALL_IT=1 (needs the claude CLI)')
class TestInstallerAccountsLive(unittest.TestCase):

    def setUp(self) -> None:
        if shutil.which('claude') is None:
            self.skipTest('the claude CLI is not on PATH')
        self.tmp = tempfile.mkdtemp(prefix='pb_install_it_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        self.accounts = [os.path.join(self.home, '.claude'), os.path.join(self.home, '.claude-work')]
        for account in self.accounts:
            os.makedirs(account)
            with open(os.path.join(account, 'settings.json'), 'w') as fh:
                fh.write('{}\n')

    def test_all_accounts_installs_and_enables_the_plugin_in_each_account(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
        env.update({'HOME': self.home,
                    'PLAYBOOK_INSTALL_REPO': _ROOT,
                    'PLAYBOOK_INSTALL_TTY': os.path.join(self.tmp, 'no-tty')})
        r = subprocess.run(['bash', _INSTALL, '--all-accounts', '--dir', os.path.join(self.tmp, 'data')],
                           capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for account in self.accounts:
            with self.subTest(account=account):
                with open(os.path.join(account, 'plugins', 'installed_plugins.json')) as fh:
                    self.assertIn(_PLUGIN_ID, json.load(fh)['plugins'])
                with open(os.path.join(account, 'settings.json')) as fh:
                    self.assertIs(json.load(fh).get('enabledPlugins', {}).get(_PLUGIN_ID), True)


if __name__ == '__main__':
    unittest.main()
