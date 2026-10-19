import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import ast
import contextlib
import hashlib
import inspect
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
import store
from common import KitError, read_json, read_yaml, sha256_file, write_yaml

PY = os.path.realpath(sys.executable)
GIT = shutil.which("git")
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PRESETS = Path(__file__).resolve().parent.parent / "presets"
TASK = "P0-M01-S01-T01"
BRANCH = f"task/{TASK}"  # the name `pk worktree new` gives the branch
POINTER = f"program/evidence/{TASK}/latest.json"  # the task's pointer, the only mutable evidence file
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
REVIEW = "# Review\n\n| 1 | SPEC names no failure mode | important | fixed |\n"
PRODUCE = '''import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["PK_SCRIPTS"])
import evidence

root = Path.cwd()
task = os.environ.get("PK_TASK", "P0-M01-S01-T01")
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
ev.write()
if os.environ.get("PK_STRAY"):
    stray = root / os.environ["PK_STRAY"]
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("left behind\\n")
if os.environ.get("PK_TOUCH"):
    (root / os.environ["PK_TOUCH"]).write_text("changed by produce\\n")
'''
HAND_MADE = '''import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.environ["PK_SCRIPTS"])
import store

head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
empty = hashlib.sha256(b"").hexdigest()
row = {"builds": True, "killed": True, "kill_reason": "test failed", "test_output_sha256": empty,
       "path": "calc.py", "find": "a + b", "replace": "a - b", "build_argv": ["python3", "-c", "pass"],
       "test_argv": ["python3", "-c", "raise SystemExit(1)"]}
record = {"task_id": "P0-M01-S01-T01", "head": head, "main": head, "merge_base": head,
          "produced_at": "2026-01-01T00:00:00Z", "inputs": {"calc.py": empty},
          "commands": [{"label": "unit", "argv": ["python3", "-c", "pass"], "exit": 0, "expected_exit": 0,
                        "as_expected": True, "derived": {}, "output_sha256": empty, "output_tail": ""}],
          "planted_bugs": [dict(row, id="made-up"),
                           dict(row, id="self-test-noncompiling", builds=False, killed=False,
                                kill_reason="build failed", test_output_sha256="")],
          "planted_bugs_all_killed": True, "all_commands_as_expected": True, "tree_unchanged_by_run": True,
          "blocked_by": [], "deviations": []}
store.record(Path.cwd(), "P0-M01-S01-T01", "results", record)
'''
FAILING_TESTS = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                 "    def test_one(self):\n        self.fail('one')\n\n"
                 "    def test_two(self):\n        self.fail('two')\n")


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
        put(self.root, f"program/tasks/{TASK}/review.md", REVIEW)  # the dispatch commit carries the spec-QA record
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

    def latest(self, kind="results", where=None):
        """(index entry, record) of the task's latest record in the worktree's index (or `where`'s)."""
        return store.load_latest(where or self.wt, TASK, kind)

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

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", *argv])
        return rc, out.getvalue(), err.getvalue()

    def assert_no_forged_ok(self, *streams):
        for text in streams:
            self.assertEqual([ln for ln in text.splitlines() if ln.startswith("OK: ")], [], text)

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
    KEYS = ("build", "test", "scan", "compile_check", "extra_gate", "parallel_steps", "guarded_files", "tiers")
    EXPECT = {
        "go": {"build": [["go", "build", "./..."]], "compile_check": [["go", "vet", "./..."]],
               "test": [["go", "test", "./..."]],
               "scan": [["gosec", "./..."], ["gitleaks", "detect", "--no-git"]],
               "guarded": ["go.mod", "go.sum", "program/checks.yaml", "program/release-set.yaml"]},
        "node": {"build": [["pnpm", "install", "--frozen-lockfile", "--offline"], ["pnpm", "build"]],
                 "compile_check": [], "test": [["pnpm", "test"]],
                 "scan": [["gitleaks", "detect", "--no-git"]],
                 "guarded": ["package.json", "pnpm-lock.yaml", "program/checks.yaml",
                             "program/release-set.yaml"]},
        "python": {"build": [["python3", "-m", "compileall", "-q", "."]], "compile_check": [],
                   "test": [["python3", "-m", "unittest", "discover"]],
                   "scan": [["python3", "-m", "pip", "check"], ["gitleaks", "detect", "--no-git"]],
                   "guarded": ["pyproject.toml", "requirements.txt", "program/checks.yaml",
                             "program/release-set.yaml"]},
    }

    def test_presets_parse_and_have_all_keys(self):
        for name, expect in self.EXPECT.items():
            with self.subTest(preset=name):
                data = read_yaml(PRESETS / f"checks.{name}.yaml")
                self.assertEqual(set(data), set(self.KEYS))
                for key in ("build", "test", "scan"):
                    self.assertTrue(data[key], key)
                self.assertEqual(data["extra_gate"], [])
                self.assertIs(data["parallel_steps"], False)
                self.assertEqual(data["guarded_files"], expect["guarded"])
                self.assertEqual(data["test"], expect["test"])
                self.assertEqual(data["scan"], expect["scan"])
                self.assertEqual(data["build"], expect["build"])
                self.assertEqual(data["compile_check"], expect["compile_check"])

    def test_no_preset_gate_step_needs_the_network(self):
        for name in self.EXPECT:
            with self.subTest(preset=name):
                data = read_yaml(PRESETS / f"checks.{name}.yaml")
                commands = [argv for key in ("build", "test", "scan", "compile_check", "extra_gate")
                            for argv in data[key]]
                self.assertEqual([argv for argv in commands if "audit" in argv], [])
        text = (PRESETS / "checks.node.yaml").read_text()
        notes = [ln for ln in text.splitlines() if ln.startswith("#") and "audit" in ln]
        self.assertEqual(len(notes), 1, text)
        self.assertIn("outside the gate", notes[0])

    def test_presets_load_through_load_checks(self):
        for name in self.EXPECT:
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "program").mkdir()
                shutil.copy(PRESETS / f"checks.{name}.yaml", Path(tmp) / "program" / "checks.yaml")
                checks = gate.load_checks(Path(tmp), require=("build", "test", "scan"))
                self.assertEqual(checks["scan"][-1], ["gitleaks", "detect", "--no-git"])


# A step command that proves overlap: it writes its own marker, then waits until every marker exists.
RENDEZVOUS = ("import os, sys, time\n"
              "d, me = sys.argv[1], sys.argv[2]\n"
              "open(os.path.join(d, me), 'w').close()\n"
              "end = time.time() + 5\n"
              "while time.time() < end:\n"
              "    if all(os.path.exists(os.path.join(d, n)) for n in ('test', 'scan', 'extra_gate')):\n"
              "        print('met ' + me)\n"
              "        raise SystemExit(0)\n"
              "    time.sleep(0.02)\n"
              "print('never met ' + me)\n"
              "raise SystemExit(3)\n")


class ParallelStepsParseTests(GitBase):
    def put_raw(self, text):
        put(self.root, "program/checks.yaml", text)

    def test_default_is_false_and_true_is_read(self):
        self.put_raw("build:\n  - - make\n")
        self.assertIs(gate.load_checks(self.root)["parallel_steps"], False)
        self.put_raw("parallel_steps: true\n")
        self.assertIs(gate.load_checks(self.root)["parallel_steps"], True)
        self.put_raw("parallel_steps: false\n")
        self.assertIs(gate.load_checks(self.root)["parallel_steps"], False)

    def test_a_non_boolean_is_refused_by_name(self):
        for text in ("parallel_steps: yes please\n", "parallel_steps: 1\n", "parallel_steps: []\n",
                     'parallel_steps: "true"\n'):
            with self.subTest(text=text):
                self.put_raw(text)
                with self.assertRaises(KitError) as ctx:
                    gate.load_checks(self.root)
                self.assertEqual(str(ctx.exception), "checks.yaml: parallel_steps must be true or false")


@skip_unless_slow()
class ParallelStepsRunTests(HandbackBase):
    def set_checks(self, parallel, **over):
        self.write_checks(parallel_steps=parallel, **over)
        commit(self.root, "checks")
        sh(self.wt, "git", "merge", "-q", "main")

    def marker_dir(self):
        marks = self.tmpdir.parent / "marks"
        shutil.rmtree(marks, ignore_errors=True)
        marks.mkdir()
        return marks

    def rendezvous(self, marks):
        return {name: [[PY, "-c", RENDEZVOUS, str(marks), name]] for name in ("test", "scan", "extra_gate")}

    def test_the_three_steps_overlap_in_the_hand_back_gate_and_the_merge_check(self):
        marks = self.marker_dir()
        self.set_checks(True, **self.rendezvous(marks))
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        for name in ("test", "scan", "extra_gate"):
            self.assertIn(f"step {name}: ok", out)
        shutil.rmtree(marks)
        marks.mkdir()
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_serial_steps_do_not_overlap(self):
        marks = self.marker_dir()
        self.set_checks(False, **self.rendezvous(marks))
        rc, out = self.merge_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("never met test", out)

    def test_the_output_order_is_test_scan_extra_gate_whatever_finishes_first(self):
        slow = [[PY, "-c", "import time; time.sleep(0.6); print('slow test')"]]
        self.set_checks(True, test=slow, extra_gate=[[PY, "-c", "print('quick extra')"]])
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        lines = [ln for ln in out.splitlines() if ln.startswith("step ")]
        self.assertEqual(lines, ["step build: ok", "step test: ok", "step scan: ok", "step extra_gate: ok"])

    def serial_and_parallel(self, runner, **over):
        outs = []
        for parallel in (False, True):
            self.set_checks(parallel, **over)
            rc, out = runner()
            outs.append((rc, re.sub(r"\b[0-9a-f]{40}\b", "<sha>", out)))  # the branch tip moves with each merge of main
        return outs

    def test_a_failing_scan_with_a_passing_test_reports_exactly_as_the_serial_run(self):
        script = "print('\\n'.join('s%d' % i for i in range(30))); raise SystemExit(2)"
        over = {"scan": [[PY, "-c", script]], "extra_gate": [[PY, "-c", "raise SystemExit(5)"]]}
        for runner in (self.handback, self.merge_check):
            with self.subTest(runner=runner.__name__):
                serial, parallel = self.serial_and_parallel(runner, **over)
                self.assertEqual(serial[0], 1)
                self.assertEqual(parallel, serial)
                lines = parallel[1].splitlines()
                self.assertIn("step test: ok", lines)
                self.assertEqual(lines[-21], f"FAILED: scan {PY} -c {script} (exit 2)")
                self.assertEqual(lines[-20:], [f"{TAIL}s{i}" for i in range(10, 30)])
                self.assertNotIn("extra_gate", parallel[1])
                self.assert_no_trial_left()

    def test_a_failing_test_hides_the_later_steps_output(self):
        over = {"test": [[PY, "-c", "print('boom'); raise SystemExit(4)"]],
                "scan": [[PY, "-c", "raise SystemExit(6)"]]}
        serial, parallel = self.serial_and_parallel(self.merge_check, **over)
        self.assertEqual(parallel, serial)
        self.assertEqual(parallel[1].splitlines()[-2:], [f"FAILED: test {PY} -c print('boom'); raise SystemExit(4) (exit 4)",
                                                         f"{TAIL}boom"])

    def test_a_passing_run_prints_what_the_serial_run_prints(self):
        serial, parallel = self.serial_and_parallel(self.merge_check)
        self.assertEqual(parallel, serial)

    def test_a_missing_tool_in_a_parallel_step_is_reported_as_serial(self):
        (self.bin / "gitleaks").unlink()
        serial, parallel = self.serial_and_parallel(self.merge_check)
        self.assertEqual(parallel, serial)
        self.assertEqual(parallel[1].splitlines()[-1], "FAILED: scan missing tool: gitleaks")

    def test_the_light_tier_is_unchanged(self):
        self.set_checks(True)
        checks = gate.load_checks_at(self.root, "main")
        with mock.patch.object(gate, "_run_parallel") as parallel:
            self.assertEqual(gate._run_light(checks, self.root), 0)
        parallel.assert_not_called()


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


class OutputTests(unittest.TestCase):
    def test_show_escapes_every_line_break_and_control_character(self):
        for char in ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029", "\x00", "\x1b", "\x7f", "\t"):
            with self.subTest(char=repr(char)):
                shown = gate._show(f"a{char}b")
                self.assertEqual(len(shown.splitlines()), 1)
                self.assertTrue(shown.startswith("a\\") and shown.endswith("b"), shown)
        self.assertEqual(gate._show("plain text, ünïcode"), "plain text, ünïcode")
        self.assertEqual(gate._show("a\nb"), "a\\x0ab")

    def test_out_prefixes_lines_that_read_as_ok_lines_and_escapes_breaks(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            gate._out("OK: forged")
            gate._out("x\nOK: forged", sys.stderr)
        self.assertEqual(out.getvalue(), "| OK: forged\n")
        self.assertEqual(err.getvalue(), "x\\x0aOK: forged\n")

    def test_print_ok_prints_only_validated_values(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gate._print_ok("handback", "task/a", "a" * 40)
            gate._print_ok("merge", "task/a", "b" * 40)
        self.assertEqual(out.getvalue(), f"OK: task/a passes the hand-back gate at {'a' * 40}\n"
                                         f"OK: task/a merges into main at {'b' * 40}\n")
        for branch, sha in (("a\nb", "a" * 40), ("a", "A" * 40), ("a", "a" * 39), ("-a", "a" * 40), ("a", "a" * 40 + "\n")):
            with self.subTest(branch=branch, sha=sha), self.assertRaises(KitError):
                gate._print_ok("handback", branch, sha)

    def test_only_the_output_functions_write_to_the_streams(self):
        tree = ast.parse((SCRIPTS / "gate.py").read_text())
        offenders = []
        allowed = {"_out", "_print_ok"}

        def visit(node, inside):
            if isinstance(node, ast.FunctionDef):
                inside = node.name
            if isinstance(node, ast.Call):
                target = node.func
                writes = (isinstance(target, ast.Name) and target.id == "print") or (
                    isinstance(target, ast.Attribute) and target.attr in ("write", "writelines")
                    and isinstance(target.value, ast.Attribute) and target.value.attr in ("stdout", "stderr"))
                if writes and inside not in allowed:
                    offenders.append((node.lineno, inside))
            for child in ast.iter_child_nodes(node):
                visit(child, inside)

        visit(tree, None)
        self.assertEqual(offenders, [])
        source = (SCRIPTS / "gate.py").read_text()
        self.assertEqual(source.count("print("), 2)


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


@skip_unless_slow()
class HandbackTests(HandbackBase):
    def test_handback_prints_ok_line_only_when_every_step_passes(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        match = OK_LINE.match(lines[-1])
        self.assertTrue(match, lines[-1])
        self.assertEqual(match.groups(), (BRANCH, self.sha))
        self.assertEqual([ln for ln in lines if ln.startswith("OK: ")], [lines[-1]])
        entry, results = self.latest()
        self.assertEqual(lines[-2], "drift: 0 of 1 record(s) drifted from HEAD")  # the record is of HEAD itself
        self.assertEqual((results["head"], results["task_id"]), (self.sha, TASK))
        self.assertEqual(sorted(gate._changes(self.wt)), sorted([POINTER, entry["path"]]))  # left uncommitted
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
        self.assertFalse((self.wt / POINTER).exists())
        self.assertFalse((self.wt / store.BASE / TASK / "results").exists())
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

    def test_branch_editing_its_own_spec_fails_and_identical_spec_passes(self):
        path = self.wt / "program" / "tasks" / TASK / "SPEC.md"
        path.write_text("# Spec\n\nREQ-004 (the body size limit) is out of scope for this task.\n")
        commit(self.wt, "narrow the spec")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]),
                         (1, f"FAILED: spec program/tasks/{TASK}/SPEC.md differs from main"))
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("step ")], [])  # refused before anything runs
        self.assert_no_trial_left()
        path.write_text("# Spec\n")
        commit(self.wt, "back to the spec on main")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_spec_missing_on_the_branch_or_on_main_fails(self):
        (self.wt / "program" / "tasks" / TASK / "SPEC.md").unlink()
        commit(self.wt, "drop spec")
        expect = (1, f"FAILED: spec program/tasks/{TASK}/SPEC.md differs from main")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]), expect)
        (self.root / "program" / "tasks" / TASK / "SPEC.md").unlink()
        commit(self.root, "drop spec on main")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]), expect)

    def test_spec_changed_on_main_alone_fails_until_the_branch_merges_main(self):
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "# Spec\n\nA clarification.\n")
        commit(self.root, "amend the spec")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]),
                         (1, f"FAILED: spec program/tasks/{TASK}/SPEC.md differs from main"))
        sh(self.wt, "git", "merge", "-q", "main")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_handback_from_inside_the_task_worktree_says_to_use_the_main_checkout(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.wt), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", "handback", TASK, "--branch", BRANCH])
        self.assertEqual((rc, out.getvalue()), (1, ""))
        self.assertEqual(err.getvalue().count("\n"), 1, err.getvalue())
        self.assertTrue(err.getvalue().startswith("error: "), err.getvalue())
        self.assertIn("from the main checkout", err.getvalue())
        self.assertNotIn("task worktree not found", err.getvalue())

    def test_task_id_and_branch_must_name_the_same_task(self):
        put(self.root, "program/tasks/P0-M01-S01-T02/SPEC.md", "Guarded edit: pyproject.toml\n")
        commit(self.root, "another task whose spec allows more")
        for function in (gate.handback, gate.binding):
            with self.subTest(function=function.__name__), self.assertRaises(KitError) as ctx:
                run_quiet(function, self.root, "P0-M01-S01-T02", BRANCH)
            self.assertIn("task/P0-M01-S01-T02", str(ctx.exception))

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
        data = self.latest()[1]
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
        broken = store.append(self.wt, "OTHER", "results", b"{ not json")
        store.set_entry(self.wt, "OTHER", "results", {"digest": broken["digest"], "path": broken["path"],
                                                       "head": "", "produced_at": ""})
        commit(self.wt, "broken record")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        line = [ln for ln in out.splitlines() if ln.startswith("FAILED: stale unreadable record ")]
        self.assertEqual(len(line), 1, out)
        self.assertIn(f"{broken['path']}: ", line[0])
        self.assertNotIn("OK: ", out)
        self.assert_no_trial_left()

    def test_a_record_altered_in_the_store_fails_the_evidence_step(self):
        script = self.wt / f"program/evidence/{TASK}/produce.py"
        script.write_text(PRODUCE.replace("ev.write()\n", (  # the planted defect: the record changes after the append
            "written = ev.write()\nwritten['file'].chmod(0o644)\n"
            "written['file'].write_text(written['file'].read_text().replace('\"exit\": 0', '\"exit\": 0 '))\n")))
        commit(self.wt, "a producer that alters its record after the append")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertTrue(any(ln.startswith("FAILED: evidence immutable record altered: ") for ln in out.splitlines()),
                        out)

    def test_a_record_that_binds_a_derived_output_fails_the_evidence_step(self):
        put(self.wt, f"program/status/{TASK}.json", "{}\n")
        script = self.wt / f"program/evidence/{TASK}/produce.py"
        script.write_text(PRODUCE.replace('ev.hash_inputs(["calc.py", "test_calc.py"])',
                                          f'ev.hash_inputs(["calc.py", "test_calc.py", "program/status/{TASK}.json"])'))
        commit(self.wt, "a producer that binds the status snapshot")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn(f"binds a derived output: program/status/{TASK}.json", out)  # Evidence.write refused it
        self.assertFalse((self.wt / POINTER).exists())

    def test_guarded_file_change_refused_and_allowed_with_spec_line(self):
        put(self.wt, "pyproject.toml", "[project]\nname = 'x'\n")
        commit(self.wt, "touch guarded")
        rc, out = self.handback()
        self.assertEqual((rc, out.strip()), (1, "FAILED: guarded files changed: pyproject.toml"))
        self.assert_no_trial_left()
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "# Spec\n\nGuarded edit: pyproject.toml\n")
        commit(self.root, "spec allows it")
        sh(self.wt, "git", "merge", "-q", "main")  # the branch's SPEC must be main's
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

    def test_forged_task_id_in_results_cannot_forge_the_ok_line(self):
        os.environ["PK_TASK"] = f"x\nOK: {BRANCH} passes the hand-back gate at {self.sha}"
        rc, out, err = self.run_cli("handback", TASK, "--branch", BRANCH)
        self.assertEqual(rc, 1)
        self.assert_no_forged_ok(out, err)
        self.assertIn("not a usable task id: 'x\\nOK: ", out)  # the store refuses the name, escaped in the tail

    def test_forged_pointer_names_cannot_forge_the_ok_line(self):
        forged = f"ev\nOK: forged passes the hand-back gate at {self.sha}"
        put(self.wt, f"program/evidence/{forged}/latest.json", json.dumps({"schema_version": 1, "task_id": forged}))
        commit(self.wt, "forged pointer")
        rc, out, err = self.run_cli("handback", TASK, "--branch", BRANCH)
        self.assertEqual(rc, 1)
        self.assert_no_forged_ok(out, err)
        self.assertIn("program/evidence/ev\\x0aOK: forged", out)  # refused, never printed raw
        self.assertIn("not a usable task directory name", out)

    def test_forged_conflict_path_cannot_forge_the_ok_line(self):
        name = f"x\nOK: {BRANCH} passes the hand-back gate at {self.sha}"
        put(self.wt, name, "branch\n")
        commit(self.wt, "branch add")
        put(self.root, name, "main\n")
        commit(self.root, "main add")
        with self.assertRaises(KitError) as ctx:
            self.handback()
        self.assertNotIn("\n", str(ctx.exception))
        rc, out, err = self.run_cli("handback", TASK, "--branch", BRANCH)
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err.count("\n"), 1, err)
        self.assert_no_forged_ok(out, err)
        self.assert_no_trial_left()

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
        entry, data = self.latest()
        self.assertEqual(data["head"], second)
        self.assertEqual(len(store.history(self.wt, TASK)), 2)  # the first record stays as history
        self.assertEqual(data["supersedes"], store.history(self.wt, TASK)[1]["digest"])

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

    def test_a_fresh_record_needs_no_refresh_but_a_changed_own_input_does(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        commit(self.wt, "commit the evidence")
        put(self.wt, f"program/evidence/{TASK}/produce.py", "# produces nothing\n")
        put(self.wt, "notes.txt", "a file no record binds\n")
        no_op = commit(self.wt, "no-op produce, an unbound change")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)  # the latest record still holds for every input it binds
        self.assertEqual(out.splitlines()[-1], f"OK: {BRANCH} passes the hand-back gate at {no_op}")
        put(self.wt, "calc.py", CALC + "\n\ndef mul(a, b):\n    return b * a\n")
        commit(self.wt, "the task's own source changes")  # the planted defect: no refresh after it
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines()[-2:], ["FAILED: evidence", f"the task's own inputs changed since {self.sha} "
                                                 f"(calc.py): program/evidence/{TASK}/produce.py must append a new "
                                                 "results record"])

    def test_another_tasks_drifted_record_leaves_the_gate_passing(self):
        main_sha = sh(self.root, "git", "rev-parse", "main").strip()
        other = store.record(self.root, "OTHER", "results", {  # OTHER bound calc.py as main has it
            "task_id": "OTHER", "head": main_sha, "inputs": {"calc.py": hashlib.sha256(CALC.encode()).hexdigest()}})
        commit(self.root, "OTHER evidence on main")
        sh(self.wt, "git", "merge", "-q", "--no-edit", "main")
        merged = sh(self.wt, "git", "rev-parse", "HEAD").strip()
        rc, out = self.handback()  # this branch changed calc.py, so OTHER's record drifts here
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[-1], f"OK: {BRANCH} passes the hand-back gate at {merged}")
        self.assertIn(f"OTHER drifted since {main_sha}: calc.py", out.splitlines())
        self.assertNotIn("OTHER", " ".join(gate._changes(self.wt)))  # nothing asked OTHER to refresh
        self.assertTrue((self.wt / other["path"]).is_file())

    def test_a_commit_that_rewrites_a_record_fails_the_evidence_step(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        commit(self.wt, "commit the evidence")
        entry = self.latest()[0]
        path = self.wt / entry["path"]
        path.chmod(0o644)
        sh(self.wt, "git", "rm", "-q", entry["path"])  # the planted defect: a committed record deleted
        removed = commit(self.wt, "drop a record")
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn(f"evidence history rewritten: {entry['path']} deleted in {removed}", out.splitlines())

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
        sh(self.wt, "git", "merge", "-q", "main")
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
        sh(self.wt, "git", "merge", "-q", "main")
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


@skip_unless_slow()
class MergeCheckTests(HandbackBase):
    def test_merge_check_prints_ok_line_with_branch_head(self):
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[-1], f"OK: {BRANCH} merges into main at {self.sha}")
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("OK: ")], [out.splitlines()[-1]])
        self.assertTrue(self.fake_log.exists(), "merge-check runs the scan from main's checks.yaml")
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

    def test_merge_check_needs_scan_commands(self):
        self.write_checks(scan=[])
        commit(self.root, "no scan")
        with self.assertRaises(KitError) as ctx:
            self.merge_check()
        self.assertEqual(str(ctx.exception), "checks.yaml: scan must list at least one command")

    def produce_without_the_gate(self):
        """What an implementer who never ran the gate leaves: a record produced directly and a typed OK line."""
        subprocess.run([PY, f"program/evidence/{TASK}/produce.py"], cwd=str(self.wt), check=True,
                       capture_output=True)
        put(self.wt, f"program/evidence/{TASK}/report.md",
            f"## Gate\n\nOK: {BRANCH} passes the hand-back gate at {self.sha}\n")
        return commit(self.wt, "evidence nobody gated")

    def test_gate_skipped_with_an_unapproved_guarded_file_is_refused(self):
        put(self.wt, "pyproject.toml", "[project]\nname = 'x'\n")
        commit(self.wt, "touch guarded")
        self.produce_without_the_gate()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.binding(self.root, TASK, BRANCH), [])  # everything QA runs still passes
        rc, out = self.merge_check()
        self.assertEqual((rc, out.strip()), (1, "FAILED: guarded files changed: pyproject.toml"))
        self.assert_no_trial_left()
        put(self.root, f"program/tasks/{TASK}/SPEC.md", "# Spec\n\nGuarded edit: pyproject.toml\n")
        commit(self.root, "the user approves the guarded edit")
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)

    def test_gate_skipped_with_a_failing_test_is_refused(self):
        put(self.wt, "test_more.py", FAILING_TESTS)
        put(self.wt, f"program/evidence/{TASK}/produce.py", HAND_MADE)
        commit(self.wt, "failing tests and a producer that runs nothing")
        self.produce_without_the_gate()
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn(f"FAILED: test {PY} -m unittest -q (exit 1)", out.splitlines())
        self.assertFalse(self.fake_log.exists(), "scan ran after test failed")
        self.assert_no_trial_left()

    def test_gate_skipped_with_a_scan_finding_or_no_scanner_is_refused(self):
        self.produce_without_the_gate()
        self.wrapper("gitleaks", 'echo "leak found"\nexit 1')
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines()[-2:], ["FAILED: scan gitleaks detect --no-git (exit 1)", TAIL + "leak found"])
        (self.bin / "gitleaks").unlink()
        rc, out = self.merge_check()
        self.assertEqual((rc, out.splitlines()[-1]), (1, "FAILED: scan missing tool: gitleaks"))
        self.assert_no_trial_left()

    def test_merge_check_runs_every_step_in_order_on_the_trial_merge(self):
        marker = self.root.parent / "order.log"
        script = "import os, sys; open(%r, 'a').write(sys.argv[1] + ' ' + os.path.basename(os.getcwd()) + '\\n')" \
            % str(marker)
        self.write_checks(**{step: [[PY, "-c", script, step]] for step in
                             ("build", "compile_check", "test", "scan", "extra_gate")})
        commit(self.root, "ordered commands")
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        ran = [line.split() for line in marker.read_text().splitlines()]
        self.assertEqual([step for step, _ in ran], ["build", "compile_check", "test", "scan", "extra_gate"])
        self.assertTrue(all(where.startswith("pk-trial-") for _, where in ran), ran)
        self.assertEqual(len({where for _, where in ran}), 1)

    def test_merge_check_refuses_a_branch_that_rewrote_its_spec_or_plan(self):
        for name, label in (("SPEC.md", "spec"), ("PLAN.md", "plan")):
            with self.subTest(name=name):
                sh(self.wt, "git", "reset", "-q", "--hard", self.sha)
                put(self.wt, f"program/tasks/{TASK}/{name}", "# Easier\n\n## Exact Commands\n\n$ python3 -c pass\n")
                commit(self.wt, f"rewrite {name}")
                rc, out = self.merge_check()
                self.assertEqual((rc, out.splitlines()[-1]),
                                 (1, f"FAILED: {label} program/tasks/{TASK}/{name} differs from main"))
                self.assertEqual([ln for ln in out.splitlines() if ln.startswith("step ")], [])
                self.assert_no_trial_left()

    def test_merge_check_needs_a_task_branch(self):
        sh(self.root, "git", "branch", "feature", BRANCH)
        for branch in ("feature", "main", "task/a/b", "task/"):
            with self.subTest(branch=branch), self.assertRaises(KitError) as ctx:
                run_quiet(gate.merge_check, self.root, branch)
            self.assertIn("task/<ID>", str(ctx.exception))
        self.assert_no_trial_left()

    def test_merge_check_fails_when_the_branch_moves_during_the_check(self):
        script = ("import subprocess, sys; subprocess.run(['git', '-C', sys.argv[1], 'commit', '-q', '--allow-empty', "
                  "'-m', 'moved'], check=True)")
        self.write_checks(extra_gate=[[PY, "-c", script, str(self.wt)]])
        commit(self.root, "a step that moves the branch")
        rc, out = self.merge_check()
        self.assertEqual((rc, out.splitlines()[-1]), (1, f"FAILED: {BRANCH} moved during the merge check"))


@skip_unless_slow()
class ReviewRecordTests(HandbackBase):
    """The spec-QA record `review.md` is frozen with the SPEC and the PLAN: both gates need it on main and
    compare the branch's with it."""

    REVIEW = f"program/tasks/{TASK}/review.md"
    LINE = f"FAILED: review program/tasks/{TASK}/review.md differs from main"
    MISSING = f"FAILED: review program/tasks/{TASK}/review.md is missing on main"

    def recheck_on_main(self):
        put(self.root, self.REVIEW, "# Review\n\n| 1 | SPEC names no failure mode | important | open |\n")
        commit(self.root, "the spec-QA record, re-checked")
        sh(self.wt, "git", "merge", "-q", "main")
        return sh(self.wt, "git", "rev-parse", "HEAD").strip()

    def dispatch_without_a_review(self):
        (self.root / self.REVIEW).unlink()
        commit(self.root, "a dispatch that skipped spec QA")
        sh(self.wt, "git", "merge", "-q", "main")

    def assert_both_gates_refuse(self, line=None):
        for run in (self.handback, self.merge_check):
            rc, out = run()
            self.assertEqual((rc, out.splitlines()[-1]), (1, line or self.LINE), run.__name__)
            self.assertEqual([ln for ln in out.splitlines() if ln.startswith("step ")], [])  # before anything runs
            self.assert_no_trial_left()

    def test_the_fixture_dispatch_carries_the_record_and_both_gates_pass(self):
        self.assertEqual(sh(self.root, "git", "show", f"main:{self.REVIEW}"), REVIEW)
        self.assertEqual(self.merge_check()[0], 0)
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_a_task_dispatched_without_a_review_record_is_refused_by_both_gates(self):
        self.dispatch_without_a_review()  # main and the branch agree: neither has one
        self.assert_both_gates_refuse(self.MISSING)
        put(self.root, self.REVIEW, REVIEW)
        commit(self.root, "spec QA ran after all")
        rc, out = self.handback()  # on main now, not yet in the task worktree
        self.assertEqual((rc, out.splitlines()[-1]), (1, self.LINE))
        self.assertEqual(self.merge_check()[0], 0)  # the trial merge carries main's record
        sh(self.wt, "git", "merge", "-q", "main")
        self.assertEqual(self.merge_check()[0], 0)
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_a_missing_record_is_one_failed_line_from_the_command_too(self):
        self.dispatch_without_a_review()
        for argv in (("handback", TASK, "--branch", BRANCH), ("merge-check", "--branch", BRANCH)):
            rc, out, err = self.run_cli(*argv)
            self.assertEqual((rc, out, err), (1, self.MISSING + "\n", ""), argv)

    def test_a_branch_that_rewrites_the_review_record_is_refused_by_both_gates(self):
        self.recheck_on_main()
        put(self.wt, self.REVIEW, "# Review\n\nNo findings.\n")
        commit(self.wt, "a kinder review")
        self.assert_both_gates_refuse()
        sh(self.wt, "git", "checkout", "-q", "main", "--", self.REVIEW)
        commit(self.wt, "back to the record on main")
        self.assertEqual(self.merge_check()[0], 0)
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_a_branch_that_removes_the_review_record_is_refused_by_both_gates(self):
        (self.wt / self.REVIEW).unlink()
        commit(self.wt, "drop the review")
        self.assert_both_gates_refuse()

    def test_a_branch_that_adds_a_review_record_main_does_not_have_is_refused_by_both_gates(self):
        self.dispatch_without_a_review()
        put(self.wt, self.REVIEW, "# Review\n\nNo findings.\n")
        commit(self.wt, "a review of its own")
        self.assert_both_gates_refuse(self.MISSING)  # a branch cannot supply the record of its own dispatch

    def test_a_record_changed_on_main_alone_fails_until_the_branch_merges_main(self):
        put(self.root, self.REVIEW, "# Review\n\nRe-checked: resolved.\n")
        commit(self.root, "spec QA re-check")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]), (1, self.LINE))
        sh(self.wt, "git", "merge", "-q", "main")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)

    def test_the_spec_and_the_plan_are_named_before_the_review_record(self):
        put(self.wt, self.REVIEW, "# Review\n\nNo findings.\n")
        put(self.wt, f"program/tasks/{TASK}/SPEC.md", "# Narrower\n")
        commit(self.wt, "both")
        rc, out = self.handback()
        self.assertEqual((rc, out.splitlines()[-1]),
                         (1, f"FAILED: spec program/tasks/{TASK}/SPEC.md differs from main"))


@skip_unless_slow()
class ByteOrderMarkTests(HandbackBase):
    """A file saved with a leading UTF-8 byte-order mark reads the same from a git object as from the disk."""

    BOM = b"\xef\xbb\xbf"

    def put_on_main(self, name, text):
        path = self.root / "program" / "tasks" / TASK / name
        path.write_bytes(self.BOM + text.encode("utf-8"))
        commit(self.root, f"{name} saved with a byte-order mark")
        sh(self.wt, "git", "merge", "-q", "main")

    def test_a_guarded_edit_line_on_the_first_line_of_a_marked_spec_counts(self):
        put(self.wt, "pyproject.toml", "[project]\nname = 'x'\n")
        commit(self.wt, "touch guarded")
        self.put_on_main("SPEC.md", "Guarded edit: pyproject.toml\n")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.merge_check()[0], 0)

    def test_the_commands_of_a_marked_plan_that_opens_with_its_section_are_run(self):
        self.put_on_main("PLAN.md", "## Exact Commands\n\n$ python3 -c pass\n")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn("step plan: ok", out.splitlines())

    def test_a_marked_checks_file_on_main_is_read(self):
        path = self.root / "program" / "checks.yaml"
        path.write_bytes(self.BOM + path.read_bytes())
        commit(self.root, "checks saved with a byte-order mark")
        self.assertEqual(self.merge_check()[0], 0)

    def test_binding_reads_a_marked_pointer(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        record = self.wt / POINTER
        record.write_bytes(self.BOM + record.read_bytes())
        commit(self.wt, "commit the evidence")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gate.binding(self.root, TASK, BRANCH), [])
        self.assertIn("binding holds", out.getvalue())


@skip_unless_slow()
class FixedMainTests(HandbackBase):
    """The rules are always read from `main`: no argument can make a branch the judge of itself."""

    def rig_the_branch(self):
        """Two failing tests, every check and the PLAN rewritten to pass, and a hand-made record."""
        put(self.wt, "test_more.py", FAILING_TESTS)
        easy = [[PY, "-c", "pass"]]
        write_yaml(self.wt / "program" / "checks.yaml", checks_data(build=easy, test=easy, scan=easy))
        put(self.wt, f"program/tasks/{TASK}/PLAN.md", "## Exact Commands\n\n$ python3 -c pass\n")
        put(self.wt, f"program/evidence/{TASK}/produce.py", HAND_MADE)
        return commit(self.wt, "rigged")

    def gate_actions(self):
        import argparse

        top = pk.build_parser()
        commands = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction)).choices
        return next(a for a in commands["gate"]._actions if isinstance(a, argparse._SubParsersAction)).choices

    def test_no_gate_function_or_subcommand_takes_a_main_branch(self):
        for function in (gate.handback, gate.merge_check, gate.binding):
            self.assertNotIn("main", inspect.signature(function).parameters, function.__name__)
        actions = self.gate_actions()
        self.assertEqual(sorted(actions), ["binding", "claims", "handback", "merge-check", "validate"])
        for name, parser in actions.items():
            options = {option for action in parser._actions for option in action.option_strings}
            self.assertEqual([o for o in options if o.startswith("--m")], [], name)

    def test_a_rigged_branch_gets_no_ok_line_by_any_argument(self):
        self.rig_the_branch()
        bases = [("handback", TASK, "--branch", BRANCH), ("merge-check", "--branch", BRANCH)]
        extras = [(), ("--main", BRANCH), (f"--main={BRANCH}",), ("--mai", BRANCH), ("--main", "main"),
                  ("--main", f"refs/heads/{BRANCH}")]
        attempts = [base + extra for base in bases for extra in extras]
        attempts += [bases[0] + ("--worktree", str(self.wt)), bases[0] + ("--worktree", str(self.wt), "--main", BRANCH)]
        for argv in attempts:
            with self.subTest(argv=argv):
                rc, out, err = self.run_cli(*argv)
                self.assertEqual(rc, 1, out)
                self.assert_no_forged_ok(out, err)
                sh(self.wt, "git", "clean", "-fdq")  # a record a wrongly passing run would leave behind
        for base in bases:
            with self.subTest(honest=base):
                rc, out, err = self.run_cli(*base)
                self.assertEqual((rc, out.strip(), err), (1, "FAILED: guarded files changed: program/checks.yaml", ""))
        self.assert_no_trial_left()

    def test_binding_takes_no_main_branch_either(self):
        rc, out, err = self.run_cli("binding", TASK, "--branch", BRANCH, "--main", BRANCH)
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"error: unrecognized arguments: --main {BRANCH}\n")


@skip_unless_slow()
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

REC_DEV = "program/evidence/P0-M01-S01-T01/results.json"  # a 0.1 record path: it names task P0-M01-S01-T01
REC_TWO = "program/evidence/P0-M01-S01-T02/results.json"
TASK_OF = {REC_DEV: "P0-M01-S01-T01", REC_TWO: "P0-M01-S01-T02"}


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
        store.record(self.root, TASK_OF[REC_DEV], "results", {"task_id": "one"})
        store.record(self.root, TASK_OF[REC_TWO], "results", {"task_id": "two"})
        commit(self.root, "init")

    def set_gates(self, gates):
        write_yaml(self.root / "program" / "gates.yaml", {"gates": gates})

    def accept(self, record=REC_DEV, **over):
        """Append an acceptance of the record's latest results record; its digest."""
        task_id = TASK_OF[record]
        latest = store.latest(store.read_index(self.root), task_id, "results")["digest"]
        data = {"task_id": "x", "decision": "accepted_development", "evidence_sha256": latest,
                "source_commit": "a" * 40, "accepted_at": "2026-10-06T00:00:00Z", "role": "qa", "commands": [],
                "planted_bugs": [], "findings": [], "blocked_by": []}
        data.update(over)
        return store.record(self.root, task_id, "acceptance", data)["digest"]

    def claim(self, gate_id, record):
        name = record.replace("/", "__")
        return read_json(self.root / "program" / "claims" / gate_id / name)


@skip_unless_slow()
class ClaimsTests(ClaimsBase):
    def test_claims_never_evidence_missing_environment(self):
        acceptance = self.accept()
        written = gate.claims(self.root, "G-LIVE")
        self.assertEqual([p.name for p in written], ["program__evidence__P0-M01-S01-T01__results.json"])
        doc = self.claim("G-LIVE", REC_DEV)
        self.assertEqual(doc["environment"], {"dev": "not_evaluated", "staging": "not_evaluated",
                                              "prod": "not_evaluated"})
        self.assertEqual(doc["acceptance_sha256"], acceptance)
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
        broken = store.append(self.root, TASK_OF[REC_DEV], "acceptance", b"{ nope")
        store.set_entry(self.root, TASK_OF[REC_DEV], "acceptance", {
            "digest": broken["digest"], "path": broken["path"], "decision": "", "accepted_at": "",
            "results_digest": ""})
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"]["dev"], "not_evaluated")

    def test_a_refreshed_record_withdraws_the_evidenced_state_until_it_is_accepted_again(self):
        self.accept()
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"]["dev"], "evidenced")
        store.record(self.root, TASK_OF[REC_DEV], "results", {"task_id": "refreshed"})  # a newer results record
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"]["dev"], "not_evaluated")
        self.accept()  # re-acceptance appends; it binds the latest results record
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"]["dev"], "evidenced")

    def test_an_altered_record_withdraws_the_evidenced_state(self):
        self.accept()
        entry = store.latest(store.read_index(self.root), TASK_OF[REC_DEV], "results")
        path = self.root / entry["path"]
        path.chmod(0o644)
        path.write_text('{"task_id": "changed"}\n')  # the planted defect: a record edited in the store
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["environment"]["dev"], "not_evaluated")

    def test_a_record_can_name_its_task_directly(self):
        self.set_gates({"G-P0": {"title": "Phase 0", "claims": ["it works"], "environment": "dev",
                                 "records": [{"task": TASK_OF[REC_DEV], "acceptance": "all pass"}]}})
        self.accept()
        written = gate.claims(self.root, "G-P0")
        self.assertEqual([p.name for p in written], ["P0-M01-S01-T01.json"])
        self.assertEqual(read_json(written[0])["environment"]["dev"], "evidenced")
        self.set_gates({"G-P0": {"title": "Phase 0", "claims": ["it works"], "environment": "dev",
                                 "records": [{"task": "../x", "acceptance": "all pass"}]}})
        with self.assertRaisesRegex(KitError, "record task must be a task or gate ID"):
            gate.claims(self.root, "G-P0")

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

    def test_gate_with_no_records_is_refused_and_nothing_is_touched(self):
        self.set_gates({"G-EMPTY": {"title": "t", "claims": [], "environment": "dev", "records": []}})
        directory = self.root / "program" / "claims" / "G-EMPTY"
        directory.mkdir(parents=True)
        (directory / "old.json").write_text("{}")
        for function in (gate.claims, gate.validate_claims):
            with self.subTest(function=function.__name__), self.assertRaises(KitError) as ctx:
                function(self.root, "G-EMPTY")
            self.assertEqual(str(ctx.exception),
                             "gate G-EMPTY: records is empty; a gate with no records claims nothing")
        self.assertEqual([p.name for p in directory.iterdir()], ["old.json"])

    def test_claims_leave_a_file_alone_when_only_the_commit_would_change(self):
        self.accept()
        first = commit(self.root, "acceptance")
        written = gate.claims(self.root, "G-P0")
        before = {path: path.read_bytes() for path in written}
        self.assertEqual(len(before), 2)
        commit(self.root, "claims")
        self.assertEqual(gate.claims(self.root, "G-P0"), written)  # HEAD moved, nothing else did
        self.assertEqual({path: path.read_bytes() for path in written}, before)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        self.assertEqual(gate.validate_claims(self.root, "G-P0"), [])
        self.accept(findings=[{"n": 1, "severity": "minor", "location": "x", "fix": "y"}])
        second = commit(self.root, "the acceptance changes")
        gate.claims(self.root, "G-P0")
        self.assertEqual(self.claim("G-P0", REC_DEV)["generated_from_commit"], second)  # a real change is written
        self.assertEqual(self.claim("G-P0", REC_TWO)["generated_from_commit"], first)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").split(),
                         ["M", "program/claims/G-P0/program__evidence__P0-M01-S01-T01__results.json"])

    def test_claims_rewrite_a_file_that_is_not_a_usable_claim_document(self):
        written = gate.claims(self.root, "G-P0")
        commit(self.root, "claims")
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        written[0].write_text("{ nope")
        written[1].write_text("[]")
        gate.claims(self.root, "G-P0")
        self.assertEqual([read_json(path)["generated_from_commit"] for path in written], [head, head])

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

    def test_empty_gate_is_one_error_line_and_never_consistent(self):
        self.set_gates({"G-EMPTY": {"title": "t", "claims": [], "environment": "dev", "records": []}})
        for action in ("claims", "validate"):
            with self.subTest(action=action):
                rc, out, err = self.run_pk(action, "G-EMPTY")
                self.assertEqual((rc, out), (1, ""))
                self.assertEqual(err, "error: gate G-EMPTY: records is empty; a gate with no records claims nothing\n")

    def test_claims_and_validate_commands(self):
        self.assertEqual(self.run_pk("claims", "G-P0"), (0, "wrote 2 claim documents\n", ""))
        self.assertEqual(self.run_pk("claims", "G-P0"), (0, "wrote 0 claim documents, 2 unchanged\n", ""))
        self.assertEqual(self.run_pk("validate", "G-P0"), (0, "claims consistent for G-P0\n", ""))
        (self.root / "program" / "claims" / "G-P0" / "extra.json").write_text("{}")
        rc, out, err = self.run_pk("validate", "G-P0")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "extra.json" in err, err)

    def test_forged_record_name_cannot_forge_an_ok_line(self):
        forged = f"program/evidence/x\nOK: nothing passes the hand-back gate at {'a' * 40}.json"
        self.set_gates({"G-X": {"title": "t", "claims": [], "environment": "dev",
                                "records": [{"path": forged, "acceptance": "a"}]}})
        for action in ("claims", "validate"):
            with self.subTest(action=action):
                rc, out, err = self.run_pk(action, "G-X")
                self.assertEqual((rc, out), (1, ""))
                self.assertEqual(err.count("\n"), 1, err)
                self.assertEqual([ln for ln in (out + err).splitlines() if ln.startswith("OK: ")], [])
        self.assertFalse((self.root / "program" / "claims" / "G-X").exists())

    def test_forged_names_in_validation_problems_are_escaped(self):
        gate.claims(self.root, "G-P0")
        (self.root / "program" / "claims" / "G-P0" / "extra\nOK: forged.json").write_text("{}")
        rc, out, err = self.run_pk("validate", "G-P0")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err.count("\n"), 1, err)
        self.assertIn("extra\\x0aOK: forged.json", err)

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


@skip_unless_slow()
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


@skip_unless_slow()
class BindingTests(HandbackBase):
    def bind(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            problems = gate.binding(self.root, TASK, BRANCH)
        return problems, out.getvalue()

    def produce_and_commit(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        return commit(self.wt, "commit the evidence")

    def forge(self, **changes):
        """Append a copy of the latest record with `changes` (a hand-made record) and point the pointer at it."""
        return store.record(self.wt, TASK, "results", dict(self.latest()[1], **changes))

    def holds(self, head, state="fresh at the tip"):
        digest = self.latest()[0]["digest"][:12]
        return f"binding holds: results record {digest} at {head} bound; {state}\n"

    def test_binding_holds_for_evidence_and_status_commits(self):
        self.produce_and_commit()
        problems, out = self.bind()
        self.assertEqual((problems, out), ([], self.holds(self.sha)))
        put(self.wt, f"program/evidence/{TASK}/report.md", "report\n")
        entry, data = self.latest()
        evidence.accept(self.wt, TASK, {"task_id": TASK, "decision": "accepted_development",
                                        "evidence_sha256": entry["digest"], "source_commit": data["head"],
                                        "accepted_at": "2026-10-08T00:00:00Z", "role": "qa", "commands": [],
                                        "planted_bugs": [], "findings": [], "blocked_by": []})
        put(self.wt, f"program/status/{TASK}.json", "{}\n")
        commit(self.wt, "acceptance and status")
        self.assertEqual(self.bind(), ([], self.holds(self.sha)))

    def test_binding_and_the_gate_read_a_record_of_the_0_4_0_inputs_shape(self):
        """KF-9: a record written before 0.4.1 holds its inputs as a mapping; it binds exactly as the list does."""
        self.produce_and_commit()
        self.assertIsInstance(self.latest()[1]["inputs"], list)
        self.forge(inputs=store.input_digests(self.latest()[1]["inputs"]))
        commit(self.wt, "a record of the 0.4.0 shape")
        self.assertIsInstance(self.latest()[1]["inputs"], dict)
        self.assertEqual(self.bind(), ([], self.holds(self.sha)))
        put(self.wt, "calc.py", CALC + "\n# later edit\n")
        commit(self.wt, "source after the evidence")
        self.assertEqual(self.bind(), ([], self.holds(self.sha, f"drifted since {self.sha} at the tip: calc.py")))

    def test_binding_ignores_the_working_tree(self):
        self.produce_and_commit()
        (self.wt / POINTER).write_text("{ nope")  # an uncommitted edit changes nothing
        self.assertEqual(self.bind()[0], [])

    def test_a_later_source_change_is_reported_as_drift_never_a_failure(self):
        self.produce_and_commit()
        put(self.wt, "calc.py", CALC + "\n# later edit\n")
        put(self.root, "lib.py", "VALUE = 1\n")
        commit(self.root, "main gains source")
        commit(self.wt, "source after the evidence")
        sh(self.wt, "git", "merge", "-q", "--no-edit", "main")
        problems, out = self.bind()
        self.assertEqual((problems, out), ([], self.holds(self.sha, f"drifted since {self.sha} at the tip: calc.py")))

    def test_binding_fails_when_the_record_is_not_committed(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        sh(self.wt, "git", "add", POINTER)
        sh(self.wt, "git", "commit", "-q", "-m", "the pointer without its record")
        problems, _ = self.bind()
        self.assertEqual(problems, [f"{self.latest()[0]['path']} is not committed on {BRANCH}"])

    def test_binding_fails_when_a_committed_record_is_rewritten(self):
        self.produce_and_commit()
        entry = self.latest()[0]
        path = self.wt / entry["path"]
        original = path.read_text()
        path.chmod(0o644)
        path.write_text(original.replace('"exit": 0', '"exit": 0 '))  # the planted defect
        commit(self.wt, "rewrite the record")
        problems, out = self.bind()
        self.assertEqual(out, "")
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(f"immutable record altered: {entry['path']} "), problems)
        path.write_text(original)  # the bytes are back, the rewrite stays in the history
        restored = commit(self.wt, "put the bytes back")
        self.assertEqual(self.bind(), ([f"evidence history rewritten: {entry['path']} modified in {restored}"], ""))

    def test_binding_fails_when_head_is_not_an_ancestor(self):
        self.produce_and_commit()
        put(self.root, "later.txt", "x\n")
        elsewhere = commit(self.root, "main moves on")
        self.forge(head=elsewhere)
        commit(self.wt, "results for a foreign head")
        problems, out = self.bind()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not an ancestor", problems[0])

    def test_binding_fails_for_a_head_edited_to_a_later_commit(self):
        self.produce_and_commit()
        put(self.wt, "calc.py", CALC.replace("a + b", "a - b") + "\n\ndef mul(a, b):\n    return a * b\n")
        broken = commit(self.wt, "break add after the evidence")
        self.assertIn("drifted since", self.bind()[1])
        recorded = store.input_digests(self.latest()[1]["inputs"])["calc.py"]
        forged = self.forge(head=broken)
        commit(self.wt, "move the recorded head past the edit")
        problems, out = self.bind()
        self.assertEqual(out, "")
        actual = hashlib.sha256((self.wt / "calc.py").read_bytes()).hexdigest()
        self.assertEqual(problems, [f"{forged['path']}: inputs: calc.py is recorded as {recorded} but its blob at "
                                    f"the recorded head {broken} hashes to {actual}"])

    def test_binding_fails_for_an_edited_or_made_up_input_hash(self):
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        honest = self.latest()[1]
        for name, edit in (("edited", {"calc.py": "0" * 64}), ("made up", {"gone.py": "1" * 64})):
            with self.subTest(case=name):
                inputs = store.input_list({**store.input_digests(honest["inputs"]), **edit})
                forged = store.record(self.wt, TASK, "results", dict(honest, inputs=inputs))
                commit(self.wt, "evidence with a hand-edited hash")
                problems, out = self.bind()
                self.assertEqual(out, "")
                self.assertEqual(len(problems), 1, problems)
                path = next(iter(edit))
                self.assertTrue(problems[0].startswith(f"{forged['path']}: inputs: {path} is recorded as "), problems)
                self.assertIn(f"the recorded head {self.sha}", problems[0])

    def test_binding_fails_when_the_pointer_and_the_record_disagree_on_the_head(self):
        self.produce_and_commit()
        index = read_json(self.wt / POINTER)
        index["results"]["head"] = "0" * 40  # the planted defect: a hand-edited pointer
        (self.wt / POINTER).write_text(json.dumps(index))
        commit(self.wt, "edit the pointer")
        problems, _ = self.bind()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(f"results entry names head {'0' * 40}", problems[0])

    def test_binding_holds_again_after_a_fix_round_regates(self):
        self.produce_and_commit()
        put(self.wt, "calc.py", CALC + "\n\ndef mul(a, b):\n    return b * a\n")
        fixed = commit(self.wt, "fix the finding")
        self.assertIn(f"drifted since {self.sha} at the tip: calc.py", self.bind()[1])
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        commit(self.wt, "evidence for the fix")
        self.assertEqual(self.bind(), ([], self.holds(fixed)))
        self.assertEqual(len(store.history(self.wt, TASK)), 2)

    def test_binding_fails_when_results_are_absent(self):
        problems, out = self.bind()
        self.assertEqual(problems, [f"{POINTER} on {BRANCH} names no results record of {TASK}"])

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
        self.assertTrue(out.getvalue().startswith("binding holds: results record "))
        self.forge(head="0" * 40)
        commit(self.wt, "a forged head")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", "binding", TASK, "--branch", BRANCH])
        self.assertEqual((rc, out.getvalue()), (1, ""))
        self.assertTrue(err.getvalue().startswith("error: "), err.getvalue())


if __name__ == "__main__":
    unittest.main()
