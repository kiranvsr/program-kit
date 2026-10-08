"""The evidence store: append-only, immutable evidence records kept outside the repository (standard library only).

Evidence is a development fingerprint, not part of the product, so it does not live in git. A store is a
directory, by default the sibling `<repo>/../<repo-name>-evidence/` of the main checkout (every worktree of the
repository shares it); `$PROGRAM_KIT_EVIDENCE_STORE` overrides that, and so does `evidence_store:` in
`program/program.yaml` (a relative path is resolved from the main checkout's root). The environment variable
wins over the file. Inside the store:

    STORE.json                                   schema version, repository name, created_at
    records/<task_id>/<kind>/<utc-compact>-<sha256[:12]>.json

`kind` is `results` or `acceptance`; `<utc-compact>` is the UTC append time `YYYYMMDDTHHMMSSffffffZ`, so names
sort in append order; the digest in the name is the sha256 of the file's bytes. The kit creates a record once,
read-only, through a temporary file that is hard-linked to its final name (an existing name is never replaced),
and never modifies or deletes one. A record whose bytes no longer hash to its name is refused everywhere as
"immutable record altered".

The repository keeps one pointer index, `program/evidence/index.json`: for each task the latest `results` and
the latest `acceptance` record (digest, path relative to the store, and a few fields of the record). It is the
only evidence file the kit writes into the repository, each append updates the entry of its own task only, and
it is replaced atomically. Every check reads the latest record through the index; older records stay in the
store as history (`pk evidence history`).

Known limits: the store is local to the host (sharing or backing it up is the user's business), and records are
not signed; a record rewritten consistently together with its name and the index is not detected.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from common import KitError, git, new_file, read_json, read_yaml, sha256_bytes, utc_now, write_json

ENV = "PROGRAM_KIT_EVIDENCE_STORE"
SCHEMA_VERSION = 1
KINDS = ("results", "acceptance")
INDEX = "program/evidence/index.json"
STORE_FILE = "STORE.json"
CONFIG = "program/program.yaml"
_COMPACT = "%Y%m%dT%H%M%S%fZ"
_NAME = re.compile(r"(\d{8}T\d{12}Z)-([0-9a-f]{12})\.json\Z")
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RESULTS_FIELDS = ("digest", "path", "head", "produced_at")
ACCEPTANCE_FIELDS = ("digest", "path", "decision", "accepted_at", "results_digest")


# --- where the store is ------------------------------------------------------------

def main_checkout(root) -> Path:
    """The main checkout of the repository `root` belongs to (the same for every worktree); `root` outside git."""
    root = Path(root).resolve()
    try:
        common = Path(root, git(root, "rev-parse", "--git-common-dir")).resolve()
    except KitError:
        return root
    return common.parent if common.name == ".git" else root


def _configured(root: Path) -> str | None:
    path = root / CONFIG
    if not path.is_file():
        return None
    data = read_yaml(path)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise KitError(f"{CONFIG}: expected a mapping")
    value = data.get("evidence_store")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise KitError(f"{CONFIG}: evidence_store must be a non-empty path")
    return value


def store_dir(root) -> Path:
    """The evidence store of the repository at `root`: the environment variable, then program.yaml, then the
    sibling `<main checkout>-evidence` directory. Relative paths are resolved from the main checkout's root."""
    root = Path(root).resolve()
    main = main_checkout(root)
    chosen = os.environ.get(ENV) or _configured(root)
    if chosen:
        path = Path(chosen).expanduser()
        return (path if path.is_absolute() else main / path).resolve()
    return (main.parent / f"{main.name}-evidence").resolve()


def init(root) -> tuple[Path, bool]:
    """Create the store and its STORE.json; (store, whether it was created now). An existing store is checked."""
    store = store_dir(root)
    marker = store / STORE_FILE
    if marker.is_file():
        _check_marker(store)
        return store, False
    try:
        (store / "records").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise KitError(f"cannot create evidence store {store}: {exc.strerror or exc}") from exc
    write_json(marker, {"schema_version": SCHEMA_VERSION, "repo": main_checkout(root).name,
                        "created_at": utc_now()})
    return store, True


def _check_marker(store: Path) -> dict:
    data = read_json(store / STORE_FILE)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise KitError(f"{store / STORE_FILE}: not an evidence store of schema version {SCHEMA_VERSION}")
    return data


def require(root) -> Path:
    """The store, which must have been initialised (`pk evidence init`)."""
    store = store_dir(root)
    if not (store / STORE_FILE).is_file():
        raise KitError(f"evidence store not initialised at {store}: run pk evidence init")
    _check_marker(store)
    return store


# --- records ------------------------------------------------------------------------

def check_task(task_id) -> str:
    if not isinstance(task_id, str) or not _TASK.match(task_id):
        raise KitError(f"not a usable task id: {task_id!r}")
    return task_id


def _kind(kind) -> str:
    if kind not in KINDS:
        raise KitError(f"unknown record kind {kind!r}; kinds are {', '.join(KINDS)}")
    return kind


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
    """Add one record (a dict, or its exact bytes) to the store; never replaces anything.

    Returns {digest, path (relative to the store), file (absolute)}. The file is created read-only through a
    temporary file in the same directory that is hard-linked to its final name, so a half-written record never
    carries a record's name and an existing record can never be overwritten.
    """
    check_task(task_id)
    _kind(kind)
    store = require(root)
    raw = data if isinstance(data, bytes) else encode(data)
    digest = sha256_bytes(raw)
    directory = store / "records" / task_id / kind
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
        raise KitError(f"refusing to replace the existing record {directory / name}") from exc
    except OSError as exc:
        raise KitError(f"cannot append to the evidence store {store}: {exc.strerror or exc}") from exc
    return {"digest": digest, "path": f"records/{task_id}/{kind}/{name}", "file": directory / name}


def check_rel(rel) -> tuple[str, str, str]:
    """(task_id, kind, name) of a store-relative record path; KitError for anything else."""
    parts = PurePosixPath(rel).parts if isinstance(rel, str) and "\\" not in rel else ()
    if len(parts) != 4 or parts[0] != "records" or not _TASK.match(parts[1]) or parts[2] not in KINDS \
            or not _NAME.match(parts[3]):
        raise KitError(f"not a record path of the evidence store: {rel!r}")
    return parts[1], parts[2], parts[3]


def altered(path: Path, raw: bytes) -> str | None:
    """Why a store file's bytes no longer match its name, or None."""
    match = _NAME.match(Path(path).name)
    actual = sha256_bytes(raw)
    if match is None or match.group(2) != actual[:12]:
        return f"immutable record altered: {Path(path).name} (its bytes hash to {actual})"
    return None


def load(store: Path, rel: str, digest: str | None = None) -> dict:
    """The record at `rel` in `store`, after checking its bytes still hash to its name (and to `digest`)."""
    check_rel(rel)
    path = Path(store) / rel
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise KitError(f"evidence record {rel} is missing from the store {store}: {exc.strerror or exc}") from exc
    problem = altered(path, raw)
    if problem is None and digest is not None and sha256_bytes(raw) != digest:
        problem = f"immutable record altered: {rel} hashes to {sha256_bytes(raw)}, not the indexed {digest}"
    if problem:
        raise KitError(problem)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise KitError(f"evidence record {rel} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise KitError(f"evidence record {rel} is not a JSON object")
    return data


# --- the index ------------------------------------------------------------------------

def empty_index() -> dict:
    return {"schema_version": SCHEMA_VERSION, "tasks": {}}


def _entry_problem(kind: str, entry) -> str | None:
    fields = RESULTS_FIELDS if kind == "results" else ACCEPTANCE_FIELDS
    if not isinstance(entry, dict) or set(entry) != set(fields):
        return f"must hold exactly {', '.join(fields)}"
    if not isinstance(entry["digest"], str) or not _HEX64.match(entry["digest"]):
        return "digest must be a 64-character lowercase hex sha256"
    try:
        check_rel(entry["path"])
    except KitError as exc:
        return str(exc)
    if not entry["path"].endswith(f"-{entry['digest'][:12]}.json"):
        return "path does not name the record of its digest"
    if any(not isinstance(entry[key], str) for key in fields):
        return "every field must be a string"
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
    return entry, load(require(root), entry["path"], entry["digest"])


# --- history ------------------------------------------------------------------------

def history(root, task_id: str) -> list[dict]:
    """Every record of `task_id` in the store, newest first: {kind, digest, path, head, at, decision, current,
    altered}. `current` is true for the record the index points at; `altered` names a record whose bytes no
    longer match its name."""
    check_task(task_id)
    store = require(root)
    index = read_index(root)
    rows = []
    for kind in KINDS:
        directory = store / "records" / task_id / kind
        if not directory.is_dir():
            continue
        current = latest(index, task_id, kind)
        for name in os.listdir(directory):
            if not _NAME.match(name):
                continue
            path = directory / name
            raw = path.read_bytes()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                data = {}
            data = data if isinstance(data, dict) else {}
            digest = sha256_bytes(raw)
            rel = f"records/{task_id}/{kind}/{name}"
            rows.append({"kind": kind, "digest": digest, "path": rel,
                         "head": str(data.get("head" if kind == "results" else "source_commit", "")),
                         "at": str(data.get("produced_at" if kind == "results" else "accepted_at", "")),
                         "decision": str(data.get("decision", "")) if kind == "acceptance" else "",
                         "current": current is not None and current["path"] == rel and current["digest"] == digest,
                         "altered": altered(path, raw)})
    rows.sort(key=lambda row: PurePosixPath(row["path"]).name, reverse=True)
    return rows


def problems(root) -> list[str]:
    """What `pk doctor` reports about the store: one line each, empty when it is usable.

    A project with no index entries needs no store yet. Otherwise the store must exist and be writable, and
    every index entry must resolve to a record whose bytes match its digest.
    """
    try:
        index = read_index(root)
    except KitError as exc:
        return [str(exc)]
    store = store_dir(root)
    entries = [(task, kind, entry) for task, kinds in index["tasks"].items() for kind, entry in kinds.items()]
    if not (store / STORE_FILE).is_file():
        return [f"evidence store missing at {store}: {len(entries)} index entries point into it "
                "(set PROGRAM_KIT_EVIDENCE_STORE or evidence_store in program/program.yaml, or run pk evidence "
                "init and pk evidence import)"] if entries else []
    found = []
    try:
        _check_marker(store)
    except KitError as exc:
        found.append(str(exc))
    if not os.access(store, os.W_OK) or not os.access(store / "records", os.W_OK):
        found.append(f"evidence store {store} is not writable")
    for task, kind, entry in entries:
        try:
            load(store, entry["path"], entry["digest"])
        except KitError as exc:
            found.append(f"{INDEX}: {task} {kind}: {exc}")
    return found
