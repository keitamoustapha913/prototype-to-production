from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import uuid
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from scripts import visual_integrity as visual


class VisualIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.manifest = {"slices": {"items-slice": {}}}
        self.case = {
            "case_id": "list-default", "route": "/items", "fixture": "member",
            "state_setup": "default list", "viewport": {"width": 1280, "height": 800},
        }

        def snapshot(directory, slice_id, value, label, viewport):
            color = "green" if value.startswith("corrected") else "red"
            stream = BytesIO()
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("capture", "closeup" if value == "latest" else value)
            size = (20, 20) if value.endswith("full") else (10, 10)
            image = Image.new("RGB", size, color)
            if value.startswith("corner"):
                image.putpixel((0, 0), (1, 2, 3))
            image.save(
                stream, "PNG", pnginfo=metadata, compress_level=0 if value == "reencoded" else 9)
            return visual._store_image(directory, slice_id, stream.getvalue())

        self.patches = [
            patch.object(visual, "result_fingerprint", return_value="result-v1"),
            patch.object(visual, "_adaptive_contract", return_value=None),
            patch.object(visual, "_approved_visual_cases", return_value={"list-default": self.case}),
            patch.object(visual, "_snapshot_png", side_effect=snapshot),
            patch.object(visual, "verify_saved_images"),
            patch.object(visual, "_require_capture", return_value={"captured_at": "9999"}),
            patch.object(visual, "_annotated_png", side_effect=lambda directory, slice_id, path, rect: path),
        ]
        for item in self.patches:
            item.start()

    def record(self, payload):
        for case in payload["cases"]:
            case["capture_id"] = str(uuid.uuid4())
        return visual.record_attempt(self.directory, self.manifest, "items-slice", payload)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def payload(self, status="clean"):
        return {
            "status": status,
            "observations": [
                {"dimension": dimension, "result": "pass", "note": f"Reviewed {dimension}"}
                for dimension in sorted(visual.OBSERVATION_DIMENSIONS)
            ],
            "findings": [], "rechecked_findings": [],
            "cases": [{
                "case_id": "list-default", "viewport": {"width": 1280, "height": 800},
                "state": {"route": "/items", "fixture": "member", "state_setup": "default list"},
                "full_screenshot": "full", "closeup_screenshot": "closeup", "comparisons": [],
                "closeup_bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
            }],
        }

    def test_adaptive_coverage_requires_all_three_checks_for_each_case(self):
        case_id = "list-default"
        coverage = [{
            "case_id": case_id, "component": "Items list", "state": "default",
            "viewport": self.case["viewport"],
            "reason": "Approved layout and selection behavior",
            "checks": {kind: {"result": "pass", "note": "Observed expected outcome"}
                       for kind in ("visual", "interaction", "blueprint")},
            "acceptance_ids": ["select-item"],
            "protected_decision_ids": [],
        }]
        with patch.object(visual, "_adaptive_contract", return_value=({"select-item"}, [])):
            payload = self.payload()
            payload["cases"][0]["state"].update(theme="light", interaction="default",
                                               data="member fixture", active_control=None, open_menus=[])
            with self.assertRaisesRegex(visual.VisualIntegrityError, "coverage"):
                self.record(payload)
            payload["cases"][0]["browser_evidence"] = {"state": {}, "geometry": {}}
            payload["coverage"] = coverage
            coverage[0]["checks"]["interaction"]["result"] = "not_checked"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "passing"):
                self.record(payload)
            coverage[0]["checks"]["interaction"]["result"] = "pass"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "interaction evidence"):
                self.record(payload)
            assertions = [{"result": "fail"}]
            payload["cases"][0]["browser_evidence"]["interaction_assertions"] = assertions
            with self.assertRaisesRegex(visual.VisualIntegrityError, "interaction evidence"):
                self.record(payload)
            coverage[0]["checks"]["interaction"]["result"] = "not_checked"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "failed coverage"):
                self.record(payload)
            coverage[0]["checks"]["interaction"]["result"] = "pass"
            assertions[0]["result"] = "pass"
            self.assertEqual(self.record(payload)["coverage"], coverage)

    def test_failed_approved_measurement_blocks_a_blueprint_pass(self):
        case_id = self.case["case_id"]
        rule = {"check_id": "menu-radius", "measurement": {"selector": "#menu", "property": "border-radius"},
                "expected": "8px"}
        self.case["visual_criteria"] = [rule]
        payload = self.payload()
        case = payload["cases"][0]
        case["state"].update(theme="light", data="fixture", interaction="default", active_control=None, open_menus=[])
        case["browser_evidence"] = {
            "interaction_assertions": [{"result": "pass"}],
            "blueprint_measurements": [{**rule, "actual": "0px"}],
        }
        payload["coverage"] = [{
            "case_id": case_id, "component": "Menu", "state": "default", "viewport": case["viewport"],
            "reason": "Approved rounded menu", "acceptance_ids": ["menu-radius"], "protected_decision_ids": [],
            "checks": {kind: {"result": "pass", "note": "Reviewed outcome"}
                       for kind in ("visual", "interaction", "blueprint")},
        }]
        with patch.object(visual, "_adaptive_contract", return_value=({"menu-radius"}, [])):
            with self.assertRaisesRegex(visual.VisualIntegrityError, "approved measurement"):
                self.record(payload)
            case["browser_evidence"]["blueprint_measurements"][0]["actual"] = "8px"
            self.assertEqual(self.record(payload)["status"], "clean")

    def test_improved_repair_and_advisory_preserve_distinct_outcomes(self):
        case_id = "list-default"
        protection = {"decision_id": "rounded-trigger"}
        payload = self.payload("unresolved")
        case = payload["cases"][0]
        case["state"].update(theme="light", data="member fixture", interaction="default",
                             active_control=None, open_menus=[])
        case["browser_evidence"] = {"state": {}, "geometry": {},
                                    "interaction_assertions": [{"result": "pass"}]}
        payload["coverage"] = [{
            "case_id": case_id, "component": "Items list", "state": "default", "viewport": self.case["viewport"],
            "reason": "Approved layout", "acceptance_ids": [], "protected_decision_ids": ["rounded-trigger"],
            "checks": {key: {"result": "pass", "note": "Observed fixture"}
                       for key in ("visual", "interaction", "blueprint")},
        }]
        finding = {"finding_id": "clip", "case_id": case_id, "dimension": "geometry", "severity": "major",
                   "description": "Label clips", "status": "open", "classification": "confirmed_defect",
                   "classification_reason": "Client width is smaller than scroll width",
                   "evidence": "Measured label overflow", "protected_decision_ids": ["rounded-trigger"]}
        comparison = {"finding_id": "clip", "before": "closeup", "latest": "closeup",
                      "normalized_rect": {"x": 0, "y": 0, "width": 1, "height": 1}}
        payload["findings"] = [finding]
        case["comparisons"] = [comparison]
        with patch.object(visual, "_adaptive_contract", return_value=(set(), [protection])):
            self.record(payload)
            finding.update(status="improved", repair_scope="Label padding preserves the rounded trigger",
                           correction="Adjusted label padding", verification="Label fits. Keyboard recheck pending.")
            payload["status"] = "improved_not_verified"
            payload["coverage"][0]["checks"]["interaction"]["result"] = "not_checked"
            comparison["latest"] = case["closeup_screenshot"] = "corrected"
            case["full_screenshot"] = "corrected-full"
            self.assertEqual(self.record(payload)["status"], "improved_not_verified")
            with self.assertRaisesRegex(visual.VisualIntegrityError, "improved_not_verified"):
                visual.require_verified(self.directory, self.manifest, "items-slice")
            payload.update(status="corrected", rechecked_findings=["clip"])
            finding["status"] = "fixed"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "passing"):
                self.record(payload)
            payload["coverage"][0]["checks"]["interaction"]["result"] = "pass"
            original_assertions = case["browser_evidence"]["interaction_assertions"]
            case["browser_evidence"]["interaction_assertions"] = [{"selector": "#unrelated", "result": "pass"}]
            with self.assertRaisesRegex(visual.VisualIntegrityError, "prior repair assertions"):
                self.record(payload)
            case["browser_evidence"]["interaction_assertions"] = original_assertions
            related = {"finding_id": "shape", "case_id": case_id, "dimension": "design", "severity": "info",
                       "description": "Rounded trigger", "status": "noted", "classification": "approved_difference",
                       "classification_reason": "Explicitly approved exception", "evidence": "Captured rounded trigger",
                       "protected_decision_ids": ["rounded-trigger"]}
            payload["findings"].append(related)
            case["comparisons"].append({**comparison, "finding_id": "shape", "before": "corrected"})
            recorded = self.record(payload)
            self.assertEqual(recorded["rechecked_findings"], ["clip"])
            self.assertEqual(visual.require_verified(self.directory, self.manifest, "items-slice")["status"], "corrected")

    def test_clean_attempt_is_durable_and_verified_for_current_result(self):
        saved = self.record(self.payload())
        self.assertEqual(saved["attempt_number"], 1)
        verified = visual.require_verified(self.directory, self.manifest, "items-slice")
        self.assertEqual(verified["attempt_id"], saved["attempt_id"])
        self.assertEqual(visual.list_attempts(self.directory, manifest=self.manifest)[0]["status"], "clean")

    def test_repair_must_recheck_every_original_region_of_one_finding(self):
        initial = self.payload("unresolved")
        finding = {"finding_id": "overlap", "case_id": "list-default",
                   "dimension": "geometry", "status": "open",
                   "severity": "high", "description": "Two item labels overlap"}
        initial["findings"] = [finding]
        case = initial["cases"][0]
        case["comparisons"] = [
            {"finding_id": "overlap", "before": "closeup", "latest": "closeup",
             "normalized_rect": {"x": x, "y": 0.0, "width": 0.4, "height": 0.4}}
            for x in (0.0, 0.5)
        ]
        self.record(initial)

        final = deepcopy(initial)
        final.update(status="corrected", rechecked_findings=["overlap"])
        final["findings"][0].update(status="fixed", correction="Widened both columns",
                                    verification="Both labels fit at the approved width")
        case = final["cases"][0]
        case.update(full_screenshot="corrected-full", closeup_screenshot="corrected")
        for comparison in case["comparisons"]:
            comparison["latest"] = "corrected"
        second = case["comparisons"].pop()
        with self.assertRaisesRegex(visual.VisualIntegrityError, "highlighted region"):
            self.record(final)
        case["comparisons"].insert(0, second)
        self.assertEqual(len(self.record(final)["cases"][0]["comparisons"]), 2)
        self.assertEqual(visual.require_verified(self.directory, self.manifest, "items-slice")["status"],
                         "corrected")

    def test_closeup_requires_matching_page_region_even_with_two_filenames(self):
        payload = self.payload()
        case = payload["cases"][0]
        case["closeup_screenshot"] = "duplicate-full"
        with self.assertRaisesRegex(visual.VisualIntegrityError, "component bounds"):
            self.record(payload)
        case["closeup_bounds"].update(width=20, height=20)
        self.record(payload)
        case.update(closeup_screenshot="corrected", closeup_bounds=dict(x=0, y=0, width=10, height=10))
        with self.assertRaisesRegex(visual.VisualIntegrityError, "pixels differ"):
            self.record(payload)

    def test_additional_mobile_keyboard_state_preserves_the_locked_case(self):
        payload = self.payload()
        mobile = deepcopy(payload["cases"][0])
        mobile.update(case_id="menu-mobile-focus", based_on="list-default", viewport={"width": 390, "height": 844},
                      state={"route": "/items", "interaction": "menu open with keyboard focus"})
        payload["cases"].append(mobile)
        saved = self.record(payload)
        self.assertEqual(saved["cases"][1]["based_on"], "list-default")
        payload["cases"] = [mobile]
        with self.assertRaisesRegex(visual.VisualIntegrityError, "every approved visual case"):
            self.record(payload)

    def test_failed_observation_cannot_be_recorded_as_clean(self):
        payload = self.payload()
        payload["observations"][0]["result"] = "fail"
        with self.assertRaisesRegex(visual.VisualIntegrityError, "passing observations"):
            self.record(payload)

    def test_unresolved_latest_attempt_blocks_even_after_clean_attempt(self):
        self.record(self.payload())
        payload = self.payload("unresolved")
        payload["observations"][0].update(result="fail", note="The selected item still overlaps its label")
        payload["findings"] = [{"finding_id": "overlap", "case_id": "list-default",
                                "dimension": "geometry", "status": "open",
                                "severity": "high", "description": "Selected item overlaps its label"}]
        payload["cases"][0]["comparisons"] = [{"before": "latest", "latest": "latest",
            "finding_id": "overlap", "normalized_rect": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.2}}]
        self.record(payload)
        with self.assertRaisesRegex(visual.VisualIntegrityError, "unresolved"):
            visual.require_verified(self.directory, self.manifest, "items-slice")

    def test_unresolved_finding_must_be_carried_and_rechecked_across_fingerprints(self):
        payload = self.payload("unresolved")
        payload["observations"][0].update(result="fail", note="Needs correction")
        payload["findings"] = [{"finding_id": "overlap", "case_id": "list-default",
                                "dimension": "geometry", "status": "open", "severity": "high", "description": "Overlap"}]
        payload["cases"][0]["comparisons"] = [{"before": "latest", "latest": "latest",
            "finding_id": "overlap", "normalized_rect": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.2}}]
        self.record(payload)
        with patch.object(visual, "result_fingerprint", return_value="result-v2"):
            fixed = self.payload("corrected")
            fixed["findings"] = [{"finding_id": "overlap", "case_id": "list-default",
                                  "dimension": "geometry", "status": "fixed", "severity": "high",
                                  "description": "Overlap removed", "correction": "Adjusted selected row spacing",
                                  "verification": "Replayed selection and confirmed no overlap"}]
            fixed["rechecked_findings"] = ["overlap"]
            fixed["cases"][0]["full_screenshot"] = "corrected-full"
            fixed["cases"][0]["closeup_screenshot"] = "corrected"
            fixed["cases"][0]["comparisons"] = [{"before": "latest", "latest": "corrected",
                "finding_id": "overlap", "normalized_rect": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.2}}]
            unchanged = deepcopy(fixed)
            unchanged["cases"][0]["full_screenshot"] = "full"
            unchanged["cases"][0]["closeup_screenshot"] = "reencoded"
            unchanged["cases"][0]["comparisons"][0]["latest"] = "reencoded"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "identical image"):
                self.record(unchanged)
            # A responsive clipping fix has the same visual evidence requirement.
            prior = visual.list_attempts(self.directory, manifest=self.manifest)
            prior[0]["findings"][0]["dimension"] = "responsive"
            responsive = deepcopy(unchanged)
            responsive["findings"][0]["dimension"] = "responsive"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "marked regions"):
                visual._validate_payload(self.directory, self.manifest, "items-slice", responsive, prior)
            unchanged["findings"][0]["dimension"] = "interaction"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "original finding dimension"):
                self.record(unchanged)
            unchanged["findings"][0]["dimension"] = "geometry"
            unchanged["cases"][0].update(full_screenshot="corner-full", closeup_screenshot="corner")
            unchanged["cases"][0]["comparisons"][0]["latest"] = "corner"
            with self.assertRaisesRegex(visual.VisualIntegrityError, "marked regions"):
                self.record(unchanged)
            saved = self.record(fixed)
            visual.require_verified(self.directory, self.manifest, "items-slice")
            self.assertEqual(saved["status"], "corrected")

    def test_tampered_append_only_journal_is_rejected(self):
        self.record(self.payload())
        journal = self.directory / "visual-integrity/items-slice/attempts.jsonl"
        entry = json.loads(journal.read_text())
        entry["status"] = "unresolved"
        journal.write_text(json.dumps(entry) + "\n")
        with self.assertRaisesRegex(visual.VisualIntegrityError, "chain is invalid"):
            visual.list_attempts(self.directory, "items-slice", manifest=self.manifest)

    def test_repeated_open_finding_stops_automatic_repair_after_two_failed_rechecks(self):
        payload = self.payload("unresolved")
        payload["observations"][0].update(result="fail", note="Overlap remains")
        payload["findings"] = [{"finding_id": "overlap", "case_id": "list-default",
                                "dimension": "geometry", "status": "open",
                                "severity": "high", "description": "Selected item overlaps its label"}]
        payload["cases"][0]["comparisons"] = [{"before": "latest", "latest": "latest",
            "finding_id": "overlap", "normalized_rect": {"x": 0.1, "y": 0.1, "width": 0.4, "height": 0.2}}]
        self.record(payload)
        payload["findings"][0].update(correction="Adjusted padding", verification="The label still overlaps")
        self.record(payload)
        third = self.record(payload)
        self.assertEqual(third["retry_policy"]["automatic_repair_stopped"], ["overlap"])
        self.assertEqual(third["retry_policy"]["next_step"], "fresh investigation with a new diagnosis")
        with self.assertRaisesRegex(visual.VisualIntegrityError, "new diagnosis"):
            self.record(payload)
        self.assertEqual(len(visual.list_attempts(self.directory, manifest=self.manifest)), 3)

        payload["findings"][0]["diagnosis"] = "The column width clips its label"
        restarted = self.record(payload)
        self.assertEqual(restarted["retry_policy"]["failed_repairs"]["overlap"], 1)
        self.record(payload)
        payload["findings"][0]["diagnosis"] = "  the column WIDTH clips its label  "
        with self.assertRaisesRegex(visual.VisualIntegrityError, "new diagnosis"):
            self.record(payload)



if __name__ == "__main__":
    unittest.main()
