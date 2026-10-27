"""`pk drift`: a report-only check that the checked-in documents still agree with their generators, approvals and code."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

import gate
import handoff
import pk
from common import KitError, write_json, write_yaml
from test_register import reg, task
from test_worktree import commit_all, make_repo, run_cli, sh

KIT = Path(__file__).resolve().parent.parent
TID = "P1-M01-S01-T01"
GEN = "from pathlib import Path\nPath('docs').mkdir(exist_ok=True)\nPath('docs/gen.txt').write_text({text!r})\n"
LINE = re.compile(r"^  (D[1-6]) (ok|DRIFT) +(.*)$")


class DriftBase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        program = self.root / "program"
        program.mkdir()
        shutil.copy(KIT / "templates" / "HANDOFF.md", program / "HANDOFF.md")
        shutil.copy(KIT / "templates" / "controller-state.json", program / "controller-state.json")
        write_yaml(program / "register.yaml", reg(task(TID)))
        self.write_checks()
        self.write("tools/gen.py", GEN.format(text="generated\n"))
        self.write("docs/gen.txt", "generated\n")
        commit_all(self.root, "program")
        handoff.update(self.root)
        commit_all(self.root, "hand-off")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_checks(self, **drift):
        section = {"regenerate": [[sys.executable, "tools/gen.py"]]}
        section.update(drift)
        write_yaml(self.root / "program" / "checks.yaml", {"build": [], "test": [], "scan": [], "drift": section})

    def drift(self, *argv):
        return run_cli("drift", self.root, "drift", *argv)

    def rows(self, out):
        return {m.group(1): (m.group(2), m.group(3)) for m in map(LINE.match, out.splitlines()) if m}

    def snapshot(self):
        return (sh(self.root, "git", "status", "--porcelain", "--untracked-files=all"),
                sh(self.root, "git", "worktree", "list", "--porcelain"), sh(self.root, "git", "rev-parse", "HEAD"))


class ReportTests(DriftBase):
    def test_a_current_program_drifts_nowhere_and_the_checkout_is_untouched(self):
        before = self.snapshot()
        code, out, err = self.drift()
        self.assertEqual((code, err), (0, ""), out)
        rows = self.rows(out)
        self.assertEqual(list(rows), ["D1", "D2", "D3", "D4", "D5", "D6"])
        self.assertTrue(all(state == "ok" for state, _ in rows.values()), out)
        head = sh(self.root, "git", "rev-parse", "HEAD").strip()
        self.assertRegex(out.splitlines()[0], rf"^drift report at {head[:12]} \(\d+ s\)$")
        self.assertEqual(out.splitlines()[-1], "  0 of 6 checks drift")
        self.assertIn("regeneration is a no-op", rows["D1"][1])
        self.assertEqual(self.drift("--strict")[0], 0)
        self.assertEqual(self.snapshot(), before)  # report only: nothing written, no worktree left behind

    def test_a_regenerate_command_that_changes_a_file_is_d1_and_only_strict_exits_1(self):
        self.write("tools/gen.py", GEN.format(text="regenerated\n"))
        commit_all(self.root, "the generator moved on; its output was not regenerated")
        before = self.snapshot()
        code, out, _ = self.drift()
        self.assertEqual(code, 0)
        state, detail = self.rows(out)["D1"]
        self.assertEqual(state, "DRIFT")
        self.assertIn("docs/gen.txt", detail)
        self.assertEqual(out.splitlines()[-1], "  1 of 6 checks drift")
        self.assertEqual(self.drift("--strict")[0], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.root / "docs" / "gen.txt").read_text(), "generated\n")

    def test_a_new_untracked_output_and_a_failing_command_are_d1(self):
        self.write("tools/new.py", "open('extra.txt', 'w').write('x')\n")
        self.write("tools/fail.py", "raise SystemExit(3)\n")
        self.write_checks(regenerate=[[sys.executable, "tools/new.py"], [sys.executable, "tools/fail.py"]])
        commit_all(self.root, "two more generators")
        detail = self.rows(self.drift()[1])["D1"][1]
        self.assertIn("extra.txt", detail)
        self.assertIn("tools/fail.py exits 3", detail)

    def test_yaml_files_are_compared_as_parsed_data(self):
        self.write("program/data.yaml", "name: 'x'\n")
        self.write("tools/yml.py", "open('program/data.yaml', 'w').write('name: x\\n')\n")
        self.write_checks(regenerate=[[sys.executable, "tools/yml.py"]])
        commit_all(self.root, "a yaml generator that requotes")
        self.assertEqual(self.rows(self.drift()[1])["D1"][0], "ok")
        self.write("tools/yml.py", "open('program/data.yaml', 'w').write('name: y\\n')\n")
        commit_all(self.root, "a yaml generator whose data moved on")
        state, detail = self.rows(self.drift()[1])["D1"]
        self.assertEqual(state, "DRIFT")
        self.assertIn("program/data.yaml", detail)

    def test_the_kit_regenerators_run_once_the_register_is_approved(self):
        write_json(self.root / "program" / "approvals" / "stage-6.json", {})
        write_yaml(self.root / "program" / "traceability.yaml", {"journeys": {}, "requirements": {},
                                                                 "components": {}, "gates": {}, "tasks": {}})
        self.assertEqual(run_cli("trace", self.root, "trace", "sync")[0], 0)
        commit_all(self.root, "register approved; briefs not generated")
        state, detail = self.rows(self.drift()[1])["D1"]
        self.assertEqual(state, "DRIFT")
        self.assertIn("briefs", detail)
        self.assertEqual(run_cli("briefs", self.root, "briefs", "generate")[0], 0)
        commit_all(self.root, "briefs generated")
        state, detail = self.rows(self.drift()[1])["D1"]
        self.assertEqual(state, "ok", detail)
        self.assertIn("trace sync and briefs check", detail)

    def test_d2_names_an_approved_stage_whose_files_changed(self):
        self.assertIn("no ledger", self.rows(self.drift()[1])["D2"][1])
        self.write("program/ledger.yaml", "sources: {}\nentries: []\n")
        commit_all(self.root, "ledger")
        with mock.patch("interview.next_stage", return_value=2), \
                mock.patch("interview._changed_since_approval", return_value=["program/product.md"]):
            state, detail = self.rows(self.drift()[1])["D2"]
        self.assertEqual(state, "DRIFT")
        self.assertIn("stage 2 was approved and its files changed: program/product.md", detail)
        with mock.patch("interview.next_stage", return_value=7), \
                mock.patch("interview._changed_since_approval", return_value=[]):
            self.assertEqual(self.rows(self.drift()[1])["D2"][0], "ok")

    def test_d3_runs_on_the_checkout_ignores_live_worktree_lines_and_is_skipped_for_another_revision(self):
        sh(self.root, "git", "worktree", "add", "-q", "--detach", str(self.root / ".worktrees" / "x"), "HEAD")
        (self.root / ".worktrees" / "x" / "dirty.txt").write_text("x")
        self.assertEqual(self.rows(self.drift()[1])["D3"][0], "ok")  # KF-14: a live worktree is not drift
        write_json(self.root / "program" / "status" / f"{TID}.json",
                   {"task_id": TID, "status": "in_progress", "role": "controller", "updated_at": "", "note": "",
                    "evidence": [], "blocked_by": [], "history": []})
        commit_all(self.root, "a task in flight; the hand-off was not updated")
        state, detail = self.rows(self.drift()[1])["D3"]
        self.assertEqual(state, "DRIFT")
        self.assertIn("stale", detail)
        code, out, _ = self.drift("--rev", "HEAD~1")
        self.assertEqual(code, 0)
        older = sh(self.root, "git", "rev-parse", "HEAD~1").strip()
        self.assertIn(f"drift report at {older[:12]}", out)
        self.assertEqual(self.rows(out)["D3"], ("ok", "skipped: not the checkout's HEAD"))

    def test_d4_lists_the_migrations_the_document_does_not_name(self):
        self.assertIn("not configured", self.rows(self.drift()[1])["D4"][1])
        self.write("db/migrations/0001_init.sql", "-- one\n")
        self.write("db/migrations/0002_add.sql", "-- two\n")
        self.write("docs/DATABASE.md", "# Database\n\nMigration 0001 creates the tables.\n")
        self.write_checks(docs_vs_migrations={"glob": "db/migrations/*.sql", "doc": "docs/DATABASE.md"})
        commit_all(self.root, "migrations")
        state, detail = self.rows(self.drift()[1])["D4"]
        self.assertEqual(state, "DRIFT")
        self.assertIn("2 files match db/migrations/*.sql", detail)
        self.assertIn("not named in docs/DATABASE.md: 0002_add.sql", detail)
        self.write("docs/DATABASE.md", "# Database\n\n0001 creates the tables; 0002_add.sql adds a column.\n")
        commit_all(self.root, "documented")
        self.assertEqual(self.rows(self.drift()[1])["D4"][0], "ok")

    def test_d5_and_d6_a_done_task_without_an_acceptance_record(self):
        write_json(self.root / "program" / "status" / f"{TID}.json",
                   {"task_id": TID, "status": "done", "role": "qa", "updated_at": "", "note": "",
                    "evidence": [], "blocked_by": [], "history": []})
        commit_all(self.root, "done by hand")
        handoff.update(self.root)
        commit_all(self.root, "hand-off")
        rows = self.rows(self.drift()[1])
        self.assertEqual(rows["D5"], ("DRIFT", f"done without an acceptance record: {TID}"))
        self.assertEqual(rows["D6"][0], "DRIFT")
        self.assertIn("status:", rows["D6"][1])

    def test_a_revision_that_is_not_a_commit_is_one_error_and_leaves_nothing(self):
        before = self.snapshot()
        for rev in ("no-such-branch", "--orphan"):
            code, out, err = self.drift(f"--rev={rev}")
            self.assertEqual((code, out), (1, ""), rev)
            self.assertTrue(err.startswith("error: drift: ") and err.count("\n") == 1, err)
        self.assertEqual(self.snapshot(), before)


class ChecksSectionTests(unittest.TestCase):
    def test_the_drift_section_is_optional_and_checked_by_shape(self):
        self.assertEqual(gate._parse_checks({}, ())["drift"], {"regenerate": [], "docs_vs_migrations": None})
        good = {"regenerate": [["python3", "tools/gen.py"]], "docs_vs_migrations": {"glob": "m/*.sql", "doc": "D.md"}}
        self.assertEqual(gate._parse_checks({"drift": good}, ())["drift"], good)
        for bad, needle in (("x", "drift must be a mapping"), ({"other": 1}, "unknown key other"),
                            ({"regenerate": "python3 gen.py"}, "argv list"), ({"regenerate": ""}, "argv list"),
                            ({"regenerate": [["a", ""]]}, "argv list"),
                            ({"docs_vs_migrations": {"glob": "m/*.sql"}}, "glob and doc"),
                            ({"docs_vs_migrations": {"glob": "/abs/*.sql", "doc": "D.md"}}, "relative"),
                            ({"docs_vs_migrations": {"glob": "m/*.sql", "doc": "../D.md"}}, "relative")):
            with self.subTest(bad=bad):
                with self.assertRaises(KitError) as caught:
                    gate._parse_checks({"drift": bad}, ())
                self.assertIn(needle, str(caught.exception))
        self.assertTrue(any("drift" in problem for problem in pk._checks_problems({"drift": "x"})))
        self.assertEqual(pk._checks_problems({"build": [["b"]], "test": [["t"]], "scan": [["s"]], "drift": good}), [])


if __name__ == "__main__":
    unittest.main()
