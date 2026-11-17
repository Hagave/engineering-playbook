"""
Structural tests for the Antigravity adapter (adapters/antigravity/).

Runs install.sh into a temp workspace and verifies the produced artifacts:
skills copied with frontmatter stripped, workflows present, AGENTS.md marker
block written idempotently.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INSTALL = os.path.join(_ROOT, 'adapters', 'antigravity', 'install.sh')
_SKILLS_SRC = os.path.join(_ROOT, 'skills')

MARK_START = '<!-- engineering-playbook:start -->'
MARK_END = '<!-- engineering-playbook:end -->'


def _run_install(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', _INSTALL, target],
        capture_output=True,
        text=True,
    )


class TestAntigravityInstall(unittest.TestCase):

    def setUp(self):
        self.target = tempfile.mkdtemp(prefix='pb_antigravity_')

    def tearDown(self):
        shutil.rmtree(self.target, ignore_errors=True)

    def test_install_exits_zero(self):
        r = _run_install(self.target)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_all_skills_copied(self):
        _run_install(self.target)
        src_count = len([
            d for d in os.listdir(_SKILLS_SRC)
            if os.path.isfile(os.path.join(_SKILLS_SRC, d, 'SKILL.md'))
        ])
        dst = os.path.join(self.target, '.agents', 'skills')
        dst_count = len([f for f in os.listdir(dst) if f.endswith('.md')])
        self.assertEqual(dst_count, src_count)
        self.assertGreaterEqual(dst_count, 30)

    def test_skill_references_copied_next_to_the_flattened_skill(self):
        _run_install(self.target)
        src = os.path.join(_SKILLS_SRC, 'engineering-security', 'references')
        dst = os.path.join(self.target, '.agents', 'skills', 'engineering-security', 'references')
        self.assertEqual(sorted(os.listdir(dst)), sorted(os.listdir(src)))

    def test_frontmatter_stripped(self):
        _run_install(self.target)
        dst = os.path.join(self.target, '.agents', 'skills')
        for name in os.listdir(dst):
            if not name.endswith('.md'):
                continue  # a skill's references/ directory
            with open(os.path.join(dst, name)) as f:
                first = f.readline().rstrip('\n')
            self.assertNotEqual(first, '---', f'frontmatter not stripped: {name}')
            self.assertNotEqual(first, '', f'leading blank line left: {name}')

    def test_rule_anchor_preserved(self):
        """The RULE-ANCHOR block is plain markdown and must survive the copy."""
        _run_install(self.target)
        path = os.path.join(self.target, '.agents', 'skills', 'engineering-playbook.md')
        with open(path) as f:
            content = f.read()
        self.assertIn('<!-- RULE-ANCHOR -->', content)

    def test_workflows_installed(self):
        _run_install(self.target)
        dst = os.path.join(self.target, '.agents', 'workflows')
        names = sorted(os.listdir(dst))
        self.assertIn('engineering-playbook.md', names)
        self.assertIn('playbook-off.md', names)
        self.assertIn('playbook-on.md', names)
        self.assertIn('playbook-config.md', names)

    def test_agents_md_created_with_markers(self):
        _run_install(self.target)
        with open(os.path.join(self.target, 'AGENTS.md')) as f:
            content = f.read()
        self.assertEqual(content.count(MARK_START), 1)
        self.assertEqual(content.count(MARK_END), 1)
        self.assertIn('Model routing (proportional tiers)', content)
        self.assertIn('Skill routing', content)

    def test_reinstall_is_idempotent(self):
        _run_install(self.target)
        _run_install(self.target)
        with open(os.path.join(self.target, 'AGENTS.md')) as f:
            content = f.read()
        self.assertEqual(content.count(MARK_START), 1, 'marker block duplicated on reinstall')
        self.assertEqual(content.count(MARK_END), 1)

    def test_upgrade_removes_the_3x_commands_and_skills(self):
        """Release review of 4.0.0: a reinstall over 3.x left the old
        /engineering-playbook-on|off|status in the menu."""
        legacy = [os.path.join(self.target, '.agents', 'workflows', 'engineering-playbook-status.md'),
                  os.path.join(self.target, '.agents', 'skills', 'engineering-prose.md')]
        for path in legacy:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write('old\n')
        r = _run_install(self.target)
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in legacy:
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.exists(
            os.path.join(self.target, '.agents', 'workflows', 'playbook-config.md')))

    def test_existing_agents_md_content_preserved(self):
        user_content = '# My project\n\nCustom project notes.\n'
        with open(os.path.join(self.target, 'AGENTS.md'), 'w') as f:
            f.write(user_content)
        _run_install(self.target)
        _run_install(self.target)
        with open(os.path.join(self.target, 'AGENTS.md')) as f:
            content = f.read()
        self.assertIn('Custom project notes.', content)
        self.assertEqual(content.count(MARK_START), 1)

    def test_missing_target_fails(self):
        r = _run_install(os.path.join(self.target, 'does-not-exist'))
        self.assertNotEqual(r.returncode, 0)

    def test_no_model_ids_hardcoded_as_rules(self):
        """The template names tiers with examples, never a pinned model ID string."""
        with open(os.path.join(_ROOT, 'adapters', 'antigravity', 'AGENTS.template.md')) as f:
            content = f.read()
        for forbidden in ('claude-sonnet-4', 'claude-haiku-4', 'gemini-3-pro-preview'):
            self.assertNotIn(forbidden, content)



class TestSettings(unittest.TestCase):
    """Antigravity has no SessionStart hook: AGENTS.md names the clone so the
    agent reads the user's settings from it."""

    def setUp(self):
        self.target = tempfile.mkdtemp(prefix='pb_agset_')
        self.addCleanup(shutil.rmtree, self.target, True)

    def test_agents_md_names_the_clone_and_the_session_command(self):
        r = subprocess.run(['bash', _INSTALL, self.target], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.target, 'AGENTS.md')) as f:
            text = f.read()
        self.assertNotIn('@@PLAYBOOK@@', text)
        home = os.path.expanduser('~')
        ref = '$HOME' + _ROOT[len(home):] if _ROOT.startswith(home + os.sep) else _ROOT
        command = f'python3 "{ref}/scripts/playbook_config.py" session --harness antigravity'
        self.assertIn(command, text)

    def test_the_clone_under_home_is_not_written_with_the_user_name(self):
        env = {**os.environ, 'HOME': os.path.dirname(_ROOT)}
        r = subprocess.run(['bash', _INSTALL, self.target], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.target, 'AGENTS.md')) as f:
            text = f.read()
        self.assertIn('"$HOME/' + os.path.basename(_ROOT) + '/scripts/playbook_config.py"', text)
        self.assertNotIn(os.path.dirname(_ROOT) + '/', text)

    def test_the_session_command_in_agents_md_runs(self):
        subprocess.run(['bash', _INSTALL, self.target], capture_output=True, check=True)
        r = subprocess.run(['python3', os.path.join(_ROOT, 'scripts', 'playbook_config.py'),
                            'session', '--harness', 'antigravity'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Prose budget:', r.stdout)


class TestGitHooks(unittest.TestCase):
    """--git-hooks installs mechanical enforcement; never touches foreign hooks."""

    def setUp(self):
        self.target = tempfile.mkdtemp(prefix='pb_aghooks_')
        subprocess.run(['git', 'init', '-q', self.target], check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.target, ignore_errors=True)

    def _install(self, *flags):
        return subprocess.run(
            ['bash', _INSTALL, *flags, self.target],
            capture_output=True, text=True,
        )

    def _hook(self, name):
        return os.path.join(self.target, '.git', 'hooks', name)

    def test_hooks_not_installed_by_default(self):
        r = self._install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self._hook('commit-msg')))
        self.assertFalse(os.path.exists(self._hook('pre-commit')))

    def test_hooks_installed_with_flag(self):
        r = self._install('--git-hooks')
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ('commit-msg', 'pre-commit'):
            path = self._hook(name)
            self.assertTrue(os.path.exists(path), name)
            self.assertTrue(os.access(path, os.X_OK), f'{name} not executable')

    def test_flag_requires_git_repo(self):
        plain = tempfile.mkdtemp(prefix='pb_nogit_')
        try:
            r = subprocess.run(
                ['bash', _INSTALL, '--git-hooks', plain],
                capture_output=True, text=True,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('git repository', r.stderr)
        finally:
            shutil.rmtree(plain, ignore_errors=True)

    def test_commit_msg_hook_blocks_ai_attribution(self):
        self._install('--git-hooks')
        msg = os.path.join(self.target, 'msg.txt')
        with open(msg, 'w') as f:
            f.write('feat: add endpoint\n\nCo-Authored-By: Claude Opus\n')
        r = subprocess.run(['bash', self._hook('commit-msg'), msg],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('attribution', r.stderr)

    def _commit_msg_with_old_python(self, message: str) -> subprocess.CompletedProcess:
        # playbook_core needs 3.10 at import; git may run the hook with a PATH
        # whose python3 is older (the macOS Command Line Tools ship 3.9).
        fake_bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, fake_bin)
        fake = os.path.join(fake_bin, 'python3')
        with open(fake, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')  # fails the version probe, as 3.9 does
        os.chmod(fake, 0o755)
        msg = os.path.join(self.target, 'msg.txt')
        with open(msg, 'w') as f:
            f.write(message)
        env = {**os.environ, 'PATH': fake_bin + os.pathsep + os.environ['PATH']}
        return subprocess.run(['bash', self._hook('commit-msg'), msg],
                              capture_output=True, text=True, env=env)

    def test_commit_msg_hook_still_blocks_on_a_python_older_than_3_10(self):
        """Ultra review of 4.0.0: the hook let every commit through there,
        which is the default macOS setup."""
        self._install('--git-hooks')
        for message in ('feat: x\n\nCo-Authored-By: Claude Opus\n',
                        'fix: y\n\nCo-Authored-By: Bot <noreply@anthropic.com>\n',
                        'docs: z\n\nGenerated with [Claude Code](https://claude.com)\n',
                        'fix: w\n\nco-authored-by: Gemini 3 Pro\n'):
            with self.subTest(message=message):
                r = self._commit_msg_with_old_python(message)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn('attribution', r.stderr)

    def test_commit_msg_hook_passes_a_clean_message_on_an_old_python(self):
        self._install('--git-hooks')
        r = self._commit_msg_with_old_python('feat: x\n\nCo-Authored-By: Maria Silva <m@x.com>\n')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_commit_msg_fallback_is_rendered_from_the_shared_rule(self):
        # No hand-kept copy of the rule in the hook: each alternative is the
        # one playbook_core keeps for `git log --grep`.
        sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
        import playbook_core
        self._install('--git-hooks')
        with open(self._hook('commit-msg')) as f:
            body = f.read()
        for pattern in playbook_core._AI_ATTRIBUTION_GREP:
            self.assertIn(shlex.quote(pattern), body)

    def test_commit_msg_hook_ignores_the_session_override(self):
        # A git hook inherits the variable from the `git commit` that sets it,
        # so an agent could switch the check off on its own command.
        self._install('--git-hooks')
        msg = os.path.join(self.target, 'msg.txt')
        with open(msg, 'w') as f:
            f.write('fix: bug\n\nCo-Authored-By: Gemini\n')
        env = {**os.environ, 'PLAYBOOK_ALLOW_AI_ATTRIBUTION': '1'}
        r = subprocess.run(['bash', self._hook('commit-msg'), msg],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_commit_msg_hook_blocks_gemini_attribution(self):
        self._install('--git-hooks')
        msg = os.path.join(self.target, 'msg.txt')
        with open(msg, 'w') as f:
            f.write('fix: bug\n\nco-authored-by: Gemini 3 Pro\n')
        r = subprocess.run(['bash', self._hook('commit-msg'), msg],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)

    def test_commit_msg_hook_uses_the_shared_rule(self):
        # The noreply address is in playbook_core.AI_ATTRIBUTION_RE only; the
        # old shell copy of the rule let it through.
        self._install('--git-hooks')
        msg = os.path.join(self.target, 'msg.txt')
        with open(msg, 'w') as f:
            f.write('fix: bug\n\nCo-Authored-By: Bot <noreply@anthropic.com>\n')
        r = subprocess.run(['bash', self._hook('commit-msg'), msg],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_commit_msg_hook_still_blocks_when_the_checker_is_gone(self):
        self._install('--git-hooks')
        with open(self._hook('commit-msg')) as f:
            body = f.read().replace('check_commit_message.py', 'missing_checker.py')
        with open(self._hook('commit-msg'), 'w') as f:
            f.write(body)
        msg = os.path.join(self.target, 'msg.txt')
        with open(msg, 'w') as f:
            f.write('feat: x\n\nCo-Authored-By: Claude\n')
        r = subprocess.run(['bash', self._hook('commit-msg'), msg],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('attribution', r.stderr)

    def test_commit_msg_hook_allows_clean_message(self):
        self._install('--git-hooks')
        msg = os.path.join(self.target, 'msg.txt')
        with open(msg, 'w') as f:
            f.write('feat: add endpoint\n\nCo-Authored-By: Maria Silva <m@x.com>\n')
        r = subprocess.run(['bash', self._hook('commit-msg'), msg],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pre_commit_passes_on_project_without_ts_tooling(self):
        self._install('--git-hooks')
        r = subprocess.run(['bash', self._hook('pre-commit')],
                           capture_output=True, text=True, cwd=self.target)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pre_commit_ignores_a_skip_variable(self):
        # The hook inherits the environment of the `git commit` that runs it,
        # so a variable the agent sets on its own command must not skip it.
        self._install('--git-hooks')
        with open(os.path.join(self.target, 'tsconfig.json'), 'w') as f:
            f.write('{}')
        fake_bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, fake_bin)
        with open(os.path.join(fake_bin, 'npx'), 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')  # a failing typecheck
        os.chmod(os.path.join(fake_bin, 'npx'), 0o755)
        env = {**os.environ, 'PLAYBOOK_SKIP_VALIDATION': '1',
               'PATH': fake_bin + os.pathsep + os.environ['PATH']}
        r = subprocess.run(['bash', self._hook('pre-commit')],
                           capture_output=True, text=True, cwd=self.target, env=env)
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_existing_foreign_hook_not_overwritten(self):
        custom = '#!/bin/sh\necho my-custom-hook\n'
        with open(self._hook('commit-msg'), 'w') as f:
            f.write(custom)
        r = self._install('--git-hooks')
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self._hook('commit-msg')) as f:
            self.assertEqual(f.read(), custom)
        self.assertIn('NOT overwritten', r.stderr)

    def test_reinstall_updates_own_hooks(self):
        self._install('--git-hooks')
        self._install('--git-hooks')
        with open(self._hook('commit-msg')) as f:
            content = f.read()
        self.assertEqual(content.count('engineering-playbook hook'), 1)


class TestCiWorkflow(unittest.TestCase):
    """--ci writes the server-side check: git hooks can be skipped with
    --no-verify, a required CI check cannot."""

    def setUp(self):
        self.target = tempfile.mkdtemp(prefix='pb_agci_')
        self.addCleanup(shutil.rmtree, self.target, True)
        subprocess.run(['git', 'init', '-q', self.target], check=True, capture_output=True)
        self.workflow = os.path.join(self.target, '.github', 'workflows', 'engineering-playbook.yml')

    def _install(self, *flags):
        return subprocess.run(['bash', _INSTALL, *flags, self.target], capture_output=True, text=True)

    def _attribution_script(self) -> str:
        with open(self.workflow) as f:
            text = f.read()
        body = text.split("python3 - <<'EOF'\n", 1)[1].split('\n          EOF', 1)[0]
        return '\n'.join(line[10:] for line in body.split('\n'))

    def _run_check(self, messages: list[str], body: str = '') -> int:
        repo = tempfile.mkdtemp(prefix='pb_agci_repo_')
        self.addCleanup(shutil.rmtree, repo, True)
        git = ['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com']
        subprocess.run(['git', 'init', '-q', repo], check=True, capture_output=True)
        subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', 'base'], check=True)
        base = subprocess.run(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        for message in messages:
            subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', message], check=True)
        head = subprocess.run(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        env = {**os.environ, 'BASE_SHA': base, 'HEAD_SHA': head, 'PR_BODY': body}
        return subprocess.run(['python3', '-c', self._attribution_script()], cwd=repo, env=env,
                              capture_output=True, text=True).returncode

    def test_not_written_without_the_flag(self):
        self._install()
        self.assertFalse(os.path.exists(self.workflow))

    def test_workflow_carries_the_shared_rule(self):
        self.assertEqual(self._install('--ci').returncode, 0)
        sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
        import playbook_core
        with open(self.workflow) as f:
            text = f.read()
        self.assertIn(playbook_core.AI_ATTRIBUTION_RE.pattern, text)
        self.assertNotIn('@@RULE@@', text)
        # Event fields reach the script through env, never interpolated into it.
        self.assertNotIn('${{ github.event.pull_request.body }}\n          python3', text)
        self.assertIn('PR_BODY: ${{ github.event.pull_request.body }}', text)

    def test_an_edited_body_is_checked_again(self):
        """Ultra review of 4.0.0: the default pull_request types leave out
        `edited`, so attribution added to the body after the last push kept
        the earlier green check."""
        self._install('--ci')
        with open(self.workflow) as f:
            text = f.read()
        self.assertIn('    types: [opened, synchronize, reopened, edited]\n', text)

    def test_attribution_in_a_commit_or_the_body_fails_the_check(self):
        self._install('--ci')
        trailer = 'Co-Authored-' + 'By: Gemini <noreply@example.com>'
        self.assertEqual(self._run_check(['feat: clean']), 0)
        self.assertEqual(self._run_check(['feat: clean', f'fix: x\n\n{trailer}']), 1)
        self.assertEqual(self._run_check(['feat: clean'], body=f'Summary\n\n{trailer}'), 1)

    def test_foreign_workflow_is_not_overwritten(self):
        os.makedirs(os.path.dirname(self.workflow))
        with open(self.workflow, 'w') as f:
            f.write('name: theirs\n')
        r = self._install('--ci')
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.workflow) as f:
            self.assertEqual(f.read(), 'name: theirs\n')
        self.assertIn('NOT overwritten', r.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
