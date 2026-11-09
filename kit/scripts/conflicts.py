#!/usr/bin/env python3
"""The merge conflicts the kit resolves itself when a task branch lands on main (standard library only).

A conflict that says nothing about the work is resolved the same way everywhere the kit merges a task: the trial
merge of the hand-back gate and the merge check (`gate.trial_merge`), `pk merge`'s real `git merge --no-ff` and the
trial merges of an integration round.

- The task's own status file, `program/status/<ID>.json` (0.6.0, KF-37): the branch holds QA's record; main's copy
  is the dispatch record or one a controller copied there by hand. The branch's version is taken. The tier check on
  the trial merge (`gate._tier_unchanged`) still refuses a branch record whose tier, fast path or ruling is not
  main's, so taking the branch's record never changes a tier.

Any other conflicted path stays a conflict and is named. Nothing here decides anything a person reviewed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from common import KitError, PRIMARY_BRANCH, git

MAIN = PRIMARY_BRANCH
REGULAR = ("100644", "100755")


def status_path(task_id: str) -> str:
    return f"program/status/{task_id}.json"


def task_of(branch: str) -> str | None:
    """The task ID of a branch `task/<ID>`, or None for any other branch."""
    return branch[len("task/"):] if isinstance(branch, str) and branch.startswith("task/") else None


def parse_stages(fields) -> dict[str, dict[int, tuple[str, str]]]:
    """{path: {stage: (mode, object)}} from `<mode> <object> <stage>\\t<path>` entries (`git ls-files -u -z`)."""
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


def _blob(root, oid: str) -> bytes:
    raw = subprocess.run(["git", "-C", str(root), "cat-file", "blob", oid], capture_output=True)
    if raw.returncode != 0:
        raise KitError(f"cannot read the object {oid[:12]}: {raw.stderr.decode('utf-8', 'replace').strip()}")
    return raw.stdout


def resolution(root, path: str, stages: dict[int, tuple[str, str]], branch: str,
               task_id: str | None) -> tuple[bytes | None, str]:
    """(the content that resolves the conflicted `path`, the line saying so), or (None, how the conflict is named).

    `task_id` is the task whose branch `branch` is merged in as theirs (stage 3); None applies no status rule.
    """
    regular = all(mode in REGULAR for mode, _ in stages.values())
    if task_id and path == status_path(task_id):
        if 3 in stages and regular:
            return _blob(root, stages[3][1]), f"resolved: {path}: took {branch}'s record (the task's own status file)"
        return None, f"{path} (deleted on one side)"
    return None, path


def resolve(root, staged: dict, branch: str, task_id: str | None) -> tuple[dict[str, bytes], list[str], list[str]]:
    """({path: content} for the conflicts resolved, the lines saying so, the conflicts that remain, named)."""
    contents, lines, remaining = {}, [], []
    for path in sorted(staged):
        content, said = resolution(root, path, staged[path], branch, task_id)
        if content is None:
            remaining.append(said)
        else:
            contents[path] = content
            lines.append(said)
    return contents, lines, remaining


def resolve_index(where, branch: str, task_id: str | None) -> tuple[list[str], list[str]]:
    """After `git merge` of `branch` stopped on conflicts in the worktree `where`: resolve what this module resolves
    and stage it. Returns (the lines saying what was resolved, the conflicts that remain, named); when any remain,
    nothing is written and the caller aborts the merge."""
    staged = index_stages(where)
    if not staged:
        return [], []
    contents, lines, remaining = resolve(where, staged, branch, task_id)
    if remaining:
        return lines, remaining
    for path, content in contents.items():
        (Path(where) / path).write_bytes(content)
    git(where, "add", "--", *contents)
    return lines, []
