---
name: prototype-to-production
description: Redesign an existing SaaS dashboard or business web application, feature, or page from current UI A to an approved visual prototype B with faithful code reuse, exact source/screenshot approval locks, unambiguous vertical-slice agent handoffs, browser visual verification, and durable project-local memory across Codex CLI sessions or context compaction. Use for text- or image-directed UI/UX redesigns, prototype approvals, visual drift, agent implementation handoffs, and recovery of in-progress redesigns.
---

# Prototype to Production

Turn a creative, runnable frontend prototype into an exact, traceable implementation contract. Be creative **before approval** and faithful **after approval**. Never infer layout, component size, alignment, copy, or interaction behavior that approved evidence does not define; stop to obtain approval for missing visual patterns.

## Start here every time

1. Read `references/workflow.md` and `references/continuity.md`. If dispatching/receiving a slice, also read `references/slice-contract.md`. Before visual acceptance, read `references/visual-qa.md`.
2. Identify the real repository root, initiative ID, authorized scope (`feature`, `page`, `app`) and user style direction or reference images. Inspect existing application code and Git status before changing it.
3. If an initiative exists, run `python <skill-root>/scripts/project_state.py resume --project-root <repo> --initiative-id <id>` and `validate` with the same flags; read its `manifest.json`, journal, checkpoint, approved source/screenshots, and relevant slice. On missing/stale evidence, report **BLOCKED** and reconcile; do not start from remembered chat context.
4. If beginning a new initiative and authorized to write to the repository, run `init --scope <feature|page|app>`; add the minimal `assets/project/AGENTS-snippet.md` pointer only with permission, preserving existing AGENTS.md instructions. Store app evidence in `design/redesigns/<id>/`, never in this globally installed skill.

## Stage router

| Stage | Actions and hard gate |
| --- | --- |
| Discover | Map app A routes, states, business behaviors and dependencies before sketching. Record evidence. |
| Prototype | Build and render actual design B frontend code from words/images; capture source, assets, fixtures and state/viewport screenshots. A transient preview alone is insufficient. |
| Approve | User explicitly approves specific B version and coverage. Freeze under versioned project paths; run `approve --approval-file <json>` to lock SHA-256. If source is unavailable, obtain a separate explicit image-only exception. |
| Specify | Extract exact tokens and patterns; map A behaviors to B. Run `slice-create --contract-file <json>` for self-contained vertical slices with screenshots, source paths, file ownership, dependencies, acceptance checks. Unspecified visual patterns remain blocked. |
| Implement | Prefer prototype code reuse, preserve all app functionality, respect isolated file ownership and user modifications. Workers use `slice-report` for slice-local evidence; coordinator uses `reconcile --action report`. |
| Verify | Run real app against B at matching viewport/data/state; capture visible screenshots, inspect alignment/spacing/overlap/copy and affected behaviors. Coordinator uses `reconcile --action verify --review-file <json>` then `reconcile --action accept`. Missing evidence is **BLOCKED**. |
| Integrate | Check accepted slices, live Git/code state, shared design patterns, and user review. No implicit commits, merges, deployment, or agent spawning. |

After **every material** choice, artifact change, stage transition, blocker or completed task, record a typed `event` and brief `checkpoint` using `scripts/project_state.py`, so compaction or a different Codex agent cannot erase progress. The manifest, append-only journal, source hashes, and actual code outrank chat context.

## Quality bar

Simple, ergonomic, precise, friendly, truthful SaaS UI: compact information density, consistent alignment, purposeful whitespace, readable concise copy, no clipped/overlapping elements or misleading controls. The **approved prototype**, not these generic preferences, controls final sizes and layout. Missing source, failed screenshot comparison, incomplete behavior coverage, stale hashes, self-review, or unavailable browser tooling never counts as acceptance.

## Included resources

- `scripts/project_state.py --help`: durable CLI state, approval, slices, report, verification, recovery and validation.
- `assets/project/manifest.template.json`, `slice.template.json`: project-local JSON contract examples.
- `assets/project/redesign.visual.spec.ts`: intentionally incomplete Playwright scaffold, to adapt to the app's existing browser tooling; requires actual fixture/state setup.
- `references/workflow.md`, `continuity.md`, `slice-contract.md`, `visual-qa.md`: detailed operational rules and gates.
