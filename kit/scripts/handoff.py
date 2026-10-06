#!/usr/bin/env python3
"""The generated part of `program/HANDOFF.md` (standard library only).

The file has hand-written parts and one generated block between the marker lines
`<!-- generated:start -->` and `<!-- generated:end -->`. `render` builds the block from the
status records, `program/controller-state.json`, the branches not merged into main, the task
worktrees and the open change requests. `update` rewrites only the text between the markers;
everything else is preserved byte for byte. `check` says whether the block on disk is current.

The block holds no wall-clock time, so `check` is stable. It carries the commit it was generated
from on one `Commit: ` line, and `check` and `update` ignore that line when they compare:
committing the hand-off moves HEAD, which must not make it stale. `update` therefore leaves the
file alone when nothing else changed, and the commit line names HEAD as of the last real change.

The main checkout is not listed among the worktrees: its dirty state changes whenever this
file is rewritten, which would make the block unstable.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import change as change_module
import status as status_module
import trace as trace_module
import worktree as worktree_module
import register as register_module
from common import KitError, find_root, git, primary_branch_problem, read_json, write_text

START, END = "<!-- generated:start -->", "<!-- generated:end -->"
COMMIT_PREFIX = "Commit: "
IN_FLIGHT = ("in_progress", "review", "blocked")
_STATE_FILE = "program/controller-state.json"


def _line(text: str) -> str:
    """`text` on one line, with `<` escaped so an unterminated `<!--` cannot hide the rest of the block."""
    return " ".join(text.split()).replace("<", "&lt;")


def _mapping(data: dict, key: str) -> dict[str, str]:
    value = data.get(key, {})
    if not (isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())):
        raise KitError(f"{_STATE_FILE}: '{key}' must be a mapping of task ID to text")
    return value


def _state(root: Path) -> dict:
    """The controller state, validated; a missing file is an empty state."""
    path = Path(root) / _STATE_FILE
    data = read_json(path) if path.exists() else {}
    if not isinstance(data, dict):
        raise KitError(f"{_STATE_FILE}: expected a mapping with stage, queued, parked and followups")
    followups = data.get("followups", [])
    if not (isinstance(followups, list) and all(
            isinstance(item, dict) and isinstance(item.get("area"), str) and isinstance(item.get("item"), str)
            for item in followups)):
        raise KitError(f"{_STATE_FILE}: 'followups' must be a list of {{area, item}} text pairs")
    return {"stage": _mapping(data, "stage"), "queued": _mapping(data, "queued"),
            "parked": _mapping(data, "parked"), "followups": followups}


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- none"]


def _commit(root: Path) -> str:
    try:
        return git(root, "rev-parse", "HEAD")
    except KitError:
        return "none"


def _worktree_line(root: Path, item: dict) -> str | None:
    path = Path(item["path"]).resolve()
    if path == Path(root).resolve():
        return None
    try:
        shown = path.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        shown = str(path)
    state = "missing" if item["missing"] else ("dirty" if item["dirty"] else "clean")
    return _line(f"{shown} ({item['branch'] or 'detached'}) head {item['head'][:7]}, {state}")


def render(root: Path, main: str = "main") -> str:
    """The generated block as Markdown ending in a newline."""
    root = Path(root)
    state = _state(root)
    records = status_module.list_all(root)
    counts = {name: 0 for name in status_module.STATUSES}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    unregistered: list[str] = []
    if (root / "program" / "register.yaml").exists():
        try:  # a hand-edited register of the wrong shape must be one error line, never a traceback
            in_register = {task_id for task_id, _ in trace_module._register_tasks(register_module.load(root))}
        except (TypeError, AttributeError, KeyError) as error:
            raise KitError(f"program/register.yaml is malformed ({type(error).__name__}); "
                           "run: pk register validate") from None
        recorded = {record["task_id"] for record in records}
        counts["not_started"] += len(in_register - recorded)  # no status record yet means not started
        unregistered = sorted(recorded - in_register)
    flying = [f"{_line(r['task_id'])} ({r['status']}): "
              f"{_line(state['stage'].get(r['task_id'], '')) or '(no stage text)'}"
              for r in records if r["status"] in IN_FLIGHT]
    problem = primary_branch_problem(root, main, need_commit=True)
    if problem:
        raise KitError(problem)
    branches = [_line(ref[len("refs/heads/"):]) for ref in git(
        root, "for-each-ref", "--format=%(refname)", f"--no-merged={main}", "refs/heads/").splitlines()]
    trees = [line for item in worktree_module.list_all(root) if (line := _worktree_line(root, item))]
    requests = [f"{c['id']}: {_line(c['title'])} ({'approved, not applied' if c['approved'] else 'awaiting approval'})"
                for c in change_module.open_requests(root)]
    followups = [f"{_line(f['area'])}: {_line(f['item'])}" for f in state["followups"]]

    def tasks(table: dict[str, str]) -> list[str]:
        return [f"{_line(task)}: {_line(text)}" for task, text in sorted(table.items())]

    lines = ["### Generated state", "", f"{COMMIT_PREFIX}{_commit(root)}", "",
             "#### Tasks by status", *[f"- {name}: {number}" for name, number in counts.items()],
             *([f"- not in register: {', '.join(_line(t) for t in unregistered)}"] if unregistered else []), "",
             "#### In flight", *_bullets(flying), "",
             f"#### Branches not merged into {main}", *_bullets(branches), "",
             "#### Task worktrees", *_bullets(trees), "",
             "#### Queued", *_bullets(tasks(state["queued"])), "",
             "#### Parked", *_bullets(tasks(state["parked"])), "",
             "#### Open follow-ups", *_bullets(followups), "",
             "#### Open change requests", *_bullets(requests)]
    block = "\n".join(lines) + "\n"
    if any(line.strip() in (START, END) for line in block.splitlines()):  # belt and braces: state text is escaped
        raise KitError("the generated block would contain a marker line; check controller-state.json")
    return block


def _comparable(block: str) -> str:
    return "".join(line for line in block.splitlines(keepends=True) if not line.startswith(COMMIT_PREFIX))


def _marker(text: str, marker: str) -> re.Match:
    found = list(re.finditer(r"^[ \t]*" + re.escape(marker) + r"[ \t]*(?:\r?\n|\Z)", text, re.MULTILINE))
    if not found:
        raise KitError(f"program/HANDOFF.md: marker line {marker} not found")
    if len(found) > 1:
        raise KitError(f"program/HANDOFF.md: marker line {marker} appears {len(found)} times")
    return found[0]


def _split(text: str) -> tuple[int, int]:
    """Offsets of the generated block: just after the start marker line, and the start of the end marker line."""
    start, end = _marker(text, START), _marker(text, END)
    if end.start() < start.end():
        raise KitError("program/HANDOFF.md: the generated markers are out of order (end before start)")
    return start.end(), end.start()


def _read(root: Path) -> tuple[Path, str]:
    path = Path(root) / "program" / "HANDOFF.md"
    try:
        return path, path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise KitError("program/HANDOFF.md not found; run: pk init") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise KitError(f"cannot read {path}: {getattr(exc, 'strerror', None) or exc}") from exc


def check(root: Path) -> bool:
    """True when the block on disk equals the block rendered now, ignoring the commit line."""
    _, text = _read(root)
    begin, finish = _split(text)
    return _comparable(text[begin:finish]) == _comparable(render(root))


def update(root: Path) -> bool:
    """Rewrite the text between the markers; return whether the file changed.

    Missing, duplicated or out-of-order markers raise KitError and nothing is written.
    """
    path, text = _read(root)
    begin, finish = _split(text)
    block = render(root)
    if _comparable(text[begin:finish]) == _comparable(block):
        return False
    write_text(path, text[:begin] + block + text[finish:])
    return True


# --- command -----------------------------------------------------------------

def cmd_handoff(args: argparse.Namespace) -> int:
    action = getattr(args, "handoff_action", None)
    if action not in ("update", "check"):
        raise KitError("handoff needs an action: update or check")
    root = find_root()
    if action == "update":
        print("handoff: updated" if update(root) else "handoff: unchanged")
    elif check(root):
        print("handoff: current")
    else:
        raise KitError("program/HANDOFF.md is stale; run: pk handoff update")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="handoff_action", metavar="action", parser_class=type(parser))
    actions.add_parser("update", help="rewrite the generated block of program/HANDOFF.md")
    actions.add_parser("check", help="exit 1 when the generated block is stale")


def register(add_command) -> None:
    add_command("handoff", cmd_handoff, help="the generated part of program/HANDOFF.md: update, check",
                configure=_configure)
