"""Portable command, journal and suspension checks for slice review.

These helpers are deliberately independent of the CLI so callers can run the
same checks while validating a bundle in a receiving workspace.
"""

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil


def _hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return sha256(encoded).hexdigest()


def _safe_path(root, relative):
    if not isinstance(relative, str) or not relative or PurePosixPath(relative).is_absolute():
        raise ValueError(f"Invalid relative workspace path: {relative!r}")
    if ".." in PurePosixPath(relative).parts:
        raise ValueError(f"Workspace path escapes root: {relative}")
    base = Path(root).resolve()
    path = base / relative
    if path.resolve() != base / relative:
        raise ValueError(f"Workspace path uses a redirect: {relative}")
    return path


def _is_path_reference(argument):
    """Return an absolute path embedded in an argv item, if present."""
    if not isinstance(argument, str):
        return None
    candidate = argument.split("=", 1)[-1] if "=" in argument else argument
    if os.path.isabs(candidate):
        return candidate
    return None


def _is_interpreter(path):
    name = Path(path).name.casefold()
    return (name in {"node", "deno", "bun", "ruby", "perl", "php", "bash", "sh", "zsh", "java"}
            or name in {"python", "python2", "python3", "pypy", "pypy2", "pypy3"}
            or name.startswith(("python2.", "python3.", "pypy2.", "pypy3.")))


def validate_transportable_commands(commands, workspace_root):
    """Return blockers for argv that will stop working after bundle relocation.

    An absolute executable is accepted only when its exact file is also found
    through PATH. This supports fixtures using ``sys.executable`` while
    rejecting absolute runner paths and absolute arguments tied to the sender.
    """
    errors = []
    root = Path(workspace_root).resolve()
    if not isinstance(commands, (list, tuple)):
        return ["Approved test commands must be a list of argument vectors"]
    for command_number, argv in enumerate(commands, 1):
        label = f"Command {command_number}"
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(part, str) for part in argv):
            errors.append(f"{label} must be a nonempty string argument vector")
            continue
        executable = argv[0]
        if os.path.isabs(executable):
            path_match = shutil.which(Path(executable).name)
            try:
                available = path_match is not None and os.path.samefile(executable, path_match)
            except OSError:
                available = False
            if not available or not _is_interpreter(executable):
                errors.append(f"{label} uses an absolute nonportable executable: {executable}")
        elif "/" in executable or "\\" in executable:
            try:
                _safe_path(root, executable)
            except (OSError, ValueError) as error:
                errors.append(f"{label} has a nonportable runner: {error}")
        elif shutil.which(executable) is None:
            errors.append(f"{label} executable is unavailable on PATH: {executable}")
        for argument in argv[1:]:
            absolute = _is_path_reference(argument)
            if absolute:
                try:
                    Path(absolute).relative_to(root)
                    detail = "workspace-specific"
                except ValueError:
                    detail = "absolute"
                errors.append(f"{label} contains a {detail} path argument that cannot travel with the bundle: {argument}")
    return errors


def validate_runtime_paths(paths, workspace_root):
    """Return blockers for required source/test paths missing in this workspace."""
    errors = []
    if not isinstance(paths, (list, tuple, set)):
        return ["Required source and test references must be a list of relative paths"]
    for relative in sorted(paths):
        try:
            path = _safe_path(workspace_root, relative)
            if not path.is_file():
                errors.append(f"Required source or test reference is missing in this workspace: {relative}")
        except (OSError, ValueError, TypeError) as error:
            errors.append(str(error))
    return errors


def _marker_events(marker):
    if not marker:
        return []
    if isinstance(marker, list):
        candidates = marker
    elif isinstance(marker, dict):
        candidates = marker.get("events", [])
        if not candidates:
            candidates = marker.get("lines", [])
    else:
        return []
    events = []
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(candidate, dict):
            events.append(candidate)
    return events


def _matching_event(events, event_type, predicate):
    return any(event.get("type") == event_type and predicate(event) for event in events)


def validate_lifecycle_history(events, registry, workspace_root, recovery_marker=None):
    """Cross-check durable lifecycle receipts against committed journal events.

    Events staged in an intact workflow-transition marker count as recoverable;
    this lets the caller recover the prepared transition instead of reporting a
    false journal gap. The caller should still run its normal marker recovery.
    """
    errors = []
    journal = [event for event in events if isinstance(event, dict)]
    journal.extend(_marker_events(recovery_marker))
    root = Path(workspace_root)
    for slice_id, review in registry.get("reviews", {}).items():
        review_id = review.get("review_id")
        path = review.get("path")
        try:
            receipt = json.loads(_safe_path(root, path).read_text())
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            errors.append(f"Review {review_id or slice_id} receipt is missing or invalid: {error}")
            continue
        expected = {key: review[key] for key in ("review_id", "slice_id", "proposal_revision",
                    "reviewed_guide_sha256", "reviewed_contract_sha256") if key in review}
        if any(receipt.get(key) != value for key, value in expected.items()):
            errors.append(f"Review {review_id or slice_id} receipt does not match the register")
        if not _matching_event(journal, "slice_spec_reviewed", lambda event:
                event.get("review_id") == review_id and event.get("receipt_path") == path
                and all(event.get(key) == value for key, value in expected.items())):
            errors.append(f"Review {review_id or slice_id} receipt has no matching journal event")

    for package_id, package_entry in registry.get("packages", {}).items():
        package_hash = package_entry.get("package_hash")
        package_path = package_entry.get("path")
        lock_path = package_entry.get("receipt_path")
        try:
            package = json.loads(_safe_path(root, package_path).read_text())
            lock = json.loads(_safe_path(root, lock_path).read_text())
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            errors.append(f"Package {package_id} lock artifacts are missing or invalid: {error}")
            continue
        package_body = {key: value for key, value in package.items() if key != "package_hash"}
        if (package.get("package_hash") != package_hash or _hash(package_body) != package_hash
                or lock.get("package_id") != package_id or lock.get("package_hash") != package_hash):
            errors.append(f"Package {package_id} lock artifacts do not match the register")
        if not _matching_event(journal, "slice_package_locked", lambda event:
                event.get("package_id") == package_id and event.get("package_hash") == package_hash
                and event.get("receipt_path") == lock_path):
            errors.append(f"Package {package_id} lock receipt has no matching journal event")

        authorization_path = package_entry.get("authorization_path")
        if authorization_path:
            try:
                authorization = json.loads(_safe_path(root, authorization_path).read_text())
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                errors.append(f"Package {package_id} authorization receipt is missing or invalid: {error}")
            else:
                if authorization.get("package_hash") != package_hash or authorization.get("package_id") != package_id:
                    errors.append(f"Package {package_id} authorization receipt does not match its lock")
                if not _matching_event(journal, "slice_implementation_authorized", lambda event:
                        event.get("receipt_path") == authorization_path
                        and event.get("receipt") == authorization):
                    errors.append(f"Package {package_id} authorization has no matching journal event")

        for slice_id, release in package_entry.get("released", {}).items():
            if not _matching_event(journal, "slice_package_released", lambda event:
                    event.get("package_id") == package_id and event.get("slice_id") == slice_id
                    and event.get("contract_sha256") == release.get("contract_sha256")):
                errors.append(f"Package {package_id} release for {slice_id} has no matching journal event")

    migration_id = registry.get("migration_approval_id")
    if migration_id and not _matching_event(journal, "slice_review_migrated", lambda event:
            event.get("approval_id") == migration_id):
        # Earlier journal formats predate the explicit ID; the preserved legacy
        # assignment list still provides a stable identity for those entries.
        if not _matching_event(journal, "slice_review_migrated", lambda event:
                migration_id in event.get("preserved_approval_ids", [])):
            errors.append(f"Slice review migration for approval {migration_id} has no matching journal event")
    return errors


def capture_suspension_checkpoint(snapshot, package_id, approval_id):
    """Freeze the current workspace inventory before suspending execution."""
    body = {"package_id": package_id, "approval_id": approval_id, "snapshot": snapshot}
    return body | {"checkpoint_sha256": _hash(body)}


def validate_suspension_checkpoint(checkpoint, snapshot, *, can_change=None):
    """Report any scope change after suspension, including new or removed files."""
    body = {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
    if checkpoint.get("checkpoint_sha256") != _hash(body) or not isinstance(body.get("snapshot"), dict):
        return ["Suspension checkpoint integrity check failed"]
    previous = body["snapshot"]
    return [f"Suspended source changed after pause: {relative}"
            for relative in sorted(previous.keys() | snapshot.keys())
            if previous.get(relative) != snapshot.get(relative) and not (can_change and can_change(relative))]
