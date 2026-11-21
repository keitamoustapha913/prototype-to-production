import json
from pathlib import Path
import tempfile
import unittest

from scripts.living_review import CATALOG, Documents, History, checkpoint


class CorrectionNotesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'selector.md'
        self.source.write_text('Three buttons')
        self.spec = {'id': 'selector', 'source': 'selector.md'}
        (self.root / CATALOG).parent.mkdir()
        self.manifest = {'conversation': {}}
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))
        self.catalog()

    def catalog(self, **values):
        self.spec.update(values)
        (self.root / CATALOG).write_text(json.dumps({'sections': [self.spec]}))

    def save(self):
        with History(self.root) as history:
            return history.sync(Documents(self.root))

    def test_preview_then_document_and_approve_a_correction(self):
        self.save()
        self.source.write_text('One dropdown')
        draft = self.save()
        self.assertTrue(draft['sections']['selector']['pending_note'])
        with self.assertRaisesRegex(ValueError, 'instruction and reason'):
            checkpoint(self.root, self.manifest)
        self.catalog(instruction='Combine the controls', reason='Keep both destinations in one menu')
        first = checkpoint(self.root, self.manifest)
        self.source.write_text('One dropdown with a preview badge')
        with self.assertRaisesRegex(ValueError, 'instruction and reason'):
            checkpoint(self.root, self.manifest)
        self.catalog(instruction='Mark the new choice as a preview')
        second = checkpoint(self.root, self.manifest)
        self.assertNotEqual(first, second)
        self.catalog(reason='Identify the experimental destination')
        self.assertNotEqual(second, checkpoint(self.root, self.manifest))
        self.catalog(reason='  ')
        with self.assertRaisesRegex(ValueError, 'instruction and reason'):
            checkpoint(self.root, self.manifest)

    def test_draft_history_keeps_original_before_through_multiple_saves_and_restart(self):
        with History(self.root) as history:
            initial = history.sync(Documents(self.root))
            self.source.write_text('Draft one')
            draft_one = history.sync(Documents(self.root))
            self.source.write_text('Draft two')
            draft_two = history.sync(Documents(self.root))
            self.catalog(instruction='Combine the controls', reason='Keep both destinations together')
            documented = history.sync(Documents(self.root))
            before = history.previous('selector', documented['revision'])
            self.assertEqual(before['revision'], initial['revision'])
            self.assertEqual(before['section']['blob'], initial['sections']['selector']['blob'])
            self.assertNotEqual(draft_one['revision'], draft_two['revision'])

        with History(self.root) as reopened:
            self.source.write_text('Draft three')
            next_draft = reopened.sync(Documents(self.root))
            before = reopened.previous('selector', next_draft['revision'])
            self.assertEqual(before['revision'], documented['revision'])
            self.assertEqual(before['section']['blob'], draft_two['sections']['selector']['blob'])

    def test_legacy_draft_history_derives_the_original_boundary(self):
        with History(self.root) as history:
            initial = history.sync(Documents(self.root))
            self.source.write_text('Draft one')
            history.sync(Documents(self.root))
            self.source.write_text('Draft two')
            current = history.sync(Documents(self.root))
            # Recreate the full JSON rows used before per-section storage.
            rows = [(revision, history.version(revision)) for revision in range(1, current['revision'] + 1)]
            history.db.execute('DELETE FROM section_edits')
            for revision, state in rows:
                state['selector'].pop('correction_base_revision', None)
                history.db.execute('UPDATE revisions SET state=? WHERE id=?',
                                   (json.dumps(state).encode(), revision))
            before = history.previous('selector', current['revision'])
            self.assertEqual(before['revision'], initial['revision'])

    def test_impact_only_and_removed_sections_keep_a_previous_comparison(self):
        with History(self.root) as history:
            initial = history.sync(Documents(self.root))
            (self.root / CATALOG).write_text(json.dumps({'sections': [
                {'id': 'selector', 'source': 'selector.md', 'features': ['changed']}]}))
            impacted = history.sync(Documents(self.root))
            before = history.previous('selector', impacted['revision'])
            self.assertEqual(before['revision'], initial['revision'])

            (self.root / CATALOG).write_text(json.dumps({'sections': []}))
            removed = history.sync(Documents(self.root))
            before = history.previous('selector', removed['revision'])
            self.assertEqual(before['revision'], impacted['revision'])

    def test_single_save_correction_and_notes_only_change_have_their_own_before(self):
        with History(self.root) as history:
            initial = history.sync(Documents(self.root))
            self.source.write_text('One dropdown')
            self.catalog(instruction='Combine the controls', reason='Keep both destinations together')
            corrected = history.sync(Documents(self.root))
            before = history.previous('selector', corrected['revision'])
            self.assertEqual(before['revision'], initial['revision'])

            self.catalog(instruction='Keep the menu compact', reason='Avoid crowding the page')
            note_update = history.sync(Documents(self.root))
            before = history.previous('selector', note_update['revision'])
            self.assertEqual(before['revision'], corrected['revision'])
            self.assertEqual(before['section']['blob'], corrected['sections']['selector']['blob'])

    def test_independent_consecutive_corrections_start_from_latest_content(self):
        with History(self.root) as history:
            initial = history.sync(Documents(self.root))
            self.source.write_text('One dropdown')
            self.catalog(instruction='Combine the controls', reason='Keep both destinations together')
            first = history.sync(Documents(self.root))
            self.source.write_text('One dropdown with a preview badge')
            self.catalog(instruction='Mark the new choice as a preview', reason='Set user expectations')
            second = history.sync(Documents(self.root))
            before = history.previous('selector', second['revision'])
            self.assertEqual(before['revision'], first['revision'])
            self.assertNotEqual(before['revision'], initial['revision'])
    def test_impact_only_change_invalidates_only_the_changed_consumer(self):
        from scripts.living_review import bindings
        self.catalog(contracts=['api'])
        selected = {}
        for name, role in [('writer', 'provides'), ('reader', 'consumes')]:
            path = f'{name}.json'
            (self.root / path).write_text(json.dumps({'dependencies': [], 'shared_contracts': {'api': {'role': role}}}))
            selected[name] = {'contract_path': path}
        self.manifest['slice_review'] = {'selected': selected}
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))
        before = self.save()
        (self.root / 'reader.json').write_text(json.dumps({'dependencies': ['writer']}))
        after = self.save()
        self.assertGreater(after['revision'], before['revision'])
        self.assertEqual(bindings(before['sections'], 'writer'), bindings(after['sections'], 'writer'))
        self.assertEqual(bindings(before['sections'], ''), bindings(after['sections'], ''))
        receipt = {'visual_review': bindings(before['sections'], 'reader')}
        with self.assertRaisesRegex(ValueError, 'changed'):
            checkpoint(self.root, self.manifest, 'reader', receipt)

    def test_consequential_correction_requires_a_decided_exchange(self):
        self.catalog(instruction='Combine the controls', reason='Keep both choices', consequential=True,
                     decisions=['question'])
        record = {'exchange_id': 'question', 'status': 'open', 'record_type': 'decision'}
        from hashlib import sha256
        data = json.dumps(record).encode()
        (self.root / 'question.json').write_bytes(data)
        self.manifest['conversation']['question'] = {'path':'question.json','sha256':sha256(data).hexdigest(),'sequence':0}
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'actual user answer'):
            checkpoint(self.root, self.manifest)

    def test_explicit_new_correction_can_repeat_the_same_instruction(self):
        self.catalog(instruction='Match the reference', reason='Keep the controls aligned')
        self.save()
        self.source.write_text('Move control one grid unit')
        self.save()
        self.catalog(correction_id='align-again')
        self.assertIn('selector', checkpoint(self.root, self.manifest))
        recorded = self.save()
        self.assertNotIn('pending_note', recorded['sections']['selector'])
        self.assertEqual(recorded['revision'], self.save()['revision'])
