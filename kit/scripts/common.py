"""Shared helpers for the program-kit scripts (standard library only)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class KitError(Exception):
    """A user-facing failure; scripts print it as one line and exit non-zero."""


def find_root(start: Path | None = None) -> Path:
    """Nearest ancestor of `start` (default: cwd) containing `program/` or `.git`."""
    here = Path(start if start is not None else Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "program").is_dir() or (candidate / ".git").exists():
            return candidate
    raise KitError(f"no project root (program/ or .git) at or above {here}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file in the same directory and os.replace.

    Any failure to write (a parent that is a file, a read-only directory, a full disk, text that
    cannot be encoded) is a KitError naming the path, never an OSError traceback.
    """
    path = Path(path)
    try:
        _write_through_temp(path, text)
    except OSError as exc:
        raise KitError(f"cannot write {path}: {exc.strerror or exc}") from exc
    except UnicodeEncodeError as exc:
        raise KitError(f"cannot write {path}: text is not encodable as UTF-8") from exc


def _write_through_temp(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: a temp file in the same directory, then os.replace.

    Parent directories are created and an existing file keeps its permission bits. Either the old
    content or the new content is on disk, never a mixture.
    """
    _atomic_write(path, text)


def drop_bom(text: str) -> str:
    """`text` without a leading UTF-8 byte-order mark (common in files saved by other tools)."""
    return text[1:] if text.startswith("\ufeff") else text


def read_text(path: Path, *, newline: str | None = None) -> str:
    """The file's text, UTF-8, without a leading byte-order mark: the one reader for every file the kit reads as text.

    A file that cannot be read or is not UTF-8 is a KitError naming the path. Line endings are translated to
    `\\n` as `open` does; pass `newline=""` to get them as they are on disk.
    """
    try:
        with open(path, encoding="utf-8", newline=newline) as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise KitError(f"cannot read {path}: {reason}") from exc
    return drop_bom(text)


def read_yaml(path: Path):
    import miniyaml  # lazy: miniyaml imports KitError from this module

    text = read_text(path)
    try:
        return miniyaml.loads(text)
    except miniyaml.YAMLError as exc:
        raise miniyaml.YAMLError(f"{path}: {exc}") from exc
    except RecursionError as exc:
        raise miniyaml.YAMLError(f"{path}: nested too deeply") from exc


def write_yaml(path: Path, data) -> None:
    import miniyaml

    try:
        text = miniyaml.dumps(data)  # dumps first: bad data never touches the file
    except RecursionError as exc:
        raise miniyaml.YAMLError(f"cannot serialise {path} as YAML: nested too deeply") from exc
    _atomic_write(path, text)


def read_json(path: Path):
    text = read_text(path)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise KitError(f"{path}: invalid JSON: {exc}") from exc
    except RecursionError as exc:
        raise KitError(f"{path}: JSON nested too deeply") from exc


def write_json(path: Path, data) -> None:
    try:
        text = json.dumps(data, indent=2) + "\n"
    except (TypeError, ValueError) as exc:
        raise KitError(f"cannot serialise {path} as JSON: {exc}") from exc
    _atomic_write(path, text)


def git(root: Path, *args: str) -> str:
    """Run git in `root`; return stripped stdout, raise KitError on failure.

    The error names git's own `fatal:` or `error:` line, else its first line: from stderr, from stdout when
    stderr is blank, and `exit N` when both are.
    """
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc
    except ValueError as exc:  # an argument git cannot be given, such as one with a NUL byte
        raise KitError("git: invalid argument") from exc
    if proc.returncode != 0:
        said = proc.stderr if proc.stderr.strip() else proc.stdout  # some failures are reported on stdout only
        lines = [ln.strip() for ln in said.splitlines() if ln.strip()]
        reason = next((ln for ln in lines if ln.startswith(("fatal:", "error:"))),
                      lines[0] if lines else f"exit {proc.returncode}")
        raise KitError(f"git {' '.join(args)} failed: {reason}")
    return proc.stdout.strip()


PRIMARY_BRANCH = "main"
GITIGNORE_LINES = ("__pycache__/", "*.pyc", ".worktrees/")


def ensure_gitignore(root: Path) -> list[str]:
    """Append the lines of GITIGNORE_LINES that `<root>/.gitignore` lacks (creating it); return the lines added.

    Existing content is never rewritten: a missing line is appended, after a newline if the file does not
    end in one. A line counts as present only when a whole line equals it (a CRLF line ending is ignored).
    """
    path = Path(root) / ".gitignore"
    try:
        existing = path.read_bytes() if path.exists() else b""
        present = {line.rstrip("\r") for line in existing.decode("utf-8", errors="replace").split("\n")}
        missing = [line for line in GITIGNORE_LINES if line not in present]
        if missing:
            lead = b"\n" if existing and not existing.endswith(b"\n") else b""
            with open(path, "ab") as handle:
                handle.write(lead + "".join(line + "\n" for line in missing).encode("utf-8"))
    except OSError as exc:
        raise KitError(f"cannot update {path}: {exc.strerror or exc}") from exc
    return missing


def primary_branch_problem(root: Path, main: str = PRIMARY_BRANCH, need_commit: bool = False) -> str | None:
    """Why `main` cannot serve as the primary branch of the repository at `root`, or None when it can.

    The primary branch is fixed as `main`. With `need_commit` false a `main` that has no commit yet
    (a fresh `git init -b main`) is accepted; the commands that read history pass True.
    """
    root = Path(root)
    try:
        git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{main}^{{commit}}")
        return None
    except KitError:
        pass
    try:
        head = git(root, "symbolic-ref", "--quiet", "HEAD")
    except KitError:
        try:
            git(root, "rev-parse", "--git-dir")
        except KitError as exc:
            return str(exc)
        head = ""  # a repository with a detached HEAD
    if head == f"refs/heads/{main}":
        return None if not need_commit else f"branch {main} has no commits yet; make the first commit"
    current = head[len("refs/heads/"):] if head.startswith("refs/heads/") else "none"
    return (f"the primary branch must be named {main} (this repository has no branch {main}; "
            f"the current branch is {current}); rename it with: git branch -m {main}")


def run_argv(argv: list[str], cwd: Path, env: dict | None = None,
             timeout: int | None = None) -> tuple[int, str]:
    """Run argv directly (never a shell); return (exit code, stdout+stderr combined).

    Shell conventions for the two non-exit cases: 124 on timeout, 127 if the
    executable cannot be started.
    """
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", errors="replace")
        return 124, f"{partial}timed out after {timeout}s: {argv[0]}\n"
    except OSError as exc:
        return 127, f"cannot run {argv[0]}: {exc.strerror or exc}\n"
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")
