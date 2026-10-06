# Roadmap

*Stage 6 artifact. The register holds the tasks; this document holds the shape.*

## Walking Skeleton

Phase 1 is the walking skeleton: the service answering /health (J1) and /echo (J2) on the loopback address of a developer machine, the only environment that exists. Done means both journeys work end to end through the one component C1, with unit tests and QA acceptance for development.

## Phases

- **P1 Walking skeleton.** Journeys J1 and J2. It is the only phase: the first release is small enough to be one. Later release secured (authentication and TLS) is not planned yet.

## Milestones

- **P1-M01 Health, echo and access log.** Gate G-P1: the results and acceptance records of both tasks, in the dev environment.

## Capacity And Cadence

One implementer at a time, tasks of size S or M. The implementer consults the controller only for a new dependency, a security boundary without precedent, a change to user scope or two conflicting rulings. The controller reports after each merge.

## Parked Items

None. No task needs a missing environment, vendor or decision.
