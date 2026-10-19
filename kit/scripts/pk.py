#!/usr/bin/env python3
"""pk: the single command-line entry for program-kit (standard library only).

Subcommands live in a registry. A command module exposes `register(add_command)`
and calls `add_command(name, handler, help=..., configure=...)` once per command;
its module name is then listed in COMMAND_MODULES. Nothing else in this file
changes when a command is added.
"""
from __future__ import annotations

import argparse
import importlib
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Callable

sys.dont_write_bytecode = True  # before any kit import: running pk must never leave bytecode in the project

from common import KitError, ensure_gitignore, find_root, git, primary_branch_problem, read_yaml  # noqa: E402

KIT_DIR = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 11)
PRESETS = ("go", "node", "python")
TEMPLATE_FILES = ("BRIEF.md", "checks.yaml", "environments.yaml", "CLAIM_BOUNDARIES.md", "ledger.yaml",
                  "HANDOFF.md", "controller-state.json", "rulings.md", "register.yaml", "traceability.yaml",
                  "gates.yaml", "COMMON.md", "release-set.yaml")

# Modules that register further subcommands (each defines `register(add_command)`).
COMMAND_MODULES: tuple[str, ...] = ("status", "rulings", "register", "briefs", "trace", "interview", "evidence",
                                    "stale", "fetch", "gate", "worktree", "handoff", "change", "manifest",
                                    "cost", "tracker", "land", "fingerprint", "dashboard")

COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {}
CONFIGURERS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None] | None]] = {}


def register(name: str, handler: Callable[[argparse.Namespace], int], help: str = "",
             configure: Callable[[argparse.ArgumentParser], None] | None = None) -> None:
    """Add subcommand `name`; `configure(subparser)` adds its arguments."""
    if name in COMMANDS:
        raise ValueError(f"command already registered: {name}")
    COMMANDS[name] = handler
    CONFIGURERS[name] = (help, configure)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise KitError(message)


def build_parser() -> argparse.ArgumentParser:
    def add_once(name, handler, **kwargs):  # build_parser runs on every main(): re-adding the same command is a no-op
        if COMMANDS.get(name) is not handler:
            register(name, handler, **kwargs)

    for module_name in COMMAND_MODULES:
        importlib.import_module(module_name).register(add_once)
    parser = _Parser(prog="pk", description="program-kit command line")
    sub = parser.add_subparsers(dest="command", metavar="command", parser_class=_Parser)
    for name in COMMANDS:
        help_text, configure = CONFIGURERS[name]
        subparser = sub.add_parser(name, help=help_text, description=help_text)
        if configure is not None:
            configure(subparser)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command is None:
            raise KitError("no command given (try: pk init)")
        return COMMANDS[args.command](args)
    except KitError as exc:
        print("error: " + " ".join(str(exc).split()), file=sys.stderr)
        return 1


def _init_root() -> Path:
    """Where `pk init` creates program/: the current directory, which must be the root of a git repository.

    Outside a repository, and in a subdirectory of one, init refuses: it never creates program/ in an
    ancestor the user did not name, and never in a directory no gate could work in.
    """
    here = Path.cwd().resolve()
    if shutil.which("git") is None:
        raise KitError("git was not found on PATH")
    try:
        top = Path(git(here, "rev-parse", "--show-toplevel")).resolve()
    except KitError:
        raise KitError(f"{here} is not inside a git repository; run `git init -b main` there, then pk init") from None
    if top != here:
        raise KitError(f"{here} is not the root of its repository; run pk init in {top}")
    return here


def _staging_dir(parent: Path) -> Path:
    """A new directory beside the target, with the mode any new directory gets (the umask is never read or set)."""
    while True:
        path = parent / f".program.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            os.mkdir(path, 0o777)
            return path
        except FileExistsError:
            continue


def cmd_init(args: argparse.Namespace) -> int:
    target = _init_root() / "program"
    if target.exists():
        raise KitError(f"{target} already exists; refusing to overwrite")
    sources = {name: KIT_DIR / "templates" / name for name in TEMPLATE_FILES}
    if args.preset:
        sources["checks.yaml"] = KIT_DIR / "presets" / f"checks.{args.preset}.yaml"
    contents = {}
    for name, source in sources.items():  # read everything first: any failure writes nothing
        try:
            contents[name] = source.read_bytes()
        except OSError as exc:
            raise KitError(f"cannot read {source}: {exc.strerror or exc}") from exc
    staging = None
    try:
        staging = _staging_dir(target.parent)
        for name, data in contents.items():
            (staging / name).write_bytes(data)
        staging.rename(target)
    except OSError as exc:
        raise KitError(f"cannot create {target}: {exc.strerror or exc}") from exc
    finally:
        if staging is not None and staging.exists():  # still there only if the rename did not happen
            shutil.rmtree(staging, ignore_errors=True)
    ensure_gitignore(target.parent)  # bytecode and worktrees must not make the first gate see a dirty tree
    print(f"created {target}")
    return 0


def _checks_problems(data) -> list[str]:
    """The problems the gate's own loader finds in a parsed checks.yaml, one line each.

    The gate stops at its first problem, so each check key, guarded_files and the unknown keys are
    handed to its loader separately; the rules are the gate's, not a copy of them.
    """
    import gate  # lazy: gate imports most of the kit

    required = ("build", "test", "scan")
    if data is not None and not isinstance(data, dict):
        parts = [(data, ())]
    else:
        data = data or {}
        parts = [({key: data.get(key)}, (key,) if key in required else ()) for key in gate.CHECK_KEYS]
        parts.append(({"guarded_files": data.get("guarded_files")}, ()))
        parts.append(({"tiers": data.get("tiers")}, ()))
        parts.append(({key: value for key, value in data.items() if key not in gate.ALL_KEYS}, ()))
    problems = []
    for part, require in parts:
        try:
            gate._parse_checks(part, require)
        except KitError as exc:
            problems.append(" ".join(str(exc).split()))
    return problems


def _tier_path_warnings(root: Path, data) -> list[str]:
    """One line per `tiers.full_paths` or `tiers.light_paths` entry that covers no tracked file.

    Such an entry guards nothing today: it is a typing mistake, or a directory nobody has added a file to yet.
    A warning, not an error, because the second case is how a full-tier path is listed ahead of its first file.
    """
    import gate  # lazy: gate imports most of the kit
    from common import run_argv

    try:
        tiers = gate._parse_tiers(data.get("tiers") if isinstance(data, dict) else None)
    except KitError:
        return []  # reported as a problem by _checks_problems
    warnings = []
    for key in ("full_paths", "light_paths"):
        for entry in tiers[key]:
            code, listed = run_argv(["git", "-C", str(root), "ls-files", "--", entry.rstrip("/")], root)
            if code == 0 and not listed.strip():
                warnings.append(f"checks.yaml: tiers.{key} entry {entry!r} covers no tracked file: check the "
                                "spelling (a directory is written with or without its trailing /)")
    return warnings


def _work_started(root: Path) -> bool:
    """Whether any task has a status record past `not_started`. Before that, no path is expected to hold a file yet.

    A status record that does not read counts as started, so a broken file never hides a warning.
    """
    import status as status_module  # lazy: status imports the register

    try:
        return any(record["status"] != "not_started" for record in status_module.list_all(root))
    except KitError:
        return True


def cmd_doctor(args: argparse.Namespace) -> int:
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        found = ".".join(str(part) for part in sys.version_info[:3])
        raise KitError(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (found {found})")
    if shutil.which("git") is None:
        raise KitError("git was not found on PATH")
    root = find_root()
    program = root / "program"
    if not program.is_dir():
        raise KitError(f"program/ not found at {root}; run `pk init`")
    checks = read_yaml(program / "checks.yaml")  # a file that does not read raises, naming the line
    problems = _checks_problems(checks)
    warnings = _tier_path_warnings(root, checks)
    if warnings and not _work_started(root):  # a tiers path is listed ahead of its first file: expected before any work
        print(f"note: {len(warnings)} tiers path warning(s) held back: no task is past not_started yet, so a tiers "
              "path may cover no file; pk doctor reports them once a task has started", file=sys.stderr)
        warnings = []
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    branch = primary_branch_problem(root)
    if branch:
        problems.append(branch)
    import store  # lazy, like gate: the evidence checks of `pk evidence validate` over the whole history

    found, pending = store.check(root)
    problems.extend(" ".join(problem.split()) for problem in found)
    for path, _ in pending:  # present with its digest, not committed yet: the evidence commit is still to come
        print(f"warning: record pending commit: {path}", file=sys.stderr)
    for problem in problems:
        print("error: " + problem, file=sys.stderr)
    if problems:
        return 1
    print("doctor: ok")
    return 0


register("init", cmd_init, help="create program/ from the templates",
         configure=lambda p: p.add_argument("--preset", choices=PRESETS,
                                            help="start checks.yaml from a language preset"))
register("doctor", cmd_doctor, help="check the environment and program/ are usable")


if __name__ == "__main__":
    sys.exit(main())
