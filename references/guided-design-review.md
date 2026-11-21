# Terminal-first guided design review

Use this conversation for discovery, the roadmap, blueprint review and each implementation slice. Lead with a short explanation in the terminal. Give the user enough information to understand and correct the design there. Link the detailed documents, diagrams and [living review viewer](living-review.md) for deeper inspection. Give the exact document paths, version and heading or viewer section label. If the browser is unavailable, explain the current design in the terminal and provide the local paths. Applicable visual approval checks still apply.

Explain what you understood, illustrate the proposal, discuss one consequential issue, incorporate corrections, then recap the agreed direction. Label observed facts, proposals, user decisions and open questions. Define unfamiliar terms and keep details proportionate to the decision. Use narrow plain-text flows, layout sketches, short tables or worked examples that fit a terminal. Keep larger Mermaid diagrams and annotated screenshots with the supporting documents.

The checkpoints below apply to blueprint initiatives. Use `init --guided-design-review` for new initiatives to enforce checkpoint evidence at discovery, roadmap and blueprint approval. Use the same conversation when resuming unfinished reviews in earlier initiatives. On the full-prototype route, use the terminal presentation for discovery, prototype review and slice handoffs, with the [prototype route's evidence and approvals](workflow.md).

## Discovery and roadmap

Complete the separate [Initial Understanding and Alignment Checkpoint](initial-alignment.md#initial-understanding-and-alignment-checkpoint) first. Discovery and roadmap review follow the confirmed initial understanding.

- **Discovery.** Show the audit that follows the confirmed understanding. Focus on the evidence for the observed baseline, what the proposed scope preserves and any remaining discovery decisions. Give the user a turn to correct the audit before freezing it. Revisit initial alignment if those corrections change the agreed understanding of the request.
- **Roadmap.** Present each proposed workstream by name, its purpose and boundary, dependencies, the order of work and the reason for that order. A short flow such as `Discovery audit -> API contract -> UI integration` can carry the structure. Invite feedback before freezing the roadmap with `plan-record`. Apply corrections and summarize the resulting scope. Save a draft while discussion is open.

Use the recorded decisions to revise the affected specifications. A brief confirmation can close a discovery or roadmap checkpoint. The separate explicit navigation rule below applies to blueprint workstreams.

## Blueprint walkthrough

Start with an overview and keep the detail at the current workstream. A short terminal turn can look like this.

```text
Blueprint spec-v2
1. Item listing  [items]
2. Permissions   [membership]

Reviewing 1 of 2 | Item listing
Members can already list their own items. The route will call a shared
service and keep the same response. The membership check runs first.

Request -> Membership check -> Item service -> Same item list
                           \-> 403 for nonmembers

Detail: <initiative>/blueprints/spec-v2/items.md, heading "items"
Viewer: <local review URL>, section "Item listing"

Tell me what needs correcting. Say Continue when you're ready to
review Permissions.
```

1. Open with a short inventory of **all** roadmap workstreams in review order. Map each to the blueprint feature IDs it covers and flag shared boundaries. Link the complete blueprint and viewer so the user can browse them throughout. Present only the first workstream in detail in this turn.
2. For the current workstream, explain the requirements in plain language, relevant existing behavior and source evidence, and which behaviors are added, preserved, modified, moved or removed. Show how the design works with a small terminal illustration or example. Describe the relevant components, interactions, architecture or API and how implementation fits into the larger application. Give the exact detail paths, the viewer URL and section label, and invite corrections. Discuss one consequential question at a time.
3. Resolve questions, update affected specifications and save consequential decisions as A+ exchanges. Recap the agreed understanding and any remaining limitation. Then explicitly invite the user to say **Continue** when ready and stop at the current workstream. `Correct`, `Looks good` and `I understand` acknowledge the discussion and leave the cursor waiting. `Continue`, `Next`, `Move to the next workstream` or another unambiguous instruction moves the review forward once the current questions and corrections are resolved. If a reply combines a correction and a continuation, reconcile the correction first. When that changes the presented design, recap the revision and wait for continuation on that revision.
4. Honor an explicit request to revisit an earlier workstream or skip a named walkthrough. Record the actual request and scope. On a revisit, retain past decisions and the position to return to. Apply corrections through linked A+ exchanges, then reopen reviews whose presented substance changed. Mark a skipped scope as explicitly skipped and include it in the final consistency check. “Skip the permissions walkthrough and move on” also authorizes navigation when Permissions is the current workstream. “Skip the permissions walkthrough” waives the walkthrough and leaves navigation pending. If the user waives a later walkthrough, record the waiver, keep the current checkpoint open and apply the waiver when reaching the named workstream. If a revisit interrupts a workstream, record the request against that checkpoint and reopen the interrupted exchange at its saved position when returning. Keep earlier work and its evidence.
5. After continuation from the final workstream, compare reviewed and explicitly skipped scopes with the complete feature inventory. Check the final blueprint against the recorded corrections, shared dependencies, conflicting behavior and interface definitions, and coverage. Reopen the affected workstreams for any unresolved material issue. Save the outcome and reference it in the approval presentation. In the terminal, explain the exact version and what formal approval covers, the preservation boundaries, any remaining limits or open decisions, and the next stage. Use a separate turn and the [blueprint approval contract](blueprint-workflow.md#approval-implementation-and-verification) for the formal approval request.

## Save and check the conversation

- Save a short, versioned Markdown excerpt of each **actual terminal presentation** under the initiative directory. Include the explanation, illustration or example, exact document or section references, and recap as displayed. References to a document or a completion checkbox alone are insufficient. Save updated presentations to new paths. Retain only the structured exchanges and excerpts needed to substantiate the review. Full transcript preservation still requires explicit opt-in.
- Use `conversation-record` with `record_type: review_checkpoint` for discovery, roadmap and workstream pacing. Use `status: open`, the exact displayed prompt in `question`, the presentation in `evidence_paths`, and `review_step` of `discovery`, `roadmap` or `workstream` in phase `discover`, `plan` or `blueprint` respectively. Set `review_version` to the planned audit ID or roadmap version, or the recorded blueprint version. Use `review_target` for the visible scope. For a workstream, include its title and position, and set `review_workstream` to the exact entry in the roadmap's `workstreams` list and `feature_ids` to the blueprint features covered. For discovery and roadmap, record the affected IDs or give a `scope_reason`.
- Resolve the checkpoint with a new `status: decided` record with the same checkpoint fields, question and scope, `responds_to` set to the open exchange ID, the actual reply in `response`, and `review_action`. Use `confirm`, `revise` or `skip` for discovery or roadmap, and `continue`, `revisit`, `revise` or `skip` for a workstream. Record design corrections in separate decision exchanges. If the user acknowledges the workstream, save a `recap` exchange when it adds evidence of understanding and keep the checkpoint open. If a revision changes what was displayed, resolve the old checkpoint as `revise` using the actual request and present a new checkpoint after updating the design. For a workstream waiver without navigation, save the waiver as a recap and keep or open a pacing checkpoint until the user explicitly continues. Use `skip` to complete a workstream when the user explicitly waives it and authorizes navigation. When there was no prior prompt, record the waiver without `responds_to`. Use the actual displayed scope recap in `question`, with the waiver and scope in the Markdown evidence.
- Before yielding, run `work-update` with the current stage, `waiting_user` and the open exchange's ID in `pending_question_id`. Use `summary`, `completed_steps`, `pending_steps` and `next_action` to save the ordered positions, current revision, pending discussion and next action. Link versioned presentations in `artifact_paths`. Resolve each material question before opening the pacing checkpoint when possible. If a new question comes up while a pacing checkpoint is open, keep the existing `pending_question_id` and note the additional exchange ID in the cursor summary. The resume packet lists both in `pending_questions`. Resolve the material question before advancing. On resume, inspect the pending exchanges or recorded replies, current position and unfinished action. An acknowledgment, a fresh session or context compaction does not authorize advancement.
- If interrupted between recording the exchange and updating the cursor, use the saved open checkpoint, presentation and roadmap order to restore `waiting_user` at that position. Reuse the exact displayed prompt. If its answer was already recorded, apply the recorded action and update the cursor before proceeding.
- Before freezing the roadmap, requesting blueprint approval or presenting the slice package, inspect the saved excerpts and linked actual replies, compare the corrections with the current artifacts, and resolve gaps. In the blueprint consistency report, cite each workstream's presentation and reply or waiver, affected feature IDs, and the current document headings where consequential corrections are reflected. Record the shared boundaries checked and the outcome. Set `workstream_consistency_path` in the approval input to this Markdown file. Include the report and decision recap in `shown_paths`. If historic presentation evidence is missing for unfinished work, resume at the first unverified or unwaived point. Preserve existing approvals and legacy locks.

This cursor captures the pause at the second workstream.

```text
summary              spec-v2 | 2 of 3 | Permissions
completed_steps      1. Navigation
pending_steps        2. Permissions, 3. Error recovery, consistency check
pending_question_id  permissions-next
next_action          Wait for Continue before presenting Error recovery
```
