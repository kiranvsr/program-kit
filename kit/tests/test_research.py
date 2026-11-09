"""0.6.0: the research track: the `research` section of checks.yaml and `pk research init | list | check | graduate`."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import shutil
import unittest
from datetime import date
from pathlib import Path

import drift
import pk
import research
import rulings
from common import KitError, write_yaml
from test_worktree import commit_all, make_repo, run_cli, sh

KIT = Path(__file__).resolve().parent.parent
TODAY = date.today().isoformat()
# the index format of the program the track came from: two tables, links relative to the index, one archived row
INDEX = """# Research (not for implementation)

Isolated: nothing here is a task.

## Items

| ID | Item | Status |
| --- | --- | --- |
| RI-01 | [Positioning](positioning.md) | open, deliberating |
| RI-02 | [Lead scoring](lead-scoring.md) | not graduated (decided, R3 pending) |
| RI-03 | [Workflow](workflow.md) | graduated by R2 (2026-11-01) as CR-001 |
| RI-04 | [Roadmap (draft)](roadmap.md) | draft, depends on RI-01 to RI-03 |
| RI-06 | [Later slices](later.md) | graduated by R1 (2026-11-01) |

## Graduated items (moved out of research)

| ID | Item | Status |
| --- | --- | --- |
| RI-05 | [Integration](../docs/research-archive/RI-05-integration.md) | graduated by R2 (2026-11-02), amended by CR-002 (R9) after a spike; archived |

Status values: `open`, `graduated` (moved into the pipeline by a ruling), `dropped`.
"""
CONFIG = {"archive": "docs/research-archive", "other_ids": ["RD-[0-9]+", "H-[0-9]+"]}


class ResearchBase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        self.checks(CONFIG)
        rulings.add(self.root, "controller", "Build the later slices?", "No: something else entirely.")  # R1 does not name it
        rulings.add(self.root, "controller", "Build the workflow item?", "Yes: RI-03 and RI-05 become CR-001.")  # R2
        self.write("research/README.md", INDEX)
        for name in ("positioning", "lead-scoring", "workflow", "roadmap", "later"):
            self.write(f"research/{name}.md", f"# {name}\n")
        self.write("docs/research-archive/RI-05-integration.md", "# integration\n")
        self.write("program/tasks/T1/SPEC.md", "# Spec\n\nNothing cited.\n")
        commit_all(self.root, "research")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def checks(self, section=None):
        data = {"build": [], "test": [], "scan": []}
        if section is not None:
            data["research"] = section
        write_yaml(self.root / "program" / "checks.yaml", data)

    def cli(self, *argv):
        return run_cli("research", self.root, "research", *argv)


class ConfigTests(ResearchBase):
    def test_absent_section_is_none_and_every_default_is_filled(self):
        self.assertIsNone(research.parse(None))
        cfg = research.parse({})
        self.assertEqual((cfg["folder"], cfg["index"], cfg["archive"], cfg["item_pattern"], cfg["other_ids"]),
                         ("research", "research/README.md", None, "RI-[0-9]+", []))
        self.assertEqual(cfg["guard"], list(research.DEFAULT_GUARD))
        self.assertEqual(research.parse({"folder": "notes/ideas"})["index"], "notes/ideas/README.md")

    def test_a_bad_key_or_value_is_refused_with_its_name(self):
        for value, needle in (([], "research must be a mapping"), ({"colour": 1}, "unknown key colour"),
                              ({"folder": "/abs"}, "research.folder"), ({"item_pattern": "RI-["}, "item_pattern"),
                              ({"item_pattern": "x*"}, "matches an empty"), ({"other_ids": "RD"}, "other_ids"),
                              ({"guard": []}, "research.guard"), ({"archive": "../out"}, "research.archive")):
            with self.subTest(value=value), self.assertRaisesRegex(KitError, needle):
                research.parse(value)

    def test_doctor_reads_the_section_through_the_gate(self):
        self.assertEqual(pk._checks_problems({"build": [["x"]], "test": [["x"]], "scan": [["x"]], "research": CONFIG}), [])
        problems = pk._checks_problems({"build": [["x"]], "test": [["x"]], "scan": [["x"]], "research": {"bogus": 1}})
        self.assertTrue(any("research: unknown key bogus" in line for line in problems), problems)

    def test_without_the_section_every_command_says_so_and_changes_nothing(self):
        self.checks()
        commit_all(self.root, "no research")
        for argv in (("init",), ("list",), ("check",), ("graduate", "RI-01", "--ruling", "R2")):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("research is not configured", err)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "")


class IndexTests(ResearchBase):
    def test_the_index_of_the_original_format_parses_with_links_resolved_against_it(self):
        items = research.items(self.root, research.config(self.root))
        self.assertEqual(list(items), ["RI-01", "RI-02", "RI-03", "RI-04", "RI-06", "RI-05"])
        self.assertEqual(items["RI-01"]["file"], "research/positioning.md")
        self.assertEqual(items["RI-05"]["file"], "docs/research-archive/RI-05-integration.md")

    def test_graduated_needs_the_word_a_ruling_that_exists_and_names_the_item(self):
        items = research.items(self.root, research.config(self.root))
        self.assertIsNone(items["RI-03"]["why"])
        self.assertIsNone(items["RI-05"]["why"])  # the first ruling after the word decides, not R9
        self.assertIn("not marked graduated", items["RI-01"]["why"])
        self.assertIn("not marked graduated", items["RI-02"]["why"])  # "not graduated" is not graduated
        self.assertIn("R1 does not name RI-06", items["RI-06"]["why"])
        self.write("research/README.md", INDEX.replace("graduated by R2 (2026-11-01)", "graduated by R7 (2026-11-01)"))
        self.assertIn("R7 is not in program/rulings.md", research.items(self.root, research.config(self.root))
                      ["RI-03"]["why"])

    def test_list_prints_each_item_with_its_state(self):
        code, out, _ = self.cli("list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("RI-03\tgraduated\tresearch/workflow.md\tgraduated by R2 (2026-11-01) as CR-001", lines)
        self.assertTrue(any(line.startswith("RI-01\topen\t") for line in lines), lines)


class GuardTests(ResearchBase):
    def check(self, *files):
        return self.cli("check", *files)

    def test_a_clean_tree_passes(self):
        code, out, _ = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("research check: ok, 1 file(s) cite no ungraduated research", out)

    def test_ungraduated_items_paths_and_other_ids_are_refused_by_line(self):
        self.write("program/tasks/T1/PLAN.md", "\n".join([
            "# Plan",
            "Builds on RI-03 (graduated) and research/workflow.md: fine.",
            "Uses RI-01 for the copy.",
            "See research/positioning.md for the audience.",
            "The research decision RD-4 and H-2 say so.",
            "The decision log in research/decision-log.md agrees.",
            "RD-4 beside RI-03 is fine.",
            "Case RI-4 and RD-10 away from it are case labels, as is case:RI-01.",
            "RI-09 is no item of the research index.",
            "", ]))
        self.write("program/briefs/phase-1/brief.md", "Read ../research/README.md first.\n")
        code, out, _ = self.check()
        self.assertEqual(code, 1)
        found = [line for line in out.splitlines() if line.startswith("program/")]
        self.assertEqual(found, [
            "program/briefs/phase-1/brief.md:1: ../research/README.md cites research/ with no graduated research "
            "item on the line",
            "program/tasks/T1/PLAN.md:3: RI-01 is not marked graduated with a ruling in research/README.md "
            "(status: open, deliberating)",
            "program/tasks/T1/PLAN.md:4: research/positioning.md (RI-01): RI-01 is not marked graduated with a "
            "ruling in research/README.md (status: open, deliberating)",
            "program/tasks/T1/PLAN.md:5: RD-4 is a research ID with no graduated research item on the line",
            "program/tasks/T1/PLAN.md:5: H-2 is a research ID with no graduated research item on the line",
            "program/tasks/T1/PLAN.md:6: research/decision-log.md cites research/ with no graduated research item "
            "on the line",
            "program/tasks/T1/PLAN.md:9: RI-09 is not listed in research/README.md",
        ])
        self.assertIn("research check: 7 refused in 3 file(s)", out)

    def test_named_files_are_checked_and_the_research_folder_is_skipped(self):
        bad = self.write("notes.md", "RI-01 is the plan.\n")
        code, out, _ = self.check(str(bad), str(self.root / "research" / "positioning.md"))
        self.assertEqual(code, 1)
        self.assertIn("notes.md:1: RI-01 is not marked graduated", out)
        self.assertIn("research/positioning.md: skipped (the research folder itself)", out)
        self.assertEqual(self.check("missing.md")[0], 1)

    def test_drift_runs_the_guard_as_d8_only_when_configured(self):
        self.write("program/tasks/T1/BRIEF.md", "Start from RI-01.\n")
        commit_all(self.root, "cite")
        _, results = drift.report(self.root)
        rows = {cid: (ok, detail) for cid, ok, detail in results}
        self.assertFalse(rows["D8"][0])
        self.assertIn("program/tasks/T1/BRIEF.md:1: RI-01", rows["D8"][1])
        self.checks()
        commit_all(self.root, "no research")
        self.assertEqual([cid for cid, _, _ in drift.report(self.root)[1]], ["D1", "D2", "D3", "D4", "D5", "D6", "D7"])


class GraduateTests(ResearchBase):
    def test_graduate_checks_the_ruling_and_writes_the_status_cell(self):
        code, _, err = self.cli("graduate", "RI-01", "--ruling", "R1")
        self.assertEqual(code, 1)
        self.assertIn("R1 does not name RI-01", err)
        code, _, err = self.cli("graduate", "RI-01", "--ruling", "R5")
        self.assertIn("R5 is not in program/rulings.md", err)
        rulings.add(self.root, "controller", "Build positioning?", "Yes: RI-01 becomes CR-003.")  # R3
        code, out, err = self.cli("graduate", "RI-01", "--ruling", "R3")
        self.assertEqual((code, err), (0, ""))
        text = (self.root / "research" / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"| RI-01 | [Positioning](positioning.md) | graduated by R3 ({TODAY}) |", text)
        self.assertIsNone(research.items(self.root, research.config(self.root))["RI-01"]["why"])
        self.assertEqual(self.cli("graduate", "RI-77", "--ruling", "R3")[0], 1)

    def test_archive_moves_the_file_with_git_and_fixes_the_link(self):
        rulings.add(self.root, "controller", "Build positioning?", "Yes: RI-01 becomes CR-003.")  # R3
        code, out, err = self.cli("graduate", "RI-01", "--ruling", "R3", "--archive")
        self.assertEqual((code, err), (0, ""), out)
        self.assertFalse((self.root / "research" / "positioning.md").exists())
        self.assertTrue((self.root / "docs" / "research-archive" / "positioning.md").is_file())
        self.assertIn("R  research/positioning.md -> docs/research-archive/positioning.md",
                      sh(self.root, "git", "status", "--porcelain"))
        text = (self.root / "research" / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"| RI-01 | [Positioning](../docs/research-archive/positioning.md) | graduated by R3 ({TODAY}) |",
                      text)
        self.assertEqual(research.items(self.root, research.config(self.root))["RI-01"]["file"],
                         "docs/research-archive/positioning.md")

    def test_archive_without_a_configured_archive_is_refused_and_changes_nothing(self):
        self.checks({})
        commit_all(self.root, "no archive")
        rulings.add(self.root, "controller", "Build positioning?", "Yes: RI-01 becomes CR-003.")
        commit_all(self.root, "R3")
        code, _, err = self.cli("graduate", "RI-01", "--ruling", "R3", "--archive")
        self.assertEqual(code, 1)
        self.assertIn("research.archive", err)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "")


class InitTests(unittest.TestCase):
    def test_init_writes_the_index_from_the_template_once(self):
        root = make_repo(self)
        write_yaml(root / "program" / "checks.yaml", {"build": [], "test": [], "scan": [], "research": {}})
        code, out, err = run_cli("research", root, "research", "init")
        self.assertEqual((code, err), (0, ""))
        text = (root / "research" / "README.md").read_text(encoding="utf-8")
        self.assertEqual(text, (KIT / "templates" / "research-README.md").read_text(encoding="utf-8"))
        self.assertEqual(research.items(root, research.config(root)), {})
        self.assertEqual(run_cli("research", root, "research", "init")[0], 1)  # never overwrites
        shutil.rmtree(root / "research")


if __name__ == "__main__":
    unittest.main()
