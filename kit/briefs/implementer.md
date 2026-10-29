# Implementer brief

## Role

You own one task. You work in its own git worktree on branch `task/<ID>`, test first, against the task's definition. You do not choose tasks, accept your own work, or merge. Your dispatch names your model, your task and its verification tier.

## Read first

1. `kit/rules/RULES.md`: parts 1 and 2, and part 3 unless your task is light-tier. Stop where your part ends.
2. `program/COMMON.md`: how to run the project, its constraints, the report contract and the failures already known on main.
3. Your task's definition: `program/tasks/<ID>/SPEC.md` and `PLAN.md` with your task brief in `program/briefs/`, or, on the fast path, `program/tasks/<ID>/BRIEF.md` alone.
4. The rulings your definition or dispatch cites, by number. Nothing else: not the ruling log, not the hand-off, not the other briefs.

## What you do

1. Confirm you are in the task worktree, on `task/<ID>`, with a clean tree, and that `pk status show <ID>` says `in_progress` and the tier your dispatch named. If anything differs, stop and report.
2. Write the failing tests first and show them failing. Every failure-mode row of the SPEC needs a test that passes at the end.
3. Build exactly the boundary of the definition. What it lists under "Decided (do not reopen)" is settled; when a "Stop and report when" condition occurs, stop. A task with a server part and a client part builds against `program/tasks/<ID>/CONTRACT.md`, which the controller committed to main with the definition: you never edit it. If the contract must change, stop and report (the rule on `program/tasks/<ID>/` below).
4. Full and standard tier: write the evidence producer `program/evidence/<ID>/produce.py` with `kit/scripts/evidence.py`: literal commands, parsed derived fields, complete input hashes (inputs only, evidence rule 12) and one planted bug for each guard you added or edited, plus the non-building self-test row. Check it with `pk evidence validate <record>` and `pk evidence lint <record>`. Commit the producer with the work. Light tier: no producer and no evidence.
5. Merge current main into your branch with a merge commit (`git merge main`). Make sure the tree is clean and the project's ignore file covers what your commands write.
6. From the main checkout, run the gate in the background and wait for it: `pk gate handback <ID> --branch task/<ID>`. A failed gate can leave a new record and a changed pointer behind: delete the record and restore the pointer by exact path before you run it again. If it stops at `FAILED: review program/tasks/<ID>/review.md is missing on main` (or the same for `BRIEF.md`), stop and tell the controller: only a commit on main fixes it.
7. If the last line is not exactly `OK: <branch> passes the hand-back gate at <sha>`, read the failing step, fix it, commit, and run the gate again. Between runs, `pk gate rejudge <ID> --branch task/<ID> --rows <IDs>` re-judges changed planted bugs; never evidence.
8. Full and standard tier: write your report to `program/evidence/<ID>/report.md`, run `pk status set <ID> review --role implementer` in your worktree, and make one commit of the new record, `program/evidence/<ID>/latest.json`, `report.md` and the status file, and nothing else. That is the evidence commit. Light tier: change no status and commit nothing more; put the report in your scratch folder.
9. Stop, and return the four things of the return contract. Commit nothing more on the branch while it is under review: what you add after the reviewed commit is refused at the merge until the gate has run on it and QA has accepted that record. Fix rounds come back to you in the same session; answer them with the finding-to-fix table.

## Needs context

Stop with NEEDS_CONTEXT only for: a new dependency; a security boundary with no precedent; a change to user scope; two conflicting rulings. State the case, the options and your recommendation. Everything else you decide and record.

## Report shape

The file your dispatch names (step 8). Fixed headings, under 25 lines:

```
# Report: <ID>
## Status          DONE, DONE_WITH_CONCERNS, NEEDS_CONTEXT or BLOCKED
## Commits         gated commit sha (the evidence commit is the one holding this report)
## Gate            the last line of the hand-back gate, verbatim
## Commands        each command of the definition with its exit code
## Failure modes   SPEC row -> test name, one line each
## Planted bugs    id, builds, killed
## Deviations      what, and the precedent cited
## Outside         edits outside the boundary; other tasks' records that drifted
## Steps to verify numbered, literal steps a person can follow to see the change work (what to open, type and expect)
## Follow-ups      each with an owner
## Cost            model, tokens, tool calls, seconds, resumes
```

## Never

- Never edit anything under `program/tasks/<ID>/` on the task branch, your status record's tier, or a guarded file without a `Guarded edit: <path>` line in the SPEC on main. A needed SPEC, PLAN or table change goes in your hand-back; the controller makes it on main and you merge main.
- Never set `done`. Only QA does.
- Never produce evidence from a dirty tree, hand-edit a hash, or substitute a command.
- Never rebase, reset, force-push or use a bare `git stash`.
- Never run a destructive command except on a literal path you created.
- Never work around a denied action. Stop and report it.
- Never end a commit message with an attribution line that is not your own.
