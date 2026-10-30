#!/usr/bin/env python3
"""Reuse of a task's evidence when its code is unchanged (program-kit 0.5.2; standard library only).

A program that sets `evidence: {reuse_unchanged_code: true}` in `program/checks.yaml` lets the hand-back gate skip
its test step and the task's producer when nothing they could depend on changed since the task's latest results
record: the gate then writes a new record that copies that record's commands and planted-bug rows, names it in
`reused_from`, and binds the new head, main, merge base and freshly hashed inputs. Build, compile check, scan, extra
gate and the PLAN commands still run. `pk gate handback --no-reuse` always runs everything.

The code compared between the old record's head and HEAD, as git stores it (mode and blob id of every file):
- every tracked path outside `program/`, except the paths `evidence.reuse_ignore` names; the kit (`kit/`) and every
  lockfile are always compared;
- `program/checks.yaml`, the task's own `program/tasks/<ID>/` (SPEC, PLAN, CONTRACT, plant tables) and its producer
  folder `program/evidence/<ID>/` without the records and the pointer;
- the paths under `program/` that `evidence.reuse_compare` names (the producer's shared tools).

A record is reused only when it passes the hand-back gate's own checks (every command as expected, every planted bug
killed), is not a gate record, and its head is an ancestor of HEAD; `kit/VERSION` must also be unchanged.
"""
from __future__ import annotations

from pathlib import PurePosixPath

import evidence as evidence_module
import store
from common import KitError, git, run_argv

LOCKFILES = frozenset({
    "pnpm-lock.yaml", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "bun.lockb", "bun.lock",
    "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
    "mix.lock", "pubspec.lock", "Podfile.lock", "packages.lock.json", "gradle.lockfile", "flake.lock",
})
ALWAYS = ("program/checks.yaml",)
SHOWN = 3  # changed paths named in a refusal


def _covers(entry: str, path: str) -> bool:
    entry = entry.rstrip("/")
    return path == entry or path.startswith(entry + "/")


def compared(path: str, task_id: str, config: dict) -> bool:
    """Whether `path` is part of the code a reused record must share with HEAD."""
    if PurePosixPath(path).name in LOCKFILES or path in ALWAYS or path.startswith("kit/"):
        return True
    if path.startswith(f"program/tasks/{task_id}/"):
        return True
    folder = f"program/evidence/{task_id}/"
    if path.startswith(folder):
        rest = path[len(folder):]
        return not (rest == store.POINTER or rest.startswith(tuple(f"{kind}/" for kind in store.RECORD_KINDS)))
    if path.startswith("program/"):
        return any(_covers(entry, path) for entry in config.get("reuse_compare", []))
    return not any(_covers(entry, path) for entry in config.get("reuse_ignore", []))


def _tree(root, commit: str) -> dict[str, str]:
    """{path: "mode type id"} of every tracked file at `commit`."""
    listing = evidence_module._git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = {}
    for item in listing.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        entries[path.decode("utf-8", "surrogateescape")] = meta.decode("ascii", "replace")
    return entries


def code_changes(root, old: str, new: str, task_id: str, config: dict) -> list[str]:
    """The compared paths whose mode or content differs between commits `old` and `new` (added and removed too)."""
    before = {path: meta for path, meta in _tree(root, old).items() if compared(path, task_id, config)}
    after = {path: meta for path, meta in _tree(root, new).items() if compared(path, task_id, config)}
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def _version(root, commit: str) -> str | None:
    try:
        return evidence_module._git_bytes(root, "show", f"{commit}:kit/VERSION").decode("utf-8", "replace").strip()
    except KitError:
        return None


def find(root, task_id: str, config: dict) -> tuple[dict | None, str]:
    """(the reusable record as {entry, data}, "") or (None, why it cannot be reused), for the clean worktree `root`."""
    try:
        entry = store.read_pointer(root, task_id).get("results")
        if entry is None:
            return None, "the task has no results record yet"
        data = store.load(root, entry["path"], entry["digest"])
    except KitError as exc:
        return None, " ".join(str(exc).split())
    problems = evidence_module.gate_problems(data, task_id, None, root)
    if problems:
        return None, f"the latest record {entry['path']} does not pass the gate: {problems[0]}"
    if data.get("children") or evidence_module.is_gate(task_id):
        return None, "a gate record is never reused"
    head, old = git(root, "rev-parse", "HEAD"), data["head"]
    code, _ = run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", old, head], root)
    if code != 0:
        return None, f"the latest record's head {old} is not an ancestor of HEAD {head}"
    before, now = _version(root, old), _version(root, head)
    if before != now:
        return None, f"the kit version changed since {old[:12]} ({before or 'none'} to {now or 'none'})"
    changed = code_changes(root, old, head, task_id, config)
    if changed:
        more = f" and {len(changed) - SHOWN} more" if len(changed) > SHOWN else ""
        return None, f"the code changed since {old[:12]}: {', '.join(changed[:SHOWN])}{more}"
    return {"entry": entry, "data": data}, ""


def write(root, task_id: str, found: dict) -> dict:
    """Append the reused record for HEAD and point the task's pointer at it; the append result of `Evidence.write`.

    The old record's input paths are hashed again at HEAD (those still tracked); its commands, planted-bug rows,
    blocked_by and deviations are copied unchanged; `reused_from` names the old record by path, digest and head.
    When the old record deferred planted-bug rows (0.5.3), `plants_reused_from` names the plants records bound to it
    or to a record it reused in turn: judged on the same code, they count for the new record too, and its deferred
    rows still pending are judged with `pk gate plants` at the new head.
    """
    entry, old = found["entry"], found["data"]
    run = evidence_module.Evidence(root, task_id)
    run.require_clean_tree()
    tracked = set(git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD").split("\0"))
    run.hash_inputs([path for path in sorted(store.input_digests(old["inputs"]) or {}) if path in tracked])
    run.commands = [dict(command) for command in old["commands"]]
    run.planted_bug_rows = [dict(row) for row in old["planted_bugs"]]
    run.blocked_by, run.deviations = list(old["blocked_by"]), list(old["deviations"])
    run.reused_from = {"path": entry["path"], "digest": entry["digest"], "head": old["head"]}
    if any(evidence_module.is_deferred(row) for row in old["planted_bugs"]):
        chain = evidence_module.plants_chain(root, entry, old)
        run.plants_reused_from = [{"path": item["path"], "digest": item["digest"]}
                                  for item in store.read_plants(root, task_id) if item["part_of"] in chain]
    return run.write()
