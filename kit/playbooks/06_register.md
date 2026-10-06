# Playbook 06: roadmap and register (stage 6)

## Purpose

Break the frozen specification into phases and tasks small enough to build one at a time, with every first-release requirement and journey owned by a task, every task tied to a gate or marked gate-less, and anything that needs a missing environment parked or blocked.

## Inputs

- The frozen specification: `SPEC.md`, `ARCHITECTURE.md`, `gates.yaml`, `environments.yaml`, `traceability.yaml`.
- `kit/interview/roles.yaml` (the agent roles a task can have) and `kit/interview/coverage.yaml` stage 6.

## Lenses active

Project owns phase shaping, capacity and parking. Architect, program, delivery and engineering contribute and challenge.

## Steps

1. Run `pk interview next` and confirm stage 6.
2. Run the interview loop as in `01_discover.md` step 3 for the three topics: phase shaping, capacity and cadence, parking and blockers.
3. Write `program/roadmap.md`. Shape phases by capability. Phase 1 is a walking skeleton: one thin end-to-end journey through every component, deployed to every environment that exists. Then vertical slices by journey. Attach a gate to each phase end.
4. Fill `program/register.yaml`: phases, milestones, steps and tasks. Each task has a one-paragraph `text`, a `role` from `roles.yaml`, a `size` of S or M (anything larger becomes a step of several tasks), its `environments`, `depends_on` (at most six), its `gate` (or `null`) and the requirement and journey IDs it `closes`. One task is one change, by one role.
5. A task that needs an environment marked `exists: false` carries `blocked_by: [<environment>]`. Park what waits on a user decision with an owner and a date.
6. Run `pk register assign-ids` to fill any missing IDs (existing IDs never change), then `pk register validate`. Fix every problem: duplicate IDs, missing dependencies, cycles, unmapped first-release requirements, granularity.
7. Run `pk trace sync` to copy each task's `closes` into the matrix, then `pk trace validate --stage 6`.
8. Run `pk briefs generate`, then `pk briefs check`. The briefs under `program/briefs/` are generated and immutable once approved.
9. Write back, get corrections, run `pk interview report 6`.
10. Challenge round: `pk interview challenge 6`, each lens in its own session, every row resolved or deferred with an owner and a date.
11. Run `pk interview validate`, get the user's approval, then run `pk interview approve 6`. This freezes the register.

## Outputs

`program/roadmap.md`, `program/register.yaml`, `program/briefs/`, `program/traceability.yaml` (tasks), `program/reports/stage-6.md`, `program/reports/stage-6-challenge.md`, `program/approvals/stage-6.json`.

## Done when

`pk interview approve 6` succeeds, `pk register validate` and `pk trace validate --stage 6` pass, and `pk briefs check` reports nothing. Later register edits go only through `pk register change --note TEXT`, which logs the change and regenerates the briefs. After one, run `pk interview next`. If it names an approved stage whose files changed, follow what it says. Stage 7 is never approved, so `next` naming stage 7 is normal and means discovery is complete; act on discovery only when it names stage 6 or lower.

## Next playbook

`07_task_specs.md`. Confirm with `pk interview next`, which now prints stage 7.
