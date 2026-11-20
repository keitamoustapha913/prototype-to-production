# Approved slice title

Replace the heading with the exact selected proposal title.

Replace every instruction with project-specific content. Repeat relevant annexes for each approved feature.

## Identity and status

Use the exact identifiers from the approved records. State the selected proposal title and the truthful state at freeze: pending review, with no package assigned yet. Link the approved feature documents and all recorded exchanges.

- slice_id: `REPLACE`
- title: `REPLACE`
- proposal_revision: `REPLACE`
- blueprint_version: `REPLACE`
- blueprint_approval_id: `REPLACE`
- plan_version: `REPLACE`
- source_snapshot_sha256: `REPLACE`
- owner: `REPLACE`
- scope: `REPLACE`
- review_status: Pending review; not approved.
- package: Not assigned at guide freeze.

## In plain language

Explain the goal, who benefits, the starting point and the observable finished result.

## Why this slice exists

Cite the parent features, baseline behavior, decisions, boundaries and explicit exclusions.

## What exists now

Show actual paths and file symbols, observed behavior, dependencies and relevant tests.

## Finished experience or service contract

For guide format version 3, use only this table in the section. Copy one row per approved feature and acceptance check. Keep explanatory plain-language wording in the section above. Do not add prose or examples here because this section is the canonical, mechanically checked summary of expected outcomes.

| Feature | Check ID | Expected outcome |
| --- | --- | --- |

## Architecture and anatomy

For every feature, name its owned components and exact implementation paths, data flows, ownership, inputs and outputs. Include an informative diagram. Label each state transition with its approved trigger, guard and effect.

## Detailed functional requirements

Give numbered operations that name the feature and its acceptance check IDs. State triggers, guards, roles, validation, persistence and effects for each operation.

## Visual specification

Give UI routes, states, geometry, typography, breakpoints, tokens and accessible behavior. Give a specific reason if inapplicable.

## Interface specification

Give exact service interfaces, schemas, permissions, errors, side effects and compatibility. Give a specific reason if inapplicable.

## State and scenario matrix

Copy the approved state and check IDs and their exact expected outcomes. For a render case, use its case ID and state with the outcome `renders ROUTE at WIDTHxHEIGHT with SELECTOR`, using the approved values. Cover each feature's applicable roles, races and retries.

| Feature | Check ID | State | Expected outcome |
| --- | --- | --- | --- |

## Data and security rules

Describe source of truth, security and privacy behavior, transactions, idempotency and compatibility.

## Exact implementation footprint

Use one row per owned path and associate it with the approved feature. Operations are `CREATE`, `MODIFY`, `DELETE` and `VERIFY`. Keep paths owned by approved `KEEP` features. Record planned files as `CREATE` and describe affected symbols and prerequisites in the recipe.

| Path | Operation | Feature |
| --- | --- | --- |

## Step-by-step implementation recipe

Give ordered steps with stable step IDs, target files and symbols, expected intermediate results and checks. Every step has an explicit `Expected result:`.

## Dependencies and integration

Name dependencies, shared contracts, order, ownership and how to wait for acceptance.

## Worked examples and diagrams

Include worked examples and link their evidence and interface fixtures. Include and link the Mermaid state diagram for approved state transitions and the sequence diagram for an approved sequence contract.

## Traceability matrix

Map each feature, baseline, decision, state and check ID to an exact heading and named implementation step.

| Feature | Kind | ID | Section | Step |
| --- | --- | --- | --- | --- |

## Tests and acceptance evidence

Use the exact approved check IDs, expected outputs, fixture paths and JSON argument arrays. A `planned` row uses `not run` as its evidence. An `observed` row names a frozen copy of matching coordinator `verification.json` from an earlier authorized delivery. Its execution receipt must still validate against the active blueprint.

Fixture sources: `PROJECT-RELATIVE-APPROVED-TEST-PATHS`

| Check ID | Status | Expected outcome | Command | Evidence |
| --- | --- | --- | --- | --- |

## Safety, rollback and prohibited deviations

Add concrete `Rollback steps:` for each affected path, including the restore/revert action, migrations or backfills, forbidden changes and when to escalate.

## Definition of done

List observable code, evidence, compatibility, UX, test and review completion criteria.

## Fresh-session handoff

List at least one existing project-relative file or frozen artifact for the implementer to read. List files that will be created in the implementation footprint.

- First files to read: [[REPLACE]]
- Environment: Set `P2P_SKILL_ROOT` to the installed prototype-to-production skill directory and use the project's documented runtime.
- Resume command: `python3 "$P2P_SKILL_ROOT/scripts/project_state.py" resume --project-root . --initiative-id INITIATIVE-ID --format json`
- Validation command: `python3 "$P2P_SKILL_ROOT/scripts/project_state.py" validate --project-root . --initiative-id INITIATIVE-ID`
- Current blockers: [[REPLACE]]
- Next action: [[REPLACE]]
- How to report findings: [[REPLACE]]

## Change and approval history

For every recorded A+ exchange, link its frozen conversation file and include its SHA-256. Record the exact review outcome, every approved exception (or `Exceptions: none`), and all invalidated prior revisions (or `Invalidated revisions: none; this is the initial revision.`). Include the hash inventory needed to identify the exact approved inputs. The locked package index links the live review receipt and prior immutable package versions.

### UI annex — FEATURE-ID

- Route and entry: [[REPLACE]]
- Viewports and breakpoints: [[REPLACE]]
- Layout and tokens: [[REPLACE]]
- Copy and assets: [[REPLACE]]
- States and roles: [[REPLACE]]
- Keyboard and assistive technology: [[REPLACE]]
- Events and dependencies: [[REPLACE]]
- Render evidence: [[REPLACE]]

### BACKEND annex — FEATURE-ID

- Inputs and outputs: [[REPLACE]]
- Authorization and validation: [[REPLACE]]
- Persistence and transactions: [[REPLACE]]
- Concurrency and retries: [[REPLACE]]
- Side effects and observability: [[REPLACE]]
- Compatibility and rollback: [[REPLACE]]
- Verification: [[REPLACE]]

### API annex — FEATURE-ID

- Method and route: [[REPLACE]]
- Request and response schemas: [[REPLACE]]
- Statuses and error schemas: [[REPLACE]]
- Authentication and scope: [[REPLACE]]
- Validation and query options: [[REPLACE]]
- Concurrency and timeouts: [[REPLACE]]
- Side effects and compatibility: [[REPLACE]]
- Verification: [[REPLACE]]

### MCP annex — FEATURE-ID

- Server and transport: [[REPLACE]]
- Registration and capabilities: [[REPLACE]]
- Input and output schemas: [[REPLACE]]
- Permissions and consent: [[REPLACE]]
- Protocol and tool errors: [[REPLACE]]
- Cancellation and timeouts: [[REPLACE]]
- Streaming and data exposure: [[REPLACE]]
- Client invocation and verification: [[REPLACE]]

### SCRIPT annex — FEATURE-ID

- Entry point and runtime: [[REPLACE]]
- Flags and configuration: [[REPLACE]]
- Environment and working directory: [[REPLACE]]
- Inputs and outputs: [[REPLACE]]
- File mutations: [[REPLACE]]
- Exit codes and streams: [[REPLACE]]
- Recovery and concurrency: [[REPLACE]]
- Verification: [[REPLACE]]
