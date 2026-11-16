# From existing UI A to approved frontend B

This process supports a single feature, page or entire existing SaaS app. Input may be plain language, screenshots, design references, or combinations. A complete redesign is a **set of mapped user journeys and UI states**, not permission to discard the functionality of A.

## 0. Initialize / resume before touching code

- Inspect repository README, existing AGENTS rules, frontend framework, routes, component library, tokens, API layer, automated tests, browser tooling and current Git/worktree changes. Respect all unrelated modifications.
- Pick a stable initiative ID such as `billing-redesign`, and record the exact repo root. For an existing initiative, run CLI `resume` and `validate` first, and review approval, checkpoint, journal, code and Git. If artifacts disagree, stop and record the blocker. The `CHECKPOINT.md` is only an index.
- For a new authorized initiative, run `python <skill-root>/scripts/project_state.py init --project-root <repo> --initiative-id <id> --scope page --next-action "Inventory billing routes and states"` (change scope and action). Save findings, style preferences, reference-image paths and source-code provenance in project files, and record typed `event` plus `checkpoint` immediately.
- If authorized, append the minimal `assets/project/AGENTS-snippet.md` to the project's **existing** AGENTS.md (or create only if absent). Fill the real ID; never overwrite previous instructions or make global skill files the app source of truth.

## 1. Discover what app A actually does

Enumerate all in-scope routes, nested views, role/permission behavior, real data shapes, API interactions, empty/loading/error states, filters, sort/pagination, form validation, dialogs, keyboard actions and navigation. Capture before-screenshots and current tests. Make an A→B functional coverage map; note which behaviors are not displayed in the small anchor prototype. Preserve business behavior during redesign unless the user explicitly authorizes a functional change.

## 2. Design and run prototype B

Use supplied description or images as creative input. Build **runnable/exportable frontend source**, not a one-time render or hand-drawn summary. It can be previewed separately, but store its exact source, CSS, assets and realistic fixture/state setup in `prototype/versions/<version>/`, plus reference PNGs under `references/<version>/`. Record design tokens, grid/container rules, spacing, type scale, density, labels, interaction states and responsive behavior explicitly—prefer source CSS values and screenshots to subjective adjectives. For real user flows show anchors and important desktop/mobile/empty/loading/error/modal states.

Iterate with the user while design is unapproved. After each material revision, record why it changed and which files/screenshots represent it. If a reference image is unavailable, request a reattachment; never pretend an earlier image was preserved. Do not use placeholder giant cards, excessive text, unaligned tables or generic spacious layouts to fill gaps in direction.

## 3. User approves **the exact artifact version**

Display the actual prototype and covered states/viewport dimensions, disclose any gaps, and ask the user to approve this version for faithful implementation. A positive reaction to a transient preview is not yet a recoverable file artifact. Preserve exact source files and every screenshot, state fixture and design asset in the project first.

Create approval JSON with `decision_id`, `approved_version`, `scope`, `mode` (`source` or `image-only`), `approved_by_user: true`, `user_approval_note`, `source_paths`, optional `asset_paths`, `visual_cases` (`case_id`, `reference_file`, `route`, `viewport.width/height`, `fixture`, `state_setup`), and `exceptions`. Every path is **relative to the initiative directory**, not the repo root. Run CLI `approve --approval-file <json>` with root and initiative ID. It records SHA-256 and the approval event; never manually alter frozen files. A source-unavailable path requires explicit `image_only_approved: true`, `image_only_reason` and an informed user approval, with its reconstruction limitations noted in downstream slices.

All new visual changes create a **new version + new explicit approval**, retaining old approvals and decisions. `revoke-approval --reason ...` stops work under the old lock. `validate` detects any missing or changed locked source, screenshot, fixture or asset.

## 4. Map all app A screens to B patterns, then slice

Extract shared CSS tokens and component rules straight from B; create a coverage matrix of every old route/state and its approved target pattern. **A few approved anchor pages do not authorize inventing the rest of the app.** If a new dialog/table/page variant is unspecified, design an additional approved prototype anchor before assigning that slice. First create coordinator-owned shared foundations (layout, nav, type, input/table system) if required.

Create one vertical slice per usable flow, with independent user-visible behavior and tests. Fill `assets/project/slice.template.json` with real code paths, locked B sources and exact visual cases, current app A behavior, tokens, responsive variants, dependencies, exclusive `allowed_paths`, acceptance/functional checks, prohibited deviations and owner. Run CLI `slice-create --contract-file <json>`. It rejects unknown/superseded references, missing cases and overlapping paths. Pending dependencies produce `blocked` status, not permission to implement.

If a task is withdrawn or replaced, run `slice-cancel --slice-id <id> --reason "..."` instead of deleting its packet or rewriting history. The cancelled slice retains evidence and releases file ownership for a new, separately identified slice. It does not undo implementation code.

Independent Codex sessions may each receive a packet, but **do not spawn agents automatically**. Each worker first resumes and validates state, implements only its owned paths, runs behavior/visual checks, writes `slice-report --slice-id ... --owner ... --report-file ...` and stops. Only the coordinator updates approvals and global slice status.

## 5. Exact implementation and visible verification

Integrate approved prototype markup and CSS into the real app's data, routes, components, permission logic, errors and tests. If exact B source cannot fit without changing the design, document the conflict and request user direction. Never substitute a loosely similar table/card/button. Avoid unrelated refactors and destructive repo operations.

Coordinator first runs `reconcile --slice-id ... --action report`. Then use `references/visual-qa.md` to operate the actual browser, capture implementation PNGs, compare against locked screenshots at the same fixture/state/viewport and inspect layout, text, overflow and interactions. Record real test output and reviewer notes in a JSON review file. Run `reconcile --action verify --review-file <json>` (this must PASS) and separately `reconcile --action accept`. A compiled UI, worker confidence, self-review, or unavailable browser does not pass. If a failure stems from missing B direction, return to prototype/approval; never reinterpret it silently.

## 6. Integrate, recover, and continue

Run `validate` before integration and after any material code change. It checks approved prototype/reference hashes, slice packet hashes, accepted screenshot/test evidence and implementation source hashes. Run `resume` again after compaction, every new Codex session and every agent handoff. State the approval ID/version, completed/blocked slices and next safe action from project evidence. Compare Git HEAD/status and actual code; log discrepancies. Ask the user for review before merging, committing their app work, or deploying. The tool cannot resurrect conversation decisions that were never recorded; persist material decisions immediately.

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
| `reconcile` | `--slice-id <id> --action report/verify/accept`; `verify` also needs `--review-file <json>` | Coordinator |
| `resume`, `validate` | No additional flags | Any reader; stop on BLOCKED |

Input JSON files are created in the app project, and may be versioned for audit. The utility never installs a browser, starts a dev server, spawns agents, commits app code or deploys. Manual actual-browser setup and honest visual review remain required.
