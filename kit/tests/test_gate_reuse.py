"""The gate's evidence reuse on unchanged code and its test-step output for producers (0.5.2)."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evidence
import gate
import reuse
import store
from common import KitError, sha256_bytes
from test_gate import BRANCH, PY, TASK, HandbackBase, checks_data, commit, put, sh

CONFIG = {"plant_workers": 1, "reuse_unchanged_code": True, "reuse_ignore": ["docs/"],
          "reuse_compare": ["program/tools/"]}
# A producer that takes the unit test's row from the gate's own test step when it can.
PRODUCE_REUSING = '''import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["PK_SCRIPTS"])
import evidence

ev = evidence.Evidence(Path.cwd(), "P0-M01-S01-T01")
ev.hash_inputs(["calc.py", "test_calc.py"])
ev.reuse_gate_test("unit", ["unit"], parse=lambda out: {"ran": "OK" in out})
ev.run_command("unit again", ["unit", "-v"])
ev.planted_bugs([{"id": "add-minus", "path": "calc.py", "find": "return a + b", "replace": "return a - b",
                  "test_args": ["test_calc"]}],
                build=[sys.executable, "-m", "compileall", "-q", "."], test=[sys.executable, "-m", "unittest", "-q"])
ev.write()
'''


class Compared(unittest.TestCase):
    def test_what_counts_as_the_code_of_a_task(self):
        counts = {path: reuse.compared(path, TASK, CONFIG) for path in (
            "calc.py", "docs/guide.md", "docs/poetry.lock", "kit/VERSION", "program/checks.yaml",
            "program/DASHBOARD.md", "program/rulings.md", f"program/tasks/{TASK}/PLAN.md",
            f"program/tasks/OTHER/PLAN.md", f"program/evidence/{TASK}/produce.py", f"program/evidence/{TASK}/latest.json",
            f"program/evidence/{TASK}/results/2026-x.json", f"program/evidence/OTHER/produce.py",
            "program/tools/plants.py", "program/toolsx/a.py")}
        self.assertEqual({path for path, yes in counts.items() if yes},
                         {"calc.py", "docs/poetry.lock", "kit/VERSION", "program/checks.yaml",
                          f"program/tasks/{TASK}/PLAN.md", f"program/evidence/{TASK}/produce.py",
                          "program/tools/plants.py"})


class GateTestOutput(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        for argv in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.name", "T"],
                     ["git", "config", "user.email", "t@example.com"]):
            sh(self.root, *argv)
        (self.root / "a.txt").write_text("a\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")
        self.results = Path(tmp.name).resolve() / "test-step.json"
        patcher = mock.patch.dict(os.environ, {evidence.GATE_TEST_ENV: str(self.results)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def step(self, **over):
        data = {"root": str(self.root), "head": sh(self.root, "git", "rev-parse", "HEAD").strip(),
                "commands": [{"argv": ["unit"], "exit": 0, "output": "Ran 3 tests\nOK\n"}]}
        data.update(over)
        self.results.write_text(json.dumps(data))

    def test_a_byte_identical_command_at_this_head_is_not_run_again(self):
        self.step()
        ev = evidence.Evidence(self.root, "T-1")
        with mock.patch.object(evidence, "run_argv") as runner:
            row = ev.reuse_gate_test("unit", ["unit"], parse=lambda out: {"ok": "OK" in out})
        runner.assert_not_called()
        self.assertEqual(row, {"label": "unit", "argv": ["unit"], "exit": 0, "expected_exit": 0, "as_expected": True,
                               "derived": {"ok": True}, "output_sha256": sha256_bytes(b"Ran 3 tests\nOK\n"),
                               "output_tail": "Ran 3 tests\nOK\n", "from_gate_step": "test"})
        self.assertEqual(ev.commands, [row])

    def test_anything_else_runs_the_command(self):
        cases = ({"head": "0" * 40}, {"root": str(self.root / "elsewhere")}, {"commands": [{"argv": ["unit", "-v"],
                                                                                          "exit": 0, "output": ""}]})
        for over in cases:
            self.step(**over)
            ev = evidence.Evidence(self.root, "T-1")
            with mock.patch.object(evidence, "run_argv", return_value=(0, "ran\n")) as runner:
                row = ev.reuse_gate_test("unit", ["unit"])
            runner.assert_called_once()
            self.assertNotIn("from_gate_step", row, over)
        self.results.write_text("not json")
        with mock.patch.object(evidence, "run_argv", return_value=(0, "ran\n")) as runner:
            evidence.Evidence(self.root, "T-1").reuse_gate_test("unit", ["unit"])
        runner.assert_called_once()
        with mock.patch.dict(os.environ, {evidence.GATE_TEST_ENV: ""}), \
                mock.patch.object(evidence, "run_argv", return_value=(0, "ran\n")) as runner:
            evidence.Evidence(self.root, "T-1").reuse_gate_test("unit", ["unit"])
        runner.assert_called_once()

    def test_a_failing_gate_command_is_recorded_as_it_ran(self):
        self.step(commands=[{"argv": ["unit"], "exit": 1, "output": "FAILED\n"}])
        row = evidence.Evidence(self.root, "T-1").reuse_gate_test("unit", ["unit"])
        self.assertEqual((row["exit"], row["as_expected"], row["from_gate_step"]), (1, False, "test"))


class ReuseBase(HandbackBase):
    def setUp(self):
        super().setUp()
        self.wrapper("unit", f'echo unit "$@" >> "$PK_FAKE_LOG"\nexec "{PY}" -m unittest -q "$@"')
        put(self.root, "kit/VERSION", "0.5.2\n")
        self.write_checks(test=[["unit"]], evidence={"reuse_unchanged_code": True, "reuse_ignore": ["docs/"]})
        commit(self.root, "opt in to reuse")
        sh(self.wt, "git", "merge", "-q", "--no-edit", "main")

    def first_record(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn("evidence reuse: none, the task has no results record yet", out.splitlines())
        commit(self.wt, "commit the evidence")
        return self.latest()

    def units(self):
        return [ln for ln in self.fake_log.read_text().splitlines() if ln.startswith("unit")] \
            if self.fake_log.exists() else []


@skip_unless_slow()
class ReuseTests(ReuseBase):
    def test_a_program_only_change_reuses_the_record_and_binds_the_new_head(self):
        entry, old = self.first_record()
        put(self.wt, "program/DASHBOARD.md", "# Dashboard\n")
        put(self.wt, "docs/guide.md", "ignored by evidence.reuse_ignore\n")
        head = commit(self.wt, "a program-only change")
        self.fake_log.write_text("")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        self.assertIn(f"evidence reuse: {entry['path']} at {old['head']} has the same code as HEAD; the test step "
                      "and produce.py do not run", lines)
        self.assertIn("step test: skipped (the reused record holds its results)", lines)
        self.assertEqual(self.units(), [])  # neither the test step nor the producer ran the suite
        new_entry, new = self.latest()
        self.assertEqual(new["reused_from"], {"path": entry["path"], "digest": entry["digest"], "head": old["head"]})
        self.assertEqual((new["head"], new["commands"], new["planted_bugs"]), (head, old["commands"],
                                                                                old["planted_bugs"]))
        self.assertEqual(new["supersedes"], entry["digest"])
        self.assertIn(f"step evidence: reused {entry['path']}, wrote {new_entry['path']}", lines)
        self.assertEqual(evidence.gate_problems(new, TASK, head), [])
        commit(self.wt, "commit the reused evidence")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.binding(self.root, TASK, BRANCH), [])
        rc, out = self.handback()  # a reused record is reused again while the code stays the same
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.latest()[1]["reused_from"]["digest"], new_entry["digest"])

    def assert_full_run(self, why):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn(why, out.splitlines())
        self.assertNotIn("reused_from", self.latest()[1])
        self.assertTrue(self.units())

    def test_a_one_byte_code_change_runs_everything(self):
        _, old = self.first_record()
        put(self.wt, "notes.txt", "one!\n")
        commit(self.wt, "one byte")
        self.assert_full_run(f"evidence reuse: none, the code changed since {old['head'][:12]}: notes.txt")

    def test_a_plan_change_runs_everything(self):
        _, old = self.first_record()
        plan = self.root / "program" / "tasks" / TASK / "PLAN.md"
        plan.write_text(plan.read_text() + "\nA note.\n")
        commit(self.root, "plan note")
        sh(self.wt, "git", "merge", "-q", "--no-edit", "main")
        self.assert_full_run(f"evidence reuse: none, the code changed since {old['head'][:12]}: "
                             f"program/tasks/{TASK}/PLAN.md")

    def test_a_producer_change_runs_everything(self):
        _, old = self.first_record()
        path = self.wt / "program" / "evidence" / TASK / "produce.py"
        path.write_text(path.read_text() + "# a comment\n")
        commit(self.wt, "producer")
        self.assert_full_run(f"evidence reuse: none, the code changed since {old['head'][:12]}: "
                             f"program/evidence/{TASK}/produce.py")

    def test_a_lockfile_change_runs_everything_even_under_an_ignored_path(self):
        put(self.wt, "docs/poetry.lock", "a = 1\n")
        commit(self.wt, "lockfile")
        _, old = self.first_record()
        put(self.wt, "docs/poetry.lock", "a = 2\n")
        commit(self.wt, "lockfile bump")
        self.assert_full_run(f"evidence reuse: none, the code changed since {old['head'][:12]}: docs/poetry.lock")

    def test_a_kit_version_change_runs_everything(self):
        _, old = self.first_record()
        put(self.root, "kit/VERSION", "0.5.3\n")
        commit(self.root, "kit upgrade")
        sh(self.wt, "git", "merge", "-q", "--no-edit", "main")
        self.assert_full_run(f"evidence reuse: none, the kit version changed since {old['head'][:12]} "
                             "(0.5.2 to 0.5.3)")

    def test_no_reuse_runs_everything(self):
        self.first_record()
        put(self.wt, "program/DASHBOARD.md", "# Dashboard\n")
        commit(self.wt, "a program-only change")
        rc, out = self.handback(no_reuse=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("evidence reuse: off for this run (--no-reuse)", out.splitlines())
        self.assertNotIn("reused_from", self.latest()[1])
        commit(self.wt, "commit the evidence")
        rc, out, _ = self.run_cli("handback", TASK, "--branch", BRANCH, "--no-reuse")
        self.assertEqual(rc, 0, out)
        self.assertIn("evidence reuse: off for this run (--no-reuse)", out.splitlines())

    def test_a_record_with_a_survivor_is_never_reused(self):
        os.environ["PK_SURVIVOR"] = "1"
        rc, out = self.handback()
        self.assertEqual(rc, 1, out)
        commit(self.wt, "commit the failing evidence")
        del os.environ["PK_SURVIVOR"]
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        reason = [ln for ln in out.splitlines() if ln.startswith("evidence reuse: none, the latest record ")]
        self.assertTrue(reason and reason[0].endswith("planted_bugs_all_killed: is not true (a planted bug survived)"),
                        out)

    def test_reuse_is_off_without_the_opt_in(self):
        self.first_record()
        self.write_checks(test=[["unit"]])
        commit(self.root, "opt out")
        sh(self.wt, "git", "merge", "-q", "--no-edit", "main")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertFalse([ln for ln in out.splitlines() if ln.startswith("evidence reuse")])
        self.assertNotIn("reused_from", self.latest()[1])


@skip_unless_slow()
class GateTestStepTests(ReuseBase):
    def test_a_producer_reuses_the_gate_test_step_and_the_suite_runs_once(self):
        put(self.wt, f"program/evidence/{TASK}/produce.py", PRODUCE_REUSING)
        commit(self.wt, "a producer that reuses the gate's test step")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.units(), ["unit", "unit -v"])  # the gate's test step, then only the other command
        _, data = self.latest()
        unit, again = data["commands"]
        self.assertEqual((unit["label"], unit["from_gate_step"], unit["derived"], unit["as_expected"]),
                         ("unit", "test", {"ran": True}, True))
        self.assertNotIn("from_gate_step", again)
        self.assertEqual([p.name for p in self.tmpdir.iterdir()], [])  # the step's results file is removed

    def test_an_unchanged_producer_runs_its_commands_as_before(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.units(), ["unit"])  # the PRODUCE fixture runs python -m unittest itself
        self.assertNotIn("from_gate_step", self.latest()[1]["commands"][0])


if __name__ == "__main__":
    unittest.main()
