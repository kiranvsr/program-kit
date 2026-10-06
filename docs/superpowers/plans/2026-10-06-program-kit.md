# program-kit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `program-kit`, an open-source, provider-neutral repository template plus installer that turns a short brief into a governed delivery program (lens-driven interview over a coverage map, frozen spec, task register) and runs it with implementers, independent QA, commit-bound evidence and a maintained hand-off.

**Architecture:** A new git repository at `/Users/kiran/Projects/program-kit`. Everything mechanical lives in `kit/scripts/*.py` (standard-library Python 3.11+, one module per responsibility, one CLI entry `pk.py`), everything an agent reads lives in `kit/playbooks`, `kit/briefs`, `kit/rules`, `kit/interview` and `kit/templates`. A project gets `kit/` (owned by the kit, replaced on upgrade) and `program/` (owned by the project, created by `pk init`). `examples/tiny-service` exercises the whole flow end to end in a temporary repository.

**Tech Stack:** Python 3.11 standard library only (no PyYAML: machine-read files use a strict YAML subset parsed by `kit/scripts/miniyaml.py`); bash for `install.sh`; git; `unittest` for tests.

**Spec:** `docs/superpowers/specs/2026-10-06-program-kit-design.md` (in the ent-harness repo; copied to `docs/DESIGN.md` of program-kit in Task 17).

## Global Constraints

- Python 3.11 or newer; standard library only; every script runs as `python3 kit/scripts/pk.py <command>` and also as a module import from `kit/scripts`.
- No file under `kit/`, `examples/` or `docs/` names a model, vendor or agent CLI. Forbidden tokens, tested in Task 17: `Claude`, `Anthropic`, `OpenAI`, `GPT`, `Codex`, `Gemini`, `Grok`, `ent-harness`, `Fable`, `Opus`, `Sonnet`.
- Machine-read files (`register.yaml`, `coverage.yaml`, `ledger.yaml`, `checks.yaml`, `environments.yaml`, `gates.yaml`, `traceability.yaml`, `seeds.yaml`, `roles.yaml`, presets) use the YAML subset of Task 1: mappings, lists, scalars (string, int, float, bool, null), indentation nesting, `#` comments, single- and double-quoted strings. No anchors, tags, flow collections or multi-line scalars.
- Commands in `checks.yaml` and PLAN files are argv lists; scripts run them without a shell.
- Every script exits non-zero with a one-line reason on the first invalid input and writes no partial output.
- Hand-back gate final line, exactly: `OK: <branch> passes the hand-back gate at <sha>`. Merge check final line, exactly: `OK: <branch> merges into main at <sha>`. Nothing else may print a line starting with `OK: `.
- Stable IDs: tasks `P<n>-M<n>-S<n>-T<n>`; personas `U<n>`; journeys `J<n>`; requirements `REQ-<nnn>`; non-functional `NFR-<nnn>`; components `C<n>`; gates `G-<NAME>`; rulings `R<n>`; change requests `CR-<nnn>`; ledger entries `F-<n>`, `A-<n>`, `D-<n>`, `Q-<n>`.
- Status values: `not_started`, `in_progress`, `review`, `done`, `blocked`, `parked`. Only `status.py` writes `program/status/*.json`.
- Fetch cache root: `~/.cache/program-kit/fetch/<sha256>/<filename>`, overridable by `PROGRAM_KIT_CACHE`. Temp files are created with `tempfile.mkstemp` in the digest directory; names match `[A-Za-z0-9][A-Za-z0-9._-]*`; symlinked entries or directories are a miss.
- Commits in the program-kit repo end with the attribution line of the session that makes them.
- Nothing is pushed; the repository has no remote until the user adds one.

## Review Focus

1. A register whose dependency graph has a cycle through two phases must fail with the cycle path in the message, not hang or stack-overflow (Task 5 test `test_validate_reports_cycle_path`).
2. A ledger open question with no lens, or any entry whose source is missing, must be refused by `approve`, since artifacts are written from the ledger (Task 8 test `test_ledger_refuses_question_without_lens_and_entry_without_source`).
3. A gate command that writes into the tree (generated file, cache) must make `evidence.write` refuse and name the path, otherwise evidence binds to a tree nobody committed (Task 10 test `test_write_refuses_when_command_dirtied_tree`).
4. A cached fetch entry replaced by a symlink after fetching must be treated as a miss on verify and must not be followed on removal (Task 11 tests `test_verify_treats_symlinked_entry_as_miss`, `test_fetch_replaces_symlinked_entry_without_following`).
5. Marking a task `done` with an acceptance file bound to an older evidence hash must be refused, otherwise a stale acceptance passes a changed branch (Task 3 test `test_done_refuses_acceptance_bound_to_other_evidence`).

---

### Task 1: Repository scaffold, common helpers, YAML subset

**Files:**
- Create: `LICENSE` (MIT, copyright holder from `git config user.name`), `README.md` (title and one paragraph; completed in Task 17), `.gitignore` (`__pycache__/`, `.worktrees/`, `*.pyc`), `kit/VERSION` (`0.1.0`)
- Create: `kit/scripts/common.py`, `kit/scripts/miniyaml.py`
- Create: `kit/tests/_paths.py`, `kit/tests/test_common.py`, `kit/tests/test_miniyaml.py`

**Interfaces:**
- Produces `common.py`: `class KitError(Exception)`; `find_root(start: Path | None = None) -> Path` (nearest ancestor containing `program/` or `.git`, else `KitError`); `sha256_file(path: Path) -> str`; `sha256_bytes(data: bytes) -> str`; `utc_now() -> str` (`YYYY-MM-DDTHH:MM:SSZ`); `read_yaml(path: Path)`; `write_yaml(path: Path, data) -> None` (atomic: temp file in the same directory then `os.replace`); `read_json(path) / write_json(path, data)` (atomic, `indent=2`, trailing newline); `git(root: Path, *args: str) -> str` (stdout stripped, `KitError` on non-zero); `run_argv(argv: list[str], cwd: Path, env: dict | None = None, timeout: int | None = None) -> tuple[int, str]` (combined output, never a shell).
- Produces `miniyaml.py`: `loads(text: str)`; `dumps(data) -> str`; `class YAMLError(KitError)`.
- Test bootstrap: every test file starts with `import _paths` which inserts `kit/scripts` into `sys.path`.

- [ ] **Step 1: Initialise the repository**

```bash
mkdir -p /Users/kiran/Projects/program-kit && cd /Users/kiran/Projects/program-kit && git init -b main
```

- [ ] **Step 2: Write the failing tests**

`kit/tests/test_miniyaml.py`:
```python
def test_round_trip_nested_mapping_list_scalars(self):
    data = {"a": 1, "b": [1, "two", True, None, 2.5], "c": {"d": "x y", "e": []}, "q": "it's: here"}
    self.assertEqual(miniyaml.loads(miniyaml.dumps(data)), data)
def test_comments_and_quotes(self):
    self.assertEqual(miniyaml.loads("# c\nk: 'v # not comment'\nn: \"1\"\n"), {"k": "v # not comment", "n": "1"})
def test_rejects_anchors_tags_flow_and_tabs(self):
    for text in ("a: &x 1\n", "a: !!str 1\n", "a: [1, 2]\n", "a:\n\tb: 1\n"):
        with self.assertRaises(miniyaml.YAMLError): miniyaml.loads(text)
def test_bad_indent_names_line(self):
    with self.assertRaisesRegex(miniyaml.YAMLError, "line 3"): miniyaml.loads("a:\n  b: 1\n c: 2\n")
```

`kit/tests/test_common.py`:
```python
def test_find_root_walks_up_to_program_dir(self): ...  # tmp/x/program exists; find_root(tmp/x/a/b) == tmp/x
def test_find_root_raises_outside_any_project(self): ...  # KitError
def test_write_yaml_is_atomic_and_round_trips(self): ...  # no leftover temp files in dir
def test_run_argv_never_uses_a_shell(self): ...  # run_argv(["echo", "$HOME"]) output == "$HOME"
def test_git_raises_kiterror_with_stderr(self): ...  # git(root, "rev-parse", "nope") -> KitError containing "nope"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/kiran/Projects/program-kit && python3 -m unittest discover -s kit/tests -v`
Expected: FAIL with `ModuleNotFoundError` for `miniyaml` and `common`.

- [ ] **Step 4: Implement `miniyaml.py` and `common.py` with the signatures above**

Parser: line-based recursive descent on indentation; scalars resolve `true/false/null/~`, ints, floats, quoted strings (`''` and `\"` escapes), else plain string; errors carry `line N`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest discover -s kit/tests -v` — Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: scaffold program-kit with common helpers and YAML subset"
```

---

### Task 2: `pk.py` CLI, `init` and `doctor`, base templates

**Files:**
- Create: `kit/scripts/pk.py`
- Create: `kit/templates/BRIEF.md`, `kit/templates/checks.yaml`, `kit/templates/environments.yaml`, `kit/templates/CLAIM_BOUNDARIES.md`, `kit/templates/ledger.yaml`, `kit/templates/HANDOFF.md`, `kit/templates/controller-state.json`, `kit/templates/rulings.md`
- Create: `kit/tests/test_pk.py`

**Interfaces:**
- Produces `pk.py`: `main(argv: list[str] | None = None) -> int`; subcommand registry `COMMANDS: dict[str, Callable[[argparse.Namespace], int]]`; later tasks register `status`, `rulings`, `register`, `briefs`, `trace`, `interview`, `change`, `evidence`, `stale`, `fetch`, `gate`, `worktree`, `handoff`. `KitError` is caught in `main`, printed as `error: <message>` to stderr, exit 1.
- Produces `pk init [--preset go|node|python]`: creates `program/` from templates, refuses if it exists; `pk doctor`: checks Python ≥ 3.11, git available, `program/` present, `checks.yaml` parses, prints `doctor: ok` or the first problem.
- `HANDOFF.md` template contains the literal markers `<!-- generated:start -->` and `<!-- generated:end -->` used by Task 13.
- `controller-state.json` template: `{"stage": {}, "queued": {}, "parked": {}, "followups": []}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_init_creates_program_tree_and_refuses_twice(self): ...  # files listed above exist under tmp/program; second init -> rc 1, "exists"
def test_init_with_preset_copies_checks(self): ...  # preset file content equals program/checks.yaml (presets created in Task 12; this test is marked skip until then via self.skipTest if kit/presets missing)
def test_doctor_ok_after_init(self): ...  # rc 0, stdout "doctor: ok"
def test_unknown_command_exits_1(self): ...
def test_kiterror_is_one_line_on_stderr(self): ...
```

- [ ] **Step 2: Run tests to verify they fail** — Expected: FAIL, no `pk` module.

- [ ] **Step 3: Implement `pk.py`, `init`, `doctor` and the eight templates**

Templates are plain files copied verbatim; `BRIEF.md` is a two-line prompt ("Write a few lines: what is to be built, for whom, known constraints."); `CLAIM_BOUNDARIES.md` has headings `Claimable in development`, `Unclaimable until the named environment exists`, `Never claimed`.

- [ ] **Step 4: Run tests** — Expected: PASS (one skipped).

- [ ] **Step 5: Commit** — `git commit -m "feat: pk CLI with init and doctor"`

---

### Task 3: Status state machine

**Files:**
- Create: `kit/scripts/status.py`, `kit/templates/status.schema.json`, `kit/tests/test_status.py`

**Interfaces:**
- Produces: `STATUSES`, `TRANSITIONS: dict[tuple[str, str], set[str]]` (from, to) → allowed roles, exactly the spec §4.3 table; `read(root, task_id) -> dict`; `list_all(root) -> list[dict]`; `transition(root, task_id, to: str, role: str, note: str = "", evidence: list[str] | None = None, blocked_by: list[str] | None = None) -> dict`; `validate(root) -> list[str]`.
- Record shape (`status.schema.json`): `{"task_id", "status", "role", "updated_at", "note", "evidence": [paths], "blocked_by": [str], "history": [{status, role, at}]}`.
- `done` requires an acceptance file among `evidence` whose JSON `evidence_sha256` equals `sha256_file` of the task's `results.json`; the file path convention is `program/evidence/<ID>/acceptance.json` and `program/evidence/<ID>/results.json`.
- CLI: `pk status set <ID> <status> --role <role> [--note] [--evidence path]*`, `pk status show <ID>`, `pk status list`, `pk status validate`.

- [ ] **Step 1: Write the failing tests**

```python
def test_allowed_transitions_by_role(self): ...  # not_started->in_progress controller ok; implementer refused
def test_review_to_done_only_qa_with_bound_acceptance(self): ...
def test_done_refuses_acceptance_bound_to_other_evidence(self): ...  # acceptance evidence_sha256 != results sha -> KitError names both hashes
def test_done_is_terminal(self): ...
def test_blocked_requires_blocked_by(self): ...
def test_validate_reports_unknown_status_and_missing_evidence(self): ...
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement `status.py` and register it in `pk.py`**
- [ ] **Step 4: Run tests** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: status state machine"`

---

### Task 4: Rulings log

**Files:**
- Create: `kit/scripts/rulings.py`, `kit/tests/test_rulings.py`

**Interfaces:**
- Produces: `add(root, role: str, question: str, decision: str, precedent: str = "") -> int` (next number; appends one block to `program/rulings.md`: `## R<n> (<date>, <role>)` then `**Question.** …`, `**Decision.** …`, `**Precedent.** …`); `read(root) -> list[dict]` (`number, date, role, question, decision, precedent, supersedes: list[int]`); `validate(root) -> list[str]` (sequential numbers from 1, no duplicates, every `supersedes R<n>` mention refers to an earlier ruling).
- CLI: `pk rulings add --role --question --decision [--precedent]`, `pk rulings list`, `pk rulings validate`.

- [ ] **Step 1: Write the failing tests** — `test_add_numbers_sequentially_and_dates`, `test_validate_detects_gap_and_duplicate`, `test_supersedes_must_point_backwards`, `test_add_is_append_only` (earlier text byte-identical after add).
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: append-only rulings log"`

---

### Task 5: Register validation and IDs

**Files:**
- Create: `kit/scripts/register.py`, `kit/templates/register.yaml`, `kit/interview/roles.yaml`, `kit/tests/test_register.py`

**Interfaces:**
- Register shape (`register.yaml`): `phases: [{id: P1, title, gate, milestones: [{id: P1-M01, title, gate, steps: [{id: P1-M01-S01, title, tasks: [{id: P1-M01-S01-T01, title, text, role, size: S|M|L, environments: [str], depends_on: [ID], gate: G-X|null, closes: [REQ-/J- IDs]}]}]}]}]`.
- `roles.yaml`: list of `{id, name, may_touch: [component IDs or "*"]}`; shipped roles: `backend`, `frontend`, `platform`, `data`, `security`, `docs`, `qa`.
- Produces: `load(root) -> dict`; `assign_ids(register: dict) -> dict` (IDs derived from position, stable for unchanged order, existing IDs kept); `validate(register: dict, environments: dict | None, roles: dict | None) -> list[str]` checks: unique IDs; `depends_on` targets exist; no cycles (message `cycle: A -> B -> A`); size `L` not allowed on a task (`split into steps`); fan-in (`len(depends_on)`) ≤ 6; role exists in `roles.yaml`; a task listing an environment whose `exists: false` must carry `blocked_by: [<env>]`; `closes` IDs match the ID patterns; `change(root, note: str) -> None` appends to `program/changes/register-log.md` with date and regenerates briefs (calls Task 6).
- CLI: `pk register validate`, `pk register assign-ids`, `pk register change --note`.

- [ ] **Step 1: Write the failing tests** — `test_assign_ids_is_stable_and_positional`, `test_validate_reports_cycle_path`, `test_validate_rejects_size_L_task_and_fan_in_over_6`, `test_missing_environment_needs_blocked_by`, `test_unknown_role_and_dependency`.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement** — cycle detection by iterative DFS with an explicit stack (no recursion).
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: register validation and stable IDs"`

---

### Task 6: Brief generation

**Files:**
- Create: `kit/scripts/briefs.py`, `kit/templates/phase_brief.md`, `kit/templates/milestone_brief.md`, `kit/templates/step_brief.md`, `kit/templates/task_brief.md`, `kit/tests/test_briefs.py`

**Interfaces:**
- Produces: `generate(root) -> list[Path]` writing `program/briefs/phases/<P>.md`, `milestones/<P-M>.md`, `steps/<P-M-S>.md`, `tasks/<ID>.md` plus `program/briefs/CATALOG.md` (one line per task: ID, title, role, gate, depends_on) and `program/briefs/MANIFEST.json` (`{path: sha256}`); `check(root) -> list[str]` (regenerates to memory and lists paths whose content differs from disk; "immutable once approved" is enforced by `check` failing after `approvals/stage-6.json` exists unless `program/changes/register-log.md` has an entry newer than the approval).
- Templates use `string.Template` with `$id $title $text $role $size $environments $depends_on $gate $closes $phase $milestone $step`; task brief table rows exactly: Phase, Milestone, Step, Agent role, Size, Environments, Depends on, Target gate, Closes; then `**Task**`, `**Unit tests required**`, `**Integration tests required**`, `**Definition of done**` from the task's `tests_unit`, `tests_integration`, `done` fields (optional strings).
- CLI: `pk briefs generate`, `pk briefs check`.

- [ ] **Step 1: Write the failing tests** — `test_generate_is_deterministic` (two runs identical bytes), `test_task_brief_has_required_rows`, `test_check_flags_drift_after_edit`, `test_check_requires_change_log_after_freeze`.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: deterministic brief generation"`

---

### Task 7: Traceability

**Files:**
- Create: `kit/scripts/trace.py`, `kit/templates/traceability.yaml`, `kit/templates/gates.yaml`, `kit/tests/test_trace.py`

**Interfaces:**
- `traceability.yaml`: `journeys: {J1: {title, requirements: [REQ-001]}}`, `requirements: {REQ-001: {title, journeys: [J1], components: [C1], nfr: false}}`, `components: {C1: {title, requirements: [...]}}`, `gates: {G-P0: {claims: [str], records: [path]}}`, `tasks: {P1-M01-S01-T01: {closes: [REQ-001, J1]}}` (tasks section filled from the register by `sync`).
- `gates.yaml`: `gates: {G-P0: {title, claims: [str], environment: str, records: [{path, acceptance}]}}`.
- Produces: `validate(root, stage: int) -> list[str]`: every journey has ≥1 requirement; every requirement names ≥1 journey (or `nfr: true`) and ≥1 component (stage ≥ 4); every gate in `gates.yaml` has ≥1 record (stage ≥ 4); every task closes only known IDs and every first-release requirement is closed by ≥1 task (stage ≥ 6); IDs match the global patterns. `sync(root) -> None` copies `closes` from the register into the `tasks` section. `impact(root, ids: list[str]) -> dict` → `{"requirements": [...], "components": [...], "gates": [...], "tasks": {"done": [...], "in_flight": [...], "not_started": [...]}}` using `status.list_all`.
- CLI: `pk trace validate --stage N`, `pk trace sync`, `pk trace impact ID...`.

- [ ] **Step 1: Write the failing tests** — `test_validate_stage_gates_apply_progressively`, `test_journey_without_requirement_fails`, `test_task_closing_unknown_id_fails`, `test_impact_splits_tasks_by_status`.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: traceability matrix validation and impact"`

---

### Task 8: Interview mechanics (coverage map, ledger, stopping rule, approvals)

**Files:**
- Create: `kit/scripts/interview.py`, `kit/templates/stage_report.md`, `kit/templates/challenge_report.md`, `kit/tests/test_interview.py`, `kit/tests/fixtures/coverage_min.yaml`

**Interfaces:**
- `coverage.yaml` shape: `stages: {1: {name, budget: int, topics: {T1.1: {title, owner: lens, contributes: [lens], required: bool, condition: str | null, closes_when: str, needs: [str]}}}}` where `needs` lists ledger tags that must appear on entries bearing on the topic (for example `needs: [measure, beneficiary]`).
- `ledger.yaml` shape: `sources: {S1: {kind: user|document|inference, ref: str, at: str}}`, `entries: [{id: F-1, kind: fact|assumption|decision|question, topics: [T1.1], tags: [str], text: str, source: S1, at: str, confirmed: bool (assumptions), ruling: int | null (decisions), lens: str (questions), blocking: bool (questions)}]`.
- Produces: `load_coverage(root) -> dict` (from `kit/interview/coverage.yaml`); `ledger_validate(root) -> list[str]` (IDs unique and well-formed; kind-specific fields present; `source` exists; a question has a `lens` from the five lens IDs `architect|engineering|program|project|delivery`); `topic_states(root, stage: int) -> dict[str, dict]` → per topic `{"state": closed|open|deferred|not_applicable, "entries": [ids], "missing": [tags]}` where closed means every `needs` tag is present on at least one fact or confirmed assumption or decision for that topic, deferred means a `D-` entry tagged `deferred` with `owner` and `due` fields, not_applicable means `condition` evaluates false (conditions are tag presence expressions like `has:regulated`); `stopping_rule(root, stage) -> list[str]` (reasons blocking approval: open required topics, blocking questions, unresolved challenge findings in `reports/stage-N-challenge.md` rows whose Resolution is empty, unconfirmed assumptions at stage 5); `report(root, stage) -> Path`; `challenge_init(root, stage) -> Path` (writes the challenge report skeleton with one section per lens); `approve(root, stage, artifacts: list[Path]) -> Path` (refuses while `stopping_rule` is non-empty; writes `approvals/stage-N.json` `{stage, at, artifacts: {path: sha256}, ledger_sha256}`); `next_stage(root) -> int` (first stage without approval or whose approved artifact hashes changed); `amend(root, stage) -> Path` (re-hashes and rewrites the approval, printing the changed paths); `ingest(root, file: Path) -> str` (copies to `program/sources/`, adds a `document` source, returns its ID); `replay(root, ledger: Path, artifacts_dir: Path) -> None` (installs the ledger and artifacts, then approves stages 1..6 in order, failing on the first stopping-rule reason).
- CLI: `pk interview next|report N|challenge N|approve N --artifact path...|amend N|ingest file|replay ledger dir|validate`.

- [ ] **Step 1: Write the failing tests** (against `fixtures/coverage_min.yaml`: two stages, three topics, one conditional)

```python
def test_topic_closes_when_all_needs_tags_present(self): ...
def test_assumption_counts_only_when_confirmed(self): ...
def test_conditional_topic_not_applicable_without_tag(self): ...
def test_deferred_topic_requires_owner_and_due(self): ...
def test_stopping_rule_lists_blocking_question_and_open_required(self): ...
def test_approve_refuses_then_writes_hashes(self): ...
def test_next_stage_detects_modified_artifact(self): ...
def test_amend_rehashes_and_reports_changed_paths(self): ...
def test_ledger_refuses_question_without_lens_and_entry_without_source(self): ...
def test_replay_approves_in_order_and_stops_on_first_gap(self): ...
def test_unconfirmed_assumption_blocks_stage_5(self): ...
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement `interview.py` and the two report templates**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: interview mechanics over a coverage map and ledger"`

---

### Task 9: Interview content: coverage map, lens charters, seed prompts

**Files:**
- Create: `kit/interview/coverage.yaml` (spec Appendix A.2, all 28 topics with `needs` tags derived from each closing criterion), `kit/interview/lenses/architect.md`, `engineering_manager.md`, `program_manager.md`, `project_manager.md`, `delivery_manager.md` (spec A.1, each with sections Owns, Must leave knowing, Writes, Challenges, Nominating a question, Sign-off line), `kit/interview/seeds.yaml` (spec A.3: `topics: {T1.1: {required: bool, condition, prompts: [str]}}`)
- Create: `kit/tests/test_interview_content.py`

**Interfaces:**
- Consumes `interview.load_coverage`, `miniyaml`.
- Produces the five lens IDs used everywhere: `architect`, `engineering`, `program`, `project`, `delivery`.

- [ ] **Step 1: Write the failing tests** — `test_coverage_has_28_topics_with_valid_owner_and_contributors`, `test_every_topic_has_closes_when_and_needs`, `test_seeds_cover_every_topic`, `test_each_lens_charter_has_required_sections`, `test_conditions_reference_known_tags`.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Write the three content files and five charters from Appendix A**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: coverage map, lens charters and seed prompts"`

---

### Task 10: Evidence kit core and planted-bug runner

**Files:**
- Create: `kit/scripts/evidence.py`, `kit/templates/results.schema.json`, `kit/templates/acceptance.schema.json`, `kit/tests/test_evidence.py`

**Interfaces:**
- Reference for approach only (copy nothing project-specific): ent-harness `scripts/program/evidence_kit.py`.
- Produces `class Evidence(root: Path, task_id: str, main: str = "main")`: `require_clean_tree() -> None` (`git status --porcelain` empty else `KitError` listing paths); `.head`, `.main_sha`, `.merge_base`; `hash_inputs(paths: list[str]) -> dict[str, str]` (sha256 of the blob at HEAD via `git cat-file blob HEAD:<path>`, error on untracked); `run_command(label: str, argv: list[str], expect: int = 0, parse: Callable[[str], dict] | None = None, cwd: Path | None = None) -> dict` → `{label, argv, exit, expected_exit, as_expected, derived: dict, output_sha256, output_tail}`; `planted_bugs(table: list[dict], build: list[str] | None, test: list[str]) -> list[dict]` where a row is `{id, path, find, replace, test_args: [str]}`; each row runs on a mirror (`git worktree add --detach` of HEAD in a temp dir): apply edit, run build (if given), run test; result `{id, builds: bool, killed: bool, kill_reason: "test failed" | "build failed" | "not killed", test_output_sha256}`; `killed` is true only when build passed and test exit ≠ 0; the runner always appends a row `{id: "self-test-noncompiling", builds: false, killed: false}` produced by a syntax-breaking edit of the first row's path; `stale(main: str) -> dict` (delegates to Task 11's `stale.py`); `write(path: Path) -> None` refuses unless: clean tree at start and now (`tree_unchanged_by_run`), every command `as_expected`, planted bugs present with the self-test row, `inputs` non-empty; writes `results.json` per `results.schema.json`: `{task_id, head, main, merge_base, produced_at, inputs, commands, planted_bugs, planted_bugs_all_killed, all_commands_as_expected, tree_unchanged_by_run, stale, blocked_by: [str], deviations: [str]}`.
- `acceptance.schema.json`: `{task_id, decision: accepted_development|rejected, evidence_sha256, source_commit, accepted_at, role: qa, commands: [...], planted_bugs: [...], findings: [{n, severity, location, fix}], blocked_by: [str]}`.
- CLI: `pk evidence validate <results.json>` (schema and invariants).

- [ ] **Step 1: Write the failing tests** (each builds a throwaway git repo with a tiny Python module and a unittest)

```python
def test_hash_inputs_uses_committed_blob_not_working_tree(self): ...
def test_run_command_records_exit_and_parsed_fields(self): ...
def test_planted_bug_counts_only_test_failure(self): ...  # a syntax-breaking row -> builds False, killed False
def test_self_test_row_always_present_and_not_killed(self): ...
def test_write_refuses_dirty_tree_and_names_paths(self): ...
def test_write_refuses_when_command_dirtied_tree(self): ...  # command touches a file -> tree_unchanged_by_run False -> refuse
def test_write_refuses_without_self_test_row_or_with_unexpected_command(self): ...
def test_validate_cli_accepts_written_file(self): ...
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement `evidence.py` and the two schemas**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: evidence kit with planted-bug runner"`

---

### Task 11: Stale check and hardened fetch

**Files:**
- Create: `kit/scripts/stale.py`, `kit/scripts/fetch.py`, `kit/tests/test_stale.py`, `kit/tests/test_fetch.py`

**Interfaces:**
- `stale.py`: `stale_records(root, main: str = "main") -> dict` → `{"main": sha, "stale": [{path, changed_inputs: [path]}], "checked": int}` by reading every `program/evidence/*/results.json` and comparing each `inputs[path]` to `git cat-file blob <main>:<path>`; CLI `pk stale [--main]`, exit 0 always, JSON to stdout.
- `fetch.py`: `cache_root() -> Path` (`PROGRAM_KIT_CACHE` or `~/.cache/program-kit/fetch`); `archive_name(name: str) -> str` (`KitError` unless `re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)`); `cache_path(root: Path, sha256: str, name: str) -> Path`; `cached(root, sha256, name) -> Path | None` (None when missing, when the entry or its digest directory is a symlink, or when the digest mismatches); `fetch(root, pins: list[dict], opener=None, timeout: int = 600) -> list[dict]` where a pin is `{url, sha256, size, filename}`; result rows `{filename, state: cached|fetched|refused, reason}`; downloads via `urllib.request` with the given opener into `tempfile.mkstemp(dir=digest_dir)`, checks size and sha256 before `os.replace`, unlinks the temp in `finally` unless renamed, turns `(OSError, http.client.HTTPException)` during read into `refused`, replaces a symlinked entry or digest directory by unlinking the link itself (never its target); `verify(root, pins) -> list[dict]` never constructs an opener and names `pk fetch` in its error on a miss. CLI: `pk fetch --pins file.json`, `pk fetch verify --pins file.json`.

- [ ] **Step 1: Write the failing tests**

```python
# stale
def test_stale_lists_record_whose_input_changed_on_main(self): ...
def test_stale_ignores_untouched_records(self): ...
# fetch (a local http.server fixture serving bytes; a FakeOpener for error injection)
def test_archive_name_rejects_traversal_dot_and_empty(self): ...
def test_verify_never_constructs_opener_and_names_fetch_on_miss(self): ...
def test_fetch_refuses_digest_mismatch_and_leaves_no_file(self): ...
def test_fetch_refuses_size_mismatch(self): ...
def test_fetch_is_idempotent_for_cached_entry(self): ...
def test_fetch_cleans_temp_on_mid_download_error_without_traceback(self): ...
def test_verify_treats_symlinked_entry_as_miss(self): ...
def test_fetch_replaces_symlinked_entry_without_following(self): ...  # target bytes untouched afterwards
def test_temp_file_is_created_in_digest_directory(self): ...  # wraps tempfile.mkstemp
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement both modules and register `stale` and `fetch` in `pk.py`**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: stale-evidence check and digest-cached fetch"`

---

### Task 12: Gate runner, presets, gate claims

**Files:**
- Create: `kit/scripts/gate.py`, `kit/presets/checks.go.yaml`, `kit/presets/checks.node.yaml`, `kit/presets/checks.python.yaml`, `kit/tests/test_gate.py`
- Modify: `kit/tests/test_pk.py` (remove the preset skip)

**Interfaces:**
- `checks.yaml` keys exactly as spec §9.3: `build, test, scan, compile_check, extra_gate` (lists of argv lists), `guarded_files: [str]`. Presets: go (`go build ./...`, `go vet ./...`, `go test ./...`, `gosec ./...`, `gitleaks detect --no-git`), node (`pnpm install --frozen-lockfile --offline`, `pnpm build`, `pnpm test`, `pnpm audit`, `gitleaks detect --no-git`), python (`python3 -m compileall -q .`, `python3 -m unittest discover`, `python3 -m pip check`, `gitleaks detect --no-git`); a scanner that is not installed is reported as `missing tool` and fails the gate.
- PLAN commands: `gate.py` reads `program/tasks/<ID>/PLAN.md`, section `## Exact Commands`, lines starting with `$ `, parsed with `shlex.split`; a line ending in ` # expect N` sets the expected exit.
- Produces: `load_checks(root) -> dict`; `trial_merge(root, branch: str) -> Path` (temporary worktree of `main` with `git merge --no-commit --no-ff <branch>`; `KitError` on conflict; caller removes); `handback(root, task_id: str, branch: str) -> int`: in order, trial merge then `build` and `compile_check` in the trial worktree; in the task worktree `test`, `scan`, `extra_gate`, PLAN commands, `evidence` production via a project-supplied `program/evidence/<ID>/produce.py` (run as `python3 produce.py`, which uses `evidence.Evidence`), then `stale`; stops at the first failure printing the step and its last 20 lines; on success prints exactly `OK: <branch> passes the hand-back gate at <sha>`; `merge_check(root, branch) -> int` (trial merge, build, compile_check, test; prints `OK: <branch> merges into main at <sha>`); `claims(root, gate_id: str) -> list[Path]` writes `program/claims/<GATE>/<record-name>.json` `{gate, record, environment: {dev: evidenced|blocked|not_evaluated, ...}, acceptance_sha256, generated_from_commit}` from `gates.yaml`, each record's `acceptance.json` and `environments.yaml` (an environment with `exists: false` is always `not_evaluated`); `validate_claims(root, gate_id) -> list[str]` (recompute and diff).
- CLI: `pk gate handback <ID> --branch`, `pk gate merge-check --branch`, `pk gate claims <GATE>`, `pk gate validate <GATE>`.

- [ ] **Step 1: Write the failing tests** (throwaway git repo with a Python package and `checks.yaml` from the python preset; a fake `gosec`/`gitleaks` on PATH that exits 0)

```python
def test_presets_parse_and_have_all_keys(self): ...
def test_plan_commands_parsed_with_expected_exit(self): ...
def test_handback_prints_ok_line_only_when_every_step_passes(self): ...
def test_handback_stops_at_first_failure_with_tail(self): ...
def test_handback_fails_on_merge_conflict(self): ...
def test_missing_scanner_fails_gate(self): ...
def test_claims_never_evidence_missing_environment(self): ...
def test_validate_claims_detects_drift(self): ...
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement `gate.py` and the three presets; un-skip the preset test in `test_pk.py`**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: hand-back gate, merge check, presets and gate claims"`

---

### Task 13: Worktrees, hand-off generator, change requests

**Files:**
- Create: `kit/scripts/worktree.py`, `kit/scripts/handoff.py`, `kit/scripts/change.py`, `kit/templates/change_request.md`, `kit/tests/test_worktree.py`, `kit/tests/test_handoff.py`, `kit/tests/test_change.py`

**Interfaces:**
- `worktree.py`: `new(root, task_id) -> Path` (`.worktrees/<id lowercased>`, branch `task/<ID>` from `main`; refuses if either exists); `list_all(root) -> list[dict]` (`path, branch, head, dirty: bool`). CLI `pk worktree new <ID>`, `pk worktree list`.
- `handoff.py`: `render(root) -> str` (Markdown between the markers: counts by status, in-flight tasks with `controller-state.stage` text, unmerged branches `git branch --no-merged main`, worktrees with dirty flag, queued and parked, follow-ups, open change requests, generated-at and commit); `update(root) -> bool` (rewrites only the marked block; returns changed); `check(root) -> bool` (True when current). CLI `pk handoff update|check` (`check` exits 1 when stale).
- `change.py`: `create(root, title, why, asked_by) -> str` (next `CR-nnn`, file from template with sections Change, Why, Asked by, Impact, Approval, Applied); `impact(root, cr_id, ids: list[str]) -> dict` (writes the Impact section from `trace.impact`); `approve(root, cr_id, by: str) -> None` (fills Approval with date and name); `apply(root, cr_id, note: str) -> None` (refuses without Approval; appends `register.change` note, runs `trace.validate(stage=6)`, writes Applied). CLI `pk change create|impact|approve|apply`.

- [ ] **Step 1: Write the failing tests** — `test_new_creates_worktree_and_branch_and_refuses_duplicates`; `test_render_lists_in_flight_and_dirty_worktrees`, `test_update_touches_only_marked_block`, `test_check_exits_1_when_stale`; `test_create_numbers_sequentially`, `test_apply_refuses_without_approval`, `test_impact_written_from_trace`.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement the three modules and register them in `pk.py`**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: worktrees, hand-off generator and change requests"`

---

### Task 14: Playbooks, briefs and rules

**Files:**
- Create: `kit/playbooks/00_START.md`, `01_discover.md`, `02_define_product.md`, `03_specify.md`, `04_architect.md`, `05_freeze_review.md`, `06_register.md`, `07_task_specs.md`, `08_execute.md`, `09_handoff.md`, `10_change.md`
- Create: `kit/briefs/controller.md`, `implementer.md`, `qa.md`, `spec_qa.md`
- Create: `kit/rules/HARD_RULES.md`, `EVIDENCE_HONESTY.md`, `THROUGHPUT.md`, `RULINGS_SEED.md`
- Create: `kit/tests/test_content.py`

**Interfaces:**
- Reference for content only, rewritten without project names: ent-harness `docs/program/controller/dispatch-brief.md`, `qa-brief.md`, `docs/program/HANDOFF.md` §1.1, §4, §5, §6, `docs/program/SUCCESSOR_PROMPT.md`.
- Each playbook has the sections: Purpose, Inputs, Lenses active (stages 1–6), Steps (numbered, each naming the `pk` command it runs), Outputs, Done when, Next playbook. `00_START.md` tells the agent to run `pk interview next` and `pk handoff check` and open the playbook named by the result.
- `08_execute.md` is spec §5 step by step with the exact commands; `09_handoff.md` contains the successor prompt (spec §7) including the fetch step and the main-suite check; `10_change.md` is spec §6.
- Briefs state role, what to read first (hand-off rule sections and cited rulings only), the report shape (fixed headings, under 25 lines), the four NEEDS_CONTEXT cases, and the rules the role must never break.
- `RULINGS_SEED.md`: rulings `K1..Kn` rewritten from ent-harness R137, R139, R140 (1–5), R141 (a–e), R142, R143 (a–d), R144, R145, R146, R147, R148, R149, R151, R172, R174, R175 with the project names removed; sections 8–10 of the spec are their digest.

- [ ] **Step 1: Write the failing tests** — `test_every_playbook_has_required_sections_and_next_link`, `test_start_playbook_names_every_other_playbook`, `test_briefs_name_the_four_needs_context_cases`, `test_rulings_seed_numbered_k1_to_kn_without_gaps`, `test_every_pk_command_mentioned_exists` (parse `pk <cmd>` mentions against `pk.COMMANDS`).
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Write the content files**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "docs: playbooks, role briefs and rules"`

---

### Task 15: Remaining templates and `install.sh`

**Files:**
- Create: `kit/templates/discovery.md`, `product.md`, `SPEC.md`, `ARCHITECTURE.md`, `adr.md`, `roadmap.md`, `task_SPEC.md`, `task_PLAN.md` (with the `## Exact Commands` section), `freeze_review.md`, `signoff.md`
- Create: `install.sh`, `kit/MANIFEST.sha256` (generated by `kit/scripts/manifest.py`), `kit/scripts/manifest.py`, `kit/tests/test_install.py`, `kit/tests/test_templates.py`

**Interfaces:**
- `manifest.py`: `write(kit_dir) -> Path` (sha256 of every file under `kit/` except the manifest), `check(kit_dir) -> list[str]` (modified or missing paths). CLI `pk manifest write|check`.
- `install.sh <target-repo> [--preset go|node|python] [--upgrade]`: copies `kit/` into `<target>/kit/`; without `--upgrade` refuses if `<target>/kit` exists; with `--upgrade` refuses if `manifest check` reports modifications, otherwise replaces `kit/` wholesale and never touches `program/`; then runs `pk init --preset` when `program/` is absent; prints `installed program-kit <VERSION> into <target>`.
- Templates: task_SPEC headings exactly: Summary, Scope In, Scope Out, Dependencies, Consumed Contracts, Security And Tenancy, Failure Modes And Recovery, Observability, Module Placement, Resolved Questions, Closes; task_PLAN headings: Status And PR Boundary, Work Breakdown, Exact Commands, Definition Of Done, Gate Evidence, Rollout And Rollback, Risks.

- [ ] **Step 1: Write the failing tests** — `test_templates_have_required_headings`; `test_install_fresh_copies_kit_and_inits_program` (bash in a temp git repo, each preset), `test_install_refuses_existing_kit_without_upgrade`, `test_upgrade_refuses_modified_kit`, `test_upgrade_replaces_kit_and_keeps_program`, `test_doctor_ok_after_install`.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement `manifest.py`, `install.sh` and the templates**
- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: installer with manifest-guarded upgrade; remaining templates"`

---

### Task 16: `examples/tiny-service` and the end-to-end test

**Files:**
- Create: `examples/tiny-service/README.md`, `BRIEF.md`, `ledger.yaml` (recorded; closes every required topic of stages 1–6 for a tiny HTTP "health and echo" service), `artifacts/` (discovery.md, environments.yaml, CLAIM_BOUNDARIES.md, product.md, SPEC.md, ARCHITECTURE.md, adr/0001-stdlib-http.md, gates.yaml, traceability.yaml, freeze/review.md, freeze/signoff.md, roadmap.md, register.yaml with one phase, one milestone, one step, two tasks), `tasks/P1-M01-S01-T01/SPEC.md` and `PLAN.md`, `src/` (the service and its unittest), `checks.yaml` (python preset)
- Create: `kit/tests/test_end_to_end.py`

**Interfaces:**
- Consumes every `pk` command. The test: creates a temp git repo with one commit; runs `install.sh` with `--preset python`; copies the example `program/` content; `pk interview replay`; `pk trace validate --stage 6`; `pk register validate`; `pk briefs generate` then `check`; `pk status set P1-M01-S01-T01 in_progress --role controller`; `pk worktree new P1-M01-S01-T01`; copies `src/` into the worktree and commits; `pk gate handback P1-M01-S01-T01 --branch task/P1-M01-S01-T01` must print the OK line and write `results.json`; writes an `acceptance.json` bound to it; `pk status set … review --role implementer` then `done --role qa --evidence …`; `pk gate merge-check --branch …` prints its OK line; `pk handoff update` then `check` exits 0; `pk gate claims G-P1` then `validate`.

- [ ] **Step 1: Write the failing end-to-end test** (`test_tiny_service_runs_the_whole_loop`, plus `test_example_ledger_validates_and_closes_all_required_topics`)
- [ ] **Step 2: Run it to verify it fails**
- [ ] **Step 3: Write the example content until the test passes** (fix kit scripts where the example exposes a gap; each fix gets its own unit test in the owning task's file)
- [ ] **Step 4: Run the whole suite** — `python3 -m unittest discover -s kit/tests -v` — Expected: all PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: tiny-service example with end-to-end test"`

---

### Task 17: README, docs, neutrality check, release tag

**Files:**
- Modify: `README.md` (what it is, five-minute start for both adoption paths, the stage flow in ten lines, the execution loop in ten lines, where rules live, how to upgrade, licence)
- Create: `docs/DESIGN.md` (the spec with the ent-harness references replaced by "the originating program"), `docs/FAQ.md` (why strict only; why no model names; why YAML subset; how to add a preset; how to take over a program)
- Create: `kit/tests/test_neutrality.py`

**Interfaces:**
- `test_neutrality.py`: walks `kit/`, `examples/`, `docs/`, `README.md` and fails on any forbidden token from Global Constraints (case-sensitive whole words), and on any `http://` or `https://` URL outside `docs/FAQ.md`.

- [ ] **Step 1: Write the failing neutrality test and run it** — Expected: FAIL if any token slipped in; fix the content.
- [ ] **Step 2: Write README, DESIGN and FAQ**
- [ ] **Step 3: Run the whole suite and `pk doctor` on the example** — Expected: all PASS, `doctor: ok`.
- [ ] **Step 4: Commit and tag** — `git commit -m "docs: README, design and FAQ" && git tag v0.1.0` (no push).
