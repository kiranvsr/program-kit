import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evidence
import gate
import pk
from common import KitError, read_json, read_yaml, sha256_file, write_yaml

PY = os.path.realpath(sys.executable)
GIT = shutil.which("git")
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PRESETS = Path(__file__).resolve().parent.parent / "presets"
TASK = "P0-M01-S01-T01"
BRANCH = "task/p0-m01-s01-t01"
TAIL = "    | "
OK_LINE = re.compile(r"OK: (\S+) passes the hand-back gate at ([0-9a-f]{40})\Z")
CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_TEST = ("import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n\n"
             "if __name__ == '__main__':\n    unittest.main()\n")
PLAN = ("# Plan\n\n## Exact Commands\n\n"
        "$ python3 -c \"import calc; print(calc.add(1, 2))\"\n"
        "$ python3 -c \"import sys; sys.exit(3)\" # expect 3\n\n"
        "## Notes\n\n$ not-a-command\n")
PRODUCE = '''import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["PK_SCRIPTS"])
import evidence

root = Path.cwd()
task = "P0-M01-S01-T01"
ev = evidence.Evidence(root, task)
ev.hash_inputs(["calc.py", "test_calc.py"])
if not os.environ.get("PK_NO_COMMANDS"):
    ev.run_command("unit", [sys.executable, "-m", "unittest", "-q"])
if os.environ.get("PK_SURVIVOR"):
    bug = {"id": "sub-plus", "path": "calc.py", "find": "return a - b", "replace": "return a + b",
           "test_args": ["test_calc"]}
else:
    bug = {"id": "add-minus", "path": "calc.py", "find": "return a + b", "replace": "return a - b",
           "test_args": ["test_calc"]}
ev.planted_bugs([bug], build=[sys.executable, "-m", "compileall", "-q", "."],
                test=[sys.executable, "-m", "unittest", "-q"])
out = root / "program" / "evidence" / task / "results.json"
ev.write(out)
if os.environ.get("PK_STRAY"):
    stray = root / os.environ["PK_STRAY"]
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("left behind\\n")
if os.environ.get("PK_TOUCH"):
    (root / os.environ["PK_TOUCH"]).write_text("changed by produce\\n")
'''


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def commit(cwd, message="change"):
    sh(cwd, "git", "add", "-A")
    sh(cwd, "git", "commit", "-q", "-m", message)
    return sh(cwd, "git", "rev-parse", "HEAD").strip()


def put(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def argv_block(commands):
    return [list(argv) for argv in commands]


def checks_data(**over):
    data = {"build": [[PY, "-m", "compileall", "-q", "."]], "test": [[PY, "-m", "unittest", "-q"]],
            "scan": [["gitleaks", "detect", "--no-git"]], "compile_check": [], "extra_gate": [],
            "guarded_files": ["pyproject.toml", "program/checks.yaml"]}
    data.update(over)
    return data


def worktree_paths(root):
    return [ln.split(" ", 1)[1] for ln in sh(root, "git", "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")]


def run_quiet(function, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = function(*args, **kwargs)
    return rc, out.getvalue()


class GitBase(unittest.TestCase):
    """A throwaway repository on branch main, with a controlled PATH and the produce.py environment."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "repo"
        self.root.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.fake_log = base / "fake.log"
        self.wrapper("python3", f'exec "{PY}" "$@"')
        self.wrapper("git", f'exec "{GIT}" "$@"')
        self.tmpdir = base / "tmp"  # every mkdtemp of the code under test lands here, so leftovers are visible
        self.tmpdir.mkdir()
        real_mkdtemp = tempfile.mkdtemp
        scoped = mock.patch.object(tempfile, "mkdtemp", lambda suffix=None, prefix=None, dir=None:
                                   real_mkdtemp(suffix, prefix, dir or str(self.tmpdir)))
        scoped.start()
        self.addCleanup(scoped.stop)
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bin), "PK_SCRIPTS": str(SCRIPTS),
                                               "PK_FAKE_LOG": str(self.fake_log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("PK_NO_COMMANDS", "PK_SURVIVOR", "PK_STRAY"):
            os.environ.pop(var, None)
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")

    def trial_dirs(self):
        return sorted(self.tmpdir.glob("pk-trial-*"))

    def wrapper(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def fake_scanner(self, name="gitleaks"):
        self.wrapper(name, 'echo "$0 $@" >> "$PK_FAKE_LOG"\nexit 0')

    def write_checks(self, **over):
        write_yaml(self.root / "program" / "checks.yaml", checks_data(**over))


class HandbackBase(GitBase):
    def setUp(self):
        super().setUp()
        self.fake_scanner()
        put(self.root, ".gitignore", "__pycache__/\n.worktrees/\n")
        put(self.root, "calc.py", CALC)
        put(self.root, "test_calc.py", CALC_TEST)
        put(self.root, "notes.txt", "one\n")
        put(self.root, "a", "a\n")
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "# Spec\n")
        put(self.root, f"program/tasks/{TASK}/PLAN.md", PLAN)
        self.write_checks()
        commit(self.root, "init")
        self.wt = self.root / ".worktrees" / TASK.lower()
        sh(self.root, "git", "worktree", "add", "-q", "-b", BRANCH, str(self.wt))
        put(self.wt, f"program/evidence/{TASK}/produce.py", PRODUCE)
        put(self.wt, "calc.py", CALC + "\n\ndef mul(a, b):\n    return a * b\n")
        self.sha = commit(self.wt, "task work")
        self.addCleanup(self.cleanup_worktrees)

    def cleanup_worktrees(self):
        subprocess.run(["git", "-C", str(self.root), "worktree", "prune"], capture_output=True)

    def check_ok_lines(self, rc, out):
        """A failed gate never prints an `OK: ` line; a passing one prints exactly one, last."""
        lines = [ln for ln in out.splitlines() if ln.startswith("OK: ")]
        if rc == 0:
            self.assertEqual(lines, out.splitlines()[-1:], out)
        else:
            self.assertEqual(lines, [], out)

    def handback(self, **kwargs):
        rc, out = run_quiet(gate.handback, self.root, TASK, BRANCH, **kwargs)
        self.check_ok_lines(rc, out)
        return rc, out

    def merge_check(self, **kwargs):
        rc, out = run_quiet(gate.merge_check, self.root, BRANCH, **kwargs)
        self.check_ok_lines(rc, out)
        return rc, out

    def set_plan(self, text):
        """Change PLAN.md on main (None removes it) and bring the task branch up to date."""
        path = self.root / "program" / "tasks" / TASK / "PLAN.md"
        if text is None:
            path.unlink()
        else:
            path.write_text(text)
        commit(self.root, "plan")
        sh(self.wt, "git", "merge", "-q", "main")

    def assert_no_trial_left(self):
        self.assertEqual(list(self.tmpdir.iterdir()), [])  # no pk-trial-* and no pk-mirror-* directory
        self.assertEqual([p for p in worktree_paths(self.root) if "pk-trial-" in p or "pk-mirror-" in p], [])


class PresetTests(unittest.TestCase):
    KEYS = ("build", "test", "scan", "compile_check", "extra_gate", "guarded_files")
    EXPECT = {
        "go": {"build": [["go", "build", "./..."]], "compile_check": [["go", "vet", "./..."]],
               "test": [["go", "test", "./..."]],
               "scan": [["gosec", "./..."], ["gitleaks", "detect", "--no-git"]],
               "guarded": ["go.mod", "go.sum", "program/checks.yaml"]},
        "node": {"build": [["pnpm", "install", "--frozen-lockfile", "--offline"], ["pnpm", "build"]],
                 "compile_check": [], "test": [["pnpm", "test"]],
                 "scan": [["pnpm", "audit"], ["gitleaks", "detect", "--no-git"]],
                 "guarded": ["package.json", "pnpm-lock.yaml", "program/checks.yaml"]},
        "python": {"build": [["python3", "-m", "compileall", "-q", "."]], "compile_check": [],
                   "test": [["python3", "-m", "unittest", "discover"]],
                   "scan": [["python3", "-m", "pip", "check"], ["gitleaks", "detect", "--no-git"]],
                   "guarded": ["pyproject.toml", "requirements.txt", "program/checks.yaml"]},
    }

    def test_presets_parse_and_have_all_keys(self):
        for name, expect in self.EXPECT.items():
            with self.subTest(preset=name):
                data = read_yaml(PRESETS / f"checks.{name}.yaml")
                self.assertEqual(set(data), set(self.KEYS))
                for key in ("build", "test", "scan"):
                    self.assertTrue(data[key], key)
                self.assertEqual(data["extra_gate"], [])
                self.assertEqual(data["guarded_files"], expect["guarded"])
                self.assertEqual(data["test"], expect["test"])
                self.assertEqual(data["scan"], expect["scan"])
                self.assertEqual(data["build"], expect["build"])
                self.assertEqual(data["compile_check"], expect["compile_check"])

    def test_presets_load_through_load_checks(self):
        for name in self.EXPECT:
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "program").mkdir()
                shutil.copy(PRESETS / f"checks.{name}.yaml", Path(tmp) / "program" / "checks.yaml")
                checks = gate.load_checks(Path(tmp), require=("build", "test", "scan"))
                self.assertEqual(checks["scan"][-1], ["gitleaks", "detect", "--no-git"])


class LoadChecksTests(GitBase):
    def put_raw(self, text):
        put(self.root, "program/checks.yaml", text)

    def test_missing_keys_default_to_empty(self):
        self.put_raw("build:\n  - - make\n")
        checks = gate.load_checks(self.root)
        self.assertEqual(checks["build"], [["make"]])
        self.assertEqual((checks["test"], checks["guarded_files"]), ([], []))

    def test_required_empty_list_names_the_key(self):
        self.write_checks(scan=[])
        with self.assertRaises(KitError) as ctx:
            gate.load_checks(self.root, require=("build", "test", "scan"))
        self.assertEqual(str(ctx.exception), "checks.yaml: scan must list at least one command")

    def test_shell_string_and_bad_shapes_are_refused(self):
        for text in ("build:\n  - make all\n", "build: make\n", "build:\n  - []\n", "guarded_files: x\n",
                     "build:\n  - - 3\n"):
            with self.subTest(text=text):
                self.put_raw(text)
                with self.assertRaises(KitError):
                    gate.load_checks(self.root)

    def test_unknown_key_and_missing_file(self):
        self.put_raw("deploy: []\n")
        with self.assertRaises(KitError):
            gate.load_checks(self.root)
        (self.root / "program" / "checks.yaml").unlink()
        with self.assertRaises(KitError):
            gate.load_checks(self.root)


class PlanTests(unittest.TestCase):
    def test_plan_commands_parsed_with_expected_exit(self):
        found = gate.parse_plan(PLAN)
        self.assertEqual(found, [(["python3", "-c", "import calc; print(calc.add(1, 2))"], 0),
                                 (["python3", "-c", "import sys; sys.exit(3)"], 3)])

    def test_only_the_exact_commands_section_counts(self):
        text = "## Other\n$ no\n## Exact Commands\nprose\n$ echo 'a b'  \n$ ls #expect 2\n## Next\n$ no\n"
        self.assertEqual(gate.parse_plan(text), [(["echo", "a b"], 0), (["ls", "#expect", "2"], 0)])

    def test_expect_needs_the_marker_and_unparsable_line_is_an_error(self):
        self.assertEqual(gate.parse_plan("## Exact Commands\n$ make  #  expect   12\n"), [(["make"], 12)])
        self.assertEqual(gate.parse_plan("## Exact Commands\n$ echo # expected\n"),
                         [(["echo", "#", "expected"], 0)])
        with self.assertRaises(KitError):
            gate.parse_plan("## Exact Commands\n$ echo 'unterminated\n")
        with self.assertRaises(KitError):
            gate.parse_plan("## Exact Commands\n$ \n")

    def test_no_section_gives_no_commands(self):
        self.assertEqual(gate.parse_plan("# Plan\n$ echo hi\n"), [])


class HandbackTests(HandbackBase):
    def test_handback_prints_ok_line_only_when_every_step_passes(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        match = OK_LINE.match(lines[-1])
        self.assertTrue(match, lines[-1])
        self.assertEqual(match.groups(), (BRANCH, self.sha))
        self.assertEqual([ln for ln in lines if ln.startswith("OK: ")], [lines[-1]])
        self.assertEqual(lines[-3:-1], ["stale: 1 record(s)", f"program/evidence/{TASK}/results.json"])
        results = read_json(self.wt / "program" / "evidence" / TASK / "results.json")
        self.assertEqual((results["head"], results["task_id"]), (self.sha, TASK))
        self.assertTrue(self.fake_log.exists())
        self.assert_no_trial_left()

    def test_handback_stops_at_first_failure_with_tail(self):
        script = "print('\\n'.join('line %d' % i for i in range(30))); raise SystemExit(2)"
        self.write_checks(test=[[PY, "-c", script]])
        commit(self.root, "failing test command")
        sh(self.wt, "git", "merge", "-q", "main")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        lines = out.splitlines()
        self.assertEqual(lines[-21], f"FAILED: test {PY} -c {script} (exit 2)")
        self.assertEqual(lines[-20:], [f"{TAIL}line {i}" for i in range(10, 30)])
        self.assertFalse([ln for ln in lines if ln.startswith("OK: ")])
        self.assertFalse(self.fake_log.exists(), "scan ran after test failed")
        self.assertFalse((self.wt / "program" / "evidence" / TASK / "results.json").exists())
        self.assert_no_trial_left()

    def test_handback_fails_on_merge_conflict(self):
        put(self.wt, "notes.txt", "branch\n")
        commit(self.wt, "branch edit")
        put(self.root, "notes.txt", "main\n")
        commit(self.root, "main edit")
        with self.assertRaises(KitError) as ctx:
            self.handback()
        self.assertIn("notes.txt", str(ctx.exception))
        self.assertIn("conflict", str(ctx.exception))
        self.assert_no_trial_left()
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")

    def test_missing_scanner_fails_gate(self):
        (self.bin / "gitleaks").unlink()
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines()[-1], "FAILED: scan missing tool: gitleaks")
        self.assertNotIn("OK: ", out)
        self.assert_no_trial_left()

    def test_trial_worktree_removed_when_a_step_fails(self):
        seen = []
        real = tempfile.mkdtemp

        def spy(*args, **kwargs):
            path = real(*args, **kwargs)
            seen.append(Path(path))
            return path

        self.write_checks(build=[[PY, "-c", "raise SystemExit(5)"]])
        commit(self.root, "failing build")
        sh(self.wt, "git", "merge", "-q", "main")
        with mock.patch.object(gate.tempfile, "mkdtemp", spy):
            rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: build ", out)
        trials = [p for p in seen if p.name.startswith("pk-trial-")]
        self.assertEqual(len(trials), 1)
        self.assertFalse(trials[0].exists())
        self.assert_no_trial_left()

    def test_trial_worktree_removed_when_the_gate_raises(self):
        with mock.patch.object(gate, "_run_steps", side_effect=KitError("boom")):
            with self.assertRaises(KitError):
                self.handback()
        self.assert_no_trial_left()

    def test_empty_scan_list_is_a_kit_error(self):
        self.write_checks(scan=[])
        commit(self.root, "empty scan")
        with self.assertRaises(KitError) as ctx:
            self.handback()
        self.assertEqual(str(ctx.exception), "checks.yaml: scan must list at least one command")

    def test_compile_check_and_extra_gate_run_in_order(self):
        marker = self.root.parent / "order.log"
        script = "import sys; open(%r, 'a').write(sys.argv[1] + '\\n')" % str(marker)
        self.write_checks(compile_check=[[PY, "-c", script, "compile"]],
                          extra_gate=[[PY, "-c", script, "extra1"], [PY, "-c", script, "extra2"]],
                          test=[[PY, "-c", script, "test"]],
                          build=[[PY, "-c", script, "build"]],
                          scan=[[PY, "-c", script, "scan"]])
        commit(self.root, "ordered commands")
        sh(self.wt, "git", "merge", "-q", "main")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertEqual(marker.read_text().split(), ["build", "compile", "test", "scan", "extra1", "extra2"])

    def test_plan_command_with_unexpected_exit_fails(self):
        self.set_plan("## Exact Commands\n$ python3 -c \"import sys; sys.exit(4)\"\n")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn('FAILED: plan python3 -c import sys; sys.exit(4) (exit 4)', out)

    def test_plan_tool_missing_is_reported(self):
        self.set_plan("## Exact Commands\n$ no-such-tool --x\n")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]), (1, "FAILED: plan missing tool: no-such-tool"))

    def test_zero_plan_commands_fails(self):
        for text in ("# Plan\n$ echo hi\n", "## Exact Commands\n\nnothing here\n"):
            with self.subTest(text=text):
                self.set_plan(text)
                rc, out = self.handback()
                self.assertEqual((rc, out.splitlines()[-1]),
                                 (1, 'FAILED: plan no commands under "## Exact Commands"'))

    def test_plan_missing_on_main_fails(self):
        self.set_plan(None)
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]),
                         (1, f"FAILED: plan program/tasks/{TASK}/PLAN.md differs from main"))

    def test_branch_editing_its_own_plan_fails_and_identical_plan_passes(self):
        path = self.wt / "program" / "tasks" / TASK / "PLAN.md"
        path.write_text(PLAN.replace("sys.exit(3)", "sys.exit(0)").replace(" # expect 3", ""))
        commit(self.wt, "easier plan")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]),
                         (1, f"FAILED: plan program/tasks/{TASK}/PLAN.md differs from main"))
        path.write_text(PLAN)
        commit(self.wt, "back to the plan on main")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_plan_missing_on_the_branch_fails(self):
        (self.wt / "program" / "tasks" / TASK / "PLAN.md").unlink()
        commit(self.wt, "drop plan")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]),
                         (1, f"FAILED: plan program/tasks/{TASK}/PLAN.md differs from main"))

    def test_missing_produce_script_fails(self):
        (self.wt / "program" / "evidence" / TASK / "produce.py").unlink()
        commit(self.wt, "drop produce")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]),
                         (1, f"FAILED: evidence program/evidence/{TASK}/produce.py is missing"))

    def test_results_with_a_survivor_fails(self):
        os.environ["PK_SURVIVOR"] = "1"
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: evidence", out)
        self.assertIn("planted_bugs_all_killed", out)
        self.assertNotIn("OK: ", out)
        self.assert_no_trial_left()

    def test_results_with_no_commands_fails(self):
        os.environ["PK_NO_COMMANDS"] = "1"
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: evidence", out)
        self.assertIn("commands", out)

    def test_results_for_another_head_or_task_fails(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        data = read_json(self.wt / "program" / "evidence" / TASK / "results.json")
        self.assertEqual(gate._results_problems(data, TASK, self.sha), [])
        self.assertTrue(any(p.startswith("task_id") for p in gate._results_problems(data, "P9-M01-S01-T01", self.sha)))
        self.assertTrue(any(p.startswith("head") for p in gate._results_problems(data, TASK, "0" * 40)))
        self.assertTrue(gate._results_problems([], TASK, self.sha))

    def test_stray_file_after_evidence_fails_naming_it(self):
        os.environ["PK_STRAY"] = "stray.txt"
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: evidence unexpected change: stray.txt", out)
        self.assertNotIn("OK: ", out)

    def test_unreadable_stale_record_fails(self):
        put(self.wt, "program/evidence/OTHER/results.json", "{ not json")
        commit(self.wt, "broken record")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        line = [ln for ln in out.splitlines() if ln.startswith("FAILED: stale unreadable record ")]
        self.assertEqual(len(line), 1, out)
        self.assertIn("program/evidence/OTHER/results.json: ", line[0])
        self.assertNotIn("OK: ", out)
        self.assert_no_trial_left()

    def test_guarded_file_change_refused_and_allowed_with_spec_line(self):
        put(self.wt, "pyproject.toml", "[project]\nname = 'x'\n")
        commit(self.wt, "touch guarded")
        rc, out = self.handback()
        self.assertEqual((rc, out.strip()), (1, "FAILED: guarded files changed: pyproject.toml"))
        self.assert_no_trial_left()
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "# Spec\n\nGuarded edit: pyproject.toml\n")
        commit(self.root, "spec allows it")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertTrue(OK_LINE.match(out.splitlines()[-1]))

    def test_every_guarded_path_needs_its_own_spec_line(self):
        put(self.wt, "pyproject.toml", "x\n")
        put(self.wt, "program/checks.yaml", "build: []\n")
        commit(self.wt, "touch two")
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "Guarded edit: pyproject.toml\n")
        commit(self.root, "spec allows one")
        rc, out = self.handback()
        self.assertEqual((rc, out.strip()), (1, "FAILED: guarded files changed: program/checks.yaml"))

    def test_spec_line_on_the_branch_alone_does_not_allow_it(self):
        put(self.wt, "pyproject.toml", "x\n")
        put(self.wt, f"program/tasks/{TASK}/SPEC.md", "Guarded edit: pyproject.toml\n")
        commit(self.wt, "self-approved")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: guarded files changed: pyproject.toml", out)

    def test_modified_one_character_tracked_file_fails(self):
        os.environ["PK_TOUCH"] = "a"
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: evidence unexpected change: a", out.splitlines())
        self.assert_no_trial_left()

    def test_rerun_after_committing_results_passes(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        commit(self.wt, "commit the evidence")
        second = sh(self.wt, "git", "rev-parse", "HEAD").strip()
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[-1], f"OK: {BRANCH} passes the hand-back gate at {second}")
        self.assertEqual(read_json(self.wt / "program" / "evidence" / TASK / "results.json")["head"], second)

    def test_changes_are_reported_raw_with_spaces_and_renames(self):
        put(self.wt, "a", "modified\n")
        put(self.wt, "dir with space/f g.txt", "x\n")
        sh(self.wt, "git", "mv", "notes.txt", "moved.txt")
        self.assertEqual(sorted(gate._changes(self.wt)),
                         ["a", "dir with space/f g.txt", "moved.txt (renamed from notes.txt)"])
        self.assertEqual(sorted(gate._unexpected_changes(self.wt, "a")),
                         ["dir with space/f g.txt", "moved.txt (renamed from notes.txt)"])
        sh(self.wt, "git", "reset", "-q", "--hard")
        sh(self.wt, "git", "clean", "-fdq")
        self.assertEqual(gate._changes(self.wt), [])

    def test_stray_path_with_a_space_is_named_exactly(self):
        os.environ["PK_STRAY"] = "dir with space/f g.txt"
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: evidence unexpected change: dir with space/f g.txt", out.splitlines())

    def test_failing_command_cannot_forge_the_ok_line(self):
        forged = f"OK: {BRANCH} passes the hand-back gate at {'a' * 40}"
        script = f"print({forged!r}); raise SystemExit(1)"
        self.write_checks(test=[[PY, "-c", script]])
        commit(self.root, "forging test command")
        sh(self.wt, "git", "merge", "-q", "main")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines()[-1], TAIL + forged)
        self.assertFalse([ln for ln in out.splitlines() if ln.startswith("OK: ")])

    def test_results_committed_earlier_with_a_no_op_produce_fails(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        commit(self.wt, "commit the evidence")
        put(self.wt, f"program/evidence/{TASK}/produce.py", "# produces nothing\n")
        commit(self.wt, "no-op produce")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: evidence", out.splitlines())
        self.assertTrue(any(ln.startswith("head: ") for ln in out.splitlines()), out)

    def test_tag_named_like_the_branch_changes_nothing(self):
        sh(self.root, "git", "tag", BRANCH, "main")  # an ambiguous name pointing at main
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[-1], f"OK: {BRANCH} passes the hand-back gate at {self.sha}")

    def test_checks_come_from_main_not_the_working_tree(self):
        self.write_checks(scan=[], build=[[PY, "-c", "raise SystemExit(9)"]])  # uncommitted edit in the main checkout
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.fake_log.exists(), "the committed scan command ran")

    def test_empty_compile_check_and_extra_gate_print_no_step_line(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn("step build: ok", out)
        self.assertNotIn("step compile_check", out)
        self.assertNotIn("step extra_gate", out)

    def test_guarded_directory_entry_and_normalised_entries(self):
        self.write_checks(guarded_files=["./pyproject.toml", "deps/", ".//vendor/"])
        commit(self.root, "guard more")
        sh(self.wt, "git", "merge", "-q", "main")
        put(self.wt, "pyproject.toml", "x\n")
        put(self.wt, "deps/sub/x.txt", "x\n")
        put(self.wt, "depsx.txt", "not guarded\n")
        commit(self.wt, "touch guarded")
        rc, out = self.handback()
        self.assertEqual((rc, out.strip()), (1, "FAILED: guarded files changed: deps/sub/x.txt, pyproject.toml"))
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "Guarded edit: ./pyproject.toml\nGuarded edit: deps/sub/x.txt\n")
        commit(self.root, "allow both")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_guarded_directory_can_be_allowed_whole(self):
        self.write_checks(guarded_files=["deps/"])
        commit(self.root, "guard deps")
        sh(self.wt, "git", "merge", "-q", "main")
        put(self.wt, "deps/a.txt", "x\n")
        put(self.wt, "deps/b.txt", "x\n")
        commit(self.wt, "touch deps")
        rc, out = self.handback()
        self.assertEqual((rc, out.strip()), (1, "FAILED: guarded files changed: deps/a.txt, deps/b.txt"))
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "Guarded edit: deps/\n")
        commit(self.root, "allow the directory")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_worktree_must_exist_be_on_the_branch_and_be_clean(self):
        with self.assertRaises(KitError):
            self.handback(worktree=self.root / "nowhere")
        sh(self.wt, "git", "switch", "-q", "-c", "other")
        with self.assertRaises(KitError) as ctx:
            self.handback()
        self.assertIn(BRANCH, str(ctx.exception))
        sh(self.wt, "git", "switch", "-q", BRANCH)
        put(self.wt, "scratch.txt", "dirty\n")
        with self.assertRaises(KitError) as ctx:
            self.handback()
        self.assertIn("not clean", str(ctx.exception))
        (self.wt / "scratch.txt").unlink()
        self.assert_no_trial_left()

    def test_worktree_option_overrides_default_location(self):
        moved = self.root.parent / "elsewhere"
        sh(self.root, "git", "worktree", "move", str(self.wt), str(moved))
        rc, out = self.handback(worktree=moved)
        self.assertEqual(rc, 0, out)

    def test_bad_task_id_and_branch_are_refused(self):
        for task, branch in (("../x", BRANCH), (TASK, "-x"), (TASK, "no-such-branch")):
            with self.subTest(task=task, branch=branch), self.assertRaises(KitError):
                run_quiet(gate.handback, self.root, task, branch)


class MergeCheckTests(HandbackBase):
    def test_merge_check_prints_ok_line_with_branch_head(self):
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[-1], f"OK: {BRANCH} merges into main at {self.sha}")
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("OK: ")], [out.splitlines()[-1]])
        self.assertFalse(self.fake_log.exists(), "merge-check does not scan")
        self.assert_no_trial_left()
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")

    def test_merge_check_fails_on_conflict_and_on_a_failing_test(self):
        put(self.wt, "notes.txt", "branch\n")
        commit(self.wt, "branch edit")
        put(self.root, "notes.txt", "main\n")
        commit(self.root, "main edit")
        with self.assertRaises(KitError):
            self.merge_check()
        self.assert_no_trial_left()

    def test_merge_check_failing_test_stops_without_ok_line(self):
        put(self.wt, "test_more.py", "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_no(self):\n        self.fail('nope')\n")
        commit(self.wt, "failing test")
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: test ", out)
        self.assertNotIn("OK: ", out)
        self.assert_no_trial_left()

    def test_merge_check_does_not_need_scan_commands(self):
        self.write_checks(scan=[])
        commit(self.root, "no scan")
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)


class TrialMergeTests(HandbackBase):
    def test_trial_merge_leaves_merge_uncommitted_and_main_untouched(self):
        head_before = sh(self.root, "git", "rev-parse", "main").strip()
        trial = gate.trial_merge(self.root, BRANCH)
        try:
            self.assertTrue(trial.name.startswith("pk-trial-"))
            self.assertIn("mul", (trial / "calc.py").read_text())
            self.assertEqual(sh(trial, "git", "rev-parse", "HEAD").strip(), head_before)
            self.assertTrue((Path(sh(trial, "git", "rev-parse", "--git-dir").strip()) / "MERGE_HEAD").exists())
        finally:
            sh(self.root, "git", "worktree", "remove", "--force", str(trial))
        self.assertEqual(sh(self.root, "git", "rev-parse", "main").strip(), head_before)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")

    def test_trial_merge_uses_the_branch_not_a_tag_of_the_same_name(self):
        sh(self.root, "git", "tag", BRANCH, "main")
        trial = gate.trial_merge(self.root, BRANCH)
        try:
            self.assertIn("mul", (trial / "calc.py").read_text())
        finally:
            sh(self.root, "git", "worktree", "remove", "--force", str(trial))

    def test_trial_worktree_is_removed_when_interrupted_during_the_merge(self):
        with mock.patch.object(gate, "run_argv", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                gate.trial_merge(self.root, BRANCH)
        self.assert_no_trial_left()

    def test_trial_merge_conflict_lists_paths_and_cleans_up(self):
        put(self.wt, "notes.txt", "branch\n")
        put(self.wt, "other.txt", "branch\n")
        commit(self.wt, "branch edit")
        put(self.root, "notes.txt", "main\n")
        put(self.root, "other.txt", "main\n")
        commit(self.root, "main edit")
        with self.assertRaises(KitError) as ctx:
            gate.trial_merge(self.root, BRANCH)
        self.assertIn("notes.txt", str(ctx.exception))
        self.assertIn("other.txt", str(ctx.exception))
        self.assert_no_trial_left()


# --- claims --------------------------------------------------------------------------

REC_DEV = "program/evidence/P0-M01-S01-T01/results.json"
REC_TWO = "program/evidence/P0-M01-S01-T02/results.json"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ClaimsBase(GitBase):
    def setUp(self):
        super().setUp()
        put(self.root, "README.md", "x\n")
        write_yaml(self.root / "program" / "environments.yaml", {"environments": {
            "dev": {"exists": True, "owner": "team", "notes": "local"},
            "staging": {"exists": True, "owner": "team", "notes": "shared"},
            "prod": {"exists": False, "owner": "ops", "notes": "not built"}}})
        self.set_gates({"G-P0": {"title": "Phase 0", "claims": ["it works"], "environment": "dev",
                                 "records": [{"path": REC_DEV, "acceptance": "all pass"},
                                             {"path": REC_TWO, "acceptance": "all pass"}]},
                        "G-LIVE": {"title": "Live", "claims": ["serves traffic"], "environment": "prod",
                                   "records": [{"path": REC_DEV, "acceptance": "all pass"}]}})
        put(self.root, REC_DEV, '{"task_id": "one"}\n')
        put(self.root, REC_TWO, '{"task_id": "two"}\n')
        commit(self.root, "init")

    def set_gates(self, gates):
        write_yaml(self.root / "program" / "gates.yaml", {"gates": gates})

    def accept(self, record=REC_DEV, **over):
        path = (self.root / record).parent / "acceptance.json"
        data = {"task_id": "x", "decision": "accepted_development", "evidence_sha256": digest(self.root / record),
                "source_commit": "a" * 40, "accepted_at": "2026-10-06T00:00:00Z", "role": "qa", "commands": [],
                "planted_bugs": [], "findings": [], "blocked_by": []}
        data.update(over)
        path.write_text(json.dumps(data))
        return path

    def claim(self, gate_id, record):
        name = record.replace("/", "__")
        return read_json(self.root / "program" / "claims" / gate_id / name)


class ClaimsTests(ClaimsBase):
    def test_claims_never_evidence_missing_environment(self):
        acceptance = self.accept()
        written = gate.claims(self.root, "G-LIVE")
        self.assertEqual([p.name for p in written], ["program__evidence__P0-M01-S01-T01__results.json"])
        doc = self.claim("G-LIVE", REC_DEV)
        self.assertEqual(doc["environment"], {"dev": "not_evaluated", "staging": "not_evaluated",
                                              "prod": "not_evaluated"})
        self.assertEqual(doc["acceptance_sha256"], digest(acceptance))
        self.assertEqual((doc["gate"], doc["record"]), ("G-LIVE", REC_DEV))
        self.assertEqual(doc["generated_from_commit"], sh(self.root, "git", "rev-parse", "HEAD").strip())
        self.accept(blocked_by=["prod"])  # a missing environment is not even "blocked"
        gate.claims(self.root, "G-LIVE")
        self.assertEqual(self.claim("G-LIVE", REC_DEV)["environment"]["prod"], "not_evaluated")
        self.accept(decision="accepted_development", blocked_by=[])
        gate.claims(self.root, "G-LIVE")
        self.assertNotIn("evidenced", self.claim("G-LIVE", REC_DEV)["environment"].values())

    def test_gate_environment_is_evidenced_only_with_a_matching_accepted_acceptance(self):
        self.accept()
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"],
                         {"dev": "evidenced", "staging": "not_evaluated", "prod": "not_evaluated"})
        doc_two = self.claim("G-P0", REC_TWO)  # no acceptance file for the second record
        self.assertEqual((doc_two["environment"]["dev"], doc_two["acceptance_sha256"]), ("not_evaluated", None))
        cases = {"rejected": {"decision": "rejected"}, "stale hash": {"evidence_sha256": "0" * 64},
                 "blocked": {"blocked_by": ["dev"]}, "bad blocked_by": {"blocked_by": "dev"}}
        for name, over in cases.items():
            with self.subTest(case=name):
                self.accept(**over)
                gate.claims(self.root, "G-P0")
                state = self.claim("G-P0", REC_DEV)["environment"]["dev"]
                self.assertEqual(state, "blocked" if name == "blocked" else "not_evaluated")
        (self.root / "program" / "evidence" / "P0-M01-S01-T01" / "acceptance.json").write_text("{ nope")
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"]["dev"], "not_evaluated")

    def test_changed_record_withdraws_the_evidenced_state(self):
        self.accept()
        (self.root / REC_DEV).write_text('{"task_id": "changed"}\n')
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"]["dev"], "not_evaluated")

    def test_stale_claim_files_are_removed_only_inside_the_gate_directory(self):
        gate.claims(self.root, "G-P0")
        directory = self.root / "program" / "claims" / "G-P0"
        (directory / "old__record.json").write_text("{}")
        outside = self.root / "program" / "claims" / "G-LIVE.json"
        outside.write_text("keep")
        written = gate.claims(self.root, "G-P0")
        self.assertEqual(sorted(p.name for p in directory.iterdir()), sorted(p.name for p in written))
        self.assertEqual(outside.read_text(), "keep")

    def test_unknown_gate_and_bad_environment_are_kit_errors(self):
        for function in (gate.claims, gate.validate_claims):
            with self.subTest(function=function.__name__):
                with self.assertRaises(KitError):
                    function(self.root, "G-NOPE")
                with self.assertRaises(KitError):
                    function(self.root, "../x")
        self.set_gates({"G-X": {"title": "t", "claims": [], "environment": "qa",
                                "records": [{"path": REC_DEV, "acceptance": "a"}]}})
        with self.assertRaises(KitError) as ctx:
            gate.claims(self.root, "G-X")
        self.assertIn("qa", str(ctx.exception))

    def test_gate_with_no_records_writes_nothing_and_clears_the_directory(self):
        self.set_gates({"G-EMPTY": {"title": "t", "claims": [], "environment": "dev", "records": []}})
        self.assertEqual(gate.claims(self.root, "G-EMPTY"), [])
        self.assertEqual(gate.validate_claims(self.root, "G-EMPTY"), [])
        directory = self.root / "program" / "claims" / "G-EMPTY"
        directory.mkdir(parents=True)
        (directory / "old.json").write_text("{}")
        self.assertEqual(gate.claims(self.root, "G-EMPTY"), [])
        self.assertEqual(list(directory.iterdir()), [])

    def test_symlinked_claims_directory_is_refused(self):
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        (self.root / "program" / "claims").mkdir()
        (self.root / "program" / "claims" / "G-P0").symlink_to(elsewhere, target_is_directory=True)
        for function in (gate.claims, gate.validate_claims):
            with self.subTest(function=function.__name__), self.assertRaises(KitError) as ctx:
                function(self.root, "G-P0")
            self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_record_path_must_stay_inside_the_project(self):
        self.set_gates({"G-X": {"title": "t", "claims": [], "environment": "dev",
                                "records": [{"path": "../outside/results.json", "acceptance": "a"}]}})
        with self.assertRaises(KitError):
            gate.claims(self.root, "G-X")

    def test_validate_claims_detects_drift(self):
        self.accept()
        gate.claims(self.root, "G-P0")
        self.assertEqual(gate.validate_claims(self.root, "G-P0"), [])
        commit(self.root, "claims")  # a new HEAD is not drift
        self.assertEqual(gate.validate_claims(self.root, "G-P0"), [])
        directory = self.root / "program" / "claims" / "G-P0"
        doc_path = directory / "program__evidence__P0-M01-S01-T01__results.json"
        doc = read_json(doc_path)
        doc["environment"]["prod"] = "evidenced"
        doc_path.write_text(json.dumps(doc))
        (directory / "program__evidence__P0-M01-S01-T02__results.json").unlink()
        (directory / "extra.json").write_text("{}")
        problems = gate.validate_claims(self.root, "G-P0")
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("T01__results.json" in p and "environment" in p for p in problems), problems)
        self.assertTrue(any("T02__results.json" in p and "missing" in p for p in problems), problems)
        self.assertTrue(any("extra.json" in p and "extra" in p for p in problems), problems)

    def test_validate_claims_notices_acceptance_changes_and_bad_json(self):
        self.accept()
        gate.claims(self.root, "G-P0")
        self.accept(findings=[{"n": 1, "severity": "low", "location": "x", "fix": "y"}])
        problems = gate.validate_claims(self.root, "G-P0")
        self.assertEqual(len(problems), 1)
        self.assertIn("acceptance_sha256", problems[0])
        gate.claims(self.root, "G-P0")
        (self.root / "program" / "claims" / "G-P0" / "program__evidence__P0-M01-S01-T02__results.json"
         ).write_text("{ nope")
        problems = gate.validate_claims(self.root, "G-P0")
        self.assertEqual(len(problems), 1)
        self.assertIn("T02__results.json", problems[0])

    def test_validate_without_a_claims_directory_reports_every_file_missing(self):
        problems = gate.validate_claims(self.root, "G-P0")
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("missing" in p for p in problems))

    def test_record_name_keeps_json_suffix_once_and_rejects_collisions(self):
        self.set_gates({"G-X": {"title": "t", "claims": [], "environment": "dev",
                                "records": [{"path": "program/evidence/a.txt", "acceptance": "a"}]}})
        written = gate.claims(self.root, "G-X")
        self.assertEqual([p.name for p in written], ["program__evidence__a.txt.json"])
        self.set_gates({"G-X": {"title": "t", "claims": [], "environment": "dev",
                                "records": [{"path": "a/b.json", "acceptance": "a"},
                                            {"path": "a__b.json", "acceptance": "a"}]}})
        with self.assertRaises(KitError):
            gate.claims(self.root, "G-X")


class CommandTests(ClaimsBase):
    def run_pk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_claims_and_validate_commands(self):
        self.assertEqual(self.run_pk("claims", "G-P0"), (0, "wrote 2 claim documents\n", ""))
        self.assertEqual(self.run_pk("validate", "G-P0"), (0, "claims consistent for G-P0\n", ""))
        (self.root / "program" / "claims" / "G-P0" / "extra.json").write_text("{}")
        rc, out, err = self.run_pk("validate", "G-P0")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "extra.json" in err, err)

    def test_unknown_gate_is_one_error_line(self):
        rc, out, err = self.run_pk("claims", "G-NOPE")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)

    def test_handback_and_merge_check_need_a_branch(self):
        for argv in (("handback", TASK), ("merge-check",)):
            with self.subTest(argv=argv):
                rc, out, err = self.run_pk(*argv)
                self.assertEqual((rc, out), (1, ""))
                self.assertTrue(err.startswith("error: "), err)

    def test_gate_without_an_action_is_an_error(self):
        rc, _, err = self.run_pk()
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: "), err)


class CommandRunTests(HandbackBase):
    def test_cli_handback_and_merge_check_print_ok_lines(self):
        for argv, expect in ((("handback", TASK, "--branch", BRANCH), f"OK: {BRANCH} passes the hand-back gate at "),
                             (("merge-check", "--branch", BRANCH), f"OK: {BRANCH} merges into main at ")):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with mock.patch.object(gate, "find_root", return_value=self.root), \
                        contextlib.redirect_stdout(out):
                    rc = pk.main(["gate", *argv])
                self.assertEqual(rc, 0, out.getvalue())
                self.assertEqual(out.getvalue().splitlines()[-1], expect + self.sha)


class BindingTests(HandbackBase):
    RESULTS = f"program/evidence/{TASK}/results.json"

    def bind(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            problems = gate.binding(self.root, TASK, BRANCH)
        return problems, out.getvalue()

    def produce_and_commit(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        return commit(self.wt, "commit the evidence")

    def test_binding_holds_for_evidence_and_status_commits(self):
        self.produce_and_commit()
        problems, out = self.bind()
        self.assertEqual((problems, out), ([], f"binding holds: results.json at {self.sha} bound, 1 later "
                                               "commit(s) touch only evidence and status\n"))
        put(self.wt, f"program/evidence/{TASK}/acceptance.json", "{}\n")
        commit(self.wt, "acceptance")
        put(self.wt, f"program/status/{TASK}.json", "{}\n")
        commit(self.wt, "status")
        problems, out = self.bind()
        self.assertEqual(problems, [])
        self.assertIn("3 later commit(s)", out)

    def test_binding_ignores_the_working_tree(self):
        self.produce_and_commit()
        (self.wt / self.RESULTS).write_text("{ nope")  # an uncommitted edit changes nothing
        self.assertEqual(self.bind()[0], [])

    def test_binding_fails_when_a_later_commit_touches_source(self):
        self.produce_and_commit()
        put(self.wt, "calc.py", CALC + "\n# later edit\n")
        put(self.wt, f"program/status/{TASK}.json", "{}\n")
        put(self.wt, "program/status/OTHER.json", "{}\n")
        commit(self.wt, "source after the evidence")
        problems, out = self.bind()
        self.assertEqual(out, "")
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(problems[0].startswith("calc.py: changed after "), problems)
        self.assertTrue(problems[1].startswith("program/status/OTHER.json: "), problems)

    def test_binding_fails_when_head_is_not_an_ancestor(self):
        self.produce_and_commit()
        put(self.root, "later.txt", "x\n")
        elsewhere = commit(self.root, "main moves on")
        data = read_json(self.wt / self.RESULTS)
        data["head"] = elsewhere
        (self.wt / self.RESULTS).write_text(json.dumps(data))
        commit(self.wt, "results for a foreign head")
        problems, out = self.bind()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not an ancestor", problems[0])

    def test_binding_fails_when_results_are_absent(self):
        problems, out = self.bind()
        self.assertEqual(problems, [f"{self.RESULTS} is absent on {BRANCH}"])

    def test_binding_fails_for_a_record_that_does_not_validate(self):
        os.environ["PK_SURVIVOR"] = "1"
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        commit(self.wt, "survivor evidence")
        problems, out = self.bind()
        self.assertTrue(any("planted_bugs_all_killed" in p for p in problems), problems)

    def test_binding_command(self):
        self.produce_and_commit()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", "binding", TASK, "--branch", BRANCH])
        self.assertEqual((rc, err.getvalue()), (0, ""))
        self.assertTrue(out.getvalue().startswith(f"binding holds: results.json at {self.sha} bound"))
        put(self.wt, "calc.py", CALC + "\n# later\n")
        commit(self.wt, "source")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", "binding", TASK, "--branch", BRANCH])
        self.assertEqual((rc, out.getvalue()), (1, ""))
        self.assertTrue(err.getvalue().startswith("error: calc.py: "), err.getvalue())


if __name__ == "__main__":
    unittest.main()
