#!/usr/bin/env python3
"""The gate runner: hand-back gate, merge check and gate claim documents (standard library only).

`handback` is the gate an implementer passes before independent QA; `merge_check` is the
controller's check from main; `claims` and `validate_claims` keep `program/claims/<GATE>/*.json`
equal to a recomputation from `gates.yaml`, each record's `acceptance.json` and `environments.yaml`.

Run it from the main checkout: the task worktree defaults to `<root>/.worktrees/<id lowercased>`.
`checks.yaml` (with its guarded files), the `Guarded edit:` lines in the task's SPEC and the PLAN commands
are all read from the main branch (`git show <main>:...`), so a task branch or a dirty main checkout
cannot change the rules it is judged by; the task's PLAN.md must be byte-identical to main's.
The trial merge happens in a temporary worktree of main and never commits or touches the main checkout.
`binding` is QA's check that a record's `head` still binds the branch tip, read from git objects only.

Limits:
- A produce.py that hand-writes a well-formed results.json is not detectable by the gate; QA re-runs the
  evidence independently and uses `pk gate binding`.
- Files written to git-ignored paths are invisible to the clean-tree checks.
- Projects must git-ignore the bytecode and build output their commands write: the evidence step
  needs a clean tree, and a leftover untracked file fails it.
- Evidence is bound to the commit it was produced at, and the produced `results.json` is left
  uncommitted in the task worktree (the only change the gate allows). Delete or commit it before a re-run.
- The task's own record shows up in the `stale:` list whenever the branch changed an input of it.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

import evidence as evidence_module
import stale as stale_module
import miniyaml
from common import KitError, find_root, git, read_json, read_yaml, run_argv, sha256_file, write_json
from register import GATE_ID

CHECK_KEYS = ("build", "test", "scan", "compile_check", "extra_gate")
ALL_KEYS = CHECK_KEYS + ("guarded_files",)
STEP_TIMEOUT = 3600  # seconds per command
TAIL_LINES = 20
TAIL_PREFIX = "    | "
PLAN_SECTION = "## Exact Commands"
NOT_EVALUATED, EVIDENCED, BLOCKED = "not_evaluated", "evidenced", "blocked"

_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*\Z")
_EXPECT = re.compile(r"\s+#\s+expect\s+(\d+)\s*\Z")
_GUARDED = re.compile(r"Guarded edit:\s*(\S.*?)\s*\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")  # every line break and terminal control character
_MISSING = object()


def _escape(match) -> str:
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def _show(value) -> str:
    """`value` as text with every control character (any line break included) rendered as a visible escape.

    Every value the gate interpolates into output or an error message passes through here, so nothing a branch,
    a file name or a command controls can start a new line.
    """
    return _CONTROL.sub(_escape, str(value))


def _out(text, stream=None) -> None:
    """The only way the gate writes a line: control characters are escaped and a line that would read
    as an `OK:` line is prefixed with `| `. The two real `OK:` lines come from `_print_ok` alone."""
    line = _show(text)
    if line.startswith("OK: "):
        line = "| " + line
    print(line, file=stream if stream is not None else sys.stdout)


def _print_ok(kind: str, branch, sha) -> None:
    """Print the final `OK:` line: nothing but a validated branch name and a 40-hex sha."""
    ending = {"handback": "passes the hand-back gate", "merge": "merges into main"}[kind]
    if not (isinstance(branch, str) and _REF.match(branch) and isinstance(sha, str) and _HEX40.match(sha)):
        raise KitError("refusing to print an OK line for an unvalidated branch or sha")
    print(f"OK: {branch} {ending} at {sha}")


# --- checks.yaml -------------------------------------------------------------------

def _argv_lists(value, key: str) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise KitError(f"checks.yaml: {_show(key)} must be a list of argv lists")
    commands = []
    for item in value:
        if not isinstance(item, list) or not item or not all(isinstance(part, str) and part for part in item):
            raise KitError(f"checks.yaml: every {_show(key)} command must be a non-empty argv list of strings, "
                           "not a shell string")
        commands.append(list(item))
    return commands


def _parse_checks(data, require: tuple[str, ...]) -> dict:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise KitError("checks.yaml: expected a mapping")
    unknown = sorted(str(key) for key in data if key not in ALL_KEYS)
    if unknown:
        raise KitError(f"checks.yaml: unknown key {_show(', '.join(unknown))} (expected {', '.join(ALL_KEYS)})")
    checks: dict = {key: _argv_lists(data.get(key), key) for key in CHECK_KEYS}
    guarded = data.get("guarded_files")
    if guarded is None:
        guarded = []
    if not isinstance(guarded, list) or not all(isinstance(path, str) and path for path in guarded):
        raise KitError("checks.yaml: guarded_files must be a list of paths")
    checks["guarded_files"] = list(guarded)
    for key in require:
        if not checks[key]:
            raise KitError(f"checks.yaml: {key} must list at least one command")
    return checks


def load_checks(root, require: tuple[str, ...] = ()) -> dict:
    """`program/checks.yaml` from the working tree as {build, test, scan, compile_check, extra_gate: argv lists;
    guarded_files: [str]}.

    Keys named in `require` must list at least one command: a gate that runs nothing proves nothing.
    """
    return _parse_checks(read_yaml(Path(root) / "program" / "checks.yaml"), require)


def load_checks_at(root, ref: str, require: tuple[str, ...] = ()) -> dict:
    """`program/checks.yaml` as committed at `ref`; the gates use this so the working tree cannot change the rules."""
    try:
        text = _git_bytes(Path(root), "show", f"{ref}:program/checks.yaml").decode("utf-8")
    except (KitError, UnicodeDecodeError) as exc:
        raise KitError(f"program/checks.yaml cannot be read on {_show(ref)}: {_show(exc)}") from exc
    try:
        data = miniyaml.loads(text)
    except miniyaml.YAMLError as exc:
        raise miniyaml.YAMLError(f"program/checks.yaml on {ref}: {exc}") from exc
    return _parse_checks(data, require)


# --- PLAN commands -----------------------------------------------------------------

def parse_plan(text: str) -> list[tuple[list[str], int]]:
    """(argv, expected exit) for each `$ ` line under `## Exact Commands`, in order.

    A trailing ` # expect N` sets the expected exit (default 0). No such section gives [].
    """
    found: list[tuple[list[str], int]] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == PLAN_SECTION
            continue
        if not inside or not line.startswith("$ "):
            continue
        body, expect = line[2:].rstrip(), 0
        match = _EXPECT.search(body)
        if match:
            body, expect = body[:match.start()], int(match.group(1))
        try:
            argv = shlex.split(body)
        except ValueError as exc:
            raise KitError(f"cannot parse PLAN command {_show(repr(line))}: {_show(exc)}") from exc
        if not argv:
            raise KitError(f"empty PLAN command: {_show(repr(line))}")
        found.append((argv, expect))
    return found


def _git_bytes(root: Path, *args: str) -> bytes:
    """Raw stdout of git in `root` (never stripped, so leading spaces of status lines survive)."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git: {_show(exc.strerror or exc)}") from exc
    if proc.returncode != 0:
        lines = [ln.strip() for ln in proc.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        raise KitError(f"git {_show(' '.join(args))} failed: "
                       f"{_show(lines[0]) if lines else f'exit {proc.returncode}'}")
    return proc.stdout


def _git_fields(root: Path, *args: str) -> list[str]:
    """The NUL-terminated fields of a `-z` git command, unstripped."""
    return [f for f in _git_bytes(root, *args).decode("utf-8", "replace").split("\0") if f]


# --- trial merge -------------------------------------------------------------------

def _check_ref(name, what: str) -> str:
    if not isinstance(name, str) or not _REF.match(name):
        raise KitError(f"not a usable {what} name: {_show(name)}")
    return name


def _branch_head(root: Path, branch: str) -> str:
    _check_ref(branch, "branch")
    return git(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")


def _remove_trial(root: Path, trial: Path) -> str | None:
    """Remove the trial worktree git created at exactly `trial`; return a problem description or None."""
    if not trial.name.startswith("pk-trial-"):
        return f"refusing to remove {trial}: not a trial worktree"
    try:
        git(root, "worktree", "remove", "--force", str(trial))
    except KitError as exc:
        return f"cannot remove trial worktree {trial}: {exc}"
    return None


def trial_merge(root, branch: str, main: str = "main") -> Path:
    """Merge `branch` into a temporary detached worktree of `main` without committing; return its path.

    The caller removes the worktree. A conflict aborts the merge, removes the worktree and raises
    KitError listing the conflicted paths. The main checkout is never touched.
    """
    root = Path(root).resolve()
    _check_ref(main, "main branch")
    _branch_head(root, branch)
    git(root, "rev-parse", "--verify", main + "^{commit}")
    trial = Path(tempfile.mkdtemp(prefix="pk-trial-"))
    try:
        git(root, "worktree", "add", "--detach", str(trial), main)
    except KitError:
        shutil.rmtree(trial, ignore_errors=True)  # git never registered it: this is the empty mkdtemp dir
        raise
    try:
        code, output = run_argv(["git", "-C", str(trial), "merge", "--no-commit", "--no-ff",
                                 f"refs/heads/{branch}"], trial)
        if code == 0:
            return trial
        try:
            conflicted = _git_fields(trial, "diff", "--name-only", "-z", "--diff-filter=U")
        except KitError:
            conflicted = []
        run_argv(["git", "-C", str(trial), "merge", "--abort"], trial)
        if conflicted:
            raise KitError(f"merge of {_show(branch)} into {_show(main)} conflicts in: "
                           f"{_show(', '.join(conflicted))}")
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        raise KitError(f"merge of {_show(branch)} into {_show(main)} failed: "
                       f"{_show(lines[-1]) if lines else f'exit {code}'}")
    except BaseException as exc:  # nothing may leave the trial worktree behind
        problem = _remove_trial(root, trial)
        if problem and isinstance(exc, KitError):
            raise KitError(f"{_show(exc)} ({_show(problem)})") from None
        if problem:
            _out(f"warning: {problem}", sys.stderr)
        raise


# --- running steps -----------------------------------------------------------------

def _run_commands(step: str, commands: list[tuple[list[str], int]], cwd: Path) -> bool:
    """Run (argv, expected exit) pairs in order; print the first failure and return False.

    Output tail lines are indented with `    | ` so a command can never print a line that looks like
    the gate's own `OK:` line. An empty list runs nothing and prints nothing.
    """
    if not commands:
        return True
    for argv, expect in commands:
        code, output = run_argv(argv, cwd, timeout=STEP_TIMEOUT)
        if code == expect:
            continue
        if code == 127:
            _out(f"FAILED: {step} missing tool: {argv[0]}")
            return False
        _out(f"FAILED: {step} {' '.join(argv)} (exit {code})")
        for line in output.splitlines()[-TAIL_LINES:]:
            _out(TAIL_PREFIX + line)
        return False
    _out(f"step {step}: ok")
    return True


def _commands(argvs: list[list[str]]) -> list[tuple[list[str], int]]:
    return [(argv, 0) for argv in argvs]


def _with_trial(root: Path, trial: Path, body) -> int:
    """Run `body()` and always remove the trial worktree; a removal failure fails a passing run."""
    try:
        code = body()
    finally:
        problem = _remove_trial(root, trial)
        if problem and sys.exc_info()[0] is not None:
            _out(f"warning: {problem}", sys.stderr)
            problem = None
    if problem:
        raise KitError(_show(problem))
    return code


# --- hand-back gate ----------------------------------------------------------------

def _task_worktree(root: Path, task_id: str, branch: str, override) -> Path:
    wt = Path(override) if override is not None else root / ".worktrees" / task_id.lower()
    if not wt.is_dir():
        raise KitError(f"task worktree not found: {_show(wt)}")
    wt = wt.resolve()
    if wt == root:
        raise KitError(f"task worktree must not be the main checkout: {_show(wt)}")
    if Path(git(wt, "rev-parse", "--show-toplevel")).resolve() != wt:
        raise KitError(f"task worktree {_show(wt)} is not the top level of a git worktree")
    common = Path(wt, git(wt, "rev-parse", "--git-common-dir")).resolve()
    if common != Path(root, git(root, "rev-parse", "--git-common-dir")).resolve():
        raise KitError(f"task worktree {_show(wt)} belongs to a different repository")
    try:  # the full ref: `--short` turns into `heads/<name>` when a tag has the same name
        current = git(wt, "symbolic-ref", "HEAD").removeprefix("refs/heads/")
    except KitError:
        current = "a detached HEAD"
    if current != branch:
        raise KitError(f"task worktree {_show(wt)} is on {_show(current)}, not on {_show(branch)}")
    dirty = _changes(wt)
    if dirty:
        raise KitError(f"task worktree {_show(wt)} is not clean: {_show(', '.join(dirty))}")
    return wt


def _norm_guard(entry: str) -> str:
    while entry.startswith("./"):
        entry = entry[2:]
    return entry


def _guarded_changes(root: Path, task_id: str, branch: str, main: str, guarded: list[str]) -> list[str]:
    """Guarded paths the branch changed against its merge-base with main and the SPEC does not allow.

    An entry is an exact path, or a directory when it ends in `/` (everything under it is guarded).
    The SPEC allows a changed path with `Guarded edit: <path>`; a directory entry may be allowed whole
    with `Guarded edit: <directory>/`.
    """
    entries = [_norm_guard(entry) for entry in guarded]
    if not entries:
        return []
    base = git(root, "merge-base", main, f"refs/heads/{branch}")
    changed = sorted(_git_fields(root, "diff", "--name-only", "--no-renames", "-z",
                                 f"{base}..refs/heads/{branch}"))

    def guard(path: str) -> str | None:
        return next((e for e in entries if path == e or (e.endswith("/") and path.startswith(e))), None)

    touched = [(path, guard(path)) for path in changed if guard(path)]
    if not touched:
        return []
    try:  # the SPEC on main: a branch cannot allow its own edit
        spec = _git_bytes(root, "show", f"{main}:program/tasks/{task_id}/SPEC.md").decode("utf-8", "replace")
    except KitError:
        spec = ""
    allowed = {_norm_guard(m.group(1)) for m in map(_GUARDED.match, spec.splitlines()) if m}
    return [path for path, entry in touched if path not in allowed and entry not in allowed]


def _results_problems(data, task_id: str, head: str | None) -> list[str]:
    """Why a results.json does not pass the hand-back gate (empty when it does); `head` None skips the head check."""
    problems = evidence_module.validate_results(data)
    if problems:
        return problems
    if data["task_id"] != task_id:
        problems.append(f"task_id: is {_show(data['task_id'])}, expected {_show(task_id)}")
    if head is not None and data["head"] != head:
        problems.append(f"head: is {_show(data['head'])}, expected the worktree HEAD {_show(head)}")
    if data["all_commands_as_expected"] is not True:
        problems.append("all_commands_as_expected: is not true")
    if data["planted_bugs_all_killed"] is not True:
        problems.append("planted_bugs_all_killed: is not true (a planted bug survived)")
    if not data["commands"]:
        problems.append("commands: is empty (evidence must record the commands that were run)")
    return problems


def _changes(wt: Path) -> list[str]:
    """Every dirty path of a worktree, one string each (a rename reads `new (renamed from old)`).

    Reads `git status --porcelain=v1 -z` raw: each entry is two status characters, a space and the path,
    and a rename or copy is followed by a field holding its source path.
    """
    fields = _git_fields(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    changes, index = [], 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        path = entry[3:]
        if entry[0] in "RC" or entry[1] in "RC":
            source = fields[index] if index < len(fields) else "?"
            index += 1
            path = f"{path} (renamed from {source})"
        changes.append(path)
    return changes


def _unexpected_changes(wt: Path, allowed: str) -> list[str]:
    return [path for path in _changes(wt) if path != allowed]


def _evidence_step(wt: Path, task_id: str) -> bool:
    base = f"program/evidence/{task_id}"
    produce, results_rel = f"{base}/produce.py", f"{base}/results.json"
    if not (wt / produce).is_file():
        _out(f"FAILED: evidence {produce} is missing")
        return False
    if not _run_commands("evidence", [(["python3", produce], 0)], wt):
        return False
    try:
        data = read_json(wt / results_rel)
    except KitError as exc:
        _out("FAILED: evidence " + " ".join(str(exc).replace(str(wt) + "/", "").split()))
        return False
    problems = _results_problems(data, task_id, git(wt, "rev-parse", "HEAD"))
    if problems:
        _out("FAILED: evidence")
        for problem in problems:
            _out(problem)
        return False
    extra = _unexpected_changes(wt, results_rel)
    for path in extra:
        _out(f"FAILED: evidence unexpected change: {path}")
    return not extra


def _stale_step(wt: Path, main: str) -> bool:
    report = stale_module.stale_records(wt, main)
    _out(f"stale: {len(report['stale'])} record(s)")
    for row in report["stale"]:
        _out(row["path"])
    for row in report["unreadable"]:
        _out(f"FAILED: stale unreadable record {row['path']}: {row['error']}")
    return not report["unreadable"]


def _run_steps(root: Path, checks: dict, task_id: str, wt: Path, trial: Path, main: str) -> int:
    for step in ("build", "compile_check"):
        if not _run_commands(step, _commands(checks[step]), trial):
            return 1
    for step in ("test", "scan", "extra_gate"):
        if not _run_commands(step, _commands(checks[step]), wt):
            return 1
    plan_rel = f"program/tasks/{task_id}/PLAN.md"
    try:
        authoritative = _git_bytes(root, "show", f"{main}:{plan_rel}")
    except KitError:
        authoritative = None
    own = wt / plan_rel
    if authoritative is None or not own.is_file() or own.read_bytes() != authoritative:
        _out(f"FAILED: plan {plan_rel} differs from {main}")
        return 1
    try:
        plan = parse_plan(authoritative.decode("utf-8", "replace"))
    except KitError as exc:
        _out(f"FAILED: plan {exc}")
        return 1
    if not plan:
        _out(f'FAILED: plan no commands under "{PLAN_SECTION}"')
        return 1
    if not _run_commands("plan", plan, wt):
        return 1
    if not _evidence_step(wt, task_id):
        return 1
    return 0 if _stale_step(wt, main) else 1


def handback(root, task_id: str, branch: str, worktree=None, main: str = "main") -> int:
    """The hand-back gate; 0 and a final `OK:` line only when every step passed."""
    root = Path(root).resolve()
    if not isinstance(task_id, str) or not _TASK.match(task_id):
        raise KitError(f"not a usable task id: {_show(task_id)}")
    _check_ref(main, "main branch")
    sha = _branch_head(root, branch)
    wt = _task_worktree(root, task_id, branch, worktree)
    checks = load_checks_at(root, main, require=("build", "test", "scan"))
    guarded = _guarded_changes(root, task_id, branch, main, checks["guarded_files"])
    if guarded:
        _out(f"FAILED: guarded files changed: {', '.join(guarded)}")
        return 1
    trial = trial_merge(root, branch, main)
    code = _with_trial(root, trial, lambda: _run_steps(root, checks, task_id, wt, trial, main))
    if code:
        return code
    if git(wt, "rev-parse", "HEAD") != sha or _branch_head(root, branch) != sha:
        _out(f"FAILED: {branch} moved during the gate")
        return 1
    _print_ok("handback", branch, sha)
    return 0


def merge_check(root, branch: str, main: str = "main") -> int:
    """The controller's check from main: trial merge, build, compile_check, test; 0 and an `OK:` line."""
    root = Path(root).resolve()
    _check_ref(main, "main branch")
    sha = _branch_head(root, branch)
    checks = load_checks_at(root, main, require=("build", "test"))
    trial = trial_merge(root, branch, main)

    def steps() -> int:
        for step in ("build", "compile_check", "test"):
            if not _run_commands(step, _commands(checks[step]), trial):
                return 1
        return 0

    code = _with_trial(root, trial, steps)
    if code:
        return code
    _print_ok("merge", branch, sha)
    return 0


# --- gate claims -------------------------------------------------------------------

def _mapping(data, label: str, key: str) -> dict:
    """The mapping under top-level `key` of a loaded file; absent or empty gives {}."""
    if data is not None and not isinstance(data, dict):
        raise KitError(f"{_show(label)}: expected a mapping")
    value = {} if data is None or data.get(key) is None else data[key]
    if not isinstance(value, dict):
        raise KitError(f"{_show(label)}: '{_show(key)}' must be a mapping")
    return value


def _environments(root: Path) -> dict[str, bool]:
    path = root / "program" / "environments.yaml"
    found = {}
    for name, info in _mapping(read_yaml(path), "program/environments.yaml", "environments").items():
        if not isinstance(info, dict) or not isinstance(info.get("exists"), bool):
            raise KitError(f"program/environments.yaml: {_show(name)}.exists must be true or false")
        found[str(name)] = info["exists"]
    return found


def _record_path(value, gate_id: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or value.startswith("/")
            or _CONTROL.search(value)):
        raise KitError(f"gate {_show(gate_id)}: record path must be a relative posix path without control "
                       f"characters: {_show(repr(value))}")
    pure = PurePosixPath(value)
    if ".." in pure.parts or not pure.parts:
        raise KitError(f"gate {_show(gate_id)}: record path must stay inside the project: {_show(value)}")
    return pure.as_posix()


def _gate(root: Path, gate_id: str) -> tuple[str, list[str]]:
    """(environment, record paths) of a gate; KitError when it is unknown or malformed."""
    if not isinstance(gate_id, str) or not GATE_ID.match(gate_id):
        raise KitError(f"not a valid gate id (expected G-<NAME>): {_show(gate_id)}")
    gates = _mapping(read_yaml(root / "program" / "gates.yaml"), "program/gates.yaml", "gates")
    entry = gates.get(gate_id)
    if entry is None:
        raise KitError(f"unknown gate: {_show(gate_id)}")
    if not isinstance(entry, dict) or not isinstance(entry.get("environment"), str):
        raise KitError(f"gate {_show(gate_id)}: environment must name an entry of environments.yaml")
    records = entry.get("records")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise KitError(f"gate {_show(gate_id)}: records must be a list of {{path, acceptance}} mappings")
    return entry["environment"], [_record_path(item.get("path"), gate_id) for item in records]


def _claim_name(record: str) -> str:
    name = record.replace("/", "__")
    return name if name.endswith(".json") else name + ".json"


def _states(root: Path, record: str, own: str, environments: dict[str, bool]):
    """({environment: state}, acceptance sha256 or None) for one record."""
    path = root / record
    record_sha = sha256_file(path) if path.is_file() else None
    acceptance_path = path.parent / "acceptance.json"
    acceptance_sha = sha256_file(acceptance_path) if acceptance_path.is_file() else None
    accepted = None
    if acceptance_sha is not None:
        try:
            accepted = read_json(acceptance_path)
        except KitError:
            accepted = None
    if not isinstance(accepted, dict):
        accepted = {}
    blocked_by = accepted.get("blocked_by")
    states = {}
    for name, exists in environments.items():
        state = NOT_EVALUATED
        if exists and name == own:
            if isinstance(blocked_by, list) and name in blocked_by:
                state = BLOCKED
            elif (isinstance(blocked_by, list) and accepted.get("decision") == "accepted_development"
                  and record_sha is not None and accepted.get("evidence_sha256") == record_sha):
                state = EVIDENCED
        states[name] = state
    return states, acceptance_sha


def _expected_claims(root: Path, gate_id: str) -> dict[str, dict]:
    own, records = _gate(root, gate_id)
    environments = _environments(root)
    if own not in environments:
        raise KitError(f"gate {_show(gate_id)}: environment {_show(own)} is not named in program/environments.yaml")
    commit = git(root, "rev-parse", "HEAD")
    docs: dict[str, dict] = {}
    for record in records:
        name = _claim_name(record)
        if name in docs:
            raise KitError(f"gate {_show(gate_id)}: records collide on the claim file {_show(name)}")
        states, acceptance_sha = _states(root, record, own, environments)
        docs[name] = {"gate": gate_id, "record": record, "environment": states,
                      "acceptance_sha256": acceptance_sha, "generated_from_commit": commit}
    return docs


def _claims_dir(root: Path, gate_id: str) -> Path:
    return root / "program" / "claims" / gate_id


def _refuse_symlink(directory: Path) -> None:
    if directory.is_symlink():
        raise KitError(f"{_show(directory)} is a symlink; refusing to use the claims directory through it")


def claims(root, gate_id: str) -> list[Path]:
    """Write `program/claims/<GATE>/<record-name>.json` per record and drop files of records no longer in the gate."""
    root = Path(root).resolve()
    docs = _expected_claims(root, gate_id)
    directory = _claims_dir(root, gate_id)
    _refuse_symlink(directory)
    written = []
    for name, doc in docs.items():
        write_json(directory / name, doc)
        written.append(directory / name)
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if entry.name not in docs and (entry.is_file() or entry.is_symlink()):
                entry.unlink()
    return written


def validate_claims(root, gate_id: str) -> list[str]:
    """One line per claim file that is missing, extra or differs from a recomputation (commit excluded)."""
    root = Path(root).resolve()
    docs = _expected_claims(root, gate_id)
    directory = _claims_dir(root, gate_id)
    _refuse_symlink(directory)
    problems = []
    for name, doc in docs.items():
        path = directory / name
        rel = path.relative_to(root).as_posix()
        if not path.is_file():
            problems.append(f"{_show(rel)}: missing")
            continue
        try:
            actual = read_json(path)
        except KitError as exc:
            problems.append(f"{_show(rel)}: unreadable: {_show(' '.join(str(exc).replace(str(path), rel).split()))}")
            continue
        if not isinstance(actual, dict):
            problems.append(f"{_show(rel)}: not a JSON object")
            continue
        differing = sorted(key for key in set(doc) | set(actual) if key != "generated_from_commit"
                           and doc.get(key, _MISSING) != actual.get(key, _MISSING))
        if differing:
            problems.append(f"{_show(rel)}: differs in {_show(', '.join(differing))}")
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if entry.name not in docs:
                problems.append(f"{_show(entry.relative_to(root).as_posix())}: extra file "
                                f"(no such record in {_show(gate_id)})")
    return problems


# --- binding ----------------------------------------------------------------------

def binding(root, task_id: str, branch: str, main: str = "main") -> list[str]:
    """Problems that stop a branch's results.json from binding its tip (empty, with a printed line, when it holds).

    Holds only when `program/evidence/<ID>/results.json` on the branch tip validates, its `head` is an
    ancestor of the tip, and every commit after `head` up to the tip (merge commits against each parent)
    changes only paths under `program/evidence/<ID>/` or exactly `program/status/<ID>.json`. A commit that
    edits other paths and a later one that reverts them therefore still fails. Everything is read from git
    objects, never from a working tree. At most one problem is reported for the commits: the first offender.
    """
    root = Path(root).resolve()
    if not isinstance(task_id, str) or not _TASK.match(task_id):
        raise KitError(f"not a usable task id: {_show(task_id)}")
    _check_ref(main, "main branch")
    tip = _branch_head(root, branch)
    rel = f"program/evidence/{task_id}/results.json"
    try:
        text = _git_bytes(root, "show", f"{tip}:{rel}").decode("utf-8")
        data = json.loads(text)
    except KitError:
        return [f"{_show(rel)} is absent on {_show(branch)}"]
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return [f"{_show(rel)} on {_show(branch)} is not valid JSON: {_show(exc)}"]
    problems = _results_problems(data, task_id, None)
    if problems:
        return [f"{_show(rel)}: {problem}" for problem in problems]
    head = data["head"]
    code, _ = run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", head, tip], root)
    if code != 0:
        return [f"{_show(rel)}: head {_show(head)} is not an ancestor of {_show(branch)} ({tip})"]
    prefix, status = f"program/evidence/{task_id}/", f"program/status/{task_id}.json"
    commits = git(root, "rev-list", "--topo-order", "--reverse", f"{head}..{tip}").split()
    for commit in commits:
        for path in _git_fields(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--root",
                                "--no-renames", "-z", commit):
            if not path.startswith(prefix) and path != status:
                return [f"{_show(path)}: changed in commit {commit} after {head}, outside {_show(prefix)} "
                        f"and {_show(status)}"]
    _out(f"binding holds: results.json at {head} bound, {len(commits)} later commit(s) touch only evidence "
         "and status")
    return []


# --- command -----------------------------------------------------------------------

def _report(problems: list[str]) -> int:
    for problem in problems:
        _out("error: " + problem, sys.stderr)
    return 1


def cmd_gate(args: argparse.Namespace) -> int:
    action = getattr(args, "gate_action", None)
    if action not in ("handback", "merge-check", "binding", "claims", "validate"):
        raise KitError("gate needs an action: handback, merge-check, binding, claims or validate")
    root = find_root()
    if action == "handback":
        return handback(root, args.task_id, args.branch, worktree=args.worktree, main=args.main)
    if action == "merge-check":
        return merge_check(root, args.branch, main=args.main)
    if action == "binding":
        problems = binding(root, args.task_id, args.branch, main=args.main)
        return _report(problems) if problems else 0
    if action == "claims":
        _out(f"wrote {len(claims(root, args.gate))} claim documents")
        return 0
    problems = validate_claims(root, args.gate)
    if problems:
        return _report(problems)
    _out(f"claims consistent for {args.gate}")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="gate_action", metavar="action", parser_class=type(parser))
    hand = actions.add_parser("handback", help="the hand-back gate an implementer passes before QA")
    hand.add_argument("task_id", metavar="ID", help="task ID, for example P0-M01-S01-T01")
    hand.add_argument("--branch", required=True, help="the task branch")
    hand.add_argument("--worktree", help="the task worktree (default: <root>/.worktrees/<id lowercased>)")
    hand.add_argument("--main", default="main", help="main branch name (default: main)")
    merge = actions.add_parser("merge-check", help="the controller's trial merge, build and test")
    merge.add_argument("--branch", required=True, help="the branch to merge")
    merge.add_argument("--main", default="main", help="main branch name (default: main)")
    bind = actions.add_parser("binding", help="QA check: the branch's results.json still binds its tip")
    bind.add_argument("task_id", metavar="ID", help="task ID, for example P0-M01-S01-T01")
    bind.add_argument("--branch", required=True, help="the task branch")
    bind.add_argument("--main", default="main", help="main branch name (default: main)")
    make = actions.add_parser("claims", help="write the claim documents of a gate")
    make.add_argument("gate", metavar="GATE", help="gate ID, for example G-P0")
    check = actions.add_parser("validate", help="fail on drift between claim documents and a recomputation")
    check.add_argument("gate", metavar="GATE", help="gate ID, for example G-P0")


def register(add_command) -> None:
    add_command("gate", cmd_gate, help="gate runner: handback, merge-check, binding, claims, validate",
                configure=_configure)
