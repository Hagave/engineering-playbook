"""
Structural tests for all SKILL.md files in the engineering-playbook.

Validates:
1. Every skill has required frontmatter fields (name, description, version, user-invocable).
2. RULE-ANCHOR blocks are byte-for-byte identical across all skills that have one.
3. OUTPUT-DISCIPLINE blocks are byte-for-byte identical across all skills that have one.
4. The 6 architectural changes from the current session are present and correct.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILLS_DIR = os.path.join(_ROOT, 'skills')


def all_skill_files():
    return sorted(glob.glob(os.path.join(_SKILLS_DIR, '*', 'SKILL.md')))


def skill_name(path):
    return os.path.basename(os.path.dirname(path))


def read_skill(name):
    path = os.path.join(_SKILLS_DIR, name, 'SKILL.md')
    with open(path) as f:
        return f.read()


def parse_frontmatter(content):
    """Extract key-value pairs from the YAML frontmatter block."""
    m = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if not m:
        return {}
    result = {}
    for line in m.group(1).split('\n'):
        if ':' in line:
            key, _, value = line.partition(':')
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def extract_block(content, tag):
    """Return the full <!-- TAG -->...<!-- /TAG --> block, or None if absent."""
    pattern = rf'<!-- {tag} -->(.*?)<!-- /{tag} -->'
    m = re.search(pattern, content, re.DOTALL)
    return m.group(0).strip() if m else None


# ── 1. Frontmatter ────────────────────────────────────────────────────────────

class TestFrontmatter(unittest.TestCase):
    """Every SKILL.md has the four required frontmatter fields."""

    def test_all_skills_have_required_fields(self):
        required = {'name', 'description', 'version', 'user-invocable'}
        for path in all_skill_files():
            with self.subTest(skill=skill_name(path)):
                with open(path) as f:
                    content = f.read()
                fm = parse_frontmatter(content)
                for field in required:
                    self.assertIn(field, fm, f'{skill_name(path)}: missing "{field}"')
                self.assertTrue(
                    len(fm.get('description', '')) > 10,
                    f'{skill_name(path)}: description is empty or too short'
                )

    def test_user_invocable_is_boolean_string(self):
        for path in all_skill_files():
            with self.subTest(skill=skill_name(path)):
                with open(path) as f:
                    content = f.read()
                fm = parse_frontmatter(content)
                val = fm.get('user-invocable', '').lower()
                self.assertIn(
                    val, ('true', 'false'),
                    f'{skill_name(path)}: user-invocable must be "true" or "false", got {val!r}'
                )


# ── 2. RULE-ANCHOR identity ───────────────────────────────────────────────────

class TestRuleAnchorIdentity(unittest.TestCase):
    """All RULE-ANCHOR blocks are byte-for-byte identical."""

    def test_rule_anchor_identical_across_all_skills(self):
        variants: dict[str, str] = {}
        for path in all_skill_files():
            with open(path) as f:
                content = f.read()
            block = extract_block(content, 'RULE-ANCHOR')
            if block:
                variants[skill_name(path)] = block

        self.assertGreater(len(variants), 1, 'Expected multiple skills to have RULE-ANCHOR')

        reference_name, reference_block = next(iter(variants.items()))
        for name, block in variants.items():
            with self.subTest(skill=name):
                self.assertEqual(
                    block, reference_block,
                    f'RULE-ANCHOR in "{name}" differs from "{reference_name}"'
                )

    def test_rule_anchor_contains_non_negotiable(self):
        for path in all_skill_files():
            with open(path) as f:
                content = f.read()
            block = extract_block(content, 'RULE-ANCHOR')
            if block:
                with self.subTest(skill=skill_name(path)):
                    self.assertIn('non-negotiable', block)

    def test_rule_anchor_contains_hypothesis_stop(self):
        for path in all_skill_files():
            with open(path) as f:
                content = f.read()
            block = extract_block(content, 'RULE-ANCHOR')
            if block:
                with self.subTest(skill=skill_name(path)):
                    self.assertIn('hypothesis', block)


# ── 3. OUTPUT-DISCIPLINE identity ─────────────────────────────────────────────

class TestOutputDisciplineIdentity(unittest.TestCase):
    """All OUTPUT-DISCIPLINE blocks are byte-for-byte identical."""

    def test_output_discipline_identical_across_all_skills(self):
        variants: dict[str, str] = {}
        for path in all_skill_files():
            with open(path) as f:
                content = f.read()
            block = extract_block(content, 'OUTPUT-DISCIPLINE')
            if block:
                variants[skill_name(path)] = block

        self.assertGreater(len(variants), 1, 'Expected multiple skills to have OUTPUT-DISCIPLINE')

        reference_name, reference_block = next(iter(variants.items()))
        for name, block in variants.items():
            with self.subTest(skill=name):
                self.assertEqual(
                    block, reference_block,
                    f'OUTPUT-DISCIPLINE in "{name}" differs from "{reference_name}"'
                )

    def test_output_discipline_forbids_preamble(self):
        for path in all_skill_files():
            with open(path) as f:
                content = f.read()
            block = extract_block(content, 'OUTPUT-DISCIPLINE')
            if block:
                with self.subTest(skill=skill_name(path)):
                    self.assertIn('No preamble', block)


# ── 4. engineering-governance changes ─────────────────────────────────────────

class TestGovernanceChanges(unittest.TestCase):
    """engineering-governance has companion skills + spec gate sections."""

    def setUp(self):
        self.content = read_skill('engineering-governance')

    def test_mandatory_companion_skills_section_exists(self):
        self.assertIn('## Mandatory companion skills', self.content)

    def test_companion_skills_are_unconditional(self):
        m = re.search(r'## Mandatory companion skills(.*?)(?=\n## |\Z)', self.content, re.DOTALL)
        self.assertIsNotNone(m, 'Section not found')
        section = m.group(1)
        self.assertIn('unconditional', section)

    def test_companion_skills_load_before_checklist_not_before_write(self):
        m = re.search(r'## Mandatory companion skills(.*?)(?=\n## |\Z)', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        section = m.group(1)
        # Must reference checklist generation (Step 6 after the F2 reorder:
        # ambiguities and reuse search now precede the checklist/STOP)
        self.assertIn('Step 6', section)
        # The old timing instruction must be gone
        self.assertNotIn('before the first Write or Edit call', section)

    def test_all_five_companion_skills_listed(self):
        m = re.search(r'## Mandatory companion skills(.*?)(?=\n## |\Z)', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        section = m.group(1)
        for skill in [
            'engineering-security',
            'engineering-concurrency',
            'engineering-error-handling',
            'engineering-performance',
        ]:
            self.assertIn(skill, section, f'Companion skill missing: {skill}')
        # The language companion is the opt-in of the code written, any language.
        self.assertIn('the language opt-in for the code you write', section)
        for lang_skill in ('engineering-typescript', 'engineering-go', 'engineering-java'):
            self.assertIn(lang_skill, section)

    def test_spec_gate_section_exists(self):
        self.assertIn('## Spec gate', self.content)

    def test_spec_confirmed_token_defined_in_spec_gate(self):
        m = re.search(r'## Spec gate(.*?)(?=\n## |\Z)', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn('[SPEC CONFIRMED]', m.group(1))

    def test_spec_gate_mentions_hook_enforcement(self):
        m = re.search(r'## Spec gate(.*?)(?=\n## |\Z)', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn('hook', m.group(1).lower())

    def test_spec_gate_covers_trivial_single_line_fix(self):
        m = re.search(r'## Spec gate(.*?)(?=\n## |\Z)', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn('single-line fix', m.group(1))


# ── 5. engineering-implement changes ──────────────────────────────────────────

class TestNoCompactRecommendation(unittest.TestCase):
    """/compact does not reliably bring a session back under the context guard's
    threshold, so no skill may schedule or recommend it -- the exit is /clear plus
    the handoff. The only mentions allowed are the prohibitions themselves."""

    _PROHIBITION = re.compile(r'never recommend `/compact`|recommending `/compact`', re.IGNORECASE)

    def test_no_skill_recommends_compact(self):
        for path in all_skill_files():
            with open(path) as f:
                for n, line in enumerate(f, 1):
                    if '/compact' in line and not self._PROHIBITION.search(line):
                        self.fail(f'{skill_name(path)}:{n} mentions /compact: {line.strip()[:120]}')


class TestImplementChanges(unittest.TestCase):
    """engineering-implement emits [SPEC CONFIRMED] at the STOP step."""

    def setUp(self):
        self.content = read_skill('engineering-implement')

    def test_stop_section_mentions_spec_confirmed(self):
        m = re.search(r'## STOP.*?## Step 7', self.content, re.DOTALL)
        self.assertIsNotNone(m, 'STOP section not found between ## STOP and ## Step 7')
        self.assertIn('[SPEC CONFIRMED]', m.group(0))

    def test_stop_section_references_governance(self):
        m = re.search(r'## STOP.*?## Step 7', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn('engineering-governance', m.group(0))

    def test_ambiguities_resolved_before_stop(self):
        """F2 fix: ambiguity identification (Step 3) must come before the STOP gate."""
        ambiguities = self.content.find('## Step 3 — Identify ambiguities')
        stop = self.content.find('## STOP')
        self.assertGreater(ambiguities, -1, 'Step 3 — Identify ambiguities not found')
        self.assertGreater(stop, ambiguities, 'STOP must come after ambiguity resolution')

    def test_step2_anchors_companion_skills_before_step3(self):
        m = re.search(r'## Step 2.*?## Step 3', self.content, re.DOTALL)
        self.assertIsNotNone(m, 'Step 2 section not found')
        section = m.group(0)
        self.assertIn('engineering-governance', section)
        self.assertIn('companion', section.lower())
        self.assertIn('Step 3', section)


# ── 6. engineering-playbook changes ───────────────────────────────────────────

class TestPlaybookChanges(unittest.TestCase):
    """engineering-playbook has the corrected session protocol and reuse rule."""

    def setUp(self):
        self.content = read_skill('engineering-playbook')

    def test_no_close_skill_instruction(self):
        self.assertNotIn(
            'Close it (do not reference it)',
            self.content,
            'Old "close it" instruction must be removed'
        )

    def test_no_when_its_step_ends(self):
        self.assertNotIn(
            'when its step ends',
            self.content,
            'Old "when its step ends" instruction must be removed'
        )

    def test_skills_remain_active_instruction_present(self):
        self.assertIn('remain active for the entire session', self.content)

    def test_reuse_protocol_not_duplicated_inline(self):
        # The 5-step sequence "extend existing → compose existing" must NOT be in playbook
        self.assertNotIn(
            'extend existing → compose existing',
            self.content,
            'Inline reuse protocol must be removed (it lives in engineering-code-economy)'
        )

    def test_reuse_rule_references_code_economy(self):
        # The no-duplication rule must point to engineering-code-economy
        m = re.search(r'\*\*No duplication\.\*\*(.*?)(?=\n\n)', self.content, re.DOTALL)
        self.assertIsNotNone(m, 'No duplication rule not found')
        self.assertIn('engineering-code-economy', m.group(0))


# ── 7. engineering-concurrency changes ────────────────────────────────────────

class TestConcurrencyChanges(unittest.TestCase):
    """engineering-concurrency has the rate limiting boundary table."""

    def setUp(self):
        self.content = read_skill('engineering-concurrency')

    def test_boundary_rule_heading_exists(self):
        self.assertIn('Boundary rule', self.content)

    def test_throttler_guard_mentioned_in_boundary(self):
        m = re.search(r'Boundary rule(.*?)For per-user', self.content, re.DOTALL)
        self.assertIsNotNone(m, 'Boundary rule section not found')
        self.assertIn('ThrottlerBehindProxyGuard', m.group(0))

    def test_boundary_distinguishes_ip_vs_entity(self):
        m = re.search(r'Boundary rule(.*?)For per-user', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        section = m.group(0).lower()
        self.assertIn('ip', section)
        self.assertIn('business entity', section)

    def test_no_stacking_rule_exists(self):
        self.assertIn('Do not stack', self.content)

    def test_boundary_cross_references_nestjs_security(self):
        m = re.search(r'Boundary rule(.*?)For per-user', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn('engineering-nestjs-security', m.group(0))


# ── 8. engineering-architecture changes ───────────────────────────────────────

class TestArchitectureChanges(unittest.TestCase):
    """engineering-architecture description no longer claims SOLID."""

    def setUp(self):
        self.content = read_skill('engineering-architecture')
        self.fm = parse_frontmatter(self.content)

    def test_solid_removed_from_description(self):
        description = self.fm.get('description', '')
        self.assertNotIn(
            'SOLID principles',
            description,
            'SOLID principles must not appear in the description triggering field'
        )

    def test_description_still_describes_layer_rules(self):
        description = self.fm.get('description', '')
        self.assertIn('Layer rules', description)

    def test_solid_section_still_exists_in_body(self):
        # SOLID enforcement is still in the body, just not in the description
        self.assertIn('## SOLID', self.content)

    def test_solid_body_defers_to_design_patterns(self):
        m = re.search(r'## SOLID(.*?)(?=\n## |\Z)', self.content, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn('engineering-design-patterns', m.group(1))


# ── Coverage summary ──────────────────────────────────────────────────────────

class TestSkillCoverage(unittest.TestCase):
    """Sanity checks on the overall skill set."""

    def test_expected_skill_count(self):
        files = all_skill_files()
        self.assertGreaterEqual(len(files), 30, f'Expected >= 30 skills, found {len(files)}')

    def test_all_skills_have_nonempty_content(self):
        for path in all_skill_files():
            with self.subTest(skill=skill_name(path)):
                with open(path) as f:
                    content = f.read()
                self.assertGreater(len(content), 200, f'{skill_name(path)}: content is suspiciously short')

    def test_no_skill_has_unclosed_anchor(self):
        for path in all_skill_files():
            with open(path) as f:
                content = f.read()
            opens = content.count('<!-- RULE-ANCHOR -->')
            closes = content.count('<!-- /RULE-ANCHOR -->')
            with self.subTest(skill=skill_name(path)):
                self.assertEqual(opens, closes, f'{skill_name(path)}: mismatched RULE-ANCHOR tags')


if __name__ == '__main__':
    unittest.main(verbosity=2)
