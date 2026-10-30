"""Planted-bug chunks as kit records (0.5.3): deferred rows in the gate record, `pk gate plants` and its `plants`
records, coverage in acceptance and merge, the early late-work check, reuse, and the gate's time estimate."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import evidence
import gate
import latework
import pk
import plants
import status
import store
from common import KitError, read_json
from test_gate import BRANCH, PRODUCE, PY, TASK, commit, put, run_quiet, sh
from test_land import LandBase
from test_parallel_plants import BUILD, PY as PLANT_PY, Base as PlantBase
from test_tiers import FULL_PATHS

# The fixture producer with a second row, deferred: `add-times` is killed by the add test. With PK_DEFER_SURVIVOR a
# third deferred row `sub-plus` survives (no test checks sub). With PK_NO_WRITE the producer appends nothing.
PRODUCE_DEFER = PRODUCE.replace(
    '''ev.planted_bugs([bug], build=''',
    '''bugs = [bug, {"id": "add-times", "path": "calc.py", "find": "return a + b", "replace": "return a * b",
               "test_args": ["test_calc"], "defer": True}]
if os.environ.get("PK_DEFER_SURVIVOR"):
    bugs.append({"id": "sub-plus", "path": "calc.py", "find": "return a - b", "replace": "return a + b",
                 "test_args": ["test_calc"], "defer": True})
if os.environ.get("PK_NO_WRITE"):
    raise SystemExit(0)
ev.planted_bugs(bugs, build=''')
assert PRODUCE_DEFER != PRODUCE
SIDE_FILE = f"program/evidence/{TASK}/gap-plants-extra.json"
COMMAND = f"pk gate plants {TASK} --branch {BRANCH} --all-pending"


def minimal_record(rows, head="a" * 40, **over):
    digest = "0" * 64
    data = {"task_id": TASK, "head": head, "main": head, "merge_base": head, "produced_at": "2026-10-31T00:00:00Z",
            "inputs": [{"path": "calc.py", "sha256": digest}],
            "commands": [{"label": "unit", "argv": ["make"], "exit": 0, "expected_exit": 0, "as_expected": True,
                          "derived": {}, "output_sha256": digest, "output_tail": ""}],
            "planted_bugs": rows, "planted_bugs_all_killed": True, "all_commands_as_expected": True,
            "tree_unchanged_by_run": True, "blocked_by": [], "deviations": [], "supersedes": None}
    data.update(over)
    return data


class DeferredRowsInTheRunner(PlantBase):
    def test_a_deferred_row_is_recorded_with_its_definition_and_never_run(self):
        for workers in (1, 2):
            (self.shared / "log").write_text("")
            table = [self.row("judged"), self.row("later", replace="return a - b", defer=True)]
            rows, _ = self.plants(table, workers)
            self.assertEqual([row["id"] for row in rows], ["judged", "later", evidence.SELF_TEST_ID])
            later = rows[1]
            self.assertEqual(set(later), set(evidence.DEFERRED_KEYS), workers)
            self.assertEqual((later["deferred"], later["serial"], later["path"], later["replace"]),
                             (True, False, "calc.py", "return a - b"))
            self.assertEqual((later["build_argv"], later["test_argv"]),
                             (BUILD, [PLANT_PY, str(self.judge), "plain", "later", str(self.shared)]))
            self.assertEqual(later["definition_sha256"], evidence.definition_digest(later))
            self.assertNotIn("later", {line[1] for line in self.log()}, workers)  # never judged here
            self.assertTrue(rows[0]["killed"])

    def test_the_record_counts_only_judged_rows_and_times_them(self):
        ev = evidence.Evidence(self.root, "T-1")
        ev.hash_inputs(["calc.py"])
        ev.run_command("unit", [PLANT_PY, "-c", "pass"])
        survivor = self.row("survives", find="return a - b", replace="return a - b + 0", defer=True)
        with contextlib.redirect_stdout(io.StringIO()):
            ev.planted_bugs([self.row("judged"), survivor], BUILD, [PLANT_PY, str(self.judge)], workers=1)
        data = store.load(self.root, ev.write()["path"])
        self.assertIs(data["planted_bugs_all_killed"], True)  # the deferred row is not counted here
        self.assertEqual(evidence.validate_results(data), [])
        self.assertEqual((data["planted_bugs_timing"]["rows"], data["planted_bugs_timing"]["workers"]), (2, 1))

    def test_defer_must_be_true_or_false(self):
        with self.assertRaises(KitError) as ctx:
            self.plants([self.row("x", defer="yes")], 1)
        self.assertIn("defer must be true or false", str(ctx.exception))

    def test_every_row_may_be_deferred_and_the_self_test_still_runs(self):
        rows, _ = self.plants([self.row("a", defer=True), self.row("b", replace="return 0", defer=True)], 2)
        self.assertEqual([row.get("deferred") for row in rows], [True, True, None])
        self.assertEqual(rows[-1]["kill_reason"], "build failed")


class Validation(unittest.TestCase):
    def deferred(self, **over):
        row = {"id": "later", "deferred": True, "path": "calc.py", "find": "a + b", "replace": "a - b",
               "build_argv": ["make"], "test_argv": ["make", "test"], "serial": False}
        row["definition_sha256"] = evidence.definition_digest(row)
        row.update(over)
        return row

    def self_test(self):
        return {"id": evidence.SELF_TEST_ID, "builds": False, "killed": False, "kill_reason": "build failed",
                "test_output_sha256": "", "path": "calc.py", "find": "", "replace": "def (:",
                "build_argv": ["make"], "test_argv": ["make", "test"]}

    def test_a_record_with_deferred_rows_validates_and_passes_the_gate_checks(self):
        data = minimal_record([self.deferred(), self.self_test()])
        self.assertEqual(evidence.validate_results(data), [])
        self.assertEqual(evidence.gate_problems(data, TASK), [])

    def test_a_deferred_row_with_a_verdict_or_another_definition_is_refused(self):
        for row, words in ((self.deferred(builds=True), "no verdict"),
                           (self.deferred(find="a * b"), "definition_sha256 does not match"),
                           (self.deferred(serial="no"), "serial")):
            problems = evidence.validate_results(minimal_record([row, self.self_test()]))
            self.assertTrue(any(words in problem for problem in problems), (words, problems))

    def test_the_optional_fields_are_checked_when_present(self):
        bad = minimal_record([self.deferred(), self.self_test()], planted_bugs_timing={"seconds": -1})
        self.assertTrue(any("planted_bugs_timing" in p for p in evidence.validate_results(bad)))
        bad = minimal_record([self.deferred(), self.self_test()], plants_reused_from=[{"path": "x", "digest": "0"}])
        self.assertTrue(any("plants_reused_from" in p for p in evidence.validate_results(bad)))

    def plants_record(self, **over):
        row = {**{k: v for k, v in self.deferred().items() if k != "deferred"}, "builds": True, "killed": True,
               "kill_reason": "test failed", "test_output_sha256": "0" * 64}
        data = {"task_id": TASK, "kind": "plants",
                "part_of": {"path": f"program/evidence/{TASK}/results/20261031T000000000000Z-{'1' * 12}.json",
                            "digest": "1" * 64},
                "head": "a" * 40, "kit_version": "0.5.3", "produced_at": "2026-10-31T00:00:00Z", "workers": 2,
                "seconds": 1.5, "rejudge": False, "rows": [row], "all_killed": True}
        data.update(over)
        return data

    def test_a_plants_record_validates_and_its_invariants_hold(self):
        self.assertEqual(evidence.validate_plants(self.plants_record()), [])
        self.assertIn("all_killed: does not match the rows", evidence.validate_plants(self.plants_record(all_killed=False)))
        self.assertTrue(evidence.validate_plants(self.plants_record(part_of={"path": "x", "digest": "1" * 64})))
        row = dict(self.plants_record()["rows"][0], replace="a * b")
        self.assertTrue(any("definition_sha256" in p for p in evidence.validate_plants(self.plants_record(rows=[row]))))


class Schemas(unittest.TestCase):
    def test_the_schemas_list_the_codes_own_keys(self):
        templates = Path(evidence.TEMPLATES)
        self.assertEqual(read_json(templates / "plants.schema.json")["required"], list(evidence.PLANTS_KEYS))
        deferred = read_json(templates / "results.schema.json")["definitions"]["deferred_row"]
        self.assertEqual(deferred["required"], list(evidence.DEFERRED_KEYS))
        self.assertEqual(sorted(read_json(templates / "plants.schema.json")["properties"]["rows"]["items"]["required"]),
                         sorted([*evidence.ROW_KEYS, *evidence.PLANTED_KEYS, "serial", "definition_sha256"]))


class Store(PlantBase):
    def results(self):
        ev = evidence.Evidence(self.root, TASK)
        ev.hash_inputs(["calc.py"])
        ev.run_command("unit", [PLANT_PY, "-c", "pass"])
        with contextlib.redirect_stdout(io.StringIO()):
            ev.planted_bugs([self.row("judged"), self.row("later", replace="return a - b", defer=True)], BUILD,
                            [PLANT_PY, str(self.judge)], workers=1)
        return ev.write()

    def chunk(self, written):
        data = store.load(self.root, written["path"])
        row = {k: v for k, v in data["planted_bugs"][1].items() if k != "deferred"}
        record = {"task_id": TASK, "kind": "plants", "part_of": {"path": written["path"], "digest": written["digest"]},
                  "head": data["head"], "kit_version": "0.5.3", "produced_at": "2026-10-31T00:00:00Z", "workers": 1,
                  "seconds": 1.0, "rejudge": False, "all_killed": True,
                  "rows": [{**row, "builds": True, "killed": True, "kill_reason": "test failed",
                            "test_output_sha256": "0" * 64}]}
        return store.record(self.root, TASK, "plants", record)

    def test_the_pointer_lists_plants_records_and_a_results_append_keeps_the_list(self):
        written = self.results()
        before = json.loads((self.root / store.pointer_path(TASK)).read_text())
        self.assertNotIn("plants", before)  # a pointer without chunks is the 0.5.2 pointer
        chunk = self.chunk(written)
        listed = store.read_plants(self.root, TASK)
        self.assertEqual([(item["path"], item["part_of"]) for item in listed], [(chunk["path"], written["digest"])])
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "evidence")
        self.assertEqual(store.integrity(self.root), [])
        entry, data = store.load_latest(self.root, TASK, "results")
        self.assertIsNone(evidence.coverage_problem(self.root, TASK, entry, data))
        store.set_entry(self.root, TASK, "results", entry)
        self.assertEqual(store.read_plants(self.root, TASK), listed)

    def test_an_unlisted_or_altered_plants_record_fails_integrity(self):
        written = self.results()
        chunk = self.chunk(written)
        pointer = self.root / store.pointer_path(TASK)
        data = json.loads(pointer.read_text())
        data.pop("plants")
        pointer.write_text(json.dumps(data))
        self.assertTrue(any("is not listed" in p for p in store.integrity(self.root)))
        entry, record = store.load_latest(self.root, TASK, "results")
        problem = evidence.coverage_problem(self.root, TASK, entry, record)
        self.assertIn("not judged yet: later", problem)
        self.assertIn(f"pk gate plants {TASK} --branch task/{TASK} --all-pending", problem)
        os.chmod(chunk["file"], 0o644)
        chunk["file"].write_text("{}\n")
        self.assertTrue(any("immutable record altered" in p for p in store.integrity(self.root)))

    def test_a_plants_record_path_is_a_record_path(self):
        self.assertEqual(store.check_rel(f"program/evidence/{TASK}/plants/20261031T000000000000Z-{'a' * 12}.json"),
                         (TASK, "plants", f"20261031T000000000000Z-{'a' * 12}.json"))
        self.assertTrue(latework.own_path(f"program/evidence/{TASK}/plants/20261031T000000000000Z-{'a' * 12}.json",
                                          TASK))
        self.assertFalse(latework.own_path(SIDE_FILE, TASK))


class ChunkBase(LandBase):
    def setUp(self):
        super().setUp()
        for var in ("PK_DEFER_SURVIVOR", "PK_NO_WRITE"):
            os.environ.pop(var, None)
        self.standard(done=False)
        put(self.wt, f"program/evidence/{TASK}/produce.py", PRODUCE_DEFER)
        commit(self.wt, "a producer that defers a row")

    def gate_record(self):
        """The hand-back gate and the evidence commit; (gate output, results entry, record)."""
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        if status.read(self.wt, TASK)["status"] == "in_progress":
            status.transition(self.wt, TASK, "review", "implementer")
        commit(self.wt, "evidence")
        entry, data = store.load_latest(self.wt, TASK, "results")
        return out, entry, data

    def gate_plants(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", "plants", TASK, "--branch", BRANCH, *argv])
        return rc, out.getvalue(), err.getvalue()

    def acceptance(self, entry, data):
        return {"task_id": TASK, "decision": "accepted_development", "evidence_sha256": entry["digest"],
                "source_commit": data["head"], "accepted_at": "2026-10-31T00:00:00Z", "role": "qa",
                "commands": [], "planted_bugs": [], "findings": [], "blocked_by": []}


class ChunkFlow(ChunkBase):
    def test_deferred_rows_are_judged_in_a_plants_record_that_acceptance_and_merge_require(self):
        out, entry, data = self.gate_record()
        lines = out.splitlines()
        self.assertEqual(lines[-3:-1], [
            f"deferred: 1 planted-bug row(s) of {entry['path']} are deferred and not judged by this gate (1 pending); "
            "QA's acceptance and the merge need each one killed in a plants record",
            f"deferred: judge them with: {COMMAND}"])
        self.assertTrue(lines[-1].startswith("OK: "))
        deferred = [row for row in data["planted_bugs"] if row.get("deferred")]
        self.assertEqual([row["id"] for row in deferred], ["add-times"])
        with self.assertRaises(KitError) as ctx:  # QA cannot accept a record whose deferred rows nobody judged
            evidence.accept(self.wt, TASK, self.acceptance(entry, data))
        self.assertIn("1 of 1 deferred planted-bug rows", str(ctx.exception))
        self.assertIn("not judged yet: add-times", str(ctx.exception))
        self.assertIn(COMMAND, str(ctx.exception))
        rc, said, err = self.gate_plants("--all-pending", "--workers", "2")
        self.assertEqual((rc, err), (0, ""), said)
        self.assertIn("plants: 1 of 1 killed", said)
        self.assertIn(f"plants: every deferred row of {entry['path']} is killed; QA can accept the record", said)
        [item] = store.read_plants(self.wt, TASK)
        chunk = store.load(self.wt, item["path"], item["digest"])
        self.assertEqual(chunk["part_of"], {"path": entry["path"], "digest": entry["digest"]})
        self.assertEqual((chunk["head"], chunk["kit_version"], chunk["workers"]),
                         (data["head"], plants.kit_version(), 2))
        [row] = chunk["rows"]
        self.assertEqual((row["id"], row["killed"], row["kill_reason"], row["definition_sha256"]),
                         ("add-times", True, "test failed", deferred[0]["definition_sha256"]))
        self.assertEqual(evidence.validate_plants(chunk), [])
        commit(self.wt, "plants chunk")  # after the reviewed commit: merge accepts records in plants/
        rc, said, err = self.gate_plants("--rows", "add-times")
        self.assertEqual(rc, 1)
        self.assertIn("already judged killed", err)
        self.qa_accepts(self.wt, TASK)
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(store.integrity(self.root, schema=True), [])


@skip_unless_slow()
class ChunkRefusals(ChunkBase):
    def test_a_surviving_deferred_row_is_named_with_its_verdict(self):
        os.environ["PK_DEFER_SURVIVOR"] = "1"
        _, entry, data = self.gate_record()
        rc, said, _ = self.gate_plants("--all-pending")
        self.assertEqual(rc, 1, said)
        self.assertIn("plants: sub-plus not killed", said)
        with self.assertRaises(KitError) as ctx:
            evidence.accept(self.wt, TASK, self.acceptance(entry, data))
        self.assertIn("judged and not killed: sub-plus (not killed)", str(ctx.exception))
        self.assertIn("--rows sub-plus --rejudge", str(ctx.exception))
        os.environ.pop("PK_DEFER_SURVIVOR")

    def test_rows_that_are_not_deferred_or_unknown_are_refused(self):
        self.gate_record()
        for argv, words in ((["--rows", "add-minus"], "not deferred"), (["--rows", "nope"], "no planted-bug row"),
                            ([], "one of the arguments")):
            rc, said, err = self.gate_plants(*argv)
            self.assertNotEqual(rc, 0)
            self.assertIn(words, err + said)
        self.assertEqual(store.read_plants(self.wt, TASK), [])

    def test_rejudge_judges_a_killed_row_again(self):
        self.gate_record()
        self.assertEqual(self.gate_plants("--all-pending")[0], 0)
        rc, said, _ = self.gate_plants("--all-pending")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", said)
        rc, said, err = self.gate_plants("--rows", "add-times", "--rejudge")
        self.assertEqual((rc, err), (0, ""), said)
        self.assertEqual([store.load(self.wt, item["path"])["rejudge"] for item in store.read_plants(self.wt, TASK)],
                         [False, True])

    def test_a_late_plants_record_bound_to_another_record_stops_the_merge(self):
        _, old_entry, old = self.gate_record()
        put(self.wt, "calc.py", (self.wt / "calc.py").read_text() + "\n\ndef neg(a):\n    return -a\n")
        commit(self.wt, "fix round")
        _, entry, data = self.gate_record()
        self.assertEqual(self.gate_plants("--all-pending")[0], 0)
        commit(self.wt, "plants chunk")
        self.qa_accepts(self.wt, TASK)
        [item] = store.read_plants(self.wt, TASK)
        stray = dict(store.load(self.wt, item["path"]), part_of={"path": old_entry["path"],
                                                                  "digest": old_entry["digest"]}, head=old["head"])
        written = store.record(self.wt, TASK, "plants", stray)
        commit(self.wt, "a chunk of the old record")
        code, out, err = self.pk("merge", TASK)
        self.assertEqual(code, 1, out)
        self.assertIn(f"{written['path']} was added after the reviewed commit", err)
        self.assertIn(f"not to the accepted record {entry['path']}", err)

    def test_a_producer_side_file_is_refused_at_acceptance_not_at_the_merge(self):
        _, entry, data = self.gate_record()
        self.assertEqual(self.gate_plants("--all-pending")[0], 0)
        put(self.wt, SIDE_FILE, "[]\n")
        with self.assertRaises(KitError) as ctx:  # uncommitted
            evidence.accept(self.wt, TASK, self.acceptance(entry, data))
        self.assertIn(f"pk evidence accept: {TASK}: {SIDE_FILE} under program/evidence/{TASK}/ is not a file the kit "
                      "writes", str(ctx.exception))
        late = commit(self.wt, "the producer's side file")
        with self.assertRaises(KitError) as ctx:  # committed after the reviewed commit
            evidence.accept(self.wt, TASK, self.acceptance(entry, data))
        message = str(ctx.exception)
        self.assertIn(f"{late[:12]} was committed after the reviewed commit {data['head'][:12]} and changes {SIDE_FILE}",
                      message)
        self.assertIn("records directly in results/, acceptance/ and plants/", message)
        self.assertIn(f"pk gate plants {TASK}", message)

    def test_a_side_file_after_acceptance_is_refused_at_done(self):
        _, entry, data = self.gate_record()
        self.assertEqual(self.gate_plants("--all-pending")[0], 0)
        evidence.accept(self.wt, TASK, self.acceptance(entry, data))
        commit(self.wt, "plants and acceptance")
        put(self.wt, SIDE_FILE, "[]\n")
        commit(self.wt, "the producer's side file")
        with self.assertRaises(KitError) as ctx:
            status.transition(self.wt, TASK, "done", "qa")
        self.assertIn(f"pk status set {TASK} done: {TASK}:", str(ctx.exception))
        self.assertIn(f"changes {SIDE_FILE}", str(ctx.exception))

    def test_the_gate_says_so_when_work_follows_an_accepted_record(self):
        _, entry, data = self.gate_record()
        self.assertEqual(self.gate_plants("--all-pending")[0], 0)
        self.qa_accepts(self.wt, TASK)
        put(self.wt, SIDE_FILE, "[]\n")
        commit(self.wt, "the producer's side file")
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        late = [line for line in out.splitlines() if line.startswith("late work: pk gate handback: ")]
        self.assertTrue(late and SIDE_FILE in late[0] and "QA must accept it again" in late[0], out)

    def test_a_gate_run_that_appends_no_record_over_late_work_says_what_merge_refuses(self):
        self.gate_record()
        put(self.wt, "notes.txt", "not an input of the record\n")
        commit(self.wt, "late work")
        os.environ["PK_NO_WRITE"] = "1"
        try:
            rc, out = self.handback()
        finally:
            os.environ.pop("PK_NO_WRITE")
        self.assertEqual(rc, 0, out)  # the record still holds for every input it binds (0.4 design)
        self.assertIn("late work: program/evidence/", out)
        self.assertIn("appended no results record, and pk merge refuses the tip over the latest one", out)
        self.assertIn("changes notes.txt", out)


@skip_unless_slow()
class ChunkReuse(ChunkBase):
    def test_plants_of_a_reused_record_count_for_the_new_record(self):
        self.write_checks(tiers=FULL_PATHS, evidence={"reuse_unchanged_code": True})
        commit(self.root, "opt in to reuse")
        sh(self.wt, "git", "merge", "-q", "--no-edit", "main")
        _, old_entry, _ = self.gate_record()
        self.assertEqual(self.gate_plants("--all-pending")[0], 0)
        commit(self.wt, "plants chunk")
        put(self.wt, "program/DASHBOARD.md", "# Dashboard\n")
        commit(self.wt, "a program-only change")
        out, entry, data = self.gate_record()
        self.assertEqual(data["reused_from"]["digest"], old_entry["digest"])
        [item] = store.read_plants(self.wt, TASK)
        self.assertEqual(data["plants_reused_from"], [{"path": item["path"], "digest": item["digest"]}])
        self.assertIsNone(evidence.coverage_problem(self.wt, TASK, entry, data))
        self.assertIn("(0 pending)", out)
        self.qa_accepts(self.wt, TASK)
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)


class Estimate(PlantBase):
    def record(self, seconds, rows, workers, judged):
        table = [self.row(f"r{n}") for n in range(judged)]
        planted = [{"id": f"r{n}", "builds": True, "killed": True, "kill_reason": "test failed",
                    "test_output_sha256": "0" * 64, "path": "calc.py", "find": "a", "replace": "b",
                    "build_argv": ["make"], "test_argv": ["make", "test"]} for n in range(len(table))]
        planted.append({"id": evidence.SELF_TEST_ID, "builds": False, "killed": False, "kill_reason": "build failed",
                        "test_output_sha256": "", "path": "calc.py", "find": "", "replace": "def (:",
                        "build_argv": ["make"], "test_argv": ["make", "test"]})
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        data = minimal_record(planted, head=head, planted_bugs_timing={"seconds": seconds, "rows": rows,
                                                                       "workers": workers})
        store.record(self.root, TASK, "results", data)

    def test_the_gate_warns_when_the_judged_rows_may_not_fit_the_evidence_step(self):
        self.record(seconds=3000.0, rows=151, workers=1, judged=150)  # about 20 s per row
        warning = gate.plant_estimate(self.root, TASK, workers=1)
        self.assertIn("evidence estimate: about 3000 s for 151 judged planted-bug rows with 1 worker(s)", warning)
        for words in ("defer: true", "pk gate plants", "evidence.plant_workers", "split the task", "the gate runs on"):
            self.assertIn(words, warning)
        self.assertIsNone(gate.plant_estimate(self.root, TASK, workers=4))  # about 745 s: no warning

    def test_no_estimate_without_a_timed_record(self):
        self.assertIsNone(gate.plant_estimate(self.root, TASK, workers=1))


if __name__ == "__main__":
    unittest.main()
