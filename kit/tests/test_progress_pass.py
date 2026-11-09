"""0.6.0: `pk progress`, the controller's one-call progress pass: replace current-state text in the progress docs,
refresh the hand-off (and the dashboard), scan copies, commit exactly what it changed, and leave D7 clean."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import sys
import unittest
from datetime import date

import progress
import rulings
from common import KitError
from test_progress import PROGRESS, T1, ProgressBase
from test_worktree import commit_all, run_cli, sh

TODAY = date.today().isoformat()
DASHBOARD = """# Dashboard

## Where we are

| Item | State |
|---|---|
| Stage | building phase 1 |
| In flight | nothing |

## Decisions the user owes

1. Pick a name.
2. Pick a price.

Text after the list stays.

## History

- 2026-11-01: started
"""
SUCCESSOR = """# Successor

*Latest ruling R0 (next R1); keep this short.*

- **Release:** none yet
- **Next:** dispatch T1
"""
HANDOFF_STEPS = "## Next steps\n\n1. Read the rules.\n2. Dispatch T1.\n"


class PassBase(ProgressBase):
    def setUp(self):
        super().setUp()
        self.checks(dict(PROGRESS, history={"doc": "program/DASHBOARD.md", "heading": "History"}))
        self.write("program/DASHBOARD.md", DASHBOARD)
        self.write("program/SUCCESSOR_PROMPT.md", SUCCESSOR)
        hand = self.root / "program" / "HANDOFF.md"
        hand.write_text(hand.read_text(encoding="utf-8").replace("## Next steps\n", HANDOFF_STEPS), encoding="utf-8")
        commit_all(self.root, "docs")
        run_cli("handoff", self.root, "handoff", "update")
        if sh(self.root, "git", "status", "--porcelain"):
            commit_all(self.root, "hand-off")

    def run_pass(self, *argv):
        return run_cli("progress", self.root, "progress", *argv)

    def text(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")


class ConfigTests(PassBase):
    def test_without_the_section_it_refuses_with_how_to_configure_it(self):
        self.checks()
        commit_all(self.root, "no section")
        code, _, err = self.run_pass("--row", "Stage=x")
        self.assertEqual(code, 1)
        self.assertIn("progress section", err)
        self.assertIn("docs", err)
        self.assertEqual(self.text("program/DASHBOARD.md"), DASHBOARD)

    def test_the_new_keys_are_parsed_and_d7_reads_as_before(self):
        cfg = progress.parse(dict(PROGRESS, history={"doc": "program/DASHBOARD.md", "heading": "History"},
                                  scan=[["scanner", "dir", "."]]))
        self.assertEqual((cfg["history"], cfg["scan"]), ({"doc": "program/DASHBOARD.md", "heading": "History"},
                                                         [["scanner", "dir", "."]]))
        old = progress.parse(PROGRESS)
        self.assertEqual((old["docs"], old["state"], old["quiet_when"], old["history"], old["scan"]),
                         (PROGRESS["docs"], PROGRESS["state"], None, None, []))
        for bad in ({"history": "History"}, {"history": {"doc": "../x", "heading": "H"}},
                    {"history": {"doc": "x.md"}}, {"history": {"doc": "x.md", "heading": "H", "z": 1}},
                    {"scan": ["scanner dir ."]}, {"scan": [[]]}):
            with self.subTest(bad=bad), self.assertRaises(KitError):
                progress.parse(dict(PROGRESS, **bad))


class EditTests(PassBase):
    def test_each_edit_replaces_current_state_text_in_place(self):
        decisions = self.root.parent / f"{self.root.name}-decisions.txt"
        decisions.write_text("Approve the change.\n\n2. Confirm the plan.\n", encoding="utf-8")
        self.addCleanup(decisions.unlink, missing_ok=True)
        code, out, err = self.run_pass("--row", "Stage=building phase 2", "--bullet", "Next=dispatch T2",
                                       "--item", "Next steps#2=Dispatch T2.", "--list",
                                       f"Decisions the user owes={decisions}", "--history", "T1 merged")
        self.assertEqual((code, err), (0, ""), out)
        dash = self.text("program/DASHBOARD.md")
        self.assertIn("| Stage | building phase 2 |\n| In flight | nothing |", dash)
        self.assertIn("## Decisions the user owes\n\n1. Approve the change.\n2. Confirm the plan.\n\nText after the "
                      "list stays.", dash)
        self.assertIn(f"## History\n\n- {TODAY}: T1 merged\n- 2026-11-01: started\n", dash)
        self.assertIn("- **Next:** dispatch T2\n", self.text("program/SUCCESSOR_PROMPT.md"))
        self.assertIn("1. Read the rules.\n2. Dispatch T2.\n", self.text("program/HANDOFF.md"))
        self.assertIn("handoff: current", out)

    def test_a_missing_or_ambiguous_target_is_refused_and_nothing_is_written(self):
        before = {rel: self.text(rel) for rel in ("program/DASHBOARD.md", "program/SUCCESSOR_PROMPT.md",
                                                   "program/HANDOFF.md")}
        self.write("program/SUCCESSOR_PROMPT.md", SUCCESSOR + "\n| Stage | elsewhere |\n")
        before["program/SUCCESSOR_PROMPT.md"] = self.text("program/SUCCESSOR_PROMPT.md")
        for argv, needle in ((("--row", "Nope=x"), "no table row named 'Nope'"),
                             (("--row", "Stage=x"), "'Stage' is in more than one place"),
                             (("--bullet", "Risk=x"), "no bullet '- **Risk:**'"),
                             (("--item", "Next steps#7=x"), "no item 7 under 'Next steps'"),
                             (("--item", "Nowhere#1=x"), "no heading 'Nowhere'"),
                             (("--item", "Next steps=x"), "HEADING#N=TEXT"),
                             (("--list", "Decisions the user owes=/no/such/file"), "cannot read"),
                             (("--row", "In flight=a | b"), "'|'"),
                             (("--row", "In flight=line one\nline two"), "one line"),
                             (("--row", "In flight=x", "--history", "x", "--bullet", "Gone=x"), "no bullet")):
            with self.subTest(argv=argv):
                code, _, err = self.run_pass(*argv)
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
                for rel, text in before.items():
                    self.assertEqual(self.text(rel), text, rel)

    def test_history_without_its_key_is_refused(self):
        self.checks(PROGRESS)
        commit_all(self.root, "no history key")
        code, _, err = self.run_pass("--history", "x")
        self.assertEqual(code, 1)
        self.assertIn("progress.history", err)

    def test_the_generated_block_of_the_hand_off_is_never_a_target(self):
        hand = self.root / "program" / "HANDOFF.md"
        hand.write_text(hand.read_text(encoding="utf-8").replace(
            "<!-- generated:end -->", "- **Inside:** generated\n<!-- generated:end -->"), encoding="utf-8")
        code, _, err = self.run_pass("--bullet", "Inside=x")
        self.assertEqual(code, 1)
        self.assertIn("no bullet '- **Inside:**'", err)

    def test_the_latest_ruling_phrase_follows_the_rulings_log(self):
        rulings.add(self.root, "controller", "Q one?", "D one.")
        rulings.add(self.root, "controller", "Q two?", "D two.")
        self.assertEqual(self.run_pass()[0], 0)
        self.assertIn("*Latest ruling R2 (next R3); keep this short.*", self.text("program/SUCCESSOR_PROMPT.md"))

    def test_dashboard_is_written_when_asked(self):
        out = self.root.parent / f"{self.root.name}-dash.html"
        self.addCleanup(out.unlink, missing_ok=True)
        code, text, _ = self.run_pass("--dashboard", str(out))
        self.assertEqual(code, 0)
        self.assertIn("<title>Program dashboard</title>", out.read_text(encoding="utf-8"))


class CommitTests(PassBase):
    def test_commit_takes_exactly_the_docs_and_bookkeeping_and_leaves_d7_clean(self):
        self.state_commit(T1, "in_progress", "dispatch T1")
        self.assertFalse(progress.behind(self.root, progress.config(self.root))["ok"])
        rulings.add(self.root, "controller", "Go?", "Go.")  # bookkeeping: committed with the docs
        self.write("src/app.py", "print(1)\n")  # not the pass's: stays uncommitted
        code, out, err = self.run_pass("--row", "In flight=T1", "--bullet", "Next=T1 gate",
                                       "--commit", "chore(program): progress", "--trailer", "Signed-off-by: A <a@b>")
        self.assertEqual((code, err), (0, ""), out)
        files = sh(self.root, "git", "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(files), ["program/DASHBOARD.md", "program/HANDOFF.md", "program/SUCCESSOR_PROMPT.md",
                                         "program/rulings.md"])
        message = sh(self.root, "git", "log", "-1", "--format=%B")
        self.assertEqual(message.strip(), "chore(program): progress\n\nSigned-off-by: A <a@b>")
        self.assertEqual(sh(self.root, "git", "status", "--porcelain"), "?? src/\n")
        self.assertTrue(progress.behind(self.root, progress.config(self.root))["ok"])
        _, report, _ = run_cli("drift", self.root, "drift")
        self.assertRegex(report, r"(?m)^  D7 ok +progress docs current")
        self.assertRegex(report, r"(?m)^  D3 ok ")
        self.assertEqual(sh(self.root, "git", "remote"), "")  # nothing to push to, and nothing tried

    def test_an_unchanged_doc_behind_state_refuses_the_commit_before_writing(self):
        self.state_commit(T1, "in_progress", "dispatch T1")
        code, _, err = self.run_pass("--row", "In flight=T1", "--commit", "progress")
        self.assertEqual(code, 1)
        self.assertIn("program/SUCCESSOR_PROMPT.md", err)
        self.assertIn("behind", err)
        self.assertEqual(self.text("program/DASHBOARD.md"), DASHBOARD)

    def test_commit_off_the_main_branch_is_refused(self):
        sh(self.root, "git", "switch", "-q", "-c", "side")
        code, _, err = self.run_pass("--row", "In flight=T1", "--bullet", "Next=x", "--commit", "progress")
        self.assertEqual(code, 1)
        self.assertIn("main", err)

    def test_a_failing_scan_over_copies_refuses_the_commit(self):
        probe = ("import pathlib, sys; t = pathlib.Path('program/DASHBOARD.md').read_text(); "
                 "sys.exit(3 if 'TOKEN' in t else 0)")
        self.checks(dict(PROGRESS, scan=[[sys.executable, "-c", probe]]))
        commit_all(self.root, "scan")
        code, out, err = self.run_pass("--row", "In flight=TOKEN", "--bullet", "Next=x", "--commit", "progress")
        self.assertEqual(code, 1)
        self.assertIn("scan", err)
        self.assertIn("nothing committed", err)
        self.assertEqual(sh(self.root, "git", "log", "-1", "--format=%s").strip(), "scan")
        code, out, err = self.run_pass("--row", "In flight=clean", "--bullet", "Next=x", "--commit", "progress")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("scan: ok", out)


if __name__ == "__main__":
    unittest.main()
