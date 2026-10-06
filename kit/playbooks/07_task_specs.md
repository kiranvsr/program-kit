# Playbook 07: task specs, just in time (stage 7)

## Purpose

Give a task its SPEC and PLAN when it reaches the front of the queue, not before, so each spec reflects what the earlier tasks taught. No task is dispatched without an accepted SPEC and PLAN on main.

## Inputs

- The task's entry in `program/register.yaml` and its brief in `program/briefs/`.
- `program/ARCHITECTURE.md`, `program/SPEC.md`, `program/traceability.yaml`, `program/CLAIM_BOUNDARIES.md`, `program/checks.yaml`.
- The rulings the task will need: `program/rulings.md` and `kit/rules/RULINGS_SEED.md`, read by heading.
- Merged work of the tasks it depends on.

## Steps

1. Pick the next task: every `depends_on` task is `done` (`pk status list`) and it is not blocked or parked. Run `pk status show <ID>` to confirm it is `not_started`.
2. Write `program/tasks/<ID>/SPEC.md`: scope in and out, dependencies, the architecture contracts it consumes, security and tenancy, failure modes (one row each, each needing a test), observability, module placement, resolved questions, and the requirement IDs it closes. For each guarded file the work must edit, add a line `Guarded edit: <path>`. Without that line on main the hand-back gate fails the branch.
3. Write `program/tasks/<ID>/PLAN.md`: the pull-request boundary, the work breakdown, the definition of done, gate evidence and rollback, and a section headed exactly `## Exact Commands`. Under it put one command per line, each starting `$ `, with an optional ` # expect N` for a non-zero expected exit. The hand-back gate runs these lines literally.
4. If the task needs an environment marked `exists: false`, name it in the SPEC as unclaimable and keep the task `blocked_by` that environment.
5. Start a spec-QA agent (not the author; `kit/briefs/spec_qa.md`). It reviews both files against the register text, the architecture and the traceability matrix, and writes `program/tasks/<ID>/review.md` as a finding-to-fix table.
6. The author fixes. The reviewer re-checks only the findings. Repeat until accepted.
7. Commit SPEC, PLAN and the review to main. Only then does the controller dispatch (`08_execute.md`). The implementer never edits its own PLAN.
8. The first time you reach this stage, run `pk interview challenge 7`, then `pk interview approve 7 --artifact program/register.yaml`. It re-validates the register and records that execution may start. Later tasks repeat steps 1 to 7 only.

## Outputs

`program/tasks/<ID>/SPEC.md`, `PLAN.md` and `review.md`, merged to main. Once, `program/approvals/stage-7.json`.

## Done when

The spec-QA agent has accepted the SPEC and PLAN, they are on main, and `pk register validate` still passes.

## Next playbook

`08_execute.md`.
