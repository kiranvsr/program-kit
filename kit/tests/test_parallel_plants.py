"""Parallel planted-bug judging (0.5.2): `Evidence.planted_bugs(..., workers=N)`."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import evidence
from common import KitError

PY = sys.executable
BUILD = [PY, "-m", "compileall", "-q", "."]
CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
SELF_TEST = evidence.SELF_TEST_ID
# The test every row runs: `judge.py <mode> <tag> <shared dir>`, from the mirror root. It logs `start` and `end` lines
# (tag, and whether the mirror holds a mutant) to <shared>/log and exits 1 for a mutant, 0 for the unmutated code.
JUDGE = r'''import os, sys, time
from pathlib import Path

ORIGINAL = %r

mode, tag, shared = sys.argv[1], sys.argv[2], Path(sys.argv[3])
text = open("calc.py").read()
mutant = text != ORIGINAL
kind = "mutant" if mutant else "base"


def note(word):
    with open(shared / "log", "a") as log:
        log.write(f"{word} {tag} {kind}\n")


def count(name):
    path = shared / name
    seen = int(path.read_text()) if path.exists() else 0
    path.write_text(str(seen + 1))
    return seen


note("start")
time.sleep(float(os.environ.get("JUDGE_SLEEP", "0.3")))
space = {}
exec(text, space)
code = 0 if space["add"](2, 3) == 5 and space["sub"](5, 3) == 2 else 1  # the real test: is the code right
if mode == "meet" and mutant:  # wait until another mutant has started too, so the two certainly overlap
    deadline = time.time() + 20
    while time.time() < deadline and sum(1 for ln in open(shared / "log") if ln.split()[::2] == ["start", "mutant"]) < 2:
        time.sleep(0.05)
elif mode == "flaky-kill" and mutant and count(tag) == 0:
    print("Error: listen EADDRINUSE: address already in use 127.0.0.1:8080")
    code = 1
elif mode == "flaky-kill" and mutant:
    code = 0  # alone, the mutant is not caught: the serial verdict must be the one recorded
elif mode == "flaky-base" and not mutant and count(tag) == 0:
    code = 1
elif mode == "bad-base":
    code = 1
note("end")
sys.exit(code)
''' % CALC


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def worktrees(root):
    return [ln for ln in sh(root, "git", "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root, self.shared = base / "repo", base / "shared"
        self.root.mkdir()
        self.shared.mkdir()
        (self.shared / "log").write_text("")
        self.judge = base / "judge.py"
        self.judge.write_text(JUDGE)
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        (self.root / "calc.py").write_text(CALC)
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")
        patcher = mock.patch.dict(os.environ, {"JUDGE_SLEEP": "0.3"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(evidence.PLANT_WORKERS_ENV, None)

    def row(self, rid, mode="plain", tag=None, find="return a + b", replace="return a * b", **extra):
        return {"id": rid, "path": "calc.py", "find": find, "replace": replace,
                "test_args": [mode, tag or rid, str(self.shared)], **extra}

    def plants(self, table, workers, **kw):
        ev = evidence.Evidence(self.root, "T-1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rows = ev.planted_bugs(table, BUILD, [PY, str(self.judge)], workers=workers, **kw)
        return rows, out.getvalue()

    def log(self):
        return [line.split() for line in (self.shared / "log").read_text().splitlines()]


class Configuration(unittest.TestCase):
    def test_the_evidence_section_defaults_to_one_worker_and_no_reuse(self):
        self.assertEqual(evidence.parse_config(None), {"plant_workers": 1, "reuse_unchanged_code": False,
                                                       "reuse_ignore": [], "reuse_compare": []})
        self.assertEqual(evidence.parse_config({"plant_workers": 4})["plant_workers"], 4)

    def test_a_bad_evidence_section_is_refused_by_name(self):
        for bad, words in (({"plant_workers": 0}, "plant_workers"), ({"plant_workers": True}, "plant_workers"),
                           ({"plant_workers": "4"}, "plant_workers"), ({"reuse_unchanged_code": "yes"}, "reuse"),
                           ({"workers": 2}, "unknown key workers"), ([1], "mapping"),
                           ({"reuse_ignore": ["program/x"]}, "under program/"),
                           ({"reuse_ignore": ["kit/"]}, "the kit"),
                           ({"reuse_compare": ["src/"]}, "not under program/"),
                           ({"reuse_compare": ["../x"]}, "relative")):
            with self.assertRaises(KitError, msg=bad) as ctx:
                evidence.parse_config(bad)
            self.assertIn(words, str(ctx.exception))

    def test_the_gate_and_doctor_accept_the_section_and_refuse_a_bad_one(self):
        import gate

        self.assertEqual(gate._parse_checks({"evidence": {"plant_workers": 2}}, ())["evidence"]["plant_workers"], 2)
        with self.assertRaises(KitError):
            gate._parse_checks({"evidence": {"plant_workers": -1}}, ())


class Workers(Base):
    def commit_checks(self, text):
        (self.root / "program").mkdir(exist_ok=True)
        (self.root / "program" / "checks.yaml").write_text(text)
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "checks")

    def test_workers_come_from_the_argument_then_the_environment_then_the_committed_checks(self):
        self.assertEqual(evidence.plant_workers(self.root), 1)  # no checks.yaml: one worker, as before 0.5.2
        self.commit_checks("evidence:\n  plant_workers: 3\n")
        self.assertEqual(evidence.plant_workers(self.root), 3)
        (self.root / "program" / "checks.yaml").write_text("evidence:\n  plant_workers: 9\n")  # not committed
        self.assertEqual(evidence.plant_workers(self.root), 3)
        with mock.patch.dict(os.environ, {evidence.PLANT_WORKERS_ENV: "2"}):
            self.assertEqual(evidence.plant_workers(self.root), 2)
            self.assertEqual(evidence.plant_workers(self.root, 1), 1)
        with mock.patch.dict(os.environ, {evidence.PLANT_WORKERS_ENV: "two"}), self.assertRaises(KitError):
            evidence.plant_workers(self.root)
        with self.assertRaises(KitError):
            evidence.plant_workers(self.root, 0)

    def test_a_serial_flag_that_is_not_a_boolean_is_a_table_error(self):
        ev = evidence.Evidence(self.root, "T-1")
        with mock.patch.object(evidence, "run_argv") as runner, self.assertRaises(KitError) as ctx:
            ev.planted_bugs([self.row("a", serial="yes")], BUILD, [PY], workers=2)
        self.assertIn("serial must be true or false", str(ctx.exception))
        runner.assert_not_called()

    def test_a_parallel_run_gives_the_rows_of_a_serial_run_in_table_order(self):
        table = [self.row("mul"), self.row("syntax", replace="return a +"),
                 self.row("noop", replace="return b + a"), self.row("sub", find="return a - b", replace="return a")]
        serial, quiet = self.plants(table, 1)
        parallel, printed = self.plants(table, 3)
        self.assertEqual(quiet, "")  # one worker prints nothing, as before
        self.assertTrue(all("judged" not in row for row in serial))  # and writes the rows exactly as before
        self.assertEqual([{k: v for k, v in row.items() if k != "judged"} for row in parallel], serial)
        self.assertEqual([(row["id"], row["kill_reason"], row["judged"]) for row in parallel],
                         [("mul", "test failed", "parallel"), ("syntax", "build failed", "serial-recheck"),
                          ("noop", "not killed", "serial-recheck"), ("sub", "test failed", "parallel"),
                          (SELF_TEST, "build failed", "serial")])
        sha = "a" * 40
        record = {"task_id": "T-1", "head": sha, "main": sha, "merge_base": sha, "produced_at": "2026-01-01T00:00:00Z",
                  "inputs": [{"path": "calc.py", "sha256": "0" * 64}], "commands": [], "planted_bugs": parallel,
                  "planted_bugs_all_killed": False, "all_commands_as_expected": True, "tree_unchanged_by_run": True,
                  "blocked_by": [], "deviations": []}
        self.assertEqual(evidence.validate_results(record), [])  # `judged` is an optional field of a valid row
        record["planted_bugs"] = [dict(parallel[0], judged="fast"), *parallel[1:]]
        self.assertEqual(evidence.validate_results(record),
                         ["planted_bugs[0]: judged must be one of parallel, serial, serial-recheck (found 'fast')"])
        lines = printed.splitlines()  # one whole line per finished baseline and row
        self.assertEqual(len([ln for ln in lines if ln.startswith("baseline of ")]), 4)
        self.assertEqual(len([ln for ln in lines if ln.startswith("plant ")]), 7)  # 5 rows, 2 judged again
        for line in lines:
            self.assertRegex(line, r"\A(baseline of|plant) \S+: [a-z ,]+ \((parallel|serial|serial-recheck), \d+ s\)"
                                   r"( \[\d+/5\])?\Z")
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "")
        self.assertEqual(len(worktrees(self.root)), 1)


@skip_unless_slow()
class Pool(Base):
    def test_pool_jobs_overlap_each_in_its_own_mirror_and_every_mirror_is_removed(self):
        made, real = [], tempfile.mkdtemp

        def spy(*a, **kw):
            made.append(real(*a, **kw))
            return made[-1]

        with mock.patch.object(evidence.tempfile, "mkdtemp", spy):
            rows, _ = self.plants([self.row("a", mode="meet", tag="t"), self.row("b", mode="meet", tag="t",
                                                                                replace="return 0")], 2)
        self.assertEqual([row["judged"] for row in rows], ["parallel", "parallel", "serial"])
        self.assertEqual(len(made), 4)  # one baseline (a and b share their test), two rows, the self-test row
        self.assertEqual(len(set(made)), 4)
        self.assertFalse(any(os.path.exists(path) for path in made))
        self.assertEqual(len(worktrees(self.root)), 1)
        starts = [i for i, entry in enumerate(self.log()) if entry[0] == "start" and entry[2] == "mutant"]
        ends = [i for i, entry in enumerate(self.log()) if entry[0] == "end" and entry[2] == "mutant"]
        self.assertLess(starts[1], ends[0], self.log())  # the second row started before the first one ended

    def test_worktree_add_and_remove_never_run_at_once(self):
        lock, state, real = threading.Lock(), {"now": 0, "most": 0}, evidence.git

        def counting(root, *args):
            if args[:1] != ("worktree",):
                return real(root, *args)
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            try:
                time.sleep(0.05)
                return real(root, *args)
            finally:
                with lock:
                    state["now"] -= 1

        table = [self.row(f"r{n}", replace=f"return a * {n}") for n in range(6)]
        with mock.patch.object(evidence, "git", counting):
            rows, _ = self.plants(table, 4)
        self.assertTrue(all(row["killed"] for row in rows[:-1]))
        self.assertEqual(state["most"], 1)

    def test_serial_rows_and_the_self_test_run_after_the_pool_one_at_a_time(self):
        table = [self.row("e2e", tag="port", serial=True), self.row("p1", replace="return 1"),
                 self.row("p2", replace="return 2"), self.row("e2e-2", tag="port", serial=True, replace="return 3")]
        rows, _ = self.plants(table, 3)
        self.assertEqual([(row["id"], row["judged"]) for row in rows],
                         [("e2e", "serial"), ("p1", "parallel"), ("p2", "parallel"), ("e2e-2", "serial"),
                          (SELF_TEST, "serial")])
        log = self.log()
        last_pool_end = max(i for i, entry in enumerate(log) if entry[0] == "end" and entry[1] in ("p1", "p2"))
        port = [i for i, entry in enumerate(log) if entry[1] == "port"]
        self.assertGreater(min(port), last_pool_end, log)  # the serial rows' baseline too: it uses their port
        self.assertEqual([log[i][0] for i in port], ["start", "end"] * 3, log)  # never two at once

    def test_a_pool_kill_showing_a_resource_error_is_judged_again_alone_and_that_verdict_recorded(self):
        rows, printed = self.plants([self.row("busy", mode="flaky-kill"), self.row("fine", replace="return 0")], 2)
        self.assertEqual([(row["id"], row["killed"], row["kill_reason"], row["judged"]) for row in rows[:2]],
                         [("busy", False, "not killed", "serial-recheck"), ("fine", True, "test failed", "parallel")])
        self.assertIn("plant busy: test failed (parallel", printed)
        self.assertIn("plant busy: not killed (serial-recheck", printed)

    def test_a_baseline_failing_in_the_pool_is_run_again_alone_and_puts_its_kills_in_doubt(self):
        rows, printed = self.plants([self.row("first", mode="flaky-base", tag="b"),
                                     self.row("second", mode="flaky-base", tag="b", replace="return 0"),
                                     self.row("other", replace="return 1")], 3)
        self.assertEqual([(row["id"], row["killed"], row["judged"]) for row in rows],
                         [("first", True, "serial-recheck"), ("second", True, "serial-recheck"),
                          ("other", True, "parallel"), (SELF_TEST, False, "serial")])
        self.assertIn("baseline of first: fails, judged again alone (parallel", printed)
        self.assertIn("baseline of first: passes (serial-recheck", printed)

    def test_a_baseline_that_fails_alone_too_fails_the_run_as_before_and_leaves_no_mirror(self):
        with self.assertRaises(KitError) as ctx:
            self.plants([self.row("a", mode="bad-base"), self.row("b", replace="return 0")], 2)
        self.assertEqual(str(ctx.exception), "unmutated mirror fails its own test (exit 1); a failing test kills "
                                             "nothing (row a)")
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_an_error_in_one_job_is_raised_after_the_pool_drained_and_no_mirror_is_left(self):
        real = evidence.run_argv

        def exploding(argv, cwd, *a, **kw):
            if "boom" in argv and "return 9" in (Path(cwd) / "calc.py").read_text():
                raise RuntimeError("runner exploded")
            return real(argv, cwd, *a, **kw)

        with mock.patch.object(evidence, "run_argv", exploding), self.assertRaises(RuntimeError):
            self.plants([self.row("ok"), self.row("x", tag="boom", replace="return 9")], 2)
        self.assertEqual(len(worktrees(self.root)), 1)


@skip_unless_slow()
class Timing(Base):
    def test_four_workers_take_well_under_the_serial_time(self):
        os.environ["JUDGE_SLEEP"] = "1"
        table = [self.row(f"r{n}", replace=f"return a * {n}") for n in range(8)]
        started = time.monotonic()
        self.plants(table, 1)
        serial = time.monotonic() - started
        started = time.monotonic()
        self.plants(table, 4)
        parallel = time.monotonic() - started
        self.assertLess(parallel, serial * 0.6, (serial, parallel))


if __name__ == "__main__":
    unittest.main()
