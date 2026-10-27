#!/usr/bin/env python3
"""The controller's tracking commands: follow-ups, ad-hoc tasks and the one-step `record` (standard library only).

`pk followups` and `pk adhoc` edit `program/controller-state.json` through `ctlstate` and nothing else; they do
not commit. `pk record` is one controller step in one command: it appends a ruling, applies any state changes
given with it (ad-hoc tasks, stage text, the queued and parked notes, follow-ups, the now line, the hand-off
budget), regenerates the hand-off block, checks the hand-off budget and commits exactly the files it wrote
(`program/rulings.md`, `program/controller-state.json`, `program/HANDOFF.md`, and the cost log when it has new
lines), with a message taken from the ruling's first sentence. Other changed files under `program/` are never
staged; after the commit one line names them (`left_out`), so a ruling and its artifact edits can be committed
together by hand. It runs in the main checkout only, with or
without `--no-commit`.

What a refusal leaves behind. Everything that can be checked before the first write is: the arguments, the
state changes, that the hand-off can be regenerated, and that no committed ruling was rewritten (hard rule
23). If a write or the commit then fails, the three files are put back as they were and nothing stays staged.
One check can only be made afterwards: the size budget is measured on the hand-off as rewritten, so a step
that takes it over budget is committed and the command then exits 1 with the reason. A process killed between
the writes is not undone: `git status` shows the files, and `git checkout -- <path>` restores them.

What it never does: set the status of a task. A register task's state is its status record (`pk status`), and
an ad-hoc task's tracking state ends at `closed`, which is refused while its status record is still in flight
or its branch is not merged.
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path

import ctlstate
import handoff as handoff_module
import rulings as rulings_module
import status as status_module
from common import KitError, PRIMARY_BRANCH, Refusal, find_root, git, run_argv, utc_now

RECORD_FILES = ("program/rulings.md", ctlstate.STATE_FILE, "program/HANDOFF.md", "program/costs.jsonl")
NOW_CHARS = 200
SUBJECT_CHARS = 72


def _today() -> str:
    return utc_now()[:10]


def _pair(text: str, flag: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.strip() or not value.strip():
        raise KitError(f"{flag} takes KEY=VALUE, not {text!r}")
    return key.strip(), value.strip()


def _open(state: dict, followup_id: str) -> dict:
    for item in state["followups"]:
        if item["id"] == followup_id:
            return item
    if any(item["id"] == followup_id for item in state["closed_followups"]):
        raise KitError(f"{followup_id} is already closed")
    raise KitError(f"{followup_id} is not a follow-up (pk followups list)")


# --- state changes (on a state mapping; nothing is written here) ----------------------

def add_followup(state: dict, area: str, item: str, tags=()) -> str:
    for tag in tags:
        if not ctlstate.TAG.match(tag):
            raise KitError(f"not a usable tag: {tag!r} (lower-case letters, digits, '.', '_' and '-')")
    followup_id = ctlstate.next_followup_id(state)
    state["followups"].append({"id": followup_id, "area": ctlstate.one_line(area, "a follow-up's area"),
                               "item": ctlstate.one_line(item, "a follow-up's item"), "added": _today(),
                               "tags": list(dict.fromkeys(tags))})
    return followup_id


def close_followup(state: dict, followup_id: str, reason: str, by: str = "", duplicate_of: str = "") -> None:
    item = _open(state, followup_id)
    if duplicate_of:
        if duplicate_of == followup_id:
            raise KitError(f"{followup_id} cannot be a duplicate of itself")
        known = {entry["id"] for entry in state["followups"] + state["closed_followups"]}
        if duplicate_of not in known:
            raise KitError(f"{duplicate_of} is not a follow-up")
    state["followups"].remove(item)
    state["closed_followups"].append(dict(item, closed=_today(), reason=ctlstate.one_line(reason, "the reason"),
                                          by=" ".join(by.split()), duplicate_of=duplicate_of))


def tag_followup(state: dict, followup_id: str, tag: str, remove: bool = False) -> None:
    item = _open(state, followup_id)
    if not ctlstate.TAG.match(tag):
        raise KitError(f"not a usable tag: {tag!r} (lower-case letters, digits, '.', '_' and '-')")
    if remove:
        if tag not in item["tags"]:
            raise KitError(f"{followup_id} does not carry the tag {tag}")
        item["tags"].remove(tag)
    elif tag not in item["tags"]:
        item["tags"].append(tag)


def add_adhoc(state: dict, title: str, note: str = "", ruling: str = "", task_state: str = "queued") -> str:
    if task_state not in ("queued", "progress"):
        raise KitError("a new ad-hoc task starts queued or in progress")
    task_id = ctlstate.next_adhoc_id(state)
    state["adhoc"].append({"id": task_id, "title": ctlstate.one_line(title, "an ad-hoc task's title", 120),
                           "state": task_state, "note": " ".join(note.split()), "ruling": ruling,
                           "updated": _today()})
    return task_id


def set_adhoc(root: Path, state: dict, task_id: str, task_state: str | None = None, note: str | None = None) -> dict:
    """Change an ad-hoc task's tracking state or note. `closed` is refused while its status record is in flight."""
    item = next((entry for entry in state["adhoc"] if entry["id"] == task_id), None)
    if item is None:
        raise KitError(f"{task_id} is not an ad-hoc task (pk adhoc list)")
    if task_state is not None:
        if task_state not in ctlstate.ADHOC_STATES:
            raise KitError(f"unknown ad-hoc state {task_state!r}; states are {', '.join(ctlstate.ADHOC_STATES)} "
                           "(done is a status only QA sets: pk status)")
        if task_state == "closed":
            held = status_module.read(root, task_id)["status"]
            if held in ("in_progress", "review", "blocked"):
                raise KitError(f"{task_id}: its status record is {held}; an ad-hoc task with work in flight is "
                               "closed only after QA set it done or, on the light tier, the controller recorded "
                               "it merged")
            branch = f"refs/heads/task/{task_id}"
            if run_argv(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", branch], Path(root))[0] == 0 \
                    and run_argv(["git", "-C", str(root), "merge-base", "--is-ancestor", branch, PRIMARY_BRANCH],
                                 Path(root))[0] != 0:
                raise KitError(f"{task_id}: its branch task/{task_id} is not merged into {PRIMARY_BRANCH}; merge "
                               f"it, or abandon it first (pk worktree finish {task_id} --keep-branch, then delete "
                               "the branch yourself)")
        item["state"] = task_state
    if note is not None:
        item["note"] = " ".join(note.split())
    item["updated"] = _today()
    return item


# --- record: one controller step ------------------------------------------------------

def _subject(number: int, text: str) -> str:
    sentence = " ".join(text.split())
    for stop in (". ", "; "):
        if stop in sentence:
            sentence = sentence.split(stop, 1)[0]
    subject = f"R{number}: {sentence.rstrip('.')}"
    return subject if len(subject) <= SUBJECT_CHARS else subject[:SUBJECT_CHARS - 3].rstrip() + "..."


def _require_main(root: Path) -> None:
    try:
        here = git(root, "symbolic-ref", "HEAD")
    except KitError:
        here = ""
    if here != f"refs/heads/{PRIMARY_BRANCH}":
        raise KitError(f"pk record runs in the main checkout, on {PRIMARY_BRANCH}")
    if git(root, "diff", "--cached", "--name-only"):
        raise KitError("pk record commits only the files it writes: unstage what is staged first")


def commit_paths(root: Path, paths, subject: str, trailers=()) -> str | None:
    """Commit exactly `paths` (those that changed) with `subject` and the trailer lines; the short sha, or None."""
    changed = [path for path in paths if git(root, "status", "--porcelain", "--", path)]
    if not changed:
        return None
    git(root, "add", "--", *changed)
    message = subject + ("\n\n" + "\n".join(trailers) if trailers else "")
    git(root, "commit", "-q", "-m", message, "--only", "--", *changed)
    return git(root, "rev-parse", "--short", "HEAD")


LEFT_OUT_SHOWN = 10


def left_out(root: Path) -> str:
    """One line naming the changed `program/` paths a `pk record` commit left out, or "" when there are none.

    `pk record` commits only the files it writes, so artifact edits made for the same step (a SPEC, a register
    change, a CR) stay uncommitted. It does not stage them: a sweep of `program/` would also take a half-written
    status file or an evidence record into a ruling commit. It says which they are, so the step can be finished
    by hand, or done next time with `--no-commit` and one commit of everything.
    """
    listing = git(root, "status", "--porcelain", "--untracked-files=all", "--", "program/")
    paths = sorted({line[3:].split(" -> ")[-1] for line in listing.splitlines() if len(line) > 3})
    if not paths:
        return ""
    shown = ", ".join(paths[:LEFT_OUT_SHOWN]) + (f" and {len(paths) - LEFT_OUT_SHOWN} more" if len(paths) >
                                                 LEFT_OUT_SHOWN else "")
    return (f"not in this commit, changed under program/: {shown} (pk record commits only its own files: commit "
            "these with git, or use pk record --no-commit and commit everything together)")


def _table_entry(planned: dict, table: str, text: str, registered: set[str]) -> str:
    """Set (`ID=TEXT`) or remove (`ID=`) one line of the queued or parked table; the line to print."""
    task_id, sep, note = (part.strip() for part in text.partition("="))
    if not sep or not task_id:
        raise KitError(f"--{table} takes ID=TEXT (ID= alone removes the line), not {text!r}")
    if ctlstate.ADHOC_ID.match(task_id):
        raise KitError(f"--{table}: {task_id} is an ad-hoc task; its tracking state is set with --task "
                       f"{task_id}=queued or =paused, and its note with --note")
    if not note:
        if task_id not in planned[table]:
            raise KitError(f"--{table}: {task_id} has no {table} line to remove")
        del planned[table][task_id]
        return f"{table}: {task_id} removed"
    if task_id not in registered:
        raise KitError(f"--{table}: {task_id} is not a task in program/register.yaml")
    planned[table][task_id] = ctlstate.one_line(note, f"a {table} line", NOW_CHARS)
    return f"{table}: {task_id}"


def record(root: Path, ruling: str, question: str = "", precedent: str = "", role: str = "controller",
           tasks=(), notes=(), adhoc=(), start: bool = False, followups=(), closes=(), now: str | None = None,
           commit: bool = True, trailers=(), queued=(), parked=(), budget: int | None = None) -> list[str]:
    """One controller step; returns the lines to print (under ten). See the module text for what a refusal leaves."""
    root = Path(root)
    _require_main(root)  # with --no-commit too: a ruling written on a task branch would number against main's
    problem = rulings_module.rewritten(root)
    if problem:
        raise KitError(problem)
    state = ctlstate.load(root)
    number = max((entry["number"] for entry in rulings_module.read(root)), default=0) + 1
    name = f"R{number}"
    planned = copy.deepcopy(state)
    lines = [f"{name} recorded"]
    for title in adhoc:
        task_id = add_adhoc(planned, title, ruling=name, task_state="progress" if start else "queued")
        lines.append(f"ad-hoc task {task_id} added ({'progress' if start else 'queued'})")
    for text in tasks:
        task_id, task_state = _pair(text, "--task")
        if not ctlstate.ADHOC_ID.match(task_id):
            raise KitError(f"--task sets an ad-hoc task's tracking state; the state of {task_id} is its status "
                           f"record (pk status set). Use --note {task_id}=TEXT for its stage text")
        set_adhoc(root, planned, task_id, task_state=task_state)
        lines.append(f"{task_id}: {task_state}")
    registered = status_module._registered(root) if notes or queued or parked else set()
    for text in notes:
        task_id, note = _pair(text, "--note")
        if ctlstate.ADHOC_ID.match(task_id):
            set_adhoc(root, planned, task_id, note=note)
        elif task_id not in registered:
            raise KitError(f"--note: {task_id} is not a task in program/register.yaml")
        else:
            planned["stage"][task_id] = " ".join(note.split())
    lines += [_table_entry(planned, "queued", text, registered) for text in queued]
    lines += [_table_entry(planned, "parked", text, registered) for text in parked]
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise KitError("--handoff-budget takes a number of bytes (0 goes back to the default)")
        planned.pop("handoff_budget_bytes", None)
        if budget:
            planned["handoff_budget_bytes"] = budget
        lines.append(f"hand-off budget: {budget or handoff_module.BUDGET_BYTES} bytes"
                     + ("" if budget else " (the default)"))
    added = [add_followup(planned, *_pair(text, "--followup")) for text in followups]
    closed = []
    for text in closes:
        followup_id, reason = _pair(text, "--close")
        close_followup(planned, followup_id, reason, by=name)
        closed.append(followup_id)
    if added or closed:
        lines.append("follow-ups: " + "; ".join(part for part in (
            f"{len(added)} added ({', '.join(added)})" if added else "",
            f"{len(closed)} closed ({', '.join(closed)})" if closed else "") if part))
    if now is not None:
        planned["now"] = ctlstate.one_line(now, "the now line", NOW_CHARS) if now.strip() else ""
    handoff_module._split(handoff_module._read(root)[1])  # a hand-off that cannot be regenerated stops the step here
    handoff_module.render(root)
    before = {name: (root / name).read_bytes() if (root / name).is_file() else None for name in RECORD_FILES}
    try:
        rulings_module.add(root, role, question or "Controller step.", ruling, precedent)
        if planned != state:
            ctlstate.save(root, planned)
        changed = handoff_module.update(root)
        problems = handoff_module.budget_problems(root)
        size = len(handoff_module._read(root)[1].encode("utf-8"))
        limit = planned.get("handoff_budget_bytes", handoff_module.BUDGET_BYTES)
        lines.append(f"hand-off: {'updated' if changed else 'unchanged'} ({size} of {limit} bytes)")
        if commit:
            sha = commit_paths(root, RECORD_FILES, _subject(number, ruling), trailers)
            lines.append(f"committed {sha}: {_subject(number, ruling)}" if sha else "nothing to commit")
            left = left_out(root)
            if left:
                lines.append(left)
        else:
            lines.append("not committed (--no-commit): " + ", ".join(RECORD_FILES))
    except BaseException:  # a failed write or commit: no ruling without its state change, nothing left staged
        for name, data in before.items():
            run_argv(["git", "-C", str(root), "restore", "--staged", "--", name], root)
            if data is None:
                (root / name).unlink(missing_ok=True)
            elif (root / name).read_bytes() != data:
                (root / name).write_bytes(data)
        raise
    if problems:  # committed, then the reason, on both streams (KF-15)
        raise Refusal("; ".join([*lines, *problems]))
    return lines


# --- commands -------------------------------------------------------------------------

def cmd_followups(args: argparse.Namespace) -> int:
    action = getattr(args, "followups_action", None)
    root = find_root()
    state = ctlstate.load(root)
    if action == "add":
        print(add_followup(state, args.area, args.item, args.tag))
    elif action == "close":
        close_followup(state, args.id, args.reason, by=args.by)
        print(f"{args.id}: closed")
    elif action == "dup":
        close_followup(state, args.id, f"duplicate of {args.of}", duplicate_of=args.of)
        print(f"{args.id}: closed as a duplicate of {args.of}")
    elif action == "tag":
        tag_followup(state, args.id, args.tag, remove=args.remove)
        print(f"{args.id}: {'untagged' if args.remove else 'tagged'} {args.tag}")
    elif action == "list":
        items = state["closed_followups"] if args.closed else state["followups"]
        items = [item for item in items if args.tag is None or args.tag in item["tags"]]
        print(f"{len(items)} {'closed' if args.closed else 'open'} follow-up(s)"
              + (f" tagged {args.tag}" if args.tag else ""))
        for item in sorted(items, key=lambda entry: entry["id"], reverse=True):
            tags = f" [{', '.join(item['tags'])}]" if item["tags"] else ""
            ending = f" (closed {item['closed']}: {item['reason']})" if args.closed else ""
            print(f"{item['id']}\t{item['added']}\t{item['area']}: {item['item']}{tags}{ending}")
        return 0
    else:
        raise KitError("followups needs an action: add, close, dup, tag or list")
    ctlstate.save(root, state)
    return 0


def cmd_adhoc(args: argparse.Namespace) -> int:
    action = getattr(args, "adhoc_action", None)
    root = find_root()
    state = ctlstate.load(root)
    if action == "add":
        if args.ruling:
            status_module._ruling_number(root, args.ruling, "an ad-hoc task")
        print(add_adhoc(state, args.title, note=args.note, ruling=args.ruling))
    elif action == "set":
        item = set_adhoc(root, state, args.id, task_state=args.state, note=args.note)
        print(f"{item['id']}: {item['state']}")
    elif action == "list":
        for item in state["adhoc"]:
            print(f"{item['id']}\t{item['state']}\t{item['updated']}\t{item['ruling'] or '-'}\t{item['title']}"
                  + (f": {item['note']}" if item["note"] else ""))
        return 0
    else:
        raise KitError("adhoc needs an action: add, set or list")
    ctlstate.save(root, state)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    lines = record(find_root(), args.ruling, question=args.question, precedent=args.precedent, role=args.role,
                   tasks=args.task, notes=args.note, adhoc=args.adhoc, start=args.start, followups=args.followup,
                   closes=args.close, now=args.now, commit=not args.no_commit, trailers=args.trailer,
                   queued=args.queued, parked=args.parked, budget=args.handoff_budget)
    print("\n".join(lines))
    return 0


def _configure_followups(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="followups_action", metavar="action", parser_class=type(parser))
    adder = actions.add_parser("add", help="add an open follow-up; prints its id")
    adder.add_argument("--area", required=True)
    adder.add_argument("--item", required=True)
    adder.add_argument("--tag", action="append", default=[], help="defer it under this tag")
    closer = actions.add_parser("close", help="move a follow-up to the closed list")
    closer.add_argument("id")
    closer.add_argument("--reason", required=True)
    closer.add_argument("--by", default="", help="the ruling or commit that closed it")
    dup = actions.add_parser("dup", help="close a follow-up as a duplicate of another")
    dup.add_argument("id")
    dup.add_argument("--of", required=True, metavar="ID")
    tagger = actions.add_parser("tag", help="tag an open follow-up (a tagged one is deferred: counted, not listed)")
    tagger.add_argument("id")
    tagger.add_argument("tag")
    tagger.add_argument("--remove", action="store_true")
    lister = actions.add_parser("list", help="open follow-ups, newest first, with a count")
    lister.add_argument("--tag")
    lister.add_argument("--closed", action="store_true")


def _configure_adhoc(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="adhoc_action", metavar="action", parser_class=type(parser))
    adder = actions.add_parser("add", help="add an ad-hoc task (work outside the register); prints its id")
    adder.add_argument("--title", required=True)
    adder.add_argument("--note", default="")
    adder.add_argument("--ruling", default="", metavar="R<n>", help="the ruling that asked for it")
    setter = actions.add_parser("set", help="set an ad-hoc task's tracking state or note")
    setter.add_argument("id")
    setter.add_argument("state", nargs="?", choices=ctlstate.ADHOC_STATES)
    setter.add_argument("--note")
    actions.add_parser("list", help="every ad-hoc task")


def _configure_record(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ruling", required=True, help="the ruling text (its decision)")
    parser.add_argument("--question", default="", help="the question it settles")
    parser.add_argument("--precedent", default="")
    parser.add_argument("--role", default="controller")
    parser.add_argument("--task", action="append", default=[], metavar="ID=STATE",
                        help="set an ad-hoc task's tracking state")
    parser.add_argument("--note", action="append", default=[], metavar="ID=TEXT",
                        help="an ad-hoc task's note, or a register task's stage text")
    parser.add_argument("--adhoc", action="append", default=[], metavar="TITLE", help="add an ad-hoc task")
    parser.add_argument("--start", action="store_true", help="the ad-hoc tasks added start in progress")
    parser.add_argument("--followup", action="append", default=[], metavar="AREA=ITEM")
    parser.add_argument("--close", action="append", default=[], metavar="ID=REASON", help="close a follow-up")
    parser.add_argument("--now", help="the one line that says what the controller is doing")
    parser.add_argument("--queued", action="append", default=[], metavar="ID=TEXT",
                        help="a register task's line in the hand-off's Queued list (ID= removes it)")
    parser.add_argument("--parked", action="append", default=[], metavar="ID=TEXT",
                        help="a register task's line in the Parked list, beside its parked status (ID= removes it)")
    parser.add_argument("--handoff-budget", dest="handoff_budget", type=int, metavar="BYTES",
                        help="raise the hand-off size budget, by this ruling (0: back to the default)")
    parser.add_argument("--no-commit", action="store_true", dest="no_commit")
    parser.add_argument("--trailer", action="append", default=[], metavar="LINE",
                        help="a line to end the commit message with: your own attribution line")


def register(add_command) -> None:
    add_command("followups", cmd_followups, help="follow-ups: add, close, dup, tag, list",
                configure=_configure_followups)
    add_command("adhoc", cmd_adhoc, help="ad-hoc tasks, outside the register: add, set, list",
                configure=_configure_adhoc)
    add_command("record", cmd_record, help="one controller step: a ruling, state changes, the hand-off and a commit",
                configure=_configure_record)
