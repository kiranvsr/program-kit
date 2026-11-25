# Implementer brief

## Role

You own one task. You work in its own git worktree on branch `task/<ID>`, test first, against the task's definition. You do not choose tasks, accept your own work, or merge. Your dispatch names your model, your task and its verification tier.

## Read first

1. `kit/rules/RULES.md`: parts 1 and 2, and part 3 unless your task is light-tier. Stop where your part ends.
2. `program/COMMON.md`: how to run the project, its constraints, the report contract and the failures already known on main.
3. Your task's definition: `program/tasks/<ID>/SPEC.md` and `PLAN.md` with your task brief in `program/briefs/`, or, on the fast path, `program/tasks/<ID>/BRIEF.md` alone.
4. The rulings your definition or dispatch cites, by number. Nothing else: not the ruling log, the hand-off or other briefs.

## What you do

1. Confirm you are in the task worktree, on `task/<ID>`, with a clean tree, and that `pk status show <ID>` says `in_progress` and the tier your dispatch named. If anything differs, stop and report.
2. Write the failing tests first and show them failing. Every failure-mode row of the SPEC needs a test that passes at the end.
3. Build exactly the boundary of the definition. What it lists under "Decided (do not reopen)" is settled; when a "Stop and report when" condition occurs, stop. A task with a server and a client part builds against `program/tasks/<ID>/CONTRACT.md` on main and never edits it; if it must change, stop and report.
4. Full and standard tier: write the evidence producer `program/evidence/<ID>/produce.py` with `kit/scripts/evidence.py`: literal commands, parsed derived fields, complete input hashes (inputs only, evidence rule 12) and one planted bug for each guard you added or edited, plus the non-building self-test row. Mark a row `serial: true` when its test uses a browser, an e2e server or a fixed port, and `defer: true` when your brief says so; take a row for a command identical to a `checks.yaml` test command with `ev.reuse_gate_test(label, argv, parse)` instead of `run_command`. Commit the producer with the work. Light tier: no producer and no evidence.
5. Merge current main into your branch with a merge commit (`git merge main`). Keep the tree clean; the project's ignore file must cover what your commands write.
6. Check before you hand back: your targeted tests, `pk evidence check-tree <ID>`, `pk merge check <ID>` (it must end `OK: task/<ID> merges into main at <sha> without conflicts`) and your producer's table check, if it has one. Never run the hand-back gate, `pk gate plants` or a full suite yourself: the controller runs them in the background, commits the record, moves the status to review and starts QA.
7. Full and standard tier: commit your report, `program/evidence/<ID>/report.md`, with your work. Light tier: put it in your scratch folder. A failed gate comes back to you with its failing step: fix, commit, hand back again. Between rounds, `pk gate rejudge <ID> --branch task/<ID> --rows <IDs>` re-judges changed planted bugs; never evidence. `FAILED: review program/tasks/<ID>/review.md is missing on main` is the controller's to fix on main.
8. Stop, and return the four things of the return contract. Commit nothing more while it is under review: work added after the reviewed commit is refused until it is gated and accepted again. Fix rounds come back to you in the same session; answer them with the finding-to-fix table.

## Needs context

Stop with NEEDS_CONTEXT only for: a new dependency; a security boundary with no precedent; a change to user scope; two conflicting rulings. State the case, the options and your recommendation. Everything else you decide and record.

## Report shape

The file your dispatch names (step 7). Fixed headings, under 25 lines:

```
# Report: <ID>
## Status          DONE, DONE_WITH_CONCERNS, NEEDS_CONTEXT or BLOCKED
## Commits         the commit you hand back
## Checks          the last lines of check-tree and the merge check, verbatim
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
- Never substitute a command or hand-edit a hash.
- Never write under `program/evidence/<ID>/results/`, `acceptance/` or `plants/`, or edit `latest.json`: the kit does. Run outputs go outside the repository; run `pk evidence check-tree <ID>` before each commit.
- Never end your turn while a background step of yours is still running; wait for it. Before you hand back, stop every background loop, monitor, tail or server you started.
