"""Focused contracts for immutable slice review evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from hashlib import sha256
import json
import unittest

from scripts import slice_evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.errors = []

        def require(condition, message):
            if not condition:
                raise ValueError(message)

        def artifact_file(directory, relative):
            path = (directory / relative).resolve(strict=True)
            if not path.is_relative_to(directory.resolve()) or not path.is_file():
                raise ValueError(f'Invalid artifact path: {relative}')
            return path

        def digest_of(path):
            return sha256(path.read_bytes()).hexdigest()

        self.review = SimpleNamespace(
            directory=self.directory,
            require=require,
            api=SimpleNamespace(artifact_file=artifact_file, digest_of=digest_of),
        )

    def artifact(self, name, contents='evidence'):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return name

    def test_evidence_hashes_rejects_empty_evidence_and_mutable_control_alias(self):
        self.assertRaises(ValueError, slice_evidence.evidence_hashes, self.review, [])
        (self.directory / 'empty.txt').write_bytes(b'')
        self.assertRaises(ValueError, slice_evidence.evidence_hashes, self.review, ['empty.txt'])
        control = self.artifact('CHECKPOINT.md')
        alias_dir = self.directory / 'inputs'
        alias_dir.mkdir()
        alias = alias_dir / 'checkpoint-alias.md'
        alias.symlink_to(self.directory / control)
        self.assertRaises(ValueError, slice_evidence.evidence_hashes, self.review,
                          ['inputs/checkpoint-alias.md'])

    def test_handoff_binds_walkthrough_and_checks_to_exact_revision(self):
        evidence_record = {'record_type': 'independent_no_code_walkthrough', 'reviewer': 'reader',
            'context': 'fresh_session', 'test_execution': False,
            'source_paths': ['blueprints/items.md',
                'slice-proposals/items/revisions/2/implementation-guide.md'],
            'cases': [{'scenario': 'items:forbidden', 'feature_id': 'items',
                'check_id': 'items.visible', 'expected': '403 forbidden', 'observed': '403 forbidden',
                'observation_basis': 'Traced the forbidden-state branch in the approved guide.',
                'references': [{'feature_id': 'items', 'check_id': 'items.visible',
                    'source_path': 'slice-proposals/items/revisions/2/implementation-guide.md',
                    'symbol': 'State and scenario matrix'}]},
                {'scenario': 'items:check:items.visible', 'feature_id': 'items',
                 'check_id': 'items.visible', 'expected': 'visible', 'observed': 'visible',
                 'observation_basis': 'Traced the approved member-list success case.',
                 'references': [{'feature_id': 'items', 'check_id': 'items.visible',
                     'source_path': 'slice-proposals/items/revisions/2/implementation-guide.md',
                     'symbol': 'Tests and acceptance evidence'}]}]}
        evidence = self.artifact('inputs/handoff.json', json.dumps(evidence_record))
        self.artifact('blueprints/items.md', '# approved source reference')
        self.artifact('slice-proposals/items/revisions/2/implementation-guide.md',
                      '## State and scenario matrix\n- forbidden gives 403.\n'
                      '## Tests and acceptance evidence\n- confirmed visible.\n'
                      '## Finished experience or service contract\n- confirmed visible.\n'
                      '## Detailed functional requirements\n- validated guard.\n'
                      '## Step-by-step implementation recipe\n- validated guard before query.\n'
                      '```markdown\n## Example heading inside a code fence\n```\n')
        packet = {'slice_id': 'items', 'proposal_revision': 2, 'owner': 'builder',
                  'evidence_schema_version': 2,
                  'acceptance_check_ids': ['items.visible'], 'guide_path': 'slice-proposals/items/revisions/2/implementation-guide.md',
                  'feature_contracts': [{'feature_id': 'items', 'states': ['forbidden'],
                      'acceptance_checks': [{'check_id': 'items.visible', 'expected': 'visible',
                                             'state_expectations': {'forbidden': '403 forbidden'}}]}],
                  'allowed_paths': ['src/items.py'], 'artifact_paths': ['blueprints/items.md'],
                  'contract_path': 'slice-proposals/items/revisions/2/contract.json'}
        entry = {'revision': 2, 'guide_path': packet['guide_path'],
                 'guide_sha256': 'a' * 64, 'contract_sha256': 'b' * 64}
        declaration = {
            'slice_id': 'items', 'proposal_revision': 2,
            'guide_sha256': 'a' * 64, 'contract_sha256': 'b' * 64,
            'result': 'PASS', 'reviewer': 'reader',
            'walkthrough': {'mode': 'no_code', 'context': 'fresh_session', 'independent': True, 'performed': True,
                            'test_execution': False,
                            'evidence_path': evidence,
                            'attestation': 'I read the frozen guide and contract without executing tests or changing code.'},
            'findings': [{'section': 'State and scenario matrix', 'citation': 'line 2',
                          'finding': 'The forbidden state is explained.', 'resolution': 'Preserved 403.'}],
            'source_paths': ['blueprints/items.md'], 'evidence_paths': [evidence],
            'read_only_scenarios': [{'scenario': 'items:forbidden', 'expected': '403 forbidden',
                                     'observed': '403 forbidden', 'read_only': True,
                                     'observation_basis': 'Traced the forbidden-state branch in the approved guide.',
                                     'references': [{'feature_id': 'items', 'check_id': 'items.visible',
                                         'source_path': packet['guide_path'], 'symbol': 'State and scenario matrix'}]},
                                    {'scenario': 'items:check:items.visible', 'expected': 'visible',
                                     'observed': 'visible', 'read_only': True,
                                     'observation_basis': 'Traced the approved member-list success case.',
                                     'references': [{'feature_id': 'items', 'check_id': 'items.visible',
                                         'source_path': packet['guide_path'], 'symbol': 'Tests and acceptance evidence'}]}],
            'acceptance_checks': [{'check_id': 'items.visible', 'expected': 'visible',
                                   'observed': 'visible', 'result': 'PASS'}],
        }
        hashes = slice_evidence.validate_handoff(self.review, packet, entry, declaration)
        self.assertEqual(set(hashes), {'blueprints/items.md', evidence})
        packet['evidence_schema_version'] = 3
        with self.assertRaisesRegex(ValueError, 'normative guide sections'):
            slice_evidence.validate_handoff(self.review, packet, entry, declaration)
        for line, section in ((6, 'Finished experience or service contract'),
                              (8, 'Detailed functional requirements'),
                              (10, 'Step-by-step implementation recipe')):
            declaration['findings'].append({'section': section, 'citation': f'line {line}',
                                            'finding': 'Compared the implementation against the approved guard.',
                                            'resolution': 'The approved member and outsider outcomes are preserved.',
                                            'result': 'PASS'})
        slice_evidence.validate_handoff(self.review, packet, entry, declaration)
        reference = declaration['read_only_scenarios'][0]['references'][0]
        for symbol in ('A heading that does not exist', 'Example heading inside a code fence'):
            with self.subTest(symbol=symbol):
                reference['symbol'] = symbol
                evidence_record['cases'][0]['references'][0]['symbol'] = symbol
                (self.directory / evidence).write_text(json.dumps(evidence_record))
                with self.assertRaisesRegex(ValueError, 'Cannot resolve walkthrough symbol'):
                    slice_evidence.validate_handoff(self.review, packet, entry, declaration)
        reference['symbol'] = 'State and scenario matrix'
        evidence_record['cases'][0]['references'][0]['symbol'] = reference['symbol']
        packet['evidence_schema_version'] = 2
        (self.directory / evidence).write_text('some arbitrary nonempty attachment')
        self.assertRaisesRegex(ValueError, 'structured no-code walkthrough record',
                               slice_evidence.validate_handoff, self.review, packet, entry, declaration)
        (self.directory / evidence).write_text(json.dumps(evidence_record))
        declaration.pop('walkthrough')
        self.assertRaisesRegex(ValueError, 'independent no-code walkthrough',
                               slice_evidence.validate_handoff, self.review, packet, entry, declaration)
        declaration['walkthrough'] = {'mode': 'no_code', 'context': 'fresh_session', 'independent': True, 'performed': True,
            'test_execution': False, 'evidence_path': evidence,
            'attestation': 'I read the frozen guide and contract without executing tests or changing code.'}
        declaration['read_only_scenarios'][0].pop('references')
        self.assertRaisesRegex(ValueError, 'approved case and source references',
                               slice_evidence.validate_handoff, self.review, packet, entry, declaration)
        declaration['read_only_scenarios'][0]['references'] = [{'feature_id': 'items',
            'check_id': 'items.visible', 'source_path': packet['guide_path'], 'symbol': 'State and scenario matrix'}]
        declaration['proposal_revision'] = 1
        self.assertRaises(ValueError, slice_evidence.validate_handoff,
                          self.review, packet, entry, declaration)

    def test_blocked_semantic_check_keeps_reason_sources_and_hashed_evidence(self):
        source = self.artifact('blueprints/items.md', 'ItemSchema defines id as a string.\n')
        evidence = self.artifact('inputs/journey.md', 'Missing outsider journey')
        packet = {'slice_id': 'items', 'proposal_revision': 2, 'feature_ids': ['items'],
                  'artifact_paths': [source], 'feature_contracts': [{'feature_id': 'items',
                    'acceptance_checks': [{'check_id': 'items.visible', 'expected': 'visible'}]}], 'shared_contracts': {}}
        entry = {'revision': 2, 'guide_sha256': 'a' * 64, 'contract_sha256': 'b' * 64}
        declaration = {
            'schema_version': 2, 'result': 'BLOCKED', 'explanation': 'The outsider journey is not covered.',
            'reviewer': {'name': 'reader', 'context': 'fresh_session', 'independent_of_authoring': True},
            'source_paths': [source], 'evidence_paths': [evidence],
            'revision_bindings': {'items': {'proposal_revision': 2, 'guide_sha256': 'a' * 64,
                                            'contract_sha256': 'b' * 64}},
            'covered_feature_ids': ['items'],
            'case_conclusions': [{'feature_id': 'items', 'check_id': 'items.visible',
                                  'result': 'BLOCKED', 'expected': 'visible',
                                  'conclusion': 'Outsider journey missing.',
                                  'source_path': source, 'citation': 'check:items.visible'}],
            'interface_conclusions': [],
            'affected_items': ['outsider journey'],
        }
        result = slice_evidence.validate_semantic(
            self.review, 'end_to_end_journeys', declaration, {'items': packet}, {'items': entry})
        self.assertEqual(result['result'], 'BLOCKED')
        self.assertEqual(result['evidence_paths'], [evidence])
        self.assertIn(evidence, result['evidence_hashes'])
        self.assertIn(source, result['evidence_hashes'])

    def test_semantic_pass_requires_exact_case_and_shared_interface_conclusions(self):
        source = self.artifact('blueprints/items.md', 'ItemSchema defines id as a string.\n')
        evidence = self.artifact('inputs/reconciliation.md')
        packets = {
            'items': {'slice_id': 'items', 'proposal_revision': 2, 'feature_ids': ['items.list'],
                'artifact_paths': [source], 'feature_contracts': [{'feature_id': 'items.list',
                    'acceptance_checks': [{'check_id': 'items.list.visible', 'expected': 'visible'}]}],
                'shared_contracts': {'ItemSchema': {'role': 'provides', 'definition': {'id': 'string'}}}},
            'shell': {'slice_id': 'shell', 'proposal_revision': 1, 'feature_ids': ['shell.items'],
                'artifact_paths': [source], 'feature_contracts': [{'feature_id': 'shell.items',
                    'acceptance_checks': [{'check_id': 'shell.items.renders', 'expected': 'renders'}]}],
                'shared_contracts': {'ItemSchema': {'role': 'consumes', 'definition': {'id': 'string'}}}},
        }
        unrelated = self.artifact('blueprints/unrelated.md', 'An unrelated slice has its own source.')
        packets['unrelated'] = {'slice_id': 'unrelated', 'proposal_revision': 1,
                                'feature_ids': [], 'artifact_paths': [unrelated], 'feature_contracts': []}
        revisions = {key: {'revision': p['proposal_revision'], 'guide_sha256': 'a' * 64,
                           'contract_sha256': 'b' * 64} for key, p in packets.items()}
        declaration = {
            'schema_version': 2, 'result': 'PASS', 'explanation': 'Reviewed each approved case and interface.',
            'reviewer': {'name': 'fresh reader', 'context': 'fresh_session', 'independent_of_authoring': True},
            'revision_bindings': {key: {'proposal_revision': v['revision'], 'guide_sha256': v['guide_sha256'],
                'contract_sha256': v['contract_sha256']} for key, v in revisions.items()},
            'covered_feature_ids': ['items.list', 'shell.items'],
            'case_conclusions': [
                {'feature_id': 'items.list', 'check_id': 'items.list.visible', 'result': 'PASS',
                 'expected': 'visible', 'conclusion': 'Renders visible items.',
                 'source_path': source, 'citation': 'check:items.list.visible'},
                {'feature_id': 'shell.items', 'check_id': 'shell.items.renders', 'result': 'PASS',
                 'expected': 'renders', 'conclusion': 'Shell renders the list.',
                 'source_path': source, 'citation': 'check:shell.items.renders'}],
            'interface_conclusions': [{'interface_id': 'ItemSchema', 'provider_slice_id': 'items',
                'consumer_slice_ids': ['shell'], 'result': 'PASS', 'conclusion': 'Both sides use id as string.',
                'source_path': source, 'citation': 'line 1'}],
            'boundary_conclusions': [{'slice_ids': ['items', 'shell'], 'result': 'PASS',
                'conclusion': 'Compared the provider and consumer boundary.',
                'source_path': source, 'citation': 'line 1'}],
            'source_paths': [source], 'evidence_paths': [evidence],
        }
        result = slice_evidence.validate_semantic(
            self.review, 'shared_contract_review', declaration, packets, revisions)
        self.assertEqual(result['result'], 'PASS')
        declaration['boundary_conclusions'][0]['source_path'] = unrelated
        with self.assertRaisesRegex(ValueError, 'Cited source path is not frozen or approved'):
            slice_evidence.validate_semantic(self.review, 'shared_contract_review', declaration, packets, revisions)
        declaration['boundary_conclusions'][0]['source_path'] = source
        declaration['case_conclusions'].pop()
        self.assertRaisesRegex(ValueError, 'case conclusions', slice_evidence.validate_semantic,
            self.review, 'shared_contract_review', declaration, packets, revisions)
        declaration['case_conclusions'].append({'feature_id': 'shell.items',
            'check_id': 'shell.items.renders', 'result': 'PASS', 'expected': 'invented',
            'conclusion': 'Shell renders the list.', 'source_path': source,
            'citation': 'check:shell.items.renders'})
        self.assertRaisesRegex(ValueError, 'approved expected outcome', slice_evidence.validate_semantic,
            self.review, 'shared_contract_review', declaration, packets, revisions)

    def test_render_and_acceptance_cases_with_same_id_require_separate_conclusions(self):
        source = self.artifact('blueprints/items.md', 'The items list renders with a 16px gap.\n')
        packet = {'slice_id': 'items', 'proposal_revision': 1, 'feature_ids': ['items'],
                  'artifact_paths': [source], 'feature_contracts': [{'feature_id': 'items',
                    'acceptance_checks': [{'check_id': 'items.visible', 'expected': '16px'}],
                    'visual_contract': {'render_cases': [{'case_id': 'items.visible', 'route': '/items',
                        'viewport': {'width': 800, 'height': 600}, 'ready_selector': '#items'}]}}]}
        entry = {'revision': 1, 'guide_sha256': 'a' * 64, 'contract_sha256': 'b' * 64}
        render = {'feature_id': 'items', 'check_id': 'items.visible', 'result': 'PASS',
                  'expected': 'renders /items at 800x600 with #items',
                  'conclusion': 'The approved render target is reachable.',
                  'source_path': source, 'citation': 'line 1'}
        declaration = {
            'schema_version': 2, 'result': 'PASS', 'explanation': 'Verified rendered list and spacing.',
            'reviewer': {'name': 'reader', 'context': 'fresh_session', 'independent_of_authoring': True},
            'revision_bindings': {'items': {'proposal_revision': 1, 'guide_sha256': 'a' * 64,
                                           'contract_sha256': 'b' * 64}},
            'covered_feature_ids': ['items'], 'source_paths': [source], 'evidence_paths': [source],
            'case_conclusions': [render], 'interface_conclusions': [],
        }
        with self.assertRaisesRegex(ValueError, 'case_kind'):
            slice_evidence.validate_semantic(self.review, 'verification_coverage', declaration,
                                             {'items': packet}, {'items': entry})
        saved = {'checks': {'verification_coverage': declaration}}
        with self.assertRaisesRegex(ValueError, 'does not distinguish'):
            slice_evidence.check_saved_case_kinds(self.review, saved, {'items': packet})
        render['case_kind'] = 'render'
        with self.assertRaisesRegex(ValueError, 'cover every exact feature/check'):
            slice_evidence.validate_semantic(self.review, 'verification_coverage', declaration,
                                             {'items': packet}, {'items': entry})
        declaration['case_conclusions'].append(render | {
            'case_kind': 'acceptance', 'expected': '16px', 'conclusion': 'Measured the exact 16px gap.'})
        result = slice_evidence.validate_semantic(self.review, 'verification_coverage', declaration,
                                                  {'items': packet}, {'items': entry})
        self.assertEqual(result['result'], 'PASS')
        slice_evidence.check_saved_case_kinds(self.review, saved, {'items': packet})

    def test_not_applicable_semantic_result_requires_scoped_reason_and_evidence(self):
        source = self.artifact('blueprints/items.md')
        evidence = self.artifact('inputs/applicability.md', 'No journeys cross the selected feature scope.')
        packet = {'slice_id': 'items', 'proposal_revision': 2, 'feature_ids': ['items'],
                  'artifact_paths': [source], 'feature_contracts': [{'feature_id': 'items',
                    'acceptance_checks': [{'check_id': 'items.visible', 'expected': 'visible'}]}],
                  'shared_contracts': {}}
        entry = {'revision': 2, 'guide_sha256': 'a' * 64, 'contract_sha256': 'b' * 64}
        declaration = {'schema_version': 2, 'result': 'NOT_APPLICABLE', 'explanation': 'No cross-slice journeys apply.',
            'reviewer': {'name': 'fresh reader', 'context': 'fresh_session', 'independent_of_authoring': True},
            'revision_bindings': {'items': {'proposal_revision': 2, 'guide_sha256': 'a' * 64, 'contract_sha256': 'b' * 64}},
            'covered_feature_ids': ['items'], 'case_conclusions': [], 'interface_conclusions': [],
            'applicability': {'scope': ['items'], 'reason': 'Only one isolated list feature is selected; no cross-slice path exists.',
                              'evidence_paths': [evidence]},
            'source_paths': [source], 'evidence_paths': [evidence]}
        result = slice_evidence.validate_semantic(self.review, 'end_to_end_journeys', declaration,
            {'items': packet}, {'items': entry})
        self.assertEqual(result['result'], 'NOT_APPLICABLE')
        del declaration['applicability']
        self.assertRaisesRegex(ValueError, 'scoped applicability', slice_evidence.validate_semantic,
            self.review, 'end_to_end_journeys', declaration, {'items': packet}, {'items': entry})

    def test_recap_contains_collection_result_and_full_review_context(self):
        review = SimpleNamespace(
            registry={'order': ['items'], 'selected': {'items': {
                'title': 'Member item list', 'feature_ids': ['items.list'], 'dependencies': [],
                'allowed_paths': ['src/items.py'], 'revision': 2, 'guide_sha256': 'a' * 64,
                'contract_sha256': 'b' * 64}},
                'reviews': {'items': {'review_mode': 'manual', 'result': 'approved',
                                     'reviewer': 'user', 'handoff_review': {'result': 'PASS',
                                        'walkthrough': {'test_execution': False}}}},
            },
            active_approval='approval-3', manifest={'approved_version': 'spec-v3'},
        )
        report = {'result': 'PASS', 'checks': {'guide_integrity': {'result': 'PASS'},
                                               'end_to_end_journeys': {'result': 'NOT_APPLICABLE',
                                                                       'explanation': 'No journeys.'},
                                               'verification_coverage': {
                                                   'result': 'PASS',
                                                   'reviewer': {'name': 'collection-reader'},
                                                   'case_conclusions': [{
                                                       'feature_id': 'items.list', 'check_id': 'member-result',
                                                       'result': 'PASS', 'expected': 'Returns items for the member',
                                                       'conclusion': 'The fixture covers the membership rule.',
                                                       'source_path': 'inputs/review.md', 'citation': 'line 3',
                                                   }],
                                                   'interface_conclusions': [{
                                                       'interface_id': 'items-read', 'provider_slice_id': 'items',
                                                       'consumer_slice_ids': ['ui'], 'result': 'PASS',
                                                       'conclusion': 'Both sides agree on the response format.',
                                                       'source_path': 'inputs/review.md', 'citation': 'line 4',
                                                   }],
                                               }},
                  'evidence_hashes': {'inputs/review.md': 'c' * 64}}
        recap = slice_evidence.render_recap(review, 'package-v1', 'slice-review/validation/v1.json', report)
        for required in ('package-v1', 'approval-3', 'spec-v3', 'Member item list', 'manual',
                         'end_to_end_journeys', 'NOT_APPLICABLE', 'src/items.py', 'a' * 64,
                         'Independent no-code walkthrough', 'test execution: no', 'collection-reader',
                         'Specification review', 'items.list/member-result', 'line 3', 'items-read', 'line 4'):
            self.assertIn(required, recap)


if __name__ == '__main__':
    unittest.main()
