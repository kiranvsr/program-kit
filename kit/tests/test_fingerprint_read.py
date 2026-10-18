"""The release fingerprint readers: the current fingerprint, show, the coverage classes, the stale field, the hand-off line."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import fingerprint as fp
import handoff
import pk
import stale
import store
from test_fingerprint import PY, Repo, entry, sh

BASE = fp.BASE
NO_FINGERPRINT, INCOMPLETE = "no fingerprint", "fingerprint incomplete"
ADHOC, NONZERO, ENV = "ad-hoc command", "expected non-zero exit", "env override"
SKIPPED, ABSENT, BUILD = "all matched entries skipped", "package not in the table", "build-changing flag"


def head(root):
    return sh(root, "git", "rev-parse", "HEAD").strip()


class Recorded(Repo):
    """A repository whose release set has been run at least once; `record_here` commits a record for HEAD."""

    def record_here(self):
        record = self.run_ok()[3]
        self.commit("record")
        return record

    def change(self, name="a.txt", text="changed\n"):
        (self.root / name).write_text(text)
        return self.commit(f"change {name}")

    def write_stamped(self, name, record, produced_at):
        """A copy of `record` under another directory name with another produced_at (a tie fixture)."""
        copy = dict(record, produced_at=produced_at)
        path = self.root / BASE / name / "fingerprint.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(copy))
        return copy


class CurrentFingerprint(Recorded):
    def setUp(self):
        super().setUp()
        self.release(entry("one", "pass"), entry("two", "pass", track="other"))

    def test_none_when_no_record_is_tracked(self):
        self.assertIsNone(fp.current_fingerprint(self.root))

    def test_an_untracked_record_is_not_a_fingerprint(self):
        self.run_ok()  # written but not committed
        self.assertIsNone(fp.current_fingerprint(self.root))

    def test_the_nearest_ancestor_wins_and_the_ref_picks_what_is_visible(self):
        first = self.record_here()
        first_ref = head(self.root)
        self.change()
        second = self.record_here()
        self.change("b.txt", "b\n")
        self.assertEqual(fp.current_fingerprint(self.root)["head"], second["head"])
        self.assertEqual(fp.current_fingerprint(self.root, "HEAD")["head12"], second["head12"])
        self.assertEqual(fp.current_fingerprint(self.root, first_ref)["head"], first["head"])
        self.assertEqual(fp.current_fingerprint(self.root, "HEAD~3")["head"], first["head"])

    def test_a_record_ahead_of_the_ref_is_not_visible_at_it(self):
        before = head(self.root)
        self.record_here()
        self.assertIsNone(fp.current_fingerprint(self.root, before))

    def test_a_record_on_another_branch_is_not_an_ancestor(self):
        sh(self.root, "git", "checkout", "-q", "-b", "side")
        self.record_here()
        sh(self.root, "git", "checkout", "-q", "main")
        self.change()
        self.assertIsNone(fp.current_fingerprint(self.root))

    def test_a_tie_in_distance_goes_to_the_latest_produced_at(self):
        record = self.record_here()
        self.write_stamped("older0000000", record, "2020-01-01T00:00:00Z")
        self.write_stamped("newer0000000", record, "2999-01-01T00:00:00Z")
        self.commit("copies")
        self.assertEqual(fp.current_fingerprint(self.root)["produced_at"], "2999-01-01T00:00:00Z")

    def test_a_tie_does_not_depend_on_the_directory_order(self):
        record = self.record_here()
        self.write_stamped("aaaa00000000", record, "2999-01-01T00:00:00Z")
        self.write_stamped("zzzz00000000", record, "2020-01-01T00:00:00Z")
        self.commit("copies")
        self.assertEqual(fp.current_fingerprint(self.root)["produced_at"], "2999-01-01T00:00:00Z")

    def test_nearer_beats_later_produced_at(self):
        old = self.record_here()
        self.change()
        new = self.record_here()
        # the older record claims a far later time, but the newer one is nearer
        self.write_stamped("old000000000", old, "2999-01-01T00:00:00Z")
        self.commit("stamp")
        self.assertEqual(fp.current_fingerprint(self.root)["head"], new["head"])

    def test_a_file_that_is_not_a_record_is_skipped(self):
        record = self.record_here()
        junk = self.root / BASE / "junk00000000"
        junk.mkdir()
        (junk / "fingerprint.json").write_text("{not json")
        self.commit("junk")
        self.assertEqual(fp.current_fingerprint(self.root)["head"], record["head"])


class Show(Recorded):
    def show(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pk.main(["fingerprint", "show", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_without_a_record_it_says_none_and_exits_zero(self):
        self.release(entry("one", "pass"))
        self.assertEqual(self.show(), (0, "fingerprint: none\n", ""))
        code, out, _ = self.show("--json")
        self.assertEqual((code, json.loads(out)), (0, {"head12": None}))

    def test_every_field(self):
        self.release(entry("good", "pass"), entry("bad", "raise SystemExit(3)", track="other"),
                     entry("fine", "pass", track="other"))
        code, _, _, record = self.run_ok()
        self.assertEqual(code, 1)
        self.commit("record")
        self.change("b.txt", "b\n")
        code, out, err = self.show("--json")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["head12"], record["head12"])
        self.assertEqual(data["produced_at"], record["produced_at"])
        self.assertEqual((data["complete"], data["all_as_expected"]), (True, False))
        self.assertEqual(data["commits_behind"], 2)  # the record commit and the change
        names = set(sh(self.root, "git", "diff", "--name-only", record["head"], "HEAD").split())
        self.assertEqual(data["paths_changed"], len(names))
        self.assertGreaterEqual(len(names), 2)
        self.assertEqual(data["tracks"], {name: {"seconds": row["seconds"], "commands": len(row["commands"])}
                                          for name, row in record["tracks"].items()})
        self.assertEqual(data["not_as_expected"], ["bad"])
        self.assertEqual(data["table_conflicts"], [])
        code, out, _ = self.show()
        self.assertEqual(code, 0)
        self.assertIn(f"fingerprint {record['head12']}", out)
        self.assertIn("2 commits behind", out)
        self.assertIn("not as expected: bad", out)

    def test_table_conflicts_are_listed(self):
        self.release(entry("one", "pass"))
        record = self.record_here()
        conflict = {"table": "unittest", "name": "m.C.t", "verdicts": ["fail", "ok"]}
        path = self.root / BASE / record["head12"] / "fingerprint.json"
        doctored = dict(record, table_conflicts=[conflict], complete=False)
        self.write_stamped("other0000000", doctored, "2999-01-01T00:00:00Z")
        self.commit("conflict")
        data = json.loads(self.show("--json")[1])
        self.assertEqual(data["table_conflicts"], [conflict])
        self.assertIn("conflict", self.show()[1])
        self.assertTrue(path.exists())


def command(argv, expected_exit=0, as_expected=True, env_keys=(), ident="x"):
    return {"id": ident, "argv": list(argv), "expected_exit": expected_exit, "as_expected": as_expected,
            "env_keys": list(env_keys)}


FP = {"head12": "abc123abc123", "complete": True, "commands": [
    command(["make", "lint"], ident="lint"),
    command(["make", "fail-on-purpose"], expected_exit=1, ident="neg"),
    command(["make", "broken"], as_expected=False, ident="broken"),
    command(["tool"], env_keys=["MODE"], ident="envy"),
]}
UNITTEST = {"": "ok", "m.C.t1": "ok", "m.C.t2": "skipped", "m.Cx.t9": "fail", "m.D.t3": "skipped",
            "n.E.t4": "fail", "n.E.t5": "ok"}
GO = {"example.com/x/pkg": {"": "pass", "TestA": "pass", "TestB": "skip", "TestA/sub": "pass"},
      "example.com/x/pkg/deep": {"": "pass", "TestZ": "pass"},
      "example.com/x/other": {"": "skip", "TestS": "skip"},
      "example.com/x/bad": {"": "fail", "TestF": "fail"}}
TABLES = {"unittest": UNITTEST, "go-tests": GO}
MODULE = "example.com/x"


def classify(argv, expected_exit=0, record=FP, tables=TABLES, **extra):
    row = {"label": "r", "argv": list(argv), "expected_exit": expected_exit, **extra}
    return fp.classify_row(row, record, tables, MODULE)


class Classify(unittest.TestCase):
    def check(self, expected, *args, **kwargs):
        self.assertEqual(classify(*args, **kwargs), expected, args)

    def test_exact(self):
        self.check(("exact", None), ["make", "lint"])

    def test_exact_with_an_expected_non_zero_exit_that_the_entry_shares(self):
        self.check(("exact", None), ["make", "fail-on-purpose"], 1)

    def test_an_entry_that_was_not_as_expected_covers_nothing(self):
        self.check(("not_covered", ADHOC), ["make", "broken"])

    def test_the_same_argv_with_another_expected_exit_is_not_exact(self):
        self.check(("not_covered", NONZERO), ["make", "lint"], 1)
        self.check(("not_covered", ADHOC), ["make", "fail-on-purpose"], 0)

    def test_no_fingerprint_and_an_incomplete_one_cover_nothing_even_an_exact_argv(self):
        self.assertEqual(fp.classify_row({"argv": ["make", "lint"], "expected_exit": 0}, None, {}),
                         ("not_covered", NO_FINGERPRINT))
        self.check(("not_covered", INCOMPLETE), ["make", "lint"], record=dict(FP, complete=False))

    def test_an_env_override_the_entry_does_not_set(self):
        self.check(("not_covered", ENV), ["make", "lint"], env_keys=["OTHER"])
        self.check(("not_covered", ENV), ["tool"], env_keys=["MODE", "OTHER"])
        self.check(("exact", None), ["tool"], env_keys=["MODE"])
        self.check(("exact", None), ["make", "lint"], env_keys=[])

    def test_an_env_override_on_a_derived_row(self):
        self.check(("not_covered", ENV), ["python3", "-m", "unittest", "m.C.t1"], env_keys=["X"])
        self.check(("not_covered", ENV), ["go", "test", "./pkg"], env_keys=["X"])

    def test_an_ad_hoc_command(self):
        self.check(("not_covered", ADHOC), ["make", "something-else"])
        self.check(("not_covered", ADHOC), ["python3", "-m", "pytest"])

    def test_unittest_by_test_class_or_module_name(self):
        base = ["python3", "-m", "unittest"]
        self.check(("derived-unittest", None), [*base, "m.C.t1"])
        self.check(("derived-unittest", None), [*base, "-v", "m.C.t1", "n.E.t5"])
        self.check(("derived-unittest", None), [*base, "m.C"])  # ok and skipped, at least one ok

    def test_unittest_name_matching_is_by_dotted_segment(self):
        # m.Cx.t9 fails; naming m.C must not pull it in
        self.check(("derived-unittest", None), ["python3", "-m", "unittest", "m.C"])
        self.check(("not_covered", ABSENT), ["python3", "-m", "unittest", "m.Cx.t"])

    def test_unittest_refusals(self):
        base = ["python3", "-m", "unittest"]
        self.check(("not_covered", SKIPPED), [*base, "m.D"])
        self.check(("not_covered", ADHOC), [*base, "n.E"])  # one test failed
        self.check(("not_covered", ADHOC), [*base, "n.E.t4"])
        self.check(("not_covered", ABSENT), [*base, "m.Nope"])
        self.check(("not_covered", ABSENT), [*base, "m.C.t1", "m.Nope"])
        self.check(("not_covered", ADHOC), [*base, "discover"])
        self.check(("not_covered", ADHOC), [*base])
        self.check(("not_covered", ADHOC), [*base, "-k", "t1", "m.C.t1"])
        self.check(("not_covered", ADHOC), ["python", "-m", "unittest", "m.C.t1"])

    def test_a_missing_table_covers_nothing(self):
        self.check(("not_covered", ABSENT), ["python3", "-m", "unittest", "m.C.t1"], tables={})

    def test_go_by_package(self):
        self.check(("derived-go", None), ["go", "test", "./pkg"])
        self.check(("derived-go", None), ["go", "test", "-count=1", "-v", "-timeout", "5m", "./pkg"])
        self.check(("derived-go", None), ["go", "test", "-timeout=5m", "./pkg/..."])
        self.check(("derived-go", None), ["go", "test", "./pkg", "./pkg/deep"])
        self.check(("derived-go", None), ["go", "test", "./pkg/..."])
        self.check(("derived-go", None), ["go", "test", "example.com/x/pkg"])

    def test_go_package_refusals(self):
        self.check(("not_covered", SKIPPED), ["go", "test", "./other"])
        self.check(("not_covered", ABSENT), ["go", "test", "./nope"])
        self.check(("not_covered", ABSENT), ["go", "test", "./pkg", "./nope"])
        self.check(("not_covered", ADHOC), ["go", "test", "./bad"])
        self.check(("not_covered", ADHOC), ["go", "test", "./..."])  # names every package, one of which failed
        self.check(("derived-go", None), ["go", "test", "./..."], tables={"go-tests": {k: v for k, v in GO.items()
                                                                                       if "bad" not in k}})
        self.check(("not_covered", ADHOC), ["go", "test"])
        self.check(("not_covered", ADHOC), ["go", "build", "./pkg"])

    def test_go_package_names_are_prefixes_by_path_segment(self):
        self.check(("not_covered", ABSENT), ["go", "test", "./pk"])  # pkg is not pk

    def test_go_run(self):
        self.check(("derived-go", None), ["go", "test", "-run", "TestA", "./pkg"])
        self.check(("derived-go", None), ["go", "test", "-run=TestA$", "./pkg"])
        self.check(("derived-go", None), ["go", "test", "-run", "TestA/sub", "./pkg"])
        self.check(("not_covered", SKIPPED), ["go", "test", "-run", "TestB", "./pkg"])
        self.check(("not_covered", ABSENT), ["go", "test", "-run", "TestNothing", "./pkg"])
        self.check(("not_covered", ADHOC), ["go", "test", "-run", "(", "./pkg"])

    def test_go_build_changing_flags(self):
        for flag in (["-tags", "integration"], ["-tags=integration"], ["-race"], ["-cover"], ["-coverprofile=c.out"],
                     ["-covermode=atomic"], ["-ldflags=-s"], ["-gcflags", "-N"]):
            self.check(("not_covered", BUILD), ["go", "test", *flag, "./pkg"])

    def test_go_other_flags_are_ad_hoc(self):
        self.check(("not_covered", ADHOC), ["go", "test", "-bench", ".", "./pkg"])
        self.check(("not_covered", ADHOC), ["go", "test", "-count=2", "./pkg"])

    def test_expected_non_zero_exit_on_a_derived_form(self):
        self.check(("not_covered", NONZERO), ["python3", "-m", "unittest", "m.C.t1"], 1)
        self.check(("not_covered", NONZERO), ["go", "test", "./pkg"], 2)

    def test_a_row_without_expected_exit_means_zero(self):
        self.assertEqual(fp.classify_row({"argv": ["make", "lint"]}, FP, TABLES), ("exact", None))

    def test_the_classifier_does_not_change_its_inputs(self):
        import copy
        before = copy.deepcopy((FP, TABLES))
        classify(["go", "test", "-run", "TestA", "./pkg"])
        classify(["python3", "-m", "unittest", "m.C"])
        self.assertEqual((FP, TABLES), before)


class CoverageCommand(Recorded):
    def setUp(self):
        super().setUp()
        self.release(
            entry("lint", "print('lint')"),
            {"id": "unit", "argv": [PY, "-c", "print('x')"], "track": "plain", "kind": "test"})
        self.argv = [PY, "-c", "print('lint')"]

    def results(self, task, rows):
        store.record(self.root, task, "results", {"task_id": task, "inputs": {"a.txt": "0" * 64}, "commands": rows})

    def cover(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pk.main(["fingerprint", "coverage", *argv])
        return code, out.getvalue(), err.getvalue()

    def rows(self):
        return [{"label": "lint", "argv": self.argv, "expected_exit": 0},
                {"label": "adhoc", "argv": ["make", "x"], "expected_exit": 0}]

    def test_without_a_fingerprint_every_row_says_so(self):
        self.results("T-1", self.rows())
        self.commit("results")
        code, out, _ = self.cover("--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIsNone(data["head12"])
        self.assertEqual([r["reason"] for r in data["tasks"]["T-1"]["rows"]], [NO_FINGERPRINT] * 2)

    def test_per_task_counts_and_reasons_over_a_real_record(self):
        self.results("T-1", self.rows())
        self.results("T-2", [{"label": "lint", "argv": self.argv, "expected_exit": 0}])
        self.commit("results")
        record = self.record_here()
        code, out, err = self.cover()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("T-1: covered 1 of 2 rows", out)
        self.assertIn("T-2: covered 1 of 1 rows", out)
        self.assertIn(f"adhoc: {ADHOC}", out)
        data = json.loads(self.cover("--json")[1])
        self.assertEqual(data["head12"], record["head12"])
        self.assertEqual(data["tasks"]["T-1"]["covered"], 1)
        self.assertEqual(data["tasks"]["T-1"]["total"], 2)
        self.assertEqual(data["tasks"]["T-1"]["rows"][0]["class"], "exact")

    def test_one_task_by_id_and_an_unknown_id(self):
        self.results("T-1", self.rows())
        self.results("T-2", self.rows())
        self.commit("results")
        self.record_here()
        data = json.loads(self.cover("T-2", "--json")[1])
        self.assertEqual(list(data["tasks"]), ["T-2"])
        code, _, err = self.cover("T-9")
        self.assertEqual(code, 1)
        self.assertIn("T-9", err)

    def test_an_incomplete_fingerprint_covers_nothing(self):
        self.results("T-1", self.rows())
        self.commit("results")
        code, _, _, record = self.run_ok("--only", "plain")
        self.assertFalse(record["complete"])
        self.commit("partial record")
        rows = json.loads(self.cover("--json")[1])["tasks"]["T-1"]["rows"]
        self.assertEqual([r["reason"] for r in rows], [INCOMPLETE] * 2)

    def test_coverage_writes_nothing(self):
        self.results("T-1", self.rows())
        self.commit("results")
        self.record_here()
        before = sh(self.root, "git", "status", "--porcelain")
        self.cover()
        self.cover("--json")
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), before)

    def test_unittest_rows_are_read_from_the_committed_table(self):
        source = ("import unittest\n\nclass C(unittest.TestCase):\n    def test_a(self):\n        pass\n"
                  "    def test_b(self):\n        self.skipTest('x')\n")
        (self.root / "tmod.py").write_text(source)
        self.release(entry("lint", "pass"),
                     {"id": "ut", "argv": [PY, "-m", "unittest", "-v", "tmod"], "track": "plain", "kind": "test",
                      "derives": "unittest"})
        self.results("T-1", [{"label": "one", "argv": ["python3", "-m", "unittest", "tmod.C.test_a"],
                              "expected_exit": 0},
                             {"label": "skipped", "argv": ["python3", "-m", "unittest", "tmod.C.test_b"],
                              "expected_exit": 0}])
        self.commit("results")
        self.record_here()
        rows = json.loads(self.cover("--json")[1])["tasks"]["T-1"]["rows"]
        self.assertEqual([(r["class"], r["reason"]) for r in rows],
                         [("derived-unittest", None), ("not_covered", SKIPPED)])


class StaleField(Recorded):
    def setUp(self):
        super().setUp()
        self.release(entry("lint", "pass"))

    def test_the_field_is_null_without_a_fingerprint_and_counts_with_one(self):
        argv = [PY, "-c", "pass"]
        store.record(self.root, "T-1", "results", {"task_id": "T-1", "inputs": {"a.txt": "0" * 64},
                                                   "commands": [{"label": "l", "argv": argv, "expected_exit": 0},
                                                                {"label": "m", "argv": ["x"], "expected_exit": 0}]})
        self.commit("results")
        self.assertIsNone(stale.drift_report(self.root, "HEAD")["tasks"][0]["fingerprint"])
        record = self.record_here()
        row = stale.drift_report(self.root, "HEAD")["tasks"][0]
        self.assertEqual(row["fingerprint"], {"head12": record["head12"], "covered_rows": 1, "total_rows": 2})


class HandoffLine(Recorded):
    def setUp(self):
        super().setUp()
        (self.root / "program").mkdir()
        (self.root / "program" / "HANDOFF.md").write_text("# h\n\n<!-- generated:start -->\n<!-- generated:end -->\n")
        self.release(entry("one", "pass"), entry("two", "raise SystemExit(1)", track="other"))

    def now(self):
        text = handoff.render(self.root)
        return text.split("#### Now\n", 1)[1].split("\n\n", 1)[0].splitlines()

    def test_none_without_a_record(self):
        self.assertEqual(self.now(), ["- Fingerprint: none"])

    def test_the_line_with_a_record_and_the_distance(self):
        record = self.record_here()
        self.change()
        short = record["head12"]
        self.assertEqual(self.now(), [f"- Fingerprint: {short}, complete, 1 of 2 as expected, 2 commits behind"])

    def test_an_incomplete_record_says_incomplete(self):
        self.run_ok("--only", "plain")
        self.commit("partial")
        self.assertIn(", incomplete, 1 of 1 as expected, 1 commits behind", self.now()[0])

    def test_it_sits_after_the_controller_line_and_before_the_task_lines(self):
        (self.root / "program" / "controller-state.json").write_text(json.dumps(
            {"now": "the controller line", "stage": {}, "queued": {}, "parked": {}, "followups": []}))
        (self.root / "program" / "status").mkdir()
        (self.root / "program" / "status" / "T-1.json").write_text(json.dumps(
            {"task_id": "T-1", "status": "in_progress", "role": "controller", "updated_at": "", "note": "n",
             "evidence": [], "blocked_by": [], "history": []}))
        self.commit("state")
        self.record_here()
        lines = self.now()
        self.assertEqual(lines[0], "- the controller line")
        self.assertTrue(lines[1].startswith("- Fingerprint: "), lines)
        self.assertIn("T-1", lines[2])

    def test_a_written_hand_off_is_current_and_stays_current_after_it_is_committed(self):
        self.record_here()
        self.assertTrue(handoff.update(self.root))
        self.assertTrue(handoff.check(self.root))
        self.assertIn("Fingerprint: ", (self.root / "program" / "HANDOFF.md").read_text())
        self.commit("hand-off")  # HEAD moves: the commits-behind count with it
        self.assertTrue(handoff.check(self.root))
        self.assertIs(handoff.update(self.root), False)

    def test_a_change_to_the_verdict_still_makes_the_hand_off_stale(self):
        self.record_here()
        handoff.update(self.root)
        path = self.root / "program" / "HANDOFF.md"
        path.write_text(path.read_text().replace("1 of 2 as expected", "2 of 2 as expected"))
        self.assertFalse(handoff.check(self.root))


if __name__ == "__main__":
    unittest.main()
