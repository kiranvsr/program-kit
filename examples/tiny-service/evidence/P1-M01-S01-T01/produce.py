"""Append a results record for this task to program/evidence/P1-M01-S01-T01/results/.

Run by the hand-back gate from the clean task worktree: python3 program/evidence/P1-M01-S01-T01/produce.py
It records the head and main commits, the hash of every input, each PLAN command exactly as written with its
real exit code, and the planted bugs the unit tests must catch. The record is named by its digest, never
rewritten, and program/evidence/index.json then points at it as the task's latest record.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # <repo>/program/evidence/<ID>/produce.py
sys.path.insert(0, str(ROOT / "kit" / "scripts"))  # the kit is installed at <repo>/kit

import evidence  # noqa: E402
import gate  # noqa: E402

TASK = "P1-M01-S01-T01"
PLAN = f"program/tasks/{TASK}/PLAN.md"
BUILD = ["python3", "-m", "compileall", "-q", "."]
TEST = ["python3", "-m", "unittest"]
TEST_ARGS = ["discover", "-s", "src"]
SERVICE = "src/tiny_service.py"
PLANTED_BUGS = [
    {"id": "health-reports-down", "path": SERVICE, "find": '{"status": "ok"}', "replace": '{"status": "down"}'},
    {"id": "limit-off-by-one", "path": SERVICE, "find": "len(body) > MAX_BODY", "replace": "len(body) >= MAX_BODY"},
    {"id": "bad-json-uncaught", "path": SERVICE, "find": "except (UnicodeDecodeError, ValueError):",
     "replace": "except UnicodeDecodeError:"},
    {"id": "listens-on-every-address", "path": SERVICE, "find": 'HOST = "127.0.0.1"', "replace": 'HOST = "0.0.0.0"'},
]


def tests_run(output):
    """The number of tests unittest reports; raises (so the command is not 'as expected') when it reports none."""
    return {"tests": int(re.search(r"^Ran (\d+) tests? in ", output, re.MULTILINE).group(1))}


def main():
    run = evidence.Evidence(ROOT, TASK)
    run.require_clean_tree()
    run.hash_inputs(["src", PLAN, f"program/tasks/{TASK}/SPEC.md", "program/checks.yaml", ".gitignore"])
    plan = (ROOT / PLAN).read_text(encoding="utf-8")
    for argv, expect in gate.parse_plan(plan):  # the PLAN's own lines, never a copy of them
        run.run_command(" ".join(argv), argv, expect=expect, parse=tests_run if "unittest" in argv else None)
    table = [dict(bug, test_args=TEST_ARGS) for bug in PLANTED_BUGS]
    run.planted_bugs(table, build=BUILD, test=TEST)
    run.stale()
    run.write()


if __name__ == "__main__":
    main()
