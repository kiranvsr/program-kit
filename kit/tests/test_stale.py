import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import evidence
import pk
import stale


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

    def record(self, task, inputs):
        self.put(f"program/evidence/{task}/results.json", json.dumps({"task_id": task, "inputs": inputs}))

    def commit(self, message):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", message)


class StaleRecords(Base):
    def test_stale_lists_record_whose_input_changed_on_main(self):
        self.put("calc.py", "v2\n")
        self.commit("change calc")
        report = stale.stale_records(self.root, "main")
        self.assertEqual(report["main"], sh(self.root, "git", "rev-parse", "main").strip())
        self.assertEqual(report["stale"], [{"path": "program/evidence/T-1/results.json",
                                            "changed_inputs": ["calc.py"]}])
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
        self.assertEqual([(r["path"], r["changed_inputs"]) for r in report["stale"]],
                         [("program/evidence/T-1/results.json", ["lib.py"]),
                          ("program/evidence/T-2/results.json", ["lib.py"])])

    def test_working_copy_edits_do_not_count_only_main_does(self):
        (self.root / "calc.py").write_text("dirty\n")
        self.assertEqual(stale.stale_records(self.root, "main")["stale"], [])

    def test_unreadable_records_are_reported_never_fresh_never_raised(self):
        self.put("program/evidence/A-bad/results.json", "{not json")
        self.put("program/evidence/B-list/results.json", "[]")
        self.put("program/evidence/C-noinputs/results.json", json.dumps({"task_id": "x"}))
        self.put("program/evidence/D-empty/results.json", json.dumps({"inputs": {}}))
        self.commit("bad records")
        report = stale.stale_records(self.root, "main")
        self.assertEqual(report["stale"], [])
        self.assertEqual([r["path"] for r in report["unreadable"]],
                         [f"program/evidence/{n}/results.json" for n in ("A-bad", "B-list", "C-noinputs", "D-empty")])
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

    def test_no_evidence_directory_checks_nothing(self):
        sh(self.root, "git", "rm", "-rq", "program")
        self.commit("no evidence")
        report = stale.stale_records(self.root, "main")
        self.assertEqual((report["stale"], report["unreadable"], report["checked"]), ([], [], 0))


class Robustness(Base):
    def test_deeply_nested_record_is_unreadable_and_the_rest_are_reported(self):
        self.put("program/evidence/A-deep/results.json", "[" * 100000)
        self.put("calc.py", "v2\n")
        self.commit("deep record, changed calc")
        report = stale.stale_records(self.root, "main")
        self.assertEqual([r["path"] for r in report["unreadable"]], ["program/evidence/A-deep/results.json"])
        self.assertIn("nested too deeply", report["unreadable"][0]["error"])
        self.assertEqual([r["path"] for r in report["stale"]], ["program/evidence/T-1/results.json"])
        self.assertEqual(report["checked"], 3)

    def test_unreadable_errors_are_root_relative(self):
        self.put("program/evidence/A-bad/results.json", "{not json")
        self.put("program/evidence/B-deep/results.json", "[" * 100000)
        self.commit("bad records")
        report = stale.stale_records(self.root, "main")
        self.assertEqual(len(report["unreadable"]), 2)
        for item in report["unreadable"]:
            self.assertNotIn(str(self.root), item["error"])
            self.assertIn(item["path"], item["error"])

    def test_non_string_input_hash_is_reported_stale_not_a_crash(self):
        self.record("T-3", {"calc.py": 5, "lib.py": None, "sub/x": ["a"]})
        self.commit("odd hashes")
        report = stale.stale_records(self.root, "main")
        row = [r for r in report["stale"] if r["path"].endswith("T-3/results.json")]
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
        self.put("program/evidence/T-0/results.json", "{not json")
        self.commit("unreadable record")
        ev = evidence.Evidence(self.root, "T-9")
        got = ev.stale()
        self.assertEqual(got["checked"], True)
        self.assertEqual(got["records"], [{"path": "program/evidence/T-1/results.json",
                                           "changed_inputs": ["calc.py"]}])
        self.assertEqual([r["path"] for r in got["unreadable"]], ["program/evidence/T-0/results.json"])
        self.assertEqual(ev.stale_result, got)


if __name__ == "__main__":
    unittest.main()
