#!/usr/bin/env python3
"""The evidence kit: commit-bound evidence records and the planted-bug runner (standard library only).

`Evidence(root, task_id)` records HEAD, the main branch and their merge-base, hashes inputs from
the committed blobs (never the working copy), runs commands as argv lists and records what they
really did, runs planted bugs on a throwaway mirror of HEAD, and appends a results record to the
task's committed, append-only record directory (`store.py`) only when every honesty precondition holds; the
task's pointer `program/evidence/<ID>/latest.json` then names it as the latest record, and the record it replaces
stays as history (`supersedes` names it). A record is a fingerprint: at commit `head` these inputs produced this
outcome. A later change never makes it stale; it only drifts from the tree at the checkout's HEAD (`pk stale`), and
the record carries no stale field. `pk evidence validate <record>` re-checks the invariants;
`gate_problems` adds what the hand-back gate, the binding check and `done` require on top of them.
`results.schema.json` describes the record those three accept: its planted-bug rows require ROW_KEYS
and PLANTED_KEYS, and `validate` refuses a row that lacks any of them.
`lint_record` keeps the evidence graph acyclic: a record binds only inputs that can change its outcome,
never a derived output (another record, an acceptance, a gate result, a status snapshot, a ledger, a claim
document or anything under `program/evidence/` but its tooling); only a gate record binds other records, by
digest (each child row says whether that child had drifted from the tree the gate ran on), and nothing binds a
gate record.
The kit never assumes a language: the caller passes the build and test argv lists.

Known limits:
- A record edited consistently by hand, renamed to its new digest and pointed at, still validates;
  binding it to the commit and re-running its commands is the reviewer's job.
- Gitignored files written by a command are invisible to the clean-tree check.
- `write` accepts zero commands and surviving planted bugs; the hand-back gate rejects them.
- The runner cannot know that a test command is not a test, or that a mutant only failed to import.
  Every planted-bug row therefore records the file, the edit and the build and test argv, so a
  reviewer can see it in the record.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

import store
from common import KitError, find_root, git, read_json, run_argv, sha256_bytes, utc_now
from register import GATE_ID

SELF_TEST_ID = "self-test-noncompiling"
BROKEN_LINE = "def (:"  # cannot parse (Python) or compile (any compiled language)
OUTPUT_TAIL = 2000
KILL_REASONS = ("test failed", "build failed", "not killed")
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
COMMAND_KEYS = ("label", "argv", "exit", "expected_exit", "as_expected", "derived", "output_sha256",
                "output_tail")
ROW_KEYS = ("id", "builds", "killed", "kill_reason", "test_output_sha256")  # what a planted-bug row found
PLANTED_KEYS = ("path", "find", "replace", "build_argv", "test_argv")  # what a row planted and what judged it
DEFINITION_KEYS = ("path", "find", "replace", "build_argv", "test_argv", "serial")  # what defines a row (0.5.3)
DEFERRED_KEYS = ("id", "deferred", *DEFINITION_KEYS, "definition_sha256")  # a deferred row of a results record
TIMING_KEYS = ("seconds", "rows", "workers")  # a results record's optional `planted_bugs_timing` (0.5.3)
_TIMED_OUT = 124
_NO_RUN = (_TIMED_OUT, 127)  # timeout, program not found: the shell conventions run_argv uses

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*\Z")
_TOOLING = (".py", ".sh", ".toml")  # what may be bound under program/evidence/: produce.py and its helpers
_DERIVED_DIRS = ("program/evidence/", "program/claims/", "program/status/", "program/approvals/")
_DERIVED_FILES = ("program/controller-state.json", "program/progress.md", "program/HANDOFF.md",
                  "program/changes/register-log.md")
_RECORD_NAMES = ("results.json", "acceptance.json")
DECISIONS = ("accepted_development", "rejected")

# --- the `evidence` section of checks.yaml (0.5.2) ---------------------------------------------------------------
EVIDENCE_KEYS = ("plant_workers", "reuse_unchanged_code", "reuse_ignore", "reuse_compare")
PLANT_WORKERS_ENV = "PK_PLANT_WORKERS"  # overrides evidence.plant_workers; an explicit `workers=` overrides both
GATE_TEST_ENV = "PK_GATE_TEST_RESULTS"  # the hand-back gate's test-step results, for `Evidence.reuse_gate_test`
MAX_WORKERS = 64
JUDGED = ("parallel", "serial", "serial-recheck")  # a planted-bug row's optional `judged`, written by a parallel run
# A sign that a test failed because of the machine, not the mutant: a pool verdict showing one is judged again alone.
RESOURCE_ERROR = re.compile(
    r"EADDRINUSE|address already in use|port is already allocated"
    r"|ECONNREFUSED[^\n]*(?:127\.\d+\.\d+\.\d+|localhost|::1)"
    r"|out of memory|cannot allocate memory|ENOMEM|ENOSPC|no space left on device|EMFILE|too many open files"
    r"|timed out", re.IGNORECASE)


def _config_paths(value, key: str, under_program: bool) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise KitError(f"checks.yaml: evidence.{key} must be a list of paths")
    paths = []
    for item in value:
        while item.startswith("./"):
            item = item[2:]
        pure = PurePosixPath(item)
        if not item or item.startswith("/") or ".." in pure.parts:
            raise KitError(f"checks.yaml: evidence.{key}: {item!r} must be a path relative to the root")
        inside = item.startswith("program/")
        if under_program and not inside:
            raise KitError(f"checks.yaml: evidence.{key}: {item!r} is not under program/ (every path outside "
                           "program/ is compared already)")
        if not under_program and (inside or item.rstrip("/") == "program"):
            raise KitError(f"checks.yaml: evidence.{key}: {item!r} is under program/, which is never compared "
                           "unless evidence.reuse_compare names it")
        if not under_program and (item.rstrip("/") == "kit" or item.startswith("kit/")):
            raise KitError(f"checks.yaml: evidence.{key}: {item!r} is the kit, which is always compared")
        paths.append(item)
    return paths


def parse_config(value) -> dict:
    """The `evidence` section of checks.yaml: {plant_workers, reuse_unchanged_code, reuse_ignore, reuse_compare}.

    Absent means one worker, no reuse and no paths, which is how every program ran before 0.5.2.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise KitError("checks.yaml: evidence must be a mapping of " + ", ".join(EVIDENCE_KEYS))
    unknown = sorted(str(key) for key in value if key not in EVIDENCE_KEYS)
    if unknown:
        raise KitError(f"checks.yaml: evidence: unknown key {', '.join(unknown)} (expected {', '.join(EVIDENCE_KEYS)})")
    workers = value.get("plant_workers", 1)
    if workers is None:
        workers = 1
    if not (isinstance(workers, int) and not isinstance(workers, bool) and 1 <= workers <= MAX_WORKERS):
        raise KitError(f"checks.yaml: evidence.plant_workers must be a whole number from 1 to {MAX_WORKERS}")
    reuse = value.get("reuse_unchanged_code", False)
    if reuse is None:
        reuse = False
    if not isinstance(reuse, bool):
        raise KitError("checks.yaml: evidence.reuse_unchanged_code must be true or false")
    return {"plant_workers": workers, "reuse_unchanged_code": reuse,
            "reuse_ignore": _config_paths(value.get("reuse_ignore"), "reuse_ignore", False),
            "reuse_compare": _config_paths(value.get("reuse_compare"), "reuse_compare", True)}


def config_at(root, ref: str = "HEAD") -> dict:
    """The `evidence` section of `program/checks.yaml` as committed at `ref`; the defaults without the file."""
    import miniyaml  # late: only a run that reads its configuration needs the parser

    try:
        raw = _git_bytes(Path(root), "show", f"{ref}:program/checks.yaml")
    except KitError:
        return parse_config(None)
    try:
        data = miniyaml.loads(raw.decode("utf-8").removeprefix("\ufeff"))
    except (UnicodeDecodeError, miniyaml.YAMLError) as exc:
        raise KitError(f"program/checks.yaml at {ref} cannot be read: {exc}") from exc
    return parse_config(data.get("evidence") if isinstance(data, dict) else None)


def plant_workers(root, workers: int | None = None) -> int:
    """How many planted-bug rows run at once: `workers` when given, else PK_PLANT_WORKERS, else
    `evidence.plant_workers` of the committed checks.yaml, else 1."""
    if workers is None:
        raw = os.environ.get(PLANT_WORKERS_ENV, "").strip()
        if raw:
            if not raw.isdigit():
                raise KitError(f"{PLANT_WORKERS_ENV} must be a whole number from 1 to {MAX_WORKERS}, not {raw!r}")
            workers = int(raw)
        else:
            workers = config_at(root)["plant_workers"]
    if not (isinstance(workers, int) and not isinstance(workers, bool) and 1 <= workers <= MAX_WORKERS):
        raise KitError(f"planted-bug workers must be a whole number from 1 to {MAX_WORKERS}, not {workers!r}")
    return workers


def definition_digest(row: dict) -> str:
    """The sha256 of a planted-bug row's definition (DEFINITION_KEYS, `serial` false when absent), as canonical JSON.

    A deferred row of a results record carries it, and every row of a `plants` record carries the digest of the
    definition it judged, so a chunk can be checked against the row the gate recorded."""
    import json

    definition = {key: row.get(key) for key in DEFINITION_KEYS}
    definition["serial"] = bool(definition["serial"])
    return sha256_bytes(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def is_deferred(row) -> bool:
    """Whether a planted-bug row of a results record was deferred (0.5.3): recorded with its definition, not judged."""
    return isinstance(row, dict) and row.get("deferred") is True


def required_keys(name: str) -> list[str]:
    """Required top-level keys of kit/templates/<name>.schema.json (`results` or `acceptance`)."""
    if name not in ("results", "acceptance"):
        raise KitError(f"unknown evidence schema: {name}")
    schema = read_json(TEMPLATES / f"{name}.schema.json")
    keys = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise KitError(f"{name}.schema.json: 'required' must be a list of key names")
    return keys


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        lines = [ln.strip() for ln in proc.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        raise KitError(f"git {' '.join(args)} failed: {lines[0] if lines else f'exit {proc.returncode}'}")
    return proc.stdout


def _argv(value, what: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) for part in value):
        raise KitError(f"{what} must be a non-empty argv list of strings, not a shell string")
    return list(value)


def _then(body: Callable[[], object], cleanup: Callable[[], str | None], describe: Callable[[object], str]):
    """`body()`, then `cleanup()` whatever happened (it returns why it failed, or None); return the body's result.

    A cleanup failure never replaces or hides what the body did. After a KitError both are raised as one
    KitError, the body's first; any other exception is raised as it is, with the cleanup failure attached as a
    note; after a result the KitError says what the body gave (`describe(result)`), then why the cleanup failed.
    """
    try:
        result = body()
    except BaseException as exc:
        problem = cleanup()
        if problem and isinstance(exc, KitError):
            raise KitError(f"{exc}; {problem}") from exc
        if problem:
            exc.add_note(problem)
        raise
    problem = cleanup()
    if problem:
        raise KitError(f"{describe(result)}; {problem}")
    return result


def _clear_bytecode(mirror: Path) -> None:
    """Remove every __pycache__ under the mirror so no stale bytecode outlives an edit."""
    for dirpath, dirnames, _ in os.walk(mirror):
        for name in [n for n in dirnames if n == "__pycache__"]:
            shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
            dirnames.remove(name)


class _MirrorCleanup:
    """What every throwaway mirror shares: its restore to the commit it was made at, and its removal.

    `Evidence.planted_bugs` and `PlantJudge` both use it, so a re-judged row runs on exactly the mirror the gate's
    runner uses. A subclass sets `self.root` and `self._mirror`; `_after_restore` runs after every restore.
    """

    root: Path
    _mirror: "Path | None"

    def _remove_mirror(self, mirror: Path, added: bool) -> str | None:
        """Remove the mirror; why that failed, or None."""
        if not added:  # git never registered it: remove the empty directory this call created
            shutil.rmtree(mirror, ignore_errors=True)
            return None
        try:
            git(self.root, "worktree", "remove", "--force", str(mirror))
        except KitError as exc:
            return f"cannot remove mirror {mirror}: {exc}"
        return None

    def _restore(self, mirror: Path) -> None:
        """Return the whole mirror to HEAD: tracked edits undone, untracked and ignored files removed.

        Refuses unless `mirror` is the temp directory this instance created and is its own git top level,
        so `git clean` can never reach the project.
        """
        if self._mirror is None or mirror != self._mirror or mirror == self.root \
                or not mirror.name.startswith("pk-mirror-"):
            raise KitError(f"refusing to clean {mirror}: not the mirror this run created")
        if Path(git(mirror, "rev-parse", "--show-toplevel")).resolve() != mirror.resolve():
            raise KitError(f"refusing to clean {mirror}: it is not its own worktree")
        git(mirror, "checkout", "--", ".")
        git(mirror, "clean", "-fdxq")

    def _restore_problem(self, mirror: Path) -> str | None:
        """Restore the mirror; why that failed, or None."""
        try:
            self._restore(mirror)
            self._after_restore(mirror)
        except (KitError, OSError) as exc:
            return f"the mirror could not be restored: {exc}"
        return None

    def _after_restore(self, mirror: Path) -> None:
        """Hook run after every restore; the evidence runner needs nothing more."""


class Evidence(_MirrorCleanup):
    """One task's evidence run. Construction records the starting state; nothing is written until `write`."""

    def __init__(self, root: Path, task_id: str, main: str = "main"):
        if not isinstance(task_id, str) or not task_id:
            raise KitError("task_id must be a non-empty string")
        if not _REF.match(main):
            raise KitError(f"not a usable branch name: {main}")
        self.root = Path(root).resolve()
        self.task_id = task_id
        self.main = main
        top = Path(git(self.root, "rev-parse", "--show-toplevel")).resolve()
        if top != self.root:  # `HEAD:<path>` is relative to the top level, so the two must agree
            raise KitError(f"root must be the git top level: {self.root} is inside {top}")
        self.head = git(self.root, "rev-parse", "HEAD")
        self.main_sha = git(self.root, "rev-parse", "--verify", main + "^{commit}")
        self.merge_base = git(self.root, "merge-base", "HEAD", main)
        self.start_dirty = self._dirty_paths()
        self.inputs: dict[str, str] = {}
        self.commands: list[dict] = []
        self.planted_bug_rows: list[dict] = []
        self._mirror: Path | None = None  # the mirror this instance created and may restore
        self.blocked_by: list[str] = []
        self.deviations: list[str] = []
        self.children: list[dict] = []  # a gate record's child records, by digest
        self.reused_from: dict | None = None  # {path, digest, head} of the record a reused run copied (0.5.2)
        self.planted_bugs_timing: dict | None = None  # {seconds, rows, workers} of the judged rows (0.5.3)
        self.plants_reused_from: list[dict] | None = None  # plants records of the reused record (0.5.3)

    @classmethod
    def judging_at(cls, root, head: str) -> "Evidence":
        """A runner that only judges planted-bug rows in mirrors of `head` (`pk gate plants`): it records nothing,
        hashes nothing and never writes a results record."""
        run = cls.__new__(cls)
        run.root, run.head, run.task_id = Path(root).resolve(), head, ""
        run._mirror = None
        return run

    # --- tree ------------------------------------------------------------------

    def _dirty_paths(self) -> list[str]:
        lines = git(self.root, "status", "--porcelain").splitlines()
        return [ln.split(None, 1)[1] if len(ln.split(None, 1)) == 2 else ln for ln in lines]

    def require_clean_tree(self) -> None:
        """Raise KitError listing the paths if `git status --porcelain` is not empty."""
        dirty = self._dirty_paths()
        if dirty:
            raise KitError("working tree is not clean: " + ", ".join(dirty))

    # --- inputs ----------------------------------------------------------------

    @staticmethod
    def _norm(path) -> str:
        """A root-relative posix path; absolute paths, `..` and the root itself are refused."""
        if not isinstance(path, str) or not path:
            raise KitError("path must be a non-empty string")
        if path.startswith("/") or PureWindowsPath(path).is_absolute():
            raise KitError(f"path must be relative to the root: {path}")
        pure = PurePosixPath(path)
        if ".." in pure.parts:
            raise KitError(f"path must not contain '..': {path}")
        if not pure.parts:
            raise KitError(f"path must name a file or directory: {path}")
        return pure.as_posix()

    def _object_type(self, path: str) -> str | None:
        try:
            return git(self.root, "cat-file", "-t", f"HEAD:{path}")
        except KitError:
            return None

    def _blob(self, path: str) -> bytes:
        return _git_bytes(self.root, "cat-file", "blob", f"HEAD:{path}")

    def hash_inputs(self, paths: list[str]) -> dict[str, str]:
        """sha256 of each path's blob at HEAD, sorted by path; directories expand to their tracked files.

        Hashing goes through `git cat-file blob`, so a dirty working copy cannot change a hash.
        A tracked symlink is hashed as its link text (what git stores), never its target.
        The result is also merged into `self.inputs`.
        """
        found: dict[str, str] = {}
        for raw in paths:
            path = self._norm(raw)
            kind = self._object_type(path)
            if kind is None:
                raise KitError(f"input is not tracked at HEAD: {path}")
            if kind == "tree":
                listing = git(self.root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", path)
                files = [name for name in listing.split("\0") if name]
            elif kind == "blob":
                files = [path]
            else:
                raise KitError(f"input is neither a file nor a directory at HEAD: {path}")
            for name in files:
                found[name] = sha256_bytes(self._blob(name))
        found = dict(sorted(found.items()))
        self.inputs = dict(sorted({**self.inputs, **found}.items()))
        return found

    # --- commands --------------------------------------------------------------

    def run_command(self, label: str, argv: list[str], expect: int = 0,
                    parse: Callable[[str], dict] | None = None, cwd: Path | None = None,
                    timeout: int | None = None) -> dict:
        """Run `argv` (never through a shell) and record the real result.

        `derived` is `parse(output)` when given. If `parse` raises or returns a non-dict, `derived`
        is `{}` and `as_expected` is false: a field that could not be parsed is never "expected".
        """
        argv = _argv(argv, "command")
        code, output = run_argv(argv, Path(cwd) if cwd is not None else self.root, timeout=timeout)
        record = self._command_record(label, argv, expect, parse, code, output)
        self.commands.append(record)
        return record

    @staticmethod
    def _command_record(label: str, argv: list[str], expect: int, parse: Callable[[str], dict] | None, code: int,
                        output: str) -> dict:
        """The command row of a command that exited `code` printing `output` (what `run_command` records)."""
        derived: dict = {}
        parsed_ok = True
        if parse is not None:
            try:
                result = parse(output)
            except Exception:  # noqa: BLE001 - any parser failure is "not parsed"
                result = None
            if isinstance(result, dict):
                derived = result
            else:
                parsed_ok = False
        record = {
            "label": label, "argv": argv, "exit": code, "expected_exit": expect,
            "as_expected": code == expect and parsed_ok, "derived": derived,
            "output_sha256": sha256_bytes(output.encode("utf-8")), "output_tail": output[-OUTPUT_TAIL:],
        }
        return record

    def reuse_gate_test(self, label: str, argv: list[str], parse: Callable[[str], dict] | None = None,
                        expect: int = 0, timeout: int | None = None) -> dict:
        """The row `run_command(label, argv, expect, parse)` records, taken from the hand-back gate's own test step
        when that step ran exactly `argv` in this root at this head; otherwise the command is run.

        The gate's test step runs `checks.yaml`'s test commands in the task worktree at the head this run records,
        and hands their exit and full output to produce.py through PK_GATE_TEST_RESULTS. A command whose argv is
        byte-identical to one of them, run from the root (no `cwd`), is not run again: its row is built from that
        output as `run_command` would build it, and carries `from_gate_step: "test"`. Without the variable (a run
        outside the gate), with a file for another root or head, or for any other command, this is `run_command`.
        """
        argv = _argv(argv, "command")
        found = gate_test_output(self.root, self.head, argv)
        if found is None:
            return self.run_command(label, argv, expect=expect, parse=parse, timeout=timeout)
        record = {**self._command_record(label, argv, expect, parse, *found), "from_gate_step": "test"}
        self.commands.append(record)
        return record

    # --- planted bugs ----------------------------------------------------------

    def _check_table(self, table) -> list[dict]:
        """Validate every row against HEAD before anything runs; a bad row is a table error."""
        if not isinstance(table, list) or not table:
            raise KitError("planted-bug table must be a non-empty list of rows")
        rows, seen = [], set()
        for index, raw in enumerate(table, 1):
            if not isinstance(raw, dict):
                raise KitError(f"planted-bug row {index} must be a mapping")
            rid = raw.get("id")
            if not isinstance(rid, str) or not rid:
                raise KitError(f"planted-bug row {index} needs a string id")
            if rid == SELF_TEST_ID or rid in seen:
                raise KitError(f"planted bug {rid}: id is reserved or repeated")
            seen.add(rid)
            find, replace, args = raw.get("find"), raw.get("replace"), raw.get("test_args", [])
            if not isinstance(find, str) or not find or not isinstance(replace, str):
                raise KitError(f"planted bug {rid}: find must be a non-empty string and replace a string")
            if find == replace:
                raise KitError(f"planted bug {rid}: replace equals find, so nothing is planted")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise KitError(f"planted bug {rid}: test_args must be a list of strings")
            serial, defer = raw.get("serial", False), raw.get("defer", False)
            if not isinstance(serial, bool):
                raise KitError(f"planted bug {rid}: serial must be true or false")
            if not isinstance(defer, bool):
                raise KitError(f"planted bug {rid}: defer must be true or false")
            path = self._norm(raw.get("path"))
            if self._object_type(path) != "blob":
                raise KitError(f"planted bug {rid}: {path} is not a tracked file at HEAD")
            mode = git(self.root, "ls-tree", "HEAD", "--", path).split(None, 1)[0]
            if mode not in ("100644", "100755"):  # a symlink would let the edit escape the mirror
                raise KitError(f"planted bug {rid}: {path} is not a regular file at HEAD (mode {mode})")
            try:
                text = self._blob(path).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise KitError(f"planted bug {rid}: {path} is not UTF-8 text") from exc
            count = text.count(find)
            if count != 1:
                raise KitError(f"planted bug {rid}: find occurs {count} times in {path} (it must occur exactly "
                               "once); fix the table")
            rows.append({"id": rid, "path": path, "find": find, "replace": replace, "test_args": args,
                         "serial": serial, "defer": defer})
        return rows

    def planted_bugs(self, table: list[dict], build: list[str] | None, test: list[str],
                     timeout: int | None = None, workers: int | None = None) -> list[dict]:
        """Apply each planted bug to a mirror of HEAD, build, run the test; return one row per bug.

        Row: `{id, builds, killed, kill_reason, test_output_sha256, path, find, replace, build_argv,
        test_argv}`: the last five say what was planted (the file and the edit) and which build and test
        argv judged it. The mirror is `git worktree add --detach` of HEAD in a fresh temp dir and is
        always removed; the working tree is never edited. A table row is
        `{id, path, find, replace, test_args}`, and optionally `serial: true` (see below); `test` is the
        base argv and a row's `test_args` are appended. `find` must occur exactly once in `path`.
        A row is killed only when the build passed and the test exited non-zero. The unmutated
        mirror must pass first, once per distinct `test_args`, so a test that already fails cannot
        "kill" anything. Bytecode caches are cleared around every run.

        A test exit of 127 or 124 (program not found, timeout) is never a kill: the row is
        `builds: false, kill_reason: "build failed"`, with or without a build step. The one exception is
        the self-test row when no build step was given: a timeout there raises KitError naming the timeout,
        because a hang is not a build failure and nothing else could tell the two apart.

        Pass a build step (for Python, `["python3", "-m", "compileall", "-q", "."]`). With `build=None`
        a syntax error is indistinguishable from a test failure, so the self-test row raises KitError
        whenever the broken file makes the test exit non-zero, and also when the test still passes. In
        practice `build=None` therefore always raises for a project whose tests load the mutated file.

        For an interpreted language a build step that only parses does not load the file: a mutant that
        parses but cannot be imported (a module-level NameError) passes the build and is recorded as
        killed by the test. Make the build step import the edited module when that difference matters.
        The runner also cannot tell that `test` is not a test; the row records both argv lists so a
        reviewer can.

        The last row is always `self-test-noncompiling`: the first row's file with a line that cannot
        parse or compile appended (its `find` is empty and its `replace` is that line). It must come
        back `builds: false, killed: false`; if it does not, the runner cannot tell a build failure
        from a test failure here and KitError is raised.

        A failure to restore or remove the mirror never replaces or hides what a row did: the error names
        the row's own error or result first, then the failure to clean up.

        Workers (0.5.2). `workers` rows are judged at once; None means PK_PLANT_WORKERS, else
        `evidence.plant_workers` of the committed `program/checks.yaml`, else 1. With one worker the run is
        exactly the one above: one mirror, restored between rows, and rows without a `judged` field. With more,
        every baseline and every row runs in its own fresh mirror (added and removed one at a time, so git's
        index lock never races), `serial: true` rows (and their baselines) and the self-test row run after the
        pool, one at a time, and every row gains `judged`: `parallel`, `serial`, or `serial-recheck` for a pool
        verdict that was judged again alone because it was not a clean kill (any row that did not build or was
        not killed, a kill whose output shows a resource error such as EADDRINUSE, ECONNREFUSED on localhost,
        a timeout, out of memory, ENOSPC or EMFILE, and every kill of a test whose baseline failed in the pool).
        A baseline that fails in the pool is run again alone before it can fail the run. The rows keep the
        table's order. Rows that use a browser or a fixed port must be marked `serial: true`. One line per
        finished baseline and row is printed to standard output.

        Deferred rows (0.5.3). A row marked `defer: true` is not judged here: it is recorded with its full definition
        (`deferred: true`, path, find, replace, build_argv, test_argv, serial and `definition_sha256`) and no
        verdict, in its table position. `pk gate plants` judges deferred rows later, at this record's head, and
        writes `plants` records bound to it; QA's acceptance and the merge need every deferred row killed there.
        `planted_bugs_all_killed` counts the judged rows only. The self-test row still runs, on the first row's file.
        """
        rows = self._check_table(table)
        test = _argv(test, "test")
        build = _argv(build, "build") if build is not None else None
        count = plant_workers(self.root, workers)
        judged = [item for item in rows if not item["defer"]]
        started = time.monotonic()
        if count > 1:
            found = _PlantPool(self, judged, build, test, timeout, count, first=rows[0]).run()
        else:
            found = self._serial_run(rows[0], judged, build, test, timeout)
        by_id = {row["id"]: row for row in found}
        results = [self._deferred_row(item, build, test) if item["defer"] else by_id[item["id"]] for item in rows]
        results.append(by_id[SELF_TEST_ID])
        self.planted_bugs_timing = {"seconds": round(time.monotonic() - started, 1), "rows": len(judged) + 1,
                                    "workers": count}
        self.planted_bug_rows = results
        return results

    def _deferred_row(self, item: dict, build: list[str] | None, test: list[str]) -> dict:
        row = {"id": item["id"], "deferred": True,
               **self._planted(item["path"], item["find"], item["replace"], build, test + item["test_args"]),
               "serial": item["serial"]}
        row["definition_sha256"] = definition_digest(row)
        return row

    def _serial_run(self, first: dict, rows: list[dict], build: list[str] | None, test: list[str],
                    timeout: int | None) -> list[dict]:
        """One worker: one mirror of HEAD, restored between rows; the rows, then the self-test row on `first`."""
        mirror = Path(tempfile.mkdtemp(prefix="pk-mirror-"))
        added = []  # non-empty once git has registered the mirror
        self._mirror = mirror

        def run() -> list[dict]:
            git(self.root, "worktree", "add", "--detach", str(mirror), "HEAD")
            added.append(mirror)
            return self._run_rows(mirror, rows, build, test, timeout, first)

        try:
            return _then(run, lambda: self._remove_mirror(mirror, bool(added)),
                         lambda found: f"{len(found)} planted-bug rows ran")
        finally:
            self._mirror = None

    def _judge(self, mirror: Path, rid: str, path: str, edit: Callable[[bytes], bytes], build: list[str] | None,
               argv: list[str], timeout: int | None, cleaner: "_MirrorCleanup | None" = None
               ) -> tuple[bool, int | None, str, str]:
        """Edit `path` in the mirror, build, test, restore (through `cleaner`, by default this instance).

        Returns (builds, test exit or None, test output hash, the output of the failing build or of the test).
        """
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        target = mirror / path

        def run() -> tuple[bool, int | None, str, str]:
            target.write_bytes(edit(target.read_bytes()))
            _clear_bytecode(mirror)
            if build is not None:
                code, output = run_argv(build, mirror, env=env, timeout=timeout)
                if code != 0:
                    return False, None, "", output
            code, output = run_argv(argv, mirror, env=env, timeout=timeout)
            builds = code not in _NO_RUN  # a timeout or a missing program is never a kill
            return builds, code, sha256_bytes(output.encode("utf-8")), output

        def gave(result) -> str:
            return (f"planted bug {rid} gave builds: {str(result[0]).lower()}, "
                    f"test exit: {'not run' if result[1] is None else result[1]}")

        return _then(run, lambda: (cleaner or self)._restore_problem(mirror), gave)

    def _attempt(self, mirror: Path, rid: str, path: str, edit: Callable[[bytes], bytes], build: list[str] | None,
                 argv: list[str], timeout: int | None,
                 cleaner: "_MirrorCleanup | None" = None) -> tuple[bool, int | None, str]:
        """Edit `path` in the mirror, build, test, restore. Returns (builds, test exit or None, test hash)."""
        return self._judge(mirror, rid, path, edit, build, argv, timeout, cleaner)[:3]

    def _baseline_problem(self, mirror: Path, build: list[str] | None, argv: list[str], rid: str,
                          timeout: int | None, cleaner: "_MirrorCleanup | None" = None) -> str | None:
        """Why the unmutated mirror does not build or pass `argv`, or None; the mirror is restored either way."""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

        def run() -> str | None:
            if build is not None:
                code, _ = run_argv(build, mirror, env=env, timeout=timeout)
                if code != 0:
                    return (f"unmutated mirror does not build (exit {code}); fix the build before planting bugs "
                            f"(row {rid})")
            code, _ = run_argv(argv, mirror, env=env, timeout=timeout)
            if code != 0:
                return f"unmutated mirror fails its own test (exit {code}); a failing test kills nothing (row {rid})"
            return None

        return _then(run, lambda: (cleaner or self)._restore_problem(mirror),
                     lambda found: found or f"unmutated mirror passes its test (row {rid})")

    def _baseline(self, mirror: Path, build: list[str] | None, argv: list[str], rid: str,
                  timeout: int | None, cleaner: "_MirrorCleanup | None" = None) -> None:
        problem = self._baseline_problem(mirror, build, argv, rid, timeout, cleaner)
        if problem:
            raise KitError(problem)

    @staticmethod
    def _planter(item: dict) -> Callable[[bytes], bytes]:
        def plant(data: bytes) -> bytes:
            return data.decode("utf-8").replace(item["find"], item["replace"]).encode("utf-8")

        return plant

    def _row(self, item: dict, builds: bool, code: int | None, digest: str, build: list[str] | None,
             argv: list[str]) -> dict:
        """A planted-bug row from what its attempt gave: killed only when it built and the test exited non-zero."""
        if not builds:
            killed, reason = False, "build failed"
        elif code != 0:
            killed, reason = True, "test failed"
        else:
            killed, reason = False, "not killed"
        return {"id": item["id"], "builds": builds, "killed": killed, "kill_reason": reason,
                "test_output_sha256": digest, **self._planted(item["path"], item["find"], item["replace"], build, argv)}

    def _run_rows(self, mirror: Path, rows: list[dict], build: list[str] | None, test: list[str],
                  timeout: int | None, first: dict | None = None) -> list[dict]:
        results, checked = [], set()
        for item in rows:
            argv = test + item["test_args"]
            if tuple(argv) not in checked:
                self._baseline(mirror, build, argv, item["id"], timeout)
                checked.add(tuple(argv))
            builds, code, digest = self._attempt(mirror, item["id"], item["path"], self._planter(item), build, argv,
                                                 timeout)
            results.append(self._row(item, builds, code, digest, build, argv))
        first = rows[0] if first is None else first
        results.append(self._self_test(mirror, first, build, test + first["test_args"], timeout))
        return results

    @staticmethod
    def _planted(path: str, find: str, replace: str, build: list[str] | None, argv: list[str]) -> dict:
        """The PLANTED_KEYS of a row: the file, the edit, and the build and test argv that judged it."""
        return {"path": path, "find": find, "replace": replace,
                "build_argv": list(build) if build is not None else None, "test_argv": list(argv)}

    def _self_test(self, mirror: Path, first: dict, build: list[str] | None, argv: list[str],
                   timeout: int | None, cleaner: "_MirrorCleanup | None" = None) -> dict:
        def break_file(data: bytes) -> bytes:
            lead = b"" if not data or data.endswith(b"\n") else b"\n"
            return data + lead + BROKEN_LINE.encode() + b"\n"

        builds, code, digest = self._attempt(mirror, SELF_TEST_ID, first["path"], break_file, build, argv, timeout,
                                             cleaner)
        if build is None:
            if code == 0:
                raise KitError(f"self-test cannot be trusted: {first['path']} with an unparseable line still "
                               "passes the test, so a broken file is not detected; pass a build command")
            if code == _TIMED_OUT:  # a hang is not a build failure, and with no build step nothing tells them apart
                limit = f"after {timeout}s" if timeout is not None else f"(exit {code})"
                raise KitError(f"self-test cannot be trusted: the test timed out {limit} on {first['path']} with an "
                               "unparseable line, and without a build step a timeout cannot be told from a build "
                               "failure; pass a build command or a longer timeout")
            if builds:  # failed for some reason other than "program missing or timed out"
                raise KitError("self-test cannot be trusted: without a build step a build failure cannot be "
                               "told from a test failure; pass a build command")
        elif code is not None:  # the build step accepted the broken file
            raise KitError(f"planted-bug results cannot be trusted: the build step accepted an unparseable line "
                           f"in {first['path']} (test exit {code}), so the runner cannot tell a build failure from "
                           "a test failure for this project")
        return {"id": SELF_TEST_ID, "builds": False, "killed": False, "kill_reason": "build failed",
                "test_output_sha256": digest if code is not None else "",
                **self._planted(first["path"], "", BROKEN_LINE, build, argv)}

    # --- child records (gate records only) ----------------------------------------

    def bind_children(self, task_ids: list[str]) -> list[dict]:
        """Bind the latest results and acceptance record of each task by digest (a gate record only).

        Each child must have a results record its pointer names; its acceptance is bound when there is one. A record
        whose bytes no longer match its pointer is refused. Each row records the child's drift at HEAD: `fresh` when
        every input of its results record is unchanged, else `drifted`. Drift is information for the reader, never
        a failure. Returns the rows added to `self.children`.
        """
        import stale as stale_module  # lazy: stale imports nothing from here, but keep the module graph flat

        if not is_gate(self.task_id):
            raise KitError(f"{self.task_id} is not a gate: only a gate record binds other records")
        index = store.read_index(self.root)
        added = []
        for task_id in task_ids:
            if is_gate(task_id):
                raise KitError(f"binds a gate record: {task_id} (nothing may bind a gate record)")
            if store.latest(index, task_id, "results") is None:
                raise KitError(f"{task_id} has no results record in {store.pointer_path(task_id)}")
            entry = store.latest(index, task_id, "results")
            inputs = store.input_digests(store.load(self.root, entry["path"], entry["digest"]).get("inputs"))
            changed = stale_module.changed_inputs(self.root, self.head, inputs or {})
            drift = "drifted" if changed or inputs is None else "fresh"
            for kind in store.KINDS:
                entry = store.latest(index, task_id, kind)
                if entry is not None:
                    store.load(self.root, entry["path"], entry["digest"])
                    added.append({"task_id": task_id, "kind": kind, "digest": entry["digest"], "drift": drift})
        self.children = [*self.children, *added]
        return added

    # --- write -----------------------------------------------------------------

    def write(self, path=None) -> dict:
        """Append the results record to `program/evidence/<task>/results/` and point the task's pointer at it; return
        {digest, path (relative to the root), file}. Raise KitError listing every reason it refused (nothing is
        appended and the pointer is untouched).

        The checks run before anything is written, so the new record and pointer do not count as dirtying the tree.
        The record names the task's previous latest results record as `supersedes` (null for the first one).
        """
        if path is not None:
            raise KitError("Evidence.write takes no path since program-kit 0.2.0: the record is appended to "
                           "program/evidence/<ID>/results/ and program/evidence/<ID>/latest.json points at it")
        reasons: list[str] = []
        now_dirty = self._dirty_paths()
        head_now = git(self.root, "rev-parse", "HEAD")
        if self.start_dirty:
            reasons.append("tree was not clean at construction: " + ", ".join(self.start_dirty))
        if now_dirty:
            reasons.append("tree is not clean now: " + ", ".join(now_dirty))
        if head_now != self.head:
            reasons.append(f"HEAD moved from {self.head} to {head_now} during the run")
        unexpected = [cmd["label"] for cmd in self.commands if not cmd["as_expected"]]
        if unexpected:
            reasons.append("commands not as expected: " + ", ".join(unexpected))
        bugs = [row for row in self.planted_bug_rows if row["id"] != SELF_TEST_ID]
        judged = [row for row in bugs if not is_deferred(row)]
        if not self.planted_bug_rows:
            reasons.append("no planted-bug rows (run planted_bugs first)")
        else:
            if not bugs:
                reasons.append("no planted-bug rows besides the self-test row")
            if len(bugs) == len(self.planted_bug_rows):
                reasons.append(f"self-test row {SELF_TEST_ID} is missing")
        if not self.inputs:
            reasons.append("no inputs hashed (call hash_inputs)")
        previous = None
        try:
            store.check_task(self.task_id)
            entry = store.read_pointer(self.root, self.task_id).get("results")
            previous = entry["digest"] if entry is not None else None
        except KitError as exc:
            reasons.append(str(exc))
        if reasons:
            raise KitError("; ".join(reasons))
        data = {
            "task_id": self.task_id, "head": self.head, "main": self.main_sha, "merge_base": self.merge_base,
            "produced_at": utc_now(), "inputs": store.input_list(self.inputs), "commands": list(self.commands),
            "planted_bugs": list(self.planted_bug_rows),
            "planted_bugs_all_killed": all(row["killed"] for row in judged),
            "all_commands_as_expected": all(cmd["as_expected"] for cmd in self.commands),
            "tree_unchanged_by_run": True,
            "blocked_by": list(self.blocked_by), "deviations": list(self.deviations),
            "supersedes": previous,
        }
        if self.children:
            data["children"] = [dict(child) for child in self.children]
        if self.reused_from is not None:
            data["reused_from"] = dict(self.reused_from)
        if self.planted_bugs_timing is not None:
            data["planted_bugs_timing"] = dict(self.planted_bugs_timing)
        if self.plants_reused_from is not None:
            data["plants_reused_from"] = [dict(item) for item in self.plants_reused_from]
        # last line of defence: never append what validate or the lint would refuse
        problems = validate_results(data) + lint_record(data, self.root)
        if problems:
            raise KitError("; ".join(problems))
        appended = store.record(self.root, self.task_id, "results", data)
        return {key: appended[key] for key in ("digest", "path", "file")}


# --- the gate's test-step output (0.5.2) ------------------------------------------

def gate_test_output(root, head: str, argv: list[str]) -> tuple[int, str] | None:
    """(exit, full output) of `argv` as the hand-back gate's test step ran it in `root` at `head`, or None.

    Read from the file PK_GATE_TEST_RESULTS names: `{"root", "head", "commands": [{"argv", "exit", "output"}]}`.
    No variable, an unreadable or malformed file, another root or head, or no byte-identical argv gives None, so
    the caller runs the command itself.
    """
    name = os.environ.get(GATE_TEST_ENV)
    if not name:
        return None
    try:
        data = read_json(Path(name))
    except KitError:
        return None
    if not isinstance(data, dict) or data.get("head") != head or not isinstance(data.get("root"), str) \
            or Path(data["root"]).resolve() != Path(root).resolve() or not isinstance(data.get("commands"), list):
        return None
    for command in data["commands"]:
        if isinstance(command, dict) and command.get("argv") == argv and _is_int(command.get("exit")) \
                and isinstance(command.get("output"), str):
            return command["exit"], command["output"]
    return None


# --- parallel planted-bug judging (0.5.2) -----------------------------------------

class _Slot(_MirrorCleanup):
    """The mirror of one job of a parallel run: its restore guard knows that mirror and no other."""

    def __init__(self, root: Path, mirror: Path):
        self.root = root
        self._mirror = mirror


class _PlantPool:
    """`Evidence.planted_bugs` with more than one worker (its docstring says what a run guarantees).

    Order: the baselines and the rows not marked `serial` in a pool of threads, each job in its own fresh mirror of
    the recorded head; then, one at a time, every baseline that failed in the pool, every pool verdict that is not a
    clean kill (or that a failed pool baseline puts in doubt), the serial rows with their baselines, and last the
    self-test row. A job's mirror is added and removed under one lock; nothing else is shared between jobs.
    """

    def __init__(self, ev: "Evidence", rows: list[dict], build: list[str] | None, test: list[str],
                 timeout: int | None, workers: int, first: dict | None = None, self_test: bool = True):
        self.ev, self.rows, self.build, self.test = ev, rows, build, test
        self.timeout, self.workers = timeout, workers
        self.first = first if first is not None else (rows[0] if rows else None)  # the self-test row's file
        self.self_test = self_test
        self._git_lock = threading.Lock()  # git worktree add and remove, one at a time
        self._print_lock = threading.Lock()
        self._done = 0

    def _argv(self, item: dict) -> list[str]:
        """The row's test argv: its own `test_argv` (a chunk of deferred rows), else the base test and its args."""
        return list(item["test_argv"]) if "test_argv" in item else self.test + item["test_args"]

    def _build(self, item: dict) -> list[str] | None:
        """The row's build argv: its own `build_argv` (a chunk of deferred rows), else the run's build."""
        return (list(item["build_argv"]) if item["build_argv"] is not None else None) if "build_argv" in item \
            else self.build

    def _key(self, item: dict) -> tuple:
        """What a baseline is shared by: the build and the test argv."""
        build = self._build(item)
        return (tuple(build) if build is not None else None, tuple(self._argv(item)))

    def _progress(self, what: str, verdict: str, judged: str, started: float, counts: bool) -> None:
        with self._print_lock:
            if counts:
                self._done += 1
            where = f" [{self._done}/{len(self.rows) + int(self.self_test)}]" if counts else ""
            print(f"{what}: {verdict} ({judged}, {time.monotonic() - started:.0f} s){where}", flush=True)

    def _in_mirror(self, body: Callable[[_Slot, Path], object], describe: Callable[[object], str]):
        """`body(slot, mirror)` in a fresh detached mirror of the recorded head, which is always removed."""
        mirror = Path(tempfile.mkdtemp(prefix="pk-mirror-"))
        slot = _Slot(self.ev.root, mirror)
        added = []

        def run():
            with self._git_lock:
                git(self.ev.root, "worktree", "add", "--detach", str(mirror), self.ev.head)
                added.append(mirror)
            return body(slot, mirror)

        def cleanup() -> str | None:
            with self._git_lock:
                return slot._remove_mirror(mirror, bool(added))

        return _then(run, cleanup, describe)

    def _baseline(self, key: tuple, rid: str, judged: str) -> str | None:
        """Why the unmutated mirror fails the build and test of `key`, or None."""
        started = time.monotonic()
        build, argv = (list(key[0]) if key[0] is not None else None), list(key[1])
        problem = self._in_mirror(
            lambda slot, mirror: self.ev._baseline_problem(mirror, build, argv, rid, self.timeout, slot),
            lambda found: found or f"unmutated mirror passes its test (row {rid})")
        verdict = "passes" if problem is None else ("fails, judged again alone" if judged == "parallel" else "fails")
        self._progress(f"baseline of {rid}", verdict, judged, started, counts=False)
        return problem

    def _judge(self, item: dict, judged: str) -> tuple[dict, str]:
        """(the row, the output that decided it) of one planted bug in its own mirror."""
        started = time.monotonic()
        argv, build = self._argv(item), self._build(item)
        builds, code, digest, output = self._in_mirror(
            lambda slot, mirror: self.ev._judge(mirror, item["id"], item["path"], self.ev._planter(item), build,
                                                argv, self.timeout, slot),
            lambda found: f"planted bug {item['id']} gave builds: {str(found[0]).lower()}, "
                          f"test exit: {'not run' if found[1] is None else found[1]}")
        row = {**self.ev._row(item, builds, code, digest, build, argv), "judged": judged}
        self._progress(f"plant {item['id']}", row["kill_reason"], judged, started, counts=judged != "parallel"
                       or not self._doubtful(row, output))
        return row, output

    @staticmethod
    def _doubtful(row: dict, output: str) -> bool:
        """Whether a pool verdict must be judged again alone: anything but a kill with no sign of a resource error."""
        return not row["killed"] or RESOURCE_ERROR.search(output) is not None

    def _pool(self, jobs: list[Callable[[], object]]) -> list:
        """Every job's result in job order; the first failure in job order is raised once every running job ended."""
        results: list = [None] * len(jobs)
        errors: dict[int, BaseException] = {}
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="pk-plant") as pool:
            futures = {pool.submit(job): index for index, job in enumerate(jobs)}
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                index = futures[future]
                try:
                    results[index] = future.result()
                except BaseException as exc:  # noqa: BLE001 - raised below, after the pool drained
                    errors[index] = exc
                    for other in futures:
                        other.cancel()
        if errors:
            raise errors[min(errors)]
        return results

    def run(self) -> list[dict]:
        firsts: dict[tuple, str] = {}  # each distinct build and test argv, with the first row that uses it
        for item in self.rows:
            firsts.setdefault(self._key(item), item["id"])
        serial_keys = {self._key(item) for item in self.rows if item["serial"]}
        pooled_baselines = [(key, rid) for key, rid in firsts.items() if key not in serial_keys]
        pooled = [item for item in self.rows if not item["serial"]]
        jobs: list[Callable[[], object]] = [
            (lambda key=key, rid=rid: self._baseline(key, rid, "parallel")) for key, rid in pooled_baselines]
        jobs += [(lambda item=item: self._judge(item, "parallel")) for item in pooled]
        outcomes = self._pool(jobs)
        doubted = set()
        for (key, rid), problem in zip(pooled_baselines, outcomes[:len(pooled_baselines)]):
            if problem is not None:  # judged again alone before it can fail the run
                again = self._baseline(key, rid, "serial-recheck")
                if again is not None:
                    raise KitError(again)
                doubted.add(key)
        results: dict[str, dict] = {}
        for item, (row, output) in zip(pooled, outcomes[len(pooled_baselines):]):
            if self._doubtful(row, output) or self._key(item) in doubted:
                row, _ = self._judge(item, "serial-recheck")
            results[item["id"]] = row
        checked = {key for key, _ in pooled_baselines}
        for item in self.rows:
            if not item["serial"]:
                continue
            key = self._key(item)
            if key not in checked:
                problem = self._baseline(key, item["id"], "serial")
                if problem is not None:
                    raise KitError(problem)
                checked.add(key)
            results[item["id"]], _ = self._judge(item, "serial")
        return [results[item["id"]] for item in self.rows] + ([self._self_test()] if self.self_test else [])

    def _self_test(self) -> dict:
        started = time.monotonic()
        first = self.first
        row = self._in_mirror(
            lambda slot, mirror: self.ev._self_test(mirror, first, self.build, self._argv(first), self.timeout, slot),
            lambda found: f"the self-test row gave {found['kill_reason']}")
        self._progress(f"plant {SELF_TEST_ID}", row["kill_reason"], "serial", started, counts=True)
        return {**row, "judged": "serial"}


# --- one row at a time ------------------------------------------------------------

class PlantJudge(_MirrorCleanup):
    """Judges single planted-bug rows on a throwaway mirror the way `Evidence.planted_bugs` does, writing nothing.

    Use it as a context manager: entering adds a detached worktree of `ref` in a fresh temp directory (`pk-mirror-`,
    the evidence runner's own prefix) and runs `prepare(mirror)` when given; leaving removes it. Each `attempt`
    starts from that clean mirror (`git checkout -- .`, `git clean -fdxq`, then `prepare` again), applies one edit,
    runs the row's build commands, then its test, and restores the mirror. The verdict is the runner's: killed only
    when every build command passed and the test exited non-zero; a test exit of 124 or 127 (timeout, program not
    found) is never a kill and reads `build failed`, as in a results record. A row with no test reports only whether
    the edit builds (`kill_reason` `builds`). No record, pointer or status is written: this is feedback between gate
    runs, never evidence. The project's own checkout is never edited.

    A row: `{id, path, find, replace}` with `build_argv` (one argv list, or None) or `build_argvs` (several, run in
    order), and `test_argv` (an argv list, or None for a build-only row). `find` must occur exactly once in `path`;
    otherwise, and for a path that leaves the mirror or is not a file, the result carries a `problem` and no verdict.
    """

    def __init__(self, root, ref: str = "HEAD", prepare: Callable[[Path], None] | None = None,
                 timeout: int | None = None):
        self.root = Path(root).resolve()
        self.ref = ref
        self.timeout = timeout
        self._prepare = prepare
        self._mirror: Path | None = None
        self._added = False

    def __enter__(self) -> "PlantJudge":
        mirror = Path(tempfile.mkdtemp(prefix="pk-mirror-")).resolve()
        self._mirror = mirror
        try:
            git(self.root, "worktree", "add", "--detach", str(mirror), self.ref)
            self._added = True
            if self._prepare is not None:
                self._prepare(mirror)
        except BaseException as exc:
            problem = self._remove_mirror(mirror, self._added)
            self._mirror, self._added = None, False
            if problem and isinstance(exc, KitError):
                raise KitError(f"{exc}; {problem}") from exc
            raise
        return self

    def __exit__(self, kind, exc, trace) -> None:
        mirror, added = self._mirror, self._added
        self._mirror, self._added = None, False
        problem = self._remove_mirror(mirror, added) if mirror is not None else None
        if problem and exc is None:
            raise KitError(problem)
        if problem:
            exc.add_note(problem)

    @property
    def mirror(self) -> Path:
        if self._mirror is None:
            raise KitError("the plant judge has no mirror: use it in a with block")
        return self._mirror

    def _after_restore(self, mirror: Path) -> None:
        if self._prepare is not None:
            self._prepare(mirror)

    def restore(self) -> None:
        """Return the mirror to `ref` (and `prepare` again) after a caller ran something in it."""
        problem = self._restore_problem(self.mirror)
        if problem:
            raise KitError(problem)

    def _env(self) -> dict:
        return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def _builds(self, builds: list[list[str]], mirror: Path) -> tuple[int, list[str] | None, str]:
        """(exit, the failing argv or None, its output) of the build commands, stopping at the first failure."""
        for argv in builds:
            code, output = run_argv(argv, mirror, env=self._env(), timeout=self.timeout)
            if code != 0:
                return code, list(argv), output
        return 0, None, ""

    @staticmethod
    def _build_list(row: dict) -> list[list[str]]:
        if row.get("build_argvs") is not None:
            return [_argv(argv, "build") for argv in row["build_argvs"]]
        if row.get("build_argv") is not None:
            return [_argv(row["build_argv"], "build")]
        return []

    def baseline(self, builds: list[list[str]], test: list[str] | None) -> dict:
        """The unmutated mirror: {ok, step (build or test), argv, exit, tail}. A failing baseline judges nothing."""
        mirror = self.mirror

        def run() -> dict:
            _clear_bytecode(mirror)
            code, argv, output = self._builds(builds, mirror)
            if code != 0:
                return {"ok": False, "step": "build", "argv": argv, "exit": code, "tail": _tail(output)}
            if test is None:
                return {"ok": True, "step": "build", "argv": None, "exit": 0, "tail": []}
            code, output = run_argv(_argv(test, "test"), mirror, env=self._env(), timeout=self.timeout)
            return {"ok": code == 0, "step": "test", "argv": list(test), "exit": code, "tail": _tail(output)}

        return _then(run, lambda: self._restore_problem(mirror), lambda found: f"the baseline gave {found['exit']}")

    def attempt(self, row: dict) -> dict:
        """One edit on the clean mirror, its build, its test; the result dict described on the class."""
        mirror = self.mirror
        rid, path, find, replace = row.get("id"), row.get("path"), row.get("find"), row.get("replace")
        builds = self._build_list(row)
        test = _argv(row["test_argv"], "test") if row.get("test_argv") is not None else None
        result = {"id": rid, "path": path, "find": find, "replace": replace, "builds": None, "killed": None,
                  "kill_reason": None, "problem": None, "build_argv": None, "build_exit": None, "build_tail": [],
                  "test_argv": test, "test_exit": None, "test_tail": []}
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            return {**result, "problem": "find must be a non-empty string and replace a string"}
        try:
            rel = Evidence._norm(path)
        except KitError as exc:
            return {**result, "problem": str(exc)}
        target = mirror / rel
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(mirror):
            return {**result, "problem": f"{rel} is not a regular file in the mirror of {self.ref}"}

        def run() -> dict:
            raw = target.read_bytes()  # bytes as they are, as the evidence runner edits them
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {**result, "problem": f"{rel} is not UTF-8 text"}
            count = text.count(find)
            if count != 1:
                return {**result, "problem": f"find occurs {count} times in {rel} (it must occur exactly once)"}
            target.write_bytes(text.replace(find, replace).encode("utf-8"))
            _clear_bytecode(mirror)
            code, argv, output = self._builds(builds, mirror)
            if code != 0:
                return {**result, "builds": False, "killed": False, "kill_reason": "build failed", "build_argv": argv,
                        "build_exit": code, "build_tail": _tail(output)}
            done = {**result, "build_exit": 0}
            if test is None:
                return {**done, "builds": True, "kill_reason": "builds"}
            code, output = run_argv(test, mirror, env=self._env(), timeout=self.timeout)
            done.update(test_exit=code, test_tail=_tail(output))
            if code in _NO_RUN:  # a timeout or a missing program is never a kill
                return {**done, "builds": False, "killed": False, "kill_reason": "build failed"}
            if code != 0:
                return {**done, "builds": True, "killed": True, "kill_reason": "test failed"}
            return {**done, "builds": True, "killed": False, "kill_reason": "not killed"}

        return _then(run, lambda: self._restore_problem(mirror),
                     lambda found: f"planted bug {rid} gave {found.get('kill_reason') or found.get('problem')}")


def _tail(output: str, lines: int = 20) -> list[str]:
    return output.splitlines()[-lines:]


# --- validation ------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _planted_problem(row: dict) -> str | None:
    """The first PLANTED_KEYS field of a row that is present with the wrong type, as `<key> must ...`."""
    for key in ("path", "find", "replace"):
        if key in row and (not isinstance(row[key], str) or (key == "path" and not row[key])):
            return f"{key} must be a {'non-empty ' if key == 'path' else ''}string"
    for key in ("build_argv", "test_argv"):
        if key not in row or (key == "build_argv" and row[key] is None):
            continue
        if not (_str_list(row[key]) and row[key]):
            return (f"{key} must be a non-empty argv list of strings"
                    + (", or null when no build step was given" if key == "build_argv" else ""))
    return None


def validate_results(data) -> list[str]:
    """Every problem found in a results record (empty when valid)."""
    if not isinstance(data, dict):
        return ["results must be a JSON object"]
    problems = [f"missing key: {key}" for key in required_keys("results") if key not in data]
    if problems:
        return problems

    def bad(key: str, why: str) -> None:
        problems.append(f"{key}: {why}")

    if not isinstance(data["task_id"], str) or not data["task_id"]:
        bad("task_id", "must be a non-empty string")
    for key in ("head", "main", "merge_base"):
        if not isinstance(data[key], str) or not _HEX40.match(data[key]):
            bad(key, "must be a 40-character lowercase hex SHA")
    if not isinstance(data["produced_at"], str) or not _STAMP.match(data["produced_at"]):
        bad("produced_at", "must be UTC, YYYY-MM-DDTHH:MM:SSZ")
    inputs = store.input_digests(data["inputs"])
    if not inputs:
        bad("inputs", "must be a non-empty list of {path, sha256} objects with distinct paths (a record written "
                      "before 0.4.1 holds a mapping of path to sha256, which is read the same way)")
    elif not all(isinstance(k, str) and isinstance(v, str) and _HEX64.match(v) for k, v in inputs.items()):
        bad("inputs", "every value must be a 64-character lowercase hex sha256")
    for key in ("planted_bugs_all_killed", "all_commands_as_expected", "tree_unchanged_by_run"):
        if not isinstance(data[key], bool):
            bad(key, "must be true or false")
    for key in ("blocked_by", "deviations"):
        if not _str_list(data[key]):
            bad(key, "must be a list of strings")

    commands = data["commands"]
    if not isinstance(commands, list):
        bad("commands", "must be a list")
        commands = []
    flags = []
    for index, cmd in enumerate(commands):
        where = f"commands[{index}]"
        if not isinstance(cmd, dict):
            bad(where, "must be an object")
            continue
        missing = [k for k in COMMAND_KEYS if k not in cmd]
        if missing:
            bad(where, "missing " + ", ".join(missing))
            continue
        ok = (isinstance(cmd["label"], str) and _str_list(cmd["argv"]) and cmd["argv"]
              and _is_int(cmd["exit"]) and _is_int(cmd["expected_exit"]) and isinstance(cmd["as_expected"], bool)
              and isinstance(cmd["derived"], dict) and isinstance(cmd["output_tail"], str)
              and isinstance(cmd["output_sha256"], str) and _HEX64.match(cmd["output_sha256"]))
        if not ok:
            bad(where, "has a field of the wrong type")
            continue
        if cmd["as_expected"] and cmd["exit"] != cmd["expected_exit"]:
            bad(where, "marked as_expected although exit differs from expected_exit")
        if "from_gate_step" in cmd and cmd["from_gate_step"] != "test":
            bad(where, "from_gate_step must be test, the only gate step whose output a producer reuses")
        flags.append(cmd["as_expected"])
    if isinstance(data["all_commands_as_expected"], bool) and data["all_commands_as_expected"] != all(flags):
        bad("all_commands_as_expected", f"is {data['all_commands_as_expected']} but the commands give {all(flags)}")

    rows = data["planted_bugs"]
    if not isinstance(rows, list):
        bad("planted_bugs", "must be a list")
        rows = []
    shaped, seen = [], set()
    for index, row in enumerate(rows):
        where = f"planted_bugs[{index}]"
        if is_deferred(row):  # 0.5.3: recorded with its definition, judged later by `pk gate plants`
            problem = _deferred_problem(row)
            if problem:
                bad(where, problem)
            elif row["id"] in seen:
                bad(where, f"repeats id {row['id']}")
            seen.add(row.get("id"))
            continue
        if not (isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("builds"), bool)
                and isinstance(row.get("killed"), bool)):
            bad(where, "needs a string id and boolean builds and killed")
            continue
        if row["id"] in seen:
            bad(where, f"repeats id {row['id']}")
        seen.add(row["id"])
        if row["killed"] and not row["builds"]:
            bad(where, "killed although it does not build")
        expected = "build failed" if not row["builds"] else "test failed" if row["killed"] else "not killed"
        if row.get("kill_reason") != expected:
            bad(where, f"kill_reason must be '{expected}' for builds={str(row['builds']).lower()}, "
                       f"killed={str(row['killed']).lower()} (found {row.get('kill_reason')!r})")
        digest = row.get("test_output_sha256")
        if not isinstance(digest, str) or not (digest == "" or _HEX64.match(digest)):
            bad(where, "test_output_sha256 must be 64 lowercase hex characters, or empty when no test ran")
        absent = [key for key in PLANTED_KEYS if key not in row]
        if absent:
            bad(where, f"does not record {', '.join(absent)} (what was planted and the commands that ran)")
        planted = _planted_problem(row)
        if planted:
            bad(where, planted)
        if "judged" in row and row["judged"] not in JUDGED:
            bad(where, f"judged must be one of {', '.join(JUDGED)} (found {row['judged']!r})")
        shaped.append(row)
    selfs = [row for row in shaped if row["id"] == SELF_TEST_ID]
    bugs = [row for row in shaped if row["id"] != SELF_TEST_ID]
    deferred = [row for row in rows if is_deferred(row)]
    if len(selfs) != 1:
        bad("planted_bugs", f"needs exactly one {SELF_TEST_ID} row (found {len(selfs)})")
    elif selfs[0]["builds"] or selfs[0]["killed"]:
        bad("planted_bugs", f"the {SELF_TEST_ID} row must have builds: false and killed: false")
    if not bugs and not deferred:
        bad("planted_bugs", "needs at least one planted-bug row besides the self-test row")
    if isinstance(data["planted_bugs_all_killed"], bool) and data["planted_bugs_all_killed"] != all(
            row["killed"] for row in bugs):
        bad("planted_bugs_all_killed", "does not match the planted-bug rows (self-test row excluded)")
    if data["tree_unchanged_by_run"] is False:
        bad("tree_unchanged_by_run", "is false: the run changed the tree, so this is not valid evidence")
    supersedes = data.get("supersedes")
    if supersedes is not None and not (isinstance(supersedes, str) and _HEX64.match(supersedes)):
        bad("supersedes", "must be the 64-character sha256 of the record it replaces, or null")
    reused = data.get("reused_from")
    if "reused_from" in data and not (isinstance(reused, dict) and set(reused) == {"path", "digest", "head"}
                                      and isinstance(reused["path"], str) and reused["path"]
                                      and isinstance(reused["digest"], str) and _HEX64.match(reused["digest"])
                                      and isinstance(reused["head"], str) and _HEX40.match(reused["head"])):
        bad("reused_from", "must be {path, digest: sha256, head: 40-hex sha} of the record this one copied")
    timing = data.get("planted_bugs_timing")
    if "planted_bugs_timing" in data and not (
            isinstance(timing, dict) and set(timing) == set(TIMING_KEYS)
            and isinstance(timing["seconds"], (int, float)) and not isinstance(timing["seconds"], bool)
            and timing["seconds"] >= 0 and _is_int(timing["rows"]) and timing["rows"] >= 1
            and _is_int(timing["workers"]) and 1 <= timing["workers"] <= MAX_WORKERS):
        bad("planted_bugs_timing", "must be {seconds: >= 0, rows: >= 1, workers: 1 to 64} of the judged rows")
    reused_plants = data.get("plants_reused_from")
    if "plants_reused_from" in data and not (isinstance(reused_plants, list) and all(
            isinstance(item, dict) and set(item) == {"path", "digest"} and isinstance(item["path"], str)
            and item["path"] and isinstance(item["digest"], str) and _HEX64.match(item["digest"])
            for item in reused_plants)):
        bad("plants_reused_from", "must be a list of {path, digest: sha256} of the plants records of the reused record")
    if "plants_reused_from" in data and "reused_from" not in data:
        bad("plants_reused_from", "belongs to a record that reused another (reused_from)")
    children = data.get("children", [])
    if not isinstance(children, list) or not all(
            isinstance(child, dict) and set(child) == {"task_id", "kind", "digest", "drift"}
            and isinstance(child["task_id"], str) and child["kind"] in store.KINDS
            and isinstance(child["digest"], str) and _HEX64.match(child["digest"])
            and child["drift"] in ("fresh", "drifted") for child in children):
        bad("children", "must be a list of {task_id, kind: results or acceptance, digest: sha256, "
                        "drift: fresh or drifted}")
    return problems


def _deferred_problem(row: dict) -> str | None:
    """Why a deferred row (`deferred: true`) is not one the runner records, or None."""
    if set(row) != set(DEFERRED_KEYS):
        extra, missing = sorted(set(row) - set(DEFERRED_KEYS)), sorted(set(DEFERRED_KEYS) - set(row))
        return ("a deferred row holds its definition and no verdict: exactly " + ", ".join(DEFERRED_KEYS)
                + (f" (found {', '.join(extra)})" if extra else f" (missing {', '.join(missing)})"))
    if not isinstance(row["id"], str) or not row["id"] or row["id"] == SELF_TEST_ID:
        return "a deferred row needs a string id other than the self-test row's"
    planted = _planted_problem(row)
    if planted:
        return planted
    if not isinstance(row["find"], str) or not row["find"] or row["test_argv"] is None:
        return "a deferred row needs a non-empty find and a test_argv"
    if not isinstance(row["serial"], bool):
        return "serial must be true or false"
    if row["definition_sha256"] != definition_digest(row):
        return "definition_sha256 does not match the row's definition"
    return None


def is_gate(task_id) -> bool:
    """Whether a record of `task_id` is a gate record: its task is a gate (`G-<NAME>`, as in gates.yaml)."""
    return isinstance(task_id, str) and GATE_ID.match(task_id) is not None


def derived_output(path: str) -> bool:
    """Whether a hashed input is a derived output that no record may bind (rule 12)."""
    pure = PurePosixPath(path)
    if path.startswith("program/evidence/"):
        return pure.suffix not in _TOOLING
    return (pure.name in _RECORD_NAMES or path in _DERIVED_FILES
            or any(path.startswith(prefix) for prefix in _DERIVED_DIRS))


def lint_record(data, root=None) -> list[str]:
    """Why a results record makes the evidence graph cyclic (empty when it does not).

    One `binds a derived output: <path>` line per offending input. Only a gate record binds other records,
    by digest (`children`), and no record binds a gate record. `root` is accepted for symmetry with the other
    checks; the rules do not depend on the checkout.
    """
    if not isinstance(data, dict):
        return []
    inputs = store.input_digests(data.get("inputs"))
    problems = [f"binds a derived output: {path}" for path in sorted(inputs)
                if derived_output(path)] if inputs is not None else []
    children = data.get("children") or []
    if not isinstance(children, list):
        return problems
    if children and not is_gate(data.get("task_id")):
        problems.append(f"binds child records but {data.get('task_id')} is not a gate: only a gate record binds "
                        "other records")
    for child in children:
        if isinstance(child, dict) and is_gate(child.get("task_id")):
            problems.append(f"binds a gate record: {child['task_id']} (nothing may bind a gate record)")
    return problems


def validate_acceptance(data) -> list[str]:
    """Every problem found in an acceptance record (empty when valid)."""
    if not isinstance(data, dict):
        return ["acceptance must be a JSON object"]
    problems = [f"missing key: {key}" for key in required_keys("acceptance") if key not in data]
    if problems:
        return problems
    if not isinstance(data["task_id"], str) or not data["task_id"]:
        problems.append("task_id: must be a non-empty string")
    if data["decision"] not in DECISIONS:
        problems.append(f"decision: must be one of {', '.join(DECISIONS)}")
    if not isinstance(data["evidence_sha256"], str) or not _HEX64.match(data["evidence_sha256"]):
        problems.append("evidence_sha256: must be the 64-character digest of the results record it binds")
    if not isinstance(data["source_commit"], str) or not _HEX40.match(data["source_commit"]):
        problems.append("source_commit: must be a 40-character lowercase hex SHA")
    if not isinstance(data["accepted_at"], str) or not _STAMP.match(data["accepted_at"]):
        problems.append("accepted_at: must be UTC, YYYY-MM-DDTHH:MM:SSZ")
    if data["role"] != "qa":
        problems.append("role: must be qa")
    for key in ("commands", "planted_bugs", "findings", "blocked_by"):
        if not isinstance(data[key], list):
            problems.append(f"{key}: must be a list")
    return problems


PLANTS_KEYS = ("task_id", "kind", "part_of", "head", "kit_version", "produced_at", "workers", "seconds", "rejudge",
               "rows", "all_killed")  # a plants record (0.5.3)


def validate_plants(data) -> list[str]:
    """Every problem found in a `plants` record (empty when valid): the verdicts of deferred planted-bug rows that
    `pk gate plants` judged at the head of the results record it names in `part_of`."""
    if not isinstance(data, dict):
        return ["plants must be a JSON object"]
    problems = [f"missing key: {key}" for key in PLANTS_KEYS if key not in data]
    if problems:
        return problems
    extra = sorted(set(data) - set(PLANTS_KEYS))
    if extra:
        problems.append(f"unknown key: {', '.join(extra)}")
    if not isinstance(data["task_id"], str) or not data["task_id"]:
        problems.append("task_id: must be a non-empty string")
    if data["kind"] != store.PLANTS:
        problems.append("kind: must be plants")
    part_of = data["part_of"]
    if not (isinstance(part_of, dict) and set(part_of) == {"path", "digest"} and isinstance(part_of["path"], str)
            and isinstance(part_of["digest"], str) and _HEX64.match(part_of["digest"])):
        problems.append("part_of: must be {path, digest: sha256} of the results record whose deferred rows it judged")
    else:
        try:
            if store.check_rel(part_of["path"])[1] != "results":
                problems.append("part_of: path must name a results record")
        except KitError as exc:
            problems.append(f"part_of: {exc}")
    if not isinstance(data["head"], str) or not _HEX40.match(data["head"]):
        problems.append("head: must be a 40-character lowercase hex SHA")
    if not isinstance(data["kit_version"], str) or not data["kit_version"]:
        problems.append("kit_version: must be a non-empty string")
    if not isinstance(data["produced_at"], str) or not _STAMP.match(data["produced_at"]):
        problems.append("produced_at: must be UTC, YYYY-MM-DDTHH:MM:SSZ")
    if not (_is_int(data["workers"]) and 1 <= data["workers"] <= MAX_WORKERS):
        problems.append(f"workers: must be a whole number from 1 to {MAX_WORKERS}")
    seconds = data["seconds"]
    if not (isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0):
        problems.append("seconds: must be a number >= 0")
    if not isinstance(data["rejudge"], bool):
        problems.append("rejudge: must be true or false")
    rows = data["rows"]
    if not isinstance(rows, list) or not rows:
        problems.append("rows: must be a non-empty list")
        rows = []
    seen, killed = set(), []
    for index, row in enumerate(rows):
        where = f"rows[{index}]"
        if not (isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] and row["id"] != SELF_TEST_ID
                and isinstance(row.get("builds"), bool) and isinstance(row.get("killed"), bool)):
            problems.append(f"{where}: needs a string id (not the self-test row's) and boolean builds and killed")
            continue
        if row["id"] in seen:
            problems.append(f"{where}: repeats id {row['id']}")
        seen.add(row["id"])
        missing = [key for key in (*ROW_KEYS, *PLANTED_KEYS, "serial", "definition_sha256") if key not in row]
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)}")
            continue
        if row["killed"] and not row["builds"]:
            problems.append(f"{where}: killed although it does not build")
        expected = "build failed" if not row["builds"] else "test failed" if row["killed"] else "not killed"
        if row["kill_reason"] != expected:
            problems.append(f"{where}: kill_reason must be '{expected}' (found {row['kill_reason']!r})")
        digest = row["test_output_sha256"]
        if not isinstance(digest, str) or not (digest == "" or _HEX64.match(digest)):
            problems.append(f"{where}: test_output_sha256 must be 64 lowercase hex characters, or empty")
        planted = _planted_problem(row)
        if planted:
            problems.append(f"{where}: {planted}")
        elif not isinstance(row["serial"], bool) or row["definition_sha256"] != definition_digest(row):
            problems.append(f"{where}: definition_sha256 does not match the row's definition")
        if "judged" in row and row["judged"] not in JUDGED:
            problems.append(f"{where}: judged must be one of {', '.join(JUDGED)}")
        killed.append(row["killed"])
    if isinstance(data["all_killed"], bool) and data["all_killed"] != all(killed):
        problems.append("all_killed: does not match the rows")
    return problems


def plants_chain(root, entry: dict, data: dict) -> dict[str, str]:
    """{results digest: head} of the record `entry` names and every record it reused, in turn (`reused_from`).

    A plants record bound to any of them counts for the record: a reuse copies the planted-bug rows of a record
    whose code is byte-identical (0.5.2), so a verdict judged on that code holds for the new record too (0.5.3)."""
    chain = {entry["digest"]: data.get("head")}
    current = data
    while isinstance(current.get("reused_from"), dict) and len(chain) < 1000:
        source = current["reused_from"]
        if source.get("digest") in chain:
            break
        current = store.load(root, source["path"], source["digest"])
        chain[source["digest"]] = current.get("head")
    return chain


def plants_coverage(root, task_id: str, entry: dict, data: dict) -> dict:
    """How far the `plants` records of the checkout at `root` cover the deferred rows of a results record.

    {deferred: [ids], killed: {id: plants path}, survived: {id: (plants path, kill_reason)}, pending: [ids],
    problems: [...]}. A row counts as killed when a plants record listed in the task's pointer, bound by `part_of`
    to the record (or a record it reused), judged at that record's head with the row's definition, says killed.
    `pending` lists the deferred rows with no killed verdict, in table order.
    """
    deferred = {row["id"]: row["definition_sha256"] for row in data.get("planted_bugs", []) if is_deferred(row)}
    found = {"deferred": list(deferred), "killed": {}, "survived": {}, "pending": [], "problems": []}
    if not deferred:
        return found
    try:
        chain = plants_chain(root, entry, data)
        items = store.read_plants(root, task_id)
    except KitError as exc:
        found["problems"].append(" ".join(str(exc).split()))
        items, chain = [], {}
    for item in items:
        if item["part_of"] not in chain:
            continue
        try:
            record = store.load(root, item["path"], item["digest"])
        except KitError as exc:
            found["problems"].append(" ".join(str(exc).split()))
            continue
        problems = validate_plants(record)
        if problems:
            found["problems"].append(f"{item['path']}: not a valid plants record: {problems[0]}")
            continue
        if record["task_id"] != task_id or record["part_of"]["digest"] != item["part_of"]:
            found["problems"].append(f"{item['path']}: the pointer's entry does not match the record")
            continue
        if record["head"] != chain[item["part_of"]]:
            found["problems"].append(f"{item['path']}: judged at {record['head']}, not at {chain[item['part_of']]}, "
                                     "the head of the record it is bound to")
            continue
        for row in record["rows"]:
            if row["id"] not in deferred:
                continue
            if row["definition_sha256"] != deferred[row["id"]]:
                found["problems"].append(f"{item['path']}: row {row['id']} judged another definition than the "
                                         "deferred row")
            elif row["killed"]:
                found["killed"].setdefault(row["id"], item["path"])
            else:
                found["survived"][row["id"]] = (item["path"], row["kill_reason"])
    found["survived"] = {rid: why for rid, why in found["survived"].items() if rid not in found["killed"]}
    found["pending"] = [rid for rid in deferred if rid not in found["killed"]]
    return found


def _listed(ids: list[str], shown: int = 20) -> str:
    return ", ".join(ids[:shown]) + (f" and {len(ids) - shown} more" if len(ids) > shown else "")


def coverage_problem(root, task_id: str, entry: dict, data: dict) -> str | None:
    """Why the deferred planted-bug rows of the results record `entry` are not all killed in `plants` records, naming
    the missing and surviving rows and the command that judges them; None when complete (or none was deferred)."""
    found = plants_coverage(root, task_id, entry, data)
    if found["problems"]:
        return f"{task_id}: the plants records cannot be read: {found['problems'][0]}"
    if not found["pending"]:
        return None
    survived = [rid for rid in found["pending"] if rid in found["survived"]]
    missing = [rid for rid in found["pending"] if rid not in found["survived"]]
    parts = []
    if missing:
        parts.append(f"not judged yet: {_listed(missing)}")
    if survived:
        parts.append("judged and not killed: " + _listed([f"{rid} ({found['survived'][rid][1]})" for rid in survived]))
    command = f"pk gate plants {task_id} --branch task/{task_id}"
    how = [f"judge them with: {command} --all-pending"] if missing else []
    if survived:
        how.append(f"after a fix, the gate writes a new record; to judge a row again at this head: {command} --rows "
                   f"{','.join(survived[:20])} --rejudge")
    return (f"{task_id}: {len(found['pending'])} of {len(found['deferred'])} deferred planted-bug rows of "
            f"{entry['path']} have no killed verdict in a plants record bound to it ({'; '.join(parts)}); "
            + "; ".join(how))


def gate_problems(data, task_id: str, head: str | None = None, root=None) -> list[str]:
    """Why a results record does not pass the hand-back gate (empty when it does); `head` None skips the head check.

    On top of `validate_results`: the record is this task's, bound to `head`, every command was as expected,
    it holds at least one command, every planted bug was killed, every planted-bug row says what was
    planted and which commands judged it, and the lint finds no derived output among its inputs. The binding
    check and `pk status set ... done` apply the same rules.
    """
    problems = validate_results(data)
    if problems:
        return problems
    problems.extend(lint_record(data, root))
    if data["task_id"] != task_id:
        problems.append(f"task_id: is {data['task_id']}, expected {task_id}")
    if head is not None and data["head"] != head:
        problems.append(f"head: is {data['head']}, expected the worktree HEAD {head}")
    if data["all_commands_as_expected"] is not True:
        problems.append("all_commands_as_expected: is not true")
    if data["planted_bugs_all_killed"] is not True:
        problems.append("planted_bugs_all_killed: is not true (a planted bug survived)")
    if not data["commands"]:
        problems.append("commands: is empty (evidence must record the commands that were run)")
    for index, row in enumerate(data["planted_bugs"]):
        missing = [key for key in PLANTED_KEYS if key not in row]
        if missing:
            problems.append(f"planted_bugs[{index}]: does not record {', '.join(missing)} (what was planted and "
                            "the commands that ran)")
    return problems


# --- command -----------------------------------------------------------------------

def _store_kind(path: Path) -> str | None:
    """`results`, `acceptance` or `plants` for a file named like a record in a directory of that kind, else None."""
    if store._NAME.match(path.name) and path.parent.name in store.RECORD_KINDS:
        return path.parent.name
    return None


def _errors(problems: list[str]) -> int:
    print("\n".join("error: " + " ".join(problem.split()) for problem in problems), file=sys.stderr)
    return 1


def _root_or_none():
    try:
        return find_root()
    except KitError:
        return None


def _validate(args) -> int:
    if args.record is None:  # the whole evidence tree: names, history and pointers
        root = find_root()
        problems, pending = store.check(root, args.base, schema=True)
        for path, _ in pending:
            print(f"warning: record pending commit: {path}", file=sys.stderr)
        if problems:
            return _errors(problems)
        print("evidence intact" + (f" since {args.base}" if args.base else ""))
        return 0
    path = Path(args.record)
    kind = _store_kind(path)
    if kind is not None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise KitError(f"cannot read {path}: {exc.strerror or exc}") from exc
        problem = store.altered(path.name, raw)
        if problem:
            return _errors([problem.replace(path.name, str(path), 1)])
    data = read_json(path)
    problems = {"acceptance": validate_acceptance, store.PLANTS: validate_plants}.get(kind, validate_results)(data)
    if problems:
        return _errors(problems)
    print(f"evidence valid: {path}")
    return 0


def _lint(args) -> int:
    path = Path(args.record)
    problems = lint_record(read_json(path), _root_or_none())
    if problems:
        return _errors(problems)
    print(f"lint: {path} binds only inputs")
    return 0


def _history(args) -> int:
    rows = store.history(find_root(), args.task_id)
    if not rows:
        print(f"{args.task_id}: no records under program/evidence/{args.task_id}/")
        return 0
    for row in rows:
        if row["kind"] == store.PLANTS:  # every plants record counts; the pointer lists each one
            state = "listed" if row["current"] else "not listed"
        else:
            state = "current" if row["current"] else "superseded"
        when = ("accepted_at " if row["kind"] == "acceptance" else "produced_at ") + (row["at"] or "-")
        fields = [state, row["kind"], row["digest"], f"head {row['head'] or '-'}", when]
        if row["kind"] == "acceptance":
            fields.append(f"decision {row['decision'] or '-'}")
        if row["altered"]:
            fields.append(row["altered"])
        print("\t".join(fields))
    return 0


def accept(root, task_id: str, data) -> dict:
    """Append an acceptance record for `task_id` and point the task's pointer at it; refuse one that does not bind the
    latest results record (by digest and by its head). A re-acceptance after a refresh appends again; nothing is
    rewritten. Returns the append result."""
    problems = validate_acceptance(data)
    if problems:
        raise KitError("; ".join(problems))
    if data["task_id"] != task_id:
        raise KitError(f"acceptance names task {data['task_id']}, not {task_id}")
    found = store.load_latest(root, task_id, "results")
    if found is None:
        raise KitError(f"{task_id} has no results record in {store.pointer_path(task_id)}")
    entry, record = found
    if data["evidence_sha256"] != entry["digest"]:
        raise KitError(f"acceptance does not bind the latest results record: it binds {data['evidence_sha256']}, "
                       f"the latest is {entry['digest']}")
    if data["source_commit"] != record.get("head"):
        raise KitError(f"acceptance source_commit {data['source_commit']} is not {record.get('head')}, the head "
                       "the latest results record names")
    if data["decision"] == "accepted_development":
        import latework  # late: keeps the module graph flat

        problem = coverage_problem(root, task_id, entry, record) \
            or latework.early_problem(root, task_id, str(record.get("head")), "pk evidence accept")
        if problem:
            raise KitError(problem)
    return store.record(root, task_id, "acceptance", data)


def _accept(args) -> int:
    root = find_root()
    appended = accept(root, args.task_id, read_json(Path(args.acceptance)))
    print(f"acceptance {appended['digest']} appended: {appended['path']}")
    return 0


def import_legacy(root) -> tuple[list[str], list[str]]:
    """Copy every 0.1 `program/evidence/<ID>/results.json` and `acceptance.json` into the append-only layout
    (`program/evidence/<ID>/results/` and `acceptance/`), byte for byte, as the first history entry, and point the
    task's pointer at them; ([lines describing what was imported], [paths to git rm]).

    A record the pointer already names (same digest) is not copied again. Every file is read and checked
    before anything is appended. The originals are never deleted.
    """
    root = Path(root)
    index = store.read_index(root)
    base = root / "program" / "evidence"
    plan, legacy = [], []
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        if not folder.is_dir():
            continue
        for kind in store.KINDS:
            source = folder / f"{kind}.json"
            if not source.is_file():
                continue
            rel = source.relative_to(root).as_posix()
            legacy.append(rel)
            store.check_task(folder.name)
            raw = source.read_bytes()
            data = read_json(source)
            if not isinstance(data, dict) or data.get("task_id") != folder.name:
                raise KitError(f"{rel}: not a {kind} record of task {folder.name}; nothing was imported")
            current = store.latest(index, folder.name, kind)
            if current is not None and current["digest"] == sha256_bytes(raw):
                continue
            if current is not None:
                raise KitError(f"{rel}: {store.pointer_path(folder.name)} already names another {kind} record; "
                               "nothing was imported")
            plan.append((folder.name, kind, raw, rel))
    done = []
    for task_id, kind, raw, rel in plan:
        appended = store.record(root, task_id, kind, raw)
        done.append(f"imported {rel} as {appended['path']}")
    return done, legacy


def _import(args) -> int:
    root = find_root()
    done, legacy = import_legacy(root)
    for line in done:
        print(line)
    print(f"{len(done)} record(s) imported; commit them with each task's {store.POINTER}")
    if legacy:
        print("the originals stay; remove them from the repository by exact path:")
        for rel in legacy:
            print(f"git rm {rel}")
    return 0


def _init(args) -> int:
    find_root()
    print(f"evidence: nothing to create; program/evidence/<ID>/{store.POINTER} appears with each task's first record")
    return 0


ACTIONS = {"init": _init, "validate": _validate, "lint": _lint, "history": _history, "accept": _accept,
           "import": _import}


def cmd_evidence(args: argparse.Namespace) -> int:
    action = ACTIONS.get(getattr(args, "evidence_action", None))
    if action is None:
        raise KitError("evidence needs an action: " + ", ".join(ACTIONS))
    return action(args)


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="evidence_action", metavar="action", parser_class=type(parser))
    actions.add_parser("init", help="nothing to create: a task's pointer appears with its first record")
    checker = actions.add_parser("validate", help="check one record against its schema and invariants (and its "
                                                  "name), or, with no record, every record, the history and the pointers")
    checker.add_argument("record", metavar="record.json", nargs="?", help="path to one record")
    checker.add_argument("--base", metavar="REF", help="check the history from REF only (default: all of it)")
    linter = actions.add_parser("lint", help="refuse a results record that binds a derived output")
    linter.add_argument("record", metavar="results.json", help="path to the results record")
    lister = actions.add_parser("history", help="every record of a task, newest first")
    lister.add_argument("task_id", metavar="ID")
    accepter = actions.add_parser("accept", help="append QA's acceptance record (it must bind the latest results)")
    accepter.add_argument("task_id", metavar="ID")
    accepter.add_argument("acceptance", metavar="acceptance.json", help="the acceptance QA wrote, outside the repo")
    actions.add_parser("import", help="copy 0.1 program/evidence/<ID>/*.json into the append-only layout and build "
                                      "the pointers")


def register(add_command) -> None:
    add_command("evidence", cmd_evidence, help="evidence records: " + ", ".join(ACTIONS), configure=_configure)
