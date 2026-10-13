# Program hand-off

Hand-written notes go in the sections below; `pk handoff` rewrites only what is between the markers. This file carries the state of the program, not its rules or its history, and `pk handoff check` fails when it grows past its budget (60 KB; Next steps past 2 KB): a different agent, model or provider must be able to read it whole and take over from it (`kit/playbooks/09_handoff.md`).

## How the program works

## Environment setup

## Known hazards

- A shell that drifted into a task worktree commits to the wrong branch: check `git rev-parse --show-toplevel` before you commit or delete.
- Fixed ports allow one local stack at a time: stop the last one before you start the next.
- A local stack left running for hours degrades: restart it before you debug it.
- An interrupted run leaves processes, containers and partial evidence behind: look for them at takeover, and remove only what is ours, by exact name.

## Next steps

## Where the rules live

`kit/rules/RULES.md` (the rules), `program/COMMON.md` (this program's run commands, constraints and model mapping) and `program/rulings.md` (every ruling; the newest are listed below).

<!-- generated:start -->
<!-- generated:end -->
