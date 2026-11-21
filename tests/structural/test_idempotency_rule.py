"""
The idempotency rule, in engineering-concurrency and everywhere else it is
repeated. The old rule kept the key and the response in Redis and wrote the
response after the database commit: a failure between the two left the
operation with no record (or a key stuck "processing"), and expiry reopened
it. These tests hold the rule that replaced it, and keep every other skill
from sending a database operation's idempotency record back to a cache.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS = os.path.join(_ROOT, 'skills')
_FENCE_RE = re.compile(r'^```[^\n]*\n(.*?)^```', re.DOTALL | re.MULTILINE)


def _read(rel: str) -> str:
    with open(os.path.join(_SKILLS, rel), encoding='utf-8') as f:
        return f.read()


def _section(text: str, heading: str) -> str:
    match = re.search(rf'^## {re.escape(heading)}\n(.*?)(?=^## |\Z)', text, re.DOTALL | re.MULTILINE)
    if not match:
        raise AssertionError(f'section "## {heading}" not found')
    return match.group(1)


class TestIdempotencyRule(unittest.TestCase):

    def setUp(self) -> None:
        self.section = _section(_read('engineering-concurrency/SKILL.md'), 'Idempotency')

    def test_the_record_shares_the_transaction_of_the_effect(self) -> None:
        self.assertIn('same transaction as the effect', self.section)
        self.assertIn('unique index on (caller, key)', self.section)

    def test_the_key_is_scoped_to_the_caller_and_bound_to_the_request(self) -> None:
        self.assertIn('The key belongs to the caller', self.section)
        self.assertIn('fingerprint', self.section)
        self.assertIn('`422`', self.section)
        self.assertIn('`409`', self.section)

    def test_the_database_pseudocode_uses_no_second_store(self) -> None:
        blocks = _FENCE_RE.findall(self.section)
        self.assertEqual(len(blocks), 1)
        code = blocks[0]
        self.assertIn('transaction:', code)
        self.assertIn('store_response', code)
        self.assertNotRegex(code.lower(), r'redis|cache')
        self.assertLess(code.index('store_response'), code.index('commit'),
                        'the response is stored before the commit, in the same transaction')

    def test_a_shared_store_is_only_for_operations_without_a_database_effect(self) -> None:
        self.assertIn('An operation with no database effect', self.section)
        self.assertIn('never a read followed by a write', self.section)

    def test_review_checks_the_rule_not_the_old_redis_pattern(self) -> None:
        review = _read('engineering-code-review/SKILL.md')
        self.assertNotIn('the correct pattern is `SET NX`', review)
        self.assertIn('outside the transaction of the effect', review)

    def test_the_fiche_proves_scope_fingerprint_and_rollback(self) -> None:
        fiche = _read('engineering-security/references/business-logic.md')
        for row in ('the same idempotency key sent by another user',
                    'the same idempotency key with a different body',
                    'the write and the key roll back together'):
            with self.subTest(row=row):
                self.assertIn(row, fiche)

    def test_no_skill_puts_a_database_operations_key_in_a_cache(self) -> None:
        """Any sentence that ties idempotency to Redis or a cache must limit it to
        operations without a database effect, or forbid it."""
        linked = re.compile(r'idempot[^.\n]*(?:redis|cache\b|\bset nx\b)|(?:redis|cache\b|\bset nx\b)[^.\n]*idempot')
        allowed = re.compile(r'no database effect|never a Redis key|Never keep it in a second store|'
                             r'second store for an operation that writes|outside the transaction',
                             re.IGNORECASE)
        offenders = []
        for path in sorted(glob.glob(os.path.join(_SKILLS, '**', '*.md'), recursive=True)):
            with open(path, encoding='utf-8') as f:
                for number, line in enumerate(f, 1):
                    if linked.search(line.lower()) and not allowed.search(line):
                        offenders.append(f'{os.path.relpath(path, _SKILLS)}:{number}: {line.strip()[:120]}')
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
