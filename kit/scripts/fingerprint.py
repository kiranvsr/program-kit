"""The release fingerprint: one committed record per commit that says what the project's release set did there.

`program/release-set.yaml` lists the commands of the release (argv lists, grouped in tracks). `pk fingerprint run`
runs them at HEAD, one thread per track, and writes `program/evidence/fingerprint/<head12>/fingerprint.json` with a
`<table>.json` per derived test table beside it. The record keeps each command's exit code, timing, output digest
and last lines, never the output, an environment value or an absolute path. Records are immutable: a commit that
modifies or deletes one is refused by `store.rewritten`.

    pk fingerprint run [--only TRACK] [--scratch DIR]
    pk fingerprint plan
    pk fingerprint validate [PATH]

Standard library only. The readers of a finished record (the current fingerprint, coverage, `show`) are a later
addition: they go in the marked section at the end of this file, after the producer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

import miniyaml
from common import KitError, find_root, read_json, run_argv, sha256_bytes, utc_now
from gate import STEP_TIMEOUT

SCHEMA = "program-kit.fingerprint.v1"
BASE = "program/evidence/fingerprint"
RECORD = "fingerprint.json"
RELEASE_SET = "program/release-set.yaml"
KINDS = ("check", "test", "gate")
DERIVES = ("unittest", "go-tests")
TAIL_LINES = 20
TOOLCHAIN_TIMEOUT = 60  # seconds for one toolchain version command

_ID = re.compile(r"[a-z0-9-]+\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
_ENTRY_KEYS = ("id", "argv", "expected_exit", "track", "kind", "derives", "env")
_TOP_KEYS = ("toolchain", "entries")
# An absolute path of the kinds that name a person or a machine. The validator looks for the prefix anywhere in the
# record's text; the scrubber replaces the prefix and the rest of the path.
_PATH_PREFIX = r"(?:/(?:Users|home|private|tmp)(?![\w.-])|(?<!\w)[A-Za-z]:[\\/])"
ABSOLUTE_PATH = re.compile(_PATH_PREFIX)
_RECORD_DIR = re.compile(re.escape(BASE) + r"/[0-9a-f]{12}(?:/|\Z)")  # an earlier, uncommitted run's record directory
_PATH_RUN = re.compile(_PATH_PREFIX + r"[^\s'\"<>():,;]*")


# --- git ----------------------------------------------------------------------------

def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        lines = [ln.strip() for ln in proc.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        raise KitError(f"git {' '.join(args)} failed: {lines[0] if lines else f'exit {proc.returncode}'}")
    return proc.stdout


def _show(root: Path, ref: str, path: str) -> bytes | None:
    """The blob of `path` at `ref`, or None when the commit has no such file."""
    try:
        return _git_bytes(root, "show", f"{ref}:{path}")
    except KitError:
        return None


def _dirty_paths(root: Path) -> list[str]:
    """`git status --porcelain` paths, except untracked files under the fingerprint directory.

    A record directory of an earlier, uncommitted run (kept while a failure is fixed) must not stop the next run;
    a tracked fingerprint file that is modified or staged still counts.
    """
    out = _git_bytes(root, "status", "--porcelain", "--untracked-files=all", "-z").decode("utf-8", "replace")
    paths, fields = [], iter(out.split("\0"))
    for field in fields:
        if len(field) < 4:
            continue
        status, path = field[:2], field[3:]
        if status[0] in "RC":
            next(fields, None)  # the original path of a rename or copy follows as its own field
        if status == "??" and _RECORD_DIR.match(path):
            continue
        paths.append(path)
    return paths


# --- the release set ------------------------------------------------------------------

def _argv(value, what: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) for part in value):
        raise KitError(f"{what} must be a non-empty list of strings (an argv list, never a shell string)")
    if not value[0]:
        raise KitError(f"{what}: the first element (the program) must not be empty")
    return list(value)


def _entry(raw, index: int, seen: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise KitError(f"release-set.yaml: entries[{index}] must be a mapping, not {raw!r}")
    ident = raw.get("id")
    where = f"entry {ident}" if isinstance(ident, str) and ident else f"entries[{index}]"
    if not isinstance(ident, str) or not _ID.match(ident):
        raise KitError(f"release-set.yaml: {where}: id must be lowercase letters, digits and hyphens, not {ident!r}")
    if ident in seen:
        raise KitError(f"release-set.yaml: {where}: id {ident!r} is used twice")
    seen.add(ident)
    for key in raw:
        if key not in _ENTRY_KEYS:
            raise KitError(f"release-set.yaml: {where}: unknown key {key!r} (keys are {', '.join(_ENTRY_KEYS)})")
    for key in ("argv", "track", "kind"):
        if key not in raw:
            raise KitError(f"release-set.yaml: {where}: missing key {key!r}")
    argv = _argv(raw["argv"], f"release-set.yaml: {where}: argv")
    expected = raw.get("expected_exit", 0)
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise KitError(f"release-set.yaml: {where}: expected_exit must be an integer, not {expected!r}")
    track = raw["track"]
    if not isinstance(track, str) or not _ID.match(track):
        raise KitError(f"release-set.yaml: {where}: track must be lowercase letters, digits and hyphens, "
                       f"not {track!r}")
    kind = raw["kind"]
    if kind not in KINDS:
        raise KitError(f"release-set.yaml: {where}: kind must be one of {', '.join(KINDS)}, not {kind!r}")
    derives = raw.get("derives")
    if derives is not None and derives not in DERIVES:
        raise KitError(f"release-set.yaml: {where}: derives must be null or one of {', '.join(DERIVES)}, "
                       f"not {derives!r}")
    env = raw.get("env", {})
    if env is None or not isinstance(env, dict):
        raise KitError(f"release-set.yaml: {where}: env must be a mapping of names to string values, not {env!r}")
    for name, value in env.items():
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise KitError(f"release-set.yaml: {where}: env name {name!r} is not a usable variable name")
        if not isinstance(value, str):
            raise KitError(f"release-set.yaml: {where}: env {name} must be a string (quote it), not {value!r}")
    return {"id": ident, "argv": argv, "expected_exit": expected, "track": track, "kind": kind, "derives": derives,
            "env": dict(env)}


def parse_release_set(text: str) -> dict:
    """The checked contents of a release-set.yaml: {toolchain: [argv, ...], entries: [entry, ...]}."""
    try:
        data = miniyaml.loads(text)
    except miniyaml.YAMLError as exc:
        raise KitError(f"release-set.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise KitError("release-set.yaml must be a mapping with the keys toolchain and entries")
    for key in data:
        if key not in _TOP_KEYS:
            raise KitError(f"release-set.yaml: unknown key {key!r} (keys are {', '.join(_TOP_KEYS)})")
    toolchain = data.get("toolchain", [])
    if toolchain is None or not isinstance(toolchain, list):
        raise KitError(f"release-set.yaml: toolchain must be a list of argv lists, not {toolchain!r}")
    toolchain = [_argv(item, f"release-set.yaml: toolchain[{n}]") for n, item in enumerate(toolchain)]
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise KitError("release-set.yaml: entries must be a non-empty list")
    seen: set[str] = set()
    return {"toolchain": toolchain, "entries": [_entry(raw, n, seen) for n, raw in enumerate(entries)]}


def load_release_set(root, ref: str) -> dict:
    """`program/release-set.yaml` as committed at `ref`, checked; the working copy is never read."""
    raw = _show(Path(root), ref, RELEASE_SET)
    if raw is None:
        raise KitError(f"{RELEASE_SET} cannot be read on {ref}: the commit has no such file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KitError(f"{RELEASE_SET} on {ref} is not UTF-8") from exc
    try:
        return parse_release_set(text.removeprefix("\ufeff"))
    except KitError as exc:
        raise KitError(f"{exc} (in {RELEASE_SET} on {ref})") from exc


# --- parsers: output -> table ----------------------------------------------------------

# Verdict names. Both tables use these ranks to decide which of two verdicts is worse.
RANK = {"skipped": 0, "skip": 0, "xfail": 1, "ok": 2, "pass": 2, "xpass": 3, "fail": 4, "error": 5}
_UNITTEST_VERDICT = r"ok|FAIL|ERROR|skipped(?: '.*'| \".*\")?|expected failure|unexpected success"
_UT_HEAD = re.compile(r"(?: {0,4})(\w+) \(([\w.]+)\)(?: \(.*?\))*(.*)\Z")
_UT_BARE = re.compile("(?:" + _UNITTEST_VERDICT + r")\Z")
_UT_DOC_END = re.compile(r" \.\.\. (" + _UNITTEST_VERDICT + r")\Z")
_UT_OK = re.compile(r"OK(?: \(.*\))?\Z")
_UT_FAILED = re.compile(r"FAILED \((.*)\)\Z")


def _unittest_verdict(text: str) -> str:
    if text.startswith("skipped"):
        return "skipped"
    return {"ok": "ok", "FAIL": "fail", "ERROR": "error", "expected failure": "xfail",
            "unexpected success": "xpass"}.get(text, "")


def _worse(a: str, b: str) -> str:
    return b if RANK[b] > RANK[a] else a


def parse_unittest(text: str) -> dict[str, str]:
    """{test name: verdict} from `unittest -v` output; the key "" is the runner's own summary.

    A name is `module.Class.test`: the 3.11+ line `test (module.Class.test)` carries it whole, the older
    `test (module.Class)` is completed with the test name. A docstring (the verdict on the next line) and a test
    that printed output before its verdict are followed. A subtest line (indented, `test (m.C.test) (i=1) ... FAIL`)
    counts under its test's name, and a test seen twice keeps its worst verdict.
    """
    table: dict[str, str] = {}

    def put(key: str, text: str) -> None:
        verdict = _unittest_verdict(text)
        if verdict:  # a text that is not a verdict is ignored, never raised
            table[key] = _worse(table[key], verdict) if key in table else verdict

    pending, strict = None, False  # strict: a docstring line is next, so only that one line may complete the test
    for line in text.splitlines():
        head = _UT_HEAD.match(line)
        if head:
            name, path, rest = head.groups()
            key = path if path.rsplit(".", 1)[-1] == name else f"{path}.{name}"
            if rest.startswith(" ... "):
                if _UT_BARE.match(rest[5:]):
                    put(key, rest[5:])
                    pending = None
                else:
                    pending, strict = key, False  # the test printed first; its verdict is a later line
            elif not rest.strip():
                pending, strict = key, True
            continue
        if pending is not None:
            if _UT_BARE.match(line):
                put(pending, line)
                pending = None
                continue
            ended = _UT_DOC_END.search(line)
            if ended and strict:
                put(pending, ended.group(1))
                pending = None
                continue
            if strict:
                pending = None
        if _UT_OK.match(line):
            table[""] = "ok"
        elif (failed := _UT_FAILED.match(line)):
            table[""] = "error" if "errors=" in failed.group(1) else "fail"
    return table


def parse_go_tests(text: str) -> dict[str, dict[str, str]]:
    """{package: {test: verdict}} from `go test -json` events; the test "" is the package's own summary."""
    table: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("Action") not in ("pass", "fail", "skip"):
            continue
        package, test = event.get("Package"), event.get("Test", "")
        if isinstance(package, str) and package and isinstance(test, str):
            rows = table.setdefault(package, {})
            verdict = event["Action"]
            rows[test] = _worse(rows[test], verdict) if test in rows else verdict
    return table


PARSERS = {"unittest": parse_unittest, "go-tests": parse_go_tests}


# --- the table merge rule ----------------------------------------------------------------

def _merge_rows(target: dict, rows: dict, table: str, prefix: str, conflicts: dict) -> None:
    for name, verdict in rows.items():
        if name == "":  # the summary takes the worst verdict
            target[""] = _worse(target[""], verdict) if "" in target else verdict
        elif name not in target or RANK[target[name]] == 0:  # a skip yields to any other verdict
            target[name] = verdict
        elif target[name] != verdict and RANK[verdict] != 0:
            conflicts.setdefault((table, prefix + name), {target[name]}).add(verdict)


def merge_tables(table: str, parts: list[dict]) -> tuple[dict, list[dict]]:
    """One table from every command's parse: (merged, conflicts as {table, name, verdicts}).

    A skip yields to any other verdict; two different non-skipped verdicts for one name are a conflict (the first
    is kept); the "" summary key takes the worst verdict (error > fail > ok > skipped).
    """
    merged: dict = {}
    conflicts: dict = {}
    for part in parts:
        if part and all(isinstance(value, dict) for value in part.values()):  # go-tests: per package
            for package, rows in part.items():
                _merge_rows(merged.setdefault(package, {}), rows, table, package + ":", conflicts)
        else:
            _merge_rows(merged, part, table, "", conflicts)
    merged = {key: (dict(sorted(value.items())) if isinstance(value, dict) else value)
              for key, value in sorted(merged.items())}
    rows = [{"table": t, "name": n, "verdicts": sorted(v)} for (t, n), v in sorted(conflicts.items())]
    return merged, rows


def _table_size(table: dict) -> int:
    """How many named tests a parse found (the "" summary alone is not a test)."""
    return sum(len([n for n in value if n]) if isinstance(value, dict) else bool(key)
               for key, value in table.items())


# --- scrubbing ---------------------------------------------------------------------------

def _scrubber(root: Path, scratch: Path, env_values: list[str]):
    """A function that removes what a record must not hold from a line of output: the repository and scratch
    paths, any other absolute path of the kinds the validator refuses, and the values of the entries' env."""
    literals = {str(root): "<root>", str(scratch): "<scratch>"}
    for base in (root, scratch):
        try:
            literals[str(base.resolve())] = "<root>" if base == root else "<scratch>"
        except OSError:
            pass
    literals[tempfile.gettempdir()] = "<tmp>"
    ordered = sorted(literals, key=len, reverse=True)
    secrets = sorted({v for v in env_values if len(v) >= 4}, key=len, reverse=True)

    def scrub(text: str) -> str:
        for literal in ordered:  # the paths first: an env value that is a path segment must not break them
            if literal and literal != "/":
                text = text.replace(literal, literals[literal])
        for value in secrets:
            text = text.replace(value, "<env>")
        return _PATH_RUN.sub("<path>", text)
    return scrub


# --- the producer --------------------------------------------------------------------------

def _run_entry(root: Path, entry: dict, scratch: Path, scrub) -> dict:
    env = dict(os.environ)
    env.update(entry["env"])
    started = time.monotonic()
    code, output = run_argv(entry["argv"], root, env=env, timeout=STEP_TIMEOUT)
    seconds = round(time.monotonic() - started, 3)
    try:
        (scratch / f"{entry['id']}.log").write_text(output, encoding="utf-8")
    except OSError as exc:
        raise KitError(f"cannot write {scratch}/{entry['id']}.log: {exc.strerror or exc}") from exc
    problem = None
    parsed = None
    if code == 124:
        problem = f"timed out after {STEP_TIMEOUT}s"
    elif code == 127:
        problem = "the command could not be started"
    elif entry["derives"] is not None:
        parsed = PARSERS[entry["derives"]](output)
        if code == 0 and not _table_size(parsed):
            problem = "no test entries parsed"
    tail = "\n".join(scrub(output).splitlines()[-TAIL_LINES:])
    return {"row": {"id": entry["id"], "track": entry["track"], "kind": entry["kind"], "argv": entry["argv"],
                    "env_keys": sorted(entry["env"]), "expected_exit": entry["expected_exit"], "exit": code,
                    "as_expected": code == entry["expected_exit"] and problem is None, "seconds": seconds,
                    "output_sha256": sha256_bytes(output.encode("utf-8")), "output_tail": tail,
                    "derives": entry["derives"], "problem": problem},
            "parsed": parsed}


def _run_track(root: Path, entries: list[dict], scratch: Path, scrub, results: dict, failures: list) -> float:
    started = time.monotonic()
    for entry in entries:
        try:
            results[entry["id"]] = _run_entry(root, entry, scratch, scrub)
        except BaseException as exc:  # a thread must report, not vanish
            failures.append(exc)
            break
    return round(time.monotonic() - started, 3)


def _toolchain_versions(root: Path, argvs: list[list[str]], scrub) -> list[dict]:
    versions = []
    for argv in argvs:
        code, output = run_argv(argv, root, timeout=TOOLCHAIN_TIMEOUT)
        lines = [ln for ln in scrub(output).splitlines() if ln.strip()]
        line = lines[0].strip() if code == 0 and lines else f"unavailable (exit {code})" if code else ""
        versions.append({"argv": argv, "line": line})
    return versions


def _hash_at(root: Path, head: str, path: str) -> str | None:
    blob = _show(root, head, path)
    return None if blob is None else sha256_bytes(blob)


def record_dir_name(head: str) -> str:
    return head[:12]


def run(root, only: str | None = None, scratch: Path | None = None) -> tuple[dict, list[str]]:
    """Run the release set at HEAD and write the record; return (record, dirty paths the run left behind).

    Refuses a dirty tree and a head12 that is already tracked. Raises KitError for a release set that does not
    load, an unknown track, or a record its own validator would refuse (nothing is written then).
    """
    root = Path(root).resolve()
    dirty = _dirty_paths(root)
    if dirty:
        raise KitError("working tree is not clean: " + ", ".join(dirty))
    head = _git_bytes(root, "rev-parse", "HEAD").decode().strip()
    head12 = record_dir_name(head)
    directory = root / BASE / head12
    tracked = _git_bytes(root, "ls-tree", "-r", "--name-only", "HEAD", "--", f"{BASE}/{head12}").decode().split()
    if tracked:
        raise KitError(f"already recorded at {head12}; a record is never rewritten")
    hidden = [line[2:].strip() for line in _git_bytes(root, "ls-files", "-v").decode("utf-8", "replace").splitlines()
              if line[:1] == "S" or line[:1].islower()]
    if hidden:
        raise KitError("git hides changes to tracked files (skip-worktree or assume-unchanged): " + ", ".join(hidden))
    release = load_release_set(root, head)
    for argv in [*release["toolchain"], *(e["argv"] for e in release["entries"])]:
        if any(ABSOLUTE_PATH.search(part) for part in argv):  # the record would be refused after the whole run
            raise KitError(f"{RELEASE_SET}: the argv {argv!r} holds an absolute path, which a record may not hold")
    tracks: dict[str, list[dict]] = {}
    for entry in release["entries"]:
        tracks.setdefault(entry["track"], []).append(entry)
    if only is not None and only not in tracks:
        raise KitError(f"no track named {only!r} in {RELEASE_SET} (tracks: {', '.join(tracks)})")
    selected = {name: rows for name, rows in tracks.items() if only in (None, name)}
    scratch = Path(tempfile.mkdtemp(prefix="fingerprint-")) if scratch is None else Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    if scratch.resolve() == root or root in scratch.resolve().parents:
        raise KitError(f"the scratch directory {scratch} is inside the repository: full output is never kept there")
    scrub = _scrubber(root, scratch.resolve(), [v for e in release["entries"] for v in e["env"].values()])

    versions = _toolchain_versions(root, release["toolchain"], scrub)  # before the tracks: the tree check sees them
    results: dict[str, dict] = {}
    seconds: dict[str, float] = {}
    failures: list = []

    def work(name: str) -> None:
        seconds[name] = _run_track(root, selected[name], scratch, scrub, results, failures)

    threads = [threading.Thread(target=work, args=(name,), name=f"track-{name}") for name in selected]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise failures[0] if isinstance(failures[0], KitError) else KitError(f"a track failed: {failures[0]}")

    after = _dirty_paths(root)
    unchanged = not after and _git_bytes(root, "rev-parse", "HEAD").decode().strip() == head
    ran = [entry for entry in release["entries"] if entry["id"] in results]
    rows = [results[entry["id"]]["row"] for entry in ran]
    tables: dict[str, dict] = {}
    conflicts: list[dict] = []
    for name in DERIVES:
        parts = [results[e["id"]]["parsed"] for e in ran if e["derives"] == name and results[e["id"]]["parsed"]]
        if any(e["derives"] == name for e in ran):
            tables[name], found = merge_tables(name, parts)
            conflicts.extend(found)
    tables = {name: table for name, table in tables.items() if table}  # a table nobody parsed anything into is not written
    files = {name: (json.dumps(table, indent=2, sort_keys=True) + "\n").encode("utf-8")
             for name, table in tables.items()}
    complete = only is None and unchanged and not conflicts
    record = {
        "schema": SCHEMA, "head": head, "head12": head12, "produced_at": utc_now(), "only": only,
        "release_set_sha256": _hash_at(root, head, RELEASE_SET),
        "toolchain": {"kit_manifest_sha256": _hash_at(root, head, "kit/MANIFEST.sha256"),
                      "checks_sha256": _hash_at(root, head, "program/checks.yaml"),
                      "versions": versions},
        "tracks": {name: {"seconds": seconds[name], "commands": [e["id"] for e in selected[name]]}
                   for name in selected},
        "commands": rows,
        "tables": {name: {"path": f"{BASE}/{head12}/{name}.json", "sha256": sha256_bytes(raw)}
                   for name, raw in files.items()},
        "table_conflicts": conflicts, "complete": complete,
        "all_as_expected": all(row["as_expected"] for row in rows), "tree_unchanged_by_run": unchanged,
    }
    text = json.dumps(record, indent=2) + "\n"
    problems = record_problems(record, text, directory.name)
    if problems:
        raise KitError("the record would not validate, so nothing was written: " + "; ".join(problems))
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():  # an earlier uncommitted run at this commit
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for name, raw in files.items():
        (directory / f"{name}.json").write_bytes(raw)
    (directory / RECORD).write_text(text, encoding="utf-8")
    return record, after


# --- the record check -------------------------------------------------------------------------

def _required() -> list[str]:
    schema = read_json(Path(__file__).resolve().parent.parent / "templates" / "fingerprint.schema.json")
    keys = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise KitError("fingerprint.schema.json: 'required' must be a list of key names")
    return keys


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def record_problems(data, text: str, dirname: str) -> list[str]:
    """Every problem a record has on its own (shape, consistency, no absolute path, head12 equals `dirname`)."""
    if not isinstance(data, dict):
        return ["the record must be a JSON object"]
    problems = [f"missing key: {key}" for key in _required() if key not in data]
    if problems:
        return problems

    def bad(key: str, why: str) -> None:
        problems.append(f"{key}: {why}")

    if data["schema"] != SCHEMA:
        bad("schema", f"must be {SCHEMA}")
    if not isinstance(data["head"], str) or not _HEX40.match(data["head"]):
        bad("head", "must be a 40-character lowercase hex SHA")
    elif data["head12"] != data["head"][:12]:
        bad("head12", "must be the first 12 characters of head")
    if data["head12"] != dirname:
        bad("head12", f"{data['head12']!r} is not the name of its directory {dirname!r}")
    if not isinstance(data["produced_at"], str) or not _STAMP.match(data["produced_at"]):
        bad("produced_at", "must be UTC, YYYY-MM-DDTHH:MM:SSZ")
    if data["only"] is not None and not isinstance(data["only"], str):
        bad("only", "must be a track name or null")
    if not isinstance(data["release_set_sha256"], str) or not _HEX64.match(data["release_set_sha256"]):
        bad("release_set_sha256", "must be a 64-character lowercase hex sha256")
    chain = data["toolchain"]
    if not isinstance(chain, dict) or set(chain) != {"kit_manifest_sha256", "checks_sha256", "versions"}:
        bad("toolchain", "must hold exactly kit_manifest_sha256, checks_sha256 and versions")
    else:
        for key in ("kit_manifest_sha256", "checks_sha256"):
            if chain[key] is not None and not (isinstance(chain[key], str) and _HEX64.match(chain[key])):
                bad(f"toolchain.{key}", "must be a 64-character lowercase hex sha256 or null")
        versions = chain["versions"]
        if not isinstance(versions, list) or not all(
                isinstance(v, dict) and set(v) == {"argv", "line"} and isinstance(v["line"], str)
                and isinstance(v["argv"], list) and all(isinstance(a, str) for a in v["argv"]) for v in versions):
            bad("toolchain.versions", "must be a list of {argv, line}")
    for key in ("complete", "all_as_expected", "tree_unchanged_by_run"):
        if not isinstance(data[key], bool):
            bad(key, "must be true or false")
    tracks = data["tracks"]
    if not isinstance(tracks, dict) or not all(
            isinstance(t, dict) and set(t) == {"seconds", "commands"} and _is_num(t["seconds"]) and t["seconds"] >= 0
            and isinstance(t["commands"], list) and all(isinstance(c, str) for c in t["commands"])
            for t in tracks.values()):
        bad("tracks", "must map each track to {seconds, commands}")
        tracks = {}
    commands = data["commands"]
    if not isinstance(commands, list):
        bad("commands", "must be a list")
        commands = []
    keys = {"id", "track", "kind", "argv", "env_keys", "expected_exit", "exit", "as_expected", "seconds",
            "output_sha256", "output_tail", "derives", "problem"}
    for index, row in enumerate(commands):
        where = f"commands[{index}]"
        if not isinstance(row, dict) or set(row) != keys:
            bad(where, "must hold exactly " + ", ".join(sorted(keys)))
            continue
        checks = [
            (isinstance(row["id"], str) and _ID.match(row["id"]), "id"),
            (isinstance(row["track"], str) and _ID.match(row["track"]), "track"),
            (row["kind"] in KINDS, "kind"),
            (isinstance(row["argv"], list) and row["argv"] and all(isinstance(a, str) for a in row["argv"]), "argv"),
            (isinstance(row["env_keys"], list) and all(isinstance(a, str) for a in row["env_keys"]), "env_keys"),
            (all(isinstance(row[k], int) and not isinstance(row[k], bool) for k in ("expected_exit", "exit")),
             "exit and expected_exit"),
            (isinstance(row["as_expected"], bool), "as_expected"),
            (_is_num(row["seconds"]) and row["seconds"] >= 0, "seconds"),
            (isinstance(row["output_sha256"], str) and _HEX64.match(row["output_sha256"]), "output_sha256"),
            (isinstance(row["output_tail"], str), "output_tail"),
            (row["derives"] in (*DERIVES, None), "derives"),
            (row["problem"] is None or isinstance(row["problem"], str), "problem"),
        ]
        for ok, key in checks:
            if not ok:
                bad(where, f"{key} is not valid")
        if row["as_expected"] is True and (row["exit"] != row["expected_exit"] or row["problem"] is not None):
            bad(where, "as_expected is true although the exit differs from expected_exit or a problem is recorded")
    if isinstance(data["all_as_expected"], bool) and all(isinstance(r, dict) for r in commands):
        if data["all_as_expected"] != all(r.get("as_expected") is True for r in commands):
            bad("all_as_expected", "does not match the command rows")
    if tracks and all(isinstance(r, dict) for r in commands):
        listed = sorted(c for t in tracks.values() for c in t["commands"])
        if listed != sorted(str(r.get("id")) for r in commands):
            bad("tracks", "the commands of the tracks are not the command rows")
    tables = data["tables"]
    if not isinstance(tables, dict) or not all(
            isinstance(t, dict) and set(t) == {"path", "sha256"} and isinstance(t["path"], str)
            and isinstance(t["sha256"], str) and _HEX64.match(t["sha256"]) for t in tables.values()):
        bad("tables", "must map each table to {path, sha256}")
        tables = {}
    for name, table in tables.items():
        if table["path"] != f"{BASE}/{dirname}/{name}.json":
            bad(f"tables.{name}.path", f"must be {BASE}/{dirname}/{name}.json")
    conflicts = data["table_conflicts"]
    if not isinstance(conflicts, list) or not all(
            isinstance(c, dict) and set(c) == {"table", "name", "verdicts"} for c in conflicts):
        bad("table_conflicts", "must be a list of {table, name, verdicts}")
        conflicts = []
    if isinstance(data["complete"], bool) and isinstance(data["tree_unchanged_by_run"], bool):
        should = data["only"] is None and data["tree_unchanged_by_run"] and not conflicts
        if data["complete"] != should:
            bad("complete", "does not match only, tree_unchanged_by_run and table_conflicts")
    found = ABSOLUTE_PATH.search(text)
    if found:
        bad("text", f"holds an absolute path ({text[found.start():found.start() + 24]!r}...)")
    return problems


def _record_files(root: Path) -> list[str]:
    """Repository-relative paths of the fingerprint.json files tracked in the index."""
    listing = _git_bytes(root, "ls-files", "-z", "--", BASE).decode("utf-8")
    return sorted(p for p in listing.split("\0") if p and PurePosixPath(p).name == RECORD
                  and len(PurePosixPath(p).parts) == 5)


def validate_record(root, rel: str) -> list[str]:
    """Every problem of the record at `rel` (relative to the root): its own checks, then its files and release set."""
    root = Path(root)
    path = root / rel
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read {rel}: {getattr(exc, 'strerror', None) or exc}"]
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        return [f"{rel}: not valid JSON: {exc}"]
    problems = record_problems(data, text, path.parent.name)
    if problems:
        return [f"{rel}: {problem}" for problem in problems]
    for name, table in sorted(data["tables"].items()):
        try:
            actual = sha256_bytes((root / table["path"]).read_bytes())
        except OSError as exc:
            problems.append(f"{rel}: table {name} cannot be read ({table['path']}): {exc.strerror or exc}")
            continue
        if actual != table["sha256"]:
            problems.append(f"{rel}: table {name} was altered: {table['path']} hashes to {actual}, not "
                            f"{table['sha256']}")
    try:
        _git_bytes(root, "cat-file", "-e", f"{data['head']}^{{commit}}")
        present = True
    except KitError:
        present = False  # a shallow clone or an unfetched commit: the release set cannot be compared
    if present:
        blob = _show(root, data["head"], RELEASE_SET)
        actual = sha256_bytes(blob) if blob is not None else None
        if actual != data["release_set_sha256"]:
            problems.append(f"{rel}: release_set_sha256 is {data['release_set_sha256']}, but {RELEASE_SET} at "
                            f"{data['head12']} hashes to {actual}")
        else:
            problems.extend(f"{rel}: {why}" for why in _release_problems(root, data))
    return problems


def _release_problems(root: Path, data: dict) -> list[str]:
    """The record against the release set at its head: the rows are the entries it ran, and no env value of 4 or
    more characters appears in the text a command could have echoed (output tails, toolchain lines, problems)."""
    try:
        release = load_release_set(root, data["head"])
    except KitError as exc:
        return [f"the release set at {data['head12']} does not load: {exc}"]
    wanted = [e for e in release["entries"] if data["only"] in (None, e["track"])]
    def shape(entry: dict) -> tuple:
        return (entry["id"], entry["argv"], entry["expected_exit"], entry["track"], entry["kind"], entry["derives"],
                sorted(entry["env"]))

    ran = [(row["id"], row["argv"], row["expected_exit"], row["track"], row["kind"], row["derives"],
            sorted(row["env_keys"])) for row in data["commands"]]
    if ran != [shape(entry) for entry in wanted]:
        have = {row[0] for row in ran}
        missing = [e["id"] for e in wanted if e["id"] not in have]
        extra = sorted(have - {e["id"] for e in wanted})
        return ["the command rows are not the entries of the release set at "
                f"{data['head12']} (missing: {', '.join(missing) or 'none'}; not in the set: "
                f"{', '.join(extra) or 'none'}; or a row differs in argv, expected_exit, track, kind, derives or "
                "env_keys)"]
    texts = [row["output_tail"] for row in data["commands"]] + [v["line"] for v in data["toolchain"]["versions"]] \
        + [row["problem"] or "" for row in data["commands"]]
    return [f"entry {entry['id']}: the value of env {name} appears in the record"
            for entry in release["entries"] for name, value in sorted(entry["env"].items())
            if len(value) >= 4 and any(value in text for text in texts)]


# --- commands ---------------------------------------------------------------------------------

def _print_plan(root: Path) -> None:
    release = load_release_set(root, "HEAD")
    seconds = _recorded_seconds(root)
    tracks: dict[str, list[dict]] = {}
    for entry in release["entries"]:
        tracks.setdefault(entry["track"], []).append(entry)
    for name, entries in tracks.items():
        print(f"track {name}")
        for entry in entries:
            took = seconds.get(entry["id"])
            print(f"  {entry['id']}\t{entry['kind']}\t{'-' if took is None else f'{took:g}s'}\t"
                  + " ".join(entry["argv"]))


def cmd_run(args: argparse.Namespace) -> int:
    root = find_root()
    scratch = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix="fingerprint-"))
    record, dirty = run(root, only=args.only, scratch=scratch)
    for row in record["commands"]:
        if not row["as_expected"]:
            why = row["problem"] or f"exit {row['exit']}, expected {row['expected_exit']}"
            print(f"FAILED: {row['id']} ({why})")
    for conflict in record["table_conflicts"]:
        print(f"CONFLICT: {conflict['table']} {conflict['name']}: {', '.join(conflict['verdicts'])}")
    print(f"fingerprint {record['head12']}: complete {str(record['complete']).lower()}, "
          f"all as expected {str(record['all_as_expected']).lower()}, {len(record['commands'])} command(s), "
          f"{BASE}/{record['head12']}/")
    print(f"full output: {scratch}")
    if dirty:
        raise KitError("the run changed the working tree: " + ", ".join(dirty))
    return 0 if record["complete"] and record["all_as_expected"] else 1


def cmd_plan(args: argparse.Namespace) -> int:
    _print_plan(find_root())
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = find_root().resolve()
    if args.path:
        target = Path(args.path).resolve()
        if target.is_dir():
            target = target / RECORD
        try:
            rels = [target.relative_to(root).as_posix()]
        except ValueError:
            raise KitError(f"{args.path} is not inside the repository at {root}") from None
    else:
        rels = _record_files(root)
    problems = [problem for rel in rels for problem in validate_record(root, rel)]
    for problem in problems:
        print("error: " + " ".join(problem.split()), file=sys.stderr)
    if problems:
        return 1
    print(f"fingerprint: {len(rels)} record(s) ok")
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    action = getattr(args, "fingerprint_action", None)
    handlers = {"run": cmd_run, "plan": cmd_plan, "validate": cmd_validate}
    if action not in handlers:
        raise KitError("fingerprint needs an action: " + ", ".join(handlers))
    return handlers[action](args)


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="fingerprint_action", metavar="action", parser_class=type(parser))
    runner = actions.add_parser("run", help="run the release set at HEAD and write the fingerprint record")
    runner.add_argument("--only", metavar="TRACK", help="run one track only (the record is then not complete)")
    runner.add_argument("--scratch", metavar="DIR", help="where the full output of each command goes "
                                                        "(default: a new directory under TMPDIR)")
    actions.add_parser("plan", help="list the release set by track with the seconds the last record took")
    checker = actions.add_parser("validate", help="check fingerprint records (default: every tracked one)")
    checker.add_argument("path", nargs="?", help="a record directory or its fingerprint.json")


def register(add_command) -> None:
    add_command("fingerprint", cmd_fingerprint, help="the release fingerprint: run, plan, validate",
                configure=_configure)


# --- readers (section 4: a later batch) ----------------------------------------------------------
# `current_fingerprint(root, ref)`, `show`, `coverage` and the `pk stale` / hand-off fields belong here, after the
# producer. Until they land, `plan` uses `_recorded_seconds`, a private stand-in with the same nearest-ancestor rule.

def _recorded_seconds(root: Path) -> dict[str, float]:
    """{command id: seconds} of the nearest tracked record at or behind HEAD; empty when there is none."""
    best = None
    for rel in _record_files(root):
        blob = _show(root, "HEAD", rel)
        try:
            data = json.loads(blob.decode("utf-8")) if blob is not None else None
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("head"), str):
            continue
        try:
            _git_bytes(root, "merge-base", "--is-ancestor", data["head"], "HEAD")
            behind = int(_git_bytes(root, "rev-list", "--count", f"{data['head']}..HEAD").decode().strip())
        except (KitError, ValueError):
            continue
        key = (behind, str(data.get("produced_at", "")))
        if best is None or key[0] < best[0][0] or (key[0] == best[0][0] and key[1] > best[0][1]):
            best = (key, data)
    if best is None:
        return {}
    return {row["id"]: row["seconds"] for row in best[1].get("commands", [])
            if isinstance(row, dict) and "id" in row and _is_num(row.get("seconds"))}
