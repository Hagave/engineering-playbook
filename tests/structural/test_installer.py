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

    def _run(self, *args: str, path_prefix: str = '') -> subprocess.CompletedProcess:
        env = {**os.environ,
               'PLAYBOOK_INSTALL_RELEASES_URL': 'file://' + self.releases,
               'PATH': (path_prefix or self.bin) + os.pathsep + os.environ['PATH']}
        return subprocess.run(['bash', _INSTALL, '--dir', self.dir, *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def _fake_claude(self) -> str:
        log = os.path.join(self.tmp, 'claude.log')
        fake = os.path.join(self.bin, 'claude')
        with open(fake, 'w') as fh:
            fh.write(f'#!/bin/sh\necho "$*" >> "{log}"\n')
        os.chmod(fake, 0o755)
        return log

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
