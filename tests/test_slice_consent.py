import json
import unittest
from unittest.mock import patch

from scripts.slice_consent import affirmative


class SliceConsentTests(unittest.TestCase):
    def test_scoped_map_preview_supplies_every_required_value_and_records_no_consent(self):
        from test_blueprint_state import BlueprintTests

        fixture = BlueprintTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        feature = fixture.reviewed_blueprint()
        fixture.enable_slice_map()
        candidate = fixture.guide_candidate(feature) | {
            'outcome': 'Members can list items while outsiders are refused.',
            'boundary_reason': 'Keep the existing guard and persistence together.',
        }
        target = fixture.review_cli('slice-plan', {'slices': [candidate]})['slice_map']['target']
        fixture.cli('slice-map')
        session = fixture.enable_contextual_confirmation()
        manifest = fixture.directory / 'manifest.json'
        before = manifest.read_bytes()
        args = ('confirmation-preview', '--slice-action', 'confirm_map')
        preview = json.loads(fixture.cli(*args).stdout)
        self.assertEqual(preview['target'], target)
        self.assertEqual(preview['display'].splitlines()[-1], 'Do you confirm this slice map?')
        for value in target.values():
            self.assertIn(value, preview['question'])
        session.prompt(preview['display'])
        session.reply('I confirm this slice map.')
        reply = json.loads(fixture.cli(*args, '--check-reply').stdout)
        self.assertEqual(reply['status'], 'confirmation_ready')
        self.assertEqual(manifest.read_bytes(), before)
        exchange = {
            'exchange_id': 'preview-map-consent', 'phase': 'slice_review', 'record_type': 'slice_consent',
            'status': 'decided', 'consent_action': 'confirm_map', 'target': target,
            'question': preview['question'], 'response': reply['reply'],
        }
        fixture.cli('conversation-record', '--exchange-file', fixture.payload('preview-consent', exchange))
        register = fixture.review_cli('slice-register')
        self.assertEqual(register['current_slice_id'], 'items-slice')
        self.assertFalse(register['implementation_authorized'])

    def test_authorizing_a_locked_package_rechecks_its_authored_guides(self):
        from test_blueprint_state import BlueprintTests, FIXTURE_CLI

        fixture = BlueprintTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        feature = fixture.reviewed_blueprint()
        fixture.review_cli('slice-plan', {'slices': [fixture.guide_candidate(feature)]})
        fixture.review_cli('slice-review-record', fixture.review_input())
        package = fixture.lock_reviewed_package()
        consent = fixture.with_slice_consent('implementation-authorize', package | {
            'instruction': 'start implementation', 'authorized_by_user': True,
            'user_response': 'Start implementation of package-v1.'})
        state = FIXTURE_CLI.slice_review_state(fixture.directory, fixture.manifest())

        with patch.object(state, 'validate_guide', side_effect=FIXTURE_CLI.StateError('guide needs correction')):
            with self.assertRaisesRegex(FIXTURE_CLI.StateError, 'guide needs correction'):
                state.authorize(consent)
        self.assertNotIn('authorization_path', state.registry['packages']['package-v1'])

    def test_conversational_consent_survives_release_and_validation(self):
        from test_blueprint_state import BlueprintTests

        fixture = BlueprintTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        feature = fixture.reviewed_blueprint()
        fixture.review_cli('slice-plan', {'slices': [fixture.guide_candidate(feature)]})
        review = fixture.review_input() | {'user_response': 'I approve revision 1 of items-slice.'}
        fixture.review_cli('slice-review-record', review)
        package = fixture.lock_reviewed_package()
        authorization = package | {'instruction': 'start implementation', 'authorized_by_user': True,
                                  'user_response': 'Please start implementation of package-v1 now.'}
        fixture.review_cli('implementation-authorize', authorization)
        fixture.review_cli('slice-release', package | {'slice_id': 'items-slice'})
        validation = fixture.cli('validate', '--slice-id', 'items-slice')
        self.assertIn('Exact package authorization and released assignment verified', validation.stdout)

    def test_accepts_unambiguous_exact_scope_in_conversation(self):
        slices = {'slices': [{'slice_id': 'items-slice', 'proposal_revision': 1}]}
        package = {'package_id': 'package-v1'}
        for action, target, response in (
            ('approve', slices, 'I approve revision 1 of items-slice.'),
            ('approve', slices, 'Yes, I approve items-slice revision 1.'),
            ('delegate', slices, 'Please automatically validate revision 1 of items-slice.'),
            ('lock', package, 'Please lock package-v1 now.'),
            ('start', package, 'Please start implementation of package-v1 now.'),
            ('start', package, 'Go ahead and implement package-v1.'),
        ):
            with self.subTest(action=action, response=response):
                self.assertTrue(affirmative(action, target, response))

    def test_rejects_wrong_scope_conditions_and_ambiguous_intent(self):
        package = {'package_id': 'package-v1'}
        for response in ('Do not start implementation of package-v1.',
                         'Please start implementation of package-v10 now.',
                         'Start implementation of package-v1 if my team approves.',
                         'Can you start implementation of package-v1?', 'I will implement package-v1.',
                         'I implement package-v1.', 'We implement package-v1.',
                         'I approve package-v1.', 'Yes.',
                         'Start implementation of package-v1! Actually, wait.'):
            with self.subTest(response=response):
                self.assertFalse(affirmative('start', package, response))
