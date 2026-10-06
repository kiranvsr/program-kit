import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import unittest
from pathlib import Path

import handoff
import worktree
from common import KitError, write_json
from test_change import put_cr
from test_trace import T1, T2, T3
from test_worktree import commit_all, make_repo, run_cli, sh

KIT = Path(__file__).resolve().parent.parent
START, END = "<!-- generated:start -->", "<!-- generated:end -->"
HEAD = "# Program hand-off\r\n\r\nWritten by hand, with CRLF and a trailing space \r\n"
TAIL = "\r\n## Next steps\r\n1. keep going\r\nno final newline"


class HandoffBase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        (self.root / "program").mkdir()
        self.file = self.root / "program" / "HANDOFF.md"
        self.write_handoff(f"{HEAD}{START}\n{END}\n{TAIL}")

    def write_handoff(self, text):
        self.file.write_bytes(text.encode("utf-8"))

    def text(self):
        return self.file.read_bytes().decode("utf-8")

    def put_status(self, task_id, state):
        record = {"task_id": task_id, "status": state, "role": "", "updated_at": "", "note": "",
                  "evidence": [], "blocked_by": [], "history": []}
        write_json(self.root / "program" / "status" / f"{task_id}.json", record)

    def put_state(self, **fields):
        state = {"stage": {}, "queued": {}, "parked": {}, "followups": []}
        state.update(fields)
        write_json(self.root / "program" / "controller-state.json", state)


class RenderTests(HandoffBase):
    def test_render_lists_in_flight_and_dirty_worktrees(self):
        self.put_status(T1, "in_progress")
        self.put_status(T2, "done")
        self.put_status(T3, "blocked")
        self.put_state(stage={T1: "round 2,\nfixes pending"}, queued={"P1-M01-S01-T04": "waits on T1"},
                       parked={"P1-M01-S01-T05": "needs a decision"},
                       followups=[{"area": "ci", "item": "pin the runner image"}])
        dirty = worktree.new(self.root, T1)
        worktree.new(self.root, T2)
        (dirty / "scratch.txt").write_text("x")
        block = handoff.render(self.root)
        self.assertTrue(block.endswith("\n"))
        for needle in ("in_progress: 1", "done: 1", "blocked: 1", "not_started: 0",
                       f"{T1} (in_progress): round 2, fixes pending",
                       f"{T3} (blocked): (no stage text)",
                       f"task/{T1}", f"task/{T2}",
                       "P1-M01-S01-T04: waits on T1", "P1-M01-S01-T05: needs a decision",
                       "ci: pin the runner image"):
            self.assertIn(needle, block)
        self.assertNotIn(f"{T2} (done)", block)  # done tasks are not in flight
        lines = block.splitlines()
        self.assertTrue(any(f".worktrees/{T1.lower()}" in ln and "dirty" in ln for ln in lines), block)
        self.assertTrue(any(f".worktrees/{T2.lower()}" in ln and "clean" in ln for ln in lines), block)
        self.assertFalse(any(ln.startswith("- ") and str(self.root) in ln for ln in lines))  # paths are relative
        self.assertEqual(block, handoff.render(self.root))  # deterministic: no wall-clock time
        self.assertIn(sh(self.root, "git", "rev-parse", "HEAD").strip(), block)

    def test_render_lists_unmerged_branches_only(self):
        sh(self.root, "git", "branch", "side")
        sh(self.root, "git", "checkout", "-q", "side")
        (self.root / "side.txt").write_text("x")
        commit_all(self.root, "side work")
        sh(self.root, "git", "checkout", "-q", "main")
        sh(self.root, "git", "branch", "already-merged")
        block = handoff.render(self.root)
        self.assertIn("- side", block)
        self.assertNotIn("already-merged", block)
        self.assertNotIn("- main", block)

    def test_render_without_state_files_is_empty_but_complete(self):
        block = handoff.render(self.root)
        self.assertIn("none", block)
        self.assertNotIn("Traceback", block)

    def test_render_lists_open_change_requests_only(self):
        put_cr(self.root, 1, title="add export", approval="2026-10-06: Ada", applied="2026-10-07: done")
        put_cr(self.root, 2, title="rename widget")
        put_cr(self.root, 3, title="drop legacy", approval="2026-10-06: Ada")
        block = handoff.render(self.root)
        self.assertNotIn("CR-001", block)
        self.assertIn("CR-002: rename widget (awaiting approval)", block)
        self.assertIn("CR-003: drop legacy (approved, not applied)", block)

    def test_a_malformed_controller_state_is_one_error_naming_the_key(self):
        cases = {"stage": ["a"], "queued": {"T": 3}, "parked": "x", "followups": {"area": "a"}}
        for key, value in cases.items():
            with self.subTest(key=key):
                self.put_state(**{key: value})
                with self.assertRaises(KitError) as caught:
                    handoff.render(self.root)
                self.assertIn(key, str(caught.exception))
        self.put_state(followups=[{"area": "ci"}])
        with self.assertRaises(KitError) as caught:
            handoff.render(self.root)
        self.assertIn("followups", str(caught.exception))
        write_json(self.root / "program" / "controller-state.json", ["not", "a", "mapping"])
        with self.assertRaises(KitError):
            handoff.render(self.root)
        (self.root / "program" / "controller-state.json").write_text("{")
        with self.assertRaises(KitError):
            handoff.render(self.root)

    def test_state_text_cannot_forge_a_marker(self):
        self.put_state(parked={T1: f"x\n{END}\nmore"})
        handoff.update(self.root)
        self.assertTrue(handoff.check(self.root))
        self.assertEqual([ln for ln in self.text().splitlines() if ln.strip() == END], [END])


class UpdateTests(HandoffBase):
    def test_update_touches_only_marked_block(self):
        self.put_status(T1, "in_progress")
        self.assertIs(handoff.update(self.root), True)
        text = self.text()
        self.assertTrue(text.startswith(f"{HEAD}{START}\n"), repr(text[:80]))
        self.assertTrue(text.endswith(f"{END}\n{TAIL}"), repr(text[-80:]))
        self.assertIn(T1, text)
        self.assertEqual(text.count("\r"), HEAD.count("\r") + TAIL.count("\r"))
        self.assertIs(handoff.update(self.root), False)  # nothing changed: byte-identical
        self.assertEqual(self.text(), text)

    def test_bad_markers_write_nothing(self):
        cases = {
            "missing start": f"top\n{END}\n",
            "missing end": f"top\n{START}\n",
            "none": "just notes\n",
            "duplicate start": f"{START}\n{START}\n{END}\n",
            "duplicate end": f"{START}\n{END}\n{END}\n",
            "reversed": f"{END}\nmiddle\n{START}\n",
        }
        for name, content in cases.items():
            with self.subTest(name):
                self.write_handoff(content)
                for action in (handoff.update, handoff.check):
                    with self.assertRaises(KitError) as caught:
                        action(self.root)
                    self.assertIn("marker", str(caught.exception))
                self.assertEqual(self.text(), content)
        self.assertEqual([p.name for p in (self.root / "program").iterdir()], ["HANDOFF.md"])  # no temp files

    def test_missing_file_is_one_error(self):
        self.file.unlink()
        with self.assertRaises(KitError) as caught:
            handoff.update(self.root)
        self.assertIn("HANDOFF.md", str(caught.exception))

    def test_template_has_the_markers(self):
        template = (KIT / "templates" / "HANDOFF.md").read_text()
        self.write_handoff(template)
        handoff.update(self.root)
        self.assertTrue(handoff.check(self.root))


class CheckTests(HandoffBase):
    def test_check_exits_1_when_stale(self):
        code, out, err = run_cli("handoff", self.root, "handoff", "check")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: program/HANDOFF.md is stale; run: pk handoff update\n")
        self.assertEqual(run_cli("handoff", self.root, "handoff", "update"), (0, "handoff: updated\n", ""))
        self.assertEqual(run_cli("handoff", self.root, "handoff", "check"), (0, "handoff: current\n", ""))
        self.assertEqual(run_cli("handoff", self.root, "handoff", "update"), (0, "handoff: unchanged\n", ""))
        self.put_status(T1, "in_progress")  # the state moves on
        code, out, err = run_cli("handoff", self.root, "handoff", "check")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("stale", err)

    def test_check_stays_current_after_committing_the_updated_handoff(self):
        self.put_status(T1, "in_progress")
        handoff.update(self.root)
        self.assertTrue(handoff.check(self.root))
        commit_all(self.root, "program: hand-off")  # HEAD moves
        self.assertTrue(handoff.check(self.root))
        self.assertIs(handoff.update(self.root), False)

    def test_check_is_stale_after_a_hand_edit_inside_the_block(self):
        handoff.update(self.root)
        self.write_handoff(self.text().replace("not_started: 0", "not_started: 9"))
        self.assertFalse(handoff.check(self.root))

    def test_commit_line_is_the_only_thing_ignored(self):
        handoff.update(self.root)
        lines = self.text().splitlines(keepends=True)
        edited = [("Commit: 0000000\n" if ln.startswith("Commit: ") else ln) for ln in lines]
        self.assertNotEqual(edited, lines)
        self.write_handoff("".join(edited))
        self.assertTrue(handoff.check(self.root))


if __name__ == "__main__":
    unittest.main()
