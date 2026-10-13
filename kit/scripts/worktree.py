#!/usr/bin/env python3
"""Task worktrees: one git worktree and branch per task (standard library only).

`new` creates `<root>/.worktrees/<id lowercased>` on branch `task/<ID>` from the main branch;
`list_all` reports every worktree git knows, with its dirty state.

`review` gives a reviewer its own detached worktree, `<root>/.worktrees/<id lowercased>-review`, at the commit
under review, and writes the diff to review beside it (`<id lowercased>-review.diff`, with the evidence and
status files and any `--exclude` paths left out), so the implementer can go on working in the task worktree
and the reviewer is handed a file, not pasted text. Both live under the git-ignored `.worktrees/`.

`finish` cleans up after a task, and never forces anything. It refuses, removing nothing, when a worktree of
the task holds an uncommitted or untracked file other than the symlinks it is told about, and when the branch
is not merged into main (unless told to keep the branch). Then it removes those symlinks by literal path, the
task and review worktrees with a plain `git worktree remove`, the merged branch with `git branch -d`, and,
when given the task's compose project name, the containers, volumes and images whose names start with exactly
that name and a separator, each by its own name and without `-f`. It prints every thing it removed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import re
import subprocess

import register as register_module
from common import KitError, PRIMARY_BRANCH, find_root, git, primary_branch_problem, run_argv

ADHOC_ID = re.compile(r"AH-\d{4}\Z")  # an ad-hoc task of the controller state
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{2,}\Z")
DIFF_EXCLUDES = ("program/evidence", "program/status")


def _is_task(task_id) -> bool:
    return isinstance(task_id, str) and bool(register_module.TASK_ID.match(task_id) or ADHOC_ID.match(task_id))


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
    if not _is_task(task_id):
        raise KitError(f"{task_id!r} is not a task id (expected P<n>-M<nn>-S<nn>-T<nn>, or AH-<nnnn> for an "
                       "ad-hoc task)")
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


def _main_checkout(root: Path, what: str) -> None:
    """KitError unless `root` is the main checkout: the top level of the repository, on the primary branch."""
    try:
        here = git(root, "symbolic-ref", "HEAD")
    except KitError:
        here = ""
    if here != f"refs/heads/{PRIMARY_BRANCH}" or Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise KitError(f"run {what} from the main checkout, on {PRIMARY_BRANCH} (not from inside a worktree)")


def review(root: Path, task_id: str, commit: str | None = None, excludes=()) -> dict:
    """Create the detached review worktree of `task_id` and its diff file; {path, diff, commit}.

    The commit under review defaults to the tip of `task/<ID>` and must be on that branch. Nothing is created
    on a refusal.
    """
    root = Path(root).resolve()
    if not _is_task(task_id):
        raise KitError(f"{task_id!r} is not a task id")
    branch = f"refs/heads/task/{task_id}"
    if not _resolves(root, branch):
        raise KitError(f"branch task/{task_id} not found")
    sha = git(root, "rev-parse", "--verify", f"{commit or branch}^{{commit}}")
    code, _ = run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", sha, branch], root)
    if code != 0:
        raise KitError(f"{sha} is not a commit of task/{task_id}")
    path = root / ".worktrees" / f"{task_id.lower()}-review"
    diff = root / ".worktrees" / f"{task_id.lower()}-review.diff"
    if os.path.lexists(path):
        raise KitError(f"{path} already exists; finish or remove the earlier review worktree first")
    for item in excludes:
        if not item or item.startswith(("-", ":", "/")) or ".." in Path(item).parts:
            raise KitError(f"not a usable path to exclude: {item!r}")
    base = git(root, "merge-base", PRIMARY_BRANCH, sha)
    spec = [f":(exclude){item}" for item in (*DIFF_EXCLUDES, *excludes)]
    try:
        text = subprocess.run(["git", "-C", str(root), "diff", f"{base}..{sha}", "--", ".", *spec],
                              capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise KitError(f"cannot write the review diff: {exc}") from exc
    git(root, "worktree", "add", "--detach", "--", str(path), sha)
    diff.write_bytes(text)
    return {"path": path, "diff": diff, "commit": sha}


def _blocking(wt: Path, symlinks: list[str]) -> list[str]:
    """The uncommitted and untracked paths of a worktree, other than the named symlinks that really are symlinks."""
    found = []
    try:  # raw output: `git` strips it, and the first status line may begin with a space
        listing = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"],
                                 capture_output=True, check=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise KitError(f"cannot read the state of {wt}: {exc}") from exc
    for line in listing.splitlines():
        code, name = line[:2], line[3:]
        if code == "??" and name in symlinks and (wt / name).is_symlink():
            continue
        found.append(name)
    return found


def _names(tool: str, *argv: str) -> list[str]:
    code, output = run_argv([tool, *argv], Path.cwd())
    if code != 0:
        raise KitError(f"{tool} {' '.join(argv)} failed: {(output.strip().splitlines() or [f'exit {code}'])[-1]}")
    return [line.strip() for line in output.splitlines() if line.strip()]


def finish(root: Path, task_id: str, symlinks=(), compose_project: str | None = None, tool: str = "docker",
           keep_branch: bool = False) -> list[str]:
    """Clean up after a task; returns one line per thing removed. Never forces; refuses before removing anything."""
    root = Path(root).resolve()
    if not _is_task(task_id):
        raise KitError(f"{task_id!r} is not a task id")
    _main_checkout(root, "pk worktree finish")
    for name in symlinks:
        if not name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise KitError(f"a symlink is named by its path inside the worktree, not {name!r}")
    if compose_project is not None and not _PROJECT.match(compose_project):
        raise KitError(f"not a usable compose project name: {compose_project!r} (the task's exact name, three or "
                       "more of a-z, 0-9, '-' and '_')")
    branch = f"task/{task_id}"
    has_branch = _resolves(root, f"refs/heads/{branch}")
    if has_branch and not keep_branch:
        code, _ = run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", f"refs/heads/{branch}",
                            PRIMARY_BRANCH], root)
        if code != 0:
            raise KitError(f"branch {branch} is not merged into {PRIMARY_BRANCH}: an unmerged branch is never "
                           "removed (pass --keep-branch to remove only the worktrees of an abandoned task)")
    registered = {str(Path(item["path"]).resolve()): item for item in list_all(root)}
    trees = [path for path in (root / ".worktrees" / task_id.lower(), root / ".worktrees" / f"{task_id.lower()}-review")
             if str(path.resolve()) in registered and not registered[str(path.resolve())]["missing"]]
    for path in trees:
        blocking = _blocking(path, list(symlinks))
        if blocking:
            raise KitError(f"{path} holds uncommitted or untracked files: {', '.join(blocking)}; commit them or "
                           "delete them by exact path first (a worktree that holds work is never force-removed)")
    removed = []
    for path in trees:
        for name in symlinks:
            if (path / name).is_symlink():
                os.unlink(path / name)
                removed.append(f"removed: symlink {path / name}")
        git(root, "worktree", "remove", "--", str(path))
        removed.append(f"removed: worktree {path}")
    diff = root / ".worktrees" / f"{task_id.lower()}-review.diff"
    if diff.is_file():
        os.unlink(diff)
        removed.append(f"removed: review diff {diff}")
    if has_branch and not keep_branch:
        git(root, "branch", "-d", "--", branch)
        removed.append(f"removed: branch {branch}")
    elif has_branch:
        removed.append(f"kept: branch {branch}")
    if compose_project is not None:
        owned = (compose_project + "-", compose_project + "_")
        for kind, listing, remover in (
                ("container", ("ps", "-a", "--format", "{{.Names}}"), ("rm",)),
                ("volume", ("volume", "ls", "--format", "{{.Name}}"), ("volume", "rm")),
                ("image", ("image", "ls", "--format", "{{.Repository}}:{{.Tag}}"), ("image", "rm"))):
            for name in _names(tool, *listing):
                if name.startswith(owned):
                    _names(tool, *remover, name)  # by its exact name, never -f: one in use stops here
                    removed.append(f"removed: {kind} {name}")
    return removed


# --- command -----------------------------------------------------------------

def cmd_worktree(args: argparse.Namespace) -> int:
    action = getattr(args, "worktree_action", None)
    if action not in ("new", "list", "review", "finish"):
        raise KitError("worktree needs an action: new, list, review or finish")
    root = find_root()
    if action == "new":
        output = str(new(root, args.task_id))
    elif action == "review":
        made = review(root, args.task_id, commit=args.commit, excludes=args.exclude)
        output = f"{made['path']}\ndiff: {made['diff']}\ncommit: {made['commit']}"
    elif action == "finish":
        output = "\n".join(finish(root, args.task_id, symlinks=args.symlink, compose_project=args.compose_project,
                                  tool=args.container_tool, keep_branch=args.keep_branch)) or "nothing to remove"
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
    reviewer = actions.add_parser("review", help="a detached worktree at the reviewed commit, and the diff file")
    reviewer.add_argument("task_id", metavar="ID")
    reviewer.add_argument("--commit", help="the commit under review (default: the tip of task/<ID>)")
    reviewer.add_argument("--exclude", action="append", default=[], metavar="PATH",
                          help="leave this path out of the diff file (generated files, lock files)")
    finisher = actions.add_parser("finish", help="remove a merged task's worktrees and branch; never forces")
    finisher.add_argument("task_id", metavar="ID")
    finisher.add_argument("--symlink", action="append", default=[], metavar="PATH",
                          help="an untracked symlink in the worktree that may be removed")
    finisher.add_argument("--compose-project", dest="compose_project", metavar="NAME",
                          help="also remove the containers, volumes and images named NAME-... or NAME_...")
    finisher.add_argument("--container-tool", dest="container_tool", default="docker", metavar="TOOL")
    finisher.add_argument("--keep-branch", dest="keep_branch", action="store_true",
                          help="an abandoned task: remove its worktrees, keep its unmerged branch")


def register(add_command) -> None:
    add_command("worktree", cmd_worktree, help="task worktrees: new, list, review, finish", configure=_configure)
