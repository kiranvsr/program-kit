# Changelog

## 0.3.0

Faster and cheaper runs without a weaker process. The evidence model of 0.2.0 is unchanged (committed, append-only records; latest wins; a record binds only its inputs), and hard rules 1 to 20 and the twelve evidence rules are word for word what they were. Nothing in `program/` has to change to upgrade.

What changed:

- **One rules file.** `kit/rules/RULES.md` replaces `HARD_RULES.md`, `EVIDENCE_HONESTY.md` and `THROUGHPUT.md`: part 1 hard rules, part 2 working rules for every agent, part 3 evidence honesty, part 4 the controller's rules. The briefs, the playbooks, the seed rulings, `docs/DESIGN.md`, the README and the successor prompt point to it instead of restating it. Each role reads its own brief, its parts of the rules and `program/COMMON.md`; `kit/playbooks/09_handoff.md` lists the files with sizes, and a test checks the table.
- **Verification tiers.** A task is `full`, `standard` or `light`, recorded in its status record at dispatch and changed only by a ruling (`pk status set --tier`, `pk status tier`). The gates read the tier from main and enforce it. A light task ends at the new status `merged`.
- **Fast path.** `--fast --ruling R<n>`: one `BRIEF.md` and a ruling replace SPEC, PLAN and spec QA. Refused for the full tier by the status machine and both gates.
- **Parallel work.** `pk worktree review <ID>` (a detached review worktree, a diff file and a stat file that names what the diff leaves out; run again, it moves to the new commit for a re-review), integration rounds (`pk integrate start`, `check`, `close`, `pk merge <ID> --round NAME`), a contract-first template, and QA no longer limited to one branch at a time. Merges stay one at a time.
- **One command per step.** `pk merge <ID>` (merge check, merge, claims, hand-off, bookkeeping commit), `pk record` (ruling, state, hand-off, commit), `pk worktree finish <ID>` (worktrees, merged branch, named symlinks, and the containers and volumes that carry the exact compose project label; never forces).
- **Tracking.** `pk followups` (IDs, close, tag, duplicates; a follow-up is never deleted and its number never reused), `pk adhoc` (work outside the register), `pk cost` (an append-only log of subagent runs, with a fresh-agent threshold). The generated hand-off block opens with a Now list carrying each task's tier, what a blocked task waits on and every open integration round, counts the done tasks by tier, lists a parked task with the decision it waits on, and shows the newest fifteen rulings; `pk handoff check` fails over a size budget. `pk record` writes everything the block says about the controller (`--now`, `--note`, `--queued`, `--parked`, `--handoff-budget`).
- **Dispatch hygiene.** `program/COMMON.md`; brief sections "Decided (do not reopen)" and "Stop and report when"; the return contract (status, commits, one test line, concerns; the report is a file); model classes A to D with every dispatch naming its model; a feasibility probe before briefs; ordered attack questions for full-tier QA; "Steps to verify" in implementer reports; "Verified" and "Not verified" in the controller's status; a Known hazards list in the hand-off template.
- **Seed rulings** K34 to K44 record the new rules with their reasons and supersede K1, K13, K18 and K21 by name.

Design choices, each with its reason:

- The rules file is ordered by audience and each role stops where its part ends, because one file read in part is cheaper than three files that restate each other. Hard rules 21 to 23 (merges one at a time, never force-remove a worktree that holds work, rulings are never rewritten) were stated only in the playbooks and are numbered now.
- A seed ruling whose rule is in the rules file names it and keeps only the reason and how to apply it; a superseded one keeps its own words, because a K number must not change its meaning.
- The tier lives in the status record on main, not only in the SPEC, because the gates must read it from somewhere a branch cannot write, and a fast-path task has no SPEC.
- Lower tiers are off until `tiers.full_paths` in `program/checks.yaml` lists the full-tier paths, so an upgraded program behaves exactly as before and "unsure" means full.
- Full-tier work is named by path, because a script can check a path and cannot check an intention; `kit/`, `program/checks.yaml` and `program/gates.yaml` are always full.
- A light task ends at `merged`, not `done`, so hard rule 6 holds as written: `done` still means QA accepted it with evidence. A `merged` task evidences nothing in a gate claim.
- The light gate keeps the scans, because a secret can land in any file; `--inspected <sha>` must name the branch tip, so the controller's diff inspection is a recorded statement about one commit.
- The standard tier keeps the hand-back gate and the evidence record unchanged and saves only QA's re-run and planted-bug quota (the suite runs twice, not three times), so the evidence model is untouched.
- The fast-path brief is held to what a SPEC and PLAN are held to (on main, unchanged by the branch, required sections), and a fast-path task cannot change a guarded file, because the approval line lives in a SPEC.
- An ad-hoc task's tracking state ends at `closed`, not `done`, so `done` stays a word only QA can write; `closed` is refused while its status record is in flight.
- Any tag defers a follow-up (counted by tag, not listed), because the only use of a tag so far is "not until that task resumes".
- `pk record` requires a ruling, validates everything it can before it writes, runs only on main with nothing staged (with `--no-commit` too), and takes the attribution line as `--trailer`, because a script cannot know who is typing (hard rule 16). A write or a commit that fails puts its three files back. The size budget can only be measured on the rewritten hand-off, so a step that goes over it is committed and then exits 1 with the reason.
- The fresh-agent threshold is 400,000 tokens or three resumes per agent of a task, overridable on `pk cost report`; `--agent` labels one session so its resumes add up.
- The hand-off budget is 60 KB (override: `pk record --handoff-budget BYTES`, so a raise carries a ruling; it is stored as `handoff_budget_bytes` in the controller state and stated in the generated block) and Next steps 2 KB; the rulings window is one clipped line per ruling, because the full text is one command away.
- A full-tier task is never a member of an integration round, and a round's re-check is the whole suite once, because a script cannot choose a safe subset of tests.
- `pk merge` runs `git merge` itself, for one task, and refuses a branch that is not accepted: the kit now refuses to merge unaccepted work, which 0.2 left to the controller. "Accepted" is derived, as `pk status set done` derives it, from the evidence committed at the branch tip; the word `done` in the status file is necessary and not sufficient.
- The acceptance binds the reviewed commit (the `head` of the accepted results record). After it a branch may add only its status file `program/status/<ID>.json`, under `program/evidence/<ID>/` only `latest.json`, `results/`, `acceptance/` and `report.md` (what the kit and the implementer's report write), and clean merges of main; any other file in the evidence folder (`produce.py`, a `conftest.py`, a test file a runner would collect) is refused like any other late change; a later commit that changes anything else, a revert included, and a merge commit that is not what git makes of its two parents are refused, because nobody reviewed them. The way on is the ordinary one: the gate runs on the new tip, the reviewer is moved to it (`pk worktree review` again) and QA accepts that record.
- `merged` is audited like `done`. The record stores the inspected commit; `pk status set <ID> merged` without `--inspected` is refused; setting it and `pk status validate` both require main to hold a merge of exactly that commit, the task to have been light on main when it landed, and the light path's guard to pass again on what the merge brought in (`<merge>^1..<merge>`, so a file added inside a hand-made merge commit counts). While `task/<ID>` exists, `--inspected` must be its tip. The check works from the merge commit, so it holds after the branch is deleted.
- A full-tier `done` needs an acceptance that lists the commands QA re-ran and at least three planted bugs of its own, so the two depths of the QA brief leave different records. A status record written before tiers existed is not held to it afterwards. Whether the quota applies is read from main's record, never from the branch's, and a branch whose status file drops the `tier` field main's record names changes its tier and fails the gates, so a new task cannot claim that exemption.
- `pk worktree finish --compose-project NAME` selects containers and volumes by the label `com.docker.compose.project=NAME`, which the container tool matches exactly, and never by a name prefix: a name prefix reaches `NAME-dev` and `NAME_e2e`, which are other projects. Images are not removed at all: a label is not reliably on them, a name is the only other handle, and they are rebuilt from the repository. Each thing is removed by its own name, one at a time and without `-f`. Every check and both listings come before the first removal; it refuses an unmerged branch unless `--keep-branch`, a worktree standing on a commit no branch holds, and git-ignored files unless `--drop-ignored`.
- QA reviews in a detached worktree but commits its acceptance on the task branch, so the acceptance still binds the task's latest results record and nothing about `done` changed. The implementer stops when it hands back, as in 0.2: the review worktree exists so that reviews of several tasks run at once and a reviewer's planted bugs never touch the task worktree, not so that the branch can move under the reviewer.
- The kit still names no model: classes A to D are roles, the mapping is the program's (`program/COMMON.md`), and `pk cost add` refuses a run without a model.
- The feasibility probe, steps to verify, verified and not verified, and batched user decisions are rules, brief sections and templates only: none of them relaxes a check, so none needs a gate. Contract first has one check: the contract goes to main with the definition, and the gates hold a branch's copy identical to main's.
- The fast brief is checked as written: its four sections, and a `Tier:` and a `Ruling:` line equal to the status record. A tier is held to the record's own history (the dispatch entry names it, each change names its ruling), so a tier edited by hand does not validate.
- A `tiers` path covers everything under it with or without its trailing slash, and `pk doctor` warns about an entry that covers no tracked file.
- `pk record`, `pk merge` and `pk rulings validate` refuse a rulings log that no longer begins with the bytes committed at HEAD (hard rule 23), so no command of the kit commits a rewritten ruling.
- A round record is re-derived from git when a member lands and when the round closes, so one written by hand covers nothing.
- No dashboard: the generated hand-off block is the kit's view of the state.

Upgrading a 0.2.0 program:

1. `sh install.sh /path/to/your-repo --upgrade`. Status records without a tier read as `full`; a 0.2 `controller-state.json` reads as it is and gets follow-up IDs the first time a command writes it; evidence is untouched.
2. Copy `kit/templates/COMMON.md` to `program/COMMON.md` and fill it in.
3. On main, add a `tiers` section to `program/checks.yaml` (copy it from `kit/templates/checks.yaml`) and list your full-tier paths. Leave it empty to keep every task full.
4. Replace references to the three old rule files with `kit/rules/RULES.md` in your hand-off, and add a Known hazards section if you want the seeded list.
5. Run `pk handoff update` and `pk handoff check`. If the check fails on the size budget, move history into rulings and close or tag follow-ups; `handoff_budget_bytes` raises the budget meanwhile.
6. Scripts that parsed `pk status list` get a fifth column (the tier); `pk status set` prints the tier when it is not full. `pk trace impact` and `pk change impact` gain a `merged` bucket.

Known limits:

- A hand edit that rewrites a status record's tier and the dispatch entry of its history together, or the `Tier:` line of a SPEC on main, is not detected by a script: the spec-QA record does not bind the SPEC's bytes, and the audit is main's git history (review finding L6, in part).
- A ruling is checked for existence only. Any existing ruling allows a fast path or a tier change; nothing requires its text to name the task, because one ruling may cover a class of tasks (L17, in part).
- `pk integrate close --recheck` does not drop a member that never merged: a round with such a member stays open, and is shown as open in the hand-off. The branch `round/<NAME>` of a closed round stays among the unmerged branches, because it names the tested tree (L12, in part).
- `pk record` puts its files back when a write or the commit fails; a process killed between the writes is not undone (`git status` shows the three files) (L16, in part).
- The model mapping table of `program/COMMON.md` is not checked: it ships blank, `pk doctor` does not warn about an empty cell and `pk cost add` takes any model name. A check would make `pk doctor` warn on every fresh install (L20, in part).
- Decisions the user owes have no list of their own: one a task waits on is that task's parked note, shown in the hand-off, and any other is a follow-up with the area `needs user` (L20, in part).
- The removals of `pk worktree finish --compose-project` are tested against a stand-in container tool. Only the listing was tried against a real one, read-only: its label filter matched a value exactly and not by prefix.
- A follow-up removed from the controller state together with a matching edit of `followups_issued` is not detected; the audit is git history.
- After `task/<ID>` is deleted, nothing ties a `merged` record's inspected commit to the task: a record set or written then may name a decoy, a light-only merge of another commit (or the branch's own first commit merged on its own), and the audit then checks that merge, not the task's work (re-review N1).
- Seed ruling K18 keeps the words "Tier 1, Tier 2" for model strength, marked superseded by K35: a K number does not change its text.

## 0.2.0

Breaking change: the evidence layout.

- **In-repo, append-only records.** A task's evidence is no longer one `program/evidence/<ID>/results.json` and one `acceptance.json` rewritten in place. Each run appends a new, read-only record named by its own digest, `program/evidence/<ID>/results/<utc>-<sha256[:12]>.json` (QA's acceptances under `acceptance/`), and records are never modified or deleted (rule 11). Each task's pointer `program/evidence/<ID>/latest.json` names its latest results and acceptance record and is the only mutable evidence file; a task branch edits only its own pointer, so evidence never conflicts on merge. `pk evidence validate` with no record, the hand-back gate and `pk doctor` refuse a record whose bytes no longer match its name, a commit (merge commits included) that modified or deleted a record, a pointer that does not name its task's newest record, and a pointer entry that does not resolve to a committed record. `pk evidence history <ID>` lists every record of a task. A refresh appends a record with `supersedes`; a re-acceptance appends an acceptance (`pk evidence accept`).
- **Drift replaces staleness.** A record states what was verified at its `head` and stays true; a later change only makes it drift. `pk stale` is now a drift report (`fresh` or `drifted since <head>`, against the checkout's HEAD) that exits 0 unless `--strict`. Records no longer carry a `stale` field and `Evidence.stale()` is gone. The hand-back gate asks a task for a new record only when its own inputs changed; `pk gate binding` reports drift instead of failing on it; `done` needs the latest acceptance to bind the latest results record and its head to be an ancestor of HEAD. A gate record binds its children by digest and records each child's drift.
- **Acyclic closure.** A record binds only inputs, never a derived output (rule 12). `pk evidence lint` names offending inputs; `Evidence.write`, the gate, the binding check and `done` refuse them. Only a gate record binds other records, by digest; nothing binds a gate record.
- **API.** `Evidence.write()` takes no path and returns `{digest, path, file}`. New commands: `pk evidence lint`, `history`, `accept`, `import` and `validate` with no record (`init` creates nothing); `pk stale --strict`. `pk doctor` runs the evidence integrity checks and warns about a record pending commit.
- **Import path for 0.1 projects.** Run `pk evidence import`: it copies each `results.json` and `acceptance.json` byte for byte into the new layout as the first history entry, writes each task's pointer, and prints the exact `git rm` lines for the originals (it never deletes them). `gates.yaml` records may keep `path: program/evidence/<ID>/results.json` or say `task: <ID>`.

## 0.1.1

Review fixes in the gate, status, evidence, fetch, common, pk and install paths.

## 0.1.0

First release.
