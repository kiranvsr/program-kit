#!/usr/bin/env python3
"""Evidence drift: how the latest records relate to the current tree (standard library only).

A results record is a fingerprint: at commit `head` these inputs, with these digests, produced this outcome. That
claim stays true forever, so a later change never makes a record stale. What a later change does is make the
record drift: its recorded input digests no longer equal the committed blobs of the tree being looked at. Only the
latest record of each task counts (the one its pointer `program/evidence/<ID>/latest.json` names); older records are
history. The tree compared with is always the checkout's HEAD (the gate's drift step and `pk stale` alike).
A record is `fresh` when every input matches, else `drifted` (since its `head`), with the changed inputs listed.
Drift is information for the reader, never a failure: `pk stale` prints the report and exits 0, unless
`--strict` is given, when a drifted or unreadable record exits 1. Hashing goes through
git (`ls-tree` once for path -> blob id, then `cat-file blob` once per distinct blob), so the
working copy never matters and a large evidence set does not spawn a process per input.
`changed_inputs` is the same comparison against any one commit; `pk gate binding` uses it to
recompute a record's hashes at the commit the record names.

An adopted programme (0.7.0, `program/adoption.yaml`) also gets `adopted` in the report: each status record that
carries `references` (an adopted task, or one reopened from adopted) is `fresh` when every reference's recorded
sha256 equals the regular file committed at HEAD, else `drifted` since `adopted_commit`, with the changed paths. It is
information under the same rule: exit 0, and 1 with `--strict` when one drifted or cannot be read. `pk status
validate` still checks the references at `adopted_commit`, so the drift never makes the record wrong.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import store
from common import KitError, find_root, git, read_json, sha256_bytes

REGULAR_MODES = ("100644", "100755")  # what a reference may be (adoption.REGULAR_MODES): never a symbolic link


def _blob_bytes(root: Path, blob: str) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(root), "cat-file", "blob", blob], capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        raise KitError(f"git cat-file blob {blob} failed")
    return proc.stdout


def _tree(root: Path, sha: str, regular_only: bool = False) -> dict[str, str]:
    """path -> blob id for every file on the commit `sha` (submodule entries are not files); with `regular_only`,
    regular files alone (a symbolic link is left out)."""
    listing = git(root, "ls-tree", "-r", "-z", "--full-tree", sha)
    files = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        info, _, path = entry.partition("\t")
        parts = info.split()
        if len(parts) == 3 and parts[1] == "blob" and (not regular_only or parts[0] in REGULAR_MODES):
            files[path] = parts[2]
    return files


def _one_line(text) -> str:
    return " ".join(str(text).split()) or "unreadable"


def _inputs(data: dict):
    """(inputs mapping, None) for a usable record, else (None, one-line reason)."""
    inputs = store.input_digests(data.get("inputs"))  # either shape: the 0.4.0 mapping or the 0.4.1 list
    if inputs is None:
        return None, "no inputs mapping"
    if not inputs:
        return None, "inputs mapping is empty"
    return inputs, None


def _hasher(root: Path, sha: str, regular_only: bool = False):
    """A function path -> sha256 of the blob at commit `sha`, or None when it is not a file there (with
    `regular_only`, when it is not a regular file there)."""
    tree = _tree(root, sha, regular_only)
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


def reference_drift(root, sha: str) -> dict | None:
    """The adopted references against commit `sha` (0.7.0): None without `program/adoption.yaml`, else
    {since, checked, drifted, tasks, unreadable}.

    `tasks` rows are {task_id, status, path, state: fresh or drifted, changed_references} for each status record that
    carries `references`, sorted by task; `changed_references` lists the paths whose recorded sha256 is not the regular
    file at `sha` (changed, gone, or now a symbolic link). `unreadable` rows are {task_id, path, error} for a status
    record that does not read or whose references are not a list of {path, sha256}. `since` is `adopted_commit`.
    """
    import adoption  # late: only an adopted programme has references

    root = Path(root)
    if not adoption.adopted(root):
        return None
    try:
        since = adoption.load(root)["adopted_commit"]
    except KitError:
        since = ""
    current = _hasher(root, sha, regular_only=True)
    folder = root / "program" / "status"
    rows, unreadable, checked = [], [], 0
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        rel = f"program/status/{path.name}"
        try:
            record = read_json(path)
        except KitError as exc:
            unreadable.append({"task_id": path.stem, "path": rel, "error": _one_line(exc)})
            continue
        if not isinstance(record, dict) or "references" not in record:
            continue
        checked += 1
        references = record["references"]
        if not (isinstance(references, list) and references and all(
                isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str)
                for item in references)):
            unreadable.append({"task_id": path.stem, "path": rel,
                               "error": "references must be a non-empty list of {path, sha256}"})
            continue
        changed = sorted({item["path"] for item in references if current(item["path"]) != item["sha256"]})
        rows.append({"task_id": path.stem, "status": str(record.get("status", "")), "path": rel,
                     "state": "drifted" if changed else "fresh", "changed_references": changed})
    return {"since": since, "checked": checked, "drifted": sum(row["state"] == "drifted" for row in rows),
            "tasks": rows, "unreadable": unreadable}


def drift_report(root, ref: str = "HEAD", references: bool = True) -> dict:
    """Compare the latest results record of every task's pointer with the tree of `ref`; return
    {ref, tasks, drifted, unreadable, checked}, and `adopted` (reference_drift) for an adopted programme when
    `references` is true (the gate's drift step, which is about the task's own record, leaves it out).

    `tasks` rows are {task_id, path, head, state: fresh or drifted, changed_inputs, fingerprint: {head12, covered_rows,
    total_rows} or None when there is no current fingerprint}; `drifted` counts the drifted
    ones; `unreadable` rows are {task_id, path, changed_inputs: [], error} (a record missing, altered or without
    inputs). Both lists are sorted by task, and `checked` counts every record examined.
    """
    root = Path(root)
    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise KitError(f"not a usable commit name: {ref}")
    sha = git(root, "rev-parse", "--verify", ref + "^{commit}")
    current = _hasher(root, sha)  # shared by every record of this run
    import fingerprint  # late: fingerprint imports gate, which imports this module
    context = fingerprint.Coverage(root, sha)
    index = store.read_index(root)
    entries = [(task, entry) for task in sorted(index["tasks"])
               if (entry := store.latest(index, task, "results")) is not None]
    rows, unreadable = [], []
    for task, entry in entries:
        rel = entry["path"]
        try:
            data = store.load(root, rel, entry["digest"])
        except KitError as exc:
            unreadable.append({"task_id": task, "path": rel, "changed_inputs": [], "error": _one_line(exc)})
            continue
        inputs, error = _inputs(data)
        if inputs is None:
            unreadable.append({"task_id": task, "path": rel, "changed_inputs": [], "error": error})
            continue
        changed = sorted(path for path, digest in inputs.items() if current(path) != digest)
        covered = None
        if context.record is not None:
            done, total = fingerprint.coverage_counts(context.rows(data.get("commands")))
            covered = {"head12": context.head12, "covered_rows": done, "total_rows": total}
        rows.append({"task_id": task, "path": rel, "head": str(data.get("head", "")),
                     "state": "drifted" if changed else "fresh", "changed_inputs": changed,
                     "fingerprint": covered})
    report = {"ref": sha, "tasks": rows, "drifted": sum(row["state"] == "drifted" for row in rows),
              "unreadable": unreadable, "checked": len(entries)}
    adopted = reference_drift(root, sha) if references else None
    if adopted is not None:
        report["adopted"] = adopted
    return report


# --- command -----------------------------------------------------------------------

def cmd_stale(args: argparse.Namespace) -> int:
    report = drift_report(find_root(), "HEAD")
    print(json.dumps(report, indent=2, sort_keys=True))
    adopted = report.get("adopted") or {}
    return 1 if args.strict and (report["drifted"] or report["unreadable"] or adopted.get("drifted")
                                 or adopted.get("unreadable")) else 0


def register(add_command) -> None:
    add_command("stale", cmd_stale, help="drift report: is each task's latest record fresh against the current tree",
                configure=lambda p: p.add_argument(
                    "--strict", action="store_true",
                    help="exit 1 when a record (or an adopted task's reference) drifted or cannot be read"))
