# Playbook 10: change requests after the freeze

## Purpose

Change the meaning of anything approved at stage 5 or later without silently breaking work that is done or in flight. A change request (CR) records what changes, why and who asked, shows what it touches through the traceability matrix, and needs the user's approval before anything is edited.

## Inputs

- The request, and who made it.
- `program/traceability.yaml`, `program/register.yaml`, `program/status/`, and the frozen artifacts.

A correction that does not change meaning (a typo, a clarified sentence) is not a CR: edit the file and run `pk interview amend N`.

Before committing an edit under `program/`, run `pk change check` (0.5.5; it only hashes the approved files, so it takes a moment). It names every approved file (SPEC.md, ARCHITECTURE.md, the register and the rest) changed since its approval and says what to do: the approved request that carries the edit (`pk change apply` re-hashes it), or `pk change create` for a change of meaning, or `pk interview amend N` for a correction. It warns and exits 0; `--strict` exits 1, for an agent tool's pre-commit step. A generated file edited by hand shows in `pk drift` D1, and an approved one in D2, both naming `pk change create`.

Research is not a change until it graduates (0.6.0, opt-in with the `research` section of `program/checks.yaml`). A folder of strategy items (`pk research init` writes its index from `kit/templates/research-README.md`) is isolated: no brief, spec or plan cites an item until the user has decided it should be built. Then record a ruling that quotes the item's ID and what was decided, run `pk research graduate <ID> --ruling R<n>` (with `--archive` to move the item's file to `research.archive`), and carry the meaning through the steps below. `pk research check` (and D8 of `pk drift`) names, as `<file>:<line>: <why>`, every guarded file that cites an item not yet graduated; `pk research list` shows each item's state.

The user's feedback during a beta or a pilot is kept in `program/beta-feedback.md` (0.6.0, optional): `pk feedback add --urgency urgent|high|polish --summary TEXT [--touches IDs] [--decision D]` when a message arrives, `pk feedback set BF-n --state S [--decision D] [--ruling R<n>]` as it moves, `pk feedback list --open` for what is waiting. Urgent feedback preempts the lowest-priority work in flight, high takes the next free slot, polish is batched; a decision that changes meaning after the freeze still goes through the steps below. With the file, `pk dashboard` shows a Feedback panel and the hand-off counts the open items by urgency.

## Steps

1. Create the CR: `pk change create --title T --why W --asked-by A`. It writes `program/changes/CR-NNN.md`. Edit the file to say exactly what changes.
2. List the impact: `pk change impact CR-NNN ID...`, naming every journey, requirement, component and gate the change touches. It lists, through the traceability matrix, each requirement, component, gate and task reached, split into done tasks (whose acceptance would no longer cover the new meaning), in-flight tasks and not-started tasks.
3. Show the user the CR and the impact. The user approves with that impact in view. Only after the user has said so, run `pk change approve CR-NNN --by NAME` with the user's name. Never approve for the user.
4. Amend the affected artifacts and put the CR number in each one's change log. Run `pk trace validate --stage 6`. It must pass again.
5. For each affected done task, add a follow-up task to `program/register.yaml` that carries the new meaning. Then run `pk register assign-ids`, `pk register validate` and `pk trace sync`. The register log line and the regenerated briefs come from step 7, so do not run `pk register change` here.
6. For each affected in-flight task, record a ruling with `pk rulings add --role controller --question Q --decision D --precedent CR-NNN` and deliver it to that task's implementer. The SPEC or PLAN of a not-started task is simply written with the new meaning when its SPEC is written (`07_task_specs.md`).
7. Run `pk change apply CR-NNN --note TEXT` to record that the change has been applied. It refuses without the approval record, and, before it writes anything, when the register is invalid or the matrix's `tasks` section differs from what `pk trace sync` would write (it says to run `pk trace sync`; run it after every register edit, as in step 5). It then runs the `pk trace validate --stage 6` check and renders the briefs, still writing nothing; if either fails, the log, the briefs and the request are as they were, and you fix the register, run `pk trace sync` and apply again. Only when everything passes does it write the one `CR-NNN: TEXT` line of the register log, regenerate the briefs and mark the request applied (a write that fails part-way is rolled back). From 0.5.5 it does two things more by default, each named in its output: a matrix whose `tasks` section is behind the register is synced first (what `pk trace sync` writes; put back when a later check fails), and once the request is applied every approved stage whose files changed is re-hashed, as `pk interview amend N` does, with each path listed. Read those lines: a path the change did not mean to touch is a hand edit, to be undone or given its own request. `--no-sync` keeps the 0.5.4 behaviour: it refuses an unsynced matrix (it says to run `pk trace sync`) and re-hashes nothing. Then run `pk briefs check`.
8. Run `pk interview next`. If it names an approved stage whose files changed (after `--no-sync`, or a file edited after the apply), run `pk interview amend N` for it. Stage 7 is never approved, so `next` naming stage 7 is normal and means discovery is complete; act on discovery only when it names stage 6 or lower. Then run `pk handoff update` and `pk handoff check`, and commit `program/`.

## Outputs

`program/changes/CR-NNN.md` with the impact and the user's approval, amended artifacts, follow-up tasks in the register, rulings for in-flight tasks, a regenerated brief tree and a current hand-off.

## Done when

`pk change apply` has succeeded, `pk trace validate --stage 6` and `pk register validate` pass, and `pk handoff check` exits 0. A CR never edits a done task's evidence or acceptance: a done task stays done, and a new task carries the change.

## Next playbook

`08_execute.md` to continue with the changed register. `07_task_specs.md` for the first follow-up task.
