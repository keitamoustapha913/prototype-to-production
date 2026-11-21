"""Final application audit and explicit acceptance of an exact technical report."""

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone

try:
    from scripts import project_state as state
except ModuleNotFoundError:
    import project_state as state


COMPLETE = {"technically_complete", "technically_complete_with_warnings"}
EVIDENCE_ERRORS = (state.StateError, OSError, ValueError, KeyError, TypeError)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def active_slices(manifest):
    version = manifest.get("approved_version")
    return {key: item for key, item in manifest.get("slices", {}).items()
            if version and item.get("blueprint_version", item.get("prototype_version")) == version
            and item.get("status") != "cancelled" and not item.get("superseded_by")}


def latest_findings(attempts):
    return {finding["finding_id"]: finding for attempt in attempts for finding in attempt["findings"]}


def assess(directory, manifest):
    """Read saved evidence and derive the four slice outcomes and project blockers."""
    visual = state.sibling_module("visual_integrity")
    cross = state.sibling_module("cross_screen")
    assignments = active_slices(manifest)
    holds = state.cross_screen_holds_by_slice(directory, manifest)
    blockers = state.check_initiative(directory, manifest, shared_holds=holds)
    blockers.extend(manifest.get("blockers", []))
    work = manifest.get("work_items", {}).get(manifest.get("active_work_id"), {})
    if work.get("status") in {"waiting_user", "blocked"}:
        blockers.append("Active work still needs a required decision or blocker resolution")
    slices = {}
    warnings = []
    defects = []
    repairs = []

    def findings(items, **scope):
        for finding in items.values():
            entry = {**scope, "finding_id": finding["finding_id"], "case_id": finding["case_id"],
                     "severity": finding["severity"], "description": finding["description"]}
            if visual.is_warning(finding):
                warnings.append(entry)
            elif finding.get("status") in {"open", "improved"}:
                defects.append(entry)
            elif finding.get("status") == "fixed":
                repairs.append(entry)

    failed_shared = set()
    plans = cross.current_plans(manifest)
    known = cross.related_assignments(directory, manifest) if plans else {}
    current_ids = {plan["change_id"] for plan in plans}
    for plan in manifest.get("cross_screen_changes", {}).values():
        if (plan["change_id"] not in current_ids and plan["status"] == "verified"
                and plan.get("approval_id") == manifest.get("active_approval_id")):
            previous = {key: row for key, row in latest_findings(plan["attempts"]).items()
                        if visual.is_warning(row) or row.get("status") == "fixed"}
            if previous:
                try:
                    cross.verify_attempt_images(directory, plan)
                except EVIDENCE_ERRORS as error:
                    blockers.append(f"Historical shared change {plan['change_id']}: {error}")
                findings(previous, slice_id=plan["slice_id"], change_id=plan["change_id"],
                         affected_slices=sorted(cross.affected_slices(manifest, plan, known=known)))
    for plan in plans:
        affected = cross.affected_slices(manifest, plan, known=known)
        attempts = plan.get("attempts", [])
        observed = latest_findings(attempts)
        findings(observed, slice_id=plan["slice_id"], change_id=plan["change_id"],
                 affected_slices=sorted(affected))
        if attempts:
            latest = attempts[-1]
            if (any(row["status"] == "failed" for row in latest["cases"])
                    or any(item.get("status") == "open" for item in observed.values())):
                failed_shared.update(affected)
            if cross.current_status(directory, plan) == "verified":
                try:
                    visual.require_accessibility(
                        [row.get("accessibility_capture") or row["after"] for row in latest["cases"]], latest["cases"])
                except visual.VisualIntegrityError as error:
                    for key in affected:
                        holds.setdefault(key, []).append(f"Shared change {plan['change_id']}: {error}")

    for key, assignment in sorted(assignments.items()):
        delivery = assignment["status"]
        reasons = list(assignment.get("blockers", [])) + holds.get(key, [])
        failed = key in failed_shared or (delivery == "blocked" and (
            assignment.get("required_check_failed", bool(assignment.get("block_evidence_hashes")))))
        try:
            visual_scope = state.slice_has_visual_scope(directory, manifest, key)
        except EVIDENCE_ERRORS as error:
            slices[key] = {"status": "not_verified", "delivery": delivery, "ui": False,
                           "reasons": [*reasons, str(error)]}
            continue
        attempts = [row for row in visual.list_attempts(directory, key, manifest=manifest, warnings=reasons)
                    if row.get("approval_id") == manifest.get("active_approval_id")] if visual_scope else []
        observed = latest_findings(attempts)
        findings(observed, slice_id=key)
        if attempts:
            latest = attempts[-1]
            failed |= (any(item.get("result") == "fail" for item in latest["observations"])
                       or any(check.get("result") == "fail" for row in latest.get("coverage", [])
                              for check in [*row["checks"].values(), row.get("accessibility", {})])
                       or any(item.get("status") == "open" for item in observed.values()))
        if visual_scope:
            try:
                receipt = visual.require_verified(directory, manifest, key)
                if not receipt.get("coverage"):
                    raise visual.VisualIntegrityError("Visual evidence lacks adaptive coverage")
                visual.require_accessibility(receipt["cases"], receipt["coverage"])
            except EVIDENCE_ERRORS as error:
                reasons.append(str(error))
        if assignment.get("warning_case_ids"):
            reasons.append("Required approved-case capture evidence is incomplete")
        if delivery in {"accepted", "verified", "provisional"}:
            reasons.extend(state.check_saved_verification(directory, key, assignment, manifest))
        else:
            reasons.append(f"Slice delivery is {delivery}")
        slices[key] = {"status": "needs_correction" if failed else "not_verified" if reasons else "verified",
                       "delivery": delivery, "ui": visual_scope, "reasons": list(dict.fromkeys(reasons))}

    for warning_id, warning in manifest.get("warnings", {}).items():
        if (warning.get("approval_id") != manifest.get("active_approval_id")
                or warning.get("slice_id") not in assignments):
            continue
        if warning.get("status") == "rejected":
            blockers.append(f"Rejected visual warning {warning_id}")
        else:
            try:
                count = len(warning_record(directory, manifest, warning_id)["comparison_pairs"])
            except EVIDENCE_ERRORS as error:
                blockers.append(str(error))
                count = 0
            warnings.append({"finding_id": warning_id, "slice_id": warning["slice_id"],
                             "severity": "capture warning", "description": warning["summary"],
                             "gallery_file": warning.get("gallery_file"), "comparison_count": count})

    for key, row in slices.items():
        if row["status"] == "verified" and any(
                key in warning.get("affected_slices", [warning["slice_id"]]) for warning in warnings):
            row["status"] = "verified_with_warnings"

    if assignments and all(row["delivery"] == "accepted" for row in slices.values()):
        try:
            ready = (state.blueprint_state(directory, manifest).all_accepted()
                     if state.initiative_mode(manifest) == "blueprint"
                     else state.all_slices_accepted(directory, manifest))
            if not ready:
                blockers.append("Active package or approved requirement coverage is incomplete")
        except EVIDENCE_ERRORS as error:
            blockers.append(str(error))
    return {"slices": slices, "warnings": warnings, "repairs": repairs, "defects": defects,
            "blockers": list(dict.fromkeys(blockers))}


def warning_file(directory, warning, relative):
    path = state.artifact_file(directory, relative)
    if state.digest_of(path) != warning["evidence_hashes"].get(relative):
        raise state.StateError("Supplemental visual warning evidence changed")
    return path


def warning_record(directory, manifest, warning_id):
    warning = manifest["warnings"][warning_id]
    return state.read_json(warning_file(directory, warning, warning["record_file"]))["warning"]


def warning_image(directory, manifest, warning_id, index, side):
    warning = manifest["warnings"][warning_id]
    record = warning_record(directory, manifest, warning_id)
    relative = record["comparison_pairs"][index]["reference_file" if side == "before" else "actual_file"]
    path = warning_file(directory, warning, relative)
    state.png_dimensions(path)
    return path.read_bytes()


def record_history(directory, manifest):
    """Verify report and acceptance history, including orphan files."""
    records = {"audit": {}, "accept": {}, "correction": {}}
    for line in (directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        kind = entry.get("type", "").removeprefix("visual_quality_")
        if kind in records and entry["type"] == "visual_quality_" + kind:
            key = entry["record_id"]
            if not state.VERSION_ID.fullmatch(key) or key in records[kind]:
                raise state.StateError("Invalid or duplicate visual quality journal ID")
            records[kind][key] = entry["sha256"]
    for kind, entries in records.items():
        folder = directory / "visual-quality" / kind
        if {path.stem for path in folder.glob("*.json")} != entries.keys():
            raise state.StateError(f"Visual quality {kind} files differ from the journal")
        for key, expected in entries.items():
            path = state.artifact_file(directory, f"visual-quality/{kind}/{key}.json")
            if state.digest_of(path) != expected:
                raise state.StateError(f"Visual quality {kind} {key} evidence changed")
    heads = {kind: next(reversed(entries), None) for kind, entries in records.items()}
    saved = manifest.get("visual_quality", {})
    if set(saved) - heads.keys() or any(saved.get(kind) != key for kind, key in heads.items()):
        raise state.StateError("Visual quality history differs from the manifest")
    return records, heads


def basis(directory, manifest, assessment, corrections=None):
    slices = active_slices(manifest)
    started = state.sibling_module("final_review").started_correction
    correction_basis = {key: row["history"][-1]["record_id"] for key, row in (corrections or {}).items()
                        if started(row)}
    return digest({
        **({"corrections": correction_basis} if correction_basis else {}),
        "workspace": state.blueprint_state(directory, manifest).workspace_snapshot(),
        "approval": manifest.get("active_approval_id"), "package": manifest.get("slice_review", {}).get("active_package"),
        "discovery": manifest.get("active_discovery_id"), "pending_coverage": manifest.get("coverage_pending"),
        "slices": {key: {**item, "saved_review": state.digest_of(root_file) if
                         (root_file := directory / "slices" / key / "verification.json").is_file() else None}
                   for key, item in slices.items()},
        "cross_screen_changes": manifest.get("cross_screen_changes", {}),
        "assessment": assessment,
    })


def read_record(directory, kind, key):
    return state.read_json(state.artifact_file(directory, f"visual-quality/{kind}/{key}.json"))


def status(directory, manifest, *, assessment=None):
    records, heads = record_history(directory, manifest)
    assessment = assess(directory, manifest) if assessment is None else assessment
    module = state.sibling_module("final_review")
    corrections = module.corrections(directory, records)
    correction_hold = any(row["status"] != "verified" and module.started_correction(row)
                          for row in corrections.values())
    audit = read_record(directory, "audit", heads["audit"]) if heads["audit"] else None
    completion = audit
    if audit and audit["status"] not in COMPLETE:
        completion = next((entry for key in reversed(records["audit"])
                           if (entry := read_record(directory, "audit", key))["status"] in COMPLETE), None)
    current = bool(audit and audit["basis"] == basis(directory, manifest, assessment, corrections))
    if current:
        try:
            current = audit["test_source_hashes"] == test_fingerprints(directory, audit["plan"]["checks"])
        except EVIDENCE_ERRORS:
            current = False
    acceptance = read_record(directory, "accept", heads["accept"]) if heads["accept"] else None
    accepted = bool(current and audit["status"] in COMPLETE and acceptance
                    and acceptance["audit_id"] == audit["audit_id"]
                    and acceptance["audit_sha256"] == records["audit"][audit["audit_id"]])
    blocked = (assessment["blockers"] or assessment["defects"]
               or correction_hold
               or any(row["status"] == "needs_correction"
                      or (row["delivery"] in {"blocked", "accepted", "provisional", "verified"} and row["status"] == "not_verified")
                      for row in assessment["slices"].values()))
    phase = ("finally_accepted" if accepted else audit["status"] if current
             else "blocked" if blocked else "in_implementation")
    return {**assessment, "status": phase, "audit": audit, "audit_current": current,
            "audit_sha256": records["audit"].get(heads["audit"]),
            "acceptance": acceptance, "acceptance_current": accepted,
            "completion_audit": completion, "corrections": corrections, "correction_hold": correction_hold,
            "audit_history": list(records["audit"]), "acceptance_history": list(records["accept"])}


def check_plan(directory, manifest, payload, assessment, previous):
    if not isinstance(payload, dict) or set(payload) not in ({"approval_id", "checks"},
                                                            {"approval_id", "checks", "workstreams"}) or (
            not manifest.get("active_approval_id") or payload["approval_id"] != manifest["active_approval_id"]):
        raise state.StateError("Audit input must name the active approval and its integration checks")
    workstreams = payload.get("workstreams", [])
    if not isinstance(workstreams, list):
        raise state.StateError("Workstreams must be a list of titles and assigned slice IDs")
    titles = set()
    for row in workstreams:
        if (not isinstance(row, dict) or row.keys() != {"title", "slice_ids"}
                or not isinstance(row["title"], str) or not row["title"].strip() or row["title"] in titles
                or not isinstance(row["slice_ids"], list) or not row["slice_ids"]
                or any(not isinstance(key, str) for key in row["slice_ids"])
                or len(set(row["slice_ids"])) != len(row["slice_ids"])
                or not set(row["slice_ids"]) <= assessment["slices"].keys()):
            raise state.StateError("Each workstream needs a distinct title and active slices")
        titles.add(row["title"])
    checks = payload["checks"]
    if not isinstance(checks, list) or not checks:
        raise state.StateError("Final audit requires integration checks")
    ids = set()
    journeys = set()
    has_integration = False
    for check in checks:
        fields = {"check_id", "kind", "slice_ids", "command", "test_source_paths", "expected", "scenario"}
        if not isinstance(check, dict) or set(check) != fields:
            raise state.StateError("Each audit check requires an ID, kind, scope, scenario, command, test sources and expectation")
        key = check["check_id"]
        if (not isinstance(key, str) or not state.VERSION_ID.fullmatch(key) or key in ids
                or check["kind"] not in ("integration", "journey")
                or not isinstance(check["scenario"], str) or not check["scenario"].strip()):
            raise state.StateError("Invalid or duplicate audit check")
        ids.add(key)
        state.blueprint_state(directory, manifest).argv(check["command"])
        scope = check["slice_ids"]
        if (not isinstance(scope, list) or not scope or any(not isinstance(item, str) for item in scope)
                or len(scope) != len(set(scope)) or not set(scope) <= assessment["slices"].keys()):
            raise state.StateError("Audit checks must reference active slices")
        paths = check["test_source_paths"]
        if (not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths)
                or len(paths) != len(set(paths))):
            raise state.StateError("Audit checks must reference actual test sources")
        for path in paths:
            state.project_source_file(directory.parents[2], path)
        if check["kind"] == "integration":
            has_integration = True
        else:
            journeys.update(scope)
    if previous and previous["approval_id"] == manifest["active_approval_id"]:
        by_id = {check["check_id"]: check for check in checks}
        for prior in previous["plan"]["checks"]:
            retained = set(prior["slice_ids"]) & assessment["slices"].keys()
            current = by_id.get(prior["check_id"], {})
            if retained and (current.get("kind") != prior["kind"] or not retained <= set(current["slice_ids"])):
                raise state.StateError(f"Retain mandatory audit check {prior['check_id']} for its active slice scope")
            if retained and digest(current["expected"]) != digest(prior["expected"]):
                raise state.StateError(f"Retain the expected outcome for mandatory audit check {prior['check_id']}")
    if not has_integration or not {key for key, row in assessment["slices"].items() if row["ui"]} <= journeys:
        raise state.StateError("Plan integration checks and browser journey checks covering every UI slice")
    return checks


def run_check(root, check):
    try:
        result = subprocess.run(check["command"], cwd=root, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {**check, "result": "not_verified", "diagnostic": str(error)}
    try:
        observed = json.loads(result.stdout)
        json.dumps(observed, allow_nan=False)
    except (ValueError, UnicodeError):
        observed = None
    passed = (result.returncode == 0 and isinstance(observed, dict)
              and observed.get("check_id") == check["check_id"]
              and {"expected", "actual"} <= observed.keys()
              and digest(observed.get("expected")) == digest(observed.get("actual")) == digest(check["expected"])
              and observed.get("result") in ("pass", "PASS"))
    return {**check, "result": "pass" if passed else "fail" if result.returncode or observed is not None else "not_verified",
            "returncode": result.returncode, "observation": observed,
            "diagnostic": result.stderr[-8192:].decode(errors="replace")}


def test_fingerprints(directory, checks):
    return {path: state.digest_of(state.project_source_file(directory.parents[2], path))
            for check in checks for path in check["test_source_paths"]}


def save(directory, manifest, kind, key, record, request, *, events=(), artifacts=None, binary_artifacts=None):
    relative = f"visual-quality/{kind}/{key}.json"
    data = (json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    manifest.setdefault("visual_quality", {"audit": None, "accept": None})[kind] = key
    event = {"type": f"visual_quality_{kind}", "record_id": key, "sha256": hashlib.sha256(data).hexdigest()}
    state.save_workflow_state(directory, manifest, transition=(request, [*events, event], artifacts or {}),
                              binary_artifacts={**(binary_artifacts or {}), relative: data})


def audit(directory, payload):
    request = {"command": "visual-quality audit", "input": payload}
    with state.coordinator_lock(directory):
        if state.recover_workflow_transition(directory, request):
            manifest = state.manifest_at(directory)
            return read_record(directory, "audit", manifest["visual_quality"]["audit"])
        manifest = state.manifest_at(directory)
        assessment = assess(directory, manifest)
        prior = status(directory, manifest, assessment=assessment)
        previous = prior["audit"]
        checks = check_plan(directory, manifest, payload, assessment, previous)
        before = basis(directory, manifest, assessment, prior["corrections"])
        sources = test_fingerprints(directory, checks)
        slices = assessment["slices"]
        eligible = bool(slices) and not (assessment["blockers"] or assessment["defects"]
                                         or prior["correction_hold"]) and all(
            row["delivery"] == "accepted" and row["status"] in {"verified", "verified_with_warnings"}
            for row in slices.values())
        if (eligible and prior["audit_current"] and previous["status"] in COMPLETE
                and previous["plan"] == payload and previous["test_source_hashes"] == sources):
            return previous
        outcomes = [run_check(directory.parents[2], check) for check in checks] if eligible else []
        if before != basis(directory, manifest, assess(directory, manifest), prior["corrections"]) or sources != test_fingerprints(directory, checks):
            raise state.StateError("Application or test evidence changed while the final audit ran")
        passed = eligible and all(row["result"] == "pass" for row in outcomes)
        phase = "blocked"
        if passed:
            phase = "technically_complete_with_warnings" if assessment["warnings"] else "technically_complete"
        elif not eligible and prior["status"] == "in_implementation":
            phase = "in_implementation"
        key = "audit-" + uuid.uuid4().hex
        report = {"audit_id": key, "approval_id": manifest["active_approval_id"], "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "status": phase, "basis": before, "plan": payload, "test_source_hashes": sources,
                  "checks": outcomes, **assessment}
        if passed:
            report["review_scope"] = state.sibling_module("final_review").scope_rows(directory, manifest, list(slices))
            if state.initiative_mode(manifest) == "blueprint":
                trace = state.blueprint_state(directory, manifest).final_requirement_traceability(
                    manifest["active_approval_id"], active_slices(manifest))
                if trace is not None:
                    report["requirement_traceability"] = trace
            visual = state.sibling_module("visual_integrity")
            cross = state.sibling_module("cross_screen")
            captures = {case["capture_id"] for key, row in slices.items() if row["ui"]
                        for case in visual.require_verified(directory, manifest, key)["cases"]}
            captures.update(case["capture_id"] for plan in cross.current_plans(manifest)
                            if plan["status"] == "verified"
                            for row in plan["attempts"][-1]["cases"]
                            for case in (row["after"], row.get("accessibility_capture")) if case)
            report["verified_capture_ids"] = sorted(captures)
        save(directory, manifest, "audit", key, report, request)
        return report


def accept(directory, payload):
    request = {"command": "visual-quality accept", "input": payload}
    with state.coordinator_lock(directory):
        if state.recover_workflow_transition(directory, request):
            manifest = state.manifest_at(directory)
            return read_record(directory, "accept", manifest["visual_quality"]["accept"])
        manifest = state.manifest_at(directory)
        current = status(directory, manifest)
        audit_record = current["audit"]
        if (not audit_record or not current["audit_current"] or audit_record["status"] not in COMPLETE
                or not isinstance(payload, dict)
                or set(payload) != {"approval_id", "audit_id", "audit_sha256", "approved_by_user", "user_response"}
                or payload["approval_id"] != audit_record["approval_id"] or payload["audit_id"] != audit_record["audit_id"]
                or payload["audit_sha256"] != current["audit_sha256"] or payload["approved_by_user"] is not True
                or not isinstance(payload["user_response"], str) or not payload["user_response"].strip()):
            raise state.StateError("Final acceptance requires an explicit user response for the exact current complete audit")
        if current["acceptance_current"]:
            if all(current["acceptance"].get(key) == value for key, value in payload.items()):
                return current["acceptance"]
            raise state.StateError("This audit already has a user's final acceptance record")
        if any(row["status"] not in {"verified", "withdrawn", "deferred"} for row in current["corrections"].values()):
            raise state.StateError("Resolve outstanding final review feedback or record the user's deferral or withdrawal before accepting")
        key = "accept-" + uuid.uuid4().hex
        record = {**payload, "acceptance_id": key, "accepted_at": state.utc_now()}
        save(directory, manifest, "accept", key, record, request)
        return record
