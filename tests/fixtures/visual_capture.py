"""Supply deterministic browser results for workflow tests. Browser tests launch Chromium."""
from pathlib import Path
import json
import shutil
from types import SimpleNamespace
from unittest.mock import patch

from scripts import visual_integrity


def coverage_row(case, acceptance_ids=()):
    return {"case_id": case["case_id"], "component": "Fixture component",
            "state": case["state"]["interaction"], "viewport": case["viewport"], "reason": "Approved fixture state",
            "checks": {kind: {"result": "pass", "note": f"Fixture confirms {kind} outcome"}
                       for kind in ("visual", "interaction", "blueprint")},
            "accessibility": {"result": "not_applicable", "note": "Fixture component contains no interactive controls"},
            "acceptance_ids": list(acceptance_ids), "protected_decision_ids": []}


def capture_fixture_case(directory, slice_id, case):
    case["state"].update(theme="light", data="controlled fixture", interaction=case["state"].get("state", "fixture"),
                         active_control=None, open_menus=[])
    render = {"route": case["state"]["route"], "viewport": case["viewport"], "ready_selector": "#fixture"}
    approved = visual_integrity._approved_visual_cases(
        directory, visual_integrity.project_state.manifest_at(directory), slice_id)[case["case_id"]]
    render["ready_selector"] = approved.get("ready_selector", "#fixture")
    render["observations"] = approved.get("observations", {})

    assertion = {"selector": render["ready_selector"], "probe": "visible", "expected": True}

    def browser(args, **kwargs):
        folder = Path(args[-1])
        folder.mkdir()
        shutil.copyfile(directory / case["full_screenshot"], folder / "full.png")
        shutil.copyfile(directory / case["closeup_screenshot"], folder / "closeup.png")
        requested = json.loads(kwargs["input"])["interaction_assertions"]
        result = {"viewport": render["viewport"], "component": "#fixture", "url": render["route"],
                  "closeup": case["closeup_bounds"], "buildEvidence": [{"url": render["route"]}],
                  "identities": {k: v.get("expected") for k, v in render["observations"].items()},
                  "browserState": {"activeControl": None, "expandedControls": []}, "geometry": {},
                  "structure": {"interactive_controls": 0},
                  "interactionAssertions": [
                      {**rule, "actual": rule["expected"], "result": "pass"} for rule in requested],
                  "actual": [criterion["expected"] for criterion in approved.get("visual_criteria", [])]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    # Keep git inventory and receipt validation real while substituting only Chrome.
    with patch.object(visual_integrity, "run_browser", side_effect=browser):
        recorded = visual_integrity.capture_case(directory, slice_id, {
            "case_id": case["case_id"], "state": case["state"], "renderCase": render,
            "interaction_assertions": [assertion]})
    case.update(capture_id=recorded["capture_id"], browser_evidence=recorded["browser_evidence"])
