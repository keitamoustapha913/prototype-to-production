import re
from pathlib import Path
import unittest

from scripts.slice_guide import (
    markdown_links,
    _parse_mermaid,
    _validate_diagram_contracts,
    validate_json_schema,
    validate_surface_contracts,
    validate_guide,
)


class _Review:
    class _Api:
        class StateError(Exception):
            pass

        @staticmethod
        def ownership_contains(owner, path):
            return owner == path

        ownership_overlaps = ownership_contains

        @staticmethod
        def project_source_file(project_root, relative):
            if relative == 'src/items.py':
                return Path('/fixture/src/items.py')
            raise FileNotFoundError(relative)

    class _Blueprint:
        manifest = {'approvals': {'approval-1': {'workspace_snapshot': {}, 'exceptions': []}}}

        def load(self, name, version):
            if name == 'blueprint_coverage':
                return {'mappings': []}
            return {'surface': 'backend'}

        def workspace_snapshot(self):
            return {}

        def test_sources(self, feature):
            return {path for check in feature.get('acceptance_checks', [])
                    for path in check.get('test_source_paths', []) +
                    [arg for arg in check.get('command', [])[1:] if arg.endswith('.py')]}

    blueprint = _Blueprint()
    api = _Api()
    manifest = {'initiative_id': 'items-demo'}
    directory = Path.cwd() / '.artifacts' / 'prototype-to-production' / 'fixture'

    def require(self, condition, message):
        if not condition:
            raise AssertionError(message)


def _guide_fixture():
    feature = {'feature_id': 'items', 'spec_paths': [], 'backend_contract': {},
               'implementation_paths': ['src/items.py'],
               'states': ['success', 'forbidden'],
               'state_transitions': [
                   {'from': 'success', 'to': 'forbidden', 'trigger': 'member request',
                    'guard': 'role is not member', 'effect': 'return 403'},
                   {'from': 'forbidden', 'to': 'success', 'trigger': 'member request',
                    'guard': 'role is member', 'effect': 'return 200 item list'}],
               'acceptance_checks': [{'check_id': 'items-member', 'expected': '200 item list',
                                      'command': ['python', 'tests/items.py'],
                                      'state_expectations': {'success': '200 item list',
                                                             'forbidden': '403 forbidden'}}]}
    packet = {'slice_id': 'items-slice', 'title': 'Item membership', 'guide_format_version': 2,
              'guide_path': 'inputs/items/guide.md', 'proposal_revision': 1,
              'blueprint_version': 'spec-v1', 'blueprint_approval_id': 'approval-1', 'plan_version': 'plan-1',
              'source_snapshot_sha256': 'a' * 64, 'owner': 'builder', 'scope': 'items',
              'allowed_paths': ['src/items.py'], 'dependencies': [], 'prohibited_deviations': [],
              'feature_contracts': [feature], 'artifact_hashes': {'conversation/decision-1.json': 'b' * 64}, 'baseline_ids': [],
              'decision_records': [{'exchange_id': 'decision-1'}],
              'baseline_contract': {'features': []}, 'decision_ids': [],
              'traceability': [
                  {'feature_id': 'items', 'kind': 'feature', 'id': 'items', 'section': 'Why this slice exists', 'step': 'step-items'},
                  {'feature_id': 'items', 'kind': 'state', 'id': 'success', 'section': 'State and scenario matrix', 'step': 'step-items'},
                  {'feature_id': 'items', 'kind': 'state', 'id': 'forbidden', 'section': 'State and scenario matrix', 'step': 'step-items'},
                  {'feature_id': 'items', 'kind': 'check', 'id': 'items-member', 'section': 'Tests and acceptance evidence', 'step': 'step-items'},
              ]}
    parts = {
        'Identity and status': ('- slice_id: `items-slice`\n- proposal_revision: `1`\n- blueprint_version: `spec-v1`\n'
                                '- blueprint_approval_id: `approval-1`\n- plan_version: `plan-1`\n'
                                f'- source_snapshot_sha256: `{"a" * 64}`\n- owner: `builder`\n- scope: `items`\n'
                                '- title: `Item membership`\n- review_status: Pending review; not approved.\n'
                                '- package: Not assigned at guide freeze.\n- invalidated_revisions: None; this is revision 1.'),
        'In plain language': 'Members can see their items after signing in.',
        'Why this slice exists': 'The items feature preserves the approved member list behavior.',
        'What exists now': 'src/items.py contains the existing member item handler.',
        'Finished experience or service contract': 'A member receives the 200 item list response.',
        'Architecture and anatomy': 'Feature items owns src/items.py. The route checks membership before reading item storage.',
        'Detailed functional requirements': '1. items items-member: On a member request, check membership before every item list read.',
        'Visual specification': 'Not applicable — This slice implements the backend item service only.',
        'Interface specification': 'GET /items returns the approved member list response.',
        'Data and security rules': 'Keep the membership check before reads and retain the current item store.',
        'State and scenario matrix': ('| Feature | Check ID | State | Expected outcome |\n| --- | --- | --- | --- |\n'
                                     '| items | items-member | forbidden | 403 forbidden |\n'
                                     '| items | items-member | success | 200 item list |'),
        'Exact implementation footprint': ('| Path | Operation | Feature |\n| --- | --- | --- |\n'
                                           '| src/items.py | MODIFY | items |'),
        'Step-by-step implementation recipe': '1. `step-items` Update src/items.py and run the approved member checks. Expected result: the approved member request returns 200 item list.',
        'Dependencies and integration': 'No delivery prerequisites are required for this item slice.',
        'Worked examples and diagrams': ('A member request passes the guard and receives the item list. '
                                         '[Diagram](diagrams/items.mmd)'),
        'Traceability matrix': ('| Feature | Kind | ID | Section | Step |\n| --- | --- | --- | --- | --- |\n'
                                '| items | feature | items | Why this slice exists | step-items |\n'
                                '| items | state | success | State and scenario matrix | step-items |\n'
                                '| items | state | forbidden | State and scenario matrix | step-items |\n'
                                '| items | check | items-member | Tests and acceptance evidence | step-items |'),
        'Tests and acceptance evidence': ('Fixture sources: `tests/items.py`\n'
                                          '| Check ID | Status | Expected outcome | Command | Evidence |\n'
                                          '| --- | --- | --- | --- | --- |\n'
                                          '| items-member | planned | 200 item list | '
                                          '`["python", "tests/items.py"]` | Not run |'),
        'Fresh-session handoff': ('- First files to read: Read src/items.py and the approved item contract before editing.\n'
                                  '- Environment: Use Python 3.11 in the repository root.\n'
                                  '- Resume command: Run `python3 "$P2P_SKILL_ROOT/scripts/project_state.py" resume --project-root . --initiative-id items-demo --format json`.\n'
                                  '- Validation command: Run `python3 "$P2P_SKILL_ROOT/scripts/project_state.py" validate --project-root . --initiative-id items-demo`.\n'
                                  '- Current blockers: No blockers are recorded for this slice.\n'
                                  '- Next action: Inspect src/items.py and implement the approved membership behavior.\n'
                                  '- How to report findings: Record each result with its check ID and evidence path.'),
        'Safety, rollback and prohibited deviations': 'Rollback steps: Restore src/items.py from the prior revision if checks fail. Preserve the membership guard.',
        'Definition of done': 'The member receives 200 and a nonmember receives 403 in the approved checks.',
        'Change and approval history': ('Revision 1 records decision [decision-1](../../conversation/decision-1.json) '
                                        'with SHA-256 `' + 'b' * 64 + '`. Outcome: pending review. '
                                        'Exceptions: none. Invalidated revisions: none; this is the initial revision.'),
    }
    guide = '# Item membership\n\n' + '\n\n'.join(f'## {title}\n{body}' for title, body in parts.items())
    guide += ('\n\n### BACKEND annex — items\n'
              '- Inputs and outputs: GET /items returns the item list to the caller.\n'
              '- Authorization and validation: Require member access before checking the request.\n'
              '- Persistence and transactions: Read the current item store after authorization succeeds.\n'
              '- Concurrency and retries: Preserve current retry and concurrency behavior for reads.\n'
              '- Side effects and observability: This read operation creates no side effects and uses existing logs.\n'
              '- Compatibility and rollback: Keep the response shape and revert the isolated handler change.\n'
              '- Verification: Run the approved member and forbidden item checks.')
    diagram = ('stateDiagram-v2\n'
               '  success --> forbidden : member request, role is not member, return 403\n'
               '  forbidden --> success : member request, role is member, return 200 item list\n')
    guide += '\n\n```mermaid\n' + diagram + '```'
    assets = {'inputs/items/diagrams/items.mmd': diagram.encode()}
    return packet, guide, assets


def _v3_guide_fixture():
    packet, guide, assets = _guide_fixture()
    packet['guide_format_version'] = 3
    table = ('| Feature | Check ID | Expected outcome |\n| --- | --- | --- |\n'
             '| items | items-member | 200 item list |')
    guide = guide.replace('A member receives the 200 item list response.', table)
    guide = guide.replace('before every item list read.',
                          'before every item list read; expected outcome: 200 item list.')
    guide = guide.replace('`step-items` Update src/items.py', '`step-items` items-member Update src/items.py')
    return packet, guide, assets


class JsonSchemaValidationTests(unittest.TestCase):
    def test_rejects_unknown_schema_type(self):
        with self.assertRaisesRegex(ValueError, 'type'):
            validate_json_schema({'type': 'totally-invalid-json-schema'})

    def test_rejects_unsupported_keyword(self):
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            validate_json_schema({'type': 'object', 'mysteryKeyword': True})

    def test_checks_nested_schema_shape_and_types(self):
        schema = {
            'type': 'object', 'required': ['name'], 'additionalProperties': False,
            'properties': {'name': {'type': 'string', 'minLength': 1},
                           'count': {'type': 'integer', 'minimum': 0}},
        }
        validate_json_schema(schema)
        with self.assertRaisesRegex(ValueError, 'required'):
            validate_json_schema({'type': 'object', 'required': 'missing'})
        with self.assertRaisesRegex(ValueError, 'minimum'):
            validate_json_schema({'type': 'string', 'minimum': 'a'})

    def test_surface_hook_checks_each_approved_api_and_mcp_schema(self):
        feature = {'surface_contracts': {
            'api': {'Request and response schemas': {
                'request': {'type': 'object', 'properties': {'name': {'type': 'string'}}},
                'response': {'type': 'array', 'items': {'type': 'string'}},
            }},
            'mcp': {'Input and output schemas': {
                'input': {'type': 'object'}, 'output': {'type': 'not-a-json-type'},
            }},
        }}
        with self.assertRaisesRegex(ValueError, 'mcp'):
            validate_surface_contracts(feature)

    def test_surface_hook_validates_each_api_error_schema(self):
        with self.assertRaisesRegex(ValueError, 'errors.403'):
            validate_surface_contracts({'surface_contracts': {'api': {
                'Statuses and error schemas': {'errors': {'403': {'type': 'not-json-type'}}},
            }}})


class GuideContractTests(unittest.TestCase):
    def test_valid_structured_guide_is_accepted(self):
        packet, guide, assets = _guide_fixture()
        validate_guide(_Review(), packet, guide, assets)

    def test_v3_finished_experience_uses_only_exact_approved_outcome_rows(self):
        packet, guide, assets = _v3_guide_fixture()
        validate_guide(_Review(), packet, guide, assets)

    def test_v3_finished_experience_rejects_unstructured_contradictory_prose(self):
        packet, guide, assets = _v3_guide_fixture()
        guide = guide.replace('| items | items-member | 200 item list |',
                              '| items | items-member | 200 item list |\n\nA nonmember receives the 200 item list.')
        with self.assertRaisesRegex(AssertionError, 'only canonical approved outcome rows'):
            validate_guide(_Review(), packet, guide, assets)

    def test_v3_finished_experience_rejects_an_unapproved_check_outcome(self):
        packet, guide, assets = _v3_guide_fixture()
        guide = guide.replace('| items | items-member | 200 item list |',
                              '| items | items-member | 403 forbidden |')
        with self.assertRaisesRegex(AssertionError, 'Finished experience or service contract'):
            validate_guide(_Review(), packet, guide, assets)

    def test_v3_numbered_operation_and_recipe_preserve_approved_check_outcome(self):
        packet, guide, assets = _v3_guide_fixture()
        guide = guide.replace('expected outcome: 200 item list.', 'expected outcome: 403 forbidden.')
        with self.assertRaisesRegex(AssertionError, 'Detailed functional requirements'):
            validate_guide(_Review(), packet, guide, assets)

        packet, guide, assets = _v3_guide_fixture()
        guide = guide.replace('Expected result: the approved member request returns 200 item list.',
                              'Expected result: the approved member request returns 403 forbidden.')
        with self.assertRaisesRegex(AssertionError, 'Step-by-step implementation recipe'):
            validate_guide(_Review(), packet, guide, assets)

    def test_footprint_rejects_unowned_extra_operation(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('| src/items.py | MODIFY | items |',
                              '| src/items.py | MODIFY | items |\n| src/secret.py | DELETE | items |')
        with self.assertRaisesRegex(AssertionError, 'unowned paths'):
            validate_guide(_Review(), packet, guide, assets)

    def test_keep_feature_cannot_delete_its_preserved_source(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0].update(classification='KEEP', baseline_ids=['legacy-items'])
        guide = guide.replace('| src/items.py | MODIFY | items |',
                              '| src/items.py | DELETE | items |')
        with self.assertRaisesRegex(AssertionError, 'cannot DELETE preserved KEEP path'):
            validate_guide(_Review(), packet, guide, assets)

    def test_move_feature_may_delete_its_original_source_path(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0].update(classification='MOVE', baseline_ids=['legacy-items'])
        guide = guide.replace('| src/items.py | MODIFY | items |',
                              '| src/items.py | DELETE | items |')
        validate_guide(_Review(), packet, guide, assets)

    def test_delete_cannot_remove_path_preserved_by_a_feature_in_another_slice(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0]['classification'] = 'MOVE'
        guide = guide.replace('| src/items.py | MODIFY | items |', '| src/items.py | DELETE | items |')

        class Blueprint(_Review._Blueprint):
            def load(self, name, version):
                if name == 'blueprints':
                    return {'surface': 'backend', 'features': [{'classification': 'KEEP',
                            'implementation_paths': ['src/items.py']}]}
                return super().load(name, version)

        review = _Review()
        review.blueprint = Blueprint()
        with self.assertRaisesRegex(AssertionError, 'cannot DELETE preserved KEEP path'):
            validate_guide(review, packet, guide, assets)

    def test_handoff_rejects_undeclared_first_read_path(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('Read src/items.py and the approved item contract before editing.',
                              'Read src/missing_handoff_file.py before editing.')
        with self.assertRaisesRegex(AssertionError, 'First files to read.*unresolved'):
            validate_guide(_Review(), packet, guide, assets)

    def test_handoff_requires_at_least_one_grounded_first_read_file(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('Read src/items.py and the approved item contract before editing.',
                              'Read the approved item contract and repository guidance before editing.')
        with self.assertRaisesRegex(AssertionError, 'First files to read.*existing project or frozen file'):
            validate_guide(_Review(), packet, guide, assets)

    def test_handoff_rejects_declared_but_missing_first_read_path(self):
        packet, guide, assets = _guide_fixture()
        packet['allowed_paths'].append('src/planned.py')
        packet['feature_contracts'][0]['implementation_paths'].append('src/planned.py')
        guide = guide.replace('| src/items.py | MODIFY | items |',
                              '| src/items.py | MODIFY | items |\n| src/planned.py | CREATE | items |')
        guide = guide.replace('Read src/items.py and the approved item contract before editing.',
                              'Read src/planned.py before editing.')
        with self.assertRaisesRegex(AssertionError, 'First files to read.*unresolved'):
            validate_guide(_Review(), packet, guide, assets)

    def test_state_matrix_must_match_blueprint_outcomes(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('403 forbidden', '200 allowed')
        with self.assertRaisesRegex(AssertionError, 'State and scenario matrix'):
            validate_guide(_Review(), packet, guide, assets)

    def test_assigned_check_matrix_is_scoped_while_diagram_keeps_full_feature_states(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0]['acceptance_checks'][0]['state_expectations'] = {
            'success': '200 item list'}
        guide = guide.replace(
            '| items | items-member | forbidden | 403 forbidden |\n',
            '')
        guide = guide.replace(
            'State and scenario matrix\n| Feature',
            'State and scenario matrix\nThe transition diagram still documents the forbidden state.\n'
            '| Feature')
        validate_guide(_Review(), packet, guide, assets)

    def test_handoff_rejects_generic_see_above(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('Read src/items.py and the approved item contract before editing.', 'See above.')
        with self.assertRaisesRegex(AssertionError, 'First files to read'):
            validate_guide(_Review(), packet, guide, assets)

    def test_observed_pass_requires_frozen_run_receipt(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('| items-member | planned | 200 item list |',
                              '| items-member | observed | 200 item list |')
        guide = guide.replace('| `["python", "tests/items.py"]` | Not run |',
                              '| `["python", "tests/items.py"]` | evidence/run.json |')
        with self.assertRaisesRegex(AssertionError, 'frozen run receipt'):
            validate_guide(_Review(), packet, guide, assets)

    def test_duplicate_acceptance_rows_are_rejected_even_when_one_is_correct(self):
        packet, guide, assets = _guide_fixture()
        row = '| items-member | planned | 200 item list | `["python", "tests/items.py"]` | Not run |'
        guide = guide.replace(row, row.replace('200 item list', '403 forbidden') + '\n' + row)
        with self.assertRaisesRegex(AssertionError, 'each assigned check exactly once'):
            validate_guide(_Review(), packet, guide, assets)

    def test_reference_style_markdown_links_are_resolved(self):
        packet, guide, assets = _guide_fixture()
        guide += '\n\n[missing example][example]\n'
        guide += '\n[example]: examples/missing.json\n'
        with self.assertRaisesRegex(AssertionError, 'frozen asset/reference paths|linked file is absent'):
            validate_guide(_Review(), packet, guide, assets)

    def test_balanced_parentheses_in_markdown_destination_are_checked(self):
        packet, guide, assets = _v3_guide_fixture()
        guide += '\n\n[Worked example](examples/missing(v2).md)\n'
        self.assertIn('examples/missing(v2).md', markdown_links(guide))
        with self.assertRaisesRegex(AssertionError, 'missing from frozen asset/reference paths|linked file is absent'):
            validate_guide(_Review(), packet, guide, assets)

    def test_nested_markdown_rejects_missing_fragment_on_repeated_path(self):
        packet, guide, assets = _guide_fixture()
        guide += '\n\n[Examples](examples/outer.md)\n'
        assets['inputs/items/examples/outer.md'] = (
            b'# Examples\n[Existing](inner.md#current) [Missing](inner.md#missing)\n')
        assets['inputs/items/examples/inner.md'] = b'# Current\n'
        with self.assertRaisesRegex(AssertionError, 'linked Markdown file has no anchor'):
            validate_guide(_Review(), packet, guide, assets)

    def test_normal_replace_verb_is_not_a_placeholder(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('Members can see their items', 'Replace the membership guard and then members can see their items')
        validate_guide(_Review(), packet, guide, assets)

    def test_rejects_unrelated_guide_title(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('- title: `Item membership`', '- title: `Inventory database migration`')
        with self.assertRaisesRegex(AssertionError, 'title'):
            validate_guide(_Review(), packet, guide, assets)

    def test_h1_must_match_the_selected_proposal_title(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('# Item membership', '# Inventory database migration')
        with self.assertRaisesRegex(AssertionError, 'H1 title'):
            validate_guide(_Review(), packet, guide, assets)

    def test_rejects_claim_of_approval_at_freeze(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('Pending review; not approved.', 'Approved and implemented.')
        with self.assertRaisesRegex(AssertionError, 'review_status'):
            validate_guide(_Review(), packet, guide, assets)

    def test_rejects_unassigned_package_name(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('Not assigned at guide freeze.', 'package-999 is approved and locked.')
        with self.assertRaisesRegex(AssertionError, 'package'):
            validate_guide(_Review(), packet, guide, assets)

    def test_handoff_requires_exact_coordinator_commands(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('resume --project-root . --initiative-id items-demo --format json',
                              'Execute the workflow correctly')
        with self.assertRaisesRegex(AssertionError, 'Resume command'):
            validate_guide(_Review(), packet, guide, assets)

    def test_history_must_trace_approved_exchange_and_digest(self):
        packet, guide, assets = _guide_fixture()
        guide = guide.replace('decision-1](../../conversation/decision-1.json)',
                              'missing-decision](../../conversation/missing-decision.json)')
        with self.assertRaisesRegex(AssertionError, 'history|exchange|linked|frozen asset'):
            validate_guide(_Review(), packet, guide, assets)

    def test_mermaid_parser_rejects_unsupported_state_edge_syntax(self):
        packet, guide, assets = _guide_fixture()
        diagram = 'stateDiagram-v2\n  [*] --> success\n  success ---> forbidden\n  forbidden --> success\n'
        guide = re.sub(r'(?ms)(```mermaid\n).*?(```)', lambda m: m[1] + diagram + m[2], guide)
        assets['inputs/items/diagrams/items.mmd'] = diagram.encode()
        with self.assertRaisesRegex(AssertionError, 'Mermaid'):
            validate_guide(_Review(), packet, guide, assets)

    def test_state_diagram_must_match_approved_transitions(self):
        packet, guide, assets = _guide_fixture()
        diagram = 'stateDiagram-v2\n  success --> forbidden\n  forbidden --> forbidden\n'
        guide = re.sub(r'(?ms)(```mermaid\n).*?(```)', lambda m: m[1] + diagram + m[2], guide)
        assets['inputs/items/diagrams/items.mmd'] = diagram.encode()
        with self.assertRaisesRegex(AssertionError, 'transition'):
            validate_guide(_Review(), packet, guide, assets)

    def test_mermaid_parser_accepts_state_sequence_and_flowchart_diagrams(self):
        sources = [
            'stateDiagram-v2\n[*] --> A\nA --> B',
            'sequenceDiagram\nparticipant A\nparticipant B\nA->>B: request',
            'flowchart TD\nA[Start] --> B[Done]',
        ]
        parsed = _parse_mermaid(_Review(), sources, 'items-slice')
        self.assertEqual([item['type'] for item in parsed], ['stateDiagram', 'sequence', 'flowchart-v2'])
        self.assertEqual(parsed[1]['messages'], [['A', 'B', 'request']])
        self.assertEqual(parsed[1]['controls'], [])

    def test_state_parser_does_not_treat_prototype_property_as_pseudostate(self):
        parsed = _parse_mermaid(_Review(), ['stateDiagram-v2\nstate constructor\n'], 'items-slice')
        self.assertEqual(parsed[0]['states'], ['constructor'])

    def test_mermaid_parser_preserves_sequence_control_flow_and_positions(self):
        source = ('sequenceDiagram\nparticipant A\nparticipant B\nA->>B: request\n'
                  'alt authorized\nB-->>A: ok\nelse denied\nB-->>A: forbidden\nend\n'
                  'loop retry\nA->>B: again\nend\n'
                  'opt cached\nB-->>A: hit\nend\n'
                  'par store\nA->>B: write\nand notify\nB-->>A: event\nend\n')
        parsed = _parse_mermaid(_Review(), [source], 'items-slice')[0]
        self.assertEqual(parsed['controls'], [
            {'type': 'alt', 'action': 'start', 'position': 1, 'label': 'authorized'},
            {'type': 'alt', 'action': 'branch', 'position': 2, 'label': 'denied'},
            {'type': 'alt', 'action': 'end', 'position': 3, 'label': ''},
            {'type': 'loop', 'action': 'start', 'position': 3, 'label': 'retry'},
            {'type': 'loop', 'action': 'end', 'position': 4, 'label': ''},
            {'type': 'opt', 'action': 'start', 'position': 4, 'label': 'cached'},
            {'type': 'opt', 'action': 'end', 'position': 5, 'label': ''},
            {'type': 'par', 'action': 'start', 'position': 5, 'label': 'store'},
            {'type': 'par', 'action': 'branch', 'position': 6, 'label': 'notify'},
            {'type': 'par', 'action': 'end', 'position': 7, 'label': ''},
        ])

    def test_v3_sequence_contract_rejects_unapproved_branch_or_loop(self):
        packet, _, _ = _guide_fixture()
        packet['feature_contracts'][0]['state_transitions'] = []
        packet['guide_format_version'] = 3
        contract = packet['feature_contracts'][0]['sequence_contract'] = {
            'participants': ['A', 'B'],
            'messages': [{'from': 'A', 'to': 'B', 'label': 'request'},
                         {'from': 'B', 'to': 'A', 'label': 'ok'}],
            'controls': [],
        }
        source = ('sequenceDiagram\nparticipant A\nparticipant B\nA->>B: request\n'
                  'alt authorized\nB-->>A: ok\nend\n')
        parsed = _parse_mermaid(_Review(), [source], 'items-slice')
        with self.assertRaisesRegex(AssertionError, 'sequence diagrams differ'):
            _validate_diagram_contracts(_Review(), packet, parsed, 'items-slice')
        contract['controls'] = [
            {'type': 'alt', 'action': 'start', 'position': 1, 'label': 'authorized'},
            {'type': 'alt', 'action': 'end', 'position': 2, 'label': ''},
        ]
        _validate_diagram_contracts(_Review(), packet, parsed, 'items-slice')

    def test_legacy_sequence_contract_keeps_v2_control_flow_behavior(self):
        packet, _, _ = _guide_fixture()
        packet['feature_contracts'][0]['state_transitions'] = []
        packet['feature_contracts'][0]['sequence_contract'] = {
            'participants': ['A', 'B'],
            'messages': [{'from': 'A', 'to': 'B', 'label': 'request'},
                         {'from': 'B', 'to': 'A', 'label': 'ok'}],
        }
        source = ('sequenceDiagram\nparticipant A\nparticipant B\nA->>B: request\n'
                  'alt authorized\nB-->>A: ok\nend\n')
        parsed = _parse_mermaid(_Review(), [source], 'items-slice')
        _validate_diagram_contracts(_Review(), packet, parsed, 'items-slice')

    def test_sequence_contract_matches_parsed_participants_and_messages(self):
        packet, guide, assets = _guide_fixture()
        feature = packet['feature_contracts'][0]
        feature['sequence_contract'] = {
            'participants': ['Member', 'Items'],
            'messages': [{'from': 'Member', 'to': 'Items', 'label': 'GET /items'}],
        }
        source = 'sequenceDiagram\nparticipant Member\nparticipant Items\nMember->>Items: GET /items\n'
        guide += '\n\n[Sequence](diagrams/sequence.mmd)\n\n```mermaid\n' + source + '```'
        assets['inputs/items/diagrams/sequence.mmd'] = source.encode()
        validate_guide(_Review(), packet, guide, assets)

    def test_state_diagram_rejects_unapproved_extra_transition(self):
        packet, guide, assets = _guide_fixture()
        diagram = ('stateDiagram-v2\n'
                   '  success --> forbidden : member request, role is not member, return 403\n'
                   '  forbidden --> success : member request, role is member, return 200 item list\n'
                   '  success --> success : unexpected retry, always, repeat read\n')
        guide = re.sub(r'(?ms)(```mermaid\n).*?(```)', lambda m: m[1] + diagram + m[2], guide)
        assets['inputs/items/diagrams/items.mmd'] = diagram.encode()
        with self.assertRaisesRegex(AssertionError, 'unapproved transitions'):
            validate_guide(_Review(), packet, guide, assets)

    def test_sequence_contract_rejects_unapproved_participant_and_message(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0]['sequence_contract'] = {
            'participants': ['Member', 'Items'],
            'messages': [{'from': 'Member', 'to': 'Items', 'label': 'GET /items'}],
        }
        source = ('sequenceDiagram\nparticipant Member\nparticipant Items\nparticipant Logger\n'
                  'Member->>Items: GET /items\nItems->>Logger: write audit\n')
        guide += '\n\n[Sequence](diagrams/sequence.mmd)\n\n```mermaid\n' + source + '```'
        assets['inputs/items/diagrams/sequence.mmd'] = source.encode()
        with self.assertRaisesRegex(AssertionError, 'sequence diagrams differ|participants differ|messages differ'):
            validate_guide(_Review(), packet, guide, assets)

    def test_state_diagram_rejects_unapproved_state_and_transition(self):
        packet, guide, assets = _guide_fixture()
        diagram = ('stateDiagram-v2\n  success --> forbidden : member request, role is not member, return 403\n'
                   '  forbidden --> success : member request, role is member, return 200 item list\n'
                   '  success --> unapproved : change the result\n')
        guide = re.sub(r'(?ms)(```mermaid\n).*?(```)', lambda match: match[1] + diagram + match[2], guide)
        assets['inputs/items/diagrams/items.mmd'] = diagram.encode()
        with self.assertRaisesRegex(AssertionError, 'unapproved states'):
            validate_guide(_Review(), packet, guide, assets)

    def test_state_diagram_rejects_unapproved_standalone_state(self):
        packet, guide, assets = _guide_fixture()
        diagram = ('stateDiagram-v2\n  state success\n  state forbidden\n  state unapproved\n'
                   '  success --> forbidden : member request, role is not member, return 403\n'
                   '  forbidden --> success : member request, role is member, return 200 item list\n')
        guide = re.sub(r'(?ms)(```mermaid\n).*?(```)', lambda match: match[1] + diagram + match[2], guide)
        assets['inputs/items/diagrams/items.mmd'] = diagram.encode()
        with self.assertRaisesRegex(AssertionError, 'unapproved state'):
            validate_guide(_Review(), packet, guide, assets)

    def test_state_diagram_rejects_uncontracted_pseudostate_edge(self):
        packet, guide, assets = _guide_fixture()
        diagram = ('stateDiagram-v2\n  [*] --> success\n'
                   '  success --> forbidden : member request, role is not member, return 403\n'
                   '  forbidden --> success : member request, role is member, return 200 item list\n')
        guide = re.sub(r'(?ms)(```mermaid\n).*?(```)', lambda match: match[1] + diagram + match[2], guide)
        assets['inputs/items/diagrams/items.mmd'] = diagram.encode()
        with self.assertRaisesRegex(AssertionError, 'unapproved transitions'):
            validate_guide(_Review(), packet, guide, assets)

    def test_state_diagram_rejects_extra_behavior_in_approved_edge_label(self):
        packet, guide, assets = _guide_fixture()
        diagram = ('stateDiagram-v2\n'
                   '  success --> forbidden : member request, role is not member, return 403, log credentials\n'
                   '  forbidden --> success : member request, role is member, return 200 item list\n')
        guide = re.sub(r'(?ms)(```mermaid\n).*?(```)', lambda match: match[1] + diagram + match[2], guide)
        assets['inputs/items/diagrams/items.mmd'] = diagram.encode()
        with self.assertRaisesRegex(AssertionError, 'unapproved transitions'):
            validate_guide(_Review(), packet, guide, assets)

    def test_sequence_contract_preserves_message_order_and_multiplicity(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0]['sequence_contract'] = {
            'participants': ['Member', 'Items'],
            'messages': [
                {'from': 'Member', 'to': 'Items', 'label': 'GET /items'},
                {'from': 'Items', 'to': 'Member', 'label': '200 item list'},
            ],
        }
        source = ('sequenceDiagram\nparticipant Member\nparticipant Items\n'
                  'Items-->>Member: 200 item list\nMember->>Items: GET /items\n')
        guide += '\n\n[Sequence](diagrams/sequence.mmd)\n\n```mermaid\n' + source + '```'
        assets['inputs/items/diagrams/sequence.mmd'] = source.encode()
        with self.assertRaisesRegex(AssertionError, 'sequence diagrams differ'):
            validate_guide(_Review(), packet, guide, assets)

    def test_sequence_only_diagram_does_not_cover_approved_state_transitions(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0]['sequence_contract'] = {
            'participants': ['Member', 'Items'],
            'messages': [{'from': 'Member', 'to': 'Items', 'label': 'GET /items'}],
        }
        guide = re.sub(r'(?ms)```mermaid\n.*?```\s*', '', guide, count=1)
        guide = guide.replace('[Diagram](diagrams/items.mmd)', '[Diagram](diagrams/sequence.mmd)')
        del assets['inputs/items/diagrams/items.mmd']
        source = 'sequenceDiagram\nparticipant Member\nparticipant Items\nMember->>Items: GET /items\n'
        guide += '\n\n[Sequence](diagrams/sequence.mmd)\n\n```mermaid\n' + source + '```'
        assets['inputs/items/diagrams/sequence.mmd'] = source.encode()
        with self.assertRaisesRegex(AssertionError, 'state diagram'):
            validate_guide(_Review(), packet, guide, assets)

    def test_state_diagrams_match_full_contract_when_features_share_state_ids(self):
        packet, _, _ = _guide_fixture()
        first = packet['feature_contracts'][0]
        second = {**first, 'feature_id': 'other-items', 'state_transitions': [
            {'from': 'success', 'to': 'forbidden', 'trigger': 'admin request',
             'guard': 'role is not admin', 'effect': 'return 403'},
            {'from': 'forbidden', 'to': 'success', 'trigger': 'admin request',
             'guard': 'role is admin', 'effect': 'return 200 item list'},
        ]}
        packet['feature_contracts'].append(second)
        correct = ('stateDiagram-v2\n'
                   'success --> forbidden : admin request, role is not admin, return 403\n'
                   'forbidden --> success : admin request, role is admin, return 200 item list\n')
        original = ('stateDiagram-v2\n'
                    'success --> forbidden : member request, role is not member, return 403\n'
                    'forbidden --> success : member request, role is member, return 200 item list\n')
        parsed = _parse_mermaid(_Review(), [correct, original], 'items-slice')
        _validate_diagram_contracts(_Review(), packet, parsed, 'items-slice')

    def test_sequence_contract_preserves_participant_order(self):
        packet, guide, assets = _guide_fixture()
        packet['feature_contracts'][0]['sequence_contract'] = {
            'participants': ['Member', 'Items'],
            'messages': [{'from': 'Member', 'to': 'Items', 'label': 'GET /items'}],
        }
        source = 'sequenceDiagram\nparticipant Items\nparticipant Member\nMember->>Items: GET /items\n'
        guide += '\n\n[Sequence](diagrams/sequence.mmd)\n\n```mermaid\n' + source + '```'
        assets['inputs/items/diagrams/sequence.mmd'] = source.encode()
        with self.assertRaisesRegex(AssertionError, 'sequence diagrams differ'):
            validate_guide(_Review(), packet, guide, assets)

    def test_sequence_contract_requires_a_distinct_diagram_for_each_feature(self):
        packet, guide, assets = _guide_fixture()
        first = packet['feature_contracts'][0]
        first['sequence_contract'] = {
            'participants': ['Member', 'Items'],
            'messages': [{'from': 'Member', 'to': 'Items', 'label': 'GET /items'}],
        }
        second = {**first, 'feature_id': 'other-items', 'sequence_contract': {
            'participants': ['Items', 'Audit'],
            'messages': [{'from': 'Items', 'to': 'Audit', 'label': 'record list access'}],
        }}
        packet['feature_contracts'].append(second)
        sequence = ('sequenceDiagram\nparticipant Member\nparticipant Items\n'
                    'Member->>Items: GET /items\n')
        packet['feature_contracts'][0]['sequence_contract']['messages'].append(
            {'from': 'Items', 'to': 'Member', 'label': '200 item list'})
        sequence = sequence.replace('Member->>Items: GET /items',
                                    'Items-->>Member: 200 item list\nMember->>Items: GET /items')
        guide += '\n\n[Sequence](diagrams/sequence.mmd)\n\n```mermaid\n' + sequence + '```'
        assets['inputs/items/diagrams/sequence.mmd'] = sequence.encode()
        state_diagram = ('stateDiagram-v2\n'
                         'success --> forbidden : member request, role is not member, return 403\n'
                         'forbidden --> success : member request, role is member, return 200 item list\n')
        parsed = _parse_mermaid(_Review(), [state_diagram, state_diagram, sequence], 'items-slice')
        with self.assertRaisesRegex(AssertionError, 'sequence diagrams differ'):
            _validate_diagram_contracts(_Review(), packet, parsed, 'items-slice')

    def test_table_cells_preserve_escaped_pipe_delimiters(self):
        packet, guide, assets = _guide_fixture()
        check = packet['feature_contracts'][0]['acceptance_checks'][0]
        check['expected'] = '200 item list | filtered'
        check['command'] = ['python', 'tests/items|special.py']
        feature = packet['feature_contracts'][0]
        packet['feature_contracts'][0]['acceptance_checks'][0]['state_expectations']['success'] = '200 item list | filtered'
        guide = guide.replace('| items | items-member | success | 200 item list |',
                              r'| items | items-member | success | 200 item list \| filtered |')
        guide = guide.replace('| items-member | planned | 200 item list | `["python", "tests/items.py"]` | Not run |',
                              r'| items-member | planned | 200 item list \| filtered | `["python", "tests/items\|special.py"]` | Not run |')
        guide = guide.replace('Fixture sources: `tests/items.py`', 'Fixture sources: `tests/items|special.py`')
        validate_guide(_Review(), packet, guide, assets)


if __name__ == '__main__':
    unittest.main()
