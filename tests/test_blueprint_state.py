"""Black-box blueprint contracts; fixtures are evidence, never real user approvals."""
import atexit
import base64
import copy
import hashlib
from functools import cache, partial
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from fixtures.blueprint_browser_probe import ItemsHandler
from fixtures.mermaid_cache import mermaid_cache_environment
from test_project_state import png
from scripts import slice_evidence as SLICE_EVIDENCE_MODULE

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/project_state.py'
FIXTURE_VIEWPORT = {'width': 160, 'height': 160}
BACKEND_OUTCOMES = {'success': '200 JSON array', 'forbidden': '403 forbidden',
                    'error': '500 retryable', 'backend_failure': '403 forbidden; 500 retryable'}
CLI_SPEC = importlib.util.spec_from_file_location('blueprint_fixture_cli', SCRIPT)
FIXTURE_CLI = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(FIXTURE_CLI)
SLICE_REVIEW_SPEC = importlib.util.spec_from_file_location(
    'blueprint_fixture_slice_review', SCRIPT.with_name('slice_review.py'))
SLICE_REVIEW_MODULE = importlib.util.module_from_spec(SLICE_REVIEW_SPEC)
SLICE_REVIEW_SPEC.loader.exec_module(SLICE_REVIEW_MODULE)


@cache
def approved_backend_sample(multiple):
    sample = BlueprintTests()
    sample.setUp()
    atexit.register(sample.doCleanups)
    feature = sample.build_reviewed_blueprint(multiple=multiple)
    return sample.root, feature


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='blueprint-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / 'design/redesigns/sample-work'
        self.env = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(self.root.parent)}
        self.env.pop('P2P_APP_URL', None)

    def test_shared_identity_sweep_catches_same_api_route_and_scopes_tokens_to_route(self):
        packets = {
            'provider': {'feature_contracts': [{'feature_id': 'a',
                'backend_contract': {'request': 'GET /items; no body', 'response': '200 JSON array'},
                'visual_contract': {'tokens': {'gap': '16px'}, 'render_cases': [{'route': '/items', 'ready_selector': '#items'}]}}]},
            'consumer': {'feature_contracts': [{'feature_id': 'b',
                'backend_contract': {'request': 'GET /items; no body', 'response': '200 object'},
                'visual_contract': {'tokens': {'gap': '24px'}, 'render_cases': [{'route': '/items', 'ready_selector': '#items'}]}}]},
            'other-screen': {'feature_contracts': [{'feature_id': 'c',
                'visual_contract': {'tokens': {'gap': '8px'}, 'render_cases': [{'route': '/settings', 'ready_selector': '#settings'}]}}]},
        }
        conflicts = SLICE_REVIEW_MODULE.SliceReview.shared_identity_conflicts(None, packets)
        self.assertIn('GET /items has divergent responses', conflicts)
        self.assertIn('/items component #items token gap has divergent values', conflicts)
        self.assertFalse(any('/settings token gap' in conflict for conflict in conflicts))

    def test_protected_visual_decision_flows_from_exchange_to_reviewed_contract(self):
        protection = {'decision_id': 'items-rounded', 'component': '/items trigger', 'property': 'shape',
                      'requirement': 'Keep the rounded trigger.', 'purpose': 'Identify the primary action.',
                      'verification': 'Inspect the trigger border radius.'}
        self.start(review=True)
        self.exchange(protected_visual_decisions=[protection])
        self.record_baseline(self.baseline('ui'))
        self.record_reviewed_plan(self.plan())
        blueprint = self.blueprint(surface='ui')
        self.cli('blueprint-record', '--blueprint-file', self.payload('blueprint', blueprint))
        doc = self.directory / blueprint['features'][0]['spec_paths'][0]
        self.assertIn('items-rounded', doc.read_text())
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage(surface='ui')))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        candidate = self.guide_candidate(blueprint['features'][0], surfaces=('ui',))
        result = self.review_cli('slice-plan', {'slices': [candidate]}, ok=False)
        self.assertIn('Guide must cite protected visual decision items-rounded', result.stderr)
        guide = self.directory / candidate['guide_path']
        guide.write_text(guide.read_text().replace(
            'Render the items list at', '`items-rounded` — Keep the rounded trigger. Render the items list at', 1))
        self.review_cli('slice-plan', {'slices': [candidate]})
        entry = self.manifest()['slice_review']['selected']['items-slice']
        packet = json.loads((self.directory / entry['contract_path']).read_text())
        self.assertEqual(packet['feature_contracts'][0]['protected_visual_decisions'],
                         [{**protection, 'exchange_ids': ['choose-layout']}])

    def test_v3_dependency_requires_typed_declaration_and_rejects_implicit_provider_path(self):
        feature = self.reviewed_blueprint(multiple=True)
        provider = self.guide_candidate(feature)
        consumer = self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py', deps=('items-slice',))
        del consumer['dependency_contracts']
        missing = self.review_cli('slice-plan', {'slices': [provider, consumer]}, ok=False)
        self.assertIn('declare how every dependency is consumed', missing.stderr)

    def test_v3_dependency_rejects_provider_path_referenced_without_shared_declaration(self):
        feature = self.reviewed_blueprint(multiple=True)
        provider = self.guide_candidate(feature)
        consumer = self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py', deps=())
        guide_path = self.directory / consumer['guide_path']
        guide_path.write_text(guide_path.read_text().replace(
            'The request handler checks the caller before invoking item storage.',
            'The request handler uses `src/service.py` from the provider.'), encoding='utf-8')
        self.review_cli('slice-plan', {'slices': [provider, consumer]})
        self.review_cli('slice-review-record', self.review_input())
        self.review_cli('slice-review-record', self.review_input('adapter-slice'))
        blocked = self.review_cli('slice-collection-validate', self.collection_input(), ok=False)
        checks = json.loads(blocked.stdout)['checks']
        self.assertIn('provider-owned paths referenced without a declared dependency',
                      checks['dependencies']['explanation'])

    def test_ordering_only_dependency_cannot_hide_a_referenced_provider_file(self):
        feature = self.reviewed_blueprint(multiple=True)
        provider = self.guide_candidate(feature)
        consumer = self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py', deps=('items-slice',))
        guide_path = self.directory / consumer['guide_path']
        guide_path.write_text(guide_path.read_text().replace(
            'Read accepted prerequisites: items-slice',
            'Read accepted prerequisites: items-slice; use `src/service.py`.'), encoding='utf-8')
        self.review_cli('slice-plan', {'slices': [provider, consumer]})
        self.review_cli('slice-review-record', self.review_input())
        self.review_cli('slice-review-record', self.review_input('adapter-slice'))
        blocked = self.review_cli('slice-collection-validate', self.collection_input(), ok=False)
        checks = json.loads(blocked.stdout)['checks']
        self.assertIn('need an interface dependency declaration', checks['dependencies']['explanation'])

    def cli(self, command, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), command,
            '--project-root', str(self.root), '--initiative-id', 'sample-work', *map(str, args)],
            capture_output=True, text=True, env=mermaid_cache_environment(self.env))
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn('Traceback', result.stderr)
        return result

    def payload(self, name, value):
        path = self.directory / 'inputs' / (name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def artifact(self, name, content='Observed test evidence'):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return name

    def serve_items(self, page=None):
        if not shutil.which('node') or not (shutil.which('google-chrome') or shutil.which('chromium')):
            self.skipTest('Node.js and Chromium are required for the browser scenario')
        if page is not None:
            source = self.root / 'src/items.html'
            source.parent.mkdir(exist_ok=True)
            source.write_text(page)
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(ItemsHandler, directory=self.root / 'src'))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env['P2P_APP_URL'] = f'http://127.0.0.1:{server.server_port}'

    def serve_fixture_states(self, gap='16px'):
        states = ''.join(f'<div id="state-{state}">{state}</div>'
                         for state in ('loading', 'success', 'forbidden', 'error'))
        self.serve_items(f'<!doctype html><main id="items" style="display:grid;gap:{gap}">{states}</main>')

    def manifest(self):
        return json.loads((self.directory / 'manifest.json').read_text())

    def test_route_free_backend_discovery(self):
        self.start()
        audit = self.baseline()
        audit['routes'] = []
        audit['features'][0].pop('route')
        for sweep in audit['sweeps']:
            sweep['routes_checked'] = []
        self.record_baseline(audit)

    def test_greenfield_ignores_git_metadata_pointer_and_workspace_skills(self):
        metadata = self.root / 'checkout'
        metadata.mkdir()
        self.root = metadata
        subprocess.run(['git', 'init', '-q', '--separate-git-dir',
                        str(metadata.parent / 'git-metadata'), str(metadata)], check=True)
        self.directory = metadata / '.artifacts/prototype-to-production/sample-work'
        skill = metadata / '.agents/skills/example'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('# Workspace skill\n')
        (skill / 'helper.py').write_text('print("Skill helper")\n')
        self.start()
        self.exchange(feature_ids=[], requirement_ids=['list-items'])
        self.record_baseline({
            'audit_id': 'audit-v1', 'scope': 'feature', 'baseline_kind': 'greenfield',
            'surface': 'backend', 'inspected_paths': ['.'], 'features': [], 'unresolved': [],
            'requirements': [{'requirement_id': 'list-items', 'text': 'List items',
                              'decision_ids': ['choose-layout']}],
            'evidence_paths': [self.artifact('discovery/empty.txt', 'Git initialized; no application source.')],
        })

    def test_incomplete_roadmap_cannot_be_frozen(self):
        self.start()
        self.exchange()
        self.record_baseline()
        plan = self.plan()
        self.artifact(plan['document_paths'][0], '# Roadmap\nNo roadmap details.')
        self.cli('plan-record', '--plan-file', self.payload('plan', plan), ok=False)

    def test_roadmap_links_to_all_blueprints_recorded_against_its_plan(self):
        self.start()
        self.exchange()
        self.record_baseline()
        plan = self.plan()
        roadmap = self.roadmap()
        without_link = roadmap.replace('[Detailed blueprint specifications](blueprints/index.md)\n\n', '')
        self.artifact(plan['document_paths'][0], without_link)
        self.cli('plan-record', '--plan-file', self.payload('plan', plan), ok=False)
        self.artifact(plan['document_paths'][0], roadmap)
        self.record_reviewed_plan(plan)

        first = self.blueprint('spec-v1')
        self.cli('blueprint-record', '--blueprint-file', self.payload('blueprint-first', first))
        second = self.blueprint('spec-v2')
        second['features'][0]['spec_paths'] = ['blueprints/spec-v2/items.md']
        second['document_paths'] = ['blueprints/spec-v2/index.md']
        self.cli('blueprint-record', '--blueprint-file', self.payload('blueprint-second', second))

        index = self.directory / 'plans/plan-v1/blueprints/index.md'
        self.assertEqual(index.read_text(),
            '# Blueprints for plan-v1\n\n'
            '- [spec-v1](../../../blueprints/spec-v1/index.md)\n'
            '- [spec-v2](../../../blueprints/spec-v2/index.md)\n')

    def test_keep_cannot_discard_audited_authorization(self):
        self.start()
        self.exchange()
        audit = self.baseline()
        spec = self.blueprint()
        feature = spec['features'][0]
        audit['behavior_contracts'] = {'list-items': {
            'behavior_contract': feature['behavior_contract'],
            'backend_contract': copy.deepcopy(feature['backend_contract']),
        }}
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan())
        feature['backend_contract']['authorization'] = 'Public; remove membership checks.'
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)

    def test_keep_cannot_cover_fullstack_behavior_with_only_ui(self):
        self.start()
        self.exchange()
        self.record_baseline(dict(self.baseline('ui'), surface='fullstack'))
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint(surface='ui')
        spec['surface'] = 'fullstack'
        spec['features'][0]['surface'] = 'ui'
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)

    def test_cancelled_assignment_does_not_allow_stale_audit_for_replacement(self):
        self.assigned()
        self.cli('slice-cancel', '--slice-id', 'items-slice', '--reason', 'Cancelled before implementation')
        (self.root / 'src/service.py').write_text('def list_items(): return [1]\n')
        self.cli('validate', ok=False)
        replacement = dict(self.slice(), slice_id='replacement')
        self.cli('slice-create', '--contract-file', self.payload('replacement', replacement), ok=False)

    def test_waiting_cursor_can_follow_recorded_discovery_stage_transition(self):
        self.start()
        cursor = {'task_id': 'discuss-access', 'phase': 'discover', 'status': 'in_progress',
                  'summary': 'Investigate access rules', 'next_action': 'Confirm current behavior',
                  'completed_steps': [], 'pending_steps': ['Confirm current behavior', 'Record user choice'],
                  'blockers': [], 'code_paths': [], 'artifact_paths': []}
        self.cli('work-update', '--work-file', self.payload('cursor', cursor))
        self.record_baseline()
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['active_work']['phase'], 'plan')
        self.assertEqual(resumed['active_work']['next_action'], resumed['next_action'])
        self.assertEqual(resumed['active_work']['pending_steps'], cursor['pending_steps'])
        self.cli('validate')
        question = self.exchange(status='open', response=None)
        cursor.update(phase='plan', status='waiting_user', pending_question_id=question['exchange_id'],
                      completed_steps=['Confirm current behavior'], pending_steps=['Record user choice'],
                      next_action='Await the saved access decision')
        self.cli('work-update', '--work-file', self.payload('cursor', cursor))
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['active_work']['phase'], 'plan')
        self.assertEqual(resumed['pending_questions'][0]['question'], question['question'])
        self.cli('validate')

    def test_resume_and_validate_detect_missing_or_changed_roadmap_index(self):
        self.prepared()
        index = self.directory / 'plans/plan-v1/blueprints/index.md'
        original = index.read_bytes()
        for contents in (None, b'# Replaced index\n'):
            with self.subTest(contents=contents):
                if contents is None:
                    index.unlink()
                else:
                    index.write_bytes(contents)
                result = self.cli('validate', ok=False)
                self.assertIn('plans/plan-v1/blueprints/index.md', result.stderr)
                resume = json.loads(self.cli('resume', '--format', 'json', ok=False).stdout)
                self.assertFalse(resume['safe_to_continue'])
        self.cli('blueprint-index-rebuild', '--plan-version', 'plan-v1')
        self.assertEqual(index.read_bytes(), original)
        self.cli('validate')

    def test_roadmap_has_a_working_index_before_any_blueprint_exists(self):
        self.start()
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        index = self.directory / 'plans/plan-v1/blueprints/index.md'
        self.assertIn('No blueprints have been recorded', index.read_text())
        self.cli('validate')
        index.unlink()
        self.cli('validate', ok=False)
        self.cli('blueprint-index-rebuild', '--plan-version', 'plan-v1')
        self.cli('validate')

    def test_resume_flags_changed_audit_source_outside_active_slice(self):
        self.start()
        self.exchange()
        audit = self.baseline()
        source = self.root / 'src/policy.py'
        source.write_text('ALLOW_ALL = False\n')
        for sweep in audit['sweeps']:
            sweep['source_files_checked'].append('src/policy.py')
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan())
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', self.blueprint()))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage()))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()))
        owned = self.root / 'src/service.py'
        owned.write_text(owned.read_text() + '\n# In-scope implementation\n')
        self.cli('validate')
        source.write_text('ALLOW_ALL = True\n')
        result = self.cli('validate', ok=False)
        self.assertIn('src/policy.py', result.stderr)
        resumed = json.loads(self.cli('resume', '--format', 'json', ok=False).stdout)
        self.assertFalse(resumed['safe_to_continue'])

    def test_interrupted_slice_creation_recovers_same_contract_and_cursor(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        cursor = {'task_id': 'deliver-items', 'phase': 'approved', 'status': 'in_progress',
                  'summary': 'Assign the items slice', 'next_action': 'Create slice',
                  'completed_steps': [], 'pending_steps': ['Create slice'], 'blockers': [],
                  'code_paths': [], 'artifact_paths': []}
        self.cli('work-update', '--work-file', self.payload('cursor', cursor))
        manifest_path = self.directory / 'manifest.json'
        before = manifest_path.read_bytes()
        packet = self.slice()
        candidate = self.payload('slice', packet)
        self.cli('slice-create', '--contract-file', candidate)
        journal = self.directory / 'decisions.jsonl'
        journal_after = journal.read_bytes()
        manifest_path.write_bytes(before)
        self.cli('validate', ok=False)
        changed = self.payload('changed-slice', dict(packet, status='ready'))
        self.cli('slice-create', '--contract-file', changed, ok=False)
        self.cli('slice-create', '--contract-file', candidate)
        self.assertEqual(journal.read_bytes(), journal_after)
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['active_work']['phase'], 'sliced')
        self.assertEqual(resumed['active_work']['pending_steps'], cursor['pending_steps'])

    def test_unsupported_manifest_schema_is_rejected(self):
        self.start()
        path = self.directory / 'manifest.json'
        manifest = self.manifest()
        path.write_text(json.dumps(dict(manifest, schema_version=999)))
        result = self.cli('validate', ok=False)
        self.assertIn('schema version', result.stderr.lower())

    def test_open_question_requires_scope_and_basis(self):
        self.start()
        question = {'exchange_id': 'review-access', 'phase': 'discover', 'status': 'open',
                    'question': 'Should we keep the approval rule?', 'response': None}
        self.cli('conversation-record', '--exchange-file', self.payload('question', question), ok=False)
        question['feature_ids'] = ['list-items']
        self.cli('conversation-record', '--exchange-file', self.payload('question', question), ok=False)
        self.baseline()
        question['source_paths'] = ['src/service.py']
        self.cli('conversation-record', '--exchange-file', self.payload('question', question))
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertIn('src/service.py', resumed['pending_questions'][0]['source_hashes_at_record'])

    def test_verification_rejects_unreported_out_of_scope_change(self):
        self.assigned()
        (self.root / 'src/outside.py').write_text('outside_scope = True\n')
        review = self.review()
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', review), ok=False)
        self.assertIn('src/outside.py', result.stderr)

    def test_independent_render_rejects_failed_approved_interaction_assertions(self):
        self.serve_items('<main id="ready">Items</main>')
        self.directory.mkdir(parents=True)
        state = FIXTURE_CLI.blueprint_state(self.directory, {})
        case = {
            'route': '/items', 'viewport': {'width': 800, 'height': 600},
            'ready_selector': '#ready',
            'interaction_assertions': [{'selector': '#missing', 'probe': 'visible', 'expected': True}],
        }
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(FIXTURE_CLI.StateError, r'interaction assertion.*#missing'):
                state.capture_render(case, [])

    def test_browser_verifies_approved_spacing_and_preserved_interaction(self):
        browser = shutil.which('google-chrome') or shutil.which('chromium')
        if not browser:
            self.skipTest('Chrome or Chromium is required for the browser scenario')
        self.start()
        self.exchange()
        audit = self.baseline('ui')
        page = self.root / 'src/items.html'
        page.write_text("""<!doctype html><style>#items{display:grid;gap:12px}</style>
<button id="toggle">Show details</button><div id="items" data-role="member" data-state="success"><span>Item</span><span hidden id="detail">Details</span></div>
<output id="observation"></output>
<script>
const detail = document.getElementById('detail');
const items = document.getElementById('items');
const toggle = document.getElementById('toggle');
toggle.onclick = () => detail.hidden = !detail.hidden;
toggle.click();
document.getElementById('observation').textContent = JSON.stringify({
    gap: getComputedStyle(items).gap, detail: detail.hidden ? 'hidden' : 'visible', route: location.pathname
});
</script>
""")
        self.serve_items()
        runner = self.root / 'tests/browser_probe.py'
        runner.parent.mkdir()
        shutil.copyfile(Path(__file__).parent / 'fixtures/blueprint_browser_probe.py', runner)

        def command(kind):
            return [sys.executable, 'tests/browser_probe.py', browser, kind]

        def observe(kind, **env):
            return subprocess.check_output(command(kind), cwd=self.root, text=True,
                                           env={**os.environ, **env}, timeout=40)

        original_png = self.directory / 'discovery/evidence/a.png'
        original = json.loads(observe('render', P2P_SCREENSHOT=str(original_png)))
        self.assertEqual(original['checks'][0]['actual'], '12px')
        self.assertEqual(json.loads(observe('toggle'))['actual'], 'visible')
        behavior = 'Click the button to reveal the item details.'
        capture = self.discovery_case('initial-ui-capture')
        capture.update(viewport={'width': 800, 'height': 600}, ready_selector='#detail:not([hidden])')
        for observation in capture['observations'].values():
            observation['selector'] = '#items'
        self.cli('discovery-capture', '--capture-file', self.payload('capture', capture))
        screenshot = self.manifest()['browser_captures'][capture['capture_id']]['screenshot']
        audit['features'][0].update(behavior=behavior, states=['success'],
            source_anchors=[{'path': 'src/items.html', 'contains': 'toggle.onclick'}],
            browser_evidence=[screenshot, 'discovery/evidence/a.png'])
        audit['behavior_contracts']['list-items'] = {'behavior_contract': behavior}
        for sweep in audit['sweeps']:
            sweep['source_files_checked'] = ['src/items.html']
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint(surface='ui')
        feature = spec['features'][0]
        feature.update(behavior_contract=behavior, states=['success'], implementation_paths=['src/items.html'])
        feature['visual_contract']['render_cases'] = [{
            'case_id': 'success-case', 'route': '/items', 'state': 'success',
            'viewport': {'width': 800, 'height': 600},
            'ready_selector': '#detail:not([hidden])', 'command': [sys.executable, '-c', 'pass'],
            'interaction_assertions': [{'selector': '#detail', 'probe': 'text', 'expected': 'Details'}],
        }]
        feature['acceptance_checks'] = [
            {'check_id': 'gap', 'kind': 'visual', 'expected': '16px', 'command': command('gap'),
             'measurement': {'selector': '#items', 'property': 'gap'}},
            {'check_id': 'toggle', 'kind': 'behavior', 'expected': 'visible', 'command': command('toggle')},
        ]
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        coverage = self.coverage(surface='ui')
        coverage['mappings'][0]['acceptance_ids'] = ['gap', 'toggle']
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        packet = dict(self.slice(), allowed_paths=['src/items.html'], functional_tests=['gap', 'toggle'])
        self.cli('slice-create', '--contract-file', self.payload('slice', packet))

        page.write_text(page.read_text().replace('gap:12px', 'gap:16px'))
        report = {'slice_id': 'items-slice', 'owner': 'builder', 'status': 'submitted',
                  'summary': 'Adjust grid spacing', 'implementation_paths': ['src/items.html'], 'shared_ui_paths': []}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('report', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        prefix = 'slices/items-slice/evidence/'
        screenshot = self.directory / (prefix + 'reviewed.png')
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        dom = observe('render', P2P_SCREENSHOT=str(screenshot))
        render = {key: value for key, value in json.loads(dom).items() if key != 'checks'}
        render.update(command=feature['visual_contract']['render_cases'][0]['command'], tool='Chrome',
                      dom_file=self.artifact(prefix + 'dom.json', dom), screenshot=prefix + 'reviewed.png')
        checks = []
        for name in ('gap', 'toggle'):
            output = observe(name)
            measured = json.loads(output)
            self.assertEqual(measured['result'], 'PASS')
            checks.append(dict(check_id=name, command=command(name), result='PASS',
                               expected=measured['expected'], observed=measured['actual'],
                               output_file=self.artifact(prefix + name + '.json', output)))
        review = {'slice_id': 'items-slice', 'reviewer': 'browser-reviewer', 'notes': 'Measured the actual browser output.',
                  'checks': checks, 'rendered_cases': [render], 'test_source_paths': ['tests/browser_probe.py'],
                  'specification_limits': 'Measured grid spacing and button interaction.'}
        self.visual_integrity_attempt(review)
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        self.cli('validate')
        self.assertEqual(self.manifest()['stage'], 'integrate')
        self.assertEqual(len(list(screenshot.parent.glob('verified-render-*.png'))), 1)
        receipt = json.loads(next(screenshot.parent.glob('verified-render-*.json')).read_text())
        self.assertEqual(receipt['browser']['interactionAssertions'], [
            {'selector': '#detail', 'probe': 'text', 'expected': 'Details', 'actual': 'Details', 'result': 'pass'},
        ])

    def test_new_mode_defaults_to_blueprint_and_cannot_be_changed(self):
        self.cli('init', '--scope', 'feature')
        self.assertEqual(self.manifest()['initiative_mode'], 'blueprint')
        self.cli('init', '--scope', 'feature', '--mode', 'prototype', ok=False)
        self.assertEqual(self.manifest()['initiative_mode'], 'blueprint')

    def test_explicit_prototype_and_missing_mode_preserve_legacy_semantics(self):
        self.cli('init', '--scope', 'feature', '--mode', 'prototype')
        manifest = self.manifest()
        self.assertEqual(manifest['initiative_mode'], 'prototype')
        del manifest['initiative_mode']
        (self.directory / 'manifest.json').write_text(json.dumps(manifest))
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['initiative_mode'], 'prototype')
        self.cli('init', '--scope', 'feature')
        self.assertNotIn('initiative_mode', self.manifest())

    def start(self, *, review=False, guided=False, plan_review=False):
        args = ['--guided-design-review'] if guided else []
        self.cli('init', '--scope', 'feature', *args)
        manifest = self.manifest()
        # Existing workflow fixtures predate the separate intake checkpoint.
        manifest.pop('initial_alignment_required', None)
        manifest.pop('requirement_traceability_required', None)
        if not plan_review:
            manifest.pop('plan_review_required', None)
        if not review:
            # Reproduce a blueprint manifest written before conversational review existed.
            manifest.pop('slice_review_required', None)
            manifest.pop('visual_baselines_required', None)
            manifest['schema_version'] = 4
        (self.directory / 'manifest.json').write_text(json.dumps(manifest))


    def exchange(self, exchange_id='choose-layout', status='decided', **changes):
        value = {'exchange_id': exchange_id, 'phase': 'discover',
            'question': 'Keep the existing authorization and response shape?',
            'options': [{'label': 'Keep', 'tradeoff': 'Compatible with current clients'}],
            'recommendation': 'Keep', 'status': status,
            'response': 'Yes, keep both.' if status == 'decided' else None,
            'feature_ids': ['list-items'], 'requirement_ids': [],
            'source_paths': [], 'illustration_paths': [],
            'basis': 'Review the user brief about compatibility with current clients.'}
        value.update(changes)
        self.cli('conversation-record', '--exchange-file', self.payload(exchange_id, value))
        return value

    def baseline(self, surface='backend', audit_id='audit-v1'):
        path = self.root / 'src/service.py'
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            'def list_items(): return []\n'
            'def handle_request(role):\n'
            '    if role != "member": return 403, None\n'
            '    try: return 200, list_items()\n'
            '    except OSError: return 500, {"retryable": True}\n')
        evidence = self.artifact('discovery/evidence/observed.txt', 'GET /items: 200 []')
        feature = {'feature_id':'list-items', 'route':'/items', 'behavior':'List authorized items',
            'roles':['member'], 'states':['success','forbidden'],
            'source_anchors':[{'path':'src/service.py','contains':'def list_items'}]}
        if surface in {'ui', 'fullstack'}:
            capture = self.artifact('discovery/evidence/observed.png', png(24, 24))
            feature['browser_evidence'] = [evidence, capture]
        else:
            feature['evidence_paths'] = [evidence]
        audit = {'audit_id':audit_id, 'scope':'feature', 'baseline_kind':'existing',
            'surface':surface, 'routes':['/items'], 'features':[feature], 'unresolved':[],
            'behavior_contracts': {'list-items': {
                'behavior_contract': 'GET /items checks membership before reading items',
                'backend_contract': self.backend_contract(),
            }},
            'sweeps':[{'sweep_id':'initial','routes_checked':['/items'],
                'source_files_checked':['src/service.py'], 'new_feature_ids':['list-items']},
                {'sweep_id':'final','routes_checked':['/items'],
                'source_files_checked':['src/service.py'], 'new_feature_ids':[]}]}
        return audit

    def record_baseline(self, value=None):
        value = value or self.baseline()
        for feature in value.get('features', []):
            if feature.get('surface', value['surface']) not in {'ui', 'fullstack'}:
                continue
            registered = {entry['screenshot'] for entry in self.manifest().get('browser_captures', {}).values()}
            evidence = feature.setdefault('browser_evidence', [])
            if not registered.intersection(evidence):
                for state in feature['states']:
                    evidence.append(self.fixture_browser_capture(feature, state))
        if self.manifest().get('visual_baselines_required'):
            features = [feature for feature in value.get('features', [])
                        if feature.get('surface', value['surface']) in {'ui', 'fullstack'}]
            if features:
                feature = features[0]
                baseline_id = 'fixture-original-' + value['audit_id']
                route = feature['route']
                gap = {'baseline_id': baseline_id, 'baseline_feature_ids': [feature['feature_id']],
                       'reason': 'Exercise workflow with a fixture evidence gap', 'essential': False,
                       'unavailable_reason': 'The full visual fixture is outside the scope of this test',
                       'source_paths': [anchor['path'] for anchor in feature['source_anchors']],
                       'state': {'route': route, 'theme': 'light', 'data': 'fixture', 'interaction': 'default',
                                 'active_control': None, 'open_menus': []},
                       'renderCase': {'route': route, 'viewport': FIXTURE_VIEWPORT, 'ready_selector': '#fixture'}}
                self.cli('visual-baseline', '--action', 'gap', '--input-file', self.payload(baseline_id, gap))
                value['visual_baseline_ids'] = [baseline_id]
        self.cli('discovery-record','--audit-file',self.payload('audit',value))
        return value

    def fixture_browser_capture(self, feature, state):
        manifest = self.manifest()
        capture_id = f"fixture-capture-{len(manifest.get('browser_captures', {})) + 1}"
        capture = self.discovery_case(capture_id, role=feature['roles'][0], state=state)
        capture.update(feature_id=feature['feature_id'], route=feature['route'],
                       viewport={'width': 24, 'height': 24}, ready_selector='#fixture')
        state_manager = FIXTURE_CLI.blueprint_state(self.directory, manifest)
        observation = {'url': f"http://fixture.invalid{feature['route']}", 'stateVisible': True,
                       'viewport': capture['viewport'],
                       'identities': {'role': feature['roles'][0], 'state': state},
                       'buildEvidence': [{'source': feature['source_anchors'][0]['path']}]}
        with patch.object(state_manager, 'capture_render', return_value=(observation, png(24, 24))), patch('builtins.print'):
            state_manager.discovery_capture(capture)
        return self.manifest()['browser_captures'][capture_id]['screenshot']

    def test_backend_discovery_uses_real_api_evidence_without_browser(self):
        self.start()
        self.record_baseline()
        self.assertEqual(self.manifest()['stage'], 'plan')
        self.assertEqual(self.manifest()['discovery_audits']['audit-v1']['feature_ids'], ['list-items'])
        self.cli('validate')

    def test_ui_discovery_requires_a_decodable_browser_capture(self):
        self.start()
        audit = self.baseline('ui')
        audit['features'][0]['browser_evidence'] = [audit['features'][0]['browser_evidence'][0]]
        rejected = self.cli('discovery-record', '--audit-file', self.payload('audit', audit), ok=False)
        self.assertIn('PNG', rejected.stderr)

        audit['features'][0]['browser_evidence'].append(
            self.artifact('discovery/evidence/fake.png', b'\x89PNG\r\n\x1a\nnot a PNG'))
        rejected = self.cli('discovery-record', '--audit-file', self.payload('audit-invalid-png', audit), ok=False)
        self.assertIn('PNG', rejected.stderr)

        audit['features'][0]['browser_evidence'].pop()
        audit['features'][0]['browser_evidence'].append(
            self.artifact('discovery/evidence/observed.png', png(24, 24)))
        rejected = self.cli('discovery-record', '--audit-file', self.payload('audit-with-capture', audit), ok=False)
        self.assertIn('browser capture receipt', rejected.stderr.lower())

    def test_fullstack_discovery_requires_a_browser_capture(self):
        self.start()
        audit = self.baseline('fullstack')
        audit['features'][0]['browser_evidence'] = [audit['features'][0]['browser_evidence'][0]]
        rejected = self.cli('discovery-record', '--audit-file', self.payload('audit', audit), ok=False)
        self.assertIn('PNG', rejected.stderr)

    def test_discovery_capture_rejects_a_host_command_before_it_can_edit_source(self):
        self.start()
        self.serve_items('<div id="state">Items</div>')
        page = self.root / 'src/items.html'
        original = page.read_bytes()
        capture = self.discovery_case()
        capture['command'] = [sys.executable, '-c',
                              'from pathlib import Path; Path("src/items.html").write_text("Changed")']
        result = self.cli('discovery-capture', '--capture-file', self.payload('capture', capture), ok=False)
        self.assertIn('host command', result.stderr)
        self.assertEqual(page.read_bytes(), original)
        self.assertFalse(self.manifest().get('browser_captures'))

    def test_discovery_rejects_receipts_for_the_wrong_role_or_state(self):
        self.start()
        self.serve_items('<div id="state" data-role="member" data-state="success">Items</div>')
        for field, value in (('role', 'guest'), ('state', 'forbidden')):
            with self.subTest(field=field):
                capture = self.discovery_case(f'wrong-{field}', **{field: value})
                result = self.cli('discovery-capture', '--capture-file', self.payload('capture', capture), ok=False)
                self.assertIn(f'observed {field}', result.stderr)
        self.assertFalse(self.manifest().get('browser_captures'))

    def test_audit_rejects_one_observed_identity_under_distinct_labels(self):
        self.start()
        audit = self.baseline('ui')
        self.serve_items('<div id="state" data-role="member" data-state="success">Items</div>')
        original = self.discovery_case('member-success')
        first = self.record_browser_capture(original)
        relabeled = dict(original, capture_id='guest-forbidden', role='guest', state='forbidden')
        second = self.record_browser_capture(relabeled)
        audit['features'][0].update(roles=['member', 'guest'], browser_evidence=[first, second])
        result = self.cli('discovery-record', '--audit-file', self.payload('audit', audit), ok=False)
        self.assertIn('same observed role for different labels', result.stderr)

    def test_discovery_rejects_source_changes_during_browser_setup(self):
        self.start()
        self.baseline()
        capture = self.discovery_case()
        capture['viewport'] = {'width': 24, 'height': 24}
        state_manager = FIXTURE_CLI.blueprint_state(self.directory, self.manifest())

        def observe(case, checks, *, build):
            source = self.root / 'src/service.py'
            source.write_text(source.read_text() + '\n# Edited during capture\n')
            return {'identities': {'role': 'member', 'state': 'success'}}, png(24, 24)

        with patch.object(state_manager, 'capture_render', side_effect=observe):
            with self.assertRaisesRegex(FIXTURE_CLI.StateError, 'changed during discovery capture: src/service.py'):
                state_manager.discovery_capture(capture)
        self.assertFalse(self.manifest().get('browser_captures'))

    def test_followup_audit_requires_current_captures_for_changed_source(self):
        self.start()
        audit = self.baseline('ui')
        page = self.root / 'src/items.html'
        original = ('<button id="reveal">View items</button>'
                    '<div id="state" data-role="guest" data-state="loading">Items</div>'
                    '<script>document.querySelector("#reveal").onclick = () => {'
                    'const panel = document.querySelector("#state");'
                    'panel.dataset.role = "member"; panel.dataset.state = "success";'
                    '};</script>')
        self.serve_items(original)
        feature = audit['features'][0]
        feature.update(states=['success'], source_anchors=[{'path': 'src/items.html', 'contains': 'id="state"'}])
        for sweep in audit['sweeps']:
            sweep['source_files_checked'] = ['src/items.html']
        capture = self.discovery_case('original-capture')
        capture['setup'] = [{'action': 'click', 'selector': '#reveal'}]
        first = self.record_browser_capture(capture)
        feature['browser_evidence'] = [first]
        self.cli('discovery-record', '--audit-file', self.payload('audit', audit))
        page.write_text(original.replace('>Items<', '>Changed items<'))
        audit.update(audit_id='audit-v2', sweeps=[audit['sweeps'][-1]])
        rejected = self.cli('discovery-record', '--audit-file', self.payload('audit', audit), ok=False)
        self.assertIn('stale browser capture', rejected.stderr.lower())
        feature['browser_evidence'].append(self.record_browser_capture(dict(capture, capture_id='updated-capture')))
        self.cli('discovery-record', '--audit-file', self.payload('audit', audit))
        self.cli('validate')

    def test_discovery_rejects_fresh_capture_of_a_stale_server_response(self):
        self.start()
        old = '<div id="state" data-role="member" data-state="success">Old app</div>'
        if not shutil.which('node') or not (shutil.which('google-chrome') or shutil.which('chromium')):
            self.skipTest('Node.js and Chromium are required for the browser scenario')
        parent = self.root / 'src'
        parent.mkdir()
        source = parent / 'items.html'
        source.write_text(old)
        class CachedItemsHandler(ItemsHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(old.encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), CachedItemsHandler)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env['P2P_APP_URL'] = f'http://127.0.0.1:{server.server_port}'
        self.record_browser_capture(self.discovery_case('old-build'))
        source.write_text(old.replace('Old app', 'New app'))
        p = self.payload('stale', self.discovery_case('new-capture'))
        result = self.cli('discovery-capture', '--capture-file', p, ok=False)
        self.assertIn('served build', result.stderr)

    def test_dynamic_discovery_requires_the_fingerprint_from_the_running_build(self):
        self.start()
        if not shutil.which('node') or not (shutil.which('google-chrome') or shutil.which('chromium')):
            self.skipTest('Node.js and Chromium are required for the browser scenario')
        audit = self.baseline()
        fingerprint = self.cli('browser-build-fingerprint').stdout.strip()

        class BuildHandler(ItemsHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('X-P2P-Build-Fingerprint', fingerprint)
                self.end_headers()
                self.wfile.write(b'<div id="state" data-role="member" data-state="success">Generated</div>')

        server = ThreadingHTTPServer(('127.0.0.1', 0), BuildHandler)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env['P2P_APP_URL'] = f'http://127.0.0.1:{server.server_port}'
        self.record_browser_capture(self.discovery_case('original-build'))
        source = self.root / audit['features'][0]['source_anchors'][0]['path']
        source.write_text(source.read_text() + '\n# New source\n')
        retry = self.discovery_case('updated-build')
        result = self.cli('discovery-capture', '--capture-file', self.payload('stale', retry), ok=False)
        self.assertIn('fingerprint is stale', result.stderr)
        fingerprint = self.cli('browser-build-fingerprint').stdout.strip()
        self.record_browser_capture(retry)

    def discovery_case(self, capture_id='browser-case', *, role='member', state='success'):
        return {'capture_id': capture_id, 'feature_id': 'list-items', 'role': role, 'state': state,
                'route': '/items', 'viewport': {'width': 200, 'height': 100}, 'ready_selector': '#state',
                'setup': [], 'observations': {
                    'role': {'selector': '#state', 'attribute': 'data-role', 'expected': role},
                    'state': {'selector': '#state', 'attribute': 'data-state', 'expected': state},
                }}

    def record_browser_capture(self, capture):
        self.cli('discovery-capture', '--capture-file', self.payload('capture', capture))
        return self.manifest()['browser_captures'][capture['capture_id']]['screenshot']

    def test_followup_can_append_browser_capture_after_original_source_moves(self):
        self.prepared('ui')
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        recorded = self.directory / 'discovery/audits/audit-v1.json'
        original = recorded.read_bytes()
        audit = json.loads(original)
        (self.root / 'src/service.py').rename(self.root / 'src/replacement.py')
        audit['audit_id'] = 'audit-v2'
        audit['retired_source_paths'] = ['src/service.py']
        audit['sweeps'] = [audit['sweeps'][-1]]
        audit['sweeps'][0]['source_files_checked'].append('src/replacement.py')
        feature = audit['features'][0]
        for state in feature['states']:
            feature['browser_evidence'].append(self.fixture_browser_capture(feature, state))
        self.record_baseline(audit)
        self.assertEqual(recorded.read_bytes(), original)

    def test_ui_baseline_rejects_no_browser_and_unfinished_sweep(self):
        self.start()
        audit = self.baseline('ui')
        audit['features'][0].pop('browser_evidence')
        self.cli('discovery-record','--audit-file',self.payload('audit',audit),ok=False)
        audit = self.baseline('ui')
        audit['sweeps'][-1]['new_feature_ids'] = ['list-items']
        self.cli('discovery-record','--audit-file',self.payload('audit',audit),ok=False)

    def test_greenfield_baseline_requires_empty_app_and_user_requirements(self):
        self.start()
        self.exchange(requirement_ids=['list-items'])
        audit = {'audit_id':'empty-v1','scope':'feature','baseline_kind':'greenfield',
            'surface':'backend','inspected_paths':['.'], 'features':[], 'unresolved':[],
            'requirements':[{'requirement_id':'list-items','text':'List items',
                'decision_ids':['choose-layout']}],
            'evidence_paths':[self.artifact('discovery/empty.txt','Inspected tree: no application source')]}
        source = self.root / 'app.py'
        source.write_text('print("already exists")')
        self.cli('discovery-record','--audit-file',self.payload('audit',audit),ok=False)
        source.unlink()
        self.record_baseline(audit)
        self.assertEqual(self.manifest()['discovery_audits']['empty-v1']['feature_ids'], ['list-items'])
        source.write_text('print("implementation after verified empty baseline")')
        audit['audit_id']='empty-v2'
        self.record_baseline(audit)

    def test_exact_question_response_and_rejection_survive_resume(self):
        self.start()
        original = self.exchange(status='open', response=None)
        resumed = json.loads(self.cli('resume','--format','json').stdout)
        self.assertEqual(resumed['pending_questions'][0]['question'], original['question'])
        self.assertFalse(resumed['safe_to_continue'])
        self.exchange('answer-one', responds_to='choose-layout', response='Yes, keep both; reject deleting the role check.')
        resumed = json.loads(self.cli('resume','--format','json').stdout)
        self.assertEqual(resumed['pending_questions'], [])
        stored = json.loads((self.directory/'conversation/choose-layout.json').read_text())
        self.assertIsNone(stored['response'])
        self.assertEqual(resumed['conversation'][-1]['response'], 'Yes, keep both; reject deleting the role check.')
        self.cli('conversation-record','--exchange-file',self.payload('duplicate',original),ok=False)

    def test_unknown_answer_link_and_tampered_exchange_block(self):
        self.start()
        value = {'exchange_id':'answer-one','phase':'discover','question':'Keep?', 'status':'decided',
            'response':'Yes','responds_to':'missing','feature_ids':['list-items'],
            'requirement_ids':[], 'options':[], 'source_paths':[], 'illustration_paths':[]}
        self.cli('conversation-record','--exchange-file',self.payload('answer',value),ok=False)
        self.exchange()
        (self.directory/'conversation/choose-layout.json').write_text('{}')
        self.cli('validate',ok=False)

    def test_unrelated_exchange_cannot_supersede_open_or_decided_question(self):
        self.start()
        self.exchange('open-question', status='open')
        self.exchange('confirmed-decision', question='Keep the current response status?')
        for target in ('open-question', 'confirmed-decision'):
            with self.subTest(target=target):
                exchange = {
                    'exchange_id': f'unrelated-{target}', 'phase': 'discover', 'status': 'decided',
                    'question': 'Change cache duration?', 'response': 'Keep it.',
                    'feature_ids': ['list-items'], 'supersedes': target,
                }
                self.cli('conversation-record', '--exchange-file', self.payload('unrelated', exchange), ok=False)
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual([q['exchange_id'] for q in resumed['pending_questions']], ['open-question'])

    def test_pending_question_flags_changed_source_and_resumes_with_exact_answer(self):
        self.start()
        self.baseline()
        source = self.root / 'src/service.py'
        original = source.read_bytes()
        question = self.exchange(status='open', source_paths=['src/service.py'])
        work = {'task_id':'discuss-layout','phase':'discover','status':'waiting_user',
                'summary':'Question shown','next_action':'Wait for answer','completed_steps':[],
                'pending_steps':['Use the answer'],'blockers':[],'code_paths':[],'artifact_paths':[],
                'pending_question_id':question['exchange_id']}
        self.cli('work-update', '--work-file', self.payload('waiting', work))
        source.write_bytes(original + b'# Changed source evidence\n')
        validation = self.cli('validate', ok=False)
        self.assertIn('src/service.py', validation.stderr)
        resumed = json.loads(self.cli('resume', '--format', 'json', ok=False).stdout)
        self.assertEqual(resumed['pending_questions'][0]['question'], question['question'])
        answer = {**question, 'exchange_id': 'answer-one', 'responds_to': question['exchange_id'],
                  'response': 'Yes, keep both exactly.', 'status': 'decided'}
        self.cli('conversation-record', '--exchange-file', self.payload('answer', answer), ok=False)
        source.write_bytes(original)
        self.cli('conversation-record', '--exchange-file', self.payload('answer', answer))
        resumed = self.cli('resume').stdout
        self.assertIn(answer['response'], resumed)
        self.assertNotIn('WAITING FOR USER', resumed)
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(packet['active_question_answer']['exchange_id'], answer['exchange_id'])
        self.assertEqual(packet['active_work']['task_id'], work['task_id'])
        source.write_bytes(original + b'# Changed after the answer\n')
        self.cli('validate', ok=False)
        continued = dict(work, status='in_progress')
        del continued['pending_question_id']
        self.cli('work-update', '--work-file', self.payload('continue', continued), ok=False)
        evidence = self.artifact('discovery/refresh.txt', 'Inspected the change. It only adds a comment.')
        refreshed = dict(answer, exchange_id='answer-refreshed', supersedes=answer['exchange_id'],
                         source_reconciliation='The source changed but authorization and response are unchanged.',
                         evidence_paths=[evidence])
        self.cli('conversation-record', '--exchange-file', self.payload('refresh', refreshed))
        self.cli('work-update', '--work-file', self.payload('continue', continued))
        self.cli('validate')

    def plan(self, version='plan-v1', baseline_id='audit-v1'):
        return {'version':version,'baseline_id':baseline_id,'objective':'Preserve item access while restructuring',
            'scope':'feature','workstreams':['Service and API'], 'dependencies':['Existing authorization'],
            'risks':['Client compatibility'], 'decision_ids':['choose-layout'],
            'document_paths':[self.artifact(f'plans/{version}/roadmap.md', self.roadmap())]}

    def plan_checkpoint(self, plan, step, *, stream=None, requirement_ids=()):
        key = f"{plan['version']}-{step.replace('_', '-')}"
        if stream is not None:
            key += f"-{plan['workstreams'].index(stream) + 1}"
        prompt = {
            'exchange_id': key, 'phase': 'plan', 'record_type': 'review_checkpoint',
            'review_step': step, 'review_version': plan['version'], 'review_target': stream or step,
            'status': 'open', 'question': 'Ready to continue?', 'feature_ids': ['list-items'],
            'requirement_ids': list(requirement_ids),
            'evidence_paths': [self.artifact(f'review/{key}.md', 'Fixture terminal review.')],
        }
        if stream is not None:
            prompt['review_workstream'] = stream
        self.cli('conversation-record', '--exchange-file', self.payload('plan-prompt', prompt))
        return prompt

    def plan_reply(self, prompt, action, response, *, ok=True):
        value = {**prompt, 'exchange_id': prompt['exchange_id'] + '-reply',
                 'responds_to': prompt['exchange_id'], 'status': 'decided',
                 'review_action': action, 'response': response}
        return self.cli('conversation-record', '--exchange-file', self.payload('plan-reply', value), ok=ok)

    def finish_plan_review(self, plan):
        mapping = plan.get('requirement_workstreams', {})
        for stream in plan['workstreams']:
            ids = [key for key, names in mapping.items() if stream in names]
            prompt = self.plan_checkpoint(plan, 'plan_workstream', stream=stream, requirement_ids=ids)
            self.plan_reply(prompt, 'continue', 'Continue')
        prompt = self.plan_checkpoint(plan, 'plan_completion', requirement_ids=mapping)
        self.plan_reply(prompt, 'continue', 'Continue to blueprint')

    def record_reviewed_plan(self, plan):
        entry = self.manifest()['discovery_audits'][plan['baseline_id']]
        baseline = json.loads((self.directory / entry['path']).read_text())
        mapping = {row['requirement_id']: [plan['workstreams'][0]] for row in baseline.get('requirements', [])}
        plan.setdefault('requirement_workstreams', mapping)
        overview = self.plan_checkpoint(plan, 'roadmap')
        self.plan_reply(overview, 'confirm', 'The roadmap covers the scope.')
        self.cli('plan-record', '--plan-file', self.payload('plan', plan))
        self.finish_plan_review(plan)

    def roadmap(self, objective='Preserve item authorization and the API.'):
        return ('# Roadmap\n\n[Detailed blueprint specifications](blueprints/index.md)\n\n'
                f'## Objective\n{objective}\n\n'
                '## User journeys\nMembers list items.\n\n'
                '## Scope boundary\nItem listing only.\n\n'
                '## Preservation constraints\nKeep membership checks and response shape.\n\n'
                '## Workstreams\nService and API.\n\n'
                '## Dependencies\nExisting authorization service.\n\n'
                '## Risks\nClient compatibility.\n\n'
                '## Build order\nVerify membership before refactoring the service.\n')

    def backend_contract(self):
        return {'request':'GET /items; no body','response':'200 JSON array',
                'authorization':'member only','side_effects':'none','failure':'403 forbidden; 500 retryable',
                'compatibility':'Retain response schema; no migration'}

    def test_command(self, *args):
        return [sys.executable, 'tests/test_items.py', *args]

    def fixture_annexes(self, feature):
        return {
            'backend': {
                'Inputs and outputs': 'GET /items; no body. 200 JSON array.',
                'Authorization and validation': 'member only. Deny outsiders before querying.',
                'Persistence and transactions': 'Read the current store without a transaction spanning requests.',
                'Concurrency and retries': '403 forbidden; 500 retryable.',
                'Side effects and observability': 'none beyond existing logs.',
                'Compatibility and rollback': 'Retain response schema; no migration.',
                'Verification': 'Run the approved member and outsider fixtures.',
            },
            'ui': {
                'Route and entry': 'Open /items as a signed-in member.',
                'Viewports and breakpoints': 'Show a single column under 768px.',
                'Layout and tokens': 'gap = 16px between item rows.',
                'Copy and assets': 'Retain the existing Items label.',
                'States and roles': 'Show success, loading and forbidden as approved.',
                'Keyboard and assistive technology': 'Focusable list with announced loading.',
                'Events and dependencies': 'Opening /items calls the approved service.',
                'Render evidence': 'Capture the gap for ' + ', '.join(
                    f"{case['case_id']} at {case['route']} {case['ready_selector']} {case['viewport']}"
                    for case in feature.get('visual_contract', {}).get('render_cases', [])),
            },
            'api': {
                'Method and route': 'GET /items serves member items.',
                'Request and response schemas': '`{"request":{"type":"object"},"response":{"type":"array"}}`',
                'Statuses and error schemas': '`{"errors":{"403":{"type":"object"}}}`',
                'Authentication and scope': 'Member session is required.',
                'Validation and query options': 'No request body or pagination parameters.',
                'Concurrency and timeouts': 'Retain the service timeout behavior.',
                'Side effects and compatibility': 'Read only and preserve existing response.',
                'Verification': 'Run API fixtures for success and forbidden.',
            },
            'mcp': {
                'Server and transport': 'Expose the read tool on the fixture MCP transport.',
                'Registration and capabilities': 'Register list_items with an item-list description.',
                'Input and output schemas': '`{"input":{"type":"object"},"output":{"type":"array"}}`',
                'Permissions and consent': 'Only the current member may call it.',
                'Protocol and tool errors': 'Map forbidden as a tool error.',
                'Cancellation and timeouts': 'Propagate cancellation to the service.',
                'Streaming and data exposure': 'Return bounded items for the member only.',
                'Client invocation and verification': 'Invoke tools/call list_items from the fixture client.',
            },
            'script': {
                'Entry point and runtime': 'Run the Python item fixture entry point.',
                'Flags and configuration': 'Accept the explicit member fixture flag.',
                'Environment and working directory': 'Run from the application root.',
                'Inputs and outputs': 'Read a member fixture and print the JSON result.',
                'File mutations': 'Read the fixture without changing files.',
                'Exit codes and streams': 'Exit 0 on success and 1 on service failure.',
                'Recovery and concurrency': 'Retry after recovering the service.',
                'Verification': 'Check stdout and exit status for each case.',
            },
        }

    def approved_fixture_annexes(self, feature, surfaces):
        annexes = self.fixture_annexes(feature)
        return {
            surface: {field: json.loads(value.strip('`')) if value.startswith('`{') else value
                      for field, value in annexes[surface].items()}
            for surface in surfaces
        }

    def blueprint(self, version='spec-v1', surface='backend'):
        feature = {'feature_id':'items-feature','purpose':'Member can list items',
            'baseline_ids':['list-items'],'classification':'KEEP','decision_ids':['choose-layout'],
            'spec_paths':[f'blueprints/{version}/items.md'],
            'anatomy':'Items route delegates to authorization and service',
            'behavior_contract':'GET /items checks membership before reading items',
            'states':['success','forbidden','error'], 'dependencies':['authorization'],
            'implementation_paths':['src/service.py'],
            'acceptance_checks':[{'check_id':'items-access','kind':'backend',
                                  'command':self.test_command('api','items-access'),
                                  'expected':'Member gets 200 array; nonmember gets 403',
                                  'state_expectations': dict(BACKEND_OUTCOMES)}],
            'backend_contract':self.backend_contract()}
        manifest = self.manifest()
        audit = manifest['discovery_audits'].get(manifest['active_discovery_id'], {})
        if audit.get('baseline_kind') == 'greenfield':
            feature['planned_test_paths'] = ['tests/test_items.py']
            feature['implementation_paths'].extend(feature['planned_test_paths'])
        if surface == 'ui':
            feature.pop('backend_contract')
            feature['states'].insert(0, 'loading')
            feature['visual_contract']={'tokens':{'gap':'16px'},'responsive':'single column under 768px',
                                        'accessibility':'Focusable list with announced loading',
                                        'render_cases':[{'case_id':state+'-case','state':state,'route':'/items','viewport':dict(FIXTURE_VIEWPORT),
                                                         'command':self.test_command('render',state),
                                                         'ready_selector':f'#state-{state}'}
                                for state in ('loading','success','forbidden','error')]}
            feature['acceptance_checks']=[{'check_id':'items-access','kind':'visual','expected':'16px',
                                           'command':self.test_command('visual','items-access'),
                                           'measurement':{'selector':'#items','property':'gap'}},
                {'check_id':'items-behavior','kind':'behavior','expected':'Member can load items; outsider denied',
                 'command':self.test_command('visual','items-behavior')}]
        value = {'version':version,'baseline_id':'audit-v1','plan_version':'plan-v1',
                 'surface':surface,'document_paths':[f'blueprints/{version}/index.md'],
                 'decision_ids':['choose-layout'],'features':[feature],'open_decisions':[]}
        if FIXTURE_CLI.requires_slice_review(manifest):
            value['surface_contract_version'] = 2
            primary_surfaces = ('ui', 'backend') if surface == 'fullstack' else (surface,)
            feature['surface_contracts'] = self.approved_fixture_annexes(feature, primary_surfaces)
            source = 'loading' if 'loading' in feature['states'] else 'error'
            feature['state_transitions'] = [
                {'from': source, 'to': state, 'trigger': 'Read the next item request',
                 'guard': f'The approved result is {state}', 'effect': f'Return the approved {state} outcome'}
                for state in feature['states'] if state != source
            ]
        return value

    def coverage(self, version='spec-v1', surface='backend'):
        return {'version':version,'baseline_id':'audit-v1','mappings':[{'baseline_id':'list-items',
            'feature_id':'items-feature','acceptance_ids':['items-access','items-behavior'] if surface=='ui' else ['items-access']}]}

    def prepared(self, surface='backend', *, review=False):
        self.start(review=review)
        self.exchange()
        self.record_baseline(self.baseline(surface))
        plan = self.plan()
        self.record_reviewed_plan(plan)
        blueprint = self.blueprint(surface=surface)
        self.cli('blueprint-record','--blueprint-file',self.payload('blueprint',blueprint))
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',self.coverage(surface=surface)))
        return blueprint

    def approval(self, version='spec-v1'):
        manifest = self.manifest()
        baseline = manifest['discovery_audits'][manifest['active_discovery_id']]
        if baseline['baseline_kind'] == 'existing' and not (self.root/'tests/test_items.py').exists():
            self.write_test_runner()
        recap = self.artifact(f'approval-evidence/{version}-recap.md', '## Decision recap\n\nPreserve membership checks.')
        return {'decision_id':'approve-' + version,'approved_version':version,'scope':'feature',
                'mode':'blueprint','approved_by_user':True,'user_approval_note':'Approve this exact specification.',
                'shown_paths':[f'blueprints/{version}/index.md',f'blueprints/{version}/items.md',
                               f'blueprint-coverage/{version}.json', recap],
                'decision_recap_path': recap, 'exceptions': []}

    def test_negative_slice_decision_cannot_approve(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        value = self.review_input() | {'user_response': 'No. I reject items-slice revision 1.'}
        result = self.with_slice_consent('slice-review-record', value, ok=False)
        self.assertIn('separate explicit user instruction', result.stderr)
        self.review_cli('slice-review-record', value, ok=False)
        self.assertEqual(self.manifest()['slice_review']['reviews'], {})

    def test_negative_start_decision_cannot_release(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        value = package | {'instruction': 'start implementation', 'authorized_by_user': True,
                           'user_response': 'Do not start implementation of package-v1.'}
        result = self.with_slice_consent('implementation-authorize', value, ok=False)
        self.assertIn('separate explicit user instruction', result.stderr)
        self.review_cli('implementation-authorize', value, ok=False)
        self.review_cli('slice-release', package | {'slice_id': 'items-slice'}, ok=False)
        self.assertEqual(self.manifest()['slices'], {})

    def test_question_is_atomic_and_blocking_other_slice_preserves_cursor(self):
        feature = self.reviewed_blueprint(multiple=True)
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature),
            self.guide_candidate(feature, 'other-slice', path='src/adapter.py')]})
        self.exchange('guard-question', status='open', phase='slice_review', feature_ids=['items-feature'])
        before = self.manifest()['slice_review']
        self.assertEqual(before['pending_question_id'], 'guard-question')
        self.review_cli('slice-review-record', {'action': 'block', 'slice_id': 'other-slice', 'proposal_revision': 1,
            'reason': 'Missing adapter recovery details',
            'evidence_paths': [self.artifact('inputs/adapter-block.md', 'The design omits recovery details.')]})
        resumed = json.loads(self.cli('resume', '--format', 'json', ok=False).stdout)
        self.assertEqual(resumed['slice_review']['current_slice_id'], 'items-slice')
        self.assertEqual(resumed['slice_review']['pending_question_id'], 'guard-question')

    def test_slice_exchange_cannot_bind_unknown_requirement(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        result = self.cli('conversation-record', '--exchange-file', self.payload('unknown-scope', {
            'exchange_id': 'scope-question', 'phase': 'slice_review', 'question': 'Which roles can read?',
            'status': 'open', 'basis': 'Review approved guards', 'feature_ids': ['items-feature'],
            'requirement_ids': ['unrelated-requirement'],
        }), ok=False)
        self.assertIn('requirement', result.stderr)

    def test_review_action_type_returns_actionable_blocker(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        result = self.review_cli('slice-review-record', {'action': [], 'slice_id': 'items-slice',
                                                        'proposal_revision': 1}, ok=False)
        self.assertIn('action', result.stderr)

    def test_slice_review_blocks_unresolvable_walkthrough_symbol(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        value = self.review_input()
        handoff = value['handoff_review']
        handoff['read_only_scenarios'][0]['references'][0]['symbol'] = 'Missing reviewed guide heading'
        attachment = self.directory / handoff['walkthrough']['evidence_path']
        record = json.loads(attachment.read_text())
        record['cases'][0]['references'][0]['symbol'] = 'Missing reviewed guide heading'
        attachment.write_text(json.dumps(record))
        value = self.with_slice_consent('slice-review-record', value)
        result = self.review_cli('slice-review-record', value, ok=False)
        self.assertIn('Cannot resolve walkthrough symbol', result.stderr)
        self.assertEqual(self.manifest()['slice_review']['reviews'], {})

    def test_interrupted_incomplete_inventory_returns_blocked_result(self):
        self.reviewed_blueprint()
        request = self.payload('incomplete-inventory', {'slices': []})
        recover = FIXTURE_CLI.recover_workflow_transition

        def interrupt(directory, value, *, replay=True):
            if not replay and (directory / '.workflow-transition.json').exists():
                raise OSError('interrupted inventory write')
            return recover(directory, value, replay=replay)

        with patch.object(FIXTURE_CLI, 'recover_workflow_transition', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'interrupted inventory write'):
                FIXTURE_CLI.run_slice_review(self.directory, 'slice-plan', str(request))
        recovered = self.cli('slice-plan', '--input-file', request, ok=False)
        journal = (self.directory / 'decisions.jsonl').read_bytes()
        retried = self.cli('slice-plan', '--input-file', request, ok=False)
        self.assertEqual(recovered.returncode, 2)
        self.assertEqual(recovered.stdout, retried.stdout)
        self.assertEqual(recovered.stderr, retried.stderr)
        self.assertEqual((self.directory / 'decisions.jsonl').read_bytes(), journal)

    def test_delegating_another_slice_preserves_answered_question_cursor(self):
        feature = self.reviewed_blueprint(multiple=True)
        first = self.guide_candidate(feature) | {'dependency_contracts': []}
        second = self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py') | {
            'dependency_contracts': []}
        self.review_cli('slice-plan', {'slices': [first, second]})
        question = 'Does the membership guard still run before reading storage?'
        self.exchange('guard-question', phase='slice_review', status='open', question=question)
        self.exchange('guard-answer', phase='slice_review', question=question, responds_to='guard-question',
                      response='Keep the guard before storage.', record_type='slice_detail',
                      slice_id='items-slice', proposal_revision=1, clarifies_decision_ids=['choose-layout'])
        result = self.review_cli('slice-review-record', self.review_input('adapter-slice', action='delegate'))
        self.assertEqual(result['reviews']['adapter-slice']['result'], 'delegated_validated')
        self.assertEqual(result['pending_question_id'], 'guard-question')
        self.assertEqual(result['current_slice_id'], 'items-slice')
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['active_question_answer']['exchange_id'], 'guard-answer')

    def test_register_keeps_verbatim_question_with_a_custom_next_action(self):
        feature = self.reviewed_blueprint()
        candidate = self.guide_candidate(feature) | {'dependency_contracts': []}
        self.review_cli('slice-plan', {'slices': [candidate]})
        question = 'Does the membership guard still run before reading storage?'
        self.exchange('guard-question', phase='slice_review', status='open', question=question)
        self.review_cli('slice-review-record', {'slice_id': 'items-slice', 'proposal_revision': 1,
            'action': 'question', 'pending_question_id': 'guard-question', 'next_action': 'Wait for confirmation'})
        register = self.review_cli('slice-register')
        self.assertEqual(register['pending_question']['question'], question)
        markdown = (self.directory / register['register_path']).read_text()
        self.assertIn(question, markdown)
        self.assertIn('Wait for confirmation', markdown)

    def test_committed_collection_retry_reuses_report(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        data = self.collection_input()
        self.assertEqual(self.review_cli('slice-collection-validate', data),
                         self.review_cli('slice-collection-validate', data))

    def test_collection_requires_case_grounded_journey_evidence_before_lock(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        data = self.collection_input()
        data['semantic_checks']['end_to_end_journeys']['case_conclusions'] = []
        response = self.review_cli('slice-collection-validate', data, ok=False)
        check = json.loads(response.stdout)['checks']['end_to_end_journeys']
        self.assertEqual(check['result'], 'BLOCKED')
        self.assertIn('exact feature/check', check['explanation'])
        self.review_cli('slice-package-preview', ok=False)

    def test_missing_coverage_and_rejected_alternatives_appear_in_register(self):
        feature = self.reviewed_blueprint()
        response = self.review_cli('slice-plan', {'slices': []}, ok=False)
        self.assertIn('items-access', response.stderr)
        register = self.review_cli('slice-register')
        self.assertEqual(register['planning_gaps']['feature_checks'], [['items-feature', 'items-access']])
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.exchange('guard-option', phase='slice_review', status='open', feature_ids=['items-feature'])
        question = 'Keep the existing authorization and response shape?'
        self.exchange('guard-rejection', phase='slice_review', status='rejected', question=question,
                      responds_to='guard-option', response='Reject the alternative that changes the guard.',
                      feature_ids=['items-feature'])
        register = self.review_cli('slice-register')
        self.assertEqual(register['rejected_alternatives']['guard-rejection']['slice_id'], 'items-slice')
        self.assertIn('guard-rejection', (self.directory / register['register_path']).read_text())

    def test_clarification_correction_preserves_history_and_freezes_effective_decision(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        q = 'Does the member guard run before storage?'
        self.exchange('guard-question', phase='slice_review', status='open', question=q, feature_ids=['items-feature'])
        binding = {'phase': 'slice_review', 'question': q, 'responds_to': 'guard-question',
                   'feature_ids': ['items-feature'], 'record_type': 'slice_detail', 'slice_id': 'items-slice',
                   'proposal_revision': 1, 'clarifies_decision_ids': ['choose-layout']}
        self.exchange('guard-answer', response='Yes, check it before querying.', **binding)
        self.exchange('guard-correction', response='Check the member role before all storage reads.',
                      supersedes='guard-answer', **binding)
        candidate = self.guide_candidate(feature, revision=2, detail='guard-correction')
        summary = self.review_cli('slice-draft', candidate)
        contract = json.loads((self.directory / summary['slices']['items-slice']['contract_path']).read_text())
        self.assertIn('guard-correction', contract['decision_ids'])
        self.assertNotIn('guard-answer', contract['decision_ids'])
        self.assertTrue((self.directory / 'conversation/guard-answer.json').is_file())
        self.review_cli('slice-review-record', self.review_input(exchanges=('guard-question', 'guard-correction')))

    def test_reviewed_blueprint_rejects_application_absolute_runner(self):
        self.test_command = lambda *args: [sys.executable, str(self.root / 'tests/test_items.py'), *args]
        self.prepared(review=True)
        result = self.cli('approve', '--approval-file', self.payload('approval', self.approval()), ok=False)
        self.assertIn('workspace-specific path', result.stderr)

    def test_missing_lifecycle_journal_events_block_release(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        journal = self.directory / 'decisions.jsonl'
        journal.write_text(''.join(line + '\n' for line in journal.read_text().splitlines()
            if json.loads(line).get('type') not in {'slice_spec_reviewed', 'slice_package_locked',
                                                  'slice_implementation_authorized'}))
        response = self.review_cli('slice-release', package | {'slice_id': 'items-slice'}, ok=False)
        self.assertIn('lifecycle history', response.stderr)
        self.assertEqual(self.manifest()['slices'], {})

    def test_suspension_preserves_authorized_work_and_detects_later_writes(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        self.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        source = self.root / 'src/service.py'
        source.write_text(source.read_text() + '\n# Work written while this slice was released.\n')
        paused_source = source.read_bytes()
        self.review_cli('slice-review-record', {'action': 'reopen', 'slice_id': 'items-slice',
                                                'proposal_revision': 1, 'user_response': 'Reopen this slice.'})
        self.cli('validate')
        source.write_text(source.read_text() + '\n# Work written after the pause.\n')
        result = self.cli('validate', ok=False)
        self.assertIn('Suspended source changed after pause', result.stderr)
        self.assertIn('src/service.py', result.stderr)
        source.write_bytes(paused_source)
        self.review_cli('slice-review-record', self.review_input())
        replacement = self.lock_reviewed_package(version=2)
        self.authorize_fixture(replacement)
        before_release = source.read_bytes()
        source.write_bytes(before_release + b'\n# Work before the separate release.\n')
        result = self.review_cli('slice-release', replacement | {'slice_id': 'items-slice'}, ok=False)
        self.assertIn('Suspended source changed after pause', result.stderr)
        source.write_bytes(before_release)
        self.review_cli('slice-release', replacement | {'slice_id': 'items-slice'})

    def test_committed_migration_retry_preserves_one_receipt(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        value = {'approved_by_user': True, 'user_response': 'Migrate the saved initiative.'}
        self.assertEqual(self.review_cli('slice-review-migrate', value),
                         self.review_cli('slice-review-migrate', value))
        events = (self.directory / 'decisions.jsonl').read_text()
        self.assertEqual(events.count('"type": "slice_review_migrated"'), 1)

    def test_observed_guide_requires_journaled_command_execution(self):
        feature = self.reviewed_blueprint()
        candidate = self.guide_candidate(feature)
        path = self.directory / candidate['guide_path']
        runner = self.root / 'tests/test_items.py'
        manifest = self.manifest()
        approved = manifest['approvals'][manifest['active_approval_id']]['workspace_snapshot']
        check = feature['acceptance_checks'][0]
        receipt = {'check_id': check['check_id'], 'argv': check['command'], 'expected': check['expected'],
                   'actual': check['expected'], 'result': 'PASS', 'exit_code': 0,
                   'executed_at': '2026-11-20T00:00:00Z',
                   'source_hashes': {str(runner.relative_to(self.root)): approved['tests/test_items.py']}}
        relative = path.parent.relative_to(self.directory) / 'examples/self-attested.json'
        candidate['asset_paths'].append(self.artifact(str(relative), json.dumps(receipt)))
        path.write_text(path.read_text().replace('| planned |', '| observed |')
                        .replace('| Not run |', '| examples/self-attested.json |'))
        result = self.review_cli('slice-plan', {'slices': [candidate]}, ok=False)
        self.assertIn('journaled verification', result.stderr)

    def test_observed_guide_can_reuse_matching_coordinator_verification(self):
        feature = self.reviewed_blueprint(multiple=True)
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        self.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        self.accept_first_released_slice()
        candidate = self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py', deps=('items-slice',))
        path = self.directory / candidate['guide_path']
        source = self.directory / 'slices/items-slice/verification.json'
        relative = path.parent.relative_to(self.directory) / 'examples/verification.json'
        candidate['asset_paths'].append(self.artifact(str(relative), source.read_bytes()))
        path.write_text(path.read_text().replace('| planned |', '| observed |')
                        .replace('| Not run |', '| examples/verification.json |'))
        self.review_cli('slice-plan', {'slices': [{'slice_id': 'items-slice', 'retain': True}, candidate],
                                       'amendment': 'Add a verified adapter handoff.'})
        self.review_cli('slice-guide-validate', {'slice_id': 'adapter-slice'})

    def test_sr001_blueprint_approval_cannot_create_an_executable_slice(self):
        self.prepared(review=True)
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        result = self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()), ok=False)
        self.assertIn('slice-release', result.stderr)
        self.assertEqual(self.manifest()['slices'], {})
        self.assertFalse((self.directory / 'slices/items-slice').exists())

    def test_sr003_guide_reports_missing_section(self):
        self.prepared(review=True)
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        candidate = self.slice() | {
            'title': 'Member item list', 'proposal_revision': 1, 'surfaces': ['backend'],
            'guide_path': self.artifact('inputs/items-guide.md', '## Identity and status\nMember item list'),
            'traceability': [], 'asset_paths': [], 'shared_contracts': {}, 'dependency_contracts': [],
        }
        result = self.cli('slice-plan', '--input-file', self.payload('inventory', {'slices': [candidate]}), ok=False)
        self.assertIn('Traceability matrix', result.stderr)

    def reviewed_blueprint(self, *, surface='backend', multiple=False, extra_surfaces=False):
        if surface == 'backend' and not extra_surfaces and not any(self.root.iterdir()):
            sample, feature = approved_backend_sample(multiple)
            shutil.copytree(sample, self.root, dirs_exist_ok=True)
            return copy.deepcopy(feature)
        return self.build_reviewed_blueprint(surface=surface, multiple=multiple, extra_surfaces=extra_surfaces)

    def build_reviewed_blueprint(self, *, surface='backend', multiple=False, extra_surfaces=False):
        self.start(review=True)
        self.exchange()
        self.record_baseline(self.baseline(surface))
        self.record_reviewed_plan(self.plan())
        blueprint = self.blueprint(surface=surface)
        if multiple:
            blueprint['features'][0]['implementation_paths'] += ['src/adapter.py', 'src/view.py']
        if extra_surfaces:
            feature = blueprint['features'][0]
            feature['slice_surfaces'] = ['api', 'mcp', 'script']
            feature['surface_contracts'].update(self.approved_fixture_annexes(feature, feature['slice_surfaces']))
        self.cli('blueprint-record', '--blueprint-file', self.payload('blueprint', blueprint))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage(surface=surface)))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        return blueprint['features'][0]

    def with_slice_consent(self, command, value, *, response=None, ok=True):
        if command == 'slice-review-record':
            action = value['action']
            target = {'slices': [{field: value[field] for field in
                                  ('slice_id', 'proposal_revision', 'guide_sha256', 'contract_sha256')}]}
            if action == 'delegate':
                target['constraints'] = value['constraints']
                value = value | {'user_response': f'Delegate review of {value["slice_id"]} revision {value["proposal_revision"]}'}
        elif command == 'slice-package-lock':
            from hashlib import sha256
            action = 'lock'
            target = {field: value[field] for field in ('package_id', 'collection_path', 'collection_sha256')}
            target['recap_sha256'] = sha256((self.directory / value['recap_path']).read_bytes()).hexdigest()
        else:
            action = 'start'
            target = {field: value[field] for field in ('package_id', 'package_hash')}
        quote = response if response is not None else value['user_response']
        consent_id = f"slice-consent-{len(self.manifest().get('conversation', {})) + 1}"
        exchange = {'exchange_id': consent_id, 'phase': 'slice_review', 'record_type': 'slice_consent',
                    'status': 'decided', 'question': f'Confirm {action} for {json.dumps(target)}',
                    'response': quote, 'consent_action': action, 'target': target}
        result = self.cli('conversation-record', '--exchange-file', self.payload(consent_id, exchange), ok=ok)
        return value | {'consent_id': consent_id} if ok else result

    def review_cli(self, command, value=None, *, ok=True):
        if ok and value and 'consent_id' not in value and (command in {'slice-package-lock', 'implementation-authorize'}
                or command == 'slice-review-record' and value.get('action') in ('approve', 'delegate')):
            value = self.with_slice_consent(command, value)
        args = [] if value is None else ['--input-file', self.payload(command, value)]
        output = self.cli(command, *args, ok=ok)
        return json.loads(output.stdout) if ok else output

    def guide_candidate(self, feature, slice_id='items-slice', *, revision=1, path='src/service.py',
                        deps=(), surfaces=('backend',), missing_trace=None, shared=None, detail=None):
        from hashlib import sha256
        manifest = self.manifest()
        approval_id = manifest['active_approval_id']
        approval = manifest['approvals'][approval_id]
        version = approval['approved_version']
        spec = json.loads((self.directory / manifest['blueprints'][version]['path']).read_text())
        snapshot_hash = sha256(json.dumps(approval['workspace_snapshot'], ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')).encode()).hexdigest()
        key = feature['feature_id']
        title = f'Member items ({slice_id})'
        spec_links = '\n'.join(f'- [Approved feature]({ "../../../../" + path })' for path in feature['spec_paths'])
        decision_links, history_hashes = [], {}
        for spec_path in feature['spec_paths']:
            history_hashes[spec_path] = sha256((self.directory / spec_path).read_bytes()).hexdigest()
        for decision_id in sorted(set(spec['decision_ids'] + feature['decision_ids'] + ([detail] if detail else []))):
            entry = manifest['conversation'][decision_id]
            decision_links.append(f'- [{decision_id}](../../../../{entry["path"]})')
            history_hashes.update({entry['path']: entry['sha256'], **entry.get('evidence_hashes', {})})
        invalidated = ('none; this is the initial revision.' if revision == 1 else f'revision {revision - 1}.')
        history = '\n'.join([
            f'Revision {revision} prepared for exact review.',
            'Outcome: pending review.',
            'Exceptions: none.',
            f'Invalidated revisions: {invalidated}',
            *decision_links,
            *(f'- `{path}` SHA-256 `{digest}`' for path, digest in sorted(history_hashes.items())),
        ])
        headings = {
            'Identity and status': '\n'.join(f'- {field}: `{value}`' for field, value in {
                'slice_id': slice_id, 'title': title, 'proposal_revision': revision, 'blueprint_version': version,
                'blueprint_approval_id': approval_id, 'plan_version': spec['plan_version'],
                'source_snapshot_sha256': snapshot_hash, 'owner': 'builder', 'scope': 'feature',
            }.items()) + '\n- review_status: Pending review; not approved.\n'
                           '- package: Not assigned at guide freeze.\n' + spec_links,
            'In plain language': 'A signed-in member opens the items list, and the application shows their items.',
            'Why this slice exists': f'`{key}` preserves `list-items` and the user decision `choose-layout`.',
            'What exists now': '`src/service.py` contains `def list_items`. Membership is checked before reads.',
            'Finished experience or service contract': ('| Feature | Check ID | Expected outcome |\n'
                '| --- | --- | --- |\n' + '\n'.join(
                    f'| {key} | {check["check_id"]} | {check["expected"]} |'
                    for check in feature['acceptance_checks'])),
            'Architecture and anatomy': f'`{key}` uses `{path}`. '
                                        'The request handler checks the caller before invoking item storage.',
            'Detailed functional requirements': '\n'.join(
                f'{index}. `{key}` binds `{check["check_id"]}` to the membership guard and '
                f'its expected outcome, {check["expected"]}.'
                for index, check in enumerate(feature['acceptance_checks'], 1)),
            'Visual specification': ('Render the items list at /items with a 16px gap and a single column under 768px.'
                                     if 'ui' in surfaces else 'Not applicable — This delivery exposes a service only.'),
            'Interface specification': ('Serve GET /items and require member authorization, preserving the array response.'
                                        if 'backend' in surfaces else 'Not applicable — This slice tests the existing UI.'),
            'State and scenario matrix': '\n'.join(f'- `{state}` yields {BACKEND_OUTCOMES.get(state, "an announced loading state")}.'
                                                  for state in feature['states']),
            'Data and security rules': 'Keep membership checks on the service, and retain the current storage read.',
            'Exact implementation footprint': '',
            'Step-by-step implementation recipe': (
                f'1. `check-items` Run approved fixtures for '
                f'{", ".join(check["check_id"] for check in feature["acceptance_checks"])} '
                f'in `{path}`. Expected result: ' + '; '.join(
                    check['expected'] for check in feature['acceptance_checks'])),
            'Dependencies and integration': 'Read accepted prerequisites: ' + (', '.join(deps) or 'none for this slice'),
            'Safety, rollback and prohibited deviations': f'Do not change membership or JSON schema. '
                                                         f'Rollback steps: Restore `{path}` to the pre-slice contents.',
            'Definition of done': 'The member list and outsider refusal pass the approved test cases, with saved evidence.',
            'Fresh-session handoff': ('- First files to read: Read src/service.py and the approved feature contract before editing.\n'
                                      '- Environment: Set P2P_SKILL_ROOT to the installed skill directory and use the app repository root.\n'
                                      '- Resume command: `python3 "$P2P_SKILL_ROOT/scripts/project_state.py" resume --project-root . --initiative-id sample-work --format json`\n'
                                      '- Validation command: `python3 "$P2P_SKILL_ROOT/scripts/project_state.py" validate --project-root . --initiative-id sample-work`\n'
                                      '- Current blockers: No blockers are recorded for this slice.\n'
                                      '- Next action: Inspect src/service.py and implement the approved membership behavior.\n'
                                      '- How to report findings: Record each result with its check ID and evidence path.'),
            'Change and approval history': history,
        }
        fixture_folder = f'inputs/{slice_id}-r{revision}'
        transitions = feature.get('state_transitions', [])
        if transitions:
            diagram = 'stateDiagram-v2\n' + ''.join(
                f'  {edge["from"]} --> {edge["to"]}: {edge["trigger"]} / {edge["guard"]} / {edge["effect"]}\n'
                for edge in transitions)
        else:
            diagram = ('stateDiagram-v2\n  [*] --> Request\n' +
                       ''.join(f'  Request --> {state}\n' for state in feature['states']))
        self.artifact(f'{fixture_folder}/diagrams/items.mmd', diagram)
        headings['Worked examples and diagrams'] = (
            'A member opens the list. The route returns its JSON result. [Diagram source](diagrams/items.mmd)\n\n'
            '```mermaid\n' + diagram + '```')
        checks = feature['acceptance_checks']
        state_rows = ['| Feature | Check ID | State | Expected outcome |', '| --- | --- | --- | --- |']
        for check in checks:
            state_rows.extend(f"| {key} | {check['check_id']} | {state} | {outcome} |"
                              for state, outcome in sorted(check.get('state_expectations', {}).items()))
        for case in feature.get('visual_contract', {}).get('render_cases', []):
            viewport = case['viewport']
            outcome = f'renders {case["route"]} at {viewport.get("width", "?")}x{viewport.get("height", "?")} with {case["ready_selector"]}'
            state_rows.append(f"| {key} | {case['case_id']} | {case['state']} | {outcome} |")
        headings['State and scenario matrix'] = '\n'.join(state_rows)
        headings['Exact implementation footprint'] = ('| Path | Operation | Feature |\n| --- | --- | --- |\n'
                                                        f'| {path} | MODIFY | {key} |')
        fixtures = FIXTURE_CLI.blueprint_state(self.directory, manifest).test_sources(feature)
        headings['Tests and acceptance evidence'] = (
            f'Fixture sources: {", ".join(sorted(fixtures))}.\n\n'
            '| Check ID | Status | Expected outcome | Command | Evidence |\n'
            '| --- | --- | --- | --- | --- |\n' + '\n'.join(
                f"| {c['check_id']} | planned | {c['expected']} | `{json.dumps(c['command'])}` | Not run |"
                for c in checks))
        trace = []
        for kind, values, section in (
            ('feature', [key], 'Why this slice exists'),
            ('baseline', ['list-items'], 'Why this slice exists'),
            ('decision', ['choose-layout'], 'Why this slice exists'),
            ('state', feature['states'], 'State and scenario matrix'),
            ('check', [c['check_id'] for c in checks], 'Tests and acceptance evidence'),
        ):
            trace += [dict(feature_id=key, kind=kind, id=value, section=section, step='check-items')
                      for value in values if value != missing_trace]
        if detail:
            headings['Why this slice exists'] += f' The user clarified this in `{detail}`.'
            trace.append(dict(feature_id=key, kind='decision', id=detail,
                              section='Why this slice exists', step='check-items'))
        headings['Traceability matrix'] = ('| Feature | Kind | ID | Section | Step |\n| --- | --- | --- | --- | --- |\n' +
            '\n'.join('| ' + ' | '.join(row.values()) + ' |' for row in trace))
        annexes = self.fixture_annexes(feature)
        guide = f'# {title}\n\n' + '\n\n'.join(
            f'## {heading}\n\n{body}' for heading, body in headings.items()) + '\n\n'
        guide += '\n\n'.join(f'### {surface.upper()} annex — {key}\n\n' +
                             '\n'.join(f'- {field}: {body}' for field, body in annexes[surface].items())
                             for surface in surfaces)
        dependency_contracts = []
        for dependency_id in deps:
            consumed = sorted(name for name, contract in (shared or {}).items()
                              if contract['role'] == 'consumes')
            if consumed:
                dependency_contracts.append({'dependency_id': dependency_id, 'kind': 'interface',
                                             'contract_ids': consumed, 'paths': []})
            else:
                dependency_contracts.append({'dependency_id': dependency_id, 'kind': 'ordering_only',
                                             'reason': 'The prerequisite supplies a required foundation before this slice.',
                                             'evidence_paths': list(feature['spec_paths'])})
        return self.slice() | {
            'slice_id': slice_id, 'allowed_paths': [path], 'dependencies': list(deps),
            'blueprint_version': version, 'functional_tests': [c['check_id'] for c in checks], 'title': title,
            'proposal_revision': revision, 'surfaces': list(surfaces),
            'guide_path': self.artifact(f'{fixture_folder}/implementation-guide.md', guide + '\n'),
            'traceability': trace, 'asset_paths': [f'{fixture_folder}/diagrams/items.mmd'],
            'artifact_paths': feature['spec_paths'],
            'shared_contracts': shared or {},
            'dependency_contracts': dependency_contracts,
        }

    def review_input(self, slice_id='items-slice', *, action='approve', exchanges=()):
        item = self.manifest()['slice_review']['selected'][slice_id]
        contract = json.loads((self.directory / item['contract_path']).read_text())
        features = contract.get('feature_contracts', [])
        checks = [check for feature in features for check in feature.get('acceptance_checks', [])]
        guide_lines = (self.directory / item['guide_path']).read_text().splitlines()
        section_line = next(index for index, line in enumerate(guide_lines, 1)
                            if line.strip() == '## State and scenario matrix')
        finding_sections = ['Finished experience or service contract', 'Detailed functional requirements',
                            'Step-by-step implementation recipe']
        normative_findings = []
        for section in finding_sections:
            start = guide_lines.index(f'## {section}') + 1
            line = next(index + 1 for index in range(start, len(guide_lines)) if guide_lines[index].strip())
            normative_findings.append({'section': section, 'citation': f'line {line}', 'result': 'PASS',
                                       'finding': 'Traced member and outsider behavior to the approved guard.',
                                       'resolution': 'The section preserves the approved list and refusal outcomes.'})
        scenario_rows = []

        def scenario(feature_id, suffix, expected, case_ids, symbol):
            return {'scenario': f'{feature_id}:{suffix}', 'expected': expected, 'observed': expected,
                    'read_only': True, 'observation_basis': f'Traced the approved entries in {symbol}.',
                    'references': [{'feature_id': feature_id, 'check_id': case_id,
                                    'source_path': item['guide_path'], 'symbol': symbol} for case_id in case_ids]}

        for feature in features:
            feature_id = feature['feature_id']
            expected, references = {}, {}
            for check in feature.get('acceptance_checks', []):
                expected.update(check.get('state_expectations', {}))
                for state in check.get('state_expectations', {}):
                    references.setdefault(state, []).append(check['check_id'])
                scenario_rows.append(scenario(feature_id, f'check:{check["check_id"]}', check['expected'],
                                              [check['check_id']], 'Tests and acceptance evidence'))
            scenario_rows.extend(scenario(feature_id, state, outcome, references[state], 'State and scenario matrix')
                for state, outcome in expected.items() if state in feature.get('states', []))
            for case in feature.get('visual_contract', {}).get('render_cases', []):
                viewport = case['viewport']
                outcome = f'renders {case["route"]} at {viewport["width"]}x{viewport["height"]} with {case["ready_selector"]}'
                scenario_rows.append(scenario(feature_id, f'render:{case["case_id"]}', outcome,
                                              [case['case_id']], 'State and scenario matrix'))
        cases = [{field: row[field] for field in ('scenario', 'expected', 'observed', 'observation_basis', 'references')}
                 | {field: reference[field] for field in ('feature_id', 'check_id')}
                 for row in scenario_rows for reference in row['references']]
        handoff_path = self.artifact(f'inputs/{slice_id}-r{item["revision"]}-handoff.json', json.dumps({
            'record_type': 'independent_no_code_walkthrough', 'reviewer': 'fresh-reader',
            'context': 'fresh_session', 'test_execution': False, 'source_paths': [item['guide_path']],
            'cases': cases,
        }))
        value = {'action': action, 'slice_id': slice_id, 'proposal_revision': item['revision'],
                 'user_response': f'{action} {slice_id} revision {item["revision"]}',
                 'guide_sha256': item['guide_sha256'], 'contract_sha256': item['contract_sha256'],
                 'conversation_exchange_ids': list(exchanges),
                 'handoff_review': {'slice_id': slice_id, 'proposal_revision': item['revision'],
                     'guide_sha256': item['guide_sha256'], 'contract_sha256': item['contract_sha256'],
                     'reviewer': 'fresh-reader', 'result': 'PASS',
                     'walkthrough': {'mode': 'no_code', 'context': 'fresh_session', 'independent': True,
                         'performed': True, 'test_execution': False, 'evidence_path': handoff_path,
                         'attestation': 'Read the frozen guide and traced existing service paths without implementing or running tests.'},
                     'findings': [{'section': 'State and scenario matrix', 'citation': f'line {section_line + 1}',
                         'finding': 'The guarded member and outsider outcomes are explicit.',
                         'resolution': 'Confirmed the stated outcomes against the approved contract.'},
                         *normative_findings],
                     'source_paths': [item['guide_path']],
                     'read_only_scenarios': scenario_rows,
                     'acceptance_checks': [{'check_id': check['check_id'], 'expected': check['expected'],
                         'observed': check['expected'], 'result': 'PASS'} for check in checks],
                     'evidence_paths': [handoff_path]} }
        if action == 'delegate':
            value.update(delegated_slice_ids=[slice_id], constraints='Validate only already approved behavior')
        return value

    def collection_input(self):
        path = self.artifact('inputs/collection-review.md', 'Reviewed existing member and outsider journeys and fixture coverage.')
        selected = self.manifest()['slice_review']['selected']
        bindings = {key: {'proposal_revision': entry['revision'], 'guide_sha256': entry['guide_sha256'],
                          'contract_sha256': entry['contract_sha256']} for key, entry in selected.items()}
        sources = [entry['guide_path'] for entry in selected.values()]
        packets = {key: json.loads((self.directory / entry['contract_path']).read_text())
                   for key, entry in selected.items()}
        features = {feature['feature_id']: feature for packet in packets.values()
                    for feature in packet['feature_contracts']}
        case_conclusions = [{'feature_id': feature_id, 'check_id': check['check_id'], 'result': 'PASS',
                             'expected': check['expected'], 'source_path': sources[0],
                             'citation': f'check:{check["check_id"]}',
                             'conclusion': f'Traced the approved expected outcome and verification source for {check["check_id"]}.'}
                            for feature_id, feature in sorted(features.items()) for check in feature['acceptance_checks']]
        for feature_id, feature in sorted(features.items()):
            for case in feature.get('visual_contract', {}).get('render_cases', []):
                viewport = case['viewport']
                case_id = case['case_id']
                case_conclusions.append({
                    'feature_id': feature_id, 'check_id': case_id, 'result': 'PASS', 'source_path': sources[0],
                    'expected': f'renders {case["route"]} at {viewport["width"]}x{viewport["height"]} with {case["ready_selector"]}',
                    'citation': f'check:{case_id}',
                    'conclusion': f'Traced the route, viewport and ready selector for {case_id}.',
                })
        interfaces = {}
        for slice_id, packet in packets.items():
            for name, contract in packet['shared_contracts'].items():
                contract_path = selected[slice_id]['contract_path']
                lines = (self.directory / contract_path).read_text().splitlines()
                citation_line = next(index for index, line in enumerate(lines, 1) if f'"{name}": {{' in line)
                record = interfaces.setdefault(name, {'interface_id': name, 'provider_slice_id': None,
                    'consumer_slice_ids': [], 'result': 'PASS',
                    'source_path': contract_path, 'citation': f'line {citation_line}',
                    'conclusion': f'Compared provider and consumer contracts for {name}.'})
                if contract['role'] == 'provides':
                    record['provider_slice_id'] = slice_id
                else:
                    record['consumer_slice_ids'].append(slice_id)
        boundary_conclusions = []
        for left_id, right_id in sorted(SLICE_EVIDENCE_MODULE._shared_boundary_pairs(packets)):
            source_path = selected[left_id]['guide_path']
            boundary_conclusions.append({'slice_ids': [left_id, right_id], 'result': 'PASS',
                'conclusion': f'Compared approved shared boundaries for {left_id} and {right_id}.',
                'source_path': source_path, 'citation': 'line 1'})
        return {'semantic_checks': {key: {'result': 'PASS', 'explanation': 'Compared affected sections and fixture outcomes.',
                                         'schema_version': 2,
                                         'reviewer': {'name': 'collection-reader', 'context': 'fresh_session',
                                                      'independent_of_authoring': True},
                                         'case_conclusions': case_conclusions,
                                         'interface_conclusions': list(interfaces.values()) if key == 'shared_contract_review' else [],
                                         'boundary_conclusions': boundary_conclusions if key == 'shared_contract_review' else [],
                                         'revision_bindings': bindings,
                                         'covered_feature_ids': sorted(features),
                                         'source_paths': sources,
                                         'evidence_paths': [path]}
                  for key in ('decision_fidelity', 'shared_contract_review', 'end_to_end_journeys', 'verification_coverage')}}

    def lock_reviewed_package(self, version=1):
        package_id = f'package-v{version}'
        report = self.review_cli('slice-collection-validate', self.collection_input())
        self.assertEqual(report['result'], 'PASS')
        manifest = self.manifest()['slice_review']
        preview = self.review_cli('slice-package-preview')
        self.assertEqual(preview['package_id'], package_id)
        self.assertEqual(preview['collection_path'], report['path'])
        recap = preview['recap']
        summary = self.review_cli('slice-package-lock', {
            'package_id': package_id, 'collection_path': report['path'],
            'collection_sha256': manifest['artifact_hashes'][report['path']],
            'recap_path': self.artifact('inputs/recap.md', recap),
            'approved_by_user': True, 'user_response': f'Approve locking {package_id}.',
        })
        self.assertEqual(summary['active_package'], package_id)
        return {'package_id': package_id, 'package_hash': summary['packages'][package_id]['package_hash']}

    def authorize_fixture(self, package):
        return self.review_cli('implementation-authorize', package | {
            'instruction': 'start implementation', 'authorized_by_user': True,
            'user_response': f'Start implementation of {package["package_id"]}.',
        })

    def test_sr002_004_005_007_complete_inventory_sequential_reviews_and_scoped_revision(self):
        feature = self.reviewed_blueprint(multiple=True)
        first = self.guide_candidate(feature)
        second = self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py', deps=('items-slice',))
        third = self.guide_candidate(feature, 'view-slice', path='src/view.py')
        summary = self.review_cli('slice-plan', {'slices': [first, second, third]})
        self.assertEqual(summary['current_slice_id'], 'items-slice')
        for entry in summary['slices'].values():
            self.assertTrue((self.directory / entry['guide_path']).is_file())
        summary = self.review_cli('slice-review-record', self.review_input())
        self.assertEqual(summary['current_slice_id'], 'adapter-slice')
        self.assertEqual(self.manifest()['slices'], {})
        summary = self.review_cli('slice-review-record', self.review_input('view-slice', action='delegate'))
        self.assertEqual(summary['reviews']['view-slice']['result'], 'delegated_validated')
        self.assertNotIn('adapter-slice', summary['reviews'])
        self.review_cli('slice-review-record', self.review_input('adapter-slice'))
        summary = self.review_cli('slice-draft', self.guide_candidate(feature, revision=2))
        self.assertEqual(summary['changed_dependents'], ['adapter-slice'])
        self.assertEqual(set(summary['reviews']), {'view-slice'})
        self.assertTrue((self.directory / 'slice-proposals/items-slice/revisions/1/contract.json').exists())

    def test_sr003_reports_exact_missing_check_and_state(self):
        feature = self.reviewed_blueprint()
        for missing in ('items-access', 'forbidden'):
            with self.subTest(missing=missing):
                candidate = self.guide_candidate(feature, missing_trace=missing)
                result = self.review_cli('slice-plan', {'slices': [candidate]}, ok=False)
                self.assertIn('Traceability matrix', result.stderr)
                self.assertIn(missing, result.stderr)

    def test_sr006_012_delegation_stops_for_exact_resumable_question(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        question = 'Where should a member retry when the service times out?'
        self.exchange('retry-question', status='open', phase='slice_review', question=question,
                      feature_ids=['items-feature'])
        self.review_cli('slice-review-record', {'action': 'question', 'slice_id': 'items-slice',
                                               'proposal_revision': 1, 'pending_question_id': 'retry-question'})
        result = self.review_cli('slice-review-record', self.review_input(action='delegate'), ok=False)
        self.assertIn('Unanswered consequential question', result.stderr)
        resumed = json.loads(self.cli('resume', '--format', 'json', ok=False).stdout)
        self.assertEqual(resumed['slice_review']['current_revision'], 1)
        self.assertEqual(resumed['slice_review']['pending_question_id'], 'retry-question')
        self.assertEqual(resumed['pending_questions'][0]['question'], question)
        self.assertEqual(resumed['next_action'], question)
        self.assertEqual(resumed['slice_review']['reviews'], {})

    def test_resolved_slice_clarification_is_frozen_without_blueprint_reapproval(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        question = 'Does the existing membership guard run before reading storage?'
        self.exchange('guard-question', status='open', phase='slice_review', question=question,
                      feature_ids=['items-feature'])
        self.review_cli('slice-review-record', {'action': 'question', 'slice_id': 'items-slice',
                                               'proposal_revision': 1, 'pending_question_id': 'guard-question'})
        self.exchange('guard-answer', phase='slice_review', question=question, responds_to='guard-question',
                      response='Yes, keep the guard before the storage call.', feature_ids=['items-feature'],
                      record_type='slice_detail', slice_id='items-slice', proposal_revision=1,
                      clarifies_decision_ids=['choose-layout'])
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['active_work']['slice_review']['pending_question_id'], 'guard-question')
        self.assertEqual(resumed['active_question_answer']['response'], 'Yes, keep the guard before the storage call.')
        self.review_cli('slice-draft', self.guide_candidate(feature, revision=2, detail='guard-answer'))
        result = self.review_cli('slice-review-record', self.review_input(exchanges=('guard-question', 'guard-answer')))
        self.assertEqual(result['reviews']['items-slice']['proposal_revision'], 2)
        self.assertEqual(self.manifest()['approved_version'], 'spec-v1')

    def test_reopening_sibling_after_answer_clears_stale_waiting_cursor(self):
        feature = self.reviewed_blueprint(multiple=True)
        first = self.guide_candidate(feature) | {'dependency_contracts': []}
        second = self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py') | {
            'dependency_contracts': []}
        self.review_cli('slice-plan', {'slices': [first, second]})
        question = 'Does the existing membership guard run before reading storage?'
        self.exchange('cursor-question', status='open', phase='slice_review', question=question,
                      feature_ids=['items-feature'])
        self.review_cli('slice-review-record', {'action': 'question', 'slice_id': 'items-slice',
            'proposal_revision': 1, 'pending_question_id': 'cursor-question'})
        self.exchange('cursor-answer', phase='slice_review', question=question,
                      responds_to='cursor-question', response='Yes, preserve the existing guard.',
                      feature_ids=['items-feature'], record_type='slice_detail', slice_id='items-slice',
                      proposal_revision=1, clarifies_decision_ids=['choose-layout'])

        summary = self.review_cli('slice-review-record', {'action': 'reopen', 'slice_id': 'adapter-slice',
            'proposal_revision': 1, 'user_response': 'Reopen the adapter slice.'})
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)

        self.assertEqual(summary['current_slice_id'], 'adapter-slice')
        self.assertIsNone(summary['pending_question_id'])
        self.assertEqual(resumed['active_work']['status'], 'in_progress')
        self.assertIsNone(resumed['active_work'].get('pending_question_id'))
        self.assertEqual(resumed['next_action'], 'Review adapter-slice revision 1')

    def test_superseded_blueprint_decision_deactivates_slice_clarification(self):
        feature = self.reviewed_blueprint()
        candidate = self.guide_candidate(feature) | {'dependency_contracts': []}
        self.review_cli('slice-plan', {'slices': [candidate]})
        question = 'Does the existing membership guard run before reading storage?'
        self.exchange('scope-question', status='open', phase='slice_review', question=question,
                      feature_ids=['items-feature'])
        self.exchange('scope-answer', phase='slice_review', question=question,
                      responds_to='scope-question', response='Yes, preserve the existing guard.',
                      feature_ids=['items-feature'], record_type='slice_detail', slice_id='items-slice',
                      proposal_revision=1, clarifies_decision_ids=['choose-layout'])
        revised = self.guide_candidate(feature, revision=2, detail='scope-answer') | {'dependency_contracts': []}
        self.review_cli('slice-draft', revised)
        self.exchange('replacement-layout', phase='slice_review', feature_ids=['list-items'],
                      slice_id='items-slice', supersedes='choose-layout',
                      response='Use the newly approved guard sequence.')

        state = FIXTURE_CLI.slice_review_state(self.directory, self.manifest())

        self.assertEqual(state.active_slice_details('items-slice'), [])

    def test_locked_package_guard_rejects_frozen_clarifications_that_are_no_longer_active(self):
        feature = self.reviewed_blueprint()
        candidate = self.guide_candidate(feature) | {'dependency_contracts': []}
        self.review_cli('slice-plan', {'slices': [candidate]})
        question = 'Does the existing membership guard run before reading storage?'
        self.exchange('locked-scope-question', status='open', phase='slice_review', question=question,
                      feature_ids=['items-feature'])
        self.exchange('locked-scope-answer', phase='slice_review', question=question,
                      responds_to='locked-scope-question', response='Yes, preserve the existing guard.',
                      feature_ids=['items-feature'], record_type='slice_detail', slice_id='items-slice',
                      proposal_revision=1, clarifies_decision_ids=['choose-layout'])
        revised = self.guide_candidate(feature, revision=2, detail='locked-scope-answer') | {'dependency_contracts': []}
        self.review_cli('slice-draft', revised)
        self.review_cli('slice-review-record', self.review_input(exchanges=('locked-scope-question',
                                                                            'locked-scope-answer')))
        self.lock_reviewed_package()
        package = json.loads((self.directory / 'slice-review/packages/package-v1/package-manifest.json').read_text())
        packet = json.loads((self.directory / package['slices']['items-slice']['contract_path']).read_text())
        self.assertIn('locked-scope-answer', packet['decision_ids'])
        state = FIXTURE_CLI.slice_review_state(self.directory, self.manifest())

        with patch.object(state, 'active_slice_details', return_value=[]):
            with self.assertRaisesRegex(FIXTURE_CLI.StateError,
                                        'locked package contains a clarification tied to a superseded'):
                state.validate_package_active_decisions(package)

    def test_replacement_slice_records_its_cancelled_predecessor(self):
        feature = self.reviewed_blueprint(multiple=True)
        initial = self.guide_candidate(feature) | {'dependency_contracts': []}
        self.review_cli('slice-plan', {'slices': [initial]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        self.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        self.cli('slice-cancel', '--slice-id', 'items-slice', '--reason', 'Replace the contract after review.')

        replacement = self.guide_candidate(feature, 'items-slice-v2', path='src/adapter.py') | {
            'dependency_contracts': [], 'replaces_slice_ids': ['items-slice']}
        amendment = {'amendment': 'Replace the cancelled reviewed assignment.',
            'user_response': 'Replace items-slice with items-slice-v2.'}
        missing_lineage = replacement.copy()
        missing_lineage.pop('replaces_slice_ids')
        blocked = self.review_cli('slice-plan', amendment | {'slices': [missing_lineage]}, ok=False)
        self.assertIn('declare the predecessor in replaces_slice_ids', blocked.stderr)
        summary = self.review_cli('slice-plan', amendment | {'slices': [replacement]})

        self.assertEqual(summary['slices']['items-slice-v2']['replaces_slice_ids'], ['items-slice'])

    def test_proposal_cannot_add_a_surface_absent_from_blueprint(self):
        feature = self.reviewed_blueprint()
        candidate = self.guide_candidate(feature, surfaces=('backend', 'mcp'))
        result = self.review_cli('slice-plan', {'slices': [candidate]}, ok=False)
        self.assertIn("unapproved surfaces ['mcp']", result.stderr)

    def test_sr008_collection_detects_conflicting_contracts_and_ownership(self):
        feature = self.reviewed_blueprint()
        provider = self.guide_candidate(feature, shared={'items-schema': {'role': 'provides', 'definition': {'type': 'array'}}})
        consumer = self.guide_candidate(feature, 'consumer-slice', deps=('items-slice',),
                                        shared={'items-schema': {'role': 'consumes', 'definition': {'type': 'object'}}})
        self.review_cli('slice-plan', {'slices': [provider, consumer]})
        for key in ('items-slice', 'consumer-slice'):
            self.review_cli('slice-review-record', self.review_input(key))
        response = self.review_cli('slice-collection-validate', self.collection_input(), ok=False)
        checks = json.loads(response.stdout)['checks']
        self.assertIn('items-schema', checks['contract_consistency']['explanation'])
        self.assertIn('consumer-slice', checks['source_ownership']['explanation'])

    def test_sr009_010_separate_authorization_releases_exact_package_and_paths(self):
        feature = self.reviewed_blueprint(multiple=True)
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature),
            self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py', deps=('items-slice',))]})
        for key in ('items-slice', 'adapter-slice'):
            self.review_cli('slice-review-record', self.review_input(key))
        package = self.lock_reviewed_package()
        release = package | {'slice_id': 'items-slice'}
        result = self.review_cli('slice-release', release, ok=False)
        self.assertIn('separate explicit start authorization', result.stderr)
        self.assertEqual(self.manifest()['slices'], {})
        self.assertIn('exact locked', self.review_cli(
            'implementation-authorize', package | {'package_hash': 'other'}, ok=False).stderr)
        old_approval = self.manifest()['slice_review']['reviews']['items-slice']['user_response']
        invalid_authorization = package | {'instruction': 'start implementation', 'authorized_by_user': True,
                                           'user_response': old_approval}
        self.assertIn('separate explicit user instruction', self.review_cli(
            'implementation-authorize', invalid_authorization, ok=False).stderr)
        self.authorize_fixture(package)
        self.assertIn('dependencies first', self.review_cli(
            'slice-release', package | {'slice_id': 'adapter-slice'}, ok=False).stderr)
        self.review_cli('slice-release', release)
        self.review_cli('slice-release', release)
        self.cli('validate')
        state = self.manifest()
        self.assertEqual(set(state['slices']), {'items-slice'})
        self.assertEqual(state['slices']['items-slice']['allowed_paths'], ['src/service.py'])
        contract = json.loads((self.directory / 'slices/items-slice/contract.json').read_text())
        self.assertEqual(contract['package_hash'], package['package_hash'])
        source_guide = (self.directory / contract['locked_guide_path']).read_bytes()
        delivered_guide_path = self.directory / contract['delivery_guide_path']
        delivered_guide = delivered_guide_path.read_text()
        self.assertNotEqual(delivered_guide.encode(), source_guide)
        self.assertIn('Package: `package-v1`', delivered_guide)
        self.assertNotIn(package['package_hash'], delivered_guide)
        self.assertIn('Review outcome: `approved`', delivered_guide)
        self.assertIn('Review mode: manual.', delivered_guide)
        self.assertIn('[Original authored guide](implementation-guide.md)', delivered_guide)
        self.assertIn('[Review receipt](', delivered_guide)
        self.assertIn('reviewer', delivered_guide.lower())
        self.assertNotIn('Outcome: pending review.', delivered_guide)
        receipt_path = self.manifest()['slice_review']['reviews']['items-slice']['path']
        receipt_line = next(line for line in delivered_guide.splitlines() if '[Review receipt](' in line)
        receipt_link = delivered_guide_path.parent / receipt_line.split('[Review receipt](', 1)[1].rstrip(')')
        self.assertTrue(receipt_link.resolve().is_file())
        self.assertEqual(receipt_link.resolve(), (self.directory / receipt_path).resolve())
        package_manifest = json.loads((self.directory / 'slice-review/packages/package-v1/package-manifest.json').read_text())
        self.assertEqual(package_manifest['slices']['items-slice']['delivery_guide_path'],
                         contract['delivery_guide_path'])
        self.assertEqual(package_manifest['artifact_hashes'][contract['delivery_guide_path']],
                         hashlib.sha256(delivered_guide_path.read_bytes()).hexdigest())
        self.assertIn(contract['delivery_guide_path'], self.manifest()['slice_review']['artifact_hashes'])
        handoff = json.loads(self.cli('resume', '--slice-id', 'items-slice', '--format', 'json').stdout)
        self.assertEqual(handoff['delivery_handoff']['package_id'], 'package-v1')
        self.assertEqual(handoff['delivery_handoff']['package_hash'], package['package_hash'])
        self.assertEqual(handoff['delivery_handoff']['guide_path'], contract['delivery_guide_path'])
        self.assertEqual(handoff['delivery_handoff']['contract_guide_path'], contract['delivery_guide_path'])
        original_delivery = delivered_guide_path.read_bytes()
        try:
            for damaged in (original_delivery + b'\nTampered.\n', None):
                if damaged is None:
                    delivered_guide_path.unlink()
                else:
                    delivered_guide_path.write_bytes(damaged)
                blocked = self.cli('validate', ok=False)
                self.assertIn(contract['delivery_guide_path'], blocked.stderr)
        finally:
            delivered_guide_path.write_bytes(original_delivery)
        self.cli('validate')

    def test_legacy_locked_guide_fields_remain_readable_for_re_release(self):
        authored_path = 'slice-proposals/items-slice/revisions/1/implementation-guide.md'
        authored_hash = 'a' * 64
        legacy_slice = {'guide_path': authored_path, 'guide_sha256': authored_hash}
        self.assertEqual(SLICE_REVIEW_MODULE.package_guide(legacy_slice), (authored_path, authored_hash))
        self.assertEqual(SLICE_REVIEW_MODULE.released_guide({}, legacy_slice), (authored_path, authored_hash))
        self.assertEqual(SLICE_REVIEW_MODULE.released_guide(
            {'guide_path': authored_path, 'guide_sha256': authored_hash}, legacy_slice),
            (authored_path, authored_hash))

    def accept_first_released_slice(self):
        report = {'owner': 'builder', 'slice_id': 'items-slice', 'summary': 'Verified guarded list behavior',
                  'status': 'submitted', 'implementation_paths': ['src/service.py'],
                  'functional_evidence': ['Member and outsider checks'], 'visual_evidence': [], 'blockers': [],
                  'next_action': 'Verify independent observations'}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('worker', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('delivery-review', self.review()))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')

    def test_integration_waits_for_unreleased_package_slices_and_freezes_dependent_contract(self):
        feature = self.reviewed_blueprint(multiple=True)
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature),
            self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py', deps=('items-slice',))]})
        for key in ('items-slice', 'adapter-slice'):
            self.review_cli('slice-review-record', self.review_input(key))
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        self.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        self.accept_first_released_slice()
        self.assertEqual(self.manifest()['stage'], 'implement')
        dependency_source = self.root / 'src/service.py'
        original_source = dependency_source.read_bytes()
        dependency_source.write_bytes(original_source + b'\n# Changed after verification\n')
        blocked = self.review_cli('slice-release', package | {'slice_id': 'adapter-slice'}, ok=False)
        self.assertIn('accepted dependency items-slice is stale', blocked.stderr)
        dependency_source.write_bytes(original_source)
        self.review_cli('slice-release', package | {'slice_id': 'adapter-slice'})
        dependent_contract = self.directory / 'slices/adapter-slice/contract.json'
        original_contract = dependent_contract.read_bytes()
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'block', '--review-file',
                 self.payload('blocker', {'slice_id': 'items-slice', 'reason': 'Investigate reported list inconsistency',
                    'next_action': 'Check the member fixtures',
                    'evidence_files': [self.artifact('slices/items-slice/evidence/incident.txt', 'Observed inconsistency')]}))
        self.assertEqual(self.manifest()['slices']['adapter-slice']['status'], 'blocked')
        self.assertEqual(dependent_contract.read_bytes(), original_contract)
        self.cli('validate')

    def test_unaffected_ready_assignment_continues_during_and_after_package_amendment(self):
        feature = self.reviewed_blueprint(multiple=True)
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature),
            self.guide_candidate(feature, 'adapter-slice', path='src/adapter.py')]})
        for key in ('items-slice', 'adapter-slice'):
            self.review_cli('slice-review-record', self.review_input(key))
        first = self.lock_reviewed_package()
        self.authorize_fixture(first)
        self.review_cli('slice-release', first | {'slice_id': 'items-slice'})
        delivery = self.directory / 'slices/items-slice/contract.json'
        original = delivery.read_bytes()
        self.review_cli('slice-review-record', {'slice_id': 'adapter-slice', 'proposal_revision': 1,
            'action': 'reopen', 'user_response': 'Reopen the adapter slice.'})
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['slices']['items-slice'], 'ready')
        self.assertEqual(resumed['dispatchable_slices'], ['items-slice'])
        self.assertEqual(resumed['slice_review']['continuing_releases'], {'items-slice': 'package-v1'})
        question = 'Should the adapter preserve the existing response shape?'
        self.exchange('adapter-question', phase='slice_review', status='open', question=question)
        scoped_resume = json.loads(self.cli(
            'resume', '--slice-id', 'items-slice', '--format', 'json').stdout)
        self.assertTrue(scoped_resume['safe_to_continue'])
        self.assertEqual(scoped_resume['slice_review']['pending_question_id'], 'adapter-question')
        self.cli('validate', '--slice-id', 'items-slice')
        self.cli('validate', '--slice-id', 'adapter-slice', ok=False)
        source = self.root / 'src/service.py'
        source.write_text(source.read_text() + '\n# Work continued under the unchanged contract.\n')
        report = {'owner': 'builder', 'slice_id': 'items-slice', 'summary': 'Preserved verified service behavior',
                  'status': 'submitted', 'implementation_paths': ['src/service.py']}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('unchanged-worker', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        self.assertEqual(self.manifest()['stage'], 'slice_review')
        self.exchange('adapter-answer', phase='slice_review', status='rejected', question=question,
                      responds_to='adapter-question', response='Reject the proposed response change.')
        self.review_cli('slice-review-record', self.review_input(
            'adapter-slice', exchanges=('adapter-question', 'adapter-answer')))
        second = self.lock_reviewed_package(version=2)
        self.assertIn('separate explicit start', self.review_cli(
            'slice-release', second | {'slice_id': 'adapter-slice'}, ok=False).stderr)
        self.authorize_fixture(second)
        self.review_cli('slice-release', second | {'slice_id': 'items-slice'})
        self.assertEqual(self.manifest()['slices']['items-slice']['status'], 'reported')
        self.assertEqual(delivery.read_bytes(), original)
        current_handoff = json.loads(self.cli(
            'resume', '--slice-id', 'items-slice', '--format', 'json').stdout)['delivery_handoff']
        contract = json.loads(delivery.read_text())
        self.assertEqual(current_handoff['package_id'], 'package-v2')
        self.assertEqual(current_handoff['guide_path'],
                         'slice-proposals/items-slice/revisions/1/implementation-guide-package-v2.md')
        self.assertEqual(current_handoff['contract_guide_path'], contract['delivery_guide_path'])
        current_manifest = json.loads((self.directory / 'slice-review/packages/package-v2/package-manifest.json').read_text())
        self.assertEqual(current_manifest['slices']['items-slice']['delivery_guide_path'],
                         current_handoff['guide_path'])
        source_path = current_manifest['slices']['items-slice']['guide_path']
        source_hash = current_manifest['slices']['items-slice']['guide_sha256']
        self.assertEqual(contract['locked_guide_path'], source_path)
        self.assertEqual(current_manifest['artifact_hashes'][source_path], source_hash)
        self.assertEqual(hashlib.sha256((self.directory / source_path).read_bytes()).hexdigest(), source_hash)
        self.cli('validate')

    def test_interrupted_release_rechecks_source_before_recovery(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        release = self.payload('release', package | {'slice_id': 'items-slice'})
        module = FIXTURE_CLI
        recover = module.recover_workflow_transition

        def interrupt(directory, request, *, replay=True):
            if not replay and (directory / '.workflow-transition.json').exists():
                raise OSError('simulated interruption')
            return recover(directory, request, replay=replay)

        with patch.object(module, 'recover_workflow_transition', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'simulated interruption'):
                module.run_slice_review(self.directory, 'slice-release', str(release))
        drift = self.root / 'outside.py'
        drift.write_text('print("outside authorized paths")\n')
        self.assertIn('outside.py', self.cli('slice-release', '--input-file', release, ok=False).stderr)
        self.assertFalse((self.directory / 'slices/items-slice').exists())
        drift.unlink()
        self.cli('slice-release', '--input-file', release)
        self.cli('validate')

    def test_revising_locked_package_preserves_old_lock_and_requires_new_start(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        first = self.lock_reviewed_package()
        self.authorize_fixture(first)
        original_lock = self.directory / 'slice-review/packages/package-v1/lock-receipt.json'
        before = original_lock.read_bytes()
        self.review_cli('slice-draft', self.guide_candidate(feature, revision=2))
        self.review_cli('slice-review-record', self.review_input())
        second = self.lock_reviewed_package(version=2)
        self.assertNotEqual(first['package_hash'], second['package_hash'])
        self.assertEqual(original_lock.read_bytes(), before)
        self.assertIn('separate explicit start', self.review_cli(
            'slice-release', second | {'slice_id': 'items-slice'}, ok=False).stderr)
        self.assertIn('exact locked', self.review_cli(
            'slice-release', first | {'slice_id': 'items-slice'}, ok=False).stderr)
        self.assertIn('separate explicit user instruction', self.review_cli('implementation-authorize', second | {
            'instruction': 'start implementation', 'authorized_by_user': True,
            'user_response': 'Start implementation of package-v1.'}, ok=False).stderr)
        self.authorize_fixture(second)
        self.review_cli('slice-release', second | {'slice_id': 'items-slice'})

    def test_sr011_013_changed_or_missing_frozen_handoff_blocks_release(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        guide = self.directory / self.manifest()['slice_review']['selected']['items-slice']['guide_path']
        original = guide.read_bytes()
        guide.write_bytes(original + b'\nDrift')
        result = self.review_cli('slice-release', package | {'slice_id': 'items-slice'}, ok=False)
        self.assertIn('Changed protected evidence', result.stderr)
        guide.unlink()
        self.assertIn('Missing approved artifact', self.cli('validate', ok=False).stderr)

    def test_sr014_surface_annexes_are_required_and_schema_checked(self):
        feature = self.reviewed_blueprint(extra_surfaces=True)
        candidate = self.guide_candidate(feature, surfaces=('backend', 'api', 'mcp', 'script'))
        path = self.directory / candidate['guide_path']
        valid = path.read_text()
        path.write_text(valid.replace('"output":{"type":"array"}', '"bogus":{"type":"array"}'))
        result = self.review_cli('slice-plan', {'slices': [candidate]}, ok=False)
        self.assertIn('MCP annex', result.stderr)
        self.assertIn('output', result.stderr)
        path.write_text(valid)
        self.review_cli('slice-plan', {'slices': [candidate]})
        self.review_cli('slice-guide-validate', {'slice_id': 'items-slice'})

    def test_sr014_ui_annex_checks_the_approved_visual_token(self):
        feature = self.reviewed_blueprint(surface='ui')
        candidate = self.guide_candidate(feature, surfaces=('ui',))
        path = self.directory / candidate['guide_path']
        valid = path.read_text()
        path.write_text(valid.replace('gap = 16px', 'gap = 17px'))
        self.assertIn('Layout and tokens differs from the approved contract', self.review_cli(
            'slice-plan', {'slices': [candidate]}, ok=False).stderr)
        path.write_text(valid)
        self.review_cli('slice-plan', {'slices': [candidate]})

    def test_sr015_report_halts_active_authorized_release_until_reapproval(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        self.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        report = {'report_id': 'missing-items', 'suspected_features': ['list-items'],
                  'summary': 'The forbidden outcome was omitted from an implementation proposal.',
                  'evidence_paths': [self.artifact('inputs/missing-items.md', 'Observed absent forbidden outcome.')]}
        self.cli('missing-feature', '--report-file', self.payload('missing-report', report))
        response = self.review_cli('slice-release', package | {'slice_id': 'items-slice'}, ok=False)
        self.assertIn('follow-up sweep', response.stderr.lower())
        audit = self.baseline(audit_id='audit-v2')
        audit['sweeps'] = [audit['sweeps'][-1]]
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan('plan-v2', 'audit-v2'))
        second_spec = self.blueprint('spec-v2') | {'baseline_id': 'audit-v2', 'plan_version': 'plan-v2'}
        self.cli('blueprint-record', '--blueprint-file', self.payload('followup-spec', second_spec))
        self.cli('blueprint-coverage', '--coverage-file', self.payload(
            'followup-coverage', self.coverage('spec-v2') | {'baseline_id': 'audit-v2'}))
        self.cli('approve', '--approval-file', self.payload('followup-approval', self.approval('spec-v2')))
        replacement = self.guide_candidate(second_spec['features'][0], 'replacement-slice') | {
            'replaces_slice_ids': ['items-slice']}
        plan = self.review_cli('slice-plan', {'slices': [replacement]})
        self.assertEqual(plan['slices']['replacement-slice']['replaces_slice_ids'], ['items-slice'])
        self.review_cli('slice-review-record', self.review_input('replacement-slice'))
        new_package = self.lock_reviewed_package(version=2)
        package_manifest = json.loads((self.directory / 'slice-review/packages/package-v2/package-manifest.json').read_text())
        self.assertEqual(package_manifest['slices']['replacement-slice']['replaces_slice_ids'], ['items-slice'])
        self.assertIn('separate explicit start', self.review_cli(
            'slice-release', new_package | {'slice_id': 'replacement-slice'}, ok=False).stderr)
        self.authorize_fixture(new_package)
        self.review_cli('slice-release', new_package | {'slice_id': 'replacement-slice'})
        self.assertEqual(self.manifest()['slices']['items-slice']['superseded_by'], 'approve-spec-v2')
        self.cli('validate')

    def test_sr017_interrupted_start_recovers_once(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        file = self.payload('auth', self.with_slice_consent('implementation-authorize', package | {
            'instruction': 'start implementation', 'authorized_by_user': True,
            'user_response': 'Start implementation of package-v1.'}))
        module = FIXTURE_CLI
        recover = module.recover_workflow_transition

        def interrupt(directory, request, *, replay=True):
            if not replay and (directory / '.workflow-transition.json').exists():
                raise OSError('simulated interruption before artifacts are written')
            return recover(directory, request, replay=replay)

        with patch.object(module, 'recover_workflow_transition', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'simulated interruption'):
                module.run_slice_review(self.directory, 'implementation-authorize', str(file))
        self.assertIn('Interrupted implementation-authorize', self.cli('validate', ok=False).stderr)
        self.cli('implementation-authorize', '--input-file', file)
        self.cli('implementation-authorize', '--input-file', file)
        log = [json.loads(line) for line in (self.directory / 'decisions.jsonl').read_text().splitlines()]
        self.assertEqual(sum(event['type'] == 'slice_implementation_authorized' for event in log), 1)
        self.cli('validate')

    def test_reconcile_cannot_advance_a_released_slice_after_review_reopens(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', self.review_input())
        package = self.lock_reviewed_package()
        self.authorize_fixture(package)
        self.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        report = {'owner': 'builder', 'slice_id': 'items-slice', 'summary': 'Observed fixture behavior',
                  'status': 'submitted', 'implementation_paths': ['src/service.py']}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('worker', report))
        self.review_cli('slice-review-record', {'action': 'reopen', 'slice_id': 'items-slice',
                                                'proposal_revision': 1, 'user_response': 'Reopen this slice.'})
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report', ok=False)
        self.assertIn('Lock the reviewed collection', result.stderr)
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['slices']['items-slice'], 'paused')
        self.assertEqual(resumed['dispatchable_slices'], [])

    def test_migration_preserves_in_progress_delivery_and_requires_current_blueprint(self):
        self.assigned()
        original = self.manifest()
        self.review_cli('slice-review-migrate', {'approved_by_user': True,
                                                'user_response': 'Migrate this initiative to review gates.'})
        current = self.manifest()
        self.assertEqual(current['slices'], original['slices'])
        self.assertEqual(current['approvals'], original['approvals'])
        result = self.review_cli('slice-plan', {'slices': []}, ok=False)
        self.assertIn('new blueprint approval', result.stderr)

    def test_review_blocker_is_durable_and_prevents_lock(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        self.review_cli('slice-review-record', {'action': 'block', 'slice_id': 'items-slice',
            'proposal_revision': 1, 'reason': 'The no-code handoff found an ambiguous role.',
            'evidence_paths': [self.artifact('inputs/open-role.md', 'The reviewer found an unspecified role.')]})
        resumed = self.review_cli('slice-register')
        self.assertIn('ambiguous role', resumed['review_blockers']['items-slice']['reason'])
        result = self.review_cli('slice-collection-validate', self.collection_input(), ok=False)
        self.assertIn('Open review blockers', json.loads(result.stdout)['checks']['review_validity']['explanation'])

    def test_sr018_unrelated_workspace_change_blocks_review_gate(self):
        feature = self.reviewed_blueprint()
        self.review_cli('slice-plan', {'slices': [self.guide_candidate(feature)]})
        (self.root / 'unrelated.py').write_text('print("unrelated change")\n')
        result = self.review_cli('slice-review-record', self.review_input(), ok=False)
        self.assertIn('unrelated.py', result.stderr)
        self.assertIn('unrelated.py', self.cli('validate', ok=False).stderr)

    def test_approval_requires_coverage_recap_and_explicit_exceptions(self):
        self.prepared()
        approval = self.approval()
        for field in ('coverage', 'decision_recap_path', 'exceptions'):
            with self.subTest(field=field):
                incomplete = dict(approval)
                if field == 'coverage':
                    incomplete['shown_paths'] = [p for p in approval['shown_paths'] if not p.startswith('blueprint-coverage/')]
                else:
                    del incomplete[field]
                result = self.cli('approve', '--approval-file', self.payload('incomplete', incomplete), ok=False)
                self.assertIn({'coverage':'coverage', 'decision_recap_path':'recap', 'exceptions':'exceptions'}[field],
                              result.stderr.lower())
        approval['exceptions'] = ['The approved plan does not modify archived items.']
        self.cli('approve', '--approval-file', self.payload('approval', approval))
        saved = self.manifest()['approvals'][approval['decision_id']]
        self.assertEqual(saved['exceptions'], approval['exceptions'])
        self.assertIn(approval['decision_recap_path'], saved['artifact_hashes'])
        self.cli('validate')
        self.artifact(approval['decision_recap_path'], 'Changed after approval')
        self.cli('validate', ok=False)

    def test_fullstack_feature_needs_its_own_backend_acceptance_check(self):
        self.start()
        self.exchange()
        audit = self.baseline('ui')
        audit['surface'] = 'fullstack'
        audit['behavior_contracts']['list-items']['backend_contract'] = self.backend_contract()
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint(surface='ui')
        spec['surface'] = 'fullstack'
        spec['features'][0]['backend_contract'] = self.backend_contract()
        result = self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)
        self.assertIn('backend acceptance check', result.stderr)
        backend_check = self.blueprint()['features'][0]['acceptance_checks'][0]
        backend_check.update(check_id='items-api', command=self.test_command('api', 'items-api'))
        spec['features'][0]['acceptance_checks'].append(backend_check)
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        coverage = self.coverage(surface='ui')
        coverage['mappings'][0]['acceptance_ids'].append('items-api')
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))

    def test_blueprint_rejects_unfinished_required_details(self):
        self.start()
        self.exchange()
        self.record_baseline(self.baseline('ui'))
        self.record_reviewed_plan(self.plan())
        for field, placeholder in (
            ('anatomy', 'TBD'),
            ('responsive', 'To be determined.'),
            ('accessibility', 'TODO: choose the tab order'),
            ('gap', 'REPLACE approved value'),
        ):
            with self.subTest(field=field):
                spec = self.blueprint(version=f'unfinished-{field}', surface='ui')
                feature = spec['features'][0]
                container = feature if field == 'anatomy' else feature['visual_contract']
                if field == 'gap':
                    container = container['tokens']
                container[field] = placeholder
                result = self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)
                self.assertIn(field, result.stderr)
                self.assertIn('placeholder', result.stderr.lower())
        spec = self.blueprint(version='unfinished-responsive-note', surface='ui')
        spec['features'][0]['visual_contract']['responsive'] = (
            'Use one column below 768px.\nTODO: decide how the list scrolls on narrow phones.'
        )
        result = self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)
        self.assertIn('visual_contract.responsive', result.stderr)
        self.assertIn('placeholder', result.stderr.lower())
        spec = self.blueprint(surface='ui')
        spec['features'][0]['anatomy'] = (
            'Replace the current wrapper with the shared layout.\nShow the literal TODO label on imported tasks.'
        )
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage(surface='ui')))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))

    def test_slice_rejects_unfinished_test_plan_and_deviations(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        for field, value in (
            ('test_plan', 'TODO: design test fixtures'),
            ('prohibited_deviations', ['TBD']),
        ):
            with self.subTest(field=field):
                packet = dict(self.slice(), slice_id=f'bad-{field.replace("_", "-")}')
                packet[field] = value
                result = self.cli('slice-create', '--contract-file', self.payload('slice', packet), ok=False)
                self.assertIn(field, result.stderr)
                self.assertIn('placeholder', result.stderr.lower())

    def test_backend_blueprint_rejects_an_unchecked_failure_state(self):
        self.start()
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint()
        spec['features'][0]['acceptance_checks'][0]['state_expectations'] = {
            'success': '200 JSON array', 'forbidden': '403 forbidden',
        }
        result = self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)
        self.assertIn('error', result.stderr)
        self.assertIn('acceptance', result.stderr.lower())

    def test_backend_verification_rejects_missing_failure_observation(self):
        self.assigned()
        review = self.review()
        output = self.directory / review['checks'][0]['output_file']
        evidence = json.loads(output.read_text())
        del evidence['states']['backend_failure']
        output.write_text(json.dumps(evidence))
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', review), ok=False)
        self.assertIn('approved backend state and outcome', result.stderr)

    def test_existing_app_can_add_a_feature_traced_only_to_a_user_requirement(self):
        self.start()
        self.exchange()
        self.exchange('add-history', feature_ids=[], requirement_ids=['item-history'],
                      response='Yes, add an endpoint for item history.')
        audit = self.baseline()
        audit['requirements'] = [{'requirement_id': 'item-history', 'text': 'Add item history',
                                  'decision_ids': ['add-history']}]
        self.record_baseline(audit)
        plan = self.plan()
        plan['decision_ids'].append('add-history')
        self.record_reviewed_plan(plan)
        spec = self.blueprint()
        spec['decision_ids'].append('add-history')
        added = copy.deepcopy(spec['features'][0])
        added.update(feature_id='history-api', baseline_ids=['item-history'], classification='ADD',
                     decision_ids=['add-history'], purpose='List past item versions',
                     implementation_paths=['src/history.py'])
        added['acceptance_checks'][0].update(check_id='history-access',
                                             command=self.test_command('api', 'history-access'))
        spec['features'].append(added)
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        coverage = self.coverage()
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage), ok=False)
        coverage['mappings'].append({'baseline_id': 'item-history', 'feature_id': 'history-api',
                                     'acceptance_ids': ['history-access']})
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        packet = dict(self.slice(), feature_ids=['history-api'], functional_tests=['history-access'],
                      allowed_paths=['src/history.py'])
        self.cli('slice-create', '--contract-file', self.payload('slice', packet))
        self.cli('validate')

    def test_backend_blueprint_approves_without_runnable_frontend(self):
        self.prepared()
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        approval = self.manifest()['approvals']['approve-spec-v1']
        self.assertEqual(approval['mode'],'blueprint')
        self.assertIn('blueprints/spec-v1/items.md',approval['artifact_hashes'])
        self.assertNotIn('replay',approval)
        self.cli('validate')

    def test_plan_is_not_approval_and_incomplete_coverage_is_rejected(self):
        self.start(); self.exchange(); self.record_baseline()
        self.record_reviewed_plan(self.plan())
        self.assertIsNone(self.manifest()['active_approval_id'])
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',self.blueprint()))
        coverage = self.coverage(); coverage['mappings']=[]
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',coverage),ok=False)

    def test_unauthorized_behavior_change_and_open_choices_rejected(self):
        self.start(); self.exchange(); self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint(); spec['features'][0]['classification']='REMOVE'
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec),ok=False)
        spec['features'][0]['classification']='KEEP'; spec['open_decisions']=['Permission handling?']
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec),ok=False)

    def test_approval_rejects_unanswered_question_and_changed_markdown(self):
        self.prepared()
        self.exchange('pending-choice',status='open',response=None)
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)
        self.exchange('answer-choice',responds_to='pending-choice')
        self.artifact('blueprints/spec-v1/items.md','Changed spec after coverage')
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)

    def test_old_approvals_are_immutable_and_tampered_plan_blocks(self):
        self.prepared()
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        before = copy.deepcopy(self.manifest()['approvals'])
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)
        self.artifact('plans/plan-v1/roadmap.md','Changed roadmap')
        self.cli('validate',ok=False)
        self.assertEqual(self.manifest()['approvals'],before)

    def test_question_cursor_resumes_exact_pending_question(self):
        self.start(); self.exchange(status='open',response=None)
        work = {'task_id':'discuss-layout','phase':'discover','status':'waiting_user','summary':'Question displayed',
            'next_action':'Wait for answer','pending_question_id':'choose-layout','completed_steps':[],
            'pending_steps':['Record answer'], 'blockers':[], 'code_paths':[], 'artifact_paths':[]}
        self.cli('work-update','--work-file',self.payload('work',work))
        resumed=json.loads(self.cli('resume','--format','json').stdout)
        self.assertEqual(resumed['active_work']['pending_question_id'],'choose-layout')
        self.assertEqual(resumed['pending_questions'][0]['exchange_id'],'choose-layout')

    def slice(self):
        return {'slice_id':'items-slice','scope':'feature','owner':'builder','status':'draft',
            'blueprint_version':'spec-v1','feature_ids':['items-feature'],
            'allowed_paths':['src/service.py'],'dependencies':[],
            'functional_tests':['items-access'],'test_plan':'Run authorization API test with member and outsider fixtures',
            'prohibited_deviations':['Do not change membership or JSON schema']}

    def assigned(self, surface='backend'):
        self.prepared(surface)
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        packet=self.slice()
        if surface=='ui':
            packet['functional_tests'].append('items-behavior')
        self.cli('slice-create','--contract-file',self.payload('slice',packet))
        report={'slice_id':'items-slice','owner':'builder','status':'submitted','summary':'Implemented item access',
            'implementation_paths':['src/service.py'],'functional_evidence':['API tests'], 'shared_ui_paths':[],
            'visual_evidence':[], 'blockers':[], 'next_action':'Independent review'}
        self.cli('slice-report','--slice-id','items-slice','--owner','builder','--report-file',self.payload('report',report))
        self.cli('reconcile','--slice-id','items-slice','--action','report')

    def write_test_runner(self):
        test_path=self.root/'tests/test_items.py'
        test_path.parent.mkdir(exist_ok=True)
        screenshot = base64.b64encode(png(FIXTURE_VIEWPORT['width'], FIXTURE_VIEWPORT['height'])).decode()
        test_path.write_text(f"""
import base64
import json
import os
from pathlib import Path
import runpy
import sys

def result(key, value):
    return dict(check_id=key, actual=value, expected=value, result='PASS')

kind, key = sys.argv[1:3]
if kind == 'render':
    if os.environ.get('P2P_SCREENSHOT'):
        Path(os.environ['P2P_SCREENSHOT']).write_bytes(base64.b64decode({screenshot!r}))
    checks = [result('items-access', '16px')]
    if len(sys.argv) > 3:
        checks.append(result('success-visibility', 'visible'))
    output = dict(feature_id='items-feature', case_id=key+'-case', route='/items', state=key,
                  viewport={FIXTURE_VIEWPORT!r}, checks=checks)
elif kind == 'api':
    path = (sys.argv[3] + '.py') if len(sys.argv) > 3 else 'src/service.py'
    handle_request = runpy.run_path(path)['handle_request']
    member_status, member_data = handle_request('member')
    outsider_status, outsider_data = handle_request('nonmember')
    assert member_status == 200 and isinstance(member_data, list)
    assert outsider_status == 403 and outsider_data is None
    def unavailable():
        raise OSError('Storage unavailable')
    handle_request.__globals__['list_items'] = unavailable
    failure_status, failure = handle_request('member')
    assert failure_status == 500 and failure == {{'retryable': True}}
    output = result(key, f'Member gets {{member_status}} array; nonmember gets {{outsider_status}}')
    output['states'] = dict(success=f'{{member_status}} JSON array', forbidden=f'{{outsider_status}} forbidden',
                            error=f'{{failure_status}} retryable',
                            backend_failure=f'{{outsider_status}} forbidden; {{failure_status}} retryable')
    if key == 'items-denial':
        output = result(key, f'Outsider gets {{outsider_status}}')
        output['states'] = dict(forbidden=f'{{outsider_status}} forbidden')
    if key == 'application-api' and os.environ.get('P2P_FIXTURE_AUDIT_FAIL'):
        output.update(actual='Everyone gets 200', result='fail')
else:
    values = {{'items-access':'16px', 'items-behavior':'Member can load items; outsider denied',
               'success-visibility':'visible'}}
    output = result(key, values[key])
print(json.dumps(output))
""")

    def review(self, surface='backend', record_integrity=True):
        expected='16px' if surface=='ui' else 'Member gets 200 array; nonmember gets 403'
        observation = {'check_id':'items-access','expected':expected,'actual':expected,'result':'PASS'}
        if surface != 'ui':
            observation['states'] = BACKEND_OUTCOMES
        output=self.artifact('slices/items-slice/evidence/check.json', json.dumps(observation))
        review={'test_source_paths':['tests/test_items.py'],'slice_id':'items-slice','reviewer':'independent-reviewer','notes':'Inspected actual output and code',
            'checks':[{'check_id':'items-access','expected':expected,'observed':expected,'result':'PASS',
                       'command':self.test_command('visual' if surface=='ui' else 'api','items-access'),
                       'output_file':output}],
            'rendered_cases':[], 'specification_limits':'No B reference exists; visual checks cover specified values only'}
        if surface=='ui':
            expected_behavior='Member can load items; outsider denied'
            behavior_output=self.artifact('slices/items-slice/evidence/behavior.json',json.dumps(
                {'check_id':'items-behavior','expected':expected_behavior,'actual':expected_behavior,'result':'PASS'}))
            review['checks'].append({'check_id':'items-behavior','expected':expected_behavior,
                'observed':expected_behavior,'result':'PASS','command':self.test_command('visual','items-behavior'),
                'output_file':behavior_output})
            for state in ('loading','success','forbidden','error'):
                screenshot=self.artifact(f'slices/items-slice/evidence/{state}.png',png(
                    FIXTURE_VIEWPORT['width'], FIXTURE_VIEWPORT['height']))
                review['rendered_cases'].append({'feature_id':'items-feature','case_id':state+'-case','state':state,'route':'/items',
                    'viewport':dict(FIXTURE_VIEWPORT),'screenshot':screenshot,'dom_file':self.artifact(f'slices/items-slice/evidence/{state}-dom.json',json.dumps(
                        {'feature_id':'items-feature','case_id':state+'-case','state':state,'route':'/items',
                         'viewport':FIXTURE_VIEWPORT, 'checks':[{'check_id':'items-access',
                            'expected':'16px','actual':'16px','result':'PASS'}]})),
                    'tool':'fixture','command':self.test_command('render',state),
                                                         'ready_selector':f'#state-{state}'})
            if record_integrity:
                self.visual_integrity_attempt(review)
        return review

    def visual_integrity_attempt(self, review, status='clean'):
        packet = json.loads((self.directory / self.manifest()['slices']['items-slice']['contract_file']).read_text())
        approved = [(feature['feature_id'], case)
                    for feature in packet.get('feature_contracts', [])
                    for case in feature.get('visual_contract', {}).get('render_cases', [])]
        counts = {}
        for _, case in approved:
            counts[case['case_id']] = counts.get(case['case_id'], 0) + 1
        cases = []
        for feature_id, contract_case in approved:
            case_id = (contract_case['case_id'] if counts[contract_case['case_id']] == 1
                       else f"{feature_id}/{contract_case['case_id']}")
            observed = next((item for item in review['rendered_cases']
                             if item['state'] == contract_case['state']), review['rendered_cases'][0])
            source = self.directory / observed['screenshot']
            prefix = f"slices/items-slice/evidence/visual-integrity-{len(cases)}"
            full = prefix + '-full.png'
            closeup = prefix + '-closeup.png'
            self.artifact(full, source.read_bytes())
            from PIL import Image
            with Image.open(source) as image:
                image.crop((0, 0, 64, 32)).save(self.directory / closeup)
            cases.append({
                'case_id': case_id,
                'viewport': contract_case['viewport'],
                'state': {key: contract_case[key] for key in ('route', 'state', 'role', 'fixture', 'state_setup')
                          if key in contract_case},
                'full_screenshot': full,
                'closeup_screenshot': closeup,
                'closeup_bounds': {'x': 0, 'y': 0, 'width': 64, 'height': 32},
            })
        attempt = {
            'status': status,
            'observations': [
                {'dimension': dimension, 'result': 'pass', 'note': note}
                for dimension, note in (
                    ('geometry', 'Compared rendered layout with the approved visual tokens.'),
                    ('design', 'Compared typography and component treatment with the specification.'),
                    ('interaction', 'Checked the documented UI state and controls.'),
                    ('responsive', 'Checked the approved render viewport.'),
                )
            ],
            'findings': [], 'rechecked_findings': [], 'cases': cases,
        }
        from fixtures.visual_capture import capture_fixture_case, coverage_row
        for case in cases:
            capture_fixture_case(self.directory, "items-slice", case)
        check_ids = {check["check_id"] for feature in packet["feature_contracts"]
                     if feature.get("visual_contract") for check in feature["acceptance_checks"]
                     if check["kind"] in ("behavior", "visual")}
        attempt["coverage"] = [coverage_row(case, sorted(check_ids)) for case in cases]
        self.cli('visual-integrity', '--action', 'record', '--slice-id', 'items-slice',
                 '--attempt-file', self.payload('visual-integrity', attempt))

    def test_backend_slice_accepts_without_screenshot_and_carries_exact_spec(self):
        self.assigned()
        cursor = {'task_id': 'review-items', 'phase': 'implement', 'status': 'in_progress',
                  'summary': 'Verify the items API', 'next_action': 'Run verification',
                  'completed_steps': [], 'pending_steps': ['Review checks'], 'blockers': [],
                  'code_paths': [], 'artifact_paths': []}
        self.cli('work-update', '--work-file', self.payload('cursor', cursor))
        packet=json.loads((self.directory/'slices/items-slice/contract.json').read_text())
        self.assertEqual(packet['feature_contracts'][0]['backend_contract']['authorization'],'member only')
        self.assertIn('blueprints/spec-v1/items.md',packet['approved_artifact_hashes'])
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',self.review()))
        self.cli('reconcile','--slice-id','items-slice','--action','accept')
        self.assertEqual(self.manifest()['slices']['items-slice']['status'],'accepted')
        self.assertNotIn('visual_integrity_required', self.manifest()['slices']['items-slice'])
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['active_work']['phase'], 'integrate')
        self.assertEqual(resumed['active_work']['next_action'], resumed['next_action'])
        self.assertEqual(resumed['active_work']['pending_steps'], cursor['pending_steps'])
        self.cli('validate')

    def test_final_audit_executes_checks_and_requires_explicit_current_acceptance(self):
        self.assigned()
        review = self.review()
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        plan = {'approval_id': self.manifest()['active_approval_id'], 'checks': [{
            'check_id': 'application-api', 'kind': 'integration', 'slice_ids': ['items-slice'],
            'scenario': 'Exercise member, forbidden and unavailable storage routes',
            'command': self.test_command('api', 'application-api'),
            'test_source_paths': ['tests/test_items.py'],
            'expected': 'Member gets 200 array; nonmember gets 403',
        }]}
        status = lambda: json.loads(self.cli('visual-quality', '--action', 'status').stdout)
        args = ('--action', 'audit', '--input-file', self.payload('final-audit', plan))
        self.env['P2P_FIXTURE_AUDIT_FAIL'] = '1'
        try:
            failed = json.loads(self.cli('visual-quality', *args, ok=False).stdout)
        finally:
            del self.env['P2P_FIXTURE_AUDIT_FAIL']
        self.assertEqual(failed['status'], 'blocked')
        self.assertEqual(failed['checks'][0]['result'], 'fail')
        removed = self.cli('visual-quality', '--action', 'audit', '--input-file',
                           self.payload('removed-check', {**plan, 'checks': [{
                               **plan['checks'][0], 'check_id': 'replacement-api'}]}), ok=False)
        self.assertIn('Retain mandatory audit check application-api', removed.stderr)
        weakened = self.cli('visual-quality', '--action', 'audit', '--input-file',
                            self.payload('weakened-audit', {**plan, 'checks': [{
                                **plan['checks'][0], 'expected': 'Everyone gets 200'}]}), ok=False)
        self.assertIn('Retain the expected outcome', weakened.stderr)
        saved = json.loads(self.cli('visual-quality', *args).stdout)
        self.assertEqual(saved['status'], 'technically_complete')
        self.assertEqual(saved['checks'][0]['observation']['states'], BACKEND_OUTCOMES)
        self.assertEqual(json.loads(self.cli('visual-quality', *args).stdout)['audit_id'], saved['audit_id'])
        current = status()
        self.assertFalse(current['acceptance_current'])
        decision = {'approval_id': saved['approval_id'], 'audit_id': saved['audit_id'],
                    'audit_sha256': current['audit_sha256'], 'approved_by_user': True,
                    'user_response': 'I accept this completed application.'}
        self.cli('visual-quality', '--action', 'accept',
                 '--input-file', self.payload('wrong-decision', {**decision, 'audit_sha256': '0' * 64}), ok=False)
        decision_file = self.payload('final-decision', decision)
        self.cli('visual-quality', '--action', 'accept', '--input-file', decision_file)
        self.assertEqual(status()['status'], 'finally_accepted')
        self.assertEqual(json.loads(self.cli('resume', '--format', 'json').stdout)['visual_quality']['status'],
                         'finally_accepted')
        implementation = self.root / 'src/service.py'
        implementation.write_text(implementation.read_text() + '\n# Changed after final review\n')
        stale = status()
        self.assertFalse(stale['audit_current'])
        self.assertFalse(stale['acceptance_current'])
        self.cli('visual-quality', '--action', 'accept', '--input-file', decision_file, ok=False)
        report = self.directory / 'visual-quality' / 'audit' / f"{saved['audit_id']}.json"
        report.write_text('{}')
        self.assertIn('evidence changed', self.cli('visual-quality', '--action', 'status', ok=False).stderr)

    def test_final_review_correction_reopens_delivery_and_preserves_prior_acceptance(self):
        self.assigned()
        key = 'items-slice'

        def verify():
            self.cli('reconcile', '--slice-id', key, '--action', 'verify',
                     '--review-file', self.payload('quality-review', self.review()))
            self.cli('reconcile', '--slice-id', key, '--action', 'accept')

        def quality(action, payload=None, expected_success=True):
            args = [] if payload is None else ['--input-file', self.payload('quality-input', payload)]
            return self.cli('visual-quality', '--action', action, *args, ok=expected_success)

        def audit():
            return json.loads(quality('audit', plan).stdout)

        def acceptance(report):
            return {'approval_id': report['approval_id'], 'audit_id': report['audit_id'],
                    'audit_sha256': json.loads(quality('status').stdout)['audit_sha256'],
                    'approved_by_user': True, 'user_response': 'I accept this completed application.'}

        def correction(phase, **args):
            return json.loads(quality('correction', {'status': phase, **args}).stdout)

        verify()
        plan = {'approval_id': self.manifest()['active_approval_id'], 'checks': [{
            'check_id': 'application-api', 'kind': 'integration', 'slice_ids': [key],
            'scenario': 'Exercise list and permission checks',
            'command': self.test_command('api', 'application-api'),
            'test_source_paths': ['tests/test_items.py'], 'expected': 'Member gets 200 array; nonmember gets 403',
        }]}
        original = audit()
        self.assertEqual(original['review_scope'][key]['features'][0]['feature_id'], 'items-feature')
        approval = acceptance(original)
        feedback = correction('reported', audit_id=original['audit_id'], slice_ids=[key],
                              user_feedback='Please simplify the handling of the empty items result.')
        pending = quality('accept', approval, expected_success=False)
        self.assertIn('outstanding final review feedback', pending.stderr)
        correction('deferred', finding_id=feedback['finding_id'], user_response='Keep this for later review.',
                   rationale='The current response passes every approved requirement.')
        accepted = json.loads(quality('accept', approval).stdout)
        self.assertEqual(json.loads(quality('status').stdout)['corrections'][feedback['finding_id']]['status'],
                         'deferred')

        request = correction('reported', audit_id=original['audit_id'], slice_ids=[key],
                             user_feedback='Simplify empty item construction while keeping the list response.')
        finding = request['finding_id']
        self.assertEqual(request['acceptance_id'], accepted['acceptance_id'])
        correction('triaged', finding_id=finding, disposition='requires_amendment',
                   rationale='A behavior change needs approval.', before_capture_ids={})
        start = {'status': 'in_progress', 'finding_id': finding, 'slice_ids': [key], 'replacements': {}}
        refused = quality('correction', start, expected_success=False)
        self.assertIn('required amendment', refused.stderr)
        correction('triaged', finding_id=finding, disposition='within_approval',
                   rationale='The expected response and permissions are preserved.', before_capture_ids={})
        correction('in_progress', finding_id=finding, slice_ids=[key], replacements={})
        self.assertEqual(self.manifest()['slices'][key]['status'], 'ready')
        premature = {'status': 'verified', 'finding_id': finding, 'after_capture_ids': {}, 'summary': 'Checks passed.'}
        self.assertIn('Reverify', quality('correction', premature, expected_success=False).stderr)
        source = self.root / 'src/service.py'
        source.write_text(source.read_text() + '\n# Clarify the implementation.\n')
        self.cli('slice-report', '--slice-id', key, '--owner', 'builder', '--report-file', str(self.directory / 'inputs' / 'report.json'))
        self.cli('reconcile', '--slice-id', key, '--action', 'report')
        verify()
        self.assertEqual(json.loads(quality('audit', plan, expected_success=False).stdout)['status'], 'blocked')
        verified = correction('verified', finding_id=finding, after_capture_ids={}, summary='Verified API and permissions.')
        self.assertIn(key, verified['verification'])
        updated = audit()
        self.assertNotEqual(updated['audit_id'], original['audit_id'])
        current = json.loads(quality('status').stdout)
        self.assertFalse(current['acceptance_current'])
        self.assertEqual(current['acceptance']['acceptance_id'], accepted['acceptance_id'])
        self.assertEqual(current['corrections'][finding]['status'], 'verified')
        quality('accept', acceptance(updated))
        self.assertEqual(len(json.loads(quality('status').stdout)['acceptance_history']), 2)

        request = correction('reported', audit_id=updated['audit_id'], slice_ids=[key],
                             user_feedback='Record the new approved approach for item access.')
        amendment = request['finding_id']
        correction('triaged', finding_id=amendment, disposition='requires_amendment',
                   rationale='The requested approach changes the explicitly approved blueprint.',
                   before_capture_ids={})
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec-v2', self.blueprint('spec-v2')))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage-v2', self.coverage('spec-v2')))
        self.cli('approve', '--approval-file', self.payload('approval-v2', self.approval('spec-v2')))
        replacement = dict(self.slice(), slice_id='items-slice-v2', blueprint_version='spec-v2')
        self.cli('slice-create', '--contract-file', self.payload('replacement', replacement))
        begun = correction('in_progress', finding_id=amendment, slice_ids=['items-slice-v2'],
                           replacements={key: 'items-slice-v2'})
        self.assertTrue(begun['amended'])
        self.assertEqual(begun['approval_id'], 'approve-spec-v2')
        self.assertEqual(begun['affected_slices'], ['items-slice-v2'])

    def test_blueprint_ui_slice_requires_post_implementation_visual_integrity(self):
        self.assigned('ui')
        review = self.review('ui', record_integrity=False)

        blocked = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                           '--review-file', self.payload('review', review), ok=False)

        self.assertIn('visual integrity', blocked.stderr.lower())
        self.assertEqual(self.manifest()['slices']['items-slice']['status'], 'blocked')

    def test_approval_and_slice_reject_missing_test_runner(self):
        self.prepared()
        approval = self.approval()
        runner = self.root / 'tests/test_items.py'
        original = runner.read_bytes()
        runner.unlink()
        self.cli('approve', '--approval-file', self.payload('approval', approval), ok=False)
        runner.write_bytes(original)
        self.cli('approve', '--approval-file', self.payload('approval', approval))
        runner.unlink()
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()), ok=False)

    def test_backend_verification_rejects_permission_regression(self):
        self.assigned()
        (self.root / 'src/service.py').write_text(
            'def list_items(): return []\ndef handle_request(role): return 200, list_items()\n')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', self.review()), ok=False)

    def test_approved_removal_verifies_absence_and_detects_recreation(self):
        self.start()
        self.exchange(authorized_changes=[{'baseline_id':'list-items','classification':'REMOVE'}],
                      question='Remove the item listing?', response='Yes, remove it.')
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint()
        source = self.root/'src/service.py'
        test_path = self.root/'tests/check_removal.py'
        test_path.parent.mkdir()
        test_path.write_text(
            "import json\nfrom pathlib import Path\n"
            "assert not Path('src/service.py').exists()\n"
            "print(json.dumps(dict(check_id='items-access', expected='Removed', actual='Removed', result='PASS', states={'removed': 'Removed'})))\n")
        command = [sys.executable, 'tests/check_removal.py']
        feature = spec['features'][0]
        feature.update(classification='REMOVE', behavior_contract='Item listing is removed', states=['removed'])
        feature['acceptance_checks'][0].update(command=command, expected='Removed', state_expectations={'removed':'Removed'})
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage()))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()))

        source.unlink()
        observed = subprocess.check_output(command, cwd=self.root)
        output_file = self.artifact('slices/items-slice/evidence/removal.json', observed)
        report = {'slice_id':'items-slice', 'owner':'builder', 'status':'submitted', 'summary':'Removed listing',
                  'implementation_paths':['src/service.py']}
        review = {'slice_id':'items-slice', 'reviewer':'reviewer', 'notes':'Confirmed listing was removed',
                  'test_source_paths':['tests/check_removal.py'],
                  'checks':[{'check_id':'items-access', 'command':command, 'result':'PASS', 'expected':'Removed',
                             'observed':'Removed', 'output_file':output_file}]}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('report', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        self.cli('validate')
        source.write_text('def list_items(): return []\n')
        self.cli('validate', ok=False)

    def test_move_can_report_deletion_of_original_source_path(self):
        self.start()
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        old_path, new_path = 'src/service.py', 'src/items.py'
        command = self.test_command('api', 'items-access', new_path.removesuffix('.py'))
        spec = self.blueprint()
        feature = spec['features'][0]
        feature.update(classification='MOVE', implementation_paths=[old_path, new_path])
        feature['acceptance_checks'][0].update(command=command)
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage()))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        self.cli('slice-create', '--contract-file',
                 self.payload('slice', dict(self.slice(), allowed_paths=[old_path, new_path])))
        (self.root / old_path).rename(self.root / new_path)
        output = self.artifact('slices/items-slice/evidence/move.json',
                               subprocess.check_output(command, cwd=self.root))
        report = {'slice_id': 'items-slice', 'owner': 'builder', 'status': 'submitted',
                  'summary': 'Moved the service', 'implementation_paths': [old_path, new_path]}
        review = self.review()
        review['checks'][0].update(command=command, output_file=output)
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('report', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        self.cli('validate')

    def test_ui_slice_requires_rendered_states_and_concrete_values(self):
        self.serve_fixture_states()
        self.assigned('ui')
        review=self.review('ui'); review['rendered_cases']=[]
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',review),ok=False)
        # A fresh independent verification is required after an explicit blocker resolution.
        resolution={'slice_id':'items-slice','resolution':'Captured all UI states','next_action':'Reverify'}
        self.cli('reconcile','--slice-id','items-slice','--action','unblock','--review-file',self.payload('resolution',resolution))
        self.cli('reconcile','--slice-id','items-slice','--action','report')
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',self.review('ui')))
        self.cli('reconcile','--slice-id','items-slice','--action','accept')
        self.cli('validate')

    def test_accept_rejects_tampered_test_evidence(self):
        self.assigned()
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',self.review()))
        self.artifact('slices/items-slice/evidence/check.json','changed')
        self.cli('reconcile','--slice-id','items-slice','--action','accept',ok=False)
        self.cli('validate',ok=False)

    def test_accept_rejects_changed_implementation(self):
        self.assigned()
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',self.review()))
        (self.root/'src/service.py').write_text('def list_items(): return None')
        self.cli('reconcile','--slice-id','items-slice','--action','accept',ok=False)

    def test_slice_rejects_unknown_scope_and_overlapping_ownership(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        packet=self.slice(); packet['feature_ids']=['unknown']
        self.cli('slice-create','--contract-file',self.payload('slice',packet),ok=False)
        self.cli('slice-create','--contract-file',self.payload('slice',self.slice()))
        packet=self.slice(); packet['slice_id']='second-slice'
        self.cli('slice-create','--contract-file',self.payload('slice',packet),ok=False)

    def test_self_review_and_wrong_observed_result_do_not_verify(self):
        self.assigned()
        review=self.review(); review['reviewer']='builder'
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',review),ok=False)

    def test_followup_audit_without_new_ids_invalidates_delivery(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        audit = self.baseline(audit_id='audit-v2')
        audit['sweeps'] = [audit['sweeps'][-1]]
        self.record_baseline(audit)
        result = self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()), ok=False)
        self.assertIn('Stale baseline audit', result.stderr)

    def test_reapproval_retires_accepted_assignment_and_releases_paths(self):
        self.assigned()
        source = self.root / 'src/service.py'
        source.write_text(source.read_text() + '\n# Accepted implementation\n')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', self.review()))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        receipt = self.directory / 'slices/items-slice/verification.json'
        original_receipt = receipt.read_bytes()
        spec = self.blueprint('spec-v2')
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage('spec-v2')))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval('spec-v2')))
        self.cli('validate')
        self.assertEqual(self.manifest()['slices']['items-slice']['superseded_by'], 'approve-spec-v2')
        accepted = source.read_bytes()
        source.write_bytes(accepted + b'\n# Change before the replacement slice\n')
        self.assertIn('src/service.py', self.cli('validate', ok=False).stderr)
        source.write_bytes(accepted)
        replacement = dict(self.slice(), slice_id='items-slice-v2', blueprint_version='spec-v2')
        self.cli('slice-create', '--contract-file', self.payload('replacement', replacement))
        self.assertEqual(receipt.read_bytes(), original_receipt)
        receipt.write_text('{}')
        self.assertIn('sha256', self.cli('validate', ok=False).stderr)

    def test_reapproval_preserves_and_validates_archived_ui_inspection(self):
        self.serve_fixture_states()
        self.assigned('ui')
        review = self.review('ui')
        first = json.loads(self.cli('visual-integrity', '--slice-id', 'items-slice',
                                     '--action', 'verify').stdout)
        self.visual_integrity_attempt(review)
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        attempt = json.loads(self.cli('visual-integrity', '--slice-id', 'items-slice',
                                      '--action', 'verify').stdout)
        archived_image = self.directory / attempt['cases'][0]['closeup_screenshot']

        self.cli('blueprint-record', '--blueprint-file',
                 self.payload('spec', self.blueprint('spec-v2', surface='ui')))
        self.cli('blueprint-coverage', '--coverage-file',
                 self.payload('coverage', self.coverage('spec-v2', surface='ui')))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval('spec-v2')))

        self.cli('validate')
        replacement = dict(self.slice(), slice_id='items-slice-v2', blueprint_version='spec-v2')
        replacement['functional_tests'].append('items-behavior')
        self.cli('slice-create', '--contract-file', self.payload('replacement', replacement))
        self.cli('validate')
        manifest_path = self.directory / 'manifest.json'
        original_manifest = manifest_path.read_bytes()
        damaged = self.manifest()
        del damaged['slices']['items-slice']['visual_captures'][first['cases'][0]['capture_id']]
        manifest_path.write_text(json.dumps(damaged))
        self.assertIn('matching browser capture', self.cli('validate', ok=False).stderr)
        manifest_path.write_bytes(original_manifest)
        archived_image.write_bytes(b'altered image')
        self.assertIn('Missing or altered visual evidence', self.cli('validate', ok=False).stderr)

    def test_conflicting_handwritten_feature_markdown_cannot_be_frozen(self):
        self.start()
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint()
        self.artifact(spec['features'][0]['spec_paths'][0], '# Access\nAllow everyone to list items.')
        result = self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)
        self.assertIn('generated blueprint', result.stderr)

    def test_static_png_and_constant_dom_do_not_verify_ui(self):
        self.assigned('ui')
        review = self.review('ui')
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', review), ok=False)
        self.assertIn('Browser', result.stderr)

    def test_browser_measurement_rejects_constant_command_on_wrong_render(self):
        self.serve_fixture_states(gap='20px')
        self.assigned('ui')
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', self.review('ui')), ok=False)
        self.assertIn('Browser measurements differ', result.stderr)

    def test_browser_rejects_a_hidden_state_even_when_spacing_matches(self):
        states = ''.join(f'<div hidden id="state-{state}">{state}</div>'
                         for state in ('loading', 'success', 'forbidden', 'error'))
        self.serve_items(f'<!doctype html><main id="items" style="display:grid;gap:16px">{states}</main>')
        self.assigned('ui')
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', self.review('ui')), ok=False)
        self.assertIn('visible', result.stderr)

    def test_preserved_roles_require_real_browser_coverage_of_the_admin_control(self):
        self.start()
        self.exchange()
        audit = self.baseline('ui')
        original = ('<button id="switch">Switch to admin</button>'
                    '<div id="state" data-role="member" data-state="success">Items</div>'
                    '<main id="items" style="display:grid;gap:16px"><div>Items</div></main>'
                    '<button id="admin-delete" hidden>Delete item</button>'
                    '<script>document.querySelector("#switch").onclick = () => {'
                    'document.querySelector("#state").dataset.role = "admin";'
                    'const button = document.querySelector("#admin-delete");'
                    'if (button) button.hidden = false;'
                    '};</script>')
        self.serve_items(original)
        feature = audit['features'][0]
        contract = 'Members can view items and admins can see the Delete item button'
        feature.update(roles=['member', 'admin'], states=['success'], behavior=contract)
        feature['source_anchors'].append({'path': 'src/items.html', 'contains': 'id="admin-delete"'})
        audit['behavior_contracts']['list-items']['behavior_contract'] = contract
        for sweep in audit['sweeps']:
            sweep['source_files_checked'].append('src/items.html')
        cases = {}
        for role in feature['roles']:
            capture = self.discovery_case(role + '-discovery', role=role)
            capture['viewport'] = {'width': 800, 'height': 600}
            if role == 'admin':
                capture.update(ready_selector='#admin-delete:not([hidden])',
                               setup=[{'action': 'click', 'selector': '#switch'}])
            feature['browser_evidence'].append(self.record_browser_capture(capture))
            cases[role] = capture
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint(surface='ui')
        proposed = spec['features'][0]
        proposed.update(behavior_contract=contract, implementation_paths=['src/items.html'], states=['success'])
        render = next(case for case in proposed['visual_contract']['render_cases'] if case['state'] == 'success')
        proposed['visual_contract']['render_cases'] = [render]
        result = self.cli('blueprint-record', '--blueprint-file', self.payload('role-blind', spec), ok=False)
        self.assertIn('audited role/state', result.stderr)
        for role, case in cases.items():
            roles = {key: case[key] for key in ('role', 'observations', 'ready_selector', 'setup')}
            cases[role] = dict(render, **roles)
            if role == 'admin':
                cases[role]['case_id'] = 'admin-case'
        proposed['visual_contract']['render_cases'] = list(cases.values())
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage(surface='ui')))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        packet = dict(self.slice(), allowed_paths=['src/items.html'], functional_tests=['items-access', 'items-behavior'])
        self.cli('slice-create', '--contract-file', self.payload('slice', packet))
        path = self.root / 'src/items.html'
        path.write_text(original.replace('<button id="admin-delete" hidden>Delete item</button>', ''))
        report = {'slice_id': 'items-slice', 'owner': 'builder', 'status': 'submitted', 'summary': 'Render items',
                  'implementation_paths': ['src/items.html'], 'shared_ui_paths': [], 'next_action': 'Independent review'}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('report', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        review = self.review('ui')
        member = next(case for case in review['rendered_cases'] if case['state'] == 'success')
        member.update(role='member')
        admin = dict(member, case_id='admin-case', role='admin',
                     screenshot=self.artifact('slices/items-slice/evidence/admin.png',
                                              png(FIXTURE_VIEWPORT['width'], FIXTURE_VIEWPORT['height'])))
        dom = json.loads((self.directory / member['dom_file']).read_text())
        admin['dom_file'] = self.artifact('slices/items-slice/evidence/admin.json',
                                          json.dumps(dict(dom, case_id='admin-case', role='admin')))
        (self.directory / member['dom_file']).write_text(json.dumps(dict(dom, role='member')))
        review['rendered_cases'] = [member, admin]
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', review), ok=False)
        self.assertIn('visible state', result.stderr)
        path.write_text(original)
        resolution = {'slice_id': 'items-slice', 'resolution': 'Restored the admin control', 'next_action': 'Verify'}
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'unblock',
                 '--review-file', self.payload('resolution', resolution))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        self.visual_integrity_attempt(review)
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')

    def test_browser_rejects_spacing_from_a_hidden_copy(self):
        states = ''.join(f'<div id="state-{state}">{state}</div>'
                         for state in ('loading', 'success', 'forbidden', 'error'))
        self.serve_items(f'<main style="display:grid;gap:4px">{states}</main>'
                         '<div id="items" style="display:none;gap:16px">Hidden copy</div>')
        self.assigned('ui')
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', self.review('ui')), ok=False)
        self.assertIn('visible measurement target', result.stderr)

    def test_browser_rejects_a_state_outside_the_captured_viewport(self):
        states = ''.join(f'<div id="state-{state}">{state}</div>'
                         for state in ('loading', 'success', 'forbidden', 'error'))
        self.serve_items(
            '<!doctype html><main id="items" style="display:grid;gap:16px">Items</main>'
            f'<aside style="position:absolute;left:10000px">{states}</aside>'
        )
        self.assigned('ui')
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', self.review('ui')), ok=False)
        self.assertIn('visible state', result.stderr)

    def test_browser_rejects_a_state_fully_clipped_by_an_ancestor(self):
        states = ''.join(f'<div id="state-{state}">{state}</div>'
                         for state in ('loading', 'success', 'forbidden', 'error'))
        self.serve_items(
            '<!doctype html><main id="items" style="display:grid;gap:16px">Items</main>'
            f'<aside style="height:0;overflow:hidden">{states}</aside>'
        )
        self.assigned('ui')
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', self.review('ui')), ok=False)
        self.assertIn('visible state', result.stderr)

    def test_browser_rejects_state_markers_covered_by_an_opaque_overlay(self):
        states = ''.join(f'<div id="state-{state}">{state}</div>'
                         for state in ('loading', 'success', 'forbidden', 'error'))
        self.serve_items(
            f'<main id="items" style="display:grid;gap:16px">{states}</main>'
            '<div style="position:fixed;inset:0;z-index:9999;background:red;pointer-events:none"></div>'
        )
        self.assigned('ui')
        result = self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                          '--review-file', self.payload('review', self.review('ui')), ok=False)
        self.assertIn('visible state', result.stderr)

    def test_browser_accepts_partly_covered_translucent_state_markers(self):
        states = ''.join(f'<div id="state-{state}" style="opacity:.8">{state}</div>'
                         for state in ('loading', 'success', 'forbidden', 'error'))
        self.serve_items(
            f'<!doctype html><main id="items" style="display:grid;gap:16px">{states}</main>'
            '<div style="position:fixed;inset:0 0 0 50%;z-index:9999;background:red"></div>'
        )
        self.assigned('ui')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', self.review('ui')))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        self.assertEqual(self.manifest()['slices']['items-slice']['status'], 'accepted')

    def test_cursor_tracks_a_renamed_source_and_rejects_its_reappearance(self):
        self.assigned()
        work = dict(task_id='move-source', phase=self.manifest()['stage'], status='in_progress',
                    summary='Move service', next_action='Rename service', completed_steps=[],
                    pending_steps=['Rename service'], blockers=[], code_paths=['src/service.py'], artifact_paths=[])
        self.cli('work-update', '--work-file', self.payload('work', work))
        (self.root / 'src/service.py').rename(self.root / 'src/moved.py')
        work.update(code_paths=['src/moved.py'], absent_code_paths=['src/service.py'],
                    completed_steps=['Rename service'], pending_steps=['Verify move'], next_action='Verify move')
        self.cli('work-update', '--work-file', self.payload('work', work))
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertIsNone(packet['active_work']['code_hashes']['src/service.py'])
        (self.root / 'src/service.py').write_text('# Recreated')
        self.assertIn('src/service.py', self.cli('validate', ok=False).stderr)

    def test_missing_feature_requires_fresh_audit_and_blueprint_approval(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        original=copy.deepcopy(self.manifest()['approvals']['approve-spec-v1'])
        report={'report_id':'missing-one','suspected_features':['list-items'],'summary':'Forbidden state regressed',
            'evidence_paths':[self.artifact('discovery/missing/observation.txt','403 case regressed')]}
        self.cli('missing-feature','--report-file',self.payload('missing',report))
        self.cli('validate',ok=False)
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)
        audit=self.baseline(audit_id='audit-v2'); audit['sweeps']=[audit['sweeps'][-1]]
        self.record_baseline(audit)
        plan=self.plan('plan-v2', 'audit-v2')
        plan['document_paths']=[self.artifact('plans/plan-v2/roadmap.md', self.roadmap('Repair permission behavior.'))]
        self.record_reviewed_plan(plan)
        spec=self.blueprint('spec-v2'); spec.update(baseline_id='audit-v2',plan_version='plan-v2')
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))
        coverage=self.coverage('spec-v2'); coverage['baseline_id']='audit-v2'
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',coverage))
        self.cli('approve','--approval-file',self.payload('approval',self.approval('spec-v2')))
        self.assertFalse(self.manifest()['coverage_pending'])
        self.assertEqual(self.manifest()['approvals']['approve-spec-v1'],original)
        self.cli('validate')

    def test_amended_decision_requires_new_blueprint(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        self.exchange('correct-choice',supersedes='choose-layout',response='Require a moderator instead of member.')
        result=self.cli('validate',ok=False)
        self.assertIn('amended',result.stderr)
        self.cli('slice-create','--contract-file',self.payload('slice',self.slice()),ok=False)

    def test_source_scenarios_gate_approval_and_follow_executed_checks(self):
        self.start()
        manifest = self.manifest()
        manifest.update(initial_alignment_required=True, requirement_traceability_required=True)
        (self.directory / 'manifest.json').write_text(json.dumps(manifest))
        self.exchange()
        baseline = self.baseline()
        evidence = self.artifact('discovery/requirements.md', 'Inspected the item service and observed both caller outcomes.')
        source_items = {
            'item-access': 'Restrict item access to members.',
            'member-list': 'Scenario 1. A member receives a JSON array.',
            'outsider-denial': 'Scenario 2. An outsider receives HTTP 403.',
            'guest-export': 'Scenario 3. Guests can export the list as CSV.',
        }
        supplied_design = self.artifact('inputs/supplied-design.md',
                                        '# Item access\n\n' + '\n\n'.join(source_items.values()))
        conflict = 'The brief also says outsiders may read items. These policies disagree.'
        rows = []
        for key, description in source_items.items():
            rows.append({'requirement_id': key, 'text': description,
                         'request_reference': f'Supplied design, item access, {description}',
                         'kind': 'requirement' if key == 'item-access' else 'acceptance_criterion',
                         **({'parent_requirement_id': 'item-access'} if key != 'item-access' else {}),
                         'classification': {'guest-export': 'missing', 'outsider-denial': 'conflicting'}.get(
                             key, 'already_implemented'),
                         'finding': conflict if key == 'outsider-denial' else
                         'Observed the supported caller outcomes and found no guest CSV export.',
                         'direction': 'Keep item authorization and discuss the guest export.',
                         'evidence_paths': [evidence]})
        prompt = {'exchange_id': 'source-inventory', 'phase': 'discover', 'record_type': 'review_checkpoint',
                  'review_step': 'initial_alignment', 'review_version': 'intake-v1',
                  'review_target': 'Initial understanding of item access', 'status': 'open', 'response': None,
                  'question': 'Have I captured the item access requirement and its three scenarios?',
                  'requirement_ids': list(source_items), 'requirement_mappings': rows,
                  'reviewed_sources': ['Supplied design, all sections and acceptance scenarios'],
                  'reviewed_source_paths': [supplied_design],
                  'source_line_mappings': {supplied_design: [
                      {'start_line': 1, 'end_line': 1, 'requirement_ids': [], 'context_reason': 'Document title'},
                      *({'start_line': line, 'end_line': line, 'requirement_ids': [key]}
                        for line, key in zip((3, 5, 7, 9), source_items)),
                  ]},
                  'evidence_paths': [self.artifact('review/intake.md', 'Preserve member and outsider outcomes. Discuss guest export.')]}
        rows_by_line = prompt['source_line_mappings'][supplied_design]
        omitted = {**prompt, 'source_line_mappings': {supplied_design: rows_by_line[:-1]}}
        result = self.cli('conversation-record', '--exchange-file', self.payload('intake', omitted), ok=False)
        self.assertIn('unaccounted source line 9', result.stderr)
        collapsed = {**prompt, 'source_line_mappings': {supplied_design: [
            *rows_by_line[:-1], {**rows_by_line[-1], 'requirement_ids': ['outsider-denial']}]}}
        result = self.cli('conversation-record', '--exchange-file', self.payload('intake', collapsed), ok=False)
        self.assertIn('distinct acceptance criterion', result.stderr)
        self.cli('conversation-record', '--exchange-file', self.payload('intake', prompt))
        answer = {key: val for key, val in prompt.items() if key not in
                  ('requirement_mappings', 'reviewed_sources', 'reviewed_source_paths', 'source_line_mappings',
                   'evidence_paths')}
        answer.update(exchange_id='source-confirmed', responds_to='source-inventory', status='decided',
                      response='I confirm your understanding.', review_action='confirm')
        self.cli('conversation-record', '--exchange-file', self.payload('intake-answer', answer))
        exclusion = {'exchange_id': 'exclude-export-proposal', 'phase': 'discover', 'status': 'open',
                     'question': 'Guests will have no CSV export in this release. Reply “Approve exclusion of guest-export”.',
                     'requirement_exclusion': 'Guests will have no CSV export in this release.',
                     'response': None, 'requirement_ids': ['guest-export'], 'basis': 'Supplied guest export scenario'}
        self.cli('conversation-record', '--exchange-file', self.payload('exclusion', exclusion))
        exclusion_answer = {
            **{key: value for key, value in exclusion.items() if key != 'requirement_exclusion'},
            'exchange_id': 'exclude-export', 'responds_to': exclusion['exchange_id'], 'status': 'decided',
            'authorized_changes': [{'baseline_id': 'guest-export', 'classification': 'REMOVE'}],
        }
        for refusal in ('No. Keep guest CSV export.', 'Approve exclusion of guest-export only if stakeholders agree'):
            result = self.cli('conversation-record', '--exchange-file', self.payload(
                'exclusion-answer', {**exclusion_answer, 'response': refusal}), ok=False)
            self.assertIn('explicit approval of this exclusion', result.stderr)
        self.cli('conversation-record', '--exchange-file', self.payload(
            'exclusion-answer', {**exclusion_answer, 'response': 'Approve exclusion of guest-export.'}))
        self.exchange('unrelated-decision', requirement_ids=['outsider-denial'],
                      question='Keep the member response schema?', response='Yes, keep the member response schema.')
        resolution = 'Use HTTP 403 for outsiders across every item route.'
        conflict_prompt = {'exchange_id': 'permission-conflict', 'phase': 'discover', 'status': 'open',
                           'question': f'{conflict} {resolution} Reply “Approve resolution of outsider-denial”.',
                           'requirement_conflict': {'finding': conflict, 'resolution': resolution},
                           'requirement_ids': ['outsider-denial'], 'basis': 'Conflicting source access rules',
                           'response': None}
        self.cli('conversation-record', '--exchange-file', self.payload('conflict', conflict_prompt))
        self.cli('conversation-record', '--exchange-file', self.payload('conflict-answer', {
            **{key: value for key, value in conflict_prompt.items() if key != 'requirement_conflict'},
            'exchange_id': 'approved-resolution', 'responds_to': 'permission-conflict',
            'status': 'decided', 'response': 'Approve resolution of outsider-denial.',
        }))
        baseline['requirements'] = [{'requirement_id': key, 'text': value} for key, value in source_items.items()]
        self.record_baseline(baseline)
        plan = self.plan()
        plan['decision_ids'].extend(['exclude-export', 'unrelated-decision', 'approved-resolution'])
        self.record_reviewed_plan(plan)
        stream = plan['workstreams'][0]

        def spec_for(version):
            blueprint = self.blueprint(version)
            blueprint['decision_ids'].extend(['exclude-export', 'unrelated-decision', 'approved-resolution'])
            blueprint['workstream_summaries'] = {stream: 'Keep member access to items and deny outsiders.'}
            feature = blueprint['features'][0]
            feature['baseline_ids'].extend(['item-access', 'member-list', 'outsider-denial'])
            feature['workstreams'] = [stream]
            feature['acceptance_checks'].append({
                'check_id': 'items-denial', 'kind': 'backend', 'expected': 'Outsider gets 403',
                'command': self.test_command('api', 'items-denial'),
                'state_expectations': {'forbidden': BACKEND_OUTCOMES['forbidden']},
            })
            self.cli('blueprint-record', '--blueprint-file', self.payload('spec', blueprint))

        def coverage_for(version):
            coverage = self.coverage(version)
            coverage['mappings'][0]['acceptance_ids'].append('items-denial')
            coverage['mappings'].extend(
                {'baseline_id': key, 'feature_id': 'items-feature', 'acceptance_ids': [check],
                 'spec_path': f'blueprints/{version}/items.md'}
                for key, check in [('item-access', 'items-access'), ('member-list', 'items-access'),
                                   ('outsider-denial', 'items-denial')]
            )
            coverage['requirements'] = [
                {'requirement_id': key, 'status': 'covered' if key == 'outsider-denial' else 'already_satisfied',
                 **({'resolution_decision_id': 'approved-resolution'} if key == 'outsider-denial' else {}),
                 'reason': 'The observed caller behavior is retained by the item service.'}
                for key in ('item-access', 'member-list', 'outsider-denial')
            ] + [{'requirement_id': 'guest-export', 'status': 'excluded', 'exclusion_decision_id': 'exclude-export',
                  'reason': 'Guests have no CSV export in this release, with explicit user approval.'}]
            return coverage

        spec_for('spec-v1')
        coverage = coverage_for('spec-v1')
        coverage['mappings'][-1]['acceptance_ids'] = ['items-access']
        result = self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage), ok=False)
        self.assertIn('distinct check IDs', result.stderr)
        coverage = coverage_for('spec-v1')
        coverage['requirements'][-1]['exclusion_decision_id'] = 'choose-layout'
        result = self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage), ok=False)
        self.assertIn('does not authorize', result.stderr)
        coverage = coverage_for('spec-v1')
        coverage['requirements'][-2]['resolution_decision_id'] = 'unrelated-decision'
        result = self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage), ok=False)
        self.assertIn('specific recorded conflict', result.stderr)
        coverage = coverage_for('spec-v1')
        coverage['mappings'].pop()
        overview = self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage)).stdout
        self.assertIn('3 source acceptance criteria', overview)
        self.assertIn('Partially covered: 1', overview)
        self.assertIn('outsider-denial', self.cli('validate', ok=False).stderr)
        result = self.cli('approve', '--approval-file', self.payload('approval', self.approval()), ok=False)
        self.assertIn('Blueprint coverage is incomplete', result.stderr)
        self.assertIn('outsider-denial', result.stderr)

        spec_for('spec-v2')
        coverage = coverage_for('spec-v2')
        overview = self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage)).stdout
        self.assertIn('0 outstanding coverage gaps', overview)
        report_path = 'blueprint-coverage/spec-v2.md'
        report = (self.directory / report_path).read_text()
        self.assertIn('../blueprints/spec-v2/items.md#check-items-denial', report)
        viewer = json.loads(self.cli('review-revision').stdout)
        self.assertIn('traceability-report-spec-v2', viewer['sections'])
        self.assertIn('traceability-spec-spec-v2-1', viewer['sections'])

        review_path = self.artifact('review/spec-v2.md',
                                    'Coverage overview shown. Member → item array. Outsider → denial. Guest export excluded.')
        question = {'exchange_id': 'review-access', 'phase': 'blueprint', 'record_type': 'review_checkpoint',
                    'status': 'open', 'response': None, 'review_step': 'workstream', 'review_version': 'spec-v2',
                    'review_target': stream, 'review_workstream': stream, 'question': 'Continue after reviewing item access?',
                    'feature_ids': ['items-feature'], 'requirement_ids': list(source_items),
                    'evidence_paths': [review_path]}
        result = self.cli('conversation-record', '--exchange-file', self.payload('question', question), ok=False)
        self.assertIn('coverage overview', result.stderr)
        question['evidence_paths'].append(report_path)
        self.cli('conversation-record', '--exchange-file', self.payload('question', question))
        self.cli('conversation-record', '--exchange-file', self.payload('answer', {
            **question, 'exchange_id': 'review-access-answer', 'responds_to': question['exchange_id'],
            'status': 'decided', 'response': 'Continue', 'review_action': 'continue',
        }))
        approval = self.approval('spec-v2')
        consistency = self.artifact('review/spec-v2-consistency.md', 'Item access, route and checks preserve the agreed service behavior.')
        approval['workstream_consistency_path'] = consistency
        approval['shown_paths'].extend([report_path, consistency])
        self.cli('approve', '--approval-file', self.payload('approval', approval))

        packet = {**self.slice(), 'blueprint_version': 'spec-v2', 'functional_tests': ['items-access', 'items-denial']}
        self.cli('slice-create', '--contract-file', self.payload('slice', packet))
        contract_file = self.directory / 'slices/items-slice/contract.json'
        contract = json.loads(contract_file.read_text())
        denial = next(row for row in contract['requirement_traceability'] if row['requirement_id'] == 'outsider-denial')
        self.assertEqual(denial['check_ids'], ['items-denial'])
        report = {'slice_id': 'items-slice', 'owner': 'builder', 'status': 'submitted',
                  'summary': 'Verified the preserved item service', 'implementation_paths': ['src/service.py'],
                  'functional_evidence': ['API tests'], 'shared_ui_paths': [], 'visual_evidence': [],
                  'blockers': [], 'next_action': 'Independent verification'}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('worker-report', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        review = self.review()
        output = {'check_id': 'items-denial', 'expected': 'Outsider gets 403', 'actual': 'Outsider gets 403',
                  'result': 'PASS', 'states': {'forbidden': BACKEND_OUTCOMES['forbidden']}}
        review['checks'].append({
            'check_id': 'items-denial', 'expected': output['expected'], 'observed': output['actual'],
            'result': 'PASS', 'command': self.test_command('api', 'items-denial'),
            'output_file': self.artifact('slices/items-slice/evidence/denial.json', json.dumps(output)),
        })
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify', '--review-file', self.payload('verification', review))
        verification = json.loads((contract_file.parent / 'verification.json').read_text())
        self.assertEqual(verification['requirement_traceability'], contract['requirement_traceability'])
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        plan = {'approval_id': 'approve-spec-v2', 'checks': [{
            'check_id': 'application-api', 'kind': 'integration', 'slice_ids': ['items-slice'],
            'scenario': 'Exercise item listing and authorization with the running service',
            'command': self.test_command('api', 'application-api'), 'test_source_paths': ['tests/test_items.py'],
            'expected': 'Member gets 200 array; nonmember gets 403',
        }]}
        completed = json.loads(self.cli('visual-quality', '--action', 'audit', '--input-file',
                                        self.payload('completion-audit', plan)).stdout)
        final = completed['requirement_traceability']
        self.assertTrue(next(row for row in final if row['requirement_id'] == 'outsider-denial')['verification'])
        self.assertEqual(next(row for row in final if row['requirement_id'] == 'guest-export')['verification'], [])
        self.cli('validate')
        self.artifact(supplied_design, 'Changed source design after confirmation.')
        self.assertIn(supplied_design, self.cli('validate', ok=False).stderr)

    def test_greenfield_requirement_coverage_can_reach_approval(self):
        self.start(); self.exchange(requirement_ids=['list-items'])
        audit={'audit_id':'audit-v1','scope':'feature','baseline_kind':'greenfield','surface':'backend',
               'inspected_paths':['.'],'features':[],'unresolved':[],
               'requirements':[{'requirement_id':'list-items','text':'List items','decision_ids':['choose-layout']}],
               'evidence_paths':[self.artifact('discovery/empty.txt','No application code exists')]}
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan())
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',self.blueprint()))
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',self.coverage()))
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        packet = self.slice()
        self.cli('slice-create', '--contract-file', self.payload('slice', packet), ok=False)
        packet['allowed_paths'].append('tests/test_items.py')
        self.cli('slice-create', '--contract-file', self.payload('slice', packet))
        self.cli('validate')

    def test_false_gap_requires_user_correction_and_preserves_old_approval(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        report={'report_id':'false-gap','suspected_features':['list-items'],'summary':'Suspected list gap',
                'evidence_paths':[self.artifact('discovery/report.txt','Suspected failure')]}
        self.cli('missing-feature','--report-file',self.payload('missing',report))
        audit=self.baseline(audit_id='audit-v2'); audit['sweeps']=[audit['sweeps'][-1]]
        audit['missing_report_resolutions']=[{'report_id':'false-gap','resolution':'not_missing',
            'confirmed_by_user':True,'user_note':'I confirmed the feature is present.',
            'evidence_paths':[self.artifact('discovery/correction.txt','Feature observed working')]}]
        self.cli('discovery-record','--audit-file',self.payload('audit',audit),ok=False)
        self.exchange('correct-report',response='I withdraw the report: the feature is working.',
                      report_id='false-gap',resolution='not_missing')
        audit['missing_report_resolutions'][0]['decision_ids']=['correct-report']
        self.record_baseline(audit)
        self.assertEqual(self.manifest()['active_approval_id'],'approve-spec-v1')
        self.assertFalse(self.manifest()['coverage_pending'])
        self.cli('validate')
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()))
        source = self.root / 'src/service.py'
        source.write_text(source.read_text() + '\n# Approved in-scope refactor\n')
        self.cli('validate')
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertTrue(resumed['safe_to_continue'])
        report = {'slice_id': 'items-slice', 'owner': 'builder', 'status': 'submitted',
                  'summary': 'Refactored item access', 'implementation_paths': ['src/service.py']}
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('report', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', self.review()))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        self.assertEqual(self.manifest()['active_approval_id'], 'approve-spec-v1')

    def test_duplicate_coverage_and_missing_backend_contract_are_rejected(self):
        self.start(); self.exchange(); self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec=self.blueprint(); spec['features'][0].pop('backend_contract')
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec),ok=False)
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',self.blueprint()))
        coverage=self.coverage(); coverage['mappings']*=2
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',coverage),ok=False)

    def test_wrong_observation_cannot_be_self_asserted_as_pass(self):
        self.assigned()
        review=self.review()
        check=review['checks'][0]
        self.artifact(check['output_file'],json.dumps({'check_id':check['check_id'],'result':'PASS',
            'expected':check['expected'],'actual':'Unauthenticated gets 200'}))
        self.cli('reconcile','--slice-id','items-slice','--action','verify',
                 '--review-file',self.payload('review',review),ok=False)

    def test_forged_registry_cannot_erase_conversation(self):
        self.start(); self.exchange(status='open',response=None)
        manifest=self.manifest(); manifest['conversation']={}
        (self.directory/'manifest.json').write_text(json.dumps(manifest))
        self.cli('validate',ok=False)

    def test_mode_crossing_is_blocked(self):
        self.prepared()
        approval=self.approval(); approval['mode']='source'
        self.cli('approve','--approval-file',self.payload('approval',approval),ok=False)

    def test_prototype_initiative_cannot_use_blueprint_phases(self):
        self.cli('init','--scope','feature','--mode','prototype')
        work={'task_id':'fake-plan','phase':'plan','status':'in_progress','summary':'Fake migration',
            'next_action':'Continue','completed_steps':[],'pending_steps':['Plan'],'blockers':[],
            'code_paths':[],'artifact_paths':[]}
        self.cli('work-update','--work-file',self.payload('work',work),ok=False)

    def test_blueprint_coverage_requires_every_feature_baseline_pair(self):
        self.start(); self.exchange(); self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec=self.blueprint(); duplicate=copy.deepcopy(spec['features'][0]); duplicate['feature_id']='second-feature'
        duplicate['acceptance_checks'][0]['check_id']='second-check'; spec['features'].append(duplicate)
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',self.coverage()),ok=False)

    def test_ui_feature_requires_behavior_checks_as_well_as_visual_values(self):
        self.start(); self.exchange(); self.record_baseline(self.baseline('ui'))
        self.record_reviewed_plan(self.plan())
        spec=self.blueprint(surface='ui')
        spec['features'][0]['acceptance_checks']=[c for c in spec['features'][0]['acceptance_checks'] if c['kind']=='visual']
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec),ok=False)

    def test_superseded_decision_cannot_authorize_a_new_approval(self):
        self.prepared()
        self.exchange('correct-choice',supersedes='choose-layout')
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)

    def test_render_on_wrong_route_or_viewport_is_not_approved_evidence(self):
        self.assigned('ui')
        review=self.review('ui')
        review['rendered_cases'][0]['route']='/unrelated'
        self.cli('reconcile','--slice-id','items-slice','--action','verify',
                 '--review-file',self.payload('review',review),ok=False)

    def test_approved_specs_cannot_lose_decisions_from_the_plan(self):
        self.start(); self.exchange(); self.exchange('decide-error',response='Use retryable errors.')
        self.record_baseline()
        plan=self.plan(); plan['decision_ids'].append('decide-error')
        self.record_reviewed_plan(plan)
        spec=self.blueprint()
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec),ok=False)

    def test_accepted_approval_hash_inventory_cannot_be_forged(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        manifest=self.manifest()
        manifest['approvals']['approve-spec-v1']['user_approval_note']='Fabricated replacement'
        (self.directory/'manifest.json').write_text(json.dumps(manifest))
        self.cli('validate',ok=False)

    def test_second_answer_requires_explicit_correction_link(self):
        self.start()
        question = self.exchange(status='open',response=None)
        self.exchange('answer-one',responds_to='choose-layout')
        value={
            'exchange_id':'answer-two','phase':'discover','question':question['question'],
            'response':'No, remove it','status':'decided','responds_to':'choose-layout',
            'feature_ids':['list-items'],'requirement_ids':[], 'options':[],
            'source_paths':[],'illustration_paths':[]}
        self.cli('conversation-record','--exchange-file',self.payload('answer2',value),ok=False)
        value['supersedes']='answer-one'
        self.cli('conversation-record','--exchange-file',self.payload('correction',value))

    def test_answer_with_different_question_leaves_original_pending(self):
        self.start()
        question = self.exchange(status='open')
        answer = dict(question, exchange_id='answer-one', responds_to=question['exchange_id'],
                      question='Should we change the response shape?', status='decided', response='Yes.')
        self.cli('conversation-record', '--exchange-file', self.payload('answer', answer), ok=False)
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual([entry['question'] for entry in packet['pending_questions']], [question['question']])

    def test_review_pending_question_is_returned_even_when_approval_is_blocked(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        self.exchange('new-question',status='open',response=None)
        result=self.cli('resume','--format','json',ok=False)
        packet=json.loads(result.stdout)
        self.assertEqual(packet['pending_questions'][0]['exchange_id'],'new-question')
        self.assertFalse(packet['safe_to_continue'])

    def test_review_new_design_decision_cannot_be_omitted_from_old_spec(self):
        self.prepared(); self.exchange('new-rule',response='Use cursor pagination.')
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)

    def test_review_stale_source_before_first_approval_is_rejected(self):
        self.prepared(); (self.root/'src/service.py').write_text('def removed(): return []')
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)

    def test_resume_and_validate_detect_audit_source_drift_before_delivery(self):
        self.prepared()
        (self.root/'src/service.py').write_text('def list_items(): return [1]\n')
        packet = json.loads(self.cli('resume', '--format', 'json', ok=False).stdout)
        self.assertFalse(packet['safe_to_continue'])
        self.assertFalse(packet['active_work'])
        self.assertIn('src/service.py', self.cli('validate', ok=False).stderr)

    def test_first_slice_rechecks_audited_source_after_approval(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        (self.root/'src/service.py').write_text('def list_items(): return [1]\n')
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()), ok=False)
        self.assertFalse(self.manifest()['slices'])

    def test_review_answer_cannot_expand_the_question_scope(self):
        self.start(); self.exchange(status='open',response=None)
        answer={'exchange_id':'broader-answer','phase':'discover','question':'Keep authorization?',
            'options':[],'response':'Yes','status':'decided','responds_to':'choose-layout',
            'feature_ids':['list-items','delete-users'],'requirement_ids':[],
            'source_paths':[],'illustration_paths':[],
            'authorized_changes':[{'baseline_id':'delete-users','classification':'REMOVE'}]}
        self.cli('conversation-record','--exchange-file',self.payload('broader',answer),ok=False)

    def test_review_roadmap_waits_for_pending_question(self):
        self.start(); self.exchange(); self.record_baseline()
        self.exchange('still-open',status='open',response=None)
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()),ok=False)

    def test_review_waiting_cursor_blocks_slice_creation(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        self.exchange('delivery-question', phase='approved', status='open', response=None)
        work={'pending_question_id':'delivery-question','task_id':'await-decision','phase':'approved','status':'waiting_user','summary':'Need user choice',
            'next_action':'Wait','completed_steps':[],'pending_steps':['User choice'],'blockers':[],
            'code_paths':[],'artifact_paths':[]}
        self.cli('work-update','--work-file',self.payload('work',work))
        self.cli('slice-create','--contract-file',self.payload('slice',self.slice()),ok=False)

    def test_review_preexisting_unfamiliar_source_is_not_greenfield(self):
        self.start(); self.exchange(requirement_ids=['list-items'])
        (self.root/'App.cs').write_text('class App { static void Main() {} }')
        audit={'audit_id':'empty-v1','scope':'feature','baseline_kind':'greenfield','surface':'backend',
               'inspected_paths':['.'],'features':[],'unresolved':[],
               'requirements':[{'requirement_id':'list-items','text':'List items','decision_ids':['choose-layout']}],
               'evidence_paths':[self.artifact('discovery/empty.txt','Inspected project tree')]}
        self.cli('discovery-record','--audit-file',self.payload('audit',audit),ok=False)

    def test_review_false_report_cannot_use_an_unrelated_old_decision(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        report={'report_id':'real-gap','suspected_features':['list-items'],'summary':'List is missing',
                'evidence_paths':[self.artifact('discovery/real-gap.txt','Actual gap')]}
        self.cli('missing-feature','--report-file',self.payload('missing',report))
        audit=self.baseline(audit_id='audit-v2'); audit['sweeps']=[audit['sweeps'][-1]]
        audit['missing_report_resolutions']=[{'report_id':'real-gap','resolution':'not_missing',
            'confirmed_by_user':True,'user_note':'Generic note','decision_ids':['choose-layout'],
            'evidence_paths':[self.artifact('discovery/other.txt','Unrelated evidence')]}]
        self.cli('discovery-record','--audit-file',self.payload('audit',audit),ok=False)

    def test_review_deleted_test_source_blocks_acceptance(self):
        self.assigned(); review=self.review()
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',review))
        (self.root/'tests/test_items.py').unlink()
        self.cli('reconcile','--slice-id','items-slice','--action','accept',ok=False)

    def test_review_png_cannot_be_used_as_dom_evidence_for_all_states(self):
        self.assigned('ui'); review=self.review('ui')
        same=review['rendered_cases'][0]['screenshot']
        for case in review['rendered_cases']:
            case['screenshot']=same; case['dom_file']=same
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',review),ok=False)

    def interrupt_manifest_write(self, operation):
        write = FIXTURE_CLI.atomic_json

        def fail_manifest(path, value):
            if path == self.directory / 'manifest.json':
                raise OSError('interrupted manifest write')
            return write(path, value)

        with patch.object(FIXTURE_CLI, 'atomic_json', side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, 'interrupted manifest write'):
                operation()

    def test_interrupted_approval_retries_the_same_decision(self):
        self.prepared()
        path = self.payload('approval', self.approval())
        self.interrupt_manifest_write(lambda: FIXTURE_CLI.approve(self.directory, str(path)))
        self.cli('validate', ok=False)
        changed = json.loads(path.read_text())
        changed['user_approval_note'] = 'Different approval'
        rejected = self.cli('approve', '--approval-file', self.payload('changed', changed), ok=False)
        self.assertIn('exact original input', rejected.stderr)
        self.cli('approve', '--approval-file', path)
        self.cli('validate')
        self.assertEqual(self.manifest()['active_approval_id'], 'approve-spec-v1')
        events = map(json.loads, (self.directory / 'decisions.jsonl').read_text().splitlines())
        self.assertEqual(sum(event['type'] == 'approval' for event in events), 1)

    def test_interrupted_withdrawal_blocks_delivery_and_retries(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        reason = 'Withdraw the approved scope'
        self.interrupt_manifest_write(lambda: FIXTURE_CLI.revoke_approval(self.directory, reason))
        self.cli('resume', '--format', 'json', ok=False)
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()), ok=False)
        self.cli('revoke-approval', '--reason', reason)
        self.assertIsNone(self.manifest()['active_approval_id'])
        self.cli('validate')

    def test_interrupted_missing_report_blocks_delivery_and_retries(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        evidence = self.artifact('discovery/evidence/missing.txt', 'Observed missing action')
        path = self.payload('gap', {'report_id': 'gap', 'summary': 'Missing action',
                                    'suspected_features': ['list-items'], 'evidence_paths': [evidence]})
        self.interrupt_manifest_write(lambda: FIXTURE_CLI.report_missing_feature(self.directory, str(path)))
        self.cli('resume', '--format', 'json', ok=False)
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()), ok=False)
        self.cli('missing-feature', '--report-file', path)
        self.assertEqual(self.manifest()['missing_reports']['gap']['status'], 'open')

    def test_interrupted_blocker_blocks_delivery_and_retries(self):
        self.assigned()
        evidence = self.artifact('slices/items-slice/evidence/defect.txt', 'Failure')
        path = self.payload('blocker', {'slice_id': 'items-slice', 'reason': 'Concrete defect',
                                       'next_action': 'Fix defect', 'evidence_files': [evidence]})
        self.interrupt_manifest_write(lambda: FIXTURE_CLI.reconcile_slice(
            self.directory, 'items-slice', 'block', str(path)))
        self.cli('resume', '--format', 'json', ok=False)
        review = self.payload('review', self.review())
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify', '--review-file', review, ok=False)
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'block', '--review-file', path)
        self.assertEqual(self.manifest()['slices']['items-slice']['status'], 'blocked')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify', '--review-file', review, ok=False)

    def test_legacy_withdrawal_journal_mismatch_blocks_new_slices(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        FIXTURE_CLI.journal(self.directory, {'type': 'approval_revoked', 'decision_id': 'approve-spec-v1',
                                             'reason': 'Interrupted before older CLI saved the manifest'})
        resumed = json.loads(self.cli('resume', '--format', 'json', ok=False).stdout)
        self.assertFalse(resumed['safe_to_continue'])
        self.assertIn('approval/withdrawal journal', ' '.join(resumed['validation_errors']))
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()), ok=False)

    def test_reapproval_after_withdrawal_rechecks_old_slice_paths(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        self.cli('slice-create', '--contract-file', self.payload('slice', self.slice()))
        self.cli('revoke-approval', '--reason', 'Withdraw before changing source')
        source = self.root / 'src/service.py'
        source.write_text(source.read_text().replace('return []', 'return ["changed"]', 1))
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec2', self.blueprint('spec-v2')))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('cov2', self.coverage('spec-v2')))
        result = self.cli('approve', '--approval-file', self.payload('approval2', self.approval('spec-v2')), ok=False)
        self.assertIn('A source changed since discovery: src/service.py', result.stderr)

    def test_review_explicit_defect_requires_unblock_before_verification(self):
        self.assigned()
        blocker={'slice_id':'items-slice','reason':'Actual defect','next_action':'Fix defect',
                 'evidence_files':[self.artifact('slices/items-slice/evidence/defect.txt','Failure')]}
        self.cli('reconcile','--slice-id','items-slice','--action','block','--review-file',self.payload('blocker',blocker))
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',self.review()),ok=False)
        self.assertEqual(self.manifest()['slices']['items-slice']['status'],'blocked')

    def test_review_incomplete_accepted_coverage_cannot_enter_integration(self):
        self.start(); self.exchange(); self.exchange('other-decision',feature_ids=['other-items'])
        audit=self.baseline(); other=copy.deepcopy(audit['features'][0]); other['feature_id']='other-items'
        audit['features'].append(other); audit['sweeps'][0]['new_feature_ids'].append('other-items')
        audit['behavior_contracts']['other-items'] = copy.deepcopy(audit['behavior_contracts']['list-items'])
        self.record_baseline(audit)
        plan=self.plan(); plan['decision_ids'].append('other-decision')
        self.record_reviewed_plan(plan)
        spec=self.blueprint(); spec['decision_ids'].append('other-decision')
        other=copy.deepcopy(spec['features'][0]); other.update(feature_id='other-feature',baseline_ids=['other-items'],decision_ids=['other-decision'])
        other['acceptance_checks'][0]['check_id']='other-check'; spec['features'].append(other)
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))
        coverage=self.coverage(); coverage['mappings'].append({'baseline_id':'other-items','feature_id':'other-feature','acceptance_ids':['other-check']})
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',coverage))
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        self.cli('slice-create','--contract-file',self.payload('slice',self.slice()))
        report={'slice_id':'items-slice','owner':'builder','status':'submitted','summary':'One feature only',
            'implementation_paths':['src/service.py'],'functional_evidence':[], 'visual_evidence':[], 'blockers':[], 'next_action':'Review'}
        self.cli('slice-report','--slice-id','items-slice','--owner','builder','--report-file',self.payload('report',report))
        self.cli('reconcile','--slice-id','items-slice','--action','report')
        self.cli('reconcile','--slice-id','items-slice','--action','verify','--review-file',self.payload('review',self.review()))
        self.cli('reconcile','--slice-id','items-slice','--action','accept')
        self.assertEqual(self.manifest()['stage'],'implement')

    def test_review_merged_features_can_use_independent_scoped_decisions(self):
        self.start(); self.exchange(); self.exchange('channel-rule',feature_ids=['channel-feature'])
        audit=self.baseline(); other=copy.deepcopy(audit['features'][0]); other['feature_id']='channel-feature'
        audit['features'].append(other); audit['sweeps'][0]['new_feature_ids'].append('channel-feature')
        audit['behavior_contracts']['channel-feature'] = copy.deepcopy(audit['behavior_contracts']['list-items'])
        self.record_baseline(audit)
        plan=self.plan(); plan['decision_ids'].append('channel-rule')
        self.record_reviewed_plan(plan)
        spec=self.blueprint(); spec['decision_ids'].append('channel-rule')
        spec['features'][0]['baseline_ids'].append('channel-feature')
        spec['features'][0]['decision_ids'].append('channel-rule')
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))

    def test_review_greenfield_requirement_can_be_amended_without_rewriting_baseline(self):
        self.start(); self.exchange(requirement_ids=['list-items'])
        audit={'audit_id':'empty-v1','scope':'feature','baseline_kind':'greenfield','surface':'backend',
            'inspected_paths':['.'],'features':[],'unresolved':[],
            'requirements':[{'requirement_id':'list-items','text':'Members can list','decision_ids':['choose-layout']}],
            'evidence_paths':[self.artifact('discovery/empty.txt','No existing application')]}
        self.record_baseline(audit)
        self.exchange('new-membership',supersedes='choose-layout',requirement_ids=['list-items'],
                      response='Moderators only.',authorized_changes=[{'baseline_id':'list-items','classification':'MODIFY'}])
        audit['audit_id']='empty-v2'
        self.record_baseline(audit)

    def test_review_orphaned_record_is_detected_and_exact_retry_recovers(self):
        self.start(); value=self.exchange()
        manifest=self.manifest(); entry=manifest['conversation'].pop('choose-layout')
        # Model the interruption after record rename but before journal/manifest persistence.
        (self.directory/'manifest.json').write_text(json.dumps(manifest))
        journal=self.directory/'decisions.jsonl'
        lines=[line for line in journal.read_text().splitlines() if json.loads(line).get('record_id')!='choose-layout']
        journal.write_text('\n'.join(lines)+'\n' if lines else '')
        self.cli('validate',ok=False)
        self.cli('conversation-record','--exchange-file',self.payload('retry',value))
        self.assertEqual(self.manifest()['conversation']['choose-layout']['sha256'],entry['sha256'])
        self.cli('validate')

    def test_review_prototype_coverage_cannot_mutate_blueprint_mode(self):
        self.start(); self.record_baseline()
        payload={'prototype_version':'v1','audit_id':'audit-v1','features':[],
                 'exceptions':[{'feature_id':'list-items','reason':'Image only'}]}
        before=self.manifest()
        result=self.cli('prototype-coverage','--coverage-file',self.payload('proto',payload),ok=False)
        self.assertIn('prototype mode',result.stderr)
        self.assertEqual(self.manifest(),before)

    def test_review_backend_resume_names_blueprint_contracts_not_a_runnable_clone(self):
        self.prepared(); self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        packet=json.loads(self.cli('resume','--format','json').stdout)
        self.assertIn('blueprint',packet['next_action'].lower())
        self.assertIn('blueprints/spec-v1/items.md',packet['approved_spec_paths'])
        report={'report_id':'gap-guidance','suspected_features':['list-items'],'summary':'Missing handling',
                'evidence_paths':[self.artifact('discovery/gap.txt','Evidence')]}
        self.cli('missing-feature','--report-file',self.payload('gap',report))
        self.assertIn('blueprint',self.manifest()['next_action'].lower())

    def test_audit_global_keep(self):
        self.start(); self.exchange(feature_ids=[],requirement_ids=[])
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec=self.blueprint()
        spec['features'][0]['decision_ids']=[]
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))


    def test_audit_recap(self):
        self.prepared()
        self.exchange('recap-confirmation',response='Yes, your decision recap is accurate.',record_type='recap')
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))

    def test_workstream_checkpoint_survives_resume_and_keeps_approval_separate(self):
        self.prepared()
        presentation = self.artifact('review/items-v1.md', 'Saved items workstream terminal presentation.')
        prompt = {
            'exchange_id': 'items-checkpoint', 'phase': 'blueprint', 'record_type': 'review_checkpoint',
            'review_step': 'workstream', 'review_target': 'spec-v1 / Service and API (1 of 1)',
            'status': 'open', 'question': 'Say Continue when ready for the consistency check.',
            'feature_ids': ['list-items'], 'evidence_paths': [presentation],
        }
        self.cli('conversation-record', '--exchange-file', self.payload('checkpoint', prompt))
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertFalse(packet['safe_to_continue'])
        self.assertIn(prompt['review_target'], packet['next_action'])
        cursor = {
            'task_id': 'review-workstreams', 'phase': 'blueprint', 'status': 'waiting_user',
            'summary': prompt['review_target'], 'next_action': 'Wait for Continue',
            'completed_steps': [], 'pending_steps': ['Review Service and API', 'Check blueprint consistency'],
            'blockers': [], 'code_paths': [], 'artifact_paths': [presentation],
            'pending_question_id': prompt['exchange_id'],
        }
        self.cli('work-update', '--work-file', self.payload('cursor', cursor))
        answer = {**prompt, 'exchange_id': 'items-continue', 'status': 'decided',
                  'responds_to': prompt['exchange_id'], 'review_action': 'continue'}
        for reply in ('Correct', 'Looks good!', 'I understand.'):
            result = self.cli('conversation-record', '--exchange-file',
                              self.payload('answer', {**answer, 'response': reply}), ok=False)
            self.assertIn('explicit navigation', result.stderr)
        result = self.cli('conversation-record', '--exchange-file', self.payload('wrong-type', {
            **answer, 'record_type': 'decision', 'response': 'Continue',
        }), ok=False)
        self.assertIn('same review checkpoint', result.stderr)
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertFalse(packet['safe_to_continue'])
        self.assertEqual(packet['pending_questions'][0]['review_target'], prompt['review_target'])
        self.assertEqual(packet['next_action'], 'Wait for Continue')
        self.assertIn(f"REVIEW CHECKPOINT {prompt['review_target']}: waiting for reply", self.cli('resume').stdout)
        approval = self.approval()
        self.cli('approve', '--approval-file', self.payload('approval', approval), ok=False)
        question = self.exchange('items-open', status='open')
        result = self.cli('conversation-record', '--exchange-file',
                          self.payload('answer', {**answer, 'response': 'Next'}), ok=False)
        self.assertIn('Resolve outstanding design questions', result.stderr)
        self.exchange('items-answered', status='rejected', responds_to=question['exchange_id'],
                      response='Drop this question. Keep the confirmed blueprint behavior.')
        self.cli('conversation-record', '--exchange-file', self.payload('answer', {**answer, 'response': 'Next'}))
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(packet['active_question_answer']['response'], 'Next')
        self.assertIsNone(packet['approval_id'])
        self.assertIn(f"REVIEW CHECKPOINT {prompt['review_target']}: continue", self.cli('resume').stdout)
        cursor.pop('pending_question_id')
        consistency = self.artifact('review/spec-v1-consistency.md',
                                    'Service and API covers list-items. Items and membership contracts agree.')
        cursor['artifact_paths'].append(consistency)
        cursor.update(status='done', next_action='Present exact blueprint for approval',
                      completed_steps=cursor['pending_steps'], pending_steps=[])
        self.cli('work-update', '--work-file', self.payload('cursor', cursor))
        approval['shown_paths'].append(consistency)
        self.cli('approve', '--approval-file', self.payload('approval', approval))
        saved = self.manifest()['approvals'][approval['decision_id']]
        self.assertIn(consistency, saved['presentation_evidence_hashes'])

    def test_discovery_and_roadmap_finalization_wait_for_their_presentations(self):
        self.start()
        audit = self.baseline()
        plan = self.plan()
        for step, phase, target, command, argument, payload in (
            ('discovery', 'discover', 'Items audit draft', 'discovery-record', '--audit-file', audit),
            ('roadmap', 'plan', 'Items roadmap draft', 'plan-record', '--plan-file', plan),
        ):
            with self.subTest(step=step):
                prompt = {
                    'exchange_id': f'{step}-prompt', 'phase': phase, 'record_type': 'review_checkpoint',
                    'review_step': step, 'review_target': target, 'status': 'open',
                    'question': 'Does this reflect the intended behavior?',
                    'feature_ids': ['list-items'],
                    'evidence_paths': [self.artifact(f'review/{step}.md', f'Actual {step} presentation.')],
                }
                self.cli('conversation-record', '--exchange-file', self.payload('prompt', prompt))
                self.cli(command, argument, self.payload(step, payload), ok=False)
                answer = {
                    **prompt, 'exchange_id': f'{step}-confirmed', 'responds_to': prompt['exchange_id'],
                    'status': 'decided', 'response': 'Correct', 'review_action': 'confirm',
                }
                self.cli('conversation-record', '--exchange-file', self.payload('answer', answer))
                if step == 'roadmap':
                    payload['decision_ids'] = []
                self.cli(command, argument, self.payload(step, payload))

    def test_conversational_plan_review_blocks_blueprint_until_explicit_handoff(self):
        self.start(plan_review=True)
        self.exchange()
        self.record_baseline()
        plan = self.plan()
        plan['workstreams'] = ['service', 'permissions']
        plan_file = self.payload('plan', plan)
        blueprint = self.blueprint()
        blueprint_file = self.payload('blueprint', blueprint)

        def present(step, question, stream=None, version='plan-v1', ok=True):
            key = f'{version}-{stream or step.replace("_", "-")}'
            prompt = {
                'exchange_id': key, 'phase': 'plan', 'record_type': 'review_checkpoint',
                'review_step': step, 'review_version': version, 'review_target': stream or step,
                'status': 'open', 'question': question, 'feature_ids': ['list-items'],
                'evidence_paths': [self.artifact(f'review/{key}.md', question)],
            }
            if stream:
                prompt['review_workstream'] = stream
            self.cli('conversation-record', '--exchange-file', self.payload('prompt', prompt), ok=ok)
            return prompt

        def reply(prompt, action, response, ok=True):
            self.cli('conversation-record', '--exchange-file', self.payload('reply', {
                **prompt, 'exchange_id': prompt['exchange_id'] + '-reply',
                'responds_to': prompt['exchange_id'], 'status': 'decided',
                'response': response, 'review_action': action,
            }), ok=ok)

        result = self.cli('plan-record', '--plan-file', plan_file, ok=False)
        self.assertIn('roadmap terminal review', result.stderr)
        reply(present('roadmap', 'Here is the roadmap. Any corrections?'), 'confirm', 'Looks good.')
        self.cli('plan-record', '--plan-file', plan_file)
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(packet['phase'], 'plan')
        self.assertEqual(packet['plan_review']['current_workstream'], 'service')
        result = self.cli('blueprint-record', '--blueprint-file', blueprint_file, ok=False)
        self.assertIn('Continue to blueprint', result.stderr)
        self.assertFalse((self.directory / 'blueprints/spec-v1/index.md').exists())
        present('plan_workstream', 'Continue?', 'permissions', ok=False)
        service = present('plan_workstream', 'Continue to Permissions?', 'service')
        reply(service, 'continue', 'Looks good.', ok=False)
        reply(service, 'skip', 'Skip it and move on.', ok=False)
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(packet['pending_questions'][0]['exchange_id'], service['exchange_id'])
        self.assertFalse(packet['safe_to_continue'])
        question = self.exchange('plan-detail-question', status='open', phase='plan')
        reply(service, 'continue', 'Continue', ok=False)
        self.exchange('plan-detail-answer', status='rejected', phase='plan',
                      responds_to=question['exchange_id'], response='Drop that alternative.')
        reply(service, 'continue', 'Continue.')
        present('plan_completion', 'Continue to blueprint?', ok=False)
        permissions = present('plan_workstream', 'Continue to the coverage check?', 'permissions')
        reply(permissions, 'continue', 'Continue')
        completion = present('plan_completion', 'Coverage and decisions checked. Continue to blueprint?')
        reply(completion, 'continue', 'Continue', ok=False)
        reply(completion, 'continue', 'Do not continue to blueprint.', ok=False)
        reply(completion, 'continue', 'Continue to blueprint.')
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(packet['phase'], 'blueprint')
        self.assertEqual(packet['plan_review']['authorization_id'], completion['exchange_id'] + '-reply')
        self.assertIsNone(packet['approval_id'])
        self.cli('blueprint-record', '--blueprint-file', blueprint_file)
        self.assertIn(f"conversation/{completion['exchange_id']}-reply.json",
                      self.manifest()['blueprints']['spec-v1']['evidence_hashes'])
        self.cli('validate')

        revised_plan = self.plan('plan-v2')
        revised_plan['workstreams'] = ['service']
        revised_plan_file = self.payload('plan-revision', revised_plan)
        reply(present('roadmap', 'The revised roadmap has one workstream.', version='plan-v2'),
              'confirm', 'That covers the scope.')
        self.cli('plan-record', '--plan-file', revised_plan_file)
        self.cli('blueprint-record', '--blueprint-file',
                 self.payload('stale-plan-blueprint', self.blueprint('spec-v2')), ok=False)
        current = present('plan_workstream', 'Continue to coverage?', 'service', version='plan-v2')
        reply(current, 'revise', 'Make permissions its own workstream again.')
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertTrue(packet['plan_review']['revision_required'])
        self.assertIsNone(packet['plan_review']['authorization_id'])
        self.assertIn('revised implementation plan', packet['next_action'])
        reply(current, 'continue', 'Continue', ok=False)

    def test_legacy_roadmap_requires_plan_review_before_first_blueprint(self):
        self.start()
        self.exchange()
        self.record_baseline()
        plan = self.plan()
        self.cli('plan-record', '--plan-file', self.payload('legacy-plan', plan))
        spec = self.blueprint()
        result = self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)
        self.assertIn('Continue to blueprint', result.stderr)
        overview = self.plan_checkpoint(plan, 'roadmap')
        self.plan_reply(overview, 'confirm', 'The roadmap covers the scope.')
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['plan_review']['current_workstream'], plan['workstreams'][0])
        self.assertIn(plan['workstreams'][0], resumed['next_action'])
        self.finish_plan_review(plan)
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))

    def test_late_planning_decision_requires_a_new_handoff(self):
        self.start(plan_review=True)
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        self.exchange('change-roadmap', phase='plan', question='Add a workstream for permissions?',
                      response='Yes, review permissions in a separate workstream.')
        spec = self.blueprint()
        spec['decision_ids'].append('change-roadmap')
        result = self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)
        self.assertIn('Continue to blueprint', result.stderr)
        self.assertFalse((self.directory / 'blueprints/spec-v1/index.md').exists())
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['phase'], 'plan')
        progress = resumed['plan_review']
        self.assertTrue(progress['revision_required'])
        self.assertIsNone(progress['authorization_id'])
        revised = self.plan('plan-v2')
        revised['decision_ids'].append('change-roadmap')
        revised['workstreams'].append('Permissions')
        self.record_reviewed_plan(revised)
        spec['plan_version'] = 'plan-v2'
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))

    def test_followup_audit_cannot_strand_an_open_plan_checkpoint(self):
        self.start(plan_review=True)
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', self.blueprint()))
        plan = self.plan('plan-v2')
        overview = self.plan_checkpoint(plan, 'roadmap')
        self.plan_reply(overview, 'confirm', 'The roadmap covers the scope.')
        self.cli('plan-record', '--plan-file', self.payload('plan', plan))
        checkpoint = self.plan_checkpoint(plan, 'plan_workstream', stream=plan['workstreams'][0])
        audit = self.baseline(audit_id='audit-v2')
        audit['sweeps'] = audit['sweeps'][-1:]
        result = self.cli('discovery-record', '--audit-file', self.payload('audit', audit), ok=False)
        self.assertIn('Resolve the current plan checkpoint', result.stderr)
        self.plan_reply(checkpoint, 'revise', 'Refresh the plan from the new audit.')
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan('plan-v3', 'audit-v2'))

    def test_guided_approval_requires_all_workstreams_and_consistency_evidence(self):
        self.start(review=True, guided=True)
        self.exchange()

        def checkpoint(step, phase, version, name=None, *, skip=False):
            key = name or step
            prompt = {
                'exchange_id': f'{key}-prompt', 'phase': phase, 'record_type': 'review_checkpoint',
                'review_step': step, 'review_version': version, 'review_target': key,
                'status': 'open', 'question': 'Ready to continue?',
                'feature_ids': ['items-feature' if name else 'list-items'],
                'evidence_paths': [self.artifact(f'review/{key}.md', f'Terminal walkthrough for {key}.')],
            }
            if name:
                prompt['review_workstream'] = name
            if skip:
                self.cli('conversation-record', '--exchange-file', self.payload('waiver', {
                    **prompt, 'exchange_id': f'{key}-waiver', 'status': 'decided',
                    'response': f'Skip {name} and move on', 'review_action': 'skip',
                }))
                return
            self.cli('conversation-record', '--exchange-file', self.payload('prompt', prompt))
            self.cli('conversation-record', '--exchange-file', self.payload('response', {
                **prompt, 'exchange_id': f'{key}-reply', 'status': 'decided',
                'responds_to': prompt['exchange_id'], 'response': 'Next' if name else 'Correct',
                'review_action': 'continue' if name else 'confirm',
            }))

        audit = self.baseline()
        result = self.cli('discovery-record', '--audit-file', self.payload('audit', audit), ok=False)
        self.assertIn('discovery terminal review', result.stderr)
        checkpoint('discovery', 'discover', 'audit-v1')
        self.record_baseline(audit)
        plan = self.plan()
        plan['workstreams'] = ['service', 'permissions']
        result = self.cli('plan-record', '--plan-file', self.payload('plan', plan), ok=False)
        self.assertIn('roadmap terminal review', result.stderr)
        checkpoint('roadmap', 'plan', 'plan-v1')
        self.cli('plan-record', '--plan-file', self.payload('plan', plan))
        self.finish_plan_review(plan)
        blueprint = self.blueprint()
        self.cli('blueprint-record', '--blueprint-file', self.payload('blueprint', blueprint))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage()))
        approval = self.approval()
        checkpoint('workstream', 'blueprint', 'spec-v1', 'service')
        result = self.cli('approve', '--approval-file', self.payload('approval', approval), ok=False)
        self.assertIn('permissions', result.stderr)
        checkpoint('workstream', 'blueprint', 'spec-v1', 'permissions', skip=True)
        result = self.cli('approve', '--approval-file', self.payload('approval', approval), ok=False)
        self.assertIn('consistency report', result.stderr)
        report = self.artifact('review/spec-v1-consistency.md',
                               'Service and permissions agree on membership policy and 403 behavior.')
        approval['workstream_consistency_path'] = report
        approval['shown_paths'].append(report)
        self.cli('approve', '--approval-file', self.payload('approval', approval))
        receipt = self.manifest()['approvals'][approval['decision_id']]
        self.assertIn(report, receipt['presentation_evidence_hashes'])
        self.assertIn('conversation/permissions-waiver.json', receipt['artifact_hashes'])
        self.cli('validate')

        unchanged = self.blueprint('spec-v2')
        self.cli('blueprint-record', '--blueprint-file', self.payload('blueprint', unchanged))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage('spec-v2')))
        approval = self.approval('spec-v2')
        report = self.artifact('review/spec-v2-consistency.md',
                               'Both workstream contracts match the reviews on spec-v1.')
        approval['workstream_consistency_path'] = report
        approval['shown_paths'].append(report)
        self.cli('approve', '--approval-file', self.payload('approval', approval))
        self.cli('validate')

        changed = self.blueprint('spec-v3')
        changed['features'][0]['anatomy'] += ' through an item repository interface'
        self.cli('blueprint-record', '--blueprint-file', self.payload('blueprint', changed))
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', self.coverage('spec-v3')))
        result = self.cli('approve', '--approval-file', self.payload('approval', self.approval('spec-v3')), ok=False)
        self.assertIn('Workstream service changed; review it against spec-v3', result.stderr)
        self.assertEqual(self.manifest()['active_approval_id'], 'approve-spec-v2')


    def test_audit_mixed_fullstack(self):
        self.start(); self.exchange()
        audit=self.baseline('ui'); audit['surface']='fullstack'
        self.record_baseline(audit)
        self.record_reviewed_plan(self.plan())
        spec=self.blueprint(surface='ui'); spec['surface']='fullstack'
        spec['features'][0]['surface']='ui'
        backend=self.blueprint(surface='backend')['features'][0]
        backend['surface']='backend'; backend['feature_id']='items-api'
        backend['acceptance_checks'][0]['check_id']='items-api-check'
        backend['acceptance_checks'][0]['command']=self.test_command('api','items-api-check')
        spec['features'].append(backend)
        self.cli('blueprint-record','--blueprint-file',self.payload('mixed-spec',spec))
        coverage = self.coverage(surface='ui')
        coverage['mappings'].append({
            'baseline_id': 'list-items', 'feature_id': 'items-api',
            'acceptance_ids': ['items-api-check'],
        })
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        packet = self.slice()
        packet.update(feature_ids=['items-api'], functional_tests=['items-api-check'])
        self.cli('slice-create', '--contract-file', self.payload('slice', packet))
        report = {
            'slice_id': 'items-slice', 'owner': 'builder', 'status': 'submitted',
            'summary': 'Implemented API contract', 'implementation_paths': ['src/service.py'],
            'functional_evidence': [], 'visual_evidence': [], 'blockers': [], 'next_action': 'Review',
        }
        self.cli('slice-report', '--slice-id', 'items-slice', '--owner', 'builder',
                 '--report-file', self.payload('report', report))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'report')
        review = self.review()
        check = review['checks'][0]
        check['check_id'] = 'items-api-check'
        check['command'] = self.test_command('api','items-api-check')
        output = self.directory / check['output_file']
        evidence = json.loads(output.read_text())
        evidence['check_id'] = check['check_id']
        output.write_text(json.dumps(evidence))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        self.cli('validate')


    def test_audit_missing_question(self):
        self.start()
        work={'task_id':'waiting-choice','phase':'discover','status':'waiting_user',
              'summary':'Need user choice','next_action':'Wait for answer','completed_steps':[],
              'pending_steps':['Record answer'],'blockers':[],'code_paths':[],'artifact_paths':[]}
        self.cli('work-update','--work-file',self.payload('waiting',work),ok=False)


    def test_audit_state_specific_visual(self):
        self.serve_fixture_states()
        self.start(); self.exchange(); self.record_baseline(self.baseline('ui'))
        self.record_reviewed_plan(self.plan())
        spec=self.blueprint(surface='ui')
        check={'check_id':'success-visibility','kind':'visual','expected':'visible',
               'command':self.test_command('visual','success-visibility'),'case_ids':['success-case'],
               'measurement':{'selector':'#state-success','property':'visibility'}}
        spec['features'][0]['acceptance_checks'].append(check)
        for case in spec['features'][0]['visual_contract']['render_cases']:
            if case['case_id']=='success-case':
                case['command'].append('visibility')
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))
        coverage=self.coverage(surface='ui');coverage['mappings'][0]['acceptance_ids'].append(check['check_id'])
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',coverage))
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))
        packet=self.slice();packet['functional_tests']=['items-access','items-behavior','success-visibility']
        self.cli('slice-create','--contract-file',self.payload('slice',packet))
        report={'slice_id':'items-slice','owner':'builder','status':'submitted','summary':'Implemented states', 'shared_ui_paths':[],
            'implementation_paths':['src/service.py'],'functional_evidence':[], 'visual_evidence':[],
            'blockers':[], 'next_action':'Review'}
        self.cli('slice-report','--slice-id','items-slice','--owner','builder','--report-file',self.payload('report',report))
        self.cli('reconcile','--slice-id','items-slice','--action','report')
        review=self.review('ui')
        output=self.artifact('slices/items-slice/evidence/success-check.json',json.dumps(
            {'check_id':'success-visibility','expected':'visible','actual':'visible','result':'PASS'}))
        review['checks'].append({'check_id':'success-visibility','expected':'visible','observed':'visible',
            'result':'PASS','command':self.test_command('visual','success-visibility'),'output_file':output})
        for rendered in review['rendered_cases']:
            if rendered['case_id']=='success-case':
                rendered['command'].append('visibility')
                path=self.directory/rendered['dom_file'];dom=json.loads(path.read_text())
                dom['checks'].append({'check_id':'success-visibility','expected':'visible','actual':'visible','result':'PASS'})
                path.write_text(json.dumps(dom))
        self.cli('reconcile','--slice-id','items-slice','--action','verify',
                       '--review-file',self.payload('review',review))


    def test_plan_requires_applicable_decisions(self):
        self.start()
        self.exchange()
        self.record_baseline()
        plan = self.plan()
        plan['decision_ids'] = []
        self.cli('plan-record', '--plan-file', self.payload('plan', plan), ok=False)

    def test_blueprint_rejects_conflicting_behavior_authorizations(self):
        self.start()
        self.exchange('modify-access', authorized_changes=[{'baseline_id':'list-items','classification':'MODIFY'}])
        self.record_baseline()
        plan = self.plan()
        plan['decision_ids'] = ['modify-access']
        self.record_reviewed_plan(plan)
        self.exchange('remove-access', authorized_changes=[{'baseline_id':'list-items','classification':'REMOVE'}])
        spec = self.blueprint()
        spec['decision_ids'] = ['modify-access', 'remove-access']
        spec['features'][0].update(classification='MODIFY', decision_ids=spec['decision_ids'])
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)

    def test_followup_audit_checks_current_source_before_reapproval(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        audit = self.baseline(audit_id='audit-v2')
        audit['sweeps'] = [audit['sweeps'][-1]]
        self.record_baseline(audit)
        plan = self.plan('plan-v2', 'audit-v2')
        self.record_reviewed_plan(plan)
        spec = self.blueprint('spec-v2')
        spec.update(plan_version='plan-v2', baseline_id='audit-v2')
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        coverage = self.coverage('spec-v2')
        coverage['baseline_id'] = 'audit-v2'
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage))
        (self.root/'src/service.py').unlink()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval('spec-v2')), ok=False)

    def test_blueprint_slice_cannot_authorize_its_own_paths(self):
        self.prepared()
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        packet = self.slice()
        packet['allowed_paths'] = ['src/**']
        self.cli('slice-create', '--contract-file', self.payload('slice', packet), ok=False)

    def test_verified_check_must_run_successfully(self):
        self.assigned()
        review = self.review()
        (self.root/'tests/test_items.py').write_text('raise RuntimeError("check failed")\n')
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review), ok=False)

    def test_work_cannot_continue_until_linked_question_is_answered(self):
        self.start()
        exchange = self.exchange(status='open', response=None)
        waiting = {'task_id':'discuss-layout','phase':'discover','status':'waiting_user','summary':'Question shown',
            'next_action':'Wait for answer','pending_question_id':'choose-layout','completed_steps':[],
            'pending_steps':['Record answer'], 'blockers':[], 'code_paths':[], 'artifact_paths':[]}
        self.cli('work-update', '--work-file', self.payload('waiting', waiting))
        self.assertIn(exchange['question'], self.cli('resume').stdout)
        resumed = dict(waiting, status='in_progress')
        del resumed['pending_question_id']
        self.cli('work-update', '--work-file', self.payload('continue', resumed), ok=False)
        self.exchange('answer-one', responds_to='choose-layout')
        self.cli('work-update', '--work-file', self.payload('continue', resumed))

    def test_validate_rejects_changed_cursor_evidence(self):
        self.start()
        path = self.root/'app.py'
        path.write_text('pass\n')
        work = {'task_id':'inspect-app','phase':'discover','status':'in_progress','summary':'Inspect app',
            'next_action':'Inspect source','completed_steps':[], 'pending_steps':['Inspect app'],
            'blockers':[], 'code_paths':['app.py'], 'artifact_paths':[]}
        self.cli('work-update', '--work-file', self.payload('work', work))
        path.write_text('print("changed")\n')
        self.cli('validate', ok=False)

    def test_greenfield_rejects_existing_static_application_assets(self):
        self.start()
        self.exchange(requirement_ids=['list-items'])
        (self.root/'index.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (self.root/'package.json').write_text('{"scripts":{"start":"serve ."}}')
        audit = {'audit_id':'audit-v1','scope':'feature','baseline_kind':'greenfield','surface':'ui',
                 'inspected_paths':['.'],'features':[],'unresolved':[],
                 'requirements':[{'requirement_id':'list-items','text':'List items','decision_ids':['choose-layout']}],
                 'evidence_paths':[self.artifact('discovery/empty.txt','Claimed empty')]}
        self.cli('discovery-record', '--audit-file', self.payload('audit', audit), ok=False)

    def test_blueprint_does_not_freeze_placeholder_index(self):
        self.start()
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint()
        self.artifact(spec['document_paths'][0], '# Blueprint\n')
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)

    def test_resume_traces_immutable_decisions_and_coverage_to_slices(self):
        self.assigned()
        packet = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(packet['conversation_usage']['choose-layout'], {
            'plan_ids': ['plan-v1'], 'blueprint_ids': ['spec-v1'], 'slice_ids': ['items-slice']
        })
        self.assertEqual(packet['coverage_with_slices'], [{
            'baseline_id': 'list-items', 'feature_id': 'items-feature',
            'acceptance_ids': ['items-access'], 'slice_ids': ['items-slice']
        }])
        exchange = json.loads((self.directory/'conversation/choose-layout.json').read_text())
        self.assertNotIn('slice_ids', exchange)

    def test_approved_feature_prerequisite_requires_its_own_slice(self):
        self.start()
        self.exchange()
        self.record_baseline()
        self.record_reviewed_plan(self.plan())
        spec = self.blueprint()
        foundation = copy.deepcopy(spec['features'][0])
        foundation['feature_id'] = 'foundation'
        foundation['implementation_paths'] = ['src/foundation.py']
        foundation['acceptance_checks'][0]['check_id'] = 'foundation-check'
        spec['features'].append(foundation)
        spec['features'][0]['prerequisite_feature_ids'] = ['foundation']
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
        coverage = self.coverage()
        coverage['mappings'].append({'baseline_id':'list-items','feature_id':'foundation',
                                     'acceptance_ids':['foundation-check']})
        self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage))
        self.cli('approve', '--approval-file', self.payload('approval', self.approval()))
        packet = self.slice()
        self.cli('slice-create', '--contract-file', self.payload('missing-prerequisite', packet), ok=False)
        foundation_slice = dict(packet, slice_id='foundation-slice', feature_ids=['foundation'],
                                allowed_paths=['src/foundation.py'], functional_tests=['foundation-check'])
        self.cli('slice-create', '--contract-file', self.payload('foundation', foundation_slice))
        self.cli('slice-create', '--contract-file', self.payload('with-prerequisite',
                 dict(packet, dependencies=['foundation-slice'])))
        self.assertEqual(self.manifest()['slices']['items-slice']['status'], 'blocked')

    def test_greenfield_followup_after_revocation_preserves_the_original_empty_baseline(self):
        self.start()
        self.exchange(requirement_ids=['list-items'])
        audit = {'audit_id':'audit-v1','scope':'feature','baseline_kind':'greenfield','surface':'backend',
                 'inspected_paths':['.'],'features':[],'unresolved':[],
                 'requirements':[{'requirement_id':'list-items','text':'List items','decision_ids':['choose-layout']}],
                 'evidence_paths':[self.artifact('discovery/empty.txt','No application code exists')]}
        self.record_baseline(audit)
        for version, audit_id in ((1, 'audit-v1'), (2, 'audit-v2')):
            plan = self.plan(f'plan-v{version}', audit_id)
            spec = self.blueprint(f'spec-v{version}')
            spec.update(plan_version=plan['version'], baseline_id=audit_id)
            coverage = dict(self.coverage(spec['version']), baseline_id=audit_id)
            self.record_reviewed_plan(plan)
            self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec))
            self.cli('blueprint-coverage', '--coverage-file', self.payload('coverage', coverage))
            self.cli('approve', '--approval-file', self.payload('approval', self.approval(spec['version'])))
            if version == 1:
                (self.root/'src').mkdir()
                (self.root/'src/service.py').write_text('def list_items(): return []\n')
                self.cli('revoke-approval', '--reason', 'Revise the blueprint after implementation.')
                self.record_baseline(dict(audit, audit_id='audit-v2'))
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertTrue(resumed['safe_to_continue'])
