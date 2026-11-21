#!/usr/bin/env python3
"""The project's secret scanner over copies of files (0.6.1, KF-21 and KF-55 of the program it came from).

    pk scan staged [--path PREFIX ...]

A gate's `scan` step runs over the whole tree, so a flagged literal quoted in a ruling, a PLAN or a stand-in patch was
found only on main, after the commit. This runs the scanner before: the commands of `secret_scan` in
`program/checks.yaml`, else those of `progress.scan` (the same shape), each an argv list run in a temporary
directory that holds copies of the files at their repository paths; `{dir}` and `{root}` in an argument are replaced,
and a program named with a slash is the repository's. A command that exits non-zero is a finding: the scan refuses,
naming the command and the tail of its output, and nothing is committed by its caller.

- `pk scan staged` scans the staged content (`git show :<path>`) of every staged file that is added, copied, modified
  or renamed (`--path` narrows it to paths under the prefixes). Exit 0 with one line, or 1 with the finding; without
  a scanner configured it is an error. Use it before a commit by hand or from a pre-commit hook.
- `pk spec precheck` scans copies of the task folder, `standin.patch` included (PC-8).
- `pk record` scans the files it commits when `secret_scan` is set (and only then: it is opt-in there).
- `pk progress` scans with `progress.scan`, else `secret_scan`, as before.
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from common import KitError, find_root, git, read_yaml, run_argv

SCAN_TIMEOUT = 600  # seconds per scan command
TAIL_CHARS = 600


def scanner(checks: dict | None) -> tuple[list[list[str]], str]:
    """(the scan commands, the key they came from) of parsed checks: `secret_scan`, else `progress.scan`."""
    checks = checks or {}
    if checks.get("secret_scan"):
        return checks["secret_scan"], "secret_scan"
    progress = checks.get("progress") or {}
    if progress.get("scan"):
        return progress["scan"], "progress.scan"
    return [], ""


def checkout_checks(root: Path) -> dict | None:
    """The parsed `program/checks.yaml` of the checkout, or None when it is absent; a bad file is a KitError."""
    path = Path(root) / "program" / "checks.yaml"
    if not path.is_file():
        return None
    import gate  # late: the gate's parser is the one rule for checks.yaml

    return gate._parse_checks(read_yaml(path), ())


def _safe(rel: str) -> str:
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise KitError(f"scan: {rel!r} is not a path inside the repository")
    return pure.as_posix()


def scan_copies(root: Path, commands: list[list[str]], files: dict[str, bytes]) -> str | None:
    """Run each command over copies of `files` ({repository path: bytes}); why it refused, or None when all pass."""
    if not commands or not files:
        return None
    with tempfile.TemporaryDirectory(prefix="pk-scan-") as tmp:
        for rel, data in files.items():
            target = Path(tmp) / _safe(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for argv in commands:
            argv = [arg.replace("{root}", str(root)).replace("{dir}", tmp) for arg in argv]
            if "/" in argv[0] and not Path(argv[0]).is_absolute():
                argv[0] = str(Path(root) / argv[0])  # a program of the repository, named from its root
            code, output = run_argv(argv, Path(tmp), timeout=SCAN_TIMEOUT)
            if code != 0:
                tail = " ".join(output.replace(tmp + "/", "").split())[-TAIL_CHARS:]
                return f"{' '.join(argv)} exit {code}: {tail}"
    return None


def working_copies(root: Path, paths) -> dict[str, bytes]:
    """{path: bytes} of the paths that are files in the working tree."""
    return {rel: (Path(root) / rel).read_bytes() for rel in paths if (Path(root) / rel).is_file()}


def staged_files(root: Path, prefixes=()) -> dict[str, bytes]:
    """{path: staged bytes} of the staged files that are added, copied, modified or renamed, under `prefixes`."""
    names = [name for name in git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
             if name]
    wanted = [name.rstrip("/") for name in prefixes]
    if wanted:
        names = [name for name in names if any(name == p or name.startswith(p + "/") for p in wanted)]
    found = {}
    for name in names:
        code, _ = run_argv(["git", "-C", str(root), "cat-file", "-e", f":{name}"], Path(root))
        if code == 0:
            found[name] = subprocess.run(["git", "-C", str(root), "show", f":{name}"], capture_output=True,
                                         check=True).stdout
    return found


def scan_staged(root: Path, prefixes=()) -> tuple[int, str]:
    """(exit, the line to print) of `pk scan staged`."""
    commands, key = scanner(checkout_checks(root))
    if not commands:
        raise KitError("pk scan staged needs a scanner: secret_scan (or progress.scan) in program/checks.yaml, a list "
                       "of argv lists run over copies ({dir} is the copies' folder, {root} the repository)")
    files = staged_files(root, prefixes)
    if not files:
        return 0, "scan staged: nothing staged to scan"
    problem = scan_copies(root, commands, files)
    if problem:
        return 1, (f"FAILED: scan staged ({key}) over {len(files)} staged file(s): {problem}; fix the text, stage it "
                   "again and re-run before committing")
    return 0, f"scan staged: ok ({len(commands)} command(s) of {key} over {len(files)} staged file(s))"


def cmd_scan(args: argparse.Namespace) -> int:
    if getattr(args, "scan_action", None) != "staged":
        raise KitError("scan needs an action: staged")
    code, line = scan_staged(find_root(), args.path)
    print(line)
    return code


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="scan_action", metavar="action", parser_class=type(parser))
    staged = actions.add_parser("staged", help="run the project's secret scanner over copies of the staged files")
    staged.add_argument("--path", action="append", default=[], metavar="PREFIX",
                        help="only staged files under this path (repeatable), for example program/")


def register(add_command) -> None:
    add_command("scan", cmd_scan, help="the project's secret scanner over copies of staged files, before a commit",
                configure=_configure)
