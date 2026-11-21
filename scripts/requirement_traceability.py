"""Source inventory, blueprint coverage audit, and delivery evidence."""
from collections import Counter, defaultdict
from hashlib import sha256
from urllib.parse import quote
import posixpath
import re


KINDS = {'requirement', 'constraint', 'decision', 'acceptance_criterion'}
STATUSES = {
    'covered': 'Covered',
    'partially_covered': 'Partially covered',
    'already_satisfied': 'Already satisfied',
    'conflict': 'Conflict',
    'unresolved': 'Unresolved',
    'excluded': 'Excluded',
}
COMPLETE = {'covered', 'already_satisfied', 'excluded'}
SCOPE_FIELDS = {'requirement_exclusion', 'requirement_conflict'}
SCENARIO_LABEL = re.compile(
    r'^(?:\d+[.)]\s+[-*` ]*)?'
    r'(?:(?:scenario(?: outline)?|acceptance criterion|test case)(?:\s+\d+\b|\s*:)|(?:ac|tc)-\d+\b)',
    re.IGNORECASE,
)


def gap_summary(issues, path):
    first = '. '.join(issues[:3])
    return f'{len(issues)} outstanding coverage gaps. {first}. Full audit at {path}'


def check_inventory(state, rows):
    by_id = {row['requirement_id']: row for row in rows}
    for row in rows:
        key = row['requirement_id']
        state.require(state.text(row.get('kind')) and row['kind'] in KINDS and all(state.text(row.get(field)) for field in
                      ('request_reference', 'repository_finding')),
                      f'{key}: record the source location, item kind and repository finding')
        state.check_placeholders({field: row.get(field) for field in
                                  ('text', 'request_reference', 'repository_finding')}, key)
        parent = row.get('parent_requirement_id')
        if row['kind'] == 'acceptance_criterion':
            state.require(state.identifier(parent) and parent in by_id
                          and by_id[parent].get('kind') != 'acceptance_criterion',
                          f'{key}: acceptance criterion needs a distinct parent requirement ID')
        else:
            state.require(parent is None, f'{key}: only acceptance criteria have a parent requirement')


def check_source_lines(state, paths, mappings, requirements, hashes):
    state.require(isinstance(mappings, dict) and mappings.keys() == set(paths),
                  'Reconcile the source lines of every saved source document')
    for path in paths:
        document = state.api.artifact_file(state.directory, path).read_bytes()
        state.require(sha256(document).hexdigest() == hashes[path],
                      f'{path}: saved source changed during reconciliation')
        try:
            lines = document.decode('utf-8').splitlines()
        except UnicodeDecodeError as error:
            raise state.api.StateError(f'{path}: provide a UTF-8 text copy for source line reconciliation') from error
        ranges = mappings[path]
        state.require(isinstance(ranges, list) and ranges, f'{path}: record source line ranges')
        accounted, criteria = set(), set()
        for span in ranges:
            state.require(isinstance(span, dict), f'{path}: invalid source line range')
            first, last = span.get('start_line'), span.get('end_line')
            state.require(type(first) is int and type(last) is int and 1 <= first <= last <= len(lines),
                          f'{path}: source line range is outside the saved text')
            ids = span.get('requirement_ids')
            state.require(state.strings(ids, False) and set(ids) <= requirements.keys(),
                          f'{path}:{first}: source line ranges must name existing inventory IDs')
            if not ids:
                reason = span.get('context_reason')
                state.require(state.text(reason), f'{path}:{first}: explain why this range is supporting context')
                state.check_placeholders(reason, f'{path}:{first} context')
            numbers = set(range(first, last + 1))
            state.require(accounted.isdisjoint(numbers), f'{path}:{first}: overlapping source line ranges')
            accounted.update(numbers)
            labelled = [number for number in numbers if SCENARIO_LABEL.match(lines[number - 1].lstrip(' \t-*+>#|`'))]
            if labelled:
                linked = {key for key in ids if requirements[key] == 'acceptance_criterion'}
                state.require(len(labelled) == 1 and len(linked) == 1 and linked.isdisjoint(criteria),
                              f'{path}:{min(labelled)}: each labelled scenario needs a distinct acceptance criterion')
                criteria.update(linked)
        missing = next((number for number, line in enumerate(lines, 1) if line.strip() and number not in accounted), None)
        state.require(missing is None, f'{path}: unaccounted source line {missing}')


def proposal_phrase(state, proposal):
    ids = proposal.get('requirement_ids', [])
    state.require(proposal['status'] in {'open', 'proposed'} and state.strings(ids)
                  and all(state.identifier(key) for key in ids)
                  and not proposal.get('authorized_changes') and len(SCOPE_FIELDS & proposal.keys()) == 1,
                  'Use a separate open proposal for a scoped exclusion or conflict resolution')
    if 'requirement_exclusion' in proposal:
        exclusion = proposal['requirement_exclusion']
        state.require(state.text(exclusion) and exclusion in proposal['question'],
                      'Describe the consequences of exclusion in the displayed question')
        state.check_placeholders(exclusion, 'requirement_exclusion')
        phrase = 'Approve exclusion of ' + ', '.join(sorted(ids))
    else:
        conflict = proposal['requirement_conflict']
        state.require(len(ids) == 1 and isinstance(conflict, dict) and all(
            state.text(conflict.get(field)) and conflict[field] in proposal['question']
            for field in ('finding', 'resolution')),
            'Describe the specific recorded conflict and proposed resolution in the displayed question')
        state.check_placeholders(conflict, 'requirement_conflict')
        phrase = f'Approve resolution of {ids[0]}'
    state.require(phrase.casefold() in proposal['question'].casefold(),
                  f'Show the exact confirmation phrase “{phrase}” in the proposal')
    return phrase


def confirms(response, phrase):
    normalized = ' '.join(response.casefold().split()).rstrip('.!')
    normalized = re.sub(r'^(?:yes,\s*)?(?:i |we )?', '', normalized)
    return normalized == phrase.casefold()


def check_scope_exchange(state, value):
    if SCOPE_FIELDS & value.keys():
        proposal_phrase(state, value)
        return
    if not value.get('responds_to'):
        return
    proposal = state.load('conversation', value['responds_to'])
    if not SCOPE_FIELDS & proposal.keys():
        return
    phrase = proposal_phrase(state, proposal)
    changes = value.get('authorized_changes', [])
    if value['status'] == 'rejected':
        state.require(not changes, 'A refusal cannot authorize a scope change')
        return
    target = 'this exclusion' if 'requirement_exclusion' in proposal else 'this conflict resolution'
    state.require(confirms(value['response'], phrase), f'Record explicit approval of {target} with “{phrase}”')
    if 'requirement_exclusion' in proposal:
        expected = [{'baseline_id': key, 'classification': 'REMOVE'} for key in proposal['requirement_ids']]
        state.require(len(changes) == len(expected) and all(change in changes for change in expected),
                      'An exclusion approval must authorize removal of every proposed source ID')


def approved_proposal(state, decision, field):
    if decision.get('responds_to'):
        proposal = state.load('conversation', decision['responds_to'])
        if field in proposal and confirms(decision['response'], proposal_phrase(state, proposal)):
            return proposal
    return {}


def source_rows(alignment):
    return {row['requirement_id']: {
        'kind': row['kind'], 'request_reference': row['request_reference'],
        'repository_status': row['classification'], 'repository_finding': row['finding'],
        'repository_evidence_paths': row.get('evidence_paths', []),
        'repository_source_paths': row.get('source_paths', []),
        **({'parent_requirement_id': row['parent_requirement_id']} if 'parent_requirement_id' in row else {}),
    } for row in alignment.get('requirement_mappings', [])}


def audit(state, spec, baseline, plan, payload):
    """Validate submitted references and compute approval blockers from frozen inputs."""
    rows = {row['requirement_id']: row for row in baseline.get('requirements', [])}
    features = {feature['feature_id']: feature for feature in spec['features']}
    workstreams = state.plan_requirement_mapping(plan, baseline)
    submitted = payload.get('requirements')
    state.require(isinstance(submitted, list), 'Record blueprint coverage status for each source item')
    statuses, mappings, issues = {}, defaultdict(list), []
    for entry in submitted:
        state.require(isinstance(entry, dict), 'Invalid source coverage status')
        key = entry.get('requirement_id')
        state.require(state.identifier(key) and key in rows and key not in statuses
                      and state.text(entry.get('status')) and entry['status'] in STATUSES and state.text(entry.get('reason')),
                      'Coverage status needs a unique known source ID, valid status and concrete reason')
        state.check_placeholders(entry['reason'], key)
        for field in ('exclusion_decision_id', 'resolution_decision_id'):
            state.require(field not in entry or state.identifier(entry[field]), f'{key}: invalid {field}')
        state.require('exclusion_decision_id' not in entry or entry['status'] == 'excluded',
                      f'{key}: exclusion decisions only apply to excluded items')
        statuses[key] = entry
    owners_by_check, owners_by_command = {}, {}
    pairs = set()
    for entry in payload['mappings']:
        state.require(isinstance(entry, dict), 'Invalid coverage mapping')
        key, feature_id = entry.get('baseline_id'), entry.get('feature_id')
        state.require(state.identifier(key) and state.identifier(feature_id) and feature_id in features
                      and key in features[feature_id]['baseline_ids'] and (key, feature_id) not in pairs,
                      'Unknown or duplicate baseline/feature coverage mapping')
        pairs.add((key, feature_id))
        feature = features[feature_id]
        checks = {check['check_id']: check for check in feature['acceptance_checks']}
        selected = entry.get('acceptance_ids')
        state.require(state.strings(selected) and set(selected) <= checks.keys(),
                      f'{key}: mapping must name exact acceptance checks on {feature_id}')
        if key in rows:
            state.require(entry.get('spec_path') in feature['spec_paths'],
                          f'{key}: name the precise generated feature specification path')
            if rows[key]['kind'] == 'acceptance_criterion':
                for check_id in selected:
                    other = owners_by_check.setdefault(check_id, key)
                    state.require(other == key, f'Use distinct check IDs for source scenarios {other} and {key}')
                    command = tuple(checks[check_id]['command'])
                    other_check = owners_by_command.setdefault(command, check_id)
                    state.require(other_check == check_id,
                                  f'Source scenario checks {other_check} and {check_id} need distinct command invocations')
        else:
            state.require(set(selected) == checks.keys(), f'{key}: preserve every observed feature acceptance check')
        mappings[key].append(entry)

    expected_pairs = {(key, feature['feature_id']) for feature in features.values() for key in feature['baseline_ids']}
    missing_pairs = expected_pairs - pairs
    issues.extend(f'{key}: missing mapping to {feature_id}' for key, feature_id in sorted(missing_pairs))
    missing_ids = {key for key, _ in missing_pairs}
    issues.extend(f"{feature['feature_id']}: observed behavior has no blueprint mapping"
                  for feature in baseline.get('features', []) if feature['feature_id'] not in mappings)
    effective = statuses.copy()
    for key, row in rows.items():
        entry = statuses.get(key)
        if entry is None:
            issues.append(f'{key}: no coverage status recorded')
            continue
        status = entry['status']
        issue_count = len(issues)
        if status not in COMPLETE:
            issues.append(f"{key}: {STATUSES[status]} — {entry['reason']}")
        if status == 'excluded':
            decision_id = entry.get('exclusion_decision_id')
            state.require(state.identifier(decision_id) and decision_id in spec['decision_ids'],
                          f'{key}: exclusion needs a user decision included in the blueprint')
            decision = state.load('conversation', decision_id)
            state.decisions([decision_id], key)
            proposal = approved_proposal(state, decision, 'requirement_exclusion')
            state.require(key in proposal.get('requirement_ids', [])
                          and {'baseline_id': key, 'classification': 'REMOVE'} in decision.get('authorized_changes', []),
                          f'{key}: exclusion needs an explicit answer authorizing removal of this source ID')
            state.require(not any(key in feature['baseline_ids'] for feature in features.values()),
                          f'{key}: an excluded item still appears in the blueprint scope')
        elif status in COMPLETE:
            mapped_features = [features[entry['feature_id']] for entry in mappings[key]]
            streams = {name for feature in mapped_features for name in feature['workstreams']}
            if not mapped_features or not set(workstreams[key]) <= streams:
                issues.append(f'{key}: missing specification or checks across the mapped roadmap workstreams')
            if status == 'already_satisfied' and not (
                    row['repository_status'] == 'already_implemented'
                    and all(feature['classification'] in {'KEEP', 'MOVE'} for feature in mapped_features)):
                issues.append(f'{key}: already satisfied requires verified existing behavior and preserved contracts')
            if (row['repository_status'] in {'missing', 'partially_implemented'} and mapped_features
                    and all(feature['classification'] in {'KEEP', 'MOVE'} for feature in mapped_features)):
                issues.append(f'{key}: the repository gap needs a specified change')
            if row['repository_status'] == 'conflicting':
                decision_id = entry.get('resolution_decision_id')
                bound = set(spec['decision_ids']) | {
                    decision for feature in mapped_features for decision in feature['decision_ids']
                }
                if state.identifier(decision_id) and decision_id in bound:
                    state.decisions([decision_id], key)
                    decision = state.load('conversation', decision_id)
                    proposal = approved_proposal(state, decision, 'requirement_conflict')
                    state.require(proposal.get('requirement_ids') == [key]
                                  and proposal['requirement_conflict']['finding'] == row['repository_finding'],
                                  f'{key}: resolution must explicitly approve the specific recorded conflict')
                else:
                    issues.append(f'{key}: repository conflict needs a scoped resolution decision')
        parent = row.get('parent_requirement_id')
        if parent and statuses.get(parent, {}).get('status') == 'excluded' and status != 'excluded':
            issues.append(f'{key}: parent {parent} is excluded, but this scenario has no approved exclusion')
        if status in COMPLETE and (len(issues) > issue_count or key in missing_ids):
            effective[key] = {**entry, 'status': 'partially_covered'}

    counts = Counter(effective.get(key, {}).get('status', 'unresolved') for key in rows)
    overview = [f'Blueprint Coverage Overview for {spec["version"]}',
                f'{sum(row["kind"] != "acceptance_criterion" for row in rows.values())} source requirements, constraints and decisions',
                f'{sum(row["kind"] == "acceptance_criterion" for row in rows.values())} source acceptance criteria',
                ', '.join(f'{label}: {counts[status]}' for status, label in STATUSES.items()),
                f'{len(issues)} outstanding coverage gaps']
    for name in plan['workstreams']:
        scoped = [feature for feature in features.values() if name in feature['workstreams']]
        ids = [key for key, names in workstreams.items() if name in names]
        overview.append(f'{name} — {spec["workstream_summaries"][name]} ({len(ids)} source items, {len(scoped)} features)')
    return overview, issues, effective, mappings


def report(state, spec, baseline, plan, coverage):
    overview, issues, statuses, mappings = audit(state, spec, baseline, plan, coverage)
    path = f'blueprint-coverage/{spec["version"]}.md'
    lines = [f'# {overview[0]}', '', *(f'- {line.strip()}' for line in overview[1:]), '',
             '## Coverage gaps', '', *(f'- {issue}' for issue in issues)]
    if not issues:
        lines.append('Every recorded source item and observed behavior has a disposition.')
    lines.extend(['', 'Coverage describes the specification. Delivery verification is recorded with each approved check.',
                  '', '## Workstreams'])
    for name in plan['workstreams']:
        lines.extend(['', f'### {name}', '', spec['workstream_summaries'][name]])
        ids = [key for key, names in plan['requirement_workstreams'].items() if name in names]
        if ids:
            lines.extend(['', 'Source items — ' + ', '.join(f'[{key}](#source-item-{key})' for key in ids)])
        for feature in spec['features']:
            if name in feature['workstreams']:
                target = quote(posixpath.relpath(feature['spec_paths'][0], posixpath.dirname(path)))
                key = feature['feature_id']
                lines.append(f"- [{key}]({target}#feature-{key}) — {feature['purpose']}")
    lines.extend(['', '## Source inventory'])
    for row in baseline.get('requirements', []):
        key = row['requirement_id']
        status = statuses.get(key, {'status':'unresolved', 'reason':'No coverage status recorded.'})
        lines.extend(['', f'### Source item {key}', '', row['text'], '',
                      f"- Source ({row['kind']}) — {row['request_reference']}",
                      f"- Repository — {row['repository_status'].replace('_', ' ')}. {row['repository_finding']}",
                      f"- Blueprint — {STATUSES[status['status']]}. {status['reason']}"])
        evidence = row['repository_source_paths'] + row['repository_evidence_paths']
        if evidence:
            lines.append('- Repository references — ' + ', '.join(f'`{item}`' for item in evidence))
        if parent := row.get('parent_requirement_id'):
            lines.append(f'- Parent — [{parent}](#source-item-{parent})')
        if decision_id := status.get('exclusion_decision_id'):
            lines.append(f'- User authorization — [{decision_id}](../conversation/{decision_id}.json)')
        if decision_id := status.get('resolution_decision_id'):
            lines.append(f'- Conflict resolution — [{decision_id}](../conversation/{decision_id}.json)')
        for mapping in mappings[key]:
            feature_id = mapping['feature_id']
            target = quote(posixpath.relpath(mapping['spec_path'], posixpath.dirname(path)))
            checks = ', '.join(f'[{key}]({target}#check-{key.lower()})' for key in mapping['acceptance_ids'])
            lines.append(f'- [{feature_id}]({target}#feature-{feature_id}) — {checks}')
    lines.extend(['', '## Observed behavior', ''])
    source_ids = {row['requirement_id'] for row in baseline.get('requirements', [])}
    for key, entries in mappings.items():
        if key not in source_ids:
            for entry in entries:
                lines.append(f"- {key} → {entry['feature_id']} — {', '.join(entry['acceptance_ids'])}")
    return path, '\n'.join(lines) + '\n', overview, issues


def slice_links(coverage, selected):
    feature_checks = {feature['feature_id']: {check['check_id'] for check in feature['acceptance_checks']}
                      for feature in selected}
    source_ids = {row['requirement_id'] for row in coverage['requirements']}
    result = []
    for row in coverage['mappings']:
        checks = [key for key in row['acceptance_ids'] if key in feature_checks.get(row['feature_id'], ())]
        if row['baseline_id'] in source_ids and checks:
            result.append({'requirement_id': row['baseline_id'], 'feature_id': row['feature_id'],
                           'spec_path': row['spec_path'], 'check_ids': checks})
    return result


def final_links(coverage, evidence):
    expected_by_requirement = defaultdict(set)
    for row in coverage['mappings']:
        expected_by_requirement[row['baseline_id']].update((row['feature_id'], key) for key in row['acceptance_ids'])
    by_requirement = defaultdict(list)
    for link in evidence:
        by_requirement[link['requirement_id']].append(link)
    for row in coverage['requirements']:
        key = row['requirement_id']
        verified = {(entry['feature_id'], check) for entry in by_requirement[key] for check in entry['check_ids']}
        if expected_by_requirement[key] != verified:
            raise ValueError(f'{key}: final report lacks the approved verification evidence')
    return [{**row, 'verification': by_requirement[row['requirement_id']]} for row in coverage['requirements']]
