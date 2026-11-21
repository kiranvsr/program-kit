"""0.6.1: the secret scanner before a commit (KF-21, KF-55) and the precheck's stale-base revert check (KF-51)."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import precheck as sp
from common import write_yaml
from test_tracker import TrackerBase
from test_worktree import commit_all, make_repo, run_cli, sh

PY = sys.executable
# A scanner over the copies in its working directory: exit 3 and name the file when any file holds the flagged word.
SCANNER = ("import pathlib, sys\n"
           "hits = [str(p) for p in pathlib.Path('.').rglob('*') if p.is_file() and 'SECRET-LITERAL' in p.read_text()]\n"
           "print('\\n'.join('leak: ' + h for h in hits)); sys.exit(3 if hits else 0)")
CHECKS = {"build": [], "test": [], "scan": []}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                          capture_output=True, text=True, check=True).stdout


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class ScanStagedTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        write_yaml(self.root / "program" / "checks.yaml", dict(CHECKS, secret_scan=[[PY, "-c", SCANNER]]))
        commit_all(self.root, "checks")

    def scan(self, *argv):
        return run_cli("scan", self.root, "scan", "staged", *argv)

    def test_a_flagged_literal_in_a_staged_file_refuses_and_a_clean_one_passes(self):
        write(self.root / "program" / "rulings.md", "# rulings\n\nR1 quotes SECRET-LITERAL.\n")
        write(self.root / "notes.md", "clean\n")
        sh(self.root, "git", "add", "-A")
        code, out, _ = self.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("FAILED: scan staged (secret_scan) over 2 staged file(s)", out)
        self.assertIn("leak: program/rulings.md", out)
        code, out, _ = self.scan("--path", "docs/")
        self.assertEqual((code, out.strip()), (0, "scan staged: nothing staged to scan"))
        code, out, _ = self.scan("--path", "notes.md")
        self.assertEqual(code, 0, out)
        self.assertIn("scan staged: ok (1 command(s) of secret_scan over 1 staged file(s))", out)

    def test_it_scans_the_staged_text_not_the_working_copy(self):
        write(self.root / "a.md", "SECRET-LITERAL\n")
        sh(self.root, "git", "add", "a.md")
        write(self.root / "a.md", "fixed but not staged\n")
        self.assertEqual(self.scan()[0], 1)

    def test_progress_scan_is_the_fallback_and_no_scanner_is_an_error(self):
        write_yaml(self.root / "program" / "checks.yaml", CHECKS)
        write(self.root / "a.md", "x\n")
        sh(self.root, "git", "add", "-A")
        code, _, err = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("needs a scanner", err)
        write_yaml(self.root / "program" / "checks.yaml",
                   dict(CHECKS, progress={"docs": ["a.md"], "state": ["b"], "scan": [[PY, "-c", SCANNER]]}))
        commit_all(self.root, "the scanner under progress (its own text holds the flagged word)")
        write(self.root / "a.md", "y\n")
        sh(self.root, "git", "add", "-A")
        code, out, _ = self.scan()
        self.assertEqual(code, 0, out)
        self.assertIn("of progress.scan over 1 staged file(s)", out)


class RecordScanTests(TrackerBase):
    def test_pk_record_scans_what_it_commits_when_secret_scan_is_set(self):
        write_yaml(self.root / "program" / "checks.yaml", dict(CHECKS, secret_scan=[[PY, "-c", SCANNER]]))
        commit_all(self.root, "checks")
        head = sh(self.root, "git", "rev-parse", "HEAD")
        code, out, err = self.cli("tracker", "record", "--ruling", "Quote SECRET-LITERAL in the ruling.")
        self.assertEqual(code, 1, out)
        self.assertIn("secret_scan", err)
        self.assertIn("leak: program/rulings.md", err)
        self.assertEqual(sh(self.root, "git", "rev-parse", "HEAD"), head)  # nothing committed
        self.assertEqual(self.dirty(), [])  # and nothing left written
        code, out, err = self.cli("tracker", "record", "--ruling", "A clean ruling.")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("committed", out)

    def test_without_secret_scan_pk_record_runs_no_scanner(self):
        write_yaml(self.root / "program" / "checks.yaml",
                   dict(CHECKS, progress={"docs": ["a.md"], "state": ["b"], "scan": [[PY, "-c", SCANNER]]}))
        commit_all(self.root, "checks")
        code, out, err = self.cli("tracker", "record", "--ruling", "Quote SECRET-LITERAL in the ruling.")
        self.assertEqual((code, err), (0, ""), out)  # opt-in: as in 0.6.0


class PrecheckScanTests(unittest.TestCase):
    """PC-8: the precheck runs the project's scanner over copies of the task folder, standin.patch included."""

    def setUp(self):
        self.root = make_repo(self)
        write(self.root / "src" / "a.py", "A = 1\n")
        write_yaml(self.root / "program" / "checks.yaml", dict(CHECKS, secret_scan=[[PY, "-c", SCANNER]]))
        commit_all(self.root, "main")
        self.folder = self.root / "program" / "tasks" / "T-1"
        write(self.folder / "PLAN.md", "# Plan\n\n## Exact Commands\n\n$ echo one\n")
        write(self.folder / "SPEC.md", "# Spec\n")

    def patch_with(self, line):
        git(self.root, "checkout", "-q", "-b", "standin")
        write(self.root / "src" / "a.py", f"A = 1\n{line}\n")
        git(self.root, "commit", "-qam", "standin")
        text = git(self.root, "diff", "main", "standin")
        git(self.root, "checkout", "-q", "main")
        write(self.folder / "standin.patch", text)

    def findings(self):
        return [str(f) for f in sp.precheck(self.folder, self.root, notes=[])]

    def test_a_flagged_literal_in_the_stand_in_is_a_finding(self):
        self.patch_with('KEY = "SECRET-LITERAL"')
        found = [line for line in self.findings() if line.startswith("PC-8 ")]
        self.assertEqual(len(found), 1, found)
        self.assertIn("leak: program/tasks/T-1/standin.patch", found[0])

    def test_a_clean_folder_has_no_scan_finding_and_no_scanner_is_a_note(self):
        self.patch_with("B = 2")
        self.assertEqual([line for line in self.findings() if line.startswith("PC-8 ")], [])
        write_yaml(self.root / "program" / "checks.yaml", CHECKS)
        commit_all(self.root, "no scanner")
        notes = []
        sp.precheck(self.folder, self.root, notes=notes)
        self.assertTrue(any("no secret scanner" in note for note in notes), notes)


class StaleBaseTests(unittest.TestCase):
    """PC-7: a stand-in hunk that undoes a commit on the base is a stale-base revert."""

    def setUp(self):
        self.root = make_repo(self)
        write(self.root / "src" / "link.test.ts",
              'const PIN_KEY = "arena.live.v1.pin";\nexpect(store.get(PIN_KEY)).toBe(1);\nother();\n')
        write(self.root / "src" / "b.ts", "export const B = 1;\n")
        commit_all(self.root, "the test as first written")
        self.old = sh(self.root, "git", "rev-parse", "HEAD").strip()
        write(self.root / "src" / "link.test.ts",
              'const PIN_SLOT = "arena.live.v1.pin";\nexpect(store.get(PIN_SLOT)).toBe(1);\nother();\n')
        commit_all(self.root, "rename the constant to PIN_SLOT")
        self.rename = sh(self.root, "git", "rev-parse", "HEAD").strip()
        self.folder = self.root.parent / "tasks-T-2"
        write(self.folder / "PLAN.md", "# Plan\n\n## Exact Commands\n\n$ echo one\n")
        write(self.folder / "SPEC.md", "# Spec\n")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def cut(self, files, base="main"):
        """The stand-in an author cuts on `base` with `files` as their working copies, as a diff against main."""
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        git(self.root, "worktree", "add", "-q", "--detach", str(work / "wt"), "main")
        for rel, text in files.items():
            write(work / "wt" / rel, text)
        git(work / "wt", "add", "-A")
        text = git(work / "wt", "diff", "--cached", "HEAD")
        git(self.root, "worktree", "remove", "--force", str(work / "wt"))
        write(self.folder / "standin.patch", text)
        return text

    def pc7(self):
        return [str(f) for f in sp.precheck(self.folder, self.root, notes=[]) if str(f).startswith("PC-7 ")]

    def test_a_hunk_that_renames_the_constant_back_names_the_rename_commit(self):
        self.cut({"src/link.test.ts": 'const PIN_KEY = "arena.live.v1.pin";\nexpect(store.get(PIN_KEY)).toBe(1);\n'
                                      "other();\nmore();\n",
                  "src/b.ts": "export const B = 2;\n"})
        found = self.pc7()
        self.assertEqual(len(found), 1, found)
        self.assertIn("src/link.test.ts", found[0])
        self.assertIn("hunk @@ -1,3 +1,4 @@", found[0])
        self.assertIn(f"undoes {self.rename[:12]} (rename the constant to PIN_SLOT)", found[0])
        self.assertIn("removes 2 line(s) it added, restores 2 line(s) it removed", found[0])

    def test_a_fresh_cut_that_changes_the_file_forward_is_clean(self):
        self.cut({"src/link.test.ts": 'const PIN_SLOT = "arena.live.v1.pin";\nexpect(store.get(PIN_SLOT)).toBe(2);\n'
                                      "other();\n"})
        self.assertEqual(self.pc7(), [])

    def test_with_a_recorded_base_a_hunk_that_only_removes_later_lines_is_a_finding_too(self):
        text = self.cut({"src/link.test.ts": 'expect(store.get(PIN_SLOT)).toBe(1);\nother();\n'})
        self.assertEqual(self.pc7(), [])  # removing a line alone is ordinary work without a recorded base
        write(self.folder / "standin.patch", text + f"\nbase-commit: {self.old}\n")
        found = self.pc7()
        self.assertEqual(len(found), 1, found)
        self.assertIn(f"undoes {self.rename[:12]}", found[0])
        self.assertIn("removes 1 line(s) it added", found[0])
        self.assertIn(f"after the patch's base-commit {self.old[:12]}", found[0])

    def test_a_given_base_ref_is_used(self):
        self.cut({"src/link.test.ts": 'const PIN_KEY = "arena.live.v1.pin";\nexpect(store.get(PIN_KEY)).toBe(1);\n'
                                      "other();\n"})
        git(self.root, "branch", "older", self.old)
        found = [str(f) for f in sp.precheck(self.folder, self.root, notes=[], base="older")]
        self.assertEqual([f for f in found if f.startswith("PC-7 ")], [])  # on the older base nothing is undone
        self.assertTrue(any(f.startswith("PC-4 ") and "older" in f for f in found), found)


if __name__ == "__main__":
    unittest.main()
