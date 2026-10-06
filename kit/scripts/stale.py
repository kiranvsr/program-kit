#!/usr/bin/env python3
"""Which evidence records are stale against main (standard library only).

A record (`program/evidence/*/results.json`) is stale when any hashed input no longer matches
the committed blob on `main`, or when that path no longer exists there. Hashing goes through
git (`ls-tree` once for path -> blob id, then `cat-file blob` once per distinct blob), so the
working copy never matters and a large evidence set does not spawn a process per input.
`pk stale` always exits 0 and prints the report; only an unresolvable `--main` is an error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from common import KitError, find_root, git, read_json, sha256_bytes


def _blob_bytes(root: Path, blob: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(root), "cat-file", "blob", blob], capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        raise KitError(f"git cat-file blob {blob} failed")
    return proc.stdout


def _main_tree(root: Path, sha: str) -> dict[str, str]:
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


def _inputs(path: Path):
    """(inputs mapping, None) for a usable record, else (None, one-line reason)."""
    try:
        data = read_json(path)
    except KitError as exc:
        return None, _one_line(exc)
    if not isinstance(data, dict):
        return None, "results.json is not a JSON object"
    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        return None, "no inputs mapping"
    if not inputs:
        return None, "inputs mapping is empty"
    return inputs, None


def stale_records(root, main: str = "main") -> dict:
    """Compare every record's inputs with `main`; return {main, stale, unreadable, checked}.

    `stale` rows are {path, changed_inputs}; `unreadable` rows are {path, changed_inputs: [], error}.
    Both are sorted by root-relative posix path, and `checked` counts every record examined.
    """
    root = Path(root)
    if not isinstance(main, str) or not main or main.startswith("-"):
        raise KitError(f"not a usable branch name: {main}")
    sha = git(root, "rev-parse", "--verify", main + "^{commit}")
    tree = _main_tree(root, sha)
    hashes: dict[str, str] = {}  # blob id -> sha256, one read per distinct blob per run

    def current(path: str) -> str | None:
        blob = tree.get(path)
        if blob is None:
            return None
        if blob not in hashes:
            hashes[blob] = sha256_bytes(_blob_bytes(root, blob))
        return hashes[blob]

    stale_rows, unreadable = [], []
    records = sorted((root / "program" / "evidence").glob("*/results.json"))
    for record in records:
        rel = record.relative_to(root).as_posix()
        inputs, error = _inputs(record)
        if inputs is None:
            unreadable.append({"path": rel, "changed_inputs": [], "error": error})
            continue
        changed = sorted(path for path, digest in inputs.items() if current(path) != digest)
        if changed:
            stale_rows.append({"path": rel, "changed_inputs": changed})
    # `records` is already sorted by path, so both lists are too.
    return {"main": sha, "stale": stale_rows, "unreadable": unreadable, "checked": len(records)}


# --- command -----------------------------------------------------------------------

def cmd_stale(args: argparse.Namespace) -> int:
    report = stale_records(find_root(), args.main)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--main", default="main", help="branch to compare against (default: main)")


def register(add_command) -> None:
    add_command("stale", cmd_stale, help="list evidence records whose inputs changed on main", configure=_configure)
