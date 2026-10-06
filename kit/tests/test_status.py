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
from common import KitError, read_json, sha256_file, write_json

TID = "P1-M01-S01-T01"


class StatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()

    # helpers
    def record_path(self, tid=TID):
        return self.root / "program" / "status" / f"{tid}.json"

    def evidence(self, tid=TID, results="results v1", bind=None):
        """Write results.json and acceptance.json; return the acceptance path (relative)."""
        folder = self.root / "program" / "evidence" / tid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "results.json").write_text(results, encoding="utf-8")
        sha = sha256_file(folder / "results.json") if bind is None else bind
        write_json(folder / "acceptance.json", {"evidence_sha256": sha})
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
        self.assertFalse((self.root / "program").exists())

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
