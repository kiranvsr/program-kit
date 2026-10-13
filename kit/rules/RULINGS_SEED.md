# Seed rulings

These are the starting rulings of a program, numbered K1 to K44. They come from earlier programs run with this process. Read the ones your task's SPEC cites, by heading. Do not read all of them up front.

The rules themselves are stated once, in `kit/rules/RULES.md`. Where a seed ruling is one of those rules, its **Rule.** line names the rule and this file keeps only what the rule file does not: why the ruling was made and how to apply it. A K number never changes its meaning: a ruling that a later one supersedes keeps its own words and says which ruling replaced it.

Your own program records its rulings in `program/rulings.md` with `pk rulings add`, numbered R1 onward. A ruling never contradicts an earlier one silently: it supersedes it by name. A K ruling can be superseded the same way.

## K1. Merge queue: QA only on evidence that reflects current main, merge right after acceptance

*Superseded in part by K36: QA may run on several branches at once; merges still go one at a time.*

**Rule.** QA starts once the branch's evidence reflects current main. Once QA accepts, the controller merges that branch next. No other code merge to main happens between the start of QA and that merge. Accepted branches merge one at a time, in acceptance order. Documentation-only hand-off commits are fine.

**Why.** Every merge to main makes every waiting branch re-merge, re-produce evidence and re-run QA. Rework, not coding, was the bottleneck.

**How to apply.** The controller keeps a queue ordered by acceptance. It starts QA on one branch whose evidence reflects current main (`pk stale`, and K16 when main moved), merges it on acceptance, and only then starts QA on the next.

## K2. Hand-back gate before any QA round

**Rule.** Working rule W2 of `kit/rules/RULES.md`.

**Why.** QA rejected work for mechanical misses that a script finds in minutes. Each rejection cost a full QA round.

**How to apply.** The implementer pastes the gate's last line into its report. The controller reads that line before it starts QA. Exit code 0 alone is never enough.

## K3. Decide, do not ask

**Rule.** Working rule W1 of `kit/rules/RULES.md`.

**Why.** Questions cost a round trip and the controller's context. Most questions had an answer already written down.

**How to apply.** Before asking, search `program/rulings.md` and this file. If a precedent fits, decide and record it in the report. If you must ask, state the case, the options and your recommendation.

## K4. Fix rounds return a finding-to-fix table and get a scoped re-check

**Rule.** Working rule W3 of `kit/rules/RULES.md`.

**Why.** The implementer already holds the context, and a full second review repeats work that was already accepted.

**How to apply.** Number the findings. Reply row by row: finding, fix, commit, command that shows it fixed. The re-checker verifies each row and reports ESCALATE for anything else it notices.

## K5. Concurrency limit: three implementers, four agents

**Rule.** Controller rule C5 of `kit/rules/RULES.md`.

**Why.** More parallel work produced more stale branches and hit session and rate limits.

**How to apply.** Before dispatching, count running agents. Finish or merge before you start more. Each agent uses its own scratch folder (K17).

## K6. A gate passes on its OK line, not on its exit code

**Rule.** Working rule W2 of `kit/rules/RULES.md`. For the hand-back gate the line is `OK: <branch> passes the hand-back gate at <sha>`; for the merge check it is `OK: <branch> merges into main at <sha>`.

**Why.** A merge check once exited 0 without checking anything, because a setup file was missing and an exit trap hid the failure.

**How to apply.** Capture the output, compare the last line exactly, and record it. If the line is absent the check did not pass, whatever the exit code says.

## K7. PLAN commands run literally

**Rule.** Evidence rule 1 of `kit/rules/RULES.md`.

**Why.** Evidence that ran a different command proves a different claim.

**How to apply.** The gate reads lines starting `$ ` (optional ` # expect N`). Copy them into the producer as they are.

## K8. Derived fields are parsed and asserted

**Rule.** Evidence rule 2 of `kit/rules/RULES.md`.

**Why.** A run marked as expected with an unparsed field claimed more than it checked.

**How to apply.** Pass a parser to `run_command`. A parser that raises or returns nothing makes the row not as expected, so a missing field fails the run.

## K9. Every new guard has a cheapest-defeat planted bug

**Rule.** Evidence rule 3 of `kit/rules/RULES.md`.

**Why.** A unit test that passes with the guard removed shows the guard is not what protects the behaviour.

**How to apply.** Add a row to the planted-bug table for each guard. Run it through the committed runner and record the kill.

## K10. A planted bug counts only on a test failure

**Rule.** Evidence rule 4 of `kit/rules/RULES.md`. The runner records `builds` and `kill_reason` per row and always carries one deliberately non-building row recorded as not caught.

**Why.** Counting build failures as catches made a broken edit look like a strong test suite. The self-test row makes a runner that does this visible.

**How to apply.** Write edits that stay valid code (`&& false`, `|| true`, assign to an unused name). If a row shows `build failed`, rewrite the edit.

## K11. Cross-boundary tests for every access policy

**Rule.** Evidence rule 5 of `kit/rules/RULES.md`.

**Why.** A policy nobody tried to cross can be wrong in both directions without any test noticing.

**How to apply.** One test per new policy. Use K12 for the update case.

## K12. Update checks need a positive eligible-record control

**Rule.** Before a cross-boundary update assertion, the test must prove that the owner has at least one record matching the exact update predicate and that the intended change is legal for it. Then show the other principal's update changes nothing. Do not reuse a fixture that earlier steps made ineligible.

**Why.** An update that matches no owner record proves nothing about isolation. It would pass with the policy removed.

**How to apply.** Order the test: owner updates its record and succeeds, restore it, other principal updates and nothing changes, owner record unchanged.

## K13. QA checks the binding and re-runs the evidence; the merge check re-runs the gate

*Superseded in part by K34: the re-run of the PLAN commands and the three planted bugs are the full tier's.*

**Rule.** QA does not re-run the hand-back gate and does not treat an OK line in a report as evidence: the results record holds the producer's commands and planted bugs, not the gate's output. QA runs `pk gate binding <ID> --branch task/<ID>` (the latest record is committed with the digest its pointer names, names an ancestor of the tip, every hashed input matches the blob at that commit, and no record was rewritten; drift is reported), `pk evidence validate` and, in the task worktree, `pk stale`. QA always re-runs the PLAN commands, the committed planted bugs and at least three of its own, and reviews the diff, scope, claims and security. The independent run of the gate's checks is the controller's `pk gate merge-check` before the merge: the guarded-file check, the SPEC, PLAN and review identity check, and the build, tests and scans on a trial merge. If the binding fails, or main moved with code that overlaps the task's own hashed inputs, QA rejects and the branch merges main and re-produces (K16).

**Why.** Re-running the longest suites on every review burned the session budget for no added assurance, and an OK line somebody typed proves nothing. One independent run at the merge is enough, and no branch can skip it.

**How to apply.** Check the binding first, mechanically. A failed binding is a rejection, not a reason to run the gate yourself. Say in the acceptance which commands and planted bugs you ran.

## K14. Read only the rules and the rulings your SPEC cites

**Rule.** Working rule W7 of `kit/rules/RULES.md`.

**Why.** The log grows without limit, and reading all of it costs more than the rest of the task.

**How to apply.** Open `program/rulings.md` and read only the numbers named in your inputs. Read this seed file by heading, not top to bottom.

## K15. After an interruption, void and resume; never restart

**Rule.** Working rule W8 and evidence rule 8 of `kit/rules/RULES.md`.

**Why.** Restarting repeats accepted work, and a half-written evidence file left in place looks like a real record.

**How to apply.** Run `git status --short` in the worktree. Restore or delete each partial evidence file by its literal path. Then resume.

## K16. A branch need not re-merge when main moved without touching its inputs

**Rule.** Controller rule C2 of `kit/rules/RULES.md`.

**Why.** Most main moves are unrelated to a given branch, and re-producing evidence for them is pure cost.

**How to apply.** Run `pk stale` against main, then the merge-tree check. Record both results in the controller's notes.

## K17. Each agent has its own scratch folder, and mutation tools record their rows

**Rule.** Every agent writes scratch files only in a folder named for its task and role (for example `<scratchpad>/<ID>-impl/` or `<ID>-qa/`), never the scratchpad root. Every committed planted-bug tool records `builds` and `kill_reason` per row and carries the non-building self-test row (K10).

**Why.** Two agents once overwrote each other's mutation script in a shared folder.

**How to apply.** Create your folder first. Put every temporary script and log there and delete only those by exact path.

## K18. Model tiers are roles, never names

*Superseded by K35: four model classes by role, and every dispatch names its model.*

**Rule.** Tier 1, your most capable model, does the controller's work, first-pass implementation, first-pass QA, spec writing and spec review, and any re-check that escalates. Tier 2, a cheaper model, does only scoped re-checks of exact fixes, mechanical re-runs with a checklist and resume-only work. A Tier 2 agent reports ESCALATE for anything outside its checklist and never accepts on judgement. Never use Tier 2 for first-pass work, a security ruling, or accepting a task with an open important finding. With one capable model, use it for everything. Nothing in the kit names a model.

**Why.** Cheap models are good at following a checklist and poor at judgement, and the session budget was the binding limit.

**How to apply.** The controller writes its tier mapping into HANDOFF when it takes over. Give a Tier 2 agent a checklist and the line "report ESCALATE for anything not on this list".

## K19. Producers use the shared evidence kit

**Rule.** Every evidence producer uses `kit/scripts/evidence.py` for the clean-tree check, input hashing, literal command recording, planted bugs and the write guard, instead of hand-written helpers.

**Why.** Each hand-written helper repeated the same mistakes, and QA kept rejecting them one by one.

**How to apply.** Start `program/evidence/<ID>/produce.py` from the `Evidence` class. Do not rewrite merged producers.

## K20. No polling and no tailing

**Rule.** Working rule W5 of `kit/rules/RULES.md`.

**Why.** Wait loops burned session budget, and an agent once sat idle for half an hour after its command had finished.

**How to apply.** Start the producer itself as the background task, then read the output file once when it finishes.

## K21. Reports are short and fixed in shape

*Superseded in part by K37: the final message is four things; the report is a file.*

**Rule.** A report follows the fixed headings in the role's brief and stays under 25 lines. It holds the finding-to-fix table, the gate results, deviations and the stale-list summary. It does not restate the SPEC.

**Why.** The controller reads many reports, and long ones hid the facts that mattered.

**How to apply.** Put detail in the evidence files and link to them by path.

## K22. A change to a generated hand-off block does not void evidence

**Rule.** A change on main limited to the generated block of `program/HANDOFF.md` does not void a binding that hashes that file, provided no evidence command or test reads it and the merge-tree check is clean. QA records the check.

**Why.** Hand-off refreshes happen after every merge, and treating each as a main move would invalidate everything.

**How to apply.** Compare the diff against the generated markers. If only text between them changed, record that and go on.

## K23. Bytecode caching is off in planted-bug runs

**Rule.** For Python projects, planted-bug runs set `PYTHONDONTWRITEBYTECODE=1`, write no bytecode in build steps and remove `__pycache__` after each restore.

**Why.** A same-size edit restored within one second can leave a valid compiled file of the planted version, so later rows run stale code and kills are over-counted.

**How to apply.** The evidence kit clears bytecode around every row. Custom runners must do the same.

## K24. A guard whose wiring needs inputs that do not exist yet may be accepted unwired

**Rule.** A guard component whose production wiring needs inputs that do not exist yet may be accepted for development unwired, provided the gap is stated in `blocked_by`, nothing in the evidence, docs or gate claims implies production enforcement, and the component is fully tested alone. The wiring becomes a follow-up with an owner, and the user decides if it is new scope. Wiring it to fail closed without the inputs is not acceptable either, because it would deny everything.

**Why.** The alternatives were to block the guard forever or to ship wiring that either lies or breaks the product.

**How to apply.** Write the gap in the SPEC and the report. Add the follow-up to the register through `pk register change`.

## K25. Consumed-input hashes are complete

**Rule.** Evidence rule 6 of `kit/rules/RULES.md`. Exclude lifecycle or generated outputs only with a written reason, and show that a representative formerly unhashed consumed file is now detected when edited.

**Why.** Stale detection sees only what was hashed, so an incomplete list let stale evidence look current.

**How to apply.** Have the collector derive the list deterministically, sorted and deduplicated, and require every path to exist at HEAD. Commit the collector, then produce once from a clean tree.

## K26. A tool's test-only fixture mode is explicit and default off

**Rule.** A tool's test-only fixture mode (for example a collector reading a fixture tree that has no version control) is enabled only by an explicit test flag, never by a silent fallback. Ordinary use requires the real source (committed blobs), and a failure to reach it never falls back to reading whatever is on disk. Test both the default rejection and the explicit success, and add a planted bug that flips the default.

**Why.** A silent fallback turns a git failure into evidence about whatever happens to be on disk.

**How to apply.** Label fixture output as test context, never as commit-bound evidence.

## K27. A failure that also happens on clean main is a named environmental deviation

**Rule.** A PLAN command that fails identically on clean main is a pre-existing environmental failure only when the producer records all of these: the literal run and its real exit code; one reproduction on a clean main worktree at a named commit with its log hash; proof the task's inputs to that command are byte-identical to main; a named deviation whose expected exit equals the observed exit and whose failure message is asserted, so a silent pass or a different failure flips the row; and nothing claimed from it, with the gap in `blocked_by` and an owner. It is never recorded as a pass. QA re-runs the main reproduction.

**Why.** A task should not be blocked by a failure it did not cause, and the failure must not be hidden either.

**How to apply.** Add the deviation row to the producer, reproduce on main once, and list the owner follow-up.

## K28. Refreshing an older record re-runs everything it holds

**Rule.** Evidence rule 9 of `kit/rules/RULES.md`.

**Why.** A refresh that kept old numbers or dropped awkward entries made a snapshot look current.

**How to apply.** Diff the old and new record entry by entry. Every entry appears in both, with real output.

## K29. An acceptance lives where the code reads it

**Rule.** Hard rule 4 of `kit/rules/RULES.md`.

**Why.** A side file left the repo-wide suite failing on main while the acceptance looked complete.

**How to apply.** Find the code that reads the field. Set the field. Run that code, and the repo-wide suite, before you call it accepted.

## K30. A refreshed helper entry records what it proves now

**Rule.** When re-running a helper entry gives a different result because other tasks changed its input, record the real command, the real output and a claim limited to what the command proves. Never keep the old number.

**Why.** A claim that outlives its output is false.

**How to apply.** Rewrite the claim sentence from the new output. If the output no longer supports the original claim, say so in the entry.

## K31. Validators never download inline

**Rule.** Evidence rule 10 of `kit/rules/RULES.md`.

**Why.** An inline download made a check slow and flaky, and it made what was checked depend on the network.

**How to apply.** Add the pin to the pins file, run `pk fetch --pins <file>` once per host, and make the verify step call `pk fetch verify`.

## K32. Evidence closures exclude controller documents

**Rule.** The hashed-input closure of an evidence record never includes controller documents: `program/HANDOFF.md`, `program/progress.md`, `program/controller-state.json`, `program/rulings.md` and anything else only the controller writes. No evidence command consumes them. The controller commits those files only after a merge, a ruling or at session end.

**Why.** One producer hashed the whole tracked tree, so every hand-off commit looked like a main move and voided a producer that was running. Completeness (K25) means the closure of what the commands consume, not the whole tree.

**How to apply.** Keep the exclusion list in the collector with the reason beside it. Dispatches and hand-backs are noted in the controller state and committed with the next merge.

## K33. Nothing else merges while a gate that reads every record is producing evidence

**Rule.** Controller rule C8 of `kit/rules/RULES.md`.

**Why.** A merge during a gate run voided the run, and the whole gate had to start again.

**How to apply.** The controller announces the quiet window, drains the merge queue, runs the gates one after another on a quiet main, and only then reopens the queue.

## K34. Verification tiers: the process follows the risk

**Rule.** Controller rule C11 of `kit/rules/RULES.md`. Supersedes K13 in part: QA's re-run of the PLAN commands and its own three planted bugs belong to the full tier; a standard-tier QA checks the binding and reviews the diff, and a light-tier task has no QA.

**Why.** One process for every task ran the longest suite three times and planted bugs against changes that could not hide one. The cost fell on the smallest tasks, and the careful ones got no more care than the trivial ones.

**How to apply.** Choose the tier when the task is specified and write it in the SPEC (`Tier: standard`). Full whenever unsure. The gates read the tier from the status record on main and fail a task that touches a full-tier path below the full tier.

## K35. Model classes by role, and every dispatch names its model

**Rule.** Controller rule C4 of `kit/rules/RULES.md`. Supersedes K18.

**Why.** Two tiers put first-pass implementation on the most expensive model and left nothing between it and a checklist follower. An unnamed model defaulted to the controller's, the dearest one.

**How to apply.** Write the class-to-model mapping in `program/COMMON.md` and name the model in every dispatch. Record each run with `pk cost add --model`.

## K36. Parallel review, one combined round, merges one at a time

**Rule.** Controller rules C1 and C13 of `kit/rules/RULES.md`. Supersedes K1 in part: QA is no longer limited to one branch at a time, and several accepted branches may be verified together in an integration round.

**Why.** Reviewing and merge-checking one branch at a time made the queue the bottleneck while agents idled, and N branches cost N full suite runs.

**How to apply.** Give each reviewer `pk worktree review <ID>`. For branches below the full tier use `pk integrate start`, `check`, `pk merge <ID> --round <NAME>` for each, then `pk integrate close`.

## K37. The final message is four things; the report is a file

**Rule.** Working rule W6 of `kit/rules/RULES.md`. Supersedes K21 in part: the 25-line limit applies to the report file, and the message that returns to the controller is status, commits, one test line and concerns.

**Why.** Every pasted report stayed in the controller's context for the rest of the session.

**How to apply.** Write the report to the path your brief names and return its path. The controller opens it only when a decision needs it.

## K38. A fast path for small bounded work, never for the full tier

**Rule.** Controller rule C12 of `kit/rules/RULES.md`.

**Why.** A one-hour change the user asked for directly waited on a SPEC, a PLAN and a spec review that restated the request.

**How to apply.** Record the ruling, write `program/tasks/<ID>/BRIEF.md` from the template, and dispatch with `--fast --ruling R<n>`. The gate holds the brief to what it held the SPEC and PLAN to: on main, unchanged by the branch.

## K39. A fresh agent after 400,000 tokens or three resumes

**Rule.** Controller rule C16 of `kit/rules/RULES.md`.

**Why.** Resuming keeps the context that makes a fix cheap, until the context itself is the cost: a long-lived agent re-reads everything it has seen on every turn.

**How to apply.** `pk cost report` flags the agent. Give the fresh one only the findings, the files and the commands.

## K40. Verify what a decision rests on

**Rule.** Controller rule C17 of `kit/rules/RULES.md`.

**Why.** Agents reported failures as already present on main, and work as verified, when neither had been checked. The controller relayed both to the user.

**How to apply.** Reproduce a claimed main failure on a clean main worktree; read back what was said to be written. The status message has a Verified and a Not verified heading.

## K41. The hand-off has a size budget, and history lives in the rulings log

**Rule.** Controller rule C19 of `kit/rules/RULES.md`.

**Why.** A hand-off that carried every ruling and every closed item grew until reading it cost more than the work it described.

**How to apply.** `pk handoff check` fails over the budget. Move history into a ruling (`pk record`), close or tag follow-ups, and keep Next steps to the order of what comes next.

## K42. A generator changes only the file it names

**Rule.** Controller rule C9 of `kit/rules/RULES.md`, last sentence.

**Why.** A generated mirror that rewrote a tracked file on every build left the tree dirty after every command and hid real changes.

**How to apply.** Write generated views to ignored paths, or make the write a no-op when nothing changed. Check `git status` after a build on a clean tree.

## K43. Finish a merged task with one command that never forces

**Rule.** Controller rule C20 and hard rule 22 of `kit/rules/RULES.md`.

**Why.** Leftover worktrees, branches and containers filled the disk and held ports; cleaning them by hand invited a forced removal of the wrong one.

**How to apply.** `pk worktree finish <ID>`, naming the symlinks it may remove and the task's exact compose project name.

## K44. Known hazards are written down once, in the hand-off

**Rule.** `program/HANDOFF.md` keeps a Known hazards list, and a hazard met twice is added to it: a shell that drifts into a worktree, fixed ports that allow one local stack, a long-running local stack that degrades, leftovers of an interrupted run.

**Why.** Each successor met the same hazards again and paid for them again.

**How to apply.** Check where you are before you commit or delete (working rule W4). Start one local stack at a time, and restart it rather than debug a stale one.
