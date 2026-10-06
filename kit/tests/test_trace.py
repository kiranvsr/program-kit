import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
import trace as trace_module  # kit/scripts/trace.py (it shadows the standard library module of that name)
from common import KitError, read_yaml, write_json, write_yaml
from test_register import reg, task

KIT = Path(__file__).resolve().parent.parent
T1, T2, T3 = "P1-M01-S01-T01", "P1-M01-S01-T02", "P1-M01-S01-T03"


def matrix(**sections):
    base = {"journeys": {}, "requirements": {}, "components": {}, "gates": {}, "tasks": {}}
    base.update(sections)
    return base


def full_matrix(**sections):
    """J1 <-> REQ-001 (C1) and an NFR-001 on C2, one gate; consistent at every stage up to 5."""
    base = matrix(
        journeys={"J1": {"title": "sign up", "requirements": ["REQ-001"]}},
        requirements={"REQ-001": {"title": "form", "journeys": ["J1"], "components": ["C1"], "nfr": False},
                      "NFR-001": {"title": "fast", "journeys": [], "components": ["C2"], "nfr": True}},
        components={"C1": {"title": "web", "requirements": ["REQ-001"]},
                    "C2": {"title": "api", "requirements": ["NFR-001"]}},
        gates={"G-P0": {"claims": ["REQ-001 works"], "records": ["program/evidence/g.json"]}})
    base.update(sections)
    return base


GATES = {"gates": {"G-P0": {"title": "p0", "claims": ["REQ-001 works"], "environment": "dev",
                            "records": [{"path": "program/evidence/g.json", "acceptance": "ok"}]}}}


class TraceBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "program").mkdir()

    def put(self, data, name="traceability.yaml"):
        write_yaml(self.root / "program" / name, data)

    def put_register(self, *tasks):
        write_yaml(self.root / "program" / "register.yaml", reg(*tasks))

    def put_status(self, task_id, state):
        record = {"task_id": task_id, "status": state, "role": "", "updated_at": "", "note": "",
                  "evidence": [], "blocked_by": [], "history": []}
        write_json(self.root / "program" / "status" / f"{task_id}.json", record)

    def validate(self, stage):
        return trace_module.validate(self.root, stage)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("trace.find_root", return_value=self.root):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class ValidateTests(TraceBase):
    def test_validate_stage_gates_apply_progressively(self):
        self.assertEqual(self.validate(1), [])  # nothing is required before stage 2
        self.assertEqual(self.validate(2), ["program/traceability.yaml is missing"])
        self.assertEqual(self.validate(6), ["program/traceability.yaml is missing"])

        self.put(matrix(journeys={"J1": {"title": "sign up", "requirements": []}}))
        self.assertEqual(self.validate(2), [])  # journeys may precede any requirement
        self.assertEqual(len(self.validate(3)), 1)
        self.assertTrue(self.validate(3)[0].startswith("J1:"))

        self.put(full_matrix(components={}, gates={}, requirements={
            "REQ-001": {"title": "form", "journeys": ["J1"], "components": [], "nfr": False}}))
        self.assertEqual(self.validate(3), [])
        stage4 = self.validate(4)
        self.assertTrue(any(p.startswith("REQ-001:") and "component" in p for p in stage4), stage4)
        self.assertIn("program/gates.yaml is missing", stage4)

        self.put(full_matrix())
        self.put({"gates": {"G-P0": {"title": "p0", "claims": [], "environment": "dev", "records": []}}}, "gates.yaml")
        self.assertTrue(any(p.startswith("G-P0:") and "record" in p for p in self.validate(4)))
        self.assertEqual(self.validate(3), [])  # gates only matter from stage 4

        self.put(GATES, "gates.yaml")
        self.assertEqual(self.validate(4), [])
        self.assertEqual(self.validate(5), [])
        stage6 = self.validate(6)
        self.assertEqual(sorted(p.split(":")[0] for p in stage6), ["NFR-001", "REQ-001"])  # nothing closes them yet

        self.put(full_matrix(tasks={T1: {"closes": ["REQ-001", "J1"]}, T2: {"closes": ["NFR-001"]}}))
        self.assertEqual(self.validate(6), [])

    def test_journey_without_requirement_fails(self):
        self.put(full_matrix(journeys={"J1": {"title": "a", "requirements": ["REQ-001"]},
                                       "J2": {"title": "b", "requirements": []},
                                       "J3": {"title": "c"}}))
        problems = self.validate(3)
        self.assertEqual([p.split(":")[0] for p in problems], ["J2", "J3"])
        # stage 2 tolerates it only while the requirements section is still empty
        self.assertEqual([p.split(":")[0] for p in self.validate(2)], ["J2", "J3"])
        self.put(matrix(journeys={"J2": {"title": "b", "requirements": []}}))
        self.assertEqual(self.validate(2), [])
        self.assertEqual([p.split(":")[0] for p in self.validate(3)], ["J2"])

    def test_stage_2_needs_journeys(self):
        self.put(matrix())
        self.assertEqual(len(self.validate(2)), 1)
        self.assertTrue(self.validate(2)[0].startswith("program/traceability.yaml"))
        self.assertEqual(self.validate(1), [])

    def test_task_closing_unknown_id_fails(self):
        self.put(GATES, "gates.yaml")
        self.put(full_matrix(tasks={T1: {"closes": ["REQ-001", "REQ-099", "J7", "NFR-001"]}}))
        problems = self.validate(6)
        self.assertEqual([p.split(":")[0] for p in problems], [T1, T1])
        self.assertTrue(any("REQ-099" in p for p in problems))
        self.assertTrue(any("J7" in p for p in problems))
        self.assertEqual(self.validate(5), [])  # unknown references only matter at stage 6

    def test_non_first_release_requirement_need_not_be_closed(self):
        later = {"REQ-001": {"title": "form", "journeys": ["J1"], "components": ["C1"], "release": "later"},
                 "NFR-001": {"title": "fast", "components": ["C2"], "nfr": True, "release": "first"}}
        self.put(GATES, "gates.yaml")
        self.put(full_matrix(requirements=later, tasks={T1: {"closes": ["NFR-001"]}}))
        self.assertEqual(self.validate(6), [])

    def test_requirement_needs_journey_unless_nfr_and_links_are_symmetric(self):
        self.put(full_matrix(
            journeys={"J1": {"title": "a", "requirements": ["REQ-001", "REQ-002"]},
                      "J2": {"title": "b", "requirements": ["REQ-001"]}},
            requirements={"REQ-001": {"title": "x", "journeys": ["J1", "J9"], "components": ["C1"]},
                          "REQ-002": {"title": "y", "journeys": [], "components": ["C1"]},
                          "REQ-003": {"title": "z", "components": ["C1"], "nfr": True}}))
        problems = "\n".join(self.validate(3))
        self.assertIn("REQ-001: names journey J9 which does not exist", problems)
        self.assertIn("J2: lists REQ-001 but REQ-001 does not list J2", problems)
        self.assertIn("J1: lists REQ-002 but REQ-002 does not list J1", problems)
        self.assertIn("REQ-002: names no journey", problems)
        self.assertNotIn("REQ-003", problems)
        self.assertEqual(self.validate(2), [])  # symmetry is a stage 3 rule

    def test_unknown_component_and_gate_references(self):
        data = full_matrix(requirements={"REQ-001": {"title": "x", "journeys": ["J1"], "components": ["C1", "C7"]}},
                           gates={"G-P0": {"records": ["a"]}, "G-NOPE": {"records": ["a"]}})
        self.put(data)
        self.put(GATES, "gates.yaml")
        problems = "\n".join(self.validate(4))
        self.assertIn("REQ-001: names component C7 which does not exist", problems)
        self.assertIn("G-NOPE: is in traceability.yaml but not in gates.yaml", problems)
        self.assertNotIn("G-P0", problems)

    def test_id_patterns_are_checked_at_every_stage(self):
        self.put(matrix(journeys={"j1": {"requirements": ["REQ-1"]}},
                        requirements={"REQ-1": {"journeys": ["J-1"], "components": ["Cx"]}},
                        components={"c1": {}}, gates={"g-p0": {}}, tasks={"T1": {"closes": ["REQ-1"]}}))
        problems = self.validate(1)
        flagged = {p.split(":")[0] for p in problems}
        self.assertEqual(flagged, {"j1", "REQ-1", "c1", "g-p0", "T1"})
        self.assertTrue(all("REQ-1" in p or "J-1" in p or "Cx" in p or "is not a valid" in p for p in problems))

    def test_wrong_shape_raises(self):
        for data in (["a"], matrix(journeys=["J1"]), matrix(tasks="x")):
            self.put(data)
            with self.assertRaises(KitError):
                self.validate(2)
        self.put(full_matrix())
        self.put(["x"], "gates.yaml")
        with self.assertRaises(KitError):
            self.validate(4)

    def test_empty_sections_may_be_null(self):
        (self.root / "program" / "traceability.yaml").write_text("journeys:\nrequirements:\n")
        self.assertEqual(self.validate(1), [])


class SyncTests(TraceBase):
    def test_sync_copies_closes_and_leaves_other_sections(self):
        data = full_matrix(tasks={"P9-M01-S01-T01": {"closes": ["J1"]}})
        self.put(data)
        self.put_register(task(T1, closes=["REQ-001", "J1"]), task(T2), task(T3, closes=["NFR-001"]))
        trace_module.sync(self.root)
        after = read_yaml(self.root / "program" / "traceability.yaml")
        self.assertEqual(after["tasks"], {T1: {"closes": ["REQ-001", "J1"]}, T2: {"closes": []},
                                          T3: {"closes": ["NFR-001"]}})
        self.assertEqual(list(after["tasks"]), [T1, T2, T3])
        for section in ("journeys", "requirements", "components", "gates"):
            self.assertEqual(after[section], data[section], section)

    def test_sync_without_register_raises(self):
        self.put(full_matrix())
        with self.assertRaises(KitError):
            trace_module.sync(self.root)

    def test_sync_without_traceability_file_starts_from_empty_sections(self):
        self.put_register(task(T1, closes=["REQ-001"]))
        trace_module.sync(self.root)
        self.assertEqual(read_yaml(self.root / "program" / "traceability.yaml"),
                         matrix(tasks={T1: {"closes": ["REQ-001"]}}))


class ImpactTests(TraceBase):
    def setUp(self):
        super().setUp()
        self.put(full_matrix(
            journeys={"J1": {"title": "a", "requirements": ["REQ-001"]}, "J2": {"title": "b", "requirements": []}},
            gates={"G-P0": {"claims": ["REQ-001 works"], "records": []},
                   "G-P1": {"claims": ["C10 is up"], "records": ["program/evidence/C2-check.json"]},
                   "G-P2": {"claims": ["unrelated"], "records": []}}))
        self.put_register(task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]), task(T3, closes=["NFR-001"]),
                          task("P1-M01-S01-T04", closes=["J2"]), task("P1-M01-S01-T05", closes=["REQ-001"]),
                          task("P1-M01-S01-T06"))
        trace_module.sync(self.root)

    def test_impact_splits_tasks_by_status(self):
        self.put_status(T1, "done")
        self.put_status(T2, "in_progress")
        self.put_status("P1-M01-S01-T05", "parked")
        self.put_status(T3, "done")
        result = trace_module.impact(self.root, ["J1"])
        self.assertEqual(result, {"requirements": ["REQ-001"], "components": ["C1"], "gates": ["G-P0"],
                                  "tasks": {"done": [T1], "in_flight": [T2], "not_started": ["P1-M01-S01-T05"]}})
        for state in ("review", "blocked"):
            self.put_status(T2, state)
            self.assertEqual(trace_module.impact(self.root, ["J1"])["tasks"]["in_flight"], [T2], state)
        # a journey is only matched when it is named: T2 closes J1, which REQ-001 alone does not reach
        self.assertNotIn(T2, sum(trace_module.impact(self.root, ["REQ-001"])["tasks"].values(), []))

    def test_impact_follows_components_and_gate_mentions(self):
        result = trace_module.impact(self.root, ["C2"])
        self.assertEqual(result["requirements"], ["NFR-001"])
        self.assertEqual(result["components"], ["C2"])
        self.assertEqual(result["gates"], ["G-P1"])  # via the record path; C10 in G-P1 claims is not C1
        self.assertEqual(result["tasks"], {"done": [], "in_flight": [], "not_started": [T3]})
        self.assertEqual(trace_module.impact(self.root, ["C1"])["gates"], ["G-P0"])  # C1 is not C10

    def test_impact_of_a_gate_and_mixed_ids(self):
        result = trace_module.impact(self.root, ["G-P2", "NFR-001", "J2"])
        self.assertEqual(result["gates"], ["G-P1", "G-P2"])
        self.assertEqual(result["requirements"], ["NFR-001"])
        self.assertEqual(result["tasks"]["not_started"], [T3, "P1-M01-S01-T04"])
        self.assertEqual(trace_module.impact(self.root, ["G-P2"]),
                         {"requirements": [], "components": [], "gates": ["G-P2"],
                          "tasks": {"done": [], "in_flight": [], "not_started": []}})

    def test_impact_reads_gates_yaml_and_dedupes(self):
        self.put(GATES, "gates.yaml")
        result = trace_module.impact(self.root, ["REQ-001", "J1", "REQ-001"])
        self.assertEqual(result["gates"], ["G-P0"])
        self.assertEqual(result["requirements"], ["REQ-001"])

    def test_impact_unknown_id_names_the_first_one(self):
        with self.assertRaises(KitError) as caught:
            trace_module.impact(self.root, ["J1", "REQ-404", "C9"])
        self.assertIn("REQ-404", str(caught.exception))
        self.assertNotIn("C9", str(caught.exception))
        with self.assertRaises(KitError):
            trace_module.impact(self.root, ["banana"])

    def test_impact_without_file_raises(self):
        (self.root / "program" / "traceability.yaml").unlink()
        with self.assertRaises(KitError):
            trace_module.impact(self.root, ["J1"])


class CliTests(TraceBase):
    def test_validate_prints_problems_to_stderr(self):
        self.put(full_matrix(journeys={"J1": {"title": "a", "requirements": []}}))
        code, out, err = self.run_cli("trace", "validate", "--stage", "3")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        lines = err.splitlines()
        self.assertTrue(lines and all(line.startswith("error: ") for line in lines), err)
        self.assertTrue(lines[0].startswith("error: J1:"))

    def test_validate_ok(self):
        self.put(full_matrix())
        self.put(GATES, "gates.yaml")
        self.assertEqual(self.run_cli("trace", "validate", "--stage", "4"), (0, "traceability valid at stage 4\n", ""))

    def test_validate_needs_a_numeric_stage(self):
        self.assertEqual(self.run_cli("trace", "validate")[0], 1)
        code, _, err = self.run_cli("trace", "validate", "--stage", "two")
        self.assertEqual((code, err.count("\n")), (1, 1))
        self.assertTrue(err.startswith("error: "))

    def test_sync_and_impact(self):
        self.put(full_matrix())
        self.put_register(task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]))
        self.assertEqual(self.run_cli("trace", "sync"), (0, "synced 2 tasks\n", ""))
        code, out, err = self.run_cli("trace", "impact", "J1", "REQ-001")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"requirements": ["REQ-001"], "components": ["C1"], "gates": ["G-P0"],
                                           "tasks": {"done": [], "in_flight": [], "not_started": [T1, T2]}})
        self.assertEqual(out, json.dumps(json.loads(out), indent=2, sort_keys=True) + "\n")

    def test_impact_unknown_id_is_one_error_line(self):
        self.put(full_matrix())
        code, out, err = self.run_cli("trace", "impact", "J99")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: J99 is not in program/traceability.yaml\n")

    def test_no_action(self):
        self.assertEqual(self.run_cli("trace")[0], 1)


class TemplateTests(unittest.TestCase):
    def test_templates_parse_to_empty_sections(self):
        self.assertEqual(read_yaml(KIT / "templates" / "traceability.yaml"), matrix())
        self.assertEqual(read_yaml(KIT / "templates" / "gates.yaml"), {"gates": {}})


if __name__ == "__main__":
    unittest.main()
