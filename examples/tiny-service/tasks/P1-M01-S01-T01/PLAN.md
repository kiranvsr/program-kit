# Task Plan: P1-M01-S01-T01 Health and echo endpoints

## Status And PR Boundary

In progress on `task/P1-M01-S01-T01`. One pull request: the three files named in the SPEC's scope. The work stops when the hand-back gate passes and the evidence commit is made.

## Work Breakdown

1. Write the failing tests for REQ-001 to REQ-005 and NFR-001.
2. Write `handle` until the function tests pass.
3. Add the handler, `make_server` and the command line until the socket tests pass.
4. Write `produce.py` with four planted bugs: a wrong health answer, an off-by-one on the limit, an uncaught bad-JSON error and a bind to every address.
5. Merge main into the branch, then run the gate from the main checkout.

## Exact Commands

Run from the root of the task worktree.

$ python3 -m unittest discover -s src
$ python3 src/tiny_service.py --help
$ python3 src/tiny_service.py --port abc # expect 2

## Definition Of Done

- The three commands above end as expected: 0, 0 and 2.
- Every failure-mode row in the SPEC has a passing test.
- The hand-back gate prints its OK line; the results record has every planted bug killed.
- The report is in `program/evidence/P1-M01-S01-T01/report.md`.

## Gate Evidence

Feeds G-P1 with the task's latest results record and QA's acceptance of it. It can honestly claim REQ-001 to REQ-005 and NFR-001 in the dev environment. Nothing is claimed for staging, which does not exist.

## Rollout And Rollback

Merge to main is the whole rollout; there is no deployment. Rollback is reverting the merge commit.

## Risks

- A planted bug that builds but is not caught: the gate fails on `planted_bugs_all_killed`.
- A leftover `__pycache__` failing the clean-tree check: the project git-ignores it.
