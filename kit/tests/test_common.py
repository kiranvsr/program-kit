import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
from common import KitError


class CommonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_find_root_walks_up_to_program_dir(self):
        (self.tmp / "x" / "program").mkdir(parents=True)
        deep = self.tmp / "x" / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(common.find_root(deep), self.tmp / "x")

    def test_find_root_accepts_git_dir_and_prefers_nearest(self):
        (self.tmp / "outer" / "program").mkdir(parents=True)
        (self.tmp / "outer" / "inner" / ".git").mkdir(parents=True)
        self.assertEqual(common.find_root(self.tmp / "outer" / "inner"), self.tmp / "outer" / "inner")

    def test_find_root_raises_outside_any_project(self):
        with self.assertRaises(KitError):
            common.find_root(self.tmp)

    def test_write_yaml_is_atomic_and_round_trips(self):
        target = self.tmp / "sub" / "f.yaml"
        data = {"a": 1, "b": ["x", {"c": None}]}
        common.write_yaml(target, data)
        common.write_yaml(target, data)
        self.assertEqual(common.read_yaml(target), data)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["f.yaml"])

    def test_write_failure_leaves_no_temp_file_and_keeps_old_content(self):
        target = self.tmp / "f.yaml"
        common.write_yaml(target, {"ok": 1})
        with self.assertRaises(KitError):
            common.write_yaml(target, {"bad": object()})
        self.assertEqual(common.read_yaml(target), {"ok": 1})
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["f.yaml"])

    def test_write_text_is_public_atomic_and_keeps_the_mode(self):
        target = self.tmp / "deep" / "er" / "f.txt"
        common.write_text(target, "one\nA\u00e9\n")  # creates the parents
        self.assertEqual(target.read_bytes(), "one\nA\u00e9\n".encode("utf-8"))
        target.chmod(0o640)
        common.write_text(target, "two\n")
        self.assertEqual(target.read_text(), "two\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["f.txt"])  # no temp file left behind
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaisesRegex(KitError, r"cannot write .*f\.txt: boom"):
                common.write_text(target, "three\n")
        self.assertEqual(target.read_text(), "two\n")  # the old content survives a failed write
        self.assertEqual([p.name for p in target.parent.iterdir()], ["f.txt"])

    def test_only_common_uses_the_private_atomic_writer(self):
        for path in sorted(Path(common.__file__).parent.glob("*.py")):
            if path.name != "common.py":
                self.assertNotIn("_atomic_write", path.read_text(encoding="utf-8"), path.name)

    def test_read_yaml_errors_name_the_file(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("a: 1\nb: [2]\n")
        with self.assertRaisesRegex(KitError, r"bad\.yaml.*line 2"):
            common.read_yaml(bad)
        with self.assertRaisesRegex(KitError, "missing.yaml"):
            common.read_yaml(self.tmp / "missing.yaml")

    def test_write_json_is_atomic_indented_with_trailing_newline(self):
        target = self.tmp / "f.json"
        common.write_json(target, {"a": [1, 2], "b": {"c": "d"}})
        text = target.read_text()
        self.assertEqual(text, json.dumps({"a": [1, 2], "b": {"c": "d"}}, indent=2) + "\n")
        self.assertEqual(common.read_json(target), {"a": [1, 2], "b": {"c": "d"}})
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["f.json"])

    def test_read_json_errors_are_kit_errors_naming_the_file(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{nope")
        with self.assertRaisesRegex(KitError, r"bad\.json"):
            common.read_json(bad)
        with self.assertRaisesRegex(KitError, "missing.json"):
            common.read_json(self.tmp / "missing.json")

    def test_sha256_helpers_agree(self):
        f = self.tmp / "f.bin"
        f.write_bytes(b"hello")
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        self.assertEqual(common.sha256_bytes(b"hello"), expected)
        self.assertEqual(common.sha256_file(f), expected)

    def test_utc_now_format(self):
        self.assertRegex(common.utc_now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_run_argv_never_uses_a_shell(self):
        code, out = common.run_argv(["echo", "$HOME"], self.tmp)
        self.assertEqual((code, out.strip()), (0, "$HOME"))

    def test_run_argv_combines_streams_and_reports_exit_code(self):
        code, out = common.run_argv([sys.executable, "-c",
                                     "import sys; print('o'); print('e', file=sys.stderr); sys.exit(3)"], self.tmp)
        self.assertEqual(code, 3)
        self.assertIn("o", out)
        self.assertIn("e", out)

    def test_run_argv_env_cwd_and_timeout(self):
        code, out = common.run_argv([sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ['KIT_X'])"],
                                    self.tmp, env={"KIT_X": "yes", "PATH": __import__("os").environ["PATH"]})
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), [str(self.tmp), "yes"])
        code, out = common.run_argv([sys.executable, "-c", "import time; time.sleep(5)"], self.tmp, timeout=1)
        self.assertEqual(code, 124)
        self.assertIn("timed out", out)

    def test_run_argv_missing_executable(self):
        code, out = common.run_argv(["definitely-not-a-real-binary-xyz"], self.tmp)
        self.assertEqual(code, 127)
        self.assertIn("definitely-not-a-real-binary-xyz", out)

    def test_git_returns_stripped_stdout(self):
        common.git(self.tmp, "init", "-q")
        self.assertEqual(common.git(self.tmp, "rev-parse", "--is-inside-work-tree"), "true")

    def test_git_turns_an_unusable_argument_into_kiterror(self):
        common.git(self.tmp, "init", "-q")
        with self.assertRaises(KitError) as caught:
            common.git(self.tmp, "rev-parse", "a\x00b")
        self.assertEqual(str(caught.exception), "git: invalid argument")

    def test_git_raises_kiterror_with_stderr(self):
        common.git(self.tmp, "init", "-q")
        with self.assertRaises(KitError) as ctx:
            common.git(self.tmp, "rev-parse", "nope")
        # "ambiguous argument" appears only in git's stderr, not in the echoed args
        self.assertRegex(str(ctx.exception), r"fatal: .*(ambiguous argument|unknown revision)")
        self.assertNotIn("\n", str(ctx.exception))

    def test_git_error_falls_back_to_stdout_and_then_to_the_exit_code(self):
        def failing(stdout, stderr):
            return mock.patch.object(common.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["git"], 3, stdout=stdout, stderr=stderr))

        for stdout, stderr, reason in (("on stdout\n", "", "on stdout"),
                                       ("on stdout\n", " \n\t\n", "on stdout"),  # blank stderr is no reason
                                       ("on stdout\n", "warning: w\nfatal: on stderr\n", "fatal: on stderr"),
                                       ("", "", "exit 3"),
                                       (" \n", "\n \n", "exit 3")):
            with self.subTest(stdout=stdout, stderr=stderr), failing(stdout, stderr):
                with self.assertRaises(KitError) as ctx:
                    common.git(self.tmp, "merge", "x")
                self.assertEqual(str(ctx.exception), f"git merge x failed: {reason}")

    def test_no_test_of_this_module_starts_a_bare_python3(self):
        # A child interpreter is the one running the suite: `python3` on PATH may be another version or absent.
        self.assertNotIn('["python' + '3"', Path(__file__).read_text(encoding="utf-8"))

    def test_nested_lists_round_trip_through_the_yaml_writer_and_reader(self):
        data = {"build": [["make"], ["go", "build", "./..."]],
                "deep": [[["a", "b"], ["c"]], [["d"]], "e"],
                "rows": [{"argv": [["x", "y"], ["z"]], "n": 1}, [{"k": ["v", ["w"]]}]],
                "empty": [[], [[]]]}
        target = self.tmp / "nested.yaml"
        common.write_yaml(target, data)
        text = target.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\[[^\]]")  # block style: no flow sequence with anything in it
        self.assertEqual(common.read_yaml(target), data)
        common.write_yaml(target, common.read_yaml(target))
        self.assertEqual(target.read_text(encoding="utf-8"), text)  # a second trip changes nothing

    def test_nested_lists_read_with_the_sequence_at_its_keys_indent(self):
        target = self.tmp / "flush.yaml"
        target.write_text("build:\n- - make\n- - go\n  - build\ndeep:\n- - - a\n    - b\n  - - c\n- e\n"
                          "outer:\n  inner:\n  - - x\n    - y\n  - z\n", encoding="utf-8")
        expected = {"build": [["make"], ["go", "build"]], "deep": [[["a", "b"], ["c"]], "e"],
                    "outer": {"inner": [["x", "y"], "z"]}}
        self.assertEqual(common.read_yaml(target), expected)
        common.write_yaml(target, expected)  # the writer's own layout reads back the same
        self.assertEqual(common.read_yaml(target), expected)


class WriteFailureTests(unittest.TestCase):
    """Finding 12: a write that fails is one KitError line, never an OSError traceback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def writers(self, target):
        return (lambda: common.write_text(target, "x\n"), lambda: common.write_json(target, {"a": 1}),
                lambda: common.write_yaml(target, {"a": 1}))

    def test_a_parent_that_is_a_file_is_a_kit_error(self):
        (self.tmp / "blocker").write_text("a file, not a directory\n")
        for write in self.writers(self.tmp / "blocker" / "sub" / "f.txt"):
            with self.assertRaisesRegex(KitError, r"cannot write .*f\.txt"):
                write()

    def test_a_target_that_is_a_directory_is_a_kit_error(self):
        (self.tmp / "dir").mkdir()
        for write in self.writers(self.tmp / "dir"):
            with self.assertRaisesRegex(KitError, r"cannot write .*dir"):
                write()
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["dir"])  # no temp file left behind

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_a_read_only_directory_is_a_kit_error(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        for write in self.writers(locked / "f.txt"):
            with self.assertRaises(KitError) as caught:
                write()
            self.assertNotIn("\n", str(caught.exception))
            self.assertIn("f.txt", str(caught.exception))

    def test_text_that_cannot_be_encoded_is_a_kit_error(self):
        with self.assertRaisesRegex(KitError, r"cannot write .*f\.txt"):
            common.write_text(self.tmp / "f.txt", "bad \ud800 surrogate")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_failure_while_writing_the_temp_file_is_a_kit_error(self):
        with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(KitError, "No space left on device"):
                common.write_text(self.tmp / "f.txt", "x\n")
        self.assertEqual(list(self.tmp.iterdir()), [])


class MalformedInputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_deeply_nested_yaml_is_a_kit_error(self):
        path = self.tmp / "deep.yaml"
        path.write_text("".join(" " * i + "k:\n" for i in range(3000)) + " " * 3000 + "v: 1\n")
        with self.assertRaisesRegex(KitError, r"deep\.yaml.*nested too deeply"):
            common.read_yaml(path)
        nest: object = 1
        for _ in range(3000):
            nest = [nest]
        with self.assertRaisesRegex(KitError, "nested too deeply"):
            common.write_yaml(self.tmp / "out.yaml", nest)
        self.assertEqual(list(self.tmp.glob("out*")), [])

    def test_non_utf8_and_binary_files_are_kit_errors(self):
        path = self.tmp / "bin.yaml"
        path.write_bytes(b"a: \xff\xfe\n")
        for read in (common.read_yaml, common.read_json):
            with self.assertRaisesRegex(KitError, r"bin\.yaml"):
                read(path)

    def test_a_leading_byte_order_mark_is_stripped_from_yaml_and_json(self):
        yaml_path, json_path = self.tmp / "b.yaml", self.tmp / "b.json"
        yaml_path.write_bytes(b"\xef\xbb\xbfbuild:\n  - - make\n")
        json_path.write_bytes(b"\xef\xbb\xbf{\"a\": 1}\n")
        self.assertEqual(common.read_yaml(yaml_path), {"build": [["make"]]})
        self.assertEqual(common.read_json(json_path), {"a": 1})

    def test_read_text_is_the_shared_reader_and_drops_only_a_leading_byte_order_mark(self):
        path = self.tmp / "t.md"
        path.write_bytes(b"\xef\xbb\xbf# Title\r\nbody \xef\xbb\xbf kept\n")
        self.assertEqual(common.read_text(path), "# Title\nbody \ufeff kept\n")
        self.assertEqual(common.read_text(path, newline=""), "# Title\r\nbody \ufeff kept\n")  # bytes as they are
        path.write_bytes(b"plain\n")
        self.assertEqual(common.read_text(path), "plain\n")
        self.assertEqual(common.read_text(str(path)), "plain\n")

    def test_read_text_failures_are_one_line_kit_errors_naming_the_file(self):
        bad = self.tmp / "bad.md"
        bad.write_bytes(b"caf\xe9\n")
        for path, why in ((self.tmp / "absent.md", "No such file"), (self.tmp, "directory"), (bad, "utf-8")):
            with self.subTest(path=path.name), self.assertRaises(KitError) as ctx:
                common.read_text(path)
            message = str(ctx.exception)
            self.assertTrue(message.startswith(f"cannot read {path}: "), message)
            self.assertIn(why, message)
            self.assertNotIn("\n", message)

    def test_no_script_of_mine_decodes_a_file_without_the_shared_reader(self):
        # Blobs read from git go through drop_bom; nothing reads a file as text behind the reader's back.
        scripts = Path(common.__file__).parent
        for name in ("gate.py", "status.py", "evidence.py", "stale.py", "fetch.py", "pk.py", "manifest.py",
                     "handoff.py", "worktree.py"):
            text = (scripts / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\.read_text\(|read_bytes\(\)\.decode\(", text), name)


class GitignoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def test_lines_are_the_three_the_kit_needs(self):
        self.assertEqual(common.GITIGNORE_LINES, ("__pycache__/", "*.pyc", ".worktrees/"))

    def test_creates_the_file_when_absent(self):
        self.assertEqual(common.ensure_gitignore(self.root), list(common.GITIGNORE_LINES))
        self.assertEqual((self.root / ".gitignore").read_text(), "__pycache__/\n*.pyc\n.worktrees/\n")

    def test_appends_only_the_missing_lines_and_keeps_existing_bytes(self):
        original = b"# mine\r\nnode_modules/\r\n*.pyc\r\n.env"  # CRLF, no final newline
        (self.root / ".gitignore").write_bytes(original)
        self.assertEqual(common.ensure_gitignore(self.root), ["__pycache__/", ".worktrees/"])
        data = (self.root / ".gitignore").read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertEqual(data[len(original):], b"\n__pycache__/\n.worktrees/\n")

    def test_a_complete_file_is_left_alone(self):
        text = "x\n.worktrees/\n__pycache__/\n*.pyc\n"
        (self.root / ".gitignore").write_text(text)
        before = (self.root / ".gitignore").stat().st_mtime_ns
        self.assertEqual(common.ensure_gitignore(self.root), [])
        self.assertEqual((self.root / ".gitignore").read_text(), text)
        self.assertEqual((self.root / ".gitignore").stat().st_mtime_ns, before)

    def test_an_unwritable_gitignore_is_a_kit_error(self):
        (self.root / ".gitignore").mkdir()
        with self.assertRaisesRegex(KitError, r"\.gitignore"):
            common.ensure_gitignore(self.root)


class PrimaryBranchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def repo(self, branch):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)

    def commit(self):
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=T", "-c", "user.email=t@e.x",
                        "commit", "-q", "--allow-empty", "-m", "c"], check=True)

    def test_a_repository_on_master_names_the_rename(self):
        self.repo("master")
        self.commit()
        message = common.primary_branch_problem(self.root)
        self.assertIn("main", message)
        self.assertIn("git branch -m main", message)
        self.assertNotIn("\n", message)

    def test_main_is_fine_with_or_without_commits(self):
        self.repo("main")
        self.assertIsNone(common.primary_branch_problem(self.root))  # unborn main: allowed
        self.assertIn("no commits", common.primary_branch_problem(self.root, need_commit=True))
        self.commit()
        self.assertIsNone(common.primary_branch_problem(self.root, need_commit=True))

    def test_main_exists_while_another_branch_is_checked_out(self):
        self.repo("main")
        self.commit()
        subprocess.run(["git", "-C", str(self.root), "checkout", "-q", "-b", "feature"], check=True)
        self.assertIsNone(common.primary_branch_problem(self.root, need_commit=True))

    def test_not_a_repository_is_reported(self):
        self.assertIn("git", common.primary_branch_problem(self.root))


if __name__ == "__main__":
    unittest.main()
