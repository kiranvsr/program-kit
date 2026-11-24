#!/usr/bin/env python3
"""`pk adopt <source.json> [--dry-run]`: move a programme that ran before the kit onto it, once (0.7.0).

The source is one neutral JSON file (`kit/templates/adoption.schema.json`, `"schema": "program-kit.adoption.v1"`)
that the programme's own exporter writes: its register, statuses, rulings log, follow-ups, ad-hoc tasks, cost rows,
hand-off and the roots of its old evidence, and optionally `notes`, the exporter's judgement calls, which stay on
record in the stored copy of the source. The command:

1. refuses unless the tree is clean, HEAD is `main`, `program/` exists without `program/adoption.yaml`, holds no kit
   work yet (an empty register, no ruling, follow-up, ad-hoc task, cost line or status record) and `pk doctor`
   passes on it;
2. validates the whole source before writing anything, and prints every problem when it fails;
3. copies the legacy rulings log and hand-off into `program/legacy/` byte for byte, and the source to
   `program/legacy/adoption-source.json`;
4. writes `program/register.yaml`, the controller state (follow-ups with their own IDs and `followups_issued`,
   ad-hoc tasks as AH-<nnnn> keeping `legacy_id`, the now line), `program/costs.jsonl` with every kit key filled,
   and last `program/adoption.yaml`;
5. runs `pk register validate`, `pk status validate`, `pk handoff update` and `pk doctor`, and when any of them fails
   puts every file it wrote back as it was.

`pk adopt --from <name> [args...]` (0.7.0) takes the source from a programme's extension instead of a file: the importer
an extension registered under that name (`api.adopt_importer`, ext.py) is called with the root and the args, and the
dict it returns is written as JSON and then validated and stored exactly as a file's bytes would be.

`--dry-run` stops after step 2 and prints the plan and the counts. The command never commits: the controller commits
`program/` as one commit. Status records are written by batch KA-2's importer (`_write_statuses`); until then a
source whose `statuses` list is not empty is refused, so nothing is half adopted.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import adoption
import cost as cost_module
import ctlstate
import miniyaml
import register as register_module
import rulings as rulings_module
from common import (KitError, PRIMARY_BRANCH, find_root, git, read_json, read_yaml, sha256_bytes, utc_now,
                    write_yaml)

SCHEMA = "program-kit.adoption.v1"
KIT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_FILE = KIT_DIR / "templates" / "adoption.schema.json"
SOURCE_KEYS = ("schema", "programme", "primary_branch", "legacy_phase_prefixes", "register", "statuses", "rulings",
               "followups", "adhoc", "costs", "handoff", "legacy_evidence_roots")
OPTIONAL_SOURCE_KEYS = ("notes",)  # the exporter's judgement calls, kept with the stored source
STATUS_KEYS = ("task_id", "status", "actor", "updated_at", "references", "note")
STATUS_VALUES = ("done", "in_progress", "review", "blocked", "not_started")  # read for shape: KA-2 imports them
OPEN_KEYS = ("id", "area", "item", "added", "tags")
CLOSED_KEYS = ("id", "area", "item", "reason", "added", "tags", "dup_of")
ADHOC_KEYS = ("legacy_id", "title", "state", "note")
# A source ad-hoc state, as the kit's tracking word: the kit's own words, and the two other words a programme uses.
ADHOC_STATE = {**{state: state for state in ctlstate.ADHOC_STATES}, "done": "closed", "in_progress": "progress"}
COST_TEXT = ("at", "task", "role", "model")
COST_NUMBERS = ("tokens", "tool_calls", "seconds")
POST_CHECKS = (("register", "validate"), ("status", "validate"), ("handoff", "update"), ("doctor",))
KA2_STATUSES = "statuses import arrives in KA-2"
_FOLLOWUP = re.compile(r"F-(\d{4})\Z", re.ASCII)
SHOWN = 20  # missing follow-up IDs named in one problem line


class _Problems(list):
    def add(self, text: str) -> None:
        self.append(" ".join(str(text).split()))


def schema_required() -> list[str]:
    """The top-level keys kit/templates/adoption.schema.json requires (the code's SOURCE_KEYS, tested)."""
    required = read_json(SCHEMA_FILE).get("required")
    if not isinstance(required, list):
        raise KitError("adoption.schema.json: 'required' must be a list of key names")
    return required


# --- step 1: the repository ---------------------------------------------------------------

def _run_pk(root: Path, *argv: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "pk.py"), *argv], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stderr.strip() or proc.stdout.strip())


def _repository_problems(root: Path, source_path: Path | None, problems: _Problems) -> None:
    try:
        head = git(root, "symbolic-ref", "--quiet", "HEAD")
    except KitError:
        head = ""
    if head != f"refs/heads/{PRIMARY_BRANCH}":
        problems.add(f"pk adopt runs in the main checkout on {PRIMARY_BRANCH} (HEAD is {head or 'detached'})")
    try:
        git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except KitError:
        problems.add(f"{PRIMARY_BRANCH} has no commit yet; commit program/ first")
    listing = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    try:
        own = source_path.resolve().relative_to(root.resolve()).as_posix() if source_path is not None else None
    except ValueError:
        own = None
    dirty = [entry[3:] for entry in listing.split("\0")
             if len(entry) > 3 and entry[2] == " " and not (entry.startswith("??") and entry[3:] == own)]
    if dirty:
        problems.add(f"the tree is not clean ({', '.join(dirty[:5])}{' and more' if len(dirty) > 5 else ''}): "
                     "commit or remove the changes first")
    program = root / "program"
    if not program.is_dir():
        problems.add("program/ does not exist: install the kit first (install.sh, or pk init)")
        return
    if (root / adoption.ADOPTION_FILE).exists():
        problems.add(f"{adoption.ADOPTION_FILE} exists: this programme has already adopted, and adoption happens once")
        return
    if (root / adoption.LEGACY_DIR).exists():
        problems.add(f"{adoption.LEGACY_DIR} exists without {adoption.ADOPTION_FILE}: remove it (it is not the kit's)")
    held = []
    try:
        if (program / "register.yaml").exists() and register_module.load(root)["phases"]:
            held.append("the register lists phases")
        if rulings_module.read(root):
            held.append("program/rulings.md holds rulings")
        state = ctlstate.load(root)
        if state["followups"] or state["closed_followups"] or state["followups_issued"] or state["adhoc"]:
            held.append("the controller state holds follow-ups or ad-hoc tasks")
        if (root / cost_module.COST_FILE).exists() and (root / cost_module.COST_FILE).read_bytes().strip():
            held.append("program/costs.jsonl has lines")
        if any((program / "status").glob("*.json")):
            held.append("program/status/ holds status records")
    except (KitError, OSError) as exc:
        held.append(f"program/ does not read ({' '.join(str(exc).split())})")
    if held:
        problems.add("program/ already holds kit work (" + "; ".join(held) + "): adoption moves a programme onto a "
                     "kit installed for it, before any kit work")
    code, said = _run_pk(root, "doctor")
    if code != 0:
        problems.add(f"pk doctor fails before adoption: {said.splitlines()[-1] if said else f'exit {code}'}")


# --- step 2: the source -------------------------------------------------------------------

def _keys(entry, allowed: tuple[str, ...], required: tuple[str, ...], who: str, problems: _Problems) -> bool:
    if not isinstance(entry, dict):
        problems.add(f"{who}: must be an object")
        return False
    missing = [key for key in required if key not in entry]
    unknown = sorted(str(key) for key in entry if key not in allowed)
    if missing:
        problems.add(f"{who}: missing {', '.join(missing)}")
    if unknown:
        problems.add(f"{who}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(allowed)})")
    return not missing


def _text(entry: dict, key: str, who: str, problems: _Problems, required: bool = False) -> str:
    value = entry.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        problems.add(f"{who}: {key} must be {'non-empty ' if required else ''}text")
        return ""
    return value


def _whole(value, what: str, problems: _Problems, minimum: int = 0) -> int | None:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        problems.add(f"{what} must be a whole number, {minimum} or more")
        return None
    return value


def _tracked_file(root: Path, path, what: str, problems: _Problems) -> Path | None:
    problem = adoption.relative_path_problem(path, what)
    if problem:
        problems.add(problem)
        return None
    if path.startswith(adoption.LEGACY_DIR):
        problems.add(f"{what} {path} is under {adoption.LEGACY_DIR}, where the copies go")
        return None
    if subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"HEAD:{path}"], capture_output=True).returncode \
            or not (root / path).is_file():
        problems.add(f"{what} {path} is not a file committed at HEAD")
        return None
    return root / path


def _statuses(source: dict, problems: _Problems) -> list[dict]:
    items = source["statuses"]
    if not isinstance(items, list):
        problems.add("statuses: must be a list")
        return []
    seen = set()
    for number, entry in enumerate(items, 1):
        who = f"statuses[{number}]"
        if not _keys(entry, STATUS_KEYS, ("task_id", "status"), who, problems):
            continue
        task_id = _text(entry, "task_id", who, problems, required=True)
        if task_id in seen:
            problems.add(f"{who}: {task_id} is listed twice")
        seen.add(task_id)
        if entry["status"] not in STATUS_VALUES:
            problems.add(f"{who}: status {entry['status']!r} must be one of {', '.join(STATUS_VALUES)}")
        for key in ("actor", "updated_at", "note"):
            _text(entry, key, who, problems)
        references = entry.get("references", [])
        if not isinstance(references, list):
            problems.add(f"{who}: references must be a list")
            continue
        for index, reference in enumerate(references, 1):
            if _keys(reference, ("path",), ("path",), f"{who}.references[{index}]", problems):
                problem = adoption.relative_path_problem(reference["path"], f"{who}.references[{index}].path")
                if problem:
                    problems.add(problem)
    if items:
        problems.add(f"statuses: {len(items)} listed; {KA2_STATUSES} (export an empty statuses list until then)")
    return items


def _rulings(root: Path, source: dict, problems: _Problems) -> tuple[Path | None, int, list[int]]:
    entry = source["rulings"]
    if not _keys(entry, ("path", "next"), ("path", "next"), "rulings", problems):
        return None, 0, []
    path = _tracked_file(root, entry["path"], "rulings.path", problems)
    start = _whole(entry["next"], "rulings.next", problems, minimum=1) or 0
    numbers = sorted(adoption.legacy_blocks(path.read_bytes())) if path else []
    if path and start:
        if not numbers:
            problems.add(f"rulings.path {entry['path']}: no ruling header (## R<n>) found")
        elif numbers[-1] + 1 != start:
            problems.add(f"rulings.next is {start}, but the legacy log's last ruling is R{numbers[-1]}: the kit "
                         f"issues the next number, R{numbers[-1] + 1}")
    return path, start, numbers


def _followups(source: dict, problems: _Problems) -> dict | None:
    entry = source["followups"]
    if not _keys(entry, ("next", "open", "closed"), ("next", "open", "closed"), "followups", problems):
        return None
    issued = _whole(entry["next"], "followups.next", problems, minimum=1)
    found: dict[int, str] = {}
    lists = {}
    for name, allowed, required in (("open", OPEN_KEYS, ("id", "area", "item")),
                                    ("closed", CLOSED_KEYS, ("id", "area", "item", "reason"))):
        items = entry[name]
        if not isinstance(items, list):
            problems.add(f"followups.{name}: must be a list")
            items = []
        kept = []
        for number, item in enumerate(items, 1):
            who = f"followups.{name}[{number}]"
            if not _keys(item, allowed, required, who, problems):
                continue
            match = _FOLLOWUP.match(item["id"]) if isinstance(item["id"], str) else None
            if not match:
                problems.add(f"{who}: id {item['id']!r} must be F-<nnnn>")
                continue
            who = f"followups.{name} {item['id']}"
            if int(match.group(1)) in found:
                problems.add(f"{who}: listed twice (also {found[int(match.group(1))]})")
                continue
            found[int(match.group(1))] = name
            converted = {"id": item["id"], "area": _text(item, "area", who, problems, required=True),
                         "item": _text(item, "item", who, problems, required=True),
                         "added": _text(item, "added", who, problems) or ctlstate.UNKNOWN_DATE}
            tags = item.get("tags", [])
            if not (isinstance(tags, list) and all(isinstance(tag, str) and tag.strip() for tag in tags)):
                problems.add(f"{who}: tags must be a list of text")
                tags = []
            converted["tags"] = list(dict.fromkeys(tags))
            if name == "closed":
                duplicate_of = item.get("dup_of") or ""
                if duplicate_of and (not isinstance(duplicate_of, str) or duplicate_of == item["id"]):
                    problems.add(f"{who}: dup_of {duplicate_of!r} must name another follow-up")
                    duplicate_of = ""
                converted.update(closed=ctlstate.UNKNOWN_DATE, reason=_text(item, "reason", who, problems), by="",
                                 duplicate_of=duplicate_of)
            kept.append(converted)
        lists[name] = kept
    if issued is None:
        return None
    if issued - 1 > 9999:
        problems.add(f"followups.next is {issued}: follow-up IDs run to F-9999")
    beyond = sorted(f"F-{n:04d}" for n in found if n >= issued)
    if beyond:
        problems.add(f"followups: {', '.join(beyond[:SHOWN])} at or past followups.next F-{issued:04d}")
    missing = [f"F-{n:04d}" for n in range(1, issued) if n not in found]
    if missing:
        problems.add(f"followups: F-0001 to F-{issued - 1:04d} must all be listed, open or closed; missing "
                     f"{len(missing)}: {', '.join(missing[:SHOWN])}{' and more' if len(missing) > SHOWN else ''}")
    for item in lists["closed"]:
        if item["duplicate_of"] and not (_FOLLOWUP.match(item["duplicate_of"])
                                         and int(item["duplicate_of"][2:]) in found):
            problems.add(f"followups.closed {item['id']}: dup_of {item['duplicate_of']} is not a follow-up of the "
                         "source")
    return {"followups": lists["open"], "closed_followups": lists["closed"], "followups_issued": issued - 1}


def _adhoc(source: dict, task_ids: set[str], problems: _Problems) -> list[dict]:
    items = source["adhoc"]
    if not isinstance(items, list):
        problems.add("adhoc: must be a list")
        return []
    if len(items) > 9999:
        problems.add(f"adhoc: {len(items)} tasks; ad-hoc IDs run to AH-9999")
    found, names = [], set()
    for number, item in enumerate(items, 1):
        who = f"adhoc[{number}]"
        if not _keys(item, ADHOC_KEYS, ("legacy_id", "title", "state"), who, problems):
            continue
        legacy = _text(item, "legacy_id", who, problems, required=True)
        if legacy in names:
            problems.add(f"{who}: legacy_id {legacy} is listed twice")
        if legacy in task_ids:
            problems.add(f"{who}: legacy_id {legacy} is also a register task ID; a cost line naming it would be "
                         "ambiguous")
        names.add(legacy)
        state = item["state"]
        if state not in ADHOC_STATE:
            problems.add(f"{who}: state {state!r} must be one of {', '.join(ADHOC_STATE)}")
        found.append({"id": f"AH-{number:04d}", "title": _text(item, "title", who, problems, required=True),
                      "state": ADHOC_STATE.get(state, "queued"), "note": _text(item, "note", who, problems),
                      "ruling": "", "updated": ctlstate.UNKNOWN_DATE, "legacy_id": legacy})
    return found


def _costs(source: dict, task_ids: set[str], adhoc_ids: dict[str, str], problems: _Problems) -> list[dict]:
    rows = source["costs"]
    if not isinstance(rows, list):
        problems.add("costs: must be a list")
        return []
    found = []
    for number, row in enumerate(rows, 1):
        who = f"costs[{number}]"
        if not isinstance(row, dict):
            problems.add(f"{who}: must be an object")
            continue
        record = dict(row)
        reserved = [key for key in ("legacy_task", "imported") if key in record]
        if reserved:
            problems.add(f"{who}: {', '.join(reserved)} is the kit's own key; the importer sets it")
            continue
        bad = [key for key in COST_TEXT if not (isinstance(record.get(key), str) and record[key].strip())]
        if bad:
            problems.add(f"{who}: {', '.join(bad)} must be non-empty text")
            continue
        numbers = [_whole(record.get(key), f"{who}: {key}", problems) for key in COST_NUMBERS]
        if None in numbers:
            continue
        record.update(zip(COST_NUMBERS, numbers))
        record.setdefault("agent", "unknown")
        record.setdefault("resume", False)
        record["imported"] = True  # its agents ran before adoption: counted in the report, never flagged
        if "category" not in record and record["task"] not in task_ids:
            if record["task"] in adhoc_ids:
                record["task"] = adhoc_ids[record["task"]]
            else:
                record["legacy_task"] = True
        problem = cost_module.record_problem(record)
        if problem:
            problems.add(f"{who}: {problem}")
            continue
        found.append(record)
    return found


def _evidence_roots(root: Path, source: dict, problems: _Problems) -> list[str]:
    roots = source["legacy_evidence_roots"]
    if not isinstance(roots, list):
        problems.add("legacy_evidence_roots: must be a list of directories")
        return []
    for entry in roots:
        problem = adoption.relative_path_problem(entry, "legacy evidence root")
        if problem:
            problems.add(problem)
            continue
        kind = subprocess.run(["git", "-C", str(root), "cat-file", "-t", f"HEAD:{entry.rstrip('/')}"],
                              capture_output=True, text=True)
        if kind.returncode != 0 or kind.stdout.strip() != "tree":
            problems.add(f"legacy evidence root {entry} is not a directory committed at HEAD")
    if len(set(roots)) != len(roots):
        problems.add("legacy_evidence_roots lists a directory twice")
    return list(roots)


def _register(root: Path, source: dict, prefixes: tuple[str, ...], problems: _Problems) -> set[str]:
    document = source["register"]
    if not (isinstance(document, dict) and set(document) == {"phases"} and isinstance(document["phases"], list)):
        problems.add("register: must be an object with one key, phases, a list (the shape of program/register.yaml)")
        return set()
    env_path = root / "program" / "environments.yaml"
    try:
        environments = read_yaml(env_path) if env_path.exists() else None
        found = register_module.validate(document, environments, read_yaml(KIT_DIR / "interview" / "roles.yaml"),
                                         prefixes)
    except KitError as exc:
        found = [str(exc)]
    problems.extend(f"register: {problem}" for problem in found)
    try:
        if miniyaml.loads(miniyaml.dumps(document)) != document:
            problems.add("register: does not survive being written as program/register.yaml (YAML subset)")
    except (KitError, RecursionError) as exc:
        problems.add(f"register: cannot be written as program/register.yaml: {exc}")
    return {node["id"] for level, node, _ in register_module._nodes(document)
            if level == 3 and isinstance(node.get("id"), str)}


def validate_source(root: Path, data: bytes) -> tuple[dict | None, list[str]]:
    """(plan, problems): everything `pk adopt` would write, worked out from the source bytes, and every problem with
    them. Nothing is written."""
    problems = _Problems()
    try:
        source = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, [f"the source is not UTF-8 JSON: {exc}"]
    if not isinstance(source, dict):
        return None, ["the source must be a JSON object"]
    missing = [key for key in SOURCE_KEYS if key not in source]
    unknown = sorted(str(key) for key in source if key not in SOURCE_KEYS + OPTIONAL_SOURCE_KEYS)
    if unknown:
        problems.add(f"unknown key(s) {', '.join(unknown)}")
    if missing:
        problems.add(f"missing key(s) {', '.join(missing)}")
        return None, problems
    if source["schema"] != SCHEMA:
        problems.add(f"schema is {source['schema']!r}; this kit reads {SCHEMA!r}")
    notes = source.get("notes", [])
    if not (isinstance(notes, list) and all(isinstance(note, str) for note in notes)):
        problems.add("notes: must be a list of text (the exporter's judgement calls)")
    if not (isinstance(source["programme"], str) and source["programme"].strip()):
        problems.add("programme must be the programme's name")
    if source["primary_branch"] != PRIMARY_BRANCH:
        problems.add(f"primary_branch is {source['primary_branch']!r}; the kit's primary branch is {PRIMARY_BRANCH}: "
                     f"rename it (git branch -m {PRIMARY_BRANCH}) and export again")
    prefixes = source["legacy_phase_prefixes"]
    if not isinstance(prefixes, list):
        problems.add("legacy_phase_prefixes: must be a list")
        prefixes = []
    problems.extend(problem for problem in map(adoption.prefix_problem, prefixes) if problem)
    prefixes = tuple(prefix for prefix in prefixes if not adoption.prefix_problem(prefix))
    if len(set(prefixes)) != len(prefixes):
        problems.add("legacy_phase_prefixes lists a prefix twice")
    task_ids = _register(root, source, prefixes, problems)
    statuses = _statuses(source, problems)
    rulings_path, start, legacy_numbers = _rulings(root, source, problems)
    followups = _followups(source, problems)
    adhoc = _adhoc(source, task_ids, problems)
    costs = _costs(source, task_ids, {item["legacy_id"]: item["id"] for item in adhoc}, problems)
    handoff = source["handoff"]
    handoff_path, now = None, ""
    if _keys(handoff, ("path", "now"), ("path", "now"), "handoff", problems):
        handoff_path = _tracked_file(root, handoff["path"], "handoff.path", problems)
        now = " ".join(_text(handoff, "now", "handoff", problems).split())
    roots = _evidence_roots(root, source, problems)
    if problems:
        return None, problems
    state = ctlstate.parse({"stage": {}, "queued": {}, "parked": {}, "adhoc": adhoc, **followups, "now": now})
    return {"source": source, "prefixes": list(prefixes), "statuses": statuses, "rulings_path": rulings_path,
            "ruling_start": start, "legacy_rulings": legacy_numbers, "state": state, "costs": costs,
            "handoff_path": handoff_path, "evidence_roots": roots, "tasks": task_ids}, []


# --- steps 3 to 5: writing, checking, undoing ----------------------------------------------

def _write_statuses(root: Path, plan: dict) -> list[str]:
    """KA-2 writes the imported status records here (`adopted` and `not_started`) and returns their paths. Until
    then the source's statuses list is empty (validate_source refuses any other) and nothing is written."""
    assert not plan["statuses"], KA2_STATUSES
    return []


def _snapshot(root: Path) -> tuple[dict[str, bytes], set[str]]:
    """Every file under program/ with its bytes, and every directory: what `_restore` puts back."""
    program = root / "program"
    files, dirs = {}, set()
    for path in program.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            dirs.add(rel)
        else:
            files[rel] = path.read_bytes()
    return files, dirs


def _restore(root: Path, saved: tuple[dict[str, bytes], set[str]]) -> None:
    """Put program/ back as `_snapshot` saw it: files it wrote removed, files it changed rewritten, new folders gone."""
    files, dirs = saved
    for path in sorted((root / "program").rglob("*"), reverse=True):  # deepest first, so folders empty before removal
        rel = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            if rel not in dirs:
                path.rmdir()
        elif rel not in files:
            path.unlink()
        elif path.read_bytes() != files[rel]:
            path.write_bytes(files[rel])
    for rel, data in files.items():
        if not (root / rel).exists():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)


def _write(root: Path, plan: dict, data: bytes) -> list[str]:
    """Steps 3 and 4; returns the paths written (adoption.yaml last)."""
    legacy = root / adoption.LEGACY_DIR
    legacy.mkdir(parents=True)
    (root / adoption.SOURCE_COPY).write_bytes(data)
    rulings_bytes = plan["rulings_path"].read_bytes()
    (root / adoption.LEGACY_RULINGS).write_bytes(rulings_bytes)
    (root / adoption.LEGACY_HANDOFF).write_bytes(plan["handoff_path"].read_bytes())
    written = [adoption.SOURCE_COPY, adoption.LEGACY_RULINGS, adoption.LEGACY_HANDOFF]
    write_yaml(root / "program" / "register.yaml", plan["source"]["register"])
    written.append("program/register.yaml")
    written += _write_statuses(root, plan)
    ctlstate.save(root, plan["state"])
    written.append(ctlstate.STATE_FILE)
    if plan["costs"]:
        with open(root / cost_module.COST_FILE, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(json.dumps(row, sort_keys=True) + "\n" for row in plan["costs"])
        written.append(cost_module.COST_FILE)
    write_yaml(root / adoption.ADOPTION_FILE, {
        "adopted_at": utc_now(), "adopted_commit": git(root, "rev-parse", "HEAD"),
        "source": adoption.SOURCE_COPY, "source_sha256": sha256_bytes(data),
        "legacy_phase_prefixes": plan["prefixes"], "ruling_start": plan["ruling_start"],
        "legacy_rulings": adoption.LEGACY_RULINGS, "legacy_rulings_sha256": sha256_bytes(rulings_bytes),
        "legacy_handoff": adoption.LEGACY_HANDOFF, "legacy_evidence_roots": plan["evidence_roots"]})
    written.append(adoption.ADOPTION_FILE)
    return written


def _post_check_failures(root: Path) -> list[str]:
    failures = []
    for argv in POST_CHECKS:
        code, said = _run_pk(root, *argv)
        if code != 0:
            failures.append(f"pk {' '.join(argv)} failed after the import: "
                            + " | ".join(said.splitlines()[-3:] or [f"exit {code}"]))
    return failures


def _summary(plan: dict) -> list[str]:
    state, costs = plan["state"], plan["costs"]
    by_prefix: dict[str, int] = {}
    for task in plan["tasks"]:
        key = task.split("-", 1)[0]
        key = "P" if re.match(r"P\d+\Z", key) else key
        by_prefix[key] = by_prefix.get(key, 0) + 1
    legacy = plan["legacy_rulings"]
    mapped = sum(1 for row in costs if row["task"].startswith("AH-") and "category" not in row)
    return [
        f"programme: {plan['source']['programme']}",
        f"register: {len(plan['tasks'])} tasks (" + ", ".join(f"{k} {v}" for k, v in sorted(by_prefix.items()))
        + f"); legacy phase prefixes: {', '.join(plan['prefixes']) or 'none'}",
        f"statuses: {len(plan['statuses'])}",
        f"rulings: {len(legacy)} legacy (R{legacy[0]} to R{legacy[-1]}) frozen in {adoption.LEGACY_RULINGS}; "
        f"the kit issues R{plan['ruling_start']} next",
        f"follow-ups: {len(state['followups'])} open, {len(state['closed_followups'])} closed; next "
        f"F-{state['followups_issued'] + 1:04d}",
        f"ad-hoc tasks: {len(state['adhoc'])} (AH-0001 to AH-{len(state['adhoc']):04d}, keeping legacy_id)"
        if state["adhoc"] else "ad-hoc tasks: 0",
        f"cost lines: {len(costs)} ({sum(1 for row in costs if row.get('legacy_task'))} legacy_task, {mapped} mapped "
        f"to AH- IDs)",
        f"legacy hand-off: {adoption.LEGACY_HANDOFF}; legacy evidence roots: "
        f"{', '.join(plan['evidence_roots']) or 'none'}",
    ]


def importer_source(root: Path, argv: list[str]) -> bytes:
    """The bytes of the source an extension's importer builds (`pk adopt --from <name> [args...]`): its dict as JSON."""
    if not argv:
        raise KitError("pk adopt --from needs the importer's name (pk ext list shows the registered importers)")
    import ext  # late: only an adoption through an extension needs it

    source = ext.run_importer(root, argv[0], argv[1:])
    try:
        return (json.dumps(source, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KitError(f"pk adopt --from {argv[0]}: the importer's source is not JSON-serialisable: {exc}; nothing "
                       "written") from exc


def adopt(root: Path, source_path: Path | None, dry_run: bool = False, data: bytes | None = None) -> list[str]:
    """Run `pk adopt`; the lines to print. KitError (after printing every problem) when it refuses or fails.

    `data`, when given, is the source itself (built by an extension's importer) and `source_path` is None."""
    root = Path(root).resolve()
    if data is None:
        try:
            data = Path(source_path).read_bytes()
        except OSError as exc:
            raise KitError(f"cannot read {source_path}: {exc.strerror or exc}") from exc
    problems = _Problems()
    _repository_problems(root, Path(source_path) if source_path is not None else None, problems)
    plan, found = validate_source(root, data)
    problems.extend(found)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        raise KitError(f"adopt: {len(problems)} problem(s); nothing written")
    lines = _summary(plan)
    if dry_run:
        return ["dry run: nothing written", *lines,
                "would write: program/legacy/ (rulings.md, HANDOFF.md, adoption-source.json), program/register.yaml, "
                "program/controller-state.json" + (", program/costs.jsonl" if plan["costs"] else "")
                + ", program/adoption.yaml"]
    saved = _snapshot(root)  # program/ is a fresh install here (step 1): a handful of files
    try:
        written = _write(root, plan, data)
        failures = _post_check_failures(root)
    except BaseException:
        _restore(root, saved)
        raise
    if failures:
        _restore(root, saved)
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        raise KitError(f"adopt: {len(failures)} check(s) failed after the import; every file it wrote is put back")
    return ["adopted:", *lines, "wrote: " + ", ".join(written) + ", program/HANDOFF.md (pk handoff update)",
            "checks: pk register validate, pk status validate, pk handoff update, pk doctor passed",
            "not committed: commit program/ as one commit"]


def cmd_adopt(args: argparse.Namespace) -> int:
    if (args.source is None) == (args.importer is None):
        raise KitError("pk adopt needs either a source file or --from <importer> [args...], not both")
    root = find_root()
    if args.importer is not None:
        data = importer_source(root, args.importer)
        print("\n".join(adopt(root, None, dry_run=args.dry_run, data=data)))
    else:
        print("\n".join(adopt(root, Path(args.source), dry_run=args.dry_run)))
    return 0


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", nargs="?", help="the adoption source, JSON (kit/templates/adoption.schema.json)")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="validate and print the plan and the counts; write nothing (before --from)")
    parser.add_argument("--from", dest="importer", nargs=argparse.REMAINDER, metavar="NAME",
                        help="build the source with the importer an extension registered under NAME; every "
                             "argument after NAME goes to the importer (0.7.0)")


def register(add_command) -> None:
    add_command("adopt", cmd_adopt, help="adopt an existing programme once, from a neutral JSON export",
                configure=_configure)
