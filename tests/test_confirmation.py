"""Natural replies can authorize only their displayed action."""
import unittest

from scripts import confirmation


class ConfirmationTests(unittest.TestCase):
    def test_plan_handoff_needs_a_scoped_prompt_for_a_relative_reply(self):
        self.assertTrue(confirmation.accepts(
            "plan_handoff", "Yes, continue.", "Would you like me to proceed to the detailed blueprint?"))
        self.assertFalse(confirmation.accepts("plan_handoff", "Yes, continue.", "Is the plan clear?"))
        self.assertFalse(confirmation.accepts("plan_handoff", "Continue", "Proceed to the detailed blueprint?"))

    def test_acknowledgments_corrections_and_other_actions_do_not_approve(self):
        prompt = "Do you approve this blueprint?"
        self.assertTrue(confirmation.accepts("blueprint_approval", "I approve this blueprint.", prompt))
        self.assertTrue(confirmation.accepts(
            "blueprint_approval", "I approve version spec-v1.", prompt,
            explicit=confirmation.blueprint_replies("spec-v1")))
        self.assertFalse(confirmation.accepts(
            "blueprint_approval", "I approve version spec-v10.", prompt,
            explicit=confirmation.blueprint_replies("spec-v1")))
        for reply in ("Okay, I understand", "That looks correct", "Approve this blueprint, but change permissions",
                      "Start implementation", "Yes, continue", "Would you say I approve this blueprint?", "Yes, if tests pass"):
            with self.subTest(reply=reply):
                self.assertFalse(confirmation.accepts("blueprint_approval", reply, prompt))

    def test_start_requires_its_own_explicit_instruction(self):
        self.assertTrue(confirmation.accepts(
            "start", "Start implementation.", "May I start implementation of this locked package?"))
        self.assertFalse(confirmation.accepts("start", "Start implementation", "Do you approve this package lock?"))
        self.assertFalse(confirmation.accepts("start", "Yes", "May I start implementation of this locked package?"))
