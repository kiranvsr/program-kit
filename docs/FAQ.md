# Frequently asked questions

Throughout, `pk` means `python3 kit/scripts/pk.py`. `docs/DESIGN.md` has the full design; this page answers the questions people ask first.

## Is there a lighter process for small or low-risk tasks?

Yes, since 0.3.0, and it is bounded by scripts rather than by judgement. Every task has a verification tier. `full` is the whole lifecycle and the default: anything touching access control, tenancy, data migrations, cryptography, release or the kit's own gates is full, and so is anything you are unsure about. `standard` keeps the gate, the evidence record and independent QA, but QA does one scoped review instead of re-running the suite and planting its own bugs. `light` runs the build, the compile check and the scans and has the controller read the diff: no tests, no evidence, no QA. A fast path replaces SPEC, PLAN and spec QA with one brief and a ruling for small work you ask for directly.

What stops the light paths from becoming where trust leaks: a new program has no lower tier at all until `tiers.full_paths` in `program/checks.yaml` says which paths are always full; the gates read a task's tier from main and fail a task below full that touches one of those paths; a branch cannot lower its own tier or switch one on; the fast path is refused for the full tier; and a light-tier task is recorded `merged`, never `done`, so "done" still means one thing: QA accepted it, with evidence. What the scripts cannot know is whether your `full_paths` list is complete. Keep it wide.

## What is a ruling, and why does a tier change need one?

A ruling is a numbered, dated decision in `program/rulings.md` that is never rewritten. A lower tier is a decision to check less, so it has to be findable later: the light tier is switched on by a ruling that names the kinds of change it covers, the fast path names the ruling that allowed it, and `pk status tier` refuses without one. `pk record --ruling TEXT` writes the ruling, updates the controller's state, regenerates the hand-off and commits, in one step.

## What does each agent have to read, and how much is it?

Its own brief, its parts of `kit/rules/RULES.md` and `program/COMMON.md`, plus the task's definition; `kit/playbooks/09_handoff.md` has the table with sizes, and a test keeps the table true. In 0.2 a controller read about 55 KB of kit files before the hand-off and an implementer about 20 KB plus the whole ruling log; in 0.3 the controller reads about 43 KB and a hand-off capped at 60 KB, an implementer about 19 KB (13 KB on the light tier), and nobody reads the ruling log top to bottom. The larger saving is in what no longer comes back: a subagent's final message is four things (status, commits, one test line, concerns) and its report is a file.

## Why does the kit never name a model, vendor or agent tool?

Because it has to work for whoever runs it, and because names go stale faster than process does. The playbooks are plain Markdown any agent can read, and the scripts are standard-library Python that run anywhere Python and git run. Where the process needs "a stronger reader" or "a cheaper re-checker", it says Tier 1 and Tier 2 and lets the controller record in `program/HANDOFF.md` which of its models plays which tier (a provider with one model uses it for both). Commit attribution lines are the agent's own, not something the kit dictates. `kit/tests/test_neutrality.py` fails the build if a vendor or model name appears anywhere in the kit, the docs or the README.

## Why a YAML subset, and what is in it?

The kit's scripts use only the Python standard library, which cannot read YAML, so `kit/scripts/miniyaml.py` reads a deliberately small subset: block mappings and sequences nested by spaces (a sequence may sit at its key's indent or deeper), `- key: value` items, the literals `[]` and `{}`, comments, and plain, single-quoted and double-quoted scalars (`true`, `false`, `null`, integers and floats are recognised). It rejects anchors, aliases, tags, flow collections, block scalars (`|` and `>`), tab indentation, duplicate keys and multi-line scalars, and it names the line number when it does. The subset is also a feature: the full language has surprises (a bare `no` or `on` becoming a boolean, a duplicate key silently winning) that a governance tool should not have. Files the kit writes read in any YAML library. Going the other way, a file from another tool reads here only if it stays inside the subset: block style throughout, with a list either indented under its key or written at the key's own indent (the default output of most YAML writers), and an optional leading byte-order mark. Flow collections (a list or mapping written inline in brackets or braces), anchors and block scalars are rejected with their line number, so rewrite them as block lists and plain or quoted strings.

## How do I add a preset?

A preset is a starting `checks.yaml`. If you only need it for your own project, you do not need a preset at all: edit `program/checks.yaml` directly. (Do not put your own preset under `kit/`; an upgrade replaces `kit/` wholesale.) To contribute a new one to the kit:

1. Copy `kit/presets/checks.python.yaml` to `kit/presets/checks.<name>.yaml` and fill `build`, `test`, `scan`, `compile_check`, `extra_gate` and `guarded_files`. Commands are argv lists, never shell strings; `build`, `test` and `scan` must each list at least one; `guarded_files` lists the build files, CI definitions and dependency manifests that only the user may approve changes to.
2. Add the name to `PRESETS` in `kit/scripts/pk.py` and to the two `go|node|python` lists in `install.sh` (the usage line and the `case` that validates `--preset`).
3. Add the name to the preset lists in `kit/tests/test_install.py`, then run the suite and `pk manifest write` (the manifest must match `kit/` for upgrades to work).

## How do I take over a program someone else was running?

Open `kit/playbooks/09_handoff.md`; it contains the successor prompt to give the incoming agent. In short: read `program/HANDOFF.md` top to bottom (it is kept under 60 KB), then `kit/rules/RULES.md`, `kit/briefs/controller.md` and `program/COMMON.md`. Verify the state before acting: `git status` and `git branch --no-merged main`, `pk worktree list` with `git status --short` in each worktree (an uncommitted `results.json` or claim file is partial evidence: void it by exact path, never commit it), leftover processes and disk, and that the project's own test commands pass on a clean main. Run `pk handoff check` (and `pk handoff update` if it exits 1). Write your own model mapping into `program/COMMON.md`, then resume at the hand-off's Now list and next steps. Never restart an accepted task, and never resume an agent whose task has already merged.

## `kit/scripts/trace.py` has the same name as a standard-library module. Is that a problem?

No. The kit never imports the standard-library `trace`, and every kit script runs with `kit/scripts` first on the import path (`python3 kit/scripts/pk.py` puts the script's own directory first, and the tests put it first explicitly), so `import trace` inside the kit always means the traceability script. The only way to get the wrong one is to run Python from another directory with its own path arranged differently and import both, which the kit never does. The name is kept because it says what the script does.

## Why must a project git-ignore bytecode and build output?

Evidence is produced only from a clean working tree, because a record "bound to one commit" is a lie if untracked files influenced the run. The clean-tree check treats any untracked, non-ignored file as dirt, so a test run that leaves `__pycache__/`, `*.pyc`, `dist/`, `target/` or `node_modules/` behind makes evidence production fail. Put those patterns in `.gitignore` before your first commit. `install.sh` and `pk init` add `__pycache__/`, `*.pyc` and `.worktrees/` for you when they are missing (and `pk` itself never writes bytecode); the build output of your own language is yours to add. This is a loud failure on purpose; the quiet alternative, ignoring untracked files, would hide real leftovers too.

## Why must the primary branch be called `main`?

The gates, `pk worktree new` and `pk handoff` all compare against `main`, and the gate reads its rules from it so that a task branch cannot change the rules it is judged by. A fixed name keeps that guarantee simple: there is no option to point the gate at another branch. `pk doctor` refuses a repository with no `main` and says how to fix it (`git branch -m main`), and `pk worktree new` and `pk handoff` give the same one-line answer.

## Why can `depends_on` name a phase, a milestone or a step as well as a task?

Because a task often waits for a whole group of work ("every task in the walking skeleton"), and listing each member would bump into the limit of six dependencies per task, which keeps every task's waiting list short enough to reason about. This is a convention only. The register validator accepts any ID that exists in the register (and rejects unknown ones); it does not compute what a group ID means, and no script blocks a task whose group is unfinished. The rule is for the controller: a task is ready only when every `depends_on` task is `done`, and for a phase, milestone or step ID, when every task under it is `done` (`pk status list` shows the tasks; a group has no status record of its own).

## What can the hand-back gate not detect, and why does QA re-run the evidence?

Two things. First, a `produce.py` that hand-writes a well-formed results record instead of really running the commands: the gate checks the record's shape, its clean-tree and commit binding, and that the commands are as expected, but it cannot know the numbers were earned. Second, files written to git-ignored paths: they are invisible to the clean-tree check. The gate is a mechanical filter that catches the common misses cheaply, so QA does not spend a round on them. It is not a proof. That is why QA is a different agent that never saw the implementer's intentions: it runs `pk gate binding` to confirm the record is a true record of the commit it names, re-runs the PLAN commands itself, and plants its own bugs to see whether the tests catch them. QA does not re-run the gate; the controller's `pk gate merge-check` runs its checks again (guarded files, SPEC and PLAN, build, tests, scans) on a trial merge before every merge, so a branch that skipped the gate does not reach main.

## Why is nothing ever downloaded inside a gate?

A gate that waits on the network is slow and flaky, and a download inside a check can change what is being checked. Anything fetched (toolchains, pinned archives, fixtures) goes through `pk fetch --pins <file.json>`, which stores it once per host in a cache addressed by sha256 (`$PROGRAM_KIT_CACHE`, default `~/.cache/program-kit/fetch`). Gate and validator steps only run `pk fetch verify`, which checks the cache and, if something is missing, fails and prints the fetch command to run. Run the fetch step in the background at session setup. The evidence stays bound to digests, so caching changes nothing about what is proven.

## What does "accepted for development" mean, and how are failures and missing environments recorded?

"Accepted for development" is the strongest thing QA is allowed to say: an independent agent re-ran the work against the SPEC and PLAN, in the environments that really exist (normally a developer machine), and found it good. It says nothing about production, staging or any environment `program/environments.yaml` marks as non-existent; those claims stay unclaimable, and work that needs them stays `blocked_by` the missing environment until it exists.

Two different things keep the record honest, and only one is computed. The claim documents (`pk gate claims`, checked by `pk gate validate`) are computed by a script from each task's latest acceptance and results records and `environments.yaml`: per environment, a claim can be other than `not_evaluated` only for the gate's own environment and only if that environment exists. Then it is `blocked` when the acceptance lists the environment in its `blocked_by`, `evidenced` when the latest acceptance has decision `accepted_development` and binds the task's latest results record, and `not_evaluated` otherwise. There is no "failed" state; a gap shows as blocked or not evaluated, never as a pass. Separately, `kit/rules/EVIDENCE_HONESTY.md` and the seed rulings say what to do when a PLAN command fails for a reason the task did not cause (for example it fails identically on a clean main): record it as a named environmental deviation with the real exit code, a reproduction on clean main, the failure message asserted, nothing claimed from it, and an owner. That is a rule QA enforces by reading, not something a script checks. The point of the kit is that a green mark is worth something; an honestly recorded gap costs a follow-up, and a flattering record costs the trust in every other one.

## Where does evidence live, and how do I see its history?

Each record is a committed file named by its own digest: `program/evidence/<ID>/results/<utc>-<sha256[:12]>.json` for results, `program/evidence/<ID>/acceptance/...` for QA's acceptances. Records are append-only: the kit never modifies or deletes one, and `pk evidence validate` (run by the hand-back gate and `pk doctor`) refuses a record whose bytes no longer match its name and a commit that modified or deleted one. `program/evidence/<ID>/latest.json` is the only evidence file that changes: it points at each task's latest results and acceptance record, and every check reads the latest record through it. `pk evidence history <ID>` lists every record of a task, newest first, marking the current ones; `git log -- program/evidence/<ID>/` shows when each arrived.

## Why does a later change not make a record stale?

Because the record never claimed the code would stay the same. It claims: at commit `head`, these inputs with these digests produced this outcome. That stays true forever. In 0.1 every record was bound to its inputs and checked against main, so a record went stale the moment anything it hashed moved, and acceptances that bound records made the graph cyclic; each merge became a batch of re-binds. Now a later change only makes a record drift from the current tree, and `pk stale` reports that drift (`fresh` or `drifted since <head>` with the changed inputs) as information; `--strict` turns it into an exit code if you want one. A task appends a new record when its own work changes (the hand-back gate asks for it then), never because another task touched a shared file. The latest record is what the program considers; the history keeps every earlier claim. A record binds only inputs that can change its outcome, never a derived output such as another record, an acceptance, a status file or a claim document; `pk evidence lint` names any that slip in.

## How do I move a 0.1 project to 0.2?

Upgrade the kit (`sh install.sh <repo> --upgrade`), then run `pk evidence import` in the project. It copies every `program/evidence/<ID>/results.json` and `acceptance.json` into the new layout byte for byte, as each task's first history entry (an acceptance's `evidence_sha256` therefore still binds), builds `program/evidence/<ID>/latest.json`, and prints a `git rm` line for each original. It never deletes them itself: check the import, run the printed `git rm` lines, and commit the new records, the pointers and the removals together. Producers call `Evidence.write()` with no path now. A `gates.yaml` record may keep its old `path: program/evidence/<ID>/results.json` (it names task `<ID>`) or say `task: <ID>`.

## How do I move a 0.2 project to 0.3?

Run `sh install.sh /path/to/your-repo --upgrade`. Nothing in `program/` has to change: a status record without a tier reads as `full`, a 0.2 `controller-state.json` reads as it is (follow-ups get IDs the first time a command writes the file), and the evidence store is untouched. Then, to use what is new: copy `kit/templates/COMMON.md` to `program/COMMON.md` and fill it; add a `tiers` section to `program/checks.yaml` (it is a guarded file, so the controller edits it on main) listing your full-tier paths; rename the Known gotchas heading of your hand-off to Known hazards if you like; and tell your agents that `kit/rules/RULES.md` replaces `HARD_RULES.md`, `EVIDENCE_HONESTY.md` and `THROUGHPUT.md`. If `pk handoff check` now fails on the size budget, move history into rulings and close or tag follow-ups; `handoff_budget_bytes` in the controller state raises the budget while you do.

## What are the known limits?

Five things the kit does not do, so you know where its checks stop.

- **Binding is not proof.** `pk gate binding` checks that the latest results record is committed with the digest its pointer names, names an ancestor of the branch tip, and that each recorded input hash is the hash of that file at that commit. A record written by hand, consistently, appended under its own digest and pointed at still binds. Binding does not catch that; the merge check does, because it runs the build, the tests and the scans again whatever the record says, and QA re-runs the evidence for the same reason.
- **Records are not signed.** Git guards the record history, but a history rewritten with its records is not detected, and nothing proves who wrote a record. Signing is a later addition.
- **Status files are not locked.** `pk status set` writes `program/status/<ID>.json` atomically, so a reader never sees half a file, but nothing stops two agents setting the same task's status at the same moment: the later write wins and the earlier history entry is lost. One task has one writer at a time. Two at once is a controller error, not something the kit detects.
- **A copy of the whole template counts its tests as the kit's.** Where a top-level `install.sh` sits beside `kit/` (the kit's own repository, or a project made by copying everything), `kit/tests` is treated as the kit's own test suite: the manifest check does not look at it, so files you add there are not reported. In a project installed with `install.sh`, a file added under `kit/tests` is reported and refuses an upgrade.
- **The suite has run on one Python version.** Python 3.11 is the stated floor and the code keeps to what 3.11 has, but the test suite has so far been run on 3.14 only. If you run it on another version and something fails, that is a bug in the kit.
