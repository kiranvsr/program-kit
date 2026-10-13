# Common rules of this program

Every brief points here instead of repeating it, and every agent reads it at every dispatch: keep it under 3 KB. The rules are in `kit/rules/RULES.md`; this file holds only what is particular to this program.

## Run

*How to build, test and start the project on a developer machine: the commands, the ports, and the one local stack that may run at a time.*

## Constraints

*What holds for every task here: languages and versions, no new dependency without a ruling, paths nobody edits, the full-tier paths (`tiers.full_paths` in `program/checks.yaml`).*

## Models

Every dispatch names its model. The model each class of controller rule C4 maps to here (one model may fill several):

| Class | Model |
|---|---|
| A | |
| B | |
| C | |
| D | |

## Report contract

Final message: status, commits, one line of test results, concerns. Full report: the file the brief names, fixed headings, under 25 lines, ending with a Cost line.

## Known failures on main

*Failures that exist on clean main today, each with the commit it was reproduced at and its owner. One listed here is not yours to fix, and it is never recorded as a pass (seed ruling K27).*
