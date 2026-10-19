"""The controller's tracking commands: follow-ups, ad-hoc tasks, the cost log, `pk record` and the hand-off budget."""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import json
import shutil
import unittest
from pathlib import Path

import cost
import ctlstate
import handoff
import rulings
import status
import tracker
from common import KitError, read_json, write_json
from test_status import write_register
from test_worktree import commit_all, make_repo, run_cli, sh

KIT = Path(__file__).resolve().parent.parent
TID = "P1-M01-S01-T01"
STATE = "program/controller-state.json"


class TrackerBase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(self)
        (self.root / "program").mkdir()
        shutil.copy(KIT / "templates" / "HANDOFF.md", self.root / "program" / "HANDOFF.md")
        shutil.copy(KIT / "templates" / "controller-state.json", self.root / STATE)
        write_register(self.root)
        commit_all(self.root, "program")

    def state(self):
        return ctlstate.load(self.root)

    def cli(self, module, *argv):
        return run_cli(module, self.root, *argv)

    def dirty(self):
        return sorted(line[3:] for line in sh(self.root, "git", "status", "--porcelain").splitlines())


class FollowupTests(TrackerBase):
    def test_follow_ups_get_stable_ids_and_are_closed_with_a_reason(self):
        self.assertEqual(self.cli("tracker", "followups", "add", "--area", "ci", "--item", "pin the image")[1],
                         "F-0001\n")
        self.assertEqual(self.cli("tracker", "followups", "add", "--area", "ui", "--item", "empty state")[1],
                         "F-0002\n")
        code, out, err = self.cli("tracker", "followups", "close", "F-0001", "--reason", "pinned", "--by", "R4")
        self.assertEqual((code, err), (0, ""))
        state = self.state()
        self.assertEqual([item["id"] for item in state["followups"]], ["F-0002"])
        closed = state["closed_followups"][0]
        self.assertEqual((closed["id"], closed["reason"], closed["by"]), ("F-0001", "pinned", "R4"))
        self.assertRegex(closed["closed"], r"\d{4}-\d\d-\d\d")
        # an id is never reused, and a closed follow-up cannot be closed again
        self.assertEqual(self.cli("tracker", "followups", "add", "--area", "ci", "--item", "again")[1], "F-0003\n")
        code, out, err = self.cli("tracker", "followups", "close", "F-0001", "--reason", "x")
        self.assertEqual(code, 1)
        self.assertIn("F-0001 is already closed", err)
        self.assertIn("F-0009 is not a follow-up", self.cli("tracker", "followups", "close", "F-0009", "--reason",
                                                           "x")[2])

    def test_tags_defer_and_duplicates_close(self):
        for item in ("one", "two", "three"):
            self.cli("tracker", "followups", "add", "--area", "a", "--item", item)
        self.assertEqual(self.cli("tracker", "followups", "tag", "F-0001", "t12-tail")[0], 0)
        self.assertEqual(self.cli("tracker", "followups", "dup", "F-0003", "--of", "F-0002")[0], 0)
        state = self.state()
        self.assertEqual(state["followups"][0]["tags"], ["t12-tail"])
        self.assertEqual(state["closed_followups"][0]["duplicate_of"], "F-0002")
        code, out, _ = self.cli("tracker", "followups", "list")
        self.assertEqual(out.splitlines()[0], "2 open follow-up(s)")
        self.assertTrue(out.splitlines()[1].startswith("F-0002"))  # newest first
        code, out, _ = self.cli("tracker", "followups", "list", "--tag", "t12-tail")
        self.assertEqual(out.splitlines()[0], "1 open follow-up(s) tagged t12-tail")
        self.assertEqual(self.cli("tracker", "followups", "tag", "F-0001", "t12-tail", "--remove")[0], 0)
        self.assertEqual(self.state()["followups"][0]["tags"], [])
        self.assertEqual(self.cli("tracker", "followups", "tag", "F-0001", "Bad Tag")[0], 1)
        self.assertEqual(self.cli("tracker", "followups", "dup", "F-0001", "--of", "F-0001")[0], 1)

    def test_a_0_2_state_file_reads_with_ids_in_file_order_and_is_rewritten_only_by_a_change(self):
        old = {"stage": {}, "queued": {}, "parked": {}, "followups": [{"area": "ci", "item": "first"},
                                                                      {"area": "ui", "item": "second"}]}
        write_json(self.root / STATE, old)
        state = self.state()
        self.assertEqual([(item["id"], item["added"]) for item in state["followups"]],
                         [("F-0001", "unknown"), ("F-0002", "unknown")])
        self.assertEqual(read_json(self.root / STATE), old)  # reading wrote nothing
        self.assertIn("F-0002 ui: second", handoff.render(self.root))
        self.cli("tracker", "followups", "add", "--area", "x", "--item", "third")
        self.assertEqual([item["id"] for item in read_json(self.root / STATE)["followups"]],
                         ["F-0001", "F-0002", "F-0003"])

    def test_the_hand_off_lists_open_follow_ups_newest_first_counted_without_the_deferred(self):
        for item in ("one", "two", "three"):
            self.cli("tracker", "followups", "add", "--area", "a", "--item", item)
        self.cli("tracker", "followups", "tag", "F-0002", "t12-tail")
        block = handoff.render(self.root)
        self.assertIn("#### Open follow-ups (3 open; 1 deferred by tag: t12-tail 1)", block)
        self.assertLess(block.index("F-0003 a: three"), block.index("F-0001 a: one"))
        self.assertNotIn("F-0002", block)

    def test_a_follow_up_deleted_from_the_file_is_noticed_and_its_number_is_never_reused(self):
        """L13, the reviewer's run: drop F-0002 from the file; then drop F-0003, the newest, as well."""
        for item in ("one", "two", "three"):
            self.cli("tracker", "followups", "add", "--area", "a", "--item", item)
        self.assertEqual(self.state()["followups_issued"], 3)
        path = self.root / STATE
        whole = read_json(path)
        write_json(path, dict(whole, followups=[item for item in whole["followups"] if item["id"] != "F-0002"]))
        with self.assertRaisesRegex(KitError, "follow-up\\(s\\) F-0002 are in neither the open nor the closed list"):
            ctlstate.load(self.root)
        code, _, err = self.cli("handoff", "handoff", "check")
        self.assertEqual(code, 1)
        self.assertIn("never deleted", err)
        write_json(path, dict(whole, followups=whole["followups"][:1]))  # F-0002 and F-0003 gone: no gap is left
        with self.assertRaisesRegex(KitError, "F-0002, F-0003 are in neither"):
            ctlstate.load(self.root)
        code, out, err = self.cli("tracker", "followups", "add", "--area", "a", "--item", "four")
        self.assertEqual((code, out), (1, ""))  # and so F-0002 is not given out a second time
        write_json(path, whole)
        self.assertEqual(self.cli("tracker", "followups", "add", "--area", "a", "--item", "four")[1], "F-0004\n")

    def test_a_file_written_before_the_counter_reads_as_it_is_and_fails_on_a_gap(self):
        write_json(self.root / STATE, {"followups": [{"id": "F-0001", "area": "a", "item": "one"},
                                                     {"id": "F-0003", "area": "a", "item": "three"}]})
        with self.assertRaisesRegex(KitError, "F-0002 are in neither"):
            ctlstate.load(self.root)
        write_json(self.root / STATE, {"followups": [{"area": "a", "item": "one"}, {"area": "a", "item": "two"}]})
        self.assertEqual(self.state()["followups_issued"], 2)  # a 0.2 file: ids in file order

    def test_a_long_follow_up_list_is_cut_with_a_pointer(self):
        state = self.state()
        for number in range(handoff.FOLLOWUPS_SHOWN + 5):
            tracker.add_followup(state, "a", f"item {number}")
        ctlstate.save(self.root, state)
        block = handoff.render(self.root)
        self.assertIn("- and 5 older: pk followups list", block)
        self.assertNotIn("item 4\n", block)


class AdhocTests(TrackerBase):
    def test_ad_hoc_tasks_are_listed_in_the_hand_off_like_register_tasks(self):
        self.assertEqual(self.cli("tracker", "adhoc", "add", "--title", "export button", "--note", "asked today")[1],
                         "AH-0001\n")
        self.cli("tracker", "adhoc", "add", "--title", "fix typo")
        self.cli("tracker", "adhoc", "set", "AH-0001", "progress")
        self.cli("tracker", "adhoc", "set", "AH-0002", "closed", "--note", "merged")
        block = handoff.render(self.root)
        self.assertIn("- AH-0001 export button (ad hoc, progress): asked today", block)
        self.assertIn("#### Ad-hoc tasks closed (newest 10 of 1)\n- AH-0002 fix typo (no status record): merged",
                      block)
        self.assertNotIn("not in register", block)

    def test_an_ad_hoc_task_with_work_in_flight_is_never_out_of_sight(self):
        """L3: closed in the tracking state while its status record or its branch says the work is not over."""
        self.cli("tracker", "adhoc", "add", "--title", "export button")
        self.cli("tracker", "adhoc", "set", "AH-0001", "closed")  # closed first, dispatched afterwards
        status.transition(self.root, "AH-0001", "in_progress", "controller")
        now = handoff.render(self.root).split("#### Tasks by status")[0]
        self.assertIn("- [full] AH-0001 (in_progress, ad hoc, tracked as closed): (no stage text)", now)
        self.assertIn("- AH-0001 export button (status in_progress)", handoff.render(self.root))
        self.cli("tracker", "adhoc", "set", "AH-0001", "progress")
        status.transition(self.root, "AH-0001", "blocked", "controller", blocked_by=["staging-env"])
        code, _, err = self.cli("tracker", "adhoc", "set", "AH-0001", "closed")
        self.assertEqual(code, 1)
        self.assertIn("its status record is blocked", err)
        # no status record in flight, but a branch that never merged
        self.cli("tracker", "adhoc", "add", "--title", "fix typo")
        commit_all(self.root, "state")
        sh(self.root, "git", "branch", "task/AH-0002")
        sh(self.root, "git", "checkout", "-q", "task/AH-0002")
        (self.root / "typo.txt").write_text("fixed\n")
        commit_all(self.root, "work")
        sh(self.root, "git", "checkout", "-q", "main")
        code, _, err = self.cli("tracker", "adhoc", "set", "AH-0002", "closed")
        self.assertEqual(code, 1)
        self.assertIn("its branch task/AH-0002 is not merged into main", err)
        sh(self.root, "git", "merge", "-q", "--no-ff", "-m", "merge", "task/AH-0002")
        self.assertEqual(self.cli("tracker", "adhoc", "set", "AH-0002", "closed")[0], 0)

    def test_only_the_newest_ten_closed_ad_hoc_tasks_are_listed(self):
        state = self.state()
        for number in range(12):
            tracker.add_adhoc(state, f"task {number}")
            state["adhoc"][-1]["state"] = "closed"
        ctlstate.save(self.root, state)
        block = handoff.render(self.root)
        self.assertIn("newest 10 of 12", block)
        self.assertIn("AH-0012", block)
        self.assertNotIn("AH-0002", block)

    def test_done_is_not_a_tracking_state_and_closed_waits_for_the_status_record(self):
        self.cli("tracker", "adhoc", "add", "--title", "export button")
        code, _, err = self.cli("tracker", "adhoc", "set", "AH-0001", "done")
        self.assertEqual(code, 1)  # done is a status only QA sets
        status.transition(self.root, "AH-0001", "in_progress", "controller")
        code, _, err = self.cli("tracker", "adhoc", "set", "AH-0001", "closed")
        self.assertEqual(code, 1)
        self.assertIn("its status record is in_progress", err)
        self.assertEqual(self.state()["adhoc"][0]["state"], "queued")
        block = handoff.render(self.root)
        self.assertIn("AH-0001 export button (ad hoc, queued; status in_progress, tier full)", block)

    def test_a_malformed_ad_hoc_list_is_one_error(self):
        for bad in ([{"id": "X-1", "title": "t"}], [{"id": "AH-0001", "title": "t", "state": "done"}],
                    [{"id": "AH-0001", "title": "t"}, {"id": "AH-0001", "title": "u"}], "x"):
            write_json(self.root / STATE, {"adhoc": bad})
            with self.subTest(bad=bad), self.assertRaisesRegex(KitError, "adhoc"):
                handoff.render(self.root)


class RecordTests(TrackerBase):
    def record(self, *argv):
        return self.cli("tracker", "record", *argv)

    def test_one_command_records_a_ruling_updates_the_state_and_the_hand_off_and_commits_them(self):
        self.cli("tracker", "followups", "add", "--area", "ci", "--item", "pin the image")
        commit_all(self.root, "a follow-up")
        (self.root / "notes.txt").write_text("not the controller's\n")
        code, out, err = self.record(
            "--ruling", "Use one local stack. Everything else waits.", "--adhoc", "export button", "--start",
            "--followup", "ui=empty state", "--close", "F-0001=pinned", "--note", f"{TID}=round 2",
            "--now", "landing the export button", "--trailer", "Co-Authored-By: Test Agent")
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        self.assertLess(len(lines), 10)
        self.assertEqual(lines[0], "R1 recorded")
        self.assertIn("follow-ups: 1 added (F-0002); 1 closed (F-0001)", lines)
        self.assertEqual([r["decision"] for r in rulings.read(self.root)], ["Use one local stack. Everything else waits."])
        state = self.state()
        self.assertEqual((state["adhoc"][0]["id"], state["adhoc"][0]["state"], state["adhoc"][0]["ruling"]),
                         ("AH-0001", "progress", "R1"))
        self.assertEqual(state["closed_followups"][0]["by"], "R1")
        self.assertEqual(state["stage"], {TID: "round 2"})
        self.assertTrue(handoff.check(self.root))
        text = (self.root / "program" / "HANDOFF.md").read_text()
        self.assertIn("- landing the export button", text)
        self.assertIn("#### Rulings (1; next R2)", text)
        # exactly the files it wrote are committed; the stray file is left alone
        self.assertEqual(self.dirty(), ["notes.txt"])
        self.assertEqual(sorted(sh(self.root, "git", "show", "--name-only", "--format=", "HEAD").split()),
                         ["program/HANDOFF.md", "program/controller-state.json", "program/rulings.md"])
        message = sh(self.root, "git", "log", "-1", "--format=%B")
        self.assertEqual(message.splitlines()[0], "R1: Use one local stack")
        self.assertEqual(message.strip().splitlines()[-1], "Co-Authored-By: Test Agent")

    def test_the_hand_off_says_why_a_task_is_blocked_or_parked(self):
        """L2, the reviewer's run: T1 blocked by an environment, T2 parked on a decision of the user."""
        status.transition(self.root, TID, "blocked", "controller", blocked_by=["staging-env"], note="no cluster yet")
        status.transition(self.root, "P1-M01-S01-T02", "parked", "controller",
                          note="waits on the user's decision about pricing tiers")
        block = handoff.render(self.root)
        self.assertIn(f"- [full] {TID} (blocked by staging-env): no cluster yet", block)
        self.assertIn("#### Parked\n- P1-M01-S01-T02: waits on the user's decision about pricing tiers", block)
        self.assertIn("- parked: 1", block)

    def test_the_queued_and_parked_lists_and_the_now_line_have_a_writer(self):
        code, out, err = self.record("--ruling", "Order of the next tasks.", "--now", "waiting for QA of T1",
                                     "--note", f"{TID}=QA round 2", "--queued", "P1-M01-S01-T02=after T1 merges",
                                     "--parked", "P1-M01-S01-T02=the user picks a payment provider")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("queued: P1-M01-S01-T02", out.splitlines())
        text = (self.root / "program" / "HANDOFF.md").read_text()
        for needle in ("- waiting for QA of T1", "#### Queued\n- P1-M01-S01-T02: after T1 merges",
                       "#### Parked\n- P1-M01-S01-T02: the user picks a payment provider"):
            self.assertIn(needle, text)
        code, out, err = self.record("--ruling", "T2 is dispatched.", "--queued", "P1-M01-S01-T02=")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("queued: P1-M01-S01-T02 removed", out.splitlines())
        self.assertIn("#### Queued\n- none", (self.root / "program" / "HANDOFF.md").read_text())
        for argv, needle in ((("--queued", "P9-M99-S99-T99=x"), "is not a task in program/register.yaml"),
                             (("--note", "P9-M99-S99-T99=stage text"), "is not a task in program/register.yaml"),
                             (("--parked", "P1-M01-S01-T01="), "has no parked line to remove"),
                             (("--queued", "AH-0001=x"), "is an ad-hoc task"),
                             (("--queued", "nothing"), "takes ID=TEXT")):
            with self.subTest(argv=argv):
                code, _, err = self.record("--ruling", "A step.", *argv)
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
        self.assertEqual(len(rulings.read(self.root)), 2)
        self.assertEqual(self.dirty(), [])

    def test_a_rewritten_ruling_is_never_committed(self):
        """L4: change R1's decision in the working tree, then record the next step."""
        self.assertEqual(self.record("--ruling", "Use port 8080.")[0], 0)
        log = self.root / "program" / "rulings.md"
        kept = log.read_text()
        log.write_text(kept.replace("8080", "9090"))
        code, out, err = self.record("--ruling", "Another step.")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("hard rule 23", err)
        self.assertEqual(len(rulings.read(self.root)), 1)  # no R2
        self.assertIn("8080", sh(self.root, "git", "show", "HEAD:program/rulings.md"))
        code, out, err = self.cli("rulings", "rulings", "validate")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("the rulings committed at HEAD are no longer the beginning of the file", err)
        log.write_text("# Rulings\n")  # deleted, not rewritten: the same answer
        self.assertIn("hard rule 23", " ".join(rulings.validate(self.root)))
        log.write_text(kept)
        self.assertEqual(rulings.validate(self.root), [])
        rulings.add(self.root, "controller", "q", "appended, not yet committed")
        self.assertEqual(rulings.validate(self.root), [])  # appending is what the log is for

    def test_a_step_whose_commit_fails_is_put_back(self):
        """L16: no ruling without its state change, nothing left staged, and a re-run is R1 again."""
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
        hook.chmod(0o755)
        before = {name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                  for name in tracker.RECORD_FILES}
        code, _, err = self.record("--ruling", "A step.", "--followup", "ui=empty state", "--now", "busy")
        self.assertEqual(code, 1)
        self.assertEqual({name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                          for name in tracker.RECORD_FILES}, before)
        self.assertEqual(self.dirty(), [])
        self.assertEqual(sh(self.root, "git", "diff", "--cached", "--name-only"), "")
        hook.unlink()
        code, out, err = self.record("--ruling", "A step.", "--followup", "ui=empty state", "--now", "busy")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[0], "R1 recorded")
        self.assertEqual([item["id"] for item in self.state()["followups"]], ["F-0001"])

    def test_no_commit_leaves_the_three_files_for_the_controller(self):
        code, out, _ = self.record("--ruling", "Wait for the user.", "--no-commit")
        self.assertEqual(code, 0)
        self.assertEqual(self.dirty(), ["program/HANDOFF.md", "program/rulings.md"])

    def test_a_refused_step_writes_nothing(self):
        before = {name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                  for name in tracker.RECORD_FILES}
        for argv, needle in ((("--close", "F-0009=x"), "F-0009 is not a follow-up"),
                             (("--task", f"{TID}=progress"), "its status record"),
                             (("--task", "AH-0001=progress"), "AH-0001 is not an ad-hoc task"),
                             (("--followup", "no-separator"), "takes KEY=VALUE"),
                             (("--now", "x" * 300), "keep it under 200")):
            with self.subTest(argv=argv):
                code, out, err = self.record("--ruling", "A step.", *argv)
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
                self.assertEqual({name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                                  for name in tracker.RECORD_FILES}, before)
        code, out, err = self.record("--ruling", "This supersedes R9.")
        self.assertEqual(code, 1)
        self.assertEqual(self.dirty(), [])

    def test_record_runs_on_main_only_and_never_sweeps_up_staged_files(self):
        (self.root / "notes.txt").write_text("x\n")
        sh(self.root, "git", "add", "notes.txt")
        code, _, err = self.record("--ruling", "A step.")
        self.assertEqual(code, 1)
        self.assertIn("unstage what is staged", err)
        sh(self.root, "git", "reset", "-q")
        sh(self.root, "git", "checkout", "-q", "-b", "side")
        for argv in ((), ("--no-commit",)):  # L15: without a commit too, or the branch gets an R1 of its own
            code, _, err = self.record("--ruling", "A step.", *argv)
            self.assertEqual(code, 1)
            self.assertIn("main checkout", err)
            self.assertEqual(rulings.read(self.root), [])
            self.assertEqual(self.dirty(), ["notes.txt"])

    def test_the_hand_off_shows_the_newest_fifteen_rulings_and_where_the_rest_are(self):
        for number in range(1, 19):
            rulings.add(self.root, "controller", "q", f"decision {number} " + "long " * 80)
        block = handoff.render(self.root)
        self.assertIn("#### Rulings (18; next R19)", block)
        shown = [line for line in block.splitlines() if line.startswith("- R")]
        self.assertEqual([line.split()[1] for line in shown], [f"R{n}" for n in range(18, 3, -1)])
        self.assertTrue(all(len(line) < 300 for line in shown))
        self.assertIn("- 3 earlier rulings: program/rulings.md, read by number", block)


class BudgetTests(TrackerBase):
    def test_check_fails_when_the_hand_off_is_over_its_budget(self):
        handoff.update(self.root)
        self.assertEqual(self.cli("handoff", "handoff", "check")[0], 0)
        path = self.root / "program" / "HANDOFF.md"
        path.write_text(path.read_text().replace("## How the program works\n",
                                                 "## How the program works\n" + "history " * 9000 + "\n"))
        code, _, err = self.cli("handoff", "handoff", "check")
        self.assertEqual(code, 1)
        self.assertIn("over its budget of 60000", err)
        commit_all(self.root, "a hand-off that grew")
        # L14: the budget is raised by a ruling, through the command, and the hand-off says so
        code, out, err = self.cli("tracker", "record", "--ruling", "Raise the hand-off budget until history moves.",
                                  "--handoff-budget", "200000")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("hand-off budget: 200000 bytes", out.splitlines())
        self.assertEqual(self.state()["handoff_budget_bytes"], 200_000)
        self.assertEqual(self.cli("handoff", "handoff", "check")[0], 0)
        self.assertIn("Size budget: 200000 bytes, set in the controller state in place of the default 60000",
                      handoff.render(self.root))
        code, out, err = self.cli("tracker", "record", "--ruling", "Back to the default budget.",
                                  "--handoff-budget", "0")
        self.assertEqual(code, 1)  # committed, then the reason: the file is over the default again
        self.assertIn("over its budget of 60000", err)
        self.assertNotIn("handoff_budget_bytes", self.state())
        self.assertNotIn("Size budget:", handoff.render(self.root))
        self.assertEqual(self.dirty(), [])
        self.assertIn("--handoff-budget takes a number of bytes",
                      self.cli("tracker", "record", "--ruling", "x", "--handoff-budget", "-5")[2])

    def test_check_fails_when_the_next_steps_section_is_over_two_kilobytes(self):
        handoff.update(self.root)
        path = self.root / "program" / "HANDOFF.md"
        text = path.read_text()
        self.assertIn("## Next steps", text)
        head, tail = text.split("## Next steps", 1)
        title, rest = tail.split("\n", 1)
        path.write_text(head + "## Next steps" + title + "\n" + "1. do the next thing\n" * 120 + rest)
        self.assertEqual(handoff.check(self.root), True)
        problems = handoff.budget_problems(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("Next steps section", problems[0])
        self.assertEqual(self.cli("handoff", "handoff", "check")[0], 1)

    def test_record_reports_an_over_budget_hand_off_as_a_failure(self):
        path = self.root / "program" / "HANDOFF.md"
        path.write_text(path.read_text().replace("## How the program works\n",
                                                 "## How the program works\n" + "history " * 9000 + "\n"))
        commit_all(self.root, "a bloated hand-off")
        code, _, err = self.cli("tracker", "record", "--ruling", "A step.")
        self.assertEqual(code, 1)
        self.assertIn("over its budget", err)

    def test_generating_the_hand_off_changes_no_other_tracked_file(self):
        status.transition(self.root, TID, "in_progress", "controller")
        self.cli("tracker", "followups", "add", "--area", "ci", "--item", "pin")
        cost.add(self.root, TID, "implementer", "model-a", 10)
        commit_all(self.root, "state")
        self.assertTrue(handoff.update(self.root))
        self.assertEqual(self.dirty(), ["program/HANDOFF.md"])
        commit_all(self.root, "hand-off")
        self.assertFalse(handoff.update(self.root))  # a build on a clean tree leaves it clean
        self.assertEqual(self.dirty(), [])
        self.assertIn(f"- [full] {TID} (in_progress): (no stage text)", (self.root / "program" / "HANDOFF.md").read_text())


class CostTests(TrackerBase):
    def test_runs_are_appended_and_totalled_per_task(self):
        code, out, err = self.cli("cost", "cost", "add", "--task", TID, "--role", "implementer", "--model", "model-a",
                                  "--tokens", "120000", "--tools", "40", "--seconds", "600")
        self.assertEqual((code, err), (0, ""))
        first = (self.root / cost.COST_FILE).read_bytes()
        self.cli("cost", "cost", "add", "--task", TID, "--role", "qa", "--model", "model-b", "--tokens", "30000")
        self.assertTrue((self.root / cost.COST_FILE).read_bytes().startswith(first))  # append-only
        self.assertEqual(len(cost.read(self.root)), 2)
        code, out, _ = self.cli("cost", "cost", "report")
        self.assertEqual(out, f"{TID}\t150000 tokens\t40 tool calls\t600 s\t2 run(s)\n")

    def test_an_unknown_task_is_refused_unless_allowed(self):
        """KF-4: a typo or an ad-hoc ID logged before the task exists is refused; --allow-unknown logs it anyway."""
        for typo in ("P1-M01-S01-T99", "AH-0001"):
            code, out, err = self.cli("cost", "cost", "add", "--task", typo, "--role", "qa", "--model", "m",
                                      "--tokens", "5")
            self.assertEqual((code, out), (1, ""), typo)
            self.assertIn(f"--task {typo} is not a task in program/register.yaml or an ad-hoc task", err)
            self.assertIn("--allow-unknown", err)
        self.assertFalse((self.root / cost.COST_FILE).exists())
        task_id = self.cli("tracker", "adhoc", "add", "--title", "a probe")[1].strip()
        self.assertEqual(task_id, "AH-0001")
        for task in (TID, task_id):
            code, _, err = self.cli("cost", "cost", "add", "--task", task, "--role", "qa", "--model", "m",
                                    "--tokens", "5")
            self.assertEqual((code, err), (0, ""), task)
        code, _, err = self.cli("cost", "cost", "add", "--task", "P9-M01-S01-T01", "--role", "qa", "--model", "m",
                                "--tokens", "5", "--allow-unknown")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([r["task"] for r in cost.read(self.root)], [TID, task_id, "P9-M01-S01-T01"])
        code, out, _ = self.cli("cost", "cost", "report")  # an existing orphan line still reads and reports
        self.assertEqual(code, 0)
        self.assertIn("P9-M01-S01-T01\t5 tokens", out)

    def test_without_a_register_only_ad_hoc_tasks_are_known(self):
        (self.root / "program" / "register.yaml").unlink()
        code, _, err = self.cli("cost", "cost", "add", "--task", TID, "--role", "qa", "--model", "m", "--tokens", "1")
        self.assertEqual(code, 1)
        self.assertIn("is not a task in program/register.yaml", err)
        task_id = self.cli("tracker", "adhoc", "add", "--title", "stage 1 interview")[1].strip()
        self.assertEqual(self.cli("cost", "cost", "add", "--task", task_id, "--role", "interviewer", "--model", "m",
                                  "--tokens", "1")[0], 0)

    def test_every_run_names_its_model(self):
        with self.assertRaisesRegex(KitError, "every dispatch names its model"):
            cost.add(self.root, TID, "implementer", "  ", 10)
        with self.assertRaisesRegex(KitError, "whole number"):
            cost.add(self.root, TID, "implementer", "model-a", -1)
        self.assertFalse((self.root / cost.COST_FILE).exists())

    def test_an_agent_past_the_token_threshold_is_flagged_for_a_fresh_agent(self):
        cost.add(self.root, TID, "implementer", "model-a", 250_000, agent="impl-1")
        self.assertEqual(cost.flagged(cost.read(self.root)), [])
        code, out, _ = self.cli("cost", "cost", "add", "--task", TID, "--role", "implementer", "--model", "model-a",
                                "--tokens", "200000", "--agent", "impl-1", "--resume")
        self.assertIn("past the threshold", out)
        self.assertIn("start a fresh agent with a tight fix brief", out)
        self.assertIn(f"{TID} impl-1 (model-a): 450000 tokens over 2 run(s), 1 resume(s)", handoff.render(self.root))
        cost.add(self.root, TID, "qa", "model-b", 10, agent="qa-1")  # another agent of the task is not flagged
        self.assertEqual([row["agent"] for row in cost.flagged(cost.read(self.root))], ["impl-1"])

    def test_three_resumes_flag_an_agent_whatever_it_spent(self):
        cost.add(self.root, TID, "implementer", "model-a", 10, agent="impl-1")
        for _ in range(2):
            cost.add(self.root, TID, "implementer", "model-a", 10, agent="impl-1", resume=True)
        self.assertEqual(cost.flagged(cost.read(self.root)), [])
        cost.add(self.root, TID, "implementer", "model-a", 10, agent="impl-1", resume=True)
        self.assertEqual(cost.flagged(cost.read(self.root))[0]["resumes"], 3)
        self.assertEqual(cost.flagged(cost.read(self.root), resumes=5), [])

    def test_a_line_that_is_not_a_record_is_named_and_nothing_is_appended(self):
        (self.root / cost.COST_FILE).write_text(json.dumps({"task": TID}) + "\n")
        with self.assertRaisesRegex(KitError, "line 1: not a cost record"):
            cost.add(self.root, TID, "implementer", "model-a", 10)
        self.assertEqual(len((self.root / cost.COST_FILE).read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
