#!/usr/bin/env python3
"""Append-only rulings log (`program/rulings.md`).

Each ruling is one block:

    ## R<n> (<date>, <role>)

    **Question.** ...

    **Decision.** ...

    **Precedent.** ...

`add` only ever appends; earlier bytes are never rewritten. A ruling supersedes
an earlier one by writing `supersedes R<n>` anywhere in its decision.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import KitError, find_root, utc_now

_HEADER = re.compile(r"## R(\d+) \((\d{4}-\d{2}-\d{2}), ([^()\n]+)\)\Z")
_SUPERSEDES = re.compile(r"\bsupersedes R(\d+)\b", re.IGNORECASE)
_FIELDS = ("Question", "Decision", "Precedent")
_MARKERS = tuple(f"**{name}.**" for name in _FIELDS)
_TITLE = "# Rulings\n"


def _path(root: Path) -> Path:
    return Path(root) / "program" / "rulings.md"


def _load(root: Path) -> bytes:
    path = _path(root)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc.strerror or exc}") from exc


def _parse(data: bytes, name: str = "rulings.md") -> list[tuple[dict, int]]:
    """Every ruling with the line its header is on; KitError names the line of a malformed block."""
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise KitError(f"{name}: not valid UTF-8: {exc.reason}") from exc
    starts = []
    for index, line in enumerate(lines):
        if line.startswith("## R"):
            if not _HEADER.match(line.rstrip()):
                raise KitError(f"{name} line {index + 1}: malformed ruling header {line!r}; "
                               f"expected '## R<n> (<YYYY-MM-DD>, <role>)'")
            starts.append(index)
    found = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        number, date, role = _HEADER.match(lines[start].rstrip()).groups()
        marks = []  # (field, line index) in the order they appear
        for index in range(start + 1, end):
            for field, marker in zip(_FIELDS, _MARKERS):
                if lines[index].startswith(marker):
                    marks.append((field, index))
        order = [field for field, _ in marks]
        if order != list(_FIELDS):
            missing = [field for field in _FIELDS if field not in order]
            why = (f"is missing {', '.join('**' + m + '.**' for m in missing)}" if missing
                   else f"has its fields out of order or repeated ({', '.join(order)}); "
                        f"expected {', '.join(_FIELDS)}")
            raise KitError(f"{name} line {start + 1}: R{number} {why}")
        text = {}
        for (field, index), following in zip(marks, [*(i for _, i in marks[1:]), end]):
            body = [lines[index][len(f"**{field}.**"):], *lines[index + 1:following]]
            text[field] = "\n".join(body).strip()
        decision = text["Decision"]
        supersedes = list(dict.fromkeys(int(n) for n in _SUPERSEDES.findall(decision)))
        found.append(({"number": int(number), "date": date, "role": role.strip(),
                       "question": text["Question"], "decision": decision,
                       "precedent": text["Precedent"], "supersedes": supersedes}, start + 1))
    return found


def read(root: Path) -> list[dict]:
    return [ruling for ruling, _ in _parse(_load(root))]


def _check_text(label: str, value: str, required: bool) -> str:
    value = value.strip()
    if required and not value:
        raise KitError(f"a ruling needs a {label}")
    for line in value.splitlines():
        if line.startswith("## ") or line.startswith(_MARKERS):
            raise KitError(f"{label} must not contain a line starting with {line.split(' ', 1)[0]!r}; "
                           f"it would corrupt the log")
    return value


def add(root: Path, role: str, question: str, decision: str, precedent: str = "") -> int:
    """Append the next ruling and return its number. Nothing is written on any refusal."""
    role = role.strip()
    if len(role.splitlines()) != 1 or "(" in role or ")" in role:  # splitlines: \r, \x0b, \x85, \u2028 ... too
        raise KitError(f"invalid role {role!r}: one non-empty line, no parentheses")
    question = _check_text("question", question, True)
    decision = _check_text("decision", decision, True)
    precedent = _check_text("precedent", precedent, False)
    data = _load(root)
    existing = _parse(data)
    number = max((ruling["number"] for ruling, _ in existing), default=0) + 1
    earlier = {ruling["number"] for ruling, _ in existing}
    for target in _SUPERSEDES.findall(decision):
        if int(target) not in earlier:
            raise KitError(f"R{number}: supersedes R{target}, which is not an earlier ruling")
    block = (f"\n## R{number} ({utc_now()[:10]}, {role})\n\n**Question.** {question}\n\n"
             f"**Decision.** {decision}\n\n" + (f"**Precedent.** {precedent}\n" if precedent else "**Precedent.**\n"))
    path = _path(root)
    prefix = _TITLE if not data else ("" if data.endswith(b"\n") else "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "ab") as handle:
            handle.write((prefix + block).encode("utf-8"))
    except OSError as exc:
        raise KitError(f"cannot write {path}: {exc.strerror or exc}") from exc
    return number


def validate(root: Path) -> list[str]:
    """Problems with the log, one line each; empty when all is well."""
    try:
        found = _parse(_load(root))
    except KitError as exc:
        return [str(exc)]
    problems: list[str] = []
    seen: set[int] = set()
    expected, previous = 1, 0
    for ruling, line in found:
        number = ruling["number"]
        if number in seen:
            problems.append(f"rulings.md line {line}: R{number} appears more than once")
        else:
            if number < previous:
                problems.append(f"rulings.md line {line}: R{number} is out of order after R{previous}")
            elif number != expected:
                problems.append(f"rulings.md line {line}: R{number} found where R{expected} expected "
                                f"(numbers run 1, 2, 3 with no gaps)")
            expected, previous = number + 1, number
        for target in ruling["supersedes"]:
            if target not in seen:
                problems.append(f"rulings.md line {line}: R{number} supersedes R{target}, "
                                f"which is not an earlier ruling")
        seen.add(number)
    return problems


def cmd_rulings(args: argparse.Namespace) -> int:
    action = getattr(args, "rulings_action", None)
    root = find_root()
    if action == "add":
        print(f"R{add(root, args.role, args.question, args.decision, args.precedent)}")
    elif action == "list":
        for ruling in read(root):
            line = [f"R{ruling['number']}", ruling["date"], ruling["role"], " ".join(ruling["question"].split())]
            if ruling["supersedes"]:
                line.append("supersedes " + ", ".join(f"R{n}" for n in ruling["supersedes"]))
            print("\t".join(line))
    elif action == "validate":
        problems = validate(root)
        if problems:
            raise KitError("; ".join(problems))
        print("rulings: ok")
    else:
        raise KitError("rulings needs an action: add, list or validate")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="rulings_action", metavar="action", parser_class=type(parser))
    adder = actions.add_parser("add", help="append the next ruling")
    adder.add_argument("--role", required=True, help="who decided")
    adder.add_argument("--question", required=True, help="the question settled")
    adder.add_argument("--decision", required=True, help="what was decided (may say 'supersedes R<n>')")
    adder.add_argument("--precedent", default="", help="the rule this generalises")
    actions.add_parser("list", help="one line per ruling")
    actions.add_parser("validate", help="check numbering and supersedes links")


def register(add_command) -> None:
    add_command("rulings", cmd_rulings, help="append-only rulings log", configure=_configure)
