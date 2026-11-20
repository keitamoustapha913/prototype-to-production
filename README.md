# Prototype to Production

A Codex skill for turning substantial frontend, backend, full-stack and greenfield work into an illustrated blueprint, then implementing the approved specification faithfully. The full runnable-prototype route remains available for high-fidelity redesigns.

The workflow discovers existing behavior, records exact consequential conversations, locks approved specifications and evidence, and resumes unfinished tasks or unanswered questions across sessions. Existing prototype initiatives retain their original approval and visual verification semantics.

## Install

Ask Codex to install this repository with the skill-installer skill:

```text
Use skill-installer to install https://github.com/keitamoustapha913/prototype-to-production
```

Or run the installer bundled with Codex (adjust the path if your Codex home differs):

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo keitamoustapha913/prototype-to-production \
  --path . \
  --name prototype-to-production
```

The default installation directory is `~/.codex/skills/prototype-to-production`. The installer refuses to overwrite an existing installation; back up and move the existing directory before reinstalling. The skill is available on your next turn after installation.

## Use

Open the app repository in Codex and describe the redesign. You can attach visual references or specify a style in words.

```text
Use $prototype-to-production to redesign the billing page using these reference images.
Investigate the existing behavior, discuss the roadmap and detailed blueprint with me,
then wait for my approval before implementation.
```

For existing work:

```text
Use $prototype-to-production to review prototype v1.
```

```text
Use $prototype-to-production to implement the approved design in its saved mode.
```

The agent discovers the relevant app, scope, and initiative from the repository and recorded evidence. Design direction comes from your brief and references. You do not need to supply internal IDs or a detailed execution checklist.

## Workflow

New initiatives default to **blueprint-first**:

1. Inspect actual source and behavior, or verify an empty baseline for a new project.
2. Explain findings and discuss one consequential question at a time, using diagrams or tables when useful. Preserve exact questions, options and actual answers.
3. Draft a concise roadmap, then indexed Markdown/Mermaid feature specifications with behavior, state, UI or API contracts and acceptance checks.
4. Prove every source feature or user requirement is covered. Show the exact blueprint version and wait for explicit approval before implementation.
5. Deliver small vertical slices bound to approved specs, decisions, source anchors and exclusive paths. Verify actual behavior, tests and rendered UI where relevant.

A complete runnable clone is optional. Backend-only work needs source/API/test evidence rather than screenshots. UI blueprint verification checks concrete approved criteria and real rendered states; it does not claim pixel equality against a nonexistent prototype.

To choose the preserved full-prototype route, request it explicitly or initialize with `--mode prototype`. That route still requires complete A-to-B interaction coverage, two clean-context replays, exact source/screenshot approval and approved-B verification. Existing manifests without a mode remain prototype initiatives; they are never automatically migrated.

Pending questions and task progress are durable. `resume --format json` also links decisions and coverage to their slices. Missing functionality blocks delivery until a fresh audit and the appropriate new approval. Transfer the complete Git-ignored initiative directory when moving between clones or machines.

Complete raw chat transcript capture is optional and requires an explicit request plus a supported capture mechanism. The skill reports partial or unavailable capture honestly; structured consequential decisions remain authoritative.

## Project state CLI

The Python utility records approvals, slice contracts, reports, blockers, warnings, journal events, and checkpoints. Run it from the installed skill or this checkout:

```bash
python3 scripts/project_state.py --help
python3 scripts/project_state.py init --help
```

To inspect an existing initiative, replace the example project path and initiative ID:

```bash
python3 scripts/project_state.py resume \
  --project-root /path/to/app --initiative-id billing-redesign

python3 scripts/project_state.py validate \
  --project-root /path/to/app --initiative-id billing-redesign
```

Use the initiative directory printed by `init`. In every Git repository, new designs live under ignored `.artifacts/prototype-to-production/<id>/`. This includes design documents, plans, runnable prototype source, preview assets, screenshots, and evidence. Paths in the templates are relative to that initiative directory. Only the approved implementation goes into tracked app code. Every CLI command checks that existing initiative files are untracked and ignored; legacy locations remain supported under the same rule. Store project evidence in the app's initiative directory rather than the installed skill.

Blueprint verification executes approved tests from the app root. UI verification uses Node.js 22 or later and Chrome or Chromium to capture the running app at `P2P_APP_URL`. Use an independent reviewer to inspect the assertions and captured results. See the [blueprint verification contract](references/blueprint-workflow.md#approval-implementation-and-verification).

## Repository guide

| Path | Purpose |
| --- | --- |
| [references/blueprint-workflow.md](references/blueprint-workflow.md) | Default blueprint workflow, conversation, coverage, approval and verification contracts. |
| [SKILL.md](SKILL.md) | Agent entry point, stage routing, and quality gates. |
| [references/workflow.md](references/workflow.md) | Detailed workflow and command reference. |
| [references/continuity.md](references/continuity.md) | Checkpoints, evidence preservation, and session recovery. |
| [references/slice-contract.md](references/slice-contract.md) | Implementation handoffs and acceptance rules. |
| [references/visual-qa.md](references/visual-qa.md) | Browser verification, retries, warnings, and evidence requirements. |
| [assets/project](assets/project) | JSON templates, project instruction snippet, and Playwright scaffold. |
| [scripts/project_state.py](scripts/project_state.py) | Project state CLI and legacy prototype gates. |
| [scripts/blueprint_state.py](scripts/blueprint_state.py) | Blueprint-specific contracts and validation. |
| [tests](tests) | Contract and state-management tests. |

## Development

Run the test suite with Python 3:

```bash
python3 -m unittest discover -s tests
```

### Live workflow checks

The November 2026 review ran Codex CLI 0.170.0 in temporary workspaces with synthetic apps and project-local copies of the skill.

| Scenario | Observed result |
| --- | --- |
| Ask for a shared backend contract for task listing and creation | With the baseline skill at `b1b4274`, Codex used general design guidance. The blueprint-first skill selected itself, audited authorization and response behavior, and saved one pending question. A fresh session recorded the answer, froze the roadmap and blueprint with coverage, and stopped for approval with application files unchanged. |
| Ask to merge Chat and Channels | The skill inspected both pages in Chrome and read their handlers. It explained sending messages and creating channels, saved the exact layout question, and paused for an answer. |
| Start a neighborhood tool-loan app | The skill recorded the empty baseline and requirement IDs for public browsing and signed-in posting. It saved a question about how borrowers contact owners. |
| Request an isolated validation-message fix | The skill stayed out of the ordinary edit. Codex changed the message and verified the existing error status. |
| Explicitly invoke the skill for the same fix | Codex froze a short roadmap, a single-feature blueprint and its coverage, then asked for approval with application files unchanged. |

Before committing, follow [AGENTS.md](AGENTS.md) and the required [commit message format](GIT_COMMIT_MESSAGE_FORMAT.md).
