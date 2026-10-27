# Review rubrics

Short, single-lens rubrics for zero-context reviewers (sometimes called goldfish reviewers: they remember nothing but what they are shown). Each reviewer follows `kit/briefs/lens.md` and gets only the task's SPEC.md, PLAN.md, CONTRACT.md if there is one, the repository files those documents cite, and one rubric. It gets no rulings, no conversation, no author hand-back and no controller summary: what the SPEC does not say, the reviewer cannot know, and that is the point. A gap it finds is a gap an implementer would meet.

Playbook 07, step 6a, runs them on full-tier tasks after the author hands back and before spec QA; spec QA then rules on every finding (`kit/briefs/spec_qa.md`, phase 2).

| Rubric | Lens | Class |
|---|---|---|
| tenancy-security.md | tenancy, access, secrets and personal data | A |
| failure-modes.md | failures, partial work, concurrency, time and limits | C |
| challenger.md | argues against each open-point recommendation before the user sees it | C |

Every finding names its rubric item (for example `FM-4`), a severity (`important`, `minor`, `info`), the SPEC or PLAN line or repository path it rests on, and the exact fix where one exists.

A program may add its own rubric here only by a ruling; each one costs a full read of the documents, so a lens earns its place by finding what spec QA does not.
