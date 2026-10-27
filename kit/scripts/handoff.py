#!/usr/bin/env python3
"""The generated part of `program/HANDOFF.md` (standard library only).

The file has hand-written parts and one generated block between the marker lines
`<!-- generated:start -->` and `<!-- generated:end -->`. `render` builds the block from the
status records, `program/controller-state.json`, the branches not merged into main, the task
worktrees and the open change requests. `update` rewrites only the text between the markers;
everything else is preserved byte for byte. `check` says whether the block on disk is current; a block that was
never written (a new program: `pk init` and `install.sh` commit nothing and need no commit to exist) is reported as
such, with the command to run, not as stale.

The block holds no wall-clock time, so `check` is stable. It carries the commit it was generated
from on one `Commit: ` line, and `check` and `update` ignore that line when they compare:
committing the hand-off moves HEAD, which must not make it stale. `update` therefore leaves the
file alone when nothing else changed, and the commit line names HEAD as of the last real change.

The main checkout is not listed among the worktrees: its dirty state changes whenever this
file is rewritten, which would make the block unstable. For the same reason `check` does not compare the lines
under the Task worktrees heading (KF-14): heads, clean or dirty states and short-lived worktrees move with every
commit and every running agent. `update` still writes them as they are.

The block is compact on purpose. It opens with a "Now" list (the controller's one line, every task in
flight with its verification tier and, when blocked, what it waits on, the ad-hoc tasks that are open, the
integration rounds that are not closed), it counts the done tasks by tier, lists a parked task with the
decision it waits on, and it shows the open follow-ups that are
not deferred by a tag (newest first, counted, at most FOLLOWUPS_SHOWN), the newest RULINGS_SHOWN rulings on one
line each with the next free number, the newest ADHOC_CLOSED_SHOWN closed ad-hoc tasks and the agents past
the fresh-agent threshold of the cost log. When `program/ledger.yaml` exists an Interview section follows the Now
list: the stage `pk interview next` names, its topics closed and open, the blocking questions unanswered and the
assumptions unconfirmed, so a takeover during discovery needs no other command. History is never copied in: it
lives in `program/rulings.md`.
`check` also fails when the file is over its size budget (BUDGET_BYTES unless the controller state sets
`handoff_budget_bytes`) or its hand-written "Next steps" section is over NEXT_STEPS_BYTES: a successor must
be able to read the hand-off whole.

Writing the block changes `program/HANDOFF.md` and no other file, and nothing at all when the block is current.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import change as change_module
import cost as cost_module
import ctlstate
import rulings as rulings_module
import status as status_module
import trace as trace_module
import worktree as worktree_module
import register as register_module
from common import KitError, find_root, git, primary_branch_problem, read_json, read_text, write_text

START, END = "<!-- generated:start -->", "<!-- generated:end -->"
COMMIT_PREFIX = "Commit: "
IN_FLIGHT = ("in_progress", "review", "blocked")
BUDGET_BYTES = 60_000  # the whole file; a successor reads it top to bottom
NEXT_STEPS_BYTES = 2048  # the hand-written "Next steps" section; the generated Now list carries the rest
RULINGS_SHOWN = 15
RULING_CHARS = 240
FOLLOWUPS_SHOWN = 30
ADHOC_CLOSED_SHOWN = 10


def _line(text: str) -> str:
    """`text` on one line, with `<` escaped so an unterminated `<!--` cannot hide the rest of the block."""
    return " ".join(text.split()).replace("<", "&lt;")


def _state(root: Path) -> dict:
    """The controller state, validated and in the current shape; a missing file is an empty state."""
    return ctlstate.load(root)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _tier(record: dict) -> str:
    return status_module.tier_of(record) + ("+fast" if status_module.is_fast(record) else "")


def _ruling_lines(root: Path) -> tuple[str, list[str]]:
    """(heading, lines) of the rulings window: the newest RULINGS_SHOWN, newest first, then where the rest are."""
    found = rulings_module.read(root)
    total = len(found)
    next_number = max((ruling["number"] for ruling in found), default=0) + 1
    lines = [_line(f"R{ruling['number']} ({ruling['date']}): {_clip(ruling['decision'], RULING_CHARS)}")
             for ruling in reversed(found[-RULINGS_SHOWN:])]
    if total > RULINGS_SHOWN:
        lines.append(f"{total - RULINGS_SHOWN} earlier rulings: program/rulings.md, read by number")
    return f"#### Rulings ({total}; next R{next_number})", lines


def _followup_lines(state: dict) -> tuple[str, list[str]]:
    """(heading, lines) of the open follow-ups: newest first, counted; the tagged ones only counted, by tag."""
    opened = sorted(state["followups"], key=lambda item: item["id"], reverse=True)
    shown = [item for item in opened if not item["tags"]]
    tags: dict[str, int] = {}
    for item in opened:
        for tag in item["tags"]:
            tags[tag] = tags.get(tag, 0) + 1
    deferred = len(opened) - len(shown)
    heading = f"#### Open follow-ups ({len(opened)} open"
    if deferred:
        heading += f"; {deferred} deferred by tag: " + ", ".join(f"{tag} {n}" for tag, n in sorted(tags.items()))
    lines = [f"{item['id']} {_line(item['area'])}: {_line(item['item'])}" for item in shown[:FOLLOWUPS_SHOWN]]
    if len(shown) > FOLLOWUPS_SHOWN:
        lines.append(f"and {len(shown) - FOLLOWUPS_SHOWN} older: pk followups list")
    return _line(heading) + ")", lines


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- none"]


def _commit(root: Path) -> str:
    try:
        return git(root, "rev-parse", "HEAD")
    except KitError:
        return "none"


def _worktree_line(root: Path, item: dict) -> str | None:
    path = Path(item["path"]).resolve()
    if path == Path(root).resolve():
        return None
    try:
        shown = path.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        shown = str(path)
    state = "missing" if item["missing"] else ("dirty" if item["dirty"] else "clean")
    return _line(f"{shown} ({item['branch'] or 'detached'}) head {item['head'][:7]}, {state}")


def render(root: Path, main: str = "main") -> str:
    """The generated block as Markdown ending in a newline."""
    root = Path(root)
    state = _state(root)
    records = status_module.list_all(root)
    counts = {name: 0 for name in status_module.STATUSES}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    unregistered: list[str] = []
    if (root / "program" / "register.yaml").exists():
        try:  # a hand-edited register of the wrong shape must be one error line, never a traceback
            in_register = {task_id for task_id, _ in trace_module._register_tasks(register_module.load(root))}
        except (TypeError, AttributeError, KeyError) as error:
            raise KitError(f"program/register.yaml is malformed ({type(error).__name__}); "
                           "run: pk register validate") from None
        recorded = {record["task_id"] for record in records}
        counts["not_started"] += len(in_register - recorded)  # no status record yet means not started
        unregistered = sorted(recorded - in_register)
    adhoc = {item["id"]: item for item in state["adhoc"]}
    unregistered = [task for task in unregistered if task not in adhoc]
    by_id = {record["task_id"]: record for record in records}
    now = [_line(state["now"])] if state["now"].strip() else []
    now.append(_line(_fingerprint_line(root)))
    listed = {item["id"] for item in state["adhoc"] if item["state"] != "closed"}  # the loop below prints these
    for r in records:
        if r["status"] not in IN_FLIGHT or r["task_id"] in listed:
            continue
        held = r["status"] + (f" by {', '.join(r['blocked_by'])}" if r["status"] == "blocked" and r["blocked_by"]
                              else "")
        held += ", ad hoc, tracked as closed" if r["task_id"] in adhoc else ""
        text = "; ".join(part for part in (state["stage"].get(r["task_id"], ""), r["note"]) if part.strip())
        now.append(f"[{_tier(r)}] {_line(r['task_id'])} ({_line(held)}): {_line(text) or '(no stage text)'}")
    for wanted in ("progress", "review", "paused", "queued"):
        for item in state["adhoc"]:
            if item["state"] != wanted:
                continue
            record = by_id.get(item["id"])
            held = f"; status {record['status']}, tier {_tier(record)}" if record else ""
            now.append(_line(f"{item['id']} {item['title']} (ad hoc, {wanted}{held}): {item['note'] or '(no note)'}"))
    now += _round_lines(root)
    closed = [item for item in reversed(state["adhoc"]) if item["state"] == "closed"]
    closed_lines = [_line(f"{item['id']} {item['title']} ("
                          + (f"status {by_id[item['id']]['status']}" if item["id"] in by_id else "no status record")
                          + ")" + (f": {item['note']}" if item["note"] else ""))
                    for item in closed[:ADHOC_CLOSED_SHOWN]]
    parked = dict(state["parked"])
    for r in records:
        if r["status"] == "parked":
            parked[r["task_id"]] = "; ".join(part for part in (r["note"], parked.get(r["task_id"], "")) if part.strip())
    done = [status_module.tier_of(r) for r in records if r["status"] == "done"]
    by_tier = ", ".join(f"{tier} {done.count(tier)}" for tier in status_module.TIERS if tier in done)
    budget = state.get("handoff_budget_bytes")
    problem = primary_branch_problem(root, main, need_commit=True)
    if problem:
        raise KitError(problem)
    branches = [_line(ref[len("refs/heads/"):]) for ref in git(
        root, "for-each-ref", "--format=%(refname)", f"--no-merged={main}", "refs/heads/").splitlines()]
    trees = [line for item in worktree_module.list_all(root) if (line := _worktree_line(root, item))]
    requests = [f"{c['id']}: {_line(c['title'])} ({'approved, not applied' if c['approved'] else 'awaiting approval'})"
                for c in change_module.open_requests(root)]
    followups_heading, followups = _followup_lines(state)
    rulings_heading, ruling_lines = _ruling_lines(root)
    over = [_line(line) for line in cost_module.flagged_lines(root)]
    interview_lines = _interview_lines(root)

    def tasks(table: dict[str, str]) -> list[str]:
        return [f"{_line(task)}: {_line(text)}" for task, text in sorted(table.items())]

    lines = ["### Generated state", "", f"{COMMIT_PREFIX}{_commit(root)}", "",
             *([f"Size budget: {budget} bytes, set in the controller state in place of the default "
                f"{BUDGET_BYTES} (pk record --handoff-budget)", ""] if budget is not None else []),
             "#### Now", *_bullets(now), "",
             *(["#### Interview", *_bullets(interview_lines), ""] if interview_lines else []),
             "#### Tasks by status",
             *[f"- {name}: {number}" + (f" ({by_tier})" if name == "done" and number else "")
               for name, number in counts.items()],
             *([f"- not in register: {', '.join(_line(t) for t in unregistered)}"] if unregistered else []), "",
             f"#### Branches not merged into {main}", *_bullets(branches), "",
             WORKTREES_HEADING, *_bullets(trees), "",
             "#### Queued", *_bullets(tasks(state["queued"])), "",
             "#### Parked", *_bullets(tasks(parked)), "",
             f"#### Ad-hoc tasks closed (newest {ADHOC_CLOSED_SHOWN} of {len(closed)})", *_bullets(closed_lines), "",
             followups_heading, *_bullets(followups), "",
             "#### Open change requests", *_bullets(requests), "",
             "#### Agents past the fresh-agent threshold", *_bullets(over), "",
             rulings_heading, *_bullets(ruling_lines)]
    block = "\n".join(lines) + "\n"
    if any(line.strip() in (START, END) for line in block.splitlines()):  # belt and braces: state text is escaped
        raise KitError("the generated block would contain a marker line; check controller-state.json")
    return block


IDS_SHOWN = 10


def _ids(items: list[str]) -> str:
    """`n (A, B, ...)`: a count and the first IDS_SHOWN ids."""
    shown = ", ".join(items[:IDS_SHOWN]) + (", ..." if len(items) > IDS_SHOWN else "")
    return f"{len(items)}" + (f" ({shown})" if items else "")


def _interview_lines(root: Path) -> list[str]:
    """The interview at a glance, for a takeover during stages 1 to 6; empty when there is no `program/ledger.yaml`.

    The stage `pk interview next` names, its topics closed and open, and over the whole ledger the blocking questions
    still unanswered and the assumptions not yet confirmed: what `pk interview next` and `report` would say first.
    """
    if not (Path(root) / "program" / "ledger.yaml").is_file():
        return []
    import interview  # late: only a program with a ledger pays for the coverage map

    try:
        coverage = interview.load_coverage(root)
        stage = interview.next_stage(root)
        entries = interview._load_ledger(root)["entries"]
        name = interview._stage(coverage, stage)["name"]
        if stage == interview.PER_TASK_STAGE:
            lines = [f"stage {stage} ({name}): discovery complete, stages 1 to 6 approved and current"]
        else:
            states = [state["state"] for state in interview.topic_states(root, stage).values()]
            applicable = [state for state in states if state != "not_applicable"]
            closed = sum(state in ("closed", "deferred") for state in applicable)
            lines = [f"stage {stage} ({name}): topics closed {closed} of {len(applicable)}, open "
                     f"{applicable.count('open')} (pk interview next, pk interview report {stage})"]
            changed = interview._changed_since_approval(root, stage)
            if changed:
                lines.append(f"stage {stage} was approved and its files changed: {', '.join(changed)}")
        unanswered = interview._unanswered(entries)
        blocking = [str(item.get("id")) for item in entries if item.get("kind") == "question"  # ledger order
                    and item.get("blocking") is True and str(item.get("id")) in unanswered]
        unconfirmed = [str(item.get("id")) for item in entries
                       if item.get("kind") == "assumption" and item.get("confirmed") is not True]
        lines += [f"blocking questions unanswered: {_ids(blocking)}", f"assumptions unconfirmed: {_ids(unconfirmed)}"]
    except KitError as exc:
        lines = [f"the interview state does not read: {exc}; run: pk interview validate"]
    except (TypeError, AttributeError, KeyError, ValueError) as exc:  # a hand-edited approval of the wrong shape
        lines = [f"the interview state does not read ({type(exc).__name__}); run: pk interview validate and "
                 "pk interview next"]
    return [_line(line) for line in lines]


def _round_lines(root: Path) -> list[str]:
    """One Now line per integration round that is not closed: checked or not, how many members landed."""
    folder = Path(root) / "program" / "rounds"
    lines = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            data = read_json(path)
        except KitError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("members"), list):
            lines.append(_line(f"round {path.stem}: its record program/rounds/{path.name} does not read"))
        elif not data.get("closed_at"):
            merged = data.get("merged") if isinstance(data.get("merged"), list) else []
            lines.append(_line(
                f"round {path.stem} (open): {'checked' if data.get('tree') else 'not checked yet'}, {len(merged)} of "
                f"{len(data['members'])} members merged; close it after the last one: pk integrate close {path.stem}"))
    return lines


_BEHIND = re.compile(r"(?<=^- Fingerprint: )(.*), \d+ commits behind(?=\r?\n?\Z)")


WORKTREES_HEADING = "#### Task worktrees"


def _comparable(block: str, live: bool = False) -> str:
    """The block without the commit line and without the fingerprint's distance: committing the hand-off moves HEAD.

    With `live`, the lines under the Task worktrees heading are left out as well (KF-14): each records a head and a
    clean or dirty state, and gates and agents add and remove short-lived worktrees, so any commit or running agent
    changed them. `check` compares this way; `update` still writes the lines as they are now.
    """
    kept, skipping = [], False
    for line in block.splitlines(keepends=True):
        if line.startswith(COMMIT_PREFIX):
            continue
        if live and line.rstrip("\r\n") == WORKTREES_HEADING:
            kept.append(line)
            skipping = True
            continue
        if skipping:
            if line.startswith("- "):
                continue
            skipping = False
        kept.append(_BEHIND.sub(r"\1", line))
    return "".join(kept)


def _fingerprint_line(root: Path) -> str:
    import fingerprint  # late: fingerprint imports gate, which imports modules that import this one
    try:
        record = fingerprint.current_fingerprint(root)
    except KitError:
        record = None
    if record is None:
        return "Fingerprint: none"
    rows = [r for r in record["commands"] if isinstance(r, dict)]
    good = sum(r.get("as_expected") is True for r in rows)
    state = "complete" if record.get("complete") is True else "incomplete"
    return (f"Fingerprint: {record.get('head12', record['head'][:12])}, {state}, {good} of {len(rows)} as expected, "
            f"{fingerprint.commits_behind(root, record)} commits behind")


def _marker(text: str, marker: str) -> re.Match:
    found = list(re.finditer(r"^[ \t]*" + re.escape(marker) + r"[ \t]*(?:\r?\n|\Z)", text, re.MULTILINE))
    if not found:
        raise KitError(f"program/HANDOFF.md: marker line {marker} not found")
    if len(found) > 1:
        raise KitError(f"program/HANDOFF.md: marker line {marker} appears {len(found)} times")
    return found[0]


def _split(text: str) -> tuple[int, int]:
    """Offsets of the generated block: just after the start marker line, and the start of the end marker line."""
    start, end = _marker(text, START), _marker(text, END)
    if end.start() < start.end():
        raise KitError("program/HANDOFF.md: the generated markers are out of order (end before start)")
    return start.end(), end.start()


def _read(root: Path) -> tuple[Path, str]:
    path = Path(root) / "program" / "HANDOFF.md"
    try:
        return path, read_text(path, newline="")  # line endings as they are: the file is written back
    except KitError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            raise KitError("program/HANDOFF.md not found; run: pk init") from None
        raise


def never_generated(root: Path) -> bool:
    """True when nothing was ever written between the markers: a new program, before its first `pk handoff update`."""
    _, text = _read(root)
    begin, finish = _split(text)
    return not text[begin:finish].strip()


def check(root: Path) -> bool:
    """True when the block on disk equals the block rendered now, ignoring the commit line and the live worktree lines."""
    _, text = _read(root)
    begin, finish = _split(text)
    return _comparable(text[begin:finish], live=True) == _comparable(render(root), live=True)


def budget_problems(root: Path) -> list[str]:
    """Why the hand-off is too big to be read whole, one line each (empty when it is within budget)."""
    _, text = _read(root)
    budget = _state(root).get("handoff_budget_bytes", BUDGET_BYTES)
    size = len(text.encode("utf-8"))
    problems = []
    if size > budget:
        problems.append(f"program/HANDOFF.md is {size} bytes, over its budget of {budget}: history belongs in "
                        "program/rulings.md, closed items in the closed lists; close or tag follow-ups and "
                        "trim the hand-written sections")
    section = re.search(r"^## Next steps[^\n]*\n(.*?)(?=^## |^[ \t]*" + re.escape(START) + r"|\Z)", text,
                        re.MULTILINE | re.DOTALL)
    if section and len(section.group(1).encode("utf-8")) > NEXT_STEPS_BYTES:
        problems.append(f"program/HANDOFF.md: the Next steps section is {len(section.group(1).encode('utf-8'))} "
                        f"bytes, over {NEXT_STEPS_BYTES}: the generated Now list carries what is in flight; keep "
                        "only the order of what comes next")
    return problems


def update(root: Path) -> bool:
    """Rewrite the text between the markers; return whether the file changed.

    Missing, duplicated or out-of-order markers raise KitError and nothing is written.
    """
    path, text = _read(root)
    begin, finish = _split(text)
    block = render(root)
    if _comparable(text[begin:finish]) == _comparable(block):
        return False
    write_text(path, text[:begin] + block + text[finish:])
    return True


# --- command -----------------------------------------------------------------

def cmd_handoff(args: argparse.Namespace) -> int:
    action = getattr(args, "handoff_action", None)
    if action not in ("update", "check"):
        raise KitError("handoff needs an action: update or check")
    root = find_root()
    if action == "update":
        print("handoff: updated" if update(root) else "handoff: unchanged")
    elif not check(root):  # check renders first, so a state that does not read is reported before this
        if never_generated(root):  # a new program: not stale, just never written (install and init commit nothing)
            raise KitError("program/HANDOFF.md has no generated block yet (a new program); run: pk handoff update, "
                           "then commit it")
        raise KitError("program/HANDOFF.md is stale; run: pk handoff update")
    else:
        problems = budget_problems(root)
        if problems:
            raise KitError("; ".join(problems))
        print("handoff: current")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="handoff_action", metavar="action", parser_class=type(parser))
    actions.add_parser("update", help="rewrite the generated block of program/HANDOFF.md")
    actions.add_parser("check", help="exit 1 when the generated block is stale or the file is over its size budget")


def register(add_command) -> None:
    add_command("handoff", cmd_handoff, help="the generated part of program/HANDOFF.md: update, check",
                configure=_configure)
