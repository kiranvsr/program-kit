# Playbook 04: architect (stage 4)

## Purpose

Decide how the system is shaped: components with boundaries, how data flows, how it deploys to each environment, and which technologies it uses and why. Also decide which named gates will prove the quality claims, and which commands the project runs to build, test and scan.

## Inputs

- Approved stages 1 to 3: `discovery.md`, `environments.yaml`, `product.md`, `SPEC.md`, `traceability.yaml`, `ledger.yaml`.
- `kit/interview/coverage.yaml` stage 4 and its seed prompts.
- `kit/presets/` if the project started from one.

## Lenses active

Architect owns candidate approaches, components, cross-cutting concerns and technology decisions. Delivery owns deployment and environments. Engineering owns quality gates and delivery mechanics. Program and project contribute and challenge.

## Steps

1. Run `pk interview next` and confirm stage 4.
2. Propose two or three candidate architectures with their trade-offs and a recommendation. Let the user choose. Record the choice as a decision in the ledger.
3. Define components `C1`, `C2` and so on, each with its boundary, the requirements it owns and where its code lives. Describe the data flow and the deployment shape for each environment in `environments.yaml`.
4. Cover the cross-cutting concerns: authentication and authorization, tenancy, observability, data lifecycle, configuration, secrets.
5. Record each technology choice as an ADR in `program/adr/NNNN-title.md` with context, options considered, decision and consequences. A technology chosen without an alternative considered is a challenge finding.
6. Write `program/gates.yaml`: one entry per gate `G-<NAME>` with its claims, the environment it needs (a name from `environments.yaml`) and the records that will prove it. A claim that needs an environment with `exists: false` stays unclaimable. Write `program/ARCHITECTURE.md`.
7. Fill `program/checks.yaml` with argv lists (never shell strings): `build`, `test`, `scan`, `compile_check`, `extra_gate` and `guarded_files`. Add the bytecode and build-output patterns the commands write to `.gitignore`. Run `pk doctor` to confirm the file reads.
8. Update `program/traceability.yaml`: add `components`, give each requirement its `components`, list the gates. Run `pk trace validate --stage 4`.
9. Write back, get corrections, run `pk interview report 4`.
10. Challenge round: `pk interview challenge 4`, each lens in its own session, every row resolved or deferred with an owner and a date.
11. Run `pk interview validate` and get the user's approval. Then run `pk interview approve 4 --artifact program/ARCHITECTURE.md program/gates.yaml` followed by each ADR path, so the ADRs are hashed too.

## Outputs

`program/ARCHITECTURE.md`, `program/adr/*.md`, `program/gates.yaml`, `program/checks.yaml`, `program/traceability.yaml` (components and gates), `program/reports/stage-4.md`, `program/reports/stage-4-challenge.md`, `program/approvals/stage-4.json`.

## Done when

`pk interview approve 4` succeeds and `pk trace validate --stage 4` passes: every requirement names a component, every gate has a record, and `checks.yaml` lists real build, test and scan commands.

## Next playbook

`05_freeze_review.md`. Confirm with `pk interview next`.
