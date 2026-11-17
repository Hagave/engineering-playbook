"""
Tests for the secret scanner: the engine (hooks/secret_scan.py), the rule
conversion (scripts/sync_secret_rules.py) and the gates in playbook_core.

Every fake secret is assembled at run time from fragments. The plugin's own
hook scans what is written in this repository, so a literal token in this file
would be denied by the very gate under test — and would be a bad example.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402
import secret_scan  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'sync_secret_rules', os.path.join(_ROOT, 'scripts', 'sync_secret_rules.py'))
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

AWS_KEY = 'AKIA' + 'Z7Q2LMN4PRST6UVW'
GITHUB_PAT = 'ghp' + '_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'
PRIVATE_KEY = (
    '-----BEGIN RSA ' + 'PRIVATE KEY-----\n'
    'MIIEpAIBAAKCAQEA3Tz2mr7SZiAMfQyuvBjM2YsvDfT1nX5kQ8h2b9c7d6e5f4g3h2i1j0k9l8m7n6o5p4q3r2s1t0\n'
    '-----END RSA ' + 'PRIVATE KEY-----'
)
GENERIC_KEY_LINE = 'api' + '_key = "Zx9Kq2Lm8Np4Rt6Vw3Yb"'


def _ids(findings):
    return sorted((f.rule_id, f.tier) for f in findings)


# ─── Rule data and conversion ─────────────────────────────────────────────────

class TestRuleData(unittest.TestCase):

    def setUp(self):
        with open(secret_scan.RULES_PATH, encoding='utf-8') as fh:
            self.data = json.load(fh)

    def test_rules_load(self):
        self.assertTrue(secret_scan.rules_loaded())

    def test_pinned_source_and_notice_ship_together(self):
        source = self.data['source']
        self.assertEqual(source['project'], 'gitleaks')
        self.assertRegex(source['ref'], r'^v\d+\.\d+\.\d+$')
        notice = os.path.join(_ROOT, source['notice'])
        with open(notice, encoding='utf-8') as fh:
            self.assertIn('MIT License', fh.read())

    def test_every_pattern_compiles_without_warnings(self):
        patterns = [r[k] for r in self.data['rules'] for k in ('regex', 'path') if k in r]
        for allow in [self.data['global_allowlist']] + [
                a for r in self.data['rules'] for a in r['allowlists']]:
            patterns += allow['regexes'] + allow['paths']
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            for pattern in patterns:
                re.compile(pattern)

    def test_tiers_are_known_and_all_present(self):
        tiers = {r['tier'] for r in self.data['rules']}
        self.assertEqual(tiers, {'deny', 'warn', 'off'})

    def test_loose_shapes_never_deny(self):
        by_id = {r['id']: r['tier'] for r in self.data['rules']}
        for rule_id in ('generic-api-key', 'jwt', 'curl-auth-header'):
            self.assertEqual(by_id[rule_id], 'warn', rule_id)
        for rule_id in ('aws-access-token', 'github-pat', 'private-key'):
            self.assertEqual(by_id[rule_id], 'deny', rule_id)


class TestRegexConversion(unittest.TestCase):

    def test_mid_pattern_flag_is_scoped_to_its_group(self):
        converted = sync.to_python_regex(r'(a(?i)b)c')
        self.assertEqual(converted, r'(a(?i:b))c')
        self.assertTrue(re.fullmatch(converted, 'aBc'))
        self.assertFalse(re.fullmatch(converted, 'aBC'))

    def test_mid_pattern_flag_at_top_level_runs_to_the_end(self):
        converted = sync.to_python_regex(r'x(?i)yz')
        self.assertTrue(re.fullmatch(converted, 'xYZ'))
        self.assertFalse(re.fullmatch(converted, 'XYZ'))

    def test_leading_flag_is_left_alone(self):
        self.assertEqual(sync.to_python_regex(r'(?i)abc'), r'(?i)abc')

    def test_flag_text_inside_a_class_is_not_a_flag(self):
        self.assertEqual(sync.to_python_regex(r'[(?i)]x'), r'[(?i)]x')

    def test_end_of_text_and_posix_classes(self):
        self.assertEqual(sync.to_python_regex(r'a\z'), r'a\Z')
        self.assertEqual(sync.to_python_regex(r'[[:alnum:]_]'), r'[A-Za-z0-9_]')


# ─── Engine ───────────────────────────────────────────────────────────────────

class TestScanText(unittest.TestCase):

    def test_distinctive_tokens_deny(self):
        self.assertEqual(_ids(secret_scan.scan_text(f'k = "{AWS_KEY}"', 'src/a.ts')),
                         [('aws-access-token', 'deny')])
        self.assertEqual(_ids(secret_scan.scan_text(f'token: {GITHUB_PAT}', 'a.yml')),
                         [('github-pat', 'deny')])
        self.assertEqual(_ids(secret_scan.scan_text(PRIVATE_KEY, 'key.txt')),
                         [('private-key', 'deny')])

    def test_a_scan_past_the_hook_deadline_is_not_a_clean_scan(self):
        self.addCleanup(secret_scan.set_deadline, None)
        secret_scan.set_deadline(time.monotonic() - 1)
        self.assertIsNone(secret_scan.scan_text(f'k = "{AWS_KEY}"', 'src/a.ts'))

    def test_only_this_repository_rule_file_is_exempt(self):
        text = f'k = "{AWS_KEY}"'
        self.assertEqual(secret_scan.scan_text(text, 'hooks/data/secret_rules.json'), [])
        self.assertTrue(secret_scan.scan_text(text, 'vendor/x/hooks/data/secret_rules.json'))

    def test_keyword_anchored_value_only_warns(self):
        self.assertEqual(_ids(secret_scan.scan_text(GENERIC_KEY_LINE, 'a.py')),
                         [('generic-api-key', 'warn')])

    def test_denied_value_is_not_repeated_as_a_warning(self):
        found = secret_scan.scan_text(f'api_key = "{AWS_KEY}"', 'a.py')
        self.assertEqual([f.tier for f in found], ['deny'])

    def test_documented_example_value_is_allowed_by_gitleaks_stopwords(self):
        example = 'AKIA' + 'IOSFODNN7' + 'EXAMPLE'
        self.assertEqual(secret_scan.scan_text(f'k = "{example}"', 'a.py'), [])

    def test_plain_text_is_clean(self):
        self.assertEqual(secret_scan.scan_text('const answer = 42;', 'a.ts'), [])

    def test_lockfiles_are_skipped_by_path(self):
        self.assertEqual(secret_scan.scan_text(f'"{AWS_KEY}"', 'package-lock.json'), [])

    def test_the_rule_file_itself_is_skipped(self):
        with open(secret_scan.RULES_PATH, encoding='utf-8') as fh:
            text = fh.read()
        self.assertEqual(secret_scan.scan_text(text, 'hooks/data/secret_rules.json'), [])

    def test_oversized_text_is_reported_as_not_scanned(self):
        self.assertIsNone(secret_scan.scan_text('x' * (secret_scan.MAX_SCAN_CHARS + 1)))

    def test_exempt_path_is_skipped_before_the_size_cap(self):
        # A regenerated lockfile past the cap made every push "too large to scan".
        big = 'x' * (secret_scan.MAX_SCAN_CHARS + 1)
        self.assertEqual(secret_scan.scan_text(big, 'package-lock.json'), [])
        self.assertIsNone(secret_scan.scan_text(big, 'src/a.py'))

    def test_finding_never_exposes_the_value(self):
        finding = secret_scan.scan_text(f'"{GITHUB_PAT}"', 'a.ts')[0]
        self.assertNotIn(GITHUB_PAT, finding.redacted)
        self.assertNotIn(GITHUB_PAT, finding.fingerprint)
        self.assertRegex(finding.fingerprint, r'^github-pat:[0-9a-f]{16}$')

    def test_fingerprint_is_stable_across_paths(self):
        a = secret_scan.scan_text(f'"{AWS_KEY}"', 'a.ts')[0]
        b = secret_scan.scan_text(f'\n\nx = "{AWS_KEY}"', 'other/b.py')[0]
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_typical_source_file_scans_fast(self):
        with open(os.path.join(_ROOT, 'hooks', 'playbook_core.py'), encoding='utf-8') as fh:
            text = fh.read()
        start = time.perf_counter()
        secret_scan.scan_text(text, 'hooks/playbook_core.py')
        self.assertLess(time.perf_counter() - start, 1.0)


class TestAddedLines(unittest.TestCase):

    def test_only_new_lines_are_returned(self):
        self.assertEqual(secret_scan.added_lines('a\nb', 'a\nb\nc'), 'c')

    def test_empty_old_returns_everything(self):
        self.assertEqual(secret_scan.added_lines('', 'a\nb'), 'a\nb')


class TestScanPatch(unittest.TestCase):

    def test_added_lines_carry_path_and_commit(self):
        patch = (
            'commit abc1234\n'
            'diff --git a/src/a.ts b/src/a.ts\n'
            '--- a/src/a.ts\n'
            '+++ b/src/a.ts\n'
            '@@ -0,0 +1 @@\n'
            f'+const k = "{AWS_KEY}";\n'
        )
        [finding] = secret_scan.scan_patch(patch)
        self.assertEqual((finding.path, finding.commit), ('src/a.ts', 'abc1234'))

    def test_removed_lines_are_ignored(self):
        patch = ('diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +0,0 @@\n'
                 f'-const k = "{AWS_KEY}";\n')
        self.assertEqual(secret_scan.scan_patch(patch), [])

    def test_added_line_starting_with_plus_plus_is_content(self):
        patch = ('diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n'
                 f'+++ "{AWS_KEY}"\n')
        self.assertEqual(len(secret_scan.scan_patch(patch)), 1)

    def test_second_commit_is_attributed_separately(self):
        patch = (
            'commit 1111111\n\ndiff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+clean\n'
            'commit 2222222\n\ndiff --git a/b b/b\n--- a/b\n+++ b/b\n@@ -0,0 +1 @@\n'
            f'+"{GITHUB_PAT}"\n'
        )
        [finding] = secret_scan.scan_patch(patch)
        self.assertEqual((finding.commit, finding.path), ('2222222', 'b'))


class TestAllowFileAndRedaction(unittest.TestCase):

    def test_allow_file_lines_and_comments(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        os.makedirs(os.path.join(root, '.playbook'))
        with open(os.path.join(root, secret_scan.ALLOW_FILE), 'w') as fh:
            fh.write('# fixtures\naws-access-token:0123456789abcdef  # test double\n\n')
        self.assertEqual(secret_scan.load_allowed(root),
                         frozenset({'aws-access-token:0123456789abcdef'}))

    def test_missing_allow_file_allows_nothing(self):
        self.assertEqual(secret_scan.load_allowed(tempfile.gettempdir() + '/nope'), frozenset())

    def test_scrub_secrets_redacts_a_bare_token(self):
        scrubbed = playbook_core.scrub_secrets(f'curl failed for {GITHUB_PAT} here')
        self.assertNotIn(GITHUB_PAT, scrubbed)
        self.assertIn('ghp_***', scrubbed)

    def test_scrub_secrets_keeps_the_keyword_pass(self):
        self.assertEqual(playbook_core.scrub_secrets('password=hunter2'), 'password=***')


# ─── Gates ────────────────────────────────────────────────────────────────────

class _RepoCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.repo = os.path.join(self.dir, 'repo')
        self.remote = os.path.join(self.dir, 'remote.git')
        subprocess.run(('git', 'init', '-q', '--bare', self.remote), capture_output=True)
        subprocess.run(('git', 'init', '-q', '-b', 'main', self.repo), capture_output=True)
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'test')
        self.write('.gitignore', '.env\n')
        self.write('README.md', 'first\n')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'first')
        self.git('remote', 'add', 'origin', self.remote)
        self.git('push', '-q', '-u', 'origin', 'main')

    def git(self, *args):
        return subprocess.run(('git', '-C', self.repo) + args, capture_output=True, text=True)

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(text)
        return path

    def allow(self, fingerprint):
        self.write(secret_scan.ALLOW_FILE, fingerprint + '\n')

    def fingerprint(self, text):
        return secret_scan.scan_text(text)[0].fingerprint


class TestSecretWrite(_RepoCase):

    def test_write_of_a_tracked_file_is_denied_without_echoing_the_value(self):
        result = playbook_core.check_secret_write(
            'Write', {'file_path': 'src/config.ts', 'content': f'export const k = "{AWS_KEY}";\n'},
            self.repo)
        self.assertTrue(result['block'])
        self.assertNotIn(AWS_KEY, result['msg'])
        self.assertIn(self.fingerprint(AWS_KEY), result['msg'])

    def test_a_checkout_under_a_vendored_path_is_still_checked(self):
        """gitleaks' path allowlists are unanchored: the absolute path of a repo
        under `node_modules/` matched them and the pre-check returned clean."""
        base = os.path.join(self.dir, 'node_modules')
        os.makedirs(base)
        shutil.move(self.repo, os.path.join(base, 'repo'))
        self.repo = os.path.join(base, 'repo')
        result = playbook_core.check_secret_write(
            'Write', {'file_path': 'src/a.py', 'content': f'K = "{GITHUB_PAT}"\n'}, self.repo)
        self.assertTrue(result['block'])

    def test_ignored_env_file_is_the_recommended_place(self):
        result = playbook_core.check_secret_write(
            'Write', {'file_path': '.env', 'content': f'AWS_KEY={AWS_KEY}\n'}, self.repo)
        self.assertIsNone(result)

    def test_outside_a_repository_is_not_checked(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        result = playbook_core.check_secret_write(
            'Write', {'file_path': os.path.join(outside, 'creds.txt'), 'content': AWS_KEY}, outside)
        self.assertIsNone(result)

    def test_edit_that_keeps_an_existing_value_is_not_denied(self):
        line = f'const k = "{AWS_KEY}";'
        result = playbook_core.check_secret_write(
            'Edit', {'file_path': 'a.ts', 'old_string': line, 'new_string': line + '\nconst x = 1;'},
            self.repo)
        self.assertIsNone(result)

    def test_edit_that_adds_a_value_is_denied(self):
        result = playbook_core.check_secret_write(
            'Edit', {'file_path': 'a.ts', 'old_string': 'x', 'new_string': f'x\n"{GITHUB_PAT}"'},
            self.repo)
        self.assertTrue(result['block'])

    def test_multiedit_is_checked_per_edit(self):
        edits = [{'old_string': 'a', 'new_string': 'b'},
                 {'old_string': 'c', 'new_string': f'"{GITHUB_PAT}"'}]
        result = playbook_core.check_secret_write(
            'MultiEdit', {'file_path': 'a.ts', 'edits': edits}, self.repo)
        self.assertTrue(result['block'])

    def test_rewrite_of_a_file_already_holding_the_value_is_not_denied(self):
        self.write('a.ts', f'const k = "{AWS_KEY}";\n')
        result = playbook_core.check_secret_write(
            'Write', {'file_path': 'a.ts', 'content': f'const k = "{AWS_KEY}";\nconst y = 2;\n'},
            self.repo)
        self.assertIsNone(result)

    def test_possible_secret_is_advisory(self):
        result = playbook_core.check_secret_write(
            'Write', {'file_path': 'a.py', 'content': GENERIC_KEY_LINE}, self.repo)
        self.assertFalse(result['block'])
        self.assertTrue(result['to_model'])

    def test_user_allowed_fingerprint_passes(self):
        self.allow(self.fingerprint(AWS_KEY))
        result = playbook_core.check_secret_write(
            'Write', {'file_path': 'test/fixture.ts', 'content': f'"{AWS_KEY}"'}, self.repo)
        self.assertIsNone(result)

    def test_model_cannot_write_the_allow_file(self):
        for tool, tool_input in (
                ('Write', {'file_path': secret_scan.ALLOW_FILE, 'content': 'x'}),
                ('Edit', {'file_path': secret_scan.ALLOW_FILE, 'old_string': '', 'new_string': 'x'})):
            result = playbook_core.check_secret_write(tool, tool_input, self.repo)
            self.assertTrue(result['block'], tool)

    def test_other_tools_are_not_checked(self):
        self.assertIsNone(playbook_core.check_secret_write('Read', {'file_path': 'a.ts'}, self.repo))


class TestSecretBash(_RepoCase):

    def test_heredoc_into_a_tracked_file_is_denied(self):
        command = f"cat > src/config.py <<'EOF'\nKEY = \"{GITHUB_PAT}\"\nEOF"
        self.assertTrue(playbook_core.check_secret_bash(command, self.repo)['block'])

    def test_redirect_into_an_ignored_file_passes(self):
        command = f'echo "AWS_KEY={AWS_KEY}" >> .env'
        self.assertIsNone(playbook_core.check_secret_bash(command, self.repo))

    def test_unresolvable_target_is_checked(self):
        command = f'echo "{AWS_KEY}" > "$OUT"'
        self.assertTrue(playbook_core.check_secret_bash(command, self.repo)['block'])

    def test_output_discarded_to_dev_null_passes(self):
        command = f'curl -s -H "Authorization: token {GITHUB_PAT}" https://example.invalid > /dev/null'
        self.assertIsNone(playbook_core.check_secret_bash(command, self.repo))

    def test_in_place_sed_is_checked_even_with_a_harmless_redirect(self):
        command = f"sed -i 's/x/{GITHUB_PAT}/' src/a.py 2>/dev/null > /dev/null"
        self.assertTrue(playbook_core.check_secret_bash(command, self.repo)['block'])

    def test_in_place_sed_without_a_redirect_is_checked(self):
        for command in (f"sed -i 's/x/{GITHUB_PAT}/' src/a.py",
                        f"sed --in-place 's/x/{GITHUB_PAT}/' src/a.py",
                        f"bash -c \"sed -i 's/x/{GITHUB_PAT}/' src/a.py\"",
                        f"eval \"sed -i 's/x/{GITHUB_PAT}/' src/a.py\"",
                        f"python3 -c \"import os; os.system('sed -i s/x/{GITHUB_PAT}/ src/a.py')\"",
                        # A shell run as an argument, not as the command word.
                        f"find src -name a.py -exec sh -c 'sed -i \"s/x/{GITHUB_PAT}/\" \"$1\"' _ {{}} \\;",
                        f"docker exec ctr sh -c \"sed -i 's/x/{GITHUB_PAT}/' /app/a.py\"",
                        # A nested script that does not tokenize is read as text.
                        f"bash -c \"sed -i 's/x/{GITHUB_PAT}/' src/a.py; echo it's\""):
            self.assertTrue(playbook_core.check_secret_bash(command, self.repo)['block'], command)

    def test_a_later_grep_flag_is_not_an_in_place_sed(self):
        # The -i belongs to grep, a later command; nothing is written.
        command = f'sed -n 1,9p src/a.py; grep -i "{GITHUB_PAT}" src/b.py'
        self.assertIsNone(playbook_core.check_secret_bash(command, self.repo))

    def test_command_that_writes_nothing_passes(self):
        command = f'curl -s -H "Authorization: token {GITHUB_PAT}" https://example.invalid'
        self.assertIsNone(playbook_core.check_secret_bash(command, self.repo))

    def test_stderr_redirects_write_nothing(self):
        command = f'curl -s -H "Authorization: token {GITHUB_PAT}" https://example.invalid'
        for redirect in (' 2>&1', ' 2>/dev/null', ' >&2'):
            with self.subTest(redirect=redirect):
                self.assertIsNone(playbook_core.check_secret_bash(command + redirect, self.repo))
        self.assertTrue(playbook_core.check_secret_bash(
            f'echo "{GITHUB_PAT}" > src/a.py 2>&1', self.repo)['block'])

    def test_codex_apply_patch_adding_a_secret_is_denied(self):
        patch = ('*** Begin Patch\n*** Update File: src/a.py\n@@\n-x = 1\n'
                 f'+token = "{GITHUB_PAT}"\n*** End Patch')
        result = playbook_core.check_secret_write('ApplyPatch', {'input': patch}, self.repo)
        self.assertTrue(result['block'])
        clean = patch.replace(GITHUB_PAT, 'nothing')
        self.assertIsNone(playbook_core.check_secret_write('ApplyPatch', {'input': clean}, self.repo))

    def test_changing_the_allow_file_is_denied(self):
        for command in (f'echo x >> {secret_scan.ALLOW_FILE}',
                        f"sed -i 's/a/b/' {secret_scan.ALLOW_FILE}",
                        f'cp /tmp/x {secret_scan.ALLOW_FILE}'):
            self.assertTrue(playbook_core.check_secret_bash(command, self.repo)['block'], command)

    def test_heredoc_into_the_allow_file_is_denied(self):
        command = f"cat > {secret_scan.ALLOW_FILE} <<'EOF'\naws-access-token:0123456789abcdef\nEOF"
        self.assertTrue(playbook_core.check_secret_bash(command, self.repo)['block'])

    def test_interpreter_writing_the_allow_file_is_denied(self):
        command = f"python3 -c \"open('{secret_scan.ALLOW_FILE}', 'a').write('x')\""
        self.assertTrue(playbook_core.check_secret_bash(command, self.repo)['block'])

    def test_message_that_names_the_allow_file_passes(self):
        command = (f"cat > /tmp/msg.txt <<'EOF'\nExceptions live in {secret_scan.ALLOW_FILE}.\nEOF\n"
                   'git commit -F /tmp/msg.txt')
        self.assertIsNone(playbook_core.check_secret_bash(command, self.repo))

    def test_reading_the_allow_file_passes(self):
        self.assertIsNone(playbook_core.check_secret_bash(
            f'cat {secret_scan.ALLOW_FILE}', self.repo))


class TestCommitSecrets(_RepoCase):

    def test_staged_secret_denies_the_commit(self):
        self.write('src/a.ts', f'const k = "{AWS_KEY}";\n')
        self.git('add', 'src/a.ts')
        result = playbook_core.check_commit_secrets('git commit -m "add config"', self.repo)
        self.assertTrue(result['block'])
        self.assertIn('src/a.ts', result['msg'])

    def test_unstaged_tracked_change_counts_with_all(self):
        self.write('README.md', f'first\n"{GITHUB_PAT}"\n')
        self.assertIsNone(playbook_core.check_commit_secrets('git commit -m x', self.repo))
        self.assertTrue(playbook_core.check_commit_secrets('git commit -am x', self.repo)['block'])

    def test_add_in_the_same_command_counts(self):
        self.write('README.md', f'first\n"{GITHUB_PAT}"\n')
        result = playbook_core.check_commit_secrets('git add README.md && git commit -m x',
                                                    self.repo)
        self.assertTrue(result['block'])

    def test_commit_tree_is_not_a_commit_of_the_index(self):
        self.write('src/a.ts', f'const k = "{AWS_KEY}";\n')
        self.git('add', 'src/a.ts')
        self.assertIsNone(playbook_core.check_commit_secrets('git commit-tree HEAD^{tree} -m x',
                                                             self.repo))

    def test_message_value_is_not_read_as_the_all_flag(self):
        self.assertFalse(playbook_core._commit_stages_all(['-mfix all']))
        self.assertTrue(playbook_core._commit_stages_all(['-am', 'x']))

    def test_clean_commit_passes(self):
        self.write('src/a.ts', 'const k = 1;\n')
        self.git('add', 'src/a.ts')
        self.assertIsNone(playbook_core.check_commit_secrets('git commit -m x', self.repo))

    def test_allowed_fingerprint_passes(self):
        self.write('test/fixture.ts', f'"{AWS_KEY}"\n')
        self.git('add', 'test/fixture.ts')
        self.allow(self.fingerprint(AWS_KEY))
        self.assertIsNone(playbook_core.check_commit_secrets('git commit -m x', self.repo))


class TestPushSecrets(_RepoCase):

    def commit(self, name, text):
        self.write(name, text)
        self.git('add', name)
        self.git('commit', '-q', '-m', f'add {name}')

    def test_unpublished_commit_with_a_secret_denies_the_push(self):
        self.commit('src/a.ts', f'const k = "{AWS_KEY}";\n')
        self.commit('src/b.ts', 'const ok = 1;\n')
        result = playbook_core.check_push_secrets('git push origin main', self.repo)
        self.assertTrue(result['block'])
        self.assertIn('src/a.ts', result['msg'])
        self.assertNotIn(AWS_KEY, result['msg'])

    def test_secret_added_and_removed_before_the_push_still_denies(self):
        self.commit('src/a.ts', f'const k = "{AWS_KEY}";\n')
        self.commit('src/a.ts', 'const k = process.env.K;\n')
        self.assertTrue(playbook_core.check_push_secrets('git push', self.repo)['block'])

    def test_clean_push_passes(self):
        self.commit('src/a.ts', 'const ok = 1;\n')
        self.assertIsNone(playbook_core.check_push_secrets('git push origin main', self.repo))

    def test_dry_run_passes(self):
        self.commit('src/a.ts', f'"{AWS_KEY}"\n')
        self.assertIsNone(playbook_core.check_push_secrets('git push --dry-run', self.repo))

    def test_runtime_refspec_fails_closed(self):
        result = playbook_core.check_push_secrets('git push origin "$BRANCH"', self.repo)
        self.assertTrue(result['block'])
        self.assertIn('built at run time', result['msg'])

    def test_missing_rules_fail_closed(self):
        self.commit('src/a.ts', 'const ok = 1;\n')
        original = secret_scan.rules_loaded
        self.addCleanup(setattr, secret_scan, 'rules_loaded', original)
        secret_scan.rules_loaded = lambda: False
        result = playbook_core.check_push_secrets('git push origin main', self.repo)
        self.assertTrue(result['block'])
        self.assertIn('rules file', result['msg'])

    def test_push_run_by_another_program_fails_closed(self):
        self.commit('src/a.ts', 'const ok = 1;\n')
        for command in (r'find . -maxdepth 0 -execdir git push origin main \;',
                        'flock /tmp/l git push origin main'):
            with self.subTest(command=command):
                result = playbook_core.check_push_secrets(command, self.repo)
                self.assertTrue(result['block'])
                self.assertIn('argument', result['msg'])

    def test_already_published_history_is_not_rescanned(self):
        self.commit('src/a.ts', f'"{AWS_KEY}"\n')
        self.git('push', '-q', 'origin', 'main')
        self.commit('src/b.ts', 'const ok = 1;\n')
        self.assertIsNone(playbook_core.check_push_secrets('git push origin main', self.repo))


if __name__ == '__main__':
    unittest.main()
