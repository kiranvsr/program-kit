#!/usr/bin/env python3
"""Status state machine (spec 4.3). The only writer of program/status/<ID>.json.

A task with no record is `not_started`. Every transition is checked in full
before anything is written, so a refusal leaves no file behind. `done` is
terminal and needs an acceptance file bound to the task's current results.json.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

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

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_FIELDS = ("task_id", "status", "role", "updated_at", "note", "evidence", "blocked_by", "history")


def _check_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _TASK_ID.match(task_id):
        raise KitError(f"invalid task id {task_id!r}: use letters, digits, '.', '_' and '-'")
    return task_id


def _record_path(root: Path, task_id: str) -> Path:
    return Path(root) / "program" / "status" / f"{_check_id(task_id)}.json"


def _norm(root: Path, path: str) -> str:
    """Evidence path as stored: relative to root, posix separators, no `./` or `..` detours."""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(Path(root).resolve())
        except ValueError:
            return candidate.as_posix()
    parts = [part for part in candidate.as_posix().split("/") if part not in ("", ".")]
    return "/".join(parts)


def _blank(task_id: str) -> dict:
    return {"task_id": task_id, "status": "not_started", "role": "", "updated_at": "", "note": "",
            "evidence": [], "blocked_by": [], "history": []}


def read(root: Path, task_id: str) -> dict:
    path = _record_path(root, task_id)
    if not path.exists():
        return _blank(task_id)
    record = read_json(path)
    if not isinstance(record, dict):
        raise KitError(f"{path}: not a status record")
    return record


def list_all(root: Path) -> list[dict]:
    folder = Path(root) / "program" / "status"
    if not folder.is_dir():
        return []
    return [read(root, path.stem) for path in sorted(folder.glob("*.json"))]


def _acceptance_problem(root: Path, task_id: str, evidence: list[str]) -> str | None:
    """Why `evidence` does not accept `task_id`, or None when the acceptance binds the current results."""
    acceptance = f"program/evidence/{task_id}/acceptance.json"
    results = f"program/evidence/{task_id}/results.json"
    if acceptance not in [_norm(root, item) for item in evidence]:
        return f"{task_id}: done needs {acceptance} among the evidence paths"
    bound = read_json(Path(root) / acceptance)
    claimed = bound.get("evidence_sha256") if isinstance(bound, dict) else None
    if not isinstance(claimed, str) or not claimed:
        return f"{task_id}: {acceptance} has no evidence_sha256"
    actual = sha256_file(Path(root) / results)
    if claimed != actual:
        return (f"{task_id}: {acceptance} binds evidence_sha256 {claimed} but {results} hashes to {actual}; "
                f"acceptance is stale")
    return None


def transition(root: Path, task_id: str, to: str, role: str, note: str = "",
               evidence: list[str] | None = None, blocked_by: list[str] | None = None) -> dict:
    root = Path(root)
    _check_id(task_id)
    if to not in STATUSES:
        raise KitError(f"unknown status {to!r}; statuses are {', '.join(STATUSES)}")
    if role not in ROLES:
        raise KitError(f"unknown role {role!r}; roles are {', '.join(ROLES)}")
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
        if not isinstance(record, dict):
            problems.append(f"{name}: not a status record (expected a JSON object)")
            continue
        missing = [field for field in _FIELDS if field not in record]
        if missing:
            problems.append(f"{name}: missing field(s) {', '.join(missing)}")
            continue
        if record["task_id"] != name:
            problems.append(f"{name}: task_id {record['task_id']!r} does not match the file name")
        state = record["status"]
        if state not in STATUSES:
            problems.append(f"{name}: unknown status {state!r}")
            continue
        evidence = record["evidence"] if isinstance(record["evidence"], list) else []
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
