# Playbook 08: execute (the controller loop)

## Purpose

Build the register's tasks with implementers working in parallel, independent review sized to each task's risk, and commit-bound evidence, merging accepted branches one at a time. Three roles, each a separate agent session: the controller picks, dispatches, answers, merges and keeps state, and never writes product code; an implementer owns one task in its own worktree; QA never wrote the code and is the only role that sets `done`. The rules are in `kit/rules/RULES.md` (part 4 is the controller's); this playbook is the order of the commands.

## Inputs

- `program/register.yaml`, `program/status/`, `program/HANDOFF.md`, `program/COMMON.md`, `program/controller-state.json`.
- For each task its definition on main: `program/tasks/<ID>/SPEC.md`, `PLAN.md` and the spec-QA `review.md` (`07_task_specs.md`), or on the fast path `program/tasks/<ID>/BRIEF.md` and its ruling.
- `program/checks.yaml` (build, test, scan, compile check, guarded files, tiers), `environments.yaml`, `CLAIM_BOUNDARIES.md`, `gates.yaml`.
- The project must git-ignore the bytecode and build output its commands write, and `.worktrees/`. An untracked leftover fails the clean-tree check of the evidence step.

What each tier runs (controller rule C11; the gates enforce it from the status record on main):

| | `full` | `standard` | `light` |
|---|---|---|---|
| Definition | SPEC, PLAN, spec-QA review | the same, or the fast path | the same, or the fast path |
| Hand-back gate | every step, with evidence | every step, with evidence | build, compile check, scans |
| Review | QA: binding, re-run, own planted bugs, attack questions | QA: binding and one scoped review | the controller reads the diff |
| Before the merge | the merge check, every command | the merge check, or one integration round | build, compile check, scans, `--inspected` |
| Ends at | `done`, by QA | `done`, by QA | `merged`, by the controller |

## Steps

1. Session setup. Run `pk handoff check`, `pk status list`, `pk cost report` and `git branch --no-merged main`. Check where your shell is (`git rev-parse --show-toplevel`) and that the ports the local stack needs are free. If the project pins downloads, run `pk fetch --pins <file.json>` in the background once per host. Never fetch inside a gate.
2. Choose the next tasks in dependency order, up to three implementers and four agents in all. Tasks that do not share files go out together. Work the user asks for directly that is not in the register becomes an ad-hoc task first: `pk adhoc add --title T` gives it an ID (`AH-0001` onward).
3. Dispatch each task. The gate reads its definition, its tier and `checks.yaml` from main, so they must be on main first.
   - `pk status set <ID> in_progress --role controller --tier <tier>` (no `--tier` means `full`). For the fast path add `--fast --ruling R<n>`; it is refused for the full tier, and a lower tier is refused until `tiers.full_paths` is filled in `program/checks.yaml`.
   - Commit the definition and that status file to main together, as one commit: the SPEC, the PLAN and the spec-QA `review.md`, or the fast-path `BRIEF.md` (its `Tier:` and `Ruling:` lines equal to the status record), and the task's `CONTRACT.md` when it has one. Without the review record the gates stop at `FAILED: review program/tasks/<ID>/review.md is missing on main`.
   - Then `pk worktree new <ID>` creates the worktree under `.worktrees/` and the branch `task/<ID>`. From here on the status record changes on the task branch; a tier change and a light task's `merged` record are the controller's, on main (controller rule C9).
   - The dispatch message names the model, the tier, `kit/briefs/implementer.md`, `program/COMMON.md`, the definition, the rulings it cites and the report path. It repeats none of them.
4. Build (implementer), test first, exactly the boundary of the definition. A task with a server part and a client part builds both against `program/tasks/<ID>/CONTRACT.md` from the dispatch commit; a change to it is appended on main by the controller.
   - The implementer decides anything with precedent and asks only in the four NEEDS_CONTEXT cases. The controller answers with `pk rulings add --role controller --question Q --decision D --precedent P`; for a new dependency, a security boundary or user scope it asks the user first.
   - The branch never edits its own definition. It edits a guarded file only if the SPEC on main has `Guarded edit: <path>`; a fast-path task never does.
5. Evidence (implementer, full and standard tier). `program/evidence/<ID>/produce.py` uses `kit/scripts/evidence.py` from a clean tree: the head and main shas, the sha256 of every consumed input, every command of `## Exact Commands` literally with its real exit code, and the planted-bug table. A planted bug counts only when a test fails.
6. Hand-back gate (implementer, from the main checkout, in the background): `pk gate handback <ID> --branch task/<ID>`.
   - It runs, in order: the guarded-file check; the tier check; the check that the definition is on main and byte-identical on the branch; a trial merge; the build and compile check on it; then the tests, scans and extra gate commands; the `## Exact Commands` lines; `produce.py`; the drift report. A light-tier task runs only the build, the compile check and the scans.
   - On success its last line is exactly `OK: <branch> passes the hand-back gate at <sha>`. Exit code 0 alone is never accepted. A failure returns to the implementer with no QA round.
7. Evidence commit (implementer, full and standard tier). In the task worktree: `pk status set <ID> review --role implementer`, then one commit of the new results record, `program/evidence/<ID>/latest.json`, `report.md` and the status file, and nothing else. A committed record is never edited or deleted; a refresh appends one.
8. Review. Start it when the evidence reflects current main: `pk stale` in the task worktree reports drift, which is information; if main moved with code, `git merge-tree --write-tree main task/<ID>` must be clean and none of the task's own hashed inputs changed, otherwise the branch merges main and re-runs the gate.
   - `pk worktree review <ID>` creates the reviewer's own detached worktree at the reviewed commit and writes the diff file and a stat file (what the diff leaves out, and every changed path). Dispatch `kit/briefs/qa.md` with them, the commit, the tier and the model. Reviews of different tasks may run at once; the implementer has stopped.
   - A light-tier task has no QA. Read its diff file yourself; the commit you read is what you pass as `--inspected`.
9. QA decides (`kit/briefs/qa.md`). It runs `pk gate binding <ID> --branch task/<ID>` and reviews the diff against the definition; on the full tier it also re-runs the commands, plants its own bugs and works through the attack questions.
   - To accept, with the branch tip still the commit it reviewed: `pk evidence accept <ID> <file>`, then `pk status set <ID> done --role qa`, committed with the pointer on the task branch. Both refuse an acceptance that does not bind the latest valid results record; a full-tier `done` also needs the commands QA re-ran and three planted bugs of its own in it.
   - To reject: `pk status set <ID> in_progress --role qa --note TEXT` and numbered findings, each with severity, location and the exact fix.
   - A branch already marked `done` that gets late work, or that QA now rejects, is not edited by hand: `pk status reopen <ID> --reason TEXT` (controller or QA) moves it back to `review`, and `pk status reopen <ID> --reason TEXT --role qa --reject` to `in_progress`. The reason stays in the history; QA accepts again against a fresh acceptance. It is refused once main holds the task as done.
10. Fix rounds. Resume the same implementer; it returns a finding-to-fix table and repeats steps 6 and 7. Before each re-check run `pk worktree review <ID>` again (it moves the clean review worktree to the new commit), then resume the same reviewer, who checks only the findings and the binding. Record each run with `pk cost add --task <ID> --role R --model M --tokens N`; when `pk cost report` flags an agent, start a fresh one with a tight fix brief.
11. Land, one task at a time, right after acceptance: `pk merge <ID>` from the main checkout (for a light task, `pk merge <ID> --inspected <sha>`).
   - It refuses a branch QA has not accepted: `done` on the tip, the acceptance derived from the evidence committed there, and nothing but the task's own evidence and status files (and clean merges of main) committed after the reviewed commit. Late work lands once the gate ran on it and QA accepted that record. Then it runs the merge check, whose last line must be exactly `OK: <branch> merges into main at <sha>`: the guarded-file and tier checks, then on a trial merge the check that the task's SPEC, PLAN and spec-QA `review.md` are main's (or its `BRIEF.md`), and the build, compile check, test, scan and extra gate commands from main's `checks.yaml`. A branch that skipped the hand-back gate fails here.
   - It then merges with `git merge --no-ff`, records a light task `merged` with the inspected commit (by hand: `pk status set <ID> merged --inspected <sha>`, which re-runs the light path's guard on the merge), rewrites the claims of the gates that name the task, regenerates the hand-off and commits those files. Pass `--trailer` with your attribution line. Never rebase.
12. Or land several in one integration round, for accepted branches below the full tier: `pk integrate start <NAME> <ID> <ID>` merges their tips onto `round/<NAME>`; run the combined acceptance there; `pk integrate check <NAME>` runs main's checks once and must end `OK: round/<NAME> passes the combined check at <sha>`; then `pk merge <ID> --round <NAME>` for each, one at a time; then `pk integrate close <NAME>`. It fails if main differs from the tested tree; `--recheck` runs main's checks over the final tree. An open round is a line of the hand-off's Now list until it is closed.
13. After every merge: `pk worktree finish <ID>` (`--symlink` for the symlinks it may remove, `--drop-ignored` once you saved what you need of the git-ignored files, `--compose-project` for the task's compose project: containers and volumes by exact label, images left alone); `pk gate validate <GATE>` for each gate the merge touched; triage the follow-ups (`pk followups add`, `close`, `tag`, `dup`); spot-check the claims the reports made, by their Steps to verify; and record what changed with `pk record --ruling TEXT`, which appends the ruling, updates the state, regenerates the hand-off and commits. Its `--now TEXT`, `--note <ID>=TEXT`, `--queued <ID>=TEXT` and `--parked <ID>=TEXT` write what the hand-off says is going on.
14. A task that has not been dispatched and needs an environment that does not exist is blocked on main: `pk status set <ID> blocked --role controller --blocked-by <environment>`. One that waits on a user decision is parked: `pk status set <ID> parked --role controller --note TEXT`. A task already in progress is set `blocked` in its worktree, on its branch. The hand-off prints what a blocked task waits on and a parked task's note. Batch the decisions you need from the user, each with a recommendation, and go on with the rest; one that no task is parked on is a follow-up with the area `needs user`.
15. Raising a tier after dispatch (working rule W10): on main, `pk status tier <ID> <tier> --ruling R<n>` and the `Tier:` line of the definition, as one commit. The command prints the rest: merge main in the task worktree and, when the status file conflicts, take main's record (`git checkout --theirs program/status/<ID>.json`). The task is `in_progress` again on the new tier. Never edit the status file by hand.
16. Special cases, each a rule: a command that fails identically on clean main is a named deviation, never a pass (seed ruling K27); a gate that reads every record runs on a quiet main (controller rule C8); after an interruption, void partial evidence and resume, never restart (working rule W8); two rejections with one cause become a gate check (controller rule C7).

## Outputs

Merged branches on main, `program/status/<ID>.json`, the append-only records under `program/evidence/<ID>/results/` and `acceptance/` with `program/evidence/<ID>/latest.json`, `program/claims/<GATE>/*.json`, `program/rulings.md`, `program/costs.jsonl`, a current `program/HANDOFF.md`.

## Done when

Every task in the register is `done`, `merged`, `blocked` or `parked` with a reason, `pk status validate` passes (it re-derives every `done` and every `merged`), `pk gate validate <GATE>` passes for every gate, and `pk handoff check` exits 0. Development acceptance is never production readiness: nothing is claimed about an environment that does not exist, and a `merged` task evidences nothing.

## Next playbook

`09_handoff.md` to hand over or take over. `10_change.md` when the user asks for a change to anything approved. `07_task_specs.md` for the next task.
