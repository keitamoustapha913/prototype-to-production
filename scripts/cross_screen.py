"""Targeted browser inspections for changes to shared UI source."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlsplit

try:
    from scripts import project_state, visual_integrity as visual
except ModuleNotFoundError:
    import project_state
    import visual_integrity as visual


CHECKS = {"visual", "interaction", "blueprint"}
LABEL = "Cross-screen visual regression"


def require(condition, message):
    if not condition:
        raise visual.VisualIntegrityError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def assignments(manifest):
    return {key: item for key, item in manifest.get("slices", {}).items()
            if item.get("status") != "cancelled" and not item.get("superseded_by")}


def affected_slices(manifest, plan, *, known=None):
    affected = {plan["slice_id"], *(key for screen in plan["screens"] for key in screen["slice_ids"])}
    active = assignments(manifest) if known is None else known
    while True:
        consumers = {key for key, item in active.items() if affected.intersection(item.get("dependencies", []))}
        if consumers <= affected:
            return affected
        affected.update(consumers)


def scoped_paths(plan):
    return sorted(set(plan["component_paths"]).union(*(screen["source_paths"] for screen in plan["screens"])))


def source_hashes(directory, paths):
    hashes = {}
    for relative in paths:
        path = project_state.implementation_file(directory, relative, allow_missing=True)
        hashes[relative] = project_state.digest_of(path) if path is not None else None
    return hashes


def checked_paths(values, *, required=False):
    require(visual._ids(values) and (values or not required), "Supply distinct project-relative source files")
    for path in values:
        try:
            _, tree = project_state.owned_path(path)
        except project_state.StateError as error:
            raise visual.VisualIntegrityError(str(error)) from error
        require(not tree and Path(path).as_posix() == path, "Use canonical source file paths")
    return values


def approved_cases(directory, manifest):
    approval = manifest["approvals"][manifest["active_approval_id"]]
    if project_state.initiative_mode(manifest) == "prototype":
        return {case["case_id"]: (case, [], []) for case in approval.get("visual_cases", [])}
    spec = project_state.blueprint_state(directory, manifest).load("blueprints", approval["approved_version"])
    return {
        f"{feature['feature_id']}/{case['case_id']}": (case, [
            {field: check[field] for field in ("check_id", "expected", "measurement")}
            for check in feature["acceptance_checks"] if check["kind"] == "visual"
            and ("case_ids" not in check or case["case_id"] in check["case_ids"])
        ], feature.get("protected_visual_decisions", []))
        for feature in spec["features"] for case in feature.get("visual_contract", {}).get("render_cases", [])
    }


def planned_case(value, route, approved):
    require(isinstance(value, dict) and visual._text(value.get("case_id"))
            and visual._text(value.get("reason")) and visual._text(value.get("expectation")),
            "Each case needs an ID, selection reason and expected visual and interaction behavior")
    state, render = value.get("state"), value.get("renderCase")
    require(visual._valid_context(state) and state.get("route") == route and isinstance(render, dict),
            "Plan the case's route, theme, data, interaction, active control and open menus")
    approved_id = value.get("approved_case")
    criteria, protections = [], []
    if approved_id is not None:
        require(isinstance(approved_id, str) and approved_id in approved, "Unknown approved case reference")
        locked, criteria, protections = approved[approved_id]
        require(locked["route"] == route and all(state.get(field) == locked[field]
                for field in ("state", "role", "fixture", "state_setup") if field in locked),
                "Case state must match its approved route, state, role and fixture")
        for key in ("route", "viewport", "ready_selector", "setup", "command", "observations",
                    "interaction_assertions", "color_scheme", "design_tokens"):
            if key in locked:
                require(key not in render or render[key] == locked[key], f"Use the approved browser case {key}")
        render = {**locked, **render}
    require(render.get("route") == route and visual._valid_viewport(render.get("viewport"))
            and visual._text(render.get("ready_selector")), "Each case requires a route, viewport and visible component selector")
    assertions = visual.check_interaction_assertions(value.get("interaction_assertions", []))
    require(assertions or render.get("interaction_assertions"), "Plan at least one browser interaction or semantics assertion")
    checks = value.get("checks", [])
    require(isinstance(checks, list), "Additional browser measurements must be an array")
    return {**value, "renderCase": render, "criteria": criteria, "protections": protections}


def related_assignments(directory, manifest):
    known = assignments(manifest)
    review = manifest.get("slice_review", {})
    package_id = review.get("active_package")
    if package_id:
        _, _, package = project_state.slice_review_state(directory, manifest).package(key=package_id)
        known = {**package["slices"], **known}
    return known


def make_plan(directory, manifest, slice_id, payload, *, existing=None):
    assignment = project_state.visual_inspection_assignment(directory, manifest, slice_id)
    require(isinstance(payload, dict), "Plan must be a JSON object")
    change_id = payload.get("change_id")
    require(isinstance(change_id, str) and visual.SLICE_ID.fullmatch(change_id)
            and visual._text(payload.get("summary")), "Provide a stable change ID and describe the change")
    previous = manifest.get("cross_screen_changes", {})
    require(existing or change_id not in previous, "Change ID is already registered")
    paths = checked_paths(payload.get("component_paths"), required=True)
    require(all(any(project_state.ownership_contains(owner, path) for owner in assignment["allowed_paths"])
                for path in paths), "Shared component changes must stay within slice ownership")
    screens = payload.get("screens")
    require(isinstance(screens, list) and screens, "Inventory all affected rendered screens")
    known = related_assignments(directory, manifest)
    approved = approved_cases(directory, manifest)
    routes, case_ids, saved = set(), set(), []
    for screen in screens:
        require(isinstance(screen, dict), "Invalid screen")
        route = screen.get("route")
        require(isinstance(route, str) and route.startswith("/") and not route.startswith("//")
                and not urlsplit(route).netloc and route not in routes and visual._text(screen.get("reason")),
                "Each distinct application route needs dependency evidence")
        routes.add(route)
        slice_ids = screen.get("slice_ids")
        require(visual._ids(slice_ids) and set(slice_ids) <= known.keys(),
                "Name the known slices that use this screen, or use an empty array for existing unassigned UI")
        source_paths = checked_paths(screen.get("source_paths", []))
        require(source_paths or visual._text(screen.get("source_note")),
                "Identify consumer source files or explain where the dependency is defined")
        cases = screen.get("cases")
        require(isinstance(cases, list) and cases, "Plan a browser case for every affected screen")
        plan_cases = [planned_case(case, route, approved) for case in cases]
        for case in plan_cases:
            require(case["case_id"] not in case_ids, "Case IDs must be unique across screens")
            case_ids.add(case["case_id"])
        on_route = {key for key, (locked, _, _) in approved.items() if locked["route"] == route}
        planned = {case["approved_case"] for case in plan_cases if case.get("approved_case")}
        exclusions = screen.get("unaffected_approved_cases", {})
        require(isinstance(exclusions, dict) and all(visual._text(reason) for reason in exclusions.values())
                and not planned.intersection(exclusions) and planned.union(exclusions) == on_route,
                "Select each affected approved case and explain why any other approved case on this route is unaffected")
        saved.append({**screen, "slice_ids": sorted(slice_ids), "source_paths": source_paths, "cases": plan_cases})

    approval = manifest["active_approval_id"]
    other_plans = [plan for plan in current_plans(manifest) if plan["change_id"] != change_id]
    for old in other_plans:
        if not set(paths).intersection(old["component_paths"]):
            continue
        require(set(old["component_paths"]) <= set(paths),
                f"Retain the shared source group from {old['change_id']}")
        require(old["status"] == "verified" or old["approval_id"] != approval,
                f"Continue open shared change {old['change_id']} before opening another for the same source")
        require({screen["route"] for screen in old["screens"]} <= routes,
                f"Retain the known consumer routes from {old['change_id']}")
        for old_screen in old["screens"]:
            latest_screen = next(screen for screen in saved if screen["route"] == old_screen["route"])
            require(set(old_screen["slice_ids"]).intersection(known) <= set(latest_screen["slice_ids"]),
                    f"Retain active consumer slices on {old_screen['route']}")
    plan = {"change_id": change_id, "slice_id": slice_id, "summary": payload["summary"], "component_paths": paths,
            "approval_id": approval, "screens": saved, "created_at": now(), "status": "pending",
            "baselines": {}, "captures": {}, "attempts": []}
    if manifest.get("visual_baselines_required"):
        plan["pre_edit_check_required"] = True
    plan["original_sources"] = source_hashes(directory, scoped_paths(plan))
    for old in other_plans:
        overlap = set(paths).intersection(old["component_paths"])
        if overlap:
            reference = (old["attempts"][-1]["sources"] if old["status"] == "verified"
                         else old["original_sources"])
            require(all(plan["original_sources"][path] == reference[path] for path in overlap),
                    f"Restore baseline source for {old['change_id']} before registering a new change")
    if existing:
        require(existing["status"] != "verified" and paths == existing["component_paths"]
                and payload["summary"] == existing["summary"],
                "Extend only an open change with its original summary and shared source group")
        require(all(plan["original_sources"].get(path) == digest for path, digest in existing["original_sources"].items()),
                "Restore the original scoped source before extending coverage")
        for old_screen in existing["screens"]:
            screen = next((item for item in saved if item["route"] == old_screen["route"]), None)
            require(screen and set(old_screen["slice_ids"]) <= set(screen["slice_ids"])
                    and set(old_screen["source_paths"]) <= set(screen["source_paths"]),
                    "Extensions retain original routes, source and slices")
        old_cases, new_cases = case_specs(existing), case_specs(plan)
        require(all(new_cases.get(key) == spec for key, spec in old_cases.items()),
                "Extensions retain the original case definitions")
        require(saved != existing["screens"], "An extension must add affected screens, cases or dependencies")
        updated = {**existing, "screens": saved, "original_sources": plan["original_sources"], "updated_at": now()}
        updated.pop("pre_edit_check", None)
        return updated
    return plan


def hydrate_baselines(directory, manifest, plan):
    catalog = project_state.sibling_module("visual_baseline")
    catalog.require_history(directory, manifest)
    for screen in plan["screens"]:
        paths = set(screen["source_paths"]) | set(plan["component_paths"])
        for spec in screen["cases"]:
            key = spec["case_id"]
            if key in plan["baselines"]:
                continue
            receipt = catalog.reusable(directory, manifest, spec, paths, plan["original_sources"])
            if receipt:
                # One discovery capture can support multiple approved cases on the same screen.
                capture_key = f"{receipt['case']['capture_id']}/{key}"
                plan["baselines"][key] = capture_key
                plan["captures"][capture_key] = receipt
                plan.get("baseline_gaps", {}).pop(key, None)
            elif spec.get("baseline_id"):
                entry = manifest["visual_baselines"][spec["baseline_id"]]
                gap = entry.get("gap") if not entry["original"] else None
                plan.setdefault("baseline_gaps", {})[key] = (gap["reason"] if gap else
                    "The saved original is unavailable or belongs to a different source version")


def case_specs(plan):
    return {case["case_id"]: case for screen in plan["screens"] for case in screen["cases"]}


def baseline_readiness(directory, plan):
    gaps = {}
    for key in case_specs(plan):
        receipt = plan["captures"].get(plan["baselines"].get(key))
        if not receipt:
            gaps[key] = plan.get("baseline_gaps", {}).get(key, "Capture the original rendered state")
            continue
        try:
            project_state.sibling_module("visual_baseline").verify_images(receipt["case"], directory)
        except (OSError, ValueError) as error:
            gaps[key] = str(error)
    return {"ready": not gaps, "gaps": gaps}


def require_ready(directory, manifest, slice_id, payload):
    require(isinstance(payload, dict), "Supply the change ID")
    require_history(directory, manifest)
    plan = get_plan(manifest, slice_id, payload.get("change_id"))
    require(source_hashes(directory, scoped_paths(plan)) == plan["original_sources"],
            "Check original baseline readiness before modifying scoped source")
    readiness = baseline_readiness(directory, plan)
    require(readiness["ready"], "Original baseline gaps: " + "; ".join(
        f"{key}: {reason}" for key, reason in readiness["gaps"].items()))
    return readiness


def save(directory, manifest, plan, action, request, **event):
    manifest.setdefault("cross_screen_changes", {})[plan["change_id"]] = plan
    project_state.save_workflow_state(directory, manifest, transition=(request, [{
        "type": f"cross_screen_{action}", "change_id": plan["change_id"], "slice_id": plan["slice_id"],
        "plan_sha256": sha256(visual._canonical(plan)).hexdigest(), **event,
    }], {}))


def get_plan(manifest, slice_id, change_id):
    require(isinstance(change_id, str), "Provide a change ID")
    plan = manifest.get("cross_screen_changes", {}).get(change_id)
    require(plan and plan["slice_id"] == slice_id and plan["approval_id"] == manifest.get("active_approval_id")
            and plan["status"] != "abandoned", "Use an active cross-screen change for the originating slice")
    require(plan in current_plans(manifest), "Use the current plan for this shared source")
    return plan


def require_after_sources(plan, sources):
    if plan["status"] != "verified":
        return
    previous = plan["attempts"][-1]["sources"]
    require(all(sources[path] == previous[path] for path in plan["component_paths"]),
            "A verified shared change requires a new plan for subsequent shared edits")
    require(sources != previous, "This change already has verified evidence for the current source")


def capture(directory, slice_id, payload):
    request = {"command": "cross-screen capture", "slice_id": slice_id, "input": payload}
    case_id, phase = payload.get("case_id"), payload.get("phase")
    with project_state.coordinator_lock(directory):
        recovered = project_state.recover_workflow_transition(directory, request)
        manifest = project_state.manifest_at(directory)
        plan = get_plan(manifest, slice_id, payload.get("change_id"))
        require_history(directory, manifest)
        if recovered:
            receipt = max((item for item in plan["captures"].values() if item["phase"] == phase
                           and item["case"]["case_id"] == case_id), key=lambda item: item["captured_at"])
            return receipt["case"]
    require(phase in ("before", "after") and isinstance(case_id, str) and case_id in case_specs(plan), "Choose a planned case and before/after phase")
    spec = case_specs(plan)[case_id]
    sources = source_hashes(directory, scoped_paths(plan))
    if phase == "before":
        require(sources == plan["original_sources"], "A before capture requires the original planned source bytes")
        require(case_id not in plan["baselines"], "Keep the first baseline capture")
        require(spec.get("baseline_id") is None, "Restore or recover the explicitly linked original visual baseline")
    else:
        require_after_sources(plan, sources)
    require(visual._text(payload.get("appUrl")) and visual._text(payload.get("browser")),
            "Provide the running application URL and Chromium executable")
    baseline = project_state.sibling_module("visual_baseline")
    head = baseline.revision(directory)
    state = project_state.blueprint_state(directory, manifest)
    existing = [path for path, digest in sources.items() if digest is not None]
    snapshot = state.discovery_snapshot(existing)
    build = {"fingerprint": state.build_fingerprint(snapshot), "sources": snapshot}
    criteria = spec["criteria"]
    case, evidence = visual.browser_case(
        directory, slice_id, state, {**spec, "browser": payload["browser"], "appUrl": payload["appUrl"]},
        spec["renderCase"], spec["state"], case_id, build,
        [*(rule["measurement"] for rule in criteria), *spec.get("checks", [])], criteria,
    )
    require(isinstance(case.get("browser_evidence"), dict), "Cross-screen captures require browser evidence")
    require(case["viewport"] == spec["renderCase"]["viewport"], "Browser viewport differs from the plan")
    with project_state.coordinator_lock(directory):
        current = project_state.manifest_at(directory)
        require_history(directory, current)
        original = get_plan(current, slice_id, plan["change_id"])
        require(original.get("updated_at", original["created_at"]) == plan.get("updated_at", plan["created_at"])
                and baseline.revision(directory) == head
                and source_hashes(directory, scoped_paths(plan)) == sources
                and project_state.blueprint_state(directory, current).discovery_snapshot(existing) == snapshot,
                "Plan or served workspace changed during capture")
        if phase == "before":
            require(case_id not in original["baselines"], "Keep the first baseline capture")
            original["baselines"][case_id] = case["capture_id"]
        else:
            require_after_sources(original, sources)
        receipt = {"case": case, "sources": sources, "phase": phase, "build": build["fingerprint"],
                   "captured_at": now(), "browser": payload["browser"], "revision": head, **evidence}
        original["captures"][case["capture_id"]] = receipt
        save(directory, current, original, "capture", request, capture_id=case["capture_id"], phase=phase)
    return case


def comparison(directory, slice_id, before, after):
    from PIL import Image, ImageChops

    with Image.open(visual.resolve_evidence_path(directory, before)) as old, Image.open(
            visual.resolve_evidence_path(directory, after)) as new:
        if old.size != new.size:
            bounds = {"x": 0, "y": 0, "width": 1, "height": 1}
        else:
            # Browser screenshots are opaque. Preserve both source images and mark
            # the bounding region of all changed pixels on copies.
            box = ImageChops.difference(old.convert("RGB"), new.convert("RGB")).getbbox()
            bounds = (dict(x=box[0]/old.width, y=box[1]/old.height,
                           width=(box[2]-box[0])/old.width, height=(box[3]-box[1])/old.height)
                      if box else None)
    result = {"before": before, "latest": after, "pixels_changed": bounds is not None}
    if bounds:
        result.update(normalized_rect=bounds,
                      annotated_before=visual._annotated_png(directory, slice_id, before, bounds),
                      annotated_latest=visual._annotated_png(directory, slice_id, after, bounds))
    return result


def review_case(directory, plan, spec, row, sources, since):
    checks = row.get("checks")
    require(isinstance(checks, dict) and set(checks) == CHECKS
            and all(isinstance(check, dict) and check.get("result") in ("pass", "fail", "not_checked")
                    and visual._text(check.get("note")) for check in checks.values()),
            "Record visual, interaction and blueprint results and evidence notes for every case")
    key = spec["case_id"]
    baseline = plan["captures"].get(plan["baselines"].get(key), {})
    base = baseline.get("case")
    capture_id = row.get("capture_id")
    latest, receipt = None, {}
    if capture_id is not None:
        receipt = plan["captures"].get(capture_id) if isinstance(capture_id, str) else None
        require(receipt and receipt["case"]["case_id"] == key and receipt["phase"] == "after"
                and receipt["captured_at"] > since and receipt["sources"] == sources,
                "Use a fresh after capture for this case and current source")
        latest = receipt["case"]
        prior_version = base.get("browser_evidence", {}).get("browser_version") if base else None
        if baseline and not prior_version:
            require(baseline["browser"] == receipt["browser"],
                    "Compare original and updated UI using the same browser")
    if base and project_state.sibling_module("visual_baseline").evidence_error(directory, baseline):
        base = None
    pair = {**row, "before": base, "after": latest, "comparisons": [],
            "baseline_id": baseline.get("baseline_id"),
            "before_revision": baseline.get("revision"), "after_revision": receipt.get("revision"),
            "before_build": baseline.get("build"), "after_build": receipt.get("build")}
    if not base or not latest:
        require(visual._text(row.get("unavailable_reason")) and not all(
            check["result"] == "pass" for check in checks.values()), "Missing evidence needs a reason and incomplete checks")
        if not latest:
            require(all(check["result"] == "not_checked" for check in checks.values()),
                    "An unrendered case is not verified")
        if not base:
            require(checks["visual"]["result"] != "pass", "A visual comparison needs the original capture")
    else:
        pair["comparisons"] = [comparison(directory, plan["slice_id"], base[field], latest[field])
                               for field in ("full_screenshot", "closeup_screenshot")]
    if latest:
        outcomes = latest["browser_evidence"]["interaction_assertions"]
        expected_result = "pass" if outcomes and all(item["result"] == "pass" for item in outcomes) else "fail"
        require(checks["interaction"]["result"] == expected_result, "Report the actual browser assertion outcome")
        visual._check_blueprint_measurements(latest, spec["criteria"], checks["blueprint"]["result"],
                                             [item["check_id"] for item in spec["criteria"]])
        decision_ids = row.get("protected_decision_ids", [])
        require(visual._ids(decision_ids) and (checks["blueprint"]["result"] != "pass" or set(decision_ids) ==
                {decision["decision_id"] for decision in spec["protections"]}),
                "A blueprint pass must cover every protected decision for the approved case")
    pair["status"] = ("failed" if any(check["result"] == "fail" for check in checks.values())
                      else "verified" if all(check["result"] == "pass" for check in checks.values())
                      else "not_verified")
    return pair


def review_findings(plan, rows, findings):
    require(isinstance(findings, list) and all(isinstance(item, dict) for item in findings),
            "Findings must be an array of objects")
    by_case = {row["case_id"]: row for row in rows}
    specs = case_specs(plan)
    old = {item["finding_id"]: item for attempt in plan["attempts"] for item in attempt["findings"]}
    seen = set()
    for item in findings:
        key, case, status = item.get("finding_id"), item.get("case_id"), item.get("status")
        require(visual._text(key) and key not in seen and isinstance(case, str) and case in by_case
                and status in ("open", "fixed", "improved", "noted")
                and visual._text(item.get("description")) and visual._text(item.get("severity"))
                and isinstance(item.get("dimension"), str) and item["dimension"] in visual.OBSERVATION_DIMENSIONS,
                "Invalid cross-screen finding")
        seen.add(key)
        previous = old.get(key, {})
        require(not previous or (case, item["dimension"]) == (previous["case_id"], previous["dimension"]),
                "Retain the original case and dimension when rechecking a finding")
        require(previous.get("status") not in ("open", "improved") or status != "noted",
                "Recheck an open defect before clearing its hold")
        spec = specs[case]
        visual._check_classification(item, {decision["decision_id"] for decision in spec["protections"]})
        require((status == "noted") == (item["classification"] in ("approved_difference", "design_concern")),
                "Use noted status for approved differences and design concerns")
        require(previous.get("classification") != "approval_conflict" or
                (item["classification"] == "approval_conflict" and status == "open" and not item.get("correction")),
                "Resolve approval conflicts through an explicit amendment")
        if status in ("fixed", "improved"):
            require(previous.get("status") in ("open", "improved") and visual._text(item.get("correction"))
                    and visual._text(item.get("verification")), "Recheck a recorded defect and explain its repair")
            if status == "fixed":
                require(by_case[case]["status"] == "verified", "A fixed finding needs passing checks on its case")
    require({key for key, item in old.items() if item["status"] in ("open", "improved")} <= seen,
            "Retain or explicitly recheck all open findings")
    require({row["case_id"] for row in rows if row["status"] == "failed"} <= {
        item["case_id"] for item in findings if item["status"] in ("open", "improved")},
        "Record a finding for each failed case")
    return visual._retry_policy(plan["attempts"], [item for item in findings if item["classification"] == "confirmed_defect"])


def record(directory, plan, payload):
    specs = case_specs(plan)
    rows = payload.get("cases")
    require(isinstance(rows, list) and all(isinstance(row, dict) and visual._text(row.get("case_id")) for row in rows)
            and len(rows) == len(specs) and {row["case_id"] for row in rows} == specs.keys(),
            "Account for every affected case, including unavailable screens")
    sources = source_hashes(directory, scoped_paths(plan))
    require_after_sources(plan, sources)
    since = max(plan.get("updated_at", plan["created_at"]),
                plan.get("pre_edit_check", plan["created_at"]),
                plan["attempts"][-1]["recorded_at"] if plan["attempts"] else plan["created_at"])
    cases = [review_case(directory, plan, specs[row["case_id"]], row, sources, since) for row in rows]
    findings = payload.get("findings", [])
    retry = review_findings(plan, cases, findings)
    verified = (all(row["status"] == "verified" for row in cases)
                and not any(item["status"] in ("open", "improved") for item in findings))
    if verified:
        require(not plan.get("pre_edit_check_required") or plan.get("pre_edit_check"),
                "Run the original baseline readiness check before editing. Restore the original source to recover.")
        require(any(sources[path] != plan["original_sources"][path] for path in plan["component_paths"]),
                "Record a verified shared change only after its source changes")
    status = "verified" if verified else "unresolved"
    attempt = {"status": status, "sources": sources, "cases": cases, "findings": findings,
               "recorded_at": now(), "retry_policy": retry}
    require(source_hashes(directory, scoped_paths(plan)) == sources, "Scoped source changed during review")
    plan["attempts"].append(attempt)
    plan["status"] = status
    return attempt


def change(directory, slice_id, action, payload):
    require(isinstance(payload, dict), "Use a JSON object")
    if action == "capture":
        return capture(directory, slice_id, payload)
    request = {"command": f"cross-screen {action}", "slice_id": slice_id, "input": payload}
    with project_state.coordinator_lock(directory):
        recovered = project_state.recover_workflow_transition(directory, request)
        manifest = project_state.manifest_at(directory)
        require_history(directory, manifest)
        if action == "ready":
            plan = get_plan(manifest, slice_id, payload.get("change_id"))
            if not recovered:
                require_ready(directory, manifest, slice_id, payload)
                plan["pre_edit_check"] = now()
                save(directory, manifest, plan, action, request)
            return {"ready": True, "gaps": {}, "checked_at": plan["pre_edit_check"]}
        if recovered:
            return manifest["cross_screen_changes"][payload["change_id"]]
        if action == "plan":
            plan = make_plan(directory, manifest, slice_id, payload)
            if manifest["stage"] == "integrate":
                manifest["stage"] = "implement"
                manifest["next_action"] = f"Verify cross-screen change {plan['change_id']} before integration"
        else:
            require(action in ("record", "abandon", "extend", "reuse"), "Unknown cross-screen action")
            plan = get_plan(manifest, slice_id, payload.get("change_id"))
            if action == "record":
                record(directory, plan, payload)
            elif action == "extend":
                plan = make_plan(directory, manifest, slice_id, payload, existing=plan)
            elif action == "reuse":
                require(source_hashes(directory, scoped_paths(plan)) == plan["original_sources"],
                        "Restore original scoped source before resolving baseline gaps")
            else:
                require(plan["status"] != "verified" and visual._text(payload.get("reason"))
                        and source_hashes(directory, scoped_paths(plan)) == plan["original_sources"],
                        "Abandonment requires a reason and restoration of all original source bytes")
                plan.update(status="abandoned", abandonment_reason=payload["reason"])
        if action in ("plan", "extend", "reuse"):
            hydrate_baselines(directory, manifest, plan)
        save(directory, manifest, plan, action, request, status=plan["status"])
        return plan


def current_plans(manifest):
    """Keep the newest active plan for each shared source group."""
    covered = set()
    current = []
    for plan in sorted(manifest.get("cross_screen_changes", {}).values(),
                       key=lambda item: item["created_at"], reverse=True):
        if plan["status"] == "abandoned":
            continue
        paths = set(plan["component_paths"])
        if not paths <= covered:
            current.append(plan)
        covered.update(paths)
    return current


def history_errors(directory, manifest):
    changes = manifest.get("cross_screen_changes", {})
    journal = directory / "decisions.jsonl"
    if not journal.is_file():
        return [f"{LABEL} history is missing"] if changes else []
    receipts = {}
    try:
        with journal.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if event.get("type", "").startswith("cross_screen_"):
                    receipts[event["change_id"]] = event["plan_sha256"]
        return [f"{LABEL} {key} differs from its decision journal"
                for key in sorted(receipts.keys() | changes.keys())
                if key not in changes or receipts.get(key) != sha256(visual._canonical(changes[key])).hexdigest()]
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as error:
        return [f"{LABEL} history is unavailable: {error}"]


def require_history(directory, manifest):
    errors = history_errors(directory, manifest)
    require(not errors, "; ".join(errors))


def current_status(directory, plan):
    if plan["status"] != "verified":
        return plan["status"]
    attempt = plan["attempts"][-1]
    try:
        if source_hashes(directory, scoped_paths(plan)) != attempt["sources"]:
            return "stale"
        visual.verify_saved_images(directory, [{"cases": [
            case
            for row in attempt["cases"]
            for case in ({**row["before"], "comparisons": []}, {**row["after"], "comparisons": row["comparisons"]})
        ]}])
    except (OSError, ValueError, project_state.StateError):
        return "evidence unavailable"
    return "verified"


def holds(directory, manifest, *, slice_id=None):
    """Return current holds with their impact, separately from slice delivery state."""
    current = current_plans(manifest)
    known = related_assignments(directory, manifest) if current else {}
    result = []
    for plan in current:
        affected = affected_slices(manifest, plan, known=known)
        if slice_id is not None and slice_id not in affected:
            continue
        status = current_status(directory, plan)
        if status != "verified":
            result.append((affected, f"{LABEL} {plan['change_id']} is {status}"))
    return result


def blockers(directory, manifest, slice_id=None):
    return [reason for _, reason in holds(directory, manifest, slice_id=slice_id)]


def review_changes(directory, manifest, slice_id=None):
    """Return original-change groups with historical attempts for the viewer."""
    if not manifest.get("cross_screen_changes"):
        return []
    current = {plan["change_id"] for plan in current_plans(manifest)}
    known = related_assignments(directory, manifest)
    result = []
    for plan in manifest.get("cross_screen_changes", {}).values():
        affected = affected_slices(manifest, plan, known=known)
        if slice_id is not None and slice_id not in affected:
            continue
        status = plan["status"]
        if plan["change_id"] in current:
            status = current_status(directory, plan)
        elif status != "abandoned":
            status = "historical"
        original = {case_id: plan["captures"][capture_id] for case_id, capture_id in plan["baselines"].items()}
        result.append(dict(deepcopy({key: value for key, value in plan.items() if key != "captures"}),
                           affected_slices=sorted(affected), current_status=status,
                           baseline_readiness=baseline_readiness(directory, plan),
                           baseline_context={case_id: {field: receipt.get(field) for field in (
                               "build", "revision", "captured_at", "sources", "baseline_id")}
                               for case_id, receipt in original.items()},
                           baseline_cases={case_id: receipt["case"] for case_id, receipt in original.items()}))
    return result


def require_planned_paths(manifest, slice_id, paths, implemented):
    current = current_plans(manifest)
    known = {path for plan in manifest.get("cross_screen_changes", {}).values() for path in plan["component_paths"]}
    require(known.intersection(implemented) <= set(paths), "Include known shared UI files in shared_ui_paths")
    registered = {path for plan in current if plan["slice_id"] == slice_id
                  and plan["approval_id"] == manifest.get("active_approval_id")
                  for path in plan["component_paths"]}
    require(set(paths) <= registered, "Declare a pre-edit cross-screen plan for every reported shared UI path")
