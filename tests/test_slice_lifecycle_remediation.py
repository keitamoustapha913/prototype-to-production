"""Regression coverage for collection blockers and cancelled assignment history."""
import json
import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    'slice_lifecycle_blueprint_fixtures', Path(__file__).with_name('test_blueprint_state.py'))
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)


class SliceLifecycleRemediationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BlueprintTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_material_collection_blocker_stales_affected_review_and_blocks_retry_pass(self):
        feature = self.fixture.reviewed_blueprint()
        self.fixture.review_cli('slice-plan', {'slices': [self.fixture.guide_candidate(feature)]})
        self.fixture.review_cli('slice-review-record', self.fixture.review_input())

        blocked = self.fixture.collection_input()
        finding = blocked['semantic_checks']['decision_fidelity']
        finding.update(result='BLOCKED', explanation='The reviewed decision is not reflected in this proposal.',
                       affected_items=['items-slice'])
        response = self.fixture.review_cli('slice-collection-validate', blocked, ok=False)
        journal = (self.fixture.directory / 'decisions.jsonl').read_bytes()
        self.fixture.review_cli('slice-collection-validate', blocked, ok=False)
        self.assertEqual(journal, (self.fixture.directory / 'decisions.jsonl').read_bytes())
        registry = self.fixture.manifest()['slice_review']
        self.assertEqual(registry['collection_sequence'], 1)
        self.assertNotIn('items-slice', registry['reviews'])
        self.assertIn('items-slice', registry['review_blockers'])
        self.assertEqual(registry['collection']['result'], 'BLOCKED')
        report = json.loads((self.fixture.directory / registry['collection']['path']).read_text())
        self.assertEqual(report['checks']['decision_fidelity']['result'], 'BLOCKED')

        passing = self.fixture.collection_input()
        self.fixture.review_cli('slice-collection-validate', passing, ok=False)
        registry = self.fixture.manifest()['slice_review']
        report = json.loads((self.fixture.directory / registry['collection']['path']).read_text())
        self.assertEqual(report['checks']['review_validity']['result'], 'BLOCKED')
        self.assertIn('Open review blockers', report['checks']['review_validity']['explanation'])

        self.fixture.review_cli('slice-review-record', self.fixture.review_input())
        resolved = self.fixture.review_cli('slice-collection-validate', self.fixture.collection_input())
        self.assertEqual(resolved['result'], 'PASS')

    def test_material_blocker_stales_dependents_but_keeps_unrelated_review_current(self):
        feature = self.fixture.reviewed_blueprint(multiple=True)
        slices = [
            self.fixture.guide_candidate(feature) | {'dependency_contracts': []},
            self.fixture.guide_candidate(feature, 'dependent-slice', path='src/adapter.py',
                                         deps=('items-slice',)),
            self.fixture.guide_candidate(feature, 'unrelated-slice', path='src/view.py') | {
                'dependency_contracts': []},
        ]
        self.fixture.review_cli('slice-plan', {'slices': slices})
        for key in ('items-slice', 'dependent-slice', 'unrelated-slice'):
            self.fixture.review_cli('slice-review-record', self.fixture.review_input(key))

        blocked = self.fixture.collection_input()
        finding = blocked['semantic_checks']['decision_fidelity']
        finding.update(result='BLOCKED', explanation='The source decision is not reflected.',
                       affected_items=['items-slice'])
        self.fixture.review_cli('slice-collection-validate', blocked, ok=False)
        registry = self.fixture.manifest()['slice_review']
        self.assertNotIn('items-slice', registry['reviews'])
        self.assertNotIn('dependent-slice', registry['reviews'])
        self.assertIn('unrelated-slice', registry['reviews'])
        self.assertEqual(registry['current_slice_id'], 'items-slice')

    def test_material_blocker_preserves_the_open_question_cursor(self):
        feature = self.fixture.reviewed_blueprint()
        candidate = self.fixture.guide_candidate(feature) | {'dependency_contracts': []}
        self.fixture.review_cli('slice-plan', {'slices': [candidate]})
        question = 'Should the existing membership guard remain before the storage read?'
        self.fixture.exchange('material-blocker-question', phase='slice_review', status='open',
                              question=question, feature_ids=[feature['feature_id']])
        self.fixture.review_cli('slice-review-record', {
            'slice_id': 'items-slice', 'proposal_revision': 1, 'action': 'question',
            'pending_question_id': 'material-blocker-question'})

        blocked = self.fixture.collection_input()
        finding = blocked['semantic_checks']['decision_fidelity']
        finding.update(result='BLOCKED', explanation='The source decision remains unresolved.',
                       affected_items=['items-slice'])
        self.fixture.review_cli('slice-collection-validate', blocked, ok=False)
        registry = self.fixture.manifest()['slice_review']
        self.assertEqual(registry['pending_question_id'], 'material-blocker-question')
        self.assertEqual(registry['current_slice_id'], 'items-slice')

    def test_material_finding_unknown_or_colliding_ids_scopes_without_losing_slices(self):
        review = object.__new__(fixtures.SLICE_REVIEW_MODULE.SliceReview)
        review.registry = {'order': ['first', 'second'], 'selected': {
            'first': {'revision': 1}, 'second': {'revision': 2}}}
        checks = {'decision_fidelity': {'result': 'BLOCKED', 'explanation': 'Material finding',
            'affected_items': ['second', 'unmapped reviewer prose'], 'evidence_paths': ['review.md']}}
        packets = {
            'first': {'feature_contracts': [{'feature_id': 'second', 'acceptance_checks': []}]},
            'second': {'feature_contracts': []},
        }
        blockers = review.material_collection_blockers(checks, packets, {'review.md': 'abc'}, 'report.json')
        self.assertEqual(set(blockers), {'first', 'second'})

    def test_cancelled_assignment_preserves_exact_authorized_source_as_replacement_baseline(self):
        feature = self.fixture.reviewed_blueprint()
        proposal = self.fixture.guide_candidate(feature) | {'dependency_contracts': []}
        self.fixture.review_cli('slice-plan', {'slices': [proposal]})
        self.fixture.review_cli('slice-review-record', self.fixture.review_input())
        package = self.fixture.lock_reviewed_package()
        self.fixture.authorize_fixture(package)
        self.fixture.review_cli('slice-release', package | {'slice_id': 'items-slice'})

        source = self.fixture.root / 'src/service.py'
        source.write_bytes(source.read_bytes() + b'\n# Authorized implementation change.\n')
        self.fixture.cli('validate')
        authorized_bytes = source.read_bytes()
        self.fixture.cli('slice-cancel', '--slice-id', 'items-slice', '--reason', 'Replace this assignment.')
        journal = (self.fixture.directory / 'decisions.jsonl').read_bytes()
        self.fixture.cli('slice-cancel', '--slice-id', 'items-slice', '--reason', 'Replace this assignment.', ok=False)
        self.assertEqual(journal, (self.fixture.directory / 'decisions.jsonl').read_bytes())

        replacement = self.fixture.guide_candidate(feature, 'items-slice-v2', path='src/service.py') | {
            'dependency_contracts': [], 'replaces_slice_ids': ['items-slice']}
        plan = self.fixture.review_cli('slice-plan', {
            'amendment': 'Replace the cancelled assignment.',
            'user_response': 'Replace items-slice with items-slice-v2.', 'slices': [replacement]})
        self.assertEqual(plan['slices']['items-slice-v2']['replaces_slice_ids'], ['items-slice'])

        source.write_bytes(source.read_bytes() + b'\n# Drift after cancellation.\n')
        drift = self.fixture.review_cli('slice-plan', {
            'amendment': 'Replace the cancelled assignment again.',
            'user_response': 'Keep replacing items-slice.', 'slices': [replacement]}, ok=False)
        self.assertIn('cancelled slice source drift', drift.stderr.lower())
        source.write_bytes(authorized_bytes)

        self.fixture.review_cli('slice-review-record', self.fixture.review_input('items-slice-v2'))
        replacement_package = self.fixture.lock_reviewed_package(version=2)
        self.fixture.authorize_fixture(replacement_package)
        self.fixture.review_cli('slice-release', replacement_package | {'slice_id': 'items-slice-v2'})
        source.write_bytes(source.read_bytes() + b'\n# Replacement implementation change.\n')
        self.fixture.cli('validate')
        self.fixture.cli('slice-cancel', '--slice-id', 'items-slice-v2', '--reason', 'Replace it again.')
        next_replacement = self.fixture.guide_candidate(
            feature, 'items-slice-v3', path='src/service.py') | {
                'dependency_contracts': [], 'replaces_slice_ids': ['items-slice', 'items-slice-v2']}
        next_plan = self.fixture.review_cli('slice-plan', {
            'amendment': 'Replace the second cancelled assignment.',
            'user_response': 'Replace the prior assignments with items-slice-v3.',
            'slices': [next_replacement]})
        self.assertEqual(next_plan['slices']['items-slice-v3']['replaces_slice_ids'],
                         ['items-slice', 'items-slice-v2'])


if __name__ == '__main__':
    unittest.main()
