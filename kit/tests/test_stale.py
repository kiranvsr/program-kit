import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import pk
import stale
import store


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        self.put("calc.py", "v1\n")
        self.put("lib.py", "lib1\n")
        self.record("T-1", {"calc.py": digest("v1\n"), "lib.py": digest("lib1\n")})
        self.record("T-2", {"lib.py": digest("lib1\n")})
        self.commit("init")

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def record(self, task, inputs, **extra):
        """Append a results record for `task` and point its pointer at it; its path."""
        return store.record(self.root, task, "results", {"task_id": task, "inputs": inputs, **extra})["path"]

    def raw_record(self, task, raw: bytes):
        """Append bytes that are no usable record, and point the task's pointer at them."""
        appended = store.append(self.root, task, "results", raw)
        store.set_entry(self.root, task, "results", {"digest": appended["digest"], "path": appended["path"],
                                                     "head": "", "produced_at": ""})
        return appended["path"]

    def path_of(self, task):
        return store.latest(store.read_index(self.root), task, "results")["path"]

    def commit(self, message):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", message)


class DriftReport(Base):
    def rows(self, report):
        return {row["task_id"]: (row["state"], row["changed_inputs"]) for row in report["tasks"]}

    def test_every_latest_record_is_fresh_or_drifted_with_its_changed_inputs(self):
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        report = stale.drift_report(self.root, "main")
        self.assertEqual(report["ref"], sh(self.root, "git", "rev-parse", "main").strip())
        self.assertEqual(report["tasks"][0], {"task_id": "T-1", "path": self.path_of("T-1"), "head": "",
                                              "fingerprint": None, "state": "drifted", "changed_inputs": ["calc.py"]})
        self.assertEqual(self.rows(report), {"T-1": ("drifted", ["calc.py"]), "T-2": ("fresh", [])})
        self.assertEqual((report["drifted"], report["unreadable"], report["checked"]), (1, [], 2))

    def test_every_row_has_a_fingerprint_field_null_without_a_fingerprint(self):
        report = stale.drift_report(self.root, "main")
        self.assertEqual([row["fingerprint"] for row in report["tasks"]], [None, None])

    def test_nothing_changed_is_all_fresh(self):
        report = stale.drift_report(self.root, "main")
        self.assertEqual((report["drifted"], report["checked"]), (0, 2))

    def test_a_drifted_record_is_never_rewritten_and_a_refresh_makes_its_task_fresh(self):
        old = self.path_of("T-1")
        before = (self.root / old).read_bytes()
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        self.assertEqual(self.rows(stale.drift_report(self.root, "main"))["T-1"], ("drifted", ["calc.py"]))
        self.assertEqual((self.root / old).read_bytes(), before)  # drift changes nothing in the record
        self.record("T-1", {"calc.py": digest("v2\n"), "lib.py": digest("lib1\n")})  # the task's own refresh
        self.commit("refresh T-1")
        report = stale.drift_report(self.root, "main")
        self.assertEqual(self.rows(report), {"T-1": ("fresh", []), "T-2": ("fresh", [])})
        self.assertTrue((self.root / old).is_file())  # still in the history

    def test_input_deleted_is_drift_for_every_record_that_bound_it(self):
        sh(self.root, "git", "rm", "-q", "lib.py")
        self.commit("drop lib")
        self.assertEqual(self.rows(stale.drift_report(self.root, "main")),
                         {"T-1": ("drifted", ["lib.py"]), "T-2": ("drifted", ["lib.py"])})

    def test_working_copy_edits_do_not_count_only_the_commit_does(self):
        (self.root / "calc.py").write_text("dirty\n")
        self.assertEqual(stale.drift_report(self.root, "main")["drifted"], 0)

    def test_unreadable_records_are_reported_never_fresh_never_raised(self):
        paths = [self.raw_record("A-bad", b"{not json"), self.raw_record("B-list", b"[]"),
                 self.record("C-noinputs", None), self.record("D-empty", {})]
        self.commit("bad records")
        report = stale.drift_report(self.root, "main")
        self.assertEqual(report["drifted"], 0)
        self.assertEqual([r["path"] for r in report["unreadable"]], paths)
        for item in report["unreadable"]:
            self.assertEqual(item["changed_inputs"], [])
            self.assertEqual(len(item["error"].splitlines()), 1)
        self.assertEqual(report["checked"], 6)

    def test_a_record_missing_or_altered_is_unreadable(self):
        path = self.root / self.path_of("T-1")
        path.chmod(0o644)
        path.write_text("{}\n")  # the planted defect: an edited record
        (self.root / self.path_of("T-2")).chmod(0o644)
        (self.root / self.path_of("T-2")).unlink()
        report = stale.drift_report(self.root, "main")
        errors = {row["task_id"]: row["error"] for row in report["unreadable"]}
        self.assertTrue(errors["T-1"].startswith("immutable record altered: "), errors)
        self.assertIn("is missing", errors["T-2"])
        self.assertEqual(report["tasks"], [])

    def test_input_hash_shared_between_records_is_computed_once(self):
        calls = []
        real = stale._blob_bytes

        def spy(root, blob):
            calls.append(blob)
            return real(root, blob)

        stale._blob_bytes = spy
        self.addCleanup(setattr, stale, "_blob_bytes", real)
        stale.drift_report(self.root, "main")
        self.assertEqual(len(calls), 2)  # calc.py and lib.py, though lib.py is an input of both records

    def test_unresolvable_ref_is_a_kit_error(self):
        with self.assertRaises(stale.KitError):
            stale.drift_report(self.root, "nope")
        with self.assertRaises(stale.KitError):
            stale.drift_report(self.root, "--all")

    def test_no_pointer_checks_nothing(self):
        sh(self.root, "git", "rm", "-rq", "program")
        self.commit("no evidence")
        report = stale.drift_report(self.root, "main")
        self.assertEqual((report["tasks"], report["unreadable"], report["checked"]), ([], [], 0))

    def test_deeply_nested_record_is_unreadable_and_the_rest_are_reported(self):
        deep = self.raw_record("A-deep", b"[" * 100000)
        self.put("calc.py", "v2\n")
        self.commit("deep record, changed calc")
        report = stale.drift_report(self.root, "main")
        self.assertEqual([r["path"] for r in report["unreadable"]], [deep])
        self.assertEqual(len(report["unreadable"][0]["error"].splitlines()), 1)
        self.assertEqual(self.rows(report)["T-1"], ("drifted", ["calc.py"]))

    def test_non_string_input_hash_is_drift_not_a_crash(self):
        self.record("T-3", {"calc.py": 5, "lib.py": None, "sub/x": ["a"]})
        self.commit("odd hashes")
        self.assertEqual(self.rows(stale.drift_report(self.root, "main"))["T-3"],
                         ("drifted", ["calc.py", "lib.py", "sub/x"]))


class ChangedInputs(Base):
    def test_changed_inputs_compares_recorded_hashes_with_the_blobs_at_a_commit(self):
        first = sh(self.root, "git", "rev-parse", "HEAD").strip()
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        second = sh(self.root, "git", "rev-parse", "HEAD").strip()
        self.put("calc.py", "uncommitted\n")  # the working copy never matters
        inputs = {"lib.py": digest("lib1\n"), "gone.py": digest("x"), "calc.py": digest("v1\n")}
        self.assertEqual(stale.changed_inputs(self.root, first, inputs), {"gone.py": None})
        changed = stale.changed_inputs(self.root, second, inputs)
        self.assertEqual(changed, {"calc.py": digest("v2\n"), "gone.py": None})
        self.assertEqual(list(changed), ["calc.py", "gone.py"])
        self.assertEqual(stale.changed_inputs(self.root, second, {}), {})


class Cli(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                contextlib.chdir(self.root):
            code = pk.main(["stale", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_prints_sorted_json_and_exits_zero_even_when_drifted(self):
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["tasks"][0]["changed_inputs"], ["calc.py"])
        self.assertEqual(out, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def test_strict_exits_one_when_a_record_drifted_or_is_unreadable(self):
        self.assertEqual(self.run_cli("--strict")[0], 0)
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        self.assertEqual(self.run_cli("--strict")[0], 1)
        self.record("T-1", {"calc.py": digest("v2\n"), "lib.py": digest("lib1\n")})
        self.commit("refresh T-1")
        self.assertEqual(self.run_cli("--strict")[0], 0)
        self.raw_record("T-9", b"{")
        self.commit("unreadable")
        self.assertEqual(self.run_cli("--strict")[0], 1)

    def test_compares_with_head_and_takes_no_main_option(self):
        sh(self.root, "git", "branch", "other")
        code, out, err = self.run_cli("--main", "other")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: unrecognized arguments: --main other\n")
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ref"], sh(self.root, "git", "rev-parse", "HEAD").strip())


if __name__ == "__main__":
    unittest.main()
