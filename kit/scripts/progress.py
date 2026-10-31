#!/usr/bin/env python3
"""Progress docs behind state, and the tasks ready to start (standard library only).

The optional `progress` section of `program/checks.yaml` names the documents a controller keeps current by hand
(`docs`) and the paths whose commits change the program's state (`state`):

    progress:
      docs: [program/DASHBOARD.md, program/SUCCESSOR_PROMPT.md]
      state: [program/status, program/rulings.md, program/controller-state.json]
      quiet_when: some-shell-command      # optional; exit 0 keeps the agent hook silent

`behind` is check D7 of `pk drift`: the docs are behind when a commit on the main branch touching a `state` path
comes after the oldest of the docs' newest commits (the commit whose doc was updated longest ago), judged by
history, not by the clock: a task branch's state commit merged after the docs counts too. Without the section the
check is skipped. `ready` lists the not-started register tasks whose dependencies are all done or
merged, and those waiting on exactly one, leaving out any task that already has a worktree or a `task/<ID>` or
`wip/<ID>...` branch. Both read git and the checkout and write nothing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

from common import PRIMARY_BRANCH, KitError, git, read_yaml

KEYS = ("docs", "state", "quiet_when")
SHOWN = 6  # newer state commits named in a report
QUIET_TIMEOUT = 20  # seconds for the quiet_when command
NOT_CONFIGURED = "skipped: not configured (progress.docs and progress.state in program/checks.yaml)"


def _paths(value, key: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise KitError(f"checks.yaml: progress.{key} must be a non-empty list of paths")
    for item in value:
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts or "\\" in item:
            raise KitError(f"checks.yaml: progress.{key}: {item!r} must be a relative path inside the repository")
    return [item.rstrip("/") or item for item in value]


def parse(value) -> dict | None:
    """The `progress` section as {docs: [path], state: [path], quiet_when: str or None}, or None when absent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KitError("checks.yaml: progress must be a mapping of docs, state and quiet_when")
    unknown = sorted(str(key) for key in value if key not in KEYS)
    if unknown:
        raise KitError(f"checks.yaml: progress: unknown key {', '.join(unknown)} (expected {', '.join(KEYS)})")
    quiet = value.get("quiet_when")
    if quiet is not None and (not isinstance(quiet, str) or not quiet.strip()):
        raise KitError("checks.yaml: progress.quiet_when must be a shell command (a string)")
    return {"docs": _paths(value.get("docs"), "docs"), "state": _paths(value.get("state"), "state"),
            "quiet_when": quiet}


def config(root: Path) -> dict | None:
    """The progress section of the checkout's `program/checks.yaml`, None when the file or the section is absent."""
    path = Path(root) / "program" / "checks.yaml"
    if not path.is_file():
        return None
    data = read_yaml(path)
    return parse(data.get("progress") if isinstance(data, dict) else None)


# --- D7 -----------------------------------------------------------------------------

def behind(root: Path, cfg: dict | None, main: str = PRIMARY_BRANCH) -> dict:
    """{ok, detail, docs_commit, newer: ["<short sha> <subject>"], more: int} for the configured docs and state."""
    if cfg is None:
        return {"ok": True, "detail": NOT_CONFIGURED, "docs_commit": None, "newer": [], "more": 0}
    try:
        git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{main}^{{commit}}")
    except KitError:
        return {"ok": True, "detail": f"skipped: branch {main} has no commit", "docs_commit": None, "newer": [],
                "more": 0}
    newest = {}  # doc: the sha of its newest commit on main
    never = []
    for doc in cfg["docs"]:
        sha = git(root, "log", "-1", "--format=%H", main, "--", doc)
        if sha:
            newest[doc] = sha
        else:
            never.append(doc)
    if never:
        span, since = [main], f"{', '.join(never)} never committed on {main}"
        docs_commit = None
    else:
        # the oldest of the docs' newest commits, by history rather than by clock (commits share a second):
        # their common ancestor, which is that commit itself whenever they lie on one line
        shas = list(newest.values())
        docs_commit = shas[0]
        for sha in shas[1:]:
            docs_commit = git(root, "merge-base", docs_commit, sha)
        oldest = [doc for doc, sha in newest.items() if sha == docs_commit]
        span = [f"{docs_commit}..{main}"]
        since = (f"{', '.join(oldest)} last changed in {docs_commit[:12]}" if oldest
                 else f"the docs' newest commits have their common ancestor at {docs_commit[:12]}")
    listing = git(root, "log", "--format=%h %s", *span, "--", *cfg["state"])
    lines = [line[:140] for line in listing.splitlines() if line.strip()]
    newer, more = lines[:SHOWN], max(0, len(lines) - SHOWN)
    if not lines:
        return {"ok": True, "detail": f"progress docs current ({since}; no newer state commit)",
                "docs_commit": docs_commit, "newer": [], "more": 0}
    shown = "; ".join(newer) + (f" (and {more} more)" if more else "")
    return {"ok": False, "detail": f"progress docs behind state: {since}; {len(lines)} newer state commit(s) on "
                                   f"{main}: {shown}", "docs_commit": docs_commit, "newer": newer, "more": more}


def quiet(root: Path, cfg: dict | None) -> bool:
    """Whether the configured quiet_when command exits 0 (run by the shell in the repository root)."""
    command = (cfg or {}).get("quiet_when")
    if not command:
        return False
    try:
        return subprocess.run(command, shell=True, cwd=str(root), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=QUIET_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --- ready ---------------------------------------------------------------------------

def _tasks(root: Path) -> list[dict]:
    """Every task mapping of the register with a string id, in document order."""
    import register as register_module

    if not (Path(root) / "program" / "register.yaml").is_file():
        return []
    register = register_module.load(root)
    return [node for level, node, _ in register_module._nodes(register)
            if level == 3 and isinstance(node.get("id"), str) and node["id"]]


def _started_elsewhere(root: Path) -> tuple[set[str], set[str]]:
    """(branch names under task/ and wip/, lower-cased worktree directory names)."""
    refs = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/task/", "refs/heads/wip/")
    import worktree

    trees = {Path(entry["worktree"]).name.lower()
             for entry in worktree._entries(git(root, "worktree", "list", "--porcelain"))}
    return set(refs.split()), trees


def _taken(task_id: str, branches: set[str], trees: set[str]) -> bool:
    low = task_id.lower()
    if f"task/{task_id}" in branches or low in trees or any(name.startswith(low + "-") for name in trees):
        return True
    prefix = f"wip/{task_id}"
    return any(name == prefix or (name.startswith(prefix) and not name[len(prefix)].isalnum()) for name in branches)


def ready(root: Path) -> dict:
    """{ready: [{id, title}], near_ready: [{id, title, waiting_on, waiting_status}]}."""
    import status as status_module

    root = Path(root)
    statuses = {}
    for path in sorted((root / "program" / "status").glob("*.json")):
        try:
            statuses[path.stem] = status_module.read(root, path.stem)["status"]
        except KitError:
            statuses[path.stem] = "unreadable"
    branches, trees = _started_elsewhere(root)
    found = {"ready": [], "near_ready": []}
    for task in _tasks(root):
        tid = task["id"]
        if statuses.get(tid, "not_started") != "not_started" or _taken(tid, branches, trees):
            continue
        deps = task.get("depends_on") if isinstance(task.get("depends_on"), list) else []
        pending = [dep for dep in deps if statuses.get(str(dep), "not_started") not in status_module.TERMINAL]
        title = " ".join(str(task.get("title", "")).split())
        if not pending:
            found["ready"].append({"id": tid, "title": title})
        elif len(pending) == 1:
            found["near_ready"].append({"id": tid, "title": title, "waiting_on": str(pending[0]),
                                        "waiting_status": statuses.get(str(pending[0]), "not_started")})
    return found


def ready_lines(found: dict) -> list[str]:
    lines = [f"{item['id']} ready: {item['title']}" for item in found["ready"]]
    lines += [f"{item['id']} near-ready, after {item['waiting_on']} ({item['waiting_status']}): {item['title']}"
              for item in found["near_ready"]]
    return lines
