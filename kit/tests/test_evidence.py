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
from common import KitError, read_json

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


class Base(unittest.TestCase):
    def setUp(self):
        tmp, self.root = make_repo()
        self.addCleanup(tmp.cleanup)

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
        out = self.root / "program" / "evidence" / "T-1" / "results.json"
        ev.write(out)
        data = read_json(out)
        for key in evidence.required_keys("results"):
            self.assertIn(key, data)
        self.assertEqual(data["task_id"], "T-1")
        self.assertTrue(data["planted_bugs_all_killed"])
        self.assertTrue(data["all_commands_as_expected"])
        self.assertTrue(data["tree_unchanged_by_run"])
        self.assertEqual(data["stale"], {"checked": False, "records": []})
        self.assertEqual((data["blocked_by"], data["deviations"]), ([], []))
        self.assertEqual(data["head"], ev.head)

    def test_write_refuses_dirty_tree_and_names_paths(self):
        (self.root / "junk.txt").write_text("x")
        (self.root / "calc.py").write_text(CALC + "# edit\n")
        ev = self.ready()
        out = self.root / "results.json"
        with self.assertRaises(KitError) as ctx:
            ev.write(out)
        self.assertIn("junk.txt", str(ctx.exception))
        self.assertIn("calc.py", str(ctx.exception))
        self.assertFalse(out.exists())

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
        out = self.root / "results.json"
        with self.assertRaises(KitError) as ctx:
            ev.write(out)
        self.assertIn("made-by-command.txt", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_write_refuses_when_head_moved(self):
        ev = self.ready()
        (self.root / "calc.py").write_text(CALC + "# more\n")
        sh(self.root, "git", "commit", "-q", "-am", "move")
        with self.assertRaises(KitError) as ctx:
            ev.write(self.root / "results.json")
        self.assertIn("HEAD", str(ctx.exception))

    def test_write_refuses_without_self_test_row_or_with_unexpected_command(self):
        ev = evidence.Evidence(self.root, "T-1")
        ev.hash_inputs(["calc.py"])
        ev.run_command("ok", [PY, "-c", "pass"])
        out = self.root / "results.json"
        with self.assertRaises(KitError) as ctx:
            ev.write(out)
        self.assertIn("planted", str(ctx.exception))
        ev.planted_bugs([row("flip")], BUILD, TEST)
        ev.planted_bug_rows = [r for r in ev.planted_bug_rows if r["id"] != SELF_TEST]
        with self.assertRaises(KitError) as ctx:
            ev.write(out)
        self.assertIn(SELF_TEST, str(ctx.exception))
        ev.planted_bug_rows.append({"id": SELF_TEST, "builds": False, "killed": False,
                                    "kill_reason": "build failed", "test_output_sha256": ""})
        ev.run_command("bad", [PY, "-c", "raise SystemExit(3)"])
        with self.assertRaises(KitError) as ctx:
            ev.write(out)
        self.assertIn("bad", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_write_refuses_with_every_reason_joined(self):
        (self.root / "junk.txt").write_text("x")
        ev = evidence.Evidence(self.root, "T-1")
        ev.run_command("bad", [PY, "-c", "raise SystemExit(3)"])
        with self.assertRaises(KitError) as ctx:
            ev.write(self.root / "results.json")
        message = str(ctx.exception)
        self.assertGreaterEqual(message.count("; "), 3)
        for needle in ("junk.txt", "bad", "planted", "inputs"):
            self.assertIn(needle, message)

    def test_write_refuses_output_outside_root(self):
        ev = self.ready()
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "results.json"
            with self.assertRaises(KitError):
                ev.write(target)
            self.assertFalse(target.exists())

    def test_blocked_by_and_deviations_are_written(self):
        ev = self.ready()
        ev.blocked_by = ["ENV-1"]
        ev.deviations = ["skipped scan"]
        out = self.root / "results.json"
        ev.write(out)
        data = read_json(out)
        self.assertEqual((data["blocked_by"], data["deviations"]), (["ENV-1"], ["skipped scan"]))

    def test_stale_without_stale_module_names_the_missing_file(self):
        ev = evidence.Evidence(self.root, "T-1")
        with mock.patch.dict(sys.modules, {"stale": None}):
            with self.assertRaises(KitError) as ctx:
                ev.stale("main")
        self.assertIn("kit/scripts/stale.py is missing", str(ctx.exception))
        self.assertEqual(ev.stale_result, {"checked": False, "records": []})

    def test_stale_delegates_to_stale_records(self):
        ev = evidence.Evidence(self.root, "T-1")
        fake = mock.Mock()
        bad = {"path": "u", "changed_inputs": [], "error": "no inputs mapping"}
        fake.stale_records.return_value = {"main": "x" * 40, "stale": [{"path": "p", "changed_inputs": ["q"]}],
                                           "unreadable": [bad], "checked": 2}
        with mock.patch.dict(sys.modules, {"stale": fake}):
            got = ev.stale("main")
        fake.stale_records.assert_called_once_with(self.root, "main")
        self.assertEqual(got, {"checked": True, "records": [{"path": "p", "changed_inputs": ["q"]}],
                               "unreadable": [bad]})
        self.assertEqual(ev.stale_result, got)


class Validate(Base):
    def written(self):
        ev = self.ready()
        out = self.root / "results.json"
        ev.write(out)
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
            "bad stale": lambda d: d.update(stale={"checked": "yes"}),
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
                                        "kill_reason": "build failed", "test_output_sha256": ""})
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
                    self.assertEqual(evidence.validate_results(stripped), [])  # the schema keeps them optional
                    self.assertEqual(evidence.gate_problems(stripped, "T-1"),
                                     [f"planted_bugs[{index}]: does not record {key} (what was planted and the "
                                      "commands that ran)"])

    def test_gate_problems_cover_task_head_commands_and_survivors(self):
        data = read_json(self.written())
        self.assertEqual(evidence.gate_problems(data, "T-1", data["head"]), [])
        self.assertEqual(evidence.gate_problems(data, "T-2"), ["task_id: is T-1, expected T-2"])
        self.assertTrue(evidence.gate_problems(data, "T-1", "0" * 40)[0].startswith("head: is "))
        self.assertEqual(evidence.gate_problems(dict(data, commands=[]), "T-1"),
                         ["commands: is empty (evidence must record the commands that were run)"])
        self.assertEqual(evidence.gate_problems([], "T-1"), ["results must be a JSON object"])

    def test_schema_lists_the_planted_fields_as_optional_properties(self):
        schema = json.loads((Path(evidence.__file__).resolve().parent.parent / "templates"
                             / "results.schema.json").read_text())
        items = schema["properties"]["planted_bugs"]["items"]
        self.assertEqual(items["required"], ["id", "builds", "killed", "kill_reason", "test_output_sha256"])
        for row_written in read_json(self.written())["planted_bugs"]:
            self.assertEqual(set(row_written), set(items["properties"]))
        for key in evidence.PLANTED_KEYS:
            self.assertNotIn(key, items["required"])

    def test_validate_all_killed_recomputed_excluding_self_test_row(self):
        out = self.written()
        data = read_json(out)
        data["planted_bugs"].insert(0, {"id": "survivor", "builds": True, "killed": False,
                                        "kill_reason": "not killed", "test_output_sha256": "0" * 64})
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


if __name__ == "__main__":
    unittest.main()
