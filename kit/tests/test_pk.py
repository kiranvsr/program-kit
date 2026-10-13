import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
from common import KitError, read_json, read_yaml

KIT = Path(__file__).resolve().parent.parent
TEMPLATE_FILES = ["BRIEF.md", "checks.yaml", "environments.yaml", "CLAIM_BOUNDARIES.md", "ledger.yaml",
                  "HANDOFF.md", "controller-state.json", "rulings.md",
                  "register.yaml", "traceability.yaml", "gates.yaml", "COMMON.md"]


class PkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)  # init needs a repository
        ceiling = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp.parent)})
        ceiling.start()  # a repository above the temp directory is never this test's
        self.addCleanup(ceiling.stop)
        old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old)

    def run_pk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_init_creates_program_tree_and_refuses_twice(self):
        rc, out, err = self.run_pk("init")
        self.assertEqual((rc, err), (0, ""))
        for name in TEMPLATE_FILES:
            self.assertEqual((self.tmp / "program" / name).read_bytes(), (KIT / "templates" / name).read_bytes(), name)
        self.assertEqual(read_json(self.tmp / "program" / "controller-state.json"),
                         {"stage": {}, "queued": {}, "parked": {}, "adhoc": [], "followups": [],
                          "closed_followups": [], "now": ""})
        handoff = (self.tmp / "program" / "HANDOFF.md").read_text()
        self.assertIn("<!-- generated:start -->", handoff)
        self.assertIn("<!-- generated:end -->", handoff)
        headings = (self.tmp / "program" / "CLAIM_BOUNDARIES.md").read_text()
        for heading in ("Claimable in development", "Unclaimable until the named environment exists",
                        "Never claimed"):
            self.assertIn(heading, headings)
        (self.tmp / "program" / "BRIEF.md").write_text("mine\n")
        rc, out, err = self.run_pk("init")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: ") and "exists" in err and err.count("\n") == 1, err)
        self.assertEqual((self.tmp / "program" / "BRIEF.md").read_text(), "mine\n")

    def test_yaml_templates_have_the_spec_shapes(self):
        checks = read_yaml(KIT / "templates" / "checks.yaml")
        self.assertEqual(sorted(checks), sorted(["build", "test", "scan", "compile_check", "extra_gate",
                                                 "guarded_files", "tiers"]))
        # no lower tier is switched on in a new program: every task is full-tier until full_paths is filled
        self.assertEqual(checks.pop("tiers"), {"full_paths": [], "light_paths": [], "light_ruling": ""})
        for key, value in checks.items():
            self.assertEqual(value, ["program/checks.yaml"] if key == "guarded_files" else [], key)
        self.assertEqual(read_yaml(KIT / "templates" / "environments.yaml"), {"environments": {}})
        self.assertEqual(read_yaml(KIT / "templates" / "ledger.yaml"), {"sources": {}, "entries": []})

    def test_init_write_failure_is_one_line_and_leaves_nothing(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.tmp.chmod(0o555)
        self.addCleanup(self.tmp.chmod, 0o755)
        rc, out, err = self.run_pk("init")
        self.tmp.chmod(0o755)
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".git"])

    def test_init_leaves_no_staging_directory(self):
        self.run_pk("init")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".git", ".gitignore", "program"])

    def test_init_with_preset_copies_checks(self):
        preset = KIT / "presets" / "checks.python.yaml"
        rc, _, err = self.run_pk("init", "--preset", "python")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((self.tmp / "program" / "checks.yaml").read_bytes(), preset.read_bytes())

    def test_init_with_missing_preset_file_writes_nothing(self):
        # A preset the real kit does not ship, let past the argument check: every template is there, the preset is not.
        with mock.patch.object(pk, "PRESETS", (*pk.PRESETS, "fortran")):
            rc, out, err = self.run_pk("init", "--preset", "fortran")
        self.assertEqual((rc, out), (1, ""))
        missing = KIT / "presets" / "checks.fortran.yaml"
        self.assertEqual(err, f"error: cannot read {missing}: No such file or directory\n")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".git"])  # no program/, no .gitignore

    def outside(self):
        """A directory that is in no git repository, made the current one."""
        place = self.tmp.parent / f"{self.tmp.name}-plain"
        place.mkdir()
        self.addCleanup(shutil.rmtree, place, ignore_errors=True)
        os.chdir(place)
        return place

    def test_init_outside_any_git_repository_is_one_error_line_and_creates_nothing(self):
        place = self.outside()
        for argv in (("init",), ("init", "--preset", "python")):
            rc, out, err = self.run_pk(*argv)
            self.assertEqual((rc, out), (1, ""))
            self.assertEqual(err, f"error: {place} is not inside a git repository; run `git init -b main` there, "
                                  "then pk init\n")
            self.assertEqual(list(place.iterdir()), [])

    def test_init_in_a_subdirectory_refuses_and_names_the_root(self):
        deep = self.tmp / "services" / "api"
        deep.mkdir(parents=True)
        for where in (deep, deep.parent):
            with self.subTest(where=where.name):
                os.chdir(where)
                rc, out, err = self.run_pk("init", "--preset", "python")
                self.assertEqual((rc, out), (1, ""))
                self.assertEqual(err, f"error: {where} is not the root of its repository; run pk init in {self.tmp}\n")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".git", "services"])  # nothing in the ancestor
        self.assertEqual(sorted(p.name for p in deep.parent.iterdir()), ["api"])
        self.assertEqual(list(deep.iterdir()), [])

    def test_init_in_a_subdirectory_does_not_report_the_ancestors_program_directory(self):
        self.assertEqual(self.run_pk("init")[0], 0)
        before = sorted(p.name for p in (self.tmp / "program").iterdir())
        (self.tmp / "sub").mkdir()
        os.chdir(self.tmp / "sub")
        rc, out, err = self.run_pk("init")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("is not the root of its repository", err)
        self.assertEqual(err.count("\n"), 1)
        self.assertEqual(sorted(p.name for p in (self.tmp / "program").iterdir()), before)

    def test_init_at_the_root_reached_through_a_symlink_works(self):
        link = self.tmp.parent / f"{self.tmp.name}-link"
        link.symlink_to(self.tmp, target_is_directory=True)
        self.addCleanup(link.unlink)
        os.chdir(link)
        rc, out, err = self.run_pk("init")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue((self.tmp / "program" / "BRIEF.md").is_file())

    def test_init_in_a_task_worktree_acts_on_that_worktree(self):
        self.git_repo(commit=True)
        wt = self.tmp / ".worktrees" / "t1"
        subprocess.run(["git", "-C", str(self.tmp), "worktree", "add", "-q", "-b", "side", str(wt)], check=True)
        os.chdir(wt)
        rc, out, err = self.run_pk("init")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue((wt / "program").is_dir())
        self.assertFalse((self.tmp / "program").exists())

    def test_init_without_git_on_path_says_so(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "no-such-bin")}):
            rc, out, err = self.run_pk("init")
        self.assertEqual((rc, out, err), (1, "", "error: git was not found on PATH\n"))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".git"])

    def test_init_rejects_unknown_preset(self):
        rc, _, err = self.run_pk("init", "--preset", "cobol")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        self.assertFalse((self.tmp / "program").exists())

    def git_repo(self, branch="main", commit=False):
        """Replace the fake .git directory with a real repository whose HEAD is `branch`."""
        shutil.rmtree(self.tmp / ".git")
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        subprocess.run(["git", "-C", str(self.tmp), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
        if commit:
            subprocess.run(["git", "-C", str(self.tmp), "-c", "user.name=T", "-c", "user.email=t@e.x",
                            "commit", "-q", "--allow-empty", "-m", "c"], check=True)

    def test_doctor_ok_after_init(self):
        self.git_repo()
        self.run_pk("init", "--preset", "python")
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out, err), (0, "doctor: ok\n", ""))

    def test_doctor_reports_missing_program_dir(self):
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "program/" in err and err.count("\n") == 1, err)

    def test_doctor_reports_unparseable_checks_yaml(self):
        self.run_pk("init")
        (self.tmp / "program" / "checks.yaml").write_text("a: [1, 2]\n")
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "checks.yaml" in err and err.count("\n") == 1, err)

    def test_doctor_refuses_a_repository_without_a_main_branch(self):
        self.git_repo("master", commit=True)
        self.run_pk("init", "--preset", "python")
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        self.assertIn("main", err)
        self.assertIn("git branch -m main", err)

    def test_doctor_accepts_main_before_and_after_the_first_commit(self):
        self.git_repo("main")
        self.run_pk("init", "--preset", "go")
        self.assertEqual(self.run_pk("doctor"), (0, "doctor: ok\n", ""))
        self.git_repo("main", commit=True)
        self.assertEqual(self.run_pk("doctor"), (0, "doctor: ok\n", ""))

    def checks_doctor(self, text):
        self.git_repo()
        self.run_pk("init", "--preset", "python")
        (self.tmp / "program" / "checks.yaml").write_text(text)
        return self.run_pk("doctor")

    def test_doctor_validates_checks_yaml_like_the_gate(self):
        rc, out, err = self.checks_doctor("build: go build\ntest:\n  - - go\n    - test\nscan:\n  - - gitleaks\n")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err.count("\n"), 1, err)
        self.assertRegex(err, r"^error: checks\.yaml: .*build.*argv")

    def test_doctor_reports_every_problem_on_its_own_line(self):
        rc, out, err = self.checks_doctor("build: go build\ntest:\n  - [go]\nscan: []\n")
        self.assertEqual((rc, out), (1, ""))  # a flow list is already a reader error: one line naming the line
        self.assertEqual(err.count("\n"), 1, err)
        rc, out, err = self.checks_doctor(
            "build: go build\ntest:\n  - a b\nscan: []\ncompile_check:\n  - - ''\nguarded_files: x\nextra: 1\n")
        self.assertEqual((rc, out), (1, ""))
        lines = err.splitlines()
        self.assertTrue(all(line.startswith("error: checks.yaml: ") for line in lines), err)
        self.assertEqual(len(lines), 6, err)
        for line, pattern in zip(lines, (r"build.*list of argv lists", r"every test command", r"scan must list",
                                         r"every compile_check command", r"guarded_files must be a list",
                                         r"unknown key extra")):
            self.assertRegex(line, pattern)

    def test_doctor_requires_a_command_in_build_test_and_scan(self):
        self.git_repo()
        self.run_pk("init")  # the unfilled template lists none
        rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        lines = err.splitlines()
        self.assertEqual(len(lines), 3, err)
        for line, step in zip(lines, ("build", "test", "scan")):
            self.assertEqual(line, f"error: checks.yaml: {step} must list at least one command")

    def test_doctor_reports_checks_problems_together_with_the_branch_problem(self):
        self.git_repo("master")
        self.run_pk("init")
        rc, out, err = self.run_pk("doctor")
        self.assertEqual(rc, 1)
        self.assertEqual(len(err.splitlines()), 4, err)
        self.assertIn("git branch -m main", err.splitlines()[-1])

    def test_doctor_reports_old_python(self):
        self.run_pk("init")
        with mock.patch.object(pk.sys, "version_info", (3, 10, 9, "final", 0)):
            rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "3.11" in err, err)

    def test_doctor_reports_missing_git(self):
        self.run_pk("init")
        with mock.patch.object(pk.shutil, "which", return_value=None):
            rc, out, err = self.run_pk("doctor")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and "git" in err, err)

    def test_init_adds_the_ignore_lines_and_keeps_existing_ones(self):
        (self.tmp / ".gitignore").write_bytes(b"dist/\n*.pyc")
        rc, out, err = self.run_pk("init")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((self.tmp / ".gitignore").read_bytes(), b"dist/\n*.pyc\n__pycache__/\n.worktrees/\n")

    def test_init_creates_gitignore_with_the_three_lines(self):
        self.run_pk("init")
        self.assertEqual((self.tmp / ".gitignore").read_text(), "__pycache__/\n*.pyc\n.worktrees/\n")

    def test_running_pk_never_writes_bytecode(self):
        scripts = self.tmp / "scripts"
        shutil.copytree(KIT / "scripts", scripts, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        run = lambda *argv: subprocess.run([sys.executable, *argv], cwd=self.tmp, env=env,  # noqa: E731
                                           capture_output=True, text=True)
        run(str(scripts / "pk.py"), "doctor")
        run(str(scripts / "pk.py"), "manifest", "check")
        self.assertEqual(sorted(p.relative_to(scripts).as_posix() for p in scripts.rglob("*.pyc")), [])
        self.assertFalse((scripts / "__pycache__").exists())
        # the control: the same modules imported without pk's flag do leave bytecode behind
        run("-c", f"import sys; sys.path.insert(0, {str(scripts)!r}); import common")
        self.assertTrue(any(scripts.rglob("*.pyc")))

    def test_unknown_command_exits_1(self):
        rc, out, err = self.run_pk("frobnicate")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)

    def test_no_command_exits_1(self):
        rc, out, err = self.run_pk()
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)

    def test_kiterror_is_one_line_on_stderr(self):
        def boom(args):
            raise KitError("boom")

        with mock.patch.dict(pk.COMMANDS), mock.patch.dict(pk.CONFIGURERS):
            pk.register("boom", boom, help="always fails")
            rc, out, err = self.run_pk("boom")
        self.assertEqual((rc, out, err), (1, "", "error: boom\n"))

    def test_register_adds_command_with_its_own_arguments(self):
        def hello(args):
            print(f"hello {args.who}")
            return 0

        with mock.patch.dict(pk.COMMANDS), mock.patch.dict(pk.CONFIGURERS):
            pk.register("hello", hello, help="say hello", configure=lambda p: p.add_argument("who"))
            rc, out, err = self.run_pk("hello", "world")
        self.assertEqual((rc, out, err), (0, "hello world\n", ""))
        self.assertNotIn("hello", pk.COMMANDS)


if __name__ == "__main__":
    unittest.main()
