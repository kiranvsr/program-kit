"""The release fingerprint producer: the release set, the run, the parsers, the merge rule, validate, plan, immutability."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fingerprint as fp
import pk
import store
from common import KitError, write_yaml

BASE = fp.BASE
PY = sys.executable


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def entry(ident, code, track="plain", **extra):
    """A release-set entry that runs a Python one-liner."""
    return {"id": ident, "argv": [PY, "-c", code], "track": track, "kind": "check", **extra}


def say(text):
    return f"print({text!r})"


class Repo(unittest.TestCase):
    """A temporary repository with a committed release set; `self.cli` runs pk in it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.scratch = Path(tmp.name).resolve() / "scratch"
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        (self.root / "a.txt").write_text("a\n")
        env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root.parent)})
        env.start()
        self.addCleanup(env.stop)

    def release(self, *entries, toolchain=(), commit=True):
        write_yaml(self.root / "program" / "release-set.yaml",
                   {"toolchain": [list(a) for a in toolchain], "entries": list(entries)})
        return self.commit("release set") if commit else None

    def commit(self, message="change"):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", message)
        return sh(self.root, "git", "rev-parse", "HEAD").strip()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pk.main(["fingerprint", *argv])
        return code, out.getvalue(), err.getvalue()

    def run_ok(self, *argv):
        """`pk fingerprint run` with the scratch directory outside the repository; returns the record."""
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        code, out, err = self.cli("run", "--scratch", str(self.scratch), *argv)
        return code, out, err, self.record(head)

    def record(self, head=None):
        head = head or sh(self.root, "git", "rev-parse", "HEAD").strip()
        return json.loads((self.root / BASE / head[:12] / "fingerprint.json").read_text())

    def text(self, head=None):
        head = head or sh(self.root, "git", "rev-parse", "HEAD").strip()
        return (self.root / BASE / head[:12] / "fingerprint.json").read_text()


def release_error(text):
    with unittest.TestCase().assertRaises(KitError) as caught:
        fp.parse_release_set(text)
    return str(caught.exception)


class ReleaseSetValidation(Repo):
    def refused(self, mutate, *needles, entries=None):
        good = entries or [entry("unit", "pass")]
        data = {"entries": good}
        mutate(data)
        write_yaml(self.root / "program" / "release-set.yaml", data)
        self.commit("bad release set")
        with self.assertRaises(KitError) as caught:
            fp.load_release_set(self.root, "HEAD")
        message = str(caught.exception)
        for needle in needles:
            self.assertIn(needle, message)

    def test_a_valid_set_loads_with_the_defaults_filled_in(self):
        self.release(entry("unit", "pass"), toolchain=[[PY, "--version"]])
        loaded = fp.load_release_set(self.root, "HEAD")
        self.assertEqual(loaded["toolchain"], [[PY, "--version"]])
        self.assertEqual(loaded["entries"][0], {"id": "unit", "argv": [PY, "-c", "pass"], "expected_exit": 0,
                                                "track": "plain", "kind": "check", "derives": None, "env": {}})

    def test_it_is_read_from_the_ref_and_not_the_working_copy(self):
        self.release(entry("unit", "pass"))
        (self.root / "program" / "release-set.yaml").write_text("entries: nonsense\n")
        self.assertEqual(fp.load_release_set(self.root, "HEAD")["entries"][0]["id"], "unit")

    def test_a_commit_without_the_file_is_refused_by_name(self):
        (self.root / "a.txt").write_text("b\n")
        self.commit()
        with self.assertRaisesRegex(KitError, "program/release-set.yaml cannot be read on HEAD"):
            fp.load_release_set(self.root, "HEAD")

    def test_each_refusal_names_the_key_or_value_and_the_entry(self):
        cases = {
            "entries missing": (lambda d: d.pop("entries"), ("entries must be a non-empty list",)),
            "entries empty": (lambda d: d.update(entries=[]), ("entries must be a non-empty list",)),
            "entries a string": (lambda d: d.update(entries="x"), ("entries must be a non-empty list",)),
            "unknown top key": (lambda d: d.update(extra=1), ("unknown key 'extra'",)),
            "toolchain a string": (lambda d: d.update(toolchain="python3"), ("toolchain must be a list",)),
            "toolchain item a string": (lambda d: d.update(toolchain=["python3 --version"]),
                                        ("toolchain[0]", "non-empty list of strings")),
            "toolchain item empty": (lambda d: d.update(toolchain=[[]]), ("toolchain[0]",)),
            "duplicate id": (lambda d: d["entries"].append(dict(d["entries"][0])), ("unit", "used twice")),
            "id upper case": (lambda d: d["entries"][0].update(id="Unit"), ("id must be lowercase", "'Unit'")),
            "id underscore": (lambda d: d["entries"][0].update(id="a_b"), ("'a_b'",)),
            "id missing": (lambda d: d["entries"][0].pop("id"), ("entries[0]", "id must be")),
            "argv a shell string": (lambda d: d["entries"][0].update(argv="make test"),
                                    ("entry unit", "argv must be a non-empty list of strings")),
            "argv empty": (lambda d: d["entries"][0].update(argv=[]), ("entry unit", "argv")),
            "argv with an int": (lambda d: d["entries"][0].update(argv=["go", 1]), ("entry unit", "argv")),
            "argv missing": (lambda d: d["entries"][0].pop("argv"), ("entry unit", "missing key 'argv'")),
            "expected_exit a string": (lambda d: d["entries"][0].update(expected_exit="0"),
                                       ("entry unit", "expected_exit must be an integer", "'0'")),
            "expected_exit a bool": (lambda d: d["entries"][0].update(expected_exit=True),
                                     ("entry unit", "expected_exit")),
            "track upper case": (lambda d: d["entries"][0].update(track="Slow"), ("entry unit", "track", "'Slow'")),
            "track missing": (lambda d: d["entries"][0].pop("track"), ("entry unit", "missing key 'track'")),
            "kind unknown": (lambda d: d["entries"][0].update(kind="lint"), ("entry unit", "kind", "'lint'")),
            "kind missing": (lambda d: d["entries"][0].pop("kind"), ("entry unit", "missing key 'kind'")),
            "derives unknown": (lambda d: d["entries"][0].update(derives="junit"),
                                ("entry unit", "derives", "'junit'")),
            "env a list": (lambda d: d["entries"][0].update(env=["A=1"]), ("entry unit", "env must be a mapping")),
            "env value an int": (lambda d: d["entries"][0].update(env={"SLOW": 1}),
                                 ("entry unit", "env SLOW must be a string")),
            "env name with equals": (lambda d: d["entries"][0].update(env={"A=B": "1"}), ("entry unit", "'A=B'")),
            "unknown entry key": (lambda d: d["entries"][0].update(timeout=5), ("entry unit", "unknown key 'timeout'")),
            "entry not a mapping": (lambda d: d.update(entries=["unit"]), ("entries[0]", "mapping")),
        }
        for name, (mutate, needles) in cases.items():
            with self.subTest(name):
                self.refused(mutate, *needles)

    def test_the_valid_values_of_every_key_are_accepted(self):
        text = ("toolchain: []\nentries:\n  - id: a-1\n    argv:\n      - go\n    expected_exit: 3\n    track: slow-2\n"
                "    kind: gate\n    derives: go-tests\n    env:\n      X: \"1\"\n  - id: b\n    argv:\n      - x\n"
                "    track: t\n    kind: test\n    derives: null\n")
        loaded = fp.parse_release_set(text)
        self.assertEqual([e["derives"] for e in loaded["entries"]], ["go-tests", None])
        self.assertEqual(loaded["entries"][0]["env"], {"X": "1"})
        self.assertEqual(loaded["entries"][0]["expected_exit"], 3)

    def test_the_shipped_template_is_refused_until_it_lists_a_command(self):
        text = (Path(fp.__file__).resolve().parent.parent / "templates" / "release-set.yaml").read_text()
        with self.assertRaisesRegex(KitError, "entries must be a non-empty list"):
            fp.parse_release_set(text)


class Producer(Repo):
    def test_a_record_binds_head_and_lists_every_command_in_release_set_order(self):
        self.release(entry("one", say("hello")), entry("two", say("again")), entry("three", say("x"), track="other"),
                     toolchain=[[PY, "-c", "print('tool 1.0'); print('second line')"]])
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        code, out, err, record = self.run_ok()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((record["schema"], record["head"], record["head12"], record["only"]),
                         ("program-kit.fingerprint.v1", head, head[:12], None))
        self.assertEqual([c["id"] for c in record["commands"]], ["one", "two", "three"])
        self.assertEqual({k: v["commands"] for k, v in record["tracks"].items()},
                         {"plain": ["one", "two"], "other": ["three"]})
        self.assertEqual(record["toolchain"]["versions"], [{"argv": [PY, "-c", "print('tool 1.0'); print('second line')"],
                                                            "line": "tool 1.0"}])
        row = record["commands"][0]
        self.assertEqual((row["exit"], row["expected_exit"], row["as_expected"], row["output_tail"], row["problem"]),
                         (0, 0, True, "hello", None))
        self.assertEqual(row["output_sha256"], fp.sha256_bytes(b"hello\n"))
        self.assertTrue(record["complete"] and record["all_as_expected"] and record["tree_unchanged_by_run"])
        self.assertEqual(record["release_set_sha256"],
                         fp.sha256_bytes((self.root / "program" / "release-set.yaml").read_bytes()))
        self.assertIsNone(record["toolchain"]["kit_manifest_sha256"])
        self.assertIsNone(record["toolchain"]["checks_sha256"])
        self.assertIn("complete true, all as expected true", out)
        self.assertEqual(sorted(p.name for p in (self.root / BASE / head[:12]).iterdir()), ["fingerprint.json"])

    def test_the_checks_and_manifest_digests_are_taken_at_head(self):
        (self.root / "program").mkdir()
        (self.root / "program" / "checks.yaml").write_text("build: []\n")
        (self.root / "kit").mkdir()
        (self.root / "kit" / "MANIFEST.sha256").write_text("abc  x\n")
        self.release(entry("one", "pass"))
        record = self.run_ok()[3]
        self.assertEqual(record["toolchain"]["checks_sha256"], fp.sha256_bytes(b"build: []\n"))
        self.assertEqual(record["toolchain"]["kit_manifest_sha256"], fp.sha256_bytes(b"abc  x\n"))

    def test_the_full_output_goes_to_the_scratch_directory_and_not_the_record(self):
        self.release(entry("loud", "[print('line', i) for i in range(30)]"))
        record = self.run_ok()[3]
        log = (self.scratch / "loud.log").read_text()
        self.assertEqual(log.count("\n"), 30)
        row = record["commands"][0]
        self.assertEqual(row["output_sha256"], fp.sha256_bytes(log.encode()))
        self.assertEqual(row["output_tail"].splitlines(), [f"line {i}" for i in range(10, 30)])
        self.assertNotIn("line 9", self.text())  # only the last 20 lines are kept

    def test_the_two_tracks_run_concurrently(self):
        marks = self.root.parent / "marks"
        marks.mkdir()
        body = ("import os, sys, time, pathlib\nd = pathlib.Path(os.environ['MARK_DIR'])\n(d / '{me}').write_text('x')\n"
                "end = time.time() + 20\nwhile not (d / '{other}').exists():\n    if time.time() > end:\n"
                "        sys.exit(1)\n    time.sleep(0.02)\n")
        env = {"MARK_DIR": str(marks)}
        self.release(entry("left", body.format(me="a", other="b"), track="one", env=env),
                     entry("right", body.format(me="b", other="a"), track="two", env=env))
        code, _, _, record = self.run_ok()
        self.assertEqual(code, 0)  # each waited for the other's marker: only possible when both were running
        self.assertTrue(record["all_as_expected"])

    def test_a_failing_command_does_not_stop_its_track(self):
        self.release(entry("first", "import sys; sys.exit(3)"), entry("second", say("ran anyway")),
                     entry("third", say("elsewhere"), track="other"))
        code, out, _, record = self.run_ok()
        rows = {r["id"]: r for r in record["commands"]}
        self.assertEqual(code, 1)
        self.assertEqual((rows["first"]["exit"], rows["first"]["as_expected"]), (3, False))
        self.assertEqual((rows["second"]["exit"], rows["second"]["as_expected"]), (0, True))
        self.assertTrue(rows["third"]["as_expected"])
        self.assertFalse(record["all_as_expected"])
        self.assertTrue(record["complete"])
        self.assertIn("FAILED: first (exit 3, expected 0)", out)

    def test_an_expected_nonzero_exit_is_as_expected(self):
        self.release(entry("refuses", "import sys; sys.exit(3)", expected_exit=3))
        code, _, _, record = self.run_ok()
        self.assertEqual(code, 0)
        self.assertEqual((record["commands"][0]["exit"], record["commands"][0]["as_expected"]), (3, True))

    def test_a_command_that_cannot_start_is_recorded_not_raised(self):
        self.release({"id": "nope", "argv": ["no-such-program-xyz"], "track": "plain", "kind": "check"})
        code, _, _, record = self.run_ok()
        row = record["commands"][0]
        self.assertEqual((code, row["exit"], row["as_expected"], row["problem"]),
                         (1, 127, False, "the command could not be started"))

    def test_a_command_past_the_timeout_is_recorded_as_timed_out(self):
        self.release(entry("slow", "import time; time.sleep(30)"))
        with mock.patch.object(fp, "STEP_TIMEOUT", 1):
            code, _, _, record = self.run_ok()
        row = record["commands"][0]
        self.assertEqual((code, row["exit"], row["as_expected"], row["problem"]), (1, 124, False, "timed out after 1s"))

    def test_only_runs_one_track_and_the_record_is_not_complete(self):
        self.release(entry("one", say("1")), entry("two", say("2"), track="other"))
        code, out, _, record = self.run_ok("--only", "other")
        self.assertEqual(code, 1)  # a partial record never satisfies the command
        self.assertEqual(record["only"], "other")
        self.assertEqual([c["id"] for c in record["commands"]], ["two"])
        self.assertEqual(list(record["tracks"]), ["other"])
        self.assertFalse(record["complete"])
        self.assertTrue(record["all_as_expected"])
        self.assertIn("complete false, all as expected true", out)

    def test_only_an_unknown_track_is_refused(self):
        self.release(entry("one", "pass"))
        code, out, err = self.cli("run", "--only", "nope", "--scratch", str(self.scratch))
        self.assertEqual(code, 1)
        self.assertIn("no track named 'nope'", err)
        self.assertFalse((self.root / BASE).exists())

    def test_a_dirty_tree_is_refused_and_nothing_runs(self):
        self.release(entry("one", "open(%r, 'w').write('x')" % str(self.root.parent / "ran.txt")))
        for name, dirty in (("untracked", lambda: (self.root / "new.txt").write_text("x")),
                            ("modified", lambda: (self.root / "a.txt").write_text("changed\n"))):
            with self.subTest(name):
                dirty()
                code, _, err = self.cli("run", "--scratch", str(self.scratch))
                self.assertEqual(code, 1)
                self.assertIn("working tree is not clean", err)
                self.assertFalse((self.root.parent / "ran.txt").exists())
                self.assertFalse((self.root / BASE).exists())
                sh(self.root, "git", "checkout", "-q", "--", "a.txt")
                (self.root / "new.txt").unlink(missing_ok=True)

    def test_a_head12_that_is_already_tracked_is_refused(self):
        self.release(entry("one", "pass"))
        name = "abcdef012345"
        (self.root / BASE / name).mkdir(parents=True)
        (self.root / BASE / name / "fingerprint.json").write_text("{}\n")
        self.commit("a record")
        with mock.patch.object(fp, "record_dir_name", lambda head: name):
            code, _, err = self.cli("run", "--scratch", str(self.scratch))
        self.assertEqual(code, 1)
        self.assertIn(f"already recorded at {name}; a record is never rewritten", err)
        self.assertEqual((self.root / BASE / name / "fingerprint.json").read_text(), "{}\n")

    def test_an_untracked_directory_of_an_earlier_run_is_rewritten(self):
        self.release(entry("one", say("first")))
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        self.run_ok()
        stale = self.root / BASE / head[:12] / "left-over.json"
        stale.write_text("{}\n")
        code, _, _, record = self.run_ok()
        self.assertEqual(code, 0)
        self.assertFalse(stale.exists())
        self.assertEqual(record["commands"][0]["output_tail"], "first")

    def test_an_untracked_directory_of_another_head_does_not_make_the_next_run_dirty(self):
        self.release(entry("one", "pass"))
        old = self.run_ok()[3]["head12"]
        (self.root / "a.txt").write_text("fixed\n")
        self.commit("the fix")
        code, _, _, record = self.run_ok()
        self.assertEqual(code, 0)
        self.assertNotEqual(record["head12"], old)
        self.assertTrue((self.root / BASE / old).is_dir())

    def test_a_command_that_dirties_the_tree_makes_the_record_incomplete_and_exits_1(self):
        self.release(entry("dirty", "open('made-by-the-run.txt', 'w').write('x')"))
        code, out, err, record = self.run_ok()
        self.assertEqual(code, 1)
        self.assertFalse(record["tree_unchanged_by_run"])
        self.assertFalse(record["complete"])
        self.assertTrue(record["all_as_expected"])
        self.assertIn("the run changed the working tree", err)
        self.assertIn("made-by-the-run.txt", err)

    def test_a_command_that_moves_head_makes_the_record_incomplete(self):
        script = ("import subprocess\nsubprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)")
        self.release(entry("mover", script))
        code, _, _, record = self.run_ok()
        self.assertEqual(code, 1)
        self.assertFalse(record["tree_unchanged_by_run"])

    def test_env_values_never_appear_in_the_record_but_the_names_do(self):
        secret = "s3cr3t-value-xyz"
        self.release(entry("uses-env", "import os; print('got', os.environ['MY_TOKEN'])", env={"MY_TOKEN": secret}))
        record = self.run_ok()[3]
        self.assertNotIn(secret, self.text())
        self.assertEqual(record["commands"][0]["env_keys"], ["MY_TOKEN"])
        self.assertEqual(record["commands"][0]["output_tail"], "got <env>")
        self.assertEqual(fp.sha256_bytes(f"got {secret}\n".encode()), record["commands"][0]["output_sha256"])

    def test_the_entrys_env_reaches_only_its_own_command(self):
        self.release(entry("with", "import os; print(os.environ.get('ONLY_HERE', 'unset'))", env={"ONLY_HERE": "yes!"}),
                     entry("without", "import os; print(os.environ.get('ONLY_HERE', 'unset'))"))
        rows = {r["id"]: r for r in self.run_ok()[3]["commands"]}
        self.assertEqual((rows["with"]["output_tail"], rows["without"]["output_tail"]), ("<env>", "unset"))
        self.assertNotIn("ONLY_HERE", os.environ)

    def test_no_absolute_path_appears_in_the_record(self):
        code = ("import os, sys, tempfile\nprint('root', os.getcwd())\nprint('tmp', tempfile.gettempdir())\n"
                "print('trace File \"/Us' + 'ers/someone/project/x.py\", line 3')\nprint('home /ho' + 'me/someone/x', 'C' + ':\\\\dir\\\\f')\n"
                "print('scratch', os.environ.get('MY_DIR'))")
        self.release(entry("paths", code, env={"MY_DIR": str(self.scratch)}),
                     toolchain=[[PY, "-c", "import sys; print(sys.executable)"]])
        record = self.run_ok()[3]
        text = self.text()
        self.assertIsNone(fp.ABSOLUTE_PATH.search(text), fp.ABSOLUTE_PATH.search(text))
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(self.scratch), text)
        self.assertIn("root <root>", record["commands"][0]["output_tail"])
        self.assertGreaterEqual(record["commands"][0]["output_tail"].count("<path>"), 3)  # the user, home and drive paths
        self.assertIn("scratch <scratch>", record["commands"][0]["output_tail"])
        self.assertEqual(fp.record_problems(record, text, record["head12"]), [])

    def test_an_argv_with_an_absolute_path_is_refused_before_anything_runs(self):
        marker = self.root.parent / "ran.txt"
        self.release(entry("first", "open(%r, 'w').write('x')" % str(marker), track="other"),
                     {"id": "abs", "argv": ["/tmp/some-tool"], "track": "plain", "kind": "check"})
        code, _, err = self.cli("run", "--scratch", str(self.scratch))
        self.assertEqual(code, 1)
        self.assertIn("holds an absolute path", err)
        self.assertFalse(marker.exists())  # refused before anything ran
        self.assertFalse((self.root / BASE).exists())

    def test_a_derived_command_that_parses_nothing_is_not_a_pass(self):
        self.release(entry("vacuous", say("nothing to see"), kind="test", derives="unittest"))
        code, _, _, record = self.run_ok()
        row = record["commands"][0]
        self.assertEqual((code, row["exit"], row["as_expected"], row["problem"]),
                         (1, 0, False, "no test entries parsed"))
        self.assertEqual(record["tables"], {})

    def test_a_summary_without_a_test_is_still_no_test(self):
        self.release(entry("zero", "print('Ran 0 tests in 0.000s'); print(); print('OK')", derives="unittest"))
        self.assertEqual(self.run_ok()[3]["commands"][0]["problem"], "no test entries parsed")

    def test_a_failing_command_with_nothing_parsed_gets_no_parse_problem(self):
        self.release(entry("crash", "import sys; sys.exit(2)", derives="unittest"))
        row = self.run_ok()[3]["commands"][0]
        self.assertEqual((row["exit"], row["problem"]), (2, None))

    def test_tables_are_written_beside_the_record_with_their_digests(self):
        lines = ("test_a (m.C.test_a) ... ok", "test_b (m.C.test_b) ... skipped 'later'", "", "OK")
        self.release(entry("t", "print(%r)" % "\n".join(lines), kind="test", derives="unittest"))
        record = self.run_ok()[3]
        table = record["tables"]["unittest"]
        self.assertEqual(table["path"], f"{BASE}/{record['head12']}/unittest.json")
        raw = (self.root / table["path"]).read_bytes()
        self.assertEqual(table["sha256"], fp.sha256_bytes(raw))
        self.assertEqual(json.loads(raw), {"": "ok", "m.C.test_a": "ok", "m.C.test_b": "skipped"})

    def test_two_commands_that_disagree_on_a_test_make_the_record_incomplete(self):
        self.release(entry("a", say("test_x (m.C.test_x) ... ok"), track="one", derives="unittest"),
                     entry("b", say("test_x (m.C.test_x) ... FAIL"), track="two", derives="unittest"))
        code, out, _, record = self.run_ok()
        self.assertEqual(code, 1)
        self.assertEqual(record["table_conflicts"], [{"table": "unittest", "name": "m.C.test_x",
                                                      "verdicts": ["fail", "ok"]}])
        self.assertFalse(record["complete"])
        self.assertIn("CONFLICT: unittest m.C.test_x: fail, ok", out)

    def test_a_skip_in_one_track_yields_to_a_pass_in_another(self):
        self.release(entry("fast", say("test_x (m.C.test_x) ... skipped 'slow'"), track="one", derives="unittest"),
                     entry("slow", say("test_x (m.C.test_x) ... ok"), track="two", derives="unittest"))
        code, _, _, record = self.run_ok()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.root / record["tables"]["unittest"]["path"]).read_text()),
                         {"m.C.test_x": "ok"})
        self.assertEqual(record["table_conflicts"], [])

    def test_a_go_table_is_written_per_package(self):
        events = [{"Action": "pass", "Package": "ex/a", "Test": "TestOne"}, {"Action": "pass", "Package": "ex/a"}]
        self.release(entry("go", "\n".join(f"print({json.dumps(e)!r})" for e in events), derives="go-tests"))
        record = self.run_ok()[3]
        self.assertEqual(json.loads((self.root / record["tables"]["go-tests"]["path"]).read_text()),
                         {"ex/a": {"": "pass", "TestOne": "pass"}})


# Exact `python3 -m unittest -v` output of tiny throwaway modules (Python 3.14.6). Only the absolute path in each
# traceback `File` line is shortened to the file name.
REAL_SUB_LAST = 'test_aa_ok (test_sub_last.A.test_aa_ok) ... ok\ntest_zz_sub (test_sub_last.Z.test_zz_sub) ... \n  test_zz_sub (test_sub_last.Z.test_zz_sub) (i=1) ... FAIL\n\n======================================================================\nFAIL: test_zz_sub (test_sub_last.Z.test_zz_sub) (i=1)\n----------------------------------------------------------------------\nTraceback (most recent call last):\n  File "test_sub_last.py", line 9, in test_zz_sub\n    self.assertEqual(i, 0)\n    ~~~~~~~~~~~~~~~~^^^^^^\nAssertionError: 1 != 0\n\n----------------------------------------------------------------------\nRan 2 tests in 0.001s\n\nFAILED (failures=1)\n'

REAL_LOGGING = 'test_a_logs (test_log.L.test_a_logs) ... ERROR:svc:connection refused\nok\ntest_b_ok (test_log.L.test_b_ok) ... ok\n\n----------------------------------------------------------------------\nRan 2 tests in 0.000s\n\nOK\n'

REAL_MIXED = 'test_apos (test_mix.C.test_apos) ... skipped "can\'t run here"\ntest_doc (test_mix.C.test_doc)\nHas a docstring. ... ok\ntest_sub_err (test_mix.C.test_sub_err) ... \n  test_sub_err (test_mix.C.test_sub_err) (i=1) ... ERROR\ntest_sub_fail (test_mix.C.test_sub_fail) ... \n  test_sub_fail (test_mix.C.test_sub_fail) (i=0) ... FAIL\n  test_sub_fail (test_mix.C.test_sub_fail) (i=1) ... FAIL\ntest_sub_ok (test_mix.C.test_sub_ok) ... ok\ntest_sub_skip (test_mix.C.test_sub_skip) ... \n  test_sub_skip (test_mix.C.test_sub_skip) (i=1) ... skipped \'later\'\ntest_z_last (test_mix.C.test_z_last) ... ok\n\n======================================================================\nERROR: test_sub_err (test_mix.C.test_sub_err) (i=1)\n----------------------------------------------------------------------\nTraceback (most recent call last):\n  File "test_mix.py", line 13, in test_sub_err\n    raise RuntimeError("x")\nRuntimeError: x\n\n======================================================================\nFAIL: test_sub_fail (test_mix.C.test_sub_fail) (i=0)\n----------------------------------------------------------------------\nTraceback (most recent call last):\n  File "test_mix.py", line 10, in test_sub_fail\n    self.assertEqual(i, 5)\n    ~~~~~~~~~~~~~~~~^^^^^^\nAssertionError: 0 != 5\n\n======================================================================\nFAIL: test_sub_fail (test_mix.C.test_sub_fail) (i=1)\n----------------------------------------------------------------------\nTraceback (most recent call last):\n  File "test_mix.py", line 10, in test_sub_fail\n    self.assertEqual(i, 5)\n    ~~~~~~~~~~~~~~~~^^^^^^\nAssertionError: 1 != 5\n\n----------------------------------------------------------------------\nRan 7 tests in 0.001s\n\nFAILED (failures=2, errors=1, skipped=2)\n'


class Parsers(unittest.TestCase):
    def test_the_3_11_and_later_name_format(self):
        out = ("test_a (pkg.test_mod.Case.test_a) ... ok\ntest_b (pkg.test_mod.Case.test_b) ... FAIL\n"
               "test_c (pkg.test_mod.Case.test_c) ... ERROR\ntest_d (pkg.test_mod.Case.test_d) ... skipped 'no gpu'\n"
               "test_e (pkg.test_mod.Case.test_e) ... expected failure\n"
               "test_f (pkg.test_mod.Case.test_f) ... unexpected success\n\n"
               "Ran 6 tests in 0.1s\n\nFAILED (failures=1, errors=1, skipped=1, expected failures=1, "
               "unexpected successes=1)\n")
        self.assertEqual(fp.parse_unittest(out), {
            "pkg.test_mod.Case.test_a": "ok", "pkg.test_mod.Case.test_b": "fail",
            "pkg.test_mod.Case.test_c": "error", "pkg.test_mod.Case.test_d": "skipped",
            "pkg.test_mod.Case.test_e": "xfail", "pkg.test_mod.Case.test_f": "xpass", "": "error"})

    def test_the_older_name_format_is_completed_with_the_test_name(self):
        out = "test_a (pkg.test_mod.Case) ... ok\ntest_b (pkg.test_mod.Case) ... skipped 'x'\n\nOK (skipped=1)\n"
        self.assertEqual(fp.parse_unittest(out), {"pkg.test_mod.Case.test_a": "ok",
                                                  "pkg.test_mod.Case.test_b": "skipped", "": "ok"})

    def test_the_two_formats_name_one_test_the_same_way(self):
        self.assertEqual(fp.parse_unittest("test_a (m.C.test_a) ... ok\n"), fp.parse_unittest("test_a (m.C) ... ok\n"))

    def test_a_failures_only_summary_is_fail_and_errors_make_it_error(self):
        self.assertEqual(fp.parse_unittest("t (m.C.t) ... FAIL\n\nFAILED (failures=2)\n")[""], "fail")
        self.assertEqual(fp.parse_unittest("t (m.C.t) ... ERROR\n\nFAILED (failures=1, errors=1)\n")[""], "error")

    def test_a_docstring_puts_the_verdict_on_the_next_line(self):
        out = "test_a (m.C.test_a)\nChecks the thing ... ok\ntest_b (m.C.test_b)\nAnother thing ... FAIL\n"
        self.assertEqual(fp.parse_unittest(out), {"m.C.test_a": "ok", "m.C.test_b": "fail"})

    def test_a_test_that_printed_before_its_verdict_is_followed(self):
        out = "test_a (m.C.test_a) ... noise from the test\nmore noise\nok\ntest_b (m.C.test_b) ... ok\n"
        self.assertEqual(fp.parse_unittest(out), {"m.C.test_a": "ok", "m.C.test_b": "ok"})

    def test_subtests_keep_the_worst_verdict_under_the_test_name(self):
        out = "test_a (m.C.test_a) (i=1) ... ok\ntest_a (m.C.test_a) (i=2) ... FAIL\ntest_a (m.C.test_a) ... ok\n"
        self.assertEqual(fp.parse_unittest(out), {"m.C.test_a": "fail"})

    def test_real_output_whose_last_test_has_a_failing_subtest_parses(self):
        # The main line has no verdict, the subtest line is indented, and the next line the parser reads that starts
        # with a verdict word is the runner's `FAIL: ...` block header. This used to raise KeyError.
        self.assertEqual(fp.parse_unittest(REAL_SUB_LAST), {"test_sub_last.A.test_aa_ok": "ok",
                                                            "test_sub_last.Z.test_zz_sub": "fail", "": "fail"})

    def test_real_output_of_a_test_that_logs_before_its_verdict_parses(self):
        # `... ERROR:svc:connection refused` is log output, not a verdict; the verdict `ok` is the next line.
        self.assertEqual(fp.parse_unittest(REAL_LOGGING), {"test_log.L.test_a_logs": "ok", "test_log.L.test_b_ok": "ok",
                                                           "": "ok"})

    def test_real_output_with_subtests_docstrings_and_quoted_skips_gives_every_test_an_entry(self):
        self.assertEqual(fp.parse_unittest(REAL_MIXED), {
            "test_mix.C.test_apos": "skipped",  # skipped "can't run here": a double-quoted reason
            "test_mix.C.test_doc": "ok", "test_mix.C.test_sub_err": "error", "test_mix.C.test_sub_fail": "fail",
            "test_mix.C.test_sub_ok": "ok", "test_mix.C.test_sub_skip": "skipped", "test_mix.C.test_z_last": "ok",
            "": "error"})

    def test_a_text_that_is_not_a_verdict_is_ignored_and_never_raises(self):
        self.assertEqual(fp.parse_unittest("test_a (m.C.test_a) ... okay then\nok, more\nFAIL: x\n"), {})
        self.assertEqual(fp._unittest_verdict("nonsense"), "")
        self.assertIsNone(fp._UT_BARE.match("okay, more"))

    def test_lines_that_are_not_tests_are_ignored(self):
        out = ("ERROR: test_a (m.C.test_a)\nTraceback (most recent call last):\n  File \"x.py\", line 1, in f\n"
               "AssertionError: 1 != 2\nsomething (else) ok\n")
        self.assertEqual(fp.parse_unittest(out), {})

    def test_a_run_that_crashed_has_no_summary(self):
        self.assertNotIn("", fp.parse_unittest("test_a (m.C.test_a) ... ok\nTraceback (most recent call last):\n"))

    def test_go_events(self):
        events = [
            {"Action": "run", "Package": "ex/a", "Test": "TestOne"},
            {"Action": "output", "Package": "ex/a", "Test": "TestOne", "Output": "x\n"},
            {"Action": "pass", "Package": "ex/a", "Test": "TestOne"},
            {"Action": "fail", "Package": "ex/a", "Test": "TestTwo"},
            {"Action": "skip", "Package": "ex/a", "Test": "TestThree"},
            {"Action": "fail", "Package": "ex/a"},
            {"Action": "pass", "Package": "ex/b"},
            {"Action": "skip", "Package": "ex/c"},
        ]
        out = "build chatter\n" + "\n".join(json.dumps(e) for e in events) + "\n{broken\n"
        self.assertEqual(fp.parse_go_tests(out), {
            "ex/a": {"TestOne": "pass", "TestTwo": "fail", "TestThree": "skip", "": "fail"},
            "ex/b": {"": "pass"}, "ex/c": {"": "skip"}})

    def test_go_output_without_json_events_is_empty(self):
        self.assertEqual(fp.parse_go_tests("ok  \tex/a\t0.1s\nFAIL\n"), {})


class MergeRule(unittest.TestCase):
    def test_a_skip_yields_to_any_other_verdict_in_either_order(self):
        for first, second in (({"a": "skipped"}, {"a": "ok"}), ({"a": "ok"}, {"a": "skipped"})):
            merged, conflicts = fp.merge_tables("unittest", [first, second])
            self.assertEqual((merged, conflicts), ({"a": "ok"}, []))
        self.assertEqual(fp.merge_tables("unittest", [{"a": "skipped"}, {"a": "fail"}])[0], {"a": "fail"})

    def test_two_different_verdicts_are_a_conflict_that_keeps_the_first(self):
        merged, conflicts = fp.merge_tables("unittest", [{"a": "ok"}, {"a": "fail"}, {"a": "error"}])
        self.assertEqual(merged, {"a": "ok"})
        self.assertEqual(conflicts, [{"table": "unittest", "name": "a", "verdicts": ["error", "fail", "ok"]}])

    def test_equal_verdicts_merge_without_a_conflict(self):
        self.assertEqual(fp.merge_tables("unittest", [{"a": "ok"}, {"a": "ok"}]), ({"a": "ok"}, []))

    def test_the_summary_takes_the_worst_verdict(self):
        for parts, worst in (([{"": "ok"}, {"": "fail"}], "fail"), ([{"": "fail"}, {"": "ok"}], "fail"),
                             ([{"": "skipped"}, {"": "ok"}], "ok"), ([{"": "fail"}, {"": "error"}], "error"),
                             ([{"": "error"}, {"": "fail"}, {"": "ok"}], "error")):
            with self.subTest(parts=parts):
                merged, conflicts = fp.merge_tables("unittest", parts)
                self.assertEqual((merged, conflicts), ({"": worst}, []))

    def test_go_tables_merge_per_package_and_name_the_conflict_with_its_package(self):
        one = {"p": {"": "pass", "TestA": "pass", "TestB": "skip"}}
        two = {"p": {"": "fail", "TestA": "fail", "TestB": "pass"}, "q": {"": "pass"}}
        merged, conflicts = fp.merge_tables("go-tests", [one, two])
        self.assertEqual(merged, {"p": {"": "fail", "TestA": "pass", "TestB": "pass"}, "q": {"": "pass"}})
        self.assertEqual(conflicts, [{"table": "go-tests", "name": "p:TestA", "verdicts": ["fail", "pass"]}])

    def test_the_merge_is_sorted_for_stable_digests(self):
        merged, _ = fp.merge_tables("unittest", [{"b": "ok", "a": "ok"}])
        self.assertEqual(list(merged), ["a", "b"])


class ValidateAndPlan(Repo):
    def recorded(self, *entries, **kwargs):
        self.release(*(entries or (entry("t", say("test_a (m.C.test_a) ... ok"), kind="test", derives="unittest"),)),
                     **kwargs)
        record = self.run_ok()[3]
        self.assertTrue(record["complete"])
        return record

    def edit_record(self, change):
        path = self.root / BASE / self.record_head12 / "fingerprint.json"
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data, indent=2) + "\n")

    def test_a_fresh_record_validates(self):
        record = self.recorded()
        self.record_head12 = record["head12"]
        code, out, err = self.cli("validate", f"{BASE}/{record['head12']}")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), "fingerprint: 1 record(s) ok")
        self.assertEqual(self.cli("validate", f"{BASE}/{record['head12']}/fingerprint.json")[0], 0)

    def test_validate_with_no_path_checks_every_tracked_record_and_none_is_fine(self):
        self.assertEqual(self.cli("validate")[1].strip(), "fingerprint: 0 record(s) ok")
        record = self.recorded()
        self.commit("record")
        self.assertEqual(self.cli("validate")[1].strip(), "fingerprint: 1 record(s) ok")
        self.record_head12 = record["head12"]
        self.edit_record(lambda d: d["tables"]["unittest"].update(sha256="0" * 64))
        code, _, err = self.cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("table unittest was altered", err)

    def test_an_altered_table_digest_is_found(self):
        record = self.recorded()
        (self.root / record["tables"]["unittest"]["path"]).write_text('{"m.C.test_a": "fail"}\n')
        code, _, err = self.cli("validate", f"{BASE}/{record['head12']}")
        self.assertEqual(code, 1)
        self.assertIn("table unittest was altered", err)
        self.assertIn(record["tables"]["unittest"]["sha256"], err)

    def test_a_missing_table_file_is_found(self):
        record = self.recorded()
        (self.root / record["tables"]["unittest"]["path"]).unlink()
        code, _, err = self.cli("validate", f"{BASE}/{record['head12']}")
        self.assertEqual(code, 1)
        self.assertIn("table unittest cannot be read", err)

    def test_an_edited_release_set_hash_is_found(self):
        record = self.recorded()
        self.record_head12 = record["head12"]
        self.edit_record(lambda d: d.update(release_set_sha256="1" * 64))
        code, _, err = self.cli("validate", f"{BASE}/{record['head12']}")
        self.assertEqual(code, 1)
        self.assertIn("release_set_sha256", err)
        self.assertIn("program/release-set.yaml at", err)

    def test_a_head_missing_from_the_repository_is_not_compared(self):
        record = self.recorded()
        self.record_head12 = record["head12"]
        fake = "f" * 40
        self.edit_record(lambda d: d.update(head=fake, head12=fake[:12]))
        os.rename(self.root / BASE / record["head12"], self.root / BASE / fake[:12])
        self.record_head12 = fake[:12]
        self.edit_record(lambda d: d["tables"]["unittest"].update(path=f"{BASE}/{fake[:12]}/unittest.json"))
        self.assertEqual(self.cli("validate", f"{BASE}/{fake[:12]}")[0], 0)

    def test_a_planted_absolute_path_is_found(self):
        record = self.recorded()
        self.record_head12 = record["head12"]
        for planted in ("/Users/someone/x.py", "/home/ci/x", "/private/var/x", "/tmp/x", "C:\\\\work\\\\x"):
            with self.subTest(planted):
                self.edit_record(lambda d: d["commands"][0].update(output_tail=f"at {planted} line 1"))
                code, _, err = self.cli("validate", f"{BASE}/{record['head12']}")
                self.assertEqual(code, 1)
                self.assertIn("holds an absolute path", err)

    def test_a_record_in_the_wrong_directory_is_found(self):
        record = self.recorded()
        os.rename(self.root / BASE / record["head12"], self.root / BASE / "000000000000")
        code, _, err = self.cli("validate", f"{BASE}/000000000000")
        self.assertEqual(code, 1)
        self.assertIn("is not the name of its directory", err)

    def test_schema_problems_are_found(self):
        record = self.recorded()
        self.record_head12 = record["head12"]
        cases = {
            "missing key": (lambda d: d.pop("tracks"), "missing key: tracks"),
            "bad schema": (lambda d: d.update(schema="other"), "schema: must be"),
            "bad head": (lambda d: d.update(head="abc"), "head: must be"),
            "bad time": (lambda d: d.update(produced_at="yesterday"), "produced_at"),
            "complete lies": (lambda d: d.update(complete=False), "complete: does not match"),
            "all_as_expected lies": (lambda d: d.update(all_as_expected=False), "all_as_expected: does not match"),
            "row not as expected but marked": (lambda d: d["commands"][0].update(exit=1), "as_expected is true"),
            "row key missing": (lambda d: d["commands"][0].pop("seconds"), "commands[0]"),
            "bad kind": (lambda d: d["commands"][0].update(kind="lint"), "kind is not valid"),
            "tracks differ from rows": (lambda d: d["tracks"]["plain"].update(commands=["other"]), "tracks"),
            "table path elsewhere": (lambda d: d["tables"]["unittest"].update(path="x/unittest.json"), "tables.unittest.path"),
        }
        pristine = (self.root / BASE / record["head12"] / "fingerprint.json").read_text()
        for name, (change, needle) in cases.items():
            with self.subTest(name):
                (self.root / BASE / record["head12"] / "fingerprint.json").write_text(pristine)
                self.edit_record(change)
                code, _, err = self.cli("validate", f"{BASE}/{record['head12']}")
                self.assertEqual(code, 1)
                self.assertIn(needle, err)

    def test_a_path_outside_the_repository_and_a_missing_record_are_refused(self):
        self.assertEqual(self.cli("validate", str(self.root.parent))[0], 1)
        code, _, err = self.cli("validate", f"{BASE}/ffffffffffff")
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)

    def test_the_required_keys_come_from_the_schema_file(self):
        schema = json.loads((Path(fp.__file__).resolve().parent.parent / "templates"
                             / "fingerprint.schema.json").read_text())
        self.assertEqual(fp._required(), schema["required"])
        self.assertEqual(schema["properties"]["schema"], {"const": fp.SCHEMA})
        self.assertEqual(sorted(schema["properties"]["commands"]["items"]["required"]),
                         sorted(["id", "track", "kind", "argv", "env_keys", "expected_exit", "exit", "as_expected",
                                 "seconds", "output_sha256", "output_tail", "derives", "problem"]))

    def test_plan_lists_the_tracks_with_no_seconds_before_any_record_and_runs_nothing(self):
        marker = self.root.parent / "ran.txt"
        self.release(entry("one", "open(%r, 'w').write('x')" % str(marker)), entry("two", "pass", track="other"))
        code, out, _ = self.cli("plan")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual([lines[0], lines[2]], ["track plain", "track other"])
        self.assertTrue(lines[1].startswith("  one\tcheck\t-\t"))
        self.assertTrue(lines[3].startswith("  two\tcheck\t-\t"))
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / BASE).exists())

    def test_plan_prints_the_seconds_the_current_record_took(self):
        self.release(entry("one", "import time; time.sleep(0.3)"), entry("two", "pass", track="other"))
        record = self.run_ok()[3]
        self.commit("record")
        seconds = {row["id"]: row["seconds"] for row in record["commands"]}
        self.assertGreaterEqual(seconds["one"], 0.3)
        out = self.cli("plan")[1]
        self.assertIn(f"  one\tcheck\t{seconds['one']:g}s\t", out)
        self.assertIn(f"  two\tcheck\t{seconds['two']:g}s\t", out)

    def test_plan_with_an_invalid_release_set_is_refused(self):
        self.release(entry("one", "pass"), entry("one", "pass"))
        code, _, err = self.cli("plan")
        self.assertEqual(code, 1)
        self.assertIn("used twice", err)


class Immutability(Repo):
    def committed_record(self):
        self.release(entry("one", say("hi")))
        head12 = self.run_ok()[3]["head12"]
        self.commit("record the fingerprint")
        return head12

    def test_adding_a_record_is_not_a_rewrite(self):
        self.committed_record()
        self.assertEqual(store.rewritten(self.root), [])
        self.assertEqual(store.check(self.root)[0], [])

    def test_a_commit_that_modifies_a_tracked_fingerprint_file_is_refused(self):
        head12 = self.committed_record()
        path = self.root / BASE / head12 / "fingerprint.json"
        path.write_text(path.read_text().replace('"complete": true', '"complete": false'))
        commit = self.commit("tidy")
        self.assertEqual(store.rewritten(self.root),
                         [f"evidence history rewritten: {BASE}/{head12}/fingerprint.json modified in {commit}"])
        self.assertEqual(store.check(self.root)[0], store.rewritten(self.root))

    def test_a_commit_that_deletes_one_is_refused(self):
        head12 = self.committed_record()
        (self.root / BASE / head12 / "fingerprint.json").unlink()
        commit = self.commit("remove")
        self.assertEqual(store.rewritten(self.root),
                         [f"evidence history rewritten: {BASE}/{head12}/fingerprint.json deleted in {commit}"])

    def test_a_table_file_is_covered_too(self):
        self.release(entry("t", say("test_a (m.C.test_a) ... ok"), derives="unittest"))
        head12 = self.run_ok()[3]["head12"]
        self.commit("record")
        (self.root / BASE / head12 / "unittest.json").write_text("{}\n")
        commit = self.commit("edit the table")
        self.assertEqual(store.rewritten(self.root),
                         [f"evidence history rewritten: {BASE}/{head12}/unittest.json modified in {commit}"])

    def test_pk_evidence_validate_and_pk_doctor_refuse_it(self):
        head12 = self.committed_record()
        (self.root / "program" / "checks.yaml").write_text("build:\n  - - x\ntest:\n  - - x\nscan:\n  - - x\n")
        self.commit("checks")
        (self.root / BASE / head12 / "fingerprint.json").unlink()
        self.commit("remove")
        for command in ("evidence", "validate"), ("doctor",):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.chdir(self.root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = pk.main(list(command))
            self.assertEqual(code, 1, command)
            self.assertIn(f"evidence history rewritten: {BASE}/{head12}/fingerprint.json deleted", err.getvalue())

    def test_a_task_records_edit_is_still_refused(self):
        first = store.append(self.root, "T-1", "results", {"x": 1})
        second = store.append(self.root, "T-2", "results", {"x": 2})
        self.commit("records")
        for appended in (first, second):
            appended["file"].chmod(0o644)
            appended["file"].write_text("{}\n")
        commit = self.commit("edit both")
        found = store.rewritten(self.root)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(commit in line for line in found))

    def test_a_task_cannot_be_named_fingerprint(self):
        # Its pointer would be program/evidence/fingerprint/latest.json, rewritten by every record: a permanent
        # "history rewritten" in the fingerprint directory.
        for call in (lambda: store.check_task("fingerprint"), lambda: store.pointer_path("fingerprint"),
                     lambda: store.record(self.root, "fingerprint", "results", {"head": "a" * 40, "produced_at": "t"})):
            with self.assertRaisesRegex(KitError, "'fingerprint' names the release fingerprint directory"):
                call()
        self.assertFalse((self.root / BASE).exists())
        self.assertEqual(store.check_task("fingerprint-2"), "fingerprint-2")


REAL_SHAPES = (  # path shapes a boundary-checking pattern misses: a letter or underscore before the prefix
    ("ld: warning: directory not found for option '-L/Us", "ers/someone/lib'"),
    ("cc -I/ho", "me/someone/include x.c"),
    ("ok _/Us", "ers/someone/gopath/x 0.1s"),
)  # each pair is joined by the command, so the program's own argv holds no absolute path


class FixRound(Repo):
    def test_path_shapes_with_a_letter_before_the_prefix_are_scrubbed_and_refused(self):
        code = "\n".join(f"print({head!r} + {tail!r})" for head, tail in REAL_SHAPES)
        self.release(entry("shapes", code))
        record = self.run_ok()[3]
        tail = record["commands"][0]["output_tail"].splitlines()
        self.assertEqual(tail, ["ld: warning: directory not found for option '-L<path>'", "cc -I<path> x.c",
                                "ok _<path> 0.1s"])
        self.assertIsNone(fp.ABSOLUTE_PATH.search(self.text()))
        for shape in ("-L/Users/someone/lib", "-I/home/someone/include", "_/Users/someone/gopath/x", "x/private/y"[1:]):
            with self.subTest(shape):
                self.assertIsNotNone(fp.ABSOLUTE_PATH.search(f"tail {shape} end"))
        for fine in ("/usersx", "src/tmpfile", "a /tmp.txt", "key:/value"):
            with self.subTest(fine):
                self.assertIsNone(fp.ABSOLUTE_PATH.search(fine))
        path = self.root / BASE / record["head12"] / "fingerprint.json"
        data = json.loads(path.read_text())
        data["commands"][0]["output_tail"] = "ld: -L/Users/someone/lib"
        path.write_text(json.dumps(data, indent=2) + "\n")
        code, _, err = self.cli("validate", f"{BASE}/{record['head12']}")
        self.assertEqual(code, 1)
        self.assertIn("holds an absolute path", err)

    def test_path_literals_are_replaced_before_env_values(self):
        self.release(entry("cwd", "import os; print('cwd', os.getcwd())", env={"SEG": self.root.name}))
        self.assertEqual(self.run_ok()[3]["commands"][0]["output_tail"], "cwd <root>")

    def test_the_validator_refuses_an_env_value_in_the_text_a_command_echoed(self):
        self.release(entry("uses-env", "import os; print('seg', os.environ['SEG'])", env={"SEG": "scratchpad"}),
                     entry("short", "import os; print('pin', os.environ['PIN'])", env={"PIN": "a9z"}))
        record = self.run_ok()[3]
        directory = f"{BASE}/{record['head12']}"
        self.assertEqual(self.cli("validate", directory)[0], 0)
        path = self.root / directory / "fingerprint.json"
        data = json.loads(path.read_text())
        data["commands"][0]["output_tail"] = "seg scratchpad"
        data["commands"][1]["output_tail"] = "pin a9z"  # under 4 characters: the scrubber keeps such values
        path.write_text(json.dumps(data, indent=2) + "\n")
        code, _, err = self.cli("validate", directory)
        self.assertEqual(code, 1)
        self.assertIn("entry uses-env: the value of env SEG appears in the record", err)
        self.assertNotIn("PIN", err)
        self.assertNotIn("scratchpad", err)  # the value itself is never printed

    def test_the_validator_ties_the_rows_to_the_release_set_at_head(self):
        self.release(entry("good", say("fine")), entry("bad", "import sys; sys.exit(3)", track="two"))
        record = self.run_ok()[3]
        self.assertFalse(record["all_as_expected"])
        directory = f"{BASE}/{record['head12']}"
        self.assertEqual(self.cli("validate", directory)[0], 0)  # red but honest
        path = self.root / directory / "fingerprint.json"
        pristine = path.read_text()
        data = json.loads(pristine)
        data["commands"] = [row for row in data["commands"] if row["id"] != "bad"]
        del data["tracks"]["two"]
        data["all_as_expected"] = True
        path.write_text(json.dumps(data, indent=2) + "\n")
        code, _, err = self.cli("validate", directory)
        self.assertEqual(code, 1)
        self.assertIn("not the entries of the release set", err)
        self.assertIn("missing: bad", err)
        for change in (lambda d: d["commands"][0].update(argv=["true"]), lambda d: d["commands"][0].update(kind="gate"),
                       lambda d: d["commands"][1].update(expected_exit=3)):
            data = json.loads(pristine)
            change(data)
            path.write_text(json.dumps(data, indent=2) + "\n")
            self.assertEqual(self.cli("validate", directory)[0], 1)

    def test_an_only_record_is_compared_with_that_track_alone(self):
        self.release(entry("one", say("1")), entry("two", say("2"), track="other"))
        record = self.run_ok("--only", "other")[3]
        self.assertEqual(self.cli("validate", f"{BASE}/{record['head12']}")[0], 0)

    def test_an_untracked_path_in_the_fingerprint_directory_that_is_not_a_record_makes_the_tree_dirty(self):
        self.release(entry("one", "pass"))
        stray = self.root / BASE / "notes.txt"
        stray.parent.mkdir(parents=True)
        stray.write_text("x\n")
        code, _, err = self.cli("run", "--scratch", str(self.scratch))
        self.assertEqual(code, 1)
        self.assertIn("working tree is not clean: " + BASE + "/notes.txt", err)

    def test_a_command_that_writes_into_the_fingerprint_directory_is_seen(self):
        self.release(entry("writer", f"import pathlib; pathlib.Path({BASE!r}, 'other.txt').parent.mkdir(parents=True, "
                                     f"exist_ok=True); pathlib.Path({BASE!r}, 'other.txt').write_text('x')"))
        code, _, err, record = self.run_ok()
        self.assertEqual(code, 1)
        self.assertFalse(record["tree_unchanged_by_run"])

    def test_a_scratch_directory_inside_the_repository_is_refused(self):
        self.release(entry("one", "pass"))
        code, _, err = self.cli("run", "--scratch", str(self.root / "logs"))
        self.assertEqual(code, 1)
        self.assertIn("is inside the repository", err)
        self.assertFalse((self.root / BASE).exists())

    def test_a_toolchain_command_that_dirties_the_tree_is_seen(self):
        self.release(entry("one", "pass"), toolchain=[[PY, "-c", "open('made-by-toolchain.txt', 'w').write('x')"]])
        code, _, err, record = self.run_ok()
        self.assertEqual(code, 1)
        self.assertFalse(record["tree_unchanged_by_run"])
        self.assertIn("made-by-toolchain.txt", err)

    def test_changes_hidden_by_skip_worktree_or_assume_unchanged_are_refused(self):
        self.release(entry("one", "pass"))
        for flag in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(flag):
                sh(self.root, "git", "update-index", flag, "a.txt")
                code, _, err = self.cli("run", "--scratch", str(self.scratch))
                self.assertEqual(code, 1)
                self.assertIn("git hides changes to tracked files", err)
                self.assertIn("a.txt", err)
                sh(self.root, "git", "update-index", "--no-" + flag[2:], "a.txt")
        self.assertEqual(self.cli("run", "--scratch", str(self.scratch))[0], 0)

    def test_an_untracked_symlink_at_the_record_directory_is_replaced_and_its_target_kept(self):
        self.release(entry("one", "pass"))
        head12 = sh(self.root, "git", "rev-parse", "HEAD").strip()[:12]
        target = self.root.parent / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("keep\n")
        (self.root / BASE).mkdir(parents=True)
        os.symlink(target, self.root / BASE / head12)
        code, _, err, record = self.run_ok()
        self.assertEqual((code, err), (0, ""))
        self.assertFalse((self.root / BASE / head12).is_symlink())
        self.assertEqual((target / "keep.txt").read_text(), "keep\n")

    def test_an_empty_program_name_is_refused(self):
        with self.assertRaisesRegex(KitError, "entry e: argv: the first element"):
            fp.parse_release_set("entries:\n  - id: e\n    argv:\n      - ''\n    track: t\n    kind: check\n")


if __name__ == "__main__":
    unittest.main()
