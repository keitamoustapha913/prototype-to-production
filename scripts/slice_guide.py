"""Mechanical validation for implementation guides and approved surface contracts.

Surface schemas accept JSON Schema Draft 2019-09 or 2020-12 keywords from
``SCHEMA_KEYWORDS`` only. The supported subset includes types, enums/constants,
object properties/required/additionalProperties, array items and size limits,
string length/pattern limits, numeric ranges/multiples, and descriptive metadata.
Unknown keywords, external references, combinators, and unsupported dialects fail
closed so a misspelled or unimplemented contract cannot silently pass review.
"""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
import posixpath
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit


@lru_cache(maxsize=1)
def _surface_contract_module():
    spec = importlib.util.spec_from_file_location(
        'p2p_surface_contracts', Path(__file__).with_name('surface_contracts.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GUIDE_SECTIONS = (
    'Identity and status', 'In plain language', 'Why this slice exists', 'What exists now',
    'Finished experience or service contract', 'Architecture and anatomy',
    'Detailed functional requirements', 'Visual specification', 'Interface specification',
    'State and scenario matrix', 'Data and security rules', 'Exact implementation footprint',
    'Step-by-step implementation recipe', 'Dependencies and integration', 'Worked examples and diagrams',
    'Traceability matrix', 'Tests and acceptance evidence', 'Safety, rollback and prohibited deviations',
    'Definition of done', 'Fresh-session handoff', 'Change and approval history',
)
ANNEX_FIELDS = _surface_contract_module().ANNEX_FIELDS
JSON_CHECKS = {
    'api': {'Request and response schemas': {'request', 'response'}, 'Statuses and error schemas': {'errors'}},
    'mcp': {'Input and output schemas': {'input', 'output'}},
}


def validate_json_schema(schema, *, location='schema'):
    """Use the shared approved surface schema validator."""
    return _surface_contract_module().validate_json_schema(schema, location=location)


def validate_surface_contracts(feature):
    """Raise ValueError when approved API or MCP schema fields are malformed."""
    helper = _surface_contract_module()
    for surface, field, names in (
        ('api', 'Request and response schemas', ('request', 'response')),
        ('mcp', 'Input and output schemas', ('input', 'output')),
    ):
        contract = feature.get('surface_contracts', {}).get(surface, {})
        if field in contract:
            schemas = contract[field]
            if not isinstance(schemas, dict) or not set(schemas) >= set(names):
                raise ValueError(f'{surface} {field} must contain {", ".join(names)} JSON Schemas')
            for name in names:
                helper.validate_json_schema(schemas[name], location=f'{surface}.{name} schema')
    errors = feature.get('surface_contracts', {}).get('api', {}).get('Statuses and error schemas')
    if errors is not None:
        if not isinstance(errors, dict) or not isinstance(errors.get('errors'), dict) or not errors['errors']:
            raise ValueError('api Statuses and error schemas must contain an errors object keyed by HTTP status')
        for status, schema in errors['errors'].items():
            if not isinstance(status, str) or not re.fullmatch(r'[1-5][0-9]{2}', status):
                raise ValueError(f'api error schema has invalid HTTP status: {status!r}')
            helper.validate_json_schema(schema, location=f'api.errors.{status} schema')


def markdown_sections(text, level=2):
    """Return heading content while ignoring headings inside fenced code blocks."""
    return {name: body for name, (_, body) in _sections_with_lines(text, level).items()}


def markdown_links(text):
    """Return inline and reference-style destinations, excluding literal brackets."""
    text = re.sub(r'(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', '', text)
    text = re.sub(r'`+[^`]*`+', '', text)
    definitions = {}
    definition = re.compile(r'^ {0,3}\[([^\]\n]+)\]:[ \t]*<?([^\s>]+)>?.*$', re.M)
    for match in definition.finditer(text):
        definitions[match[1].strip().casefold()] = match[2]
    text = definition.sub('', text)
    destinations, inline_spans = [], []
    inline_start = re.compile(r'!?\[[^\]\n]*\]\(')
    for match in inline_start.finditer(text):
        index = match.end()
        while index < len(text) and text[index].isspace():
            index += 1
        start = index
        if index < len(text) and text[index] == '<':
            end = text.find('>', index + 1)
            if end < 0 or '\n' in text[index:end]:
                continue
            destination = text[index + 1:end]
            index = end + 1
        else:
            depth = 0
            while index < len(text):
                char = text[index]
                if char == '\\' and index + 1 < len(text):
                    index += 2
                    continue
                if char.isspace() and depth == 0:
                    break
                if char == '(':
                    depth += 1
                elif char == ')':
                    if depth == 0:
                        break
                    depth -= 1
                index += 1
            if depth:
                continue
            destination = re.sub(r'\\([()])', r'\1', text[start:index])
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] in '\"\'':
            quote = text[index]
            index += 1
            while index < len(text) and text[index] != quote and text[index] != '\n':
                index += 1
            if index < len(text) and text[index] == quote:
                index += 1
            while index < len(text) and text[index].isspace():
                index += 1
        if index < len(text) and text[index] == ')':
            destinations.append(destination)
            inline_spans.append((match.start(), index + 1))
    for start, end in reversed(inline_spans):
        text = text[:start] + ' ' * (end - start) + text[end:]
    reference = re.compile(r'!?\[([^\]\n]+)\]\[([^\]\n]*)\]')
    for match in reference.finditer(text):
        label = (match[2] or match[1]).strip().casefold()
        if label not in definitions:
            raise ValueError(f'Missing Markdown link reference: {label}')
        destinations.append(definitions[label])
    text = reference.sub('', text)
    for match in re.finditer(r'(?<!\\)\[([^\]\n]+)\]', text):
        label = match[1].strip().casefold()
        if label in definitions:
            destinations.append(definitions[label])
    return destinations


def _fail(review, condition, message):
    review.require(condition, message)


def _table_rows(markdown):
    rows = []
    for line in markdown.splitlines():
        if line.lstrip().startswith('|'):
            cells = [cell.strip().replace(r'\|', '|').strip('`') for cell in _split_table_row(line)]
            if cells and not all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells):
                rows.append(cells)
    return rows


def _split_table_row(line):
    """Split a Markdown table row on unescaped pipes only."""
    text = line.strip()
    if text.startswith('|'):
        text = text[1:]
    if text.endswith('|') and not _is_escaped(text, len(text) - 1):
        text = text[:-1]
    cells, start = [], 0
    for index, char in enumerate(text):
        if char == '|' and not _is_escaped(text, index):
            cells.append(text[start:index])
            start = index + 1
    cells.append(text[start:])
    return cells


def _is_escaped(text, index):
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == '\\':
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _require_table(review, body, title, headers, expected, key):
    rows = _table_rows(body)
    _fail(review, bool(rows) and rows[0] == headers, f'{key}: {title} must use columns {headers}')
    actual = rows[1:] if rows else []
    _fail(review, actual == expected,
          f'{key}: {title} rows differ from the approved contract; expected {expected}, found {actual}')


def _check_handoff(review, body, packet, assets, key, *, strict):
    required = ('First files to read', 'Environment', 'Resume command', 'Validation command',
                'Current blockers', 'Next action', 'How to report findings')
    fields = dict(re.findall(r'^- ([^:\n]+):\s*(.+)$', body, re.M))
    for field in required:
        value = fields.get(field, '').strip()
        _fail(review, len(value) >= 12 and value.lower() not in {'see above', 'same as above', 'n/a'},
              f'{key}: Fresh-session handoff needs a substantive {field} field')
    first_reads = fields.get('First files to read', '')
    path_references = re.findall(r'(?<![\w.-])(?:[\w.-]+/)*[\w@().-]+\.[A-Za-z0-9]{1,12}(?![\w.-])',
                                 first_reads)
    _fail(review, bool(path_references),
          f'{key}: Fresh-session handoff First files to read must name at least one existing project or frozen file')
    guide_dir = posixpath.dirname(packet['guide_path'])
    for path in path_references:
        asset_path = posixpath.normpath(posixpath.join(guide_dir, path))
        frozen_path = path if path in packet.get('artifact_hashes', {}) else asset_path
        try:
            if path in assets or asset_path in assets:
                continue
            if frozen_path in packet.get('artifact_hashes', {}):
                review.api.artifact_file(review.directory, frozen_path)
            else:
                review.api.project_source_file(review.directory.parents[2].resolve(), path)
        except (review.api.StateError, OSError, RuntimeError, ValueError) as error:
            _fail(review, False,
                  f'{key}: Fresh-session handoff First files to read contains unresolved path {path}: {error}')
    if strict:
        initiative = review.manifest.get('initiative_id')
        qualifier = f' --initiative-id {initiative}' if initiative else ''
        resume = ('python3 "$P2P_SKILL_ROOT/scripts/project_state.py" resume --project-root .'
                  f'{qualifier} --format json')
        validate = ('python3 "$P2P_SKILL_ROOT/scripts/project_state.py" validate --project-root .'
                    f'{qualifier}')
        resume_text = fields.get('Resume command', '').strip().removeprefix('Run ').strip().rstrip('.').strip('`')
        _fail(review, resume_text == resume,
              f'{key}: Fresh-session handoff Resume command must be the exact coordinator resume command')
        command = fields.get('Validation command', '').strip().removeprefix('Run ').strip().rstrip('.').strip('`')
        _fail(review, command == validate,
              f'{key}: Fresh-session handoff Validation command must be the exact coordinator validate command')


def _check_placeholder(review, text, key):
    # Placeholder syntax is restricted to actual template sentinels. Normal verbs such as
    # "Replace the authorization guard" are valid implementation instructions.
    token = r'(?:TBD|TODO|FIXME|REPLACE(?:_ME)?)'
    markers = re.compile(
        rf'(?im)(?:\[\[{token}\]\]|<{token}>|'
        rf'(?:^\s*(?:[-*]\s*)?|:\s*)`?{token}`?\s*$|^\s*(?:[-*]\s*)?(?:TODO|FIXME):)')
    vague = re.compile(r'\b(?:make it intuitive|follow best practices|handle errors)\b', re.I)
    _fail(review, not markers.search(text) and not vague.search(text), f'{key}: guide contains an unfinished placeholder or vague instruction')


def validate_guide(review, packet, guide, assets):
    """Validate exact scope, state outcomes, evidence and linked local assets."""
    _validate_original_guide(review, packet, guide, assets)
    key = packet['slice_id']
    sections = markdown_sections(guide)
    mandatory = ('In plain language', 'State and scenario matrix', 'Exact implementation footprint',
                 'Tests and acceptance evidence', 'Fresh-session handoff')
    for heading in mandatory:
        _fail(review, heading in sections, f'{key}: implementation guide is missing section {heading}')
    _check_placeholder(review, guide, key)
    _check_handoff(review, sections['Fresh-session handoff'], packet, assets, key,
                   strict=packet.get('guide_format_version', 1) >= 2)

    allowed = set(packet.get('allowed_paths', []))
    footprint = _table_rows(sections['Exact implementation footprint'])
    expected_paths = sorted(allowed)
    actual_paths = [row[0] for row in footprint[1:] if len(row) == 3] if footprint else []
    _fail(review, bool(footprint) and footprint[0] == ['Path', 'Operation', 'Feature'],
          f'{key}: Exact implementation footprint needs Path, Operation and Feature columns')
    _fail(review, all(len(row) == 3 for row in footprint[1:]) and
          all(line.lstrip().startswith('|') for line in sections['Exact implementation footprint'].splitlines()
              if line.strip()), f'{key}: Exact implementation footprint allows only canonical table rows')
    _fail(review, sorted(actual_paths) == expected_paths,
          f'{key}: implementation footprint has missing or unowned paths; expected {expected_paths}, found {actual_paths}')
    kept_paths = set()
    if any(row[1].upper() == 'DELETE' for row in footprint[1:]):
        approved = review.blueprint.load('blueprints', packet['blueprint_version'])
        kept_features = [feature for feature in approved.get('features', packet['feature_contracts'])
                         if feature.get('classification') == 'KEEP']
        kept_paths.update(path for feature in kept_features for path in feature.get('implementation_paths', []))
        kept_baseline_ids = {item for feature in kept_features for item in feature.get('baseline_ids', [])}
        kept_paths.update(anchor['path'] for baseline in packet['baseline_contract'].get('features', [])
                          if baseline['feature_id'] in kept_baseline_ids for anchor in baseline.get('source_anchors', []))
    for row in footprint[1:]:
        if len(row) != 3:
            continue
        path, operation, feature_id = row
        _fail(review, operation.upper() in {'CREATE', 'MODIFY', 'DELETE', 'VERIFY'},
              f'{key}: unsupported operation {operation!r} for {path}')
        _fail(review, feature_id in {f['feature_id'] for f in packet['feature_contracts']},
              f'{key}: unowned operation for {path} assigned to unknown feature {feature_id}')
        feature = next((f for f in packet['feature_contracts'] if f['feature_id'] == feature_id), {})
        feature_paths = feature.get('implementation_paths', [])
        _fail(review, any(review.api.ownership_contains(owner, path) for owner in feature_paths),
              f'{key}: feature {feature_id} does not own implementation path {path}')
        _fail(review, operation.upper() != 'DELETE' or not any(
            review.api.ownership_overlaps(kept_path, path) for kept_path in kept_paths),
            f'{key}: cannot DELETE preserved KEEP path {path} for feature {feature_id}')

    expected_outcomes = []
    for feature in packet['feature_contracts']:
        feature_id = feature['feature_id']
        for check in feature['acceptance_checks']:
            for state, outcome in sorted(check.get('state_expectations', {}).items()):
                expected_outcomes.append([feature_id, check['check_id'], state, str(outcome)])
        for case in feature.get('visual_contract', {}).get('render_cases', []):
            viewport = case['viewport']
            size = f'{viewport.get("width", "?")}x{viewport.get("height", "?")}'
            outcome = f'renders {case["route"]} at {size} with {case["ready_selector"]}'
            expected_outcomes.append([feature_id, case['case_id'], case['state'], outcome])
    matrix = sections['State and scenario matrix']
    _require_table(review, matrix, 'State and scenario matrix',
                   ['Feature', 'Check ID', 'State', 'Expected outcome'], expected_outcomes, key)

    tests = sections['Tests and acceptance evidence']
    evidence_rows = _table_rows(tests)
    _fail(review, bool(evidence_rows) and evidence_rows[0] ==
          ['Check ID', 'Status', 'Expected outcome', 'Command', 'Evidence'],
          f'{key}: Tests and acceptance evidence needs a planned/observed evidence table')
    expected_checks = {c['check_id']: c for f in packet['feature_contracts'] for c in f['acceptance_checks']}
    rows = [row for row in evidence_rows[1:] if len(row) == 5] if evidence_rows else []
    check_ids = [row[0] for row in rows]
    _fail(review, len(rows) == len(expected_checks) and set(check_ids) == set(expected_checks)
          and len(check_ids) == len(set(check_ids)),
          f'{key}: test evidence must include each assigned check exactly once')
    evidence = {row[0]: row for row in rows}
    for check_id, check in expected_checks.items():
        row = evidence.get(check_id)
        if not row:
            continue
        _fail(review, row[2] == check['expected'], f'{key}: test evidence expected output differs for {check_id}')
        _fail(review, row[3] == json.dumps(check.get('command', []), ensure_ascii=False),
              f'{key}: test evidence command differs for {check_id}')
        status, receipt = row[1].lower(), row[4]
        _fail(review, status in {'planned', 'observed'}, f'{key}: {check_id} status must be planned or observed')
        if status == 'planned':
            _fail(review, receipt.lower() in {'not run', 'not run yet'},
                  f'{key}: planned check {check_id} cannot claim a run result')
        else:
            _validate_run_receipt(review, packet, check_id, check, receipt, assets, key)

    if packet.get('guide_format_version', 1) >= 2:
        _validate_guide_specificity(review, packet, sections, key)
    if packet.get('guide_format_version', 1) >= 2:
        _validate_change_history(review, packet, sections['Change and approval history'], key)

    folder = posixpath.dirname(packet['guide_path'])
    _validate_reachable_links(review, packet, guide, assets, folder, key, set())


def _validate_original_guide(review, packet, guide, assets):
    key = packet['slice_id']
    sections = _sections_with_lines(guide, 2)
    missing = [heading for heading in GUIDE_SECTIONS if heading not in sections]
    _fail(review, not missing, f'{key}: implementation-guide.md missing sections: {", ".join(missing)}')
    for heading, (line, content) in sections.items():
        _fail(review, bool(content) and (not content.startswith('Not applicable') or
              re.fullmatch(r'Not applicable — \S.{8,}', content)),
              f'{key}: empty section or unexplained N/A in {heading}, line {line}')
    for heading in set(GUIDE_SECTIONS) - {'Visual specification', 'Interface specification'}:
        _fail(review, not sections[heading][1].startswith('Not applicable'), f'{key}: {heading} is required')
    _check_placeholder(review, guide, key)
    identity = sections['Identity and status'][1]
    for field in ('slice_id', 'proposal_revision', 'blueprint_version', 'blueprint_approval_id',
                  'plan_version', 'source_snapshot_sha256', 'owner', 'scope'):
        value = str(packet[field])
        _fail(review, re.search(rf'^- {field}: `?{re.escape(value)}`?$', identity, re.M),
              f'{key}: Identity and status must state {field} = {value}')
    if packet.get('guide_format_version', 1) >= 2:
        h1 = re.findall(r'^# (.+)$', guide, re.M)
        _fail(review, h1 == [packet.get('title')],
              f'{key}: guide H1 title must match the selected proposal title')
        _fail(review, re.search(rf'^- title: `?{re.escape(packet.get("title", ""))}`?$', identity, re.M),
              f'{key}: Identity and status title must match the selected proposal')
        _fail(review, re.search(r'^- review_status: Pending review; not approved\.$', identity, re.M),
              f'{key}: review_status must accurately state pending review at freeze')
        _fail(review, re.search(r'^- package: Not assigned at guide freeze\.$', identity, re.M),
              f'{key}: package must state that no package is assigned at guide freeze')
    for feature in packet['feature_contracts']:
        for reference in feature['spec_paths']:
            _fail(review, reference in packet['artifact_hashes'],
                  f'{key}: Identity and status needs the approved feature document {reference}')
    current = sections['What exists now'][1]
    for feature in packet['baseline_contract'].get('features', []):
        if feature['feature_id'] in packet['baseline_ids']:
            for anchor in feature.get('source_anchors', []):
                _fail(review, anchor['path'] in current and anchor['contains'] in current,
                      f'{key}: What exists now omits approved source anchor {anchor["path"]}: {anchor["contains"]}')
    for field, section in (('allowed_paths', 'Exact implementation footprint'),
                           ('dependencies', 'Dependencies and integration'),
                           ('prohibited_deviations', 'Safety, rollback and prohibited deviations')):
        for item in packet[field]:
            _fail(review, item in sections[section][1], f'{key}: {section} omits {item}')
    diagrams_by_path = {path: data for path, data in assets.items() if path.endswith('.mmd')}
    diagrams = re.findall(r'^```mermaid\s*\n(.*?)^```\s*$', guide, re.M | re.S)
    _fail(review, bool(diagrams) and bool(diagrams_by_path), f'{key}: include a Mermaid diagram and linked Mermaid source')
    strict_guide = packet.get('guide_format_version', 1) >= 2
    parsed_diagrams = _parse_mermaid(review, diagrams, key) if strict_guide else []
    if not strict_guide:
        for diagram in diagrams:
            _fail(review, _mermaid_lines(diagram), f'{key}: invalid or unsupported Mermaid syntax')
    for diagram in diagrams:
        _fail(review, any(diagram.strip() == (data.decode() if isinstance(data, bytes) else data).strip()
                          for data in diagrams_by_path.values()),
              f'{key}: Mermaid diagram has no matching source asset')
    stateful = any(len(feature['states']) > 1 for feature in packet['feature_contracts'])
    _fail(review, not stateful or any(d.lstrip().startswith(('stateDiagram-v2', 'sequenceDiagram')) for d in diagrams),
          f'{key}: multi-state guides require a state or sequence diagram')
    _validate_legacy_links(review, packet, guide, assets, diagrams_by_path)
    if strict_guide:
        _validate_diagram_contracts(review, packet, parsed_diagrams, key)
    elif stateful:
        normalized = ''.join(re.sub(r'[^a-z0-9]', '', diagram.lower()) for diagram in diagrams)
        for state in {state for feature in packet['feature_contracts'] for state in feature['states']}:
            expected = re.sub(r'[^a-z0-9]', '', state.lower())
            _fail(review, expected in normalized, f'{key}: Worked examples and diagrams omit the approved state {state}')
    _validate_traceability(review, packet, sections)
    _validate_annexes(review, packet, guide)
    for feature in packet['feature_contracts']:
        for check in feature['acceptance_checks']:
            tests = sections['Tests and acceptance evidence'][1]
            _fail(review, check['check_id'] in tests,
                  f'{key}: Tests and acceptance evidence omits {check["check_id"]}')
            argv = check.get('command')
            if argv:
                arrays = []
                for row in _table_rows(tests)[1:]:
                    if len(row) == 5:
                        try:
                            arrays.append(json.loads(row[3].strip('`')))
                        except ValueError:
                            continue
                _fail(review, argv in arrays, f'{key}: Tests and acceptance evidence omits argv for {check["check_id"]}')


def _sections_with_lines(text, level):
    sections, current, fence = {}, None, None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            fence = token if fence is None else None if token[0] == fence[0] and len(token) >= len(fence) else fence
        heading = re.fullmatch(rf'{"#" * level} (.+)', line) if not fence and not marker else None
        if heading:
            current = heading[1].strip()
            if current in sections:
                raise ValueError(f'Duplicate heading {current} at line {number}')
            sections[current] = (number, [])
        elif current is not None:
            sections[current][1].append(line)
    if fence:
        raise ValueError('Unclosed Markdown fence')
    return {name: (number, '\n'.join(lines).strip()) for name, (number, lines) in sections.items()}


def _parse_mermaid(review, diagrams, key):
    if not diagrams:
        return []
    script = Path(__file__).with_name('parse_mermaid.mjs')
    try:
        result = subprocess.run(['node', str(script)], input=json.dumps(diagrams), text=True,
                                capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        _fail(review, False, f'{key}: Mermaid parser could not run; install pinned skill dependencies with npm ci: {error}')
        return []
    if result.returncode:
        _fail(review, False, f'{key}: Mermaid parser rejected a diagram: {result.stderr.strip() or result.stdout.strip()}')
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        _fail(review, False, f'{key}: Mermaid parser returned invalid structured output: {error}')
        return []


def blueprint_state_diagrams(review, spec):
    """Round-trip state diagram sources through the slice guide validator."""
    if not spec.get('surface_contract_version'):
        return {}
    features = [feature for feature in spec['features'] if feature.get('state_transitions')]
    sources = {}
    for feature in features:
        ids = {state: f'p2p_state_{index}' for index, state in enumerate(feature['states'], 1)}
        lines = ['stateDiagram-v2']
        for state, internal_id in ids.items():
            label = state.replace('#', '#35;').replace('"', '#quot;')
            lines.append(f'    state "{label}" as {internal_id}')
        for edge in feature['state_transitions']:
            label = ', '.join(' '.join(edge[field].split()) for field in ('trigger', 'guard', 'effect'))
            lines.append(f"    {ids[edge['from']]} --> {ids[edge['to']]} : {label}")
        sources[feature['feature_id']] = '\n'.join(lines) + '\n'
    key = f"Blueprint {spec.get('version', '')} state diagrams ({', '.join(sources)})"
    parsed = _parse_mermaid(review, list(sources.values()), key)
    _fail(review, len(parsed) == len(features), f'{key}: Mermaid parser output is incomplete')
    for feature, diagram in zip(features, parsed):
        packet = {'feature_contracts': [feature]}
        _validate_diagram_contracts(review, packet, [diagram], feature['feature_id'],
                                    state_only=True)
    return sources


def _validate_diagram_contracts(review, packet, parsed, key, *, state_only=False):
    features = {feature['feature_id']: feature for feature in packet['feature_contracts']}
    state_diagrams = [diagram for diagram in parsed if diagram.get('type') == 'stateDiagram']
    sequence_diagrams = [diagram for diagram in parsed if diagram.get('type') == 'sequence']
    sequence_contracts = ([] if state_only else
                          [feature for feature in features.values() if feature.get('sequence_contract')])
    state_contracts = [feature for feature in features.values() if feature.get('state_transitions')]
    matched_state_features = set()
    if state_diagrams:
        state_features = [feature for feature in features.values() if feature.get('states')]
        _fail(review, len(state_diagrams) <= len(state_features),
              f'{key}: Mermaid state diagrams must map to approved feature states')
        unmatched = list(state_diagrams)
        for feature in state_features:
            expected_edges = feature.get('state_transitions', [])
            state_match = next((diagram for diagram in unmatched
                                if set(diagram.get('states', [])) == set(feature['states'])
                                and _state_edges_match(expected_edges,
                                                       diagram.get('state_edges', []))), None)
            if state_match is not None:
                unmatched.remove(state_match)
                matched_state_features.add(feature['feature_id'])
        if unmatched:
            state_sets = [set(feature['states']) for feature in state_features]
            same_states = any(set(diagram.get('states', [])) == expected
                              for diagram in unmatched for expected in state_sets)
            problem = 'unapproved transitions' if same_states else 'unapproved states'
            _fail(review, False,
                  f'{key}: Mermaid state diagrams contain {problem} or do not match an approved '
                  'feature contract')

    if sequence_diagrams or sequence_contracts:
        _fail(review, len(sequence_diagrams) == len(sequence_contracts),
              f'{key}: Mermaid sequence diagrams differ from the approved feature contracts')
        unmatched = list(sequence_diagrams)
        for feature in sequence_contracts:
            contract = feature['sequence_contract']
            expected_messages = [[message['from'], message['to'], message['label']]
                                 for message in contract['messages']]
            compare_controls = packet.get('guide_format_version', 1) >= 3 or 'controls' in contract
            match = next((diagram for diagram in unmatched
                          if diagram.get('participants', []) == contract['participants']
                          and diagram.get('messages', []) == expected_messages
                          and (not compare_controls
                               or diagram.get('controls', []) == contract.get('controls', []))), None)
            _fail(review, match is not None,
                  f'{key}: Mermaid sequence diagrams differ from the approved feature contract '
                  f'for {feature["feature_id"]}')
            if match is not None:
                unmatched.remove(match)
    for feature in state_contracts:
        _fail(review, feature['feature_id'] in matched_state_features,
              f'{key}: approved state transitions for {feature["feature_id"]} need an exact '
              'state diagram')
    for feature in features.values():
        if not state_diagrams and len(feature.get('states', [])) > 1:
            sequence = feature.get('sequence_contract', {})
            _fail(review, bool(sequence.get('participants') and sequence.get('messages')),
                  f'{key}: a sequence-only diagram needs an approved structured sequence contract for '
                  f'{feature["feature_id"]}')


def _state_edges_match(transitions, edges):
    if len(transitions) != len(edges):
        return False
    unmatched = list(edges)
    for transition in transitions:
        source, target = transition['from'], transition['to']
        expected_parts = [' '.join(str(transition[field]).split()).casefold()
                          for field in ('trigger', 'guard', 'effect')]
        expected_labels = {', '.join(expected_parts), ' / '.join(expected_parts)}
        match = next((edge for edge in unmatched
                      if edge[0] == source and edge[1] == target
                      and ' '.join(edge[2].split()).casefold() in expected_labels), None)
        if match is None:
            return False
        unmatched.remove(match)
    return not unmatched


def _validate_legacy_links(review, packet, guide, assets, diagrams):
    linked, anchors = set(), {heading_id(h) for h in re.findall(r'^#{1,6} (.+)$', guide, re.M)}
    folder = posixpath.dirname(packet['guide_path'])
    for link in markdown_links(guide):
        parsed = urlsplit(link.strip('<>'))
        if parsed.scheme or parsed.netloc:
            _fail(review, parsed.scheme in {'https', 'http'}, f'Unsupported guide link: {link}')
            continue
        if not parsed.path:
            _fail(review, unquote(parsed.fragment) in anchors, f'Missing guide anchor: {link}')
            continue
        path = posixpath.normpath(posixpath.join(folder, unquote(parsed.path)))
        _fail(review, path in assets or path in packet['artifact_hashes'],
              f'Guide link is missing from frozen asset/reference paths: {link}')
        linked.add(path)
        if parsed.fragment and path.endswith('.md'):
            content = _asset_text(review, path, assets)
            headings = re.findall(r'^#{1,6} (.+)$', content, re.M)
            _fail(review, unquote(parsed.fragment) in {heading_id(h) for h in headings},
                  f'Missing linked Markdown anchor: {link}')
    _fail(review, set(diagrams) <= linked, 'Worked examples must link all Mermaid source assets')
    for feature in packet['feature_contracts']:
        _fail(review, set(feature['spec_paths']) <= linked,
              f'{feature["feature_id"]}: link each approved feature document from the guide')


def heading_id(heading):
    return re.sub(r'[^\w -]', '', heading.lower()).replace(' ', '-')


def _asset_text(review, path, assets):
    if path in assets:
        data = assets[path]
        return data.decode() if isinstance(data, bytes) else data
    return review.api.artifact_file(review.directory, path).read_text()


def _validate_traceability(review, packet, sections):
    key, required = packet['slice_id'], set()
    mappings = review.blueprint.load('blueprint_coverage', packet['blueprint_version'])['mappings']
    for feature in packet['feature_contracts']:
        f = feature['feature_id']
        required.add((f, 'feature', f))
        required.update((f, 'state', state) for state in feature['states'])
        required.update((f, 'check', check['check_id']) for check in feature['acceptance_checks'])
        required.update((f, 'baseline', m['baseline_id']) for m in mappings if m['feature_id'] == f)
        required.update((f, 'decision', decision) for decision in packet['decision_ids'])
    trace = packet['traceability']
    valid = isinstance(trace, list) and all(isinstance(t, dict) and
            set(t) == {'feature_id', 'kind', 'id', 'section', 'step'} and
            all(isinstance(value, str) for value in t.values()) for t in trace)
    _fail(review, valid, f'{key}: Traceability matrix needs feature_id, kind, id, section and step for each row')
    found = {(t['feature_id'], t['kind'], t['id']) for t in trace}
    _fail(review, found == required and len(found) == len(trace),
          f'{key}: Traceability matrix missing IDs {sorted(required - found)}; unknown IDs {sorted(found - required)}')
    recipe, body = sections['Step-by-step implementation recipe'][1], sections['Traceability matrix'][1]
    rows = [[cell.strip().strip('`') for cell in row] for row in _table_rows(body)]
    for item in trace:
        section, step, feature_id, ident = item['section'], item['step'], item['feature_id'], item['id']
        _fail(review, section in sections and section != 'Traceability matrix' and
              re.search(rf'(?<![\w-]){re.escape(ident)}(?![\w-])', sections[section][1]),
              f'{key}: {feature_id}/{ident} missing from guide section {section}')
        _fail(review, re.search(rf'^\d+[.)] `?{re.escape(step)}`?(?:\s|:)', recipe, re.M),
              f'{key}: {feature_id}/{ident} refers to missing recipe step {step}')
        _fail(review, [feature_id, item['kind'], ident, section, step] in rows,
              f'{key}: {feature_id}/{ident} missing from Markdown Traceability matrix')


def _validate_change_history(review, packet, history, key):
    lowered = history.casefold()
    for record in packet.get('decision_records', []):
        exchange_id = record.get('exchange_id')
        if not exchange_id:
            continue
        _fail(review, exchange_id in history,
              f'{key}: Change and approval history must list linked exchange {exchange_id}')
        paths = [path for path in packet.get('artifact_hashes', {}) if exchange_id in path]
        _fail(review, bool(paths) and any(f'[{exchange_id}]' in history or path in history for path in paths),
              f'{key}: Change and approval history must link exchange {exchange_id}')
    for path, digest in packet.get('artifact_hashes', {}).items():
        _fail(review, digest in history,
              f'{key}: Change and approval history must include SHA-256 for {path}')
    approval = review.blueprint.manifest.get('approvals', {}).get(packet.get('blueprint_approval_id'), {})
    exceptions = approval.get('exceptions', [])
    if exceptions:
        for exception in exceptions:
            _fail(review, exception in history,
                  f'{key}: Change and approval history omits approved exception: {exception}')
    else:
        _fail(review, re.search(r'exceptions:\s*none\b', lowered) is not None,
              f'{key}: Change and approval history must state the exact empty exception list')
    _fail(review, re.search(r'(?i)\bOutcome:\s*pending review\b', history),
          f'{key}: Change and approval history must state the exact freeze-time outcome')
    _fail(review, 'invalidated revisions:' in lowered,
          f'{key}: Change and approval history must include invalidated revisions')
    if packet.get('proposal_revision', 1) == 1:
        _fail(review, 'initial revision' in lowered and 'invalidated revisions: none' in lowered,
              f'{key}: initial history must state that no previous revision was invalidated')
    else:
        previous = packet['proposal_revision'] - 1
        _fail(review, 'invalidated revisions: none' not in lowered and
              f'invalidated revisions: revision {previous}' in lowered,
              f'{key}: revised history must identify the invalidated prior revision')


def _validate_guide_specificity(review, packet, sections, key):
    architecture = sections['Architecture and anatomy']
    behavior = sections['Detailed functional requirements']
    rollback = sections['Safety, rollback and prohibited deviations']
    recipe = sections['Step-by-step implementation recipe']
    test_evidence = sections['Tests and acceptance evidence']
    fixture_manifest = ' '.join(re.findall(r'^Fixture sources:\s*(.+)$', test_evidence, re.M))
    for feature in packet['feature_contracts']:
        feature_id = feature['feature_id']
        _fail(review, feature_id in architecture and
              all(path in architecture for path in set(feature.get('implementation_paths', [])) &
                  set(packet.get('allowed_paths', []))),
              f'{key}: Architecture and anatomy must ground feature {feature_id} in its owned components')
        test_source_reader = getattr(review.blueprint, 'test_sources', None)
        approved_fixtures = set(test_source_reader(feature)) if callable(test_source_reader) else set()
        for check in feature['acceptance_checks']:
            _fail(review, re.search(rf'^\s*\d+[.)].*{re.escape(feature_id)}.*{re.escape(check["check_id"])}',
                                    behavior, re.M),
                  f'{key}: Detailed functional requirements needs a numbered operation for '
                  f'{feature_id}/{check["check_id"]}')
            if packet.get('guide_format_version', 1) >= 3:
                operation = next((line for line in behavior.splitlines()
                                  if re.match(r'^\s*\d+[.)]', line)
                                  and feature_id in line and check['check_id'] in line), '')
                _fail(review, check['expected'] in operation,
                      f'{key}: Detailed functional requirements must preserve the approved outcome for '
                      f'{feature_id}/{check["check_id"]}')
            if not approved_fixtures:
                approved_fixtures.update(check.get('test_source_paths', []))
                approved_fixtures.update(part for part in check.get('command', [])
                                         if isinstance(part, str) and
                                         part.endswith(('.py', '.js', '.mjs', '.ts', '.sh', '.json')))
            _fail(review, bool(approved_fixtures) and all(path in fixture_manifest for path in approved_fixtures),
                  f'{key}: acceptance checks need approved fixture paths in the Fixture sources list')
    if packet.get('guide_format_version', 1) >= 3:
        finished = sections['Finished experience or service contract']
        _require_table(review, finished, 'Finished experience or service contract',
                       ['Feature', 'Check ID', 'Expected outcome'],
                       [[feature['feature_id'], check['check_id'], check['expected']]
                        for feature in packet['feature_contracts']
                        for check in feature['acceptance_checks']], key)
        _fail(review, all(line.lstrip().startswith('|') for line in finished.splitlines() if line.strip()),
              f'{key}: Finished experience or service contract permits only canonical approved outcome rows')
        trace_rows = _table_rows(sections['Traceability matrix'])
        for feature in packet['feature_contracts']:
            for check in feature['acceptance_checks']:
                rows = [row for row in trace_rows[1:]
                        if len(row) == 5 and row[0] == feature['feature_id']
                        and row[1] == 'check' and row[2] == check['check_id']]
                _fail(review, len(rows) == 1,
                      f'{key}: Step-by-step implementation recipe needs one trace row for '
                      f'{feature["feature_id"]}/{check["check_id"]}')
                step = rows[0][4].strip('`') if rows else ''
                recipe_line = next((line for line in recipe.splitlines()
                                    if re.match(r'^\s*\d+[.)]', line)
                                    and re.search(rf'`?{re.escape(step)}`?', line)
                                    and check['check_id'] in line), '')
                _fail(review, bool(recipe_line) and check['expected'] in recipe_line
                      and 'Expected result:' in recipe_line,
                      f'{key}: Step-by-step implementation recipe must tie {feature["feature_id"]}/'
                      f'{check["check_id"]} to its approved expected result')
    _fail(review, 'Rollback steps:' in rollback and
          re.search(r'\b(?:revert|restore)\b', rollback, re.I) and
          all(path in rollback for path in packet.get('allowed_paths', [])),
          f'{key}: Safety, rollback and prohibited deviations needs concrete rollback steps for each affected path')
    for row in _table_rows(sections['Traceability matrix'])[1:]:
        if len(row) == 5:
            step = row[4].strip('`')
            _fail(review, re.search(rf'^\s*\d+[.)]\s*`?{re.escape(step)}`?.*Expected result:\s*\S', recipe, re.M),
                  f'{key}: recipe step {step} needs a concrete Expected result')


def _annex_fields(body):
    """Read bullet fields, supporting consecutive lines and fenced literal values."""
    fields = {}
    current = None
    fence = None
    indent = ''
    for line in body.splitlines():
        if fence:
            if re.fullmatch(rf'\s*{re.escape(fence[0])}{{{len(fence)},}}\s*', line):
                fence = None
                current = None
            else:
                current.append(line.removeprefix(indent))
            continue
        field = re.fullmatch(r'- ([^:\n]+):(?: (.*))?', line)
        if field:
            name, value = field.groups()
            current = [value] if value else []
            fields[name] = current
        elif current is not None:
            opening = re.fullmatch(r'(\s*)(`{3,}|~{3,})[^\s`~]*\s*', line)
            if not current and opening:
                indent, fence = opening.groups()
            elif not line.strip():
                current = None
            else:
                current.append(line)
    return {field: '\n'.join(lines) for field, lines in fields.items()}


def _validate_annexes(review, packet, guide):
    sections = _sections_with_lines(guide, 3)
    spec = review.blueprint.load('blueprints', packet['blueprint_version'])
    compact = lambda value: ''.join(value.split())
    helper_spec = importlib.util.spec_from_file_location(
        'p2p_surface_contracts', Path(__file__).with_name('surface_contracts.py'))
    surface_api = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(surface_api)
    for feature in packet['feature_contracts']:
        f = feature['feature_id']
        actual = feature.get('surface', spec['surface'])
        needed = set(('ui', 'backend') if actual == 'fullstack' else (actual,))
        needed.update(feature.get('slice_surfaces', []))
        surface_version = packet.get('surface_contract_version')
        if surface_version is not None:
            try:
                surface_api.validate_surface_contracts(
                    surface_version, sorted(needed), feature.get('surface_contracts', {}))
            except ValueError as error:
                raise review.api.StateError(f'{packet["slice_id"]}/{f}: {error}') from error
        for surface in sorted(needed):
            heading = f'{surface.upper()} annex — {f}'
            _fail(review, heading in sections, f'{packet["slice_id"]}: missing {heading}')
            body = sections[heading][1]
            fields = _annex_fields(body)
            for field in surface_api.ANNEX_FIELDS[surface]:
                content = fields.get(field, '')
                approved = feature.get('surface_contracts', {}).get(surface, {}).get(field)
                grounded_na = (surface_version is not None and isinstance(approved, dict) and
                               approved.get('applicability') == 'not_applicable')
                _fail(review, (grounded_na and content == f'Not applicable — {approved["reason"]}') or
                      (len(content.strip()) >= 8 and content not in {'Not applicable', 'N/A'}),
                      f'{heading}: missing or unexplained {field}')
            for field, names in JSON_CHECKS.get(surface, {}).items():
                approved_schema_value = feature.get('surface_contracts', {}).get(surface, {}).get(field)
                if (surface_version is not None and isinstance(approved_schema_value, dict) and
                        approved_schema_value.get('applicability') == 'not_applicable'):
                    continue
                try:
                    value = json.loads(fields[field].strip('`'))
                except (ValueError, KeyError) as error:
                    raise review.api.StateError(f'{heading}: {field} needs a JSON object') from error
                _fail(review, isinstance(value, dict) and set(value) >= names and
                      all(isinstance(value[name], dict) and value[name] for name in names),
                      f'{heading}: {field} needs nonempty objects {sorted(names)}')
                if field in {'Request and response schemas', 'Input and output schemas'}:
                    for name in names:
                        surface_api.validate_json_schema(value[name], location=f'{heading}.{name}')
                if field == 'Statuses and error schemas':
                    for status, error_schema in value['errors'].items():
                        _fail(review, bool(re.fullmatch(r'[1-5][0-9]{2}', status)),
                              f'{heading}: invalid HTTP status key in error schemas: {status}')
                        surface_api.validate_json_schema(error_schema, location=f'{heading}.errors.{status}')
            for field, approved in feature.get('surface_contracts', {}).get(surface, {}).items():
                _fail(review, field in fields, f'{heading}: missing approved field {field}')
                observed = fields[field].strip('`')
                if isinstance(approved, dict) and approved.get('applicability') == 'not_applicable':
                    expected = f'Not applicable — {approved["reason"]}'
                    _fail(review, fields[field] == expected,
                          f'{heading}: {field} differs from the approved N/A reason')
                    continue
                if isinstance(approved, (dict, list)):
                    try: observed = json.loads(observed)
                    except ValueError as error:
                        raise review.api.StateError(f'{heading}: {field} needs its approved JSON value') from error
                _fail(review, observed == approved, f'{heading}: {field} differs from the approved contract')
            if surface == 'ui':
                visual = feature['visual_contract']
                for name, target in visual['tokens'].items():
                    _fail(review, name in body and str(target) in body, f'{heading}: missing approved token {name} = {target}')
                for field in ('responsive', 'accessibility'):
                    _fail(review, str(visual[field]) in body, f'{heading}: missing approved {field} contract')
                for case in visual['render_cases']:
                    _fail(review, case['case_id'] in body and case['route'] in body and case['ready_selector'] in body
                          and all(str(number) in body for number in case['viewport'].values()),
                          f'{heading}: missing approved render case {case["case_id"]}')
            if surface == 'backend':
                for name, target in feature['backend_contract'].items():
                    serialized = target if isinstance(target, str) else json.dumps(target, ensure_ascii=False)
                    _fail(review, compact(serialized) in compact(body), f'{heading}: missing approved backend field {name}')


def _validate_reachable_links(review, packet, text, assets, base, key, seen):
    for link in markdown_links(text):
        parsed = urlsplit(link.strip('<>'))
        if parsed.scheme or parsed.netloc:
            _fail(review, parsed.scheme in {'https', 'http'}, f'{key}: unsupported external link {link}')
            continue
        path = posixpath.normpath(posixpath.join(base, unquote(parsed.path))) if parsed.path else ''
        if not path:
            anchor = unquote(parsed.fragment)
            headings = re.findall(r'^#{1,6} (.+)$', text, re.M)
            _fail(review, anchor in {heading_id(name) for name in headings},
                  f'{key}: missing linked Markdown anchor {link}')
            continue
        _fail(review, path in assets or path in packet.get('artifact_hashes', {}),
              f'{key}: linked file is absent from frozen assets: {link}')
        if not path.endswith('.md'):
            continue
        if path in assets:
            content = assets[path]
            content = content.decode() if isinstance(content, bytes) else content
        else:
            try:
                content = review.api.artifact_file(review.directory, path).read_text()
            except (AttributeError, OSError):
                continue
        if parsed.fragment:
            headings = markdown_sections(content, level=1)
            headings.update(markdown_sections(content, level=2))
            headings.update(markdown_sections(content, level=3))
            anchor = unquote(parsed.fragment)
            _fail(review, anchor in {re.sub(r'[^\w -]', '', name.lower()).replace(' ', '-') for name in headings},
                  f'{key}: linked Markdown file has no anchor {link}')
        if path in seen:
            continue
        seen.add(path)
        _validate_reachable_links(review, packet, content, assets, posixpath.dirname(path), key, seen)


def _validate_run_receipt(review, packet, check_id, check, reference, assets, key):
    """Require actual execution evidence from the coordinator's delivery verifier."""
    receipt_path = reference.strip('`')
    folder = posixpath.dirname(packet['guide_path'])
    candidate = posixpath.normpath(posixpath.join(folder, receipt_path))
    data = assets.get(candidate, assets.get(receipt_path))
    _fail(review, data is not None, f'{key}: observed check {check_id} requires a frozen run receipt')
    try:
        receipt = json.loads(data)
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise review.api.StateError(f'{key}: invalid verification receipt for {check_id}') from error
    _fail(review, isinstance(receipt, dict), f'{key}: observed {check_id} needs journaled verification')
    delivery_id = receipt.get('slice_id')
    _fail(review, review.blueprint.identifier(delivery_id),
          f'{key}: observed {check_id} needs journaled verification from a released slice')
    assignment = review.manifest.get('slices', {}).get(delivery_id, {})
    fingerprint = sha256(data).hexdigest()
    recorded = any(event.get('type') == 'slice_verified' and event.get('slice_id') == delivery_id
                   and event.get('verification_sha256') == fingerprint for event in review.records())
    _fail(review, assignment.get('status') in {'verified', 'accepted'}
          and assignment.get('verification_sha256') == fingerprint and recorded
          and receipt.get('approval_id') == packet['blueprint_approval_id'],
          f'{key}: {check_id} requires journaled verification from the active blueprint')
    expected = check['expected']
    argv = check['command']
    matched = [entry for entry in receipt.get('checks', [])
               if entry.get('check_id') == check_id and entry.get('command') == argv
               and entry.get('expected') == expected and entry.get('observed') == expected
               and entry.get('result') == 'PASS']
    _fail(review, len(matched) == 1 and argv in receipt.get('executed_commands', []),
          f'{key}: verification does not execute the exact command and approved outcome for {check_id}')
    errors = review.blueprint.check_saved_verification(delivery_id, assignment)
    _fail(review, not errors, f'{key}: recorded verification is stale for {check_id}: ' + '; '.join(errors))


def render_reviewed_guide(source_bytes, *, source_path, package_id, review):
    """Render current review state into a delivery copy without mutating authored bytes."""
    text = source_bytes.decode('utf-8')
    source_directory = posixpath.dirname(source_path)
    receipt_link = posixpath.relpath(review['path'], source_directory)
    source_link = posixpath.basename(source_path)
    result = review['result']
    mode = review['review_mode']
    reviewer = review['reviewer']
    transformed = text
    status_line = f'- review_status: {result}; review complete, specification approval only.'
    identity = (f'- review_status: {result}; review complete, specification approval only.\n'
                f'- package: {package_id}\n'
                f'- review_mode: {mode}\n'
                f'- reviewer: {reviewer}\n'
                f'- [Authored proposal guide]({source_link})\n'
                f'- [Review receipt]({receipt_link})')
    has_status = '- review_status:' in transformed and '## Identity and status\n' in transformed
    historical_source = not has_status
    if has_status:
        transformed = re.sub(r'^- review_status:.*$', status_line, transformed, count=1, flags=re.M)
        transformed = re.sub(r'^- package:.*$', f'- package: {package_id}', transformed,
                             count=1, flags=re.M)
        status_metadata = (f'- review_mode: {mode}\n- reviewer: {reviewer}\n'
                           f'- [Authored proposal guide]({source_link})\n'
                           f'- [Review receipt]({receipt_link})')
        transformed = transformed.replace(status_line, status_line + '\n' + status_metadata, 1)
    if historical_source:
        # Keep old guide wording readable while identifying its freeze-time fields as historical.
        transformed = ('# Current review status\n\n' + identity +
                       '\n\nThe linked authored guide below records the proposal as it appeared before review. '
                       'Its freeze-time status is historical.\n\n' + transformed)
    history_heading = '## Change and approval history\n'
    if history_heading in transformed:
        before_history, history = transformed.split(history_heading, 1)
        history = re.sub(r'^Outcome: pending review\.$', f'Outcome: {result}.', history,
                         count=1, flags=re.M)
        transformed = before_history + history_heading + history
    history_record = (f'\n\nPackage: `{package_id}`. Review outcome: `{result}`.\n'
                      f'Review mode: {mode}. Reviewer: {reviewer}.\n'
                      f'[Exact review receipt]({receipt_link}).\n'
                      f'[Original authored guide]({source_link}).\n')
    if history_heading in transformed:
        transformed = transformed.replace(history_heading, history_heading + history_record, 1)
    else:
        transformed += '\n\n## Change and approval history' + history_record
    return transformed.encode('utf-8')
