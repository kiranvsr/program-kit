"""`pk adopt` (0.7.0): the whole source is validated before anything is written, a refusal writes nothing, and a
check that fails after the import puts every file back. The importer moves the register, rulings, follow-ups, ad-hoc
tasks, cost lines and hand-off; statuses wait for KA-2 and a source that lists any is refused.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import adopt
import adoption
import cost
import ctlstate
import pk
import rulings
import tracker
from common import KitError, read_yaml, sha256_bytes
from test_adoption import CX, KIT, LEGACY_LOG, P1, register_doc
from test_worktree import commit_all, make_repo, run_cli, sh


def source(**overrides):
    data = {
        "schema": "program-kit.adoption.v1", "programme": "sample", "primary_branch": "main",
        "legacy_phase_prefixes": ["CX"], "register": register_doc(), "statuses": [],
        "rulings": {"path": "old/RULINGS.md", "next": 6},
        "followups": {"next": 4,
                      "open": [{"id": "F-0003", "area": "ci", "item": "pin the image", "added": "2026-01-03",
                                "tags": []}],
                      "closed": [{"id": "F-0001", "area": "ui", "item": "empty state", "reason": "fixed"},
                                 {"id": "F-0002", "area": "ui", "item": "empty", "reason": "dup", "dup_of": "F-0001",
                                  "added": "2026-01-02", "tags": ["later"]}]},
        "adhoc": [{"legacy_id": "kit-fp", "title": "fingerprint work", "state": "done", "note": "landed"},
                  {"legacy_id": "COMPOSITION", "title": "compose", "state": "in_progress", "note": ""}],
        "costs": [{"at": "2026-01-01T00:00:00Z", "task": P1, "role": "implementer", "model": "m-1", "tokens": 10,
                   "tool_calls": 2, "seconds": 3},
                  {"at": "2026-01-02T00:00:00Z", "task": "kit-fp", "role": "qa", "model": "m-1", "tokens": 5,
                   "tool_calls": 1, "seconds": 4.0, "agent": "a1", "resume": True, "note": "kept"},
                  {"at": "2026-01-03T00:00:00Z", "task": "G-P1", "role": "qa", "model": "m-2", "tokens": 7,
                   "tool_calls": 0, "seconds": 0}],
        "handoff": {"path": "old/HANDOFF.md", "now": "moving onto the kit"},
        "legacy_evidence_roots": ["old/evidence/"],
    }
    data.update(overrides)
    return data


def install(root: Path) -> None:
    """program/ as install.sh leaves it (the python preset), and the legacy files, committed on main."""
    program = root / "program"
    program.mkdir()
    for name in pk.TEMPLATE_FILES:
        shutil.copy(KIT / "templates" / name, program / name)
    shutil.copy(KIT / "presets" / "checks.python.yaml", program / "checks.yaml")
    (root / "old" / "evidence").mkdir(parents=True)
    (root / "old" / "RULINGS.md").write_text(LEGACY_LOG)
    (root / "old" / "HANDOFF.md").write_bytes(b"# Old hand-off\r\n\r\nbytes kept as they are\r\n")
    (root / "old" / "evidence" / "acceptance.json").write_text("{}\n")
    commit_all(root, "install the kit")



class SourceValidationTests(unittest.TestCase):
    """validate_source: every problem of the source, before anything is written."""

    @classmethod
    def setUpClass(cls):
        cls._case = unittest.TestCase()
        cls.root = make_repo(cls._case)
        install(cls.root)
        (cls.root / "old" / "ignored.md").write_text(LEGACY_LOG)  # on disk, git-ignored: never committed
        with open(cls.root / ".git" / "info" / "exclude", "a") as handle:
            handle.write("old/ignored.md\n")

    @classmethod
    def tearDownClass(cls):
        cls._case.doCleanups()

    def problems(self, data):
        return adopt.validate_source(self.root, json.dumps(data).encode())[1]

    def assertProblem(self, data, text):
        found = self.problems(data)
        self.assertTrue(any(text in problem for problem in found), found)

    def test_the_fixture_is_valid_and_the_plan_maps_everything(self):
        plan, found = adopt.validate_source(self.root, json.dumps(source()).encode())
        self.assertEqual(found, [])
        state = plan["state"]
        self.assertEqual((state["followups_issued"], [i["id"] for i in state["followups"]]), (3, ["F-0003"]))
        self.assertEqual(state["closed_followups"][1]["duplicate_of"], "F-0001")
        self.assertEqual([(i["id"], i["legacy_id"], i["state"]) for i in state["adhoc"]],
                         [("AH-0001", "kit-fp", "closed"), ("AH-0002", "COMPOSITION", "progress")])
        first, second, third = plan["costs"]
        self.assertEqual((first["task"], first["agent"], first["resume"]), (P1, "unknown", False))
        self.assertNotIn("legacy_task", first)
        self.assertEqual((second["task"], second["agent"], second["seconds"], second["note"]), ("AH-0001", "a1", 4,
                                                                                               "kept"))
        self.assertEqual((third["task"], third["legacy_task"]), ("G-P1", True))
        self.assertEqual(plan["ruling_start"], 6)

    def test_the_schema_requires_the_keys_the_importer_reads(self):
        self.assertEqual(adopt.schema_required(), list(adopt.SOURCE_KEYS))

    def test_a_follow_up_gap_is_refused_naming_the_missing_ids(self):
        followups = source()["followups"]
        followups["closed"] = followups["closed"][1:]
        self.assertProblem(source(followups=followups), "F-0001 to F-0003 must all be listed, open or closed; "
                                                        "missing 1: F-0001")

    def test_non_empty_statuses_wait_for_ka2(self):
        self.assertProblem(source(statuses=[{"task_id": P1, "status": "done", "references": [{"path": "x"}]}]),
                           "statuses import arrives in KA-2")
        self.assertProblem(source(statuses=[{"task_id": P1, "status": "adopted"}]), "must be one of done")
        widened = [{"task_id": P1, "status": state, "actor": "", "updated_at": ""}
                   for state in ("in_progress", "review", "blocked", "not_started")]
        widened[1]["task_id"], widened[2]["task_id"], widened[3]["task_id"] = CX, "CX-M01-S02-T01", "P9-M01-S01-T01"
        self.assertEqual(self.problems(source(statuses=widened)),
                         ["statuses: 4 listed; statuses import arrives in KA-2 (export an empty statuses list until "
                          "then)"])  # the widened enum and empty actor and updated_at are shapes KA-2 imports

    def test_notes_are_optional_text_and_stay_in_the_stored_source(self):
        self.assertEqual(self.problems(source(notes=["dropped two empty rows", "nulled a bad date"])), [])
        self.assertProblem(source(notes="one note"), "notes: must be a list of text")
        self.assertProblem(source(notes=[1]), "notes: must be a list of text")

    def test_each_refusal(self):
        cases = [
            (dict(schema="other"), "this kit reads 'program-kit.adoption.v1'"),
            (dict(primary_branch="master"), "the kit's primary branch is main"),
            (dict(legacy_phase_prefixes=["P"]), "'P' is the kit's own"),
            (dict(legacy_phase_prefixes=["CX", "CX"]), "legacy_phase_prefixes lists a prefix twice"),
            (dict(rulings={"path": "old/ignored.md", "next": 6}), "old/ignored.md is not a file committed at HEAD"),
            (dict(legacy_phase_prefixes=[]), "CX: id is not a valid phase id"),
            (dict(rulings={"path": "old/RULINGS.md", "next": 9}), "the legacy log's last ruling is R5"),
            (dict(rulings={"path": "old/missing.md", "next": 6}), "is not a file committed at HEAD"),
            (dict(rulings={"path": "../x.md", "next": 6}), "must be a repository-relative path"),
            (dict(handoff={"path": "old/HANDOFF.md"}), "handoff: missing now"),
            (dict(adhoc=[{"legacy_id": P1, "title": "t", "state": "queued"}]), "is also a register task ID"),
            (dict(adhoc=[{"legacy_id": "a", "title": "t", "state": "finished"}]), "state 'finished' must be one of"),
            (dict(adhoc=[{"legacy_id": "a", "title": "t", "state": "queued"}] * 2), "legacy_id a is listed twice"),
            (dict(costs=[{"at": "t", "task": P1, "role": "r", "tokens": 1, "tool_calls": 0, "seconds": 0}]),
             "costs[1]: model must be non-empty text"),
            (dict(costs=[{"at": "t", "task": P1, "role": "r", "model": "m", "tokens": 1.5, "tool_calls": 0,
                          "seconds": 0}]), "costs[1]: tokens must be a whole number"),
            (dict(costs=[{"at": "t", "task": "x", "role": "r", "model": "m", "tokens": 1, "tool_calls": 0,
                          "seconds": 0, "legacy_task": True}]), "legacy_task is the kit's own key"),
            (dict(costs=[{"at": "t", "task": "x", "role": "r", "model": "m", "tokens": 1, "tool_calls": 0,
                          "seconds": 0, "imported": True}]), "imported is the kit's own key"),
            (dict(costs=[{"at": "t", "task": "x", "role": "r", "model": "m", "tokens": 1, "tool_calls": 0,
                          "seconds": 0, "agent": 3}]), "costs[1]: not a cost record"),
            (dict(legacy_evidence_roots=["old/none/"]), "old/none/ is not a directory committed at HEAD"),
            (dict(extra=1), "unknown key(s) extra"),
        ]
        for change, text in cases:
            with self.subTest(text=text):
                self.assertProblem(source(**change), text)
        followups = source()["followups"]
        followups["open"][0]["id"] = "F-0004"
        self.assertProblem(source(followups=followups), "F-0004 at or past followups.next F-0004")
        followups = source()["followups"]
        followups["closed"][1]["dup_of"] = "F-0009"
        self.assertProblem(source(followups=followups), "dup_of F-0009 is not a follow-up of the source")
        followups = source()["followups"]
        followups["closed"][0]["resolution"] = "x"
        self.assertProblem(source(followups=followups), "unknown key(s) resolution")
        missing = source()
        del missing["costs"]
        self.assertEqual(self.problems(missing), ["missing key(s) costs"])
        self.assertEqual(adopt.validate_source(self.root, b"[]")[1], ["the source must be a JSON object"])


class AdoptCommandTests(unittest.TestCase):
    """`pk adopt` end to end on a temporary repository: refusals write nothing; a failed check undoes the import."""

    def setUp(self):
        self.root = make_repo(self)
        install(self.root)
        self.source = self.root / "adoption-source.json"  # untracked in the tree: the one file a clean tree may add
        self.write(source(notes=["the exporter kept every row"]))

    def write(self, data):
        self.source.write_text(json.dumps(data, indent=1))

    def adopt(self, *extra):
        return run_cli("adopt", self.root, "adopt", str(self.source), *extra)

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in (self.root / "program").rglob("*")), \
            sh(self.root, "git", "status", "--porcelain")

    def test_adopt_imports_everything_and_the_kit_then_reads_it(self):
        code, out, err = self.adopt()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("rulings: 2 legacy (R3 to R5) frozen in program/legacy/rulings.md; the kit issues R6 next", out)
        self.assertIn("cost lines: 3 (1 legacy_task, 1 mapped to AH- IDs)", out)
        record = adoption.load(self.root)
        self.assertEqual(list(record), list(adoption.KEYS))
        self.assertEqual(record["adopted_commit"], sh(self.root, "git", "rev-parse", "HEAD").strip())
        self.assertEqual((record["legacy_phase_prefixes"], record["ruling_start"]), (["CX"], 6))
        legacy = self.root / "program" / "legacy"
        self.assertEqual((legacy / "rulings.md").read_bytes(), (self.root / "old" / "RULINGS.md").read_bytes())
        self.assertEqual((legacy / "HANDOFF.md").read_bytes(), (self.root / "old" / "HANDOFF.md").read_bytes())
        self.assertEqual((legacy / "adoption-source.json").read_bytes(), self.source.read_bytes())
        self.assertEqual(json.loads(self.source.read_bytes())["notes"], ["the exporter kept every row"])
        self.assertEqual(record["source_sha256"], sha256_bytes(self.source.read_bytes()))
        self.assertEqual(read_yaml(self.root / "program" / "register.yaml"), register_doc())
        state = ctlstate.load(self.root)
        self.assertEqual(state["now"], "moving onto the kit")
        self.assertEqual(tracker.add_followup(state, "a", "b"), "F-0004")
        rows = cost.read(self.root)
        self.assertEqual([row["task"] for row in rows], [P1, "AH-0001", "G-P1"])
        self.assertEqual([row["imported"] for row in rows], [True, True, True])
        handoff = (self.root / "program" / "HANDOFF.md").read_text()
        self.assertIn("#### Rulings (0; next R6)", handoff)  # pk handoff update ran, numbering from ruling_start
        self.assertIn("rulings before R6: program/legacy/rulings.md", handoff)
        self.assertIn(f"{adoption.ADOPTION_FILE}", sh(self.root, "git", "status", "--porcelain", "-uall"))  # no commit
        self.assertEqual(run_cli("rulings", self.root, "rulings", "show", "R5")[1].splitlines()[0], "## R5 (undated)")
        self.assertEqual(rulings.add(self.root, "controller", "q", "supersedes R5"), 6)
        self.assertEqual(adoption.problems(self.root), [])
        code, out, err = self.adopt()  # once only
        self.assertEqual(code, 1)
        self.assertIn("program/adoption.yaml exists: this programme has already adopted", err)
        (self.root / adoption.LEGACY_RULINGS).write_bytes(LEGACY_LOG.encode() + b"edited\n")
        code, said = adopt._run_pk(self.root, "doctor")
        self.assertEqual(code, 1)
        self.assertIn("program/legacy/rulings.md no longer matches legacy_rulings_sha256", said)

    def test_imported_cost_lines_are_never_flagged_and_new_lines_count_from_zero(self):
        heavy = [{"at": "2026-01-01T00:00:00Z", "task": task, "role": "qa", "model": "m-1", "tokens": 300_000,
                  "tool_calls": 0, "seconds": 0, "agent": "qa", "resume": True} for task in (P1, "kit-fp") * 3]
        self.write(source(costs=heavy))
        self.assertEqual(self.adopt()[0], 0)
        self.assertEqual(cost.totals(cost.read(self.root))["AH-0001"]["tokens"], 900_000)  # still in the report
        self.assertEqual(cost.flagged_lines(self.root), [])  # 900k tokens and 3 resumes each, but before adoption
        self.assertIn("#### Agents past the fresh-agent threshold\n- none",
                      (self.root / "program" / "HANDOFF.md").read_text())
        for task in ("AH-0001", P1):
            code, out, err = run_cli("cost", self.root, "cost", "add", "--task", task, "--role", "qa", "--model",
                                     "m-1", "--tokens", "1000")
            self.assertEqual((code, err), (0, ""))
            self.assertNotIn("past the threshold", out)
        run_cli("cost", self.root, "cost", "add", "--task", P1, "--role", "qa", "--model", "m-1", "--tokens",
                "400000")
        self.assertEqual(cost.flagged_lines(self.root),
                         [f"{P1} qa (m-1): 401000 tokens over 2 run(s), 0 resume(s): start a fresh agent with a "
                          "tight fix brief"])  # the new lines alone, from zero

    def test_pk_doctor_is_one_of_the_checks_after_the_import(self):
        before = self.tree()
        real = adopt._run_pk

        def failing(root, *argv):
            if argv == ("doctor",) and (Path(root) / adoption.ADOPTION_FILE).exists():
                return 1, "error: planted doctor failure"
            return real(root, *argv)

        with mock.patch.object(adopt, "_run_pk", side_effect=failing):
            code, out, err = self.adopt()
        self.assertEqual(code, 1)
        self.assertIn("pk doctor failed after the import: error: planted doctor failure", err)
        self.assertEqual(self.tree(), before)

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        before = self.tree()
        code, out, err = self.adopt("--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "dry run: nothing written")
        self.assertIn("register: 3 tasks (CX 2, P 1); legacy phase prefixes: CX", out)
        self.assertIn("follow-ups: 1 open, 2 closed; next F-0004", out)
        self.assertEqual(self.tree(), before)

    def test_a_bad_source_prints_every_problem_and_writes_nothing(self):
        followups = source()["followups"]
        followups["open"] = []
        self.write(source(followups=followups, statuses=[{"task_id": P1, "status": "done"}]))
        before = self.tree()
        code, out, err = self.adopt()
        self.assertEqual(code, 1)
        self.assertIn("missing 1: F-0003", err)
        self.assertIn("statuses import arrives in KA-2", err)
        self.assertIn("error: adopt: 2 problem(s); nothing written", err)
        self.assertEqual(self.tree(), before)

    def test_the_repository_must_be_clean_on_main_and_hold_no_kit_work(self):
        (self.root / "stray.txt").write_text("x\n")
        rulings.add(self.root, "controller", "q", "d")
        before = self.tree()
        code, out, err = self.adopt()
        self.assertEqual(code, 1)
        self.assertIn("the tree is not clean (program/rulings.md, stray.txt)", err)
        self.assertIn("program/ already holds kit work (program/rulings.md holds rulings)", err)
        self.assertEqual(self.tree(), before)
        sh(self.root, "git", "checkout", "-q", "-b", "other")
        self.assertIn("runs in the main checkout on main (HEAD is refs/heads/other)", self.adopt()[2])

    def test_a_failed_check_after_the_import_puts_everything_back(self):
        before = self.tree()
        real = adopt._run_pk

        def failing(root, *argv):
            if argv == ("status", "validate"):
                return 1, "error: planted failure"
            return real(root, *argv)

        with mock.patch.object(adopt, "_run_pk", side_effect=failing):
            code, out, err = self.adopt()
        self.assertEqual(code, 1)
        self.assertIn("pk status validate failed after the import: error: planted failure", err)
        self.assertIn("every file it wrote is put back", err)
        self.assertEqual(self.tree(), before)

    def test_a_failed_write_puts_everything_back(self):
        before = self.tree()
        real = adopt.write_yaml

        def failing(path, data):
            if Path(path).name == "adoption.yaml":
                raise KitError("disk full (planted)")
            return real(path, data)

        with mock.patch.object(adopt, "write_yaml", side_effect=failing):
            code, out, err = self.adopt()
        self.assertEqual(code, 1)
        self.assertIn("disk full (planted)", err)
        self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
