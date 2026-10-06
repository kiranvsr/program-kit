# Playbook 01: discover (stage 1)

## Purpose

Learn what is to be built, for whom, under what constraints, and what exists today. The most important output is the environment matrix: which environments really exist. Anything that needs one that does not exist is unclaimable from the start.

## Inputs

- `program/BRIEF.md`, anything from three lines to a full document.
- Any existing document the user points at. Run `pk interview ingest <file>` for each. It stores the file under `program/sources/` and records it as a ledger source.
- `kit/interview/coverage.yaml` stage 1, `kit/interview/seeds.yaml` for example questions, and the lens charters in `kit/interview/lenses/`.

## Lenses active

Program owns the outcome, users, scope edges, risks and regulatory topics. Architect owns the existing landscape. Project owns constraints. Delivery owns environments. Engineering owns team and ways of working.

## Steps

1. Run `pk interview next`. Read the stage 1 topics in the coverage map. Each topic says what must be known for it to close.
2. Ingest every document the user supplied (`pk interview ingest <file>`). Map it onto the topics and write ledger entries with the document as their source. Ask only about what is still open.
3. Run the loop. Ask the user. After each answer, update `program/ledger.yaml`. Then take each lens in turn over the open topics. Each nominates at most one next question with its reason. Ask the one with the highest impact and uncertainty. The ledger has two parts:
   - `sources`: each source has a `kind` (`user`, `document` or `inference`), a `ref` (for example "message 3") and `at` (a date).
   - `entries`: each has `id`, `kind` (fact `F-n`, assumption `A-n`, decision `D-n`, question `Q-n`; the prefix must match), `topics` (coverage topic IDs), `tags` (the `needs` tags it settles), `text`, `source` (a key in `sources`) and `at`. An assumption adds `confirmed` (true or false), a decision adds `ruling` (a ruling number or null), a question adds `lens` and `blocking`. An entry may carry `answers` (the question IDs it answers). A deferral is a decision tagged `deferred` with `owner` and `due`.

```
sources:
  S1:
    kind: user
    ref: message 1
    at: "2026-10-06"
entries:
  - id: F-1
    kind: fact
    topics:
      - T1.1
    tags:
      - outcome
    text: Support agents should resolve tickets faster.
    source: S1
    at: "2026-10-06"
  - id: Q-1
    kind: question
    topics:
      - T1.6
    tags: []
    text: Is there a staging environment?
    source: S1
    at: "2026-10-06"
    lens: delivery
    blocking: true
  - id: D-1
    kind: decision
    topics:
      - T1.6
    tags:
      - deferred
    text: Staging is decided later.
    source: S1
    at: "2026-10-06"
    ruling: null
    answers:
      - Q-1
    owner: Dana
    due: "2026-11-01"
```
4. Prefer an assumption to a question where a sensible default exists. Show assumptions in batches for the user to correct. Batch closed questions three to five per message. Ask open questions one per message. Show progress each time ("14 of 22 topics closed"). The user may say "decide", "defer" (record a decision tagged `deferred` with an owner and a due date) or "take defaults".
5. When an answer names a regulated industry, personal data, payments, a government body, healthcare or outside customers, or an external system, tag the entry (`regulated`, `personal_data`, `payments`, `government`, `healthcare`, `external_customers`, `external_system`). The conditional topics then apply.
6. Write back your understanding in a short note and ask the user to correct it. Write the artifacts from the corrected note and the ledger: `program/discovery.md`, `program/environments.yaml` (one row per environment, `exists: true` or `false`, owner, notes) and `program/CLAIM_BOUNDARIES.md` (claimable in development, unclaimable until an environment exists, never claimed).
7. Run `pk interview report 1`. It writes `program/reports/stage-1.md`: which topics are open and which tags each one still needs, plus questions asked against the budget. Use it any time to see what is left. Run `pk interview validate` and fix every problem it lists.
8. Read the report to the user. Get explicit approval. Then run `pk interview approve 1`. It refuses while a required topic is open, a blocking question is unanswered or an entry is malformed, and names the reason.

## Outputs

`program/discovery.md`, `program/environments.yaml`, `program/CLAIM_BOUNDARIES.md`, `program/ledger.yaml`, `program/reports/stage-1.md`, `program/approvals/stage-1.json`.

## Done when

`pk interview approve 1` succeeds. Every applicable required topic is closed or deferred with an owner and a date, no lens holds a blocking question, and the best remaining questions would not change scope, architecture, plan or a gate.

## Next playbook

`02_define_product.md`. Confirm with `pk interview next`.
