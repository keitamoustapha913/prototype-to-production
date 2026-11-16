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
2. Adapt `assets/project/redesign.visual.spec.ts` to the app's actual routes, service mocks, auth, and fixture/state preparation. Its `prepareCase` intentionally **throws** until you provide a real deterministic setup; no stub can pass. It discovers the hidden or legacy initiative directory and decodes/compares pixels against the locked screenshot, never creating new golden baselines. It requires exact pixel equality until you explicitly document an allowed deviation.
3. Set `REDESIGN_PROJECT_ROOT`, `REDESIGN_INITIATIVE_ID`, `REDESIGN_SLICE_ID`, and `REDESIGN_APP_BASE_URL`, and run the test from the app's existing Playwright setup. The environment should match the approved prototype capture: viewport, device scale, browser/font versions, theme, locale, timezone, realistic fixture, loading state, modal state, timestamps and animations.
4. Capture implementation PNGs under `slices/<id>/evidence/`, plus useful diffs/overlays if tooling permits. Keep real test output logs. The reference files under `references/<version>/` remain **untouched**.

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

Each visual `case_id` and screenshot hash must match the **currently approved** contract. Every case must have an actual implementation PNG and PASS, a working tool/command, and review notes. Each functional requirement must be named in a `covers` list with a real passing test result and stored log. The reviewer must differ from the worker. The utility hashes the reviewed screenshot, functional log and implementation source; subsequent changes cause `validate`/`resume` to BLOCK further acceptance.

After the worker has filed a slice-local report and the coordinator has reconciled it:

```text
python <skill>/scripts/project_state.py reconcile --project-root <repo> --initiative-id <id> --slice-id <slice> --action verify --review-file <review.json>
python <skill>/scripts/project_state.py reconcile --project-root <repo> --initiative-id <id> --slice-id <slice> --action accept
python <skill>/scripts/project_state.py validate --project-root <repo> --initiative-id <id>
```

A missing screenshot, failing browser test, modified approved reference, unverified behavior, self-review, or altered implementation code must never be recorded as accepted. Record reasons as durable blockers and seek reapproval if the design itself changes. A manual review is a genuine review of captured pixels, not a claim in a log. The CLI checks that evidence records and files exist and match hashes; it cannot attest that someone actually looked at the images, and the coordinator remains responsible for honest review.
