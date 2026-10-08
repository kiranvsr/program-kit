#!/usr/bin/env python3
"""The evidence kit: commit-bound evidence records and the planted-bug runner (standard library only).

`Evidence(root, task_id)` records HEAD, the main branch and their merge-base, hashes inputs from
the committed blobs (never the working copy), runs commands as argv lists and records what they
really did, runs planted bugs on a throwaway mirror of HEAD, and appends a results record to the
task's committed, append-only record directory (`store.py`) only when every honesty precondition holds; the
pointer index `program/evidence/index.json` then names it as the task's latest record, and the record it replaces
stays as history (`supersedes` names it). A record is a fingerprint: at commit `head` these inputs produced this
outcome. A later change never makes it stale; it only drifts from the current tree (`pk stale`). `pk evidence validate <record>` re-checks the invariants;
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
- A record edited consistently by hand, renamed to its new digest and re-indexed, still validates;
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
_TIMED_OUT = 124
_NO_RUN = (_TIMED_OUT, 127)  # timeout, program not found: the shell conventions run_argv uses

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*\Z")
_TOOLING = (".py", ".sh", ".toml")  # what may be bound under program/evidence/: produce.py and its helpers
_DERIVED_DIRS = ("program/evidence/", "program/claims/", "program/status/", "program/approvals/")
_DERIVED_FILES = ("program/controller-state.json", "program/progress.md", "program/HANDOFF.md",
                  "program/ledger.yaml", "program/changes/register-log.md")
_RECORD_NAMES = ("results.json", "acceptance.json")
DECISIONS = ("accepted_development", "rejected")


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


class Evidence:
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
        self.stale_result: dict = {"checked": False, "records": []}
        self.blocked_by: list[str] = []
        self.deviations: list[str] = []
        self.children: list[dict] = []  # a gate record's child records, by digest

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
            rows.append({"id": rid, "path": path, "find": find, "replace": replace, "test_args": args})
        return rows

    def planted_bugs(self, table: list[dict], build: list[str] | None, test: list[str],
                     timeout: int | None = None) -> list[dict]:
        """Apply each planted bug to a mirror of HEAD, build, run the test; return one row per bug.

        Row: `{id, builds, killed, kill_reason, test_output_sha256, path, find, replace, build_argv,
        test_argv}`: the last five say what was planted (the file and the edit) and which build and test
        argv judged it. The mirror is `git worktree add --detach` of HEAD in a fresh temp dir and is
        always removed; the working tree is never edited. A table row is
        `{id, path, find, replace, test_args}`; `test` is the
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
        """
        rows = self._check_table(table)
        test = _argv(test, "test")
        build = _argv(build, "build") if build is not None else None
        mirror = Path(tempfile.mkdtemp(prefix="pk-mirror-"))
        added = []  # non-empty once git has registered the mirror
        self._mirror = mirror

        def run() -> list[dict]:
            git(self.root, "worktree", "add", "--detach", str(mirror), "HEAD")
            added.append(mirror)
            return self._run_rows(mirror, rows, build, test, timeout)

        try:
            results = _then(run, lambda: self._remove_mirror(mirror, bool(added)),
                            lambda found: f"{len(found)} planted-bug rows ran")
        finally:
            self._mirror = None
        self.planted_bug_rows = results
        return results

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
        except (KitError, OSError) as exc:
            return f"the mirror could not be restored: {exc}"
        return None

    def _attempt(self, mirror: Path, rid: str, path: str, edit: Callable[[bytes], bytes], build: list[str] | None,
                 argv: list[str], timeout: int | None) -> tuple[bool, int | None, str]:
        """Edit `path` in the mirror, build, test, restore. Returns (builds, test exit or None, test hash)."""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        target = mirror / path

        def run() -> tuple[bool, int | None, str]:
            target.write_bytes(edit(target.read_bytes()))
            _clear_bytecode(mirror)
            if build is not None:
                code, _ = run_argv(build, mirror, env=env, timeout=timeout)
                if code != 0:
                    return False, None, ""
            code, output = run_argv(argv, mirror, env=env, timeout=timeout)
            builds = code not in _NO_RUN  # a timeout or a missing program is never a kill
            return builds, code, sha256_bytes(output.encode("utf-8"))

        def gave(result) -> str:
            return (f"planted bug {rid} gave builds: {str(result[0]).lower()}, "
                    f"test exit: {'not run' if result[1] is None else result[1]}")

        return _then(run, lambda: self._restore_problem(mirror), gave)

    def _baseline(self, mirror: Path, build: list[str] | None, argv: list[str], rid: str,
                  timeout: int | None) -> None:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

        def run() -> None:
            if build is not None:
                code, _ = run_argv(build, mirror, env=env, timeout=timeout)
                if code != 0:
                    raise KitError(f"unmutated mirror does not build (exit {code}); fix the build before planting "
                                   f"bugs (row {rid})")
            code, _ = run_argv(argv, mirror, env=env, timeout=timeout)
            if code != 0:
                raise KitError(f"unmutated mirror fails its own test (exit {code}); a failing test kills "
                               f"nothing (row {rid})")

        _then(run, lambda: self._restore_problem(mirror), lambda _: f"unmutated mirror passes its test (row {rid})")

    def _run_rows(self, mirror: Path, rows: list[dict], build: list[str] | None, test: list[str],
                  timeout: int | None) -> list[dict]:
        results, checked = [], set()
        for item in rows:
            argv = test + item["test_args"]
            if tuple(argv) not in checked:
                self._baseline(mirror, build, argv, item["id"], timeout)
                checked.add(tuple(argv))

            def plant(data: bytes, item=item) -> bytes:
                return data.decode("utf-8").replace(item["find"], item["replace"]).encode("utf-8")

            builds, code, digest = self._attempt(mirror, item["id"], item["path"], plant, build, argv, timeout)
            if not builds:
                killed, reason = False, "build failed"
            elif code != 0:
                killed, reason = True, "test failed"
            else:
                killed, reason = False, "not killed"
            results.append({"id": item["id"], "builds": builds, "killed": killed, "kill_reason": reason,
                            "test_output_sha256": digest,
                            **self._planted(item["path"], item["find"], item["replace"], build, argv)})
        results.append(self._self_test(mirror, rows[0], build, test + rows[0]["test_args"], timeout))
        return results

    @staticmethod
    def _planted(path: str, find: str, replace: str, build: list[str] | None, argv: list[str]) -> dict:
        """The PLANTED_KEYS of a row: the file, the edit, and the build and test argv that judged it."""
        return {"path": path, "find": find, "replace": replace,
                "build_argv": list(build) if build is not None else None, "test_argv": list(argv)}

    def _self_test(self, mirror: Path, first: dict, build: list[str] | None, argv: list[str],
                   timeout: int | None) -> dict:
        def break_file(data: bytes) -> bytes:
            lead = b"" if not data or data.endswith(b"\n") else b"\n"
            return data + lead + BROKEN_LINE.encode() + b"\n"

        builds, code, digest = self._attempt(mirror, SELF_TEST_ID, first["path"], break_file, build, argv, timeout)
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

        Each child must have a results record in the index; its acceptance is bound when there is one. A record
        whose bytes no longer match the index is refused. Each row records the child's drift at HEAD: `fresh` when
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
                raise KitError(f"{task_id} has no results record in {store.INDEX}")
            entry = store.latest(index, task_id, "results")
            inputs = store.load(self.root, entry["path"], entry["digest"]).get("inputs")
            changed = stale_module.changed_inputs(self.root, self.head, inputs if isinstance(inputs, dict) else {})
            drift = "drifted" if changed or not isinstance(inputs, dict) else "fresh"
            for kind in store.KINDS:
                entry = store.latest(index, task_id, kind)
                if entry is not None:
                    store.load(self.root, entry["path"], entry["digest"])
                    added.append({"task_id": task_id, "kind": kind, "digest": entry["digest"], "drift": drift})
        self.children = [*self.children, *added]
        return added

    # --- stale -----------------------------------------------------------------

    def stale(self, main: str | None = None) -> dict:
        """List the latest records whose hashed inputs drifted from `main` (delegates to stale.py) and remember
        the result. Drift is information: a drifted record is still a true record of what it verified."""
        try:
            import stale as stale_module  # lazy: stale.py arrives with the stale-check task
        except ModuleNotFoundError as exc:
            if exc.name not in (None, "stale"):
                raise KitError(f"stale check unavailable: {exc}") from exc
            raise KitError("stale check unavailable: kit/scripts/stale.py is missing") from exc
        except ImportError as exc:
            raise KitError(f"stale check unavailable: {exc}") from exc
        report = stale_module.drift_report(self.root, main or self.main)
        self.stale_result = {"checked": True,
                             "records": [row for row in report.get("tasks", []) if row.get("state") == "drifted"],
                             "unreadable": list(report.get("unreadable", []))}
        return self.stale_result

    # --- write -----------------------------------------------------------------

    def write(self, path=None) -> dict:
        """Append the results record to `program/evidence/<task>/results/` and point the index at it; return
        {digest, path (relative to the root), file}. Raise KitError listing every reason it refused (nothing is
        appended and the index is untouched).

        The checks run before anything is written, so updating the index does not count as dirtying the tree.
        The record names the task's previous latest results record as `supersedes` (null for the first one).
        """
        if path is not None:
            raise KitError("Evidence.write takes no path since program-kit 0.2.0: the record is appended to "
                           "program/evidence/<ID>/results/ and program/evidence/index.json points at it")
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
            entry = store.latest(store.read_index(self.root), self.task_id, "results")
            previous = entry["digest"] if entry is not None else None
        except KitError as exc:
            reasons.append(str(exc))
        if reasons:
            raise KitError("; ".join(reasons))
        data = {
            "task_id": self.task_id, "head": self.head, "main": self.main_sha, "merge_base": self.merge_base,
            "produced_at": utc_now(), "inputs": dict(self.inputs), "commands": list(self.commands),
            "planted_bugs": list(self.planted_bug_rows),
            "planted_bugs_all_killed": all(row["killed"] for row in bugs),
            "all_commands_as_expected": all(cmd["as_expected"] for cmd in self.commands),
            "tree_unchanged_by_run": True, "stale": dict(self.stale_result),
            "blocked_by": list(self.blocked_by), "deviations": list(self.deviations),
            "supersedes": previous,
        }
        if self.children:
            data["children"] = [dict(child) for child in self.children]
        # last line of defence: never append what validate or the lint would refuse
        problems = validate_results(data) + lint_record(data, self.root)
        if problems:
            raise KitError("; ".join(problems))
        appended = store.record(self.root, self.task_id, "results", data)
        return {key: appended[key] for key in ("digest", "path", "file")}


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
    inputs = data["inputs"]
    if not isinstance(inputs, dict) or not inputs:
        bad("inputs", "must be a non-empty mapping of path to sha256")
    elif not all(isinstance(k, str) and isinstance(v, str) and _HEX64.match(v) for k, v in inputs.items()):
        bad("inputs", "every value must be a 64-character lowercase hex sha256")
    for key in ("planted_bugs_all_killed", "all_commands_as_expected", "tree_unchanged_by_run"):
        if not isinstance(data[key], bool):
            bad(key, "must be true or false")
    for key in ("blocked_by", "deviations"):
        if not _str_list(data[key]):
            bad(key, "must be a list of strings")
    stale = data["stale"]
    if not (isinstance(stale, dict) and isinstance(stale.get("checked"), bool)
            and isinstance(stale.get("records"), list)):
        bad("stale", "must be {checked: bool, records: list}")

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
        shaped.append(row)
    selfs = [row for row in shaped if row["id"] == SELF_TEST_ID]
    bugs = [row for row in shaped if row["id"] != SELF_TEST_ID]
    if len(selfs) != 1:
        bad("planted_bugs", f"needs exactly one {SELF_TEST_ID} row (found {len(selfs)})")
    elif selfs[0]["builds"] or selfs[0]["killed"]:
        bad("planted_bugs", f"the {SELF_TEST_ID} row must have builds: false and killed: false")
    if not bugs:
        bad("planted_bugs", "needs at least one planted-bug row besides the self-test row")
    if isinstance(data["planted_bugs_all_killed"], bool) and data["planted_bugs_all_killed"] != all(
            row["killed"] for row in bugs):
        bad("planted_bugs_all_killed", "does not match the planted-bug rows (self-test row excluded)")
    if data["tree_unchanged_by_run"] is False:
        bad("tree_unchanged_by_run", "is false: the run changed the tree, so this is not valid evidence")
    supersedes = data.get("supersedes")
    if supersedes is not None and not (isinstance(supersedes, str) and _HEX64.match(supersedes)):
        bad("supersedes", "must be the 64-character sha256 of the record it replaces, or null")
    children = data.get("children", [])
    if not isinstance(children, list) or not all(
            isinstance(child, dict) and set(child) == {"task_id", "kind", "digest", "drift"}
            and isinstance(child["task_id"], str) and child["kind"] in store.KINDS
            and isinstance(child["digest"], str) and _HEX64.match(child["digest"])
            and child["drift"] in ("fresh", "drifted") for child in children):
        bad("children", "must be a list of {task_id, kind: results or acceptance, digest: sha256, "
                        "drift: fresh or drifted}")
    return problems


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
    inputs = data.get("inputs")
    problems = [f"binds a derived output: {path}" for path in sorted(inputs)
                if derived_output(path)] if isinstance(inputs, dict) else []
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
    """`results` or `acceptance` for a file named like a record in a directory of that kind, else None."""
    if store._NAME.match(path.name) and path.parent.name in store.KINDS:
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
    if args.record is None:  # the whole evidence tree: names, history and index
        root = find_root()
        problems = store.integrity(root, args.base)
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
    problems = validate_acceptance(data) if kind == "acceptance" else validate_results(data)
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
        print(f"{args.task_id}: no records in the evidence store")
        return 0
    for row in rows:
        state = "current" if row["current"] else "superseded"
        when = ("produced_at " if row["kind"] == "results" else "accepted_at ") + (row["at"] or "-")
        fields = [state, row["kind"], row["digest"], f"head {row['head'] or '-'}", when]
        if row["kind"] == "acceptance":
            fields.append(f"decision {row['decision'] or '-'}")
        if row["altered"]:
            fields.append(row["altered"])
        print("\t".join(fields))
    return 0


def accept(root, task_id: str, data) -> dict:
    """Append an acceptance record for `task_id` and point the index at it; refuse one that does not bind the
    latest results record (by digest and by its head). A re-acceptance after a refresh appends again; nothing is
    rewritten. Returns the append result."""
    problems = validate_acceptance(data)
    if problems:
        raise KitError("; ".join(problems))
    if data["task_id"] != task_id:
        raise KitError(f"acceptance names task {data['task_id']}, not {task_id}")
    found = store.load_latest(root, task_id, "results")
    if found is None:
        raise KitError(f"{task_id} has no results record in {store.INDEX}")
    entry, record = found
    if data["evidence_sha256"] != entry["digest"]:
        raise KitError(f"acceptance does not bind the latest results record: it binds {data['evidence_sha256']}, "
                       f"the latest is {entry['digest']}")
    if data["source_commit"] != record.get("head"):
        raise KitError(f"acceptance source_commit {data['source_commit']} is not {record.get('head')}, the head "
                       "the latest results record names")
    return store.record(root, task_id, "acceptance", data)


def _accept(args) -> int:
    root = find_root()
    appended = accept(root, args.task_id, read_json(Path(args.acceptance)))
    print(f"acceptance {appended['digest']} appended: {appended['path']}")
    return 0


def import_legacy(root) -> tuple[list[str], list[str]]:
    """Copy every 0.1 `program/evidence/<ID>/results.json` and `acceptance.json` into the append-only layout
    (`program/evidence/<ID>/results/` and `acceptance/`), byte for byte, as the first history entry, and point the
    index at them; ([lines describing what was imported], [paths to git rm]).

    A record the index already points at (same digest) is not copied again. Every file is read and checked
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
                raise KitError(f"{rel}: the index already points at another {kind} record of {folder.name}; "
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
    print(f"{len(done)} record(s) imported; commit them with {store.INDEX}")
    if legacy:
        print("the originals stay; remove them from the repository by exact path:")
        for rel in legacy:
            print(f"git rm {rel}")
    return 0


def _init(args) -> int:
    path, created = store.init(find_root())
    print(f"evidence index {'created' if created else 'exists'}: {path}")
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
    actions.add_parser("init", help="create program/evidence/index.json when it is absent")
    checker = actions.add_parser("validate", help="check one record against its schema and invariants (and its "
                                                  "name), or, with no record, every record, the history and the index")
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
                                      "the index")


def register(add_command) -> None:
    add_command("evidence", cmd_evidence, help="evidence records: " + ", ".join(ACTIONS), configure=_configure)
