import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import pk
import status
from common import KitError, read_json, sha256_file, write_json, write_yaml

TID = "P1-M01-S01-T01"
REGISTERED = (TID, "P1-M01-S01-T02", "A-1", "A-2", "B-1")
FULL = {"task_id": TID, "status": "in_progress", "role": "controller", "updated_at": "", "note": "",
        "evidence": [], "blocked_by": [], "history": []}


def write_register(root, task_ids=REGISTERED):
    tasks = [{"id": task_id, "title": task_id} for task_id in task_ids]
    write_yaml(Path(root) / "program" / "register.yaml", {"phases": [{"id": "P1", "milestones": [
        {"id": "P1-M01", "steps": [{"id": "P1-M01-S01", "tasks": tasks}]}]}]})


def results_record(task_id=TID, **over):
    """A results record the hand-back gate would pass (the shape of kit/templates/results.schema.json)."""
    digest = "0" * 64
    row = {"builds": True, "killed": True, "kill_reason": "test failed", "test_output_sha256": digest,
           "path": "calc.py", "find": "a + b", "replace": "a - b", "build_argv": ["make"],
           "test_argv": ["make", "test"]}
    data = {"task_id": task_id, "head": "a" * 40, "main": "b" * 40, "merge_base": "b" * 40,
            "produced_at": "2026-01-01T00:00:00Z", "inputs": {"calc.py": digest},
            "commands": [{"label": "unit", "argv": ["make", "test"], "exit": 0, "expected_exit": 0,
                          "as_expected": True, "derived": {}, "output_sha256": digest, "output_tail": ""}],
            "planted_bugs": [dict(row, id="flip"),
                             dict(row, id="self-test-noncompiling", builds=False, killed=False,
                                  kill_reason="build failed", test_output_sha256="", find="", replace="def (:")],
            "planted_bugs_all_killed": True, "all_commands_as_expected": True, "tree_unchanged_by_run": True,
            "stale": {"checked": False, "records": []}, "blocked_by": [], "deviations": []}
    data.update(over)
    return data


class StatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        write_register(self.root)

    # helpers
    def record_path(self, tid=TID):
        return self.root / "program" / "status" / f"{tid}.json"

    def evidence(self, tid=TID, results=None, drop=(), **over):
        """Write results.json and an accepting acceptance.json bound to it; return the acceptance path (relative).

        `results` replaces the record (a str is written as it is); `over` replaces acceptance fields and
        `drop` removes them.
        """
        folder = self.root / "program" / "evidence" / tid
        folder.mkdir(parents=True, exist_ok=True)
        if isinstance(results, str):
            (folder / "results.json").write_text(results, encoding="utf-8")
        else:
            write_json(folder / "results.json", results_record(tid) if results is None else results)
        acceptance = {"task_id": tid, "decision": "accepted_development",
                      "evidence_sha256": sha256_file(folder / "results.json"), "source_commit": "a" * 40,
                      "accepted_at": "2026-01-01T00:00:00Z", "role": "qa", "commands": [], "planted_bugs": [],
                      "findings": [], "blocked_by": []}
        acceptance.update(over)
        write_json(folder / "acceptance.json", {k: v for k, v in acceptance.items() if k not in drop})
        return f"program/evidence/{tid}/acceptance.json"

    def to_review(self, tid=TID):
        status.transition(self.root, tid, "in_progress", "controller")
        return status.transition(self.root, tid, "review", "implementer")

    def refused(self, pattern, *args, **kwargs):
        before = self.record_path(args[0] if args else TID)
        snapshot = before.read_bytes() if before.exists() else None
        with self.assertRaisesRegex(KitError, pattern):
            status.transition(self.root, *args, **kwargs)
        self.assertEqual(before.read_bytes() if before.exists() else None, snapshot, "refusal wrote the record")

    # table
    def test_table_is_exactly_the_spec(self):
        self.assertEqual(status.STATUSES, ("not_started", "in_progress", "review", "done", "blocked", "parked"))
        self.assertEqual(status.TRANSITIONS, {
            ("not_started", "in_progress"): {"controller"},
            ("in_progress", "review"): {"implementer"},
            ("review", "in_progress"): {"qa"},
            ("review", "done"): {"qa"},
            ("not_started", "blocked"): {"controller"},
            ("in_progress", "blocked"): {"controller"},
            ("blocked", "not_started"): {"controller"},
            ("blocked", "in_progress"): {"controller"},
            ("not_started", "parked"): {"controller"},
            ("parked", "not_started"): {"controller"},
        })

    def test_allowed_transitions_by_role(self):
        self.assertEqual(status.read(self.root, TID)["status"], "not_started")  # no file yet: implicit
        for wrong in ("implementer", "qa"):
            self.refused("controller", TID, "in_progress", wrong)
        self.assertFalse((self.root / "program" / "status").exists(), "refusal must not create anything")
        rec = status.transition(self.root, TID, "in_progress", "controller", note="dispatched")
        self.assertEqual((rec["task_id"], rec["status"], rec["role"], rec["note"]),
                         (TID, "in_progress", "controller", "dispatched"))
        self.assertEqual(read_json(self.record_path()), rec)
        for wrong in ("controller", "qa"):
            self.refused("implementer", TID, "review", wrong)
        status.transition(self.root, TID, "review", "implementer")
        self.refused("qa", TID, "in_progress", "controller")
        rec = status.transition(self.root, TID, "in_progress", "qa", note="finding 1")
        self.assertEqual([h["status"] for h in rec["history"]], ["in_progress", "review", "in_progress"])
        self.assertEqual([h["role"] for h in rec["history"]], ["controller", "implementer", "qa"])
        self.assertTrue(all(h["at"].endswith("Z") for h in rec["history"]))
        self.assertEqual(rec["updated_at"], rec["history"][-1]["at"])

    def test_disallowed_edges_unknown_status_and_unknown_role_are_refused(self):
        self.refused("not allowed", TID, "review", "implementer")        # skips in_progress
        self.refused("not allowed", TID, "done", "qa")
        self.refused("not allowed", TID, "not_started", "controller")    # same state
        self.refused("unknown status 'shipped'", TID, "shipped", "controller")
        self.refused("unknown role 'boss'", TID, "in_progress", "boss")
        for bad in ("../x", "a/b", "", ".hidden", "a b"):
            with self.assertRaisesRegex(KitError, "invalid task id"):
                status.transition(self.root, bad, "in_progress", "controller")
        self.assertFalse((self.root / "program" / "status").exists())

    def test_review_to_done_only_qa_with_bound_acceptance(self):
        self.to_review()
        acceptance = self.evidence()
        for wrong in ("controller", "implementer"):
            self.refused("qa", TID, "done", wrong, evidence=[acceptance])
        self.refused("acceptance", TID, "done", "qa")                       # no evidence at all
        self.refused("acceptance", TID, "done", "qa", evidence=["README.md"])  # acceptance not listed
        rec = status.transition(self.root, TID, "done", "qa", evidence=[acceptance], note="accepted")
        self.assertEqual((rec["status"], rec["evidence"]), ("done", [acceptance]))
        self.assertEqual(read_json(self.record_path()), rec)
        self.assertEqual(status.validate(self.root), [])

    def test_evidence_recorded_earlier_counts_and_absolute_paths_are_normalised(self):
        self.to_review()
        acceptance = self.evidence()
        rec = status.transition(self.root, TID, "in_progress", "qa", evidence=[str(self.root / acceptance)])
        self.assertEqual(rec["evidence"], [acceptance])
        status.transition(self.root, TID, "review", "implementer", evidence=[acceptance])
        rec = status.transition(self.root, TID, "done", "qa")
        self.assertEqual(rec["evidence"], [acceptance])  # deduplicated

    def test_done_refuses_acceptance_bound_to_other_evidence(self):
        self.to_review()
        acceptance = self.evidence()
        stale = sha256_file(self.root / "program" / "evidence" / TID / "results.json")
        (self.root / "program" / "evidence" / TID / "results.json").write_text("results v2", encoding="utf-8")
        current = sha256_file(self.root / "program" / "evidence" / TID / "results.json")
        with self.assertRaises(KitError) as ctx:
            status.transition(self.root, TID, "done", "qa", evidence=[acceptance])
        self.assertIn(stale, str(ctx.exception))
        self.assertIn(current, str(ctx.exception))
        self.assertNotIn("\n", str(ctx.exception))
        self.assertEqual(status.read(self.root, TID)["status"], "review")

    def test_done_refuses_missing_or_malformed_acceptance_and_missing_results(self):
        self.to_review()
        acceptance = self.evidence()
        folder = self.root / "program" / "evidence" / TID
        (folder / "acceptance.json").write_text("not json", encoding="utf-8")
        self.refused("invalid JSON", TID, "done", "qa", evidence=[acceptance])
        write_json(folder / "acceptance.json", {"verdict": "ok"})
        self.refused("evidence_sha256", TID, "done", "qa", evidence=[acceptance])
        write_json(folder / "acceptance.json", ["x"])
        self.refused("evidence_sha256", TID, "done", "qa", evidence=[acceptance])
        self.evidence()
        (folder / "results.json").unlink()
        self.refused("results.json", TID, "done", "qa", evidence=[acceptance])
        (folder / "acceptance.json").unlink()
        self.refused("acceptance.json", TID, "done", "qa", evidence=[acceptance])

    def test_done_refuses_an_acceptance_that_does_not_accept(self):
        self.to_review()
        cases = {"rejecting": dict(decision="rejected"), "made up": dict(decision="fine"),
                 "not text": dict(decision=True), "missing": dict(drop=("decision",))}
        for name, how in cases.items():
            with self.subTest(case=name):
                acceptance = self.evidence(**how)
                with self.assertRaises(KitError) as ctx:
                    status.transition(self.root, TID, "done", "qa", evidence=[acceptance])
                self.assertIn("decision", str(ctx.exception))
                self.assertNotIn("\n", str(ctx.exception))
                self.assertEqual(status.read(self.root, TID)["status"], "review")
        self.refused("accepted_development", TID, "done", "qa", evidence=[self.evidence(decision="rejected")])
        self.refused("task_id", TID, "done", "qa", evidence=[self.evidence(task_id="P1-M01-S01-T02")])
        self.refused("role", TID, "done", "qa", evidence=[self.evidence(role="implementer")])
        self.refused("source_commit", TID, "done", "qa", evidence=[self.evidence(drop=("source_commit",))])
        self.assertEqual(status.transition(self.root, TID, "done", "qa", evidence=[self.evidence()])["status"], "done")

    def test_done_refuses_a_one_field_rejection_over_empty_results(self):
        self.to_review()
        folder = self.root / "program" / "evidence" / TID
        folder.mkdir(parents=True)
        (folder / "results.json").write_text("{}", encoding="utf-8")
        write_json(folder / "acceptance.json", {"decision": "rejected", "findings": [{"n": 1, "severity": "critical"}],
                                                "evidence_sha256": sha256_file(folder / "results.json")})
        acceptance = f"program/evidence/{TID}/acceptance.json"
        with self.assertRaises(KitError) as ctx:
            status.transition(self.root, TID, "done", "qa", evidence=[acceptance])
        self.assertNotIn("\n", str(ctx.exception))
        self.assertEqual(status.read(self.root, TID)["status"], "review")
        self.evidence(results="{}")  # a complete, accepting acceptance of a record that is not evidence
        self.refused("results.json: missing key", TID, "done", "qa", evidence=[acceptance])

    def test_done_refuses_results_the_gate_would_not_pass(self):
        self.to_review()
        survivor = results_record(planted_bugs_all_killed=False)
        survivor["planted_bugs"][0].update(killed=False, kill_reason="not killed")
        unexpected = results_record(all_commands_as_expected=False)
        unexpected["commands"][0].update(exit=1, as_expected=False)
        cases = {"planted_bugs_all_killed": survivor, "all_commands_as_expected": unexpected,
                 "task_id": results_record("P1-M01-S01-T02"), "commands": results_record(commands=[]),
                 "head": results_record(head="abc"), "JSON object": []}
        for pattern, record in cases.items():
            with self.subTest(case=pattern):
                acceptance = self.evidence(results=record)
                self.refused(f"results.json: .*{pattern}", TID, "done", "qa", evidence=[acceptance])

    def test_validate_reports_a_done_record_whose_acceptance_rejects_or_whose_results_are_empty(self):
        self.to_review()
        acceptance = self.evidence()
        status.transition(self.root, TID, "done", "qa", evidence=[acceptance])
        self.assertEqual(status.validate(self.root), [])
        self.evidence(decision="rejected")
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("decision", problems[0])
        self.evidence(results="{}")
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("results.json: missing key", problems[0])

    def test_task_not_in_the_register_is_refused(self):
        self.refused("P9-M01-S01-T01.*not a task in program/register.yaml", "P9-M01-S01-T01", "in_progress",
                     "controller")
        self.assertFalse((self.root / "program" / "status").exists())
        write_register(self.root, ["P9-M01-S01-T01"])
        self.assertEqual(status.transition(self.root, "P9-M01-S01-T01", "in_progress", "controller")["status"],
                         "in_progress")
        self.refused("not a task in program/register.yaml", TID, "in_progress", "controller")
        for text in ("phases: 5\n", "phases:\n  - 5\n  - milestones: 7\n  - milestones:\n      - steps:\n"
                     "          - tasks: x\n          - tasks:\n              - 5\n              - id: 9\n"):
            (self.root / "program" / "register.yaml").write_text(text, encoding="utf-8")
            with self.assertRaises(KitError) as ctx:
                status.transition(self.root, TID, "in_progress", "controller")
            self.assertNotIn("\n", str(ctx.exception))
        (self.root / "program" / "register.yaml").unlink()
        self.refused("register.yaml", TID, "in_progress", "controller")
        self.assertEqual(status.read(self.root, TID)["status"], "not_started")  # reading needs no register

    def test_scalar_and_element_types_are_checked_on_read(self):
        entry = {"status": "in_progress", "role": "controller", "at": "2026-01-01T00:00:00Z"}
        cases = {"task_id": 5, "role": 7, "updated_at": None, "note": ["x"], "evidence": [1], "blocked_by": [{}],
                 "history": ["x"]}
        bad_entries = ({"status": "in_progress", "role": "controller"}, dict(entry, at=5), dict(entry, role=None),
                       dict(entry, status=3))
        records = [(field, dict(FULL, **{field: value})) for field, value in cases.items()]
        records += [("history", dict(FULL, history=[entry, bad])) for bad in bad_entries]
        for field, record in records:
            with self.subTest(field=field, value=record[field]):
                write_json(self.record_path(), record)
                for reader in (lambda: status.read(self.root, TID), lambda: status.list_all(self.root),
                               lambda: status.transition(self.root, TID, "review", "implementer")):
                    with self.assertRaises(KitError) as ctx:
                        reader()
                    self.assertIn(field, str(ctx.exception))
                    self.assertNotIn("\n", str(ctx.exception))
                problems = status.validate(self.root)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(field, problems[0])
        write_json(self.record_path(), dict(FULL, history=[entry], evidence=["a"], blocked_by=["b"]))
        self.assertEqual(status.read(self.root, TID)["history"], [entry])

    def test_done_is_terminal(self):
        self.to_review()
        status.transition(self.root, TID, "done", "qa", evidence=[self.evidence()])
        for to in status.STATUSES:
            for role in ("controller", "implementer", "qa"):
                self.refused("terminal", TID, to, role)

    def test_blocked_requires_blocked_by(self):
        self.refused("blocked_by", TID, "blocked", "controller")
        self.refused("blocked_by", TID, "blocked", "controller", blocked_by=[])
        self.refused("controller", TID, "blocked", "qa", blocked_by=["P1-M01-S01-T00"])
        rec = status.transition(self.root, TID, "blocked", "controller", blocked_by=["P1-M01-S01-T00"])
        self.assertEqual((rec["status"], rec["blocked_by"]), ("blocked", ["P1-M01-S01-T00"]))
        self.refused("controller", TID, "in_progress", "implementer")
        rec = status.transition(self.root, TID, "in_progress", "controller")
        self.assertEqual(rec["blocked_by"], [])
        status.transition(self.root, TID, "blocked", "controller", blocked_by=["X"])
        self.assertEqual(status.transition(self.root, TID, "not_started", "controller")["status"], "not_started")
        self.refused("blocked_by", TID, "in_progress", "controller", blocked_by=["X"])  # only blocked takes it
        self.to_review("P1-M01-S01-T02")
        self.refused("not allowed", "P1-M01-S01-T02", "blocked", "controller", blocked_by=["X"])

    def test_parked_carries_the_decision_it_waits_on(self):
        self.refused("note", TID, "parked", "controller")
        self.refused("controller", TID, "parked", "qa", note="needs user")
        rec = status.transition(self.root, TID, "parked", "controller", note="user: pick vendor A or B")
        self.assertEqual(rec["status"], "parked")
        self.refused("controller", TID, "not_started", "implementer")
        self.assertEqual(status.transition(self.root, TID, "not_started", "controller")["status"], "not_started")
        status.transition(self.root, TID, "in_progress", "controller")
        self.refused("not allowed", TID, "parked", "controller", note="late")  # only from not_started

    def test_list_all_is_sorted_and_read_reports_corruption(self):
        self.assertEqual(status.list_all(self.root), [])
        status.transition(self.root, "B-1", "in_progress", "controller")
        status.transition(self.root, "A-1", "in_progress", "controller")
        self.assertEqual([r["task_id"] for r in status.list_all(self.root)], ["A-1", "B-1"])
        self.record_path("A-1").write_text("{oops", encoding="utf-8")
        with self.assertRaisesRegex(KitError, "invalid JSON"):
            status.read(self.root, "A-1")

    def test_validate_reports_unknown_status_and_missing_evidence(self):
        self.assertEqual(status.validate(self.root), [])
        status.transition(self.root, "A-1", "in_progress", "controller")
        status.transition(self.root, "A-2", "in_progress", "controller")
        rec = read_json(self.record_path("A-1"))
        rec["status"] = "shipped"
        write_json(self.record_path("A-1"), rec)
        rec = read_json(self.record_path("A-2"))
        rec["evidence"] = ["program/evidence/A-2/gone.txt"]
        write_json(self.record_path("A-2"), rec)
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("A-1" in p and "unknown status 'shipped'" in p for p in problems), problems)
        self.assertTrue(any("A-2" in p and "gone.txt" in p and "missing" in p for p in problems), problems)

    def test_validate_reports_done_whose_acceptance_no_longer_binds(self):
        self.to_review()
        status.transition(self.root, TID, "done", "qa", evidence=[self.evidence()])
        self.assertEqual(status.validate(self.root), [])
        (self.root / "program" / "evidence" / TID / "results.json").write_text("changed", encoding="utf-8")
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(TID, problems[0])
        self.assertIn("evidence_sha256", problems[0])

    def test_validate_reports_shape_problems_without_raising(self):
        folder = self.root / "program" / "status"
        folder.mkdir(parents=True)
        (folder / "A-1.json").write_text("{oops", encoding="utf-8")
        write_json(folder / "A-2.json", ["not", "a", "record"])
        write_json(folder / "A-3.json", {"task_id": "other", "status": "blocked", "role": "controller",
                                         "updated_at": "", "note": "", "evidence": [], "blocked_by": [],
                                         "history": []})
        problems = status.validate(self.root)
        self.assertTrue(any("A-1" in p and "invalid JSON" in p for p in problems), problems)
        self.assertTrue(any("A-2" in p for p in problems), problems)
        self.assertTrue(any("A-3" in p and "task_id" in p for p in problems), problems)
        self.assertTrue(any("A-3" in p and "blocked_by" in p for p in problems), problems)

    def test_evidence_path_outside_root_or_with_dotdot_is_refused(self):
        outside = str(self.root.parent / "elsewhere.json")
        for bad in ("../x.json", "program/../../x.json", outside):
            self.refused("evidence path", TID, "in_progress", "controller", evidence=[bad])
        self.assertFalse((self.root / "program" / "status").exists())

    def test_wrong_shape_record_is_a_kit_error_not_a_traceback(self):
        full = dict(FULL)
        for case in ({}, {k: v for k, v in full.items() if k != "evidence"},
                     {k: v for k, v in full.items() if k != "history"}, dict(full, status="shipped"),
                     dict(full, history="x")):
            write_json(self.record_path(), case)
            before = self.record_path().read_bytes()
            with self.assertRaisesRegex(KitError, TID):
                status.transition(self.root, TID, "review", "implementer")
            with self.assertRaises(KitError):
                status.list_all(self.root)
            self.assertEqual(self.record_path().read_bytes(), before)

    def test_accepting_decision_is_the_one_the_acceptance_schema_names(self):
        schema = json.loads((Path(__file__).resolve().parent.parent / "templates" / "acceptance.schema.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["decision"]["enum"], [status.ACCEPTED, "rejected"])
        self.assertEqual(schema["properties"]["role"]["enum"], ["qa"])
        self.assertIn("decision", schema["required"])

    def test_schema_template_describes_the_record(self):
        schema = json.loads((Path(__file__).resolve().parent.parent / "templates" / "status.schema.json")
                            .read_text(encoding="utf-8"))
        rec = self.to_review()
        self.assertEqual(set(schema["required"]), set(rec))
        self.assertEqual(schema["properties"]["status"]["enum"], list(status.STATUSES))
        self.assertEqual(set(schema["properties"]), set(rec))


class StatusCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        write_register(self.root)
        old = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)

    def run_pk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pk.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_set_show_list_validate(self):
        rc, out, err = self.run_pk("status", "set", TID, "in_progress", "--role", "controller", "--note", "go")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(TID, out)
        self.assertIn("in_progress", out)
        rc, out, err = self.run_pk("status", "show", TID)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["note"], "go")
        rc, out, err = self.run_pk("status", "list")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.split(), [TID, "in_progress", "controller", json.loads(self.run_pk("status", "show", TID)[1])["updated_at"]])
        rc, out, err = self.run_pk("status", "validate")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("ok", out)

    def test_set_with_blocked_by_and_evidence_flags(self):
        rc, _, err = self.run_pk("status", "set", TID, "blocked", "--role", "controller", "--blocked-by", "X-1",
                                 "--blocked-by", "X-2")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(read_json(self.root / "program" / "status" / f"{TID}.json")["blocked_by"], ["X-1", "X-2"])

    def test_refusal_is_one_error_line_with_no_stdout(self):
        rc, out, err = self.run_pk("status", "set", TID, "done", "--role", "qa")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: "))
        self.assertEqual(err.count("\n"), 1)
        self.assertFalse((self.root / "program" / "status").exists())
        rc, out, err = self.run_pk("status", "set", TID, "in_progress")  # --role is required
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: "))
        rc, out, err = self.run_pk("status")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: "))

    def test_wrong_shape_record_gives_one_error_line_through_transition_and_list(self):
        folder = self.root / "program" / "status"
        full = {"task_id": TID, "status": "in_progress", "role": "controller", "updated_at": "", "note": "",
                "evidence": [], "blocked_by": [], "history": []}
        for missing in ("evidence", "history"):
            write_json(folder / f"{TID}.json", {k: v for k, v in full.items() if k != missing})
            before = (folder / f"{TID}.json").read_bytes()
            for argv in (("status", "set", TID, "review", "--role", "implementer"), ("status", "list")):
                rc, out, err = self.run_pk(*argv)
                self.assertEqual((rc, out), (1, ""), argv)
                self.assertTrue(err.startswith("error: "), err)
                self.assertEqual(err.count("\n"), 1, err)
                self.assertIn(missing, err)
                self.assertNotIn("Traceback", err)
            self.assertEqual((folder / f"{TID}.json").read_bytes(), before)

    def test_malformed_scalars_are_one_error_line_from_every_action(self):
        path = self.root / "program" / "status" / f"{TID}.json"
        write_json(path, dict(FULL, task_id=5, role=7))
        before = path.read_bytes()
        for argv in (("status", "list"), ("status", "show", TID), ("status", "validate"),
                     ("status", "set", TID, "review", "--role", "implementer")):
            with self.subTest(argv=argv):
                rc, out, err = self.run_pk(*argv)
                self.assertEqual((rc, out), (1, ""))
                self.assertTrue(err.startswith("error: "), err)
                self.assertEqual(err.count("\n"), 1, err)
                self.assertIn("task_id", err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(path.read_bytes(), before)
        write_json(path, dict(FULL, role=7))
        rc, out, err = self.run_pk("status", "list")
        self.assertEqual((rc, out, err.count("\n")), (1, "", 1))
        self.assertIn("role", err)

    def test_set_refuses_an_unregistered_task_with_one_error_line(self):
        rc, out, err = self.run_pk("status", "set", "P9-M01-S01-T01", "in_progress", "--role", "controller")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, "error: P9-M01-S01-T01: not a task in program/register.yaml\n")
        self.assertFalse((self.root / "program" / "status").exists())

    def test_done_with_a_rejecting_acceptance_is_one_error_line(self):
        for argv in (("in_progress", "--role", "controller"), ("review", "--role", "implementer")):
            self.assertEqual(self.run_pk("status", "set", TID, *argv)[0], 0)
        folder = self.root / "program" / "evidence" / TID
        write_json(folder / "results.json", results_record())
        acceptance = {"task_id": TID, "decision": "rejected", "evidence_sha256": sha256_file(folder / "results.json"),
                      "source_commit": "a" * 40, "accepted_at": "2026-01-01T00:00:00Z", "role": "qa", "commands": [],
                      "planted_bugs": [], "findings": [{"n": 1, "severity": "critical", "location": "x", "fix": "y"}],
                      "blocked_by": []}
        write_json(folder / "acceptance.json", acceptance)
        argv = ("status", "set", TID, "done", "--role", "qa", "--evidence", f"program/evidence/{TID}/acceptance.json")
        rc, out, err = self.run_pk(*argv)
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"error: {TID}: program/evidence/{TID}/acceptance.json: decision is 'rejected', not "
                              "'accepted_development'\n")
        self.assertEqual(self.run_pk("status", "validate"), (0, "status: ok\n", ""))
        write_json(folder / "acceptance.json", dict(acceptance, decision="accepted_development", findings=[]))
        self.assertEqual(self.run_pk(*argv), (0, f"{TID}: done\n", ""))
        self.assertEqual(self.run_pk("status", "validate"), (0, "status: ok\n", ""))

    def test_validate_prints_each_problem_and_fails(self):
        self.run_pk("status", "set", TID, "in_progress", "--role", "controller")
        path = self.root / "program" / "status" / f"{TID}.json"
        rec = read_json(path)
        rec["status"] = "shipped"
        write_json(path, rec)
        rc, out, err = self.run_pk("status", "validate")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("unknown status 'shipped'", err)


if __name__ == "__main__":
    unittest.main()
