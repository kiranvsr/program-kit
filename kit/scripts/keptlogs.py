#!/usr/bin/env python3
"""Kept logs of failed gate steps (0.7.6, KF-77), standard library only.

A failing gate step prints only the last TAIL_LINES lines of its output, which for a whole-suite test command is the
summary and never the failing test. So when a step of `pk gate handback` or the merge check fails (build, compile
check, test, scan, extra gate, e2e, plan, evidence, an extension's step), its full combined output is kept on the
machine that ran it, outside every worktree, so no worktree clean-up removes it:

    <git common dir>/pk-logs/<ID>/<stamp>-<step>.log      the full output, as the command wrote it
    <git common dir>/pk-logs/<ID>/<stamp>-<step>.digest   its failure lines (MARKERS), at most DIGEST_LINES

`<stamp>` is the UTC time `YYYYMMDDTHHMMSSZ` (PK_LOG_STAMP when it holds one: `pk remote gate` sets it, so it can
find the run's digest on the lane). The gate prints the tail as before, then one `kept log:` line with both paths.
On a lane the common dir is the lane checkout's `.git`; `pk remote gate` copies the digest back next to this
checkout's kept logs and names the log on the lane, and `pk remote log` lists, greps, tails or copies it.

Until done. A task's kept logs go when the task is done: `pk merge` removes them here once it merged the task; a
gate that keeps a log here, and every lane step (after the lane's main is reset to the mirror's), removes the logs
of each task whose status record on that main is terminal (done, merged, adopted), and of each task whose newest
kept file is older than KEEP_DAYS days. A later passing gate leaves them. `PRUNE` is that clean-up as a stand-alone
script, so a lane runs it with nothing but python3; `prune` runs the same code here.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

DIR = "pk-logs"
KEEP_DAYS = 14  # a task's kept logs outlive it at most this long when it is never merged
DIGEST_LINES = 200
STAMP_ENV = "PK_LOG_STAMP"
MARKERS = ("FAIL", "✗", "×", "Error", "panic:")  # also matches `--- FAIL`, AssertionError, TypeError, ...
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z\Z")
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def stamp() -> str:
    """PK_LOG_STAMP when it holds a stamp, else the UTC time now, as `YYYYMMDDTHHMMSSZ`."""
    given = os.environ.get(STAMP_ENV, "").strip()
    return given if _STAMP.match(given) else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def folder(common: Path, task_id: str) -> Path:
    if not _TASK.match(task_id):
        raise ValueError(f"not a task id: {task_id!r}")
    return Path(common) / DIR / task_id


def plain(text: str) -> str:
    """`text` without terminal colour sequences."""
    return _ANSI.sub("", text)


def digest(output: str, cap: int = DIGEST_LINES) -> list[str]:
    """`<line number>: <line>` of each output line holding a failure marker, at most `cap` of them."""
    found = []
    for number, line in enumerate(plain(output).splitlines(), 1):
        if any(mark in line for mark in MARKERS):
            found.append(f"{number}: {line}")
            if len(found) >= cap:
                break
    return found


def _name(step: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", step).strip(".-") or "step"


def keep(common: Path, task_id: str, step: str, header: str, output: str) -> tuple[Path, Path, int, int]:
    """Write `output` in full, and its digest, under the task's kept-log folder; (log, digest, lines, failure lines).

    The digest starts with `# kept log: <log path>` and `# <header>` (the failing command and its exit)."""
    where = folder(common, task_id)
    where.mkdir(parents=True, exist_ok=True)
    base, n = f"{stamp()}-{_name(step)}", 1
    log = where / f"{base}.log"
    while log.exists() or log.is_symlink():
        n += 1
        log = where / f"{base}-{n}.log"
    report = log.with_suffix(".digest")
    with open(log, "x", encoding="utf-8", errors="replace") as handle:
        handle.write(output)
    lines = digest(output)
    header = _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", header)  # one line, whatever the argv held
    report.write_text("\n".join([f"# kept log: {log}", f"# {header}", *lines]) + "\n", encoding="utf-8",
                      errors="replace")
    return log, report, len(output.splitlines()), len(lines)


def remove(common: Path, task_id: str) -> int:
    """Remove the task's kept logs; how many files went."""
    where = folder(common, task_id)
    if where.is_symlink():
        where.unlink()
        return 1
    if not where.is_dir():
        return 0
    count = sum(1 for _ in where.iterdir())
    import shutil
    shutil.rmtree(where)
    return count


# The clean-up, stand-alone (python3 -c PRUNE <common dir> <days> <repository> <ref>): remove each task's kept-log
# folder whose task's status record at <ref> of <repository> is terminal, or whose newest file is older than <days>
# days. A link is unlinked, never followed. Prints one line per folder removed.
PRUNE = r'''
import json, os, shutil, subprocess, sys, time

TERMINAL = ("done", "merged", "adopted")


def finished(repo, ref, task):
    try:
        raw = subprocess.run(["git", "-C", repo, "show", f"{ref}:program/status/{task}.json"], capture_output=True,
                             timeout=60).stdout
        return json.loads(raw.decode("utf-8-sig")).get("status") in TERMINAL
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return False


def prune(common, days, repo, ref, now=None):
    top = os.path.join(common, "pk-logs")
    if os.path.islink(top) or not os.path.isdir(top):
        return []
    now = time.time() if now is None else now
    gone = []
    for task in sorted(os.listdir(top)):
        path = os.path.join(top, task)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        times = [os.lstat(os.path.join(path, name)).st_mtime for name in os.listdir(path)]
        old = not times or now - max(times) > float(days) * 86400
        why = "done" if finished(repo, ref, task) else "older than %s days" % days if old else None
        if why:
            shutil.rmtree(path)
            gone.append((task, why))
    return gone


if __name__ == "__main__":
    for task, why in prune(*sys.argv[1:5]):
        print("remote: removed the kept logs of %s (%s)" % (task, why), flush=True)
'''


def prune(common: Path, repo: Path, ref: str, days: int = KEEP_DAYS, now: float | None = None) -> list[tuple[str, str]]:
    """Run PRUNE here: (task, why) of each kept-log folder removed."""
    space: dict = {"__name__": "pk_keptlogs_prune"}
    exec(compile(PRUNE, "keptlogs.PRUNE", "exec"), space)
    return space["prune"](str(common), days, str(repo), ref, now)
