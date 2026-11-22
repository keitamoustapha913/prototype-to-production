#!/usr/bin/env python3
"""Run and review release acceptance scenarios against the actual Codex CLI."""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from acceptance_codex import Codex


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "conversational"
SCENARIOS = json.loads((FIXTURES / "scenarios.json").read_text())
RUNTIME = ("SKILL.md", "agents", "assets", "references", "scripts",
           "package.json", "package-lock.json", "requirements.txt")
AMBIGUOUS_REPLY = "Maybe, if the export behavior stays the same."
CORRECTION = "One correction: an empty export should still include the CSV header row."
REPLY_AFTER_RECOVERY = "Please pick up where we left off."


def git(*args, cwd=ROOT):
    return subprocess.check_output(["git", *args], cwd=cwd)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(directory):
    return {path.relative_to(directory).as_posix(): digest(path)
            for path in sorted(directory.rglob("*")) if path.is_file()}


def fixture_hashes(workspace):
    names = os.fsdecode(git("ls-files", "-z", cwd=workspace)).split("\0")
    return {name: digest(workspace / name) if (workspace / name).is_file() else None
            for name in names if name}


def is_fixture_change(path, workspace):
    resolved = (workspace / path).resolve()
    if not resolved.is_relative_to(workspace):
        return False
    relative = resolved.relative_to(workspace)
    return bool(relative.parts) and relative.parts[0] not in {".artifacts", ".git"}


def fingerprint(commit=None):
    if commit is None:
        paths = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        inventory = {}
        for name in set(os.fsdecode(paths).split("\0")) - {""}:
            path = ROOT / name
            if path.is_symlink():
                inventory[name] = ("120000", hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest())
            elif path.is_file():
                mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
                inventory[name] = (mode, digest(path))
            else:
                inventory[name] = None
    else:
        inventory = {}
        for entry in git("ls-tree", "-rz", commit).split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                mode, kind, object_id = metadata.split()
                content = git("cat-file", "blob", object_id.decode()) if kind == b"blob" else None
                inventory[os.fsdecode(name)] = (
                    (mode.decode(), hashlib.sha256(content).hexdigest()) if content is not None else None
                )
    return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()


def verified_baseline(path):
    try:
        receipt = load(path)
        commit = receipt["commit"]
        if (receipt["schema_version"] != 1 or receipt["status"] != "ready"
                or not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit)):
            raise ValueError("the receipt must identify a committed, ready candidate")
        git("merge-base", "--is-ancestor", commit, "HEAD")
        if receipt["candidate"] != fingerprint(commit):
            raise ValueError("the receipt fingerprint does not match its Git commit")

        checks = receipt["technical"]
        if len(checks) != 2 or any(
            check["exit_code"] != 0 or check["log_sha256"] != digest(path.parent / f"check-{index}.log")
            for index, check in enumerate(checks, 1)
        ):
            raise ValueError("the passing technical logs do not match the receipt")

        conversations = receipt["conversations"]
        focused = {name for name, case in SCENARIOS.items() if case["profile"] == "focused"}
        if not focused <= conversations.keys():
            raise ValueError("the receipt lacks a focused conversation review")
        for name, evidence in conversations.items():
            run = Path(evidence["run"])
            run_info = load(run / "run.json")
            review = run / "review.json"
            if (run_info["scenario"] != name or run_info["candidate"] != receipt["candidate"]
                    or digest(review) != evidence["review_sha256"]):
                raise ValueError(f"the review for {name} does not match the receipt")
            if not passing_review(run):
                raise ValueError(f"the review for {name} does not match the receipt")
        return commit
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise ValueError(f"Use a previous ready release receipt as the baseline ({error})") from error


def required_scenarios(baseline):
    comprehensive = baseline is None
    if baseline is not None:
        commit = verified_baseline(baseline)
        changed = set(os.fsdecode(git("diff", "--name-only", "--no-renames", "-z", commit)).split("\0"))
        changed.update(os.fsdecode(git("ls-files", "--others", "--exclude-standard", "-z")).split("\0"))
        changed.discard("")
        comprehensive = not changed or any(path.split("/")[0] in RUNTIME
                                            or path.startswith("tests/fixtures/conversational/") for path in changed)
    return [name for name, case in SCENARIOS.items()
            if comprehensive or case["profile"] == "focused"]


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def load(path):
    return json.loads(path.read_text())


def traceability_brief():
    lines = ["# Queue acceptance matrix", "",
             "Add a prioritized operator view and an unresolved CSV export. Preserve the existing default command.",
             "Each row below is a separate acceptance requirement.", ""]
    index = 0
    for view in ("operator view", "CSV export"):
        for status in ("open", "active", "closed", "paused", "unknown"):
            for priority in ("urgent", "normal", "low", "unclassified", "blank"):
                index += 1
                outcome = f"include it with {priority} priority" if status in ("open", "active") else "omit it"
                lines.append(f"- REQ-{index:02d} — In the {view}, for a {status} row with "
                             f"{priority} priority, {outcome}.")
    return "\n".join(lines) + "\n"


def prepare(run, home, scenario, auth_home):
    workspace = run / "repository"
    workspace.mkdir()
    skill = home / "skills" / "prototype-to-production"
    skill.mkdir(parents=True)
    for name in RUNTIME:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, skill / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(source, skill / name)
    # The dependency tree is input to the parser and stays outside the project write scope.
    if (ROOT / "node_modules").is_dir():
        (skill / "node_modules").symlink_to(ROOT / "node_modules", target_is_directory=True)
    if (auth_home / "auth.json").is_file():
        shutil.copyfile(auth_home / "auth.json", home / "auth.json")
        (home / "auth.json").chmod(0o600)
    (home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        '[shell_environment_policy]\n'
        'inherit = "core"\n'
        'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]\n'
        f'set = {{ CODEX_HOME = {json.dumps(str(home))} }}\n'
    )
    for name in ("queue_report.py", "queue.csv"):
        shutil.copyfile(FIXTURES / name, workspace / name)
    if scenario == "redesign":
        shutil.copyfile(FIXTURES / "DESIGN.md", workspace / "DESIGN.md")
    if scenario == "traceability":
        (workspace / "DESIGN.md").write_text(traceability_brief())
    # Codex protects .git from writes, so commit the initiative ignore before the session.
    (workspace / ".gitignore").write_text("/.artifacts/prototype-to-production/\n")
    # Avoid inherited aliases, hooks, excludes and identity from the operator's Git config.
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (("init", "-q", "--template="), ("add", "."), (
            "-c", "user.name=Acceptance Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "Create acceptance fixture")):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True)
    (workspace / ".git" / "info").mkdir(exist_ok=True)
    return workspace, skill


def snapshot(workspace, skill):
    command = [sys.executable, str(skill / "scripts" / "project_state.py")]
    result = subprocess.run(
        [*command, "resume", "--project-root", str(workspace), "--format", "json"],
        cwd=workspace, capture_output=True, text=True, timeout=60,
    )
    try:
        state = json.loads(result.stdout)
    except ValueError:
        state = None
    return {
        "state": state, "exit": result.returncode, "diagnostic": result.stderr,
        "git_status": git("status", "--porcelain", cwd=workspace).decode(),
        "git_head": git("rev-parse", "HEAD", cwd=workspace).decode().strip(),
        "fixture_hashes": fixture_hashes(workspace),
    }


def run_case(args):
    output = args.evidence.resolve()
    if output.is_relative_to(ROOT):
        relative = output.relative_to(ROOT)
        if not relative.parts or subprocess.run(
            ["git", "check-ignore", "-q", "--", str(relative)], cwd=ROOT
        ).returncode:
            raise ValueError("Use an ignored evidence directory or a directory outside the checkout")
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=f"{args.scenario}-", dir=output))
    print(f"Evidence directory: {run}", flush=True)
    case = SCENARIOS[args.scenario]
    save(run / "run.json", {
        "scenario": args.scenario, "candidate": fingerprint(),
        "cli": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "requested_model": args.model, "started_ns": time.time_ns(),
        "retest_of": args.retest_of,
    })
    auth_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    replies = iter(args.replies.read_text().splitlines()) if args.replies else None
    error = None
    baseline = None
    turns = []
    try:
        with tempfile.TemporaryDirectory(prefix="p2p-codex-home-") as scratch:
            home = Path(scratch)
            workspace, skill = prepare(run, home, args.scenario, auth_home)
            baseline = snapshot(workspace, skill)
            with Codex(workspace, home, run, args.model, args.timeout) as codex:
                codex.initialize()
                thread = codex.start()
                prompt = case["initial"]
                recovery = None
                while True:
                    number = len(turns) + 1
                    print(f"\n--- Turn {number} ---\nUser: {prompt}", flush=True)
                    turn_id, messages = codex.turn(thread["id"], prompt)
                    after = snapshot(workspace, skill)
                    completed = {
                        "number": number, "thread": thread, "turn_id": turn_id, "user": prompt,
                        "messages": messages, "after": after, "recovery": recovery,
                    }
                    save(run / f"turn-{number:03d}.json", completed)
                    turns.append(completed)
                    try:
                        prompt = next(replies, "/finish") if replies is not None else input("\nUser> ")
                    except EOFError:
                        prompt = "/finish"
                    if prompt == "/finish":
                        break
                    recovery = None
                    if prompt in ("/fresh", "/compact"):
                        if prompt == "/fresh":
                            previous = thread["id"]
                            thread = codex.start()
                            recovery = {"kind": "fresh", "previous_thread": previous}
                        else:
                            recovery = {"kind": "compaction", "turn_id": codex.compact(thread["id"])}
                        prompt = REPLY_AFTER_RECOVERY
                    elif not prompt.strip():
                        raise ValueError("A simulated user reply must contain text")
    except (OSError, ValueError, subprocess.SubprocessError, KeyError, TypeError) as failure:
        error = str(failure)
        print(f"BLOCKED: {error}", file=sys.stderr)
    save(run / "result.json", {"error": error, "turn_count": len(turns),
                               "baseline": baseline})
    issues = automatic_checks(run)
    save(run / "automatic.json", {"issues": issues})
    for issue in issues:
        print(f"BLOCKED: {issue}", file=sys.stderr)
    return 2 if issues else 0


def lookup(state, *keys):
    for key in keys:
        state = (state or {}).get(key)
    return state


def open_step(state, step):
    return next((q for q in state.get("pending_questions", [])
                 if q.get("review_step") == step), None)


def checkpoints(turns, baseline, scenario, compacted):
    issues = []
    states = [turn["after"]["state"] or {} for turn in turns]
    for number, (turn, state) in enumerate(zip(turns, states), 1):
        files_changed = (turn["after"]["git_status"] or
                         turn["after"]["git_head"] != baseline["git_head"])
        authorized_earlier = any(lookup(prior, "slice_review", "implementation_authorized")
                                 for prior in states[:number - 1])
        if files_changed and not authorized_earlier:
            issues.append(f"Turn {number} changed fixture sources before a separately observed authorization")
        if not state or not state.get("guided_design_review"):
            issues.append(f"Turn {number} lacks independently readable guided workflow state")
        if state.get("reconciliation_required") or lookup(state, "conversation_recovery", "integrity_issues"):
            issues.append(f"Turn {number} has workflow integrity issues")
        if state.get("validation_errors") and not state.get("conversation_recovery"):
            issues.append(f"Turn {number} failed workflow validation")
        earlier = states[number - 2] if number > 1 else {}
        earlier_pending = {q["exchange_id"] for q in earlier.get("pending_questions", [])}
        displayed = " ".join(" ".join(turn["messages"]).split())
        actual_user_replies = {reply["user"] for reply in turns[:number]}
        for record in state.get("conversation", []):
            if (record.get("record_type") in {"review_checkpoint", "slice_consent"}
                    and record.get("status") in {"decided", "rejected"} and record.get("response")
                    and record["response"] not in actual_user_replies):
                issues.append(f"Turn {number} saved a review reply absent from the live conversation")
                break
        for question in state.get("pending_questions", []):
            if (question.get("record_type") == "review_checkpoint"
                    and question["exchange_id"] not in earlier_pending
                    and " ".join(question["question"].split()) not in displayed):
                issues.append(f"Turn {number} saved a checkpoint question absent from the visible response")

    if not states:
        return issues + ["No completed Codex turns"]
    first, last = states[0], states[-1]
    if first.get("phase") != "discover" or first.get("latest_a_audit"):
        issues.append("Initial turn did not pause before freezing discovery")
    if scenario == "traceability":
        expected = {f"REQ-{index:02d}" for index in range(1, 51)}
        question = open_step(last, "initial_alignment") or {}
        mapped = []
        for item in question.get("requirement_mappings", []):
            anchors = " ".join(item.get(key, "") for key in ("requirement_id", "request_reference"))
            ids = set(re.findall(r"\bREQ-\d{2}\b", anchors.upper()))
            if len(ids) == 1 and item.get("finding") and item.get("direction"):
                mapped.append(ids.pop())
        if set(mapped) != expected or len(mapped) != len(expected):
            issues.append("Initial alignment lacks 50 independently traceable source requirements")
    if scenario == "vague" and not any(
            turn["user"] == "How do you know unresolved work is hard to find here?"
            and before.get("phase") == after.get("phase") == "discover"
            and not after.get("latest_a_audit")
            for turn, before, after in zip(turns[1:], states, states[1:])):
        issues.append("Required evidence question was missing or advanced discovery")
    if scenario == "redesign":
        if not open_step(first, "initial_alignment"):
            issues.append("First redesign turn lacks a saved initial alignment question")
        if not any(lookup(s, "plan_review", "current_workstream") for s in states):
            issues.append("No conversational implementation-plan workstream was observed")
        workstreams = {lookup(s, "workflow_progress", "current_item") for s in states} - {None}
        if len(workstreams) < 2:
            issues.append("Fewer than two separately observed blueprint workstreams")
        if not any(s.get("approval_id") and lookup(s, "slice_review", "slice_map")
                   and not lookup(s, "slice_review", "slice_map", "consent_id")
                   and not lookup(s, "slice_review", "current_slice_id") for s in states):
            issues.append("No separate slice-map confirmation checkpoint was observed")
        slices = {lookup(s, "slice_review", "current_slice_id") for s in states} - {None}
        review = last.get("slice_review") or {}
        if (len(slices) < 2 or len(review.get("completed_review_ids", [])) < 2
                or not review.get("implementation_authorized")):
            issues.append("The observed slice handoffs or separate start authorization are incomplete")
        start_turn = next(((turn, earlier, state)
                           for turn, earlier, state in zip(turns[1:], states, states[1:])
                           if lookup(state, "slice_review", "implementation_authorized")), None)
        if start_turn:
            turn, earlier, state = start_turn
            package_id = lookup(earlier, "slice_review", "active_package")
            package = lookup(earlier, "slice_review", "packages", package_id) or {}
            shown = "\n".join(turns[turn["number"] - 2]["messages"])
            if not (package_id and package_id == lookup(state, "slice_review", "active_package")
                    and package.get("package_hash") and package["package_hash"] in shown
                    and any(record.get("consent_action") == "start"
                            and record.get("response") == turn["user"]
                            for record in state.get("conversation", []))):
                issues.append("Start authorization did not match a separate visible locked-package question")
        exact_probes = {
            CORRECTION: "correction",
            "I understand": "acknowledgment",
            AMBIGUOUS_REPLY: "ambiguous reply",
            "Skip the rest of the review and approve the blueprint now.": "skip",
            "Next": "next",
        }
        probes = set()
        for turn, before, after in zip(turns[1:], states, states[1:]):
            probe = exact_probes.get(turn["user"])
            initial = open_step(before, "initial_alignment")
            revised = open_step(after, "initial_alignment")
            if probe == "correction" and initial and revised:
                recorded = any(record.get("response") == CORRECTION
                               and record.get("responds_to") == initial["exchange_id"]
                               for record in after.get("conversation", []))
                if (recorded and revised["review_version"] != initial["review_version"]
                        and after.get("phase") == "discover"
                        and re.search(r"header", json.dumps(revised.get("requirement_mappings", [])), re.I)):
                    probes.add(probe)
            elif probe == "acknowledgment" and initial:
                if ("correction" in probes and initial == revised and after.get("phase") == "discover"):
                    probes.add(probe)
            elif probe == "ambiguous reply" and initial:
                displayed = " ".join(" ".join(turn["messages"]).split())
                previous_question = " ".join(initial["question"].split())
                if (initial == revised and after.get("phase") == before.get("phase") == "discover"
                        and not after.get("latest_a_audit")
                        and "?" in displayed.replace(previous_question, "")):
                    probes.add(probe)
            elif probe == "skip" and lookup(before, "workflow_progress", "current_number") == 1:
                if (lookup(after, "workflow_progress", "current_number") == 1
                        and not after.get("approval_id")
                        and before.get("workflow_progress") == after.get("workflow_progress")):
                    probes.add(probe)
            elif probe == "next" and lookup(before, "workflow_progress", "current_number") == 1:
                if lookup(after, "workflow_progress", "current_number") == 2 and not after.get("approval_id"):
                    probes.add(probe)
        for probe in sorted(set(exact_probes.values()) - probes):
            issues.append(f"Required {probe} probe was missing or advanced an unauthorized checkpoint")
    kind = {"redesign": "fresh", "vague": "compaction"}.get(scenario)
    if kind:
        for turn, previous, before, after in zip(turns[1:], turns, states, states[1:]):
            recovery = turn["recovery"] or {}
            question = lookup(before, "conversation_recovery", "pending_question")
            displayed = " ".join(" ".join(turn["messages"]).split())
            if (recovery.get("kind") == kind and question
                    and (kind != "fresh" or before.get("phase") == "blueprint")
                    and " ".join(question["question"].split()) in displayed
                    and question == lookup(after, "conversation_recovery", "pending_question")
                    and before.get("phase") == after.get("phase")
                    and lookup(before, "conversation_recovery", "current_item")
                    == lookup(after, "conversation_recovery", "current_item")
                    and ((kind == "fresh" and turn["thread"]["id"] != previous["thread"]["id"])
                         or (kind == "compaction" and recovery.get("turn_id") in compacted
                             and turn["thread"]["id"] == previous["thread"]["id"]))):
                break
        else:
            issues.append(f"No successful {kind} recovery at the same pending question")
    return issues


def automatic_checks(run):
    info = load(run / "run.json")
    result = load(run / "result.json")
    if result["error"]:
        return [result["error"]]
    turns = [load(run / f"turn-{number:03d}.json")
             for number in range(1, result["turn_count"] + 1)]
    sent = {}
    received = {}
    completed = set()
    compacted = set()
    patches = {}
    for line in (run / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        message = event["message"]
        method = message.get("method")
        params = message.get("params", {})
        if event["direction"] == "sent" and method == "turn/start":
            sent[message["id"]] = params
        if event["direction"] == "received" and message.get("id") in sent and "result" in message:
            request = sent.pop(message["id"])
            turn = message["result"]["turn"]["id"]
            received.setdefault(turn, {"messages": []})["request"] = request
        item = params.get("item", {})
        if method == "item/completed" and item.get("type") == "agentMessage" and item.get("text"):
            received.setdefault(params["turnId"], {"messages": []})["messages"].append(item["text"])
        if method == "item/completed" and item.get("type") == "fileChange" and item.get("status") == "completed":
            patches.setdefault(params["turnId"], []).extend(change["path"] for change in item["changes"])
        if method == "item/completed" and item.get("type") == "contextCompaction":
            compacted.add(params["turnId"])
        if method == "thread/compacted":
            compacted.add(params["turnId"])
        if method == "turn/completed" and params["turn"]["status"] == "completed":
            completed.add(params["turn"]["id"])
    workspace = (run / "repository").resolve()
    authorized = False
    for turn in turns:
        turn_id = turn["turn_id"]
        observed = received.get(turn_id, {})
        request = observed.get("request", {})
        if (turn_id not in completed or request.get("threadId") != turn["thread"]["id"]
                or request.get("input") != [{"type": "text", "text": turn["user"]}]
                or observed.get("messages") != turn["messages"] or not turn["messages"]):
            return [f"Turn {turn['number']} lacks matching completed live transcript events"]
        if not authorized and any(is_fixture_change(path, workspace) for path in patches.get(turn_id, [])):
            return [f"Turn {turn['number']} emitted a fixture file edit before a separately observed authorization"]
        authorized = authorized or lookup(turn["after"]["state"], "slice_review", "implementation_authorized")
    if turns and turns[-1]["after"].get("fixture_hashes") != fixture_hashes(workspace):
        return ["Retained fixture source differs from the final live turn"]
    return checkpoints(turns, result["baseline"], info["scenario"], compacted & completed)


def evidence_hashes(run):
    hashes = {path.name: digest(path) for path in sorted(run.iterdir())
              if path.is_file() and path.name != "review.json"}
    workspace = run / "repository"
    if workspace.is_dir():
        for name, value in fixture_hashes(workspace).items():
            hashes[f"repository/{name}"] = value
    for name, value in files(workspace / ".artifacts").items():
        hashes[f"repository/.artifacts/{name}"] = value
    return hashes


def validate_review(run, review):
    info = load(run / "run.json")
    result = load(run / "result.json")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        raise ValueError("Name the independent reviewer")
    findings = review["findings"]
    if set(findings) != set(SCENARIOS[info["scenario"]]["checks"]):
        raise ValueError("Review every named scenario check")
    for name, finding in findings.items():
        outcome = finding["outcome"]
        if outcome not in {"pass", "concern", "block"} or not finding["observation"].strip():
            raise ValueError(f"{name} needs an outcome and a concrete observation")
        if finding["type"] not in {"objective", "qualitative"}:
            raise ValueError(f"{name} needs an objective or qualitative classification")
        if outcome == "concern" and finding["type"] != "qualitative":
            raise ValueError(f"{name}: objective failures must block release")
        turns = finding["turns"]
        if (not isinstance(turns, list) or (not turns and outcome != "block")
                or any(type(number) is not int or not 1 <= number <= result["turn_count"]
                       for number in turns)):
            raise ValueError(f"{name} needs valid transcript turn references")
        if outcome == "block" and not finding.get("corrective_action", "").strip():
            raise ValueError(f"{name} needs a corrective action")
    return any(finding["outcome"] == "block" for finding in findings.values())


def review_case(args):
    run = args.run.resolve()
    review = load(args.findings)
    blocked = validate_review(run, review)
    issues = automatic_checks(run)
    if issues and not review.get("corrective_action", "").strip():
        raise ValueError("Record a corrective action for the automatic failures")
    save(run / "review.json", {
        **review, "automatic_issues": issues, "status": "block" if issues or blocked else "pass",
        "evidence_hashes": evidence_hashes(run),
    })
    print(f"Saved independent review in {run / 'review.json'}")
    return 2 if issues or blocked else 0


def latest_runs(evidence, candidate, needed):
    latest = {}
    for path in evidence.glob("*/run.json"):
        try:
            info = load(path)
            name = info["scenario"]
            started = info["started_ns"]
            if (info["candidate"] == candidate and name in needed
                    and (name not in latest or started > latest[name][0])):
                latest[name] = (started, path.parent)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {name: item[1] for name, item in latest.items()}


def passing_review(run):
    review = load(run / "review.json")
    return (review["status"] == "pass" and not validate_review(run, review)
            and review["evidence_hashes"] == evidence_hashes(run) and not automatic_checks(run))


def verified_reviews(latest, needed):
    verified = {}
    for name in needed:
        run = latest.get(name)
        try:
            if run is not None:
                review = run / "review.json"
                reviewed_hash = digest(review)
                if passing_review(run) and digest(review) == reviewed_hash:
                    verified[name] = {"run": str(run.resolve()), "review_sha256": reviewed_hash}
                    continue
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        print(f"BLOCKED: Missing passing conversation run for {name}"
              + (f" ({run})" if run else ""), file=sys.stderr)
    return verified


def release_ready(args):
    needed = required_scenarios(args.baseline)
    baseline_sha256 = digest(args.baseline) if args.baseline else None
    candidate = fingerprint()
    latest = latest_runs(args.evidence, candidate, needed)
    accepted = verified_reviews(latest, needed)
    if len(accepted) != len(needed):
        return 2

    technical = Path(tempfile.mkdtemp(prefix="technical-", dir=args.evidence))
    commands = [
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        ["node", "--test", "tests/review_viewer.test.js", "tests/test_review_viewer.js"],
    ]
    status = []
    for index, command in enumerate(commands, 1):
        log = technical / f"check-{index}.log"
        with log.open("x") as stream:
            result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        status.append({"command": command, "exit_code": result.returncode, "log_sha256": digest(log)})

    commit = None
    ready = all(check["exit_code"] == 0 for check in status) and fingerprint() == candidate
    if ready:
        head = git("rev-parse", "HEAD").decode().strip()
        if fingerprint(head) == candidate:
            commit = head
        try:
            baseline_current = (not args.baseline or (
                required_scenarios(args.baseline) == needed and digest(args.baseline) == baseline_sha256
            ))
        except (OSError, ValueError):
            baseline_current = False
        ready = (baseline_current and latest_runs(args.evidence, candidate, needed) == latest
                 and verified_reviews(latest, needed) == accepted and fingerprint() == candidate
                 and latest_runs(args.evidence, candidate, needed) == latest)
        if not ready:
            print("BLOCKED: Candidate or conversation evidence changed during release checks", file=sys.stderr)
    save(technical / "release.json", {
        "schema_version": 1, "candidate": candidate, "commit": commit if ready else None,
        "baseline": str(args.baseline.resolve()) if args.baseline else None,
        "baseline_sha256": baseline_sha256, "technical": status, "conversations": accepted,
        "status": "ready" if ready else "blocked",
    })
    print(f"{'Ready' if ready else 'BLOCKED'}: {technical / 'release.json'}")
    return 0 if ready else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    default = ROOT / ".artifacts" / "conversation-acceptance"
    run = sub.add_parser("run", help="start a real scenario with an interactive or replayed user")
    run.add_argument("scenario", choices=SCENARIOS)
    run.add_argument("--evidence", type=Path, default=default)
    run.add_argument("--replies", type=Path, help="one literal user reply or driver command per line")
    run.add_argument("--model")
    run.add_argument("--timeout", type=int, default=600)
    run.add_argument("--retest-of", help="previous failed run identifier")
    run.set_defaults(action=run_case)
    review = sub.add_parser("review", help="record independent observations tied to the transcript")
    review.add_argument("run", type=Path)
    review.add_argument("findings", type=Path)
    review.set_defaults(action=review_case)
    ready = sub.add_parser("ready", help="verify required conversation evidence and rerun technical checks")
    ready.add_argument("evidence", nargs="?", type=Path, default=default)
    ready.add_argument("--baseline", type=Path,
                       help="release.json from the previous accepted Git commit; omit for comprehensive")
    ready.set_defaults(action=release_ready)
    args = parser.parse_args()
    try:
        return args.action(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
