# Natural replies and authorization

Use this process for blueprint initiatives with `confirmation_protocol_version: 1`. Earlier initiatives keep their recorded confirmation contracts. Ask one question for one action. In the same visible Codex reply, identify the initiative and the exact version, revision or digest under discussion. Put the question on the final line. Use a separate exchange for each consequential action.

| Action | Example question | Example reply |
| --- | --- | --- |
| Confirm the current intake | Is my understanding correct? | That looks correct. |
| Confirm the discovery audit | Is this discovery audit correct? | Yes. |
| Confirm the roadmap overview | Does this roadmap capture the direction? | Yes. |
| Advance one workstream | Are you ready to continue? | Yes, continue. |
| Create the detailed blueprint | Would you like me to proceed to the detailed blueprint? | Yes, continue. |
| Approve the current blueprint | Do you approve this blueprint? | I approve this blueprint. |
| Confirm the complete slice map | Do you confirm this slice map? | Yes. |
| Advance to the next slice | Are you ready to continue to the next slice? | Continue. |
| Approve the current slice revision | Do you approve this slice revision? | I approve this revision. |
| Delegate specific slice revisions | Do you want to delegate review of these slice revisions? | Yes. |
| Lock the reviewed package | May I lock this reviewed package? | Go ahead and lock it. |
| Authorize implementation | May I start implementation of this locked package? | Start implementation. |
| Accept the current final audit | Do you accept this final audit? | I accept this report. |

Show the required scope alongside the question. For workstreams, include the name and plan or blueprint version. For slice consent, include every value required by the consent target. For implementation, show the package ID and full hash and explain that authorization permits edits to source code within that package. For final acceptance, show the audit ID and full digest. The user can refer to the displayed scope in their reply.

An acknowledgment keeps the checkpoint open. If a reply is conditional, mixes approval with a correction, or could mean more than one thing, ask one focused clarification and wait. If the user requests a change, record their actual reply and apply the existing correction or amendment workflow. Present changed material for a fresh decision. A plan handoff opens blueprint drafting. Blueprint approval opens slice planning. Package approval permits locking. Only a separate implementation instruction for the locked package permits coding.

## Record the decision

For intake, plan and blueprint reviews, record the open checkpoint before the user replies. Then link the exact reply to that checkpoint with `responds_to`. For slice consent use the existing `slice_consent` exchange and its exact target and quote. For `visual-quality --action accept`, include the exact displayed `question`, `presentation_path`, audit ID, digest and user response.

The coordinator looks up the current thread in the local Codex session store. It verifies the preceding visible question, the exact displayed scope and the latest text reply with user provenance. It rejects a mismatched quote, an already-used reply, and ambiguity about the permitted action. The receipt stores the event IDs, timestamps, scoped action and hashes. The coordinator reads the current session log without copying the transcript into the initiative. An unavailable session or missing user provenance leaves the checkpoint waiting. Use a supported Codex session and ask the user about the same scope again if the original exchange cannot be verified.

The local coordinator trusts the Codex process and its session store. Its checks depend on those records being genuine and intact.
