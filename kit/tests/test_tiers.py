"""Verification tiers and the fast path: what the status machine records and what the gates enforce.

Every lighter path has a guard, and each guard is tested here by trying to get round it: a lower tier that is
not switched on, a task that touches a full-tier path, a branch that lowers its own tier, a fast path on the
full tier, a brief that is not on main, a light merge nobody inspected, a light task called done.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import subprocess
import tempfile
import unittest
from pathlib import Path

import gate
import pk
import rulings
import status
from common import KitError, read_json, write_json, write_yaml
from test_gate import BRANCH, PLAN, PY, TASK, HandbackBase, checks_data, commit, put, sh
from test_status import TID, git_repo, write_register
from test_worktree import run_cli

FULL_PATHS = {"full_paths": ["auth/"], "light_paths": [], "light_ruling": ""}


def brief(tier="standard", ruling="R1"):
    """A fast brief whose `Tier:` and `Ruling:` lines say what the task's status record says."""
    return (f"# Fast brief\n\nRuling: {ruling}\n\nTier: {tier}\n\n## Scope\n\nAdd mul.\n\n"
            "## Assumptions checked\n\n- calc.py imports\n\n## Decided (do not reopen)\n\n- none\n\n"
            "## Stop and report when\n\n- a guarded file must change\n\n## Exact Commands\n\n"
            "$ python3 -c \"import calc; print(calc.add(1, 2))\"\n")


BRIEF = brief()
LIGHT_BRIEF = brief("light")


def status_record(task_id, state="in_progress", **over):
    """A status record as the machine writes it: the first history entry names the tier it was dispatched at."""
    record = {"task_id": task_id, "status": state, "role": "controller", "updated_at": "", "note": "",
              "evidence": [], "blocked_by": [], "history": [{"status": state, "role": "controller", "at": ""}],
              "tier": "full", "fast": False, "ruling": ""}
    record.update(over)
    record["history"] = [dict(entry) for entry in record["history"]]
    record["history"][0].setdefault("tier", record["tier"])
    return record


class TierStatusTests(unittest.TestCase):
    """The status machine: the tier is set at dispatch, changes only by a ruling, and bounds what a task may become."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        write_register(self.root)
        (self.root / ".gitignore").write_text(".worktrees/\n")
        self.checks()
        git_repo(self.root)

    def checks(self, **tiers):
        write_yaml(self.root / "program" / "checks.yaml", checks_data(tiers=dict(
            {"full_paths": [], "light_paths": [], "light_ruling": ""}, **tiers)))

    def ruling(self) -> str:
        return f"R{rulings.add(self.root, 'controller', 'q', 'd')}"

    def git(self, *argv):
        return subprocess.run(["git", *argv], cwd=str(self.root), check=True, capture_output=True, text=True).stdout

    def refused(self, pattern, *args, **kwargs):
        path = self.root / "program" / "status" / f"{TID}.json"
        before = path.read_bytes() if path.exists() else None
        with self.assertRaisesRegex(KitError, pattern):
            status.transition(self.root, *args, **kwargs)
        self.assertEqual(path.read_bytes() if path.exists() else None, before, "a refusal wrote the record")

    def test_a_dispatch_without_a_tier_is_full_and_the_record_says_so(self):
        record = status.transition(self.root, TID, "in_progress", "controller")
        self.assertEqual((record["tier"], record["fast"], record["ruling"]), ("full", False, ""))
        self.assertEqual(read_json(self.root / "program" / "status" / f"{TID}.json")["tier"], "full")
        self.assertEqual(status.validate(self.root), [])

    def test_a_record_written_before_tiers_reads_as_full(self):
        old = status_record(TID)
        for key in ("tier", "fast", "ruling"):
            del old[key]
        write_json(self.root / "program" / "status" / f"{TID}.json", old)
        self.assertEqual(status.tier_of(status.read(self.root, TID)), "full")
        self.assertEqual(status.validate(self.root), [])
        record = status.transition(self.root, TID, "review", "implementer")
        self.assertEqual(record["tier"], "full")  # carried on, now written down

    def test_a_lower_tier_is_refused_until_the_program_lists_its_full_paths(self):
        self.refused("not switched on", TID, "in_progress", "controller", tier="standard")
        self.checks(full_paths=["auth/"])
        record = status.transition(self.root, TID, "in_progress", "controller", tier="standard")
        self.assertEqual(record["tier"], "standard")

    def test_the_fast_path_is_never_available_for_the_full_tier(self):
        self.checks(full_paths=["auth/"])
        ruling = self.ruling()
        self.refused("never available for the full tier", TID, "in_progress", "controller", fast=True, ruling=ruling)
        self.refused("never available for the full tier", TID, "in_progress", "controller", tier="full", fast=True,
                     ruling=ruling)

    def test_the_fast_path_needs_a_ruling_that_exists(self):
        self.checks(full_paths=["auth/"])
        self.refused("needs --ruling", TID, "in_progress", "controller", tier="standard", fast=True)
        self.refused("R7 is not a ruling", TID, "in_progress", "controller", tier="standard", fast=True, ruling="R7")
        record = status.transition(self.root, TID, "in_progress", "controller", tier="standard", fast=True,
                                   ruling=self.ruling())
        self.assertEqual((record["tier"], record["fast"], record["ruling"]), ("standard", True, "R1"))

    def test_the_light_tier_needs_its_ruling_and_its_paths_in_checks(self):
        self.checks(full_paths=["auth/"])
        self.refused("light tier is not switched on", TID, "in_progress", "controller", tier="light")
        self.checks(full_paths=["auth/"], light_paths=["web/"], light_ruling="R1")
        self.refused("R1 is not a ruling", TID, "in_progress", "controller", tier="light")
        self.ruling()
        self.assertEqual(status.transition(self.root, TID, "in_progress", "controller", tier="light")["tier"], "light")

    def test_the_tier_is_set_at_dispatch_and_carried_by_every_later_move(self):
        self.checks(full_paths=["auth/"])
        status.transition(self.root, TID, "in_progress", "controller", tier="standard")
        self.refused("set at dispatch", TID, "review", "implementer", tier="full")
        self.assertEqual(status.transition(self.root, TID, "review", "implementer")["tier"], "standard")
        self.refused("set at dispatch", TID, "in_progress", "qa", tier="light")
        self.assertEqual(status.transition(self.root, TID, "in_progress", "qa")["tier"], "standard")

    def light_task(self):
        self.checks(full_paths=["auth/"], light_paths=["web/"], light_ruling="R1")
        self.ruling()
        status.transition(self.root, TID, "in_progress", "controller", tier="light")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "dispatch")

    def test_a_light_task_is_never_done_and_has_no_review(self):
        self.light_task()
        self.refused("no QA round", TID, "review", "implementer")
        write_json(self.root / "program" / "status" / f"{TID}.json", status_record(TID, "review", tier="light"))
        self.refused("never done", TID, "done", "qa")

    def light_branch(self, *names):
        """A branch task/<TID> of the dispatched light task that adds `names`; its tip, with main checked out again."""
        self.git("checkout", "-q", "-b", f"task/{TID}")
        for name in names:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "work")
        tip = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "main")
        return tip

    def by_hand(self, **over):
        write_json(self.root / "program" / "status" / f"{TID}.json",
                   status_record(TID, "merged", tier="light", **over))
        return " ".join(status.validate(self.root))

    def test_merged_needs_the_light_tier_the_controller_main_and_a_merge_of_the_inspected_commit(self):
        self.light_task()
        tip = self.light_branch("web/page.txt")
        self.refused("for controller", TID, "merged", "qa", inspected=tip)
        self.git("checkout", "-q", f"task/{TID}")
        self.refused("recorded on main", TID, "merged", "controller", inspected=tip)  # not in the main checkout
        self.git("checkout", "-q", "main")
        self.refused("merged needs the commit the controller inspected", TID, "merged", "controller")
        self.refused("merged needs the commit the controller inspected", TID, "merged", "controller", inspected="abc")
        self.refused(f"main has no merge of {tip}", TID, "merged", "controller", inspected=tip)
        self.git("merge", "-q", "--no-ff", "-m", "merge", f"task/{TID}")
        self.refused("main has no merge of 0000", TID, "merged", "controller", inspected="0" * 40)
        self.refused("--inspected belongs to merged", TID, "blocked", "controller", blocked_by=["x"], inspected=tip)
        record = status.transition(self.root, TID, "merged", "controller", inspected=tip)
        self.assertEqual((record["status"], record["tier"], record["inspected"]), ("merged", "light", tip))
        self.assertEqual(status.validate(self.root), [])
        self.git("branch", "-d", f"task/{TID}")  # the audit works from the merge commit, not from the branch
        self.assertEqual(status.validate(self.root), [])
        self.refused("merged is terminal", TID, "in_progress", "controller")

    def test_a_fast_forward_is_not_a_merge_of_the_inspected_commit(self):
        self.light_task()
        tip = self.light_branch("web/page.txt")
        self.git("merge", "-q", "--ff-only", f"task/{TID}")
        self.refused(f"main has no merge of {tip}", TID, "merged", "controller", inspected=tip)

    def test_a_hand_merge_round_a_failed_merge_check_is_never_recorded_merged(self):
        """The reviewer's sequence (M3): the merge check fails on a full-tier path, the branch is merged by hand."""
        self.light_task()
        tip = self.light_branch("web/page.txt", "auth/policy.py")
        self.git("merge", "-q", "--no-ff", "-m", "by hand", f"task/{TID}")
        self.refused("merged needs the commit the controller inspected", TID, "merged", "controller")
        self.refused("changed full-tier paths: auth/policy.py; a light path never applies to the full tier",
                     TID, "merged", "controller", inspected=tip)
        code, out, err = run_cli("status", self.root, "status", "set", TID, "merged", "--role", "controller")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("merged needs the commit the controller inspected", err)
        code, out, err = run_cli("status", self.root, "status", "set", TID, "merged", "--role", "controller",
                                 "--inspected", tip)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("changed full-tier paths: auth/policy.py", err)
        self.assertEqual(status.validate(self.root), [])  # still in_progress: nothing was recorded
        # and a record somebody writes by hand is found by the audit, with or without the commit
        self.assertIn("changed full-tier paths: auth/policy.py", self.by_hand(inspected=tip))
        self.assertIn("merged needs the commit the controller inspected", self.by_hand())
        self.assertIn("main has no merge of", self.by_hand(inspected="1" * 40))

    def test_a_merge_that_left_the_light_paths_is_never_recorded_merged(self):
        self.light_task()
        tip = self.light_branch("web/page.txt", "server/api.py")
        self.git("merge", "-q", "--no-ff", "-m", "by hand", f"task/{TID}")
        self.refused("changed paths outside tiers.light_paths: server/api.py", TID, "merged", "controller",
                     inspected=tip)
        self.assertIn("outside tiers.light_paths: server/api.py", self.by_hand(inspected=tip))

    def test_a_full_tier_file_added_inside_the_merge_commit_is_never_recorded_merged(self):
        """The re-review's N1 (a): the branch is light-only, the hand-made merge commit adds auth/policy.py."""
        self.light_task()
        tip = self.light_branch("web/page.txt")
        self.git("merge", "-q", "--no-ff", "--no-commit", f"task/{TID}")
        (self.root / "auth").mkdir()
        (self.root / "auth" / "policy.py").write_text("ALLOW_ALL = True\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"Merge task/{TID}")
        self.refused("changed full-tier paths: auth/policy.py", TID, "merged", "controller", inspected=tip)
        self.assertIn("changed full-tier paths: auth/policy.py", self.by_hand(inspected=tip))
        self.git("branch", "-D", f"task/{TID}")
        self.assertIn("changed full-tier paths: auth/policy.py", self.by_hand(inspected=tip))

    def test_merged_names_the_tip_of_the_task_branch_not_an_earlier_commit_or_a_decoy(self):
        """The re-review's N1 (b) and (c): a light-only first commit, or a decoy branch, named as inspected."""
        self.light_task()
        self.git("checkout", "-q", "-b", "decoy")
        (self.root / "web").mkdir()
        (self.root / "web" / "other.txt").write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "decoy")
        decoy = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "main")
        first = self.light_branch("web/page.txt")
        self.git("checkout", "-q", f"task/{TID}")
        (self.root / "auth").mkdir()
        (self.root / "auth" / "policy.py").write_text("ALLOW_ALL = True\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "more")
        tip = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "decoy", "decoy")
        self.git("merge", "-q", "--no-ff", "-m", "first", first)
        self.git("merge", "-q", "--no-ff", "-m", "rest", f"task/{TID}")
        for named in (first, decoy):
            self.refused(f"--inspected {named} is not the tip of task/{TID} \\({tip}\\)", TID, "merged",
                         "controller", inspected=named)
        self.refused("changed full-tier paths: auth/policy.py", TID, "merged", "controller", inspected=tip)
        for named in (first, decoy):  # and a record written by hand, while the branch exists
            self.assertIn(f"is not the tip of task/{TID}", self.by_hand(inspected=named))

    def test_a_light_merge_with_main_moved_first_is_recorded_merged_and_validates(self):
        self.light_task()
        tip = self.light_branch("web/page.txt")
        (self.root / "notes.txt").write_text("main moved\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "main moves")
        self.git("merge", "-q", "--no-ff", "-m", "merge", f"task/{TID}")
        self.assertEqual(status.transition(self.root, TID, "merged", "controller", inspected=tip)["status"], "merged")
        self.assertEqual(status.validate(self.root), [])
        self.git("branch", "-d", f"task/{TID}")
        self.assertEqual(status.validate(self.root), [])

    def test_a_merged_record_written_by_hand_for_a_task_that_was_not_light_does_not_validate(self):
        self.checks(full_paths=["auth/"], light_paths=["web/"], light_ruling="R1")
        self.ruling()
        status.transition(self.root, TID, "in_progress", "controller")  # dispatched full
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "dispatch")
        tip = self.light_branch("web/page.txt")
        self.git("merge", "-q", "--no-ff", "-m", "merge", f"task/{TID}")
        self.assertIn(f"{TID}: it was full-tier on main when", self.by_hand(inspected=tip))
        self.assertIn("a full-tier task ends at done", self.by_hand(inspected=tip))

    def test_a_tier_edited_by_hand_does_not_validate(self):
        self.checks(full_paths=["auth/"])
        status.transition(self.root, TID, "in_progress", "controller")
        path = self.root / "program" / "status" / f"{TID}.json"
        record = read_json(path)
        self.assertEqual(record["history"][0]["tier"], "full")  # the dispatch entry names the tier
        write_json(path, dict(record, tier="standard"))
        self.assertIn(f"{TID}: tier is standard but its history last set full", " ".join(status.validate(self.root)))
        change = {"status": "in_progress", "role": "controller", "at": "", "tier": "standard", "ruling": "R9"}
        write_json(path, dict(record, tier="standard", history=[*record["history"], change]))  # the history too
        self.assertIn("changed to standard by R9, which is not a ruling in program/rulings.md",
                      " ".join(status.validate(self.root)))
        bare = [{key: value for key, value in record["history"][0].items() if key != "tier"}]
        write_json(path, dict(record, tier="standard", history=bare))
        self.assertIn("tier standard is not in the record's history", " ".join(status.validate(self.root)))
        write_json(path, record)
        self.assertEqual(status.validate(self.root), [])

    def test_merged_is_refused_for_any_other_tier(self):
        status.transition(self.root, TID, "in_progress", "controller")
        self.refused("merged is for the light tier", TID, "merged", "controller")
        write_json(self.root / "program" / "status" / f"{TID}.json", status_record(TID, "merged", tier="standard"))
        self.assertIn("merged is the terminal status of the light tier only", " ".join(status.validate(self.root)))

    def test_a_record_that_is_fast_and_full_does_not_read(self):
        write_json(self.root / "program" / "status" / f"{TID}.json", status_record(TID, fast=True, ruling="R1"))
        with self.assertRaisesRegex(KitError, "never available for the full tier"):
            status.read(self.root, TID)

    def test_a_tier_changes_only_by_a_ruling_on_main_by_the_controller(self):
        self.checks(full_paths=["auth/"])
        with self.assertRaisesRegex(KitError, "no status record yet"):
            status.set_tier(self.root, TID, "standard", "R1")
        status.transition(self.root, TID, "in_progress", "controller")
        with self.assertRaisesRegex(KitError, "R1 is not a ruling"):
            status.set_tier(self.root, TID, "standard", "R1")
        with self.assertRaisesRegex(KitError, "needs --ruling"):
            status.set_tier(self.root, TID, "standard", "")
        ruling = self.ruling()
        with self.assertRaisesRegex(KitError, "only the controller"):
            status.set_tier(self.root, TID, "standard", ruling, role="implementer")
        self.git("checkout", "-q", "-b", "side")
        with self.assertRaisesRegex(KitError, "recorded on main"):
            status.set_tier(self.root, TID, "standard", ruling)
        self.git("checkout", "-q", "main")
        record = status.set_tier(self.root, TID, "standard", ruling)
        self.assertEqual((record["tier"], record["ruling"], record["status"]), ("standard", "R1", "in_progress"))
        self.assertEqual([entry["tier"] for entry in record["history"]], ["full", "standard"])
        self.assertEqual(status.validate(self.root), [])
        with self.assertRaisesRegex(KitError, "already standard"):
            status.set_tier(self.root, TID, "standard", ruling)

    def test_a_fast_task_is_raised_to_full_only_by_leaving_the_fast_path(self):
        self.checks(full_paths=["auth/"])
        ruling = self.ruling()
        status.transition(self.root, TID, "in_progress", "controller", tier="standard", fast=True, ruling=ruling)
        with self.assertRaisesRegex(KitError, "never available for the full tier"):
            status.set_tier(self.root, TID, "full", ruling)
        record = status.set_tier(self.root, TID, "full", ruling, spec=True)
        self.assertEqual((record["tier"], record["fast"]), ("full", False))

    def test_the_command_line_sets_and_changes_the_tier(self):
        self.checks(full_paths=["auth/"])
        ruling = self.ruling()
        code, out, err = run_cli("status", self.root, "status", "set", TID, "in_progress", "--role", "controller",
                                 "--tier", "standard", "--fast", "--ruling", ruling)
        self.assertEqual((code, out, err), (0, f"{TID}: in_progress (tier standard, fast path)\n", ""))
        code, out, err = run_cli("status", self.root, "status", "list")
        self.assertEqual(out.split()[-1], "standard+fast")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "dispatch")
        code, out, err = run_cli("status", self.root, "status", "tier", TID, "full", "--ruling", ruling, "--spec")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], f"{TID}: tier full by {ruling}")
        said = out.splitlines()[1]  # what to do next, so nobody reaches for a hand edit of the status file
        for needle in ("next: change the `Tier:` line", f"git checkout --theirs program/status/{TID}.json",
                       "in_progress again at tier full"):
            self.assertIn(needle, said)
        code, out, err = run_cli("status", self.root, "status", "set", "P1-M01-S01-T02", "in_progress", "--role",
                                 "controller", "--fast", "--ruling", ruling)
        self.assertEqual(code, 1)
        self.assertIn("never available for the full tier", err)

    def test_an_ad_hoc_task_listed_in_the_controller_state_can_have_a_status(self):
        with self.assertRaisesRegex(KitError, "not a task"):
            status.transition(self.root, "AH-0001", "in_progress", "controller")
        write_json(self.root / "program" / "controller-state.json", {"adhoc": [{"id": "AH-0001", "title": "t"}]})
        self.assertEqual(status.transition(self.root, "AH-0001", "in_progress", "controller")["tier"], "full")


class TierGateBase(HandbackBase):
    def on_main(self, record=None, tiers=None, rulings_count=0, files=None, remove=()):
        """Put a status record, a tiers section, rulings and files on main, commit, and merge main into the task."""
        if tiers is not None:
            self.write_checks(tiers=tiers)
        if record is not None:
            write_json(self.root / "program" / "status" / f"{TASK}.json", record)
        for _ in range(rulings_count):
            rulings.add(self.root, "controller", "q", "d")
        for name, text in (files or {}).items():
            put(self.root, name, text)
        for name in remove:
            (self.root / name).unlink()
        commit(self.root, "main changes")
        sh(self.wt, "git", "merge", "-q", "main")

    def work(self, name, text="x\n"):
        put(self.wt, name, text)
        commit(self.wt, f"change {name}")


class StandardTierGateTests(TierGateBase):
    def test_a_standard_task_passes_both_gates_when_it_stays_off_the_full_paths(self):
        self.on_main(status_record(TASK, tier="standard"), FULL_PATHS,
                     files={f"program/tasks/{TASK}/SPEC.md": "# Spec\n\nTier: standard\n"})
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        commit(self.wt, "evidence")
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertIn("step test: ok", out)  # the suite runs once more at the merge check

    def test_a_standard_task_that_changes_a_full_tier_path_fails_both_gates(self):
        self.on_main(status_record(TASK, tier="standard"), FULL_PATHS,
                     files={f"program/tasks/{TASK}/SPEC.md": "# Spec\n\nTier: standard\n"})
        self.work("auth/policy.py")
        for run in (self.handback, self.merge_check):
            rc, out = run()
            self.assertEqual(rc, 1)
            self.assertIn(f"FAILED: tier {TASK} is standard but changes full-tier paths: auth/policy.py", out)

    def test_a_full_path_written_without_its_trailing_slash_still_guards_the_directory(self):
        self.on_main(status_record(TASK, tier="standard"), dict(FULL_PATHS, full_paths=["auth"]),
                     files={f"program/tasks/{TASK}/SPEC.md": "# Spec\n\nTier: standard\n"})
        self.work("auth/policy.py")
        for run in (self.handback, self.merge_check):
            rc, out = run()
            self.assertEqual(rc, 1)
            self.assertIn(f"FAILED: tier {TASK} is standard but changes full-tier paths: auth/policy.py", out)
        self.assertEqual(gate._under("auth", ["auth"]), "auth")
        self.assertEqual(gate._under("auth/a/b.py", ["auth/"]), "auth/")
        self.assertIsNone(gate._under("authz/policy.py", ["auth"]))  # a name that only begins the same is not under it
        self.assertIsNone(gate._under("authz/policy.py", ["auth/"]))

    def test_a_branch_that_edits_the_contract_main_holds_fails(self):
        contract = f"program/tasks/{TASK}/CONTRACT.md"
        self.on_main(status_record(TASK, tier="standard"), FULL_PATHS,
                     files={f"program/tasks/{TASK}/SPEC.md": "# Spec\n\nTier: standard\n", contract: "# Contract\n"})
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)  # the contract is main's: nothing to say
        self.work(contract, "# Contract\n\nGET /mine\n")
        for run in (self.handback, self.merge_check):
            rc, out = run()
            self.assertEqual(rc, 1)
            self.assertIn(f"FAILED: contract {contract} differs from main", out)

    def test_the_kits_own_gates_are_full_tier_whatever_the_program_lists(self):
        self.on_main(status_record(TASK, tier="standard"), FULL_PATHS,
                     files={f"program/tasks/{TASK}/SPEC.md": "# Spec\n\nTier: standard\n"})
        self.work("kit/scripts/gate.py", "# eased\n")
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("changes full-tier paths: kit/scripts/gate.py", out)

    def test_a_lower_tier_fails_when_main_has_not_switched_it_on(self):
        self.on_main(status_record(TASK, tier="standard"),
                     files={f"program/tasks/{TASK}/SPEC.md": "# Spec\n\nTier: standard\n"})
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("the lower tiers are not switched on", out)

    def test_a_branch_cannot_lower_its_own_tier(self):
        self.on_main(status_record(TASK), FULL_PATHS)  # main says full
        write_json(self.wt / "program" / "status" / f"{TASK}.json", status_record(TASK, tier="standard"))
        commit(self.wt, "lower my own tier")
        for run in (self.handback, self.merge_check):
            rc, out = run()
            self.assertEqual(rc, 1)
            self.assertIn(f"FAILED: tier program/status/{TASK}.json changes the tier", out)

    def test_a_branch_that_deletes_the_tier_main_names_changes_the_tier(self):
        """The re-review's N2: no tier field reads as full, but it is the exemption of a record from before tiers."""
        self.on_main(status_record(TASK), FULL_PATHS)  # main names the full tier
        bare = status_record(TASK)
        for key in ("tier", "fast", "ruling"):
            del bare[key]
        bare["history"][0].pop("tier")
        write_json(self.wt / "program" / "status" / f"{TASK}.json", bare)
        commit(self.wt, "drop the tier fields")
        for run in (self.handback, self.merge_check):
            rc, out = run()
            self.assertEqual(rc, 1)
            self.assertIn(f"FAILED: tier program/status/{TASK}.json changes the tier", out)
        self.on_main(bare)  # a record written before tiers existed, on main and on the branch: no change
        self.assertTrue(gate._tier_unchanged(self.wt, TASK, gate.task_meta(self.root, TASK)))
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)

    def test_a_branch_cannot_switch_a_tier_on_for_itself(self):
        self.on_main(status_record(TASK))
        write_yaml(self.wt / "program" / "checks.yaml", checks_data(tiers=FULL_PATHS))
        commit(self.wt, "switch tiers on")
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("guarded files changed: program/checks.yaml", out)

    def test_the_spec_on_main_must_state_the_tier_the_record_carries(self):
        self.on_main(status_record(TASK, tier="standard"), FULL_PATHS)  # the SPEC has no Tier: line
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("says no Tier: line, the status record says standard", out)
        self.on_main(files={f"program/tasks/{TASK}/SPEC.md": "# Spec\n\nTier: light\n"})
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("says Tier: light, the status record says standard", out)

    def test_a_status_record_on_main_that_does_not_read_is_never_taken_for_a_lighter_tier(self):
        put(self.root, f"program/status/{TASK}.json", "{")
        commit(self.root, "broken record")
        sh(self.wt, "git", "merge", "-q", "main")
        with self.assertRaisesRegex(KitError, "does not read"):
            gate.merge_check(self.root, BRANCH)


class FastPathGateTests(TierGateBase):
    def fast(self, **over):
        record = status_record(TASK, tier="standard", fast=True, ruling="R1")
        record.update(over)
        return record

    def test_a_fast_task_is_judged_by_its_brief_on_main_without_spec_plan_or_review(self):
        self.on_main(self.fast(), FULL_PATHS, rulings_count=1, files={f"program/tasks/{TASK}/BRIEF.md": BRIEF},
                     remove=[f"program/tasks/{TASK}/{name}" for name in ("SPEC.md", "PLAN.md", "review.md")])
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertIn("step plan: ok", out)  # the brief's exact commands ran
        commit(self.wt, "evidence")
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)

    def test_a_fast_task_without_its_brief_on_main_fails(self):
        self.on_main(self.fast(), FULL_PATHS, rulings_count=1)
        put(self.wt, f"program/tasks/{TASK}/BRIEF.md", BRIEF)
        commit(self.wt, "my own brief")
        for run in (self.handback, self.merge_check):
            rc, out = run()
            self.assertEqual(rc, 1)
            self.assertIn(f"FAILED: brief program/tasks/{TASK}/BRIEF.md is missing on main", out)

    def test_a_branch_that_edits_its_brief_fails(self):
        self.on_main(self.fast(), FULL_PATHS, rulings_count=1, files={f"program/tasks/{TASK}/BRIEF.md": BRIEF})
        self.work(f"program/tasks/{TASK}/BRIEF.md", BRIEF + "\n$ true\n")
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn(f"FAILED: brief program/tasks/{TASK}/BRIEF.md differs from main", out)

    def test_a_brief_without_the_fast_path_sections_fails(self):
        self.on_main(self.fast(), FULL_PATHS, rulings_count=1,
                     files={f"program/tasks/{TASK}/BRIEF.md": BRIEF.replace("## Decided (do not reopen)", "## Notes")})
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("lacks the section(s) ## Decided (do not reopen)", out)

    def test_a_brief_whose_tier_or_ruling_line_is_not_the_records_fails(self):
        for text, said in ((brief(tier="light"), "says Tier: light, the status record says standard"),
                           (brief(ruling="R2"), "says Ruling: R2, the status record says R1"),
                           (brief().replace("Tier: standard\n", ""), "says no Tier: line, the status record says"),
                           (brief().replace("Ruling: R1\n", ""), "says no Ruling: line, the status record says R1"),
                           (brief() + "\nTier: light\n", "says Tier: light, Tier: standard, the status record")):
            with self.subTest(said):
                self.on_main(self.fast(), FULL_PATHS, rulings_count=2, files={f"program/tasks/{TASK}/BRIEF.md": text})
                for run in (self.handback, self.merge_check):
                    rc, out = run()
                    self.assertEqual(rc, 1)
                    self.assertIn(f"FAILED: brief program/tasks/{TASK}/BRIEF.md on main {said}", out)

    def test_a_brief_without_its_assumptions_section_fails(self):
        self.on_main(self.fast(), FULL_PATHS, rulings_count=1,
                     files={f"program/tasks/{TASK}/BRIEF.md": BRIEF.replace("## Assumptions checked", "## Notes")})
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("lacks the section(s) ## Assumptions checked", out)

    def test_a_brief_without_commands_fails_the_hand_back_gate(self):
        self.on_main(self.fast(), FULL_PATHS, rulings_count=1,
                     files={f"program/tasks/{TASK}/BRIEF.md": BRIEF.split("## Exact Commands")[0]})
        rc, out = self.handback()
        self.assertEqual(rc, 1)
        self.assertIn('FAILED: brief no commands under "## Exact Commands"', out)

    def test_the_fast_path_ruling_must_be_on_main(self):
        self.on_main(self.fast(), FULL_PATHS, files={f"program/tasks/{TASK}/BRIEF.md": BRIEF})  # no ruling at all
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("on the fast path by R1, which is not a ruling in program/rulings.md on main", out)

    def test_a_forged_fast_full_record_on_main_is_refused(self):
        put(self.root, f"program/tasks/{TASK}/BRIEF.md", BRIEF)
        write_json(self.root / "program" / "status" / f"{TASK}.json", self.fast(tier="full"))
        commit(self.root, "forged")
        sh(self.wt, "git", "merge", "-q", "main")
        with self.assertRaisesRegex(KitError, "never available for the full tier"):
            gate.merge_check(self.root, BRANCH)

    def test_a_fast_task_cannot_change_a_full_tier_path_or_a_guarded_file(self):
        self.on_main(self.fast(), FULL_PATHS, rulings_count=1, files={f"program/tasks/{TASK}/BRIEF.md": BRIEF})
        self.work("auth/policy.py")
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("changes full-tier paths: auth/policy.py", out)
        self.work("pyproject.toml")
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn("guarded files changed: pyproject.toml", out)


class LightTierGateTests(TierGateBase):
    LIGHT = {"full_paths": ["auth/"], "light_paths": ["web/"], "light_ruling": "R1"}

    def setUp(self):
        super().setUp()
        # a light branch changes only light paths: restart the task branch from main with one web change
        sh(self.root, "git", "worktree", "remove", "--force", str(self.wt))
        sh(self.root, "git", "branch", "-D", BRANCH)
        sh(self.root, "git", "worktree", "add", "-q", "-b", BRANCH, str(self.wt))
        self.work("web/page.txt")

    def light(self, **over):
        record = status_record(TASK, tier="light", fast=True, ruling="R1")
        record.update(over)
        return record

    def switch_on(self, **kwargs):
        # the test command fails on purpose: a light gate that passes proves it did not run the tests
        self.write_checks(test=[[PY, "-c", "raise SystemExit(9)"]])
        self.on_main(self.light(), self.LIGHT, rulings_count=1, files={f"program/tasks/{TASK}/BRIEF.md": LIGHT_BRIEF},
                     **kwargs)
        self.write_checks(test=[[PY, "-c", "raise SystemExit(9)"]], tiers=self.LIGHT)
        commit(self.root, "failing tests")
        sh(self.wt, "git", "merge", "-q", "main")
        return sh(self.wt, "git", "rev-parse", "HEAD").strip()

    def test_a_light_merge_check_needs_the_controllers_inspection_of_the_tip(self):
        tip = self.switch_on()
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn(f"FAILED: inspection {TASK} is light-tier", out)
        rc, out = self.merge_check(inspected="0" * 40)
        self.assertEqual(rc, 1)
        rc, out = self.merge_check(inspected=tip)
        self.assertEqual(rc, 0, out)
        self.assertIn("step build: ok", out)
        self.assertIn("step scan: ok", out)
        self.assertNotIn("step test", out)

    def test_a_light_hand_back_runs_the_compile_gate_and_needs_no_evidence(self):
        self.switch_on()
        rc, out = self.handback()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("step evidence", out)
        self.assertFalse((self.wt / "program" / "evidence").exists())

    def test_a_light_task_that_leaves_the_light_paths_fails(self):
        self.switch_on()
        self.work("server/api.py")
        rc, out = self.merge_check(inspected=sh(self.wt, "git", "rev-parse", "HEAD").strip())
        self.assertEqual(rc, 1)
        self.assertIn("changes paths outside tiers.light_paths: server/api.py", out)

    def test_a_light_task_that_touches_a_full_tier_path_fails(self):
        self.switch_on()
        self.work("auth/policy.py")
        rc, out = self.merge_check(inspected=sh(self.wt, "git", "rev-parse", "HEAD").strip())
        self.assertEqual(rc, 1)
        self.assertIn("is light but changes full-tier paths: auth/policy.py", out)

    def test_the_light_tier_needs_its_ruling_on_main(self):
        self.on_main(self.light(), dict(self.LIGHT, light_ruling="R9"), rulings_count=1,
                     files={f"program/tasks/{TASK}/BRIEF.md": LIGHT_BRIEF})
        rc, out = self.merge_check(inspected=sh(self.wt, "git", "rev-parse", "HEAD").strip())
        self.assertEqual(rc, 1)
        self.assertIn("has not switched the light tier on", out)

    def test_the_command_line_takes_the_inspected_commit(self):
        tip = self.switch_on()
        rc, out, err = self.run_cli("merge-check", "--branch", BRANCH, "--inspected", tip)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(out.splitlines()[-1], f"OK: {BRANCH} merges into main at {tip}")


class ChecksTiersTests(unittest.TestCase):
    def test_the_tiers_section_is_parsed_and_its_mistakes_are_named(self):
        parsed = gate._parse_checks({"tiers": {"full_paths": ["./auth/"], "light_ruling": "R3"}}, ())
        self.assertEqual(parsed["tiers"], {"full_paths": ["auth/"], "light_paths": [], "light_ruling": "R3"})
        self.assertEqual(gate._parse_checks({}, ())["tiers"], {"full_paths": [], "light_paths": [], "light_ruling": ""})
        for bad, needle in (({"tiers": ["auth/"]}, "tiers must be a mapping"),
                            ({"tiers": {"paths": []}}, "unknown key paths"),
                            ({"tiers": {"full_paths": "auth/"}}, "tiers.full_paths must be a list"),
                            ({"tiers": {"light_ruling": "seven"}}, "must name a ruling")):
            with self.subTest(needle), self.assertRaisesRegex(KitError, needle):
                gate._parse_checks(bad, ())

    def test_doctor_reports_a_bad_tiers_section(self):
        self.assertEqual(pk._checks_problems({"build": [["a"]], "test": [["a"]], "scan": [["a"]],
                                              "tiers": {"nope": 1}}),
                         ["checks.yaml: tiers: unknown key nope (expected full_paths, light_paths, light_ruling)"])


    def test_doctor_warns_about_a_tier_path_that_covers_no_tracked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "auth").mkdir()
            (root / "auth" / "policy.py").write_text("x\n")
            git_repo(root)
            for tiers, expected in (({"full_paths": ["auth"]}, []), ({"full_paths": ["auth/"]}, []),
                                    ({"full_paths": ["auth/policy.py", "atuh/"], "light_paths": ["web/"]},
                                     ["checks.yaml: tiers.full_paths entry 'atuh/' covers no tracked file",
                                      "checks.yaml: tiers.light_paths entry 'web/' covers no tracked file"])):
                with self.subTest(tiers):
                    found = pk._tier_path_warnings(root, {"tiers": tiers})
                    self.assertEqual([line.split(": check the")[0] for line in found], expected)
            self.assertEqual(pk._tier_path_warnings(root, {"tiers": "nope"}), [])  # an error elsewhere, not a warning


if __name__ == "__main__":
    unittest.main()
