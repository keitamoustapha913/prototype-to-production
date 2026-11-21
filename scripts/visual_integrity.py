"""Append-only visual QA evidence for implemented slices."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from subprocess import run as run_browser
import uuid

try:
    from scripts import project_state, review_media
except ModuleNotFoundError:
    import project_state
    import review_media


ATTEMPT_ROOT = Path("visual-integrity")
SLICE_ID = re.compile(r"^[a-z][a-z0-9-]{1,60}$")
OBSERVATION_DIMENSIONS = {"geometry", "design", "interaction", "responsive"}
VERIFIED_STATUSES = {"clean", "corrected"}


class VisualIntegrityError(ValueError):
    """Evidence is missing, stale, malformed, or unsafe to serve."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _contract(directory, manifest, slice_id):
    if not isinstance(slice_id, str) or not SLICE_ID.fullmatch(slice_id):
        raise VisualIntegrityError("Invalid slice ID")
    assignment = manifest.get("slices", {}).get(slice_id)
    if not assignment or assignment.get("status") == "cancelled" or assignment.get("superseded_by"):
        raise VisualIntegrityError(f"No active assignment for {slice_id}")
    try:
        path = project_state.artifact_file(directory, assignment["contract_file"])
        if project_state.digest_of(path) != assignment["contract_sha256"]:
            raise VisualIntegrityError(f"Locked contract changed for {slice_id}")
        return project_state.read_json(path)
    except (KeyError, project_state.StateError) as error:
        raise VisualIntegrityError(f"Missing locked contract for {slice_id}: {error}") from error


def _approved_visual_cases(directory: Path, manifest: dict, slice_id: str) -> dict[str, dict]:
    packet = _contract(directory, manifest, slice_id)
    if project_state.initiative_mode(manifest) == "prototype":
        return {case["case_id"]: case for case in packet["visual_cases"]}
    cases = [(feature, case) for feature in packet["feature_contracts"]
             for case in feature.get("visual_contract", {}).get("render_cases", [])]
    # Case IDs in blueprint approvals are scoped to their feature.
    counts = Counter(case["case_id"] for _, case in cases)
    approved = {}
    for feature, case in cases:
        case_id = case["case_id"]
        key = case_id if counts[case_id] == 1 else f"{feature['feature_id']}/{case_id}"
        criteria = [{field: check[field] for field in ("check_id", "expected", "measurement")}
                    for check in feature["acceptance_checks"]
                    if check["kind"] == "visual" and ("case_ids" not in check or case_id in check["case_ids"])]
        approved[key] = {**case, "visual_criteria": criteria}
    return approved


def _adaptive_contract(directory, manifest, slice_id, *, force=False):
    packet = _contract(directory, manifest, slice_id)
    features = [feature for feature in packet.get("feature_contracts", []) if feature.get("visual_contract")]
    checks = {check["check_id"] for feature in features for check in feature["acceptance_checks"]
              if check["kind"] in ("visual", "behavior")}
    protections = {decision["decision_id"]: decision for feature in features
                   for decision in feature.get("protected_visual_decisions", [])}
    if not (force or packet.get("visual_qa_version") or manifest.get("visual_qa_version") or protections):
        return None
    return checks, [protections[key] for key in sorted(protections)]


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _valid_context(state):
    return (isinstance(state, dict) and all(_text(state.get(field)) for field in ("theme", "data", "interaction"))
            and "active_control" in state and (state["active_control"] is None or _text(state["active_control"]))
            and isinstance(state.get("open_menus"), list) and all(_text(menu) for menu in state["open_menus"]))


def _valid_viewport(value):
    return isinstance(value, dict) and all(type(value.get(axis)) is int and value[axis] > 0
                                           for axis in ("width", "height"))


def _ids(value):
    return isinstance(value, list) and all(_text(key) for key in value) and len(set(value)) == len(value)


def check_interaction_assertions(value):
    if not isinstance(value, list):
        raise VisualIntegrityError("Interaction assertions must be an array")
    for rule in value:
        if (not isinstance(rule, dict) or not _text(rule.get("selector"))
                or set(rule) - {"selector", "probe", "expected", "attribute"}):
            raise VisualIntegrityError("Each interaction assertion needs a browser selector")
        probe = rule.get("probe")
        expected = rule.get("expected")
        if probe in ("visible", "focused"):
            valid = type(expected) is bool
        elif probe == "text":
            valid = isinstance(expected, str)
        else:
            valid = probe == "attribute" and _text(rule.get("attribute")) and (
                isinstance(expected, str) or "expected" in rule and expected is None)
        if not valid:
            raise VisualIntegrityError("Interaction assertion needs a valid probe and expected browser outcome")
    return value


def _assertion_keys(assertions):
    return {_canonical({key: value for key, value in item.items() if key not in ("actual", "result")})
            for item in assertions}


def _check_blueprint_measurements(case, criteria, result, acceptance_ids):
    if not criteria:
        return
    evidence = case.get("browser_evidence", {})
    measurements = evidence.get("blueprint_measurements", []) if isinstance(evidence, dict) else []
    if measurements and (
            not isinstance(measurements, list) or len(measurements) != len(criteria) or any(
                not isinstance(observation, dict) or "actual" not in observation
                or {key: value for key, value in observation.items() if key != "actual"} != criterion
                for observation, criterion in zip(measurements, criteria))):
        raise VisualIntegrityError("Browser measurements must match the approved measurement criteria")
    mismatch = any(item["actual"] != item["expected"] for item in measurements)
    required_ids = {item["check_id"] for item in criteria}
    if result == "pass" and (not measurements or mismatch or not required_ids <= set(acceptance_ids)):
        raise VisualIntegrityError("A blueprint pass requires every approved measurement for this case to pass")
    if mismatch and result == "not_checked":
        raise VisualIntegrityError("Record an observed mismatch against an approved measurement as failed blueprint coverage")


def _check_coverage(coverage, cases, expected, contract, records, status):
    if not isinstance(coverage, list) or not coverage or any(not isinstance(row, dict) for row in coverage):
        raise VisualIntegrityError("Adaptive coverage needs a nonempty case plan")
    planned = set()
    acceptance, protections = contract
    decision_ids = {decision["decision_id"] for decision in protections}
    checked_acceptance, checked_decisions = set(), set()
    passing = set()
    captured = {case["case_id"]: case for case in cases}
    prior = {row["case_id"]: row for record in records for row in record.get("coverage", [])}
    required_assertions = {}
    for record in records:
        if record["status"] not in VERIFIED_STATUSES:
            for case in record["cases"]:
                assertions = case.get("browser_evidence", {}).get("interaction_assertions", [])
                required_assertions.setdefault(case["case_id"], set()).update(_assertion_keys(assertions))
    for row in coverage:
        case = row.get("case_id")
        checks = row.get("checks")
        if (not all(_text(row.get(key)) for key in ("case_id", "component", "state", "reason"))
                or not _valid_viewport(row.get("viewport")) or case in planned or not isinstance(checks, dict)
                or set(checks) != {"visual", "interaction", "blueprint"}
                or any(not isinstance(check, dict) or check.get("result") not in ("pass", "fail", "not_checked")
                       or not _text(check.get("note")) for check in checks.values())
                or not _ids(row.get("acceptance_ids")) or not set(row["acceptance_ids"]) <= acceptance
                or not _ids(row.get("protected_decision_ids")) or not set(row["protected_decision_ids"]) <= decision_ids):
            raise VisualIntegrityError("Each coverage case needs its component, state, reason, three checks and valid decision/check IDs")
        planned.add(case)
        if case in prior and any(row[field] != prior[case][field] for field in ("component", "state", "viewport")):
            raise VisualIntegrityError("Recheck previously planned components, states and viewports under their original case IDs")
        if ((case in expected and row["viewport"] != expected[case]["viewport"])
                or (case in captured and (row["viewport"] != captured[case]["viewport"]
                    or row["state"] != captured[case]["state"]["interaction"]))):
            raise VisualIntegrityError("Planned coverage must match the approved viewport and captured interaction state")
        if checks["visual"]["result"] == "pass" and case not in captured:
            raise VisualIntegrityError("Passing visual coverage requires fresh browser captures")
        evidence = captured.get(case, {}).get("browser_evidence", {})
        assertions = evidence.get("interaction_assertions", []) if isinstance(evidence, dict) else []
        if not isinstance(assertions, list) or any(
                not isinstance(item, dict) or item.get("result") not in ("pass", "fail") for item in assertions):
            raise VisualIntegrityError("Invalid captured interaction evidence")
        interaction_result = checks["interaction"]["result"]
        if interaction_result == "pass" and (
                not assertions or any(item["result"] != "pass" for item in assertions)
                or not required_assertions.get(case, set()) <= _assertion_keys(assertions)):
            raise VisualIntegrityError("Passing interaction coverage requires browser interaction evidence "
                                       "with every assertion passing, including prior repair assertions")
        if interaction_result == "not_checked" and any(item["result"] == "fail" for item in assertions):
            raise VisualIntegrityError("A failed browser interaction assertion must be recorded as failed coverage")
        _check_blueprint_measurements(captured.get(case, {}), expected.get(case, {}).get("visual_criteria", []),
                                      checks["blueprint"]["result"], row["acceptance_ids"])
        if all(check["result"] == "pass" for check in checks.values()):
            passing.add(case)
            checked_acceptance.update(row["acceptance_ids"])
            checked_decisions.update(row["protected_decision_ids"])
    if not (set(expected) | set(prior) | set(captured)) <= planned:
        raise VisualIntegrityError("Retain coverage for every approved, captured and previously planned case")
    if status == "improved_not_verified":
        results = {check["result"] for row in coverage for check in row["checks"].values()}
        if "fail" in results or "not_checked" not in results:
            raise VisualIntegrityError("Improved status needs incomplete checks and no remaining failed coverage check")
    if status in VERIFIED_STATUSES and (
            passing != planned or checked_acceptance != acceptance or checked_decisions != decision_ids):
        raise VisualIntegrityError("Verification requires passing visual, interaction and blueprint coverage for every case, "
                                   "acceptance check and protected decision")


FINDING_CLASSIFICATIONS = {"approved_difference", "confirmed_defect", "design_concern", "approval_conflict"}


def _check_classification(finding, decision_ids):
    classification = finding.get("classification")
    if (not isinstance(classification, str) or classification not in FINDING_CLASSIFICATIONS
            or not _text(finding.get("classification_reason")) or not _text(finding.get("evidence")) or not _ids(finding.get("protected_decision_ids"))
            or not set(finding["protected_decision_ids"]) <= decision_ids):
        raise VisualIntegrityError("Record the finding classification, rationale, objective evidence and protected decision IDs")
    if classification == "approved_difference" and not finding["protected_decision_ids"]:
        raise VisualIntegrityError("An approved difference must cite a protected decision")
    if finding.get("correction") or finding.get("status") in ("fixed", "improved"):
        if classification != "confirmed_defect" or not _text(finding.get("repair_scope")):
            raise VisualIntegrityError("Automatic repair requires a confirmed defect and an approved-scope rationale")


def result_fingerprint(directory: Path, manifest: dict, slice_id: str) -> str:
    """Bind the inspection to the same implementation files as delivery verification."""
    packet = _contract(directory, manifest, slice_id)
    try:
        report_path = project_state.artifact_file(directory, f"slices/{slice_id}/worker-report.json")
        report = project_state.read_json(report_path)
        paths = report.get("implementation_paths")
        if not isinstance(paths, list) or not paths:
            raise VisualIntegrityError("Visual integrity requires a worker report with implementation paths")
        if project_state.initiative_mode(manifest) == "blueprint":
            sources = project_state.blueprint_state(directory, manifest).implementation_hashes(packet, paths)
        else:
            sources = {path: project_state.digest_of(project_state.implementation_file(directory, path)) for path in paths}
    except (project_state.StateError, OSError) as error:
        raise VisualIntegrityError(f"Cannot fingerprint visual integrity evidence: {error}") from error
    basis = [slice_id, manifest["active_approval_id"], manifest["slices"][slice_id]["contract_sha256"],
             project_state.digest_of(report_path), sources]
    return hashlib.sha256(_canonical(basis)).hexdigest()


def capture_case(directory, slice_id, request):
    """Run the browser helper and bind the observed case to the implementation."""
    manifest = project_state.manifest_at(directory)
    project_state.visual_inspection_assignment(directory, manifest, slice_id)
    approved_cases = _approved_visual_cases(directory, manifest, slice_id)
    case_id = request.get("case_id")
    base_id = request.get("based_on", case_id)
    if not isinstance(case_id, str) or not isinstance(base_id, str) or (case_id in approved_cases and base_id != case_id):
        raise VisualIntegrityError("Invalid case ID or based_on case")
    approved = approved_cases.get(base_id)
    if not approved or not case_id:
        raise VisualIntegrityError("Browser capture requires an approved case or based_on case")
    fingerprint = result_fingerprint(directory, manifest, slice_id)
    contract = _adaptive_contract(directory, manifest, slice_id)
    state = project_state.blueprint_state(directory, manifest)
    baseline = project_state.sibling_module("visual_baseline")
    head = baseline.revision(directory)
    sources = state.discovery_snapshot([])
    build = {"fingerprint": state.build_fingerprint(sources), "sources": sources}
    render = request.get("renderCase", {})
    observed_state = request.get("state")
    if (not isinstance(render, dict) or not isinstance(observed_state, dict)
            or render.get("route") != approved.get("route") or observed_state.get("route") != render["route"]
            or not isinstance(render.get("ready_selector", approved.get("ready_selector")), str)):
        raise VisualIntegrityError("Capture the approved route with a visible component selector and state")
    if (contract is not None or request.get("adaptive")) and not _valid_context(observed_state):
        raise VisualIntegrityError("Adaptive capture needs theme, data, interaction, active_control and open_menus")
    locked_criteria = approved.get("visual_criteria", []) if case_id in approved_cases else []
    checks = request.get("checks", [])
    if not isinstance(checks, list):
        raise VisualIntegrityError("Visual measurements must be an array")
    checks = [*(criterion["measurement"] for criterion in locked_criteria), *checks]
    if case_id in approved_cases:
        required_state = {k: approved[k] for k in ("route", "state", "role", "fixture", "state_setup") if k in approved}
        if any(observed_state.get(k) != v for k, v in required_state.items()):
            raise VisualIntegrityError("Capture the approved role, fixture and interaction state")
        for key in ("ready_selector", "setup", "command", "observations", "interaction_assertions",
                    "color_scheme", "design_tokens"):
            if key in approved and key in render and render[key] != approved[key]:
                raise VisualIntegrityError(f"Capture the locked browser case {key}")
        render = {**approved, **render}
    if case_id in approved_cases and render.get("viewport") != approved["viewport"]:
        raise VisualIntegrityError("Capture the approved viewport")
    if not render.get("ready_selector", "").strip():
        raise VisualIntegrityError("Supply a visible component selector")
    baseline_id = baseline.link_actual(directory, manifest, slice_id, request, render)
    case, evidence = browser_case(directory, slice_id, state, request, render, observed_state,
                                  case_id, build, checks, locked_criteria)
    if case_id not in approved_cases:
        case["based_on"] = request["based_on"]
    with project_state.coordinator_lock(directory):
        current = project_state.manifest_at(directory)
        project_state.visual_inspection_assignment(directory, current, slice_id)
        if result_fingerprint(directory, current, slice_id) != fingerprint:
            raise VisualIntegrityError("Implementation changed during browser capture")
        if baseline_id and (baseline.revision(directory) != head or
                project_state.blueprint_state(directory, current).discovery_snapshot([]) != sources):
            raise VisualIntegrityError("Source revision changed during visual baseline comparison capture")
        receipt = {"case": case, "result_fingerprint": fingerprint, "build": build["fingerprint"],
                   "revision": head, "approval_id": manifest.get("active_approval_id"),
                   **evidence, "captured_at": datetime.now(timezone.utc).isoformat()}
        if baseline_id:
            paths = manifest["visual_baselines"][baseline_id]["definition"]["source_paths"]
            receipt.update(baseline_id=baseline_id, baseline_comparison_note=request.get("baseline_comparison_note"),
                           sources={path: sources.get(path) for path in paths})
        current["slices"][slice_id].setdefault("visual_captures", {})[case["capture_id"]] = receipt
        project_state.atomic_json(directory / "manifest.json", current)
    return case


def browser_case(directory, slice_id, state, request, render, observed_state, case_id, build, checks, locked_criteria):
    """Capture a selected application state and return its browser receipt."""
    tokens = render.get("design_tokens", [])
    if not _ids(tokens) or any(not token.startswith("--") for token in tokens):
        raise VisualIntegrityError("Design tokens must be distinct CSS custom properties")
    state.check_browser_setup(render.get("setup", []))
    if "command" in render:
        state.argv(render["command"])
    identities = render.get("observations", {})
    if not isinstance(identities, dict) or not all(isinstance(v, dict) for v in identities.values()):
        raise VisualIntegrityError("State observations must describe browser selectors")
    assertions = check_interaction_assertions(render.get("interaction_assertions", []))
    supplied = check_interaction_assertions(request.get("interaction_assertions", []))
    assertions = list({_canonical(rule): rule for rule in [*assertions, *supplied]}.values())
    driver = Path(__file__).with_name("blueprint_browser.mjs")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "capture"
        try:
            run = run_browser(["node", str(driver), "--visual-evidence", str(folder)],
                              input=json.dumps({**request, "renderCase": render, "build": build,
                                                "interaction_assertions": assertions, "checks": checks}), capture_output=True,
                              text=True, timeout=120, cwd=directory.parents[2],
                              env={**os.environ, "P2P_CAPTURE_PYTHON": sys.executable})
        except subprocess.TimeoutExpired as error:
            raise VisualIntegrityError("Browser capture timed out") from error
        if run.returncode:
            raise VisualIntegrityError("Browser capture failed: " + run.stderr.strip())
        measured = json.loads(run.stdout)
        if not measured.get("buildEvidence") or any(
                measured.get("identities", {}).get(k) != rule["expected"]
                for k, rule in identities.items() if "expected" in rule):
            raise VisualIntegrityError("Browser could not confirm the served build or expected state")
        outcomes = measured.get("interactionAssertions", [])
        if (len(outcomes) != len(assertions) or any(
                {key: value for key, value in outcome.items() if key not in ("actual", "result")} != rule
                or outcome.get("result") not in ("pass", "fail")
                for rule, outcome in zip(assertions, outcomes))):
            raise VisualIntegrityError("Browser did not return the requested interaction assertions")
        actual = measured.get("actual", [])
        if len(actual) != len(checks):
            raise VisualIntegrityError("Browser did not return the requested measurements")
        blueprint_measurements = [{**criterion, "actual": value} for criterion, value in zip(locked_criteria, actual)]
        capture_id = str(uuid.uuid4())
        case = {"case_id": case_id, "capture_id": capture_id, "state": observed_state,
                "viewport": measured["viewport"], "closeup_bounds": measured["closeup"],
                "full_screenshot": _store_image(directory, slice_id, (folder / "full.png").read_bytes()),
                "closeup_screenshot": _store_image(directory, slice_id, (folder / "closeup.png").read_bytes())}
        if "browserState" in measured:
            case["browser_evidence"] = {
                "state": measured["browserState"], "geometry": measured.get("geometry"),
                "structure": measured.get("structure"), "design_tokens": measured.get("designTokens"),
                "browser_version": measured.get("browserVersion"),
                "setup": render.get("setup", []), "measurements": actual,
                "checks": checks, "identities": measured.get("identities", {}),
                "blueprint_measurements": blueprint_measurements,
                "harness": render.get("command"), "interaction_assertions": outcomes,
            }
    _check_closeup(directory, case)
    return case, {"served": measured["buildEvidence"], "url": measured["url"], "component": measured["component"],
                  "identities": measured.get("identities", {}), "setup": render.get("setup", []),
                  "checks": checks, "measurements": actual, "harness": render.get("command")}


def _require_capture(manifest, slice_id, fingerprint, case):
    key = case.get("capture_id")
    receipts = manifest["slices"][slice_id].get("visual_captures", {})
    receipt = receipts.get(key) if isinstance(key, str) else None
    evidence = {k: v for k, v in case.items() if k != "comparisons"}
    if not receipt or receipt.get("case") != evidence or receipt.get("result_fingerprint") != fingerprint:
        raise VisualIntegrityError("Case requires a matching browser capture for the current implementation")
    return receipt


def _slice_store(directory, slice_id):
    if not isinstance(slice_id, str) or not SLICE_ID.fullmatch(slice_id):
        raise VisualIntegrityError("Invalid slice ID")
    base = directory / ATTEMPT_ROOT
    store = base / slice_id
    if base.is_symlink() or store.is_symlink():
        raise VisualIntegrityError("Visual integrity store cannot be a symlink")
    return store


def _evidence_root(directory: Path, slice_id: str) -> Path:
    root = _slice_store(directory, slice_id) / "evidence"
    if root.is_symlink():
        raise VisualIntegrityError("Visual evidence store cannot be a symlink")
    return root


def resolve_evidence_path(directory: Path, relative_path: str) -> Path:
    """Only serve intact content-addressed PNGs from the initiative's image store."""
    parts = Path(relative_path).parts
    if (len(parts) != 4 or parts[0] != ATTEMPT_ROOT.name or parts[2] != "evidence"
            or not re.fullmatch(r"[0-9a-f]{64}\.png", parts[3])):
        raise VisualIntegrityError("Unsafe visual evidence path")
    path = _evidence_root(directory, parts[1]) / parts[3]
    if path.is_symlink() or not path.is_file() or project_state.digest_of(path) != path.stem:
        raise VisualIntegrityError(f"Missing or altered visual evidence: {relative_path}")
    return path


def verify_saved_images(directory, records):
    paths = set()
    for case in (case for record in records for case in record["cases"]):
        paths.update((case["full_screenshot"], case["closeup_screenshot"]))
        for comparison in case["comparisons"]:
            paths.update(comparison[field] for field in ("before", "latest", "annotated_before", "annotated_latest")
                         if field in comparison)
    for path in paths:
        resolve_evidence_path(directory, path)


def _resolve_input_png(directory: Path, slice_id: str, value: object) -> Path:
    if not isinstance(value, str):
        raise VisualIntegrityError("Screenshot must name a PNG file")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts or raw.suffix.lower() != ".png":
        raise VisualIntegrityError("Screenshot must be a safe relative PNG path")
    stored_prefix = ATTEMPT_ROOT / slice_id / "evidence"
    if raw.parts[:3] == stored_prefix.parts:
        return resolve_evidence_path(directory, raw.as_posix())
    if raw.parts[:3] != ("slices", slice_id, "evidence"):
        raise VisualIntegrityError("Screenshot must belong to the slice evidence directory")
    path = directory / raw
    if path.resolve() != directory.resolve() / raw or not path.is_file():
        raise VisualIntegrityError("Screenshot must be a slice-local regular file without symlinks")
    return path


def _snapshot_png(directory: Path, slice_id: str, value: object, label: str, viewport: dict) -> str:
    source = _resolve_input_png(directory, slice_id, value)
    data = source.read_bytes()
    review_media.validate_raster(data, "png", label)
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if label == "full screenshot" and (width < viewport["width"] or height < viewport["height"]):
        raise VisualIntegrityError("Full screenshot does not cover the declared viewport")
    return _store_image(directory, slice_id, data)


def _store_image(directory, slice_id, data):
    digest = hashlib.sha256(data).hexdigest()
    folder = _evidence_root(directory, slice_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{digest}.png"
    relative = target.relative_to(directory).as_posix()
    if target.is_symlink():
        raise VisualIntegrityError("Visual evidence cannot be a symlink")
    if target.exists():
        resolve_evidence_path(directory, relative)
    else:
        project_state.atomic_bytes(target, data)
    return relative


def _annotated_png(directory, slice_id, path, rect):
    from io import BytesIO
    from PIL import Image, ImageDraw

    with Image.open(resolve_evidence_path(directory, path)) as original:
        image = original.convert("RGB")
    width, height = image.size
    left = min(width - 1, int(rect["x"] * width))
    top = min(height - 1, int(rect["y"] * height))
    right = max(left, min(width - 1, math.ceil((rect["x"] + rect["width"]) * width) - 1))
    bottom = max(top, min(height - 1, math.ceil((rect["y"] + rect["height"]) * height) - 1))
    draw = ImageDraw.Draw(image)
    draw.rectangle((left, top, right, bottom), outline="#351b00", width=4)
    draw.rectangle((left, top, right, bottom), outline="#ffb02e", width=2)
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    return _store_image(directory, slice_id, encoded.getvalue())


def _validate_rect(rect: object) -> dict:
    if not isinstance(rect, dict) or any(type(rect.get(key)) not in (int, float) for key in ("x", "y", "width", "height")):
        raise VisualIntegrityError("Comparison normalized_rect needs numeric x, y, width and height")
    values = [rect[key] for key in ("x", "y", "width", "height")]
    if any(not math.isfinite(value) for value in values):
        raise VisualIntegrityError("Comparison normalized_rect cannot contain nonfinite values")
    x, y, width, height = values
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise VisualIntegrityError("Comparison normalized_rect must be within the 0..1 viewport")
    return {key: rect[key] for key in ("x", "y", "width", "height")}


def _retry_policy(records, findings):
    prior = records[-1]["retry_policy"] if records and "retry_policy" in records[-1] else {}
    failures = dict(prior.get("failed_repairs", {}))
    stopped = set(prior.get("automatic_repair_stopped", []))
    limit = 2
    for finding in findings:
        key = finding["finding_id"]
        diagnosis = finding.get("diagnosis", "")
        if not isinstance(diagnosis, str):
            raise VisualIntegrityError(f"{key}: diagnosis must be text")
        if key in stopped:
            previous = {" ".join(item.get("diagnosis", "").split()).casefold()
                        for record in records for item in record["findings"]
                        if item["finding_id"] == key and isinstance(item.get("diagnosis", ""), str)}
            normalized = " ".join(diagnosis.split()).casefold()
            if not normalized or normalized in previous:
                raise VisualIntegrityError(f"{key}: automatic retries stopped; record a new diagnosis before another repair")
            failures[key] = 0
            stopped.remove(key)
        if finding.get("status") == "improved":
            failures.setdefault(key, 0)
        elif finding.get("status") == "fixed":
            failures.pop(key, None)
            stopped.discard(key)
        elif key in failures:
            if any(not isinstance(finding.get(field), str) or not finding[field].strip()
                   for field in ("correction", "verification")):
                raise VisualIntegrityError(f"{key}: describe the attempted repair and remaining defect on recheck")
            failures[key] += 1
            if failures[key] >= limit or finding.get("progress") == "none":
                stopped.add(key)
        else:
            failures[key] = 0
    next_step = "independent slice verification"
    if failures:
        next_step = "fresh investigation with a new diagnosis" if stopped else "review and correct open findings"
    return {
        "failed_repairs": failures, "automatic_retry_limit": limit,
        "automatic_repair_stopped": sorted(stopped),
        "next_step": next_step,
    }


def _pixels(image):
    return image.convert("RGBA").convert("RGBa").tobytes()


def _check_closeup(directory, case):
    from PIL import Image

    bounds = case.get("closeup_bounds")
    if not isinstance(bounds, dict) or any(type(bounds.get(k)) is not int for k in ("x", "y", "width", "height")):
        raise VisualIntegrityError("Supply integer closeup_bounds from the component capture in full-page pixels")
    x, y, width, height = (bounds[k] for k in ("x", "y", "width", "height"))
    with Image.open(resolve_evidence_path(directory, case["full_screenshot"])) as page, Image.open(
            resolve_evidence_path(directory, case["closeup_screenshot"])) as detail:
        if (min(x, y) < 0 or min(width, height) <= 0 or x + width > page.width or y + height > page.height
                or detail.size != (width, height)):
            raise VisualIntegrityError("Close-up dimensions must match the component bounds within the full page")
        if _pixels(page.crop((x, y, x + width, y + height))) != _pixels(detail):
            raise VisualIntegrityError("Close-up pixels differ from the component region on the full page")


def _marked_box(case, screenshot, rect, page):
    bounds = case.get("closeup_bounds") if screenshot == case.get("closeup_screenshot") else None
    if bounds:
        x, y, w, h = (bounds[k] for k in ("x", "y", "width", "height"))
    else:
        x, y, w, h = 0, 0, page.width, page.height
    return (x + math.floor(rect["x"] * w), y + math.floor(rect["y"] * h),
            x + math.ceil((rect["x"] + rect["width"]) * w),
            y + math.ceil((rect["y"] + rect["height"]) * h))


def _marked_pixels_changed(directory, original_case, case, pair):
    from PIL import Image

    # Historical captures may predate page coordinates. Compare in the
    # screenshot's own coordinate system when either crop cannot be located.
    use_pages = original_case.get("closeup_bounds") and case.get("closeup_bounds")
    before = original_case["full_screenshot"] if use_pages else pair["before"]
    latest = case["full_screenshot"] if use_pages else pair["latest"]
    with Image.open(resolve_evidence_path(directory, before)) as first, Image.open(
            resolve_evidence_path(directory, latest)) as second:
        rect = pair["normalized_rect"]
        boxes = (_marked_box(original_case if use_pages else {}, pair["before"], rect, first),
                 _marked_box(case if use_pages else {}, pair["latest"], pair.get("latest_rect", rect), second))
        for box in boxes:
            # Compare the same page location across captures, including both
            # ends of a move, while excluding unrelated pixels between them.
            old = first.crop(tuple(min(value, limit) for value, limit in zip(box, first.size * 2)))
            new = second.crop(tuple(min(value, limit) for value, limit in zip(box, second.size * 2)))
            if old.size != new.size or _pixels(old) != _pixels(new):
                return True
    return False


def _check_finding_evidence(directory, cases, findings, records):
    def regions(pairs):
        return Counter((pair["before"], tuple(sorted(pair["normalized_rect"].items()))) for pair in pairs)

    originals = {}
    dimensions = {}
    for record in records:
        for previous in record["findings"]:
            dimensions.setdefault(previous["finding_id"], previous["dimension"])
        for case in record["cases"]:
            groups = {}
            for pair in case["comparisons"]:
                if pair.get("finding_id"):
                    groups.setdefault(pair["finding_id"], []).append(pair)
            for key, pairs in groups.items():
                originals.setdefault(key, (case, pairs))
    by_case = {case["case_id"]: case for case in cases}
    for finding in findings:
        key = finding["finding_id"]
        case = by_case[finding["case_id"]]
        pairs = [pair for pair in case["comparisons"] if pair.get("finding_id") == key]
        if not pairs:
            raise VisualIntegrityError(f"{key}: provide an annotated comparison in the affected case")
        original_case, original_pairs = originals.get(key, (case, []))
        if original_pairs:
            if case["case_id"] != original_case["case_id"] or regions(pairs) != regions(original_pairs):
                raise VisualIntegrityError(f"{key}: preserve the original case, screenshots and highlighted regions")
        elif any(pair["before"] != pair["latest"] for pair in pairs):
            raise VisualIntegrityError(f"{key}: preserve the original case, screenshot and highlighted region")
        original_dimension = dimensions.get(key, finding["dimension"])
        if finding["dimension"] != original_dimension:
            raise VisualIntegrityError(f"{key}: retain the original finding dimension ({original_dimension})")
        # An interaction fix can legitimately change the observed focus or menu result.
        outcomes = {"active_control", "open_menus"} if original_dimension == "interaction" else set()
        original_browser = original_case.get("browser_evidence", {}).get("state", {})
        current_browser = case.get("browser_evidence", {}).get("state", {})
        browser_outcomes = {"activeControl", "expandedControls"} if outcomes else set()
        rechecking = finding.get("status") in ("fixed", "improved")
        if rechecking and (
                case["viewport"] != original_case["viewport"]
                or any(case["state"].get(k) != v for k, v in original_case["state"].items() if k not in outcomes)
                or case.get("based_on") != original_case.get("based_on")
                or any(current_browser.get(k) != v for k, v in original_browser.items() if k not in browser_outcomes)):
            raise VisualIntegrityError(f"{key}: recheck the original viewport and interaction state")
        for pair in pairs:
            if pair["latest"] not in (case["full_screenshot"], case["closeup_screenshot"]):
                raise VisualIntegrityError(f"{key}: latest image must come from the current case capture")
            if (rechecking and original_dimension in {"geometry", "design", "responsive"}
                    and not _marked_pixels_changed(directory, original_case, case, pair)):
                raise VisualIntegrityError(f"{key}: identical image pixels in the marked regions after correction")


def _validate_payload(directory: Path, manifest: dict, slice_id: str, payload: dict, old_records: list) -> dict:
    if not isinstance(payload, dict):
        raise VisualIntegrityError("Attempt must be a JSON object")
    adaptive_contract = _adaptive_contract(directory, manifest, slice_id, force="coverage" in payload)
    adaptive = adaptive_contract is not None or "coverage" in payload or any("coverage" in row for row in old_records)
    allowed = {"status", "observations", "findings", "rechecked_findings", "cases", "coverage"}
    if set(payload) - allowed:
        raise VisualIntegrityError("Unknown attempt fields: " + ", ".join(sorted(set(payload) - allowed)))
    status = payload.get("status")
    if status not in ("clean", "corrected", "improved_not_verified", "unresolved"):
        raise VisualIntegrityError("Status must be clean, corrected, improved_not_verified, or unresolved")
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise VisualIntegrityError("observations must cover geometry, design, interaction and responsive behavior")
    observed, failed = set(), set()
    for item in observations:
        if (not isinstance(item, dict) or not isinstance(item.get("dimension"), str) or item["dimension"] not in OBSERVATION_DIMENSIONS
                or item.get("result") not in ("pass", "fail", "not_checked")
                or not isinstance(item.get("note"), str) or not item["note"].strip()):
            raise VisualIntegrityError("Every geometry/design/interaction/responsive observation needs a result and review note")
        if item["dimension"] in observed:
            raise VisualIntegrityError("Each review dimension must appear exactly once")
        if item["result"] != "pass":
            failed.add(item["dimension"])
        observed.add(item["dimension"])
    if observed != OBSERVATION_DIMENSIONS:
        raise VisualIntegrityError("Observations must cover geometry, design, interaction and responsive behavior")
    findings = payload.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        raise VisualIntegrityError("Findings must be an array of finding objects")
    finding_ids = [item.get("finding_id") for item in findings]
    if any(not isinstance(item, str) or not item for item in finding_ids) or len(set(finding_ids)) != len(finding_ids):
        raise VisualIntegrityError("Every finding needs a unique finding_id")
    for item in findings:
        if (item.get("status", "open") not in ("open", "fixed", "improved", "noted")
                or not isinstance(item.get("dimension"), str) or item["dimension"] not in OBSERVATION_DIMENSIONS
                or not isinstance(item.get("case_id"), str)
                or not isinstance(item.get("description"), str) or not item["description"].strip()):
            raise VisualIntegrityError("Each finding needs a case, dimension, status and actionable description")
        if adaptive:
            _check_classification(item, {decision["decision_id"] for decision in (adaptive_contract or (set(), []))[1]})
        advisory = item.get("classification") in ("approved_difference", "design_concern")
        if advisory != (item.get("status") == "noted"):
            raise VisualIntegrityError("Approved differences and design concerns use the noted status")
    resolved = payload.get("rechecked_findings", [])
    if not isinstance(resolved, list) or any(not isinstance(item, str) for item in resolved) or len(set(resolved)) != len(resolved):
        raise VisualIntegrityError("rechecked_findings must be a list of unique prior finding IDs")
    open_prior = set()
    conflicts = set()
    approval = manifest.get("active_approval_id")
    for record in old_records:
        for finding in record["findings"]:
            if finding.get("classification") == "approval_conflict" and record.get("approval_id") == approval:
                conflicts.add(finding["finding_id"])
            if finding.get("status", "open") in ("open", "improved"):
                open_prior.add(finding["finding_id"])
            elif finding.get("status") == "fixed":
                open_prior.discard(finding["finding_id"])
    new_open = {item["finding_id"] for item in findings if item.get("status", "open") in ("open", "improved")}
    improved = {item["finding_id"] for item in findings if item.get("status") == "improved"}
    fixed = {item["finding_id"] for item in findings if item.get("status") == "fixed"}
    if any(item["finding_id"] in conflicts
           and (item.get("correction") or item.get("status") in ("improved", "fixed")) for item in findings):
        raise VisualIntegrityError("Approval conflicts require an explicit amendment before repair")
    if improved and (not improved <= open_prior or any(
            not _text(item.get("correction")) or not _text(item.get("verification"))
            for item in findings if item["finding_id"] in improved)):
        raise VisualIntegrityError("Improvement requires a prior defect, correction and incomplete verification evidence")
    if status == "improved_not_verified" and (not improved or improved != new_open or open_prior - improved or not adaptive):
        raise VisualIntegrityError("Improved status requires improved findings and incomplete adaptive verification")
    if fixed and adaptive and status != "corrected":
        raise VisualIntegrityError("An adaptive repair is verified fixed only after the full selected coverage passes")
    if fixed != set(resolved) or not fixed <= open_prior:
        raise VisualIntegrityError("Each fixed finding must have a previously recorded defect and a current recheck")
    if status == "clean" and (new_open or fixed or open_prior):
        raise VisualIntegrityError("A clean inspection cannot clear findings; record their verified correction")
    if status == "corrected" and (not fixed or new_open or open_prior - fixed):
        raise VisualIntegrityError("A corrected inspection must resolve and recheck every open finding")
    if status in VERIFIED_STATUSES and failed:
        raise VisualIntegrityError("Clean/corrected status requires passing observations in every review dimension")
    if status == "unresolved" and not new_open and not adaptive:
        raise VisualIntegrityError("An unresolved inspection must record an open finding")
    for finding in findings:
        if not isinstance(finding.get("severity"), str) or not finding["severity"].strip():
            raise VisualIntegrityError("Record a severity for each visual finding")
        if finding["finding_id"] in fixed and any(
                not isinstance(finding.get(key), str) or not finding[key].strip()
                for key in ("correction", "verification")):
            raise VisualIntegrityError("Describe the correction and browser verification for each fixed finding")

    retry_policy = _retry_policy(old_records, [finding for finding in findings
                                               if finding.get("classification", "confirmed_defect") == "confirmed_defect"])
    expected = _approved_visual_cases(directory, manifest, slice_id)
    cases = payload.get("cases")
    if not isinstance(cases, list) or (not adaptive and not cases) or any(
            not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"] for case in cases):
        raise VisualIntegrityError("Cases must include every approved visual case")
    case_ids = {item.get("case_id") for item in cases if isinstance(item, dict)}
    if status in VERIFIED_STATUSES and not set(expected).issubset(case_ids):
        raise VisualIntegrityError("A successful inspection must include every approved visual case")
    if len(case_ids) != len(cases):
        raise VisualIntegrityError("Attempt case IDs must be unique")
    if any(item.get("case_id") not in case_ids for item in findings):
        raise VisualIntegrityError("Every finding must identify a case in this attempt")
    saved_cases = []
    for case in cases:
        approved_id = case["case_id"] if case["case_id"] in expected else case.get("based_on")
        if not isinstance(approved_id, str) or approved_id not in expected:
            raise VisualIntegrityError("Supplementary case must name a based_on approved case")
        approved = expected[approved_id]
        viewport = case.get("viewport")
        if case["case_id"] in expected and viewport != approved.get("viewport"):
            raise VisualIntegrityError(f"Case {case['case_id']} viewport differs from approved design")
        if not _valid_viewport(viewport):
            raise VisualIntegrityError(f"Case {case['case_id']} needs actual positive viewport dimensions")
        state = case.get("state")
        required_state = {key: approved[key] for key in ("route", "fixture", "state_setup", "state", "role") if key in approved}
        if case["case_id"] not in expected:
            required_state = {"route": approved["route"]}
            if not isinstance(state, dict) or not any(
                    isinstance(state.get(key), str) and state[key].strip() for key in ("state", "state_setup", "interaction")):
                raise VisualIntegrityError("Describe the actual state for each supplementary capture")
        if not isinstance(state, dict) or any(state.get(key) != value for key, value in required_state.items()):
            raise VisualIntegrityError(f"Case {case['case_id']} state differs from approved route/fixture/setup")
        if adaptive and not _valid_context(state):
            raise VisualIntegrityError("Each adaptive case needs theme, data, interaction, active_control and open_menus")
        if not case.get("capture_id") and case.get("full_screenshot") == case.get("closeup_screenshot"):
            raise VisualIntegrityError("Capture a separate full-page image and component close-up")
        full = _snapshot_png(directory, slice_id, case.get("full_screenshot"), "full screenshot", viewport)
        close = _snapshot_png(directory, slice_id, case.get("closeup_screenshot"), "closeup screenshot", viewport)
        comparisons = case.get("comparisons", [])
        if not isinstance(comparisons, list):
            raise VisualIntegrityError("Case comparisons must be an array")
        saved_comparisons = []
        for comparison in comparisons:
            if not isinstance(comparison, dict):
                raise VisualIntegrityError("Each comparison must be an object")
            before = _snapshot_png(directory, slice_id, comparison.get("before"), "comparison before", viewport)
            latest = _snapshot_png(directory, slice_id, comparison.get("latest"), "comparison latest", viewport)
            saved_comparison = {"before": before, "latest": latest,
                                "normalized_rect": _validate_rect(comparison.get("normalized_rect"))}
            if "latest_rect" in comparison:
                saved_comparison["latest_rect"] = _validate_rect(comparison["latest_rect"])
            if comparison.get("finding_id") is not None:
                if comparison["finding_id"] not in finding_ids:
                    raise VisualIntegrityError("An annotated comparison must identify a finding in this attempt")
                saved_comparison["finding_id"] = comparison["finding_id"]
            saved_comparisons.append(saved_comparison)
        saved_case = {"case_id": case["case_id"], "viewport": viewport, "state": state,
                      "full_screenshot": full, "closeup_screenshot": close,
                      "comparisons": saved_comparisons, "closeup_bounds": case.get("closeup_bounds")}
        saved_case["capture_id"] = case.get("capture_id")
        if "browser_evidence" in case:
            saved_case["browser_evidence"] = case["browser_evidence"]
        _check_closeup(directory, saved_case)
        if case["case_id"] not in expected:
            saved_case["based_on"] = approved_id
        saved_cases.append(saved_case)
    coverage = payload.get("coverage")
    if adaptive:
        same_approval = [record for record in old_records if record.get("approval_id") == approval]
        _check_coverage(coverage, saved_cases, expected, adaptive_contract or (set(), []), same_approval, status)
    _check_finding_evidence(directory, saved_cases, findings, old_records)
    for case in saved_cases:
        for pair in case["comparisons"]:
            for field in ("before", "latest"):
                rect = pair.get("latest_rect", pair["normalized_rect"]) if field == "latest" else pair["normalized_rect"]
                pair["annotated_" + field] = _annotated_png(directory, slice_id, pair[field], rect)
    return {"status": status, "observations": observations, "findings": findings,
            "rechecked_findings": resolved, "cases": saved_cases,
            "retry_policy": retry_policy,
            **({"coverage": coverage, "protected_visual_decisions": (adaptive_contract or (set(), []))[1]}
               if adaptive else {})}


def _journal_path(directory: Path, slice_id: str) -> Path:
    return _slice_store(directory, slice_id) / "attempts.jsonl"


def _read_slice_attempts(directory: Path, slice_id: str, warnings=None) -> list[dict]:
    if not SLICE_ID.fullmatch(slice_id):
        raise VisualIntegrityError("Invalid slice ID")
    path = _journal_path(directory, slice_id)
    if path.is_symlink():
        raise VisualIntegrityError("Visual integrity journal cannot be a symlink")
    if not path.exists():
        return []
    records = []
    previous_hash = "0" * 64
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                record = json.loads(line)
                saved_hash = record.pop("record_hash", None)
                if record.get("previous_hash") != previous_hash or saved_hash != hashlib.sha256(_canonical(record)).hexdigest():
                    raise VisualIntegrityError(f"Visual integrity register chain is invalid at {path}:{line_number}")
                record["record_hash"] = saved_hash
                records.append(record)
                previous_hash = saved_hash
    except (OSError, ValueError, TypeError, AttributeError) as error:
        message = f"{slice_id}: incomplete visual history: {error}"
        if warnings is None:
            raise VisualIntegrityError(message) from error
        warnings.append(message)
    return records


def list_attempts(directory: Path, slice_id: str | None = None, *, manifest=None, warnings=None) -> list[dict]:
    """Check the surviving journal prefix against the project's saved head."""
    if manifest is None:
        manifest = project_state.manifest_at(directory)
    assignments = manifest.get("slices", {})
    root = directory / ATTEMPT_ROOT
    if root.is_symlink():
        raise VisualIntegrityError("Visual integrity store cannot be a symlink")
    keys = {slice_id} if slice_id is not None else {
        key for key, item in assignments.items() if item.get("visual_integrity_head")}
    if slice_id is None and root.is_dir():
        keys.update(child.name for child in root.iterdir() if child.is_dir())
    attempts = []
    for key in sorted(keys):
        try:
            rows = _read_slice_attempts(directory, key, warnings)
        except VisualIntegrityError as error:
            if warnings is None:
                raise
            warnings.append(f"{key}: incomplete visual history: {error}")
            continue
        actual = rows[-1]["record_hash"] if rows else None
        if actual != assignments.get(key, {}).get("visual_integrity_head"):
            message = f"{key}: incomplete visual history; the surviving journal differs from the saved head"
            if warnings is None:
                raise VisualIntegrityError(message)
            warnings.append(message)
        attempts.extend(rows)
    return attempts


def _dependent_slices(directory, manifest, slice_id):
    assignments = {key: item for key, item in manifest["slices"].items()
                   if item.get("status") != "cancelled" and not item.get("superseded_by")}
    registry = manifest.get("slice_review", {})
    package_id = registry.get("active_package") or registry.get("continuing_releases", {}).get(slice_id)
    if package_id:
        review = project_state.slice_review_state(directory, manifest)
        _, _, package = review.package(key=package_id)
        assignments.update(package["slices"])
    consumers = {}
    for key, item in assignments.items():
        for dependency in item.get("dependencies", []):
            consumers.setdefault(dependency, []).append(key)
    pending = [slice_id]
    affected = set()
    while pending:
        for key in consumers.get(pending.pop(), []):
            if key not in affected:
                affected.add(key)
                pending.append(key)
    return sorted(affected - {slice_id})


def record_attempt(directory: Path, manifest: dict, slice_id: str, payload: dict, *, commit=None) -> dict:
    """Validate and append an immutable attempt bound to the exact current result."""
    journal = _journal_path(directory, slice_id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _slice_store(directory, slice_id) / "attempts.lock"
    if lock_path.is_symlink():
        raise VisualIntegrityError("Visual integrity lock cannot be a symlink")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        records = _read_slice_attempts(directory, slice_id)
        fingerprint = result_fingerprint(directory, manifest, slice_id)
        if lock_path.is_symlink() or journal.is_symlink():
            raise VisualIntegrityError("Visual integrity register paths cannot be symlinks")
        data = _validate_payload(directory, manifest, slice_id, payload, records)
        previous_captures = {case.get("capture_id") for record in records for case in record["cases"]}
        for case in data["cases"]:
            receipt = _require_capture(manifest, slice_id, fingerprint, case)
            if "coverage" in data and not isinstance(case.get("browser_evidence"), dict):
                raise VisualIntegrityError("Adaptive verification requires browser state and geometry evidence")
            if records and receipt["captured_at"] <= records[-1]["recorded_at"]:
                raise VisualIntegrityError("Use a fresh browser capture taken after the previous inspection")
            if case["capture_id"] in previous_captures:
                raise VisualIntegrityError("Each inspection attempt requires a fresh browser capture")
        body = {"schema_version": 1, "attempt_id": str(uuid.uuid4()),
                "attempt_number": len(records) + 1,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "slice_id": slice_id, "result_fingerprint": fingerprint,
                "approval_id": manifest.get("active_approval_id"),
                "previous_hash": records[-1]["record_hash"] if records else "0" * 64, **data}
        body["dependent_slices"] = _dependent_slices(directory, manifest, slice_id)
        body["record_hash"] = hashlib.sha256(_canonical(body)).hexdigest()
        if commit is not None:
            commit(body, journal, records)
            return body
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        manifest["slices"][slice_id]["visual_integrity_head"] = body["record_hash"]
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return body


def require_verified(directory: Path, manifest: dict, slice_id: str, *, historical: bool = False) -> dict:
    """Check saved evidence and bind active inspections to the current result."""
    fingerprint = None if historical else result_fingerprint(directory, manifest, slice_id)
    records = _read_slice_attempts(directory, slice_id)
    if not records:
        raise VisualIntegrityError(f"Slice {slice_id} has no visual integrity attempt for the current result")
    latest = records[-1]
    if not historical and latest["result_fingerprint"] != fingerprint:
        raise VisualIntegrityError(f"Slice {slice_id} has a newer visual integrity attempt for a different result")
    if manifest.get("slices", {}).get(slice_id, {}).get("visual_integrity_head") != records[-1].get("record_hash"):
        raise VisualIntegrityError(f"Slice {slice_id} visual integrity head does not match its append-only register")
    if latest.get("status") not in VERIFIED_STATUSES:
        raise VisualIntegrityError(f"Slice {slice_id} visual integrity is unresolved; latest attempt is {latest.get('status')}")
    if not historical and _adaptive_contract(directory, manifest, slice_id) is not None and "coverage" not in latest:
        raise VisualIntegrityError("Current UI contract requires adaptive visual coverage")
    verify_saved_images(directory, records)
    for record in records if historical else [latest]:
        for case in record["cases"]:
            _require_capture(manifest, slice_id, record["result_fingerprint"], case)
    for case in latest["cases"]:
        _check_closeup(directory, case)
    if not historical and latest["status"] == "corrected":
        _check_finding_evidence(directory, latest["cases"], latest["findings"], records[:-1])
    return latest
