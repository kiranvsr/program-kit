# Playbook 08: execute (the controller loop)

## Purpose

Build the register's tasks one at a time with an implementer, independent QA and commit-bound evidence, merging one accepted branch at a time. Three roles, each a separate agent session: the controller picks, dispatches, answers, merges and keeps state, and never writes product code; an implementer owns one task in its own worktree; QA never wrote the code and is the only role that sets `done`. Read `kit/briefs/controller.md`, `implementer.md` and `qa.md`, and `kit/rules/THROUGHPUT.md`.

## Inputs

- `program/register.yaml`, `program/status/`, `program/HANDOFF.md`, `program/controller-state.json`.
- For the task: `program/tasks/<ID>/SPEC.md` and `PLAN.md`, accepted by spec QA (`07_task_specs.md`) and committed to main in the dispatch commit (step 3).
- `program/checks.yaml` (build, test, scan, compile check, guarded files), `environments.yaml`, `CLAIM_BOUNDARIES.md`, `gates.yaml`.
- The project must git-ignore the bytecode and build output its test and scan commands write. An untracked leftover fails the clean-tree check of the evidence step.

## Steps

1. Session setup. Run `pk handoff check`, `pk status list` and `git branch --no-merged main`. If the project pins downloads, run `pk fetch --pins <file.json>` in the background once per host. Never fetch inside a gate.
2. Choose the next task in dependency order. Its SPEC and PLAN are accepted. Count running agents: at most three implementers and four agents in all.
3. Dispatch. The gate reads the SPEC, the PLAN and `checks.yaml` from main, so they must be on main first.
   - `pk status set <ID> in_progress --role controller`.
   - Commit the SPEC, the PLAN, the spec-QA `review.md` and that status file to main together, as one commit. The gate needs all three task files on main: without the review record it stops at `FAILED: review program/tasks/<ID>/review.md is missing on main`.
   - Then `pk worktree new <ID>` creates the worktree under `.worktrees/` and the branch `task/<ID>`. The project must git-ignore `.worktrees/`; add it to `.gitignore` before the first dispatch, or every worktree makes main look dirty. From here on the status record changes only on the task branch.
   - Give the implementer `kit/briefs/implementer.md`, the task brief, the SPEC, the PLAN, the rules, the full text of the rulings the SPEC cites, and the report path `program/evidence/<ID>/report.md` (written after the gate passes and committed with `results.json` in the evidence commit).
4. Build (implementer). Test first, and build exactly the PLAN's boundary.
   - Decide anything with precedent and record it. Ask the controller only for a new dependency, a security boundary with no precedent, a change to user scope, or two conflicting rulings.
   - The controller answers with `pk rulings add --role controller --question Q --decision D --precedent P`. For the first three it asks the user and records the user's answer. For two conflicting rulings it supersedes one explicitly.
   - The branch never edits its own SPEC or PLAN. It edits a guarded file only if the SPEC on main has `Guarded edit: <path>`.
5. Evidence (implementer). `program/evidence/<ID>/produce.py` uses `kit/scripts/evidence.py` and runs from a clean tree.
   - It records the head and main shas, the sha256 of every consumed input, every PLAN command literally with its real exit code and parsed fields, and the planted-bug table.
   - A planted bug counts only when a test fails, never when the build fails. The runner needs a build command and always carries one non-building row recorded as not caught.
6. Hand-back gate (implementer, from the main checkout, in the background): `pk gate handback <ID> --branch task/<ID>`.
   - It runs, in order: the guarded-file check; the check that the branch's SPEC.md and PLAN.md, and the spec-QA `review.md`, are byte-identical to main's; a trial merge of the branch into main; the build and compile check on the trial merge; then the test, scan and extra gate commands; the PLAN's `## Exact Commands` lines (each starts `$ `, with an optional ` # expect N`); evidence production (`produce.py`); the stale list.
   - It fails a branch that changed its own SPEC.md or PLAN.md or the spec-QA `review.md`, or a guarded file without its `Guarded edit:` line. It fails a task whose `review.md` is not on main at all (`FAILED: review program/tasks/<ID>/review.md is missing on main`): the fix is the controller's, on main, never the branch's.
   - On success its last line is exactly `OK: <branch> passes the hand-back gate at <sha>`. Exit code 0 alone is never accepted: read the last line.
   - A failure returns to the implementer with no QA round.
7. Evidence commit (implementer). The gate leaves `results.json` uncommitted. In the task worktree run `pk status set <ID> review --role implementer --evidence program/evidence/<ID>/results.json`. Commit the files of `program/evidence/<ID>/` that are new (`results.json` and `report.md`) and the status file, and nothing else. That is the evidence commit, and it follows the gated commit.
8. Start QA only when the evidence reflects current main, one branch at a time.
   - Run `pk stale` in the task worktree (it reads the checkout it runs in): it lists evidence records whose hashed inputs changed on main. This task's own record is listed whenever its branch changed one of its own inputs; ignore that. If main moved with code, the overlap check applies: `git merge-tree --write-tree main task/<ID>` is clean and none of the hashed inputs changed. Otherwise the branch merges main and re-produces.
   - Start `kit/briefs/qa.md`.
9. QA decides.
   - It runs `pk gate binding <ID> --branch task/<ID>` (`results.json` was committed directly on the commit it records, every later commit touches only this task's evidence directory and status file, and every hashed input matches the blob at that commit), `pk evidence validate` and `pk stale` in the task worktree. It does not re-run the hand-back gate: step 11 is the independent gate run. It re-runs the PLAN commands, plants its own bugs, and checks scope against the SPEC, closed requirement IDs against the traceability matrix, and claims against `CLAIM_BOUNDARIES.md`.
   - To accept, in the task worktree it writes `program/evidence/<ID>/acceptance.json` with `decision: accepted_development` and runs `pk status set <ID> done --role qa --evidence program/evidence/<ID>/results.json --evidence program/evidence/<ID>/acceptance.json`, which refuses an acceptance that does not accept or a results file that does not validate. It then commits the acceptance file and the status file together, as one commit on the task branch.
   - To reject, it runs `pk status set <ID> in_progress --role qa --note TEXT` and lists numbered findings, each with severity, location and the exact fix.
10. Fix rounds. Findings go back to the same implementer, who returns a finding-to-fix table. The re-check covers only the findings and the binding. It may run on Tier 2 (a cheaper model), which must report ESCALATE for anything outside its checklist. After a fix, repeat steps 6 and 7.
11. Merge, one branch at a time, right after acceptance.
   - From main run `pk gate merge-check --branch task/<ID>`. Its last line must be exactly `OK: <branch> merges into main at <sha>`.
   - The merge check is the controller's own run of the gate's checks and does not trust that the implementer ran it: the guarded-file check, then on a trial merge the check that the task's SPEC, PLAN and spec-QA `review.md` are main's (all three must be on main), and the build, compile check, test, scan and extra gate commands from main's `checks.yaml`. A branch that skipped the hand-back gate fails here.
   - Then `git merge --no-ff task/<ID>`. Never rebase.
   - Afterwards leave the worktree in place, or remove it by literal path with `git worktree remove <path>`. The kit has no remove command on purpose.
   - The merge appends the stale list (`pk stale`) for batched re-binding later. Do not chase stale evidence one merge at a time.
12. Bookkeeping after every merge or state change.
   - Update `program/controller-state.json` (its `stage`, `queued`, `parked` and `followups`) and append a line to `program/progress.md`.
   - Run `pk gate claims <GATE>` and `pk gate validate <GATE>` for each gate the merge touched.
   - Run `pk handoff update` and `pk handoff check`, then commit `program/`. Commit these files only after a merge, a ruling or at session end.
13. A task that has not been dispatched and needs an environment that does not exist is blocked on main: `pk status set <ID> blocked --role controller --blocked-by <environment>`. A task not yet dispatched that waits on a user decision is parked with the decision named: `pk status set <ID> parked --role controller --note TEXT`. A task already in progress that needs a user decision is set `blocked` with the reason, in its worktree, on its branch: `pk status set <ID> blocked --role controller --blocked-by <the decision>`. Parked is reachable only from `not_started`.
14. A PLAN command that fails identically on clean main is never a pass. Record a named environmental deviation: the real exit, one reproduction on clean main with its log hash, proof the task's inputs to it are unchanged, the failure message asserted, nothing claimed from it, and an owner follow-up.
15. A gate that reads every record (a phase gate, a re-bind batch) runs on a quiet main. Merge the feature tasks first, then run the gates back to back. Merge nothing else while one produces evidence.
16. After an interruption, void partial evidence by exact path and resume the agent at the interrupted step. Never restart a task. When two rejections share a cause, add the cause to the hand-back gate and record a ruling.

## Outputs

Merged branches on main, `program/status/<ID>.json`, `program/evidence/<ID>/results.json` and `acceptance.json`, `program/claims/<GATE>/*.json`, `program/rulings.md`, a current `program/HANDOFF.md`.

## Done when

Every task in the register is `done`, `blocked` or `parked` with a reason, `pk status validate` passes, `pk gate validate <GATE>` passes for every gate, and `pk handoff check` exits 0. Development acceptance is never production readiness: nothing is claimed about an environment that does not exist.

## Next playbook

`09_handoff.md` to hand over or take over. `10_change.md` when the user asks for a change to anything approved. `07_task_specs.md` for the next task.
