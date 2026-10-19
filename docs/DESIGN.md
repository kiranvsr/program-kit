# program-kit: a governed delivery framework for agent-run software programs

This is the standing design document for program-kit. It describes the kit as built (version 0.4.0): what it is for, how a program moves from a few lines of intent to a frozen specification and a task register, how tasks are executed and accepted, and why each rule exists. `README.md` has the commands to start; `docs/FAQ.md` answers the questions newcomers ask most. Throughout, `pk` means `python3 kit/scripts/pk.py`.

## 1. Purpose

`program-kit` is an open-source, provider-neutral template that turns a few lines of intent into a fully specified, governed delivery program, and then runs that program with AI agents at a quality bar that survives hand-offs between sessions, models and providers.

It is extracted from the originating program (152 tasks, three controllers from three providers, 174 rulings) with every project-specific element removed. The process shipped here is the strict process as run there, and it is the default: unless a program switches the lower tiers on, every task, however small, goes through the same lifecycle. Since 0.3.0 a program may name the paths that always get the full process and give other work a lighter one (section 10.1); a lighter path is never available for those paths.

A user starts by writing a short brief. The kit then explores the requirements with them through a fixed set of stages. Five role lenses (architect, engineering manager, program manager, project manager, delivery manager) drive the questions dynamically from the user's answers, over a coverage map that fixes what must be known before a stage can close. This continues until the product is defined end to end, specified with identified requirements, architected with recorded decisions, and frozen by an independent review. Only then is the task register built. The kit's execution loop then builds the tasks with implementers, independent QA and commit-bound evidence, merging one branch at a time, and keeps a hand-off document current so anyone can take over.

**Who it is for.** A single developer or small team that wants to run a multi-week build with agents and needs the result to be trustworthy: no fabricated test results, no silently narrowed scope, nothing claimed about environments that do not exist.

**Non-goals.** It is not an agent runtime, a CI system or a project-management tool. It does not host anything. It does not choose models. The strict process is the default. The lower verification tiers and the fast path (section 10.1) let a program give named kinds of work a lighter process, only once it has switched them on and never for its full-tier paths; outside them, nothing in the kit lets a task skip a step.

## 2. Delivery shape

- **Repository.** MIT licence, usable as a template repository. The kit never pushes anything anywhere; publishing a project is always the user's decision.
- **Two ways to adopt.** Create a new repository from the template, or run `sh install.sh <target-repo> --preset go|node|python` to copy the `kit/` directory into an existing repository (then open `kit/playbooks/00_START.md`). `kit/VERSION` records the kit version. `kit/MANIFEST.sha256` records the sha256 of every file under `kit/`. `install.sh --upgrade` first checks the installed `kit/` against that manifest and refuses if anything was changed locally; otherwise it replaces `kit/` wholesale, and it also ensures that the project's `.gitignore` holds the kit's ignore lines (`__pycache__/`, `*.pyc`, `.worktrees/`), adding any that are missing and never rewriting the rest, exactly as a first install does. The kit's own tests (`kit/tests`) are neither installed nor recorded, so in an installed project a file under `kit/tests` that the manifest does not list is a local addition like any other; only where `install.sh` sits beside `kit/` (the kit's own repository, or a copy of the whole template) is that directory the kit's test suite and left out of the check. It never reads, writes or lists the project's own `program/` directory.
- **Provider neutrality.** No file names a model, vendor or agent CLI, and `kit/tests/test_neutrality.py` fails the build if one does. Playbooks are plain Markdown an agent reads. Scripts are Python 3.11+ standard library only, so they run anywhere Python and git run. Model strength is spoken of as classes A to D (section 7), never as names; "tier" means a task's verification tier (section 10.1) and nothing else.
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
      RULES.md, RULINGS_SEED.md
    templates/
      BRIEF.md, discovery.md, product.md, SPEC.md, ARCHITECTURE.md, adr.md, roadmap.md
      environments.yaml, gates.yaml, traceability.yaml, register.yaml, ledger.yaml
      task_SPEC.md, task_PLAN.md, freeze_review.md, signoff.md
      task_FAST_BRIEF.md, CONTRACT.md, COMMON.md    the fast-path brief, the contract-first file, the program's common rules
      phase_brief.md, milestone_brief.md, step_brief.md, task_brief.md     the generated brief shapes
      results.schema.json, acceptance.schema.json, status.schema.json
      checks.yaml, CLAIM_BOUNDARIES.md, HANDOFF.md, controller-state.json, rulings.md
      release-set.yaml, fingerprint.schema.json     the release set example and the schema of a fingerprint record (section 9.4)
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
      store.py              the append-only record layout, the per-task pointers and the integrity checks (section 9.2)
      gate.py               the hand-back gate, merge check, binding check and gate claims
      fingerprint.py        the release fingerprint: runs the release set on parallel tracks, derives per-test tables, validates and reads records (section 9.4)
      fetch.py              fetches and caches network inputs by digest; the only script that downloads
      stale.py              drift report: is each task's latest record fresh against the current tree
      rulings.py            appends a numbered ruling; validates the log
      handoff.py            regenerates the generated block of HANDOFF.md; check, with the size budget
      worktree.py           task worktrees: new, list, the detached review worktree, the safe finish
      ctlstate.py           the one reader and writer of controller-state.json
      tracker.py            follow-ups, ad-hoc tasks and `pk record`, one controller step in one command
      cost.py               the append-only cost log and the fresh-agent threshold
      land.py               `pk merge` (the per-merge chain) and integration rounds
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
  approvals/stage-N.json    stage, time, approved artifact paths and hashes, ledger hash (stage 5: also a
                            digest of the ledger entries that touch a topic of stages 1 to 5
                            and of whether each of those topics applies)
  briefs/                   generated phase/milestone/step/task briefs
  tasks/<ID>/SPEC.md, PLAN.md, review.md    or, on the fast path, BRIEF.md alone; CONTRACT.md when contract first
  status/<ID>.json          status, verification tier, fast path and ruling
  evidence/<ID>/latest.json the task's pointer: its latest results and acceptance record (the only mutable
                            evidence file)
  evidence/<ID>/produce.py, report.md
  evidence/<ID>/results/<utc>-<sha256[:12]>.json      append-only results records, never modified
  evidence/<ID>/acceptance/<utc>-<sha256[:12]>.json   append-only acceptance records, never modified
  claims/<GATE>/*.json      per-gate claim docs regenerated after merges
  rulings.md                append-only, numbered R1..
  changes/CR-NNN.md         change requests after approval
  changes/register-log.md   one line per register change after approval
  checks.yaml               commands, guarded files, `parallel_steps` and the `tiers` section
  release-set.yaml          the commands the release fingerprint runs, grouped in tracks (section 9.4)
  evidence/fingerprint/<head12>/   the release fingerprint of one commit: fingerprint.json and one table file per derived table (section 9.4)
  COMMON.md                 run commands, constraints, model mapping, report contract, known failures on main
  costs.jsonl               append-only, one line per subagent run
  rounds/<NAME>.json        integration rounds: members, tested tree, what merged
  HANDOFF.md, controller-state.json, progress.md
```
## 4. Stage flow: from a few lines to a frozen spec and a task register

### 4.1 How requirements are explored

The interview is not a scripted questionnaire. Questions are generated from the user's answers by five role lenses, and a coverage map decides when a stage is finished. The lenses supply relevance and depth; the map supplies a definition of done that is the same for every project, model and provider.

- **Coverage map.** `kit/interview/coverage.yaml` lists, per stage, the topics that must be closed, the lens that owns each, the lenses that contribute, and the closing criterion: what must be known, in what form, for the topic to count as closed. It contains no question wording. A topic may be conditional on an earlier answer: it applies when a tag its condition names is on any ledger entry, of every kind, including an unanswered question and an unconfirmed assumption, because switching a conditional topic on too early is the safe direction. Appendix A summarises its content.
- **Role lenses.** Each lens has a charter in `kit/interview/lenses/`: what it owns, what it must leave each stage knowing, which artifact sections it writes, and what it challenges. The three management lenses have separate ground: the program manager owns what and why, the project manager owns when and in what order, the delivery manager owns how it reaches users and stays running. Appendix A summarises the charters.
- **Requirements ledger.** `program/ledger.yaml` is the shared memory of the interview. Every entry has an ID, the topics it bears on, its source (a user message with its date, an ingested document, or an inference) and one of four kinds: fact (`F-`), assumption (`A-`, with the default taken and whether the user has confirmed it), decision (`D-`, made by the user, or by the agent after "decide" with its ruling number), open question (`Q-`, with the lens that raised it and whether it blocks the stage). Artifacts are written from the ledger, so nothing in a spec lacks a recorded origin.
- **The loop.** One interviewer speaks to the user. After each answer it updates the ledger, then takes each lens in turn over the open topics of the current stage. Each lens nominates at most one next question, with the reason and what a different answer would change. The interviewer merges duplicates and asks the nomination with the highest impact and uncertainty. By default this is one agent adopting the lenses in turn, not five agents per question.
- **Assumptions before questions.** Where a sensible default exists, a lens records an assumption instead of asking. Assumptions are shown to the user in batches to correct, which is faster than asking each one. An unconfirmed assumption stays marked in every artifact that depends on it.
- **Seed prompts.** `kit/interview/seeds.yaml` holds example questions per topic (Appendix A summarises them). A lens may use, adapt or ignore them. They exist so a weaker model still explores every topic to a reasonable depth.
- **Challenge round.** Before a stage is approved, each lens reviews the draft artifact and the ledger against its charter in its own agent session, independent of the interviewer, and returns gaps as questions or findings. Lens sessions may run in parallel; a single-model provider runs them one after another. The interviewer resolves each gap with the user or records it as deferred. `program/reports/stage-N-challenge.md` lists every gap and its resolution.
- **Stopping rule.** A stage can be approved when all of these hold: every applicable required topic meets its closing criterion or is deferred with an owner and a date; every lens records no blocking open question; the challenge round has no unresolved finding; and the best remaining nominations would not change scope, architecture, plan or a gate. Each stage also has a question budget shown to the user; on reaching it the interviewer asks whether to continue or to take defaults for what remains.
- **Coverage report.** `pk interview report N` writes `program/reports/stage-N.md`: a table of every topic with its title, owner, state, what it still misses and the ledger entries that bear on it, the counts of topics and blocking questions, the question budget, and the assumptions still unconfirmed. It does not list the lenses' sign-off lines (they are in the charters, and at stage 5 in the freeze review) or why a topic is not applicable.
- **Write-back.** Before writing a stage's artifact, the interviewer writes back its understanding in a short note the user corrects. The artifact is produced from the corrected note and the ledger.
- **Approval.** `pk interview approve N` checks the stopping rule mechanically (topic states, blocking questions, challenge findings) and writes `program/approvals/stage-N.json` with the stage, the time, each artifact path and sha256 and the ledger's sha256. It also refuses unless every stage below N is approved and current, and names the lowest one that is not. From stage 5 on, the stopping rule runs the topic and blocking-question checks of every earlier stage again, so a topic reopened or a question raised after its own approval blocks the freeze (the reason names the stage); stage 6 also runs the register validation that `pk register validate` runs and, because its requirement check reads the matrix's copy of each task's `closes`, refuses while that `tasks` section differs from what `pk trace sync` would write from the register. Playbook 06 generates the briefs before approval 6, so stage 6 also refuses while `pk briefs check` would report a brief that is missing or differs from the register. A challenge report that is still the untouched skeleton, or has a lens section with neither a finding row nor a `No findings.` line, does not count as a challenge round. `pk interview next` reads the approvals and the artifact hashes to name the next stage, so a session can stop anywhere and the next one resumes at the first unapproved or modified stage, with the ledger intact. For a stage that is approved but modified it lists the changed files and says to run `pk interview amend N` for a correction or to raise a change request; `amend` re-hashes the stage's current file set, so a decision record removed or renamed since the approval leaves it and a new one joins.
- **The ledger after the freeze.** The ledger grows while stages 1 to 4 are worked, so their approvals are judged by their artifact hashes only. From approval 5 on, a ledger change makes the highest approval modified. Stage 6 is itself an interview stage, so its answers are written after approval 5: the stage 5 approval therefore also records `frozen_entries_sha256`, a digest over a canonical form of the entries that touch a topic of stages 1 to 5 and the sources they cite, and also whether each stage 1 to 5 topic applies over the whole ledger (a tag on a stage 6 entry that switches an earlier conditional topic on or off therefore counts as a change to the freeze). The digest is what is compared for the freeze approval even after approval 6 is written, so a later edit to an earlier-stage entry names stage 5 and cannot be absorbed by amending stage 6 alone. Adding, changing or removing an entry whose topics all belong to stage 6 leaves stage 5 current; an entry that names a stage 6 topic and an earlier one counts as the earlier stage, and adding, changing or removing any entry that touches stages 1 to 5 makes stage 5 modified until `pk interview amend 5` or a change request. After approval 6 the whole ledger file is compared, so any ledger change makes stage 6 modified. `approve`, `amend`, `next` and `replay` apply the same rule, and a stage 5 approval written without the digest is compared by the whole file.
- **Amend.** `pk interview amend N` re-hashes the approval after a correction in one step, for corrections that do not change meaning. A change of meaning after stage 5 goes through a change request (section 6).
- **Pacing.** Closed questions on one topic may be batched, three to five per message, each with the default the lens would take. Open questions go one per message. Every message shows progress against the coverage map ("14 of 22 topics closed for this stage"). The user may say "decide" to delegate a question, "defer" to park a topic with an owner, or "take defaults" to accept the lenses' assumptions for all remaining optional topics.
- **Pre-answered input.** The user may point at an existing document. `pk interview ingest <file>` copies the document into `program/sources/` and records it; the agent then maps it onto the coverage map and writes ledger entries with the document as their source; the lenses then explore only what is still open.
- **Replay and testing.** The scripts validate structure, never wording: ledger entries, topic states, the stopping rule, approvals. `pk interview replay <ledger> <artifacts-dir>` installs a recorded ledger and its artifacts and approves stages 1 to 6 without a user, for the example and for tests.
- **Cost.** This costs more tokens per stage than a scripted questionnaire, and two runs on the same brief will differ in wording. The coverage map keeps them equal in what they cover.

### 4.2 The stages

**Stage 0, brief.** `program/BRIEF.md`: anything from three lines to a full document. No format is enforced.

**Stage 1, discover.** Purpose, who it is for, success criteria, constraints, what is explicitly out, what exists already, team and timeline, and the environment matrix: `environments.yaml` lists every environment (local, CI, staging, production, air-gapped, hosted services) with `exists: true|false` today. `CLAIM_BOUNDARIES.md` is written by hand from its template, using `environments.yaml` (playbook 01, step 6): anything that needs an environment that does not exist is unclaimable from the start. No script reads that file; `pk gate` enforces the rule at evidence time from `environments.yaml` and `gates.yaml`. Artifacts: `discovery.md`, `environments.yaml`, `CLAIM_BOUNDARIES.md`.

**Stage 2, define the product.** Personas with IDs (`U1..`), user journeys with IDs (`J1..`) each with entry, steps, exit and failure paths, and the product surface checklist: onboarding, authentication and authorization, administration, billing or entitlement if any, operations (deploy, backup and restore, monitoring, alerting), data lifecycle, documentation, support. Every checklist item is in scope, out of scope with reason, or deferred to a named later release. The first release is defined as a set of journeys; later releases are named. Artifact: `product.md`; `traceability.yaml` starts with the journeys.

**Stage 3, specify.** Requirements with IDs (`REQ-001..`), each traced to at least one journey or to a cross-cutting concern; non-functional requirements with IDs (`NFR-..`) for scale, availability, latency, security, compliance, tenancy, data retention and residency, each with a measurable target or an explicit "no target yet, owner"; domain objects and interfaces; security and tenancy boundaries; failure modes and recovery; observability; open questions closed by answer or ruling. Artifact: `SPEC.md`; `traceability.yaml` gains requirements.

**Stage 4, architect.** The agent proposes two or three candidate architectures with trade-offs and a recommendation; the user picks. Then: components with IDs (`C1..`) and boundaries, module placement, data flow, deployment shape per environment, cross-cutting concerns (authn/z, tenancy, observability, data lifecycle, configuration, secrets), technology choices each recorded as an ADR in `program/adr/` (context, options, decision, consequences), and `gates.yaml`: the named gates (`G-..`) that will prove quality claims, each with the evidence it requires and the environment it needs. Artifacts: `ARCHITECTURE.md`, `adr/*`, `gates.yaml`; `traceability.yaml` gains components and gates.

**Stage 5, freeze review.** An independent spec-QA agent, never the author, reviews stages 1 to 4 together against `kit/briefs/spec_qa.md`: placeholders, contradictions, ambiguity, every journey covered by requirements, every requirement owned by a component, every quality claim owned by a gate, every non-functional requirement with a target or an owner, every environment claim consistent with `environments.yaml`, surface checklist complete. It returns a finding-to-fix table in `program/freeze/review.md`; the author fixes, the reviewer re-checks only the findings. `pk trace validate --stage 5` must pass. Every assumption in the ledger must by now be confirmed by the user or converted to a decision; none survives the freeze unconfirmed. Each lens records its sign-off, and the user signs `program/freeze/signoff.md`. Approval 5 freezes the spec; from here on changes are change requests. The answers of the stage 6 interview are the one thing still added to the ledger (see "The ledger after the freeze" above).

**Stage 6, roadmap and register.** `roadmap.md` shapes phases by capability: the first phase is a walking skeleton (one thin end-to-end journey through every component, deployed to every existing environment), then vertical slices by journey, with gates attached to phase ends. `register.yaml` then holds phases → milestones → steps → tasks with stable IDs (`P1-M02-S01-T03`), each task with a one-paragraph text, agent role from `roles.yaml`, size (S, M, L with the kit's definitions), environments, dependencies, target gate, and the requirement and journey IDs it closes. `pk register validate` checks: unique IDs and ID shape, dependencies exist, no cycles, role exists in `roles.yaml`, size S or M (size L is refused: split it into steps), fan-in limit per task, well-formed `closes` IDs, and no task that needs an environment marked non-existent unless it is explicitly `blocked_by` that environment. `pk trace validate --stage 6` checks that every first-release requirement is closed by at least one task and that every ID a task closes exists. A task's gate is optional (`null` is allowed), and one task being one PR by one role is a convention the controller applies when it reviews the register, not something a script checks. `pk briefs generate` writes the brief tree, and `pk briefs check` must report nothing before approval 6. Approval 6 freezes the register; later changes go through `pk register change --note TEXT`, which logs the change in `program/changes/register-log.md` and regenerates the briefs.

**Stage 7, task specs, just in time.** Stage 7 runs once per task and is never approved as a whole: after approval 6 `pk interview next` names stage 7 and stays there, which means discovery is complete. A task gets its `SPEC.md` and `PLAN.md` when it reaches the front of the queue, not before, so specs reflect what was learned from earlier tasks. The SPEC fixes scope in/out, dependencies, consumed contracts from the architecture, security and tenancy, failure modes, observability, module placement and resolved questions, and lists the requirement IDs it closes; it may carry `Guarded edit: <path>` lines, the user's approval for the task to change a guarded file. The PLAN fixes the PR boundary, work breakdown, exact commands (each line starts `$ `, with an optional ` # expect N`), definition of done, gate evidence and rollback. A spec-QA agent, never the author, reviews both against the register text, the architecture and the traceability matrix with a finding-to-fix table; the task is dispatched only on acceptance. The SPEC, the PLAN and the task's `in_progress` status are then committed to main together (the dispatch commit, section 5), because the gate reads them from main.

### 4.3 Status state machine

`pk status set` (the only writer of `program/status/<ID>.json`) allows only these transitions, by role:
- `not_started → in_progress` (controller, at dispatch)
- `in_progress → review` (implementer, after the hand-back gate prints its OK line)
- `review → in_progress` (QA, with findings)
- `review → done` (QA only: the task's latest acceptance record accepts, `decision: accepted_development`, names the task and binds the task's latest results record by digest; that record passes the gate's results checks and its `head` is an ancestor of HEAD)
- `done → review` or, for QA rejecting, `done → in_progress`, only through `pk status reopen <ID> --reason TEXT [--role qa [--reject]]` (controller or QA): the way back for late work after acceptance or a QA rejection. The reason is kept on the history entry as the audit line; it is refused once main holds the task as done, reopen changes only the status and appends no record, so the next `done` needs what `done` always needs (an acceptance binding the latest results record); late work that changed the task's inputs needs a new results record, and so a new acceptance, first
- `not_started|in_progress → blocked` (controller, with `blocked_by`) and `blocked → not_started|in_progress`
- `not_started → parked` (controller, with the user decision it waits on) and `parked → not_started`
- `in_progress → merged` (controller, light tier only, in the main checkout, after the task's branch is merged into main)

Every record carries the task's verification tier (`full`, `standard`, `light`), whether it runs on the fast path, and the ruling that allowed either (section 10.1). They are set at dispatch and change only through `pk status tier <ID> <tier> --ruling R<n>`; a record written before 0.3.0 has none and reads as `full`. The fast path is refused for the full tier, here and by the record reader. A light-tier task has no QA, so `review` and `done` are refused for it: `merged` is its terminal status, it carries no acceptance, and a gate claim never counts it as evidenced. Work outside the register (an ad-hoc task `AH-<nnnn>` listed in the controller state) can be given a status like a register task.

`done` and `merged` are terminal for `pk status set`, and `done` once it is on main; a later change request creates a new task. Both satisfy a `depends_on`, and `pk trace impact` and `pk change impact` list `merged` tasks in a bucket of their own, never as not started. `pk status validate` re-derives both: a `done` from the task's acceptance, a `merged` from the merge commit of the inspected commit the record names. A task with no status file is `not_started`. Every transition is checked in full before anything is written, so a refusal leaves no file behind.
## 5. Execution loop (playbook 08)

**Roles.** Three roles, each a separate agent session:
- The **controller** picks the next task, dispatches it, answers questions with recorded rulings, merges, and keeps the state current. It never writes product code.
- An **implementer** owns one task in its own worktree and branch, builds test-first against SPEC and PLAN.
- **QA** never wrote the code. It decides acceptance with evidence. Only QA, through `pk status set ... done --role qa`, can mark a task done.

The steps below are the full tier, which is the default. Section 10.1 says what the standard and light tiers leave out and what guards each of them; section 10.2 covers parallel review and the integration round. A reviewer works in its own detached worktree at the reviewed commit (`pk worktree review <ID>`) and is handed the diff as a file.

**Per task.**
1. **Dispatch.** The gate reads the task's SPEC, its PLAN, the `Guarded edit:` lines and `checks.yaml` from main, so they must be on main before work starts. The controller sets the task `in_progress`, commits the SPEC, the PLAN and that status file to main together as one commit (the dispatch commit), and only then runs `pk worktree new <ID>`, which creates `.worktrees/<id>` on branch `task/<ID>` from main (the project must git-ignore `.worktrees/`, or every worktree makes main look dirty). The implementer receives its role brief, the task brief, the SPEC, the PLAN, the rules and the rulings the SPEC cites. From here on the status record changes on the task branch, with two exceptions the controller makes on main: a tier change (`pk status tier`) and a light task's `merged` record, which `pk merge` writes (section 10.1).
2. **Build.** Test-first. The implementer decides anything with precedent itself and records the decision. It asks the controller only for a new dependency, a security boundary without precedent, a change to user scope, or conflicting rulings. The controller answers with a numbered ruling. A branch never edits its own SPEC or PLAN, and edits a guarded file only when the SPEC on main carries a matching `Guarded edit:` line.
3. **Evidence producer.** The implementer writes `program/evidence/<ID>/produce.py` with `kit/scripts/evidence.py`: literal PLAN commands, parsed derived fields, complete input hashes (inputs only, never a derived output) and the planted-bug table.
4. **Hand-back gate.** `pk gate handback <ID> --branch task/<ID>`, run from the main checkout, runs in order: the guarded-file check; the check that the branch did not change its own SPEC or PLAN or the spec-QA `review.md` of its dispatch (the SPEC and the PLAN must be byte-identical to main's; the review record must be as main has it, so a branch can neither rewrite nor remove it, nor add one main lacks, and main must hold the task's spec-QA `review.md` at all: a task dispatched without one is refused with `FAILED: review <path> is missing on main`); a trial merge of the branch into main in a temporary worktree; the project's build and compile checks on the trial merge; the project's test, scan and extra gate commands from `checks.yaml`; the PLAN's exact commands; `produce.py` (a task whose own inputs changed since its latest record must append a new one; the record history must be intact since the branch left main); the drift report. The rules are always read from `main`; no option names another branch. It prints a final `OK: <branch> passes the hand-back gate at <sha>` line only if every step passed. Exit code alone is not a pass; the line is. Failing the gate returns the work to the implementer without a QA round.
5. **Evidence commit.** The results record, produced only from a clean tree and appended to `program/evidence/<ID>/results/`, binds the work to one commit: head and main SHAs, the sha256 of every consumed input (source, tests, fixtures, dependency closure, config, lock files, tool helpers), every command with its literal text, real exit code, parsed derived fields and `as_expected`, and the planted-bug table. The gate leaves the new record and the updated pointer uncommitted. The implementer then sets the task `review`, writes its report to `program/evidence/<ID>/report.md`, and makes one commit of the record, the pointer, the report and the status file. That is the evidence commit.
6. **QA.** An independent agent runs `pk gate binding <ID> --branch task/<ID>` (the latest record is committed with the digest its pointer names, its `head` is an ancestor of the tip, every recorded input hash is recomputed from the blob at that commit, no record was rewritten since the branch left main; drift to the tip is reported, never a failure), re-runs the PLAN commands, tries to defeat each new guard with its own planted bugs, checks scope in/out against the SPEC, the closed requirement IDs against the traceability matrix, and claims against `CLAIM_BOUNDARIES.md`. QA does not re-run the hand-back gate; the merge check in step 8 is the independent gate run. It either appends an acceptance with `pk evidence accept <ID> <file>` (it must bind the latest results record and its head, with QA's own commands and bugs), sets status done and commits the acceptance record, the pointer and the status as one commit, or rejects with numbered findings, each with severity, location and the exact fix expected.
7. **Fix rounds.** Fixes go back to the same implementer, resumed in the same session, and the re-check to the same reviewer. It covers only the findings and the binding check (the implementer re-runs the gate after a fix). Each run is recorded in the cost log; an agent past 400,000 tokens or three resumes is flagged, and the controller starts a fresh one with a tight fix brief.
8. **Land.** `pk merge <ID>`, from the main checkout, is the whole per-merge chain for one task. It refuses a branch QA has not accepted: the status record on its tip must say `done`, the evidence committed at the tip must hold what `pk status set <ID> done` requires (derived there, in a temporary worktree, never read from the word in the file), and after the reviewed commit, the `head` of the accepted results record, the branch may have added only the task's own evidence and status files and clean merges of main. Then it runs the merge check and requires its last line `OK: <branch> merges into main at <sha>`, merges with `git merge --no-ff`, rewrites the claim documents of the gates that name the task, regenerates the hand-off block and commits exactly the bookkeeping files it wrote. The merge check is the controller's own run of the gate's checks and does not rely on the implementer having run it: the guarded-file check, the tier check, then on a trial merge the SPEC, PLAN and `review.md` identity check and every build, compile-check, test, scan and extra-gate command of main's `checks.yaml`. Merges are one at a time: the command takes one task.
9. **After the merge.** `pk worktree finish <ID>` removes the task's worktrees and merged branch (section 10.2); the controller triages follow-ups, spot-checks what the reports claimed, and records decisions and state changes with `pk record` (section 10.3).

**Blocked and parked.** A task not yet dispatched that needs an environment which does not exist is set `blocked` on main with `blocked_by` naming it. A task that waits on a user decision is `parked` with the decision named; parked is reachable only from `not_started`. A task already in progress that needs a user decision is set `blocked` on its own branch.

**Gate claims.** `gates.yaml` names, per gate, the evidence records it aggregates. `pk gate claims <GATE>` regenerates `program/claims/<GATE>/*.json`, one per record, each stating per environment whether the claim is evidenced, blocked or not evaluated, computed only from each task's latest acceptance and results records (evidenced only when the latest acceptance accepts and binds the latest results record) and `environments.yaml`. A claim file is rewritten only when something other than the commit it names would change, so committed claims stay clean, and a gate with no records is refused. `pk gate validate <GATE>` fails on any drift between the claim docs and a recomputation. Nothing ever upgrades a development claim to a non-existent environment.

**Planted bugs.** A planted bug counts as caught only when a test fails in code that builds (or, for non-compiled artifacts, that loads). The planted-bug runner therefore needs a build command, records `builds` and `kill_reason` per row together with the file, the edit and the build and test commands, and always includes one deliberately non-building row recorded as not caught, so a runner that counts build failures as catches is visible.

**Pre-existing failures.** A PLAN command that fails identically on clean main is recorded as a named environmental deviation: real exit, one reproduction on clean main with its log hash, proof the task's inputs to it are unchanged, the failure message asserted so a silent pass or a different failure flips the row, nothing claimed from it, and an owner follow-up. Never a pass.

**What the gate cannot see.** A `produce.py` that hand-writes a well-formed results record cannot be detected by the gate, and files written to git-ignored paths are invisible to the clean-tree checks. That is why QA re-runs the evidence independently and checks the binding rather than trusting the record, and why a project must git-ignore its bytecode and build output (a leftover untracked file fails the clean-tree check, so the omission is loud, not silent).
## 6. Change requests after the freeze (playbook 10)

A change of meaning to anything approved at stage 5 or later is a change request `program/changes/CR-NNN.md`: what changes, why, who asked. `pk change create` writes the request; `pk change impact CR-NNN <IDs>` lists through the traceability matrix every requirement, component, gate and task touched, split into done tasks (whose acceptance would no longer cover the new meaning), in-flight tasks and not-started tasks. The user approves the CR with that impact in view. Then: artifacts are amended with the CR number in their change log, `pk trace validate --stage 6` must pass again, affected done tasks get a new follow-up task in the register, affected in-flight tasks get a ruling delivered to their implementer, and `pk change apply --note TEXT` writes the one register-log line for the edit, regenerates the briefs and marks the request applied. Before it writes anything `pk change apply` checks that the register is valid, that the matrix's `tasks` section is what `pk trace sync` would write from it (the same guard as approval 6), that the stage 6 matrix check passes and that the briefs render, so a follow-up task added without a sync, or a change that leaves a first-release requirement unclosed, is refused with no log line, no regenerated brief and the request untouched; a write that fails part-way is rolled back. `pk change apply` also refuses until `pk change approve` has recorded the user's approval. A CR never edits a done task's evidence or acceptance.

## 7. Hand-off and takeover (playbook 09)

`program/HANDOFF.md` is the single document a successor needs. It carries the state of the program, not its rules and not its history, and it has a size budget so that it can always be read whole. It has a maintained part and a generated part:
- maintained: how the program works, environment setup, known hazards (seeded with the four every program meets), the order of what comes next (2 KB at most), and one line saying where the rules live;
- generated by `pk handoff update`, between markers: a Now list (the controller's one line, every task in flight with its tier and stage text, the open ad-hoc tasks), counts by status, unmerged branches and worktrees, queued and parked tasks, the newest ten closed ad-hoc tasks, the open follow-ups that are not deferred by a tag (newest first, counted, at most thirty), open change requests, the agents past the fresh-agent threshold, and the newest fifteen rulings on one line each with the next free number.

`pk handoff check` exits 1 when the generated part is stale, when the file is over its budget (60 KB unless the controller state sets `handoff_budget_bytes`, which `pk record --handoff-budget` writes by a ruling and the generated block then states) or when the Next steps section is over 2 KB. History lives in `program/rulings.md`. Writing the block changes `program/HANDOFF.md` and no other tracked file, and nothing when the block is current.

`kit/playbooks/09_handoff.md` holds a table of what each role reads at start, with sizes that a test checks against the shipped files, and the successor prompt: read HANDOFF, the rules file, the controller brief and `program/COMMON.md`; verify the state (git status, unmerged branches, worktree cleanliness, void partial evidence by exact path, leftover processes and containers, disk, and that the repo-wide suite passes on main); report to the user what was found and what was verified; resume at the Now list. The prompt points to the rules; it does not restate them.

**Model classes.** Roles, never names: the kit names no model. Controller rule C4 defines four classes (A for the final review of full-tier work and security rulings, B for the controller and specs, C for implementation and scoped review, D for mechanical work); `program/COMMON.md` maps each class to a model by name, every dispatch names its model, and the cost log refuses a run without one.

## 8. Hard rules (kit/rules/RULES.md, part 1)

The rules are stated once, in `kit/rules/RULES.md`, and this document does not restate them. Part 1 holds the hard rules, numbered 1 to 23, which bind every agent in every role and stage: evidence and claims (1 to 5), status, register and frozen artifacts (6 to 10), git and files (11 to 17), permissions and scope (18 to 20), and merges, worktrees and rulings (21 to 23: merges one at a time, never force-remove a worktree that holds work, a ruling is never rewritten). If a rule seems to block the work, the agent stops and tells the user. Rules 1 to 20 are word for word those of 0.2.0; 21 to 23 were stated in the playbooks before and are numbered now.

## 9. Evidence honesty (kit/rules/RULES.md, part 3) and the evidence kit

### 9.1 Rules

Part 3 of `kit/rules/RULES.md` holds the twelve evidence honesty rules, word for word those of 0.2.0: literal commands, parsed derived fields, a cheapest-defeat planted bug per guard that counts only on a test failure, cross-boundary tests for access policies, complete input hashes, a clean tree bound to one commit, voided partial evidence, honest refreshes, no inline downloads, and (rules 11 and 12) committed, append-only, immutable records that bind only their inputs. The rest of this section describes the machinery that enforces them. Verification tiers (section 10.1) change how much evidence a task owes, never what a record is: a standard-tier task produces the same record through the same gate, and a light-tier task produces none and can never be `done`.

### 9.2 The evidence store and evidence.py

**Why it changed in 0.2.0.** In 0.1 each task kept one `results.json` and one `acceptance.json`, both bound by hash to everything, including their own outputs and other records' acceptances. That made the evidence graph cyclic (a record went stale the moment it was accepted) and turned every merge into a re-bind batch over hundreds of committed files. Evidence is a development fingerprint, not a product feature: it should be immutable and append-only, the latest record per task is the one the program considers, the history stays available, and a record binds only its inputs.

**Layout.** Records are committed with the project, next to the task's producer:

```
program/evidence/<ID>/latest.json                                 the task's pointer (the only mutable evidence file)
program/evidence/<ID>/produce.py                                  the producer (source)
program/evidence/<ID>/results/<utc-compact>-<sha256[:12]>.json    results records
program/evidence/<ID>/acceptance/<utc-compact>-<sha256[:12]>.json acceptance records
```

`<utc-compact>` is the UTC append time `YYYYMMDDTHHMMSSffffffZ`, so names sort in append order; the digest in the name is the sha256 of the file's bytes. The kit creates a record once, read-only, through a temporary file hard-linked to its final name, so an existing record is never replaced and a half-written one never carries a record's name. There is nothing to initialise: a task's pointer appears with its first record (`pk evidence init` says so and creates nothing).

**Input digests.** A results record's `inputs` holds the sha256 of each input's blob at `head`. Since 0.4.1 it is written as a list of `{"path": ..., "sha256": ...}` objects sorted by path; 0.4.0 and earlier wrote a mapping `{"<path>": "<sha256>"}`, which put a path such as `src/auth/token.py` and a long hex value on one JSON line, the pattern a secret scanner's generic-key rule matches, so a committed record failed every later scan. A record is never rewritten, so every reader (`pk evidence validate`, `accept` and `lint`, the hand-back gate, `pk gate binding`, `pk merge`, `pk stale`, `pk status set ... done` and gate records' child rows) reads both shapes through one function, `store.input_digests`. The shape itself says which it is; records carry no schema version of their own (the pointer's `schema_version` is unchanged).

**The pointer.** Each task's `program/evidence/<ID>/latest.json` names its latest results record (digest, path, head, produced_at) and its latest acceptance record (digest, path, decision, accepted_at, and the results digest it binds). An append replaces only its own task's pointer, atomically. A task branch therefore edits only files under its own `program/evidence/<ID>/`, so two branches never conflict on evidence when they merge. Every check reads the latest record through the pointer: `pk stale`, `pk gate handback`, `pk gate binding`, `pk gate claims` and `pk status set ... done`.

**Latest wins, history kept.** A refresh appends a new record that names the previous one in `supersedes`; the previous one stays. A re-acceptance appends a new acceptance. `pk evidence history <ID>` lists every record of a task, newest first, with digest, kind, head, time, decision and whether it is the current one.

**A record never becomes stale.** A results record claims "at commit `head` these inputs, with these digests, produced this outcome". That stays true forever, so a later change never invalidates it: it only makes the record drift from the current tree. Drift has one reference everywhere: the record's input digests against the tree at the checkout's HEAD (in `pk stale`, the gate's drift step, `pk gate binding`, where the checkout is the branch tip, and gate records' child rows). A record carries no stale field. `pk stale` is a drift report: per task, `fresh` (every recorded input digest equals the committed blob at HEAD) or `drifted` since its head, with the changed inputs. It exits 0, or 1 with `--strict` when a record drifted or cannot be read. The latest record is what the program considers; the history shows every earlier claim. The hand-back gate asks a task for a new record only when the task's own source changed: an input recorded by the task's latest record changed between that record's head and HEAD. It never asks other tasks to refresh because a shared input changed. `pk gate binding` reports drift between the record's head and the branch tip and never fails on it. `done` needs only that the latest acceptance binds the latest results record and that the record's head is an ancestor of HEAD. A gate record binds its child records by digest and records each child's drift (`fresh` or `drifted`) in its own output, for the reader.

**Immutability through git.** `pk evidence validate` with no record (run by the hand-back gate over the branch's range and by `pk doctor` over the whole history) checks that every record file's sha256 matches the digest in its name ("immutable record altered"), that no commit modified or deleted a record file (`git log -m --diff-filter=MD` over the record directories, merge commits included, since every task reaches main by a merge: "evidence history rewritten"), and that every pointer entry names the newest record of its kind and resolves to a record committed in HEAD's tree with its digest. A pointed-at record that is present with its digest but not committed yet (between the gate and the evidence commit) is a warning, `record pending commit: <path>`, with exit 0; only a missing or altered record fails. The check that each record also parses as a results or acceptance record of the kit's schema runs on `pk merge` and on `pk evidence validate` only; the hand-back gate and `pk doctor` check names, history and pointers but not the schema.

**Acyclic closure.** A results record's inputs may name only what can change its outcome (rule 6). `pk evidence lint <record>` reports `binds a derived output: <path>` for any input under `program/evidence/` other than the producer and its `*.py`, `*.sh` and `*.toml` tooling, any `results.json` or `acceptance.json`, anything under `program/claims/`, `program/status/` or `program/approvals/`, and the status snapshots and logs (`program/controller-state.json`, `progress.md`, `HANDOFF.md`, `changes/register-log.md`). The interview's requirements ledger `program/ledger.yaml` is an input, not a derived output. `Evidence.write` refuses such a record and the gate, the binding check and `done` run the same lint. Only a gate record (its task is a gate, `G-<NAME>`) carries `children`, each `{task_id, kind, digest, drift}`; a child that is itself a gate is refused.

**evidence.py.** Standard-library Python. `Evidence(root, task_id)` checks the tree is clean, records head, merge-base and main, and `hash_inputs` hashes inputs through `git cat-file` so the hash is of the committed blob, never the working copy. `run_command(label, argv, expect, ...)` records the literal text, exit code, parsed fields and `as_expected`. `planted_bugs(table, build, test)` applies each edit to a throwaway mirror of HEAD, builds, runs the named test, records `builds` and `kill_reason` with the file, the edit and the build and test argv, restores the mirror between rows, clears caches, and appends the non-building self-test row; it needs a build command, because a row cannot be judged "builds" without one. `bind_children(task_ids)` (gate records only) binds the children's latest records. `write()` refuses to append unless the clean-tree guard, the self-test row, `all_commands_as_expected` and the lint hold, then appends the record and points the task's pointer at it. `pk evidence validate <record>` re-checks a record against `kit/templates/results.schema.json` (or `acceptance.schema.json` for an acceptance record) and its invariants, and its name. `pk evidence accept <ID> <file>` appends QA's acceptance only when it binds the latest results record and its head. `checks.yaml` supplies the project's build, test and scan commands; the kit never assumes a language.

**Migrating a 0.1 project.** `pk evidence import` copies each `program/evidence/<ID>/results.json` and `acceptance.json` byte for byte into the new layout as the first history entry (so an acceptance's `evidence_sha256` still binds) and writes each task's pointer. It never deletes the originals; it prints the exact `git rm` command for each. A `gates.yaml` record may keep its 0.1 path `program/evidence/<ID>/results.json` (it names task `<ID>`) or say `task: <ID>`.

Known limits, stated so nobody relies on more than is there: a record edited consistently by hand, appended under its new digest and pointed at, still validates; a history rewritten with the records in it defeats the git check; records are not signed (signing is a later addition); files written to git-ignored paths are invisible to the clean-tree check. Re-running the record's commands is the reviewer's job.

### 9.3 checks.yaml
```yaml
build:
  - - go
    - build
    - ./...
test:
  - - go
    - test
    - ./...
scan:
  - - gosec
    - ./...
  - - gitleaks
    - detect
    - '--no-git'
compile_check:
  - - go
    - vet
    - ./...
extra_gate: []            # project-specific gate commands, run in order
parallel_steps: false     # true runs test, scan and extra_gate concurrently
guarded_files:
  - go.mod
  - go.sum
  - program/checks.yaml
```
Presets fill these for Go, Node/TypeScript and Python. `parallel_steps` is false by default and a value that is not a boolean is refused by name. When true, in the hand-back gate and in the merge check, `build` and `compile_check` still run first, in order; then `test`, `scan` and `extra_gate` run concurrently, one thread each, the commands of a step still one after another and each step's output captured; when all three have finished the outputs are printed in the fixed order test, scan, extra_gate and the first failing step in that order decides the exit, so the printed result is the one the serial run prints (a later step may have run although an earlier one failed, and prints nothing). The `plan` commands, the evidence step and the drift step stay serial after them, and the light tier is unchanged. Keep it false unless the test, scan and extra-gate commands can share one tree at once: a test that writes into the tree while a scanner reads it is a reason to leave it off. Commands are argv lists; `pk gate` runs them without a shell. `build`, `test` and `scan` must each list a command. A guarded file is an exact path, or a directory when the entry ends in a slash. The gate reads `checks.yaml` from main, never from the task branch or a dirty checkout, so a branch cannot change the rules it is judged by. Unclaimable environments come from `environments.yaml`, not from here. `pk doctor` applies the same rules to a project's `checks.yaml` and reports each problem on its own line, and runs the evidence integrity checks of section 9.2.
### 9.4 The release fingerprint

**What it claims.** A results record binds one task's inputs and commands at one commit. It cannot say "the whole release set passed at commit X", and answering that today means re-running every task's commands. A release fingerprint is one committed record per commit that runs the project's release set on parallel tracks and derives a per-test table from the output. It claims exactly: at `head`, these commands ran with these exit codes and these test verdicts. It claims nothing about any other commit; the readers say how far the tree has moved since.

**The release set.** `program/release-set.yaml` (block-style YAML in the kit's subset; `kit/templates/release-set.yaml` is the commented example and `pk init` copies it) lists `entries`, each with an `id` (`[a-z0-9-]+`, unique), the literal `argv` (never a shell string), `expected_exit` (default 0), a `track` (one thread per distinct track), a `kind` (`check`, `test` or `gate`), `derives` (`null`, `unittest` or `go-tests`: the table the output is parsed into) and an optional `env` that is set for that command only and whose values are never recorded; `toolchain` lists argv lists whose first output line is recorded as the toolchain version. Anything else is refused by name. It is read from a git ref, never from the working copy, and it is a guarded file, so a task branch cannot change what the fingerprint runs.

**Layout.** `program/evidence/fingerprint/<head12>/fingerprint.json` and one `<table>.json` per derived table, checked against `kit/templates/fingerprint.schema.json` (`"schema": "program-kit.fingerprint.v1"`). The record keeps, per command, the exit, whether it was as expected, the seconds, the `output_sha256` and the last 20 lines of output; full output goes to a scratch directory, never into the record. It also keeps the sha256 of the release set, of `program/checks.yaml` and of `kit/MANIFEST.sha256` at `head`, and the toolchain lines. No absolute path, no environment value and no output body appears in it.

**The producer.** `pk fingerprint run [--only TRACK] [--scratch DIR]` needs a clean tree and refuses when the directory for HEAD is already tracked ("a record is never rewritten"); an untracked directory of that name from an earlier uncommitted run is removed and rewritten. It runs every entry at HEAD from the repository root, one thread per track and the entries of a track in release-set order, with the gate's per-command timeout; a failing command does not stop its track. `unittest` tables are parsed from the verbose runner's lines and `go-tests` tables from `go test -json` events. A command that exited 0 but parsed to no entries is recorded as not as expected ("no test entries parsed"): a vacuous pass is not a pass. Tables from several commands merge: `skipped` yields to any other verdict, two different non-skipped verdicts for one name are a `table_conflicts` entry and make the record incomplete, and the summary key takes the worst verdict. After the run the tree must be clean again (`tree_unchanged_by_run`), otherwise the record says so and the command exits 1. `complete` means every entry ran (no `--only`), the tree was unchanged and there are no conflicts; `all_as_expected` means every command was. It exits 0 only when both hold. `pk fingerprint plan` prints the entries by track with the seconds the current fingerprint recorded for each, and runs nothing. `pk fingerprint validate [PATH]` checks the schema, each table file's digest, the release-set digest at `head`, that no absolute path appears and that `head12` is the directory name; the history check behind `pk evidence validate` and `pk doctor` also covers `program/evidence/fingerprint/`, so a commit that modified or deleted a tracked fingerprint file is refused like a modified task record.

**What the record does not promise.** Test verdicts are what the runner's output says: a command can print a line shaped like a result. Output tails keep the values of inherited environment variables as they were printed; only values the release set sets itself (4 or more characters) are replaced. Paths are replaced for `/Users`, `/home`, `/private`, `/tmp`, the repository and scratch directories and drive letters; other shapes (`/root`, `/opt`, `/Volumes`) are kept. `pk fingerprint validate` also compares the command rows with the release set at `head` and refuses a release-set env value found in the free text of the record.

**The readers.** The current fingerprint is the record tracked at a ref whose `head` is that ref or an ancestor of it, the nearest one winning and the latest `produced_at` breaking a tie. `pk fingerprint show [--json]` reports it, how many commits and paths the tree has moved since, the seconds per track and the commands that were not as expected; it always exits 0, as information, like `pk stale`. `pk fingerprint coverage [ID]` classifies each row of a task's latest results record: `exact` (same argv and expected exit as an entry that was as expected), `derived-unittest` or `derived-go` (a named-test or package-level command whose tests are in the derived table as passed), or `not_covered` with one reason (no fingerprint, incomplete fingerprint, ad-hoc command, expected non-zero exit, env override, all matched entries skipped, package not in the table, build-changing flag). `pk stale` adds a `fingerprint` field to each task's entry and `pk handoff update` adds one Now line, `Fingerprint: <head12>, complete, N of M as expected, K commits behind` or `Fingerprint: none`. Coverage is computed on read and never written.

**When the controller runs it.** After a merge that changes the release set or a suite it names, and before a release. The record is committed only when it is complete and every command was as expected, in its own commit made with plain `git add` and `git commit` of exactly that record directory (`pk record` commits only the files it writes, so it is not used here). A red run is not committed: keep the directory uncommitted, fix, and run again at the new commit. There is no run store: the kit never re-binds, QA runs fresh by design, and the merge check runs on a trial-merge commit, so a stored result would never match.

**What it is not.** It does not replace the gates or QA's re-run, it is not signed, and it is not a statement about any commit but its own `head`.

## 10. Working rules and the controller's rules (kit/rules/RULES.md, parts 2 and 4)

Rework and token cost, not coding, are what slow a program down. Part 2 of the rules file holds the working rules every agent follows (W1 to W12: decide don't ask, the OK line, fix rounds, your own folder and worktree, no polling, the return contract, read only what you are pointed at, interruptions, decided means decided, work to your tier, report the cost, fast default with the slow set in the release set). Part 4 holds the controller's rules (C1 to C20). This section explains the three mechanisms behind them; the rules themselves are not repeated here.

### 10.1 Verification tiers and the fast path

One process for every task spent the most on the tasks that needed it least. A task therefore has a verification tier, chosen when it is specified (the `Tier:` line of its SPEC, checked by spec QA), recorded in its status record at dispatch, and changed only by a ruling.

| Tier | For | Process | Ends at |
|---|---|---|---|
| `full` | access control, tenancy, data migrations, cryptography, release, the kit's own gates; the default when unsure | section 5 as written: spec QA, hand-back gate, QA that re-runs the commands, plants its own bugs and works through the attack questions, merge check | `done`, set by QA |
| `standard` | ordinary work off the full-tier paths | the same gate and the same evidence record; one independent scoped review (binding, diff against the definition) with no re-run and no planted-bug quota; the suite runs at the hand-back gate and at the merge check | `done`, set by QA |
| `light` | the kinds of change a ruling of the program names | build, compile check and scans; the controller reads the diff; no tests, no evidence, no QA | `merged`, set by the controller |

The fast path replaces SPEC, PLAN and spec QA with one `program/tasks/<ID>/BRIEF.md` and a ruling, for small bounded work the user asks for directly. It is never available for the full tier.

Each lighter path has a guard that a script enforces, not a sentence:
- **The tier is read from main.** The gates take tier, fast path and ruling from `main:program/status/<ID>.json`. A branch whose own status record says otherwise fails (`FAILED: tier ... changes the tier`). A record that does not read is an error, never a lighter tier.
- **Lower tiers are off until switched on.** `pk status set --tier standard|light` and both gates refuse a lower tier while `tiers.full_paths` in main's `checks.yaml` is empty. `checks.yaml` is a guarded file, so a branch cannot switch a tier on for itself.
- **Full-tier paths stay full.** A task below the full tier fails both gates when its branch changes a path under `tiers.full_paths` or one of the built-in full-tier paths (`kit/`, `program/checks.yaml`, `program/gates.yaml`).
- **The definition and the record agree.** A SPEC-path task below full must carry `Tier: <tier>` in its SPEC on main, equal to its record. A fast brief must carry one `Tier:` and one `Ruling:` line, each equal to the record. `pk status validate` holds the record's tier to its own history: the dispatch entry names the tier, every change appends an entry with its ruling, and a tier edited by hand no longer matches.
- **The fast path is never full.** Refused by `pk status set`, by `pk status tier` (unless `--spec` says the SPEC, PLAN and review now exist), by the status record reader, and by both gates. The brief must be on main, byte-identical on the branch, with its Scope, Assumptions checked, Decided and Stop sections; its ruling must be in main's rulings log; a standard-tier brief must list at least one exact command. A fast-path task cannot change a guarded file, because the approval line lives in a SPEC.
- **Light is bounded.** It needs `tiers.light_ruling` (a ruling on main) and `tiers.light_paths`; a light branch that changes any path outside them fails. The merge check needs `--inspected <sha>` equal to the branch tip. `review` and `done` are refused: a light task ends at `merged`, set by the controller on main only after the branch is merged, and a claim document never counts it. The record stores the inspected commit, and `merged` is checked like `done`, when it is set and by `pk status validate`: main's first-parent history must hold a merge of exactly that commit, the task must have been light on main when it landed, and the light path's guard is run again on what the merge brought in. A hand merge round a failed merge check is never recorded `merged`.
- **Nothing unaccepted merges through the kit.** `pk merge` refuses a full or standard branch whose status record on its tip is not `done`, one whose committed evidence does not accept it (the word `done` typed into the file is not an acceptance), and one that changed anything but its own evidence and status files after the reviewed commit, a merge commit that carries an edit of its own included.
- **The full tier's review is in the record.** For a full-tier task `done` needs the acceptance to list the commands QA re-ran and at least three planted bugs of its own. A standard-depth acceptance does not close a full-tier task. It is a statement in an immutable record: the script checks that it was made, not that it is true.

What the tiers cannot do: tell whether the `full_paths` list is complete. A program that leaves its access-control code out of the list has switched the guard off for it; spec QA reviews the tier of every SPEC for that reason.

### 10.2 Parallel work, review worktrees and integration rounds

Work is dispatched in parallel, up to three implementers and four agents; merges stay one at a time (hard rule 21).

- **Contract first.** Work with a server part and a client part has `program/tasks/<ID>/CONTRACT.md` written first and committed to main in the dispatch commit, where every worktree sees it; both parts build against it, the gates hold a branch's copy identical to main's, and changes are appended on main by the controller.
- **Review worktree.** `pk worktree review <ID>` creates a detached worktree at the reviewed commit and writes the diff to a file beside it, with evidence, status and excluded paths left out, and a stat file that names what was left out and lists every path the branch changed. Run again, it moves the clean review worktree to the new commit for a re-review. Reviews of different tasks run at once. The implementer stops when it hands back: `pk merge` refuses a branch that gained anything but its own evidence and status files after the reviewed commit. The acceptance is still committed on the task branch, and it still must bind the task's latest results record.
- **Integration round.** For accepted branches below the full tier: `pk integrate start NAME ID...` merges the members' tips onto `round/NAME`; `pk integrate check NAME` runs every command of main's `checks.yaml` once on the combined tree, records the tested tree and ends `OK: round/NAME passes the combined check at <sha>`; `pk merge ID --round NAME` lands each member, one at a time, without a second suite run; `pk integrate close NAME` compares the tested tree with main and fails on any difference outside the controller's documents until `--recheck` has run main's checks over the final tree. Guards: a full-tier task is never a member; a member must be `done` on its tip before the round starts; a member whose tip moved after the check, or a main that gained a commit that is neither a member's merge nor controller documents, voids the round for what is left; every member still passes the guarded-file, tier and definition checks and a trial merge when it lands; the round record is re-derived from git when a member lands and when the round closes (the branch `round/NAME` at the recorded head, the tested tree that commit's tree, one merge per member from the base), so a record written by hand covers nothing unless it describes `round/NAME` exactly; it cannot show that the combined check ran, and a correct tested tree typed into a started round's record is not detected (L11 is fixed in part; see the 0.3.0 Known limits). An open round is a line of the hand-off's Now list. A script cannot choose a safe subset of tests, so the re-check is the whole suite once; the listed paths tell the reviewer where to look.
- **Finish.** `pk worktree finish <ID>` removes the task and review worktrees, the merged branch, the symlinks it is told about and, given the task's compose project name, the containers and volumes that carry the label `com.docker.compose.project=<name>`, which the container tool matches exactly; nothing is chosen by a name prefix, and images are left alone. It refuses a worktree with uncommitted or untracked files, one that stands on a commit no branch holds, git-ignored files it was not told to drop, and an unmerged branch. Every check and both listings come before the first removal, and it never passes a force flag. A removal the container tool itself refuses stops the command there and says what went and what did not.

### 10.3 The controller's tracking commands

The controller state (`program/controller-state.json`) is written by commands, never by hand.

- **Rulings.** One append-only log; the hand-off shows the newest fifteen and the next free number.
- **Ad-hoc tasks** (`pk adhoc`): work outside the register is a record with an ID, a title, a tracking state (queued, progress, review, paused, closed), a note and the ruling that asked for it, shown in the hand-off like a register task. The tracking state ends at `closed`, never `done`, and `closed` is refused while the task's status record is in flight or blocked, or its branch is not merged. The closed list shows each task's status record beside it.
- **Follow-ups** (`pk followups`): stable IDs, an added date, tags; close with a reason and the ruling or commit that closed it; mark duplicates. A tagged follow-up is deferred: counted by tag, not listed. A follow-up is never deleted: the state counts the ids it issued and refuses to load when one is in neither list.
- **`pk record`**: one controller step in one command. It appends the ruling, applies the state changes given with it, regenerates the hand-off, checks the budget and commits exactly the files it wrote, with a subject taken from the ruling's first sentence. It is the writer of everything the hand-off says about the controller: `--now`, `--note <ID>=TEXT`, `--queued`, `--parked`, `--handoff-budget`. Everything that can be is validated before anything is written, a ruling rewritten in the working tree included (hard rule 23); a failed write or commit puts the three files back; the size budget is measured afterwards, so a step that goes over it is committed and then exits 1. It runs in the main checkout, with or without `--no-commit`, and refuses when anything is staged.
- **Cost log** (`pk cost`): one line per subagent run (task, role, agent, model, tokens, tool calls, seconds, resume). Totals per task; an agent past 400,000 tokens or three resumes is flagged in `pk cost report` and in the hand-off.

## 11. Rulings

`program/rulings.md` is append-only. `pk rulings add --role ... --question ... --decision ... --precedent ...` writes the next number (R1 onward), the date, the role that decided, the question settled, the decision and the precedent it generalises. A ruling that changes the process also changes the playbook or rule file in the same commit. A ruling never contradicts an earlier one silently; it supersedes it by name, and `pk rulings validate` checks numbering and supersedes links. `kit/rules/RULINGS_SEED.md` holds the starting set, numbered K1..K44, written from the originating program without project names. Where a seed ruling is one of the rules of `kit/rules/RULES.md` it names that rule and keeps only the reason and how to apply it; K34 to K44 record the 0.3.0 rules and supersede K1, K13, K18 and K21 by name, and a superseded ruling keeps its own words. A K ruling can be superseded in the same way as an R ruling.
## 12. Error handling

- Every script exits non-zero with a one-line reason on the first invalid input (`error: ...` on stderr); none writes partial output.  `pk register validate` prints `error: register invalid: <N> problem(s)` first, then each problem prefixed `error: `.
- `pk interview approve` refuses while a required topic is open, a lens holds a blocking question, a challenge finding is unresolved or the challenge report is untouched, a ledger entry is malformed or has no source, a stage below it is not approved and current, or (stage 6) the register fails validation or the matrix's `tasks` section is not what `pk trace sync` would write; `pk interview next` refuses to open a stage whose predecessor has no approval, or whose approved artifact hash no longer matches (it lists the changed files and names the amend or change-request path).
- `pk trace validate` fails on any journey without a requirement, requirement without a component or task, gate without a record, or task closing an unknown ID.
- `pk register validate` fails on duplicate IDs, missing dependencies, cycles, granularity violations and unclaimable environments without `blocked_by`.
- `pk status set` refuses a transition the state machine does not allow, a task the register does not list, and `done` unless the latest acceptance accepts and binds the latest results record, that record is valid evidence whose `head` is an ancestor of HEAD, and the acceptance's `source_commit` is that `head`.
- `pk gate` stops at the first failing step, prints the step and its last 20 lines, and never prints the OK line.
- `pk evidence validate` and the evidence writer refuse a dirty tree, a missing self-test row, a command not as expected, or a record that binds a derived output; `pk evidence validate` with no record, `pk gate handback` and `pk doctor` refuse an altered record, a rewritten record history, and a pointer entry that does not resolve to a committed record.
- `pk fetch` refuses to store a file whose digest does not match the pin; `pk fetch verify` refuses to download and names the fetch command instead.
- `pk change apply` refuses to apply a CR without the user's approval record, and refuses before writing anything while the register is invalid, the matrix's `tasks` section differs from the register or the stage 6 matrix check fails.
- `install.sh --upgrade` refuses if `kit/` does not match its manifest.
## 13. Testing the kit itself

- `kit/tests/` covers every script with standard-library unittest: coverage-map loading and conditional topics, ledger validation (kinds, sources, topic links), the stopping rule, challenge-report resolution, coverage reports, approval and amend, resume at the right stage, replay from a recorded ledger, ingest marking, traceability validation, register validation (duplicate IDs, cycles, unmapped requirements, granularity), ID assignment stability, brief generation determinism, status transitions, evidence writing guards, the record store (append-only naming, immutability, the per-task pointer, history, integrity, import), the derived-output lint, the planted-bug runner (including that a build failure is not a catch), the drift report, gate claims and drift, binding, ruling numbering, hand-off staleness, change-request impact, fetch guards, and the manifest.
- The shipped content is tested too: every playbook has its required sections, every command a playbook names exists, every playbook file a playbook names exists, and the seed prompts and charters match the coverage map.
- `kit/tests/test_neutrality.py` scans every file in the repository except version-control, worktree and cache directories. It fails on any forbidden vendor, model or project name (case-insensitively, wherever they occur), on any URL outside `docs/FAQ.md` and the one test file that needs loopback addresses, on a file that is not valid UTF-8, and on a zero-width or invisible character (soft hyphen, direction marks and the like). The names are matched as substrings, in the text of every file and in every file and directory name; the project name also matches with a space or underscore in place of its hyphen. The forbidden names live in `kit/tests/_tokens.py`, assembled from fragments, so no file contains one literally.
- `examples/tiny-service/` is a worked example with an end-to-end test.
- Every lighter path is tested by trying to get round its guard: `test_tiers.py` (a lower tier that is not switched on, a full-tier path touched below full, a branch that lowers its own tier, a fast path on the full tier, a light merge nobody inspected, a light task called done), `test_land.py` (an unaccepted branch, a round with a full-tier member, a member or a main that moved, a tested tree that no longer matches, a finish that would lose work) and `test_tracker.py` (a refused step that writes nothing, the hand-off budget, a generator that touches no other tracked file).
- `install.sh` is tested by installing into a temporary repository with each preset and running `pk doctor`.
- The suite follows the kit's own rule W12: a fast default and a slow set behind one environment variable. Run either with a private temporary directory so leftovers from other runs cannot confuse the leftover checks:
  - default: `TMPDIR=$(mktemp -d) python3 -W error -m unittest discover -s kit/tests` runs 1097 tests, 314 of them skipped as slow, in about 96 s (measured: 95.8 s);
  - everything: `KIT_TESTS_SLOW=1 TMPDIR=$(mktemp -d) python3 -W error -m unittest discover -s kit/tests` runs all 1097 in about 676 s (measured: 11 min 16 s).
  - The slow cases are the ones that build whole repositories, run the gate end to end, install the kit or spawn many processes. They are marked with `skip_unless_slow()` from `kit/tests/_slow.py`, and only these modules use it: `test_gate.py`, `test_land.py`, `test_end_to_end.py`, `test_evidence.py`, `test_tiers.py` and `test_install.py`. Each keeps fast cases that import and drive its entry point, so a broken import still fails the default run. A test in `test_content.py` fails when the set of modules importing `_slow` differs from this list.

## 14. Out of scope for the first version

A web UI or dashboard (the generated hand-off block is the kit's only view of the state); a process lighter than the light tier; hosted anything; model selection; language-specific project scaffolding beyond `checks.yaml` presets; migration of an existing non-kit program into the kit's layout; multi-repository programs.

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

The map holds 28 topics over stages 1, 2, 3, 4 and 6 (stage 5 is the freeze review and stage 7 is per task; neither has topics). Each topic has an ID, a title, an owning lens, contributing lenses, whether it is required, an optional condition, a closing criterion in words (`closes_when`) and a list of `needs` tags, one per clause of the criterion. A ledger entry carries the tags it settles; a topic closes when every tag is present on a fact, a decision or a confirmed assumption. A conditional topic applies only when its condition holds over the tags present in the ledger, taken from every kind of entry, an unanswered question and an unconfirmed assumption included (switching a topic on too early is the safe direction; for example `regulated`, `personal_data`, `payments`, `external_system`). Each stage also has a question budget.

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
