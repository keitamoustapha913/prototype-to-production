import json
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.slice_integrity import (
    capture_suspension_checkpoint,
    validate_lifecycle_history,
    validate_runtime_paths,
    validate_suspension_checkpoint,
    validate_transportable_commands,
)


class TransportableCommandTests(unittest.TestCase):
    def test_allows_absolute_path_interpreter_available_on_path(self):
        with tempfile.TemporaryDirectory() as root:
            errors = validate_transportable_commands(
                [[sys.executable, "tests/test_items.py", "items-api"]], root
            )
        self.assertEqual(errors, [])

    def test_rejects_absolute_project_runner_argument(self):
        with tempfile.TemporaryDirectory() as root:
            runner = Path(root) / "tests" / "test_items.py"
            runner.parent.mkdir()
            runner.touch()
            errors = validate_transportable_commands(
                [[sys.executable, str(runner), "items-api"]], root
            )
        self.assertTrue(any("absolute" in error.lower() or "workspace" in error.lower()
                            for error in errors), errors)

    def test_receiving_workspace_requires_the_approved_runner_reference(self):
        with tempfile.TemporaryDirectory() as root:
            errors = validate_runtime_paths(["tests/test_items.py"], root)
        self.assertTrue(any("tests/test_items.py" in error for error in errors), errors)


class LifecycleHistoryTests(unittest.TestCase):
    def test_requires_journal_event_for_durable_review_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            receipt = {"review_id": "slice-a-r1-review-1", "slice_id": "slice-a",
                       "proposal_revision": 1, "reviewed_guide_sha256": "a" * 64,
                       "reviewed_contract_sha256": "b" * 64}
            path = Path(root) / "slice-proposals/slice-a/reviews/review.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(receipt))
            registry = {"reviews": {"slice-a": receipt | {"path": "slice-proposals/slice-a/reviews/review.json"}},
                        "packages": {}, "artifact_hashes": {}}
            errors = validate_lifecycle_history([], registry, root)
        self.assertTrue(any("review" in error.lower() and "journal" in error.lower()
                            for error in errors), errors)

    def test_accepts_recovery_marker_when_it_carries_missing_event(self):
        with tempfile.TemporaryDirectory() as root:
            receipt = {"review_id": "slice-a-r1-review-1", "slice_id": "slice-a",
                       "proposal_revision": 1, "reviewed_guide_sha256": "a" * 64,
                       "reviewed_contract_sha256": "b" * 64}
            rel = "slice-proposals/slice-a/reviews/review.json"
            path = Path(root) / rel
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(receipt))
            registry = {"reviews": {"slice-a": receipt | {"path": rel}},
                        "packages": {}, "artifact_hashes": {}}
            events = [{"type": "slice_review_transition"}]
            marker = {"events": [{"type": "slice_spec_reviewed", **receipt,
                                  "receipt_path": rel}]}
            errors = validate_lifecycle_history(events, registry, root, recovery_marker=marker)
        self.assertEqual(errors, [])

    def test_requires_lock_authorization_and_release_journal_events(self):
        with tempfile.TemporaryDirectory() as root:
            package_body = {"package_id": "package-v1", "package_hash": ""}
            package_body.pop("package_hash")
            package_hash = __import__("hashlib").sha256(json.dumps(
                package_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()).hexdigest()
            package = package_body | {"package_hash": package_hash}
            package_path = Path(root) / "package.json"
            lock_path = Path(root) / "lock.json"
            auth_path = Path(root) / "authorization.json"
            package_path.write_text(json.dumps(package))
            lock_path.write_text(json.dumps({"package_id": "package-v1", "package_hash": package_hash}))
            authorization = {"package_id": "package-v1", "package_hash": package_hash}
            auth_path.write_text(json.dumps(authorization))
            registry = {"reviews": {}, "artifact_hashes": {}, "packages": {
                "package-v1": {"package_id": "package-v1", "package_hash": package_hash,
                    "path": "package.json", "receipt_path": "lock.json",
                    "authorization_path": "authorization.json",
                    "released": {"slice-a": {"contract_sha256": "d" * 64}}}}}
            errors = validate_lifecycle_history([], registry, root)
        joined = " ".join(errors).lower()
        self.assertIn("lock receipt has no matching journal event", joined)
        self.assertIn("authorization has no matching journal event", joined)
        self.assertIn("release for slice-a has no matching journal event", joined)


class SuspensionCheckpointTests(unittest.TestCase):
    def test_checkpoint_detects_source_edit_after_pause(self):
        checkpoint = capture_suspension_checkpoint(
            {"src/service.py": "authorized hash"}, "package-v1", "approve-spec-v1")
        errors = validate_suspension_checkpoint(checkpoint, {"src/service.py": "new hash", "src/new.py": "added"})
        self.assertIn("Suspended source changed after pause: src/service.py", errors)
        self.assertIn("Suspended source changed after pause: src/new.py", errors)

    def test_checkpoint_preserves_work_already_present_when_paused(self):
        snapshot = {"src/service.py": "authorized implementation"}
        checkpoint = capture_suspension_checkpoint(snapshot, "package-v1", "approve-spec-v1")
        self.assertEqual(validate_suspension_checkpoint(checkpoint, snapshot), [])


if __name__ == "__main__":
    unittest.main()
