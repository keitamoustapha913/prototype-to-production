# From existing UI A to approved frontend B

This process supports a single feature, page or entire existing web application, regardless of its domain, frontend framework, audience or visual style. Input may be plain language, screenshots, design references, or combinations. A complete redesign is a **set of mapped user journeys and UI states**, not permission to discard the functionality of A.

## Natural-language intake — do the work without a user-authored checklist

Typical complete requests: “Redesign this account page using the attached images,” “Prototype a lively events dashboard,” “Review v1,” “Approve v2,” or “Implement what I approved.” The **skill** owns the stages, tool commands and evidence; users supply intent, scope and optional style references, not boilerplate. Determine the actual source, route, scope and stable initiative ID from the repository, previous project state and the user's words. Ask only if more than one plausible page/app, missing references or unspecified visual direction would materially change the outcome. Derive source code facts by inspection, not from a generic hardcoded metrics list. Never treat the pilot application's features or aesthetic as a product default.

Route by intent: a new creative redesign enters Discover/Prototype and stops for review; a revision versions B and stays in Prototype; explicit approval locks only the named covered version; implementation operates only on a validated approved version. A request to “simplify” an already approved design must create a new prototype version and request fresh approval; it must not silently alter implementation.

## 0. Initialize / resume before touching code

- Inspect repository README, existing AGENTS rules, frontend framework, routes, component library, tokens, API layer, automated tests, browser tooling and current Git/worktree changes. Respect all unrelated modifications.
- Pick a stable initiative ID such as `billing-redesign` and the actual project root. For an existing initiative, run CLI `resume` and `validate`; compare approval, checkpoint, journal, code and Git. The `CHECKPOINT.md` is only an index.
- For a new authorized initiative, run `python <skill-root>/scripts/project_state.py init --project-root <repo> --initiative-id <id> --scope page --next-action "Inventory billing routes and states"` (change scope and action). **Use the returned directory** for all subsequent artifacts. For Git projects, new state goes in `.artifacts/prototype-to-production/<id>/`, ignored locally through Git's exclude file without modifying tracked `.gitignore`. Non-Git projects keep `design/redesigns/<id>/`; existing initiatives in either place are resumed in place. Never move or delete legacy artifacts automatically.
- Store design notes, source provenance, runnable prototypes, screenshot references, QA reports, test inputs, browser outputs and slice packets **inside the initiative directory**, not beside app source. Do not create untracked project-root `.playwright-mcp/` or loose screenshots where output paths can be configured. If an external tool necessarily writes outside, record and explicitly ignore its cache or relocate only your own new files safely.
- A tracked `AGENTS.md` pointer is **optional** and requires the user's approval; never insert it as part of default initialization. Preserve existing instructions and unrelated changes. Do not auto-commit, stage, deploy or change tracked `.gitignore`.

## 1. Discover what app A actually does

Enumerate all in-scope routes, nested views, role/permission behavior, real data shapes, API interactions, empty/loading/error states, filters, sort/pagination, form validation, dialogs, keyboard actions and navigation. Capture before-screenshots and current tests. Make an A→B functional coverage map; note which behaviors are not displayed in the small anchor prototype. Preserve business behavior during redesign unless the user explicitly authorizes a functional change.

## 2. Design and run prototype B

Use the **current project's** written brief or images as the creative direction. Aesthetic choices such as layout density, palette, typography, shape and tone must come from that brief and iteration with the user; do not impose a universal design style. Build **runnable/exportable frontend source**, not a one-time render or hand-drawn summary. It can be previewed separately, but store exact source/CSS/assets in `prototype/versions/<version>/`, and reference PNGs in `references/<version>/`. Realistic test data must have a **replayable clean-browser fixture loader** that restores app-relevant IDs, timestamps, selected records, permissions, locale, storage and binary resources on a new run. A saved JSON snapshot that the prototype cannot import does not meet this gate. Save browser replay source and a report from at least **two clean contexts**; inspect and compare the reference pixels rather than trusting a test-summary count.

Mark test/fixture toolbars, fake controls, debugger toggles and QA-only banners as prototype-only. Separate them by a test mode or hide them from final product reference captures; declare their exclusion explicitly in the approval and slice contracts. A locked full-page screenshot containing a testing toolbar becomes an impossible implementation target. Keep legitimate product role controls and truthful disclosure if they are actually part of app A; distinguish those from QA harness UI.

Record exact design tokens, grid/container rules, spacing, type scale, density, labels, interaction states and responsive behavior **as chosen for this initiative**. Check legibility and usable mobile interaction targets; do not sacrifice basic readability just to satisfy a desired page density. Check 320, 390, 768, 1024 and 1440px, plus breakpoint neighbors and long supported languages where relevant. Test empty, loading, error, permission, lifecycle and modal states **only where the actual app supports them**. State any uncovered cases honestly before approval.

Iterate with the user while design is unapproved. After each material revision, record why it changed and which files/screenshots represent it. If a reference image is unavailable, request a reattachment; never pretend an earlier image was preserved. When significant visual details are unspecified, offer a targeted prototype variation or ask a question; do not substitute the pilot style or the implementer's own design preferences.

## 3. User approves **the exact artifact version**

Display the actual prototype and covered states/viewport dimensions, disclose any gaps, and ask the user to approve this version for faithful implementation. A positive reaction to a transient preview is not yet a recoverable file artifact. Preserve exact source files and every screenshot, state fixture and design asset in the project first.

Create approval JSON using `assets/project/approval.template.json`: `decision_id`, `approved_version`, `scope`, `mode` (`source` or `image-only`), `approved_by_user: true`, the user's actual approval note, `source_paths`, optional `asset_paths`, `visual_cases` (`case_id`, `reference_file`, `route`, `viewport.width/height`, `fixture`, `state_setup`), and `exceptions`. For **source-mode approvals**, also provide `replay` with an executable versioned script, a real browser command, a saved versioned PASS report covering each visual case from two clean browser contexts and an explicit QA-controls exclusion. The utility hashes the replay script/log together with source, screenshots and fixtures and **rejects** missing or inconsistent replay evidence. Only record `approved_by_user: true` after the user actually approves the exact preview and scope. Every path is relative to the initiative directory, not the repo root.

Run `approve --approval-file <json>` with root and initiative ID. Never manually alter locked files. Source-unavailable image-only approval requires separate `image_only_approved: true`, `image_only_reason` and informed user approval; downstream reconstruction limitations remain explicit.

All new visual changes create a **new version + new explicit approval**, retaining old approvals and decisions. `revoke-approval --reason ...` stops work under the old lock. `validate` detects any missing or changed locked source, screenshot, fixture or asset.

## 4. Map all app A screens to B patterns, then slice

Extract shared CSS tokens and component rules straight from B; create a coverage matrix of every old route/state and its approved target pattern. **A few approved anchor pages do not authorize inventing the rest of the app.** If a new dialog/table/page variant is unspecified, design an additional approved prototype anchor before assigning that slice. First create coordinator-owned shared foundations (layout, nav, type, input/table system) if required.

Create one vertical slice per usable flow, with independent user-visible behavior and tests. Fill `assets/project/slice.template.json` with real code paths, locked B sources and exact visual cases, current app A behavior, tokens, responsive variants, dependencies, exclusive `allowed_paths`, acceptance/functional checks, prohibited deviations (including **no QA-only controls**) and owner. Run CLI `slice-create --contract-file <json>`. It rejects unknown/superseded references, missing cases and overlapping paths. Dependencies stay `blocked` until accepted or **provisionally verified with an independently reviewed capture-only warning**; provisional is permission to continue dependent implementation, not final acceptance.

If a task is withdrawn or replaced, run `slice-cancel --slice-id <id> --reason "..."` instead of deleting its packet or rewriting history. The cancelled slice retains evidence and releases file ownership for a new, separately identified slice. It does not undo implementation code.

Independent Codex sessions may each receive a packet, but **do not spawn agents automatically**. Each worker first resumes and validates state, implements only its owned paths, runs behavior/visual checks, writes `slice-report --slice-id ... --owner ... --report-file ...` and stops. Only the coordinator updates approvals and global slice status.

## 5. Exact implementation and visible verification

Integrate approved prototype markup and CSS into the real app's data, routes, components, permission logic, errors and tests. If exact B source cannot fit without changing the design, document the conflict and request user direction. Never substitute a loosely similar table/card/button. Avoid unrelated refactors and destructive repo operations.

Coordinator first runs `reconcile --slice-id ... --action report`. Then use `references/visual-qa.md` to operate the actual browser. Create an explicit comparison matrix: in-scope implemented **B case vs identical approved B case**; unchanged/out-of-scope **A after vs A before**. A-vs-B cross-document comparisons are diagnostic only. Capture the right fixture/state/viewport; inspect layout, text, overflow, interactions and changed pixels. On a mismatch, diagnose the cause and retry **three times in clean contexts**. Real UI defects and incomplete functional coverage remain BLOCKED: coordinator runs `reconcile --action block --review-file <defect.json>` to synchronize slice status, evidence, checkpoint and dependent tasks. After fixing, use `reconcile --action unblock --review-file <resolution.json>`, then re-report and reverify; do not make the user manually edit the manifest. Only independently reviewed, insignificant capture-only differences can be recorded as `WARNING` with three retry PNGs and a hashed diagnostic; `reconcile --action verify --review-file <json>` then marks the B-case slice `provisional`, allowing downstream implementation. This is **not PASS**. Record **supplemental** capture warnings (including unchanged A-page regressions and informational flaky captures) using `warning-record`; it hashes the evidence and creates a side-by-side HTML gallery with labeled Before/After images. Pending warnings require `warning-decide` after the user reviews the gallery, before `reconcile --action accept`. A provisional B-case warning additionally needs `reconcile --action accept-warning --review-file <user-decision.json>`. Never fabricate review/approval, relax thresholds globally or update locked references. A compiled UI, worker confidence, self-review, or unavailable browser does not pass. If a failure stems from missing B direction, return to prototype/approval; never reinterpret it silently.

For each provisional approved-B case, the verifier automatically saves a side-by-side HTML gallery of the initial mismatch and all three retries, with hashes and a checkpoint link. Supplemental warning galleries remain separate from the approved-case result.

## 6. Integrate, recover, and continue

Run `validate` before integration and after any material code change. It checks approved prototype/reference/replay hashes, slice packet hashes, accepted screenshot/test evidence and implementation source hashes. Run `resume` after compaction, each new session and handoff. State the approval ID/version, completed/blocked slices and next action from files, not memory. Compare actual code and Git state. Ask the user before merging, committing app changes or deploying. Ignored artifacts survive **same-workspace** sessions only; before handing work to an agent in a different clone/worktree/machine, transfer the complete initiative directory through an authorized shared workspace or verified archive and validate its hashes there. Never presume a Git clone contains ignored artifacts. Missing state is **BLOCKED**, not an invitation to reconstruct it from prose.

## CLI command summary

All commands require `--project-root <real-repo-root> --initiative-id <id>` after the subcommand.

| Command | Additional arguments | Role |
| --- | --- | --- |
| `init` | `--scope page` (or feature/app), `--next-action "..."` | Coordinator |
| `event` | `--event-file <json>` | Coordinator |
| `checkpoint` | `--checkpoint-file <json>` | Coordinator |
| `approve` | `--approval-file <json>` | Coordinator after user approval |
| `revoke-approval` | `--reason "..."` | Coordinator with reason |
| `slice-create` | `--contract-file <json>` | Coordinator |
| `slice-cancel` | `--slice-id <id> --reason "..."` | Coordinator |
| `slice-report` | `--slice-id <id> --owner <name> --report-file <json>` | Worker, slice-local only |
| `reconcile` | `--slice-id <id> --action report/block/unblock/verify/accept/accept-warning`; all but `report` and `accept` need `--review-file <json>` | Coordinator; block/unblock status transitions and verification |
| `warning-record` | `--warning-file <json>` | Coordinator; save immutable warning evidence and a side-by-side HTML gallery |
| `warning-decide` | `--warning-id <id> --decision-file <json>` | Coordinator after explicit user acceptance or rejection of the displayed warning |
| `resume`, `validate` | No additional flags | Any reader; stop on BLOCKED |

Input JSON files are created in the app project, and may be versioned for audit. The utility never installs a browser, starts a dev server, spawns agents, commits app code or deploys. Manual actual-browser setup and honest visual review remain required.
