#!/usr/bin/env python3
"""The gate runner: hand-back gate, merge check and gate claim documents (standard library only).

`handback` is the gate an implementer passes before independent QA; `merge_check` is the
controller's check from main; `claims` and `validate_claims` keep `program/claims/<GATE>/*.json`
equal to a recomputation from `gates.yaml`, each record's `acceptance.json` and `environments.yaml`.

Run it from the main checkout: the task worktree defaults to `<root>/.worktrees/<id lowercased>`.
`checks.yaml`, the guarded files and the `Guarded edit:` lines in the task's SPEC are all read from
the main checkout and the main branch, so a task branch cannot change the rules it is judged by.
The trial merge happens in a temporary worktree of main and never commits or touches the main checkout.

Known limits:
- Projects must git-ignore the bytecode and build output their commands write: the evidence step
  needs a clean tree, and a leftover untracked file fails it.
- Evidence is bound to the commit it was produced at, and the produced `results.json` is left
  uncommitted in the task worktree (the only change the gate allows). Delete or commit it before a re-run.
- The task's own record shows up in the `stale:` list whenever the branch changed an input of it.
"""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

import evidence as evidence_module
import stale as stale_module
from common import KitError, find_root, git, read_json, read_yaml, run_argv, sha256_file, write_json
from register import GATE_ID

CHECK_KEYS = ("build", "test", "scan", "compile_check", "extra_gate")
ALL_KEYS = CHECK_KEYS + ("guarded_files",)
STEP_TIMEOUT = 3600  # seconds per command
TAIL_LINES = 20
PLAN_SECTION = "## Exact Commands"
NOT_EVALUATED, EVIDENCED, BLOCKED = "not_evaluated", "evidenced", "blocked"

_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*\Z")
_EXPECT = re.compile(r"\s+#\s+expect\s+(\d+)\s*\Z")
_GUARDED = re.compile(r"Guarded edit:\s*(\S.*?)\s*\Z")
_MISSING = object()


# --- checks.yaml -------------------------------------------------------------------

def _argv_lists(value, key: str) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise KitError(f"checks.yaml: {key} must be a list of argv lists")
    commands = []
    for item in value:
        if not isinstance(item, list) or not item or not all(isinstance(part, str) and part for part in item):
            raise KitError(f"checks.yaml: every {key} command must be a non-empty argv list of strings, "
                           "not a shell string")
        commands.append(list(item))
    return commands


def load_checks(root, require: tuple[str, ...] = ()) -> dict:
    """`program/checks.yaml` as {build, test, scan, compile_check, extra_gate: argv lists; guarded_files: [str]}.

    Keys named in `require` must list at least one command: a gate that runs nothing proves nothing.
    """
    data = read_yaml(Path(root) / "program" / "checks.yaml")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise KitError("checks.yaml: expected a mapping")
    unknown = sorted(str(key) for key in data if key not in ALL_KEYS)
    if unknown:
        raise KitError(f"checks.yaml: unknown key {', '.join(unknown)} (expected {', '.join(ALL_KEYS)})")
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
            raise KitError(f"cannot parse PLAN command {line!r}: {exc}") from exc
        if not argv:
            raise KitError(f"empty PLAN command: {line!r}")
        found.append((argv, expect))
    return found


def plan_commands(worktree, task_id: str) -> list[tuple[list[str], int]]:
    """The PLAN commands of a task in `worktree`; [] when PLAN.md does not exist."""
    path = Path(worktree) / "program" / "tasks" / task_id / "PLAN.md"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KitError(f"cannot read {path}: {exc}") from exc
    return parse_plan(text)


# --- trial merge -------------------------------------------------------------------

def _check_ref(name, what: str) -> str:
    if not isinstance(name, str) or not _REF.match(name):
        raise KitError(f"not a usable {what} name: {name}")
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
    code, output = run_argv(["git", "-C", str(trial), "merge", "--no-commit", "--no-ff", branch], trial)
    if code == 0:
        return trial
    try:
        conflicted = [p for p in git(trial, "diff", "--name-only", "-z", "--diff-filter=U").split("\0") if p]
    except KitError:
        conflicted = []
    run_argv(["git", "-C", str(trial), "merge", "--abort"], trial)
    problem = _remove_trial(root, trial)
    if conflicted:
        message = f"merge of {branch} into {main} conflicts in: {', '.join(conflicted)}"
    else:
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        message = f"merge of {branch} into {main} failed: {lines[-1] if lines else f'exit {code}'}"
    raise KitError(message + (f" ({problem})" if problem else ""))


# --- running steps -----------------------------------------------------------------

def _run_commands(step: str, commands: list[tuple[list[str], int]], cwd: Path) -> bool:
    """Run (argv, expected exit) pairs in order; print the first failure and return False."""
    for argv, expect in commands:
        code, output = run_argv(argv, cwd, timeout=STEP_TIMEOUT)
        if code == expect:
            continue
        if code == 127:
            print(f"FAILED: {step} missing tool: {argv[0]}")
            return False
        print(f"FAILED: {step} {' '.join(argv)} (exit {code})")
        for line in output.splitlines()[-TAIL_LINES:]:
            print(line)
        return False
    print(f"step {step}: ok")
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
            print(f"warning: {problem}", file=sys.stderr)
            problem = None
    if problem:
        raise KitError(problem)
    return code


# --- hand-back gate ----------------------------------------------------------------

def _task_worktree(root: Path, task_id: str, branch: str, override) -> Path:
    wt = Path(override) if override is not None else root / ".worktrees" / task_id.lower()
    if not wt.is_dir():
        raise KitError(f"task worktree not found: {wt}")
    wt = wt.resolve()
    if wt == root:
        raise KitError(f"task worktree must not be the main checkout: {wt}")
    if Path(git(wt, "rev-parse", "--show-toplevel")).resolve() != wt:
        raise KitError(f"task worktree {wt} is not the top level of a git worktree")
    common = Path(wt, git(wt, "rev-parse", "--git-common-dir")).resolve()
    if common != Path(root, git(root, "rev-parse", "--git-common-dir")).resolve():
        raise KitError(f"task worktree {wt} belongs to a different repository")
    try:
        current = git(wt, "symbolic-ref", "--short", "HEAD")
    except KitError:
        current = "a detached HEAD"
    if current != branch:
        raise KitError(f"task worktree {wt} is on {current}, not on {branch}")
    dirty = git(wt, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        paths = ", ".join(line[3:] for line in dirty.splitlines())
        raise KitError(f"task worktree {wt} is not clean: {paths}")
    return wt


def _guarded_changes(root: Path, task_id: str, branch: str, main: str, guarded: list[str]) -> list[str]:
    """Guarded files the branch changed against its merge-base with main and the SPEC does not allow."""
    if not guarded:
        return []
    base = git(root, "merge-base", main, f"refs/heads/{branch}")
    changed = {p for p in git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}..refs/heads/{branch}")
               .split("\0") if p}
    touched = [path for path in guarded if path in changed]
    if not touched:
        return []
    try:  # the SPEC on main: a branch cannot allow its own edit
        spec = git(root, "show", f"{main}:program/tasks/{task_id}/SPEC.md")
    except KitError:
        spec = ""
    allowed = {m.group(1) for m in map(_GUARDED.match, spec.splitlines()) if m}
    return [path for path in touched if path not in allowed]


def _results_problems(data, task_id: str, head: str) -> list[str]:
    """Why a results.json does not pass the hand-back gate (empty when it does)."""
    problems = evidence_module.validate_results(data)
    if problems:
        return problems
    if data["task_id"] != task_id:
        problems.append(f"task_id: is {data['task_id']}, expected {task_id}")
    if data["head"] != head:
        problems.append(f"head: is {data['head']}, expected the worktree HEAD {head}")
    if data["all_commands_as_expected"] is not True:
        problems.append("all_commands_as_expected: is not true")
    if data["planted_bugs_all_killed"] is not True:
        problems.append("planted_bugs_all_killed: is not true (a planted bug survived)")
    if not data["commands"]:
        problems.append("commands: is empty (evidence must record the commands that were run)")
    return problems


def _unexpected_changes(wt: Path, allowed: str) -> list[str]:
    entries = git(wt, "status", "--porcelain", "-z", "--untracked-files=all").split("\0")
    paths, skip = [], False
    for entry in entries:
        if skip:  # the origin half of a rename or copy
            skip = False
            continue
        if len(entry) < 4:
            continue
        if entry[0] in "RC" or entry[1] in "RC":
            skip = True
        paths.append(entry[3:])
    return [path for path in paths if path != allowed]


def _evidence_step(wt: Path, task_id: str) -> bool:
    base = f"program/evidence/{task_id}"
    produce, results_rel = f"{base}/produce.py", f"{base}/results.json"
    if not (wt / produce).is_file():
        print(f"FAILED: evidence {produce} is missing")
        return False
    if not _run_commands("evidence", [(["python3", produce], 0)], wt):
        return False
    try:
        data = read_json(wt / results_rel)
    except KitError as exc:
        print("FAILED: evidence " + " ".join(str(exc).replace(str(wt) + "/", "").split()))
        return False
    problems = _results_problems(data, task_id, git(wt, "rev-parse", "HEAD"))
    if problems:
        print("FAILED: evidence")
        for problem in problems:
            print(problem)
        return False
    extra = _unexpected_changes(wt, results_rel)
    for path in extra:
        print(f"FAILED: evidence unexpected change: {path}")
    return not extra


def _stale_step(wt: Path, main: str) -> bool:
    report = stale_module.stale_records(wt, main)
    print(f"stale: {len(report['stale'])} record(s)")
    for row in report["stale"]:
        print(row["path"])
    for row in report["unreadable"]:
        print(f"FAILED: stale unreadable record {row['path']}: {row['error']}")
    return not report["unreadable"]


def _run_steps(root: Path, checks: dict, task_id: str, wt: Path, trial: Path, main: str) -> int:
    for step in ("build", "compile_check"):
        if not _run_commands(step, _commands(checks[step]), trial):
            return 1
    for step in ("test", "scan", "extra_gate"):
        if not _run_commands(step, _commands(checks[step]), wt):
            return 1
    try:
        plan = plan_commands(wt, task_id)
    except KitError as exc:
        print(f"FAILED: plan {exc}")
        return 1
    if not plan:
        print(f'FAILED: plan no commands under "{PLAN_SECTION}"')
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
        raise KitError(f"not a usable task id: {task_id}")
    _check_ref(main, "main branch")
    sha = _branch_head(root, branch)
    wt = _task_worktree(root, task_id, branch, worktree)
    checks = load_checks(root, require=("build", "test", "scan"))
    guarded = _guarded_changes(root, task_id, branch, main, checks["guarded_files"])
    if guarded:
        print(f"FAILED: guarded files changed: {', '.join(guarded)}")
        return 1
    trial = trial_merge(root, branch, main)
    code = _with_trial(root, trial, lambda: _run_steps(root, checks, task_id, wt, trial, main))
    if code:
        return code
    if git(wt, "rev-parse", "HEAD") != sha or _branch_head(root, branch) != sha:
        print(f"FAILED: {branch} moved during the gate")
        return 1
    print(f"OK: {branch} passes the hand-back gate at {sha}")
    return 0


def merge_check(root, branch: str, main: str = "main") -> int:
    """The controller's check from main: trial merge, build, compile_check, test; 0 and an `OK:` line."""
    root = Path(root).resolve()
    _check_ref(main, "main branch")
    sha = _branch_head(root, branch)
    checks = load_checks(root, require=("build", "test"))
    trial = trial_merge(root, branch, main)

    def steps() -> int:
        for step in ("build", "compile_check", "test"):
            if not _run_commands(step, _commands(checks[step]), trial):
                return 1
        return 0

    code = _with_trial(root, trial, steps)
    if code:
        return code
    print(f"OK: {branch} merges into main at {sha}")
    return 0


# --- gate claims -------------------------------------------------------------------

def _mapping(data, label: str, key: str) -> dict:
    """The mapping under top-level `key` of a loaded file; absent or empty gives {}."""
    if data is not None and not isinstance(data, dict):
        raise KitError(f"{label}: expected a mapping")
    value = {} if data is None or data.get(key) is None else data[key]
    if not isinstance(value, dict):
        raise KitError(f"{label}: '{key}' must be a mapping")
    return value


def _environments(root: Path) -> dict[str, bool]:
    path = root / "program" / "environments.yaml"
    found = {}
    for name, info in _mapping(read_yaml(path), "program/environments.yaml", "environments").items():
        if not isinstance(info, dict) or not isinstance(info.get("exists"), bool):
            raise KitError(f"program/environments.yaml: {name}.exists must be true or false")
        found[str(name)] = info["exists"]
    return found


def _record_path(value, gate_id: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise KitError(f"gate {gate_id}: record path must be a relative posix path: {value!r}")
    pure = PurePosixPath(value)
    if ".." in pure.parts or not pure.parts:
        raise KitError(f"gate {gate_id}: record path must stay inside the project: {value}")
    return pure.as_posix()


def _gate(root: Path, gate_id: str) -> tuple[str, list[str]]:
    """(environment, record paths) of a gate; KitError when it is unknown or malformed."""
    if not isinstance(gate_id, str) or not GATE_ID.match(gate_id):
        raise KitError(f"not a valid gate id (expected G-<NAME>): {gate_id}")
    gates = _mapping(read_yaml(root / "program" / "gates.yaml"), "program/gates.yaml", "gates")
    entry = gates.get(gate_id)
    if entry is None:
        raise KitError(f"unknown gate: {gate_id}")
    if not isinstance(entry, dict) or not isinstance(entry.get("environment"), str):
        raise KitError(f"gate {gate_id}: environment must name an entry of environments.yaml")
    records = entry.get("records")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise KitError(f"gate {gate_id}: records must be a list of {{path, acceptance}} mappings")
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
        raise KitError(f"gate {gate_id}: environment {own} is not named in program/environments.yaml")
    commit = git(root, "rev-parse", "HEAD")
    docs: dict[str, dict] = {}
    for record in records:
        name = _claim_name(record)
        if name in docs:
            raise KitError(f"gate {gate_id}: records collide on the claim file {name}")
        states, acceptance_sha = _states(root, record, own, environments)
        docs[name] = {"gate": gate_id, "record": record, "environment": states,
                      "acceptance_sha256": acceptance_sha, "generated_from_commit": commit}
    return docs


def _claims_dir(root: Path, gate_id: str) -> Path:
    return root / "program" / "claims" / gate_id


def claims(root, gate_id: str) -> list[Path]:
    """Write `program/claims/<GATE>/<record-name>.json` per record and drop files of records no longer in the gate."""
    root = Path(root).resolve()
    docs = _expected_claims(root, gate_id)
    directory = _claims_dir(root, gate_id)
    if directory.is_symlink():
        raise KitError(f"{directory} is a symlink; refusing to write through it")
    written = []
    for name, doc in docs.items():
        write_json(directory / name, doc)
        written.append(directory / name)
    for entry in sorted(directory.iterdir()):
        if entry.name not in docs and (entry.is_file() or entry.is_symlink()):
            entry.unlink()
    return written


def validate_claims(root, gate_id: str) -> list[str]:
    """One line per claim file that is missing, extra or differs from a recomputation (commit excluded)."""
    root = Path(root).resolve()
    docs = _expected_claims(root, gate_id)
    directory = _claims_dir(root, gate_id)
    problems = []
    for name, doc in docs.items():
        path = directory / name
        rel = path.relative_to(root).as_posix()
        if not path.is_file():
            problems.append(f"{rel}: missing")
            continue
        try:
            actual = read_json(path)
        except KitError as exc:
            problems.append(f"{rel}: unreadable: {' '.join(str(exc).replace(str(path), rel).split())}")
            continue
        if not isinstance(actual, dict):
            problems.append(f"{rel}: not a JSON object")
            continue
        differing = sorted(key for key in set(doc) | set(actual) if key != "generated_from_commit"
                           and doc.get(key, _MISSING) != actual.get(key, _MISSING))
        if differing:
            problems.append(f"{rel}: differs in {', '.join(differing)}")
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if entry.name not in docs:
                problems.append(f"{entry.relative_to(root).as_posix()}: extra file (no such record in {gate_id})")
    return problems


# --- command -----------------------------------------------------------------------

def cmd_gate(args: argparse.Namespace) -> int:
    action = getattr(args, "gate_action", None)
    if action not in ("handback", "merge-check", "claims", "validate"):
        raise KitError("gate needs an action: handback, merge-check, claims or validate")
    root = find_root()
    if action == "handback":
        return handback(root, args.task_id, args.branch, worktree=args.worktree, main=args.main)
    if action == "merge-check":
        return merge_check(root, args.branch, main=args.main)
    if action == "claims":
        print(f"wrote {len(claims(root, args.gate))} claim documents")
        return 0
    problems = validate_claims(root, args.gate)
    if problems:
        print("\n".join("error: " + problem for problem in problems), file=sys.stderr)
        return 1
    print(f"claims consistent for {args.gate}")
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
    make = actions.add_parser("claims", help="write the claim documents of a gate")
    make.add_argument("gate", metavar="GATE", help="gate ID, for example G-P0")
    check = actions.add_parser("validate", help="fail on drift between claim documents and a recomputation")
    check.add_argument("gate", metavar="GATE", help="gate ID, for example G-P0")


def register(add_command) -> None:
    add_command("gate", cmd_gate, help="gate runner: handback, merge-check, claims, validate", configure=_configure)
