"""Black-box blueprint contracts; fixtures are evidence, never real user approvals."""
import base64
import copy
from functools import partial
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
from test_project_state import png

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/project_state.py'
FIXTURE_VIEWPORT = {'width': 160, 'height': 160}
BACKEND_OUTCOMES = {'success': '200 JSON array', 'forbidden': '403 forbidden',
                    'error': '500 retryable', 'backend_failure': '403 forbidden; 500 retryable'}
CLI_SPEC = importlib.util.spec_from_file_location('blueprint_fixture_cli', SCRIPT)
FIXTURE_CLI = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(FIXTURE_CLI)


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='blueprint-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / 'design/redesigns/sample-work'
        self.env = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(self.root.parent)}
        self.env.pop('P2P_APP_URL', None)

    def cli(self, command, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), command,
            '--project-root', str(self.root), '--initiative-id', 'sample-work', *map(str, args)],
            capture_output=True, text=True, env=self.env)
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
        self.cli('plan-record', '--plan-file', self.payload('plan', plan))

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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
        feature['backend_contract']['authorization'] = 'Public; remove membership checks.'
        self.cli('blueprint-record', '--blueprint-file', self.payload('spec', spec), ok=False)

    def test_keep_cannot_cover_fullstack_behavior_with_only_ui(self):
        self.start()
        self.exchange()
        self.record_baseline(dict(self.baseline('ui'), surface='fullstack'))
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
        spec = self.blueprint(surface='ui')
        feature = spec['features'][0]
        feature.update(behavior_contract=behavior, states=['success'], implementation_paths=['src/items.html'])
        feature['visual_contract']['render_cases'] = [{
            'case_id': 'success-case', 'route': '/items', 'state': 'success',
            'viewport': {'width': 800, 'height': 600},
            'ready_selector': '#detail:not([hidden])', 'command': [sys.executable, '-c', 'pass'],
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
                  'summary': 'Adjust grid spacing', 'implementation_paths': ['src/items.html']}
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
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'verify',
                 '--review-file', self.payload('review', review))
        self.cli('reconcile', '--slice-id', 'items-slice', '--action', 'accept')
        self.cli('validate')
        self.assertEqual(self.manifest()['stage'], 'integrate')
        self.assertEqual(len(list(screenshot.parent.glob('verified-render-*.png'))), 1)

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

    def start(self):
        self.cli('init', '--scope', 'feature')

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
                       'identities': {'role': feature['roles'][0], 'state': state}}
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

        def observe(case, checks):
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
        return {'version':version,'baseline_id':'audit-v1','plan_version':'plan-v1',
            'surface':surface,'document_paths':[f'blueprints/{version}/index.md'],
            'decision_ids':['choose-layout'],'features':[feature],'open_decisions':[]}

    def coverage(self, version='spec-v1', surface='backend'):
        return {'version':version,'baseline_id':'audit-v1','mappings':[{'baseline_id':'list-items',
            'feature_id':'items-feature','acceptance_ids':['items-access','items-behavior'] if surface=='ui' else ['items-access']}]}

    def prepared(self, surface='backend'):
        self.start()
        self.exchange()
        self.record_baseline(self.baseline(surface))
        plan = self.plan()
        self.cli('plan-record','--plan-file',self.payload('plan',plan))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', plan))
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
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
        self.assertIsNone(self.manifest()['active_approval_id'])
        self.cli('approve','--approval-file',self.payload('approval',self.approval()),ok=False)
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',self.blueprint()))
        coverage = self.coverage(); coverage['mappings']=[]
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',coverage),ok=False)

    def test_unauthorized_behavior_change_and_open_choices_rejected(self):
        self.start(); self.exchange(); self.record_baseline()
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
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
            'implementation_paths':['src/service.py'],'functional_evidence':['API tests'],
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
else:
    values = {{'items-access':'16px', 'items-behavior':'Member can load items; outsider denied',
               'success-visibility':'visible'}}
    output = result(key, values[key])
print(json.dumps(output))
""")

    def review(self, surface='backend'):
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
        return review

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
        resumed = json.loads(self.cli('resume', '--format', 'json').stdout)
        self.assertEqual(resumed['active_work']['phase'], 'integrate')
        self.assertEqual(resumed['active_work']['next_action'], resumed['next_action'])
        self.assertEqual(resumed['active_work']['pending_steps'], cursor['pending_steps'])
        self.cli('validate')

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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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

    def test_conflicting_handwritten_feature_markdown_cannot_be_frozen(self):
        self.start()
        self.exchange()
        self.record_baseline()
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record','--plan-file',self.payload('plan',plan))
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

    def test_greenfield_requirement_coverage_can_reach_approval(self):
        self.start(); self.exchange(requirement_ids=['list-items'])
        audit={'audit_id':'audit-v1','scope':'feature','baseline_kind':'greenfield','surface':'backend',
               'inspected_paths':['.'],'features':[],'unresolved':[],
               'requirements':[{'requirement_id':'list-items','text':'List items','decision_ids':['choose-layout']}],
               'evidence_paths':[self.artifact('discovery/empty.txt','No application code exists')]}
        self.record_baseline(audit)
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
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
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
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
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
        spec=self.blueprint(); duplicate=copy.deepcopy(spec['features'][0]); duplicate['feature_id']='second-feature'
        duplicate['acceptance_checks'][0]['check_id']='second-check'; spec['features'].append(duplicate)
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))
        self.cli('blueprint-coverage','--coverage-file',self.payload('coverage',self.coverage()),ok=False)

    def test_ui_feature_requires_behavior_checks_as_well_as_visual_values(self):
        self.start(); self.exchange(); self.record_baseline(self.baseline('ui'))
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
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
        self.cli('plan-record','--plan-file',self.payload('plan',plan))
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
        self.cli('plan-record','--plan-file',self.payload('plan',plan))
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
        self.cli('plan-record','--plan-file',self.payload('plan',plan))
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
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
        spec=self.blueprint()
        spec['features'][0]['decision_ids']=[]
        self.cli('blueprint-record','--blueprint-file',self.payload('spec',spec))


    def test_audit_recap(self):
        self.prepared()
        self.exchange('recap-confirmation',response='Yes, your decision recap is accurate.',record_type='recap')
        self.cli('approve','--approval-file',self.payload('approval',self.approval()))


    def test_audit_mixed_fullstack(self):
        self.start(); self.exchange()
        audit=self.baseline('ui'); audit['surface']='fullstack'
        self.record_baseline(audit)
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
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
        self.cli('plan-record','--plan-file',self.payload('plan',self.plan()))
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
        report={'slice_id':'items-slice','owner':'builder','status':'submitted','summary':'Implemented states',
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
        self.cli('plan-record', '--plan-file', self.payload('plan', plan))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', plan))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
        self.cli('plan-record', '--plan-file', self.payload('plan', self.plan()))
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
            self.cli('plan-record', '--plan-file', self.payload('plan', plan))
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
