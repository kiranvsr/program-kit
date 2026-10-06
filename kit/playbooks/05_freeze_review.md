# Playbook 05: freeze review (stage 5)

## Purpose

Have someone other than the author check stages 1 to 4 together, fix what they find, and then freeze the specification. After approval, changes to the specification are change requests only.

## Inputs

- Approved stages 1 to 4 and everything they produced, including `program/ledger.yaml`.
- `kit/briefs/spec_qa.md`, the brief for the reviewer.

## Lenses active

All five. Each lens records its sign-off in the review. The reviewer is a separate agent, never the author of any stage artifact.

## Steps

1. Start a spec-QA agent that did not write any stage artifact (your most capable model). Give it `kit/briefs/spec_qa.md` and the paths of every artifact. It runs `pk trace validate --stage 5` and `pk interview validate` first.
2. The reviewer checks for: placeholders; contradictions; ambiguity; every journey covered by requirements; every requirement owned by a component; every quality claim owned by a gate; every non-functional requirement with a target or an owner; every environment claim consistent with `environments.yaml`; a complete product-surface checklist.
3. The reviewer writes `program/freeze/review.md` as a finding-to-fix table: number, severity, location, problem, exact fix.
4. The author fixes each finding and replies in the table with the fix. If a fix changes meaning, ask the user first.
5. The reviewer re-checks only the findings and any regression the fixes caused. Repeat from step 4 until no critical or important finding is open.
6. Settle every assumption. Each `A-` entry in the ledger is either confirmed by the user (`confirmed: true`) or replaced by a decision. None survives the freeze unconfirmed. Show the user the list in one batch.
7. Run `pk trace validate --stage 5`. Fix every problem it lists.
8. Run `pk interview challenge 5`. Each lens adds its sign-off line to the review and a row for any remaining gap.
9. The user reads the review and signs `program/freeze/signoff.md`: a dated paragraph in the user's own words saying the specification is accepted as frozen. Never write this for the user.
10. Run `pk interview report 5`, then `pk interview approve 5`. This freezes the specification.

## Outputs

`program/freeze/review.md`, `program/freeze/signoff.md`, `program/reports/stage-5.md`, `program/reports/stage-5-challenge.md`, `program/approvals/stage-5.json`.

## Done when

`pk interview approve 5` succeeds: no unconfirmed assumption, `pk trace validate --stage 5` passes, every lens has signed off and the user has signed. From now on a change of meaning to any approved artifact goes through `10_change.md`.

## Next playbook

`06_register.md`. Confirm with `pk interview next`.
