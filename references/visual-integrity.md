# Visual Integrity Gate

Run this gate after implementing each UI-changing slice and before accepting it. During blueprint and slice discussions, edit the illustration in the living viewer. Start the application and collect screenshot evidence when verifying the implemented UI.

For shared UI changes, begin [Cross-Screen Visual Regression Protection](cross-screen-regression.md) before editing. Its original-change group contains the affected routes, baseline captures, comparisons and results. It also applies when a slice-local correction changes shared source.

## Choose adaptive coverage

1. Read the locked blueprint, slice guide, relevant A+ history and protected decisions. Inventory the changed component types and map each to its relevant states, interactions and widths. Include every approved render case, assigned UI acceptance check and protected decision. Use existing application tokens and neighboring components as defaults for unspecified properties.
2. Select applicable critical states by component. For a dropdown, inspect closed, expanded, selected, keyboard focus, disabled and error states where applicable. Exercise pointer selection, keyboard navigation, dismissal and focus return. For a form, examine normal, validation-error, loading and success states where they apply. Check supported breakpoints where layout changes, including the narrowest supported width. At narrow widths, inspect the controls in their surrounding layout.
3. For each selected case, capture the full rendered context and a component close-up. Check layout, spacing, alignment, hierarchy, overflow, typography, color and consistency. Measure bounds, scrolling, clipping or the relevant computed styles in the browser. Exercise the actual controls and record the observed interaction and accessibility outcomes. Compare component structure, design tokens and behavior with the approved references. A visible focus indicator can be required by the keyboard contract.
4. Record three results for each case, `visual`, `interaction` and `blueprint`, each with an observed result and supporting note. Cite the acceptance IDs and protected decision IDs exercised there. Use browser assertions to check the expected outcomes. Use the project's browser harness for complex interaction sequences or fixtures, then assert the resulting state in Chromium. For a static component, assert the relevant semantics or the surrounding control's expected state.
5. Add cases for related states, neighboring components or affected screens when a defect recurs or suggests shared impact. Keep earlier required cases in the next pass. A documented unavailable case uses `not_checked` and stays in the coverage plan until it can be exercised. The plan can avoid redundant combinations while retaining every critical state and required acceptance case.

Use the visual-integrity capture command for each case. It launches Chromium, checks the served build, and records the component and both image hashes against the current implementation. Substitute the actual approved case, route, state, component, viewport and browser in this request. The optional case `command` is an argument array for the project's deterministic state setup. It receives `P2P_BROWSER_PAGE` and `P2P_BROWSER_ENDPOINT` for interacting with the opened page before capture.

```json
{
  "case_id": "erp-menu-open",
  "state": {
    "route": "/apps", "state": "open", "interaction": "expanded by pointer",
    "theme": "light", "data": "ERP options fixture", "active_control": "ERP trigger",
    "open_menus": ["ERP choices"]
  },
  "browser": "google-chrome",
  "appUrl": "http://localhost:3000",
  "renderCase": {
    "route": "/apps",
    "viewport": {"width": 390, "height": 844},
    "setup": [{"action": "click", "selector": "[data-testid='erp-toggle']"}],
    "ready_selector": "[role='menu']"
  },
  "checks": [{"selector": "[data-testid='erp-card']", "property": "padding"}],
  "interaction_assertions": [
    {"selector": "[data-testid='erp-toggle']", "probe": "attribute", "attribute": "aria-expanded", "expected": "true"},
    {"selector": "[data-testid='erp-new']", "probe": "text", "expected": "New (Preview)"}
  ]
}
```

Run from the skill directory after submitting the worker report. Use the returned case object, including its `capture_id`, in the attempt. Add comparisons to that object for findings. Each new inspection needs fresh captures, including rechecks at the original viewport and interaction state. Existing screenshots remain available in the register. Recapture legacy cases before verifying them.

```sh
python3 scripts/project_state.py visual-integrity --project-root <app> --initiative-id <initiative> --slice-id <slice> --action capture --capture-file <request.json>
```

Assertions record expected and actual values with pass or fail. Supported probes include `visible` for viewport visibility and `focused` for the active element, both with boolean expectations. `text` compares trimmed text and `attribute` compares the requested attribute, including null when absent on an existing element. An exact approved render case inherits any assertions declared in the blueprint. For a keyboard case, use setup steps such as `{"action":"press","key":"Tab"}` and `{"action":"press","key":"Enter"}`, then assert the menu and focus outcomes. The browser also supports Escape, Space, the arrow keys, Home, End and the optional boolean `shift` modifier.

The browser automatically measures the CSS criteria assigned to each approved blueprint case. The register shows the approval check ID, expected value and observed value. A blueprint pass requires every applicable measurement to match and its check ID to appear on that case's coverage row. A measurement target required to be visible can be unavailable while the component capture still records the defect. Use `checks` for additional diagnostic measurements.

For a compiled application, serve its build-time `X-P2P-Build-Fingerprint` using the existing `browser-build-fingerprint` command. The helper can also verify served source bytes directly. For a project-specific Playwright or browser harness, use `renderCase.command` to exercise the state on the provided browser page. Keep the page open for the coordinator’s capture and use a selector that identifies the resulting state.

## Classify against approved intent

Compare the rendered UI and actual interaction with the approved blueprint, locked slice guide, A+ decision history and existing design system. Favor approved choices when they differ from the application's defaults. Use measurements or a failed interaction assertion to distinguish a defect from a stylistic difference.

| Classification | Action |
| --- | --- |
| `approved_difference` | Preserve the explicitly approved property and cite its protected decision. |
| `confirmed_defect` | Record the violated requirement, measured problem or failed approved interaction. Repair when the change is clear, falls inside the locked ownership and preserves the approved decisions. |
| `design_concern` | Save the observation for deferred review. Investigate uncertain intent before changing the design. |
| `approval_conflict` | Preserve the evidence and stop automatic changes in the affected area. Obtain a user decision through the amendment workflow. |

Record the classification, rationale, evidence and relevant protected decision IDs. For an automatic repair, explain the approved scope and which requirements the repair preserves. Capture the original evidence before changing the implementation. After the change, repeat the triggering action with compatible route, viewport, theme, data and interaction context. Preserve capture context for every screenshot, including focus and open menus. When the interaction itself was broken, compare the resulting focus or menu against the approved outcome using the same input. Record what changed and add checks for affected related states.

Stop autonomous repair on the first attempt that makes no meaningful progress, or after two unsuccessful repair attempts for the same open finding, including attempts on different builds. Preserve the latest evidence and remaining problem. To resume a stopped finding, record the fresh investigation’s new diagnosis in its `diagnosis` field. The first repair under that diagnosis counts toward the new two-attempt limit. If a repair needs a different design, use the existing amendment and authorization process. Keep the affected slice unresolved and its dependents waiting. Release independent assignments whose own dependencies are satisfied. At handoff, surface unresolved findings and any decision needed. Blocking visual, accessibility or functional defects keep the application from being declared fully verified.

For routine fixes, save the comparison and continue. A verified correction awaits optional user review. The register preserves the original, marked area, latest capture, repair history and verification result for inspection after the slice or after delivery. The existing independent implementation review still applies. Raster-only prototype warnings follow their own [capture-warning workflow](visual-qa.md).

## Record and reopen the evidence

Use the [Final Visual Quality Standards](visual-quality.md) to record applicable keyboard and accessibility checks, classify severity and defer nonblocking warnings. The final audit validates the same evidence before technical completion.

Start with the [attempt template](../assets/project/visual-integrity-attempt.template.json). Replace the example case, viewport and state with the locked values. For blueprint cases include the approved route, state and role when present. If features share a case ID, identify it as `feature_id/case_id`. For prototype cases use the approved route, fixture and state setup. A full inspection includes each required case. For an additional focus, open-menu or responsive view, give it its own case ID and set `based_on` to the approved case on that route. Include the observed interaction state and actual viewport. These additional captures supplement the locked cases.

Each finding has a stable ID, a case, a severity, a description and a dimension. Mark the region on its close-up using fractions of image width and height. Keep the original dimension and marked rectangle across attempts. Supply `latest_rect` when the affected region moves in the new image. The register stores separately marked PNGs alongside the original pixels. Keep the original evidence path for `before` across repair attempts and use the recaptured close-up for `latest`. On a successful fix, record what changed in `correction`, what the browser recheck showed in `verification`, set the finding to `fixed`, include its ID in `rechecked_findings`, and record the attempt as `corrected`. A clean first inspection has passing observations and coverage. Keep advisory findings with status `noted` and annotate their evidence. An attempt can contain an advisory finding without blocking delivery.

Record the case plan in `coverage`. Each row has `case_id`, `component`, `state` matching the captured `state.interaction`, `viewport`, selection `reason`, `acceptance_ids`, `protected_decision_ids` and `checks`. Each check contains `result` (`pass`, `fail` or `not_checked`) and `note`. Include all approved cases and previously selected cases. A passing interaction row requires browser assertions that all passed. Re-run the assertions from prior incomplete attempts for that case before marking it passed. Failed assertions remain available with their screenshots. Use a supplementary case to record defects that prevent an approved state from rendering. The browser receipt preserves measured geometry, focus, expanded controls and the actual setup beside the declared context.

| Viewer outcome | Attempt record |
| --- | --- |
| Verified fixed | Set the repaired finding to `fixed` and the attempt to `corrected` once every selected visual, interaction and blueprint check passes on fresh evidence. |
| Improved — not verified | Set the finding to `improved` and the attempt to `improved_not_verified` when there is evidenced improvement and some verification is still incomplete. |
| Unresolved | Use `unresolved` while a defect, approval conflict or uncovered required state prevents verification. |

Only verified fixes satisfy the repair gate. Incomplete and unresolved attempts hold the affected slice and its dependents. Preserve all outcomes in the register. Existing saved attempts remain readable.

Submit the slice's worker report so the inspection can be bound to its implementation files. From the skill directory, use the same project root and initiative ID as for reconciliation.

```sh
python3 scripts/project_state.py visual-integrity --project-root <app> --initiative-id <initiative> --slice-id <slice> --action record --attempt-file <attempt.json>
python3 scripts/project_state.py visual-integrity --project-root <app> --initiative-id <initiative> --slice-id <slice> --action verify
python3 scripts/project_state.py review-viewer --project-root <app> --initiative-id <initiative>
```

Select **Visual Review Register** at the printed URL. At slice and project handoff, include the viewer address or launch command, the affected slice and finding IDs, and outstanding blockers. To inspect saved records from the CLI, use `visual-integrity --action list` with the same project and initiative arguments. Preserve the entire initiative directory when transferring or archiving the project.
