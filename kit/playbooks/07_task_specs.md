# Playbook 07: task specs, just in time (per task)

## Purpose

Give a task its SPEC and PLAN when it reaches the front of the queue, not before, so each spec reflects what the earlier tasks taught. No task is dispatched without a SPEC and PLAN that spec QA has accepted.

## Inputs

- `pk interview next` printing stage 7 means discovery is complete. Stage 7 is never approved. This playbook and `08_execute.md` run once per task.
- The task's entry in `program/register.yaml` and its brief in `program/briefs/`.
- `program/ARCHITECTURE.md`, `program/SPEC.md`, `program/traceability.yaml`, `program/CLAIM_BOUNDARIES.md`, `program/checks.yaml`.
- The rulings the task will need: `program/rulings.md` and `kit/rules/RULINGS_SEED.md`, read by heading.
- Merged work of the tasks it depends on.

## Steps

1. Pick the next task: every `depends_on` task is `done` (`pk status list`) and it is not blocked or parked. A phase, milestone or step ID in `depends_on` has no status record: check every task under it. Run `pk status show <ID>` to confirm it is `not_started`.
2. Write `program/tasks/<ID>/SPEC.md`: scope in and out, dependencies, the architecture contracts it consumes, security and tenancy, failure modes (one row each, each needing a test), observability, module placement, resolved questions, and the requirement IDs it closes.
3. If the work must edit a guarded file (one listed under `guarded_files` in `program/checks.yaml`), ask the user first. Only when the user agrees, add a line `Guarded edit: <path>` to the SPEC. That line records the user's approval. Without it the hand-back gate fails the branch.
4. Write `program/tasks/<ID>/PLAN.md`: the pull-request boundary, the work breakdown, the definition of done, gate evidence and rollback, and a section headed exactly `## Exact Commands`. Under it put one command per line.
   - Each line starts with a dollar sign and a space at column 0, then the command.
   - The gate splits the line into words the way a shell would, but runs it without a shell. So no pipes, `&&`, redirects, globbing or `VAR=value` prefixes. Put anything like that in a script and run the script.
   - A trailing ` # expect N` sets the expected exit code, for a command that must fail (the default is 0).
   - The section ends at the next `## ` heading.
5. If the task needs an environment marked `exists: false`, name it in the SPEC as unclaimable and keep the task `blocked_by` that environment.
6. Start a spec-QA agent (not the author; `kit/briefs/spec_qa.md`). It reviews both files against the register text, the architecture and the traceability matrix, and writes `program/tasks/<ID>/review.md` as a finding-to-fix table.
7. The author fixes. The reviewer re-checks only the findings. Repeat until accepted.
8. Leave SPEC.md, PLAN.md and review.md uncommitted in the controller's checkout. The controller commits them to main together with the task's `in_progress` status as one dispatch commit, before the worktree is created (`08_execute.md`, step 3). The implementer never edits its own PLAN.

## Outputs

`program/tasks/<ID>/SPEC.md`, `PLAN.md` and `review.md`, ready for the dispatch commit.

## Done when

The spec-QA agent has accepted the SPEC and PLAN and `pk register validate` still passes.

## Next playbook

`08_execute.md`.
