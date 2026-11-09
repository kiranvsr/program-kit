#!/usr/bin/env python3
"""The merge conflicts the kit resolves itself when a task branch lands on main (standard library only).

A conflict that says nothing about the work is resolved the same way everywhere the kit merges a task: the trial
merge of the hand-back gate and the merge check (`gate.trial_merge`), `pk merge`'s real `git merge --no-ff`, the
trial merges of an integration round, `pk merge check` (`check`, on `git merge-tree`: no checkout) and the late-work
judgement of a merge of main into the branch after the reviewed commit (`latework`).

- The task's own status file, `program/status/<ID>.json` (0.6.0, KF-37): the branch holds QA's record; main's copy
  is the dispatch record or one a controller copied there by hand. The branch's version is taken. The tier check on
  the trial merge (`gate._tier_unchanged`) still refuses a branch record whose tier, fast path or ruling is not
  main's, so taking the branch's record never changes a tier.
- A file `checks.yaml` lists under `merge.union_files` (0.6.0, KF-38; off by default; paths, directories and globs
  as `gate._under` reads them): an append-only file, such as a migrations journal both sides added an entry to, is
  merged as the union of both sides' lines (`git merge-file --union` on the three stages: in each conflicting hunk
  our lines, then theirs). A `.json` file whose union does not read as JSON stays a conflict, named as such.

Any other conflicted path stays a conflict and is named. Nothing here decides anything a person reviewed.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from common import KitError, PRIMARY_BRANCH, git

MAIN = PRIMARY_BRANCH
REGULAR = ("100644", "100755")


def status_path(task_id: str) -> str:
    return f"program/status/{task_id}.json"


def task_of(branch: str) -> str | None:
    """The task ID of a branch `task/<ID>`, or None for any other branch."""
    return branch[len("task/"):] if isinstance(branch, str) and branch.startswith("task/") else None


def union_files(root, ref: str = MAIN) -> list[str]:
    """`merge.union_files` of `program/checks.yaml` at `ref`; [] when there is no checks.yaml there.

    A checks.yaml that is there and does not read is a KitError, as for every other reader of it.
    """
    import gate  # late: gate imports this module

    if subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{ref}:program/checks.yaml"],
                      capture_output=True).returncode != 0:
        return []
    return gate.load_checks_at(root, ref)["merge"]["union_files"]


def parse_stages(fields) -> dict[str, dict[int, tuple[str, str]]]:
    """{path: {stage: (mode, object)}} from `<mode> <object> <stage>\\t<path>` entries (`git ls-files -u -z`, and the
    conflicted-file part of `git merge-tree --write-tree -z`)."""
    found: dict[str, dict[int, tuple[str, str]]] = {}
    for field in fields:
        head, _, path = field.partition("\t")
        parts = head.split()
        if len(parts) != 3 or not path or not parts[2].isdigit():
            continue
        found.setdefault(path, {})[int(parts[2])] = (parts[0], parts[1])
    return found


def index_stages(where) -> dict[str, dict[int, tuple[str, str]]]:
    """The unmerged entries of the index of the worktree `where`, after a `git merge` stopped on conflicts."""
    raw = subprocess.run(["git", "-C", str(where), "ls-files", "-u", "-z"], capture_output=True)
    if raw.returncode != 0:
        return {}
    return parse_stages(raw.stdout.decode("utf-8", "surrogateescape").split("\0"))


def merge_tree(root, ours: str, theirs: str) -> tuple[str | None, dict[str, dict[int, tuple[str, str]]]]:
    """(the tree `git merge-tree --write-tree` makes of `ours` and `theirs`, its conflicted stages); (None, {}) when
    git cannot merge them at all (git older than 2.38, no history in common). The tree of a conflicted merge holds
    the conflict markers."""
    try:
        made = subprocess.run(["git", "-C", str(root), "merge-tree", "--write-tree", "-z", "--no-messages", ours,
                               theirs], capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git merge-tree: {exc}") from exc
    fields = made.stdout.decode("utf-8", "surrogateescape").split("\0")
    if made.returncode not in (0, 1) or not fields[0].strip():
        return None, {}
    return fields[0].strip(), parse_stages(fields[1:])


def _blob(root, oid: str) -> bytes:
    raw = subprocess.run(["git", "-C", str(root), "cat-file", "blob", oid], capture_output=True)
    if raw.returncode != 0:
        raise KitError(f"cannot read the object {oid[:12]}: {raw.stderr.decode('utf-8', 'replace').strip()}")
    return raw.stdout


def _union(root, stages: dict[int, tuple[str, str]]) -> bytes:
    """`git merge-file -p --union` of ours (stage 2), the base (stage 1; empty when both sides added the file) and
    theirs (stage 3)."""
    with tempfile.TemporaryDirectory(prefix="pk-union-") as folder:
        names = []
        for stage, name in ((2, "ours"), (1, "base"), (3, "theirs")):
            path = Path(folder) / name
            path.write_bytes(_blob(root, stages[stage][1]) if stage in stages else b"")
            names.append(str(path))
        made = subprocess.run(["git", "merge-file", "-p", "--union", *names], capture_output=True)
    if made.returncode < 0 or made.returncode > 127:
        raise KitError(f"git merge-file --union failed: {made.stderr.decode('utf-8', 'replace').strip()}")
    return made.stdout


def resolution(root, path: str, stages: dict[int, tuple[str, str]], branch: str, task_id: str | None,
               union=()) -> tuple[bytes | None, str]:
    """(the content that resolves the conflicted `path`, the line saying so), or (None, how the conflict is named).

    `task_id` is the task whose branch `branch` is merged in as theirs (stage 3); None applies no status rule.
    `union` is `merge.union_files`.
    """
    import gate  # late: gate imports this module

    regular = all(mode in REGULAR for mode, _ in stages.values())
    if task_id and path == status_path(task_id):
        if 3 in stages and regular:
            return _blob(root, stages[3][1]), f"resolved: {path}: took {branch}'s record (the task's own status file)"
        return None, f"{path} (deleted on one side)"
    if union and gate._under(path, union):
        if not (2 in stages and 3 in stages and regular):
            return None, f"{path} (merge.union_files: deleted on one side, or not a regular file)"
        merged = _union(root, stages)
        if path.endswith(".json"):
            try:
                json.loads(merged.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None, f"{path} (merge.union_files: the line union of both sides is not valid JSON)"
        return merged, f"resolved: {path}: merged as the line union of both sides (merge.union_files)"
    return None, path


def resolve(root, staged: dict, branch: str, task_id: str | None, union=None,
            ref: str = MAIN) -> tuple[dict[str, bytes], list[str], list[str]]:
    """({path: content} for the conflicts resolved, the lines saying so, the conflicts that remain, named).

    `union` None reads `merge.union_files` from `ref`'s checks.yaml, and only when a conflict is not the status file.
    """
    if union is None:
        union = union_files(root, ref) if any(path != status_path(task_id or "") for path in staged) else []
    contents, lines, remaining = {}, [], []
    for path in sorted(staged):
        content, said = resolution(root, path, staged[path], branch, task_id, union)
        if content is None:
            remaining.append(said)
        else:
            contents[path] = content
            lines.append(said)
    return contents, lines, remaining


def resolve_index(where, branch: str, task_id: str | None, ref: str = MAIN) -> tuple[list[str], list[str]]:
    """After `git merge` of `branch` stopped on conflicts in the worktree `where`: resolve what this module resolves
    and stage it. Returns (the lines saying what was resolved, the conflicts that remain, named); when any remain,
    nothing is written and the caller aborts the merge."""
    staged = index_stages(where)
    if not staged:
        return [], []
    contents, lines, remaining = resolve(where, staged, branch, task_id, ref=ref)
    if remaining:
        return lines, remaining
    for path, content in contents.items():
        (Path(where) / path).write_bytes(content)
    git(where, "add", "--", *contents)
    return lines, []


def check(root, branch: str, main: str = MAIN) -> tuple[str, list[str], list[str]]:
    """`pk merge check`: (main's commit, the resolved lines, the conflicts that remain) of merging `branch` into
    `main` as `git merge-tree` makes it, with the resolutions `pk merge` applies; no checkout, nothing written."""
    sha = git(root, "rev-parse", "--verify", f"{main}^{{commit}}")
    tip = git(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    tree, staged = merge_tree(root, sha, tip)
    if tree is None:
        raise KitError(f"git merge-tree cannot merge {branch} into {main} (it needs git 2.38 or later)")
    _, lines, remaining = resolve(root, staged, branch, task_of(branch), ref=sha) if staged else ({}, [], [])
    return sha, lines, remaining
