#!/usr/bin/env python3
"""Progress docs behind state, and the tasks ready to start (standard library only).

The optional `progress` section of `program/checks.yaml` names the documents a controller keeps current by hand
(`docs`) and the paths whose commits change the program's state (`state`):

    progress:
      docs: [program/DASHBOARD.md, program/SUCCESSOR_PROMPT.md]
      state: [program/status, program/rulings.md, program/controller-state.json]
      quiet_when: some-shell-command      # optional; exit 0 keeps the agent hook silent

`behind` is check D7 of `pk drift`: the docs are behind when a commit on the main branch touching a `state` path
comes after the oldest of the docs' newest commits (the commit whose doc was updated longest ago), judged by
history, not by the clock: a task branch's state commit merged after the docs counts too. Without the section the
check is skipped. `ready` lists the not-started register tasks whose dependencies are all done or
merged, and those waiting on exactly one, leaving out any task that already has a worktree or a `task/<ID>` or
`wip/<ID>...` branch. Both read git and the checkout and write nothing.

`pk progress` (0.6.0) is the controller's progress pass in one call, so D7 is cleared the same way every time. It
REPLACES current-state text, never appends to it, in the `docs` and the hand-written part of `program/HANDOFF.md`:
`--row NAME=TEXT` (the second cell of the table row whose first cell is NAME), `--bullet KEY=TEXT` (`- **KEY:** ...`),
`--item "HEADING#N=TEXT"` (numbered item N under the heading), `--list "HEADING=FILE"` (the numbered list under it)
and `--history TEXT` (one dated line at the top of the list under `history: {doc, heading}`). Each target must
exist exactly once; every edit is made in memory first, so a refusal writes nothing. Every `Latest ruling R<n>
(next R<n+1>)` phrase follows `program/rulings.md`. Then `pk handoff update` and `pk handoff check`, `pk dashboard OUT`
with `--dashboard OUT`, the `scan` argv lists (run in a temporary directory holding copies of the files it would
commit; `{dir}` and `{root}` in an argument are replaced, and a program named with a slash is the repository's), and
with `--commit MSG [--trailer LINE]` one commit, on main, of exactly the docs and the bookkeeping files that
changed. A commit that would leave D7 drifting (a doc unchanged and behind state) is refused before anything is
written. It never pushes.
"""
from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path, PurePosixPath

from common import PRIMARY_BRANCH, KitError, Refusal, find_root, git, read_text, read_yaml, write_text

KEYS = ("docs", "state", "quiet_when", "history", "scan")  # history, scan: `pk progress` only (0.6.0)
HISTORY_KEYS = ("doc", "heading")
SHOWN = 6  # newer state commits named in a report
QUIET_TIMEOUT = 20  # seconds for the quiet_when command
NOT_CONFIGURED = "skipped: not configured (progress.docs and progress.state in program/checks.yaml)"


def _paths(value, key: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise KitError(f"checks.yaml: progress.{key} must be a non-empty list of paths")
    for item in value:
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts or "\\" in item:
            raise KitError(f"checks.yaml: progress.{key}: {item!r} must be a relative path inside the repository")
    return [item.rstrip("/") or item for item in value]


def parse(value) -> dict | None:
    """The `progress` section as {docs: [path], state: [path], quiet_when: str or None}, or None when absent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KitError("checks.yaml: progress must be a mapping of docs, state and quiet_when")
    unknown = sorted(str(key) for key in value if key not in KEYS)
    if unknown:
        raise KitError(f"checks.yaml: progress: unknown key {', '.join(unknown)} (expected {', '.join(KEYS)})")
    quiet = value.get("quiet_when")
    if quiet is not None and (not isinstance(quiet, str) or not quiet.strip()):
        raise KitError("checks.yaml: progress.quiet_when must be a shell command (a string)")
    return {"docs": _paths(value.get("docs"), "docs"), "state": _paths(value.get("state"), "state"),
            "quiet_when": quiet, "history": _history(value.get("history")), "scan": _scan(value.get("scan"))}


def _history(value) -> dict | None:
    """progress.history (0.6.0): {doc, heading}, where `pk progress --history` adds its dated line; None when absent."""
    if value is None:
        return None
    if not isinstance(value, dict) or sorted(value) != sorted(HISTORY_KEYS):
        raise KitError("checks.yaml: progress.history must be a mapping of doc (a path) and heading (the heading's "
                       "text), for example doc: program/DASHBOARD.md and heading: History")
    heading = value["heading"]
    if not isinstance(heading, str) or not heading.strip() or "\n" in heading:
        raise KitError("checks.yaml: progress.history.heading must be the heading's text on one line")
    return {"doc": _paths([value["doc"]], "history.doc")[0], "heading": heading.strip().lstrip("#").strip()}


def _scan(value) -> list[list[str]]:
    """progress.scan (0.6.0): argv lists run over copies of what `pk progress` would commit; [] when absent."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(
            isinstance(argv, list) and argv and all(isinstance(arg, str) and arg for arg in argv) for argv in value):
        raise KitError("checks.yaml: progress.scan must be a list of commands, each a non-empty argv list of strings "
                       "(never a shell string)")
    return [list(argv) for argv in value]


def config(root: Path) -> dict | None:
    """The progress section of the checkout's `program/checks.yaml`, None when the file or the section is absent."""
    path = Path(root) / "program" / "checks.yaml"
    if not path.is_file():
        return None
    data = read_yaml(path)
    return parse(data.get("progress") if isinstance(data, dict) else None)


# --- D7 -----------------------------------------------------------------------------

def behind(root: Path, cfg: dict | None, main: str = PRIMARY_BRANCH) -> dict:
    """{ok, detail, docs_commit, newer: ["<short sha> <subject>"], more: int} for the configured docs and state."""
    if cfg is None:
        return {"ok": True, "detail": NOT_CONFIGURED, "docs_commit": None, "newer": [], "more": 0}
    try:
        git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{main}^{{commit}}")
    except KitError:
        return {"ok": True, "detail": f"skipped: branch {main} has no commit", "docs_commit": None, "newer": [],
                "more": 0}
    newest = {}  # doc: the sha of its newest commit on main
    never = []
    for doc in cfg["docs"]:
        sha = git(root, "log", "-1", "--format=%H", main, "--", doc)
        if sha:
            newest[doc] = sha
        else:
            never.append(doc)
    if never:
        span, since = [main], f"{', '.join(never)} never committed on {main}"
        docs_commit = None
    else:
        # the oldest of the docs' newest commits, by history rather than by clock (commits share a second):
        # their common ancestor, which is that commit itself whenever they lie on one line
        shas = list(newest.values())
        docs_commit = shas[0]
        for sha in shas[1:]:
            docs_commit = git(root, "merge-base", docs_commit, sha)
        oldest = [doc for doc, sha in newest.items() if sha == docs_commit]
        span = [f"{docs_commit}..{main}"]
        since = (f"{', '.join(oldest)} last changed in {docs_commit[:12]}" if oldest
                 else f"the docs' newest commits have their common ancestor at {docs_commit[:12]}")
    listing = git(root, "log", "--format=%h %s", *span, "--", *cfg["state"])
    lines = [line[:140] for line in listing.splitlines() if line.strip()]
    newer, more = lines[:SHOWN], max(0, len(lines) - SHOWN)
    if not lines:
        return {"ok": True, "detail": f"progress docs current ({since}; no newer state commit)",
                "docs_commit": docs_commit, "newer": [], "more": 0}
    shown = "; ".join(newer) + (f" (and {more} more)" if more else "")
    return {"ok": False, "detail": f"progress docs behind state: {since}; {len(lines)} newer state commit(s) on "
                                   f"{main}: {shown}", "docs_commit": docs_commit, "newer": newer, "more": more}


def quiet(root: Path, cfg: dict | None) -> bool:
    """Whether the configured quiet_when command exits 0 (run by the shell in the repository root)."""
    command = (cfg or {}).get("quiet_when")
    if not command:
        return False
    try:
        return subprocess.run(command, shell=True, cwd=str(root), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=QUIET_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --- ready ---------------------------------------------------------------------------

def _tasks(root: Path) -> list[dict]:
    """Every task mapping of the register with a string id, in document order."""
    import register as register_module

    if not (Path(root) / "program" / "register.yaml").is_file():
        return []
    register = register_module.load(root)
    return [node for level, node, _ in register_module._nodes(register)
            if level == 3 and isinstance(node.get("id"), str) and node["id"]]


def _started_elsewhere(root: Path) -> tuple[set[str], set[str]]:
    """(branch names under task/ and wip/, lower-cased worktree directory names)."""
    refs = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/task/", "refs/heads/wip/")
    import worktree

    trees = {Path(entry["worktree"]).name.lower()
             for entry in worktree._entries(git(root, "worktree", "list", "--porcelain"))}
    return set(refs.split()), trees


def _taken(task_id: str, branches: set[str], trees: set[str]) -> bool:
    low = task_id.lower()
    if f"task/{task_id}" in branches or low in trees or any(name.startswith(low + "-") for name in trees):
        return True
    prefix = f"wip/{task_id}"
    return any(name == prefix or (name.startswith(prefix) and not name[len(prefix)].isalnum()) for name in branches)


def ready(root: Path) -> dict:
    """{ready: [{id, title}], near_ready: [{id, title, waiting_on, waiting_status}]}."""
    import status as status_module

    root = Path(root)
    statuses = {}
    for path in sorted((root / "program" / "status").glob("*.json")):
        try:
            statuses[path.stem] = status_module.read(root, path.stem)["status"]
        except KitError:
            statuses[path.stem] = "unreadable"
    branches, trees = _started_elsewhere(root)
    found = {"ready": [], "near_ready": []}
    for task in _tasks(root):
        tid = task["id"]
        if statuses.get(tid, "not_started") != "not_started" or _taken(tid, branches, trees):
            continue
        deps = task.get("depends_on") if isinstance(task.get("depends_on"), list) else []
        pending = [dep for dep in deps if statuses.get(str(dep), "not_started") not in status_module.TERMINAL]
        title = " ".join(str(task.get("title", "")).split())
        if not pending:
            found["ready"].append({"id": tid, "title": title})
        elif len(pending) == 1:
            found["near_ready"].append({"id": tid, "title": title, "waiting_on": str(pending[0]),
                                        "waiting_status": statuses.get(str(pending[0]), "not_started")})
    return found


def ready_lines(found: dict) -> list[str]:
    lines = [f"{item['id']} ready: {item['title']}" for item in found["ready"]]
    lines += [f"{item['id']} near-ready, after {item['waiting_on']} ({item['waiting_status']}): {item['title']}"
              for item in found["near_ready"]]
    return lines


# --- pk progress (0.6.0) --------------------------------------------------------------

HANDOFF = "program/HANDOFF.md"
BOOKKEEPING = ("program/HANDOFF.md", "program/rulings.md", "program/costs.jsonl", "program/controller-state.json",
               "program/beta-feedback.md")  # committed with the docs when the pass leaves them changed
SCAN_TIMEOUT = 600  # seconds per scan command
NOT_SET_UP = ("pk progress needs the progress section of program/checks.yaml: docs (the hand-kept progress "
              "documents) and state (the paths whose commits change the program's state), and optionally history "
              "(doc and heading for --history) and scan (argv lists run over copies before a commit); "
              "kit/templates/checks.yaml has the example")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
NUMBERED = re.compile(r"^(\d+)\.[ \t]")
PHRASE = re.compile(r"Latest ruling R\d+ \(next R\d+\)")


class _Doc:
    """One document's lines; a HANDOFF's generated block is out of reach of every edit."""

    def __init__(self, rel: str, text: str):
        self.rel, self.original = rel, text
        self.lines = text.splitlines(keepends=True)

    def text(self) -> str:
        return "".join(self.lines)

    def editable(self) -> range:
        """The line indexes an edit may touch: all, or for the hand-off those before its generated block."""
        if self.rel == HANDOFF:
            import handoff

            for number, line in enumerate(self.lines):
                if line.strip() == handoff.START:
                    return range(number)
        return range(len(self.lines))

    @staticmethod
    def ending(line: str) -> str:
        return line[len(line.rstrip("\r\n")):] or "\n"


def _one_line(text: str, flag: str) -> str:
    if "\n" in text or "\r" in text:
        raise KitError(f"{flag}: the text must be one line")
    text = text.strip()
    if not text:
        raise KitError(f"{flag} needs a text after the =")
    return text


def _pair(value: str, flag: str, shape: str) -> tuple[str, str]:
    if "=" not in value:
        raise KitError(f"{flag} needs {shape}, got {value!r}")
    name, text = value.split("=", 1)
    if not name.strip():
        raise KitError(f"{flag} needs {shape}, got {value!r}")
    return name.strip(), text


def _only(hits: list[tuple["_Doc", int]], what: str) -> tuple["_Doc", int]:
    if not hits:
        raise KitError(f"no {what} in the progress docs; it must exist before the pass replaces its text")
    if len(hits) > 1:
        places = ", ".join(f"{doc.rel}:{number + 1}" for doc, number in hits)
        raise KitError(f"{what} is in more than one place ({places}); keep one")
    return hits[0]


def _row(docs: list["_Doc"], name: str, text: str) -> str:
    text = _one_line(text, "--row")
    if "|" in text:
        raise KitError("--row: the text must not contain '|' (it would split the table cell)")
    hits = []
    for doc in docs:
        for number in doc.editable():
            line = doc.lines[number]
            if line.lstrip().startswith("|"):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                if len(cells) >= 2 and cells[0] == name:
                    hits.append((doc, number))
    doc, number = _only(hits, f"table row named {name!r}")
    cells = [cell.strip() for cell in doc.lines[number].strip().strip("|").split("|")]
    cells[1] = text
    doc.lines[number] = "| " + " | ".join(cells) + " |" + _Doc.ending(doc.lines[number])
    return f"{doc.rel}: row {name}"


def _bullet(docs: list["_Doc"], key: str, text: str) -> str:
    text = _one_line(text, "--bullet")
    prefix = f"- **{key}:**"
    hits = [(doc, n) for doc in docs for n in doc.editable()
            if doc.lines[n].startswith(prefix + " ") or doc.lines[n].rstrip("\r\n") == prefix]
    doc, number = _only(hits, f"bullet '{prefix}'")
    doc.lines[number] = f"{prefix} {text}{_Doc.ending(doc.lines[number])}"
    return f"{doc.rel}: bullet {key}"


def _section(docs: list["_Doc"], heading: str) -> tuple["_Doc", int, int]:
    """(doc, heading line index, end index) of the one heading whose text is `heading` or starts with it and a space."""
    hits = []
    for doc in docs:
        for number in doc.editable():
            m = HEADING.match(doc.lines[number].rstrip("\r\n"))
            if m and (m.group(2) == heading or m.group(2).startswith(heading + " ")):
                hits.append((doc, number))
    doc, start = _only(hits, f"heading {heading!r}")
    level = len(HEADING.match(doc.lines[start].rstrip("\r\n")).group(1))
    end = start + 1
    allowed = doc.editable()
    while end in allowed:
        m = HEADING.match(doc.lines[end].rstrip("\r\n"))
        if m and len(m.group(1)) <= level:
            break
        end += 1
    return doc, start, end


def _list_span(doc: "_Doc", start: int, end: int) -> tuple[int, int] | None:
    """(first, past-last) line of the first numbered list in the section; continuation lines are indented."""
    first = next((n for n in range(start + 1, end) if NUMBERED.match(doc.lines[n])), None)
    if first is None:
        return None
    last = first + 1
    while last < end and (NUMBERED.match(doc.lines[last]) or (doc.lines[last][:1] in (" ", "\t")
                                                             and doc.lines[last].strip())):
        last += 1
    return first, last


def _item(docs: list["_Doc"], target: str, text: str) -> str:
    text = _one_line(text, "--item")
    heading, _, number = target.rpartition("#")
    if not heading.strip() or not number.strip().isdigit():
        raise KitError(f"--item needs HEADING#N=TEXT, got {target!r}")
    heading, number = heading.strip(), number.strip()
    doc, start, end = _section(docs, heading)
    hits = [n for n in range(start + 1, end) if doc.lines[n].startswith(f"{number}. ")]
    if len(hits) != 1:
        raise KitError(f"no item {number} under {heading!r} in {doc.rel}" if not hits
                       else f"item {number} is under {heading!r} more than once in {doc.rel}")
    at = hits[0]
    past = at + 1
    while past < end and doc.lines[past][:1] in (" ", "\t") and doc.lines[past].strip():
        past += 1  # the item's continuation lines are current-state text too
    doc.lines[at:past] = [f"{number}. {text}{_Doc.ending(doc.lines[at])}"]
    return f"{doc.rel}: item {number} under {heading}"


def _list(docs: list["_Doc"], heading: str, source: str) -> str:
    items = [NUMBERED.sub("", line.strip(), count=1).strip() for line in read_text(Path(source)).splitlines()]
    items = [item for item in items if item]
    if not items:
        raise KitError(f"--list: {source} holds no line")
    doc, start, end = _section(docs, heading)
    ending = _Doc.ending(doc.lines[start])
    new = [f"{n}. {item}{ending}" for n, item in enumerate(items, 1)]
    span = _list_span(doc, start, end)
    if span is None:
        doc.lines[start + 1:start + 1] = [ending, *new]
    else:
        doc.lines[span[0]:span[1]] = new
    return f"{doc.rel}: list under {heading} ({len(items)} items)"


def _history_line(docs: list["_Doc"], cfg: dict, text: str) -> str:
    text = _one_line(text, "--history")
    if cfg["history"] is None:
        raise KitError("--history needs progress.history in program/checks.yaml (doc and heading: where the dated "
                       "line goes)")
    doc_rel, heading = cfg["history"]["doc"], cfg["history"]["heading"]
    mine = [doc for doc in docs if doc.rel == doc_rel]
    if not mine:
        raise KitError(f"progress.history.doc {doc_rel} is not a progress doc that exists")
    doc, start, end = _section(mine, heading)
    ending = _Doc.ending(doc.lines[start])
    first = next((n for n in range(start + 1, end) if doc.lines[n].startswith("- ")), None)
    line = f"- {date.today().isoformat()}: {text}{ending}"
    if first is None:
        doc.lines[start + 1:start + 1] = [ending, line]
    else:
        doc.lines.insert(first, line)
    return f"{doc.rel}: history line"


def _latest_ruling(root: Path, docs: list["_Doc"]) -> list[str]:
    import rulings as rulings_module

    numbers = [ruling["number"] for ruling in rulings_module.read(root)]
    if not numbers:
        return []
    phrase = f"Latest ruling R{max(numbers)} (next R{max(numbers) + 1})"
    done = []
    for doc in docs:
        for number in doc.editable():
            line = doc.lines[number]
            if PHRASE.search(line) and PHRASE.sub(phrase, line) != line:
                doc.lines[number] = PHRASE.sub(phrase, line)
                done.append(f"{doc.rel}: {phrase}")
    return done


def _changed(root: Path, paths) -> list[str]:
    """The paths among `paths` whose working-tree content differs from HEAD (untracked ones included)."""
    if not paths:
        return []
    found = set(git(root, "diff", "--name-only", "HEAD", "--", *paths).splitlines())
    found |= set(git(root, "ls-files", "--others", "--exclude-standard", "--", *paths).splitlines())
    return [path for path in paths if path in found or any(name.startswith(path + "/") for name in found)]


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _behind_after(root: Path, cfg: dict, committing: set[str]) -> list[str]:
    """The progress docs that would still be behind state after a commit of `committing`, one reason each."""
    reasons = []
    state_now = any(_under(path, cfg["state"]) for path in committing)
    for doc in cfg["docs"]:
        if doc in committing:
            continue
        sha = git(root, "log", "-1", "--format=%H", PRIMARY_BRANCH, "--", doc)
        newer = git(root, "log", "--format=%h %s", f"{sha}..{PRIMARY_BRANCH}", "--", *cfg["state"]) if sha else "x"
        if not sha:
            reasons.append(f"{doc} was never committed on {PRIMARY_BRANCH}")
        elif newer.strip():
            reasons.append(f"{doc} is unchanged and behind state commit {newer.splitlines()[0]}")
        elif state_now:
            reasons.append(f"{doc} is unchanged and this commit holds a state path")
    return reasons


def _scan_copies(root: Path, cfg: dict, paths: list[str]) -> None:
    """Run each progress.scan command in a temporary directory holding copies of `paths`; refuse on any failure."""
    import shutil
    import tempfile

    from common import run_argv

    if not cfg["scan"] or not paths:
        return
    with tempfile.TemporaryDirectory(prefix="pk-progress-scan-") as tmp:
        for rel in paths:
            target = Path(tmp) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if (Path(root) / rel).is_file():
                shutil.copyfile(Path(root) / rel, target)
        for argv in cfg["scan"]:
            argv = [arg.replace("{root}", str(root)).replace("{dir}", tmp) for arg in argv]
            if "/" in argv[0] and not Path(argv[0]).is_absolute():
                argv[0] = str(Path(root) / argv[0])  # a program of the repository, named from its root
            code, output = run_argv(argv, Path(tmp), timeout=SCAN_TIMEOUT)
            if code != 0:
                tail = " ".join(output.split())[-600:]
                raise KitError(f"scan {' '.join(argv)} exit {code} over copies of {', '.join(paths)}: {tail}; the "
                               "docs are written and nothing committed: fix the text and run the pass again")


def run_pass(root: Path, rows=(), bullets=(), items=(), lists=(), history=None, dashboard_out=None,
             commit=None, trailers=()) -> list[str]:
    """The progress pass; returns its report lines. Every edit is made in memory first: a refusal writes nothing."""
    root = Path(root)
    cfg = config(root)
    if cfg is None:
        raise KitError(NOT_SET_UP)
    if commit is not None:
        if not commit.strip():
            raise KitError("--commit needs a message")
        branch = git(root, "branch", "--show-current")
        if branch != PRIMARY_BRANCH:
            raise KitError(f"--commit commits the progress docs on {PRIMARY_BRANCH}; this checkout is on "
                           f"{branch or 'a detached HEAD'}")
    targets = list(dict.fromkeys([*cfg["docs"], HANDOFF]))
    docs = [_Doc(rel, read_text(root / rel, newline="")) for rel in targets if (root / rel).is_file()]
    report = []
    for value in rows:
        report.append(_row(docs, *_pair(value, "--row", "NAME=TEXT")))
    for value in bullets:
        report.append(_bullet(docs, *_pair(value, "--bullet", "KEY=TEXT")))
    for value in items:
        report.append(_item(docs, *_pair(value, "--item", "HEADING#N=TEXT")))
    for value in lists:
        report.append(_list(docs, *_pair(value, "--list", "HEADING=FILE")))
    if history is not None:
        report.append(_history_line(docs, cfg, history))
    report += _latest_ruling(root, docs)
    edited = [doc for doc in docs if doc.text() != doc.original]
    if commit is not None:  # before any write: a commit that would leave D7 drifting is refused up front
        will = {doc.rel for doc in edited} | set(_changed(root, list(BOOKKEEPING)))
        if HANDOFF in cfg["state"] or _under(HANDOFF, cfg["state"]):
            will.add(HANDOFF)
        reasons = _behind_after(root, cfg, will)
        if reasons:
            raise KitError("--commit would leave pk drift D7 drifting: " + "; ".join(reasons) + "; give that doc its "
                           "current-state text (--row, --bullet, --item, --list, --history) and run the pass again")
    for doc in edited:
        write_text(root / doc.rel, doc.text())
    import handoff

    report.append("handoff: updated" if handoff.update(root) else "handoff: unchanged")
    problems = handoff.budget_problems(root)
    if problems:
        raise Refusal("; ".join(problems))
    if not handoff.check(root):
        raise KitError("program/HANDOFF.md is stale after its update; run: pk handoff update")
    report.append("handoff: current")
    if dashboard_out is not None:
        import dashboard

        out = Path(dashboard_out)
        if out.is_dir() or not out.parent.is_dir():
            raise KitError(f"--dashboard {out}: name an HTML file in a directory that exists")
        page, _ = dashboard.render(root)
        write_text(out, page)
        report.append(f"dashboard: wrote {out}")
    paths = _changed(root, list(dict.fromkeys([*cfg["docs"], *BOOKKEEPING])))
    if cfg["scan"] and paths:
        _scan_copies(root, cfg, paths)
        report.append(f"scan: ok ({len(cfg['scan'])} command(s) over {len(paths)} file(s))")
    if commit is not None:
        if not paths:
            report.append("commit: nothing changed, nothing to commit")
            return report
        message = commit.strip() + ("\n\n" + "\n".join(t.strip() for t in trailers if t.strip()) if trailers else "")
        git(root, "add", "--", *paths)
        git(root, "commit", "-q", "-m", message, "--", *paths)
        report.append("committed " + git(root, "log", "-1", "--format=%h %s") + f" ({', '.join(paths)}); not pushed")
        result = behind(root, cfg)
        report.append(f"D7 {'ok' if result['ok'] else 'DRIFT'}: {result['detail']}")
    return report


def cmd_progress(args) -> int:
    for line in run_pass(find_root(), rows=args.row, bullets=args.bullet, items=args.item, lists=args.list,
                         history=args.history, dashboard_out=args.dashboard, commit=args.commit,
                         trailers=args.trailer):
        print(line)
    return 0


def _configure(parser) -> None:
    parser.add_argument("--row", action="append", default=[], metavar="NAME=TEXT",
                        help="replace the second cell of the table row whose first cell is NAME")
    parser.add_argument("--bullet", action="append", default=[], metavar="KEY=TEXT",
                        help="replace the bullet that starts '- **KEY:**'")
    parser.add_argument("--item", action="append", default=[], metavar="HEADING#N=TEXT",
                        help="replace numbered item N under the heading")
    parser.add_argument("--list", action="append", default=[], metavar="HEADING=FILE",
                        help="replace the numbered list under the heading with the lines of FILE")
    parser.add_argument("--history", metavar="TEXT", help="add one dated line at the top of progress.history")
    parser.add_argument("--dashboard", metavar="OUT", help="also write pk dashboard's page to OUT")
    parser.add_argument("--commit", metavar="MSG", help="commit the docs and the bookkeeping files that changed "
                                                        "(never pushes)")
    parser.add_argument("--trailer", action="append", default=[], metavar="LINE",
                        help="a trailer line of the commit message (repeatable)")


def register(add_command) -> None:
    add_command("progress", cmd_progress, help="the controller's progress pass: replace current-state text in the "
                "progress docs, refresh the hand-off, optionally scan and commit", configure=_configure)
