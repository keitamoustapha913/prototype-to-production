# Browser-based visual QA and acceptance

The approved prototype defines the visual truth; generic opinions about dashboards do not. Verification must be **visible**: run the actual app and capture the rendered UI for each contract case so the coordinator can inspect it side by side with the hash-locked prototype screenshot. Source code and a green build are not visual proof.

## Browser setup

1. Inspect the project's existing browser-test tooling first. Use its Playwright/Cypress/browser workflow when present; do not add dependencies or bypass login policies without user permission. If the browser, test runner, fonts, fixture, credentials, or app server are unavailable, record `BLOCKED` and ask for the missing setup. Do not mark a case PASS.
2. Adapt `assets/project/redesign.visual.spec.ts` to the app's actual routes, service mocks, auth, and fixture/state preparation. Its `prepareCase` intentionally **throws** until you provide a real deterministic setup; no stub can pass. The example decodes and compares actual pixels against the approved hash-checked screenshot without creating golden baselines. It requires exact pixel equality until you explicitly document an allowed deviation.
3. Set `REDESIGN_PROJECT_ROOT`, `REDESIGN_INITIATIVE_ID`, `REDESIGN_SLICE_ID`, and `REDESIGN_APP_BASE_URL`, and run the test from the app's existing Playwright setup. The environment should match the approved prototype capture: viewport, device scale, browser/font versions, theme, locale, timezone, realistic fixture, loading state, modal state, timestamps and animations.
4. Capture implementation PNGs under `slices/<id>/evidence/`, plus useful diffs/overlays if tooling permits. Keep real test output logs. The reference files under `references/<version>/` remain **untouched**.

## What to inspect visibly

- Side-by-side render with the approved version at every approved state and viewport. Check page/container sizes, margins/gaps, grid alignment, exact component density, row heights, typography, truncation, table columns, buttons, labels, icon sizing and consistency.
- Check long and short content, empty/loading/error/permission states, menus and dialogs, keyboard focus, horizontal scroll and clipped/overlapping elements. No false statuses, unnecessary explanations, placeholder microcopy, or unexplained oversized controls.
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
