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
    """Write `text` to `path` via a temp file in the same directory and os.replace."""
    path = Path(path)
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


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        raise KitError(f"cannot read {path}: {reason}") from exc


def read_yaml(path: Path):
    import miniyaml  # lazy: miniyaml imports KitError from this module

    text = _read_text(path)
    try:
        return miniyaml.loads(text)
    except miniyaml.YAMLError as exc:
        raise miniyaml.YAMLError(f"{path}: {exc}") from exc


def write_yaml(path: Path, data) -> None:
    import miniyaml

    _atomic_write(path, miniyaml.dumps(data))  # dumps first: bad data never touches the file


def read_json(path: Path):
    text = _read_text(path)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise KitError(f"{path}: invalid JSON: {exc}") from exc


def write_json(path: Path, data) -> None:
    try:
        text = json.dumps(data, indent=2) + "\n"
    except (TypeError, ValueError) as exc:
        raise KitError(f"cannot serialise {path} as JSON: {exc}") from exc
    _atomic_write(path, text)


def git(root: Path, *args: str) -> str:
    """Run git in `root`; return stripped stdout, raise KitError with stderr on failure."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr or proc.stdout).splitlines() if ln.strip()]
        reason = next((ln for ln in lines if ln.startswith(("fatal:", "error:"))),
                      lines[0] if lines else f"exit {proc.returncode}")
        raise KitError(f"git {' '.join(args)} failed: {reason}")
    return proc.stdout.strip()


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
