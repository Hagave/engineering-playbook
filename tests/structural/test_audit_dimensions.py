"""
Structural tests for the audit mode of engineering-code-review: the dimension
file the passes read, and the contract between it, the skill and the surface
script.

A dimension that walks a field the surface never emits, or names a checklist
section the skill does not have, would run a pass over nothing and report it as
covered, so both are checked against the real artifacts, not a copy of them.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILL_DIR = os.path.join(_ROOT, 'skills', 'engineering-code-review')
_SKILL = os.path.join(_SKILL_DIR, 'SKILL.md')
_DIMENSIONS = os.path.join(_SKILL_DIR, 'references', 'audit-dimensions.json')
_SURFACE = os.path.join(_ROOT, 'audit', 'surface.py')
sys.path.insert(0, os.path.dirname(_SURFACE))
import surface

REQUIRED_KEYS = {'id', 'title', 'walks', 'checklist', 'question'}


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _dimensions() -> dict:
    with open(_DIMENSIONS, encoding='utf-8') as handle:
        return json.load(handle)


def _surface_fields() -> set[str]:
    root = tempfile.mkdtemp()
    try:
        path = os.path.join(root, 'C.java')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('@RestController\n@RequestMapping("/a")\nclass C {\n@GetMapping("/b")\nX b() {}\n}\n')
        return set(surface.build(root, surface.Seed(route_prefix='/a')))
    finally:
        shutil.rmtree(root)


def _object_schemas(schema: dict) -> list[dict]:
    found = [schema] if schema.get('type') == 'object' else []
    for child in schema.get('properties', {}).values():
        found += _object_schemas(child)
    if isinstance(schema.get('items'), dict):
        found += _object_schemas(schema['items'])
    return found


class TestDimensions(unittest.TestCase):

    def test_every_dimension_has_the_required_keys(self) -> None:
        for dimension in _dimensions()['dimensions']:
            with self.subTest(dimension=dimension.get('id')):
                self.assertEqual(REQUIRED_KEYS - dimension.keys(), set())
                self.assertTrue(dimension['question'].strip())
                self.assertTrue(dimension['walks'])

    def test_ids_are_unique(self) -> None:
        ids = [d['id'] for d in _dimensions()['dimensions']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_walked_fields_are_fields_the_surface_emits(self) -> None:
        emitted = _surface_fields()
        for dimension in _dimensions()['dimensions']:
            with self.subTest(dimension=dimension['id']):
                self.assertEqual(set(dimension['walks']) - emitted, set())

    def test_every_surface_item_field_is_walked_by_some_dimension(self) -> None:
        walked = {field for d in _dimensions()['dimensions'] for field in d['walks']}
        items = _surface_fields() - {'repos', 'unknown', 'limits'}
        self.assertEqual(items - walked, set())

    def test_named_checklist_sections_exist_in_the_skill(self) -> None:
        headings = set(re.findall(r'^### (.+)$', _read(_SKILL), re.MULTILINE))
        for dimension in _dimensions()['dimensions']:
            with self.subTest(dimension=dimension['id']):
                self.assertEqual(set(dimension['checklist']) - headings, set())


class TestFindingSchema(unittest.TestCase):

    def test_required_keys_are_declared_properties(self) -> None:
        for schema in _object_schemas(_dimensions()['finding_schema']):
            with self.subTest(required=schema.get('required')):
                self.assertEqual(set(schema.get('required', [])) - set(schema.get('properties', {})), set())

    def test_evidence_is_reproduction_or_trace(self) -> None:
        finding = _dimensions()['finding_schema']['properties']['findings']['items']
        self.assertEqual(finding['properties']['evidence']['properties']['kind']['enum'],
                         ['reproduced', 'code-trace'])
        self.assertIn('evidence', finding['required'])

    def test_coverage_is_required(self) -> None:
        self.assertIn('coverage', _dimensions()['finding_schema']['required'])


class TestSkillContract(unittest.TestCase):

    def test_skill_points_at_the_dimension_file_and_the_surface_script(self) -> None:
        text = _read(_SKILL)
        self.assertIn('references/audit-dimensions.json', text)
        self.assertIn('<playbook>/audit/surface.py', text)
        self.assertTrue(os.path.isfile(_SURFACE))

    def test_surface_script_is_two_levels_above_the_skill(self) -> None:
        self.assertEqual(os.path.realpath(os.path.join(_SKILL_DIR, '..', '..', 'audit', 'surface.py')),
                         os.path.realpath(_SURFACE))

    def test_surface_flags_used_by_the_skill_exist(self) -> None:
        flags = set(re.findall(r'surface\.py --root <dir> (--[\w-]+)', _read(_SKILL)))
        self.assertEqual(flags, {'--diff', '--route-prefix', '--path'})
        for flag in flags:
            with self.subTest(flag=flag):
                self.assertIn(f"'{flag}'", _read(_SURFACE))

    def test_pre_push_base_covers_a_branch_without_upstream(self) -> None:
        text = _read(_SKILL)
        self.assertIn('--diff <base>', text)
        self.assertRegex(text, r'`<base>` is .*default branch .*pushed for the first time')

    def test_verification_defers_hostile_payloads_to_security(self) -> None:
        self.assertRegex(_read(_SKILL), r'Hostile payloads from the `engineering-security` fiches stay under that skill')

    def test_pre_push_review_runs_the_audit(self) -> None:
        self.assertRegex(_read(_SKILL), r'pre-push review runs in \*\*audit mode\*\*')

    def test_description_routes_audit_requests(self) -> None:
        description = re.search(r'^description: "(.+)"$', _read(_SKILL), re.MULTILINE).group(1)
        self.assertIn('audit existing code', description)


if __name__ == '__main__':
    unittest.main()
