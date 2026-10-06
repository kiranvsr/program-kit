#!/usr/bin/env python3
"""Task register (`program/register.yaml`): loading, stable IDs and validation.

The register is phases > milestones > steps > tasks. IDs are P<n>, P<n>-M<nn>,
P<n>-M<nn>-S<nn> and P<n>-M<nn>-S<nn>-T<nn>. `assign_ids` fills missing IDs from
position and never changes an ID that is already there; `validate` returns one
message per problem and raises only when the document has the wrong shape.
"""
from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

from common import KitError, find_root, read_yaml, utc_now, write_yaml

KIT_DIR = Path(__file__).resolve().parent.parent
MAX_FAN_IN = 6
SIZES = ("S", "M")

KINDS = ("phase", "milestone", "step", "task")
_CHILD = ("milestones", "steps", "tasks")  # the list each of the first three kinds holds
_LETTER = ("", "M", "S", "T")
_ID = {
    "phase": re.compile(r"P\d+\Z"),
    "milestone": re.compile(r"P\d+-M\d+\Z"),
    "step": re.compile(r"P\d+-M\d+-S\d+\Z"),
    "task": re.compile(r"P\d+-M\d+-S\d+-T\d+\Z"),
}
_SHAPE = {"phase": "P<n>", "milestone": "P<n>-M<n>", "step": "P<n>-M<n>-S<n>", "task": "P<n>-M<n>-S<n>-T<n>"}
_GATE = re.compile(r"G-[A-Z0-9][A-Z0-9_]*\Z")
_CLOSES = re.compile(r"(?:REQ-\d{3}|NFR-\d{3}|J\d+)\Z")


def _path(root: Path) -> Path:
    return Path(root) / "program" / "register.yaml"


def _check_shape(register, name: str = "register") -> None:
    if not isinstance(register, dict):
        raise KitError(f"{name}: expected a mapping with a 'phases' list")
    if not isinstance(register.get("phases"), list):
        raise KitError(f"{name}: missing 'phases' list")


def load(root: Path) -> dict:
    path = _path(root)
    register = read_yaml(path)
    _check_shape(register, str(path))
    return register


def _nodes(register: dict, problems: list[str] | None = None):
    """Yield (level, node, label) for every mapping in document order.

    `label` locates the node by position, for nodes that have no usable ID. Entries
    that are not mappings, and child collections that are not lists, are skipped; when
    `problems` is given they are reported there.
    """
    stack: list[tuple[int, str, dict]] = []

    def push(items, label: str, level: int) -> None:
        if not isinstance(items, list):
            if problems is not None:
                problems.append(f"{label}: must be a list")
            return
        found = []
        for number, item in enumerate(items, 1):
            if isinstance(item, dict):
                found.append((level, f"{label}[{number}]", item))
            elif problems is not None:
                problems.append(f"{label}[{number}]: must be a mapping")
        stack.extend(reversed(found))

    push(register["phases"], "phases", 0)
    while stack:
        level, label, node = stack.pop()
        yield level, node, label
        if level < 3 and _CHILD[level] in node:
            name = node["id"] if isinstance(node.get("id"), str) and node["id"] else label
            push(node[_CHILD[level]], f"{name}.{_CHILD[level]}", level + 1)


def task_count(register: dict) -> int:
    return sum(1 for level, _, _ in _nodes(register) if level == 3)


# --- ids ---------------------------------------------------------------------

def assign_ids(register: dict) -> dict:
    """A copy of `register` with every missing ID derived from its position.

    Kept IDs are never touched. A derived number that another node already holds
    is skipped, so a task inserted before an existing one gets the next free number.
    """
    _check_shape(register)
    result = copy.deepcopy(register)
    taken = {node["id"] for _, node, _ in _nodes(result) if isinstance(node.get("id"), str)}

    def walk(items, label: str, level: int, parent: str) -> None:
        if not isinstance(items, list):
            raise KitError(f"cannot assign ids: {label} is not a list")
        for position, node in enumerate(items, 1):
            if not isinstance(node, dict):
                raise KitError(f"cannot assign ids: {label}[{position}] is not a mapping")
            if not isinstance(node.get("id"), str):
                prefix = "P" if level == 0 else f"{parent}-{_LETTER[level]}"
                number = position
                width = 1 if level == 0 else 2
                while f"{prefix}{number:0{width}d}" in taken:
                    number += 1
                new = f"{prefix}{number:0{width}d}"
                taken.add(new)
                rest = {key: value for key, value in node.items() if key != "id"}
                node.clear()
                node["id"] = new
                node.update(rest)
            if level < 3 and _CHILD[level] in node:
                walk(node[_CHILD[level]], f"{node['id']}.{_CHILD[level]}", level + 1, node["id"])

    walk(result["phases"], "phases", 0, "")
    return result


# --- validation --------------------------------------------------------------

def _id_list(node: dict, field: str, who: str, problems: list[str]) -> list[str]:
    """The strings in node[field] (absent means empty); anything else is reported and ignored."""
    value = node.get(field)
    if field not in node:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{who}: {field} must be a list of strings")
        return []
    return value


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Every back edge in `graph` as the cycle it closes, by iterative DFS (no recursion)."""
    cycles: list[list[str]] = []
    state: dict[str, int] = {}  # 1 on the current path, 2 finished
    for start in graph:
        if start in state:
            continue
        path = [start]
        state[start] = 1
        stack = [iter(graph[start])]
        while stack:
            target = next(stack[-1], None)
            if target is None:
                state[path.pop()] = 2
                stack.pop()
            elif target not in graph:
                continue
            elif state.get(target) == 1:
                cycles.append(path[path.index(target):] + [target])
            elif target not in state:
                state[target] = 1
                path.append(target)
                stack.append(iter(graph[target]))
    return cycles


def validate(register: dict, environments: dict | None, roles: dict | None) -> list[str]:
    """Problems with the register, one string each (the offending ID first); empty when valid."""
    _check_shape(register)
    known_envs = None
    if environments is not None:
        if not isinstance(environments, dict) or not isinstance(environments.get("environments"), dict):
            raise KitError("environments: expected a mapping with an 'environments' mapping")
        known_envs = environments["environments"]
    role_ids = None
    if roles is not None:
        if not isinstance(roles, dict) or not isinstance(roles.get("roles"), list):
            raise KitError("roles: expected a mapping with a 'roles' list")
        role_ids = {role["id"] for role in roles["roles"] if isinstance(role, dict) and isinstance(role.get("id"), str)}

    problems: list[str] = []
    nodes = list(_nodes(register, problems))
    all_ids = [node["id"] for _, node, _ in nodes if isinstance(node.get("id"), str)]
    known = set(all_ids)
    seen: set[str] = set()
    graph: dict[str, list[str]] = {}
    for level, node, label in nodes:
        kind = KINDS[level]
        ident = node.get("id")
        if not isinstance(ident, str) or not ident:
            problems.append(f"{label}: missing id")
            who = label
        else:
            who = ident
            if not _ID[kind].match(ident):
                problems.append(f"{who}: id is not a valid {kind} id (expected {_SHAPE[kind]})")
            if ident in seen:
                problems.append(f"{who}: duplicate id")
            seen.add(ident)
        title = node.get("title")
        if not isinstance(title, str) or not title.strip():
            problems.append(f"{who}: title is missing")
        gate = node.get("gate")
        if gate is not None and not (isinstance(gate, str) and _GATE.match(gate)):
            problems.append(f"{who}: gate {gate!r} must be G-<NAME> or null")
        if level < 3:
            continue

        size = node.get("size")
        if size == "L":
            problems.append(f"{who}: size L is not allowed on a task; split into steps")
        elif size not in SIZES:
            problems.append(f"{who}: size {size!r} must be S or M")
        role = node.get("role")
        if role_ids is not None and role not in role_ids:
            problems.append(f"{who}: unknown role {role!r}")
        depends = _id_list(node, "depends_on", who, problems)
        if len(depends) > MAX_FAN_IN:
            problems.append(f"{who}: depends_on has {len(depends)} entries; at most {MAX_FAN_IN} are allowed")
        for target in depends:
            if target not in known:
                problems.append(f"{who}: depends_on {target} does not exist")
        for target in _id_list(node, "closes", who, problems):
            if not _CLOSES.match(target):
                problems.append(f"{who}: closes {target!r} must match REQ-<nnn>, NFR-<nnn> or J<n>")
        envs = _id_list(node, "environments", who, problems)
        blocked = _id_list(node, "blocked_by", who, problems)
        if known_envs is not None:
            for env in envs:
                entry = known_envs.get(env)
                if env not in known_envs:
                    problems.append(f"{who}: environment {env} is not defined in environments.yaml")
                elif not (isinstance(entry, dict) and entry.get("exists") is True) and env not in blocked:
                    problems.append(f"{who}: environment {env} does not exist yet; "
                                    f"add blocked_by: [{env}]")
        if isinstance(ident, str) and ident:
            graph[ident] = [t for t in depends if t != "" and t in known]
    for cycle in _find_cycles(graph):
        problems.append("cycle: " + " -> ".join(cycle))
    return problems


# --- change log --------------------------------------------------------------

_LOG_TITLE = "# Register change log\n"


def change(root: Path, note: str) -> None:
    """Append `- <date>: <note>` to program/changes/register-log.md, then regenerate the briefs.

    The log is append-only. A note that is empty or spans several lines, or a register
    the briefs cannot be built from, is refused before anything is written.
    """
    import briefs  # lazy: briefs imports this module

    note = note.strip()
    if not note or len(note.splitlines()) != 1:  # splitlines: \r, \x0b, \x85, \u2028 ... too
        raise KitError("a register change note must be one non-empty line")
    briefs.render(root)  # raises if the register cannot be rendered; nothing written yet
    path = Path(root) / "program" / "changes" / "register-log.md"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc.strerror or exc}") from exc
    prefix = _LOG_TITLE + "\n" if not data else ("" if data.endswith(b"\n") else "\n")
    entry = f"- {utc_now()[:10]}: {note}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as handle:
            handle.write((prefix + entry).encode("utf-8"))
    except OSError as exc:
        raise KitError(f"cannot write {path}: {exc.strerror or exc}") from exc
    briefs.generate(root)


# --- command -----------------------------------------------------------------

def cmd_register(args: argparse.Namespace) -> int:
    action = getattr(args, "register_action", None)
    if action not in ("validate", "assign-ids", "change"):
        raise KitError("register needs an action: validate, assign-ids or change")
    root = find_root()
    if action == "change":
        import briefs

        change(root, args.note)
        print(f"recorded register change; generated {briefs.brief_count(briefs.generate(root))} briefs")
        return 0
    register = load(root)
    if action == "assign-ids":
        assigned = assign_ids(register)
        write_yaml(_path(root), assigned)
        print(f"assigned ids: {task_count(assigned)} tasks")
        return 0
    env_path = Path(root) / "program" / "environments.yaml"
    environments = read_yaml(env_path) if env_path.exists() else None
    roles = read_yaml(KIT_DIR / "interview" / "roles.yaml")
    problems = validate(register, environments, roles)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"register valid: {task_count(register)} tasks")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="register_action", metavar="action", parser_class=type(parser))
    actions.add_parser("validate", help="check program/register.yaml")
    actions.add_parser("assign-ids", help="fill missing IDs in program/register.yaml from position")
    changer = actions.add_parser("change", help="log a register change and regenerate the briefs")
    changer.add_argument("--note", required=True, help="one line saying what changed and why")


def register(add_command) -> None:
    add_command("register", cmd_register, help="task register: validate, assign IDs, log changes", configure=_configure)
