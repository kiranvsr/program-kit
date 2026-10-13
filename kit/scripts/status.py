#!/usr/bin/env python3
"""Status state machine (spec 4.3). The only writer of program/status/<ID>.json.

A task with no record is `not_started`. Every transition is checked in full
before anything is written, so a refusal leaves no file behind, and only a task
the register lists can be given a status. `done` is terminal and needs the task's
latest acceptance record (through its pointer `program/evidence/<ID>/latest.json`) to accept
(`decision: accepted_development`), to name this task, and to bind by digest the
task's latest results record, which the hand-back gate would pass and whose `head`
is an ancestor of HEAD. The role is a flag the caller passes: the machine checks
which role may make a move, not who is typing.

Every record carries the task's verification tier (`full`, `standard` or `light`; a record written before
tiers existed reads as `full`), whether it runs on the fast path (a brief and a ruling in place of SPEC, PLAN
and spec QA) and the ruling that allowed either. They are set once, by the controller, at dispatch, and
change only through `set_tier`, which needs a ruling. The gates read them from main. The fast path is refused
for the full tier. A light-tier task has no QA, so it is never `done`: after its branch is merged the
controller records it `merged`, a separate terminal status that carries no acceptance and evidences nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import evidence as evidence_module
import register as register_module
import rulings as rulings_module
import store
from common import KitError, PRIMARY_BRANCH, find_root, git, read_json, run_argv, utc_now, write_json

STATUSES = ("not_started", "in_progress", "review", "done", "blocked", "parked", "merged")
ROLES = ("controller", "implementer", "qa")
TIERS = ("full", "standard", "light")  # verification tiers; a record without the field is `full`
TERMINAL = ("done", "merged")
ADHOC_ID = re.compile(r"AH-\d{4}\Z")  # work outside the register, listed in controller-state.json

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
    ("in_progress", "merged"): {"controller"},  # light tier only, after the branch is merged into main
}

ACCEPTED = "accepted_development"  # the accepting value of `decision` in acceptance.schema.json

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_FIELDS = ("task_id", "status", "role", "updated_at", "note", "evidence", "blocked_by", "history")
_OPTIONAL_FIELDS = ("tier", "fast", "ruling")  # absent in records written before 0.3.0: full, not fast, no ruling
_TEXT_FIELDS = ("task_id", "role", "updated_at", "note")
_ENTRY_FIELDS = ("status", "role", "at")
_ENTRY_OPTIONAL = ("tier", "ruling")  # on the entry `set_tier` appends
_RULING = re.compile(r"R(\d+)\Z")


def tier_of(record) -> str:
    """The verification tier of a status record; a record without the field (or no record at all) is `full`."""
    tier = record.get("tier", "full") if isinstance(record, dict) else "full"
    return tier if tier in TIERS else "full"


def is_fast(record) -> bool:
    return isinstance(record, dict) and record.get("fast") is True


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
    if "tier" in entry and entry["tier"] not in TIERS:
        problems.append(f"unknown tier {entry['tier']!r}")
    if "ruling" in entry and not isinstance(entry["ruling"], str):
        problems.append(f"ruling must be text, not {entry['ruling']!r}")
    unknown = sorted(str(key) for key in entry if key not in _ENTRY_FIELDS + _ENTRY_OPTIONAL)
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
    unknown = sorted(str(key) for key in record if key not in _FIELDS + _OPTIONAL_FIELDS)
    if unknown:
        problems.append(f"unknown field(s) {', '.join(unknown)}")
    if "tier" in record and (not isinstance(record["tier"], str) or record["tier"] not in TIERS):
        problems.append(f"unknown tier {record['tier']!r}")
    if "fast" in record and not isinstance(record["fast"], bool):
        problems.append(f"fast must be true or false, not {record['fast']!r}")
    if "ruling" in record and not isinstance(record["ruling"], str):
        problems.append(f"ruling must be text, not {record['ruling']!r}")
    if record.get("fast") is True and record.get("tier", "full") == "full":
        problems.append("the fast path is never available for the full tier")
    if record.get("status") == "merged" and record.get("tier", "full") != "light":
        problems.append("merged is the terminal status of the light tier only")
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
    found = adhoc_ids(root)
    if not path.is_file():
        if found:
            return found
        raise KitError("program/register.yaml does not exist; a status belongs to a task the register lists")
    for phase in register_module.load(root)["phases"]:
        for milestone in children(phase, "milestones"):
            for step in children(milestone, "steps"):
                found.update(task["id"] for task in children(step, "tasks")
                             if isinstance(task, dict) and isinstance(task.get("id"), str))
    return found


def adhoc_ids(root: Path) -> set[str]:
    """The IDs of the ad-hoc tasks `program/controller-state.json` lists (work outside the register)."""
    path = Path(root) / "program" / "controller-state.json"
    if not path.is_file():
        return set()
    data = read_json(path)
    items = data.get("adhoc") if isinstance(data, dict) else None
    return {item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)
            and ADHOC_ID.match(item["id"])} if isinstance(items, list) else set()


def _ruling_number(root: Path, ruling: str, what: str) -> int:
    """The number of ruling `R<n>` after checking `program/rulings.md` holds it; KitError otherwise."""
    match = _RULING.match(ruling or "")
    if not match:
        raise KitError(f"{what} needs --ruling R<n>, the ruling that allows it")
    if int(match.group(1)) not in {entry["number"] for entry in rulings_module.read(root)}:
        raise KitError(f"{what}: {ruling} is not a ruling in program/rulings.md")
    return int(match.group(1))


def tier_config(root: Path) -> dict:
    """The `tiers` mapping of `program/checks.yaml` in the working tree: full_paths, light_paths, light_ruling."""
    import gate  # lazy: gate imports this module's neighbours

    return gate.load_checks(root)["tiers"]


def _tier_problem(root: Path, task_id: str, tier: str, fast: bool, ruling: str) -> None:
    """Refuse (KitError) a tier or fast path the program has not allowed. `full` on the SPEC path needs nothing."""
    if tier not in TIERS:
        raise KitError(f"unknown tier {tier!r}; tiers are {', '.join(TIERS)}")
    if fast and tier == "full":
        raise KitError(f"{task_id}: the fast path is never available for the full tier")
    if tier == "full":
        return
    config = tier_config(root)
    if not config["full_paths"]:
        raise KitError(f"{task_id}: tier {tier} is not switched on: list the paths that are always full-tier "
                       "under tiers.full_paths in program/checks.yaml (until then every task is full)")
    if tier == "light":
        if not config["light_ruling"] or not config["light_paths"]:
            raise KitError(f"{task_id}: the light tier is not switched on: program/checks.yaml needs "
                           "tiers.light_ruling (the ruling that names the kinds of change it covers) and "
                           "tiers.light_paths (where those changes live)")
        _ruling_number(root, config["light_ruling"], f"{task_id}: tiers.light_ruling in program/checks.yaml")
    if fast:
        _ruling_number(root, ruling, f"{task_id}: the fast path")
    elif ruling:
        _ruling_number(root, ruling, f"{task_id}: tier {tier}")


def _on_main(root: Path, what: str) -> None:
    try:
        here = git(root, "symbolic-ref", "HEAD")
    except KitError:
        here = ""
    if here != f"refs/heads/{PRIMARY_BRANCH}":
        raise KitError(f"{what} is recorded on {PRIMARY_BRANCH}: run it in the main checkout")


def _merged_problem(root: Path, task_id: str) -> str | None:
    """Why the task's branch does not count as merged into the checked-out main, or None."""
    code, _ = run_argv(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                        f"refs/heads/task/{task_id}^{{commit}}"], Path(root))
    if code != 0:
        return f"{task_id}: branch task/{task_id} not found; record merged before the branch is deleted"
    code, _ = run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", f"refs/heads/task/{task_id}", "HEAD"],
                       Path(root))
    return None if code == 0 else f"{task_id}: branch task/{task_id} is not merged into {PRIMARY_BRANCH}"


def _acceptance_problem(root: Path, task_id: str) -> str | None:
    """Why the task's evidence does not accept it, or None.

    None only when the task's pointer names a latest acceptance and a latest results record, both present
    with their pointed-at digests; the acceptance has every key acceptance.schema.json requires, accepts
    (`decision` is `accepted_development`), is QA's and this task's, and binds the latest results record by
    digest; that record passes the checks of the hand-back gate (`evidence.gate_problems`); the acceptance's
    `source_commit` is the `head` the record names; and that `head` is an ancestor of HEAD. Drift since `head`
    does not matter: the record says what was verified there.
    """
    try:
        index = {"tasks": {task_id: store.read_pointer(root, task_id)}}
    except KitError as exc:
        return f"{task_id}: {exc}"
    acceptance = store.latest(index, task_id, "acceptance")
    results = store.latest(index, task_id, "results")
    if acceptance is None:
        return f"{task_id}: done needs an acceptance record in {store.pointer_path(task_id)} (pk evidence accept)"
    if results is None:
        return f"{task_id}: done needs a results record in {store.pointer_path(task_id)}"
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
               evidence: list[str] | None = None, blocked_by: list[str] | None = None,
               tier: str | None = None, fast: bool = False, ruling: str = "") -> dict:
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
    if current == "merged":
        raise KitError(f"{task_id} is merged and merged is terminal; new work is a new task")
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
    dispatched = any(entry["status"] == "in_progress" for entry in record["history"])
    if tier is not None or fast or ruling:
        if to != "in_progress" or dispatched:
            raise KitError(f"{task_id}: the tier and the fast path are set at dispatch (the first move to "
                           f"in_progress); change the tier with: pk status tier {task_id} <tier> --ruling R<n>")
        _tier_problem(root, task_id, tier or "full", fast, ruling)
    if dispatched or to != "in_progress":
        tier, fast, ruling = tier_of(record), is_fast(record), record.get("ruling", "")
    else:
        tier = tier or "full"
    if to == "done":
        if tier == "light":
            raise KitError(f"{task_id}: a light-tier task has no QA and is never done; the controller records it "
                           f"merged, or raises its tier with: pk status tier {task_id} <tier> --ruling R<n>")
        problem = _acceptance_problem(root, task_id)
        if problem:
            raise KitError(problem)
    if to == "merged":
        if tier != "light":
            raise KitError(f"{task_id}: merged is for the light tier; a {tier}-tier task ends at done, set by QA")
        _on_main(root, f"{task_id}: merged")
        problem = _merged_problem(root, task_id)
        if problem:
            raise KitError(problem)
    if to == "review" and tier == "light":
        raise KitError(f"{task_id}: a light-tier task has no QA round; hand back with a report and the "
                       "controller merges it")
    now = utc_now()
    updated = {"task_id": task_id, "status": to, "role": role, "updated_at": now, "note": note,
               "evidence": merged, "blocked_by": blockers,
               "history": [*record["history"], {"status": to, "role": role, "at": now}],
               "tier": tier, "fast": bool(fast), "ruling": ruling}
    write_json(_record_path(root, task_id), updated)
    return updated


def set_tier(root: Path, task_id: str, tier: str, ruling: str, role: str = "controller", spec: bool = False) -> dict:
    """Change a dispatched task's tier on main; needs a ruling. Returns the new record.

    Refused for a terminal task, for any role but the controller, off main (the gates read main), without an
    existing ruling, for a tier the program has not switched on, and for a fast-path task raised to full
    unless `spec` says its SPEC, PLAN and spec-QA review now exist (the gates then require them).
    """
    root = Path(root)
    _check_id(task_id)
    if role != "controller":
        raise KitError(f"{task_id}: only the controller changes a tier, by a ruling")
    path = _record_path(root, task_id)
    if not path.exists():
        raise KitError(f"{task_id}: no status record yet; the tier is set at dispatch (pk status set ... --tier)")
    record = read(root, task_id)
    if record["status"] in TERMINAL:
        raise KitError(f"{task_id} is {record['status']} and that is terminal")
    _on_main(root, f"{task_id}: a tier change")
    _ruling_number(root, ruling, f"{task_id}: a tier change")
    fast = is_fast(record) and not spec
    if spec and not is_fast(record):
        raise KitError(f"{task_id}: --spec leaves the fast path, and this task is not on it")
    _tier_problem(root, task_id, tier, fast, ruling)
    if tier == tier_of(record) and fast == is_fast(record):
        raise KitError(f"{task_id}: tier is already {tier}")
    now = utc_now()
    updated = dict(record, role=role, updated_at=now, tier=tier, fast=fast, ruling=ruling,
                   history=[*record["history"], {"status": record["status"], "role": role, "at": now,
                                                 "tier": tier, "ruling": ruling}])
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
                            evidence=args.evidence, blocked_by=args.blocked_by, tier=args.tier, fast=args.fast,
                            ruling=args.ruling)
        lower = "" if record["tier"] == "full" else f" (tier {record['tier']}{', fast path' if record['fast'] else ''})"
        print(f"{record['task_id']}: {record['status']}{lower}")
    elif action == "tier":
        record = set_tier(_root(), args.task_id, args.tier, args.ruling, role=args.role, spec=args.spec)
        print(f"{record['task_id']}: tier {record['tier']}{', fast path' if record['fast'] else ''} "
              f"by {record['ruling']}")
    elif action == "show":
        print(json.dumps(read(_root(), args.task_id), indent=2))
    elif action == "list":
        for record in list_all(_root()):
            print("\t".join([record["task_id"], record["status"], record["role"], record["updated_at"],
                             tier_of(record) + ("+fast" if is_fast(record) else "")]))
    elif action == "validate":
        problems = validate(_root())
        for problem in problems:  # each on its own line, like every other check that can find several
            print("error: " + " ".join(problem.split()), file=sys.stderr)
        if problems:
            return 1
        print("status: ok")
    else:
        raise KitError("status needs an action: set, tier, show, list or validate")
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
    setter.add_argument("--tier", choices=TIERS, help="at dispatch: the verification tier (default full)")
    setter.add_argument("--fast", action="store_true",
                        help="at dispatch: the fast path (BRIEF.md and a ruling replace SPEC, PLAN and spec QA); "
                             "never with --tier full")
    setter.add_argument("--ruling", default="", metavar="R<n>", help="at dispatch: the ruling that allows the "
                                                                   "fast path or the lower tier")
    changer = actions.add_parser("tier", help="change a dispatched task's tier on main, by a ruling")
    changer.add_argument("task_id")
    changer.add_argument("tier", choices=TIERS)
    changer.add_argument("--ruling", required=True, metavar="R<n>")
    changer.add_argument("--role", default="controller", help="only the controller changes a tier")
    changer.add_argument("--spec", action="store_true",
                         help="leave the fast path: the task now has SPEC, PLAN and a spec-QA review on main")
    actions.add_parser("show", help="print one record").add_argument("task_id")
    actions.add_parser("list", help="one line per task")
    actions.add_parser("validate", help="check every record")


def register(add_command) -> None:
    add_command("status", cmd_status, help="task status state machine", configure=_configure)
