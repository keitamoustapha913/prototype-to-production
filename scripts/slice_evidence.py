"""Validation and rendering for evidence attached to slice review gates.

The review workflow owns the state changes. This module keeps the rules for
review evidence in one place so every gate hashes and binds evidence the same
way.
"""

from pathlib import Path
import json
import re


MUTABLE_CONTROLS = {
    'manifest.json', 'decisions.jsonl', 'CHECKPOINT.md', '.workflow-transition.json',
    '.coordinator.lock', 'slice-review/register.md', 'slice-review/register.json',
    'slice-review/inventory.json',
}


def _require(review, condition, message):
    review.require(condition, message)


def _paths(review, paths, *, allow_empty=False):
    _require(review, isinstance(paths, list) and (allow_empty or bool(paths)),
             'Evidence paths must be a nonempty list')
    _require(review, all(isinstance(path, str) and path.strip() for path in paths),
             'Evidence paths must be nonempty initiative-relative paths')
    _require(review, len(paths) == len(set(paths)), 'Evidence paths must be distinct')
    return paths


def _mutable_targets(review):
    targets = set()
    for control in MUTABLE_CONTROLS:
        try:
            targets.add((review.directory / control).resolve())
        except (OSError, RuntimeError):
            # A control file need not exist yet; resolve still canonicalizes
            # its parent and gives us a stable path for alias comparison.
            targets.add((review.directory / control).absolute())
    return targets


def evidence_hashes(review, paths):
    """Hash nonempty initiative evidence, rejecting aliases of mutable controls."""
    paths = _paths(review, paths)
    protected = _mutable_targets(review)
    hashes = {}
    for relative in paths:
        path = review.api.artifact_file(review.directory, relative)
        _require(review, path.resolve() not in protected,
                 f'Mutable workflow control cannot be frozen as evidence: {relative}')
        # Hard links have different resolved paths but still point to the
        # mutable control's inode, so reject those aliases too.
        for control in MUTABLE_CONTROLS:
            candidate = review.directory / control
            try:
                if candidate.exists() and path.samefile(candidate):
                    review.require(False, f'Mutable workflow control cannot be frozen as evidence: {relative}')
            except OSError:
                pass
        _require(review, path.stat().st_size > 0, f'Evidence file is empty: {relative}')
        hashes[relative] = review.api.digest_of(path)
    return hashes


def _nonempty_strings(review, values, label):
    _require(review, isinstance(values, list) and bool(values)
             and all(isinstance(value, str) and value.strip() for value in values),
             f'{label} must be a nonempty list of strings')
    _require(review, len(values) == len(set(values)), f'{label} must not contain duplicates')


def _anchor_paths(packets):
    return {anchor.get('path') for packet in packets.values()
            for anchor in packet.get('approved_source_anchors', [])
            if isinstance(anchor, dict) and isinstance(anchor.get('path'), str)}


def _citation_lines(review, path, packets):
    if any(path in packet.get('artifact_paths', [])
           or path in packet.get('artifact_hashes', {})
           or path in {packet.get('guide_path'), packet.get('contract_path')}
           for packet in packets.values()):
        return review.api.artifact_file(review.directory, path).read_text(encoding='utf-8').splitlines()
    anchors = _anchor_paths(packets)
    _require(review, path in anchors, f'Cited source path is not frozen or approved: {path}')
    source_file = review.api.project_source_file(review.directory.parents[2], path)
    return source_file.read_text(encoding='utf-8').splitlines()


def _validate_citation(review, packets, source_path, citation, *, check_id=None):
    _require(review, isinstance(source_path, str) and source_path.strip()
             and isinstance(citation, str) and citation.strip(),
             'Finding needs a source path and citation')
    match = re.fullmatch(r'check:(.+)', citation)
    if match:
        _require(review, check_id is not None and match.group(1) == check_id,
                 'Check citation must name the exact approved check or render case')
        return
    lines = re.fullmatch(r'(?:line|lines) (\d+)(?:-(\d+))?', citation)
    _require(review, lines is not None,
             'Source citation must name the exact check ID or real line numbers')
    source_lines = _citation_lines(review, source_path, packets)
    first, last = int(lines.group(1)), int(lines.group(2) or lines.group(1))
    _require(review, 1 <= first <= last <= len(source_lines),
             f'Source citation is outside {source_path}')


def _shared_boundary_pairs(packets):
    """Pairs whose approved contracts make interaction plausible enough to reconcile."""
    pairs = set()
    ids = sorted(packets)
    for index, left_id in enumerate(ids):
        left = packets[left_id]
        for right_id in ids[index + 1:]:
            right = packets[right_id]
            related = (right_id in left.get('dependencies', [])
                       or left_id in right.get('dependencies', [])
                       or bool(set(left.get('surfaces', [])) & set(right.get('surfaces', [])))
                       or bool(set(left.get('shared_contracts', {})) & set(right.get('shared_contracts', {})))
                       or bool(set(left.get('allowed_paths', [])) & set(right.get('allowed_paths', [])))
                       or bool({path for row in left.get('dependency_contracts', []) if isinstance(row, dict)
                                for path in row.get('paths', [])} &
                               {path for row in right.get('dependency_contracts', []) if isinstance(row, dict)
                                for path in row.get('paths', [])}))
            if not related:
                left_routes = set()
                right_routes = set()
                left_api = set()
                right_api = set()
                for feature in left.get('feature_contracts', []):
                    for case in feature.get('visual_contract', {}).get('render_cases', []):
                        if isinstance(case.get('route'), str):
                            left_routes.add(case['route'])
                    request = feature.get('backend_contract', {}).get('request', '')
                    if isinstance(request, dict):
                        method, route = request.get('method'), request.get('route', request.get('path'))
                        if isinstance(method, str) and isinstance(route, str):
                            left_api.add((method.upper(), route))
                    elif isinstance(request, str):
                        match = re.search(r'\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+([^;\s]+)', request, re.I)
                        if match:
                            left_api.add((match.group(1).upper(), match.group(2)))
                    api_contract = feature.get('surface_contracts', {}).get('api', {})
                    api_route = api_contract.get('Method and route', '') if isinstance(api_contract, dict) else ''
                    match = re.search(r'\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+([^;\s`]+)', api_route, re.I) if isinstance(api_route, str) else None
                    if match:
                        left_api.add((match.group(1).upper(), match.group(2)))
                for feature in right.get('feature_contracts', []):
                    for case in feature.get('visual_contract', {}).get('render_cases', []):
                        if isinstance(case.get('route'), str):
                            right_routes.add(case['route'])
                    request = feature.get('backend_contract', {}).get('request', '')
                    if isinstance(request, dict):
                        method, route = request.get('method'), request.get('route', request.get('path'))
                        if isinstance(method, str) and isinstance(route, str):
                            right_api.add((method.upper(), route))
                    elif isinstance(request, str):
                        match = re.search(r'\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+([^;\s]+)', request, re.I)
                        if match:
                            right_api.add((match.group(1).upper(), match.group(2)))
                    api_contract = feature.get('surface_contracts', {}).get('api', {})
                    api_route = api_contract.get('Method and route', '') if isinstance(api_contract, dict) else ''
                    match = re.search(r'\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+([^;\s`]+)', api_route, re.I) if isinstance(api_route, str) else None
                    if match:
                        right_api.add((match.group(1).upper(), match.group(2)))
                related = bool(left_routes & right_routes or left_api & right_api)
            if related:
                pairs.add((left_id, right_id))
    return pairs


def _heading_lines(lines):
    fence = None
    for number, line in enumerate(lines, 1):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            token, rest = marker.groups()
            if fence is None:
                fence = token
            elif token.startswith(fence) and not rest.strip():
                fence = None
            continue
        if fence is None:
            heading = re.match(r'^ {0,3}#{1,6}[ \t]+(.+?)\s*$', line)
            if heading:
                yield heading[1], number


def validate_handoff(review, packet, entry, value, *, verify_symbols=False):
    """Validate a no-code walkthrough bound to one exact frozen slice revision.

    `value` contains exact slice/revision/digests; reviewer/result; detailed
    findings with section, citation, finding, and resolution; source_paths;
    read_only_scenarios with scenario, expected, observed, and read_only=true;
    acceptance_checks with check_id, expected, observed, and PASS; and evidence_paths.
    """
    _require(review, isinstance(value, dict), 'Structured handoff review required')
    key = packet.get('slice_id')
    revision = entry.get('revision')
    _require(review, value.get('slice_id') == key and value.get('proposal_revision') == revision
             and packet.get('proposal_revision') == revision,
             'Handoff review must name the exact selected slice revision')
    for field in ('guide_sha256', 'contract_sha256'):
        _require(review, value.get(field) == entry.get(field),
                 f'Handoff review must bind the exact {field}')
    reviewer = value.get('reviewer')
    _require(review, isinstance(reviewer, str) and reviewer.strip()
             and reviewer != packet.get('owner'), 'Independent reviewer is required')
    _require(review, value.get('result') == 'PASS', 'Handoff walkthrough must pass before approval')

    schema_version = packet.get('evidence_schema_version', 1)
    if schema_version >= 2:
        walkthrough = value.get('walkthrough')
        _require(review, isinstance(walkthrough, dict)
                 and walkthrough.get('mode') == 'no_code'
                 and walkthrough.get('context') == 'fresh_session'
                 and walkthrough.get('independent') is True
                 and walkthrough.get('performed') is True
                 and walkthrough.get('test_execution') is False
                 and isinstance(walkthrough.get('attestation'), str)
                 and walkthrough['attestation'].strip(),
                 'An independent no-code walkthrough attestation is required')
        _require(review, isinstance(walkthrough.get('evidence_path'), str)
                 and walkthrough['evidence_path'] in value.get('evidence_paths', []),
                 'Walkthrough evidence must be included in evidence_paths')

    guide_path = review.api.artifact_file(review.directory, entry.get('guide_path'))
    guide_lines = guide_path.read_text(encoding='utf-8').splitlines()
    headings = dict(_heading_lines(guide_lines))
    findings = value.get('findings')
    _require(review, isinstance(findings, list) and bool(findings),
             'Handoff review needs specific findings')
    reviewed_sections = set()
    for finding in findings:
        _require(review, isinstance(finding, dict)
                 and all(isinstance(finding.get(field), str) and finding[field].strip()
                         for field in ('section', 'citation', 'finding', 'resolution')),
                 'Each handoff finding needs a section citation, finding, and resolution')
        title = finding['section']
        _require(review, title in headings, f'Handoff finding cites unknown guide section: {title}')
        citation = re.fullmatch(r'(?:line|lines) (\d+)(?:-(\d+))?', finding['citation'])
        _require(review, citation is not None, 'Handoff citation must name exact guide line numbers')
        start = int(citation.group(1))
        end = int(citation.group(2) or start)
        next_heading = min((line for line in headings.values() if line > headings[title]), default=len(guide_lines) + 1)
        _require(review, headings[title] < start <= end < next_heading,
                 f'Handoff citation must point inside {title}')
        _require(review, schema_version < 3 or finding.get('result') != 'BLOCKED',
                 f'Handoff records a blocker in {title}: {finding["finding"]}')
        if finding.get('result') == 'PASS' and any(line.strip() for line in guide_lines[start - 1:end]):
            reviewed_sections.add(title)
    if schema_version >= 3:
        normative_sections = {'Finished experience or service contract', 'Detailed functional requirements',
                              'Step-by-step implementation recipe'}
        missing = normative_sections - reviewed_sections
        _require(review, not missing, f'Handoff needs passing, line-cited findings for normative guide sections: '
                 f'{", ".join(sorted(missing))}')

    sources = value.get('source_paths')
    _nonempty_strings(review, sources, 'Handoff source paths')
    known_sources = set(packet.get('artifact_paths', []))
    known_sources.update(packet.get('artifact_hashes', {}).keys())
    if entry.get('guide_path'):
        known_sources.add(entry['guide_path'])
    if entry.get('contract_path'):
        known_sources.add(entry['contract_path'])
    known_sources.update(_anchor_paths({key: packet}))
    _require(review, set(sources) <= known_sources,
             'Handoff source paths must be grounded in this slice contract')

    scenarios = value.get('read_only_scenarios')
    _require(review, isinstance(scenarios, list) and bool(scenarios),
             'Handoff review needs read-only scenario walkthroughs')
    expected_scenarios = {}
    expected_check_ids = set()
    for feature in packet.get('feature_contracts', []):
        states = feature.get('states', [])
        state_expectations = {}
        for check in feature.get('acceptance_checks', []):
            expected_check_ids.add((feature['feature_id'], check['check_id']))
            state_expectations.update(check.get('state_expectations', {}))
            check_id = check['check_id']
            expected_scenarios[f'{feature["feature_id"]}:check:{check_id}'] = (
                check.get('expected'), {(feature['feature_id'], check_id)})
        for state in states:
            if state in state_expectations:
                check_ids = {(feature['feature_id'], check['check_id'])
                             for check in feature.get('acceptance_checks', [])
                             if check.get('state_expectations', {}).get(state) == state_expectations[state]}
                expected_scenarios[f'{feature["feature_id"]}:{state}'] = (state_expectations[state], check_ids)
        for render_case in feature.get('visual_contract', {}).get('render_cases', []):
            viewport = render_case['viewport']
            size = f'{viewport.get("width", "?")}x{viewport.get("height", "?")}'
            outcome = f'renders {render_case["route"]} at {size} with {render_case["ready_selector"]}'
            expected_check_ids.add((feature['feature_id'], render_case['case_id']))
            expected_scenarios[f'{feature["feature_id"]}:render:{render_case["case_id"]}'] = (
                outcome, {(feature['feature_id'], render_case['case_id'])})
    actual_scenarios = {}
    source_lines = {}
    for scenario in scenarios:
        _require(review, isinstance(scenario, dict)
                 and all(isinstance(scenario.get(field), str) and scenario[field].strip()
                         for field in ('scenario', 'expected', 'observed'))
                 and scenario.get('read_only') is True,
                 'Each walkthrough must name expected and observed outcomes and be read-only')
        if schema_version >= 2:
            _require(review, isinstance(scenario.get('observation_basis'), str)
                     and scenario['observation_basis'].strip(),
                     'Walkthrough observations need a source-based observation basis')
            references = scenario.get('references')
            _require(review, isinstance(references, list) and bool(references),
                     'Walkthrough scenarios need approved case and source references')
            for reference in references:
                _require(review, isinstance(reference, dict)
                         and isinstance(reference.get('feature_id'), str)
                         and isinstance(reference.get('check_id'), str)
                         and (reference['feature_id'], reference['check_id']) in expected_check_ids
                         and isinstance(reference.get('source_path'), str)
                         and reference['source_path'] in known_sources
                         and isinstance(reference.get('symbol'), str) and reference['symbol'].strip(),
                         'Walkthrough references must name an approved case, source path, and symbol')
                if schema_version >= 3 or verify_symbols:
                    source_path, symbol = reference['source_path'], reference['symbol']
                    if source_path not in source_lines:
                        source_lines[source_path] = _citation_lines(review, source_path, {key: packet})
                    lines = source_lines[source_path]
                    if source_path.casefold().endswith('.md'):
                        exists = any(heading == symbol for heading, _ in _heading_lines(lines))
                    else:
                        target = re.compile(r'(?<![\w-])' + re.escape(symbol) + r'(?![\w-])')
                        exists = any(target.search(line) for line in lines)
                    _require(review, exists,
                             f'Cannot resolve walkthrough symbol {symbol!r} in {source_path}')
        actual_scenarios[scenario['scenario']] = scenario
    if schema_version >= 2:
        try:
            evidence_path = review.api.artifact_file(review.directory, walkthrough['evidence_path'])
            walkthrough_record = json.loads(evidence_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            walkthrough_record = None
        _require(review, isinstance(walkthrough_record, dict)
                 and walkthrough_record.get('record_type') == 'independent_no_code_walkthrough'
                 and walkthrough_record.get('reviewer') == reviewer
                 and walkthrough_record.get('context') == 'fresh_session'
                 and walkthrough_record.get('test_execution') is False,
                 'A structured no-code walkthrough record with the independent reviewer is required')
        record_sources = walkthrough_record.get('source_paths')
        _require(review, isinstance(record_sources, list) and bool(record_sources)
                 and all(isinstance(path, str) and path in known_sources for path in record_sources),
                 'Walkthrough record source paths must be approved for this slice')
        record_cases = walkthrough_record.get('cases')
        _require(review, isinstance(record_cases, list) and bool(record_cases),
                 'Walkthrough record needs specific observed cases')
        expected_record_cases = set()
        required_record_sources = set()
        for scenario_id, scenario in actual_scenarios.items():
            for reference in scenario.get('references', []):
                expected_record_cases.add((scenario_id, reference['feature_id'], reference['check_id']))
                required_record_sources.add(reference['source_path'])
        _require(review, required_record_sources <= set(record_sources),
                 'Walkthrough evidence must include every referenced approved source path')
        actual_record_cases = set()
        for case in record_cases:
            _require(review, isinstance(case, dict)
                     and all(isinstance(case.get(field), str) and case[field].strip()
                             for field in ('scenario', 'feature_id', 'check_id', 'expected', 'observed',
                                           'observation_basis'))
                     and isinstance(case.get('references'), list) and bool(case['references']),
                     'Walkthrough record cases need exact IDs, outcomes, basis, and references')
            key = (case['scenario'], case['feature_id'], case['check_id'])
            _require(review, key in expected_record_cases and key not in actual_record_cases,
                     'Walkthrough record contains an unapproved or duplicate case')
            scenario = actual_scenarios[case['scenario']]
            _require(review, case['expected'] == scenario['expected']
                     and case['observed'] == scenario['observed']
                     and case['observation_basis'] == scenario['observation_basis']
                     and case['references'] == scenario['references'],
                     'Walkthrough record does not match the structured scenario observations')
            actual_record_cases.add(key)
        _require(review, actual_record_cases == expected_record_cases,
                 'Walkthrough record omits an approved feature/check case')
    for scenario_id, (expected, required_references) in expected_scenarios.items():
        scenario = actual_scenarios.get(scenario_id)
        _require(review, scenario is not None and scenario['expected'] == expected
                 and scenario['observed'] == expected,
                 f'Handoff walkthrough does not verify approved outcome for {scenario_id}')
        actual_references = {(item['feature_id'], item['check_id'])
                             for item in scenario.get('references', [])}
        _require(review, actual_references == required_references,
                 f'Handoff walkthrough references do not match approved cases for {scenario_id}')

    checks = value.get('acceptance_checks')
    _require(review, isinstance(checks, list) and bool(checks),
             'Handoff review needs acceptance-check outcomes')
    expected_ids = set(packet.get('acceptance_check_ids', []))
    if not expected_ids:
        expected_ids = {check.get('check_id') for feature in packet.get('feature_contracts', [])
                        for check in feature.get('acceptance_checks', [])}
    observed_ids = set()
    expected_checks = {check['check_id']: check for feature in packet.get('feature_contracts', [])
                       for check in feature.get('acceptance_checks', [])}
    for check in checks:
        _require(review, isinstance(check, dict)
                 and all(isinstance(check.get(field), str) and check[field].strip()
                         for field in ('check_id', 'expected', 'observed'))
                 and check.get('result') == 'PASS',
                 'Each acceptance check needs expected and observed passing outcomes')
        approved = expected_checks.get(check['check_id'])
        _require(review, approved is not None and check['expected'] == approved.get('expected')
                 and check['observed'] == approved.get('expected'),
                 f'Acceptance check {check["check_id"]} does not match its approved expected outcome')
        observed_ids.add(check['check_id'])
    _require(review, expected_ids <= observed_ids,
             f'Handoff review omits acceptance checks: {sorted(expected_ids - observed_ids)}')

    evidence = _paths(review, value.get('evidence_paths'))
    # Source documents and reviewer evidence are both content addressed.
    anchor_paths = _anchor_paths({key: packet})
    hash_sources = [path for path in sources if path not in anchor_paths]
    return evidence_hashes(review, list(dict.fromkeys(hash_sources + evidence)))


def collection_case_outcomes(packets):
    outcomes = {}
    for packet in packets.values():
        for feature in packet.get('feature_contracts', []):
            key = feature['feature_id']
            for check in feature.get('acceptance_checks', []):
                outcomes.setdefault((key, check['check_id']), {})['acceptance'] = check.get('expected')
            for case in feature.get('visual_contract', {}).get('render_cases', []):
                viewport = case['viewport']
                size = f'{viewport.get("width", "?")}x{viewport.get("height", "?")}'
                outcomes.setdefault((key, case['case_id']), {})['render'] = (
                    f'renders {case["route"]} at {size} with {case["ready_selector"]}')
    return outcomes


def check_saved_case_kinds(review, report, packets):
    """Old reports are reusable unless they conflate two approved cases."""
    collisions = {key for key, kinds in collection_case_outcomes(packets).items() if len(kinds) > 1}
    if not collisions:
        return
    for check in report['checks'].values():
        if check['result'] != 'PASS' or 'case_conclusions' not in check:
            continue
        covered = {(item['feature_id'], item['check_id'], item.get('case_kind'))
                   for item in check['case_conclusions']}
        _require(review, all((*key, kind) in covered for key in collisions for kind in ('acceptance', 'render')),
                 'Collection report does not distinguish an acceptance check from a render case with the same ID. '
                 'Run fresh collection validation before locking or reopen the affected locked collection.')


def validate_semantic(review, name, declaration, packets, revisions):
    """Validate and hash cross-slice evidence, retaining BLOCKED declarations."""
    _require(review, isinstance(declaration, dict), f'{name} review declaration must be an object')
    result = declaration.get('result')
    _require(review, result in {'PASS', 'NOT_APPLICABLE', 'BLOCKED'},
             f'{name} result must be PASS, NOT_APPLICABLE, or BLOCKED')
    explanation = declaration.get('explanation')
    _require(review, isinstance(explanation, str) and explanation.strip(),
             f'{name} needs an explanation')
    if result == 'BLOCKED':
        affected = declaration.get('affected_items')
        _nonempty_strings(review, affected, f'{name} affected items')

    _require(review, declaration.get('schema_version') == 2,
             f'{name} evidence schema version 2 is required')
    reviewer = declaration.get('reviewer')
    _require(review, isinstance(reviewer, dict)
             and isinstance(reviewer.get('name'), str) and reviewer['name'].strip()
             and isinstance(reviewer.get('context'), str) and reviewer['context'].strip()
             and reviewer.get('independent_of_authoring') is True,
             f'{name} needs a structured independent reviewer')
    owners = {packet.get('owner') for packet in packets.values()}
    _require(review, reviewer['name'] not in owners,
             f'{name} reviewer must be distinct from slice owners')

    bindings = declaration.get('revision_bindings')
    _require(review, isinstance(bindings, dict) and set(bindings) == set(packets),
             f'{name} must bind every selected slice revision')
    for key, packet in packets.items():
        entry = revisions[key]
        binding = bindings.get(key)
        _require(review, isinstance(binding, dict)
                 and binding.get('proposal_revision') == entry.get('revision')
                 and packet.get('proposal_revision') == entry.get('revision')
                 and binding.get('guide_sha256') == entry.get('guide_sha256')
                 and binding.get('contract_sha256') == entry.get('contract_sha256'),
                 f'{name} evidence is stale or mismatched for slice {key}')

    expected_features = {feature_id for packet in packets.values()
                         for feature_id in packet.get('feature_ids', [])}
    covered_features = declaration.get('covered_feature_ids')
    _nonempty_strings(review, covered_features, f'{name} covered feature IDs')
    _require(review, set(covered_features) == expected_features,
             f'{name} must cover every selected feature: {sorted(expected_features - set(covered_features))}')

    sources = declaration.get('source_paths')
    _nonempty_strings(review, sources, f'{name} source paths')
    grounded = set()
    for key, packet in packets.items():
        grounded.update(packet.get('artifact_paths', []))
        grounded.update(packet.get('artifact_hashes', {}).keys())
        if packet.get('guide_path'):
            grounded.add(packet['guide_path'])
        if packet.get('contract_path'):
            grounded.add(packet['contract_path'])
    anchor_paths = _anchor_paths(packets)
    grounded.update(anchor_paths)
    _require(review, set(sources) <= grounded,
             f'{name} source paths must refer to the locked collection artifacts or approved source anchors')

    expected_outcomes = collection_case_outcomes(packets)
    expected_cases = {(*key, kind) for key, outcomes in expected_outcomes.items() for kind in outcomes}
    case_conclusions = declaration.get('case_conclusions')
    _require(review, isinstance(case_conclusions, list),
             f'{name} case conclusions must be a list')
    observed_cases = set()
    for conclusion in case_conclusions:
        _require(review, isinstance(conclusion, dict)
                 and isinstance(conclusion.get('feature_id'), str)
                 and isinstance(conclusion.get('check_id'), str)
                 and isinstance(conclusion.get('conclusion'), str) and conclusion['conclusion'].strip()
                 and conclusion.get('result') in {'PASS', 'BLOCKED'},
                 f'{name} case conclusions need exact feature/check findings')
        key = (conclusion['feature_id'], conclusion['check_id'])
        outcomes = expected_outcomes.get(key, {})
        kind = conclusion.get('case_kind')
        if kind is None and len(outcomes) == 1:
            kind = next(iter(outcomes))
        case = (*key, kind)
        _require(review, len(outcomes) < 2 or kind is not None,
                 f'{name} must specify case_kind for shared acceptance and render ID {key[1]}')
        _require(review, isinstance(kind, str) and case in expected_cases and case not in observed_cases,
                 f'{name} case conclusions contain an unknown or duplicate feature/check')
        _require(review, conclusion.get('expected') == outcomes[kind],
                 f'{name} case conclusion differs from the approved expected outcome for {case[1]}')
        _require(review, conclusion.get('source_path') in grounded
                 or conclusion.get('source_path') in _anchor_paths(packets),
                 f'{name} case conclusion source path is not frozen or approved')
        _validate_citation(review, packets, conclusion.get('source_path'), conclusion.get('citation'),
                           check_id=case[1])
        observed_cases.add(case)
    if result == 'PASS':
        _require(review, observed_cases == expected_cases
                 and all(item['result'] == 'PASS' for item in case_conclusions),
                 f'{name} case conclusions must cover every exact feature/check and pass')

    expected_interfaces = {}
    for slice_id, packet in packets.items():
        for interface_id, contract in packet.get('shared_contracts', {}).items():
            record = expected_interfaces.setdefault(interface_id, {'providers': [], 'consumers': []})
            record['providers' if contract['role'] == 'provides' else 'consumers'].append(slice_id)
    interface_conclusions = declaration.get('interface_conclusions')
    _require(review, isinstance(interface_conclusions, list),
             f'{name} interface conclusions must be a list')
    observed_interfaces = {}
    for conclusion in interface_conclusions:
        _require(review, isinstance(conclusion, dict)
                 and isinstance(conclusion.get('interface_id'), str)
                 and isinstance(conclusion.get('provider_slice_id'), str)
                 and isinstance(conclusion.get('consumer_slice_ids'), list)
                 and all(isinstance(item, str) for item in conclusion['consumer_slice_ids'])
                 and isinstance(conclusion.get('conclusion'), str) and conclusion['conclusion'].strip()
                 and isinstance(conclusion.get('source_path'), str)
                 and conclusion.get('result') in {'PASS', 'BLOCKED'},
                 f'{name} interface conclusions need exact interface findings')
        interface_id = conclusion['interface_id']
        expected = expected_interfaces.get(interface_id)
        _require(review, expected is not None and interface_id not in observed_interfaces
                 and expected['providers'] == [conclusion['provider_slice_id']]
                 and sorted(expected['consumers']) == sorted(conclusion['consumer_slice_ids']),
                 f'{name} interface conclusion has an unknown interface or mismatched endpoints')
        _require(review, conclusion['source_path'] in grounded
                 or conclusion['source_path'] in _anchor_paths(packets),
                 f'{name} interface source path is not frozen or approved')
        _validate_citation(review, packets, conclusion['source_path'], conclusion.get('citation'))
        observed_interfaces[interface_id] = conclusion
    if name == 'shared_contract_review' and result == 'PASS':
        _require(review, set(observed_interfaces) == set(expected_interfaces)
                 and all(item['result'] == 'PASS' for item in interface_conclusions),
                 f'{name} must conclude on every exact shared interface')

    boundary_conclusions = declaration.get('boundary_conclusions', [])
    if name == 'shared_contract_review':
        _require(review, isinstance(boundary_conclusions, list),
                 f'{name} boundary conclusions must be a list')
        expected_pairs = _shared_boundary_pairs(packets)
        observed_pairs = set()
        for conclusion in boundary_conclusions:
            _require(review, isinstance(conclusion, dict)
                     and isinstance(conclusion.get('slice_ids'), list)
                     and len(conclusion['slice_ids']) == 2
                     and all(isinstance(item, str) for item in conclusion['slice_ids'])
                     and conclusion['slice_ids'] == sorted(conclusion['slice_ids'])
                     and isinstance(conclusion.get('conclusion'), str) and conclusion['conclusion'].strip()
                     and conclusion.get('result') in {'PASS', 'BLOCKED'}
                     and isinstance(conclusion.get('source_path'), str),
                     f'{name} boundary conclusions need exact slice-pair findings')
            pair = tuple(conclusion['slice_ids'])
            _require(review, pair in expected_pairs and pair not in observed_pairs,
                     f'{name} boundary conclusion has an unrelated or duplicate slice pair')
            pair_packets = {slice_id: packets[slice_id] for slice_id in pair}
            _validate_citation(review, pair_packets, conclusion['source_path'], conclusion.get('citation'))
            observed_pairs.add(pair)
        if result == 'PASS':
            _require(review, observed_pairs == expected_pairs
                     and all(item['result'] == 'PASS' for item in boundary_conclusions),
                     f'{name} must conclude on every plausible shared boundary')

    applicability = declaration.get('applicability')
    if result == 'NOT_APPLICABLE':
        _require(review, isinstance(applicability, dict)
                 and isinstance(applicability.get('scope'), list) and bool(applicability['scope'])
                 and all(isinstance(item, str) and item in expected_features for item in applicability['scope'])
                 and isinstance(applicability.get('reason'), str) and applicability['reason'].strip()
                 and isinstance(applicability.get('evidence_paths'), list)
                 and bool(applicability['evidence_paths']),
                 f'{name} NOT_APPLICABLE requires scoped applicability reasoning and evidence')
        _require(review, set(applicability['scope']) == expected_features,
                 f'{name} NOT_APPLICABLE must explain applicability for every selected feature')
        if name == 'shared_contract_review':
            dependencies_exist = any(packet.get('dependencies') for packet in packets.values())
            surfaces = {surface for packet in packets.values() for surface in packet.get('surfaces', [])}
            _require(review, not expected_interfaces and not dependencies_exist
                     and len(surfaces) <= 1 and len(packets) == 1,
                     'shared_contract_review applies because selected slices declare or imply interfaces')
        if name == 'end_to_end_journeys':
            dependencies_exist = any(packet.get('dependencies') for packet in packets.values())
            surfaces = {surface for packet in packets.values() for surface in packet.get('surfaces', [])}
            _require(review, not dependencies_exist and len(surfaces) <= 1,
                     'end_to_end_journeys applies across dependencies or multiple surfaces')

    evidence = _paths(review, declaration.get('evidence_paths'))
    if result == 'NOT_APPLICABLE':
        _require(review, set(applicability['evidence_paths']) <= set(evidence),
                 f'{name} applicability evidence must be included in evidence_paths')
    anchor_paths = _anchor_paths(packets)
    hashes = evidence_hashes(review, list(dict.fromkeys(
        [path for path in sources if path not in anchor_paths] + evidence)))
    return {
        'result': result,
        'explanation': explanation,
        'reviewer': reviewer,
        'case_conclusions': case_conclusions,
        'interface_conclusions': interface_conclusions,
        **({'boundary_conclusions': boundary_conclusions} if name == 'shared_contract_review' else {}),
        'source_paths': sources,
        'evidence_paths': evidence,
        'evidence_hashes': hashes,
        **({'affected_items': declaration['affected_items']} if result == 'BLOCKED' else {}),
        **({'applicability': applicability} if result == 'NOT_APPLICABLE' else {}),
    }


def render_recap(review, package_id, collection_path, report):
    """Render the canonical user-facing summary for a package lock."""
    registry = review.registry
    selected = registry.get('selected', {})
    order = registry.get('order', [])
    reviews = registry.get('reviews', {})
    lines = [f'# Final review recap — {package_id}', '',
             f'- Initiative: {review.manifest.get("initiative_id", "unknown")}',
             f'- Blueprint approval: {review.active_approval}',
             f'- Blueprint version: {review.manifest.get("approved_version", "unknown")}',
             f'- Collection report: `{collection_path}`',
             f'- Collection SHA-256: `{registry.get("artifact_hashes", {}).get(collection_path, "unknown")}`',
             f'- Collection result: **{report.get("result", "BLOCKED")}**', '',
             '## Blueprint approval exceptions', '']
    approved = review.manifest.get('approvals', {}).get(review.active_approval, {})
    exceptions = approved.get('exceptions', [])
    lines.extend(f'- {exception}' for exception in exceptions)
    if not exceptions:
        lines.append('No exceptions recorded.')
    lines.extend(['', '## Collection checks', ''])
    for name, check in sorted(report.get('checks', {}).items()):
        lines.append(f'- **{name} — {check.get("result", "BLOCKED")}:** '
                     f'{check.get("explanation", "No explanation recorded.")}')
        if reviewer := check.get('reviewer'):
            lines.append(f'  Specification review by {reviewer["name"]}')
            for case in check.get('case_conclusions', []):
                kind = f' ({case["case_kind"]})' if case.get('case_kind') else ''
                lines.append(f'  - `{case["feature_id"]}/{case["check_id"]}`{kind} — {case["result"]}. '
                             f'{case["conclusion"]} Expected outcome — {case["expected"]}. '
                             f'From `{case["source_path"]}` ({case["citation"]}).')
            for interface in check.get('interface_conclusions', []):
                consumers = ', '.join(interface['consumer_slice_ids']) or 'none'
                lines.append(f'  - `{interface["interface_id"]}` — {interface["result"]}. '
                             f'{interface["provider_slice_id"]} provides the interface to {consumers}. '
                             f'{interface["conclusion"]} '
                             f'From `{interface["source_path"]}` ({interface["citation"]}).')
            if applicability := check.get('applicability'):
                lines.append(f'  - Scope `{", ".join(applicability["scope"])}` — {applicability["reason"]}')
        if check.get('evidence_paths'):
            lines.append(f'  Evidence: {", ".join(check["evidence_paths"])}')
    lines.extend(['', '## Reviewed slices', ''])
    for key in order:
        entry = selected[key]
        receipt = reviews.get(key, {})
        packet_reader = getattr(review, 'selected_packet', None)
        packet = packet_reader(key) if callable(packet_reader) else {}
        check_ids = packet.get('acceptance_check_ids', [])
        if not check_ids:
            check_ids = [check.get('check_id') for feature in packet.get('feature_contracts', [])
                         for check in feature.get('acceptance_checks', [])]
        lines.extend([
            f'### {key} — {entry.get("title", "Untitled slice")}', '',
            f'- Features: {", ".join(entry.get("feature_ids", [])) or "None recorded"}',
            f'- Baseline behaviors: {", ".join(packet.get("baseline_ids", [])) or "None recorded"}',
            f'- Acceptance checks: {", ".join(check_ids) or "None recorded"}',
            f'- Scope: {packet.get("scope", "See the linked frozen contract")}',
            f'- Revision: {entry.get("revision", "unknown")}',
            f'- Guide SHA-256: `{entry.get("guide_sha256", "missing")}`',
            f'- Contract SHA-256: `{entry.get("contract_sha256", "missing")}`',
            f'- Dependencies: {", ".join(entry.get("dependencies", [])) or "None"}',
            f'- Allowed paths: {", ".join(entry.get("allowed_paths", [])) or "None recorded"}',
            f'- Review method: {receipt.get("review_mode", "missing")}',
            f'- Review result: {receipt.get("result", "missing")}',
        ])
        walkthrough = receipt.get('handoff_review', {}).get('walkthrough', {})
        if walkthrough:
            lines.append(f'- Independent no-code walkthrough: '
                         f'{receipt.get("handoff_review", {}).get("result", "PASS")} '
                         f'(reviewer attestation; test execution: '
                         f'{"yes" if walkthrough.get("test_execution") else "no"})')
        if receipt.get('constraints'):
            lines.append(f'- Delegation limits: {receipt["constraints"]}')
        lines.append('')
    if report.get('evidence_hashes'):
        lines.extend(['## Evidence included', ''])
        for path, value in sorted(report['evidence_hashes'].items()):
            lines.append(f'- `{path}` — `{value}`')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'
