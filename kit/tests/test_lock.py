"""`pk lock` (0.6.0, KF-16 of the program it came from): the machine lock for heavy runs, with real processes."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import lock
from common import KitError

PK = Path(__file__).resolve().parent.parent / "scripts" / "pk.py"
PY = sys.executable


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


class LockBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "repo"
        self.root.mkdir()
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        (self.root / "a").write_text("a\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "init")
        self.env = {**os.environ, "PK_LOCK_POLL": "0.05"}
        self.env.pop("PK_LOCK_NAME", None)
        self.lock = self.root / ".git" / "pk-heavy.lock"

    def pk(self, *argv, cwd=None, **kwargs):
        return subprocess.run([PY, str(PK), "lock", *argv], cwd=str(cwd or self.root), env=self.env,
                              capture_output=True, text=True, timeout=60, **kwargs)

    def start(self, *argv, cwd=None):
        return subprocess.Popen([PY, str(PK), "lock", *argv], cwd=str(cwd or self.root), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for(self, condition, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("timed out waiting")

    @staticmethod
    def stop(child):
        child.kill()
        child.wait()

    def dead_pid(self):
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait()
        return child.pid

    def plant(self, pid=None, owner="theirs", age=0):
        """A lock directory as another holder (or the program's own shell wrapper) leaves it."""
        self.lock.mkdir()
        if pid is not None:
            (self.lock / "owner").write_text(f"owner={owner}\npid={pid}\nstarted=2026-11-09T00:00:00Z\n")
        if age:
            past = time.time() - age
            os.utime(self.lock, (past, past))


class Run(LockBase):
    def test_run_holds_the_lock_with_an_owner_file_for_the_command_alone_and_returns_its_status(self):
        out = self.base / "seen"
        result = self.pk("run", "gate-T1", "--", PY, "-c",
                         f"import shutil, sys; shutil.copy({str(self.lock / 'owner')!r}, {str(out)!r}); sys.exit(3)")
        self.assertEqual(result.returncode, 3, result.stderr)
        lines = out.read_text().splitlines()
        self.assertEqual([line.split("=", 1)[0] for line in lines], ["owner", "pid", "started"])
        self.assertEqual(lines[0], "owner=gate-T1")
        self.assertRegex(lines[2], r"started=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assertFalse(self.lock.exists())  # released
        self.assertEqual(self.pk("status").stdout, "free\n")

    def test_a_second_run_waits_for_a_live_holder_saying_so_once_then_runs(self):
        log = self.base / "log"
        first = self.start("run", "first", "--", PY, "-c",
                           f"import time; open({str(log)!r}, 'a').write('first start\\n'); time.sleep(1.0); "
                           f"open({str(log)!r}, 'a').write('first end\\n')")
        self.wait_for(lambda: log.exists())
        status = self.pk("status").stdout.splitlines()
        self.assertEqual(status[:2], ["held:", "owner=first"])
        second = self.pk("run", "second", "--", PY, "-c", f"open({str(log)!r}, 'a').write('second\\n')")
        first.communicate()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(log.read_text().splitlines(), ["first start", "first end", "second"])
        self.assertEqual(second.stderr.count("pk lock: waiting for: owner=first pid="), 1, second.stderr)

    def test_a_worktree_shares_the_lock_of_its_clone(self):
        sh(self.root, "git", "worktree", "add", "-q", "-b", "task/x", str(self.base / "wt"))
        holder = self.start("run", "from-the-worktree", "--", PY, "-c", "import time; time.sleep(30)",
                            cwd=self.base / "wt")
        self.wait_for(lambda: (self.lock / "owner").is_file())
        self.assertIn("owner=from-the-worktree", self.pk("status").stdout)
        holder.send_signal(signal.SIGTERM)
        holder.communicate()

    def test_the_name_comes_from_the_flag_or_the_environment_and_shares_a_programs_own_lock(self):
        theirs = self.root / ".git" / "their-heavy.lock"
        theirs.mkdir()
        (theirs / "owner").write_text(f"owner=their-script\npid={os.getpid()}\nstarted=2026-11-09T00:00:00Z\n")
        self.assertEqual(self.pk("status", "--name", "their-heavy").stdout.splitlines()[:2],
                         ["held:", "owner=their-script"])
        self.env["PK_LOCK_NAME"] = "their-heavy"
        self.assertIn("owner=their-script", self.pk("status").stdout)
        self.assertEqual(self.pk("status", "--name", "pk-heavy").stdout, "free\n")
        result = self.pk("run", "o", "--name", "other", "--", PY, "-c", "pass")
        self.assertEqual(result.returncode, 0, result.stderr)  # another name: no wait

    def test_a_missing_command_exits_127_and_releases_the_lock(self):
        result = self.pk("run", "o", "--", str(self.base / "no-such-program"))
        self.assertEqual(result.returncode, 127)
        self.assertIn("pk lock: cannot run", result.stderr)
        self.assertFalse(self.lock.exists())


class Stale(LockBase):
    def test_a_lock_whose_pid_is_gone_is_cleared_and_the_run_goes_on(self):
        self.plant(pid=self.dead_pid(), owner="crashed")
        self.assertIn("(holder PID not running: stale, cleared by the next run)", self.pk("status").stdout)
        result = self.pk("run", "o", "--", PY, "-c", "pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pk lock: cleared stale lock: owner=crashed", result.stderr)
        self.assertFalse(self.lock.exists())
        self.assertEqual(list(self.lock.parent.glob("pk-heavy.lock*")), [])

    def test_a_lock_without_an_owner_file_is_stale_only_after_a_minute(self):
        self.plant(age=120)
        self.assertTrue(lock.stale(self.lock))
        self.lock.rmdir()
        self.plant()
        self.assertFalse(lock.stale(self.lock))  # a holder that is still writing its owner file

    def test_a_live_holder_is_never_removed_and_a_signal_ends_the_wait(self):
        holder = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.stop, holder)
        self.plant(pid=holder.pid, owner="live")
        waiter = self.start("run", "o", "--", PY, "-c", "pass")
        time.sleep(1.0)
        self.assertIsNone(waiter.poll())  # still waiting
        waiter.send_signal(signal.SIGTERM)
        _, err = waiter.communicate(timeout=30)
        self.assertEqual(waiter.returncode, 128 + signal.SIGTERM, err)
        self.assertIn("waiting for: owner=live", err)
        self.assertIn(f"pid={holder.pid}", (self.lock / "owner").read_text())  # untouched

    def test_a_lock_moved_aside_that_turns_out_live_is_put_back(self):
        holder = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.stop, holder)
        self.plant(pid=holder.pid, owner="live")
        lock._clear_stale(self.lock)  # as a peer would after judging it stale a moment too early
        self.assertIn("owner=live", (self.lock / "owner").read_text())


class Signals(LockBase):
    def test_a_signal_is_passed_to_the_command_and_the_lock_released(self):
        holder = self.start("run", "o", "--", PY, "-c", "import time; time.sleep(60)")
        self.wait_for(lambda: (self.lock / "owner").is_file())
        time.sleep(0.3)  # the command has started
        holder.send_signal(signal.SIGTERM)
        holder.communicate(timeout=30)
        self.assertEqual(holder.returncode, 128 + signal.SIGTERM)
        self.assertFalse(self.lock.exists())


class Refusals(LockBase):
    def test_a_bad_name_owner_or_command_is_refused(self):
        for argv, words in ((["status", "--name", "../x"], "lock name"), (["status", "--name", "x.lock"], "lock name"),
                            (["run", "o", "--"], "no command given"), (["run", "o", "--name"], "--name needs a value"),
                            (["run", "", "--", "true"], "owner must be a non-empty name")):
            result = self.pk(*argv)
            self.assertEqual(result.returncode, 1, argv)
            self.assertIn(words, result.stderr, argv)
        self.assertFalse(self.lock.exists())

    def test_outside_a_repository_the_lock_is_refused(self):
        with self.assertRaisesRegex(KitError, "not inside a git repository"):
            lock.lock_path(self.base)


if __name__ == "__main__":
    unittest.main()
