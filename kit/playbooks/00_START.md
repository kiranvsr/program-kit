# Start here

## Purpose

This is the entry point. You are an agent that has just opened this project. In five minutes you will know what state the program is in and which playbook to open. Do not open any other playbook until you have done the steps below.

## Inputs

- The repository, with `kit/` (owned by the kit, replaced on upgrade) and, once started, `program/` (owned by the project).
- `program/HANDOFF.md` if it exists. It is the single document a successor needs.
- Your role: controller, interviewer, implementer, QA or spec QA. If you are not told, you are the controller and the interviewer.

## Steps

1. Set up the command. `pk` in every playbook means `python3 kit/scripts/pk.py`. Make it a shell alias or type it in full. Run `pk doctor`. It checks Python 3.11 or later, git, and that `program/checks.yaml` reads. If `program/` does not exist, ask the user for a brief of a few lines, then run `pk init` (add `--preset go`, `--preset node` or `--preset python` to start `checks.yaml` from a preset) and put the brief in `program/BRIEF.md`.
2. Check the hand-off is current: run `pk handoff check`. If it exits 1, run `pk handoff update`, then commit the result.
3. Find the stage: run `pk interview next`. It prints `next stage: N (name)` and how many topics are closed, or `all stages approved`.
4. Open the playbook for that result from the table below. Stages 1 to 6 are an interview with the user. Stage 7 and execution are the build loop.
5. Open the brief for your role in `kit/briefs/` (controller, implementer, qa or spec_qa) and read it. Read `kit/rules/HARD_RULES.md`. They bind you in every playbook.
6. If you are taking over from another agent, read `program/HANDOFF.md` top to bottom and follow `09_handoff.md` before you do anything else.
7. If the user asks to change something that is already approved at stage 5 or later, stop and follow `10_change.md`. Do not edit the artifact.

| `pk interview next` says | Open |
|---|---|
| stage 1 (discover) | `01_discover.md` |
| stage 2 (define the product) | `02_define_product.md` |
| stage 3 (specify) | `03_specify.md` |
| stage 4 (architect) | `04_architect.md` |
| stage 5 (freeze review) | `05_freeze_review.md` |
| stage 6 (roadmap and register) | `06_register.md` |
| stage 7 (task specs) | `07_task_specs.md`, then `08_execute.md` |
| all stages approved | `08_execute.md` |

A stage that was approved and then modified is named again by `pk interview next`. Its message says whether to run `pk interview amend N` (a correction that does not change meaning) or to raise a change request.

## Outputs

- You know the stage, your role and the playbook to follow.
- `program/HANDOFF.md` is current.
- You have told the user in a few lines what you found and what you will do next.

## Done when

You have opened the playbook the table names and have read the brief for your role.

## Next playbook

One of `01_discover.md`, `02_define_product.md`, `03_specify.md`, `04_architect.md`, `05_freeze_review.md`, `06_register.md`, `07_task_specs.md`, `08_execute.md`, as the table says. Later playbooks: `09_handoff.md` for takeover and `10_change.md` for changes after approval.
