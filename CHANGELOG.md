# Changelog

## 0.3.0

Faster and cheaper runs without a weaker process. The evidence model of 0.2.0 is unchanged (committed, append-only records; latest wins; a record binds only its inputs), and hard rules 1 to 20 and the twelve evidence rules are word for word what they were. Nothing in `program/` has to change to upgrade.

What changed:

- **One rules file.** `kit/rules/RULES.md` replaces `HARD_RULES.md`, `EVIDENCE_HONESTY.md` and `THROUGHPUT.md`: part 1 hard rules, part 2 working rules for every agent, part 3 evidence honesty, part 4 the controller's rules. The briefs, the playbooks, the seed rulings, `docs/DESIGN.md`, the README and the successor prompt point to it instead of restating it. Each role reads its own brief, its parts of the rules and `program/COMMON.md`; `kit/playbooks/09_handoff.md` lists the files with sizes, and a test checks the table.
- **Verification tiers.** A task is `full`, `standard` or `light`, recorded in its status record at dispatch and changed only by a ruling (`pk status set --tier`, `pk status tier`). The gates read the tier from main and enforce it. A light task ends at the new status `merged`.
- **Fast path.** `--fast --ruling R<n>`: one `BRIEF.md` and a ruling replace SPEC, PLAN and spec QA. Refused for the full tier by the status machine and both gates.
- **Parallel work.** `pk worktree review <ID>` (a detached review worktree and a diff file), integration rounds (`pk integrate start`, `check`, `close`, `pk merge <ID> --round NAME`), a contract-first template, and QA no longer limited to one branch at a time. Merges stay one at a time.
- **One command per step.** `pk merge <ID>` (merge check, merge, claims, hand-off, bookkeeping commit), `pk record` (ruling, state, hand-off, commit), `pk worktree finish <ID>` (worktrees, merged branch, named symlinks, exact-name containers; never forces).
- **Tracking.** `pk followups` (IDs, close, tag, duplicates), `pk adhoc` (work outside the register), `pk cost` (an append-only log of subagent runs, with a fresh-agent threshold). The generated hand-off block opens with a Now list carrying each task's tier and shows the newest fifteen rulings; `pk handoff check` fails over a size budget.
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
- `pk record` requires a ruling, validates everything before it writes, runs only on main with nothing staged, and takes the attribution line as `--trailer`, because a script cannot know who is typing (hard rule 16).
- The fresh-agent threshold is 400,000 tokens or three resumes per agent of a task, overridable on `pk cost report`; `--agent` labels one session so its resumes add up.
- The hand-off budget is 60 KB (override: `handoff_budget_bytes` in the controller state, visible in a tracked file) and Next steps 2 KB; the rulings window is one clipped line per ruling, because the full text is one command away.
- A full-tier task is never a member of an integration round, and a round's re-check is the whole suite once, because a script cannot choose a safe subset of tests.
- `pk merge` runs `git merge` itself, for one task, and refuses a branch whose status is not `done`: the kit now refuses to merge unaccepted work, which 0.2 left to the controller.
- `pk worktree finish` removes containers, volumes and images only by a name that starts with the given project name and a separator, one at a time and without `-f`, and refuses an unmerged branch unless `--keep-branch`.
- QA reviews in a detached worktree but commits its acceptance on the task branch, so the acceptance still binds the task's latest results record and nothing about `done` changed.
- The kit still names no model: classes A to D are roles, the mapping is the program's (`program/COMMON.md`), and `pk cost add` refuses a run without a model.
- Contract first, the feasibility probe, steps to verify, verified and not verified, and batched user decisions are rules, brief sections and templates only: none of them relaxes a check, so none needs a gate.
- No dashboard: the generated hand-off block is the kit's view of the state.

Upgrading a 0.2.0 program:

1. `sh install.sh /path/to/your-repo --upgrade`. Status records without a tier read as `full`; a 0.2 `controller-state.json` reads as it is and gets follow-up IDs the first time a command writes it; evidence is untouched.
2. Copy `kit/templates/COMMON.md` to `program/COMMON.md` and fill it in.
3. On main, add a `tiers` section to `program/checks.yaml` (copy it from `kit/templates/checks.yaml`) and list your full-tier paths. Leave it empty to keep every task full.
4. Replace references to the three old rule files with `kit/rules/RULES.md` in your hand-off, and add a Known hazards section if you want the seeded list.
5. Run `pk handoff update` and `pk handoff check`. If the check fails on the size budget, move history into rulings and close or tag follow-ups; `handoff_budget_bytes` raises the budget meanwhile.
6. Scripts that parsed `pk status list` get a fifth column (the tier); `pk status set` prints the tier when it is not full.

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
