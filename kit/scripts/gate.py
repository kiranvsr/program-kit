#!/usr/bin/env python3
"""The gate runner: hand-back gate, merge check and gate claim documents (standard library only).

`handback` is the gate an implementer passes before independent QA; `merge_check` is the
controller's independent run before every merge: the guarded-file check, the SPEC, PLAN and review.md
identity check, then the build, the tests and the scans on the trial merge, so a branch whose implementer
skipped the hand-back gate still cannot reach main. `claims` and `validate_claims` keep
`program/claims/<GATE>/*.json` equal to a recomputation from `gates.yaml`, the latest results and acceptance
record of each task the gate names (read through each task's pointer `program/evidence/<ID>/latest.json`) and
`environments.yaml`.

Run it from the main checkout: the task worktree defaults to `<root>/.worktrees/<id lowercased>`.
`checks.yaml` (with its guarded files), the `Guarded edit:` lines in the task's SPEC and the PLAN commands
are all read from the primary branch, which is always `main` (`git show main:...`): no option names
another branch, so a task branch or a dirty main checkout cannot change the rules it is judged by. The
task's SPEC.md and PLAN.md, and the spec-QA record review.md, must be byte-identical to main's, and a task's
branch is `task/<ID>`. All three must be on main: a task dispatched without its review.md fails both gates.
The trial merge happens in a temporary worktree of main and never commits or touches the main checkout.
`binding` is QA's check that the task's latest results record is a true record of its `head`, read from git
objects only: the record is committed on the branch tip with the digest its pointer names, validates and passes the
lint, its `head` is an ancestor of the tip, every recorded input hash is the hash of the blob at `head`, and no
commit since the branch left main modified or deleted a record. Drift between `head` and the tip is reported,
never a failure: the record says what was verified at `head`, and that stays true.

Limits:
- A produce.py that hand-writes a well-formed record is not detectable by the gate; QA re-runs the
  evidence independently and uses `pk gate binding`.
- `binding` refuses an edited `head` or an edited input hash, but not a record rewritten consistently,
  appended under its new digest and pointed at; the merge check runs the tests and scans again whatever the record
  says.
- Files written to git-ignored paths are invisible to the clean-tree checks.
- Projects must git-ignore the bytecode and build output their commands write: the evidence step
  needs a clean tree, and a leftover untracked file fails it.
- Evidence is bound to the commit it was produced at. The gate leaves the new record and the updated
  task pointer `program/evidence/<ID>/latest.json` uncommitted in the task worktree (the only changes it allows).
  Commit both (the evidence commit) or remove the record and restore the pointer by exact path before a re-run.
- A task whose own inputs did not change since its latest record needs no new record: produce.py may append one,
  or leave the fresh record in place. Other tasks' records are never refreshed because a shared input changed.
"""
from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

import evidence as evidence_module
import stale as stale_module
import store
import miniyaml
from common import KitError, drop_bom, find_root, git, read_json, read_yaml, run_argv, write_json
from register import GATE_ID

MAIN = "main"  # the primary branch; fixed, so no caller can choose where the rules are read from
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
        text = drop_bom(_git_bytes(Path(root), "show", f"{ref}:program/checks.yaml").decode("utf-8"))
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


def trial_merge(root, branch: str, main: str = MAIN) -> Path:
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

def _task_branch(task_id, branch) -> str:
    """`task_id`, checked, when `branch` is its branch `task/<ID>` (the name `pk worktree new` gives it).

    A task is judged by its own SPEC and PLAN on main, so the ID and the branch may not name different tasks.
    """
    if not isinstance(task_id, str) or not _TASK.match(task_id):
        raise KitError(f"not a usable task id: {_show(task_id)}")
    if branch != f"task/{task_id}":
        raise KitError(f"the branch of task {task_id} is task/{task_id}, not {_show(branch)}")
    return task_id


def _branch_task(branch) -> str:
    """The task ID a task branch `task/<ID>` belongs to; KitError for any other branch name."""
    task_id = branch[len("task/"):] if isinstance(branch, str) and branch.startswith("task/") else ""
    if not _TASK.match(task_id):
        raise KitError(f"merge-check needs a task branch named task/<ID>, not {_show(branch)}")
    return task_id


def _task_worktree(root: Path, task_id: str, branch: str, override) -> Path:
    try:
        here = git(root, "symbolic-ref", "HEAD").removeprefix("refs/heads/")
    except KitError:
        here = None
    if here == branch:  # the command was started inside the task worktree, where `root` is that worktree
        raise KitError(f"run pk gate handback from the main checkout: {_show(root)} is the worktree of "
                       f"{_show(branch)}")
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
        spec = drop_bom(_git_bytes(root, "show", f"{main}:program/tasks/{task_id}/SPEC.md").decode("utf-8", "replace"))
    except KitError:
        spec = ""
    allowed = {_norm_guard(m.group(1)) for m in map(_GUARDED.match, spec.splitlines()) if m}
    return [path for path, entry in touched if path not in allowed and entry not in allowed]


def _results_problems(data, task_id: str, head: str | None, root=None) -> list[str]:
    """Why a results record does not pass the hand-back gate (empty when it does); `head` None skips the head check.

    The rules live in `evidence.gate_problems` (the status machine applies them too), the derived-output lint
    included; here every problem is escaped, because it quotes values the record controls.
    """
    return [_show(problem) for problem in evidence_module.gate_problems(data, task_id, head, root)]


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
    """Run produce.py, then judge the task's latest results record.

    A record appended by this run must pass the gate's checks at the worktree HEAD. When produce.py appended
    nothing, the latest record must still be fresh: its `head` an ancestor of HEAD and none of its inputs changed
    since (a task whose own source changed needs a new record; a shared input changed by another task does not
    make it refresh anything else). The record history must be intact since the branch left main. The new record
    and the task's own pointer (`program/evidence/<ID>/latest.json`) are the only changes allowed in the worktree.
    """
    produce = f"program/evidence/{task_id}/produce.py"
    if not (wt / produce).is_file():
        _out(f"FAILED: evidence {produce} is missing")
        return False
    try:
        before = store.read_pointer(wt, task_id).get("results")
    except KitError as exc:
        _out("FAILED: evidence " + " ".join(str(exc).split()))
        return False
    if not _run_commands("evidence", [(["python3", produce], 0)], wt):
        return False
    head = git(wt, "rev-parse", "HEAD")
    try:
        entry = store.read_pointer(wt, task_id).get("results")
        if entry is None:
            _out("FAILED: evidence")
            _out(f"{store.pointer_path(task_id)} names no results record of {task_id}")
            return False
        data = store.load(wt, entry["path"], entry["digest"])
    except KitError as exc:
        _out("FAILED: evidence " + " ".join(str(exc).replace(str(wt) + "/", "").split()))
        return False
    new = entry != before
    problems = _results_problems(data, task_id, head if new else None, wt)
    if not problems and not new:
        recorded = data["head"]
        code, _ = run_argv(["git", "-C", str(wt), "merge-base", "--is-ancestor", recorded, head], wt)
        changed = stale_module.changed_inputs(wt, head, data["inputs"]) if code == 0 else {}
        if code != 0:
            problems = [f"head: {recorded} of the latest record is not an ancestor of {head}"]
        elif changed:
            problems = [f"the task's own inputs changed since {recorded} ({_show(', '.join(changed))}): "
                        f"{produce} must append a new results record"]
    base = git(wt, "merge-base", MAIN, head)
    problems += [_show(p) for p in store.integrity(wt, base, {entry["path"]} if new else set())]
    if problems:
        _out("FAILED: evidence")
        for problem in problems:
            _out(problem)
        return False
    allowed = {store.pointer_path(task_id), entry["path"]} if new else set()
    extra = [path for path in _changes(wt) if path not in allowed]
    for path in extra:
        _out(f"FAILED: evidence unexpected change: {path}")
    return not extra


def _stale_step(wt: Path) -> bool:
    """The drift report against the worktree's HEAD: information, never a failure; an unreadable record fails."""
    report = stale_module.drift_report(wt, "HEAD")
    _out(f"drift: {report['drifted']} of {report['checked']} record(s) drifted from HEAD")
    for row in report["tasks"]:
        if row["state"] == "drifted":
            _out(f"{row['task_id']} drifted since {row['head']}: {', '.join(row['changed_inputs'])}")
    for row in report["unreadable"]:
        _out(f"FAILED: stale unreadable record {row['path']}: {row['error']}")
    return not report["unreadable"]


def _frozen_plan(root: Path, tree: Path, task_id: str) -> bytes | None:
    """Main's PLAN.md for the task, when the task's SPEC.md, PLAN.md and review.md in `tree` are byte-identical
    to main's.

    Otherwise prints the failure line of the first file that fails and returns None: a branch may not narrow
    the SPEC it is reviewed against, ease the PLAN it is run by or rewrite the spec-QA record (`review.md`) of
    its own dispatch. All three must exist on main and be the same bytes in `tree`. A review record that main
    does not have is `FAILED: review <path> is missing on main`, whatever the branch holds: the record is part
    of the dispatch commit, so a task without one was dispatched without spec QA and a branch cannot supply it.
    """
    plan = None
    for step, name in (("spec", "SPEC.md"), ("plan", "PLAN.md"), ("review", "review.md")):
        rel = f"program/tasks/{task_id}/{name}"
        try:
            authoritative = _git_bytes(root, "show", f"{MAIN}:{rel}")
        except KitError:
            authoritative = None
        if authoritative is None and name == "review.md":
            _out(f"FAILED: {step} {rel} is missing on {MAIN}")
            return None
        own = tree / rel
        if authoritative is None or (own.read_bytes() if own.is_file() else None) != authoritative:
            _out(f"FAILED: {step} {rel} differs from {MAIN}")
            return None
        if name == "PLAN.md":
            plan = authoritative
    return plan


def _run_steps(checks: dict, task_id: str, wt: Path, trial: Path, authoritative: bytes) -> int:
    for step in ("build", "compile_check"):
        if not _run_commands(step, _commands(checks[step]), trial):
            return 1
    for step in ("test", "scan", "extra_gate"):
        if not _run_commands(step, _commands(checks[step]), wt):
            return 1
    try:
        plan = parse_plan(drop_bom(authoritative.decode("utf-8", "replace")))
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
    return 0 if _stale_step(wt) else 1


def handback(root, task_id: str, branch: str, worktree=None) -> int:
    """The hand-back gate; 0 and a final `OK:` line only when every step passed."""
    root = Path(root).resolve()
    _task_branch(task_id, branch)
    sha = _branch_head(root, branch)
    wt = _task_worktree(root, task_id, branch, worktree)
    checks = load_checks_at(root, MAIN, require=("build", "test", "scan"))
    guarded = _guarded_changes(root, task_id, branch, MAIN, checks["guarded_files"])
    if guarded:
        _out(f"FAILED: guarded files changed: {', '.join(guarded)}")
        return 1
    authoritative = _frozen_plan(root, wt, task_id)
    if authoritative is None:
        return 1
    trial = trial_merge(root, branch)
    code = _with_trial(root, trial, lambda: _run_steps(checks, task_id, wt, trial, authoritative))
    if code:
        return code
    if git(wt, "rev-parse", "HEAD") != sha or _branch_head(root, branch) != sha:
        _out(f"FAILED: {branch} moved during the gate")
        return 1
    _print_ok("handback", branch, sha)
    return 0


def merge_check(root, branch: str) -> int:
    """The controller's independent gate run before a merge; 0 and a final `OK:` line only when all of it passed.

    It does not rely on the hand-back gate having been run: the guarded-file check, then on the trial merge the
    SPEC, PLAN and review.md identity check and every build, compile_check, test, scan and extra_gate command of main's
    `checks.yaml`. The task is the one the branch `task/<ID>` names; any other branch is refused.
    """
    root = Path(root).resolve()
    task_id = _branch_task(branch)
    sha = _branch_head(root, branch)
    checks = load_checks_at(root, MAIN, require=("build", "test", "scan"))
    guarded = _guarded_changes(root, task_id, branch, MAIN, checks["guarded_files"])
    if guarded:
        _out(f"FAILED: guarded files changed: {', '.join(guarded)}")
        return 1
    trial = trial_merge(root, branch)

    def steps() -> int:
        if _frozen_plan(root, trial, task_id) is None:
            return 1
        for step in ("build", "compile_check", "test", "scan", "extra_gate"):
            if not _run_commands(step, _commands(checks[step]), trial):
                return 1
        return 0

    code = _with_trial(root, trial, steps)
    if code:
        return code
    if _branch_head(root, branch) != sha:
        _out(f"FAILED: {branch} moved during the merge check")
        return 1
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


def _record_task(item: dict, gate_id: str) -> tuple[str, str | None]:
    """(the record's name in claim documents, the task whose latest records it means, or None).

    A record names its task as `task: <ID>`; a 0.1 entry `path: program/evidence/<ID>/results.json` still names
    task <ID>. Any other path names no task, so nothing can evidence it.
    """
    if "task" in item:
        task_id = item.get("task")
        if not isinstance(task_id, str) or not _TASK.match(task_id):
            raise KitError(f"gate {_show(gate_id)}: record task must be a task or gate ID: {_show(repr(task_id))}")
        return task_id, task_id
    path = _record_path(item.get("path"), gate_id)
    parts = PurePosixPath(path).parts
    named = parts[2] if len(parts) == 4 and parts[:2] == ("program", "evidence") and _TASK.match(parts[2]) else None
    return path, named


def _gate(root: Path, gate_id: str) -> tuple[str, list[tuple[str, str | None]]]:
    """(environment, [(record name, task)]) of a gate; KitError when it is unknown or malformed."""
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
    if not records:  # nothing to evidence: never "0 documents written" and never "consistent"
        raise KitError(f"gate {_show(gate_id)}: records is empty; a gate with no records claims nothing")
    return entry["environment"], [_record_task(item, gate_id) for item in records]


def _claim_name(record: str) -> str:
    name = record.replace("/", "__")
    return name if name.endswith(".json") else name + ".json"


def _loaded(root: Path, entry: dict | None) -> dict | None:
    """The record a pointer entry names, or None when there is none or it cannot be trusted."""
    if entry is None:
        return None
    try:
        return store.load(root, entry["path"], entry["digest"])
    except KitError:
        return None


def _states(task_id: str | None, own: str, environments: dict[str, bool], index: dict, source: Path):
    """({environment: state}, latest acceptance digest or None) for one record of a gate.

    Evidenced only when the task's latest acceptance accepts, is not blocked there, and binds the task's latest
    results record, and both records are present with their pointed-at digests.
    """
    results = store.latest(index, task_id, "results") if task_id else None
    acceptance = store.latest(index, task_id, "acceptance") if task_id else None
    accepted = _loaded(source, acceptance) or {}
    results_ok = results is not None and _loaded(source, results) is not None
    blocked_by = accepted.get("blocked_by")
    states = {}
    for name, exists in environments.items():
        state = NOT_EVALUATED
        if exists and name == own:
            if isinstance(blocked_by, list) and name in blocked_by:
                state = BLOCKED
            elif (isinstance(blocked_by, list) and accepted.get("decision") == "accepted_development"
                  and results_ok and accepted.get("evidence_sha256") == results["digest"]):
                state = EVIDENCED
        states[name] = state
    return states, acceptance["digest"] if acceptance is not None else None


def _expected_claims(root: Path, gate_id: str) -> dict[str, dict]:
    own, records = _gate(root, gate_id)
    environments = _environments(root)
    if own not in environments:
        raise KitError(f"gate {_show(gate_id)}: environment {_show(own)} is not named in program/environments.yaml")
    commit = git(root, "rev-parse", "HEAD")
    index = store.read_index(root)
    source = root
    docs: dict[str, dict] = {}
    for record, task_id in records:
        name = _claim_name(record)
        if name in docs:
            raise KitError(f"gate {_show(gate_id)}: records collide on the claim file {_show(name)}")
        states, acceptance_sha = _states(task_id, own, environments, index, source)
        docs[name] = {"gate": gate_id, "record": record, "environment": states,
                      "acceptance_sha256": acceptance_sha, "generated_from_commit": commit}
    return docs


def _claims_dir(root: Path, gate_id: str) -> Path:
    return root / "program" / "claims" / gate_id


def _refuse_symlink(directory: Path) -> None:
    if directory.is_symlink():
        raise KitError(f"{_show(directory)} is a symlink; refusing to use the claims directory through it")


def _differing(doc: dict, actual: dict) -> list[str]:
    """The keys in which a claim document on disk differs from its recomputation, the commit excluded."""
    return sorted(key for key in set(doc) | set(actual) if key != "generated_from_commit"
                  and doc.get(key, _MISSING) != actual.get(key, _MISSING))


def _current(path: Path, doc: dict) -> bool:
    """Whether the claim file at `path` already says what `doc` says, whatever commit it was generated from."""
    if not path.is_file() or path.is_symlink():
        return False
    try:
        actual = read_json(path)
    except KitError:
        return False
    return (isinstance(actual, dict) and isinstance(actual.get("generated_from_commit"), str)
            and not _differing(doc, actual))


def _write_claims(root: Path, gate_id: str) -> tuple[list[Path], int]:
    """(every claim path of the gate, how many of them were written)."""
    docs = _expected_claims(root, gate_id)
    directory = _claims_dir(root, gate_id)
    _refuse_symlink(directory)
    paths, written = [], 0
    for name, doc in docs.items():
        paths.append(directory / name)
        if not _current(directory / name, doc):
            write_json(directory / name, doc)
            written += 1
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if entry.name not in docs and (entry.is_file() or entry.is_symlink()):
                entry.unlink()
    return paths, written


def claims(root, gate_id: str) -> list[Path]:
    """Bring `program/claims/<GATE>/<record-name>.json` up to date, one per record; return every claim path.

    A file is rewritten only when something other than `generated_from_commit` would change, so committing
    the claims (which moves HEAD) does not make them dirty again on the next run: the commit a file names is
    HEAD as of its last real change. Files of records no longer in the gate are dropped.
    """
    return _write_claims(Path(root).resolve(), gate_id)[0]


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
        differing = _differing(doc, actual)
        if differing:
            problems.append(f"{_show(rel)}: differs in {_show(', '.join(differing))}")
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if entry.name not in docs:
                problems.append(f"{_show(entry.relative_to(root).as_posix())}: extra file "
                                f"(no such record in {_show(gate_id)})")
    return problems


# --- binding ----------------------------------------------------------------------

def _pointer_at(root: Path, commit: str, task_id: str) -> dict:
    """{kind: entry} of the task's pointer committed at `commit` ({} when it has none); KitError when malformed."""
    rel = store.pointer_path(task_id)
    try:
        raw = _git_bytes(root, "show", f"{commit}:{rel}")
    except KitError:
        return {}
    try:
        text = drop_bom(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise KitError(f"{rel} at {commit} is not UTF-8") from exc
    return store.pointer_from_text(text, task_id, f"{rel} at {commit}")


def binding(root, task_id: str, branch: str) -> list[str]:
    """Problems that stop the task's latest results record from being a true record of its `head` on the branch
    (empty, with a printed line, when it holds).

    Read from git objects only, never from a working tree: the task's pointer on the branch tip names a results
    record that is committed there with that digest, validates and passes the lint; the pointer entry names the
    record's own `head`; that `head` is an ancestor of the tip; every recorded input hash is the sha256 of that
    path's blob at `head`; and no commit since the branch left main modified or deleted a record file. Drift
    (inputs changed between `head` and the tip, the branch's HEAD) is reported on the printed line, never a problem.
    """
    root = Path(root).resolve()
    _task_branch(task_id, branch)
    tip = _branch_head(root, branch)
    pointer = store.pointer_path(task_id)
    try:
        entry = _pointer_at(root, tip, task_id).get("results")
    except KitError as exc:
        return [_show(exc)]
    if entry is None:
        return [f"{pointer} on {_show(branch)} names no results record of {task_id}"]
    rel = entry["path"]
    try:
        raw = _git_bytes(root, "show", f"{tip}:{rel}")
    except KitError:
        return [f"{_show(rel)} is not committed on {_show(branch)}"]
    try:
        data = store.parse(rel, raw, entry["digest"])
    except KitError as exc:
        return [_show(exc)]
    problems = _results_problems(data, task_id, None, root)
    if problems:
        return [f"{_show(rel)}: {problem}" for problem in problems]
    head = data["head"]
    if entry["head"] != head:
        return [f"{pointer}: the results entry names head {_show(entry['head'])}, but {_show(rel)} "
                f"names {head}"]
    code, _ = run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", head, tip], root)
    if code != 0:
        return [f"{_show(rel)}: head {_show(head)} is not an ancestor of {_show(branch)} ({tip})"]
    changed = stale_module.changed_inputs(root, head, data["inputs"])
    if changed:
        path, actual = next(iter(changed.items()))
        found = (f"its blob at the recorded head {head} hashes to {actual}" if actual is not None
                 else f"it is not a file at the recorded head {head}")
        return [f"{_show(rel)}: inputs: {_show(path)} is recorded as {data['inputs'][path]} but {found}"]
    rewritten = store.rewritten(root, git(root, "merge-base", MAIN, tip), tip)
    if rewritten:
        return [_show(rewritten[0])]
    drift = stale_module.changed_inputs(root, tip, data["inputs"])
    state = (f"drifted since {head} at the tip: {_show(', '.join(drift))}" if drift else "fresh at the tip")
    _out(f"binding holds: results record {entry['digest'][:12]} at {head} bound; {state}")
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
        return handback(root, args.task_id, args.branch, worktree=args.worktree)
    if action == "merge-check":
        return merge_check(root, args.branch)
    if action == "binding":
        problems = binding(root, args.task_id, args.branch)
        return _report(problems) if problems else 0
    if action == "claims":
        paths, written = _write_claims(Path(root).resolve(), args.gate)
        unchanged = len(paths) - written
        _out(f"wrote {written} claim documents" + (f", {unchanged} unchanged" if unchanged else ""))
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
    # No subcommand takes a main-branch option: the rules are always read from `main`.
    merge = actions.add_parser("merge-check", help="the controller's gate run before a merge: guarded files, "
                                                   "SPEC, PLAN and review.md, then build, test and scan on a "
                                                   "trial merge")
    merge.add_argument("--branch", required=True, help="the task branch to merge, task/<ID>")
    bind = actions.add_parser("binding", help="QA check: the task's latest results record is a true record of its "
                                              "head; drift to the tip is reported")
    bind.add_argument("task_id", metavar="ID", help="task ID, for example P0-M01-S01-T01")
    bind.add_argument("--branch", required=True, help="the task branch")
    make = actions.add_parser("claims", help="write the claim documents of a gate")
    make.add_argument("gate", metavar="GATE", help="gate ID, for example G-P0")
    check = actions.add_parser("validate", help="fail on drift between claim documents and a recomputation")
    check.add_argument("gate", metavar="GATE", help="gate ID, for example G-P0")


def register(add_command) -> None:
    add_command("gate", cmd_gate, help="gate runner: handback, merge-check, binding, claims, validate",
                configure=_configure)
