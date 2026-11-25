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
            deadline = time.time() + 60
            while sum("pk_free" in call for call in self.ssh_calls()) < 4 and time.time() < deadline:
                time.sleep(0.1)
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
        self.assertIn("git push -q -f '<PK_A>:lanes/repo.git'", result.stdout)
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


if __name__ == "__main__":
    unittest.main()
