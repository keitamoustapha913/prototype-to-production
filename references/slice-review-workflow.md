# Conversational slice review

Lead with the [terminal-first guided review](guided-design-review.md) and use the [living review viewer](living-review.md) for deeper detail and focused before-and-after comparisons.

Use this workflow after the user approves an exact blueprint in a new blueprint-first initiative. The source of truth is the recorded current user decision and amendment, then the active approved blueprint and its coverage, then the locked package, then the guide and contract, then delivery evidence. Escalate conflicts to the user and preserve the old records.

## From blueprint to implementation

```mermaid
flowchart TD
    A[Approved blueprint] --> B[Draft complete slice map and handoff guides]
    B --> C[Audit coverage, ownership, interfaces and dependencies]
    C --> D{Any material gaps?}
    D -->|Slice boundary or handoff gap| B
    D -->|Blueprint gap| E[Revise and reapprove affected blueprint specifications]
    E --> A
    D -->|Clear| F[Explain the full map and dependency diagram in Codex CLI]
    F --> G{User explicitly confirms this map?}
    G -->|Request corrections| B
    G -->|Confirm breakdown| H[Check the current slice handoff]
    H --> D1{Material gap?}
    D1 -->|Yes| H1[Correct the guide and freeze a new revision]
    H1 --> H
    D1 -->|Clear| R1{Independent review required?}
    R1 -->|Yes| R2[Fresh agent traces the candidate and its references]
    R2 -->|Gap found| H1
    R2 -->|Ready| I[Show readiness and walk through the slice in the terminal]
    R1 -->|Coordinator evidence is sufficient| I
    I -->|Correction| H1
    I -->|Coverage and comprehension verified| I0[Request approval of the exact revision]
    I0 -->|User approves| I1{More slices?}
    I1 -->|Yes| I2[Wait for explicit instruction to continue]
    I2 -->|User continues| H
    I1 -->|All reviewed| J[Run final cross-slice and handoff validation]
    J -->|Guide findings| H
    J -->|Map findings| B
    J -->|Pass| L[Obtain package approval and lock]
    L --> M[Wait for separate coding authorization]
    M -->|User authorizes| N[Release assignments in dependency order]
```

```mermaid
flowchart LR
    R[Source requirement and acceptance criterion IDs] --> B[Exact approved blueprint feature and checks]
    B --> S[Responsible slice and guide recipe]
    S --> E[Executed checks and final acceptance evidence]
```

## Draft the complete inventory

1. Read the approved roadmap, blueprint, baseline audit, coverage and A+ decisions. Group implementation into small vertical slices in dependency order. Assign shared files and contracts to an explicit owner and make their consumers depend on it. Cover every feature/check pair and every audited baseline behavior or requirement. Assign writable paths only within the approved footprint.
2. Copy the [proposal](../assets/project/slice-proposal.template.json) and [guide](../assets/project/implementation-guide.template.md) for each slice. Draft under a distinct subdirectory of the initiative's `inputs/`. Put Mermaid sources in `diagrams/` and worked examples in `examples/`. Link the diagrams and include fenced Mermaid. Use a state diagram for approved state transitions and a sequence diagram for an approved sequence contract. Add the approved feature documents to `artifact_paths` and link to them and the frozen A+ records from the guide's eventual directory. Fill every applicable annex from the blueprint's `surface_contracts`. Carry over reasoned inapplicability explicitly. Name the existing files a fresh agent should read first and describe planned files separately in the implementation footprint. Set `P2P_SKILL_ROOT` to the installed skill directory when running the guide's handoff commands. Put all proposal objects in a JSON object with a `slices` array and run `slice-plan` before opening a review.
3. Give each proposal a `boundary_reason` explaining why the work belongs together. The map takes each observable outcome directly from the approved checks assigned to the slice. Run `slice-plan` with the whole collection. It checks that every approved feature/check pair and audited baseline item is assigned, each acceptance check has one responsible slice, each writable path has one owner, and dependencies and shared interfaces agree. Read `slice-review/register.md` for reported gaps. Correct a decomposition problem in the proposed collection. If the approved blueprint itself leaves a behavior, interface or acceptance outcome unspecified or conflicts with a proposed change, return to blueprint review and obtain fresh approval.

Use `functional_tests` to list the approved acceptance checks assigned to a slice. Assign at least one check for each included feature. You can split a feature's checks between slices when the complete inventory still covers every approved feature and check pair. The guide's test table and delivery verification follow that slice's assignment.

For every direct dependency, add one `dependency_contracts` entry with its `dependency_id` and the kind of prerequisite.

- Use `interface` with the exact `contract_ids` declared by the provider and consumer. List any provider-owned files the slice reads in `paths`.
- Use `files` with exact provider-owned `paths` when the prerequisite supplies files without a named shared contract.
- Use `ordering_only` with a concrete `reason` and frozen `evidence_paths` when the prerequisite only sets delivery order.

Reference files the slice needs from another provider by their repository-relative paths in the architecture, requirements, integration or recipe section. A referenced provider file requires a direct `interface` or `files` dependency even when the provider is already earlier in the delivery order. Record baseline observations under **What exists now**.

All commands take `--project-root` and `--initiative-id`. Input-driven commands also take `--input-file <JSON>`. `slice-register` prints the saved question alongside the next action, slice map, slice details, package references, rejected alternatives, missing coverage and the approved source snapshot digest. `slice-guide-validate` takes `{"slice_id":"slice-example"}` and rechecks the selected guide. Install the pinned Mermaid parser in the skill directory as described in the [README](../README.md#install).

## Present and confirm the complete slice map

1. Run `slice-map` and explain the full breakdown in the terminal before opening an individual slice review. The command saves the initial terminal output for the proposed map. For each slice, explain what it delivers, why it exists, which approved requirements and checks it owns, which existing behavior it preserves, and where it changes source. Draw the dependency arrows in execution order and explain what each provider supplies. Call out overlaps or uncertainties, including ones found by reading the approved design that the CLI cannot infer. Link the generated Markdown, living viewer, and exact guide paths for detail. The viewer opens the linked guides, contracts, and current register from the map.
2. Give the user a turn to ask about scope and propose corrections. For a split, merge, ownership or dependency change, submit the complete revised collection through `slice-plan` with the actual correction and an amendment reason. Reconcile scope, coverage and dependency interfaces and present the revised map. The latest map ID and digest appear in `slice-register` and `slice-map`.
3. Ask the user to confirm the displayed map. Use the [map consent template](../assets/project/slice-map-consent.template.json) to record the exact displayed question with the map ID, path and full digest, plus the actual affirmative answer. The coordinator requires the saved terminal presentation for the exact map and binds it to the consent receipt. For example, ask for `Confirm slice-map-v1` if that is the displayed ID. If the user asks for changes or gives a conditional or unclear reply, continue the map discussion. A confirmed map opens the first pending slice. Map confirmation authorizes detailed review of the breakdown. Individual slice approval, package lock and coding each require their own decisions.
4. When a later revision changes the map, present and confirm its new version before resuming individual reviews. If review was waiting for an instruction to continue, the new map confirmation preserves that checkpoint and updates it to the next slice and revision. Refining a guide without changing the breakdown preserves map confirmation and still requires review of the new guide revision. The map Markdown keeps its original guide links. Use the register for current guide paths.

## Verify adaptive handoff readiness

After map confirmation and before presenting an individual slice, run `slice-guide-validate` for its exact candidate. Read the frozen guide, contract, assets, approved checks, and permitted repository references. Try to reconstruct the change from that material alone and compare the result with the blueprint and source. Keep the check read-only and record observations from inspection. Actual test execution belongs to authorized delivery.

- Trace every assigned requirement and acceptance criterion to a concrete step and observable expected result. Check repository paths and symbols, decisions and existing behavior to preserve, interfaces and dependencies, scope and prohibited changes, applicable states, errors, permissions, and boundary conditions. Open the illustrations and verify that they explain the intended interactions or data. Record an explicit reason for inapplicable details.
- Follow the implementation recipe in order. Flag decisions a new coding session would have to invent. Cite the guide lines and authoritative references that resolve each implementation question. Save failed command output or line-cited findings in a new Markdown or JSON file under the initiative's `inputs/`. Record a material gap with `slice-review-record` and `action: block`, `handoff_failure: true`, exact slice and revision, explanation, and that evidence path. To resolve a blocker that needs user input before readiness, record a focused `conversation-record` question with `handoff_blocker: true` and the saved blocker evidence path. Fix the affected specifications before repeating readiness. Use the blueprint amendment process for changes to approved behavior.
- Assess complexity and risk. Pass `assessment` to `slice-guide-validate` to see the computed review depth. Set `complexity` to `simple`, `moderate`, or `complex`. Give a source-based `rationale` and include each applicable `risk_factors` value from `architecture`, `security`, `dependencies`, and `ambiguity`. Use `ambiguity` when inspection leaves substantial uncertainty or low confidence. Record actual user requests for independent review using `action: require-independent` and `user_response`. Both a recorded handoff failure and an explicit request carry forward through corrections.
- For a simple, isolated change, use the baseline checks and the coordinator's source-grounded walkthrough. For moderate complexity, add `assessment.specification_analysis` describing the interfaces and specifications compared, the edge cases examined and where each decision is established. Require independent review for complex work, any listed material risk, multiple shared dependencies or interfaces, a previous handoff failure, or the user's request. Use the same completeness standard at every depth.
- After fixing any initial gaps, give an independent reviewer a fresh session with only the frozen candidate, its approved references and the relevant read-only repository source. Ask the reviewer to reconstruct the files, symbols, steps, states, dependency interfaces and verification checks, then cite missing or contradictory instructions. Require their own line-cited findings and walkthrough. Keep the gate closed if independent review is required and unavailable. Resolve material findings and have the reviewer check the corrected revision.

Record the passing result with `slice-review-record` using the [slice review input](../assets/project/slice-review-decision.template.json). Use `action: readiness`, the completed `assessment` and `handoff_review`. Bind the exact revision and hashes. Each finding in the passing receipt must be `PASS`. For each applicable guide section, cite the lines examined, explain what the reviewer traced and how the approved behavior or reasoned inapplicability was established. If `risk_factors` includes `ambiguity`, record how the independent reviewer resolved it in `assessment.ambiguity_resolution`. The readiness summary displays that resolution and the assessment rationale. Include the approved repository source anchors in `source_paths` in both records. Keep the content specific enough to recover the change without the conversation.

Attach a walkthrough JSON record. Use `record_type: independent_no_code_walkthrough`, `context: fresh_session` and `independent: true` in the receipt's walkthrough for independent review. For coordinator review, use `record_type: coordinator_no_code_walkthrough`, `context: coordinator` and `independent: false`. The attachment carries the same reviewer and context, `test_execution: false`, approved `source_paths` and `cases`, one per scenario and feature/check reference. Each case keeps `scenario`, `feature_id`, `check_id`, `expected`, `observed`, `observation_basis` and `references`. Use `<feature>:<state>` for states, `<feature>:check:<check-id>` for acceptance checks and `<feature>:render:<case-id>` for approved render cases. Cite exact Markdown headings or repository symbols. An observation here describes the design traced during the walkthrough. In the guide's test table, record future tests as planned and cite already observed tests only with passing coordinator verification from authorized delivery and its current source and runner fingerprints.

## Review one slice at a time

### Present the Guided Slice Walkthrough

Show the saved readiness summary from `resume` or the register. Start with an illustrated, standalone explanation of the purpose, current behavior, proposed behavior, dependencies and verification. Explain technical terms when introducing them. Use a concrete example with approved values and show a compact terminal flow, state table, component sketch or layout comparison. For a straightforward slice, cover the whole scope in one walkthrough. For complex work, begin with the same essential picture and group the follow-up into natural parts, such as a user action and its underlying data flow. Let the user choose where to go deeper. Emphasize behavior and consequences in the conversation and use the coverage table when saving evidence.

| Topic key | What the user needs to understand |
| --- | --- |
| `purpose` | What the slice delivers, who benefits and why |
| `existing_functionality` | What the application does today, grounded in the repository or approved baseline |
| `proposed_changes` | What will be added, preserved, modified or removed, with the expected result |
| `illustrations` | A readable terminal example of the resulting interface, architecture or workflow |
| `technical_implementation` | The components, meaningful source files and symbols, API or schema changes and how the pieces interact |
| `dependencies` | What other slices supply or consume, or why the slice can stand on its own |
| `verification` | How the assigned checks will demonstrate completion, including material failure cases and observable outcomes |
| `open_decisions` | What needs a decision and what has been settled, including explicit confirmation when there are no material open decisions |

Link the exact Markdown guide, contract and focused section of the living viewer for deeper inspection. The viewer provides the technical specifications, exact source references, diagrams, schemas, dependencies, detailed decisions and acceptance tests. Show the essential information in the terminal even when the viewer is unavailable.

### Save progress and resolve changes

1. Save a versioned UTF-8 excerpt of what was actually shown, beginning with the exact current readiness summary. Include the explanation and terminal illustration in the body. Use `slice-review-record` with `action: presentation` and the [walkthrough input](../assets/project/slice-walkthrough.template.json). Set the `position` to where the discussion should resume. For each covered topic, cite a distinct, verbatim excerpt from the explanation. Save a cumulative excerpt and updated topic mapping after each material section. Use new evidence paths when the presented text changes. Read `walkthroughs` in the register or `resume --format json` to recover the position and remaining topics.
2. Invite questions, repository investigation, corrections and more illustrations. Record consequential exchanges through `conversation-record` with the exact question and answer, rejected options, illustrations, source basis and affected IDs. Use phase `slice_review`, the current `slice_id` and `proposal_revision`. Record material open questions as open A+ exchanges and link the answers as separate records.
3. If an answer changes the approved blueprint, obtain a new blueprint approval. For a clarification of approved behavior, use `record_type: slice_detail` and unchanged `clarifies_decision_ids`. Link corrections with `supersedes`, keeping the same answer link and scope. For an earlier slice, record `action: reopen` with the exact reopening request. Update traceability, the guide, contract and illustrations, then freeze a new revision with `slice-draft`. The viewer refreshes generated guides and illustrations automatically. For custom catalog sections, update the source under the same section ID. Run `review-revision` and show the focused before-and-after view. Repeat readiness, recheck affected dependents and present the corrected revision.

### Verify comprehension before asking for approval

1. Check the recorded coverage against the exact guide and contract. Explain any missing specifications or scenarios and settle all material ambiguities. Recap the expected result, including any correction. Ask one concrete question about a material consequence or confirm a specific example with the user. For example, describe what the existing membership guard does and ask what a member and an outsider should see after the change. Let the user answer in their own words or explicitly confirm the concrete recap. Expand and check again if the response is unclear, introduces a correction, or leaves a material misunderstanding. A generic acknowledgment, an approval instruction or a valid handoff by itself provides insufficient evidence.
2. With all topics saved, record the exact comprehension prompt and actual response through `conversation-record`. Use `record_type: recap`, `status: decided`, `phase: slice_review`, `comprehension_check: true`, the current `slice_id` and `proposal_revision`, and the saved presentation in `evidence_paths`. Rerun the exact presentation input with `comprehension.exchange_id` and `comprehension.basis` explaining what the reply confirmed. The CLI checks coverage and evidence binding. Codex judges whether the answer confirms the proposed behavior. Record any consequential decision or correction separately using the usual A+ workflow.
3. After comprehension is recorded, show the exact guide path, revision and guide and contract hashes, then request explicit approval. Attach the same presentation in both `presentation_path` and `evidence_paths` on the [consent exchange](../assets/project/slice-consent.template.json). Pass the affirmative exchange ID as `consent_id` with `slice-review-record` and link consequential exchanges in `conversation_exchange_ids`. For an explicitly delegated range, record the exact slice revisions, hashes and constraints. Delegation uses the handoff evidence for the delegated reviews.
4. After approval, wait for the user's explicit instruction to continue. The register and `resume --format json` expose `pending_continue` with the exact next revision. Present that scope and record a separate `slice_consent` exchange with `consent_action: continue` to open it. After the last review, validate the collection. Package lock and coding each require their own authorization.

## Record explicit consent

Use `conversation-record` with `record_type: slice_consent` and `phase: slice_review`. Set `consent_action` to `confirm_map`, `approve`, `delegate`, `continue`, `lock` or `start`. Capture the full displayed question and actual spoken or written answer. The displayed question includes the exact IDs, revisions and hashes in `target`. The replies below are examples. Clear alternatives such as “I approve revision 1 of slice-example” and “Please start implementation of package-v1 now” also identify the exact action and scope. Record a negative, conditional or ambiguous answer as discussion and keep the gate closed. Use a new exchange for each new approval.

| Decision | Affirmative reply | Target fields |
| --- | --- | --- |
| Confirm slice map | `Confirm slice-map-v1` | `map_id`, `map_path` and `map_sha256` from the current register |
| Approve one slice | `Approve slice-example revision 1` | `slices` with one object containing `slice_id`, `proposal_revision`, `guide_sha256` and `contract_sha256` |
| Delegate review | `Delegate review of slice-example revision 1` | Same slice fields plus `constraints`. For a range, list scopes in the displayed order, separated by commas. |
| Continue to the next slice | `Continue to slice-example revision 1` | Exact `pending_continue` target from the register |
| Lock the reviewed collection | `Approve locking package-v1` | `package_id`, `collection_path`, `collection_sha256` and `recap_sha256` from the preview |
| Start implementation | `Start implementation of package-v1` | `package_id` and `package_hash` from the completed lock |

Use the generated register for progress. Review commands update the existing work cursor, or initialize one if needed. Use `work-update` for other substantial steps. When a user returns, read the saved question and any linked answer from `resume` before resuming work. Once the active question has an answer, a separately delegated sibling can be reviewed while the current slice retains its saved question and answer. Reopening or revising a proposal preserves previous receipts and invalidates the changed review and its affected dependents.

## Validate, lock and start

| Step | Command | Evidence and result |
| --- | --- | --- |
| Reconcile the collection | `slice-collection-validate` | Use the [collection input](../assets/project/slice-collection.template.json). Bind the current revisions and both hashes. Have an independent reviewer cite each applicable feature/check, render case, and shared interface, including the approved outcome or exact provider and consumers. Cover scoped inapplicability with evidence. Include affected items for a blocker. Compare decisions, schemas, routes, roles, errors, and test coverage across slices. Any blocker stops locking. |
| Approve and lock | `slice-package-lock` | Run `slice-package-preview`, save its canonical `recap` Markdown verbatim, and show the recap and hashes to the user. Record the explicit lock consent and use the [lock template](../assets/project/slice-package-lock.template.json). The CLI creates the package index, canonical manifest, exact recap and immutable lock receipt. |
| Authorize implementation | `implementation-authorize` | Present the exact package ID and full hash from the lock. Wait for an explicit new instruction to start it. Record separate start consent and use the [authorization template](../assets/project/implementation-authorization.template.json). |
| Release one executable assignment | `slice-release` | Pass the exact `package_id`, `package_hash` and `slice_id`. Release only when every prerequisite was released and accepted. Use `resume --slice-id <id>` and follow its current package `delivery_handoff.guide_path`; the contract's `locked_guide_path` identifies the exact authored evidence used to render it. Confirm `resume` and `validate` succeed and the exact slice appears under `dispatchable_slices` before dispatch or source changes. |

Under `shared_contract_review`, record a `boundary_conclusions` entry for each interacting slice pair, including pairs with a dependency or shared surface, route, path or interface. Use both sorted IDs in `slice_ids` and cite an actual line in a file frozen or approved for one of the two slices. Explain the comparison of shared routes, schemas, events, tokens or permissions, including evidence of independence where the designs share a surface but no interface.

For `case_conclusions`, use `case_kind: acceptance` for an acceptance check and `case_kind: render` for a render case. When both use the same feature and check ID, record them as two conclusions and include `case_kind` on each.

Use stable slice, feature, check, baseline or shared interface IDs in `affected_items` for material findings. The coordinator reopens the affected reviews and their dependents. If an affected item cannot be mapped to a slice, it reopens the whole collection. Resolve the recorded issue and obtain fresh review receipts before rerunning collection validation.

The CLI validates structure, provenance and explicit receipt fields. Semantic fidelity, accessibility outcomes, diagram meaning and guide clarity require independent review and user judgment. Source and test contents remain governed by the approved blueprint and existing delivery verification commands.

## Amend and recover

Stop affected work and its dependents when a material discovery changes the package. Under the same approved blueprint, unchanged released slices outside the affected dependency graph can continue under their original package authorization. Confirm the scope with `resume --slice-id <id>` and `validate --slice-id <id>`. An open question in the affected review stays on the review cursor. Re-release unchanged assignments against the newly authorized package to carry over the delivery status, immutable contract and evidence. The current package handoff names its newly rendered guide and review receipt; the retained contract may continue to point at the originating guide and records that provenance.

When cancelling a released assignment, the coordinator records the exact source in its allowed scope. This carries authorized changes into replacement planning. Keep those files at the recorded state until a replacement is separately authorized and released for the relevant paths.

Record evidence with `reconcile --action block` or `missing-feature` as applicable. For missing existing functionality, re-audit the affected scope through a full zero-new sweep and follow the blueprint reapproval procedure. Clarify the change with the user in A+ records. For a guide-only amendment, reopen and draft a new revision. A released assignment whose specification changes needs a new slice ID. On each replacement proposal, list its cancelled or superseded predecessors in `replaces_slice_ids`. Preserve their evidence and record cancellation when replacing an assignment under the same blueprint. Submit the revised inventory through `slice-plan` with the recorded amendment and actual user response. With a new blueprint approval, reconcile historical accepted work with the new contract and record fresh coverage before treating it as accepted. Create a new package version and obtain new lock approval and start authorization for the changed scope.

If a write is interrupted, retry the original command with the exact input. The prepared transition includes the staged artifact bytes, prior file hashes and the journal boundary. Recovery completes it once. Restore missing frozen files from the exact evidence set. Record a correction in a new revision and keep the original approved bytes. A repeated collection-validation request returns its committed report. To request a fresh validation run with the same evidence, add a new `validation_id` to the input.

Transfer the entire ignored initiative directory, application source and relative test references when moving to another workspace. Keep source and runner paths in accepted command arrays portable. Use a runtime available to the receiving environment and project-relative runner arguments. Include the manifest, journal, proposals, reviews, package, reports and attachments. Run `artifact-setup` in the receiving Git checkout if its local exclude needs configuring. Then start from the package index and run `resume` and `validate`. If a path or digest fails, request the original evidence from the sender before continuing. Capture versioned copies when a report needs checkpoint or register content. Workflow control files change as the initiative progresses.

## Legacy migration

Prior blueprint initiatives can continue to use their legacy assignments. Offer `slice-review-migrate` when they are ready to adopt these gates. Require `approved_by_user: true` and the verbatim `user_response`. Migration preserves all recorded history. For existing active assignments, reconcile their source and receipts and obtain a new blueprint approval before planning the reviewed inventory. Full-prototype initiatives continue to use runnable-B parity, screenshot and replay gates.

Existing locked packages retain their original approval policy. If an older reviewed-delivery initiative has an inventory and has yet to lock a package, run `slice-plan` with an amendment to create the map before continuing individual reviews or approving the package. Retain unchanged slices and their evidence in the revised inventory. Amendments that replace a locked package enter the map checkpoint before review of the replacement.

For saved slice-review packages created before scoped consent was introduced, reopen the applicable slices and record consent for a fresh reviewed package. Preserve old guide bytes, package locks and receipts as history.


## Protect approved visual and interaction details

Review the inherited protected decisions with the slice's component anatomy, responsive states and interactions. Record a newly confirmed, slice-local exception with `protected_visual_decisions` on the decided A+ `slice_detail` exchange. Include its ID and exact requirement in the guide and show it in the next review. A decision that revises an approved property or applies to other slices goes through a blueprint amendment. Update the affected guides and revalidate their approval and dependency contracts.
