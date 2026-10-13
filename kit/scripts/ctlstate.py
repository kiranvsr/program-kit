#!/usr/bin/env python3
"""The controller state, `program/controller-state.json`: one reader and one writer (standard library only).

The file holds what the controller tracks outside the status records:

    stage, queued, parked    {task ID: text}                     notes on register tasks
    adhoc                    [{id, title, state, note, ruling, updated}]   work that is not in the register
    followups                [{id, area, item, added, tags}]     open follow-ups, F-0001 onward
    closed_followups         the same plus {closed, reason, by, duplicate_of}
    followups_issued         how many follow-up ids were ever given out: the next one is that plus one
    now                      one short line: what the controller is doing
    handoff_budget_bytes     optional override of the hand-off size budget (`pk record --handoff-budget`)

`load` validates and normalises: a 0.2 file (follow-ups as bare {area, item} pairs, no ad-hoc list) reads as
the new shape, with ids given in file order and the added date `unknown`; nothing is written until a command
changes the state. An ad-hoc task's state is the controller's tracking word, never an acceptance: it ends at
`closed`, and `done` stays the word only QA can write, in the status record.

A follow-up is closed, never deleted, and its number is never given out twice: `load` refuses a file in which
any id from F-0001 to the last one issued is in neither list.
"""
from __future__ import annotations

import re
from pathlib import Path

from common import KitError, read_json, write_json

STATE_FILE = "program/controller-state.json"
ADHOC_STATES = ("queued", "progress", "review", "paused", "closed")
ADHOC_ID = re.compile(r"AH-(\d{4})\Z")
FOLLOWUP_ID = re.compile(r"F-(\d{4})\Z")
TAG = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
UNKNOWN_DATE = "unknown"
_KEYS = ("stage", "queued", "parked", "adhoc", "followups", "closed_followups", "followups_issued", "now",
         "handoff_budget_bytes")


def _mapping(data: dict, key: str) -> dict[str, str]:
    value = data.get(key, {})
    if not (isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())):
        raise KitError(f"{STATE_FILE}: '{key}' must be a mapping of task ID to text")
    return dict(value)


def _text(item: dict, key: str, default: str = "") -> str:
    value = item.get(key, default)
    return value if isinstance(value, str) else default


def _followups(data: dict, key: str, taken: set[int]) -> list[dict]:
    items = data.get(key, [])
    if not (isinstance(items, list) and all(
            isinstance(item, dict) and isinstance(item.get("area"), str) and isinstance(item.get("item"), str)
            for item in items)):
        raise KitError(f"{STATE_FILE}: '{key}' must be a list of follow-ups, each with an area and an item")
    found = []
    for item in items:
        number = FOLLOWUP_ID.match(item["id"]) if isinstance(item.get("id"), str) else None
        if "id" in item and (number is None or int(number.group(1)) in taken):
            raise KitError(f"{STATE_FILE}: '{key}' has a missing, malformed or repeated id: {item.get('id')!r}")
        if number is not None:
            taken.add(int(number.group(1)))
        tags = item.get("tags", [])
        if not (isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)):
            raise KitError(f"{STATE_FILE}: '{key}': tags must be a list of text")
        entry = {"id": item.get("id"), "area": item["area"], "item": item["item"],
                 "added": _text(item, "added", UNKNOWN_DATE), "tags": list(tags)}
        if key == "closed_followups":
            entry.update(closed=_text(item, "closed", UNKNOWN_DATE), reason=_text(item, "reason"),
                         by=_text(item, "by"), duplicate_of=_text(item, "duplicate_of"))
        found.append(entry)
    return found


def _adhoc(data: dict) -> list[dict]:
    items = data.get("adhoc", [])
    if not (isinstance(items, list) and all(isinstance(item, dict) for item in items)):
        raise KitError(f"{STATE_FILE}: 'adhoc' must be a list of ad-hoc tasks")
    found, seen = [], set()
    for item in items:
        task_id, state = item.get("id"), item.get("state", "queued")
        if not (isinstance(task_id, str) and ADHOC_ID.match(task_id)) or task_id in seen:
            raise KitError(f"{STATE_FILE}: 'adhoc' has a missing, malformed or repeated id: {task_id!r} "
                           "(expected AH-0001 onward)")
        if state not in ADHOC_STATES or not isinstance(item.get("title"), str) or not item["title"].strip():
            raise KitError(f"{STATE_FILE}: 'adhoc' {task_id} needs a title and a state of {', '.join(ADHOC_STATES)}")
        seen.add(task_id)
        found.append({"id": task_id, "title": item["title"], "state": state, "note": _text(item, "note"),
                      "ruling": _text(item, "ruling"), "updated": _text(item, "updated", UNKNOWN_DATE)})
    return found


def load(root: Path) -> dict:
    """The controller state, validated and in the current shape; a missing file is an empty state."""
    path = Path(root) / STATE_FILE
    data = read_json(path) if path.exists() else {}
    if not isinstance(data, dict):
        raise KitError(f"{STATE_FILE}: expected a mapping with stage, queued, parked and followups")
    taken: set[int] = set()
    closed = _followups(data, "closed_followups", taken)
    followups = _followups(data, "followups", taken)
    for entry in closed + followups:
        if entry["id"] is None:  # a 0.2 follow-up: the next free number, in file order
            number = max(taken, default=0) + 1
            taken.add(number)
            entry["id"] = f"F-{number:04d}"
    issued = data.get("followups_issued", max(taken, default=0))
    if not isinstance(issued, int) or isinstance(issued, bool) or issued < max(taken, default=0):
        raise KitError(f"{STATE_FILE}: 'followups_issued' must be the number of the last follow-up id given out")
    gone = [f"F-{number:04d}" for number in range(1, issued + 1) if number not in taken]
    if gone:
        raise KitError(f"{STATE_FILE}: follow-up(s) {', '.join(gone[:5])}{' and more' if len(gone) > 5 else ''} "
                       f"are in neither the open nor the closed list ({issued} were issued): a follow-up is "
                       "closed with a reason (pk followups close), never deleted; restore the file from git")
    now = data.get("now", "")
    budget = data.get("handoff_budget_bytes")
    if not isinstance(now, str):
        raise KitError(f"{STATE_FILE}: 'now' must be one line of text")
    if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0):
        raise KitError(f"{STATE_FILE}: 'handoff_budget_bytes' must be a positive whole number")
    state = {"stage": _mapping(data, "stage"), "queued": _mapping(data, "queued"),
             "parked": _mapping(data, "parked"), "adhoc": _adhoc(data), "followups": followups,
             "closed_followups": closed, "followups_issued": issued, "now": now}
    if budget is not None:
        state["handoff_budget_bytes"] = budget
    return state


def save(root: Path, state: dict) -> None:
    """Write the state in the current shape, keys in a fixed order."""
    write_json(Path(root) / STATE_FILE, {key: state[key] for key in _KEYS if key in state})


def next_followup_id(state: dict) -> str:
    """Give out the next follow-up id and count it as issued, so the number is never used again."""
    numbers = [int(FOLLOWUP_ID.match(item["id"]).group(1)) for item in state["followups"] + state["closed_followups"]]
    state["followups_issued"] = max([state.get("followups_issued", 0), *numbers]) + 1
    return f"F-{state['followups_issued']:04d}"


def next_adhoc_id(state: dict) -> str:
    numbers = [int(ADHOC_ID.match(item["id"]).group(1)) for item in state["adhoc"]]
    return f"AH-{max(numbers, default=0) + 1:04d}"


def one_line(text: str, what: str, limit: int = 0) -> str:
    """`text` on one line with single spaces; KitError when it is empty or (with `limit`) too long."""
    text = " ".join(str(text).split())
    if not text:
        raise KitError(f"{what} must not be empty")
    if limit and len(text) > limit:
        raise KitError(f"{what} is {len(text)} characters; keep it under {limit}")
    return text
