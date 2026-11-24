#!/usr/bin/env python3
"""An adopted programme's identity: `program/adoption.yaml` and the frozen history under `program/legacy/`.

`pk adopt` (adopt.py) writes `program/adoption.yaml` once, when a programme that ran before the kit moves onto it.
Without the file every function here answers as for a programme the kit started: no extra phase prefixes, rulings
from R1, no legacy log. With it:

- a register phase ID is `P<n>` or one of `legacy_phase_prefixes`, and the milestone, step and task IDs below it
  follow (register.valid_id decides, at every site that checks a task ID);
- the kit issues rulings from `ruling_start` upward in `program/rulings.md`; the rulings below it are read from the
  frozen byte copy of the old log, `legacy_rulings` (`pk rulings show R<n>`), and a new ruling may supersede one;
- `pk doctor` checks that the copied source and the legacy log still match the digests recorded at adoption.

Every key is required and no other key is allowed. The file and everything under `program/legacy/` are always
guarded: no task branch may change them (gate.ALWAYS_GUARDED).
"""
from __future__ import annotations

import re
from pathlib import Path

from common import KitError, read_yaml, sha256_file

ADOPTION_FILE = "program/adoption.yaml"
LEGACY_DIR = "program/legacy/"
SOURCE_COPY = "program/legacy/adoption-source.json"
LEGACY_RULINGS = "program/legacy/rulings.md"
LEGACY_HANDOFF = "program/legacy/HANDOFF.md"
KEYS = ("adopted_at", "adopted_commit", "source", "source_sha256", "legacy_phase_prefixes", "ruling_start",
        "legacy_rulings", "legacy_rulings_sha256", "legacy_handoff", "legacy_evidence_roots")
PREFIX = re.compile(r"[A-Z]{1,4}\Z", re.ASCII)  # a legacy phase prefix; `P` alone is the kit's own
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z", re.ASCII)
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_HEADER = re.compile(r"## R(\d+)\b", re.ASCII)  # the old log's headers: `## R<n> (<date or undated>)`


def prefix_problem(prefix) -> str | None:
    """Why `prefix` cannot be a legacy phase prefix, or None."""
    if not isinstance(prefix, str) or not PREFIX.match(prefix):
        return f"legacy phase prefix {prefix!r} must be one to four capital letters"
    if prefix == "P":
        return "legacy phase prefix 'P' is the kit's own (P<n>); name the legacy phases' own prefix"
    return None


def relative_path_problem(path, what: str) -> str | None:
    """Why `path` is not a plain repository-relative path (posix, no `..`, not absolute), or None."""
    if not isinstance(path, str) or not path.strip():
        return f"{what} must be a repository-relative path"
    parts = path.split("/")
    if path.startswith("/") or "\\" in path or ".." in parts or "." in parts or "" in parts[:-1]:
        return f"{what} {path!r} must be a repository-relative path (no leading /, no . or .. parts)"
    return None


def _check(data) -> dict:
    if not isinstance(data, dict):
        raise KitError(f"{ADOPTION_FILE}: expected a mapping with the keys {', '.join(KEYS)}")
    missing = [key for key in KEYS if key not in data]
    unknown = sorted(str(key) for key in data if key not in KEYS)
    problems = []
    if missing:
        problems.append(f"missing key(s) {', '.join(missing)}")
    if unknown:
        problems.append(f"unknown key(s) {', '.join(unknown)}")
    if not missing:
        if not (isinstance(data["adopted_at"], str) and _STAMP.match(data["adopted_at"])):
            problems.append("adopted_at must be a UTC time, YYYY-MM-DDTHH:MM:SSZ")
        if not (isinstance(data["adopted_commit"], str) and _SHA40.match(data["adopted_commit"])):
            problems.append("adopted_commit must be a 40-character commit sha")
        for key in ("source_sha256", "legacy_rulings_sha256"):
            if not (isinstance(data[key], str) and _SHA64.match(data[key])):
                problems.append(f"{key} must be 64 lower-case hex characters")
        for key in ("source", "legacy_rulings", "legacy_handoff"):
            problem = relative_path_problem(data[key], key)
            if problem or not data[key].startswith(LEGACY_DIR):
                problems.append(problem or f"{key} must be a file under {LEGACY_DIR}")
        prefixes = data["legacy_phase_prefixes"]
        if not isinstance(prefixes, list):
            problems.append("legacy_phase_prefixes must be a list")
        else:
            problems += [problem for problem in map(prefix_problem, prefixes) if problem]
            if len(set(map(str, prefixes))) != len(prefixes):
                problems.append("legacy_phase_prefixes lists a prefix twice")
        start = data["ruling_start"]
        if isinstance(start, bool) or not isinstance(start, int) or start < 1:
            problems.append("ruling_start must be a whole number, 1 or more")
        roots = data["legacy_evidence_roots"]
        if not isinstance(roots, list):
            problems.append("legacy_evidence_roots must be a list of paths")
        else:
            problems += [problem for problem in (relative_path_problem(entry, "legacy evidence root")
                                                 for entry in roots) if problem]
    if problems:
        raise KitError(f"{ADOPTION_FILE}: " + "; ".join(problems))
    return data


def load(root: Path) -> dict | None:
    """The adoption record, checked; None for a programme that has not adopted (no file)."""
    path = Path(root) / ADOPTION_FILE
    if not path.exists():
        return None
    return _check(read_yaml(path))


def adopted(root: Path) -> bool:
    return (Path(root) / ADOPTION_FILE).exists()


def phase_prefixes(root: Path) -> tuple[str, ...]:
    """The legacy phase prefixes a register phase ID may be, besides P<n>; empty without adoption."""
    record = load(root)
    return tuple(record["legacy_phase_prefixes"]) if record else ()


def ruling_start(root: Path) -> int:
    """The first ruling number the kit issues: 1, or `ruling_start` of an adopted programme."""
    record = load(root)
    return record["ruling_start"] if record else 1


# --- the legacy rulings log ------------------------------------------------------------------

def legacy_blocks(data: bytes) -> dict[int, str]:
    """{number: block text} of an old rulings log: each block runs from its `## R<n>` header to the next `## ` line.

    The old log is read as it is: any text after the number on the header line, no fixed fields. A number that
    appears twice keeps its first block.
    """
    lines = data.decode("utf-8", errors="replace").splitlines()
    found: dict[int, str] = {}
    for index, line in enumerate(lines):
        match = _LEGACY_HEADER.match(line)
        if not match:
            continue
        end = next((n for n in range(index + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
        found.setdefault(int(match.group(1)), "\n".join(lines[index:end]).rstrip() + "\n")
    return found


def legacy_ruling_numbers(root: Path) -> set[int]:
    """The ruling numbers the frozen legacy log holds; empty without adoption."""
    record = load(root)
    if record is None:
        return set()
    path = Path(root) / record["legacy_rulings"]
    try:
        return set(legacy_blocks(path.read_bytes()))
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc.strerror or exc}") from exc


def legacy_ruling(root: Path, number: int) -> str | None:
    """The block of legacy ruling R<number>, or None when the programme has not adopted or the log lacks it."""
    record = load(root)
    if record is None or number >= record["ruling_start"]:
        return None
    path = Path(root) / record["legacy_rulings"]
    try:
        return legacy_blocks(path.read_bytes()).get(number)
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc.strerror or exc}") from exc


# --- doctor --------------------------------------------------------------------------------

def problems(root: Path) -> list[str]:
    """What `pk doctor` reports about the adoption: a record that does not read, a copied file that is missing or
    whose digest no longer matches. Empty without adoption."""
    try:
        record = load(root)
    except KitError as exc:
        return [str(exc)]
    if record is None:
        return []
    found = []
    for key, digest_key in (("source", "source_sha256"), ("legacy_rulings", "legacy_rulings_sha256")):
        path = Path(root) / record[key]
        if not path.is_file():
            found.append(f"{ADOPTION_FILE}: {key} {record[key]} is missing; it is frozen at adoption: restore it "
                         "from git")
        elif sha256_file(path) != record[digest_key]:
            found.append(f"{ADOPTION_FILE}: {record[key]} no longer matches {digest_key}; it is frozen at adoption: "
                         f"restore it from git (git log -- {record[key]} shows the change)")
    if not (Path(root) / record["legacy_handoff"]).is_file():
        found.append(f"{ADOPTION_FILE}: legacy_handoff {record['legacy_handoff']} is missing; restore it from git")
    return found
