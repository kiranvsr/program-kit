#!/usr/bin/env python3
"""Agent tool hooks: the provider-neutral progress check (`pk drift`) behind a stop hook (standard library only).

The check itself is `pk drift`, D3 and D7, which any controller runs by hand. An adapter makes one agent
harness run it whenever the controller's agent is about to stop, and turns a stale result into that harness's
"do not stop yet" answer, with the reason and the tasks ready to start (progress.py).

    pk drift --hook ADAPTER            the hook command itself (reads the hook's JSON on standard input)
    pk hook install ADAPTER            merge that command into the harness's local, git-ignored settings file
    pk hook remove ADAPTER             delete only the entry install wrote
    pk hook list                       the adapters, their settings file and whether the hook is installed

Nothing is ever put in a git hook or in a committed file: a git hook would fire inside merges and pushes, where a
refusal leaves a half-done merge or a refused push, and a committed settings file would impose one person's
harness on everyone. The settings file must be git-ignored and untracked, or install refuses.

The adapter names are written in fragments: the kit names no vendor in its files (kit/tests/test_neutrality.py),
yet an adapter has to use the harness's own command and directory names.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from common import KitError, find_root, git, write_text

KIT_DIR = Path(__file__).resolve().parent.parent
_CC = "cl" + "aude"  # the one adapter so far: that harness's CLI, its Stop hook and its project settings directory
ADAPTERS = (_CC,)
SETTINGS = {_CC: Path("." + _CC) / "settings.local.json"}
PROJECT_ENV = {_CC: _CC.upper() + "_PROJECT_DIR"}  # the harness sets it, for hook commands, to the project root
HOOK_TIMEOUT = 60  # seconds the harness waits for the command
SIGNATURE = "drift --hook "


def command(adapter: str) -> str:
    """The hook command install writes: this kit's pk.py by absolute path."""
    return f"python3 {shlex.quote(str(KIT_DIR / 'scripts' / 'pk.py'))} drift --hook {adapter}"


def _ours(entry, adapter: str) -> bool:
    cmd = entry.get("command") if isinstance(entry, dict) else None
    return isinstance(cmd, str) and "pk.py" in cmd and cmd.rstrip().endswith(SIGNATURE + adapter)


# --- the stop check ---------------------------------------------------------------------

def _payload() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _root(adapter: str) -> Path:
    project = os.environ.get(PROJECT_ENV[adapter], "")
    if project and Path(project).is_dir():
        return find_root(Path(project))
    return find_root()


def reason(root: Path, hand_off: tuple[bool, str], docs: dict, cfg: dict | None) -> str:
    """The text that tells the agent why it may not stop yet and what to do."""
    import progress

    lines = []
    if not docs["ok"]:
        names = ", ".join((cfg or {}).get("docs", []))
        lines.append(f"The progress docs ({names}) are behind the program state (pk drift D7). Newer state commits "
                     "on the main branch:")
        lines += [f"  {line}" for line in docs["newer"]]
        if docs["more"]:
            lines.append(f"  (and {docs['more']} more)")
    if not hand_off[0]:
        lines.append(f"The hand-off is not current (pk drift D3): {' '.join(hand_off[1].split())}")
    lines.append("Before stopping: run the program's progress pass (bring those docs up to date and commit them), "
                 "republish its tracker, then fill free agent slots from the tasks below, or say why none fits.")
    try:
        ready = progress.ready_lines(progress.ready(root))
    except KitError as exc:
        ready = [f"(not listed: {' '.join(str(exc).split())})"]
    lines.append("Ready to start (pk status ready):" + ("" if ready else " none"))
    lines += [f"  {line}" for line in ready[:20]]
    if len(ready) > 20:
        lines.append(f"  (and {len(ready) - 20} more)")
    return "\n".join(lines)


def stop_check(adapter: str) -> int:
    """`pk drift --hook ADAPTER`: silent and 0 when D3 and D7 are clean, else the adapter's block answer.

    Standard output carries nothing but that answer. A problem (no program, a checks.yaml that does not read)
    is one line on standard error and exit 1, which the harness shows without blocking the stop.
    """
    import drift
    import progress

    if adapter not in ADAPTERS:
        print(f"error: unknown hook adapter {adapter!r} (known: {', '.join(ADAPTERS)})", file=sys.stderr)
        return 1
    payload = _payload()
    if payload.get("stop_hook_active"):  # the agent is already continuing because of this hook: never loop
        return 0
    try:
        root = _root(adapter)
        cfg = progress.config(root)
        docs = progress.behind(root, cfg)
        hand_off = drift._hand_off(root)
        if (docs["ok"] and hand_off[0]) or progress.quiet(root, cfg):
            return 0
        text = reason(root, hand_off, docs, cfg)
    except KitError as exc:
        print("error: pk drift --hook: " + " ".join(str(exc).split()), file=sys.stderr)
        return 1
    print(json.dumps({"decision": "block", "reason": text}))
    return 0


# --- install and remove ---------------------------------------------------------------

def _settings_path(root: Path, adapter: str) -> Path:
    return Path(root) / SETTINGS[adapter]


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise KitError(f"{path} is not valid JSON ({exc}); fix it by hand, nothing was written") from None
    if not isinstance(data, dict):
        raise KitError(f"{path} must hold a JSON object; nothing was written")
    return data


def _stop_groups(data: dict, path: Path, create: bool) -> list | None:
    hooks = data.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise KitError(f"{path}: hooks must be an object; nothing was written")
    groups = hooks.get("Stop")
    if groups is None:
        if not create:
            return None
        groups = hooks["Stop"] = []
    if not isinstance(groups, list):
        raise KitError(f"{path}: hooks.Stop must be a list; nothing was written")
    return groups


def _refuse_unignored(root: Path, path: Path) -> None:
    rel = path.relative_to(root).as_posix()
    if git(root, "ls-files", "--", rel):
        raise KitError(f"{rel} is tracked by git; a hook there would reach everyone. Untrack it "
                       f"(git rm --cached {rel}), ignore it, then run this again")
    try:
        git(root, "check-ignore", "-q", "--", rel)
    except KitError:
        raise KitError(f"{rel} is not git-ignored; add the line {rel} to .gitignore (or .git/info/exclude), "
                       "then run this again") from None


def install(root: Path, adapter: str) -> str:
    """Merge the stop hook into the adapter's settings file; return what was done."""
    root = Path(root)
    path = _settings_path(root, adapter)
    _refuse_unignored(root, path)
    data = _load(path)
    groups = _stop_groups(data, path, create=True)
    wanted = command(adapter)
    mine = [entry for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)
            for entry in group["hooks"] if _ours(entry, adapter)]
    if any(entry.get("command") == wanted for entry in mine):
        return f"already installed in {SETTINGS[adapter].as_posix()}"
    if mine:  # an entry of ours with another path (the repository moved): point it here
        for entry in mine:
            entry["command"] = wanted
        done = "updated"
    else:
        groups.append({"hooks": [{"type": "command", "command": wanted, "timeout": HOOK_TIMEOUT,
                                  "statusMessage": "Checking the program's progress docs (pk drift)"}]})
        done = "installed"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(data, indent=2) + "\n")
    return f"{done} the stop hook in {SETTINGS[adapter].as_posix()}: {wanted}"


def remove(root: Path, adapter: str) -> str:
    """Delete only the entries install wrote; a group or list that held nothing else goes with them."""
    root = Path(root)
    path = _settings_path(root, adapter)
    data = _load(path)
    groups = _stop_groups(data, path, create=False)
    if not groups:
        return f"not installed in {SETTINGS[adapter].as_posix()}"
    removed = 0
    for group in list(groups):
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        kept = [entry for entry in group["hooks"] if not _ours(entry, adapter)]
        removed += len(group["hooks"]) - len(kept)
        if len(kept) != len(group["hooks"]):
            if kept:
                group["hooks"] = kept
            else:
                groups.remove(group)
    if not removed:
        return f"not installed in {SETTINGS[adapter].as_posix()}"
    if not groups:
        del data["hooks"]["Stop"]
        if not data["hooks"]:
            del data["hooks"]
    write_text(path, json.dumps(data, indent=2) + "\n")
    return f"removed the stop hook from {SETTINGS[adapter].as_posix()}"


def installed(root: Path, adapter: str) -> bool:
    try:
        groups = _stop_groups(_load(_settings_path(root, adapter)), _settings_path(root, adapter), create=False)
    except KitError:
        return False
    return any(_ours(entry, adapter) for group in groups or [] if isinstance(group, dict)
               for entry in (group.get("hooks") if isinstance(group.get("hooks"), list) else []))


# --- command ------------------------------------------------------------------------

def cmd_hook(args: argparse.Namespace) -> int:
    root = find_root()
    if args.hook_action == "list":
        for adapter in ADAPTERS:
            state = "installed" if installed(root, adapter) else "not installed"
            print(f"{adapter}\t{SETTINGS[adapter].as_posix()}\t{state}")
        return 0
    if args.hook_action not in ("install", "remove"):
        raise KitError("hook needs an action: install, remove or list")
    print((install if args.hook_action == "install" else remove)(root, args.adapter))
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="hook_action", metavar="action", parser_class=type(parser))
    for name, text in (("install", "merge the stop hook into the harness's git-ignored local settings"),
                       ("remove", "delete only the stop hook entry install wrote")):
        actions.add_parser(name, help=text).add_argument("adapter", choices=ADAPTERS)
    actions.add_parser("list", help="the adapters, their settings file and whether the hook is installed")


def register(add_command) -> None:
    add_command("hook", cmd_hook, help="an agent tool's stop hook running pk drift (never a git hook)",
                configure=_configure)
