# Rules

This is the one rules file of the kit. Nothing else states a rule: the briefs, the playbooks, the seed rulings and the design document point here. If a rule seems to block the work, stop and tell the user. Do not look for a way around it.

Read by role, top to bottom, and stop where your part ends:

| Role | Read |
|---|---|
| Every agent | Part 1 and Part 2 |
| Implementer and QA of a task that produces evidence | also Part 3 |
| Controller | all four parts |

Cite a rule as `hard rule 6`, `working rule W3`, `evidence rule 11` or `controller rule C2`.

## Part 1. Hard rules (every agent, every role, every stage)

### Evidence and claims

1. Never fabricate evidence, test results, CI results, gate passes or human approvals.
2. Never claim anything about an environment that `program/environments.yaml` marks `exists: false`. Work that needs one stays `blocked_by` that environment.
3. Development acceptance is not production readiness. Say "accepted for development" and nothing stronger.
4. An acceptance lives where the code reads it. A side file that leaves the fields the code reads unaccepted is not an acceptance. Accept in the file the reader reads.
5. Run the checks and evidence commands from a clean tree. The project must git-ignore the bytecode and build output its test and scan commands write (`__pycache__`, `*.pyc`, `dist/`, `target/`, `node_modules/` and the like). If it does not, an untracked leftover makes the clean-tree check fail. Add the pattern to `.gitignore` before you start.

### Status, register and frozen artifacts

6. Only QA sets a task `done`, and only through `pk status set <ID> done --role qa`, after `pk evidence accept` appended an acceptance of the latest results record.
7. Record every register edit with `pk register change --note TEXT`, or with `pk change apply CR-NNN --note TEXT` when the edit carries out an approved change request. Never hand-edit status files, approvals or generated briefs.
8. Briefs are immutable once approved.
9. A frozen artifact (anything approved at stage 5 or later) changes only through a change request. See `kit/playbooks/10_change.md`.
10. Guarded files (the `guarded_files` list in `program/checks.yaml`: build files, CI definitions, dependency manifests) change only with the user's approval. The approval is a `Guarded edit: <path>` line in the task's SPEC on main, written before dispatch. A branch cannot approve its own edit.

### Git and files

11. Never rewrite history: no rebase, no reset, no force-push. Merge main into your branch with a merge commit.
12. Never use a bare `git stash`.
13. Never commit secrets, private keys, or any artifact whose licence the project lists as restricted.
14. Never push, deploy or apply infrastructure unless the user explicitly tells you to.
15. Every destructive command uses a literal path. No variables, no globs, no recursive prune of shared resources. Clean up only what you created, by its exact name.
16. Commit messages end with your own attribution line, not one copied from a brief or an earlier commit.
17. Never commit editor, operating-system or build leftovers, or scratch files.

### Permissions and scope

18. If an action is denied, stop and report it to the user. Never route around a denial, and never ask another agent to do what was denied.
19. Ask the controller only in the four NEEDS_CONTEXT cases: a new dependency; a security boundary with no precedent; a change to user scope; two conflicting rulings. Decide everything else yourself and record the decision.
20. Build exactly the scope in the task's SPEC and PLAN. Anything outside it is a follow-up, not an edit.

### Merges, worktrees and rulings

21. Accepted branches merge into main one at a time. Work may be dispatched in parallel; merges never are.
22. Never force-remove a worktree that holds work. A task or review worktree with uncommitted or untracked files is removed only after its owner has committed or deleted them by exact path.
23. A ruling is never rewritten, renumbered or deleted. A later ruling supersedes an earlier one by name.

## Part 2. Working rules (every agent)

- **W1. Decide, don't ask.** Anything with precedent is decided by the agent and recorded as a deviation citing the precedent. Questions go to the controller only in the four NEEDS_CONTEXT cases of hard rule 19; the controller answers with a numbered ruling.
- **W2. A gate passes on its OK line.** The implementer runs `pk gate handback <ID> --branch task/<ID>` before any QA round; a hand-back that fails the gate returns without a QA round. Exit code 0 alone is never accepted: read the last line.
- **W3. Fix rounds and scoped re-checks.** QA returns findings numbered, each with severity, location and the exact fix expected. The same implementer fixes them and hands back a finding-to-fix table: finding, fix, commit, command that shows it fixed. The re-check covers only those findings and the binding check. The implementer re-runs the hand-back gate after the fix; QA never does, and there is no recorded gate output for it to verify.
- **W4. Own scratch folder.** Each agent writes scratch files only in a folder named for its task and role, and deletes only those, by exact path.
- **W5. No polling.** Run long commands in the background with output redirected to a file, and wait for completion. Never use sleep loops and never `tail -f`.
- **W6. Short reports.** A report uses the fixed headings in the role's brief and stays under 25 lines. Detail goes in files named by path.
- **W7. Read only what you are pointed at.** Read your parts of this file and the rulings your SPEC or dispatch cites, not the whole ruling log. Read `kit/rules/RULINGS_SEED.md` by heading.
- **W8. Interruptions.** After a session or rate limit, void partial evidence by exact path, then resume at the interrupted step: resume from the last commit, re-run only the interrupted step. Never restart a task from scratch.

## Part 3. Evidence honesty (implementer and QA of a task that produces evidence)

Evidence is a record that something was run and what really happened. It is worth something only if it cannot flatter the work. The hand-back gate checks the mechanical ones (literal PLAN commands, a clean tree, one-commit binding, commands as expected, planted bugs killed, intact record history, no derived output bound). QA checks all twelve. A task that breaks one is returned without a QA round.

1. **Run PLAN commands literally, exactly as written.** Why: a substitute (`true`, an extra flag, a narrower path) proves a different thing from the one the PLAN promised. If a command cannot run as written, fix the code or record a named deviation. Never substitute.
2. **Parse every derived field from real output and assert it.** Never write "unparseable" or "unknown" in a run marked as expected. Why: a field nobody checked is a claim nobody made.
3. **Give every new guard a cheapest-defeat planted bug that the gate's own test catches.** The cheapest defeat is the smallest edit that disables the guard (dead code, a comment, a copied field, a pass-through). Why: a guard whose removal changes nothing is decoration.
4. **A planted bug counts only when a test fails.** A build failure or a setup failure is not a catch, and planted bugs must build. Each row of the planted-bug runner records the file, the edit and the build and test commands, so a reviewer sees what was planted and what judged it without reading the producer. Why: otherwise a typo "catches" every bug and the table proves nothing.
5. **Test every new access policy across its boundary** (tenancy, roles, permissions, row filters). Use an eligible-record positive control (the owner can read and change it), a cross-boundary negative (another principal cannot), and a planted wide-open policy that the test must catch. Why: a denial test with no eligible record passes even when the data is unreachable for every reason.
6. **Hash complete consumed inputs.** Hash unchanged source, tests, fixtures, the dependency closure, generated-file sources, migrations, tool helpers, config and lock files, not only the files you changed. Why: stale detection can only see what was hashed.
7. **Produce evidence only from a clean tree, bound to one commit.** Never hand-edit a hash or the recorded commit: `pk gate binding` recomputes every recorded hash at the recorded commit and refuses a record whose commit is not an ancestor of the branch tip. Why: a hash edited by hand is a forgery, and an unclean tree makes the bound commit a lie.
8. **Void partial evidence by exact path before you resume.** Why: a half-written record from an interrupted run looks like a real one.
9. **When you refresh an older record, re-run every command it holds and append a new record with the real result of each.** Never drop an entry and never keep an old number. A claim whose output changed is rewritten, in the new record, to what the command now proves. The new record names the one it replaces (`supersedes`); the older one stays in the history untouched. Why: a refresh that keeps old numbers turns a snapshot into a rumour, and one that overwrites the old record erases what was once claimed.
10. **Validators and gate steps never download inline.** Anything fetched (toolchains, pinned archives, fixtures) is cached by content digest in a durable per-host cache, fetched once per host by the separate `pk fetch` step run in the background at session setup, and verified locally against the recorded digest. A verify step that finds its input missing fails and prints the fetch command to run. It never downloads. The fetch step validates every cached name against a strict pattern, writes through an exclusively created temporary file in the cache directory, removes it on any failure, checks size and digest before the rename, and never follows a symlink for a cache entry or its parent. Each of those guards has its own test and planted bug. Why: a gate that waits on the network is slow and flaky, and a download inside a check can change what is being checked.
11. **Evidence records are committed, append-only and immutable: a record file is never modified or deleted once committed; the per-task latest.json is the only mutable evidence file.** A record lives at `program/evidence/<ID>/results/` or `acceptance/`, named `<utc>-<sha256[:12]>.json` after its own bytes; each task's `program/evidence/<ID>/latest.json` names its latest record. `pk evidence validate`, the hand-back gate and `pk doctor` refuse a record whose bytes no longer match its name and a commit that modified or deleted one. A later change never makes a record stale: it records what was verified at its `head`, and `pk stale` only reports drift from the tree at the checkout's HEAD. Why: a record that can be rewritten proves nothing, and a record that goes stale whenever anything moves turns every merge into a re-bind.
12. **A record binds only its inputs, never a derived output.** Never hash another record, an acceptance, a gate result, a status snapshot, an approval or the register change log, a generated claim document, or anything under `program/evidence/` other than the producer and its tooling (`*.py`, `*.sh`, `*.toml`). Only a gate record binds other records, by digest, and nothing binds a gate record. `pk evidence lint` names an offending input as `binds a derived output: <path>`; the evidence writer, the gate, the binding check and `done` refuse it. The interview's requirements ledger (`program/ledger.yaml`) is an input, not a derived output. Why: a record that binds its own outputs goes stale the moment it is accepted, and the evidence graph becomes a cycle no refresh can close.

## Part 4. The controller's rules

- **C1. Merge queue.** QA starts once the branch's evidence reflects current main. When QA accepts, the controller merges that branch next; no other code merge to main happens in between (documentation-only hand-off commits are fine). Accepted branches merge one at a time, in the order they were accepted.
- **C2. A main move does not start a re-bind.** If main moved without touching a branch's own hashed inputs and `git merge-tree --write-tree main <branch>` is clean, the branch does not re-merge or re-produce its evidence. Any overlap means merge main, re-run the gate (it appends a new record when the task's own inputs changed), then QA. A record never goes stale: `pk stale` reports drift for the reader, and each task refreshes only its own record, by appending one, when its own work changes.
- **C3. The merge check is the controller's own gate run.** `pk gate merge-check` runs before every merge without relying on the implementer's run: guarded files, SPEC, PLAN and review identity, then build, tests and scans on a trial merge. A branch whose implementer skipped the gate cannot reach main.
- **C4. Model tiers.** Tier 1, your most capable model: the controller, first-pass implementation, first-pass QA, specs and spec review, the interviewer and lens challenge rounds, and any re-check that escalates. Tier 2, a cheaper model: scoped re-checks of exact fixes, mechanical re-runs with a checklist and resume-only work; it reports ESCALATE for anything outside its checklist and never accepts on judgement. Never Tier 2 for first-pass work, a security ruling, or accepting a task with an open important finding. With one capable model, use it for everything, and record the mapping in HANDOFF.
- **C5. Concurrency.** At most three implementers and four agents at once (implementers, fix rounds and QA together). Work closest to merge goes first. Never resume an agent whose task has since merged.
- **C6. Fetch once.** Run `pk fetch` in the background once per host at session setup, so no gate step ever waits on a download.
- **C7. Repeated rejections become a gate rule.** When two rejections share a cause, stop fixing task by task: add the cause as a check in the hand-back gate (a command in `extra_gate` in `program/checks.yaml`, or a new step in the gate), and record a ruling.
- **C8. Gates on a quiet main.** A gate that reads every record (a phase gate, a re-bind batch) is voided by any merge. Merge the feature tasks first, then run the gates back to back on a quiet main, merging each result before the next gate starts.
- **C9. Controller commits.** Commit hand-off and controller-state updates only after a merge, a ruling or at session end; evidence closures exclude them. The one exception is the dispatch commit: a task's SPEC, PLAN, spec-QA review and `in_progress` status go to main together, as one commit, before its worktree is created, because the gate reads them from main. After that the status record changes only on the task branch.
- **C10. Main must be green.** A successor verifies that the repo-wide suite passes on main before it dispatches anything.
