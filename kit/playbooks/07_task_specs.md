# Playbook 07: task specs, just in time (per task)

## Purpose

Give a task its SPEC and PLAN when it reaches the front of the queue, not before, so each spec reflects what the earlier tasks taught, and choose its verification tier. No task is dispatched without a SPEC and PLAN that spec QA has accepted, except on the fast path (step 9), which is never available for the full tier.

## Inputs

- `pk interview next` printing stage 7 means discovery is complete. Stage 7 is never approved. This playbook and `08_execute.md` run once per task.
- The task's entry in `program/register.yaml` and its brief in `program/briefs/`.
- `program/ARCHITECTURE.md`, `program/SPEC.md`, `program/traceability.yaml`, `program/CLAIM_BOUNDARIES.md`, `program/checks.yaml`.
- The rulings the task will need: `program/rulings.md` and `kit/rules/RULINGS_SEED.md`, read by heading.
- Merged work of the tasks it depends on.

## Steps

1. Pick the next task: every `depends_on` task is `done`, or `merged` on the light tier (`pk status list`), and it is not blocked or parked. A phase, milestone or step ID in `depends_on` has no status record: check every task under it. Run `pk status show <ID>` to confirm it is `not_started`.
2. Probe first. Before any spec is written, have a cheap agent (class D) check the assumptions the task rests on: the routes and reads the flow needs exist, the data is reachable, the local environment runs. Record what it found under Assumptions Checked; an assumption that fails changes the task before it costs a brief.
3. Write `program/tasks/<ID>/SPEC.md`: the tier, scope in and out, dependencies, the architecture contracts it consumes, security and tenancy, failure modes (one row each, each needing a test), observability, module placement, what is decided and must not be reopened, when the implementer must stop and report, and the requirement IDs it closes. Choose the tier and write it on the `Tier:` line: `full` for anything touching access control, tenancy, data migrations, cryptography, release or the kit's own gates, and whenever unsure; `standard` for ordinary work off those paths; `light` only for the kinds of change the program's ruling names (controller rule C11). A task with a server part and a client part gets `program/tasks/<ID>/CONTRACT.md` (from `kit/templates/CONTRACT.md`) written with its SPEC and committed to main in the dispatch commit, so both parts can build against it in parallel (controller rule C14).
4. If the work must edit a guarded file (one listed under `guarded_files` in `program/checks.yaml`), ask the user first. Only when the user agrees, add a line `Guarded edit: <path>` to the SPEC. That line records the user's approval. Without it the hand-back gate fails the branch.
5. Write `program/tasks/<ID>/PLAN.md`: the pull-request boundary, the work breakdown, the definition of done, gate evidence and rollback, and a section headed exactly `## Exact Commands`. Under it put one command per line.
   - Each line starts with a dollar sign and a space at column 0, then the command.
   - The gate splits the line into words the way a shell would, but runs it without a shell. So no pipes, `&&`, redirects, globbing or `VAR=value` prefixes. Put anything like that in a script and run the script.
   - A trailing ` # expect N` sets the expected exit code, for a command that must fail (the default is 0).
   - The section ends at the next `## ` heading.
6. If the task needs an environment marked `exists: false`, name it in the SPEC as unclaimable and keep the task `blocked_by` that environment.
6a. Zero-context reviews, full tier only. After the author hands back the SPEC and PLAN and before spec QA, start two lens reviewers in parallel, each with `kit/briefs/lens.md` and one rubric from `kit/rubrics/`: tenancy-security (class A) and failure-modes (class C). Each gets the SPEC, the PLAN, the CONTRACT if any, the files they cite and its rubric, nothing else, and writes its report in its own scratch folder. When the SPEC has open points, start the challenger (class C, `kit/rubrics/challenger.md`) on them as well, before any user point goes to the user, and put its new options beside the author's when you ask. Record each run with `pk cost add`. Standard and light tasks skip this step.
7. Start a spec-QA agent (not the author; `kit/briefs/spec_qa.md`). It reviews both files against the register text, the architecture and the traceability matrix, checks the tier against the scope, and writes `program/tasks/<ID>/review.md` as a finding-to-fix table. On the full tier it reviews blind first, then, in phase 2, rules on every row of the step 6a reports in the same `review.md`; no other agent merges or judges them. The author fixes; the same reviewer, resumed, re-checks only the findings. Repeat until accepted.
8. Leave SPEC.md, PLAN.md and review.md uncommitted in the controller's checkout. The controller commits them to main together with the task's `in_progress` status as one dispatch commit, before the worktree is created (`08_execute.md`, step 3), where `pk status set <ID> in_progress --role controller --tier <tier>` records the tier the SPEC states; the gates fail a task whose record and SPEC disagree. The implementer never edits its own PLAN. All three are required: a task dispatched without its `review.md` on main fails the hand-back gate and the merge check, and the branch cannot add it.
9. The fast path, for small bounded work the user asks for directly (controller rule C12): record the ruling (`pk record --ruling TEXT --adhoc TITLE`, or `pk rulings add`), write `program/tasks/<ID>/BRIEF.md` from `kit/templates/task_FAST_BRIEF.md` (scope, assumptions checked, decided, stop conditions, exact commands), and dispatch with `--fast --ruling R<n>`. There is no SPEC, PLAN or spec QA. It is refused for the full tier, a fast-path task cannot change a guarded file, and the gates hold the brief to what they hold a SPEC and PLAN to: on main, unchanged by the branch.

## Outputs

`program/tasks/<ID>/SPEC.md`, `PLAN.md` and `review.md`, or the fast-path `BRIEF.md` and its ruling, ready for the dispatch commit.

## Done when

The spec-QA agent has accepted the SPEC and PLAN, tier included, its `review.md` is written and `pk register validate` still passes; or, on the fast path, the ruling is recorded and the brief has every section.

## Next playbook

`08_execute.md`.
