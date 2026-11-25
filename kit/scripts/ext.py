#!/usr/bin/env python3
"""The extension interface (0.7.0): a programme adds its own behaviour in code it owns, never by editing kit/.

Declaration. `program/extensions.yaml` holds `api: 1` and `modules: [{name: <module>}]`. Each module is a Python module
(`program/extensions/<name>.py`) or package (`program/extensions/<name>/__init__.py`) whose name matches
`[a-z_][a-z0-9_]*`. Without the file the kit behaves exactly as before. When the declared api differs from
API_VERSION, or the file does not read, every pk command refuses, naming the problem.

Loading from main. Extension code is always read from main's committed tree (`git ls-tree` and `git cat-file` of
`refs/heads/main`, so a tag named main cannot stand in for the branch), written into a private temporary directory
outside the repository and imported from there under a package name of its own, so no kit module is shadowed and no
bytecode lands in the repository; the directory is removed when the registry is closed (at the latest when the pk
process exits). A task branch therefore cannot change the rules it
is judged by. `PK_EXT_FROM=worktree` loads the working copy instead, for developing an extension; the hand-back gate,
the merge check (so `pk merge` and `pk integrate check`), the binding check and every status move (`pk status set`,
`pk status reopen`, `pk evidence accept --done`; a tier change is not a move) refuse to run while it is set.

Registration. Each module defines `register(api)` and calls these, and only these, while it runs:

- `api.command(name, handler, help="", configure=None)`: a new pk subcommand; refused when it collides with a kit
  command or another extension's.
- `api.on_status_transition(fn)`: `fn(task_id, old, new, record, root)` returns None or a refusal string; called
  after the kit's own checks pass and before anything is written. It gets a copy of the record.
- `api.after_worktree_new(fn)`: `fn(task_id, path, root)`; when it raises, `pk worktree new` removes what it made.
- `api.gate_step(name, fn, gates=("handback", "merge"))`: `fn(ctx)` returns `(ok, lines)`; runs after the kit's own
  steps and before the `OK:` line, as `step ext:<module>.<name>`, under the kit's step timeout.
- `api.prepare_tree(fn)` (0.7.1): `fn(ctx)` with `ctx.root`, `ctx.tree`, `ctx.purpose` (handback, merge or
  integrate) and `ctx.head`; called once in each tree the kit makes to run a gate's commands in (the trial merge of
  the hand-back gate and of the merge check, the integration round's combined tree), after the kit made it and before
  any step runs there. It returns None. It may write only git-ignored paths: `git status --porcelain
  --untracked-files=all` of the tree is the same before and after the call, or the gate refuses, naming the paths.
- `api.drift_check(code, fn)`: code `X<n>`; `fn(root)` returns `[{severity: "note"|"fail", text}]`.
- `api.handoff_lines(fn)`: `fn(root)` returns a list of strings for the hand-off's Now list, after the kit's lines.
- `api.dashboard_data(fn)`: `fn(root)` returns a JSON-serialisable dict, shown under `extensions.<module>` (one per
  module).
- `api.adopt_importer(name, fn)`: `fn(root, argv)` returns an adoption source dict (`pk adopt --from <name>`).
- `api.reference_resolver(fn)`: `fn(path, data_bytes)` returns `{kind, task_id?, decision?, summary}`, for display.

Extensions add and refuse, never loosen. Nothing a hook returns can make the kit accept what its own checks refuse:
the hooks run after those checks, a status hook sees a copy, and anything but the exact accepting value (None from a
status hook or a tree preparer, `(True, [str])` from a gate step) is a refusal. An exception or a sys.exit
(HOOK_FAILURES) in a refusing point (status transition, gate step, prepare-tree, after-worktree, importer) is a
refusal naming it, and so is a module that did not
load in the registry the point reads. An exception or a sys.exit in an informational point (drift, hand-off,
dashboard, resolver) is a note, and the command still runs.

The guarantee is about the interface. Extension code runs in the pk process with the kit's own rights, so code that
patches kit modules is not stopped by this file: that is why the declaration and every extension file are always
guarded and always full-tier (gate.EXT_GUARDED, gate.BUILTIN_FULL), so a change to them gets the full review.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import copy
import importlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path

import miniyaml
from common import KitError, PRIMARY_BRANCH, drop_bom, find_root, sha256_bytes

API_VERSION = 1
DECLARATION = "program/extensions.yaml"
EXT_DIR = "program/extensions"
PATHS = (DECLARATION, EXT_DIR + "/")  # always guarded and always full-tier
MAIN_REF = f"refs/heads/{PRIMARY_BRANCH}"  # the branch: a tag named main must not stand in for it
ENV = "PK_EXT_FROM"
WORKTREE = "worktree"
DECLARATION_KEYS = ("api", "modules")
MODULE_KEYS = ("name",)
GATES = ("handback", "merge")
POINTS = ("command", "on_status_transition", "after_worktree_new", "gate_step", "prepare_tree", "drift_check",
          "handoff_lines", "dashboard_data", "adopt_importer", "reference_resolver")
PURPOSES = ("handback", "merge", "integrate")  # the trees prepare_tree is called for (0.7.1)
SHOWN_PATHS = 10  # changed paths a prepare_tree refusal names
SEVERITIES = ("note", "fail")
RESOLVED_KEYS = ("kind", "task_id", "decision", "summary")
DESCRIBE_CHARS = 300
# What a hook's failure is, at every point: an exception, and a sys.exit too (argparse in a hook raises SystemExit(2)),
# so a hook can never end the pk command, skip what follows it (pk merge's bookkeeping) or pass by exiting 0.
# KeyboardInterrupt is the person at the keyboard, not the hook: it still stops the command.
HOOK_FAILURES = (Exception, SystemExit)

_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_COMMAND = re.compile(r"[a-z][a-z0-9-]*\Z")
_IMPORTER = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_DRIFT = re.compile(r"X[1-9][0-9]*\Z")


class ExtensionError(KitError):
    """A module broke the interface while registering (it then contributes nothing)."""


def describe(exc: BaseException) -> str:
    """`Type: message` on one line, cut to DESCRIBE_CHARS."""
    text = " ".join(str(exc).split())
    said = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return said if len(said) <= DESCRIBE_CHARS else said[:DESCRIBE_CHARS - 3] + "..."


def _one_line(text) -> str:
    return " ".join(str(text).split())


def note(text: str) -> None:
    """An informational point failed: one `note:` line on standard error; the command runs on."""
    print(f"note: {_one_line(text)}", file=sys.stderr)


# --- where the code comes from ---------------------------------------------------------------

def mode() -> str:
    """"main" (the default) or "worktree" (PK_EXT_FROM=worktree); any other value is refused."""
    value = os.environ.get(ENV, "")
    if value == "":
        return "main"
    if value == WORKTREE:
        return WORKTREE
    raise KitError(f"{ENV}={value!r} is not understood: its only value is {WORKTREE} (unset it to load extensions "
                   f"from {PRIMARY_BRANCH})")


def development_refusal(what: str) -> str | None:
    """Why `what` refuses to run: PK_EXT_FROM is set (whatever its value), or None."""
    value = os.environ.get(ENV, "")
    if not value:
        return None
    return (f"{what} refuses to run while {ENV} is set ({ENV}={value}): it is for developing an extension, and a "
            f"gate or a status write is judged by the extensions on {PRIMARY_BRANCH}; unset it")


def _git(root: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True)
    except OSError as exc:
        raise KitError(f"cannot run git: {exc.strerror or exc}") from exc


def _tree_files(root: Path, ref: str) -> dict[str, bytes] | None:
    """{path: bytes} of the declaration and every file under program/extensions/ committed at `ref`.

    None when `ref` does not name a commit (no main yet, or not a repository). A symlink or a submodule there is
    refused: extension code is plain files.
    """
    listed = _git(root, "ls-tree", "-r", "-z", "--full-tree", ref, "--", DECLARATION, EXT_DIR)
    if listed.returncode != 0:
        if _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
            return None
        said = listed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise KitError(f"cannot list {EXT_DIR} at {ref}: {said[0] if said else f'exit {listed.returncode}'}")
    entries = []
    for item in listed.stdout.decode("utf-8", "replace").split("\0"):
        if not item:
            continue
        meta, _, path = item.partition("\t")
        mode_, kind, sha = meta.split()
        if kind != "blob" or mode_ == "120000":
            raise KitError(f"{path} at {ref} is a {'symlink' if mode_ == '120000' else kind}: extension code must be "
                           "plain files")
        entries.append((path, sha))
    if not entries:
        return {}
    batch = _git(root, "cat-file", "--batch", data="".join(f"{sha}\n" for _, sha in entries).encode())
    if batch.returncode != 0:
        raise KitError(f"cannot read {EXT_DIR} at {ref}")
    out, files, at = batch.stdout, {}, 0
    for path, _ in entries:
        end = out.index(b"\n", at)
        size = int(out[at:end].split()[2])
        files[path] = out[end + 1:end + 1 + size]
        at = end + 1 + size + 1
    return files


def _worktree_files(root: Path) -> dict[str, bytes]:
    """The working copy's declaration and extension files (PK_EXT_FROM=worktree), without bytecode."""
    files = {}
    declaration = root / DECLARATION
    if declaration.is_file():
        files[DECLARATION] = declaration.read_bytes()
    base = root / EXT_DIR
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if "__pycache__" in path.relative_to(base).parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise KitError(f"{rel} is a symlink: extension code must be plain files")
            if path.is_file():
                files[rel] = path.read_bytes()
    return files


def digest_at(root, ref: str) -> str | None:
    """sha256 over the declaration and every extension file at `ref`: one `<sha256>  <path>` line per file, sorted by
    path (the fingerprint's `toolchain.extensions_sha256`); None when there is none."""
    files = _tree_files(Path(root), ref)
    if not files:
        return None
    return sha256_bytes("".join(f"{sha256_bytes(data)}  {path}\n" for path, data in sorted(files.items())).encode())


def parse_declaration(data: bytes, where: str) -> dict:
    """{api, modules: [name]} from the declaration's bytes; KitError naming the problem."""
    try:
        loaded = miniyaml.loads(drop_bom(data.decode("utf-8")))
    except UnicodeDecodeError:
        raise KitError(f"{where} is not UTF-8") from None
    except (miniyaml.YAMLError, RecursionError) as exc:
        raise KitError(f"{where}: {_one_line(exc)}") from None
    if not isinstance(loaded, dict):
        raise KitError(f"{where} must be a mapping with the keys api and modules")
    unknown = sorted(str(key) for key in loaded if key not in DECLARATION_KEYS)
    if unknown:
        raise KitError(f"{where}: unknown key(s) {', '.join(unknown)} (the keys are api and modules)")
    api = loaded.get("api")
    if not isinstance(api, int) or isinstance(api, bool):
        raise KitError(f"{where}: api must be a whole number (this kit's extension api is {API_VERSION})")
    if api != API_VERSION:
        raise KitError(f"{where} declares extension api {api}, but this kit's extension api is {API_VERSION} "
                       "(kit/scripts/ext.py API_VERSION): every pk command refuses until they match; port the "
                       f"extensions and change the declaration on {PRIMARY_BRANCH}")
    modules = loaded.get("modules")
    if modules is None:
        modules = []
    if not isinstance(modules, list):
        raise KitError(f"{where}: modules must be a list of {{name: <module>}}")
    names = []
    for index, entry in enumerate(modules):
        if not isinstance(entry, dict) or set(entry) != set(MODULE_KEYS):
            raise KitError(f"{where}: modules[{index}] must be a mapping with the one key name")
        name = entry["name"]
        if not isinstance(name, str) or not _NAME.match(name):
            raise KitError(f"{where}: modules[{index}] name {name!r} must match [a-z_][a-z0-9_]*")
        if name in names:
            raise KitError(f"{where}: module {name} is declared twice")
        names.append(name)
    return {"api": api, "modules": names}


# --- registration ------------------------------------------------------------------------------

@dataclass(frozen=True)
class GateContext:
    """What a gate step gets: `path` is where the gate checks (the task worktree at the hand-back gate, the trial
    merge at the merge check, the round's worktree in an integration round); `head` is the branch tip judged."""
    gate: str
    root: Path
    task_id: str
    branch: str
    path: Path
    head: str
    worktree: Path | None = None
    trial: Path | None = None


@dataclass(frozen=True)
class TreeContext:
    """What a tree preparer gets (0.7.1): `tree` is the tree the kit just made, `root` the main checkout, `purpose`
    one of PURPOSES (the gate the tree is for: integrate for an integration round's tree) and `head` the commit
    judged there (the branch tip for handback and merge, the round's head or main's tip for integrate)."""
    root: Path
    tree: Path
    purpose: str
    head: str


@dataclass
class Registry:
    """What the declared modules registered, and every module that did not load (`errors`)."""
    source: str
    api: int
    modules: list[str]
    points: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    commands: dict[str, tuple] = field(default_factory=dict)  # name -> (module, handler, help, configure)
    status_hooks: list[tuple] = field(default_factory=list)  # (module, fn)
    worktree_hooks: list[tuple] = field(default_factory=list)  # (module, fn)
    gate_steps: list[tuple] = field(default_factory=list)  # (module, name, fn, gates)
    tree_hooks: list[tuple] = field(default_factory=list)  # (module, fn)
    drift_checks: list[tuple] = field(default_factory=list)  # (module, code, fn)
    handoff: list[tuple] = field(default_factory=list)  # (module, fn)
    dashboard: list[tuple] = field(default_factory=list)  # (module, fn)
    importers: dict[str, tuple] = field(default_factory=dict)  # name -> (module, fn)
    resolvers: list[tuple] = field(default_factory=list)  # (module, fn)
    directory: Path | None = None
    package: str | None = None
    key: tuple = ()

    def where(self) -> str:
        return f"{PRIMARY_BRANCH}'s committed tree" if self.source == "main" else f"the working copy ({ENV}={WORKTREE})"


def _callable(value, what: str) -> None:
    if not callable(value):
        raise ExtensionError(f"{what} must be callable")


class Api:
    """What `register(api)` receives: one per module, closed when `register` returns."""

    version = API_VERSION

    def __init__(self, module: str, kit_commands):
        self._module = module
        self._kit_commands = kit_commands
        self._open = True
        self._found: dict[str, list] = {point: [] for point in POINTS}

    def _add(self, point: str, item, label: str) -> None:
        if not self._open:
            raise ExtensionError(f"api.{point} called after register(api) returned")
        self._found[point].append((item, label))

    def command(self, name, handler, help="", configure=None):
        if not isinstance(name, str) or not _COMMAND.match(name):
            raise ExtensionError(f"command {name!r}: a name matches [a-z][a-z0-9-]*")
        if name in self._kit_commands():
            raise ExtensionError(f"command {name} collides with a kit command: an extension adds commands, it never "
                                 "replaces one")
        if any(item[0] == name for item, _ in self._found["command"]):
            raise ExtensionError(f"command {name} is registered twice")
        _callable(handler, f"command {name}: the handler")
        if configure is not None:
            _callable(configure, f"command {name}: configure")
        if not isinstance(help, str):
            raise ExtensionError(f"command {name}: help must be text")
        self._add("command", (name, handler, help, configure), f"command {name}")

    def on_status_transition(self, fn):
        _callable(fn, "on_status_transition")
        self._add("on_status_transition", fn, "on_status_transition")

    def after_worktree_new(self, fn):
        _callable(fn, "after_worktree_new")
        self._add("after_worktree_new", fn, "after_worktree_new")

    def gate_step(self, name, fn, gates=GATES):
        if not isinstance(name, str) or not _NAME.match(name):
            raise ExtensionError(f"gate_step {name!r}: a name matches [a-z_][a-z0-9_]*")
        if any(item[0] == name for item, _ in self._found["gate_step"]):
            raise ExtensionError(f"gate_step {name} is registered twice")
        _callable(fn, f"gate_step {name}")
        if isinstance(gates, str) or not isinstance(gates, (list, tuple)) or not gates \
                or any(gate not in GATES for gate in gates) or len(set(gates)) != len(gates):
            raise ExtensionError(f"gate_step {name}: gates must be a non-empty list of {' and '.join(GATES)}")
        self._add("gate_step", (name, fn, tuple(gates)), f"gate_step {name} ({', '.join(gates)})")

    def prepare_tree(self, fn):
        _callable(fn, "prepare_tree")
        self._add("prepare_tree", fn, "prepare_tree")

    def drift_check(self, code, fn):
        if not isinstance(code, str) or not _DRIFT.match(code):
            raise ExtensionError(f"drift_check {code!r}: a code is X<n> (D1 to D9 are the kit's)")
        if any(item[0] == code for item, _ in self._found["drift_check"]):
            raise ExtensionError(f"drift_check {code} is registered twice")
        _callable(fn, f"drift_check {code}")
        self._add("drift_check", (code, fn), f"drift_check {code}")

    def handoff_lines(self, fn):
        _callable(fn, "handoff_lines")
        self._add("handoff_lines", fn, "handoff_lines")

    def dashboard_data(self, fn):
        _callable(fn, "dashboard_data")
        if self._found["dashboard_data"]:
            raise ExtensionError("dashboard_data is registered twice: one dict per module, under extensions.<module>")
        self._add("dashboard_data", fn, "dashboard_data")

    def adopt_importer(self, name, fn):
        if not isinstance(name, str) or not _IMPORTER.match(name):
            raise ExtensionError(f"adopt_importer {name!r}: a name matches [a-z0-9][a-z0-9-]*")
        if any(item[0] == name for item, _ in self._found["adopt_importer"]):
            raise ExtensionError(f"adopt_importer {name} is registered twice")
        _callable(fn, f"adopt_importer {name}")
        self._add("adopt_importer", (name, fn), f"adopt_importer {name}")

    def reference_resolver(self, fn):
        _callable(fn, "reference_resolver")
        self._add("reference_resolver", fn, "reference_resolver")


def _kit_commands_uncached() -> frozenset:
    import pk  # late: pk imports this module; importing it again as a module only defines its registry

    names = set(pk.COMMANDS)

    def collect(name, *args, **kwargs):
        names.add(name)

    for module_name in pk.COMMAND_MODULES:
        importlib.import_module(module_name).register(collect)
    return frozenset(names)


_KIT_COMMANDS: list[frozenset] = []


def kit_commands() -> frozenset:
    """Every kit command name (computed once per process)."""
    if not _KIT_COMMANDS:
        _KIT_COMMANDS.append(_kit_commands_uncached())
    return _KIT_COMMANDS[0]


def _merge(registry: Registry, module: str, api: Api) -> None:
    """Add one module's registrations; a collision with an earlier module refuses the whole module."""
    found = api._found
    for (name, *_), _ in found["command"]:
        if name in registry.commands:
            raise ExtensionError(f"command {name} is already registered by {registry.commands[name][0]}")
    for (code, _), _ in found["drift_check"]:
        taken = next((m for m, c, _ in registry.drift_checks if c == code), None)
        if taken:
            raise ExtensionError(f"drift_check {code} is already registered by {taken}")
    for (name, _), _ in found["adopt_importer"]:
        if name in registry.importers:
            raise ExtensionError(f"adopt_importer {name} is already registered by {registry.importers[name][0]}")
    for (name, handler, help_text, configure), _ in found["command"]:
        registry.commands[name] = (module, handler, help_text, configure)
    registry.status_hooks += [(module, fn) for fn, _ in found["on_status_transition"]]
    registry.worktree_hooks += [(module, fn) for fn, _ in found["after_worktree_new"]]
    registry.gate_steps += [(module, name, fn, gates) for (name, fn, gates), _ in found["gate_step"]]
    registry.tree_hooks += [(module, fn) for fn, _ in found["prepare_tree"]]
    registry.drift_checks += [(module, code, fn) for (code, fn), _ in found["drift_check"]]
    registry.handoff += [(module, fn) for fn, _ in found["handoff_lines"]]
    registry.dashboard += [(module, fn) for fn, _ in found["dashboard_data"]]
    for (name, fn), _ in found["adopt_importer"]:
        registry.importers[name] = (module, fn)
    registry.resolvers += [(module, fn) for fn, _ in found["reference_resolver"]]
    registry.points[module] = [label for point in POINTS for _, label in found[point]]


@contextlib.contextmanager
def _no_bytecode():
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = before


def _materialise(files: dict[str, bytes]) -> Path:
    """A new private directory holding program/extensions/ as its top level."""
    directory = Path(tempfile.mkdtemp(prefix="pk-ext-"))
    prefix = EXT_DIR + "/"
    for path, data in files.items():
        if not path.startswith(prefix):
            continue
        target = directory / path[len(prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return directory


def _load_module(registry: Registry, name: str) -> None:
    base = registry.directory
    single, package = base / f"{name}.py", base / name / "__init__.py"
    origin = "the working copy" if registry.source == WORKTREE else PRIMARY_BRANCH
    if single.is_file() and package.is_file():
        raise ExtensionError(f"both {EXT_DIR}/{name}.py and {EXT_DIR}/{name}/__init__.py exist on {origin}")
    if not single.is_file() and not package.is_file():
        raise ExtensionError(f"no {EXT_DIR}/{name}.py or {EXT_DIR}/{name}/__init__.py on {origin}")
    with _no_bytecode():
        module = importlib.import_module(f"{registry.package}.{name}")
    hook = getattr(module, "register", None)
    if not callable(hook):
        raise ExtensionError("defines no register(api)")
    api = Api(name, kit_commands)
    try:
        hook(api)
    finally:
        api._open = False
    _merge(registry, name, api)


def _build(root: Path, source: str, files: dict[str, bytes], key: tuple) -> Registry | None:
    if DECLARATION not in files:
        return None
    declared = parse_declaration(files[DECLARATION], DECLARATION + (f" on {PRIMARY_BRANCH}" if source == "main"
                                                                    else " (working copy)"))
    registry = Registry(source=source, api=declared["api"], modules=declared["modules"], key=key)
    if not registry.modules:
        return registry
    registry.directory = _materialise(files)
    registry.package = f"_pk_ext_{secrets.token_hex(8)}"
    package = types.ModuleType(registry.package)
    package.__path__ = [str(registry.directory)]
    package.__package__ = registry.package
    sys.modules[registry.package] = package
    importlib.invalidate_caches()
    for name in registry.modules:
        try:
            _load_module(registry, name)
        except HOOK_FAILURES as exc:  # a module that does not load contributes nothing, and is named
            registry.errors.append(f"{name}: {describe(exc)}")
            registry.points.setdefault(name, [])
    return registry


_CURRENT: list[Registry] = []


def close() -> None:
    """Drop the loaded registry: its modules leave sys.modules and its private directory is removed."""
    while _CURRENT:
        registry = _CURRENT.pop()
        if registry.package:
            for name in [n for n in sys.modules if n == registry.package or n.startswith(registry.package + ".")]:
                del sys.modules[name]
        if registry.directory is not None:
            shutil.rmtree(registry.directory, ignore_errors=True)


atexit.register(close)


def load(root) -> Registry | None:
    """The extensions of the repository at `root`: None without a declaration (the kit as before).

    Read from main's committed tree, or from the working copy with PK_EXT_FROM=worktree. A declaration that does not
    read, or names another api, is a KitError: every pk command refuses. A module that does not load is listed in
    `errors`. The same sources give the same registry back without loading again.
    """
    root = Path(root).resolve()
    source = mode()
    files = _tree_files(root, MAIN_REF) if source == "main" else _worktree_files(root)
    if not files or DECLARATION not in files:
        return None
    key = (source, tuple(sorted((path, sha256_bytes(data)) for path, data in files.items())))
    if _CURRENT and _CURRENT[0].key == key:
        return _CURRENT[0]
    close()
    registry = _build(root, source, files, key)
    if registry is not None:
        _CURRENT.append(registry)
    return registry


def for_pk() -> Registry | None:
    """pk's start: the registry of the project around the current directory (None outside one, or without a
    declaration). Raises KitError on a declaration problem, so every command refuses. Without git nothing can be
    read and each command says so itself, as before; a point that needs the extensions then refuses (load raises)."""
    if shutil.which("git") is None:
        return None
    try:
        root = find_root()
    except KitError:
        return None
    return load(root)


def _not_loaded(registry: Registry | None, what: str) -> str | None:
    if registry is None or not registry.errors:
        return None
    return (f"{what} refuses: extension module(s) did not load from {registry.where()} ({'; '.join(registry.errors)});"
            " a module that does not load may hold a refusal, so nothing it guards runs (see: pk ext check)")


def load_problem(root, what: str) -> str | None:
    """Why a refusing point refuses before running anything: a module did not load (it may hold a refusal)."""
    return _not_loaded(load(root), what)


def problems(root) -> list[str]:
    """`pk doctor`'s lines: every module that did not load."""
    registry = load(root)
    if registry is None:
        return []
    return [f"extension {error} (loaded from {registry.where()})" for error in registry.errors]


# --- the points, as the kit calls them --------------------------------------------------------------

def run_command(registry: Registry, args: argparse.Namespace) -> int:
    module, handler, _, _ = registry.commands[args.command]
    try:
        code = handler(args)
    except KitError:
        raise
    except Exception as exc:
        raise KitError(f"extension command {args.command} ({module}) raised {describe(exc)}") from exc
    return code if isinstance(code, int) and not isinstance(code, bool) else (0 if code is None else 1)


def status_refusal(root, task_id: str, old: str, new: str, record: dict) -> str | None:
    """None when every status hook accepts; otherwise the refusal (also for a hook that raised, returned anything but
    None or a string, or a module that did not load)."""
    registry = load(root)  # once: the registry judged is the registry checked
    problem = _not_loaded(registry, f"{task_id}: the move {old} -> {new}")
    if registry is None or problem:
        return problem
    for module, fn in registry.status_hooks:
        try:
            said = fn(task_id, old, new, copy.deepcopy(record), Path(root))
        except HOOK_FAILURES as exc:
            return f"{task_id}: {old} -> {new} refused by extension {module}: on_status_transition raised {describe(exc)}"
        if said is None:
            continue
        if isinstance(said, str):
            return f"{task_id}: {old} -> {new} refused by extension {module}: {_one_line(said) or '(no reason given)'}"
        return (f"{task_id}: {old} -> {new} refused by extension {module}: on_status_transition returned "
                f"{type(said).__name__}, not None or a refusal string")
    return None


def after_worktree_new(root, task_id: str, path: Path) -> str | None:
    """None when every after-worktree hook ran; otherwise why the new worktree must go (also when a module no longer
    loads: main moved since the check before the worktree was made)."""
    registry = load(root)
    problem = _not_loaded(registry, f"pk worktree new {task_id}")
    if registry is None or problem:
        return problem
    for module, fn in registry.worktree_hooks:
        try:
            fn(task_id, Path(path), Path(root))
        except HOOK_FAILURES as exc:
            return f"extension {module}: after_worktree_new raised {describe(exc)}"
    return None


def steps_for(root, gate: str) -> list[tuple]:
    """(module, name, fn) of every gate step registered for `gate`, in declaration order.

    KitError when a module does not load now: main may have moved since the gate's first check, and a gate that ran
    no extension step because its module stopped loading must not pass (KX-1 fix round 1, M2)."""
    registry = load(root)
    problem = _not_loaded(registry, f"the {gate} gate's extension steps")
    if problem:
        raise KitError(problem)
    if registry is None:
        return []
    return [(module, name, fn) for module, name, fn, gates in registry.gate_steps if gate in gates]


def _flush_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        with contextlib.suppress(Exception):
            if stream is not None:
                stream.flush()


@contextlib.contextmanager
def _descriptors_into(sink):
    """File descriptors 1 and 2 point at `sink` while the block runs, so a child process a gate step starts (which
    writes to the descriptors, past sys.stdout) lands in the step's lines, never bare in the gate's output. A
    descriptor that cannot be duplicated (closed) is left as it is."""
    _flush_standard_streams()
    saved = []
    try:
        for fd in (1, 2):
            with contextlib.suppress(OSError):
                saved.append((fd, os.dup(fd)))
        for fd, _ in saved:
            os.dup2(sink.fileno(), fd)
        yield
    finally:
        _flush_standard_streams()
        for fd, kept in saved:
            os.dup2(kept, fd)
            os.close(kept)


def _call_captured(fn, arg, timeout: float) -> tuple[dict, list[str], bool]:
    """Call `fn(arg)` in a thread under `timeout` seconds: (box, printed, timed_out). `box` holds "value" or "error"
    (an exception or a sys.exit, HOOK_FAILURES); `printed` is what it printed, and what a child process it started
    wrote to the standard descriptors, while it ran."""
    box: dict = {}
    captured = io.StringIO()

    def target():
        try:
            box["value"] = fn(arg)
        except HOOK_FAILURES as exc:  # a hook's SystemExit is a failure too, never the gate's exit
            box["error"] = exc

    worker = threading.Thread(target=target, name="pk-ext-gate-hook", daemon=True)
    with tempfile.TemporaryFile() as sink:
        with _descriptors_into(sink), contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            worker.start()
            worker.join(timeout)
        sink.seek(0)
        children = sink.read().decode("utf-8", "replace")
    return box, captured.getvalue().splitlines() + children.splitlines(), worker.is_alive()


def run_step(fn, ctx: GateContext, timeout: float) -> tuple[bool, list[str]]:
    """Run one gate step under `timeout` seconds: (ok, lines). Its own printing, and what a child process it starts
    writes to the standard descriptors while it runs, are captured into the lines.

    Only `(True, [str, ...])` passes: an exception, a sys.exit, a timeout or any other return fails the step, naming
    why. After a timeout the step's thread is abandoned but keeps running (Python cannot stop it): what it prints
    afterwards reaches the gate's streams unprefixed, after the gate's FAILED line, and its child processes run on.
    """
    box, printed, timed_out = _call_captured(fn, ctx, timeout)
    if timed_out:
        return False, [*printed, f"timed out after {timeout:g}s (the step's thread is abandoned)"]
    if "error" in box:
        return False, [*printed, f"raised {describe(box['error'])}"]
    value = box.get("value")
    if not (isinstance(value, (tuple, list)) and len(value) == 2 and isinstance(value[0], bool)
            and isinstance(value[1], (list, tuple)) and all(isinstance(line, str) for line in value[1])):
        return False, [*printed, f"returned {type(value).__name__}, not (ok, lines) with ok True or False and lines "
                                 "a list of text"]
    return value[0], [*printed, *value[1]]


# --- prepare_tree (0.7.1) ----------------------------------------------------------------------------

def tree_state(tree) -> dict[str, tuple]:
    """{path: (status, content)} of `git status --porcelain --untracked-files=all` in `tree`, ignored files excluded.

    `content` is the sha256 of what is in the tree now (a symlink's target, "missing" for a deleted path) for every
    entry whose working-tree side differs from the index, so a second write to a path git already lists as changed is
    a difference too; None when the working tree matches the index. KitError when git cannot say.
    """
    tree = Path(tree)
    listed = _git(tree, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
    if listed.returncode != 0:
        said = listed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise KitError(f"cannot read the status of {tree}: {said[0] if said else f'exit {listed.returncode}'}")
    state = {}
    for entry in listed.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        content = None
        if code[1] != " ":
            target = tree / path
            if target.is_symlink():
                content = "link:" + sha256_bytes(os.fsencode(os.readlink(target)))
            elif target.is_file():
                content = sha256_bytes(target.read_bytes())
            else:
                content = "missing" if not target.exists() else "other"
        state[path] = (code, content)
    return state


def _changed(before: dict, after: dict) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _named(paths: list[str]) -> str:
    shown = ", ".join(paths[:SHOWN_PATHS])
    return shown + (f" and {len(paths) - SHOWN_PATHS} more" if len(paths) > SHOWN_PATHS else "")


def _link_hint(tree: Path, paths: list[str]) -> str:
    links = [path for path in paths if (tree / path).is_symlink()]
    if not links:
        return ""
    return (f" ({_named(links)}: a symlink is a file to git, so an ignore pattern with a trailing slash, such as "
            "`deps/`, does not match it; ignore it without the slash)")


def prepare_tree(root, tree, purpose: str, head: str, timeout: float) -> list[tuple[str, str | None, list[str]]]:
    """Run every registered tree preparer in `tree`, in declaration order: [(module, problem, lines)], stopping at
    the first problem (a refusal of the gate). [] without extensions or preparers: nothing runs and nothing changes.

    A preparer passes when it returns None within `timeout` seconds and the tree's `git status --porcelain
    --untracked-files=all` (ignored files excluded, `tree_state`) is the same after the call as before it: it may
    write only git-ignored paths. An exception, a sys.exit, a timeout, any other return or a changed path is the
    problem, naming the reason or the paths. KitError when PK_EXT_FROM is set or a module did not load (it may hold a
    preparer the gate needs).
    """
    if purpose not in PURPOSES:
        raise KitError(f"prepare_tree: unknown purpose {purpose!r} (one of {', '.join(PURPOSES)})")
    what = f"the {purpose} tree's prepare_tree"
    registry = load(root)
    if registry is None:
        return []
    problem = development_refusal(what) or _not_loaded(registry, what)
    if problem:
        raise KitError(problem)
    tree = Path(tree)
    ctx = TreeContext(root=Path(root), tree=tree, purpose=purpose, head=head)
    results = []
    for module, fn in registry.tree_hooks:
        before = tree_state(tree)
        box, printed, timed_out = _call_captured(fn, ctx, timeout)
        changed = _changed(before, tree_state(tree))
        reasons = []
        if timed_out:
            reasons.append(f"timed out after {timeout:g}s (its thread is abandoned)")
        elif "error" in box:
            reasons.append(f"raised {describe(box['error'])}")
        elif box.get("value") is not None:
            said = box["value"]
            reasons.append(f"returned {_one_line(said) if isinstance(said, str) else type(said).__name__}, not None")
        if changed:
            reasons.append(f"changed what git sees in the tree, where only git-ignored paths may be written: "
                           f"{_named(changed)}{_link_hint(tree, changed)}")
        problem = "; ".join(reasons) or None
        results.append((module, problem, printed))
        if problem:
            break
    return results


def drift_results(root) -> tuple[list[tuple[str, bool, str]], list[tuple[str, str]]]:
    """(checks, notes) for `pk drift`: one (code, ok, detail) per drift check; a finding of severity note, a check
    that raised or returned the wrong shape, an informational hook that raised and a module that did not load are
    notes (code, text), never drift."""
    registry = load(root)
    if registry is None:
        return [], []
    checks, notes = [], [("ext", f"did not load: {error}") for error in registry.errors]
    for module, code, fn in registry.drift_checks:
        try:
            findings = fn(Path(root))
            bad = not isinstance(findings, list) or not all(
                isinstance(f, dict) and set(f) == {"severity", "text"} and f["severity"] in SEVERITIES
                and isinstance(f["text"], str) for f in findings)
        except HOOK_FAILURES as exc:
            notes.append((code, f"extension {module} raised {describe(exc)}"))
            continue
        if bad:
            notes.append((code, f"extension {module} returned {type(findings).__name__}, not a list of "
                                "{severity: note|fail, text}"))
            continue
        failed = [_one_line(f["text"]) for f in findings if f["severity"] == "fail"]
        notes += [(code, _one_line(f["text"])) for f in findings if f["severity"] == "note"]
        checks.append((code, not failed, "; ".join(failed) if failed else f"{module}: no failing finding"))
    for text in _probe(root, registry):
        notes.append(("ext", text))
    return checks, notes


def _probe(root, registry: Registry) -> list[str]:
    """The hand-off and dashboard hooks, called once each so `pk drift` notes one that fails."""
    found = []
    for module, fn in registry.handoff:
        problem = _handoff_problem(module, fn, root)[1]
        if problem:
            found.append(problem)
    for module, fn in registry.dashboard:
        problem = _dashboard_problem(module, fn, root)[1]
        if problem:
            found.append(problem)
    return found


def _handoff_problem(module: str, fn, root) -> tuple[list[str], str | None]:
    try:
        lines = fn(Path(root))
    except HOOK_FAILURES as exc:
        return [], f"extension {module}: handoff_lines raised {describe(exc)}"
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return [], f"extension {module}: handoff_lines returned {type(lines).__name__}, not a list of text"
    return lines, None


def handoff_lines(root) -> list[str]:
    """The hand-off's extra Now lines, in declaration order; a hook that fails is a note and adds nothing."""
    registry = load(root)
    if registry is None:
        return []
    for error in registry.errors:
        note(f"extension did not load, its hand-off lines are missing: {error}")
    lines = []
    for module, fn in registry.handoff:
        found, problem = _handoff_problem(module, fn, root)
        if problem:
            note(problem)
        lines += found
    return lines


def _dashboard_problem(module: str, fn, root) -> tuple[dict | None, str | None]:
    try:
        data = fn(Path(root))
        if not isinstance(data, dict):
            return None, f"extension {module}: dashboard_data returned {type(data).__name__}, not a dict"
        json.dumps(data, sort_keys=True)
    except HOOK_FAILURES as exc:
        return None, f"extension {module}: dashboard_data failed: {describe(exc)}"
    return data, None


def dashboard_data(root) -> dict:
    """{module: data} for the dashboard; a hook that fails is a note and is left out."""
    registry = load(root)
    if registry is None:
        return {}
    for error in registry.errors:
        note(f"extension did not load, its dashboard data is missing: {error}")
    found = {}
    for module, fn in registry.dashboard:
        data, problem = _dashboard_problem(module, fn, root)
        if problem:
            note(problem)
        else:
            found[module] = data
    return found


def resolve_reference(root, path: str, data: bytes) -> dict | None:
    """How the first resolver that answers describes a legacy reference ({kind, task_id?, decision?, summary}), for
    display only; None when none answers. A resolver that raises or answers in the wrong shape is a note."""
    registry = load(root)
    if registry is None:
        return None
    for module, fn in registry.resolvers:
        try:
            said = fn(path, data)
        except HOOK_FAILURES as exc:
            note(f"extension {module}: reference_resolver raised {describe(exc)} for {path}")
            continue
        if said is None:
            continue
        if not (isinstance(said, dict) and set(said) <= set(RESOLVED_KEYS) and {"kind", "summary"} <= set(said)
                and all(isinstance(value, str) for value in said.values())):
            note(f"extension {module}: reference_resolver answered {type(said).__name__} for {path}, not "
                 "{kind, task_id?, decision?, summary} of text")
            continue
        return dict(said)
    return None


def run_importer(root, name: str, argv: list[str]) -> dict:
    """The adoption source an extension's importer builds; KitError when there is none of that name, a module did
    not load, or the importer fails."""
    registry = load(root)
    if registry is None:
        raise KitError(f"pk adopt --from {name}: no {DECLARATION} on {PRIMARY_BRANCH}, so there is no importer")
    problem = _not_loaded(registry, f"pk adopt --from {name}")
    if problem:
        raise KitError(problem)
    if name not in registry.importers:
        known = ", ".join(sorted(registry.importers)) or "none"
        raise KitError(f"pk adopt --from {name}: no extension registers that importer (registered: {known})")
    module, fn = registry.importers[name]
    try:
        source = fn(Path(root), list(argv))
    except HOOK_FAILURES as exc:
        raise KitError(f"pk adopt --from {name}: the importer of extension {module} raised {describe(exc)}; "
                       "nothing written") from exc
    if not isinstance(source, dict):
        raise KitError(f"pk adopt --from {name}: the importer of extension {module} returned "
                       f"{type(source).__name__}, not a source dict; nothing written")
    return source


# --- pk ext ----------------------------------------------------------------------------------------

def _listing(registry: Registry) -> list[str]:
    lines = [f"extensions: api {registry.api}, {len(registry.modules)} module(s) from {registry.where()}"]
    failed = {error.split(":", 1)[0]: error.split(":", 1)[1].strip() for error in registry.errors}
    for name in registry.modules:
        if name in failed:
            lines.append(f"  {name}: did not load: {failed[name]}")
        else:
            lines.append(f"  {name}: " + ("; ".join(registry.points.get(name, [])) or "registers nothing"))
    return lines


def cmd_ext(args: argparse.Namespace) -> int:
    action = getattr(args, "ext_action", None)
    if action not in ("list", "check"):
        raise KitError("ext needs an action: list or check")
    root = find_root()
    registry = load(root)
    if registry is None:
        where = f"on {PRIMARY_BRANCH}" if mode() == "main" else "in the working copy"
        print(f"extensions: none (no {DECLARATION} {where})")
        return 0
    if action == "list":
        print("\n".join(_listing(registry)))
        return 0
    for error in registry.errors:
        print(f"error: extension {error}", file=sys.stderr)
    if registry.errors:
        return 1
    print(f"ext check: ok, api {registry.api}, {len(registry.modules)} module(s) loaded from {registry.where()} "
          "(nothing run)")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="ext_action", metavar="action", parser_class=type(parser))
    actions.add_parser("list", help="each extension module and the points it registered")
    actions.add_parser("check", help="load and validate every extension module without running any of its hooks")


def register(add_command) -> None:
    add_command("ext", cmd_ext, help="the programme's extensions (program/extensions.yaml): list, check",
                configure=_configure)
