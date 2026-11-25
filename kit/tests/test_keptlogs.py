"""Kept logs of failed gate steps (0.7.6, KF-77): a failing step's full output stays on the machine that ran it, under
`<git common dir>/pk-logs/<ID>/`, with a digest of its failure lines, until the task is done or the logs are older
than keptlogs.KEEP_DAYS days. Before 0.7.6 only the printed tail of TAIL_LINES lines was left."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import gate
import keptlogs
import status

PY = sys.executable
TASK = "P0-M01-S01-T01"
LONG = ("import sys\nfor i in range(80): print('ok %d' % i)\nprint('\\x1b[31mFAIL\\x1b[0m test_realtime (live.test)')\n"
        "print('AssertionError: 1 != 2')\nfor i in range(40): print('after %d' % i)\nsys.exit(1)\n")


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        (self.root / "a.txt").write_text("a\n")
        self.commit()
        self.common = self.root / ".git"
        self.kept = self.common / keptlogs.DIR

    def commit(self):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "change")


class KeepTests(Base):
    def test_a_failing_step_longer_than_the_tail_keeps_its_full_output_at_the_printed_path(self):
        with gate.keeping(self.root, TASK):
            passed, lines = gate._step_lines("test", [([PY, "-c", LONG], 0)], self.root)
        self.assertFalse(passed)
        self.assertEqual(len(lines), 1 + gate.TAIL_LINES + 1)  # FAILED, the tail as before, then the kept-log line
        self.assertEqual(lines[1:-1], [gate.TAIL_PREFIX + f"after {i}" for i in range(20, 40)])
        said = lines[-1]
        self.assertRegex(said, r"\Akept log: \S+/pk-logs/P0-M01-S01-T01/[0-9]{8}T[0-9]{6}Z-test\.log \(the full output, "
                               r"122 line\(s\); 2 failure line\(s\) in \S+-test\.digest\)\Z")
        log = Path(said.split()[2])
        self.assertEqual(log.parent, self.kept / TASK)
        full = log.read_text()
        self.assertEqual(full.splitlines()[0], "ok 0")  # far before the tail
        self.assertEqual(len(full.splitlines()), 122)
        digest = log.with_suffix(".digest").read_text().splitlines()
        self.assertEqual(digest[0], f"# kept log: {log}")
        self.assertRegex(digest[1], r"\A# step test: .* \(exit 1\)\Z")
        self.assertEqual(digest[2:], ["81: FAIL test_realtime (live.test)", "82: AssertionError: 1 != 2"])

    def test_a_passing_step_and_a_step_outside_a_gate_keep_nothing(self):
        with gate.keeping(self.root, TASK):
            passed, lines = gate._step_lines("test", [([PY, "-c", "print('fine')"], 0)], self.root)
        self.assertEqual((passed, lines), (True, ["step test: ok"]))
        passed, lines = gate._step_lines("test", [([PY, "-c", LONG], 0)], self.root)  # no gate run: as before 0.7.6
        self.assertFalse(passed)
        self.assertEqual(len(lines), 1 + gate.TAIL_LINES)
        self.assertFalse(self.kept.exists())

    def test_a_second_failure_in_the_same_second_gets_its_own_file(self):
        patch = mock.patch.dict(os.environ, {keptlogs.STAMP_ENV: "20261125T101010Z"})
        patch.start()
        self.addCleanup(patch.stop)
        first = keptlogs.keep(self.common, TASK, "ext:mod.check", "h", "one\n")[0]
        second = keptlogs.keep(self.common, TASK, "ext:mod.check", "h", "two\n")[0]
        self.assertEqual((first.name, second.name), ("20261125T101010Z-ext-mod.check.log",
                                                     "20261125T101010Z-ext-mod.check-2.log"))
        self.assertEqual(second.read_text(), "two\n")

    def test_the_digest_holds_every_marker_and_is_capped(self):
        text = "\n".join(["plain", "--- FAIL: TestX (0.01s)", "panic: runtime error", "✗ broke", "× also",
                          "TypeError: x", "fine"])
        self.assertEqual(keptlogs.digest(text), ["2: --- FAIL: TestX (0.01s)", "3: panic: runtime error",
                                                 "4: ✗ broke", "5: × also", "6: TypeError: x"])
        self.assertEqual(len(keptlogs.digest("FAIL\n" * 500)), keptlogs.DIGEST_LINES)


class PruneTests(Base):
    def put_logs(self, task, age_days=0.0):
        folder = self.kept / task
        folder.mkdir(parents=True)
        log = folder / "20261125T101010Z-test.log"
        log.write_text("x\n")
        then = time.time() - age_days * 86400
        os.utime(log, (then, then))
        return folder

    def test_clean_up_removes_a_done_tasks_logs_and_old_ones_and_keeps_an_unmerged_recent_one(self):
        (self.root / "program" / "status").mkdir(parents=True)
        (self.root / "program" / "status" / "DONE-1.json").write_text(json.dumps({"status": "done"}))
        (self.root / "program" / "status" / "MERGED-1.json").write_text(json.dumps({"status": "merged"}))
        (self.root / "program" / "status" / f"{TASK}.json").write_text(json.dumps({"status": "review"}))
        self.commit()
        for task in ("DONE-1", "MERGED-1", TASK):
            self.put_logs(task)
        self.put_logs("OLD-1", age_days=keptlogs.KEEP_DAYS + 1)
        self.put_logs("NEWISH-1", age_days=keptlogs.KEEP_DAYS - 1)
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not ours\n")
        (self.kept / "LINK-1").symlink_to(outside)
        gone = keptlogs.prune(self.common, self.root, "refs/heads/main")
        self.assertEqual(gone, [("DONE-1", "done"), ("MERGED-1", "done"),
                                ("OLD-1", f"older than {keptlogs.KEEP_DAYS} days")])
        self.assertEqual(sorted(os.listdir(self.kept)), ["LINK-1", "NEWISH-1", TASK])
        self.assertEqual(os.listdir(outside), ["keep.txt"])
        self.assertEqual(set(status.TERMINAL), {"done", "merged", "adopted"})  # PRUNE's own copy of them

    def test_the_lane_runs_the_same_clean_up_with_python_alone(self):
        self.put_logs("OLD-1", age_days=keptlogs.KEEP_DAYS + 1)
        self.put_logs(TASK)
        out = subprocess.run([PY, "-c", keptlogs.PRUNE, str(self.common), str(keptlogs.KEEP_DAYS), ".", "HEAD"],
                             cwd=str(self.root), capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, f"remote: removed the kept logs of OLD-1 (older than {keptlogs.KEEP_DAYS} days)\n")
        self.assertEqual(os.listdir(self.kept), [TASK])

    def test_remove_takes_one_tasks_logs(self):
        self.put_logs(TASK)
        self.put_logs("OTHER-1")
        self.assertEqual(keptlogs.remove(self.common, TASK), 1)
        self.assertEqual(keptlogs.remove(self.common, TASK), 0)
        self.assertEqual(os.listdir(self.kept), ["OTHER-1"])


if __name__ == "__main__":
    unittest.main()
