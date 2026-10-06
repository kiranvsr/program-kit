#!/usr/bin/env python3
"""Task worktrees: one git worktree and branch per task (standard library only).

`new` creates `<root>/.worktrees/<id lowercased>` on branch `task/<ID>` from the main branch;
`list_all` reports every worktree git knows, with its dirty state. There is deliberately no
remove command: removing a worktree discards work, so it is a human decision.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import register as register_module
from common import KitError, PRIMARY_BRANCH, find_root, git, primary_branch_problem


def _resolves(root: Path, ref: str) -> bool:
    try:
        git(root, "rev-parse", "--verify", "--quiet", ref)
    except KitError:
        return False
    return True


def new(root: Path, task_id: str, main: str = "main") -> Path:
    """Create the worktree and branch for `task_id` and return the worktree path.

    Refuses (KitError, nothing created) for a task ID that is not P<n>-M<nn>-S<nn>-T<nn>,
    when the directory exists, when branch `task/<ID>` exists, or when `main` does not
    name a commit.
    """
    root = Path(root)
    if not isinstance(task_id, str) or not register_module.TASK_ID.match(task_id):
        raise KitError(f"{task_id!r} is not a task id (expected P<n>-M<nn>-S<nn>-T<nn>)")
    if (not isinstance(main, str) or not main or main.startswith("-")
            or any(ch.isspace() or not ch.isprintable() for ch in main)):
        raise KitError(f"{main!r} is not a usable main branch name")
    path = root / ".worktrees" / task_id.lower()
    branch = f"task/{task_id}"
    if os.path.lexists(path):
        raise KitError(f"{path} already exists")
    if _resolves(root, f"refs/heads/{branch}"):
        raise KitError(f"branch {branch} already exists")
    if not _resolves(root, f"{main}^{{commit}}"):
        if main == PRIMARY_BRANCH:  # say how to fix the usual cause (a repository still on master) in one line
            raise KitError(primary_branch_problem(root, main, need_commit=True)
                           or f"{main} does not resolve to a commit")
        raise KitError(f"{main} does not resolve to a commit")
    git(root, "worktree", "add", "-b", branch, "--", str(path), main)
    return path


def _entries(listing: str) -> list[dict[str, str]]:
    """The blocks of `git worktree list --porcelain` as {key: value}; a bare key has the value ''."""
    entries = []
    for block in listing.split("\n\n"):
        entry: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            entry[key] = value
        if "worktree" in entry:
            entries.append(entry)
    return entries


def list_all(root: Path) -> list[dict]:
    """Every worktree: path, branch (None when detached), head, dirty, missing.

    `dirty` is whether `git status --porcelain` shows anything. A worktree whose directory
    is gone is reported with `dirty: None` and `missing: True`.
    """
    found = []
    for entry in _entries(git(root, "worktree", "list", "--porcelain")):
        if "bare" in entry:
            continue
        path = entry["worktree"]
        ref = entry.get("branch", "")
        missing = not Path(path).is_dir()
        found.append({"path": path,
                      "branch": ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else (ref or None),
                      "head": entry.get("HEAD", ""),
                      "dirty": None if missing else bool(git(Path(path), "status", "--porcelain")),
                      "missing": missing})
    return found


# --- command -----------------------------------------------------------------

def cmd_worktree(args: argparse.Namespace) -> int:
    action = getattr(args, "worktree_action", None)
    if action not in ("new", "list"):
        raise KitError("worktree needs an action: new or list")
    root = find_root()
    if action == "new":
        output = str(new(root, args.task_id))
    else:
        rows = []
        for item in list_all(root):
            state = "missing" if item["missing"] else ("dirty" if item["dirty"] else "clean")
            rows.append(f"{item['path']}\t{item['branch'] or '(detached)'}\t{item['head'][:8]}\t{state}")
        output = "\n".join(rows)
    if output:
        print(output)
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="worktree_action", metavar="action", parser_class=type(parser))
    maker = actions.add_parser("new", help="create .worktrees/<id> on branch task/<ID> from main")
    maker.add_argument("task_id", metavar="ID", help="task ID, P<n>-M<nn>-S<nn>-T<nn>")
    actions.add_parser("list", help="list worktrees with branch, head and dirty state")


def register(add_command) -> None:
    add_command("worktree", cmd_worktree, help="task worktrees: new, list", configure=_configure)
