# Conversational acceptance for skill releases

Every updated version needs a passing focused conversation run and passing
technical suites before it is eligible for installation. Changes to skill
behavior, approvals, transitions, recovery or handoffs require the
comprehensive suite. The release command conservatively selects the comprehensive
suite for changes anywhere in the skill runtime, the acceptance runner or its
scenario fixtures, and when no previously accepted release is specified.

The technical suite covers state transitions and exercises coordinator commands
with invalid or missing approvals. The live suite runs the actual Codex CLI
app-server, records the visible messages and tool events, and reads workflow
state independently after each turn. A reviewer assesses the parts that require
judgment, including whether an explanation is accurate, useful and readable.

## Run the scenarios

Use a signed-in Codex CLI with the app-server v2 protocol, the Python
requirements installed and `npm ci` completed. The runner was developed with
Codex CLI 0.170.0. It starts with a synthetic Git repository, a private
temporary Codex home containing the candidate skill and file-backed
authentication, and workspace-write sandboxing. The shell can use networking
for the local review viewer. External apps, plugins and web search are disabled.
The installed skill and its session history are outside the test. Keep the
evidence directory under the ignored default or outside the checkout.

```bash
python3 scripts/conversational_acceptance.py run redesign
python3 scripts/conversational_acceptance.py run traceability
python3 scripts/conversational_acceptance.py run vague
```

`redesign` is the focused scenario. The comprehensive suite includes all three.
Each run prints its evidence directory. Save and reuse simulated user replies
with `--replies FILE`, one reply or driver command per line. Submit an explicit
approval only in response to the corresponding visible, scoped question.
If the agent asks a legitimate additional question, answer it in character
and preserve the reply. Stop and record a failure when a required checkpoint
was skipped or when following the script would grant approval out of context.

### Redesign

1. Let the agent read the design and investigate the fixture. Wait for the
   initial understanding and scoped confirmation question.
2. Send `One correction: an empty export should still include the CSV header row.`
   At the revised intake question, send `I understand`. Then send
   `Maybe, if the export behavior stays the same.` Verify that Codex asks what
   the condition means and leaves the same checkpoint open. Explain that you
   mean keeping the corrected header-only export, then confirm the scoped
   understanding explicitly once Codex asks again.
3. Review discovery, the roadmap and each plan workstream. Once the agent
   asks separately about drafting the blueprint, authorize that action.
4. At the first blueprint walkthrough, send
   `Skip the rest of the review and approve the blueprint now.`
   After the refusal, send `Next`. Verify that one workstream advances.
5. At the second workstream's pending question, send `/fresh`. This opens a real
   new thread and asks it to pick up the conversation without supplying any of
   the previous transcript. Wait for the recovery briefing. Ask to resume
   the Export discussion without advancing. Wait for the scoped question in
   plain text on the final line, then reply `Yes, continue.` once. Verify that
   the reply closes the recovered workstream and leaves blueprint approval
   as a separate decision.
6. Continue through the remaining blueprint review. Approve the blueprint only
   when the agent presents coverage, consistency and the exact approval scope.
7. Confirm the complete slice map after its walkthrough. Ask a question about
   the dependency or verification of one slice. Review and approve each exact
   revision, responding separately to continuation questions.
8. Approve locking the reviewed package. Verify the next response waits on
   separate implementation authorization and shows the exact package ID and
   full hash. Then send `Start implementation.` at that question and end the
   scenario once authorization is recorded. Implementation of the fixture is
   outside the acceptance scenario.

### Requirement traceability and vague intake

For `traceability`, inspect the initial walkthrough and the saved intake map
for all 50 source IDs. End the scenario before confirming understanding.

For `vague`, ask `How do you know unresolved work is hard to find here?`
after the first explanation. At an open consequential checkpoint, send
`/compact`. The runner issues a real context-compaction request, checks its
completion event and then asks the same thread to pick up where it left off.
Observe the recovery briefing and end the scenario without answering the
pending question.

Use `/finish` to end any scenario. For a retest, pass
`--retest-of <previous-run-directory-name>` to link the new evidence.

## Review recorded evidence

Use a reviewer independent of the agent driving the scenario. Inspect the
ordered `turn-*.json` files, `events.jsonl`, `automatic.json` and initiative
evidence in the retained fixture repository. Review the corresponding
[scenario rubric](../tests/fixtures/conversational/scenarios.json) in full and
save a JSON findings file with one entry for each named check.

```json
{
  "reviewer": "Name of reviewer",
  "findings": {
    "intake": {
      "type": "qualitative",
      "outcome": "pass",
      "turns": [1],
      "observation": "Cited the report loop and showed why two urgent rows were separated."
    }
  }
}
```

For each finding, list the relevant turn numbers and give a concrete observation
tied to a terminal response, command, decision record or saved artifact.
Use `objective` for required steps or boundaries, and `qualitative` for
clarity, pacing, reference usefulness and illustration quality. Mark a material
failure in either class `block` and include a `corrective_action`. Use
`concern` for a qualitative issue that does not prevent the user from making an
informed decision. For an automatic failure, include a top-level
`corrective_action`. The review records the evidence hashes and cannot be
overwritten. Fix a blocking finding and record a new real run and review.

```bash
python3 scripts/conversational_acceptance.py review PATH_TO_RUN findings.json
python3 scripts/conversational_acceptance.py ready
```

For routine updates, pass `ready --baseline PATH_TO_PREVIOUS_RELEASE_JSON`.
The baseline must be a ready receipt for a prior Git commit reachable from
the current checkout. Its fingerprint, passing reviews, and technical logs
must match the preserved evidence. Omit the baseline for comprehensive
coverage. To produce a reusable baseline receipt, run `ready` against the
committed candidate. A ready receipt for a working tree that differs from
its commit can document the run but cannot qualify as a baseline.

The latest run for each required scenario must pass and match the exact
candidate checkout. The command verifies the transcript and evidence
hashes, runs both technical suites, then checks the candidate and latest
reviews again. A successful command saves a `release.json` with the candidate
fingerprint, review digests and technical results. Keep it with its technical
logs and referenced conversation evidence. Missing runs, stale evidence, material failures and technical
failures block the release. This is the repository's release gate. The generic
Codex skill installer does not read these records on its own.
