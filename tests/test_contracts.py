"""Contract tests for the reusable, project-independent redesign packets."""

import importlib.util
import json
from pathlib import Path
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]


def load_state_module():
    module_path = SKILL_ROOT / "scripts" / "project_state.py"
    spec = importlib.util.spec_from_file_location("project_state", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContractTests(unittest.TestCase):
    def test_distributed_instructions_do_not_reference_the_pilot_application(self):
        """A pilot-specific example must not bias future app discovery."""
        instruction_paths = [SKILL_ROOT / "SKILL.md"]
        instruction_paths.extend((SKILL_ROOT / "references").glob("*.md"))
        instruction_paths.extend((SKILL_ROOT / "assets/project").glob("*.md"))
        forbidden = ("qworks", "vda", "audit collaboration", "shared overview", "ccoverview")
        for path in instruction_paths:
            source = path.read_text(encoding="utf-8").lower()
            for phrase in forbidden:
                with self.subTest(path=path.relative_to(SKILL_ROOT), phrase=phrase):
                    self.assertNotIn(phrase, source)

    def test_skill_does_not_impose_pilot_design_style(self):
        overview = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
        workflow = (SKILL_ROOT / "references/workflow.md").read_text(encoding="utf-8").lower()
        browser_qa = (SKILL_ROOT / "references/visual-qa.md").read_text(encoding="utf-8").lower()
        self.assertIn("design direction comes from the current user", overview)
        self.assertNotIn("simpler, cleaner or more ergonomic ui", overview)
        self.assertNotIn("simple, ergonomic, precise, friendly", overview)
        self.assertNotIn("compact **but legible** density", overview)
        self.assertNotIn("do not use placeholder giant cards", workflow)
        self.assertNotIn("audit selection", browser_qa)

    def test_manifest_has_durable_state_fields(self):
        manifest = json.loads((SKILL_ROOT / "assets/project/manifest.template.json").read_text())
        self.assertTrue({
            "schema_version", "initiative_id", "scope", "stage", "approved_version",
            "references", "slices", "warnings", "next_action", "last_checkpoint", "provenance",
        }.issubset(manifest))
        self.assertIn(manifest["scope"], ("feature", "page", "app"))
        self.assertIsInstance(manifest["slices"], dict)

    def test_slice_template_is_self_contained(self):
        packet = json.loads((SKILL_ROOT / "assets/project/slice.template.json").read_text())
        self.assertTrue({
            "slice_id", "owner", "status", "allowed_paths", "dependencies",
            "current_behavior", "prototype_version", "visual_cases", "acceptance_checks",
            "evidence", "blockers", "routes", "prototype_source_paths",
            "design_tokens", "prohibited_deviations", "functional_tests",
        }.issubset(packet))
        self.assertEqual(packet["status"], "draft")

    def test_approval_template_requires_real_user_approval_and_replay_proof(self):
        path = SKILL_ROOT / "assets/project/approval.template.json"
        self.assertTrue(path.is_file(), "A copyable approval contract must exist")
        approval = json.loads(path.read_text())
        self.assertIs(approval["approved_by_user"], False)
        self.assertEqual(approval["mode"], "source")
        self.assertTrue(approval["visual_cases"])
        self.assertIn("script", approval["replay"])
        self.assertIn("evidence_file", approval["replay"])
        self.assertIs(approval["replay"]["qa_controls_excluded"], False)

    def test_replay_report_template_cannot_be_falsely_approved_by_default(self):
        path = SKILL_ROOT / "assets/project/replay.template.json"
        self.assertTrue(path.is_file(), "A browser replay report contract must exist")
        report = json.loads(path.read_text())
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["clean_context_runs"], 0)
        self.assertIs(report["cases"][0]["fixture_replayed"], False)

    def test_invalid_scope_and_empty_ready_visual_cases_are_rejected(self):
        state = load_state_module()
        packet = json.loads((SKILL_ROOT / "assets/project/slice.template.json").read_text())
        self.assertTrue(state.validate_scope("unknown"))
        packet["status"] = "ready"
        packet["visual_cases"] = []
        self.assertTrue(any("visual" in error.lower() for error in state.validate_slice_contract(packet)))

    def test_visual_browser_template_requires_locked_files_and_state_setup(self):
        source = (SKILL_ROOT / "assets/project/redesign.visual.spec.ts").read_text(encoding="utf-8")
        self.assertIn("approved_artifact_hashes", source)
        self.assertIn("visual_cases", source)
        self.assertIn("prepareCase", source)
        self.assertIn("BLOCKED", source)
        self.assertIn("page.screenshot", source)
        self.assertIn("createImageBitmap", source)
        self.assertIn("differentPixels", source)
        self.assertNotIn("actual.equals(reference)", source)
        self.assertIn("reference", source)
        self.assertIn("approved.route !== visualCase.route", source)
        self.assertIn("approved.reference_file !== visualCase.reference_file", source)
        self.assertNotIn("--update-snapshots", source)

    def test_comparison_matrix_and_three_retry_warning_policy_are_documented(self):
        source = (SKILL_ROOT / "references/visual-qa.md").read_text(encoding="utf-8")
        for rule in (
            "Original **A before-change**", "**Approved B**", "A-to-B change illustration",
            "Retry exactly three times", "independent reviewer", "provisional",
            "accept-warning", "capture_instability", "visible_ui_defect",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, source)

    def test_side_by_side_warning_handoff_and_canonical_blocker_commands_are_documented(self):
        browser_qa = (SKILL_ROOT / "references/visual-qa.md").read_text(encoding="utf-8")
        workflow = (SKILL_ROOT / "references/workflow.md").read_text(encoding="utf-8")
        for phrase in ("warning-record", "warning-decide", "comparison.html", "side-by-side", "original_before_to_original_after"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, browser_qa)
        self.assertIn("reconcile --action block", workflow)
        self.assertIn("reconcile --action unblock", workflow)

    def test_discovery_and_parity_are_mandatory_prior_to_prototype_review(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        workflow = (SKILL_ROOT / "references/workflow.md").read_text(encoding="utf-8")
        reference = (SKILL_ROOT / "references/functionality-discovery.md")
        self.assertTrue(reference.is_file())
        self.assertIn("references/functionality-discovery.md", skill)
        for command in ("discovery-record", "prototype-coverage", "missing-feature"):
            self.assertIn(command, skill)
            self.assertIn(command, workflow)
        for template in ("discovery.template.json", "prototype-coverage.template.json",
                         "missing-feature.template.json"):
            with self.subTest(template=template):
                self.assertTrue((SKILL_ROOT / "assets/project" / template).is_file())
        self.assertIn("one full zero-new sweep", reference.read_text(encoding="utf-8"))
        self.assertIn("Never silently dismiss", reference.read_text(encoding="utf-8"))

    def test_compaction_and_fresh_session_recovery_are_mandatory(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        continuity = (SKILL_ROOT / "references/continuity.md").read_text(encoding="utf-8")
        workflow = (SKILL_ROOT / "references/workflow.md").read_text(encoding="utf-8")
        self.assertIn("work-update", skill)
        self.assertIn("resume --project-root", skill)
        self.assertIn("--format json", skill)
        self.assertIn("work-update", workflow)
        self.assertIn("before context compaction", continuity)
        self.assertIn("after an interruption", continuity.lower())
        template = json.loads((SKILL_ROOT / "assets/project/work.template.json").read_text())
        self.assertTrue({"task_id", "phase", "status", "completed_steps", "pending_steps", "next_action"}.issubset(template))


if __name__ == "__main__":
    unittest.main()
