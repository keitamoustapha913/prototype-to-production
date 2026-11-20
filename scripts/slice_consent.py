"""Explicit, scoped consent for slice review, collection lock, and delivery."""

import re


def review_target(key, entry):
    return {'slice_id': key, 'proposal_revision': entry['revision'],
            'guide_sha256': entry['guide_sha256'], 'contract_sha256': entry['contract_sha256']}


def confirmation_phrases(action, target):
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
