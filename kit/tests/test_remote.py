"""`pk remote` (0.6.0): heavy steps on other machines over ssh, git and tar, opt-in by the checks.yaml `remote`
section. A fake ssh (an executable that drops the options and the host and runs the remote command here with sh -c)
stands in for the machine; a local directory holds the bare mirror and the lanes."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import gate
import remote
from common import KitError, write_yaml
from test_gate import checks_data

KIT = Path(__file__).resolve().parent.parent
PK = KIT / "scripts" / "pk.py"
PY = sys.executable
TASK = "P0-M01-S01-T01"
BRANCH = f"task/{TASK}"
# The fake ssh: options and the host go, the rest runs here, with python3 meaning this interpreter and git's own
# programs (git-receive-pack, for the push) on PATH. Each call is logged.
FAKE_SSH = '''#!/bin/sh
echo "$*" >> "$FAKE_SSH_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p|-i|-l|-F) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
PATH="$FAKE_BIN:$(git --exec-path):$PATH"
export PATH
exec sh -c "$*"
'''


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def commit(cwd, message):
    sh(cwd, "git", "add", "-A")
    sh(cwd, "git", "commit", "-q", "-m", message)


def section(base, **over):
    data = {"dir": str(base), "lanes": ["lane1", "lane2"],
            "setup": [[PY, "-c", "open('setup-ran', 'w').write('yes')"]],
            "env": {"LANE_FLAG": "on"}, "wrap": ["env", "WRAPPED=yes"], "lane_wrap": {"lane1": ["env", "PINNED=1"]},
            "workers": 2}
    data.update(over)
    return data


class Parse(unittest.TestCase):
    def test_an_absent_section_is_none_and_a_full_one_is_checked(self):
        self.assertIsNone(remote.parse(None))
        parsed = remote.parse({"dir": "/srv/x/", "lanes": ["lane1"], "setup": [["make", "deps"]],
                               "env": {"PK_LOCK_NAME": "their-heavy"}, "wrap": ["nice", "-n", "5"],
                               "lane_wrap": {"lane1": ["taskset", "-c", "0-5"]}, "workers": 3})
        self.assertEqual(parsed, {"host_env": "PK_REMOTE_HOST", "dir": "/srv/x/", "dir_env": None, "lanes": ["lane1"],
                                  "setup": [["make", "deps"]], "env": {"PK_LOCK_NAME": "their-heavy"},
                                  "wrap": ["nice", "-n", "5"], "lane_wrap": {"lane1": ["taskset", "-c", "0-5"]},
                                  "workers": 3})
        self.assertEqual(remote.parse({"dir_env": "LANES_DIR", "lanes": ["a"], "host_env": "MY_HOST"})["host_env"],
                         "MY_HOST")

    def test_every_bad_value_is_refused_by_name(self):
        good = {"dir": "lanes", "lanes": ["lane1"]}
        for over, words in (({"host": "x"}, "unknown key host"), ({"host_env": "a-b"}, "host_env must name"),
                            ({"dir": None}, "exactly one of dir"), ({"dir_env": "X"}, "exactly one of dir"),
                            ({"dir": "~/lanes"}, "remote.dir must be"), ({"dir": "a/../b"}, "remote.dir must be"),
                            ({"dir": "h:x"}, "remote.dir must be"), ({"lanes": []}, "remote.lanes must list"),
                            ({"lanes": ["auto"]}, "remote.lanes must list"), ({"lanes": ["a b"]}, "remote.lanes"),
                            ({"lanes": ["a", "a"]}, "names a lane twice"), ({"setup": ["make deps"]}, "remote.setup"),
                            ({"setup": [[]]}, "remote.setup"), ({"env": {"A": 1}}, "remote.env"),
                            ({"env": {"PK_LOCK_NAME": "../x"}}, "lock name"), ({"wrap": "nice"}, "remote.wrap"),
                            ({"lane_wrap": {"lane9": ["x"]}}, "lane9, not in remote.lanes"),
                            ({"workers": 0}, "remote.workers"), ({"workers": True}, "remote.workers"), ([], "mapping")):
            value = over if isinstance(over, list) else {**good, **over}
            if isinstance(value, dict):
                value = {key: item for key, item in value.items() if item is not None}
            with self.assertRaises(KitError, msg=over) as ctx:
                remote.parse(value)
            self.assertIn(words, str(ctx.exception), over)

    def test_the_checks_loader_and_doctor_see_the_section(self):
        with self.assertRaisesRegex(KitError, "remote.lanes"):
            gate._parse_checks({"remote": {"dir": "x", "lanes": []}}, ())
        self.assertEqual(gate._parse_checks({"remote": {"dir": "x", "lanes": ["l"]}}, ())["remote"]["lanes"], ["l"])


class RemoteBase(unittest.TestCase):
    """A program repository with a task branch and worktree, a `remote` section on main, and a fake remote machine:
    `<remote>/repo.git` (bare) and two lanes cloned from it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root, self.machine, self.bin = self.base / "repo", self.base / "machine", self.base / "bin"
        for path in (self.root, self.machine, self.bin):
            path.mkdir()
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text("__pycache__/\n.worktrees/\n")
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.root / "gone.txt").write_text("to be deleted\n")
        shutil.copytree(KIT / "scripts", self.root / "kit" / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "kit" / "VERSION").write_text((KIT / "VERSION").read_text())
        write_yaml(self.root / "program" / "checks.yaml", checks_data(remote=section(self.machine)))
        commit(self.root, "init")
        self.wt = self.root / ".worktrees" / TASK.lower()
        sh(self.root, "git", "worktree", "add", "-q", "-b", BRANCH, str(self.wt))
        (self.wt / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        commit(self.wt, "task work")
        # --no-local: no hard links into a repository whose background gc may repack its objects meanwhile
        sh(self.base, "git", "clone", "-q", "--no-local", "--bare", str(self.root), str(self.machine / "repo.git"))
        for lane in ("lane1", "lane2"):
            sh(self.base, "git", "clone", "-q", "--no-local", str(self.machine / "repo.git"), str(self.machine / lane))
        ssh = self.bin / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(0o755)
        (self.bin / "python3").symlink_to(PY)
        self.log = self.base / "ssh.log"
        self.env = {**os.environ, "PK_REMOTE_HOST": "lanes-host", "PK_REMOTE_SSH": str(ssh), "FAKE_SSH_LOG": str(self.log),
                    "FAKE_BIN": str(self.bin), "PK_REMOTE_POLL": "0.1", "PK_LOCK_POLL": "0.1"}
        for var in ("GIT_SSH_COMMAND", "PK_LOCK_NAME", "PK_PLANT_WORKERS"):
            self.env.pop(var, None)

    def pk(self, *argv, cwd=None, env=None):
        return subprocess.run([PY, str(PK), "remote", *argv], cwd=str(cwd or self.root), env=env or self.env,
                              capture_output=True, text=True, timeout=120)

    def ssh_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class DryRun(RemoteBase):
    def test_a_gate_dry_run_prints_the_push_to_the_mirror_and_the_lane_plan_and_runs_nothing(self):
        result = self.pk("gate", TASK, "--dry-run", "--", "--no-reuse", "$(touch pwned)")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn(f"remote: gate {TASK} on lanes-host, lane auto (the first free of lane1, lane2; waits while all "
                      "are busy)", out)
        self.assertIn(f"remote: push (to the lanes' mirror only, never to origin): git push -q -f "
                      f"lanes-host:{self.machine}/repo.git refs/heads/main:refs/heads/main "
                      f"refs/heads/{BRANCH}:refs/heads/{BRANCH}", out)
        self.assertIn(f"remote: on the lane, under its pk lock 'pk-heavy': env PINNED=1 env WRAPPED=yes python3 "
                      f"kit/scripts/pk.py gate handback {TASK} --branch {BRANCH} --no-reuse '$(touch pwned)'", out)
        self.assertIn("remote: dry run: nothing pushed, nothing run", out)
        self.assertEqual(self.ssh_calls(), [])
        self.assertFalse((self.root / "pwned").exists())

    def test_a_plants_dry_run_names_the_rows_and_a_chosen_lane(self):
        result = self.pk("plants", TASK, "--rows", "g1,g2", "--lane", "lane2", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"remote: plants {TASK} on lanes-host, lane lane2", result.stdout)
        self.assertIn(f"env WRAPPED=yes python3 kit/scripts/pk.py gate plants {TASK} --branch {BRANCH} --rows g1,g2",
                      result.stdout)
        self.assertNotIn("PINNED", result.stdout.split("ssh: ")[0])  # lane1's prefix is lane1's alone
        self.assertEqual(self.ssh_calls(), [])

    def test_a_run_dry_run_lists_what_it_ships(self):
        (self.wt / "calc.py").write_text("changed\n")
        (self.wt / "new.txt").write_text("new\n")
        (self.wt / "gone.txt").unlink()
        result = self.pk("run", "--dry-run", "--lane", "lane1", "suite", "--", PY, "-c", "print(1)", cwd=self.wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("remote: ships 2 changed or untracked file(s): calc.py, new.txt; deletes 1: gone.txt",
                      result.stdout)
        self.assertIn(f"refs/runs/run-{TASK.lower()}", result.stdout)
        self.assertEqual(self.ssh_calls(), [])

    def test_refusals_name_the_fix(self):
        bare = {key: value for key, value in self.env.items() if key != "PK_REMOTE_HOST"}
        for argv, env, words in ((["gate", TASK, "--dry-run"], bare, "PK_REMOTE_HOST is not set"),
                                 (["gate", TASK, "--lane", "lane9", "--dry-run"], None, "no lane lane9"),
                                 (["gate", "bad id", "--dry-run"], None, "not a usable task id"),
                                 (["run", "a b", "--", "true"], None, "owner 'a b'"),
                                 (["run", "o", "--"], None, "no command given"),
                                 (["run", "--get", "../x", "o", "--", "true"], None, "--get '../x'"),
                                 (["run", "o", "--", "cat", str(Path.home() / "x")], None, "names a path on this machine"),
                                 (["plants", TASK, "--dry-run"], None, "pass --rows"),
                                 (["plants", TASK, "--rows", "a", "--chunk-seconds", "9", "--dry-run"], None,
                                  "--chunk-seconds splits --all-pending")):
            result = self.pk(*argv, env=env, cwd=self.wt if argv[0] == "run" else None)
            self.assertEqual(result.returncode, 1, argv)
            self.assertIn(words, result.stderr, argv)
        self.assertEqual(self.ssh_calls(), [])

    def test_without_a_remote_section_pk_remote_says_it_is_opt_in(self):
        write_yaml(self.root / "program" / "checks.yaml", checks_data())
        commit(self.root, "no remote")
        result = self.pk("status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no remote section: pk remote is opt-in", result.stderr)


class Run(RemoteBase):
    def test_a_run_ships_head_and_the_uncommitted_changes_runs_under_the_lane_lock_and_brings_back_get_paths(self):
        (self.wt / "calc.py").write_text("changed here\n")
        (self.wt / "new.txt").write_text("untracked\n")
        (self.wt / "gone.txt").unlink()
        probe = ("import json, os, sys; json.dump({'args': sys.argv[1:], 'calc': open('calc.py').read(), "
                 "'gone': os.path.exists('gone.txt'), 'new': open('new.txt').read(), "
                 "'env': [os.environ.get(k) for k in ('LANE_FLAG', 'WRAPPED', 'PINNED', 'PK_PLANT_WORKERS')], "
                 "'setup': os.path.exists('setup-ran'), "
                 "'lock': open(os.path.join(os.popen('git rev-parse --git-common-dir').read().strip(), "
                 "'pk-heavy.lock', 'owner')).read()}, open('out.json', 'w')); print('lane says hi')")
        result = self.pk("run", "--lane", "lane1", "--get", "out.json", "suite", "--", PY, "-c", probe, "$(id)",
                         "a b", cwd=self.wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("lane says hi", result.stdout)
        import json
        seen = json.loads((self.wt / "out.json").read_text())
        self.assertEqual(seen["args"], ["$(id)", "a b"])
        self.assertEqual((seen["calc"], seen["gone"], seen["new"]), ("changed here\n", False, "untracked\n"))
        self.assertEqual(seen["env"], ["on", "yes", "1", "2"])
        self.assertTrue(seen["setup"])
        self.assertIn("owner=run-suite", seen["lock"])
        self.assertFalse((self.machine / "lane1" / ".git" / "pk-heavy.lock").exists())  # released
        log = (self.machine / "lane1.log").read_text().splitlines()
        self.assertRegex(log[0], r"start run-suite at [0-9a-f]{12}\Z")
        self.assertTrue(any(line.endswith(" lane says hi") for line in log), log)
        self.assertRegex(log[-1], r"end run-suite exit 0\Z")
        refs = sh(self.machine / "repo.git", "git", "for-each-ref", "--format=%(refname)")
        self.assertIn(f"refs/runs/run-{TASK.lower()}", refs)
        self.assertNotIn("origin", sh(self.root, "git", "remote"))  # nothing but the mirror was pushed to
        failed = self.pk("run", "--lane", "lane2", "suite", "--", PY, "-c", "raise SystemExit(5)", cwd=self.wt)
        self.assertEqual(failed.returncode, 5, failed.stderr)

    def test_status_shows_each_lane_and_its_lock(self):
        (self.machine / "lane2" / ".git" / "pk-heavy.lock").mkdir()
        (self.machine / "lane2" / ".git" / "pk-heavy.lock" / "owner").write_text(
            f"owner=someone\npid={os.getpid()}\nstarted=2026-11-09T00:00:00Z\n")
        result = self.pk("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("== lane1: ", result.stdout)
        self.assertIn("lock: free", result.stdout)
        self.assertIn("lock: busy", result.stdout)
        self.assertIn("  owner=someone", result.stdout)

    def test_auto_takes_the_first_free_lane(self):
        lock_dir = self.machine / "lane1" / ".git" / "pk-heavy.lock"
        lock_dir.mkdir()
        (lock_dir / "owner").write_text(f"owner=busy\npid={os.getpid()}\nstarted=2026-11-09T00:00:00Z\n")
        result = self.pk("run", "o", "--", PY, "-c", "print('ran')", cwd=self.wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"o on lanes-host:lane2 at", result.stderr)
        self.assertTrue((self.machine / "lane2.log").exists())
        self.assertFalse((self.machine / "lane1.log").exists())


if __name__ == "__main__":
    unittest.main()
