import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evidence
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
        self.root = Path(tmp.name).resolve() / "repo"  # the evidence store is its sibling
        self.root.mkdir()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(store.ENV, None)
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        self.put("calc.py", "v1\n")
        self.put("lib.py", "lib1\n")
        self.store, _ = store.init(self.root)
        self.record("T-1", {"calc.py": digest("v1\n"), "lib.py": digest("lib1\n")})
        self.record("T-2", {"lib.py": digest("lib1\n")})
        self.commit("init")

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def record(self, task, inputs, **extra):
        """Append a results record for `task` and point the index at it; its store path."""
        return store.record(self.root, task, "results", {"task_id": task, "inputs": inputs, **extra})["path"]

    def raw_record(self, task, raw: bytes):
        """Append bytes that are no usable record, and point the index at them."""
        appended = store.append(self.root, task, "results", raw)
        store.set_entry(self.root, task, "results", {"digest": appended["digest"], "path": appended["path"],
                                                     "head": "", "produced_at": ""})
        return appended["path"]

    def path_of(self, task):
        return store.latest(store.read_index(self.root), task, "results")["path"]

    def commit(self, message):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", message)


class StaleRecords(Base):
    def test_stale_lists_record_whose_input_changed_on_main(self):
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        report = stale.stale_records(self.root, "main")
        self.assertEqual(report["main"], sh(self.root, "git", "rev-parse", "main").strip())
        self.assertEqual(report["stale"], [{"task_id": "T-1", "path": self.path_of("T-1"),
                                            "changed_inputs": ["calc.py"], "superseded_children": []}])
        self.assertEqual(report["unreadable"], [])
        self.assertEqual(report["checked"], 2)

    def test_stale_ignores_untouched_records(self):
        report = stale.stale_records(self.root, "main")
        self.assertEqual(report["stale"], [])
        self.assertEqual(report["checked"], 2)

    def test_input_deleted_on_main_is_changed(self):
        sh(self.root, "git", "rm", "-q", "lib.py")
        self.commit("drop lib")
        report = stale.stale_records(self.root, "main")
        self.assertEqual([(r["task_id"], r["changed_inputs"]) for r in report["stale"]],
                         [("T-1", ["lib.py"]), ("T-2", ["lib.py"])])

    def test_only_the_latest_record_counts_and_a_superseded_one_is_never_stale(self):
        old = self.path_of("T-1")
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        self.assertEqual([r["path"] for r in stale.stale_records(self.root, "main")["stale"]], [old])
        fresh = self.record("T-1", {"calc.py": digest("v2\n"), "lib.py": digest("lib1\n")})  # the refresh
        report = stale.stale_records(self.root, "main")
        self.assertEqual((report["stale"], report["checked"]), ([], 2))
        self.assertTrue((self.store / old).is_file())  # still in the history
        self.assertNotEqual(fresh, old)

    def test_a_gate_record_is_stale_when_a_child_it_binds_is_no_longer_the_latest(self):
        child = store.latest(store.read_index(self.root), "T-2", "results")["digest"]
        self.record("G-P0", {"lib.py": digest("lib1\n")},
                    children=[{"task_id": "T-2", "kind": "results", "digest": child}])
        self.assertEqual(stale.stale_records(self.root, "main")["stale"], [])
        self.record("T-2", {"lib.py": digest("lib1\n")}, refreshed=True)
        report = stale.stale_records(self.root, "main")
        self.assertEqual([(r["task_id"], r["changed_inputs"], r["superseded_children"]) for r in report["stale"]],
                         [("G-P0", [], ["T-2 results"])])

    def test_working_copy_edits_do_not_count_only_main_does(self):
        (self.root / "calc.py").write_text("dirty\n")
        self.assertEqual(stale.stale_records(self.root, "main")["stale"], [])

    def test_unreadable_records_are_reported_never_fresh_never_raised(self):
        paths = [self.raw_record("A-bad", b"{not json"), self.raw_record("B-list", b"[]"),
                 self.record("C-noinputs", None), self.record("D-empty", {})]
        self.commit("bad records")
        report = stale.stale_records(self.root, "main")
        self.assertEqual(report["stale"], [])
        self.assertEqual([r["path"] for r in report["unreadable"]], paths)
        for item in report["unreadable"]:
            self.assertEqual(item["changed_inputs"], [])
            self.assertEqual(len(item["error"].splitlines()), 1)
        self.assertEqual(report["checked"], 6)

    def test_input_hash_shared_between_records_is_computed_once(self):
        calls = []
        real = stale._blob_bytes

        def spy(root, blob):
            calls.append(blob)
            return real(root, blob)

        stale._blob_bytes = spy
        self.addCleanup(setattr, stale, "_blob_bytes", real)
        stale.stale_records(self.root, "main")
        self.assertEqual(len(calls), 2)  # calc.py and lib.py, though lib.py is an input of both records

    def test_unresolvable_main_is_a_kit_error(self):
        with self.assertRaises(stale.KitError):
            stale.stale_records(self.root, "nope")
        with self.assertRaises(stale.KitError):
            stale.stale_records(self.root, "--all")

    def test_no_index_checks_nothing(self):
        sh(self.root, "git", "rm", "-rq", "program")
        self.commit("no evidence")
        report = stale.stale_records(self.root, "main")
        self.assertEqual((report["stale"], report["unreadable"], report["checked"]), ([], [], 0))

    def test_a_record_missing_or_altered_in_the_store_is_unreadable(self):
        path = self.store / self.path_of("T-1")
        path.chmod(0o644)
        path.write_text("{}\n")  # the planted defect: an edited record
        (self.store / self.path_of("T-2")).unlink()
        report = stale.stale_records(self.root, "main")
        self.assertEqual(report["stale"], [])
        errors = {row["task_id"]: row["error"] for row in report["unreadable"]}
        self.assertTrue(errors["T-1"].startswith("immutable record altered: "), errors)
        self.assertIn("is missing from the store", errors["T-2"])


class Robustness(Base):
    def test_deeply_nested_record_is_unreadable_and_the_rest_are_reported(self):
        deep = self.raw_record("A-deep", b"[" * 100000)
        self.put("calc.py", "v2\n")
        self.commit("deep record, changed calc")
        report = stale.stale_records(self.root, "main")
        self.assertEqual([r["path"] for r in report["unreadable"]], [deep])
        self.assertEqual(len(report["unreadable"][0]["error"].splitlines()), 1)
        self.assertEqual([r["task_id"] for r in report["stale"]], ["T-1"])
        self.assertEqual(report["checked"], 3)

    def test_unreadable_errors_name_the_record_by_its_store_path(self):
        self.raw_record("A-bad", b"{not json")
        self.commit("bad records")
        report = stale.stale_records(self.root, "main")
        self.assertEqual(len(report["unreadable"]), 1)
        for item in report["unreadable"]:
            self.assertNotIn(str(self.root), item["error"])
            self.assertIn(item["path"], item["error"])

    def test_non_string_input_hash_is_reported_stale_not_a_crash(self):
        self.record("T-3", {"calc.py": 5, "lib.py": None, "sub/x": ["a"]})
        self.commit("odd hashes")
        report = stale.stale_records(self.root, "main")
        row = [r for r in report["stale"] if r["task_id"] == "T-3"]
        self.assertEqual(row[0]["changed_inputs"], ["calc.py", "lib.py", "sub/x"])
        self.assertEqual(report["unreadable"], [])


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

    def test_prints_sorted_json_and_exits_zero_even_when_stale(self):
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["stale"][0]["changed_inputs"], ["calc.py"])
        self.assertEqual(out, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def test_no_main_option_and_unresolvable_main(self):
        sh(self.root, "git", "branch", "other")
        code, out, err = self.run_cli("--main", "other")  # the primary branch is fixed: no flag can move it
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: unrecognized arguments: --main other\n")
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["main"], sh(self.root, "git", "rev-parse", "main").strip())
        sh(self.root, "git", "checkout", "-q", "other")
        sh(self.root, "git", "branch", "-D", "main")
        code, out, err = self.run_cli()
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: "))


class EvidenceIntegration(Base):
    def test_evidence_stale_uses_the_real_module(self):
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        bad = self.raw_record("T-0", b"{not json")
        self.commit("unreadable record")
        ev = evidence.Evidence(self.root, "T-9")
        got = ev.stale()
        self.assertEqual(got["checked"], True)
        self.assertEqual(got["records"], [{"task_id": "T-1", "path": self.path_of("T-1"),
                                           "changed_inputs": ["calc.py"], "superseded_children": []}])
        self.assertEqual([r["path"] for r in got["unreadable"]], [bad])
        self.assertEqual(ev.stale_result, got)


if __name__ == "__main__":
    unittest.main()
