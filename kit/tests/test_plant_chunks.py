"""Planted-bug chunks as kit records (0.5.3): deferred rows in the gate record, `pk gate plants` and its `plants`
records, coverage in acceptance and merge, the early late-work check, reuse, and the gate's time estimate; and (0.6.0)
chunks timed from earlier records (`--chunk-seconds`), `--plan` and `--chunk K/M` for several machines."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
from _slow import skip_unless_slow
import contextlib
import io
import json
import os
import shutil
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
from common import KitError, read_json, read_yaml, write_yaml
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


class EvidenceLimit(Estimate):
    """KF-23 (0.5.5): the evidence step's limit scales with the rows and the workers, or comes from checks.yaml."""

    def test_the_limit_is_the_3600_s_floor_without_a_record_and_for_a_small_task(self):
        self.assertEqual(gate.evidence_limit(self.root, TASK, workers=1)[0], 3600)
        self.record(seconds=200.0, rows=11, workers=1, judged=10)  # 11 rows of about 18 s: far below the floor
        limit, said = gate.evidence_limit(self.root, TASK, workers=1)
        self.assertEqual(limit, 3600)
        self.assertIn("the floor", said)

    def test_the_limit_scales_with_the_judged_rows_and_the_workers(self):
        # the program's AH-0067: 3 workers stopped at row 43 of 56 under 3600 s, about 251 s per row and worker
        self.record(seconds=3600.0, rows=43, workers=3, judged=55)
        limit, said = gate.evidence_limit(self.root, TASK, workers=3)
        self.assertEqual(limit, 9377)  # 2 x 56 rows x 251.2 s / 3 workers
        for words in ("56 judged planted-bug rows", "3 worker(s)", "measured", "evidence.step_timeout"):
            self.assertIn(words, said)
        self.assertLess(gate.evidence_limit(self.root, TASK, workers=6)[0], limit)  # more workers, less time
        self.assertIsNone(gate.plant_estimate(self.root, TASK, workers=3, limit=limit))  # it fits the scaled limit

    def test_a_record_without_timing_scales_with_the_default_seconds_per_row(self):
        self.record(seconds=1.0, rows=1, workers=1, judged=60)
        data = store.load_latest(self.root, TASK, "results")[1]
        del data["planted_bugs_timing"]
        store.record(self.root, TASK, "results", data)
        limit, said = gate.evidence_limit(self.root, TASK, workers=2)
        self.assertEqual(limit, 2 * 61 * gate.ROW_SECONDS // 2)
        self.assertIn("no timing recorded", said)

    def test_checks_yaml_sets_the_limit_and_never_below_the_floor(self):
        self.record(seconds=3600.0, rows=43, workers=3, judged=55)
        config = evidence.parse_config({"step_timeout": 5400})
        self.assertEqual(gate.evidence_limit(self.root, TASK, config, workers=3),
                         (5400, "evidence limit: 5400 s (evidence.step_timeout in program/checks.yaml)"))
        for bad in (600, "7200", True, 0):
            with self.assertRaisesRegex(KitError, "step_timeout must be a whole number of seconds, at least 3600"):
                evidence.parse_config({"step_timeout": bad})
        self.assertIsNone(evidence.parse_config(None)["step_timeout"])

    def test_the_evidence_step_runs_the_producer_under_the_limit(self):
        seen = []
        real = gate.run_argv
        put(self.root, f"program/evidence/{TASK}/produce.py", "")

        def spy(argv, cwd, env=None, timeout=None):
            seen.append(timeout)
            return real(argv, cwd, env=env, timeout=timeout)

        with mock.patch.object(gate, "run_argv", spy), contextlib.redirect_stdout(io.StringIO()):
            gate._evidence_step(self.root, TASK, limit=7777)
        self.assertEqual(seen[0], 7777)


# --- 0.6.0 (KF-34): deferred rows judged in timed chunks, `--plan` and `--chunk K/M` for several machines ---------

T2 = "P0-M01-S01-T02"
# The fixture producer with more deferred rows, each killed by the add test: add-times, add-more-0, add-more-1 and
# add-more-2 (with PK_DEFER_SURVIVOR, sub-plus last, which survives). With PK_SPLIT_SURVIVOR the surviving sub-plus
# sits third, between add-more-0 and add-more-1.
PRODUCE_MANY = PRODUCE_DEFER.replace('if os.environ.get("PK_DEFER_SURVIVOR"):', '''survivor = {"id": "sub-plus", "path": "calc.py", "find": "return a - b", "replace": "return a + b",
            "test_args": ["test_calc"], "defer": True}
for n, replace in enumerate(["return a + b + 1", "return a", "return b"]):
    if n == 1 and os.environ.get("PK_SPLIT_SURVIVOR"):
        bugs.append(survivor)
    bugs.append({"id": f"add-more-{n}", "path": "calc.py", "find": "return a + b", "replace": replace,
                 "test_args": ["test_calc"], "defer": True})
if os.environ.get("PK_DEFER_SURVIVOR"):''')
assert PRODUCE_MANY != PRODUCE_DEFER
ROWS = ["add-times", "add-more-0", "add-more-1", "add-more-2"]


def rows(count, serial=()):
    return [{"id": f"r{n}", "serial": n in serial} for n in range(count)]


class ChunkEstimate(unittest.TestCase):
    def test_a_chunk_is_its_pool_slots_times_the_seconds_plus_one_slot_per_serial_row(self):
        self.assertEqual(plants.estimate(rows(4, serial=(3,)), 2, 10.0), 30.0)  # ceil(3 / 2) x 10 + 10
        self.assertEqual(plants.estimate(rows(3), 3, 113.0), 113.0)
        self.assertEqual(plants.estimate([], 3, 113.0), 0)

    def test_the_rows_are_split_in_table_order_into_chunks_that_fit(self):
        chunks = plants.split(rows(7), 3, 100.0, 200)
        self.assertEqual([[row["id"] for row in chunk] for chunk in chunks],
                         [["r0", "r1", "r2", "r3", "r4", "r5"], ["r6"]])
        self.assertEqual([len(chunk) for chunk in plants.split(rows(7), 3, 100.0, 50)], [1] * 7)  # one row at least
        serial = plants.split(rows(5, serial=(1, 2)), 2, 10.0, 30)
        self.assertEqual([[row["id"] for row in chunk] for chunk in serial], [["r0", "r1", "r2", "r3"], ["r4"]])

    def test_the_measured_cost_of_the_program_it_came_from_splits_175_rows_into_three_chunks(self):
        # 175 deferred rows took 6,606 s at 3 workers: about 113 s per row slot, ceil(175 / 3) slots
        chunks = plants.split(rows(175), 3, 113.0, 3000)
        self.assertEqual([len(chunk) for chunk in chunks], [78, 78, 19])
        self.assertTrue(all(plants.estimate(chunk, 3, 113.0) <= 3000 for chunk in chunks))

    def test_a_chunk_argument_is_k_of_m(self):
        self.assertEqual(plants.parse_chunk("2/3"), (2, 3))
        for bad in ("0/3", "4/3", "x", "1/0", "1-2", "/2", "1/"):
            with self.assertRaisesRegex(KitError, "--chunk must be K/M"):
                plants.parse_chunk(bad)


class RowSecondsConfiguration(unittest.TestCase):
    def test_evidence_plant_row_seconds_is_a_number_above_zero_and_absent_by_default(self):
        self.assertIsNone(evidence.parse_config(None)["plant_row_seconds"])
        self.assertEqual(evidence.parse_config({"plant_row_seconds": 90})["plant_row_seconds"], 90)
        self.assertEqual(evidence.parse_config({"plant_row_seconds": 12.5})["plant_row_seconds"], 12.5)
        for bad in (0, -1, True, "90"):
            with self.assertRaisesRegex(KitError, "plant_row_seconds must be a number of seconds above 0"):
                evidence.parse_config({"plant_row_seconds": bad})


class PerRowSeconds(PlantBase):
    """Where the seconds per row slot come from, in order."""

    def results(self, task_id=TASK, timing=True):
        ev = evidence.Evidence(self.root, task_id)
        ev.hash_inputs(["calc.py"])
        ev.run_command("unit", [PLANT_PY, "-c", "pass"])
        table = [self.row("judged")] + [self.row(f"later-{n}", replace=f"return {n}", defer=True) for n in range(3)]
        with contextlib.redirect_stdout(io.StringIO()):
            ev.planted_bugs(table, BUILD, [PLANT_PY, str(self.judge)], workers=1)
        if not timing:
            ev.planted_bugs_timing = None
        written = ev.write()
        commit(self.root, "evidence")
        return {"path": written["path"], "digest": written["digest"]}, store.load(self.root, written["path"])

    def chunk(self, task_id, entry, data, seconds, workers, count):
        deferred = [row for row in data["planted_bugs"] if row.get("deferred")][:count]
        out = [{**{k: v for k, v in row.items() if k != "deferred"}, "builds": True, "killed": True,
                "kill_reason": "test failed", "test_output_sha256": "0" * 64} for row in deferred]
        record = {"task_id": task_id, "kind": "plants", "part_of": entry, "head": data["head"], "kit_version": "0.6.0",
                  "produced_at": "2026-11-09T00:00:00Z", "workers": workers, "seconds": seconds, "rejudge": False,
                  "rows": out, "all_killed": True}
        store.record(self.root, task_id, "plants", record)
        commit(self.root, "plants")

    def per_row(self, data, entry, explicit=None):
        return plants.per_row_seconds(self.root, self.root, TASK, entry, data, explicit)

    def test_the_flag_comes_first_then_the_plants_records_of_this_task(self):
        entry, data = self.results()
        self.chunk(TASK, entry, data, seconds=12.0, workers=2, count=3)  # ceil(3 / 2) slots: 6 s each
        self.assertEqual(self.per_row(data, entry, explicit=7.5), (7.5, "--per-row-seconds"))
        seconds, source = self.per_row(data, entry)
        self.assertEqual(seconds, 6.0)
        self.assertEqual(source, f"measured in 1 plants record(s) of {TASK}")

    def test_the_plants_records_of_other_tasks_come_before_the_results_timing(self):
        other, other_data = self.results(T2)
        self.chunk(T2, other, other_data, seconds=30.0, workers=1, count=3)  # 10 s per slot
        self.chunk(T2, other, other_data, seconds=4.0, workers=1, count=1)  # 4 s: 34 s over 4 slots
        entry, data = self.results()
        seconds, source = self.per_row(data, entry)
        self.assertEqual(seconds, 8.5)
        self.assertEqual(source, "measured in 2 plants record(s) of the program's other tasks")

    def test_then_the_results_timing_then_checks_yaml_then_the_default(self):
        entry, data = self.results()
        timing = data["planted_bugs_timing"]
        seconds, source = self.per_row(data, entry)
        self.assertEqual(seconds, round(timing["seconds"] / -(-timing["rows"] // timing["workers"]), 1) or 0.1)
        self.assertEqual(source, f"planted_bugs_timing of {entry['path']}")
        entry, data = self.results(timing=False)
        self.assertEqual(self.per_row(data, entry), (gate.ROW_SECONDS, "no measurement: the default"))
        put_checks = self.root / "program" / "checks.yaml"
        put_checks.parent.mkdir(parents=True, exist_ok=True)
        put_checks.write_text("evidence:\n  plant_row_seconds: 90\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "row seconds")
        self.assertEqual(self.per_row(data, entry), (90, "evidence.plant_row_seconds in program/checks.yaml"))

    def test_an_unreadable_plants_record_of_another_task_is_skipped(self):
        other, other_data = self.results(T2)
        self.chunk(T2, other, other_data, seconds=30.0, workers=1, count=3)
        [item] = store.read_plants(self.root, T2)
        entry, data = self.results(timing=False)
        os.chmod(self.root / item["path"], 0o644)
        (self.root / item["path"]).write_text("{}\n")
        self.assertEqual(self.per_row(data, entry)[1], "no measurement: the default")


class ChunkOptionRefusals(unittest.TestCase):
    def run_pk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=Path("/nonexistent")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", "plants", TASK, "--branch", BRANCH, *argv])
        return rc, out.getvalue() + err.getvalue()

    def test_the_chunk_options_are_checked_before_anything_is_read(self):
        for argv, words in ((["--rows", "a", "--chunk-seconds", "60"], "--chunk-seconds splits --all-pending"),
                            (["--all-pending", "--plan"], "--plan and --chunk need --chunk-seconds"),
                            (["--all-pending", "--chunk", "1/2"], "--plan and --chunk need --chunk-seconds"),
                            (["--all-pending", "--per-row-seconds", "5"], "--per-row-seconds needs --chunk-seconds"),
                            (["--all-pending", "--chunk-seconds", "0"], "--chunk-seconds must be at least 1"),
                            (["--all-pending", "--chunk-seconds", "60", "--per-row-seconds", "0"],
                             "--per-row-seconds must be above 0"),
                            (["--all-pending", "--chunk-seconds", "60", "--chunk", "3/2"], "--chunk must be K/M"),
                            (["--all-pending", "--chunk-seconds", "60", "--plan", "--chunk", "1/2"],
                             "--plan or --chunk, not both"),
                            (["--adopt", "--all-pending"], "--adopt takes no rows or chunk options"),
                            (["--adopt", "--chunk-seconds", "60"], "--adopt takes no rows or chunk options")):
            rc, said = self.run_pk(*argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(words, said, argv)
            self.assertNotIn("worktree", said, argv)


class SplitBase(ChunkBase):
    def setUp(self):
        super().setUp()
        for var in ("PK_SPLIT_SURVIVOR",):
            os.environ.pop(var, None)
        put(self.wt, f"program/evidence/{TASK}/produce.py", PRODUCE_MANY)
        commit(self.wt, "a producer that defers four rows")

    def chunks_of(self):
        return [[row["id"] for row in store.load(self.wt, item["path"])["rows"]]
                for item in store.read_plants(self.wt, TASK)]


class ChunkedRun(SplitBase):
    def test_pending_rows_are_judged_in_chunks_that_fit_one_record_each(self):
        _, entry, data = self.gate_record()
        rc, said, err = self.gate_plants("--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5",
                                         "--workers", "1")
        self.assertEqual((rc, err), (0, ""), said)
        self.assertIn("plants: 5 s per row slot (--per-row-seconds); 4 pending row(s) in 2 chunk(s) of at most 10 s "
                      "with 1 worker(s)", said)
        self.assertIn("plants: chunk 1/2: 2 row(s), about 10 s: add-times, add-more-0", said)
        self.assertIn("plants: chunk 2/2: 2 row(s), about 10 s: add-more-1, add-more-2", said)
        self.assertIn(f"plants: every deferred row of {entry['path']} is killed; QA can accept the record", said)
        self.assertEqual(self.chunks_of(), [ROWS[:2], ROWS[2:]])
        self.assertIsNone(evidence.coverage_problem(self.wt, TASK, entry, data))
        rc, said, _ = self.gate_plants("--all-pending", "--chunk-seconds", "10")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", said)

    def test_the_plan_covers_every_deferred_row_and_names_the_exact_command_of_each_chunk(self):
        self.gate_record()
        self.assertEqual(self.gate_plants("--rows", "add-more-1")[0], 0)  # killed: the plan stays the same
        rc, said, err = self.gate_plants("--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5",
                                         "--workers", "1", "--plan")
        self.assertEqual((rc, err), (0, ""), said)
        command = (f"pk gate plants {TASK} --branch {BRANCH} --all-pending --chunk-seconds 10 --per-row-seconds 5 "
                   "--workers 1")
        self.assertIn("plants: plan over all 4 deferred row(s), 5 s per row slot (--per-row-seconds), at most 10 s "
                      "a chunk with 1 worker(s): 2 chunk(s); nothing judged", said)
        self.assertIn("chunk 1/2: 2 row(s), about 10 s, 2 not killed yet: add-times, add-more-0", said)
        self.assertIn("chunk 2/2: 2 row(s), about 10 s, 1 not killed yet: add-more-1, add-more-2", said)
        self.assertIn(f"  {command} --chunk 1/2", said)
        self.assertIn(f"  {command} --chunk 2/2", said)
        self.assertEqual(len(store.read_plants(self.wt, TASK)), 1)  # the plan judged nothing
        rc, said, err = self.gate_plants("--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5",
                                         "--workers", "1", "--chunk", "1/3")
        self.assertEqual(rc, 1)
        self.assertIn("the plan with --chunk-seconds 10, 5 s per row slot and 1 worker(s) has 2 chunk(s), not 3", err)
        self.assertIn(f"{command} --plan", err)


@skip_unless_slow()
class ChunkOfAPlan(SplitBase):
    def test_a_chunk_of_the_plan_judges_its_rows_not_yet_killed(self):
        _, entry, data = self.gate_record()
        base = ["--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5", "--workers", "1"]
        self.assertEqual(self.gate_plants("--rows", "add-more-2")[0], 0)
        rc, said, err = self.gate_plants(*base, "--chunk", "2/2")
        self.assertEqual((rc, err), (0, ""), said)
        self.assertEqual(self.chunks_of(), [["add-more-2"], ["add-more-1"]])
        self.assertIn("2 of 4 deferred rows have no killed verdict yet", said)
        rc, said, err = self.gate_plants(*base, "--chunk", "2/2")
        self.assertEqual((rc, err), (0, ""), said)
        self.assertIn("plants: chunk 2/2: every row is already killed; nothing to do", said)
        self.assertEqual(self.gate_plants(*base, "--chunk", "1/2")[0], 0)
        self.assertIsNone(evidence.coverage_problem(self.wt, TASK, entry, data))


@skip_unless_slow()
class ChunkedStops(SplitBase):
    def test_the_run_stops_at_the_first_chunk_that_is_not_all_killed_and_says_how_to_go_on(self):
        os.environ["PK_SPLIT_SURVIVOR"] = "1"
        try:
            self.gate_record()
        finally:
            os.environ.pop("PK_SPLIT_SURVIVOR")
        rc, said, _ = self.gate_plants("--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5",
                                       "--workers", "1")
        self.assertEqual(rc, 1, said)
        self.assertEqual(self.chunks_of(), [["add-times", "add-more-0"], ["sub-plus", "add-more-1"]])
        self.assertIn("plants: sub-plus not killed", said)
        self.assertIn("plants: chunk 2/3 is not all killed; stopped before 1 more chunk(s) (1 row(s)); 2 deferred "
                      "row(s) have no killed verdict: sub-plus, add-more-2", said)
        self.assertIn(f"continue with: pk gate plants {TASK} --branch {BRANCH} --all-pending --chunk-seconds 10 "
                      "--per-row-seconds 5", said)


# --- 0.6.0: plants records judged in another checkout, listed here by --adopt -----------------------------------------

class AdoptBase(SplitBase):
    """Plants records judged in another checkout (another machine) are listed in this pointer by --adopt."""

    def elsewhere(self, *argv):
        """Judge in a second clone of the task branch, as another machine would; the new plants files there."""
        other = self.root.parent / "elsewhere"
        if not other.exists():
            sh(self.root.parent, "git", "clone", "-q", str(self.root), str(other))
            sh(other, "git", "worktree", "add", "-q", "-B", BRANCH, f".worktrees/{TASK.lower()}", f"origin/{BRANCH}")
        before = {item["path"] for item in store.read_plants(other / ".worktrees" / TASK.lower(), TASK)}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gate, "find_root", return_value=other), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(["gate", "plants", TASK, "--branch", BRANCH, *argv])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        owt = other / ".worktrees" / TASK.lower()
        return [owt / item["path"] for item in store.read_plants(owt, TASK) if item["path"] not in before]

    def bring(self, files):
        for path in files:
            target = self.wt / path.relative_to(path.parents[4])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


class Adopt(AdoptBase):
    def test_records_judged_elsewhere_are_listed_by_adopt_and_count_for_the_record(self):
        _, entry, data = self.gate_record()
        base = ["--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5", "--workers", "1"]
        self.assertEqual(self.gate_plants(*base, "--chunk", "1/2")[0], 0)  # here: chunk 1, listed in this pointer
        files = self.elsewhere(*base, "--chunk", "2/2")
        self.assertEqual(len(files), 1)
        self.bring(files)
        rc, said, err = self.gate_plants("--adopt")
        self.assertEqual((rc, err), (0, ""), said)
        rel = files[0].relative_to(files[0].parents[4]).as_posix()
        self.assertIn(f"plants: adopted {rel}", said)
        self.assertEqual(self.chunks_of(), [ROWS[:2], ROWS[2:]])
        self.assertIsNone(evidence.coverage_problem(self.wt, TASK, entry, data))
        listed = {item["path"] for item in store.read_plants(self.wt, TASK)}
        self.assertEqual(store.integrity(self.wt, uncommitted=listed), [])
        rc, said, err = self.gate_plants("--adopt")
        self.assertEqual((rc, err), (0, ""), said)
        self.assertIn("plants: nothing to adopt", said)



@skip_unless_slow()
class AdoptRefusals(AdoptBase):
    def test_a_file_that_is_not_a_plants_record_of_this_record_is_refused_and_nothing_is_adopted(self):
        _, entry, data = self.gate_record()
        good = self.elsewhere("--rows", "add-times")
        self.bring(good)
        folder = self.wt / f"program/evidence/{TASK}/plants"
        record = json.loads(good[0].read_text())
        for name, body, words in (
                ("20261109T000000000000Z-000000000000.json", good[0].read_bytes(), "immutable record altered"),
                (None, {**record, "task_id": T2}, "is a record of"),
                (None, {**record, "part_of": {"path": entry["path"].replace(entry["digest"][:12], "1" * 12),
                                              "digest": "1" * 64}}, "not bound to"),
                (None, {**record, "head": "b" * 40}, "judged at"),
                (None, {**record, "seconds": -1}, "not a valid plants record"),
                (None, {**record, "rows": [{**record["rows"][0], "id": "add-minus"}]}, "not a deferred row")):
            raw = body if isinstance(body, bytes) else store.encode(body)
            if name is None:
                name = f"20261109T000000000000Z-{evidence.sha256_bytes(raw)[:12]}.json"
            path = folder / name
            path.write_bytes(raw)
            rc, said, err = self.gate_plants("--adopt")
            self.assertEqual(rc, 1, words)
            self.assertIn(f"program/evidence/{TASK}/plants/{name}", err, words)
            self.assertIn(words, err)
            self.assertEqual(store.read_plants(self.wt, TASK), [], words)  # the good one is not adopted either
            path.unlink()
        self.assertEqual(self.gate_plants("--adopt")[0], 0)
        self.assertEqual(len(store.read_plants(self.wt, TASK)), 1)

    def test_a_record_bound_to_a_record_this_one_reused_is_adopted(self):
        _, entry, data = self.gate_record()
        files = self.elsewhere("--rows", "add-times")
        reused = minimal_record(data["planted_bugs"], head=data["head"],
                                reused_from={"path": entry["path"], "digest": entry["digest"], "head": data["head"]})
        store.record(self.wt, TASK, "results", reused)
        self.bring(files)
        rc, said, err = self.gate_plants("--adopt")
        self.assertEqual((rc, err), (0, ""), said)
        self.assertEqual(len(store.read_plants(self.wt, TASK)), 1)


# --- 0.6.0: a chunk judged on a lane of another machine through pk remote, and adopted here ------------------------

@skip_unless_slow()
class RemoteChunk(SplitBase):
    """`pk remote plants --chunk K/M` against a fake remote machine (a local directory with the bare mirror and a lane
    clone, reached through the fake ssh of test_remote), then the other chunk here."""

    def machine(self):
        """The fake remote machine with one lane; the environment pk remote needs to reach it."""
        from test_remote import FAKE_SSH
        machine, bin_dir = self.root.parent / "machine", self.root.parent / "fake-bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(0o755)
        (bin_dir / "python3").symlink_to(PY)
        shutil.copytree(Path(plants.__file__).parent.parent, self.root / "kit",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        checks = read_yaml(self.root / "program" / "checks.yaml")
        checks["remote"] = {"dir": str(machine), "lanes": ["lane1"], "workers": 1}
        write_yaml(self.root / "program" / "checks.yaml", checks)
        commit(self.root, "the kit and the remote section on main")
        # --no-local: no hard links into a repository whose background gc may repack its objects meanwhile
        sh(self.root.parent, "git", "clone", "-q", "--no-local", "--bare", str(self.root), str(machine / "repo.git"))
        sh(self.root.parent, "git", "clone", "-q", "--no-local", str(machine / "repo.git"), str(machine / "lane1"))
        env = {"PK_REMOTE_HOST": "lanes-host", "PK_REMOTE_SSH": str(ssh), "FAKE_SSH_LOG": str(self.root.parent / "log"),
               "FAKE_BIN": str(bin_dir), "PK_REMOTE_POLL": "0.1", "PK_LOCK_POLL": "0.1",
               "PATH": os.environ["PATH"] + os.pathsep + os.defpath}  # the gate fixture's PATH, and sh, tar, sed
        return machine, env

    def remote(self, env, *argv):
        import remote
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(remote, "find_root", return_value=self.root), \
                mock.patch.object(gate, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            os.environ.pop("GIT_SSH_COMMAND", None)
            rc = pk.main(["remote", *argv])
        return rc, out.getvalue() + err.getvalue()

    def test_a_chunk_judged_on_a_lane_comes_back_as_a_plants_record_and_is_adopted(self):
        _, entry, data = self.gate_record()
        machine, env = self.machine()
        rc, said = self.remote(env, "plants", TASK, "--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5",
                               "--chunk", "2/2", "--lane", "lane1")
        self.assertEqual(rc, 0, said)
        [item] = store.read_plants(self.wt, TASK)
        self.assertIn(f"plants: adopted {item['path']}", said)
        self.assertIn(f"remote: copied back {item['path']}", said)
        self.assertEqual(self.chunks_of(), [ROWS[2:]])
        self.assertEqual(evidence.plants_coverage(self.wt, TASK, entry, data)["pending"], ROWS[:2])
        log = (machine / "lane1.log").read_text().splitlines()
        tip = sh(self.wt, "git", "rev-parse", "HEAD").strip()
        self.assertIn(f"start remote-plants-{TASK} at {tip[:12]}", log[0])
        self.assertTrue(any("plants: chunk 2/2: 2 row(s)" in line for line in log), log)
        self.assertIn(f"end remote-plants-{TASK} exit 0", log[-1])
        self.assertFalse((machine / "lane1" / ".git" / "pk-heavy.lock").exists())
        self.assertEqual(self.gate_plants("--all-pending", "--chunk-seconds", "10", "--per-row-seconds", "5",
                                          "--workers", "1", "--chunk", "1/2")[0], 0)
        self.assertIsNone(evidence.coverage_problem(self.wt, TASK, entry, data))

    def test_a_gate_on_a_lane_brings_its_results_record_and_pointer_back(self):
        _, old_entry, _ = self.gate_record()
        machine, env = self.machine()
        rc, said = self.remote(env, "gate", TASK, "--lane", "lane1", "--", "--no-reuse")
        self.assertEqual(rc, 0, said)
        entry, data = store.load_latest(self.wt, TASK, "results")
        self.assertNotEqual(entry["digest"], old_entry["digest"])
        self.assertIn(f"remote: copied back {entry['path']}", said)
        self.assertIn(f"remote: copied back {store.pointer_path(TASK)}", said)
        self.assertEqual(evidence.validate_results(data), [])
        self.assertIn("OK: task/", (machine / "lane1.log").read_text())


if __name__ == "__main__":
    unittest.main()
