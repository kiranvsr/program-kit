# Playbook 11: adopting an existing programme

## Purpose

Move a programme that ran before the kit onto it, once, without rewriting its history. Its register, statuses, rulings, follow-ups, ad-hoc tasks, cost log and hand-off become the kit's state; its old evidence stays where it is and can be cited; everything after adoption follows the kit's rules.

## Inputs

- The programme's own exporter, which writes one neutral JSON file in the shape of `kit/templates/adoption.schema.json` (`"schema": "program-kit.adoption.v1"`), or an `adopt_importer` that one of its extensions registers.
- A repository on `main` with the kit installed (`install.sh`), `pk doctor` passing, and no kit work in `program/` yet.
- The legacy rulings log and hand-off, and the directories of old evidence (`legacy_evidence_roots`), committed at HEAD.

## Steps

1. Export the source, then validate it without writing anything: `pk adopt <source.json> --dry-run`, or `pk adopt --from <importer> [args...] --dry-run`. It prints every problem at once, or the plan and the counts: tasks by prefix, statuses (adopted, in flight, not started), what is held to shape only, rulings, follow-ups with the gaps below the import point, ad-hoc tasks and cost lines. Fix the exporter until the dry run is clean.
2. Read the plan with the user. Each source `done` becomes `adopted`; each task that was `in_progress`, `review` or `blocked` comes in `not_started` with a follow-up "re-dispatch <ID> (was <state> at adoption)"; a parked task's note becomes a follow-up. Register entries the kit's size, fan-in, role or gate-ID rules would refuse are kept as they are and listed under `legacy_register` in `program/adoption.yaml`.
3. Adopt: the same command without `--dry-run`. It copies the legacy rulings log and hand-off into `program/legacy/` byte for byte, writes the register, the status records, the controller state, the cost log and `program/adoption.yaml`, then runs `pk register validate`, `pk status validate`, `pk handoff update` and `pk doctor`; when one fails, every file it wrote is put back. It commits nothing.
4. Check the result: `pk status list` shows the adopted tasks as `adopted`, never `done`; `pk followups list` shows the open follow-ups and the hand-off names the next ruling; `pk cost report` totals the imported rows. Then commit `program/` as one commit.
5. Work on. The kit issues rulings from `ruling_start`; `pk rulings show R<n>` reads an older one from the frozen log. A task that depends on an adopted task is ready as it would be after a `done`. New work on an adopted task starts with `pk status reopen <ID> --reason TEXT` (back to `in_progress` at the full tier, through the whole loop). A new task's producer cites old evidence with `Evidence.reference(path)` (a path under `legacy_evidence_roots`); `pk evidence history <ID>` and `pk dashboard` show the citation, and the gate checks its digest at the record's head.
6. Re-dispatch the tasks the follow-ups name through `07_task_specs.md`, and close each follow-up when its task is dispatched.

## Outputs

`program/adoption.yaml`, the frozen copies under `program/legacy/`, the register, `adopted` and imported `not_started` status records, the controller state with the imported and the opened follow-ups, `program/costs.jsonl`, and a current hand-off, in one commit.

## Done when

`pk doctor`, `pk register validate`, `pk status validate` and `pk handoff check` pass on main, and the adoption commit holds `program/` alone. Adoption happens once: `pk adopt` refuses a programme that has `program/adoption.yaml`, and no task branch may change it or anything under `program/legacy/`.

## Next playbook

`08_execute.md` to run the loop on the adopted register. `07_task_specs.md` for the first re-dispatched or new task.
