# Playbook 02: define the product (stage 2)

## Purpose

Say who uses the product and what they do with it, end to end, in terms a user would recognise. The result is a set of personas and journeys, and a decision on every part of the product surface (onboarding, operations, support and so on) so nothing is forgotten by accident.

## Inputs

- Approved stage 1: `program/discovery.md`, `environments.yaml`, `CLAIM_BOUNDARIES.md`, `ledger.yaml`.
- `kit/interview/coverage.yaml` stage 2 and its seed prompts.

## Lenses active

Program owns personas and journeys. Delivery owns the product surface and the release shape. Architect and engineering contribute: they raise what each journey needs from the system.

## Steps

1. Run `pk interview next` and confirm stage 2. Read the stage 2 topics.
2. Run the interview loop as in `01_discover.md` step 3: ask, update the ledger, let each lens nominate one question, ask the best one. Record assumptions in preference to asking where a default is sensible.
3. Personas get IDs `U1`, `U2` and so on, each with goals, frequency, expertise, context and main frustration.
4. Journeys get IDs `J1`, `J2` and so on, each with trigger, steps, end state and failure paths. Choose the walking-skeleton journey: the one thin journey that will cross every component.
5. Walk the product surface checklist: onboarding, authentication and authorization, administration, billing or entitlement if any, operations (deploy, backup and restore, monitoring, alerting), data lifecycle, documentation, support. Mark each item in the first release, in a named later release, or out with a reason.
6. Define the first release as a set of journeys and name the later releases. Write `program/product.md`.
7. Start `program/traceability.yaml` with the journeys (`journeys:` section: ID, title, empty `requirements:` list). Run `pk trace validate --stage 2`.
8. Write back your understanding and have the user correct it. Run `pk interview report 2`.
9. Challenge round: run `pk interview challenge 2`. Each lens reviews `product.md` and the ledger against its charter, in its own session, and adds a row for every gap. Resolve each with the user or defer it with an owner and a date. Write the resolution in the row. A row with an empty resolution blocks approval. A lens with no findings writes `No findings.` under its table; the untouched skeleton does not count as a round.
10. Run `pk interview validate`, get the user's approval, then run `pk interview approve 2`.

## Outputs

`program/product.md`, `program/traceability.yaml` (journeys), `program/ledger.yaml`, `program/reports/stage-2.md`, `program/reports/stage-2-challenge.md`, `program/approvals/stage-2.json`.

## Done when

`pk interview approve 2` succeeds and `pk trace validate --stage 2` passes.

## Next playbook

`03_specify.md`. Confirm with `pk interview next`.
