import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
import worktree
from common import KitError

T1, T2 = "P1-M01-S01-T01", "P1-M01-S01-T02"


def sh(cwd, *argv):
    return subprocess.run(argv, cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def commit_all(root, message):
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", message)


def make_repo(test) -> Path:
    """A throwaway git repository on branch main with one commit; removed when `test` finishes."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve()
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.name", "Test")
    sh(root, "git", "config", "user.email", "test@example.com")
    sh(root, "git", "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".worktrees/\n")
    (root / "README.md").write_text("hello\n")
    commit_all(root, "init")
    return root


def run_cli(module, root, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch(f"{module}.find_root", return_value=root):
        code = pk.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)

    def branches(self):
        return sh(self.root, "git", "branch", "--format=%(refname:short)").split()

    def test_new_creates_worktree_and_branch_and_refuses_duplicates(self):
        path = worktree.new(self.root, T1)
        self.assertEqual(path, self.root / ".worktrees" / T1.lower())
        self.assertTrue((path / "README.md").exists())
        self.assertIn("task/" + T1, self.branches())
        self.assertEqual(sh(path, "git", "symbolic-ref", "--short", "HEAD").strip(), "task/" + T1)
        with self.assertRaises(KitError) as caught:
            worktree.new(self.root, T1)  # directory and branch both exist
        self.assertIn(T1.lower(), str(caught.exception))
        # a branch alone is enough to refuse, and nothing is created
        sh(self.root, "git", "branch", "task/" + T2)
        with self.assertRaises(KitError) as caught:
            worktree.new(self.root, T2)
        self.assertIn("task/" + T2, str(caught.exception))
        self.assertFalse((self.root / ".worktrees" / T2.lower()).exists())
        # a directory alone is enough to refuse; the branch is not created
        t3 = "P1-M01-S01-T03"
        (self.root / ".worktrees" / t3.lower()).mkdir()
        with self.assertRaises(KitError):
            worktree.new(self.root, t3)
        self.assertNotIn("task/" + t3, self.branches())

    def test_new_refuses_a_bad_task_id_and_leaves_nothing_behind(self):
        for bad in ("", "P\u0661-M01-S01-T01", "p1-m01-s01-t01", "P1-M01-S01", "P1-M01-S01-T01/../x", "--force",
                    "P1-M01-S01-T01\n", "main"):
            with self.subTest(bad=bad), self.assertRaises(KitError):
                worktree.new(self.root, bad)
        self.assertFalse((self.root / ".worktrees").exists())
        self.assertEqual(self.branches(), ["main"])

    def test_new_refuses_when_main_does_not_resolve(self):
        with self.assertRaises(KitError) as caught:
            worktree.new(self.root, T1, main="trunk")
        self.assertIn("trunk", str(caught.exception))
        with self.assertRaises(KitError):
            worktree.new(self.root, T1, main="--orphan")
        self.assertFalse((self.root / ".worktrees").exists())
        self.assertEqual(self.branches(), ["main"])
        for bad in ("a\x00b", "a b", "a\nb", "a..b", "", "a\tb", "a\x7fb"):
            with self.subTest(main=bad), self.assertRaises(KitError):
                worktree.new(self.root, T1, main=bad)
        self.assertFalse((self.root / ".worktrees").exists())
        sh(self.root, "git", "branch", "trunk")  # the main branch name is a parameter
        self.assertEqual(worktree.new(self.root, T1, main="trunk"), self.root / ".worktrees" / T1.lower())

    def test_list_all_reports_branch_head_and_dirty(self):
        path = worktree.new(self.root, T1)
        other = worktree.new(self.root, T2)
        (path / "scratch.txt").write_text("x")
        found = {Path(item["path"]).name: item for item in worktree.list_all(self.root)}
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        self.assertEqual(found[T1.lower()], {"path": str(path), "branch": "task/" + T1, "head": head,
                                             "dirty": True, "missing": False})
        self.assertIs(found[T2.lower()]["dirty"], False)
        self.assertEqual(found[self.root.name]["branch"], "main")  # the main checkout is listed too
        self.assertEqual(other.name, T2.lower())

    def test_list_all_with_a_deleted_worktree_directory(self):
        path = worktree.new(self.root, T1)
        shutil.rmtree(path)
        found = {Path(item["path"]).name: item for item in worktree.list_all(self.root)}
        self.assertIsNone(found[T1.lower()]["dirty"])
        self.assertIs(found[T1.lower()]["missing"], True)
        self.assertEqual(found[T1.lower()]["branch"], "task/" + T1)

    def test_cli_new_and_list(self):
        code, out, err = run_cli("worktree", self.root, "worktree", "new", T1)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"{self.root / '.worktrees' / T1.lower()}\n")
        code, out, err = run_cli("worktree", self.root, "worktree", "new", T1)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        code, out, err = run_cli("worktree", self.root, "worktree", "list")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"task/{T1}", out)
        self.assertIn("clean", out)
        code, out, err = run_cli("worktree", self.root, "worktree")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: "))

    def test_there_is_no_remove_command(self):
        code, _, err = run_cli("worktree", self.root, "worktree", "remove", T1)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))


if __name__ == "__main__":
    unittest.main()
