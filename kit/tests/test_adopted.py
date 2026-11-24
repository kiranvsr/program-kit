"""Adoption completed (0.7.0, KA-2): the `adopted` status, references into legacy evidence, the generic import rules
and what the dashboard shows of them.

Each rule is tried against the cheapest way round it: adopted counted as done or satisfying a gate claim, a reference
outside the legacy evidence roots or with a digest nobody checks, `pk status set adopted`, a grandfathered register
entry that keeps its exemption after it changed, a new task exempt from the granularity rules, a follow-up gap above
the import point, a task in flight imported as in_progress. Without program/adoption.yaml the kit behaves as before.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adopt
import adoption
import change
import ctlstate
import dashboard
import drift
import evidence
import ext
import gate
import handoff
import progress
import register
import stale
import status
import store
import trace
from common import KitError, read_json, read_yaml, sha256_bytes, write_json, write_yaml
from test_adopt import install, source
from test_adoption import CX, task
from test_worktree import commit_all, make_repo, run_cli, sh

T = [f"P1-M01-S01-T{n:02d}" for n in range(1, 9)]
BIG = "P1-M01-S02-T01"  # grandfathered: size L, seven dependencies, a role outside the catalog, a hyphenated gate
FIRST, SECOND = "old/evidence/acceptance.json", "old/evidence/second/acceptance.json"
SECOND_BYTES = b'{"task": "second", "decision": "accepted"}\n'
PARK_NOTE = "waits for a staging environment"


def legacy_register():
    tasks = [task(ident) for ident in T]
    tasks[5]["depends_on"] = [T[0], T[1]]  # on the two adopted tasks: ready
    tasks[6]["depends_on"] = [T[2]]  # on the task that was in flight: near-ready
    big = dict(task(BIG), size="L", role="platform-agent", depends_on=T[:7], gate="G-DEP-LAND")
    return {"phases": [
        {"id": "P1", "title": "First", "milestones": [{"id": "P1-M01", "title": "m", "steps": [
            {"id": "P1-M01-S01", "title": "s", "tasks": tasks},
            {"id": "P1-M01-S02", "title": "s2", "gate": "G-DEP-LAND", "tasks": [big]}]}]},
        {"id": "CX", "title": "Legacy", "milestones": [{"id": "CX-M01", "title": "m", "steps": [
            {"id": "CX-M01-S01", "title": "s", "tasks": [task(CX)]}]}]}]}


def legacy_statuses():
    return [
        {"task_id": T[0], "status": "done", "actor": "qa-agent", "updated_at": "2026-01-01T00:00:00Z",
         "references": [{"path": FIRST}], "note": "accepted in development"},
        {"task_id": T[1], "status": "done", "actor": "qa-agent", "updated_at": "2026-01-02T00:00:00Z",
         "references": [{"path": SECOND}, {"path": FIRST}], "note": ""},
        {"task_id": T[2], "status": "in_progress", "actor": "implementer", "updated_at": "2026-01-03T00:00:00Z",
         "references": [], "note": "half done"},
        {"task_id": T[3], "status": "not_started", "actor": "", "updated_at": "", "references": [], "note": PARK_NOTE},
        {"task_id": T[4], "status": "not_started", "actor": "", "updated_at": ""},
    ]


def ka2_source(**overrides):
    followups = source()["followups"]
    followups["closed"] = followups["closed"][:1]  # F-0002 is not in the source: a gap below next (F-0004)
    data = source(register=legacy_register(), statuses=legacy_statuses(), followups=followups,
                  costs=[{"at": "2026-01-01T00:00:00Z", "task": T[0], "role": "qa", "model": "m-1", "tokens": 9,
                          "tool_calls": 1, "seconds": 2}])
    data.update(overrides)
    return data


def install_legacy(root: Path) -> None:
    install(root)
    path = root / SECOND
    path.parent.mkdir(parents=True)
    path.write_bytes(SECOND_BYTES)
    commit_all(root, "the second acceptance")


class _Template:
    """One adopted repository, built once per process and copied for each test that changes it."""
    path: Path | None = None
    keep = None

    @classmethod
    def get(cls) -> Path:
        if cls.path is None:
            case = unittest.TestCase()
            root = make_repo(case)
            cls.keep = case  # its cleanups run at exit (the temporary directory goes with the process)
            install_legacy(root)
            src = root.parent / f"{root.name}-source.json"
            src.write_text(json.dumps(ka2_source(), indent=1))
            code, out, err = run_cli("adopt", root, "adopt", str(src))
            assert code == 0, (out, err)
            commit_all(root, "adopt")
            cls.path = root
        return cls.path


def tearDownModule():
    if _Template.keep is not None:
        _Template.keep.doCleanups()


class AdoptedBase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(ext.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "repo"
        shutil.copytree(_Template.get(), self.root, symlinks=True)
        self.record = adoption.load(self.root)

    def status_cli(self, *argv):
        return run_cli("status", self.root, "status", *argv)

    def write_record(self, task_id, record):
        write_json(self.root / "program" / "status" / f"{task_id}.json", record)

    def rec(self, task_id):
        return read_json(self.root / "program" / "status" / f"{task_id}.json")


# --- the import --------------------------------------------------------------------------------------------------

class ImportTests(AdoptedBase):
    def test_a_done_task_is_adopted_with_its_references_digests_at_head(self):
        first = sha256_bytes((self.root / FIRST).read_bytes())
        record = self.rec(T[0])
        self.assertEqual((record["status"], record["legacy_status"], record["legacy_actor"],
                          record["legacy_updated_at"], record["note"]),
                         ("adopted", "done", "qa-agent", "2026-01-01T00:00:00Z", "accepted in development"))
        self.assertEqual(record["references"], [{"path": FIRST, "sha256": first}])
        self.assertEqual(self.rec(T[1])["references"], [{"path": SECOND, "sha256": sha256_bytes(SECOND_BYTES)},
                                                        {"path": FIRST, "sha256": first}])
        self.assertEqual(record["history"], [{"status": "adopted", "role": "controller",
                                              "at": self.record["adopted_at"]}])
        self.assertNotIn("tier", record)  # it went through no kit gate

    def test_a_task_in_flight_is_imported_not_started_with_a_re_dispatch_follow_up(self):
        record = self.rec(T[2])
        self.assertEqual((record["status"], record["legacy_status"], record["role"], record["history"]),
                         ("not_started", "in_progress", "", []))
        state = ctlstate.load(self.root)
        opened = {item["id"]: item for item in state["followups"]}
        self.assertEqual((opened["F-0004"]["area"], opened["F-0004"]["item"]),
                         ("adoption", f"re-dispatch {T[2]} (was in_progress at adoption)"))
        self.assertEqual(opened["F-0005"]["item"], f"{T[3]} was parked at adoption: {PARK_NOTE}")
        self.assertEqual(state["followups_issued"], 5)
        self.assertFalse((self.root / "program" / "status" / f"{T[3]}.json").exists())  # a parked task: no record
        self.assertFalse((self.root / "program" / "status" / f"{T[4]}.json").exists())
        status.transition(self.root, T[2], "in_progress", "controller")  # it re-enters the loop by a dispatch
        self.assertEqual(self.rec(T[2])["legacy_status"], "in_progress")  # where it came from stays on the record

    def test_adoption_records_the_gaps_and_the_grandfathered_entries(self):
        self.assertEqual(self.record["followup_gaps"], ["F-0002"])
        self.assertEqual(self.record["legacy_register"],
                         {"tasks": [BIG], "gates": ["G-DEP-LAND"], "roles": ["platform-agent"]})
        self.assertEqual(adoption.followup_gaps(self.root), frozenset({2}))

    def test_the_kit_reads_the_import_and_adopted_is_never_done(self):
        self.assertEqual(register.problems_in(self.root), [])
        self.assertEqual(status.validate(self.root), [])
        code, out, err = self.status_cli("list")
        self.assertIn(f"{T[0]}\tadopted\tcontroller\t{self.record['adopted_at']}\t-", out.splitlines())
        block = handoff.render(self.root)
        self.assertIn("- done: 0\n", block)
        self.assertIn("- adopted: 2\n", block)
        self.assertIn(f"F-0005 adoption: {T[3]} was parked at adoption", block)
        ok, detail = drift._evidence(self.root)
        self.assertEqual((ok, detail), (True, "every done task has an acceptance record (0 done)"))

    def test_an_adopted_dependency_is_satisfied_as_done_satisfies_it(self):
        found = progress.ready(self.root)
        self.assertIn(T[5], [item["id"] for item in found["ready"]])
        self.assertIn({"id": T[6], "title": f"task {T[6]}", "waiting_on": T[2], "waiting_status": "not_started"},
                      found["near_ready"])
        self.assertNotIn(BIG, [item["id"] for item in found["ready"]])  # T03 to T07 are not finished


class SourceStatusTests(unittest.TestCase):
    """What validate_source refuses in the statuses, before anything is written."""

    @classmethod
    def setUpClass(cls):
        cls._case = unittest.TestCase()
        cls.root = make_repo(cls._case)
        install_legacy(cls.root)
        (cls.root / "old" / "outside.json").write_text("{}\n")
        (cls.root / "old" / "outside").mkdir()
        (cls.root / "old" / "outside" / "a.json").write_text("{}\n")
        (cls.root / "old" / "evidence" / "link.json").symlink_to("../outside.json")  # a linked file
        (cls.root / "old" / "evidence" / "linked").symlink_to("../outside")  # a linked directory
        sh(cls.root, "git", "add", "old")
        sh(cls.root, "git", "commit", "-q", "-m", "a file outside the evidence roots, and links to it")

    @classmethod
    def tearDownClass(cls):
        cls._case.doCleanups()

    def problems(self, statuses=None, **overrides):
        data = ka2_source(**overrides) if statuses is None else ka2_source(statuses=statuses, **overrides)
        return adopt.validate_source(self.root, json.dumps(data).encode())[1]

    def one(self, **entry):
        return [dict({"task_id": T[0], "status": "done", "actor": "qa", "updated_at": "t", "references": []}, **entry)]

    def test_the_fixture_is_valid(self):
        self.assertEqual(self.problems(), [])

    def test_a_reference_outside_the_legacy_evidence_roots_is_refused(self):
        self.assertIn("statuses[1].references[1]: old/outside.json is not under legacy_evidence_roots (old/evidence/)",
                      self.problems(self.one(references=[{"path": "old/outside.json"}])))

    def test_a_reference_that_is_not_committed_is_refused(self):
        self.assertIn("statuses[1].references[1]: old/evidence/none.json is not a file committed at HEAD",
                      self.problems(self.one(references=[{"path": "old/evidence/none.json"}])))
        self.assertIn("statuses[1].references[2]: old/evidence/acceptance.json is listed twice for " + T[0],
                      self.problems(self.one(references=[{"path": FIRST}, {"path": FIRST}])))

    def test_a_symbolic_link_or_a_path_below_one_is_refused(self):
        found = self.problems(self.one(references=[{"path": "old/evidence/link.json"},
                                                   {"path": "old/evidence/linked/a.json"}]))
        self.assertIn("statuses[1].references[1]: old/evidence/link.json is a symbolic link at HEAD: a reference "
                      "must be a regular file (a link's digest covers its target's name, not the file it points to)",
                      found)
        self.assertIn("statuses[1].references[2]: old/evidence/linked/a.json lies below old/evidence/linked, a "
                      "symbolic link at HEAD: a reference must be a regular file reached through directories", found)

    def test_references_belong_to_a_done_task(self):
        found = self.problems(self.one(status="in_progress", references=[{"path": FIRST}]))
        self.assertIn(f"statuses[1]: references belong to a done task (adopted); {T[0]} is in_progress", found)

    def test_only_a_not_started_entry_may_leave_actor_and_time_empty(self):
        self.assertIn("statuses[1]: actor must be non-empty text", self.problems(self.one(actor="")))
        self.assertIn("statuses[1]: updated_at must be non-empty text",
                      self.problems(self.one(status="blocked", updated_at="")))
        self.assertEqual(self.problems(self.one(status="not_started", actor="", updated_at="")), [])

    def test_a_follow_up_at_or_past_next_is_still_refused(self):
        followups = ka2_source()["followups"]
        followups["open"].append({"id": "F-0004", "area": "a", "item": "b"})
        self.assertIn("followups: F-0004 at or past followups.next F-0004", self.problems(followups=followups))

    def test_the_plan_imports_in_flight_states_as_not_started(self):
        plan, found = adopt.validate_source(self.root, json.dumps(ka2_source()).encode())
        self.assertEqual(found, [])
        stamp = "2026-11-24T00:00:00Z"
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target)
        (target / "program" / "status").mkdir(parents=True)
        for state in ("in_progress", "review", "blocked"):
            plan["statuses"] = [dict(legacy_statuses()[2], status=state)]
            adopt._write_statuses(target, plan, stamp)
            written = read_json(target / "program" / "status" / f"{T[2]}.json")
            self.assertEqual((written["status"], written["legacy_status"]), ("not_started", state))
        self.assertIn("statuses: 5: 2 adopted (3 references), 1 in flight imported as not_started with a re-dispatch "
                      "follow-up, 2 not_started (1 with a park note, kept as a follow-up)",
                      adopt._summary(adopt.validate_source(self.root, json.dumps(ka2_source()).encode())[0]))


# --- the adopted status --------------------------------------------------------------------------------------------

class AdoptedStatusTests(AdoptedBase):
    def test_pk_status_set_refuses_adopted_as_a_target_and_as_a_source(self):
        for task_id in (T[4], T[2]):
            with self.assertRaisesRegex(KitError, "adopted is written only by pk adopt"):
                status.transition(self.root, task_id, "adopted", "controller")
        code, out, err = self.status_cli("set", T[5], "adopted", "--role", "qa")
        self.assertEqual(code, 1)
        self.assertIn("adopted is written only by pk adopt", err)
        before = (self.root / "program" / "status" / f"{T[0]}.json").read_bytes()
        for to, role in (("in_progress", "controller"), ("done", "qa"), ("review", "implementer")):
            with self.assertRaisesRegex(KitError, "adopted is terminal; new work on it starts with: pk status reopen"):
                status.transition(self.root, T[0], to, role)
        with self.assertRaisesRegex(KitError, "is adopted and that is terminal"):
            status.set_tier(self.root, T[0], "standard", "R6")
        self.assertEqual((self.root / "program" / "status" / f"{T[0]}.json").read_bytes(), before)

    def test_reopen_moves_an_adopted_task_to_in_progress_with_an_audit_line(self):
        with self.assertRaisesRegex(KitError, "reopened by the controller"):
            status.reopen(self.root, T[0], "late work", role="qa")
        record = status.reopen(self.root, T[0], "the old acceptance missed  the error path")
        self.assertEqual((record["status"], record["tier"], record["fast"], record["note"]),
                         ("in_progress", "full", False, "the old acceptance missed the error path"))
        self.assertEqual(record["history"][-1], {"status": "in_progress", "role": "controller",
                                                 "at": record["updated_at"], "tier": "full",
                                                 "reason": "the old acceptance missed the error path"})
        self.assertEqual(record["references"], self.rec(T[1])["references"][1:])  # kept: where it came from
        self.assertEqual(status.validate(self.root), [])
        status.transition(self.root, T[0], "review", "implementer")  # now the kit's whole loop: done needs QA
        with self.assertRaisesRegex(KitError, "done needs an acceptance record"):
            status.transition(self.root, T[0], "done", "qa")
        self.assertNotIn(T[5], [item["id"] for item in progress.ready(self.root)["ready"]])  # no longer satisfied

    def test_validate_checks_every_reference_at_adopted_commit(self):
        path = self.root / "program" / "status" / f"{T[1]}.json"
        good = read_json(path)
        (self.root / SECOND).write_bytes(b"changed after adoption\n")
        commit_all(self.root, "the legacy file moves on")
        self.assertEqual(status.validate(self.root), [])  # checked at adopted_commit, not at HEAD
        bad = json.loads(json.dumps(good))
        bad["references"][0]["sha256"] = "0" * 64
        write_json(path, bad)
        self.assertEqual(status.validate(self.root), [
            f"{T[1]}: reference 1: {SECOND} at {self.record['adopted_commit'][:12]} has sha256 "
            f"{sha256_bytes(SECOND_BYTES)}, not the recorded {'0' * 64}"])
        bad["references"][0] = {"path": "old/evidence/other.json", "sha256": good["references"][0]["sha256"]}
        write_json(path, bad)
        self.assertIn("are not the paths the frozen source lists", status.validate(self.root)[0])
        write_json(path, good)
        data = read_yaml(self.root / adoption.ADOPTION_FILE)
        write_yaml(self.root / adoption.ADOPTION_FILE, dict(data, legacy_evidence_roots=["old/other/"]))
        found = status.validate(self.root)
        self.assertIn(f"{T[1]}: reference 1: {SECOND} is not under the legacy evidence roots "
                      "(old/other/)", found)

    def test_pk_stale_reports_an_adopted_task_s_drifted_reference_as_information(self):
        report = stale.drift_report(self.root)["adopted"]
        self.assertEqual((report["since"], report["checked"], report["drifted"], report["unreadable"]),
                         (self.record["adopted_commit"], 2, 0, []))
        self.assertEqual(run_cli("stale", self.root, "stale", "--strict")[0], 0)
        (self.root / SECOND).write_bytes(b"changed after adoption\n")
        commit_all(self.root, "the legacy file moves on")
        self.assertEqual(status.validate(self.root), [])  # still true at adopted_commit
        report = stale.drift_report(self.root)["adopted"]
        self.assertEqual(report["tasks"], [
            {"task_id": T[0], "status": "adopted", "path": f"program/status/{T[0]}.json", "state": "fresh",
             "changed_references": []},
            {"task_id": T[1], "status": "adopted", "path": f"program/status/{T[1]}.json", "state": "drifted",
             "changed_references": [SECOND]}])
        self.assertEqual(report["drifted"], 1)
        code, out, err = run_cli("stale", self.root, "stale")
        self.assertEqual(code, 0)  # information
        self.assertEqual(json.loads(out)["adopted"]["tasks"][1]["changed_references"], [SECOND])
        self.assertEqual(run_cli("stale", self.root, "stale", "--strict")[0], 1)
        self.assertNotIn("adopted", stale.drift_report(self.root, references=False))  # the gate's own drift step
        (self.root / SECOND).unlink()
        (self.root / SECOND).symlink_to("../acceptance.json")  # a link now: never fresh, whatever its text
        commit_all(self.root, "the legacy file becomes a link")
        self.assertEqual(stale.drift_report(self.root)["adopted"]["tasks"][1]["changed_references"], [SECOND])
        status.reopen(self.root, T[0], "new work")  # a reopened record keeps its references, so it is reported too
        (self.root / FIRST).write_text('{"moved": "on"}\n')
        commit_all(self.root, "the first legacy file moves on")
        rows = stale.drift_report(self.root)["adopted"]["tasks"]
        self.assertEqual([(row["task_id"], row["status"], row["changed_references"]) for row in rows],
                         [(T[0], "in_progress", [FIRST]), (T[1], "adopted", [FIRST, SECOND])])

    def test_an_adopted_record_written_by_hand_is_refused(self):
        forged = dict(self.rec(T[0]), task_id=T[4], references=[])
        self.write_record(T[4], forged)
        self.assertIn(f"{T[4]}: adopted with references, but the frozen source does not list {T[4]} as done",
                      status.validate(self.root)[0])
        self.write_record(T[4], dict(forged, history=[], references=[]))
        self.assertIn("an adopted record's history is the one entry pk adopt wrote", status.validate(self.root)[0])
        without = dict(self.rec(T[0]))
        del without["references"]
        self.write_record(T[0], without)
        self.assertIn("it carries legacy_status done and its references", "; ".join(status.validate(self.root)))

    def test_without_adoption_an_adopted_record_does_not_validate(self):
        (self.root / adoption.ADOPTION_FILE).unlink()
        self.assertIn(f"{T[0]}: no {adoption.ADOPTION_FILE}: only pk adopt writes adopted records",
                      status.validate(self.root)[0])

    def test_an_adopted_task_evidences_nothing_in_a_gate_claim(self):
        write_yaml(self.root / "program" / "gates.yaml", {"gates": {"G-P1": {
            "title": "t", "claims": ["c"], "environment": "dev",
            "records": [{"task": T[0], "acceptance": "a"}, {"task": T[5], "acceptance": "a"}]}}})
        write_yaml(self.root / "program" / "environments.yaml", {"environments": {"dev": {"exists": True}}})
        commit_all(self.root, "gates")
        docs = gate._expected_claims(self.root, "G-P1")
        adopted = docs[f"{T[0]}.json"]
        self.assertEqual((adopted["environment"], adopted["adopted"], adopted["references"]),
                         ({"dev": "not_evaluated"}, "adopted; see references", self.rec(T[0])["references"]))
        self.assertNotIn("adopted", docs[f"{T[5]}.json"])
        gate.claims(self.root, "G-P1")
        self.assertEqual(gate.validate_claims(self.root, "G-P1"), [])

    def test_the_impact_of_a_change_lists_adopted_tasks_on_their_own(self):
        state = {r["task_id"]: r["status"] for r in status.list_all(self.root)}
        self.assertEqual(trace._BUCKET[state[T[0]]], "adopted")
        write_yaml(self.root / "program" / "traceability.yaml", {"journeys": {"J1": {}}, "requirements": {},
                                                                 "components": {}, "gates": {}, "tasks": {}})
        data = read_yaml(self.root / "program" / "register.yaml")
        data["phases"][0]["milestones"][0]["steps"][0]["tasks"][0]["closes"] = ["J1"]
        write_yaml(self.root / "program" / "register.yaml", data)
        found = trace.impact(self.root, ["J1"])["tasks"]
        self.assertEqual((found["adopted"], found["done"]), ([T[0]], []))


class WithoutAdoptionTests(unittest.TestCase):
    """Without program/adoption.yaml nothing of this batch shows."""

    def test_the_hand_off_trace_and_dashboard_are_as_before(self):
        root = make_repo(self)
        program = root / "program"
        program.mkdir()
        write_yaml(program / "register.yaml", {"phases": [{"id": "P1", "title": "p", "milestones": [
            {"id": "P1-M01", "title": "m", "steps": [{"id": "P1-M01-S01", "title": "s", "tasks": [task(T[0])]}]}]}]})
        shutil.copy(Path(adopt.KIT_DIR) / "templates" / "HANDOFF.md", program / "HANDOFF.md")
        commit_all(root, "program")
        self.assertNotIn("adopted", handoff.render(root))
        page, summary = dashboard.render(root)
        self.assertNotIn("adopted", page.replace(".s-adopted", "").replace("--adopted", ""))
        self.assertNotIn("References (", page)
        self.assertNotIn("adopted", summary)
        self.assertEqual(ctlstate.parse({"followups": [{"id": "F-0001", "area": "a", "item": "b"}]})["followups_issued"],
                         1)
        with self.assertRaisesRegex(KitError, "only an adopted programme cites legacy evidence"):
            evidence.Evidence(root, T[0]).reference("README.md")
        self.assertNotIn("adopted", stale.drift_report(root))


# --- the register: shape for what was adopted, granularity for everything after ----------------------------------

class GrandfatherTests(AdoptedBase):
    def edit_register(self, change_task):
        data = read_yaml(self.root / "program" / "register.yaml")
        step = data["phases"][0]["milestones"][0]["steps"][1]
        change_task(step)
        write_yaml(self.root / "program" / "register.yaml", data)

    def test_an_unchanged_grandfathered_entry_is_held_to_shape_only(self):
        self.assertEqual(register.problems_in(self.root), [])
        self.edit_register(lambda step: step["tasks"][0].update(depends_on=[*T[:7], "P1-M01-S01-T99"]))
        self.assertIn(f"{BIG}: depends_on P1-M01-S01-T99 does not exist", register.problems_in(self.root))

    def test_a_changed_grandfathered_task_gets_every_rule(self):
        self.edit_register(lambda step: step["tasks"][0].update(title="a new title"))
        found = register.problems_in(self.root)
        changed = register.CHANGED
        self.assertEqual(found, [
            f"{BIG}: gate 'G-DEP-LAND' must be {register.GATE_SHAPE}, or null{changed}",
            f"{BIG}: size L is not allowed on a task; split into steps{changed}",
            f"{BIG}: unknown role 'platform-agent'{changed}",
            f"{BIG}: depends_on has 7 entries; at most 6 are allowed{changed}"])

    def test_a_change_request_re_checks_the_grandfathered_task(self):
        write_yaml(self.root / "program" / "traceability.yaml", {"journeys": {"J1": {}}, "requirements": {},
                                                                 "components": {}, "gates": {}, "tasks": {}})
        cr = change.create(self.root, "Retitle the big task", "It reads better.", "the owner")
        change.impact(self.root, cr, ["J1"])
        change.approve(self.root, cr, "the owner")
        self.edit_register(lambda step: step["tasks"][0].update(title="a new title"))
        with self.assertRaisesRegex(KitError, f"{cr} not applied: program/register.yaml has 4 problem"):
            change.apply(self.root, cr, "retitled")

    def test_a_task_added_after_adoption_is_held_to_granularity(self):
        new = dict(task("P1-M01-S02-T02"), size="L", role="platform-agent", depends_on=T[:7], gate="G-DEP-LAND")
        self.edit_register(lambda step: step["tasks"].append(new))
        found = [line for line in register.problems_in(self.root) if line.startswith("P1-M01-S02-T02")]
        self.assertEqual(found, [
            f"P1-M01-S02-T02: gate 'G-DEP-LAND' must be {register.GATE_SHAPE}, or null",
            "P1-M01-S02-T02: size L is not allowed on a task; split into steps",
            "P1-M01-S02-T02: unknown role 'platform-agent'",
            "P1-M01-S02-T02: depends_on has 7 entries; at most 6 are allowed"])

    def test_the_grandfathered_step_gate_stays_and_a_changed_step_loses_it(self):
        self.edit_register(lambda step: step.update(title="a new step title"))
        self.assertEqual(register.problems_in(self.root),
                         [f"P1-M01-S02: gate 'G-DEP-LAND' must be {register.GATE_SHAPE}, or null{register.CHANGED}"])


class FollowupGapTests(AdoptedBase):
    def test_exactly_the_recorded_gaps_are_accepted(self):
        self.assertEqual(ctlstate.load(self.root)["followups_issued"], 5)
        state = read_json(self.root / ctlstate.STATE_FILE)
        state["followups"] = [item for item in state["followups"] if item["id"] != "F-0005"]  # a gap above the point
        write_json(self.root / ctlstate.STATE_FILE, state)
        with self.assertRaisesRegex(KitError, r"follow-up\(s\) F-0005 are in neither the open nor the closed list"):
            ctlstate.load(self.root)

    def test_a_listed_gap_that_is_not_a_gap_is_refused(self):
        data = read_yaml(self.root / adoption.ADOPTION_FILE)
        write_yaml(self.root / adoption.ADOPTION_FILE, dict(data, followup_gaps=["F-0001", "F-0002"]))
        with self.assertRaisesRegex(KitError, "lists follow-up gaps that are not gaps \\(F-0001\\)"):
            ctlstate.load(self.root)
        write_yaml(self.root / adoption.ADOPTION_FILE, dict(data, followup_gaps=["F-0002", "F-0009"]))
        with self.assertRaisesRegex(KitError, "past the last one issued"):
            ctlstate.load(self.root)
        write_yaml(self.root / adoption.ADOPTION_FILE, dict(data, followup_gaps=["F-0002", "F-0002"]))
        with self.assertRaisesRegex(KitError, "followup_gaps must be a list of follow-up IDs"):
            adoption.load(self.root)

    def test_doctor_and_validate_derive_the_gaps_from_the_frozen_source(self):
        self.assertEqual((adoption.problems(self.root), status.validate(self.root)), ([], []))
        state = read_json(self.root / ctlstate.STATE_FILE)
        state["followups"] = [item for item in state["followups"] if item["id"] != "F-0003"]  # deleted, not closed
        write_json(self.root / ctlstate.STATE_FILE, state)
        data = read_yaml(self.root / adoption.ADOPTION_FILE)
        write_yaml(self.root / adoption.ADOPTION_FILE, dict(data, followup_gaps=["F-0002", "F-0003"]))  # hidden
        ctlstate.load(self.root)  # the state alone cannot tell: it trusts the listed gaps
        said = (f"{adoption.ADOPTION_FILE}: followup_gaps [F-0002, F-0003] are not the gaps of the frozen source "
                f"{adoption.SOURCE_COPY} [F-0002]: the gaps are fixed at adoption; restore {adoption.ADOPTION_FILE} "
                "from git")
        self.assertEqual(adoption.problems(self.root), [said])
        self.assertEqual(status.validate(self.root), [said])
        code, out, err = run_cli("pk", self.root, "doctor")
        self.assertEqual(code, 1)
        self.assertIn("error: " + said, err)
        write_yaml(self.root / adoption.ADOPTION_FILE, dict(data, followup_gaps=[]))  # a gap dropped is refused too
        self.assertIn("followup_gaps [] are not the gaps", adoption.problems(self.root)[0])
        write_yaml(self.root / adoption.ADOPTION_FILE, data)
        (self.root / adoption.SOURCE_COPY).write_text("{}")  # the source itself changed: reported once, as changed
        found = adoption.problems(self.root)
        self.assertEqual(len(found), 1)
        self.assertIn("no longer matches source_sha256", found[0])
        self.assertEqual(status.validate(self.root)[-1], f"{adoption.ADOPTION_FILE}: followup_gaps cannot be checked: "
                         f"{adoption.SOURCE_COPY} no longer matches source_sha256; restore it from git")

    def test_the_next_follow_up_is_numbered_after_the_import(self):
        state = ctlstate.load(self.root)
        self.assertEqual(ctlstate.next_followup_id(state), "F-0006")


# --- references from new work -----------------------------------------------------------------------------------

def rows(root: Path) -> list[dict]:
    """A self-test row and one killed row: what `write` needs, without running a planted bug."""
    base = {"path": "README.md", "find": "a", "replace": "b", "build_argv": None, "test_argv": ["true"]}
    return [dict(base, id=evidence.SELF_TEST_ID, builds=False, killed=False, kill_reason="build failed",
                 test_output_sha256=""),
            dict(base, id="bug", builds=True, killed=True, kill_reason="test failed", test_output_sha256="a" * 64)]


class ReferenceTests(AdoptedBase):
    def run_record(self, *references):
        run = evidence.Evidence(self.root, T[5])
        run.hash_inputs(["README.md"])
        run.run_command("ok", ["git", "--version"])
        run.planted_bug_rows = rows(self.root)
        for path in references:
            run.reference(path)
        return run

    def test_a_reference_is_written_beside_the_inputs_never_among_them(self):
        run = self.run_record(FIRST)
        self.assertEqual(run.reference(FIRST), {"path": FIRST, "sha256": sha256_bytes((self.root / FIRST).read_bytes())})
        written = run.write()
        commit_all(self.root, "the record")
        data = read_json(self.root / written["path"])
        self.assertEqual(data["references"], [{"path": FIRST, "sha256": sha256_bytes((self.root / FIRST).read_bytes())}])
        self.assertEqual(list(store.input_digests(data["inputs"])), ["README.md"])
        self.assertEqual(evidence.gate_problems(data, T[5], root=self.root), [])
        self.assertNotIn("references", read_json(self.root / self.run_record().write()["path"]))  # none, no key

    def test_a_reference_outside_the_roots_or_not_committed_is_refused(self):
        run = evidence.Evidence(self.root, T[5])
        with self.assertRaisesRegex(KitError, "is not under the legacy evidence roots \\(old/evidence/\\)"):
            run.reference("README.md")
        with self.assertRaisesRegex(KitError, "is not a file committed at HEAD"):
            run.reference("old/evidence/none.json")
        with self.assertRaisesRegex(KitError, "must not contain"):
            run.reference("old/evidence/../RULINGS.md")

    def test_a_symbolic_link_or_a_path_below_one_is_refused(self):
        (self.root / "secret.txt").write_text("outside the roots\n")
        (self.root / "elsewhere").mkdir()
        (self.root / "elsewhere" / "a.json").write_text("{}\n")
        (self.root / "old" / "evidence" / "link.json").symlink_to("../../secret.txt")
        (self.root / "old" / "evidence" / "linked").symlink_to("../../elsewhere")
        commit_all(self.root, "links under the evidence roots")
        run = evidence.Evidence(self.root, T[5])
        with self.assertRaisesRegex(KitError, "reference old/evidence/link.json is a symbolic link at HEAD: a "
                                              "reference must be a regular file"):
            run.reference("old/evidence/link.json")
        with self.assertRaisesRegex(KitError, "reference old/evidence/linked/a.json lies below old/evidence/linked, a "
                                              "symbolic link at HEAD"):
            run.reference("old/evidence/linked/a.json")
        self.assertEqual(run.references, [])
        data = read_json(self.root / self.run_record(FIRST).write()["path"])
        commit_all(self.root, "a record")
        link = sha256_bytes(b"../../secret.txt")  # the link's text: what a blob digest of it would cover
        crafted = dict(data, references=[{"path": "old/evidence/link.json", "sha256": link}])
        self.assertEqual(evidence.gate_problems(crafted, T[5], root=self.root),
                         [f"references[1]: old/evidence/link.json is a symbolic link at {data['head'][:12]}: a "
                          "reference must be a regular file (a link's digest covers its target's name, not the file "
                          "it points to)"])

    def test_the_gate_checks_each_reference_at_the_record_s_head(self):
        run = self.run_record(FIRST)
        data = read_json(self.root / run.write()["path"])
        (self.root / FIRST).write_text('{"moved": "on"}\n')
        commit_all(self.root, "the legacy file changes after the record")
        self.assertEqual(evidence.gate_problems(data, T[5], root=self.root), [])  # at head, not at HEAD
        inputs = store.input_digests(data["inputs"])
        self.assertEqual(stale.changed_inputs(self.root, "HEAD", inputs), {})  # never drift-binding
        tampered = dict(data, references=[{"path": FIRST, "sha256": "0" * 64}])
        self.assertIn(f"references[1]: {FIRST} at {data['head'][:12]} has sha256",
                      evidence.gate_problems(tampered, T[5], root=self.root)[0])
        outside = dict(data, references=[{"path": "README.md", "sha256": data["references"][0]["sha256"]}])
        self.assertEqual(evidence.gate_problems(outside, T[5], root=self.root),
                         ["references[1]: README.md is not under the legacy evidence roots (old/evidence/)"])
        self.assertIn("references: must be a non-empty list of {path, sha256}",
                      evidence.validate_results(dict(data, references=[]))[0])
        (self.root / adoption.ADOPTION_FILE).unlink()
        self.assertIn("this programme has no legacy_evidence_roots",
                      evidence.gate_problems(data, T[5], root=self.root)[0])

    def test_history_and_the_dashboard_show_references_and_a_resolver_describes_them(self):
        run = self.run_record(FIRST, SECOND)
        written = run.write()
        commit_all(self.root, "a new record citing legacy evidence")
        code, out, err = run_cli("evidence", self.root, "evidence", "history", T[5])
        lines = out.splitlines()
        self.assertEqual(lines[1:], [f"  reference {FIRST} sha256 {sha256_bytes((self.root / FIRST).read_bytes())}",
                                     f"  reference {SECOND} sha256 {sha256_bytes(SECOND_BYTES)}"])
        (self.root / "program" / "extensions").mkdir()
        (self.root / "program" / "extensions.yaml").write_text("api: 1\nmodules:\n  - name: legacy\n")
        (self.root / "program" / "extensions" / "legacy.py").write_text(
            "import json\n\n\ndef resolve(path, data):\n"
            "    said = json.loads(data)\n"
            "    return {'kind': 'acceptance', 'task_id': said.get('task', ''), 'summary': 'legacy QA'}\n\n\n"
            "def register(api):\n    api.reference_resolver(resolve)\n")
        commit_all(self.root, "a resolver")
        ext.close()
        code, out, err = run_cli("evidence", self.root, "evidence", "history", T[5])
        self.assertIn(f"  reference {SECOND} sha256 {sha256_bytes(SECOND_BYTES)} (acceptance second: legacy QA)",
                      out.splitlines())
        page, summary = dashboard.render(self.root)
        self.assertIn("<h2>References (5)</h2>", page)  # three of the adopted tasks, two of the new record
        self.assertIn(f"<code>{T[5]}</code> <span class=meta>results {written['path']}</span> {SECOND} sha256 "
                      f"{sha256_bytes(SECOND_BYTES)} (acceptance second: legacy QA)", page)
        self.assertIn(f"<code>{T[0]}</code> <span class=meta>adopted</span> {FIRST}", page)


class DashboardTests(AdoptedBase):
    def test_adopted_tasks_are_their_own_state_and_follow_ups_are_newest_first_and_counted(self):
        page, summary = dashboard.render(self.root)
        self.assertEqual(summary["adopted"], 2)
        self.assertIn('<div class="n">2</div><div class="l">adopted (before the kit)</div>', page)
        self.assertIn('<div class="n">0</div><div class="l">done or merged</div>', page)
        self.assertIn('<span class="chip s-adopted">adopted</span> 2', page)
        self.assertIn("0 of 9 done, 2 adopted", page)
        self.assertIn('<td><span class="chip s-adopted">adopted</span></td><td></td>', page)  # no kit tier
        self.assertIn("<h2>Open follow-ups (3, newest first)</h2>", page)
        order = [page.index(f"<code>F-000{n}</code>") for n in (5, 4, 3)]
        self.assertEqual(order, sorted(order))


# --- the gate tries a branch's extensions before they reach main --------------------------------------------------

class ExtCheckStepTests(unittest.TestCase):
    BRANCH = f"task/{T[0]}"

    def setUp(self):
        self.addCleanup(ext.close)
        self.root = make_repo(self)
        (self.root / "program" / "extensions").mkdir(parents=True)
        (self.root / "program" / "extensions.yaml").write_text("api: 1\nmodules:\n  - name: mine\n")
        (self.root / "program" / "extensions" / "mine.py").write_text("def register(api):\n    pass\n")
        commit_all(self.root, "an extension")
        sh(self.root, "git", "branch", self.BRANCH)
        self.wt = self.root / ".worktrees" / "t"
        sh(self.root, "git", "worktree", "add", "-q", str(self.wt), self.BRANCH)
        self.addCleanup(sh, self.root, "git", "worktree", "remove", "--force", str(self.wt))

    def step(self):
        trial = gate.trial_merge(self.root, self.BRANCH)
        out = []
        try:
            with mock.patch.object(gate, "_out", side_effect=lambda text, stream=None: out.append(text)):
                ok = gate.ext_check_step(self.root, {}, self.BRANCH, trial)
        finally:
            gate._remove_trial(self.root, trial)
        return ok, out

    def test_a_branch_that_does_not_touch_the_extensions_is_still_checked(self):
        (self.wt / "other.txt").write_text("x\n")
        commit_all(self.wt, "other work")
        self.assertEqual(self.step(), (True, ["step ext-check: ok (the merged tree's extensions load)"]))

    def test_a_tree_without_extensions_runs_nothing(self):
        sh(self.root, "git", "rm", "-q", "-r", "program/extensions.yaml", "program/extensions")
        sh(self.root, "git", "commit", "-q", "-m", "no extensions")
        sh(self.wt, "git", "merge", "-q", "main")
        (self.wt / "other.txt").write_text("x\n")
        commit_all(self.wt, "other work")
        self.assertEqual(self.step(), (True, []))

    def test_a_branch_that_breaks_a_helper_an_extension_imports_fails_the_step(self):
        (self.root / "repo_helper.py").write_text("NAME = 'helper'\n")
        (self.root / "program" / "extensions" / "mine.py").write_text(
            "import os\nimport sys\n\nsys.path.insert(0, os.getcwd())\nimport repo_helper  # noqa: E402\n\n\n"
            "def register(api):\n    assert repo_helper.NAME\n")
        commit_all(self.root, "an extension that imports repository code")
        sh(self.wt, "git", "merge", "-q", "main")
        (self.wt / "repo_helper.py").write_text("OTHER = 'renamed'\n")  # no extension file changes
        commit_all(self.wt, "the helper changes")
        ok, out = self.step()
        self.assertFalse(ok)
        self.assertTrue(out[0].startswith("FAILED: ext-check the branch changes no extension file (code they import "
                                          "may have changed), and the merged tree's extensions do not load"), out)
        self.assertTrue(any("NAME" in line for line in out[1:]), out)

    def test_an_extension_that_does_not_import_fails_the_step(self):
        (self.wt / "program" / "extensions" / "mine.py").write_text("import nonexistent_dependency\n")
        commit_all(self.wt, "a broken extension")
        ok, out = self.step()
        self.assertFalse(ok)
        self.assertTrue(out[0].startswith("FAILED: ext-check the branch changes program/extensions/mine.py, and the "
                                          "merged tree's extensions do not load"), out)
        self.assertTrue(any("nonexistent_dependency" in line for line in out[1:]), out)
        self.assertTrue(all(line.startswith(gate.TAIL_PREFIX) for line in out[1:]), out)

    def test_an_api_mismatch_fails_and_a_healthy_change_passes(self):
        (self.wt / "program" / "extensions.yaml").write_text("api: 2\nmodules:\n  - name: mine\n")
        commit_all(self.wt, "another api")
        self.assertFalse(self.step()[0])
        (self.wt / "program" / "extensions.yaml").write_text("api: 1\nmodules:\n  - name: mine\n")
        (self.wt / "program" / "extensions" / "mine.py").write_text("X = 1\n\n\ndef register(api):\n    pass\n")
        commit_all(self.wt, "a healthy change")
        self.assertEqual(self.step(), (True, ["step ext-check: ok (program/extensions/mine.py changed: the merged "
                                              "tree's extensions load)"]))


if __name__ == "__main__":
    unittest.main()
