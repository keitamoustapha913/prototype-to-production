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
    def test_manifest_has_durable_state_fields(self):
        manifest = json.loads((SKILL_ROOT / "assets/project/manifest.template.json").read_text())
        self.assertTrue({
            "schema_version", "initiative_id", "scope", "stage", "approved_version",
            "references", "slices", "next_action", "last_checkpoint", "provenance",
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
        self.assertNotIn("--update-snapshots", source)


if __name__ == "__main__":
    unittest.main()
