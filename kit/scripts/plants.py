#!/usr/bin/env python3
"""`pk gate plants`: judge the deferred planted-bug rows of a task's latest results record, as kit records.

A producer may mark planted-bug rows `defer: true`. The hand-back gate then records those rows with their full
definition and a definition digest but no verdict, and judges only the others (`Evidence.planted_bugs`). This
command judges deferred rows afterwards, in chunks, at any time after the gate (while QA reviews, too):

    pk gate plants <ID> --branch task/<ID> (--rows ID,ID,... | --all-pending) [--workers N] [--rejudge]

- The rows are judged at the `head` of the task's latest results record, never at the branch tip, each in a clean
  detached mirror of that commit, with the 0.5.2 pool: `--workers` (else PK_PLANT_WORKERS, else
  `evidence.plant_workers` of main's checks.yaml, else 1) rows at once, `serial: true` rows alone after the pool,
  every pool verdict that is not a clean kill judged again alone. A kill needs a passing build and a non-zero test
  exit, as in the gate.
- The definitions come from the results record, never from the producer: its commands are not run again. A row
  whose definition no longer matches its digest is refused, and so is a row that is not deferred, or that a plants
  record bound to the same record (or one it reused) already judged killed, unless `--rejudge`.
- The verdicts are appended through the store as a new immutable record of kind `plants`,
  `program/evidence/<ID>/plants/<utc>-<sha256[:12]>.json`, in the task worktree, and listed in the task's pointer.
  It names the task, the results record it judged (`part_of`: path and digest), the head, the kit version and each
  row's verdict in the gate's row schema with the row's definition digest. The implementer commits it with the
  pointer on the task branch; after the reviewed commit, merge accepts records directly in `plants/`.
- QA's acceptance (`pk evidence accept`), `pk status set <ID> done` and `pk merge` need every deferred row killed in
  such a record.

The kit takes no machine lock here, as in the gate; a program that serialises heavy runs wraps this command in its
own lock.
"""
from __future__ import annotations

import time
from pathlib import Path

import evidence as evidence_module
import gate
import latework
import store
from common import KitError, git, utc_now

KIT_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def kit_version() -> str:
    try:
        return KIT_VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _worktree(root: Path, task_id: str, branch: str, override) -> Path:
    """The task worktree; uncommitted files the kit writes for the task (an earlier chunk's record) are allowed."""
    return gate._task_worktree(root, task_id, branch, override, allow=lambda path: latework.own_path(path, task_id),
                               command="pk gate plants")


def _checked_rows(root: Path, head: str, rows: list[dict]) -> None:
    """KitError unless each deferred row's definition matches its digest and its edit applies once at `head`."""
    for row in rows:
        rid = gate._show(row["id"])
        if evidence_module.definition_digest(row) != row["definition_sha256"]:
            raise KitError(f"deferred row {rid}: its definition does not match its definition_sha256; nothing judged")
        path = evidence_module.Evidence._norm(row["path"])
        listing = git(root, "ls-tree", head, "--", path).split(None, 3)
        if not listing or listing[1] != "blob" or listing[0] not in ("100644", "100755"):
            raise KitError(f"deferred row {rid}: {gate._show(path)} is not a regular file at {head}")
        text = gate._git_bytes(root, "show", f"{head}:{path}").decode("utf-8", "replace")
        count = text.count(row["find"])
        if count != 1:
            raise KitError(f"deferred row {rid}: find occurs {count} times in {gate._show(path)} at {head} (it must "
                           "occur exactly once)")


def select(data: dict, coverage: dict, names: list[str] | None, all_pending: bool, rejudge: bool) -> list[dict]:
    """The deferred rows to judge, in table order; KitError for a name that is unknown, not deferred, or killed."""
    rows = [row for row in data["planted_bugs"] if row.get("id") != evidence_module.SELF_TEST_ID]
    deferred = {row["id"]: row for row in rows if evidence_module.is_deferred(row)}
    if all_pending:
        wanted = [rid for rid in coverage["pending"]]
    else:
        wanted = list(dict.fromkeys(name for name in names or [] if name))
        if not wanted:
            raise KitError("name the rows to judge: --rows ID[,ID...], or --all-pending")
        known = {row["id"] for row in rows}
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise KitError(f"no planted-bug row {', '.join(map(gate._show, unknown))} in the record")
        judged = [name for name in wanted if name not in deferred]
        if judged:
            raise KitError(f"{', '.join(map(gate._show, judged))}: not deferred; the gate judged "
                           f"{'it' if len(judged) == 1 else 'them'} (pk gate rejudge re-runs a judged row as feedback)")
        done = [name for name in wanted if name in coverage["killed"]]
        if done and not rejudge:
            raise KitError("already judged killed in a plants record bound to this record: "
                           + ", ".join(f"{gate._show(name)} ({coverage['killed'][name]})" for name in done)
                           + "; pass --rejudge to judge again")
    return [deferred[rid] for rid in deferred if rid in wanted]


def plants(root, task_id: str, branch: str, names: list[str] | None = None, all_pending: bool = False,
           workers: int | None = None, rejudge: bool = False, timeout: int | None = gate.STEP_TIMEOUT,
           worktree=None) -> int:
    """Judge deferred rows of the task's latest results record and append a plants record; 0 when every judged row
    was killed (and nothing was pending), 1 otherwise."""
    root = Path(root).resolve()
    gate._task_branch(task_id, branch)
    if all_pending and names:
        raise KitError("pass --rows or --all-pending, not both")
    wt = _worktree(root, task_id, branch, worktree)
    entry = store.read_pointer(wt, task_id).get("results")
    if entry is None:
        raise KitError(f"{store.pointer_path(task_id)} names no results record: run the hand-back gate first")
    data = store.load(wt, entry["path"], entry["digest"])
    problems = evidence_module.validate_results(data)
    if problems:
        raise KitError(f"{entry['path']}: {problems[0]}")
    if data["task_id"] != task_id:
        raise KitError(f"{entry['path']} is a record of {gate._show(data['task_id'])}, not of {task_id}")
    if not any(evidence_module.is_deferred(row) for row in data["planted_bugs"]):
        raise KitError(f"{entry['path']} deferred no planted-bug rows: the gate judged every row; nothing to do")
    head = data["head"]
    try:
        git(wt, "cat-file", "-e", f"{head}^{{commit}}")
    except KitError:
        raise KitError(f"the head {head} of {entry['path']} is not a commit of this repository") from None
    coverage = evidence_module.plants_coverage(wt, task_id, entry, data)
    if coverage["problems"]:
        raise KitError(coverage["problems"][0])
    rows = select(data, coverage, names, all_pending, rejudge)
    command = f"pk gate plants {task_id} --branch {branch}"
    if not rows:
        gate._out(f"plants: every deferred row of {entry['path']} is already killed in a plants record; nothing to do")
        return 0
    _checked_rows(wt, head, rows)
    count = evidence_module.plant_workers(root, workers)
    gate._out(f"plants: judging {len(rows)} deferred row(s) of {entry['path']} at its head {head} with {count} "
              f"worker(s)")
    runner = evidence_module.Evidence.judging_at(wt, head)
    items = [{"id": row["id"], "path": row["path"], "find": row["find"], "replace": row["replace"],
              "build_argv": row["build_argv"], "test_argv": row["test_argv"], "serial": row["serial"],
              "test_args": []} for row in rows]
    started = time.monotonic()
    judged = evidence_module._PlantPool(runner, items, None, [], timeout, count, self_test=False).run()
    seconds = round(time.monotonic() - started, 1)
    out = [{**verdict, "serial": row["serial"], "definition_sha256": row["definition_sha256"]}
           for verdict, row in zip(judged, rows)]
    record = {"task_id": task_id, "kind": store.PLANTS, "part_of": {"path": entry["path"], "digest": entry["digest"]},
              "head": head, "kit_version": kit_version(), "produced_at": utc_now(), "workers": count,
              "seconds": seconds, "rejudge": bool(rejudge), "rows": out,
              "all_killed": all(row["killed"] for row in out)}
    problems = evidence_module.validate_plants(record)
    if problems:
        raise KitError("; ".join(problems))
    appended = store.record(wt, task_id, store.PLANTS, record)
    killed = sum(row["killed"] for row in out)
    gate._out(f"plants: {killed} of {len(out)} killed at {head[:12]} ({seconds:.0f} s); wrote {appended['path']}")
    for row in out:
        if not row["killed"]:
            gate._out(f"plants: {gate._show(row['id'])} {row['kill_reason']}")
    gate._out(f"plants: commit {appended['path']} and {store.pointer_path(task_id)} on {branch}")
    after = evidence_module.plants_coverage(wt, task_id, entry, data)
    if after["pending"]:
        gate._out(f"plants: {len(after['pending'])} of {len(after['deferred'])} deferred rows have no killed verdict "
                  f"yet; judge the rest with: {command} --all-pending")
    else:
        gate._out(f"plants: every deferred row of {entry['path']} is killed; QA can accept the record")
    return 0 if killed == len(out) else 1


def cmd_plants(args) -> int:
    names = [name.strip() for part in args.rows or [] for name in part.split(",")]
    return plants(gate.find_root(), args.task_id, args.branch, names=names, all_pending=args.all_pending,
                  workers=args.workers, rejudge=args.rejudge, timeout=args.timeout, worktree=args.worktree)


def configure(actions) -> None:
    """Add the `plants` action to `pk gate`'s action parsers."""
    parser = actions.add_parser("plants", help="judge the deferred planted-bug rows of the task's latest results "
                                               "record at its head and append a plants record bound to it")
    parser.add_argument("task_id", metavar="ID", help="task ID, for example P0-M01-S01-T01")
    parser.add_argument("--branch", required=True, help="the task branch, task/<ID>")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--rows", action="append", metavar="ID[,ID...]",
                       help="the deferred rows to judge (repeat or separate with commas)")
    which.add_argument("--all-pending", action="store_true",
                       help="every deferred row that has no killed verdict in a plants record yet")
    parser.add_argument("--workers", type=int, metavar="N", help="rows judged at once (default: PK_PLANT_WORKERS, "
                                                                  "else evidence.plant_workers, else 1)")
    parser.add_argument("--rejudge", action="store_true", help="judge a row again although a plants record bound to "
                                                               "this record already judged it killed")
    parser.add_argument("--timeout", type=int, default=gate.STEP_TIMEOUT, metavar="SECONDS",
                        help=f"per command (default {gate.STEP_TIMEOUT})")
    parser.add_argument("--worktree", help="the task worktree (default: <root>/.worktrees/<id lowercased>)")
