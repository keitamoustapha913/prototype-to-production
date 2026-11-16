# Self-contained vertical-slice contracts

The coordinator issues one `slices/<id>/contract.json` per end-to-end user flow, not per disconnected CSS task. A worker should be able to complete its slice with only the repository, this contract, the exact approved references, and existing app tests. When a referenced design detail is absent, report BLOCKED and ask the coordinator; never infer it from generic design taste.

In Git projects the handoff lives under ignored `.artifacts/prototype-to-production/<id>/`. If a worker uses a **different** clone, worktree, computer or isolated filesystem, the coordinator must explicitly transfer the complete hashed initiative evidence first; Git alone will not include it. A worker lacking that directory must stop, never recreate the prototype from a prose task packet.

## Required contract content

Read the `assets/project/slice.template.json` example. Fill every field with **actual** paths and behavior from application A and approved B. Especially:

- `scope`, `routes`, `owner`, `dependencies`, `allowed_paths`: affected user flow, exclusive path ownership (`file` or `directory/**`), and prerequisites. Shared tokens/components are coordinator-owned slices done first; do not assign overlapping files to parallel workers.
- `current_behavior` and `functional_tests`: every filter, navigation, form validation, loading/empty/error/permission state, keyboard action, and workflow that application A already supports. The prototype's omissions do not authorize feature removal.
- `prototype_version`, `prototype_source_paths`, `approval_id`, `approved_artifact_hashes`: the exact reusable source and assets for B. Prefer adapting that source within existing app infrastructure. For image-only exceptions, explicitly state reconstruction status and uncertainty.
- `visual_cases`: actual `case_id`, `route`, locked `reference_file`, viewport width/height, named fixture file, and state setup. The case must exactly match one in the active user approval. Provide desktop/mobile and significant interaction states; newly needed visual patterns require approval before dispatch.
- `design_tokens`, `acceptance_checks`, `prohibited_deviations`: explicit component hierarchy, spacing scale, grid columns, alignment, widths/heights, typography, row density, labels, truncation, icons and error/help text. Tie numerical requirements to approved CSS and pixels, even when the approved design is intentionally spacious, dense or unconventional. **Exclude prototype-only fixture selectors, reset buttons, harness banners and testing affordances** from production source. Prevent accidental overflow, inaccessible controls and false status claims; do not restyle the user's approved visual direction.
- `evidence`, `blockers`: actual screenshots and test output must be produced in verification; do not mark a task accepted merely because it compiles.

The utility locks the contract's hash. Alterations after dispatch invalidate reconciliation. A task whose dependency is neither accepted nor **provisionally verified under an independently reviewed three-retry capture warning** is `blocked`. Provisional dependencies may release downstream implementation, but remain outstanding for final user acceptance.

## Worker protocol

1. Run `resume` and `validate`; read the slice packet, locked prototype source/reference pixels, executable fixture setup and replay proof; compare current Git and user modifications. If ignored state is absent in your worktree or the approval is stale, **stop**.
2. Work only inside `allowed_paths`; if the real app requires a shared component or another file, request a coordinator-owned prerequisite, do not reach across ownership boundaries.
3. Implement current behavior end-to-end while reusing prototype markup/tokens. Preserve unrelated code changes. Run relevant behavior checks and capture visual evidence at every specified case.
4. Write a worker JSON file with `slice_id`, `owner`, `status` (`submitted` or `blocked`), `summary`, `implementation_paths`, `source_revision`, `functional_evidence`, `visual_evidence`, `blockers` and `next_action`.
5. Run `slice-report --slice-id ... --owner ... --report-file ...`; this writes only within your slice directory. **Never** directly edit global approval, manifest, or checkpoint files.
6. The coordinator runs `reconcile --action report`, inspects the worker's code and evidence, then separately accepts or rejects according to the visual verification gate. A submitted report is not acceptance.

## Coordinator acceptance

Confirm current prototype lock hashes, contract hash, correct screenshot-to-route/state mapping, reviewer confirmation, interaction/behavior tests, code scope, existing user changes, and no unresolved blockers. Record explicit PASS/FAIL/BLOCKED per case, with **WARNING only for documented negligible capture instability after three clean retries and independent visual review**. A warning is `provisional`, not accepted; its screenshot/diagnostic hashes are preserved and downstream implementation can continue. Final acceptance requires explicit user warning acceptance via `reconcile --action accept-warning --review-file ...`. Real UI defects, missing screenshot/browser, unknown or incorrect comparison targets, and functional failures remain BLOCKED. If discrepancies require new design invention, request user decision, save it, version the prototype and dispatch a replacement slice.
