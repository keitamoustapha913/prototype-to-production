"""Trace final-review feedback through approval, implementation and revalidation."""

import hashlib
import json
import uuid
from datetime import datetime, timezone

try:
    from scripts import project_state as state, visual_quality as quality
except ModuleNotFoundError:
    import project_state as state
    import visual_quality as quality


def corrections(directory, records):
    findings = {}
    archived = set()
    for key in records["correction"]:
        event = quality.read_record(directory, "correction", key)
        findings.setdefault(event["finding_id"], []).append(event)
        archived.update(event.get("archived_evidence", {}).values())
    for relative in archived:
        if state.digest_of(state.artifact_file(directory, relative)) != relative.rsplit("/", 1)[1]:
            raise state.StateError(f"Archived verification evidence changed: {relative}")
    return {key: {"finding_id": key, "status": history[-1]["status"], "history": history}
            for key, history in findings.items()}


def started_correction(finding):
    return next((event for event in reversed(finding["history"]) if event["status"] == "in_progress"), None)


def scope_rows(directory, manifest, slice_ids):
    active = quality.active_slices(manifest)
    if (not isinstance(slice_ids, list) or not slice_ids or any(not isinstance(key, str) for key in slice_ids)
            or len(set(slice_ids)) != len(slice_ids) or not set(slice_ids) <= active.keys()):
        raise state.StateError("Identify distinct active slices for the correction")
    rows = {}
    for key in slice_ids:
        item = active[key]
        packet = state.artifact_file(directory, item["contract_file"])
        if state.digest_of(packet) != item["contract_sha256"]:
            raise state.StateError(f"Slice contract changed: {key}")
        spec = state.read_json(packet)
        features = spec.get("feature_contracts", [])
        rows[key] = {
            "title": spec.get("title") or key,
            "contract_file": item["contract_file"],
            "contract_sha256": item["contract_sha256"],
            "ui": state.slice_has_visual_scope(directory, manifest, key),
            "features": [{"feature_id": f["feature_id"], "title": f.get("title") or f["feature_id"],
                          "spec_paths": f.get("spec_paths", [])} for f in features],
            "decision_ids": [d["exchange_id"] for d in spec.get("decision_records", []) if "exchange_id" in d],
            "protected_decision_ids": sorted({d["decision_id"] for f in features
                                              for d in f.get("protected_visual_decisions", [])}),
            "approved_references": sorted(spec.get("approved_artifact_hashes", {})),
        }
    return rows


def captured_cases(directory, manifest, scope, selected):
    visual = state.sibling_module("visual_integrity")
    ui = {key for key, row in scope.items() if row["ui"]}
    if not isinstance(selected, dict) or selected.keys() != ui:
        raise state.StateError("Supply browser capture IDs for every UI slice in the correction scope")
    cases = {}
    for key, ids in selected.items():
        if (not isinstance(ids, list) or not ids or any(not isinstance(value, str) for value in ids)
                or len(ids) != len(set(ids))):
            raise state.StateError(f"Select distinct browser captures for {key}")
        assignment = manifest["slices"][key]
        fingerprint = visual.result_fingerprint(directory, manifest, key)
        cases[key] = []
        for capture_id in ids:
            receipt = assignment.get("visual_captures", {}).get(capture_id)
            if (not receipt or receipt["result_fingerprint"] != fingerprint
                    or receipt["approval_id"] != manifest["active_approval_id"]):
                raise state.StateError(f"Capture {capture_id} does not describe the current implementation of {key}")
            case = receipt["case"]
            for field in ("full_screenshot", "closeup_screenshot"):
                visual.resolve_evidence_path(directory, case[field])
            cases[key].append({"captured_at": receipt["captured_at"], "case": case})
    return cases


def compare_cases(directory, before, after, replacements, mapped):
    def conditions(case):
        return case["state"], case["viewport"]
    compare = state.sibling_module("cross_screen").comparison
    pairs = []
    for key, captures in before.items():
        target = replacements.get(key, key)
        candidates = list(after.get(target, []))
        for original in captures:
            link = mapped.get(original["case"]["capture_id"])
            if link:
                match = next((i for i, updated in enumerate(candidates)
                              if updated["case"]["capture_id"] == link["after_capture_id"]), None)
            else:
                match = next((i for i, updated in enumerate(candidates)
                              if conditions(original["case"]) == conditions(updated["case"])), None)
            if match is None:
                raise state.StateError(f"Recapture the before state and viewport for {key} or document its approved replacement")
            updated = candidates.pop(match)
            difference = compare(directory, target, original["case"]["closeup_screenshot"],
                                 updated["case"]["closeup_screenshot"])
            pairs.append({"slice_id": target, "before": original["case"], "after": updated["case"],
                          "difference": difference,
                          **({"comparison_note": link["note"]} if link else {})})
    return pairs


def saved_verification(directory, slice_id):
    return state.read_json(state.artifact_file(directory, f"slices/{slice_id}/verification.json"))


def archive_verification(directory, receipts, binary_artifacts):
    archived = {}
    for receipt in receipts.values():
        for field in ("evidence_hashes", "executed_render_hashes"):
            for relative, expected in receipt.get(field, {}).items():
                evidence = state.artifact_file(directory, relative).read_bytes()
                if hashlib.sha256(evidence).hexdigest() != expected:
                    raise state.StateError(f"Verification evidence changed: {relative}")
                target = f"visual-quality/review-evidence/{expected}"
                if (directory / target).exists():
                    if state.digest_of(state.artifact_file(directory, target)) != expected:
                        raise state.StateError(f"Archived verification evidence changed: {target}")
                else:
                    binary_artifacts[target] = evidence
                archived[relative] = target
    return archived


def require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise state.StateError(f"Supply {label}")


def record(directory, payload):
    if not isinstance(payload, dict):
        raise state.StateError("Final review input must be an object")
    phase = payload.get("status")
    fields = {
        "reported": {"audit_id", "slice_ids", "user_feedback"},
        "triaged": {"finding_id", "disposition", "rationale", "before_capture_ids"},
        "in_progress": {"finding_id", "slice_ids", "replacements"},
        "verified": {"finding_id", "after_capture_ids", "summary"},
        "withdrawn": {"finding_id", "user_response"},
        "deferred": {"finding_id", "user_response", "rationale"},
    }
    optional = {"comparison_map"} if phase == "verified" else set()
    if (not isinstance(phase, str) or phase not in fields or not fields[phase] | {"status"} <= payload.keys()
            or payload.keys() - (fields[phase] | {"status"} | optional)):
        raise state.StateError("Select a final review status and provide its required fields")
    request = {"command": "visual-quality correction", "input": payload}
    with state.coordinator_lock(directory):
        recovered = state.recover_workflow_transition(directory, request)
        manifest = state.manifest_at(directory)
        records, heads = quality.record_history(directory, manifest)
        if recovered:
            return quality.read_record(directory, "correction", heads["correction"])
        findings = corrections(directory, records)
        key = "review-" + uuid.uuid4().hex
        event = {**payload, "record_id": key, "recorded_at": datetime.now(timezone.utc).isoformat()}
        events, artifacts, binary_artifacts = [], {}, {}
        if phase == "reported":
            require_text(payload["user_feedback"], "the user's feedback")
            audit_id = payload["audit_id"]
            if not isinstance(audit_id, str) or audit_id not in records["audit"]:
                raise state.StateError("Select a saved technical completion report")
            audit = quality.read_record(directory, "audit", audit_id)
            if audit["status"] not in quality.COMPLETE or audit["approval_id"] != manifest.get("active_approval_id"):
                raise state.StateError("Feedback must refer to a complete report for the active approval")
            scope = scope_rows(directory, manifest, payload["slice_ids"])
            if not scope.keys() <= audit["slices"].keys():
                raise state.StateError("Select slices delivered in the technical completion report")
            audit_sha256 = records["audit"][audit_id]
            acceptance = next((entry for key in reversed(records["accept"])
                               if (entry := quality.read_record(directory, "accept", key))["audit_id"] == audit_id
                               and entry["audit_sha256"] == audit_sha256), None)
            event.update(finding_id=key, scope=scope, approval_id=audit["approval_id"], audit_sha256=audit_sha256,
                         acceptance_id=acceptance["acceptance_id"] if acceptance else None)
        else:
            finding_id = payload["finding_id"]
            finding = findings.get(finding_id) if isinstance(finding_id, str) else None
            allowed = {"triaged": {"reported", "triaged", "deferred", "in_progress"}, "in_progress": {"triaged"},
                       "verified": {"in_progress"}, "withdrawn": {"reported", "triaged", "deferred"},
                       "deferred": {"reported", "triaged"}}
            if not finding or finding["status"] not in allowed[phase]:
                raise state.StateError("Invalid transition for this final review finding")
            original = finding["history"][0]
            previous = finding["history"][-1]
            if phase == "triaged":
                require_text(payload["rationale"], "the approved-scope comparison and decision rationale")
                if payload["disposition"] not in ("within_approval", "requires_amendment"):
                    raise state.StateError("Determine whether the correction requires an approved amendment")
                started = started_correction(finding)
                if started:
                    if (payload["disposition"] != "requires_amendment"
                            or payload["before_capture_ids"] != started["before_capture_ids"]):
                        raise state.StateError("Retain the original comparison and seek amendment approval for the started correction")
                    event["before"] = started["before"]
                else:
                    if original["approval_id"] != manifest.get("active_approval_id"):
                        raise state.StateError("Record the comparison before changing the approval")
                    event["before"] = captured_cases(directory, manifest, original["scope"], payload["before_capture_ids"])
                    event["previous_verification"] = {key: saved_verification(directory, key)
                                                      for key in original["scope"]}
            elif phase in {"withdrawn", "deferred"}:
                if started_correction(finding):
                    raise state.StateError("Revalidate the work before closing a started correction")
                require_text(payload["user_response"], "the user's explicit review decision")
                if phase == "deferred":
                    require_text(payload["rationale"], "why this feedback does not affect mandatory checks")
                    current = quality.status(directory, manifest)
                    if not current["audit_current"] or current["audit"]["status"] not in quality.COMPLETE:
                        raise state.StateError("A nonblocking deferral requires a current passing technical report")
            elif phase == "in_progress":
                start(directory, manifest, event, original, previous, findings, events, artifacts)
            else:
                finish(directory, manifest, event, previous)
        receipts = event.get("previous_verification", event.get("verification", {}))
        event["archived_evidence"] = archive_verification(directory, receipts, binary_artifacts)
        quality.save(directory, manifest, "correction", key, event, request, events=events, artifacts=artifacts,
                     binary_artifacts=binary_artifacts)
        return event


def start(directory, manifest, event, original, triage, findings, events, artifacts):
    approval = manifest.get("active_approval_id")
    amended = triage["disposition"] == "requires_amendment"
    if amended != (approval != original["approval_id"]):
        raise state.StateError("Use the original approval for an in-scope fix or obtain the required amendment before starting")
    scope = scope_rows(directory, manifest, event["slice_ids"])
    original_ids = set(original["scope"])
    replacements = event["replacements"]
    if (not isinstance(replacements, dict) or not replacements.keys() <= original_ids
            or any(not isinstance(item, str) or item not in scope for item in replacements.values())
            or (replacements and not amended)
            or {replacements.get(key, key) for key in original_ids} != scope.keys()):
        raise state.StateError("Map the originally affected slices to their current approved assignments")
    for key, replacement in replacements.items():
        item = manifest["slices"][key]
        replacement_packet = state.read_json(state.artifact_file(directory, scope[replacement]["contract_file"]))
        old_features = {feature["feature_id"] for feature in original["scope"][key]["features"]}
        new_features = {feature["feature_id"] for feature in scope[replacement]["features"]}
        same_scope = (old_features & new_features or any(
            state.ownership_overlaps(old, new) for old in item["allowed_paths"]
            for new in manifest["slices"][replacement]["allowed_paths"]))
        if item.get("superseded_by") not in {approval, replacement} or (
                key not in replacement_packet.get("replaces_slice_ids", []) and not same_scope):
            raise state.StateError(f"Replacement for {key} must come from the approved package and trace to the original scope")
    if state.requires_slice_review(manifest):
        review = state.slice_review_state(directory, manifest)
        for key in scope:
            review.require_execution(key)
    if state.initiative_mode(manifest) == "blueprint":
        state.blueprint_state(directory, manifest).require_ready()
    active = quality.active_slices(manifest)
    affected = set(scope)
    while dependents := {key for key, item in active.items() if set(item["dependencies"]) & affected} - affected:
        affected.update(dependents)
    for key, finding in findings.items():
        previous_start = started_correction(finding)
        if (key != event["finding_id"] and previous_start and finding["status"] != "verified"
                and affected.intersection(previous_start["affected_slices"])):
            raise state.StateError("An overlapping final review correction is already in progress")
    if any(active[key]["status"] not in {"ready", "accepted", "blocked"} for key in scope):
        raise state.StateError("The correction requires a ready or accepted assignment")
    if any(active[key]["status"] == "blocked" and (not active[key]["blockers"] or any(
            not any(reason == f"Waiting for accepted dependency: {dep}" for dep in scope)
            for reason in active[key]["blockers"])) for key in scope):
        raise state.StateError("Resolve existing blockers before starting the correction")

    before = triage["before"]
    if not amended and before != captured_cases(directory, manifest, scope, triage["before_capture_ids"]):
        raise state.StateError("Refresh the before capture against the unchanged implementation")
    prior_verification = {key: saved_verification(directory, key) for key in affected
                          if active[key]["status"] == "accepted"}
    event.update(approval_id=approval, amended=amended, scope=scope, affected_slices=sorted(affected), before=before,
                 before_capture_ids=triage["before_capture_ids"], previous_verification=prior_verification)
    for key in scope:
        state.block_dependents(directory, manifest, key, staged=(events, artifacts))
    for key in scope:
        assignment = active[key]
        if assignment["status"] == "accepted":
            assignment["status"] = "ready"
        assignment["warning_case_ids"] = []
    manifest["stage"] = "implement"
    manifest["next_action"] = f"Implement and reverify final review finding {event['finding_id']}."


def finish(directory, manifest, event, started):
    require_text(event["summary"], "a focused correction and verification summary")
    if manifest.get("active_approval_id") != started["approval_id"]:
        raise state.StateError("The correction scope must be re-established against the current approval")
    assessment = quality.assess(directory, manifest)
    verification = {}
    journal = [json.loads(line) for line in (directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    start_index = next(index for index, item in enumerate(journal) if item.get("record_id") == started["record_id"])
    reaccepted = {item["slice_id"] for item in journal[start_index + 1:]
                  if item["type"] in {"slice_accepted", "capture_warning_user_accepted"}}
    visual = state.sibling_module("visual_integrity")
    for key in started["affected_slices"]:
        row = assessment["slices"].get(key)
        if (key not in reaccepted or not row or row["delivery"] != "accepted"
                or row["status"] not in {"verified", "verified_with_warnings"}):
            raise state.StateError(f"Reverify and accept the affected slice: {key}")
        verification[key] = saved_verification(directory, key)
    after = captured_cases(directory, manifest, started["scope"], event["after_capture_ids"])
    for key, captures in after.items():
        receipt = visual.require_verified(directory, manifest, key)
        inspected = {case.get("capture_id") for case in receipt["cases"]}
        if any(row["captured_at"] <= started["recorded_at"] or row["case"]["capture_id"] not in inspected for row in captures):
            raise state.StateError(f"Record fresh captures in the passing visual attempt for {key}")
    mapped = event.get("comparison_map", {})
    original_ids = {row["case"]["capture_id"] for rows in started["before"].values() for row in rows}
    if not isinstance(mapped, dict) or (mapped and not started["amended"]) or not mapped.keys() <= original_ids:
        raise state.StateError("Map changed capture conditions only for an approved amendment")
    for link in mapped.values():
        if not isinstance(link, dict) or link.keys() != {"after_capture_id", "note"}:
            raise state.StateError("A changed comparison needs an after capture ID and a note")
        require_text(link["after_capture_id"], "the replacement capture ID")
        require_text(link["note"], "how the amended case maps to the original capture")
    event["comparisons"] = compare_cases(directory, started["before"], after, started["replacements"], mapped)
    event["verification"] = verification


def dashboard(directory, manifest, status):
    if not status["completion_audit"]:
        return None
    audit = status["completion_audit"]
    active = quality.active_slices(manifest)
    keys = sorted(active.keys() & audit["slices"].keys())
    return {"audit": audit, "slices": audit.get("review_scope") or (scope_rows(directory, manifest, keys) if keys else {}),
            "prior_reports": [report for key in reversed(status["audit_history"]) if key != audit["audit_id"]
                              and (report := quality.read_record(directory, "audit", key))["status"] in quality.COMPLETE],
            "corrections": list(status["corrections"].values()),
            "acceptances": [quality.read_record(directory, "accept", key) for key in status["acceptance_history"]]}
