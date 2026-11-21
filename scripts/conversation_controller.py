"""Conversation gates backed by the initiative's frozen exchange records."""


def required(manifest):
    return manifest.get('conversation_controller_version') == 1 or (
        manifest.get('initiative_mode') == 'blueprint'
        and manifest.get('plan_review_required')
        and manifest.get('requirement_traceability_required')
    )


def normalized(reply):
    return ' '.join(reply.casefold().split()).rstrip('.!').rstrip()


def approval_phrase(version):
    return f'Approve blueprint {version}'


def request_fields(value):
    return {key: item for key, item in value.items()
            if key not in {'approved_by_user', 'user_approval_note', 'approval_exchange_id'}}


def records_for(records, step, version):
    return [record for record in records if record.get('record_type') == 'review_checkpoint'
            and record.get('review_step') == step and record.get('review_version') == version]


def latest_request(records, version):
    return next((record for record in reversed(records_for(records, 'blueprint_approval', version))
                 if record['status'] == 'open'), None)


def workstream_position(records, version, plan, *, allow_skips=False):
    completed = {}
    revisits = []
    for record in records:
        if (record.get('record_type') != 'review_checkpoint' or record.get('review_version') != version
                or record['status'] != 'decided'):
            continue
        step = record.get('review_step')
        action = record.get('review_action')
        if step in {'workstream', 'blueprint_approval'} and action == 'revisit' and record.get('revisit_workstream'):
            revisits.append(record['revisit_workstream'])
        elif step == 'workstream':
            name = record['review_workstream']
            if action == 'continue' or (allow_skips and action == 'skip'):
                completed[name] = record
                if revisits and revisits[-1] == name:
                    revisits.pop()
            else:
                completed.pop(name, None)
    current = revisits[-1] if revisits else next((name for name in plan['workstreams'] if name not in completed), None)
    return completed, current, revisits


def check_revisit(state, value, completed, revisits):
    target = value.get('revisit_workstream')
    state.require(isinstance(target, str) and target in completed and target not in revisits,
                  'A revisit must name a completed workstream with no pending revisit')
    state.require(normalized(value['response']) == normalized(f'Revisit {target}'),
                  f'Wait for the explicit instruction “Revisit {target}”')


def require_presentation(state, documents, question):
    state.require(any(question in doc and any(
        line.strip() and not line.lstrip().startswith('#')
        for line in doc.replace(question, '', 1).splitlines()
    ) for doc in documents.values()), 'Save the displayed explanation and exact question in the terminal presentation')


def check_approval_checkpoint(state, value):
    version = value.get('review_version')
    state.require(required(state.manifest) and state.manifest['stage'] == 'blueprint'
                  and state.identifier(version), 'Finish planning before requesting exact blueprint approval')
    phrase = approval_phrase(version)
    if value['status'] == 'open':
        state.require(not any(value.get(field) for field in ('responds_to', 'review_action')),
                      'Save the approval request before recording the reply')
        draft = value.get('approval_presentation')
        state.require(isinstance(draft, dict) and draft == request_fields(draft)
                      and draft.get('approved_version') == version,
                      'Provide the exact blueprint approval presentation without any user decision')
        preview = approval_preview(state, draft)
        _, documents = state.markdown(value.get('evidence_paths'))
        confirmation = state.api.sibling_module('confirmation')
        if confirmation.enabled(state.manifest):
            question = normalized(value['question'].strip().splitlines()[-1])
            state.require(question in {'do you approve this blueprint?', 'approve this blueprint?',
                                        f'do you {phrase.casefold()}?', f'{phrase.casefold()}?'},
                          'Ask a single approval question about the current blueprint version')
        else:
            state.require(phrase in value['question'], f'Ask the user to say “{phrase}”')
        require_presentation(state, documents, value['question'])
        value['approval_basis'] = preview['presentation_hashes']
        return
    action = value.get('review_action')
    state.require(value['status'] == 'decided' and action in {'confirm', 'revise', 'revisit'}
                  and value.get('responds_to') and not {'approval_presentation', 'approval_basis'} & value.keys(),
                  'Record the actual reply linked to the displayed blueprint approval request')
    if action == 'revisit':
        spec = state.load('blueprints', version)
        plan = state.load('blueprint_plans', spec['plan_version'])
        completed, _, revisits = workstream_position(state.conversations(), version, plan)
        check_revisit(state, value, completed, revisits)
    if action == 'confirm':
        if not state.api.sibling_module('confirmation').enabled(state.manifest):
            state.require(normalized(value['response']) == normalized(phrase),
                          f'Wait for the explicit instruction “{phrase}”')
        state.require(all(record['exchange_id'] == value['responds_to'] for record in state.pending()),
                      'Resolve outstanding review questions before confirming blueprint approval')


def approval_preview(state, draft):
    state.require(state.manifest['stage'] == 'blueprint' and draft == request_fields(draft),
                  'Provide the current blueprint presentation with the approval fields omitted')
    preview = state.validate_approval(draft, requesting=True)
    contextual = state.api.sibling_module('confirmation').enabled(state.manifest)
    return {'approval_presentation': draft,
            'instruction': 'Do you approve this blueprint?' if contextual else approval_phrase(draft['approved_version']),
            'presentation_hashes': preview['presentation_evidence_hashes']}


def approval_exchange_hashes(state, value, presentation_hashes, *, current):
    exchange_id = value.get('approval_exchange_id')
    state.require(state.identifier(exchange_id),
                  'Record the separately displayed blueprint approval request and explicit reply before approving')
    answer = state.load('conversation', exchange_id)
    version = value['approved_version']
    state.require(answer.get('record_type') == 'review_checkpoint'
                  and answer.get('review_step') == 'blueprint_approval'
                  and answer.get('review_version') == version
                  and answer.get('review_action') == 'confirm'
                  and answer.get('response') == value['user_approval_note'],
                  'Use the exact recorded user reply before approving')
    confirmation = state.api.sibling_module('confirmation')
    if (current and confirmation.enabled(state.manifest)) or answer.get('confirmation_origin'):
        state.require(answer.get('confirmation_origin', {}).get('action') == 'blueprint_approval',
                      'A verified user-originated blueprint approval reply is required')
        confirmation.clarify(state, 'blueprint_approval', answer['response'], answer['question'],
                             explicit=confirmation.blueprint_replies(version))
    else:
        state.require(normalized(answer['response']) == normalized(approval_phrase(version)),
                      f'Record the exact reply “{approval_phrase(version)}” before approving')
    request = state.load('conversation', answer['responds_to'])
    state.require(request.get('approval_basis') == presentation_hashes,
                  'The reviewed evidence changed; present a fresh blueprint approval request')
    if current:
        state.require(request == latest_request(state.conversations(), version),
                      'Use the reply to the latest recorded blueprint approval request')
        state.require(request['approval_presentation'] == request_fields(value),
                      'Approval must match the exact recorded approval request')
    return state.review_exchange_hashes([answer])


def blueprint_progress(state, records, pending):
    versions = state.manifest.get('blueprints', {})
    if state.manifest['stage'] != 'blueprint' or not versions:
        return None
    version = max(versions, key=lambda key: versions[key]['sequence'])
    spec = state.load('blueprints', version)
    plan = state.load('blueprint_plans', spec['plan_version'])
    completed, current, revisits = workstream_position(
        records, version, plan, allow_skips=not required(state.manifest))
    coverage = state.manifest.get('blueprint_coverage', {}).get(version)
    checkpoint = pending[0] if pending else None
    if checkpoint:
        target = checkpoint.get('review_target') or checkpoint['question']
        action = f"Wait for the user's reply on {target}"
    elif not coverage:
        action = f'Validate requirement coverage for blueprint {version}.'
    elif coverage.get('coverage_gaps'):
        action = f"Resolve the coverage gaps in {coverage['report_path']}."
    elif revisits:
        action = f'Revisit blueprint workstream {current}.'
    elif current:
        action = f'Present blueprint workstream {current}.'
    else:
        request = latest_request(records, version)
        later = records[records.index(request) + 1:] if request else []
        confirmed = request and not records_for(later, 'workstream', version) and any(
            record.get('responds_to') == request['exchange_id'] and record.get('review_action') == 'confirm'
            for record in records_for(later, 'blueprint_approval', version))
        action = (f'Record approval for blueprint {version} using the saved reply.' if confirmed else
                  f'Validate consistency and present the formal approval request for blueprint {version}.')
    return {'stage': 'blueprint', 'version': version, 'plan_version': spec['plan_version'],
            'completed': len(completed),
            'total': len(plan['workstreams']), 'current_item': current,
            'current_number': plan['workstreams'].index(current) + 1 if current else None,
            'completed_workstreams': [name for name in plan['workstreams']
                                      if name in completed and completed[name]['review_action'] == 'continue'],
            'skipped_workstreams': [name for name in plan['workstreams']
                                    if name in completed and completed[name]['review_action'] == 'skip'],
            'completed_exchange_ids': [record['exchange_id'] for record in completed.values()],
            'revisits': revisits,
            'pending_question_id': checkpoint['exchange_id'] if checkpoint else None,
            'next_action': action}


def final_acceptance_evidence(state, value):
    phrase = f"Accept final audit {value['audit_id']}"
    state.require(normalized(value['user_response']) == normalized(phrase),
                  f'Wait for the explicit instruction “{phrase}”')
    hashes, documents = state.markdown([value.get('presentation_path')])
    presentation = documents[value['presentation_path']]
    state.require(phrase in presentation and value['audit_sha256'] in presentation,
                  'Save the displayed final acceptance question with the exact audit ID and digest')
    return hashes
