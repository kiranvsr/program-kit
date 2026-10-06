# tiny-service: a recorded run of the kit

tiny-service is a small HTTP service with `GET /health` and `POST /echo`, built with the program-kit loop from a short brief to a merged, accepted task. Everything here is a record of that run. `kit/tests/test_end_to_end.py` replays it in a throwaway git repository and drives every `pk` command, so what you read here is known to work.

## What is here

| Path | What it is |
|---|---|
| `BRIEF.md` | The starting point: the brief the user gave. |
| `ledger.yaml` | The recorded interview. It closes every required topic of stages 1 to 4 and 6; the two conditional topics (regulatory, integrations) do not apply because no entry names a regulated or external thing. |
| `artifacts/` | What the interview produced, laid out as it lands under `program/`: discovery, environments, claim boundaries, product, spec, architecture, the ADR, gates, traceability matrix, freeze review and sign-off, roadmap, register, and the challenge report of each stage from 2 to 6. |
| `checks.yaml` | The python preset, with the unit tests pointed at `src/`. |
| `tasks/P1-M01-S01-T01/` | The task's SPEC, PLAN and spec-QA review: the dispatch commit. |
| `src/` | The service and its unit tests, the work of that task. |
| `evidence/P1-M01-S01-T01/produce.py` | The evidence producer the hand-back gate runs. |
| `HANDOFF.md`, `controller-state.json` | The hand-written part of the hand-off and the controller's queue. |
| `.gitignore` | Ignores bytecode and worktrees; without it the clean-tree check of the evidence step fails. |

The register has one phase, one milestone, one step and two tasks. `P1-M01-S01-T01` (health and echo) is built and accepted; `P1-M01-S01-T02` (access log) stays `not_started`, and the hand-off lists it as next.

The environment matrix has `dev` (exists) and `staging` (does not exist). So `CLAIM_BOUNDARIES.md` says nothing may be claimed for staging, and the gate document for G-P1 shows `dev` as evidenced and `staging` as not evaluated.

`artifacts/freeze/signoff.md` is a fixture: in a real program the user writes the sign-off in their own words and the kit never does.

## The loop, command by command

1. `sh install.sh <repo> --preset python`, then put `BRIEF.md`, `checks.yaml`, `HANDOFF.md` and `.gitignore` in place.
2. `pk interview replay ledger.yaml artifacts` installs the ledger and artifacts and approves stages 1 to 6. `pk interview next` then prints stage 7: discovery is complete.
3. `pk trace validate --stage 6`, `pk register validate`, `pk briefs generate`, `pk briefs check`.
4. Dispatch: copy the task's SPEC, PLAN and spec-QA `review.md`, `pk status set P1-M01-S01-T01 in_progress --role controller`, commit the four together to main, `pk worktree new P1-M01-S01-T01`. The gates refuse a task whose `review.md` is not on main.
5. The implementer puts `src/` and `produce.py` in the worktree and commits. The controller answers a question with `pk rulings add`.
6. `pk gate handback P1-M01-S01-T01 --branch task/P1-M01-S01-T01` from main. Its last line is `OK: task/P1-M01-S01-T01 passes the hand-back gate at <sha>`. It needs `gitleaks` on the PATH. The test puts a stand-in `gitleaks` that always succeeds there: it proves the gate calls the scanner and fails without it, not that secrets are found.
7. Evidence commit (`pk status set ... review`), QA (`pk gate binding`, `acceptance.json`, `pk status set ... done --role qa`), `pk gate merge-check`, merge with `--no-ff`.
8. `pk handoff update` and `check`, `pk gate claims G-P1` and `validate`, and one change request (`pk change create`, `impact`, `approve`, `apply`).

## Running the service

    python3 src/tiny_service.py --port 8080
    curl 127.0.0.1:8080/health
    curl -d '{"hello": "world"}' 127.0.0.1:8080/echo

Run its tests with `python3 -m unittest discover -s src`.
