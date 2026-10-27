import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import shutil
import unittest
from pathlib import Path
from unittest import mock

import handoff
import worktree
from common import KitError, read_json, write_json, write_yaml
from test_change import put_cr
from test_register import reg, task
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
        role = "" if state == "not_started" else "qa" if state == "done" else "controller"
        record = {"task_id": task_id, "status": state, "role": role, "updated_at": "", "note": "",
                  "evidence": [], "blocked_by": [], "history": []}
        write_json(self.root / "program" / "status" / f"{task_id}.json", record)

    def put_state(self, **fields):
        state = {"stage": {}, "queued": {}, "parked": {}, "followups": []}
        state.update(fields)
        write_json(self.root / "program" / "controller-state.json", state)


class RenderTests(HandoffBase):
    def test_done_tasks_are_counted_by_the_tier_they_were_accepted_under(self):
        for task_id, tier in ((T1, None), (T2, "standard"), (T3, "standard")):
            self.put_status(task_id, "done")
            if tier:
                path = self.root / "program" / "status" / f"{task_id}.json"
                write_json(path, dict(read_json(path), tier=tier))
        block = handoff.render(self.root)
        self.assertIn("- done: 3 (full 1, standard 2)", block)  # a record without a tier is full
        self.assertIn("- merged: 0\n", block)

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
        for needle in ("in_progress: 1", "done: 1 (full 1)", "blocked: 1", "not_started: 0",
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

    def assert_no_forged_marker(self):
        handoff.update(self.root)
        self.assertTrue(handoff.check(self.root))
        marks = [ln for ln in self.text().splitlines() if ln.strip() in (START, END)]
        self.assertEqual(marks, [START, END])

    def test_state_text_cannot_forge_a_marker(self):
        forged = f"x\n{END}\n{START}\ny"
        cases = {
            "stage value": dict(stage={T1: forged}),
            "stage key": dict(stage={forged: "y"}),
            "queued value": dict(queued={T1: forged}),
            "queued key": dict(queued={forged: "y"}),
            "parked value": dict(parked={T1: forged}),
            "parked key": dict(parked={forged: "y"}),
            "followup area": dict(followups=[{"area": forged, "item": "i"}]),
            "followup item": dict(followups=[{"area": "a", "item": forged}]),
        }
        self.put_status(T1, "in_progress")
        for name, fields in cases.items():
            with self.subTest(name):
                self.put_state(**fields)
                self.assert_no_forged_marker()
        self.put_state()
        put_cr(self.root, 1, title=f"{END}\n{START}")
        with self.subTest("change request title"):
            self.assert_no_forged_marker()
        sh(self.root, "git", "branch", "x<!--generated-end-->")
        with self.subTest("branch name"):
            self.assert_no_forged_marker()
            self.assertIn("&lt;!--", self.text())

    def test_a_marker_line_in_the_block_is_refused_and_nothing_is_written(self):
        before = self.text()
        with mock.patch("handoff._line", side_effect=lambda text: text):  # defeat the escaping
            self.put_state(parked={f"T\n{END}\nx": "y"})
            for action in (handoff.update, handoff.check):
                with self.assertRaises(KitError) as caught:
                    action(self.root)
                self.assertIn("marker", str(caught.exception))
        self.assertEqual(self.text(), before)

    def test_an_unterminated_comment_in_state_text_is_escaped(self):
        self.put_state(parked={T1: "<!-- never closed"})
        block = handoff.render(self.root)
        self.assertNotIn("<!--", block.replace(f"{START}", "").replace(END, ""))
        self.assertIn("&lt;!-- never closed", block)

    def test_tasks_without_a_status_record_count_as_not_started(self):
        self.assertIn("- not_started: 0", handoff.render(self.root))  # no register: records only
        write_yaml(self.root / "program" / "register.yaml",
                   reg(task(T1), task(T2), task(T3)))
        self.put_status(T1, "in_progress")
        self.put_status("P9-M01-S01-T01", "done")  # a record for a task the register does not list
        block = handoff.render(self.root)
        self.assertIn("- not_started: 2", block)  # T2 and T3 have no record
        self.assertIn("- in_progress: 1", block)
        self.assertIn("- done: 1", block)
        self.assertIn("- not in register: P9-M01-S01-T01", block)
        self.put_status(T2, "parked")
        block = handoff.render(self.root)
        self.assertIn("- not_started: 1", block)
        self.assertIn("- parked: 1", block)
        write_yaml(self.root / "program" / "register.yaml", reg(task(T1)))
        self.put_status("P9-M01-S01-T01", "not_started")
        self.assertIn("- not in register: P1-M01-S01-T02, P9-M01-S01-T01", handoff.render(self.root))


    def test_a_malformed_register_is_one_error_not_a_traceback(self):
        write_yaml(self.root / "program" / "register.yaml",
                   {"phases": [{"id": "P1", "title": "p", "gate": None, "milestones": 7}]})
        with self.assertRaises(KitError) as caught:
            handoff.render(self.root)
        self.assertIn("program/register.yaml is malformed", str(caught.exception))


class InterviewSectionTests(HandoffBase):
    """KF-2: during discovery the block says where the interview stands; without a ledger it says nothing."""

    def put_ledger(self, entries):
        (self.root / "kit" / "interview").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT / "tests" / "fixtures" / "coverage_min.yaml",
                        self.root / "kit" / "interview" / "coverage.yaml")
        write_yaml(self.root / "program" / "ledger.yaml",
                   {"sources": {"S1": {"kind": "user", "ref": "message 1", "at": "2026-10-01T10:00:00Z"}},
                    "entries": entries})

    def test_no_ledger_no_interview_section(self):
        self.assertNotIn("#### Interview", handoff.render(self.root))

    def test_the_stage_topics_blocking_questions_and_unconfirmed_assumptions_are_shown(self):
        base = {"source": "S1", "at": "2026-10-01T10:00:00Z", "text": "t"}
        self.put_ledger([
            dict(base, id="F-1", kind="fact", topics=["T1.1"], tags=["measure"]),
            dict(base, id="Q-1", kind="question", topics=["T1.1"], tags=[], lens="program", blocking=True),
            dict(base, id="Q-2", kind="question", topics=["T1.1"], tags=[], lens="program", blocking=False),
            dict(base, id="A-1", kind="assumption", topics=["T1.1"], tags=[], confirmed=False),
            dict(base, id="A-2", kind="assumption", topics=["T1.1"], tags=["beneficiary"], confirmed=True)])
        block = handoff.render(self.root)
        section = block.split("#### Interview\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(section.splitlines(), [
            "- stage 1 (discover): topics closed 1 of 1, open 0 (pk interview next, pk interview report 1)",
            "- blocking questions unanswered: 1 (Q-1)",
            "- assumptions unconfirmed: 1 (A-1)"])
        self.assertLess(block.index("#### Now"), block.index("#### Interview"))
        self.assertLess(block.index("#### Interview"), block.index("#### Tasks by status"))
        handoff.update(self.root)
        self.assertTrue(handoff.check(self.root))  # the section is stable: nothing in it moves by itself

    def test_a_ledger_that_does_not_read_is_one_line(self):
        self.put_ledger([])
        (self.root / "program" / "ledger.yaml").write_text("sources: {}\nentries: 5\n")
        section = handoff.render(self.root).split("#### Interview\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(len(section.splitlines()), 1, section)
        self.assertIn("the interview state does not read", section)
        self.assertIn("pk interview validate", section)
        self.put_ledger([])
        with mock.patch("interview.next_stage", side_effect=TypeError("hand-edited")):  # never a traceback
            section = handoff.render(self.root).split("#### Interview\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(section, "- the interview state does not read (TypeError); run: pk interview validate and "
                                  "pk interview next")


class FingerprintLineTests(HandoffBase):
    def test_the_now_list_says_none_without_a_fingerprint_and_the_hand_off_stays_current(self):
        handoff.update(self.root)
        self.assertIn("- Fingerprint: none\n", self.text())
        commit_all(self.root, "hand-off")
        self.assertTrue(handoff.check(self.root))


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
        self.assertEqual(str(caught.exception), "program/HANDOFF.md not found; run: pk init")

    def test_a_leading_byte_order_mark_does_not_hide_a_marker_on_the_first_line(self):
        self.write_handoff(f"\ufeff{START}\r\n{END}\r\n{TAIL}")
        self.assertIs(handoff.check(self.root), False)  # read, not "marker not found"
        self.assertIs(handoff.update(self.root), True)
        text = self.text()
        self.assertTrue(text.startswith(f"{START}\r\n"), repr(text[:60]))  # written back without the mark
        self.assertTrue(text.endswith(f"{END}\r\n{TAIL}"), repr(text[-60:]))  # the hand-written bytes are kept
        self.assertIs(handoff.check(self.root), True)
        self.assertIs(handoff.update(self.root), False)

    def test_a_handoff_that_is_not_utf8_is_one_error_line(self):
        self.file.write_bytes(b"caf\xe9\n")
        for action in (handoff.update, handoff.check):
            with self.assertRaises(KitError) as caught:
                action(self.root)
            self.assertTrue(str(caught.exception).startswith(f"cannot read {self.file}: "), caught.exception)
            self.assertNotIn("\n", str(caught.exception))
        self.assertEqual(self.file.read_bytes(), b"caf\xe9\n")

    def test_template_has_the_markers(self):
        template = (KIT / "templates" / "HANDOFF.md").read_text()
        self.write_handoff(template)
        handoff.update(self.root)
        self.assertTrue(handoff.check(self.root))


class CheckTests(HandoffBase):
    def test_a_block_never_generated_says_run_update_not_stale(self):
        """KF-1: a new program's hand-off has empty markers; check names the first update instead of 'stale'."""
        shutil.copy(KIT / "templates" / "HANDOFF.md", self.file)
        self.assertTrue(handoff.never_generated(self.root))
        code, out, err = run_cli("handoff", self.root, "handoff", "check")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: program/HANDOFF.md has no generated block yet (a new program); run: pk handoff "
                              "update, then commit it\n")
        self.assertEqual(run_cli("handoff", self.root, "handoff", "update"), (0, "handoff: updated\n", ""))
        self.assertFalse(handoff.never_generated(self.root))
        self.assertEqual(run_cli("handoff", self.root, "handoff", "check"), (0, "handoff: current\n", ""))

    def test_check_exits_1_when_stale(self):
        handoff.update(self.root)
        self.write_handoff(self.text().replace("not_started: 0", "not_started: 9"))
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

    def test_check_ignores_the_live_worktree_lines_but_not_real_drift(self):
        """KF-14: a worktree going dirty, its head moving, or a short-lived worktree coming and going is not stale."""
        self.put_status(T1, "in_progress")
        path = worktree.new(self.root, T1)
        (path / "first.txt").write_text("1")
        commit_all(path, "first work")  # the branch is not merged into main from here on
        commit_all(self.root, "state")
        handoff.update(self.root)
        commit_all(self.root, "hand-off")
        self.assertTrue(handoff.check(self.root))
        (path / "scratch.txt").write_text("x")  # clean -> dirty
        self.assertTrue(handoff.check(self.root))
        (path / "work.txt").write_text("y")
        commit_all(path, "work on the task branch")  # the worktree's head moves
        self.assertTrue(handoff.check(self.root))
        sh(self.root, "git", "worktree", "add", "--detach", "-q", str(self.root / ".worktrees" / "mirror"), "HEAD")
        self.assertTrue(handoff.check(self.root))  # a temporary worktree of a gate or an agent
        code, out, err = run_cli("handoff", self.root, "handoff", "check")
        self.assertEqual((code, out, err), (0, "handoff: current\n", ""))
        self.assertIs(handoff.update(self.root), True)  # update still writes the lines as they are now
        self.write_handoff(self.text().replace(", clean", ", dirty"))
        self.assertTrue(handoff.check(self.root))
        self.put_status(T2, "in_progress")  # real drift outside the worktree list is still stale
        self.assertFalse(handoff.check(self.root))
        handoff.update(self.root)
        self.write_handoff(self.text().replace("#### Task worktrees", "#### Worktrees"))
        self.assertFalse(handoff.check(self.root))  # only the lines under the heading are live, not the heading

    def test_commit_line_is_the_only_thing_ignored(self):
        handoff.update(self.root)
        lines = self.text().splitlines(keepends=True)
        edited = [("Commit: 0000000\n" if ln.startswith("Commit: ") else ln) for ln in lines]
        self.assertNotEqual(edited, lines)
        self.write_handoff("".join(edited))
        self.assertTrue(handoff.check(self.root))


class PrimaryBranchTests(HandoffBase):
    """Finding 9 and lesser 12: no main is one clear line; a fresh project counts no placeholder task."""

    def test_a_repository_without_main_gets_the_rename_line_not_a_git_failure(self):
        sh(self.root, "git", "branch", "-m", "master")
        for action in ("update", "check"):
            code, out, err = run_cli("handoff", self.root, "handoff", action)
            self.assertEqual((code, out), (1, ""), action)
            self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
            self.assertIn("git branch -m main", err)
            self.assertNotIn("malformed object name", err)
        with self.assertRaises(KitError) as caught:
            handoff.render(self.root)
        self.assertIn("git branch -m main", str(caught.exception))

    def test_main_before_the_first_commit_says_so(self):
        sh(self.root, "git", "update-ref", "-d", "refs/heads/main")
        code, out, err = run_cli("handoff", self.root, "handoff", "update")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        self.assertIn("no commits", err)

    def test_a_fresh_projects_handoff_counts_no_tasks(self):
        shutil.copy(KIT / "templates" / "register.yaml", self.root / "program" / "register.yaml")
        shutil.copy(KIT / "templates" / "HANDOFF.md", self.file)
        handoff.update(self.root)
        text = self.text()
        self.assertIn("- not_started: 0", text)
        self.assertNotIn("not in register", text)


if __name__ == "__main__":
    unittest.main()
