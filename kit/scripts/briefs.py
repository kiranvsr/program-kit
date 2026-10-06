#!/usr/bin/env python3
"""Deterministic brief generation from the task register (`program/register.yaml`).

`generate` writes one brief per phase, milestone, step and task under `program/briefs/`,
plus `CATALOG.md` (one row per task) and `MANIFEST.json` (path -> sha256). The output
depends only on the register and the templates in `kit/templates/`: no clock, register
order kept, only MANIFEST keys sorted. `check` regenerates in memory and reports every
file that differs from disk. Once `program/approvals/stage-6.json` exists the briefs are
frozen: drift is then also reported as frozen unless a register change has been recorded
(`program/changes/register-log.md` newer than the approval).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from string import Template

from common import KitError, find_root, sha256_bytes, write_text
from register import ID_PATTERNS, KINDS, load

KIT_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = KIT_DIR / "templates"
TEMPLATE_NAMES = {"phase": "phase_brief.md", "milestone": "milestone_brief.md",
                  "step": "step_brief.md", "task": "task_brief.md"}
SUBDIRS = {"phase": "phases", "milestone": "milestones", "step": "steps", "task": "tasks"}
_CHILD = ("milestones", "steps", "tasks")
_PLACEHOLDERS = ("id", "title", "text", "role", "size", "environments", "depends_on", "gate", "closes",
                 "phase", "milestone", "step", "children", "tests_unit", "tests_integration", "done")
BRIEFS = "program/briefs"
FROZEN = "briefs are frozen by program/approvals/stage-6.json; record a register change first"


# --- reading the register ------------------------------------------------------

def _cell(value: str) -> str:
    """One line, safe inside a markdown table cell."""
    return " ".join(value.split()).replace("|", "\\|")


def _string(node: dict, field: str, who: str, default: str = "-") -> str:
    value = node.get(field)
    if value is None:
        return default
    if not isinstance(value, str):
        raise KitError(f"{who}: {field} must be a string")
    return value


def _list(node: dict, field: str, who: str) -> str:
    value = node.get(field)
    if value is None or value == []:
        return "-"
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise KitError(f"{who}: {field} must be a list of strings")
    return ", ".join(_cell(item) for item in value) or "-"


def _body(node: dict, field: str, who: str) -> str:
    return _string(node, field, who).strip() or "-"


def _line(node: dict, field: str, who: str) -> str:
    return _cell(_string(node, field, who)) or "-"


def _walk(register: dict):
    """Yield (level, node, parents) in register order, where parents holds the ancestors' IDs."""
    seen: set[str] = set()

    def visit(items, label: str, level: int, parents: tuple[str, ...]):
        if not isinstance(items, list):
            raise KitError(f"{label} must be a list")
        for number, node in enumerate(items, 1):
            where = f"{label}[{number}]"
            if not isinstance(node, dict):
                raise KitError(f"{where} must be a mapping")
            ident = node.get("id")
            if not isinstance(ident, str) or not ID_PATTERNS[KINDS[level]].match(ident):
                raise KitError(f"{where}: id {ident!r} is not a valid {KINDS[level]} id; "
                               f"run `pk register assign-ids`")
            if ident in seen:
                raise KitError(f"{ident}: duplicate id")
            seen.add(ident)
            yield level, node, parents
            if level < 3 and node.get(_CHILD[level]) is not None:
                yield from visit(node[_CHILD[level]], f"{ident}.{_CHILD[level]}", level + 1, parents + (ident,))

    yield from visit(register["phases"], "phases", 0, ())


def _children(node: dict, level: int) -> list[dict]:
    items = node.get(_CHILD[level]) if level < 3 else None
    return items if isinstance(items, list) else []


# --- rendering -------------------------------------------------------------------

def _template(name: str) -> Template:
    path = TEMPLATES / name
    try:
        return Template(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise KitError(f"cannot read template {path}: {getattr(exc, 'strerror', None) or exc}") from exc


def _fill(name: str, template: Template, values: dict[str, str]) -> str:
    try:
        text = template.substitute(values)
    except KeyError as exc:
        raise KitError(f"template {name}: unknown placeholder ${exc.args[0]}") from exc
    except ValueError as exc:
        raise KitError(f"template {name}: {exc}") from exc
    return text.rstrip("\n") + "\n"


def render(root: Path) -> dict[str, bytes]:
    """Every generated file as {path relative to root (posix): bytes}. Writes nothing."""
    document = load(root)
    templates = {kind: _template(name) for kind, name in TEMPLATE_NAMES.items()}
    files: dict[str, str] = {}
    catalog = ["# Catalog", "", "| ID | Title | Role | Gate | Depends on |", "|---|---|---|---|---|"]
    for level, node, parents in _walk(document):
        kind = KINDS[level]
        ident = node["id"]
        ids = dict(zip(("phase", "milestone", "step"), parents + (ident,)))
        values = {key: "-" for key in _PLACEHOLDERS}
        values.update(id=ident, title=_line(node, "title", ident), gate=_line(node, "gate", ident),
                      text=_body(node, "text", ident))
        values.update({key: ids.get(key, "-") for key in ("phase", "milestone", "step")})
        children = [f"- {child['id']}: {_line(child, 'title', child['id'])}"
                    for child in _children(node, level) if isinstance(child, dict) and isinstance(child.get("id"), str)]
        values["children"] = "\n".join(children) or "-"
        if level == 3:
            values.update(role=_line(node, "role", ident), size=_line(node, "size", ident),
                          environments=_list(node, "environments", ident),
                          depends_on=_list(node, "depends_on", ident), closes=_list(node, "closes", ident),
                          tests_unit=_body(node, "tests_unit", ident),
                          tests_integration=_body(node, "tests_integration", ident),
                          done=_body(node, "done", ident))
            catalog.append(f"| {ident} | {values['title']} | {values['role']} | {values['gate']} "
                           f"| {values['depends_on']} |")
        name = TEMPLATE_NAMES[kind]
        files[f"{BRIEFS}/{SUBDIRS[kind]}/{ident}.md"] = _fill(name, templates[kind], values)
    files[f"{BRIEFS}/CATALOG.md"] = "\n".join(catalog) + "\n"
    data = {rel: text.encode("utf-8") for rel, text in files.items()}
    manifest = {rel: sha256_bytes(data[rel]) for rel in sorted(data)}
    data[f"{BRIEFS}/MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    return data


def _stale(root: Path, wanted) -> list[str]:
    """Files under the four brief directories that the register no longer produces."""
    found = []
    for sub in SUBDIRS.values():
        folder = Path(root) / BRIEFS / sub
        if folder.is_dir():
            for entry in sorted(folder.iterdir()):
                rel = f"{BRIEFS}/{sub}/{entry.name}"
                if (entry.is_file() or entry.is_symlink()) and rel not in wanted:
                    found.append(rel)
    return found


# --- public interface ------------------------------------------------------------

def generate(root: Path) -> list[Path]:
    """Write the brief tree; remove briefs the register no longer has. Returns the written paths, sorted."""
    root = Path(root)
    files = render(root)  # everything is built before the first write
    try:
        for rel, data in sorted(files.items()):
            path = root / rel
            try:
                if path.read_bytes() == data:
                    continue
            except OSError:
                pass
            write_text(path, data.decode("utf-8"))
        for rel in _stale(root, files):
            (root / rel).unlink()
    except OSError as exc:
        raise KitError(f"cannot write briefs under {root / BRIEFS}: {exc.strerror or exc}") from exc
    return sorted(root / rel for rel in files)


def check(root: Path) -> list[str]:
    """Problems between a fresh render and disk, one string each; empty when the briefs are current."""
    root = Path(root)
    files = render(root)
    problems = []
    for rel, data in sorted(files.items()):
        try:
            current = (root / rel).read_bytes()
        except FileNotFoundError:
            problems.append(f"{rel}: missing; run `pk briefs generate`")
            continue
        except OSError as exc:
            raise KitError(f"cannot read {root / rel}: {exc.strerror or exc}") from exc
        if current != data:
            problems.append(f"{rel}: differs from the register")
    problems += [f"{rel}: not generated from the register" for rel in _stale(root, files)]
    if problems:
        approval = root / "program" / "approvals" / "stage-6.json"
        log = root / "program" / "changes" / "register-log.md"
        if approval.exists() and not (log.exists() and log.stat().st_mtime_ns > approval.stat().st_mtime_ns):
            problems.append(FROZEN)
    return problems


def brief_count(paths: list[Path]) -> int:
    """How many of the generated paths are briefs (not CATALOG.md or MANIFEST.json)."""
    return sum(1 for path in paths if path.parent.name in SUBDIRS.values())


# --- command ---------------------------------------------------------------------

def cmd_briefs(args: argparse.Namespace) -> int:
    action = getattr(args, "briefs_action", None)
    if action not in ("generate", "check"):
        raise KitError("briefs needs an action: generate or check")
    root = find_root()
    if action == "generate":
        print(f"generated {brief_count(generate(root))} briefs")
        return 0
    problems = check(root)
    if problems:
        print("\n".join("error: " + problem for problem in problems), file=sys.stderr)
        return 1
    print("briefs up to date")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="briefs_action", metavar="action", parser_class=type(parser))
    actions.add_parser("generate", help="write program/briefs/ from the register")
    actions.add_parser("check", help="report briefs that differ from the register")


def register(add_command) -> None:
    add_command("briefs", cmd_briefs, help="generate and check the brief tree", configure=_configure)
