# Spec review: P1-M01-S01-T01

Reviewed by a spec QA agent that wrote neither file, against the register text, ARCHITECTURE.md and the traceability matrix.

| # | Severity | Location | Problem | Exact fix | Author reply |
| --- | --- | --- | --- | --- | --- |
| 1 | important | PLAN.md, Exact Commands | No command shows the `# expect N` form for a command that must fail | Add the bad `--port` command with `# expect 2` | Fixed: added |
| 2 | minor | SPEC.md, Failure Modes | The process-down row names no test | Say why it has none | Fixed: stated in the row |

Decision: accepted. Both findings closed, and the closed IDs match the traceability matrix.
