#!/usr/bin/env python3
"""Change requests after the freeze: `program/changes/CR-<nnn>.md` (standard library only).

A change of meaning to anything approved at stage 5 or later is a change request with the
sections Change, Why, Asked by, Impact, Approval and Applied, in that order. `create` starts
one from `kit/templates/change_request.md`; `impact` records what the change reaches (from the
traceability matrix) and is refused once the request is approved, so the impact the approver saw
cannot change afterwards; `approve` fills Approval; `apply` records the register change, checks
the matrix again and fills Applied. Sections are found by their `## ` headings, so a value that
would start a heading line is refused.

A change request never edits a done task's evidence or acceptance: nothing here writes there.
Done tasks affected by a change get a follow-up task in the register instead.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import register as register_module
import trace as trace_module
from common import KitError, _atomic_write, find_root, utc_now

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "change_request.md"
HEADINGS = ("Change", "Why", "Asked by", "Impact", "Approval", "Applied")
_FILE = re.compile(r"CR-([0-9]{3,})\.md\Z")
_ID = re.compile(r"CR-[0-9]{3,}\Z")
_HEADING = re.compile(r"^## (.*?)[ \t\r]*$", re.MULTILINE)
_ATTEMPTS = 5  # tries at a free number before create gives up
_FIELDS = re.compile(r"\{(id|change|why|asked_by)\}")


def _folder(root: Path) -> Path:
    return Path(root) / "program" / "changes"


def _text(label: str, value: str, one_line: bool = True) -> str:
    """`value` stripped; refused when empty, spread over lines (unless allowed) or able to forge a heading."""
    if not isinstance(value, str) or not value.strip():
        raise KitError(f"{label} must not be empty")
    value = value.strip()
    if one_line and len(value.splitlines()) != 1:
        raise KitError(f"{label} must be one line")
    if any(line.startswith("## ") for line in value.splitlines()):
        raise KitError(f"{label} must not contain a line starting with '## '")
    return value


def _spans(text: str, label: str) -> dict[str, tuple[int, int, int]]:
    """name -> (heading start, heading end, section end) for each `## ` heading; all six must appear once."""
    matches = list(_HEADING.finditer(text))
    found: dict[str, tuple[int, int, int]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if match.group(1) in found:
            raise KitError(f"{label}: heading '## {match.group(1)}' appears more than once")
        found[match.group(1)] = (match.start(), match.end(), end)
    for name in HEADINGS:
        if name not in found:
            raise KitError(f"{label}: missing heading '## {name}'")
    return found


def section(text: str, name: str, label: str = "change request") -> str:
    """The stripped body of section `name` of change request `text`."""
    _, heading_end, end = _spans(text, label)[name]
    return text[heading_end:end].strip()


def _replace(text: str, name: str, body: str, label: str) -> str:
    start, _, end = _spans(text, label)[name]
    block = f"## {name}\n" + (f"\n{body}\n" if body else "") + ("\n" if end < len(text) else "")
    return text[:start] + block + text[end:]


def _number(name: str) -> int | None:
    match = _FILE.match(name)
    return int(match.group(1)) if match else None


def _path(root: Path, cr_id: str) -> Path:
    if not isinstance(cr_id, str) or not _ID.match(cr_id):
        raise KitError(f"{cr_id!r} is not a change request id (expected CR-nnn)")
    return _folder(root) / f"{cr_id}.md"


def _read(root: Path, cr_id: str) -> tuple[Path, str]:
    path = _path(root, cr_id)
    try:
        return path, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KitError(f"{cr_id}: no such change request (program/changes/{cr_id}.md)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise KitError(f"cannot read {path}: {getattr(exc, 'strerror', None) or exc}") from exc


def _create_exclusive(path: Path, text: str) -> bool:
    """Create `path` with `text`; False when it already exists (another create won the number)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        return False
    except OSError as exc:
        raise KitError(f"cannot create {path}: {exc.strerror or exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def _next_id(root: Path) -> str:
    folder = _folder(root)
    numbers = [n for n in (_number(p.name) for p in folder.iterdir()) if n is not None] if folder.is_dir() else []
    return f"CR-{max(numbers, default=0) + 1:03d}"


def create(root: Path, title: str, why: str, asked_by: str) -> str:
    """Write the next change request (CR-001, CR-002, ...) and return its ID.

    Numbers continue after the highest existing file, so gaps are not reused; names that do not
    match CR-<nnn>.md are ignored. Nothing is written when an argument is refused. The file is
    created exclusively, and the next number is tried when another create took it first.
    """
    title = _text("title", title)
    why = _text("why", why, one_line=False)
    asked_by = _text("asked-by", asked_by)
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KitError(f"cannot read {TEMPLATE}: {getattr(exc, 'strerror', None) or exc}") from exc
    for _ in range(_ATTEMPTS):
        cr_id = _next_id(root)
        values = {"id": cr_id, "change": title, "why": why, "asked_by": asked_by}
        text = _FIELDS.sub(lambda match: values[match.group(1)], template)
        _spans(text, str(TEMPLATE))  # a damaged template is refused before anything is written
        if _create_exclusive(_path(root, cr_id), text):
            return cr_id
    raise KitError(f"could not allocate a change request number after {_ATTEMPTS} attempts")


def _listing(names: list[str]) -> str:
    return ", ".join(names) if names else "none"


def impact(root: Path, cr_id: str, ids: list[str]) -> dict:
    """Write the Impact section from `trace.impact(ids)`, replacing any earlier one; return the dict.

    Refused once Approval is filled: the impact the approver saw must not change afterwards.
    """
    path, text = _read(root, cr_id)
    if section(text, "Approval", cr_id):
        raise KitError(f"{cr_id} is already approved; its impact can no longer change")
    if not ids:
        raise KitError("impact needs at least one ID")
    reached = trace_module.impact(root, list(ids))
    tasks = reached["tasks"]
    body = "\n".join([
        f"- Changed: {', '.join(ids)}",
        f"- Requirements: {_listing(reached['requirements'])}",
        f"- Components: {_listing(reached['components'])}",
        f"- Gates: {_listing(reached['gates'])}",
        f"- Done tasks: {_listing(tasks['done'])}",
        f"- In-flight tasks: {_listing(tasks['in_flight'])}",
        f"- Not-started tasks: {_listing(tasks['not_started'])}",
    ])
    _atomic_write(path, _replace(text, "Impact", body, cr_id))
    return reached


def approve(root: Path, cr_id: str, by: str) -> None:
    """Fill Approval with today's date and `by`. Needs an Impact, and an empty Approval."""
    by = _text("approver", by)
    path, text = _read(root, cr_id)
    if section(text, "Approval", cr_id):
        raise KitError(f"{cr_id} is already approved")
    if not section(text, "Impact", cr_id):
        raise KitError(f"{cr_id} has no impact yet; run: pk change impact {cr_id} ID...")
    _atomic_write(path, _replace(text, "Approval", f"{utc_now()[:10]}: {by}", cr_id))


def apply(root: Path, cr_id: str, note: str) -> None:
    """Record the register change, check the matrix at stage 6, then fill Applied.

    Refused without Approval or when already applied. The register log entry
    (`<CR-id>: <note>`) is written first and the log is append-only, so when the matrix
    check reports problems the entry stays, Applied stays empty and KitError lists the
    problems; apply again after fixing them (which appends a further log line).
    """
    note = _text("note", note)
    path, text = _read(root, cr_id)
    if section(text, "Applied", cr_id):
        raise KitError(f"{cr_id} is already applied")
    if not section(text, "Approval", cr_id):
        raise KitError(f"{cr_id} is not approved; apply needs an Approval")
    register_module.change(root, f"{cr_id}: {note}")
    problems = trace_module.validate(root, 6)
    if problems:
        raise KitError(f"{cr_id} not applied: traceability at stage 6 has {len(problems)} problem(s): "
                       + "; ".join(problems) + ". The register log entry stays (the log is append-only)")
    _atomic_write(path, _replace(text, "Applied", f"{utc_now()[:10]}: {note}", cr_id))


def open_requests(root: Path) -> list[dict]:
    """The change requests whose Applied section is empty, in number order: id, title, approved."""
    folder = _folder(root)
    if not folder.is_dir():
        return []
    found = []
    for number, name in sorted((n, p.name) for p in folder.iterdir() if (n := _number(p.name)) is not None):
        cr_id = name[:-3]
        _, text = _read(root, cr_id)
        if not section(text, "Applied", cr_id):
            title = section(text, "Change", cr_id).splitlines()
            found.append({"id": cr_id, "title": title[0] if title else "",
                          "approved": bool(section(text, "Approval", cr_id))})
    return found


# --- command -----------------------------------------------------------------

def cmd_change(args: argparse.Namespace) -> int:
    action = getattr(args, "change_action", None)
    if action not in ("create", "impact", "approve", "apply"):
        raise KitError("change needs an action: create, impact, approve or apply")
    root = find_root()
    if action == "create":
        output = create(root, args.title, args.why, args.asked_by)
    elif action == "impact":
        output = json.dumps(impact(root, args.cr_id, args.ids), indent=2, sort_keys=True)
    elif action == "approve":
        approve(root, args.cr_id, args.by)
        output = f"{args.cr_id} approved by {args.by.strip()}"
    else:
        apply(root, args.cr_id, args.note)
        output = f"{args.cr_id} applied"
    print(output)
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="change_action", metavar="action", parser_class=type(parser))
    creator = actions.add_parser("create", help="start a change request; prints its ID")
    creator.add_argument("--title", required=True, help="what changes, one line")
    creator.add_argument("--why", required=True, help="why it changes")
    creator.add_argument("--asked-by", required=True, dest="asked_by", help="who asked")
    finder = actions.add_parser("impact", help="write the impact of the given IDs into a change request")
    finder.add_argument("cr_id", metavar="CR-nnn")
    finder.add_argument("ids", nargs="+", metavar="ID", help="journey, requirement, component or gate IDs")
    approver = actions.add_parser("approve", help="record the approval of a change request")
    approver.add_argument("cr_id", metavar="CR-nnn")
    approver.add_argument("--by", required=True, help="who approved, one line")
    applier = actions.add_parser("apply", help="record the register change, re-check the matrix, mark applied")
    applier.add_argument("cr_id", metavar="CR-nnn")
    applier.add_argument("--note", required=True, help="what was amended, one line")


def register(add_command) -> None:
    add_command("change", cmd_change, help="change requests: create, impact, approve, apply", configure=_configure)
