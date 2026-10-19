# Start here

## Purpose

This is the entry point. You are an agent that has just opened this project. In five minutes you will know what state the program is in and which playbook to open. Do not open any other playbook until you have done the steps below.

## Inputs

- The repository, with `kit/` (owned by the kit, replaced on upgrade) and, once started, `program/` (owned by the project).
- `program/HANDOFF.md` if it exists. It is the single document a successor needs.
- Your role: controller, interviewer, implementer, QA or spec QA. If you are not told, you are the controller and the interviewer.

## Steps

1. Set up the command. `pk` in every playbook means `python3 kit/scripts/pk.py`. Make it a shell alias or type it in full. Run `pk doctor`. It checks Python 3.11 or later, git, and that `program/checks.yaml` reads. It also checks that the primary branch is named `main` (`git branch -m main` renames it) and that `program/checks.yaml` lists a build, a test and a scan command; run it again after you fill `checks.yaml`. In a project started without a preset its three `checks.yaml: ... must list at least one command` lines are expected until the architecture stage fills `program/checks.yaml` (`04_architect.md`, step 7); fix any other line first. If `program/` does not exist, ask the user for a brief of a few lines, then run `pk init` (add `--preset go`, `--preset node` or `--preset python` to start `checks.yaml` from a preset) and put the brief in `program/BRIEF.md`.
2. Check the hand-off is current and within its size budget: run `pk handoff check`. In a new program it says the hand-off has no generated block yet, because `pk init` and `install.sh` write none: run `pk handoff update` once and commit it. If it says stale, run `pk handoff update`, then commit the result. If it says over budget, trim it before you go on (`09_handoff.md`, step 2).
3. Find the stage: run `pk interview next`. It prints `next stage: N (name)` and how many topics are closed, and when the stage was approved and then modified it lists the changed files. Stage 7 is never approved: once stages 1 to 6 are approved and current, `next` prints stage 7 and stays there. Seeing 7 is normal and means discovery is complete. Act on discovery only when it names stage 6 or lower.
4. Open the playbook for that result from the table below. Playbooks 01 to 06 are the interviewer's brief: stages 1 to 6 are an interview with the user. Playbooks 07 and 08 are the build loop and run once per task.
5. Open the brief for your role in `kit/briefs/` (controller, implementer, qa or spec_qa) and read it, and only that one. Read `kit/rules/RULES.md`, the parts its table names for your role, and `program/COMMON.md`. They bind you in every playbook. `09_handoff.md` lists what each role reads, with sizes.
6. If you are taking over from another agent, read `program/HANDOFF.md` top to bottom and do the state check of `09_handoff.md` before you do anything else.
7. If the user asks to change something that is already approved at stage 5 or later, stop and follow `10_change.md`. Do not edit the artifact.

| `pk interview next` says | Open |
|---|---|
| stage 1 (discover) | `01_discover.md` |
| stage 2 (define the product) | `02_define_product.md` |
| stage 3 (specify) | `03_specify.md` |
| stage 4 (architect) | `04_architect.md` |
| stage 5 (freeze review) | `05_freeze_review.md` |
| stage 6 (roadmap and register) | `06_register.md` |
| stage 7 (task specs) | `07_task_specs.md`, then `08_execute.md`, once per task. Discovery is complete. |

A stage that was approved and then modified is named again by `pk interview next`. Its message lists the changed files and says to run `pk interview amend N` (a correction that does not change meaning) or to raise a change request. `pk interview approve N` also needs every stage below N approved and current, and names the lowest one that is not.

## Outputs

- You know the stage, your role and the playbook to follow.
- `program/HANDOFF.md` is current.
- You have told the user in a few lines what you found and what you will do next.

## Done when

You have opened the playbook the table names and have read the brief for your role.

## Next playbook

One of `01_discover.md`, `02_define_product.md`, `03_specify.md`, `04_architect.md`, `05_freeze_review.md`, `06_register.md`, `07_task_specs.md`, `08_execute.md`, as the table says. Later playbooks: `09_handoff.md` for takeover and `10_change.md` for changes after approval.
