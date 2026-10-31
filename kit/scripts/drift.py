#!/usr/bin/env python3
"""Program drift: do the checked-in documents still agree with their generators, approvals and code (standard library only).

`pk drift [--rev REV] [--strict]` reports and never fixes. It checks one commit (default HEAD) in a temporary
detached worktree, so nothing in the checkout is written, and removes that worktree when it is done.

  D1 regeneration is a no-op: the commands of the optional `drift.regenerate` list in `program/checks.yaml`, then,
     once stage 6 is approved, the kit's own `pk trace sync` and `pk briefs check`, leave the tree as committed.
     A changed YAML file counts only when its parsed data changed (generators and the kit requote YAML).
  D2 approvals: `pk interview next` names no approved stage whose files changed (skipped without a ledger).
  D3 hand-off: `pk handoff check`, run on the real checkout before the temporary worktree exists, and compared
     without the live worktree lines (KF-14); skipped when REV is not the checkout's HEAD.
  D4 docs vs migrations: with the optional `drift.docs_vs_migrations` pair {glob, doc}, every file the glob
     matches is named in the document, by its name, its stem or its leading number.
  D5 QA evidence: every task whose status is `done` has an acceptance record.
  D6 validators: `pk register validate`, `pk status validate` and `pk trace validate` at the highest approved stage.
  D7 progress docs behind state: with the optional `progress` section ({docs, state}), no commit on the main branch
     touching a state path comes after the oldest of the docs' newest commits (progress.py); skipped without it.
     Like D3 it reads the real checkout and the main branch, whatever REV is.

After the checks it prints, as information and never as drift, the tasks ready to start (`pk status ready`).
Drift is information: the command exits 0 unless `--strict` is given and a check drifts (for a scheduled run).
Different from `pk stale`, which compares one task's evidence with the tree.

`pk drift --hook <adapter>` is the adapter for an agent tool's stop hook (hook.py names the adapters): it runs D3 and D7 only, on
the checkout, and prints nothing when both are clean; otherwise it prints the harness's stop-hook answer, which
blocks the agent's stop with the reason. A problem is one line on stderr and exit 1, which never blocks.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from common import KitError, find_root, git, read_json, read_yaml, run_argv

KEYS = ("regenerate", "docs_vs_migrations")
PAIR_KEYS = ("glob", "doc")
REGENERATE_TIMEOUT = 1800  # seconds per command
FIRST_STAGE, REGISTER_STAGE = 1, 6


def _one_line(text) -> str:
    return " ".join(str(text).split())


def _relative(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KitError(f"checks.yaml: drift.docs_vs_migrations needs glob and doc, each a path ({what} is not)")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise KitError(f"checks.yaml: drift.docs_vs_migrations.{what} must be a relative path inside the repository")
    return value


def parse(value) -> dict:
    """The `drift` section of checks.yaml as {regenerate: [argv], docs_vs_migrations: {glob, doc} or None}."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise KitError("checks.yaml: drift must be a mapping of regenerate and docs_vs_migrations")
    unknown = sorted(str(key) for key in value if key not in KEYS)
    if unknown:
        raise KitError(f"checks.yaml: drift: unknown key {', '.join(unknown)} (expected {', '.join(KEYS)})")
    commands = value.get("regenerate")
    if commands is None:
        commands = []
    if not isinstance(commands, list) or not all(
            isinstance(item, list) and item and all(isinstance(part, str) and part for part in item)
            for item in commands):
        raise KitError("checks.yaml: drift.regenerate must be a list of commands, each a non-empty argv list of "
                       "strings, not a shell string")
    pair = value.get("docs_vs_migrations")
    if pair is not None:
        if not isinstance(pair, dict) or sorted(pair) != sorted(PAIR_KEYS):
            raise KitError("checks.yaml: drift.docs_vs_migrations needs glob and doc, and nothing else")
        pair = {key: _relative(pair[key], key) for key in PAIR_KEYS}
    return {"regenerate": [list(item) for item in commands], "docs_vs_migrations": pair}


# --- the checks ---------------------------------------------------------------------

def _approved_stage(root: Path) -> int:
    """The highest stage 1 to 6 with an approval file, 0 when none."""
    folder = root / "program" / "approvals"
    return max((n for n in range(FIRST_STAGE, REGISTER_STAGE + 1) if (folder / f"stage-{n}.json").is_file()),
               default=0)


def _first(problems: list[str]) -> str:
    return _one_line(problems[0]) + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else "")


def _hand_off(root: Path) -> tuple[bool, str]:
    import handoff

    try:
        if not handoff.check(root):
            if handoff.never_generated(root):
                return False, "no generated block yet; run: pk handoff update"
            return False, "program/HANDOFF.md is stale; run: pk handoff update"
        problems = handoff.budget_problems(root)
    except KitError as exc:
        return False, _one_line(exc)
    return (False, "; ".join(problems)) if problems else (True, "current (live worktree lines not compared)")


def _approvals(root: Path) -> tuple[bool, str]:
    if not (root / "program" / "ledger.yaml").is_file():
        return True, "no ledger: no stage approved yet"
    import interview

    try:
        stage = interview.next_stage(root)
        changed = interview._changed_since_approval(root, stage)
    except KitError as exc:
        return False, _one_line(exc)
    if changed:
        return False, f"stage {stage} was approved and its files changed: {', '.join(changed)}"
    return True, f"approvals current; next stage {stage}"


def _named(name: str, text: str) -> bool:
    number = re.match(r"(\d+)[_.-]", name)
    return name in text or Path(name).stem in text or bool(number and number.group(1) in text)


def _docs(root: Path, pair: dict | None) -> tuple[bool, str]:
    if pair is None:
        return True, "not configured (drift.docs_vs_migrations in program/checks.yaml)"
    files = sorted(path.name for path in root.glob(pair["glob"]) if path.is_file())
    doc = root / pair["doc"]
    if not doc.is_file():
        return False, f"{len(files)} files match {pair['glob']}; {pair['doc']} does not exist"
    text = doc.read_text(encoding="utf-8", errors="replace")
    missing = [name for name in files if not _named(name, text)]
    return not missing, (f"{len(files)} files match {pair['glob']}; not named in {pair['doc']}: "
                         f"{', '.join(missing) or 'none'}")


def _evidence(root: Path) -> tuple[bool, str]:
    lacking, done = [], 0
    for path in sorted((root / "program" / "status").glob("*.json")):
        try:
            record = read_json(path)
        except KitError:
            continue  # D6 reports a record that does not read
        if not isinstance(record, dict) or record.get("status") != "done":
            continue
        done += 1
        task = record.get("task_id") if isinstance(record.get("task_id"), str) else path.stem
        if not any((root / "program" / "evidence" / task / "acceptance").glob("*.json")):
            lacking.append(task)
    if lacking:
        return False, f"done without an acceptance record: {', '.join(lacking)}"
    return True, f"every done task has an acceptance record ({done} done)"


def _validators(root: Path, stage: int) -> tuple[bool, str]:
    import register as register_module
    import status as status_module
    import trace as trace_module

    bad, ran = [], []
    checks = [("status", lambda: status_module.validate(root))]
    if (root / "program" / "register.yaml").is_file():
        checks.insert(0, ("register", lambda: register_module.problems_in(root)))
    if stage and (root / "program" / "traceability.yaml").is_file():
        checks.append((f"trace at stage {stage}", lambda: trace_module.validate(root, stage)))
    for name, run in checks:
        ran.append(name)
        try:
            problems = run()
        except KitError as exc:
            problems = [str(exc)]
        if problems:
            bad.append(f"{name}: {_first(problems)}")
    return not bad, "; ".join(bad) or f"{', '.join(ran)} valid"


def _changed(root: Path, sha: str) -> list[str]:
    """Paths the regeneration changed, added or removed; a YAML file only when its parsed data differs."""
    import miniyaml

    proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all"],
                          capture_output=True)
    if proc.returncode != 0:
        raise KitError("drift: git status failed in the temporary worktree")
    entries = proc.stdout.decode("utf-8", errors="replace").split("\0")
    paths, skip = [], False
    for entry in entries:
        if skip or not entry:
            skip = False
            continue
        code, path = entry[:2], entry[3:]
        skip = "R" in code or "C" in code  # the next entry is the old name
        paths.append(path)
    changed = []
    for path in sorted(set(paths)):
        if path.endswith((".yaml", ".yml")) and (root / path).is_file():
            try:
                before = miniyaml.loads(git(root, "show", f"{sha}:{path}"))
                if read_yaml(root / path) == before:
                    continue
            except (KitError, miniyaml.YAMLError, RecursionError):
                pass  # new, or not YAML the kit reads: the change counts
        changed.append(path)
    return changed


def _regeneration(root: Path, sha: str, commands: list[list[str]], stage: int) -> tuple[bool, str]:
    import briefs
    import trace as trace_module

    failures = []
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for argv in commands:
        code, _ = run_argv(argv, root, env=env, timeout=REGENERATE_TIMEOUT)
        if code != 0:
            failures.append(f"{' '.join(argv)} exits {code}")
    kit = stage >= REGISTER_STAGE
    if kit:
        try:
            trace_module.sync(root)
            problems = briefs.check(root)
        except KitError as exc:
            problems = [str(exc)]
        if problems:
            failures.append(f"briefs: {_first(problems)}")
    changed = _changed(root, sha)
    ran = f"{len(commands)} command(s)" + (", trace sync and briefs check" if kit else "")
    if not failures and not changed:
        return True, f"regeneration is a no-op ({ran})"
    return False, "; ".join(([f"regeneration changes {', '.join(changed)}"] if changed else []) + failures)


def report(root: Path, rev: str = "HEAD") -> tuple[str, list[tuple[str, bool, str]]]:
    """(sha, [(check id, ok, detail)]) for commit `rev`; writes nothing in the checkout."""
    root = Path(root)
    if not rev or rev.startswith("-"):
        raise KitError(f"drift: {_one_line(rev) or 'an empty revision'} is not a commit")
    try:
        sha = git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    except KitError:
        raise KitError(f"drift: {_one_line(rev)} is not a commit") from None
    try:
        head = git(root, "rev-parse", "HEAD")
    except KitError:
        head = ""
    # D3 first, on the real checkout: the temporary worktree below would otherwise be listed in the hand-off
    hand_off = _hand_off(root) if sha == head else (True, "skipped: not the checkout's HEAD")
    progress_docs = _progress(root)
    with tempfile.TemporaryDirectory(prefix="pk-drift-") as tmp:
        tree = Path(tmp) / "tree"
        git(root, "worktree", "add", "--detach", "--quiet", str(tree), sha)
        try:
            checks = tree / "program" / "checks.yaml"
            data = read_yaml(checks) if checks.is_file() else None
            config = parse(data.get("drift") if isinstance(data, dict) else None)
            stage = _approved_stage(tree)
            found = {"D2": _approvals(tree), "D3": hand_off, "D4": _docs(tree, config["docs_vs_migrations"]),
                     "D5": _evidence(tree), "D6": _validators(tree, stage)}
            found["D1"] = _regeneration(tree, sha, config["regenerate"], stage)  # last: it writes into the tree
        finally:
            subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(tree)], capture_output=True)
            subprocess.run(["git", "-C", str(root), "worktree", "prune"], capture_output=True)
    found["D7"] = progress_docs
    return sha, [(cid, *found[cid]) for cid in ("D1", "D2", "D3", "D4", "D5", "D6", "D7")]


def _progress(root: Path) -> tuple[bool, str]:
    import progress

    try:
        result = progress.behind(root, progress.config(root))
    except KitError as exc:
        return False, _one_line(exc)
    return result["ok"], result["detail"]


def _ready_lines(root: Path) -> list[str]:
    import progress

    try:
        return progress.ready_lines(progress.ready(root))
    except KitError as exc:
        return [f"not listed: {_one_line(exc)}"]


# --- command ------------------------------------------------------------------------

def cmd_drift(args: argparse.Namespace) -> int:
    if args.hook is not None:
        import hook

        return hook.stop_check(args.hook)
    started = time.monotonic()
    root = find_root()
    sha, results = report(root, args.rev)
    print(f"drift report at {sha[:12]} ({time.monotonic() - started:.0f} s)")
    for cid, ok, detail in results:
        print(f"  {cid} {'ok' if ok else 'DRIFT':5} {_one_line(detail)}")
    lines = _ready_lines(root)
    print(f"  ready to start (information, not a check): {len(lines) or 'none'}")
    for line in lines:
        print(f"    {line}")
    drifted = sum(1 for _, ok, _ in results if not ok)
    print(f"  {drifted} of {len(results)} checks drift")
    return 1 if args.strict and drifted else 0


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rev", default="HEAD", metavar="REV", help="the commit to check (default HEAD)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any check drifts")
    import hook

    parser.add_argument("--hook", choices=hook.ADAPTERS, metavar="ADAPTER",
                        help="run as an agent stop hook (D3 and D7 only; reads the hook's JSON on stdin): "
                             + ", ".join(hook.ADAPTERS))


def register(add_command) -> None:
    add_command("drift", cmd_drift, help="report-only: do the documents still agree with their generators, "
                "approvals and code, and are the progress docs behind state (D1 to D7)", configure=_configure)
