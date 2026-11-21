"""Black-box CLI tests; every command starts a fresh process like a new Codex session."""

import json
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/project_state.py"


def png(width=1440, height=900, color=(244, 246, 250)):
    """Generate a real, deterministic PNG without Pillow or external binaries."""
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    row = b"\x00" + bytes(color) * width
    image = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(image, 6))
        + chunk(b"IEND", b"")
    )


class ProjectStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p2p-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.initiative = self.root / "design/redesigns/billing-refresh"
        # A test directory can itself live under the skill Git checkout.
        # Prevent discovery of an unrelated parent repository.
        self.test_env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(self.root.parent)}

    def cli(self, command, *extra, success=True):
        if command == "init" and "--mode" not in extra:
            extra = (*extra, "--mode", "prototype")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--project-root", str(self.root),
             "--initiative-id", "billing-refresh", *map(str, extra)],
            text=True, capture_output=True, env=self.test_env,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli_without_id(self, command, *extra, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--project-root", str(self.root), *map(str, extra)],
            text=True, capture_output=True, env=self.test_env,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def work_update(self, *, task_id="inspect-roles", phase="discover", status="in_progress",
                    completed_steps=None, pending_steps=None, blockers=None, code_paths=None):
        payload = {
            "task_id": task_id, "phase": phase, "status": status,
            "summary": "Traced the invoice flow and authorization checks",
            "next_action": "Inspect the permission guard and record the behavior",
            "completed_steps": completed_steps if completed_steps is not None else ["Identify invoice route"],
            "pending_steps": pending_steps if pending_steps is not None else ["Inspect permission guard"],
            "blockers": blockers if blockers is not None else [],
            "code_paths": code_paths if code_paths is not None else [],
            "artifact_paths": [],
        }
        return self.cli("work-update", "--work-file", self.write_input("work.json", payload))

    def init(self, *, legacy=True, scope="page"):
        self.cli("init", "--scope", scope, "--next-action", "Map billing states")
        if legacy:
            # Compatibility fixtures predate the visual baseline requirement.
            if not self.initiative.exists():
                self.initiative = self.root / ".artifacts/prototype-to-production/billing-refresh"
            path = self.initiative / "manifest.json"
            manifest = self.manifest()
            manifest.pop("visual_baselines_required", None)
            path.write_text(json.dumps(manifest), encoding="utf-8")

    def write_input(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def discovery_audit(self, audit_id="audit-v1", *, include_note=False):
        source = self.root / "src" / "invoices.ts"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("export function invoiceSearch() { return 'rows'; }\n", encoding="utf-8")
        self.artifact("discovery/evidence/invoices.png", png(width=390, height=844))
        features = [{
            "feature_id": "invoice-search",
            "route": "/invoices",
            "behavior": "Search and filter invoices with retained search state.",
            "roles": ["manager", "viewer"],
            "states": ["default", "filtered", "empty"],
            "source_anchors": [{"path": "src/invoices.ts", "contains": "invoiceSearch"}],
            "browser_evidence": ["discovery/evidence/invoices.png"],
        }]
        if include_note:
            features.append({
                "feature_id": "invoice-note",
                "route": "/invoices",
                "behavior": "Append an invoice note for permitted users.",
                "roles": ["manager"],
                "states": ["detail", "saved"],
                "source_anchors": [{"path": "src/invoices.ts", "contains": "invoiceSearch"}],
                "browser_evidence": ["discovery/evidence/invoices.png"],
            })
        prior = self.manifest().get("active_discovery_id")
        new_features = (["invoice-note"] if include_note else []) if prior else [
            item["feature_id"] for item in features
        ]
        return {
            "audit_id": audit_id, "scope": self.manifest()["scope"], "routes": ["/invoices"],
            "features": features, "unresolved": [],
            "sweeps": [
                {"sweep_id": "source-and-browser", "routes_checked": ["/invoices"],
                 "source_files_checked": ["src/invoices.ts"],
                 "new_feature_ids": new_features},
                {"sweep_id": "independent-cross-check", "routes_checked": ["/invoices"],
                 "source_files_checked": ["src/invoices.ts"], "new_feature_ids": []},
            ],
        }

    def discovery_record(self, audit):
        return self.cli("discovery-record", "--audit-file", self.write_input("audit.json", audit))

    def prototype_coverage(self, approval, *, features=("invoice-search",)):
        version = approval["approved_version"]
        mappings = []
        for feature_id in features:
            proof = f"coverage/evidence/{version}/{feature_id}.json"
            self.artifact(proof, json.dumps({
                "feature_id": feature_id, "result": "PASS",
                "command": f"npx playwright test {feature_id}",
                "observed_steps": ["Open the A-derived flow", "Exercise and confirm its behavior"],
            }).encode("utf-8"))
            mappings.append({
                "feature_id": feature_id,
                "status": "IMAGE_ONLY_EXCEPTION" if approval["mode"] == "image-only" else "PASS",
                "prototype_source_paths": approval["source_paths"],
                "test_evidence": [proof],
            })
        return {
            "prototype_version": version, "audit_id": self.manifest()["active_discovery_id"],
            "mappings": mappings,
        }

    def parity_record(self, parity):
        return self.cli("prototype-coverage", "--coverage-file", self.write_input("parity.json", parity))

    def missing_report(self, report_id="gap-001"):
        self.artifact("discovery/evidence/missing-feature.png", png(width=390, height=844))
        report = {
            "report_id": report_id,
            "summary": "An unrepresented invoice note action was found after prototype approval.",
            "suspected_features": ["invoice-note"],
            "evidence_paths": ["discovery/evidence/missing-feature.png"],
        }
        return self.cli("missing-feature", "--report-file", self.write_input("missing.json", report))

    def manifest(self):
        return json.loads((self.initiative / "manifest.json").read_text(encoding="utf-8"))

    def artifact(self, relative, contents=b"artifact-v1"):
        path = self.initiative / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def approval(self, version="v1", mode="source", source=True, replay=True):
        source_path = f"prototype/versions/{version}/invoices.tsx"
        screenshot_path = f"references/{version}/invoices-desktop.png"
        fixture_path = f"fixtures/{version}/invoices.json"
        if source:
            self.artifact(source_path, b"export const Invoices = () => <table />;")
        self.artifact(screenshot_path, png(color=(244, 246, 250) if version == "v1" else (242, 244, 249)))
        self.artifact(fixture_path, b'{"rows": ["INV-001"]}')
        approval = {
            "decision_id": f"decision-{version}", "approved_version": version,
            "scope": "page", "mode": mode, "approved_by_user": True,
            "user_approval_note": "Looks like the preview; approved desktop invoice page",
            "source_paths": [source_path] if source else [],
            "visual_cases": [{
                "case_id": "invoices-desktop", "reference_file": screenshot_path,
                "route": "/invoices", "viewport": {"width": 1440, "height": 900},
                "fixture": fixture_path, "state_setup": "default",
            }], "exceptions": [],
        }
        if mode == "source" and replay:
            approval["replay"] = self.replay_proof(approval)
        return approval

    def replay_proof(self, approval, *, controls_visible=False, cases=None):
        """A saved browser-run contract; the coordinator still reviews the real pixels."""
        version = approval["approved_version"]
        script = f"prototype/versions/{version}/replay.mjs"
        evidence = f"evidence/{version}/replay.json"
        self.artifact(script, b"// Invoke the reproducible fixture setup and compare screenshots.\n")
        logged_cases = []
        for case in approval["visual_cases"]:
            source = (self.initiative / case["reference_file"]).read_bytes()
            first = f"evidence/{version}/{case['case_id']}-run1.png"
            second = f"evidence/{version}/{case['case_id']}-run2.png"
            self.artifact(first, source)
            self.artifact(second, source)
            logged_cases.append({
                "case_id": case["case_id"],
                "reference_sha256": hashlib.sha256(source).hexdigest(),
                "first_capture": first,
                "second_capture": second,
                "fixture_replayed": True,
                "matching_pixels": True,
                "qa_controls_visible": controls_visible,
            })
        if cases is not None:
            logged_cases = cases
        self.artifact(evidence, json.dumps({
            "status": "PASS", "clean_context_runs": 2,
            "cases": logged_cases,
        }).encode("utf-8"))
        return {
            "command": f"node {script}",
            "script": script,
            "evidence_file": evidence,
            "qa_controls_excluded": not controls_visible,
        }

    def approve(self, approval):
        # Model a complete prior discovery/parity stage for unrelated acceptance tests.
        # Tests that assert the mandatory gate call the CLI directly instead.
        if not self.manifest().get("active_discovery_id"):
            self.discovery_record(self.discovery_audit())
        if approval["approved_version"] not in self.manifest()["prototype_coverage"]:
            self.parity_record(self.prototype_coverage(approval))
        approval_file = self.write_input("approve.json", approval)
        return self.cli("approve", "--approval-file", approval_file)

    def slice_packet(self, approval, slice_id="invoice-list", owner="agent-1", paths=None):
        return {
            "slice_id": slice_id, "scope": "page", "owner": owner, "status": "draft",
            "routes": ["/invoices"],
            "allowed_paths": paths or ["src/features/invoices/**"],
            "dependencies": [],
            "current_behavior": ["List, search and filter invoices; navigate to details"],
            "prototype_version": approval["approved_version"],
            "prototype_source_paths": approval["source_paths"],
            "visual_cases": approval["visual_cases"],
            "design_tokens": [],
            "acceptance_checks": ["Exact density, spacing, alignment and labels match approved screenshot"],
            "functional_tests": ["Search, filter, and invoice detail navigation work"],
            "prohibited_deviations": ["Do not alter approved copy or table layout"],
            "evidence": {"functional": [], "visual": []}, "blockers": [],
        }

    def slice_create(self, packet, success=True):
        return self.cli("slice-create", "--contract-file", self.write_input("slice.json", packet), success=success)

    def submit_slice(self):
        report = {
            "slice_id": "invoice-list", "owner": "agent-1", "status": "submitted",
            "summary": "Implemented the invoice flow", "implementation_paths": ["src/features/invoices/table.tsx"],
            "functional_evidence": ["npm run test:billing"], "visual_evidence": [], "shared_ui_paths": [],
            "blockers": [], "next_action": "Independent verification",
        }
        self.cli("slice-report", "--slice-id", "invoice-list", "--owner", "agent-1",
                 "--report-file", self.write_input("report.json", report))
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "report")

    def review(self, approval, screenshot=True, tool="playwright", result="PASS", record_integrity=True):
        if screenshot:
            self.artifact("slices/invoice-list/evidence/implemented.png", png(color=(245, 247, 252)))
        self.artifact("slices/invoice-list/evidence/functional.log", b"PASS search/filter/detail")
        code = self.root / "src/features/invoices/table.tsx"
        code.parent.mkdir(parents=True, exist_ok=True)
        code.write_text("export const InvoiceTable = () => null;", encoding="utf-8")
        if record_integrity:
            self.visual_integrity_attempt(approval)
        relative_ref = approval["visual_cases"][0]["reference_file"]
        digest = hashlib.sha256((self.initiative / relative_ref).read_bytes()).hexdigest()
        return {
            "slice_id": "invoice-list", "reviewer": "coordinator",
            "notes": "Compared rendered invoice table, density, spacing, texts, states and screenshots",
            "visual_evidence": [{
                "case_id": "invoices-desktop", "reference_sha256": digest,
                "implementation_screenshot": "slices/invoice-list/evidence/implemented.png",
                "tool": tool, "command": "npx playwright test --grep invoice-list", "result": result,
                "notes": "Reviewed exact viewport, fixture and state against locked PNG",
            }],
            "functional_evidence": [{
                "name": "Invoice search/filter/navigation", "command": "npm run test:billing",
                "result": "PASS", "output_file": "slices/invoice-list/evidence/functional.log",
                "covers": ["Search, filter, and invoice detail navigation work"],
            }],
        }

    def visual_integrity_attempt(self, approval, status="clean", dimension="geometry"):
        """Record the separate post-implementation QA evidence used by gate tests."""
        cases = []
        for case in approval["visual_cases"]:
            latest = png(case["viewport"]["width"], case["viewport"]["height"])
            prefix = f"slices/invoice-list/evidence/visual-integrity-{case['case_id']}"
            full_path = f"{prefix}-full.png"
            closeup_path = f"{prefix}-closeup.png"
            self.artifact(full_path, latest)
            self.artifact(closeup_path, png(64, 32))
            case_record = {
                "case_id": case["case_id"], "viewport": case["viewport"],
                "state": {"route": case["route"], "fixture": case["fixture"],
                          "state_setup": case["state_setup"]},
                "full_screenshot": full_path, "closeup_screenshot": closeup_path,
                "closeup_bounds": {"x": 0, "y": 0, "width": 64, "height": 32},
            }
            if status == "unresolved":
                case_record["comparisons"] = [{
                    "before": closeup_path, "latest": closeup_path,
                    "finding_id": "geometry-mismatch", "severity": "major",
                    "normalized_rect": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                }]
            from fixtures.visual_capture import capture_fixture_case, coverage_row
            capture_fixture_case(self.initiative, "invoice-list", case_record)
            cases.append(case_record)
        observations = [
            {"dimension": dimension, "result": "pass", "note": note}
            for dimension, note in (
                ("geometry", "Compared layout bounds and spacing."),
                ("design", "Compared typography, color, and component treatment."),
                ("interaction", "Checked the documented visible state."),
                ("responsive", "Checked the approved viewport."),
            )
        ]
        findings = []
        if status == "unresolved":
            next(item for item in observations if item["dimension"] == dimension)["result"] = "fail"
            findings = [{"finding_id": "geometry-mismatch", "case_id": cases[0]["case_id"],
                         "dimension": dimension, "status": "open", "severity": "major",
                         "description": "The content column is wider than the approved layout.",
                         "classification": "confirmed_defect", "classification_reason": "Measured layout deviation",
                         "evidence": "Column width exceeds the approved value", "protected_decision_ids": [],
                         "repair_scope": "Spacing inside the owned component"}]
        attempt = {
            "status": status,
            "observations": observations,
            "findings": findings, "rechecked_findings": [], "cases": cases,
            "coverage": [coverage_row(case) for case in cases],
        }
        self.cli("visual-integrity", "--action", "record", "--slice-id", "invoice-list",
                 "--attempt-file", self.write_input("visual-integrity.json", attempt))

    def warning_review(self, approval):
        """An independently reviewed capture-only mismatch, not a visual PASS."""
        review = self.review(approval, result="WARNING")
        visual = review["visual_evidence"][0]
        retry_paths = [
            f"slices/invoice-list/evidence/retry-{n}.png" for n in range(1, 4)
        ]
        for n, retry in enumerate(retry_paths):
            self.artifact(retry, png(color=(245 + n, 247, 252)))
        diagnosis_path = "slices/invoice-list/evidence/capture-diagnosis.json"
        self.artifact(diagnosis_path, json.dumps({
            "case_id": "invoices-desktop",
            "classification": "capture_instability",
            "comparison_scope": "approved_to_implementation",
            "investigation": "Fresh contexts and identical layout/text/interaction checks; only edge raster varies.",
            "retry_different_pixels": [20, 17, 21],
            "retry_context_ids": ["isolated-context-1", "isolated-context-2", "isolated-context-3"],
            "browser_version": "Chromium pinned-test-version",
            "visible_ui_defect": False,
        }).encode("utf-8"))
        visual["capture_warning"] = {
            "diagnosis_file": diagnosis_path,
            "retry_screenshots": retry_paths,
            "reviewer_notes": "Compared every retry: geometry, copy, controls and interactions are unchanged.",
        }
        return review

    def events(self):
        return [json.loads(line) for line in (self.initiative / "decisions.jsonl").read_text().splitlines()]

    def test_init_is_idempotent_but_does_not_overwrite_scope(self):
        self.init()
        self.assertEqual(self.manifest()["scope"], "page")
        self.cli("init", "--scope", "page")
        self.assertEqual(self.events(), [])
        failure = self.cli("init", "--scope", "app", success=False)
        self.assertNotEqual(failure.returncode, 0)
        self.assertEqual(self.manifest()["scope"], "page")

    def test_git_project_uses_ignored_hidden_artifacts_without_changing_gitignore(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        gitignore = self.root / ".gitignore"
        gitignore.write_text("node_modules/\n", encoding="utf-8")
        self.cli("init", "--scope", "page")
        hidden = self.root / ".artifacts/prototype-to-production/billing-refresh"
        self.assertTrue((hidden / "manifest.json").is_file())
        self.assertFalse((self.initiative / "manifest.json").exists())
        self.assertEqual(gitignore.read_text(), "node_modules/\n")
        ignored = subprocess.run(
            ["git", "-C", str(self.root), "check-ignore", "-q",
             ".artifacts/prototype-to-production/billing-refresh/manifest.json"],
            capture_output=True,
        )
        self.assertEqual(ignored.returncode, 0)
        status = subprocess.check_output(["git", "-C", str(self.root), "status", "--short"], text=True)
        self.assertNotIn(".artifacts", status)
        self.assertIn("NOT YET APPROVED", self.cli("validate").stdout)

    def test_git_project_still_resumes_a_preexisting_legacy_initiative(self):
        self.init()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.assertIn("billing-refresh", self.cli("resume").stdout)
        self.cli("init", "--scope", "page")
        ignored = subprocess.run(
            ["git", "-C", str(self.root), "check-ignore", "-q",
             "design/redesigns/billing-refresh/manifest.json"],
            capture_output=True,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertTrue((self.initiative / "manifest.json").is_file())
        self.assertFalse((self.root / ".artifacts/prototype-to-production/billing-refresh").exists())

    def test_resumed_legacy_initiative_is_ignored_without_rerunning_init(self):
        self.init()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.cli("resume")
        ignored = subprocess.run(
            ["git", "-C", str(self.root), "check-ignore", "-q", "--",
             str(self.initiative / "manifest.json")], capture_output=True,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_review_read_commands_leave_git_exclude_untouched_until_artifact_setup(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.cli("init", "--scope", "page", "--mode", "blueprint")
        exclude = self.root / ".git/info/exclude"
        original = "# Fresh receiving workspace\n"
        exclude.write_text(original)
        for command in ("resume", "validate", "slice-register", "slice-package-preview"):
            with self.subTest(command=command):
                failure = self.cli(command, success=False)
                self.assertIn("artifact-setup", failure.stderr)
                self.assertEqual(exclude.read_text(), original)
        self.cli("artifact-setup")
        self.cli("resume")
        self.cli("validate")
        self.cli("slice-register")

    def test_event_refuses_tracked_legacy_artifacts_without_modifying_them(self):
        self.init()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "design"], check=True)
        before = (self.initiative / "manifest.json").read_bytes()
        event = self.write_input("event.json", {"type": "progress", "summary": "Continue design"})
        failure = self.cli("event", "--event-file", event, success=False)
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("tracked redesign artifacts", failure.stderr)
        self.assertEqual((self.initiative / "manifest.json").read_bytes(), before)
        self.assertEqual(self.events(), [])

    def test_validate_refuses_force_added_prototype_source(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.init()
        self.initiative = self.root / ".artifacts/prototype-to-production/billing-refresh"
        prototype = self.artifact("prototype/versions/v1/index.html", b"<h1>Preview</h1>")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", str(prototype)], check=True)
        failure = self.cli("validate", success=False)
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("tracked redesign artifacts", failure.stderr)

    def test_resume_ignores_prototype_even_when_only_manifest_was_ignored(self):
        self.init()
        prototype = self.artifact("prototype/versions/v1/index.html", b"<h1>Preview</h1>")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(
            "/design/redesigns/billing-refresh/manifest.json\n", encoding="utf-8",
        )
        self.cli("resume")
        ignored = subprocess.run(
            ["git", "-C", str(self.root), "check-ignore", "-q", "--", str(prototype)],
            capture_output=True,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_hidden_parent_symlink_cannot_redirect_git_artifacts(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / ".artifacts").symlink_to(elsewhere, target_is_directory=True)
        result = self.cli("init", "--scope", "page", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((elsewhere / "prototype-to-production").exists())

    def test_git_nested_project_artifacts_are_ignored_within_its_subdirectory(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        project = self.root / "apps" / "dashboard"
        project.mkdir(parents=True)
        self.root = project
        self.cli("init", "--scope", "page")
        hidden = project / ".artifacts/prototype-to-production/billing-refresh/manifest.json"
        self.assertTrue(hidden.is_file())
        status = subprocess.check_output(["git", "-C", str(project), "status", "--short"], text=True)
        self.assertNotIn(".artifacts", status)

    def test_non_git_project_works_even_when_git_is_not_installed(self):
        for command, extra in (("init", ["--scope", "page"]), ("resume", [])):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), command, "--project-root", str(self.root),
                 "--initiative-id", "billing-refresh", *extra],
                capture_output=True, text=True,
                env={**self.test_env, "PATH": str(self.root / "nonexistent-bin")},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.initiative / "manifest.json").is_file())

    def test_invalid_initiative_id_cannot_escape_project(self):
        result = subprocess.run([
            sys.executable, str(SCRIPT), "init", "--project-root", str(self.root),
            "--initiative-id", "../escape", "--scope", "page",
        ], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "design/escape").exists())

    def test_symlinked_initiative_must_not_redirect_files_outside_repo(self):
        container = self.root / "outside"
        container.mkdir()
        parent = self.root / "design/redesigns"
        parent.mkdir(parents=True)
        (parent / "billing-refresh").symlink_to(container, target_is_directory=True)
        rejection = self.cli("init", "--scope", "page", success=False)
        self.assertNotEqual(rejection.returncode, 0)
        self.assertFalse((container / "manifest.json").exists())

    def test_events_append_and_preserve_decisions(self):
        self.init()
        first = self.write_input("first.json", {
            "type": "style", "summary": "Compact tables; 36px row height", "code_paths": ["src/billing.tsx"],
            "next_action": "Prototype compact invoices",
        })
        self.cli("event", "--event-file", first)
        second = self.write_input("second.json", {
            "type": "interaction", "summary": "Filter persists across pagination",
        })
        self.cli("event", "--event-file", second)
        self.assertEqual([e["summary"] for e in self.events()], [
            "Compact tables; 36px row height", "Filter persists across pagination",
        ])
        self.assertEqual(self.manifest()["next_action"], "Prototype compact invoices")
        self.assertEqual(self.events()[0]["code_paths"], ["src/billing.tsx"])

    def test_material_events_and_approvals_update_checkpoint_recovery_index(self):
        self.init()
        style = self.write_input("style.json", {"type": "style", "summary": "Dense, friendly table UI"})
        self.cli("event", "--event-file", style)
        self.assertIn("Dense, friendly table UI", (self.initiative / "CHECKPOINT.md").read_text())
        self.approve(self.approval())
        resume = self.cli("resume").stdout
        self.assertIn("decision-v1", resume)
        self.assertIn("invoices-desktop", resume)
        self.assertIn("references/v1/invoices-desktop.png", resume)
        self.assertIn("Approved prototype v1", (self.initiative / "CHECKPOINT.md").read_text())

    def test_unapproved_initiative_is_not_misreported_as_verified(self):
        self.init()
        output = self.cli("validate").stdout
        self.assertIn("NOT YET APPROVED", output)
        self.assertNotIn("active approval", output.lower())

    def test_event_cannot_overwrite_approval_fields(self):
        self.init()
        malicious = self.write_input("bad.json", {
            "type": "decision", "summary": "Forged approval", "approved_version": "v9",
        })
        result = self.cli("event", "--event-file", malicious, success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.manifest()["approved_version"])
        self.assertEqual(self.events(), [])

    def test_checkpoint_resume_recovers_progress_without_chat_history(self):
        self.init()
        checkpoint = self.write_input("checkpoint.json", {
            "summary": "Prototype B working with empty state",
            "next_action": "Ask user to approve desktop and mobile",
            "blockers": ["Mobile dialog not captured"],
            "completed": ["Captured desktop default state"],
            "repository_head": "abcd1234", "worktree_status": " M src/billing.tsx",
        })
        self.cli("checkpoint", "--checkpoint-file", checkpoint)
        rehydrated = self.cli("resume").stdout
        self.assertIn("Ask user to approve desktop and mobile", rehydrated)
        self.assertIn("Mobile dialog not captured", rehydrated)
        self.assertIn("Captured desktop default state", rehydrated)
        self.assertIn("abcd1234", rehydrated)
        self.assertIn("Prototype B working", (self.initiative / "CHECKPOINT.md").read_text())

    def test_fresh_cli_session_recovers_exact_in_progress_work_without_initiative_id(self):
        self.init()
        self.cli("event", "--event-file", self.write_input("design-decision.json", {
            "type": "style", "summary": "User chose high-contrast table styling",
        }))
        self.work_update()
        packet = json.loads(self.cli_without_id("resume", "--format", "json").stdout)
        self.assertTrue(packet["safe_to_continue"])
        self.assertEqual(packet["initiative_id"], "billing-refresh")
        self.assertEqual(packet["phase"], "discover")
        self.assertEqual(packet["active_work"]["task_id"], "inspect-roles")
        self.assertEqual(packet["active_work"]["status"], "in_progress")
        self.assertEqual(packet["active_work"]["completed_steps"], ["Identify invoice route"])
        self.assertEqual(packet["active_work"]["pending_steps"], ["Inspect permission guard"])
        self.assertIn("Inspect the permission guard", packet["next_action"])
        self.assertIn("User chose high-contrast table styling",
                      [item["summary"] for item in packet["recent_decisions"]])
        self.assertTrue(Path(packet["decision_journal_file"]).is_file())
        self.assertIn("inspect-roles", (self.initiative / "CHECKPOINT.md").read_text())

    def test_resume_without_id_rejects_ambiguous_projects(self):
        self.init()
        self.cli("init", "--initiative-id", "other-refresh", "--scope", "page")
        ambiguous = self.cli_without_id("resume", "--format", "json", success=False)
        self.assertEqual(ambiguous.returncode, 2)
        self.assertIn("billing-refresh", ambiguous.stderr)
        self.assertIn("other-refresh", ambiguous.stderr)

    def test_task_cursor_cannot_skip_unfinished_steps_or_replace_an_active_task(self):
        self.init()
        self.work_update()
        rejected = self.cli("work-update", "--work-file", self.write_input("other.json", {
            "task_id": "build-prototype", "phase": "discover", "status": "in_progress",
            "summary": "Skipped ahead", "next_action": "Make new design",
            "completed_steps": [], "pending_steps": ["Build"],
            "blockers": [], "code_paths": [], "artifact_paths": [],
        }), success=False)
        self.assertIn("active", rejected.stderr.lower())
        incomplete = self.cli("work-update", "--work-file", self.write_input("done.json", {
            "task_id": "inspect-roles", "phase": "discover", "status": "done",
            "summary": "Finished all", "next_action": "Start prototype",
            "completed_steps": ["Identify invoice route"],
            "pending_steps": ["Inspect permission guard"],
            "blockers": [], "code_paths": [], "artifact_paths": [],
        }), success=False)
        self.assertIn("pending", incomplete.stderr.lower())
        self.assertEqual(self.manifest()["active_work_id"], "inspect-roles")

    def test_blocked_cursor_survives_compaction_and_never_says_ready(self):
        self.init()
        self.work_update(status="blocked", blockers=["API fixture missing"])
        packet = json.loads(self.cli("resume", "--format", "json").stdout)
        self.assertFalse(packet["safe_to_continue"])
        self.assertEqual(packet["active_work"]["status"], "blocked")
        self.assertIn("API fixture missing", packet["blockers"])
        self.assertIn("BLOCKED", (self.initiative / "CHECKPOINT.md").read_text())

    def test_resume_detects_external_code_changes_since_last_saved_work_step(self):
        self.init()
        code = self.root / "src" / "invoice.tsx"
        code.parent.mkdir()
        code.write_text("export const Invoice = () => null;", encoding="utf-8")
        self.work_update(code_paths=["src/invoice.tsx"])
        code.write_text("export const Invoice = () => <table />;", encoding="utf-8")
        packet = json.loads(self.cli("resume", "--format", "json").stdout)
        self.assertFalse(packet["safe_to_continue"])
        self.assertIn("src/invoice.tsx", " ".join(packet["reconciliation_required"]))

    def test_completed_work_is_preserved_and_fresh_session_uses_next_phase_step(self):
        self.init()
        self.work_update()
        self.work_update(status="done", completed_steps=["Identify invoice route", "Inspect permission guard"],
                         pending_steps=[])
        packet = json.loads(self.cli_without_id("resume", "--format", "json").stdout)
        self.assertIsNone(packet["active_work"])
        self.assertEqual(packet["finished_tasks"], ["inspect-roles"])
        self.assertEqual(self.manifest()["work_items"]["inspect-roles"]["status"], "done")
        self.assertTrue(packet["safe_to_continue"])

    def test_waiting_for_user_is_not_interpreted_as_permission_to_continue(self):
        self.init()
        self.work_update(status="waiting_user", pending_steps=["Wait for user to select design option"])
        packet = json.loads(self.cli("resume", "--format", "json").stdout)
        self.assertFalse(packet["safe_to_continue"])
        self.assertEqual(packet["active_work"]["status"], "waiting_user")

    def test_generic_checkpoint_cannot_clear_a_blocked_work_cursor(self):
        self.init()
        self.work_update(status="blocked", blockers=["Unavailable preview server"])
        blocked = self.cli("checkpoint", "--checkpoint-file", self.write_input("bad-checkpoint.json", {
            "summary": "Everything ready", "next_action": "Inspect the permission guard and record the behavior",
            "blockers": [],
        }), success=False)
        self.assertIn("cannot hide", blocked.stderr.lower())
        self.assertEqual(self.manifest()["work_items"]["inspect-roles"]["status"], "blocked")
        self.cli("validate")

    def test_lost_active_cursor_is_not_mistaken_for_finished_work_after_interruption(self):
        self.init()
        self.work_update()
        manifest = self.manifest()
        manifest["active_work_id"] = None
        (self.initiative / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        packet = json.loads(self.cli("resume", "--format", "json", success=False).stdout)
        self.assertFalse(packet["safe_to_continue"])
        self.assertIn("unfinished work", " ".join(packet["validation_errors"]).lower())

    def test_journal_catches_accidentally_reverted_task_progress(self):
        self.init()
        self.work_update()
        self.work_update(completed_steps=["Identify invoice route", "Inspect permission guard"],
                         pending_steps=["Inspect error route"])
        manifest = self.manifest()
        manifest["work_items"]["inspect-roles"]["revision"] = 1
        (self.initiative / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        packet = json.loads(self.cli("resume", "--format", "json", success=False).stdout)
        self.assertIn("journal", " ".join(packet["validation_errors"]).lower())

    def test_approval_is_hash_locked_and_can_be_resumed(self):
        self.init()
        self.approve(self.approval())
        self.cli("validate")
        self.assertEqual(self.manifest()["approved_version"], "v1")
        self.assertEqual(self.manifest()["stage"], "approved")
        locked = self.manifest()["approvals"]["decision-v1"]["artifact_hashes"]
        self.assertEqual(len(locked), 7)
        self.assertTrue(all(len(digest) == 64 for digest in locked.values()))
        self.assertIn("v1", self.cli("resume").stdout)

    def test_new_prototype_cannot_be_approved_before_a_to_b_coverage(self):
        self.init()
        approval = self.approval()
        result = self.cli(
            "approve", "--approval-file", self.write_input("uncovered.json", approval),
            success=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("functionality coverage", result.stderr.lower())

    def test_discovery_record_requires_real_source_and_a_zero_new_sweep(self):
        self.init()
        audit = self.discovery_audit()
        audit["sweeps"][-1]["new_feature_ids"] = ["invoice-search"]
        blocked = self.cli(
            "discovery-record", "--audit-file", self.write_input("audit.json", audit),
            success=False,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("zero new", blocked.stderr.lower())

    def test_new_ui_discovery_requires_a_preserved_original_or_an_explicit_gap(self):
        self.init(legacy=False)
        audit = self.discovery_audit()
        rejected = self.cli("discovery-record", "--audit-file", self.write_input("audit.json", audit), success=False)
        self.assertIn("Record the selected original visual baseline IDs", rejected.stderr)
        route = "/invoices"
        gap = {"baseline_id": "invoices-original", "baseline_feature_ids": ["invoice-search"],
               "source_paths": ["src/invoices.ts"], "reason": "Invoice search default",
               "essential": True, "unavailable_reason": "The original data fixture is unavailable",
               "state": {"route": route, "theme": "light", "data": "invoice fixture",
                         "interaction": "default", "active_control": None, "open_menus": []},
               "renderCase": {"route": route, "viewport": {"width": 390, "height": 844},
                              "ready_selector": "#invoices"}}
        self.cli("visual-baseline", "--action", "gap", "--input-file", self.write_input("gap.json", gap))
        audit["visual_baseline_ids"] = ["invoices-original"]
        self.discovery_record(audit)
        self.assertEqual(self.manifest()["active_discovery_id"], "audit-v1")
        self.assertIn("visual_baseline_build", self.manifest()["discovery_audits"]["audit-v1"])

    def test_discovery_record_is_durable_and_hash_protected(self):
        self.init()
        self.discovery_record(self.discovery_audit())
        self.assertEqual(self.manifest()["active_discovery_id"], "audit-v1")
        self.cli("validate")
        self.artifact("discovery/evidence/invoices.png", png(width=390, height=844, color=(0, 0, 0)))
        blocked = self.cli("validate", success=False)
        self.assertIn("discovery", blocked.stderr.lower())

    def test_prototype_coverage_rejects_unmapped_a_features(self):
        self.init()
        self.discovery_record(self.discovery_audit())
        approval = self.approval()
        parity = self.prototype_coverage(approval, features=())
        blocked = self.cli(
            "prototype-coverage", "--coverage-file", self.write_input("parity.json", parity),
            success=False,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("unmapped", blocked.stderr.lower())

    def test_verified_prototype_parity_allows_approval_and_is_hashed(self):
        self.init()
        self.discovery_record(self.discovery_audit())
        approval = self.approval()
        self.parity_record(self.prototype_coverage(approval))
        self.approve(approval)
        self.cli("validate")
        proof = "coverage/evidence/v1/invoice-search.json"
        self.artifact(proof, b"tampered")
        blocked = self.cli("validate", success=False)
        self.assertIn("functionality", blocked.stderr.lower())

    def test_postapproval_missing_feature_blocks_without_erasing_user_decision(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        original = self.manifest()["approvals"]["decision-v1"]
        self.missing_report()
        after = self.manifest()
        self.assertEqual(after["approvals"]["decision-v1"], original)
        self.assertEqual(after["approved_version"], "v1")
        self.assertTrue(after["coverage_pending"])
        blocked = self.cli("validate", success=False)
        self.assertIn("functionality", blocked.stderr.lower())

    def test_missing_feature_requires_wider_sweep_and_new_version(self):
        self.init()
        first = self.approval()
        self.approve(first)
        self.missing_report()
        expanded = self.discovery_audit("audit-v2", include_note=True)
        expanded["sweeps"][-1]["new_feature_ids"] = ["invoice-note"]
        blocked = self.cli("discovery-record", "--audit-file",
                           self.write_input("expanded.json", expanded), success=False)
        self.assertIn("zero new", blocked.stderr.lower())
        expanded["sweeps"][-1]["new_feature_ids"] = []
        self.discovery_record(expanded)
        self.assertTrue(self.manifest()["coverage_pending"])
        blocked = self.cli("validate", success=False)
        self.assertIn("functionality", blocked.stderr.lower())
        # The original A behavior is still mandatory in a new prototype.
        second = self.approval("v2")
        self.parity_record(self.prototype_coverage(second, features=("invoice-search", "invoice-note")))
        self.approve(second)
        self.assertFalse(self.manifest()["coverage_pending"])
        self.assertEqual(set(self.manifest()["approvals"]), {"decision-v1", "decision-v2"})
        self.cli("validate")

    def test_no_new_feature_in_one_full_sweep_does_not_silently_dismiss_user_report(self):
        self.init()
        self.approve(self.approval())
        self.missing_report()
        followup = self.discovery_audit("audit-v2")
        followup["sweeps"] = [followup["sweeps"][-1]]
        self.discovery_record(followup)
        self.assertTrue(self.manifest()["coverage_pending"])
        self.assertEqual(self.manifest()["approved_version"], "v1")
        self.assertIn("needs a complete follow-up sweep", self.cli("validate", success=False).stderr)

    def test_one_full_clean_sweep_and_explicit_user_correction_resume_same_design(self):
        self.init()
        self.approve(self.approval())
        self.missing_report()
        self.artifact("discovery/evidence/resolution.json", b'{"verified_a_actions": ["search"], "new_actions": []}')
        followup = self.discovery_audit("audit-v2")
        followup["sweeps"] = [followup["sweeps"][-1]]
        followup["missing_report_resolutions"] = [{
            "report_id": "gap-001", "resolution": "not_missing",
            "confirmed_by_user": True, "user_note": "I checked and retract the reported gap",
            "evidence_paths": ["discovery/evidence/resolution.json"],
        }]
        self.discovery_record(followup)
        self.assertFalse(self.manifest()["coverage_pending"])
        self.assertEqual(self.manifest()["approved_version"], "v1")
        self.assertEqual(self.manifest()["missing_reports"]["gap-001"]["status"], "dismissed")
        self.cli("validate")

    def test_pending_report_cannot_be_bypassed_by_a_new_version_without_resweep(self):
        self.init()
        self.approve(self.approval())
        self.missing_report()
        next_approval = self.approval("v2")
        rejected_parity = self.cli(
            "prototype-coverage", "--coverage-file",
            self.write_input("coverage-v2.json", self.prototype_coverage(next_approval)),
            success=False,
        )
        self.assertIn("full follow-up a sweep", rejected_parity.stderr.lower())
        blocked = self.cli("approve", "--approval-file",
                           self.write_input("next.json", next_approval), success=False)
        self.assertIn("functionality coverage is missing", blocked.stderr.lower())

    def test_postapproval_missing_evidence_is_hash_protected(self):
        self.init()
        self.approve(self.approval())
        self.missing_report()
        self.artifact("discovery/evidence/missing-feature.png", b"changed")
        blocked = self.cli("validate", success=False)
        self.assertIn("changed protected evidence", blocked.stderr.lower())

    def test_postapproval_missing_feature_stops_new_slice_assignments(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.missing_report()
        blocked = self.cli("slice-create", "--contract-file",
                           self.write_input("slice.json", self.slice_packet(approval)), success=False)
        self.assertIn("functionality", blocked.stderr.lower())

    def test_new_discovery_cannot_rewrite_previous_a_behavior(self):
        self.init()
        self.discovery_record(self.discovery_audit())
        changed = self.discovery_audit("audit-v2")
        changed["features"][0]["behavior"] = "Rewrite prior behavior silently"
        blocked = self.cli("discovery-record", "--audit-file",
                           self.write_input("changed.json", changed), success=False)
        self.assertIn("overwrite", blocked.stderr.lower())

    def test_reaudit_keeps_original_a_source_proof_when_production_code_was_replaced(self):
        self.init()
        self.discovery_record(self.discovery_audit())
        self.approve(self.approval())
        self.missing_report()
        audit = self.discovery_audit("audit-v2")
        audit["sweeps"] = [audit["sweeps"][-1]]
        (self.root / "src/invoices.ts").write_text(
            "export function redesignedInvoiceView() { return 'current production'; }\n",
            encoding="utf-8",
        )
        self.discovery_record(audit)
        self.assertEqual(self.manifest()["active_discovery_id"], "audit-v2")
        self.assertTrue(self.manifest()["coverage_pending"])

    def test_source_approval_without_reproducible_visual_evidence_is_blocked(self):
        self.init()
        approval = self.approval(replay=False)
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("replay", rejected.stderr.lower())
        self.assertIsNone(self.manifest()["approved_version"])

    def test_replay_evidence_with_visible_qa_controls_cannot_be_approved(self):
        self.init()
        approval = self.approval()
        approval["replay"] = self.replay_proof(approval, controls_visible=True)
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("qa", rejected.stderr.lower())

    def test_replay_evidence_must_cover_all_locked_visual_cases(self):
        self.init()
        approval = self.approval()
        approval["replay"] = self.replay_proof(approval, cases=[])
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("replay", rejected.stderr.lower())

    def test_malformed_replay_case_reports_clean_blocker_instead_of_crashing(self):
        self.init()
        approval = self.approval()
        approval["replay"] = self.replay_proof(approval, cases=[{"case_id": []}])
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("BLOCKED:", rejected.stderr)
        self.assertNotIn("Traceback", rejected.stderr)

    def test_replay_script_and_report_become_hash_locked(self):
        self.init()
        approval = self.approval()
        approval["replay"] = self.replay_proof(approval)
        self.approve(approval)
        self.cli("validate")
        self.artifact(approval["replay"]["script"], b"changed fixture setup")
        rejected = self.cli("validate", success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("sha256", rejected.stderr.lower())

    def test_replay_approval_rejects_missing_real_capture(self):
        self.init()
        approval = self.approval()
        report_path = self.initiative / approval["replay"]["evidence_file"]
        report = json.loads(report_path.read_text())
        report["cases"][0]["first_capture"] = "evidence/v1/missing-run.png"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("capture", rejected.stderr.lower())

    def test_replay_approval_rejects_different_capture_pixels(self):
        self.init()
        approval = self.approval()
        self.artifact("evidence/v1/invoices-desktop-run2.png", png(color=(90, 91, 92)))
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("capture", rejected.stderr.lower())

    def test_replay_captures_are_hash_locked_after_approval(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.artifact("evidence/v1/invoices-desktop-run2.png", png(color=(90, 91, 92)))
        rejected = self.cli("validate", success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("sha256", rejected.stderr.lower())

    def test_missing_source_or_reference_blocks_approval(self):
        self.init()
        approval = self.approval()
        (self.initiative / approval["source_paths"][0]).unlink()
        rejection = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejection.returncode, 0)
        self.assertIsNone(self.manifest()["approved_version"])
        self.artifact(approval["source_paths"][0], b"ready")
        (self.initiative / approval["visual_cases"][0]["reference_file"]).unlink()
        rejection = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejection.returncode, 0)
        self.assertIsNone(self.manifest()["approved_version"])

    def test_reference_screenshot_dimensions_must_match_approved_viewport(self):
        self.init()
        approval = self.approval()
        self.artifact(approval["visual_cases"][0]["reference_file"], png(width=1280))
        failure = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("viewport", failure.stderr)

    def test_mutated_approved_screenshot_fails_validation_and_resume(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.artifact(approval["visual_cases"][0]["reference_file"], b"different image")
        result = self.cli("validate", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("sha256", result.stderr.lower())
        self.assertIn("BLOCKED", self.cli("resume", success=False).stdout)

    def test_image_only_fallback_requires_explicit_user_exception(self):
        self.init()
        approval = self.approval(mode="image-only", source=False)
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        approval.update({"image_only_approved": True, "image_only_reason": "Temporary preview source lost; recreate from locked PNG"})
        self.approve(approval)
        self.assertEqual(self.manifest()["approvals"]["decision-v1"]["mode"], "image-only")

    def test_superseding_and_revoking_preserves_old_approval_events(self):
        self.init()
        self.approve(self.approval("v1"))
        self.approve(self.approval("v2"))
        manifest = self.manifest()
        self.assertEqual(manifest["approved_version"], "v2")
        self.assertEqual(set(manifest["approvals"]), {"decision-v1", "decision-v2"})
        approvals = [event for event in self.events() if event["type"] == "approval"]
        self.assertEqual(len(approvals), 2)
        self.assertEqual(approvals[1]["supersedes"], "decision-v1")
        self.cli("revoke-approval", "--reason", "User withdrew v2 approval")
        self.assertIsNone(self.manifest()["approved_version"])
        self.assertEqual(self.manifest()["stage"], "prototype")
        self.assertEqual([event["type"] for event in self.events()
                          if event["type"] in {"approval", "approval_revoked"}],
                         ["approval", "approval", "approval_revoked"])

    def test_approval_requires_user_decision_and_new_version_id(self):
        self.init()
        approval = self.approval()
        approval["approved_by_user"] = False
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        approval["approved_by_user"] = True
        self.approve(approval)
        rejected = self.cli("approve", "--approval-file", self.write_input("approve.json", approval), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(len([event for event in self.events() if event["type"] == "approval"]), 1)

    def test_slice_packet_is_reproducible_without_chat(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        packet = json.loads((self.initiative / "slices/invoice-list/contract.json").read_text())
        self.assertEqual(packet["visual_cases"], approval["visual_cases"])
        self.assertEqual(packet["prototype_source_paths"], approval["source_paths"])
        self.assertEqual(packet["status"], "ready")
        self.assertIn("approved_artifact_hashes", packet)
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["owner"], "agent-1")

    def test_whole_app_redesign_allows_narrower_page_or_feature_slices(self):
        self.init(scope="app")
        approval = self.approval()
        approval["scope"] = "app"
        self.approve(approval)
        page_packet = self.slice_packet(approval)
        page_packet["scope"] = "page"
        self.slice_create(page_packet)
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "ready")

    def test_slice_rejects_unknown_version_and_unapproved_visual_case(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        wrong = self.slice_packet(approval)
        wrong["prototype_version"] = "v9"
        self.assertNotEqual(self.slice_create(wrong, success=False).returncode, 0)
        wrong = self.slice_packet(approval)
        wrong["visual_cases"][0]["viewport"]["width"] = 1280
        self.assertNotEqual(self.slice_create(wrong, success=False).returncode, 0)
        wrong = self.slice_packet(approval)
        wrong["visual_cases"] = []
        self.assertNotEqual(self.slice_create(wrong, success=False).returncode, 0)
        self.assertEqual(self.manifest()["slices"], {})

    def test_slice_rejects_duplicate_id_and_overlapping_file_ownership(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        duplicate = self.slice_packet(approval)
        self.assertNotEqual(self.slice_create(duplicate, success=False).returncode, 0)
        conflicting = self.slice_packet(approval, slice_id="invoice-details", paths=["src/features/invoices/details.tsx"])
        self.assertNotEqual(self.slice_create(conflicting, success=False).returncode, 0)
        independent = self.slice_packet(approval, slice_id="customer-details", paths=["src/features/customers/**"])
        self.slice_create(independent)
        self.assertEqual(len(self.manifest()["slices"]), 2)

    def test_cancel_slice_preserves_history_and_frees_its_owned_paths(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.cli("slice-cancel", "--slice-id", "invoice-list", "--reason", "User removed this implementation task")
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "cancelled")
        self.assertEqual(self.events()[-1]["type"], "slice_cancelled")
        replacement = self.slice_packet(approval, slice_id="invoice-list-replacement")
        self.slice_create(replacement)
        self.assertEqual(self.manifest()["slices"]["invoice-list-replacement"]["status"], "ready")
        self.assertTrue((self.initiative / "slices/invoice-list/contract.json").exists())

    def test_worker_reports_do_not_modify_global_manifest_or_approval(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        before = (self.initiative / "manifest.json").read_bytes()
        report = {
            "slice_id": "invoice-list", "owner": "agent-1", "status": "submitted",
            "summary": "Implemented invoice table", "implementation_paths": ["src/features/invoices/table.tsx"],
            "functional_evidence": ["pytest invoices: PASS"], "visual_evidence": [], "shared_ui_paths": [],
            "blockers": [], "next_action": "Coordinator visual review",
        }
        report_file = self.write_input("worker.json", report)
        self.cli("slice-report", "--slice-id", "invoice-list", "--owner", "agent-1", "--report-file", report_file)
        self.assertEqual((self.initiative / "manifest.json").read_bytes(), before)
        self.assertTrue((self.initiative / "slices/invoice-list/worker-report.json").exists())
        forged = {**report, "approved_version": "v999"}
        rejected = self.cli("slice-report", "--slice-id", "invoice-list", "--owner", "agent-1",
                            "--report-file", self.write_input("forged.json", forged), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual((self.initiative / "manifest.json").read_bytes(), before)
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "report")
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "reported")

    def test_dependency_not_accepted_blocks_dispatch(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        dependent = self.slice_packet(approval, slice_id="invoice-export", paths=["src/features/export/**"])
        dependent["dependencies"] = ["invoice-list"]
        self.slice_create(dependent)
        self.assertEqual(self.manifest()["slices"]["invoice-export"]["status"], "blocked")
        early_report = {
            "slice_id": "invoice-export", "owner": "agent-1", "status": "submitted",
            "summary": "Should not have started", "implementation_paths": [],
        }
        refused = self.cli("slice-report", "--slice-id", "invoice-export", "--owner", "agent-1",
                           "--report-file", self.write_input("early.json", early_report), success=False)
        self.assertNotEqual(refused.returncode, 0)

    def test_accepting_dependency_unblocks_ready_slice_contract(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        dependent = self.slice_packet(approval, slice_id="invoice-export", paths=["src/features/export/**"])
        dependent["dependencies"] = ["invoice-list"]
        self.slice_create(dependent)
        self.submit_slice()
        review = self.review(approval)
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify", "--review-file", self.write_input("review.json", review))
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept")
        self.assertEqual(self.manifest()["slices"]["invoice-export"]["status"], "ready")
        packet = json.loads((self.initiative / "slices/invoice-export/contract.json").read_text())
        self.assertEqual(packet["status"], "ready")
        self.cli("validate")

    def test_acceptance_is_impossible_without_independent_visual_review(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        rejected = self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept", success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "reported")

    def test_missing_or_unavailable_visual_proof_cannot_pass(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        missing = self.review(approval, screenshot=False)
        rejected = self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                            "--review-file", self.write_input("review.json", missing), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotEqual(self.manifest()["slices"]["invoice-list"]["status"], "accepted")
        unavailable = self.review(approval, screenshot=True, tool="unavailable", result="BLOCKED")
        rejected = self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                            "--review-file", self.write_input("review.json", unavailable), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotEqual(self.manifest()["slices"]["invoice-list"]["status"], "accepted")

    def test_end_to_end_verification_then_accept_and_detect_later_code_drift(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        review = self.review(approval)
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("review.json", review))
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "verified")
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept")
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "accepted")
        self.cli("validate")
        attempts = json.loads(self.cli("visual-integrity", "--action", "list",
                                       "--slice-id", "invoice-list").stdout)
        image = self.initiative / attempts[-1]["cases"][0]["full_screenshot"]
        original_image = image.read_bytes()
        image.write_bytes(b"tampered finalization image")
        integrity_failure = self.cli("validate", success=False)
        self.assertIn("visual integrity", integrity_failure.stderr.lower())
        image.write_bytes(original_image)
        self.cli("validate")
        (self.root / "src/features/invoices/table.tsx").write_text("changed after review", encoding="utf-8")
        rejected = self.cli("validate", success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("sha256", rejected.stderr.lower())

    def test_post_implementation_visual_integrity_is_required_before_slice_verification(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        review = self.review(approval, record_integrity=False)

        rejected = self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                            "--review-file", self.write_input("review.json", review), success=False)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("visual integrity", rejected.stderr.lower())
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "blocked")

    def test_unresolved_visual_integrity_blocks_only_the_slice_and_its_dependents(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        dependent = self.slice_packet(approval, slice_id="invoice-export", paths=["src/features/export/**"])
        dependent["dependencies"] = ["invoice-list"]
        self.slice_create(dependent)
        independent = self.slice_packet(approval, slice_id="invoice-history", paths=["src/features/history/**"])
        self.slice_create(independent)
        self.submit_slice()
        review = self.review(approval)
        self.visual_integrity_attempt(approval, status="unresolved")
        rejected = self.cli("reconcile", "--slice-id", "invoice-list", "--action", "unblock", "--review-file",
                            self.write_input("resolution.json", {"slice_id": "invoice-list",
                                "resolution": "Ready to try again", "next_action": "Recheck"}), success=False)
        self.assertIn("Visual integrity is unresolved", rejected.stderr)

        rejected = self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                            "--review-file", self.write_input("review.json", review), success=False)

        self.assertIn("visual integrity", rejected.stderr.lower())
        states = self.manifest()["slices"]
        self.assertEqual(states["invoice-list"]["status"], "blocked")
        self.assertEqual(states["invoice-export"]["status"], "blocked")
        self.assertEqual(states["invoice-history"]["status"], "ready")

    def test_visual_gate_rejects_false_repairs_then_verifies_marked_change(self):
        from PIL import Image

        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        self.review(approval, record_integrity=False)
        self.visual_integrity_attempt(approval, status="unresolved")
        attempt = json.loads((self.root / "visual-integrity.json").read_text())
        case = attempt["cases"][0]
        finding = attempt["findings"][0]
        pair = case["comparisons"][0]
        args = ("visual-integrity", "--slice-id", "invoice-list")

        def record(expected_error=None):
            file = self.write_input("repair.json", attempt)
            result = self.cli(*args, "--action", "record", "--attempt-file", file, success=expected_error is None)
            if expected_error:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertNotEqual(self.cli(*args, "--action", "verify", success=False).returncode, 0)

        attempt["status"] = "corrected"
        attempt["rechecked_findings"] = [finding["finding_id"]]
        attempt["observations"][0]["result"] = "pass"
        finding.update(status="fixed", dimension="interaction", correction="Adjusted the column width",
                       verification="Rechecked the marked column at the approved viewport")
        record("original finding dimension")
        finding["dimension"] = "geometry"
        original = self.initiative / case["full_screenshot"]
        case["full_screenshot"] = str(Path(case["full_screenshot"]).with_name("rechecked-full.png"))
        case["closeup_screenshot"] = str(Path(case["closeup_screenshot"]).with_name("rechecked-close.png"))
        pair["latest"] = case["closeup_screenshot"]
        with Image.open(original) as page:
            page.putpixel((0, 0), (0, 0, 0))
            page.save(self.initiative / case["full_screenshot"])
            page.save(self.initiative / case["closeup_screenshot"])
            record("component bounds")
            page.crop((0, 0, 64, 32)).save(self.initiative / case["closeup_screenshot"])
            record("marked regions")
            # Move the control: only its new marked location changes.
            case["closeup_bounds"]["x"] = 80
            page.putpixel((112, 16), (0, 0, 0))
            page.save(self.initiative / case["full_screenshot"])
            page.crop((80, 0, 144, 32)).save(self.initiative / case["closeup_screenshot"])
        from fixtures.visual_capture import capture_fixture_case
        capture_fixture_case(self.initiative, "invoice-list", case)
        record()
        self.cli(*args, "--action", "verify")

    def test_responsive_finding_requires_visible_correction_even_with_fresh_capture(self):
        from fixtures.visual_capture import capture_fixture_case

        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        self.review(approval, record_integrity=False)
        self.visual_integrity_attempt(approval, status="unresolved", dimension="responsive")
        attempt = json.loads((self.root / "visual-integrity.json").read_text())
        attempt.update(status="corrected", rechecked_findings=["geometry-mismatch"])
        attempt["findings"][0].update(status="fixed", correction="Widened the menu", verification="Rechecked clipping")
        for item in attempt["observations"]:
            item["result"] = "pass"
        capture_fixture_case(self.initiative, "invoice-list", attempt["cases"][0])
        result = self.cli("visual-integrity", "--action", "record", "--slice-id", "invoice-list",
                          "--attempt-file", self.write_input("repair.json", attempt), success=False)
        self.assertIn("identical image pixels in the marked regions", result.stderr)
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "blocked")

    def test_capture_receipt_and_incomplete_history_through_cli(self):
        from functools import partial
        from http.server import ThreadingHTTPServer
        import shutil
        from threading import Thread
        from fixtures.blueprint_browser_probe import ItemsHandler

        browser = shutil.which("google-chrome") or shutil.which("chromium")
        if not browser:
            self.skipTest("Chromium is unavailable")
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        self.review(approval, record_integrity=False)
        self.visual_integrity_attempt(approval)
        attempt = json.loads((self.root / "visual-integrity.json").read_text())
        case = attempt["cases"][0]
        args = ("visual-integrity", "--slice-id", "invoice-list")
        case.pop("capture_id")
        rejected = self.cli(*args, "--action", "record", "--attempt-file",
                            self.write_input("generated.json", attempt), success=False)
        self.assertIn("matching browser capture", rejected.stderr)
        (self.root / "invoices.html").write_text(
            "<details><summary>Open menu</summary><p>Classic</p></details>")

        class MenuHandler(ItemsHandler):
            def do_GET(self):
                if self.path == "/invoices":
                    self.path = "/invoices.html"
                super().do_GET()

        with ThreadingHTTPServer(("127.0.0.1", 0), partial(MenuHandler, directory=self.root)) as server:
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                request = {"case_id": case["case_id"], "state": case["state"], "browser": browser,
                           "appUrl": f"http://127.0.0.1:{server.server_port}",
                           "interaction_assertions": [{"selector": "details[open] p", "probe": "text", "expected": "Classic"}],
                           "renderCase": {"route": "/invoices", "viewport": case["viewport"],
                                          "ready_selector": "details[open]",
                                          "setup": [{"action": "click", "selector": "summary"}]}}
                source = self.write_input("capture-request.json", request)
                captured = json.loads(self.cli(*args, "--action", "capture", "--capture-file", source).stdout)
            finally:
                server.shutdown()
        attempt["cases"] = [captured]
        file = self.write_input("captured.json", attempt)
        self.cli(*args, "--action", "record", "--attempt-file", file)
        self.cli(*args, "--action", "verify")
        self.assertIn("fresh browser capture", self.cli(*args, "--action", "record", "--attempt-file", file,
                                                       success=False).stderr)
        self.visual_integrity_attempt(approval, status="unresolved")
        journal = self.initiative / "visual-integrity/invoice-list/attempts.jsonl"
        journal.write_bytes(b"".join(journal.read_bytes().splitlines(keepends=True)[:-1]))
        listed = self.cli(*args, "--action", "list", success=False)
        self.assertNotEqual(listed.returncode, 0)
        self.assertIn("incomplete visual history", listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)), 2)
        self.assertIn("head", self.cli(*args, "--action", "verify", success=False).stderr)
        from urllib.request import urlopen
        command = [sys.executable, str(SCRIPT), "review-viewer", "--project-root", str(self.root),
                   "--initiative-id", "billing-refresh", "--port", "0"]
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=self.test_env) as viewer:
            try:
                address = viewer.stdout.readline().strip().split()[-1]
                with urlopen(address + "visual-integrity", timeout=10) as response:
                    register = json.load(response)
                self.assertEqual(len(register["attempts"]), 2)
                self.assertIn("incomplete visual history", register["project"]["history_warnings"][0])
            finally:
                viewer.terminate()


    def test_tampered_visual_integrity_image_blocks_validate_and_dependent_dispatch(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        dependent = self.slice_packet(approval, slice_id="invoice-export", paths=["src/features/export/**"])
        dependent["dependencies"] = ["invoice-list"]
        self.slice_create(dependent)
        self.submit_slice()
        review = self.review(approval)
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("review.json", review))
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept")

        attempts = json.loads(self.cli("visual-integrity", "--action", "list",
                                       "--slice-id", "invoice-list").stdout)
        image = self.initiative / attempts[-1]["cases"][0]["full_screenshot"]
        resumed = json.loads(self.cli("resume", "--format", "json").stdout)
        self.assertIn("invoice-export", resumed["dispatchable_slices"])
        self.assertNotIn("invoice-list", resumed["dispatchable_slices"])
        image.write_bytes(b"tampered visual evidence")

        invalid = self.cli("validate", success=False)
        resumed = json.loads(self.cli("resume", "--format", "json", success=False).stdout)

        self.assertIn("visual integrity", invalid.stderr.lower())
        self.assertNotIn("invoice-export", resumed["dispatchable_slices"])

    def test_self_review_or_failed_functional_check_blocks_acceptance(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        review = self.review(approval)
        review["reviewer"] = "agent-1"
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                                     "--review-file", self.write_input("review.json", review), success=False).returncode, 0)
        review["reviewer"] = "coordinator"
        review["functional_evidence"][0]["result"] = "FAIL"
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                                     "--review-file", self.write_input("review.json", review), success=False).returncode, 0)

    def test_invalid_screenshot_bytes_cannot_count_as_visual_proof(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        review = self.review(approval)
        self.artifact("slices/invoice-list/evidence/implemented.png", b"\x89PNG\r\n\x1a\nnot actually PNG")
        rejected = self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                            "--review-file", self.write_input("review.json", review), success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("PNG", rejected.stderr)

    def test_three_reviewed_capture_retries_allow_provisional_progress_not_acceptance(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        dependent = self.slice_packet(approval, slice_id="invoice-export", paths=["src/features/export/**"])
        dependent["dependencies"] = ["invoice-list"]
        self.slice_create(dependent)
        self.submit_slice()
        review = self.warning_review(approval)
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("warning-review.json", review))
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "provisional")
        self.assertEqual(self.manifest()["slices"]["invoice-export"]["status"], "ready")
        checkpoint = (self.initiative / "CHECKPOINT.md").read_text(encoding="utf-8")
        self.assertIn("Capture warnings needing user review", checkpoint)
        self.assertIn("invoices-desktop (provisional, NOT accepted)", checkpoint)
        recorded = json.loads((self.initiative / "slices/invoice-list/verification.json").read_text())
        self.assertEqual(recorded["status"], "WARNING")
        self.assertEqual(recorded["warning_case_ids"], ["invoices-desktop"])
        self.assertEqual(len(recorded["visual_evidence"][0]["capture_warning"]["retry_screenshots"]), 3)
        warning_gallery = recorded.get("warning_gallery_file")
        self.assertIsInstance(warning_gallery, str)
        gallery_html = (self.initiative / warning_gallery).read_text(encoding="utf-8")
        self.assertIn("Before / approved prototype", gallery_html)
        self.assertIn("After / implemented page", gallery_html)
        self.assertIn("retry-3.png", gallery_html)
        self.assertIn("implemented.png", gallery_html)
        self.assertIn(warning_gallery, recorded["evidence_hashes"])
        self.assertIn(warning_gallery, (self.initiative / "CHECKPOINT.md").read_text())
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list",
                                     "--action", "accept", success=False).returncode, 0)
        self.cli("validate")

    def test_capture_warning_rejects_missing_retries_and_real_ui_defects(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        review = self.warning_review(approval)
        review["visual_evidence"][0]["capture_warning"]["retry_screenshots"].pop()
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                                     "--review-file", self.write_input("warning-review.json", review),
                                     success=False).returncode, 0)
        review = self.warning_review(approval)
        diagnosis = self.initiative / review["visual_evidence"][0]["capture_warning"]["diagnosis_file"]
        details = json.loads(diagnosis.read_text())
        details["visible_ui_defect"] = True
        diagnosis.write_text(json.dumps(details), encoding="utf-8")
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                                     "--review-file", self.write_input("warning-review.json", review),
                                     success=False).returncode, 0)

    def test_capture_warning_rejects_wrong_comparison_scope_and_reused_contexts(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        review = self.warning_review(approval)
        diagnosis = self.initiative / review["visual_evidence"][0]["capture_warning"]["diagnosis_file"]
        details = json.loads(diagnosis.read_text())
        details["comparison_scope"] = "unrelated_a_to_b"
        diagnosis.write_text(json.dumps(details), encoding="utf-8")
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                                     "--review-file", self.write_input("warning-review.json", review),
                                     success=False).returncode, 0)
        details["comparison_scope"] = "approved_to_implementation"
        details["retry_context_ids"] = ["same-context"] * 3
        diagnosis.write_text(json.dumps(details), encoding="utf-8")
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                                     "--review-file", self.write_input("warning-review.json", review),
                                     success=False).returncode, 0)

    def test_provisional_warning_requires_later_explicit_user_review_and_keeps_hashes_locked(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        review = self.warning_review(approval)
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("warning-review.json", review))
        rejected = {
            "slice_id": "invoice-list", "approved_by_user": False,
            "warning_case_ids": ["invoices-desktop"], "user_note": "I reviewed the capture warning.",
        }
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept-warning",
                                     "--review-file", self.write_input("user-decision.json", rejected),
                                     success=False).returncode, 0)
        accepted = {**rejected, "approved_by_user": True,
                    "approval_id": self.manifest()["active_approval_id"]}
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept-warning",
                 "--review-file", self.write_input("user-decision.json", accepted))
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "accepted")
        self.cli("validate")
        verification_path = self.initiative / "slices/invoice-list/verification.json"
        saved = json.loads(verification_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "WARNING_USER_ACCEPTED")
        saved["status"] = "PASS"
        verification_path.write_text(json.dumps(saved), encoding="utf-8")
        self.assertNotEqual(self.cli("validate", success=False).returncode, 0)
        saved["status"] = "WARNING_USER_ACCEPTED"
        verification_path.write_text(json.dumps(saved), encoding="utf-8")
        self.cli("validate")
        self.artifact("slices/invoice-list/evidence/retry-2.png", png(color=(10, 20, 30)))
        self.assertNotEqual(self.cli("validate", success=False).returncode, 0)

    def warning_packet(self, disposition="needs_user_review"):
        """Three genuine, separately named comparisons plus an investigation file."""
        pairs = []
        for index in range(1, 4):
            original = f"slices/invoice-list/evidence/unchanged-before-{index}.png"
            observed = f"slices/invoice-list/evidence/unchanged-after-{index}.png"
            self.artifact(original, png(width=390, height=844, color=(240, 240, 240)))
            self.artifact(observed, png(width=390, height=844, color=(241, 240, 240)))
            pairs.append({
                "case_id": "unchanged-invoice-mobile", "attempt_id": f"fresh-{index}",
                "reference_file": original, "actual_file": observed, "different_pixels": 10,
            })
        diagnosis = "slices/invoice-list/evidence/capture-diagnosis.json"
        self.artifact(diagnosis, b'{"reason":"Color rounding only after navigation","layout_matches":true}')
        return {
            "warning_id": "unchanged-page-raster", "slice_id": "invoice-list",
            "comparison_scope": "original_before_to_original_after",
            "classification": "capture_instability",
            "disposition": disposition,
            "summary": "Ten channel-level pixels differ without a layout change",
            "reviewer": "independent-qa", "reviewer_notes": "Checked all three pairs, text and geometry",
            "retry_context_ids": ["fresh-1", "fresh-2", "fresh-3"],
            "comparison_pairs": pairs, "supporting_files": [diagnosis],
        }

    def test_coordinator_block_updates_official_status_and_checkpoint_without_accepting(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        evidence = "slices/invoice-list/evidence/dirty-cancel.json"
        self.artifact(evidence, b'{"model":"auditor","visible":"client"}')
        blocker = {
            "slice_id": "invoice-list", "reason": "Visible selector disagrees with retained model after Cancel",
            "next_action": "Fix dirty-cancel selection and rerun interaction checks",
            "evidence_files": [evidence],
        }
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "block",
                 "--review-file", self.write_input("blocker.json", blocker))
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "blocked")
        self.assertIn("visible selector", " ".join(self.manifest()["slices"]["invoice-list"]["blockers"]).lower())
        checkpoint = (self.initiative / "CHECKPOINT.md").read_text(encoding="utf-8")
        self.assertIn("Visible selector disagrees", checkpoint)
        self.assertIn("invoice-list: blocked", checkpoint)
        self.cli("validate")
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept",
                                     success=False).returncode, 0)
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "unblock",
                                     success=False).returncode, 0)
        resolution = {"slice_id": "invoice-list", "resolution": "Corrected select value and retested Cancel",
                      "next_action": "Re-run worker tests before verification"}
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "unblock",
                 "--review-file", self.write_input("resolution.json", resolution))
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "ready")
        self.cli("validate")
        self.artifact(evidence, b'{"model":"client","visible":"client"}')
        self.assertNotEqual(self.cli("validate", success=False).returncode, 0)

    def test_warning_registry_generates_side_by_side_gallery_and_protects_evidence(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        warning = self.warning_packet()
        self.cli("warning-record", "--warning-file", self.write_input("warning.json", warning))
        manifest = self.manifest()
        stored = manifest["warnings"]["unchanged-page-raster"]
        self.assertEqual(stored["status"], "pending_user_review")
        gallery = self.initiative / stored["gallery_file"]
        source = gallery.read_text(encoding="utf-8")
        self.assertIn("Before", source)
        self.assertIn("After", source)
        self.assertIn("Before / original baseline", source)
        self.assertIn("After / post-change capture", source)
        self.assertIn("unchanged-before-1.png", source)
        self.assertIn("unchanged-after-3.png", source)
        self.assertEqual(source.count("class=\"comparison-pair\""), 3)
        self.assertIn("unchanged-page-raster", (self.initiative / "CHECKPOINT.md").read_text())
        self.assertNotEqual(self.cli("warning-record", "--warning-file", self.write_input("warning.json", warning),
                                     success=False).returncode, 0)
        self.cli("validate")
        self.artifact(warning["comparison_pairs"][0]["actual_file"], png(width=390, height=844, color=(20, 20, 20)))
        self.assertNotEqual(self.cli("validate", success=False).returncode, 0)

    def test_malformed_warning_input_returns_clean_blocker(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        for field in ("slice_id", "comparison_scope", "disposition"):
            warning = self.warning_packet()
            warning[field] = ["invalid-list-instead-of-text"]
            result = self.cli("warning-record", "--warning-file", self.write_input("malformed-warning.json", warning),
                              success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("BLOCKED:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.manifest()["warnings"], {})

    def test_pending_supplemental_warning_blocks_acceptance_until_user_reviews_gallery(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("review.json", self.review(approval)))
        self.cli("warning-record", "--warning-file", self.write_input("warning.json", self.warning_packet()))
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept",
                                     success=False).returncode, 0)
        decision = {"warning_id": "unchanged-page-raster", "approval_id": self.manifest()["active_approval_id"],
                    "decision": "accept", "confirmed_by_user": False,
                    "user_note": "Reviewed the side-by-side evidence; artifact is insignificant"}
        self.assertNotEqual(self.cli("warning-decide", "--warning-id", "unchanged-page-raster",
                                     "--decision-file", self.write_input("decision.json", decision),
                                     success=False).returncode, 0)
        decision["confirmed_by_user"] = True
        self.cli("warning-decide", "--warning-id", "unchanged-page-raster",
                 "--decision-file", self.write_input("decision.json", decision))
        self.assertEqual(self.manifest()["warnings"]["unchanged-page-raster"]["status"], "accepted")
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept")
        self.cli("validate")
        decision_path = self.initiative / self.manifest()["warnings"]["unchanged-page-raster"]["decision_file"]
        decision_path.write_text('{"tampered":true}', encoding="utf-8")
        self.assertNotEqual(self.cli("validate", success=False).returncode, 0)

    def test_information_only_warning_is_preserved_but_does_not_block_acceptance(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("review.json", self.review(approval)))
        warning = self.warning_packet(disposition="informational")
        for item in warning["comparison_pairs"]:
            self.artifact(item["actual_file"], (self.initiative / item["reference_file"]).read_bytes())
            item["different_pixels"] = 0
        self.cli("warning-record", "--warning-file", self.write_input("warning.json", warning))
        self.assertEqual(self.manifest()["warnings"]["unchanged-page-raster"]["status"], "informational")
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept")
        self.cli("validate")

    def test_stale_worker_report_cannot_silently_erase_real_blocker(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        evidence = "slices/invoice-list/evidence/selector-state.json"
        self.artifact(evidence, b'{"visible":"wrong"}')
        blocker = {"slice_id": "invoice-list", "reason": "Unsaved draft selector stays wrong after Cancel",
                   "next_action": "Fix the selector", "evidence_files": [evidence]}
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "block",
                 "--review-file", self.write_input("blocker.json", blocker))
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "report",
                                     success=False).returncode, 0)
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "blocked")

    def test_rejected_visual_warning_never_allows_final_acceptance(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("review.json", self.review(approval)))
        self.cli("warning-record", "--warning-file", self.write_input("warning.json", self.warning_packet()))
        rejection = {"warning_id": "unchanged-page-raster", "approval_id": self.manifest()["active_approval_id"],
                     "decision": "reject", "confirmed_by_user": True,
                     "user_note": "The comparison may hide an actual regression"}
        self.cli("warning-decide", "--warning-id", "unchanged-page-raster",
                 "--decision-file", self.write_input("reject.json", rejection))
        self.assertEqual(self.manifest()["slices"]["invoice-list"]["status"], "blocked")
        resolution = {"slice_id": "invoice-list", "resolution": "Will reproduce and reverify",
                      "next_action": "Rerun browser checks"}
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "unblock",
                 "--review-file", self.write_input("resolution.json", resolution))
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "report")
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("review.json", self.review(approval)))
        self.assertNotEqual(self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept",
                                     success=False).returncode, 0)

    def test_new_real_blocker_holds_dependents_that_were_already_ready(self):
        self.init()
        approval = self.approval()
        self.approve(approval)
        self.slice_create(self.slice_packet(approval))
        self.submit_slice()
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "verify",
                 "--review-file", self.write_input("review.json", self.review(approval)))
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "accept")
        dependent = self.slice_packet(approval, slice_id="invoice-export", paths=["src/features/export/**"])
        dependent["dependencies"] = ["invoice-list"]
        self.slice_create(dependent)
        self.assertEqual(self.manifest()["slices"]["invoice-export"]["status"], "ready")
        path = "slices/invoice-list/evidence/new-regression.json"
        self.artifact(path, b'{"problem":"new regression"}')
        blocker = {"slice_id": "invoice-list", "reason": "New regression in a shared selector",
                   "next_action": "Fix and reverify all affected journeys", "evidence_files": [path]}
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "block",
                 "--review-file", self.write_input("blocker.json", blocker))
        manifest = self.manifest()
        self.assertEqual(manifest["slices"]["invoice-export"]["status"], "blocked")
        self.assertIn("Waiting for accepted dependency: invoice-list", manifest["slices"]["invoice-export"]["blockers"])
        self.cli("validate")


if __name__ == "__main__":
    unittest.main()
