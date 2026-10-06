# Frequently asked questions

Throughout, `pk` means `python3 kit/scripts/pk.py`. `docs/DESIGN.md` has the full design; this page answers the questions people ask first.

## Why is there only a strict process? Where is the lite mode?

Every task, however small, goes through the same lifecycle: a SPEC and PLAN accepted by a second reader, an isolated branch, a hand-back gate, commit-bound evidence, independent QA and a one-at-a-time merge. The strict process looks heavy for a one-line change, but most of its checks are scripts, and the expensive part (a second human or agent reading) is spent only where a second reader catches things the author cannot. A lite path is where trust leaks: the first task that "doesn't need" independent QA is the one nobody can vouch for later, and a program that mixes two standards cannot say what "done" means. If a task really is trivial, the strict process makes it a small task, not an exempt one.

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

Open `kit/playbooks/09_handoff.md`; it contains the successor prompt to give the incoming agent. In short: read `program/HANDOFF.md` top to bottom, then the role briefs in `kit/briefs/` and the three rule files in `kit/rules/`. Verify the state before acting: `git status` and `git branch --no-merged main`, `pk worktree list` with `git status --short` in each worktree (an uncommitted `results.json` or claim file is partial evidence: void it by exact path, never commit it), leftover processes and disk, and that the project's own test commands pass on a clean main. Run `pk handoff check` (and `pk handoff update` if it exits 1). Record your own model-tier mapping in the hand-off, then resume at its next steps. Never restart an accepted task, and never resume an agent whose task has already merged.

## `kit/scripts/trace.py` has the same name as a standard-library module. Is that a problem?

No. The kit never imports the standard-library `trace`, and every kit script runs with `kit/scripts` first on the import path (`python3 kit/scripts/pk.py` puts the script's own directory first, and the tests put it first explicitly), so `import trace` inside the kit always means the traceability script. The only way to get the wrong one is to run Python from another directory with its own path arranged differently and import both, which the kit never does. The name is kept because it says what the script does.

## Why must a project git-ignore bytecode and build output?

Evidence is produced only from a clean working tree, because a record "bound to one commit" is a lie if untracked files influenced the run. The clean-tree check treats any untracked, non-ignored file as dirt, so a test run that leaves `__pycache__/`, `*.pyc`, `dist/`, `target/` or `node_modules/` behind makes evidence production fail. Put those patterns in `.gitignore` before your first commit. `install.sh` and `pk init` add `__pycache__/`, `*.pyc` and `.worktrees/` for you when they are missing (and `pk` itself never writes bytecode); the build output of your own language is yours to add. This is a loud failure on purpose; the quiet alternative, ignoring untracked files, would hide real leftovers too.

## Why must the primary branch be called `main`?

The gates, `pk worktree new` and `pk handoff` all compare against `main`, and the gate reads its rules from it so that a task branch cannot change the rules it is judged by. A fixed name keeps that guarantee simple: there is no option to point the gate at another branch. `pk doctor` refuses a repository with no `main` and says how to fix it (`git branch -m main`), and `pk worktree new` and `pk handoff` give the same one-line answer.

## Why can `depends_on` name a phase, a milestone or a step as well as a task?

Because a task often waits for a whole group of work ("every task in the walking skeleton"), and listing each member would bump into the limit of six dependencies per task, which keeps every task's waiting list short enough to reason about. This is a convention only. The register validator accepts any ID that exists in the register (and rejects unknown ones); it does not compute what a group ID means, and no script blocks a task whose group is unfinished. The rule is for the controller: a task is ready only when every `depends_on` task is `done`, and for a phase, milestone or step ID, when every task under it is `done` (`pk status list` shows the tasks; a group has no status record of its own).

## What can the hand-back gate not detect, and why does QA re-run the evidence?

Two things. First, a `produce.py` that hand-writes a well-formed `results.json` instead of really running the commands: the gate checks the record's shape, its clean-tree and commit binding, and that the commands are as expected, but it cannot know the numbers were earned. Second, files written to git-ignored paths: they are invisible to the clean-tree check. The gate is a mechanical filter that catches the common misses cheaply, so QA does not spend a round on them. It is not a proof. That is why QA is a different agent that never saw the implementer's intentions: it runs `pk gate binding` to confirm the record still binds the branch, re-runs the PLAN commands itself, and plants its own bugs to see whether the tests catch them.

## Why is nothing ever downloaded inside a gate?

A gate that waits on the network is slow and flaky, and a download inside a check can change what is being checked. Anything fetched (toolchains, pinned archives, fixtures) goes through `pk fetch --pins <file.json>`, which stores it once per host in a cache addressed by sha256 (`$PROGRAM_KIT_CACHE`, default `~/.cache/program-kit/fetch`). Gate and validator steps only run `pk fetch verify`, which checks the cache and, if something is missing, fails and prints the fetch command to run. Run the fetch step in the background at session setup. The evidence stays bound to digests, so caching changes nothing about what is proven.

## What does "accepted for development" mean, and how are failures and missing environments recorded?

"Accepted for development" is the strongest thing QA is allowed to say: an independent agent re-ran the work against the SPEC and PLAN, in the environments that really exist (normally a developer machine), and found it good. It says nothing about production, staging or any environment `program/environments.yaml` marks as non-existent; those claims stay unclaimable, and work that needs them stays `blocked_by` the missing environment until it exists.

Two different things keep the record honest, and only one is computed. The claim documents (`pk gate claims`, checked by `pk gate validate`) are computed by a script from the gate's acceptance files and `environments.yaml`: per environment, a claim can be other than `not_evaluated` only for the gate's own environment and only if that environment exists. Then it is `blocked` when the acceptance lists the environment in its `blocked_by`, `evidenced` when the acceptance has decision `accepted_development` and is bound to the current evidence file, and `not_evaluated` otherwise. There is no "failed" state; a gap shows as blocked or not evaluated, never as a pass. Separately, `kit/rules/EVIDENCE_HONESTY.md` and the seed rulings say what to do when a PLAN command fails for a reason the task did not cause (for example it fails identically on a clean main): record it as a named environmental deviation with the real exit code, a reproduction on clean main, the failure message asserted, nothing claimed from it, and an owner. That is a rule QA enforces by reading, not something a script checks. The point of the kit is that a green mark is worth something; an honestly recorded gap costs a follow-up, and a flattering record costs the trust in every other one.
