# Task Spec: ID and title

*Written when the task reaches the front of the queue, from the register text, the architecture and the traceability matrix. Reviewed by someone other than the author before dispatch.*

## Summary

*One paragraph: what this task delivers and why, in the words of the register entry.*

## Tier

*The verification tier, on the line below: `full` for anything touching access control, tenancy, data migrations, cryptography, release or the kit's own gates, and whenever unsure; `standard` for ordinary work off those paths; `light` only for the kinds of change this program's ruling names. Spec QA checks it, and the gates compare it with the status record.*

Tier: full

## Scope In

*What this task changes, as a list a reviewer can check the pull request against.*

## Scope Out

*What it deliberately does not change, including neighbouring work that belongs to another task.*

## Dependencies

*Tasks, decisions and environments this task needs, and the state each must be in.*

## Consumed Contracts

*The interfaces, schemas and architecture decisions this task relies on, each with the section that defines it.*

## Security And Tenancy

*How the change keeps the trust and tenancy boundaries of the specification, or a statement that it touches none.*

## Failure Modes And Recovery

*The ways this change can fail, how each is detected and how the system recovers.*

## Observability

*The logs, metrics and alerts this change adds or relies on.*

## Module Placement

*The paths where the new code lives and the modules that may and may not depend on it.*

## Assumptions Checked

*What the feasibility probe confirmed before this spec was written (the route exists, the data is readable, the local environment runs), one line each with the command or file that showed it. A task with a server part and a client part names its contract file here: `program/tasks/<ID>/CONTRACT.md`.*

## Decided (do not reopen)

*The design choices already made, each with its answer or the ruling that settled it. The implementer applies them and does not revisit them.*

## Stop and report when

*The conditions under which the implementer stops instead of deciding, beyond the four NEEDS_CONTEXT cases: a guarded or full-tier path must change, the scope turns out larger than stated.*

## Closes

*The requirement and journey IDs this task closes, one per line.*
