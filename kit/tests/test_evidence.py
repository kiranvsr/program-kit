import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evidence
import pk
import store
from common import KitError, read_json, sha256_bytes

PY = sys.executable
BUILD = [PY, "-m", "compileall", "-q", "."]
TEST = [PY, "-m", "unittest", "-q"]
CALC = "def add(a, b):\n    return a + b\n"
CALC_TEST = ("import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n\n"
             "if __name__ == '__main__':\n    unittest.main()\n")
SELF_TEST = "self-test-noncompiling"


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def make_repo():
    """A throwaway repository on branch main with a tiny module, its unittest and a .gitignore."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name).resolve()
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.name", "Test")
    sh(root, "git", "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (root / "calc.py").write_text(CALC)
    (root / "test_calc.py").write_text(CALC_TEST)
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("a\n")
    (root / "sub" / "b.txt").write_text("b\n")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "init")
    return tmp, root


def row(rid, find="a + b", replace="a - b", path="calc.py", test_args=("test_calc",)):
    return {"id": rid, "path": path, "find": find, "replace": replace, "test_args": list(test_args)}


def worktrees(root):
    return [ln for ln in sh(root, "git", "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]


@contextlib.contextmanager
def spy_mkdtemp():
    """Record every directory evidence creates with tempfile.mkdtemp."""
    made, real = [], tempfile.mkdtemp

    def spy(*a, **kw):
        made.append(real(*a, **kw))
        return made[-1]

    with mock.patch.object(evidence.tempfile, "mkdtemp", spy):
        yield made


# what a hand-written planted-bug row must also carry: the edit and the commands that judged it
PLANTED = {"path": "calc.py", "find": "a", "replace": "b", "build_argv": ["true"], "test_argv": ["true"]}


class Base(unittest.TestCase):
    def setUp(self):
        tmp, self.root = make_repo()
        self.addCleanup(tmp.cleanup)

    def appended(self):
        """The record files (every kind, every task)."""
        return sorted((self.root / store.BASE).glob("*/*/*.json"))

    def ready(self, **kw):
        """An Evidence with one command, hashed inputs and planted bugs: writable."""
        ev = evidence.Evidence(self.root, "T-1")
        ev.hash_inputs(["calc.py", "test_calc.py"])
        ev.run_command("tests", TEST + ["test_calc"])
        ev.planted_bugs([row("flip")], BUILD, TEST)
        return ev


class HashInputs(Base):
    def test_hash_inputs_uses_committed_blob_not_working_tree(self):
        ev = evidence.Evidence(self.root, "T-1")
        (self.root / "calc.py").write_text("def add(a, b):\n    return 0\n")  # dirty after construction
        got = ev.hash_inputs(["calc.py"])
        self.assertEqual(got, {"calc.py": hashlib.sha256(CALC.encode()).hexdigest()})

    def test_hash_inputs_refuses_untracked_path_naming_it(self):
        ev = evidence.Evidence(self.root, "T-1")
        (self.root / "new.py").write_text("x = 1\n")
        with self.assertRaises(KitError) as ctx:
            ev.hash_inputs(["new.py"])
        self.assertIn("new.py", str(ctx.exception))

    def test_hash_inputs_refuses_parent_and_absolute_paths(self):
        ev = evidence.Evidence(self.root, "T-1")
        for bad in ("../calc.py", "sub/../../x", str(self.root / "calc.py"), ""):
            with self.assertRaises(KitError, msg=bad):
                ev.hash_inputs([bad])

    def test_hash_inputs_expands_directories_sorted(self):
        ev = evidence.Evidence(self.root, "T-1")
        got = ev.hash_inputs(["sub", "calc.py"])
        self.assertEqual(list(got), ["calc.py", "sub/a.txt", "sub/b.txt"])
        self.assertTrue(all(len(v) == 64 for v in got.values()))

    def test_root_must_be_the_git_top_level(self):
        with self.assertRaises(KitError) as ctx:
            evidence.Evidence(self.root / "sub", "T-1")
        self.assertIn("top level", str(ctx.exception))

    def test_heads_recorded(self):
        ev = evidence.Evidence(self.root, "T-1")
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        self.assertEqual((ev.head, ev.main_sha, ev.merge_base), (head, head, head))


class RunCommand(Base):
    def test_run_command_records_exit_and_parsed_fields(self):
        ev = evidence.Evidence(self.root, "T-1")
        code = "import sys; print('count=3'); print('warn', file=sys.stderr); sys.exit(2)"
        rec = ev.run_command("probe", [PY, "-c", code], expect=2,
                             parse=lambda out: {"count": int(out.split("count=")[1].split()[0])})
        self.assertEqual((rec["label"], rec["exit"], rec["expected_exit"], rec["as_expected"]),
                         ("probe", 2, 2, True))
        self.assertEqual(rec["derived"], {"count": 3})
        self.assertEqual(rec["argv"], [PY, "-c", code])
        self.assertEqual(len(rec["output_sha256"]), 64)
        self.assertIn("count=3", rec["output_tail"])
        self.assertIn("warn", rec["output_tail"])
        self.assertEqual(ev.commands, [rec])

    def test_exit_mismatch_is_not_as_expected(self):
        ev = evidence.Evidence(self.root, "T-1")
        rec = ev.run_command("fail", [PY, "-c", "raise SystemExit(1)"])
        self.assertFalse(rec["as_expected"])

    def test_output_tail_is_last_2000_chars_and_hash_covers_all(self):
        ev = evidence.Evidence(self.root, "T-1")
        rec = ev.run_command("long", [PY, "-c", "print('x' * 5000 + 'END')"])
        self.assertEqual(len(rec["output_tail"]), 2000)
        self.assertTrue(rec["output_tail"].endswith("END\n"))
        self.assertEqual(rec["output_sha256"], hashlib.sha256(("x" * 5000 + "END\n").encode()).hexdigest())

    def test_parse_that_raises_or_returns_non_dict_is_not_as_expected(self):
        ev = evidence.Evidence(self.root, "T-1")

        def boom(out):
            raise ValueError("no count")

        rec = ev.run_command("a", [PY, "-c", "print(1)"], parse=boom)
        self.assertEqual((rec["exit"], rec["as_expected"], rec["derived"]), (0, False, {}))
        rec = ev.run_command("b", [PY, "-c", "print(1)"], parse=lambda out: ["list"])
        self.assertEqual((rec["as_expected"], rec["derived"]), (False, {}))

    def test_cwd_defaults_to_root(self):
        ev = evidence.Evidence(self.root, "T-1")
        rec = ev.run_command("cwd", [PY, "-c", "import os; print(os.getcwd())"])
        self.assertEqual(Path(rec["output_tail"].strip()).resolve(), self.root)

    def test_command_must_be_argv_list(self):
        ev = evidence.Evidence(self.root, "T-1")
        with self.assertRaises(KitError):
            ev.run_command("shell", "echo hi")


class PlantedBugs(Base):
    def test_planted_bug_counts_only_test_failure(self):
        ev = evidence.Evidence(self.root, "T-1")
        rows = ev.planted_bugs([
            row("flip"),
            row("syntax", find="return a + b", replace="return a +"),
            row("noop", find="return a + b", replace="return b + a"),
        ], BUILD, TEST)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual((by_id["flip"]["builds"], by_id["flip"]["killed"], by_id["flip"]["kill_reason"]),
                         (True, True, "test failed"))
        self.assertEqual((by_id["syntax"]["builds"], by_id["syntax"]["killed"], by_id["syntax"]["kill_reason"]),
                         (False, False, "build failed"))
        self.assertEqual((by_id["noop"]["builds"], by_id["noop"]["killed"], by_id["noop"]["kill_reason"]),
                         (True, False, "not killed"))
        self.assertEqual(len(by_id["flip"]["test_output_sha256"]), 64)

    def test_rows_record_what_was_planted_and_the_commands_that_ran(self):
        ev = evidence.Evidence(self.root, "T-1")
        flip, noop, self_test = ev.planted_bugs(
            [row("flip"), row("noop", find="return a + b", replace="return b + a", test_args=())], BUILD, TEST)
        self.assertEqual({key: flip[key] for key in evidence.PLANTED_KEYS},
                         {"path": "calc.py", "find": "a + b", "replace": "a - b", "build_argv": BUILD,
                          "test_argv": TEST + ["test_calc"]})
        self.assertEqual((noop["find"], noop["replace"], noop["test_argv"]), ("return a + b", "return b + a", TEST))
        # the self-test row: nothing is replaced, the unparseable line is appended to the first row's file
        self.assertEqual({key: self_test[key] for key in evidence.PLANTED_KEYS},
                         {"path": "calc.py", "find": "", "replace": evidence.BROKEN_LINE, "build_argv": BUILD,
                          "test_argv": TEST + ["test_calc"]})
        for item in (flip, noop, self_test):
            self.assertIsNot(item["test_argv"], TEST)
            self.assertIsNot(item["build_argv"], BUILD)

    def test_a_test_that_is_not_a_test_shows_in_the_row(self):
        ev = evidence.Evidence(self.root, "T-1")
        comment = row("comment-only", find="def add(a, b):", replace="def add(a, b):  # same code", test_args=())
        rows = ev.planted_bugs([comment], BUILD, ["git", "diff", "--quiet"])
        # still counted as killed: the runner cannot know the command is not a test, so the row has to say what ran
        self.assertEqual((rows[0]["killed"], rows[0]["kill_reason"]), (True, "test failed"))
        self.assertEqual(rows[0]["test_argv"], ["git", "diff", "--quiet"])
        self.assertEqual((rows[0]["path"], rows[0]["replace"]), ("calc.py", "def add(a, b):  # same code"))

    def test_self_test_row_always_present_and_not_killed(self):
        ev = evidence.Evidence(self.root, "T-1")
        rows = ev.planted_bugs([row("flip")], BUILD, TEST)
        self.assertEqual([r["id"] for r in rows], ["flip", SELF_TEST])
        self.assertFalse(rows[-1]["builds"])
        self.assertFalse(rows[-1]["killed"])

    def test_self_test_row_killed_means_runner_cannot_be_trusted(self):
        ev = evidence.Evidence(self.root, "T-1")
        always_builds = [PY, "-c", "pass"]  # a build step that accepts a file that cannot compile
        with self.assertRaises(KitError) as ctx:
            ev.planted_bugs([row("flip")], always_builds, TEST)
        self.assertIn("cannot be trusted", str(ctx.exception))

    def test_build_none_cannot_tell_build_failure_from_test_failure(self):
        ev = evidence.Evidence(self.root, "T-1")
        with self.assertRaises(KitError) as ctx:
            ev.planted_bugs([row("flip")], None, TEST)
        self.assertEqual(str(ctx.exception), "self-test cannot be trusted: without a build step a build failure "
                         "cannot be told from a test failure; pass a build command")
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_build_none_self_test_that_passes_is_untrusted(self):
        ev = evidence.Evidence(self.root, "T-1")
        with self.assertRaises(KitError) as ctx:
            ev.planted_bugs([row("flip")], None, [PY, "-c", "pass"])
        self.assertIn("cannot be trusted", str(ctx.exception))

    def test_build_none_missing_test_program_is_build_failed(self):
        ev = evidence.Evidence(self.root, "T-1")
        with self.assertRaises(KitError):  # the self-test cannot fail "not passing" through a missing program
            ev.planted_bugs([row("flip")], None, ["no-such-program-pk"])

    HANGS_ON_A_BROKEN_FILE = ("import sys, time\ntext = open('calc.py').read()\n"
                              "if 'def (:' in text:\n    time.sleep(60)\n"
                              "sys.exit(1 if 'a - b' in text else 0)\n")

    def test_build_none_self_test_that_times_out_names_the_timeout(self):
        ev = evidence.Evidence(self.root, "T-1")
        with self.assertRaises(KitError) as ctx:
            ev.planted_bugs([row("flip", test_args=[])], None, [PY, "-c", self.HANGS_ON_A_BROKEN_FILE], timeout=2)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("self-test cannot be trusted: the test timed out after 2s"), message)
        self.assertIn("calc.py", message)
        self.assertIn("pass a build command", message)
        self.assertNotIn("\n", message)
        self.assertEqual(ev.planted_bug_rows, [])  # nothing was recorded as "build failed"
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_with_a_build_step_a_test_that_times_out_is_still_only_a_row_that_does_not_build(self):
        ev = evidence.Evidence(self.root, "T-1")
        hang = row("hang", find="return a + b", replace="while True: pass")
        rows = ev.planted_bugs([hang], BUILD, TEST, timeout=3)
        self.assertEqual([(r["id"], r["builds"], r["kill_reason"]) for r in rows],
                         [("hang", False, "build failed"), (SELF_TEST, False, "build failed")])

    def failing_restore(self, ev, fail_from=1):
        """Make the restore step of `ev` fail from its `fail_from`-th call on; returns the list of calls."""
        calls, real = [], ev._restore

        def restore(mirror):
            calls.append(mirror)
            if len(calls) >= fail_from:
                raise KitError("git clean -fdxq failed: disk is full")
            real(mirror)

        patcher = mock.patch.object(ev, "_restore", restore)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_a_restore_failure_does_not_replace_the_error_of_the_row(self):
        ev = evidence.Evidence(self.root, "T-1")
        self.failing_restore(ev)
        with self.assertRaises(KitError) as ctx:
            ev.planted_bugs([row("flip")], BUILD, [PY, "-c", "raise SystemExit(1)"])
        message = str(ctx.exception)
        first, second = "unmutated mirror fails its own test (exit 1)", "the mirror could not be restored: git clean"
        self.assertIn(first, message)
        self.assertIn(second, message)
        self.assertLess(message.index(first), message.index(second))  # the row's own error comes first
        self.assertNotIn("\n", message)
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_a_restore_failure_does_not_hide_the_result_of_the_row(self):
        ev = evidence.Evidence(self.root, "T-1")
        calls = self.failing_restore(ev, fail_from=2)  # the baseline restores; the first planted bug cannot
        with self.assertRaises(KitError) as ctx:
            ev.planted_bugs([row("flip"), row("again", replace="a * b")], BUILD, TEST)
        self.assertEqual(len(calls), 2)  # nothing more ran on a mirror that is no longer HEAD
        message = str(ctx.exception)
        self.assertRegex(message, r"\Aplanted bug flip gave builds: true, test exit: 1; "
                                  r"the mirror could not be restored: git clean -fdxq failed: disk is full\Z")
        self.assertEqual(ev.planted_bug_rows, [])

    def test_a_restore_failure_is_added_to_an_error_that_is_not_the_kits(self):
        ev = evidence.Evidence(self.root, "T-1")
        self.failing_restore(ev)
        real_run = evidence.run_argv

        def exploding(argv, cwd, *a, **kw):
            if "unittest" in argv:
                raise RuntimeError("runner exploded")
            return real_run(argv, cwd, *a, **kw)

        with mock.patch.object(evidence, "run_argv", exploding):
            with self.assertRaises(RuntimeError) as ctx:
                ev.planted_bugs([row("flip")], BUILD, TEST)
        self.assertEqual(str(ctx.exception), "runner exploded")  # the row's own exception, unchanged
        self.assertEqual(ctx.exception.__notes__,
                         ["the mirror could not be restored: git clean -fdxq failed: disk is full"])

    def test_a_mirror_removal_failure_does_not_replace_the_error_of_the_row(self):
        ev = evidence.Evidence(self.root, "T-1")
        real_git = evidence.git

        def failing_git(root, *args):
            if args[:2] == ("worktree", "remove"):
                raise KitError("git worktree remove failed: locked")
            return real_git(root, *args)

        with spy_mkdtemp() as made:
            try:
                with mock.patch.object(evidence, "git", failing_git):
                    with self.assertRaises(KitError) as ctx:
                        ev.planted_bugs([row("flip")], BUILD, [PY, "-c", "raise SystemExit(1)"])
            finally:  # the test, not the code under test, cleans up what it forced to leak
                sh(self.root, "git", "worktree", "remove", "--force", made[0])
        message = str(ctx.exception)
        self.assertIn("unmutated mirror fails its own test (exit 1)", message)
        self.assertIn(f"cannot remove mirror {made[0]}", message)
        self.assertLess(message.index("unmutated mirror"), message.index("cannot remove mirror"))

    def test_find_occurring_zero_or_twice_is_a_table_error_before_anything_runs(self):
        (self.root / "dup.py").write_text("x = 1\nx = 1\n")
        sh(self.root, "git", "add", "dup.py")
        sh(self.root, "git", "commit", "-q", "-m", "dup")
        ev = evidence.Evidence(self.root, "T-1")
        with mock.patch.object(evidence, "run_argv") as runner:
            for bad in (row("none", find="nope"), row("twice", find="x = 1", path="dup.py")):
                with self.assertRaises(KitError) as ctx:
                    ev.planted_bugs([row("ok"), bad], BUILD, TEST)
                self.assertIn(bad["id"], str(ctx.exception))
            runner.assert_not_called()
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_table_errors_for_untracked_outside_and_duplicate_ids(self):
        (self.root / "loose.py").write_text("a = 1\n")
        ev = evidence.Evidence(self.root, "T-1")
        for bad in (row("u", path="loose.py", find="a = 1"), row("o", path="../calc.py"),
                    row("p", path=str(self.root / "calc.py")), row(SELF_TEST)):
            with self.assertRaises(KitError, msg=bad["id"]):
                ev.planted_bugs([bad], BUILD, TEST)
        with self.assertRaises(KitError):
            ev.planted_bugs([row("same"), row("same")], BUILD, TEST)
        with self.assertRaises(KitError):
            ev.planted_bugs([], BUILD, TEST)

    def test_timeout_is_never_a_kill(self):
        ev = evidence.Evidence(self.root, "T-1")
        hang = row("hang", find="return a + b", replace="while True: pass")
        rows = ev.planted_bugs([hang], BUILD, TEST, timeout=3)
        self.assertEqual((rows[0]["builds"], rows[0]["killed"], rows[0]["kill_reason"]),
                         (False, False, "build failed"))

    def test_symlink_row_is_a_table_error_and_outside_file_is_untouched(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other).resolve() / "outside_target.py"
            outside.write_text("secret = 1\n")
            os.symlink(outside, self.root / "link.py")
            sh(self.root, "git", "add", "link.py")
            sh(self.root, "git", "commit", "-q", "-m", "link")
            ev = evidence.Evidence(self.root, "T-1")
            with spy_mkdtemp() as made, self.assertRaises(KitError) as ctx:
                ev.planted_bugs([row("sym", path="link.py", find="outside_target", replace="x")], BUILD, TEST)
            self.assertIn("120000", str(ctx.exception))
            self.assertEqual(made, [])
            self.assertEqual(outside.read_bytes(), b"secret = 1\n")
            self.assertEqual(ev.hash_inputs(["link.py"]),  # hashed as link text, never followed
                             {"link.py": hashlib.sha256(str(outside).encode()).hexdigest()})

    def test_baseline_failure_removes_the_mirror(self):
        ev = evidence.Evidence(self.root, "T-1")
        with spy_mkdtemp() as made, self.assertRaises(KitError) as ctx:
            ev.planted_bugs([row("flip")], BUILD, [PY, "-c", "raise SystemExit(1)"])
        self.assertIn("unmutated", str(ctx.exception))
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_table_error_creates_no_mirror_directory(self):
        ev = evidence.Evidence(self.root, "T-1")
        with spy_mkdtemp() as made:
            for bad in (row("none", find="nope"), row("same", replace="a + b"), row("p", path="../calc.py")):
                with self.assertRaises(KitError):
                    ev.planted_bugs([bad], BUILD, TEST)
        self.assertEqual(made, [])
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_next_row_starts_from_head_after_a_test_dirties_the_whole_mirror(self):
        script = ("import os, sys\n"
                  "if sys.argv[1] == 'dirty':\n"
                  "    open('junk.txt', 'w').write('x')\n"
                  "    open('sub/a.txt', 'a').write('more')\n"
                  "    sys.exit(1 if 'a - b' in open('calc.py').read() else 0)\n"
                  "clean = not os.path.exists('junk.txt') and open('sub/a.txt').read() == 'a\\n'\n"
                  "sys.exit(0 if clean else 1)\n")
        ev = evidence.Evidence(self.root, "T-1")
        rows = ev.planted_bugs([row("first", test_args=["dirty"]),
                                row("second", replace="a * b", test_args=["check"])],
                               BUILD, [PY, "-c", script])
        self.assertTrue(rows[0]["killed"])
        self.assertFalse(rows[1]["killed"], "the second row saw files left by the first")

    def test_restore_refuses_anything_but_its_own_mirror(self):
        ev = evidence.Evidence(self.root, "T-1")
        (self.root / "keep.txt").write_text("untracked")
        with self.assertRaises(KitError):
            ev._restore(self.root)
        self.assertTrue((self.root / "keep.txt").exists())

    def test_working_tree_untouched_and_mirror_removed(self):
        ev = evidence.Evidence(self.root, "T-1")
        ev.planted_bugs([row("flip"), row("again", find="a + b", replace="a * b")], BUILD, TEST)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "")
        self.assertEqual((self.root / "calc.py").read_text(), CALC)
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_each_row_starts_from_a_clean_checkout(self):
        # row two's find would not occur in a file left mutated by row one
        ev = evidence.Evidence(self.root, "T-1")
        rows = ev.planted_bugs([row("flip"), row("pad", find="a + b", replace="a + b  ")], BUILD, TEST)
        self.assertTrue(rows[0]["killed"])
        self.assertFalse(rows[1]["killed"])
        self.assertEqual(rows[1]["kill_reason"], "not killed")

    def test_mirror_removed_even_when_a_row_test_raises(self):
        ev = evidence.Evidence(self.root, "T-1")
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def spy(*a, **kw):
            made.append(real_mkdtemp(*a, **kw))
            return made[-1]

        real_run = evidence.run_argv

        def exploding(argv, cwd, *a, **kw):
            if "unittest" in argv:
                raise RuntimeError("runner exploded")
            return real_run(argv, cwd, *a, **kw)

        with mock.patch.object(evidence.tempfile, "mkdtemp", spy), \
                mock.patch.object(evidence, "run_argv", exploding):
            with self.assertRaises(RuntimeError):
                ev.planted_bugs([row("flip")], BUILD, TEST)
        self.assertEqual(len(made), 1)
        self.assertTrue(os.path.basename(made[0]).startswith("pk-mirror-"))
        self.assertFalse(os.path.exists(made[0]))
        self.assertEqual(len(worktrees(self.root)), 1)

    def test_mirror_removal_failure_raises_naming_the_path(self):
        ev = evidence.Evidence(self.root, "T-1")
        made = []
        real_mkdtemp = tempfile.mkdtemp
        real_git = evidence.git

        def spy(*a, **kw):
            made.append(real_mkdtemp(*a, **kw))
            return made[-1]

        def failing_git(root, *args):
            if args[:2] == ("worktree", "remove"):
                raise KitError("git worktree remove failed: locked")
            return real_git(root, *args)

        try:
            with mock.patch.object(evidence.tempfile, "mkdtemp", spy), \
                    mock.patch.object(evidence, "git", failing_git):
                with self.assertRaises(KitError) as ctx:
                    ev.planted_bugs([row("flip")], BUILD, TEST)
            self.assertIn(made[0], str(ctx.exception))
        finally:  # the test, not the code under test, cleans up what it forced to leak
            sh(self.root, "git", "worktree", "remove", "--force", made[0])


class Write(Base):
    def test_write_creates_schema_shaped_file(self):
        ev = self.ready()
        written = ev.write()
        out = written["file"]
        self.assertEqual(out, self.root / written["path"])
        self.assertEqual(written["digest"], sha256_bytes(out.read_bytes()))
        data = read_json(out)
        for key in evidence.required_keys("results"):
            self.assertIn(key, data)
        self.assertEqual(data["task_id"], "T-1")
        self.assertTrue(data["planted_bugs_all_killed"])
        self.assertTrue(data["all_commands_as_expected"])
        self.assertTrue(data["tree_unchanged_by_run"])
        self.assertNotIn("stale", data)  # a record is never stale; drift is computed when it is read
        self.assertEqual((data["blocked_by"], data["deviations"]), ([], []))
        self.assertEqual(data["head"], ev.head)
        self.assertIsNone(data["supersedes"])
        self.assertNotIn("children", data)
        index = store.read_index(self.root)
        self.assertEqual(store.latest(index, "T-1", "results"),
                         {"digest": written["digest"], "path": written["path"], "head": ev.head,
                          "produced_at": data["produced_at"]})
        self.assertEqual(sorted(sh(self.root, "git", "status", "--porcelain", "--untracked-files=all").split()[1::2]),
                         sorted([written["path"], store.pointer_path("T-1")]))  # the record and its pointer alone

    def test_a_refresh_appends_a_new_record_that_supersedes_the_previous_one(self):
        first = self.ready().write()
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "evidence")
        second = self.ready().write()
        self.assertNotEqual(first["digest"], second["digest"])
        self.assertEqual(read_json(second["file"])["supersedes"], first["digest"])
        self.assertTrue(first["file"].is_file())  # the previous record stays in the store, untouched
        self.assertEqual(store.latest(store.read_index(self.root), "T-1", "results")["digest"], second["digest"])
        self.assertEqual([row["digest"] for row in store.history(self.root, "T-1")],
                         [second["digest"], first["digest"]])

    def test_write_with_a_path_is_refused_naming_the_store(self):
        ev = self.ready()
        with self.assertRaisesRegex(KitError, "takes no path since program-kit 0.2.0"):
            ev.write(self.root / "results.json")
        self.assertEqual(self.appended(), [])

    def test_write_refuses_a_record_that_binds_a_derived_output(self):
        for derived in ("program/evidence/T-0/results.json", "program/evidence/T-0/latest.json",
                        "program/status/T-1.json", "program/claims/G-P0/x.json", "docs/acceptance.json"):
            with self.subTest(derived=derived):
                path = self.root / derived
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"schema_version": 1, "tasks": {}}\n')  # also a valid, empty index
                sh(self.root, "git", "add", "-A")
                sh(self.root, "git", "commit", "-q", "-m", f"add {derived}")
                ev = self.ready()
                ev.hash_inputs([derived])  # the planted defect: a derived output among the inputs
                with self.assertRaises(KitError) as ctx:
                    ev.write()
                self.assertIn(f"binds a derived output: {derived}", str(ctx.exception))
                self.assertEqual(self.appended(), [])

    def test_write_accepts_producer_tooling_under_program_evidence(self):
        for name in ("produce.py", "helpers.sh", "pyproject.toml"):
            path = self.root / "program" / "evidence" / "T-1" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# tooling\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "tooling")
        ev = self.ready()
        ev.hash_inputs(["program/evidence/T-1"])
        self.assertIn("program/evidence/T-1/produce.py", read_json(ev.write()["file"])["inputs"])

    def test_write_refuses_dirty_tree_and_names_paths(self):
        (self.root / "junk.txt").write_text("x")
        (self.root / "calc.py").write_text(CALC + "# edit\n")
        ev = self.ready()
        with self.assertRaises(KitError) as ctx:
            ev.write()
        self.assertIn("junk.txt", str(ctx.exception))
        self.assertIn("calc.py", str(ctx.exception))
        self.assertEqual(self.appended(), [])

    def test_require_clean_tree_lists_paths(self):
        ev = evidence.Evidence(self.root, "T-1")
        ev.require_clean_tree()
        (self.root / "junk.txt").write_text("x")
        with self.assertRaises(KitError) as ctx:
            ev.require_clean_tree()
        self.assertIn("junk.txt", str(ctx.exception))

    def test_write_refuses_when_command_dirtied_tree(self):
        ev = evidence.Evidence(self.root, "T-1")
        ev.hash_inputs(["calc.py"])
        ev.run_command("touch", [PY, "-c", "open('made-by-command.txt', 'w').write('x')"])
        ev.planted_bugs([row("flip")], BUILD, TEST)
        with self.assertRaises(KitError) as ctx:
            ev.write()
        self.assertIn("made-by-command.txt", str(ctx.exception))
        self.assertEqual(self.appended(), [])

    def test_write_refuses_when_head_moved(self):
        ev = self.ready()
        (self.root / "calc.py").write_text(CALC + "# more\n")
        sh(self.root, "git", "commit", "-q", "-am", "move")
        with self.assertRaises(KitError) as ctx:
            ev.write()
        self.assertIn("HEAD", str(ctx.exception))

    def test_write_refuses_without_self_test_row_or_with_unexpected_command(self):
        ev = evidence.Evidence(self.root, "T-1")
        ev.hash_inputs(["calc.py"])
        ev.run_command("ok", [PY, "-c", "pass"])
        with self.assertRaises(KitError) as ctx:
            ev.write()
        self.assertIn("planted", str(ctx.exception))
        ev.planted_bugs([row("flip")], BUILD, TEST)
        ev.planted_bug_rows = [r for r in ev.planted_bug_rows if r["id"] != SELF_TEST]
        with self.assertRaises(KitError) as ctx:
            ev.write()
        self.assertIn(SELF_TEST, str(ctx.exception))
        ev.planted_bug_rows.append({"id": SELF_TEST, "builds": False, "killed": False,
                                    "kill_reason": "build failed", "test_output_sha256": ""})
        ev.run_command("bad", [PY, "-c", "raise SystemExit(3)"])
        with self.assertRaises(KitError) as ctx:
            ev.write()
        self.assertIn("bad", str(ctx.exception))
        self.assertEqual(self.appended(), [])

    def test_write_refuses_with_every_reason_joined(self):
        (self.root / "junk.txt").write_text("x")
        ev = evidence.Evidence(self.root, "T-1")
        ev.run_command("bad", [PY, "-c", "raise SystemExit(3)"])
        with self.assertRaises(KitError) as ctx:
            ev.write()
        message = str(ctx.exception)
        self.assertGreaterEqual(message.count("; "), 3)
        for needle in ("junk.txt", "bad", "planted", "inputs"):
            self.assertIn(needle, message)

    def test_blocked_by_and_deviations_are_written(self):
        ev = self.ready()
        ev.blocked_by = ["ENV-1"]
        ev.deviations = ["skipped scan"]
        data = read_json(ev.write()["file"])
        self.assertEqual((data["blocked_by"], data["deviations"]), (["ENV-1"], ["skipped scan"]))

    def test_the_record_has_no_stale_field_and_the_writer_no_stale_step(self):
        self.assertFalse(hasattr(evidence.Evidence, "stale"))
        self.assertNotIn("stale", evidence.required_keys("results"))
        self.assertNotIn("stale", read_json(self.ready().write()["file"]))


class Validate(Base):
    def written(self):
        """A copy of a written record outside the store, free to tamper with."""
        out = self.root.parent / "results.json"
        out.write_bytes(self.ready().write()["file"].read_bytes())
        return out

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pk.main(["evidence", "validate", *argv])
        return code, out.getvalue(), err.getvalue()

    def tamper(self, path, edit):
        data = read_json(path)
        edit(data)
        path.write_text(json.dumps(data))

    def test_validate_cli_accepts_written_file(self):
        out = self.written()
        code, stdout, stderr = self.run_cli(str(out))
        self.assertEqual((code, stderr), (0, ""))
        self.assertFalse(stdout.startswith("OK: "))

    def test_validate_rejects_each_broken_invariant(self):
        out = self.written()
        cases = {
            "missing key": lambda d: d.pop("deviations"),
            "wrong type": lambda d: d.update(blocked_by="x"),
            "all_killed lie": lambda d: d.update(planted_bugs_all_killed=False),
            "all_expected lie": lambda d: d.update(all_commands_as_expected=False),
            "no self-test": lambda d: d["planted_bugs"].pop(),
            "self-test builds": lambda d: d["planted_bugs"][-1].update(builds=True),
            "self-test killed": lambda d: d["planted_bugs"][-1].update(killed=True),
            "empty inputs": lambda d: d.update(inputs={}),
            "short input hash": lambda d: d["inputs"].update({"calc.py": "abc"}),
            "short head": lambda d: d.update(head="abc"),
            "short merge_base": lambda d: d.update(merge_base="Z" * 40),
            "tree changed": lambda d: d.update(tree_unchanged_by_run=False),
            "expected lie": lambda d: d["commands"][0].update(exit=1),
            "bad supersedes": lambda d: d.update(supersedes="abc"),
        }
        original = out.read_text()
        for name, edit in cases.items():
            out.write_text(original)
            self.tamper(out, edit)
            code, _, stderr = self.run_cli(str(out))
            self.assertEqual(code, 1, name)
            self.assertTrue(stderr.startswith("error: "), name)

    def test_validate_row_reason_rules(self):
        out = self.written()
        original = out.read_text()
        h = "0" * 64
        cases = {
            "unbuilt but reason test failed": {"builds": False, "killed": False, "kill_reason": "test failed"},
            "built killed reason build failed": {"builds": True, "killed": True, "kill_reason": "build failed"},
            "survivor reason test failed": {"builds": True, "killed": False, "kill_reason": "test failed"},
            "killed reason not killed": {"builds": True, "killed": True, "kill_reason": "not killed"},
            "reason missing": {"builds": True, "killed": True, "kill_reason": None},
            "short digest": {"builds": True, "killed": True, "kill_reason": "test failed", "test_output_sha256": "ab"},
            "digest missing": {"builds": True, "killed": True, "kill_reason": "test failed", "test_output_sha256": None},
        }
        for name, fields in cases.items():
            data = json.loads(original)
            bug = {"id": "extra", "test_output_sha256": h, **fields}
            for key in [k for k, v in bug.items() if v is None]:
                del bug[key]
            data["planted_bugs"].insert(0, bug)
            data["planted_bugs_all_killed"] = False
            out.write_text(json.dumps(data))
            self.assertEqual(self.run_cli(str(out))[0], 1, name)
        good = json.loads(original)
        good["planted_bugs"].insert(0, {"id": "empty-digest", "builds": False, "killed": False,
                                        "kill_reason": "build failed", "test_output_sha256": "", **PLANTED})
        good["planted_bugs_all_killed"] = False
        out.write_text(json.dumps(good))
        self.assertEqual(self.run_cli(str(out))[0], 0)

    def test_validate_checks_the_planted_fields_when_present(self):
        out = self.written()
        original = out.read_text()
        cases = [("path", 5), ("path", ""), ("find", 5), ("replace", None), ("build_argv", "make"),
                 ("build_argv", []), ("build_argv", [1]), ("test_argv", None), ("test_argv", []),
                 ("test_argv", "make test"), ("test_argv", ["make", 2])]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                out.write_text(original)
                self.tamper(out, lambda d: d["planted_bugs"][0].update({key: value}))
                code, _, stderr = self.run_cli(str(out))
                self.assertEqual(code, 1)
                self.assertEqual(stderr.count("\n"), 1, stderr)
                self.assertTrue(stderr.startswith(f"error: planted_bugs[0]: {key} "), stderr)
        out.write_text(original)
        self.tamper(out, lambda d: d["planted_bugs"][0].update(build_argv=None))  # no build step was given
        self.assertEqual(self.run_cli(str(out))[0], 0)

    def test_the_gate_needs_every_row_to_say_what_was_planted_and_what_ran(self):
        data = read_json(self.written())
        self.assertEqual(evidence.gate_problems(data, "T-1"), [])
        for index in (0, 1):
            for key in evidence.PLANTED_KEYS:
                with self.subTest(index=index, key=key):
                    stripped = copy.deepcopy(data)
                    del stripped["planted_bugs"][index][key]
                    problem = [f"planted_bugs[{index}]: does not record {key} (what was planted and the "
                               "commands that ran)"]
                    self.assertEqual(evidence.validate_results(stripped), problem)  # validate refuses it too
                    self.assertEqual(evidence.gate_problems(stripped, "T-1"), problem)

    def test_gate_problems_cover_task_head_commands_and_survivors(self):
        data = read_json(self.written())
        self.assertEqual(evidence.gate_problems(data, "T-1", data["head"]), [])
        self.assertEqual(evidence.gate_problems(data, "T-2"), ["task_id: is T-1, expected T-2"])
        self.assertTrue(evidence.gate_problems(data, "T-1", "0" * 40)[0].startswith("head: is "))
        self.assertEqual(evidence.gate_problems(dict(data, commands=[]), "T-1"),
                         ["commands: is empty (evidence must record the commands that were run)"])
        self.assertEqual(evidence.gate_problems([], "T-1"), ["results must be a JSON object"])

    def test_schema_requires_the_fields_every_written_row_carries(self):
        schema = json.loads((Path(evidence.__file__).resolve().parent.parent / "templates"
                             / "results.schema.json").read_text())
        items = schema["properties"]["planted_bugs"]["items"]
        self.assertEqual(items["required"], [*evidence.ROW_KEYS, *evidence.PLANTED_KEYS])
        for row_written in read_json(self.written())["planted_bugs"]:
            self.assertEqual(list(row_written), [*evidence.ROW_KEYS, *evidence.PLANTED_KEYS])
            self.assertEqual(set(row_written), set(items["properties"]))

    def test_validate_all_killed_recomputed_excluding_self_test_row(self):
        out = self.written()
        data = read_json(out)
        data["planted_bugs"].insert(0, {"id": "survivor", "builds": True, "killed": False,
                                        "kill_reason": "not killed", "test_output_sha256": "0" * 64, **PLANTED})
        out.write_text(json.dumps(data))
        code, _, stderr = self.run_cli(str(out))
        self.assertEqual(code, 1)
        self.assertIn("planted_bugs_all_killed", stderr)
        data["planted_bugs_all_killed"] = False
        out.write_text(json.dumps(data))
        self.assertEqual(self.run_cli(str(out))[0], 0)

    def test_validate_missing_or_non_json_file_is_one_line_error(self):
        code, _, stderr = self.run_cli(str(self.root / "nope.json"))
        self.assertEqual((code, stderr.count("\n")), (1, 1))
        bad = self.root / "bad.json"
        bad.write_text("{")
        code, _, stderr = self.run_cli(str(bad))
        self.assertEqual((code, stderr.count("\n")), (1, 1))

    def test_required_keys_come_from_the_schema_files(self):
        schema = json.loads((Path(evidence.__file__).resolve().parent.parent / "templates"
                             / "results.schema.json").read_text())
        self.assertEqual(evidence.required_keys("results"), schema["required"])
        acceptance = json.loads((Path(evidence.__file__).resolve().parent.parent / "templates"
                                 / "acceptance.schema.json").read_text())
        self.assertEqual(evidence.required_keys("acceptance"), acceptance["required"])
        self.assertIn("planted_bugs_all_killed", schema["required"])
        self.assertIn("decision", acceptance["required"])



def run_pk(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pk.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Lint(Base):
    def record(self, inputs, task_id="T-1", children=None):
        data = {"task_id": task_id, "inputs": {path: "0" * 64 for path in inputs}}
        if children is not None:
            data["children"] = children
        return data

    def test_derived_outputs_are_named_one_line_each(self):
        derived = ["program/evidence/T-2/results.json", "program/evidence/T-2/acceptance.json",
                   "program/evidence/T-2/report.md", "program/evidence/T-2/latest.json", "program/claims/G-P1/a.json",
                   "program/status/T-2.json", "program/controller-state.json", "program/progress.md",
                   "program/HANDOFF.md", "program/changes/register-log.md",
                   "program/approvals/stage-6.json", "elsewhere/acceptance.json", "out/results.json"]
        allowed = ["src/app.py", "program/evidence/T-1/produce.py", "program/evidence/T-1/helpers.sh",
                   "program/evidence/tool.toml", "program/tasks/T-1/PLAN.md", "program/checks.yaml", "go.sum",
                   "program/rulings.md", "program/ledger.yaml"]  # the requirements ledger is an input
        self.assertEqual(evidence.lint_record(self.record(derived + allowed)),
                         [f"binds a derived output: {path}" for path in sorted(derived)])
        self.assertEqual(evidence.lint_record(self.record(allowed)), [])

    def test_only_a_gate_record_binds_children_and_never_a_gate(self):
        child = {"task_id": "T-2", "kind": "results", "digest": "1" * 64, "drift": "fresh"}
        gate_child = {"task_id": "G-P0", "kind": "results", "digest": "2" * 64, "drift": "fresh"}
        self.assertEqual(evidence.lint_record(self.record(["calc.py"], "G-P1", [child])), [])
        self.assertEqual(evidence.lint_record(self.record(["calc.py"], "T-1", [child])),
                         ["binds child records but T-1 is not a gate: only a gate record binds other records"])
        self.assertEqual(evidence.lint_record(self.record(["calc.py"], "G-P1", [child, gate_child])),
                         ["binds a gate record: G-P0 (nothing may bind a gate record)"])

    def test_a_gate_record_binds_the_latest_child_records_by_digest_with_their_drift(self):
        child = self.ready().write()
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "T-1 evidence")
        (self.root / "test_calc.py").write_text(CALC_TEST + "\n# later\n")  # T-1 drifts; it is not refreshed
        sh(self.root, "git", "commit", "-q", "-am", "a later change")
        gate_run = evidence.Evidence(self.root, "G-P0")
        gate_run.hash_inputs(["calc.py", "test_calc.py"])
        gate_run.run_command("tests", TEST + ["test_calc"])
        gate_run.planted_bugs([row("flip")], BUILD, TEST)
        rows = [{"task_id": "T-1", "kind": "results", "digest": child["digest"], "drift": "drifted"}]
        self.assertEqual(gate_run.bind_children(["T-1"]), rows)
        data = read_json(gate_run.write()["file"])  # drift is information in the record, never a refusal
        self.assertEqual(data["children"], rows)
        self.assertEqual(evidence.gate_problems(data, "G-P0"), [])
        with self.assertRaisesRegex(KitError, "nothing may bind a gate record"):
            gate_run.bind_children(["G-P0"])
        with self.assertRaisesRegex(KitError, "only a gate record binds other records"):
            evidence.Evidence(self.root, "T-1").bind_children(["T-1"])

    def test_write_refuses_children_on_a_task_record(self):
        ev = self.ready()
        ev.children = [{"task_id": "T-2", "kind": "results", "digest": "1" * 64, "drift": "fresh"}]  # planted
        with self.assertRaisesRegex(KitError, "only a gate record binds other records"):
            ev.write()
        self.assertEqual(self.appended(), [])

    def test_the_gate_problems_include_the_lint(self):
        data = read_json(self.ready().write()["file"])
        data["inputs"]["program/evidence/T-1/acceptance.json"] = "0" * 64
        self.assertEqual(evidence.gate_problems(data, "T-1"),
                         ["binds a derived output: program/evidence/T-1/acceptance.json"])

    def test_lint_command(self):
        out = self.root.parent / "results.json"
        out.write_text(json.dumps(self.record(["calc.py", "program/status/T-1.json"])))
        code, stdout, stderr = run_pk("evidence", "lint", str(out))
        self.assertEqual((code, stdout, stderr), (1, "", "error: binds a derived output: program/status/T-1.json\n"))
        out.write_text(json.dumps(self.record(["calc.py"])))
        code, stdout, stderr = run_pk("evidence", "lint", str(out))
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("binds only inputs", stdout)


class StoreCommands(Base):
    def acceptance(self, results, **over):
        data = read_json(results["file"])
        accepted = {"task_id": "T-1", "decision": "accepted_development", "evidence_sha256": results["digest"],
                    "source_commit": data["head"], "accepted_at": "2026-10-08T00:00:00Z", "role": "qa",
                    "commands": [], "planted_bugs": [], "findings": [], "blocked_by": []}
        accepted.update(over)
        return accepted

    def test_validate_refuses_a_store_record_whose_bytes_changed(self):
        written = self.ready().write()
        self.assertEqual(run_pk("evidence", "validate", str(written["file"]))[0], 0)
        written["file"].chmod(0o644)
        written["file"].write_text(written["file"].read_text().replace('"T-1"', '"T-1" '))  # the planted defect
        code, _, stderr = run_pk("evidence", "validate", str(written["file"]))
        self.assertEqual(code, 1)
        self.assertTrue(stderr.startswith("error: immutable record altered: "), stderr)

    def test_validate_checks_an_acceptance_record_by_its_own_schema(self):
        results = self.ready().write()
        appended = evidence.accept(self.root, "T-1", self.acceptance(results))
        self.assertEqual(run_pk("evidence", "validate", str(appended["file"]))[0], 0)
        self.assertEqual(evidence.validate_acceptance(self.acceptance(results, role="dev", decision="maybe")),
                         ["decision: must be one of accepted_development, rejected", "role: must be qa"])

    def test_accept_appends_only_an_acceptance_of_the_latest_results_record(self):
        first = self.ready().write()
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "evidence")
        second = self.ready().write()
        with self.assertRaisesRegex(KitError, "acceptance does not bind the latest results record"):
            evidence.accept(self.root, "T-1", self.acceptance(first))  # the planted defect: an older record
        with self.assertRaisesRegex(KitError, "is not .*, the head the latest results record names"):
            evidence.accept(self.root, "T-1", self.acceptance(second, source_commit="0" * 40))
        with self.assertRaisesRegex(KitError, "names task T-2"):
            evidence.accept(self.root, "T-1", self.acceptance(second, task_id="T-2"))
        self.assertIsNone(store.latest(store.read_index(self.root), "T-1", "acceptance"))
        rejected = evidence.accept(self.root, "T-1", self.acceptance(second, decision="rejected"))
        accepted = evidence.accept(self.root, "T-1", self.acceptance(second))  # re-acceptance appends
        entry = store.latest(store.read_index(self.root), "T-1", "acceptance")
        self.assertEqual((entry["digest"], entry["decision"], entry["results_digest"]),
                         (accepted["digest"], "accepted_development", second["digest"]))
        self.assertTrue(rejected["file"].is_file())

    def test_accept_command(self):
        results = self.ready().write()
        path = self.root.parent / "acceptance.json"
        path.write_text(json.dumps(self.acceptance(results)))
        with mock.patch.object(evidence, "find_root", return_value=self.root):
            code, stdout, stderr = run_pk("evidence", "accept", "T-1", str(path))
        self.assertEqual((code, stderr), (0, ""), stderr)
        self.assertTrue(stdout.startswith("acceptance "))

    def test_history_command_lists_newest_first_with_the_current_records(self):
        first = self.ready().write()
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "evidence")
        second = self.ready().write()
        accepted = evidence.accept(self.root, "T-1", self.acceptance(second))
        with mock.patch.object(evidence, "find_root", return_value=self.root):
            code, stdout, stderr = run_pk("evidence", "history", "T-1")
        self.assertEqual((code, stderr), (0, ""))
        lines = [line.split("\t") for line in stdout.splitlines()]
        self.assertEqual([(line[0], line[1], line[2]) for line in lines],
                         [("current", "acceptance", accepted["digest"]), ("current", "results", second["digest"]),
                          ("superseded", "results", first["digest"])])
        self.assertEqual(lines[0][-1], "decision accepted_development")

    def test_init_command_creates_nothing(self):
        with mock.patch.object(evidence, "find_root", return_value=self.root):
            self.assertEqual(run_pk("evidence", "init"),
                             (0, "evidence: nothing to create; program/evidence/<ID>/latest.json appears with each "
                                 "task's first record\n", ""))
        self.assertFalse((self.root / store.BASE).exists())

    def test_validate_without_a_record_checks_names_history_and_pointers(self):
        written = self.ready().write()
        with mock.patch.object(evidence, "find_root", return_value=self.root):  # gated, evidence commit to come
            self.assertEqual(run_pk("evidence", "validate"),
                             (0, "evidence intact\n", f"warning: record pending commit: {written['path']}\n"))
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "evidence")
        with mock.patch.object(evidence, "find_root", return_value=self.root):
            self.assertEqual(run_pk("evidence", "validate"), (0, "evidence intact\n", ""))
            written["file"].chmod(0o644)
            written["file"].write_text("{}\n")  # the planted defect, committed: history rewritten
            sh(self.root, "git", "commit", "-q", "-am", "rewrite the record")
            code, _, stderr = run_pk("evidence", "validate")
        self.assertEqual(code, 1)
        self.assertIn(f"error: immutable record altered: {written['path']} ", stderr)
        self.assertIn(f"error: evidence history rewritten: {written['path']} modified in ", stderr)

    def test_validate_without_a_record_reports_a_digest_named_file_that_is_not_a_record(self):
        written = self.ready().write()
        raw = b'{"hello": "world"}\n'
        fake = written["file"].parent / f"20000101T000000000000Z-{sha256_bytes(raw)[:12]}.json"
        self.assertEqual(sha256_bytes(raw)[:12], fake.name.split("-")[1][:12])  # named after its own digest
        fake.write_bytes(raw)
        with mock.patch.object(evidence, "find_root", return_value=self.root):
            code, _, stderr = run_pk("evidence", "validate")
        self.assertEqual(code, 1)
        self.assertIn(f"error: {store.BASE}/T-1/results/{fake.name}: not a valid results record: missing key", stderr)
        fake.unlink()
        junk = b"not json at all"
        (fake.parent / f"20000101T000000000000Z-{sha256_bytes(junk)[:12]}.json").write_bytes(junk)
        self.assertIn("does not parse as JSON", "\n".join(store.check(self.root, schema=True)[0]))


class Import(Base):
    def legacy(self):
        """A 0.1 layout: results.json and acceptance.json committed under program/evidence/T-1/."""
        written = self.ready().write()
        folder = self.root / "program" / "evidence" / "T-1"
        folder.mkdir(parents=True, exist_ok=True)
        raw = written["file"].read_bytes()
        (folder / "results.json").write_bytes(raw)
        accepted = {"task_id": "T-1", "decision": "accepted_development", "evidence_sha256": sha256_bytes(raw),
                    "source_commit": read_json(written["file"])["head"], "accepted_at": "2026-10-08T00:00:00Z",
                    "role": "qa", "commands": [], "planted_bugs": [], "findings": [], "blocked_by": []}
        (folder / "acceptance.json").write_text(json.dumps(accepted))
        (self.root / store.pointer_path("T-1")).unlink()  # a 0.1 project has no pointer
        for path in self.appended():
            path.chmod(0o644)
            path.unlink()
        return raw

    def test_import_copies_each_record_byte_for_byte_and_builds_the_index(self):
        raw = self.legacy()
        done, legacy = evidence.import_legacy(self.root)
        self.assertEqual(legacy, ["program/evidence/T-1/results.json", "program/evidence/T-1/acceptance.json"])
        self.assertEqual(len(done), 2)
        index = store.read_index(self.root)
        results = store.latest(index, "T-1", "results")
        self.assertEqual(results["digest"], sha256_bytes(raw))
        self.assertEqual((self.root / results["path"]).read_bytes(), raw)
        self.assertEqual(store.latest(index, "T-1", "acceptance")["results_digest"], results["digest"])
        self.assertTrue((self.root / "program" / "evidence" / "T-1" / "results.json").is_file())  # never deleted
        self.assertEqual(evidence.import_legacy(self.root)[0], [])  # a second run copies nothing again

    def test_import_command_prints_what_to_git_rm(self):
        self.legacy()
        with mock.patch.object(evidence, "find_root", return_value=self.root):
            code, stdout, stderr = run_pk("evidence", "import")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("git rm program/evidence/T-1/results.json\ngit rm program/evidence/T-1/acceptance.json\n",
                      stdout)

    def test_import_refuses_a_record_of_another_task_before_copying_anything(self):
        self.legacy()
        other = self.root / "program" / "evidence" / "T-9"
        other.mkdir()
        (other / "results.json").write_text('{"task_id": "T-1"}')
        with self.assertRaisesRegex(KitError, "not a results record of task T-9"):
            evidence.import_legacy(self.root)
        self.assertEqual(self.appended(), [])
        self.assertFalse((self.root / store.pointer_path("T-1")).exists())


if __name__ == "__main__":
    unittest.main()
