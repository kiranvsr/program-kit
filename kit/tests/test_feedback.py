"""0.6.0: the feedback register `program/beta-feedback.md`: `pk feedback add | set | list | check`, the dashboard's
Feedback panel and the hand-off's one line."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import re
import shutil
import unittest
from datetime import date
from pathlib import Path

import dashboard
import feedback
import handoff
from common import KitError
from test_worktree import commit_all, make_repo, run_cli

KIT = Path(__file__).resolve().parent.parent
TODAY = date.today().isoformat()
# the register's shape in the program it came from: a free header, then the table with no row yet
ORIGINAL = """# Beta feedback log

The user's feedback messages, one row each. The controller adds a row when the message arrives and
updates it as the item moves.

- **BF**: `BF-n`, numbered in order of arrival, never reused.
- **Urgency**: `urgent`, `high` or `polish`.

| BF | Date | Urgency | Summary | Touches | Decision | State | Ruling |
|---|---|---|---|---|---|---|---|
"""
ROWS = """| BF-1 | 2026-11-01 | polish | Softer colours | none | batched | done | R4 |
| BF-2 | 2026-11-02 | high | Export the scores | T2 | follow-up | open | - |
| BF-3 | 2026-11-03 | urgent | Join fails on a phone | T1, T3 | fold-in | in progress | R5, R6 |
| BF-4 | 2026-11-04 | polish | Rename the button | T1 | batched | declined | R7 |
| BF-5 | 2026-11-05 | urgent | Second urgent item | none | pull-forward | open | - |
"""


class FeedbackBase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        program = self.root / "program"
        program.mkdir()
        shutil.copy(KIT / "templates" / "HANDOFF.md", program / "HANDOFF.md")
        shutil.copy(KIT / "templates" / "controller-state.json", program / "controller-state.json")
        commit_all(self.root, "program")
        self.path = program / "beta-feedback.md"

    def cli(self, *argv):
        return run_cli("feedback", self.root, "feedback", *argv)


class RegisterTests(FeedbackBase):
    def test_the_original_file_validates_unchanged(self):
        self.path.write_text(ORIGINAL, encoding="utf-8")
        self.assertEqual(feedback.read(self.root), [])
        self.assertEqual(self.cli("check")[:2], (0, "feedback: ok, 0 row(s), 0 open\n"))
        self.path.write_text(ORIGINAL + ROWS, encoding="utf-8")
        rows = feedback.read(self.root)
        self.assertEqual([r["bf"] for r in rows], ["BF-1", "BF-2", "BF-3", "BF-4", "BF-5"])
        self.assertEqual(rows[2]["ruling"], "R5, R6")
        self.assertEqual(self.path.read_text(encoding="utf-8"), ORIGINAL + ROWS)

    def test_a_malformed_row_names_the_row_and_the_field(self):
        for old, new, needle in (("| high | Export", "| soon | Export", "BF-2: urgency 'soon' is not urgent | high | polish"),
                                 ("| 2026-11-02 |", "| 2 Nov |", "BF-2: date '2 Nov' is not YYYY-MM-DD"),
                                 ("| follow-up | open |", "| later | open |", "BF-2: decision 'later'"),
                                 ("| open | - |\n| BF-3", "| waiting | - |\n| BF-3", "BF-2: state 'waiting'"),
                                 ("| open | - |\n| BF-3", "| open | 12 |\n| BF-3", "BF-2: ruling '12' is not R<n>"),
                                 ("| T2 | follow-up", "| follow-up", "BF-2: 7 columns, the register has 8"),
                                 ("| BF-4 |", "| BF-2 |", "BF-2: duplicate BF number")):
            with self.subTest(needle=needle):
                self.path.write_text(ORIGINAL + ROWS.replace(old, new, 1), encoding="utf-8")
                with self.assertRaises(KitError) as caught:
                    feedback.read(self.root)
                self.assertIn("program/beta-feedback.md: " + needle, str(caught.exception))
                self.assertEqual(self.cli("check")[0], 1)

    def test_add_creates_the_file_from_the_template_and_numbers_in_order(self):
        code, out, err = self.cli("add", "--urgency", "high", "--summary", "The timer is too fast")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "feedback: added BF-1 (high, follow-up, open)\n")
        text = self.path.read_text(encoding="utf-8")
        template = (KIT / "templates" / "beta-feedback.md").read_text(encoding="utf-8")
        self.assertEqual(text, template + f"| BF-1 | {TODAY} | high | The timer is too fast | none | follow-up | open | - |\n")
        self.cli("add", "--urgency", "urgent", "--summary", "Crash on join", "--touches", "T1,T2",
                 "--decision", "pull-forward")
        rows = feedback.read(self.root)
        self.assertEqual((rows[1]["bf"], rows[1]["touches"], rows[1]["decision"]), ("BF-2", "T1, T2", "pull-forward"))

    def test_add_puts_the_row_after_the_last_row_and_keeps_text_below_the_table(self):
        self.path.write_text(ORIGINAL + ROWS + "\nNotes below the table.\n", encoding="utf-8")
        self.assertEqual(self.cli("add", "--urgency", "polish", "--summary", "Wider margins")[0], 0)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(ROWS + f"| BF-6 | {TODAY} | polish | Wider margins | none | batched | open | - |\n\nNotes below", text)

    def test_add_and_set_refuse_bad_values_and_write_nothing(self):
        self.path.write_text(ORIGINAL + ROWS, encoding="utf-8")
        for argv in (("add", "--urgency", "soon", "--summary", "x"), ("add", "--urgency", "high", "--summary", "a | b"),
                     ("add", "--urgency", "high", "--summary", " "), ("set", "BF-9", "--state", "done"),
                     ("set", "BF-2", "--state", "finished"), ("set", "BF-2", "--ruling", "12"),
                     ("set", "BF-2",)):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0], 1)
                self.assertEqual(self.path.read_text(encoding="utf-8"), ORIGINAL + ROWS)

    def test_set_replaces_only_the_named_cells(self):
        self.path.write_text(ORIGINAL + ROWS, encoding="utf-8")
        code, out, _ = self.cli("set", "BF-2", "--state", "done", "--ruling", "R8", "--touches", "T2,T4")
        self.assertEqual(code, 0)
        self.assertIn("| BF-2 | 2026-11-02 | high | Export the scores | T2, T4 | follow-up | done | R8 |",
                      self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.path.read_text(encoding="utf-8").count("\n"), (ORIGINAL + ROWS).count("\n"))

    def test_list_shows_every_row_or_only_the_open_ones(self):
        self.path.write_text(ORIGINAL + ROWS, encoding="utf-8")
        _, out, _ = self.cli("list", "--open")
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["BF-3", "BF-5", "BF-2"])
        _, out, _ = self.cli("list")
        self.assertEqual(len(out.splitlines()), 5)
        self.assertIn("BF-3 urgent in progress fold-in touches T1, T3 R5, R6: Join fails on a phone", out)


class PanelTests(FeedbackBase):
    def out(self):
        out = self.root.parent / f"{self.root.name}-dashboard.html"
        self.addCleanup(out.unlink, missing_ok=True)
        return out

    def test_the_dashboard_shows_open_by_urgency_then_number_and_hides_declined(self):
        self.path.write_text(ORIGINAL + ROWS, encoding="utf-8")
        page, _ = dashboard.render(self.root)
        panel = page[page.index("<h2>Feedback</h2>"):]
        order = [m for m in re.findall(r"BF-\d", panel)]
        self.assertEqual(order[:4], ["BF-3", "BF-5", "BF-2", "BF-1"])  # open by urgency then number, then done
        self.assertNotIn("BF-4", page)
        self.assertIn("3 open, 1 done", panel)

    def test_only_the_five_most_recently_done_are_shown(self):
        rows = "".join(f"| BF-{n} | 2026-10-{n:02d} | polish | item {n} | none | batched | done | - |\n" for n in range(1, 9))
        self.path.write_text(ORIGINAL + rows, encoding="utf-8")
        page, _ = dashboard.render(self.root)
        self.assertEqual(re.findall(r"item (\d)", page), ["8", "7", "6", "5", "4"])

    def test_a_malformed_row_fails_the_dashboard_and_no_file_means_no_panel(self):
        self.path.write_text(ORIGINAL + ROWS.replace("| high |", "| soon |"), encoding="utf-8")
        code, _, err = run_cli("dashboard", self.root, "dashboard", str(self.out()))
        self.assertEqual(code, 1)
        self.assertIn("BF-2: urgency 'soon'", err)
        self.path.unlink()
        page, _ = dashboard.render(self.root)
        self.assertNotIn("Feedback", page)


class HandoffLineTests(FeedbackBase):
    def test_the_hand_off_counts_open_items_by_urgency_only_when_the_file_exists(self):
        before = handoff.render(self.root)
        self.assertNotIn("Feedback", before)
        self.path.write_text(ORIGINAL + ROWS, encoding="utf-8")
        block = handoff.render(self.root)
        self.assertIn("- Feedback (program/beta-feedback.md): 3 open: 2 urgent, 1 high, 0 polish", block)
        self.assertEqual(block.replace(
            "- Feedback (program/beta-feedback.md): 3 open: 2 urgent, 1 high, 0 polish\n", ""), before)
        self.path.write_text(ORIGINAL + ROWS.replace("| high |", "| soon |"), encoding="utf-8")
        self.assertIn("does not read; run pk feedback check", handoff.render(self.root))


if __name__ == "__main__":
    unittest.main()
