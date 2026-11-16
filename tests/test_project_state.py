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
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--project-root", str(self.root),
             "--initiative-id", "billing-refresh", *map(str, extra)],
            text=True, capture_output=True, env=self.test_env,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def init(self):
        self.cli("init", "--scope", "page", "--next-action", "Map billing states")

    def write_input(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

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
            "functional_evidence": ["npm run test:billing"], "visual_evidence": [],
            "blockers": [], "next_action": "Independent verification",
        }
        self.cli("slice-report", "--slice-id", "invoice-list", "--owner", "agent-1",
                 "--report-file", self.write_input("report.json", report))
        self.cli("reconcile", "--slice-id", "invoice-list", "--action", "report")

    def review(self, approval, screenshot=True, tool="playwright", result="PASS"):
        if screenshot:
            self.artifact("slices/invoice-list/evidence/implemented.png", png(color=(245, 247, 252)))
        self.artifact("slices/invoice-list/evidence/functional.log", b"PASS search/filter/detail")
        code = self.root / "src/features/invoices/table.tsx"
        code.parent.mkdir(parents=True, exist_ok=True)
        code.write_text("export const InvoiceTable = () => null;", encoding="utf-8")
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
        self.assertEqual([event["type"] for event in self.events()], ["approval", "approval"])
        self.assertEqual(self.events()[1]["supersedes"], "decision-v1")
        self.cli("revoke-approval", "--reason", "User withdrew v2 approval")
        self.assertIsNone(self.manifest()["approved_version"])
        self.assertEqual(self.manifest()["stage"], "prototype")
        self.assertEqual([event["type"] for event in self.events()], ["approval", "approval", "approval_revoked"])

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
        self.assertEqual(len(self.events()), 1)

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
        self.cli("init", "--scope", "app")
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
            "functional_evidence": ["pytest invoices: PASS"], "visual_evidence": [],
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
        (self.root / "src/features/invoices/table.tsx").write_text("changed after review", encoding="utf-8")
        rejected = self.cli("validate", success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("sha256", rejected.stderr.lower())

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


if __name__ == "__main__":
    unittest.main()
