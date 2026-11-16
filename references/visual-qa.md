# Browser-based visual QA and acceptance

The approved prototype defines the visual truth; generic opinions about dashboards do not. Verification must be **visible**: run the actual app and capture the rendered UI for each contract case so the coordinator can inspect it side by side with the hash-locked prototype screenshot. Source code and a green build are not visual proof.

## Before prototype approval: repeatable visual truth

1. Create an **executable fixture loader**, not merely an exported JSON object. Include all UI-visible records and resources: deterministic timestamps/IDs, selected entities, permissions, locale, local storage/file blobs, active overlays, feature flags, network responses and clock **as relevant to the actual application**. Preserve original app A data separately; never let the prototype overwrite user storage.
2. In a clean browser context, run the loader, wait for fonts/assets/stability, capture each reference viewport and state. Repeat in a **second fresh context** and compare rendered pixels with the saved reference. Test-script success counts alone do not establish repeatability. Retain the script, command, screenshots, and case-by-case PASS report in the initiative. Browser unavailable → **BLOCKED**, not PASS.
3. Label any prototype fixture selector, reset button, debug toolbar or fake backend action `data-prototype-only` (or `data-qa-only`) and exclude these from **every approved product reference**. Keep QA controls in a separate testing mode, not in locked full-page screenshots. Distinguish actual product role previews and truthful local-demo disclosures from test harness controls; state which should exist in production.
4. Inspect real screenshots at 320, 390, 768, 1024, 1440px and near app-specific breakpoints where meaningful. Test supported long-label languages, keyboard focus, touch targets, and the actual app's empty/loading/error, permission, lifecycle and overlay states. Assess accessibility and legibility independently of the chosen visual style; investigate illegibly small body text, accidental truncation, unreachable controls and ambiguous status labels.
5. For source-mode approval, save `replay` details following `assets/project/approval.template.json`: versioned script path, command, evidence JSON and QA exclusion. The evidence JSON records `status: PASS`, `clean_context_runs` of at least 2, and each case's reference SHA-256, **two separately captured PNG paths** (`first_capture`, `second_capture`), `fixture_replayed: true`, `matching_pixels: true`, `qa_controls_visible: false`. The utility checks actual PNG bytes and dimensions against the saved reference, requires the two captures to be distinct files, then SHA-locks both images, the script and report. Keep PNG encoding/environment deterministic too; metadata-only changes also block this stricter preapproval gate. This verifies **real file evidence**, but only a human review can confirm that a testing toolbar was excluded honestly.

Full-page images are useful for review but are not approval `visual_cases` when their dimensions differ from the specified viewport. For approval, use viewport-sized captures and separately preserve full-page context images without QA controls.

## Browser setup

1. Inspect the project's existing browser-test tooling first. Use its Playwright/Cypress/browser workflow when present; do not add dependencies or bypass login policies without user permission. If the browser, test runner, fonts, fixture, credentials, or app server are unavailable, record `BLOCKED` and ask for the missing setup. Do not mark a case PASS.
2. Adapt `assets/project/redesign.visual.spec.ts` to the app's actual routes, service mocks, auth, and fixture/state preparation. Its `prepareCase` intentionally **throws** until you provide a real deterministic setup; no stub can pass. It discovers the hidden or legacy initiative directory and decodes/compares pixels against the locked screenshot, never creating new golden baselines. Each test must match the **same `case_id`, locked route, viewport, fixture and state** in the active approval and slice contract. Initial comparison is strict decoded-pixel equality; capture-only warnings have their own reviewed path below, never a silent tolerance increase.
3. Set `REDESIGN_PROJECT_ROOT`, `REDESIGN_INITIATIVE_ID`, `REDESIGN_SLICE_ID`, and `REDESIGN_APP_BASE_URL`, and run the test from the app's existing Playwright setup. The environment should match the approved prototype capture: viewport, device scale, browser/font versions, theme, locale, timezone, realistic fixture, loading state, modal state, timestamps and animations.
4. Capture implementation PNGs under `slices/<id>/evidence/`, plus useful diffs/overlays if tooling permits. Keep real test output logs. The reference files under `references/<version>/` remain **untouched**.

## Comparison targets: never cross the wrong pages

Before comparing, write a case matrix containing `comparison_purpose`, actual URL/route, selected record, role, locale, state, viewport/DPR and the **specific reference path and SHA-256**. Check each row against the active approval and slice contract. A visually similar page, an unrelated tab or a convenient screenshot with the same dimensions is **not** the same test case. Never select the nearest-looking baseline automatically.

| Purpose | Expected/reference | Actual | Gate |
| --- | --- | --- | --- |
| Prototype replay before approval | Prototype B case/reference in first clean context | **The same B case** in a second fresh context | Required repeatability check; no warning bypass at approval |
| In-scope redesign implementation | **Approved B** reference for the exact locked case | Production implementation of **that same B case**, with the same fixture, role, locale, state and viewport | Strict visual check; may enter the documented capture-only warning path below |
| Unchanged/out-of-scope regression | Original **A before-change** screenshot | The same **A after-change** page/state | Detect regressions; real deviations block, but this comparison cannot demand that A look like B |
| A-to-B change illustration | Original A | Prototype B | Informational design-diff only; **never** an exact-pixel acceptance gate |

Do not use an original-A screenshot as an approved-B baseline. Do not let a diagnostic cross-document A-vs-B comparison block unrelated implementation. Conversely, never dismiss a real out-of-scope A regression as harmless just because B's main cases pass. Preserve and inspect independent before/after A regression evidence.

## When exact pixels disagree: investigate before classifying

Distinguish **real UI defects** (wrong layout/alignment, overlapping or clipped controls, missing content/actions, wrong state, wrong text, broken behavior) from **capture instability** (minor antialiasing, subpixel/font rasterization, color-rounding, browser/environment variance with independently confirmed identical layout and behavior). A tiny pixel count alone **does not prove** capture instability; small differences may signal a real missing indicator or border.

1. **Validate the comparison pair first.** Confirm approved `case_id`, URL/route, reference hash, fixture ID, role, language, state, viewport, device scale factor and screenshot crop. Confirm both pictures depict the **same page and UI state**. A wrong reference is a **test setup bug**: fix the mapping and rerun; do not waive it as a warning.
2. **Make capture deterministic.** Pin browser and OS/font versions, screenshot scale/DPR, zoom, theme, locale, timezone and clock; await assets/fonts/data readiness and stable animations; remove caret/hover artifacts. Isolate fixtures/storage/network responses. Run captures **sequentially** in fresh contexts and record environment and browser version. Do not compare files of different dimensions or infer equality from filenames.
3. **Localize the mismatch.** Preserve the original failed PNG and reference; calculate changed-pixel count, bounding rectangles, representative RGB/channel deltas and a diff/overlay. Inspect the affected area at full resolution, verify DOM bounding boxes, computed styles, visible text, control reachability and interaction/functional tests. If layout, semantics, accessibility or business behavior is wrong, it is a **real defect → BLOCKED** and must be fixed.
4. **Retry exactly three times** after the initial failure, in **three independent clean browser contexts**, rerunning the fixture and capture. Save each PNG and changed-pixel count. If one stable retry exactly matches and there is no defect, mark visual `PASS` and log the intermittent capture problem. If the retries reveal a real defect, inconsistent data/state, a wrong mapping, missing functional verification or an unresolved cause, keep **BLOCKED**.
5. **Narrow warning exception:** If all three retries still fail exact equality but an **independent reviewer** has directly inspected all captures/diffs and confirmed the same geometry, text, states, interactions and accessibility, with only insignificant raster noise, save a case-level `WARNING` plus diagnosis JSON and all three retry PNGs. **Do not claim PASS**. Run `reconcile --action verify` to mark the slice `provisional` and allow dependent implementation to proceed. Preserve the warning in the checkpoint for the user's **later explicit acceptance or rejection**. Never automatically treat it as user-approved.

The exception is only for **approved-B → in-scope implementation** screenshot comparisons. It cannot waive prototype approval replay, out-of-scope functional regressions, missing tests, unknown discrepancies, broken UI, or inaccessible elements. Never mutate approved references, crop away defects, mask changed regions, manufacture retry captures, increase a global threshold, or convert a test tool error into PASS. Browser unavailability is **BLOCKED**.

## Supplemental warnings: mandatory side-by-side visual handoff

Keep **all material screenshot warnings**, including original-A before/after regression comparisons, distinct from the approved-B case results. A clean B-case PASS must not be rewritten to WARNING because an unrelated original-page screenshot differs. A known tiny capture artifact on an unchanged page may be reviewed as a supplemental warning, but an actual change to layout, content, function or accessibility is **BLOCKED**.

1. Save the **original failed** screenshot, reference, three separately named fresh-context retry pairs, changed-pixel counts, diagnosis/DOM/style comparisons, independent review and any lost-evidence disclosure. Use unique output filenames for each run; never silently reuse an old output path. Missing historical files must remain described as missing, not fabricated.
2. Prepare a warning JSON in the initiative's ignored `inputs/` or `slices/<id>/evidence/`. For each comparison, map the exact before/after images; include `case_id`, `attempt_id`, `reference_file`, `actual_file`, and `different_pixels`. Include a meaningful summary and independent reviewer notes. Three distinct `retry_context_ids` are mandatory. `disposition: "informational"` is for issues whose fresh retries exactly match and need no user waiver; `"needs_user_review"` is for independently confirmed residual capture-only differences, which block final slice acceptance until explicitly reviewed.
3. Run `python <skill>/scripts/project_state.py warning-record --project-root <repo> --initiative-id <id> --warning-file <warning.json>`. The utility creates **`warnings/<warning-id>/comparison.html`**: a responsive side-by-side gallery with captions identifying whether it compares an approved prototype, original-application baseline or prototype replay to its correct capture. Original-size image links support close inspection. It also locks the report, every screenshot, investigation file, and gallery itself by SHA-256 in `manifest.json`.
4. Include that gallery path, warning ID and status in the handoff and final review. The checkpoint lists all registered warnings and their galleries. Re-run `validate`/`resume` to detect missing or changed evidence. If evidence has been overwritten, record the incident in the investigation; keep authentic independent retry pairs and request a decision, not an invented reconstruction.
5. When a warning is pending, ask the **user** to inspect the gallery and explicitly accept or reject it. Record the actual response as a decision JSON and run `warning-decide --warning-id <id> --decision-file <json>`. A rejected warning blocks its slice for correction. Neither Codex nor the reviewer may manufacture `confirmed_by_user: true` on the user's behalf.

Example warning input (shortened to one affected case; repeat it for each actual page/state):

```json
{
  "warning_id": "unchanged-settings-raster",
  "slice_id": "account-page",
  "comparison_scope": "original_before_to_original_after",
  "classification": "capture_instability",
  "disposition": "needs_user_review",
  "summary": "Small font edge pixels differ after navigation; geometry and behavior agree",
  "reviewer": "independent-reviewer",
  "reviewer_notes": "Inspected all three pairs, DOM positions, labels, focus and interactions",
  "retry_context_ids": ["fresh-1", "fresh-2", "fresh-3"],
  "comparison_pairs": [
    {"case_id": "settings-desktop", "attempt_id": "fresh-1", "reference_file": "slices/account-page/evidence/before-1.png", "actual_file": "slices/account-page/evidence/after-1.png", "different_pixels": 20},
    {"case_id": "settings-desktop", "attempt_id": "fresh-2", "reference_file": "slices/account-page/evidence/before-2.png", "actual_file": "slices/account-page/evidence/after-2.png", "different_pixels": 20},
    {"case_id": "settings-desktop", "attempt_id": "fresh-3", "reference_file": "slices/account-page/evidence/before-3.png", "actual_file": "slices/account-page/evidence/after-3.png", "different_pixels": 20}
  ],
  "supporting_files": ["slices/account-page/evidence/investigation.json"]
}
```

After the user agrees, a decision file contains `warning_id`, active `approval_id`, `decision: "accept"` (or `"reject"`), `confirmed_by_user: true`, and the actual `user_note`. The utility saves and hashes that decision. A supplemental warning's status never changes an approved B-case visual result; it has its own audit trail. For an already-provisional approved B case, also follow the separate `accept-warning` protocol below.

## What to inspect visibly

- Side-by-side render with the approved version at every approved state and viewport. Check page/container sizes, margins/gaps, grid alignment, exact component density, row heights, typography, truncation, table columns, buttons, labels, icon sizing and consistency.
- Check long and short content, actual app states, menus and dialogs, keyboard focus, horizontal scroll and clipped/overlapping elements. No visible prototype QA controls, false statuses, placeholder data presented as real, or inaccessible text/controls. Evaluate whitespace, text length, density and component sizes **against the user's approved visual references**, not an assumed house style.
- Run functional checks for every `functional_tests` behavior inherited from application A, not only behaviors illustrated in B. Confirm backend integration and validation behavior remain correct.
- Treat screenshots as objective comparison evidence and reviewer inspection as a separate gate. Pixel-level comparisons can be noisy across browser/OS/fonts; diagnose environment differences and record explicitly approved exceptions. Do **not** solve failing snapshots by silently changing the baseline, increasing tolerance indiscriminately, or redesigning the UI.

## Reviewed evidence format

The coordinator writes a JSON review file (the CLI consumes `--review-file`) with:

```json
{
  "slice_id": "invoice-list",
  "reviewer": "coordinator-name",
  "notes": "Compared all rendered states side by side at identical viewports and fixtures",
  "visual_evidence": [{
    "case_id": "invoices-desktop",
    "reference_sha256": "ACTUAL_64_CHARACTER_SHA256_FROM_MANIFEST",
    "implementation_screenshot": "slices/invoice-list/evidence/invoices-desktop.png",
    "tool": "playwright",
    "command": "npx playwright test tests/visual/redesign.visual.spec.ts",
    "result": "PASS",
    "notes": "No unexplained material drift; checked column widths, rows, labels and spacing"
  }],
  "functional_evidence": [{
    "name": "Invoice search/filter/navigation",
    "command": "npm run test:billing",
    "result": "PASS",
    "output_file": "slices/invoice-list/evidence/billing-tests.log",
    "covers": ["Search, filter, and invoice detail navigation work"]
  }]
}
```

Each visual `case_id` and screenshot hash must match the **currently approved** contract. Every case must have an actual implementation PNG and either `PASS` or the independently reviewed, fully documented capture-only `WARNING` described below; a working browser tool/command and comparison notes are mandatory. Each functional requirement must be named in a `covers` list with a real passing test result and stored log. The reviewer must differ from the worker. The utility hashes the reviewed screenshots, all retry PNGs, diagnostic JSON, functional logs and implementation source; subsequent changes cause `validate`/`resume` to BLOCK further acceptance.

For a capture-only warning, replace that visual item's `result` with `"WARNING"` and add:

```json
"capture_warning": {
  "diagnosis_file": "slices/invoice-list/evidence/capture-diagnosis.json",
  "retry_screenshots": [
    "slices/invoice-list/evidence/retry-1.png",
    "slices/invoice-list/evidence/retry-2.png",
    "slices/invoice-list/evidence/retry-3.png"
  ],
  "reviewer_notes": "Compared three fresh captures with the locked case: DOM geometry, copy and interactions match; only edge raster varies."
}
```

The diagnosis JSON must contain `case_id` matching the approved case, `classification: "capture_instability"`, `comparison_scope: "approved_to_implementation"`, `visible_ui_defect: false`, a substantive `investigation` explanation, `retry_different_pixels` with **three positive integer counts**, `retry_context_ids` with **three distinct, nonempty fresh-context IDs** in retry order, and a nonempty `browser_version`. Store other environment details and diff reports there too. The CLI checks for the required records and files, but cannot independently prove human visual equivalence: the reviewer must make that judgment honestly.

`verify` also **automatically generates** `slices/<id>/evidence/approved-capture-warnings.html`, placing the initial failed capture and all three retry PNGs alongside their approved references. The file is SHA-locked in `verification.json` and linked from the checkpoint. Every B-case warning thus has a side-by-side handoff without a separate manual gallery command. Use `warning-record` additionally for supplemental/out-of-scope comparisons, never to relabel the approved B-case result.

`verify` marks warnings as `provisional`, **not accepted**, and unlocks downstream implementation tasks. `accept` refuses a provisional slice. At final review, show the user the exact warning screenshots and diagnostics. Only after the user explicitly accepts those warnings, save a decision JSON like:

```json
{
  "slice_id": "invoice-list",
  "approval_id": "THE_CURRENT_APPROVAL_ID",
  "warning_case_ids": ["invoices-desktop"],
  "approved_by_user": true,
  "user_note": "I reviewed this minor capture-only border mismatch and accept it."
}
```

Then run `reconcile --action accept-warning --review-file <user-decision.json>`. This retains `WARNING_USER_ACCEPTED` and the original evidence, rather than claiming exact visual equality. If the user rejects or any evidence/code changes, fix and reverify. **No final all-accepted integration status until every provisional warning is resolved by the user.**

After the worker has filed a slice-local report and the coordinator has reconciled it:

```text
python <skill>/scripts/project_state.py reconcile --project-root <repo> --initiative-id <id> --slice-id <slice> --action verify --review-file <review.json>
python <skill>/scripts/project_state.py reconcile --project-root <repo> --initiative-id <id> --slice-id <slice> --action accept
python <skill>/scripts/project_state.py validate --project-root <repo> --initiative-id <id>
```

A missing screenshot, failing browser setup, real UI defect, modified approved reference, unverified behavior, self-review, or altered implementation code must never be recorded as accepted. An **isolated capture comparison** may be provisionally warned only through the three-retry and independent-review evidence path; it is never a visual PASS and needs the user's final decision. Record all reasons and seek reapproval if the design itself changes. A manual review is a genuine review of captured pixels, not a claim in a log. The CLI checks that evidence records and files exist and match hashes; it cannot attest that someone actually looked at the images, and the coordinator remains responsible for honest review.
