"""Blueprint-mode contracts. The CLI supplies its shared storage/locking helpers.

This module never imports the CLI or changes legacy prototype records.
"""
from pathlib import Path
from base64 import b64decode
from copy import deepcopy
from hashlib import sha256
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import quote

GROUP_PATHS = {'discovery_audits':'discovery/audits', 'browser_captures':'discovery/browser-receipts',
               'conversation':'conversation', 'blueprint_plans':'plans',
               'blueprints':'blueprints', 'blueprint_coverage':'blueprint-coverage'}
ROADMAP_SECTIONS = frozenset({
    'objective', 'user journeys', 'scope boundary', 'preservation constraints',
    'workstreams', 'dependencies', 'risks', 'build order',
})
SURFACE_PARTS = {'ui': {'ui'}, 'backend': {'backend'}, 'fullstack': {'ui', 'backend'}}
SCRIPT_SUFFIXES = {'.py', '.sh', '.js', '.mjs', '.cjs', '.ts', '.mts', '.cts', '.rb', '.php', '.ps1'}
BACKEND_CONTRACT_FIELDS = ('request', 'response', 'authorization', 'side_effects', 'failure', 'compatibility')
UNFINISHED_FIELD = re.compile(
    r'(?:TBD|TODO|TBC|TO BE DETERMINED|TO BE CONFIRMED)(?:\s*[:–—-].*)?',
    re.IGNORECASE,
)


class BlueprintState:
    def __init__(self, api, directory, manifest):
        self.api, self.directory, self.manifest = api, directory, manifest

    def require(self, condition, message):
        if not condition:
            raise self.api.StateError(message)

    def text(self, value):
        return isinstance(value, str) and bool(value.strip())

    def strings(self, value, nonempty=True):
        return (isinstance(value, list) and (bool(value) or not nonempty)
                and all(self.text(x) for x in value) and len(value) == len(set(value)))

    def identifier(self, value):
        return isinstance(value, str) and bool(self.api.VERSION_ID.fullmatch(value))

    def hashes(self, paths):
        self.require(self.strings(paths), 'Real, distinct evidence/spec paths are required')
        result = {}
        for relative in paths:
            path = self.api.artifact_file(self.directory, relative)
            self.require(path.stat().st_size > 0, f'Empty evidence/spec file: {relative}')
            result[relative] = self.api.digest_of(path)
        return result

    def markdown(self, paths):
        hashes = self.hashes(paths)
        documents = {}
        for relative in hashes:
            path = self.api.artifact_file(self.directory, relative)
            self.require(path.suffix.lower() == '.md', f'Markdown document required: {relative}')
            content = path.read_text(encoding='utf-8')
            self.require(any(line.strip() and not line.lstrip().startswith('#')
                             for line in content.splitlines()), f'Empty Markdown content: {relative}')
            documents[relative] = content
        return hashes, documents

    def blueprint_documents(self, spec, baseline, *, create=False):
        version = spec.get('version')
        self.require(self.identifier(version), 'Invalid blueprint version')
        self.require(self.strings(spec.get('document_paths')) and len(spec['document_paths']) == 1,
                     'Provide one generated blueprint index in document_paths')
        index = spec['document_paths'][0]
        sections = {}
        for feature in spec['features']:
            self.require(self.strings(feature.get('spec_paths')), 'Feature needs generated blueprint document paths')
            section = [f"## {feature['feature_id']}", feature['purpose']]
            for label, key in (('Anatomy', 'anatomy'), ('Behavior', 'behavior_contract')):
                section.extend((f'### {label}', feature[key]))
            fields = {key: value for key, value in feature.items()
                      if key not in {'feature_id', 'purpose', 'anatomy', 'behavior_contract', 'spec_paths', 'diagrams'}}
            if baseline['baseline_kind'] == 'existing':
                observed = [item for item in baseline['features'] if item['feature_id'] in feature['baseline_ids']]
                if observed:
                    fields['observed_baseline'] = observed
            else:
                fields['starting_baseline'] = 'New project / no A'
            requirements = [item for item in baseline.get('requirements', [])
                            if item['requirement_id'] in feature['baseline_ids']]
            if requirements:
                fields['requirements'] = requirements
            for key, value in sorted(fields.items()):
                label = key.replace('_', ' ').capitalize()
                content = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
                section.extend((f'### {label}', '~~~json\n' + content + '\n~~~'))
            diagrams = feature.get('diagrams', [])
            self.require(self.strings(diagrams, False) and all('~~~' not in item for item in diagrams),
                         'Blueprint diagrams must contain Mermaid source')
            for diagram in diagrams:
                section.extend(('### Supporting diagram', '~~~mermaid\n' + diagram + '\n~~~'))
            for path in feature['spec_paths']:
                sections.setdefault(path, []).append('\n\n'.join(section))
        self.require(index not in sections, 'Generated blueprint index must be distinct from feature documents')
        prefix = f'blueprints/{version}/'
        documents = {path: '# Feature contracts\n\n' + '\n\n'.join(items) + '\n'
                     for path, items in sections.items()}
        links = [f'- [{path.removeprefix(prefix)}]({quote(posixpath.relpath(path, posixpath.dirname(index) or "."))})'
                 for path in documents]
        documents[index] = f'# Blueprint {version}\n\n' + '\n'.join(links) + '\n'
        for relative, content in documents.items():
            path = self.directory / relative
            self.require(relative.startswith(prefix) and relative == posixpath.normpath(relative)
                         and path.suffix == '.md' and path.resolve() == self.directory.resolve() / relative,
                         'Generated blueprint Markdown must stay in its version directory')
            if path.exists() or not create:
                self.require(self.api.artifact_file(self.directory, relative).read_bytes() == content.encode('utf-8'),
                             f'Existing file differs from generated blueprint: {relative}; use a new version or path')
        for relative, content in documents.items():
            if not (self.directory / relative).exists():
                self.api.atomic_text(self.directory / relative, content)
        return self.hashes(list(documents))

    def argv(self, value):
        self.require(isinstance(value, list) and bool(value)
                     and all(isinstance(arg, str) and '\0' not in arg for arg in value)
                     and self.text(value[0]), 'Verification requires an approved command argument array')
        return value

    def test_sources(self, feature):
        root = self.directory.parents[2]
        sources = set()
        operations = feature['acceptance_checks'] + feature.get('visual_contract', {}).get('render_cases', [])
        for operation in operations:
            declared = operation.get('test_source_paths', [])
            self.require(self.strings(declared, False), 'Test source paths must be distinct project files')
            operation_sources = set(declared)
            command = self.argv(operation['command'])
            for index, argument in enumerate(command):
                path = Path(argument)
                command_path = index == 0 and '/' in argument
                if (argument.startswith('-') or not (command_path or path.suffix in SCRIPT_SUFFIXES)
                        or (index and command[index - 1] in {'-c', '-e', '--eval', '--command'})):
                    continue
                if path.is_absolute():
                    if not path.is_relative_to(root):
                        continue
                    path = path.relative_to(root)
                operation_sources.add(path.as_posix())
            if 'check_id' in operation:
                self.require(operation_sources,
                             f"Acceptance check {operation['check_id']} must identify its project test source")
            sources.update(operation_sources)
        return sources

    def check_test_sources(self, feature, allowed_paths):
        if self.api.requires_slice_review(self.manifest):
            operations = feature['acceptance_checks'] + feature.get('visual_contract', {}).get('render_cases', [])
            integrity = self.api.sibling_module('slice_integrity')
            errors = integrity.validate_transportable_commands(
                [operation['command'] for operation in operations], self.directory.parents[2])
            self.require(not errors, '; '.join(errors))
        sources = self.test_sources(feature)
        planned = feature.get('planned_test_paths', [])
        self.require(self.strings(planned, False) and set(planned) <= sources,
                     'Planned test files must be referenced by an approved check or render case')
        if self.api.requires_slice_review(self.manifest):
            errors = integrity.validate_runtime_paths(sources - set(planned), self.directory.parents[2])
            self.require(not errors, '; '.join(errors))
        self.require(all(any(self.api.ownership_contains(allowed, path)
                             for allowed in feature['implementation_paths']) for path in planned),
                     'Planned test files must be within the approved feature implementation paths')
        for path in sorted(sources):
            source = self.api.implementation_file(self.directory, path, allow_missing=True)
            may_create = path in planned and any(self.api.ownership_contains(allowed, path)
                                                 for allowed in allowed_paths)
            self.require(source or may_create,
                         f'Missing test source {path}; plan it within the approved implementation paths')

    def load(self, group, key):
        entry = self.manifest.get(group, {}).get(key)
        self.require(entry is not None, f'Unknown {group} record: {key}')
        self.check_hashes({entry['path']:entry['sha256'], **entry.get('evidence_hashes', {})})
        return self.api.read_json(self.api.artifact_file(self.directory, entry['path']))

    def check_hashes(self, hashes):
        for relative, expected in hashes.items():
            self.require(self.api.digest_of(self.api.artifact_file(self.directory, relative)) == expected,
                         f'Changed protected evidence/spec sha256: {relative}')

    def freeze(self, group, key, value, hashes=None, *, transition=None, **metadata):
        self.require(self.identifier(key), f'Invalid record ID/version: {key!r}')
        registry = self.manifest.setdefault(group, {})
        relative = f'{GROUP_PATHS[group]}/{key}.json'
        target = self.directory / relative
        self.require(key not in registry, 'Immutable record already exists; use a new ID/version')
        value = {**value, '_frozen_evidence':hashes or {}}
        if transition is not None:
            self.require(not target.exists(), f'Orphaned record {relative}; reconcile original evidence first')
            entry = {'path': relative, 'sha256': sha256(self.api.json_content(value).encode()).hexdigest(),
                     'evidence_hashes': hashes or {}, 'sequence': len(registry), **metadata}
            writes, events = transition
            writes[relative] = value
            events.append({'type': 'blueprint_record_frozen', 'group': group, 'record_id': key, 'entry': entry})
            registry[key] = entry
            return
        if target.exists():
            saved = self.api.read_json(target)
            # Recovery may reuse only the original content and still-intact evidence.
            self.require({k:v for k,v in saved.items() if k != 'recorded_at'} ==
                         {k:v for k,v in value.items() if k != 'recorded_at'},
                         'Incomplete write differs from retry input; preserve and reconcile original record')
            self.check_hashes(saved['_frozen_evidence'])
        else:
            self.api.atomic_json(target, value)
        entry = {'path':relative, 'sha256':self.api.digest_of(target),
                 'evidence_hashes':hashes or {}, 'sequence':len(registry), **metadata}
        events = [json.loads(line) for line in (self.directory/'decisions.jsonl').read_text().splitlines()]
        earlier = [e['entry'] for e in events if e.get('type') == 'blueprint_record_frozen'
                   and e.get('group') == group and e.get('record_id') == key]
        if earlier:
            self.require(len(earlier) == 1 and earlier[0] == entry, 'Incomplete journal record conflicts with retry')
        else:
            self.api.journal(self.directory, {'type':'blueprint_record_frozen', 'group':group,
                                              'record_id':key, 'entry':entry})
        registry[key] = entry

    def persist(self):
        self.api.save_workflow_state(self.directory, self.manifest)

    def conversations(self):
        return [self.load('conversation', key) for key in sorted(self.manifest.get('conversation', {}),
                   key=lambda key: self.manifest['conversation'][key]['sequence'])]

    def pending(self):
        records = self.conversations()
        resolved = {r.get('responds_to') for r in records if r['status'] in {'decided','rejected'}}
        return [r for r in records if r['status'] in {'open','proposed'}
                and r['exchange_id'] not in resolved]

    def same_decision(self, first, second):
        return (first['status'] in {'decided', 'rejected'} and second['status'] in {'decided', 'rejected'}
                and all(first.get(field) == second.get(field)
                        for field in ('question', 'responds_to', 'report_id'))
                and all(set(first.get(field, [])) == set(second.get(field, []))
                        for field in ('feature_ids', 'requirement_ids'))
                and first.get('record_type', 'decision') == second.get('record_type', 'decision')
                and first.get('record_type', 'decision') in {'decision', 'slice_detail'}
                and (first.get('record_type') != 'slice_detail' or
                     all(first.get(field) == second.get(field) for field in ('slice_id', 'clarifies_decision_ids'))))

    def superseded_decisions(self, records):
        by_id = {record['exchange_id']: record for record in records}
        return {target for record in records if (target := record.get('supersedes')) in by_id
                and self.same_decision(by_id[target], record)}

    def question_context(self, question_id, records):
        answers = [record for record in records if record.get('responds_to') == question_id
                   and record['status'] in {'decided', 'rejected'}]
        original = self.load('conversation', question_id)
        context = next((record for record in reversed(answers) if record.get('source_reconciliation')), original)
        return (answers[-1] if answers else None), context

    def check_question_sources(self, project_root, questions):
        errors = []
        for question in questions:
            question_id = question.get('responds_to') or question['exchange_id']
            for path, expected in question.get('source_hashes_at_record', {}).items():
                try:
                    actual = self.api.digest_of(self.api.project_source_file(project_root, path))
                    if actual != expected:
                        errors.append(f'Source changed for question {question_id}: {path}')
                except (self.api.StateError, OSError) as error:
                    errors.append(f'Source unavailable for question {question_id}: {path} ({error})')
        return errors

    def decisions(self, ids, feature_id=None, historical=False, *, allow_empty=False):
        self.require(self.strings(ids, nonempty=not allow_empty), 'Concrete recorded decision IDs are required')
        result = {}
        superseded = self.superseded_decisions(self.conversations()) if ids and not historical else set()
        for key in ids:
            record = self.load('conversation', key)
            self.require(key not in superseded,
                         f'Decision {key} was superseded; use the recorded amendment')
            self.require(record.get('record_type', 'decision') == 'decision', 'Only material decisions can authorize blueprint changes')
            self.require(record['status'] == 'decided' and self.text(record.get('response')),
                         f'Decision {key} has no actual user answer')
            if feature_id:
                self.require(feature_id in record.get('feature_ids', []) + record.get('requirement_ids', []),
                             f'Decision {key} does not authorize feature/requirement {feature_id}')
            entry = self.manifest['conversation'][key]
            result.update({entry['path']:entry['sha256'], **entry.get('evidence_hashes', {})})
        return result

    def protected_visual_decisions(self, records, scope=None):
        """Project approved visual requirements from immutable A+ exchanges."""
        by_id, by_property = {}, {}
        for record in records:
            affected = set(record.get('feature_ids', []) + record.get('requirement_ids', []))
            if scope is not None and not affected & scope:
                continue
            for decision in record.get('protected_visual_decisions', []):
                key = decision['decision_id']
                target = (decision['component'], decision['property'])
                previous = by_property.setdefault(target, decision['requirement'])
                self.require(previous == decision['requirement'] and
                             (key not in by_id or all(by_id[key][field] == val for field, val in decision.items())),
                             f'Conflicting protected visual decisions for {target[0]} / {target[1]}; '
                             'record an explicit user correction')
                entry = by_id.setdefault(key, {**decision, 'exchange_ids': []})
                entry['exchange_ids'].append(record['exchange_id'])
        return [by_id[key] for key in sorted(by_id)]

    def check_protected_visual_decisions(self, value):
        decisions = value.get('protected_visual_decisions', [])
        fields = {'decision_id', 'component', 'property', 'requirement', 'purpose', 'verification'}
        self.require(isinstance(decisions, list) and all(
            isinstance(item, dict) and item.keys() == fields and self.identifier(item['decision_id'])
            and all(self.text(item[field]) for field in fields) for item in decisions),
            'Protected visual decisions need an ID, component, property, requirement, purpose and verification')
        ids = [item['decision_id'] for item in decisions]
        self.require(len(ids) == len(set(ids)), 'Protected visual decision IDs must be unique per exchange')
        if decisions:
            self.require(value['status'] == 'decided'
                         and value.get('record_type') in (None, 'decision', 'slice_detail')
                         and (value.get('feature_ids') or value.get('requirement_ids')),
                         'Protected visual decisions require an explicit user decision with affected scope')
            self.protected_visual_decisions([value])
        if value.get('supersedes'):
            previous = self.load('conversation', value['supersedes'])
            prior_ids = {item['decision_id'] for item in previous.get('protected_visual_decisions', [])}
            self.require(prior_ids <= set(ids),
                         'A correction must explicitly carry forward or revise each protected visual decision')

    def conversation_record(self, value, *, transition=None):
        key = value.get('exchange_id')
        self.require(self.identifier(key), 'Stable exchange_id is required')
        self.require(value.get('phase') in self.api.VALID_STAGES, 'Actual conversation phase required')
        self.require(self.text(value.get('question')), 'Exact displayed question/proposal is required')
        self.require(value.get('status') in {'proposed','decided','rejected','open','superseded'},
                     'Conversation status is invalid')
        if value['status'] in {'decided','rejected'}:
            self.require(self.text(value.get('response')), 'Actual user response is required')
        else:
            self.require(value.get('response') is None, 'An unanswered proposal cannot invent an answer')
        for field in ('feature_ids','requirement_ids','source_paths','illustration_paths','evidence_paths'):
            self.require(self.strings(value.get(field, []), False), f'{field} must contain distinct references')
        if value['status'] in {'open', 'proposed'}:
            self.require(value.get('feature_ids') or value.get('requirement_ids') or self.text(value.get('scope_reason')),
                         'A pending question needs affected feature/requirement IDs or an explicit scope reason')
            self.require(value.get('source_paths') or value.get('evidence_paths') or self.text(value.get('basis')),
                         'A pending question needs source/evidence paths or an explicit basis in user intent')
        self.require(isinstance(value.get('options', []), list) and all(
            isinstance(x, dict) and self.text(x.get('label')) and self.text(x.get('tradeoff'))
            for x in value.get('options', [])), 'Options need exact labels and tradeoffs')
        record_type = value.get('record_type', 'decision')
        self.require(record_type in {'decision', 'recap', 'slice_detail', 'slice_consent'}, 'Unknown conversation record type')
        if record_type == 'slice_consent':
            self.require(transition is not None and value['phase'] == 'slice_review' and value['status'] == 'decided'
                         and not any(value.get(field) for field in ('responds_to', 'supersedes', 'report_id', 'authorized_changes')),
                         'Slice consent requires a distinct decided exchange for the exact reviewed scope')
        if record_type == 'slice_detail':
            approval = self.manifest.get('approvals', {}).get(self.manifest.get('active_approval_id'), {})
            slice_item = self.manifest.get('slice_review', {}).get('selected', {}).get(value.get('slice_id'), {})
            self.require(self.api.requires_slice_review(self.manifest) and value['phase'] == 'slice_review'
                         and value['status'] == 'decided' and value.get('responds_to')
                         and slice_item.get('revision') == value.get('proposal_revision')
                         and self.strings(value.get('clarifies_decision_ids'))
                         and set(value['clarifies_decision_ids']) <= set(approval.get('blueprint_decision_ids', []))
                         and not any(value.get(field) for field in ('authorized_changes', 'report_id')),
                         'Slice detail must answer the exact open slice question and cite unchanged approved decisions')
        if record_type == 'recap':
            self.require(value['status'] == 'decided'
                         and not any(value.get(field) for field in
                                     ('responds_to', 'supersedes', 'authorized_changes', 'report_id')),
                         'Recaps only acknowledge unchanged decisions; record corrections separately')
        for field in ('responds_to','supersedes'):
            if value.get(field):
                earlier = self.load('conversation', value[field])
                if field == 'responds_to':
                    self.require(earlier['status'] in {'open', 'proposed'}
                                 and value['status'] in {'decided', 'rejected'},
                                 'An answer must link an open question and contain the actual response')
                    self.require(value['question'] == earlier['question'],
                                 'An answer must retain the exact linked question or proposal')
                    if record_type == 'slice_detail':
                        registry = self.manifest['slice_review']
                        self.require(earlier.get('slice_id', registry.get('current_slice_id')) == value['slice_id']
                                     and (registry.get('pending_question_id') == value['responds_to']
                                          or value.get('supersedes')),
                                     'Slice detail must answer the question saved for this slice')
                else:
                    self.require(self.same_decision(earlier, value),
                                 'A correction must retain the exact question, answer link and scope of a prior decision')
                    self.require(value[field] not in self.superseded_decisions(self.conversations()),
                                 'Correct the current decision, not an older superseded record')
                self.require(set(earlier.get('feature_ids', [])) == set(value.get('feature_ids', []))
                    and set(earlier.get('requirement_ids', [])) == set(value.get('requirement_ids', [])),
                    'Answer/correction must retain the affected feature/requirement IDs')
        if value.get('responds_to'):
            current, context = self.question_context(value['responds_to'], self.conversations())
            self.require(not current or value.get('supersedes') == current['exchange_id'],
                         'Question already answered; corrections must explicitly supersede the prior answer')
            source_errors = self.check_question_sources(self.directory.parents[2], [context])
            if source_errors and not value.get('source_reconciliation'):
                raise self.api.StateError('Reconcile question source with fresh evidence before recording the answer: '
                                          + '; '.join(source_errors))
        if 'source_reconciliation' in value:
            self.require(value.get('responds_to') and self.text(value['source_reconciliation'])
                         and self.strings(value.get('evidence_paths')),
                         'Question source reconciliation needs a linked answer, explanation and fresh evidence')
        if value.get('report_id'):
            report = self.manifest.get('missing_reports', {}).get(value['report_id'])
            self.require(report and report['status'] == 'open' and value.get('resolution') == 'not_missing'
                         and value['status'] == 'decided', 'Report corrections require the exact open report and actual user response')
            self.require(set(report['suspected_features']) <= set(value.get('feature_ids', []) + value.get('requirement_ids', [])),
                         'Report correction must identify its actual affected behavior')
        for change in value.get('authorized_changes', []):
            self.require(isinstance(change, dict) and change.get('baseline_id') in
                         value.get('feature_ids', []) + value.get('requirement_ids', [])
                         and change.get('classification') in {'MODIFY','REMOVE'}, 'Authorization exceeds exchange scope')
        self.check_protected_visual_decisions(value)
        paths = value.get('illustration_paths', []) + value.get('evidence_paths', [])
        hashes = self.hashes(paths) if paths else {}
        sources = {p:self.api.digest_of(self.api.project_source_file(self.directory.parents[2], p))
                   for p in value.get('source_paths', [])}
        self.freeze('conversation', key, {**value, 'recorded_at':self.api.utc_now(),
                    'source_hashes_at_record':sources}, hashes, transition=transition)
        if transition is None:
            self.persist()

    def traceability(self):
        usage = {key: {'plan_ids': [], 'blueprint_ids': [], 'slice_ids': []}
                 for key in self.manifest.get('conversation', {})}
        for version in sorted(self.manifest.get('blueprint_plans', {})):
            for decision in self.load('blueprint_plans', version)['decision_ids']:
                usage[decision]['plan_ids'].append(version)
        for version in sorted(self.manifest.get('blueprints', {})):
            spec = self.load('blueprints', version)
            ids = set(spec['decision_ids']) | {d for f in spec['features'] for d in f['decision_ids']}
            for decision in ids:
                usage[decision]['blueprint_ids'].append(version)
        active = self.manifest.get('approved_version')
        feature_slices = {}
        for slice_id, assignment in sorted(self.manifest.get('slices', {}).items()):
            path = self.api.artifact_file(self.directory, assignment['contract_file'])
            self.require(self.api.digest_of(path) == assignment['contract_sha256'], 'Changed slice contract sha256')
            packet = self.api.read_json(path)
            for decision in packet['decision_records']:
                usage[decision['exchange_id']]['slice_ids'].append(slice_id)
            if assignment.get('blueprint_version') == active and assignment['status'] != 'cancelled':
                for feature_id in packet['feature_ids']:
                    feature_slices.setdefault(feature_id, []).append(slice_id)
        coverage = self.load('blueprint_coverage', active)['mappings'] if active else []
        return usage, [{**row, 'slice_ids': feature_slices.get(row['feature_id'], [])} for row in coverage]

    def record_discovery(self, project_root, candidate):
        self.require(candidate.get('scope') == self.manifest['scope'], 'Discovery scope must match initiative')
        kind, surface = candidate.get('baseline_kind'), candidate.get('surface')
        self.require(kind in {'existing','greenfield'} and surface in {'ui','backend','fullstack'},
                     'Blueprint discovery requires baseline_kind and surface')
        previous_id = self.manifest.get('active_discovery_id')
        previous = None
        if previous_id:
            previous = self.baseline(previous_id)
            self.require(previous.get('baseline_kind') == kind and previous.get('surface') == surface,
                         'Baseline kind/surface cannot silently change')
        requirements = candidate.get('requirements', [])
        self.require(isinstance(requirements, list) and (requirements or kind == 'existing'),
                     'Greenfield needs recorded user requirements; use a requirements list for existing apps')
        prior_requirements = (previous or {}).get('requirements', [])
        self.require(all(requirement in requirements for requirement in prior_requirements),
                     'Cannot remove/rewrite prior requirements')
        if kind == 'greenfield':
            self.require(candidate.get('features') == [] and candidate.get('unresolved') == [],
                         'Greenfield must declare no A and no unresolved baseline claims')
            self.require(candidate.get('inspected_paths') == ['.'], 'Verify the entire empty application baseline')
            existing = self.application_files(project_root)
            self.require(previous is not None or not existing, 'Greenfield baseline contains existing application source; use existing discovery')
            ids, hashes = [], self.hashes(candidate.get('evidence_paths'))
            sources = {}
        else:
            def normalized(value):
                result = {**value, 'features':[]}
                for feature in value.get('features', []):
                    record = dict(feature)
                    feature_surface = record.get('surface', surface)
                    self.require(feature_surface in {'ui', 'backend', 'fullstack'}
                                 and (surface == 'fullstack' or feature_surface == surface),
                                 'Discovery feature surface must fit the initiative surface')
                    if feature_surface == 'backend':
                        record['browser_evidence'] = record.get('evidence_paths', [])
                    result['features'].append(record)
                return result
            hashes, sources, errors = self.api.validate_discovery_audit(self.directory, project_root,
                            normalized(candidate), normalized(previous) if previous else None, blueprint=True)
            self.require(not errors, 'Discovery BLOCKED: ' + '; '.join(errors))
            prior_features = {feature['feature_id']: feature for feature in (previous or {}).get('features', [])}
            ids = [f['feature_id'] for f in candidate['features']]
            contracts = candidate.get('behavior_contracts', {})
            self.require(isinstance(contracts, dict) and set(contracts) <= set(ids)
                         and all(isinstance(contract, dict) and self.text(contract.get('behavior_contract'))
                                 and ('backend_contract' not in contract or
                                      isinstance(contract['backend_contract'], dict))
                                 for contract in contracts.values()),
                         'Audited behavior contracts must describe known A feature IDs')
            self.require(all(contracts.get(key) == contract for key, contract in
                             (previous or {}).get('behavior_contracts', {}).items()),
                         'Cannot rewrite audited behavior contracts; record changes in the blueprint')
            historical_paths = {a['path'] for f in (previous or {}).get('features', [])
                                for a in f['source_anchors']}
            retired = candidate.get('retired_source_paths', [])
            self.require(self.strings(retired, False) and set(retired) <= historical_paths
                         and (not retired or self.manifest.get('approvals')),
                         'Only historical A anchors may be retired after approval')
            checked = {path for sweep in candidate['sweeps'] for path in sweep['source_files_checked']}
            for path in checked:
                if path in retired:
                    self.require(not (project_root / path).exists(), f'Retired A source still exists: {path}')
                    sources[path] = None
                else:
                    sources[path] = self.api.digest_of(self.api.project_source_file(project_root, path))
            for feature in candidate['features']:
                if feature.get('surface', surface) in {'ui', 'fullstack'}:
                    is_new_evidence = feature != prior_features.get(feature['feature_id'])
                    hashes.update(self.check_browser_evidence(feature, sources, required=is_new_evidence))
            if not self.manifest.get('approvals'):
                for feature in candidate['features']:
                    for anchor in feature['source_anchors']:
                        source = self.api.project_source_file(project_root, anchor['path'])
                        self.require(anchor['contains'] in source.read_text(), 'Stale A source anchor before initial approval')

        for requirement in requirements:
            self.require(isinstance(requirement, dict) and self.identifier(requirement.get('requirement_id'))
                         and self.text(requirement.get('text')), 'Requirements need stable IDs and actual intent')
            key = requirement['requirement_id']
            self.require(key not in ids, f'Requirement ID {key} duplicates another requirement or observed A feature')
            hashes.update(self.decisions(requirement.get('decision_ids'), key,
                                         historical=requirement in prior_requirements))
            ids.append(key)

        self.require(self.identifier(candidate.get('audit_id')), 'Stable audit ID required')
        audit_id = candidate['audit_id']
        registry = self.manifest.setdefault('discovery_audits', {})
        relative = f'discovery/audits/{audit_id}.json'
        self.require(audit_id not in registry, 'Discovery already exists')
        # False-alarm corrections require explicit user confirmation and fresh evidence.
        resolved = []
        for resolution in candidate.get('missing_report_resolutions', []):
            report_id = resolution.get('report_id')
            report = self.manifest.get('missing_reports', {}).get(report_id)
            self.require(report and report['status'] == 'open' and resolution.get('resolution') == 'not_missing'
                         and resolution.get('confirmed_by_user') is True and self.text(resolution.get('user_note')),
                         'Missing reports need explicit user-confirmed corrections')
            hashes.update(self.hashes(resolution.get('evidence_paths')))
            hashes.update(self.decisions(resolution.get('decision_ids')))
            corrections = [self.load('conversation', d) for d in resolution['decision_ids']]
            self.require(any(c.get('report_id') == report_id and c.get('resolution') == 'not_missing' for c in corrections),
                         'False-gap dismissal requires a fresh correction explicitly linked to this report')
            resolved.append(report_id)
        active = self.manifest.get('approvals', {}).get(self.manifest.get('active_approval_id'))
        equivalent_approval_id = None
        if active and resolved:
            approved_audit_id = active['baseline_id']
            approved_audit = self.baseline(approved_audit_id, current=False)
            if all(candidate.get(field) == approved_audit.get(field) for field in (
                    'features', 'behavior_contracts', 'requirements', 'routes', 'retired_source_paths')) and (
                    sources == self.manifest['discovery_audits'][approved_audit_id]['source_hashes_at_discovery']):
                equivalent_approval_id = active['decision_id']
        self.freeze('discovery_audits', audit_id, candidate, hashes,
                    source_hashes_at_discovery=sources,feature_ids=ids,baseline_kind=kind,surface=surface,
                    approved_before_discovery=bool(self.manifest.get('approvals')),
                    equivalent_approval_id=equivalent_approval_id)
        self.manifest['active_discovery_id'] = audit_id
        for report_id in resolved:
            self.manifest['missing_reports'][report_id].update(status='dismissed', resolution_audit_id=audit_id)
        if active:
            old = self.manifest['discovery_audits'][active['baseline_id']]['feature_ids']
            open_reports = any(r['status'] == 'open' for r in self.manifest.get('missing_reports', {}).values())
            self.manifest['coverage_pending'] = open_reports or set(ids) != set(old)
            if not self.manifest['coverage_pending']:
                self.manifest['blockers'] = [b for b in self.manifest.get('blockers', [])
                    if not b.startswith('Missing application-A functionality reported:')]
        self.manifest['stage'] = 'plan'
        self.manifest['next_action'] = 'Discuss the roadmap, then record a new blueprint; preserve prior approvals'
        self.persist()

    def check_browser_setup(self, setup):
        self.require(isinstance(setup, list), 'Browser setup must be a list of actions')
        for step in setup:
            self.require(isinstance(step, dict) and step.get('action') in ('navigate', 'wait', 'click', 'fill', 'press'),
                         'Browser setup actions must be navigate, wait, click, fill or press')
            if step['action'] == 'navigate':
                route = step.get('route')
                self.require(isinstance(route, str) and route.startswith('/') and not route.startswith('//'),
                             'Browser setup navigation needs a local app route')
            elif step['action'] == 'press':
                self.require(step.get('key') in ('Tab', 'Enter', 'Escape', 'Space', 'End', 'Home',
                                                'ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown')
                             and type(step.get('shift', False)) is bool,
                             'Browser press requires a supported key and an optional boolean shift modifier')
            else:
                self.require(self.text(step.get('selector')), 'Browser setup action needs a selector')
            if step['action'] == 'fill':
                self.require(isinstance(step.get('value'), str), 'Browser fill needs a string value')
    def check_browser_observations(self, observations):
        self.require(isinstance(observations, dict) and set(observations) == {'role', 'state'},
                     'Discovery needs browser observations for the role and state')
        for name, observation in observations.items():
            self.require(isinstance(observation, dict) and self.text(observation.get('selector'))
                         and self.text(observation.get('expected'))
                         and ('attribute' not in observation or self.text(observation['attribute'])),
                         f'Discovery needs a selector and expected observed {name}')

    def discovery_capture(self, value):
        key = value.get('capture_id')
        self.require(self.identifier(key) and self.identifier(value.get('feature_id')),
                     'Browser discovery requires stable capture and feature IDs')
        self.require(key not in self.manifest.get('browser_captures', {}), 'Browser capture ID already recorded')
        route = value.get('route')
        self.require(isinstance(route, str) and route.startswith('/') and not route.startswith('//')
                     and all(self.text(value.get(field)) for field in ('role', 'state', 'ready_selector')),
                     'Browser capture requires a route, role, state and ready selector')
        viewport = value.get('viewport')
        self.require(isinstance(viewport, dict) and all(type(viewport.get(k)) is int and viewport[k] > 0
                     for k in ('width', 'height')), 'Browser capture requires positive viewport dimensions')
        self.require('command' not in value, 'Discovery does not accept host commands; use browser setup actions')
        self.check_browser_setup(value.get('setup', []))
        observations = value.get('observations')
        self.check_browser_observations(observations)
        paths = value.get('source_paths', [])
        self.require(self.strings(paths, False), 'Capture source paths must be distinct project files')
        receipt_path = f"{GROUP_PATHS['browser_captures']}/{key}.json"
        if (self.directory / receipt_path).is_file():
            saved = self.api.read_json(self.api.artifact_file(self.directory, receipt_path))
            observation, relative = saved['browser'], saved['screenshot']
            source_snapshot = saved.get('source_snapshot')
            path = self.api.artifact_file(self.directory, relative)
        else:
            source_snapshot = self.discovery_snapshot(paths)
            try:
                observation, image = self.capture_render(value, [], build={
                    'fingerprint': self.build_fingerprint(source_snapshot), 'sources': source_snapshot,
                })
            except subprocess.TimeoutExpired as error:
                raise self.api.StateError('Browser discovery capture timed out') from error
            after = self.discovery_snapshot(paths)
            changed = sorted(path for path in source_snapshot.keys() | after.keys()
                             if source_snapshot.get(path) != after.get(path))
            self.require(not changed, 'Application files changed during discovery capture: ' + ', '.join(changed))
            self.check_capture_identity(observations, observation)
            relative = f'discovery/browser/{key}-{sha256(image).hexdigest()}.png'
            path = self.directory / relative
            self.require(path.resolve() == self.directory.resolve() / relative, 'Browser capture cannot use a redirect')
            self.api.atomic_bytes(path, image)
        self.require(self.api.png_dimensions(path) == (viewport['width'], viewport['height']),
                     'Browser discovery capture does not match its viewport')
        self.freeze('browser_captures', key, {**value, 'browser': observation, 'screenshot': relative,
                                               'source_snapshot': source_snapshot},
                    self.hashes([relative]), screenshot=relative)
        self.persist()
        print(f'Browser discovery evidence: {relative}')

    @staticmethod
    def build_fingerprint(snapshot):
        return sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def discovery_snapshot(self, extra_paths):
        snapshot = self.workspace_snapshot()
        root = self.directory.parents[2]
        for path in extra_paths:
            source = self.api.project_source_file(root, path)
            snapshot[Path(path).as_posix()] = f'{stat.S_IMODE(source.stat().st_mode):o}:' + self.api.digest_of(source)
        return snapshot

    def check_capture_identity(self, observations, browser):
        actual = browser.get('identities', {})
        for name, observation in observations.items():
            self.require(actual.get(name) == observation['expected'],
                         f"Browser observed {name} {actual.get(name)!r}; expected {observation['expected']!r}")

    def check_browser_evidence(self, feature, sources, *, required=True):
        hashes = {}
        labels = {'role': {}, 'state': {}}
        stale = set()
        paths = set(feature.get('browser_evidence', []))
        for key, entry in self.manifest.get('browser_captures', {}).items():
            if entry['screenshot'] not in paths:
                continue
            capture = self.load('browser_captures', key)
            self.require(capture['feature_id'] == feature['feature_id'] and capture['route'] == feature['route']
                         and capture['state'] in feature['states'] and capture['role'] in feature['roles'],
                         'Browser discovery receipt does not match the feature, route, role and state')
            snapshot = capture.get('source_snapshot')
            if not isinstance(snapshot, dict) or not capture['browser'].get('buildEvidence'):
                continue  # Old receipts remain in history and need fresh evidence for new approval.
            changed = set()
            for path, expected in sources.items():
                fingerprint = snapshot.get(Path(path).as_posix())
                matches = fingerprint is None if expected is None else re.fullmatch(
                    r'[0-7]+:' + re.escape(expected), str(fingerprint))
                if not matches:
                    changed.add(path)
            if changed:
                stale.update(changed)
                continue
            self.check_capture_identity(capture['observations'], capture['browser'])
            for name, observed in capture['browser']['identities'].items():
                prior = labels[name].setdefault(observed, capture[name])
                self.require(prior == capture[name],
                             f"Browser evidence uses the same observed {name} for different labels")
            hashes.update(self.bound_hashes('browser_captures', key))
        complete = set(labels['state'].values()) == set(feature['states']) and set(labels['role'].values()) == set(feature['roles'])
        if not complete:
            self.require(not stale, f"Stale browser capture for {feature['feature_id']}: {', '.join(sorted(stale))}; recapture")
            self.require(not required and not hashes,
                         f"Feature {feature['feature_id']} needs a current CLI browser capture receipt for every state and role")
        return hashes

    def check_records(self, recovering=None, *, check_indexes=True):
        errors = []
        try:
            journal = [json.loads(line) for line in (self.directory/'decisions.jsonl').read_text().splitlines()]
            frozen = {(e['group'], e['record_id']):e['entry'] for e in journal
                      if e.get('type') == 'blueprint_record_frozen'}
            for group in GROUP_PATHS:
                for key, entry in self.manifest.get(group, {}).items():
                    self.load(group, key)
                    self.require(frozen.get((group,key)) == entry, 'Record registry differs from append-only journal')
            self.require(all(key in self.manifest.get(group, {}) or (group,key) == recovering for group,key in frozen),
                         'Frozen conversation/spec record is missing from manifest')
            for group, folder in GROUP_PATHS.items():
                for path in (self.directory/folder).glob('*.json'):
                    key = path.stem
                    self.require(key in self.manifest.get(group, {}) or (group,key) == recovering,
                                 f'Incomplete record write: {path.relative_to(self.directory)}; retry original command with exact input')
            created_slices = {e['slice_id'] for e in journal if e.get('type') == 'blueprint_slice_created'}
            saved_slices = {path.parent.name for path in (self.directory / 'slices').glob('*/contract.json')}
            for key in sorted((created_slices | saved_slices) - self.manifest.get('slices', {}).keys()):
                self.require(recovering == ('slices', key),
                             f'Incomplete slice creation: {key}; retry slice-create with the original input')
            for audit_id in self.manifest.get('discovery_audits', {}):
                self.baseline(audit_id, current=False)
            if check_indexes:
                self.check_roadmap_indexes(recovering)
            approvals = {e['decision_id']:e['approval'] for e in journal if e.get('type') == 'approval'}
            self.require(approvals == self.manifest.get('approvals', {}), 'Approval history differs from immutable journal')
            self.check_delivery_journal(journal)
            retired = {slice_id: e['decision_id'] for e in journal if e.get('type') == 'approval'
                       for slice_id in e.get('superseded_slice_ids', [])}
            self.require(retired == {key: item['superseded_by'] for key, item in self.manifest.get('slices', {}).items()
                                     if item.get('superseded_by')}, 'Superseded slice history differs from journal')
            if self.manifest.get('stage') == 'integrate':
                self.require(self.all_accepted(), 'Integration lacks accepted coverage for approved feature/check pairs')
        except (self.api.StateError, OSError, ValueError, KeyError, TypeError) as error:
            errors.append(str(error))
        return errors

    def check_delivery_journal(self, journal):
        """Detect interrupted delivery writes from initiatives created before staged transitions."""
        active = None
        explicit_blocks = {}
        reports = set()
        for event in journal:
            kind = event.get('type')
            if kind == 'approval':
                active = event['decision_id']
            elif kind == 'approval_revoked':
                active = None
            elif kind == 'application_a_functionality_missing':
                reports.add(event['report_id'])
            elif kind == 'slice_blocked':
                explicit_blocks[event['slice_id']] = event
            elif kind in {'slice_unblocked_after_resolution', 'slice_cancelled'}:
                explicit_blocks.pop(event['slice_id'], None)
        self.require(active == self.manifest.get('active_approval_id'),
                     'Active approval differs from the approval/withdrawal journal')
        saved_reports = self.manifest.get('missing_reports', {})
        files = {path.stem for path in (self.directory / 'discovery/missing').glob('*.json')}
        self.require(reports == set(saved_reports) == files,
                     'Missing-feature report files, journal and manifest disagree')
        for slice_id, event in explicit_blocks.items():
            assignment = self.manifest.get('slices', {}).get(slice_id, {})
            self.require((assignment.get('status') == 'blocked' and event['reason'] in assignment.get('blockers', []))
                         or assignment.get('superseded_by'),
                         f'Slice {slice_id}: saved status disagrees with its explicit defect blocker')

    def bound_hashes(self, group, key):
        self.load(group, key)
        entry = self.manifest[group][key]
        return {entry['path']:entry['sha256'], **entry['evidence_hashes']}

    def check_roadmap_indexes(self, recovering):
        recovering_plan = None
        if (recovering and recovering[0] == 'blueprints' and self.identifier(recovering[1])
                and recovering[1] not in self.manifest.get('blueprints', {})):
            draft = f'blueprints/{recovering[1]}.json'
            if (self.directory / draft).is_file():
                recovering_plan = self.api.read_json(self.api.artifact_file(self.directory, draft)).get('plan_version')
        for version in self.manifest.get('blueprint_plans', {}):
            if version != recovering_plan and (index := self.plan_blueprint_index(version)):
                relative, content = index
                path = self.directory / relative
                self.require(path.is_file() and path.read_bytes() == content.encode('utf-8'),
                             f'Missing or changed roadmap blueprint index: {relative}; run blueprint-index-rebuild')

    def plan_blueprint_index(self, plan_version):
        """Derive the roadmap index from the frozen plan and blueprint records."""
        plan = self.load('blueprint_plans', plan_version)
        relative_index = plan.get('blueprint_index_path')
        if relative_index is None:
            return
        target = self.directory / relative_index
        self.require(relative_index == f'plans/{plan_version}/blueprints/index.md'
                     and target.resolve() == self.directory.resolve() / relative_index
                     and relative_index not in plan['document_paths'],
                     'Generated blueprint index must stay separate from frozen plan documents')
        links = []
        for version in sorted(self.manifest.get('blueprints', {})):
            spec = self.load('blueprints', version)
            if spec['plan_version'] != plan_version:
                continue
            index = spec['document_paths'][0]
            relative = posixpath.relpath(index, f'plans/{plan_version}/blueprints')
            links.append(f'- [{version}]({quote(relative)})')
        if links or plan.get('index_from_plan'):
            listing = '\n'.join(links) if links else 'No blueprints have been recorded for this roadmap yet.'
            return relative_index, f'# Blueprints for {plan_version}\n\n{listing}\n'

    def write_plan_blueprint_index(self, plan_version):
        index = self.plan_blueprint_index(plan_version)
        if index:
            relative, content = index
            self.api.atomic_text(self.directory / relative, content)
            return relative

    def baseline(self, key, current=True):
        entry = self.manifest.get('discovery_audits', {}).get(key)
        self.require(entry is not None, 'Missing frozen A/greenfield baseline')
        if current:
            self.require(key == self.manifest.get('active_discovery_id'), 'Stale baseline audit')
        self.check_hashes({entry['path']:entry['sha256'], **entry['evidence_hashes']})
        return self.api.read_json(self.api.artifact_file(self.directory, entry['path']))

    def baseline_hashes(self, key):
        self.baseline(key)
        entry = self.manifest['discovery_audits'][key]
        return {entry['path']:entry['sha256'], **entry['evidence_hashes']}

    def plan_record(self, value):
        self.require_ready()
        self.require(not self.pending(), "Roadmap is waiting on a consequential question")
        self.require(value.get('scope') == self.manifest['scope'] and self.text(value.get('objective')),
                     'Plan requires initiative scope and objective')
        for field in ('workstreams','dependencies','risks'):
            self.require(self.strings(value.get(field), nonempty=field == 'workstreams'), f'Plan requires concrete {field}')
        hashes = self.baseline_hashes(value.get('baseline_id'))
        hashes.update(self.decisions(value.get('decision_ids'), allow_empty=True))
        self.require_applicable_decisions({**value, 'features': []})
        markdown_hashes, documents = self.markdown(value.get('document_paths'))
        self.check_roadmap(documents)
        self.require(self.identifier(value.get('version')), 'Roadmap requires a stable version')
        expected_navigation = f"plans/{value['version']}/blueprints/index.md"
        navigation = value.get('blueprint_index_path', expected_navigation)
        self.require(navigation == expected_navigation and navigation not in documents,
                     'Blueprint index must be a separate plans/<plan-version>/blueprints/index.md')
        linked_path = any(
            f']({posixpath.relpath(navigation, posixpath.dirname(path) or ".")})' in content
            for path, content in documents.items())
        self.require(linked_path, 'Roadmap must link to its generated blueprint index')
        hashes.update(markdown_hashes)
        self.freeze('blueprint_plans', value['version'],
                    {**value, 'blueprint_index_path': navigation, 'index_from_plan': True}, hashes)
        self.write_plan_blueprint_index(value['version'])
        self.manifest['stage'] = 'blueprint'
        self.manifest['next_action'] = 'Discuss detailed anatomy, state and API contracts; freeze a complete blueprint'
        self.persist()

    def check_roadmap(self, documents):
        sections = set()
        for content in documents.values():
            parts = re.split(r'(?m)^#{1,6}[ \t]+([^\n]+)\n', content)
            for heading, body in zip(parts[1::2], parts[2::2]):
                if body.strip():
                    sections.add(heading.strip().casefold())
        missing = ROADMAP_SECTIONS - sections
        self.require(not missing, 'Roadmap needs substantive sections: ' + ', '.join(sorted(missing)))

    def check_preserved_behavior(self, baseline, spec):
        if baseline['baseline_kind'] != 'existing':
            return
        audited = {record['feature_id']: record for record in baseline['features']}
        contracts = baseline.get('behavior_contracts', {})
        preserved_surfaces = {}
        for feature in spec['features']:
            if feature['classification'] not in {'KEEP', 'MOVE'}:
                continue
            parts = SURFACE_PARTS[feature.get('surface', spec['surface'])]
            for baseline_id in feature['baseline_ids']:
                if baseline_id not in audited:
                    continue
                observed = contracts.get(baseline_id)
                self.require(observed is not None, f'{baseline_id}: record the observed A behavior contract before KEEP/MOVE')
                matching_behavior = observed['behavior_contract'] == feature['behavior_contract']
                matching_backend = ('backend' not in parts or
                                    observed.get('backend_contract') == feature['backend_contract'])
                keeps_states = set(audited[baseline_id]['states']) <= set(feature['states'])
                self.require(matching_behavior and matching_backend and keeps_states,
                             f'{baseline_id}: KEEP/MOVE must preserve audited behavior, backend rules and states; '
                             'use a user-authorized MODIFY for a behavior change')
                if 'ui' in parts and len(audited[baseline_id]['roles']) > 1:
                    self.check_preserved_roles(audited[baseline_id], feature)
                preserved_surfaces.setdefault(baseline_id, set()).update(parts)
        for baseline_id, parts in preserved_surfaces.items():
            original = audited[baseline_id].get('surface', baseline['surface'])
            self.require(SURFACE_PARTS[original] <= parts,
                         f'{baseline_id}: KEEP/MOVE must cover every audited UI and backend surface')

    def check_preserved_roles(self, audited, feature):
        cases = feature['visual_contract']['render_cases']
        receipts = set(audited['browser_evidence'])
        for key, entry in self.manifest.get('browser_captures', {}).items():
            if entry['screenshot'] not in receipts:
                continue
            capture = self.load('browser_captures', key)
            self.require(any(
                case.get('role') == capture['role']
                and case['state'] == capture['state']
                and case['ready_selector'] == capture['ready_selector']
                and all(case.get('observations', {}).get(name, {}).get('expected') == observed['expected']
                        for name, observed in capture['observations'].items())
                for case in cases
            ), f"{audited['feature_id']}: verify audited role/state {capture['role']}/{capture['state']} "
               f"and its visible marker {capture['ready_selector']} in a render case")

    def check_fullstack_acceptance(self, spec):
        for feature in spec['features']:
            if feature.get('surface', spec['surface']) == 'fullstack':
                self.require(any(check['kind'] == 'backend' for check in feature['acceptance_checks']),
                             f"Full-stack feature {feature['feature_id']} needs a backend acceptance check")

    def check_placeholders(self, value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                self.check_placeholders(item, f'{path}.{key}')
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.check_placeholders(item, f'{path}[{index}]')
        elif isinstance(value, str):
            for number, line in enumerate(value.splitlines(), 1):
                text = re.sub(r'^(?:(?:[-+*]|\d+[.)])\s+)?(?:\[[ xX]\]\s+)?', '', line.strip())
                text = text.strip(' `*_[]{}<>.!?')
                template_marker = text == 'REPLACE' or text.startswith(('REPLACE ', 'REPLACE_'))
                self.require(not UNFINISHED_FIELD.fullmatch(text) and not template_marker,
                             f'Unfinished placeholder in {path}, line {number}; resolve the detail before continuing')

    def check_blueprint_placeholders(self, spec):
        for feature in spec['features']:
            fields = {name: feature[name] for name in ('purpose', 'anatomy', 'behavior_contract')}
            surface = feature.get('surface', spec['surface'])
            if surface in {'ui', 'fullstack'}:
                visual = feature['visual_contract']
                fields['visual_contract'] = {name: visual[name] for name in ('tokens', 'responsive', 'accessibility')}
            if surface in {'backend', 'fullstack'}:
                backend = feature['backend_contract']
                fields['backend_contract'] = {name: backend[name] for name in BACKEND_CONTRACT_FIELDS}
            self.check_placeholders(fields, f"feature {feature['feature_id']}")

    def check_backend_acceptance(self, spec):
        for feature in spec['features']:
            surface = feature.get('surface', spec['surface'])
            if surface not in {'backend', 'fullstack'}:
                continue
            states = set(feature['states'])
            required = states.copy() if surface == 'backend' else set()
            if feature['classification'] != 'REMOVE':
                required.add('backend_failure')
            covered = {}
            for check in feature['acceptance_checks']:
                outcomes = check.get('state_expectations', {})
                self.require(isinstance(outcomes, dict) and set(outcomes) <= states | {'backend_failure'}
                             and all(self.text(result) for result in outcomes.values())
                             and (not outcomes or check['kind'] == 'backend'),
                             'Only backend acceptance checks may declare named state expectations')
                self.check_placeholders(outcomes, f"acceptance check {check['check_id']}.state_expectations")
                for name, outcome in outcomes.items():
                    self.require(covered.setdefault(name, outcome) == outcome,
                                 f'Conflicting backend acceptance expectations for {name}')
            self.require(required <= covered.keys(),
                         f"Feature {feature['feature_id']} needs backend acceptance evidence for: "
                         + ', '.join(sorted(required - covered.keys())))
            if 'backend_failure' in required:
                self.require(covered['backend_failure'] == feature['backend_contract']['failure'],
                             'Backend acceptance must assert the exact backend_contract.failure outcome')

    def blueprint_record(self, value):
        self.require_ready()
        surface_contracts = self.api.sibling_module('surface_contracts')
        if self.api.requires_slice_review(self.manifest):
            self.require(value.get('surface_contract_version') == surface_contracts.SURFACE_CONTRACT_VERSION,
                         f'New reviewed blueprints require surface_contract_version '
                         f'{surface_contracts.SURFACE_CONTRACT_VERSION}')
        baseline = self.baseline(value.get('baseline_id'))
        plan = self.load('blueprint_plans', value.get('plan_version'))
        self.require(plan['baseline_id'] == value['baseline_id'], 'Plan and blueprint baselines conflict')
        self.require(value.get('surface') == baseline['surface'], 'Blueprint surface must match audited scope')
        self.require(set(plan['decision_ids']) <= set(value.get('decision_ids', [])),
                     'Blueprint cannot omit roadmap decisions; record an amendment or include them')
        self.require(value.get('open_decisions') == [] and not self.pending(), 'Unresolved consequential questions block blueprint')
        hashes = self.bound_hashes('blueprint_plans', value['plan_version'])
        hashes.update(self.decisions(value.get('decision_ids'), allow_empty=True))
        features = value.get('features')
        self.require(isinstance(features, list) and features, 'Concrete blueprint features required')
        baseline_ids = set(self.manifest['discovery_audits'][value['baseline_id']]['feature_ids'])
        requirement_ids = {requirement['requirement_id'] for requirement in baseline.get('requirements', [])}
        seen, check_ids = set(), set()
        for feature in features:
            self.require(isinstance(feature, dict) and self.identifier(feature.get('feature_id')), 'Stable blueprint feature ID required')
            key = feature['feature_id']
            self.require(key not in seen, 'Duplicate blueprint feature')
            seen.add(key)
            self.require(self.strings(feature.get('baseline_ids')) and set(feature['baseline_ids']) <= baseline_ids,
                         'Blueprint feature must map actual A/requirement IDs')
            classification = feature.get('classification')
            self.require(classification in {'KEEP','MOVE','MODIFY','REMOVE','ADD'}, 'Explicit change classification required')
            requirements_only = set(feature['baseline_ids']) <= requirement_ids
            self.require(classification != 'ADD' or requirements_only,
                         'ADD must map only to recorded user requirements')
            self.require(baseline['baseline_kind'] != 'existing' or not requirements_only
                         or classification not in {'KEEP', 'MOVE'},
                         'A new feature with only requirement IDs needs ADD or an authorized change to the requirement')
            changes_behavior = classification in {'MODIFY', 'REMOVE'}
            hashes.update(self.decisions(feature.get('decision_ids'), allow_empty=not changes_behavior))
            if changes_behavior:
                decisions = [self.load('conversation', decision_id) for decision_id in feature['decision_ids']]
                for baseline_id in feature['baseline_ids']:
                    authorization = {'baseline_id': baseline_id, 'classification': classification}
                    self.require(any(authorization in decision.get('authorized_changes', []) for decision in decisions),
                                 'Behavior MODIFY/REMOVE requires explicit feature-specific user authorization')
            for field in ('purpose','anatomy','behavior_contract'):
                self.require(self.text(feature.get(field)), f'Feature {key} requires exact {field}')
            self.require(self.strings(feature.get('states')) and self.strings(feature.get('dependencies'), False),
                         'Feature states and dependencies required')
            paths = feature.get('implementation_paths')
            self.require(self.strings(paths), 'Feature needs approved implementation paths')
            for path in paths:
                self.api.owned_path(path)
            self.require(self.strings(feature.get('prerequisite_feature_ids', []), False),
                         'Prerequisite feature IDs must be distinct')
            extras = feature.get('slice_surfaces', [])
            self.require(self.strings(extras, False) and set(extras) <= {'api', 'mcp', 'script'},
                         f'{key}: slice_surfaces must name approved API, MCP or script surfaces')
            extra_contracts = feature.get('surface_contracts', {})
            self.require(isinstance(extra_contracts, dict) and set(extras) <= set(extra_contracts)
                         and all(isinstance(contract, dict) and contract for contract in extra_contracts.values()),
                         f'{key}: each declared surface needs an approved structured contract')
            if self.api.requires_slice_review(self.manifest):
                self.api.sibling_module('slice_guide').validate_surface_contracts(feature)
            surface = feature.get('surface', value['surface'])
            self.require(surface in {'ui', 'backend', 'fullstack'}
                         and (value['surface'] == 'fullstack' or surface == value['surface']),
                         'Blueprint feature surface must fit the initiative surface')
            contract_surfaces = set(extras)
            if surface == 'fullstack':
                contract_surfaces.update(('ui', 'backend'))
            else:
                contract_surfaces.add(surface)
            try:
                surface_contracts.validate_surface_contracts(value.get('surface_contract_version'),
                                                             sorted(contract_surfaces), extra_contracts)
                surface_contracts.validate_state_transitions(value.get('surface_contract_version'),
                                                             feature.get('states'), feature.get('state_transitions'))
                if value.get('surface_contract_version') and 'sequence_contract' in feature:
                    surface_contracts.validate_sequence_contract(feature['sequence_contract'])
            except ValueError as error:
                raise self.api.StateError(f'{key}: {error}') from error
            if surface in {'ui','fullstack'}:
                visual = feature.get('visual_contract', {})
                self.require(isinstance(visual, dict) and isinstance(visual.get('tokens'), dict) and visual['tokens']
                             and self.text(visual.get('responsive')) and self.text(visual.get('accessibility')),
                             'UI features require exact visual tokens, responsive and accessibility contracts')
                cases = visual.get('render_cases')
                self.require(isinstance(cases, list) and cases, 'UI contract needs exact render cases')
                case_ids, states = set(), set()
                for case in cases:
                    self.require(isinstance(case, dict) and self.identifier(case.get('case_id'))
                                 and case['case_id'] not in case_ids and self.text(case.get('route'))
                                 and case['route'].startswith('/') and not case['route'].startswith('//')
                                 and case.get('state') in feature['states'] and self.text(case.get('ready_selector')),
                                 'Unique browser route/state cases and a ready selector are required')
                    viewport = case.get('viewport', {})
                    self.require(isinstance(viewport, dict) and all(type(viewport.get(k)) is int and viewport[k] > 0
                                 for k in ('width','height')), 'Concrete render viewport dimensions required')
                    self.argv(case.get('command'))
                    self.check_browser_setup(case.get('setup', []))
                    if 'interaction_assertions' in case:
                        self.api.sibling_module('visual_integrity').check_interaction_assertions(
                            case['interaction_assertions'])
                    if 'role' in case or 'observations' in case:
                        self.require(self.text(case.get('role')), 'Browser identity observations require a role')
                        self.check_browser_observations(case.get('observations'))
                    case_ids.add(case['case_id']); states.add(case['state'])
                self.require(states == set(feature['states']), 'Render cases must cover every UI state')
            if surface in {'backend','fullstack'}:
                backend = feature.get('backend_contract', {})
                self.require(isinstance(backend, dict) and all(self.text(backend.get(k)) for k in
                             BACKEND_CONTRACT_FIELDS),
                             'Backend features require concrete request/response/auth/side-effect/failure/compatibility contracts')
            checks = feature.get('acceptance_checks')
            self.require(isinstance(checks, list) and checks, 'Concrete acceptance checks required')
            kinds, checked_cases = set(), set()
            for check in checks:
                self.require(isinstance(check, dict) and self.identifier(check.get('check_id'))
                             and check.get('kind') in {'backend','behavior','visual'} and self.text(check.get('expected')),
                             'Acceptance check needs stable ID, kind and exact expected outcome')
                self.require(check['check_id'] not in check_ids, 'Duplicate acceptance check ID')
                self.argv(check.get('command'))
                check_ids.add(check['check_id']); kinds.add(check['kind'])
                if 'case_ids' in check:
                    self.require(check['kind'] == 'visual' and surface != 'backend'
                                 and self.strings(check['case_ids'])
                                 and set(check['case_ids']) <= case_ids,
                                 'Visual check case_ids must name distinct approved render cases')
                if check['kind'] == 'visual':
                    self.require(surface != 'backend', 'Backend-only features cannot have visual checks')
                    measurement = check.get('measurement')
                    self.require(isinstance(measurement, dict) and all(self.text(measurement.get(key))
                                 for key in ('selector', 'property'))
                                 and measurement.get('visibility', 'visible') in ('visible', 'hidden'),
                                 'Each visual check needs a browser selector and computed CSS property')
                    checked_cases.update(check.get('case_ids', case_ids))
            if surface in {'ui','fullstack'}:
                self.require('visual' in kinds and 'behavior' in kinds, 'UI feature needs visual and behavior acceptance checks')
                self.require(checked_cases == case_ids, 'Every render case needs an applicable visual check')
        self.check_blueprint_placeholders(value)
        self.check_fullstack_acceptance(value)
        self.check_backend_acceptance(value)
        self.check_preserved_behavior(baseline, value)
        graph = {f['feature_id']: set(f.get('prerequisite_feature_ids', [])) for f in features}
        unresolved = set(graph)
        while unresolved:
            ready = {feature_id for feature_id in unresolved if not graph[feature_id] - (seen - unresolved)}
            self.require(ready, 'Unknown or circular blueprint feature prerequisite')
            unresolved -= ready
        self.require_applicable_decisions(value)
        keys = set(value['decision_ids']) | {key for feature in features for key in feature['decision_ids']}
        records = [self.load('conversation', key) for key in sorted(keys)]
        protections = self.protected_visual_decisions(records)
        inherited = set()
        for feature in features:
            if feature.get('visual_contract'):
                scope = set(feature['baseline_ids']) | {feature['feature_id']}
                applicable = self.protected_visual_decisions(records, scope)
                inherited.update(item['decision_id'] for item in applicable)
                if applicable:
                    feature['protected_visual_decisions'] = applicable
                else:
                    feature.pop('protected_visual_decisions', None)
            else:
                self.require(not feature.get('protected_visual_decisions'),
                             'Protected visual decisions apply only to UI features')
        self.require(inherited == {item['decision_id'] for item in protections},
                     'Map each protected visual decision to an affected UI feature')
        if any(feature.get('visual_contract') for feature in features):
            value['visual_qa_version'] = 1
        self.require(value.get('version') not in self.manifest.get('blueprints', {}),
                     'Immutable record already exists; use a new version')
        hashes.update(self.blueprint_documents(value, baseline, create=True))
        self.freeze('blueprints', value.get('version'), value, hashes)
        self.write_plan_blueprint_index(value['plan_version'])
        self.manifest['next_action'] = 'Record complete baseline-to-blueprint coverage before approval review'
        self.persist()

    def blueprint_coverage(self, value):
        self.require_ready()
        spec = self.load('blueprints', value.get('version'))
        self.require(value.get('baseline_id') == spec['baseline_id'], 'Coverage baseline conflicts with blueprint')
        hashes = self.baseline_hashes(spec['baseline_id'])
        hashes.update(self.bound_hashes('blueprints', value['version']))
        features = {f['feature_id']:f for f in spec['features']}
        mappings = value.get('mappings')
        self.require(isinstance(mappings, list) and mappings, 'Complete coverage mappings required')
        seen, pairs = set(), set()
        for mapping in mappings:
            self.require(isinstance(mapping, dict), 'Invalid coverage mapping')
            feature = features.get(mapping.get('feature_id'))
            baseline_id = mapping.get('baseline_id')
            self.require(feature and baseline_id in feature['baseline_ids'], 'Unknown baseline/feature coverage mapping')
            pair = (baseline_id, feature['feature_id'])
            self.require(pair not in pairs, 'Duplicate coverage mapping')
            pairs.add(pair); seen.add(baseline_id)
            checks = {c['check_id'] for c in feature['acceptance_checks']}
            self.require(self.strings(mapping.get('acceptance_ids')) and set(mapping['acceptance_ids']) == checks,
                         'Mapping must cover exact feature acceptance IDs')
        self.require(seen == set(self.manifest['discovery_audits'][spec['baseline_id']]['feature_ids']),
                     'Missing baseline feature/requirement coverage')
        self.require(pairs == {(baseline_id, f['feature_id']) for f in features.values() for baseline_id in f['baseline_ids']},
                     'Every blueprint feature/baseline pair needs coverage')
        self.freeze('blueprint_coverage', value['version'], value, hashes)
        self.manifest['next_action'] = 'Show exact blueprint, decision recap and coverage; wait for explicit user approval'
        self.persist()

    def approval_presentation_hashes(self, value, spec):
        recap = value.get('decision_recap_path')
        coverage = self.manifest['blueprint_coverage'][spec['version']]['path']
        required = set(spec['document_paths']) | {p for f in spec['features'] for p in f['spec_paths']}
        self.require(self.text(recap) and recap not in required and recap != coverage,
                     'Approval requires a separate decision recap Markdown file')
        self.markdown([recap])
        self.require(self.strings(value.get('exceptions'), False),
                     'Record the approved exceptions explicitly; use an empty list when there are none')
        shown = value.get('shown_paths')
        self.require(self.strings(shown) and required | {coverage, recap} <= set(shown),
                     'Approval must name the displayed index, feature specs, exact coverage record and decision recap')
        return self.hashes(shown)

    def validate_approval(self, value):
        self.require_ready()
        self.require(value.get('mode') == 'blueprint', 'Blueprint initiatives require blueprint approval mode')
        self.require(value.get('scope') == self.manifest['scope'], 'Approval scope conflicts')
        self.require(value.get('approved_by_user') is True and self.text(value.get('user_approval_note')),
                     'Explicit user approval of exact version required')
        self.require(self.identifier(value.get('decision_id')) and self.identifier(value.get('approved_version')),
                     'Approval decision and version IDs required')
        self.require(value['decision_id'] not in self.manifest.get('approvals', {}) and not any(
            a['approved_version'] == value['approved_version'] for a in self.manifest.get('approvals', {}).values()),
            'Approval/version already used; preserve history')
        self.require(not self.pending(), 'Unanswered consequential questions block approval')
        version = value['approved_version']
        spec = self.load('blueprints', version)
        self.check_blueprint_placeholders(spec)
        self.check_backend_acceptance(spec)
        baseline = self.baseline(spec['baseline_id'])
        if baseline.get('baseline_kind') == 'existing':
            sources = self.manifest['discovery_audits'][spec['baseline_id']]['source_hashes_at_discovery']
            for feature in baseline.get('features', []):
                feature_surface = feature.get('surface', baseline.get('surface'))
                if feature_surface not in {'ui', 'fullstack'}:
                    continue
                self.check_browser_evidence(feature, sources)
        self.check_preserved_behavior(baseline, spec)
        self.check_fullstack_acceptance(spec)
        self.blueprint_documents(spec, baseline)
        for feature in spec['features']:
            self.check_test_sources(feature, feature['implementation_paths'])
        plan = self.load('blueprint_plans', spec['plan_version'])
        _, documents = self.markdown(plan['document_paths'])
        self.check_roadmap(documents)
        self.check_baseline_before_delivery()
        self.require_applicable_decisions(spec)
        self.decisions(sorted(set(spec['decision_ids'] + [d for f in spec['features'] for d in f['decision_ids']])),
                       allow_empty=True)
        hashes = self.bound_hashes('blueprint_coverage', version)
        presentation_hashes = self.approval_presentation_hashes(value, spec)
        self.require(all(hashes[path] == presentation_hashes[path] for path in hashes.keys() & presentation_hashes.keys()),
                     'A displayed file changed after the blueprint or coverage was frozen')
        hashes.update(presentation_hashes)
        audit_id = spec['baseline_id']
        latest_features = set(self.manifest['discovery_audits'][audit_id]['feature_ids'])
        for report_id, report in self.manifest.get('missing_reports', {}).items():
            if report['status'] == 'open':
                self.require(report['discovery_at_report'] != audit_id
                             and set(report['suspected_features']) <= latest_features,
                             f'Missing report {report_id} needs full follow-up discovery and complete coverage')
        self.require(not self.check_records(), 'Blueprint record history is inconsistent')
        if self.api.has_living_review(self.directory) or value.get('visual_review'):
            viewer = self.api.sibling_module('living_review')
            self.require(viewer.checkpoint(self.directory, self.manifest) == value.get('visual_review', {}),
                         'Confirm the displayed blueprint visual_review hashes')
        return {**value, 'baseline_id':audit_id, 'artifact_hashes':hashes, 'visual_cases':[],
                'presentation_evidence_hashes':presentation_hashes,
                'workspace_snapshot': self.workspace_snapshot(),
                'approved_at':self.api.utc_now(), 'blueprint_decision_ids':sorted(set(spec['decision_ids'] +
                    [d for f in spec['features'] for d in f['decision_ids']]))}

    def approval_matches_audit(self, approval, audit_id):
        audit = self.manifest.get('discovery_audits', {}).get(audit_id, {})
        return approval['baseline_id'] == audit_id or audit.get('equivalent_approval_id') == approval['decision_id']

    def check_approval(self, *, allow_questions=False):
        try:
            key = self.manifest.get('active_approval_id')
            if not key:
                self.require(self.manifest['stage'] in {'discover','plan','blueprint'}, 'No active approved blueprint')
                return []
            approval = self.manifest['approvals'][key]
            self.require(approval.get('mode') == 'blueprint' and approval['approved_version'] == self.manifest['approved_version'],
                         'Blueprint approval/version mismatch')
            spec = self.load('blueprints', approval['approved_version'])
            self.check_preserved_behavior(self.baseline(approval['baseline_id'], current=False), spec)
            active_audit = self.manifest.get('active_discovery_id')
            self.require(self.approval_matches_audit(approval, active_audit),
                'Stale baseline audit; approve a blueprint against the current audit')
            self.require_applicable_decisions(spec)
            if approval.get('visual_review') or self.api.has_living_review(self.directory):
                self.api.sibling_module('living_review').checkpoint(self.directory, self.manifest, receipt=approval)
            self.check_hashes(approval['artifact_hashes'])
            expected = self.bound_hashes('blueprint_coverage', approval['approved_version'])
            if 'presentation_evidence_hashes' in approval:
                presentation_hashes = self.approval_presentation_hashes(approval, spec)
                self.require(presentation_hashes == approval['presentation_evidence_hashes'],
                             'Approval presentation hash inventory mismatch')
                expected.update(presentation_hashes)
            self.require(expected == approval['artifact_hashes'], 'Approved blueprint hash inventory mismatch')
            superseded = self.superseded_decisions(self.conversations())
            self.require(not superseded.intersection(approval['blueprint_decision_ids']),
                         'Approved decision was amended; new blueprint approval required')
            self.require(allow_questions or not self.pending(),
                         'Unanswered consequential question blocks implementation')
        except (self.api.StateError, KeyError, TypeError, ValueError, OSError) as error:
            return [str(error)]
        return []

    def slice_packet(self, candidate, dependencies=None, *, allow_partial_checks=False):
        """Resolve a delivery specification from the approved blueprint."""
        key = candidate.get('slice_id')
        self.require(self.identifier(key), 'Stable slice ID required')
        approval_id = self.manifest.get('active_approval_id')
        self.require(approval_id is not None, 'Active blueprint approval required')
        approval = self.manifest['approvals'][approval_id]
        self.require(candidate.get('blueprint_version') == approval['approved_version'], 'Slice must target exact approved blueprint')
        self.require('workspace_snapshot' in approval, 'Approve a new blueprint version with a workspace snapshot before delivery')
        self.require(self.api.validate_scope(candidate.get('scope')) == [] and
                     candidate['scope'] in {'app':{'app','page','feature'}, 'page':{'page','feature'}, 'feature':{'feature'}}[self.manifest['scope']],
                     'Slice scope cannot exceed approved scope')
        self.require(self.text(candidate.get('owner')) and candidate['owner'] != 'unassigned', 'Specific owner required')
        self.require(candidate.get('status') in {'draft','ready'}, 'New slice status must be draft or ready')
        spec = self.load('blueprints', approval['approved_version'])
        self.blueprint_documents(spec, self.baseline(approval['baseline_id'], current=False))
        features = {f['feature_id']:f for f in spec['features']}
        ids = candidate.get('feature_ids')
        self.require(self.strings(ids) and set(ids) <= set(features), 'Slice has unknown or missing approved feature IDs')
        selected = [deepcopy(features[key]) for key in ids]
        checks = {c['check_id'] for f in selected for c in f['acceptance_checks']}
        if allow_partial_checks:
            requested = candidate.get('functional_tests')
            self.require(self.strings(requested) and set(requested) <= checks,
                         'Genuine test plan must name approved acceptance IDs')
            for feature in selected:
                feature['acceptance_checks'] = [check for check in feature['acceptance_checks']
                                                if check['check_id'] in requested]
                self.require(bool(feature['acceptance_checks']),
                             f"Feature {feature['feature_id']} needs at least one assigned acceptance check")
                selected_sources = self.test_sources(feature)
                feature['planned_test_paths'] = [path for path in feature.get('planned_test_paths', [])
                                                 if path in selected_sources]
            checks = set(requested)
        paths = candidate.get('allowed_paths')
        self.require(self.strings(paths), 'Allowed implementation paths required')
        for index, path in enumerate(paths):
            self.api.owned_path(path)
            self.require(any(self.api.ownership_contains(allowed, path) for feature in selected
                             for allowed in feature.get('implementation_paths', [])),
                         f'Slice path is outside approved feature paths: {path}')
            self.require(not any(self.api.ownership_overlaps(path, other) for other in paths[:index]), 'Overlapping paths inside slice')
            for old in self.manifest.get('slices', {}).values() if dependencies is None else ():
                self.require(old['status'] == 'cancelled' or old.get('superseded_by')
                             or not any(self.api.ownership_overlaps(path, p) for p in old['allowed_paths']),
                             'Slice ownership overlaps an existing slice')
        deps = candidate.get('dependencies')
        assignments = self.manifest.get('slices', {})
        available = assignments if dependencies is None else dependencies
        self.require(self.strings(deps, False) and all(dep != key and dep in available for dep in deps),
                     'Unknown/circular slice dependency')
        supplied = set()
        test_ownership = list(paths)
        for dep in deps:
            if dependencies is None:
                assignment = assignments[dep]
                self.require(assignment.get('blueprint_version') == approval['approved_version']
                             and assignment['status'] != 'cancelled', 'Prerequisite slice is stale or cancelled')
                dependency = self.api.artifact_file(self.directory, assignment['contract_file'])
                self.require(self.api.digest_of(dependency) == assignment['contract_sha256'],
                             'Prerequisite slice contract has changed')
                prerequisite = self.api.read_json(dependency)
            else:
                prerequisite = dependencies[dep]
            supplied.update(prerequisite['feature_ids'])
            test_ownership.extend(prerequisite['allowed_paths'])
        for feature in selected:
            self.check_test_sources(feature, test_ownership)
        prerequisites = {required for f in selected for required in f.get('prerequisite_feature_ids', [])}
        self.require(prerequisites <= supplied, 'A slice must depend on slices covering all approved prerequisite features')
        if not allow_partial_checks:
            self.require(self.strings(candidate.get('functional_tests')) and set(candidate['functional_tests']) == checks,
                         'Genuine test plan must cover every selected acceptance ID')
        self.require(self.text(candidate.get('test_plan')) and self.strings(candidate.get('prohibited_deviations')),
                     'Slice requires concrete test plan and prohibited deviations')
        self.check_placeholders({field: candidate[field] for field in ('test_plan', 'prohibited_deviations')},
                                f'slice {key}')
        baseline = self.baseline(approval['baseline_id'], current=False)
        decision_ids = set(spec['decision_ids']) | {d for feature in selected for d in feature['decision_ids']}
        decision_ids.update(d for feature in selected for protection in feature.get('protected_visual_decisions', [])
                            for d in protection.get('exchange_ids', []))
        return {**candidate, 'approval_id':approval_id, 'approval_mode':'blueprint',
            'surface_contract_version':spec.get('surface_contract_version'),
            **({'visual_qa_version': spec['visual_qa_version']} if 'visual_qa_version' in spec else {}),
            'approved_artifact_hashes':approval['artifact_hashes'], 'feature_contracts':selected,
            'decision_records':[self.load('conversation', d) for d in sorted(decision_ids)],
            'baseline_contract':baseline, 'acceptance_checks':sorted(checks)}

    def create_slice(self, candidate):
        self.require(not self.api.requires_slice_review(self.manifest),
                     'Reviewed blueprint assignments require slice-release')
        self.require_ready()
        key = candidate.get('slice_id')
        self.require(self.identifier(key) and key not in self.manifest.get('slices', {}),
                     'New stable slice ID required')
        errors = self.api.check_approval(self.directory, self.manifest) + self.api.check_coverage_records(self.directory, self.manifest)
        errors.extend(self.check_records(recovering=('slices', key)))
        self.require(not errors, 'Cannot create slice: ' + '; '.join(errors))
        self.check_baseline_before_delivery()
        packet = self.slice_packet(candidate)
        paths, deps = packet['allowed_paths'], packet['dependencies']
        for dependency_id in deps:
            dependency = self.manifest['slices'][dependency_id]
            if dependency.get('status') == 'accepted' and dependency.get('visual_integrity_required'):
                self.api.require_visual_integrity(self.directory, self.manifest, dependency_id)
        waiting = [dep for dep in deps if self.manifest['slices'][dep]['status'] != 'accepted']
        packet.update(status='blocked' if waiting else 'ready',
                      blockers=[f'Waiting for accepted dependency: {dep}' for dep in waiting])
        input_hash = sha256(json.dumps(candidate, sort_keys=True).encode('utf-8')).hexdigest()
        relative = f'slices/{key}/contract.json'
        target = self.directory / relative
        self.require(target.resolve() == self.directory.resolve() / relative, 'Slice contract cannot use a redirect')
        events = [json.loads(line) for line in (self.directory / 'decisions.jsonl').read_text().splitlines()]
        creations = [index for index, event in enumerate(events)
                     if event.get('type') == 'blueprint_slice_created' and event.get('slice_id') == key]
        self.require(len(creations) <= 1, 'Interrupted slice has conflicting journal entries')
        if target.exists():
            saved = self.api.read_json(target)
            self.require(saved == packet or saved == {**packet, 'creation_input_sha256': input_hash},
                         'Interrupted slice contract differs from the original input')
        else:
            self.require(not creations, 'Interrupted slice contract is missing; restore its original evidence')
            self.require(not target.parent.exists() or not any(target.parent.iterdir()),
                         'Slice directory contains unrelated evidence')
            self.api.atomic_json(target, {**packet, 'creation_input_sha256': input_hash})
        contract_hash = self.api.digest_of(target)
        retry_cursor = None
        if creations:
            index = creations[0]
            self.require(events[index]['contract_sha256'] == contract_hash,
                         'Interrupted slice contract differs from the append-only journal')
            trailing = events[index + 1:]
            self.require(not trailing or (len(trailing) == 1 and trailing[0].get('type') == 'work_cursor_updated'
                         and trailing[0].get('task', {}).get('task_id') == self.manifest.get('active_work_id')),
                         'Interrupted slice has subsequent journal changes; reconcile before retrying')
            if trailing:
                retry_cursor = trailing[0]['task']
        else:
            self.api.journal(self.directory, {'type':'blueprint_slice_created','slice_id':key,
                                              'contract_sha256':contract_hash})
        self.manifest.setdefault('slices', {})[key] = {'owner':candidate['owner'], 'status':packet['status'],
            'blueprint_version':candidate['blueprint_version'],'allowed_paths':paths,'dependencies':deps,
            'contract_file':relative,'contract_sha256':contract_hash,'blockers':packet['blockers']}
        self.manifest['stage'] = 'sliced'
        self.manifest['next_action'] = 'Implement ready slices against exact blueprint and decision contracts'
        self.api.save_workflow_state(self.directory, self.manifest, slice_retry_cursor=retry_cursor)

    def execute(self, command, expected, **context):
        env = {**os.environ, **context}
        output = subprocess.run(command, cwd=self.directory.parents[2], env=env,
                                capture_output=True, text=True, timeout=120, check=False)
        self.require(output.returncode == 0, f'Approved verification command failed ({output.returncode}): {command!r}')
        try:
            observed = json.loads(output.stdout)
        except ValueError as error:
            raise self.api.StateError(f'Verification command did not emit a JSON observation: {command!r}') from error
        self.require(observed == expected, 'Executed verification output differs from the reviewed observation')

    def capture_render(self, render_case, checks, *, build=None):
        app_url = os.environ.get('P2P_APP_URL')
        browser = os.environ.get('P2P_CHROMIUM') or shutil.which('google-chrome') or shutil.which('chromium')
        self.require(app_url and browser and shutil.which('node'),
                     'Browser verification requires P2P_APP_URL, Chromium and Node.js 22 or later')
        driver = Path(__file__).with_name('blueprint_browser.mjs')
        assertions = render_case.get('interaction_assertions', [])
        payload = {'browser': browser, 'appUrl': app_url, 'renderCase': render_case,
                   'checks': [check['measurement'] for check in checks], 'build': build,
                   'interaction_assertions': assertions}
        result = subprocess.run(['node', str(driver)], input=json.dumps(payload), cwd=self.directory.parents[2],
                                text=True, capture_output=True, timeout=100, check=False)
        self.require(result.returncode == 0, 'Browser verification failed: ' + result.stderr.strip())
        observation = json.loads(result.stdout)
        screenshot = b64decode(observation.pop('screenshot'), validate=True)
        if render_case.get('observations'):
            self.check_capture_identity(render_case['observations'], observation)
        self.require(observation.get('actual') == [check['expected'] for check in checks],
                     'Browser measurements differ from the approved visual criteria')
        outcomes = observation.get('interactionAssertions', [])
        self.require(isinstance(outcomes, list) and len(outcomes) == len(assertions),
                     'Browser did not return every approved interaction assertion')
        for rule, outcome in zip(assertions, outcomes):
            self.require(isinstance(outcome, dict) and
                         {key: value for key, value in outcome.items() if key not in ('actual', 'result')} == rule,
                         'Browser did not return the approved interaction assertion for ' + rule['selector'])
            self.require(outcome.get('result') == 'pass' and outcome.get('actual') == rule['expected'],
                         f"Browser interaction assertion failed for {rule['selector']} ({rule['probe']}): "
                         f"expected {rule['expected']!r}, observed {outcome.get('actual')!r}")
        return observation, screenshot

    def implementation_hashes(self, packet, paths):
        self.require(self.strings(paths), 'Real implemented code within slice ownership required')
        snapshot = self.manifest['approvals'][packet['approval_id']].get('workspace_snapshot')
        legacy_removals = []
        if snapshot is None:
            legacy_removals = [path for feature in packet['feature_contracts']
                               if feature['classification'] == 'REMOVE'
                               for path in feature['implementation_paths']]
        hashes = {}
        for path in paths:
            _, is_tree = self.api.owned_path(path)
            self.require(not is_tree and any(self.api.ownership_contains(allowed, path)
                         for allowed in packet['allowed_paths']), 'Implementation file is outside slice ownership')
            if snapshot is None:
                allow_missing = any(self.api.ownership_contains(allowed, path) for allowed in legacy_removals)
            else:
                allow_missing = path in snapshot
            file = self.api.implementation_file(self.directory, path, allow_missing=allow_missing)
            hashes[path] = self.api.digest_of(file) if file is not None else None
        return hashes

    def verify(self, slice_id, review, *, execute=False):
        errors = []
        frozen = {}
        try:
            assignment = self.manifest['slices'][slice_id]
            packet = self.api.read_json(self.api.artifact_file(self.directory, assignment['contract_file']))
            self.require(self.api.digest_of(self.directory/assignment['contract_file']) == assignment['contract_sha256'],
                         'Changed slice contract sha256')
            report = self.api.read_json(self.directory/'slices'/slice_id/'worker-report.json')
            self.require(review.get('slice_id') == slice_id and self.text(review.get('reviewer'))
                         and review['reviewer'] != assignment['owner'], 'Independent reviewer and exact slice required')
            self.require(self.text(review.get('notes')), 'Concrete review notes required')
            expected = {c['check_id']:c for f in packet['feature_contracts'] for c in f['acceptance_checks']}
            checks = review.get('checks')
            self.require(isinstance(checks,list) and checks, 'Executed checks with real output required')
            seen, hashes, executions = set(), {}, []
            for check in checks:
                self.require(isinstance(check, dict) and check.get('check_id') in expected
                             and check['check_id'] not in seen, 'Unknown/duplicate acceptance check')
                key = check['check_id']; seen.add(key)
                command = self.argv(expected[key].get('command'))
                self.require(check.get('result') == 'PASS' and check.get('command') == command
                             and check.get('expected') == expected[key]['expected']
                             and check.get('observed') == check['expected'], 'Acceptance command or observed result differs from approval')
                relative = check.get('output_file')
                self.require(isinstance(relative, str) and relative.startswith(f'slices/{slice_id}/evidence/'),
                             'Check output must belong to slice evidence')
                hashes.update(self.hashes([relative]))
                output = self.api.read_json(self.api.artifact_file(self.directory, relative))
                self.require(output.get('check_id') == key and output.get('result') == 'PASS'
                             and output.get('expected') == check['expected'] and output.get('actual') == check['observed'],
                             'Saved test/DOM output does not substantiate acceptance check')
                if expected[key].get('state_expectations'):
                    self.require(output.get('states') == expected[key]['state_expectations'],
                                 f'Acceptance check {key} must report every approved backend state and outcome')
                executions.append((command, output))
            self.require(seen == set(expected), 'Missing acceptance-test coverage')
            render_jobs = []
            spec = self.load('blueprints', packet['blueprint_version'])
            features = {
                feature['feature_id']: feature for feature in packet['feature_contracts']
                if feature.get('surface', spec['surface']) in {'ui', 'fullstack'}
            }
            if features:
                self.require(self.text(review.get('specification_limits')),
                             'Record textual-spec verification limits; no approved-B pixel parity exists')
                renders = review.get('rendered_cases')
                self.require(isinstance(renders, list) and renders, 'Actual rendered UI evidence is required')
                observed, screenshot_paths, dom_paths = set(), set(), set()
                for case in renders:
                    self.require(isinstance(case, dict) and case.get('feature_id') in features,
                                 'Unknown rendered feature')
                    feature = features[case['feature_id']]
                    self.require(case.get('state') in feature['states'] and self.text(case.get('route'))
                                 and self.text(case.get('tool'))
                                 and case['tool'] not in {'none','unavailable'}, 'Real browser route/state/tool required')
                    matches = [c for c in feature['visual_contract']['render_cases'] if all(
                        case.get(k) == c.get(k) for k in ('case_id','state','route','viewport','role'))]
                    self.require(len(matches) == 1, 'Render route/state/viewport must match exact approved case')
                    command = self.argv(matches[0].get('command'))
                    self.require(case.get('command') == command, 'Render command differs from approval')
                    pair = (feature['feature_id'],case['case_id'])
                    self.require(pair not in observed, 'Duplicate rendered case')
                    observed.add(pair)
                    for field in ('screenshot','dom_file'):
                        relative = case.get(field)
                        self.require(isinstance(relative, str) and relative.startswith(f'slices/{slice_id}/evidence/'),
                                     'Render evidence must belong to slice')
                        hashes.update(self.hashes([relative]))
                    self.require(case['screenshot'] not in screenshot_paths and case['dom_file'] not in dom_paths
                                 and case['screenshot'] != case['dom_file'], 'Each rendered case needs distinct screenshot and DOM evidence')
                    screenshot_paths.add(case['screenshot']); dom_paths.add(case['dom_file'])
                    dom = self.api.read_json(self.api.artifact_file(self.directory,case['dom_file']))
                    self.require(all(dom.get(k) == case.get(k) for k in ('feature_id','case_id','route','state','viewport','role')),
                                 'DOM/style evidence does not describe this approved rendered case')
                    visual_checks = {
                        check['check_id']: check for check in feature['acceptance_checks']
                        if check['kind'] == 'visual'
                        and ('case_ids' not in check or case['case_id'] in check['case_ids'])
                    }
                    observations = dom.get('checks')
                    self.require(isinstance(observations,list) and len(observations) == len(visual_checks),
                                 'DOM/style evidence must measure every approved visual criterion')
                    measured = set()
                    for observation in observations:
                        key = observation.get('check_id') if isinstance(observation,dict) else None
                        self.require(key in visual_checks and key not in measured and observation.get('result') == 'PASS'
                                     and observation.get('expected') == visual_checks[key]['expected']
                                     and observation.get('actual') == visual_checks[key]['expected'],
                                     'Case-specific DOM/style measurement differs from approved criterion')
                        measured.add(key)
                    viewport = case.get('viewport', {})
                    self.require(self.api.png_dimensions(self.api.artifact_file(self.directory,case['screenshot'])) ==
                                 (viewport.get('width'),viewport.get('height')), 'Render screenshot viewport mismatch')
                    render_jobs.append((matches[0], list(visual_checks.values()), dom))
                self.require(observed == {(f['feature_id'],case['case_id']) for f in features.values() for case in f['visual_contract']['render_cases']},
                             'Rendered evidence must cover every approved UI state')
            self.require(self.strings(review.get('test_source_paths')), 'Actual executed test source paths are required')
            required_sources = {path for feature in packet['feature_contracts'] for path in self.test_sources(feature)}
            self.require(required_sources <= set(review['test_source_paths']),
                         'Review must include the actual approved test sources')
            test_sources = {p:self.api.digest_of(self.api.project_source_file(self.directory.parents[2],p))
                            for p in review['test_source_paths']}
            sources = self.implementation_hashes(packet, report.get('implementation_paths', []))
            self.check_workspace_changes(packet, sources, historical=not execute and assignment['status'] == 'accepted')
            rendered = {}
            if execute:
                for command, observation in executions:
                    self.execute(command, observation)
                snapshot = self.discovery_snapshot([]) if render_jobs else {}
                build = {'fingerprint': self.build_fingerprint(snapshot), 'sources': snapshot}
                with tempfile.TemporaryDirectory() as temporary:
                    for index, (case, checks, reviewed) in enumerate(render_jobs):
                        observation, image = self.capture_render(case, checks, build=build)
                        screenshot = Path(temporary) / 'capture.png'
                        screenshot.write_bytes(image)
                        viewport = case['viewport']
                        self.require(self.api.png_dimensions(screenshot) == (viewport['width'], viewport['height']),
                                     'Fresh render screenshot viewport mismatch')
                        receipt = {**reviewed, 'browser': observation}
                        identity = sha256(image)
                        identity.update(json.dumps(receipt, sort_keys=True).encode('utf-8'))
                        prefix = f'slices/{slice_id}/evidence/verified-render-{index}-{identity.hexdigest()}'
                        self.api.atomic_bytes(self.directory / (prefix + '.png'), image)
                        self.api.atomic_json(self.directory / (prefix + '.json'), receipt)
                        rendered.update(self.hashes([prefix + '.png', prefix + '.json']))
                self.require(not render_jobs or self.discovery_snapshot([]) == snapshot,
                             'Application files changed during browser verification')
                self.check_hashes(hashes)
                for path, expected_hash in test_sources.items():
                    self.require(self.api.digest_of(self.api.project_source_file(self.directory.parents[2], path))
                                 == expected_hash, f'Verification command altered source: {path}')
                self.require(sources == self.implementation_hashes(packet, list(sources)),
                             'Verification command altered implementation files')
                self.check_workspace_changes(packet, sources)
                self.check_hashes(packet['approved_artifact_hashes'])
            else:
                rendered = review.get('executed_render_hashes', {})
                self.require(review.get('executed_commands') == (
                    [command for command, _ in executions] + [case['command'] for case, _, _ in render_jobs])
                    and len(rendered) == 2 * len(render_jobs),
                             'Saved verification requires command execution receipts')
                self.check_hashes(rendered)
            frozen = {**review, 'status':'PASS','warning_case_ids':[], 'reviewed_at':self.api.utc_now(),
                'approval_id':self.manifest['active_approval_id'], 'blueprint_version':packet['blueprint_version'],
                'contract_sha256':assignment['contract_sha256'],'evidence_hashes':hashes,
                'test_source_hashes':test_sources,'implementation_hashes':sources,
                  'executed_commands': [command for command, _ in executions]
                  + [case['command'] for case, _, _ in render_jobs], 'executed_render_hashes': rendered}
        except (self.api.StateError, KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
        return frozen, errors

    def check_saved_verification(self, slice_id, assignment):
        try:
            path = self.directory/'slices'/slice_id/'verification.json'
            self.require(self.api.digest_of(path) == assignment.get('verification_sha256'), 'Saved verification sha256 changed')
            with (self.directory/'decisions.jsonl').open(encoding='utf-8') as log:
                receipts = [event['verification_sha256'] for line in log if (event := json.loads(line)).get('type') == 'slice_verified'
                            and event.get('slice_id') == slice_id and 'verification_sha256' in event]
            self.require(receipts and receipts[-1] == assignment['verification_sha256'],
                         'Saved verification differs from the execution receipt in the journal')
            saved = self.api.read_json(path)
            if assignment.get('superseded_by'):
                self.check_hashes({**saved['evidence_hashes'], **saved.get('executed_render_hashes', {})})
                return []
            self.require(saved.get('status') == 'PASS' and saved.get('approval_id') == self.manifest.get('active_approval_id'),
                         'Saved verification is stale')
            fresh, errors = self.verify(slice_id, saved)
            self.require(not errors, '; '.join(errors))
            self.require(saved['evidence_hashes'] == fresh['evidence_hashes'] and
                         saved['implementation_hashes'] == fresh['implementation_hashes'] and
                         saved.get('test_source_hashes') == fresh.get('test_source_hashes'),
                         'Changed verification evidence, test source or implementation sha256')
        except (self.api.StateError, KeyError, ValueError, OSError) as error:
            return [str(error)]
        return []

    def require_ready(self):
        work = self.manifest.get('work_items', {}).get(self.manifest.get('active_work_id'))
        self.require(not work or work['status'] not in {'waiting_user','blocked'},
                     'Saved task is waiting/blocked; resolve that exact task before progressing')

    def application_files(self, project_root):
        """Treat unfamiliar non-document files as possible application source."""
        skipped = {'.git','.artifacts','node_modules','.venv','venv','__pycache__','.github'}
        documents = {'.md','.rst','.pdf'}
        scaffolding = {'.git','.gitignore','.gitattributes','.editorconfig','package.json','package-lock.json',
                       'yarn.lock','pnpm-lock.yaml','pyproject.toml','requirements.txt','tsconfig.json'}
        local_skill_roots = {project_root / '.agents/skills', project_root / '.codex/skills'}
        found = []
        for current, dirs, files in os.walk(project_root, followlinks=False):
            current = Path(current)
            dirs[:] = [d for d in dirs if d not in skipped and current / d not in local_skill_roots
                       and not (current / d).is_relative_to(self.directory)]
            for name in files:
                path = current/name
                if name == 'package.json' and self.api.read_json(path).get('scripts'):
                    found.append(path)
                elif name not in scaffolding and path.suffix.lower() not in documents:
                    found.append(path)
        return found

    def workspace_snapshot(self):
        root = self.directory.parents[2]
        if self.api.git_workspace_root(root):
            result = subprocess.run(
                ['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                capture_output=True, check=False)
            self.require(result.returncode == 0, 'Cannot inventory workspace files with git ls-files')
            paths = {root / os.fsdecode(path) for path in result.stdout.split(b'\0') if path}
        else:
            paths = set()
            for current, dirs, files in os.walk(root, followlinks=False):
                folder = Path(current)
                dirs[:] = [name for name in dirs
                           if name not in {'.git', '.artifacts', '__pycache__', 'node_modules', '.venv', 'venv'}
                           and folder / name != root / self.api.LEGACY_ARTIFACT_ROOT]
                paths.update(folder / name for name in files)
                paths.update(folder / name for name in dirs if (folder / name).is_symlink())
        snapshot = {}
        for path in sorted(paths):
            if path.is_relative_to(self.directory):
                continue
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            key = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                snapshot[key] = 'link:' + os.readlink(path)
            elif stat.S_ISREG(mode):
                snapshot[key] = f'{stat.S_IMODE(mode):o}:' + self.api.digest_of(path)
            elif stat.S_ISDIR(mode) and self.api.git_workspace_root(path) == path:
                snapshot[key] = list(self.api.git_info(path))
        return snapshot

    def check_workspace_changes(self, packet, reported, *, historical=False):
        approval = self.manifest['approvals'][packet['approval_id']]
        before = approval.get('workspace_snapshot')
        if before is None and historical:
            return
        self.require(before is not None, 'Approve a new blueprint version with a workspace snapshot before delivery')
        after = self.workspace_snapshot()
        changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        assigned_paths = [path for assignment in self.manifest['slices'].values()
                          if assignment.get('blueprint_version') == packet['blueprint_version']
                          and assignment['status'] != 'cancelled'
                          for path in assignment['allowed_paths']]
        outside = sorted(path for path in changed
                         if not any(self.api.ownership_contains(owner, path) for owner in assigned_paths))
        self.require(not outside, 'Workspace changes outside assigned slice paths: ' + ', '.join(outside))
        unreported = sorted(path for path in changed - reported.keys()
                            if any(self.api.ownership_contains(owner, path) for owner in packet['allowed_paths']))
        self.require(not unreported, 'Changed slice paths missing from worker report: ' + ', '.join(unreported))

    def check_baseline_before_delivery(self):
        audit_id = self.manifest.get('active_discovery_id')
        if not audit_id:
            return
        approved_versions = {approval['approved_version']
                             for approval in self.manifest.get('approvals', {}).values()
                             if self.approval_matches_audit(approval, audit_id)}
        active_id = self.manifest.get('active_approval_id')
        assignments = [assignment for assignment in self.manifest.get('slices', {}).values()
                       if active_id and assignment.get('blueprint_version') in approved_versions
                       and assignment['status'] != 'cancelled']
        owned = [path for assignment in assignments if not assignment.get('superseded_by')
                 for path in assignment['allowed_paths']]
        retired = [path for assignment in assignments if assignment.get('superseded_by')
                   for path in assignment['allowed_paths']]
        active = self.manifest.get('approvals', {}).get(self.manifest.get('active_approval_id'), {})
        snapshot = active.get('workspace_snapshot') if retired and active.get('approved_version') in approved_versions else None
        self.current_baseline(self.baseline(audit_id), audit_id, owned, retired, snapshot)

    def current_baseline(self, baseline, audit_id, owned, retired, snapshot):
        root = self.directory.parents[2]
        entry = self.manifest['discovery_audits'][audit_id]
        live = self.workspace_snapshot() if snapshot is not None else {}

        def is_owned(path):
            return any(self.api.ownership_contains(owner, path) for owner in owned)

        def was_owned(path):
            # Reapproval freezes the result of earlier assigned work before releasing its paths.
            return snapshot is not None and any(self.api.ownership_contains(owner, path) for owner in retired)

        if baseline['baseline_kind'] == 'greenfield':
            if not entry.get('approved_before_discovery'):
                for source in self.application_files(root):
                    path = source.relative_to(root).as_posix()
                    self.require(is_owned(path) or (was_owned(path) and path in snapshot
                                                    and live.get(path) == snapshot[path]),
                                 f'Greenfield source appeared or changed outside assigned slice paths: {path}')
        else:
            hashes = entry['source_hashes_at_discovery']
            for path, expected in hashes.items():
                if is_owned(path):
                    continue
                if was_owned(path):
                    self.require(live.get(path) == snapshot.get(path), f'A source changed since reapproval: {path}')
                    continue
                actual = (self.api.digest_of(self.api.project_source_file(root, path)) if expected is not None
                          else None if not (root / path).exists() else 'present')
                self.require(actual == expected, f'A source changed since discovery: {path}')

    def require_applicable_decisions(self, spec):
        baseline_ids = set(self.manifest['discovery_audits'][spec['baseline_id']]['feature_ids'])
        scope = baseline_ids | {f['feature_id'] for f in spec['features']}
        records = self.conversations()
        superseded = self.superseded_decisions(records)
        applicable = set()
        authorizations = {}
        for record in records:
            if (record['status'] != 'decided' or record['exchange_id'] in superseded
                    or record.get('report_id') or record.get('record_type') in {'recap', 'slice_detail', 'slice_consent'}):
                continue
            ids = set(record.get('feature_ids', []) + record.get('requirement_ids', []))
            if not ids or ids & scope:
                applicable.add(record['exchange_id'])
                for change in record.get('authorized_changes', []):
                    baseline_id = change['baseline_id']
                    classification = change['classification']
                    existing = authorizations.setdefault(baseline_id, classification)
                    self.require(existing == classification,
                                 f'Conflicting decisions for {baseline_id}; record an explicit correction')
        bound = set(spec['decision_ids']) | {d for f in spec['features'] for d in f['decision_ids']}
        for feature in spec['features']:
            for baseline_id in feature['baseline_ids']:
                self.require(authorizations.get(baseline_id, feature['classification']) == feature['classification'],
                             f'Blueprint classification conflicts with the active user decision for {baseline_id}')
        self.require(applicable <= bound, 'New or amended material decisions are not incorporated in this blueprint: '
                     + ', '.join(sorted(applicable-bound)))

    def all_accepted(self):
        active = self.manifest.get('active_approval_id')
        if not active:
            return False
        approval = self.manifest['approvals'][active]
        if self.api.requires_slice_review(self.manifest):
            review = self.manifest.get('slice_review', {})
            package = review.get('packages', {}).get(review.get('active_package'), {})
            if (review.get('approval_id') != active or not package.get('authorization_path')
                    or set(package['released']) != set(review.get('selected', {}))
                    or any(self.manifest.get('slices', {}).get(key, {}).get('status') != 'accepted'
                           for key in package['released'])):
                return False
        spec = self.load('blueprints',approval['approved_version'])
        expected = {(f['feature_id'],c['check_id']) for f in spec['features'] for c in f['acceptance_checks']}
        covered = set()
        for assignment in self.manifest.get('slices', {}).values():
            if assignment.get('blueprint_version') != approval['approved_version'] or assignment['status'] == 'cancelled':
                continue
            if assignment['status'] != 'accepted':
                return False
            packet = self.api.read_json(self.api.artifact_file(self.directory,assignment['contract_file']))
            if assignment.get('visual_integrity_required'):
                self.api.require_visual_integrity(self.directory, self.manifest, packet['slice_id'])
            if packet.get('approval_id') != active:
                continue
            self.require(self.api.digest_of(self.directory/assignment['contract_file']) == assignment['contract_sha256'],
                         'Accepted slice contract changed before integration')
            self.require(not self.check_saved_verification(packet['slice_id'],assignment),
                         'Accepted slice verification became stale before integration')
            covered.update((f['feature_id'],c['check_id']) for f in packet['feature_contracts'] for c in f['acceptance_checks'])
        return covered == expected
