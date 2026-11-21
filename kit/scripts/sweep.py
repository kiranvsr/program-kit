#!/usr/bin/env python3
"""`pk sweep` (0.6.2, KF-60): the stale processes of this project, listed, and with `--kill` stopped.

    pk sweep [--kill]

Agents start wait loops (`until ...; do sleep N; done`), `tail -f` watchers and servers in the background, hand
back, and never stop them; in the program it came from 146 such loops, some ten days old, and 16 orphaned tails
had built up under one agent session. `pk sweep` lists what is stale, from one `ps` call:

- **pk runs that ended badly.** When pk starts a long-running child (`pk lock run`'s command, a gate step, a lane's
  ssh) it writes `<git common dir>/pk-procs/<pid>.json` (pid, start time, owner, command, pk's own pid) and removes
  it when the child ends. An entry whose child still runs, with the start time written, while the pk process that
  wrote it is gone, is stale whatever its age: pk was killed and left its child behind. Its descendants are listed
  with it. An entry whose pk still runs is active and left alone.
- **Wait loops and watchers.** A non-interactive shell (`sh`, `bash`, `zsh`, `dash`, `ksh` given `-c`) whose
  command loops or waits (it names `sleep`, `tail`, `until` or `while`, or has a `sleep` or `tail` child), whose
  command line or working directory points into this repository, one of its worktrees, its git common dir or a
  `processes.paths` prefix, and that is older than `processes.max_age_hours` (24); with it its `sleep` and `tail`
  children. An orphaned `tail` (its parent is the first process) whose command line or working directory points
  there and that is that old. Interactive shells, editors, browsers and anything else are never matched.

Never listed: another user's process, this command and its ancestors, a process holding a pk lock
(`<common>/<name>.lock/owner`) with its ancestors and descendants, and an active registered run likewise.

`--kill` stops only what the report lists: SIGTERM, then SIGKILL to what is still running after
`processes.kill_grace_seconds` (5). Before each signal it reads `ps` again and skips a pid that is gone, whose start
time moved (a reused pid) or that has since become protected. Without `--kill` nothing is written or signalled.

The working directory is read only for candidates whose command line does not match: from `/proc` on Linux, with
one `lsof` call elsewhere. `pk drift` counts the same list as D9 (a note unless `processes.drift: fail`).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from common import KitError, find_root, git, read_yaml

KEYS = ("max_age_hours", "paths", "kill_grace_seconds", "drift")
DEFAULTS = {"max_age_hours": 24, "paths": [], "kill_grace_seconds": 5, "drift": "note"}
DRIFT_MODES = ("note", "fail")
REGISTRY = "pk-procs"  # directory in the git common dir
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
WATCHERS = frozenset({"sleep", "tail"})
LOOPING = re.compile(r"(?<![\w-])(sleep|tail|until|while)(?![\w-])")
START_SLACK = 3  # seconds two readings of one process's start time may differ (ps reports whole seconds)
PS_ARGV = ["ps", "-A", "-ww", "-o", "pid=,ppid=,uid=,stat=,etime=,command="]
PS_TIMEOUT = 30
COMMAND_CHARS = 160  # of a command shown in the report


# --- configuration --------------------------------------------------------------------

def _number(value, what: str, least: float, most: float | None = None) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < least or \
            (most is not None and value > most):
        bound = f"from {least:g} to {most:g}" if most is not None else f"at least {least:g}"
        raise KitError(f"checks.yaml: processes.{what} must be a number {bound}")
    return value


def parse(value) -> dict:
    """The `processes` section of checks.yaml as {max_age_hours, paths, kill_grace_seconds, drift}; absent, the
    defaults (24 hours, no extra paths, 5 s, a note)."""
    if value is None:
        return {**DEFAULTS, "paths": []}
    if not isinstance(value, dict):
        raise KitError("checks.yaml: processes must be a mapping of " + ", ".join(KEYS))
    unknown = sorted(str(key) for key in value if key not in KEYS)
    if unknown:
        raise KitError(f"checks.yaml: processes: unknown key {', '.join(unknown)} (expected {', '.join(KEYS)})")
    found = {**DEFAULTS, "paths": []}
    if value.get("max_age_hours") is not None:
        found["max_age_hours"] = _number(value["max_age_hours"], "max_age_hours", 0)
    if value.get("kill_grace_seconds") is not None:
        found["kill_grace_seconds"] = _number(value["kill_grace_seconds"], "kill_grace_seconds", 0, 600)
    paths = value.get("paths")
    if paths is not None:
        if not isinstance(paths, list) or not all(isinstance(path, str) and path.strip() for path in paths):
            raise KitError("checks.yaml: processes.paths must be a list of absolute path prefixes")
        for path in paths:
            expanded = os.path.expanduser(path.strip())
            if not os.path.isabs(expanded) or expanded.rstrip("/") == "":
                raise KitError(f"checks.yaml: processes.paths entry {path!r} must be an absolute path other than /")
            found["paths"].append(expanded.rstrip("/"))
    if value.get("drift") is not None:
        if value["drift"] not in DRIFT_MODES:
            raise KitError("checks.yaml: processes.drift must be note or fail")
        found["drift"] = value["drift"]
    return found


def config(root: Path) -> dict:
    """The `processes` section of the checkout's `program/checks.yaml`; the defaults when it or the file is absent."""
    path = Path(root) / "program" / "checks.yaml"
    data = read_yaml(path) if path.is_file() else None
    return parse(data.get("processes") if isinstance(data, dict) else None)


# --- the registry of pk's own long-running children ------------------------------------

@functools.lru_cache(maxsize=16)
def _common_dir(cwd: str) -> Path | None:
    try:
        return Path(git(Path(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"))
    except (KitError, OSError):
        return None


def registry_dir(cwd) -> Path | None:
    """`<git common dir>/pk-procs` of the repository at `cwd`, None outside one."""
    common = _common_dir(str(Path(cwd).resolve()))
    return common / REGISTRY if common is not None else None


def record_child(cwd, pid: int, owner: str, argv, started: float | None = None) -> Path | None:
    """Write the registry entry of child `pid`; None when it cannot be written (never an error for the caller)."""
    folder = registry_dir(cwd)
    if folder is None:
        return None
    text = json.dumps({"pid": pid, "started": time.time() if started is None else started, "owner": str(owner),
                       "command": shlex.join(str(part) for part in argv)[:1000], "pk_pid": os.getpid(),
                       "cwd": str(cwd)}, sort_keys=True) + "\n"
    path = folder / f"{pid}.json"
    for _ in range(5):  # a peer removing the empty folder between our mkdir and our write
        try:
            folder.mkdir(exist_ok=True)
            tmp = folder / f".{pid}.{secrets.token_hex(4)}.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return path
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return None


def drop_child(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
    try:
        path.parent.rmdir()  # only when no other entry is left
    except OSError:
        pass


@contextmanager
def tracked(cwd, pid: int, owner: str, argv):
    """Keep child `pid` in the registry while the body runs; its entry is removed however the body ends."""
    path = record_child(cwd, pid, owner, argv)
    try:
        yield path
    finally:
        drop_child(path)


def entries(folder: Path | None) -> list[tuple[Path, dict]]:
    """(path, data) of each readable registry entry; an unreadable one is skipped."""
    if folder is None:
        return []
    found = []
    try:
        names = sorted(name for name in os.listdir(folder) if name.endswith(".json"))
    except OSError:
        return []
    for name in names:
        try:
            data = json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("pid"), int) and isinstance(data.get("pk_pid"), int):
            found.append((folder / name, data))
    return found


# --- reading processes ----------------------------------------------------------------

@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    stat: str
    age: int  # seconds since it started
    command: str

    @property
    def argv(self) -> list[str]:
        return self.command.split()

    @property
    def name(self) -> str:
        first = self.argv[0] if self.argv else ""
        return os.path.basename(first.lstrip("-"))


@dataclass
class Item:
    proc: Proc
    why: str
    started: float = field(default=0.0)  # epoch seconds, from now minus its age


def parse_etime(text: str) -> int:
    """Seconds of a `ps` etime, `[[dd-]hh:]mm:ss`."""
    days, _, rest = text.strip().rpartition("-")
    parts = [int(part) for part in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return (int(days) if days else 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_ps(text: str) -> list[Proc]:
    found = []
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5 or not all(part.isdigit() for part in parts[:3]):
            continue
        try:
            age = parse_etime(parts[4])
        except ValueError:
            continue
        found.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), parts[3], age,
                          parts[5] if len(parts) > 5 else ""))
    return found


def snapshot() -> list[Proc]:
    """Every process, from one `ps` call."""
    try:
        proc = subprocess.run(PS_ARGV, capture_output=True, text=True, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KitError(f"pk sweep: cannot read the process list with ps: {exc}") from None
    if proc.returncode != 0 and not proc.stdout:
        raise KitError("pk sweep: ps failed: " + " ".join(proc.stderr.split())[:200])
    return parse_ps(proc.stdout)


def cwds(pids: list[int]) -> dict[int, str]:
    """The working directory of each pid that can be read: /proc on Linux, else one lsof call."""
    if not pids:
        return {}
    found = {}
    if Path("/proc/self/cwd").exists():
        for pid in pids:
            try:
                found[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
        return found
    try:
        proc = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(str(pid) for pid in pids)],
                              capture_output=True, text=True, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    pid = None
    for line in proc.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            found[pid] = line[1:]
    return found


# --- what is stale --------------------------------------------------------------------

def project_paths(root: Path, extra: list[str]) -> list[str]:
    """Prefixes that mean "this project": the checkout, every worktree, the git common dir and `extra`, each also
    as its resolved path (a temp directory reads /private/tmp on one machine and /tmp in a command line)."""
    found = [str(root)]
    try:
        listing = git(root, "worktree", "list", "--porcelain")
        found += [line[len("worktree "):] for line in listing.splitlines() if line.startswith("worktree ")]
    except KitError:
        pass
    common = _common_dir(str(Path(root).resolve()))
    if common is not None:
        found.append(str(common))
    found += list(extra)
    prefixes = []
    for path in found:
        for form in (path.rstrip("/"), os.path.realpath(path).rstrip("/")):
            if form and form not in prefixes:
                prefixes.append(form)
    return prefixes


def _boundary(prefixes: list[str]) -> re.Pattern | None:
    if not prefixes:
        return None
    return re.compile("(?:" + "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True)) +
                      r")(?![A-Za-z0-9._-])")


def _under(path: str, prefixes: list[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _noninteractive_shell(proc: Proc) -> bool:
    argv = proc.argv
    if proc.name not in SHELLS or not argv or argv[0].startswith("-"):
        return False
    return any(arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:] for arg in argv[1:4])


def lock_holders(common: Path | None) -> set[int]:
    """The pids the owner files of `<common>/*.lock` name."""
    found = set()
    if common is None:
        return found
    for owner in common.glob("*.lock/owner"):
        try:
            for line in owner.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("pid=") and line[4:].strip().isdigit():
                    found.add(int(line[4:].strip()))
        except OSError:
            continue
    return found


def _family(pids: set[int], by_pid: dict[int, Proc], kids: dict[int, list[Proc]], up: bool = True) -> set[int]:
    """`pids` with their descendants and (when `up`) their ancestors."""
    found = set(pids)
    todo = list(pids)
    while todo:
        for child in kids.get(todo.pop(), []):
            if child.pid not in found:
                found.add(child.pid)
                todo.append(child.pid)
    if up:
        for pid in pids:
            seen = 0
            proc = by_pid.get(pid)
            while proc is not None and proc.ppid > 1 and seen < 256:
                found.add(proc.ppid)
                proc = by_pid.get(proc.ppid)
                seen += 1
    return found


def find(root: Path, cfg: dict, procs: list[Proc] | None = None, cwd_of=cwds, now: float | None = None,
         me: int | None = None, uid: int | None = None) -> dict:
    """{stale: [Item], active: int, dead: [registry path], prefixes: [str]} for the project at `root`.

    `procs`, `cwd_of`, `now`, `me` and `uid` are for tests: the process list, the working-directory reader, the
    clock, the process whose ancestors are never listed (this one) and the user whose processes count.
    """
    root = Path(root)
    procs = snapshot() if procs is None else procs
    now = time.time() if now is None else now
    me = os.getpid() if me is None else me
    uid = os.getuid() if uid is None else uid
    by_pid = {proc.pid: proc for proc in procs if not proc.stat.startswith("Z")}
    kids: dict[int, list[Proc]] = {}
    for proc in by_pid.values():
        kids.setdefault(proc.ppid, []).append(proc)
    common = _common_dir(str(root.resolve()))
    folder = common / REGISTRY if common is not None else None

    protected = _family({me}, by_pid, {}, up=True)  # this command and its ancestors (not its children)
    protected |= _family({pid for pid in lock_holders(common) if pid in by_pid}, by_pid, kids)
    stale: dict[int, Item] = {}
    active, dead, orphaned = 0, [], []
    for path, data in entries(folder):
        child = by_pid.get(data["pid"])
        started = data.get("started")
        if child is None or not isinstance(started, (int, float)) or \
                abs((now - child.age) - started) > START_SLACK:
            dead.append(path)  # ended, or the pid now belongs to another process
        elif data["pk_pid"] in by_pid:
            active += 1
            protected |= _family({data["pk_pid"], child.pid}, by_pid, kids)
        else:
            orphaned.append((child, data))
    for child, data in orphaned:
        why = f"pk run ended: {' '.join(str(data.get('owner', '')).split())[:60]} (pk pid {data['pk_pid']} gone)"
        stale[child.pid] = Item(child, why)
        for pid in _family({child.pid}, by_pid, kids, up=False) - {child.pid}:
            stale.setdefault(pid, Item(by_pid[pid], f"under {child.pid}"))

    prefixes = project_paths(root, cfg["paths"])
    pattern = _boundary(prefixes)
    limit = float(cfg["max_age_hours"]) * 3600

    def watcher_child(proc: Proc) -> bool:
        return any(kid.name in WATCHERS for kid in kids.get(proc.pid, []))

    candidates = []
    for proc in by_pid.values():
        if proc.uid != uid or proc.age < limit or proc.pid in protected or proc.pid <= 1:
            continue
        if _noninteractive_shell(proc) and (LOOPING.search(proc.command) or watcher_child(proc)):
            candidates.append((proc, "wait loop"))
        elif proc.name == "tail" and proc.ppid == 1:
            candidates.append((proc, "orphaned tail"))
    unmatched = [proc.pid for proc, _ in candidates if not (pattern and pattern.search(proc.command))]
    where = cwd_of(unmatched) if unmatched else {}
    for proc, why in candidates:
        if pattern and pattern.search(proc.command) or _under(where.get(proc.pid, ""), prefixes):
            stale.setdefault(proc.pid, Item(proc, why))
            if why == "wait loop":
                for kid in kids.get(proc.pid, []):
                    if kid.name in WATCHERS and kid.uid == uid and kid.pid not in protected:
                        stale.setdefault(kid.pid, Item(kid, f"{kid.name} of {proc.pid}"))
    items = [item for pid, item in sorted(stale.items()) if pid not in protected and pid != me]
    for item in items:
        item.started = now - item.proc.age
    return {"stale": items, "active": active, "dead": dead, "prefixes": prefixes}


# --- stopping -------------------------------------------------------------------------

def _still(item: Item, procs: dict[int, Proc], now: float) -> bool:
    """Whether the process the report listed still runs as that same process (its start time unmoved)."""
    proc = procs.get(item.proc.pid)
    return proc is not None and not proc.stat.startswith("Z") and \
        abs((now - proc.age) - item.started) <= START_SLACK


def stop(root: Path, cfg: dict, items: list[Item], read=snapshot, send=os.kill, clock=time.time,
         pause=time.sleep) -> tuple[int, int, int]:
    """SIGTERM each listed process, then SIGKILL what still runs after the grace; (termed, killed, skipped).

    Before each round the list is read again: a pid that is gone, whose start time moved or that has become
    protected (it took a pk lock, or a pk run registered it) is skipped. Nothing outside `items` is signalled."""
    def current() -> tuple[dict[int, Proc], set[int]]:
        procs = read()
        listed = find(root, cfg, procs=procs, now=clock())["stale"]
        return {proc.pid: proc for proc in procs}, {item.proc.pid for item in listed}

    procs, still_stale = current()
    now = clock()
    targets = [item for item in items if item.proc.pid in still_stale and _still(item, procs, now)]
    skipped = len(items) - len(targets)
    termed = 0
    for item in targets:
        try:
            send(item.proc.pid, signal.SIGTERM)
            termed += 1
        except OSError:
            pass
    deadline = clock() + float(cfg["kill_grace_seconds"])
    left = targets
    while left:
        procs = {proc.pid: proc for proc in read()}
        now = clock()
        left = [item for item in left if _still(item, procs, now)]
        if not left or now >= deadline:
            break
        pause(0.2)
    killed = 0
    for item in left:
        try:
            send(item.proc.pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return termed, killed, skipped


# --- command --------------------------------------------------------------------------

def _age(seconds: int) -> str:
    days, rest = divmod(int(seconds), 86400)
    return f"{days}d{rest // 3600:02d}h" if days else f"{rest // 3600}h{rest % 3600 // 60:02d}m"


def summary(found: dict, cfg: dict) -> str:
    count = len(found["stale"])
    return (f"{count} stale process(es) of this project (older than {cfg['max_age_hours']:g} h, or left by a pk run "
            "that ended); pk sweep lists them" if count else "no stale process of this project")


def cmd_sweep(args: argparse.Namespace) -> int:
    root = find_root()
    cfg = config(root)
    found = find(root, cfg)
    items = found["stale"]
    print(f"sweep: {len(items)} stale process(es) at {root} (older than {cfg['max_age_hours']:g} h; "
          f"pk runs that ended: any age)")
    for item in items:
        command = " ".join(item.proc.command.split())
        if len(command) > COMMAND_CHARS:
            command = command[:COMMAND_CHARS - 3] + "..."
        print(f"  {item.proc.pid:>7} {_age(item.proc.age):>6}  {item.why}: {command}")
    if found["active"] or found["dead"]:
        print(f"registry: {found['active']} active pk run(s) left alone; {len(found['dead'])} entr(y/ies) of ended "
              "processes" + (" removed" if args.kill else " (removed by --kill)"))
    if not args.kill:
        if items:
            print(f"report only: pk sweep --kill stops these (SIGTERM, then SIGKILL after "
                  f"{cfg['kill_grace_seconds']:g} s)")
        return 0
    for path in found["dead"]:
        drop_child(path)
    for item in items:
        if item.why.startswith("pk run ended"):
            drop_child(registry_dir(root) / f"{item.proc.pid}.json")
    if not items:
        return 0
    termed, killed, skipped = stop(root, cfg, items)
    print(f"stopped: {termed} sent SIGTERM, {killed} of them SIGKILL after {cfg['kill_grace_seconds']:g} s; "
          f"{skipped} skipped (gone, a reused pid, or protected since)")
    return 0


def drift_check(root: Path) -> tuple[str, bool, str]:
    """D9 for `pk drift`: (mode, ok, detail); mode `note` never drifts, `fail` drifts while anything is stale."""
    try:
        cfg = config(root)
    except KitError as exc:
        return "note", True, f"not read: {' '.join(str(exc).split())}"
    try:
        found = find(root, cfg)
    except KitError as exc:
        return cfg["drift"], True, f"not read: {' '.join(str(exc).split())}"
    return cfg["drift"], not found["stale"], summary(found, cfg)


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kill", action="store_true",
                        help="stop what the report lists: SIGTERM, then SIGKILL after processes.kill_grace_seconds")


def register(add_command) -> None:
    add_command("sweep", cmd_sweep, help="report-first: list this project's stale processes (wait loops, orphaned "
                "tails, children of pk runs that ended); --kill stops exactly those", configure=_configure)
