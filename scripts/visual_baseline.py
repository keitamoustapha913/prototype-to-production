"""Preserve original rendered UI and resolve its approved and implemented references."""

from hashlib import sha256
import json
from urllib.parse import urlsplit

try:
    from scripts import cross_screen, project_state, visual_integrity as visual
except ModuleNotFoundError:
    import cross_screen
    import project_state
    import visual_integrity as visual


GROUP = "visual_baselines"
EVENT = "visual_baseline_recorded"


def revision(directory):
    head, _ = project_state.git_info(directory.parents[2])
    return head


def definition(payload):
    key = payload.get("baseline_id")
    state, render = payload.get("state"), payload.get("renderCase")
    features = payload.get("baseline_feature_ids")
    cross_screen.require(isinstance(key, str) and visual.SLICE_ID.fullmatch(key)
                         and visual._ids(features) and features and visual._text(payload.get("reason")),
                         "Provide a baseline ID, discovery feature IDs and an adaptive coverage reason")
    route = state.get("route") if isinstance(state, dict) else None
    cross_screen.require(visual._valid_context(state) and isinstance(route, str) and route.startswith("/")
                         and not route.startswith("//") and not urlsplit(route).netloc and isinstance(render, dict)
                         and render.get("route") == route and visual._valid_viewport(render.get("viewport"))
                         and visual._text(render.get("ready_selector")) and "command" not in render,
                         "Provide an original local route, context, viewport and component selector")
    checks = payload.get("checks", [])
    cross_screen.require(isinstance(checks, list), "Baseline measurements must be an array")
    assertions = visual.check_interaction_assertions(payload.get("interaction_assertions", []))
    return {"baseline_id": key, "baseline_feature_ids": features, "reason": payload["reason"],
            "state": state, "renderCase": render, "checks": checks, "interaction_assertions": assertions,
            "source_paths": cross_screen.checked_paths(payload.get("source_paths"), required=True)}


def verify_images(case, directory):
    for field in ("full_screenshot", "closeup_screenshot"):
        visual.resolve_evidence_path(directory, case[field])


def evidence_error(directory, original):
    if original:
        try:
            verify_images(original["case"], directory)
        except (OSError, ValueError) as error:
            return str(error)
    return None


def served_sources(original):
    return {item["source"]: item["sha256"] for item in (original or {}).get("served", []) if "source" in item}


def history_errors(directory, manifest):
    records = manifest.get(GROUP, {})
    receipts = {}
    path = directory / "decisions.jsonl"
    try:
        if path.is_file():
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    event = json.loads(line)
                    if event.get("type") == EVENT:
                        receipts[event["baseline_id"]] = event["sha256"]
        return [f"Visual baseline {key} differs from its decision journal"
                for key in sorted(receipts.keys() | records.keys())
                if key not in records or receipts.get(key) != sha256(visual._canonical(records[key])).hexdigest()]
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as error:
        return [f"Visual baseline history is unavailable: {error}"]


def require_history(directory, manifest):
    errors = history_errors(directory, manifest)
    cross_screen.require(not errors, "; ".join(errors))


def discovery_errors(directory, manifest, candidate, sources, *, ui_features):
    """Validate the selected historical cases referenced by an immutable discovery audit."""
    records = manifest.get(GROUP, {})
    if not manifest.get("visual_baselines_required") and "visual_baseline_ids" not in candidate and not records:
        return []
    identifiers = candidate.get("visual_baseline_ids", [] if not ui_features else None)
    if not visual._ids(identifiers) or (ui_features and not identifiers):
        return ["Record the selected original visual baseline IDs (captures or explicit gaps) in discovery"]
    errors = history_errors(directory, manifest)
    for key in identifiers:
        entry = records.get(key)
        if not entry or any(ui_features.get(feature) != entry["definition"]["state"]["route"]
                            for feature in entry["definition"]["baseline_feature_ids"]):
            errors.append(f"Visual baseline {key} must reference discovered UI features on its original route")
        elif not manifest.get("approvals") and any(path not in sources or sources[path] != digest
                                                   for path, digest in (entry["sources"] | served_sources(
                                                       entry["original"])).items() if digest is not None):
            errors.append(f"Visual baseline {key} differs from the original discovery source")
        elif error := evidence_error(directory, entry["original"]):
            errors.append(error)
    return errors


def historical_audits(directory, manifest):
    audits = manifest.get("discovery_audits", {})
    order = {key: entry["sequence"] for key, entry in audits.items() if "sequence" in entry}
    if len(order) != len(audits):
        journal = directory / "decisions.jsonl"
        if journal.is_file():
            with journal.open(encoding="utf-8") as stream:
                for index, line in enumerate(stream):
                    event = json.loads(line)
                    if event.get("type") == "functionality_discovery_frozen":
                        order.setdefault(event["audit_id"], index)
    return [audits[key] for key in sorted(audits, key=lambda key: order.get(key, len(order)))]


def record(directory, action, payload):
    cross_screen.require(isinstance(payload, dict), "Use a JSON baseline definition")
    spec = definition(payload)
    key = spec["baseline_id"]
    request = {"command": f"visual-baseline {action}", "input": payload}
    with project_state.coordinator_lock(directory):
        recovered = project_state.recover_workflow_transition(directory, request)
        manifest = project_state.manifest_at(directory)
        require_history(directory, manifest)
        old = manifest.get(GROUP, {}).get(key)
        if recovered:
            return old
        observed_sources = cross_screen.source_hashes(directory, spec["source_paths"])
        sources = observed_sources
        head = revision(directory)
        audits = historical_audits(directory, manifest)
        frozen = {}
        for audit in audits:
            for path, digest in audit.get("source_hashes_at_discovery", {}).items():
                frozen.setdefault(path, digest)
        if old:
            cross_screen.require(old["definition"] == spec and action == "capture" and not old.get("original"),
                                 "Keep the immutable original and its definition")
            expected = {path: digest if digest is not None else frozen.get(path)
                        for path, digest in old["sources"].items()}
            cross_screen.require(sources == expected, "Restore the recorded original scoped source to resolve this gap")
        elif audits or manifest.get("approvals"):
            expected = {path: frozen.get(path) for path in observed_sources}
            if action == "capture":
                cross_screen.require(sources == expected, "Original capture after approval requires source bytes from discovery")
            else:
                sources = expected
        existing_paths = [path for path, digest in observed_sources.items() if digest is not None]
        digest_snapshot = project_state.blueprint_state(directory, manifest).discovery_snapshot(
            existing_paths) if action == "capture" else None

    original, gap = None, (old or {}).get("gap")
    if action == "gap":
        cross_screen.require(visual._text(payload.get("unavailable_reason"))
                             and type(payload.get("essential")) is bool,
                             "Record why the original could not be captured and whether it is essential")
        gap = {"reason": payload["unavailable_reason"], "essential": payload["essential"],
               "recorded_at": cross_screen.now()}
    else:
        cross_screen.require(action == "capture" and all(digest is not None for digest in sources.values())
                             and visual._text(payload.get("appUrl")) and visual._text(payload.get("browser")),
                             "Capture requires original source files, a running application and Chromium")
        state = project_state.blueprint_state(directory, manifest)
        build = {"fingerprint": state.build_fingerprint(digest_snapshot), "sources": digest_snapshot}
        case, evidence = visual.browser_case(
            directory, "discovery-baselines", state,
            {**spec, "appUrl": payload["appUrl"], "browser": payload["browser"]},
            spec["renderCase"], spec["state"], key, build, spec["checks"], [])
        if audits or manifest.get("approvals"):
            direct = served_sources(evidence)
            cross_screen.require(all((frozen | sources).get(path) == digest for path, digest in direct.items()),
                                 "Cannot establish original source for a served dependency")
            if any("fingerprint" in item for item in evidence["served"]):
                discovery_build = audits[0].get("visual_baseline_build") if audits else None
                cross_screen.require(build["fingerprint"] == discovery_build,
                                     "Cannot establish the original compiled build")
        original = {"case": case, "build": build["fingerprint"], "browser": payload["browser"],
                    "sources": sources, "revision": head, "captured_at": cross_screen.now(), **evidence}

    with project_state.coordinator_lock(directory):
        current = project_state.manifest_at(directory)
        require_history(directory, current)
        cross_screen.require(current.get(GROUP, {}).get(key) == old
                             and current.get("active_approval_id") == manifest.get("active_approval_id")
                             and current.get("active_discovery_id") == manifest.get("active_discovery_id")
                             and revision(directory) == head
                             and (action == "gap" or project_state.blueprint_state(
                                 directory, current).discovery_snapshot(existing_paths) == digest_snapshot)
                             and cross_screen.source_hashes(directory, spec["source_paths"]) == observed_sources,
                             "Baseline conditions changed during capture")
        result = {"definition": spec, "sources": sources, "revision": (old or {}).get("revision", head),
                  "gap": gap, "original": original}
        current.setdefault(GROUP, {})[key] = result
        project_state.save_workflow_state(directory, current, transition=(request, [{
            "type": EVENT, "baseline_id": key, "sha256": sha256(visual._canonical(result)).hexdigest(),
        }], {}))
        return result


def case_signature(spec):
    render = spec["renderCase"]
    return (spec["state"], render["viewport"], render["ready_selector"], render.get("setup", []),
            render.get("command"), render.get("observations", {}), render.get("color_scheme"),
            visual._assertion_keys([*render.get("interaction_assertions", []), *spec.get("interaction_assertions", [])]))


def reusable(directory, manifest, spec, paths, sources):
    """Return a compatible original for this change and its immutable catalog ID."""
    explicit = spec.get("baseline_id")
    catalog = manifest.get(GROUP, {})
    if explicit is not None:
        cross_screen.require(isinstance(explicit, str) and explicit in catalog,
                             "Unknown visual baseline ID")
    for key, entry in ([(explicit, catalog[explicit])] if explicit else sorted(catalog.items())):
        definition = entry["definition"]
        if explicit:
            cross_screen.require(definition["state"]["route"] == spec["state"]["route"]
                                 and definition["renderCase"]["viewport"] == spec["renderCase"]["viewport"],
                                 "Linked original must cover the same route and viewport")
            cross_screen.require(case_signature(definition) == case_signature(spec)
                                 or visual._text(spec.get("baseline_comparison_note")),
                                 "Explain how the original state or component maps to the approved case")
        elif case_signature(definition) != case_signature(spec):
            continue
        original = entry.get("original")
        if not original or not paths <= original["sources"].keys():
            continue
        expected = original["sources"] | served_sources(original)
        actual = sources | cross_screen.source_hashes(directory, expected.keys() - sources.keys())
        if any(actual.get(path) != digest for path, digest in expected.items()):
            continue
        if any("fingerprint" in item for item in original["served"]):
            state = project_state.blueprint_state(directory, manifest)
            if state.build_fingerprint(state.discovery_snapshot(original["sources"])) != original["build"]:
                continue
        if not explicit and (set(spec["renderCase"].get("design_tokens", []))
                             - set(definition["renderCase"].get("design_tokens", []))
                             or any(check not in definition["checks"] for check in spec.get("checks", []))):
            continue
        if evidence_error(directory, original):
            continue
        return {**original, "phase": "before", "baseline_id": key,
                "case": {**original["case"], "case_id": spec["case_id"]}}
    return None


def design_references(directory, manifest, feature_ids, route, *, approval_id=None):
    key = approval_id or manifest.get("active_approval_id")
    approval = manifest.get("approvals", {}).get(key)
    if not approval:
        return {}
    if project_state.initiative_mode(manifest) == "prototype":
        cases = [case for case in approval.get("visual_cases", []) if case.get("route") == route]
        return {key: {"version": approval.get("approved_version"), "prototype_cases": cases,
                      "artifact_hashes": approval.get("artifact_hashes", {})}}
    version = approval["approved_version"]
    state = project_state.blueprint_state(directory, manifest)
    spec = state.load("blueprints", version)
    features = [{field: feature[field] for field in (
                    "feature_id", "baseline_ids", "classification", "purpose", "anatomy", "behavior_contract",
                    "visual_contract", "acceptance_checks", "protected_visual_decisions") if field in feature}
                for feature in spec["features"] if set(feature["baseline_ids"]) & feature_ids]
    artifact = manifest["blueprints"][version]
    return {key: {"version": version, "path": artifact["path"], "sha256": artifact["sha256"],
                  "features": features, "artifact_hashes": approval.get("artifact_hashes", {})}}


def approved_image(directory, manifest, approval_id, case_id):
    approval = manifest.get("approvals", {}).get(approval_id, {})
    case = next((item for item in approval.get("visual_cases", []) if item["case_id"] == case_id), None)
    cross_screen.require(case is not None, "Unknown approved visual case")
    relative = case["reference_file"]
    try:
        data = project_state.artifact_file(directory, relative).read_bytes()
    except project_state.StateError as error:
        raise visual.VisualIntegrityError(str(error)) from error
    cross_screen.require(data.startswith(b"\x89PNG\r\n\x1a\n")
                         and sha256(data).hexdigest() == approval.get("artifact_hashes", {}).get(relative),
                         "Approved screenshot is altered or unavailable")
    return data


def scope(directory, manifest, entry, references):
    feature_ids = {item["feature_id"] for design in references.values() for item in design.get("features", [])}
    route = entry["definition"]["state"]["route"]
    result = set()
    for key, assignment in cross_screen.related_assignments(directory, manifest).items():
        if set(assignment.get("feature_ids", [])) & feature_ids:
            result.add(key)
            continue
        # Prototype and already issued packages carry approved routes in their locked contracts.
        if assignment.get("contract_file") and key in manifest.get("slices", {}):
            contract = visual._contract(directory, manifest, key)
            cases = contract.get("visual_cases", [])
            if any(case.get("route") == route for case in cases):
                result.add(key)
                continue
        if any(project_state.ownership_contains(owner, path) for owner in assignment.get("allowed_paths", [])
               for path in entry["definition"]["source_paths"]):
            result.add(key)
    return result


def dependent_scope(directory, manifest, slices):
    known = cross_screen.related_assignments(directory, manifest)
    return set().union(*(cross_screen.affected_slices(manifest, {"slice_id": key, "screens": []}, known=known)
                         for key in slices))


def holds(directory, manifest):
    holds = []
    for key, entry in manifest.get(GROUP, {}).items():
        gap = entry.get("gap")
        if (entry["original"] and not evidence_error(directory, entry["original"])
                or not entry["original"] and not gap["essential"]):
            continue
        refs = design_references(directory, manifest, set(entry["definition"]["baseline_feature_ids"]),
                                 entry["definition"]["state"]["route"])
        slices = dependent_scope(directory, manifest, scope(directory, manifest, entry, refs))
        if slices:
            holds.append((slices, f"Original visual baseline {key} is unavailable"))
    return holds


def link_actual(directory, manifest, slice_id, request, render):
    entries = manifest.get(GROUP, {})
    explicit = request.get("baseline_id")
    if not entries and explicit is None:
        return None
    require_history(directory, manifest)
    route, viewport = render["route"], render["viewport"]
    if explicit is not None:
        cross_screen.require(isinstance(explicit, str) and explicit in entries, "Unknown visual baseline ID")
        candidates = [(explicit, entries[explicit])]
    else:
        candidates = entries.items()
    matches = []
    for key, entry in candidates:
        definition = entry["definition"]
        refs = design_references(directory, manifest, set(definition["baseline_feature_ids"]),
                                 definition["state"]["route"])
        if (slice_id in scope(directory, manifest, entry, refs) and definition["state"]["route"] == route
                and definition["renderCase"]["viewport"] == viewport):
            matches.append((key, definition))
    if explicit is not None:
        cross_screen.require(matches, "Link an original for this slice, route and viewport")
        definition = matches[0][1]
        observed = {**request, "renderCase": render}
        cross_screen.require(case_signature(definition) == case_signature(observed)
                             or visual._text(request.get("baseline_comparison_note")),
                             "Explain how the original state or component maps to the approved case")
        return explicit
    exact = [key for key, definition in matches
             if case_signature(definition) == case_signature({**request, "renderCase": render})]
    cross_screen.require(len(exact) == 1 or not matches,
                         "Choose the visual baseline ID and explain any changed state or component")
    return exact[0] if exact else None


def catalog(directory, manifest, slice_id=None):
    result = []
    actuals = {key: [] for key in manifest.get(GROUP, {})}
    for key, assignment in manifest.get("slices", {}).items():
        for receipt in assignment.get("visual_captures", {}).values():
            baseline_id = receipt.get("baseline_id")
            if baseline_id in actuals:
                actuals[baseline_id].append({**receipt, "slice_id": key})
    for plan in manifest.get("cross_screen_changes", {}).values():
        for spec in cross_screen.case_specs(plan).values():
            before = plan["captures"].get(plan["baselines"].get(spec["case_id"]), {})
            baseline_id = before.get("baseline_id")
            if baseline_id in actuals:
                actuals[baseline_id].extend({**receipt, "slice_id": plan["slice_id"], "change_id": plan["change_id"],
                                             "approval_id": plan["approval_id"]}
                                            for receipt in plan["captures"].values() if receipt["phase"] == "after"
                                            and receipt["case"]["case_id"] == spec["case_id"])
    for key, entry in manifest.get(GROUP, {}).items():
        features = set(entry["definition"]["baseline_feature_ids"])
        route = entry["definition"]["state"]["route"]
        refs = design_references(directory, manifest, features, route)
        linked = scope(directory, manifest, entry, refs) | {item["slice_id"] for item in actuals[key]}
        affected = dependent_scope(directory, manifest, linked)
        if slice_id is not None and slice_id not in affected:
            continue
        for approval in manifest.get("approvals", {}):
            if approval in refs:
                continue
            historical = design_references(directory, manifest, features, route, approval_id=approval)
            if historical[approval].get("features") or historical[approval].get("prototype_cases"):
                refs.update(historical)
        error = evidence_error(directory, entry["original"])
        result.append({**entry, "approved": refs, "slice_ids": sorted(linked), "affected_slice_ids": sorted(affected),
                       "actual": sorted(actuals[key], key=lambda item: item["captured_at"]),
                       "available": bool(entry["original"]) and not error, "evidence_error": error})
    return result
