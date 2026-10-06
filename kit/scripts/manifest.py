"""Kit manifest (`kit/MANIFEST.sha256`): one `<sha256>  <path>` line per file under kit/.

`write` records every regular file of the kit directory (paths are POSIX, relative to kit/,
sorted); `check` compares the directory with the recorded lines and reports modified, missing
and added files. The upgrade path of install.sh uses `check` to refuse replacing a kit
that has local changes.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from common import KitError, _atomic_write, sha256_file

KIT_DIR = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "MANIFEST.sha256"
_SKIPPED_DIRS = {"__pycache__"}
_SKIPPED_FILES = {".DS_Store"}
_LINE = re.compile(r"([0-9a-f]{64})  (\S.*)\Z")


def _files(kit_dir: Path) -> dict[str, Path]:
    """Relative POSIX path -> file, for every regular file the manifest covers."""
    kit_dir = Path(kit_dir)
    found: dict[str, Path] = {}
    for current, dirs, names in os.walk(kit_dir):
        base = Path(current)
        for name in dirs:
            if (base / name).is_symlink():
                raise KitError(f"{(base / name).relative_to(kit_dir).as_posix()}: symlinks are not allowed in the kit")
        dirs[:] = sorted(name for name in dirs if name not in _SKIPPED_DIRS)
        for name in names:
            path = base / name
            relative = path.relative_to(kit_dir).as_posix()
            if path.is_symlink():
                raise KitError(f"{relative}: symlinks are not allowed in the kit")
            if name in _SKIPPED_FILES or name.endswith(".pyc") or relative == MANIFEST_NAME:
                continue
            if path.is_file():
                found[relative] = path
    return found


def write(kit_dir: Path) -> Path:
    """Write `<kit_dir>/MANIFEST.sha256` atomically and return its path."""
    kit_dir = Path(kit_dir)
    lines = []
    for relative, path in sorted(_files(kit_dir).items()):
        line = f"{sha256_file(path)}  {relative}"
        try:
            line.encode("utf-8")
        except UnicodeEncodeError:
            raise KitError(f"{ascii(relative)}: file name is not valid UTF-8 and cannot be recorded") from None
        if _LINE.match(line) is None or line.splitlines() != [line]:
            raise KitError(f"{ascii(relative)}: file name cannot be recorded in the manifest (line break or leading space)")
        lines.append(line)
    target = kit_dir / MANIFEST_NAME
    _atomic_write(target, "".join(line + "\n" for line in lines))
    return target


def _read(kit_dir: Path) -> dict[str, str]:
    path = Path(kit_dir) / MANIFEST_NAME
    try:
        text = path.read_bytes().decode("utf-8")  # no newline translation
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise KitError(f"cannot read {path}: {reason}") from exc
    recorded: dict[str, str] = {}
    if text and not text.endswith("\n"):
        raise KitError(f"{path}: malformed manifest (last line has no newline)")
    for number, line in enumerate(text.split("\n")[:-1], 1):  # split on "\n" only: a CRLF line must not pass
        match = _LINE.match(line)
        if match is None or line.splitlines() != [line]:
            raise KitError(f"{path} line {number}: malformed manifest line")
        if match.group(2) in recorded:
            raise KitError(f"{path} line {number}: duplicate path {match.group(2)}")
        recorded[match.group(2)] = match.group(1)
    return recorded


def check(kit_dir: Path) -> list[str]:
    """Sorted `modified: `, `missing: ` and `added: ` lines; empty when the kit matches its manifest."""
    recorded = _read(kit_dir)
    present = _files(kit_dir)
    problems = [f"added: {name}" for name in present if name not in recorded]
    for name, digest in recorded.items():
        if name not in present:
            problems.append(f"missing: {name}")
        elif sha256_file(present[name]) != digest:
            problems.append(f"modified: {name}")
    return sorted(problems)


def cmd_manifest(args: argparse.Namespace) -> int:
    if args.action == "write":
        target = write(KIT_DIR)
        print(f"manifest: {len(_read(KIT_DIR))} files")
        return 0
    problems = check(KIT_DIR)
    if not problems:
        print("manifest: clean")
        return 0
    for problem in problems:
        print("error: " + problem, file=sys.stderr)
    return 1


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("write", "check"), help="write the manifest, or compare the kit with it")


def register(add_command) -> None:
    add_command("manifest", cmd_manifest, help="write or check kit/MANIFEST.sha256", configure=_configure)
