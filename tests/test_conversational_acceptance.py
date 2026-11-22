"""Release acceptance must fail closed when conversational evidence is missing."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "conversational_acceptance.py"
sys.path.insert(0, str(CLI.parent))

import conversational_acceptance as gate
from conversational_acceptance import (
    automatic_checks, checkpoints, evidence_hashes, fingerprint, required_scenarios, validate_review,
)


class ConversationalAcceptanceTests(unittest.TestCase):
    def test_release_requires_recorded_scenarios(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(CLI), "ready", directory],
                cwd=ROOT, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Missing passing conversation run", result.stderr)

    def test_release_cannot_use_a_self_reported_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "made-up-run"
            run.mkdir()
            (run / "run.json").write_text(json.dumps({
                "candidate": fingerprint(), "scenario": "redesign", "started_ns": 100,
            }))
            (run / "review.json").write_text('{"status":"pass","scenario":"redesign"}')
            result = subprocess.run(
                [sys.executable, str(CLI), "ready", directory],
                cwd=ROOT, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Missing passing conversation run", result.stderr)

    def test_run_with_a_turn_but_without_live_events_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "run.json").write_text('{"scenario":"vague"}')
            (run / "result.json").write_text('{"error":null,"turn_count":1}')
            (run / "events.jsonl").touch()
            (run / "turn-001.json").write_text(json.dumps({
                "number": 1, "turn_id": "fake", "thread": {"id": "fake"},
                "user": "Please look at our Git repo.", "messages": ["I followed the workflow."],
            }))
            self.assertEqual(automatic_checks(run), [
                "Turn 1 lacks matching completed live transcript events"
            ])

    def test_reviewer_cannot_downgrade_objective_failure_to_concern(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "run.json").write_text('{"scenario":"traceability"}')
            (run / "result.json").write_text('{"turn_count":1}')
            finding = {"type": "objective", "outcome": "concern", "turns": [1],
                       "observation": "The required records were missing."}
            with self.assertRaisesRegex(ValueError, "objective failures must block"):
                validate_review(run, {"reviewer": "Reviewer", "findings": {
                    "intake": finding, "traceability": finding,
                }})

    def test_first_release_requires_all_three_scenarios(self):
        self.assertEqual(required_scenarios(None), ["redesign", "traceability", "vague"])

    def test_same_source_id_in_two_letter_cases_counts_once(self):
        rows = [
            {"requirement_id": f"req-{number:02d}",
             "request_reference": f"DESIGN.md (REQ-{number:02d})",
             "finding": "Missing in the current report.", "direction": "Exercise this case."}
            for number in range(1, 51)
        ]
        state = {
            "phase": "discover", "guided_design_review": True,
            "pending_questions": [{
                "exchange_id": "checkpoint", "review_step": "initial_alignment",
                "requirement_mappings": rows,
            }],
        }
        turn = {"after": {"state": state, "git_head": "fixture", "git_status": ""},
                "messages": ["Is my understanding correct?"], "user": "Please investigate."}
        self.assertEqual(checkpoints([turn], {"git_head": "fixture"}, "traceability", set()), [])

    def test_edited_fixture_source_invalidates_review_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            repository = run / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            fixture = repository / "queue.csv"
            fixture.write_text("original input")
            subprocess.run(["git", "add", "queue.csv"], cwd=repository, check=True)
            original = evidence_hashes(run)
            fixture.write_text("changed input")
            self.assertNotEqual(original, evidence_hashes(run))

    def test_git_ref_alone_cannot_reduce_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(CLI), "ready", directory, "--baseline", "HEAD"],
                cwd=ROOT, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("previous ready release receipt", result.stderr)

    def test_release_revalidates_the_same_reviews_after_technical_checks(self):
        for change in ("none", "new run", "changed review"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                run = output / "first"
                run.mkdir()
                review = run / "review.json"
                review.write_text('{"status":"pass"}')
                (run / "run.json").write_text(json.dumps({
                    "candidate": "candidate", "scenario": "redesign", "started_ns": 1,
                }))

                def technical_check(*args, **kwargs):
                    if change == "new run":
                        newer = output / "retest"
                        newer.mkdir(exist_ok=True)
                        (newer / "run.json").write_text(json.dumps({
                            "candidate": "candidate", "scenario": "redesign", "started_ns": 2,
                        }))
                    elif change == "changed review":
                        review.write_text('{"status":"pass","observation":"Changed during verification"}')
                    return SimpleNamespace(returncode=0)

                with patch.object(gate, "required_scenarios", return_value=["redesign"]), \
                        patch.object(gate, "fingerprint", return_value="candidate"), \
                        patch.object(gate, "passing_review", return_value=True), \
                        patch.object(gate, "git", return_value=b"fixture-head"), \
                        patch.object(gate.subprocess, "run", side_effect=technical_check):
                    code = gate.release_ready(SimpleNamespace(baseline=None, evidence=output))
                receipt = json.loads(next(output.glob("technical-*/release.json")).read_text())
                self.assertEqual(code, 0 if change == "none" else 2)
                self.assertEqual(receipt["status"], "ready" if change == "none" else "blocked")
                if change == "none":
                    self.assertEqual(receipt["commit"], "fixture-head")
                    self.assertEqual(receipt["conversations"]["redesign"]["review_sha256"], gate.digest(review))

    def test_redesign_requires_an_ambiguous_reply_to_leave_intake_open(self):
        question = {
            "exchange_id": "alignment", "review_step": "initial_alignment",
            "question": "Is my understanding correct?",
        }
        state = {"phase": "discover", "guided_design_review": True, "pending_questions": [question]}

        def turn(user, after, message):
            return {"user": user, "messages": [message], "recovery": None,
                    "after": {"state": after, "git_head": "fixture", "git_status": ""}}

        initial = turn("Please investigate.", state, question["question"])
        ambiguous = "Maybe, if the export behavior stays the same."
        clarification = "Which export behavior do you want to preserve?"
        with self.subTest("absent"):
            issues = checkpoints([initial], {"git_head": "fixture"}, "redesign", set())
            self.assertTrue(any("ambiguous" in issue for issue in issues))
        with self.subTest("accepted"):
            advanced = {**state, "phase": "plan", "pending_questions": []}
            issues = checkpoints([initial, turn(ambiguous, advanced, clarification)],
                                 {"git_head": "fixture"}, "redesign", set())
            self.assertTrue(any("ambiguous" in issue for issue in issues))
        with self.subTest("only repeated"):
            issues = checkpoints([initial, turn(ambiguous, state, question["question"])],
                                 {"git_head": "fixture"}, "redesign", set())
            self.assertTrue(any("ambiguous" in issue for issue in issues))
        with self.subTest("clarified"):
            issues = checkpoints([initial, turn(ambiguous, state, clarification)],
                                 {"git_head": "fixture"}, "redesign", set())
            self.assertFalse(any("ambiguous" in issue for issue in issues))

    def test_redesign_requires_first_scoped_blueprint_and_map_replies_to_apply(self):
        blueprint = {'exchange_id': 'approval', 'review_step': 'blueprint_approval',
                     'review_version': 'spec-v1', 'question': 'Do you approve this blueprint?'}
        map_target = {'map_id': 'slice-map-v1', 'map_path': 'maps/v1.md', 'map_sha256': 'a' * 64}
        for pending, question, reply, applied, issue in (
                ({'pending_questions': [blueprint]}, 'Do you approve this blueprint?',
                 'I approve this blueprint.', {'approval_id': 'approve-spec-v1', 'approved_version': 'spec-v1'},
                 'blueprint approval did not apply'),
                ({'slice_review': {'slice_map': {'target': map_target}}},
                 'Do you confirm this slice map?', 'I confirm this slice map.',
                 {'slice_review': {'slice_map': {'target': map_target, 'consent_id': 'map-consent'}}},
                 'slice-map confirmation did not apply')):
            with self.subTest(issue=issue):
                def turn(user, message, state):
                    return {'user': user, 'messages': [message], 'recovery': None,
                            'after': {'state': state, 'git_head': 'fixture', 'git_status': ''}}

                prompt = turn('Please present the review.', question, pending)
                repeated = turn(reply, question, pending)
                advanced = turn(reply, 'Recorded.', applied)
                baseline = {'git_head': 'fixture'}
                self.assertTrue(any(issue in item for item in
                                    checkpoints([prompt, repeated, advanced], baseline, 'redesign', set())))
                self.assertFalse(any(issue in item for item in
                                     checkpoints([prompt, advanced], baseline, 'redesign', set())))

    def test_baseline_requires_untampered_evidence_for_its_commit(self):
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        candidate = fingerprint(commit)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run = output / "redesign"
            run.mkdir()
            (run / "run.json").write_text(json.dumps({"candidate": candidate, "scenario": "redesign"}))
            transcript = run / "events.jsonl"
            transcript.write_text("Recorded evidence")
            review = run / "review.json"
            review.write_text(json.dumps({"status": "pass", "evidence_hashes": evidence_hashes(run)}))
            technical = []
            for index in (1, 2):
                log = output / f"check-{index}.log"
                log.write_text("Passing technical check")
                technical.append({"exit_code": 0, "log_sha256": gate.digest(log)})
            receipt = output / "release.json"
            receipt.write_text(json.dumps({
                "schema_version": 1, "status": "ready", "commit": commit, "candidate": candidate,
                "conversations": {"redesign": {"run": str(run), "review_sha256": gate.digest(review)}},
                "technical": technical,
            }))
            with patch.object(gate, "automatic_checks", return_value=[]), \
                    patch.object(gate, "validate_review", return_value=False):
                self.assertEqual(gate.verified_baseline(receipt), commit)
                transcript.write_text("Different evidence")
                with self.assertRaisesRegex(ValueError, "review for redesign does not match"):
                    gate.verified_baseline(receipt)

    def test_verified_baseline_keeps_full_coverage_for_runtime_changes(self):
        for path, expected in (
            ("README.md", ["redesign"]),
            ("scripts/confirmation.py", ["redesign", "traceability", "vague"]),
        ):
            with self.subTest(path=path), \
                    patch.object(gate, "verified_baseline", return_value="verified-commit"), \
                    patch.object(gate, "git", side_effect=[path.encode() + b"\0", b""]):
                self.assertEqual(required_scenarios(Path("release.json")), expected)
