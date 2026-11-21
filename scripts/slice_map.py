"""Build and display the approved-blueprint decomposition for map review."""

from collections import defaultdict
from posixpath import relpath
from urllib.parse import quote


def duplicated_checks(review, packets):
    owners = defaultdict(list)
    for key, packet in packets.items():
        for feature in packet['feature_contracts']:
            for check in feature['acceptance_checks']:
                owners[feature['feature_id'], check['check_id']].append(key)
    duplicates = [f'{feature}/{check} → {", ".join(slices)}'
                  for (feature, check), slices in sorted(owners.items()) if len(slices) > 1]
    review.require(not duplicates, 'Acceptance responsibility has multiple owners: ' + '; '.join(duplicates))


def breakdown(approval_id, packets, coverage):
    slices = []
    for key, packet in packets.items():
        checks = {feature['feature_id']: [check['check_id'] for check in feature['acceptance_checks']]
                  for feature in packet['feature_contracts']}
        specs = {feature['feature_id']: feature['spec_paths'] for feature in packet['feature_contracts']}
        mappings = []
        for row in coverage['mappings']:
            assigned = [check for check in row['acceptance_ids'] if check in checks.get(row['feature_id'], ())]
            if assigned:
                mappings.append({'requirement_id': row['baseline_id'], 'feature_id': row['feature_id'],
                                 'spec_path': row.get('spec_path') or specs[row['feature_id']][0], 'check_ids': assigned})
        slices.append({
            'slice_id': key,
            **{field: packet[field] for field in ('title', 'scope', 'allowed_paths',
                                                 'prohibited_deviations', 'shared_contracts')},
            'replaces_slice_ids': packet.get('replaces_slice_ids', []),
            'boundary_reason': packet.get('boundary_reason') or 'Carries forward the existing slice handoff.',
            'outcomes': [{'check_id': check['check_id'], 'expected': check['expected']}
                         for feature in packet['feature_contracts'] for check in feature['acceptance_checks']],
            'baseline_paths': sorted({entry['path'] for entry in packet.get('approved_source_anchors', [])}),
            'features': [{'feature_id': feature['feature_id'], 'spec_paths': feature['spec_paths'],
                          'classification': feature['classification'], 'check_ids': checks[feature['feature_id']]}
                         for feature in packet['feature_contracts']],
            'mappings': mappings,
            'dependencies': [
                {field: value for field, value in contract.items() if field != 'evidence_paths'}
                for contract in packet.get('dependency_contracts', [])
            ],
        })
    return {'approval_id': approval_id, 'blueprint_version': coverage['version'], 'slices': slices,
            'source_requirement_ids': [row['requirement_id'] for row in coverage.get('requirements', [])]}


def dependency_arrows(data):
    return [f"{dep['dependency_id']} → {item['slice_id']}"
            for item in data['slices'] for dep in item['dependencies']]


def terminal(data, selected, target, status):
    lines = [f"IMPLEMENTATION SLICE MAP — {target['map_id']}", '',
             f"Blueprint {data['blueprint_version']} · approval {data['approval_id']}", '']
    for number, item in enumerate(data['slices'], 1):
        key = item['slice_id']
        checks = sum(len(feature['check_ids']) for feature in item['features'])
        requirements = len({row['requirement_id'] for row in item['mappings']})
        lines.extend([f"{number:02d}  {item['title']} ({key})",
                      f"    {item['boundary_reason']}",
                      '    Blueprint features — ' + ', '.join(feature['feature_id'] for feature in item['features']),
                      f'    Coverage — {requirements} source or baseline IDs, {checks} acceptance checks',
                      '    Writable paths — ' + ', '.join(item['allowed_paths']),
                      '    Guide — ' + selected[key]['guide_path']])
        lines.extend(f"    Approved outcome ({row['check_id']}) — {row['expected']}" for row in item['outcomes'][:3])
        if len(item['outcomes']) > 3:
            lines.append('    See the detailed map for the remaining approved outcomes.')
        lines.append('')
    lines.extend(['DEPENDENCIES', 'Required by (prerequisite → dependent)',
                  *(dependency_arrows(data) or ['No dependencies between slices.']),
                  '', 'STATUS', status, 'Coverage, ownership and dependency checks passed.',
                  'No implementation is authorized by this map.',
                  f"Detailed requirements, boundaries and diagrams — {target['map_path']}",
                  f"Map SHA-256 — {target['map_sha256']}", ''])
    return '\n'.join(lines)


def render(map_id, data, selected, coverage_path):
    def link(label, path, anchor=''):
        return f'[{label}]({quote(relpath(path, "slice-review/maps"))}{anchor})'

    lines = ['# Implementation slice map — proposed', '', f"Map `{map_id}` for blueprint "
             f"`{data['blueprint_version']}` (approval `{data['approval_id']}`).",
             '', 'This checkpoint confirms how the work is divided. No implementation is authorized by this map.',
             '', '[Current slice register](../register.md) · ' + link('Blueprint coverage', coverage_path),
             '', '## Dependencies', '', 'Required by (prerequisite → dependent)', '']
    arrows = dependency_arrows(data)
    if arrows:
        lines.extend(f'- {arrow}' for arrow in arrows)
        nodes = {item['slice_id']: f'S{index}' for index, item in enumerate(data['slices'], 1)}
        lines.extend(['', '```mermaid', 'flowchart LR'])
        lines.extend(f'    {node}["{key}"]' for key, node in nodes.items())
        lines.extend(f"    {nodes[dep['dependency_id']]} --> {nodes[item['slice_id']]}"
                     for item in data['slices'] for dep in item['dependencies'])
        lines.append('```')
    else:
        lines.append('- Each slice can start independently after package authorization and release.')
    for index, item in enumerate(data['slices'], 1):
        key = item['slice_id']
        entry = selected[key]
        lines.extend(['', f"## {index}. {item['title']} (`{key}`)", '', item['boundary_reason'], '',
                      '- Writable files and boundaries — ' + ', '.join(f'`{path}`' for path in item['allowed_paths']),
                      '- Existing source to inspect — ' +
                      (', '.join(f'`{path}`' for path in item['baseline_paths']) or 'No baseline source anchors recorded.'),
                      '- Preserve these boundaries — ' + ' · '.join(item['prohibited_deviations']),
                      '- ' + link('Implementation guide and illustrations', entry['guide_path']) + ' · ' +
                      link('Handoff contract', entry['contract_path'])])
        lines.extend(f"- Approved outcome (`{row['check_id']}`) — {row['expected']}" for row in item['outcomes'])
        for dep in item['dependencies']:
            detail = dep.get('reason') or ', '.join(dep.get('contract_ids', []) + dep.get('paths', []))
            lines.append(f"- Uses `{dep['dependency_id']}` ({dep['kind'].replace('_', ' ')}) — {detail}")
        for name, contract in item['shared_contracts'].items():
            lines.append(f"- {contract['role'].capitalize()} shared contract `{name}`.")
        for feature in item['features']:
            docs = ', '.join(link(feature['feature_id'], path, '#feature-' + feature['feature_id'])
                             for path in feature['spec_paths'])
            lines.append(f"- {feature['classification'].capitalize()} · {docs} · Checks "
                         + ', '.join(f'`{check}`' for check in feature['check_ids']))
        for row in item['mappings']:
            checks = ', '.join(link(check, row['spec_path'], '#check-' + check.lower()) for check in row['check_ids'])
            key = row['requirement_id']
            reference = (link(key, coverage_path, '#source-item-' + key)
                         if key in data['source_requirement_ids'] else f'`{key}`')
            lines.append(f'- Requirement or baseline {reference} → {checks}')
    lines.extend(['', '## Coverage and boundaries', '',
                  'Approved checks and audited behaviors have assignments. Writable ownership, dependencies and '
                  'declared interfaces passed validation. Discuss any boundary changes before confirming the map.'])
    return '\n'.join(lines) + '\n'
