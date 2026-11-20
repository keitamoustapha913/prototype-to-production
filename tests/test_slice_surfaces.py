import unittest
import hashlib
import re
import tempfile
from pathlib import Path

from scripts.blueprint_state import BlueprintState
from scripts.surface_contracts import (
    ANNEX_FIELDS, validate_sequence_contract, validate_state_transitions, validate_surface_contracts,
)


def contract(surface, value='Concrete approved contract value.'):
    return {field: value for field in ANNEX_FIELDS[surface]}


class _DocumentApi:
    StateError = ValueError
    VERSION_ID = re.compile(r'^[a-z][a-z0-9-]{0,50}$')

    @staticmethod
    def artifact_file(directory, relative):
        return directory / relative

    @staticmethod
    def digest_of(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def atomic_text(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')


class SurfaceContractValidationTests(unittest.TestCase):
    def test_requires_every_field_for_each_applicable_surface(self):
        values = {'backend': contract('backend')}
        del values['backend']['Concurrency and retries']
        with self.assertRaisesRegex(ValueError, 'Concurrency and retries'):
            validate_surface_contracts(2, ['backend'], values)

    def test_accepts_grounded_not_applicable_value(self):
        values = contract('script')
        values['Recovery and concurrency'] = {
            'applicability': 'not_applicable',
            'reason': 'The script is read-only and has no persistent state to recover.',
        }
        validate_surface_contracts(2, ['script'], {'script': values})

    def test_rejects_unexplained_not_applicable_value(self):
        values = contract('api')
        values['Concurrency and timeouts'] = {
            'applicability': 'not_applicable',
            'reason': 'N/A',
        }
        with self.assertRaisesRegex(ValueError, 'grounded reason'):
            validate_surface_contracts(2, ['api'], {'api': values})

    def test_rejects_placeholder_contract_values(self):
        values = contract('mcp')
        values['Client invocation and verification'] = 'REPLACE exact client call'
        with self.assertRaisesRegex(ValueError, 'concrete approved value'):
            validate_surface_contracts(2, ['mcp'], {'mcp': values})

    def test_api_contract_requires_structured_request_response_and_error_schemas(self):
        values = contract('api')
        values['Request and response schemas'] = {
            'request': {'type': 'object'}, 'response': {'type': 'object'},
        }
        values['Statuses and error schemas'] = {'errors': {'403': {'type': 'object'}}}
        validate_surface_contracts(2, ['api'], {'api': values})
        values['Statuses and error schemas'] = 'Errors are returned as JSON.'
        with self.assertRaisesRegex(ValueError, 'errors object'):
            validate_surface_contracts(2, ['api'], {'api': values})

    def test_legacy_blueprint_surface_contracts_remain_readable(self):
        validate_surface_contracts(None, ['api'], {'api': {}})

    def test_multi_state_workflow_requires_explicit_transition_behavior(self):
        transitions = [{
            'from': 'success', 'to': 'forbidden', 'trigger': 'Caller requests protected data.',
            'guard': 'Caller lacks the required membership.', 'effect': 'Return 403 without reading data.',
        }]
        with self.assertRaisesRegex(ValueError, 'cover every declared feature state'):
            validate_state_transitions(2, ['success', 'forbidden', 'error'], transitions)
        transitions.append({
            'from': 'success', 'to': 'error', 'trigger': 'The store fails.',
            'guard': 'The read operation raises the approved error.', 'effect': 'Return a retryable error.',
        })
        validate_state_transitions(2, ['success', 'forbidden', 'error'], transitions)

    def test_transition_details_reject_placeholders(self):
        transitions = [{
            'from': 'ready', 'to': 'done', 'trigger': 'TBD',
            'guard': 'The operation is authorized.', 'effect': 'Return the result.',
        }]
        with self.assertRaisesRegex(ValueError, 'concrete approved value'):
            validate_state_transitions(2, ['ready', 'done'], transitions)

    def test_transition_details_require_labels_that_can_match_diagrams(self):
        transitions = [{
            'from': 'ready', 'to': 'done', 'trigger': ['Caller starts a read'],
            'guard': 'The operation is authorized.', 'effect': 'Return the result.',
        }]
        with self.assertRaisesRegex(ValueError, 'trigger must be text'):
            validate_state_transitions(2, ['ready', 'done'], transitions)

    def test_sequence_requires_declared_endpoints_and_concrete_labels(self):
        sequence = {'participants': ['Caller', 'Service'],
                    'messages': [{'from': 'Caller', 'to': 'Service', 'label': 'Read authorized items'}]}
        validate_sequence_contract(sequence)
        sequence['messages'][0]['to'] = 'Unknown'
        with self.assertRaisesRegex(ValueError, 'approved endpoints'):
            validate_sequence_contract(sequence)

    def test_sequence_controls_require_ordered_closed_branch_structure(self):
        sequence = {
            'participants': ['Caller', 'Service'],
            'messages': [{'from': 'Caller', 'to': 'Service', 'label': 'Read request'}],
            'controls': [
                {'type': 'alt', 'action': 'start', 'position': 0, 'label': 'Authorized'},
                {'type': 'alt', 'action': 'branch', 'position': 1, 'label': 'Denied'},
                {'type': 'alt', 'action': 'end', 'position': 1, 'label': ''},
            ],
        }
        validate_sequence_contract(sequence)
        sequence['controls'][2]['type'] = 'loop'
        with self.assertRaisesRegex(ValueError, 'matching open control'):
            validate_sequence_contract(sequence)


class BlueprintDocumentTests(unittest.TestCase):
    def generate(self, feature, *, surface_contract_version=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        state = BlueprintState(_DocumentApi(), directory, {})
        spec = {
            'version': 'spec-v2',
            'surface_contract_version': surface_contract_version,
            'document_paths': ['blueprints/spec-v2/index.md'],
            'features': [feature],
        }
        state.blueprint_documents(spec, {'baseline_kind': 'greenfield', 'requirements': []}, create=True)
        return (directory / feature['spec_paths'][0]).read_text(encoding='utf-8')

    def test_v2_markdown_shows_every_transition_and_surface_contract_value(self):
        backend = contract('backend')
        backend['Inputs and outputs'] = 'GET /items returns the member item array.'
        backend['Concurrency and retries'] = {
            'applicability': 'not_applicable',
            'reason': 'The read operation has no mutation to retry or deduplicate.',
        }
        api = contract('api')
        api['Request and response schemas'] = {
            'request': {'type': 'object', 'additionalProperties': False},
            'response': {'type': 'array', 'items': {'type': 'object'}},
        }
        api['Statuses and error schemas'] = {'errors': {'403': {'type': 'object'}}}
        transitions = [{
            'from': 'success', 'to': 'forbidden', 'trigger': 'An outsider requests the route.',
            'guard': 'The request has no active member role.', 'effect': 'Return 403 without reading data.',
        }]
        feature = {
            'feature_id': 'items', 'purpose': 'Members list items.',
            'anatomy': 'Route calls the item service.', 'behavior_contract': 'Authorize then read.',
            'spec_paths': ['blueprints/spec-v2/items.md'],
            'baseline_ids': ['items'], 'classification': 'KEEP', 'decision_ids': [],
            'states': ['success', 'forbidden'], 'state_transitions': transitions,
            'slice_surfaces': ['api'], 'surface_contracts': {'backend': backend, 'api': api},
        }
        markdown = self.generate(feature, surface_contract_version=2)
        for value in ('State transitions', 'An outsider requests the route.',
                      'The request has no active member role.', 'Return 403 without reading data.',
                      'Surface contracts', 'GET /items returns the member item array.',
                      'The read operation has no mutation to retry or deduplicate.',
                      'Request and response schemas', '"403"', '"additionalProperties": false'):
            self.assertIn(value, markdown)
        for surface in ('backend', 'api'):
            for field in ANNEX_FIELDS[surface]:
                self.assertIn(f'"{field}"', markdown)

    def test_legacy_markdown_bytes_keep_the_previous_json_layout(self):
        feature = {
            'feature_id': 'items', 'purpose': 'Members list items.',
            'anatomy': 'Route calls the item service.', 'behavior_contract': 'Authorize then read.',
            'spec_paths': ['blueprints/spec-v2/items.md'],
            'baseline_ids': ['items'], 'classification': 'KEEP', 'decision_ids': [],
            'states': ['success'], 'dependencies': [],
        }
        markdown = self.generate(feature)
        expected = (
            '# Feature contracts\n\n## items\n\nMembers list items.\n\n'
            '### Anatomy\n\nRoute calls the item service.\n\n'
            '### Behavior\n\nAuthorize then read.\n\n'
            '### Baseline ids\n\n~~~json\n[\n  "items"\n]\n~~~\n\n'
            '### Classification\n\n~~~json\n"KEEP"\n~~~\n\n'
            '### Decision ids\n\n~~~json\n[]\n~~~\n\n'
            '### Dependencies\n\n~~~json\n[]\n~~~\n\n'
            '### Starting baseline\n\n~~~json\n"New project / no A"\n~~~\n\n'
            '### States\n\n~~~json\n[\n  "success"\n]\n~~~\n'
        )
        self.assertEqual(markdown, expected)



if __name__ == '__main__':
    unittest.main()
