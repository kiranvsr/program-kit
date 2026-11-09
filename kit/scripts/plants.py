#!/usr/bin/env python3
"""`pk gate plants`: judge the deferred planted-bug rows of a task's latest results record, as kit records.

A producer may mark planted-bug rows `defer: true`. The hand-back gate then records those rows with their full
definition and a definition digest but no verdict, and judges only the others (`Evidence.planted_bugs`). This
command judges deferred rows afterwards, in chunks, at any time after the gate (while QA reviews, too):

    pk gate plants <ID> --branch task/<ID> (--rows ID,ID,... | --all-pending) [--workers N] [--rejudge]
    pk gate plants <ID> --branch task/<ID> --all-pending --chunk-seconds N [--per-row-seconds S] [--plan | --chunk K/M]
    pk gate plants <ID> --branch task/<ID> --adopt

- The rows are judged at the `head` of the task's latest results record, never at the branch tip, each in a clean
  detached mirror of that commit, with the 0.5.2 pool: `--workers` (else PK_PLANT_WORKERS, else
  `evidence.plant_workers` of main's checks.yaml, else 1) rows at once, `serial: true` rows alone after the pool,
  every pool verdict that is not a clean kill judged again alone. A kill needs a passing build and a non-zero test
  exit, as in the gate.
- The definitions come from the results record, never from the producer: its commands are not run again. A row
  whose definition no longer matches its digest is refused, and so is a row that is not deferred, or that a plants
  record bound to the same record (or one it reused) already judged killed, unless `--rejudge`.
- Chunks (0.6.0): `--all-pending --chunk-seconds N [--per-row-seconds S]` splits the pending rows, in table order,
  into chunks estimated to fit N seconds each and judges them in order, one plants record per chunk, stopping at the
  first chunk that is not all killed (exit 1, naming what remains and the command that goes on). A chunk of rows is
  estimated as ceil(pool rows / workers) x S plus S per `serial` row (`row_slots`), and holds one row at least. S,
  the seconds of one row slot, comes from `--per-row-seconds`, else the measured time of this task's plants records
  (their `seconds` over their row slots at their `workers`), else of the program's other tasks' plants records, else
  the results record's `planted_bugs_timing`, else `evidence.plant_row_seconds` of main's checks.yaml, else
  gate.ROW_SECONDS; the run says which. (A fixed 45 s in the first version of this in the program it came from
  under-split badly: its rows took about 113 s a slot at 3 workers.)
- Several machines: `--plan` cuts ALL deferred rows of the record (killed ones too) the same way, so chunk K of M is
  the same everywhere, and prints each chunk with its rows, estimate and exact command, pinning S and the workers;
  it judges nothing. `--chunk K/M` judges chunk K's rows not killed yet, and refuses when the plan with these
  settings does not have M chunks. Each machine writes its own plants record and its own copy of the pointer.
- Adopting (0.6.0): `pk gate plants <ID> --branch task/<ID> --adopt` lists in this pointer every plants record file
  of `program/evidence/<ID>/plants/` it does not list yet (one judged elsewhere and copied here, never its pointer),
  after checking its name and digest, its schema, its task, that it is bound to the latest results record or one it
  reused and judged at that record's head, and that its rows are deferred rows of it; anything else refuses the
  whole adoption. `pk remote plants` copies new plants records back and adopts them.
- The verdicts are appended through the store as a new immutable record of kind `plants`,
  `program/evidence/<ID>/plants/<utc>-<sha256[:12]>.json`, in the task worktree, and listed in the task's pointer.
  It names the task, the results record it judged (`part_of`: path and digest), the head, the kit version and each
  row's verdict in the gate's row schema with the row's definition digest. The implementer commits it with the
  pointer on the task branch; after the reviewed commit, merge accepts records directly in `plants/`.
- QA's acceptance (`pk evidence accept`), `pk status set <ID> done` and `pk merge` need every deferred row killed in
  such a record.

The kit takes no machine lock here, as in the gate; a program that serialises heavy runs wraps this command in
`pk lock run <owner> -- ...` (0.6.0), or runs it on another machine with `pk remote plants`.
"""
from __future__ import annotations

import math
import re
import time
from pathlib import Path

import evidence as evidence_module
import gate
import latework
import store
from common import KitError, git, utc_now

KIT_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_CHUNK = re.compile(r"([0-9]+)/([0-9]+)\Z")


def kit_version() -> str:
    try:
        return KIT_VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _worktree(root: Path, task_id: str, branch: str, override) -> Path:
    """The task worktree; uncommitted files the kit writes for the task (an earlier chunk's record) are allowed."""
    return gate._task_worktree(root, task_id, branch, override, allow=lambda path: latework.own_path(path, task_id),
                               command="pk gate plants")


def _checked_rows(root: Path, head: str, rows: list[dict]) -> None:
    """KitError unless each deferred row's definition matches its digest and its edit applies once at `head`."""
    for row in rows:
        rid = gate._show(row["id"])
        if evidence_module.definition_digest(row) != row["definition_sha256"]:
            raise KitError(f"deferred row {rid}: its definition does not match its definition_sha256; nothing judged")
        path = evidence_module.Evidence._norm(row["path"])
        listing = git(root, "ls-tree", head, "--", path).split(None, 3)
        if not listing or listing[1] != "blob" or listing[0] not in ("100644", "100755"):
            raise KitError(f"deferred row {rid}: {gate._show(path)} is not a regular file at {head}")
        text = gate._git_bytes(root, "show", f"{head}:{path}").decode("utf-8", "replace")
        count = text.count(row["find"])
        if count != 1:
            raise KitError(f"deferred row {rid}: find occurs {count} times in {gate._show(path)} at {head} (it must "
                           "occur exactly once)")


def select(data: dict, coverage: dict, names: list[str] | None, all_pending: bool, rejudge: bool) -> list[dict]:
    """The deferred rows to judge, in table order; KitError for a name that is unknown, not deferred, or killed."""
    rows = [row for row in data["planted_bugs"] if row.get("id") != evidence_module.SELF_TEST_ID]
    deferred = {row["id"]: row for row in rows if evidence_module.is_deferred(row)}
    if all_pending:
        wanted = [rid for rid in coverage["pending"]]
    else:
        wanted = list(dict.fromkeys(name for name in names or [] if name))
        if not wanted:
            raise KitError("name the rows to judge: --rows ID[,ID...], or --all-pending")
        known = {row["id"] for row in rows}
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise KitError(f"no planted-bug row {', '.join(map(gate._show, unknown))} in the record")
        judged = [name for name in wanted if name not in deferred]
        if judged:
            raise KitError(f"{', '.join(map(gate._show, judged))}: not deferred; the gate judged "
                           f"{'it' if len(judged) == 1 else 'them'} (pk gate rejudge re-runs a judged row as feedback)")
        done = [name for name in wanted if name in coverage["killed"]]
        if done and not rejudge:
            raise KitError("already judged killed in a plants record bound to this record: "
                           + ", ".join(f"{gate._show(name)} ({coverage['killed'][name]})" for name in done)
                           + "; pass --rejudge to judge again")
    return [deferred[rid] for rid in deferred if rid in wanted]


def parse_chunk(value) -> tuple[int, int]:
    """(K, M) of a `--chunk K/M` argument; KitError unless 1 <= K <= M."""
    match = _CHUNK.match(value) if isinstance(value, str) else None
    if match is None or not 1 <= int(match.group(1)) <= int(match.group(2)):
        raise KitError(f"--chunk must be K/M, chunk K of a plan of M chunks (1 <= K <= M), not {gate._show(value)}")
    return int(match.group(1)), int(match.group(2))


def row_slots(rows: list[dict], workers: int) -> int:
    """The row slots a chunk of `rows` takes: ceil(pool rows / workers), plus one per `serial` row (run alone)."""
    serial = sum(1 for row in rows if row.get("serial"))
    return math.ceil((len(rows) - serial) / workers) + serial


def estimate(rows: list[dict], workers: int, per_row: float) -> float:
    """The seconds a chunk of `rows` is estimated to take: its row slots times the seconds of one slot."""
    return row_slots(rows, workers) * per_row


def split(rows: list[dict], workers: int, per_row: float, limit: int) -> list[list[dict]]:
    """`rows` in table order as consecutive chunks whose estimate fits `limit` seconds (each holds one row at
    least, so a row estimated above the limit is a chunk of its own)."""
    chunks, current = [], []
    for row in rows:
        if current and estimate([*current, row], workers, per_row) > limit:
            chunks.append(current)
            current = []
        current.append(row)
    if current:
        chunks.append(current)
    return chunks


def _measured(wt: Path, task_ids: list[str]) -> tuple[float, int] | None:
    """(seconds per row slot, records) over the valid plants records the pointers of `task_ids` list, or None.

    Each record says how long it took (`seconds`) for its rows at its `workers`; the slots are counted as in
    `row_slots`, and the seconds of every record are divided by the slots of every record. A pointer or record
    that does not read is skipped: a measurement is a default, never a check."""
    seconds, slots, count = 0.0, 0, 0
    for task_id in task_ids:
        try:
            items = store.read_plants(wt, task_id)
        except KitError:
            continue
        for item in items:
            try:
                record = store.load(wt, item["path"], item["digest"])
            except KitError:
                continue
            if evidence_module.validate_plants(record) or record["seconds"] <= 0:
                continue
            seconds += record["seconds"]
            slots += row_slots(record["rows"], record["workers"])
            count += 1
    return (seconds / slots, count) if slots else None


def _other_tasks(wt: Path, task_id: str) -> list[str]:
    base = wt / store.BASE
    found = sorted(path.parent.name for path in base.glob(f"*/{store.POINTER}")) if base.is_dir() else []
    return [name for name in found if name != task_id]


def per_row_seconds(root, wt, task_id: str, entry: dict, data: dict, explicit: float | None = None) -> tuple[float, str]:
    """(seconds of one row slot, where they came from) for `--chunk-seconds`, in this order: `explicit`
    (`--per-row-seconds`); the measured time of this task's plants records; of the program's other tasks' plants
    records; the `planted_bugs_timing` of the results record (seconds / ceil(rows / workers)); the checks.yaml
    `evidence.plant_row_seconds` on main; else gate.ROW_SECONDS. A measured value is rounded to 0.1 s, so the
    command a plan prints reproduces the plan exactly."""
    if explicit is not None:
        return explicit, "--per-row-seconds"
    wt = Path(wt)
    own = _measured(wt, [task_id])
    if own is not None:
        return _rounded(own[0]), f"measured in {own[1]} plants record(s) of {task_id}"
    others = _measured(wt, _other_tasks(wt, task_id))
    if others is not None:
        return _rounded(others[0]), f"measured in {others[1]} plants record(s) of the program's other tasks"
    timing = data.get("planted_bugs_timing")
    if isinstance(timing, dict) and timing.get("seconds", 0) > 0:
        return (_rounded(timing["seconds"] / math.ceil(timing["rows"] / timing["workers"])),
                f"planted_bugs_timing of {entry['path']}")
    configured = evidence_module.config_at(root)["plant_row_seconds"]
    if configured is not None:
        return configured, "evidence.plant_row_seconds in program/checks.yaml"
    return gate.ROW_SECONDS, "no measurement: the default"


def _rounded(seconds: float) -> float:
    return max(0.1, round(seconds, 1))


def _seconds(value: float) -> str:
    """A number of seconds as the shortest text that reads back as the same value (`5`, `113.2`)."""
    return f"{value:g}" if float(f"{value:g}") == value else repr(value)


def _context(root: Path, task_id: str, branch: str, worktree) -> dict:
    """The task worktree, its latest results record (entry and data, checked) and the plants coverage of it."""
    wt = _worktree(root, task_id, branch, worktree)
    entry = store.read_pointer(wt, task_id).get("results")
    if entry is None:
        raise KitError(f"{store.pointer_path(task_id)} names no results record: run the hand-back gate first")
    data = store.load(wt, entry["path"], entry["digest"])
    problems = evidence_module.validate_results(data)
    if problems:
        raise KitError(f"{entry['path']}: {problems[0]}")
    if data["task_id"] != task_id:
        raise KitError(f"{entry['path']} is a record of {gate._show(data['task_id'])}, not of {task_id}")
    return {"wt": wt, "entry": entry, "data": data}


def _checked_context(root: Path, task_id: str, branch: str, worktree) -> dict:
    """`_context`, refusing a record that deferred nothing or whose head is not a commit here, with its coverage."""
    found = _context(root, task_id, branch, worktree)
    wt, entry, data = found["wt"], found["entry"], found["data"]
    if not any(evidence_module.is_deferred(row) for row in data["planted_bugs"]):
        raise KitError(f"{entry['path']} deferred no planted-bug rows: the gate judged every row; nothing to do")
    try:
        git(wt, "cat-file", "-e", f"{data['head']}^{{commit}}")
    except KitError:
        raise KitError(f"the head {data['head']} of {entry['path']} is not a commit of this repository") from None
    coverage = evidence_module.plants_coverage(wt, task_id, entry, data)
    if coverage["problems"]:
        raise KitError(coverage["problems"][0])
    found["coverage"] = coverage
    return found


def chunk_plan(root, task_id: str, branch: str, chunk_seconds: int, per_row: float | None = None,
               workers: int | None = None, worktree=None) -> dict:
    """The plan of `--chunk-seconds` over ALL deferred rows of the task's latest results record, in table order, so
    chunk K of M is the same on every machine that uses the same settings: {chunks: [[row]], per_row, source,
    workers, killed: {id: path}, entry, context}. Judges nothing."""
    root = Path(root).resolve()
    found = _checked_context(root, task_id, branch, worktree)
    count = evidence_module.plant_workers(root, workers)
    seconds, source = per_row_seconds(root, found["wt"], task_id, found["entry"], found["data"], per_row)
    deferred = [row for row in found["data"]["planted_bugs"] if evidence_module.is_deferred(row)]
    return {"chunks": split(deferred, count, seconds, chunk_seconds), "per_row": seconds, "source": source,
            "workers": count, "killed": found["coverage"]["killed"], "entry": found["entry"], "context": found}


def _judge(found: dict, task_id: str, rows: list[dict], count: int, rejudge: bool, timeout) -> tuple[dict, str]:
    """Judge `rows` at the record's head with `count` workers and append one plants record; (record, its path)."""
    wt, entry, head = found["wt"], found["entry"], found["data"]["head"]
    gate._out(f"plants: judging {len(rows)} deferred row(s) of {entry['path']} at its head {head} with {count} "
              f"worker(s)")
    runner = evidence_module.Evidence.judging_at(wt, head)
    items = [{"id": row["id"], "path": row["path"], "find": row["find"], "replace": row["replace"],
              "build_argv": row["build_argv"], "test_argv": row["test_argv"], "serial": row["serial"],
              "test_args": []} for row in rows]
    started = time.monotonic()
    judged = evidence_module._PlantPool(runner, items, None, [], timeout, count, self_test=False).run()
    seconds = round(time.monotonic() - started, 1)
    out = [{**verdict, "serial": row["serial"], "definition_sha256": row["definition_sha256"]}
           for verdict, row in zip(judged, rows)]
    record = {"task_id": task_id, "kind": store.PLANTS, "part_of": {"path": entry["path"], "digest": entry["digest"]},
              "head": head, "kit_version": kit_version(), "produced_at": utc_now(), "workers": count,
              "seconds": seconds, "rejudge": bool(rejudge), "rows": out,
              "all_killed": all(row["killed"] for row in out)}
    problems = evidence_module.validate_plants(record)
    if problems:
        raise KitError("; ".join(problems))
    appended = store.record(wt, task_id, store.PLANTS, record)
    killed = sum(row["killed"] for row in out)
    gate._out(f"plants: {killed} of {len(out)} killed at {head[:12]} ({seconds:.0f} s); wrote {appended['path']}")
    for row in out:
        if not row["killed"]:
            gate._out(f"plants: {gate._show(row['id'])} {row['kill_reason']}")
    return record, appended["path"]


def _summary(found: dict, task_id: str, branch: str) -> list[str]:
    """What is left after a run: the deferred rows with no killed verdict and the command, or that QA can accept."""
    after = evidence_module.plants_coverage(found["wt"], task_id, found["entry"], found["data"])
    if after["pending"]:
        return after["pending"]
    gate._out(f"plants: every deferred row of {found['entry']['path']} is killed; QA can accept the record")
    return []


def adopt(root, task_id: str, branch: str, worktree=None) -> int:
    """List in the task's pointer every plants record file of its `plants/` folder that the pointer does not list
    yet: records judged in another checkout (another machine, `pk remote plants`) and copied here. Each must be
    a record under its own name (the digest in the name is the file's), of the plants schema, of this task, bound by
    `part_of` to the latest results record or a record it reused, judged at that record's head, and hold only
    deferred rows of it with their definitions. Any file that is not refuses the whole adoption, naming it; nothing
    is listed then. 0 when done (or nothing was new)."""
    root = Path(root).resolve()
    gate._task_branch(task_id, branch)
    found = _context(root, task_id, branch, worktree)
    wt, entry, data = found["wt"], found["entry"], found["data"]
    chain = evidence_module.plants_chain(wt, entry, data)
    deferred = {row["id"]: row["definition_sha256"] for row in data["planted_bugs"] if evidence_module.is_deferred(row)}
    listed = {item["path"] for item in store.read_plants(wt, task_id)}
    folder = wt / store.BASE / task_id / store.PLANTS
    names = sorted(path.name for path in folder.iterdir() if path.is_file() and not path.name.startswith(".")) \
        if folder.is_dir() else []
    new = [f"{store.BASE}/{task_id}/{store.PLANTS}/{name}" for name in names]
    new = [rel for rel in new if rel not in listed]
    if not new:
        gate._out(f"plants: nothing to adopt: every plants record in {store.BASE}/{task_id}/{store.PLANTS}/ is "
                  f"listed in {store.pointer_path(task_id)}")
        return 0
    adopted, problems = [], []
    for rel in new:
        problem, record = _adoptable(wt, task_id, rel, chain, deferred)
        if problem:
            problems.append(f"{gate._show(rel)}: {problem}")
        else:
            adopted.append((rel, record))
    if problems:
        raise KitError(f"pk gate plants --adopt: nothing adopted; {len(problems)} file(s) in "
                       f"{store.BASE}/{task_id}/{store.PLANTS}/ are not plants records of {entry['path']} or a record "
                       f"it reused: " + "; ".join(problems) + " (remove what does not belong there, then adopt again)")
    for rel, record in adopted:
        store.add_plants_entry(wt, task_id, store.plants_entry({"digest": evidence_module.sha256_bytes(
            (wt / rel).read_bytes()), "path": rel}, record))
        killed = sum(row["killed"] for row in record["rows"])
        gate._out(f"plants: adopted {rel} ({killed} of {len(record['rows'])} killed, judged at "
                  f"{record['head'][:12]})")
    gate._out(f"plants: commit the adopted records and {store.pointer_path(task_id)} on {branch}")
    pending = _summary(found, task_id, branch)
    if pending:
        gate._out(f"plants: {len(pending)} of {len(deferred)} deferred rows have no killed verdict yet")
    return 0


def _adoptable(wt: Path, task_id: str, rel: str, chain: dict, deferred: dict) -> tuple[str | None, dict | None]:
    """(why `rel` may not be adopted or None, the record)."""
    try:
        record = store.load(wt, rel)
    except KitError as exc:
        return " ".join(str(exc).split()), None
    problems = evidence_module.validate_plants(record)
    if problems:
        return f"not a valid plants record: {problems[0]}", None
    if record["task_id"] != task_id:
        return f"is a record of {gate._show(record['task_id'])}, not of {task_id}", None
    part_of = record["part_of"]
    if part_of["digest"] not in chain or store.check_rel(part_of["path"])[0] != task_id \
            or not part_of["path"].endswith(f"-{part_of['digest'][:12]}.json"):
        return (f"not bound to the latest results record or a record it reused (part_of "
                f"{gate._show(part_of['path'])})"), None
    if record["head"] != chain[part_of["digest"]]:
        return f"judged at {record['head']}, not at {chain[part_of['digest']]}, the head of the record it is bound to", None
    for row in record["rows"]:
        if row["id"] not in deferred:
            return f"row {gate._show(row['id'])} is not a deferred row of the record", None
        if row["definition_sha256"] != deferred[row["id"]]:
            return f"row {gate._show(row['id'])} judged another definition than the deferred row", None
    return None, record


def _check_options(names, all_pending, chunk_seconds, per_row, plan, chunk) -> None:
    """KitError for a combination of the chunk options that has no meaning, before anything is read."""
    if all_pending and names:
        raise KitError("pass --rows or --all-pending, not both")
    if chunk_seconds is None:
        if plan or chunk is not None:
            raise KitError("--plan and --chunk need --chunk-seconds N (the plan they name is cut by it)")
        if per_row is not None:
            raise KitError("--per-row-seconds needs --chunk-seconds N")
        return
    if not all_pending:
        raise KitError("--chunk-seconds splits --all-pending into chunks; for rows you choose, pass --rows alone")
    if isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, int) or chunk_seconds < 1:
        raise KitError(f"--chunk-seconds must be at least 1, not {gate._show(chunk_seconds)}")
    if per_row is not None and not per_row > 0:
        raise KitError(f"--per-row-seconds must be above 0, not {gate._show(per_row)}")
    if plan and chunk is not None:
        raise KitError("pass --plan or --chunk, not both")
    if chunk is not None:
        parse_chunk(chunk)


def plants(root, task_id: str, branch: str, names: list[str] | None = None, all_pending: bool = False,
           workers: int | None = None, rejudge: bool = False, timeout: int | None = gate.STEP_TIMEOUT,
           worktree=None, chunk_seconds: int | None = None, per_row: float | None = None, plan: bool = False,
           chunk: str | None = None) -> int:
    """Judge deferred rows of the task's latest results record and append a plants record (one per chunk with
    `chunk_seconds`); 0 when every judged row was killed (and nothing was pending), 1 otherwise. With `plan`,
    print the chunks and judge nothing."""
    root = Path(root).resolve()
    gate._task_branch(task_id, branch)
    _check_options(names, all_pending, chunk_seconds, per_row, plan, chunk)
    if chunk_seconds is not None:
        return _chunked(root, task_id, branch, workers, rejudge, timeout, worktree, chunk_seconds, per_row, plan,
                        chunk)
    found = _checked_context(root, task_id, branch, worktree)
    wt, entry, data, coverage = found["wt"], found["entry"], found["data"], found["coverage"]
    rows = select(data, coverage, names, all_pending, rejudge)
    command = f"pk gate plants {task_id} --branch {branch}"
    if not rows:
        gate._out(f"plants: every deferred row of {entry['path']} is already killed in a plants record; nothing to do")
        return 0
    _checked_rows(wt, data["head"], rows)
    count = evidence_module.plant_workers(root, workers)
    record, path = _judge(found, task_id, rows, count, rejudge, timeout)
    gate._out(f"plants: commit {path} and {store.pointer_path(task_id)} on {branch}")
    pending = _summary(found, task_id, branch)
    if pending:
        gate._out(f"plants: {len(pending)} of {len(coverage['deferred'])} deferred rows have no killed verdict "
                  f"yet; judge the rest with: {command} --all-pending")
    return 0 if record["all_killed"] else 1


def _chunked(root: Path, task_id: str, branch: str, workers, rejudge: bool, timeout, worktree, chunk_seconds: int,
             per_row, plan: bool, chunk) -> int:
    """`--chunk-seconds`: the pending rows in chunks that fit, run in order (one record each, stopping at the first
    that is not all killed); with `plan` the plan over every deferred row; with `chunk` its chunk K alone."""
    command = f"pk gate plants {task_id} --branch {branch} --all-pending --chunk-seconds {chunk_seconds}"
    if plan or chunk is not None:
        planned = chunk_plan(root, task_id, branch, chunk_seconds, per_row, workers, worktree)
        found, count, seconds = planned["context"], planned["workers"], planned["per_row"]
        chunks, killed = planned["chunks"], planned["killed"]
        pinned = f"{command} --per-row-seconds {_seconds(seconds)} --workers {count}"
        if plan:
            total = sum(len(rows) for rows in chunks)
            gate._out(f"plants: plan over all {total} deferred row(s), {_seconds(seconds)} s per row slot "
                      f"({planned['source']}), at most {chunk_seconds} s a chunk with {count} worker(s): "
                      f"{len(chunks)} chunk(s); nothing judged")
            for number, rows in enumerate(chunks, 1):
                left = sum(1 for row in rows if row["id"] not in killed)
                gate._out(f"plants: chunk {number}/{len(chunks)}: {len(rows)} row(s), about "
                          f"{estimate(rows, count, seconds):.0f} s, {left} not killed yet: "
                          + ", ".join(gate._show(row["id"]) for row in rows))
                gate._out(f"  {pinned} --chunk {number}/{len(chunks)}")
            return 0
        number, of = parse_chunk(chunk)
        if len(chunks) != of:
            raise KitError(f"the plan with --chunk-seconds {chunk_seconds}, {_seconds(seconds)} s per row slot "
                           f"and {count} worker(s) has {len(chunks)} chunk(s), not {of}; print it with: {pinned} "
                           "--plan (and pass the same --per-row-seconds and --workers on every machine)")
        rows = [row for row in chunks[number - 1] if rejudge or row["id"] not in killed]
        if not rows:
            gate._out(f"plants: chunk {number}/{of}: every row is already killed; nothing to do")
            return 0
        _checked_rows(found["wt"], found["data"]["head"], rows)
        gate._out(f"plants: chunk {number}/{of}: {len(rows)} row(s), about {estimate(rows, count, seconds):.0f} s "
                  f"({_seconds(seconds)} s per row slot, {planned['source']}): "
                  + ", ".join(gate._show(row["id"]) for row in rows))
        record, path = _judge(found, task_id, rows, count, rejudge, timeout)
        gate._out(f"plants: commit {path} and {store.pointer_path(task_id)} on {branch}")
        pending = _summary(found, task_id, branch)
        if pending:
            gate._out(f"plants: {len(pending)} of {len(found['coverage']['deferred'])} deferred rows have no killed "
                      f"verdict yet; the other chunks: {pinned} --plan")
        return 0 if record["all_killed"] else 1
    found = _checked_context(root, task_id, branch, worktree)
    rows = select(found["data"], found["coverage"], None, True, rejudge)
    if not rows:
        gate._out(f"plants: every deferred row of {found['entry']['path']} is already killed in a plants record; "
                  "nothing to do")
        return 0
    _checked_rows(found["wt"], found["data"]["head"], rows)
    count = evidence_module.plant_workers(root, workers)
    seconds, source = per_row_seconds(root, found["wt"], task_id, found["entry"], found["data"], per_row)
    chunks = split(rows, count, seconds, chunk_seconds)
    gate._out(f"plants: {_seconds(seconds)} s per row slot ({source}); {len(rows)} pending row(s) in {len(chunks)} "
              f"chunk(s) of at most {chunk_seconds} s with {count} worker(s)")
    if per_row is not None:
        command += f" --per-row-seconds {_seconds(per_row)}"
    for number, chunk_rows in enumerate(chunks, 1):
        gate._out(f"plants: chunk {number}/{len(chunks)}: {len(chunk_rows)} row(s), about "
                  f"{estimate(chunk_rows, count, seconds):.0f} s: " + ", ".join(gate._show(row["id"]) for row in chunk_rows))
        record, path = _judge(found, task_id, chunk_rows, count, rejudge, timeout)
        if not record["all_killed"]:
            rest = chunks[number:]
            pending = evidence_module.plants_coverage(found["wt"], task_id, found["entry"], found["data"])["pending"]
            gate._out(f"plants: chunk {number}/{len(chunks)} is not all killed; stopped before {len(rest)} more "
                      f"chunk(s) ({sum(len(r) for r in rest)} row(s)); {len(pending)} deferred row(s) have no killed "
                      f"verdict: " + evidence_module._listed([gate._show(rid) for rid in pending]))
            gate._out(f"plants: commit the plants records and {store.pointer_path(task_id)} on {branch}; after a fix "
                      f"the gate writes a new record; to go on at this head, continue with: {command}")
            return 1
    gate._out(f"plants: commit the plants records and {store.pointer_path(task_id)} on {branch}")
    _summary(found, task_id, branch)
    return 0


def cmd_plants(args) -> int:
    names = [name.strip() for part in args.rows or [] for name in part.split(",")]
    if args.adopt:
        if args.rows or args.all_pending or args.rejudge or args.workers is not None or any(
                value not in (None, False) for value in (args.chunk_seconds, args.per_row_seconds, args.plan,
                                                         args.chunk)):
            raise KitError("--adopt takes no rows or chunk options: it judges nothing and lists the plants records "
                           "of the folder that the pointer does not list yet")
        return adopt(gate.find_root(), args.task_id, args.branch, worktree=args.worktree)
    if not args.rows and not args.all_pending:
        raise KitError("one of the arguments --rows --all-pending is required (or --adopt)")
    return plants(gate.find_root(), args.task_id, args.branch, names=names, all_pending=args.all_pending,
                  workers=args.workers, rejudge=args.rejudge, timeout=args.timeout, worktree=args.worktree,
                  chunk_seconds=args.chunk_seconds, per_row=args.per_row_seconds, plan=args.plan, chunk=args.chunk)


def configure(actions) -> None:
    """Add the `plants` action to `pk gate`'s action parsers."""
    parser = actions.add_parser("plants", help="judge the deferred planted-bug rows of the task's latest results "
                                               "record at its head and append a plants record bound to it")
    parser.add_argument("task_id", metavar="ID", help="task ID, for example P0-M01-S01-T01")
    parser.add_argument("--branch", required=True, help="the task branch, task/<ID>")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--rows", action="append", metavar="ID[,ID...]",
                       help="the deferred rows to judge (repeat or separate with commas)")
    which.add_argument("--all-pending", action="store_true",
                       help="every deferred row that has no killed verdict in a plants record yet")
    parser.add_argument("--chunk-seconds", type=int, metavar="N",
                        help="with --all-pending: judge the rows in chunks estimated to fit N seconds each, in table "
                             "order, one plants record per chunk, stopping at the first chunk not all killed")
    parser.add_argument("--per-row-seconds", type=float, metavar="S",
                        help="the seconds of one row slot (default: measured by earlier plants records of this "
                             "task, else of the program's other tasks, else the results record's timing, else "
                             f"evidence.plant_row_seconds, else {gate.ROW_SECONDS})")
    parser.add_argument("--plan", action="store_true",
                        help="print the chunks over every deferred row, with the command of each; judge nothing")
    parser.add_argument("--adopt", action="store_true",
                        help="list in the pointer the plants records of the plants/ folder it does not list yet "
                             "(judged in another checkout and copied here), after checking each; judges nothing")
    parser.add_argument("--chunk", metavar="K/M", help="judge only chunk K of the plan of M chunks (its rows not "
                                                       "killed yet), for chunks spread over several machines")
    parser.add_argument("--workers", type=int, metavar="N", help="rows judged at once (default: PK_PLANT_WORKERS, "
                                                                  "else evidence.plant_workers, else 1)")
    parser.add_argument("--rejudge", action="store_true", help="judge a row again although a plants record bound to "
                                                               "this record already judged it killed")
    parser.add_argument("--timeout", type=int, default=gate.STEP_TIMEOUT, metavar="SECONDS",
                        help=f"per command (default {gate.STEP_TIMEOUT})")
    parser.add_argument("--worktree", help="the task worktree (default: <root>/.worktrees/<id lowercased>)")
