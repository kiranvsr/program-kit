"""The evidence store: committed, append-only, immutable evidence records (standard library only).

Records live in the repository, next to the task's `produce.py`, and are committed like any other file:

    program/evidence/<task_id>/results/<utc-compact>-<sha256[:12]>.json
    program/evidence/<task_id>/acceptance/<utc-compact>-<sha256[:12]>.json

`<utc-compact>` is the UTC append time `YYYYMMDDTHHMMSSffffffZ`, so names sort in append order; the digest in the
name is the sha256 of the file's bytes. The kit creates a record once, read-only, through a temporary file that is
hard-linked to its final name (an existing name is never replaced), and never modifies or deletes one.
Immutability is enforced through git, not a file system boundary (`integrity`): a record whose bytes no longer hash
to its name is refused as "immutable record altered", and a commit that modifies or deletes a record file is refused
as "evidence history rewritten".

`program/evidence/index.json` is the only mutable evidence file: for each task it points at the latest `results`
and the latest `acceptance` record (digest, repository-relative path, and a few fields of the record). Each append
updates the entry of its own task only, and the index is replaced atomically. Every check reads the latest record
through the index; older records stay as history (`pk evidence history`).

Known limits: records are not signed; a record rewritten consistently together with its name and the index, in a
history that is itself rewritten, is not detected.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from common import KitError, git, new_file, read_json, sha256_bytes, write_json

SCHEMA_VERSION = 1
KINDS = ("results", "acceptance")
BASE = "program/evidence"
INDEX = f"{BASE}/index.json"
RECORD_PATHSPECS = tuple(f":(glob){BASE}/*/{kind}/**" for kind in KINDS)
_COMPACT = "%Y%m%dT%H%M%S%fZ"
_NAME = re.compile(r"(\d{8}T\d{12}Z)-([0-9a-f]{12})\.json\Z")
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RESULTS_FIELDS = ("digest", "path", "head", "produced_at")
ACCEPTANCE_FIELDS = ("digest", "path", "decision", "accepted_at", "results_digest")


def check_task(task_id) -> str:
    if not isinstance(task_id, str) or not _TASK.match(task_id):
        raise KitError(f"not a usable task id: {task_id!r}")
    return task_id


def _kind(kind) -> str:
    if kind not in KINDS:
        raise KitError(f"unknown record kind {kind!r}; kinds are {', '.join(KINDS)}")
    return kind


def init(root) -> tuple[Path, bool]:
    """Create `program/evidence/index.json` when it is absent; (its path, whether it was created now)."""
    path = Path(root) / INDEX
    if path.exists():
        read_index(root)
        return path, False
    write_index(root, empty_index())
    return path, True


# --- records ------------------------------------------------------------------------

def encode(data) -> bytes:
    """The bytes a record dict is stored as."""
    try:
        return (json.dumps(data, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KitError(f"cannot serialise the record as JSON: {exc}") from exc


def _next_stamp(directory: Path) -> str:
    """Now as `YYYYMMDDTHHMMSSffffffZ`, later than every record already in `directory` (names sort in append order)."""
    now = datetime.now(timezone.utc)
    names = [m.group(1) for m in map(_NAME.match, os.listdir(directory)) if m] if directory.is_dir() else []
    if names:
        last = datetime.strptime(max(names), _COMPACT).replace(tzinfo=timezone.utc)
        if now <= last:
            now = last + timedelta(microseconds=1)
    return now.strftime(_COMPACT)


def append(root, task_id: str, kind: str, data) -> dict:
    """Add one record file (a dict, or its exact bytes); never replaces anything.

    Returns {digest, path (relative to the repository root), file (absolute)}. The file is created read-only
    through a temporary file in the same directory that is hard-linked to its final name, so a half-written record
    never carries a record's name and an existing record can never be overwritten.
    """
    check_task(task_id)
    _kind(kind)
    raw = data if isinstance(data, bytes) else encode(data)
    digest = sha256_bytes(raw)
    directory = Path(root) / BASE / task_id / kind
    name = "?"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{_next_stamp(directory)}-{digest[:12]}.json"
        fd, tmp = new_file(directory, f".{name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o444)
            os.link(tmp, directory / name)
        finally:
            os.unlink(tmp)
    except FileExistsError as exc:
        raise KitError(f"refusing to replace the existing record {BASE}/{task_id}/{kind}/{name}") from exc
    except OSError as exc:
        raise KitError(f"cannot append to {directory}: {exc.strerror or exc}") from exc
    return {"digest": digest, "path": f"{BASE}/{task_id}/{kind}/{name}", "file": directory / name}


def check_rel(rel) -> tuple[str, str, str]:
    """(task_id, kind, name) of a repository-relative record path; KitError for anything else."""
    parts = PurePosixPath(rel).parts if isinstance(rel, str) and "\\" not in rel else ()
    if len(parts) != 5 or "/".join(parts[:2]) != BASE or not _TASK.match(parts[2]) or parts[3] not in KINDS \
            or not _NAME.match(parts[4]):
        raise KitError(f"not an evidence record path ({BASE}/<ID>/<kind>/<utc>-<digest>.json): {rel!r}")
    return parts[2], parts[3], parts[4]


def altered(rel: str, raw: bytes) -> str | None:
    """Why a record file's bytes no longer match its name, or None."""
    match = _NAME.match(PurePosixPath(rel).name)
    actual = sha256_bytes(raw)
    if match is None or match.group(2) != actual[:12]:
        return f"immutable record altered: {rel} (its bytes hash to {actual})"
    return None


def parse(rel: str, raw: bytes, digest: str | None = None) -> dict:
    """The record `raw` read from `rel`, after checking its bytes still hash to its name (and to `digest`)."""
    check_rel(rel)
    problem = altered(rel, raw)
    if problem is None and digest is not None and sha256_bytes(raw) != digest:
        problem = f"immutable record altered: {rel} hashes to {sha256_bytes(raw)}, not the indexed {digest}"
    if problem:
        raise KitError(problem)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise KitError(f"evidence record {rel} is not valid JSON: {' '.join(str(exc).split())}") from exc
    if not isinstance(data, dict):
        raise KitError(f"evidence record {rel} is not a JSON object")
    return data


def load(root, rel: str, digest: str | None = None) -> dict:
    """The record at `rel` in the checkout at `root`, checked against its name (and `digest`)."""
    check_rel(rel)
    try:
        raw = (Path(root) / rel).read_bytes()
    except OSError as exc:
        raise KitError(f"evidence record {rel} is missing: {exc.strerror or exc}") from exc
    return parse(rel, raw, digest)


# --- the index ------------------------------------------------------------------------

def empty_index() -> dict:
    return {"schema_version": SCHEMA_VERSION, "tasks": {}}


def _entry_problem(kind: str, entry) -> str | None:
    fields = RESULTS_FIELDS if kind == "results" else ACCEPTANCE_FIELDS
    if not isinstance(entry, dict) or set(entry) != set(fields):
        return f"must hold exactly {', '.join(fields)}"
    if any(not isinstance(entry[key], str) for key in fields):
        return "every field must be a string"
    if not _HEX64.match(entry["digest"]):
        return "digest must be a 64-character lowercase hex sha256"
    try:
        check_rel(entry["path"])
    except KitError as exc:
        return str(exc)
    if not entry["path"].endswith(f"-{entry['digest'][:12]}.json"):
        return "path does not name the record of its digest"
    return None


def parse_index(data, where: str = INDEX) -> dict:
    """A checked index; KitError naming the first problem."""
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(data.get("tasks"), dict) or set(data) != {"schema_version", "tasks"}:
        raise KitError(f"{where}: not an evidence index (schema_version {SCHEMA_VERSION} and tasks)")
    for task_id, kinds in data["tasks"].items():
        if not _TASK.match(task_id) or not isinstance(kinds, dict) or not kinds or not set(kinds) <= set(KINDS):
            raise KitError(f"{where}: task {task_id!r} must map results and/or acceptance to an entry")
        for kind, entry in kinds.items():
            problem = _entry_problem(kind, entry)
            if problem:
                raise KitError(f"{where}: {task_id} {kind}: {problem}")
            if check_rel(entry["path"])[:2] != (task_id, kind):
                raise KitError(f"{where}: {task_id} {kind}: path names another task or kind")
    return data


def read_index(root) -> dict:
    """`program/evidence/index.json` of the checkout at `root`; an empty index when there is none."""
    path = Path(root) / INDEX
    if not path.exists():
        return empty_index()
    return parse_index(read_json(path))


def index_from_text(text: str, where: str) -> dict:
    try:
        return parse_index(json.loads(text), where)
    except (ValueError, RecursionError) as exc:
        raise KitError(f"{where}: invalid JSON: {exc}") from exc


def _sorted(index: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION,
            "tasks": {task: {kind: dict(sorted(entry.items())) for kind, entry in sorted(kinds.items())}
                      for task, kinds in sorted(index["tasks"].items())}}


def write_index(root, index: dict) -> None:
    """Replace the index atomically (a temporary file renamed over it), in a stable sorted form."""
    write_json(Path(root) / INDEX, _sorted(parse_index(index)))


def set_entry(root, task_id: str, kind: str, entry: dict) -> None:
    """Point the index of `task_id` at `entry` for `kind`; every other task's entry is left as it is."""
    index = read_index(root)
    index["tasks"].setdefault(check_task(task_id), {})[_kind(kind)] = entry
    write_index(root, index)


def latest(index: dict, task_id: str, kind: str) -> dict | None:
    return index["tasks"].get(task_id, {}).get(kind)


def results_entry(appended: dict, data: dict) -> dict:
    return {"digest": appended["digest"], "path": appended["path"], "head": str(data.get("head", "")),
            "produced_at": str(data.get("produced_at", ""))}


def acceptance_entry(appended: dict, data: dict) -> dict:
    return {"digest": appended["digest"], "path": appended["path"], "decision": str(data.get("decision", "")),
            "accepted_at": str(data.get("accepted_at", "")), "results_digest": str(data.get("evidence_sha256", ""))}


def record(root, task_id: str, kind: str, data) -> dict:
    """Append a record and point the index at it: the one way the kit adds evidence. Returns the append result.

    `data` is the record dict, or its exact bytes (an import keeps the bytes, so digests bound to them still hold).
    """
    try:
        parsed = json.loads(data.decode("utf-8")) if isinstance(data, bytes) else data
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise KitError(f"an evidence record must be JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise KitError("an evidence record must be a JSON object")
    appended = append(root, task_id, kind, data)
    entry = results_entry(appended, parsed) if kind == "results" else acceptance_entry(appended, parsed)
    set_entry(root, task_id, kind, entry)
    return appended


def load_latest(root, task_id: str, kind: str, index: dict | None = None) -> tuple[dict, dict] | None:
    """(index entry, record) of the latest `kind` record of `task_id`, or None when the index has none."""
    index = read_index(root) if index is None else index
    entry = latest(index, task_id, kind)
    if entry is None:
        return None
    return entry, load(root, entry["path"], entry["digest"])


# --- history ------------------------------------------------------------------------

def _record_files(root) -> list[str]:
    """Repository-relative paths of every file in a `results/` or `acceptance/` directory under program/evidence."""
    base = Path(root) / BASE
    found = []
    for kind in KINDS:
        for path in base.glob(f"*/{kind}/*"):
            if path.is_file() and not path.name.startswith("."):
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def history(root, task_id: str) -> list[dict]:
    """Every record of `task_id`, newest first: {kind, digest, path, head, at, decision, current, altered}.

    `current` is true for the record the index points at; `altered` names a record whose bytes no longer match
    its name."""
    check_task(task_id)
    index = read_index(root)
    rows = []
    for kind in KINDS:
        directory = Path(root) / BASE / task_id / kind
        if not directory.is_dir():
            continue
        current = latest(index, task_id, kind)
        for name in os.listdir(directory):
            if not _NAME.match(name):
                continue
            rel = f"{BASE}/{task_id}/{kind}/{name}"
            raw = (directory / name).read_bytes()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                data = {}
            data = data if isinstance(data, dict) else {}
            digest = sha256_bytes(raw)
            rows.append({"kind": kind, "digest": digest, "path": rel,
                         "head": str(data.get("head" if kind == "results" else "source_commit", "")),
                         "at": str(data.get("produced_at" if kind == "results" else "accepted_at", "")),
                         "decision": str(data.get("decision", "")) if kind == "acceptance" else "",
                         "current": current is not None and current["path"] == rel and current["digest"] == digest,
                         "altered": altered(rel, raw)})
    rows.sort(key=lambda row: PurePosixPath(row["path"]).name, reverse=True)
    return rows


# --- integrity ------------------------------------------------------------------------

def rewritten(root, base: str | None = None, head: str = "HEAD") -> list[str]:
    """`evidence history rewritten: <path> modified|deleted in <commit>` for every commit in `base..head` (the whole
    history up to `head` when `base` is None) that modified or deleted a record file. Empty without commits."""
    span = f"{base}..{head}" if base else head
    try:
        git(root, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}")
    except KitError:
        return []
    listing = git(root, "log", "--diff-filter=MD", "--no-renames", "--name-status", "--format=commit %H", span,
                  "--", *RECORD_PATHSPECS)
    found, commit = [], "?"
    for line in listing.splitlines():
        if line.startswith("commit "):
            commit = line[len("commit "):]
        elif "\t" in line:
            status, path = line.split("\t", 1)
            how = "modified" if status.startswith("M") else "deleted"
            found.append(f"evidence history rewritten: {path} {how} in {commit}")
    return found


def integrity(root, base: str | None = None, uncommitted: set[str] | frozenset = frozenset()) -> list[str]:
    """Every reason the evidence of the checkout at `root` cannot be trusted; empty when it can.

    (1) every record file's sha256 matches the digest in its name; (2) no commit in `base..HEAD` (the whole history
    when `base` is None) modified or deleted a record file; (3) every index entry resolves to a committed record
    with its indexed digest (paths in `uncommitted` may be new, not yet committed records).
    """
    root = Path(root)
    problems = []
    for rel in _record_files(root):
        problem = altered(rel, (root / rel).read_bytes()) if _NAME.match(PurePosixPath(rel).name) else \
            f"{rel}: not a record name (<utc>-<sha256[:12]>.json)"
        if problem:
            problems.append(problem)
    problems.extend(rewritten(root, base))
    try:
        index = read_index(root)
    except KitError as exc:
        return problems + [str(exc)]
    entries = [(task, kind, entry) for task, kinds in sorted(index["tasks"].items()) for kind, entry in kinds.items()]
    try:
        tracked = set(git(root, "ls-files", "-z", "--", BASE).split("\0")) if entries else set()
    except KitError:
        tracked = set()
    for task, kind, entry in entries:
        try:
            load(root, entry["path"], entry["digest"])
        except KitError as exc:
            problems.append(f"{INDEX}: {task} {kind}: {exc}")
            continue
        if entry["path"] not in tracked and entry["path"] not in uncommitted:
            problems.append(f"{INDEX}: {task} {kind}: {entry['path']} is not committed")
    return problems
