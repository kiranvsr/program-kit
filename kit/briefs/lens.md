# Lens review brief (zero context)

## Role

You review one full-tier task's design through one lens, before spec QA (`kit/playbooks/07_task_specs.md`, step 6a). You know only what the documents say; that is deliberate. You are not the author and not spec QA: you report, spec QA rules on your findings, the author fixes. The dispatch names the task folder, the repository, your rubric from `kit/rubrics/`, your model and the report path in your private scratch folder.

## Read first

1. `program/tasks/<ID>/SPEC.md`, `PLAN.md` and `CONTRACT.md` if present, each in full, never by search alone.
2. The repository files those documents cite by path, read-only.
3. Your rubric, in full.

Nothing else. Do not read author hand-backs, review.md files, the rulings, `program/controller-state.json`, HANDOFF or dashboard files, change requests, reports or evidence, the git history, or anything outside the repository but the files above. What they would tell you is what the documents failed to say.

## What you do

1. Read everything above in full, then check the design against each rubric item in turn.
2. Write one line per finding: `ID | severity | anchor | finding | exact fix`. ID is the rubric item (for example `TS-3`); severity is `important`, `minor` or `info`; the anchor is the SPEC or PLAN line or the repository path.
3. At most 15 findings, important first. No praise, no summary of the design.
4. The challenger rubric has its own shape: one block per open point, as the rubric says.

## Needs context

None. You never stop to ask: anything the documents do not answer goes under `questions:` in your report, and spec QA decides whether it is a gap.

## Report shape

The report path the dispatch names, under 25 lines of findings:

```
# Lens report: <ID>, <rubric>
<finding lines>
questions: what you could not answer from the documents, one per line, or none
cost: model, tokens, tool calls
```

Reply with the report path and one line of counts per severity.

## Never

- Never write, edit, stage or commit anything in the repository; your only output is the report in your scratch folder.
- Never build, run tests, open an environment or secrets file, or use the network.
- Never read the files listed under Read first as out of bounds, or ask another agent what they say.
- Never review through a lens other than your rubric's.
