# program-kit

program-kit is a provider-neutral template for running a software program with AI agents at a quality bar that survives hand-offs between sessions, models and providers. You write a short brief; an agent interviews you stage by stage until the product is specified, architected and frozen by an independent review, then builds the tasks one at a time with independent QA and evidence bound to the commit it came from. Everything is plain Markdown and standard-library Python, and the state lives in your repository, so another agent can pick up where the last one stopped.

Throughout, `pk` means `python3 kit/scripts/pk.py`.

## Five-minute start: a new repository

1. Create a new repository from this template (use the "use this template" feature of your git host, or copy the files into a fresh `git init`) and enter it.
2. Create the project's working directory, choosing the preset for your language (`go`, `node` or `python`; leave `--preset` out to fill `program/checks.yaml` yourself):

   ```
   python3 kit/scripts/pk.py init --preset python
   python3 kit/scripts/pk.py doctor
   ```

   `doctor` prints `doctor: ok` when Python, git and `program/checks.yaml` are usable.
3. Replace the contents of `program/BRIEF.md` with your brief: anything from three lines to a full document.
4. Commit, then point your agent at `kit/playbooks/00_START.md` and tell it to follow it.

## Five-minute start: an existing repository

1. From a copy of this repository, install the kit into the top level of your project (it must be a git repository):

   ```
   sh install.sh /path/to/your-repo --preset python
   ```

   This copies `kit/` into your repository and, because `program/` does not exist yet, creates it from the templates with the chosen preset (`go`, `node` or `python`; leave it out to fill `program/checks.yaml` yourself). It commits nothing.
2. Check it:

   ```
   cd /path/to/your-repo
   python3 kit/scripts/pk.py doctor
   ```
3. Put your brief in `program/BRIEF.md`, commit `kit/` and `program/`, and point your agent at `kit/playbooks/00_START.md`.

A worked example with an end-to-end test lives in `examples/tiny-service/`.

## The stages

The interview is driven by five role lenses (architect, engineering manager, program manager, project manager, delivery manager) over a coverage map that says what must be known before a stage may close. `pk interview next` always names the stage to work on, and `00_START.md` says which playbook to open for it.

Stages 1 to 6 are approved one at a time with `pk interview approve N`, and from stage 2 on each ends with a challenge round: every lens reviews the draft in its own session before the user approves. A session can stop anywhere, because `pk interview next` resumes at the first unapproved or modified stage.

- Stage 1, discover: purpose, constraints and the environment matrix, which fixes what can never be claimed.
- Stage 2, define the product: personas, journeys and the product-surface checklist.
- Stage 3, specify: numbered requirements and measurable non-functional targets.
- Stage 4, architect: components, decisions recorded as ADRs and the gates that will prove quality claims.
- Stage 5, freeze review: an independent reviewer checks stages 1 to 4 together, every assumption is confirmed and the user signs. After this, changes go through change requests.
- Stage 6, roadmap and register: phases, milestones, steps and tasks with stable IDs, validated and turned into briefs.
- Stage 7, task specs: one SPEC and PLAN per task, written just in time. Stage 7 runs once per task and is never approved as a whole; seeing it in `pk interview next` means discovery is complete.

## The execution loop

Each task is built by an implementer in its own worktree, accepted by a different agent, and merged by a controller (`08_execute.md` has every command).

1. Dispatch commit: the controller sets the task `in_progress` and commits its SPEC, PLAN and status file to main together, because the gate reads them from main.
2. `pk worktree new <ID>` creates the worktree and branch `task/<ID>`; the implementer builds test first.
3. The implementer writes an evidence producer, then runs the hand-back gate: `pk gate handback <ID> --branch task/<ID>`.
4. The gate's last line must be exactly `OK: <branch> passes the hand-back gate at <sha>`. Exit code 0 alone is never accepted; a failed gate returns the work with no QA round.
5. Evidence commit: the implementer sets the task `review` and commits `results.json`, its report and the status file, and nothing else.
6. Independent QA runs `pk gate binding <ID> --branch task/<ID>`, re-runs the commands, plants its own bugs and checks scope, then accepts (the only way a task becomes `done`) or rejects with numbered findings.
7. Merge check: from main, `pk gate merge-check --branch task/<ID>` must end with `OK: <branch> merges into main at <sha>`. It is the controller's own run of the gate's checks: guarded files, the task's SPEC and PLAN unchanged, then the build, the tests and the scans on a trial merge.
8. Merge with `git merge --no-ff`, one accepted branch at a time.
9. Hand-off update: `pk handoff update`, `pk handoff check`, and commit `program/`.

## Where the rules live

The rules are in `kit/rules/`: `HARD_RULES.md`, `EVIDENCE_HONESTY.md` and `THROUGHPUT.md` bind every agent, and `RULINGS_SEED.md` holds the starting rulings K1 to K33. Your own decisions go in `program/rulings.md` with `pk rulings add`; a ruling never contradicts an earlier one silently, it supersedes it by name. `program/HANDOFF.md` carries the state of the program and points at the rules rather than copying them. The design and the reasons behind each rule are in `docs/DESIGN.md`; `docs/FAQ.md` answers common questions.

## What the kit will not do

- It never pushes, deploys or applies infrastructure; that is always your decision.
- It never claims anything about an environment that does not exist. Environments are listed in `program/environments.yaml`, and work that needs a missing one stays blocked.
- It never marks a task `done` without an acceptance record that accepts the current, valid evidence, and it refuses that move for any role but QA. The role is a flag the caller passes, so keeping QA a different agent from the implementer is the controller's job, not something the kit can verify. "Accepted for development" is never "ready for production".
- It never writes evidence from a dirty tree, never downloads inside a gate, and never rewrites history.

## Upgrading

Run `sh install.sh /path/to/your-repo --upgrade` from a newer copy of the kit. The installer first checks the installed `kit/` against `kit/MANIFEST.sha256` and refuses if you changed anything under it, then replaces `kit/` wholesale. It never reads, writes or lists `program/`. Keep your own changes in `program/`, not in `kit/`.

## Requirements

Python 3.11 or newer (standard library only), git, and a POSIX shell for `install.sh`. Run the kit's own tests with `TMPDIR=$(mktemp -d) python3 -W error -m unittest discover -s kit/tests`.

## Licence

MIT. See `LICENSE`.
