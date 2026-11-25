"""`pk remote` (0.6.0): heavy steps on other machines over ssh, git and tar, opt-in by the checks.yaml `remote`
section. A fake ssh (an executable that drops the options and the host and runs the remote command here with sh -c)
stands in for the machine; a local directory holds the bare mirror and the lanes. From 0.7.2 two fake hosts, each a
home of its own, stand in for several machines (hosts_env, lane sizes, lane worktree clean-up, status)."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
# programs (git-receive-pack, for the push) on PATH. Each call is logged. With FAKE_HOMES set (0.7.2, several hosts)
# the destination names its own home, $FAKE_HOMES/<destination>, where the command starts as ssh starts in a home;
# a destination without one does not answer (exit 255, as ssh).
FAKE_SSH = '''#!/bin/sh
echo "$*" >> "$FAKE_SSH_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p|-i|-l|-F) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
dest=$1
shift
if [ -n "$FAKE_HOMES" ]; then
  [ -d "$FAKE_HOMES/$dest" ] || { echo "ssh: connect to host $dest: Connection refused" >&2; exit 255; }
  HOME="$FAKE_HOMES/$dest"
  export HOME
  cd "$HOME" || exit 255
fi
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
        self.assertIn(f"remote: push (to the lanes' mirror only, never to origin): git push -q --atomic --porcelain "
                      f"lanes-host:{self.machine}/repo.git refs/heads/main:refs/heads/main "
                      f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}", out)
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



# --- 0.7.2: several hosts, lane sizes, lane worktree clean-up, status -------------------------------------------

LANES = [{"name": "big", "size": "big"}, {"name": "small1", "size": "small"}, {"name": "small2", "size": "small"}]
HOSTS = {"PK_A": "alpha-host", "PK_B": "beta-host"}


class ParseHosts(unittest.TestCase):
    def test_hosts_sizes_and_the_new_keys_are_checked_and_appear_only_when_set(self):
        parsed = remote.parse({"hosts_env": ["PK_A", "PK_B"], "dir": "lanes", "lanes": [*LANES, "spare"],
                               "sizes": {"gate": "big", "plants": "small"}, "keep_worktrees": True,
                               "disk_warn_percent": 80})
        self.assertEqual((parsed["host_env"], parsed["hosts_env"]), (None, ["PK_A", "PK_B"]))
        self.assertEqual(parsed["lanes"], ["big", "small1", "small2", "spare"])
        self.assertEqual(parsed["lane_sizes"], {"big": "big", "small1": "small", "small2": "small",
                                                "spare": "default"})
        self.assertEqual((parsed["sizes"], parsed["keep_worktrees"], parsed["disk_warn_percent"]),
                         ({"gate": "big", "plants": "small"}, True, 80))
        filled = remote.config(remote.parse({"dir": "lanes", "lanes": ["lane1"]}))
        self.assertEqual((filled["host_vars"], filled["lane_sizes"], filled["sizes"], filled["keep_worktrees"],
                          filled["disk_warn_percent"]), (["PK_REMOTE_HOST"], {"lane1": "default"}, {}, False, None))

    def test_bad_hosts_and_sizes_are_refused_by_name(self):
        good = {"dir": "lanes", "lanes": LANES}
        for over, words in (({"host_env": "PK_A", "hosts_env": ["PK_B"]}, "not both"),
                            ({"hosts_env": []}, "remote.hosts_env must list"),
                            ({"hosts_env": ["a-b"]}, "remote.hosts_env must list"),
                            ({"hosts_env": ["PK_A", "PK_A"]}, "names a variable twice"),
                            ({"lanes": [{"name": "x", "cores": 2}]}, "a name and a size only"),
                            ({"lanes": [{"size": "big"}]}, "a name and a size only"),
                            ({"lanes": [{"name": "x", "size": "any"}]}, "the size of x must be a word"),
                            ({"lanes": [{"name": "x", "size": "a b"}]}, "the size of x must be a word"),
                            ({"lanes": [{"name": "x", "size": "big"}, "x"]}, "names a lane twice"),
                            ({"sizes": {"gate": "huge"}}, "remote.sizes.gate: 'huge' is not the size of a lane"),
                            ({"sizes": {"merge": "big"}}, "remote.sizes must map gate, plants and run"),
                            ({"keep_worktrees": "yes"}, "remote.keep_worktrees"),
                            ({"disk_warn_percent": 0}, "remote.disk_warn_percent"),
                            ({"disk_warn_percent": 100}, "remote.disk_warn_percent")):
            with self.assertRaises(KitError, msg=over) as ctx:
                remote.parse({**good, **over})
            self.assertIn(words, str(ctx.exception), over)


class TwoHosts(RemoteBase):
    """Two fake hosts, PK_A and PK_B, each with `lanes/repo.git` and lanes big (size big), small1 and small2 (size
    small) under its own home; the section on main gives sizes {gate: big, run: small}."""

    def setUp(self):
        super().setUp()
        self.homes = self.base / "homes"
        self.write_section()
        for dest in HOSTS.values():
            lanes = self.homes / dest / "lanes"
            lanes.mkdir(parents=True)
            sh(self.base, "git", "clone", "-q", "--no-local", "--bare", str(self.root), str(lanes / "repo.git"))
            for lane in ("big", "small1", "small2"):
                sh(self.base, "git", "clone", "-q", "--no-local", str(lanes / "repo.git"), str(lanes / lane))
        self.env.pop("PK_REMOTE_HOST", None)
        self.env.update(HOSTS, FAKE_HOMES=str(self.homes))

    def write_section(self, **over):
        data = {"hosts_env": ["PK_A", "PK_B"], "dir": "lanes", "lanes": LANES, "sizes": {"gate": "big", "run": "small"},
                "env": {"LANE_FLAG": "on"}, **over}
        write_yaml(self.root / "program" / "checks.yaml", checks_data(remote=data))
        commit(self.root, "two hosts")

    def lane(self, dest, lane):
        return self.homes / dest / "lanes" / lane

    def hold(self, dest, *lanes):
        for lane in lanes:
            held = self.lane(dest, lane) / ".git" / "pk-heavy.lock"
            held.mkdir()
            (held / "owner").write_text(f"owner=busy\npid={os.getpid()}\nstarted=2026-11-09T00:00:00Z\n")

    def free(self, dest, lane):
        shutil.rmtree(self.lane(dest, lane) / ".git" / "pk-heavy.lock")

    def run_here(self, *argv, env=None):
        return self.pk("run", *argv, "o", "--", PY, "-c", "print('ran')", cwd=self.wt, env=env)

    def where_ran(self):
        return sorted(f"{dest}:{log.stem}" for dest in HOSTS.values()
                      for log in (self.homes / dest / "lanes").glob("*.log"))

    def assert_no_destination(self, result):
        for dest in HOSTS.values():
            self.assertNotIn(dest, result.stdout + result.stderr)


class Hosts(TwoHosts):
    def test_auto_takes_the_wanted_size_in_host_order_skipping_a_busy_host_and_pushes_there_only(self):
        self.hold("alpha-host", "small1", "small2")
        result = self.run_here()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("remote: o on PK_B:small1 at ", result.stderr)
        self.assertIn("ran", result.stdout)
        self.assert_no_destination(result)
        self.assertEqual(self.where_ran(), ["beta-host:small1"])
        log = (self.homes / "beta-host" / "lanes" / "small1.log").read_text().splitlines()
        self.assertRegex(log[0], r"start run-o at [0-9a-f]{12} on PK_B:small1\Z")
        pushes = [call for call in self.ssh_calls() if "git-receive-pack" in call]
        self.assertEqual(len(pushes), 1, pushes)
        self.assertIn("beta-host", pushes[0])
        refs = {dest: sh(self.homes / dest / "lanes" / "repo.git", "git", "for-each-ref", "--format=%(refname)")
                for dest in HOSTS.values()}
        self.assertIn(f"refs/runs/run-{TASK.lower()}", refs["beta-host"])
        self.assertNotIn("refs/runs/", refs["alpha-host"])

    def test_size_any_and_another_size_override_the_default_and_lane_and_host_pin(self):
        self.hold("alpha-host", "big")
        self.assertEqual(self.run_here("--size", "any").returncode, 0)  # big is busy: the next lane of any size
        self.assertEqual(self.where_ran(), ["alpha-host:small1"])
        self.assertIn("remote: o on PK_B:big at ", self.run_here("--size", "big").stderr)  # alpha's big is busy
        self.assertIn("remote: o on PK_A:small2 at ", self.run_here("--lane", "small2").stderr)
        self.assertIn("remote: o on PK_B:small2 at ", self.run_here("--host", "PK_B", "--lane", "small2").stderr)
        self.hold("alpha-host", "small1")
        self.assertIn("remote: o on PK_B:small1 at ", self.run_here("--lane", "small1").stderr)  # any host

    def test_auto_waits_while_every_lane_of_the_size_is_busy_and_never_takes_another_size(self):
        for dest in HOSTS.values():
            self.hold(dest, "small1", "small2")
        proc = subprocess.Popen([PY, str(PK), "remote", "run", "o", "--", PY, "-c", "print('ran')"], cwd=str(self.wt),
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            def polls():  # the log is read by line; each poll's script ends with one `echo none` line (and a quote)
                return sum(call.startswith("echo none") for call in self.ssh_calls())

            deadline = time.time() + 300  # a second poll of both hosts has started, however loaded the machine is
            while polls() < 4 and time.time() < deadline:
                time.sleep(0.1)
            self.assertGreaterEqual(polls(), 4, "no second poll")
            self.assertIsNone(proc.poll())  # both big lanes are free, and still it waits
            self.free("beta-host", "small2")
            out, err = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("remote: every lane of size small is busy; waiting for one", err)
        self.assertIn("remote: o on PK_B:small2 at ", err)
        self.assertEqual(self.where_ran(), ["beta-host:small2"])

    def test_an_unset_and_an_unreachable_host_are_skipped_with_one_note_each(self):
        unset = {key: value for key, value in self.env.items() if key != "PK_A"}
        result = self.run_here(env=unset)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("remote: PK_A is not set; its lanes are skipped", result.stderr)
        self.assertIn("remote: o on PK_B:small1 at ", result.stderr)
        result = self.run_here(env={**self.env, "PK_A": "gone-host"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("remote: PK_A does not answer; its lanes are skipped until it does"), 1)
        self.assertIn("remote: o on PK_B:small1 at ", result.stderr)
        self.assertNotIn("gone-host", result.stderr)

    def test_refusals_name_the_fix(self):
        bare = {key: value for key, value in self.env.items() if key not in HOSTS}
        for argv, env, words in ((["--size", "huge"], None, "no lane has size huge"),
                                 (["--lane", "big", "--size", "big"], None, "not both"),
                                 (["--host", "PK_C"], None, "no host variable PK_C in remote.hosts_env"),
                                 ([], bare, "none of PK_A, PK_B is set")):
            result = self.run_here(*argv, env=env)
            self.assertEqual(result.returncode, 1, argv)
            self.assertIn(words, result.stderr, argv)
        result = self.pk("gate", TASK, "--size", "tiny", "--dry-run")
        self.assertIn("no lane has size tiny", result.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_a_dry_run_names_the_host_variables_never_their_values(self):
        result = self.pk("gate", TASK, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"remote: gate {TASK} on PK_A or PK_B (in that order), lane auto (the first free big lane of "
                      "big; waits while all are busy)", result.stdout)
        self.assertIn("git push -q --atomic --porcelain '<PK_A>:lanes/repo.git'", result.stdout)
        self.assert_no_destination(result)
        result = self.pk("plants", TASK, "--rows", "g1", "--host", "PK_B", "--size", "any", "--dry-run")
        self.assertIn(f"remote: plants {TASK} on PK_B, lane auto (the first free of big, small1, small2;",
                      result.stdout)
        self.assertEqual(self.ssh_calls(), [])


class Worktrees(TwoHosts):
    def setUp(self):
        super().setUp()
        self.small1 = self.lane("beta-host", "small1")
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("not the lane's\n")
        trees = self.small1 / ".worktrees"
        sh(self.small1, "git", "worktree", "add", "-q", "--detach", str(trees / "old-task"))
        (trees / "old-task" / "node_modules").mkdir()
        (trees / "old-task" / "node_modules" / "out").symlink_to(self.outside)  # a link inside: unlinked only
        (trees / "run-stale").mkdir()
        (trees / "run-stale" / "big.bin").write_bytes(b"x" * 1024)
        (trees / "a-link").symlink_to(self.outside)  # a link at the top: unlinked only
        (trees / "stray.txt").write_text("stray\n")

    def test_a_step_keeps_only_its_own_worktree_and_never_follows_a_link_out(self):
        result = self.run_here("--host", "PK_B", "--lane", "small1", "--get", "out.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("remote: removed 4 earlier lane worktree(s) under .worktrees", result.stdout)
        self.assertEqual(sorted(os.listdir(self.small1 / ".worktrees")), [f"run-{TASK.lower()}"])
        self.assertEqual((self.outside / "keep.txt").read_text(), "not the lane's\n")
        listed = sh(self.small1, "git", "worktree", "list", "--porcelain")
        self.assertNotIn("old-task", listed)  # pruned
        self.assertIn(f"run-{TASK.lower()}", listed)
        marks = self.small1 / ".git" / "pk-fetch"
        self.assertEqual(os.listdir(marks), [])  # the copy-back cleared its mark
        again = self.run_here("--host", "PK_B", "--lane", "small1")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("removed", again.stdout)
        self.assertEqual(sorted(os.listdir(self.small1 / ".worktrees")), [f"run-{TASK.lower()}"])

    def test_keep_worktrees_turns_the_clean_up_off(self):
        self.write_section(keep_worktrees=True)
        result = self.run_here("--host", "PK_B", "--lane", "small1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(os.listdir(self.small1 / ".worktrees")),
                         ["a-link", "old-task", f"run-{TASK.lower()}", "run-stale", "stray.txt"])

    def clean(self, top, keep, wait="0.3"):
        common = self.base / "common"
        return subprocess.run([PY, "-c", remote._CLEAN, keep, str(common), wait], cwd=str(top), capture_output=True,
                              text=True, check=True).stdout

    def test_the_clean_up_waits_for_a_pending_copy_back_and_refuses_a_linked_worktrees_directory(self):
        top = self.base / "lane"
        (top / ".worktrees" / "earlier").mkdir(parents=True)
        (top / ".worktrees" / "mine").mkdir()
        (self.base / "common" / "pk-fetch").mkdir(parents=True)
        (self.base / "common" / "pk-fetch" / "earlier").write_text("")
        out = self.clean(top, "mine")
        self.assertIn("remote: waiting for the copy-back from .worktrees/earlier", out)
        self.assertEqual(os.listdir(top / ".worktrees"), ["mine"])
        self.assertEqual(os.listdir(self.base / "common" / "pk-fetch"), [])  # a mark past its wait goes too
        linked = self.base / "linked"
        linked.mkdir()
        (linked / ".worktrees").symlink_to(self.outside)
        self.assertIn(".worktrees is not a plain directory here; no lane worktree removed", self.clean(linked, "x"))
        self.assertEqual(os.listdir(self.outside), ["keep.txt"])


class Status(TwoHosts):
    def test_status_lists_every_host_with_disk_use_sizes_and_lock_holders_and_an_unreachable_one(self):
        self.write_section(disk_warn_percent=1)
        self.hold("beta-host", "small2")
        result = self.pk("status", env={**self.env, "PK_A": "gone-host"})
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("host PK_A: unreachable", out)
        self.assertRegex(out, r"host PK_B: disk \d+% used under lanes")
        self.assertRegex(out, r"warning: the disk of PK_B is \d+% used \(remote.disk_warn_percent 1\)")
        self.assertIn("== big: ", out)
        self.assertIn("size: big", out)
        self.assertIn("size: small", out)
        self.assertRegex(out, r"lock: busy \(held by busy for \d+d\d\dh\)")
        self.assertIn("  owner=busy", out)
        self.assertEqual(out.count("lock: free"), 2)
        self.assert_no_destination(result)
        self.assertNotIn("gone-host", out + result.stderr)
        unset = self.pk("status", env={key: value for key, value in self.env.items() if key != "PK_A"})
        self.assertIn("host PK_A: not set", unset.stdout)

    def test_a_step_warns_at_its_start_when_the_disk_is_past_the_mark(self):
        self.write_section(disk_warn_percent=1)
        result = self.run_here()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r"remote: warning: the disk of PK_A is \d+% used \(remote.disk_warn_percent 1\)")


# --- 0.7.3: main reaches the mirror as a fast-forward only (KF-67), ad-hoc tasks (KF-71), the fingerprint on a lane
# (KF-70) and pk remote doctor (KF-72) ---------------------------------------------------------------------------

PROBE = [PY, "-c", "print('ran')"]


class MirrorMain(RemoteBase):
    """The task branch and refs/runs/<name> stay forced; main is pushed only as a fast-forward of the mirror's."""

    def mirror(self, ref):
        return sh(self.machine / "repo.git", "git", "rev-parse", "-q", "--verify", ref).strip()

    def local(self, ref):
        return sh(self.root, "git", "rev-parse", ref).strip()

    def advance_mirror(self) -> str:
        """The mirror's main moves past this checkout's, as when a current checkout pushed meanwhile."""
        other = self.base / "other"
        sh(self.base, "git", "clone", "-q", "--no-local", str(self.machine / "repo.git"), str(other))
        sh(other, "git", "config", "user.name", "Test")
        sh(other, "git", "config", "user.email", "test@example.com")
        (other / "later.txt").write_text("later\n")
        commit(other, "a later main")
        sh(other, "git", "push", "-q", "origin", "main")
        return self.mirror("refs/heads/main")

    def test_a_fast_forward_of_main_is_pushed_and_the_task_branch_is_still_forced(self):
        (self.root / "more.txt").write_text("more\n")
        commit(self.root, "main moves on")
        sh(self.wt, "git", "commit", "-q", "--amend", "-m", "task work, amended")  # the branch is rewritten
        result = self.pk("gate", TASK, "--lane", "lane1", "--dry-run")
        self.assertIn(f"refs/heads/main:refs/heads/main +refs/heads/{BRANCH}:refs/heads/{BRANCH}", result.stdout)
        result = self.pk("run", "--lane", "lane1", "o", "--", *PROBE, cwd=self.wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mirror("refs/heads/main"), self.local("main"))
        self.assertEqual(self.mirror(f"refs/runs/run-{TASK.lower()}"), self.local(BRANCH))
        sh(self.wt, "git", "commit", "-q", "--amend", "-m", "task work, amended again")
        result = self.pk("run", "--lane", "lane1", "o", "--", *PROBE, cwd=self.wt)  # a rewritten run ref is forced
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mirror(f"refs/runs/run-{TASK.lower()}"), self.local(BRANCH))

    def test_a_stale_main_is_refused_and_nothing_is_pushed_or_run(self):
        newer = self.advance_mirror()
        branch_before = self.mirror(f"refs/heads/{BRANCH}")
        (self.wt / "calc.py").write_text("changed\n")
        commit(self.wt, "more task work")
        for argv, cwd in ((["run", "--lane", "lane1", "o", "--", *PROBE], self.wt),
                          (["gate", TASK, "--lane", "lane1"], None)):
            result = self.pk(*argv, cwd=cwd)
            self.assertEqual(result.returncode, 1, (argv, result.stdout, result.stderr))
            self.assertIn("refused: the main of the lanes' mirror is not an ancestor of this checkout's main",
                          result.stderr, argv)
            self.assertIn("Nothing was pushed and nothing ran", result.stderr)
            self.assertIn("--force-main", result.stderr)
        self.assertEqual(self.mirror("refs/heads/main"), newer)
        self.assertEqual(self.mirror(f"refs/heads/{BRANCH}"), branch_before)  # atomic: the branch did not go either
        self.assertNotIn("refs/runs/", sh(self.machine / "repo.git", "git", "for-each-ref", "--format=%(refname)"))
        self.assertFalse((self.machine / "lane1.log").exists())
        self.assertEqual(sum("git-receive-pack" in call for call in self.ssh_calls()), 2)  # one try each, no retry

    def test_force_main_replaces_the_mirror_main(self):
        self.advance_mirror()
        dry = self.pk("gate", TASK, "--lane", "lane1", "--force-main", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(" +refs/heads/main:refs/heads/main ", dry.stdout)
        self.assertNotIn("--force-main", dry.stdout)  # a pk remote option, never passed to the gate
        result = self.pk("run", "--lane", "lane1", "--force-main", "o", "--", *PROBE, cwd=self.wt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ran", result.stdout)
        self.assertEqual(self.mirror("refs/heads/main"), self.local("main"))


class AdHoc(RemoteBase):
    """`pk remote gate` takes an ad-hoc AH-<nnnn> as `pk gate handback` does."""

    def test_an_ad_hoc_task_runs_its_gate_on_a_lane_like_a_register_task(self):
        adhoc, wt = "AH-0001", self.root / ".worktrees" / "ah-0001"
        missing = self.pk("gate", adhoc, "--lane", "lane2")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("pk worktree new AH-0001; ad-hoc work takes an ID first: pk adhoc add", missing.stderr)
        sh(self.root, "git", "worktree", "add", "-q", "-b", f"task/{adhoc}", str(wt))
        (wt / "chore.txt").write_text("a chore\n")
        commit(wt, "the chore")
        dry = self.pk("gate", adhoc, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f"python3 kit/scripts/pk.py gate handback {adhoc} --branch task/{adhoc}", dry.stdout)
        # On the lane, echo stands in for the gate (wrap), so the test sees the command the lane ran.
        write_yaml(self.root / "program" / "checks.yaml",
                   checks_data(remote=section(self.machine, wrap=["echo", "lane-ran"], lane_wrap={})))
        commit(self.root, "echo on the lanes")
        result = self.pk("gate", adhoc, "--lane", "lane2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"lane-ran python3 kit/scripts/pk.py gate handback {adhoc} --branch task/{adhoc}", result.stdout)
        lane_wt = self.machine / "lane2" / ".worktrees" / "ah-0001"
        self.assertEqual((lane_wt / "chore.txt").read_text(), "a chore\n")
        self.assertEqual(sh(lane_wt, "git", "rev-parse", "HEAD"), sh(wt, "git", "rev-parse", "HEAD"))


class FingerprintOnALane(RemoteBase):
    """The release fingerprint runs through `pk remote run` from a clean checkout, and its record comes back with
    `--get` (the command playbook 08 and 12_lanes.md give)."""

    def test_the_documented_command_brings_back_a_record_that_validates(self):
        shutil.copytree(KIT / "templates", self.root / "kit" / "templates")  # the record's schema
        write_yaml(self.root / "program" / "checks.yaml", checks_data(remote=section(self.machine, setup=[])))
        write_yaml(self.root / "program" / "release-set.yaml", {"toolchain": [], "entries": [
            {"id": "unit", "argv": ["python3", "-c", "print('release ok')"], "track": "plain", "kind": "check"}]})
        commit(self.root, "a release set")
        head12 = self.local("HEAD")[:12]
        result = self.pk("run", "--lane", "lane1", "--get", f"program/evidence/fingerprint/{head12}", "fingerprint",
                         "--", "python3", "kit/scripts/pk.py", "fingerprint", "run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"fingerprint {head12}: complete true, all as expected true", result.stdout)
        record = self.root / "program" / "evidence" / "fingerprint" / head12 / "fingerprint.json"
        self.assertTrue(record.is_file())
        check = subprocess.run([PY, str(PK), "fingerprint", "validate", str(record)], cwd=str(self.root),
                               env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    local = MirrorMain.local


class Doctor(TwoHosts):
    def test_doctor_checks_each_host_and_names_each_problem(self):
        for var in ("SSL_CERT_FILE", "DOCKER_HOST"):  # the fake ssh passes this machine's environment on
            self.env.pop(var, None)
        result = self.pk("doctor")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for var in HOSTS:
            for what in ("python3 3.", "git", "tar", "mirror", "lane big", "lane small1", "lane small2"):
                self.assertIn(f"host {var}: ok {what}", result.stdout)
        self.assertIn("remote doctor: ok", result.stdout)
        shutil.rmtree(self.lane("beta-host", "small2"))
        self.write_section(env={"SSL_CERT_FILE": "/nowhere/cert.pem", "DOCKER_HOST": "unix:" + "///nowhere/docker.sock"})
        result = self.pk("doctor", env={**self.env, "PK_A": "gone-host"})
        self.assertEqual(result.returncode, 1)
        out = result.stdout
        self.assertIn("host PK_A: problem: unreachable", out)
        self.assertIn("host PK_B: problem: lane small2: no checkout at", out)
        self.assertIn("host PK_B: ok lane small1", out)
        self.assertIn("host PK_B: problem: SSL_CERT_FILE names a file that is not on this host", out)
        self.assertIn("host PK_B: problem: DOCKER_HOST names a socket that is not on this host", out)
        self.assertIn("remote doctor: problems found", out)
        self.assert_no_destination(result)
        self.assertNotIn("gone-host", out + result.stderr)



LANE_PREPARER = """import json
import os
from pathlib import Path


def prepare(ctx):
    if os.environ.get("LANE_PREPARE_STRAY"):
        (Path(ctx.tree) / "stray.txt").write_text("not ignored")
        return None
    (Path(ctx.tree) / "deps").mkdir(exist_ok=True)
    (Path(ctx.tree) / "deps" / "ctx.json").write_text(json.dumps({
        "purpose": ctx.purpose, "root": str(ctx.root), "tree": str(ctx.tree), "head": ctx.head,
        "installed": (Path(ctx.root) / "deps-cache" / "dep.txt").read_text()}))


def register(api):
    api.prepare_tree(prepare)
"""


class PrepareTreeOnALane(RemoteBase):
    """0.7.3 (KF-73): a lane's step tree is prepared by the extensions' prepare_tree, purpose remote, with the lane's
    checkout (which holds the lane's git-ignored dependencies) as ctx.root, before setup and the step."""

    def setUp(self):
        super().setUp()
        (self.root / ".gitignore").write_text("__pycache__/\n.worktrees/\ndeps\ndeps-cache/\nsetup-ran\n")
        (self.root / "program" / "extensions").mkdir(parents=True)
        (self.root / "program" / "extensions.yaml").write_text("api: 1\nmodules:\n  - name: deps\n")
        (self.root / "program" / "extensions" / "deps.py").write_text(LANE_PREPARER)
        commit(self.root, "an extension that prepares trees")
        sh(self.wt, "git", "merge", "-q", "main")
        (self.machine / "lane1" / "deps-cache").mkdir()
        (self.machine / "lane1" / "deps-cache" / "dep.txt").write_text("installed on lane1\n")
        self.env.pop("PK_EXT_FROM", None)

    def test_the_lane_tree_is_prepared_with_the_lane_checkout_as_root(self):
        probe = [PY, "-c", "import json; print('seen', json.dumps(json.load(open('deps/ctx.json'))))"]
        result = self.pk("run", "--lane", "lane1", "o", "--", *probe, cwd=self.wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("step ext:deps.prepare_tree: ok (remote tree)", result.stdout)
        import json
        seen = json.loads(result.stdout.split("seen ", 1)[1].splitlines()[0])
        lane = (self.machine / "lane1").resolve()
        self.assertEqual(seen, {"purpose": "remote", "root": str(lane),
                                "tree": str(lane / ".worktrees" / f"run-{TASK.lower()}"),
                                "head": sh(self.wt, "git", "rev-parse", "HEAD").strip(),
                                "installed": "installed on lane1\n"})
        dry = self.pk("gate", TASK, "--lane", "lane1", "--dry-run")
        self.assertIn(f"ext prepare-tree remote .worktrees/{TASK.lower()} --head ", dry.stdout)

    def test_a_refusing_preparer_fails_the_step_before_it_runs(self):
        result = self.pk("run", "--lane", "lane1", "o", "--", PY, "-c", "print('the step ran')", cwd=self.wt,
                         env={**self.env, "LANE_PREPARE_STRAY": "1"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED: ext:deps.prepare_tree changed what git sees in the tree", result.stdout)
        self.assertNotIn("the step ran", result.stdout)


# A stand-in pk.py: it logs which kit ran which command to $KIT_MARK, then hands over to the real kit (pk_real.py)
# beside it. As main's 0.7.2 it lacks `ext prepare-tree`, failing as that kit's argparse did (KF-75); either kit's
# `gate` only says who judged it.
KIT_SHIM = """import os
import runpy
import sys
from pathlib import Path

WHO = "{who}"
with open(os.environ["KIT_MARK"], "a", encoding="utf-8") as mark:
    mark.write(WHO + " " + " ".join(sys.argv[1:3]) + "\\n")
if WHO == "main-kit" and sys.argv[1:3] == ["ext", "prepare-tree"]:
    sys.stderr.write("pk ext: invalid choice: 'prepare-tree'\\n")
    sys.exit(2)
if sys.argv[1:2] == ["gate"]:
    print(WHO + " judged the gate")
    sys.exit(0)
runpy.run_path(str(Path(__file__).with_name("pk_real.py")), run_name="__main__")
"""


class KitFromTheRunTree(RemoteBase):
    """0.7.4 (KF-75): a branch that upgrades the kit (0.7.4 here, main's 0.7.2) runs its step's kit commands on the
    lane with its own kit; only the lane lock keeps main's. A lane kit without what the step needs is refused up
    front, before anything is pushed."""

    def setUp(self):
        super().setUp()
        (self.root / ".gitignore").write_text("__pycache__/\n.worktrees/\ndeps\ndeps-cache/\nsetup-ran\n")
        (self.root / "program" / "extensions").mkdir(parents=True)
        (self.root / "program" / "extensions.yaml").write_text("api: 1\nmodules:\n  - name: deps\n")
        (self.root / "program" / "extensions" / "deps.py").write_text(LANE_PREPARER)
        self.shim(self.root, "main-kit", "0.7.2")
        commit(self.root, "main: kit 0.7.2 and an extension that prepares trees")
        sh(self.wt, "git", "merge", "-q", "main")
        self.shim(self.wt, "tree-kit", "0.7.4")
        commit(self.wt, "kit: upgrade to 0.7.4")
        (self.machine / "lane1" / "deps-cache").mkdir()
        (self.machine / "lane1" / "deps-cache" / "dep.txt").write_text("installed on lane1\n")
        self.mark = self.base / "kit-mark.log"
        self.env["KIT_MARK"] = str(self.mark)
        self.env.pop("PK_EXT_FROM", None)

    @staticmethod
    def shim(tree, who, version):
        scripts = tree / "kit" / "scripts"
        if not (scripts / "pk_real.py").exists():
            (scripts / "pk.py").rename(scripts / "pk_real.py")
        (scripts / "pk.py").write_text(KIT_SHIM.replace("{who}", who))
        (tree / "kit" / "VERSION").write_text(version + "\n")

    def marks(self):
        return self.mark.read_text().splitlines() if self.mark.exists() else []

    def test_a_kit_upgrade_branch_runs_on_a_lane_with_its_own_kit(self):
        result = self.pk("run", "--lane", "lane1", "o", "--", PY, "-c", "print('the step ran')", cwd=self.wt)
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertNotIn("invalid choice", out)
        self.assertIn("kit on the lane: the run tree's (main's 0.7.2, the run tree's 0.7.4)", out)
        self.assertIn("step ext:deps.prepare_tree: ok (remote tree)", result.stdout)
        self.assertIn("the step ran", result.stdout)
        self.assertEqual(self.marks(), ["main-kit lock run", "tree-kit ext prepare-tree"])

    def test_the_gate_and_prepare_tree_run_through_the_run_trees_kit(self):
        kit = f".worktrees/{TASK.lower()}/kit/scripts/pk.py"
        dry = self.pk("gate", TASK, "--lane", "lane1", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f"python3 {kit} gate handback {TASK} --branch {BRANCH}", dry.stdout)
        self.assertIn(f"python3 {kit} ext prepare-tree remote .worktrees/{TASK.lower()} --head ", dry.stdout)
        self.assertIn("remote: kit on the lane: the run tree's (main's 0.7.2, the run tree's 0.7.4)", dry.stdout)
        plants = self.pk("plants", TASK, "--rows", "g1", "--lane", "lane1", "--dry-run")
        self.assertIn(f"python3 {kit} gate plants {TASK} --branch {BRANCH} --rows g1", plants.stdout)
        result = self.pk("gate", TASK, "--lane", "lane1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("step ext:deps.prepare_tree: ok (remote tree)", result.stdout)
        self.assertIn("tree-kit judged the gate", result.stdout)
        self.assertEqual(self.marks(), ["main-kit lock run", "tree-kit ext prepare-tree", "tree-kit gate handback"])

    def test_a_lane_kit_without_prepare_tree_is_refused_up_front_and_pushes_nothing(self):
        (self.wt / "kit" / "VERSION").write_text("0.7.2\n")  # the branch's kit is main's again: no prepare-tree
        commit(self.wt, "kit: back to 0.7.2")
        for argv, cwd in ((["run", "--lane", "lane1", "o", "--", *PROBE], self.wt),
                          (["run", "--lane", "lane1", "--dry-run", "o", "--", *PROBE], self.wt),
                          (["gate", TASK, "--lane", "lane1"], None), (["gate", TASK, "--dry-run"], None),
                          (["plants", TASK, "--rows", "g1", "--lane", "lane1", "--dry-run"], None)):
            result = self.pk(*argv, cwd=cwd)
            self.assertEqual(result.returncode, 1, (argv, result.stdout, result.stderr))
            said = " ".join(result.stderr.split())
            self.assertIn("refused before any push: the lane would run this step's kit commands with main's kit 0.7.2 "
                          "(main's 0.7.2, the run tree's 0.7.2), older than 0.7.3, the first kit with `pk ext "
                          "prepare-tree`", said, argv)
            self.assertIn("Nothing was pushed and nothing ran; run the step here, or merge a kit", said, argv)
            self.assertNotIn("invalid choice", result.stdout + result.stderr)
        self.assertEqual(self.ssh_calls(), [])  # no lane polled, nothing pushed
        self.assertEqual(self.marks(), [])
        self.assertFalse((self.machine / "lane1.log").exists())
        self.assertNotIn("refs/runs/", sh(self.machine / "repo.git", "git", "for-each-ref", "--format=%(refname)"))
        (self.wt / "kit" / "VERSION").write_text("0.7.4\n")  # for run, a shipped kit/VERSION is the run tree's
        dry = self.pk("run", "--lane", "lane1", "--dry-run", "o", "--", *PROBE, cwd=self.wt)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("kit on the lane: the run tree's (main's 0.7.2, the run tree's 0.7.4)", dry.stdout)


class KitOfAnOlderBranch(RemoteBase):
    def test_a_branch_whose_kit_is_older_than_mains_keeps_mains_kit(self):
        (self.root / "kit" / "VERSION").write_text("9.9.9\n")
        commit(self.root, "main: a newer kit")
        old = (KIT / "VERSION").read_text().strip()
        dry = self.pk("gate", TASK, "--lane", "lane1", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f"python3 kit/scripts/pk.py gate handback {TASK}", dry.stdout)
        self.assertIn(f"remote: kit on the lane: main's (main's 9.9.9, the run tree's {old})", dry.stdout)


# --- 0.7.6: a failed gate step's full output is kept on the lane until the task is done (KF-77) -----------------

# A stand-in for `pk gate handback` on the lane (the section's wrap puts it first): it keeps the step's long output
# with the lane's own kit (keptlogs, under PK_LOG_STAMP), prints the gate's lines and fails as a failed test step does.
FAKE_GATE = """import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "kit", "scripts"))
import keptlogs

task = sys.argv[sys.argv.index("handback") + 1]
common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True,
                        text=True, check=True).stdout.strip()
output = "".join("ok %d\\n" % i for i in range(60)) + "FAIL realtime.test.ts > reconnects\\nAssertionError: 1 != 2\\n"
output += "".join("after %d\\n" % i for i in range(30))
log, digest, count, failures = keptlogs.keep(common, task, "test", "step test: pnpm -r test (exit 1)", output)
print("FAILED: test pnpm -r test (exit 1)")
print("kept log: %s" % log)
sys.exit(1)
"""


class KeptLogsOnALane(TwoHosts):
    def setUp(self):
        super().setUp()
        (self.root / "fake_gate.py").write_text(FAKE_GATE)
        self.write_section(wrap=["python3", "fake_gate.py"])
        self.big = self.lane("beta-host", "big")
        self.kept = self.big / ".git" / "pk-logs" / TASK

    def test_a_gate_dry_run_names_where_a_failing_step_keeps_its_log_and_the_script_keeps_and_cleans(self):
        result = self.pk("gate", TASK, "--host", "PK_B", "--lane", "big", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"remote: a failing step keeps its full output on the lane, at lanes/big/\.git/"
                                        rf"pk-logs/{TASK}/[0-9]{{8}}T[0-9]{{6}}Z-<step>\.log \(until {TASK} is merged, "
                                        r"or 14 days\); its failure digest comes back here; read the log with "
                                        rf"pk remote log {TASK}")
        ssh = result.stdout.split("remote: ssh: ", 1)[1]
        self.assertRegex(ssh, r"export PK_LOG_STAMP=[0-9]{8}T[0-9]{6}Z")
        self.assertIn("the kept-log clean-up failed; the step runs on", ssh)
        self.assert_no_destination(result)
        self.assertEqual(self.ssh_calls(), [])

    def test_a_failed_gate_step_keeps_its_full_log_on_the_lane_brings_its_digest_back_and_log_reads_it(self):
        result = self.pk("gate", TASK, "--host", "PK_B", "--lane", "big")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        logs = sorted(self.kept.glob("*.log"))
        self.assertEqual(len(logs), 1, result.stdout + result.stderr)
        log = logs[0]
        self.assertEqual(len(log.read_text().splitlines()), 92)
        self.assertIn(f"kept log: {log}", result.stdout)  # the lane's own line, streamed, with its path there
        shown = f"lanes/big/.git/pk-logs/{TASK}/{log.name}"  # the runner's: under remote.dir, as the section says
        self.assertIn(f"remote: kept log on PK_B:big: {shown} (the step's full output; kept until {TASK} is merged, or "
                      "14 days)", result.stderr)
        local = self.root / ".git" / "pk-logs" / TASK / f"PK_B-big-{log.stem}.digest"
        self.assertIn(f"remote: failure digest (2 line(s)) copied to {local}", result.stderr)
        self.assertEqual(local.read_text().splitlines()[2:], ["61: FAIL realtime.test.ts > reconnects",
                                                              "62: AssertionError: 1 != 2"])
        self.assertIn(f"remote: read it: pk remote log {TASK} --host PK_B --lane big --file {log.name} --tail 200",
                      result.stderr)
        self.assertEqual(sorted(self.lane("alpha-host", "big").glob(".git/pk-logs/*")), [])
        listed = self.pk("log", TASK)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.splitlines(), [f"PK_B:big {log.name} {log.stat().st_size} bytes {shown}"])
        tail = self.pk("log", TASK, "--tail", "3")
        self.assertEqual((tail.returncode, tail.stdout), (0, "after 27\nafter 28\nafter 29\n"), tail.stderr)
        self.assertIn(f"remote: PK_B:big {shown}", tail.stderr)
        grep = self.pk("log", TASK, "--host", "PK_B", "--grep", "FAIL|Assertion")
        self.assertEqual(grep.stdout.splitlines(), ["61:FAIL realtime.test.ts > reconnects",
                                                    "62:AssertionError: 1 != 2"], grep.stderr)
        dest = self.base / "copied"
        dest.mkdir()
        copy = self.pk("log", TASK, "--file", log.name, "--copy", str(dest))
        self.assertEqual(copy.returncode, 0, copy.stderr)
        self.assertEqual((dest / log.name).read_bytes(), log.read_bytes())
        self.assertNotIn("beta-host", result.stderr)  # the lane's streamed lines may name its home; the runner never
        for done in (listed, tail, grep, copy):
            self.assert_no_destination(done)
        none = self.pk("log", TASK, "--host", "PK_A", "--tail", "3")
        self.assertEqual(none.returncode, 1)
        self.assertIn(f"no kept log of {TASK} on the lanes", none.stderr)

    def test_a_lane_step_removes_a_done_tasks_and_old_kept_logs_and_keeps_an_unmerged_recent_one(self):
        status_dir = self.root / "program" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "DONE-1.json").write_text('{"status": "done"}\n')
        commit(self.root, "DONE-1 is done")
        self.write_section()  # no stand-in gate: a plain run
        kept = self.lane("alpha-host", "small1") / ".git" / "pk-logs"
        for task, days in (("DONE-1", 0), ("OLD-1", 15), (TASK, 0)):
            (kept / task).mkdir(parents=True)
            log = kept / task / "20261101T000000Z-test.log"
            log.write_text("x\n")
            then = time.time() - days * 86400
            os.utime(log, (then, then))
        result = self.run_here("--host", "PK_A", "--lane", "small1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("remote: removed the kept logs of DONE-1 (done)", result.stdout)
        self.assertIn("remote: removed the kept logs of OLD-1 (older than 14 days)", result.stdout)
        self.assertEqual(os.listdir(kept), [TASK])


if __name__ == "__main__":
    unittest.main()
