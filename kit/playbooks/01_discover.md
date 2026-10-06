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
3. Run the loop. Ask the user. After each answer, update `program/ledger.yaml` with entries of four kinds: fact (`F-n`), assumption (`A-n`, with `confirmed` true or false), decision (`D-n`) and open question (`Q-n`, with the lens that raised it and `blocking`). Each entry has `topics`, `tags` (the `needs` tags it settles), `text`, a `source` and a date. Then take each lens in turn over the open topics. Each nominates at most one next question with its reason. Ask the one with the highest impact and uncertainty.
4. Prefer an assumption to a question where a sensible default exists. Show assumptions in batches for the user to correct. Batch closed questions three to five per message. Ask open questions one per message. Show progress each time ("14 of 22 topics closed"). The user may say "decide", "defer" (record a decision tagged `deferred` with an owner and a due date) or "take defaults".
5. When an answer names a regulated industry, personal data, payments, a government body, healthcare or outside customers, or an external system, tag the entry (`regulated`, `personal_data`, `payments`, `government`, `healthcare`, `external_customers`, `external_system`). The conditional topics then apply.
6. Write back your understanding in a short note and ask the user to correct it. Write the artifacts from the corrected note and the ledger: `program/discovery.md`, `program/environments.yaml` (one row per environment, `exists: true` or `false`, owner, notes) and `program/CLAIM_BOUNDARIES.md` (claimable in development, unclaimable until an environment exists, never claimed).
7. Run `pk interview report 1`. It writes `program/reports/stage-1.md` and prints questions asked against the budget. Run `pk interview validate` and fix every problem it lists.
8. Read the report to the user. Get explicit approval. Then run `pk interview approve 1`. It refuses while a required topic is open, a blocking question is unanswered or an entry is malformed, and names the reason.

## Outputs

`program/discovery.md`, `program/environments.yaml`, `program/CLAIM_BOUNDARIES.md`, `program/ledger.yaml`, `program/reports/stage-1.md`, `program/approvals/stage-1.json`.

## Done when

`pk interview approve 1` succeeds. Every applicable required topic is closed or deferred with an owner and a date, no lens holds a blocking question, and the best remaining questions would not change scope, architecture, plan or a gate.

## Next playbook

`02_define_product.md`. Confirm with `pk interview next`.
