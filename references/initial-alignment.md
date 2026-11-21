# Initial Understanding and Alignment

Run this stage for every new Prototype-to-Production initiative, including the full runnable-prototype route, whether the user supplies an idea, an implementation request or a detailed specification. Resume an open intake from its saved position. A previously approved design or saved initiative already past intake continues from its recorded checkpoint.

New initiatives enforce the checkpoint in the CLI by default. For an initiative created before this checkpoint that is still in intake, begin the A+ alignment record before advancing.

## Investigate and map the request

1. Read the entire brief and its referenced material. Extract each distinct goal, requirement, constraint and prior decision. Give each requirement a stable ID and a reference to its source, such as a heading, paragraph, message or attachment.
2. Inspect the relevant architecture, source, tests and observed behavior. Match each requirement to specific findings and evidence. Use source anchors, test results, API observations or browser captures as applicable. For greenfield work, record the inspected repository and the evidence of an empty application baseline. If verification is incomplete, record what you inspected and what remains unknown.
3. Give each requirement its own row with one of these classifications. Describe what the evidence establishes and the proposed direction, including behavior to preserve. Mark proposals and unresolved questions clearly.

| Classification | When to use it | Record value |
| --- | --- | --- |
| Already implemented | Observed behavior satisfies the requirement | `already_implemented` |
| Partially implemented | Observed behavior covers only part of the requirement | `partially_implemented` |
| Missing | The investigation found no relevant implementation | `missing` |
| Conflicting | Existing behavior or another requirement conflicts with the request | `conflicting` |
| Not yet verified | The available evidence leaves the result uncertain | `not_yet_verified` |

## Discuss the findings in Codex CLI

Open with **Understanding Your Request**. Briefly state the requested outcome, important repository findings with source references, the proposed direction and which areas need discussion. Continue one significant area at a time. Explain what exists, what would change and why, with a compact terminal sketch or worked example when useful. Use exact file paths or source anchors so the user can inspect the evidence.

Give the user room to ask questions and correct the interpretation. Ask about consequential uncertainties and conflicts. Carry forward decisions already in the brief. Record exact consequential questions, answers, corrections and decisions as linked A+ exchanges through `conversation-record`. Recap the resolved area and wait for the user's direction to continue before presenting the next area. Preserve versioned excerpts of what you actually showed.

Save the full draft map as Markdown before opening the discussion. Register it as a Markdown section titled **Initial Understanding and Alignment** in `living-review/sections.json` and share the local path and viewer link. Update the viewer section to point to the generated map when you record the confirmation prompt. Use the [living review viewer](living-review.md#register-the-review-material) when available. The terminal discussion should stand on its own.

## Initial Understanding and Alignment Checkpoint

When all significant areas have been discussed, recap the current understanding and explicitly ask the user to confirm it before planning. Save the actual presentation and prompt, then use `conversation-record --exchange-file` with [the initial alignment template](../assets/project/initial-alignment.template.json).

- The open A+ record has `phase: discover`, `record_type: review_checkpoint`, `review_step: initial_alignment` and a fresh `review_version`. Include every mapped ID in `requirement_ids`. The top-level `evidence_paths` hold the actual terminal excerpts. Each `requirement_mappings` row has the request reference, classification, finding, direction and repository-relative `source_paths` or initiative-relative `evidence_paths`. In a new blueprint initiative, use the [complete source inventory](blueprint-workflow.md#complete-requirement-to-blueprint-traceability) for `reviewed_sources`, source item kinds and scenario IDs. The CLI generates and protects `alignment/<review-version>.md` from these rows.
- Save a `waiting_user` work cursor with `pending_question_id` set to the open exchange. Recover the exact prompt and map in `resume --format json`. Record the actual reply before changing the cursor. Close the intake task when the confirmed review is complete, then resume discovery and planning.
- Record a new `status: decided` exchange with the original prompt, checkpoint fields and IDs, `responds_to` set to the open exchange ID, and the exact user reply in `response`. Keep `requirement_mappings`, `reviewed_sources`, `reviewed_source_paths` and `source_line_mappings` on the open presentation. Use the [scoped intake confirmation question](user-confirmation.md) for protocol-enabled initiatives. Use `review_action: confirm` when the user confirms the presented understanding. Earlier initiatives recognize replies such as **Confirm understanding**. If the reply includes a correction, resolve it first. An acknowledgment, a request to skip, a comprehensive brief or a reply to a different checkpoint leaves intake incomplete.
- For a correction, save the consequential exchange and close the presentation with `review_action: revise`. Investigate the affected evidence, update the mapped requirements, and explain what changed before asking again. Preserve stable IDs across versions. In new blueprint initiatives, keep excluded IDs in the source inventory and record their status and authorization in blueprint coverage. In prototype mode and legacy blueprint initiatives, cite removal decisions in the new presentation's `scope_removal_decision_ids`. Open a fresh version with new terminal excerpts and preserve the earlier maps and exchanges. If linked source changed while the prompt was open, use the [A+ source reconciliation fields](blueprint-workflow.md#evidence-led-conversation) on the linked reply.
- Begin implementation planning after confirmation of the current presentation. In blueprint mode, complete [Conversational Implementation-Plan Review](implementation-plan-review.md) before drafting the blueprint. In blueprint mode, carry the confirmed IDs and text into the discovery audit's `requirements` and map them to the applicable named `requirement_workstreams` in the roadmap. Cite any additional material decisions through `decision_ids`. The confirmed map supplies the intent for requirements already stated in the brief. Trace the IDs through blueprint coverage and slice assignments. In prototype mode, use the confirmed IDs in the prototype plan and each slice handoff's `requirement_ids`. The CLI links the handoff to the map bound to the approved prototype's discovery audit. Keep unverified points and user-approved dispositions visible wherever they affect the work.

| Decision | What it establishes |
| --- | --- |
| Initial understanding | Codex correctly understands the request and relevant existing behavior |
| Blueprint approval | The exact detailed solution meets the user's intent |
| Individual slice review | The slice instructions represent the approved blueprint |
| Final package approval | The reviewed slices form a complete and consistent package |
| Implementation authorization | The user authorizes coding on the exact package |
| Final acceptance | The delivered application meets the user's expectations |

Each decision requires its own record. Initial confirmation opens planning. Follow the applicable route's approval and start requirements before changing application code.
