#!/usr/bin/env python3
"""The feedback register, `program/beta-feedback.md` (0.6.0): the user's feedback during a beta, one table row each.

The file has a free header (`kit/templates/beta-feedback.md` explains the columns) and one table:

    | BF | Date | Urgency | Summary | Touches | Decision | State | Ruling |
    |---|---|---|---|---|---|---|---|
    | BF-1 | 2026-11-09 | urgent | Join fails on a phone | T1 | fold-in | open | - |

BF is `BF-n`, never reused; Urgency `urgent`, `high` or `polish`; Decision `fold-in`, `follow-up`, `pull-forward`
or `batched`; State `open`, `in progress`, `done` or `declined`; Ruling `R<n>[, R<n>]` or `-`. `read` validates
every row and names the row and the field of the first that does not read, so nothing is ever made from a register
it cannot read. `pk feedback add` appends the next BF-n with today's date, state open and ruling `-` (creating the
file from the template when absent); `set` replaces named cells of one row; `list` and `check` read. The file is
optional: without it the dashboard has no Feedback panel and the hand-off no Feedback line.
"""
from __future__ import annotations

import argparse
import html
import re
from datetime import date
from pathlib import Path

from common import KitError, find_root, read_text, write_text

FILE = "program/beta-feedback.md"
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "beta-feedback.md"
HEADER = ("BF", "Date", "Urgency", "Summary", "Touches", "Decision", "State", "Ruling")
FIELDS = ("bf", "date", "urgency", "summary", "touches", "decision", "state", "ruling")
URGENCY = ("urgent", "high", "polish")
DECISION = ("fold-in", "follow-up", "pull-forward", "batched")
STATE = ("open", "in progress", "done", "declined")
OPEN = ("open", "in progress")
DEFAULT_DECISION = {"urgent": "fold-in", "high": "follow-up", "polish": "batched"}  # what each urgency means
DONE_SHOWN = 5
RULING = re.compile(r"-|R\d+(, R\d+)*")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_header(line: str) -> bool:
    return line.lstrip().startswith("|") and tuple(_cells(line)) == HEADER


def _is_row(line: str) -> bool:
    return line.startswith("| BF") and not _is_header(line)


def _problem(row: dict) -> str | None:
    checks = (("bf", re.fullmatch(r"BF-\d+", row["bf"]) is not None, "BF-n"),
              ("date", re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]) is not None, "YYYY-MM-DD"),
              ("urgency", row["urgency"] in URGENCY, " | ".join(URGENCY)),
              ("summary", bool(row["summary"]), "one line of text"),
              ("touches", bool(row["touches"]), "task IDs or none"),
              ("decision", row["decision"] in DECISION, " | ".join(DECISION)),
              ("state", row["state"] in STATE, " | ".join(STATE)),
              ("ruling", RULING.fullmatch(row["ruling"]) is not None, "R<n> or -"))
    for field, ok, want in checks:
        if not ok:
            return f"{row['bf']}: {field} {row[field]!r} is not {want}"
    return None


def _parse(text: str) -> list[dict]:
    rows, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        if not _is_row(line):
            continue
        cells = _cells(line)
        name = cells[0] or "?"
        if len(cells) != len(FIELDS):
            raise KitError(f"{FILE}: {name}: {len(cells)} columns, the register has {len(FIELDS)} (line {number})")
        row = dict(zip(FIELDS, cells))
        problem = _problem(row)
        if problem:
            raise KitError(f"{FILE}: {problem} (line {number})")
        if row["bf"] in seen:
            raise KitError(f"{FILE}: {row['bf']}: duplicate BF number (line {number}); a number is never reused")
        seen.add(row["bf"])
        row["n"], row["line"] = int(row["bf"][3:]), number
        rows.append(row)
    return rows


def exists(root: Path) -> bool:
    return (Path(root) / FILE).is_file()


def read(root: Path) -> list[dict]:
    """Every row in file order, validated; [] when the file is absent or has no row."""
    path = Path(root) / FILE
    return _parse(read_text(path)) if path.is_file() else []


def _row_line(row: dict) -> str:
    return "| " + " | ".join(row[field] for field in FIELDS) + " |"


def _text(value: str, what: str) -> str:
    value = " ".join(str(value).split())
    if not value:
        raise KitError(f"--{what} needs text")
    if "|" in value:
        raise KitError(f"--{what} must not contain '|' (it would split the table cell)")
    return value


def _touches(value: str) -> str:
    items = [item.strip() for item in _text(value, "touches").split(",") if item.strip()]
    return ", ".join(items) or "none"


def _rulings(value: str) -> str:
    value = ", ".join(part.strip() for part in str(value).split(",") if part.strip()) or str(value).strip()
    if not RULING.fullmatch(value):
        raise KitError(f"--ruling {value!r} must be R<n>, several comma separated, or -")
    return value


def _choice(value: str, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise KitError(f"--{what} {value!r} must be one of {', '.join(allowed)}")
    return value


def add(root: Path, urgency: str, summary: str, touches: str = "none", decision: str | None = None) -> dict:
    """Append the next row and return it; the file is created from the template when absent."""
    urgency = _choice(urgency, URGENCY, "urgency")
    row = {"date": date.today().isoformat(), "urgency": urgency, "summary": _text(summary, "summary"),
           "touches": _touches(touches), "decision": _choice(decision or DEFAULT_DECISION[urgency], DECISION,
                                                            "decision"),
           "state": "open", "ruling": "-"}
    path = Path(root) / FILE
    text = read_text(path, newline="") if path.is_file() else read_text(TEMPLATE)
    rows = _parse(text)
    row["bf"] = f"BF-{max((r['n'] for r in rows), default=0) + 1}"
    lines = text.splitlines(keepends=True)
    if rows:
        at = rows[-1]["line"]  # after the last row
    else:
        header = next((n for n, line in enumerate(lines) if _is_header(line)), None)
        if header is None:
            raise KitError(f"{FILE} has no table header `| {' | '.join(HEADER)} |`; add it (kit/templates/"
                           "beta-feedback.md has the file's shape)")
        at = header + 2 if header + 1 < len(lines) and set(lines[header + 1].strip()) <= set("|-: ") else header + 1
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, _row_line(row) + "\n")
    write_text(path, "".join(lines))
    return row


def set_fields(root: Path, bf: str, **changes) -> dict:
    """Replace the given cells (state, decision, ruling, touches) of row `bf` and return the row."""
    wanted = {key: value for key, value in changes.items() if value is not None}
    if not wanted:
        raise KitError("feedback set needs at least one of --state, --decision, --ruling, --touches")
    clean = {}
    for key, value in wanted.items():
        clean[key] = {"state": lambda v: _choice(v, STATE, "state"),
                      "decision": lambda v: _choice(v, DECISION, "decision"),
                      "ruling": _rulings, "touches": _touches}[key](value)
    path = Path(root) / FILE
    if not path.is_file():
        raise KitError(f"{FILE} does not exist; pk feedback add creates it")
    text = read_text(path, newline="")
    row = next((r for r in _parse(text) if r["bf"] == bf), None)
    if row is None:
        raise KitError(f"{bf} is not a row of {FILE} (pk feedback list)")
    row.update(clean)
    lines = text.splitlines(keepends=True)
    old = lines[row["line"] - 1]
    ending = old[len(old.rstrip("\r\n")):]
    lines[row["line"] - 1] = _row_line(row) + ending
    write_text(path, "".join(lines))
    return row


def open_rows(rows: list[dict]) -> list[dict]:
    """The open and in-progress rows, by urgency (urgent, high, polish), then by number."""
    return sorted((r for r in rows if r["state"] in OPEN), key=lambda r: (URGENCY.index(r["urgency"]), r["n"]))


def done_rows(rows: list[dict]) -> list[dict]:
    """The done rows, most recent first (by date, then number)."""
    return sorted((r for r in rows if r["state"] == "done"), key=lambda r: (r["date"], r["n"]), reverse=True)


def handoff_line(root: Path) -> str | None:
    """The hand-off's one Feedback line, or None without the file."""
    if not exists(root):
        return None
    try:
        rows = open_rows(read(root))
    except KitError:
        return f"Feedback ({FILE}): does not read; run pk feedback check"
    counts = ", ".join(f"{sum(r['urgency'] == u for r in rows)} {u}" for u in URGENCY)
    return f"Feedback ({FILE}): {len(rows)} open: {counts}"


def panel_html(root: Path) -> str:
    """The dashboard's Feedback panel ('' without the file); a row that does not read raises, naming it."""
    if not exists(root):
        return ""
    rows = read(root)
    e = lambda value: html.escape(str(value), quote=True)  # noqa: E731

    def item(r: dict) -> str:
        meta = [r["state"], r["decision"], f"touches {r['touches']}",
                r["ruling"] if r["ruling"] != "-" else "no ruling yet", r["date"]]
        return (f'<li><span class="chip u-{e(r["urgency"])}">{e(r["urgency"])}</span> <code>{e(r["bf"])}</code> '
                f'{e(r["summary"])}<br><span class=meta>{e(" · ".join(meta))}</span></li>')

    opened, done = open_rows(rows), done_rows(rows)
    body = (f"<ul>{''.join(item(r) for r in opened)}</ul>" if opened
            else '<p class="empty">No feedback open.</p>')
    if done:
        body += f"<h3>Recently done</h3><ul>{''.join(item(r) for r in done[:DONE_SHOWN])}</ul>"
    return (f'\n<h2>Feedback</h2>\n<div class="panel"><p class=meta>{len(opened)} open, {len(done)} done '
            f'&middot; {FILE}</p>{body}</div>')


# --- command -----------------------------------------------------------------

def _show(r: dict) -> str:
    return f"{r['bf']} {r['urgency']} {r['state']} {r['decision']} touches {r['touches']} {r['ruling']}: {r['summary']}"


def cmd_feedback(args: argparse.Namespace) -> int:
    action = getattr(args, "feedback_action", None)
    root = find_root()
    if action == "add":
        row = add(root, args.urgency, args.summary, args.touches, args.decision)
        print(f"feedback: added {row['bf']} ({row['urgency']}, {row['decision']}, {row['state']})")
    elif action == "set":
        row = set_fields(root, args.bf, state=args.state, decision=args.decision, ruling=args.ruling,
                         touches=args.touches)
        print("feedback: " + _show(row))
    elif action == "list":
        rows = read(root)
        for row in (open_rows(rows) if args.open else rows):
            print(_show(row))
    elif action == "check":
        rows = read(root)
        if not exists(root):
            print(f"feedback: no {FILE} (pk feedback add creates it)")
        else:
            print(f"feedback: ok, {len(rows)} row(s), {len(open_rows(rows))} open")
    else:
        raise KitError("feedback needs an action: add, set, list or check")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="feedback_action", metavar="action", parser_class=type(parser))
    adder = actions.add_parser("add", help=f"append the next BF-n row to {FILE} (created when absent)")
    adder.add_argument("--urgency", required=True, choices=URGENCY)
    adder.add_argument("--summary", required=True, help="one line, in the user's terms")
    adder.add_argument("--touches", default="none", help="task IDs, comma separated (default none)")
    adder.add_argument("--decision", choices=DECISION,
                       help="default by urgency: urgent fold-in, high follow-up, polish batched")
    setter = actions.add_parser("set", help="replace cells of one row")
    setter.add_argument("bf", metavar="BF-n")
    setter.add_argument("--state", choices=STATE)
    setter.add_argument("--decision", choices=DECISION)
    setter.add_argument("--ruling", metavar="R<n>", help="R<n>, several comma separated, or -")
    setter.add_argument("--touches", help="task IDs, comma separated, or none")
    lister = actions.add_parser("list", help="every row, or with --open the open ones by urgency")
    lister.add_argument("--open", action="store_true")
    actions.add_parser("check", help=f"validate {FILE}")


def register(add_command) -> None:
    add_command("feedback", cmd_feedback, help="the feedback register: add, set, list, check", configure=_configure)
