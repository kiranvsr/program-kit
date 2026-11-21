"""KF-49 (0.6.1): every planted-bug run has its own TMPDIR (TMP, TEMP), removed after it, in every runner."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evidence

PY = sys.executable
BUILD = [PY, "-m", "compileall", "-q", "."]
CALC = "def add(a, b):\n    return a + b\n"
# `probe.py <log> [hang]`: logs its TMPDIR, TMP and TEMP, leaves a file and a folder there as a leaky test would, then
# fails for a mutant (add is wrong) and passes for the unmutated code; with `hang` a mutant sleeps past its limit.
PROBE = r'''import os, sys, tempfile, time
log = sys.argv[1]
with open(log, "a") as out:
    out.write(" ".join(os.environ.get(name, "-") for name in ("TMPDIR", "TMP", "TEMP")) + "\n")
tempfile.mkdtemp(prefix="leaked-")
open(os.path.join(os.environ["TMPDIR"], "leaked.txt"), "w").write("x")
space = {}
exec(open("calc.py").read(), space)
if space["add"](2, 3) != 5:
    if len(sys.argv) > 2:
        time.sleep(30)
    sys.exit(1)
'''


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root, self.parent, self.log = base / "repo", base / "tmp", base / "log"
        self.root.mkdir()
        self.parent.mkdir()
        self.probe = base / "probe.py"
        self.probe.write_text(PROBE)
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text("__pycache__/\n")
        (self.root / "calc.py").write_text(CALC)
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")
        scoped = mock.patch.object(tempfile, "tempdir", str(self.parent))  # the kit's own temp dirs land here
        scoped.start()
        self.addCleanup(scoped.stop)
        os.environ.pop(evidence.PLANT_WORKERS_ENV, None)

    def rows(self):
        return [{"id": "mul", "path": "calc.py", "find": "return a + b", "replace": "return a * b",
                 "test_args": [str(self.log)]},
                {"id": "sub", "path": "calc.py", "find": "return a + b", "replace": "return a - b",
                 "test_args": [str(self.log)], "serial": True}]

    def run_plants(self, workers, **kw):
        ev = evidence.Evidence(self.root, "T-1")
        with contextlib.redirect_stdout(io.StringIO()):
            return ev.planted_bugs(self.rows(), BUILD, [PY, str(self.probe)], workers=workers, **kw)

    def seen(self):
        return [line.split() for line in self.log.read_text().splitlines()]

    def assert_own_and_gone(self, minimum):
        seen = self.seen()
        self.assertGreaterEqual(len(seen), minimum)
        for tmpdir, tmp, temp in seen:
            self.assertEqual(tmpdir, tmp)
            self.assertEqual(tmpdir, temp)
            self.assertEqual(Path(tmpdir).parent, self.parent)
            self.assertTrue(Path(tmpdir).name.startswith(f"pk-plant-tmp-{os.getpid()}-"), tmpdir)
            self.assertFalse(Path(tmpdir).exists(), f"{tmpdir} was left behind")
        self.assertEqual(len({row[0] for row in seen}), len(seen), "a run shared another run's TMPDIR")
        self.assertEqual([p.name for p in self.parent.iterdir() if p.name.startswith("pk-plant-tmp-")], [])


class EachRunItsOwn(Base):
    def test_one_worker(self):
        rows = self.run_plants(1)
        self.assertEqual([row["kill_reason"] for row in rows], ["test failed", "test failed", "build failed"])
        self.assert_own_and_gone(3)  # a baseline, two rows (the self-test row does not build)

    def test_a_pool(self):
        rows = self.run_plants(2)
        self.assertEqual([row["kill_reason"] for row in rows], ["test failed", "test failed", "build failed"])
        self.assert_own_and_gone(3)

    def test_a_run_that_times_out_still_loses_its_folder(self):
        table = self.rows()[:1]
        table[0]["test_args"] = [str(self.log), "hang"]
        ev = evidence.Evidence(self.root, "T-1")
        rows = ev.planted_bugs(table, BUILD, [PY, str(self.probe)], workers=1, timeout=3)
        self.assertEqual(rows[0]["kill_reason"], "build failed")  # a timeout is never a kill
        self.assert_own_and_gone(2)

    def test_the_plant_judge_of_rejudge_and_gate_plants(self):
        row = dict(self.rows()[0], build_argv=BUILD, test_argv=[PY, str(self.probe), str(self.log)])
        with evidence.PlantJudge(self.root, "HEAD") as judge:
            self.assertTrue(judge.baseline([BUILD], [PY, str(self.probe), str(self.log)])["ok"])
            self.assertTrue(judge.attempt(row)["killed"])
        self.assert_own_and_gone(2)

    def test_a_folder_left_by_a_killed_run_is_cleared_by_the_next_run_and_a_live_ones_is_kept(self):
        dead = subprocess.Popen([PY, "-c", "pass"])
        dead.wait()
        live = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (live.kill(), live.wait()))
        gone = self.parent / f"pk-plant-tmp-{dead.pid}-abcd1234"
        kept = self.parent / f"pk-plant-tmp-{live.pid}-abcd1234"
        for folder in (gone, kept):
            (folder / "deep").mkdir(parents=True)
            (folder / "deep" / "file").write_text("x")
        self.run_plants(1)
        self.assertFalse(gone.exists())
        self.assertTrue(kept.exists())


if __name__ == "__main__":
    unittest.main()
