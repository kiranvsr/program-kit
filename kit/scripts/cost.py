#!/usr/bin/env python3
"""The cost log, `program/costs.jsonl`: one line per subagent run, append-only (standard library only).

`pk cost add` refuses a task that is neither in the register nor an ad-hoc task (`unknown_task`) unless
`--allow-unknown` is given. `add` appends one JSON object: when, task, role, agent (a label for one agent session,
so its resumes add up), model, tokens, tool calls, seconds and whether the run resumed an earlier one. The model
is required: every dispatch names the model it ran on. Earlier lines are never rewritten. `totals` sums per task;
`flagged` names each agent of a task that has passed the fresh-agent threshold (TOKEN_THRESHOLD tokens in all,
or RESUME_THRESHOLD resumes): past it the controller stops resuming that agent and starts a fresh one with a
tight fix brief, because an agent that has read that much re-reads it on every turn.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import KitError, find_root, read_text, utc_now

COST_FILE = "program/costs.jsonl"
TOKEN_THRESHOLD = 400_000
RESUME_THRESHOLD = 3
_KEYS = ("at", "task", "role", "agent", "model", "tokens", "tool_calls", "seconds", "resume")
_TEXT = ("at", "task", "role", "agent", "model")
_NUMBERS = ("tokens", "tool_calls", "seconds")


def _word(value: str, what: str) -> str:
    value = " ".join(str(value).split())
    if not value:
        raise KitError(f"a cost record needs {what}")
    return value


def _count(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KitError(f"{what} must be a whole number, zero or more")
    return value


def read(root: Path) -> list[dict]:
    """Every record of the log, oldest first; a line that is not a record is a KitError naming it."""
    path = Path(root) / COST_FILE
    if not path.exists():
        return []
    found = []
    for number, line in enumerate(read_text(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise KitError(f"{COST_FILE} line {number}: not JSON: {exc}") from exc
        if not (isinstance(record, dict) and all(key in record for key in _KEYS)
                and all(isinstance(record[key], str) for key in _TEXT)
                and all(isinstance(record[key], int) and not isinstance(record[key], bool) for key in _NUMBERS)
                and isinstance(record["resume"], bool)):
            raise KitError(f"{COST_FILE} line {number}: not a cost record ({', '.join(_KEYS)})")
        found.append(record)
    return found


def add(root: Path, task: str, role: str, model: str, tokens: int, tool_calls: int = 0, seconds: int = 0,
        agent: str = "", resume: bool = False) -> dict:
    """Append one run and return its record; nothing is written on a refusal."""
    role = _word(role, "a role")
    record = {"at": utc_now(), "task": _word(task, "a task"), "role": role,
              "agent": _word(agent or role, "an agent label"),
              "model": _word(model, "the model the agent ran on (every dispatch names its model)"),
              "tokens": _count(tokens, "tokens"), "tool_calls": _count(tool_calls, "tool calls"),
              "seconds": _count(seconds, "seconds"), "resume": bool(resume)}
    read(root)  # a log that does not read is repaired first, never appended to
    path = Path(root) / COST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        raise KitError(f"cannot write {path}: {exc.strerror or exc}") from exc
    return record


def unknown_task(root: Path, task: str) -> str | None:
    """Why `task` is not a task a cost line may name, or None: it must be in `program/register.yaml` or be an ad-hoc
    task `program/controller-state.json` lists. `pk cost add` refuses an unknown one unless `--allow-unknown`, so a
    typo, or an ad-hoc ID logged before the task was added, does not leave an orphan line. Lines already in the
    log are never checked: the log is read as it is."""
    import status as status_module  # lazy: status imports the register, which nothing here needs otherwise

    task = " ".join(str(task).split())
    try:
        known = status_module._registered(root)
    except KitError:
        if (Path(root) / "program" / "register.yaml").is_file():
            raise  # a register that does not read is its own error, not an unknown task
        known = set()  # no register yet (discovery): only ad-hoc tasks are known
    if task in known:
        return None
    return (f"--task {task} is not a task in program/register.yaml or an ad-hoc task in program/controller-state.json; "
            "check the ID, add the ad-hoc task first (pk adhoc add, or pk record --adhoc), or pass --allow-unknown "
            "to log it anyway")


def totals(records: list[dict]) -> dict[str, dict]:
    """{task: {tokens, tool_calls, seconds, runs}} in first-seen order."""
    found: dict[str, dict] = {}
    for record in records:
        row = found.setdefault(record["task"], {"tokens": 0, "tool_calls": 0, "seconds": 0, "runs": 0})
        for key in _NUMBERS:
            row[key] += record[key]
        row["runs"] += 1
    return found


def flagged(records: list[dict], tokens: int = TOKEN_THRESHOLD, resumes: int = RESUME_THRESHOLD) -> list[dict]:
    """Each (task, agent) past the threshold: {task, agent, model, tokens, runs, resumes}, in first-seen order."""
    agents: dict[tuple[str, str], dict] = {}
    for record in records:
        row = agents.setdefault((record["task"], record["agent"]),
                                {"task": record["task"], "agent": record["agent"], "model": record["model"],
                                 "tokens": 0, "runs": 0, "resumes": 0})
        row["tokens"] += record["tokens"]
        row["runs"] += 1
        row["resumes"] += 1 if record["resume"] else 0
        row["model"] = record["model"]
    return [row for row in agents.values() if row["tokens"] > tokens or row["resumes"] >= resumes]


def _flag_line(row: dict) -> str:
    return (f"{row['task']} {row['agent']} ({row['model']}): {row['tokens']} tokens over {row['runs']} run(s), "
            f"{row['resumes']} resume(s): start a fresh agent with a tight fix brief")


def flagged_lines(root: Path) -> list[str]:
    """The flagged agents at the default threshold, one line each (what the hand-off shows)."""
    return [_flag_line(row) for row in flagged(read(root))]


# --- command -----------------------------------------------------------------

def cmd_cost(args: argparse.Namespace) -> int:
    action = getattr(args, "cost_action", None)
    root = find_root()
    if action == "add":
        problem = None if args.allow_unknown else unknown_task(root, args.task)
        if problem:
            raise KitError(problem)
        record = add(root, args.task, args.role, args.model, args.tokens, args.tools, args.seconds,
                     agent=args.agent, resume=args.resume)
        print(f"cost: {record['task']} {record['agent']} ({record['model']}) {record['tokens']} tokens")
        for row in flagged(read(root)):
            if (row["task"], row["agent"]) == (record["task"], record["agent"]):
                print("past the threshold: " + _flag_line(row))
    elif action == "report":
        records = [r for r in read(root) if args.task in (None, r["task"])]
        for task, row in totals(records).items():
            print(f"{task}\t{row['tokens']} tokens\t{row['tool_calls']} tool calls\t{row['seconds']} s\t"
                  f"{row['runs']} run(s)")
        for row in flagged(records, args.tokens, args.resumes):
            print("past the threshold: " + _flag_line(row))
    else:
        raise KitError("cost needs an action: add or report")
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="cost_action", metavar="action", parser_class=type(parser))
    adder = actions.add_parser("add", help="append one subagent run to program/costs.jsonl")
    adder.add_argument("--task", required=True, help="task ID (register or ad hoc)")
    adder.add_argument("--role", required=True, help="implementer, qa, spec_qa, ...")
    adder.add_argument("--model", required=True, help="the model the agent ran on")
    adder.add_argument("--tokens", required=True, type=int)
    adder.add_argument("--tools", type=int, default=0, help="tool calls")
    adder.add_argument("--seconds", type=int, default=0)
    adder.add_argument("--agent", default="", help="a label for this agent session (default: the role)")
    adder.add_argument("--resume", action="store_true", help="this run resumed the same agent")
    adder.add_argument("--allow-unknown", action="store_true", dest="allow_unknown",
                       help="log a task ID that is neither in the register nor an ad-hoc task")
    report = actions.add_parser("report", help="totals per task, and the agents past the fresh-agent threshold")
    report.add_argument("--task")
    report.add_argument("--tokens", type=int, default=TOKEN_THRESHOLD, help="token threshold per agent")
    report.add_argument("--resumes", type=int, default=RESUME_THRESHOLD, help="resume threshold per agent")


def register(add_command) -> None:
    add_command("cost", cmd_cost, help="the cost log: add, report", configure=_configure)
