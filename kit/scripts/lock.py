#!/usr/bin/env python3
"""`pk lock`: the machine lock for heavy runs (0.6.0), one holder at a time across every worktree of a clone.

    pk lock run <owner> [--name NAME] -- <command> [args...]
    pk lock status [--name NAME]

The kit's rules name a lock for heavy runs (suites, gates, planted-bug chunks) but shipped no wrapper, so each agent
and provider wrote its own, and the copies drifted (KF-16 in the program it came from). This is that wrapper:

- The lock is a directory in the repository's git common dir, `<common>/<name>.lock`, so every worktree of the clone
  sees the same one whatever tool or provider runs it. The name is `--name`, else PK_LOCK_NAME, else `pk-heavy`.
- `run` takes the lock with an atomic mkdir, writes the owner file `owner=<owner>`, `pid=<pid>`, `started=<UTC>`
  (one per line), runs the command, and releases the lock when the command ends, whatever its exit, and on SIGINT,
  SIGTERM and SIGHUP (passed on to the command first). The exit status is the command's (128 + N for signal N).
  Hold it for that one command only: never wrap model work or waiting.
- While the lock is held, `run` waits, saying once whom for, and tries again every PK_LOCK_POLL seconds (15).
- A lock is stale when its recorded pid is not running, or when it still has no owner file a minute after the
  directory was made (a holder that died before writing it). A stale lock is moved aside atomically, checked again
  where it was moved (a peer may have cleared it and taken a fresh one in between), and removed only if it is still
  stale; a live holder is never removed. Release removes the lock only while its owner file names this process.
- The directory and the owner file have the format of the shell wrapper of the program it came from, so a program's
  own script that uses the same directory interoperates: pass `--name` (or PK_LOCK_NAME) with its lock's name to
  share it while both are in use.

`status` prints `free`, or `held:` and the owner file's lines (and a line when the holder's pid is not running).
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from common import KitError, git, utc_now

DEFAULT_NAME = "pk-heavy"
NAME_ENV = "PK_LOCK_NAME"
POLL_ENV = "PK_LOCK_POLL"
POLL_SECONDS = 15.0
NO_OWNER_GRACE = 60  # seconds a lock directory may have no owner file before it counts as stale
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CONTROL = re.compile("[\x00-\x1f\x7f]")


def lock_name(name: str | None = None) -> str:
    """`name`, else PK_LOCK_NAME, else DEFAULT_NAME, checked: letters, digits, `.`, `_` and `-`."""
    value = name if name is not None else os.environ.get(NAME_ENV, "").strip() or DEFAULT_NAME
    if not _NAME.match(value) or value.endswith(".lock"):
        raise KitError(f"lock name {value!r}: use letters, digits, '.', '_' and '-', without .lock (the directory "
                       "is <git common dir>/<name>.lock)")
    return value


def lock_path(cwd=None, name: str | None = None) -> Path:
    """`<git common dir>/<name>.lock` of the repository at `cwd` (default: the current directory)."""
    here = Path(cwd) if cwd is not None else Path.cwd()
    try:
        common = git(here, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except KitError:
        raise KitError(f"pk lock: {here} is not inside a git repository (the lock lives in its git common dir)") \
            from None
    return Path(common) / f"{lock_name(name)}.lock"


def _owner_file(path: Path) -> str | None:
    try:
        return (path / "owner").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def holder(path: Path) -> str:
    """The owner file's lines, or `(no owner file)`."""
    text = _owner_file(path)
    return text.rstrip("\n") if text is not None else "(no owner file)"


def _pid(path: Path) -> int | None:
    for line in (_owner_file(path) or "").splitlines():
        if line.startswith("pid="):
            value = line[4:].strip()
            return int(value) if value.isdigit() else None
    return None


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:  # another user's process: it runs
        return True
    except OSError:
        return False
    return True


def alive(path: Path) -> bool:
    """Whether the owner file names a pid that is running."""
    pid = _pid(path)
    return pid is not None and pid > 0 and _running(pid)


def stale(path: Path) -> bool:
    """Whether the lock at `path` may be cleared: its pid is not running, or it has had no owner file for a minute."""
    if (path / "owner").is_file():
        return not alive(path)
    try:
        made = path.stat().st_mtime
    except OSError:
        return False  # gone: nothing to clear
    return time.time() - made > NO_OWNER_GRACE


def _poll() -> float:
    raw = os.environ.get(POLL_ENV, "").strip()
    if not raw:
        return POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise KitError(f"{POLL_ENV} must be a number of seconds above 0, not {raw!r}") from None
    if not value > 0:
        raise KitError(f"{POLL_ENV} must be a number of seconds above 0, not {raw!r}")
    return value


def _say(text: str) -> None:
    print(f"pk lock: {text}", file=sys.stderr, flush=True)


def _clear_stale(path: Path) -> None:
    """Move a stale lock aside atomically, check what was moved, and remove it only if it is still stale."""
    aside = path.with_name(f"{path.name}.stale.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        os.rename(path, aside)
    except OSError:
        return  # a peer moved or released it first
    if stale(aside):
        _say("cleared stale lock: " + " ".join(holder(aside).splitlines()))
        shutil.rmtree(aside, ignore_errors=True)
        return
    try:  # a peer took a fresh lock in between, or the holder is alive after all: put it back
        os.rename(aside, path)
    except OSError:
        _say(f"moved a live lock aside to {aside}; restore it by hand")


def acquire(path: Path, owner: str) -> None:
    """Wait for the lock at `path`, then take it for `owner` and this process's pid."""
    if not owner or _CONTROL.search(owner):
        raise KitError("pk lock: the owner must be a non-empty name on one line")
    poll = _poll()
    announced = False
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            pass
        except OSError as exc:
            raise KitError(f"pk lock: cannot create {path}: {exc.strerror or exc}") from exc
        if stale(path):
            _clear_stale(path)
            continue
        if not announced:
            _say("waiting for: " + " ".join(holder(path).splitlines()))
            announced = True
        time.sleep(poll)
    tmp = path / f".owner.{os.getpid()}"
    tmp.write_text(f"owner={owner}\npid={os.getpid()}\nstarted={utc_now()}\n", encoding="utf-8")
    os.replace(tmp, path / "owner")


def release(path: Path) -> None:
    """Remove the lock at `path` if its owner file names this process; anyone else's lock stays."""
    if _pid(path) != os.getpid():
        return
    aside = path.with_name(f"{path.name}.released.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        os.rename(path, aside)
    except OSError:
        return
    shutil.rmtree(aside, ignore_errors=True)


class Signalled(Exception):
    """A signal arrived while waiting for the lock or holding it without a command."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


@contextmanager
def held(owner: str, name: str | None = None, cwd=None):
    """Hold the lock for the body of a `with` block, released on exit (SIGTERM and SIGHUP raise Signalled)."""
    path = lock_path(cwd, name)

    def stop(signum, frame):
        raise Signalled(signum)

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGHUP)}
    try:
        acquire(path, owner)
        yield path
    finally:
        try:
            release(path)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def run(owner: str, argv: list[str], name: str | None = None, cwd=None) -> int:
    """Run `argv` under the lock; its exit status (128 + N when signal N ended it, or this wrapper)."""
    if not argv:
        raise KitError("pk lock run: no command given (pk lock run <owner> -- <command> [args...])")
    path = lock_path(cwd, name)
    state = {"child": None, "signum": None}

    def forward(signum, frame):
        state["signum"] = signum
        if state["child"] is None:
            raise Signalled(signum)
        try:
            state["child"].send_signal(signum)
        except OSError:
            pass

    previous = {signum: signal.signal(signum, forward) for signum in SIGNALS}
    try:
        try:
            acquire(path, owner)
            try:
                state["child"] = subprocess.Popen(argv, cwd=str(cwd) if cwd is not None else None)
            except OSError as exc:
                _say(f"cannot run {argv[0]}: {exc.strerror or exc}")
                return 127 if isinstance(exc, FileNotFoundError) else 126
            code = state["child"].wait()
        except Signalled as exc:
            return 128 + exc.signum
        finally:
            release(path)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if code < 0:
        return 128 - code
    if state["signum"] is not None and code == 0:  # the command caught the signal and ended well: still a stop
        return 128 + state["signum"]
    return code


def status_lines(name: str | None = None, cwd=None) -> list[str]:
    """`free`, or `held:` and the owner file's lines, with a line when the holder's pid is not running."""
    path = lock_path(cwd, name)
    if not path.is_dir():
        return ["free"]
    lines = ["held:", *holder(path).splitlines()]
    if (path / "owner").is_file() and not alive(path):
        lines.append("(holder PID not running: stale, cleared by the next run)")
    return lines


def _split_run(rest: list[str]) -> tuple[str | None, list[str]]:
    """(--name value, command) of what follows the owner: `[--name NAME] [--] command...`.

    Parsed here, not by argparse: Python versions differ in whether a `--` before a REMAINDER reaches it."""
    name = None
    while rest and (rest[0] == "--name" or rest[0].startswith("--name=")):
        if rest[0] == "--name":
            if len(rest) < 2:
                raise KitError("pk lock run: --name needs a value")
            name, rest = rest[1], rest[2:]
        else:
            name, rest = rest[0].split("=", 1)[1], rest[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    return name, rest


def cmd_lock(args) -> int:
    if args.action == "status":
        for line in status_lines(args.name):
            print(line)
        return 0
    name, argv = _split_run(list(args.rest))
    if name is not None and args.name is not None:
        raise KitError("pk lock run: --name given twice")
    return run(args.owner, argv, name=name if name is not None else args.name)


def configure(parser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="action", required=True, parser_class=type(parser))
    runner = actions.add_parser("run", help="run one command under the machine lock, waiting for it")
    runner.add_argument("--name", help=f"the lock's name (default: {NAME_ENV}, else {DEFAULT_NAME})")
    runner.add_argument("owner", help="who holds it, shown to `pk lock status` and to waiters (one line)")
    runner.add_argument("rest", nargs=argparse.REMAINDER, metavar="-- command ...",
                        help="the command and its arguments, after --")
    status = actions.add_parser("status", help="print free, or held: and the owner file")
    status.add_argument("--name", help=f"the lock's name (default: {NAME_ENV}, else {DEFAULT_NAME})")


def register(add_command) -> None:
    add_command("lock", cmd_lock, help="the machine lock for heavy runs: run one command under it, or show it",
                configure=configure)
