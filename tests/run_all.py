#!/usr/bin/env python3
"""
Test runner for the Engineering Playbook.

Discovers and runs:
  unit/       — context_guard.py function-level tests (no subprocess)
  structural/ — SKILL.md file structure and content validation
  e2e/        — context_guard.py end-to-end subprocess tests

Usage:
  python3 tests/run_all.py           # run everything
  python3 tests/run_all.py unit      # run only unit tests
  python3 tests/run_all.py structural
  python3 tests/run_all.py e2e

Behavioral tests (tests/behavioral/) require ANTHROPIC_API_KEY and the claude
CLI. Run them separately with: bash tests/behavioral/run_behavioral.sh
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.join(_ROOT, 'tests')

SUITES = ['unit', 'structural', 'e2e']


def load_suite(name: str) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    start = os.path.join(_TESTS, name)
    return loader.discover(start, pattern='test_*.py', top_level_dir=_ROOT)


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else None

    if target and target not in SUITES:
        print(f'Unknown suite: {target!r}. Choose from: {", ".join(SUITES)}', file=sys.stderr)
        return 1

    suites_to_run = [target] if target else SUITES
    combined = unittest.TestSuite()
    for name in suites_to_run:
        combined.addTests(load_suite(name))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(combined)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
