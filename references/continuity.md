# Durable state and recovery protocol

Everything material lives in the application workspace, inside the initiative directory returned by `scripts/project_state.py init` or `resume`. In a Git project the default is locally ignored `.artifacts/prototype-to-production/<initiative-id>/`; non-Git projects and preexisting initiatives may use `design/redesigns/<initiative-id>/`. The global installed skill never stores app decisions. Chat memory, compaction and a checkpoint summary are not approval sources.

## Git ignore and portability

On `init` in a Git worktree, the utility adds a narrowly scoped local exclusion in Git's `info/exclude` if needed; it does **not** edit tracked `.gitignore`, stage files or untrack previously committed artifacts. New initiatives are created in the hidden directory. Existing `design/redesigns/<id>/` initiatives stay at their original path and are locally ignored when `init` is rerun there. If both paths contain the same ID, a symlink escapes the workspace, or artifacts are already tracked, stop and ask for reconciliation; do not auto-move, delete or run `git rm --cached`.

**Tradeoff:** Ignored artifacts are not included in Git clones, PRs or independent worktrees. They persist through context compaction and fresh Codex CLI sessions in the **same local workspace**. For a different machine/agent checkout, explicitly transfer the complete initiative directory, including locked sources, screenshots, fixtures, replay logs, manifest and decisions journal, by an authorized channel; validate there. If it is unavailable, report `BLOCKED`. Do not claim cross-machine durability from ignore rules alone. The user owns backups of ignored state; offer an archive/export when useful, without silently uploading sensitive app data.

## Authority order

1. Actual repository files, current Git status, and SHA-256 of locked artifacts.
2. `manifest.json`, `decisions.jsonl` (append-only), versioned prototype source/assets, screenshots, and slice contracts.
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

Commands consume **real JSON files**; use ordinary project files for intermediate payloads (e.g. `design/redesigns/<id>/inputs/`), so intent is reviewable. Do not write secrets into artifacts or reports.

## Recovery on a fresh session or after compaction

1. Read the repo `AGENTS.md` and the active redesign pointer if one exists. Identify the exact initiative ID and locate it using the skill script; do not guess between hidden/legacy paths or similarly named projects.
2. Run `resume --project-root ... --initiative-id ...`; read `manifest.json`, `CHECKPOINT.md`, recent journal events, and relevant slice contract. Read the approval entry and its paths.
3. Run `validate` to hash-check the locked sources, screenshots, fixtures, replay code, blocker evidence, and every registered warning's gallery/PNG/log hashes; compare current Git HEAD/status and relevant code with recorded provenance. Missing ignored artifacts after a clone or a hash mismatch are blockers; only the original approval process can authorize new artifacts.
4. State the active approved version, completed slices, current ownership, blockers, and **next safe action**. Resume only after unresolved discrepancies are reconciled.
5. Record the rehydration findings as an event/checkpoint, including any new blockers. If a critical earlier decision was never recorded, ask the user again; do not make it up.

## Version and change control

Each approval is a separately identified, hash-locked record in `manifest.json` and `decisions.jsonl`. Changing a file in place does not update its approval. Prototype changes require a new version and explicit user approval; `revoke-approval` preserves history. Keep previous immutable version directories for audit. Image-only reconstruction is a lower-confidence path allowed only after an explicit user decision, never as a silent substitution for the prototype source.

## Git and recovery limitations

Store source paths, actual git HEAD/status, and relevant commit/diff references in checkpoints. This utility does not commit, stash, revert, reset, merge, or deploy the user's code. If Git is unavailable, describe that limitation and read relevant code manually. Files and event history can recover only choices that were persisted before compaction; the skill cannot recover unrecorded chat details.
