#!/usr/bin/env python3
"""Durable, project-local state for exact prototype-to-production handoffs."""

from __future__ import annotations

import argparse
import importlib.util
from types import SimpleNamespace
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
from html import escape as html_escape
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
VALID_STAGES = frozenset({"discover", "plan", "blueprint", "prototype", "approved", "sliced", "implement", "verify", "integrate"})
VALID_WORK_STATUSES = frozenset({"in_progress", "blocked", "waiting_user", "done"})
INITIATIVE_ID = re.compile(r"^[a-z][a-z0-9-]{2,60}$")
VERSION_ID = re.compile(r"^[a-z][a-z0-9-]{1,60}$")
SKILL_ROOT = Path(__file__).resolve().parents[1]
HIDDEN_ARTIFACT_ROOT = Path(".artifacts") / "prototype-to-production"
LEGACY_ARTIFACT_ROOT = Path("design") / "redesigns"


class StateError(Exception):
    """A stopped stage gate, never an implicit success."""


def initiative_mode(manifest: dict) -> str:
    mode = manifest.get("initiative_mode", "prototype")
    if mode not in {"blueprint", "prototype"}:
        raise StateError("Unknown initiative mode; reconcile explicitly")
    return mode


def blueprint_state(directory: Path, manifest: dict):
    # Load beside the CLI even when imported by path from an unrelated working directory.
    spec = importlib.util.spec_from_file_location("p2p_blueprint", SKILL_ROOT / "scripts/blueprint_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BlueprintState(SimpleNamespace(**globals()), directory, manifest)


def record_blueprint_command(directory: Path, command: str, input_file: str, project_root: Path) -> None:
    value = read_json(Path(input_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        if initiative_mode(manifest) != "blueprint":
            raise StateError("Blueprint commands require an explicitly initialized blueprint initiative")
        state = blueprint_state(directory, manifest)
        group = {"conversation-record":"conversation", "plan-record":"blueprint_plans",
                 "blueprint-record":"blueprints", "blueprint-coverage":"blueprint_coverage",
                 "discovery-record":"discovery_audits", "discovery-capture":"browser_captures"}[command]
        key = value.get({"conversation-record": "exchange_id", "discovery-record": "audit_id",
                         "discovery-capture": "capture_id"}.get(command, "version"))
        integrity_errors = state.check_records(recovering=(group,key))
        if integrity_errors:
            raise StateError("Reconcile protected record history first: " + "; ".join(integrity_errors))
        if command == "discovery-record":
            state.record_discovery(project_root, value)
        else:
            getattr(state, command.replace("-", "_"))(value)
    print(f"Recorded {command}")


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
    """Keep new and resumed Git-backed artifacts ignored without changing the index."""
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
    # A manifest-only check misses rules that expose prototype source or screenshots.
    targets = {directory / "manifest.json"}
    if directory.exists():
        targets.update(path for path in directory.rglob("*") if path.is_file())

    def is_ignored() -> bool:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
            input="\0".join(str(path) for path in targets) + "\0",
            capture_output=True, text=True,
        )
        return result.returncode == 0 and set(result.stdout.rstrip("\0").split("\0")) == {str(path) for path in targets}

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


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_text(path: Path, content: str) -> None:
    atomic_bytes(path, content.encode("utf-8"))


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def json_content(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def atomic_json(path: Path, payload: dict) -> None:
    atomic_text(path, json_content(payload))


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise StateError(f"Expected JSON object: {path}")
    return value


def manifest_at(directory: Path) -> dict:
    marker = directory / ".workflow-transition.json"
    if marker.is_file():
        command = read_json(marker).get("request", {}).get("command", "workflow transition")
        raise StateError(f"Interrupted {command}; retry its original command with the exact input")
    if not (directory / "manifest.json").exists():
        raise StateError(f"Initiative not initialized: {directory}. Run init first.")
    manifest = read_json(directory / "manifest.json")
    version = manifest.get("schema_version", 1)
    if type(version) is not int or version not in {1, 2, 3, 4}:
        raise StateError(f"Unsupported manifest schema version: {version!r}")
    return manifest


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


def project_source_file(project_root: Path, relative: str) -> Path:
    """Resolve an observed application-A source file without following escapes."""
    if not isinstance(relative, str) or not relative.strip():
        raise StateError("Discovery source path must be a nonempty project-relative string")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise StateError(f"Discovery source must stay inside the project: {relative!r}")
    try:
        path = (project_root / raw).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StateError(f"Missing application-A source file {relative!r}: {error}") from error
    if not path.is_relative_to(project_root.resolve()) or not path.is_file():
        raise StateError(f"Discovery source escapes the project or is not a file: {relative!r}")
    return path


def validate_discovery_audit(directory: Path, project_root: Path, candidate: dict,
                             previous: dict | None, *, blueprint: bool = False
                             ) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Validate genuine A evidence and an exhaustive final zero-new sweep."""
    errors: list[str] = []
    if not isinstance(candidate.get("audit_id"), str) or not VERSION_ID.fullmatch(candidate["audit_id"]):
        errors.append("Discovery requires a stable audit_id")
    if candidate.get("scope") not in VALID_SCOPES:
        errors.append("Discovery scope must be feature, page or app")
    routes = candidate.get("routes", [] if blueprint else None)
    if not isinstance(routes, list) or (not blueprint and not routes) or any(
        not isinstance(route, str) or not route.startswith("/") for route in routes
    ):
        errors.append("Discovery must enumerate concrete in-scope routes")
        routes = []
    if len(routes) != len(set(routes)):
        errors.append("Discovery routes must be unique")
    if candidate.get("unresolved") != []:
        errors.append("Unresolved application-A functionality must be investigated before prototype work")
    features = candidate.get("features")
    if not isinstance(features, list) or not features:
        errors.append("Discovery needs at least one source-backed application-A behavior")
        features = []
    indexed: dict[str, dict] = {}
    evidence_hashes: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    previous_features = {
        item["feature_id"]: item for item in previous.get("features", [])
    } if isinstance(previous, dict) else {}
    preserved_definitions = set()
    for item in features:
        if not isinstance(item, dict):
            errors.append("Discovery feature must be an object")
            continue
        feature_id = item.get("feature_id")
        if not isinstance(feature_id, str) or not VERSION_ID.fullmatch(feature_id) or feature_id in indexed:
            errors.append(f"Discovery feature IDs must be unique and stable: {feature_id!r}")
            continue
        indexed[feature_id] = item
        surface = item.get("surface", candidate.get("surface"))
        backend = blueprint and surface == "backend"
        prior_feature = previous_features.get(feature_id)
        historical_unchanged = prior_feature == item
        if historical_unchanged:
            preserved_definitions.add(feature_id)
        elif blueprint and prior_feature and surface in {"ui", "fullstack"}:
            old_evidence = prior_feature.get("browser_evidence")
            new_evidence = item.get("browser_evidence")
            if (isinstance(old_evidence, list) and isinstance(new_evidence, list)
                    and new_evidence[:len(old_evidence)] == old_evidence
                    and len(new_evidence) > len(old_evidence)
                    and {**prior_feature, "browser_evidence": new_evidence} == item):
                preserved_definitions.add(feature_id)
        route = item.get("route")
        if (route not in routes and not (backend and route is None)) or not str(item.get("behavior") or "").strip():
            errors.append(f"Feature {feature_id}: missing valid route or actual behavior")
        for field in ("states", "source_anchors", "browser_evidence"):
            if not isinstance(item.get(field), list) or not item[field]:
                errors.append(f"Feature {feature_id}: concrete {field} is required")
        roles = item.get("roles", [] if backend else None)
        if not isinstance(roles, list) or (not backend and not roles):
            errors.append(f"Feature {feature_id}: concrete roles are required")
        for field in ("roles", "states"):
            if isinstance(item.get(field), list) and any(not isinstance(label, str) or not label.strip() for label in item[field]):
                errors.append(f"Feature {feature_id}: {field} must be meaningful named values")
        for anchor in item.get("source_anchors", []) if isinstance(item.get("source_anchors"), list) else []:
            if not isinstance(anchor, dict) or not isinstance(anchor.get("contains"), str) or not anchor["contains"].strip():
                errors.append(f"Feature {feature_id}: source anchor needs a file and unique code text")
                continue
            if feature_id in preserved_definitions:
                # A was source-verified in a previous immutable audit, whose
                # hashes are still checked. Production code may since change.
                # Keep the old A source anchor as historical proof rather than
                # pretending it must still exist in the new implementation.
                continue
            try:
                path = project_source_file(project_root, anchor.get("path"))
                if anchor["contains"] not in path.read_text(encoding="utf-8"):
                    errors.append(f"Feature {feature_id}: source anchor not found in {anchor.get('path')}")
                else:
                    source_hashes[anchor["path"]] = digest_of(path)
            except (StateError, OSError, UnicodeError) as error:
                errors.append(f"Feature {feature_id}: {error}")
        has_valid_browser_png = False
        for relative in item.get("browser_evidence", []) if isinstance(item.get("browser_evidence"), list) else []:
            try:
                path = artifact_file(directory, relative)
                if not path.stat().st_size:
                    errors.append(f"Feature {feature_id}: empty browser evidence {relative}")
                if relative.endswith(".png"):
                    png_dimensions(path)
                    has_valid_browser_png = True
                evidence_hashes[relative] = digest_of(path)
            except (StateError, OSError) as error:
                errors.append(f"Feature {feature_id}: {error}")
        if (blueprint and surface in {"ui", "fullstack"}
                and not historical_unchanged and not has_valid_browser_png):
            errors.append(f"Feature {feature_id}: UI discovery requires at least one valid browser PNG capture")
    for feature_id in previous_features:
        if feature_id not in indexed:
            errors.append(f"Cannot remove previously discovered feature {feature_id}; past decisions are immutable")
        elif feature_id not in preserved_definitions:
            errors.append(f"Cannot overwrite previously discovered feature {feature_id}; record a user decision and a new addendum")
    new_ids = set(indexed) - set(previous_features)
    sweeps = candidate.get("sweeps")
    minimum_sweeps = 1 if previous else 2
    if not isinstance(sweeps, list) or len(sweeps) < minimum_sweeps:
        errors.append("Discovery requires an initial source/browser sweep and a full zero-new sweep; later reviews need at least one full zero-new sweep")
        sweeps = []
    found: list[str] = []
    seen_sweeps: set[str] = set()
    required_sources = {
        anchor["path"]
        for feature in features if isinstance(feature, dict)
        for anchor in feature.get("source_anchors", []) if isinstance(anchor, dict)
        and isinstance(anchor.get("path"), str)
    }
    for sweep in sweeps:
        if not isinstance(sweep, dict):
            errors.append("Each discovery sweep must be an object")
            continue
        sweep_id = sweep.get("sweep_id")
        if not isinstance(sweep_id, str) or not sweep_id.strip() or sweep_id in seen_sweeps:
            errors.append("Discovery sweep IDs must be distinct and named")
        else:
            seen_sweeps.add(sweep_id)
        checked_routes = sweep.get("routes_checked")
        if not isinstance(checked_routes, list) or any(not isinstance(item, str) for item in checked_routes) or not set(routes).issubset(set(checked_routes)):
            errors.append(f"Sweep {sweep_id}: every in-scope route must be revisited")
        checked_sources = sweep.get("source_files_checked")
        if not isinstance(checked_sources, list) or any(not isinstance(item, str) for item in checked_sources) or not required_sources.issubset(set(checked_sources)):
            errors.append(f"Sweep {sweep_id}: inspect all relevant A source files")
        new = sweep.get("new_feature_ids")
        if not isinstance(new, list) or any(not isinstance(item, str) for item in new):
            errors.append(f"Sweep {sweep_id}: new_feature_ids must be a list")
        else:
            found.extend(new)
    if sweeps and isinstance(sweeps[-1], dict) and sweeps[-1].get("new_feature_ids") != []:
        errors.append("One full discovery sweep with zero new features is required before returning")
    if len(found) != len(set(found)) or set(found) != new_ids:
        errors.append("Sweep discoveries must account exactly once for newly inventoried A features")
    return evidence_hashes, source_hashes, errors


def record_discovery(directory: Path, project_root: Path, audit_file: str) -> None:
    candidate = read_json(Path(audit_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        audits = manifest.setdefault("discovery_audits", {})
        audit_id = candidate.get("audit_id")
        if not isinstance(audit_id, str) or not VERSION_ID.fullmatch(audit_id):
            raise StateError("Discovery audit_id must be a stable lowercase identifier")
        if audit_id in audits:
            raise StateError("Discovery audit already exists; keep earlier evidence immutable")
        previous_id = manifest.get("active_discovery_id")
        previous = read_json(artifact_file(directory, audits[previous_id]["path"])) if previous_id else None
        evidence, sources, errors = validate_discovery_audit(directory, project_root, candidate, previous)
        if candidate.get("scope") != manifest.get("scope"):
            errors.append("Discovery scope must match the redesign initiative")
        resolutions = candidate.get("missing_report_resolutions", [])
        if not isinstance(resolutions, list):
            errors.append("Discovery missing_report_resolutions must be a list")
            resolutions = []
        resolved_ids: set[str] = set()
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                errors.append("Each missing-report resolution must be an object")
                continue
            report_id = resolution.get("report_id")
            report = manifest.get("missing_reports", {}).get(report_id) if isinstance(report_id, str) else None
            if (not isinstance(report_id, str) or report_id in resolved_ids or not report
                    or report.get("status") != "open"):
                errors.append(f"Unknown, repeated or already resolved missing report: {report_id!r}")
                continue
            resolved_ids.add(report_id)
            if (resolution.get("resolution") != "not_missing"
                    or resolution.get("confirmed_by_user") is not True
                    or not str(resolution.get("user_note") or "").strip()):
                errors.append(f"Missing report {report_id}: only explicit user-confirmed not_missing corrections can dismiss a report")
            paths = resolution.get("evidence_paths")
            if not isinstance(paths, list) or not paths:
                errors.append(f"Missing report {report_id}: dismissal requires fresh investigation evidence")
            for relative in paths if isinstance(paths, list) else []:
                try:
                    evidence[relative] = digest_of(artifact_file(directory, relative))
                except (StateError, OSError) as error:
                    errors.append(f"Missing report {report_id}: {error}")
        if errors:
            raise StateError("Discovery BLOCKED: " + "; ".join(errors))
        relative = f"discovery/audits/{audit_id}.json"
        if (directory / relative).exists():
            raise StateError("Refusing to overwrite an existing discovery audit")
        atomic_json(directory / relative, candidate)
        audits[audit_id] = {
            "path": relative, "sha256": digest_of(directory / relative),
            "evidence_hashes": evidence, "source_hashes_at_discovery": sources,
            "feature_ids": [item["feature_id"] for item in candidate["features"]],
        }
        manifest["active_discovery_id"] = audit_id
        previous_ids = {item["feature_id"] for item in previous["features"]} if previous else set()
        newly_found = set(audits[audit_id]["feature_ids"]) - previous_ids
        for report_id in resolved_ids:
            manifest["missing_reports"][report_id]["status"] = "dismissed"
            manifest["missing_reports"][report_id]["resolution_audit_id"] = audit_id
        active_approval = manifest.get("approvals", {}).get(manifest.get("active_approval_id"))
        original_audit_id = (active_approval.get("functionality_coverage") or {}).get("audit_id") if active_approval else None
        original_features = set(audits.get(original_audit_id, {}).get("feature_ids", []))
        uncovered_ids = set(audits[audit_id]["feature_ids"]) - original_features if active_approval else set()
        open_reports = [report_id for report_id, report in manifest.get("missing_reports", {}).items()
                        if report.get("status") == "open"]
        if active_approval and (uncovered_ids or open_reports):
            manifest["coverage_pending"] = True
            manifest["stage"] = "prototype"
            if newly_found:
                message = ("Discovered additional application-A behavior after prototype approval: "
                           + ", ".join(sorted(newly_found)))
                if message not in manifest["blockers"]:
                    manifest["blockers"].append(message)
            manifest["next_action"] = ("Completed a full discovery sweep. Repair missing A-to-B behaviors in NEW B source and "
                                       "verify a NEW version for user reapproval; preserve old decisions. "
                                       "Unconfirmed false alarms require explicit user-approved dismissal")
        elif active_approval and manifest.get("coverage_pending"):
            manifest["coverage_pending"] = False
            manifest["stage"] = next(
                (report.get("stage_before_report", "approved")
                 for report in manifest.get("missing_reports", {}).values()
                 if report.get("resolution_audit_id") == audit_id),
                "approved",
            )
            manifest["blockers"] = [item for item in manifest["blockers"]
                                    if not item.startswith(("Missing application-A functionality reported:",
                                                           "Discovered additional application-A behavior after prototype approval:"))]
            manifest["next_action"] = "All reports were explicitly corrected and a full zero-new A sweep completed; resume the unchanged approval"
        elif not active_approval:
            manifest["stage"] = "prototype"
            manifest["next_action"] = "Build prototype B from the frozen A functionality inventory, then record parity"
        journal(directory, {"type": "functionality_discovery_frozen", "audit_id": audit_id,
                            "new_feature_ids": sorted(newly_found), "source_hashes": sources,
                            "user_confirmed_resolutions": sorted(resolved_ids)})
        note_progress(manifest, f"Discovery {audit_id}: {len(candidate['features'])} behaviors; final sweep found zero new")
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Frozen application-A discovery {audit_id} ({len(candidate['features'])} source-backed behaviors)")


def report_missing_feature(directory: Path, report_file: str) -> None:
    """Protect a suspected postapproval A->B gap without rewriting any approval."""
    candidate = read_json(Path(report_file))
    request = {"command": "missing-feature", "input": candidate}
    with coordinator_lock(directory):
        if recover_workflow_transition(directory, request):
            return
        manifest = manifest_at(directory)
        mode = initiative_mode(manifest)
        if not manifest.get("active_approval_id"):
            raise StateError(f"Missing-feature recovery needs an existing approved {mode}")
        report_id = candidate.get("report_id")
        if not isinstance(report_id, str) or not VERSION_ID.fullmatch(report_id):
            raise StateError("Missing-feature report needs a stable report_id")
        if report_id in manifest.setdefault("missing_reports", {}):
            raise StateError("Missing-feature report already exists; do not overwrite its evidence")
        suspected = candidate.get("suspected_features")
        if (not isinstance(suspected, list) or not suspected
                or any(not isinstance(item, str) or not VERSION_ID.fullmatch(item) for item in suspected)
                or len(set(suspected)) != len(suspected)
                or not str(candidate.get("summary") or "").strip()):
            raise StateError("Missing-feature report needs a real summary and distinct suspected feature IDs")
        paths = candidate.get("evidence_paths")
        if not isinstance(paths, list) or not paths:
            raise StateError("Missing-feature report needs actual observation evidence")
        hashes: dict[str, str] = {}
        for relative in paths:
            path = artifact_file(directory, relative)
            if path.stat().st_size == 0:
                raise StateError(f"Missing-feature report has empty evidence: {relative}")
            hashes[relative] = digest_of(path)
        relative = f"discovery/missing/{report_id}.json"
        if (directory / relative).exists():
            raise StateError("Refusing to overwrite previous missing-feature evidence")
        manifest["missing_reports"][report_id] = {
            "path": relative, "sha256": hashlib.sha256(json_content(candidate).encode()).hexdigest(),
            "evidence_hashes": hashes, "status": "open",
            "discovery_at_report": manifest.get("active_discovery_id"),
            "approval_id": manifest["active_approval_id"],
            "stage_before_report": manifest.get("stage"),
            "suspected_features": suspected,
        }
        manifest["coverage_pending"] = True
        manifest["stage"] = "discover"
        blocker = f"Missing application-A functionality reported: {report_id} ({', '.join(suspected)})"
        if blocker not in manifest["blockers"]:
            manifest["blockers"].append(blocker)
        manifest["next_action"] = ("Investigate missing A functionality across affected UI, source, API, roles and "
                                   "states. Freeze a follow-up discovery with a complete zero-new sweep; "
                                   "then create and verify new B and seek explicit reapproval")
        if mode == "blueprint":
            manifest["next_action"] = "Re-audit affected source/API behavior, preserve earlier decisions, then freeze a new blueprint with full coverage and explicit user approval"
        event = {"type": "application_a_functionality_missing", "report_id": report_id,
                 "approval_id": manifest["active_approval_id"], "suspected_features": suspected}
        coverage = "A-to-blueprint" if mode == "blueprint" else "A-to-B"
        note_progress(manifest, f"Postapproval {coverage} gap {report_id}: investigation required")
        save_workflow_state(directory, manifest, transition=(request, [event], {relative: candidate}))
    print(f"Recorded missing application-A behavior {report_id}; old user approval unchanged, follow-up sweep REQUIRED")


def record_prototype_coverage(directory: Path, coverage_file: str) -> None:
    """Freeze exact A-feature -> B-interaction proof before prototype review."""
    candidate = read_json(Path(coverage_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        if initiative_mode(manifest) != "prototype":
            raise StateError("prototype-coverage requires prototype mode")
        audit_id = manifest.get("active_discovery_id")
        audits = manifest.get("discovery_audits", {})
        if not audit_id or audit_id not in audits:
            raise StateError("Functionality coverage BLOCKED: first freeze a source/browser application-A discovery")
        version = candidate.get("prototype_version")
        if not isinstance(version, str) or not VERSION_ID.fullmatch(version):
            raise StateError("Prototype functionality coverage needs a stable prototype_version")
        if candidate.get("audit_id") != audit_id:
            raise StateError("Prototype functionality coverage must use the latest A discovery audit")
        if version in manifest.setdefault("prototype_coverage", {}):
            raise StateError("Prototype functionality coverage is already frozen; use a new prototype version")
        audit = read_json(artifact_file(directory, audits[audit_id]["path"]))
        required = {item["feature_id"] for item in audit["features"]}
        for report_id, report in manifest.get("missing_reports", {}).items():
            if report.get("status") != "open":
                continue
            if audit_id == report.get("discovery_at_report"):
                raise StateError(f"Functionality coverage BLOCKED: missing report {report_id} still needs a full follow-up A sweep")
            if not set(report.get("suspected_features", [])).issubset(required):
                raise StateError(f"Functionality coverage BLOCKED: reported feature {report_id} is absent from A; seek explicit user correction")
        mappings = candidate.get("mappings")
        if not isinstance(mappings, list):
            raise StateError("Prototype needs a concrete mapping for every application-A behavior")
        seen: set[str] = set()
        errors: list[str] = []
        evidence_hashes: dict[str, str] = {}
        for entry in mappings:
            if not isinstance(entry, dict):
                errors.append("Prototype behavior mapping must be an object")
                continue
            feature_id = entry.get("feature_id")
            if not isinstance(feature_id, str) or feature_id not in required or feature_id in seen:
                errors.append(f"Unknown or repeated prototype feature mapping: {feature_id!r}")
                continue
            seen.add(feature_id)
            status = entry.get("status")
            if status not in {"PASS", "IMAGE_ONLY_EXCEPTION"}:
                errors.append(f"Feature {feature_id}: functionality must pass an interaction replay")
                continue
            sources = entry.get("prototype_source_paths")
            if status == "PASS" and (not isinstance(sources, list) or not sources):
                errors.append(f"Feature {feature_id}: B requires runnable source paths, not a visual imitation")
            for relative in sources if isinstance(sources, list) else []:
                if not isinstance(relative, str) or not relative.startswith(f"prototype/versions/{version}/"):
                    errors.append(f"Feature {feature_id}: source must belong to prototype version {version}")
                    continue
                try:
                    evidence_hashes[relative] = digest_of(artifact_file(directory, relative))
                except (StateError, OSError) as error:
                    errors.append(f"Feature {feature_id}: {error}")
            proofs = entry.get("test_evidence")
            if status == "PASS" and (not isinstance(proofs, list) or not proofs):
                errors.append(f"Feature {feature_id}: passing browser/interaction evidence is required")
            for relative in proofs if isinstance(proofs, list) else []:
                try:
                    proof = read_json(artifact_file(directory, relative))
                    if (proof.get("feature_id") != feature_id or proof.get("result") != "PASS"
                            or not str(proof.get("command") or "").strip()
                            or not isinstance(proof.get("observed_steps"), list)
                            or not proof["observed_steps"]):
                        errors.append(f"Feature {feature_id}: test proof must identify real exercised actions and PASS")
                    evidence_hashes[relative] = digest_of(artifact_file(directory, relative))
                except (StateError, OSError) as error:
                    errors.append(f"Feature {feature_id}: {error}")
        if seen != required:
            errors.append("Unmapped application-A functionality: " + ", ".join(sorted(required - seen)))
        if errors:
            raise StateError("Functionality coverage BLOCKED: " + "; ".join(errors))
        relative = f"coverage/versions/{version}.json"
        if (directory / relative).exists():
            raise StateError("Refusing to overwrite a previous prototype functionality contract")
        atomic_json(directory / relative, candidate)
        manifest["prototype_coverage"][version] = {
            "audit_id": audit_id, "path": relative,
            "sha256": digest_of(directory / relative),
            "evidence_hashes": evidence_hashes,
            "feature_ids": sorted(required),
            "exception_ids": [entry["feature_id"] for entry in mappings
                              if entry["status"] == "IMAGE_ONLY_EXCEPTION"],
        }
        manifest["stage"] = "prototype"
        manifest["next_action"] = ("Prototype parity evidence frozen. Present exact runnable B and screenshots "
                                   "for user review; no production implementation before explicit approval")
        journal(directory, {"type": "prototype_functionality_verified", "version": version,
                            "audit_id": audit_id, "feature_ids": sorted(required)})
        note_progress(manifest, f"Prototype {version}: all {len(required)} A behaviors mapped with evidence")
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Prototype {version} functionality coverage verified ({len(required)} A behaviors)")


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
    if initiative_mode(existing) == "blueprint":
        try:
            return blueprint_state(directory, existing).validate_approval(value), []
        except (StateError, KeyError, TypeError, ValueError) as error:
            return {}, [str(error)]
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
    parity = existing.get("prototype_coverage", {}).get(version)
    audit_id = existing.get("active_discovery_id")
    if not parity or not audit_id or parity.get("audit_id") != audit_id:
        errors.append("Mandatory A-to-B functionality coverage is missing or stale; run discovery-record and prototype-coverage before presenting approval")
    elif parity.get("exception_ids") and not (
        mode == "image-only" and value.get("image_only_approved") is True
        and str(value.get("image_only_reason") or "").strip()
    ):
        errors.append("Prototype has unverified functionality exceptions without explicit user-approved image-only mode")
    latest_features = set(existing.get("discovery_audits", {}).get(audit_id, {}).get("feature_ids", []))
    for report_id, report in existing.get("missing_reports", {}).items():
        if report.get("status") != "open":
            continue
        if report.get("discovery_at_report") == audit_id:
            errors.append(f"Missing report {report_id} requires a new full A discovery sweep before reapproval")
        if not set(report.get("suspected_features", [])).issubset(latest_features):
            errors.append(f"Missing report {report_id} must be inventoried in A or explicitly dismissed by the user")
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
              "replay_required": mode == "source",
              "functionality_coverage": {
                  "audit_id": audit_id,
                  "prototype_version": version,
                  "contract_sha256": parity.get("sha256"),
              } if parity else None}
    return frozen, errors


def check_approval(directory: Path, manifest: dict) -> list[str]:
    if initiative_mode(manifest) == "blueprint":
        return blueprint_state(directory, manifest).check_approval()
    approval_id = manifest.get("active_approval_id")
    if not approval_id:
        return ["No active approved prototype"] if manifest.get("stage") not in {"discover", "plan", "blueprint", "prototype"} else []
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


def journal_line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"


def journal(directory: Path, item: dict) -> None:
    encoded = journal_line({**item, "at": utc_now()}).encode("utf-8")
    with (directory / "decisions.jsonl").open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def recover_workflow_transition(directory: Path, request: dict, *, replay: bool = True) -> bool:
    """Finish a prepared write under the coordinator lock, without duplicating journal entries."""
    marker = directory / ".workflow-transition.json"
    if not marker.is_file():
        return False
    pending = read_json(marker)
    if pending["request"] != request:
        raise StateError(f"Interrupted {pending['request']['command']}; retry it with the exact original input")
    writes = pending["artifacts"] | {"manifest.json": pending["manifest"]}
    journal_path = directory / "decisions.jsonl"
    before = pending["before"]
    journal_bytes = journal_path.read_bytes()
    suffix = "".join(pending["lines"]).encode("utf-8")
    if hashlib.sha256(journal_bytes).hexdigest() == before["decisions.jsonl"]:
        append = True
    elif journal_bytes.endswith(suffix) and hashlib.sha256(journal_bytes[:-len(suffix)]).hexdigest() == before["decisions.jsonl"]:
        append = False
    else:
        raise StateError("Journal changed during interrupted workflow transition")
    for relative, value in writes.items():
        path = directory / relative
        if path.resolve() != directory.resolve() / relative:
            raise StateError(f"Invalid write path in interrupted transition: {relative}")
        old = digest_of(path) if path.exists() else None
        if old not in {before[relative], hashlib.sha256(json_content(value).encode()).hexdigest()}:
            raise StateError(f"File changed during interrupted workflow transition: {relative}")
    for relative, value in pending["artifacts"].items():
        atomic_json(directory / relative, value)
    if append:
        atomic_bytes(journal_path, journal_bytes + suffix)
    atomic_json(directory / "manifest.json", pending["manifest"])
    atomic_text(directory / "CHECKPOINT.md", render_checkpoint(pending["manifest"]))
    marker.unlink()
    sync_directory(directory)
    if replay:
        print(f"Recovered interrupted {request['command']} and preserved its journal entries")
    return True


def save_workflow_state(directory: Path, manifest: dict, *, slice_retry_cursor: dict | None = None,
                        transition: tuple[dict, list[dict], dict] | None = None) -> None:
    """Persist blueprint phase changes together with the active task position."""
    if initiative_mode(manifest) == "blueprint":
        task_id = manifest.get("active_work_id")
        work = manifest.get("work_items", {}).get(task_id)
        if work:
            phase = manifest["stage"]
            if work["status"] in {"waiting_user", "blocked"}:
                manifest["next_action"] = work["next_action"]
            next_action = manifest["next_action"]
            if (phase, next_action) != (work["phase"], work["next_action"]):
                updated = {**work, "phase": phase, "next_action": next_action,
                           "updated_at": utc_now(), "revision": work["revision"] + 1}
                if slice_retry_cursor is None:
                    event = {"type": "work_cursor_updated", "task": updated}
                    if transition is None:
                        journal(directory, event)
                    else:
                        transition[1].append(event)
                else:
                    expected = {**updated, "updated_at": slice_retry_cursor.get("updated_at")}
                    if slice_retry_cursor != expected:
                        raise StateError("Interrupted slice cursor differs from the expected transition")
                    updated = slice_retry_cursor
                manifest["work_items"][task_id] = updated
    if transition is not None:
        request, events, artifacts = transition
        paths = ["manifest.json", "decisions.jsonl", *artifacts]
        before = {relative: digest_of(directory / relative) if (directory / relative).exists() else None
                  for relative in paths}
        atomic_json(directory / ".workflow-transition.json", {
            "request": request, "manifest": manifest, "artifacts": artifacts, "before": before,
            "lines": [journal_line({**event, "at": utc_now()}) for event in events],
        })
        recover_workflow_transition(directory, request, replay=False)
        return
    atomic_json(directory / "manifest.json", manifest)
    atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))


def render_checkpoint(manifest: dict, *, resume_action: str | None = None) -> str:
    mode = initiative_mode(manifest)
    recent = manifest.get("last_checkpoint") or {}
    work = manifest.get("work_items", {}).get(manifest.get("active_work_id"))
    lines = [
        f"# Redesign checkpoint: {manifest['initiative_id']}", "",
        "This is a recovery index. The manifest, journal, approved files, and live repository are authoritative.",
        "", f"Scope: {manifest['scope']}", f"Stage: {manifest['stage']}",
        f"Approved {mode}: {manifest.get('approved_version') or 'none'}",
        f"Active approval: {manifest.get('active_approval_id') or 'none'}",
        f"Next action: {resume_action or manifest.get('next_action') or 'UNSET'}", "",
        "## Exact continuation cursor", "",
    ]
    if work:
        lines.extend([
            f"- Task: {work['task_id']}",
            f"- Phase: {work['phase']}",
            f"- Status: {'ANSWER RECORDED' if resume_action else work['status'].upper()}",
            f"- Last saved: {work.get('updated_at', 'unknown')}",
            f"- Resume action: {resume_action or work['next_action']}",
            "- Completed steps: " + ("; ".join(work.get("completed_steps", [])) or "none"),
            "- Pending steps: " + ("; ".join(work.get("pending_steps", [])) or "none"),
            "- Code paths: " + ("; ".join(work.get("code_paths", [])) or "none"),
            "- Evidence paths: " + ("; ".join(work.get("artifact_paths", [])) or "none"),
        ])
    else:
        lines.append("No unfinished task cursor; follow the next action above and start a work task before acting.")
    lines += ["",
        "## Recent progress", "",
        recent.get("summary") or "No checkpoint summary yet.", "",
        "## Completed", "",
    ]
    lines.extend(f"- {item}" for item in recent.get("completed", []))
    lines += ["", "## Blockers", ""]
    lines.extend(f"- {item}" for item in manifest.get("blockers", []))
    for slice_id, item in sorted(manifest.get("slices", {}).items()):
        if item.get("status") == "blocked" and not item.get("superseded_by"):
            lines.extend(f"- {slice_id}: {reason}" for reason in item.get("blockers", []))
    lines += ["", "## Slices", ""]
    for slice_id, item in sorted(manifest.get("slices", {}).items()):
        status = item.get("status", "unknown")
        if item.get("superseded_by"):
            status += f", superseded by {item['superseded_by']}"
        lines.append(f"- {slice_id}: {status} ({item.get('owner', 'unassigned')})")
    lines += ["", "## Functionality coverage", ""]
    lines.append(f"- Latest application-A audit: {manifest.get('active_discovery_id') or 'none'}")
    coverage_group = "blueprint_coverage" if mode == "blueprint" else "prototype_coverage"
    coverage_label = "Blueprint coverage" if mode == "blueprint" else "Prototype parity"
    lines.append(f"- {coverage_label} versions: {', '.join(sorted(manifest.get(coverage_group, {}))) or 'none'}")
    lines.append(f"- Additional A functionality pending review: {'YES — BLOCKED' if manifest.get('coverage_pending') else 'no'}")
    lines += ["", "## Capture warnings needing user review", ""]
    pending = [
        f"- {slice_id}: {', '.join(item.get('warning_case_ids') or [])} (provisional, NOT accepted); gallery: {item.get('warning_gallery_file', 'UNAVAILABLE')}"
        for slice_id, item in sorted(manifest.get("slices", {}).items())
        if item.get("status") == "provisional"
    ]
    lines.extend(pending or ["None pending."])
    lines += ["", "## Supplemental visual warnings and side-by-side reviews", ""]
    for warning_id, item in sorted(manifest.get("warnings", {}).items()):
        lines.append(f"- {warning_id}: {item.get('status')} ({item.get('comparison_scope')}); gallery: {item.get('gallery_file')}")
    if not manifest.get("warnings"):
        lines.append("None recorded.")
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


def initialize(directory: Path, scope: str, next_action: str | None, mode: str | None = None) -> None:
    errors = validate_scope(scope)
    if errors:
        raise StateError("; ".join(errors))
    with coordinator_lock(directory):
        if (directory / "manifest.json").exists():
            manifest = manifest_at(directory)
            if manifest.get("scope") != scope:
                raise StateError(f"Existing scope is {manifest.get('scope')!r}; refusing to overwrite")
            if mode is not None and mode != initiative_mode(manifest):
                raise StateError("Existing initiative mode cannot change; create a separate initiative")
            print(f"Existing initiative preserved: {directory}")
            return
        template = read_json(SKILL_ROOT / "assets/project/manifest.template.json")
        template.update({
            "initiative_mode": mode or "blueprint",
            "schema_version": 4,
            "initiative_id": directory.name,
            "scope": scope,
            "stage": "discover",
            "next_action": next_action or template["next_action"],
        })
        template["provenance"]["created_at"] = utc_now()
        folders = ("plans", "blueprints", "conversation", "slices") if template["initiative_mode"] == "blueprint" else ("prototype/versions", "references", "fixtures", "slices")
        for subdirectory in folders:
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
        active = manifest.get("work_items", {}).get(manifest.get("active_work_id"))
        if active and "next_action" in value and value["next_action"] != active.get("next_action"):
            raise StateError("An active task owns its next action; use work-update to change the continuation cursor")
        if active and active.get("status") == "blocked" and "blockers" in value:
            prefix = f"Work {active['task_id']}:"
            expected = {f"{prefix} {reason}" for reason in active["blockers"]}
            if not isinstance(value["blockers"], list) or not expected.issubset(value["blockers"]):
                raise StateError("Cannot hide a blocked task by clearing checkpoint blockers; use work-update")
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
        active = manifest.get("work_items", {}).get(manifest.get("active_work_id"))
        if active and "next_action" in value and value["next_action"] != active.get("next_action"):
            raise StateError("An active task owns its next action; use work-update to change the continuation cursor")
        if active and active.get("status") == "blocked" and "blockers" in value:
            prefix = f"Work {active['task_id']}:"
            expected = {f"{prefix} {reason}" for reason in active["blockers"]}
            if not isinstance(value["blockers"], list) or not expected.issubset(value["blockers"]):
                raise StateError("Cannot hide a blocked task by clearing checkpoint blockers; use work-update")
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


def work_update(directory: Path, project_root: Path, work_file: str) -> None:
    """Persist an exact restart cursor before/after each meaningful work step."""
    value = read_json(Path(work_file))
    required = {"task_id", "phase", "status", "summary", "next_action",
                "completed_steps", "pending_steps", "blockers", "code_paths", "artifact_paths"}
    if set(value) - {"pending_question_id", "absent_code_paths"} != required:
        raise StateError("Work cursor needs exactly: " + ", ".join(sorted(required)))
    task_id = value["task_id"]
    if not isinstance(task_id, str) or not INITIATIVE_ID.fullmatch(task_id):
        raise StateError("Task ID must be a stable lowercase identifier")
    if (not isinstance(value["phase"], str) or value["phase"] not in VALID_STAGES
            or not isinstance(value["status"], str) or value["status"] not in VALID_WORK_STATUSES):
        raise StateError("Work cursor needs an actual redesign phase and recognized status")
    for field in ("summary", "next_action"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise StateError(f"Work cursor requires a concrete {field}")
    for field in ("completed_steps", "pending_steps", "blockers", "code_paths", "artifact_paths", "absent_code_paths"):
        entries = value.get(field, [])
        if (not isinstance(entries, list) or any(not isinstance(item, str) or not item.strip() for item in entries)
                or len(entries) != len(set(entries))):
            raise StateError(f"Work cursor {field} must be a list of distinct, meaningful strings")
    if value["status"] == "done" and (value["pending_steps"] or value["blockers"]):
        raise StateError("A task cannot be done with pending steps or blockers")
    if value["status"] == "blocked" and not value["blockers"]:
        raise StateError("Blocked work needs a recorded concrete blocker")
    if value["status"] != "blocked" and value["blockers"]:
        raise StateError("Only blocked work may have unresolved task blockers")
    if value["status"] != "done" and not value["pending_steps"]:
        raise StateError("Unfinished work needs an explicit pending step for resumption")
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        blueprint = initiative_mode(manifest) == "blueprint"
        if value["phase"] in {"plan", "blueprint"} and not blueprint:
            raise StateError("Plan/blueprint phases require blueprint mode")
        if blueprint and value["phase"] != manifest["stage"]:
            raise StateError(f"Set the work phase to the recorded blueprint stage {manifest['stage']}")
        if not blueprint and value.get("absent_code_paths"):
            raise StateError("Expected source absence requires blueprint mode")
        question_id = value.get("pending_question_id")
        if blueprint and value["status"] == "waiting_user" and not question_id:
            raise StateError("Blueprint waiting_user requires pending_question_id linking the exact open exchange")
        if question_id is not None:
            if not isinstance(question_id, str) or not question_id.strip():
                raise StateError("Pending question ID must be a nonempty exchange ID")
            pending = blueprint_state(directory, manifest).pending()
            if value["pending_question_id"] not in {q["exchange_id"] for q in pending}:
                raise StateError("Pending question ID is unknown or already answered")
            if value["status"] != "waiting_user":
                raise StateError("Pending question requires waiting_user status")
        items = manifest.setdefault("work_items", {})
        existing = items.get(task_id)
        if (blueprint and existing and existing.get("pending_question_id")
                and existing["pending_question_id"] != question_id):
            state = blueprint_state(directory, manifest)
            answer, context = state.question_context(existing["pending_question_id"], state.conversations())
            if not answer:
                raise StateError("The saved consequential question is still unanswered")
            source_errors = state.check_question_sources(project_root, [context])
            if source_errors:
                raise StateError("Reconcile the saved question source before advancing: " + "; ".join(source_errors))
        active_id = manifest.get("active_work_id")
        if active_id and active_id != task_id:
            raise StateError(f"Another task is active ({active_id}); finish it before starting {task_id}")
        if existing and existing["status"] == "done":
            raise StateError("Completed task history cannot be rewritten; use a new task ID")
        if existing:
            if not blueprint and value["phase"] != existing["phase"]:
                raise StateError("An existing work task cannot silently change phase")
            old_completed = set(existing["completed_steps"])
            old_pending = set(existing["pending_steps"])
            if not old_completed.issubset(value["completed_steps"]):
                raise StateError("Previously completed task steps cannot be forgotten")
            if not old_pending.issubset(set(value["completed_steps"]) | set(value["pending_steps"])):
                raise StateError("Previously pending task steps cannot disappear without being completed")
        elif value["phase"] != manifest.get("stage"):
            raise StateError(f"New work phase {value['phase']} conflicts with current stage {manifest.get('stage')}")
        code_paths = sorted(set(value["code_paths"]) | set(existing.get("code_paths", []) if existing else []))
        artifact_paths = sorted(set(value["artifact_paths"]) | set(existing.get("artifact_paths", []) if existing else []))
        absent = set(value.get("absent_code_paths", ()))
        if not absent <= set(existing.get("code_paths", []) if existing else ()) or absent.intersection(value["code_paths"]):
            raise StateError("Declare expected absence separately for previously recorded code paths")
        code_hashes = {
            path: work_source_hash(directory, path, expected_absent=path in absent)
            for path in code_paths
        }
        artifact_hashes = {path: digest_of(artifact_file(directory, path)) for path in artifact_paths}
        repository_head, worktree_status = git_info(project_root)
        recorded = {**value, "code_paths": code_paths, "artifact_paths": artifact_paths,
                    "code_hashes": code_hashes, "artifact_hashes": artifact_hashes,
                    "repository_head": repository_head, "worktree_status": worktree_status,
                    "created_at": existing["created_at"] if existing else utc_now(),
                    "updated_at": utc_now(), "revision": existing["revision"] + 1 if existing else 1}
        items[task_id] = recorded
        manifest["active_work_id"] = None if value["status"] == "done" else task_id
        manifest["next_action"] = value["next_action"]
        prefix = f"Work {task_id}:"
        manifest["blockers"] = [item for item in manifest.get("blockers", []) if not item.startswith(prefix)]
        if value["status"] == "blocked":
            manifest["blockers"].extend(f"{prefix} {reason}" for reason in value["blockers"])
        journal(directory, {"type": "work_cursor_updated", "task": recorded})
        note_progress(manifest, f"Task {task_id}: {value['status']} — {value['summary']}",
                      completed=f"Finished task {task_id}" if value["status"] == "done" else None)
        atomic_json(directory / "manifest.json", manifest)
        atomic_text(directory / "CHECKPOINT.md", render_checkpoint(manifest))
    print(f"Work cursor {task_id}: {value['status']} (revision {recorded['revision']})")


def approve(directory: Path, approval_file: str) -> None:
    candidate = read_json(Path(approval_file))
    request = {"command": "approve", "input": candidate}
    with coordinator_lock(directory):
        if recover_workflow_transition(directory, request):
            return
        manifest = manifest_at(directory)
        approval, errors = validate_approval_record(directory, candidate, manifest)
        if errors:
            raise StateError("Approval invalid: " + "; ".join(errors))
        mode = initiative_mode(manifest)
        previous = manifest.get("active_approval_id")
        retired = []
        if mode == "blueprint":
            for slice_id, assignment in manifest.get("slices", {}).items():
                if assignment["status"] != "cancelled" and not assignment.get("superseded_by"):
                    assignment["superseded_by"] = approval["decision_id"]
                    retired.append(slice_id)
        event = {
            "type": "approval", "decision_id": approval["decision_id"],
            "approved_version": approval["approved_version"], "supersedes": previous,
            "approval": approval,
            **({"superseded_slice_ids": retired} if retired else {}),
        }
        manifest["approvals"][approval["decision_id"]] = approval
        for report in manifest.get("missing_reports", {}).values():
            if report.get("status") == "open":
                report["status"] = "addressed"
                report["addressed_by_approval"] = approval["decision_id"]
        manifest.update({
            "active_approval_id": approval["decision_id"],
            "approved_version": approval["approved_version"],
            "stage": "approved",
            "references": approval["visual_cases"],
            "blockers": [
                item for item in manifest.get("blockers", [])
                if not item.startswith(("Missing application-A functionality reported:",
                                        "Discovered additional application-A behavior after prototype approval:"))
                and not (mode == "blueprint" and item.startswith("Approval ")
                         and item.endswith("; implementation is paused"))
            ],
            "coverage_pending": False,
            "next_action": "Create complete slice contracts grounded in the locked blueprint" if initiative_mode(manifest) == "blueprint" else "Create complete slice contracts grounded in the locked prototype",
        })
        note_progress(manifest, f"Approved {mode} {approval['approved_version']}, decision {approval['decision_id']}",
                      completed=f"Frozen {mode} {approval['approved_version']}")
        save_workflow_state(directory, manifest, transition=(request, [event], {}))
    print(f"Approved and hash-locked {approval['approved_version']} ({approval['decision_id']})")


def revoke_approval(directory: Path, reason: str) -> None:
    if not reason.strip():
        raise StateError("A written reason is required to revoke approval")
    request = {"command": "revoke-approval", "reason": reason}
    with coordinator_lock(directory):
        if recover_workflow_transition(directory, request):
            return
        manifest = manifest_at(directory)
        active = manifest.get("active_approval_id")
        if not active:
            raise StateError("No active approval to revoke")
        event = {"type": "approval_revoked", "decision_id": active, "reason": reason}
        manifest.update({
            "active_approval_id": None, "approved_version": None, "stage": "blueprint" if initiative_mode(manifest) == "blueprint" else "prototype",
            "references": [], "next_action": "Revise blueprint and seek explicit new approval" if initiative_mode(manifest) == "blueprint" else "Revise prototype and seek explicit new approval",
            "blockers": [f"Approval {active} revoked: {reason}; implementation is paused", *(
                item for item in manifest.get("blockers", [])
                if initiative_mode(manifest) == "blueprint" and item.startswith("Work ")
            )],
        })
        note_progress(manifest, f"Revoked approval {active}: {reason}")
        save_workflow_state(directory, manifest, transition=(request, [event], {}))
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


def ownership_contains(allowed: str, requested: str) -> bool:
    parent, subtree = owned_path(allowed)
    child, requested_tree = owned_path(requested)
    return (subtree and child[:len(parent)] == parent) or (parent == child and not requested_tree)


def create_slice(directory: Path, contract_file: str) -> None:
    candidate = read_json(Path(contract_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        approval_errors = check_approval(directory, manifest) + check_coverage_records(directory, manifest)
        if approval_errors:
            raise StateError("Cannot assign slice: " + "; ".join(approval_errors))
        active_id = manifest.get("active_approval_id")
        if not active_id:
            raise StateError("Cannot create a slice without active approval")
        approval = manifest["approvals"][active_id]
        if initiative_mode(manifest) == "blueprint":
            blueprint_state(directory, manifest).create_slice(candidate)
            return
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
        pending = [dep for dep in dependencies if manifest["slices"][dep]["status"] not in {"accepted", "provisional"}]
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
        if not assignment or assignment.get("status") == "cancelled" or assignment.get("superseded_by"):
            raise StateError(f"Slice is unknown or already cancelled: {slice_id}")
        prior = assignment["status"]
        journal(directory, {"type": "slice_cancelled", "slice_id": slice_id, "prior_status": prior, "reason": reason})
        assignment["status"] = "cancelled"
        assignment["blockers"] = [f"Cancelled: {reason}; existing code remains untouched"]
        manifest["next_action"] = "Review cancelled slice impact, reassign its paths if needed, and preserve old evidence"
        note_progress(manifest, f"Cancelled slice {slice_id} (was {prior}): {reason}")
        save_workflow_state(directory, manifest)
    print(f"Cancelled {slice_id}; old contract and evidence retained")


def worker_report(directory: Path, slice_id: str, owner: str, report_file: str) -> None:
    manifest = manifest_at(directory)
    assigned = manifest.get("slices", {}).get(slice_id)
    if not assigned:
        raise StateError(f"Unknown slice: {slice_id}")
    if assigned.get("superseded_by"):
        raise StateError(f"Slice {slice_id} was superseded by {assigned['superseded_by']}")
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
    if manifest.get("coverage_pending") and report.get("status") != "blocked":
        raise StateError("New application-A functionality is pending investigation; implementation reports cannot claim completion")
    blueprint = initiative_mode(manifest) == "blueprint"
    if blueprint and report["status"] == "submitted":
        if assigned.get("blueprint_version") != manifest.get("approved_version"):
            raise StateError("Slice must target the active blueprint approval")
        errors = check_approval(directory, manifest)
        if errors:
            raise StateError("Cannot submit blueprint slice: " + "; ".join(errors))
    if not isinstance(report.get("implementation_paths"), list) or not all(
        isinstance(p, str) and any(
            ownership_contains(q, p) if blueprint else ownership_overlaps(p, q)
            for q in assigned["allowed_paths"])
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


def implementation_file(directory: Path, relative: str, *, allow_missing: bool = False) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        raise StateError("Implemented code path must be nonempty")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise StateError(f"Implementation path must be relative to project: {relative!r}")
    project_root = directory.parents[2]
    path = project_root / raw
    try:
        if allow_missing:
            try:
                path.lstat()
            except FileNotFoundError:
                if path.resolve().is_relative_to(project_root.resolve()):
                    return None
        actual = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StateError(f"Missing implementation file {relative}: {error}") from error
    if not actual.is_relative_to(project_root.resolve()) or not actual.is_file():
        raise StateError(f"Implementation file escapes project or is not a file: {relative!r}")
    return actual


def work_source_hash(directory: Path, path: str, *, expected_absent: bool) -> str | None:
    if expected_absent:
        if implementation_file(directory, path, allow_missing=True) is not None:
            raise StateError(f"Recorded deleted task source reappeared: {path}")
        return None
    return digest_of(project_source_file(directory.parents[2], path))


def validate_verification(directory: Path, slice_id: str, manifest: dict, review: dict) -> tuple[dict, list[str]]:
    if initiative_mode(manifest) == "blueprint":
        return blueprint_state(directory, manifest).verify(slice_id, review, execute=True)
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
    if initiative_mode(manifest) == "blueprint":
        return blueprint_state(directory, manifest).check_saved_verification(slice_id, assignment)
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


def warning_gallery(directory: Path, warning_dir: Path, report: dict) -> str:
    """Portable, dependency-free side-by-side proof, never a production UI."""
    scope_labels = {
        "approved_to_implementation": ("Before / approved prototype", "After / implemented page"),
        "original_before_to_original_after": ("Before / original baseline", "After / post-change capture"),
        "prototype_replay": ("Before / prototype reference", "After / prototype replay"),
    }
    before_label, after_label = scope_labels[report["comparison_scope"]]

    def linked_image(relative: str, label: str) -> str:
        url = html_escape(os.path.relpath(directory / relative, warning_dir).replace(os.sep, "/"), quote=True)
        safe_label = html_escape(label, quote=True)
        return f'<a href="{url}" target="_blank"><img loading="lazy" src="{url}" alt="{safe_label}"></a>'

    cards = []
    for item in report["comparison_pairs"]:
        first, second = item["reference_file"], item["actual_file"]
        width, height = png_dimensions(artifact_file(directory, first))
        heading = f'{html_escape(item["case_id"])} — {html_escape(item["attempt_id"])} ({width} × {height})'
        cards.append(
            '<section class="comparison-pair">'
            f'<h2>{heading}</h2><p>Changed pixels reported: {html_escape(str(item["different_pixels"]))}</p>'
            '<div class="images">'
            f'<figure><figcaption>{before_label}</figcaption>{linked_image(first, before_label)}</figure>'
            f'<figure><figcaption>{after_label}</figcaption>{linked_image(second, after_label)}</figure>'
            '</div></section>'
        )
    title = html_escape(report["warning_id"])
    scope = html_escape(report["comparison_scope"])
    summary = html_escape(report["summary"])
    notes = html_escape(report["reviewer_notes"])
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Visual warning: {title}</title><style>'
            'body{font:15px/1.55 system-ui,sans-serif;max-width:1500px;margin:0 auto;padding:24px;color:#182333;background:#f4f6fa}'
            'h1{margin-bottom:4px}header,.comparison-pair{background:white;border:1px solid #dce2e9;border-radius:12px;padding:18px;margin-bottom:18px}'
            '.images{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}'
            'figure{min-width:0;margin:0}figcaption{font-weight:600;margin-bottom:8px}'
            'img{display:block;max-width:100%;height:auto;border:1px solid #ccd3dd;border-radius:5px}'
            'a{display:block}p{overflow-wrap:anywhere}small{color:#4c596b}'
            '@media(max-width:700px){.images{grid-template-columns:1fr}body{padding:10px}}'
            '</style></head><body>'
            f'<header><h1>Visual warning: {title}</h1><p>{summary}</p>'
            f'<small>Comparison: {scope}. Reviewer notes: {notes}</small></header>'
            + "".join(cards) + '</body></html>\n')


def record_warning(directory: Path, input_file: str) -> None:
    warning = read_json(Path(input_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        errors = check_initiative(directory, manifest)
        warning_id = warning.get("warning_id")
        slice_id = warning.get("slice_id")
        assignment = manifest.get("slices", {}).get(slice_id) if isinstance(slice_id, str) else None
        if not isinstance(warning_id, str) or not INITIATIVE_ID.fullmatch(warning_id):
            errors.append("Warning requires a unique lowercase warning_id")
        if isinstance(warning_id, str) and warning_id in manifest.get("warnings", {}):
            errors.append("Warning ID already exists; evidence must never be overwritten")
        if not assignment or assignment.get("status") == "cancelled":
            errors.append("Warning must name an existing, active slice")
        if warning.get("classification") != "capture_instability":
            errors.append("Only independently investigated capture instability can be a visual WARNING; real UI defects must BLOCK")
        if not isinstance(warning.get("comparison_scope"), str) or warning.get("comparison_scope") not in {
            "approved_to_implementation", "original_before_to_original_after", "prototype_replay"
        }:
            errors.append("Warning needs the correct explicit comparison_scope")
        disposition = warning.get("disposition")
        if not isinstance(disposition, str) or disposition not in {"needs_user_review", "informational"}:
            errors.append("Warning disposition must be needs_user_review or informational")
        if assignment and assignment.get("status") == "accepted" and disposition == "needs_user_review":
            errors.append("A pending warning cannot be added after slice acceptance; record it before accepting")
        if not isinstance(warning.get("summary"), str) or not warning["summary"].strip():
            errors.append("Warning needs a clear summary")
        if (not isinstance(warning.get("reviewer"), str) or not warning["reviewer"].strip()
                or assignment and warning["reviewer"] == assignment["owner"]):
            errors.append("Warning requires an independent reviewer")
        if not isinstance(warning.get("reviewer_notes"), str) or not warning["reviewer_notes"].strip():
            errors.append("Warning requires independent visual-review notes")
        contexts = warning.get("retry_context_ids")
        if (not isinstance(contexts, list) or len(contexts) != 3
                or any(not isinstance(value, str) or not value.strip() for value in contexts)
                or len(set(contexts)) != 3):
            errors.append("Warning needs exactly three distinct clean-browser retry context IDs")
            contexts = []
        pairs = warning.get("comparison_pairs")
        if not isinstance(pairs, list) or len(pairs) < 3:
            errors.append("Warning needs three or more actual before/after screenshot pairs")
            pairs = []
        seen_pairs: set[tuple[str, str]] = set()
        seen_actual_paths: set[str] = set()
        used_contexts: set[str] = set()
        evidence_hashes: dict[str, str] = {}
        any_difference = False
        for item in pairs:
            if not isinstance(item, dict):
                errors.append("Each comparison pair must be an object")
                continue
            case_id, attempt = item.get("case_id"), item.get("attempt_id")
            before, after = item.get("reference_file"), item.get("actual_file")
            if (not isinstance(case_id, str) or not case_id.strip()
                    or not isinstance(attempt, str) or attempt not in contexts):
                errors.append("Comparison pair needs a case_id and one of the named clean retry contexts")
                continue
            if (case_id, attempt) in seen_pairs or not isinstance(after, str) or after in seen_actual_paths or before == after:
                errors.append("Screenshot pairs must have distinct paths and unique case/context combinations")
                continue
            seen_pairs.add((case_id, attempt))
            used_contexts.add(attempt)
            seen_actual_paths.add(after)
            changed = item.get("different_pixels")
            if type(changed) is not int or changed < 0:
                errors.append("Comparison pair needs a nonnegative integer changed-pixel count")
                continue
            any_difference |= changed > 0
            try:
                before_path = artifact_file(directory, before)
                after_path = artifact_file(directory, after)
                if png_dimensions(before_path) != png_dimensions(after_path):
                    errors.append(f"Comparison pair {case_id} has different screenshot dimensions")
                evidence_hashes[before] = digest_of(before_path)
                evidence_hashes[after] = digest_of(after_path)
            except (StateError, OSError) as error:
                errors.append(str(error))
        if set(contexts) != used_contexts:
            errors.append("Every clean retry context must have a saved screenshot comparison")
        if disposition == "needs_user_review" and not any_difference:
            errors.append("A pending capture WARNING must show actual residual differences; exact retries are informational")
        supporting = warning.get("supporting_files")
        if not isinstance(supporting, list) or not supporting or any(not isinstance(p, str) for p in supporting):
            errors.append("Warning needs actual saved investigation files")
            supporting = []
        for path in supporting:
            try:
                evidence_hashes[path] = digest_of(artifact_file(directory, path))
            except (StateError, OSError) as error:
                errors.append(str(error))
        if errors:
            raise StateError("Cannot record visual warning: " + "; ".join(errors))
        warning_dir = directory / "warnings" / warning_id
        warning_dir.mkdir(parents=True, exist_ok=False)
        record_file = f"warnings/{warning_id}/record.json"
        gallery_file = f"warnings/{warning_id}/comparison.html"
        atomic_json(directory / record_file, {"warning": warning, "artifact_hashes": evidence_hashes,
                                              "approval_id": manifest.get("active_approval_id"), "recorded_at": utc_now()})
        atomic_text(directory / gallery_file, warning_gallery(directory, warning_dir, warning))
        evidence_hashes[record_file] = digest_of(directory / record_file)
        evidence_hashes[gallery_file] = digest_of(directory / gallery_file)
        manifest.setdefault("warnings", {})[warning_id] = {
            "slice_id": slice_id, "status": "pending_user_review" if disposition == "needs_user_review" else "informational",
            "comparison_scope": warning["comparison_scope"], "summary": warning["summary"],
            "approval_id": manifest.get("active_approval_id"), "record_file": record_file,
            "gallery_file": gallery_file, "evidence_hashes": evidence_hashes,
        }
        journal(directory, {"type": "visual_warning_recorded", "warning_id": warning_id,
                            "slice_id": slice_id, "status": manifest["warnings"][warning_id]["status"],
                            "gallery_file": gallery_file})
        manifest["next_action"] = (f"Review side-by-side gallery {gallery_file} before final acceptance"
                                   if disposition == "needs_user_review" else manifest.get("next_action"))
        note_progress(manifest, f"Saved visual warning {warning_id} ({manifest['warnings'][warning_id]['status']})")
        save_workflow_state(directory, manifest)
    print(f"Recorded visual warning {warning_id}: {gallery_file}")


def check_warning_evidence(directory: Path, manifest: dict) -> list[str]:
    errors: list[str] = []
    for warning_id, warning in manifest.get("warnings", {}).items():
        if warning.get("slice_id") not in manifest.get("slices", {}):
            errors.append(f"Visual warning {warning_id}: unknown slice")
        if warning.get("status") not in {"pending_user_review", "informational", "accepted", "rejected"}:
            errors.append(f"Visual warning {warning_id}: invalid status")
        evidence_hashes = warning.get("evidence_hashes")
        if (not isinstance(evidence_hashes, dict) or not evidence_hashes
                or warning.get("record_file") not in evidence_hashes
                or warning.get("gallery_file") not in evidence_hashes):
            errors.append(f"Visual warning {warning_id}: missing source, screenshot or side-by-side gallery hashes")
            continue
        for relative, digest in evidence_hashes.items():
            try:
                if digest_of(artifact_file(directory, relative)) != digest:
                    errors.append(f"Visual warning {warning_id}: sha256 mismatch in evidence {relative}")
            except (StateError, OSError) as error:
                errors.append(str(error))
        if warning.get("status") in {"accepted", "rejected"} and warning.get("decision_file") not in evidence_hashes:
            errors.append(f"Visual warning {warning_id}: missing hashed user decision")
        if (warning.get("status") == "pending_user_review"
                and manifest["slices"].get(warning.get("slice_id"), {}).get("status") == "accepted"
                and warning.get("approval_id") == manifest.get("active_approval_id")):
            errors.append(f"Visual warning {warning_id}: slice accepted without user's warning decision")
    return errors


def save_approved_case_warning_gallery(directory: Path, slice_id: str, manifest: dict, review: dict) -> str:
    """Include original mismatch and all three retries, also for legacy B-case warning records."""
    pairs: list[dict] = []
    packet = read_json(directory / "slices" / slice_id / "contract.json")
    approved = {case["case_id"]: case for case in packet["visual_cases"]}
    for item in review["visual_evidence"]:
        if item["result"] != "WARNING":
            continue
        case_id = item["case_id"]
        reference = approved[case_id]["reference_file"]
        diagnosis = read_json(artifact_file(directory, item["capture_warning"]["diagnosis_file"]))
        pairs.append({
            "case_id": case_id, "attempt_id": "initial-failure", "reference_file": reference,
            "actual_file": item["implementation_screenshot"],
            "different_pixels": diagnosis.get("initial_different_pixels", "initial mismatch, count not recorded"),
        })
        for index, screenshot in enumerate(item["capture_warning"]["retry_screenshots"]):
            pairs.append({
                "case_id": case_id, "attempt_id": diagnosis["retry_context_ids"][index],
                "reference_file": reference, "actual_file": screenshot,
                "different_pixels": diagnosis["retry_different_pixels"][index],
            })
    report = {
        "warning_id": f"approved-capture-warnings-{slice_id}",
        "comparison_scope": "approved_to_implementation",
        "summary": "Provisional capture-only discrepancies: original failed image and three reviewed fresh retries per case",
        "reviewer_notes": review["notes"],
        "comparison_pairs": pairs,
    }
    relative = f"slices/{slice_id}/evidence/approved-capture-warnings.html"
    target = directory / relative
    content = warning_gallery(directory, target.parent, report)
    if target.exists():
        if target.read_text(encoding="utf-8") != content:
            raise StateError("Existing warning gallery changed; preserve it and start a new verification version")
    else:
        atomic_text(target, content)
    return relative


def decide_warning(directory: Path, warning_id: str, decision_file: str) -> None:
    decision = read_json(Path(decision_file))
    with coordinator_lock(directory):
        manifest = manifest_at(directory)
        errors = check_initiative(directory, manifest)
        warning = manifest.get("warnings", {}).get(warning_id)
        if errors:
            raise StateError("Cannot decide warning while verification state is invalid: " + "; ".join(errors))
        if not warning or warning["status"] != "pending_user_review":
            raise StateError("Warning is missing or no longer awaiting user review")
        if (decision.get("warning_id") != warning_id or decision.get("decision") not in {"accept", "reject"}
                or decision.get("confirmed_by_user") is not True
                or decision.get("approval_id") != warning.get("approval_id")
                or not str(decision.get("user_note") or "").strip()):
            raise StateError("Explicit user decision must name this warning, current approval, accept/reject and a reason")
        relative = f"warnings/{warning_id}/decision.json"
        if (directory / relative).exists():
            raise StateError("Warning decision was already recorded; do not overwrite user decisions")
        atomic_json(directory / relative, {**decision, "recorded_at": utc_now()})
        warning["decision_file"] = relative
        warning["evidence_hashes"][relative] = digest_of(directory / relative)
        warning["status"] = "accepted" if decision["decision"] == "accept" else "rejected"
        if warning["status"] == "rejected":
            assignment = manifest["slices"][warning["slice_id"]]
            assignment["status"] = "blocked"
            assignment["blockers"] = [f"User rejected visual warning {warning_id}: {decision['user_note']}"]
            block_dependents(directory, manifest, warning["slice_id"])
            manifest["stage"] = "verify"
            manifest["next_action"] = "Investigate the rejected visual warning and reverify before final acceptance"
        else:
            manifest["next_action"] = "Revalidate evidence and finish remaining slice acceptance"
        journal(directory, {"type": "visual_warning_decided", "warning_id": warning_id,
                            "status": warning["status"], "decision": decision})
        note_progress(manifest, f"Visual warning {warning_id}: {warning['status']} by explicit user decision")
        save_workflow_state(directory, manifest)
    print(f"Visual warning {warning_id}: {warning['status']}")


def check_coverage_records(directory: Path, manifest: dict) -> list[str]:
    """Protect approved source/browser discoveries across sessions and compaction."""
    errors: list[str] = []
    audits = manifest.get("discovery_audits", {})
    active_id = manifest.get("active_discovery_id")
    if active_id and active_id not in audits:
        errors.append("Active A discovery audit is missing from the manifest")
    for audit_id, item in audits.items():
        for relative, expected in {item["path"]: item["sha256"], **item.get("evidence_hashes", {})}.items():
            try:
                if digest_of(artifact_file(directory, relative)) != expected:
                    errors.append(f"Discovery {audit_id}: changed protected evidence {relative}")
            except (StateError, OSError) as error:
                errors.append(f"Discovery {audit_id}: {error}")
    for version, item in manifest.get("prototype_coverage", {}).items():
        if item.get("audit_id") not in audits:
            errors.append(f"Functionality coverage {version}: referenced A audit is missing")
        for relative, expected in {item["path"]: item["sha256"], **item.get("evidence_hashes", {})}.items():
            try:
                if digest_of(artifact_file(directory, relative)) != expected:
                    errors.append(f"Functionality coverage {version}: changed protected evidence {relative}")
            except (StateError, OSError) as error:
                errors.append(f"Functionality coverage {version}: {error}")
    for report_id, item in manifest.get("missing_reports", {}).items():
        for relative, expected in {item["path"]: item["sha256"], **item.get("evidence_hashes", {})}.items():
            try:
                if digest_of(artifact_file(directory, relative)) != expected:
                    errors.append(f"Missing feature {report_id}: changed protected evidence {relative}")
            except (StateError, OSError) as error:
                errors.append(f"Missing feature {report_id}: {error}")
    for approval_id, approval in manifest.get("approvals", {}).items():
        bound = approval.get("functionality_coverage")
        if not bound:
            # Read-only compatibility for approvals frozen before this stronger gate existed.
            continue
        record = manifest.get("prototype_coverage", {}).get(bound.get("prototype_version"))
        if (not record or bound.get("contract_sha256") != record.get("sha256")
                or bound.get("audit_id") != record.get("audit_id")):
            errors.append(f"Approved {approval_id}: immutable A-to-B functionality contract is missing or changed")
    if manifest.get("coverage_pending"):
        review = "blueprint coverage review" if initiative_mode(manifest) == "blueprint" else "prototype parity review"
        errors.append(f"New or suspected application-A functionality needs a complete follow-up sweep and {review}")
    return errors


def check_work_state(directory: Path, manifest: dict) -> list[str]:
    """Detect progress/status drift before a session trusts its restart cursor."""
    errors: list[str] = []
    items = manifest.get("work_items", {})
    if not isinstance(items, dict):
        return ["Work history is not a task dictionary"]
    active_id = manifest.get("active_work_id")
    if active_id and active_id not in items:
        return [f"Active work cursor {active_id} is missing"]
    unfinished = [task_id for task_id, item in items.items()
                  if isinstance(item, dict) and item.get("status") != "done"]
    if not active_id and unfinished:
        errors.append("Unfinished work exists but its active cursor was lost: " + ", ".join(sorted(unfinished)))
    if active_id and any(task_id != active_id for task_id in unfinished):
        errors.append("Multiple unfinished work tasks conflict with the active cursor")
    for task_id, item in items.items():
        if not isinstance(item, dict) or item.get("task_id") != task_id:
            errors.append(f"Task {task_id}: malformed work cursor")
            continue
        if item.get("status") not in VALID_WORK_STATUSES or item.get("phase") not in VALID_STAGES:
            errors.append(f"Task {task_id}: invalid saved phase or status")
        if item.get("status") == "done" and (item.get("pending_steps") or item.get("blockers")):
            errors.append(f"Task {task_id}: marked done with unfinished work")
    if active_id and active_id in items:
        active = items[active_id]
        if (initiative_mode(manifest) == "blueprint" and active.get("status") == "waiting_user"
                and not active.get("pending_question_id")):
            errors.append(f"Task {active_id}: waiting_user cursor needs its exact conversation exchange linked")
        if active.get("status") == "done":
            errors.append(f"Completed task {active_id} cannot remain the active cursor")
        if active.get("phase") != manifest.get("stage"):
            errors.append(f"Task {active_id}: phase {active.get('phase')} disagrees with stage {manifest.get('stage')}")
        if active.get("next_action") != manifest.get("next_action"):
            errors.append(f"Task {active_id}: next action contradicts manifest; update the work cursor")
        prefix = f"Work {active_id}:"
        recorded = [item for item in manifest.get("blockers", []) if item.startswith(prefix)]
        expected = [f"{prefix} {reason}" for reason in active.get("blockers", [])]
        if active.get("status") == "blocked" and sorted(recorded) != sorted(expected):
            errors.append(f"Task {active_id}: blocked status/reasons contradict canonical blockers")
        if active.get("status") != "blocked" and recorded:
            errors.append(f"Task {active_id}: old blocker remains after recovery")
    latest_journal_tasks: dict[str, dict] = {}
    try:
        with (directory / "decisions.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "work_cursor_updated":
                    task = event.get("task")
                    if isinstance(task, dict) and isinstance(task.get("task_id"), str):
                        latest_journal_tasks[task["task_id"]] = task
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"Work decision journal is unreadable: {error}")
    for task_id, saved in latest_journal_tasks.items():
        if saved != items.get(task_id):
            errors.append(f"Task {task_id}: manifest differs from the append-only journal; reconcile task progress")
    for task_id in set(items) - set(latest_journal_tasks):
        errors.append(f"Task {task_id}: missing append-only journal history")
    return errors


def check_initiative(directory: Path, manifest: dict) -> list[str]:
    errors = check_approval(directory, manifest)
    errors.extend(check_coverage_records(directory, manifest))
    errors.extend(check_work_state(directory, manifest))
    if initiative_mode(manifest) == "blueprint":
        state = blueprint_state(directory, manifest)
        errors.extend(state.check_records())
        try:
            state.check_baseline_before_delivery()
        except (StateError, KeyError, OSError) as error:
            errors.append(str(error))
    for slice_id, assignment in manifest.get("slices", {}).items():
        status = assignment.get("status")
        blockers = assignment.get("blockers", [])
        if status == "blocked" and not blockers:
            errors.append(f"Slice {slice_id}: blocked status requires an explicit recorded reason")
        if status in {"ready", "reported", "verified", "provisional", "accepted"} and blockers:
            errors.append(f"Slice {slice_id}: nonblocked status contradicts recorded blockers")
        if not assignment.get("superseded_by") and status != "cancelled" and assignment.get("blueprint_version", assignment.get("prototype_version")) != manifest.get("approved_version"):
            errors.append(f"Slice {slice_id}: references a superseded or revoked {initiative_mode(manifest)}")
        try:
            actual = digest_of(artifact_file(directory, assignment["contract_file"]))
            if actual != assignment["contract_sha256"]:
                errors.append(f"Slice {slice_id}: sha256 mismatch in handoff contract")
        except (StateError, OSError, KeyError) as error:
            errors.append(str(error))
        if assignment.get("status") in {"verified", "provisional", "accepted"}:
            errors.extend(check_saved_verification(directory, slice_id, assignment, manifest))
        for path, saved_digest in assignment.get("block_evidence_hashes", {}).items():
            try:
                if digest_of(artifact_file(directory, path)) != saved_digest:
                    errors.append(f"Slice {slice_id}: altered original blocker evidence {path}")
            except (StateError, OSError) as error:
                errors.append(str(error))
    errors.extend(check_warning_evidence(directory, manifest))
    return errors


def pending_supplemental_warnings(manifest: dict, slice_id: str) -> list[str]:
    return [warning_id for warning_id, warning in manifest.get("warnings", {}).items()
            if warning.get("slice_id") == slice_id
            and warning.get("approval_id") == manifest.get("active_approval_id")
            and warning.get("status") in {"pending_user_review", "rejected"}]


def block_dependents(directory: Path, manifest: dict, prerequisite_id: str,
                     *, staged: tuple[list[dict], dict] | None = None) -> None:
    """A newly blocked prerequisite cannot leave dependent tasks falsely ready or accepted."""
    for dependent_id, dependent in manifest["slices"].items():
        if prerequisite_id not in dependent.get("dependencies", []) or dependent["status"] == "cancelled":
            continue
        reason = f"Waiting for accepted dependency: {prerequisite_id}"
        if reason in dependent.get("blockers", []):
            continue
        previous = dependent["status"]
        dependent["status"] = "blocked"
        dependent["blockers"] = list(dependent.get("blockers", [])) + [reason]
        if previous == "ready":
            packet_path = artifact_file(directory, dependent["contract_file"])
            packet = read_json(packet_path)
            packet["status"], packet["blockers"] = "blocked", dependent["blockers"]
            if staged is None:
                atomic_json(packet_path, packet)
            else:
                staged[1][dependent["contract_file"]] = packet
            dependent["contract_sha256"] = hashlib.sha256(json_content(packet).encode()).hexdigest()
        event = {"type": "dependent_slice_blocked", "slice_id": dependent_id,
                 "prerequisite": prerequisite_id, "prior_status": previous}
        if staged is None:
            journal(directory, event)
        else:
            staged[0].append(event)
        block_dependents(directory, manifest, dependent_id, staged=staged)


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
    transition = None
    with coordinator_lock(directory):
        if action == "block" and review_file:
            blocker = read_json(Path(review_file))
            request = {"command": "reconcile --action block", "slice_id": slice_id, "input": blocker}
            if recover_workflow_transition(directory, request):
                return
            transition = (request, [], {})
        manifest = manifest_at(directory)
        if initiative_mode(manifest) == "blueprint" and action in {"report", "verify", "accept", "accept-warning"}:
            blueprint_state(directory, manifest).require_ready()
        assignment = manifest.get("slices", {}).get(slice_id)
        if not assignment:
            raise StateError(f"Unknown slice: {slice_id}")
        errors = check_initiative(directory, manifest)
        if assignment.get("blueprint_version", assignment.get("prototype_version")) != manifest.get("approved_version"):
            errors.append(f"Slice references superseded {initiative_mode(manifest)}; reapproval and reslicing required")
        if errors:
            raise StateError("Cannot reconcile: " + "; ".join(errors))
        if action == "block":
            if assignment["status"] == "cancelled" or not review_file:
                raise StateError("Blocking an active slice requires --review-file with the defect and next action")
            if (blocker.get("slice_id") != slice_id
                    or not str(blocker.get("reason") or "").strip()
                    or not str(blocker.get("next_action") or "").strip()):
                raise StateError("Blocker must identify the slice, concrete defect and next action")
            evidence = blocker.get("evidence_files")
            if not isinstance(evidence, list) or not evidence or not all(isinstance(p, str) for p in evidence):
                raise StateError("A real blocker requires supporting evidence files")
            hashes: dict[str, str] = {}
            for path in evidence:
                if not path.startswith(f"slices/{slice_id}/evidence/"):
                    raise StateError("Blocker evidence must belong to the affected slice")
                hashes[path] = digest_of(artifact_file(directory, path))
            assignment.setdefault("block_evidence_hashes", {}).update(hashes)
            assignment["status"] = "blocked"
            assignment["blockers"] = [blocker["reason"]]
            _, events, artifacts = transition
            block_dependents(directory, manifest, slice_id, staged=(events, artifacts))
            manifest["stage"] = "implement"
            manifest["next_action"] = blocker["next_action"]
            events.append({"type": "slice_blocked", "slice_id": slice_id,
                           "reason": blocker["reason"], "evidence_hashes": hashes,
                           "next_action": blocker["next_action"]})
        elif action == "unblock":
            if assignment["status"] != "blocked" or not review_file:
                raise StateError("Only a blocked slice can be resumed with a written resolution")
            if any(manifest["slices"][dep]["status"] not in {"accepted", "provisional"}
                   for dep in assignment.get("dependencies", [])):
                raise StateError("Slice still has an unfinished or blocked dependency")
            resolution = read_json(Path(review_file))
            if (resolution.get("slice_id") != slice_id
                    or not str(resolution.get("resolution") or "").strip()
                    or not str(resolution.get("next_action") or "").strip()):
                raise StateError("Unblocking requires an explicit resolution and next action")
            assignment["status"], assignment["blockers"] = "ready", []
            manifest["stage"] = "implement"
            manifest["next_action"] = resolution["next_action"]
            journal(directory, {"type": "slice_unblocked_after_resolution", "slice_id": slice_id,
                                "resolution": resolution["resolution"],
                                "next_action": resolution["next_action"]})
        elif action == "report":
            if assignment["status"] in {"verified", "provisional", "accepted", "blocked"}:
                raise StateError("Cannot erase a blocker or verification with a stale worker report; resolve the blocker first")
            report = read_json(directory / "slices" / slice_id / "worker-report.json")
            if report.get("owner") != assignment["owner"] or report.get("slice_id") != slice_id:
                raise StateError("Worker report identity conflicts with coordinator assignment")
            assignment["status"] = "reported" if report["status"] == "submitted" else "blocked"
            assignment["blockers"] = (report.get("blockers") or [f"Worker reported blocked: {report['summary']}"])
            if assignment["status"] == "reported":
                assignment["blockers"] = []
            else:
                block_dependents(directory, manifest, slice_id)
            journal(directory, {"type": "slice_reconciled", "slice_id": slice_id, "status": assignment["status"], "summary": report["summary"]})
            manifest["stage"] = "implement"
            manifest["next_action"] = ("Verify reported slice against approved blueprint checks"
                                       if initiative_mode(manifest) == "blueprint"
                                       else "Verify reported slice against approved visual cases and affected behavior")
        elif action == "verify":
            if initiative_mode(manifest) == "blueprint":
                blueprint_state(directory, manifest).require_ready()
                if assignment["status"] != "reported" or assignment.get("blockers") or any(
                    manifest["slices"][dep]["status"] != "accepted" for dep in assignment.get("dependencies", [])
                ):
                    raise StateError("Resolve explicit defect/dependency blockers and report the slice before verification")
            if assignment["status"] not in {"reported", "blocked", "verified"}:
                raise StateError("Slice must first have a reconciled worker report")
            if not review_file:
                raise StateError("Verification requires --review-file with independently inspected visual and functional evidence")
            review = read_json(Path(review_file))
            verified, failures = validate_verification(directory, slice_id, manifest, review)
            if failures:
                assignment["status"] = "blocked"
                assignment["blockers"] = failures
                block_dependents(directory, manifest, slice_id)
                assignment["warning_case_ids"] = []
                manifest["stage"] = "verify"
                manifest["next_action"] = "Fix verification blockers, rerun visual/functional checks and submit review"
                journal(directory, {"type": "verification_blocked", "slice_id": slice_id, "errors": failures, "review": review})
                save_workflow_state(directory, manifest)
                raise StateError("Verification BLOCKED: " + "; ".join(failures))
            if verified["status"] == "WARNING":
                gallery = save_approved_case_warning_gallery(directory, slice_id, manifest, verified)
                verified["warning_gallery_file"] = gallery
                verified["evidence_hashes"][gallery] = digest_of(directory / gallery)
                assignment["warning_gallery_file"] = gallery
            atomic_json(directory / "slices" / slice_id / "verification.json", verified)
            if initiative_mode(manifest) == "blueprint":
                assignment["verification_sha256"] = digest_of(directory / "slices" / slice_id / "verification.json")
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
                event = {"type": "slice_verified", "slice_id": slice_id, "reviewer": verified["reviewer"],
                         "verified_at": verified["reviewed_at"]}
                if initiative_mode(manifest) == "blueprint":
                    event["verification_sha256"] = assignment["verification_sha256"]
                journal(directory, event)
        elif action == "accept":
            if assignment["status"] != "verified":
                raise StateError("Slice must have PASS verification and independent reviewer before acceptance")
            pending = pending_supplemental_warnings(manifest, slice_id)
            if pending:
                raise StateError("Review supplemental side-by-side capture warnings before acceptance: " + ", ".join(pending))
            failures = check_saved_verification(directory, slice_id, assignment, manifest)
            if failures:
                raise StateError("Verification became stale: " + "; ".join(failures))
            assignment["status"] = "accepted"
            assignment["blockers"] = []
            journal(directory, {"type": "slice_accepted", "slice_id": slice_id, "approved_version": manifest["approved_version"]})
            unblock_dependents(directory, manifest)
            manifest["stage"] = "integrate" if (blueprint_state(directory, manifest).all_accepted() if initiative_mode(manifest) == "blueprint" else all(item["status"] == "accepted" for item in manifest["slices"].values())) else "implement"
            manifest["next_action"] = "Review accepted slice integration and remaining dependencies; do not merge/deploy without user authorization"
        elif action == "accept-warning":
            if assignment["status"] != "provisional":
                raise StateError("Only a provisional capture WARNING can receive explicit later user acceptance")
            pending = pending_supplemental_warnings(manifest, slice_id)
            if pending:
                raise StateError("Resolve supplemental side-by-side warnings before slice acceptance: " + ", ".join(pending))
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
            manifest["stage"] = "integrate" if (blueprint_state(directory, manifest).all_accepted() if initiative_mode(manifest) == "blueprint" else all(item["status"] == "accepted" for item in manifest["slices"].values())) else "implement"
            manifest["next_action"] = "Review accepted slice integration and remaining dependencies; do not merge/deploy without user authorization"
        else:
            raise StateError(f"Unknown reconciliation action: {action}")
        note_progress(manifest, f"Slice {slice_id}: {assignment['status']} ({action})",
                      completed=f"Accepted {slice_id}" if action in {"accept", "accept-warning"} else None)
        save_workflow_state(directory, manifest, transition=transition)
    print(f"Reconciled slice {slice_id}: {assignment['status']} ({action})")


def git_info(project_root: Path) -> tuple[str | None, str | None]:
    def run(*args):
        try:
            result = subprocess.run(["git", "-C", str(project_root), *args], capture_output=True, text=True)
        except OSError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None
    return run("rev-parse", "HEAD"), run("status", "--short")


def discover_single_initiative(project_root: Path) -> str:
    """Find the only existing initiative; never guess between multiple projects."""
    candidates: set[str] = set()
    for relative in (HIDDEN_ARTIFACT_ROOT, LEGACY_ARTIFACT_ROOT):
        base = project_root / relative
        if base.is_symlink():
            raise StateError("Initiative root is a symlink; select a verified project directory")
        if not base.is_dir():
            continue
        for candidate in base.iterdir():
            if (candidate.is_dir() and not candidate.is_symlink()
                    and INITIATIVE_ID.fullmatch(candidate.name)
                    and (candidate / "manifest.json").is_file()):
                candidates.add(candidate.name)
    if not candidates:
        raise StateError("No saved redesign initiative in this project; initialize one before resuming")
    if len(candidates) != 1:
        raise StateError("Multiple redesign initiatives found: " + ", ".join(sorted(candidates))
                         + "; select the intended initiative with --initiative-id")
    return next(iter(candidates))


def recent_journal_events(directory: Path, limit: int = 30) -> list[dict]:
    """Surface recent decisions while retaining the full append-only history on disk."""
    path = directory / "decisions.jsonl"
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = deque(handle, maxlen=limit)
        result: list[dict] = []
        for line in lines:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Non-object event in decision journal")
            summary = (event.get("summary") or (event.get("task") or {}).get("summary")
                       or event.get("approved_version") or event.get("type"))
            result.append({"type": event.get("type"), "at": event.get("at"),
                           "summary": summary, "decision_id": event.get("decision_id")})
        return result
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot reconstruct recent decision history from {path}: {error}") from error


def build_resume_packet(directory: Path, project_root: Path) -> dict:
    """Create a read-only, machine-usable continuation plan from durable files."""
    manifest = manifest_at(directory)
    active = manifest.get("approvals", {}).get(manifest.get("active_approval_id"))
    work = manifest.get("work_items", {}).get(manifest.get("active_work_id"))
    actual_head, actual_status = git_info(project_root)
    recorded = manifest.get("provenance", {})
    reconciliation: list[str] = []
    if recorded.get("repository_head") and recorded["repository_head"] != actual_head:
        reconciliation.append("Git HEAD changed since the saved repository checkpoint")
    if recorded.get("worktree_status") and recorded["worktree_status"].strip() != (actual_status or ""):
        reconciliation.append("Git worktree status changed since the saved repository checkpoint")
    if work:
        if work.get("repository_head") and work["repository_head"] != actual_head:
            reconciliation.append("Git HEAD changed since the task's last saved step")
        if work.get("worktree_status") is not None and work["worktree_status"] != actual_status:
            reconciliation.append("Git worktree status changed since the task's last saved step")
        for path, expected in work.get("code_hashes", {}).items():
            try:
                if work_source_hash(directory, path, expected_absent=expected is None) != expected:
                    reconciliation.append(f"Code changed since the saved task step: {path}")
            except (StateError, OSError) as error:
                reconciliation.append(f"Code from the saved task is unavailable: {path} ({error})")
        for path, expected in work.get("artifact_hashes", {}).items():
            try:
                if digest_of(artifact_file(directory, path)) != expected:
                    reconciliation.append(f"Task evidence changed since the saved step: {path}")
            except (StateError, OSError) as error:
                reconciliation.append(f"Saved task evidence is unavailable: {path} ({error})")
    errors = check_initiative(directory, manifest)
    blockers = list(manifest.get("blockers", []))
    if work:
        for item in work.get("blockers", []):
            if item not in blockers:
                blockers.append(item)
    conversation, pending_questions = [], []
    active_question_answer = None
    question_source_errors = []
    conversation_usage, coverage_with_slices = {}, []
    if initiative_mode(manifest) == "blueprint":
        state = blueprint_state(directory, manifest)
        try:
            conversation = state.conversations()
            pending_questions = state.pending()
            question_source_errors = state.check_question_sources(project_root, pending_questions)
            if work and work.get("pending_question_id"):
                active_question_answer, question_context = state.question_context(work["pending_question_id"], conversation)
                if active_question_answer:
                    question_source_errors.extend(state.check_question_sources(project_root, [question_context]))
        except (StateError, OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"Conversation recovery is blocked: {error}")
        try:
            conversation_usage, coverage_with_slices = state.traceability()
        except (StateError, OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"Traceability recovery is blocked: {error}")
    errors.extend(question_source_errors)
    next_action = work.get("next_action") if work else manifest.get("next_action")
    if question_source_errors:
        next_action = "Reconcile the changed question source before continuing."
    elif active_question_answer:
        next_action = "Use the recorded answer and update the saved work cursor."
    return {
        "initiative_id": manifest["initiative_id"], "initiative_directory": str(directory),
        "scope": manifest["scope"], "phase": manifest["stage"],
        "initiative_mode": initiative_mode(manifest),
        "approved_version": manifest.get("approved_version"),
        "approval_id": manifest.get("active_approval_id"),
        "approved_source_paths": active.get("source_paths", []) if active else [],
        "approved_spec_paths": [p for p in active.get("artifact_hashes", {}) if p.endswith(".md")] if active and active.get("mode") == "blueprint" else [],
        "visual_cases": active.get("visual_cases", []) if active else [],
        "design_direction": manifest.get("design_direction", {}),
        "decision_journal_file": str(directory / "decisions.jsonl"),
        "recent_decisions": recent_journal_events(directory),
        "last_checkpoint": manifest.get("last_checkpoint"),
        "latest_a_audit": manifest.get("active_discovery_id"),
        "coverage_pending": manifest.get("coverage_pending", False),
        "active_work": work,
        "conversation": conversation,
        "pending_questions": pending_questions,
        "active_question_answer": active_question_answer,
        "conversation_usage": conversation_usage,
        "coverage_with_slices": coverage_with_slices,
        "finished_tasks": sorted(task_id for task_id, item in manifest.get("work_items", {}).items()
                                 if item.get("status") == "done"),
        "slices": {key: item.get("status") for key, item in manifest.get("slices", {}).items()
                   if not item.get("superseded_by")},
        "superseded_slices": {key: {"last_status": item.get("status"), "superseded_by": item["superseded_by"]}
                              for key, item in manifest.get("slices", {}).items() if item.get("superseded_by")},
        "warnings": {key: item.get("status") for key, item in manifest.get("warnings", {}).items()},
        "next_action": next_action,
        "blockers": blockers, "validation_errors": errors,
        "reconciliation_required": reconciliation,
        "git_head": actual_head, "git_worktree_status": actual_status,
        "safe_to_continue": not errors and not reconciliation and not blockers
        and (not work or work.get("status") == "in_progress")
        and not pending_questions,
    }


def print_resume(directory: Path, project_root: Path) -> list[str]:
    packet = build_resume_packet(directory, project_root)
    manifest = manifest_at(directory)
    answer = packet["active_question_answer"]
    print(render_checkpoint(manifest, resume_action=packet["next_action"] if answer else None))
    active = manifest.get("approvals", {}).get(manifest.get("active_approval_id"))
    if active and packet["initiative_mode"] == "blueprint":
        print("Approved blueprint specifications:")
        for path in packet["approved_spec_paths"]:
            print(f"- {path}")
    elif active:
        print("Approved visual cases (read images directly before work):")
        for case in active.get("visual_cases", []):
            viewport = case.get("viewport", {})
            print(f"- {case.get('case_id')}: {case.get('reference_file')} at {viewport.get('width')}x{viewport.get('height')} ({case.get('state_setup')})")
        print("Approved prototype source files:")
        for path in active.get("source_paths", []):
            print(f"- {path}")
    print(f"Current Git HEAD: {packet['git_head'] or 'unavailable (inspect code manually)'}")
    print(f"Current Git status: {packet['git_worktree_status'] or ('clean' if packet['git_worktree_status'] is not None else 'unavailable')}")
    if packet["active_work"]:
        status = "answer recorded" if answer else packet["active_work"]["status"]
        print(f"RESUME TASK: {packet['active_work']['task_id']} ({status})")
        print(f"NEXT: {packet['next_action']}")
    if answer:
        print(f"RECORDED QUESTION {answer['responds_to']}: {answer['question']}")
        print(f"RECORDED ANSWER {answer['exchange_id']}: {answer['response']}")
    for question in packet["pending_questions"]:
        print(f"PENDING QUESTION {question['exchange_id']}: {question['question']}")
        for option in question.get("options", []):
            print(f"- {option['label']}: {option['tradeoff']}")
        for path in question.get("source_paths", []) + question.get("evidence_paths", []) + question.get("illustration_paths", []):
            print(f"- Evidence: {path}")
        if question.get("recommendation"):
            print(f"- Recommendation: {question['recommendation']}")
    for issue in packet["reconciliation_required"]:
        print(f"RECONCILE: {issue}")
    for error in packet["validation_errors"]:
        print(f"BLOCKED: {error}")
    if packet["active_work"] and packet["active_work"]["status"] == "waiting_user" and not answer:
        print("WAITING FOR USER: do not infer or invent the pending approval or decision")
    return packet["validation_errors"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "init", "event", "checkpoint", "conversation-record", "plan-record", "blueprint-record", "blueprint-coverage", "blueprint-index-rebuild", "work-update", "discovery-record", "discovery-capture", "browser-build-fingerprint", "missing-feature", "prototype-coverage", "approve", "revoke-approval", "slice-create", "slice-cancel",
        "slice-report", "reconcile", "warning-record", "warning-decide", "resume", "validate",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("--project-root", required=True)
        sub.add_argument("--initiative-id", required=name not in {"resume", "validate"})
        if name == "browser-build-fingerprint":
            sub.add_argument("--source-path", action="append", default=[])
        if name == "init":
            sub.add_argument("--scope", choices=sorted(VALID_SCOPES), required=True)
            sub.add_argument("--next-action")
            sub.add_argument("--mode", choices=["blueprint", "prototype"])
        if name == "event":
            sub.add_argument("--event-file", required=True)
        if name == "checkpoint":
            sub.add_argument("--checkpoint-file", required=True)
        if name in {"plan-record", "blueprint-record", "blueprint-coverage"}:
            sub.add_argument({"plan-record":"--plan-file", "blueprint-record":"--blueprint-file", "blueprint-coverage":"--coverage-file"}[name], required=True)
        if name == "blueprint-index-rebuild":
            sub.add_argument("--plan-version", required=True)
        if name == "conversation-record":
            sub.add_argument("--exchange-file", required=True)
        if name == "work-update":
            sub.add_argument("--work-file", required=True)
        if name == "discovery-record":
            sub.add_argument("--audit-file", required=True)
        if name == "discovery-capture":
            sub.add_argument("--capture-file", required=True)
        if name == "missing-feature":
            sub.add_argument("--report-file", required=True)
        if name == "prototype-coverage":
            sub.add_argument("--coverage-file", required=True)
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
            sub.add_argument("--action", choices=["report", "block", "unblock", "verify", "accept", "accept-warning"], required=True)
            sub.add_argument("--review-file")
        if name == "warning-record":
            sub.add_argument("--warning-file", required=True)
        if name == "warning-decide":
            sub.add_argument("--warning-id", required=True)
            sub.add_argument("--decision-file", required=True)
        if name == "resume":
            sub.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    try:
        project_root = Path(args.project_root).resolve(strict=True)
        initiative_id = args.initiative_id or discover_single_initiative(project_root)
        directory = location(str(project_root), initiative_id)
        ensure_local_git_ignore(project_root, directory)
        if args.command == "init":
            initialize(directory, args.scope, args.next_action, args.mode)
        elif args.command == "event":
            add_event(directory, args.event_file)
        elif args.command == "checkpoint":
            checkpoint(directory, args.checkpoint_file)
        elif args.command == "work-update":
            work_update(directory, project_root, args.work_file)
        elif args.command in {"plan-record", "blueprint-record", "blueprint-coverage"}:
            input_file = getattr(args, {"plan-record":"plan_file", "blueprint-record":"blueprint_file", "blueprint-coverage":"coverage_file"}[args.command])
            record_blueprint_command(directory, args.command, input_file, project_root)
        elif args.command == "blueprint-index-rebuild":
            with coordinator_lock(directory):
                manifest = manifest_at(directory)
                if initiative_mode(manifest) != "blueprint":
                    raise StateError("Roadmap blueprint indexes require blueprint mode")
                state = blueprint_state(directory, manifest)
                errors = state.check_records(check_indexes=False)
                if errors:
                    raise StateError("; ".join(errors))
                if not state.write_plan_blueprint_index(args.plan_version):
                    raise StateError("No generated roadmap index for that plan")
            print(f"Rebuilt roadmap blueprint index for {args.plan_version}")
        elif args.command == "conversation-record":
            record_blueprint_command(directory, args.command, args.exchange_file, project_root)
        elif args.command == "discovery-capture":
            record_blueprint_command(directory, args.command, args.capture_file, project_root)
        elif args.command == "browser-build-fingerprint":
            state = blueprint_state(directory, manifest_at(directory))
            print(state.build_fingerprint(state.discovery_snapshot(args.source_path)))
        elif args.command == "discovery-record":
            if initiative_mode(manifest_at(directory)) == "blueprint":
                record_blueprint_command(directory, args.command, args.audit_file, project_root)
            else:
                record_discovery(directory, project_root, args.audit_file)
        elif args.command == "missing-feature":
            report_missing_feature(directory, args.report_file)
        elif args.command == "prototype-coverage":
            record_prototype_coverage(directory, args.coverage_file)
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
        elif args.command == "warning-record":
            record_warning(directory, args.warning_file)
        elif args.command == "warning-decide":
            decide_warning(directory, args.warning_id, args.decision_file)
        elif args.command == "resume":
            if args.format == "json":
                packet = build_resume_packet(directory, project_root)
                print(json.dumps(packet, indent=2, ensure_ascii=False, sort_keys=True))
                return 2 if packet["validation_errors"] else 0
            return 2 if print_resume(directory, project_root) else 0
        elif args.command == "validate":
            manifest = manifest_at(directory)
            if initiative_mode(manifest) == "blueprint":
                packet = build_resume_packet(directory, project_root)
                errors = packet["validation_errors"] + packet["reconciliation_required"]
            else:
                errors = check_initiative(directory, manifest)
            if errors:
                raise StateError("; ".join(errors))
            if not manifest.get("active_approval_id"):
                mode = initiative_mode(manifest)
                print(f"NOT YET APPROVED: initiative state is valid, but no {mode} is approved for implementation")
            else:
                print("OK: current approval, slice handoffs, warning galleries, and evidence hashes are consistent")
        return 0
    except (OSError, ValueError, StateError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
