"""`pk sweep` (0.6.2, KF-60): this project's stale processes, listed, and with --kill stopped; D9 of `pk drift`."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import common
import lock
import pk
import sweep
from common import KitError, write_yaml
from test_worktree import commit_all, make_repo, run_cli, sh

PY = sys.executable
PK = Path(__file__).resolve().parent.parent / "scripts" / "pk.py"
ME = 50000  # the pid find() treats as itself in the synthetic tables
UID = 501
DAY = 86400


def proc(pid, ppid, command, age=2 * DAY, uid=UID, stat="S"):
    return sweep.Proc(pid, ppid, uid, stat, age, command)


def loop(path="", text="until [ -f done ]; do sleep 30; done"):
    return f"/bin/zsh -c -l source /home/u/.snap.sh && eval '{text}' {path}".strip()


class Config(unittest.TestCase):
    def test_absent_means_the_defaults(self):
        self.assertEqual(sweep.parse(None), {"max_age_hours": 24, "paths": [], "kill_grace_seconds": 5,
                                             "drift": "note"})

    def test_values_are_read_and_paths_expanded(self):
        cfg = sweep.parse({"max_age_hours": 0.5, "paths": ["/tmp/scratch/", "~/x"], "kill_grace_seconds": 2,
                           "drift": "fail"})
        self.assertEqual(cfg["max_age_hours"], 0.5)
        self.assertEqual(cfg["paths"], ["/tmp/scratch", os.path.expanduser("~/x")])
        self.assertEqual((cfg["kill_grace_seconds"], cfg["drift"]), (2, "fail"))

    def test_a_wrong_shape_is_refused_by_name(self):
        for value, words in (([], "processes must be a mapping"), ({"age": 1}, "unknown key age"),
                             ({"max_age_hours": -1}, "max_age_hours"), ({"max_age_hours": True}, "max_age_hours"),
                             ({"paths": "x"}, "processes.paths"), ({"paths": ["rel/dir"]}, "absolute"),
                             ({"paths": ["/"]}, "absolute"), ({"kill_grace_seconds": 601}, "kill_grace_seconds"),
                             ({"drift": "warn"}, "note or fail")):
            with self.assertRaisesRegex(KitError, words, msg=value):
                sweep.parse(value)

    def test_pk_doctor_validates_the_section(self):
        self.assertEqual(pk._checks_problems({"build": [["true"]], "test": [["true"]], "scan": [["true"]],
                                              "processes": {"max_age_hours": 12, "paths": ["/tmp/s"]}}), [])
        problems = pk._checks_problems({"build": [["true"]], "test": [["true"]], "scan": [["true"]],
                                        "processes": {"drift": "loud"}})
        self.assertEqual(problems, ["checks.yaml: processes.drift must be note or fail"])


class Parsing(unittest.TestCase):
    def test_etime(self):
        self.assertEqual(sweep.parse_etime("05:03"), 303)
        self.assertEqual(sweep.parse_etime("01:02:03"), 3723)
        self.assertEqual(sweep.parse_etime("10-01:00:00"), 10 * DAY + 3600)

    def test_ps_lines(self):
        rows = sweep.parse_ps("  12     1   501 Ss   2-00:00:01 /bin/sh -c while true; do sleep 1; done\n"
                              "garbage\n  13    12   501 S       00:01 sleep 1\n")
        self.assertEqual([(r.pid, r.ppid, r.uid, r.age, r.name) for r in rows],
                         [(12, 1, 501, 2 * DAY + 1, "sh"), (13, 12, 501, 1, "sleep")])
        self.assertEqual(rows[0].command, "/bin/sh -c while true; do sleep 1; done")


class Classify(unittest.TestCase):
    """The matching rules on a synthetic process table: what is listed, and everything that must never be."""

    def setUp(self):
        self.root = make_repo(self)
        self.other = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.other, ignore_errors=True))
        self.scratch = "/scratch/session-x"
        self.cfg = sweep.parse({"paths": [self.scratch]})
        self.asked = []

    def find(self, procs, where=None, cfg=None, now=None):
        where = where or {}

        def cwd_of(pids):
            self.asked.append(sorted(pids))
            return {pid: where[pid] for pid in pids if pid in where}

        table = [proc(ME, 400, "python3 pk.py sweep", age=1), proc(400, 300, loop(), age=9 * DAY),
                 proc(300, 1, "node agent-session"), *procs]
        found = sweep.find(self.root, cfg or self.cfg, procs=table, cwd_of=cwd_of, now=now or time.time(), me=ME,
                           uid=UID)
        return {item.proc.pid: item.why for item in found["stale"]}, found

    def test_an_old_loop_in_the_repository_and_its_sleep_are_listed(self):
        listed, _ = self.find([proc(10, 300, loop()), proc(11, 10, "sleep 30", age=5)], {10: str(self.root)})
        self.assertEqual(listed, {10: "wait loop", 11: "sleep of 10"})

    def test_a_loop_naming_a_configured_path_or_a_worktree_is_listed_without_reading_its_cwd(self):
        wt = self.root.parent / (self.root.name + "-wt")
        sh(self.root, "git", "worktree", "add", "-q", "-b", "task/x", str(wt))
        self.addCleanup(lambda: __import__("shutil").rmtree(wt, ignore_errors=True))
        listed, _ = self.find([proc(10, 300, loop(f"> {self.scratch}/out.txt")),
                               proc(12, 300, loop(text=f"while ! test -f {wt}/x; do sleep 5; done"))])
        self.assertEqual(listed, {10: "wait loop", 12: "wait loop"})
        self.assertEqual(self.asked, [])  # matched by the command line: no working directory read

    def test_an_orphaned_tail_on_the_scratch_path_is_listed_and_one_under_a_terminal_is_not(self):
        listed, _ = self.find([proc(20, 1, f"tail -f {self.scratch}/task.output"),
                               proc(21, 22, f"tail -f {self.scratch}/other.output"), proc(22, 1, "-zsh")])
        self.assertEqual(listed, {20: "orphaned tail"})

    def test_nothing_else_is_ever_listed(self):
        repo = str(self.root)
        procs = [
            proc(30, 300, loop(), age=3600),                                    # young
            proc(31, 300, loop()),                                              # another project (cwd elsewhere)
            proc(32, 1, "-zsh"), proc(33, 1, "/bin/zsh -il"),                   # interactive shells in the repo
            proc(34, 1, "/Applications/Code.app/Contents/MacOS/Code Helper --type=renderer"),   # an editor
            proc(35, 1, f"/Applications/Browser.app/Contents/MacOS/Browser --user-data-dir={repo}/.profile"),
            proc(36, 300, "/bin/bash -c pnpm dev"),                             # a server, no loop
            proc(37, 300, loop(), uid=UID + 1),                                 # another user's
            proc(38, 300, loop(text=f"sleep 9 > {repo}-other/log")),            # a neighbour sharing a name prefix
            proc(39, 1, f"sleep 600 {repo}"),                                   # a bare sleep: only as a loop's child
            proc(40, 300, loop(), stat="Z"),                                    # a zombie
        ]
        where = {pid: repo for pid in (30, 32, 33, 34, 35, 36, 37, 39, 40)}
        where[31] = str(self.other)
        listed, _ = self.find(procs, where)
        self.assertEqual(listed, {})

    def test_this_command_and_its_ancestors_are_never_listed(self):
        listed, _ = self.find([], {400: str(self.root)})  # 400 is an old loop in the repository, and our parent
        self.assertEqual(listed, {})

    def test_a_lock_holder_its_ancestors_and_descendants_are_never_listed(self):
        holder_lock = self.root / ".git" / "pk-heavy.lock"
        holder_lock.mkdir()
        (holder_lock / "owner").write_text("owner=gate\npid=51\nstarted=2026-11-20T00:00:00Z\n")
        procs = [proc(50, 300, loop()), proc(51, 50, "python3 pk.py lock run gate -- make test"),
                 proc(52, 51, loop(text="while true; do sleep 1; done")), proc(53, 52, "sleep 1", age=1),
                 proc(54, 300, loop())]
        listed, _ = self.find(procs, {pid: str(self.root) for pid in (50, 52, 54)})
        self.assertEqual(listed, {54: "wait loop"})

    def test_a_registered_child_whose_pk_is_gone_is_listed_with_its_descendants_at_any_age(self):
        now = time.time()
        folder = self.root / ".git" / "pk-procs"
        folder.mkdir()
        entry = {"pid": 60, "started": now - 30, "owner": "gate step test", "command": "make test", "pk_pid": 69}
        (folder / "60.json").write_text(json.dumps(entry))
        active = dict(entry, pid=62, pk_pid=63, owner="lock run")
        (folder / "62.json").write_text(json.dumps(active))
        reused = dict(entry, pid=64, started=now - 5 * DAY)  # pid 64 now belongs to a newer process
        (folder / "64.json").write_text(json.dumps(reused))
        (folder / "65.json").write_text(json.dumps(dict(entry, pid=65)))  # ended
        procs = [proc(60, 1, "make test", age=30), proc(61, 60, "node test-runner", age=29),
                 proc(62, 63, "make e2e", age=30), proc(63, 300, "python3 pk.py lock run x -- make e2e", age=31),
                 proc(64, 300, "vim notes", age=10)]
        listed, found = self.find(procs, now=now)
        self.assertEqual(listed, {60: "pk run ended: gate step test (pk pid 69 gone)", 61: "under 60"})
        self.assertEqual(found["active"], 1)
        self.assertEqual(sorted(path.name for path in found["dead"]), ["64.json", "65.json"])


class Registry(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        self.folder = self.root / ".git" / "pk-procs"

    def test_tracked_writes_an_entry_while_the_body_runs_and_removes_it(self):
        with sweep.tracked(self.root, 4242, "owner-x", ["make", "test it"]) as path:
            data = json.loads(path.read_text())
            self.assertEqual((data["pid"], data["owner"], data["command"], data["pk_pid"]),
                             (4242, "owner-x", "make 'test it'", os.getpid()))
            self.assertAlmostEqual(data["started"], time.time(), delta=5)
        self.assertFalse(self.folder.exists())

    def test_outside_a_repository_nothing_is_written_and_nothing_fails(self):
        with tempfile.TemporaryDirectory() as tmp, sweep.tracked(tmp, 4242, "o", ["x"]) as path:
            self.assertIsNone(path)

    def test_pk_lock_run_registers_its_command_while_it_runs(self):
        seen = self.root.parent / (self.root.name + "-seen.json")
        self.addCleanup(lambda: seen.unlink(missing_ok=True))
        code = f"import glob, shutil; shutil.copy(glob.glob({str(self.folder)!r} + '/*.json')[0], {str(seen)!r})"
        env = {**os.environ, "PK_LOCK_POLL": "0.05"}
        done = subprocess.run([PY, str(PK), "lock", "run", "owner-y", "--", PY, "-c", code], cwd=str(self.root),
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(seen.read_text())
        self.assertEqual(data["owner"], "lock run owner-y")
        self.assertIn("-c", data["command"])
        self.assertFalse(self.folder.exists())  # removed when the command ended

    def test_a_tracked_run_argv_registers_and_removes_its_child(self):
        out = self.root.parent / (self.root.name + "-entries")
        self.addCleanup(lambda: out.unlink(missing_ok=True))
        code = f"import os; open({str(out)!r}, 'w').write(' '.join(os.listdir({str(self.folder)!r})))"
        status, _ = common.run_argv([PY, "-c", code], self.root, track="gate step test")
        self.assertEqual(status, 0)
        self.assertRegex(out.read_text(), r"^\d+\.json$")
        self.assertFalse(self.folder.exists())
        self.assertEqual(common.run_argv([PY, "-c", "import time; time.sleep(30)"], self.root, timeout=1,
                                         track="slow")[0], 124)
        self.assertFalse(self.folder.exists())

    def test_a_gate_step_runs_tracked(self):
        import gate
        seen = []
        real = sweep.record_child
        with mock.patch.object(sweep, "record_child", side_effect=lambda *a, **k: seen.append(a[2]) or real(*a, **k)):
            passed, _ = gate._step_lines("test", [([PY, "-c", "pass"], 0)], self.root)
        self.assertTrue(passed)
        self.assertEqual(seen, ["gate step test"])
        with mock.patch.object(sweep, "record_child") as untracked:
            common.run_argv([PY, "-c", "pass"], self.root)
        untracked.assert_not_called()  # outside a tracking block, run_argv registers nothing


class Stop(unittest.TestCase):
    def test_a_pid_that_ended_or_was_reused_is_not_signalled_and_a_survivor_gets_kill(self):
        now = 1_000_000.0
        items = [sweep.Item(proc(70, 1, "a"), "x", now - 100), sweep.Item(proc(71, 1, "b"), "x", now - 100),
                 sweep.Item(proc(72, 1, "c"), "x", now - 100)]
        table = [proc(70, 1, "a", age=100), proc(72, 1, "c", age=5)]  # 71 ended; 72 is a newer process now
        sent = []
        with mock.patch.object(sweep, "find", return_value={"stale": items}):
            result = sweep.stop(Path("."), sweep.parse({"kill_grace_seconds": 0}), items, read=lambda: table,
                                send=lambda pid, sig: sent.append((pid, sig)), clock=lambda: now,
                                pause=lambda s: None)
        self.assertEqual(sent, [(70, signal.SIGTERM), (70, signal.SIGKILL)])
        self.assertEqual(result, (1, 1, 2))


class RealProcesses(unittest.TestCase):
    """With real shells: a loop in the project is listed and stopped; others are left running."""

    def setUp(self):
        self.root = make_repo(self)
        (self.root / "program").mkdir()
        write_yaml(self.root / "program" / "checks.yaml",
                   {"build": [["true"]], "test": [["true"]], "scan": [["true"]],
                    "processes": {"max_age_hours": 0, "kill_grace_seconds": 2}})
        commit_all(self.root, "program")
        self.elsewhere = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.elsewhere, ignore_errors=True))

    def spawn(self, argv, cwd):
        child = subprocess.Popen(argv, cwd=str(cwd), start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

        def stop():
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()
        self.addCleanup(stop)
        return child

    def sweep(self, *argv):
        return subprocess.run([PY, str(PK), "sweep", *argv], cwd=str(self.root), capture_output=True, text=True,
                              timeout=120)

    def test_report_lists_the_projects_loop_only_and_kill_stops_only_it(self):
        mine = self.spawn(["sh", "-c", "while true; do sleep 1; done"], self.root)
        theirs = self.spawn(["sh", "-c", "while true; do sleep 1; done"], self.elsewhere)
        held = self.spawn([PY, str(PK), "lock", "run", "holder", "--", "sh", "-c", "while true; do sleep 1; done"],
                          self.root)
        deadline = time.monotonic() + 20
        while not (self.root / ".git" / "pk-heavy.lock" / "owner").is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(1.2)
        report = self.sweep()
        self.assertEqual(report.returncode, 0, report.stderr)
        listed = [int(line.split()[0]) for line in report.stdout.splitlines()[1:] if line[:9].strip().isdigit()]
        self.assertIn(mine.pid, listed, report.stdout)
        self.assertNotIn(theirs.pid, listed)
        self.assertNotIn(held.pid, listed)
        self.assertIn("report only: pk sweep --kill", report.stdout)
        self.assertIsNone(mine.poll())  # a report signals nothing
        killed = self.sweep("--kill")
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.assertIn("stopped:", killed.stdout)
        mine.wait(timeout=20)
        self.assertIsNone(theirs.poll())
        self.assertIsNone(held.poll())
        self.assertTrue((self.root / ".git" / "pk-heavy.lock").is_dir())


class DriftD9(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        (self.root / "program").mkdir()

    def checks(self, processes=None):
        data = {"build": [["true"]], "test": [["true"]], "scan": [["true"]]}
        if processes is not None:
            data["processes"] = processes
        write_yaml(self.root / "program" / "checks.yaml", data)

    def items(self, n):
        return {"stale": [sweep.Item(proc(80 + i, 1, "x"), "wait loop") for i in range(n)], "active": 0,
                "dead": [], "prefixes": []}

    def test_without_fail_it_is_a_note_that_never_drifts(self):
        self.checks()
        with mock.patch.object(sweep, "find", return_value=self.items(3)):
            self.assertEqual(sweep.drift_check(self.root)[:2], ("note", False))
            import drift
            self.assertNotIn("D9", [cid for cid, _, _ in drift.report(self.root)[1]])

    def test_with_fail_it_is_a_check_that_drifts(self):
        self.checks({"drift": "fail"})
        import drift
        with mock.patch.object(sweep, "find", return_value=self.items(2)):
            rows = {cid: (ok, detail) for cid, ok, detail in drift.report(self.root)[1]}
        self.assertFalse(rows["D9"][0])
        self.assertIn("2 stale process(es)", rows["D9"][1])
        with mock.patch.object(sweep, "find", return_value=self.items(0)):
            rows = {cid: (ok, detail) for cid, ok, detail in drift.report(self.root)[1]}
        self.assertTrue(rows["D9"][0])

    def test_the_note_is_printed_after_the_checks(self):
        self.checks()
        commit_all(self.root, "program")
        with mock.patch.object(sweep, "find", return_value=self.items(1)):
            code, out, _ = run_cli("drift", self.root, "drift")
        self.assertEqual(code, 0)
        self.assertIn("  D9 note  1 stale process(es) of this project", out)
        self.assertRegex(out.splitlines()[-1], r"^  \d of 7 checks drift$")


if __name__ == "__main__":
    unittest.main()
