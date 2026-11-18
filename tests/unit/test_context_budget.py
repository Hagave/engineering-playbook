"""
Unit tests for the context token budget (context_guard._check_budget).

A cost advisory: it must reach the model, never block, and not repeat itself —
a notice on every call would be context the next call pays to re-read.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

spec = importlib.util.spec_from_file_location(
    'context_guard', os.path.join(_ROOT, 'hooks', 'context_guard.py'))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402

PREFS = {'context_budget': 100_000}


class TestCheckBudget(unittest.TestCase):

    def setUp(self):
        cg._AGENT_ID = ''
        self.addCleanup(setattr, cg, '_AGENT_ID', '')

    def test_550k_by_default(self):
        defaults = dict(playbook_core.PREFS_DEFAULTS)
        self.assertEqual(defaults['context_budget'], 550_000)
        self.assertIsNone(cg._check_budget(549_999, {}, defaults))
        self.assertIsNotNone(cg._check_budget(550_000, {}, defaults))

    def test_zero_turns_it_off(self):
        prefs = {**playbook_core.PREFS_DEFAULTS, 'context_budget': 0}
        self.assertIsNone(cg._check_budget(900_000, {}, prefs))

    def test_below_budget_is_silent(self):
        self.assertIsNone(cg._check_budget(99_999, {}, PREFS))

    def test_crossing_advises_without_blocking(self):
        result = cg._check_budget(100_000, {}, PREFS)
        self.assertFalse(result['block'])
        self.assertTrue(result['to_model'])
        self.assertIn('playbook:context', result['msg'])
        self.assertIn(cg.HANDOFF_OPEN, result['msg'])
        self.assertIn('/clear', result['msg'])

    def test_speaks_once_per_multiple(self):
        state = {}
        self.assertIsNotNone(cg._check_budget(120_000, state, PREFS))
        self.assertIsNone(cg._check_budget(150_000, state, PREFS))
        self.assertIsNone(cg._check_budget(199_999, state, PREFS))
        again = cg._check_budget(200_000, state, PREFS)
        self.assertIn('(2x)', again['msg'])
        self.assertIsNone(cg._check_budget(250_000, state, PREFS))

    def test_rearms_after_usage_drops(self):
        state = {}
        self.assertIsNotNone(cg._check_budget(120_000, state, PREFS))
        self.assertIsNone(cg._check_budget(40_000, state, PREFS))
        self.assertIsNotNone(cg._check_budget(110_000, state, PREFS))

    def test_unknown_usage_is_silent(self):
        self.assertIsNone(cg._check_budget(None, {}, PREFS))

    def test_subagent_is_exempt(self):
        cg._AGENT_ID = 'agent-1'
        self.assertIsNone(cg._check_budget(500_000, {}, PREFS))

    def test_advisory_is_emitted_as_model_context(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cg._emit_pretooluse(cg._check_budget(100_000, {}, PREFS))
        out = json.loads(buf.getvalue())
        self.assertIn('playbook:context', out['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('permissionDecision', out['hookSpecificOutput'])


class TestBudgetPreference(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _set(self, value):
        return playbook_core.set_pref(self._tmp.name, 'context_budget', value)

    def test_plain_and_suffixed_counts(self):
        for raw, expected in (('250000', 250_000), ('250k', 250_000), ('1.5m', 1_500_000), ('off', 0)):
            with self.subTest(raw=raw):
                _, error = self._set(raw)
                self.assertIsNone(error)
                self.assertEqual(playbook_core.load_prefs(self._tmp.name)['context_budget'], expected)

    def test_garbage_is_refused(self):
        for raw in ('lots', '-1', '70%', ''):
            with self.subTest(raw=raw):
                self.assertIsNotNone(self._set(raw)[1])


if __name__ == '__main__':
    unittest.main()
