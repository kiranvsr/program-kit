# program-kit: a governed delivery framework for agent-run software programs

Date: 2026-10-06. Status: design approved in conversation (parts 1–4), critical review folded in; spec for user review.

## 1. Purpose

`program-kit` is an open-source, provider-neutral template that turns a few lines of intent into a fully specified, governed delivery program, and then runs that program with AI agents at a quality bar that survives hand-offs between sessions, models and providers.

It is extracted from the ent-harness delivery program (152 tasks, three controllers from three providers, 174 rulings) with every project-specific element removed. The process shipped here is the strict process as run there: every task, however small, goes through the same lifecycle.

A user starts by writing a short brief. The kit then explores the requirements with them through a fixed set of stages. Five role lenses (architect, engineering manager, program manager, project manager, delivery manager) drive the questions dynamically from the user's answers, over a coverage map that fixes what must be known before a stage can close. This continues until the product is defined end to end, specified with identified requirements, architected with recorded decisions, and frozen by an independent review. Only then is the task register built. The kit's execution loop then builds the tasks with implementers, independent QA and commit-bound evidence, merging one branch at a time, and keeps a hand-off document current so anyone can take over.

**Who it is for.** A single developer or small team that wants to run a multi-week build with agents and needs the result to be trustworthy: no fabricated test results, no silently narrowed scope, nothing claimed about environments that do not exist.

**Non-goals.** It is not an agent runtime, a CI system or a project-management tool. It does not host anything. It does not choose models. A "lite" process is out of scope; the user chose strict only.

## 2. Delivery shape

- **Repository.** `program-kit`, MIT licence, a GitHub template repository at `/Users/kiran/Projects/program-kit` during development. Nothing is pushed until the user says so.
- **Two ways to adopt.** Use it as a GitHub template for a new project, or run `install.sh` to copy the `kit/` directory into an existing repository. `kit/VERSION` records the installed kit version; `install.sh --upgrade` replaces `kit/` wholesale and never touches the project's own `program/` directory.
- **Provider neutrality.** No file names a model, vendor or agent CLI. Playbooks are plain Markdown an agent reads. Scripts are Python 3 standard library only, so they run anywhere Python runs.
- **Project-declared checks.** The project declares its build, test and scan commands in `program/checks.yaml`. The kit ships presets for Go, Node/TypeScript and Python that the installer offers; a project can start from one and edit it.

## 3. Repository layout

```
program-kit/
  README.md                 what it is, 5-minute start, the two adoption paths
  LICENSE                   MIT
  install.sh                copies kit/ into a target repo; --upgrade; --preset go|node|python
  kit/
    VERSION
    playbooks/
      00_START.md           entry point: which playbook to open next, from the state files
      01_discover.md        stage 1
      02_define_product.md  stage 2
      03_specify.md         stage 3
      04_architect.md       stage 4
      05_freeze_review.md   stage 5
      06_register.md        stage 6: roadmap and task register
      07_task_specs.md      stage 7, just in time per task
      08_execute.md         the controller loop (dispatch, hand-back gate, QA, merge)
      09_handoff.md         hand-off, takeover, model tiers
      10_change.md          change requests after approval
    interview/
      coverage.yaml         the coverage map: per stage, the topics that must be closed and what closes each
      lenses/               role charters: architect, engineering_manager, program_manager, project_manager, delivery_manager
      seeds.yaml            seed prompts per topic: a floor for weaker models, never a script
      roles.yaml            the agent role catalog used by the register
    briefs/
      controller.md, implementer.md, qa.md, spec_qa.md
    rules/
      HARD_RULES.md, EVIDENCE_HONESTY.md, THROUGHPUT.md, RULINGS_SEED.md
    templates/
      BRIEF.md
      discovery.md, product.md, SPEC.md, ARCHITECTURE.md, adr.md
      environments.yaml, gates.yaml, traceability.yaml
      register.yaml
      task_brief.md, task_SPEC.md, task_PLAN.md
      results.schema.json, acceptance.schema.json, status.schema.json
      checks.yaml, CLAIM_BOUNDARIES.md
      ledger.yaml           the requirements ledger: facts, assumptions, decisions, open questions
      stage_report.md       the completion report every stage ends with
      challenge_report.md   the per-lens challenge round report
    presets/
      checks.go.yaml, checks.node.yaml, checks.python.yaml
    scripts/
      pk.py                 single CLI entry: `python3 kit/scripts/pk.py <command>`
      interview.py          stage state machine, ledger and coverage validation, reports, approvals, resume, replay
      trace.py              traceability matrix: builds and validates journeys→requirements→components→gates→tasks
      register.py           validates register.yaml; assigns IDs; dependency closure; cycle check; granularity rules
      briefs.py             generates program/briefs/** from the register (immutable once approved)
      status.py             the only writer of program/status/*.json
      evidence.py           the evidence kit (section 9.2)
      gate.py               runs checks.yaml and the hand-back gate; gate claims per gates.yaml
      fetch.py              fetches and caches network inputs by digest; the only script that downloads
      stale.py              stale-evidence check of records against main
      rulings.py            appends a numbered ruling; validates the log
      handoff.py            regenerates HANDOFF.md live section; --check
      worktree.py           creates/lists task worktrees
      change.py             change requests: impact analysis through the traceability matrix
  kit/tests/                unit tests for every script (stdlib unittest)
  examples/
    tiny-service/           brief, recorded ledger for replay, full stage outputs, one task executed with evidence
  docs/
    DESIGN.md               this document, trimmed
    FAQ.md
```

A project that installs the kit gets `kit/` (owned by the kit, replaced on upgrade) and `program/` (owned by the project):

```
program/
  BRIEF.md                  stage 0
  discovery.md, environments.yaml, CLAIM_BOUNDARIES.md      stage 1
  product.md                                                stage 2
  SPEC.md                                                   stage 3
  ARCHITECTURE.md, adr/NNNN-*.md, gates.yaml                stage 4
  traceability.yaml                                         built from stage 2 on, validated at 5 and 6
  freeze/review.md, freeze/signoff.md                       stage 5
  roadmap.md, register.yaml                                 stage 6
  ledger.yaml               the requirements ledger, appended throughout stages 1 to 6
  reports/stage-N.md        completion report per stage
  reports/stage-N-challenge.md   what each lens challenged before approval and how it was resolved
  approvals/stage-N.json    date, approved artifact paths and hashes, open-topic count
  briefs/                   generated phase/milestone/step/task briefs
  tasks/<ID>/SPEC.md, PLAN.md
  status/<ID>.json
  evidence/<ID>/results.json, acceptance.json
  claims/<GATE>/*.json      per-gate claim docs regenerated after merges
  rulings.md                append-only, numbered R1..
  changes/CR-NNN.md         change requests after approval
  checks.yaml
  HANDOFF.md, controller-state.json, progress.md
```

## 4. Stage flow: from a few lines to a frozen spec and a task register

### 4.1 How requirements are explored

The interview is not a scripted questionnaire. Questions are generated from the user's answers by five role lenses, and a coverage map decides when a stage is finished. The lenses supply relevance and depth; the map supplies a definition of done that is the same for every project, model and provider.

- **Coverage map.** `kit/interview/coverage.yaml` lists, per stage, the topics that must be closed, the lens that owns each, the lenses that contribute, and the closing criterion: what must be known, in what form, for the topic to count as closed. It contains no question wording. A topic may be conditional on an earlier answer. Appendix A.2 is its content.
- **Role lenses.** Each lens has a charter in `kit/interview/lenses/`: what it owns, what it must leave each stage knowing, which artifact sections it writes, and what it challenges. The three management lenses have separate ground: the program manager owns what and why, the project manager owns when and in what order, the delivery manager owns how it reaches users and stays running. Appendix A.1 holds the charters.
- **Requirements ledger.** `program/ledger.yaml` is the shared memory of the interview. Every entry has an ID, the topics it bears on, its source (a user message with its date, an ingested document, or an inference) and one of four kinds: fact (`F-`), assumption (`A-`, with the default taken and whether the user has confirmed it), decision (`D-`, made by the user, or by the agent after "decide" with its ruling number), open question (`Q-`, with the lens that raised it and whether it blocks the stage). Artifacts are written from the ledger, so nothing in a spec lacks a recorded origin.
- **The loop.** One interviewer speaks to the user. After each answer it updates the ledger, then takes each lens in turn over the open topics of the current stage. Each lens nominates at most one next question, with the reason and what a different answer would change. The interviewer merges duplicates and asks the nomination with the highest impact and uncertainty. By default this is one agent adopting the lenses in turn, not five agents per question.
- **Assumptions before questions.** Where a sensible default exists, a lens records an assumption instead of asking. Assumptions are shown to the user in batches to correct, which is faster than asking each one. An unconfirmed assumption stays marked in every artifact that depends on it.
- **Seed prompts.** `kit/interview/seeds.yaml` holds example questions per topic (Appendix A.3). A lens may use, adapt or ignore them. They exist so a weaker model still explores every topic to a reasonable depth.
- **Challenge round.** Before a stage is approved, each lens reviews the draft artifact and the ledger against its charter in its own agent session, independent of the interviewer, and returns gaps as questions or findings. Lens sessions may run in parallel; a single-model provider runs them one after another. The interviewer resolves each gap with the user or records it as deferred. `program/reports/stage-N-challenge.md` lists every gap and its resolution.
- **Stopping rule.** A stage can be approved when all of these hold: every applicable required topic meets its closing criterion or is deferred with an owner and a date; every lens records no blocking open question; the challenge round has no unresolved finding; and the best remaining nominations would not change scope, architecture, plan or a gate. Each stage also has a question budget shown to the user; on reaching it the interviewer asks whether to continue or to take defaults for what remains.
- **Coverage report.** `interview.py report` writes `program/reports/stage-N.md`: every topic with its state (closed, with the ledger entries that close it; deferred, with owner and date; not applicable, with the lens's reason), the assumptions still unconfirmed, and each lens's sign-off line.
- **Write-back.** Before writing a stage's artifact, the interviewer writes back its understanding in a short note the user corrects. The artifact is produced from the corrected note and the ledger.
- **Approval.** `interview.py approve N` checks the stopping rule mechanically (topic states, blocking questions, challenge findings) and writes `program/approvals/stage-N.json` with the date and each artifact path and sha256. `interview.py next` reads the approvals and the artifact hashes to name the next stage, so a session can stop anywhere and the next one resumes at the first unapproved or modified stage, with the ledger intact.
- **Amend.** `interview.py amend N` shows the diff since the approval and re-approves in one step, for corrections that do not change meaning. A change of meaning after stage 5 goes through a change request (section 6).
- **Pacing.** Closed questions on one topic may be batched, three to five per message, each with the default the lens would take. Open questions go one per message. Every message shows progress against the coverage map ("14 of 22 topics closed for this stage"). The user may say "decide" to delegate a question, "defer" to park a topic with an owner, or "take defaults" to accept the lenses' assumptions for all remaining optional topics.
- **Pre-answered input.** The user may point at an existing document. `interview.py ingest <file>` has the agent map it onto the coverage map and write ledger entries with the document as their source; the lenses then explore only what is still open.
- **Replay and testing.** The scripts validate structure, never wording: ledger entries, topic states, the stopping rule, approvals. `interview.py --ledger recorded.yaml` drives every stage from a recorded ledger instead of a user, for the example and for tests.
- **Cost.** This costs more tokens per stage than a scripted questionnaire, and two runs on the same brief will differ in wording. The coverage map keeps them equal in what they cover.

### 4.2 The stages

**Stage 0, brief.** `program/BRIEF.md`: anything from three lines to a full document. No format is enforced.

**Stage 1, discover.** Purpose, who it is for, success criteria, constraints, what is explicitly out, what exists already, team and timeline, and the environment matrix: `environments.yaml` lists every environment (local, CI, staging, production, air-gapped, hosted services) with `exists: true|false` today. `CLAIM_BOUNDARIES.md` is generated from it: anything that needs an environment that does not exist is unclaimable from the start, and `gate.py` enforces that at evidence time. Artifacts: `discovery.md`, `environments.yaml`, `CLAIM_BOUNDARIES.md`.

**Stage 2, define the product.** Personas with IDs (`U1..`), user journeys with IDs (`J1..`) each with entry, steps, exit and failure paths, and the product surface checklist: onboarding, authentication and authorization, administration, billing or entitlement if any, operations (deploy, backup and restore, monitoring, alerting), data lifecycle, documentation, support. Every checklist item is in scope, out of scope with reason, or deferred to a named later release. The first release is defined as a set of journeys; later releases are named. Artifact: `product.md`; `traceability.yaml` starts with the journeys.

**Stage 3, specify.** Requirements with IDs (`REQ-001..`), each traced to at least one journey or to a cross-cutting concern; non-functional requirements with IDs (`NFR-..`) for scale, availability, latency, security, compliance, tenancy, data retention and residency, each with a measurable target or an explicit "no target yet, owner"; domain objects and interfaces; security and tenancy boundaries; failure modes and recovery; observability; open questions closed by answer or ruling. Artifact: `SPEC.md`; `traceability.yaml` gains requirements.

**Stage 4, architect.** The agent proposes two or three candidate architectures with trade-offs and a recommendation; the user picks. Then: components with IDs (`C1..`) and boundaries, module placement, data flow, deployment shape per environment, cross-cutting concerns (authn/z, tenancy, observability, data lifecycle, configuration, secrets), technology choices each recorded as an ADR in `program/adr/` (context, options, decision, consequences), and `gates.yaml`: the named gates (`G-..`) that will prove quality claims, each with the evidence it requires and the environment it needs. Artifacts: `ARCHITECTURE.md`, `adr/*`, `gates.yaml`; `traceability.yaml` gains components and gates.

**Stage 5, freeze review.** An independent spec-QA agent, never the author, reviews stages 1 to 4 together against `kit/briefs/spec_qa.md`: placeholders, contradictions, ambiguity, every journey covered by requirements, every requirement owned by a component, every quality claim owned by a gate, every non-functional requirement with a target or an owner, every environment claim consistent with `environments.yaml`, surface checklist complete. It returns a finding-to-fix table in `program/freeze/review.md`; the author fixes, the reviewer re-checks only the findings. `trace.py validate` must pass. Every assumption in the ledger must by now be confirmed by the user or converted to a decision; none survives the freeze unconfirmed. Each lens records its sign-off, and the user signs `program/freeze/signoff.md`. Approval 5 freezes the spec; from here on changes are change requests.

**Stage 6, roadmap and register.** `roadmap.md` shapes phases by capability: the first phase is a walking skeleton (one thin end-to-end journey through every component, deployed to every existing environment), then vertical slices by journey, with gates attached to phase ends. `register.yaml` then holds phases → milestones → steps → tasks with stable IDs (`P1-M02-S01-T03`), each task with a one-paragraph text, agent role from `roles.yaml`, size (S, M, L with the kit's definitions), environments, dependencies, target gate, and the requirement and journey IDs it closes. `register.py` validates: unique IDs, dependencies exist, no cycles, every requirement and journey of the first release mapped to at least one task, every task mapped to a gate or explicitly gate-less, granularity rules (one task is one PR, one role, size L split into steps), fan-in limit per task, and no task that needs an environment marked non-existent unless it is explicitly `blocked_by` that environment. `briefs.py` generates the brief tree. Approval 6 freezes the register; later changes go through `pk.py register change`, which records the change and regenerates briefs.

**Stage 7, task specs, just in time.** A task gets its `SPEC.md` and `PLAN.md` when it reaches the front of the queue, not before, so specs reflect what was learned from earlier tasks. The SPEC fixes scope in/out, dependencies, consumed contracts from the architecture, security and tenancy, failure modes, observability, module placement and resolved questions, and lists the requirement IDs it closes. The PLAN fixes the PR boundary, work breakdown, exact test commands, definition of done, gate evidence and rollback. A spec-QA agent, never the author, reviews both against the register text, the architecture and the traceability matrix with a finding-to-fix table; the task is dispatched only on acceptance.

### 4.3 Status state machine

`status.py` allows only these transitions, by role:
- `not_started → in_progress` (controller, at dispatch)
- `in_progress → review` (implementer, after the hand-back gate prints its OK line)
- `review → in_progress` (QA, with findings)
- `review → done` (QA only, with an acceptance file bound to the current evidence commit)
- `not_started|in_progress → blocked` (controller, with `blocked_by`) and `blocked → not_started|in_progress`
- `not_started → parked` (controller, with the user decision it waits on) and `parked → not_started`
`done` is terminal; a later change request creates a new task.

## 5. Execution loop (playbook 08)

**Roles.** Three roles, each a separate agent session:
- The **controller** picks the next task, dispatches it, answers questions with recorded rulings, merges, and keeps the state current. It never writes product code.
- An **implementer** owns one task in its own worktree and branch, builds test-first against SPEC and PLAN.
- **QA** never wrote the code. It decides acceptance with evidence. Only QA, through `status.py`, can mark a task done.

**Per task.**
1. **Dispatch.** `worktree.py new <ID>` creates the worktree and branch. The controller hands the implementer its brief, the SPEC, the PLAN, the rules and the rulings the SPEC cites.
2. **Build.** Test-first. The implementer decides anything with precedent itself and records the decision. It asks the controller only for a new dependency, a security boundary without precedent, a change to user scope, or conflicting rulings. The controller answers with a numbered ruling.
3. **Hand-back gate.** `gate.py handback <ID>` runs, in order: a trial merge of the branch into main in a temporary worktree followed by the project's build and compile checks; the project's test, security and secret-scan commands from `checks.yaml`; the task's PLAN commands; `evidence.py produce`; `stale.py`. It prints a final `OK: <branch> passes the hand-back gate at <sha>` line only if every step passed. Exit code alone is not a pass; the line is. Failing the gate returns the work to the implementer without a QA round.
4. **Evidence.** `program/evidence/<ID>/results.json`, produced only from a clean tree, binds the work to one commit: head and main SHAs, the sha256 of every consumed input (source, tests, fixtures, dependency closure, config, lock files, tool helpers), every command with its literal text, real exit code, parsed derived fields and `as_expected`, and the planted-bug table.
5. **QA.** An independent agent re-runs the gate, tries to defeat each new guard with its own planted bugs, checks scope in/out against the SPEC, the closed requirement IDs against the traceability matrix, and claims against `CLAIM_BOUNDARIES.md`, and either writes `acceptance.json` (bound to the evidence commit, with its own commands and bugs) and sets status done, or rejects with numbered findings, each with severity, location and the exact fix expected.
6. **Fix rounds.** Fixes go back to the same implementer. The re-check covers only the findings and may run on a Tier 2 model, which must escalate anything outside its checklist.
7. **Merge.** The controller re-runs `gate.py merge-check <branch>` from main, then `git merge --no-ff`. Accepted branches merge one at a time, right after acceptance. A branch whose hashed inputs were untouched by later main moves and whose merge-tree is clean does not re-produce its evidence. Each merge appends the stale-evidence list for later batched re-binding; stale evidence is never chased one merge at a time.
8. **Bookkeeping.** After every merge or state change: update `controller-state.json`, append to `progress.md`, run `gate.py claims` for the gates the merge touched, run `handoff.py`, commit `program/`.

**Gate claims.** `gates.yaml` names, per gate, the evidence records it aggregates. `gate.py claims <GATE>` regenerates `program/claims/<GATE>/*.json`, one per record, each stating per environment whether the claim is evidenced, blocked or not evaluated, computed only from acceptance files and `environments.yaml`. `gate.py validate <GATE>` fails on any drift between the claim docs and a recomputation. Nothing ever upgrades a development claim to a non-existent environment.

**Planted bugs.** A planted bug counts as caught only when a test fails in code that builds (or, for non-compiled artifacts, that loads). The planted-bug runner records `builds` and `kill_reason` per row and always includes one deliberately non-building row recorded as not caught, so a runner that counts build failures as catches is visible.

**Pre-existing failures.** A PLAN command that fails identically on clean main is recorded as a named environmental deviation: real exit, one reproduction on clean main with its log hash, proof the task's inputs to it are unchanged, the failure message asserted so a silent pass or a different failure flips the row, nothing claimed from it, and an owner follow-up. Never a pass.

## 6. Change requests after the freeze (playbook 10)

A change of meaning to anything approved at stage 5 or later is a change request `program/changes/CR-NNN.md`: what changes, why, who asked. `change.py impact CR-NNN` lists through the traceability matrix every requirement, component, gate and task touched, split into done tasks (whose acceptance would no longer cover the new meaning), in-flight tasks and not-started tasks. The user approves the CR with that impact in view. Then: artifacts are amended with the CR number in their change log, `trace.py validate` must pass again, affected done tasks get a new follow-up task in the register, affected in-flight tasks get a ruling delivered to their implementer, and `pk.py register change` records the register edit and regenerates briefs. A CR never edits a done task's evidence or acceptance.

## 7. Hand-off and takeover (playbook 09)

`program/HANDOFF.md` is the single document a successor needs. It has a maintained part and a generated part:
- maintained: how the program works, the hard rules, evidence rules, throughput rules, the ruling digest, environment setup, known gotchas, ordered next steps;
- generated by `handoff.py`: counts, in-flight tasks with their stage text, unmerged branches and worktrees, queued and parked tasks, open follow-ups, open change requests.

`handoff.py --check` exits 1 when the generated part is stale; the controller runs it before every commit of `program/`. A process ruling changes the maintained part in the same commit.

`kit/playbooks/09_handoff.md` includes the successor prompt: read HANDOFF top to bottom, read the role briefs, verify the state (git status, unmerged branches, worktree cleanliness, void partial evidence by exact path, leftover processes and containers, disk, and that the repo-wide suite passes on main), then resume at HANDOFF's next steps. A successor never restarts an accepted task and never resumes an agent whose task has since merged.

**Model tiers.** Roles, never names. Tier 1 is the strongest model available: controller, first-pass implementation and QA, the interviewer and the lens challenge rounds, stage artifacts and specs, freeze review, any re-check that escalates. Tier 2 is a cheaper model, used only for scoped re-checks of exact fixes, re-bind runs and mechanical producer runs with a checklist; it reports ESCALATE instead of judging anything outside the checklist. Never Tier 2 for first-pass work, security rulings, or accepting with an open important finding. A provider with one model uses it for everything. Nothing in the kit names a model; each controller records its mapping in HANDOFF when it takes over. The commit attribution line follows the controller's session and is not a model choice.

## 8. Hard rules (kit/rules/HARD_RULES.md)

- Never fabricate evidence, test results, CI results, gate passes or human approvals. Environments marked non-existent in `environments.yaml` are never claimed; work that needs them stays `blocked_by`.
- Development acceptance is not production readiness.
- Only QA sets done, only through `status.py`. The register changes only through `pk.py register change`. Briefs are immutable once approved. Frozen artifacts change only through a change request.
- Never rewrite history: no rebase, reset or force-push; merge main into branches; never a bare `git stash`.
- Never commit secrets, private keys or licence-restricted artifacts the project lists.
- Never push, deploy or apply infrastructure without the user's explicit instruction.
- Every destructive command uses literal paths; never prune shared resources; clean up only what you created, by exact name.
- Permission denials are surfaced to the user, never routed around and never delegated to another agent.
- Guarded files the project lists (build files, CI definitions, dependency manifests) change only with the user's approval.
- An acceptance lives where the code reads it; a side file that leaves the read fields unaccepted is not an acceptance.

## 9. Evidence honesty (kit/rules/EVIDENCE_HONESTY.md) and the evidence kit

### 9.1 Rules
1. PLAN commands run literally, as written.
2. Derived fields are parsed from real output and asserted; never "unparseable" while marked as expected.
3. Every new guard has a cheapest-defeat planted bug caught by the gate's own test.
4. A planted bug counts only on a test failure, never a build or setup failure; planted bugs must build.
5. Every new tenancy or access policy gets a cross-boundary test with a positive eligible-row control and a wide-open planted policy.
6. Consumed-input hashes are complete: unchanged source, tests, fixtures, dependency closure, generated-file sources, migrations, tool helpers, config and lock files.
7. Evidence is produced only from a clean tree and binds to one commit; hashes are never hand-edited.
8. Partial evidence left by an interrupted run is voided by exact path before resuming.
9. Refreshing an older record re-runs every command it holds and replaces each with the real result; entries are never dropped or kept stale, and a claim whose output changed is rewritten to what the command now proves.
10. Validators and gate steps never download inline. Anything fetched from the network (toolchains, pinned archives, fixtures) is cached by content digest under a durable per-host cache, fetched once per host by a separate `fetch` step that runs in the background at session setup with a long timeout, and verified locally against the recorded digest. A verify step that finds its input missing from the cache fails with the fetch command to run, never by downloading. Receipts stay digest-bound, so caching changes nothing about what is proven. The fetch step validates every cached name against a strict pattern, writes through an exclusively created temporary file in the cache directory, removes it on any failure, checks size and digest before the rename, and never follows a symlink for a cache entry or its parent; each of those guards has its own test and planted bug.

### 9.2 evidence.py
Standard-library Python. `Evidence(root)` checks the tree is clean, records head, merge-base and main, and hashes inputs through `git cat-file` so the hash is of the committed blob. `run_command(argv, expect, parse=...)` records literal text, exit code, parsed fields and `as_expected`. `planted_bugs(table)` applies each edit to a mirror copy, builds, runs the named test, records `builds` and `kill_reason`, restores, clears caches, and appends the non-building self-test row. `stale(main)` lists records whose hashed inputs changed on main. `write()` refuses to write unless the clean-tree guard, the self-test row and `all_commands_as_expected` are present. `checks.yaml` supplies the project's build, test and scan commands; the kit never assumes a language.

### 9.3 checks.yaml
```yaml
build:  [["go", "build", "./..."]]
test:   [["go", "test", "./..."]]
scan:   [["gosec", "./..."], ["gitleaks", "detect", "--no-git"]]
compile_check: [["go", "vet", "./..."]]
extra_gate: []            # project-specific gate commands, run in order
guarded_files: ["go.mod", "Makefile", ".github/workflows/ci.yml"]
```
Presets fill these for Go, Node/TypeScript and Python. Commands are argv lists; `gate.py` runs them without a shell. Unclaimable environments come from `environments.yaml`, not from here.

## 10. Throughput rules (kit/rules/THROUGHPUT.md)

- Merge queue: QA starts only once the branch's evidence reflects current main; merge immediately after acceptance.
- Hand-back gate: no QA round until the implementer passed every mechanical check.
- Decide, don't ask: anything with precedent is decided and recorded; questions are reserved for the four cases in section 5.
- Fix rounds return a finding-to-fix table; the re-check covers only those findings.
- At most three implementers and four agents at once; each agent has its own scratch folder.
- No polling: long commands run in the background and the agent waits for completion; never `tail -f`.
- Short reports, in a fixed shape the briefs define.
- After an interruption: void partial evidence by exact path, resume at the interrupted step, never restart a task.
- When two rejections share a cause, the cause becomes a gate check.
- Read the hand-off's rule sections and the rulings a SPEC cites; not the whole ruling log.
- A successor verifies that main's repo-wide suite passes before dispatching anything.
- Session setup runs the kit's `fetch` step in the background once per host so no later gate step waits on a download.

## 11. Rulings

`program/rulings.md` is append-only. `rulings.py add` writes the next number, the date, the role that decided, the question settled, the decision and the precedent it generalises. A ruling that changes the process also changes the playbook or rule file in the same commit. A ruling never contradicts an earlier one silently; it supersedes it by name. `kit/rules/RULINGS_SEED.md` holds the starting set, numbered K1..Kn, rewritten from ent-harness without project names: the rules in sections 8 to 10 are their digest.

## 12. Error handling

- Every script exits non-zero with a one-line reason on the first invalid input; none writes partial output.
- `interview.py approve` refuses while a required topic is open, a lens holds a blocking question, a challenge finding is unresolved, or a ledger entry is malformed or has no source; `interview.py next` refuses to open a stage whose predecessor has no approval, or whose approved artifact hash no longer matches (it names the amend or change-request path instead).
- `trace.py validate` fails on any journey without a requirement, requirement without a component or task, gate without a record, or task closing an unknown ID.
- `register.py` fails on duplicate IDs, missing dependencies, cycles, granularity violations and unclaimable environments without `blocked_by`.
- `status.py` refuses a transition the state machine does not allow.
- `gate.py` stops at the first failing step, prints the step and its last 20 lines, and never prints the OK line.
- `evidence.py write` refuses on a dirty tree, a missing self-test row, or a command not as expected.
- `fetch.py` refuses to store a file whose digest does not match the pin; verify steps refuse to download and name the fetch command instead.
- `change.py` refuses to apply a CR without the user's approval record.
- `install.sh --upgrade` refuses if `kit/` has local modifications.

## 13. Testing the kit itself

- `kit/tests/` covers every script with stdlib unittest: coverage-map loading and conditional topics, ledger validation (kinds, sources, topic links), the stopping rule, challenge-report resolution, coverage reports, approval and amend, resume at the right stage, replay from a recorded ledger, ingest marking, traceability validation, register validation (duplicate IDs, cycles, unmapped requirements, granularity), ID assignment stability, brief generation determinism, status transitions, evidence writing guards, the planted-bug runner (including that a build failure is not a catch), stale detection, gate claims and drift, ruling numbering, hand-off staleness, change-request impact.
- `examples/tiny-service/` is executed end to end by a test: brief, a recorded ledger replayed through all seven stages including a scripted freeze review, a validated register, one task through hand-back gate, QA and merge, in a temporary git repository, using the Python preset.
- `install.sh` is tested by installing into a temporary repository with each preset and running `pk.py doctor`.

## 14. Out of scope for the first version

A web UI or dashboard; a lite process; hosted anything; model selection; language-specific project scaffolding beyond `checks.yaml` presets; migration of an existing non-kit program into the kit's layout; multi-repository programs.

## 15. Build plan summary

Built with Fable 5.1 as controller and implementer, following the kit's own loop where practical. Order: scripts with tests (interview, trace, register, briefs, status, evidence, gate, stale, rulings, handoff, worktree, change), then the coverage map, lens charters, seed prompts, role catalog, templates and presets, then playbooks and briefs rewritten from the ent-harness controller files, then the example with its replay file and end-to-end test, then README, install.sh and docs. Every script copied or adapted from ent-harness is reviewed for project-specific content before it lands.

## Appendix A. Lens charters, coverage map and seed prompts

### A.1 Lens charters (kit/interview/lenses/)

**Architect.** Owns the shape of the system. Must leave knowing: what exists today, the data and who owns it, the integrations, measurable non-functional targets, how each part fails, and which architectures are viable. Writes: the domain, data, non-functional and failure sections of `SPEC.md`; `ARCHITECTURE.md`; the ADRs. Challenges: unstated assumptions about scale, consistency or security; a requirement no component owns; a component with no requirement behind it; a technology chosen without an alternative considered.

**Engineering manager.** Owns how it is built and the quality bar. Must leave knowing: who builds it and with what skills, the ways of working already in force, the toolchain, the test strategy, the definition of done, and what evidence each gate needs. Writes: the delivery mechanics section; `checks.yaml`; the evidence requirements in `gates.yaml`; the role catalog use in the register. Challenges: anything that cannot be tested; a gate with no evidence; a task too large for one change; a technology the team cannot maintain.

**Program manager.** Owns what is being built and why. Must leave knowing: the outcome and its measures, every stakeholder and who decides, the scope edges, the risks and unverified assumptions, the regulatory and contractual obligations. Writes: the purpose, users and scope sections of `discovery.md`; personas and journeys in `product.md`; the risk list and decision owners. Challenges: scope that grew without a decision; a journey with no owner; a metric with no target; a risk with no owner.

**Project manager.** Owns when and in what order. Must leave knowing: the hard constraints of date and budget, the dependencies and the critical path, the capacity available, and what is parked and on whom. Writes: the constraints section; `roadmap.md`; the phase and milestone structure of the register; the parked list. Challenges: a dependency ordered wrongly; a milestone with no gate; a date with no basis; work that cannot start because its predecessor has no owner.

**Delivery manager.** Owns how it reaches users and stays running. Must leave knowing: which environments exist today and who runs them, the release shape, the path from a merge to production, operational readiness, and who supports it. Writes: `environments.yaml` and `CLAIM_BOUNDARIES.md`; the release shape; the deployment and operations sections; the operations items of the product surface checklist. Challenges: any claim that needs an environment that does not exist; a release with no rollback; no backup, monitoring or on-call; a first release nobody can actually use.

### A.2 Coverage map (kit/interview/coverage.yaml)

A topic is closed when the ledger holds entries that meet its closing criterion. Conditional topics apply only when the stated condition is true.

| Topic | Owner | Contributes | Closes when |
|---|---|---|---|
| T1.1 Outcome | Program | Delivery | The outcome is one sentence; the first beneficiary is named; up to three measures each have a target and date or an explicit "no target yet"; the cost of not building is stated |
| T1.2 Users and stakeholders | Program | Architect | Every kind of user and dependent system is listed; the decider, the acceptor and the payer are named; conflicting needs have a stated priority |
| T1.3 Existing landscape | Architect | Engineering | Each existing system has an owner and a health note; what must stay unchanged is listed; prior documents are ingested or declared absent |
| T1.4 Scope edges | Program | Project | At least three explicit exclusions; a reduced first release is described; deferred items are listed |
| T1.5 Constraints | Project | Engineering, Program | Dates with their reasons, budget shape, mandated and forbidden technologies and licences, and organisational constraints are each stated or declared none |
| T1.6 Environments | Delivery | Architect, Engineering | `environments.yaml` has a row per environment with whether it exists today and who runs it; each missing one has an owner or is declared out |
| T1.7 Team and ways of working | Engineering | Project | Builders and skills, the human and agent split, reviewer and merger, on-call, conventions in force and expected response time are stated |
| T1.8 Risks and unknowns | Program | all | The top risk is named; prior failures are recorded; each unverified assumption has a verifier and a date; a spike is decided for or against |
| T1.9 Regulatory and contractual (conditional: regulated industry, personal data, payments, government, healthcare or external customers) | Program | Architect | Applicable regulations and standards, existing commitments and the compliance sign-off owner are named |
| T2.1 Personas | Program | Delivery | Each user kind has goals, frequency, expertise, context and main frustration |
| T2.2 Journeys | Program | Architect | Each first-release journey has an ID, trigger, steps, end state and failure paths; the walking-skeleton journey is chosen |
| T2.3 Product surface | Delivery | Engineering, Program | Every checklist item is in the first release, in a named later release, or out with a reason |
| T2.4 Release shape | Delivery | Project | Incremental or single release, pilot users, and the entry condition for each later release are stated |
| T3.1 Functional requirements | Architect | Program | Every journey step has requirements with IDs covering rule, input, output, validation and error |
| T3.2 Data | Architect | Delivery | Entities and ownership, volumes and growth, sensitivity class, retention and deletion, residency where relevant, and the source of truth are stated |
| T3.3 Integrations (conditional: an external system was named) | Architect | Engineering | Each integration has direction, protocol, authentication, limits, owner, sandbox availability and behaviour when it is down |
| T3.4 Non-functional targets | Architect | Engineering, Delivery | Scale, latency, availability, recovery, security, tenancy, observability, cost and accessibility each have a number or an explicit "no target yet" with owner and date |
| T3.5 Failure and recovery | Architect | Delivery | Each integration and stateful component has its slow, down, corrupt and data-loss behaviour, what the user sees and what the operator does |
| T4.1 Candidate approaches | Architect | Engineering, Project | Two or three approaches with trade-offs, a recommendation, and the user's choice recorded as a decision |
| T4.2 Components and boundaries | Architect | Engineering | Each component has an ID, one responsibility, an interface, its data and an owner role; build, buy or reuse is stated |
| T4.3 Cross-cutting concerns | Architect | Engineering | Authentication and authorization, tenancy enforcement, configuration and secrets, observability, migration policy, versioning and error policy are decided |
| T4.4 Deployment and environments | Delivery | Architect | The runtime per environment, the path from merge to production, rollback and migration ordering are stated |
| T4.5 Technology decisions | Architect | Engineering | Each major choice has an ADR with options, decision, consequences and reversibility; licences are checked against the forbidden list |
| T4.6 Quality gates | Engineering | Architect, Delivery | Each quality claim has a gate with its evidence and environment in `gates.yaml`; gates needing a missing environment are marked blocked |
| T4.7 Delivery mechanics | Engineering | Delivery | Repository layout, branching and review rules, definition of done, test expectations per component, merge-gating checks and agent roles per component are stated |
| T6.1 Phase shaping | Project | Architect, Program | The walking skeleton is defined; phases are ordered by risk and value; each milestone has a gate |
| T6.2 Capacity and cadence | Project | Engineering | Parallel implementer count, expected task size, consultation frequency and reporting are agreed |
| T6.3 Parking and blockers | Project | Program, Delivery | Every task that needs a missing environment, decision, vendor or person is parked with an owner and a date |

### A.3 Seed prompts (kit/interview/seeds.yaml)

These are example questions per topic. A lens may use, adapt or ignore them; they are a floor for coverage depth and never a script. Each topic is marked required or optional, with its condition where one applies.

### Stage 1, discover

**T1.1 Outcome** (required)
- In one sentence, what will be true when this is done that is not true today?
- Who feels that change first: an end user, an internal team, a customer's customer?
- How will you measure it? Name up to three metrics with a target and a date, or say "no target yet".
- What happens if this is not built in the next twelve months?

**T1.2 Users and stakeholders** (required)
- List every kind of person or system that will use or depend on this. For each: what they do today without it.
- Who decides scope, who signs acceptance, who pays? Names or roles.
- Who must be consulted before a security, data or legal decision?
- Are there users whose needs conflict? Which wins?

**T1.3 Existing landscape** (required)
- What exists today that this replaces, extends or must integrate with? For each: owner, age, technology, state of health.
- Is there code, data, or a prior spec we should read before asking more? (triggers ingest)
- What must stay unchanged: APIs, data formats, URLs, user habits?
- What are the known problems with what exists?

**T1.4 Scope edges** (required)
- What is explicitly out, even if it seems adjacent? List at least three things.
- What would a first usable release contain if it had to ship in a quarter of the time?
- What is "nice to have" that we should write down and not build now?

**T1.5 Constraints** (required)
- Hard dates and why they are hard (contract, event, regulation, funding).
- Budget shape: fixed, capped, or open with review points? Includes cloud and licence costs?
- Mandated technologies, vendors, clouds or languages, and the reason for each mandate.
- Forbidden technologies or licences (for example copyleft in distributed code).
- Organisational constraints: approvals, procurement, change windows, release trains.

**T1.6 Environments that exist today** (required; feeds environments.yaml)
- For each of: local developer machine, shared CI, test/staging, production, customer-hosted, air-gapped, demo: does it exist now, who runs it, how is it reached, what can run there today?
- Which of these will exist by the time the first release needs them, and who is making that happen?
- Is there a hosted CI with a remote repository, or only local machines?
- What can never be tested outside production (payments, third-party sandboxes, hardware)?

**T1.7 Team and ways of working** (required)
- Who is building this: how many people, which skills, how much of their time? Which parts will agents build and which parts must a human build?
- Who reviews and merges? Who is on call after release?
- Branching, review and release conventions already in force that the program must follow.
- Time zones and availability for questions; expected response time to a blocked task.

**T1.8 Risk and unknowns** (required)
- What is the one thing most likely to sink this? Technical, organisational, or external.
- What has been tried before and failed? Why?
- Which assumptions are we making that nobody has verified? Who can verify each and by when?
- Is there a proof of concept worth doing before committing to the full build? (branch: spike before stage 4)

**T1.9 Regulatory and contractual** (optional; branch: any regulated industry, personal data, payments, government, healthcare, or external customers)
- Which regulations or standards apply (GDPR, HIPAA, PCI DSS, SOC 2, ISO 27001, accessibility such as WCAG, export control)?
- Which contractual commitments exist today: SLAs, data processing agreements, audit rights, uptime credits?
- Who is the accountable owner for compliance sign-off?

### Stage 2, define the product

**T2.1 Personas** (required)
- For each user kind from T1.2: goals, frequency of use, expertise, device and context (desk, field, mobile, API only), what frustrates them most today.

**T2.2 Journeys** (required)
- For each persona, the journeys that matter in the first release: trigger, steps, end state, what counts as success, what can go wrong at each step and what the user sees then.
- Which journey, if it worked end to end and nothing else did, would prove the product? (becomes the walking skeleton)
- Which journeys involve more than one persona or a hand-off between people?

**T2.3 Product surface checklist** (required)
For each item: in first release, later release (named), or out with reason.
- Onboarding and first-run; account and identity; roles and permissions; administration and configuration; notifications; search and reporting; import and export; billing, plans or entitlements; audit and history; help, documentation and support channel; internationalisation and accessibility; mobile or offline use; public API and integrations; operations: deployment, backup and restore, monitoring, alerting, incident runbooks; data lifecycle: retention, deletion, archival, subject access requests.

**T2.4 Release shape** (required)
- Big-bang or incremental? Pilot users first? Feature flags or separate deployments?
- What is the minimum that can go in front of real users, and who are those users?
- What must be true before each later release is started?

### Stage 3, specify

**T3.1 Functional requirements** (required)
- For each journey step: the rule, the inputs, the outputs, the validation, the error. Each becomes a REQ with the journey it serves.
- Which rules are configurable per customer or tenant, and by whom?
- Which behaviours must be identical to the existing system? (branch: T1.3 named one)

**T3.2 Data** (required)
- The main entities and how they relate; which are owned here and which are mirrored from elsewhere.
- Volumes today and in two years; growth drivers.
- Sensitivity classification per entity: public, internal, confidential, personal, special category.
- Retention, deletion and legal hold rules; data residency (branch: more than one jurisdiction).
- Source of truth for each entity when the same data lives in two systems.

**T3.3 Integrations** (optional; branch: T1.3 or T2.3 named external systems)
- For each external system: direction, protocol, authentication, rate limits, ownership, sandbox availability, failure behaviour expected when it is down, contractual constraints.
- Who owns the contract tests and the credentials?

**T3.4 Non-functional targets** (required)
- Scale: users, requests per second, data size, peak versus average, growth.
- Latency and throughput targets per journey that matters.
- Availability target and what an outage costs; maintenance windows allowed.
- Recovery: acceptable data loss (RPO) and downtime (RTO) per data class.
- Security: authentication methods, single sign-on, multi-factor, session rules, secrets handling, threat model summary (who is the attacker, what do they want).
- Tenancy: single tenant, multi-tenant with shared database, or isolated per tenant; cross-tenant isolation proof required?
- Observability: what must be visible to operators, what must be auditable, log retention.
- Cost ceiling per month or per unit of work.
- Accessibility and localisation targets.
- Each target gets a number or an explicit "no target yet, owner, date".

**T3.5 Failure and recovery** (required)
- For each integration and each stateful component: what happens when it is slow, down, returns garbage, or loses data? What does the user see? What does the operator do?
- Which operations must be idempotent or exactly-once?
- Which failures must page someone?

### Stage 4, architect

**T4.1 Candidate approaches** (required)
- Two or three shapes the system could take, with trade-offs on cost, risk, time, operability and fit with T1.5 mandates; the recommendation and why.

**T4.2 Components and boundaries** (required)
- Components, each with one responsibility, its interface, its owner role, its data, and what it must never know about.
- Synchronous versus asynchronous boundaries; where queues or events are justified.
- What is built, what is bought, what is reused from T1.3.

**T4.3 Cross-cutting** (required)
- Authentication and authorization model; tenancy enforcement point; configuration and secrets; observability stack; data migration and schema evolution policy; API versioning; error and retry policy; time and locale handling.

**T4.4 Deployment and environments** (required)
- Target runtime per environment from T1.6; infrastructure as code; how a change travels from a merge to production; rollback; database migration ordering; feature flags.
- What differs between environments and why; what is identical by construction.

**T4.5 Technology decisions** (required)
- For each major choice (language, framework, database, queue, cloud services, auth provider, UI stack): options considered, decision, consequences, reversibility. Each becomes an ADR.
- Licences of every chosen dependency against T1.5 forbidden list.

**T4.6 Quality gates** (required)
- For each quality claim the product will make (it isolates tenants, it survives a zone failure, it handles the target load, it is accessible): which gate proves it, with what evidence, in which environment, and whether that environment exists (from T1.6). Gates needing a non-existent environment are recorded as blocked from the start.

**T4.7 Delivery mechanics** (required)
- Repository layout and ownership; branching and merge policy; code review rules; definition of done for a task; test pyramid expectations per component; CI steps that gate a merge; how secrets reach CI and runtime.
- Agent roles needed (from roles.yaml) and which components each may touch.

### Stage 6, roadmap and register (planning prompts the lenses work through with the user)

**T6.1 Phase shaping** (required)
- Walking skeleton: which journey, which components, which environments.
- Phase order by risk and value: what must be learned early; what depends on external parties; what can be parallelised.
- Milestones that mean something to stakeholders, each with its gate.

**T6.2 Capacity and cadence** (required)
- How many implementers can run in parallel given review and merge capacity; expected task size; how often the user wants to be consulted (per phase, per milestone, only on blockers).
- What the user wants reported, and where.

**T6.3 Parking and blockers** (required)
- Tasks that need an environment, a decision, a vendor, or a person that is not available: parked with an owner and a date, never silently dropped.
