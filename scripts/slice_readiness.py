"""Risk-based, read-only review of a frozen implementation handoff."""


RISK_FACTORS = {
    'architecture': 'Material architectural change',
    'security': 'Material security or permissions impact',
    'dependencies': 'Complex dependency interfaces',
    'ambiguity': 'Substantial ambiguity or insufficient confidence after baseline checks',
}
DEPTH_LABELS = {'standard': 'Standard', 'detailed': 'Detailed specification analysis',
                'independent': 'Independent fresh-agent review'}


def depth(review, packet, assessment):
    review.require(isinstance(assessment, dict), 'Assess handoff complexity and risk first')
    complexity = assessment.get('complexity')
    factors = assessment.get('risk_factors')
    review.require(complexity in ('simple', 'moderate', 'complex')
                   and review.blueprint.strings(factors, False) and set(factors) <= RISK_FACTORS.keys()
                   and review.blueprint.text(assessment.get('rationale')),
                   'Record complexity, applicable handoff risk factors and a source-based rationale')
    review.blueprint.check_placeholders(assessment['rationale'], 'handoff risk rationale')
    key = packet['slice_id']
    neighbors = set(packet['dependencies']) | {
        other for other, entry in review.registry['selected'].items() if key in entry['dependencies']}
    reasons = [RISK_FACTORS[factor] for factor in factors]
    if complexity == 'complex':
        reasons.append('Complex implementation')
    if len(neighbors) > 1 or len(packet.get('shared_contracts', {})) > 1:
        reasons.append('Multiple shared dependencies or interfaces')
    reasons.extend(review.registry.get('handoff_escalations', {}).get(key, {}).values())
    if reasons:
        return 'independent', reasons
    if complexity == 'moderate' or neighbors or packet.get('shared_contracts') or len(packet['surfaces']) > 1:
        review.require(review.blueprint.text(assessment.get('specification_analysis')),
                       'Record the deeper comparison of applicable specifications and interfaces')
        review.blueprint.check_placeholders(assessment['specification_analysis'], 'handoff specification analysis')
        return 'detailed', []
    return 'standard', []


def terminal(entry, receipt):
    target = receipt['target']
    assessment = receipt['assessment']
    handoff = receipt['handoff_review']
    return '\n'.join([
        f"{target['slice_id']} — {entry['title']} (revision {target['proposal_revision']})",
        'Ready for discussion',
        f"Approved acceptance checks traced — {len(handoff['acceptance_checks'])}",
        f"Guide findings resolved — {len(handoff['findings'])}",
        f"Verification depth — {DEPTH_LABELS[receipt['depth']]}",
        *(f"  {reason}" for reason in receipt['reasons']),
        f"Assessment — {assessment['rationale']}",
        *([f"Ambiguity resolved — {assessment['ambiguity_resolution']}"]
          if 'ambiguity' in assessment['risk_factors'] else []),
        f"Guide — {entry['guide_path']}",
        f"Contract — {entry['contract_path']}",
    ]) + '\n'
