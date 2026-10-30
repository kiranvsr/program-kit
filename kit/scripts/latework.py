#!/usr/bin/env python3
"""The late-work rule: what a task branch may add after the commit QA reviewed (standard library only).

A results record binds the work to one commit, its `head`. After that commit a branch may add only what the kit
writes for the task: its status file and, under `program/evidence/<ID>/`, the pointer `latest.json`, the
implementer's `report.md`, and records directly in `results/`, `acceptance/` and (0.5.3) `plants/`. Anything else
(a producer's side file of extra planted-bug verdicts, a test, a fix) is work nobody reviewed.

`pk merge` has always refused such a tip. From 0.5.3 the same check runs at the first step that can see the file:
`pk evidence accept`, `pk status set <ID> done` and, before its long run, `pk gate handback`, so a file merge would
refuse is reported where it is still cheap to fix (F-0403 in the program this came from: a side file of extra
planted-bug verdicts was refused only at the merge, after QA had accepted). Planted bugs judged after the gate are
recorded by `pk gate plants`, as kit records bound to the gate record, never in a file of the producer's.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import store
from common import KitError, PRIMARY_BRANCH, git

MAIN = PRIMARY_BRANCH
OWN_FOLDERS = ("results/", "acceptance/", f"{store.PLANTS}/")


def own_path(path: str, task_id: str) -> bool:
    """Whether `path` is one the kit writes for the task after its review: all a branch may add then.

    The status file, and under `program/evidence/<ID>/` the pointer `latest.json`, a record name
    (`<utc>-<sha256[:12]>.json`) directly in `results/`, `acceptance/` or `plants/`, and the implementer's
    `report.md`. Anything else (`produce.py`, a `conftest.py`, a test file, a side file of planted-bug verdicts, in
    those folders too) is work.
    """
    folder = f"program/evidence/{task_id}/"
    if path.startswith(tuple(folder + sub for sub in OWN_FOLDERS)):
        try:  # only a record's own name, directly in its folder; its bytes: store.integrity
            store.check_rel(path)
        except KitError:
            return False
        return True
    return path in (f"program/status/{task_id}.json", f"{folder}latest.json", f"{folder}report.md")


def allowed(task_id: str) -> str:
    """The sentence naming what a branch may add after the reviewed commit."""
    return (f"after the commit QA reviews, a branch adds only its status file and, under program/evidence/{task_id}/, "
            f"latest.json, report.md and records directly in results/, acceptance/ and plants/ (planted bugs judged "
            f"after the gate are recorded with pk gate plants {task_id} --branch task/{task_id}, never in a file of "
            f"the producer's)")


def again(task_id: str) -> str:
    return (f"re-run the hand-back gate so a new results record names the tip, and have QA review and accept "
            f"that record (pk worktree review {task_id})")


def _names(root: Path, *args: str) -> list[str]:
    raw = subprocess.run(["git", "-C", str(root), *args, "--name-only", "--no-renames", "-z"],
                         capture_output=True)
    if raw.returncode != 0:
        raise KitError(f"git {' '.join(args)} failed: {raw.stderr.decode('utf-8', 'replace').strip()}")
    return [name for name in raw.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def _commit_exists(root: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          capture_output=True).returncode == 0


def _commits(root: Path, head: str, sha: str) -> list[list[str]]:
    """[commit, *parents] of every commit of `head..sha` that is not on main (all of them without a main)."""
    span = ["rev-list", "--parents", f"{head}..{sha}"] + (["--not", MAIN] if _commit_exists(root, MAIN) else [])
    return [line.split() for line in git(root, *span).splitlines()]


def _late_paths(root: Path, task_id: str, commit: str, parents: list[str]) -> tuple[list[str], str] | str:
    """(late paths, how the commit is described) of one commit, or a refusal string when it cannot be checked."""
    if len(parents) == 1:
        return ([path for path in _names(root, "diff-tree", "-r", "--no-commit-id", commit)
                 if not own_path(path, task_id)], "was committed after")
    if len(parents) == 2:
        try:  # exit 1 is a conflict: the tree it still writes holds the conflict markers, so it differs
            made = subprocess.run(["git", "-C", str(root), "merge-tree", "--write-tree", *parents],
                                  capture_output=True, text=True)
        except OSError as exc:
            raise KitError(f"cannot run git merge-tree: {exc}") from exc
        tree = made.stdout.split()[0] if made.returncode in (0, 1) and made.stdout.split() else None
        if tree is None:
            return "cannot re-derive"
        return ([path for path in _names(root, "diff", tree, commit) if not own_path(path, task_id)],
                "is a merge that is not what git makes of its two parents; it was made after")
    return ["(a merge of more than two parents)"], "was made after"


def late_work(root, task_id: str, head: str, sha: str = "HEAD") -> str | None:
    """Why the commits after the reviewed commit `head` stop the tip `sha` from landing, or None.

    Every commit of `head..sha` that is not on main is looked at. A commit with one parent may change only the
    task's own evidence and status files. A merge commit must be what git makes of its two parents (outside
    those same files): a merge that carries an edit of its own, or a conflict resolved by hand, is work nobody
    reviewed.
    """
    root = Path(root)
    for commit, *parents in _commits(root, head, sha):
        found = _late_paths(root, task_id, commit, parents)
        if isinstance(found, str):
            return (f"{task_id}: cannot re-derive the merge commit {commit[:12]} made after the reviewed commit "
                    f"{head[:12]} (git merge-tree --write-tree needs git 2.38 or later); {again(task_id)}")
        late, what = found
        if late:
            return (f"{task_id}: {commit[:12]} {what} the reviewed commit {head[:12]} and changes "
                    f"{', '.join(late)}: {allowed(task_id)}; {again(task_id)}")
    return None


def late_plants(root, task_id: str, head: str, sha: str = "HEAD") -> list[str]:
    """The `plants/` record paths the commits of `head..sha` (not on main) added."""
    root = Path(root)
    prefix = f"{store.BASE}/{task_id}/{store.PLANTS}/"
    found: list[str] = []
    for commit, *parents in _commits(root, head, sha):
        if len(parents) != 1:
            continue
        for path in _names(root, "diff-tree", "-r", "--no-commit-id", "--diff-filter=A", commit):
            if path.startswith(prefix) and own_path(path, task_id) and path not in found:
                found.append(path)
    return sorted(found)


def uncommitted(root, task_id: str) -> list[str]:
    """Uncommitted paths under the task's evidence folder that the kit does not write (merge would refuse them)."""
    folder = f"program/evidence/{task_id}/"
    raw = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all", "--",
                          folder], capture_output=True)
    if raw.returncode != 0:
        return []
    fields = raw.stdout.decode("utf-8", "surrogateescape").split("\0")
    paths, index = [], 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if entry[0] in "RC" or entry[1] in "RC":
            index += 1  # the rename's source path
        path = entry[3:]
        if not own_path(path, task_id) and path not in paths:
            paths.append(path)
    return sorted(paths)


def early_problem(root, task_id: str, head: str, step: str) -> str | None:
    """What `pk merge` would refuse in the checkout at `root`, reported at `step` (accept, done or the gate), or None.

    The committed late work after the reviewed commit `head`, then uncommitted files under the task's evidence
    folder that the kit does not write. A `head` that is not a commit here cannot be checked and is left to the
    checks that bind the record to its commit.
    """
    root = Path(root)
    if not _commit_exists(root, head):
        return None
    late = late_work(root, task_id, head)
    if late:
        return f"{step}: {late} (pk merge refuses this tip; it is reported here so it is fixed before the merge)"
    loose = uncommitted(root, task_id)
    if loose:
        return (f"{step}: {task_id}: {', '.join(loose)} under program/evidence/{task_id}/ is not a file the kit "
                f"writes, and pk merge refuses it after the reviewed commit {head[:12]}: {allowed(task_id)}")
    return None
