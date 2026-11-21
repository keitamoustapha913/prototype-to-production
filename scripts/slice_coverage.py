"""Revision-bound evidence of what Codex presented during a slice review."""

from hashlib import sha256
import json
import re


TOPIC_SECTIONS = {
    'purpose': ('In plain language', 'Why this slice exists'),
    'existing_functionality': ('What exists now',),
    'proposed_changes': ('Finished experience or service contract', 'Detailed functional requirements',
                         'Visual specification', 'State and scenario matrix'),
    'illustrations': ('Visual specification', 'Worked examples and diagrams'),
    'technical_implementation': ('Architecture and anatomy', 'Interface specification', 'Data and security rules',
                                 'Exact implementation footprint', 'Step-by-step implementation recipe'),
    'dependencies': ('Dependencies and integration', 'Safety, rollback and prohibited deviations'),
    'verification': ('Traceability matrix', 'Tests and acceptance evidence', 'Definition of done'),
    'open_decisions': ('Change and approval history',),
}

RECAP_SECTIONS = {'expected_result', 'review_changes', 'material_questions', 'approval_scope'}
FLOW_EDGE = re.compile(r'\S[ \t]*(?:-+>|=+>|→|←|↓|↑)[ \t]*\S')
TABLE_DIVIDER = re.compile(r'^\s*\|?[ \t]*:?-{3,}:?[ \t]*\|[ \t]*:?-{3,}:?', re.M)


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def discussion(review, key):
    return [record['exchange_id'] for record in review.blueprint.conversations()
            if record['phase'] == 'slice_review' and record.get('slice_id') == key
            and record.get('record_type') not in ('recap', 'slice_consent')]


def documents(review, key, ready):
    packet = review.selected_packet(key)
    path = packet['guide_path']
    parser = review.api.sibling_module('slice_guide')
    sections = parser.markdown_sections(review.api.artifact_file(review.directory, path).read_text(encoding='utf-8'))
    illustrations = [packet['artifact_hashes'][path] for path in packet.get('artifact_paths', [])]
    illustrations.extend(packet['asset_hashes'].values())
    extra = {
        'existing_functionality': ready['source_hashes'],
        'proposed_changes': packet['feature_contracts'],
        'illustrations': sorted(illustrations) + [ready['target'].get('visual_sha256')],
        'technical_implementation': [ready['source_hashes'], packet['feature_contracts']],
        'dependencies': [packet['dependencies'], packet['allowed_paths'], packet['shared_contracts'],
                         packet['dependency_contracts']],
        'verification': packet['feature_contracts'],
        'open_decisions': [packet['decision_ids'], packet['unresolved_question_ids']],
    }
    return {topic: {'guide_path': path, 'guide_sections': list(headings),
                    'specification_sha256': fingerprint([[sections[heading] for heading in headings],
                                                         extra.get(topic)])}
            for topic, headings in TOPIC_SECTIONS.items()}


def read_excerpt(review, path):
    review.require(review.blueprint.text(path), 'Supply the saved conversation excerpt path')
    hashes = review.api.sibling_module('slice_evidence').evidence_hashes(review, [path])
    body = review.api.artifact_file(review.directory, path).read_bytes()
    review.require(sha256(body).hexdigest() == hashes[path], 'The conversation excerpt changed while it was read')
    try:
        text = body.decode('utf-8').replace('\r\n', '\n')
    except UnicodeError as error:
        raise review.api.StateError('Save the conversation excerpt as UTF-8') from error
    return text, hashes[path]


def save_progress(review, key, progress):
    revision = progress['target']['proposal_revision']
    receipt = f'slice-proposals/{key}/walkthroughs/r{revision}-{fingerprint(progress)}.json'
    review.files[receipt] = progress
    review.registry.setdefault('walkthroughs', {})[key] = progress | {'path': receipt}
    review.events.append({'type': 'slice_walkthrough_recorded', 'slice_id': key, 'path': receipt})


def matching_topics(prior, documented, reopen=()):
    return {topic: proof | documented[topic] for topic, proof in prior.get('topics', {}).items()
            if topic not in reopen and proof['specification_sha256'] == documented[topic]['specification_sha256']}


def continue_walkthrough(review, progress):
    if not review.blueprint.pending():
        review.manifest['next_action'] = ('Continue the guided walkthrough' if progress['remaining_topics']
                                          else 'Verify completeness and present the slice review recap')


def refresh_readiness(review, key, ready):
    prior = review.registry.get('walkthroughs', {}).get(key, {})
    if not prior.get('documented'):
        review.registry.get('walkthroughs', {}).pop(key, None)
        return
    documented = documents(review, key, ready)
    explained = matching_topics(prior, documented)
    progress = {field: item for field, item in prior.items() if field not in ('path', 'recap')}
    progress.update(target=ready['target'], readiness_path=ready['path'], documented=documented, topics=explained,
                    remaining_topics=[topic for topic in TOPIC_SECTIONS if topic not in explained])
    save_progress(review, key, progress)
    if key == review.registry.get('current_slice_id'):
        continue_walkthrough(review, progress)


def require_illustration(review, excerpt, paths):
    links = set(review.api.sibling_module('slice_guide').markdown_links(excerpt))
    shown_inline = (FLOW_EDGE.search(excerpt) or TABLE_DIVIDER.search(excerpt)
                    or ('┌' in excerpt and '┘' in excerpt and '\n' in excerpt))
    linked = all(path in links or str(review.api.artifact_file(review.directory, path)) in links for path in paths)
    review.require((shown_inline or paths) and linked,
                   'Record a shown illustration or displayed link to the saved illustration')


def record_presentation(review, key, value, ready):
    path, topics, position = (value.get(field) for field in ('presentation_path', 'topics', 'position'))
    review.require(review.blueprint.text(position) and isinstance(topics, dict)
                   and set(topics) <= TOPIC_SECTIONS.keys(),
                   'Supply the saved terminal presentation, covered topics and current review position')
    current = review.registry.get('walkthroughs', {}).get(key, {})
    prior = current if 'documented' in current else review.registry.get('previous_coverage', {}).get(key, {})
    text, checksum = read_excerpt(review, path)
    if current.get('readiness_path') != ready['path'] or 'documented' not in current:
        summary = ready['terminal']
        review.require(text.startswith(summary) and text[len(summary):].strip(),
                       'The terminal presentation must start with the current readiness summary and include the slice explanation')
        text = text[len(summary):]

    documented = documents(review, key, ready)
    entry = review.registry['selected'][key]
    excerpt_file = review.api.artifact_file(review.directory, path)
    review.require(not any(excerpt_file.samefile(review.api.artifact_file(review.directory, entry[field]))
                           for field in ('guide_path', 'contract_path')),
                   'Save what Codex actually said in a separate conversation excerpt')
    reopen = reopened_topics(review, value)
    explained = matching_topics(prior, documented, reopen)
    exchange_ids = value.get('conversation_exchange_ids', [])
    exchanges = discussion(review, key)
    review.require(review.blueprint.strings(exchange_ids, False) and set(exchange_ids) <= set(exchanges),
                   'Cite recorded discussion exchanges from this slice')
    review.require(text.strip() and (topics or exchange_ids),
                   'Save a presented explanation or a recorded discussion turn')
    illustrations = value.get('illustration_paths', [])
    review.require(review.blueprint.strings(illustrations, False) and (not illustrations or 'illustrations' in topics),
                   'Link illustration files when recording presentation of the illustration topic')
    illustration_hashes = (review.api.sibling_module('slice_evidence').evidence_hashes(review, illustrations)
                           if illustrations else {})
    review.require(all(review.blueprint.text(excerpt) and excerpt in text for excerpt in topics.values()),
                   'Provide a verbatim terminal excerpt for every topic presented in this turn')
    review.require(len(set(topics.values())) == len(topics), 'Cite distinct excerpts for the covered topics')
    for topic, excerpt in topics.items():
        explained[topic] = documented[topic] | {
            'excerpt': excerpt, 'presentation_path': path, 'presentation_sha256': checksum,
            'presented_revision': ready['target']['proposal_revision'], 'exchange_ids': exchange_ids,
        }
    if 'illustrations' in topics:
        require_illustration(review, topics['illustrations'], illustrations)
        explained['illustrations']['illustration_hashes'] = illustration_hashes
    progress = {'target': ready['target'], 'readiness_path': ready['path'], 'presentation_path': path,
                'presentation_sha256': checksum, 'documented': documented, 'topics': explained,
                'remaining_topics': [topic for topic in TOPIC_SECTIONS if topic not in explained],
                'position': position, 'discussion': exchanges}
    review.references.update(illustration_hashes)
    review.references[path] = checksum
    review.registry.get('previous_coverage', {}).pop(key, None)
    save_progress(review, key, progress)
    continue_walkthrough(review, progress)


def reopened_topics(review, value):
    topics = value.get('reopen_topics', [])
    review.require(review.blueprint.strings(topics, False) and set(topics) <= TOPIC_SECTIONS.keys(),
                   'Reopened topics must name distinct coverage topics')
    return topics


def reopen_coverage(review, key, topics):
    if not topics:
        return
    for field in ('walkthroughs', 'previous_coverage'):
        progress = review.registry.get(field, {}).get(key, {})
        if not progress.get('documented'):
            continue
        updated = {field: item for field, item in progress.items() if field not in ('path', 'recap')}
        updated['topics'] = {topic: proof for topic, proof in progress['topics'].items() if topic not in topics}
        updated['remaining_topics'] = [topic for topic in TOPIC_SECTIONS if topic not in updated['topics']]
        if field == 'walkthroughs':
            save_progress(review, key, updated)
        else:
            review.registry[field][key] = updated


def validate_recap(review, progress, sections, recap):
    review.require(isinstance(sections, dict) and set(sections) == RECAP_SECTIONS
                   and all(review.blueprint.text(excerpt) and excerpt in recap for excerpt in sections.values())
                   and len(set(sections.values())) == len(RECAP_SECTIONS),
                   'Supply distinct verbatim recap excerpts for expected result, review changes, material questions and approval scope')
    review.blueprint.check_placeholders(sections, 'slice recap')
    target = progress['target']
    scope = sections['approval_scope'].replace('`', '')
    guide = progress['documented']['purpose']['guide_path']
    revision = target['proposal_revision']
    slice_id = re.escape(target['slice_id'])
    revision_label = rf'(?<![\w-]){slice_id}\s+revision\s+{revision}\b'
    review.require(re.search(revision_label, scope) and guide in scope,
                   'The recap must identify the exact slice revision and guide in the approval scope')
    illustrations = progress['topics']['illustrations']
    require_illustration(review, illustrations['excerpt'], illustrations.get('illustration_hashes', {}))


def record_recap(review, key, value, ready):
    progress = review.registry.get('walkthroughs', {}).get(key)
    review.require(progress and progress.get('documented') and not progress['remaining_topics']
                   and progress['readiness_path'] == ready['path'] and progress['path'] == value.get('walkthrough_path'),
                   'Complete the current guided walkthrough and cite its exact coverage receipt')
    review.require(not review.blueprint.pending(), 'Cover all topics and resolve all material questions before the recap')
    review.require(progress['discussion'] == discussion(review, key),
                   'Update the saved walkthrough with the latest discussion before the recap')
    packet = review.selected_packet(key)
    details = {record['exchange_id'] for record in review.active_slice_details(key)}
    review.require(details <= set(packet['decision_ids']),
                   'Freeze a new revision incorporating the recorded clarification before the recap')
    path = value.get('recap_path')
    recap, checksum = read_excerpt(review, path)
    review.require(recap.strip() and path != progress['presentation_path'],
                   'Save the concise completeness recap displayed to the user as a separate excerpt')
    sections = value.get('recap_sections')
    validate_recap(review, progress, sections, recap)
    review.references[path] = checksum
    progress['recap'] = {'path': path, 'sha256': checksum, 'sections': sections}
    review.events.append({'type': 'slice_coverage_completed', 'slice_id': key,
                          'walkthrough_path': progress['path'], 'recap_path': path})
    review.manifest['next_action'] = f'Ask for approval of the exact reviewed revision of {key}'


def require_recap(review, key, exchange, ready):
    path = exchange.get('presentation_path')
    evidence_paths = exchange.get('evidence_paths', [])
    review.require(review.blueprint.text(path) and path in evidence_paths,
                   'Attach the saved terminal presentation in presentation_path and evidence_paths before approval')
    progress = review.registry.get('walkthroughs', {}).get(key)
    review.require(progress and progress.get('documented') and progress['readiness_path'] == ready['path']
                   and progress['presentation_path'] == path and not progress['remaining_topics'],
                   'Complete the current guided walkthrough before approval')
    recap = progress.get('recap')
    review.require(recap and not review.blueprint.pending() and progress['discussion'] == discussion(review, key),
                   'Record the completeness recap for the current discussion before approval')
    text, checksum = read_excerpt(review, recap['path'])
    validate_recap(review, progress, recap.get('sections'), text)
    question = exchange.get('question')
    review.require(checksum == recap['sha256'] and recap['path'] in evidence_paths
                   and review.blueprint.text(question) and text.strip() in question.replace('\r\n', '\n'),
                   'Include the recorded completeness recap in the approval prompt and evidence_paths')
    return {'path': path, 'sha256': progress['presentation_sha256'], 'readiness_path': ready['path'],
            'walkthrough_path': progress['path'], 'recap_path': recap['path'], 'recap_sha256': checksum}


def bound_evidence(review, key, presentation):
    progress = review.registry['walkthroughs'][key]
    hashes = review.registry['artifact_hashes']
    paths = {presentation['walkthrough_path'], presentation['recap_path'], presentation['path']}
    paths.update(proof['presentation_path'] for proof in progress['topics'].values())
    evidence = {path: hashes[path] for path in paths}
    for proof in progress['topics'].values():
        evidence.update(proof.get('illustration_hashes', {}))
    for exchange_id in progress['discussion']:
        evidence.update(review.blueprint.bound_hashes('conversation', exchange_id))
    return evidence
