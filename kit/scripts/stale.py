#!/usr/bin/env python3
"""Which evidence records are stale against main (standard library only).

Only the latest results record of each task counts: the one `program/evidence/index.json` points at, read
from the evidence store. A superseded record is history, never stale. The latest record is stale when any
hashed input no longer matches the committed blob on `main`, or when that path no longer exists there; a gate
record is also stale when a child record it binds by digest is no longer its task's latest. Hashing goes through
git (`ls-tree` once for path -> blob id, then `cat-file blob` once per distinct blob), so the
working copy never matters and a large evidence set does not spawn a process per input.
`pk stale` always exits 0 and prints the report; only a repository with no `main` branch is an error.
`changed_inputs` is the same comparison against any one commit; `pk gate binding` uses it to
recompute a record's hashes at the commit the record names.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import store
from common import KitError, find_root, git, sha256_bytes


def _blob_bytes(root: Path, blob: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(root), "cat-file", "blob", blob], capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        raise KitError(f"git cat-file blob {blob} failed")
    return proc.stdout


def _tree(root: Path, sha: str) -> dict[str, str]:
    """path -> blob id for every file on the commit `sha` (submodule entries are not files)."""
    listing = git(root, "ls-tree", "-r", "-z", "--full-tree", sha)
    files = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        info, _, path = entry.partition("\t")
        parts = info.split()
        if len(parts) == 3 and parts[1] == "blob":
            files[path] = parts[2]
    return files


def _one_line(text) -> str:
    return " ".join(str(text).split()) or "unreadable"


def _inputs(data: dict):
    """(inputs mapping, None) for a usable record, else (None, one-line reason)."""
    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        return None, "no inputs mapping"
    if not inputs:
        return None, "inputs mapping is empty"
    return inputs, None


def _superseded_children(data: dict, index: dict) -> list[str]:
    """`<task> <kind>` for each child record a gate record binds that is no longer its task's latest."""
    children = data.get("children")
    if not isinstance(children, list):
        return []
    found = []
    for child in children:
        if not isinstance(child, dict):
            continue
        entry = store.latest(index, str(child.get("task_id")), str(child.get("kind")))
        if entry is None or entry["digest"] != child.get("digest"):
            found.append(f"{child.get('task_id')} {child.get('kind')}")
    return sorted(found)


def _hasher(root: Path, sha: str):
    """A function path -> sha256 of the blob at commit `sha`, or None when it is not a file there."""
    tree = _tree(root, sha)
    hashes: dict[str, str] = {}  # blob id -> sha256, one read per distinct blob

    def current(path: str) -> str | None:
        blob = tree.get(path)
        if blob is None:
            return None
        if blob not in hashes:
            hashes[blob] = sha256_bytes(_blob_bytes(root, blob))
        return hashes[blob]

    return current


def changed_inputs(root, sha: str, inputs: dict) -> dict[str, str | None]:
    """The recorded inputs that do not match commit `sha`: path -> the blob's real sha256, or None when the
    path is not a file there. Sorted by path; empty when every recorded hash is the committed one."""
    current = _hasher(Path(root), sha)
    return {path: current(path) for path, digest in sorted(inputs.items()) if current(path) != digest}


def stale_records(root, main: str = "main") -> dict:
    """Compare the latest results record of every task in the index with `main`; return
    {main, stale, unreadable, checked}.

    `stale` rows are {task_id, path, changed_inputs, superseded_children}; `unreadable` rows are
    {task_id, path, changed_inputs: [], error} (a record missing from the store, altered, or unusable). `path`
    is the record's path in the store. Both lists are sorted by task, and `checked` counts every record examined.
    """
    root = Path(root)
    if not isinstance(main, str) or not main or main.startswith("-"):
        raise KitError(f"not a usable branch name: {main}")
    sha = git(root, "rev-parse", "--verify", main + "^{commit}")
    current = _hasher(root, sha)  # shared by every record of this run
    index = store.read_index(root)
    entries = [(task, entry) for task in sorted(index["tasks"])
               if (entry := store.latest(index, task, "results")) is not None]
    source = store.store_dir(root) if entries else None

    stale_rows, unreadable = [], []
    for task, entry in entries:
        rel = entry["path"]
        try:
            data = store.load(source, rel, entry["digest"])
        except KitError as exc:
            unreadable.append({"task_id": task, "path": rel, "changed_inputs": [], "error": _one_line(exc)})
            continue
        inputs, error = _inputs(data)
        if inputs is None:
            unreadable.append({"task_id": task, "path": rel, "changed_inputs": [], "error": error})
            continue
        changed = sorted(path for path, digest in inputs.items() if current(path) != digest)
        children = _superseded_children(data, index)
        if changed or children:
            stale_rows.append({"task_id": task, "path": rel, "changed_inputs": changed,
                               "superseded_children": children})
    return {"main": sha, "stale": stale_rows, "unreadable": unreadable, "checked": len(entries)}


# --- command -----------------------------------------------------------------------

def cmd_stale(args: argparse.Namespace) -> int:
    report = stale_records(find_root(), "main")  # the primary branch is fixed; a flag here could hide staleness
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def register(add_command) -> None:
    add_command("stale", cmd_stale, help="list evidence records whose inputs changed on main")
