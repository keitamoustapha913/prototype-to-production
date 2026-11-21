import json
from pathlib import Path
import tempfile
import unittest

from scripts.living_review import CATALOG, Documents, History, bindings, checkpoint
from scripts.review_watcher import FileChanges
import test_blueprint_state as fixtures


class LivingReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manifest = {'slice_review': {'selected': {}}}
        for key, dependencies in [('provider', []), ('consumer', ['provider']), ('other', [])]:
            path = f'{key}.json'
            self.write(path, {'dependencies': dependencies, 'feature_ids': [key], 'allowed_paths': [f'src/{key}/**']})
            self.manifest['slice_review']['selected'][key] = {'contract_path': path}
        self.write('manifest.json', self.manifest)
        self.write('guide.md', '# Guide\n## Menu\nButtons\n## Route\nGET\n')
        self.write(CATALOG, {'sections': [
            {'id': 'menu', 'slice': 'provider', 'source': 'guide.md', 'heading': 'Menu'},
            {'id': 'route', 'slice': 'other', 'source': 'guide.md', 'heading': 'Route'}]})

    def write(self, path, value):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value if isinstance(value, str) else json.dumps(value))

    def save(self):
        with History(self.root) as history:
            return history.sync(Documents(self.root))

    def test_slice_map_is_visible_without_entering_blueprint_approval_bindings(self):
        before = self.save()['visual_review']
        path = 'slice-review/maps/slice-map-v1.md'
        guide = 'slice-proposals/provider/revisions/1/implementation-guide.md'
        self.write(path, '# Implementation slice map\n\nProvider → consumer\n')
        self.write(guide, '# Provider guide')
        self.write('slice-review/register.md', '# Register')
        self.manifest['slice_review']['selected']['provider']['guide_path'] = guide
        self.manifest['active_approval_id'] = 'blueprint-v1'
        self.manifest['slice_review'].update(approval_id='blueprint-v1', slice_map={
            'target': {'map_path': path},
            'documents': {'provider': {'guide_path': guide, 'contract_path': 'provider.json'}},
        })
        self.write('manifest.json', self.manifest)
        proposal = self.save()
        self.assertEqual(proposal['sections']['implementation-slice-map']['source'], path)
        self.assertTrue({guide, 'provider.json', 'slice-review/register.md'} <=
                        {section['source'] for section in proposal['sections'].values()})
        self.assertEqual(proposal['visual_review'], before)
        self.manifest['slice_review']['slice_map']['consent_id'] = 'confirm-map'
        self.write('manifest.json', self.manifest)
        confirmed = self.save()
        self.assertIn('confirmed', confirmed['sections']['implementation-slice-map']['title'])
        self.assertEqual(confirmed['visual_review'], before)
        self.manifest['active_approval_id'] = 'blueprint-v2'
        self.write('manifest.json', self.manifest)
        next_blueprint = self.save()
        self.assertNotIn('implementation-slice-map', next_blueprint['sections'])
        self.assertFalse(next_blueprint['removals'])

    def test_shared_section_keeps_each_slices_own_approval_history(self):
        first = self.save()['visual_review']
        owner_path = 'slice-proposals/provider/reviews/owner.json'
        self.write(owner_path, {'slice_id': 'provider', 'approved_at': '2026-11-19',
                                'visual_review': first['provider']})
        self.write('guide.md', '# Guide\n## Menu\nOne dropdown\n## Route\nGET\n')
        second = self.save()['visual_review']
        self.manifest['active_approval_id'] = 'blueprint-v1'
        self.manifest['slice_review'].update(
            approval_id='blueprint-v1',
            artifact_hashes={owner_path: 'saved'},
            reviews={'consumer': {'slice_id': 'consumer', 'approved_at': '2026-11-20',
                                  'visual_review': second['consumer']}})
        self.write('manifest.json', self.manifest)

        state = self.save()

        self.assertEqual(state['approved_by_scope']['provider']['menu'], first['provider']['menu'])
        self.assertEqual(state['approved_by_scope']['consumer']['menu'], second['consumer']['menu'])
        self.assertNotIn('menu', state['current_by_scope'].get('provider', {}))
        self.assertEqual(state['current_by_scope']['consumer']['menu'], second['consumer']['menu'])
        self.assertEqual(state['slice_status']['consumer'], 'Approved')

        self.manifest['active_approval_id'] = 'blueprint-v2'
        self.write('manifest.json', self.manifest)
        state = self.save()

        self.assertEqual(state['approved_by_scope']['consumer']['menu'], second['consumer']['menu'])
        self.assertNotIn('consumer', state['current_by_scope'])
        self.assertEqual(state['slice_status']['consumer'], 'Awaiting review')

    def test_edit_changes_one_section_and_invalidates_only_affected_reviews(self):
        docs = Documents(self.root)
        with History(self.root) as history:
            before = history.sync(docs)
            self.write('guide.md', '# Guide\n## Menu\nOne dropdown\n## Route\nGET\n')
            after = history.sync(docs)
            self.assertEqual(history.version(before['revision']), before['sections'])
            self.assertEqual(after['revision'], history.sync(docs)['revision'])
        self.assertEqual(before['sections']['route'], after['sections']['route'])
        self.assertEqual(after['sections']['menu']['impact']['consumer']['kind'], 'Compatibility')
        for key in ['provider','consumer','other']:
            receipt = {'visual_review': bindings(before['sections'], key)}
            if key == 'other':
                checkpoint(self.root, self.manifest, key, receipt)
            else:
                with self.assertRaisesRegex(ValueError, 'changed'):
                    checkpoint(self.root, self.manifest, key, receipt)
        with History(self.root) as reopened:
            self.assertEqual(reopened.version(1)['menu']['blob'], before['sections']['menu']['blob'])

    def test_500_section_refresh_evaluates_and_serializes_only_the_delta(self):
        sections = []
        for index in range(500):
            source = f'sources/{index}.md'
            self.write(source, f'# Section {index}\nInitial text {index}\n')
            sections.append({'id': f'section-{index}', 'source': source})
        self.write(CATALOG, {'sections': sections})
        docs = Documents(self.root)
        calls = {'stamp': 0, 'content': 0}
        original_stamp, original_content = docs.stamp, docs.content
        def count_stamp(path):
            calls['stamp'] += 1
            return original_stamp(path)
        def count_content(spec):
            calls['content'] += 1
            return original_content(spec)
        docs.stamp, docs.content = count_stamp, count_content
        with History(self.root) as history:
            before = history.sync(docs)
            old_etag = history.etag
            full_before_size = len(history.payload)
            calls.update(stamp=0, content=0)

            self.write('sources/237.md', '# Section 237\nChanged text 237\n')
            after = history.sync(docs, dirty_paths={'sources/237.md'})
            self.assertEqual(after['revision'], before['revision'] + 1)
            self.assertEqual(docs.evaluated_ids, {'section-237'})
            edits = history.db.execute('SELECT section_id FROM section_edits WHERE revision=?',
                                       (after['revision'],)).fetchall()
            self.assertEqual(edits, [('section-237',)])
            self.assertEqual(history.version(after['revision']), after['sections'])
            self.assertEqual(calls['stamp'], 1)
            self.assertEqual(calls['content'], 2)
            self.assertEqual(history.version(before['revision']), before['sections'])

            delta = history.delta(old_etag)
            self.assertIsNone(history._payload)  # Delta generation never encodes the full response.
            full_after = history.payload
            patch = json.loads(delta)
            self.assertEqual(set(patch['maps']['sections']['children']), {'section-237'})
            self.assertEqual(patch['maps']['sections']['delete'], [])
            self.assertIn(b'section-237', delta)
            self.assertLess(len(delta), full_before_size // 10)
            self.assertGreater(len(full_after), len(delta) * 10)

    def test_file_watcher_tracks_missing_inputs_and_new_directories(self):
        watcher = FileChanges(self.root)
        self.addCleanup(watcher.close)
        if watcher.fd is None:
            self.skipTest('inotify is unavailable')
        watcher.refresh(['new/input.md'])
        watcher.changed()  # Drain setup events before observing changes.
        watcher.refresh(['new/input.md'])
        self.assertFalse(watcher.changed())
        (self.root / 'new').mkdir()
        self.assertTrue(watcher.changed())
        watcher.refresh(['new/input.md'])
        watcher.changed()
        (self.root / 'new' / 'input.md').write_text('created')
        self.assertTrue(watcher.changed())

    def test_file_watcher_reports_just_the_saved_inputs(self):
        watcher = FileChanges(self.root)
        self.addCleanup(watcher.close)
        if watcher.fd is None:
            self.skipTest('inotify is unavailable')
        watcher.refresh(['guide.md', 'provider.json', 'unrelated.json'])
        self.assertIsNone(watcher.changed_paths())
        self.write('provider.json', {'updated': True})
        self.write('unregistered.md', 'Scratch note')
        self.assertEqual(watcher.changed_paths(), {'provider.json'})
        self.assertEqual(watcher.changed_paths(), set())

    def test_file_watcher_tracks_symlink_target_directory(self):
        actual = self.root / 'actual'
        actual.mkdir()
        source = actual / 'input.md'
        source.write_text('before')
        (self.root / 'alias').symlink_to(actual, target_is_directory=True)
        watcher = FileChanges(self.root)
        self.addCleanup(watcher.close)
        if watcher.fd is None:
            self.skipTest('inotify is unavailable')
        watcher.refresh(['alias/input.md'])
        watcher.changed()
        source.write_text('after')
        self.assertTrue(watcher.changed())

    def test_file_watcher_falls_back_after_watch_failure(self):
        watcher = FileChanges(self.root)
        self.addCleanup(watcher.close)
        if watcher.fd is None:
            watcher.refresh(['missing/input.md'])
            self.assertTrue(watcher.changed())
            watcher.refresh(['another/missing/input.md'])
            self.assertTrue(watcher.changed())
            return

        class Unwatchable(FileChanges):
            def add_watch(self, directory, mask):
                return -1

        watcher = Unwatchable(self.root)
        self.addCleanup(watcher.close)
        watcher.refresh(['missing/input.md'])
        self.assertTrue(watcher.watch_failed)
        self.assertTrue(watcher.changed())
        self.assertTrue(watcher.changed())
        watcher.refresh(['another/missing/input.md'])
        self.assertTrue(watcher.changed())

    def test_reference_image_reaches_its_slice_and_dependents(self):
        self.write('screen.svg', '<svg/>')
        self.write(CATALOG, {'sections': [
            {'id': 'layout', 'source': 'guide.md', 'reference': 'screen.svg'}
        ]})
        self.write('provider.json', {'dependencies': [], 'asset_hashes': {'screen.svg': 'original'}})
        docs = Documents(self.root)
        before = docs.sections(self.manifest)
        self.assertEqual(before['layout']['impact'], {
            'provider': {'kind': 'Direct', 'reasons': ['references screen.svg']},
            'consumer': {'kind': 'Compatibility', 'reasons': ['depends on provider']},
        })
        self.write('screen.svg', '<svg><circle r="5"/></svg>')
        for scope in ['provider', 'consumer']:
            with self.assertRaisesRegex(ValueError, 'changed'):
                checkpoint(self.root, self.manifest, scope, {'visual_review': bindings(before, scope)})
        checkpoint(self.root, self.manifest, 'other', {'visual_review': {}})

    def test_deleted_last_section_keeps_preview_and_note_bound_to_receipt(self):
        self.write(CATALOG, {'sections': [
            {'id': 'last', 'title': 'Last section', 'source': 'guide.md', 'heading': 'Menu'}]})
        with History(self.root) as history:
            before = history.sync(Documents(self.root))
        self.write(CATALOG, {'sections': []})
        with History(self.root) as history:
            pending = history.sync(Documents(self.root))
        self.assertTrue(pending['removals']['last']['note_pending'])
        self.assertEqual(pending['sections'], {})
        with History(self.root) as history:
            self.assertEqual(history.version(before['revision'])['last']['blob'], before['sections']['last']['blob'])
            archived = history.db.execute('SELECT data FROM blobs WHERE id=?',
                                          (before['sections']['last']['blob'],)).fetchone()[0]
        self.assertEqual(archived, b'## Menu\nButtons\n')
        with self.assertRaisesRegex(ValueError, 'removal instruction and reason'):
            checkpoint(self.root, self.manifest)

        self.write(CATALOG, {'sections': [], 'removals': [{
            'id': 'last', 'instruction': 'Remove the retired menu preview',
            'reason': 'The approved menu no longer contains this destination.'}]})
        receipt_map = checkpoint(self.root, self.manifest)
        self.assertIn('removed:last', receipt_map)
        receipt = {'visual_review': receipt_map}
        # The catalog copy may disappear because the durable record is now authoritative.
        self.write(CATALOG, {'sections': []})
        self.assertEqual(checkpoint(self.root, self.manifest, receipt=receipt), receipt_map)
        with History(self.root) as history:
            saved = history.sync(Documents(self.root))
        self.assertEqual(saved['removals']['last']['reason'], 'The approved menu no longer contains this destination.')

        self.write(CATALOG, {'sections': [], 'removals': [{
            'id': 'last', 'instruction': 'Remove the retired menu preview',
            'reason': 'The old destination is obsolete.'}]})
        changed = checkpoint(self.root, self.manifest)
        self.assertNotEqual(changed['removed:last'], receipt_map['removed:last'])
        with History(self.root) as history:
            revised = history.sync(Documents(self.root))
        self.assertGreater(revised['revision'], saved['revision'])
        self.assertEqual(revised['removals']['last']['updated_revision'], revised['revision'])
        self.assertEqual(revised['removals']['last']['note_history'][0]['reason'],
                         'The approved menu no longer contains this destination.')
        with self.assertRaisesRegex(ValueError, 'illustrations changed'):
            checkpoint(self.root, self.manifest, receipt=receipt)

    def test_deleted_section_retains_its_old_slice_scope(self):
        self.write(CATALOG, {'sections': [
            {'id': 'menu', 'source': 'guide.md', 'heading': 'Menu', 'slice': 'provider'}]})
        with History(self.root) as history:
            old = history.sync(Documents(self.root))
        self.write(CATALOG, {'sections': [], 'removals': [{
            'id': 'menu', 'instruction': 'Remove menu preview', 'reason': 'The section was retired.'}]})
        owner_receipt = checkpoint(self.root, self.manifest, 'provider')
        self.assertIn('removed:menu', owner_receipt)
        self.assertNotIn('removed:menu', checkpoint(self.root, self.manifest, 'other'))
        self.assertNotIn('removed:menu', checkpoint(self.root, self.manifest, ''))
        self.assertIn('provider', old['sections']['menu']['impact'])

    def test_redeleting_same_id_preserves_prior_note_without_reusing_it(self):
        self.write(CATALOG, {'sections': [{'id': 'menu', 'source': 'guide.md', 'heading': 'Menu'}]})
        self.save()
        self.write(CATALOG, {'sections': [], 'removals': [{
            'id': 'menu', 'instruction': 'Remove menu', 'reason': 'First removal.'}]})
        self.save()
        self.write(CATALOG, {'sections': [{'id': 'menu', 'source': 'guide.md', 'heading': 'Menu'}]})
        self.save()
        self.write(CATALOG, {'sections': []})
        with History(self.root) as history:
            restored = history.sync(Documents(self.root))
        self.assertTrue(restored['removals']['menu']['active'])
        self.assertTrue(restored['removals']['menu']['note_pending'])
        self.assertEqual(len(restored['removals']['menu']['history']), 1)
        self.assertEqual(restored['removals']['menu']['history'][0]['reason'], 'First removal.')

    def test_real_blueprint_approval_requires_displayed_revision(self):
        fixture = fixtures.BlueprintTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepared(review=True)
        path = fixture.directory / CATALOG
        path.parent.mkdir()
        source = fixture.directory / 'inputs' / 'preview.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        path.write_text(json.dumps({'sections':[{'id':'control','source':'inputs/preview.svg'}]}))
        visual = json.loads(fixture.cli('review-revision').stdout)['visual_review']['']
        approval = fixture.approval()
        blocked = fixture.cli('approve','--approval-file',fixture.payload('approval', approval),ok=False)
        self.assertIn('visual_review', blocked.stderr)
        approval['visual_review'] = visual
        fixture.cli('approve','--approval-file',fixture.payload('approval',approval))
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>')
        blocked = fixture.cli('validate', ok=False)
        self.assertIn('illustrations changed', blocked.stdout + blocked.stderr)

    def test_real_blueprint_approval_records_a_deleted_last_section(self):
        fixture = fixtures.BlueprintTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepared(review=True)
        path = fixture.directory / CATALOG
        path.parent.mkdir()
        source = fixture.directory / 'inputs' / 'preview.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        path.write_text(json.dumps({'sections': [{'id': 'last', 'source': 'inputs/preview.svg'}]}))
        fixture.cli('review-revision')
        path.write_text(json.dumps({'sections': []}))
        draft = json.loads(fixture.cli('review-revision').stdout)
        approval = fixture.approval()
        approval['visual_review'] = draft['visual_review']['']
        blocked = fixture.cli('approve', '--approval-file', fixture.payload('approval', approval), ok=False)
        self.assertIn('removal instruction and reason', blocked.stderr)

        path.write_text(json.dumps({'sections': [], 'removals': [{
            'id': 'last', 'instruction': 'Remove the retired preview',
            'reason': 'The section no longer appears in the approved blueprint.',
            'consequential': True}]}))
        reviewed = json.loads(fixture.cli('review-revision').stdout)
        approval['visual_review'] = reviewed['visual_review']['']
        blocked = fixture.cli('approve', '--approval-file', fixture.payload('approval', approval), ok=False)
        self.assertIn('consequential removal decision', blocked.stderr)

        path.write_text(json.dumps({'sections': [], 'removals': [{
            'id': 'last', 'instruction': 'Remove the retired preview',
            'reason': 'The section no longer appears in the approved blueprint.',
            'consequential': True, 'decisions': ['choose-layout']}]}))
        reviewed = json.loads(fixture.cli('review-revision').stdout)
        approval['visual_review'] = reviewed['visual_review']['']
        fixture.cli('approve', '--approval-file', fixture.payload('approval', approval))
        self.assertIn('removed:last', approval['visual_review'])

    def test_living_review_validates_slice_details_against_the_section_owner(self):
        fixture = fixtures.BlueprintTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        feature = fixture.reviewed_blueprint(multiple=True)
        owner = fixture.guide_candidate(feature) | {'dependency_contracts': []}
        consumer = fixture.guide_candidate(feature, 'adapter-slice', path='src/adapter.py') | {
            'dependency_contracts': []}
        fixture.review_cli('slice-plan', {'slices': [owner, consumer]})
        question = 'Does the membership guard run before reading storage?'
        fixture.exchange('guard-question', status='open', phase='slice_review', question=question,
                         feature_ids=['items-feature'])
        fixture.exchange('guard-answer', phase='slice_review', question=question,
                         responds_to='guard-question', response='Yes, keep the guard before the storage call.',
                         feature_ids=['items-feature'], record_type='slice_detail', slice_id='items-slice',
                         proposal_revision=1, clarifies_decision_ids=['choose-layout'])
        catalog = fixture.directory / CATALOG
        catalog.parent.mkdir()
        source = fixture.directory / 'inputs' / 'guard.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        section = {'id': 'guard', 'source': 'inputs/guard.svg', 'features': ['items-feature'],
                   'instruction': 'Keep the guard first', 'reason': 'Preserve the previous guard order.',
                   'consequential': True, 'decisions': ['guard-answer']}
        manifest = fixture.manifest()

        for scope in ('', 'adapter-slice'):
            with self.subTest(unauthorized_owner=scope):
                catalog.write_text(json.dumps({'sections': [section | {'slice': scope}]}))
                with self.assertRaisesRegex(ValueError, 'decision'):
                    checkpoint(fixture.directory, manifest, scope)
        catalog.write_text(json.dumps({'sections': [section | {
            'slice': 'items-slice', 'reason': 'The items slice owns the guard order.'}]}))
        for scope in ('items-slice', 'adapter-slice'):
            with self.subTest(section_review=scope):
                self.assertIn('guard', checkpoint(fixture.directory, manifest, scope))

        removal = {'id': 'guard', 'instruction': 'Remove the duplicate guard illustration',
                   'reason': 'The slice guide already illustrates the same order.',
                   'consequential': True, 'decisions': ['guard-answer']}
        catalog.write_text(json.dumps({'sections': [], 'removals': [removal]}))
        for scope in ('items-slice', 'adapter-slice'):
            with self.subTest(removal_review=scope):
                self.assertIn('removed:guard', checkpoint(fixture.directory, manifest, scope))

        fixture.exchange('replacement-layout', phase='slice_review', feature_ids=['list-items'],
                         slice_id='items-slice', supersedes='choose-layout',
                         response='Use the newly approved guard sequence.')
        with self.assertRaisesRegex(ValueError, 'decision'):
            checkpoint(fixture.directory, fixture.manifest(), 'items-slice')

    def test_real_slice_review_gate_rejects_deletion_after_visual_consent(self):
        fixture = fixtures.BlueprintTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        feature = fixture.reviewed_blueprint()
        catalog = fixture.directory / CATALOG
        catalog.parent.mkdir()
        source = fixture.directory / 'inputs' / 'preview.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        catalog.write_text(json.dumps({'sections': [{
            'id': 'slice-preview', 'source': 'inputs/preview.svg', 'slice': 'items-slice'}]}))
        fixture.review_cli('slice-plan', {'slices': [fixture.guide_candidate(feature)]})
        shown = json.loads(fixture.cli('review-revision').stdout)
        old_visual = shown['visual_review']['items-slice']
        decision = fixture.review_input()
        decision['visual_review'] = old_visual
        target = {'slices': [{name: decision[name] for name in
                             ('slice_id', 'proposal_revision', 'guide_sha256', 'contract_sha256')}]}
        target['slices'][0]['visual_sha256'] = shown['consent_hashes']['items-slice']
        consent_id = 'slice-removal-consent'
        fixture.exchange(consent_id, phase='slice_review', record_type='slice_consent',
                         consent_action='approve', question=f'Approve {json.dumps(target)}?',
                         response=decision['user_response'], target=target)
        decision['consent_id'] = consent_id

        catalog.write_text(json.dumps({'sections': []}))
        after_removal = json.loads(fixture.cli('review-revision').stdout)
        target['slices'][0]['visual_sha256'] = after_removal['consent_hashes']['items-slice']
        consent_id = 'slice-removal-consent-updated'
        fixture.exchange(consent_id, phase='slice_review', record_type='slice_consent',
                         consent_action='approve', question=f'Approve {json.dumps(target)}?',
                         response=decision['user_response'], target=target)
        decision['consent_id'] = consent_id
        blocked = fixture.review_cli('slice-review-record', decision, ok=False)
        self.assertIn('removal instruction and reason', blocked.stderr)

    def test_material_correction_blocks_authorization_of_previously_locked_slice(self):
        f = fixtures.BlueprintTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        feature = f.reviewed_blueprint()
        catalog = f.directory / CATALOG
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({'sections': [
            {'id':'schema', 'slice':'items-slice', 'source':'inputs/response.json'}]}))
        source = f.directory / 'inputs/response.json'
        source.write_text('{"items":[]}')
        f.review_cli('slice-plan', {'slices': [f.guide_candidate(feature)]})
        view = json.loads(f.cli('review-revision').stdout)
        key = 'items-slice'
        decision = f.review_input()
        target = {'slices': [{name: decision[name] for name in
                             ('slice_id','proposal_revision','guide_sha256','contract_sha256')}]}
        target['slices'][0]['visual_sha256'] = view['consent_hashes'][key]
        f.exchange('visual-consent', phase='slice_review', record_type='slice_consent', consent_action='approve',
                   question=f'Approve {json.dumps(target)}?', response=decision['user_response'], target=target)
        f.review_cli('slice-review-record', decision | {'consent_id':'visual-consent',
                     'visual_review':view['visual_review'][key]})
        package = f.lock_reviewed_package()
        source.write_text('{"items":{},"page":1}')
        changed = json.loads(f.cli('review-revision').stdout)
        self.assertEqual(changed['approved']['schema'],view['sections']['schema']['stamp'])
        self.assertNotEqual(changed['sections']['schema']['stamp'],changed['approved']['schema'])
        start = package | {'instruction':'start implementation','authorized_by_user':True,
                           'user_response':'Start implementation of package-v1'}
        start = f.with_slice_consent('implementation-authorize',start)
        blocked = f.review_cli('implementation-authorize',start,ok=False)
        self.assertIn('illustrations changed', blocked.stderr)
