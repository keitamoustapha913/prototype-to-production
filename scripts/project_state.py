#!/usr/bin/env python3
"""Durable, project-local state for exact prototype-to-production handoffs."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import struct
import sys
import tempfile
import zlib


VALID_SCOPES = frozenset({"feature", "page", "app"})
INITIATIVE_ID = re.compile(r"^[a-z][a-z0-9-]{2,60}$")
VERSION_ID = re.compile(r"^[a-z][a-z0-9-]{1,60}$")
SKILL_ROOT = Path(__file__).resolve().parents[1]
HIDDEN_ARTIFACT_ROOT = Path(".artifacts") / "prototype-to-production"
LEGACY_ARTIFACT_ROOT = Path("design") / "redesigns"


class StateError(Exception):
    """A stopped stage gate, never an implicit success."""


def validate_scope(scope: str) -> list[str]:
    """Return actionable contract errors (empty means valid)."""
    return [] if isinstance(scope, str) and scope in VALID_SCOPES else [f"Invalid scope {scope!r}; choose feature, page or app"]


def validate_slice_contract(packet: dict) -> list[str]:
    if not isinstance(packet, dict):
        return ["Slice contract must be an object"]
    errors = validate_scope(packet.get("scope"))
    mandatory = (
        "slice_id", "owner", "status", "allowed_paths", "dependencies",
        "current_behavior", "prototype_version", "prototype_source_paths",
        "visual_cases", "acceptance_checks", "functional_tests", "evidence",
    )
    errors.extend(f"Missing slice field: {field}" for field in mandatory if field not in packet)
    if packet.get("status") in {"ready", "in_progress", "reported", "accepted"} and not packet.get("visual_cases"):
        errors.append("Ready or active slice requires exact visual cases")
    for key in ("allowed_paths", "current_behavior", "acceptance_checks", "functional_tests"):
        if key in packet and (not isinstance(packet[key], list) or not packet[key]):
            errors.append(f"Slice needs concrete {key} rather than empty instructions")
    errors.extend(validate_visual_cases(packet.get("visual_cases")))
    return errors


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_workspace_root(project_root: Path) -> Path | None:
    """Detect normal, unborn and linked Git worktrees without changing them."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 and result.stdout.strip() else None


def location(project_root: str, initiative_id: str) -> Path:
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise StateError(f"Project root is not a directory: {root}")
    if not INITIATIVE_ID.fullmatch(initiative_id):
        raise StateError("Initiative ID must be 3–61 lowercase letters/digits/hyphens, starting with a letter")
    hidden = root / HIDDEN_ARTIFACT_ROOT / initiative_id
    legacy = root / LEGACY_ARTIFACT_ROOT / initiative_id
    for candidate in (hidden, legacy):
        if candidate.parent.resolve() != candidate.parent or candidate.is_symlink():
            raise StateError("Initiative directory cannot use a symlink or redirect outside its project path")
    existing = [candidate for candidate in (hidden, legacy) if (candidate / "manifest.json").is_file()]
    if len(existing) > 1:
        raise StateError("Ambiguous initiative: both hidden and legacy state exist; reconcile before continuing")
    if existing:
        return existing[0]
    # Never trample an incomplete/unrecognized initiative directory.
    if hidden.exists() or legacy.exists():
        raise StateError("Unrecognized or partially initialized initiative directory; inspect it before continuing")
    return hidden if git_workspace_root(root) else legacy


def ensure_local_git_ignore(project_root: Path, directory: Path) -> None:
    """Keep a new Git-backed initiative local without modifying tracked .gitignore."""
    root = git_workspace_root(project_root)
    if root is None:
        return
    if directory.is_relative_to(project_root / HIDDEN_ARTIFACT_ROOT):
        ignore_base = HIDDEN_ARTIFACT_ROOT
    elif directory.is_relative_to(project_root / LEGACY_ARTIFACT_ROOT):
        # Existing initiatives stay in place; never move old source/screenshot links.
        ignore_base = directory.relative_to(project_root)
    else:
        return
    relative = directory.relative_to(root).as_posix()
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", relative + "/"],
        capture_output=True, text=True,
    )
    if tracked.returncode != 0 or tracked.stdout.strip():
        raise StateError("Existing tracked redesign artifacts cannot be made untracked by an ignore rule; ask before any Git index change")
    target = directory / "manifest.json"

    def is_ignored() -> bool:
        return subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", str(target)],
            capture_output=True,
        ).returncode == 0

    if is_ignored():
        return
    git_path = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True,
    )
    if git_path.returncode != 0 or not git_path.stdout.strip():
        raise StateError("Cannot locate Git's local exclude file for private redesign artifacts")
    exclude = Path(git_path.stdout.strip())
    if not exclude.is_absolute():
        exclude = root / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    project_prefix = project_root.relative_to(root).as_posix()
    pattern = "/" + ("" if project_prefix == "." else project_prefix + "/") + ignore_base.as_posix() + "/"
    with exclude.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing = handle.read()
            if pattern not in existing.splitlines():
                handle.seek(0, os.SEEK_END)
                handle.write(("\n" if existing and not existing.endswith("\n") else "")
                             + "# prototype-to-production: local state and visual evidence\n" + pattern + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    if not is_ignored():
        raise StateError("Git is not ignoring the redesign artifact directory; inspect .gitignore exclusion rules")


@contextmanager
def coordinator_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".coordinator.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_json(path: Path, payload: dict) -> None:
    atomic_text(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise StateError(f"Expected JSON object: {path}")
    return value


def manifest_at(directory: Path) -> dict:
    if not (directory / "manifest.json").exists():
        raise StateError(f"Initiative not initialized: {directory}. Run init first.")
    return read_json(directory / "manifest.json")


def artifact_file(directory: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise StateError("Artifact path must be a nonempty string")
    untrusted = Path(relative)
    if untrusted.is_absolute() or not untrusted.parts or ".." in untrusted.parts:
        raise StateError(f"Artifact path must be relative to initiative: {relative!r}")
    try:
        path = (directory / untrusted).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StateError(f"Missing approved artifact {relative!r}: {error}") from error
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise StateError(f"Approved artifact escapes initiative or is not a file: {relative!r}")
    return path


def digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_dimensions(path: Path) -> tuple[int, int]:
    """Reject bogus/truncated PNG evidence and return its actual pixel dimensions."""
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 45:
        raise StateError(f"Invalid PNG screenshot: {path}")
    offset, ihdr, image_data, ended = 8, False, False, False
    dimensions = (0, 0)
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        if size > len(data) - offset - 12:
            raise StateError(f"Truncated PNG screenshot: {path}")
        tag = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        expected_crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if zlib.crc32(tag + payload) != expected_crc:
            raise StateError(f"PNG chunk CRC mismatch: {path}")
        if not ihdr:
            if tag != b"IHDR" or size != 13:
                raise StateError(f"PNG missing first IHDR chunk: {path}")
            dimensions = struct.unpack_from(">II", payload, 0)
            if not 1 <= dimensions[0] <= 10000 or not 1 <= dimensions[1] <= 10000:
                raise StateError(f"PNG has invalid dimensions: {path}")
            ihdr = True
        if tag == b"IDAT":
            image_data = True
        if tag == b"IEND":
            ended = (size == 0 and offset + 12 == len(data))
            break
        offset += 12 + size
    if not (ihdr and image_data and ended):
        raise StateError(f"PNG missing image data or final IEND: {path}")
    return dimensions


def validate_visual_cases(cases: object) -> list[str]:
    if not isinstance(cases, list) or not cases:
        return ["At least one approved visual case with a screenshot is required"]
    errors, ids = [], set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"Visual case #{index + 1} is not an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in ids:
            errors.append(f"Missing or duplicate visual case ID at #{index + 1}")
        ids.add(case_id)
        for field in ("reference_file", "route", "fixture", "state_setup"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                errors.append(f"Visual case {case_id}: missing {field}")
        if isinstance(case.get("route"), str) and not case["route"].startswith("/"):
            errors.append(f"Visual case {case_id}: route must start with /")
        viewport = case.get("viewport")
        if not isinstance(viewport, dict) or not all(
            type(viewport.get(key)) is int and 200 <= viewport[key] <= 10000
            for key in ("width", "height")
        ):
            errors.append(f"Visual case {case_id}: valid width and height required")
    return errors


def validate_replay_proof(directory: Path, approval: dict) -> tuple[list[str], list[str]]:
    """Check durable evidence that every source-mode reference can be replayed."""
    proof = approval.get("replay")
    if not isinstance(proof, dict):
        return [], ["Source approval requires a replay proof with deterministic browser evidence"]
    version = approval.get("approved_version")
    errors: list[str] = []
    for key in ("command", "script", "evidence_file"):
        if not isinstance(proof.get(key), str) or not proof[key].strip():
            errors.append(f"Replay proof requires a nonempty {key}")
    if proof.get("qa_controls_excluded") is not True:
        errors.append("Replay proof must exclude prototype-only QA controls from approved product screenshots")
    script, evidence_file = proof.get("script"), proof.get("evidence_file")
    if not isinstance(script, str) or not script.startswith(f"prototype/versions/{version}/"):
        errors.append(f"Replay script must be versioned under prototype/versions/{version}/")
    if not isinstance(evidence_file, str) or not evidence_file.startswith(f"evidence/{version}/"):
        errors.append(f"Replay evidence must be versioned under evidence/{version}/")
    paths = [path for path in (script, evidence_file) if isinstance(path, str) and path]
    try:
        evidence = read_json(artifact_file(directory, evidence_file)) if isinstance(evidence_file, str) else {}
    except (OSError, StateError) as error:
        errors.append(f"Replay evidence unavailable: {error}")
        return paths, errors
    if evidence.get("status") != "PASS" or type(evidence.get("clean_context_runs")) is not int or evidence["clean_context_runs"] < 2:
        errors.append("Replay evidence requires PASS from two or more clean browser contexts")
    records = evidence.get("cases")
    if not isinstance(records, list):
        records = []
    cases = approval.get("visual_cases")
    expected = {case["case_id"]: case for case in cases if isinstance(case, dict) and isinstance(case.get("case_id"), str)} if isinstance(cases, list) else {}
    observed: dict[str, dict] = {}
    for row in records:
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str) or row["case_id"] in observed:
            errors.append("Replay evidence contains an invalid or repeated case")
            continue
        observed[row["case_id"]] = row
    if set(observed) != set(expected):
        errors.append("Replay evidence must cover exactly every approved visual case")
    for case_id, case in expected.items():
        record = observed.get(case_id)
        if not record:
            continue
        try:
            digest = digest_of(artifact_file(directory, case["reference_file"]))
        except (StateError, OSError, KeyError) as error:
            errors.append(f"Replay reference {case_id}: {error}")
            continue
        if record.get("reference_sha256") != digest or record.get("matching_pixels") is not True:
            errors.append(f"Replay visual case {case_id} does not match the saved reference screenshot")
        if record.get("fixture_replayed") is not True:
            errors.append(f"Replay visual case {case_id} has no proven fixture setup")
        if record.get("qa_controls_visible") is not False:
            errors.append(f"Replay visual case {case_id} includes QA-only controls")
        first, second = record.get("first_capture"), record.get("second_capture")
        if first == second or any(path == case.get("reference_file") for path in (first, second)):
            errors.append(f"Replay visual case {case_id}: fresh captures must be separate files from the reference and each other")
        for label, path in (("first", first), ("second", second)):
            if not isinstance(path, str) or not path.startswith(f"evidence/{version}/") or not path.endswith(".png"):
                errors.append(f"Replay visual case {case_id}: {label} capture must be a versioned PNG in evidence/{version}/")
                continue
            paths.append(path)
            try:
                captured = artifact_file(directory, path)
                dimensions = png_dimensions(captured)
                viewport = case.get("viewport") if isinstance(case.get("viewport"), dict) else {}
                if dimensions != (viewport.get("width"), viewport.get("height")):
                    errors.append(f"Replay visual case {case_id}: {label} capture has a wrong viewport")
                if digest_of(captured) != digest:
                    errors.append(f"Replay visual case {case_id}: {label} capture bytes differ from the saved reference")
            except (StateError, OSError) as error:
                errors.append(f"Replay visual case {case_id}: missing or invalid {label} capture: {error}")
    return paths, errors


def validate_approval_record(directory: Path, value: dict, existing: dict) -> tuple[dict, list[str]]:
    errors = validate_scope(value.get("scope"))
    if value.get("scope") != existing.get("scope"):
        errors.append("Approved scope must match initialized initiative")
    decision_id, version = value.get("decision_id"), value.get("approved_version")
    if not isinstance(decision_id, str) or not INITIATIVE_ID.fullmatch(decision_id):
        errors.append("decision_id must be a stable lowercase identifier")
    if not isinstance(version, str) or not VERSION_ID.fullmatch(version):
        errors.append("approved_version must be a stable lowercase identifier")
    if decision_id in existing.get("approvals", {}):
        errors.append("Approval ID already used; never overwrite a recorded approval")
    if any(a.get("approved_version") == version for a in existing.get("approvals", {}).values()):
        errors.append("Prototype version already approved; use a fresh version instead of rewriting history")
    if value.get("approved_by_user") is not True or not str(value.get("user_approval_note") or "").strip():
        errors.append("Explicit user approval and a record of what was approved are required")
    mode = value.get("mode")
    if mode not in {"source", "image-only"}:
        errors.append("Approval mode must be source or image-only")
    sources = value.get("source_paths", [])
    assets = value.get("asset_paths", [])
    if not isinstance(sources, list) or not isinstance(assets, list):
        errors.append("source_paths and asset_paths must be lists")
        sources, assets = [], []
    if mode == "source" and not sources:
        errors.append("Source approval requires preserved runnable prototype source")
    if mode == "image-only" and not (
        value.get("image_only_approved") is True and str(value.get("image_only_reason") or "").strip()
    ):
        errors.append("Image-only fallback requires separate explicit user approval and rationale")
    cases = value.get("visual_cases")
    errors.extend(validate_visual_cases(cases))
    paths = list(sources) + list(assets)
    if mode == "source" or "replay" in value:
        replay_paths, replay_errors = validate_replay_proof(directory, value)
        paths.extend(replay_paths)
        errors.extend(replay_errors)
    if isinstance(cases, list):
        for case in cases:
            if isinstance(case, dict):
                paths.extend([case.get("reference_file"), case.get("fixture")])
    digests: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, str) or not source.startswith(f"prototype/versions/{version}/"):
            errors.append(f"Prototype source must be versioned under prototype/versions/{version}/: {source!r}")
    for case in cases if isinstance(cases, list) else []:
        if isinstance(case, dict) and isinstance(case.get("reference_file"), str):
            if not case["reference_file"].startswith(f"references/{version}/"):
                errors.append(f"Screenshot must be versioned under references/{version}/: {case['reference_file']!r}")
    for path in paths:
        try:
            digests[path] = digest_of(artifact_file(directory, path))
        except (StateError, OSError, TypeError) as error:
            errors.append(str(error))
    if isinstance(cases, list):
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("reference_file"), str):
                continue
            try:
                dims = png_dimensions(artifact_file(directory, case["reference_file"]))
                viewport = case.get("viewport")
                if isinstance(viewport, dict) and dims != (viewport.get("width"), viewport.get("height")):
                    errors.append(f"Reference PNG viewport mismatch for {case['reference_file']}: image {dims}, requested {viewport}")
            except (StateError, OSError) as error:
                errors.append(str(error))
    frozen = {**value, "approved_at": utc_now(), "artifact_hashes": digests,
              "replay_required": mode == "source"}
    return frozen, errors


def check_approval(directory: Path, manifest: dict) -> list[str]:
    approval_id = manifest.get("active_approval_id")
    if not approval_id:
        return ["No active approved prototype"] if manifest.get("stage") not in {"discover", "prototype"} else []
    approval = manifest.get("approvals", {}).get(approval_id)
    if not approval or manifest.get("approved_version") != approval.get("approved_version"):
        return ["Approval ID/version conflicts with manifest; coordinator reconciliation required"]
    errors = validate_visual_cases(approval.get("visual_cases"))
    if approval.get("mode") == "source" and not approval.get("source_paths"):
        errors.append("Approved prototype source paths are missing")
    if approval.get("mode") == "image-only" and approval.get("image_only_approved") is not True:
        errors.append("Image-only reconstruction was not explicitly approved")
    replay_paths: list[str] = []
    if approval.get("replay_required") or "replay" in approval:
        replay_paths, replay_errors = validate_replay_proof(directory, approval)
        errors.extend(replay_errors)
    for path, expected in approval.get("artifact_hashes", {}).items():
        try:
            actual = digest_of(artifact_file(directory, path))
            if actual != expected:
                errors.append(f"sha256 mismatch for locked artifact {path}: expected {expected}, found {actual}")
        except (StateError, OSError) as error:
            errors.append(str(error))
    expected_paths = set(approval.get("source_paths", [])) | set(approval.get("asset_paths", [])) | set(replay_paths)
    for case in approval.get("visual_cases", []):
        if isinstance(case, dict):
            expected_paths.update([case.get("reference_file"), case.get("fixture")])
    if not expected_paths or set(approval.get("artifact_hashes", {})) != expected_paths:
        errors.append("Approved artifact hash inventory does not match approval references")
    return errors


def journal(directory: Path, item: dict) -> None:
    event = {**item, "at": utc_now()}
    encoded = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with (directory / "decisions.jsonl").open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def render_checkpoint(manifest: dict) -> str:
    recent = manifest.get("last_checkpoint") or {}
    lines = [
        f"# Redesign checkpoint: {manifest['initiative_id']}", "",
        "This is a recovery index. The manifest, journal, approved files, and live repository are authoritative.",
        "", f"Scope: {manifest['scope']}", f"Stage: {manifest['stage']}",
        f"Approved prototype: {manifest.get('approved_version') or 'none'}",
        f"Active approval: {manifest.get('active_approval_id') or 'none'}",
        f"Next action: {manifest.get('next_action') or 'UNSET'}", "",
        "## Recent progress", "",
        recent.get("summary") or "No checkpoint summary yet.", "",
        "## Completed", "",
    ]
    lines.extend(f"- {item}" for item in recent.get("completed", []))
    lines += ["", "## Blockers", ""]
    lines.extend(f"- {item}" for item in manifest.get("blockers", []))
    lines += ["", "## Slices", ""]
    for slice_id, item in sorted(manifest.get("slices", {}).items()):
        lines.append(f"- {slice_id}: {item.get('status', 'unknown')} ({item.get('owner', 'unassigned')})")
    lines += ["", "## Capture warnings needing user review", ""]
    pending = [
        f"- {slice_id}: {', '.join(item.get('warning_case_ids') or [])} (provisional, NOT accepted)"
        for slice_id, item in sorted(manifest.get("slices", {}).items())
        if item.get("status") == "provisional"
    ]
    lines.extend(pending or ["None pending."])
    lines += ["", "## Recorded repository provenance", ""]
    for key, value in manifest.get("provenance", {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines).rstrip() + "\n"


def note_progress(manifest: dict, summary: str, completed: str | None = None) -> None:
    """Refresh the recovery index synchronously with every material transition."""
    previous = manifest.get("last_checkpoint") or {}
    finished = list(previous.get("completed", []))
    if completed and completed not in finished:
        finished.append(completed)
    manifest["last_checkpoint"] = {
        "at": utc_now(), "summary": summary,
        "completed": finished, "owner": "coordinator",
    }


def initialize(directory: Path, scope: str, next_action: str | None) -> None:
    errors = validate_scope(scope)
    if errors:
        raise StateError("; ".join(errors))
    with coordinator_lock(directory):
        if (directory / "manifest.json").exists():
            manifest = manifest_at(directory)
            if manifest.get("scope") != scope:
                raise StateError(f"Existing scope is {manifest.get('scope')!r}; refusing to overwrite")
            print(f"Existing initiative preserved: {directory}")
            return
        template = read_json(SKILL_ROOT / "assets/project/manifest.template.json")
        template.update({
            "initiative_id": directory.name,
            "scope": scope,
            "stage": "discover",
            "next_action": next_action or template["next_action"],
        })
        template["provenance"]["created_at"] = utc_now()
        for subdirectory in ("prototype/versions", "references", "fixtures", "slices"):
            (directory / subdirectory).mkdir(parents=True, exist_ok=True)
        (directory / "decisions.jsonl").touch(exist_ok=True)
        atomic_json(directory / "manifest.json", template)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(template))
    print(f"Initialized initiative: {directory}")


def add_event(directory: Path, event_file: str) -> None:
    value = read_json(Path(event_file))
    allowed = {"type", "summary", "code_paths", "artifact_paths", "next_action", "blockers", "evidence", "decision_id"}
    extraneous = set(value) - allowed
    if extraneous:
        raise StateError(f"Event cannot mutate canonical fields: {', '.join(sorted(extraneous))}")
    if value.get("type") not in {
        "style", "interaction", "decision", "progress", "blocker", "discovery", "prototype", "handoff", "recovery",
    } or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise StateError("Event needs a recognized type and nonempty summary")
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        journal(directory, value)
        if "next_action" in value:
            manifest["next_action"] = value["next_action"]
        if "blockers" in value:
            manifest["blockers"] = value["blockers"]
        note_progress(manifest, value["summary"])
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Recorded {value['type']} decision in {directory / 'decisions.jsonl'}")


def checkpoint(directory: Path, checkpoint_file: str) -> None:
    value = read_json(Path(checkpoint_file))
    allowed = {"summary", "next_action", "blockers", "completed", "repository_head", "worktree_status", "owner"}
    if set(value) - allowed or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise StateError("Checkpoint needs a summary and only allowed recovery fields")
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        manifest["last_checkpoint"] = {
            "at": utc_now(), "summary": value["summary"],
            "completed": value.get("completed", []), "owner": value.get("owner", "coordinator"),
        }
        if "next_action" in value:
            manifest["next_action"] = value["next_action"]
        if "blockers" in value:
            manifest["blockers"] = value["blockers"]
        for field in ("repository_head", "worktree_status"):
            if field in value:
                manifest["provenance"][field] = value[field]
        journal(directory, {"type": "checkpoint", **value})
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Checkpoint saved: {directory / 'CHECKPOINT.md'}")


def approve(directory: Path, approval_file: str) -> None:
    candidate = read_json(Path(approval_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        approval, errors = validate_approval_record(directory, candidate, manifest)
        if errors:
            raise StateError("Approval invalid: " + "; ".join(errors))
        previous = manifest.get("active_approval_id")
        journal(directory, {
            "type": "approval", "decision_id": approval["decision_id"],
            "approved_version": approval["approved_version"], "supersedes": previous,
            "approval": approval,
        })
        manifest["approvals"][approval["decision_id"]] = approval
        manifest.update({
            "active_approval_id": approval["decision_id"],
            "approved_version": approval["approved_version"],
            "stage": "approved",
            "references": approval["visual_cases"],
            "blockers": [],
            "next_action": "Create complete slice contracts grounded in the locked prototype",
        })
        note_progress(manifest, f"Approved prototype {approval['approved_version']}, decision {approval['decision_id']}",
                      completed=f"Frozen prototype {approval['approved_version']}")
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Approved and hash-locked {approval['approved_version']} ({approval['decision_id']})")


def revoke_approval(directory: Path, reason: str) -> None:
    if not reason.strip():
        raise StateError("A written reason is required to revoke approval")
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        active = manifest.get("active_approval_id")
        if not active:
            raise StateError("No active approval to revoke")
        journal(directory, {"type": "approval_revoked", "decision_id": active, "reason": reason})
        manifest.update({
            "active_approval_id": None, "approved_version": None, "stage": "prototype",
            "references": [], "next_action": "Revise prototype and seek explicit new approval",
            "blockers": [f"Approval {active} revoked: {reason}; implementation is paused"],
        })
        note_progress(manifest, f"Revoked approval {active}: {reason}")
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Approval revoked (history preserved): {active}")


def owned_path(raw: str) -> tuple[tuple[str, ...], bool]:
    """A file or a directory/**. Other globs are too ambiguous for agent ownership."""
    if not isinstance(raw, str) or not raw.strip() or raw.startswith("/"):
        raise StateError(f"Invalid owned path: {raw!r}")
    is_tree = raw.endswith("/**")
    path = raw[:-3] if is_tree else raw
    parts = Path(path).parts
    if not parts or any(part in {"..", "."} or "*" in part or "?" in part or "[" in part for part in parts):
        raise StateError(f"Owned paths must be exact files or directories ending /**: {raw!r}")
    return parts, is_tree


def ownership_overlaps(first: str, second: str) -> bool:
    a, a_tree = owned_path(first)
    b, b_tree = owned_path(second)
    return (a == b) or (a_tree and b[:len(a)] == a) or (b_tree and a[:len(b)] == b)


def create_slice(directory: Path, contract_file: str) -> None:
    candidate = read_json(Path(contract_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        approval_errors = check_approval(directory, manifest)
        if approval_errors:
            raise StateError("Cannot assign slice: " + "; ".join(approval_errors))
        active_id = manifest.get("active_approval_id")
        if not active_id:
            raise StateError("Cannot create a slice without active approval")
        approval = manifest["approvals"][active_id]
        errors = validate_slice_contract(candidate)
        slice_id = candidate.get("slice_id")
        if not isinstance(slice_id, str) or not INITIATIVE_ID.fullmatch(slice_id):
            errors.append("slice_id must be a stable lowercase identifier")
        if slice_id in manifest.get("slices", {}):
            errors.append("Duplicate slice_id; existing slices cannot be overwritten")
        if candidate.get("prototype_version") != approval["approved_version"]:
            errors.append("Slice must target the current exact approved prototype version")
        permitted_scopes = {
            "app": {"app", "page", "feature"},
            "page": {"page", "feature"},
            "feature": {"feature"},
        }[manifest["scope"]]
        if candidate.get("scope") not in permitted_scopes:
            errors.append("Slice scope cannot be broader than its approved redesign scope")
        if not isinstance(candidate.get("owner"), str) or candidate.get("owner") in {"", "unassigned"}:
            errors.append("Assign a specific owner before dispatching a slice")
        if candidate.get("status") not in {"draft", "ready"}:
            errors.append("New slice contract status must be draft or ready")
        sources = candidate.get("prototype_source_paths")
        if not isinstance(sources, list) or not set(sources).issubset(set(approval.get("source_paths", []))):
            errors.append("Slice prototype source paths must be present in the approved source")
        if approval.get("mode") == "source" and not sources:
            errors.append("Source-backed slice needs exact approved prototype source paths")
        approved_cases = {case["case_id"]: case for case in approval["visual_cases"]}
        if isinstance(candidate.get("visual_cases"), list):
            for case in candidate["visual_cases"]:
                if not isinstance(case, dict) or approved_cases.get(case.get("case_id")) != case:
                    errors.append("Every visual case must exactly equal a user-approved case (route, screenshot, fixture, state, viewport)")
        paths = candidate.get("allowed_paths")
        if isinstance(paths, list):
            for path in paths:
                try:
                    owned_path(path)
                    for old_id, old in manifest.get("slices", {}).items():
                        if old.get("status") != "cancelled" and any(ownership_overlaps(path, old_path) for old_path in old["allowed_paths"]):
                            errors.append(f"File ownership overlap with slice {old_id}: {path}")
                except StateError as error:
                    errors.append(str(error))
            for index, path in enumerate(paths):
                if any(ownership_overlaps(path, earlier) for earlier in paths[:index]):
                    errors.append(f"Duplicate or overlapping paths within slice: {path}")
        dependencies = candidate.get("dependencies", [])
        if not isinstance(dependencies, list):
            errors.append("Dependencies must be an array")
            dependencies = []
        for dep in dependencies:
            if dep == slice_id or dep not in manifest.get("slices", {}):
                errors.append(f"Unknown or circular slice dependency: {dep}")
        if errors:
            raise StateError("Invalid slice handoff: " + "; ".join(errors))
        pending = [dep for dep in dependencies if manifest["slices"][dep]["status"] != "accepted"]
        packet = {
            **candidate,
            "status": "blocked" if pending else "ready",
            "blockers": [f"Waiting for accepted dependency: {dep}" for dep in pending],
            "approval_id": active_id,
            "approval_mode": approval["mode"],
            "approved_artifact_hashes": approval["artifact_hashes"],
            "created_at": utc_now(),
        }
        target = directory / "slices" / slice_id
        target.mkdir(parents=True, exist_ok=False)
        atomic_json(target / "contract.json", packet)
        contract_hash = digest_of(target / "contract.json")
        manifest.setdefault("slices", {})[slice_id] = {
            "owner": candidate["owner"], "status": packet["status"],
            "prototype_version": packet["prototype_version"],
            "allowed_paths": paths, "dependencies": dependencies,
            "contract_file": f"slices/{slice_id}/contract.json",
            "contract_sha256": contract_hash, "blockers": packet["blockers"],
        }
        journal(directory, {"type": "slice_created", "slice_id": slice_id, "owner": candidate["owner"], "status": packet["status"], "contract_sha256": contract_hash})
        manifest["stage"] = "sliced"
        manifest["next_action"] = "Dispatch ready slices with locked source and visual cases; hold blocked dependencies"
        note_progress(manifest, f"Created slice {slice_id} ({packet['status']})")
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Slice packet {slice_id}: {packet['status']} ({target / 'contract.json'})")


def cancel_slice(directory: Path, slice_id: str, reason: str) -> None:
    if not reason.strip():
        raise StateError("Cancelling a slice requires a written reason")
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        assignment = manifest.get("slices", {}).get(slice_id)
        if not assignment or assignment.get("status") == "cancelled":
            raise StateError(f"Slice is unknown or already cancelled: {slice_id}")
        prior = assignment["status"]
        journal(directory, {"type": "slice_cancelled", "slice_id": slice_id, "prior_status": prior, "reason": reason})
        assignment["status"] = "cancelled"
        assignment["blockers"] = [f"Cancelled: {reason}; existing code remains untouched"]
        manifest["next_action"] = "Review cancelled slice impact, reassign its paths if needed, and preserve old evidence"
        note_progress(manifest, f"Cancelled slice {slice_id} (was {prior}): {reason}")
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Cancelled {slice_id}; old contract and evidence retained")


def worker_report(directory: Path, slice_id: str, owner: str, report_file: str) -> None:
    manifest = manifest_at(directory)
    assigned = manifest.get("slices", {}).get(slice_id)
    if not assigned:
        raise StateError(f"Unknown slice: {slice_id}")
    if assigned.get("status") not in {"ready", "reported"}:
        raise StateError(f"Slice {slice_id} is {assigned.get('status')}; not ready for worker reporting")
    report = read_json(Path(report_file))
    allowed = {
        "slice_id", "owner", "status", "summary", "implementation_paths",
        "functional_evidence", "visual_evidence", "blockers", "next_action", "source_revision",
    }
    if set(report) - allowed:
        raise StateError("Worker reports cannot modify global approval, stage or manifest fields")
    if owner != assigned["owner"] or report.get("owner") != owner or report.get("slice_id") != slice_id:
        raise StateError("Worker identity and slice assignment must match exactly")
    if report.get("status") not in {"submitted", "blocked"} or not str(report.get("summary") or "").strip():
        raise StateError("Worker report needs status submitted/blocked and a summary")
    if not isinstance(report.get("implementation_paths"), list) or not all(
        isinstance(p, str) and any(ownership_overlaps(p, q) for q in assigned["allowed_paths"])
        for p in report["implementation_paths"]
    ):
        raise StateError("Implementation paths must stay within assigned ownership")
    target = directory / "slices" / slice_id
    with coordinator_lock(target):
        journal_report = {**report, "reported_at": utc_now()}
        journal_file = target / "worker-history.jsonl"
        with journal_file.open("ab") as handle:
            handle.write((json.dumps(journal_report, ensure_ascii=False) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        atomic_json(target / "worker-report.json", journal_report)
    print(f"Worker report saved locally for slice {slice_id}; coordinator reconciliation required")


def implementation_file(directory: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise StateError("Implemented code path must be nonempty")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise StateError(f"Implementation path must be relative to project: {relative!r}")
    project_root = directory.parents[2]
    try:
        actual = (project_root / raw).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StateError(f"Missing implementation file {relative}: {error}") from error
    if not actual.is_relative_to(project_root.resolve()) or not actual.is_file():
        raise StateError(f"Implementation file escapes project or is not a file: {relative!r}")
    return actual


def validate_verification(directory: Path, slice_id: str, manifest: dict, review: dict) -> tuple[dict, list[str]]:
    assignment = manifest["slices"][slice_id]
    packet = read_json(artifact_file(directory, assignment["contract_file"]))
    report = read_json(directory / "slices" / slice_id / "worker-report.json")
    approval = manifest["approvals"][manifest["active_approval_id"]]
    errors: list[str] = []
    if review.get("slice_id") != slice_id:
        errors.append("Verification slice_id does not match assigned slice")
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer == assignment["owner"]:
        errors.append("Independent reviewer required; worker cannot self-approve")
    if not isinstance(review.get("notes"), str) or not review["notes"].strip():
        errors.append("Review needs explicit human-visible comparison notes")
    approved_cases = {case["case_id"]: case for case in packet["visual_cases"]}
    visual = review.get("visual_evidence")
    if not isinstance(visual, list):
        visual = []
        errors.append("Verification needs visual evidence records")
    seen: set[str] = set()
    artifact_hashes: dict[str, str] = {}
    warning_case_ids: list[str] = []
    for item in visual:
        if not isinstance(item, dict):
            errors.append("Visual evidence must be a JSON object")
            continue
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or case_id not in approved_cases or case_id in seen:
            errors.append(f"Unknown or repeated visual case: {case_id!r}")
            continue
        seen.add(case_id)
        frozen_case = approved_cases[case_id]
        ref_hash = approval["artifact_hashes"].get(frozen_case["reference_file"])
        if item.get("reference_sha256") != ref_hash:
            errors.append(f"Visual case {case_id}: reference_sha256 does not match approved screenshot")
        result = item.get("result")
        if result == "WARNING":
            warning_case_ids.append(case_id)
            capture_warning = item.get("capture_warning")
            if not isinstance(capture_warning, dict):
                errors.append(f"Visual case {case_id}: WARNING needs an independently reviewed capture_warning record")
                continue
            if not str(capture_warning.get("reviewer_notes") or "").strip():
                errors.append(f"Visual case {case_id}: capture WARNING needs explicit reviewer notes")
            diagnostic_path = capture_warning.get("diagnosis_file")
            diagnosis = None
            if (not isinstance(diagnostic_path, str)
                    or not diagnostic_path.startswith(f"slices/{slice_id}/evidence/")
                    or not diagnostic_path.endswith(".json")):
                errors.append(f"Visual case {case_id}: capture diagnosis must be a JSON file in this slice's evidence directory")
            else:
                try:
                    diagnosis_file = artifact_file(directory, diagnostic_path)
                    diagnosis = read_json(diagnosis_file)
                    artifact_hashes[diagnostic_path] = digest_of(diagnosis_file)
                except (StateError, OSError, ValueError) as error:
                    errors.append(str(error))
            retries = capture_warning.get("retry_screenshots")
            if (not isinstance(retries, list) or len(retries) != 3
                    or any(not isinstance(path, str) for path in retries)
                    or len(set(map(str, retries))) != 3
                    or item.get("implementation_screenshot") in retries):
                errors.append(f"Visual case {case_id}: exactly three distinct clean-browser retry PNGs are required")
            else:
                for retry_path in retries:
                    if (not isinstance(retry_path, str)
                            or not retry_path.startswith(f"slices/{slice_id}/evidence/")
                            or not retry_path.endswith(".png")):
                        errors.append(f"Visual case {case_id}: invalid retry screenshot path")
                        continue
                    try:
                        path = artifact_file(directory, retry_path)
                        requested = frozen_case["viewport"]
                        if png_dimensions(path) != (requested["width"], requested["height"]):
                            errors.append(f"Visual case {case_id}: retry PNG viewport mismatch")
                        artifact_hashes[retry_path] = digest_of(path)
                    except (StateError, OSError) as error:
                        errors.append(str(error))
            if not isinstance(diagnosis, dict) or (
                diagnosis.get("case_id") != case_id
                or diagnosis.get("classification") != "capture_instability"
                or diagnosis.get("comparison_scope") != "approved_to_implementation"
                or diagnosis.get("visible_ui_defect") is not False
                or not str(diagnosis.get("investigation") or "").strip()
                or not isinstance(diagnosis.get("retry_different_pixels"), list)
                or len(diagnosis["retry_different_pixels"]) != 3
                or any(type(count) is not int or count <= 0 for count in diagnosis["retry_different_pixels"])
                or not isinstance(diagnosis.get("retry_context_ids"), list)
                or len(diagnosis["retry_context_ids"]) != 3
                or any(not isinstance(ctx, str) or not ctx.strip() for ctx in diagnosis["retry_context_ids"])
                or len(set(diagnosis["retry_context_ids"])) != 3
                or not str(diagnosis.get("browser_version") or "").strip()
            ):
                errors.append(f"Visual case {case_id}: diagnosis must confirm three distinct-context residual capture-only mismatches without a visible UI defect")
        elif result != "PASS":
            errors.append(f"Visual case {case_id}: result is {result or 'BLOCKED'}, not PASS or a documented capture WARNING")
        if item.get("tool") in {None, "", "none", "unavailable"} or not str(item.get("command") or "").strip():
            errors.append(f"Visual case {case_id}: working browser tool and command are required")
        if not str(item.get("notes") or "").strip():
            errors.append(f"Visual case {case_id}: reviewer must describe visual inspection")
        screenshot = item.get("implementation_screenshot")
        if not isinstance(screenshot, str) or not screenshot.startswith(f"slices/{slice_id}/evidence/") or not screenshot.endswith(".png"):
            errors.append(f"Visual case {case_id}: screenshot must be a PNG in this slice's evidence directory")
        else:
            try:
                path = artifact_file(directory, screenshot)
                actual_dims = png_dimensions(path)
                requested = frozen_case["viewport"]
                if actual_dims != (requested["width"], requested["height"]):
                    errors.append(f"Visual case {case_id}: implementation PNG viewport mismatch")
                artifact_hashes[screenshot] = digest_of(path)
            except (StateError, OSError) as error:
                errors.append(str(error))
    if seen != set(approved_cases):
        errors.append("Visual evidence does not cover every approved case: " + ", ".join(sorted(set(approved_cases) - seen)))

    functional = review.get("functional_evidence")
    if not isinstance(functional, list) or not functional:
        errors.append("Functional tests need results and real output files")
        functional = []
    functional_covered: set[str] = set()
    for item in functional:
        if not isinstance(item, dict):
            errors.append("Functional evidence must be a JSON object")
            continue
        if not all(str(item.get(k) or "").strip() for k in ("name", "command")) or item.get("result") != "PASS":
            errors.append(f"Functional check {item.get('name', 'unnamed')}: missing command or PASS")
        covers = item.get("covers", [])
        if not isinstance(covers, list) or any(not isinstance(c, str) for c in covers):
            errors.append("Functional evidence covers must be a list of contract check names")
        else:
            functional_covered.update(covers)
        evidence_path = item.get("output_file")
        if not isinstance(evidence_path, str) or not evidence_path.startswith(f"slices/{slice_id}/evidence/"):
            errors.append("Functional test output_file must be in this slice's evidence directory")
        else:
            try:
                artifact_hashes[evidence_path] = digest_of(artifact_file(directory, evidence_path))
            except (StateError, OSError) as error:
                errors.append(str(error))
    expected_checks = set(packet["functional_tests"])
    if not expected_checks.issubset(functional_covered):
        errors.append("Missing functional-test coverage: " + "; ".join(sorted(expected_checks - functional_covered)))

    source_hashes: dict[str, str] = {}
    for code_path in report.get("implementation_paths", []):
        try:
            source_hashes[code_path] = digest_of(implementation_file(directory, code_path))
        except (StateError, OSError) as error:
            errors.append(str(error))
    if not source_hashes:
        errors.append("Missing code-file hashes; cannot tie screenshots to implemented code")
    record = {
        **review,
        "reviewed_at": utc_now(),
        "approval_id": manifest["active_approval_id"],
        "prototype_version": manifest["approved_version"],
        "contract_sha256": assignment["contract_sha256"],
        "evidence_hashes": artifact_hashes,
        "implementation_hashes": source_hashes,
        "warning_case_ids": sorted(warning_case_ids),
        "status": "BLOCKED" if errors else ("WARNING" if warning_case_ids else "PASS"),
    }
    return record, errors


def warning_ids_match(decision: dict, expected: object) -> bool:
    ids = decision.get("warning_case_ids")
    return (isinstance(ids, list) and all(isinstance(item, str) for item in ids)
            and isinstance(expected, list) and all(isinstance(item, str) for item in expected)
            and sorted(ids) == sorted(expected))


def check_saved_verification(directory: Path, slice_id: str, assignment: dict, manifest: dict) -> list[str]:
    try:
        review = read_json(directory / "slices" / slice_id / "verification.json")
    except StateError as error:
        return [str(error)]
    errors: list[str] = []
    expected_statuses = {
        "verified": {"PASS"},
        "provisional": {"WARNING"},
        "accepted": {"PASS", "WARNING_USER_ACCEPTED"},
    }
    if (review.get("status") not in expected_statuses.get(assignment.get("status"), set())
            or review.get("approval_id") != manifest.get("active_approval_id")):
        errors.append(f"Slice {slice_id}: missing valid verification for active approval and slice status")
    visual_records = review.get("visual_evidence")
    if not isinstance(visual_records, list) or any(not isinstance(item, dict) for item in visual_records):
        errors.append(f"Slice {slice_id}: saved visual evidence must be a list of reviewed cases")
        visual_records = []
    warning_items = [item for item in visual_records if item.get("result") == "WARNING"]
    if any(not isinstance(item.get("case_id"), str) for item in warning_items):
        errors.append(f"Slice {slice_id}: malformed warning case ID")
    actual_warning_ids = sorted(item["case_id"] for item in warning_items if isinstance(item.get("case_id"), str))
    if actual_warning_ids != review.get("warning_case_ids", []) or (
        review.get("status") == "PASS" and actual_warning_ids
    ):
        errors.append(f"Slice {slice_id}: capture warnings cannot be silently promoted to a visual PASS")
    if review.get("status") == "WARNING_USER_ACCEPTED":
        decision = review.get("user_warning_acceptance")
        if (not isinstance(decision, dict) or decision.get("approved_by_user") is not True
                or decision.get("approval_id") != manifest.get("active_approval_id")
                or decision.get("slice_id") != slice_id
                or not warning_ids_match(decision, review.get("warning_case_ids"))
                or not str(decision.get("user_note") or "").strip()):
            errors.append(f"Slice {slice_id}: missing explicit user acceptance of the exact capture warnings")
    if review.get("status") in {"WARNING", "WARNING_USER_ACCEPTED"} and not review.get("warning_case_ids"):
        errors.append(f"Slice {slice_id}: capture warning must identify its visual cases")
    if review.get("contract_sha256") != assignment.get("contract_sha256"):
        errors.append(f"Slice {slice_id}: verification is for a different contract version")
    if not review.get("evidence_hashes") or not review.get("implementation_hashes"):
        errors.append(f"Slice {slice_id}: missing screenshot/functional and source hash evidence")
    for path, digest in review.get("evidence_hashes", {}).items():
        try:
            if digest_of(artifact_file(directory, path)) != digest:
                errors.append(f"sha256 mismatch in visual or functional evidence: {path}")
        except (StateError, OSError) as error:
            errors.append(str(error))
    for path, digest in review.get("implementation_hashes", {}).items():
        try:
            if digest_of(implementation_file(directory, path)) != digest:
                errors.append(f"sha256 mismatch in accepted implementation code: {path}")
        except (StateError, OSError) as error:
            errors.append(str(error))
    return errors


def check_initiative(directory: Path, manifest: dict) -> list[str]:
    errors = check_approval(directory, manifest)
    for slice_id, assignment in manifest.get("slices", {}).items():
        if assignment.get("status") != "cancelled" and assignment.get("prototype_version") != manifest.get("approved_version"):
            errors.append(f"Slice {slice_id}: references a superseded or revoked prototype")
        try:
            actual = digest_of(artifact_file(directory, assignment["contract_file"]))
            if actual != assignment["contract_sha256"]:
                errors.append(f"Slice {slice_id}: sha256 mismatch in handoff contract")
        except (StateError, OSError, KeyError) as error:
            errors.append(str(error))
        if assignment.get("status") in {"verified", "provisional", "accepted"}:
            errors.extend(check_saved_verification(directory, slice_id, assignment, manifest))
    return errors


def unblock_dependents(directory: Path, manifest: dict) -> None:
    """Release implementation dependencies, not final acceptance, after reviewed warnings."""
    for dependent_id, dependent in manifest["slices"].items():
        if dependent["status"] != "blocked" or not dependent["dependencies"]:
            continue
        if any(manifest["slices"][dep]["status"] not in {"accepted", "provisional"}
               for dep in dependent["dependencies"]):
            continue
        if any(not str(blocker).startswith("Waiting for accepted dependency:") for blocker in dependent["blockers"]):
            continue
        packet_path = artifact_file(directory, dependent["contract_file"])
        packet = read_json(packet_path)
        packet["status"], packet["blockers"] = "ready", []
        atomic_json(packet_path, packet)
        dependent["status"], dependent["blockers"] = "ready", []
        dependent["contract_sha256"] = digest_of(packet_path)
        journal(directory, {"type": "slice_unblocked", "slice_id": dependent_id,
                            "reason": "Dependencies accepted or provisionally verified with capture warnings"})


def reconcile_slice(directory: Path, slice_id: str, action: str, review_file: str | None = None) -> None:
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        assignment = manifest.get("slices", {}).get(slice_id)
        if not assignment:
            raise StateError(f"Unknown slice: {slice_id}")
        errors = check_initiative(directory, manifest)
        if assignment["prototype_version"] != manifest.get("approved_version"):
            errors.append("Slice references superseded prototype; reapproval and reslicing required")
        if errors:
            raise StateError("Cannot reconcile: " + "; ".join(errors))
        if action == "report":
            if assignment["status"] in {"verified", "provisional", "accepted"}:
                raise StateError("Cannot replace verification/acceptance with a later worker report")
            report = read_json(directory / "slices" / slice_id / "worker-report.json")
            if report.get("owner") != assignment["owner"] or report.get("slice_id") != slice_id:
                raise StateError("Worker report identity conflicts with coordinator assignment")
            assignment["status"] = "reported" if report["status"] == "submitted" else "blocked"
            assignment["blockers"] = report.get("blockers", [])
            journal(directory, {"type": "slice_reconciled", "slice_id": slice_id, "status": assignment["status"], "summary": report["summary"]})
            manifest["stage"] = "implement"
            manifest["next_action"] = "Verify reported slice against approved visual cases and affected behavior"
        elif action == "verify":
            if assignment["status"] not in {"reported", "blocked", "verified"}:
                raise StateError("Slice must first have a reconciled worker report")
            if not review_file:
                raise StateError("Verification requires --review-file with independently inspected visual and functional evidence")
            review = read_json(Path(review_file))
            verified, failures = validate_verification(directory, slice_id, manifest, review)
            if failures:
                assignment["status"] = "blocked"
                assignment["blockers"] = failures
                assignment["warning_case_ids"] = []
                manifest["stage"] = "verify"
                manifest["next_action"] = "Fix verification blockers, rerun visual/functional checks and submit review"
                journal(directory, {"type": "verification_blocked", "slice_id": slice_id, "errors": failures, "review": review})
                atomic_json(directory / "manifest.json", manifest)
                atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
                raise StateError("Verification BLOCKED: " + "; ".join(failures))
            atomic_json(directory / "slices" / slice_id / "verification.json", verified)
            assignment["status"] = "provisional" if verified["status"] == "WARNING" else "verified"
            assignment["blockers"] = []
            assignment["warning_case_ids"] = verified["warning_case_ids"]
            manifest["stage"] = "verify"
            if assignment["status"] == "provisional":
                manifest["next_action"] = ("Continue dependent implementation; capture warning cases "
                                           f"{', '.join(verified['warning_case_ids'])} require later explicit user review before final acceptance")
                journal(directory, {"type": "slice_provisional_capture_warning", "slice_id": slice_id,
                                    "warning_case_ids": verified["warning_case_ids"],
                                    "reviewer": verified["reviewer"], "verified_at": verified["reviewed_at"]})
                unblock_dependents(directory, manifest)
            else:
                manifest["next_action"] = "Coordinator: verify source and evidence hashes, then explicitly accept the slice"
                journal(directory, {"type": "slice_verified", "slice_id": slice_id, "reviewer": verified["reviewer"], "verified_at": verified["reviewed_at"]})
        elif action == "accept":
            if assignment["status"] != "verified":
                raise StateError("Slice must have PASS verification and independent reviewer before acceptance")
            failures = check_saved_verification(directory, slice_id, assignment, manifest)
            if failures:
                raise StateError("Verification became stale: " + "; ".join(failures))
            assignment["status"] = "accepted"
            assignment["blockers"] = []
            journal(directory, {"type": "slice_accepted", "slice_id": slice_id, "approved_version": manifest["approved_version"]})
            unblock_dependents(directory, manifest)
            manifest["stage"] = "integrate" if all(item["status"] == "accepted" for item in manifest["slices"].values()) else "implement"
            manifest["next_action"] = "Review accepted slice integration and remaining dependencies; do not merge/deploy without user authorization"
        elif action == "accept-warning":
            if assignment["status"] != "provisional":
                raise StateError("Only a provisional capture WARNING can receive explicit later user acceptance")
            if not review_file:
                raise StateError("Capture WARNING acceptance needs --review-file with the user's explicit decision")
            decision = read_json(Path(review_file))
            saved = read_json(directory / "slices" / slice_id / "verification.json")
            if (decision.get("approved_by_user") is not True
                    or decision.get("slice_id") != slice_id
                    or decision.get("approval_id") != manifest.get("active_approval_id")
                    or not warning_ids_match(decision, saved.get("warning_case_ids"))
                    or not str(decision.get("user_note") or "").strip()):
                raise StateError("Explicit user decision must name current approval, slice and exact warnings with a note")
            if check_saved_verification(directory, slice_id, assignment, manifest):
                raise StateError("Capture warning evidence or code changed; reverify before user acceptance")
            saved["status"] = "WARNING_USER_ACCEPTED"
            saved["user_warning_acceptance"] = {**decision, "accepted_at": utc_now()}
            atomic_json(directory / "slices" / slice_id / "verification.json", saved)
            assignment["status"], assignment["blockers"] = "accepted", []
            journal(directory, {"type": "capture_warning_user_accepted", "slice_id": slice_id,
                                "warning_case_ids": saved["warning_case_ids"], "decision": decision})
            unblock_dependents(directory, manifest)
            manifest["stage"] = "integrate" if all(item["status"] == "accepted" for item in manifest["slices"].values()) else "implement"
            manifest["next_action"] = "Review accepted slice integration and remaining dependencies; do not merge/deploy without user authorization"
        else:
            raise StateError(f"Unknown reconciliation action: {action}")
        note_progress(manifest, f"Slice {slice_id}: {assignment['status']} ({action})",
                      completed=f"Accepted {slice_id}" if action in {"accept", "accept-warning"} else None)
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Reconciled slice {slice_id}: {assignment['status']} ({action})")


def git_info(project_root: Path) -> tuple[str | None, str | None]:
    def run(*args):
        try:
            result = subprocess.run(["git", "-C", str(project_root), *args], capture_output=True, text=True)
        except OSError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None
    return run("rev-parse", "HEAD"), run("status", "--short")


def print_resume(directory: Path, project_root: Path) -> list[str]:
    manifest = manifest_at(directory)
    print(render_checkpoint(manifest))
    active = manifest.get("approvals", {}).get(manifest.get("active_approval_id"))
    if active:
        print("Approved visual cases (read images directly before work):")
        for case in active.get("visual_cases", []):
            viewport = case.get("viewport", {})
            print(f"- {case.get('case_id')}: {case.get('reference_file')} at {viewport.get('width')}x{viewport.get('height')} ({case.get('state_setup')})")
        print("Approved prototype source files:")
        for path in active.get("source_paths", []):
            print(f"- {path}")
    actual_head, actual_status = git_info(project_root)
    print(f"Current Git HEAD: {actual_head or 'unavailable (inspect code manually)'}")
    print(f"Current Git status: {actual_status or ('clean' if actual_status is not None else 'unavailable')}")
    recorded = manifest.get("provenance", {})
    if recorded.get("repository_head") and recorded["repository_head"] != actual_head:
        print("RECONCILE: Git HEAD differs from the recorded checkpoint; inspect changes before continuing")
    if recorded.get("worktree_status") and recorded["worktree_status"].strip() != (actual_status or ""):
        print("RECONCILE: Worktree status differs from the recorded checkpoint; inspect changes before continuing")
    errors = check_initiative(directory, manifest)
    for error in errors:
        print(f"BLOCKED: {error}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "init", "event", "checkpoint", "approve", "revoke-approval", "slice-create", "slice-cancel",
        "slice-report", "reconcile", "resume", "validate",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("--project-root", required=True)
        sub.add_argument("--initiative-id", required=True)
        if name == "init":
            sub.add_argument("--scope", choices=sorted(VALID_SCOPES), required=True)
            sub.add_argument("--next-action")
        if name == "event":
            sub.add_argument("--event-file", required=True)
        if name == "checkpoint":
            sub.add_argument("--checkpoint-file", required=True)
        if name == "approve":
            sub.add_argument("--approval-file", required=True)
        if name == "revoke-approval":
            sub.add_argument("--reason", required=True)
        if name == "slice-create":
            sub.add_argument("--contract-file", required=True)
        if name == "slice-cancel":
            sub.add_argument("--slice-id", required=True)
            sub.add_argument("--reason", required=True)
        if name == "slice-report":
            sub.add_argument("--slice-id", required=True)
            sub.add_argument("--owner", required=True)
            sub.add_argument("--report-file", required=True)
        if name == "reconcile":
            sub.add_argument("--slice-id", required=True)
            sub.add_argument("--action", choices=["report", "verify", "accept", "accept-warning"], required=True)
            sub.add_argument("--review-file")
    args = parser.parse_args(argv)
    try:
        directory = location(args.project_root, args.initiative_id)
        if args.command == "init":
            ensure_local_git_ignore(Path(args.project_root).resolve(), directory)
            initialize(directory, args.scope, args.next_action)
        elif args.command == "event":
            add_event(directory, args.event_file)
        elif args.command == "checkpoint":
            checkpoint(directory, args.checkpoint_file)
        elif args.command == "approve":
            approve(directory, args.approval_file)
        elif args.command == "revoke-approval":
            revoke_approval(directory, args.reason)
        elif args.command == "slice-create":
            create_slice(directory, args.contract_file)
        elif args.command == "slice-cancel":
            cancel_slice(directory, args.slice_id, args.reason)
        elif args.command == "slice-report":
            worker_report(directory, args.slice_id, args.owner, args.report_file)
        elif args.command == "reconcile":
            reconcile_slice(directory, args.slice_id, args.action, args.review_file)
        elif args.command == "resume":
            return 2 if print_resume(directory, Path(args.project_root).resolve()) else 0
        elif args.command == "validate":
            manifest = manifest_at(directory)
            errors = check_initiative(directory, manifest)
            if errors:
                raise StateError("; ".join(errors))
            if not manifest.get("active_approval_id"):
                print("NOT YET APPROVED: initiative state is valid, but no prototype is approved for implementation")
            else:
                print("OK: current approval, slice handoffs, and accepted evidence hashes are consistent")
        return 0
    except (OSError, ValueError, StateError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
