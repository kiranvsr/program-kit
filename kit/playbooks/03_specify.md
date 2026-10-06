# Playbook 03: specify (stage 3)

## Purpose

Turn the journeys into numbered requirements that can be built and tested. Every requirement traces to a journey (or to a cross-cutting concern), and every non-functional requirement has a measurable target or a named owner for the missing target.

## Inputs

- Approved stages 1 and 2: `discovery.md`, `product.md`, `environments.yaml`, `traceability.yaml`, `ledger.yaml`.
- `kit/interview/coverage.yaml` stage 3 and its seed prompts.

## Lenses active

Architect owns the whole stage: functional requirements, data, integrations, non-functional targets and failure behaviour. Program, delivery and engineering contribute and challenge.

## Steps

1. Run `pk interview next` and confirm stage 3.
2. Run the interview loop as in `01_discover.md` step 3. Tag any entry that names an outside system `external_system`, so the integrations topic applies.
3. Write requirements `REQ-001`, `REQ-002` and so on. Each covers a journey step: rule, input, output, validation and error. A requirement that serves no journey must say which cross-cutting concern it serves.
4. Write non-functional requirements `NFR-001` and so on for scale, availability, latency, security, compliance, tenancy, data retention and residency. Each has a measurable target or an explicit "no target yet" with an owner.
5. Write the domain objects and interfaces, the data (entities, owners, volumes, sensitivity, retention, source of truth), each integration (direction, protocol, authentication, limits, owner, what happens when it is down), security and tenancy boundaries, failure modes and recovery, and observability.
6. Close every open question by an answer or a ruling (`pk rulings add --role interviewer --question Q --decision D`). Write `program/SPEC.md`.
7. Add the requirements to `program/traceability.yaml` (`requirements:` with `journeys`, and `nfr: true` where it applies). Run `pk trace validate --stage 3`.
8. Write back and have the user correct it. Run `pk interview report 3`.
9. Challenge round: `pk interview challenge 3`, each lens in its own session, every row resolved or deferred with an owner and date.
10. Run `pk interview validate`, get the user's approval, then run `pk interview approve 3`.

## Outputs

`program/SPEC.md`, `program/traceability.yaml` (requirements), `program/ledger.yaml`, `program/reports/stage-3.md`, `program/reports/stage-3-challenge.md`, `program/approvals/stage-3.json`.

## Done when

`pk interview approve 3` succeeds and `pk trace validate --stage 3` passes: every journey lists a requirement, every requirement names a journey or has an `nfr` flag, and the two lists agree.

## Next playbook

`04_architect.md`. Confirm with `pk interview next`.
