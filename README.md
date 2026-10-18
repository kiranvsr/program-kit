# program-kit

Imagine handing a few lines describing what you want built to a team of AI agents and getting back not a pile of code, but a finished, checked piece of software with a paper trail you can trust. program-kit is the method that makes that possible: it asks you the questions a good team would ask, writes down every decision, has one agent build each piece and a different one check it, and keeps proof of every test next to the exact version it proved. If the work stops halfway, because a session ended or a subscription hit its rate limit, the next agent, on any model from any provider's subscription, reads one page and carries on safely where the last one left off. Nothing is pushed, deployed or declared finished without a check you can see, so you stay in charge while the agents do the work.

program-kit is a provider-neutral template for running a software program with AI agents at a quality bar that survives hand-offs between sessions, models and providers. You write a short brief; an agent interviews you stage by stage until the product is specified, architected and frozen by an independent review, then builds the tasks one at a time with independent QA and evidence bound to the commit it came from. Everything is plain Markdown and standard-library Python, and the state lives in your repository, so another agent can pick up where the last one stopped.

Throughout, `pk` means `python3 kit/scripts/pk.py`.

## What the kit gives you, and why it helps

The problems the kit is built against are the ones a long program run by agents actually meets: a session ends mid-task and the next one cannot find its place; an agent reports a pass that never ran; a reviewer reviews one commit and a later one lands; cleanup removes the wrong thing; and the bill grows because every step is re-explained from scratch. Each feature below answers one of those.

| Feature | What it does | How it helps |
|---|---|---|
| Staged interview with role lenses | Turns a brief of a few lines into discovery, product definition, specification, architecture, a frozen review and a register of tasks with stable IDs, each stage approved by you and challenged by every lens before it closes. | You do not write the spec; you answer questions and approve. What is decided is written down before anything is built, so the build does not re-decide it. |
| Change requests after the freeze | Once stage 5 is signed, a change to the specification goes through a change request with its own record. | Scope moves on purpose, with a reason and a date, instead of drifting one task at a time. |
| Rulings log and hand-off | Every decision is a numbered ruling in an append-only log; the hand-off document is regenerated from state, kept under 60 KB, and shows the newest fifteen rulings and what is going on now. | A new session, model or provider picks up in minutes from one file, and nobody has to remember why something was decided. |
| One command per controller step | `pk record --ruling TEXT` appends the ruling, updates the state, regenerates the hand-off and commits exactly those files. | Bookkeeping cannot be skipped or half-done, and it costs one command instead of five edits. |
| Worktree per task, merges one at a time | `pk worktree new` gives each task its own branch and directory; `pk merge` lands one accepted branch at a time with a merge commit. | Tasks run in parallel without touching each other, and main only ever moves by one reviewed step. |
| Gates that read from main | The hand-back gate and the merge check run the project's build, test and scan commands and check the task's SPEC, PLAN and spec-QA review against main, not against the branch. | A branch cannot grade itself. What the gate checks is what was agreed, not what the implementer changed it to. |
| Evidence bound to the commit | Every gate pass is a record named by its own digest, committed and never edited, that names the commit and the inputs it came from; `latest.json` points at the newest one and the history is kept. | A claim of "tests pass" is a file anyone can re-check against a commit. A later change never alters an old record; `pk stale` reports which records have drifted from the current tree, for information. |
| Release fingerprint | `pk fingerprint run` runs the project's release set (`program/release-set.yaml`) on parallel tracks at one commit, derives a per-test table and writes one record, which the controller commits when it is complete and every command was as expected; `show` says how far the tree has moved since and `coverage` which rows of a task's latest record it covers. `parallel_steps: true` in `program/checks.yaml` runs the gate's test, scan and extra-gate steps concurrently. | "The whole release set passed at commit X" is a file, not a re-run of every task's commands, and a slow suite runs once per commit that matters instead of at every hand-back (working rule W12). |
| Independent QA and `done` | Only QA, in its own detached worktree, can accept a task; it re-runs the commands, plants bugs to see the tests catch them and checks scope. `done` is set by the acceptance record, not by a flag. | The agent that built the work is never the one that says it is finished, and a typed `done` is refused. |
| Verification tiers and the fast path | Every task is full, standard or light; the lighter tiers skip what a trivial change does not need, a ruling names which kinds of change qualify, and the paths that must always be full are listed in `program/checks.yaml`. | A typo and a tenancy change are not judged by the same process, so the careful process stays affordable where it matters. |
| Integration rounds | Several accepted branches below the full tier are verified together once, then still merge one at a time. | One combined test run instead of one per branch, without giving up the one-step merge. |
| Review of the exact commit | `pk worktree review` gives the reviewer a detached worktree at the reviewed commit and a diff file; `pk merge` refuses a branch that changed anything but its own evidence after that commit, and only genuine evidence records may be added there. | What was reviewed is what lands. A commit slipped in after the review is refused, not missed. |
| Safe finish | `pk worktree finish` removes a task's worktrees and merged branch and, given the exact compose project, the containers and volumes that carry its label. It refuses a worktree that holds work and never forces. | Disks and ports are reclaimed after every merge, and nothing is removed by a name that merely looks right. |
| Follow-ups and ad-hoc work | Follow-ups have IDs, areas and tags and are closed, tagged or marked duplicate; work outside the register is an ad-hoc task the hand-off shows like any other. | Nothing is lost between sessions, and work you asked for on the side is tracked without a change request. |
| Cost log | `pk cost add` records each agent run with its model and tokens; `pk cost report` flags an agent past 400,000 tokens or three resumes. | You see what each task cost, and when a fresh agent with a tight brief is cheaper than another resume. |
| Reading sets by role | Each role reads only its own brief, its part of the one rules file and `program/COMMON.md`; the hand-off playbook lists the files and their sizes, and a test keeps them under a ceiling. | Agents start with what they need and nothing else, which is faster and cheaper on every dispatch. |
| Provider-neutral, standard library only | Plain Markdown, Python with no dependencies, git. `program/COMMON.md` says which model fills which class in your program. | The same program can move between agents, models and providers; when one subscription hits its rate limit, the work resumes on another provider's subscription from the committed state, with nothing lost. There is nothing to install that can break or go stale. |
| Install, manifest and upgrade | `install.sh` copies the kit into an existing repository; a manifest records every kit file; `--upgrade` refuses when either copy has been changed. | You know the kit you run is the kit that was tested, and an upgrade never silently overwrites a local change. |

What it will not do is listed further down; the short version is that it never pushes, deploys, applies infrastructure or claims anything about an environment that does not exist.

## Five-minute start: a new repository

1. Create a new repository from this template (use the "use this template" feature of your git host, or copy the files into a fresh `git init`) and enter it. The primary branch must be called `main`; if `git init` gave you another name, run `git branch -m main`.
2. Create the project's working directory, choosing the preset for your language (`go`, `node` or `python`; leave `--preset` out to fill `program/checks.yaml` yourself):

   ```
   python3 kit/scripts/pk.py init --preset python
   python3 kit/scripts/pk.py doctor
   ```

   `init` also adds `__pycache__/`, `*.pyc` and `.worktrees/` to `.gitignore` if they are missing, so the first commit holds no bytecode. `doctor` prints `doctor: ok` when Python and git are usable, the branch is named `main` and `program/checks.yaml` lists at least one `build`, `test` and `scan` command (fill them in first if you left the preset out); otherwise it prints one `error:` line per problem. Without a preset, the three `checks.yaml` lines are expected until the architecture stage (stage 4) fills `program/checks.yaml`; pass `--preset` to start with working commands.
3. Replace the contents of `program/BRIEF.md` with your brief: anything from three lines to a full document.
4. Commit, then point your agent at `kit/playbooks/00_START.md` and tell it to follow it.

## Five-minute start: an existing repository

1. Your repository must be a git repository whose primary branch is called `main` (`git branch -m main` renames it; `pk doctor` refuses anything else). From a copy of this repository, install the kit into its top level:

   ```
   sh install.sh /path/to/your-repo --preset python
   ```

   This copies `kit/` into your repository (not the kit's own tests, which stay in the kit's repository), appends `__pycache__/`, `*.pyc` and `.worktrees/` to your `.gitignore` if they are missing (existing lines are left as they are), and, because `program/` does not exist yet, creates it from the templates with the chosen preset (`go`, `node` or `python`; leave it out to fill `program/checks.yaml` yourself). It commits nothing, and it refuses a copy of the kit that does not match its own manifest.
2. Check it:

   ```
   cd /path/to/your-repo
   python3 kit/scripts/pk.py doctor
   ```
3. Put your brief in `program/BRIEF.md`, then commit with `git add kit program .gitignore` and `git commit`, and point your agent at `kit/playbooks/00_START.md`. Running `pk` writes no bytecode, so the tree stays clean for the first gate.

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

Tasks are built in parallel, each by an implementer in its own worktree, reviewed by a different agent, and merged by a controller one at a time (`kit/playbooks/08_execute.md` has every command). The steps below are the full tier, the default.

1. Dispatch commit: the controller sets the task `in_progress` with its tier (`pk status set <ID> in_progress --role controller --tier <tier>`) and commits its SPEC, PLAN, spec-QA `review.md` and status file to main together, because the gate reads them from main. A task whose review record is not on main fails both gates with `FAILED: review program/tasks/<ID>/review.md is missing on main`.
2. `pk worktree new <ID>` creates the worktree and branch `task/<ID>`; the implementer builds test first.
3. The implementer writes an evidence producer, then runs the hand-back gate: `pk gate handback <ID> --branch task/<ID>`.
4. The gate's last line must be exactly `OK: <branch> passes the hand-back gate at <sha>`. Exit code 0 alone is never accepted; a failed gate returns the work with no QA round.
5. Evidence commit: the implementer sets the task `review` and commits the new results record (`program/evidence/<ID>/results/<utc>-<digest>.json`), `program/evidence/<ID>/latest.json`, its report and the status file, and nothing else.
6. Independent QA works in its own detached worktree (`pk worktree review <ID>`, which also writes the diff file it reviews), runs `pk gate binding <ID> --branch task/<ID>`, re-runs the commands, plants its own bugs and checks scope, then accepts with `pk evidence accept <ID> <file>` (the only way a task becomes `done`) or rejects with numbered findings.
7. Land: from main, `pk merge <ID>` refuses a branch QA has not accepted (it derives that from the evidence committed on the branch, and refuses work committed after the reviewed commit), runs the merge check, which must end with `OK: <branch> merges into main at <sha>`, merges with `git merge --no-ff`, rewrites the gate claims, regenerates the hand-off and commits the bookkeeping. The merge check is the controller's own run of the gate's checks: guarded files, the tier, the task's SPEC, PLAN and spec-QA `review.md` on main and unchanged, then the build, the tests and the scans on a trial merge.
8. `pk worktree finish <ID>` removes the task's worktrees and merged branch; it refuses a worktree that holds work and never forces. Given `--compose-project NAME` it also removes the containers and volumes that carry that compose project's label, exactly, and nothing chosen by name.

## Verification tiers

Not every task needs all of that, and the careful ones should not be judged by the same process as a typo. Every task has a tier, written in its SPEC, recorded in its status record at dispatch and changed only by a ruling:

| Tier | For | What it leaves out | Ends at |
|---|---|---|---|
| `full` | access control, tenancy, data migrations, cryptography, release, the kit's own gates; the default when unsure | nothing | `done`, set by QA |
| `standard` | ordinary work off those paths | QA's re-run of the suite and its planted-bug quota: one scoped review, the suite once at the gate and once at the merge check | `done`, set by QA |
| `light` | the kinds of change a ruling of your program names | tests, evidence and QA: build, compile check and scans, then the controller reads the diff | `merged`, set by the controller; never `done` |

A fast path (`--fast --ruling R<n>`) replaces SPEC, PLAN and spec QA with one brief and a ruling for small bounded work you ask for directly; it is never available for the full tier. Several accepted branches below the full tier can be verified together in an integration round (`pk integrate`) and still merge one at a time.

The gates enforce all of this from main, not from the branch. In a new program every task is full-tier: the lower tiers exist only once `tiers.full_paths` in `program/checks.yaml` lists the paths that must always be full, and a task below full that changes one of them fails. `docs/DESIGN.md`, section 10.1, lists every guard.

## Keeping a long program cheap

- One command per controller step: `pk record --ruling TEXT` appends the ruling, updates the state, regenerates the hand-off and commits exactly those files.
- The hand-off has a size budget (60 KB) that `pk handoff check` enforces; history lives in `program/rulings.md`, and the hand-off shows the newest fifteen rulings.
- Follow-ups have IDs and are closed, tagged or marked duplicate (`pk followups`); work outside the register is an ad-hoc task (`pk adhoc`) that the hand-off shows like any other.
- `pk cost add` records each subagent run with the model it ran on; `pk cost report` flags an agent past 400,000 tokens or three resumes, when a fresh agent with a tight brief is cheaper than another resume.
- `pk fingerprint run|plan|validate|show|coverage`: the release fingerprint of section 9.4 of `docs/DESIGN.md`. Run it after a merge that changes the release set or a suite it names, and before a release; keep the `test` step fast and put slow cases in their own tracks (working rule W12).
- Each role reads only its own brief, its parts of the rules file and `program/COMMON.md`. `kit/playbooks/09_handoff.md` lists the files and their sizes.

## Where evidence lives

Evidence records are committed, append-only files named by their own digest under `program/evidence/<ID>/results/` and `acceptance/`; the kit never modifies or deletes one. Each task's pointer `program/evidence/<ID>/latest.json`, the only evidence file that changes, names its latest record, and every check reads that one. `pk evidence history <ID>` shows every earlier record. A later change never makes a record stale; `pk stale` reports which records have drifted from the current tree, for information. A project made with kit 0.1 moves to the new layout with `pk evidence import` (see `docs/FAQ.md`).

## Where the rules live

The rules are stated once, in `kit/rules/RULES.md` (hard rules, working rules, evidence honesty and the controller's rules, read by role); nothing else restates them. `kit/rules/RULINGS_SEED.md` holds the starting rulings K1 to K44 with the reason for each, and `program/COMMON.md` holds what is particular to your program: how to run it, its constraints, which model fills which class, and the failures already known on main. Your own decisions go in `program/rulings.md` with `pk rulings add`; a ruling never contradicts an earlier one silently, it supersedes it by name. `program/HANDOFF.md` carries the state of the program and points at the rules rather than copying them. The design and the reasons behind each rule are in `docs/DESIGN.md`; `docs/FAQ.md` answers common questions.

## What the kit will not do

- It never pushes, deploys or applies infrastructure; that is always your decision.
- It never claims anything about an environment that does not exist. Environments are listed in `program/environments.yaml`, and work that needs a missing one stays blocked.
- It never marks a task `done` unless the latest acceptance record accepts and binds the latest, valid results record, and it refuses that move for any role but QA. A light-tier task, which has no QA, is recorded `merged`, never `done`, and no gate claim counts it.
- It never applies the fast path, the light tier or an integration round to a full-tier task, never force-removes a worktree, and never merges two branches in one command. The role is a flag the caller passes, so keeping QA a different agent from the implementer is the controller's job, not something the kit can verify. "Accepted for development" is never "ready for production".
- It never writes evidence from a dirty tree, never modifies or deletes an evidence record, never downloads inside a gate, and never rewrites history.

## Upgrading

Run `sh install.sh /path/to/your-repo --upgrade` from a newer copy of the kit. The installer first checks the new copy against its own manifest and the installed `kit/` against its `kit/MANIFEST.sha256`, and refuses if either does not match (for the installed one, if you changed anything under it, a file you added under `kit/tests` included), then replaces `kit/` wholesale; a `kit/tests` left by an older install goes with the old kit. It also appends `__pycache__/`, `*.pyc` and `.worktrees/` to `.gitignore` if they are missing, exactly as a fresh install does (existing lines are left as they are), and it never reads, writes or lists `program/`. Keep your own changes in `program/`, not in `kit/`.

From 0.2 to 0.3 nothing in `program/` has to change: every existing task reads as full-tier and the 0.2 controller state reads as it is. To use what is new, add `program/COMMON.md` from `kit/templates/COMMON.md`, add a `tiers` section to `program/checks.yaml`, and point your agents at `kit/rules/RULES.md` in place of the three rule files it replaces. `CHANGELOG.md` has the steps.

## Requirements

Python 3.11 or newer (standard library only), git, and a POSIX shell for `install.sh`. The kit's own tests run in the kit's own repository (they need its `install.sh`, `examples/`, `docs/` and `LICENSE`, and are not installed into projects): `TMPDIR=$(mktemp -d) python3 -W error -m unittest discover -s kit/tests`.

## Licence

MIT. See `LICENSE`.
