import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import re
import shutil
import unittest
from pathlib import Path

import cost
import dashboard
import status
from common import KitError, write_json, write_yaml
from test_register import reg, task
from test_worktree import commit_all, make_repo, run_cli

KIT = Path(__file__).resolve().parent.parent
T1, T2, T3 = "P1-M01-S01-T01", "P1-M01-S01-T02", "P1-M01-S01-T03"


class DashboardTests(unittest.TestCase):
    """KF-3: `pk dashboard OUT.html` writes the kit's view of the state as one self-contained page."""

    def setUp(self):
        self.root = make_repo(self)
        program = self.root / "program"
        program.mkdir()
        shutil.copy(KIT / "templates" / "HANDOFF.md", program / "HANDOFF.md")
        shutil.copy(KIT / "templates" / "controller-state.json", program / "controller-state.json")
        write_yaml(program / "register.yaml", reg(task(T1, title="Sign <in> & out"), task(T2), task(T3)))
        write_yaml(program / "gates.yaml", {"gates": {"G-P1": {"title": "Phase 1 exit", "claims": ["it works"],
                                                               "environment": "dev",
                                                               "records": [{"task": T1, "acceptance": "ok"}]}}})
        commit_all(self.root, "program")
        status.transition(self.root, T1, "in_progress", "controller")
        cost.add(self.root, T1, "implementer", "model-a", 1200)
        cost.add(self.root, T1, "qa", "model-b", 300)
        self.assertEqual(run_cli("tracker", self.root, "followups", "add", "--area", "ci", "--item", "pin it")[0], 0)
        commit_all(self.root, "state")
        self.out = self.root.parent / f"{self.root.name}-dashboard.html"
        self.addCleanup(self.out.unlink, missing_ok=True)

    def test_the_page_holds_the_state_and_nothing_external(self):
        code, out, err = run_cli("dashboard", self.root, "dashboard", str(self.out))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"dashboard: wrote {self.out} (3 tasks, 1 gates, 1,500 tokens logged)\n")
        page = self.out.read_text(encoding="utf-8")
        for needle in (T1, T2, T3, "Sign &lt;in&gt; &amp; out", "G-P1", "Phase 1 exit", "model-a", "1,500",
                       "F-0001", "pin it", "#### Now"[5:], "Rulings (0; next R1)", "in progress", "not started"):
            self.assertIn(needle, page)
        self.assertNotIn("Sign <in>", page)  # text from the state is escaped
        for external in ("<script", "<link", "src=", "@import", "url(", "//"):
            self.assertNotIn(external, page.lower().replace("<!doctype", ""))
        self.assertIsNone(re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d", page))  # no wall-clock time

    def test_the_same_state_gives_the_same_bytes_and_nothing_tracked_changes(self):
        run_cli("dashboard", self.root, "dashboard", str(self.out))
        first = self.out.read_bytes()
        run_cli("dashboard", self.root, "dashboard", str(self.out))
        self.assertEqual(self.out.read_bytes(), first)
        self.assertEqual(run_cli("handoff", self.root, "handoff", "check")[0], 1)  # the hand-off file was not written
        status_before = sorted(p.name for p in (self.root / "program").iterdir())
        dashboard.render(self.root)
        self.assertEqual(sorted(p.name for p in (self.root / "program").iterdir()), status_before)

    def test_a_program_before_its_register_still_gets_a_page(self):
        (self.root / "program" / "register.yaml").unlink()
        (self.root / "program" / "gates.yaml").unlink()
        page, summary = dashboard.render(self.root)
        self.assertEqual(summary, {"tasks": 0, "gates": 0, "tokens": 1500})
        self.assertIn("No register yet", page)
        self.assertIn("No gates in program/gates.yaml yet", page)

    def test_a_missing_directory_or_a_directory_target_is_one_error(self):
        code, out, err = run_cli("dashboard", self.root, "dashboard", str(self.root / "nope" / "d.html"))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("does not exist", err)
        code, _, err = run_cli("dashboard", self.root, "dashboard", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("is a directory", err)

    def test_a_state_that_does_not_read_is_the_hand_offs_error(self):
        write_json(self.root / "program" / "controller-state.json", [])
        with self.assertRaises(KitError):
            dashboard.render(self.root)


if __name__ == "__main__":
    unittest.main()
