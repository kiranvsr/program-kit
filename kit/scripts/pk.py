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
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

from common import KitError, find_root, read_yaml

KIT_DIR = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 11)
PRESETS = ("go", "node", "python")
TEMPLATE_FILES = ("BRIEF.md", "checks.yaml", "environments.yaml", "CLAIM_BOUNDARIES.md", "ledger.yaml",
                  "HANDOFF.md", "controller-state.json", "rulings.md", "register.yaml", "traceability.yaml",
                  "gates.yaml")

# Modules that register further subcommands (each defines `register(add_command)`).
COMMAND_MODULES: tuple[str, ...] = ("status", "rulings", "register", "briefs", "trace", "interview")

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


def _project_root() -> Path:
    try:
        return find_root()
    except KitError:
        return Path.cwd().resolve()  # a fresh directory: init creates program/ here


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def cmd_init(args: argparse.Namespace) -> int:
    target = _project_root() / "program"
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
        staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=".program.", suffix=".tmp"))
        for name, data in contents.items():
            (staging / name).write_bytes(data)
        os.chmod(staging, 0o777 & ~_umask())  # mkdtemp makes it 0700
        staging.rename(target)
    except OSError as exc:
        raise KitError(f"cannot create {target}: {exc.strerror or exc}") from exc
    finally:
        if staging is not None and staging.exists():  # still there only if the rename did not happen
            shutil.rmtree(staging, ignore_errors=True)
    print(f"created {target}")
    return 0


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
    read_yaml(program / "checks.yaml")  # raises KitError naming the file and line
    print("doctor: ok")
    return 0


register("init", cmd_init, help="create program/ from the templates",
         configure=lambda p: p.add_argument("--preset", choices=PRESETS,
                                            help="start checks.yaml from a language preset"))
register("doctor", cmd_doctor, help="check the environment and program/ are usable")


if __name__ == "__main__":
    sys.exit(main())
