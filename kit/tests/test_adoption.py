"""Adoption's identity rules (0.7.0): what `program/adoption.yaml` switches on, and that nothing changes without it.

Without the file the kit behaves as before: a P-M-S-T-only programme refuses a legacy task ID, a ruling gap and a
follow-up gap. With it, a legacy phase prefix makes `CX-M01-S01-T01` a task ID at every site that checks one
(register.valid_id), rulings run from `ruling_start` with the frozen legacy log readable by `pk rulings show`, an
imported cost line may carry `legacy_task: true`, an ad-hoc task keeps its `legacy_id`, `pk doctor` checks the frozen
copies' digests, and no task branch may change the adoption files.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import json
import unittest
from pathlib import Path

import adoption
import briefs
import cost
import ctlstate
import gate
import register
import rulings
import status
import trace
import tracker
import worktree
from common import KitError, read_json, read_yaml, sha256_bytes, write_json, write_yaml
from test_worktree import commit_all, make_repo, run_cli, sh

KIT = Path(__file__).resolve().parent.parent
CX = "CX-M01-S01-T01"
P1 = "P1-M01-S01-T01"
LEGACY_LOG = ("# Old rulings\n\nSome preamble.\n\n## R3 (2026-01-02)\n\nKeep the old gate.\n\n"
              "## R5 (undated)\n\nUse the second store.\nIt has two lines.\n")


def task(ident, **extra):
    return {"id": ident, "title": f"task {ident}", "role": "backend", "size": "S", **extra}


def register_doc():
    return {"phases": [
        {"id": "P1", "title": "First", "milestones": [{"id": "P1-M01", "title": "m", "steps": [
            {"id": "P1-M01-S01", "title": "s", "tasks": [task(P1)]}]}]},
        {"id": "CX", "title": "Legacy", "milestones": [{"id": "CX-M01", "title": "m", "steps": [
            {"id": "CX-M01-S01", "title": "s", "tasks": [task(CX)]},
            {"id": "CX-M01-S02", "title": "s", "tasks": [task("CX-M01-S02-T01", depends_on=[CX])]}]}]}]}


def adopted_by_hand(root: Path, start: int = 6, prefixes=("CX",), log: str = LEGACY_LOG) -> None:
    """program/adoption.yaml and the legacy copies, written directly (the identity rules alone, without pk adopt)."""
    legacy = root / "program" / "legacy"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "rulings.md").write_text(log)
    (legacy / "HANDOFF.md").write_text("old\n")
    source = b'{"followups": {"next": 1, "open": [], "closed": []}}\n'  # what doctor derives followup_gaps from
    (legacy / "adoption-source.json").write_bytes(source)
    write_yaml(root / adoption.ADOPTION_FILE, {
        "adopted_at": "2026-11-24T00:00:00Z", "adopted_commit": "a" * 40, "source": adoption.SOURCE_COPY,
        "source_sha256": sha256_bytes(source), "legacy_phase_prefixes": list(prefixes), "ruling_start": start,
        "legacy_rulings": adoption.LEGACY_RULINGS, "legacy_rulings_sha256": sha256_bytes(log.encode()),
        "legacy_handoff": adoption.LEGACY_HANDOFF, "legacy_evidence_roots": ["old/evidence/"], "followup_gaps": [],
        "legacy_register": {"tasks": [], "gates": [], "roles": []}})


class Base(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)

    def program(self):
        (self.root / "program").mkdir(exist_ok=True)
        write_yaml(self.root / "program" / "register.yaml", register_doc())

    def problems(self):
        return register.problems_in(self.root)


class WithoutAdoptionTests(Base):
    """0.6.2 behaviour, unchanged: no adoption.yaml means no legacy prefix, rulings from R1 and gapless follow-ups."""

    def test_a_p_only_programme_refuses_a_legacy_task_id_everywhere(self):
        self.program()
        self.assertIn(f"{CX}: id is not a valid task id (expected P<n>-M<n>-S<n>-T<n>)", self.problems())
        self.assertIn("CX: id is not a valid phase id (expected P<n>)", self.problems())
        self.assertFalse(register.valid_id("task", CX))
        self.assertFalse(register.is_task_id(self.root, CX))
        with self.assertRaisesRegex(KitError, r"is not a task id \(expected P<n>-M<nn>-S<nn>-T<nn>, or AH"):
            worktree.new(self.root, CX)
        with self.assertRaisesRegex(KitError, "not a valid phase id"):
            briefs.render(self.root)

    def test_a_ruling_gap_is_refused(self):
        (self.root / "program").mkdir()
        rulings.add(self.root, "controller", "q1", "d1")
        self.assertEqual(rulings.next_number(self.root), 2)
        text = (self.root / "program" / "rulings.md").read_text().replace("## R1 ", "## R3 ")
        (self.root / "program" / "rulings.md").write_text(text)
        self.assertIn("R3 found where R1 expected (numbers run 1, 2, 3 with no gaps)", rulings.validate(self.root)[0])
        self.assertIsNone(rulings.block(self.root, 2))

    def test_a_follow_up_gap_is_refused(self):
        with self.assertRaisesRegex(KitError, r"F-0001 are in neither the open nor the closed list"):
            ctlstate.parse({"followups": [{"id": "F-0002", "area": "a", "item": "b"}], "followups_issued": 2})


class IdentityTests(Base):
    """The legacy phase prefix, through register.valid_id, at every site that checks a task ID."""

    def setUp(self):
        super().setUp()
        self.program()
        adopted_by_hand(self.root)

    def test_the_shared_function_takes_the_prefixes(self):
        for kind, ident in (("phase", "CX"), ("milestone", "CX-M01"), ("step", "CX-M01-S02"), ("task", CX)):
            self.assertTrue(register.valid_id(kind, ident, ("CX",)), ident)
            self.assertFalse(register.valid_id(kind, ident, ("QX",)), ident)
        self.assertFalse(register.valid_id("task", "CX-M01-S01", ("CX",)))  # a step is not a task
        self.assertFalse(register.valid_id("phase", "CX1", ("CX",)))
        self.assertTrue(register.valid_id("task", P1, ("CX",)))
        self.assertEqual(register.phase_prefixes(self.root), ("CX",))
        self.assertTrue(register.is_task_id(self.root, CX))
        self.assertTrue(register.is_task_id(self.root, "AH-0001"))
        self.assertFalse(register.is_task_id(self.root, "QX-M01-S01-T01"))

    def test_register_briefs_and_trace_accept_the_legacy_ids(self):
        self.assertEqual(self.problems(), [])
        self.assertIn("program/briefs/tasks/CX-M01-S02-T01.md", briefs.render(self.root))
        write_yaml(self.root / "program" / "traceability.yaml", {"tasks": {CX: {"closes": []}, "QX-M01-S01-T01": {}}})
        found = trace.validate(self.root, 1)
        self.assertFalse([p for p in found if CX in p], found)
        self.assertIn("QX-M01-S01-T01: is not a valid task id (expected P<n>-M<n>-S<n>-T<n> or CX-M<n>-S<n>-T<n>)",
                      found)

    def test_worktree_status_and_cost_accept_a_legacy_task(self):
        path = worktree.new(self.root, CX)
        self.assertEqual(path.name, CX.lower())
        with self.assertRaisesRegex(KitError, r"expected P<n>-M<nn>-S<nn>-T<nn>, CX-M<nn>-S<nn>-T<nn>, or AH"):
            worktree.new(self.root, "QX-M01-S01-T01")
        status.transition(self.root, CX, "in_progress", "controller")
        self.assertEqual(status.read(self.root, CX)["status"], "in_progress")
        self.assertIsNone(cost.unknown_task(self.root, CX))
        self.assertIsNotNone(cost.unknown_task(self.root, "G-P1"))  # pk cost add still refuses an unknown task

    def test_a_malformed_adoption_record_is_refused(self):
        path = self.root / adoption.ADOPTION_FILE
        good = read_yaml(path)
        for change, message in ((lambda d: d.pop("ruling_start"), "missing key"),
                                (lambda d: d.update(extra=1), "unknown key"),
                                (lambda d: d.update(legacy_phase_prefixes=["P"]), "is the kit's own"),
                                (lambda d: d.update(legacy_phase_prefixes=["cx"]), "one to four capital"),
                                (lambda d: d.update(legacy_phase_prefixes=["CX", "CX"]), "lists a prefix twice"),
                                (lambda d: d.update(source_sha256="x"), "source_sha256 must be"),
                                (lambda d: d.update(ruling_start=0), "ruling_start must be"),
                                (lambda d: d.update(legacy_rulings="old/RULINGS.md"), "under program/legacy/")):
            with self.subTest(message=message):
                data = dict(good)
                change(data)
                write_yaml(path, data)
                with self.assertRaisesRegex(KitError, message):
                    register.phase_prefixes(self.root)
        write_yaml(path, good)

    def test_doctor_reports_a_changed_legacy_copy(self):
        self.assertEqual(adoption.problems(self.root), [])
        (self.root / adoption.LEGACY_RULINGS).write_text(LEGACY_LOG + "\n## R6 (late)\n")
        (self.root / adoption.LEGACY_HANDOFF).unlink()
        (self.root / adoption.SOURCE_COPY).write_text('{"edited": true}\n')
        found = adoption.problems(self.root)
        self.assertEqual(len(found), 3, found)
        self.assertIn("program/legacy/adoption-source.json no longer matches source_sha256", found[0])
        self.assertIn("program/legacy/rulings.md no longer matches legacy_rulings_sha256", found[1])
        self.assertIn("legacy_handoff program/legacy/HANDOFF.md is missing", found[2])

    def test_the_adoption_files_are_always_guarded(self):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "adopted")
        sh(self.root, "git", "checkout", "-q", "-b", f"task/{P1}")
        (self.root / adoption.LEGACY_RULINGS).write_text("rewritten\n")
        (self.root / "src.txt").write_text("work\n")
        spec = self.root / "program" / "tasks" / P1 / "SPEC.md"
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "work")
        sh(self.root, "git", "checkout", "-q", "main")
        spec.parent.mkdir(parents=True)
        spec.write_text(f"Guarded edit: {adoption.LEGACY_RULINGS}\nGuarded edit: program/legacy/\n")
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", "spec")
        self.assertEqual(gate._guarded_changes(self.root, P1, f"task/{P1}", "main", []), [adoption.LEGACY_RULINGS])
        self.assertEqual(gate._guarded_changes(self.root, P1, f"task/{P1}", "main", ["program/"]),
                         [adoption.LEGACY_RULINGS])


class RulingsTests(Base):
    def setUp(self):
        super().setUp()
        self.program()
        adopted_by_hand(self.root)

    def test_the_kit_issues_rulings_from_ruling_start(self):
        self.assertEqual(rulings.next_number(self.root), 6)
        self.assertEqual(rulings.add(self.root, "controller", "q", "d, supersedes R3"), 6)
        self.assertEqual(rulings.add(self.root, "controller", "q", "d"), 7)
        self.assertEqual(rulings.validate(self.root), [])
        self.assertEqual(rulings.read(self.root)[0]["supersedes"], [3])
        with self.assertRaisesRegex(KitError, "supersedes R4, which is not an earlier ruling"):
            rulings.add(self.root, "controller", "q", "supersedes R4")  # below the start, but not in the legacy log

    def test_validate_refuses_a_number_below_the_start_and_a_gap_above_it(self):
        path = self.root / "program" / "rulings.md"
        rulings.add(self.root, "controller", "q", "d")
        path.write_text(path.read_text().replace("## R6 ", "## R5 "))
        self.assertIn("R5 is below ruling_start R6", rulings.validate(self.root)[0])
        path.write_text(path.read_text().replace("## R5 ", "## R8 "))
        self.assertIn("R8 found where R6 expected (numbers run 6, 7, 8 with no gaps)", rulings.validate(self.root)[0])

    def test_show_reads_the_legacy_log_below_the_start(self):
        rulings.add(self.root, "controller", "the question", "the decision")
        self.assertEqual(rulings.block(self.root, 5), "## R5 (undated)\n\nUse the second store.\nIt has two lines.\n")
        self.assertTrue(rulings.block(self.root, 6).startswith("## R6 ("))
        self.assertIn("**Decision.** the decision", rulings.block(self.root, 6))
        code, out, err = run_cli("rulings", self.root, "rulings", "show", "R3")
        self.assertEqual((code, out), (0, "## R3 (2026-01-02)\n\nKeep the old gate.\n"))
        code, out, err = run_cli("rulings", self.root, "rulings", "show", "R4")
        self.assertEqual(code, 1)
        self.assertIn("R4 is not in the legacy log program/legacy/rulings.md", err)
        self.assertEqual(run_cli("rulings", self.root, "rulings", "show", "R9")[0], 1)
        self.assertIn("takes a ruling as R<n>", run_cli("rulings", self.root, "rulings", "show", "6")[2])

    def test_show_returns_every_block_of_a_number_suffixed_and_sub_blocks_too(self):
        log = ("# Old rulings\n\n## R84 (2026-01-01)\n\nBase.\n\n## R84a (2026-01-02)\n\nSuffixed.\n\n"
               "## R840 (2026-02-01)\n\nAnother number.\n\n## R141(a) (2026-03-01)\n\nFirst part.\n\n"
               "## R141(e) (2026-03-05)\n\nAmendment.\n\n## R12-R13 (undated)\n\nA range.\n")
        adopted_by_hand(self.root, start=841, log=log)
        self.assertEqual(rulings.block(self.root, 84),
                         "## R84 (2026-01-01)\n\nBase.\n\n## R84a (2026-01-02)\n\nSuffixed.\n")
        self.assertEqual(rulings.block(self.root, 141),
                         "## R141(a) (2026-03-01)\n\nFirst part.\n\n## R141(e) (2026-03-05)\n\nAmendment.\n")
        self.assertEqual(rulings.block(self.root, 840), "## R840 (2026-02-01)\n\nAnother number.\n")
        self.assertEqual(adoption.legacy_ruling_numbers(self.root), {12, 84, 141, 840})
        code, out, _ = run_cli("rulings", self.root, "rulings", "show", "R84")
        self.assertEqual((code, out.count("## R84")), (0, 2))
        self.assertEqual(rulings.add(self.root, "controller", "q", "supersedes R141 and supersedes R12"), 841)
        self.assertEqual(rulings.validate(self.root), [])
        with self.assertRaisesRegex(KitError, "supersedes R13, which is not an earlier ruling"):
            rulings.add(self.root, "controller", "q", "supersedes R13")

    def test_record_and_the_hand_off_number_from_the_start(self):
        (self.root / "program" / "HANDOFF.md").write_bytes((KIT / "templates" / "HANDOFF.md").read_bytes())
        write_json(self.root / ctlstate.STATE_FILE, read_json(KIT / "templates" / "controller-state.json"))
        commit_all(self.root, "adopted")
        lines = tracker.record(self.root, "the first kit ruling", commit=False)
        self.assertEqual(lines[0], "R6 recorded")
        text = (self.root / "program" / "HANDOFF.md").read_text()
        self.assertIn("#### Rulings (1; next R7)", text)
        self.assertIn("rulings before R6: program/legacy/rulings.md", text)


class CostAndStateTests(Base):
    def row(self, **extra):
        return {"at": "t", "task": "G-P1", "role": "qa", "agent": "a", "model": "m", "tokens": 500_000,
                "tool_calls": 0, "seconds": 0, "resume": False, **extra}

    def test_the_reader_accepts_legacy_task_true_only_and_never_flags_it(self):
        (self.root / "program").mkdir()
        log = self.root / cost.COST_FILE
        log.write_text(json.dumps(self.row(legacy_task=True)) + "\n")
        self.assertEqual(len(cost.read(self.root)), 1)
        self.assertEqual(cost.flagged_lines(self.root), [])
        self.assertEqual(cost.totals(cost.read(self.root))["G-P1"]["tokens"], 500_000)
        log.write_text(json.dumps(self.row()) + "\n")
        self.assertEqual(len(cost.flagged_lines(self.root)), 1)  # the same line without the key is flagged
        log.write_text(json.dumps(self.row(legacy_task=False)) + "\n")
        with self.assertRaisesRegex(KitError, "line 1: legacy_task, when present, is true"):
            cost.read(self.root)
        log.write_text(json.dumps(self.row(task="AH-0001", imported=True)) + "\n")
        self.assertEqual(cost.flagged_lines(self.root), [])  # an imported line of a kit task is never flagged
        self.assertEqual(cost.totals(cost.read(self.root))["AH-0001"]["tokens"], 500_000)
        log.write_text(json.dumps(self.row(imported=False)) + "\n")
        with self.assertRaisesRegex(KitError, "line 1: imported, when present, is true"):
            cost.read(self.root)

    def test_the_state_keeps_legacy_id_and_adhoc_list_shows_it(self):
        (self.root / "program").mkdir()
        write_json(self.root / ctlstate.STATE_FILE, {"adhoc": [
            {"id": "AH-0001", "title": "fp", "state": "closed", "legacy_id": "kit-fp"},
            {"id": "AH-0002", "title": "new", "state": "queued"}]})
        state = ctlstate.load(self.root)
        self.assertEqual(state["adhoc"][0]["legacy_id"], "kit-fp")
        self.assertNotIn("legacy_id", state["adhoc"][1])
        ctlstate.save(self.root, state)
        self.assertEqual(ctlstate.load(self.root), state)
        out = run_cli("tracker", self.root, "adhoc", "list")[1].splitlines()
        self.assertTrue(out[0].endswith("fp [legacy kit-fp]"), out)
        self.assertNotIn("legacy", out[1])
        with self.assertRaisesRegex(KitError, "legacy_id must be"):
            ctlstate.parse({"adhoc": [{"id": "AH-0001", "title": "x", "state": "queued", "legacy_id": ""}]})


if __name__ == "__main__":
    unittest.main()
