"""Initial alignment through the public initiative CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fixtures.confirmation_session import ConfirmationSession


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/project_state.py"


class InitialAlignmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = os.environ.copy()

    def cli(self, command, *args, blocked=None):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--project-root", str(self.root),
             "--initiative-id", "intake", *map(str, args)], capture_output=True, text=True, env=self.env,
        )
        if blocked:
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(blocked, result.stderr)
        else:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def initialize(self, mode, *, legacy=True):
        output = self.cli("init", "--scope", "feature", "--mode", mode)
        self.directory = Path(output.strip().removeprefix("Initialized initiative: "))
        if legacy:
            manifest_path = self.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("confirmation_protocol_version", None)
            manifest_path.write_text(json.dumps(manifest))
        (self.directory / "presentation.md").write_text("# Intake\n\nThe empty app needs item listing.\n")
        (self.directory / "inspection.txt").write_text("Inspected the project tree and found no application source.\n")

    def record(self, command, argument, data, **expectation):
        path = self.directory / "command-input.json"
        path.write_text(json.dumps(data))
        return self.cli(command, argument, path, **expectation)

    def exchange(self, value, **expectation):
        self.record("conversation-record", "--exchange-file", value, **expectation)

    def presentation(self, version="intake-v1"):
        return {
            "exchange_id": version, "phase": "discover", "record_type": "review_checkpoint",
            "review_step": "initial_alignment", "review_version": version,
            "review_target": "Initial Understanding and Alignment",
            "status": "open", "response": None,
            "question": "Have I understood your request? Please confirm before planning.",
            "requirement_ids": ["items"], "evidence_paths": ["presentation.md"],
            "reviewed_sources": ["User brief in full"],
            "requirement_mappings": [{
                "kind": "requirement",
                "requirement_id": "items", "text": "List items.", "request_reference": "User brief, item listing",
                "classification": "missing", "finding": "The project has no application source.",
                "direction": "Add an item listing.", "evidence_paths": ["inspection.txt"],
            }],
        }

    def reply(self, presentation, action, response, **expectation):
        answer = {key: val for key, val in presentation.items()
                  if key not in {"requirement_mappings", "evidence_paths", "reviewed_sources"}}
        answer.update(exchange_id=presentation["exchange_id"] + "-" + action, status="decided",
                      responds_to=presentation["exchange_id"], review_action=action, response=response)
        self.exchange(answer, **expectation)

    def test_new_intake_records_a_natural_user_confirmation(self):
        self.initialize("blueprint", legacy=False)
        with tempfile.TemporaryDirectory() as store:
            session = ConfirmationSession(self.root, store, self.env)
            first = self.presentation()
            first["question"] = "Is my understanding correct?"
            self.exchange(first)
            session.prompt("Initiative intake / Initial understanding intake-v1\nIs my understanding correct?")
            session.reply("That looks correct.")
            self.reply(first, "confirm", "That looks correct.")
        receipt = json.loads((self.directory / "conversation/intake-v1-confirm.json").read_text())
        self.assertEqual(receipt["confirmation_origin"]["action"], "initial_alignment")
        self.assertEqual(receipt["confirmation_origin"]["scope"], ["intake", "intake-v1"])

    def test_blueprint_requires_confirmation_and_carries_the_current_map_into_the_plan(self):
        self.initialize("blueprint")
        audit = {"audit_id": "audit-v1", "scope": "feature", "surface": "backend",
                 "baseline_kind": "greenfield", "features": [], "unresolved": [],
                 "inspected_paths": ["."], "evidence_paths": ["inspection.txt"], "requirements": []}
        self.record("discovery-record", "--audit-file", audit, blocked="Initial understanding")
        first = self.presentation()
        self.exchange(first)
        catalog = self.directory / "living-review/sections.json"
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({"sections": [
            {"id": "understanding", "title": "Initial Understanding and Alignment",
             "source": "alignment/intake-v1.md"},
        ]}))
        revision = json.loads(self.cli("review-revision"))
        self.assertIn("understanding", revision["sections"])
        resumed = json.loads(self.cli("resume", "--format", "json"))
        self.assertEqual(resumed["pending_questions"][0]["exchange_id"], "intake-v1")
        self.assertIn("alignment/intake-v1.md", resumed["pending_questions"][0]["evidence_paths"])
        self.assertIn("Missing", (self.directory / "alignment/intake-v1.md").read_text())
        self.reply(first, "skip", "Skip it.", blocked="cannot be waived")
        self.reply(first, "confirm", "Looks good.", blocked="explicitly confirm")
        self.reply(first, "confirm", "No, you missed filtering.", blocked="explicitly confirm")
        self.reply(first, "confirm", "I disagree with that interpretation.", blocked="explicitly confirm")
        self.reply(first, "confirm", "Confirm the blueprint.", blocked="explicitly confirm")
        self.reply(first, "revise", "The list needs a filter.")
        self.record("discovery-record", "--audit-file", audit, blocked="Initial understanding")
        revised = self.presentation("intake-v2")
        (self.directory / "revised.md").write_text("# Intake revised\n\nThe app needs an item list with a filter.\n")
        revised["evidence_paths"] = ["revised.md"]
        revised["requirement_mappings"][0]["text"] = "List and filter items."
        self.exchange(revised)
        self.reply(revised, "confirm", "Yes, you understood the requirements. Continue to planning.")
        self.record("discovery-record", "--audit-file", audit, blocked="confirmed requirement IDs")

        audit["requirements"] = [{"requirement_id": "items", "text": "List items.", "decision_ids": []}]
        self.record("discovery-record", "--audit-file", audit, blocked="match the confirmed requirement text")
        audit["requirements"][0]["text"] = "List and filter items."
        self.record("discovery-record", "--audit-file", audit)
        plan_folder = self.directory / "plans/plan-v1"
        plan_folder.mkdir(parents=True, exist_ok=True)
        sections = ("Objective", "User journeys", "Scope boundary", "Preservation constraints",
                    "Workstreams", "Dependencies", "Risks", "Build order")
        (plan_folder / "roadmap.md").write_text(
            "[Detailed blueprint specifications](blueprints/index.md)\n\n"
            + "".join(f"## {section}\nItem listing.\n\n" for section in sections))
        plan = {"version": "plan-v1", "baseline_id": "audit-v1", "scope": "feature",
                "objective": "List and filter items.", "workstreams": ["Item listing"],
                "dependencies": [], "risks": [], "decision_ids": [],
                "document_paths": ["plans/plan-v1/roadmap.md"]}
        self.record("plan-record", "--plan-file", plan, blocked="Map every confirmed requirement")
        plan["requirement_workstreams"] = {"items": ["Item listing"]}
        overview = {
            "exchange_id": "plan-overview", "phase": "plan", "record_type": "review_checkpoint",
            "review_step": "roadmap", "review_version": "plan-v1", "review_target": "Item listing roadmap",
            "status": "open", "question": "Does this roadmap capture the direction?",
            "requirement_ids": ["items"], "evidence_paths": plan["document_paths"],
        }
        self.exchange(overview)
        self.reply(overview, "confirm", "The roadmap matches the scope.")
        self.record("plan-record", "--plan-file", plan)
        saved = json.loads((plan_folder.with_suffix(".json")).read_text())
        self.assertIn("alignment/intake-v2.md", saved["_frozen_evidence"])
        self.assertNotIn("alignment/intake-v1.md", saved["_frozen_evidence"])
        requirement_map = self.directory / saved["requirement_map_path"]
        self.assertIn("List and filter items.", requirement_map.read_text())
        self.assertIn("Workstreams — Item listing", requirement_map.read_text())

        workstream = {**overview, "exchange_id": "listing-review", "review_step": "plan_workstream",
                      "review_workstream": "Item listing", "question": "Continue after reviewing the listing?",
                      "feature_ids": ["items"]}
        self.exchange({**workstream, "requirement_ids": []}, blocked="every requirement ID mapped")
        self.exchange(workstream)
        self.reply(workstream, "revise", "Filtering also needs an empty-state message.")
        adjusted = self.presentation("intake-v3")
        adjusted["evidence_paths"] = ["adjusted.md"]
        adjusted["requirement_mappings"][0]["text"] = "List and filter items with an empty-state message."
        (self.directory / "adjusted.md").write_text("# Intake adjusted\n\nExplain when filters return no items.\n")
        self.exchange(adjusted)
        self.reply(adjusted, "confirm", "Confirm understanding")
        self.record("plan-record", "--plan-file", {**plan, "version": "plan-v2"},
                    blocked="current initial understanding")
        audit["audit_id"] = "audit-v2"
        audit["requirements"][0]["text"] = adjusted["requirement_mappings"][0]["text"]
        self.record("discovery-record", "--audit-file", audit)

    def test_revision_cannot_silently_drop_a_requirement(self):
        self.initialize("blueprint")
        first = self.presentation()
        first["requirement_ids"].append("export")
        first["requirement_mappings"].append({**first["requirement_mappings"][0],
                                                "requirement_id": "export", "text": "Export items."})
        self.exchange(first)
        self.reply(first, "revise", "Only implement item listing. Remove export from this request.")
        revised = self.presentation("intake-v2")
        (self.directory / "revised.md").write_text("# Revised\n\nThis request is for item listing only.\n")
        revised["evidence_paths"] = ["revised.md"]
        self.exchange(revised, blocked="Keep previously inventoried IDs")
        self.exchange({"exchange_id": "remove-export", "phase": "discover", "status": "decided",
                       "question": "Have I understood your request? Please confirm before planning.",
                       "response": "Only implement item listing. Remove export from this request.",
                       "requirement_ids": ["export"],
                       "authorized_changes": [{"baseline_id": "export", "classification": "REMOVE"}]})
        revised["scope_removal_decision_ids"] = ["remove-export"]
        self.exchange(revised, blocked="Keep previously inventoried IDs")
        revised.pop("scope_removal_decision_ids")
        revised["requirement_ids"].append("export")
        revised["requirement_mappings"].append(first["requirement_mappings"][1])
        self.exchange(revised)
        markdown = (self.directory / "alignment/intake-v2.md").read_text()
        self.assertIn("Export items.", markdown)

    def test_prototype_uses_the_same_checkpoint_and_recovers_an_open_exchange(self):
        self.initialize("prototype")
        self.record("discovery-record", "--audit-file", {}, blocked="Initial understanding")
        first = self.presentation()
        self.exchange(first)
        cursor = {"task_id": "intake", "phase": "discover", "status": "waiting_user",
                  "summary": "Presented initial understanding", "next_action": "Wait for confirmation",
                  "completed_steps": ["Investigated the repository"], "pending_steps": ["Confirm understanding"],
                  "blockers": [], "code_paths": [], "artifact_paths": ["alignment/intake-v1.md"],
                  "pending_question_id": "intake-v1"}
        self.record("work-update", "--work-file", cursor)
        resumed = json.loads(self.cli("resume", "--format", "json"))
        self.assertEqual(resumed["pending_questions"][0]["exchange_id"], "intake-v1")
        self.reply(first, "confirm", "You understood the requested scope. Continue.")
        resumed = json.loads(self.cli("resume", "--format", "json"))
        self.assertEqual(resumed["active_question_answer"]["review_action"], "confirm")
        self.record("discovery-record", "--audit-file", {}, blocked="Discovery audit_id")
        (self.directory / "alignment/intake-v1.md").write_text("Changed")
        self.cli("validate", blocked="Changed protected evidence")

    def test_confirmed_alignment_requires_current_repository_evidence(self):
        self.initialize("blueprint")
        source = self.root / "service.py"
        source.write_text("# Existing service\n")
        initial = self.presentation()
        initial["requirement_mappings"][0]["source_paths"] = ["service.py"]
        self.exchange(initial)
        self.reply(initial, "confirm", "You understood the requirements. Continue to planning.")
        source.write_text("# Existing service now includes item listing\n")
        self.record("discovery-record", "--audit-file", {"audit_id": "audit-v1", "scope": "feature"},
                    blocked="Update the evidence and reconfirm initial understanding")

    def test_revised_alignment_after_discovery_requires_a_new_audit(self):
        self.initialize("blueprint")
        original = self.presentation()
        self.exchange(original)
        self.reply(original, "confirm", "Confirm understanding")
        audit = {"audit_id": "audit-v1", "scope": "feature", "surface": "backend",
                 "baseline_kind": "greenfield", "features": [], "unresolved": [],
                 "inspected_paths": ["."], "evidence_paths": ["inspection.txt"],
                 "requirements": [{"requirement_id": "items", "text": "List items."}]}
        self.record("discovery-record", "--audit-file", audit)
        revised = self.presentation("intake-v2")
        (self.directory / "revised.md").write_text("# Revised\n\nThe mandatory filter is now in scope.\n")
        revised["evidence_paths"] = ["revised.md"]
        revised["requirement_mappings"][0]["text"] = "List and filter items."
        self.exchange(revised)
        self.reply(revised, "confirm", "Confirm understanding")
        stale_plan = {"version": "plan-v1", "baseline_id": "audit-v1", "scope": "feature",
                      "objective": "List items.", "workstreams": ["Listing"], "dependencies": [], "risks": []}
        self.record("plan-record", "--plan-file", stale_plan, blocked="current initial understanding")
        audit["audit_id"] = "audit-v2"
        audit["requirements"][0]["text"] = "List and filter items."
        self.record("discovery-record", "--audit-file", audit)
        audits = self.directory / "discovery/audits"
        self.assertEqual(json.loads((audits / "audit-v1.json").read_text())["initial_alignment_id"], "intake-v1")
        self.assertEqual(json.loads((audits / "audit-v2.json").read_text())["initial_alignment_id"], "intake-v2")
