#!/usr/bin/env python3
"""Task worktrees: one git worktree and branch per task (standard library only).

`new` creates `<root>/.worktrees/<id lowercased>` on branch `task/<ID>` from the main branch;
`list_all` reports every worktree git knows, with its dirty state.

`review` gives a reviewer its own detached worktree, `<root>/.worktrees/<id lowercased>-review`, at the commit
under review, and writes two files beside it: the diff to review (`<id lowercased>-review.diff`, with the
evidence and status files and any `--exclude` paths left out) and what the diff leaves out
(`<id lowercased>-review.stat`: the commit, the paths left out and `git diff --stat` of the whole range, so a
change hidden by an exclusion still shows). The reviewer plants and runs in its own worktree and is handed a
file, not pasted text. Run again, for a re-review after a fix round, it moves the clean review worktree to the
new commit and rewrites both files. The implementer does not commit on the branch while it is under review:
`pk merge` refuses a branch that changed anything but its own evidence and status files after the reviewed
commit. All of it lives under the git-ignored `.worktrees/`.

`finish` cleans up after a task, and never forces anything. Everything is checked before the first removal:
it refuses when a worktree of the task holds an uncommitted or untracked file other than the symlinks it is
told about, when one holds git-ignored files and it was not told to drop them, when one stands on a commit
that no branch holds (work that would be lost with the worktree), when the branch is not merged into main
(unless told to keep the branch), and when the container tool does not answer. Then it removes those symlinks
by literal path, the task and review worktrees with a plain `git worktree remove`, the merged branch with
`git branch -d`, and, when given the task's compose project name, the containers and volumes that carry the
label `com.docker.compose.project=<that name>`, each by its own name and without `-f`. The tool matches a
label value exactly, so a project whose name merely begins with the same letters is never touched; nothing is
ever selected by a name prefix. Images are not removed at all: they are rebuilt from the repository, and a
name is the only way to select them. It prints every thing it removed. A removal the container tool refuses
(a container still running) stops the command there, after the worktrees and the branch have gone; run the
tool's own command for what is left.
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
    """Create the detached review worktree of `task_id`, or move it for a re-review, and write its diff and stat
    files; {path, diff, stat, commit, left_out, moved}.

    The commit under review defaults to the tip of `task/<ID>` and must be on that branch. A review worktree
    that exists is moved to the commit (`git checkout --detach`) only when it is clean and stands on a commit
    of the task branch or of main: one that holds a file or a commit of its own is refused, never overwritten.
    Nothing is created or moved on a refusal.
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
    stat = root / ".worktrees" / f"{task_id.lower()}-review.stat"
    moved = os.path.lexists(path)
    if moved:
        if str(path.resolve()) not in {str(Path(item["path"]).resolve()) for item in list_all(root)}:
            raise KitError(f"{path} already exists and is not a worktree of this repository; remove it by exact "
                           "path first")
        held = _blocking(path, [])
        if held:
            raise KitError(f"{path} holds uncommitted or untracked files: {', '.join(held)}; restore or delete "
                           "them by exact path, then run pk worktree review again to move it to the new commit")
        problem = _unheld(root, path)
        if problem:
            raise KitError(f"{problem}; give that commit a branch or drop it, then run pk worktree review again")
    for item in excludes:
        if not item or item.startswith(("-", ":", "/")) or ".." in Path(item).parts:
            raise KitError(f"not a usable path to exclude: {item!r}")
    base = git(root, "merge-base", PRIMARY_BRANCH, sha)
    left_out = list(dict.fromkeys((*DIFF_EXCLUDES, *excludes)))
    spec = [f":(exclude){item}" for item in left_out]
    try:
        text = subprocess.run(["git", "-C", str(root), "diff", f"{base}..{sha}", "--", ".", *spec],
                              capture_output=True, check=True).stdout
        whole = subprocess.run(["git", "-C", str(root), "diff", "--stat=200", f"{base}..{sha}"],
                               capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise KitError(f"cannot write the review diff: {exc}") from exc
    if moved:
        git(path, "checkout", "--quiet", "--detach", sha)
    else:
        git(root, "worktree", "add", "--detach", "--", str(path), sha)
    diff.write_bytes(text)
    stat.write_bytes(f"commit under review: {sha}\nbase on {PRIMARY_BRANCH}: {base}\n"
                     f"left out of {diff.name}: {', '.join(left_out)}\n\n"
                     f"git diff --stat {base}..{sha} (every path, the ones left out included):\n".encode() + whole)
    return {"path": path, "diff": diff, "stat": stat, "commit": sha, "left_out": left_out, "moved": moved}


def _unheld(root: Path, wt: Path) -> str | None:
    """Why removing or moving the worktree `wt` would lose a commit, or None: its HEAD is on no local branch."""
    head = git(wt, "rev-parse", "HEAD")
    if git(root, "for-each-ref", "--count=1", "--contains", head, "refs/heads/"):
        return None
    return (f"{wt} stands on {head[:12]}, a commit no branch holds: removing or moving the worktree would lose "
            f"it (keep it with: git branch <name> {head[:12]})")


def _ignored(wt: Path, symlinks: list[str]) -> list[str]:
    """The git-ignored files and folders of a worktree (a folder as one entry), other than the named symlinks."""
    try:
        listing = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--ignored"],
                                 capture_output=True, check=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise KitError(f"cannot read the state of {wt}: {exc}") from exc
    names = [line[3:] for line in listing.splitlines() if line[:2] == "!!"]
    return [name for name in names if not (name.rstrip("/") in symlinks and (wt / name.rstrip("/")).is_symlink())]


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


COMPOSE_LABEL = "com.docker.compose.project"  # what compose writes on every container and volume it creates


def finish(root: Path, task_id: str, symlinks=(), compose_project: str | None = None, tool: str = "docker",
           keep_branch: bool = False, drop_ignored: bool = False) -> list[str]:
    """Clean up after a task; returns one line per thing removed. Never forces; every check and every listing
    comes before the first removal."""
    root = Path(root).resolve()
    if not _is_task(task_id):
        raise KitError(f"{task_id!r} is not a task id")
    _main_checkout(root, "pk worktree finish")
    for name in symlinks:
        if not name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise KitError(f"a symlink is named by its path inside the worktree, not {name!r}")
    if compose_project is not None and not _PROJECT.match(compose_project):
        raise KitError(f"not a usable compose project name: {compose_project!r} (the project's exact name, three "
                       "or more of a-z, 0-9, '-' and '_')")
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
    dropped = []
    for path in trees:
        blocking = _blocking(path, list(symlinks))
        if blocking:
            raise KitError(f"{path} holds uncommitted or untracked files: {', '.join(blocking)}; commit them or "
                           "delete them by exact path first (a worktree that holds work is never force-removed)")
        problem = _unheld(root, path)
        if problem:
            raise KitError(problem)
        ignored = _ignored(path, list(symlinks))
        if ignored and not drop_ignored:
            raise KitError(f"{path} holds git-ignored files that would go with it: {', '.join(ignored)}; save "
                           "what you need (a local .env, a database file), then pass --drop-ignored")
        dropped += [f"dropped: ignored {path / name}" for name in ignored]
    owned = []  # listed before anything is removed: a container tool that does not answer stops the command here
    if compose_project is not None:
        chosen = ("--filter", f"label={COMPOSE_LABEL}={compose_project}")
        for kind, listing, remover in (("container", ("ps", "-a", *chosen, "--format", "{{.Names}}"), ("rm",)),
                                       ("volume", ("volume", "ls", *chosen, "--format", "{{.Name}}"),
                                        ("volume", "rm"))):
            owned += [(kind, remover, name) for name in _names(tool, *listing)]
    removed = []
    for path in trees:
        for name in symlinks:
            if (path / name).is_symlink():
                os.unlink(path / name)
                removed.append(f"removed: symlink {path / name}")
        git(root, "worktree", "remove", "--", str(path))
        removed.append(f"removed: worktree {path}")
    removed += dropped
    for kind in ("diff", "stat"):
        extra = root / ".worktrees" / f"{task_id.lower()}-review.{kind}"
        if extra.is_file():
            os.unlink(extra)
            removed.append(f"removed: review {kind} {extra}")
    if has_branch and not keep_branch:
        git(root, "branch", "-d", "--", branch)
        removed.append(f"removed: branch {branch}")
    elif has_branch:
        removed.append(f"kept: branch {branch}")
    for number, (kind, remover, name) in enumerate(owned):
        try:
            _names(tool, *remover, name)  # by its exact name, never -f: one in use stops here
        except KitError as exc:
            left = ", ".join(f"{k} {n}" for k, _, n in owned[number:])
            raise KitError("; ".join([*removed, f"stopped: {exc}", f"not removed: {left}"])) from exc
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
        output = (f"{made['path']}{' (moved to the new commit)' if made['moved'] else ''}\ndiff: {made['diff']}\n"
                  f"commit: {made['commit']}\nleft out of the diff: {', '.join(made['left_out'])}\n"
                  f"stat of every path: {made['stat']}")
    elif action == "finish":
        output = "\n".join(finish(root, args.task_id, symlinks=args.symlink, compose_project=args.compose_project,
                                  tool=args.container_tool, keep_branch=args.keep_branch,
                                  drop_ignored=args.drop_ignored)) or "nothing to remove"
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
    reviewer = actions.add_parser("review", help="a detached worktree at the reviewed commit, and the diff file; "
                                                 "run again, it moves to the new commit for a re-review")
    reviewer.add_argument("task_id", metavar="ID")
    reviewer.add_argument("--commit", help="the commit under review (default: the tip of task/<ID>)")
    reviewer.add_argument("--exclude", action="append", default=[], metavar="PATH",
                          help="leave this path out of the diff file (generated files, lock files)")
    finisher = actions.add_parser("finish", help="remove a merged task's worktrees and branch; never forces")
    finisher.add_argument("task_id", metavar="ID")
    finisher.add_argument("--symlink", action="append", default=[], metavar="PATH",
                          help="an untracked symlink in the worktree that may be removed")
    finisher.add_argument("--compose-project", dest="compose_project", metavar="NAME",
                          help="also remove the containers and volumes labelled com.docker.compose.project=NAME "
                               "(an exact match, never a name prefix; images are left alone)")
    finisher.add_argument("--container-tool", dest="container_tool", default="docker", metavar="TOOL")
    finisher.add_argument("--drop-ignored", dest="drop_ignored", action="store_true",
                          help="the git-ignored files in the worktrees (build output, a local .env) may go with them")
    finisher.add_argument("--keep-branch", dest="keep_branch", action="store_true",
                          help="an abandoned task: remove its worktrees, keep its unmerged branch")


def register(add_command) -> None:
    add_command("worktree", cmd_worktree, help="task worktrees: new, list, review, finish", configure=_configure)
