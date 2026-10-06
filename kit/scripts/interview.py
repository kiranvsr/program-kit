#!/usr/bin/env python3
"""Interview mechanics: coverage map, requirements ledger, stopping rule and approvals.

The scripts here judge structure, never wording. A coverage map (`kit/interview/coverage.yaml`)
lists the topics of each stage and the ledger tags each one needs; the ledger
(`program/ledger.yaml`) records facts, assumptions, decisions and questions; `topic_states` and
`stopping_rule` decide from those two whether a stage may be approved; `approve` records the
artifact and ledger hashes in `program/approvals/stage-N.json`, and `next_stage`, `amend` and
`replay` work from those records.
"""
from __future__ import annotations

import argparse
import re
import string
import sys
from pathlib import Path

from common import (KitError, find_root, read_json, read_yaml, sha256_bytes, sha256_file, utc_now, write_json,
                    write_yaml)

KIT_DIR = Path(__file__).resolve().parent.parent
LENSES = ("architect", "engineering", "program", "project", "delivery")
STAGES = range(1, 8)
LAST_REPLAY_STAGE = 6
MAX_SOURCE_BYTES = 20 * 1024 * 1024
CHALLENGE_HEADER = "| # | Finding | Severity | Resolution |"
STATES = ("closed", "open", "deferred", "not_applicable")

_KIND_PREFIX = {"fact": "F", "assumption": "A", "decision": "D", "question": "Q"}
_ENTRY_ID = re.compile(r"([FADQ])-(\d+)\Z")
_SOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CONDITION_TOKEN = re.compile(r"\s*(\(|\)|has:[A-Za-z0-9_.-]+|(?:and|or|not)(?![A-Za-z0-9_.:-]))")


# --- conditions --------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    tokens, position = [], 0
    text = text.rstrip()
    while position < len(text):
        match = _CONDITION_TOKEN.match(text, position)
        if not match:
            raise KitError(f"condition {text!r}: cannot read it at {text[position:].strip()!r}")
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def evaluate_condition(condition: str | None, tags: set[str]) -> bool:
    """Evaluate `has:<tag>` combined with and / or / not and parentheses (not > and > or)."""
    if condition is None or not str(condition).strip():
        return True
    tokens = _tokens(str(condition))
    if not tokens:
        raise KitError(f"condition {condition!r} is empty")
    at = 0

    def fail(expected: str):
        found = tokens[at] if at < len(tokens) else "the end"
        raise KitError(f"condition {condition!r}: expected {expected}, found {found}")

    def disjunction() -> bool:
        nonlocal at
        value = conjunction()
        while at < len(tokens) and tokens[at] == "or":
            at += 1
            value = conjunction() or value  # evaluate both sides: a later syntax error is still an error
        return value

    def conjunction() -> bool:
        nonlocal at
        value = negation()
        while at < len(tokens) and tokens[at] == "and":
            at += 1
            value = negation() and value
        return value

    def negation() -> bool:
        nonlocal at
        if at < len(tokens) and tokens[at] == "not":
            at += 1
            return not negation()
        return atom()

    def atom() -> bool:
        nonlocal at
        if at >= len(tokens):
            fail("has:<tag> or '('")
        token = tokens[at]
        if token == "(":
            at += 1
            value = disjunction()
            if at >= len(tokens) or tokens[at] != ")":
                fail("')'")
            at += 1
            return value
        if token.startswith("has:"):
            at += 1
            return token[4:] in tags
        fail("has:<tag> or '('")

    result = disjunction()
    if at != len(tokens):
        fail("'and', 'or' or the end")
    return result


# --- coverage map ------------------------------------------------------------

def _string_list(value, who: str, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise KitError(f"{who}: {field} must be a list of strings")
    return list(value)


def _normalise_topic(topic_id: str, topic, where: str) -> dict:
    who = f"{where}: topic {topic_id}"
    if not isinstance(topic, dict):
        raise KitError(f"{who} must be a mapping")
    if topic.get("owner") not in LENSES:
        raise KitError(f"{who}: owner must be one of {', '.join(LENSES)}")
    contributes = _string_list(topic.get("contributes"), who, "contributes")
    for lens in contributes:
        if lens not in LENSES:
            raise KitError(f"{who}: contributes names unknown lens {lens!r}")
    required = topic.get("required", True)
    if not isinstance(required, bool):
        raise KitError(f"{who}: required must be true or false")
    condition = topic.get("condition")
    if condition is not None:
        if not isinstance(condition, str):
            raise KitError(f"{who}: condition must be a string or null")
        try:
            evaluate_condition(condition, set())
        except KitError as exc:
            raise KitError(f"{who}: {exc}") from exc
    return {"title": str(topic.get("title", topic_id)), "owner": topic["owner"], "contributes": contributes,
            "required": required, "condition": condition, "closes_when": str(topic.get("closes_when", "")),
            "needs": _string_list(topic.get("needs"), who, "needs")}


def load_coverage(root: Path, path: Path | None = None) -> dict:
    """The coverage map as `{"stages": {N: {name, budget, artifacts, topics: {id: {...}}}}}`, stage keys as ints.

    Without `path` it reads `<root>/kit/interview/coverage.yaml`, else the kit's own copy.
    """
    if path is None:
        path = Path(root) / "kit" / "interview" / "coverage.yaml"
        if not path.exists():
            path = KIT_DIR / "interview" / "coverage.yaml"
    path = Path(path)
    if not path.exists():
        raise KitError(f"coverage map not found: {path}")
    data = read_yaml(path)
    where = str(path)
    if not isinstance(data, dict) or not isinstance(data.get("stages"), dict):
        raise KitError(f"{where}: expected a mapping with a 'stages' mapping")
    stages: dict[int, dict] = {}
    seen: set[str] = set()
    for key, stage in data["stages"].items():
        if not re.fullmatch(r"\d+", str(key)):
            raise KitError(f"{where}: stage key {key!r} must be a number")
        if not isinstance(stage, dict):
            raise KitError(f"{where}: stage {key} must be a mapping")
        budget = stage.get("budget")
        if isinstance(budget, bool) or not isinstance(budget, int):
            raise KitError(f"{where}: stage {key}: budget must be an integer")
        raw_topics = stage.get("topics")
        raw_topics = {} if raw_topics is None else raw_topics
        if not isinstance(raw_topics, dict):
            raise KitError(f"{where}: stage {key}: topics must be a mapping")
        topics = {}
        for topic_id, topic in raw_topics.items():
            if topic_id in seen:
                raise KitError(f"{where}: duplicate topic {topic_id}")
            seen.add(topic_id)
            topics[str(topic_id)] = _normalise_topic(str(topic_id), topic, f"{where}: stage {key}")
        stages[int(key)] = {"name": str(stage.get("name", f"stage {key}")), "budget": budget,
                            "artifacts": _string_list(stage.get("artifacts"), f"{where}: stage {key}", "artifacts"),
                            "topics": topics}
    return {"stages": dict(sorted(stages.items()))}


def _check_stage(stage) -> int:
    if isinstance(stage, bool) or not isinstance(stage, int) or stage not in STAGES:
        raise KitError(f"stage must be a number from {STAGES[0]} to {STAGES[-1]}, got {stage!r}")
    return stage


def _stage(coverage: dict, stage: int) -> dict:
    """The map entry for `stage`; a stage the map does not list (such as 7) has a name and no topics."""
    _check_stage(stage)
    return coverage["stages"].get(stage, {"name": f"stage {stage}", "budget": None, "artifacts": [], "topics": {}})


# --- ledger ------------------------------------------------------------------

def _ledger_path(root: Path) -> Path:
    return Path(root) / "program" / "ledger.yaml"


def _check_ledger_shape(data, label: str) -> dict:
    if not isinstance(data, dict):
        raise KitError(f"{label}: expected a mapping with 'sources' and 'entries'")
    sources = data.get("sources", {})
    entries = data.get("entries", [])
    if not isinstance(sources, dict):
        raise KitError(f"{label}: 'sources' must be a mapping")
    if not isinstance(entries, list):
        raise KitError(f"{label}: 'entries' must be a list")
    for number, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            raise KitError(f"{label}: entry {number} must be a mapping")
    return {"sources": sources, "entries": entries}


def _load_ledger(root: Path) -> dict:
    path = _ledger_path(root)
    if not path.exists():
        return {"sources": {}, "entries": []}
    return _check_ledger_shape(read_yaml(path), str(path))


def _strings(value) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _label(item: dict, position: int) -> str:
    ident = item.get("id")
    return str(ident) if ident not in (None, "") else f"entries[{position}]"


def _nonempty_string(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_deferred(item: dict) -> bool:
    return "deferred" in _strings(item.get("tags"))


def _problems(coverage: dict, ledger: dict) -> list[str]:
    topics = {topic for stage in coverage["stages"].values() for topic in stage["topics"]}
    sources = ledger["sources"]
    problems: list[str] = []
    seen: set[str] = set()
    for position, item in enumerate(ledger["entries"], 1):
        who = _label(item, position)
        ident, kind = item.get("id"), item.get("kind")
        match = _ENTRY_ID.match(ident) if isinstance(ident, str) else None
        if not match:
            problems.append(f"{who}: id must look like F-<n>, A-<n>, D-<n> or Q-<n>")
        elif who in seen:
            problems.append(f"{who}: duplicate id")
        seen.add(who)
        if kind not in _KIND_PREFIX:
            problems.append(f"{who}: kind must be fact, assumption, decision or question")
        elif match and match.group(1) != _KIND_PREFIX[kind]:
            problems.append(f"{who}: id prefix does not match kind {kind}")
        listed = item.get("topics")
        if not isinstance(listed, list) or not listed or not all(isinstance(t, str) for t in listed):
            problems.append(f"{who}: topics must be a non-empty list")
        else:
            problems.extend(f"{who}: topic {t} is not in the coverage map" for t in listed if t not in topics)
        if "source" not in item or item["source"] in (None, ""):
            problems.append(f"{who}: source is missing")
        elif item["source"] not in sources:
            problems.append(f"{who}: source {item['source']} is not in sources")
        if not _nonempty_string(item.get("at")):
            problems.append(f"{who}: at is missing")
        if kind == "assumption" and not isinstance(item.get("confirmed"), bool):
            problems.append(f"{who}: confirmed must be true or false")
        if kind == "decision":
            ruling = item.get("ruling")
            if ruling is not None and (isinstance(ruling, bool) or not isinstance(ruling, int)):
                problems.append(f"{who}: ruling must be an integer or null")
        if kind == "question":
            if item.get("lens") not in LENSES:
                problems.append(f"{who}: question needs a lens (one of {', '.join(LENSES)})")
            if not isinstance(item.get("blocking"), bool):
                problems.append(f"{who}: blocking must be true or false")
        if _is_deferred(item) and not (_nonempty_string(item.get("owner")) and _nonempty_string(item.get("due"))):
            problems.append(f"{who}: deferred entry needs a non-empty owner and due")
    return problems


def ledger_validate(root: Path) -> list[str]:
    """Problems with `program/ledger.yaml`, each starting with the entry ID; empty when valid."""
    return _problems(load_coverage(root), _load_ledger(root))


# --- topic states ------------------------------------------------------------

def _all_tags(entries: list[dict]) -> set[str]:
    return {tag for item in entries for tag in _strings(item.get("tags"))}


def _topic_state(topic: dict, bearing: list[dict], all_tags: set[str]) -> dict:
    ids = [str(item.get("id")) for item in bearing]
    if not evaluate_condition(topic["condition"], all_tags):
        return {"state": "not_applicable", "entries": ids, "missing": []}
    deferred = [item for item in bearing if item.get("kind") == "decision" and _is_deferred(item)]
    valid = [item for item in deferred if _nonempty_string(item.get("owner")) and _nonempty_string(item.get("due"))]
    if valid:
        return {"state": "deferred", "entries": ids, "missing": []}
    counting = [item for item in bearing if item.get("kind") in ("fact", "decision")
                or (item.get("kind") == "assumption" and item.get("confirmed") is True)]
    present = {tag for item in counting for tag in _strings(item.get("tags"))}
    missing = [tag for tag in topic["needs"] if tag not in present]
    if topic["needs"]:
        closed = not missing
    else:
        closed = bool(counting)
    if deferred:  # a deferred entry without owner or due keeps the topic open
        closed = False
    return {"state": "closed" if closed else "open", "entries": ids, "missing": missing}


def topic_states(root: Path, stage: int) -> dict[str, dict]:
    """Per topic of `stage`, in coverage order: `{"state", "entries", "missing"}`."""
    topics = _stage(load_coverage(root), stage)["topics"]
    entries = _load_ledger(root)["entries"]
    tags = _all_tags(entries)
    return {topic_id: _topic_state(topic, [item for item in entries if topic_id in _strings(item.get("topics"))], tags)
            for topic_id, topic in topics.items()}


# --- stopping rule -----------------------------------------------------------

def _stage_questions(entries: list[dict], topic_ids) -> list[dict]:
    return [item for item in entries if item.get("kind") == "question"
            and set(_strings(item.get("topics"))) & set(topic_ids)]


def _answers(item: dict) -> list[str]:
    value = item.get("answers")
    return [value] if isinstance(value, str) else _strings(value)


def _unanswered(entries: list[dict]) -> set[str]:
    """IDs of questions with neither `answered: true` nor a later fact or decision listing them in `answers`."""
    later: set[str] = set()
    open_ids: set[str] = set()
    for item in reversed(entries):
        if item.get("kind") in ("fact", "decision"):
            later.update(_answers(item))
        elif item.get("kind") == "question" and item.get("answered") is not True and item.get("id") not in later:
            open_ids.add(str(item.get("id")))
    return open_ids


def _challenge_path(root: Path, stage: int) -> Path:
    return Path(root) / "program" / "reports" / f"stage-{stage}-challenge.md"


def _unresolved_findings(path: Path) -> int:
    """Rows of the findings tables whose Resolution cell (the last one) is empty."""
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KitError(f"cannot read {path}: {getattr(exc, 'strerror', None) or exc}") from exc
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in (line[1:-1] if line.endswith("|") and len(line) > 1 else line[1:]).split("|")]
        if cells[0] == "#" or all(re.fullmatch(r":?-+:?", cell) for cell in cells) or not any(cells):
            continue  # header, separator, blank row
        if len(cells) < 4 or not cells[-1]:
            count += 1
    return count


def _register_problems(root: Path) -> list[str]:
    import register as register_module

    if not (Path(root) / "program" / "register.yaml").exists():
        return []
    environments_path = Path(root) / "program" / "environments.yaml"
    environments = read_yaml(environments_path) if environments_path.exists() else None
    roles = read_yaml(KIT_DIR / "interview" / "roles.yaml")
    return register_module.validate(register_module.load(root), environments, roles)


def stopping_rule(root: Path, stage: int) -> list[str]:
    """Reasons `stage` cannot be approved, each starting with a topic or entry ID or a file name."""
    coverage = load_coverage(root)
    topics = _stage(coverage, stage)["topics"]
    entries = _load_ledger(root)["entries"]
    reasons: list[str] = []
    for topic_id, state in topic_states(root, stage).items():
        if state["state"] == "open" and topics[topic_id]["required"]:
            missing = ", ".join(state["missing"]) or "any fact, confirmed assumption or decision"
            reasons.append(f"{topic_id}: required topic open (missing: {missing})")
    unanswered = _unanswered(entries)
    for item in _stage_questions(entries, topics):
        if item.get("blocking") is True and str(item.get("id")) in unanswered:
            reasons.append(f"{item.get('id')}: blocking question unanswered")
    findings = _unresolved_findings(_challenge_path(root, stage))
    if findings:
        noun = "finding" if findings == 1 else "findings"
        reasons.append(f"stage-{stage}-challenge.md: {findings} {noun} unresolved")
    if stage >= 5:
        reasons.extend(f"{item.get('id')}: assumption unconfirmed at stage {stage}" for item in entries
                       if item.get("kind") == "assumption" and item.get("confirmed") is not True)
    if stage in (5, 6):
        import trace as trace_module  # lazy: trace imports register and status

        reasons.extend(trace_module.validate(root, stage))
    if stage == 7:
        reasons.extend(_register_problems(root))
    return reasons


# --- reports -----------------------------------------------------------------

def _template(name: str) -> string.Template:
    path = KIT_DIR / "templates" / name
    try:
        return string.Template(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc.strerror or exc}") from exc


def _write_text(path: Path, text: str, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x" if exclusive else "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise KitError(f"{path} already exists; refusing to overwrite") from exc
    except OSError as exc:
        raise KitError(f"cannot write {path}: {exc.strerror or exc}") from exc


def _cell(text) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def report(root: Path, stage: int) -> Path:
    """Write `program/reports/stage-N.md`: every topic with its state, owner, gaps and entries."""
    coverage = load_coverage(root)
    info = _stage(coverage, stage)
    entries = _load_ledger(root)["entries"]
    states = topic_states(root, stage)
    rows = ["| Topic | Title | Owner | State | Missing | Entries |", "|---|---|---|---|---|---|"]
    for topic_id, state in states.items():
        topic = info["topics"][topic_id]
        rows.append("| " + " | ".join(_cell(value) for value in (
            topic_id, topic["title"], topic["owner"], state["state"], ", ".join(state["missing"]),
            ", ".join(state["entries"]))) + " |")
    questions = _stage_questions(entries, info["topics"])
    unanswered = _unanswered(entries)
    counts = {name: sum(1 for state in states.values() if state["state"] == name) for name in STATES}
    text = _template("stage_report.md").substitute(
        stage=stage, name=info["name"], closed=counts["closed"], open=counts["open"], deferred=counts["deferred"],
        not_applicable=counts["not_applicable"], questions_asked=len(questions),
        budget="none" if info["budget"] is None else info["budget"],
        blocking=sum(1 for q in questions if q.get("blocking") is True and str(q.get("id")) in unanswered),
        table="\n".join(rows))
    path = Path(root) / "program" / "reports" / f"stage-{stage}.md"
    _write_text(path, text)
    return path


def questions_line(root: Path, stage: int) -> str:
    info = _stage(load_coverage(root), stage)
    asked = len(_stage_questions(_load_ledger(root)["entries"], info["topics"]))
    return f"questions asked: {asked} of budget {'none' if info['budget'] is None else info['budget']}"


def challenge_init(root: Path, stage: int) -> Path:
    """Write the challenge report skeleton, one section per lens; never overwrites an existing report."""
    info = _stage(load_coverage(root), stage)
    sections = "\n\n".join(f"## {lens}\n\n{CHALLENGE_HEADER}\n|---|---|---|---|" for lens in LENSES)
    text = _template("challenge_report.md").substitute(stage=stage, name=info["name"], sections=sections)
    path = _challenge_path(root, stage)
    _write_text(path, text.rstrip("\n") + "\n", exclusive=True)
    return path


# --- approvals ---------------------------------------------------------------

def _approval_path(root: Path, stage: int) -> Path:
    return Path(root) / "program" / "approvals" / f"stage-{stage}.json"


def _relpath(root: Path, path: Path | str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(root) / candidate
    try:
        relative = candidate.resolve().relative_to(Path(root).resolve())
    except ValueError:
        raise KitError(f"artifact {path} is outside the project root") from None
    if not candidate.is_file():
        raise KitError(f"artifact {relative.as_posix()} not found")
    return relative.as_posix()


def _ledger_hash(root: Path) -> str | None:
    path = _ledger_path(root)
    return sha256_file(path) if path.exists() else None


def approve(root: Path, stage: int, artifacts: list[Path]) -> Path:
    """Write `program/approvals/stage-N.json`, or refuse with the stopping-rule reasons (writing nothing)."""
    _check_stage(stage)
    reasons = stopping_rule(root, stage)
    if reasons:
        raise KitError("; ".join(reasons))
    ledger_hash = _ledger_hash(root)
    if ledger_hash is None:
        raise KitError("program/ledger.yaml is missing")
    hashes = {}
    for artifact in artifacts:
        relative = _relpath(root, artifact)
        hashes[relative] = sha256_file(Path(root) / relative)
    path = _approval_path(root, stage)
    write_json(path, {"stage": stage, "at": utc_now(), "artifacts": dict(sorted(hashes.items())),
                      "ledger_sha256": ledger_hash})
    return path


def _stale(root: Path, record, check_ledger: bool) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("artifacts"), dict):
        return True
    for relative, recorded in record["artifacts"].items():
        try:
            if sha256_file(Path(root) / relative) != recorded:
                return True
        except KitError:
            return True
    return check_ledger and record.get("ledger_sha256") != _ledger_hash(root)


def next_stage(root: Path) -> int:
    """First stage 1..7 that is unapproved or whose approved files changed; 8 when all are current.

    The ledger grows during later stages, so its hash is compared only for the latest approval.
    """
    approved = [stage for stage in STAGES if _approval_path(root, stage).exists()]
    for stage in STAGES:
        if stage not in approved:
            return stage
        if _stale(root, read_json(_approval_path(root, stage)), check_ledger=stage == approved[-1]):
            return stage
    return STAGES[-1] + 1


def amend(root: Path, stage: int) -> list[str]:
    """Re-hash an approval's artifacts and the ledger; rewrite it if any changed; return the changed paths."""
    _check_stage(stage)
    path = _approval_path(root, stage)
    if not path.exists():
        raise KitError(f"stage {stage} has no approval to amend")
    record = read_json(path)
    if not isinstance(record, dict) or not isinstance(record.get("artifacts"), dict):
        raise KitError(f"{path}: not a valid approval record")
    changed, hashes = [], {}
    for relative, recorded in record["artifacts"].items():
        hashes[relative] = sha256_file(Path(root) / relative)
        if hashes[relative] != recorded:
            changed.append(relative)
    ledger_hash = _ledger_hash(root)
    if ledger_hash is None:
        raise KitError("program/ledger.yaml is missing")
    if ledger_hash != record.get("ledger_sha256"):
        changed.append("program/ledger.yaml")
    if changed:
        write_json(path, {"stage": stage, "at": utc_now(), "artifacts": hashes, "ledger_sha256": ledger_hash})
    return sorted(changed)


# --- ingest and replay -------------------------------------------------------

def ingest(root: Path, file: Path) -> str:
    """Copy `file` into `program/sources/`, record it as a `document` source and return its ID."""
    source = Path(file)
    if source.is_symlink():
        raise KitError(f"{source}: refusing to ingest a symbolic link")
    if not source.is_file():
        raise KitError(f"{source}: not a file")
    if not _SOURCE_NAME.match(source.name):
        raise KitError(f"{source.name}: file name must match [A-Za-z0-9][A-Za-z0-9._-]*")
    try:
        with open(source, "rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise KitError(f"cannot read {source}: {exc.strerror or exc}") from exc
    if len(data) > MAX_SOURCE_BYTES:
        raise KitError(f"{source.name}: larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB")
    ledger = _load_ledger(root)
    numbers = [int(m.group(1)) for key in ledger["sources"] if (m := re.fullmatch(r"S(\d+)", str(key)))]
    source_id = f"S{max(numbers, default=0) + 1}"
    stored = Path(root) / "program" / "sources" / f"{sha256_bytes(data)[:12]}-{source.name}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    try:
        stored.write_bytes(data)
    except OSError as exc:
        raise KitError(f"cannot write {stored}: {exc.strerror or exc}") from exc
    ledger["sources"][source_id] = {"kind": "document", "ref": stored.relative_to(Path(root)).as_posix(),
                                    "at": utc_now()}
    write_yaml(_ledger_path(root), ledger)
    return source_id


def replay(root: Path, ledger: Path, artifacts_dir: Path) -> None:
    """Install a recorded ledger and artifacts, then approve each mapped stage up to 6, stopping at the first gap.

    Stage artifacts in the coverage map are paths relative to `program/`; stages the map does not list are skipped.
    """
    root, ledger, artifacts_dir = Path(root), Path(ledger), Path(artifacts_dir)
    coverage = load_coverage(root)
    data = _check_ledger_shape(read_yaml(ledger), str(ledger))
    problems = _problems(coverage, data)
    if problems:
        raise KitError("ledger invalid: " + "; ".join(problems))
    if not artifacts_dir.is_dir():
        raise KitError(f"{artifacts_dir}: not a directory")
    copies = []
    for source in sorted(artifacts_dir.rglob("*")):
        relative = source.relative_to(artifacts_dir)
        if source.is_file() and not source.is_symlink() and not any(part.startswith(".") for part in relative.parts):
            copies.append((source, relative))
    approvals = root / "program" / "approvals"
    for old in approvals.glob("stage-*.json") if approvals.is_dir() else ():
        old.unlink()
    _write_bytes(_ledger_path(root), ledger.read_bytes())
    for source, relative in copies:
        _write_bytes(root / "program" / relative, source.read_bytes())
    for stage, info in coverage["stages"].items():
        if stage > LAST_REPLAY_STAGE:
            continue
        reasons = stopping_rule(root, stage)
        if reasons:
            raise KitError(f"replay stopped at stage {stage}: " + "; ".join(reasons))
        try:
            approve(root, stage, [root / "program" / relative for relative in info["artifacts"]])
        except KitError as exc:
            raise KitError(f"replay stopped at stage {stage}: {exc}") from exc


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise KitError(f"cannot write {path}: {exc.strerror or exc}") from exc


# --- command -----------------------------------------------------------------

ACTIONS = ("next", "report", "challenge", "approve", "amend", "ingest", "replay", "validate")


def _show(root: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def cmd_interview(args: argparse.Namespace) -> int:
    action = getattr(args, "interview_action", None)
    if action not in ACTIONS:
        raise KitError("interview needs an action: " + ", ".join(ACTIONS))
    root = find_root()
    if action == "next":
        stage = next_stage(root)
        if stage > STAGES[-1]:
            print("all stages approved")
            return 0
        info = _stage(load_coverage(root), stage)
        states = topic_states(root, stage)
        applicable = [s for s in states.values() if s["state"] != "not_applicable"]
        done = sum(1 for s in applicable if s["state"] in ("closed", "deferred"))
        print(f"next stage: {stage} ({info['name']})\ntopics closed: {done} of {len(applicable)}")
    elif action == "report":
        path = report(root, args.stage)
        print(f"{_show(root, path)}\n{questions_line(root, args.stage)}")
    elif action == "challenge":
        print(_show(root, challenge_init(root, args.stage)))
    elif action == "approve":
        artifacts = [Path.cwd() / item for item in args.artifact] if args.artifact else [
            root / "program" / item for item in _stage(load_coverage(root), args.stage)["artifacts"]]
        if not artifacts:
            raise KitError(f"stage {args.stage} names no artifacts; pass --artifact")
        print(f"approved stage {args.stage}: {_show(root, approve(root, args.stage, artifacts))}")
    elif action == "amend":
        changed = amend(root, args.stage)
        print("\n".join(changed) if changed else "no changes")
    elif action == "ingest":
        source_id = ingest(root, Path.cwd() / args.file)
        print(f"{source_id} {_read_ref(root, source_id)}")
    elif action == "replay":
        replay(root, Path.cwd() / args.ledger, Path.cwd() / args.artifacts_dir)
        print("replay complete")
    else:
        problems = ledger_validate(root)
        if problems:
            print("\n".join("error: " + problem for problem in problems), file=sys.stderr)
            return 1
        print(f"ledger valid: {len(_load_ledger(root)['entries'])} entries")
    return 0


def _read_ref(root: Path, source_id: str) -> str:
    return str(_load_ledger(root)["sources"][source_id]["ref"])


def _configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="interview_action", metavar="action", parser_class=type(parser))
    actions.add_parser("next", help="name the next stage to work on")
    for name, text in (("report", "write the stage coverage report"),
                       ("challenge", "write the challenge report skeleton"),
                       ("amend", "re-hash an approval after a correction")):
        actions.add_parser(name, help=text).add_argument("stage", type=int, help="interview stage, 1 to 7")
    approver = actions.add_parser("approve", help="approve a stage if the stopping rule holds")
    approver.add_argument("stage", type=int, help="interview stage, 1 to 7")
    approver.add_argument("--artifact", nargs="+", metavar="PATH",
                          help="artifact files (default: the coverage map's list for the stage)")
    actions.add_parser("ingest", help="copy a document into program/sources/ and record it").add_argument(
        "file", help="the document to ingest")
    replayer = actions.add_parser("replay", help="install a recorded ledger and approve stages 1 to 6")
    replayer.add_argument("ledger", help="recorded ledger.yaml")
    replayer.add_argument("artifacts_dir", help="directory of artifacts to install under program/")
    actions.add_parser("validate", help="check program/ledger.yaml")


def register(add_command) -> None:
    add_command("interview", cmd_interview, help="interview: coverage, ledger, approvals", configure=_configure)
