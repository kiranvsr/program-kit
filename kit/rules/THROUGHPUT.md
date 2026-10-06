# Throughput rules

Rework and token cost, not coding, are what slow a program down. These rules exist to remove rework. The rulings behind them are in `kit/rules/RULINGS_SEED.md`.

## Merge queue

- QA starts only on a branch that has merged current main and produced its evidence on that merge.
- When QA accepts, the controller merges that branch next. No other code merge to main happens in between. Documentation-only hand-off commits are fine.
- Accepted branches merge one at a time, in the order they were accepted.
- If main moved without touching a branch's hashed inputs and `git merge-tree --write-tree main <branch>` is clean, the branch does not re-merge or re-produce its evidence. Any overlap means merge main, re-produce, then QA.
- Stale evidence is never chased one merge at a time. Each merge appends the stale list, and the records are re-bound later in one batch by one runner.

## Hand-back gate before QA

- No QA round until the implementer has run `pk gate handback <ID> --branch task/<ID>` and its last line is the OK line. Exit code 0 alone is never accepted.
- A hand-back that fails the gate returns to the implementer without a QA round.

## Decide, don't ask

- Anything with precedent is decided by the agent and recorded as a deviation citing the precedent.
- Questions go to the controller only in four cases: a new dependency; a security boundary with no precedent; a change to user scope; two conflicting rulings. The controller answers with a numbered ruling.

## Fix rounds and scoped re-checks

- QA returns findings numbered, each with severity, location and the exact fix expected.
- The same implementer fixes them and hands back a finding-to-fix table: finding, fix, commit, command that shows it fixed.
- The re-check covers only those findings, the binding check and the hand-back gate. It does not repeat the full review.
- When the evidence binds at the branch's HEAD, QA verifies recorded gate output by hash and re-runs only the PLAN commands, the planted bugs and its own.

## Model tiers

- Tier 1, your most capable model: the controller, first-pass implementation, first-pass QA, specs and spec review, the interviewer and lens challenge rounds, and any re-check that escalates.
- Tier 2, a cheaper model: scoped re-checks of exact fixes, mechanical re-runs with a checklist and resume-only work. It reports ESCALATE for anything outside its checklist and never accepts on judgement.
- Never Tier 2 for first-pass work, a security ruling, or accepting a task with an open important finding.
- With one capable model, use it for everything. The controller records its tier mapping in HANDOFF when it takes over.

## Concurrency, waiting and reports

- At most three implementers and four agents at once (implementers, fix rounds and QA together). Work closest to merge goes first.
- Each agent has its own scratch folder named for its task and role.
- No polling. Run long commands in the background with output redirected to a file, and wait for completion. Never use sleep loops and never `tail -f`.
- Reports are short and use the fixed headings in the role's brief.
- Read the hand-off's rule sections and the rulings your SPEC cites, not the whole ruling log.
- Run the kit's `pk fetch` step in the background once per host at session setup, so no gate step ever waits on a download.

## Interruptions

- After a session or rate limit, void partial evidence by exact path, then resume each agent at the interrupted step with: resume from the last commit, re-run only the interrupted step.
- Never restart a task from scratch. Never resume an agent whose task has since merged.

## Repeated rejections become a gate rule

- When two rejections share a cause, stop fixing task by task. Add the cause as a check in the hand-back gate (a command in `extra_gate` in `program/checks.yaml`, or a new step in the gate), and record a ruling.

## Gates on a quiet main

- A gate that reads every record (a phase gate, a re-bind batch) is voided by any merge. While one runs, merge nothing else.
- Merge the feature tasks first. Then run the gates back to back on a quiet main, merging each result before the next gate starts.
- The controller commits `program/` hand-off files only after a merge, a ruling or at session end, and evidence closures exclude them.
- A successor verifies that the repo-wide suite passes on main before it dispatches anything.
