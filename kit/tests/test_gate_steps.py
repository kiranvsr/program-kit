"""The gate's step limits (KF-50), its heavy lock (KF-52) and its e2e step (KF-53, F-0630), all 0.6.1."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import json
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import gate
import land
import lock
import pk
from common import KitError
from test_gate import BRANCH, PY, TASK, HandbackBase, checks_data, commit, put, run_quiet, sh

PK = Path(__file__).resolve().parent.parent / "scripts" / "pk.py"


class StepTimeoutParseTests(unittest.TestCase):
    def parse(self, value):
        return gate._parse_checks(checks_data(step_timeout=value), ())["step_timeout"]

    def test_absent_is_the_old_fixed_limit_for_every_step(self):
        parsed = gate._parse_checks(checks_data(), ())["step_timeout"]
        self.assertEqual({step: gate.step_limit({"step_timeout": parsed}, step) for step in gate.TIMED_STEPS},
                         {step: 3600 for step in gate.TIMED_STEPS})

    def test_a_number_sets_every_step_and_a_mapping_sets_one(self):
        self.assertEqual(gate.step_limit({"step_timeout": self.parse(5400)}, "build"), 5400)
        parsed = self.parse({"default": 1800, "test": 4200})
        self.assertEqual(gate.step_limit({"step_timeout": parsed}, "test"), 4200)
        self.assertEqual(gate.step_limit({"step_timeout": parsed}, "scan"), 1800)
        self.assertEqual(gate.step_limit({"step_timeout": parsed}, "e2e"), 1800)

    def test_bad_values_are_refused(self):
        for value, words in ((0, "whole number of seconds"), ("1h", "whole number of seconds"), (True, "whole number"),
                             ({"tests": 10}, "unknown key tests"), ({"test": -1}, "whole number"),
                             ({"scale": {"margin": 0.5}}, "margin"), ({"scale": {"steps": ["nope"]}}, "steps"),
                             ({"scale": {"cap": 10, "floor": 20}}, "cap"), ({"scale": {"sideways": 1}}, "unknown")):
            with self.subTest(value=value), self.assertRaisesRegex(KitError, words):
                self.parse(value)

    def test_doctor_reports_a_bad_new_key_once_each(self):
        problems = pk._checks_problems(checks_data(step_timeout="x", lock={"nme": "y"}, e2e={"paths": ["a/"]}))
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("step_timeout" in p for p in problems))
        self.assertTrue(any("lock" in p for p in problems))
        self.assertTrue(any("e2e" in p for p in problems))


@skip_unless_slow()
class StepLimitRunTests(HandbackBase):
    def set_checks(self, **over):
        self.write_checks(**over)
        commit(self.root, "checks")
        sh(self.wt, "git", "merge", "-q", "main")

    def test_the_test_step_runs_under_its_own_limit(self):
        self.set_checks(test=[[PY, "-c", "import time; time.sleep(5)"]], step_timeout={"test": 1})
        rc, out = self.merge_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("step limit: test 1 s (step_timeout in program/checks.yaml)", out)
        self.assertIn(f"FAILED: test {PY} -c import time; time.sleep(5) (exit 124)", out)
        self.assertIn("timed out after 1s", out)
        self.assert_no_trial_left()

    def test_without_the_key_nothing_new_is_printed_and_the_limit_is_3600(self):
        seen = []
        real = gate.run_argv

        def spy(argv, cwd, env=None, timeout=None):
            seen.append((list(argv), timeout))
            return real(argv, cwd, env=env, timeout=timeout)

        with mock.patch.object(gate, "run_argv", spy):
            rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("step limit", out)
        steps = [timeout for argv, timeout in seen if argv[0] != "git"]
        self.assertTrue(steps)
        self.assertEqual(set(steps), {3600})

    def test_a_scaled_limit_comes_from_the_last_measured_run_between_floor_and_cap(self):
        times = Path(sh(self.root, "git", "rev-parse", "--path-format=absolute", "--git-common-dir").strip()) \
            / gate.TIMES_FILE
        times.write_text(json.dumps({"test": {"seconds": 45.0, "at": "2026-11-21T00:00:00Z"}}))
        self.set_checks(step_timeout={"default": 60, "scale": {"steps": ["test"], "margin": 2, "cap": 100}})
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertIn("step limit: test 90 s (scaled: 2 x 45 s measured on this machine by the last run, floor 60, "
                      "cap 100)", out)
        self.assertIn("step limit: build 60 s", out)
        recorded = json.loads(times.read_text())
        self.assertLess(recorded["test"]["seconds"], 45.0)  # this run's own time replaced it
        times.write_text(json.dumps({"test": {"seconds": 400.0, "at": "2026-11-21T00:00:00Z"}}))
        rc, out = self.merge_check()
        self.assertIn("step limit: test 100 s (scaled: 2 x 400 s measured on this machine by the last run, "
                      "floor 60, cap 100)", out)

    def test_without_scale_no_time_is_recorded(self):
        self.set_checks(step_timeout=4000)
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        common = Path(sh(self.root, "git", "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        self.assertFalse((common / gate.TIMES_FILE).exists())


@skip_unless_slow()
class GateLockTests(HandbackBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"PK_LOCK_POLL": "0.05"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PK_LOCK_HELD", None)
        os.environ.pop("PK_LOCK_NAME", None)
        self.lock_dir = self.root / ".git" / "t-heavy.lock"
        self.seen = self.root.parent / "seen-owner"

    def set_checks(self, **over):
        self.write_checks(**over)
        commit(self.root, "checks")
        sh(self.wt, "git", "merge", "-q", "main")

    def copy_owner(self):
        return [[PY, "-c", f"import os, shutil\nif os.path.exists({str(self.lock_dir / 'owner')!r}): "
                           f"shutil.copy({str(self.lock_dir / 'owner')!r}, {str(self.seen)!r})\n"
                           f"open({str(self.seen)!r} + '.env', 'w').write(os.environ.get('PK_LOCK_HELD', ''))"]]

    def live(self):
        holder = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        return holder

    def plant(self, pid):
        self.lock_dir.mkdir()
        (self.lock_dir / "owner").write_text(f"owner=someone\npid={pid}\nstarted=2026-11-21T00:00:00Z\n")

    def test_without_a_lock_setting_the_gate_takes_none(self):
        self.set_checks(test=self.copy_owner())
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.seen.exists())
        self.assertEqual(list((self.root / ".git").glob("*.lock")), [])

    def test_the_hand_back_gate_and_the_merge_check_hold_the_named_lock_while_their_steps_run(self):
        self.set_checks(test=self.copy_owner(), lock={"name": "t-heavy"})
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"owner=gate-handback-{TASK}", self.seen.read_text())
        self.assertIn("t-heavy:", Path(str(self.seen) + ".env").read_text())
        self.assertFalse(self.lock_dir.exists())
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"owner=gate-merge-check-{TASK}", self.seen.read_text())
        self.assertFalse(self.lock_dir.exists())

    def test_the_gate_waits_for_another_holder(self):
        self.set_checks(lock="t-heavy")
        holder = self.live()
        self.plant(holder.pid)
        result = {}
        worker = threading.Thread(target=lambda: result.update(out=self.merge_check()))
        worker.start()
        time.sleep(1.0)
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.trial_dirs(), [])  # it waits before the trial merge
        holder.kill()
        holder.wait()
        worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["out"][0], 0, result["out"][1])

    def test_a_gate_under_its_own_holder_does_not_wait_and_a_command_under_the_gate_can_take_it_again(self):
        nested = [[PY, str(PK), "lock", "run", "inner", "--name", "t-heavy", "--", PY, "-c", "pass"]]
        self.set_checks(test=nested, lock={"name": "t-heavy"})
        holder = self.live()
        self.plant(holder.pid)
        with mock.patch.dict(os.environ, {"PK_LOCK_HELD": f"t-heavy:{holder.pid}", "PATH": os.environ["PATH"]}):
            started = time.monotonic()
            rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertLess(time.monotonic() - started, 50)
        self.assertTrue(self.lock_dir.exists())  # the outer holder's lock stays
        self.lock_dir.joinpath("owner").unlink()
        self.lock_dir.rmdir()
        rc, out = self.handback()  # the gate's own hold, with `pk lock run` nested in its test step
        self.assertEqual(rc, 0, out)

    def test_a_bad_lock_name_is_refused(self):
        with self.assertRaisesRegex(KitError, "lock"):
            gate._parse_checks(checks_data(lock={"name": "../x"}), ())


E2E_LOG = "import os, sys; open(os.environ['PK_E2E_LOG'], 'a').write(os.getcwd() + chr(10)); sys.exit({code})"


@skip_unless_slow()
class E2eTests(HandbackBase):
    def setUp(self):
        super().setUp()
        self.log = self.root.parent / "e2e.log"
        patcher = mock.patch.dict(os.environ, {"PK_E2E_LOG": str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_checks(self, code=0, **e2e):
        self.write_checks(e2e={"command": [[PY, "-c", E2E_LOG.format(code=code)]], **e2e})
        commit(self.root, "checks")
        sh(self.wt, "git", "merge", "-q", "main")

    def test_off_unless_configured(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("e2e", out)
        self.assertFalse(self.log.exists())

    def test_the_hand_back_gate_runs_it_when_the_branch_touches_a_listed_path(self):
        self.set_checks(paths=["calc.py"])
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn("e2e: the branch changes calc.py", out)
        self.assertIn("step e2e: ok", out)
        self.assertEqual(self.log.read_text().splitlines(), [str(self.wt)])

    def test_it_is_skipped_when_nothing_listed_changed(self):
        self.set_checks(paths=["web/", "e2e/**/*.ts"])
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn("e2e: skipped (the branch changes nothing under e2e.paths)", out)
        self.assertFalse(self.log.exists())

    def test_a_failing_e2e_fails_the_gate(self):
        self.set_checks(code=3, paths=["calc.py"])
        rc, out = self.handback()
        self.assertEqual(rc, 1, out)
        self.assertIn("FAILED: e2e", out)
        self.assertIn("(exit 3)", out)

    def test_the_merge_check_re_runs_it_when_mains_specs_changed_after_the_branch_left(self):
        self.set_checks(paths=["web/"], specs=["e2e/"])
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertIn("e2e: skipped (the branch changes nothing under e2e.paths, and main's e2e specs did not change "
                      "since the branch left it)", out)
        put(self.root, "e2e/flow.spec.py", "# a new spec on main\n")
        commit(self.root, "main's e2e spec changes")
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertIn("e2e: main's e2e specs changed since the branch left it: e2e/flow.spec.py", out)
        self.assertIn("step e2e: ok", out)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("pk-trial-", self.log.read_text())  # on the trial merge
        self.assert_no_trial_left()

    def test_pk_merge_check_says_that_the_merge_check_will_run_e2e_and_runs_nothing(self):
        self.set_checks(paths=["web/"], specs=["e2e/"])
        put(self.root, "e2e/flow.spec.py", "# a new spec on main\n")
        commit(self.root, "main's e2e spec changes")
        rc, out = run_quiet(land.merge_check, self.root, TASK)
        self.assertEqual(rc, 0, out)
        self.assertIn("e2e: main's e2e specs changed since the branch left it: e2e/flow.spec.py; the merge check "
                      "(pk merge, pk gate merge-check) runs the e2e step", out)
        self.assertFalse(self.log.exists())

    def test_bad_settings_are_refused(self):
        for value, words in (({"paths": ["a/"]}, "command"), ({"command": [["x"]]}, "paths"),
                             ({"command": "x", "paths": ["a"]}, "argv"), ({"command": [["x"]], "paths": ["a"],
                                                                          "timeout": 0}, "timeout"),
                             ({"command": [["x"]], "paths": ["a"], "when": 1}, "unknown")):
            with self.subTest(value=value), self.assertRaisesRegex(KitError, words):
                gate._parse_checks(checks_data(e2e=value), ())


if __name__ == "__main__":
    unittest.main()
