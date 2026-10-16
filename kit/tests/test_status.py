import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
import status
import store
from common import KitError, read_json, write_json, write_yaml

TID = "P1-M01-S01-T01"
REGISTERED = (TID, "P1-M01-S01-T02", "A-1", "A-2", "B-1")
FULL = {"task_id": TID, "status": "in_progress", "role": "controller", "updated_at": "", "note": "",
        "evidence": [], "blocked_by": [], "history": []}


def write_register(root, task_ids=REGISTERED):
    tasks = [{"id": task_id, "title": task_id} for task_id in task_ids]
    write_yaml(Path(root) / "program" / "register.yaml", {"phases": [{"id": "P1", "milestones": [
        {"id": "P1-M01", "steps": [{"id": "P1-M01-S01", "tasks": tasks}]}]}]})


def git_repo(root) -> str:
    """Make `root` a repository with one commit; its sha."""
    def sh(*argv):
        return subprocess.run(["git", *argv], cwd=str(root), check=True, capture_output=True, text=True).stdout
    sh("init", "-q", "-b", "main")
    sh("config", "user.name", "Test")
    sh("config", "user.email", "test@example.com")
    sh("add", "-A")
    sh("commit", "-q", "-m", "init")
    return sh("rev-parse", "HEAD").strip()


def raw_record(root, tid, kind, raw: bytes) -> str:
    """Append bytes that are no usable record and point the task's pointer at them; their path."""
    appended = store.append(root, tid, kind, raw)
    entry = ({"digest": appended["digest"], "path": appended["path"], "head": "", "produced_at": ""}
             if kind == "results" else {"digest": appended["digest"], "path": appended["path"], "decision": "",
                                        "accepted_at": "", "results_digest": ""})
    store.set_entry(root, tid, kind, entry)
    return appended["path"]


def full_review():
    """What a full-tier acceptance must list (status.FULL_PLANTED): the commands QA re-ran and its own planted bugs."""
    digest = "0" * 64
    row = {"builds": True, "killed": True, "kill_reason": "test failed", "test_output_sha256": digest,
           "path": "calc.py", "find": "a + b", "replace": "a - b", "build_argv": ["make"],
           "test_argv": ["make", "test"]}
    return {"commands": [{"label": "unit", "argv": ["make", "test"], "exit": 0, "expected_exit": 0,
                          "as_expected": True, "derived": {}, "output_sha256": digest, "output_tail": ""}],
            "planted_bugs": [dict(row, id=f"qa-{number}") for number in (1, 2, 3)]}


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
            "blocked_by": [], "deviations": []}
    data.update(over)
    return data


class StatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        write_register(self.root)
        self.head = git_repo(self.root)

    # helpers
    def record_path(self, tid=TID):
        return self.root / "program" / "status" / f"{tid}.json"

    def evidence(self, tid=TID, results=None, drop=(), **over):
        """Append a results record and an accepting acceptance bound to it; return the acceptance path.

        `results` replaces the record (bytes or a non-object are appended as they are); `over` replaces
        acceptance fields and `drop` removes them. The records are appended as `pk evidence accept` would,
        without its checks, so a test can plant a bad acceptance.
        """
        data = results_record(tid, head=self.head) if results is None else results
        if isinstance(data, dict):
            digest = store.record(self.root, tid, "results", data)["digest"]
        else:
            raw_record(self.root, tid, "results", data if isinstance(data, bytes) else store.encode(data))
            digest = store.latest(store.read_index(self.root), tid, "results")["digest"]
        acceptance = {"task_id": tid, "decision": "accepted_development", "evidence_sha256": digest,
                      "source_commit": self.head, "accepted_at": "2026-01-01T00:00:00Z", "role": "qa",
                      **full_review(), "findings": [], "blocked_by": []}
        acceptance.update(over)
        return store.record(self.root, tid, "acceptance",
                            {k: v for k, v in acceptance.items() if k not in drop})["path"]

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
        self.assertEqual(status.STATUSES, ("not_started", "in_progress", "review", "done", "blocked", "parked", "merged"))
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
            ("in_progress", "merged"): {"controller"},  # the light tier's terminal move, never QA's done
        })
        self.assertEqual(status.TRANSITIONS[("review", "done")], {"qa"})  # hard rule 6: only QA sets done

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
        self.refused(f"done needs an acceptance record in program/evidence/{TID}/latest.json", TID, "done", "qa")
        acceptance = self.evidence()
        for wrong in ("controller", "implementer"):
            self.refused("qa", TID, "done", wrong)
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

    def test_done_refuses_an_acceptance_that_does_not_bind_the_latest_results_record(self):
        self.to_review()
        self.evidence()
        bound = store.latest(store.read_index(self.root), TID, "results")["digest"]
        refreshed = store.record(self.root, TID, "results", results_record(head=self.head, deviations=["again"]))
        with self.assertRaises(KitError) as ctx:  # the planted defect: a refresh after the acceptance
            status.transition(self.root, TID, "done", "qa")
        self.assertIn("acceptance does not bind the latest results record", str(ctx.exception))
        self.assertIn(bound, str(ctx.exception))
        self.assertIn(refreshed["digest"], str(ctx.exception))
        self.assertNotIn("\n", str(ctx.exception))
        self.assertEqual(status.read(self.root, TID)["status"], "review")
        self.evidence(results=results_record(head=self.head, deviations=["again"]))  # re-acceptance appends
        self.assertEqual(status.transition(self.root, TID, "done", "qa")["status"], "done")

    def test_done_refuses_a_record_whose_head_is_not_an_ancestor_of_head(self):
        self.to_review()
        self.evidence(results=results_record(head="c" * 40), source_commit="c" * 40)
        self.refused(f"head {'c' * 40} is not an ancestor of HEAD", TID, "done", "qa")

    def test_done_refuses_missing_or_malformed_acceptance_and_missing_results(self):
        self.to_review()
        self.evidence()
        raw_record(self.root, TID, "acceptance", b"not json")
        self.refused("is not valid JSON", TID, "done", "qa")
        store.record(self.root, TID, "acceptance", {"verdict": "ok"})
        self.refused("evidence_sha256", TID, "done", "qa")
        raw_record(self.root, TID, "acceptance", b'["x"]')
        self.refused("not a JSON object", TID, "done", "qa")
        self.evidence()
        results = store.latest(store.read_index(self.root), TID, "results")["path"]
        (self.root / results).chmod(0o644)
        (self.root / results).unlink()
        self.refused(f"{results} is missing", TID, "done", "qa")

    def test_done_refuses_an_acceptance_that_does_not_accept(self):
        self.to_review()
        cases = {"rejecting": dict(decision="rejected"), "made up": dict(decision="fine"),
                 "not text": dict(decision=True), "missing": dict(drop=("decision",))}
        for name, how in cases.items():
            with self.subTest(case=name):
                self.evidence(**how)
                with self.assertRaises(KitError) as ctx:
                    status.transition(self.root, TID, "done", "qa")
                self.assertIn("decision", str(ctx.exception))
                self.assertNotIn("\n", str(ctx.exception))
                self.assertEqual(status.read(self.root, TID)["status"], "review")
        self.evidence(decision="rejected")
        self.refused("accepted_development", TID, "done", "qa")
        self.evidence(task_id="P1-M01-S01-T02")
        self.refused("task_id", TID, "done", "qa")
        self.evidence(role="implementer")
        self.refused("role", TID, "done", "qa")
        self.evidence(drop=("source_commit",))
        self.refused("source_commit", TID, "done", "qa")
        self.evidence()
        self.assertEqual(status.transition(self.root, TID, "done", "qa")["status"], "done")

    def test_done_refuses_an_acceptance_of_another_commit_than_the_results_name(self):
        self.to_review()
        for wrong in ("not-a-commit", "b" * 40, "a" * 39, None, ["a" * 40]):
            with self.subTest(source_commit=wrong):
                self.evidence(source_commit=wrong)
                with self.assertRaises(KitError) as ctx:
                    status.transition(self.root, TID, "done", "qa")
                self.assertIn(": source_commit is ", str(ctx.exception))
                self.assertIn(self.head, str(ctx.exception))  # the commit the results name
                self.assertNotIn("\n", str(ctx.exception))
                self.assertEqual(status.read(self.root, TID)["status"], "review")
        self.evidence()
        self.assertEqual(status.transition(self.root, TID, "done", "qa")["status"], "done")
        self.evidence(source_commit="b" * 40)  # appended after done: validate reports it
        self.assertEqual(len(status.validate(self.root)), 1)
        self.assertIn("source_commit is ", status.validate(self.root)[0])

    def test_done_refuses_a_one_field_rejection_over_empty_results(self):
        self.to_review()
        digest = store.record(self.root, TID, "results", {})["digest"]
        store.record(self.root, TID, "acceptance", {"decision": "rejected", "evidence_sha256": digest,
                                                    "findings": [{"n": 1, "severity": "critical"}]})
        with self.assertRaises(KitError) as ctx:
            status.transition(self.root, TID, "done", "qa")
        self.assertNotIn("\n", str(ctx.exception))
        self.assertEqual(status.read(self.root, TID)["status"], "review")
        self.evidence(results={})  # a complete, accepting acceptance of a record that is not evidence
        self.refused("missing key", TID, "done", "qa")

    def test_done_refuses_results_the_gate_would_not_pass(self):
        self.to_review()
        survivor = results_record(head=self.head, planted_bugs_all_killed=False)
        survivor["planted_bugs"][0].update(killed=False, kill_reason="not killed")
        unexpected = results_record(head=self.head, all_commands_as_expected=False)
        unexpected["commands"][0].update(exit=1, as_expected=False)
        derived = results_record(head=self.head)
        derived["inputs"][f"program/status/{TID}.json"] = "0" * 64
        cases = {"planted_bugs_all_killed": survivor, "all_commands_as_expected": unexpected,
                 "task_id": results_record("P1-M01-S01-T02", head=self.head),
                 "commands": results_record(head=self.head, commands=[]),
                 "head": results_record(head="abc"), "JSON object": [],
                 "binds a derived output": derived}
        for pattern, record in cases.items():
            with self.subTest(case=pattern):
                self.evidence(results=record)
                self.refused(pattern, TID, "done", "qa")

    def test_validate_reports_a_done_record_whose_acceptance_rejects_or_whose_results_are_empty(self):
        self.to_review()
        self.evidence()
        status.transition(self.root, TID, "done", "qa")
        self.assertEqual(status.validate(self.root), [])
        self.evidence(decision="rejected")
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("decision", problems[0])
        self.evidence(results={})
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("missing key", problems[0])

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

    def test_history_entries_have_the_shape_the_schema_gives(self):
        entry = {"status": "in_progress", "role": "controller", "at": "2026-01-01T00:00:00Z"}
        cases = ((dict(entry, status="shipped"), "history entry 2: unknown status 'shipped'"),
                 (dict(entry, role="boss"), "history entry 2: unknown role 'boss'"),
                 (dict(entry, extra=1), "history entry 2: unknown field(s) extra"),
                 ([entry], "history entry 2: must be an object"))
        for bad, problem in cases:
            with self.subTest(problem=problem):
                write_json(self.record_path(), dict(FULL, history=[entry, bad]))
                with self.assertRaises(KitError) as ctx:
                    status.read(self.root, TID)
                self.assertEqual(str(ctx.exception), f"{self.record_path()}: {problem}")
                self.assertEqual(status.validate(self.root), [f"{TID}: {problem}"])

    def test_a_field_the_schema_does_not_name_is_refused(self):
        write_json(self.record_path(), dict(FULL, owner="someone", zeta=1))
        self.assertEqual(status.validate(self.root), [f"{TID}: unknown field(s) owner, zeta"])
        with self.assertRaisesRegex(KitError, r"unknown field\(s\) owner, zeta"):
            status.read(self.root, TID)

    def test_every_shape_problem_is_reported_not_only_the_first(self):
        entry = {"status": "in_progress", "role": "controller", "at": "2026-01-01T00:00:00Z"}
        record = {"task_id": 5, "status": "shipped", "role": "controller", "updated_at": "",
                  "evidence": ["a", 1, None], "blocked_by": "X-1",
                  "history": [entry, {"status": "gone", "role": 7}, "x"]}
        expected = ["missing field(s) note",
                    "unknown status 'shipped'",
                    "task_id must be text, not 5",
                    "evidence item 2 must be text, not 1",
                    "evidence item 3 must be text, not None",
                    "blocked_by must be a list",
                    "history entry 2: missing at",
                    "history entry 2: unknown status 'gone'",
                    "history entry 2: unknown role 7",
                    "history entry 3: must be an object"]
        write_json(self.record_path(), record)
        self.assertEqual(status.validate(self.root), [f"{TID}: {problem}" for problem in expected])
        for reader in (lambda: status.read(self.root, TID), lambda: status.list_all(self.root),
                       lambda: status.transition(self.root, TID, "review", "implementer")):
            with self.assertRaises(KitError) as ctx:
                reader()
            self.assertEqual(str(ctx.exception), f"{self.record_path()}: " + "; ".join(expected))  # one line

    def test_done_is_terminal(self):
        self.to_review()
        status.transition(self.root, TID, "done", "qa", evidence=[self.evidence()])
        for to in status.STATUSES:
            for role in ("controller", "implementer", "qa"):
                self.refused("terminal", TID, to, role)

    def done(self):
        self.to_review()
        return status.transition(self.root, TID, "done", "qa", evidence=[self.evidence()])

    def test_reopen_moves_a_done_task_back_to_review_with_an_audit_line(self):
        self.done()
        rec = status.reopen(self.root, TID, "late work after acceptance:  a hotfix")
        self.assertEqual((rec["status"], rec["role"]), ("review", "controller"))
        last = rec["history"][-1]
        self.assertEqual((last["status"], last["role"], last["reason"]),
                         ("review", "controller", "late work after acceptance: a hotfix"))
        self.assertEqual(read_json(self.record_path()), rec)
        self.assertEqual(status.validate(self.root), [])
        rec = status.transition(self.root, TID, "done", "qa")  # QA accepts again on the same, still bound, record
        self.assertEqual(rec["status"], "done")
        self.assertEqual([e["status"] for e in rec["history"]][-3:], ["done", "review", "done"])

    def test_qa_can_reject_a_task_already_marked_done(self):
        self.done()
        rec = status.reopen(self.root, TID, "rejected: finding 1 is critical", role="qa", reject=True)
        self.assertEqual((rec["status"], rec["role"], rec["history"][-1]["reason"]),
                         ("in_progress", "qa", "rejected: finding 1 is critical"))
        self.assertEqual(status.validate(self.root), [])
        status.transition(self.root, TID, "review", "implementer")  # and it goes round again

    def test_reopen_is_refused_without_a_reason_for_other_roles_and_for_tasks_not_done(self):
        with self.assertRaisesRegex(KitError, "not done"):
            status.reopen(self.root, TID, "why")
        self.done()
        for kwargs, pattern in (({"reason": "  "}, "needs a reason"),
                                ({"reason": "x", "role": "implementer"}, "controller or qa"),
                                ({"reason": "x", "reject": True}, "only qa rejects")):
            with self.assertRaisesRegex(KitError, pattern):
                status.reopen(self.root, TID, **kwargs)
        self.assertEqual(read_json(self.record_path())["status"], "done")

    def test_reopen_is_refused_once_main_holds_the_task_as_done(self):
        self.done()
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=T", "-c", "user.email=t@e.x", "commit", "-q",
                        "-m", "landed"], check=True)
        with self.assertRaisesRegex(KitError, "has landed"):
            status.reopen(self.root, TID, "too late")

    def test_reopen_command(self):
        self.done()
        with mock.patch.object(status, "find_root", return_value=self.root):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = pk.main(["status", "reopen", TID, "--reason", "late work"])
        self.assertEqual((rc, err.getvalue()), (0, ""))
        self.assertIn(f"{TID}: review (reopened by controller: late work)", out.getvalue())

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
        store.record(self.root, TID, "results", results_record(head=self.head, deviations=["later"]))
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(TID, problems[0])
        self.assertIn("acceptance does not bind the latest results record", problems[0])

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

    def test_evidence_path_through_a_symlink_that_leaves_the_root_is_refused(self):
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(lambda: [p.unlink() for p in outside.iterdir()] and None or outside.rmdir())
        (outside / "acceptance.json").write_text("{}", encoding="utf-8")
        (self.root / "program" / "evidence").mkdir(parents=True)
        (self.root / "program" / "evidence" / "away").symlink_to(outside, target_is_directory=True)
        (self.root / "program" / "evidence" / "file-away.json").symlink_to(outside / "acceptance.json")
        for bad in ("program/evidence/away/acceptance.json", "program/evidence/away/not-there.json",
                    "program/evidence/file-away.json", str(self.root / "program/evidence/away/acceptance.json")):
            with self.subTest(bad=bad):
                self.refused("evidence path .* leaves the project root through a symbolic link",
                             TID, "in_progress", "controller", evidence=[bad])
        self.assertFalse((self.root / "program" / "status").exists())

    def test_evidence_path_through_a_symlink_that_stays_inside_is_kept_as_written(self):
        real = self.root / "program" / "evidence" / TID
        real.mkdir(parents=True)
        (real / "notes.txt").write_text("n", encoding="utf-8")
        (self.root / "latest").symlink_to(real, target_is_directory=True)
        (self.root / "loop").symlink_to("loop")
        rec = status.transition(self.root, TID, "in_progress", "controller", evidence=["latest/notes.txt"])
        self.assertEqual(rec["evidence"], ["latest/notes.txt"])
        self.assertEqual(status.validate(self.root), [])
        try:  # a link that loops leads nowhere outside; an interpreter that cannot resolve it gives one line
            status.transition(self.root, TID, "blocked", "controller", blocked_by=["X"], evidence=["loop/x.json"])
        except KitError as exc:
            self.assertRegex(str(exc), r"\Aevidence path loop/x\.json cannot be resolved: .+\Z")

    def test_validate_reports_a_stored_evidence_path_that_leaves_the_root(self):
        outside = self.root.parent / f"{self.root.name}-elsewhere"
        outside.mkdir()
        self.addCleanup(lambda: [p.unlink() for p in outside.iterdir()] and None or outside.rmdir())
        (outside / "x.json").write_text("{}", encoding="utf-8")
        (self.root / "away").symlink_to(outside, target_is_directory=True)
        write_json(self.record_path(), dict(FULL, evidence=["away/x.json", f"../{outside.name}/x.json"]))
        problems = status.validate(self.root)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("away/x.json leaves the project root through a symbolic link", problems[0])
        self.assertIn("must not contain '..'", problems[1])

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
        self.assertEqual(set(schema["required"]) | {"tier", "fast", "ruling"}, set(rec))  # a 0.2 record has no tier
        self.assertEqual(schema["properties"]["status"]["enum"], list(status.STATUSES))
        self.assertEqual(set(schema["properties"]), set(rec) | {"inspected"})  # on a merged record only
        self.assertEqual(schema["properties"]["inspected"]["pattern"], "^" + status._SHA.pattern.replace("\\Z", "$"))


class StatusCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        write_register(self.root)
        self.head = git_repo(self.root)
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
        self.assertEqual(out.split(), [TID, "in_progress", "controller", json.loads(self.run_pk("status", "show", TID)[1])["updated_at"],
                                       "full"])
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
        for argv in (("status", "list"), ("status", "show", TID),
                     ("status", "set", TID, "review", "--role", "implementer")):
            with self.subTest(argv=argv):
                rc, out, err = self.run_pk(*argv)
                self.assertEqual((rc, out), (1, ""))
                self.assertEqual(err, f"error: {path}: task_id must be text, not 5; role must be text, not 7\n")
        rc, out, err = self.run_pk("status", "validate")  # validate gives each problem its own line
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"error: {TID}: task_id must be text, not 5\nerror: {TID}: role must be text, not 7\n")
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

    def test_a_full_tier_task_is_not_done_on_an_acceptance_that_records_no_full_tier_review(self):
        for argv in (("in_progress", "--role", "controller"), ("review", "--role", "implementer")):
            self.assertEqual(self.run_pk("status", "set", TID, *argv)[0], 0)
        digest = store.record(self.root, TID, "results", results_record(head=self.head))["digest"]
        scoped = {"task_id": TID, "decision": "accepted_development", "evidence_sha256": digest,
                  "source_commit": self.head, "accepted_at": "2026-01-01T00:00:00Z", "role": "qa", "commands": [],
                  "planted_bugs": [], "findings": [], "blocked_by": []}  # what a standard-tier review records
        store.record(self.root, TID, "acceptance", scoped)
        argv = ("status", "set", TID, "done", "--role", "qa")
        rc, out, err = self.run_pk(*argv)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("a full-tier task is accepted only on the full tier's review", err)
        self.assertIn("at least 3 planted bugs of its own (it lists 0 and 0)", err)
        two = dict(scoped, **full_review())
        two["planted_bugs"] = two["planted_bugs"][:2]
        store.record(self.root, TID, "acceptance", two)
        self.assertIn("(it lists 1 and 2)", self.run_pk(*argv)[2])
        store.record(self.root, TID, "acceptance", dict(scoped, **full_review()))
        self.assertEqual(self.run_pk(*argv), (0, f"{TID}: done\n", ""))
        # the audit: a done record whose latest acceptance is the scoped one again does not validate ...
        store.record(self.root, TID, "acceptance", scoped)
        rc, _, err = self.run_pk("status", "validate")
        self.assertEqual(rc, 1)
        self.assertIn("a full-tier task is accepted only on the full tier's review", err)
        # ... while a record written before tiers existed (no tier field) is not judged by a quota of a later day
        path = self.root / "program" / "status" / f"{TID}.json"
        old = read_json(path)
        for key in ("tier", "fast", "ruling"):
            del old[key]
        for entry in old["history"]:
            entry.pop("tier", None)
        write_json(path, old)
        self.assertEqual(self.run_pk("status", "validate"), (0, "status: ok\n", ""))

    def test_done_with_a_rejecting_acceptance_is_one_error_line(self):
        for argv in (("in_progress", "--role", "controller"), ("review", "--role", "implementer")):
            self.assertEqual(self.run_pk("status", "set", TID, *argv)[0], 0)
        digest = store.record(self.root, TID, "results", results_record(head=self.head))["digest"]
        acceptance = {"task_id": TID, "decision": "rejected", "evidence_sha256": digest,
                      "source_commit": self.head, "accepted_at": "2026-01-01T00:00:00Z", "role": "qa", "commands": [],
                      "planted_bugs": [], "findings": [{"n": 1, "severity": "critical", "location": "x", "fix": "y"}],
                      "blocked_by": []}
        rejected = store.record(self.root, TID, "acceptance", acceptance)["path"]
        argv = ("status", "set", TID, "done", "--role", "qa")
        rc, out, err = self.run_pk(*argv)
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"error: {TID}: latest acceptance {rejected}: decision is 'rejected', not "
                              "'accepted_development'\n")
        self.assertEqual(self.run_pk("status", "validate"), (0, "status: ok\n", ""))
        store.record(self.root, TID, "acceptance", dict(acceptance, decision="accepted_development", findings=[],
                                                          **full_review()))
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
        self.assertEqual(err, f"error: {TID}: unknown status 'shipped'\n")
        rec["evidence"] = [3]
        write_json(path, rec)
        write_json(path.with_name("A-1.json"), dict(FULL, task_id="A-1", status="blocked"))
        rc, out, err = self.run_pk("status", "validate")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err.splitlines(), ["error: A-1: blocked without blocked_by",
                                            f"error: {TID}: unknown status 'shipped'",
                                            f"error: {TID}: evidence item 1 must be text, not 3"])

    def test_an_evidence_path_that_leaves_through_a_symlink_is_one_error_line(self):
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / "away").symlink_to(outside, target_is_directory=True)
        rc, out, err = self.run_pk("status", "set", TID, "in_progress", "--role", "controller",
                                   "--evidence", "away/acceptance.json")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, "error: evidence path away/acceptance.json leaves the project root through a "
                              "symbolic link\n")
        self.assertFalse((self.root / "program" / "status").exists())


if __name__ == "__main__":
    unittest.main()
