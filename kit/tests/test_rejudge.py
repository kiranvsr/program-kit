"""`evidence.PlantJudge` (one edit on a clean detached mirror, build, judge) and `pk gate rejudge`, which re-runs
only the named planted-bug rows of a task branch and writes nothing."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evidence
import gate
import pk
import rejudge
from common import KitError

PY = sys.executable
BUILD = [PY, "-m", "compileall", "-q", "."]
TEST = [PY, "-m", "unittest", "-q"]
TASK = "P0-M01-S01-T01"
BRANCH = f"task/{TASK}"
CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_TEST = ("import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n\n"
             "if __name__ == '__main__':\n    unittest.main()\n")
TABLE = [{"id": "A1", "path": "calc.py", "find": "return a + b", "replace": "return a - b", "test_args": ["test_calc"]},
         {"id": "A2", "path": "calc.py", "find": "return a - b", "replace": "return a + b", "test_args": ["test_calc"]}]
ROWS_PRODUCER = '''import sys

BUILD = [sys.executable, "-m", "compileall", "-q", "."]
TEST = [sys.executable, "-m", "unittest", "-q", "test_calc"]


def rejudge_rows():
    print("noise from the producer")
    return [{"id": "A1", "path": "calc.py", "find": "return a + b", "replace": "return a +", "build_argv": BUILD,
             "test_argv": TEST},
            {"id": "A3", "path": "calc.py", "find": "return a + b", "replace": "return b + a", "build_argv": BUILD,
             "test_argv": TEST}]


if __name__ == "__main__":
    raise SystemExit("the producer's main must not run")
'''


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def commit(cwd, message="change"):
    sh(cwd, "git", "add", "-A")
    sh(cwd, "git", "commit", "-q", "-m", message)
    return sh(cwd, "git", "rev-parse", "HEAD").strip()


def worktrees(root):
    return [ln for ln in sh(root, "git", "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]


def make_repo(case):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve() / "repo"
    root.mkdir()
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.name", "Test")
    sh(root, "git", "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n.worktrees/\n")
    (root / "calc.py").write_text(CALC)
    (root / "test_calc.py").write_text(CALC_TEST)
    commit(root, "init")
    return root


def judge_row(rid, find, replace, path="calc.py", build=True, test=True):
    return {"id": rid, "path": path, "find": find, "replace": replace,
            "build_argv": BUILD if build else None, "test_argv": TEST + ["test_calc"] if test else None}


class PlantJudgeTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)

    def run_rows(self, rows, **kw):
        with evidence.PlantJudge(self.root, "HEAD", **kw) as judge:
            return [judge.attempt(row) for row in rows]

    def test_a_row_is_killed_survives_or_fails_to_build_as_the_gate_would_judge_it(self):
        killed, survived, broken = self.run_rows([judge_row("k", "return a + b", "return a - b"),
                                                  judge_row("s", "return a + b", "return b + a"),
                                                  judge_row("b", "return a + b", "return a +")])
        self.assertEqual((killed["builds"], killed["killed"], killed["kill_reason"]), (True, True, "test failed"))
        self.assertEqual((survived["builds"], survived["killed"], survived["kill_reason"]),
                         (True, False, "not killed"))
        self.assertEqual((broken["builds"], broken["killed"], broken["kill_reason"]), (False, False, "build failed"))
        self.assertNotEqual(broken["build_exit"], 0)
        self.assertTrue(broken["build_tail"], broken)
        self.assertIsNone(broken["test_exit"])

    def test_each_attempt_starts_from_a_clean_mirror_and_nothing_is_left_behind(self):
        before = sh(self.root, "git", "status", "--porcelain")
        first, second = self.run_rows([judge_row("one", "return a + b", "return a - b"),
                                       judge_row("two", "return a + b", "return b + a")])
        self.assertTrue(first["killed"])
        self.assertEqual(second["kill_reason"], "not killed")  # it found its find: the first edit was undone
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), before)
        self.assertEqual(len(worktrees(self.root)), 1)
        self.assertEqual((self.root / "calc.py").read_text(), CALC)

    def test_a_find_that_does_not_occur_once_is_a_problem_not_a_verdict(self):
        gone, twice = self.run_rows([judge_row("g", "return a * b", "x"), judge_row("t", "return a", "x")])
        self.assertIsNone(gone["killed"])
        self.assertIn("find occurs 0 times in calc.py", gone["problem"])
        self.assertIn("find occurs 2 times in calc.py", twice["problem"])

    def test_a_path_outside_the_mirror_or_missing_is_refused(self):
        for path in ("../calc.py", "/etc/hosts", "nope.py"):
            with self.subTest(path=path):
                (result,) = self.run_rows([judge_row("p", "a", "b", path=path)])
                self.assertIsNone(result["killed"])
                self.assertTrue(result["problem"], result)

    def test_build_only_reports_whether_the_edit_builds(self):
        good, bad = self.run_rows([judge_row("g", "return a + b", "return a - b", test=False),
                                   judge_row("b", "return a + b", "return a +", test=False)])
        self.assertEqual((good["builds"], good["killed"], good["kill_reason"]), (True, None, "builds"))
        self.assertEqual((bad["builds"], bad["kill_reason"]), (False, "build failed"))

    def test_several_build_commands_run_in_order_and_the_first_failure_stops(self):
        row = dict(judge_row("m", "return a + b", "return a - b", test=False),
                   build_argvs=[BUILD, [PY, "-c", "import sys; sys.exit(3)"], [PY, "-c", "raise SystemExit(9)"]])
        (result,) = self.run_rows([row])
        self.assertEqual((result["builds"], result["build_exit"]), (False, 3))
        self.assertIn("sys.exit(3)", " ".join(result["build_argv"]))

    def test_the_baseline_runs_the_unmutated_mirror(self):
        with evidence.PlantJudge(self.root, "HEAD") as judge:
            self.assertEqual(judge.baseline([BUILD], TEST + ["test_calc"])["ok"], True)
            failing = judge.baseline([[PY, "-c", "raise SystemExit(4)"]], None)
        self.assertEqual((failing["ok"], failing["exit"]), (False, 4))

    def test_prepare_runs_on_the_new_mirror_and_after_every_restore(self):
        def prepare(mirror):
            (mirror / "extra.py").write_text("VALUE = 1\n")

        first, second = self.run_rows([judge_row("x", "VALUE = 1", "VALUE = (", path="extra.py", test=False),
                                       judge_row("y", "VALUE = 1", "VALUE = 2", path="extra.py", test=False)],
                                      prepare=prepare)
        self.assertEqual(first["kill_reason"], "build failed")
        self.assertEqual(second["kill_reason"], "builds")

    def test_a_ref_other_than_head_is_mirrored(self):
        sh(self.root, "git", "checkout", "-q", "-b", "other")
        (self.root / "calc.py").write_text(CALC.replace("a + b", "a + b + 0"))
        commit(self.root, "other")
        sh(self.root, "git", "checkout", "-q", "main")
        with evidence.PlantJudge(self.root, "other") as judge:
            on_other = judge.attempt(judge_row("o", "return a + b + 0", "return a - b"))
        self.assertTrue(on_other["killed"], on_other)


@skip_unless_slow()
class RejudgeTests(unittest.TestCase):
    """`pk gate rejudge`: the named rows of the task branch, from its producer or its latest results record."""

    def setUp(self):
        self.root = make_repo(self)
        sh(self.root, "git", "branch", BRANCH)
        self.wt = self.root / ".worktrees" / TASK.lower()
        sh(self.root, "git", "worktree", "add", "-q", str(self.wt), BRANCH)
        ev = evidence.Evidence(self.wt, TASK)
        ev.hash_inputs(["calc.py", "test_calc.py"])
        ev.run_command("unit", TEST + ["test_calc"])
        ev.planted_bugs(TABLE, BUILD, TEST)
        ev.write()
        self.tip = commit(self.wt, "the evidence commit")

    def rejudge(self, rows, branch=BRANCH, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rejudge.rejudge(self.root, TASK, branch, rows, **kw)
        return code, out.getvalue()

    def assert_nothing_written(self):
        self.assertEqual(sh(self.wt, "git", "status", "--porcelain"), "")
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "")
        self.assertEqual(sh(self.root, "git", "rev-parse", BRANCH).strip(), self.tip)
        self.assertEqual(len(worktrees(self.root)), 2)  # the main checkout and the task worktree

    def test_rows_from_the_latest_record_are_re_judged_on_the_branch_tip(self):
        code, out = self.rejudge(["A1", "A2"])
        self.assertEqual(code, 1, out)  # A2 plants a + b where a + b already is: the test still passes
        lines = out.splitlines()
        self.assertIn("not gate evidence", lines[0])
        self.assertIn("not gate evidence", lines[-1])
        self.assertTrue(any(ln.startswith("A1 killed") for ln in lines), out)
        self.assertTrue(any(ln.startswith("A2 not killed") for ln in lines), out)
        self.assertIn("latest results record", out)
        self.assert_nothing_written()

    def test_only_the_named_rows_run(self):
        code, out = self.rejudge(["A1"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("A2", out)
        self.assertNotIn(evidence.SELF_TEST_ID, out)
        self.assertIn("1 of 1 killed", out)

    def test_a_producer_with_rejudge_rows_is_preferred_and_its_main_never_runs(self):
        produce = self.wt / "program" / "evidence" / TASK / "produce.py"
        produce.write_text(ROWS_PRODUCER)
        self.tip = commit(self.wt, "the producer names its rows")
        code, out = self.rejudge(["A1", "A3"])
        self.assertEqual(code, 1, out)
        self.assertIn("rejudge_rows()", out)
        self.assertTrue(any(ln.startswith("A1 build failed") for ln in out.splitlines()), out)
        self.assertTrue(any(ln.startswith("A3 not killed") for ln in out.splitlines()), out)
        self.assertNotIn("must not run", out)
        self.assert_nothing_written()

    def test_an_unknown_row_is_refused_naming_the_known_rows(self):
        with self.assertRaises(KitError) as ctx:
            self.rejudge(["A1", "Z9"])
        self.assertIn("Z9", str(ctx.exception))
        self.assertIn("A1", str(ctx.exception))
        self.assert_nothing_written()

    def test_the_branch_must_be_the_tasks_own(self):
        with self.assertRaises(KitError):
            self.rejudge(["A1"], branch="main")
        with self.assertRaises(KitError):
            rejudge.rejudge(self.root, TASK, "task/P0-M01-S01-T02", ["A1"])

    def test_a_branch_without_a_record_or_rows_is_refused(self):
        sh(self.root, "git", "branch", "task/P0-M01-S01-T02")
        with self.assertRaises(KitError) as ctx, contextlib.redirect_stdout(io.StringIO()):
            rejudge.rejudge(self.root, "P0-M01-S01-T02", "task/P0-M01-S01-T02", ["A1"])
        self.assertIn("rejudge_rows", str(ctx.exception))

    def test_the_baseline_flag_runs_the_unmutated_judge_first(self):
        code, out = self.rejudge(["A1"], baseline=True)
        self.assertEqual(code, 0, out)
        self.assertIn("baseline", out)

    def test_pk_gate_rejudge_is_wired(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pk.main(["gate", "rejudge", TASK, "--branch", BRANCH, "--rows", "A1"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn("A1 killed", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pk.main(["gate", "rejudge", TASK, "--branch", BRANCH]), 1)  # --rows is required


if __name__ == "__main__":
    unittest.main()
