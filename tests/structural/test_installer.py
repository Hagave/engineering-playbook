"""
Tests for install.sh, the curl installer, run against a release served from a
local directory (PLAYBOOK_INSTALL_RELEASES_URL=file://...) and a fake `claude`
CLI, so nothing reaches GitHub.

The release archive is built the way .github/workflows/release.yml builds it:
`git archive --prefix=engineering-playbook-<version>/`, with a `.sha256`
next to it.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INSTALL = os.path.join(_ROOT, 'install.sh')
_RELEASE_YML = os.path.join(_ROOT, '.github', 'workflows', 'release.yml')


def _git_tree() -> bool:
    return subprocess.run(['git', '-C', _ROOT, 'rev-parse', 'HEAD'],
                          capture_output=True).returncode == 0


@unittest.skipUnless(_git_tree(), 'needs the git checkout to build a release archive')
class TestInstaller(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='pb_install_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.releases = os.path.join(self.tmp, 'releases')
        self.dir = os.path.join(self.tmp, 'data')
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        # A HOME of its own: account detection reads ~/.claude*, the shell
        # startup files, and must never see the developer's real ones.
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)

    def _release(self, version: str, corrupt: bool = False) -> None:
        folder = os.path.join(self.releases, f'v{version}')
        os.makedirs(folder, exist_ok=True)
        asset = os.path.join(folder, f'engineering-playbook-{version}.tar.gz')
        subprocess.run(['git', '-C', _ROOT, 'archive', '--format=tar.gz',
                        f'--prefix=engineering-playbook-{version}/', '-o', asset, 'HEAD'],
                       check=True, capture_output=True)
        with open(asset, 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if corrupt:
            digest = '0' * 64
        with open(asset + '.sha256', 'w') as fh:
            fh.write(f'{digest}  engineering-playbook-{version}.tar.gz\n')

    def _run(self, *args: str, path_prefix: str = '', tty: str | None = None,
             extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run install.sh. `tty` is what the person types at the account
        question; None means there is no terminal to ask on."""
        tty_path = os.path.join(self.tmp, 'no-tty')
        if tty is not None:
            tty_path = os.path.join(self.tmp, 'tty-answer')
            with open(tty_path, 'w') as fh:
                fh.write(tty + '\n')
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
        env.update({'HOME': self.home,
                    'PLAYBOOK_INSTALL_TTY': tty_path,
                    'PLAYBOOK_INSTALL_RELEASES_URL': 'file://' + self.releases,
                    'PATH': (path_prefix or self.bin) + os.pathsep + os.environ['PATH']})
        env.update(extra_env or {})
        return subprocess.run(['bash', _INSTALL, '--dir', self.dir, *args],
                              capture_output=True, text=True, env=env, timeout=120,
                              cwd=self.tmp)

    def _fake_claude(self) -> str:
        log = os.path.join(self.tmp, 'claude.log')
        fake = os.path.join(self.bin, 'claude')
        with open(fake, 'w') as fh:
            fh.write(f'#!/bin/sh\necho "$*" >> "{log}"\n')
        os.chmod(fake, 0o755)
        return log

    def _fake_claude_per_account(self, fail_for: str = '') -> str:
        """A claude CLI that logs `<CLAUDE_CONFIG_DIR or unset>|<args>` and
        fails for the account `fail_for`."""
        log = os.path.join(self.tmp, 'claude.log')
        fake = os.path.join(self.bin, 'claude')
        with open(fake, 'w') as fh:
            fh.write('#!/bin/sh\n'
                     'acct="${CLAUDE_CONFIG_DIR-unset}"\n'
                     f'echo "$acct|$*" >> "{log}"\n'
                     f'[ "$acct" = "{fail_for}" ] && exit 1\n'
                     'exit 0\n')
        os.chmod(fake, 0o755)
        return log

    def _account(self, name: str, marker: str = 'settings.json') -> str:
        """A config directory under the fake HOME that Claude Code has used."""
        path = os.path.join(self.home, name)
        os.makedirs(path, exist_ok=True)
        if marker.endswith('/'):
            os.makedirs(os.path.join(path, marker), exist_ok=True)
        elif marker:
            open(os.path.join(path, marker), 'w').close()
        return path

    def _installed_into(self, log: str) -> list[str]:
        """The accounts `plugin install` ran in, in order ("unset" is ~/.claude
        with CLAUDE_CONFIG_DIR unset)."""
        if not os.path.exists(log):
            return []
        with open(log) as fh:
            lines = fh.read().splitlines()
        return [line.split('|', 1)[0] for line in lines
                if line.split('|', 1)[1].startswith('plugin install ')]

    def _startup_file(self, name: str, text: str) -> None:
        path = os.path.join(self.home, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(text)

    # ── Release download ──────────────────────────────────────────────────────

    def test_a_verified_release_is_unpacked_and_linked_as_current(self) -> None:
        self._release('9.9.9')
        r = self._run('--download-only', '--version', '9.9.9')
        self.assertEqual(r.returncode, 0, r.stderr)
        current = os.path.join(self.dir, 'current')
        self.assertEqual(r.stdout.strip().splitlines()[-1], current)
        self.assertEqual(os.readlink(current), '9.9.9')
        self.assertTrue(os.path.isfile(os.path.join(current, 'hooks', 'context_guard.py')))

    def test_a_checksum_mismatch_installs_nothing(self) -> None:
        self._release('9.9.9', corrupt=True)
        r = self._run('--download-only', '--version', '9.9.9')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('checksum mismatch', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, '9.9.9')))
        self.assertFalse(os.path.lexists(os.path.join(self.dir, 'current')))
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith('.download-')], [])

    def test_a_missing_release_says_which_versions_have_an_archive(self) -> None:
        r = self._run('--download-only', '--version', '4.0.1')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('4.0.2', r.stderr)

    def test_an_update_moves_the_link_and_keeps_the_previous_release(self) -> None:
        self._release('9.9.9')
        self._release('9.9.10')
        self.assertEqual(self._run('--download-only', '--version', '9.9.9').returncode, 0)
        r = self._run('--download-only', '--version', '9.9.10')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.readlink(os.path.join(self.dir, 'current')), '9.9.10')
        self.assertTrue(os.path.isdir(os.path.join(self.dir, '9.9.9')))

    def test_a_release_already_unpacked_is_reused(self) -> None:
        self._release('9.9.9')
        self._run('--download-only', '--version', '9.9.9')
        r = self._run('--download-only', '--version', '9.9.9')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('already in', r.stdout)

    def test_a_version_that_is_not_x_y_z_is_refused(self) -> None:
        r = self._run('--download-only', '--version', '1.2')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('not X.Y.Z', r.stderr)

    # ── Harnesses ─────────────────────────────────────────────────────────────

    def test_first_claude_install_shows_a_quick_start_then_is_quiet_on_update(self) -> None:
        self._fake_claude()
        first = self._run()  # default claude harness, first time
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('quick start', first.stdout.lower())
        self.assertIn('/playbook-config', first.stdout)
        second = self._run()  # same dir again: an update stays quiet
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn('quick start', second.stdout.lower())

    def test_antigravity_is_installed_from_the_current_link(self) -> None:
        # The commit-msg hook names the checker through DIR/current, so an
        # update that moves the link reaches it without a reinstall.
        self._release('9.9.9')
        workspace = os.path.join(self.tmp, 'app')
        subprocess.run(['git', 'init', '-q', workspace], check=True)
        r = self._run('--antigravity', workspace, '--git-hooks', '--version', '9.9.9')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(workspace, 'AGENTS.md')))
        with open(os.path.join(workspace, '.git', 'hooks', 'commit-msg')) as fh:
            hook = fh.read()
        self.assertIn(os.path.join(self.dir, 'current', 'scripts', 'check_commit_message.py'), hook)

    def test_antigravity_needs_an_existing_workspace(self) -> None:
        self._release('9.9.9')
        r = self._run('--antigravity', os.path.join(self.tmp, 'missing'), '--version', '9.9.9')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('does not exist', r.stderr)

    def test_claude_code_installs_through_the_marketplace(self) -> None:
        log = self._fake_claude()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as fh:
            calls = fh.read().splitlines()
        self.assertEqual(calls, [
            'plugin marketplace add Hagave/engineering-playbook',
            'plugin marketplace update engineering-playbook',
            'plugin install engineering-playbook@engineering-playbook',
            'plugin update engineering-playbook@engineering-playbook',
        ])
        self.assertIn('Enable auto-update', r.stdout)

    def test_claude_code_can_be_pinned_to_a_release(self) -> None:
        log = self._fake_claude()
        self.assertEqual(self._run('--claude', '--version', 'v4.0.2').returncode, 0)
        with open(log) as fh:
            self.assertEqual(fh.readline().strip(),
                             'plugin marketplace add Hagave/engineering-playbook#v4.0.2')

    def test_claude_code_refuses_a_version_that_is_not_x_y_z(self) -> None:
        log = self._fake_claude()
        r = self._run('--version', '4.0.2;rm')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('not X.Y.Z', r.stderr)
        self.assertFalse(os.path.exists(log))

    def test_claude_code_without_the_cli_says_how_to_get_it(self) -> None:
        empty = os.path.join(self.tmp, 'empty-bin')
        os.makedirs(empty)
        env = {**os.environ, 'PATH': empty + os.pathsep + '/usr/bin' + os.pathsep + '/bin'}
        if shutil.which('claude', path=env['PATH']):
            self.skipTest('a claude CLI is installed system-wide')
        r = subprocess.run(['bash', _INSTALL], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Install Claude Code', r.stderr)

    # ── Claude Code accounts ──────────────────────────────────────────────────

    def test_with_one_account_it_installs_there_without_asking(self) -> None:
        self._account('.claude')
        log = self._fake_claude_per_account()
        r = self._run(tty='n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._installed_into(log), ['unset'])
        self.assertNotIn('Also found', r.stdout + r.stderr)

    def test_without_a_terminal_only_the_current_account_is_installed(self) -> None:
        self._account('.claude')
        self._account('.claude-work')
        log = self._fake_claude_per_account()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._installed_into(log), ['unset'])
        self.assertIn('1 more Claude Code account(s) found', r.stdout)
        self.assertIn('--all-accounts', r.stdout)

    def test_all_accounts_runs_each_with_its_own_config_dir(self) -> None:
        self._account('.claude')
        work = self._account('.claude-work')
        hv = self._account('.claude-hv', marker='projects/')
        log = self._fake_claude_per_account()
        # Run from a shell set to the work account: it comes first, and the
        # default account still runs with CLAUDE_CONFIG_DIR unset, not pointed
        # at ~/.claude.
        r = self._run('--all-accounts', extra_env={'CLAUDE_CONFIG_DIR': work})
        self.assertEqual(r.returncode, 0, r.stderr)
        installed = self._installed_into(log)
        self.assertEqual(installed[0], work)
        self.assertCountEqual(installed, [work, 'unset', hv])
        self.assertIn('auto-update is set per account', r.stdout)

    def test_the_answer_on_the_terminal_picks_the_accounts(self) -> None:
        self._account('.claude')
        a = self._account('.claude-a')
        b = self._account('.claude-b')
        cases = {'': ['unset', a, b], 'a': ['unset', a, b], 'ALL': ['unset', a, b],
                 'n': ['unset'], '2': ['unset', b], '1, 2': ['unset', a, b]}
        for answer, expected in cases.items():
            with self.subTest(answer=answer):
                shutil.rmtree(self.dir, ignore_errors=True)
                log = self._fake_claude_per_account()
                if os.path.exists(log):
                    os.remove(log)
                r = self._run(tty=answer)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertCountEqual(self._installed_into(log), expected)
                self.assertIn('Also found', r.stderr)

    def test_an_answer_not_understood_installs_nothing(self) -> None:
        self._account('.claude')
        self._account('.claude-a')
        for answer in ('x', '3', '0', '01', '1;rm'):
            with self.subTest(answer=answer):
                log = self._fake_claude_per_account()
                if os.path.exists(log):
                    os.remove(log)
                r = self._run(tty=answer)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self._installed_into(log), [])
                self.assertFalse(os.path.exists(os.path.join(self.dir, 'claude-accounts')))

    def test_a_rerun_reuses_the_answer_and_asks_only_about_a_new_account(self) -> None:
        self._account('.claude')
        work = self._account('.claude-work')
        declined = self._account('.claude-old')
        log = self._fake_claude_per_account()
        self.assertEqual(self._run(tty='2').returncode, 0)  # asked in order: 1) old, 2) work
        os.remove(log)
        again = self._run()  # no terminal: the saved answer still applies
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertCountEqual(self._installed_into(log), ['unset', work])
        self.assertNotIn('more Claude Code account', again.stdout)
        self.assertNotIn(declined, self._installed_into(log))
        new = self._account('.claude-new')
        os.remove(log)
        third = self._run(tty='a')
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertCountEqual(self._installed_into(log), ['unset', work, new])
        self.assertIn(new, third.stderr)
        self.assertNotIn(declined, third.stderr)  # answered before: not asked again

    def test_an_alias_in_a_startup_file_names_an_account(self) -> None:
        self._account('.claude')
        zsh = self._account('accounts/zsh')
        bash = self._account('accounts/bash')
        fish = self._account('accounts/fish')
        tilde = self._account('accounts/tilde')
        self._startup_file('.zshrc', "alias cz='CLAUDE_CONFIG_DIR=\"$HOME/accounts/zsh\" claude'\n")
        self._startup_file('.bashrc', 'alias cb="CLAUDE_CONFIG_DIR=${HOME}/accounts/bash claude"\n'
                                      'export CLAUDE_CONFIG_DIR=~/accounts/tilde\n')
        self._startup_file('.config/fish/config.fish', 'set -x CLAUDE_CONFIG_DIR ~/accounts/fish\n')
        log = self._fake_claude_per_account()
        r = self._run('--all-accounts')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertCountEqual(self._installed_into(log), ['unset', zsh, bash, fish, tilde])

    def test_startup_files_are_read_as_text_and_never_run(self) -> None:
        """Hostile startup-file lines: nothing they hold runs, and none of
        them becomes an account."""
        self._account('.claude')
        os.makedirs(os.path.join(self.tmp, 'claude'))  # a relative name that exists in cwd
        open(os.path.join(self.tmp, 'claude', 'settings.json'), 'w').close()
        self._account('elsewhere')
        hostile = {
            'command substitution': 'export CLAUDE_CONFIG_DIR=$(touch "$HOME/PWNED")',
            'backticks': 'export CLAUDE_CONFIG_DIR=`touch $HOME/PWNED`',
            'another variable': 'export CLAUDE_CONFIG_DIR=$OTHER/elsewhere',
            'relative path': 'alias c="env -u CLAUDE_CONFIG_DIR claude"',
            'chained command': 'export CLAUDE_CONFIG_DIR=;touch$IFS$HOME/PWNED',
        }
        self._startup_file('.zshrc', '\n'.join(hostile.values()) + '\n')
        log = self._fake_claude_per_account()
        r = self._run('--all-accounts', extra_env={'OTHER': self.home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._installed_into(log), ['unset'])
        self.assertFalse(os.path.exists(os.path.join(self.home, 'PWNED')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'PWNED')))

    def test_home_claude_named_in_an_alias_is_the_default_account(self) -> None:
        self._account('.claude')
        self._startup_file('.zshrc', 'alias c="CLAUDE_CONFIG_DIR=$HOME/.claude/ claude"\n')
        log = self._fake_claude_per_account()
        r = self._run('--all-accounts')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._installed_into(log), ['unset'])

    def test_a_directory_claude_code_never_used_is_not_an_account(self) -> None:
        self._account('.claude')
        self._account('.claude-backup', marker='')
        log = self._fake_claude_per_account()
        r = self._run('--all-accounts')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._installed_into(log), ['unset'])

    def test_accounts_installs_exactly_the_directories_named(self) -> None:
        self._account('.claude')
        work = self._account('.claude-work')
        self._account('.claude-other')
        log = self._fake_claude_per_account()
        r = self._run('--accounts', 'default,~/.claude-work')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._installed_into(log), ['unset', work])

    def test_accounts_refuses_a_missing_or_relative_directory_and_installs_nothing(self) -> None:
        self._account('.claude')
        for value in (os.path.join(self.home, 'missing'), 'relative', '$OTHER/x', ','):
            with self.subTest(value=value):
                log = self._fake_claude_per_account()
                r = self._run('--accounts', value)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self._installed_into(log), [])

    def test_account_options_apply_to_a_claude_code_install_only(self) -> None:
        for args in (('--codex', '--all-accounts'), ('--download-only', '--accounts', 'default'),
                     ('--all-accounts', '--accounts', 'default'),
                     ('--accounts', 'default', '--all-accounts')):
            with self.subTest(args=args):
                r = self._run(*args)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('--accounts', r.stderr)

    def test_an_account_that_fails_does_not_stop_the_others(self) -> None:
        self._account('.claude')
        work = self._account('.claude-work')
        hv = self._account('.claude-hv')
        log = self._fake_claude_per_account(fail_for=work)
        r = self._run('--all-accounts')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(work, r.stderr)
        self.assertIn('unset', self._installed_into(log))
        self.assertIn(hv, self._installed_into(log))

    def test_git_hooks_without_antigravity_is_refused(self) -> None:
        r = self._run('--codex', '--git-hooks')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--antigravity only', r.stderr)


class TestInstallerShape(unittest.TestCase):

    def test_a_download_cut_short_runs_nothing(self) -> None:
        """curl | bash runs what arrived; everything is inside main(), called
        on the last line, so a truncated script is a syntax error, not half an
        install."""
        with open(_INSTALL, encoding='utf-8') as fh:
            text = fh.read()
        self.assertEqual(text.rstrip().splitlines()[-1], 'main "$@"')
        cut = text[:text.index('main() {') + len('main() {') + 40]
        r = subprocess.run(['bash', '-s', '--', '--help'], input=cut,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('[playbook]', r.stdout)

    def test_the_release_workflow_publishes_the_archive_the_installer_fetches(self) -> None:
        with open(_RELEASE_YML, encoding='utf-8') as fh:
            workflow = fh.read()
        with open(_INSTALL, encoding='utf-8') as fh:
            installer = fh.read()
        self.assertIn('asset="engineering-playbook-${TAG#v}.tar.gz"', workflow)
        self.assertIn('--prefix="engineering-playbook-${TAG#v}/"', workflow)
        self.assertIn('"$asset" "$asset.sha256"', workflow)
        self.assertIn('local asset="engineering-playbook-${version}.tar.gz"', installer)
        self.assertIn('${RELEASES_URL}/v${version}/${asset}', installer)


if __name__ == '__main__':
    unittest.main()
