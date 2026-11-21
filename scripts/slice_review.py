"""Pre-dispatch blueprint slice review and versioned package authorization."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import posixpath
import re


SLICE_FIELDS = ('slice_id', 'scope', 'owner', 'status', 'blueprint_version', 'feature_ids', 'allowed_paths',
                'dependencies', 'functional_tests', 'test_plan', 'prohibited_deviations')
REGISTER = 'slice-review/register'
INVENTORY = 'slice-review/inventory.json'


def digest(data):
    return sha256(data).hexdigest()


def canonical_hash(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def package_guide(package_slice):
    """Read guide provenance from current or legacy package slice entries."""
    return (package_slice.get('delivery_guide_path') or package_slice['guide_path'],
            package_slice.get('delivery_guide_sha256') or package_slice['guide_sha256'])


def released_guide(release, package_slice):
    """Read the reviewed guide receipt, falling back to legacy immutable lock fields."""
    path, checksum = package_guide(package_slice)
    return (release.get('guide_path') or path,
            release.get('guide_sha256') or checksum)


class SliceReview:
    def __init__(self, blueprint):
        self.blueprint = blueprint
        self.api = blueprint.api
        self.directory = blueprint.directory
        self.manifest = blueprint.manifest
        self.require = blueprint.require
        self.registry = self.manifest.get('slice_review', {})
        self.files = {}
        self.binary = {}
        self.events = []
        self.references = {}
        self.source_guard = None

    @property
    def required(self):
        return self.api.requires_slice_review(self.manifest)

    @property
    def active_approval(self):
        return self.manifest.get('active_approval_id')

    @property
    def map_required(self):
        if self.registry.get('slice_map'):
            return True
        if 'slice_map_required' in self.manifest:
            return self.manifest['slice_map_required'] is True
        return not (self.registry.get('active_package') and self.registry.get('approval_id') == self.active_approval)

    def current_map(self):
        if self.registry.get('approval_id') == self.active_approval:
            return self.registry.get('slice_map')
        return None

    def require_map_confirmed(self):
        if self.map_required:
            current = self.current_map()
            self.require(current and current.get('consent_id') and not self.registry.get('planning_gaps'),
                         'Confirm the current slice map before beginning individual slice reviews or package approval')

    @property
    def adaptive_handoff(self):
        return self.manifest.get('adaptive_handoff_required', self.map_required)

    def require_review_turn(self):
        self.require(not self.registry.get('pending_continue'),
                     'Wait for the explicit instruction to continue to the next slice')

    def map_presentation(self, request=None):
        current = self.current_map()
        self.require(current and not self.registry.get('planning_gaps'),
                     'Resolve planning gaps and generate a complete slice map with slice-plan')
        target = current['target']
        status = 'Confirmed' if current.get('consent_id') else 'Awaiting explicit confirmation'
        data_path = self.api.artifact_file(self.directory, str(Path(target['map_path']).with_suffix('.json')))
        data = self.api.read_json(data_path)
        service = self.api.sibling_module('slice_map')
        terminal = service.terminal(data, current['documents'], target, status)
        if request is not None and not current.get('presentation'):
            path = str(Path(target['map_path']).with_suffix('.terminal.txt'))
            body = terminal.encode()
            self.binary[path] = body
            current['presentation'] = {'path': path, 'sha256': digest(body)}
            self.events.append({'type': 'slice_map_presented', 'target': target,
                                'presentation': current['presentation']})
            self.persist(request)
        return terminal

    def records(self):
        return [json.loads(line) for line in (self.directory / 'decisions.jsonl').read_text().splitlines()]

    def pending_question(self):
        question_id = self.registry.get('pending_question_id')
        if not question_id:
            return None
        path = self.manifest.get('conversation', {}).get(question_id, {}).get('path')
        if path in self.files:
            return self.files[path]
        try:
            return self.blueprint.load('conversation', question_id)
        except (self.api.StateError, OSError):
            # The normal integrity checks report the missing or changed record.
            return None

    def summary(self):
        if not self.required:
            return {'required': False, 'migration_available': True}
        current_map = self.current_map()
        current = self.registry.get('current_slice_id')
        waiting_for_map = self.map_required and not (current_map or {}).get('consent_id')
        if waiting_for_map:
            current = None
        pending = self.pending_question()
        next_action = self.manifest['next_action']
        if waiting_for_map and not current_map and self.registry.get('selected') and not pending:
            next_action = 'Plan the complete slice map for the active blueprint before reviewing individual slices'
        approval = self.manifest.get('approvals', {}).get(self.active_approval, {})
        snapshot = approval.get('workspace_snapshot')
        active_package = self.registry.get('active_package') if self.registry.get('approval_id') == self.active_approval else None
        return {
            'required': True,
            'phase': self.manifest['stage'],
            'inventory_path': INVENTORY if self.registry.get('selected') else None,
            'register_path': REGISTER + '.md' if (self.registry.get('selected') or self.registry.get('planning_gaps')) else None,
            'slice_map_required': self.map_required,
            'slice_map': current_map,
            'blueprint_approval_id': self.active_approval,
            'source_snapshot_sha256': canonical_hash(snapshot) if snapshot is not None else None,
            'needs_plan': (not self.registry.get('selected') or self.registry.get('approval_id') != self.active_approval
                           or (self.map_required and not current_map)),
            'current_slice_id': current,
            'current_revision': self.registry.get('selected', {}).get(current, {}).get('revision'),
            'pending_question_id': self.registry.get('pending_question_id'),
            'pending_question': pending,
            'completed_review_ids': [r['review_id'] for r in self.registry.get('reviews', {}).values()],
            'changed_dependents': self.registry.get('changed_dependents', []),
            'next_action': next_action,
            'collection_report': self.registry.get('collection'),
            'active_package': active_package,
            'continuing_releases': self.registry.get('continuing_releases', {}),
            'implementation_authorized': bool(active_package and
                self.registry['packages'][active_package].get('authorization_path')
                and not self.manifest.get('coverage_pending')),

            'packages': self.registry.get('packages', {}),
            'slices': self.registry.get('selected', {}),
            'reviews': self.registry.get('reviews', {}),
            'review_blockers': self.registry.get('review_blockers', {}),
            'handoff_readiness': self.registry.get('handoff_readiness', {}),
            'pending_continue': None if waiting_for_map else self.registry.get('pending_continue'),
            'rejected_alternatives': self.registry.get('rejected_alternatives', {}),
            'planning_gaps': self.registry.get('planning_gaps', {}),
        }

    def persist(self, request):
        summary = self.summary()
        if self.registry.get('selected'):
            self.sync_review_work(summary)
            self.files[INVENTORY] = {'approval_id': self.registry['approval_id'],
                                     'order': self.registry['order'], 'slices': self.registry['selected']}
        if summary['register_path']:
            self.files[REGISTER + '.json'] = summary
            self.binary[REGISTER + '.md'] = self.render_register(summary).encode()
        self.manifest['slice_review'] = self.registry
        hashes = self.registry.setdefault('artifact_hashes', {})
        for path, expected in self.references.items():
            self.require(path not in hashes or hashes[path] == expected, f'Frozen evidence differs: {path}')
        hashes.update(self.references)
        hashes.update({p: digest(self.api.json_content(v).encode()) for p, v in self.files.items()})
        hashes.update({p: digest(data) for p, data in self.binary.items()})
        self.events.append({'type': 'slice_review_transition', 'command': request['command'],
                            'journal_prefix_sha256': self.event_digest(self.records() + self.events),
                            'request_sha256': canonical_hash(request),
                            'registry_sha256': canonical_hash(self.registry)})
        self.api.save_workflow_state(self.directory, self.manifest, transition=(request, self.events, self.files),
                                     binary_artifacts=self.binary, review_source_guard=self.source_guard)

    def sync_review_work(self, summary):
        items = self.manifest.setdefault('work_items', {})
        task_id = self.manifest.get('active_work_id')
        if not task_id and self.manifest['stage'] != 'slice_review':
            return
        if not task_id:
            task_id = 'slice-review-cursor'
            suffix = 1
            while task_id in items:
                suffix += 1
                task_id = f'slice-review-cursor-{suffix}'
            self.manifest['active_work_id'] = task_id
        now = self.api.utc_now()
        prior = items.get(task_id, {})
        cursor = {k: summary[k] for k in ('current_slice_id', 'current_revision', 'pending_question_id',
                                         'completed_review_ids', 'changed_dependents', 'register_path',
                                         'inventory_path', 'active_package', 'slice_map', 'pending_continue', 'next_action')}
        initial = {'task_id': task_id, 'status': 'in_progress', 'summary': 'Review the complete slice inventory',
                   'phase': self.manifest['stage'], 'completed_steps': [],
                   'pending_steps': ['Review and authorize the slice collection'], 'blockers': [],
                   'code_paths': [], 'artifact_paths': [], 'code_hashes': {}, 'artifact_hashes': {},
                   'created_at': now, 'updated_at': now, 'revision': 0}
        item = initial | prior | {'slice_review': cursor}
        if summary['pending_question_id']:
            item.update(pending_question_id=summary['pending_question_id'], status='waiting_user',
                        next_action=summary['next_action'])
        elif summary['pending_continue']:
            item.update(pending_question_id=None, status='waiting_user', next_action=summary['next_action'])
        elif item['status'] == 'waiting_user':
            item.update(pending_question_id=None, status='in_progress',
                        next_action=summary['next_action'])
        review_step = 'Review and authorize the slice collection'
        release_step = 'Release reviewed assignments in dependency order'
        delivery_step = 'Implement and verify released assignments'
        if task_id.startswith('slice-review-cursor') and summary['implementation_authorized']:
            complete = [review_step]
            package = summary['packages'][summary['active_package']]
            upcoming = release_step
            if set(package['released']) == set(summary['slices']):
                complete.append(release_step)
                upcoming = delivery_step
            item['completed_steps'] = list(dict.fromkeys(item['completed_steps'] + complete))
            item['pending_steps'] = [step for step in item['pending_steps'] if step not in complete]
            if upcoming not in item['pending_steps']:
                item['pending_steps'].append(upcoming)
        if item['status'] == 'blocked' and not summary['pending_question_id']:
            self.manifest['next_action'] = item['next_action']
            summary['next_action'] = item['next_action']
            cursor['next_action'] = item['next_action']
        item.update(phase=self.manifest['stage'], next_action=summary['next_action'],
                    revision=item['revision'] + 1, updated_at=now)
        items[task_id] = item
        self.events.append({'type': 'work_cursor_updated', 'task': item})

    def render_register(self, summary):
        rows = ['# Slice review register', '', f"Next action — {summary['next_action']}", '']
        if current := summary['slice_map']:
            target = current['target']
            status = 'confirmed' if current.get('consent_id') else 'awaiting confirmation'
            rows.extend([f"[Implementation slice map](../{target['map_path']}) · {status}", ''])
        question = summary['pending_question']
        if question:
            rows.extend([f"Current review question ({question['exchange_id']})", '', question['question'], ''])
        if ready := summary['handoff_readiness'].get(summary['current_slice_id']):
            rows.extend(['## Handoff readiness', '', '~~~text', ready['terminal'].rstrip(), '~~~', ''])
        rows.extend(['| Slice | Purpose | Features | Revision | Method | Status | Blockers | Dependencies | Next action |',
                     '| --- | --- | --- | --- | --- | --- | --- | --- | --- |'])
        for key in self.registry.get('order', []):
            entry = summary['slices'][key]
            review = summary['reviews'].get(key, {})
            question = summary['pending_question_id'] if key == summary['current_slice_id'] else None
            blocker = summary['review_blockers'].get(key)
            pending_status = 'Under review' if key == summary['current_slice_id'] else 'Pending review'
            fields = [f"[{key}](../{entry['guide_path']})", entry['title'], ', '.join(entry['feature_ids']),
                      str(entry['revision']), review.get('review_mode', 'Manual'),
                      'Blocked' if blocker else review.get('result', pending_status),
                      question or (blocker['reason'] if blocker else '—'), ', '.join(entry['dependencies']) or '—',
                      'Resolve the recorded concern' if blocker else
                      'Await collection validation' if review else 'Discuss and review the exact revision']
            rows.append('| ' + ' | '.join(field.replace('|', '\\|').replace('\n', ' ') for field in fields) + ' |')
        if summary['planning_gaps']:
            rows.extend(['', '## Missing coverage', ''])
            for feature, check in summary['planning_gaps']['feature_checks']:
                rows.append(f'- Feature `{feature}` needs check `{check}`.')
            for baseline in summary['planning_gaps']['baseline_ids']:
                rows.append(f'- Baseline behavior or requirement `{baseline}` needs a slice.')
            for category, gap in summary['planning_gaps'].get('cross_slice', {}).items():
                rows.append(f'- {category} — {gap}')
        if summary['rejected_alternatives']:
            rows.extend(['', '## Rejected alternatives', ''])
            for key, item in summary['rejected_alternatives'].items():
                rows.append(f"- {item['slice_id']} · [{key}](../{item['path']})")
        return '\n'.join(rows) + '\n'

    def validate_exchange_scope(self, packet, exchange):
        feature_ids = set(exchange.get('feature_ids', []))
        requirement_ids = set(exchange.get('requirement_ids', []))
        requirements = {item['requirement_id'] for item in packet['baseline_contract'].get('requirements', [])}
        self.require(feature_ids <= set(packet['feature_ids'] + packet['baseline_ids']),
                     'Review exchange names an unknown feature or exceeds this slice scope')
        self.require(requirement_ids <= requirements.intersection(packet['baseline_ids']),
                     'Review exchange names an unknown requirement or exceeds this slice scope')

    def record_exchange(self, request, value):
        self.check_history()
        value = dict(value)
        self.require(self.registry.get('selected') and self.registry.get('approval_id') == self.active_approval,
                     'Plan the complete slice inventory before recording slice-review exchanges')
        for field in ('feature_ids', 'requirement_ids', 'illustration_paths', 'evidence_paths'):
            self.require(self.blueprint.strings(value.get(field, []), False), f'{field} must contain distinct references')
        consent_action = value.get('consent_action') if value.get('record_type') == 'slice_consent' else None
        map_consent = consent_action == 'confirm_map'
        if consent_action == 'continue' and (exchange_id := value.get('exchange_id')) in self.registry.get('continuations', []):
            saved = self.blueprint.load('conversation', exchange_id)
            self.require(all(saved.get(field) == item for field, item in value.items()),
                         'The recorded continuation is immutable')
            return
        if map_consent and (exchange_id := value.get('exchange_id')) and (
                self.current_map() or {}).get('consent_id') == exchange_id:
            saved = self.blueprint.load('conversation', exchange_id)
            self.require(all(saved.get(field) == item for field, item in value.items()),
                         'The recorded slice map confirmation is immutable')
            return
        # Finish a question saved before the map checkpoint so it cannot deadlock replanning.
        legacy_answer = (self.map_required and not self.current_map()
                         and value.get('responds_to') == self.registry.get('pending_question_id')
                         and bool(value.get('responds_to')) and value.get('status') in ('decided', 'rejected'))
        if not (map_consent or legacy_answer):
            self.require_map_confirmed()
        if value.get('record_type') == 'slice_consent':
            self.validate_consent(value)
            if map_consent:
                value['map_presentation'] = self.current_map()['presentation']
        else:
            linked_id = value.get('responds_to')
            earlier = self.blueprint.load('conversation', linked_id) if linked_id else {}
            key = value.get('slice_id', earlier.get('slice_id', self.registry.get('current_slice_id')))
            self.require(self.blueprint.identifier(key) and key in self.registry['selected'],
                         'Review exchange must belong to a selected slice')
            value['slice_id'] = key
            self.validate_exchange_scope(self.selected_packet(key), value)
            if not linked_id and self.adaptive_handoff and key not in self.registry['reviews']:
                self.require_review_turn()
                self.require(key == self.registry['current_slice_id'], 'Discuss the current slice before opening another')
                self.require_readiness_or_blocker_question(key, value)
            if value.get('status') in ('open', 'proposed'):
                self.require(key == self.registry['current_slice_id'] and not self.registry.get('pending_question_id'),
                             'Finish the exact pending question before opening another')
                revision = self.registry['selected'][key]['revision']
                self.require(value.get('proposal_revision', revision) == revision,
                             'Question must target the current proposal revision')
                value['proposal_revision'] = revision
        paths = value.get('illustration_paths', []) + value.get('evidence_paths', [])
        if paths:
            self.api.sibling_module('slice_evidence').evidence_hashes(self, paths)
        self.blueprint.conversation_record(value, transition=(self.files, self.events))
        if map_consent:
            self.registry['slice_map']['consent_id'] = value['exchange_id']
            self.events.append({'type': 'slice_map_confirmed', 'exchange_id': value['exchange_id'],
                                'target': value['target']})
            self.advance()
        elif consent_action == 'continue':
            self.registry.pop('pending_continue')
            self.registry.setdefault('continuations', []).append(value['exchange_id'])
            self.events.append({'type': 'slice_review_continued', 'exchange_id': value['exchange_id'],
                                'target': value['target']})
            self.advance(value['target']['slices'][0]['slice_id'])
        elif value.get('status') in ('open', 'proposed'):
            self.registry['pending_question_id'] = value['exchange_id']
            self.registry['collection'] = None
            self.manifest['next_action'] = value['question']
        elif value.get('status') == 'rejected':
            self.registry.setdefault('rejected_alternatives', {})[value['exchange_id']] = {
                'slice_id': value['slice_id'], 'question': value['question'], 'response': value['response'],
                'path': self.manifest['conversation'][value['exchange_id']]['path'],
            }
        elif value.get('record_type') == 'slice_detail' and value.get('supersedes'):
            if value['slice_id'] in self.registry['reviews']:
                self.invalidate({value['slice_id']})
                self.advance(value['slice_id'])
        self.persist(request)

    def visual_consent_target(self, key, entry, *, verify_illustrations=False):
        target = self.api.sibling_module('slice_consent').review_target(key, entry)
        if self.api.has_living_review(self.directory):
            viewer = self.api.sibling_module('living_review')
            if verify_illustrations:
                visual = viewer.checkpoint(self.directory, self.manifest, key)
            else:
                with viewer.History(self.directory) as history:
                    state = history.sync(viewer.Documents(self.directory))
                visual = viewer.review_bindings(state['sections'], key, state['removals'])
            if visual:
                target['visual_sha256'] = viewer.digest(viewer.encode(visual))
        return target

    def validate_consent(self, exchange):
        service = self.api.sibling_module('slice_consent')
        action, target = exchange.get('consent_action'), exchange.get('target')
        self.require(isinstance(target, dict) and action in ('confirm_map', 'approve', 'delegate', 'continue', 'lock', 'start'),
                     'Record an explicit slice consent action and exact scope')
        if action == 'confirm_map':
            current = self.current_map()
            self.require(self.map_required and current and not self.registry.get('planning_gaps'),
                         'Generate a complete slice map before requesting confirmation')
            self.require(current.get('consent_id') in (None, exchange.get('exchange_id')),
                         'The current slice map is already confirmed')
            presentation = current.get('presentation')
            self.require(presentation, 'Run slice-map to save the terminal presentation before confirming the breakdown')
            approval = self.approval()
            self.guard_source(approval, self.authorized_paths())
            expected = current['target']
            self.source_guard['artifact_hashes'] = approval['artifact_hashes'] | {
                expected['map_path']: expected['map_sha256'], presentation['path']: presentation['sha256']}
        elif action in ('approve', 'delegate'):
            self.require_review_turn()
            items = target.get('slices')
            self.require(isinstance(items, list) and bool(items) and all(isinstance(item, dict) for item in items),
                         'Consent must identify exact slice revisions')
            keys = [self.selected_id(item) for item in items]
            self.require(len(keys) == len(set(keys)), 'Consent cannot name the same slice twice')
            if self.adaptive_handoff and action == 'approve':
                self.require(len(keys) == 1 and keys[0] == self.registry['current_slice_id'],
                             'Manual approval must identify the current slice')
                readiness = self.require_readiness(keys[0])
                exchange['slice_presentation'] = self.check_slice_presentation(exchange, readiness)
            expected = {'slices': [self.visual_consent_target(key, self.registry['selected'][key]) for key in keys]}
            if action == 'delegate':
                self.require(self.blueprint.text(target.get('constraints')), 'Delegation must record its exact constraints')
                expected['constraints'] = target['constraints']
        elif action == 'continue':
            expected = self.registry.get('pending_continue')
            self.require(expected, 'There is no slice review waiting for continuation')
        elif action == 'lock':
            preview = self.package_preview()
            expected = {field: preview[field] for field in
                        ('package_id', 'collection_path', 'collection_sha256', 'recap_sha256')}
        else:
            key, entry, _ = self.package(target)
            expected = {'package_id': key, 'package_hash': entry['package_hash']}
        self.require(target == expected and self.blueprint.text(exchange.get('question'))
                     and all(value in exchange['question'] for value in service.displayed_values(expected)),
                     'The recorded prompt must display the exact scope and hashes before consent')
        self.require(service.affirmative(action, target, exchange.get('response')),
                     'Record the separate explicit user instruction naming the exact approved scope')

    def check_consent(self, exchange_id, action, target, response):
        self.require(self.blueprint.identifier(exchange_id),
                     'Record a separate explicit user instruction and supply its consent_id')
        exchange = self.blueprint.load('conversation', exchange_id)
        self.require(exchange.get('record_type') == 'slice_consent' and exchange.get('consent_action') == action
                     and exchange.get('response') == response and exchange.get('status') == 'decided',
                     'The receipt must match the separate explicit user instruction')
        actual = exchange['target']
        if action == 'delegate':
            self.require(target['slices'][0] in actual['slices'] and target['constraints'] == actual['constraints'],
                         'Delegation consent does not cover this exact slice revision and constraints')
        else:
            self.require(actual == target, 'Consent must match the exact scope and hashes shown to the user')
        service = self.api.sibling_module('slice_consent')
        self.require(service.affirmative(action, actual, response)
                     and all(value in exchange['question'] for value in service.displayed_values(actual)),
                     'Consent must be an affirmative instruction for the displayed scope and hashes')

    def consume_consent(self, value, action, target, *, slice_id=None):
        exchange_id = value.get('consent_id')
        self.check_consent(exchange_id, action, target, value.get('user_response'))
        used = self.registry.setdefault('consents_used', {})
        use = slice_id or action
        self.require(use not in used.get(exchange_id, []), 'This user consent has already been used')
        used.setdefault(exchange_id, []).append(use)
        self.references.update(self.blueprint.bound_hashes('conversation', exchange_id))
        return exchange_id

    def active_slice_details(self, key):
        records = self.blueprint.conversations()
        stale = self.blueprint.superseded_decisions(records)
        approval = self.manifest.get('approvals', {}).get(self.active_approval, {})
        approved_decisions = set(approval.get('blueprint_decision_ids', []))
        return [record for record in records if record.get('record_type') == 'slice_detail'
                and record.get('slice_id') == key and record['exchange_id'] not in stale
                and set(record.get('clarifies_decision_ids', [])) <= approved_decisions
                and not (set(record.get('clarifies_decision_ids', [])) & stale)]

    def collection_request_key(self, value):
        validation_id = value.get('validation_id')
        self.require(validation_id is None or self.blueprint.identifier(validation_id),
                     'Collection validation_id must be a stable ID')
        scope = {field: self.registry.get(field) for field in
                 ('approval_id', 'selected', 'reviews', 'pending_question_id', 'review_blockers')}
        if current := self.current_map():
            scope['slice_map'] = current
        if self.api.has_living_review(self.directory):
            viewer = self.api.sibling_module('living_review')
            with viewer.History(self.directory) as history:
                state = history.sync(viewer.Documents(self.directory))
            scope['visual_review'] = state['visual_review']
            scope['removed_sections'] = {key: removal['stamp'] for key, removal in state['removals'].items()
                                         if removal['active']}
        return canonical_hash({'input': value, 'scope': scope, 'active_approval_id': self.active_approval})

    def run(self, request):
        command, value = request['command'], request['input']
        self.check_history()
        if command == 'slice-map':
            return self.map_presentation(request)
        if command == 'slice-review-migrate' and self.registry.get('migration_request_sha256') == canonical_hash(value):
            return self.summary()
        if command == 'slice-collection-validate':
            if self.registry.get('selected') and self.registry.get('approval_id') == self.active_approval:
                self.require_map_confirmed()
                self.require_review_turn()
            request_key = self.collection_request_key(value)
            saved = self.registry.get('collection_requests', {}).get(request_key)
            if saved:
                report = self.api.read_json(self.api.artifact_file(self.directory, saved))
                if report['result'] == 'PASS':
                    approval = self.approval()
                    self.source(approval, self.authorized_paths())
                    self.check_selected_runtime()
                return report
        previous = [e for e in self.records() if e.get('type') == 'slice_review_transition']
        if previous and previous[-1]['request_sha256'] == canonical_hash(request) and command not in {
                'slice-guide-validate', 'slice-release', 'slice-collection-validate'}:
            if command != 'slice-review-migrate':
                approval = self.approval(allow_questions=value.get('action') in ('question', 'block', 'require-independent'))
                self.source(approval, self.authorized_paths())
            if command == 'slice-plan' and self.registry.get('planning_gaps'):
                return self.planning_blocker()
            return self.summary()
        if command == 'slice-review-migrate':
            return self.migrate(request)
        self.require(self.required, 'Legacy blueprint initiative; explicit slice-review-migrate is available')
        if command == 'slice-register':
            return self.summary()
        if command == 'slice-plan':
            report = self.plan(value)
            if report:
                self.persist(request)
                return report
        else:
            self.require(self.registry.get('selected') and self.registry.get('approval_id') == self.active_approval,
                         'Plan the complete slice inventory for the current approved blueprint first')
            if command not in {'slice-draft', 'slice-guide-validate'}:
                self.require_map_confirmed()
            if command == 'slice-guide-validate':
                key = self.selected_id(value)
                packet = self.validate_frozen(key)
                result = {'result': 'PASS', 'slice': self.registry['selected'][key]}
                if 'assessment' in value:
                    self.require_map_confirmed()
                    self.require_review_turn()
                    depth, reasons = self.api.sibling_module('slice_readiness').depth(self, packet, value['assessment'])
                    result.update(depth=depth, reasons=reasons)
                return result
            if command == 'slice-package-preview':
                return self.package_preview()
            if command == 'slice-draft':
                self.draft(value)
            elif command == 'slice-review-record':
                self.review(value)
            elif command == 'slice-collection-validate':
                self.require_review_turn()
                report = self.collection(value)
                requests = self.registry.setdefault('collection_requests', {})
                requests[request_key] = report['path']
                # A material finding may invalidate reviews as part of this
                # transition. Preserve the exact retry under its resulting scope.
                requests[self.collection_request_key(value)] = report['path']
                self.persist(request)
                return report
            elif command == 'slice-package-lock':
                self.lock(value)
            elif command == 'implementation-authorize':
                self.authorize(value)
            elif command == 'slice-release' and self.release(value) is False:
                return self.summary()
        self.persist(request)
        return self.summary()

    def approval(self, *, allow_questions=False):
        self.require(self.active_approval, 'An exact blueprint approval is required')
        errors = self.blueprint.check_approval(allow_questions=allow_questions)
        errors += self.blueprint.check_records()
        errors += self.api.check_coverage_records(self.directory, self.manifest)
        self.require(not errors, '; '.join(errors))
        self.require(not self.manifest.get('coverage_pending')
                     and not any(r['status'] == 'open' for r in self.manifest.get('missing_reports', {}).values()),
                     'Open missing-feature investigation requires reconciliation')
        return self.manifest['approvals'][self.active_approval]

    def source(self, approval, allowed=(), *, suspension=None, snapshot=None, writable_paths=None):
        before = approval.get('workspace_snapshot')
        self.require(isinstance(before, dict), 'Blueprint approval requires a workspace snapshot')
        live = self.blueprint.workspace_snapshot() if snapshot is None else snapshot
        historical_paths = set()
        checkpoint_drift = []
        latest_by_path = {}
        assignments = self.manifest.get('slices', {})
        for event in self.records():
            if event.get('type') != 'slice_cancelled' or not event.get('source_checkpoint'):
                continue
            checkpoint = event['source_checkpoint']
            slice_id = event.get('slice_id')
            assignment = assignments.get(slice_id, {})
            self.require(assignment.get('cancelled_source_checkpoint') == checkpoint,
                         f'{slice_id}: cancelled source checkpoint differs from its journal record')
            if checkpoint.get('approval_id') != self.active_approval:
                continue
            scope = checkpoint.get('allowed_paths', [])
            expected_snapshot = checkpoint.get('snapshot', {})
            scoped_paths = {path for path in (before.keys() | expected_snapshot.keys() | live.keys())
                            if any(self.api.ownership_contains(owner, path) for owner in scope)}
            for path in scoped_paths:
                latest_by_path[path] = expected_snapshot.get(path)
        for path, expected in latest_by_path.items():
            # A replacement release can write only paths it currently owns;
            # the rest of the cancelled scope stays frozen at cancellation.
            if any(self.api.ownership_contains(owner, path) for owner in allowed):
                continue
            if live.get(path) != expected:
                checkpoint_drift.append(path)
            else:
                historical_paths.add(path)
        self.require(not checkpoint_drift,
                     'Cancelled slice source drift after cancellation: ' + ', '.join(sorted(set(checkpoint_drift))))
        changes = sorted(p for p in before.keys() | live.keys() if before.get(p) != live.get(p)
                         and p not in historical_paths
                         and not any(self.api.ownership_contains(path, p) for path in allowed))
        self.require(not changes, 'Source changed since blueprint approval; reconcile: ' + ', '.join(changes))
        paused = suspension or self.registry.get('suspension')
        if paused and paused['approval_id'] == self.active_approval:
            writable = self.released_paths() if writable_paths is None else writable_paths
            errors = self.api.sibling_module('slice_integrity').validate_suspension_checkpoint(
                paused, live, can_change=lambda path: any(
                    self.api.ownership_contains(owner, path) for owner in writable))
            self.require(not errors, '; '.join(errors))
        return canonical_hash(before)

    def guard_source(self, approval, allowed=()):
        snapshot_hash = self.source(approval, allowed)
        self.source_guard = {'allowed_paths': allowed, 'approval_id': self.active_approval,
                             'artifact_hashes': approval['artifact_hashes'],
                             'suspension': self.registry.get('suspension'),
                             'writable_paths': self.released_paths()}
        return snapshot_hash

    def check_recovery(self, guard):
        approval = self.manifest['approvals'][guard['approval_id']]
        self.blueprint.check_hashes(guard['artifact_hashes'])
        self.source(approval, guard['allowed_paths'], suspension=guard.get('suspension'),
                    writable_paths=guard.get('writable_paths'))
        if guard.get('validate_runtime'):
            self.check_selected_runtime()
        for key in guard.get('dependencies', []):
            assignment = self.manifest['slices'][key]
            self.require(not self.blueprint.check_saved_verification(key, assignment),
                         f'{key}: accepted dependency verification became stale during recovery')

    def selected_id(self, value):
        key = value.get('slice_id')
        self.require(self.blueprint.identifier(key) and key in self.registry.get('selected', {}),
                     f'Unknown slice proposal: {key}')
        return key

    def selected_packet(self, key):
        entry = self.registry['selected'][key]
        return self.api.read_json(self.api.artifact_file(self.directory, entry['contract_path']))

    def plan(self, value):
        approval = self.approval()
        adopt_map = not self.map_required and 'slice_map_required' not in self.manifest
        if adopt_map:
            self.manifest['slice_map_required'] = True
        self.require(self.registry.get('migration_approval_id') != self.active_approval,
                     'A new blueprint approval must reconcile legacy assignments before planning reviewed delivery')
        previous_registry = deepcopy(self.registry)
        previous = self.registry.get('selected', {})
        same_approval = self.registry.get('approval_id') == self.active_approval
        self.require(not previous or not same_approval or self.blueprint.text(value.get('amendment')),
                     'An existing inventory needs an explicit documented amendment')
        items = value.get('slices')
        self.require(isinstance(items, list) and all(isinstance(item, dict) for item in items),
                     'Complete dependency-ordered slices array required')
        selected, packets = {}, {}
        live_deliveries = self.current_deliveries()
        source_hash = self.guard_source(approval, self.authorized_paths())
        for item in items:
            key = item.get('slice_id')
            self.require(self.blueprint.identifier(key) and key not in selected, 'Unique stable slice IDs required')
            if item == {'slice_id': key, 'retain': True}:
                self.require(same_approval and key in previous, f'Unknown retained proposal: {key}')
                selected[key], packets[key] = previous[key], self.selected_packet(key)
                self.require(set(packets[key]['dependencies']) <= packets.keys() - {key},
                             f'{key}: retained dependencies must appear before this slice')
                continue
            self.require(key not in self.manifest.get('slices', {}),
                         f'{key}: use a new slice ID for a previously released assignment')
            revision = self.next_revision(key)
            entry, packet = self.freeze_proposal(item, approval, packets, revision, source_hash)
            selected[key], packets[key] = entry, packet
        retiring = set(live_deliveries) - set(selected)
        if retiring:
            self.require(self.blueprint.text(value.get('user_response')) and self.blueprint.text(value.get('amendment')),
                         'Replacing released assignments requires the actual user amendment instruction')
            for key in retiring:
                self.require(live_deliveries[key]['status'] == 'cancelled',
                             f'{key}: record cancellation of the affected assignment before replacing it')
        declared_predecessors = {predecessor for packet in packets.values()
                                 for predecessor in packet.get('replaces_slice_ids', [])}
        predecessor_candidates = {
            key: assignment for key, assignment in self.manifest.get('slices', {}).items()
            if (key in retiring and assignment.get('status') == 'cancelled')
            or assignment.get('superseded_by') == self.active_approval
        }
        for predecessor, assignment in predecessor_candidates.items():
            contract_path = assignment.get('contract_file')
            contract_hash = assignment.get('contract_sha256')
            if contract_path and contract_hash:
                self.blueprint.check_hashes({contract_path: contract_hash})
                prior_contract = self.api.read_json(self.api.artifact_file(self.directory, contract_path))
                prior_features = set(prior_contract.get('feature_ids', []))
            else:
                prior_features = set(assignment.get('feature_ids', []))
            if prior_features and any(prior_features.intersection(packet['feature_ids']) for packet in packets.values()):
                self.require(predecessor in declared_predecessors,
                             f'{predecessor}: declare the predecessor in replaces_slice_ids for its replacement')
        for successor, packet in packets.items():
            for predecessor in packet.get('replaces_slice_ids', []):
                prior_assignment = self.manifest.get('slices', {}).get(predecessor, {})
                eligible = (predecessor in retiring and live_deliveries[predecessor]['status'] == 'cancelled'
                            or prior_assignment.get('superseded_by') == self.active_approval)
                self.require(eligible,
                             f'{successor}: replacement predecessor must be a cancelled or newly superseded assignment')
        gaps = self.coverage_gaps(packets)
        if self.map_required:
            gaps['cross_slice'] = self.map_conflicts(packets)
        if any(gaps.values()):
            if adopt_map:
                self.manifest.pop('slice_map_required')
            self.registry = previous_registry
            self.registry['planning_gaps'] = gaps
            self.files.clear()
            self.binary.clear()
            self.references.clear()
            return self.planning_blocker()
        self.registry.pop('planning_gaps', None)
        if self.map_required:
            self.manifest['slice_map_required'] = True
        changed = {key for key in previous.keys() | selected.keys() if previous.get(key) != selected.get(key)}
        changed.update(self.changed_readiness_providers(previous, selected, changed))
        self.registry.update(approval_id=self.active_approval, selected=selected, order=list(selected))
        self.invalidate(changed, reset=not same_approval)
        self.update_map(packets)
        self.manifest.pop('slice_review_needs_plan', None)
        self.advance()
        if previous:
            self.events.append({'type': 'slice_inventory_amended', 'reason': value.get('amendment'),
                                'user_response': value.get('user_response'), 'changed': sorted(changed)})

    def next_revision(self, key):
        return self.registry.get('latest_revisions', {}).get(key, 0) + 1

    def freeze_proposal(self, item, approval, dependencies, revision, source_hash):
        key = item['slice_id']
        self.require(type(item.get('proposal_revision')) is int and item['proposal_revision'] == revision,
                     f'{key}: next proposal revision must be {revision}')
        self.require(self.blueprint.text(item.get('title')) and '\n' not in item['title'], f'{key}: title required')
        missing = set(SLICE_FIELDS) - item.keys()
        self.require(not missing, f'{key}: missing contract fields {sorted(missing)}')
        packet = self.blueprint.slice_packet({field: item[field] for field in SLICE_FIELDS}, dependencies,
                                             allow_partial_checks=True)
        root = self.directory.parents[2].resolve()
        for path in packet['allowed_paths']:
            parts, _ = self.api.owned_path(path)
            target = root.joinpath(*parts)
            self.require(parts[0] not in {'.git', '.artifacts'} and not target.is_relative_to(self.directory)
                         and target.resolve() == target, f'{key}: forbidden or redirected implementation path: {path}')
        spec = self.blueprint.load('blueprints', approval['approved_version'])
        coverage = self.blueprint.load('blueprint_coverage', approval['approved_version'])
        selected = packet['feature_contracts']
        baseline_ids = sorted({m['baseline_id'] for m in coverage['mappings']
                               if m['feature_id'] in packet['feature_ids']})
        surfaces = item.get('surfaces')
        known_surfaces = self.api.sibling_module('slice_guide').ANNEX_FIELDS
        self.require(self.blueprint.strings(surfaces) and set(surfaces) <= known_surfaces.keys(),
                     f'{key}: recognized surface annexes required')
        required_surfaces = set()
        for feature in selected:
            surface = feature.get('surface', spec['surface'])
            required_surfaces.update(('ui', 'backend') if surface == 'fullstack' else (surface,))
            required_surfaces.update(feature.get('slice_surfaces', []))
        self.require(required_surfaces == set(surfaces),
                     f'{key}: missing surface annexes {sorted(required_surfaces - set(surfaces))}; '
                     f'unapproved surfaces {sorted(set(surfaces) - required_surfaces)}')
        paths = item.get('asset_paths', [])
        self.require(self.blueprint.strings(paths, False), f'{key}: asset_paths must be distinct initiative paths')
        base = f'slice-proposals/{key}/revisions/{revision}'
        input_guide = self.api.artifact_file(self.directory, item.get('guide_path'))
        guide_path = f'{base}/implementation-guide.md'
        contract_path = f'{base}/contract.json'
        self.require(input_guide.relative_to(self.directory).parts[0] == 'inputs',
                     f'{key}: stage guide inputs under initiative inputs/')
        assets = {}
        for raw in paths:
            path = self.api.artifact_file(self.directory, raw)
            self.require(path.is_relative_to(input_guide.parent), f'{key}: assets must be beside the draft guide: {raw}')
            relative = path.relative_to(input_guide.parent)
            self.require(relative.parts[0] in {'diagrams', 'examples'}, f'{key}: use diagrams/ or examples/ for {raw}')
            assets[f'{base}/{relative.as_posix()}'] = path.read_bytes()
        guide = input_guide.read_bytes()
        shared = item.get('shared_contracts', {})
        self.require(isinstance(shared, dict) and all(self.blueprint.text(name) and isinstance(contract, dict)
                     and contract.get('role') in ('provides', 'consumes') and contract.get('definition')
                     and set(contract) == {'role', 'definition'} for name, contract in shared.items()),
                     f'{key}: shared contracts need a role and concrete definition')
        dependency_contracts = item.get('dependency_contracts')
        if dependency_contracts is None and not packet['dependencies']:
            dependency_contracts = []
        self.require(isinstance(dependency_contracts, list),
                     f'{key}: declare how every dependency is consumed')
        replaces = item.get('replaces_slice_ids', [])
        self.require(isinstance(replaces, list) and (not replaces or self.blueprint.strings(replaces))
                     and key not in replaces,
                     f'{key}: replaces_slice_ids must be distinct predecessor slice IDs')
        reference_paths = item.get('artifact_paths', [])
        self.require(self.blueprint.strings(reference_paths, False), f'{key}: artifact_paths must be distinct references')
        references = (self.api.sibling_module('slice_evidence').evidence_hashes(self, reference_paths)
                      if reference_paths else {})
        details = self.active_slice_details(key)
        for decision in packet['decision_records'] + details:
            references.update(self.blueprint.bound_hashes('conversation', decision['exchange_id']))
        self.references.update(references)
        packet['decision_records'].extend(details)
        for feature in selected:
            if feature.get('visual_contract'):
                scope = set(feature['baseline_ids']) | {feature['feature_id']}
                protections = self.blueprint.protected_visual_decisions(packet['decision_records'], scope)
                if protections:
                    feature['protected_visual_decisions'] = protections
        anchors = list({
            (anchor['path'], anchor['contains']): anchor
            for feature in packet['baseline_contract'].get('features', [])
            if feature['feature_id'] in baseline_ids
            for anchor in feature.get('source_anchors', [])
        }.values())
        packet.update(proposal_revision=revision, title=item['title'], review_status='pending',
                      evidence_schema_version=3, guide_format_version=3,
                      blueprint_approval_id=self.active_approval, plan_version=spec['plan_version'],
                      baseline_ids=baseline_ids, surfaces=surfaces, guide_path=guide_path, contract_path=contract_path,
                      approved_source_anchors=anchors,
                      decision_ids=sorted(d['exchange_id'] for d in packet['decision_records']),
                      acceptance_check_ids=packet['acceptance_checks'], source_snapshot_sha256=source_hash,
                      guide_sha256=digest(guide), asset_hashes={p: digest(data) for p, data in assets.items()},
                      artifact_hashes=references, traceability=item.get('traceability'),
                      artifact_paths=reference_paths, shared_contracts=shared,
                      dependency_contracts=dependency_contracts,
                      replaces_slice_ids=replaces,
                      unresolved_question_ids=item.get('unresolved_question_ids', []))
        if self.map_required:
            reason = item.get('boundary_reason')
            self.require(self.blueprint.text(reason) and '\n' not in reason and '\r' not in reason,
                         f'{key}: give the proposed slice a one-line boundary_reason for map review')
            self.blueprint.check_placeholders(reason, f'slice map {key}')
            packet['boundary_reason'] = reason
        self.require(self.blueprint.strings(packet['unresolved_question_ids'], False),
                     f'{key}: unresolved_question_ids must be distinct exchange IDs')
        self.require(set(packet['unresolved_question_ids']) <= set(self.manifest.get('conversation', {})),
                     f'{key}: unresolved question refers to an unknown A+ exchange')
        self.validate_guide(packet, guide.decode(), assets)
        entry = {field: packet[field] for field in ('title', 'feature_ids', 'dependencies', 'allowed_paths',
                                                   'guide_path', 'guide_sha256')}
        entry.update(revision=revision, contract_path=contract_path,
                     contract_sha256=digest(self.api.json_content(packet).encode()),
                     replaces_slice_ids=replaces)
        self.registry.setdefault('latest_revisions', {})[key] = revision
        self.files[contract_path] = packet
        self.binary.update({guide_path: guide, **assets})
        return entry, packet

    def draft(self, value):
        key = self.selected_id(value)
        self.require(key not in self.current_deliveries(), f'{key}: replace a released slice through a package amendment')
        approval = self.approval(allow_questions=True)
        self.require(not self.blueprint.pending(), 'Resolve open A+ questions before freezing a revised guide')
        packets = {k: self.selected_packet(k) for k in self.registry['order']}
        prior = self.registry['order'][:self.registry['order'].index(key)]
        source_hash = self.guard_source(approval, self.authorized_paths())
        entry, packet = self.freeze_proposal(value, approval, {k: packets[k] for k in prior},
                                             self.next_revision(key), source_hash)
        packets[key] = packet
        self.coverage(packets)
        if self.map_required:
            conflicts = self.map_conflicts(packets)
            self.require(not conflicts, f'Inventory has cross-slice conflicts: {conflicts}')
        previous = self.registry['selected']
        selected = previous | {key: entry}
        changed = {key} | self.changed_readiness_providers(previous, selected, {key})
        self.registry['selected'] = selected
        if self.registry.get('pending_question_id'):
            self.clear_question_work(self.registry['pending_question_id'])
        self.invalidate(changed)
        self.update_map(packets)
        self.advance(key)

    def changed_readiness_providers(self, previous, selected, changed):
        providers = set()
        for key in changed:
            before = set(previous.get(key, {}).get('dependencies', []))
            after = set(selected.get(key, {}).get('dependencies', []))
            providers.update(before ^ after)
        pending_providers = self.registry.get('handoff_readiness', {}).keys() - self.current_deliveries().keys()
        return providers & pending_providers

    def invalidate(self, changed, *, reset=False):
        impacted = set(changed)
        for key in self.registry['order']:
            if impacted.intersection(self.registry['selected'][key]['dependencies']):
                impacted.add(key)
        package_id = self.registry.get('active_package')
        package = self.registry.get('packages', {}).get(package_id, {})
        if reset:
            self.registry.pop('suspension', None)
            self.registry.pop('continuing_releases', None)
        elif package.get('authorization_path') or self.registry.get('suspension'):
            approval = self.manifest['approvals'][self.active_approval]
            paths = self.authorized_paths()
            snapshot = self.blueprint.workspace_snapshot()
            self.source(approval, paths, snapshot=snapshot)
            continuing = self.registry.get('continuing_releases', {}) | {
                key: package_id for key in package.get('released', {})}
            self.registry['continuing_releases'] = {
                key: origin for key, origin in continuing.items()
                if key in self.registry['selected'] and key not in impacted}
            checkpoint = self.api.sibling_module('slice_integrity').capture_suspension_checkpoint(
                snapshot, package_id or self.registry['suspension']['package_id'],
                self.active_approval)
            self.registry['suspension'] = checkpoint
            continuing_paths = self.delivery_paths(self.registry['continuing_releases'])
            self.source_guard = {'allowed_paths': paths, 'approval_id': self.active_approval,
                                 'artifact_hashes': approval['artifact_hashes'], 'suspension': checkpoint,
                                 'writable_paths': continuing_paths}
            self.events.append({'type': 'slice_package_suspended', 'package_id': checkpoint['package_id'],
                                'source_snapshot_sha256': canonical_hash(checkpoint['snapshot']),
                                'affected_slices': sorted(impacted),
                                'continuing_releases': self.registry['continuing_releases']})
        reviews = self.registry.setdefault('reviews', {})
        stale = [reviews.pop(k)['review_id'] for k in list(reviews) if reset or k in impacted]
        blockers = self.registry.setdefault('review_blockers', {})
        for key in list(blockers):
            if reset or key in changed:
                blockers.pop(key)
        for key in list(self.registry.get('handoff_readiness', {})):
            if reset or key in impacted:
                self.registry['handoff_readiness'].pop(key)
        self.registry.update(changed_dependents=sorted(impacted - changed), collection=None,
                             active_package=None, pending_question_id=None)
        if stale:
            self.events.append({'type': 'slice_reviews_staled', 'review_ids': stale, 'affected': sorted(impacted)})

    def coverage_gaps(self, packets):
        spec = self.blueprint.load('blueprints', self.manifest['approved_version'])
        expected = {(f['feature_id'], c['check_id']) for f in spec['features'] for c in f['acceptance_checks']}
        actual = {(f['feature_id'], c['check_id']) for p in packets.values()
                  for f in p['feature_contracts'] for c in f['acceptance_checks']}
        self.require(actual <= expected, f'Inventory contains unapproved feature/check pairs: {sorted(actual - expected)}')
        mapping = self.blueprint.load('blueprint_coverage', self.manifest['approved_version'])
        missing = {m['baseline_id'] for m in mapping['mappings']} - {b for p in packets.values() for b in p['baseline_ids']}
        return {'feature_checks': [list(pair) for pair in sorted(expected - actual)], 'baseline_ids': sorted(missing)}

    def coverage(self, packets):
        gaps = self.coverage_gaps(packets)
        self.require(not any(gaps.values()), f'Inventory is missing approved coverage: {gaps}')

    def planning_blocker(self):
        gaps = dict(sorted(self.registry['planning_gaps'].items()))
        explanation = (f'Inventory has unresolved coverage or cross-slice boundaries: {gaps}' if gaps.get('cross_slice')
                       else f'Inventory is missing approved coverage: {gaps}')
        return {'result': 'BLOCKED', 'explanation': explanation, 'planning_gaps': gaps,
                'register_path': REGISTER + '.md'}

    def map_conflicts(self, packets):
        service = self.api.sibling_module('slice_map')
        conflicts = {}
        for name, check in (('source_ownership', self.ownership), ('dependencies', self.dependency_contracts),
                            ('shared_interfaces', self.shared_checks),
                            ('duplicate_checks', lambda proposed: service.duplicated_checks(self, proposed))):
            try:
                check(packets)
            except self.api.StateError as error:
                conflicts[name] = str(error)
        untyped = [key for key, packet in packets.items()
                   if packet['dependencies'] and packet.get('evidence_schema_version', 1) < 3]
        if untyped:
            conflicts['legacy_dependencies'] = 'Revise these slices with explicit dependency contracts: ' + ', '.join(untyped)
        return conflicts

    def update_map(self, packets):
        if not self.map_required:
            return
        service = self.api.sibling_module('slice_map')
        version = self.manifest['approved_version']
        coverage = self.blueprint.load('blueprint_coverage', version)
        data = service.breakdown(self.active_approval, packets, coverage)
        checksum = canonical_hash(data)
        if self.registry.get('slice_map', {}).get('breakdown_sha256') == checksum:
            return
        sequence = self.registry.get('map_sequence', 0) + 1
        map_id = f'slice-map-v{sequence}'
        path = f'slice-review/maps/{map_id}.md'
        coverage_entry = self.manifest['blueprint_coverage'][version]
        body = service.render(map_id, data, self.registry['selected'],
                              coverage_entry.get('report_path') or coverage_entry['path']).encode()
        self.binary[path] = body
        self.files[f'slice-review/maps/{map_id}.json'] = data
        target = {'map_id': map_id, 'map_path': path, 'map_sha256': digest(body)}
        documents = {key: {field: item[field] for field in ('guide_path', 'contract_path')}
                     for key, item in self.registry['selected'].items()}
        self.registry.update(map_sequence=sequence, slice_map={'target': target, 'breakdown_sha256': checksum,
                                                               'documents': documents})
        self.events.append({'type': 'slice_map_proposed', 'target': target})

    def advance(self, preferred=None):
        if self.map_required and not (self.current_map() or {}).get('consent_id'):
            self.registry.update(current_slice_id=None, pending_question_id=None)
            self.manifest.update(stage='slice_review', next_action='Present and confirm the proposed implementation slice map')
            return
        pending = [key for key in self.registry['order'] if key not in self.registry['reviews']]
        key = preferred or (pending[0] if pending else None)
        if 'pending_continue' in self.registry:
            self.wait_for_next_slice(key)
            return
        self.registry['current_slice_id'] = key
        self.registry['pending_question_id'] = None
        self.manifest.update(stage='slice_review',
                             next_action=(f'Prepare handoff readiness for {key} revision {self.registry["selected"][key]["revision"]}'
                                          if key and self.adaptive_handoff and key not in self.registry.get('handoff_readiness', {})
                                          else f'Review {key} revision {self.registry["selected"][key]["revision"]}'
                                          if key else 'Validate the reviewed collection'))

    def wait_for_next_slice(self, key):
        self.registry.update(current_slice_id=None, pending_question_id=None)
        if not key:
            self.registry.pop('pending_continue', None)
            self.advance()
            return
        target = self.api.sibling_module('slice_consent').review_target(key, self.registry['selected'][key])
        self.registry['pending_continue'] = {'slices': [target]}
        self.manifest['next_action'] = f'Wait for the user to continue to {key} revision {target["proposal_revision"]}'

    def validate_guide(self, packet, guide, assets):
        self.api.sibling_module('slice_guide').validate_guide(self, packet, guide, assets)
        for feature in packet['feature_contracts']:
            for decision in feature.get('protected_visual_decisions', []):
                self.require(decision['decision_id'] in guide and decision['requirement'] in guide,
                             f"Guide must cite protected visual decision {decision['decision_id']}")

    def check_packet_runtime(self, packet):
        allowed = packet['allowed_paths'] + [path for key in packet['dependencies']
                  for path in self.registry['selected'][key]['allowed_paths']]
        for feature in packet['feature_contracts']:
            self.blueprint.check_test_sources(feature, allowed)

    def check_selected_runtime(self):
        for key in self.registry.get('order', []):
            self.check_packet_runtime(self.selected_packet(key))

    def validate_frozen(self, key):
        entry = self.registry['selected'][key]
        self.blueprint.check_hashes({entry['guide_path']: entry['guide_sha256'],
                                     entry['contract_path']: entry['contract_sha256']})
        packet = self.selected_packet(key)
        active_details = {record['exchange_id'] for record in self.active_slice_details(key)}
        frozen_details = {record['exchange_id'] for record in packet['decision_records']
                          if record.get('record_type') == 'slice_detail'}
        self.require(frozen_details <= active_details,
                     f'{key}: proposal includes a clarification tied to a superseded or unapproved blueprint decision')
        self.check_packet_runtime(packet)
        self.blueprint.check_hashes(packet['asset_hashes'] | packet['artifact_hashes'])
        guide = self.api.artifact_file(self.directory, packet['guide_path']).read_text()
        self.validate_guide(packet, guide, {p: self.api.artifact_file(self.directory, p).read_bytes()
                                           for p in packet['asset_hashes']})
        return packet

    def record_readiness(self, key, entry, value, approval):
        self.require(self.adaptive_handoff and key not in self.registry['reviews'],
                     'Readiness applies to pending adaptive reviews')
        if key != self.registry.get('current_slice_id'):
            consent_id = value.get('consent_id')
            self.require(self.blueprint.identifier(consent_id), 'Prepare the current slice or supply scoped delegation')
            exchange = self.blueprint.load('conversation', consent_id)
            self.check_consent(consent_id, 'delegate', {'slices': [self.visual_consent_target(key, entry)],
                'constraints': exchange.get('target', {}).get('constraints')}, exchange.get('response'))
        if blocker := self.registry.get('review_blockers', {}).get(key):
            self.require(blocker['revision'] != entry['revision'],
                         'Correct the recorded blocker and freeze a new slice revision before repeating readiness')
        packet = self.validate_frozen(key)
        details = {record['exchange_id'] for record in self.active_slice_details(key)}
        self.require(details <= set(packet['decision_ids']),
                     'Freeze a new revision incorporating the recorded clarification before readiness')
        service = self.api.sibling_module('slice_readiness')
        assessment = value.get('assessment')
        depth, reasons = service.depth(self, packet, assessment)
        self.guard_source(approval, self.authorized_paths())
        handoff = value.get('handoff_review')
        hashes = self.api.sibling_module('slice_evidence').validate_handoff(
            self, packet, entry, handoff, verify_symbols=True, depth=depth)
        if 'ambiguity' in assessment['risk_factors']:
            resolution = assessment.get('ambiguity_resolution')
            self.require(self.blueprint.text(resolution),
                         'Explain how the independent review resolved the material ambiguity')
            self.blueprint.check_placeholders(resolution, 'handoff ambiguity resolution')
        self.validate_handoff_illustrations(packet)
        target = self.visual_consent_target(key, entry, verify_illustrations=True)
        receipt = {'target': target, 'result': 'PASS', 'depth': depth, 'reasons': reasons,
                   'assessment': assessment, 'handoff_review': handoff, 'evidence_hashes': hashes,
                   'source_hashes': self.readiness_source_hashes(packet)}
        receipt['terminal'] = service.terminal(entry, receipt)
        path = f'slice-proposals/{key}/readiness/r{entry["revision"]}-{canonical_hash(receipt)}.json'
        self.files[path] = receipt
        self.references.update(hashes)
        self.registry.setdefault('handoff_readiness', {})[key] = receipt | {'path': path}
        self.events.append({'type': 'slice_handoff_ready', 'path': path, 'target': target, 'depth': depth})
        if key == self.registry.get('current_slice_id'):
            self.manifest['next_action'] = f'Discuss {key} revision {entry["revision"]} using its readiness result and guide'

    def require_readiness(self, key, packet=None):
        receipt = self.registry.get('handoff_readiness', {}).get(key)
        entry = self.registry['selected'][key]
        self.require(receipt and receipt['target'] == self.visual_consent_target(key, entry),
                     f'{key}: verify handoff readiness for the current guide, contract and illustrations first')
        self.blueprint.check_hashes(receipt['evidence_hashes'])
        if packet is None:
            packet = self.validate_frozen(key)
        if key not in self.registry.get('continuing_releases', {}):
            self.require(receipt['source_hashes'] == self.readiness_source_hashes(packet),
                         f'{key}: approved repository references changed since handoff readiness')
            depth, _ = self.api.sibling_module('slice_readiness').depth(self, packet, receipt['assessment'])
            self.require(depth == receipt['depth'], f'{key}: repeat handoff readiness at the current verification depth')
        return receipt

    def validate_handoff_illustrations(self, packet):
        media = self.api.sibling_module('review_media')
        for relative in sorted(packet['asset_hashes'].keys() | packet['artifact_hashes'].keys()):
            extension = Path(relative).suffix[1:].lower()
            if extension not in media.RASTER_FORMATS and extension != 'svg':
                continue
            data = self.api.artifact_file(self.directory, relative).read_bytes()
            if extension == 'svg':
                media.validate_svg(data, relative)
            else:
                media.validate_raster(data, extension, relative)

    def check_slice_presentation(self, exchange, readiness):
        path = exchange.get('presentation_path')
        self.require(self.blueprint.text(path) and path in exchange.get('evidence_paths', []),
                     'Attach the saved terminal presentation in presentation_path and evidence_paths before approval')
        hashes = self.api.sibling_module('slice_evidence').evidence_hashes(self, [path])
        text = self.api.artifact_file(self.directory, path).read_text(encoding='utf-8')
        summary = readiness['terminal']
        self.require(text.startswith(summary) and text[len(summary):].strip(),
                     'The terminal presentation must start with the current readiness summary and include the slice explanation')
        return {'path': path, 'sha256': hashes[path], 'readiness_path': readiness['path']}

    def require_readiness_or_blocker_question(self, key, exchange):
        if exchange.get('handoff_blocker') is True:
            blocker = self.registry.get('review_blockers', {}).get(key)
            self.require(blocker and blocker.get('handoff_failure')
                         and blocker['revision'] == self.registry['selected'][key]['revision']
                         and exchange['status'] in ('open', 'proposed')
                         and set(exchange.get('evidence_paths', [])) & blocker['evidence_hashes'].keys(),
                         'A pre-readiness question must cite the current handoff blocker evidence')
        else:
            self.require_readiness(key)

    def readiness_source_hashes(self, packet):
        paths = {anchor['path'] for anchor in packet.get('approved_source_anchors', [])}
        return {path: self.api.digest_of(self.api.project_source_file(self.directory.parents[2], path))
                for path in sorted(paths)}

    def review(self, value):
        key = self.selected_id(value)
        entry = self.registry['selected'][key]
        self.require(type(value.get('proposal_revision')) is int and value['proposal_revision'] == entry['revision'],
                     f'{key}: review must target exact revision {entry["revision"]}')
        action = value.get('action')
        self.require(action in ('question', 'reopen', 'block', 'approve', 'delegate', 'readiness', 'require-independent'),
                     'Choose a valid review action: readiness, require-independent, question, reopen, block, approve or delegate')
        if action not in ('reopen', 'block', 'require-independent'):
            self.require_review_turn()
        if value.get('next_action') is not None:
            self.require(self.blueprint.text(value['next_action']), 'Review next_action must be text')
        approval = self.approval(allow_questions=action in ('question', 'block', 'require-independent'))
        if action == 'readiness':
            self.record_readiness(key, entry, value, approval)
            return
        if action == 'require-independent':
            self.require(self.adaptive_handoff and key not in self.registry['reviews']
                         and self.blueprint.text(value.get('user_response')),
                         'Record the exact user request for an independent review of a pending slice')
            self.registry.setdefault('handoff_escalations', {}).setdefault(key, {})['user'] = (
                'The user requested independent review')
            self.registry.get('handoff_readiness', {}).pop(key, None)
            if key == self.registry.get('current_slice_id') and not self.registry.get('pending_question_id'):
                self.manifest['next_action'] = f'Prepare independent handoff readiness for {key}'
        elif action == 'reopen':
            self.require(self.blueprint.text(value.get('user_response')), 'Record the exact reopening request')
            if self.registry.get('pending_continue'):
                self.require(key in self.registry['reviews'], 'Continue explicitly or reopen an already reviewed slice')
            self.registry.pop('pending_continue', None)
            self.invalidate({key})
            self.advance(key)
        elif action == 'question':
            self.require(key == self.registry['current_slice_id'], 'Question must belong to the open slice')
            question_id = value.get('pending_question_id')
            question = next((q for q in self.blueprint.pending() if q['exchange_id'] == question_id), None)
            self.require(question and question['phase'] == 'slice_review'
                         and question.get('slice_id', key) == key
                         and question.get('proposal_revision', entry['revision']) == entry['revision'],
                         'Record the exact slice-scoped A+ question first')
            self.require(self.registry.get('pending_question_id') in (None, question_id),
                         'The exact pending question must be resolved first')
            self.validate_exchange_scope(self.selected_packet(key), question)
            if self.adaptive_handoff:
                self.require_readiness_or_blocker_question(key, question)
            self.registry['pending_question_id'] = question_id
            self.manifest['next_action'] = value.get('next_action') or question['question']
            self.registry['collection'] = None
        elif action == 'block':
            reason = value.get('reason')
            paths = value.get('evidence_paths')
            self.require(self.blueprint.text(reason) and self.blueprint.strings(paths),
                         f'{key}: give the material blocker and its evidence paths')
            hashes = self.api.sibling_module('slice_evidence').evidence_hashes(self, paths)
            handoff_failure = self.adaptive_handoff and (value.get('handoff_failure') is True
                               or key not in self.registry.get('handoff_readiness', {}))
            if handoff_failure:
                self.registry.setdefault('handoff_escalations', {}).setdefault(key, {})['failure'] = (
                    'A previous handoff readiness check failed')
            pending_question = self.registry.get('pending_question_id')
            current = self.registry.get('current_slice_id')
            next_action = self.manifest['next_action']
            self.invalidate({key})
            self.advance(current if pending_question else key)
            self.registry['pending_question_id'] = pending_question
            self.registry['review_blockers'][key] = {'revision': entry['revision'], 'reason': reason,
                                                    'evidence_hashes': hashes}
            if handoff_failure:
                self.registry['review_blockers'][key]['handoff_failure'] = True
            self.references.update(hashes)
            self.events.append({'type': 'slice_review_blocked', 'slice_id': key,
                                'revision': entry['revision'], 'reason': reason, 'evidence_hashes': hashes})
            self.manifest['next_action'] = (next_action if pending_question else
                                            value.get('next_action') or f'Resolve {key}: {reason}')
        else:
            self.approve_review(key, entry, value)
            self.registry['review_blockers'].pop(key, None)
            next_slice = next((item for item in self.registry['order'] if item not in self.registry['reviews']), None)
            if self.adaptive_handoff and key == self.registry['current_slice_id'] and next_slice:
                self.wait_for_next_slice(next_slice)
            elif self.review_question_id(key) or not self.registry.get('pending_question_id'):
                self.advance()
        self.events.append({'type': 'slice_review_action', 'slice_id': key, 'revision': entry['revision'],
                            'action': action, 'user_response': value.get('user_response'),
                            'question_id': value.get('pending_question_id')})

    def approve_review(self, key, entry, value):
        action = value.get('action')
        self.require(action in {'approve', 'delegate'}, 'Choose question, reopen, block, approve or delegate')
        self.require(key not in self.registry['reviews'], f'{key}: already reviewed; use reopen to reconsider')
        self.require(action == 'delegate' or key == self.registry['current_slice_id'], 'Manually review the open slice first')
        self.require(self.blueprint.text(value.get('user_response')), 'Exact user approval or delegation required')
        for field in ('guide_sha256', 'contract_sha256'):
            self.require(value.get(field) == entry[field], f'{key}: {field} must match the displayed revision')
        packet = self.validate_frozen(key)
        pending = self.blueprint.pending()
        self.require(not pending, f'{key}: resolve the open A+ question before reviewing')
        details = [record['exchange_id'] for record in self.active_slice_details(key)]
        self.require(set(details) <= set(packet['decision_ids']),
                     f'{key}: freeze a new revision incorporating the recorded clarification')
        exchanges = value.get('conversation_exchange_ids', [])
        self.require(self.blueprint.strings(exchanges, False), 'Review exchanges must be distinct IDs')
        question_id = self.review_question_id(key)
        if question_id:
            answer, _ = self.blueprint.question_context(question_id, self.blueprint.conversations())
            self.require(answer and {question_id, answer['exchange_id']} <= set(exchanges),
                         'Link both the saved question and the exact recorded answer')
        for record in packet['decision_records']:
            if record.get('record_type') == 'slice_detail':
                self.require({record['exchange_id'], record['responds_to']} <= set(exchanges),
                             'Link each slice clarification and its saved question in the review receipt')
        evidence = {}
        for exchange_id in exchanges:
            q = self.blueprint.load('conversation', exchange_id)
            self.require(q['phase'] == 'slice_review' and q.get('slice_id', key) == key,
                         'Review exchange exceeds this slice scope')
            self.validate_exchange_scope(packet, q)
            evidence.update(self.blueprint.bound_hashes('conversation', exchange_id))
        readiness = self.require_readiness(key, packet) if self.adaptive_handoff else None
        handoff = readiness['handoff_review'] if readiness else value.get('handoff_review')
        if readiness:
            self.require(value.get('handoff_review', handoff) == handoff,
                         'Approval must use the saved handoff readiness evidence')
            evidence.update(readiness['evidence_hashes'])
            evidence[readiness['path']] = self.registry['artifact_hashes'][readiness['path']]
        else:
            evidence.update(self.api.sibling_module('slice_evidence').validate_handoff(
                self, packet, entry, handoff, verify_symbols=True))
        self.references.update(evidence)
        if action == 'delegate':
            self.require(value.get('delegated_slice_ids') == [key] and self.blueprint.text(value.get('constraints')),
                         'Delegation must explicitly cover this exact slice and state its constraints')
        target = {'slices': [self.visual_consent_target(key, entry)]}
        if action == 'delegate':
            target['constraints'] = value['constraints']
        self.guard_source(self.manifest['approvals'][self.active_approval], self.authorized_paths())
        consent_id = self.consume_consent(value, action, target, slice_id=key)
        presentation = None
        if readiness and action == 'approve':
            exchange = self.blueprint.load('conversation', consent_id)
            presentation = self.check_slice_presentation(exchange, readiness)
            self.require(exchange.get('slice_presentation') == presentation,
                         'Slice consent must bind the current terminal presentation and readiness receipt')
        evidence.update(self.blueprint.bound_hashes('conversation', consent_id))
        review_id = f'{key}-r{entry["revision"]}-review-{self.registry.get("review_sequence", 0) + 1}'
        result = {'review_id': review_id, 'slice_id': key, 'proposal_revision': entry['revision'],
                  'review_mode': 'manual' if action == 'approve' else 'delegated',
                  'reviewer': 'user' if action == 'approve' else handoff['reviewer'],
                  'result': 'approved' if action == 'approve' else 'delegated_validated',
                  'user_response': value['user_response'], 'consent_id': consent_id,
                  'conversation_exchange_ids': exchanges,
                  'reviewed_guide_sha256': entry['guide_sha256'], 'reviewed_contract_sha256': entry['contract_sha256'],
                  'approved_at': self.api.utc_now(), 'handoff_review': handoff, 'evidence_hashes': evidence}
        if readiness:
            result['readiness_path'] = readiness['path']
        if presentation:
            result['presentation'] = presentation
        if action == 'delegate':
            result.update(delegated_slice_ids=[key], constraints=value['constraints'])
        if self.api.has_living_review(self.directory) or value.get('visual_review'):
            visual = self.api.sibling_module('living_review').checkpoint(self.directory, self.manifest, key)
            self.require(visual == value.get('visual_review', {}), 'Confirm the displayed slice visual_review hashes')
            result['visual_review'] = visual
        path = f'slice-proposals/{key}/reviews/{review_id}.json'
        self.files[path] = result
        self.registry['review_sequence'] = self.registry.get('review_sequence', 0) + 1
        self.registry['reviews'][key] = result | {'path': path}
        self.events.append({'type': 'slice_spec_reviewed', **result, 'receipt_path': path})
        if question_id:
            self.clear_question_work(question_id)

    def review_question_id(self, slice_id):
        if slice_id == self.registry.get('current_slice_id'):
            return self.registry.get('pending_question_id')
        return None

    def clear_question_work(self, question_id):
        task_id = self.manifest.get('active_work_id')
        work = self.manifest.get('work_items', {}).get(task_id, {})
        if work.get('pending_question_id') == question_id:
            updated = {**work, 'status': 'in_progress', 'pending_question_id': None,
                       'revision': work['revision'] + 1, 'updated_at': self.api.utc_now()}
            self.manifest['work_items'][task_id] = updated
            self.events.append({'type': 'work_cursor_updated', 'task': updated})

    def current_deliveries(self):
        return {key: a for key, a in self.manifest.get('slices', {}).items()
                if not a.get('superseded_by') and a.get('blueprint_version') == self.manifest.get('approved_version')}

    def delivery_paths(self, released):
        return [path for key, assignment in self.current_deliveries().items()
                if key in released and assignment['status'] != 'cancelled'
                for path in assignment['allowed_paths']]

    def released_paths(self):
        return self.delivery_paths(self.api.active_review_release(self.manifest))

    def unrelated_review_questions(self, slice_id):
        if slice_id not in self.registry.get('continuing_releases', {}):
            return False
        pending = self.blueprint.pending()
        if not pending and (question := self.pending_question()):
            pending = [question]
        return bool(pending) and all(
            q.get('phase') == 'slice_review' and q.get('slice_id') in self.registry.get('selected', {})
            and q['slice_id'] not in self.registry['continuing_releases'] for q in pending)

    def authorized_paths(self):
        released = {slice_id: release for package in self.registry.get('packages', {}).values()
                    for slice_id, release in package['released'].items()}
        paths = []
        for slice_id, assignment in self.current_deliveries().items():
            if slice_id not in released or assignment['status'] == 'cancelled':
                continue
            receipt = released[slice_id]
            self.blueprint.check_hashes({receipt['contract_path']: receipt['contract_sha256']})
            contract = self.api.read_json(self.api.artifact_file(self.directory, receipt['contract_path']))
            self.require(assignment['allowed_paths'] == contract['allowed_paths'] and assignment['owner'] == contract['owner'],
                         f'{slice_id}: delivery ownership differs from the released contract')
            paths.extend(contract['allowed_paths'])
        return paths

    def shared_checks(self, packets):
        providers = {}
        for key, packet in packets.items():
            for name, contract in packet['shared_contracts'].items():
                if contract['role'] == 'provides':
                    self.require(name not in providers, f'{name}: conflicting providers {providers.get(name, ("",))[0]} and {key}')
                    providers[name] = (key, contract['definition'])
        for key, packet in packets.items():
            for name, contract in packet['shared_contracts'].items():
                if contract['role'] == 'consumes':
                    self.require(name in providers, f'{key}: no provider for shared contract {name}')
                    owner, definition = providers[name]
                    self.require(owner in packet['dependencies'], f'{key}: {name} requires explicit dependency on {owner}')
                    self.require(definition == contract['definition'], f'{key} conflicts with {owner} on shared contract {name}')
        conflicts = self.shared_identity_conflicts(packets)
        self.require(not conflicts, 'Conflicting approved shared interface identities: ' + '; '.join(conflicts))

    def shared_identity_conflicts(self, packets):
        """Compare identities that are explicit in approved feature contracts."""
        api_routes = {}
        ui_tokens = {}
        api_schemas = {}
        protected_properties = {}
        for slice_id, packet in packets.items():
            for feature in packet.get('feature_contracts', []):
                backend = feature.get('backend_contract', {})
                request = backend.get('request')
                if isinstance(request, dict):
                    method = request.get('method')
                    route = request.get('route', request.get('path'))
                    match = (method.upper(), route) if isinstance(method, str) and isinstance(route, str) else None
                elif isinstance(request, str):
                    match = re.search(r'\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+([^;\s]+)', request, re.I)
                    if match:
                        match = (match.group(1).upper(), match.group(2))
                else:
                    match = None
                api_annex = feature.get('surface_contracts', {}).get('api', {})
                api_route_text = api_annex.get('Method and route', '') if isinstance(api_annex, dict) else ''
                api_match = re.search(r'\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+([^;\s`]+)',
                                      api_route_text, re.I) if isinstance(api_route_text, str) else None
                if api_match:
                    schema_fields = api_annex.get('Request and response schemas')
                    error_schemas = api_annex.get('Statuses and error schemas')
                    if isinstance(schema_fields, dict) or isinstance(error_schemas, dict):
                        identity = (api_match.group(1).upper(), api_match.group(2))
                        api_schemas.setdefault(identity, []).append((slice_id, feature['feature_id'], schema_fields, error_schemas))
                if match:
                    identity = match if isinstance(match, tuple) else (match.group(1).upper(), match.group(2))
                    api_routes.setdefault(identity, []).append((slice_id, feature['feature_id'], backend.get('response'), backend.get('authorization')))
                for decision in feature.get('protected_visual_decisions', []):
                    target = (decision['component'], decision['property'])
                    protected_properties.setdefault(target, set()).add(decision['requirement'])
                visual = feature.get('visual_contract', {})
                routes = {case.get('route') for case in visual.get('render_cases', [])
                          if isinstance(case, dict) and isinstance(case.get('route'), str)}
                for route in routes:
                    selectors = sorted({case.get('ready_selector') for case in visual.get('render_cases', [])
                                        if case.get('route') == route and isinstance(case.get('ready_selector'), str)})
                    default_component = visual.get('component') or ','.join(selectors)
                    for token, value in visual.get('tokens', {}).items():
                        component = default_component
                        if isinstance(value, dict) and (value.get('component') or value.get('selector')):
                            component = value.get('component') or value.get('selector')
                        ui_tokens.setdefault((route, token, component), []).append((slice_id, feature['feature_id'], value))
        conflicts = [f'{component} / {prop} has conflicting protected visual decisions'
                     for (component, prop), values in sorted(protected_properties.items()) if len(values) > 1]
        for identity, records in api_routes.items():
            responses = {json.dumps(record[2], sort_keys=True, separators=(',', ':'))
                         for record in records if record[2] is not None}
            permissions = {json.dumps(record[3], sort_keys=True, separators=(',', ':'))
                           for record in records if record[3] is not None}
            if len({record[0] for record in records}) > 1 and len(responses) > 1:
                conflicts.append(f'{identity[0]} {identity[1]} has divergent responses')
            if len({record[0] for record in records}) > 1 and len(permissions) > 1:
                conflicts.append(f'{identity[0]} {identity[1]} has divergent authorization')
        for identity, records in api_schemas.items():
            schemas = {(json.dumps(record[2], sort_keys=True, separators=(',', ':')) if record[2] is not None else None,
                        json.dumps(record[3], sort_keys=True, separators=(',', ':')) if record[3] is not None else None)
                       for record in records}
            if len({record[0] for record in records}) > 1 and len(schemas) > 1:
                conflicts.append(f'{identity[0]} {identity[1]} has divergent approved API schemas')
        for (route, token, component), records in ui_tokens.items():
            values = {json.dumps(record[2], sort_keys=True, separators=(',', ':')) for record in records}
            if len({record[0] for record in records}) > 1 and len(values) > 1:
                conflicts.append(f'{route} component {component or "unspecified"} token {token} has divergent values')
        return conflicts

    def dependency_contracts(self, packets):
        for key, packet in packets.items():
            if packet.get('evidence_schema_version', 1) < 3:
                # Historical contracts did not carry the typed, frozen dependency evidence.
                continue
            for provider_id, provider in packets.items():
                if provider_id == key:
                    continue
                omitted_paths = self.referenced_dependency_paths(packet, provider)
                if omitted_paths:
                    self.require(provider_id in packet.get('dependencies', []),
                                 f'{key}: provider-owned paths referenced without a declared dependency on '
                                 f'{provider_id}: {", ".join(omitted_paths)}')
            declarations = packet.get('dependency_contracts')
            if declarations is None and packet.get('evidence_schema_version', 1) < 3:
                # Frozen packages created before typed dependency consumption remain readable.
                continue
            self.require(isinstance(declarations, list),
                         f'{key}: dependency consumption declarations are required')
            by_dependency = {}
            for declaration in declarations:
                self.require(isinstance(declaration, dict),
                             f'{key}: dependency declaration must be an object')
                dependency_id = declaration.get('dependency_id')
                self.require(dependency_id in packet['dependencies'] and dependency_id not in by_dependency,
                             f'{key}: dependency declarations must identify each direct dependency once')
                by_dependency[dependency_id] = declaration
            self.require(set(by_dependency) == set(packet['dependencies']),
                         f'{key}: dependency declarations must cover every direct dependency')

            for dependency_id in packet['dependencies']:
                declaration = by_dependency[dependency_id]
                provider = packets[dependency_id]
                kind = declaration.get('kind')
                frozen_evidence = set(packet.get('artifact_paths', []))
                frozen_evidence.update(packet.get('artifact_hashes', {}).keys())
                frozen_evidence.update(path for path in (packet.get('guide_path'), packet.get('contract_path')) if path)
                frozen_evidence.update(anchor['path'] for anchor in packet.get('approved_source_anchors', [])
                                       if isinstance(anchor, dict) and isinstance(anchor.get('path'), str))
                consumed = sorted(name for name, contract in packet['shared_contracts'].items()
                                  if contract['role'] == 'consumes'
                                  and provider['shared_contracts'].get(name, {}).get('role') == 'provides')
                if kind == 'ordering_only':
                    self.require(set(declaration) == {'dependency_id', 'kind', 'reason', 'evidence_paths'}
                                 and self.blueprint.text(declaration.get('reason'))
                                 and self.blueprint.strings(declaration.get('evidence_paths'))
                                 and set(declaration['evidence_paths']) <= frozen_evidence,
                                 f'{key}: ordering-only dependency {dependency_id} needs a grounded reason and frozen evidence')
                    self.require(not consumed,
                                 f'{key}: dependency {dependency_id} provides a shared contract and cannot be ordering-only')
                elif kind == 'interface':
                    self.require(set(declaration) == {'dependency_id', 'kind', 'contract_ids', 'paths'}
                                 and self.blueprint.strings(declaration.get('contract_ids'))
                                 and self.blueprint.strings(declaration.get('paths'), False),
                                 f'{key}: interface dependency {dependency_id} needs exact contract IDs and provider paths')
                    self.require(set(declaration['contract_ids']) == set(consumed) and bool(consumed),
                                 f'{key}: interface dependency {dependency_id} must name its exact consumed contracts')
                    for path in declaration['paths']:
                        self.require(any(self.api.ownership_contains(owned, path)
                                         for owned in provider['allowed_paths']),
                                     f'{key}: {path} is not owned by dependency {dependency_id}')
                if kind == 'interface':
                    for name in declaration['contract_ids']:
                        self.require(name in provider['shared_contracts']
                                     and provider['shared_contracts'][name]['role'] == 'provides'
                                     and packet['shared_contracts'].get(name, {}).get('role') == 'consumes'
                                     and provider['shared_contracts'][name]['definition']
                                         == packet['shared_contracts'][name]['definition'],
                                     f'{key}: shared contract {name} does not match provider {dependency_id}')
                elif kind == 'files':
                    self.require(set(declaration) == {'dependency_id', 'kind', 'paths'}
                                 and self.blueprint.strings(declaration.get('paths'))
                                 and not consumed,
                                 f'{key}: file dependency {dependency_id} needs exact provider-owned paths')
                    for path in declaration['paths']:
                        self.require(any(self.api.ownership_contains(owned, path)
                                         for owned in provider['allowed_paths']),
                                     f'{key}: {path} is not owned by dependency {dependency_id}')
                elif kind == 'ordering_only':
                    pass
                else:
                    self.require(False, f'{key}: dependency {dependency_id} needs kind interface, files, or ordering_only')

                referenced_paths = self.referenced_dependency_paths(packet, provider)
                if referenced_paths:
                    self.require(kind in {'interface', 'files'}
                                 and set(referenced_paths) <= set(declaration.get('paths', [])),
                                 f'{key}: provider-owned paths referenced by this slice need an interface dependency declaration: '
                                 + ', '.join(referenced_paths))

    def referenced_dependency_paths(self, packet, provider):
        """Find provider-owned paths named in the consumer handoff or test commands."""
        path = packet['guide_path']
        guide = (self.binary[path].decode('utf-8') if path in self.binary
                 else self.api.artifact_file(self.directory, path).read_text(encoding='utf-8'))
        sections = self.api.sibling_module('slice_guide').markdown_sections(guide)
        prose = '\n'.join(sections.get(name, '') for name in
                           ('Architecture and anatomy', 'Detailed functional requirements',
                            'Dependencies and integration', 'Step-by-step implementation recipe'))
        candidates = set()
        candidates.update(argument for feature in packet['feature_contracts']
                          for check in feature.get('acceptance_checks', [])
                          for argument in check.get('command', [])
                          if isinstance(argument, str) and '/' in argument and not argument.startswith('/'))
        candidates.update(argument for feature in packet['feature_contracts']
                          for argument in self.blueprint.test_sources(feature) if '/' in argument)
        candidates.update(match.group(0) for match in re.finditer(
            r'(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.*-]+', prose))
        def project_path(path):
            # Relative artifact links can contain parent hops and are not source ownership references.
            if not path or path.startswith('/') or '\\' in path or any(char in path for char in '*?[]'):
                return False
            parts = path.split('/')
            return all(part not in {'', '.', '..'} for part in parts)

        return sorted(path for path in candidates if project_path(path)
                      and any(self.api.ownership_contains(owned, path) for owned in provider['allowed_paths']))

    def ownership(self, packets):
        paths = []
        for key, packet in packets.items():
            for path in packet['allowed_paths']:
                for owner, assigned in paths:
                    self.require(not self.api.ownership_overlaps(path, assigned),
                                 f'Overlapping writable paths in {owner} ({assigned}) and {key} ({path})')
                paths.append((key, path))

    def dependencies(self, packets):
        prior = set()
        for key in self.registry['order']:
            packet = packets[key]
            self.require(set(packet['dependencies']) <= prior, f'{key}: dependency graph is cyclic or incorrectly ordered')
            self.blueprint.slice_packet({field: packet[field] for field in SLICE_FIELDS},
                                        {dep: packets[dep] for dep in prior}, allow_partial_checks=True)
            prior.add(key)
        self.dependency_contracts(packets)

    def check_visual_review(self, key):
        receipt = self.registry.get('reviews', {}).get(key, {})
        if receipt.get('visual_review') or self.api.has_living_review(self.directory):
            self.api.sibling_module('living_review').checkpoint(self.directory, self.manifest, key, receipt)

    def check_visual_owners(self, packets):
        if self.api.has_living_review(self.directory):
            viewer = self.api.sibling_module('living_review')
            with viewer.History(self.directory) as history:
                state = history.sync(viewer.Documents(self.directory))
            for section in state['sections'].values():
                owner = section['slice']
                self.require(not owner or owner in packets,
                             f"{section['id']}: unknown review slice {owner}")
            # A retired slice no longer reaches the per-packet review loop.
            retired = {scope for removal in state['removals'].values() if removal['active']
                       for scope in removal['affected_scopes'] if scope and scope not in packets}
            for scope in retired:
                viewer.checkpoint(self.directory, self.manifest, scope)

    def ensure_reviews(self, packets):
        self.check_visual_owners(packets)
        self.require(not self.registry.get('pending_question_id') and not self.blueprint.pending(),
                     'Open consequential review question')
        blockers = self.registry.get('review_blockers', {})
        self.require(not blockers, 'Open review blockers: ' + ', '.join(
            f'{key}: {item["reason"]}' for key, item in blockers.items()))
        reviews = self.registry['reviews']
        for key, packet in packets.items():
            entry = self.registry['selected'][key]
            result = reviews.get(key)
            self.require(result and result['proposal_revision'] == entry['revision']
                         and result['reviewed_guide_sha256'] == entry['guide_sha256']
                         and result['reviewed_contract_sha256'] == entry['contract_sha256'],
                         f'{key}: missing or stale slice review')
            self.check_visual_review(key)
            self.blueprint.check_hashes(result['evidence_hashes'])
            action = 'approve' if result['review_mode'] == 'manual' else 'delegate'
            target = {'slices': [self.visual_consent_target(key, entry)]}
            if action == 'delegate':
                target['constraints'] = result['constraints']
            self.check_consent(result.get('consent_id'), action, target, result['user_response'])
            depth = None
            if result.get('readiness_path'):
                readiness = self.require_readiness(key, packet)
                self.require(readiness['path'] == result['readiness_path'],
                             f'{key}: the approved handoff readiness evidence changed')
                depth = readiness['depth']
            self.api.sibling_module('slice_evidence').validate_handoff(
                self, packet, entry, result['handoff_review'], verify_symbols=True, depth=depth)
            pending = {q['exchange_id'] for q in self.blueprint.pending()}
            self.require(not pending.intersection(packet['unresolved_question_ids']),
                         f'{key}: proposal contains unresolved design choices')

    def collection(self, value):
        approval = self.approval(allow_questions=True)
        self.require(not self.registry.get('active_package'), 'A locked collection is immutable; reopen a slice to amend it')
        entries = self.registry['selected']
        semantic_checks = value.get('semantic_checks', {})
        self.require(isinstance(semantic_checks, dict), 'semantic_checks must be an object keyed by reconciliation check')
        paths = [entries[key]['guide_path'] for key in self.registry['order']]
        packets, checks, semantic_hashes = {}, {}, {}

        def check(name, action, evidence, explanation):
            try:
                action()
            except (self.api.StateError, OSError, ValueError, TypeError, KeyError) as error:
                checks[name] = {'result': 'BLOCKED', 'explanation': str(error), 'evidence_paths': evidence}
            else:
                checks[name] = {'result': 'PASS', 'explanation': explanation, 'evidence_paths': evidence}

        def structural():
            for key in self.registry['order']:
                packets[key] = self.validate_frozen(key)

        def semantic(name):
            declaration = semantic_checks.get(name, {})
            result = self.api.sibling_module('slice_evidence').validate_semantic(
                self, name, declaration, packets, entries)
            if name in {'decision_fidelity', 'verification_coverage'} and result['result'] == 'NOT_APPLICABLE':
                result.update(result='BLOCKED', explanation=f'{name} applies to every collection')
            semantic_hashes.update(result.pop('evidence_hashes'))
            checks[name] = result

        check('guide_integrity', structural, paths, 'Guides, annexes, traces and frozen evidence verified')
        mechanical = (
            ('completeness', self.coverage, 'All approved feature/check pairs and baseline IDs covered'),
            ('dependencies', self.dependencies, 'Approved prerequisites and tests fit the dependency order'),
            ('source_ownership', self.ownership, 'Writable paths do not overlap'),
            ('contract_consistency', self.shared_checks, 'Registered providers and consumers agree'),
            ('review_validity', self.ensure_reviews, 'Receipts bind current revisions and no question is open'),
        )
        for name, action, explanation in mechanical:
            if len(packets) != len(entries):
                checks[name] = {'result': 'BLOCKED', 'explanation': 'Resolve guide integrity first', 'evidence_paths': paths}
            else:
                check(name, lambda action=action: action(packets), paths, explanation)
        if self.map_required:
            service = self.api.sibling_module('slice_map')
            check('unique_acceptance_ownership', lambda: service.duplicated_checks(self, packets),
                  paths, 'Every approved acceptance check has one responsible slice')
        check('freshness', lambda: self.source(approval, self.authorized_paths()),
              list(approval['artifact_hashes']), 'Blueprint evidence and source snapshot still match')
        check('handoff_portability', self.check_history, paths, 'Frozen artifact inventory resolves in this workspace')
        for name in ('decision_fidelity', 'shared_contract_review', 'end_to_end_journeys', 'verification_coverage'):
            try:
                semantic(name)
            except (self.api.StateError, OSError, ValueError, TypeError, KeyError) as error:
                checks[name] = {'result': 'BLOCKED', 'explanation': str(error), 'evidence_paths': []}
        result = 'BLOCKED' if any(c['result'] == 'BLOCKED' for c in checks.values()) else 'PASS'
        sequence = self.registry.get('collection_sequence', 0) + 1
        path = f'slice-review/validation/collection-v{sequence}.json'
        review_ids = {k: r['review_id'] for k, r in self.registry['reviews'].items()}
        material_blockers = self.material_collection_blockers(checks, packets, semantic_hashes, path)
        if material_blockers:
            pending_question = self.registry.get('pending_question_id')
            current_slice = self.registry.get('current_slice_id')
            next_action = self.manifest['next_action']
            self.invalidate(set(material_blockers))
            if pending_question:
                self.registry['pending_question_id'] = pending_question
                self.registry['current_slice_id'] = current_slice
                self.manifest['next_action'] = next_action
            else:
                earliest = next(key for key in self.registry['order'] if key in material_blockers)
                self.advance(earliest)
            blockers = self.registry.setdefault('review_blockers', {})
            for key, blocker in material_blockers.items():
                blockers[key] = blocker
        report = {'result': result, 'checks': checks, 'evidence_hashes': semantic_hashes,
                  'approval_id': self.active_approval, 'revisions': entries,
                  'review_ids': review_ids, 'path': path}
        if self.map_required:
            report['slice_map'] = self.current_map()
        self.files[path] = report
        self.references.update(semantic_hashes)
        self.registry.update(collection_sequence=sequence, collection={'path': path, 'result': result})
        self.manifest['next_action'] = ('Review the final recap and approve locking the collection'
                                        if result == 'PASS' else f'Reconcile the blockers recorded in {path}')
        return report

    def material_collection_blockers(self, checks, packets, evidence_hashes, report_path):
        """Resolve trustworthy material findings to proposal slices for scoped re-review."""
        affected_slices = {}
        for key, packet in packets.items():
            affected_slices.setdefault(key, set()).add(key)
            for feature in packet.get('feature_contracts', []):
                feature_id = feature.get('feature_id')
                if feature_id:
                    affected_slices.setdefault(feature_id, set()).add(key)
                for item in feature.get('acceptance_checks', []):
                    check_id = item.get('check_id')
                    if check_id:
                        affected_slices.setdefault(check_id, set()).add(key)
                for case in feature.get('visual_contract', {}).get('render_cases', []):
                    case_id = case.get('case_id')
                    if case_id:
                        affected_slices.setdefault(case_id, set()).add(key)
            for baseline_id in packet.get('baseline_ids', []):
                affected_slices.setdefault(baseline_id, set()).add(key)
            for interface_id in packet.get('shared_contracts', {}):
                affected_slices.setdefault(interface_id, set()).add(key)

        blockers = {}
        all_slices = set(self.registry['order'])
        for name, check in checks.items():
            if check.get('result') != 'BLOCKED' or not check.get('affected_items'):
                continue
            # A declaration only drives lifecycle changes after its semantic evidence
            # passed structural validation and was content-addressed.
            if not evidence_hashes or not check.get('evidence_paths'):
                continue
            matched = set().union(*(affected_slices.get(item, set()) for item in check['affected_items']))
            if any(item not in affected_slices for item in check['affected_items']):
                matched = all_slices
            for key in matched:
                blockers[key] = {'revision': self.registry['selected'][key]['revision'],
                                 'reason': check['explanation'], 'evidence_hashes': evidence_hashes,
                                 'collection_path': report_path, 'reconciliation_check': name,
                                 'affected_items': list(check['affected_items'])}
        return blockers

    def package_preview(self):
        collection = self.registry.get('collection')
        self.require(not self.registry.get('active_package') and collection and collection['result'] == 'PASS',
                     'Passing collection validation required before previewing an unlocked package')
        path = collection['path']
        report = self.api.read_json(self.api.artifact_file(self.directory, path))
        self.blueprint.check_hashes(report['evidence_hashes'])
        self.check_saved_collection_cases(report, self.registry['selected'])
        package_id = f"package-v{len(self.registry.get('packages', {})) + 1}"
        recap = self.api.sibling_module('slice_evidence').render_recap(self, package_id, path, report)
        return {'package_id': package_id, 'collection_path': path,
                'collection_sha256': self.registry['artifact_hashes'][path], 'recap': recap,
                'recap_sha256': digest(recap.encode())}

    def reviewed_guide(self, key, package_id):
        """Render a delivery copy with current review state while preserving the proposal guide."""
        entry = self.registry['selected'][key]
        review = self.registry['reviews'][key]
        source_path = entry['guide_path']
        source_guide = self.api.artifact_file(self.directory, source_path).read_bytes()
        source_directory = posixpath.dirname(source_path)
        renderer = self.api.sibling_module('slice_guide').render_reviewed_guide
        transformed = renderer(source_guide, source_path=source_path, package_id=package_id, review=review)
        output_path = f'{source_directory}/implementation-guide-{package_id}.md'
        return output_path, transformed, source_guide

    def lock(self, value):
        approval = self.approval()
        preview = self.package_preview()
        target = {field: preview[field] for field in ('package_id', 'collection_path', 'collection_sha256', 'recap_sha256')}
        package_id, path = target['package_id'], target['collection_path']
        report = self.api.read_json(self.api.artifact_file(self.directory, path))
        self.require(all(value.get(field) == target[field] for field in
                         ('package_id', 'collection_path', 'collection_sha256'))
                     and value.get('approved_by_user') is True,
                     'Explicit collection approval must name the exact displayed package and validation digest')
        recap = self.api.artifact_file(self.directory, value.get('recap_path')).read_bytes()
        self.require(recap == preview['recap'].encode(), 'Display and approve the complete canonical package recap')
        consent_id = self.consume_consent(value, 'lock', target)
        packets = {key: self.validate_frozen(key) for key in self.registry['order']}
        self.ensure_reviews(packets)
        self.guard_source(approval, self.authorized_paths())
        self.source_guard['artifact_hashes'] = approval['artifact_hashes'] | {
            path: expected for path, expected in self.registry['artifact_hashes'].items()
            if path not in {INVENTORY, REGISTER + '.md', REGISTER + '.json'}}
        packages = self.registry.setdefault('packages', {})
        base = f'slice-review/packages/{package_id}'
        hashes = approval['artifact_hashes'] | report['evidence_hashes'] | self.references | {
            path: self.registry['artifact_hashes'][path],
            f'{base}/final-recap.md': digest(recap),
        }
        current_map = self.current_map()
        if self.map_required:
            self.require(report.get('slice_map') == current_map,
                         'The collection report must validate the confirmed slice map')
            target = current_map['target']
            hashes[target['map_path']] = target['map_sha256']
            presentation = current_map['presentation']
            hashes[presentation['path']] = presentation['sha256']
            hashes.update(self.blueprint.bound_hashes('conversation', current_map['consent_id']))
        package_slices = {}
        for key, packet in packets.items():
            entry, receipt = self.registry['selected'][key], self.registry['reviews'][key]
            reviewed_path, reviewed_bytes, source_guide = self.reviewed_guide(key, package_id)
            reviewed_hash = digest(reviewed_bytes)
            self.binary[reviewed_path] = reviewed_bytes
            package_slices[key] = entry | {'delivery_guide_path': reviewed_path,
                                           'delivery_guide_sha256': reviewed_hash}
            hashes[reviewed_path] = reviewed_hash
            self.require(digest(source_guide) == entry['guide_sha256'],
                         f'{key}: authored guide changed while composing its reviewed delivery copy')
            hashes.update({entry['guide_path']: entry['guide_sha256'], entry['contract_path']: entry['contract_sha256'],
                           receipt['path']: self.registry['artifact_hashes'][receipt['path']]})
            hashes.update(packet['asset_hashes'] | packet['artifact_hashes'] | receipt['evidence_hashes'])
        timestamp = self.api.utc_now()
        package = {'initiative_id': self.manifest['initiative_id'], 'package_id': package_id,
                   'blueprint_approval_id': self.active_approval, 'blueprint_version': self.manifest['approved_version'],
                   'source_snapshot_sha256': canonical_hash(approval['workspace_snapshot']),
                   'order': self.registry['order'], 'slices': package_slices,
                   'review_receipts': {key: r['path'] for key, r in self.registry['reviews'].items()},
                   'collection_path': path, 'artifact_hashes': hashes, 'locked_at': timestamp}
        if current_map:
            package['slice_map'] = current_map
        package_hash = canonical_hash(package)
        package_path = f'{base}/package-manifest.json'
        receipt_path = f'{base}/lock-receipt.json'
        index_path = f'{base}/index.md'
        self.files[package_path] = package | {'package_hash': package_hash}
        self.files[receipt_path] = {'package_id': package_id, 'package_hash': package_hash, 'status': 'locked',
                                    'locked_at': timestamp, 'collection_approval_response': value['user_response'],
                                    'consent_id': consent_id}
        self.binary[f'{base}/final-recap.md'] = recap
        index = [f'# Implementation package {package_id}', '', f'Package hash `{package_hash}`', '',
                 '[Review recap](final-recap.md) · [Package manifest](package-manifest.json)', '',
                 'Run initiative resume and validate before using this handoff. Read the approved blueprint, then the guide and contract.',
                 'Implementation requires authorization and an executable release for this exact package.', '',
                 f'[Collection validation]({posixpath.relpath(path, base)})', '']
        if current_map:
            index.append(f"[Confirmed slice map]({posixpath.relpath(current_map['target']['map_path'], base)})")
        for previous_id, previous in packages.items():
            index.append(f"- [Earlier package {previous_id}]({posixpath.relpath(previous['index_path'], base)})")
        for key, entry in self.registry['selected'].items():
            review_path = self.registry['reviews'][key]['path']
            index.append(f"- [{key} revision {entry['revision']} implementation guide]"
                         f"({posixpath.relpath(package_slices[key]['delivery_guide_path'], base)}) · "
                         f"[authored proposal]({posixpath.relpath(entry['guide_path'], base)}) · "
                         f"[contract]({posixpath.relpath(entry['contract_path'], base)}) · "
                         f"[review receipt]({posixpath.relpath(review_path, base)})")
            prior_revisions = [
                f"[revision {revision}]({posixpath.relpath(guide, base)})"
                for revision in range(1, entry['revision'])
                if (guide := f'slice-proposals/{key}/revisions/{revision}/implementation-guide.md')
                in self.registry['artifact_hashes']
            ]
            if prior_revisions:
                index.append(f"  - Earlier guides · {', '.join(prior_revisions)}")
        self.binary[index_path] = ('\n'.join(index) + '\n').encode()
        packages[package_id] = {'path': package_path, 'package_hash': package_hash, 'receipt_path': receipt_path,
                                'index_path': index_path, 'released': {}}
        self.registry['active_package'] = package_id
        self.manifest['next_action'] = f'Await explicit start instruction for {package_id} ({package_hash})'
        self.events.append({'type': 'slice_package_locked', 'package_id': package_id, 'package_hash': package_hash,
                            'receipt_path': receipt_path})

    def package(self, value=None, *, key=None):
        key = key or self.registry.get('active_package')
        self.require(key, 'Lock the reviewed collection before requesting implementation authorization')
        entry = self.registry['packages'][key]
        if value is not None:
            self.require(value.get('package_id') == key and value.get('package_hash') == entry['package_hash'],
                         f'Request must match exact locked {key} and package hash')
        package = self.api.read_json(self.api.artifact_file(self.directory, entry['path']))
        package_hash = package.pop('package_hash')
        self.require(canonical_hash(package) == package_hash == entry['package_hash']
                     and package['blueprint_approval_id'] == self.active_approval,
                     f'{key}: package integrity or active blueprint approval changed')
        self.blueprint.check_hashes(package['artifact_hashes'])
        report = self.api.read_json(self.api.artifact_file(self.directory, package['collection_path']))
        self.check_saved_collection_cases(report, package['slices'])
        return key, entry, package

    def check_saved_collection_cases(self, report, entries):
        packets = {key: self.api.read_json(self.api.artifact_file(self.directory, entry['contract_path']))
                   for key, entry in entries.items() if entry.get('contract_path')}
        self.api.sibling_module('slice_evidence').check_saved_case_kinds(self, report, packets)

    def validate_package_active_decisions(self, package, visual_scope=None):
        """Reject active packages that froze clarifications outside the current decision set."""
        if visual_scope is None:
            self.check_visual_owners(package['slices'])
        for key in package['order']:
            if visual_scope is None or visual_scope == key:
                self.check_visual_review(key)
            package_entry = package['slices'][key]
            contract_path = package_entry.get('contract_path')
            if not contract_path:
                continue  # Older package shapes remain readable.
            packet = self.api.read_json(self.api.artifact_file(self.directory, contract_path))
            active_details = {record['exchange_id'] for record in self.active_slice_details(key)}
            frozen_details = {record['exchange_id'] for record in packet.get('decision_records', [])
                              if record.get('record_type') == 'slice_detail'}
            self.require(frozen_details <= active_details,
                         f'{key}: locked package contains a clarification tied to a superseded or '
                         'unapproved blueprint decision; create and review a fresh package')

    def authorize(self, value):
        approval = self.approval()
        key, entry, package = self.package(value)
        self.check_package_consents(key, entry, package)
        self.validate_package_active_decisions(package)
        self.require(not entry.get('authorization_path'), f'{key}: immutable start authorization already recorded')
        # A package may have been locked before the current guide rules were installed.
        for slice_id in package['order']:
            self.validate_frozen(slice_id)
        self.require(value.get('instruction') == 'start implementation' and value.get('authorized_by_user') is True,
                     'Record the separate explicit user instruction to start implementation')
        consent_id = self.consume_consent(value, 'start', {'package_id': key, 'package_hash': entry['package_hash']})
        self.guard_source(approval, self.authorized_paths())
        self.source_guard['artifact_hashes'] = package['artifact_hashes'] | self.references
        self.source_guard['validate_runtime'] = True
        path = f'slice-review/packages/{key}/authorization.json'
        receipt = {'package_id': key, 'package_hash': entry['package_hash'], 'authorized_by_user': True,
                   'user_response': value['user_response'], 'instruction': value['instruction'],
                   'scope': package['order'], 'authorized_at': self.api.utc_now(), 'consent_id': consent_id}
        self.files[path] = receipt
        entry['authorization_path'] = path
        self.manifest['next_action'] = f'Release authorized assignments for {key} in dependency order'
        self.events.append({'type': 'slice_implementation_authorized', 'receipt': receipt, 'receipt_path': path})

    def check_package_consents(self, key, entry, package):
        if current_map := package.get('slice_map'):
            self.check_map_consent(current_map)
        lock = self.api.read_json(self.api.artifact_file(self.directory, entry['receipt_path']))
        collection_path = package['collection_path']
        recap_path = f'slice-review/packages/{key}/final-recap.md'
        self.check_consent(lock.get('consent_id'), 'lock',
                           {'package_id': key, 'collection_path': collection_path,
                            'collection_sha256': package['artifact_hashes'][collection_path],
                            'recap_sha256': package['artifact_hashes'][recap_path]}, lock['collection_approval_response'])
        if path := entry.get('authorization_path'):
            receipt = self.api.read_json(self.api.artifact_file(self.directory, path))
            self.require(receipt['package_hash'] == entry['package_hash'] and receipt['scope'] == package['order'],
                         f'{key}: execution authorization does not match package')
            self.check_consent(receipt.get('consent_id'), 'start',
                               {'package_id': key, 'package_hash': entry['package_hash']}, receipt['user_response'])

    def check_map_consent(self, current_map):
        key, target = current_map['consent_id'], current_map['target']
        exchange = self.blueprint.load('conversation', key)
        self.check_consent(key, 'confirm_map', target, exchange['response'])
        presentation = current_map['presentation']
        self.require(exchange.get('map_presentation') == presentation,
                     'Map confirmation must refer to the saved terminal presentation')
        self.blueprint.check_hashes({target['map_path']: target['map_sha256'],
                                     presentation['path']: presentation['sha256']})

    def require_execution(self, slice_id=None, value=None):
        self.check_history()
        approval = self.approval(allow_questions=self.unrelated_review_questions(slice_id))
        active = self.registry.get('packages', {}).get(self.registry.get('active_package'), {})
        origin = (self.registry.get('continuing_releases', {}).get(slice_id)
                  if slice_id and slice_id not in active.get('released', {}) else None)
        key, entry, package = self.package(value, key=origin)
        self.require(entry.get('authorization_path'), f'{key}: separate explicit start authorization required')
        self.check_package_consents(key, entry, package)
        if key == self.registry.get('active_package'):
            self.validate_package_active_decisions(package, slice_id or (value or {}).get('slice_id'))
        self.source(approval, self.authorized_paths())
        if origin:
            self.check_visual_review(slice_id)
            selected = package['slices'][slice_id]
            self.require(self.registry['selected'][slice_id] ==
                         {field: selected[field] for field in self.registry['selected'][slice_id]},
                         f'{slice_id}: continuing assignment revision changed')
            self.check_packet_runtime(self.selected_packet(slice_id))
        else:
            self.check_selected_runtime()
        if slice_id:
            assignment = self.current_deliveries().get(slice_id, {})
            release = entry['released'].get(slice_id)
            self.require(release and assignment.get('contract_file') == release['contract_path']
                         and assignment.get('contract_sha256') == release['contract_sha256']
                         and assignment.get('allowed_paths') == package['slices'][slice_id]['allowed_paths']
                         and assignment.get('dependencies') == package['slices'][slice_id]['dependencies'],
                         f'{slice_id}: assignment is not released by {key}')
            self.require(assignment['status'] != 'cancelled', f'{slice_id}: assignment is cancelled')
            guide_path, guide_hash = released_guide(release, package['slices'][slice_id])
            self.blueprint.check_hashes({release['contract_path']: release['contract_sha256'],
                                         guide_path: guide_hash})
            self.accepted_dependencies(slice_id, assignment['dependencies'], entry)
        return key, entry, package

    def accepted_dependencies(self, slice_id, dependencies, package_entry):
        pending = list(dependencies)
        verified = set()
        while pending:
            key = pending.pop()
            if key in verified:
                continue
            assignment = self.current_deliveries().get(key, {})
            release = package_entry['released'].get(key, {})
            self.require(assignment.get('status') == 'accepted' and release
                         and release['contract_sha256'] == assignment['contract_sha256'],
                         f'{slice_id}: release and accept locked dependencies first ({key})')
            errors = self.blueprint.check_saved_verification(key, assignment)
            self.require(not errors, f'{slice_id}: accepted dependency {key} is stale: ' + '; '.join(errors))
            self.blueprint.api.require_cross_screen(self.blueprint.directory, self.manifest, key)
            if assignment.get('visual_integrity_required'):
                self.blueprint.api.require_visual_integrity(self.blueprint.directory,
                                                            self.manifest, key)
            verified.add(key)
            pending.extend(assignment['dependencies'])
        return sorted(verified)

    def release(self, value):
        key, entry, package = self.require_execution(value=value)
        slice_id = value.get('slice_id')
        self.require(isinstance(slice_id, str) and slice_id in package['slices'], f'Unknown slice in {key}: {slice_id}')
        self.blueprint.api.require_cross_screen(self.blueprint.directory, self.manifest, slice_id)
        self.blueprint.require_ready()
        self.guard_source(self.manifest['approvals'][self.active_approval], self.authorized_paths())
        authorization = entry['authorization_path']
        self.source_guard['artifact_hashes'] = package['artifact_hashes'] | {
            authorization: self.registry['artifact_hashes'][authorization]}
        selected = package['slices'][slice_id]
        self.source_guard['dependencies'] = self.accepted_dependencies(slice_id, selected['dependencies'], entry)
        self.source_guard['validate_runtime'] = True
        if slice_id in entry['released']:
            self.require_execution(slice_id)
            current = self.manifest['slices'][slice_id]
            self.require(current['status'] != 'blocked',
                         f'{slice_id}: ' + '; '.join(current['blockers']))
            return False
        original = self.current_deliveries().get(slice_id)
        prefix = f'slices/{slice_id}'
        contract_path = f'{prefix}/contract.json'
        delivery_guide_path, delivery_guide_hash = package_guide(selected)
        packet = self.selected_packet(slice_id)
        if original:
            self.require(original['status'] != 'cancelled' and
                         original.get('proposal_contract_sha256') == selected['contract_sha256']
                         and original['allowed_paths'] == selected['allowed_paths']
                         and original['dependencies'] == selected['dependencies'],
                         f'{slice_id}: only unchanged assignments can be retained')
            self.blueprint.check_hashes({contract_path: original['contract_sha256']})
            if original['status'] in {'verified', 'accepted'}:
                self.require(not self.blueprint.check_saved_verification(slice_id, original),
                             f'{slice_id}: previous verification must still be current before re-release')
            contract_hash = original['contract_sha256']
            original_package = self.registry['packages'].get(original.get('review_package_id'), {})
            original_package_path = original_package.get('path')
            if original_package_path:
                original_manifest = self.api.read_json(self.api.artifact_file(self.directory, original_package_path))
                origin_slice = original_manifest['slices'][slice_id]
                original_guide_path, original_guide_hash = package_guide(origin_slice)
            else:
                original_guide_path, original_guide_hash = package_guide(selected)
            self.blueprint.check_hashes({original.get('guide_path', original_guide_path): original_guide_hash})
        else:
            self.require(slice_id not in self.manifest.get('slices', {}) and not (self.directory / prefix).exists(),
                         f'{slice_id}: existing delivery evidence cannot be overwritten')
            self.files[contract_path] = {**packet, 'status': 'ready', 'blockers': [],
                                         'review_package_id': key, 'package_hash': entry['package_hash'],
                                         'locked_guide_path': packet['guide_path'],
                                         'delivery_guide_path': delivery_guide_path}
            contract_hash = digest(self.api.json_content(self.files[contract_path]).encode())
            self.manifest['slices'][slice_id] = {
                'owner': packet['owner'], 'status': 'ready', 'blockers': [],
                'blueprint_version': packet['blueprint_version'], 'allowed_paths': packet['allowed_paths'],
                'dependencies': packet['dependencies'], 'contract_file': contract_path,
                'contract_sha256': contract_hash, 'review_package_id': key,
                'guide_path': delivery_guide_path,
                'proposal_contract_sha256': selected['contract_sha256'],
            }
            self.events.append({'type': 'blueprint_slice_created', 'slice_id': slice_id, 'contract_sha256': contract_hash})
        entry['released'][slice_id] = {
            'contract_path': contract_path, 'contract_sha256': contract_hash,
            'guide_path': delivery_guide_path,
            'guide_sha256': delivery_guide_hash,
            'contract_guide_path': original['guide_path'] if original else delivery_guide_path,
        }
        self.registry.get('continuing_releases', {}).pop(slice_id, None)
        if set(entry['released']) == set(package['slices']):
            self.registry.pop('suspension', None)
        self.events.append({'type': 'slice_package_released', 'slice_id': slice_id, 'package_id': key,
                            'contract_sha256': contract_hash})
        if self.blueprint.all_accepted():
            self.manifest.update(stage='integrate', next_action='Review accepted slice integration')
        else:
            self.manifest.update(stage='sliced', next_action=f'Implement released {slice_id} within its exact locked contract')

    def migrate(self, request):
        value = request['input']
        self.require(not self.required, 'Initiative already uses slice review')
        self.require(value.get('approved_by_user') is True and self.blueprint.text(value.get('user_response')),
                     'Migration requires the actual explicit user approval')
        existing = any(a['status'] != 'cancelled' and not a.get('superseded_by')
                       for a in self.manifest.get('slices', {}).values())
        if existing and self.active_approval:
            self.registry['migration_approval_id'] = self.active_approval
        self.manifest['slice_review_required'] = True
        self.manifest['slice_map_required'] = True
        self.registry['migration_request_sha256'] = canonical_hash(value)
        self.events.append({'type': 'slice_review_migrated', 'user_response': value['user_response'],
                            'preserved_assignments': sorted(self.manifest.get('slices', {})), 'approval_id': self.active_approval})
        if self.active_approval:
            action = ('Reconcile legacy delivery and obtain a new blueprint approval'
                      if existing else 'Plan the complete slice inventory under the review gates')
            self.manifest.update(stage='approved', next_action=action)
        self.persist(request)
        return self.summary()

    @staticmethod
    def event_digest(events):
        return canonical_hash([{key: value for key, value in event.items() if key != 'at'} for event in events])

    def check_history(self):
        records = self.records()
        latest = next((i for i in reversed(range(len(records)))
                       if records[i].get('type') == 'slice_review_transition'), None)
        if latest is not None:
            transition = records[latest]
            self.require(self.required and transition['registry_sha256'] == canonical_hash(self.registry),
                         'Slice review register differs from the append-only journal')
            if 'journal_prefix_sha256' in transition:
                self.require(self.event_digest(records[:latest]) == transition['journal_prefix_sha256'],
                             'Slice review lifecycle history differs from the append-only journal')
            self.blueprint.check_hashes(self.registry['artifact_hashes'])
            errors = self.api.sibling_module('slice_integrity').validate_lifecycle_history(
                records, self.registry, self.directory)
            self.require(not errors, '; '.join(errors))
        else:
            self.require(not self.registry, 'Slice review journal history is missing')

    def check(self):
        try:
            self.check_history()
            if self.required and self.active_approval:
                if self.registry.get('active_package') and self.registry['approval_id'] == self.active_approval:
                    self.check_package_consents(*self.package())
                if self.registry.get('approval_id') == self.active_approval:
                    current_map = self.current_map()
                    if current_map and current_map.get('consent_id'):
                        self.check_map_consent(current_map)
                    for origin in set(self.registry.get('continuing_releases', {}).values()):
                        self.check_package_consents(*self.package(key=origin))
                    self.check_selected_runtime()
                if self.registry.get('migration_approval_id') != self.active_approval:
                    self.source(self.manifest['approvals'][self.active_approval], self.authorized_paths())
        except (self.api.StateError, OSError, ValueError, KeyError, TypeError) as error:
            return [str(error)]
        return []
