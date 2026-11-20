---
name: prototype-to-production
description: Use for greenfield apps, significant redesigns, and implementation across multiple features, user journeys, shared frontend state, or backend contracts and architecture. Explicit invocation also supports small tasks. Resume saved initiatives and obtain blueprint approval before implementation. Use the full runnable-prototype route when requested. Isolated, well-specified low-risk fixes use the ordinary workflow unless explicitly invoked.
---

# Prototype to Production

Turn an idea into an illustrated, approved implementation contract, then deliver small verified slices. New initiatives use **blueprint-first**; a complete runnable clone is optional. Existing initiatives without a mode retain their original **prototype** semantics and approval locks.

## Start and route

1. Inspect repository instructions, Git state, relevant source, tests and existing initiative evidence. First try `python3 <skill-root>/scripts/project_state.py resume --project-root <repo> --format json`; select an ID only if ambiguous. Resume the exact saved task and pending question. Run `validate` before continuing; reconcile changed/missing evidence instead of restarting.
2. Read [references/blueprint-workflow.md](references/blueprint-workflow.md) for the default route, including backend, greenfield, conversation and amendment contracts. Read [references/continuity.md](references/continuity.md) for checkpoints, interruptions and transfer.
3. Initialize new work with `init --project-root <repo> --initiative-id <id> --scope <feature|page|app>`. The default mode is `blueprint`. Choose `--mode prototype` only for the explicit full runnable-B route, then read [references/workflow.md](references/workflow.md), [references/functionality-discovery.md](references/functionality-discovery.md), and [references/visual-qa.md](references/visual-qa.md). Never change an existing initiative's mode.
4. Use the **directory printed by init/resume**. In any Git repository, generated design documents, plans, diagrams, prototype source, preview assets, screenshots, fixtures, JSON inputs and handoffs belong under ignored `.artifacts/prototype-to-production/<id>/` or the existing ignored initiative path. Template paths are relative to that directory. This rule applies when another workflow suggests tracked `docs/` or `design/`. Confirm each destination with `git ls-files -- <path>` (empty) and `git check-ignore -- <path>` (matched). Only approved implementation goes in tracked app code. The CLI guards every command and refuses tracked artifacts; never untrack or move legacy evidence automatically.

When scope is unclear, name the uncertainty in a brief plan and start with a short blueprint. Expand it if discovery finds shared behavior, roles, migrations, or high-impact data flows.

For an explicit small request, keep the roadmap and feature contract short. Run `discovery-record`, `plan-record`, `blueprint-record`, `blueprint-coverage`, and `validate` before requesting approval. Link the generated blueprint index and coverage when you ask. If a command remains blocked, show the draft and the exact blocker.

## Blueprint-first route

**Investigate → discuss → roadmap → detailed blueprint → prove coverage → explicit version approval → small slices → implement and verify.**

- **Investigate before asking.** Discover in-scope behavior from actual source and browser/API/test evidence. Explain findings in simple terms. For existing A, inventory stable feature IDs, hidden controls, roles, states and backend effects, with a final full zero-new sweep. For greenfield, verify an empty A baseline and derive requirement IDs from recorded user intent. Use `discovery-capture` for named browser states and roles. Backend-only work uses source and API evidence.
- **Discuss one consequential question at a time.** Separate **Observed in the app**, **Codex proposal**, **Your decision**, and **Still open**. Define unfamiliar terms. Show a compact Mermaid flow, anatomy outline, state table or illustrated alternatives when useful; explain tradeoffs and recommend grounded options. Settle minor internal implementation details as labeled builder choices. Respect explicit task opt-out and scale detail to complexity.
- **Record exact consequential exchanges immediately.** `conversation-record` preserves the displayed question/proposal, options, recommendation, tradeoffs, illustrations and actual response. Answers and corrections are new linked records; earlier questions, rejected options and decisions remain unchanged. Give short decision recaps after discovery, before approval and after amendments. Preserve the full transcript only on explicit request through a supported mechanism; disclose unavailable/partial capture. Never fabricate missing exchanges.
- **Plan, then blueprint.** `plan-record` saves a concise roadmap grounded in the conversation. `blueprint-record` saves a navigable versioned Markdown/Mermaid specification with exact feature anatomy, behavior/state/API contracts, UI tokens where relevant, source anchors and acceptance IDs. A plan is not permission to implement. Request explicit permission before a temporary isolated runnable experiment; treat its output as disposable evidence.
- **Prove coverage and wait.** `discovery-record` freezes A or the empty baseline and records user requirements for new work. `blueprint-coverage` maps each existing feature and requirement to the blueprint and acceptance checks. KEEP/MOVE preserve behavior, MODIFY/REMOVE require linked user authorization, and ADD traces new features to recorded requirements. Missing material choices remain BLOCKED. Show the exact version, coverage and saved decision recap, then wait for explicit approval. Record what the user saw and any approved exceptions in the blueprint approval.
- **Deliver exact small contracts.** After approval, read [references/slice-contract.md](references/slice-contract.md). `slice-create` binds approved feature contracts, decisions, baseline anchors and hashes to exclusive paths, dependencies and a genuine test plan. Implement user-meaningful vertical slices; new material choices require an amendment and fresh approval.
- **Verify actual results.** Blueprint reconciliation runs approved check commands and captures UI render evidence where applicable. Use `reconcile --action block` or `unblock` for defects. Final acceptance requires independent review, successful execution and unchanged source and evidence hashes.

## Preserved full-prototype route

For `prototype` initiatives: discover and freeze A → build runnable B → exercise every A feature → `prototype-coverage` → two clean-context replays with QA controls excluded → explicit exact-version source/image-only approval → slices → approved-B visual/functional verification. The original screenshot, replay, warning-gallery and user-warning-acceptance gates still apply. Blueprint coverage cannot substitute for runnable A-to-B parity in this mode.

Design direction comes from the current user and this initiative's references. Before approval, propose values clearly; after approval, preserve exact choices. No aesthetic from another app is a default.

## Continuity and gaps

Run `work-update` before substantial work and after each meaningful step, test, blocker or decision, including before context compaction when possible. Save phase, task ID, completed/pending steps, code/evidence paths, next action and blockers; for a user question, save `pending_question_id` and `waiting_user`. On resume, restore the exact question/answer and unfinished task, not a fresh questionnaire.

If functionality was missed after approval, run `missing-feature`, re-audit the affected scope until one full zero-new sweep, and preserve prior decisions. Real gaps require a new blueprint or prototype version with full mode-specific coverage and fresh explicit user approval. False reports need an explicit evidenced user correction. Do not silently dismiss reports or revise locked artifacts.

Ignored evidence survives sessions in the same workspace, not Git clones or independent worktrees. Transfer/export the entire initiative through an authorized channel and validate hashes before cross-workspace work. Hashes detect change; they are not backups. No implicit commit, merge, deployment, installation or agent spawning.

## Resources

- `scripts/project_state.py --help`: modes, records, recovery, approval, slices and validation.
- `assets/project/*template.json`: intentionally unapproved examples; replace placeholders with real evidence.
- `assets/project/redesign.visual.spec.ts`: prototype-only Playwright scaffold; adapt to real fixture/browser tooling.
