import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
import rulings
from common import KitError

HEADER = "# Rulings\n\nDecisions that bind the program, newest last.\n"


class RulingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        (self.root / "program").mkdir()
        self.path = self.root / "program" / "rulings.md"

    def seed(self, text=HEADER):
        self.path.write_bytes(text.encode("utf-8"))

    def block(self, n, decision="Use X.", date="2026-01-02", role="controller"):
        return (f"\n## R{n} ({date}, {role})\n\n**Question.** Which?\n\n**Decision.** {decision}\n\n"
                f"**Precedent.** Always.\n")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("pk.find_root", return_value=self.root), \
                mock.patch("rulings.find_root", return_value=self.root):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # add / read
    def test_add_numbers_sequentially_and_dates(self):
        self.seed()
        with mock.patch("rulings.utc_now", return_value="2026-10-06T12:00:00Z"):
            self.assertEqual(rulings.add(self.root, "controller", "Q one?", "D one.", "P one."), 1)
            self.assertEqual(rulings.add(self.root, "qa", "Q two?", "D two; supersedes R1."), 2)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("## R1 (2026-10-06, controller)\n\n**Question.** Q one?\n\n**Decision.** D one.\n\n"
                      "**Precedent.** P one.\n", text)
        self.assertIn("## R2 (2026-10-06, qa)", text)
        self.assertEqual(rulings.read(self.root), [
            {"number": 1, "date": "2026-10-06", "role": "controller", "question": "Q one?",
             "decision": "D one.", "precedent": "P one.", "supersedes": []},
            {"number": 2, "date": "2026-10-06", "role": "qa", "question": "Q two?",
             "decision": "D two; supersedes R1.", "precedent": "", "supersedes": [1]},
        ])
        self.assertEqual(rulings.validate(self.root), [])

    def test_header_only_file_reads_empty_and_validates(self):
        self.seed()
        self.assertEqual(rulings.read(self.root), [])
        self.assertEqual(rulings.validate(self.root), [])

    def test_add_creates_a_missing_file_and_multiline_text_round_trips(self):
        self.assertEqual(rulings.add(self.root, "controller", "Q\nsecond line", "D\n\nmore"), 1)
        (ruling,) = rulings.read(self.root)
        self.assertEqual((ruling["question"], ruling["decision"]), ("Q\nsecond line", "D\n\nmore"))

    def test_supersedes_found_anywhere_in_decision(self):
        self.seed(HEADER + self.block(1) + self.block(2, "First line.\nThen it supersedes R1 here."))
        self.assertEqual(rulings.read(self.root)[1]["supersedes"], [1])

    def test_add_is_append_only(self):
        original = (HEADER + self.block(1)).replace("\n", "\r\n")  # odd bytes must survive untouched
        self.seed(original)
        before = self.path.read_bytes()
        self.assertEqual(rulings.add(self.root, "controller", "Q?", "D."), 2)
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))
        no_newline = HEADER.rstrip("\n")
        self.seed(no_newline)
        rulings.add(self.root, "controller", "Q?", "D.")
        self.assertTrue(self.path.read_bytes().startswith(no_newline.encode()))

    # validate
    def test_validate_detects_gap_and_duplicate(self):
        self.seed(HEADER + self.block(1) + self.block(3))
        problems = rulings.validate(self.root)
        self.assertEqual(len(problems), 1)
        self.assertRegex(problems[0], r"R3.*R2 expected")
        self.seed(HEADER + self.block(1) + self.block(2) + self.block(2))
        problems = rulings.validate(self.root)
        self.assertEqual(len(problems), 1)
        self.assertRegex(problems[0], r"R2.*more than once")
        self.seed(HEADER + self.block(2))
        self.assertRegex(rulings.validate(self.root)[0], r"R2.*R1 expected")

    def test_validate_names_only_the_real_discontinuity(self):
        self.seed(HEADER + self.block(1) + self.block(2) + self.block(3) + self.block(2) + self.block(4))
        problems = rulings.validate(self.root)
        self.assertEqual(len(problems), 1)
        self.assertRegex(problems[0], r"R2.*more than once")
        self.seed(HEADER + self.block(1) + self.block(3) + self.block(2))
        problems = rulings.validate(self.root)
        self.assertEqual(len(problems), 2)
        self.assertRegex(problems[0], r"R3 found where R2 expected")
        self.assertRegex(problems[1], r"R2 is out of order after R3")

    def test_header_with_trailing_whitespace_is_accepted(self):
        self.seed(HEADER + "\n## R1 (2026-01-02, controller) \t\n\n**Question.** Q?\n\n**Decision.** D.\n\n**Precedent.**\n")
        self.assertEqual(rulings.read(self.root)[0]["role"], "controller")
        self.assertEqual(rulings.validate(self.root), [])

    def test_list_reads_a_crlf_file(self):
        self.seed((HEADER + self.block(1) + self.block(2, "Then supersedes R1.")).replace("\n", "\r\n"))
        code, out, err = self.run_cli("rulings", "list")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("supersedes R1", out)

    def test_trailing_partial_block_without_final_newline(self):
        self.seed(HEADER + self.block(1) + "\n## R2 (2026-01-02, controller)\n\n**Question.** Q?")
        before = self.path.read_bytes()
        self.assertRegex(rulings.validate(self.root)[0], r"line 13: R2 is missing .*Decision")
        with self.assertRaisesRegex(KitError, "R2 is missing"):
            rulings.add(self.root, "controller", "Q?", "D.")
        self.assertEqual(self.path.read_bytes(), before)

    def test_supersedes_must_point_backwards(self):
        self.seed(HEADER + self.block(1, "Supersedes R2 later.") + self.block(2, "Self: supersedes R2."))
        problems = rulings.validate(self.root)
        self.assertEqual(len(problems), 2)
        self.assertRegex(problems[0], r"R1.*supersedes R2.*earlier")
        self.assertRegex(problems[1], r"R2.*supersedes R2.*earlier")
        self.seed(HEADER + self.block(1, "Supersedes R9."))
        self.assertRegex(rulings.validate(self.root)[0], r"R9")

    def test_add_refuses_forward_supersedes_and_writes_nothing(self):
        self.seed(HEADER + self.block(1))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(KitError, "supersedes R5"):
            rulings.add(self.root, "controller", "Q?", "This supersedes R5.")
        self.assertEqual(self.path.read_bytes(), before)

    # malformed input
    def test_a_non_ascii_digit_in_a_header_is_malformed(self):
        for header in ("## R\u0661 (2026-01-02, controller)", "## R1 (\u0662026-01-02, controller)"):
            self.seed(HEADER + f"\n{header}\n\n**Question.** Which?\n\n**Decision.** X.\n")
            with self.assertRaises(KitError) as caught:
                rulings.read(self.root)
            self.assertIn("malformed ruling header", str(caught.exception))

    def test_malformed_block_is_reported_with_its_line(self):
        self.seed(HEADER + "\n## R1 (2026-01-02, controller)\n\n**Question.** Q?\n\n**Precedent.** P.\n")
        with self.assertRaisesRegex(KitError, r"rulings\.md line 5: R1 .*Decision"):
            rulings.read(self.root)
        self.assertRegex(rulings.validate(self.root)[0], r"line 5")
        self.seed(HEADER + "\n## R1 2026-01-02 controller\n")
        with self.assertRaisesRegex(KitError, r"line 5: .*header"):
            rulings.read(self.root)

    def test_add_refuses_bad_input_and_a_malformed_log_without_writing(self):
        self.seed()
        before = self.path.read_bytes()
        for role, question, decision, precedent in [
            ("", "Q?", "D.", ""), ("a)b", "Q?", "D.", ""), ("x\ny", "Q?", "D.", ""),
            ("controller", " ", "D.", ""), ("controller", "Q?", "", ""),
            ("controller", "Q?", "line\n## R9 (2026-01-01, x)", ""),
            ("controller", "Q\n## heading", "D.", ""),
            ("controller", "Q?", "D.", "p\r## sneaky"),
            ("x\ry", "Q?", "D.", ""), ("x\x0by", "Q?", "D.", ""), ("x\x0cy", "Q?", "D.", ""),
            ("x\x85y", "Q?", "D.", ""), ("x\u2028y", "Q?", "D.", ""), ("x\u2029y", "Q?", "D.", ""),
            ("controller", "Q?", "line\n**Decision.** twice", ""),
        ]:
            with self.assertRaises(KitError, msg=repr((role, question, decision))):
                rulings.add(self.root, role, question, decision, precedent)
            self.assertEqual(self.path.read_bytes(), before)
        self.seed(HEADER + "\n## R1 (bad header\n")
        before = self.path.read_bytes()
        with self.assertRaises(KitError):
            rulings.add(self.root, "controller", "Q?", "D.")
        self.assertEqual(self.path.read_bytes(), before)

    # CLI
    def test_cli_add_list_validate(self):
        self.seed()
        code, out, err = self.run_cli("rulings", "add", "--role", "controller", "--question", "Q?",
                                      "--decision", "D.", "--precedent", "P.")
        self.assertEqual((code, out.strip(), err), (0, "R1", ""))
        code, _, _ = self.run_cli("rulings", "add", "--role", "qa", "--question", "Q2?",
                                  "--decision", "D2, supersedes R1.")
        self.assertEqual(code, 0)
        code, out, err = self.run_cli("rulings", "list")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("R1\t") and "controller" in lines[0] and "Q?" in lines[0])
        self.assertIn("supersedes R1", lines[1])
        self.assertEqual(self.run_cli("rulings", "validate"), (0, "rulings: ok\n", ""))

    def test_cli_errors_are_one_line_and_exit_1(self):
        self.seed(HEADER + self.block(1) + self.block(3))
        code, out, err = self.run_cli("rulings", "validate")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1)
        code, out, err = self.run_cli("rulings", "add", "--role", "qa", "--question", "Q", "--decision", " ")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.count("\n"), 1)
        code, out, err = self.run_cli("rulings")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: "))


if __name__ == "__main__":
    unittest.main()
