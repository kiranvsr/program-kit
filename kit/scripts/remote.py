#!/usr/bin/env python3
"""`pk remote`: run heavy steps on other machines over plain ssh, git and tar (0.6.0), opt-in by checks.yaml.

    pk remote status
    pk remote gate <ID> [--lane L|auto] [--dry-run] [-- extra pk gate handback args]
    pk remote plants <ID> (--rows ID,... | --all-pending) [--chunk-seconds N [--per-row-seconds S]
                     [--chunk K/M | --plan]] [--rejudge] [--lane L|auto] [--dry-run]
    pk remote run [--lane L|auto] [--get PATH]... [--dry-run] <owner> -- <command> [args...]

Provider-neutral and host-neutral: the kit names no host, user, path or provider. The `remote` section of main's
program/checks.yaml says where (`parse`); the ssh destination is read at run time from the environment variable
it names (`host_env`, default PK_REMOTE_HOST), never from the file. On the remote, under `dir`:

- `<dir>/repo.git`, a bare mirror. The runner pushes main and the task branch (for `run`, the worktree's HEAD as
  `refs/runs/<name>`, and main) to that mirror ONLY, by its ssh path, with a forced update: the mirror is the
  lanes' alone. It never pushes to origin or to any other remote, and the remote machine needs no credential of the
  project's host.
- `<dir>/<lane>` for each lane, a clone of the mirror; each is its own checkout, so each has its own `pk lock`. On
  the lane, under that lane's lock (taken with the kit of the pushed main, extracted with `git archive`, so a lane's
  own checkout may be older), the step resets the lane's main to the mirror's, checks the task branch out in
  `.worktrees/<id lowercased>` (for `run`, a detached `.worktrees/run-<name>` with this worktree's uncommitted
  changes, deletions included, laid over it), runs each `setup` argv in that worktree (PK_LANE_ROOT names the lane
  root), then the step from the lane root (for `run`, from the same relative directory): `lane_wrap[lane]` + `wrap`
  + the command, with `env` exported and PK_PLANT_WORKERS set to `workers`. Output streams back and to
  `<dir>/<lane>.log`, timestamped, between a start and an end line. The exit status is the step's.
- `--lane auto` (the default) takes the first lane whose lock is free (or stale) and waits while every lane is busy.
- After a gate step, `latest.json` and the new `results/` and `plants/` records of the task come back into the local
  task worktree; after a plants step only the new plants record files (never `latest.json`), which
  `pk gate plants --adopt` then lists in the local pointer. Only regular files under record names come back (for
  `run`, the `--get` paths). The controller commits them on the task branch as after a local run.
- Every value that reaches a shell is quoted (shlex); IDs, lanes and owners are checked; an argument naming this
  machine's home or worktree is refused (the command runs elsewhere). `--dry-run` prints the push, the lane plan and
  the ssh command, and runs nothing.

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

KEYS = ("host_env", "dir", "dir_env", "lanes", "setup", "env", "wrap", "lane_wrap", "workers")
DEFAULT_HOST_ENV = "PK_REMOTE_HOST"
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


def parse(value) -> dict | None:
    """The `remote` section of checks.yaml, checked, or None when absent (pk remote refuses then).

    {host_env: str, dir: str | None, dir_env: str | None, lanes: [str], setup: [[str]], env: {str: str},
    wrap: [str], lane_wrap: {lane: [str]}, workers: int | None}. Exactly one of `dir` and `dir_env` (the variable
    that holds it) names the base directory on the remote; a relative one is relative to the remote user's home."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KitError("checks.yaml: remote must be a mapping of " + ", ".join(KEYS))
    unknown = sorted(str(key) for key in value if key not in KEYS)
    if unknown:
        raise KitError(f"checks.yaml: remote: unknown key {', '.join(unknown)} (expected {', '.join(KEYS)})")
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
    lanes = value.get("lanes")
    if not isinstance(lanes, list) or not lanes or not all(isinstance(lane, str) and _LANE.match(lane)
                                                            and lane not in ("auto", "repo.git") for lane in lanes):
        raise KitError("checks.yaml: remote.lanes must list at least one lane name (letters, digits, '.', '_', '-'; "
                       "not auto), each a clone of <dir>/repo.git at <dir>/<lane>")
    if len(set(lanes)) != len(lanes):
        raise KitError("checks.yaml: remote.lanes names a lane twice")
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
    return {"host_env": host_env, "dir": base, "dir_env": base_env, "lanes": list(lanes),
            "setup": [_argv(item, "remote.setup: each command") for item in setup], "env": dict(env),
            "wrap": _argv(value.get("wrap") or [], "remote.wrap", empty=True),
            "lane_wrap": {lane: _argv(argv, f"remote.lane_wrap.{lane}", empty=True)
                          for lane, argv in lane_wrap.items()},
            "workers": workers}


# --- configuration at run time ------------------------------------------------------------------------------

def _settings(root: Path) -> dict:
    """The `remote` section of main's committed checks.yaml, with the host (and a `dir_env` directory) read from the
    environment; KitError naming the fix when the section, the host or the directory is missing."""
    config = gate.load_checks_at(root, gate.MAIN)["remote"]
    if config is None:
        raise KitError("program/checks.yaml on main has no remote section: pk remote is opt-in; see the remote "
                       "example in kit/templates/checks.yaml")
    host = os.environ.get(config["host_env"], "").strip()
    if not host:
        raise KitError(f"{config['host_env']} is not set: export it with the ssh destination of the lanes' machine "
                       "(an ssh alias or user@host; the kit never stores it)")
    if not _HOST.match(host):
        raise KitError(f"{config['host_env']}: {host!r} is not an ssh destination this runner uses (letters, digits, "
                       "@ . _ -; anything else goes in an alias of your ssh config)")
    if config["dir"] is not None:
        base = _check_dir(config["dir"], "checks.yaml: remote.dir")
    else:
        raw = os.environ.get(config["dir_env"], "").strip()
        if not raw:
            raise KitError(f"{config['dir_env']} is not set: export it with the base directory on the remote "
                           "(remote.dir_env in program/checks.yaml)")
        base = _check_dir(raw, config["dir_env"])
    return {**config, "host": host, "base": base}


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


def _setup_lines(settings: dict, where: str) -> list[str]:
    return [f"(cd {_q(where)} && PK_LANE_ROOT=\"$lane_root\" {shlex.join(argv)})" for argv in settings["setup"]]


def _prefix(settings: dict, lane_name: str) -> list[str]:
    return [*settings["lane_wrap"].get(lane_name, []), *settings["wrap"]]


def _lane_script(settings: dict, lane_name: str, owner: str, main_sha: str, body: list[str], short: str) -> str:
    """The script the ssh command runs: take the lane's lock with the pushed main's kit and run `body` under it from
    the lane root, its output timestamped to the caller and the lane's log; exit with the body's status."""
    inner = "\n".join(["set -e", "lane_root=$(pwd)", *body])
    return "\n".join([
        *_exports(settings),
        f"base={_q(settings['base'])}",
        f"log=\"$base\"/{_q(lane_name + '.log')}",
        "tmp=$(mktemp -d) || exit 1",
        "trap 'rm -rf \"$tmp\"' EXIT",
        f"git --git-dir=\"$base/repo.git\" archive {_q(main_sha)} kit/scripts | tar -x -C \"$tmp\" || "
        "{ echo 'remote: the pushed main has no kit/scripts to take the lane lock with' >&2; exit 1; }",
        f"cd \"$base\"/{_q(lane_name)} || exit 1",
        f"echo \"$(date -u +%Y-%m-%dT%H:%M:%SZ) start {owner} at {short}\" >> \"$log\"",
        "{ python3 \"$tmp/kit/scripts/pk.py\" lock run " + _q(owner) + " -- sh -c " + _q(inner) + " 2>&1; "
        "echo $? > \"$tmp/rc\"; } | while IFS= read -r line; do printf '%s %s\\n' \"$(date -u +%H:%M:%S)\" "
        "\"$line\"; done | tee -a \"$log\"",
        "rc=$(cat \"$tmp/rc\" 2>/dev/null || echo 1)",
        f"echo \"$(date -u +%Y-%m-%dT%H:%M:%SZ) end {owner} exit $rc\" >> \"$log\"",
        "exit \"$rc\"",
    ])


def _ssh_argv(settings: dict, script: str) -> list[str]:
    return [*_ssh(), settings["host"], "sh -c " + _q(script)]


def _push_argv(settings: dict, refspecs: list[str]) -> list[str]:
    return ["git", "push", "-q", "-f", f"{settings['host']}:{settings['base']}/repo.git", *refspecs]


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
                           + " ".join((proc.stderr or proc.stdout).split())[:600])
        time.sleep(1)


def _remote(settings: dict, script: str, stdin: bytes | None = None, capture: bool = False):
    argv = _ssh_argv(settings, script)
    if capture:
        return subprocess.run(argv, input=stdin, capture_output=True)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL)
    proc.communicate(stdin)
    return proc


def _pick_lane(settings: dict, wanted: str, dry_run: bool) -> str:
    """`wanted` when it is a lane; for auto the first lane whose lock is free or stale, waiting while all are busy
    (a dry run says auto)."""
    if wanted != "auto":
        if wanted not in settings["lanes"]:
            raise KitError(f"no lane {gate._show(wanted)} in remote.lanes ({', '.join(settings['lanes'])}, or auto)")
        return wanted
    if dry_run:
        return "auto"
    script = "\n".join([_free_function(settings), f"base={_q(settings['base'])}",
                        "for l in " + " ".join(_q(lane) for lane in settings["lanes"]) + "; do",
                        '  s=$(pk_free "$base/$l"); if [ "$s" = free ] || [ "$s" = stale ]; then echo "$l"; exit 0; fi',
                        "done", "echo none"])
    announced = False
    while True:
        proc = _remote(settings, script, capture=True)
        found = proc.stdout.decode("utf-8", "replace").split()[-1:] if proc.returncode == 0 else []
        if found and found[0] in settings["lanes"]:
            return found[0]
        if not announced:  # an ssh hiccup while polling is retried, not fatal
            _say("every lane is busy; waiting for one" if found else "cannot reach the lanes yet; trying again")
            announced = True
        time.sleep(_poll())


# --- copying back -------------------------------------------------------------------------------------------

def _fetch_files(settings: dict, where: str, listing: str) -> list[tuple[str, bytes]]:
    """(relative path, bytes) of the files the remote `listing` command names (NUL-separated, relative to the remote
    directory `where`), read through one tar stream; [] when `where` is not there."""
    script = "\n".join([f"cd {_q(where)} || exit 3", "l=$(mktemp) || exit 1", f"{listing} > \"$l\" || exit 1",
                        "COPYFILE_DISABLE=1 tar -cf - --null -T \"$l\"", "rc=$?", "rm -f \"$l\"", "exit $rc"])
    proc = _remote(settings, script, capture=True)
    if proc.returncode == 3:
        return []
    if proc.returncode != 0:
        raise KitError("pk remote: cannot read the step's files back: "
                       + " ".join(proc.stderr.decode("utf-8", "replace").split())[:400])
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


def _dry(settings: dict, what: str, lane: str, refspecs: list[str], command: list[str], script: str) -> None:
    lanes = ", ".join(settings["lanes"])
    print(f"remote: {gate._show(what)} on {settings['host']}, lane "
          + (f"auto (the first free of {lanes}; waits while all are busy)" if lane == "auto" else lane))
    print("remote: push (to the lanes' mirror only, never to origin): " + shlex.join(_push_argv(settings, refspecs)))
    print(f"remote: on the lane, under its pk lock {_lock_name(settings)!r}: {shlex.join(command)}")
    print("remote: ssh: " + shlex.join(_ssh_argv(settings, script)))
    print("remote: dry run: nothing pushed, nothing run")


def _step(root: Path, settings: dict, task_id: str, branch: str, kind: str, pk_args: list[str], wanted: str,
          dry_run: bool) -> int:
    """Push main and the task branch, run one pk step for the task on a lane under its lock, and copy back what the
    step wrote (for plants, then adopt it)."""
    wt = None if dry_run else _task_checks(root, task_id, branch, kind)
    problem = _local_paths_problem(pk_args, root)
    if problem:
        raise KitError(f"pk remote {kind}: {problem}")
    refspecs = ["refs/heads/main:refs/heads/main", f"refs/heads/{branch}:refs/heads/{branch}"]
    main_sha, head = _sha(root, "refs/heads/main"), _sha(root, f"refs/heads/{branch}")
    owner = f"remote-{kind}-{task_id}"
    lane = _pick_lane(settings, wanted, dry_run)
    shown = lane if lane != "auto" else settings["lanes"][0]
    command = [*_prefix(settings, shown), "python3", "kit/scripts/pk.py", "gate",
               "handback" if kind == "gate" else "plants", task_id, "--branch", branch, *pk_args]
    body = [*_prepare_task(task_id, branch), *_setup_lines(settings, f".worktrees/{task_id.lower()}"),
            "exec " + shlex.join(command)]
    script = _lane_script(settings, shown, owner, main_sha, body, head[:12])
    if dry_run:
        _dry(settings, f"{kind} {task_id}", lane, refspecs, command, script)
        return 0
    _push(root, settings, refspecs)
    _say(f"{task_id} {kind} on {settings['host']}:{lane} at {head[:12]}: {shlex.join(command)}")
    proc = _remote(settings, script)
    lane_wt = f"{settings['base']}/{lane}/.worktrees/{task_id.lower()}"
    folder = f"{store.BASE}/{task_id}"
    if kind == "gate":
        listing = (f"git ls-files -z -m -o --exclude-standard -- {_q(folder + '/results')} {_q(folder + '/plants')} "
                   f"{_q(store.pointer_path(task_id))}")
        written = _bring_records(wt, task_id, _fetch_files(settings, lane_wt, listing), ("results", "plants"), True)
    else:
        listing = f"git ls-files -z -o --exclude-standard -- {_q(folder + '/' + store.PLANTS)}"
        written = _bring_records(wt, task_id, _fetch_files(settings, lane_wt, listing), (store.PLANTS,), False)
    for path in written:
        _say(f"copied back {path}")
    if kind == "plants" and written:
        with lock.held(f"remote-adopt-{task_id}", name=ADOPT_LOCK, cwd=root):
            plants_module.adopt(root, task_id, branch)
    _say(f"exit {proc.returncode}; commit the copied evidence on {branch} as after a local run")
    return proc.returncode


def cmd_status(args) -> int:
    root = find_root()
    settings = _settings(root)
    name = _lock_name(settings)
    script = "\n".join([
        _free_function(settings), f"base={_q(settings['base'])}", "uptime 2>/dev/null || true",
        "for l in " + " ".join(_q(lane) for lane in settings["lanes"]) + "; do",
        '  d="$base/$l"; echo "== $l: $(git -C "$d" log --oneline -1 2>/dev/null || echo no checkout)"',
        '  s=$(pk_free "$d"); echo "lock: $s"',
        '  if [ "$s" != free ] && [ "$s" != missing ]; then',
        f'    sed "s/^/  /" "$(git -C "$d" rev-parse --path-format=absolute --git-common-dir)/{name}.lock/owner" '
        "2>/dev/null || true",
        "  fi",
        '  tail -3 "$base/$l.log" 2>/dev/null | cut -c1-160 | sed "s/^/  /"',
        "done"])
    return _remote(settings, script).returncode


def cmd_gate(args) -> int:
    root = find_root()
    settings = _settings(root)
    found, extra = _take(list(args.rest), {"--lane": True, "--dry-run": False})
    task_id = args.task_id
    gate._task_branch(task_id, f"task/{task_id}")
    return _step(root, settings, task_id, f"task/{task_id}", "gate", extra, found.get("--lane", args.lane),
                 bool(found.get("--dry-run") or args.dry_run))


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
    return _step(root, settings, task_id, branch, "plants", pk_args, args.lane, args.dry_run)


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
    lane = _pick_lane(settings, args.lane, args.dry_run)
    shown = lane if lane != "auto" else settings["lanes"][0]
    full = [*_prefix(settings, shown), *command]
    r = _q(f".worktrees/{name}")
    body = [
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
    script = _lane_script(settings, shown, f"run-{owner}", main_sha, body, head[:12])
    if args.dry_run:
        _dry(settings, f"run {owner}", lane, refspecs, full, script)
        print(f"remote: ships {len(shipped)} changed or untracked file(s)"
              + (": " + ", ".join(shipped[:20]) if shipped else "")
              + (f"; deletes {len(deleted)}: " + ", ".join(deleted[:20]) if deleted else ""))
        return 0
    data = _tar(wt, shipped)
    _push(wt, settings, refspecs)
    _say(f"{owner} on {settings['host']}:{lane} at {head[:12]}: {shlex.join(command)}")
    proc = _remote(settings, script, stdin=data)
    if gets:
        where = f"{settings['base']}/{lane}/.worktrees/{name}"
        listing = ("for p in " + " ".join(_q(g) for g in gets)
                   + "; do if [ -e \"$p\" ]; then find \"$p\" -type f -print0; fi; done")
        found = _fetch_files(settings, where, listing)
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
    actions.add_parser("status", help="each lane's checkout, lock and last log lines")
    lane_help = "a lane of remote.lanes, or auto (default): the first free one, waiting while all are busy"
    dry_help = "print the push, the lane plan and the ssh command; run nothing"
    step = actions.add_parser("gate", help="pk gate handback for the task on a lane; copies its evidence back")
    step.add_argument("--lane", default="auto", help=lane_help)
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
    chunk.add_argument("--lane", default="auto", help=lane_help)
    chunk.add_argument("--dry-run", action="store_true", help=dry_help)
    run = actions.add_parser("run", help="one heavy command for the current worktree (its HEAD and uncommitted "
                                         "changes) on a lane, under the lane's lock")
    run.add_argument("--lane", default="auto", help=lane_help)
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
