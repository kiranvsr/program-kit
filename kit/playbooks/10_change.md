# Playbook 10: change requests after the freeze

## Purpose

Change the meaning of anything approved at stage 5 or later without silently breaking work that is done or in flight. A change request (CR) records what changes, why and who asked, shows what it touches through the traceability matrix, and needs the user's approval before anything is edited.

## Inputs

- The request, and who made it.
- `program/traceability.yaml`, `program/register.yaml`, `program/status/`, and the frozen artifacts.

A correction that does not change meaning (a typo, a clarified sentence) is not a CR: edit the file and run `pk interview amend N`.

## Steps

1. Create the CR: `pk change create --title T --why W --asked-by A`. It writes `program/changes/CR-NNN.md`. Edit the file to say exactly what changes.
2. List the impact: `pk change impact CR-NNN ID...`, naming every journey, requirement, component and gate the change touches. It lists, through the traceability matrix, each requirement, component, gate and task reached, split into done tasks (whose acceptance would no longer cover the new meaning), in-flight tasks and not-started tasks.
3. Show the user the CR and the impact. The user approves with that impact in view. Only after the user has said so, run `pk change approve CR-NNN --by NAME` with the user's name. Never approve for the user.
4. Amend the affected artifacts and put the CR number in each one's change log. Run `pk trace validate --stage 6`. It must pass again.
5. For each affected done task, add a follow-up task to `program/register.yaml` that carries the new meaning. Then run `pk register assign-ids`, `pk register validate` and `pk trace sync`. The register log line and the regenerated briefs come from step 7, so do not run `pk register change` here.
6. For each affected in-flight task, record a ruling with `pk rulings add --role controller --question Q --decision D --precedent CR-NNN` and deliver it to that task's implementer. The SPEC or PLAN of a not-started task is simply written with the new meaning when its SPEC is written (`07_task_specs.md`).
7. Run `pk change apply CR-NNN --note TEXT` to record that the change has been applied. It refuses without the approval record. It writes the one `CR-NNN: TEXT` line of the register log, regenerates the briefs and runs `pk trace validate --stage 6`. Then run `pk briefs check`.
8. Run `pk interview next`. If it names an approved stage whose files changed, run `pk interview amend N` for it. Stage 7 is never approved, so `next` naming stage 7 is normal and means discovery is complete; act on discovery only when it names stage 6 or lower. Then run `pk handoff update` and `pk handoff check`, and commit `program/`.

## Outputs

`program/changes/CR-NNN.md` with the impact and the user's approval, amended artifacts, follow-up tasks in the register, rulings for in-flight tasks, a regenerated brief tree and a current hand-off.

## Done when

`pk change apply` has succeeded, `pk trace validate --stage 6` and `pk register validate` pass, and `pk handoff check` exits 0. A CR never edits a done task's evidence or acceptance: a done task stays done, and a new task carries the change.

## Next playbook

`08_execute.md` to continue with the changed register. `07_task_specs.md` for the first follow-up task.
