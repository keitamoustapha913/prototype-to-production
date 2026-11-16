---
name: prototype-to-production
description: Use when redesigning the UI of any existing web app, dashboard, or business interface from a user brief or visual references; when reviewing or approving prototypes, copying an approved design into the real app, preventing visual drift or ambiguous implementation handoffs, or recovering redesign progress after Codex context compaction.
---

# Prototype to Production

Turn a runnable frontend prototype into a precise, recoverable implementation contract. Be creative **before approval**, faithful **after approval**. Unspecified approved layout or interaction details are blockers, not creative freedom for the implementer.

## Short requests are complete requests

A user can simply say **“Redesign the billing page using these reference images”**, **“Prototype this app with a bold editorial look”**, **“Review prototype v1”**, or **“Implement the approved prototype.”** Do not make them write an initiative ID, stage list, source filename, validation checklist, or an 80-line prompt. Infer the real app, page/feature scope and stable initiative ID by inspecting the open repository and conversation/artifacts. Ask one focused question only when the target or design direction cannot safely be identified.

**Design direction comes from the current user and this initiative's references**, never from the skill, a previous app, or an earlier test case. Any visual style is eligible: dense or spacious, minimalist or expressive, formal or playful. If a style is not specified and cannot be inferred from supplied references or a request to retain the existing style, ask a focused question. Record the agreed direction in initiative-local decisions and prototype evidence; do not carry another project's aesthetic forward.

Default route for a new redesign: **discover A → build separate runnable B → verify visually → present a short review → STOP for explicit approval**. “Review” means inspect evidence and request revisions or user approval, never approve on the user's behalf. “Implement” means resume/validate the exact prior approval, specify slices, implement and verify without restyling. Auto-resume existing initiatives before acting; never restart based solely on chat memory. See `references/workflow.md`.

## Start here every time

1. Read `references/workflow.md`, `references/continuity.md`, and, before prototype capture or acceptance, `references/visual-qa.md`. Read `references/slice-contract.md` when preparing/receiving slices.
2. Inspect the real repository, source UI A, Git state and existing instructions. Locate the requested scope and style from the user's short words/images; clarify only consequential ambiguity.
3. For existing work, run `python <skill-root>/scripts/project_state.py resume --project-root <repo> --initiative-id <id>` and `validate`; inspect actual code, manifest, journal, checkpoint, prototype source, screenshot hashes, **recorded blockers and side-by-side warning galleries**. Resolve discrepancies **before** work.
4. For new work, run `init --scope <feature|page|app>` and use the **directory it prints**. In Git worktrees, the script places new artifacts under ignored `.artifacts/prototype-to-production/<id>/` and preserves older initiative locations. All generated plans, screenshots, browser results, fixtures, inputs and handoffs belong inside the initiative directory. Do not write project evidence into the installed skill or silently change tracked `AGENTS.md`/`.gitignore`.

## Stage router

| Stage | Actions and hard gate |
| --- | --- |
| Discover | Map app A routes, states, business behaviors and dependencies before sketching. Record evidence. |
| Prototype | Build runnable B from the actual code/data. Inspect mobile, tablet, desktop, long copy and roles. Save reproducible fixtures, replay source, actual screenshots and browser results; **exclude QA-only controls from product reference captures**. |
| Approve | Require explicit user approval of B version/cases. For source mode, `approve --approval-file <json>` now requires two clean-context replay runs and QA-free screenshot evidence, all SHA-locked. Image-only requires a separately approved exception. |
| Specify | Extract exact tokens and patterns; map A behaviors to B. Run `slice-create --contract-file <json>` for self-contained vertical slices with screenshots, source paths, file ownership, dependencies, acceptance checks. Unspecified visual patterns remain blocked. |
| Implement | Prefer prototype code reuse, preserve all app functionality, respect isolated file ownership and user modifications. Workers use `slice-report` for slice-local evidence; coordinator uses `reconcile --action report`. |
| Verify | Match **exact approved B cases** to the same implemented route/fixture/state; check unchanged A pages only against their own A baselines. Diagnose mismatches and retry three times in clean contexts. Record real defects through `reconcile --action block` so the official task status and checkpoint agree. Approved B-case capture warnings receive an automatic SHA-locked **side-by-side HTML comparison gallery**; record other material warnings, including ancillary A-before/A-after warnings, with `warning-record`. Real UI defects/missing evidence are **BLOCKED**. Narrow B-case capture warnings may be provisional, never PASS. See `references/visual-qa.md`. |
| Integrate | Check accepted slices, live Git/code state, shared patterns, and user review. Pending warning galleries require explicit `warning-decide` before slice acceptance; a provisional B-case warning also needs `accept-warning`. Confirm evidence hashes with `validate`. No implicit commits, merges, deployment, or agent spawning. |

After **every material** choice, revision, blocker, or completed task, record `event` and `checkpoint` using `scripts/project_state.py`. Project files, hashes and code outrank chat context. Ignored files persist across sessions **in the same workspace**, not automatically across clones, machines or isolated worktrees: arrange a verified external handoff of the whole initiative when workers cannot share the directory; otherwise report **BLOCKED**.

## Quality bar

Evaluate the UI against **the user's chosen aesthetic**, applicable accessibility/usability requirements, and the real application's behavior. Check alignment, readability, control reachability, truthful content, focus states, responsiveness, and absence of accidental overlap/clipping. Do not impose a default palette, density, card size, typography, copy tone, or amount of whitespace. Before approval, test 320/390/768/1024/1440px (or documented equivalent) and supported long-text locales; inspect real pixels. The **approved prototype**, not generic advice, controls final dimensions and visual character. Unreplayable fixtures, screenshots containing prototype QA controls, missing source or browser, incomplete functional coverage, stale hashes or self-review never count as acceptance.

## Included resources

- `scripts/project_state.py --help`: durable CLI state, approval, slices, report, verification, recovery and validation.
- `assets/project/manifest.template.json`, `approval.template.json`, `replay.template.json`, `slice.template.json`: project-local JSON contract examples.
- `assets/project/redesign.visual.spec.ts`: intentionally incomplete Playwright scaffold, to adapt to the app's existing browser tooling; requires actual fixture/state setup.
- `references/workflow.md`, `continuity.md`, `slice-contract.md`, `visual-qa.md`: detailed operational rules and gates.
