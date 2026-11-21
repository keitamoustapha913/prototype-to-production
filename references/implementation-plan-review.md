# Complete Conversational Implementation-Plan Review

Complete this review after [Initial Understanding and Alignment](initial-alignment.md) and before drafting any detailed blueprint. The discussion settles direction, scope, preservation constraints and work organization. Blueprint approval, slice review, package locking and authorization to start each require their own checkpoints.

## Present the whole plan

1. Revisit the confirmed requirements and audit. Assign each source requirement and acceptance scenario its own stable ID and source reference in the confirmed map. Carry each ID and its text into the audit. In the plan input, map every audited requirement ID to one or more named workstreams with `requirement_workstreams`. Group related items in the conversation and retain each row in the map.
2. Show an **Implementation Plan — Overview** in the terminal and label it **Proposed**. Recap the confirmed requirements, then show all workstreams in discussion order with each objective, scope, preservation constraints, requirement coverage, dependencies and intended outcome. Explain the order and link the complete roadmap by its exact path. Show a compact text flow when workstreams share a dependency.
3. Invite feedback and save the actual overview and prompt as a `roadmap` review checkpoint in phase `plan`. Record the reply before saving that revision with `plan-record`. Give a revised overview a new version and presentation. The CLI freezes the roadmap revision and generates `plans/<plan-version>/requirements.md` with every requirement and its workstreams. Give the user the path to that map and the source alignment map.

## Discuss one workstream at a time

Present the current workstream with its position and exact roadmap heading or document path. Cover each of these points in plain language:

- Why it is needed and which original requirements and acceptance scenarios it addresses.
- What exists today, with the paths and observations that support the findings.
- Which features, files, behavior or architecture the change affects, and which existing behavior and decisions the plan preserves.
- How the proposed behavior works, using a narrow text flow, simple table or worked example.
- Which workstreams or shared components depend on it and what it needs from them.
- What is still uncertain and which decisions the user needs to make.
- Which observable outcomes, tests or evidence will establish completion.

For example, a visual QA discussion can show:

```text
Reviewing 2 of 3 | Visual QA | Proposed
Open the approved screen in a real browser -> Capture the same state
-> Compare with the approved design -> Repair verified defects in scope
-> Recheck and save the before/after evidence

The approved design and screen states come from Blueprint Review.
Completion requires a passing recheck or a recorded blocker.
```

Answer questions and investigate evidence on request. Record the discussion and corrections, update the affected plan and recap the result. Invite **Continue** when the current questions are resolved and stop. Acknowledgments leave the current checkpoint open. Accept **Continue**, **Next** or **Move to the next workstream** on their own. A response that requests a change and continuation calls for resolving the change and presenting the revised direction first. Review every workstream, including a single-workstream plan and the last workstream. A request to skip leaves the review waiting.

Use a new plan version when the roadmap changes. Close the superseded checkpoint with the actual revision request, preserve prior evidence, and show the revised overview and workstreams in the new version. Cite earlier explanations when they still apply and call out the differences. If a correction changes the confirmed understanding before the first blueprint, return to initial alignment and save an audit tied to the new confirmation. For later amendments, use the existing blueprint change-control process.

Close an open plan review checkpoint as `revise` before saving a replacement discovery audit. Record a material planning correction in phase `plan` even if the user has already authorized the blueprint handoff. Review the revised plan and ask for fresh permission to draft the blueprint.

## Verify the plan and wait for the blueprint instruction

After the last **Continue**, check that all workstreams were reviewed, every requirement and acceptance scenario maps to a workstream that actually addresses it, dependencies and build order agree, shared boundaries and preservation rules are consistent, and the latest applicable user decisions appear in the plan. Resolve discrepancies and review a new revision if the plan changes.

Save the results with the cited requirement map, reviewed presentations and applicable decisions. In the terminal, summarize the counts of mapped requirements and reviewed workstreams, explain how any gaps or conflicts were resolved, and give the full map and verification paths. Show the precise plan version and ask the [scoped plan-handoff question](user-confirmation.md). A clear affirmative reply to that question authorizes blueprint drafting. Earlier initiatives require **Continue to blueprint**. A standalone **Continue** closes a workstream checkpoint. Begin blueprint drafting after recording the decision on the completed plan.

## Record progress and recover the review

Use `conversation-record --exchange-file <json>` and [the conversation template](../assets/project/conversation.template.json). Save actual terminal excerpts in versioned Markdown. Retain the displayed explanation, illustration, document references, user questions and replies, corrections and recap. Keep the structured exchanges linked to those files. See [transcript export guidance](blueprint-workflow.md#evidence-led-conversation) for preserving raw session transcripts.

- For pacing, use `record_type: review_checkpoint`, `phase: plan`, `status: open`, the exact `question`, a visible `review_target` such as “2 of 3 | Visual QA”, the plan version as `review_version`, and `evidence_paths` for the actual Markdown presentation. Use `review_step: roadmap` for the overview, `plan_workstream` for each workstream and `plan_completion` for the final coverage and consistency presentation.
- On a workstream checkpoint set `review_workstream` to its exact name in the plan and include all requirement IDs mapped to it. On the completion checkpoint include every mapped requirement ID. Use affected `feature_ids` or a concrete `scope_reason` when the checkpoint has no requirement IDs.
- Record the actual reply in a new `status: decided` record with the same checkpoint identity, question and scope and `responds_to` set to its open exchange ID. Use `review_action: confirm` for the roadmap overview, `continue` for explicit navigation or the blueprint handoff, and `revise` for a revision request. Keep discussions and acknowledgments in the saved excerpts or additional exchanges while the pacing checkpoint stays open. Capture material design decisions as separate linked decision records. Resolve them before recording a continuation.
- Save `work-update` at each pause, keeping the review position, version, discussion paths and open exchange ID with `status: waiting_user`. On `resume --format json`, read `plan_review` with `pending_questions`, `active_question_answer` and the cursor. Use the journaled position or exact reply if the cursor update was interrupted. A fresh session resumes the same workstream or final checkpoint.

The CLI requires this sequence for every new blueprint, including one based on an unfinished legacy roadmap. Use the current frozen roadmap if it has a complete requirement mapping. If the mapping is missing, save a revised roadmap with all requirement IDs before starting the workstream review. For a legacy roadmap saved without a generated map, publish the mapping in Markdown and link it in the review evidence. Existing frozen blueprints and approvals remain available in the initiative history.
