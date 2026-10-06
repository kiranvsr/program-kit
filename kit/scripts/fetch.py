#!/usr/bin/env python3
"""Digest-addressed download cache: the only kit script that touches the network.

Anything a validator needs from the network is cached once per host under
`<cache root>/<sha256>/<filename>` by `pk fetch`, and validators only ever check the cache
(`pk fetch verify`), failing with the fetch command to run. A pin is
`{url, sha256, size, filename}`. The `root` parameter of every cache function here is the
cache root (see `cache_root()`), not the project root.

Guards: names are one plain path component; the URL scheme must be https (http only when
PROGRAM_KIT_FETCH_ALLOW_HTTP=1, used by tests against a local server); a download is written
to a temporary file created in the digest directory, bounded by the pinned size, checked for
size and digest, then renamed into place; the temporary file is removed on every other path;
a symlinked entry or digest directory is never followed (the link itself is unlinked).
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

from common import KitError, read_json

ALLOW_HTTP = "PROGRAM_KIT_FETCH_ALLOW_HTTP"
CHUNK = 1 << 20
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PIN_KEYS = ("url", "sha256", "size", "filename")


def cache_root() -> Path:
    """The cache root: $PROGRAM_KIT_CACHE, else ~/.cache/program-kit/fetch."""
    override = os.environ.get("PROGRAM_KIT_CACHE")
    return Path(override) if override else Path.home() / ".cache" / "program-kit" / "fetch"


def archive_name(name: str) -> str:
    """`name` unchanged if it is one plain path component; KitError otherwise."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise KitError(f"cache name rejected: {name!r} (letters, digits, '.', '_', '-'; must not start with a symbol)")
    return name


def _digest(value) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise KitError(f"sha256 rejected: {value!r} (needs 64 lowercase hex characters)")
    return value


def cache_path(root: Path, sha256: str, name: str) -> Path:
    """`<root>/<sha256>/<name>`, with root the cache root; KitError for a bad digest or name."""
    return Path(root) / _digest(sha256) / archive_name(name)


def _inspect(root: Path, sha256: str, name: str) -> tuple[Path | None, str]:
    """(path, "") for a good cache entry, else (None, why it is a miss)."""
    path = cache_path(root, sha256, name)
    if path.parent.is_symlink():
        return None, "digest directory is a symlink"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)  # never block on a FIFO
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "not in the cache"
    except OSError:  # includes ELOOP for a symlinked entry
        return None, "entry is a symlink or unreadable"
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "entry is not a regular file"
        while chunk := os.read(fd, CHUNK):
            digest.update(chunk)
    except OSError as error:
        return None, f"entry cannot be read: {_one_line(error)}"
    finally:
        os.close(fd)
    if digest.hexdigest() != sha256:
        return None, "cached file does not match its digest"
    return path, ""


def cached(root: Path, sha256: str, name: str) -> Path | None:
    """The entry's path if it is a regular file whose sha256 matches, else None.

    A symlinked entry or digest directory is a miss, and a mismatching file is left in place.
    """
    return _inspect(root, sha256, name)[0]


def _pin(pin) -> dict:
    if not isinstance(pin, dict):
        raise KitError("each pin must be an object with url, sha256, size and filename")
    for key in _PIN_KEYS:
        if key not in pin:
            raise KitError(f"pin is missing '{key}'")
    if not isinstance(pin["url"], str) or not pin["url"]:
        raise KitError(f"pin {pin['filename']!r}: url must be a non-empty string")
    if not isinstance(pin["size"], int) or isinstance(pin["size"], bool) or pin["size"] < 0:
        raise KitError(f"pin {pin['filename']!r}: size must be a non-negative integer")
    _digest(pin["sha256"])
    archive_name(pin["filename"])
    return pin


def _allowed_schemes() -> tuple[str, ...]:
    return ("https", "http") if os.environ.get(ALLOW_HTTP) == "1" else ("https",)


class _Refused(Exception):
    """A pin that cannot be stored; the message is the one-line reason."""


class _SchemeRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect may not leave the allowed schemes (https must not fall back to http, or to file)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in _allowed_schemes():
            raise urllib.error.URLError("redirect scheme not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _one_line(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _download(root: Path, pin: dict, opener, timeout: int) -> Path:
    url, size, sha256 = pin["url"], pin["size"], pin["sha256"]
    if urllib.parse.urlsplit(url).scheme.lower() not in _allowed_schemes():
        raise _Refused("scheme not allowed")
    path = cache_path(root, sha256, pin["filename"])
    directory = path.parent
    temporary, renamed = None, False
    try:
        if opener is None:
            opener = urllib.request.build_opener(_SchemeRedirects)
        with closing(opener.open(urllib.request.Request(url), timeout=timeout)) as response:
            if directory.is_symlink():
                os.unlink(directory)  # the link itself, never its target
            directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".part")
            digest, received = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise _Refused(f"size mismatch: more than the pinned {size} bytes arrived")
                    digest.update(chunk)
                    out.write(chunk)
        if received != size:
            raise _Refused(f"size mismatch: expected {size} bytes, received {received}")
        if digest.hexdigest() != sha256:
            raise _Refused(f"digest mismatch: expected {sha256}, received {digest.hexdigest()}")
        if path.is_symlink():
            os.unlink(path)  # the link itself, never its target
        os.replace(temporary, path)
        renamed = True
        return path
    except (OSError, http.client.HTTPException, urllib.error.URLError, ValueError) as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        raise _Refused(_one_line(error)) from error
    finally:
        if temporary is not None and not renamed:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def fetch(root: Path, pins: list[dict], opener=None, timeout: int = 600) -> list[dict]:
    """Make every pin present in the cache (root is the cache root); never raises for a bad download.

    Rows are {filename, state: cached|fetched|refused, reason}; reason is the path for cached and
    fetched, and one line saying why for refused. A malformed pin is a KitError before any network call.
    """
    checked = [_pin(pin) for pin in pins]
    rows = []
    for pin in checked:
        name = pin["filename"]
        hit = cached(root, pin["sha256"], name)
        if hit is not None:
            rows.append({"filename": name, "state": "cached", "reason": str(hit)})
            continue
        try:
            rows.append({"filename": name, "state": "fetched", "reason": str(_download(root, pin, opener, timeout))})
        except _Refused as refusal:
            rows.append({"filename": name, "state": "refused", "reason": str(refusal)})
    return rows


def verify(root: Path, pins: list[dict]) -> list[dict]:
    """Cache-only check (root is the cache root): rows {filename, state: cached|missing, reason}.

    Builds no opener and makes no request; a miss is the caller's cue to name `pk fetch`.
    """
    rows = []
    for pin in [_pin(pin) for pin in pins]:
        path, why = _inspect(root, pin["sha256"], pin["filename"])
        rows.append({"filename": pin["filename"], "state": "missing" if path is None else "cached",
                     "reason": why if path is None else str(path)})
    return rows


# --- command -----------------------------------------------------------------------

def _load_pins(path: str | None) -> list[dict]:
    if not path:
        raise KitError("needs --pins file.json")
    data = read_json(Path(path))
    if not isinstance(data, list) or not data:
        raise KitError(f"{path}: expected a non-empty JSON list of pins")
    return data


def cmd_fetch(args: argparse.Namespace) -> int:
    pins_file = getattr(args, "verify_pins", None) or args.pins
    pins = _load_pins(pins_file)
    if getattr(args, "fetch_action", None) == "verify":
        rows = verify(cache_root(), pins)
        for row in rows:
            if row["state"] == "missing":
                print(f"error: {row['filename']} is not in the cache; run: pk fetch --pins {pins_file}",
                      file=sys.stderr)
            else:
                print(f"{row['state']} {row['filename']} {row['reason']}")
        return 1 if any(row["state"] == "missing" for row in rows) else 0
    rows = fetch(cache_root(), pins)
    for row in rows:
        print(f"{row['state']} {row['filename']} {row['reason']}")
    return 1 if any(row["state"] == "refused" for row in rows) else 0


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pins", metavar="file.json", help="JSON list of {url, sha256, size, filename}")
    actions = parser.add_subparsers(dest="fetch_action", metavar="action", parser_class=type(parser))
    checker = actions.add_parser("verify", help="check the cache only; never downloads")
    checker.add_argument("--pins", dest="verify_pins", metavar="file.json", help="JSON list of pins")


def register(add_command) -> None:
    add_command("fetch", cmd_fetch, help="download pinned files into the digest cache, or verify them",
                configure=_configure)
