# Prototype to Production

A Codex skill for redesigning an existing web app from a brief or visual references, then implementing the approved prototype faithfully.

The workflow preserves the app's behavior, locks approved source and screenshots, and records progress in project-local files so work can resume across sessions and context compaction.

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
Build and visually check a runnable preview, then stop for my approval.
```

For existing work:

```text
Use $prototype-to-production to review prototype v1.
```

```text
Use $prototype-to-production to implement the approved prototype.
```

The agent discovers the relevant app, scope, and initiative from the repository and recorded evidence. Design direction comes from your brief and references. You do not need to supply internal IDs or a detailed execution checklist.

## Workflow

| Stage | Result |
| --- | --- |
| Discover | Map existing routes, states, behaviors, and dependencies. |
| Prototype | Build a separate runnable preview and inspect responsive layouts, interactions, and actual browser captures. |
| Approve | Stop for your explicit approval of a specific prototype version and cases. Lock source, fixtures, replay evidence, and screenshots with SHA-256 hashes. |
| Specify | Create vertical-slice contracts with source paths, screenshots, file ownership, dependencies, and acceptance checks. |
| Implement | Reuse approved prototype code while preserving real app behavior. |
| Verify | Compare implemented cases against the exact approved cases; compare unchanged pages against their own original baselines. |
| Integrate | Resolve blockers and pending warning decisions before final acceptance. |

Source-backed approval requires two clean-context replay runs and captures without QA-only controls. Image-only approval requires a separately approved exception. The included Playwright scaffold must be adapted to the app's browser tooling and real fixture setup.

### Blockers and visual warnings

Real UI defects, missing evidence, stale hashes, and incomplete functional coverage block acceptance. Block/unblock reconciliation keeps slice status, evidence, checkpoints, and affected dependents aligned.

Negligible capture instability can receive a provisional warning only after three clean retries and independent visual review. Approved-case warning galleries show the initial mismatch and retries side by side. Supplemental warnings, including comparisons of unchanged pages, have a separate evidence register and gallery.

Pending warnings require an explicit user decision. A provisional approved-case warning also requires `accept-warning` before final acceptance. Hashes detect missing or changed evidence; keep backups when transferring work between clones, machines, or isolated worktrees.

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

The utility does not install a browser, start the app, spawn agents, commit, merge, or deploy. Browser setup and actual visual review remain part of the workflow.

## Repository guide

| Path | Purpose |
| --- | --- |
| [SKILL.md](SKILL.md) | Agent entry point, stage routing, and quality gates. |
| [references/workflow.md](references/workflow.md) | Detailed workflow and command reference. |
| [references/continuity.md](references/continuity.md) | Checkpoints, evidence preservation, and session recovery. |
| [references/slice-contract.md](references/slice-contract.md) | Implementation handoffs and acceptance rules. |
| [references/visual-qa.md](references/visual-qa.md) | Browser verification, retries, warnings, and evidence requirements. |
| [assets/project](assets/project) | JSON templates, project instruction snippet, and Playwright scaffold. |
| [scripts/project_state.py](scripts/project_state.py) | Project state CLI. |
| [tests](tests) | Contract and state-management tests. |

## Development

Run the test suite with Python 3:

```bash
python3 -m unittest discover -s tests
```

Before committing, follow [AGENTS.md](AGENTS.md) and the required [commit message format](GIT_COMMIT_MESSAGE_FORMAT.md).
