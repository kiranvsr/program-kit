# program-kit: a governed delivery framework for agent-run software programs

This is the standing design document for program-kit. It describes the kit as built (version 0.1.0): what it is for, how a program moves from a few lines of intent to a frozen specification and a task register, how tasks are executed and accepted, and why each rule exists. `README.md` has the commands to start; `docs/FAQ.md` answers the questions newcomers ask most. Throughout, `pk` means `python3 kit/scripts/pk.py`.

## 1. Purpose

`program-kit` is an open-source, provider-neutral template that turns a few lines of intent into a fully specified, governed delivery program, and then runs that program with AI agents at a quality bar that survives hand-offs between sessions, models and providers.

It is extracted from the originating program (152 tasks, three controllers from three providers, 174 rulings) with every project-specific element removed. The process shipped here is the strict process as run there: every task, however small, goes through the same lifecycle.

A user starts by writing a short brief. The kit then explores the requirements with them through a fixed set of stages. Five role lenses (architect, engineering manager, program manager, project manager, delivery manager) drive the questions dynamically from the user's answers, over a coverage map that fixes what must be known before a stage can close. This continues until the product is defined end to end, specified with identified requirements, architected with recorded decisions, and frozen by an independent review. Only then is the task register built. The kit's execution loop then builds the tasks with implementers, independent QA and commit-bound evidence, merging one branch at a time, and keeps a hand-off document current so anyone can take over.

**Who it is for.** A single developer or small team that wants to run a multi-week build with agents and needs the result to be trustworthy: no fabricated test results, no silently narrowed scope, nothing claimed about environments that do not exist.

**Non-goals.** It is not an agent runtime, a CI system or a project-management tool. It does not host anything. It does not choose models. A "lite" process is out of scope: only the strict process is shipped, and nothing in the kit lets a task skip a step.

## 2. Delivery shape

- **Repository.** MIT licence, usable as a template repository. The kit never pushes anything anywhere; publishing a project is always the user's decision.
- **Two ways to adopt.** Create a new repository from the template, or run `sh install.sh <target-repo> --preset go|node|python` to copy the `kit/` directory into an existing repository (then open `kit/playbooks/00_START.md`). `kit/VERSION` records the kit version. `kit/MANIFEST.sha256` records the sha256 of every file under `kit/`. `install.sh --upgrade` first checks the installed `kit/` against that manifest and refuses if anything was changed locally; otherwise it replaces `kit/` wholesale. It never reads, writes or lists the project's own `program/` directory.
- **Provider neutrality.** No file names a model, vendor or agent CLI, and `kit/tests/test_neutrality.py` fails the build if one does. Playbooks are plain Markdown an agent reads. Scripts are Python 3.11+ standard library only, so they run anywhere Python and git run. Model strength is spoken of as tiers (section 7), never as names.
- **Project-declared checks.** The project declares its build, test and scan commands in `program/checks.yaml`. The kit ships presets for Go, Node/TypeScript and Python that the installer offers; a project can start from one and edit it.
- **A small YAML subset.** The kit's machine-read files (coverage map, ledger, register, checks, gates, environments) use a deliberately small YAML subset read by `kit/scripts/miniyaml.py`, so that the scripts need no third-party parser. The reader refuses anything outside the subset with the line number instead of guessing.
## 3. Repository layout

```
program-kit/
  README.md                 what it is, five-minute start, the two adoption paths
  LICENSE                   MIT
  install.sh                copies kit/ into a target repo; --upgrade; --preset go|node|python
  kit/
    VERSION
    MANIFEST.sha256         sha256 of every file under kit/; the upgrade guard
    playbooks/
      00_START.md           entry point: which playbook to open next, from the state files
      01_discover.md        stage 1
      02_define_product.md  stage 2
      03_specify.md         stage 3
      04_architect.md       stage 4
      05_freeze_review.md   stage 5
      06_register.md        stage 6: roadmap and task register
      07_task_specs.md      stage 7, just in time per task, never approved
      08_execute.md         the controller loop (dispatch, hand-back gate, QA, merge)
      09_handoff.md         hand-off, takeover, successor prompt
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
      BRIEF.md, discovery.md, product.md, SPEC.md, ARCHITECTURE.md, adr.md, roadmap.md
      environments.yaml, gates.yaml, traceability.yaml, register.yaml, ledger.yaml
      task_SPEC.md, task_PLAN.md, freeze_review.md, signoff.md
      phase_brief.md, milestone_brief.md, step_brief.md, task_brief.md     the generated brief shapes
      results.schema.json, acceptance.schema.json, status.schema.json
      checks.yaml, CLAIM_BOUNDARIES.md, HANDOFF.md, controller-state.json, rulings.md
      stage_report.md       the completion report every stage ends with
      challenge_report.md   the per-lens challenge round report
      change_request.md
    presets/
      checks.go.yaml, checks.node.yaml, checks.python.yaml
    scripts/                every script runs as `pk <command>`; `kit/scripts` is first on the path
      pk.py                 the single CLI entry: `python3 kit/scripts/pk.py <command>`
      common.py             root finding, atomic file writes, YAML and JSON helpers, git and subprocess helpers
      miniyaml.py           the YAML subset reader and writer
      interview.py          stage state machine, ledger and coverage validation, reports, approvals, ingest, replay
      trace.py              traceability matrix: validate, sync, impact (journeys, requirements, components, gates, tasks)
      register.py           validates register.yaml; assigns IDs; dependency closure; cycle check; granularity rules; change log
      briefs.py             generates program/briefs/** from the register
      status.py             the only writer of program/status/*.json
      evidence.py           the evidence kit (section 9.2)
      gate.py               the hand-back gate, merge check, binding check and gate claims
      fetch.py              fetches and caches network inputs by digest; the only script that downloads
      stale.py              stale-evidence check of records against main
      rulings.py            appends a numbered ruling; validates the log
      handoff.py            regenerates the generated block of HANDOFF.md; check
      worktree.py           creates and lists task worktrees
      change.py             change requests: create, impact, approve, apply
      manifest.py           writes and checks kit/MANIFEST.sha256
    tests/                  unit tests for every script (standard-library unittest) and the neutrality test
  examples/
    tiny-service/           a worked example with an end-to-end test
  docs/
    DESIGN.md               this document
    FAQ.md
```

A project that installs the kit gets `kit/` (owned by the kit, replaced on upgrade) and `program/` (owned by the project). `pk init` creates `program/` with the starting files below; everything else appears as the stages and tasks produce it:

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
  sources/                  documents ingested into the interview
  reports/stage-N.md        completion report per stage
  reports/stage-N-challenge.md   what each lens challenged before approval and how it was resolved
  approvals/stage-N.json    date, approved artifact paths and hashes, open-topic count
  briefs/                   generated phase/milestone/step/task briefs
  tasks/<ID>/SPEC.md, PLAN.md
  status/<ID>.json
  evidence/<ID>/produce.py, results.json, report.md, acceptance.json
  claims/<GATE>/*.json      per-gate claim docs regenerated after merges
  rulings.md                append-only, numbered R1..
  changes/CR-NNN.md         change requests after approval
  changes/register-log.md   one line per register change after approval
  checks.yaml
  HANDOFF.md, controller-state.json, progress.md
```
## 4. Stage flow: from a few lines to a frozen spec and a task register

### 4.1 How requirements are explored

The interview is not a scripted questionnaire. Questions are generated from the user's answers by five role lenses, and a coverage map decides when a stage is finished. The lenses supply relevance and depth; the map supplies a definition of done that is the same for every project, model and provider.

- **Coverage map.** `kit/interview/coverage.yaml` lists, per stage, the topics that must be closed, the lens that owns each, the lenses that contribute, and the closing criterion: what must be known, in what form, for the topic to count as closed. It contains no question wording. A topic may be conditional on an earlier answer. Appendix A summarises its content.
- **Role lenses.** Each lens has a charter in `kit/interview/lenses/`: what it owns, what it must leave each stage knowing, which artifact sections it writes, and what it challenges. The three management lenses have separate ground: the program manager owns what and why, the project manager owns when and in what order, the delivery manager owns how it reaches users and stays running. Appendix A summarises the charters.
- **Requirements ledger.** `program/ledger.yaml` is the shared memory of the interview. Every entry has an ID, the topics it bears on, its source (a user message with its date, an ingested document, or an inference) and one of four kinds: fact (`F-`), assumption (`A-`, with the default taken and whether the user has confirmed it), decision (`D-`, made by the user, or by the agent after "decide" with its ruling number), open question (`Q-`, with the lens that raised it and whether it blocks the stage). Artifacts are written from the ledger, so nothing in a spec lacks a recorded origin.
- **The loop.** One interviewer speaks to the user. After each answer it updates the ledger, then takes each lens in turn over the open topics of the current stage. Each lens nominates at most one next question, with the reason and what a different answer would change. The interviewer merges duplicates and asks the nomination with the highest impact and uncertainty. By default this is one agent adopting the lenses in turn, not five agents per question.
- **Assumptions before questions.** Where a sensible default exists, a lens records an assumption instead of asking. Assumptions are shown to the user in batches to correct, which is faster than asking each one. An unconfirmed assumption stays marked in every artifact that depends on it.
- **Seed prompts.** `kit/interview/seeds.yaml` holds example questions per topic (Appendix A summarises them). A lens may use, adapt or ignore them. They exist so a weaker model still explores every topic to a reasonable depth.
- **Challenge round.** Before a stage is approved, each lens reviews the draft artifact and the ledger against its charter in its own agent session, independent of the interviewer, and returns gaps as questions or findings. Lens sessions may run in parallel; a single-model provider runs them one after another. The interviewer resolves each gap with the user or records it as deferred. `program/reports/stage-N-challenge.md` lists every gap and its resolution.
- **Stopping rule.** A stage can be approved when all of these hold: every applicable required topic meets its closing criterion or is deferred with an owner and a date; every lens records no blocking open question; the challenge round has no unresolved finding; and the best remaining nominations would not change scope, architecture, plan or a gate. Each stage also has a question budget shown to the user; on reaching it the interviewer asks whether to continue or to take defaults for what remains.
- **Coverage report.** `pk interview report N` writes `program/reports/stage-N.md`: every topic with its state (closed, with the ledger entries that close it; deferred, with owner and date; not applicable, with the lens's reason), the assumptions still unconfirmed, and each lens's sign-off line.
- **Write-back.** Before writing a stage's artifact, the interviewer writes back its understanding in a short note the user corrects. The artifact is produced from the corrected note and the ledger.
- **Approval.** `pk interview approve N` checks the stopping rule mechanically (topic states, blocking questions, challenge findings) and writes `program/approvals/stage-N.json` with the date and each artifact path and sha256. `pk interview next` reads the approvals and the artifact hashes to name the next stage, so a session can stop anywhere and the next one resumes at the first unapproved or modified stage, with the ledger intact.
- **Amend.** `pk interview amend N` re-hashes the approval after a correction in one step, for corrections that do not change meaning. A change of meaning after stage 5 goes through a change request (section 6).
- **Pacing.** Closed questions on one topic may be batched, three to five per message, each with the default the lens would take. Open questions go one per message. Every message shows progress against the coverage map ("14 of 22 topics closed for this stage"). The user may say "decide" to delegate a question, "defer" to park a topic with an owner, or "take defaults" to accept the lenses' assumptions for all remaining optional topics.
- **Pre-answered input.** The user may point at an existing document. `pk interview ingest <file>` copies the document into `program/sources/` and records it; the agent then maps it onto the coverage map and writes ledger entries with the document as their source; the lenses then explore only what is still open.
- **Replay and testing.** The scripts validate structure, never wording: ledger entries, topic states, the stopping rule, approvals. `pk interview replay <ledger> <artifacts-dir>` installs a recorded ledger and its artifacts and approves stages 1 to 6 without a user, for the example and for tests.
- **Cost.** This costs more tokens per stage than a scripted questionnaire, and two runs on the same brief will differ in wording. The coverage map keeps them equal in what they cover.

### 4.2 The stages

**Stage 0, brief.** `program/BRIEF.md`: anything from three lines to a full document. No format is enforced.

**Stage 1, discover.** Purpose, who it is for, success criteria, constraints, what is explicitly out, what exists already, team and timeline, and the environment matrix: `environments.yaml` lists every environment (local, CI, staging, production, air-gapped, hosted services) with `exists: true|false` today. `CLAIM_BOUNDARIES.md` is generated from it: anything that needs an environment that does not exist is unclaimable from the start, and `pk gate` enforces that at evidence time. Artifacts: `discovery.md`, `environments.yaml`, `CLAIM_BOUNDARIES.md`.

**Stage 2, define the product.** Personas with IDs (`U1..`), user journeys with IDs (`J1..`) each with entry, steps, exit and failure paths, and the product surface checklist: onboarding, authentication and authorization, administration, billing or entitlement if any, operations (deploy, backup and restore, monitoring, alerting), data lifecycle, documentation, support. Every checklist item is in scope, out of scope with reason, or deferred to a named later release. The first release is defined as a set of journeys; later releases are named. Artifact: `product.md`; `traceability.yaml` starts with the journeys.

**Stage 3, specify.** Requirements with IDs (`REQ-001..`), each traced to at least one journey or to a cross-cutting concern; non-functional requirements with IDs (`NFR-..`) for scale, availability, latency, security, compliance, tenancy, data retention and residency, each with a measurable target or an explicit "no target yet, owner"; domain objects and interfaces; security and tenancy boundaries; failure modes and recovery; observability; open questions closed by answer or ruling. Artifact: `SPEC.md`; `traceability.yaml` gains requirements.

**Stage 4, architect.** The agent proposes two or three candidate architectures with trade-offs and a recommendation; the user picks. Then: components with IDs (`C1..`) and boundaries, module placement, data flow, deployment shape per environment, cross-cutting concerns (authn/z, tenancy, observability, data lifecycle, configuration, secrets), technology choices each recorded as an ADR in `program/adr/` (context, options, decision, consequences), and `gates.yaml`: the named gates (`G-..`) that will prove quality claims, each with the evidence it requires and the environment it needs. Artifacts: `ARCHITECTURE.md`, `adr/*`, `gates.yaml`; `traceability.yaml` gains components and gates.

**Stage 5, freeze review.** An independent spec-QA agent, never the author, reviews stages 1 to 4 together against `kit/briefs/spec_qa.md`: placeholders, contradictions, ambiguity, every journey covered by requirements, every requirement owned by a component, every quality claim owned by a gate, every non-functional requirement with a target or an owner, every environment claim consistent with `environments.yaml`, surface checklist complete. It returns a finding-to-fix table in `program/freeze/review.md`; the author fixes, the reviewer re-checks only the findings. `pk trace validate --stage 5` must pass. Every assumption in the ledger must by now be confirmed by the user or converted to a decision; none survives the freeze unconfirmed. Each lens records its sign-off, and the user signs `program/freeze/signoff.md`. Approval 5 freezes the spec; from here on changes are change requests.

**Stage 6, roadmap and register.** `roadmap.md` shapes phases by capability: the first phase is a walking skeleton (one thin end-to-end journey through every component, deployed to every existing environment), then vertical slices by journey, with gates attached to phase ends. `register.yaml` then holds phases → milestones → steps → tasks with stable IDs (`P1-M02-S01-T03`), each task with a one-paragraph text, agent role from `roles.yaml`, size (S, M, L with the kit's definitions), environments, dependencies, target gate, and the requirement and journey IDs it closes. `pk register validate` checks: unique IDs and ID shape, dependencies exist, no cycles, role exists in `roles.yaml`, size S or M (size L is refused: split it into steps), fan-in limit per task, well-formed `closes` IDs, and no task that needs an environment marked non-existent unless it is explicitly `blocked_by` that environment. `pk trace validate --stage 6` checks that every first-release requirement is closed by at least one task and that every ID a task closes exists. A task's gate is optional (`null` is allowed), and one task being one PR by one role is a convention the controller applies when it reviews the register, not something a script checks. `pk briefs generate` writes the brief tree. Approval 6 freezes the register; later changes go through `pk register change --note TEXT`, which logs the change in `program/changes/register-log.md` and regenerates the briefs.

**Stage 7, task specs, just in time.** Stage 7 runs once per task and is never approved as a whole: after approval 6 `pk interview next` names stage 7 and stays there, which means discovery is complete. A task gets its `SPEC.md` and `PLAN.md` when it reaches the front of the queue, not before, so specs reflect what was learned from earlier tasks. The SPEC fixes scope in/out, dependencies, consumed contracts from the architecture, security and tenancy, failure modes, observability, module placement and resolved questions, and lists the requirement IDs it closes; it may carry `Guarded edit: <path>` lines, the user's approval for the task to change a guarded file. The PLAN fixes the PR boundary, work breakdown, exact commands (each line starts `$ `, with an optional ` # expect N`), definition of done, gate evidence and rollback. A spec-QA agent, never the author, reviews both against the register text, the architecture and the traceability matrix with a finding-to-fix table; the task is dispatched only on acceptance. The SPEC, the PLAN and the task's `in_progress` status are then committed to main together (the dispatch commit, section 5), because the gate reads them from main.

### 4.3 Status state machine

`pk status set` (the only writer of `program/status/<ID>.json`) allows only these transitions, by role:
- `not_started → in_progress` (controller, at dispatch)
- `in_progress → review` (implementer, after the hand-back gate prints its OK line)
- `review → in_progress` (QA, with findings)
- `review → done` (QA only, with an acceptance file bound to the current results record)
- `not_started|in_progress → blocked` (controller, with `blocked_by`) and `blocked → not_started|in_progress`
- `not_started → parked` (controller, with the user decision it waits on) and `parked → not_started`

`done` is terminal; a later change request creates a new task. A task with no status file is `not_started`. Every transition is checked in full before anything is written, so a refusal leaves no file behind.
## 5. Execution loop (playbook 08)

**Roles.** Three roles, each a separate agent session:
- The **controller** picks the next task, dispatches it, answers questions with recorded rulings, merges, and keeps the state current. It never writes product code.
- An **implementer** owns one task in its own worktree and branch, builds test-first against SPEC and PLAN.
- **QA** never wrote the code. It decides acceptance with evidence. Only QA, through `pk status set ... done --role qa`, can mark a task done.

**Per task.**
1. **Dispatch.** The gate reads the task's SPEC, its PLAN, the `Guarded edit:` lines and `checks.yaml` from main, so they must be on main before work starts. The controller sets the task `in_progress`, commits the SPEC, the PLAN and that status file to main together as one commit (the dispatch commit), and only then runs `pk worktree new <ID>`, which creates `.worktrees/<id>` on branch `task/<ID>` from main (the project must git-ignore `.worktrees/`, or every worktree makes main look dirty). The implementer receives its role brief, the task brief, the SPEC, the PLAN, the rules and the rulings the SPEC cites. From here on the status record changes only on the task branch.
2. **Build.** Test-first. The implementer decides anything with precedent itself and records the decision. It asks the controller only for a new dependency, a security boundary without precedent, a change to user scope, or conflicting rulings. The controller answers with a numbered ruling. A branch never edits its own SPEC or PLAN, and edits a guarded file only when the SPEC on main carries a matching `Guarded edit:` line.
3. **Evidence producer.** The implementer writes `program/evidence/<ID>/produce.py` with `kit/scripts/evidence.py`: literal PLAN commands, parsed derived fields, complete input hashes and the planted-bug table.
4. **Hand-back gate.** `pk gate handback <ID> --branch task/<ID>`, run from the main checkout, runs in order: the guarded-file check; a trial merge of the branch into main in a temporary worktree; the project's build and compile checks on the trial merge; the project's test, scan and extra gate commands from `checks.yaml`; the check that the branch did not change its own PLAN, then the PLAN's exact commands; `produce.py`; `pk stale`. It prints a final `OK: <branch> passes the hand-back gate at <sha>` line only if every step passed. Exit code alone is not a pass; the line is. Failing the gate returns the work to the implementer without a QA round.
5. **Evidence commit.** `program/evidence/<ID>/results.json`, produced only from a clean tree, binds the work to one commit: head and main SHAs, the sha256 of every consumed input (source, tests, fixtures, dependency closure, config, lock files, tool helpers), every command with its literal text, real exit code, parsed derived fields and `as_expected`, and the planted-bug table. The gate leaves it uncommitted. The implementer then sets the task `review` with the results file as evidence, writes its report to `program/evidence/<ID>/report.md`, and makes one commit of `results.json`, the report and the status file. That is the evidence commit, and it follows the gated commit.
6. **QA.** An independent agent runs `pk gate binding <ID> --branch task/<ID>` (the commit recorded in `results.json` is an ancestor of the branch tip, and every later commit touches only this task's evidence directory and status file), re-runs the PLAN commands, tries to defeat each new guard with its own planted bugs, checks scope in/out against the SPEC, the closed requirement IDs against the traceability matrix, and claims against `CLAIM_BOUNDARIES.md`. It either writes `acceptance.json` (bound to the evidence commit, with its own commands and bugs) and sets status done, or rejects with numbered findings, each with severity, location and the exact fix expected.
7. **Fix rounds.** Fixes go back to the same implementer. The re-check covers only the findings, the binding check and the gate, and may run on a Tier 2 model, which must escalate anything outside its checklist.
8. **Merge.** The controller runs `pk gate merge-check --branch task/<ID>` from main; its last line must be `OK: <branch> merges into main at <sha>`. Then `git merge --no-ff`. Accepted branches merge one at a time, right after acceptance. A branch whose hashed inputs were untouched by later main moves and whose merge-tree is clean does not re-produce its evidence. Each merge appends the stale-evidence list for later batched re-binding; stale evidence is never chased one merge at a time.
9. **Bookkeeping.** After every merge or state change: update `controller-state.json`, append to `progress.md`, run `pk gate claims` and `pk gate validate` for the gates the merge touched, run `pk handoff update` and `pk handoff check`, commit `program/`.

**Blocked and parked.** A task not yet dispatched that needs an environment which does not exist is set `blocked` on main with `blocked_by` naming it. A task that waits on a user decision is `parked` with the decision named; parked is reachable only from `not_started`. A task already in progress that needs a user decision is set `blocked` on its own branch.

**Gate claims.** `gates.yaml` names, per gate, the evidence records it aggregates. `pk gate claims <GATE>` regenerates `program/claims/<GATE>/*.json`, one per record, each stating per environment whether the claim is evidenced, blocked or not evaluated, computed only from acceptance files and `environments.yaml`. `pk gate validate <GATE>` fails on any drift between the claim docs and a recomputation. Nothing ever upgrades a development claim to a non-existent environment.

**Planted bugs.** A planted bug counts as caught only when a test fails in code that builds (or, for non-compiled artifacts, that loads). The planted-bug runner therefore needs a build command, records `builds` and `kill_reason` per row, and always includes one deliberately non-building row recorded as not caught, so a runner that counts build failures as catches is visible.

**Pre-existing failures.** A PLAN command that fails identically on clean main is recorded as a named environmental deviation: real exit, one reproduction on clean main with its log hash, proof the task's inputs to it are unchanged, the failure message asserted so a silent pass or a different failure flips the row, nothing claimed from it, and an owner follow-up. Never a pass.

**What the gate cannot see.** A `produce.py` that hand-writes a well-formed `results.json` cannot be detected by the gate, and files written to git-ignored paths are invisible to the clean-tree checks. That is why QA re-runs the evidence independently and checks the binding rather than trusting the record, and why a project must git-ignore its bytecode and build output (a leftover untracked file fails the clean-tree check, so the omission is loud, not silent).
## 6. Change requests after the freeze (playbook 10)

A change of meaning to anything approved at stage 5 or later is a change request `program/changes/CR-NNN.md`: what changes, why, who asked. `pk change create` writes the request; `pk change impact CR-NNN <IDs>` lists through the traceability matrix every requirement, component, gate and task touched, split into done tasks (whose acceptance would no longer cover the new meaning), in-flight tasks and not-started tasks. The user approves the CR with that impact in view. Then: artifacts are amended with the CR number in their change log, `pk trace validate --stage 6` must pass again, affected done tasks get a new follow-up task in the register, affected in-flight tasks get a ruling delivered to their implementer, and `pk register change --note TEXT` records the register edit and regenerates briefs. `pk change apply` refuses until `pk change approve` has recorded the user's approval. A CR never edits a done task's evidence or acceptance.

## 7. Hand-off and takeover (playbook 09)

`program/HANDOFF.md` is the single document a successor needs. It has a maintained part and a generated part:
- maintained: how the program works, environment setup, known gotchas, ordered next steps, the model-tier mapping, and one line saying where the rules live (`kit/rules/*.md` and `program/rulings.md`). The hand-off carries state, not rules: the rules are cited from where they live so that a copy cannot drift;
- generated by `pk handoff update`, between markers: counts, in-flight tasks with their stage text, unmerged branches and worktrees, queued and parked tasks, open follow-ups, open change requests.

`pk handoff check` exits 1 when the generated part is stale; the controller runs it before every commit of `program/`. A process ruling changes the playbook or rule file in the same commit.

`kit/playbooks/09_handoff.md` includes the successor prompt: read HANDOFF top to bottom, read the role briefs and `kit/rules/*.md`, verify the state (git status, unmerged branches, worktree cleanliness, void partial evidence by exact path, leftover processes and containers, disk, and that the repo-wide suite passes on main, run from the commands in `program/checks.yaml`), then resume at HANDOFF's next steps. A successor never restarts an accepted task and never resumes an agent whose task has since merged.

**Model tiers.** Roles, never names. Tier 1 is the strongest model available: controller, first-pass implementation and QA, the interviewer and the lens challenge rounds, stage artifacts and specs, freeze review, any re-check that escalates. Tier 2 is a cheaper model, used only for scoped re-checks of exact fixes, re-bind runs and mechanical producer runs with a checklist; it reports ESCALATE instead of judging anything outside the checklist. Never Tier 2 for first-pass work, security rulings, or accepting with an open important finding. A provider with one model uses it for everything. Nothing in the kit names a model; each controller records its mapping in HANDOFF when it takes over. The commit attribution line follows the controller's own session and is not copied from an earlier commit.

## 8. Hard rules (kit/rules/HARD_RULES.md)

These bind every agent in every role and stage. If a rule seems to block the work, the agent stops and tells the user; it does not look for a way around. The file holds twenty numbered rules; in summary:

- **Evidence and claims.** Never fabricate evidence, test results, CI results, gate passes or human approvals. Never claim anything about an environment `environments.yaml` marks `exists: false`; work that needs one stays `blocked_by` it. Development acceptance is not production readiness. An acceptance lives where the code reads it: a side file that leaves the read fields unaccepted is not an acceptance. Checks and evidence run from a clean tree, so the project git-ignores the bytecode and build output its commands write.
- **Status, register and frozen artifacts.** Only QA sets `done`, only through `pk status set`. The register changes only through `pk register change`; status files, approvals and generated briefs are never hand-edited. Briefs are immutable once approved. A frozen artifact changes only through a change request. Guarded files (the `guarded_files` list in `checks.yaml`) change only with the user's approval, recorded as a `Guarded edit: <path>` line in the task's SPEC on main before dispatch; a branch cannot approve its own edit.
- **Git and files.** Never rewrite history: no rebase, reset or force-push; merge main into branches; never a bare `git stash`. Never commit secrets, private keys, licence-restricted artifacts, editor or build leftovers or scratch files. Never push, deploy or apply infrastructure without the user's explicit instruction. Every destructive command uses a literal path; clean up only what you created, by exact name. Commit messages end with the agent's own attribution line.
- **Permissions and scope.** A denied action is reported to the user, never routed around and never delegated to another agent. An implementer asks the controller only in four cases (a new dependency, a security boundary with no precedent, a change to user scope, two conflicting rulings) and decides everything else itself. A task builds exactly the scope of its SPEC and PLAN; anything else is a follow-up.
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
10. Validators and gate steps never download inline. Anything fetched from the network (toolchains, pinned archives, fixtures) is cached by content digest under a durable per-host cache (`$PROGRAM_KIT_CACHE`, default `~/.cache/program-kit/fetch`), fetched once per host by a separate `pk fetch --pins <file.json>` step that runs in the background at session setup with a long timeout, and verified locally against the recorded digest with `pk fetch verify`. A pin is `{url, sha256, size, filename}`. A verify step that finds its input missing from the cache fails with the fetch command to run, never by downloading. Receipts stay digest-bound, so caching changes nothing about what is proven. The fetch step accepts https URLs only, validates every cached name against a strict pattern, writes through an exclusively created temporary file in the digest directory, removes it on any failure, checks size and digest before the rename, and never follows a symlink for a cache entry or its parent; each of those guards has its own test and planted bug.

### 9.2 evidence.py
Standard-library Python. `Evidence(root, task_id)` checks the tree is clean, records head, merge-base and main, and `hash_inputs` hashes inputs through `git cat-file` so the hash is of the committed blob, never the working copy. `run_command(label, argv, expect, ...)` records the literal text, exit code, parsed fields and `as_expected`. `planted_bugs(table, build, test)` applies each edit to a throwaway mirror of HEAD, builds, runs the named test, records `builds` and `kill_reason`, restores the mirror between rows, clears caches, and appends the non-building self-test row; it needs a build command, because a row cannot be judged "builds" without one. `stale()` lists records whose hashed inputs changed on main. `write()` refuses to write unless the clean-tree guard, the self-test row and `all_commands_as_expected` hold. `pk evidence validate <results.json>` re-checks a record against `kit/templates/results.schema.json` and its invariants. `checks.yaml` supplies the project's build, test and scan commands; the kit never assumes a language.

Known limits, stated so nobody relies on more than is there: a record edited consistently by hand still validates, and files written to git-ignored paths are invisible to the clean-tree check. Binding a record to its commit (`pk gate binding`) and re-running its commands is the reviewer's job.

### 9.3 checks.yaml
```yaml
build:  [["go", "build", "./..."]]
test:   [["go", "test", "./..."]]
scan:   [["gosec", "./..."], ["gitleaks", "detect", "--no-git"]]
compile_check: [["go", "vet", "./..."]]
extra_gate: []            # project-specific gate commands, run in order
guarded_files: ["go.mod", "go.sum", "program/checks.yaml"]
```
Presets fill these for Go, Node/TypeScript and Python. Commands are argv lists; `pk gate` runs them without a shell. `build`, `test` and `scan` must each list a command. A guarded file is an exact path, or a directory when the entry ends in a slash. The gate reads `checks.yaml` from main, never from the task branch or a dirty checkout, so a branch cannot change the rules it is judged by. Unclaimable environments come from `environments.yaml`, not from here.
## 10. Throughput rules (kit/rules/THROUGHPUT.md)

Rework and token cost, not coding, are what slow a program down. These rules exist to remove rework.

- Merge queue: QA starts only once the branch's evidence reflects current main; merge immediately after acceptance, one branch at a time, in acceptance order. Only documentation-only hand-off commits may land in between.
- Hand-back gate: no QA round until the implementer passed every mechanical check and the gate's last line is the OK line.
- Decide, don't ask: anything with precedent is decided and recorded; questions are reserved for the four cases in section 5.
- Fix rounds return a finding-to-fix table; the re-check covers only those findings, the binding check and the gate.
- At most three implementers and four agents at once; each agent has its own scratch folder.
- No polling: long commands run in the background and the agent waits for completion; never `tail -f`.
- Short reports, in a fixed shape the briefs define.
- After an interruption: void partial evidence by exact path, resume at the interrupted step, never restart a task.
- When two rejections share a cause, the cause becomes a gate check (an `extra_gate` command or a new gate step) and a ruling.
- Read the three rule files and the rulings a SPEC cites; not the whole ruling log.
- A gate that reads every record (a phase gate, a re-bind batch) runs on a quiet main: merge the feature tasks first, then run the gates back to back, merging nothing else while one produces evidence.
- The controller commits hand-off and controller-state updates only after a merge, a ruling or at session end. The one exception is the dispatch commit (SPEC, PLAN and `in_progress` status together, before the worktree is created), because the gate reads them from main.
- A successor verifies that the repo-wide suite passes on main before it dispatches anything.
- Session setup runs `pk fetch` in the background once per host so no later gate step waits on a download.
## 11. Rulings

`program/rulings.md` is append-only. `pk rulings add --role ... --question ... --decision ... --precedent ...` writes the next number (R1 onward), the date, the role that decided, the question settled, the decision and the precedent it generalises. A ruling that changes the process also changes the playbook or rule file in the same commit. A ruling never contradicts an earlier one silently; it supersedes it by name, and `pk rulings validate` checks numbering and supersedes links. `kit/rules/RULINGS_SEED.md` holds the starting set, numbered K1..K33, written from the originating program without project names: the rules in sections 8 to 10 are their digest. A K ruling can be superseded in the same way as an R ruling.
## 12. Error handling

- Every script exits non-zero with a one-line reason on the first invalid input (`error: ...` on stderr); none writes partial output.  `pk register validate` prints `error: register invalid: <N> problem(s)` first, then each problem prefixed `error: `.
- `pk interview approve` refuses while a required topic is open, a lens holds a blocking question, a challenge finding is unresolved, or a ledger entry is malformed or has no source; `pk interview next` refuses to open a stage whose predecessor has no approval, or whose approved artifact hash no longer matches (it names the amend or change-request path instead).
- `pk trace validate` fails on any journey without a requirement, requirement without a component or task, gate without a record, or task closing an unknown ID.
- `pk register validate` fails on duplicate IDs, missing dependencies, cycles, granularity violations and unclaimable environments without `blocked_by`.
- `pk status set` refuses a transition the state machine does not allow.
- `pk gate` stops at the first failing step, prints the step and its last 20 lines, and never prints the OK line.
- `pk evidence validate` and the evidence writer refuse a dirty tree, a missing self-test row, or a command not as expected.
- `pk fetch` refuses to store a file whose digest does not match the pin; `pk fetch verify` refuses to download and names the fetch command instead.
- `pk change apply` refuses to apply a CR without the user's approval record.
- `install.sh --upgrade` refuses if `kit/` does not match its manifest.
## 13. Testing the kit itself

- `kit/tests/` covers every script with standard-library unittest: coverage-map loading and conditional topics, ledger validation (kinds, sources, topic links), the stopping rule, challenge-report resolution, coverage reports, approval and amend, resume at the right stage, replay from a recorded ledger, ingest marking, traceability validation, register validation (duplicate IDs, cycles, unmapped requirements, granularity), ID assignment stability, brief generation determinism, status transitions, evidence writing guards, the planted-bug runner (including that a build failure is not a catch), stale detection, gate claims and drift, binding, ruling numbering, hand-off staleness, change-request impact, fetch guards, and the manifest.
- The shipped content is tested too: every playbook has its required sections, every command a playbook names exists, every playbook file a playbook names exists, and the seed prompts and charters match the coverage map.
- `kit/tests/test_neutrality.py` scans every file in the repository except version-control, worktree and cache directories. It fails on any forbidden vendor, model or project name (case-insensitively; the short names only when not glued to a letter), on any URL outside `docs/FAQ.md` and the one test file that needs loopback addresses, on a file that is not valid UTF-8, and on a zero-width character. The forbidden names live in `kit/tests/_tokens.py`, assembled from fragments, so no file contains one literally.
- `examples/tiny-service/` is a worked example with an end-to-end test.
- `install.sh` is tested by installing into a temporary repository with each preset and running `pk doctor`.
- Run everything with a private temporary directory so leftovers from other runs cannot confuse the leftover checks: `TMPDIR=$(mktemp -d) python3 -W error -m unittest discover -s kit/tests`.
## 14. Out of scope for the first version

A web UI or dashboard; a lite process; hosted anything; model selection; language-specific project scaffolding beyond `checks.yaml` presets; migration of an existing non-kit program into the kit's layout; multi-repository programs.

## 15. How the kit was built

The kit was built task by task, test first, following its own loop where practical: scripts with tests first (interview, trace, register, briefs, status, evidence, gate, stale, rulings, handoff, worktree, change, fetch, manifest), then the coverage map, lens charters, seed prompts, role catalog, templates and presets, then the playbooks and role briefs rewritten from the originating program's controller files, then the installer, the example with its end-to-end test, and these documents. Every script or document adapted from the originating program was reviewed for project-specific content before it landed, and the neutrality test keeps it that way.
## Appendix A. Interview content in summary

The full text lives in `kit/interview/`; this page says what is there and how it is used. Wording changes between runs and models; the structure below does not.

### A.1 Lens charters (kit/interview/lenses/)

Each charter has the same sections: what the lens owns, what it must leave each stage knowing, which artifact sections it writes, what it challenges, how it nominates a question, and its sign-off line.

- **Architect** owns the shape of the system: what exists today, the data and its owners, integrations, measurable non-functional targets, how each part fails, which architectures are viable. Writes the domain, data, non-functional and failure sections of the SPEC, `ARCHITECTURE.md` and the ADRs. Challenges unstated assumptions about scale or security, a requirement no component owns, a component with no requirement, a technology chosen without an alternative.
- **Engineering manager** owns how it is built and the quality bar: who builds it, the toolchain, the test strategy, the definition of done, the evidence each gate needs. Writes the delivery mechanics section, `checks.yaml` and the evidence requirements in `gates.yaml`. Challenges anything untestable, a gate with no evidence, a task too large for one change.
- **Program manager** owns what is being built and why: the outcome and its measures, stakeholders and deciders, scope edges, risks, regulatory and contractual obligations. Writes the purpose, users and scope sections of `discovery.md`, the personas and journeys in `product.md` and the risk list. Challenges scope that grew without a decision, a journey with no owner, a metric with no target.
- **Project manager** owns when and in what order: hard dates and budget, dependencies and the critical path, capacity, what is parked and on whom. Writes the constraints section, `roadmap.md` and the register's phase and milestone structure. Challenges a dependency ordered wrongly, a milestone with no gate, a date with no basis.
- **Delivery manager** owns how it reaches users and stays running: which environments exist and who runs them, the release shape, the path from merge to production, operational readiness, support. Writes `environments.yaml`, `CLAIM_BOUNDARIES.md` and the deployment and operations sections. Challenges any claim that needs an environment that does not exist, a release with no rollback, no backup or monitoring, a first release nobody can use.

### A.2 Coverage map (kit/interview/coverage.yaml)

The map holds 28 topics over stages 1, 2, 3, 4 and 6 (stage 5 is the freeze review and stage 7 is per task; neither has topics). Each topic has an ID, a title, an owning lens, contributing lenses, whether it is required, an optional condition, a closing criterion in words (`closes_when`) and a list of `needs` tags, one per clause of the criterion. A ledger entry carries the tags it settles; a topic closes when every tag is present on a fact, a decision or a confirmed assumption. A conditional topic applies only when its condition holds over the tags present in the ledger (for example `regulated`, `personal_data`, `payments`, `external_system`). Each stage also has a question budget.

| Stage | Topics |
|---|---|
| 1 discover (9) | outcome; users and stakeholders; existing landscape; scope edges; constraints; environments; team and ways of working; risks and unknowns; regulatory and contractual (conditional) |
| 2 define the product (4) | personas; journeys; product surface; release shape |
| 3 specify (5) | functional requirements; data; integrations (conditional); non-functional targets; failure and recovery |
| 4 architect (7) | candidate approaches; components and boundaries; cross-cutting concerns; deployment and environments; technology decisions; quality gates; delivery mechanics |
| 6 roadmap and register (3) | phase shaping; capacity and cadence; parking and blockers |

### A.3 Seed prompts (kit/interview/seeds.yaml)

Example questions per topic, marked required or optional with the same condition as the map. A lens may use, adapt or ignore them. They are a floor for coverage depth so that a weaker model still explores every topic, never a script. For example, the environments topic asks, for each of local machine, shared CI, staging, production, customer-hosted, air-gapped and demo, whether it exists now, who runs it and what can run there today, and what can never be tested outside production.

### A.4 Role catalog (kit/interview/roles.yaml)

Seven roles a register task can carry: backend, frontend, platform, data, security, docs and qa. Each lists which components the role may touch.
