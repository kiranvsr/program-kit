#!/usr/bin/env python3
"""Status state machine (spec 4.3). The only writer of program/status/<ID>.json.

A task with no record is `not_started`. Every transition is checked in full
before anything is written, so a refusal leaves no file behind, and only a task
the register lists can be given a status. `done` is terminal and needs the task's
latest acceptance record (through `program/evidence/index.json`) to accept
(`decision: accepted_development`), to name this task, and to bind by digest the
task's latest results record, which the hand-back gate would pass and whose `head`
is an ancestor of HEAD. The role is a flag the caller passes: the machine checks
which role may make a move, not who is typing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import evidence as evidence_module
import register as register_module
import store
from common import KitError, find_root, read_json, run_argv, utc_now, write_json

STATUSES = ("not_started", "in_progress", "review", "done", "blocked", "parked")
ROLES = ("controller", "implementer", "qa")

# (from, to) -> roles allowed to make that move.
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("not_started", "in_progress"): {"controller"},
    ("in_progress", "review"): {"implementer"},
    ("review", "in_progress"): {"qa"},
    ("review", "done"): {"qa"},
    ("not_started", "blocked"): {"controller"},
    ("in_progress", "blocked"): {"controller"},
    ("blocked", "not_started"): {"controller"},
    ("blocked", "in_progress"): {"controller"},
    ("not_started", "parked"): {"controller"},
    ("parked", "not_started"): {"controller"},
}

ACCEPTED = "accepted_development"  # the accepting value of `decision` in acceptance.schema.json

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_FIELDS = ("task_id", "status", "role", "updated_at", "note", "evidence", "blocked_by", "history")
_TEXT_FIELDS = ("task_id", "role", "updated_at", "note")
_ENTRY_FIELDS = ("status", "role", "at")


def _check_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _TASK_ID.match(task_id):
        raise KitError(f"invalid task id {task_id!r}: use letters, digits, '.', '_' and '-'")
    return task_id


def _record_path(root: Path, task_id: str) -> Path:
    return Path(root) / "program" / "status" / f"{_check_id(task_id)}.json"


def _norm(root: Path, path: str) -> str:
    """Evidence path as stored: relative to root, posix separators, no `./` parts.

    A `..` component, an absolute path outside the root and a path that leaves the root through a symbolic
    link are refused. A link that stays inside the root is fine, and the path is stored as it was written.
    """
    base = Path(root).resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(base)
        except ValueError:
            raise KitError(f"evidence path {path} is outside the project root") from None
    parts = [part for part in candidate.as_posix().split("/") if part not in ("", ".")]
    if ".." in parts:
        raise KitError(f"evidence path {path} must not contain '..'")
    try:
        resolved = base.joinpath(*parts).resolve()
    except (OSError, RuntimeError, ValueError) as exc:  # a link that loops, or a name the system refuses
        raise KitError(f"evidence path {path} cannot be resolved: {getattr(exc, 'strerror', None) or exc}") from exc
    if resolved != base and base not in resolved.parents:
        raise KitError(f"evidence path {path} leaves the project root through a symbolic link")
    return "/".join(parts)


def _blank(task_id: str) -> dict:
    return {"task_id": task_id, "status": "not_started", "role": "", "updated_at": "", "note": "",
            "evidence": [], "blocked_by": [], "history": []}


def _entry_problems(entry) -> list[str]:
    """What is wrong with one history entry, in the terms of status.schema.json (empty when nothing is)."""
    if not isinstance(entry, dict):
        return ["must be an object"]
    problems = []
    missing = [key for key in _ENTRY_FIELDS if key not in entry]
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    for key, known in (("status", STATUSES), ("role", ROLES)):
        if key in entry and (not isinstance(entry[key], str) or entry[key] not in known):
            problems.append(f"unknown {key} {entry[key]!r}")
    if "at" in entry and not isinstance(entry["at"], str):
        problems.append(f"at must be text, not {entry['at']!r}")
    unknown = sorted(str(key) for key in entry if key not in _ENTRY_FIELDS)
    if unknown:
        problems.append(f"unknown field(s) {', '.join(unknown)}")
    return problems


def _shape_problems(record) -> list[str]:
    """Every reason `record` is not the record status.schema.json describes, one line each; empty when it is.

    Shared by read and validate. Nothing stops at the first problem: each field is checked on its own, down to
    the items of `evidence` and `blocked_by` and the entries of `history`.
    """
    if not isinstance(record, dict):
        return ["not a status record (expected a JSON object)"]
    problems = []
    missing = [field for field in _FIELDS if field not in record]
    if missing:
        problems.append(f"missing field(s) {', '.join(missing)}")
    unknown = sorted(str(key) for key in record if key not in _FIELDS)
    if unknown:
        problems.append(f"unknown field(s) {', '.join(unknown)}")
    if "status" in record and (not isinstance(record["status"], str) or record["status"] not in STATUSES):
        problems.append(f"unknown status {record['status']!r}")
    for field in _TEXT_FIELDS:
        if field in record and not isinstance(record[field], str):
            problems.append(f"{field} must be text, not {record[field]!r}")
    role = record.get("role")
    if isinstance(role, str) and role not in ROLES and not (role == "" and record.get("status") == "not_started"):
        problems.append(f"unknown role {role!r}")  # empty only on a record nobody has set yet
    for field in ("evidence", "blocked_by", "history"):
        if field not in record:
            continue
        if not isinstance(record[field], list):
            problems.append(f"{field} must be a list")
        elif field == "history":
            problems.extend(f"history entry {number}: {problem}" for number, entry in enumerate(record[field], 1)
                            for problem in _entry_problems(entry))
        else:
            problems.extend(f"{field} item {number} must be text, not {item!r}"
                            for number, item in enumerate(record[field], 1) if not isinstance(item, str))
    return problems


def read(root: Path, task_id: str) -> dict:
    path = _record_path(root, task_id)
    if not path.exists():
        return _blank(task_id)
    record = read_json(path)
    problems = _shape_problems(record)
    if problems:
        raise KitError(f"{path}: {'; '.join(problems)}")
    return record


def list_all(root: Path) -> list[dict]:
    folder = Path(root) / "program" / "status"
    if not folder.is_dir():
        return []
    return [read(root, path.stem) for path in sorted(folder.glob("*.json"))]


def _registered(root: Path) -> set[str]:
    """Every task ID in `program/register.yaml`; entries of the wrong shape are skipped, not followed."""
    def children(node, key: str) -> list:
        items = node.get(key) if isinstance(node, dict) else None
        return items if isinstance(items, list) else []

    path = Path(root) / "program" / "register.yaml"
    if not path.is_file():
        raise KitError("program/register.yaml does not exist; a status belongs to a task the register lists")
    found = set()
    for phase in register_module.load(root)["phases"]:
        for milestone in children(phase, "milestones"):
            for step in children(milestone, "steps"):
                found.update(task["id"] for task in children(step, "tasks")
                             if isinstance(task, dict) and isinstance(task.get("id"), str))
    return found


def _acceptance_problem(root: Path, task_id: str) -> str | None:
    """Why the task's evidence does not accept it, or None.

    None only when the index names a latest acceptance and a latest results record for the task, both present
    with their indexed digests; the acceptance has every key acceptance.schema.json requires, accepts
    (`decision` is `accepted_development`), is QA's and this task's, and binds the latest results record by
    digest; that record passes the checks of the hand-back gate (`evidence.gate_problems`); the acceptance's
    `source_commit` is the `head` the record names; and that `head` is an ancestor of HEAD. Drift since `head`
    does not matter: the record says what was verified there.
    """
    try:
        index = store.read_index(root)
    except KitError as exc:
        return f"{task_id}: {exc}"
    acceptance = store.latest(index, task_id, "acceptance")
    results = store.latest(index, task_id, "results")
    if acceptance is None:
        return f"{task_id}: done needs an acceptance record in {store.INDEX} (pk evidence accept)"
    if results is None:
        return f"{task_id}: done needs a results record in {store.INDEX}"
    label = f"latest acceptance {acceptance['path']}"
    bound = store.load(root, acceptance["path"], acceptance["digest"])
    missing = [key for key in evidence_module.required_keys("acceptance") if key not in bound]
    if missing:
        return f"{task_id}: {label}: missing {', '.join(missing)}"
    if bound["decision"] != ACCEPTED:
        return f"{task_id}: {label}: decision is {bound['decision']!r}, not {ACCEPTED!r}"
    for key, expected in (("task_id", task_id), ("role", "qa")):
        if bound[key] != expected:
            return f"{task_id}: {label}: {key} is {bound[key]!r}, not {expected!r}"
    claimed = bound["evidence_sha256"]
    if claimed != results["digest"]:
        return (f"{task_id}: acceptance does not bind the latest results record: {label} binds {claimed!r}, "
                f"the latest results record is {results['digest']}")
    record = store.load(root, results["path"], results["digest"])
    problems = evidence_module.gate_problems(record, task_id, root=root)
    if problems:
        more = f" (and {len(problems) - 1} more; run pk evidence validate)" if len(problems) > 1 else ""
        return f"{task_id}: {results['path']}: {problems[0]}{more}"
    if bound["source_commit"] != record["head"]:
        return (f"{task_id}: {label}: source_commit is {bound['source_commit']!r}, not {record['head']}, "
                f"the head {results['path']} names")
    code, _ = run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", record["head"], "HEAD"], Path(root))
    if code != 0:
        return f"{task_id}: {results['path']}: head {record['head']} is not an ancestor of HEAD"
    return None


def transition(root: Path, task_id: str, to: str, role: str, note: str = "",
               evidence: list[str] | None = None, blocked_by: list[str] | None = None) -> dict:
    root = Path(root)
    _check_id(task_id)
    if to not in STATUSES:
        raise KitError(f"unknown status {to!r}; statuses are {', '.join(STATUSES)}")
    if role not in ROLES:
        raise KitError(f"unknown role {role!r}; roles are {', '.join(ROLES)}")
    if task_id not in _registered(root):
        raise KitError(f"{task_id}: not a task in program/register.yaml")
    record = read(root, task_id)
    current = record["status"]
    if current == "done":
        raise KitError(f"{task_id} is done and done is terminal; a change request creates a new task")
    allowed = TRANSITIONS.get((current, to))
    if allowed is None:
        raise KitError(f"{task_id}: {current} -> {to} is not allowed")
    if role not in allowed:
        raise KitError(f"{task_id}: {current} -> {to} is for {' or '.join(sorted(allowed))}, not {role}")
    blockers = list(blocked_by or [])
    if to == "blocked" and not blockers:
        raise KitError(f"{task_id}: blocked needs blocked_by (what it waits on)")
    if to != "blocked" and blockers:
        raise KitError(f"{task_id}: blocked_by only applies when moving to blocked")
    if to == "parked" and not note.strip():
        raise KitError(f"{task_id}: parked needs a note naming the user decision it waits on")
    merged = list(record["evidence"])
    for item in evidence or []:
        item = _norm(root, item)
        if item and item not in merged:
            merged.append(item)
    if to == "done":
        problem = _acceptance_problem(root, task_id)
        if problem:
            raise KitError(problem)
    now = utc_now()
    updated = {"task_id": task_id, "status": to, "role": role, "updated_at": now, "note": note,
               "evidence": merged, "blocked_by": blockers,
               "history": [*record["history"], {"status": to, "role": role, "at": now}]}
    write_json(_record_path(root, task_id), updated)
    return updated


def validate(root: Path) -> list[str]:
    """Problems with the stored records, one line each; empty when all is well."""
    root = Path(root)
    folder = root / "program" / "status"
    problems: list[str] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        name = path.stem
        try:
            record = read_json(path)
        except KitError as exc:
            problems.append(f"{name}: {exc}")
            continue
        shape = _shape_problems(record)
        if shape:
            problems.extend(f"{name}: {problem}" for problem in shape)
            continue
        if record["task_id"] != name:
            problems.append(f"{name}: task_id {record['task_id']!r} does not match the file name")
        state = record["status"]
        evidence = []  # the stored paths that stay inside the project
        for item in record["evidence"]:
            try:
                _norm(root, item)
            except KitError as exc:
                problems.append(f"{name}: {exc}")
                continue
            evidence.append(item)
            if not (root / item).is_file():
                problems.append(f"{name}: evidence file missing: {item}")
        if state == "blocked" and not record["blocked_by"]:
            problems.append(f"{name}: blocked without blocked_by")
        if state == "done":
            try:
                problem = _acceptance_problem(root, name)
            except KitError as exc:
                problem = f"{name}: {exc}"
            if problem:
                problems.append(problem)
    return problems


def _root() -> Path:
    return find_root()


def cmd_status(args: argparse.Namespace) -> int:
    action = getattr(args, "status_action", None)
    if action == "set":
        record = transition(_root(), args.task_id, args.status, args.role, note=args.note,
                            evidence=args.evidence, blocked_by=args.blocked_by)
        print(f"{record['task_id']}: {record['status']}")
    elif action == "show":
        print(json.dumps(read(_root(), args.task_id), indent=2))
    elif action == "list":
        for record in list_all(_root()):
            print("\t".join([record["task_id"], record["status"], record["role"], record["updated_at"]]))
    elif action == "validate":
        problems = validate(_root())
        for problem in problems:  # each on its own line, like every other check that can find several
            print("error: " + " ".join(problem.split()), file=sys.stderr)
        if problems:
            return 1
        print("status: ok")
    else:
        raise KitError("status needs an action: set, show, list or validate")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="status_action", metavar="action", parser_class=type(parser))
    setter = actions.add_parser("set", help="move a task to a new status")
    setter.add_argument("task_id")
    setter.add_argument("status")
    setter.add_argument("--role", required=True, help="who is moving it: " + ", ".join(ROLES))
    setter.add_argument("--note", default="")
    setter.add_argument("--evidence", action="append", default=[], metavar="PATH")
    setter.add_argument("--blocked-by", action="append", default=[], metavar="ID", dest="blocked_by")
    actions.add_parser("show", help="print one record").add_argument("task_id")
    actions.add_parser("list", help="one line per task")
    actions.add_parser("validate", help="check every record")


def register(add_command) -> None:
    add_command("status", cmd_status, help="task status state machine", configure=_configure)
