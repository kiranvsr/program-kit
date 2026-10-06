#!/usr/bin/env python3
"""Traceability matrix (`program/traceability.yaml`, `program/gates.yaml`).

The matrix links journeys (J<n>) to requirements (REQ-<nnn>, NFR-<nnn>), requirements
to components (C<n>), gates (G-<NAME>) to the records that prove them, and tasks to
the IDs they close. `validate` applies the checks that belong to an interview stage,
cumulatively; `sync` copies each task's `closes` from the register; `impact` answers
what a change to some IDs reaches (a gate whose claims name only a journey is not matched).
Both files are optional until the stage that needs them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import register as register_module
import status as status_module
from common import KitError, find_root, read_yaml, write_yaml

SECTIONS = ("journeys", "requirements", "components", "gates", "tasks")
STAGE_JOURNEYS, STAGE_LINKS, STAGE_COMPONENTS, STAGE_TASKS = 2, 3, 4, 6

_JOURNEY = re.compile(r"J\d+\Z", re.ASCII)
_REQUIREMENT = re.compile(r"(?:REQ|NFR)-\d{3}\Z", re.ASCII)
_COMPONENT = re.compile(r"C\d+\Z", re.ASCII)
_KINDS = {  # kind -> (pattern, how it is written)
    "journey": (_JOURNEY, "J<n>"),
    "requirement": (_REQUIREMENT, "REQ-<nnn> or NFR-<nnn>"),
    "component": (_COMPONENT, "C<n>"),
    "gate": (register_module.GATE_ID, "G-<NAME>"),
    "task": (register_module.TASK_ID, "P<n>-M<nn>-S<nn>-T<nn>"),
    "closes": (register_module.CLOSES_ID, "REQ-<nnn>, NFR-<nnn> or J<n>"),
}
_NOT_STARTED, _IN_FLIGHT, _DONE = "not_started", "in_flight", "done"
_BUCKET = {"done": _DONE, "in_progress": _IN_FLIGHT, "review": _IN_FLIGHT, "blocked": _IN_FLIGHT}


def _path(root: Path, name: str) -> Path:
    return Path(root) / "program" / name


def _sections(data, label: str, names: tuple[str, ...]) -> dict[str, dict]:
    """The named top-level mappings of `data`; an absent or empty one is an empty mapping."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise KitError(f"{label}: expected a mapping")
    found = {}
    for name in names:
        value = {} if data.get(name) is None else data[name]
        if not isinstance(value, dict):
            raise KitError(f"{label}: '{name}' must be a mapping")
        found[name] = value
    return found


def _load(root: Path, name: str, names: tuple[str, ...]) -> dict[str, dict] | None:
    path = _path(root, name)
    if not path.exists():
        return None
    return _sections(read_yaml(path), f"program/{name}", names)


class _Entries:
    """Tolerant reads of the entries of one section, collecting shape and ID-pattern problems."""

    def __init__(self, problems: list[str]):
        self.problems = problems

    def key(self, key, kind: str, file: str | None = None) -> str:
        """Report a malformed key; gate keys appear in two files, so theirs are prefixed by the file."""
        pattern, shape = _KINDS[kind]
        if not (isinstance(key, str) and pattern.match(key)):
            prefix = f"{file}: {key} is" if file else f"{key}: is"
            self.problems.append(f"{prefix} not a valid {kind} id (expected {shape})")
        return str(key)

    def table(self, section: dict, kind: str, file: str) -> dict[str, dict]:
        """The entries of `section` by key. An entry that is not a mapping raises (wrong shape)."""
        found = {}
        for key, value in section.items():
            name = self.key(key, kind, file if kind == "gate" else None)
            if value is None:
                value = {}
            elif not isinstance(value, dict):
                raise KitError(f"{file}: {key} must be a mapping")
            found[name] = value
        return found

    def strings(self, who: str, entry: dict, field: str) -> None:
        value = entry.get(field)
        if value is not None and not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
            self.problems.append(f"{who}: {field} must be a list of strings")

    def ids(self, who: str, entry: dict, field: str, kind: str) -> list[str]:
        value = entry.get(field)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.problems.append(f"{who}: {field} must be a list of strings")
            return []
        pattern, shape = _KINDS[kind]
        for item in value:
            if not pattern.match(item):
                self.problems.append(f"{who}: {field} entry {item!r} is not a valid {kind} id (expected {shape})")
        return value


def validate(root: Path, stage: int) -> list[str]:
    """Problems with the matrix at interview `stage`, each starting with the offending ID or file."""
    matrix = _load(root, "traceability.yaml", SECTIONS)
    if matrix is None:
        return ["program/traceability.yaml is missing"] if stage >= STAGE_JOURNEYS else []
    gate_file = _load(root, "gates.yaml", ("gates",))  # read at every stage: its IDs are checked everywhere

    problems: list[str] = []
    read = _Entries(problems)
    where = "program/traceability.yaml"
    journeys = read.table(matrix["journeys"], "journey", where)
    requirements = read.table(matrix["requirements"], "requirement", where)
    components = read.table(matrix["components"], "component", where)
    gates = read.table(matrix["gates"], "gate", where)
    tasks = read.table(matrix["tasks"], "task", where)
    defined = read.table(gate_file["gates"], "gate", "program/gates.yaml") if gate_file is not None else {}

    journey_reqs = {j: read.ids(j, e, "requirements", "requirement") for j, e in journeys.items()}
    req_journeys = {r: read.ids(r, e, "journeys", "journey") for r, e in requirements.items()}
    req_components = {r: read.ids(r, e, "components", "component") for r, e in requirements.items()}
    comp_reqs = {c: read.ids(c, e, "requirements", "requirement") for c, e in components.items()}
    for gate, entry in gates.items():
        read.strings(gate, entry, "claims")
        read.strings(gate, entry, "records")
    closes = {t: read.ids(t, e, "closes", "closes") for t, e in tasks.items()}

    if stage >= STAGE_JOURNEYS:
        if not journeys:
            problems.append("program/traceability.yaml: no journeys listed")
        for journey in journeys:
            if not journey_reqs[journey] and stage >= STAGE_LINKS:
                problems.append(f"{journey}: lists no requirement")
    if stage >= STAGE_LINKS:
        for req, entry in requirements.items():
            if not req_journeys[req] and entry.get("nfr") is not True:
                problems.append(f"{req}: names no journey (set nfr: true for a non-functional requirement)")
        for journey, listed in journey_reqs.items():
            for req in listed:
                if req not in requirements:
                    problems.append(f"{journey}: lists {req} which does not exist")
                elif journey not in req_journeys[req]:
                    problems.append(f"{journey}: lists {req} but {req} does not list {journey}")
        for req, listed in req_journeys.items():
            for journey in listed:
                if journey not in journeys:
                    problems.append(f"{req}: names journey {journey} which does not exist")
                elif req not in journey_reqs[journey]:
                    problems.append(f"{req}: names {journey} but {journey} does not list {req}")
    if stage >= STAGE_COMPONENTS:
        for req, listed in req_components.items():
            if not listed:
                problems.append(f"{req}: names no component")
            problems.extend(f"{req}: names component {c} which does not exist" for c in listed if c not in components)
        for comp, listed in comp_reqs.items():
            problems.extend(f"{comp}: lists {r} which does not exist" for r in listed if r not in requirements)
        if gate_file is None:
            problems.append("program/gates.yaml is missing")
        else:
            for gate, entry in defined.items():
                records = entry.get("records")
                if not isinstance(records, list) or not records:
                    problems.append(f"{gate}: has no records in gates.yaml")
            problems.extend(f"{g}: is in traceability.yaml but not in gates.yaml" for g in gates if g not in defined)
    if stage >= STAGE_TASKS:
        closed: set[str] = set()
        for task, listed in closes.items():
            for target in listed:
                if target in journeys or target in requirements:
                    closed.add(target)
                elif _KINDS["closes"][0].match(target):  # a malformed ID was reported above
                    problems.append(f"{task}: closes {target} which does not exist")
        for req, entry in requirements.items():
            if entry.get("release", "first") == "first" and req not in closed:
                problems.append(f"{req}: first-release requirement is not closed by any task")
    return problems


# --- sync --------------------------------------------------------------------

def _children(node, field: str, where: str) -> list:
    """The list under `field` of a register node; an absent or empty one is empty, a wrong shape raises."""
    value = node.get(field) if isinstance(node, dict) else None
    if value is None:
        return []
    if not isinstance(value, list):
        raise KitError(f"program/register.yaml is malformed: {where}.{field} must be a list; run: pk register validate")
    for number, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise KitError(f"program/register.yaml is malformed: {where}.{field}[{number}] must be a mapping; "
                           "run: pk register validate")
    return value


def _register_tasks(register: dict) -> list[tuple[str, list[str]]]:
    """(task id, closes) for every task in register order; a task without a string id is skipped.

    Raises KitError when a phase, milestone, step or task list has the wrong shape, so that `sync` never
    rewrites the `tasks` section from half a register.
    """
    found = []
    for phase in _children(register, "phases", "register"):
        for milestone in _children(phase, "milestones", "phase"):
            for step in _children(milestone, "steps", "milestone"):
                for item in _children(step, "tasks", "step"):
                    if isinstance(item.get("id"), str):
                        closes = item.get("closes")
                        found.append((item["id"], [c for c in closes if isinstance(c, str)]
                                      if isinstance(closes, list) else []))
    return found


def sync(root: Path) -> int:
    """Rewrite the `tasks` section from the register, leaving the other sections alone.

    Starts from empty sections when traceability.yaml does not exist yet. Returns the task count.
    """
    register = register_module.load(root)
    path = _path(root, "traceability.yaml")
    data = read_yaml(path) if path.exists() else None
    _sections(data, "program/traceability.yaml", SECTIONS)  # refuses a file of the wrong shape
    matrix = dict(data) if data else {name: {} for name in SECTIONS}
    tasks = {task_id: {"closes": closes} for task_id, closes in _register_tasks(register)}
    matrix["tasks"] = tasks
    write_yaml(path, matrix)
    return len(tasks)


# --- impact ------------------------------------------------------------------

def _strings(value) -> list[str]:
    """Every string in a list, looking one mapping level deep (gates.yaml records are mappings)."""
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            found.extend(v for v in item.values() if isinstance(v, str))
    return found


def _mentions(text: str, ident: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(ident) + r"(?![A-Za-z0-9_])", text) is not None


def impact(root: Path, ids: list[str]) -> dict:
    """What a change to `ids` (journeys, requirements, components, gates) reaches.

    Requirements: those of the named journeys and components, plus the named ones.
    Components: those of any reached requirement, plus the named ones. Gates: the named
    ones, plus any whose records or claims mention a reached requirement or component.
    Tasks: those whose `closes` hold a reached requirement or a named journey, from the
    register and the `tasks` section together (a stale section can only over-report),
    bucketed by status record.
    """
    matrix = _load(root, "traceability.yaml", SECTIONS)
    if matrix is None:
        raise KitError("program/traceability.yaml is missing")
    gate_file = _load(root, "gates.yaml", ("gates",)) or {"gates": {}}
    journeys, requirements, components = (matrix[name] for name in ("journeys", "requirements", "components"))
    gate_ids = set(matrix["gates"]) | set(gate_file["gates"])

    for ident in ids:
        if ident not in journeys and ident not in requirements and ident not in components and ident not in gate_ids:
            raise KitError(f"{ident} is not in program/traceability.yaml")

    def listed(table: dict, ident: str, field: str) -> set[str]:
        entry = table.get(ident)
        value = entry.get(field) if isinstance(entry, dict) else None
        return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()

    def holders(table: dict, ident: str, field: str) -> set[str]:
        return {key for key in table if ident in listed(table, key, field)}

    named_journeys = {i for i in ids if i in journeys}
    named_components = {i for i in ids if i in components}
    reached_reqs = {i for i in ids if i in requirements}
    for journey in named_journeys:
        reached_reqs |= listed(journeys, journey, "requirements") | holders(requirements, journey, "journeys")
    for component in named_components:
        reached_reqs |= listed(components, component, "requirements") | holders(requirements, component, "components")
    reached_comps = set(named_components)
    for req in reached_reqs:
        reached_comps |= listed(requirements, req, "components") | holders(components, req, "requirements")

    texts: dict[str, list[str]] = {}
    for table in (matrix["gates"], gate_file["gates"]):
        for gate, entry in table.items():
            if isinstance(entry, dict):
                texts.setdefault(gate, []).extend(_strings(entry.get("claims")) + _strings(entry.get("records")))
            else:
                texts.setdefault(gate, [])
    reached_gates = {i for i in ids if i in gate_ids}
    for gate, strings in texts.items():
        if any(_mentions(text, ident) for text in strings for ident in reached_reqs | reached_comps):
            reached_gates.add(gate)

    closes: dict[str, set[str]] = {}
    path = _path(root, "register.yaml")
    if path.exists():
        for task_id, items in _register_tasks(register_module.load(root)):
            closes.setdefault(task_id, set()).update(items)
    for task_id in matrix["tasks"]:
        closes.setdefault(task_id, set()).update(listed(matrix["tasks"], task_id, "closes"))
    states = {record["task_id"]: record["status"] for record in status_module.list_all(root)}
    hit = reached_reqs | named_journeys
    buckets: dict[str, list[str]] = {_DONE: [], _IN_FLIGHT: [], _NOT_STARTED: []}
    for task_id in sorted(closes):
        if closes[task_id] & hit:
            buckets[_BUCKET.get(states.get(task_id, "not_started"), _NOT_STARTED)].append(task_id)
    return {"requirements": sorted(reached_reqs), "components": sorted(reached_comps),
            "gates": sorted(reached_gates), "tasks": buckets}


# --- command -----------------------------------------------------------------

def cmd_trace(args: argparse.Namespace) -> int:
    action = getattr(args, "trace_action", None)
    if action not in ("validate", "sync", "impact"):
        raise KitError("trace needs an action: validate, sync or impact")
    root = find_root()
    if action == "validate":
        problems = validate(root, args.stage)
        if problems:
            print("\n".join("error: " + problem for problem in problems), file=sys.stderr)
            return 1
        print(f"traceability valid at stage {args.stage}")
    elif action == "sync":
        print(f"synced {sync(root)} tasks")
    else:
        print(json.dumps(impact(root, args.ids), indent=2, sort_keys=True))
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="trace_action", metavar="action", parser_class=type(parser))
    checker = actions.add_parser("validate", help="check program/traceability.yaml for an interview stage")
    checker.add_argument("--stage", type=int, required=True, help="interview stage (checks are cumulative)")
    actions.add_parser("sync", help="copy each task's closes from the register into the tasks section")
    finder = actions.add_parser("impact", help="what a change to the given IDs reaches")
    finder.add_argument("ids", nargs="+", metavar="ID", help="journey, requirement, component or gate IDs")


def register(add_command) -> None:
    add_command("trace", cmd_trace, help="traceability matrix: validate, sync, impact", configure=_configure)
