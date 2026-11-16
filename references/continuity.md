# Durable state and recovery protocol

Everything material lives in the application's Git worktree, under `design/redesigns/<initiative-id>/`. The global skill never stores app decisions. A conversation transcript, context compaction, or checkpoint summary is not an approval source.

## Authority order

1. Actual repository files, current Git status, and SHA-256 of locked artifacts.
2. `manifest.json`, `decisions.jsonl` (append-only), versioned prototype source/assets, screenshots, and slice contracts.
3. `CHECKPOINT.md`, a human-readable **index**, not an override of the manifest or evidence.
4. Chat history, useful for discussion but not for reconstructing unrecorded approvals.

Only a **coordinator** updates `manifest.json`, global decisions, approvals, or global checkpoints. Workers read them and write only `slices/<slice-id>/worker-report.json`; the coordinator runs `reconcile` after checking the report. The utility serializes coordinator updates with a project-local file lock and atomically replaces JSON/Markdown manifests. Do not delete or rewrite decision history. Do not manually edit it when there is a supported command.

## Record decisions immediately

At every material user decision, style choice, interaction requirement, new prototype variant, blocker, ownership assignment, source change, test outcome, or next-step change:

1. Save the exact design/source/artifact/code references in the project, including screenshot state and viewport when relevant.
2. Write a typed event JSON and run `python <skill>/scripts/project_state.py event --project-root <repo> --initiative-id <id> --event-file <json>`.
3. Write a checkpoint JSON (`summary`, `completed`, `next_action`, `blockers`, optional `repository_head` and `worktree_status`) and run `checkpoint ... --checkpoint-file <json>`.
4. Verify `manifest.json`, `CHECKPOINT.md`, and the actual files agree before continuing. Avoid leaving the only record in chat.

Commands consume **real JSON files**; use ordinary project files for intermediate payloads (e.g. `design/redesigns/<id>/inputs/`), so intent is reviewable. Do not write secrets into artifacts or reports.

## Recovery on a fresh session or after compaction

1. Read the repo `AGENTS.md` and the active redesign pointer if one exists. Identify exact initiative ID; do not guess from similarly named directories.
2. Run `resume --project-root ... --initiative-id ...`; read `manifest.json`, `CHECKPOINT.md`, recent journal events, and relevant slice contract. Read the approval entry and its paths.
3. Run `validate` to hash-check the locked sources, screenshots and fixtures; compare current Git HEAD/status and relevant code with recorded provenance. Any mismatch needs inspection. A hash mismatch to a locked reference is a blocker until explicitly reapproved.
4. State the active approved version, completed slices, current ownership, blockers, and **next safe action**. Resume only after unresolved discrepancies are reconciled.
5. Record the rehydration findings as an event/checkpoint, including any new blockers. If a critical earlier decision was never recorded, ask the user again; do not make it up.

## Version and change control

Each approval is a separately identified, hash-locked record in `manifest.json` and `decisions.jsonl`. Changing a file in place does not update its approval. Prototype changes require a new version and explicit user approval; `revoke-approval` preserves history. Keep previous immutable version directories for audit. Image-only reconstruction is a lower-confidence path allowed only after an explicit user decision, never as a silent substitution for the prototype source.

## Git and recovery limitations

Store source paths, actual git HEAD/status, and relevant commit/diff references in checkpoints. This utility does not commit, stash, revert, reset, merge, or deploy the user's code. If Git is unavailable, describe that limitation and read relevant code manually. Files and event history can recover only choices that were persisted before compaction; the skill cannot recover unrecorded chat details.
