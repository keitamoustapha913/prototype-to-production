"""Reconstruct the conversation checkpoint from the workflow's durable records."""

STAGE_LABELS = {
    "discover": "Discovery and initial alignment",
    "plan": "Implementation plan review",
    "blueprint": "Blueprint review",
    "prototype": "Prototype review",
    "approved": "Slice planning and review",
    "slice_review": "Slice review",
}


def version_reference(registry, version=None):
    if not registry:
        return None
    version = version or max(registry, key=lambda key: registry[key]["sequence"])
    entry = registry[version]
    return {"version": version, "path": entry["path"], "sha256": entry["sha256"],
            "evidence_hashes": entry.get("evidence_hashes", {})}


def changed_documents(versions, manifest, slice_review, digest):
    evidence = {}
    for role in ("plan", "blueprint"):
        if version := versions[role]:
            evidence[f"{role} {version['version']}"] = {
                version["path"]: version["sha256"], **version["evidence_hashes"],
            }
    approval_id = versions["approval_id"]
    if approval_id:
        approval = manifest.get("approvals", {}).get(approval_id, {})
        evidence[f"approval {approval_id}"] = approval.get("artifact_hashes", {})
    for exchange_id, entry in manifest.get("conversation", {}).items():
        evidence[f"conversation {exchange_id}"] = {
            entry["path"]: entry["sha256"], **entry.get("evidence_hashes", {}),
        }
    for review in slice_review.get("reviews", {}).values():
        evidence[f"slice review {review['review_id']}"] = review.get("evidence_hashes", {})
    if walkthrough := slice_review.get("walkthroughs", {}).get(slice_review.get("current_slice_id")):
        current = slice_review["current_slice_id"]
        evidence[f"slice walkthrough {current}"] = {
            walkthrough["presentation_path"]: walkthrough["presentation_sha256"],
        }
    observed = {}
    changes = {}
    for scope, hashes in evidence.items():
        for path, expected in hashes.items():
            if path not in observed:
                observed[path] = digest(path)
            actual = observed[path]
            if actual != expected:
                changes.setdefault(path, []).append({
                    "review_evidence": scope, "saved_sha256": expected, "current_sha256": actual,
                })
    return changes


def checkpoint(manifest, packet, superseded_decisions, digest):
    if packet["delivery_slice"] is not None:
        return None
    stage = packet["phase"]
    work = packet["active_work"] or {}
    slice_review = packet["slice_review"] or {}
    quality = packet["visual_quality"] or {}
    waiting = work.get("status") == "waiting_user" or bool(packet["pending_questions"])
    reviewing_slices = slice_review.get("required") and (
        slice_review.get("current_slice_id") or slice_review.get("pending_continue")
        or (stage in {"approved", "slice_review"} and not slice_review["implementation_authorized"]))
    final_review = (quality.get("audit_current") and not quality.get("acceptance_current")
                    and quality.get("audit") == quality.get("completion_audit"))
    discovery = stage == "discover" and (
        packet["initiative_mode"] == "blueprint" or manifest.get("initial_alignment_required")
        or manifest.get("conversation"))
    prototype_review = stage == "prototype" and any(
        entry.get("audit_id") == packet["latest_a_audit"]
        for entry in manifest.get("prototype_coverage", {}).values())
    if not (waiting or discovery or reviewing_slices or final_review or prototype_review
            or stage in {"plan", "blueprint"}):
        return None

    records = packet["conversation"]
    by_id = {record["exchange_id"]: record for record in records}
    pending = {record["exchange_id"]: record for record in packet["pending_questions"]}
    answered = {record["responds_to"]: record["exchange_id"] for record in records
                if record.get("responds_to") and record["status"] in {"decided", "rejected"}}
    progress = packet["workflow_progress"] or {}
    plan = packet["plan_review"] or {}
    position = {}
    completed = []
    walkthrough = None
    readiness = None
    if reviewing_slices:
        current = slice_review["current_slice_id"]
        position = {"kind": "slice", "name": current, "revision": slice_review["current_revision"]}
        completed = list(slice_review["reviews"])
        walkthrough = slice_review["walkthroughs"].get(current)
        readiness = slice_review["handoff_readiness"].get(current)
    elif stage == "plan" and plan:
        current = plan["current_workstream"]
        position = {"kind": "workstream", "name": current, "total": len(plan["workstreams"]),
                    "number": plan["workstreams"].index(current) + 1 if current else None}
        completed = plan["completed_workstreams"]
    elif progress:
        position = {"kind": "workstream", "name": progress["current_item"], "total": progress["total"],
                    "number": progress["current_number"]}
        completed = progress["completed_workstreams"]

    question_id = ((slice_review.get("pending_question_id") if reviewing_slices else None)
                   or work.get("pending_question_id") or progress.get("pending_question_id"))
    answered_record = by_id.get(answered.get(question_id))
    question = pending.get(question_id)
    if question is None and answered_record is None:
        question = next((record for record in pending.values() if record.get("phase") == stage),
                        next(iter(pending.values()), None))
    current = position.get("name")
    same_stage = [record for record in records if record.get("phase") == stage]
    shown = question or by_id.get(question_id) or next(
        (record for record in reversed(same_stage)
         if (record.get("evidence_paths") or record.get("illustration_paths"))
         and (not current or current in (record.get("review_workstream"), record.get("slice_id")))), None)
    if not shown:
        shown = next((record for record in reversed(same_stage)
                      if record.get("evidence_paths") and not record.get("responds_to")), None)
    if shown:
        shown = by_id.get(shown.get("responds_to"), shown)
    if not position and shown and shown.get("review_workstream"):
        position = {"kind": "workstream", "name": shown["review_workstream"]}
    presentation = None
    if shown:
        presentation = {key: shown[key] for key in
                        ("exchange_id", "review_target", "evidence_paths", "illustration_paths")
                        if key in shown}
    if walkthrough:
        presentation = {**(presentation or {}), "walkthrough_path": walkthrough["presentation_path"]}
    decisions = [
        {"exchange_id": record["exchange_id"], "response": record["response"]}
        for record in records if record["status"] == "decided" and record.get("response")
        and record.get("record_type", "decision") in {"decision", "slice_detail"}
        and record["exchange_id"] not in superseded_decisions
    ]
    latest_review_reply = next(
        (record for record in reversed(same_stage)
         if record.get("responds_to") and record.get("record_type") == "review_checkpoint"), None)
    recorded_reply = answered_record
    if recorded_reply is None and shown:
        recorded_reply = by_id.get(answered.get(shown["exchange_id"]))
    errors = [error for error in packet["validation_errors"]
              if not pending or error != "Unanswered consequential question blocks implementation"]
    issues = list(dict.fromkeys(errors + packet["reconciliation_required"]))
    versions = {
        "plan": version_reference(manifest.get("blueprint_plans", {}), progress.get("plan_version")),
        "blueprint": version_reference(manifest.get("blueprints", {})),
        "approved_version": packet["approved_version"],
        "approval_id": packet["approval_id"],
    }
    changes = changed_documents(versions, manifest, slice_review, digest) if issues else {}
    stage_label = STAGE_LABELS.get(stage, stage.replace("_", " ").title())
    if final_review:
        stage_label = "Application completion review"
    elif reviewing_slices and stage not in {"approved", "slice_review"}:
        stage_label = "Slice review"
    return {
        "stage": stage,
        "stage_label": stage_label,
        "current_item": position or None,
        "completed_items": completed,
        "latest_presentation": presentation,
        "pending_question": question,
        "answered_questions": answered,
        "recorded_reply": recorded_reply or latest_review_reply,
        "confirmed_decision_ids": [record["exchange_id"] for record in decisions],
        "recent_confirmed_decisions": decisions[-3:],
        "versions": versions,
        "review_evidence": {
            "workstream_exchange_ids": progress.get("completed_exchange_ids", []),
            "plan_exchange_ids": plan.get("continuation_ids", []),
            "slice_review_ids": slice_review.get("completed_review_ids", []),
            "slice_map": slice_review.get("slice_map"),
            "package_id": slice_review.get("active_package"),
            "pending_slice_continuation": slice_review.get("pending_continue"),
            "final_audit_id": (quality.get("audit") or {}).get("audit_id"),
        },
        "coverage": {"blueprint": packet["blueprint_coverage"], "slice_walkthrough": walkthrough,
                     "skipped_workstreams": progress.get("skipped_workstreams", [])},
        "pending_revisions": {
            "plan_revision_required": plan.get("revision_required", False),
            "blueprint_revisits": progress.get("revisits", []),
            "latest_revision_request": latest_review_reply if latest_review_reply
            and latest_review_reply.get("review_action") == "revise" else None,
            "affected_slices": slice_review.get("changed_dependents", []),
            "slice_review_blockers": slice_review.get("review_blockers", {}),
        },
        "ready_for_discussion": readiness is not None,
        "completed_steps": work.get("completed_steps", []),
        "pending_steps": work.get("pending_steps", []),
        "saved_next_action": packet["next_action"],
        "next_permitted_action": "reconcile" if issues else "wait_for_user",
        "integrity_issues": issues,
        "changed_documents": changes,
    }


def briefing(packet):
    recovery = packet["conversation_recovery"]
    issues = recovery["integrity_issues"]
    lines = ["Prototype-to-Production — Session Recovery",
             f"{packet['initiative_id']} | {recovery['stage_label']} | "
             + ("Review evidence needs reconciliation" if issues else "Review paused")]
    if item := recovery["current_item"]:
        if item["name"]:
            revision = f" revision {item['revision']}" if item.get("revision") is not None else ""
            order = f" ({item['number']} of {item['total']})" if item.get("number") else ""
            lines.append(f"Current {item['kind']} — {item['name']}{revision}{order}")
    if completed := recovery["completed_items"]:
        lines.append("Reviewed — " + ", ".join(completed))
    elif completed := recovery["completed_steps"]:
        lines.append("Completed — " + ", ".join(completed))
    if skipped := recovery["coverage"]["skipped_workstreams"]:
        lines.append("Skipped by request — " + ", ".join(skipped))
    versions = recovery["versions"]
    for label in ("plan", "blueprint"):
        if reference := versions[label]:
            lines.append(f"{label.title()} version — {reference['version']}")
    if versions["approval_id"]:
        lines.append(f"Recorded approval — {versions['approval_id']} for {versions['approved_version']}")
    if issues:
        lines.extend(f"BLOCKED: {issue}" for issue in issues)
        for path, changes in recovery["changed_documents"].items():
            scopes = ", ".join(change["review_evidence"] for change in changes)
            lines.append(f"Affected evidence — {path} ({scopes})")
        lines.append("Next — Reconcile the affected evidence and check which reviews or approvals require a fresh decision.")
        return "\n".join(lines)
    for decision in recovery["recent_confirmed_decisions"]:
        lines.append(f"Confirmed — {decision['response']} ({decision['exchange_id']})")
    if reply := recovery["recorded_reply"]:
        if reply.get("review_action"):
            lines.append(f"REVIEW CHECKPOINT {reply['review_target']}: {reply['review_action']}")
        lines.append(f"Recorded reply — {reply['response']}")
    if question := recovery["pending_question"]:
        if question.get("review_target"):
            lines.append(f"REVIEW CHECKPOINT {question['review_target']}: waiting for reply")
        lines.append(f"Pending question — {question['question']}")
    if presentation := recovery["latest_presentation"]:
        paths = presentation.get("evidence_paths", []) + presentation.get("illustration_paths", [])
        if path := presentation.get("walkthrough_path"):
            paths = [path, *paths]
        if paths:
            lines.append("Saved presentation — " + ", ".join(dict.fromkeys(paths)))
    if recovery["ready_for_discussion"] and not recovery["coverage"]["slice_walkthrough"]:
        lines.append("Ready for discussion")
    if walkthrough := recovery["coverage"]["slice_walkthrough"]:
        lines.append(f"Where we stopped — {walkthrough['position']}")
        if remaining := walkthrough["remaining_topics"]:
            lines.append("Still to discuss — " + ", ".join(topic.replace("_", " ") for topic in remaining))
    if affected := recovery["pending_revisions"]["affected_slices"]:
        lines.append("Affected dependent slices — " + ", ".join(affected))
    lines.extend(f"BLOCKED: {item}" for item in packet["blockers"])
    lines.append(f"Saved next step — {recovery['saved_next_action']}")
    lines.append("Wait for your instruction before continuing the review.")
    return "\n".join(lines)
