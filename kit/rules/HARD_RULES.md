# Hard rules

These rules hold for every agent in every role, in every stage. If a rule seems to block the work, stop and tell the user. Do not look for a way around it.

## Evidence and claims

1. Never fabricate evidence, test results, CI results, gate passes or human approvals.
2. Never claim anything about an environment that `program/environments.yaml` marks `exists: false`. Work that needs one stays `blocked_by` that environment.
3. Development acceptance is not production readiness. Say "accepted for development" and nothing stronger.
4. An acceptance lives where the code reads it. A side file that leaves the fields the code reads unaccepted is not an acceptance. Accept in the file the reader reads.
5. Run the checks and evidence commands from a clean tree. The project must git-ignore the bytecode and build output its test and scan commands write (`__pycache__`, `*.pyc`, `dist/`, `target/`, `node_modules/` and the like). If it does not, an untracked leftover makes the clean-tree check fail. Add the pattern to `.gitignore` before you start.

## Status, register and frozen artifacts

6. Only QA sets a task `done`, and only through `pk status set <ID> done --role qa --evidence ...`.
7. Change the register only through `pk register change --note TEXT`. Never hand-edit status files, approvals or generated briefs.
8. Briefs are immutable once approved.
9. A frozen artifact (anything approved at stage 5 or later) changes only through a change request. See `kit/playbooks/10_change.md`.
10. Guarded files (the `guarded_files` list in `program/checks.yaml`: build files, CI definitions, dependency manifests) change only with the user's approval. The approval is a `Guarded edit: <path>` line in the task's SPEC on main, written before dispatch. A branch cannot approve its own edit.

## Git and files

11. Never rewrite history: no rebase, no reset, no force-push. Merge main into your branch with a merge commit.
12. Never use a bare `git stash`.
13. Never commit secrets, private keys, or any artifact whose licence the project lists as restricted.
14. Never push, deploy or apply infrastructure unless the user explicitly tells you to.
15. Every destructive command uses a literal path. No variables, no globs, no recursive prune of shared resources. Clean up only what you created, by its exact name.
16. Commit messages end with your own attribution line, not one copied from a brief or an earlier commit.
17. Never commit `.DS_Store`, `__pycache__`, build output or scratch files.

## Permissions and scope

18. If an action is denied, stop and report it to the user. Never route around a denial, and never ask another agent to do what was denied.
19. Ask the controller only in the four NEEDS_CONTEXT cases: a new dependency; a security boundary with no precedent; a change to user scope; two conflicting rulings. Decide everything else yourself and record the decision.
20. Build exactly the scope in the task's SPEC and PLAN. Anything outside it is a follow-up, not an edit.
