"""Explicit, scoped consent for slice review, collection lock, and delivery."""

import re


def review_target(key, entry):
    return {'slice_id': key, 'proposal_revision': entry['revision'],
            'guide_sha256': entry['guide_sha256'], 'contract_sha256': entry['contract_sha256']}


def confirmation_phrases(action, target):
    if action == 'confirm_map':
        return {f"{verb} {target['map_id']}" for verb in ('confirm', 'confirm slice map', 'approve slice map')}
    if action == 'continue':
        item, = target['slices']
        return {f"continue to {item['slice_id']} revision {item['proposal_revision']}", 'continue to the next slice',
                'continue'}
    if action in {'approve', 'delegate'}:
        items = target['slices']
        if action == 'approve' and len(items) != 1:
            raise ValueError('Manual approval must identify exactly one slice')
        names = [(item['slice_id'], item['proposal_revision']) for item in items]
        scopes = {', '.join(f'{key} revision {revision}' for key, revision in names),
                  ', '.join(f'revision {revision} of {key}' for key, revision in names)}
        verbs = ('approve',) if action == 'approve' else (
            'delegate review of', 'delegate validation of', 'automatically validate')
        return {f'{verb} {scope}' for verb in verbs for scope in scopes}
    if action == 'lock':
        return {f"{verb} {target['package_id']}" for verb in ('approve locking', 'lock')}
    if action == 'start':
        return {f"{verb} {target['package_id']}" for verb in
                ('start implementation of', 'begin implementation of', 'start implementing', 'implement')}
    raise ValueError('Unknown slice consent action')


def affirmative(action, target, response):
    if not isinstance(response, str):
        return False
    text = ' '.join(response.casefold().split()).rstrip('.!').rstrip()
    text = re.sub(r'^yes,\s*', '', text)
    text = re.sub(r'^(?:please |go ahead and )', '', text)
    if action in {'approve', 'delegate', 'lock'}:
        text = re.sub(r'^(?:i |we )(?=approve |delegate )', '', text)
    elif action == 'confirm_map':
        text = re.sub(r'^(?:i |we )(?=confirm |approve )', '', text)
    text = text.removesuffix(' now')
    return text in {phrase.casefold() for phrase in confirmation_phrases(action, target)}


def displayed_values(target):
    values = []
    for key, value in target.items():
        if key == 'slices':
            for item in value:
                values.extend(displayed_values(item))
        elif isinstance(value, str) or type(value) is int:
            values.append(str(value))
    return values


SLICE_QUESTIONS = {
    'confirm_map': 'Do you confirm this slice map?',
    'approve': 'Do you approve this slice revision?',
    'continue': 'Are you ready to continue to the next slice?',
    'lock': 'May I lock this reviewed package?',
    'start': 'May I start implementation of this locked package?',
}
SCOPE_LABELS = {
    'map_id': 'Map', 'map_path': 'Map path', 'map_sha256': 'Map SHA-256',
    'guide_sha256': 'Guide SHA-256', 'contract_sha256': 'Contract SHA-256',
    'visual_sha256': 'Visual review SHA-256', 'package_id': 'Package',
    'collection_path': 'Collection path', 'collection_sha256': 'Collection SHA-256',
    'recap_sha256': 'Recap SHA-256', 'package_hash': 'Package SHA-256',
}


def render_prompt(initiative, action, target, *, recap='', guide_path=None):
    lines = [f'Initiative {initiative}']
    if 'slices' in target:
        for item in target['slices']:
            lines.append(f"Slice {item['slice_id']} revision {item['proposal_revision']}")
            lines.extend(f'{SCOPE_LABELS[key]} {value}' for key, value in item.items() if key in SCOPE_LABELS)
    else:
        lines.extend(f'{SCOPE_LABELS[key]} {value}' for key, value in target.items())
    if guide_path:
        lines.append(f'Guide path {guide_path}')
    if action == 'start':
        lines.append('Starting implementation authorizes edits to source code within this locked package.')
    blocks = [recap.strip(), '\n'.join(lines), SLICE_QUESTIONS[action]]
    return '\n\n'.join(block for block in blocks if block)
