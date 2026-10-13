# Fast brief: ID and title

*The fast path: this one file and the ruling it names replace SPEC, PLAN and spec QA for small bounded work the user asked for directly. It is never used for the full tier. It goes to main in the dispatch commit as `program/tasks/<ID>/BRIEF.md`; the gates read it from there and fail a branch that changes it, a brief without the four sections Scope, Assumptions checked, Decided and Stop and report when, and a `Ruling:` or `Tier:` line that does not say what the task's status record says.*

Ruling: R

Tier: standard

## Scope

*What changes, as a list a reviewer can check the diff against, and what deliberately does not.*

## Assumptions checked

*What the feasibility probe confirmed before this brief was written: the route exists, the data is readable, the local environment runs. One line each, with the command or file that showed it.*

## Decided (do not reopen)

*The design choices already made, each with its ruling. The implementer applies them and does not revisit them.*

## Stop and report when

*The conditions under which the implementer stops instead of deciding: a guarded or full-tier path must change, a dependency is needed, the scope turns out larger than stated.*

## Exact Commands

*One command per line, each beginning with a dollar sign and a space, optionally ending with ` # expect N`. They run without a shell. A standard-tier brief lists at least one; a light-tier brief may list none.*

## Return

*Final message: status, commits, one line of test results, concerns. Report file: the path the dispatch names, with numbered steps a person can follow to see the change work.*
