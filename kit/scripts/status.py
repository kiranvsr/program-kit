#!/usr/bin/env python3
"""Status state machine (spec 4.3). The only writer of program/status/<ID>.json.

A task with no record is `not_started`. Every transition is checked in full
before anything is written, so a refusal leaves no file behind, and only a task
the register lists can be given a status. `done` is terminal and needs an
acceptance file that accepts (`decision: accepted_development`), names this
task, and is bound by hash to a current results.json the hand-back gate would
pass. The role is a flag the caller passes: the machine checks which role may
make a move, not who is typing.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import evidence as evidence_module
import register as register_module
from common import KitError, find_root, read_json, sha256_file, utc_now, write_json

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

    A `..` component or an absolute path outside the root is refused.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(Path(root).resolve())
        except ValueError:
            raise KitError(f"evidence path {path} is outside the project root") from None
    parts = [part for part in candidate.as_posix().split("/") if part not in ("", ".")]
    if ".." in parts:
        raise KitError(f"evidence path {path} must not contain '..'")
    return "/".join(parts)


def _blank(task_id: str) -> dict:
    return {"task_id": task_id, "status": "not_started", "role": "", "updated_at": "", "note": "",
            "evidence": [], "blocked_by": [], "history": []}


def _shape_problem(record) -> str | None:
    """Why `record` is not a usable status record, or None. Shared by read and validate."""
    if not isinstance(record, dict):
        return "not a status record (expected a JSON object)"
    missing = [field for field in _FIELDS if field not in record]
    if missing:
        return f"missing field(s) {', '.join(missing)}"
    if not isinstance(record["status"], str) or record["status"] not in STATUSES:
        return f"unknown status {record['status']!r}"
    for field in _TEXT_FIELDS:
        if not isinstance(record[field], str):
            return f"{field} must be text, not {record[field]!r}"
    for field in ("evidence", "blocked_by", "history"):
        if not isinstance(record[field], list):
            return f"{field} must be a list"
    for field in ("evidence", "blocked_by"):
        if not all(isinstance(item, str) for item in record[field]):
            return f"{field} must be a list of text items"
    for number, entry in enumerate(record["history"], 1):
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in _ENTRY_FIELDS):
            return f"history entry {number} must be an object with text {', '.join(_ENTRY_FIELDS)}"
    return None


def read(root: Path, task_id: str) -> dict:
    path = _record_path(root, task_id)
    if not path.exists():
        return _blank(task_id)
    record = read_json(path)
    problem = _shape_problem(record)
    if problem:
        raise KitError(f"{path}: {problem}")
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


def _acceptance_problem(root: Path, task_id: str, evidence: list[str]) -> str | None:
    """Why `evidence` does not accept `task_id`, or None.

    None only when the acceptance file is listed, has every key acceptance.schema.json requires, accepts
    (`decision` is `accepted_development`), is QA's and this task's, binds the current results.json by hash,
    and that results.json passes the checks of the hand-back gate (`evidence.gate_problems`).
    """
    acceptance = f"program/evidence/{task_id}/acceptance.json"
    results = f"program/evidence/{task_id}/results.json"
    if acceptance not in [_norm(root, item) for item in evidence]:
        return f"{task_id}: done needs {acceptance} among the evidence paths"
    bound = read_json(Path(root) / acceptance)
    missing = [key for key in evidence_module.required_keys("acceptance")
               if not isinstance(bound, dict) or key not in bound]
    if missing:
        return f"{task_id}: {acceptance}: missing {', '.join(missing)}"
    if bound["decision"] != ACCEPTED:
        return f"{task_id}: {acceptance}: decision is {bound['decision']!r}, not {ACCEPTED!r}"
    for key, expected in (("task_id", task_id), ("role", "qa")):
        if bound[key] != expected:
            return f"{task_id}: {acceptance}: {key} is {bound[key]!r}, not {expected!r}"
    claimed = bound["evidence_sha256"]
    if not isinstance(claimed, str) or not claimed:
        return f"{task_id}: {acceptance} has no evidence_sha256"
    actual = sha256_file(Path(root) / results)
    if claimed != actual:
        return (f"{task_id}: {acceptance} binds evidence_sha256 {claimed} but {results} hashes to {actual}; "
                f"acceptance is stale")
    problems = evidence_module.gate_problems(read_json(Path(root) / results), task_id)
    if problems:
        more = f" (and {len(problems) - 1} more; run pk evidence validate)" if len(problems) > 1 else ""
        return f"{task_id}: {results}: {problems[0]}{more}"
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
        problem = _acceptance_problem(root, task_id, merged)
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
        problem = _shape_problem(record)
        if problem:
            problems.append(f"{name}: {problem}")
            continue
        if record["task_id"] != name:
            problems.append(f"{name}: task_id {record['task_id']!r} does not match the file name")
        state = record["status"]
        evidence = record["evidence"]
        for item in evidence:
            if not isinstance(item, str) or not (root / item).is_file():
                problems.append(f"{name}: evidence file missing: {item}")
        if state == "blocked" and not record["blocked_by"]:
            problems.append(f"{name}: blocked without blocked_by")
        if state == "done":
            try:
                problem = _acceptance_problem(root, name, [item for item in evidence if isinstance(item, str)])
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
        if problems:
            raise KitError("; ".join(problems))
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
