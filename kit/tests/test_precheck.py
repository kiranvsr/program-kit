"""`pk spec precheck` on a throw-away git repository: one clean task folder, then one planted problem per check, and
the opt-in `--build` check that every plant row's replacement still builds."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
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
import precheck as sp
from common import write_yaml

PK = Path(__file__).resolve().parent.parent / "scripts" / "pk.py"
PY = os.path.realpath(sys.executable)
A_TS = "export const A = 1;\nexport const B = 2;\n"
A_TEST = '''import { it, expect } from "vitest";
it("checks a", () => {
  expect(A, "case:A one").toBe(1);
  expect(B, "case:A two").toBe(2);
});
it("checks b", () => {
  expect(A, "case:B only").toBe(1);
});
'''
PLANTS = '''ROWS = [
    ("P1", "pkg/src/a.ts", "export const A = 1;\\n", "export const A = 0;\\n", "pkg", "src/a.test.ts", "checks a", "case:A one"),
    ("P2", "pkg/src/a.ts", "export const B = 2;\\n", "export const B = 3;\\n", "pkg", "src/a.test.ts", "checks a", "case:A two"),
]
GAPS = [
    ("G1", "pkg/src/a.ts", "export const A = 1;\\n", "export const A = 5;\\n", "pkg", "src/a.test.ts", "checks b", "case:B only"),
]
ALL = ROWS + GAPS
'''
PLAN = '''# Plan

## Exact Commands

$ echo one
$ python3 -c "print(1)"

## Plant Table

| # | File | Find | Replace | Killing test | Needle | Verdict |
|---|---|---|---|---|---|---|
| P1 | `pkg/src/a.ts` | `export const A = 1;\\n` | `export const A = 0;\\n` | "checks a" | `case:A one` | killed |
| P2 | `pkg/src/a.ts` | `export const B = 2;\\n` | `export const B = 3;\\n` | "checks a" | `case:A two` | killed |
| G1 | `pkg/src/a.ts` | `export const A = 1;\\n` | `export const A = 5;\\n` | "checks b" | `case:B only` | killed |
'''
SPEC = '''# Spec

## Failure Modes And Recovery

| Failure | Detection | Recovery | Test (literal name) |
|---|---|---|---|
| A is wrong | the a case | fix | "checks a" |
| B is wrong | the b case | fix | "checks b" |
'''


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                          capture_output=True, text=True, check=check)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class PrecheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="precheck-test-"))
        cls.base_repo = cls.tmp / "base"
        r = cls.base_repo
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        write(r / "pkg/src/a.ts", A_TS)
        write(r / "pkg/src/b.ts", "export const C = 1;\n")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "main")
        # the stand-in: the test file added, a.ts left alone
        git(r, "checkout", "-q", "-b", "standin")
        write(r / "pkg/src/a.test.ts", A_TEST)
        git(r, "add", "-A")
        git(r, "commit", "-qm", "standin")
        cls.patch = git(r, "diff", "main", "standin").stdout
        git(r, "checkout", "-q", "main")
        git(r, "update-ref", "refs/remotes/origin/main", "main")
        # an unmerged task branch that touches an unrelated file
        git(r, "checkout", "-q", "-b", "other")
        write(r / "pkg/src/b.ts", "export const C = 2;\n")
        git(r, "commit", "-qam", "other")
        git(r, "update-ref", "refs/remotes/origin/task/Other", "other")
        git(r, "checkout", "-q", "main")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="precheck-case-", dir=self.tmp)) / "repo"
        shutil.copytree(self.base_repo, self.repo, symlinks=True)
        self.spec = self.repo.parent / "SPEC-X"
        write(self.spec / "tools/plants.py", PLANTS)
        write(self.spec / "PLAN.md", PLAN)
        write(self.spec / "SPEC.md", SPEC)
        write(self.spec / "standin.patch", self.patch)

    def run_check(self, notes=None, **edits):
        for name, (old, new) in edits.items():
            path = self.spec / {"tools__plants__py": "tools/plants.py", "PLAN__md": "PLAN.md", "SPEC__md": "SPEC.md"}[name]
            text = path.read_text()
            self.assertIn(old, text)
            path.write_text(text.replace(old, new, 1))
        return [str(f) for f in sp.precheck(self.spec, self.repo, notes=notes)]

    def only(self, lines, check):
        return [l for l in lines if l.startswith(check + " ")]

    def test_clean(self):
        self.assertEqual(self.run_check(), [])

    def test_clean_leaves_repo_untouched(self):
        before = git(self.repo, "status", "--porcelain").stdout
        self.run_check()
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, before)
        self.assertEqual(git(self.repo, "worktree", "list").stdout.count("\n"), 1)
        self.assertFalse(list(self.spec.rglob("__pycache__")))

    def test_cli_exit_codes(self):
        ok = subprocess.run([PY, str(PK), "spec", "precheck", str(self.spec)], cwd=str(self.repo),
                            capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "spec precheck: clean"), ok.stderr)
        (self.spec / "PLAN.md").write_text(PLAN.replace("$ echo one", "$ echo 'one"))
        bad = subprocess.run([PY, str(PK), "spec", "precheck", str(self.spec)], cwd=str(self.repo),
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("PC-5", bad.stdout)

    def test_a_task_id_names_its_folder_under_program_tasks(self):
        shutil.copytree(self.spec, self.repo / "program" / "tasks" / "SPEC-X")
        out = io.StringIO()
        with mock.patch.object(sp, "find_root", return_value=self.repo), contextlib.redirect_stdout(out):
            self.assertEqual(pk.main(["spec", "precheck", "SPEC-X"]), 0)
        self.assertEqual(out.getvalue().strip(), "spec precheck: clean")

    def test_pc1_find_twice_or_missing(self):
        twice = self.run_check(tools__plants__py=('"export const B = 2;\\n", "export const B = 3;', '"export const ", "x'))
        self.assertTrue(any("P2 find occurs 2 times" in l for l in self.only(twice, "PC-1")), twice)
        self.setUp()
        gone = self.run_check(tools__plants__py=('"export const B = 2;\\n", "export const B = 3;', '"export const Z = 2;\\n", "x'))
        self.assertTrue(any("P2 find occurs 0 times" in l for l in self.only(gone, "PC-1")), gone)

    def test_pc1_needle_not_in_test(self):
        out = self.run_check(tools__plants__py=('"checks a", "case:A two"', '"checks a", "case:A missing"'))
        self.assertTrue(any("P2 needle 'case:A missing' is not in src/a.test.ts" in l for l in out), out)

    def test_pc1_plan_row_without_needle_or_test(self):
        out = self.run_check(PLAN__md=('| `case:A two` |', '|  |'))
        self.assertTrue(any("P2 names no needle" in l for l in out), out)
        self.setUp()
        out = self.run_check(PLAN__md=('| "checks b" | `case:B only`', '|  | `case:B only`'))
        self.assertTrue(any("G1 names no killing test" in l for l in out), out)

    def test_pc1_plant_without_plan_row_and_reverse(self):
        out = self.run_check(PLAN__md=("| G1 |", "| G9 |"))
        self.assertTrue(any("G1 has no PLAN table row" in l for l in out), out)
        self.assertTrue(any("G9 has no entry in plants.py" in l for l in out), out)

    def test_pc1_a_row_named_in_another_tools_script_has_an_entry(self):
        write(self.spec / "tools/journey.py", 'EXTRA = ["G9"]\n')
        out = self.run_check(PLAN__md=("| G1 |", "| G9 |"))
        self.assertFalse([l for l in out if "G9 has no entry" in l], out)

    def test_pc2_needle_prefix_of_another_label(self):
        out = self.run_check(tools__plants__py=('"checks a", "case:A one"', '"checks a", "case:A"'),
                             PLAN__md=("`case:A one` | killed", "`case:A` | killed"))
        pc2 = self.only(out, "PC-2")
        self.assertTrue(any("needle 'case:A' is a prefix of label 'case:A one'" in l for l in pc2), out)

    def test_pc3_failure_mode_without_plant(self):
        out = self.run_check(SPEC__md=('| "checks b" |', '| "checks c" |'))
        self.assertTrue(any("failure mode 'B is wrong' has no plant" in l for l in self.only(out, "PC-3")), out)

    def test_pc3_row_column(self):
        spec = SPEC.replace("| Test (literal name) |", "| Test | Row |").replace('| "checks a" |', '| x | P1 |') \
            .replace('| "checks b" |', '| x | P77 |').replace("|---|---|---|---|", "|---|---|---|---|---|")
        (self.spec / "SPEC.md").write_text(spec)
        out = [str(f) for f in sp.precheck(self.spec, self.repo)]
        pc3 = self.only(out, "PC-3")
        self.assertEqual(len(pc3), 1, out)
        self.assertIn("B is wrong", pc3[0])

    def test_pc4_does_not_apply_to_main(self):
        git(self.repo, "checkout", "-q", "main")
        write(self.repo / "pkg/src/a.test.ts", "// already on main\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "main moved")
        out = self.run_check()
        pc4 = self.only(out, "PC-4")
        self.assertTrue(any("does not apply to main" in l and "pkg/src/a.test.ts" in l for l in pc4), out)

    def test_pc4_branch_conflict_by_file(self):
        git(self.repo, "checkout", "-q", "-b", "clash", "main")
        write(self.repo / "pkg/src/a.test.ts", "// a different test file\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "clash")
        git(self.repo, "update-ref", "refs/remotes/origin/task/Clash", "clash")
        git(self.repo, "checkout", "-q", "main")
        out = self.run_check()
        pc4 = self.only(out, "PC-4")
        self.assertEqual(len(pc4), 1, out)
        self.assertIn("origin/task/Clash", pc4[0])
        self.assertIn("pkg/src/a.test.ts", pc4[0])

    def test_pc4_a_local_task_branch_is_checked_too_and_a_merged_one_is_not(self):
        git(self.repo, "checkout", "-q", "-b", "task/Local", "main")
        write(self.repo / "pkg/src/a.test.ts", "// a different test file\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "local clash")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "branch", "task/Merged", "main")
        pc4 = self.only(self.run_check(), "PC-4")
        self.assertEqual(len(pc4), 1, pc4)
        self.assertIn("task/Local", pc4[0])

    def test_pc4_own_branch_is_skipped(self):
        git(self.repo, "update-ref", "refs/remotes/origin/task/SPEC-X", "standin")
        git(self.repo, "branch", "task/SPEC-X", "standin")
        self.assertEqual(self.run_check(), [])

    def test_without_a_stand_in_the_task_branch_is_read(self):
        (self.spec / "standin.patch").unlink()
        git(self.repo, "branch", "task/SPEC-X", "standin")
        self.assertEqual(self.run_check(), [])

    def test_without_a_stand_in_or_a_branch_the_code_checks_are_a_note(self):
        (self.spec / "standin.patch").unlink()
        notes = []
        self.assertEqual(self.run_check(notes=notes), [])
        self.assertTrue(any("no standin.patch and no task branch" in n for n in notes), notes)

    def test_without_any_plant_table_the_plant_checks_are_a_note_and_tests_are_still_required(self):
        (self.spec / "tools/plants.py").unlink()
        (self.spec / "PLAN.md").write_text(PLAN.split("## Plant Table")[0])
        notes = []
        self.assertEqual(self.run_check(notes=notes), [])
        self.assertTrue(any("no plant table" in n for n in notes), notes)
        out = self.run_check(SPEC__md=('| "checks b" |', '|  |'))
        self.assertTrue(any("failure mode 'B is wrong' names no test" in l for l in self.only(out, "PC-3")), out)

    def test_require_plants_makes_a_missing_table_or_missing_code_a_finding(self):
        (self.spec / "tools/plants.py").unlink()
        (self.spec / "PLAN.md").write_text(PLAN.split("## Plant Table")[0])
        out = [str(f) for f in sp.precheck(self.spec, self.repo, require_plants=True)]
        self.assertTrue(any("no PLAN plant table with a Find column" in l for l in self.only(out, "PC-1")), out)
        self.assertEqual(len(self.only(out, "PC-3")), 2, out)
        self.setUp()
        (self.spec / "standin.patch").unlink()
        out = [str(f) for f in sp.precheck(self.spec, self.repo, require_plants=True)]
        self.assertTrue(any("no standin.patch and no task branch" in l for l in self.only(out, "PC-1")), out)

    def test_a_local_branch_and_its_pushed_copy_at_one_commit_are_reported_once(self):
        git(self.repo, "checkout", "-q", "-b", "task/Clash", "main")
        write(self.repo / "pkg/src/a.test.ts", "// a different test file\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "clash")
        git(self.repo, "update-ref", "refs/remotes/origin/task/Clash", "task/Clash")
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(len(self.only(self.run_check(), "PC-4")), 1)

    def test_pc5_unparsable_plan_commands(self):
        out = self.run_check(PLAN__md=("$ echo one", "$ echo 'one"))
        self.assertTrue(any("gate.parse_plan failed" in l for l in self.only(out, "PC-5")), out)
        self.setUp()
        out = self.run_check(PLAN__md=("## Exact Commands", "## Commands"))
        self.assertTrue(any("finds no" in l for l in self.only(out, "PC-5")), out)

    def test_json_plants_and_template_titles(self):
        (self.spec / "tools/plants.py").unlink()
        write(self.spec / "plants.json",
              '[{"id":"P1","file":"a.ts","find":"export const A = 1;\\n","replace":"","killer":"checks a","what":"x"}]')
        (self.spec / "PLAN.md").write_text(PLAN.replace("| P2 |", "| X2 |").replace("| G1 |", "| X3 |"))
        out = [str(f) for f in sp.precheck(self.spec, self.repo)]
        self.assertFalse([l for l in out if "P1 " in l], out)  # relative file resolved, killer title found

    def test_helpers(self):
        self.assertTrue(sp.title_matches("deny scan: <class> is refused as S10-SCAN-<ID>", "deny scan: a url is refused as s10-scan-url"))
        self.assertFalse(sp.title_matches("a different name", "deny scan: a url"))
        self.assertEqual(sp.clashes("case:A", ["case:A", "case:A one", "case:B", "case:A ${n}"]),
                         ["case:A one", "case:A ${n}"])
        self.assertEqual(sp.split_row("| a \\| b | `c` |"), ["a \\| b", "`c`"])
        self.assertTrue(sp.template_matches("redaction: location id in a value", ["redaction: ", " in a value becomes it"]))
        self.assertTrue(sp.composed("case:X hold", 'expect(1, "case:X"); `${label} hold`'))


CALC = "def add(a, b):\n    return a + b\n"
BUILD_PLAN = '''# Plan

## Exact Commands

$ python3 -m unittest -q

## Planted bugs

| # | File | Find | Replace | Killing test |
|---|---|---|---|---|
| B1 | `calc.py` | `return a + b` | `return a - b` | "test_add" |
| B2 | `calc.py` | `return a + b` | `return a +` | "test_add" |
| B3 | `extra.py` | `VALUE = 1` | `VALUE = (` | "test_add" |
'''
BUILD_SPEC = '''# Spec

## Failure Modes And Recovery

| Failure | Detection | Recovery | Test |
|---|---|---|---|
| add is wrong | the test | fix | "test_add" |
'''
CALC_TEST = ("import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")


class BuildTests(unittest.TestCase):
    """`--build`: each plant row's replacement, applied to a mirror of the stand-in, runs main's build commands."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve() / "repo"
        r = self.repo
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        write(r / ".gitignore", "__pycache__/\n*.pyc\n")
        write(r / "calc.py", CALC)
        write_yaml(r / "program" / "checks.yaml", {
            "build": [[PY, "-m", "compileall", "-q", "."]], "test": [[PY, "-m", "unittest", "-q"]],
            "scan": [[PY, "-c", "pass"]], "compile_check": [[PY, "-c", "import calc"]]})
        git(r, "add", "-A")
        git(r, "commit", "-qm", "main")
        git(r, "checkout", "-q", "-b", "standin")
        write(r / "test_calc.py", CALC_TEST)
        write(r / "extra.py", "VALUE = 1\n")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "standin")
        patch = git(r, "diff", "main", "standin").stdout
        git(r, "checkout", "-q", "main")
        self.spec = r / "program" / "tasks" / "T-1"
        write(self.spec / "PLAN.md", BUILD_PLAN)
        write(self.spec / "SPEC.md", BUILD_SPEC)
        write(self.spec / "standin.patch", patch)

    def findings(self, **kw):
        return [str(f) for f in sp.precheck(self.spec, self.repo, build=True, **kw)]

    def test_rows_whose_replacement_does_not_build_are_reported(self):
        before = git(self.repo, "status", "--porcelain").stdout
        out = self.findings()
        pc6 = [l for l in out if l.startswith("PC-6 ")]
        self.assertEqual(len(pc6), 2, out)
        self.assertTrue(any("B2 replace does not build" in l for l in pc6), out)
        self.assertTrue(any("B3 replace does not build" in l for l in pc6), out)
        self.assertFalse([l for l in out if "B1" in l], out)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, before)
        self.assertEqual(git(self.repo, "worktree", "list").stdout.count("\n"), 1)

    def test_the_compile_check_counts_as_the_build(self):
        write_yaml(self.repo / "program" / "checks.yaml", {
            "build": [[PY, "-c", "pass"]], "test": [[PY, "-m", "unittest", "-q"]], "scan": [[PY, "-c", "pass"]],
            "compile_check": [[PY, "-c", "import calc, extra"]]})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "a compile check that imports")
        out = self.findings()
        self.assertTrue(any("B2 replace does not build" in l and "import calc, extra" in l for l in out), out)

    def test_an_unmutated_stand_in_that_does_not_build_judges_no_row(self):
        patch = self.spec / "standin.patch"
        patch.write_text(patch.read_text().replace("+VALUE = 1", "+VALUE = ("))
        (self.spec / "PLAN.md").write_text(BUILD_PLAN.replace("`VALUE = 1` | `VALUE = (`", "`VALUE = (` | `VALUE = 1`"))
        pc6 = [l for l in self.findings() if l.startswith("PC-6 ")]
        self.assertEqual(len(pc6), 1, pc6)
        self.assertIn("unmutated", pc6[0])

    def test_without_the_flag_nothing_is_built(self):
        self.assertEqual([str(f) for f in sp.precheck(self.spec, self.repo)], [])

    def test_the_cli_takes_the_flag(self):
        out = io.StringIO()
        with mock.patch.object(sp, "find_root", return_value=self.repo), contextlib.redirect_stdout(out):
            self.assertEqual(pk.main(["spec", "precheck", "T-1", "--build"]), 1)
        self.assertIn("PC-6", out.getvalue())


if __name__ == "__main__":
    unittest.main()
