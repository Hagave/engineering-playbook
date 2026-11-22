"""
Every real run of a skill's code template is in the scheduled workflow that
runs it against the releases a new project installs today. A template test
left out of the matrix would only run when someone remembers to, and a release
that breaks it would reach users first. The test files also must not write a
version or a registry tag themselves: a pinned run proves an older release, and
a hard-coded `latest` installs a prerelease when the tag names one. They build
package.json through tests/e2e/npm_versions.py, which checks the tag at run
time ("Dependency versions" in engineering-governance).
"""
import glob
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_WORKFLOW = os.path.join(_ROOT, '.github', 'workflows', 'template-latest.yml')
_TEMPLATE_TESTS = sorted(glob.glob(os.path.join(_ROOT, 'tests', 'e2e', 'test_*_template.py')))


def _suite(path: str) -> str:
    return 'tests.e2e.' + os.path.splitext(os.path.basename(path))[0]


class TestTemplateLatestWorkflow(unittest.TestCase):

    def setUp(self) -> None:
        with open(_WORKFLOW, encoding='utf-8') as f:
            self.workflow = f.read()

    def test_there_is_a_template_test(self) -> None:
        self.assertTrue(_TEMPLATE_TESTS)

    def test_every_template_test_is_in_the_matrix(self) -> None:
        listed = set(re.findall(r'^\s*-\s*(tests\.e2e\.test_\w+_template)\s*$', self.workflow, re.MULTILINE))
        self.assertEqual(listed, {_suite(p) for p in _TEMPLATE_TESTS})

    def test_the_workflow_runs_on_a_schedule_and_on_demand(self) -> None:
        self.assertRegex(self.workflow, r'(?m)^\s*schedule:\s*$')
        self.assertRegex(self.workflow, r'(?m)^\s*workflow_dispatch:\s*$')

    def test_template_tests_resolve_the_release_at_run_time(self) -> None:
        for path in _TEMPLATE_TESTS:
            with open(path, encoding='utf-8') as f:
                text = f.read()
            with self.subTest(test=os.path.basename(path)):
                self.assertNotRegex(text, r"""['"](?:[\^~]\d+(?:\.\d+){0,2}|\d+\.\d+\.\d+)['"]""",
                                    'a template test pins a version')
                self.assertNotRegex(text, r"""['"]latest['"]""",
                                    'a template test hard-codes the latest tag; it can name a prerelease')
                self.assertIn('from tests.e2e.npm_versions import package_json', text)


if __name__ == '__main__':
    unittest.main()
