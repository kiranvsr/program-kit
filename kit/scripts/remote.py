#!/usr/bin/env python3
"""`pk remote`: run heavy steps on other machines over plain ssh, git and tar (0.6.0), opt-in by checks.yaml.

    pk remote status
    pk remote gate <ID> [--lane L|auto] [--host VAR] [--size S|any] [--dry-run] [-- extra pk gate handback args]
    pk remote plants <ID> (--rows ID,... | --all-pending) [--chunk-seconds N [--per-row-seconds S]
                     [--chunk K/M | --plan]] [--rejudge] [--lane L|auto] [--host VAR] [--size S|any] [--dry-run]
    pk remote run [--lane L|auto] [--host VAR] [--size S|any] [--get PATH]... [--dry-run] <owner> -- <command> ...

Provider-neutral and host-neutral: the kit names no host, user, path or provider. The `remote` section of main's
program/checks.yaml says where (`parse`); the ssh destination is read at run time from the environment variable
it names (`host_env`, default PK_REMOTE_HOST), never from the file. From 0.7.2 `hosts_env: [VAR, ...]` names one
variable per machine instead (giving both is refused): each holds one destination, one that is unset or empty is
skipped with a note, and none set is the refusal of an unset `host_env`. Every host has the same layout and lane
names. On each host, under `dir`:

- `<dir>/repo.git`, a bare mirror. The runner pushes main and the task branch (for `run`, the worktree's HEAD as
  `refs/runs/<name>`, and main) to the chosen host's mirror ONLY, by its ssh path, with a forced update: the mirror
  is the lanes' alone. It never pushes to origin or to any other remote, and the remote machine needs no credential
  of the project's host.
- `<dir>/<lane>` for each lane, a clone of the mirror; each is its own checkout, so each has its own `pk lock`. On
  the lane, under that lane's lock (taken with the kit of the pushed main, extracted with `git archive`, so a lane's
  own checkout may be older), the step first removes every other entry of the lane's `.worktrees/` (0.7.2, below),
  then resets the lane's main to the mirror's, checks the task branch out in `.worktrees/<id lowercased>` (for
  `run`, a detached `.worktrees/run-<name>` with this worktree's uncommitted changes, deletions included, laid over
  it), runs each `setup` argv in that worktree (PK_LANE_ROOT names the lane root), then the step from the lane root
  (for `run`, from the same relative directory): `lane_wrap[lane]` + `wrap` + the command, with `env` exported and
  PK_PLANT_WORKERS set to `workers`. Output streams back and to `<dir>/<lane>.log`, timestamped, between a start
  and an end line. The exit status is the step's.
- Lane sizes (0.7.2). A `lanes` entry is a bare name (size `default`) or `{name: L, size: S}`; `sizes: {gate: S,
  plants: S, run: S}` gives the size a step kind takes by default (a kind left out takes any), `--size S|any`
  overrides it. `--lane auto` (the default) polls each host in order, one ssh call per host per poll, and takes the
  first lane of that size whose lock is free (or stale), in host order then lane order; it never falls back to
  another size and waits, saying for which size, while every such lane is busy. A host that does not answer is
  skipped for that poll with one note (a lone host is retried, as before). `--lane L` pins a lane on any host,
  `--host VAR` pins a host. A lane taken between the pick and the lock waits on the lock. With `hosts_env` the
  chosen lane is printed and logged as `VAR:lane`, never with the destination's value; a `host_env` section prints
  as 0.7.0 did.
- Lane worktrees (0.7.2, KF-66). Every step, under the lane's lock and before its own worktree is prepared, removes
  every direct child of `<lane root>/.worktrees` but its own (a symbolic link is unlinked, never followed; nothing
  outside that directory is touched), then runs `git worktree prune`, so at most one worktree stays per lane. A step
  whose records are still to be copied back marks its worktree in the lane's git dir (`pk-fetch/<name>`) until the
  copy-back has read it; the next step waits for that mark (at most FETCH_WAIT seconds) before it removes or resets
  the worktree. `keep_worktrees: true` turns the removal off. `disk_warn_percent` (1 to 99) warns, at each step's
  start and in `pk remote status`, when the base directory's file system is at least that full (`df -P`).
- After a gate step, `latest.json` and the new `results/` and `plants/` records of the task come back into the local
  task worktree; after a plants step only the new plants record files (never `latest.json`), which
  `pk gate plants --adopt` then lists in the local pointer. Only regular files under record names come back (for
  `run`, the `--get` paths). The controller commits them on the task branch as after a local run.
- `pk remote status` shows every configured host by its variable name: the base directory's disk use, then each
  lane's size, checkout, lock (free, busy with its holder and age, stale, or missing) and last log lines; a host that
  does not answer is shown as unreachable and status still exits 0.
- Every value that reaches a shell is quoted (shlex); IDs, lanes, sizes and owners are checked; an argument naming
  this machine's home or worktree is refused (the command runs elsewhere). `--dry-run` prints the push, the lane plan
  and the ssh command, and runs nothing.

PK_REMOTE_SSH replaces the ssh command (`ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=60`) and
serves the push too (as GIT_SSH_COMMAND, unless that is set); PK_REMOTE_POLL sets the seconds between lane polls.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

import evidence as evidence_module
import gate
import latework
import lock
import plants as plants_module
import store
from common import KitError, find_root, git

KEYS = ("host_env", "hosts_env", "dir", "dir_env", "lanes", "sizes", "setup", "env", "wrap", "lane_wrap", "workers",
        "keep_worktrees", "disk_warn_percent")
DEFAULT_HOST_ENV = "PK_REMOTE_HOST"
DEFAULT_SIZE = "default"  # the size of a lane given by its bare name (0.7.2)
KINDS = ("gate", "plants", "run")  # the step kinds remote.sizes may name
FETCH_WAIT = 120  # seconds a step waits for an earlier step's copy-back before it removes that step's worktree
SSH_ENV = "PK_REMOTE_SSH"
POLL_ENV = "PK_REMOTE_POLL"
POLL_SECONDS = 20.0
SSH_DEFAULT = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=60")
PUSH_TRIES = 3
ADOPT_LOCK = "pk-remote-adopt"  # the local lock around adopting, so parallel chunks never race on latest.json
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_LANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9@._-]*\Z")


# --- the checks.yaml section ------------------------------------------------------------------------------

def _argv(value, where: str, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not empty) or not all(isinstance(p, str) and p for p in value):
        raise KitError(f"checks.yaml: {where} must be {'an' if empty else 'a non-empty'} argv list of strings "
                       "(quote numbers), not a shell string")
    return list(value)


def _check_dir(base, where: str) -> str:
    if not isinstance(base, str) or not base.strip() or base != base.strip() or any(ch in base for ch in ":\n\r\0") \
            or base.startswith("~") or ".." in PurePosixPath(base).parts:
        raise KitError(f"{where} must be a directory path on the remote, absolute or relative to its home, without "
                       "~, .., a colon or a line break")
    return base.rstrip("/") or "/"


def _lanes(value) -> tuple[list[str], dict[str, str], bool]:
    """(lane names, size of each lane, whether any lane was given as a map) of `remote.lanes`."""
    if not isinstance(value, list) or not value:
        raise KitError("checks.yaml: remote.lanes must list at least one lane name (letters, digits, '.', '_', '-'; "
                       "not auto), each a clone of <dir>/repo.git at <dir>/<lane>")
    names, sizes, mapped = [], {}, False
    for item in value:
        size = DEFAULT_SIZE
        if isinstance(item, dict):
            mapped = True
            if "name" not in item or set(item) - {"name", "size"}:
                raise KitError("checks.yaml: remote.lanes: a lane given as a map has a name and a size only, for "
                               "example {name: lane1, size: big}")
            item, size = item["name"], item.get("size", DEFAULT_SIZE)
        if not (isinstance(item, str) and _LANE.match(item) and item not in ("auto", "repo.git")):
            raise KitError("checks.yaml: remote.lanes must list at least one lane name (letters, digits, '.', '_', "
                           "'-'; not auto), each a clone of <dir>/repo.git at <dir>/<lane>")
        if not (isinstance(size, str) and _LANE.match(size) and size not in ("any", "auto")):
            raise KitError(f"checks.yaml: remote.lanes: the size of {item} must be a word (letters, digits, '.', '_', "
                           "'-'; not any or auto), for example big or small")
        names.append(item)
        sizes[item] = size
    if len(set(names)) != len(names):
        raise KitError("checks.yaml: remote.lanes names a lane twice")
    return names, sizes, mapped


def parse(value) -> dict | None:
    """The `remote` section of checks.yaml, checked, or None when absent (pk remote refuses then).

    {host_env: str, dir: str | None, dir_env: str | None, lanes: [str], setup: [[str]], env: {str: str},
    wrap: [str], lane_wrap: {lane: [str]}, workers: int | None}. Exactly one of `dir` and `dir_env` (the variable
    that holds it) names the base directory on the remote; a relative one is relative to the remote user's home.

    The keys of 0.7.2 appear only when the section uses them, so a 0.7.0 section parses to the same mapping:
    `hosts_env: [str]` (then `host_env` is None), `lane_sizes: {lane: size}` (when a lane is given as a map),
    `sizes: {kind: size}`, `keep_worktrees: bool` and `disk_warn_percent: int`. `config` fills the defaults."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KitError("checks.yaml: remote must be a mapping of " + ", ".join(KEYS))
    unknown = sorted(str(key) for key in value if key not in KEYS)
    if unknown:
        raise KitError(f"checks.yaml: remote: unknown key {', '.join(unknown)} (expected {', '.join(KEYS)})")
    hosts_env = value.get("hosts_env")
    if hosts_env is not None:
        if value.get("host_env") is not None:
            raise KitError("checks.yaml: remote: give host_env (one machine) or hosts_env (several), not both")
        if not isinstance(hosts_env, list) or not hosts_env or not all(isinstance(name, str) and _ENV_NAME.match(name)
                                                                       for name in hosts_env):
            raise KitError("checks.yaml: remote.hosts_env must list environment variable names, one per machine "
                           "(each holds that machine's ssh destination; the file never does)")
        if len(set(hosts_env)) != len(hosts_env):
            raise KitError("checks.yaml: remote.hosts_env names a variable twice")
        host_env = None
    else:
        host_env = value.get("host_env") or DEFAULT_HOST_ENV
        if not isinstance(host_env, str) or not _ENV_NAME.match(host_env):
            raise KitError("checks.yaml: remote.host_env must name an environment variable (it holds the ssh "
                           f"destination; the file never does), for example {DEFAULT_HOST_ENV}")
    base, base_env = value.get("dir"), value.get("dir_env")
    if (base is None) == (base_env is None):
        raise KitError("checks.yaml: remote needs exactly one of dir (the base directory on the remote) and dir_env "
                       "(the environment variable that holds it)")
    if base is not None:
        _check_dir(base, "checks.yaml: remote.dir")
    if base_env is not None and not (isinstance(base_env, str) and _ENV_NAME.match(base_env)):
        raise KitError("checks.yaml: remote.dir_env must name an environment variable")
    lanes, lane_sizes, mapped = _lanes(value.get("lanes"))
    sizes = value.get("sizes")
    if sizes is not None:
        if not isinstance(sizes, dict) or not all(kind in KINDS for kind in sizes):
            raise KitError("checks.yaml: remote.sizes must map gate, plants and run (any of them) to a lane size")
        for kind, size in sizes.items():
            if size not in lane_sizes.values():
                raise KitError(f"checks.yaml: remote.sizes.{kind}: {size!r} is not the size of a lane in remote.lanes "
                               f"({', '.join(sorted(set(lane_sizes.values())))})")
    keep = value.get("keep_worktrees")
    if keep is not None and not isinstance(keep, bool):
        raise KitError("checks.yaml: remote.keep_worktrees must be true or false")
    warn = value.get("disk_warn_percent")
    if warn is not None and not (isinstance(warn, int) and not isinstance(warn, bool) and 1 <= warn <= 99):
        raise KitError("checks.yaml: remote.disk_warn_percent must be a whole number from 1 to 99")
    setup = value.get("setup") or []
    if not isinstance(setup, list):
        raise KitError("checks.yaml: remote.setup must be a list of argv lists")
    env = value.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and _ENV_NAME.match(k) and isinstance(v, str)
                                            for k, v in env.items()):
        raise KitError("checks.yaml: remote.env must map environment variable names to strings (quote numbers)")
    if "PK_LOCK_NAME" in env:
        lock.lock_name(env["PK_LOCK_NAME"])
    lane_wrap = value.get("lane_wrap") or {}
    if not isinstance(lane_wrap, dict):
        raise KitError("checks.yaml: remote.lane_wrap must map lane names to argv lists")
    strays = sorted(str(lane) for lane in lane_wrap if lane not in lanes)
    if strays:
        raise KitError(f"checks.yaml: remote.lane_wrap names {', '.join(strays)}, not in remote.lanes")
    workers = value.get("workers")
    if workers is not None and not (isinstance(workers, int) and not isinstance(workers, bool)
                                    and 1 <= workers <= evidence_module.MAX_WORKERS):
        raise KitError(f"checks.yaml: remote.workers must be a whole number from 1 to {evidence_module.MAX_WORKERS}")
    parsed = {"host_env": host_env, "dir": base, "dir_env": base_env, "lanes": list(lanes),
              "setup": [_argv(item, "remote.setup: each command") for item in setup], "env": dict(env),
              "wrap": _argv(value.get("wrap") or [], "remote.wrap", empty=True),
              "lane_wrap": {lane: _argv(argv, f"remote.lane_wrap.{lane}", empty=True)
                            for lane, argv in lane_wrap.items()},
              "workers": workers}
    for key, item in (("hosts_env", hosts_env), ("lane_sizes", lane_sizes if mapped else None), ("sizes", sizes),
                      ("keep_worktrees", keep), ("disk_warn_percent", warn)):
        if item is not None:
            parsed[key] = dict(item) if isinstance(item, dict) else list(item) if isinstance(item, list) else item
    return parsed


def config(parsed: dict) -> dict:
    """A parsed section with the 0.7.2 keys filled: `hosts_env` (the variables, in order), `lane_sizes`, `sizes`,
    `keep_worktrees` and `disk_warn_percent` (None: no warning)."""
    return {**parsed, "hosts_env": parsed.get("hosts_env"),
            "host_vars": list(parsed.get("hosts_env") or [parsed["host_env"]]),
            "lane_sizes": dict(parsed.get("lane_sizes") or {lane: DEFAULT_SIZE for lane in parsed["lanes"]}),
            "sizes": dict(parsed.get("sizes") or {}), "keep_worktrees": bool(parsed.get("keep_worktrees")),
            "disk_warn_percent": parsed.get("disk_warn_percent")}


# --- configuration at run time ------------------------------------------------------------------------------

def _settings(root: Path, quiet: bool = False) -> dict:
    """The `remote` section of main's committed checks.yaml (`config`), with the hosts (and a `dir_env` directory)
    read from the environment; KitError naming the fix when the section, every host or the directory is missing.

    `hosts` lists {var, host} of each set host variable in order; an unset one is skipped with one note (none with
    `quiet`). `host` and `host_var` name the first."""
    parsed = gate.load_checks_at(root, gate.MAIN)["remote"]
    if parsed is None:
        raise KitError("program/checks.yaml on main has no remote section: pk remote is opt-in; see the remote "
                       "example in kit/templates/checks.yaml")
    settings = config(parsed)
    hosts, unset = [], []
    for var in settings["host_vars"]:
        host = os.environ.get(var, "").strip()
        if not host:
            unset.append(var)
            continue
        if not _HOST.match(host):
            raise KitError(f"{var} does not hold an ssh destination this runner uses (letters, digits, @ . _ -; "
                           "anything else goes in an alias of your ssh config)")
        hosts.append({"var": var, "host": host})
    if not hosts:
        if settings["hosts_env"] is None:
            raise KitError(f"{settings['host_env']} is not set: export it with the ssh destination of the lanes' "
                           "machine (an ssh alias or user@host; the kit never stores it)")
        raise KitError(f"none of {', '.join(settings['host_vars'])} is set: export each with the ssh destination of "
                       "its lanes' machine (an ssh alias or user@host; the kit never stores it)")
    if not quiet:
        for var in unset:
            _say(f"{var} is not set; its lanes are skipped")
    if settings["dir"] is not None:
        base = _check_dir(settings["dir"], "checks.yaml: remote.dir")
    else:
        raw = os.environ.get(settings["dir_env"], "").strip()
        if not raw:
            raise KitError(f"{settings['dir_env']} is not set: export it with the base directory on the remote "
                           "(remote.dir_env in program/checks.yaml)")
        base = _check_dir(raw, settings["dir_env"])
    return {**settings, "hosts": hosts, "unset": unset, "host": hosts[0]["host"], "host_var": hosts[0]["var"],
            "base": base}


def _on(settings: dict, host: dict) -> dict:
    """The settings for one host: `host` and `host_var` name it."""
    return {**settings, "host": host["host"], "host_var": host["var"]}


def _label(settings: dict) -> str:
    """How the chosen host is printed: its variable's name with `hosts_env`; a `host_env` section prints the
    destination, as 0.7.0 did."""
    return settings["host"] if settings.get("hosts_env") is None else settings["host_var"]


def _ssh() -> list[str]:
    raw = os.environ.get(SSH_ENV, "").strip()
    return shlex.split(raw) if raw else list(SSH_DEFAULT)


def _poll() -> float:
    raw = os.environ.get(POLL_ENV, "").strip()
    try:
        value = float(raw) if raw else POLL_SECONDS
    except ValueError:
        raise KitError(f"{POLL_ENV} must be a number of seconds above 0, not {raw!r}") from None
    if not value > 0:
        raise KitError(f"{POLL_ENV} must be a number of seconds above 0, not {raw!r}")
    return value


def _q(value) -> str:
    return shlex.quote(str(value))


def _lock_name(settings: dict) -> str:
    return lock.lock_name(settings["env"].get("PK_LOCK_NAME") or lock.DEFAULT_NAME)


def _say(text: str) -> None:
    print(f"remote: {gate._show(text)}", file=sys.stderr, flush=True)


def _local_paths_problem(values: list[str], wt: Path) -> str | None:
    """Why an argument names a path of this machine, which the lane does not have, or None."""
    for value in values:
        for mark in (str(Path.home()), str(wt)):
            if mark and mark != "/" and mark in value:
                return (f"{value!r} names a path on this machine ({mark}); the command runs on the lane, so use a "
                        "path relative to the worktree")
    return None


def _take(rest: list[str], flags: dict[str, bool]) -> tuple[dict, list[str]]:
    """Known options at the front of a REMAINDER (`flags`: option -> takes a value), then an optional `--`.

    Parsed here, not by argparse: Python versions differ in whether a `--` before a REMAINDER reaches it."""
    found = {}
    while rest and rest[0] in flags:
        option = rest.pop(0)
        if flags[option]:
            if not rest:
                raise KitError(f"{option} needs a value")
            found[option] = rest.pop(0)
        else:
            found[option] = True
    if rest and rest[0] == "--":
        rest = rest[1:]
    return found, rest


# --- the remote shell ---------------------------------------------------------------------------------------

def _free_function(settings: dict) -> str:
    """A shell function `pk_free <lane dir>` printing free, stale, busy or missing for the lane's lock (as
    `pk lock status` judges it: a recorded pid that is not running is stale)."""
    name = _lock_name(settings)
    return ("pk_free() { c=$(git -C \"$1\" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || "
            "{ echo missing; return; }; l=\"$c/" + name + ".lock\"; if [ ! -d \"$l\" ]; then echo free; return; fi; "
            "p=$(sed -n 's/^pid=//p' \"$l/owner\" 2>/dev/null); if [ -n \"$p\" ] && ! kill -0 \"$p\" 2>/dev/null; "
            "then echo stale; else echo busy; fi; }")


def _exports(settings: dict) -> list[str]:
    lines = [f"export {key}={_q(value)}" for key, value in settings["env"].items()]
    if settings["workers"] is not None:
        lines.append(f"export PK_PLANT_WORKERS={settings['workers']}")
    lines.append("export PYTHONUNBUFFERED=1")
    return lines


def _prepare_task(task_id: str, branch: str) -> list[str]:
    """Lines that reset the lane's main to the mirror's and check the task branch out in its lane worktree (files an
    earlier step left go first, so the checkout cannot trip on them)."""
    w, b = _q(f".worktrees/{task_id.lower()}"), _q(branch)
    return [
        "git fetch -q --prune origin",
        "git checkout -q -f -B main origin/main",
        "git reset -q --hard origin/main",
        f"if [ -e {w}/.git ]; then",
        f"  git -C {w} reset -q --hard && git -C {w} clean -fdq",
        f"  git -C {w} checkout -q -f -B {b} origin/{b} && git -C {w} reset -q --hard origin/{b}",
        f"  git -C {w} clean -fdq",
        "else",
        f"  rm -rf {w}; git worktree prune; git worktree add -q -f -B {b} {w} origin/{b}",
        "fi",
    ]


# Run with python3 from the lane root, under the lane's lock: remove every direct child of <lane root>/.worktrees but
# `keep` (argv 1), never following a symbolic link (a link is unlinked; rmtree unlinks the links inside a tree), after
# waiting at most `wait` seconds (argv 3) for each entry's copy-back mark in <git common dir>/pk-fetch (argv 2).
_CLEAN = """\
import os, shutil, sys, time
keep, common, wait = sys.argv[1], sys.argv[2], float(sys.argv[3])
top = os.path.join(os.path.realpath(os.getcwd()), ".worktrees")
if os.path.islink(top) or (os.path.lexists(top) and not os.path.isdir(top)):
    print("remote: .worktrees is not a plain directory here; no lane worktree removed", flush=True)
    sys.exit(0)
gone = 0
for name in sorted(os.listdir(top)) if os.path.isdir(top) else []:
    path = os.path.join(top, name)
    link = os.path.islink(path)
    if os.path.dirname(path) != top or not (link or os.path.realpath(path) == path):
        continue
    mark, until, said = os.path.join(common, "pk-fetch", name), time.time() + wait, False
    while os.path.exists(mark) and time.time() < until:
        if not said:
            print(f"remote: waiting for the copy-back from .worktrees/{name}", flush=True)
            said = True
        time.sleep(0.5)
    if os.path.lexists(mark):
        os.unlink(mark)
    if name == keep and not link:
        continue
    if link or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)
    gone += 1
if gone:
    print(f"remote: removed {gone} earlier lane worktree(s) under .worktrees", flush=True)
"""


def _common_dir() -> str:
    return "$(git rev-parse --path-format=absolute --git-common-dir)"


def _clean_lines(settings: dict, keep: str) -> list[str]:
    """Lines (under the lane's lock, from the lane root, before the step's worktree is prepared) that remove every
    other lane worktree and prune git's list of them; none with keep_worktrees."""
    if settings["keep_worktrees"]:
        return []
    return [f"python3 -c {_q(_CLEAN)} {_q(keep)} \"{_common_dir()}\" {FETCH_WAIT} || "
            "echo 'remote: the lane worktree clean-up failed; the step runs on'",
            "git worktree prune"]


def _mark_lines(settings: dict, name: str) -> list[str]:
    """Lines that mark the step's worktree as waiting for its copy-back, which removes the mark."""
    if settings["keep_worktrees"]:
        return []
    return [f"pk_marks=\"{_common_dir()}/pk-fetch\"", "mkdir -p \"$pk_marks\"", f": > \"$pk_marks\"/{_q(name)}"]


def _disk_lines(settings: dict) -> list[str]:
    """Lines that warn on standard error when the base directory's file system is at least disk_warn_percent full."""
    warn = settings["disk_warn_percent"]
    if warn is None:
        return []
    return [f"pct=$(df -P \"$base\" 2>/dev/null | awk {_q('NR==2 {sub(/%/, "", $5); print $5}')})",
            f"if [ -n \"$pct\" ] && [ \"$pct\" -ge {int(warn)} ] 2>/dev/null; then echo \"remote: warning: the disk "
            f"of \"{_q(settings['host_var'])}\" is $pct% used (remote.disk_warn_percent {int(warn)})\" >&2; fi"]


def _setup_lines(settings: dict, where: str) -> list[str]:
    return [f"(cd {_q(where)} && PK_LANE_ROOT=\"$lane_root\" {shlex.join(argv)})" for argv in settings["setup"]]


def _prefix(settings: dict, lane_name: str) -> list[str]:
    return [*settings["lane_wrap"].get(lane_name, []), *settings["wrap"]]


def _lane_script(settings: dict, lane_name: str, owner: str, main_sha: str, body: list[str], short: str) -> str:
    """The script the ssh command runs: take the lane's lock with the pushed main's kit and run `body` under it from
    the lane root, its output timestamped to the caller and the lane's log; exit with the body's status."""
    inner = "\n".join(["set -e", "lane_root=$(pwd)", *body])
    where = "" if settings.get("hosts_env") is None else _q(f" on {settings['host_var']}:{lane_name}")
    return "\n".join([
        *_exports(settings),
        f"base={_q(settings['base'])}",
        'case "$base" in /*) ;; *) base="$(pwd)/$base" ;; esac',  # relative to the home the ssh session starts in
        f"log=\"$base\"/{_q(lane_name + '.log')}",
        "tmp=$(mktemp -d) || exit 1",
        "trap 'rm -rf \"$tmp\"' EXIT",
        f"git --git-dir=\"$base/repo.git\" archive {_q(main_sha)} kit/scripts | tar -x -C \"$tmp\" || "
        "{ echo 'remote: the pushed main has no kit/scripts to take the lane lock with' >&2; exit 1; }",
        *_disk_lines(settings),
        f"cd \"$base\"/{_q(lane_name)} || exit 1",
        f"echo \"$(date -u +%Y-%m-%dT%H:%M:%SZ) start {owner} at {short}\"{where} >> \"$log\"",
        "{ python3 \"$tmp/kit/scripts/pk.py\" lock run " + _q(owner) + " -- sh -c " + _q(inner) + " 2>&1; "
        "echo $? > \"$tmp/rc\"; } | while IFS= read -r line; do printf '%s %s\\n' \"$(date -u +%H:%M:%S)\" "
        "\"$line\"; done | tee -a \"$log\"",
        "rc=$(cat \"$tmp/rc\" 2>/dev/null || echo 1)",
        f"echo \"$(date -u +%Y-%m-%dT%H:%M:%SZ) end {owner} exit $rc\" >> \"$log\"",
        "exit \"$rc\"",
    ])


def _dest(settings: dict, shown: bool) -> str:
    """The ssh destination, or for printing with `hosts_env` a stand-in naming its variable (never the value)."""
    return f"<{settings['host_var']}>" if shown and settings.get("hosts_env") is not None else settings["host"]


def _scrub(settings: dict, text: str) -> str:
    """`text` (an error from ssh or git) with the destination replaced by its variable's stand-in under hosts_env."""
    return text.replace(settings["host"], _dest(settings, True)) if settings.get("hosts_env") is not None else text


def _ssh_argv(settings: dict, script: str, shown: bool = False) -> list[str]:
    return [*_ssh(), _dest(settings, shown), "sh -c " + _q(script)]


def _push_argv(settings: dict, refspecs: list[str], shown: bool = False) -> list[str]:
    return ["git", "push", "-q", "-f", f"{_dest(settings, shown)}:{settings['base']}/repo.git", *refspecs]


def _push(root: Path, settings: dict, refspecs: list[str]) -> None:
    """Push `refspecs` to the lanes' mirror by its ssh path (never to a named remote), retried on a lost ref race."""
    env = dict(os.environ)
    if not env.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = shlex.join(_ssh())
    for attempt in range(1, PUSH_TRIES + 1):
        proc = subprocess.run(_push_argv(settings, refspecs), cwd=str(root), env=env, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        if attempt == PUSH_TRIES:
            raise KitError(f"pk remote: the push to the lanes' mirror {settings['base']}/repo.git failed: "
                           + _scrub(settings, " ".join((proc.stderr or proc.stdout).split()))[:600])
        time.sleep(1)


def _remote(settings: dict, script: str, stdin: bytes | None = None, capture: bool = False):
    argv = _ssh_argv(settings, script)
    if capture:
        return subprocess.run(argv, input=stdin, capture_output=True)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL)
    import sweep  # late: the registry of pk's long-running children (0.6.2, KF-60)

    with sweep.tracked(Path.cwd(), proc.pid, "pk remote", [argv[0], _dest(settings, True), "(lane script)"]):
        proc.communicate(stdin)
    return proc


def _wanted_size(settings: dict, kind: str, size: str | None) -> str | None:
    """The lane size a step of `kind` takes: `--size` (any: None), else remote.sizes, else None (any)."""
    if size is not None:
        if size != "any" and size not in settings["lane_sizes"].values():
            raise KitError(f"no lane has size {gate._show(size)} (remote.lanes sizes: "
                           f"{', '.join(sorted(set(settings['lane_sizes'].values())))}, or any)")
        return None if size == "any" else size
    return settings["sizes"].get(kind)


def _candidates(settings: dict, wanted: str, host_var: str | None, size: str | None) -> list[tuple[dict, str]]:
    """(host, lane) pairs a step may take, in host order then lane order."""
    hosts = settings["hosts"]
    if host_var is not None:
        if host_var not in settings["host_vars"]:
            raise KitError(f"no host variable {gate._show(host_var)} in remote."
                           + ("host_env" if settings["hosts_env"] is None else "hosts_env")
                           + f" ({', '.join(settings['host_vars'])})")
        hosts = [host for host in hosts if host["var"] == host_var]
        if not hosts:
            raise KitError(f"{host_var} is not set: export it with the ssh destination of its lanes' machine")
    if wanted != "auto":
        if wanted not in settings["lanes"]:
            raise KitError(f"no lane {gate._show(wanted)} in remote.lanes ({', '.join(settings['lanes'])}, or auto)")
        lanes = [wanted]
    else:
        lanes = [lane for lane in settings["lanes"] if size is None or settings["lane_sizes"][lane] == size]
    return [(host, lane) for host in hosts for lane in lanes]


def _plan(settings: dict, pairs: list[tuple[dict, str]], wanted: str, size: str | None) -> str:
    """The dry run's words for where the step goes."""
    hosts = list(dict.fromkeys(host["var"] for host, _ in pairs))
    if settings["hosts_env"] is None:
        where = settings["host"]
    else:
        where = hosts[0] if len(hosts) == 1 else " or ".join(hosts) + " (in that order)"
    if wanted != "auto":
        return f"{where}, lane {wanted}" + (" (the first host where it is free)" if len(pairs) > 1 else "")
    lanes = ", ".join(dict.fromkeys(lane for _, lane in pairs))
    sized = f" {size} lane" if size is not None else ""
    return f"{where}, lane auto (the first free{sized} of {lanes}; waits while all are busy)"


def _pick(settings: dict, pairs: list[tuple[dict, str]], wanted: str, size: str | None) -> tuple[dict, str]:
    """A pinned lane on its one host as it is; else, polling each host with one ssh call per poll, the first pair
    whose lock is free or stale, waiting while every one is busy. A host that does not answer is skipped for that
    poll with one note; a lone host is retried, as before."""
    if wanted != "auto" and len(pairs) == 1:
        return pairs[0]
    order: dict[str, tuple[dict, list[str]]] = {}
    for host, lane in pairs:
        order.setdefault(host["var"], (host, []))[1].append(lane)
    alone, noted, announced = len(order) == 1, set(), False
    while True:
        answered = False
        for var, (host, lanes) in order.items():
            on = _on(settings, host)
            script = "\n".join([_free_function(on), f"base={_q(on['base'])}",
                                "for l in " + " ".join(_q(lane) for lane in lanes) + "; do",
                                '  s=$(pk_free "$base/$l"); if [ "$s" = free ] || [ "$s" = stale ]; then echo "$l"; '
                                "exit 0; fi",
                                "done", "echo none"])
            proc = _remote(on, script, capture=True)
            found = proc.stdout.decode("utf-8", "replace").split()[-1:] if proc.returncode == 0 else []
            if found and found[0] in lanes:
                return host, found[0]
            if found:
                answered = True
            elif not alone and var not in noted:
                _say(f"{_label(on)} does not answer; its lanes are skipped until it does")
                noted.add(var)
        if not announced and (answered or alone):  # an ssh hiccup while polling is retried, not fatal
            if not answered:
                _say("cannot reach the lanes yet; trying again")
            elif wanted != "auto":
                _say(f"lane {wanted} is busy on every host; waiting for it")
            elif size is not None:
                _say(f"every lane of size {size} is busy; waiting for one (no other size is taken)")
            else:
                _say("every lane is busy; waiting for one")
            announced = True
        time.sleep(_poll())


# --- copying back -------------------------------------------------------------------------------------------

def _fetch_files(settings: dict, where: str, listing: str,
                 mark: tuple[str, str] | None = None) -> list[tuple[str, bytes]]:
    """(relative path, bytes) of the files the remote `listing` command names (NUL-separated, relative to the remote
    directory `where`), read through one tar stream; [] when `where` is not there. `mark` (lane directory, worktree
    name) is the copy-back mark the step left, removed when this ends."""
    clear = []
    if mark is not None and not settings["keep_worktrees"]:
        lane_dir, name = mark
        clear = [f"c=$(git -C {_q(lane_dir)} rev-parse --path-format=absolute --git-common-dir 2>/dev/null) && "
                 "trap " + _q('rm -f "$c/pk-fetch/"' + _q(name)) + " EXIT"]
    script = "\n".join([*clear, f"cd {_q(where)} || exit 3", "l=$(mktemp) || exit 1", f"{listing} > \"$l\" || exit 1",
                        "COPYFILE_DISABLE=1 tar -cf - --null -T \"$l\"", "rc=$?", "rm -f \"$l\"", "exit $rc"])
    proc = _remote(settings, script, capture=True)
    if proc.returncode == 3:
        return []
    if proc.returncode != 0:
        raise KitError("pk remote: cannot read the step's files back: "
                       + _scrub(settings, " ".join(proc.stderr.decode("utf-8", "replace").split()))[:400])
    return _members(proc.stdout)


def _members(raw: bytes) -> list[tuple[str, bytes]]:
    """(name, bytes) of each regular file of a tar stream; KitError for a link or device (nothing is copied)."""
    found = []
    if not raw.strip(b"\0"):
        return found
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isreg():
                raise KitError(f"pk remote: {gate._show(member.name)} came back as a link or device; nothing copied")
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            handle = archive.extractfile(member)
            found.append((name, handle.read() if handle is not None else b""))
    return found


def _write_new(target: Path, raw: bytes, mode: int | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(raw)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, target)


def _bring_records(wt: Path, task_id: str, files: list[tuple[str, bytes]], kinds: tuple[str, ...],
                   pointer: bool) -> list[str]:
    """Write the copied-back records (and, with `pointer`, latest.json) into the task worktree; the paths written.

    Only record names of this task in `kinds` whose bytes match their name (and its pointer, checked) are accepted,
    or nothing is written; a record that is here with the same bytes is left, one with other bytes refused."""
    records, pointer_raw = [], None
    for name, raw in files:
        if pointer and name == store.pointer_path(task_id):
            pointer_raw = raw
            continue
        try:
            owner, kind, _ = store.check_rel(name)
        except KitError:
            owner, kind = None, None
        if owner != task_id or kind not in kinds or store.altered(name, raw):
            raise KitError(f"pk remote: {gate._show(name)} is not a {' or '.join(kinds)} record of {task_id} under "
                           "its own name; nothing copied")
        if (wt / name).exists() and (wt / name).read_bytes() != raw:
            raise KitError(f"pk remote: {name} is here with other bytes; nothing copied")
        records.append((name, raw))
    if pointer_raw is not None:
        store.pointer_from_text(pointer_raw.decode("utf-8", "replace"), task_id, store.pointer_path(task_id))
    written = []
    for name, raw in records:
        if not (wt / name).exists():
            _write_new(wt / name, raw, 0o444)
            written.append(name)
    if pointer_raw is not None:
        _write_new(wt / store.pointer_path(task_id), pointer_raw, None)
        written.append(store.pointer_path(task_id))
    return written


# --- the steps ----------------------------------------------------------------------------------------------

def _task_checks(root: Path, task_id: str, branch: str, kind: str) -> Path:
    """The local task worktree the step's records come back into; KitError unless it is fit for `kind`: clean for a
    gate; for plants, clean but for plants records and the pointer, with the latest results record committed."""
    wt = root / ".worktrees" / task_id.lower()
    if not wt.is_dir():
        raise KitError(f"pk remote {kind}: no local task worktree {wt} (pk worktree new {task_id})")
    try:
        git(root, "rev-parse", "-q", "--verify", f"refs/heads/{branch}")
    except KitError:
        raise KitError(f"pk remote {kind}: no local branch {branch}") from None
    dirty = gate._changes(wt)
    if kind == "plants":
        dirty = [path for path in dirty if not (latework.own_path(path, task_id) and (
            path.startswith(f"{store.BASE}/{task_id}/{store.PLANTS}/") or path == store.pointer_path(task_id)))]
    if dirty:
        raise KitError(f"pk remote {kind}: the task worktree {wt} has uncommitted changes ({', '.join(dirty[:5])}); "
                       "the lane runs the committed branch, so commit them first")
    if kind == "plants":
        try:
            text = git(root, "show", f"refs/heads/{branch}:{store.pointer_path(task_id)}")
            committed = store.pointer_from_text(text, task_id, store.pointer_path(task_id)).get("results")
        except KitError:
            committed = None
        here = store.read_pointer(wt, task_id).get("results")
        if committed is None or here is None or committed["digest"] != here["digest"]:
            raise KitError(f"pk remote plants: the latest results record of {task_id} here is not the one committed "
                           f"on {branch}; commit the evidence first (the lane judges the committed record)")
    return wt


def _sha(root: Path, ref: str) -> str:
    return git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _dry(settings: dict, what: str, plan: str, refspecs: list[str], command: list[str], script: str) -> None:
    print(f"remote: {gate._show(what)} on {plan}")
    print("remote: push (to the lanes' mirror only, never to origin): "
          + shlex.join(_push_argv(settings, refspecs, shown=True)))
    print(f"remote: on the lane, under its pk lock {_lock_name(settings)!r}: {shlex.join(command)}")
    print("remote: ssh: " + shlex.join(_ssh_argv(settings, script, shown=True)))
    print("remote: dry run: nothing pushed, nothing run")


def _target(settings: dict, kind: str, wanted: str, host_var: str | None, size: str | None,
            dry_run: bool) -> tuple[dict, str, str | None]:
    """(settings for the chosen host, lane, the dry run's plan or None): `--lane` and `--size` together are refused."""
    if wanted != "auto" and size is not None:
        raise KitError("pass --lane (one lane, whatever its size) or --size, not both")
    size = _wanted_size(settings, kind, size) if wanted == "auto" else None
    pairs = _candidates(settings, wanted, host_var, size)
    if not pairs:
        raise KitError(f"no lane of size {size} in remote.lanes")
    if dry_run:
        host, lane = pairs[0]
        return _on(settings, host), lane, _plan(settings, pairs, wanted, size)
    host, lane = _pick(settings, pairs, wanted, size)
    return _on(settings, host), lane, None


def _step(root: Path, settings: dict, task_id: str, branch: str, kind: str, pk_args: list[str], wanted: str,
          dry_run: bool, host_var: str | None = None, size: str | None = None) -> int:
    """Push main and the task branch, run one pk step for the task on a lane under its lock, and copy back what the
    step wrote (for plants, then adopt it)."""
    wt = None if dry_run else _task_checks(root, task_id, branch, kind)
    problem = _local_paths_problem(pk_args, root)
    if problem:
        raise KitError(f"pk remote {kind}: {problem}")
    refspecs = ["refs/heads/main:refs/heads/main", f"refs/heads/{branch}:refs/heads/{branch}"]
    main_sha, head = _sha(root, "refs/heads/main"), _sha(root, f"refs/heads/{branch}")
    owner = f"remote-{kind}-{task_id}"
    settings, lane, plan = _target(settings, kind, wanted, host_var, size, dry_run)
    name = task_id.lower()
    command = [*_prefix(settings, lane), "python3", "kit/scripts/pk.py", "gate",
               "handback" if kind == "gate" else "plants", task_id, "--branch", branch, *pk_args]
    body = [*_clean_lines(settings, name), *_mark_lines(settings, name), *_prepare_task(task_id, branch),
            *_setup_lines(settings, f".worktrees/{name}"), "exec " + shlex.join(command)]
    script = _lane_script(settings, lane, owner, main_sha, body, head[:12])
    if dry_run:
        _dry(settings, f"{kind} {task_id}", plan, refspecs, command, script)
        return 0
    _push(root, settings, refspecs)
    _say(f"{task_id} {kind} on {_label(settings)}:{lane} at {head[:12]}: {shlex.join(command)}")
    proc = _remote(settings, script)
    lane_dir = f"{settings['base']}/{lane}"
    lane_wt = f"{lane_dir}/.worktrees/{name}"
    folder = f"{store.BASE}/{task_id}"
    if kind == "gate":
        listing = (f"git ls-files -z -m -o --exclude-standard -- {_q(folder + '/results')} {_q(folder + '/plants')} "
                   f"{_q(store.pointer_path(task_id))}")
        written = _bring_records(wt, task_id, _fetch_files(settings, lane_wt, listing, (lane_dir, name)),
                                 ("results", "plants"), True)
    else:
        listing = f"git ls-files -z -o --exclude-standard -- {_q(folder + '/' + store.PLANTS)}"
        written = _bring_records(wt, task_id, _fetch_files(settings, lane_wt, listing, (lane_dir, name)),
                                 (store.PLANTS,), False)
    for path in written:
        _say(f"copied back {path}")
    if kind == "plants" and written:
        with lock.held(f"remote-adopt-{task_id}", name=ADOPT_LOCK, cwd=root):
            plants_module.adopt(root, task_id, branch)
    _say(f"exit {proc.returncode}; commit the copied evidence on {branch} as after a local run")
    return proc.returncode


def _age(seconds: int) -> str:
    if seconds < 0:
        return "?"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


def _started(text: str) -> int | None:
    import calendar
    try:
        return calendar.timegm(time.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def _host_status(settings: dict) -> list[str]:
    """The status lines of one host, from one ssh call: disk use, then each lane's size, checkout, lock and last log
    lines; one `unreachable` line when the host does not answer."""
    name = _lock_name(settings)
    lanes = settings["lanes"]
    script = "\n".join([
        _free_function(settings), f"base={_q(settings['base'])}", "echo \"now $(date -u +%s)\"",
        'echo "disk $(df -P "$base" 2>/dev/null | awk \'NR==2 {print $5}\')"',
        'echo "up $(uptime 2>/dev/null)"',
        "for l in " + " ".join(_q(lane) for lane in lanes) + "; do",
        '  d="$base/$l"; echo "lane $l"; echo "head $(git -C "$d" log --oneline -1 2>/dev/null || echo no checkout)"',
        '  s=$(pk_free "$d"); echo "lock $s"',
        '  if [ "$s" != free ] && [ "$s" != missing ]; then',
        f'    sed "s/^/owner /" "$(git -C "$d" rev-parse --path-format=absolute --git-common-dir)/{name}.lock/owner" '
        "2>/dev/null || true",
        "  fi",
        '  tail -3 "$base/$l.log" 2>/dev/null | cut -c1-160 | sed "s/^/log /"',
        "done", "echo pk-status-end"])
    proc = _remote(settings, script, capture=True)
    raw = proc.stdout.decode("utf-8", "replace").splitlines()
    if proc.returncode != 0 or "pk-status-end" not in raw:
        return [f"host {settings['host_var']}: unreachable"]
    now, disk, up, found = None, "", "", []
    for line in raw:
        word, _, rest = line.partition(" ")
        if word == "now" and rest.strip().isdigit():
            now = int(rest)
        elif word == "disk":
            disk = rest.strip()
        elif word == "up":
            up = rest.strip()
        elif word == "lane" and rest in lanes:
            found.append({"lane": rest, "head": "", "state": "missing", "owner": [], "log": []})
        elif found and word in ("head", "lock"):
            found[-1]["head" if word == "head" else "state"] = rest
        elif found and word in ("owner", "log"):
            found[-1][word].append(rest)
    out = [f"host {settings['host_var']}: disk {disk or '?'} used under {settings['base']}"]
    warn, percent = settings["disk_warn_percent"], disk.rstrip("%")
    if warn is not None and percent.isdigit() and int(percent) >= warn:
        out.append(f"warning: the disk of {settings['host_var']} is {disk} used (remote.disk_warn_percent {warn})")
    if up:
        out.append(up)
    for lane in found:
        fields = dict(item.partition("=")[::2] for item in lane["owner"])
        state = lane["state"]
        if state == "busy" and fields.get("owner"):
            began = _started(fields.get("started", ""))
            state += f" (held by {fields['owner']}" + (f" for {_age(now - began)}" if now and began else "") + ")"
        out += [f"== {lane['lane']}: {lane['head']}", f"size: {settings['lane_sizes'][lane['lane']]}",
                f"lock: {state}", *(f"  {item}" for item in lane["owner"]), *(f"  {item}" for item in lane["log"])]
    return [gate._show(line) for line in out]


def cmd_status(args) -> int:
    root = find_root()
    settings = _settings(root, quiet=True)
    for var in settings["host_vars"]:
        host = next((item for item in settings["hosts"] if item["var"] == var), None)
        for line in [f"host {var}: not set"] if host is None else _host_status(_on(settings, host)):
            print(line, flush=True)
    return 0


def cmd_gate(args) -> int:
    root = find_root()
    settings = _settings(root)
    found, extra = _take(list(args.rest), {"--lane": True, "--host": True, "--size": True, "--dry-run": False})
    task_id = args.task_id
    gate._task_branch(task_id, f"task/{task_id}")
    return _step(root, settings, task_id, f"task/{task_id}", "gate", extra, found.get("--lane", args.lane),
                 bool(found.get("--dry-run") or args.dry_run), found.get("--host", args.host),
                 found.get("--size", args.size))


def cmd_plants(args) -> int:
    root = find_root()
    settings = _settings(root)
    task_id, branch = args.task_id, f"task/{args.task_id}"
    gate._task_branch(task_id, branch)
    names = [name.strip() for part in args.rows or [] for name in part.split(",") if name.strip()]
    if bool(names) == bool(args.all_pending):
        raise KitError("pass --rows ID[,ID...] or --all-pending")
    plants_module._check_options(names, args.all_pending, args.chunk_seconds, args.per_row_seconds, args.plan,
                                 args.chunk)
    pk_args = ["--rows", ",".join(names)] if names else ["--all-pending"]
    if args.chunk_seconds is not None:  # the plan is cut here and pinned, so every lane cuts the same one
        planned = plants_module.chunk_plan(root, task_id, branch, args.chunk_seconds, args.per_row_seconds,
                                           settings["workers"])
        seconds, count, chunks = plants_module._seconds(planned["per_row"]), planned["workers"], planned["chunks"]
        pk_args += ["--chunk-seconds", str(args.chunk_seconds), "--per-row-seconds", seconds, "--workers", str(count)]
        if args.plan:
            print(f"remote: plan over all {sum(len(c) for c in chunks)} deferred row(s), {seconds} s per row slot "
                  f"({planned['source']}), at most {args.chunk_seconds} s a chunk with {count} worker(s): "
                  f"{len(chunks)} chunk(s); start them at once, each takes a free lane")
            for number, rows in enumerate(chunks, 1):
                left = sum(1 for row in rows if row["id"] not in planned["killed"])
                print(f"remote: chunk {number}/{len(chunks)}: {len(rows)} row(s), about "
                      f"{plants_module.estimate(rows, count, planned['per_row']):.0f} s, {left} not killed yet: "
                      + ", ".join(gate._show(row["id"]) for row in rows))
                print(f"  pk remote plants {task_id} --all-pending --chunk-seconds {args.chunk_seconds} "
                      f"--per-row-seconds {seconds} --chunk {number}/{len(chunks)}")
            return 0
        if args.chunk is not None:
            pk_args += ["--chunk", args.chunk]
    if args.rejudge:
        pk_args.append("--rejudge")
    return _step(root, settings, task_id, branch, "plants", pk_args, args.lane, args.dry_run, args.host, args.size)


def _run_name(wt: Path) -> str:
    return "run-" + (re.sub(r"[^A-Za-z0-9._-]", "-", wt.name) or "worktree")


def _changed_files(wt: Path) -> tuple[list[str], list[str]]:
    """(changed and untracked files to ship, deleted files) of the worktree against its HEAD, staged or not."""
    def listed(*argv):
        return [item for item in gate._git_fields(wt, *argv) if item]

    changed = listed("diff", "-z", "--name-only", "--no-renames", "--diff-filter=d", "HEAD")
    untracked = listed("ls-files", "-z", "-o", "--exclude-standard")
    deleted = listed("diff", "-z", "--name-only", "--no-renames", "--diff-filter=D", "HEAD")
    shipped = sorted(path for path in set(changed) | set(untracked) if os.path.lexists(wt / path))
    return shipped, sorted(deleted)


def _tar(wt: Path, paths: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            archive.add(str(wt / path), arcname=path, recursive=False)
    return buffer.getvalue()


def _gets(values: list[str]) -> list[str]:
    out = []
    for value in values:
        pure = PurePosixPath(value)
        if not value or value.startswith("/") or ".." in pure.parts or "\\" in value:
            raise KitError(f"--get {value!r} must be a path relative to the worktree, without ..")
        out.append(pure.as_posix())
    return out


def _under(rel: str, gets: list[str]) -> bool:
    return ".." not in rel.split("/") and any(rel == g or rel.startswith(g.rstrip("/") + "/") for g in gets)


def cmd_run(args) -> int:
    root = find_root()
    settings = _settings(root)
    owner = args.owner
    if not _OWNER.match(owner or ""):
        raise KitError(f"owner {owner!r}: letters, digits, '.', '_' and '-' only")
    _, command = _take(list(args.rest), {})
    if not command:
        raise KitError("pk remote run: no command given (pk remote run <owner> -- <command> [args...])")
    here = Path.cwd()
    wt = Path(git(here, "rev-parse", "--show-toplevel")).resolve()
    sub = git(here, "rev-parse", "--show-prefix").strip().rstrip("/")
    gets = _gets(args.get or [])
    problem = _local_paths_problem([*command, *gets], wt)
    if problem:
        raise KitError(f"pk remote run: {problem}")
    name = _run_name(wt)
    head, main_sha = _sha(wt, "HEAD"), _sha(wt, "refs/heads/main")
    refspecs = [f"{head}:refs/runs/{name}", "refs/heads/main:refs/heads/main"]
    shipped, deleted = _changed_files(wt)
    settings, lane, plan = _target(settings, "run", args.lane, args.host, args.size, args.dry_run)
    full = [*_prefix(settings, lane), *command]
    r = _q(f".worktrees/{name}")
    body = [
        *_clean_lines(settings, name),
        *(_mark_lines(settings, name) if gets else []),
        f"git fetch -q origin +refs/heads/main:refs/remotes/origin/main {_q(f'+refs/runs/{name}:refs/runs/{name}')}",
        "git checkout -q -f -B main origin/main",
        f"if [ -e {r}/.git ]; then",
        f"  git -C {r} reset -q --hard && git -C {r} clean -fdq",
        f"  git -C {r} checkout -q -f --detach {head} && git -C {r} reset -q --hard {head} && git -C {r} clean -fdq",
        "else",
        f"  rm -rf {r}; git worktree prune; git worktree add -q -f --detach {r} {head}",
        "fi",
        f"cd {r}",
        "tar -xf -",
        *([f"rm -f -- {' '.join(_q(path) for path in deleted)}"] if deleted else []),
        *_setup_lines(settings, "."),
        f"cd ./{_q(sub)}" if sub else ":",
        "exec " + shlex.join(full),
    ]
    script = _lane_script(settings, lane, f"run-{owner}", main_sha, body, head[:12])
    if args.dry_run:
        _dry(settings, f"run {owner}", plan, refspecs, full, script)
        print(f"remote: ships {len(shipped)} changed or untracked file(s)"
              + (": " + ", ".join(shipped[:20]) if shipped else "")
              + (f"; deletes {len(deleted)}: " + ", ".join(deleted[:20]) if deleted else ""))
        return 0
    data = _tar(wt, shipped)
    _push(wt, settings, refspecs)
    _say(f"{owner} on {_label(settings)}:{lane} at {head[:12]}: {shlex.join(command)}")
    proc = _remote(settings, script, stdin=data)
    if gets:
        where = f"{settings['base']}/{lane}/.worktrees/{name}"
        listing = ("for p in " + " ".join(_q(g) for g in gets)
                   + "; do if [ -e \"$p\" ]; then find \"$p\" -type f -print0; fi; done")
        found = _fetch_files(settings, where, listing, (f"{settings['base']}/{lane}", name))
        strays = [rel for rel, _ in found if not _under(rel, gets)]
        if strays:
            raise KitError(f"pk remote run: {gate._show(strays[0])} came back outside the --get paths; nothing copied")
        for rel, raw in found:
            _write_new(wt / rel, raw, None)
            _say(f"copied back {rel}")
        missing = [g for g in gets if not any(_under(rel, [g]) for rel, _ in found)]
        if missing:
            _say("not there on the lane: " + ", ".join(missing))
    _say(f"exit {proc.returncode}")
    return proc.returncode


def configure(parser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="action", required=True, parser_class=type(parser))
    actions.add_parser("status", help="each host's disk use and each lane's size, checkout, lock and last log lines")
    lane_help = ("a lane of remote.lanes (on any host, unless --host), or auto (default): the first free one of the "
                 "wanted size, in host order then lane order, waiting while all are busy")
    host_help = "only the host whose ssh destination this variable of remote.hosts_env (or host_env) holds"
    size_help = "the lane size for auto (default: remote.sizes for this kind of step, else any), or any"
    dry_help = "print the push, the lane plan and the ssh command; run nothing"

    def where(parser) -> None:
        parser.add_argument("--lane", default="auto", help=lane_help)
        parser.add_argument("--host", metavar="VAR", help=host_help)
        parser.add_argument("--size", metavar="SIZE", help=size_help)

    step = actions.add_parser("gate", help="pk gate handback for the task on a lane; copies its evidence back")
    where(step)
    step.add_argument("--dry-run", action="store_true", help=dry_help)
    step.add_argument("task_id", metavar="ID")
    step.add_argument("rest", nargs=argparse.REMAINDER, metavar="-- args", help="passed to pk gate handback")
    chunk = actions.add_parser("plants", help="pk gate plants for the task on a lane; copies the new plants records "
                                              "back and adopts them")
    chunk.add_argument("task_id", metavar="ID")
    chunk.add_argument("--rows", action="append", metavar="ID[,ID...]")
    chunk.add_argument("--all-pending", action="store_true")
    chunk.add_argument("--chunk-seconds", type=int, metavar="N")
    chunk.add_argument("--per-row-seconds", type=float, metavar="S",
                       help="default: measured here, as pk gate plants does, and passed to the lane")
    chunk.add_argument("--chunk", metavar="K/M")
    chunk.add_argument("--plan", action="store_true", help="print the chunks and their pk remote commands; run nothing")
    chunk.add_argument("--rejudge", action="store_true")
    where(chunk)
    chunk.add_argument("--dry-run", action="store_true", help=dry_help)
    run = actions.add_parser("run", help="one heavy command for the current worktree (its HEAD and uncommitted "
                                         "changes) on a lane, under the lane's lock")
    where(run)
    run.add_argument("--get", action="append", metavar="PATH",
                     help="a path relative to the worktree to copy back after the run, whatever its exit (repeat)")
    run.add_argument("--dry-run", action="store_true", help=dry_help)
    run.add_argument("owner", help="who runs it (letters, digits, . _ -)")
    run.add_argument("rest", nargs=argparse.REMAINDER, metavar="-- command ...")


def cmd_remote(args) -> int:
    return {"status": cmd_status, "gate": cmd_gate, "plants": cmd_plants, "run": cmd_run}[args.action](args)


def register(add_command) -> None:
    add_command("remote", cmd_remote, help="run heavy steps on other machines over ssh, git and tar (opt-in: the "
                                           "remote section of program/checks.yaml)", configure=configure)
