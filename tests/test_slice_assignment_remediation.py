"""Regressions for assigning approved acceptance checks across reviewed slices."""
import copy
import json
import unittest

import test_blueprint_state as blueprint_fixtures


class SliceAssignmentRemediationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = blueprint_fixtures.BlueprintTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def approved_feature_with_multiple_checks(self):
        fixture = self.fixture
        fixture.start(review=True)
        fixture.exchange()
        fixture.record_baseline(fixture.baseline('ui'))
        fixture.record_reviewed_plan(fixture.plan())
        blueprint = fixture.blueprint(surface='ui')
        feature = blueprint['features'][0]
        feature['implementation_paths'] += ['src/adapter.py']
        fixture.cli('blueprint-record', '--blueprint-file', fixture.payload('blueprint', blueprint))
        coverage = fixture.coverage(surface='ui')
        fixture.cli('blueprint-coverage', '--coverage-file', fixture.payload('coverage', coverage))
        fixture.cli('approve', '--approval-file', fixture.payload('approval', fixture.approval()))
        return feature

    def test_reviewed_inventory_can_split_checks_and_collection_requires_the_union(self):
        feature = self.approved_feature_with_multiple_checks()
        first_feature = copy.deepcopy(feature)
        first_feature['acceptance_checks'] = [feature['acceptance_checks'][0]]
        second_feature = copy.deepcopy(feature)
        second_feature['acceptance_checks'] = [feature['acceptance_checks'][1]]

        first = self.fixture.guide_candidate(first_feature, path='src/service.py', surfaces=('ui',))
        second = self.fixture.guide_candidate(second_feature, 'history-slice', path='src/adapter.py', surfaces=('ui',))
        partial = self.fixture.cli('slice-plan', '--input-file',
                                   self.fixture.payload('partial-inventory', {'slices': [first]}), ok=False)
        partial_report = json.loads(partial.stdout)
        self.assertEqual(partial_report['result'], 'BLOCKED')
        self.assertIn(['items-feature', 'items-behavior'], partial_report['planning_gaps']['feature_checks'])

        planned = self.fixture.review_cli('slice-plan', {'slices': [first, second]})

        packets = {key: self.fixture.manifest()['slice_review']['selected'][key]
                   for key in self.fixture.manifest()['slice_review']['order']}
        contracts = {key: json.loads((self.fixture.directory / entry['contract_path']).read_text())
                     for key, entry in packets.items()}
        self.assertEqual(contracts['items-slice']['acceptance_check_ids'], ['items-access'])
        self.assertEqual(contracts['history-slice']['acceptance_check_ids'], ['items-behavior'])
        self.fixture.review_cli('slice-review-record', self.fixture.review_input())
        self.fixture.review_cli('slice-review-record', self.fixture.review_input('history-slice'))
        collection = self.fixture.collection_input()
        case_conclusions = [
            {'feature_id': feature['feature_id'], 'check_id': check['check_id'], 'result': 'PASS',
             'expected': check['expected'], 'source_path': packets['items-slice']['guide_path'],
             'citation': f'check:{check["check_id"]}',
             'conclusion': f'Traced approved verification for {check["check_id"]}.'}
            for check in feature['acceptance_checks']]
        case_conclusions.extend(
            {'feature_id': feature['feature_id'], 'check_id': case['case_id'], 'result': 'PASS',
             'expected': f'renders {case["route"]} at {case["viewport"]["width"]}x'
                         f'{case["viewport"]["height"]} with {case["ready_selector"]}',
             'source_path': packets['items-slice']['guide_path'], 'citation': f'check:{case["case_id"]}',
             'conclusion': f'Traced approved render case {case["case_id"]}.'}
            for case in feature['visual_contract']['render_cases'])
        for key in ('decision_fidelity', 'shared_contract_review', 'end_to_end_journeys', 'verification_coverage'):
            collection['semantic_checks'][key]['case_conclusions'] = copy.deepcopy(case_conclusions)
        report = self.fixture.review_cli('slice-collection-validate', collection)
        self.assertEqual(report['result'], 'PASS')

        preview = self.fixture.review_cli('slice-package-preview')
        review_manifest = self.fixture.manifest()['slice_review']
        package_id = preview['package_id']
        locked = self.fixture.review_cli('slice-package-lock', {
            'package_id': package_id, 'collection_path': report['path'],
            'collection_sha256': review_manifest['artifact_hashes'][report['path']],
            'recap_path': self.fixture.artifact('inputs/recap.md', preview['recap']),
            'approved_by_user': True, 'user_response': f'Approve locking {package_id}.'})
        package = {'package_id': package_id,
                   'package_hash': locked['packages'][package_id]['package_hash']}
        self.fixture.authorize_fixture(package)
        self.fixture.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        released = self.fixture.manifest()['slices']['items-slice']
        release_packet = json.loads((self.fixture.directory / released['contract_file']).read_text())
        self.assertEqual(release_packet['acceptance_check_ids'], ['items-access'])
        self.assertEqual([check['check_id'] for check in release_packet['feature_contracts'][0]['acceptance_checks']],
                         ['items-access'])
        self.assertFalse(blueprint_fixtures.FIXTURE_CLI.blueprint_state(
            self.fixture.directory, self.fixture.manifest()).all_accepted())
        self.fixture.review_cli('slice-release', package | {'slice_id': 'history-slice'})
        released = self.fixture.manifest()['slices']['history-slice']
        release_packet = json.loads((self.fixture.directory / released['contract_file']).read_text())
        self.assertEqual(release_packet['acceptance_check_ids'], ['items-behavior'])
        self.assertEqual([check['check_id'] for check in release_packet['feature_contracts'][0]['acceptance_checks']],
                         ['items-behavior'])
