#!/usr/bin/env python3
"""The research track (0.6.0): an isolated folder of strategy items that graduate into the pipeline only by a ruling.

Opt-in through the `research` section of `program/checks.yaml`; without it every `pk research` command says it is
not configured and changes nothing. Every key has a default:

    research:
      folder: research                  # the isolated folder
      index: research/README.md         # default <folder>/README.md: the items table
      archive: docs/research-archive    # default none: where `graduate --archive` moves a graduated item's file
      item_pattern: 'RI-[0-9]+'         # an item's ID
      other_ids:                        # default none: IDs that may appear only beside a graduated item on a line
        - 'RD-[0-9]+'                   #   that speaks of research (decisions, hypotheses)
      guard:                            # the files that may cite only graduated items (globs; ** crosses folders)
        - program/tasks/*/SPEC.md       # default: the task SPEC, PLAN and BRIEF files and program/briefs/**/*.md

The index lists one item per markdown table row, `| <ID> | [title](file) | status |`, the link relative to the
index; any number of tables may hold such rows (open items, graduated ones). An item is graduated when its status
says `graduated` (not "not graduated" or "ungraduated") with a ruling `R<n>`, the first after the word, that is in
`program/rulings.md` and whose text names the item's ID. Why a ruling: the folder holds what is being considered,
and only the user's recorded word turns any of it into work.

The guard (`pk research check`, and D8 of `pk drift` when the section is present) refuses, as `<file>:<line>: <why>`:
an item ID that is not graduated, or not listed when the line speaks of research; a path into the folder that is
an ungraduated item's file, or no item's file, on a line that cites no graduated item; an `other_ids` ID on a line
that speaks of research (a research path or ID, or the word) and cites no graduated item. An ID glued to a word,
a colon, a slash or a hyphen (`case:RI-01`) is a label, not a citation. The research folder itself is skipped.
`graduate` writes the status cell `graduated by R<n> (<date>)` once the ruling names the item, and with
`--archive` moves the item's file into the archive with `git mv` and points the row's link at it.
"""
from __future__ import annotations

import argparse
import posixpath
import re
import subprocess
from datetime import date
from pathlib import Path, PurePosixPath

from common import KitError, find_root, git, read_text, read_yaml, write_text

KEYS = ("folder", "index", "archive", "item_pattern", "other_ids", "guard")
DEFAULT_GUARD = ("program/tasks/*/SPEC.md", "program/tasks/*/PLAN.md", "program/tasks/*/BRIEF.md",
                 "program/briefs/**/*.md")
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "research-README.md"
RULINGS = "program/rulings.md"
NOT_CONFIGURED = ("research is not configured: add a `research` section to program/checks.yaml (an empty mapping "
                  "takes every default; kit/templates/checks.yaml has the example), then run pk research init")
GRADUATED = re.compile(r"(?<!not )(?<!un)\bgraduated\b", re.I)
RULING = re.compile(r"\bR(\d+)\b")
WORD = re.compile(r"\bresearch\b", re.I)
EDGE = r"(?<![\w:/-])"  # an ID glued to a word, a colon, a slash or a hyphen is a label, not a citation
SHOWN = 6  # problems named in a drift line


def _relative(value, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KitError(f"checks.yaml: research.{key} must be a path (a string)")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise KitError(f"checks.yaml: research.{key}: {value!r} must be a relative path inside the repository")
    return value.rstrip("/") or value


def _pattern(value, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KitError(f"checks.yaml: research.{key} must be a regular expression (a quoted string)")
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise KitError(f"checks.yaml: research.{key}: {value!r} is not a regular expression: {exc}") from exc
    if compiled.fullmatch(""):
        raise KitError(f"checks.yaml: research.{key}: {value!r} matches an empty string; write the ID's shape, "
                       "such as 'RI-[0-9]+'")
    return value


def parse(value) -> dict | None:
    """The `research` section as {folder, index, archive, item_pattern, other_ids, guard}, or None when absent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KitError("checks.yaml: research must be a mapping of " + ", ".join(KEYS) + " (each optional)")
    unknown = sorted(str(key) for key in value if key not in KEYS)
    if unknown:
        raise KitError(f"checks.yaml: research: unknown key {', '.join(unknown)} (expected {', '.join(KEYS)})")
    folder = _relative(value.get("folder", "research"), "folder")
    index = _relative(value["index"], "index") if value.get("index") is not None else f"{folder}/README.md"
    archive = _relative(value["archive"], "archive") if value.get("archive") is not None else None
    others = value.get("other_ids", [])
    if not isinstance(others, list):
        raise KitError("checks.yaml: research.other_ids must be a list of regular expressions (quoted strings)")
    guard = value.get("guard", list(DEFAULT_GUARD))
    if not isinstance(guard, list) or not guard or not all(isinstance(g, str) and g.strip() for g in guard):
        raise KitError("checks.yaml: research.guard must be a non-empty list of globs")
    for item in guard:
        if PurePosixPath(item).is_absolute() or ".." in PurePosixPath(item).parts:
            raise KitError(f"checks.yaml: research.guard: {item!r} must be a relative glob inside the repository")
    return {"folder": folder, "index": index, "archive": archive,
            "item_pattern": _pattern(value.get("item_pattern", "RI-[0-9]+"), "item_pattern"),
            "other_ids": [_pattern(item, "other_ids") for item in others], "guard": list(guard)}


def config(root: Path) -> dict | None:
    """The research section of the checkout's `program/checks.yaml`; None when the file or the section is absent."""
    path = Path(root) / "program" / "checks.yaml"
    if not path.is_file():
        return None
    data = read_yaml(path)
    return parse(data.get("research") if isinstance(data, dict) else None)


def _required(root: Path) -> dict:
    cfg = config(root)
    if cfg is None:
        raise KitError(NOT_CONFIGURED)
    return cfg


# --- the index ------------------------------------------------------------------------

LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)[^)]*\)")


def _cells(line: str) -> list[str] | None:
    line = line.strip()
    if len(line) < 2 or not (line.startswith("|") and line.endswith("|")):
        return None
    return [cell.strip() for cell in line[1:-1].split("|")]


def _rows(text: str, cfg: dict):
    """(line index, ID, link as written, resolved file, status) of every item row of the index text."""
    pattern = re.compile(cfg["item_pattern"])
    base = posixpath.dirname(cfg["index"])
    for number, line in enumerate(text.splitlines()):
        cells = _cells(line)
        if not cells or len(cells) != 3 or not pattern.fullmatch(cells[0]):
            continue
        link = LINK.search(cells[1])
        if link is None:
            continue
        target = link.group(1)
        yield number, cells[0], target, posixpath.normpath(posixpath.join(base, target)), cells[2]


def _ruling_texts(root: Path) -> dict[int, str]:
    import rulings as rulings_module

    return {r["number"]: "\n".join((r["question"], r["decision"], r["precedent"]))
            for r in rulings_module.read(root)}


def _names(text: str, rid: str) -> bool:
    return re.search(EDGE + re.escape(rid) + r"(?![\w-])", text) is not None


def _why_not(rid: str, status: str, texts: dict[int, str], cfg: dict) -> str | None:
    """None when the item is graduated by a ruling that exists and names it; else why it is not."""
    word = GRADUATED.search(status)
    ruling = RULING.search(status, word.end()) if word else None
    if word is None or ruling is None:
        return f"{rid} is not marked graduated with a ruling in {cfg['index']} (status: {status})"
    number = int(ruling.group(1))
    if number not in texts:
        return f"{rid} is marked graduated by R{number}, but R{number} is not in {RULINGS}"
    if not _names(texts[number], rid):
        return f"{rid} is marked graduated by R{number}, but R{number} does not name {rid}"
    return None


def items(root: Path, cfg: dict) -> dict[str, dict]:
    """The index's items in order: {ID: {file, link, status, why}}, `why` None when graduated, else the reason."""
    path = Path(root) / cfg["index"]
    if not path.is_file():
        raise KitError(f"{cfg['index']} not found; run: pk research init")
    texts = _ruling_texts(root)
    found = {}
    for _, rid, link, target, status in _rows(read_text(path), cfg):
        if rid in found:
            raise KitError(f"{cfg['index']}: {rid} is listed twice")
        found[rid] = {"file": target, "link": link, "status": status, "why": _why_not(rid, status, texts, cfg)}
    return found


# --- the guard ------------------------------------------------------------------------

def check_line(line: str, found: dict[str, dict], cfg: dict) -> list[str]:
    """The reasons one line is refused."""
    item_id = re.compile(EDGE + "(" + cfg["item_pattern"] + r")(?![\w-])")
    others = [re.compile(EDGE + "(" + pattern + r")(?![\w-])") for pattern in cfg["other_ids"]]
    path_re = re.compile(r"(?<![\w/.-])(?:\.{1,2}/)*" + re.escape(cfg["folder"]) + r"/[\w./#-]*")
    by_file = {item["file"]: rid for rid, item in found.items()}
    ids = list(dict.fromkeys(m.group(1) for m in item_id.finditer(line)))
    paths = []
    for m in path_re.finditer(line):
        raw = m.group(0).rstrip(".,;:)")
        rel = posixpath.normpath(re.sub(r"^(?:\.{1,2}/)+", "", raw).split("#", 1)[0])
        paths.append((raw, rel, by_file.get(rel)))
    cited = {rid for rid in ids if rid in found} | {rid for _, _, rid in paths if rid}
    graduated = any(found[rid]["why"] is None for rid in cited)
    context = bool(cited or paths or WORD.search(line))
    reasons = []
    for rid in ids:
        if rid in found:
            if found[rid]["why"]:
                reasons.append(found[rid]["why"])
        elif context:
            reasons.append(f"{rid} is not listed in {cfg['index']}")
    for raw, rel, rid in paths:
        if rid is None:
            if not graduated:
                reasons.append(f"{raw} cites {cfg['folder']}/ with no graduated research item on the line")
        elif found[rid]["why"]:
            reasons.append(f"{rel} ({rid}): {found[rid]['why']}")
    if context and not graduated:
        for pattern in others:
            for m in pattern.finditer(line):
                reasons.append(f"{m.group(1)} is a research ID with no graduated research item on the line")
    return reasons


def _inside(rel: str, folder: str) -> bool:
    return rel == folder or rel.startswith(folder + "/")


def guard_files(root: Path, cfg: dict) -> list[str]:
    """The files the guard globs match, repository-relative and sorted, the research folder left out."""
    root = Path(root)
    found = set()
    for pattern in cfg["guard"]:
        for path in root.glob(pattern):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                if not _inside(rel, cfg["folder"]):
                    found.add(rel)
    return sorted(found)


def check(root: Path, cfg: dict, files: list[str] | None = None) -> tuple[list[str], list[str], int]:
    """(problem lines `<file>:<line>: <why>`, note lines, files scanned) for the given files or the guard's."""
    root = Path(root)
    found = items(root, cfg)
    problems, notes, scanned = [], [], 0
    for rel in (files if files is not None else guard_files(root, cfg)):
        if _inside(rel, cfg["folder"]):
            notes.append(f"{rel}: skipped (the research folder itself)")
            continue
        scanned += 1
        for number, line in enumerate(read_text(root / rel).splitlines(), 1):
            problems += [f"{rel}:{number}: {why}" for why in check_line(line, found, cfg)]
    return problems, notes, scanned


def drift_check(tree: Path) -> tuple[bool, str] | None:
    """D8 of `pk drift` on a checked-out tree: None without the section, else (ok, detail)."""
    try:
        cfg = config(tree)
        if cfg is None:
            return None
        problems, _, scanned = check(tree, cfg)
    except KitError as exc:
        return False, " ".join(str(exc).split())
    if not problems:
        return True, f"research guard: {scanned} file(s) cite no ungraduated research"
    more = f" (and {len(problems) - SHOWN} more)" if len(problems) > SHOWN else ""
    return False, f"research guard: {len(problems)} refused: {'; '.join(problems[:SHOWN])}{more} (pk research check)"


# --- graduate and init ----------------------------------------------------------------

def graduate(root: Path, cfg: dict, rid: str, ruling: str, archive: bool = False) -> dict:
    """Mark item `rid` graduated by `ruling` (`R<n>`, which must name it); with `archive`, move its file there.

    Every check comes before the first write: a refusal changes nothing."""
    root = Path(root)
    if not re.fullmatch(r"R[1-9]\d*", ruling or ""):
        raise KitError(f"--ruling {ruling!r} must be a ruling number such as R12")
    if archive and not cfg["archive"]:
        raise KitError("--archive needs research.archive in program/checks.yaml (the folder graduated items move to)")
    found = items(root, cfg)
    if rid not in found:
        raise KitError(f"{rid} is not listed in {cfg['index']}")
    texts = _ruling_texts(root)
    number = int(ruling[1:])
    if number not in texts:
        raise KitError(f"{ruling} is not in {RULINGS}; record the user's decision first (pk rulings add)")
    if not _names(texts[number], rid):
        raise KitError(f"{ruling} does not name {rid}: a graduation ruling quotes the item's ID")
    status = f"graduated by {ruling} ({date.today().isoformat()})"
    index_path = root / cfg["index"]
    text = read_text(index_path, newline="")
    lines = text.splitlines(keepends=True)
    row = next(n for n, row_id, *_ in _rows(text, cfg) if row_id == rid)
    item = found[rid]
    link, moved = item["link"], None
    if archive:
        target = posixpath.join(cfg["archive"], posixpath.basename(item["file"]))
        if item["file"] == target:
            raise KitError(f"{rid} is already in {cfg['archive']}")
        if not (root / item["file"]).is_file():
            raise KitError(f"{item['file']} ({rid}) does not exist; nothing to archive")
        if (root / target).exists():
            raise KitError(f"{target} already exists; move it aside first")
        moved = (item["file"], target)
        link = posixpath.relpath(target, posixpath.dirname(cfg["index"]) or ".")
    ending = "\r\n" if lines[row].endswith("\r\n") else "\n" if lines[row].endswith("\n") else ""
    title = _cells(lines[row])[1].replace(f"]({item['link']}", f"]({link}", 1)
    lines[row] = f"| {rid} | {title} | {status} |{ending}"
    if moved:
        (root / moved[1]).parent.mkdir(parents=True, exist_ok=True)
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", moved[0]],
                                 capture_output=True).returncode == 0
        if tracked:
            git(root, "mv", "--", moved[0], moved[1])
        else:
            (root / moved[0]).rename(root / moved[1])
    write_text(index_path, "".join(lines))
    return {"id": rid, "status": status, "moved": moved}


def init(root: Path, cfg: dict) -> Path:
    """Create the folder and the index from the kit's template; refuse when the index exists."""
    path = Path(root) / cfg["index"]
    if path.exists():
        raise KitError(f"{cfg['index']} already exists; refusing to overwrite")
    (Path(root) / cfg["folder"]).mkdir(parents=True, exist_ok=True)
    write_text(path, read_text(TEMPLATE))
    return path


# --- command --------------------------------------------------------------------------

def _given(root: Path, names: list[str]) -> list[str]:
    files = []
    for given in names:
        path = Path(given) if Path(given).is_absolute() else Path.cwd() / given
        if not path.is_file():
            raise KitError(f"no such file: {given}")
        try:
            files.append(path.resolve().relative_to(Path(root).resolve()).as_posix())
        except ValueError:
            raise KitError(f"{given} is outside the repository at {root}") from None
    return files


def cmd_research(args: argparse.Namespace) -> int:
    action = getattr(args, "research_action", None)
    if action not in ("init", "list", "check", "graduate"):
        raise KitError("research needs an action: init, list, check or graduate")
    root = find_root()
    cfg = _required(root)
    if action == "init":
        print(f"research: wrote {init(root, cfg).relative_to(root).as_posix()}")
    elif action == "list":
        for rid, item in items(root, cfg).items():
            print(f"{rid}\t{'graduated' if item['why'] is None else 'open'}\t{item['file']}\t{item['status']}")
    elif action == "check":
        problems, notes, scanned = check(root, cfg, _given(root, args.files) if args.files else None)
        for line in notes + problems:
            print(line)
        if problems:
            print(f"research check: {len(problems)} refused in {scanned} file(s) ({cfg['index']} says what is "
                  "graduated; a citation waits for the graduation ruling)")
            return 1
        print(f"research check: ok, {scanned} file(s) cite no ungraduated research")
    else:
        done = graduate(root, cfg, args.id, args.ruling, archive=args.archive)
        moved = f"; moved {done['moved'][0]} to {done['moved'][1]} (git mv)" if done["moved"] else ""
        print(f"research: {done['id']} {done['status']}{moved}; commit {cfg['index']} with the ruling")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="research_action", metavar="action", parser_class=type(parser))
    actions.add_parser("init", help="create the research folder and its index from the kit's template")
    actions.add_parser("list", help="every item of the index: ID, graduated or open, file, status")
    checker = actions.add_parser("check", help="the citation guard: lines that cite ungraduated research")
    checker.add_argument("files", nargs="*", metavar="FILE", help="files to check (default: research.guard)")
    grad = actions.add_parser("graduate", help="mark an item graduated by the ruling that names it")
    grad.add_argument("id", metavar="ID")
    grad.add_argument("--ruling", required=True, metavar="R<n>")
    grad.add_argument("--archive", action="store_true", help="move the item's file to research.archive (git mv)")


def register(add_command) -> None:
    add_command("research", cmd_research, help="the research track (opt-in): init, list, check, graduate",
                configure=_configure)
