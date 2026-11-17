"""
Unit tests for the CLAUDE.md budget gate (context_guard) and its preference.

The gate judges growth past the budget, not size: an oversized file must stay
editable, and shrinking it must never be punished.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

spec = importlib.util.spec_from_file_location(
    'context_guard', os.path.join(_ROOT, 'hooks', 'context_guard.py'))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

sys.path.insert(0, os.path.join(_ROOT, 'hooks'))
import playbook_core  # noqa: E402

spec = importlib.util.spec_from_file_location(
    'session_start', os.path.join(_ROOT, 'hooks', 'session_start.py'))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


class _Project(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'CLAUDE.md')

    def write(self, text: str) -> None:
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text)

    def edit(self, state: dict, tool: str = 'Edit') -> None:
        cg._note_memory_write(state, tool, {'file_path': self.path})


class TestMemoryBudgetGate(_Project):

    PREFS = {'claude_md_budget': 100}

    def test_growth_past_the_budget_blocks(self):
        self.write('x' * 50)
        state = {}
        self.edit(state)
        self.write('x' * 150)
        result = cg._check_memory_budget(state, self.PREFS)
        self.assertTrue(result['block'])
        self.assertIn('playbook:memory', result['msg'])
        self.assertIn('150', result['msg'])

    def test_growth_within_the_budget_passes(self):
        self.write('x' * 50)
        state = {}
        self.edit(state)
        self.write('x' * 90)
        self.assertIsNone(cg._check_memory_budget(state, self.PREFS))

    def test_shrinking_an_oversized_file_passes(self):
        """The fix must not trip the gate it is fixing."""
        self.write('x' * 500)
        state = {}
        self.edit(state)
        self.write('x' * 300)
        self.assertIsNone(cg._check_memory_budget(state, self.PREFS))

    def test_oversized_file_left_the_same_size_passes(self):
        self.write('x' * 500)
        state = {}
        self.edit(state)
        self.assertIsNone(cg._check_memory_budget(state, self.PREFS))

    def test_file_created_over_budget_blocks(self):
        state = {}
        self.edit(state, 'Write')
        self.write('x' * 150)
        self.assertTrue(cg._check_memory_budget(state, self.PREFS)['block'])

    def test_before_size_is_the_first_one_seen_this_turn(self):
        """Two edits that each add a little must be judged as one growth."""
        self.write('x' * 90)
        state = {}
        self.edit(state)
        self.write('x' * 110)
        self.edit(state)
        self.write('x' * 130)
        self.assertIn('from 90 to 130', cg._check_memory_budget(state, self.PREFS)['msg'])

    def test_budget_zero_turns_the_gate_off(self):
        self.write('x' * 50)
        state = {}
        self.edit(state)
        self.write('x' * 5000)
        self.assertIsNone(cg._check_memory_budget(state, {'claude_md_budget': 0}))

    def test_verdict_is_consumed(self):
        self.write('x' * 50)
        state = {}
        self.edit(state)
        self.write('x' * 150)
        self.assertIsNotNone(cg._check_memory_budget(state, self.PREFS))
        self.assertIsNone(cg._check_memory_budget(state, self.PREFS))

    def test_other_files_and_tools_are_not_tracked(self):
        state = {}
        cg._note_memory_write(state, 'Edit', {'file_path': '/repo/README.md'})
        cg._note_memory_write(state, 'Read', {'file_path': self.path})
        cg._note_memory_write(state, 'Bash', {'command': 'cat CLAUDE.md'})
        self.assertNotIn('memory_before', state)

    def test_long_live_rules_are_pointed_at(self):
        self.write('x' * 10)
        state = {}
        self.edit(state)
        self.write('<!-- engineering-learn:live -->\n- %s\n- short\n'
                   '<!-- /engineering-learn:live -->\n' % ('y' * (cg.LIVE_RULE_MAX_CHARS + 1)))
        msg = cg._check_memory_budget(state, self.PREFS)['msg']
        self.assertIn('1 rule(s)', msg)

    def test_unreadable_file_gives_no_verdict(self):
        state = {'memory_before': {os.path.join(self._tmp.name, 'gone', 'CLAUDE.md'): 0}}
        self.assertIsNone(cg._check_memory_budget(state, self.PREFS))


class TestBudgetPreference(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def test_default_is_on(self):
        self.assertGreater(playbook_core.load_prefs(self.dir)['claude_md_budget'], 0)

    def test_set_and_reload(self):
        _, error = playbook_core.set_pref(self.dir, 'claude_md_budget', '25000')
        self.assertIsNone(error)
        self.assertEqual(playbook_core.load_prefs(self.dir)['claude_md_budget'], 25000)

    def test_off_means_zero(self):
        _, error = playbook_core.set_pref(self.dir, 'claude_md_budget', 'off')
        self.assertIsNone(error)
        self.assertEqual(playbook_core.load_prefs(self.dir)['claude_md_budget'], 0)

    def test_garbage_is_refused_and_not_stored(self):
        _, error = playbook_core.set_pref(self.dir, 'claude_md_budget', '-5')
        self.assertIsNotNone(error)
        self.assertEqual(playbook_core.load_prefs(self.dir)['claude_md_budget'],
                         playbook_core.PREFS_DEFAULTS['claude_md_budget'])

    def test_hand_edited_bool_degrades_to_default(self):
        with open(playbook_core.prefs_path(self.dir), 'w') as f:
            f.write('{"claude_md_budget": true}')
        self.assertEqual(playbook_core.load_prefs(self.dir)['claude_md_budget'],
                         playbook_core.PREFS_DEFAULTS['claude_md_budget'])


class TestSessionStartNotice(_Project):

    def _notice(self, budget: int) -> str:
        prefs_dir = tempfile.mkdtemp(dir=self._tmp.name)
        playbook_core.set_pref(prefs_dir, 'claude_md_budget', str(budget))
        original = ss.STATE_DIR
        ss.STATE_DIR = prefs_dir
        try:
            return ss._memory_notice(self._tmp.name)
        finally:
            ss.STATE_DIR = original

    def test_over_budget_is_announced(self):
        self.write('x' * 200)
        self.assertIn('playbook:memory', self._notice(100))

    def test_within_budget_is_silent(self):
        self.write('x' * 50)
        self.assertEqual(self._notice(100), '')

    def test_no_file_is_silent(self):
        self.assertEqual(self._notice(100), '')

    def test_off_is_silent(self):
        self.write('x' * 200)
        self.assertEqual(self._notice(0), '')


if __name__ == '__main__':
    unittest.main()
