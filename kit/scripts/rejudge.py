#!/usr/bin/env python3
"""`pk gate rejudge`: re-run named planted-bug rows of a task branch the way the gate's runner does, writing nothing.

Between two hand-back gate runs a fixer wants to know, quickly, whether a changed row now builds and is killed. This
runs only the named rows: a clean detached mirror of the branch tip (`evidence.PlantJudge`, the mirror machinery of
`Evidence.planted_bugs`), one edit, the row's build, the row's test, the runner's verdict. It is not gate evidence: it
writes no results record, pointer or status, and its lines say so. Only the hand-back gate's record counts.

Where the rows come from, in order:
1. the task's producer `program/evidence/<ID>/produce.py` at the branch tip, when it defines `rejudge_rows()`: a list
   of rows `{id, path, find, replace, build_argv, test_argv}` (the shape of a results record's planted-bug row:
   `build_argv` one argv list or None, `test_argv` the full test argv). The producer is imported in a separate
   Python process with the mirror as its working directory; its main block does not run. This is the source that
   sees a row the producer changed since the last gate run;
2. otherwise the task's latest results record on the branch tip (read from git objects, as `pk gate binding` reads
   it): each row as the gate last judged it, with the build and test argv it recorded.

Only committed work is judged: the mirror is the branch tip, never a working tree. The kit takes no machine lock
here or in the gate; a program that serialises heavy runs wraps this command in its own lock as it wraps the gate.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import gate
import store
from common import KitError
from evidence import SELF_TEST_ID, PlantJudge

HOOK = "rejudge_rows"
NOT_EVIDENCE = "rejudge: fast feedback, not gate evidence: no results record is written; only the hand-back gate's counts"
_MARK = "pk-rejudge-rows: "
_BOOT = (
    "import importlib.util, json, sys\n"
    "path = sys.argv[1]\n"
    "spec = importlib.util.spec_from_file_location('produce', path)\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "sys.modules['produce'] = module\n"
    "spec.loader.exec_module(module)\n"
    "rows = module.rejudge_rows()\n"
    "sys.stdout.write('\\n' + sys.argv[2] + json.dumps(rows) + '\\n')\n"
)


def _producer_rows(judge: PlantJudge, task_id: str, timeout: int | None) -> list | None:
    """The rows `rejudge_rows()` of the producer at the mirror's commit returns, or None when it defines none."""
    produce = judge.mirror / "program" / "evidence" / task_id / "produce.py"
    if not produce.is_file() or f"def {HOOK}(" not in produce.read_text(encoding="utf-8", errors="replace"):
        return None
    try:
        proc = subprocess.run([sys.executable, "-B", "-c", _BOOT, str(produce), _MARK], cwd=str(judge.mirror),
                              capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise KitError(f"program/evidence/{task_id}/produce.py: {HOOK}() did not return within {timeout}s") from None
    finally:
        judge.restore()  # importing the producer may have written into the mirror
    found = [line[len(_MARK):] for line in proc.stdout.split("\n") if line.startswith(_MARK)]
    if proc.returncode != 0 or not found:
        tail = [line for line in proc.stderr.splitlines() if line.strip()][-1:] or [f"exit {proc.returncode}"]
        raise KitError(f"program/evidence/{task_id}/produce.py: {HOOK}() failed: {gate._show(tail[0])}")
    try:
        return json.loads(found[-1])
    except ValueError as exc:
        raise KitError(f"program/evidence/{task_id}/produce.py: {HOOK}() returned no JSON list: {exc}") from None


def _record_rows(root: Path, task_id: str, tip: str) -> tuple[list | None, str]:
    """(the planted-bug rows of the latest results record at `tip`, a label), or (None, "") without one."""
    entry = gate._pointer_at(root, tip, task_id).get("results")
    if entry is None:
        return None, ""
    raw = gate._git_bytes(root, "show", f"{tip}:{entry['path']}")
    data = store.parse(entry["path"], raw, entry["digest"])
    label = f"the latest results record {entry['digest'][:12]} (head {str(data.get('head'))[:12]})"
    return data.get("planted_bugs"), label


def _checked(rows, source: str) -> dict[str, dict]:
    """{id: row} of well-formed rows; the self-test row is left out. KitError names the first bad row."""
    if not isinstance(rows, list):
        raise KitError(f"{source}: the rows must be a list")
    found: dict[str, dict] = {}
    for index, row in enumerate(rows, 1):
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or not rid:
            raise KitError(f"{source}: row {index} has no string id")
        if rid == SELF_TEST_ID:
            continue
        build, test = row.get("build_argv"), row.get("test_argv")
        if not (isinstance(row.get("path"), str) and isinstance(row.get("find"), str) and row["find"]
                and isinstance(row.get("replace"), str)):
            raise KitError(f"{source}: row {gate._show(rid)} needs a path, a non-empty find and a replace")
        for name, value, required in (("build_argv", build, False), ("test_argv", test, True)):
            if (value is None and required) or (value is not None and not (
                    isinstance(value, list) and value and all(isinstance(part, str) and part for part in value))):
                raise KitError(f"{source}: row {gate._show(rid)}: {name} must be an argv list of strings")
        found[rid] = row
    return found


def _seconds(start: float) -> str:
    return f"({time.monotonic() - start:.0f} s)"


def _report(result: dict, took: str) -> list[str]:
    rid, reason = gate._show(result["id"]), result["kill_reason"]
    if result["problem"]:
        return [f"{rid} not judged: {result['problem']}"]
    if reason == "test failed":
        return [f"{rid} killed: build ok, test exit {result['test_exit']} {took}"]
    if reason == "not killed":
        return [f"{rid} not killed: build ok, test exit 0 {took}"]
    if result["build_exit"] == 0:  # the build passed; the test timed out or could not start
        return [f"{rid} build failed: the test did not run (exit {result['test_exit']}) {took}",
                *(gate.TAIL_PREFIX + line for line in result["test_tail"])]
    return [f"{rid} build failed: {' '.join(result['build_argv'])} exit {result['build_exit']} {took}",
            *(gate.TAIL_PREFIX + line for line in result["build_tail"])]


def rejudge(root, task_id: str, branch: str, names: list[str], baseline: bool = False,
            timeout: int | None = gate.STEP_TIMEOUT) -> int:
    """Re-judge the rows `names` of the task branch; 0 when every one was killed. Writes nothing (see the module)."""
    root = Path(root).resolve()
    gate._task_branch(task_id, branch)
    tip = gate._branch_head(root, branch)
    wanted = list(dict.fromkeys(name for name in names if name))
    if not wanted:
        raise KitError("name at least one planted-bug row: --rows ID[,ID...]")
    gate._out(NOT_EVIDENCE)
    with PlantJudge(root, tip, timeout=timeout) as judge:
        rows, source = _producer_rows(judge, task_id, timeout), f"program/evidence/{task_id}/produce.py {HOOK}()"
        if rows is None:
            rows, source = _record_rows(root, task_id, tip)
        if rows is None:
            raise KitError(f"{branch} has neither a producer that defines {HOOK}() nor a results record its pointer "
                           f"names: nothing says what the rows are")
        table = _checked(rows, source)
        unknown = [name for name in wanted if name not in table]
        if unknown:
            known = ", ".join(list(table)[:30]) + (" ..." if len(table) > 30 else "")
            raise KitError(f"no planted-bug row {', '.join(unknown)} in {source}; its rows: {known or 'none'}")
        gate._out(f"rejudge: {branch} at {tip}, rows from {source}")
        killed, failed_baselines = 0, {}
        for name in wanted:
            row = table[name]
            if baseline:
                key = (tuple(row.get("build_argv") or ()), tuple(row["test_argv"]))
                if key not in failed_baselines:
                    found = judge.baseline([row["build_argv"]] if row.get("build_argv") else [], row["test_argv"])
                    failed_baselines[key] = None if found["ok"] else found
                    gate._out(f"baseline {' '.join(row['test_argv'])}: "
                              + ("ok" if found["ok"] else f"{found['step']} exit {found['exit']}"))
                if failed_baselines[key] is not None:
                    gate._out(f"{gate._show(name)} not judged: the unmutated mirror fails its own "
                              f"{failed_baselines[key]['step']}, so a failing test kills nothing")
                    continue
            start = time.monotonic()
            result = judge.attempt(row)
            for line in _report(result, _seconds(start)):
                gate._out(line)
            killed += result["killed"] is True
    gate._out(f"rejudge: {killed} of {len(wanted)} killed; {NOT_EVIDENCE.split(': ', 1)[1]}")
    return 0 if killed == len(wanted) else 1


def cmd_rejudge(args) -> int:
    names = [name.strip() for part in args.rows for name in part.split(",")]
    return rejudge(gate.find_root(), args.task_id, args.branch, names, baseline=args.baseline,
                   timeout=args.timeout)


def configure(actions) -> None:
    """Add the `rejudge` action to `pk gate`'s action parsers."""
    parser = actions.add_parser("rejudge", help="re-run named planted-bug rows of a task branch on a clean mirror: "
                                                "fast feedback between gate runs, never gate evidence")
    parser.add_argument("task_id", metavar="ID", help="task ID, for example P0-M01-S01-T01")
    parser.add_argument("--branch", required=True, help="the task branch, task/<ID>")
    parser.add_argument("--rows", required=True, action="append", metavar="ID[,ID...]",
                        help="the planted-bug rows to re-judge (repeat or separate with commas)")
    parser.add_argument("--baseline", action="store_true",
                        help="first run each row's build and test on the unmutated mirror, as the gate's runner does")
    parser.add_argument("--timeout", type=int, default=gate.STEP_TIMEOUT, metavar="SECONDS",
                        help=f"per command (default {gate.STEP_TIMEOUT})")
