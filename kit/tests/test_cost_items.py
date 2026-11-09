"""0.6.0 (KF-41): `pk cost add --item <ID> --category <word>` logs work that is not a task; `pk cost report` groups it."""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import json
import unittest

import cost
from common import KitError
from test_tracker import TID, TrackerBase


class ItemCostTests(TrackerBase):
    def add(self, *argv):
        return self.cli("cost", "cost", "add", "--role", "researcher", "--model", "model-a", *argv)

    def test_an_item_with_a_category_is_logged_without_allow_unknown(self):
        code, out, err = self.add("--item", "RI-07", "--category", "research", "--tokens", "5000")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "cost: research RI-07 researcher (model-a) 5000 tokens\n")
        record = cost.read(self.root)[0]
        self.assertEqual((record["item"], record["category"], record["task"]), ("RI-07", "research", "RI-07"))

    def test_the_flags_are_refused_in_the_wrong_combinations(self):
        for argv, needle in ((("--item", "RI-07"), "--item needs --category"),
                             (("--category", "research", "--task", TID), "--category goes with --item"),
                             (("--item", "RI-07", "--category", "research", "--task", TID), "either --task or --item"),
                             ((), "--task <ID>, or --item <ID> with --category"),
                             (("--item", "RI 07", "--category", "research"), "--item"),
                             (("--item", "RI-07", "--category", "Two words"), "--category")):
            with self.subTest(argv=argv):
                code, _, err = self.add(*argv, "--tokens", "1")
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
        self.assertFalse((self.root / cost.COST_FILE).exists())

    def test_report_keeps_task_lines_as_before_and_groups_items_by_category(self):
        self.cli("cost", "cost", "add", "--task", TID, "--role", "qa", "--model", "m", "--tokens", "100")
        _, before, _ = self.cli("cost", "cost", "report")
        self.add("--item", "RI-07", "--category", "research", "--tokens", "10")
        self.add("--item", "RI-02", "--category", "research", "--tokens", "20", "--tools", "3")
        self.add("--item", "RI-07", "--category", "research", "--tokens", "30")
        self.add("--item", "W-1", "--category", "ops", "--tokens", "4")
        code, out, _ = self.cli("cost", "cost", "report")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(before), out)
        self.assertEqual(out[len(before):].splitlines(), [
            "ops/W-1\t4 tokens\t0 tool calls\t0 s\t1 run(s)",
            "research/RI-07\t40 tokens\t0 tool calls\t0 s\t2 run(s)",
            "research/RI-02\t20 tokens\t3 tool calls\t0 s\t1 run(s)",
            "research (all items)\t60 tokens\t3 tool calls\t0 s\t3 run(s)",
        ])
        _, only, _ = self.cli("cost", "cost", "report", "--category", "ops")
        self.assertEqual(only.splitlines(), ["ops/W-1\t4 tokens\t0 tool calls\t0 s\t1 run(s)"])

    def test_old_rows_read_and_a_malformed_item_field_is_named(self):
        old = {"at": "2026-10-01T00:00:00Z", "task": TID, "role": "qa", "agent": "qa", "model": "m", "tokens": 1,
               "tool_calls": 0, "seconds": 0, "resume": False}
        (self.root / cost.COST_FILE).write_text(json.dumps(old) + "\n")
        self.assertEqual(cost.read(self.root), [old])
        (self.root / cost.COST_FILE).write_text(json.dumps(dict(old, item="RI-1")) + "\n")
        with self.assertRaisesRegex(KitError, "line 1: item and category go together"):
            cost.read(self.root)


if __name__ == "__main__":
    unittest.main()
