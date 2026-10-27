#!/usr/bin/env python3
"""Landing work on main: `pk merge` and the integration round, `pk integrate` (standard library only).

`pk merge <ID>` is the whole per-merge chain in one command, for exactly one task: it refuses a branch QA has
not accepted (a light-tier task instead needs the controller's `--inspected <tip>`), runs the merge check
and requires it to pass, merges with `git merge --no-ff`, records a light-tier task `merged` with the commit
that was inspected, rewrites the claim documents of the gates that name the task, regenerates the hand-off
block and commits exactly the bookkeeping files it wrote. Merges stay one at a time: the command takes one ID
and merges one branch.

"Accepted" is derived, never read from a word in a file: the branch tip's status record must say `done`, and
the evidence committed at the tip must hold what `pk status set <ID> done` requires (the latest acceptance
binds the latest results record, which passes the hand-back checks). The acceptance binds the commit that was
reviewed: after the `head` that record names, the branch may add only the task's own evidence and status
files and clean merges of main. A later commit that changes anything else, or a merge commit whose tree is
not what git makes of its two parents, is refused: it was not reviewed.

An integration round verifies several accepted branches together, once. `pk integrate start NAME ID...`
merges the members' tips onto a branch `round/NAME` (worktree `.worktrees/round-NAME`); `pk integrate check
NAME` runs every check of main's `checks.yaml` once on that combined tree and records the tree it tested;
`pk merge ID --round NAME` then lands each member, one at a time, without running the suite again; and
`pk integrate close NAME` compares the tested tree with main and fails on any difference outside the
controller's documents until `--recheck` has run main's checks over the final tree. The guards: a full-tier
task is never a member (it merges through its own merge check); a member must be accepted before the round
starts; a member whose tip moved, or a main that moved outside the round, voids the round; every member
still passes the guarded-file, tier and definition checks and a trial merge when it lands.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import ctlstate
import gate
import handoff as handoff_module
import rulings as rulings_module
import status as status_module
import store
import tracker
from common import (KitError, PRIMARY_BRANCH, Refusal, find_root, git, read_json, read_yaml, run_argv, utc_now,
                    write_json)

MAIN = PRIMARY_BRANCH
ROUND_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
CONTROLLER_FILES = ("program/HANDOFF.md", ctlstate.STATE_FILE, "program/rulings.md", "program/costs.jsonl",
                    "program/progress.md")
CONTROLLER_DIRS = ("program/claims/", "program/rounds/")
SUITE = ("build", "compile_check", "test", "scan", "extra_gate")


def _controller_path(path: str, status_of=()) -> bool:
    return (path in CONTROLLER_FILES or path.startswith(CONTROLLER_DIRS)
            or path in {f"program/status/{task_id}.json" for task_id in status_of})


def _main_checkout(root: Path, what: str) -> None:
    """KitError unless `root` is the main checkout with nothing staged and no change outside the controller's files."""
    try:
        here = git(root, "symbolic-ref", "HEAD")
    except KitError:
        here = ""
    if here != f"refs/heads/{MAIN}" or Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise KitError(f"run {what} from the main checkout, on {MAIN}")
    if git(root, "diff", "--cached", "--name-only"):
        raise KitError(f"{what}: unstage what is staged first; it commits only the files it writes")
    other = [name for name in git(root, "diff", "--name-only").splitlines() if not _controller_path(name)]
    if other:
        raise KitError(f"{what}: the main checkout has uncommitted changes: {', '.join(other)}")


def _tip_status(root: Path, task_id: str, sha: str) -> str:
    """The task's status as its record at commit `sha` says (`not_started` when it has none there)."""
    try:
        record = json.loads(git(root, "show", f"{sha}:program/status/{task_id}.json"))
    except (KitError, ValueError):
        return "not_started"
    return record.get("status", "not_started") if isinstance(record, dict) else "not_started"


def _own_path(path: str, task_id: str) -> bool:
    """Whether `path` is one the kit writes for the task after its review: all a branch may add then.

    The status file, and under `program/evidence/<ID>/` the pointer `latest.json`, a record name
    (`<utc>-<sha256[:12]>.json`) directly in `results/` or `acceptance/`, and the implementer's `report.md`.
    Anything else there (`produce.py`, a `conftest.py`, a test file, in those folders too) is work.
    """
    folder = f"program/evidence/{task_id}/"
    if path.startswith((f"{folder}results/", f"{folder}acceptance/")):
        try:  # only a record's own name, directly in results/ or acceptance/; its bytes: store.integrity
            store.check_rel(path)
        except KitError:
            return False
        return True
    return path in (f"program/status/{task_id}.json", f"{folder}latest.json", f"{folder}report.md")


def _names(root: Path, *args: str) -> list[str]:
    return gate._git_fields(root, *args, "--name-only", "--no-renames", "-z")


def _late_work(root: Path, task_id: str, head: str, sha: str) -> str | None:
    """Why the commits after the reviewed commit `head` stop the tip `sha` from landing, or None.

    Every commit of `head..sha` that is not on main is looked at. A commit with one parent may change only the
    task's own evidence and status files. A merge commit must be what git makes of its two parents (outside
    those same files): a merge that carries an edit of its own, or a conflict resolved by hand, is work nobody
    reviewed.
    """
    again = (f"re-run the hand-back gate so a new results record names the tip, and have QA review and accept "
             f"that record (pk worktree review {task_id})")
    for line in git(root, "rev-list", "--parents", f"{head}..{sha}", "--not", MAIN).splitlines():
        commit, *parents = line.split()
        if len(parents) == 1:
            late = [path for path in _names(root, "diff-tree", "-r", "--no-commit-id", commit)
                    if not _own_path(path, task_id)]
            what = "was committed after"
        elif len(parents) == 2:
            try:  # exit 1 is a conflict: the tree it still writes holds the conflict markers, so it differs
                made = subprocess.run(["git", "-C", str(root), "merge-tree", "--write-tree", *parents],
                                      capture_output=True, text=True)
            except OSError as exc:
                raise KitError(f"cannot run git merge-tree: {exc}") from exc
            tree = made.stdout.split()[0] if made.returncode in (0, 1) and made.stdout.split() else None
            if tree is None:
                return (f"{task_id}: cannot re-derive the merge commit {commit[:12]} made after the reviewed commit "
                        f"{head[:12]} (git merge-tree --write-tree needs git 2.38 or later); {again}")
            late = [path for path in _names(root, "diff", tree, commit) if not _own_path(path, task_id)]
            what = "is a merge that is not what git makes of its two parents; it was made after"
        else:
            late, what = ["(a merge of more than two parents)"], "was made after"
        if late:
            return (f"{task_id}: {commit[:12]} {what} the reviewed commit {head[:12]} and changes "
                    f"{', '.join(late)}: after the commit QA accepted, a branch adds only its status file and, under "
                    f"program/evidence/{task_id}/, latest.json, report.md and records directly in results/ and acceptance/; "
                    f"{again}")
    return None


def _accepted_problem(root: Path, task_id: str, sha: str, tier: str) -> str | None:
    """Why the branch tip is not ready to land, or None.

    A light-tier task lands from `in_progress`. Any other task must be accepted, and that is derived here from
    the evidence committed at the tip (in a temporary detached worktree, removed again), exactly as
    `pk status set <ID> done` derives it: a status file that says `done` is not an acceptance. Whether the full
    tier's quota applies is read from main's record (`tier`), never from the branch's. Then nothing but the
    task's own evidence and status files may have changed since the commit that was reviewed.
    """
    held = _tip_status(root, task_id, sha)
    if tier == "light":
        return None if held == "in_progress" else f"{task_id}: a light-tier task lands from in_progress, not {held}"
    if held != "done":
        return (f"{task_id}: its branch says {held}; only a task QA has set done is merged (pk status set "
                f"{task_id} done --role qa, after pk evidence accept)")
    trial = Path(tempfile.mkdtemp(prefix="pk-trial-"))
    try:
        git(root, "worktree", "add", "--detach", str(trial), sha)
    except KitError:
        shutil.rmtree(trial, ignore_errors=True)  # git never registered it: this is the empty mkdtemp directory
        raise
    head, broken = None, []
    on_main = gate.status_at(root, task_id)  # the tier is main's; only a record from before tiers is exempt
    full = tier == "full" and not (on_main is not None and "tier" not in on_main)
    try:
        problem = status_module._acceptance_problem(trial, task_id, full=full)
        if problem is None:
            head = store.load_latest(trial, task_id, "results")[1]["head"]
            broken = store.integrity(trial, git(root, "merge-base", MAIN, sha), schema=True)  # what pk evidence validate checks
    except KitError as exc:
        problem = f"{task_id}: {exc}"
    finally:
        leftover = gate._remove_trial(root, trial)
    if leftover:
        raise KitError(leftover)
    if problem:
        return (f"{' '.join(problem.replace(str(trial) + '/', '').split())}: its branch says done, but the evidence "
                "committed at its tip does not accept it, and only an accepted task is merged")
    late = _late_work(root, task_id, head, sha)
    if late or not broken:
        return late
    return (f"{task_id}: the evidence at the tip of task/{task_id} fails the store's integrity check: "
            f"{' '.join(broken[0].replace(str(trial) + '/', '').split())}"
            + (f" (and {len(broken) - 1} more)" if len(broken) > 1 else ""))


def _gates_naming(root: Path, task_id: str) -> list[str]:
    """The gates of `program/gates.yaml` that list `task_id` among their records."""
    path = root / "program" / "gates.yaml"
    if not path.is_file():
        return []
    data = read_yaml(path)
    gates = data.get("gates") if isinstance(data, dict) else None
    found = []
    for gate_id in gates if isinstance(gates, dict) else {}:
        try:
            _, records = gate._gate(root, gate_id)
        except KitError:
            continue
        if any(named == task_id for _, named in records):
            found.append(gate_id)
    return found


def _quiet(function, *args, **kwargs):
    """Run a gate function, returning (its result, the lines it printed) without printing them."""
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = function(*args, **kwargs)
    return result, out.getvalue().splitlines()


# --- integration rounds ----------------------------------------------------------------

def _round_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not ROUND_NAME.match(name):
        raise KitError(f"not a usable round name: {name!r} (lower-case letters, digits and '-')")
    return root / "program" / "rounds" / f"{name}.json"


def _round(root: Path, name: str) -> dict:
    path = _round_path(root, name)
    if not path.is_file():
        raise KitError(f"no integration round named {name} (pk integrate start {name} ID...)")
    data = read_json(path)
    if not (isinstance(data, dict) and isinstance(data.get("members"), list) and isinstance(data.get("base"), str)):
        raise KitError(f"{path}: not a round record")
    return data


def _outside_round(root: Path, data: dict) -> list[str]:
    """Commits on main since the round's base that are neither a member's merge nor controller documents only."""
    members = {member["sha"]: member["id"] for member in data["members"]}
    light = [member["id"] for member in data["members"] if member["tier"] == "light"]
    strangers = []
    for commit in git(root, "rev-list", "--first-parent", f"{data['base']}..{MAIN}").splitlines():
        parents = git(root, "rev-list", "--parents", "-n", "1", commit).split()[1:]
        if len(parents) == 2 and parents[1] in members:
            continue
        changed = git(root, "diff", "--name-only", f"{parents[0]}..{commit}").splitlines() if parents else ["(root)"]
        if not all(_controller_path(name, light) for name in changed):
            strangers.append(commit)
    return strangers


def round_start(root: Path, name: str, task_ids: list[str]) -> Path:
    """Merge the members' tips onto `round/<name>` in `.worktrees/round-<name>`; nothing is left on a refusal."""
    root = Path(root).resolve()
    _main_checkout(root, "pk integrate start")
    path = _round_path(root, name)
    worktree = root / ".worktrees" / f"round-{name}"
    if path.exists() or worktree.exists() or run_argv(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"refs/heads/round/{name}"], root)[0] == 0:
        raise KitError(f"round {name} already exists")
    if len(task_ids) < 2 or len(set(task_ids)) != len(task_ids):
        raise KitError("an integration round takes two or more different tasks")
    members = []
    for task_id in task_ids:
        sha = gate._branch_head(root, f"task/{task_id}")
        meta = gate.task_meta(root, task_id)
        if meta["tier"] == "full":
            raise KitError(f"{task_id} is full-tier: a full-tier task is never a member of an integration round; "
                           "it merges through its own merge check")
        problem = _accepted_problem(root, task_id, sha, meta["tier"])
        if problem:
            raise KitError(problem + " before it joins a round")
        members.append({"id": task_id, "branch": f"task/{task_id}", "sha": sha, "tier": meta["tier"]})
    base = git(root, "rev-parse", MAIN)
    trial = Path(tempfile.mkdtemp(prefix="pk-trial-"))
    git(root, "worktree", "add", "--detach", str(trial), MAIN)
    try:
        for member in members:
            code, output = run_argv(["git", "-C", str(trial), "merge", "--no-ff", "-m",
                                     f"round {name}: merge {member['branch']}", member["sha"]], trial)
            if code != 0:
                run_argv(["git", "-C", str(trial), "merge", "--abort"], trial)
                raise KitError(f"round {name}: {member['branch']} does not merge after the members before it: "
                               f"{(output.strip().splitlines() or ['conflict'])[-1]}")
        head = git(trial, "rev-parse", "HEAD")
    finally:
        problem = gate._remove_trial(root, trial)
        if problem:
            print(f"warning: {problem}")
    git(root, "branch", "--", f"round/{name}", head)
    git(root, "worktree", "add", "--", str(worktree), f"round/{name}")
    write_json(path, {"name": name, "base": base, "head": head, "members": members, "tree": "", "checked_at": "",
                      "merged": [], "closed_at": "", "rechecked": []})
    return worktree


def round_check(root: Path, name: str) -> int:
    """Every check of main's checks.yaml, once, on the combined tree; records the tested tree and prints the OK line."""
    root = Path(root).resolve()
    data = _round(root, name)
    worktree = root / ".worktrees" / f"round-{name}"
    if not worktree.is_dir() or git(worktree, "rev-parse", "HEAD") != data["head"] or gate._changes(worktree):
        raise KitError(f"round {name}: {worktree} must be clean and at the round's head {data['head']}")
    if _outside_round(root, data):
        raise KitError(f"round {name}: {MAIN} moved since the round started; start a new round")
    checks = gate.load_checks_at(root, MAIN, require=("build", "test", "scan"))
    for member in data["members"]:
        task_id, branch = member["id"], member["branch"]
        if gate._branch_head(root, branch) != member["sha"]:
            raise KitError(f"round {name}: {branch} moved since the round started; start a new round")
        meta = gate.task_meta(root, task_id)
        guarded = gate._guarded_changes(root, task_id, branch, MAIN, checks["guarded_files"])
        if guarded:
            gate._out(f"FAILED: guarded files changed: {', '.join(guarded)}")
            return 1
        if meta["tier"] == "full":
            gate._out(f"FAILED: tier {task_id} is full-tier and cannot be a member of a round")
            return 1
        if not (gate._tier_step(root, task_id, branch, checks, meta)
                and gate._definition(root, worktree, task_id, meta) is not None
                and gate._tier_unchanged(worktree, task_id, meta)):
            return 1
    for step in SUITE:
        if not gate._run_commands(step, gate._commands(checks[step]), worktree):
            return 1
    if gate._changes(worktree):
        gate._out(f"FAILED: round the checks left {worktree} dirty")
        return 1
    data.update(tree=git(root, "rev-parse", f"{data['head']}^{{tree}}"), checked_at=utc_now())
    write_json(_round_path(root, name), data)
    gate._print_ok("round", f"round/{name}", data["head"])
    return 0


def _round_member(root: Path, name: str, task_id: str, sha: str) -> dict:
    """The round record, after checking the round was checked and `task_id` at `sha` is an unmerged member of it."""
    data = _round(root, name)
    if not data.get("tree") or data.get("closed_at"):
        raise KitError(f"round {name} has not passed its combined check (pk integrate check {name}), or is closed")
    member = next((item for item in data["members"] if item["id"] == task_id), None)
    if member is None:
        raise KitError(f"{task_id} is not a member of round {name}")
    if member["sha"] != sha:
        raise KitError(f"task/{task_id} moved since round {name} was checked ({member['sha']} then, {sha} now): "
                       "the tested tree no longer holds it; start a new round or merge it on its own")
    strangers = _outside_round(root, data)
    if strangers:
        raise KitError(f"{MAIN} moved outside round {name} ({strangers[0][:12]}): merge the rest on their own, or "
                       "start a new round")
    problem = _round_forged(root, name, data)
    if problem:
        raise KitError(f"round {name}: {problem}; the record is not what pk integrate start and check wrote: "
                       "start a new round")
    return data


def _round_forged(root: Path, name: str, data: dict) -> str | None:
    """Why the round record is not a description of the branch `round/<name>`, or None.

    Re-derived from git, so a record written by hand covers nothing: the branch is at the record's `head`, the
    tested `tree` is that commit's tree, and from `base` the head is one merge per member, in order, each of
    exactly the member's recorded commit.
    """
    try:
        head = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/round/{name}^{{commit}}")
    except KitError:
        return f"branch round/{name} not found"
    if head != data.get("head"):
        return f"branch round/{name} is at {head}, the record says {data.get('head')}"
    if git(root, "rev-parse", f"{head}^{{tree}}") != data["tree"]:
        return f"the tested tree {data['tree']} is not the tree of round/{name}"
    chain = [line.split() for line in git(root, "rev-list", "--first-parent", "--reverse", "--parents",
                                          f"{data['base']}..{head}").splitlines()]
    wanted, parent = [member["sha"] for member in data["members"]], data["base"]
    if len(chain) != len(wanted):
        return f"round/{name} is {len(chain)} commit(s) from its base, the record lists {len(wanted)} member(s)"
    for (commit, *parents), sha in zip(chain, wanted):
        if parents != [parent, sha]:
            return f"{commit[:12]} of round/{name} is not the merge of the member at {sha[:12]}"
        parent = commit
    return None


def round_close(root: Path, name: str, recheck: bool = False, commit: bool = True, trailers=()) -> list[str]:
    """Compare the tested tree with main; lines to print. KitError while a difference is not re-checked."""
    root = Path(root).resolve()
    _main_checkout(root, "pk integrate close")
    data = _round(root, name)
    if not data.get("tree"):
        raise KitError(f"round {name} has not passed its combined check")
    forged = _round_forged(root, name, data)
    if forged:
        raise KitError(f"round {name}: {forged}; the record is not what pk integrate start and check wrote")
    waiting = [m["id"] for m in data["members"] if run_argv(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", m["sha"], MAIN], root)[0] != 0]
    if waiting:
        raise KitError(f"round {name}: not merged yet: {', '.join(waiting)} (pk merge <ID> --round {name})")
    light = [m["id"] for m in data["members"] if m["tier"] == "light"]
    differing = [path for path in git(root, "diff", "--name-only", data["tree"], MAIN).splitlines()
                 if not _controller_path(path, light)]
    lines = []
    if differing:
        lines += [f"changed since the tested tree: {path}" for path in differing]
        if not recheck:
            raise KitError("; ".join([*lines, f"re-check them: pk integrate close {name} --recheck runs "
                                              f"{MAIN}'s checks once over the final tree"]))
        checks = gate.load_checks_at(root, MAIN, require=("build", "test", "scan"))
        trial = Path(tempfile.mkdtemp(prefix="pk-trial-"))
        git(root, "worktree", "add", "--detach", str(trial), MAIN)
        code = gate._with_trial(root, trial, lambda: 0 if all(
            gate._run_commands(step, gate._commands(checks[step]), trial) for step in SUITE) else 1)
        if code:
            raise KitError(f"round {name}: the re-check failed on {MAIN}; fix it before anything else merges")
        lines.append(f"re-checked {len(differing)} changed path(s): {MAIN}'s checks pass on the final tree")
    else:
        lines.append(f"round {name}: {MAIN} matches the tested tree {data['tree']}")
    data.update(closed_at=utc_now(), rechecked=differing)
    write_json(_round_path(root, name), data)
    worktree = root / ".worktrees" / f"round-{name}"
    if worktree.is_dir() and not gate._changes(worktree):
        git(root, "worktree", "remove", "--", str(worktree))
        lines.append(f"removed: worktree {worktree} (branch round/{name} is kept: it names the tested tree)")
    if commit:
        sha = tracker.commit_paths(root, [f"program/rounds/{name}.json"], f"chore(program): round {name} closed",
                                   trailers)
        lines.append(f"committed {sha}" if sha else "nothing to commit")
    return lines


# --- pk merge ---------------------------------------------------------------------------

def merge(root: Path, task_id: str, inspected: str | None = None, round_name: str | None = None,
          commit: bool = True, trailers=()) -> list[str]:
    """Land one task: check, merge, record, claims, hand-off, commit. Returns the lines to print (under ten)."""
    root = Path(root).resolve()
    _main_checkout(root, "pk merge")
    branch = f"task/{task_id}"
    sha = gate._branch_head(root, branch)
    meta = gate.task_meta(root, task_id)
    tier = meta["tier"] + ("+fast" if meta["fast"] else "")
    problem = rulings_module.rewritten(root) or _accepted_problem(root, task_id, sha, meta["tier"])
    if problem:
        raise KitError(problem)
    data = None
    if round_name is not None:
        if meta["tier"] == "full":
            raise KitError(f"{task_id} is full-tier: it merges through its own merge check, never through a round")
        data = _round_member(root, round_name, task_id, sha)
    code, printed = _quiet(gate.merge_check, root, branch, inspected=inspected, suite=data is None)
    if code != 0 or (data is None and (not printed or printed[-1] != f"OK: {branch} merges into {MAIN} at {sha}")):
        raise KitError("; ".join(["the merge check did not pass", *printed[-6:]]))
    lines = [printed[-1] if data is None else f"round {round_name}: {branch} at {sha} is in the tested tree"]
    message = ["-m", f"Merge {branch} ({tier} tier)"] + [part for line in trailers for part in ("-m", line)]
    code, output = run_argv(["git", "-C", str(root), "merge", "--no-ff", *message, f"refs/heads/{branch}"], root)
    if code != 0:
        run_argv(["git", "-C", str(root), "merge", "--abort"], root)
        raise KitError(f"git merge of {branch} failed: {(output.strip().splitlines() or [f'exit {code}'])[-1]}")
    lines.append(f"merged: {branch} into {MAIN} at {git(root, 'rev-parse', 'HEAD')}")
    wrote = ["program/HANDOFF.md", ctlstate.STATE_FILE, "program/costs.jsonl", "program/rulings.md"]
    problems = []
    if meta["tier"] == "light":
        try:
            status_module.transition(root, task_id, "merged", "controller", note=f"inspected at {sha}",
                                     inspected=sha)
            wrote.append(f"program/status/{task_id}.json")
            lines.append(f"{task_id}: merged (light tier: no QA, nothing evidenced)")
        except KitError as exc:
            problems.append(f"status: {exc}")
    for gate_id in _gates_naming(root, task_id):
        try:
            gate._write_claims(root, gate_id)
            wrote.append(f"program/claims/{gate_id}")
            lines.append(f"claims: {gate_id} rewritten")
        except KitError as exc:
            problems.append(f"claims of {gate_id}: {exc}")
    if data is not None:
        data["merged"].append(task_id)
        write_json(_round_path(root, round_name), data)
        wrote.append(f"program/rounds/{round_name}.json")
        left = [member["id"] for member in data["members"] if member["id"] not in data["merged"]]
        lines.append(f"round {round_name}: {len(left)} member(s) still to merge ({', '.join(left)})" if left else
                     f"round {round_name}: every member is merged; close it now: pk integrate close {round_name}")
    try:
        lines.append("hand-off: updated" if handoff_module.update(root) else "hand-off: unchanged")
        problems += handoff_module.budget_problems(root)
    except KitError as exc:
        problems.append(f"hand-off: {exc}")
    if commit:
        done = tracker.commit_paths(root, wrote, f"chore(program): {task_id} merged ({tier} tier); hand-off and claims",
                                    trailers)
        lines.append(f"committed {done}" if done else "nothing to commit")
    if problems:  # merged and committed, then the reasons, on both streams (KF-15)
        raise Refusal("; ".join([*lines, *problems]))
    return lines


# --- commands ---------------------------------------------------------------------------

def cmd_merge(args: argparse.Namespace) -> int:
    print("\n".join(merge(find_root(), args.task_id, inspected=args.inspected, round_name=args.round,
                          commit=not args.no_commit, trailers=args.trailer)))
    return 0


def cmd_integrate(args: argparse.Namespace) -> int:
    action = getattr(args, "integrate_action", None)
    root = find_root()
    if action == "start":
        print(round_start(root, args.name, args.task_ids))
    elif action == "check":
        return round_check(root, args.name)
    elif action == "close":
        print("\n".join(round_close(root, args.name, recheck=args.recheck, commit=not args.no_commit,
                                    trailers=args.trailer)))
    else:
        raise KitError("integrate needs an action: start, check or close")
    return 0


def _configure_merge(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", metavar="ID", help="the one task to land; its branch is task/<ID>")
    parser.add_argument("--inspected", metavar="SHA", help="light tier: the branch tip whose diff you read")
    parser.add_argument("--round", metavar="NAME", help="the integration round whose combined check covers it")
    parser.add_argument("--no-commit", action="store_true", dest="no_commit",
                        help="leave the bookkeeping files uncommitted")
    parser.add_argument("--trailer", action="append", default=[], metavar="LINE",
                        help="a line to end the merge and bookkeeping commit messages with: your attribution line")


def _configure_integrate(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="integrate_action", metavar="action", parser_class=type(parser))
    starter = actions.add_parser("start", help="merge accepted branches onto round/<NAME> for one combined check")
    starter.add_argument("name", metavar="NAME")
    starter.add_argument("task_ids", metavar="ID", nargs="+")
    actions.add_parser("check", help="run main's checks once on the combined tree").add_argument("name", metavar="NAME")
    closer = actions.add_parser("close", help="compare the tested tree with main after the merges")
    closer.add_argument("name", metavar="NAME")
    closer.add_argument("--recheck", action="store_true", help="run main's checks over the final tree")
    closer.add_argument("--no-commit", action="store_true", dest="no_commit")
    closer.add_argument("--trailer", action="append", default=[], metavar="LINE")


def register(add_command) -> None:
    add_command("merge", cmd_merge, help="land one task: merge check, merge, claims, hand-off and one commit",
                configure=_configure_merge)
    add_command("integrate", cmd_integrate, help="integration rounds: start, check, close",
                configure=_configure_integrate)
