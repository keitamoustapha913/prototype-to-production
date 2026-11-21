# Durable state and recovery protocol

Everything material lives in the application workspace, inside the initiative directory returned by `scripts/project_state.py init` or `resume`. In a Git project the default is locally ignored `.artifacts/prototype-to-production/<initiative-id>/`; non-Git projects and preexisting initiatives may use `design/redesigns/<initiative-id>/`. The global installed skill never stores app decisions. Chat memory, compaction and a checkpoint summary are not approval sources.

## Mode and conversation recovery

New initiatives persist `initiative_mode: blueprint`; an absent field means legacy prototype and never triggers migration. Blueprint phases add `plan`, `blueprint`, and `slice_review`. Read [blueprint-workflow.md](blueprint-workflow.md) for exact conversation, roadmap, coverage and approval contracts; the runnable B-specific requirements below apply only to prototype mode.

For blueprint work, persist each exact material question/proposal, options, tradeoffs and actual user response through `conversation-record`. Corrections and rejected alternatives remain as linked append-only records. `work-update` accepts `pending_question_id` with `waiting_user`. Resume returns the original pending question, all structured exchanges and the full journal path; answered questions do not need to be asked again. Preserve a decision recap as linked evidence before approval and after amendments. Complete raw transcript capture is explicit opt-in and must disclose supported capture scope or missing history.

For [guided design review](guided-design-review.md), recover the current workstream and revision from the saved `review_checkpoint` and work cursor. Use the recorded position, finished and pending steps, presentation paths, and linked response to continue the same discussion. A workstream acknowledgment leaves the checkpoint open. Explicit continuation, revisits and scoped skips are recorded separately from A+ design decisions and formal approval.

During slice review, preserve the current ID and revision, open A+ question, completed review IDs, impacted dependents, next action, and paths. Use `slice-review-record` alongside `work-update` with phase `slice_review`. For an open question, record the A+ exchange and attach its ID to the open slice. On return, run `validate` and read `resume --format json` for the original wording and recorded answer. Continue from the saved slice. Use the [review workflow](slice-review-workflow.md) for amendments, interrupted transactions and exact package transfer.

For UI implementation, read the slice's `visual_baselines` and `cross_screen_changes` in the resume packet. They point to preserved images, approved references, capture conditions and outstanding gaps. Follow [Visual Baseline Capture](visual-baseline.md) before editing affected UI.

Use immutable versioned plan, blueprint and coverage records; approval hashes protect those files plus their baseline and recorded decisions. A new version cannot silently rewrite an old approval. Missing-feature recovery requires a fresh audit, coverage and explicit reapproval in the initiative's own mode.

## Git ignore and portability

Every CLI command in a Git repository checks that the initiative's files are untracked and ignored. For a reviewed blueprint initiative, `init` and `artifact-setup` can add a narrow entry in Git's local `info/exclude`. Run `artifact-setup` in a receiving checkout that lacks that entry before resuming. Legacy commands can configure the local exclusion during ordinary use. New initiatives use the hidden directory, and existing `design/redesigns/<id>/` initiatives stay at their original path. If both paths contain the same ID, a symlink escapes the workspace, or initiative artifacts are tracked, stop and reconcile the reported conflict.

**Tradeoff:** Ignored artifacts are not included in Git clones, PRs or independent worktrees. They persist through context compaction and fresh Codex CLI sessions in the **same local workspace**. For a different machine/agent checkout, explicitly transfer the complete initiative directory, including locked sources, screenshots, fixtures, replay logs, manifest and decisions journal, by an authorized channel; validate there. If it is unavailable, report `BLOCKED`. Do not claim cross-machine durability from ignore rules alone. The user owns backups of ignored state; offer an archive/export when useful, without silently uploading sensitive app data.

## Authority order

1. Actual repository files, current Git status, and SHA-256 of locked artifacts.
2. `manifest.json`, `decisions.jsonl` (append-only), immutable discovery audits, A-to-B functionality coverage, postapproval missing-feature reports, versioned prototype source/assets, screenshots, and slice contracts.
3. `CHECKPOINT.md`, a human-readable **index**, not an override of the manifest or evidence.
4. Chat history, useful for discussion but not for reconstructing unrecorded approvals.

Only a **coordinator** updates `manifest.json`, global decisions, approvals, or global checkpoints. Workers read them and write only `slices/<slice-id>/worker-report.json`; the coordinator runs `reconcile` after checking the report. The utility serializes coordinator updates with a project-local file lock and atomically replaces JSON/Markdown manifests. Do not delete or rewrite decision history. Do not manually edit it when there is a supported command.

## Record decisions immediately

At every material user decision, style choice, interaction requirement, new prototype variant, blocker, ownership assignment, source change, test outcome, or next-step change:

1. Save the exact design/source/artifact/code references inside the initiative directory, including runnable fixture loaders, replay results and screenshot state/viewport when relevant. Mark test-only controls separately from product UI.
2. Write a typed event JSON and run `python <skill>/scripts/project_state.py event --project-root <repo> --initiative-id <id> --event-file <json>`.
3. Write a checkpoint JSON (`summary`, `completed`, `next_action`, `blockers`, optional `repository_head` and `worktree_status`) and run `checkpoint ... --checkpoint-file <json>`.
4. Verify `manifest.json`, `CHECKPOINT.md`, and the actual files agree before continuing. Avoid leaving the only record in chat.

The coordinator must use `reconcile --action block` whenever a confirmed defect stops a slice. A sentence such as "Task 1 is blocked" in a checkpoint without the canonical slice status `blocked` is an invalid handoff; `validate` checks status/blocker consistency. After correction, use `reconcile --action unblock` with a resolution note and reverify. Checkpoints show all currently blocked slices and their reasons, rather than relying on a manually maintained duplicate list.

Save all material screenshot warnings with `warning-record`, including unchanged-page (A-before/A-after) comparisons that **must not** be mislabeled as approved-B failures. Its evidence register protects the report, before/after PNGs, retries, investigation files and side-by-side HTML gallery with SHA-256 hashes; `validate` catches later changes and missing files. Use unique filenames per run and never overwrite evidence. When something was already lost, record that loss honestly, preserve remaining independent retries, and ask the user to accept or reject the evidenced limitation. A hash detects change/loss; **it is not a backup**. Ignored evidence still needs a verified archive or transfer before moving to another worktree/computer.

For app A discovery, use `discovery-record` to preserve the actual source anchors, browser observations, all audited feature IDs, route/source sweeps and a final zero-new-feature pass. Use `prototype-coverage` to freeze per-feature tested B interactions under a **specific** version before seeking approval. Both records and their evidence hashes are checked by `validate` after context compaction; a mere natural-language assertion that B preserves A is not enough. The installed skill is generic; project-specific decisions and A feature IDs belong only inside the active initiative.

When any additional A behavior is reported after approval, use `missing-feature --report-file` to atomically write its observation evidence and mark `coverage_pending` and a blocker in `manifest.json`/`CHECKPOINT.md`. **Do not manually mark the project ready**, continue implementation, or silently rewrite a previous user approval. A full follow-up discovery sweep is required; if any real gap remains, a new B version, fresh coverage and fresh explicit user approval are required. Even a zero-new sweep cannot dismiss a reported gap without the user's recorded correction and evidence. Earlier approved versions, evidence and decisions remain preserved.

Commands consume **real JSON files**; save intermediate payloads under `inputs/` inside the ignored initiative directory returned by `init` or `resume` (for example `.artifacts/prototype-to-production/<id>/inputs/` in Git repositories). Keep design documents, plans, prototype source and preview assets there too, including documents produced by other planning workflows; do not create them in tracked `docs/`, `design/`, or application directories. Check each destination with `git ls-files` and `git check-ignore` before writing. Only approved app implementation changes go into tracked code. Do not write secrets into artifacts or reports.

## Mandatory durable task cursor, including during long sessions

The general checkpoint alone cannot tell a fresh agent **what work was in progress**. The coordinator must create and maintain exactly one active task through `work-update --work-file <json>`, using `assets/project/work.template.json`. Store the real phase (`discover`, `plan`, `blueprint`, `prototype`, `approved`, `sliced`, `implement`, `verify` or `integrate`; `plan`/`blueprint` apply only to blueprint mode), stable `task_id`, `status` (`in_progress`, `blocked`, `waiting_user`, `done`), concise summary, completed and pending steps, exact next action, relevant existing code and evidence paths, and blockers. No task is implicitly complete because a command ended or a chat message was truncated.

1. **Before work begins:** save the task cursor **before** making substantial code, design, or verification changes. The utility locks the coordinator, appends the work event to the journal, and safely replaces `manifest.json` and `CHECKPOINT.md`, with recorded code/evidence hashes and Git information. If the process crashes partway through these separate file writes, compare the journal and manifest before continuing.
2. **During work:** refresh it after every meaningful inspection, file edit, test, blocker or decision, **before context compaction** when possible, and before any long operation or intended handoff. Explicitly move finished steps to `completed_steps` and keep uncompleted work in `pending_steps`. Work updates may only advance the existing active task; unfinished tasks cannot be silently replaced or marked done with pending steps.

When a blueprint command advances the initiative stage, set the phase in the next `work-update` to the stage reported by `resume`. Carry forward earlier completed and pending step strings verbatim. Move a pending step to the completed list only after it is done. If the new phase and the saved cursor disagree after an interruption, use that update to reconcile the same task and link any saved pending question.

3. **When blocked or waiting:** set `status: blocked` and preserve concrete blocker reasons, or `status: waiting_user` when a genuine user decision is needed. Do not present either state as ready or quietly clear it using a generic checkpoint. Update the task before trying a different phase.
4. **On completion:** save `status: done`, empty pending steps and blockers, the real outcome and an exact `next_action`. The completed task remains in durable history. Create the next task only afterward. Versioned user decisions, approvals and screenshot references remain immutable throughout.

The skill cannot install a hidden hook into an arbitrary Codex context-compaction event. Persistence therefore comes from **preemptive, frequent, atomic work checkpoints**, not from expecting the model to remember to write one after memory has already been compacted. Changes made after the last checkpoint may need inspection; the saved code/evidence hashes help detect them rather than pretending they never happened.

## Recovery on a fresh session or after compaction

1. Read the repo `AGENTS.md`. Run `resume --project-root <repo> --format json` first. It locates the sole saved initiative automatically; if multiple initiatives exist, select the correct one with `--initiative-id`, never guess. A new session does not require the user to repeat the previous prompt or choose the phase again.
2. Read the returned `active_work` (task ID, phase, status, completed/pending steps, exact next action, code/evidence paths), `approval_id`, audit, `design_direction`, `recent_decisions`, full `decision_journal_file`, `safe_to_continue`, and `reconciliation_required`. The latest 30 decisions are a quick index, **not** the entire decision history; inspect the complete append-only journal and relevant original approval/source files before altering any previously chosen behavior or visual. Read `manifest.json`, `CHECKPOINT.md`, and relevant slice contract for supporting details. Treat the continuation cursor as authoritative for unfinished work; do not repeat completed steps or regenerate approved designs.
3. Run `validate` to check baseline and approval hashes, the mode's coverage evidence, postapproval missing-feature reports, and slice receipts. Blueprint mode checks its plan, specifications, conversation records, and reviewed test and render output. Prototype mode checks runnable-B lineage, fixtures, replay code, visual cases, and registered warning evidence. Compare current Git HEAD/status and relevant code with recorded provenance. Missing ignored artifacts after a clone or a hash mismatch are blockers.
4. State the active approved version, latest baseline audit, current mode's coverage version, any `coverage_pending` missing-feature reports, completed slices, current ownership, blockers, and **next safe action**. If `safe_to_continue` is true and the active task is `in_progress`, **continue its next unfinished step automatically**. For `waiting_user`, use `active_question_answer` when present to resume the same task. Otherwise restore the pending question and await its answer. For `blocked`, resolve the exact blocker. Inspect any reported evidence or Git changes before advancing.
5. After an interruption or context overflow, reconcile any code/evidence changes since the last recorded cursor, then persist a `work-update` with the verified state. A resumed task remains active until the actual pending steps are completed; do not claim success merely because recovery succeeded. If a critical earlier decision was never recorded, ask the user again; do not make it up.

## Version and change control

Each approval is a separately identified, hash-locked record in `manifest.json` and `decisions.jsonl`. Changing a file in place does not update its approval. Prototype changes require a new version and explicit user approval; `revoke-approval` preserves history. Keep previous immutable version directories for audit. Image-only reconstruction is a lower-confidence path allowed only after an explicit user decision, never as a silent substitution for the prototype source.

## Git and recovery limitations

Store source paths, actual git HEAD/status, and relevant commit/diff references in checkpoints. This utility does not commit, stash, revert, reset, merge, or deploy the user's code. If Git is unavailable, describe that limitation and read relevant code manually. Files and event history can recover only choices that were persisted before compaction; the skill cannot recover unrecorded chat details.

When resuming completed or deferred work, use [Visual Quality status](visual-quality.md) to recover the current technical report and its acceptance. Preserve the `visual-quality/` files with the initiative and use the exact audit ID for user acceptance.
