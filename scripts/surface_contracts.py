"""Shared approved surface contract fields and JSON Schema checks."""

import re


SURFACE_CONTRACT_VERSION = 2
SURFACE_FIELDS = {
    'ui': ('Route and entry', 'Viewports and breakpoints', 'Layout and tokens', 'Copy and assets',
           'States and roles', 'Keyboard and assistive technology', 'Events and dependencies', 'Render evidence'),
    'backend': ('Inputs and outputs', 'Authorization and validation', 'Persistence and transactions',
                'Concurrency and retries', 'Side effects and observability', 'Compatibility and rollback', 'Verification'),
    'api': ('Method and route', 'Request and response schemas', 'Statuses and error schemas', 'Authentication and scope',
            'Validation and query options', 'Concurrency and timeouts', 'Side effects and compatibility', 'Verification'),
    'mcp': ('Server and transport', 'Registration and capabilities', 'Input and output schemas',
            'Permissions and consent', 'Protocol and tool errors', 'Cancellation and timeouts',
            'Streaming and data exposure', 'Client invocation and verification'),
    'script': ('Entry point and runtime', 'Flags and configuration', 'Environment and working directory',
               'Inputs and outputs', 'File mutations', 'Exit codes and streams', 'Recovery and concurrency', 'Verification'),
}
ANNEX_FIELDS = SURFACE_FIELDS
SCHEMA_DIALECTS = {
    'https://json-schema.org/draft/2020-12/schema',
    'https://json-schema.org/draft/2019-09/schema',
}
SCHEMA_TYPES = {'array', 'boolean', 'integer', 'null', 'number', 'object', 'string'}
SCHEMA_KEYWORDS = {
    '$schema', '$id', '$comment', 'title', 'description', 'type', 'enum', 'const',
    'properties', 'required', 'additionalProperties', 'items', 'minItems', 'maxItems',
    'minLength', 'maxLength', 'pattern', 'minimum', 'maximum', 'exclusiveMinimum',
    'exclusiveMaximum', 'multipleOf', 'minProperties', 'maxProperties', 'uniqueItems',
}
_PLACEHOLDER = re.compile(r'\b(?:REPLACE\b|REPLACE_|(?i:TBD|TODO|TBC|TO BE DETERMINED|TO BE CONFIRMED)\b)')


def validate_json_schema(schema, *, location='schema'):
    """Validate the deliberately small JSON Schema subset supported by the workflow."""
    if not isinstance(schema, dict):
        raise ValueError(f'{location} must be a JSON Schema object')
    unknown = set(schema) - SCHEMA_KEYWORDS
    if unknown:
        raise ValueError(f'{location} has unsupported JSON Schema keyword(s): {sorted(unknown)}')
    if '$schema' in schema and schema['$schema'] not in SCHEMA_DIALECTS:
        raise ValueError(f'{location} declares an unsupported JSON Schema dialect')
    for key in ('title', 'description', '$id', '$comment'):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError(f'{location}.{key} must be a string')
    if 'type' in schema:
        types = schema['type']
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or not types or any(not isinstance(t, str) or t not in SCHEMA_TYPES for t in types):
            raise ValueError(f'{location}.type must name a supported JSON type')
        if len(set(types)) != len(types):
            raise ValueError(f'{location}.type contains duplicate types')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise ValueError(f'{location}.enum must be a non-empty array')
    if 'properties' in schema:
        if not isinstance(schema['properties'], dict) or any(not isinstance(k, str) for k in schema['properties']):
            raise ValueError(f'{location}.properties must be an object')
        for name, child in schema['properties'].items():
            validate_json_schema(child, location=f'{location}.properties.{name}')
    if 'required' in schema:
        required = schema['required']
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ValueError(f'{location}.required must be an array of strings')
        if len(set(required)) != len(required):
            raise ValueError(f'{location}.required must not contain duplicates')
    if 'additionalProperties' in schema:
        additional = schema['additionalProperties']
        if not isinstance(additional, bool):
            validate_json_schema(additional, location=f'{location}.additionalProperties')
    if 'items' in schema:
        validate_json_schema(schema['items'], location=f'{location}.items')
    for keyword in ('minItems', 'maxItems', 'minLength', 'maxLength', 'minProperties', 'maxProperties'):
        if keyword in schema and (type(schema[keyword]) is not int or schema[keyword] < 0):
            raise ValueError(f'{location}.{keyword} must be a non-negative integer')
    for keyword in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'):
        if keyword in schema and (type(schema[keyword]) not in (int, float) or
                                  (keyword == 'multipleOf' and schema[keyword] <= 0)):
            raise ValueError(f'{location}.{keyword} must be a valid number')
    if 'pattern' in schema:
        if not isinstance(schema['pattern'], str):
            raise ValueError(f'{location}.pattern must be a string')
        try:
            re.compile(schema['pattern'])
        except re.error as error:
            raise ValueError(f'{location}.pattern is invalid: {error}') from error
    for low, high in (('minItems', 'maxItems'), ('minLength', 'maxLength'),
                      ('minProperties', 'maxProperties'), ('minimum', 'maximum'),
                      ('exclusiveMinimum', 'exclusiveMaximum')):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ValueError(f'{location}.{low} must not exceed {high}')
    return schema


def validate_surface_schemas(surface, contract):
    """Validate structured request/response/error schemas for API and MCP surfaces."""
    schema_fields = {
        'api': ('Request and response schemas', ('request', 'response')),
        'mcp': ('Input and output schemas', ('input', 'output')),
    }
    if surface in schema_fields:
        field, names = schema_fields[surface]
        value = contract.get(field)
        if not (isinstance(value, dict) and value.get('applicability') == 'not_applicable'):
            if not isinstance(value, dict) or not set(value) >= set(names):
                raise ValueError(f'{surface} {field} must contain {", ".join(names)} JSON Schemas')
            for name in names:
                validate_json_schema(value[name], location=f'{surface}.{name} schema')
    if surface == 'api':
        error_contract = contract.get('Statuses and error schemas')
        if not (isinstance(error_contract, dict) and error_contract.get('applicability') == 'not_applicable'):
            errors = error_contract.get('errors') if isinstance(error_contract, dict) else None
            if not isinstance(errors, dict) or not errors:
                raise ValueError('api Statuses and error schemas must contain an errors object keyed by HTTP status')
            for status, schema in errors.items():
                if not isinstance(status, str) or not re.fullmatch(r'[1-5][0-9]{2}', status):
                    raise ValueError(f'api error schema has invalid HTTP status: {status!r}')
                validate_json_schema(schema, location=f'api.errors.{status} schema')


def _concrete(value, location):
    if isinstance(value, str):
        if not value.strip() or value.strip().casefold() in {'n/a', 'na', 'not applicable'} or _PLACEHOLDER.search(value):
            raise ValueError(f'{location} needs a concrete approved value or grounded N/A')
        return
    if isinstance(value, (dict, list)) and value:
        if _PLACEHOLDER.search(str(value)):
            raise ValueError(f'{location} needs a concrete approved value or grounded N/A')
        return
    raise ValueError(f'{location} needs a concrete approved value or grounded N/A')


def _validate_value(value, location):
    if isinstance(value, dict) and value.get('applicability') == 'not_applicable':
        if set(value) != {'applicability', 'reason'}:
            raise ValueError(f'{location} N/A must contain only applicability and reason')
        reason = value.get('reason')
        if not isinstance(reason, str) or len(reason.strip()) < 20 or _PLACEHOLDER.search(reason):
            raise ValueError(f'{location} N/A needs a grounded reason of at least 20 characters')
        return
    _concrete(value, location)


def validate_surface_contracts(version, surfaces, contracts):
    """Validate v2 surface completeness; unversioned blueprints remain readable as legacy."""
    if version is None:
        return
    if version != SURFACE_CONTRACT_VERSION:
        raise ValueError(f'unsupported surface contract version: {version!r}')
    if not isinstance(contracts, dict) or set(contracts) != set(surfaces):
        raise ValueError('surface contracts must exactly cover all applicable surfaces')
    for surface in surfaces:
        fields = contracts.get(surface)
        if not isinstance(fields, dict) or set(fields) != set(SURFACE_FIELDS[surface]):
            missing = set(SURFACE_FIELDS[surface]) - set(fields or {})
            extra = set(fields or {}) - set(SURFACE_FIELDS[surface])
            raise ValueError(f'{surface} contract must cover every annex field; missing={sorted(missing)}, extra={sorted(extra)}')
        for field in SURFACE_FIELDS[surface]:
            _validate_value(fields[field], f'{surface}.{field}')
        validate_surface_schemas(surface, fields)


def validate_state_transitions(version, states, transitions):
    """Require explicit, machine-checkable transitions for every multi-state v2 feature."""
    if version is None:
        return
    if not isinstance(states, list) or not states or any(not isinstance(state, str) or not state.strip() for state in states):
        raise ValueError('feature states must be non-empty strings')
    if len(states) != len(set(states)):
        raise ValueError('feature states must be distinct')
    if transitions is None:
        transitions = []
    if not isinstance(transitions, list):
        raise ValueError('state_transitions must be an array')
    if len(states) > 1 and not transitions:
        raise ValueError('multi-state features require approved state_transitions')
    participating = set()
    observed = set()
    for index, transition in enumerate(transitions):
        location = f'state_transitions[{index}]'
        if not isinstance(transition, dict) or set(transition) != {'from', 'to', 'trigger', 'guard', 'effect'}:
            raise ValueError(f'{location} must contain from, to, trigger, guard and effect')
        source, target = transition['from'], transition['to']
        if source not in states or target not in states:
            raise ValueError(f'{location} must reference declared feature states')
        for key in ('trigger', 'guard', 'effect'):
            if not isinstance(transition[key], str):
                raise ValueError(f'{location}.{key} must be text')
            _concrete(transition[key], f'{location}.{key}')
        edge = (source, target, *(transition[key] for key in ('trigger', 'guard', 'effect')))
        if edge in observed:
            raise ValueError(f'{location} duplicates a state transition')
        observed.add(edge)
        participating.update((source, target))
    if len(states) > 1 and participating != set(states):
        raise ValueError('state_transitions must cover every declared feature state')


def validate_sequence_contract(sequence):
    """Validate the ordered Mermaid participants and messages approved for a feature."""
    if (not isinstance(sequence, dict)
            or set(sequence) not in ({'participants', 'messages'}, {'participants', 'messages', 'controls'})):
        raise ValueError('sequence_contract requires participants and messages')
    participants, messages = sequence['participants'], sequence['messages']
    if (not isinstance(participants, list) or not participants
            or not all(isinstance(name, str) and name.strip() for name in participants)
            or len(participants) != len(set(participants))):
        raise ValueError('sequence_contract participants must name distinct diagram IDs')
    if not isinstance(messages, list) or not messages:
        raise ValueError('sequence_contract needs ordered messages')
    for index, message in enumerate(messages):
        if (not isinstance(message, dict) or set(message) != {'from', 'to', 'label'}
                or message['from'] not in participants or message['to'] not in participants
                or not isinstance(message['label'], str)):
            raise ValueError(f'sequence_contract.messages[{index}] needs approved endpoints and label text')
        _concrete(message['label'], f'sequence_contract.messages[{index}].label')
    controls = sequence.get('controls', [])
    if not isinstance(controls, list):
        raise ValueError('sequence_contract.controls must be an ordered array')
    stack = []
    last_position = 0
    for index, control in enumerate(controls):
        location = f'sequence_contract.controls[{index}]'
        if (not isinstance(control, dict)
                or set(control) != {'type', 'action', 'position', 'label'}
                or control['type'] not in {'alt', 'loop', 'opt', 'par', 'par_over', 'critical', 'break'}
                or control['action'] not in {'start', 'branch', 'end'}
                or not isinstance(control['position'], int) or isinstance(control['position'], bool)
                or not last_position <= control['position'] <= len(messages)
                or not isinstance(control['label'], str)):
            raise ValueError(f'{location} must name an ordered Mermaid control event')
        if control['action'] == 'branch':
            if control['type'] not in {'alt', 'par', 'par_over', 'critical'} or not stack or stack[-1] != control['type']:
                raise ValueError(f'{location} must branch an open alt, par or critical control')
            _concrete(control['label'], f'{location}.label')
        elif control['action'] == 'start':
            _concrete(control['label'], f'{location}.label')
            stack.append(control['type'])
        else:
            if not stack or stack.pop() != control['type'] or control['label']:
                raise ValueError(f'{location} must close its matching open control')
        last_position = control['position']
    if stack:
        raise ValueError('sequence_contract.controls must close every control block')
