"""
Structural tests for the repository protection: the rulesets in
.github/rulesets/ must name status checks that the workflows actually
produce, keep the guarantees the owner asked for, and the apply script must
update a ruleset that exists instead of creating a duplicate.

A required check that no workflow runs blocks every merge, and GitHub does
not warn about it: the job names and the ruleset contexts must stay in step.
"""
import importlib.util
import json
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RULESETS = os.path.join(_ROOT, '.github', 'rulesets')
_WORKFLOWS = os.path.join(_ROOT, '.github', 'workflows')
_spec = importlib.util.spec_from_file_location(
    'apply_repo_protection', os.path.join(_ROOT, 'scripts', 'apply_repo_protection.py'))
protection = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protection)

GITHUB_ACTIONS_APP = 15368


def _job_names() -> set[str]:
    names = set()
    for name in os.listdir(_WORKFLOWS):
        with open(os.path.join(_WORKFLOWS, name), encoding='utf-8') as fh:
            text = fh.read()
        jobs = text.split('\njobs:\n', 1)[-1]
        names |= set(re.findall(r'^  ([a-z][\w-]*):\s*$', jobs, re.MULTILINE))
    return names


def _rulesets() -> dict[str, dict]:
    return {r['name']: r for r in protection.load_rulesets()}


def _rules(ruleset: dict) -> dict[str, dict]:
    return {rule['type']: rule.get('parameters', {}) for rule in ruleset['rules']}


class TestRulesets(unittest.TestCase):

    def test_required_checks_are_jobs_that_exist_and_run_on_actions(self):
        jobs = _job_names()
        for ruleset in _rulesets().values():
            for check in _rules(ruleset).get('required_status_checks', {}).get('required_status_checks', []):
                with self.subTest(ruleset=ruleset['name'], check=check['context']):
                    self.assertIn(check['context'], jobs)
                    # Bound to GitHub Actions: a commit status posted by anyone
                    # with write access under the same name does not count.
                    self.assertEqual(check.get('integration_id'), GITHUB_ACTIONS_APP)

    def test_develop_and_main_cannot_be_force_pushed_or_deleted_by_anyone(self):
        protected = [r for r in _rulesets().values()
                     if r['target'] == 'branch' and {'deletion', 'non_fast_forward'} <= set(_rules(r))]
        self.assertTrue(protected)
        for ruleset in protected:
            self.assertEqual(ruleset['bypass_actors'], [])
            self.assertEqual(set(ruleset['conditions']['ref_name']['include']),
                             {'refs/heads/develop', 'refs/heads/main'})

    def test_others_need_the_owners_review_and_the_owner_merges_through_a_pull_request(self):
        review = [r for r in _rulesets().values() if 'pull_request' in _rules(r)]
        self.assertEqual(len(review), 1)
        parameters = _rules(review[0])['pull_request']
        self.assertGreaterEqual(parameters['required_approving_review_count'], 1)
        self.assertTrue(parameters['require_code_owner_review'])
        # The admin may skip the review, but only through a pull request:
        # `pull_request` mode, never `always`, so a direct push stays refused.
        self.assertEqual(review[0]['bypass_actors'],
                         [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'pull_request'}])
        with open(os.path.join(_ROOT, '.github', 'CODEOWNERS'), encoding='utf-8') as fh:
            self.assertIn('* @Hagave', fh.read())

    def test_release_tags_cannot_be_moved_or_deleted(self):
        tags = [r for r in _rulesets().values() if r['target'] == 'tag']
        self.assertEqual(len(tags), 1)
        self.assertTrue({'update', 'deletion', 'non_fast_forward'} <= set(_rules(tags[0])))

    def test_the_release_workflow_needs_no_bypass_to_create_its_tag(self):
        """Ultra review of 4.0.0: a `creation` rule needed the GitHub Actions
        app (15368) as a bypass actor for release.yml's GITHUB_TOKEN, which
        GitHub does not document; refused, it broke every release, or aborted
        apply_repo_protection.py before the repository settings."""
        tags = [r for r in _rulesets().values() if r['target'] == 'tag'][0]
        self.assertNotIn('creation', _rules(tags))
        self.assertEqual(tags['bypass_actors'],
                         [{'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'}])


class TestApplyPlan(unittest.TestCase):

    def test_existing_rulesets_are_updated_not_duplicated(self):
        rulesets = protection.load_rulesets()
        first = rulesets[0]['name']
        steps = protection.plan('o/r', rulesets, {first: 42})
        calls = {description: call for description, call, _ in steps}
        self.assertEqual(calls[f'update ruleset "{first}"'], ['-X', 'PUT', 'repos/o/r/rulesets/42'])
        creates = [d for d in calls if d.startswith('create ruleset')]
        self.assertEqual(len(creates), len(rulesets) - 1)

    def test_every_ruleset_file_is_valid_json_with_a_name(self):
        for name in os.listdir(_RULESETS):
            with self.subTest(file=name), open(os.path.join(_RULESETS, name), encoding='utf-8') as fh:
                self.assertTrue(json.load(fh)['name'])


if __name__ == '__main__':
    unittest.main()
