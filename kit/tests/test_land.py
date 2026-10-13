"""Landing work: the review worktree, the safe finish, `pk merge` and the integration round.

Each test of a shortcut tries the thing the shortcut must not allow: merging a branch nobody accepted, a light
merge nobody inspected, a round with a full-tier member, a member that moved after the combined check, a main
that moved outside the round, a tested tree that no longer matches main, a finish that would lose work.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import os
import shutil
from pathlib import Path
from unittest import mock

import land
import pk
import status
import worktree
from common import KitError, read_json, write_json, write_yaml
from test_gate import BRANCH, PLAN, REVIEW, TASK, commit, put, sh
from test_status import write_register
from test_tiers import BRIEF, FULL_PATHS, TierGateBase, status_record

KIT = Path(__file__).resolve().parent.parent
T2 = "P0-M01-S01-T02"
STANDARD_SPEC = "# Spec\n\nTier: standard\n"


class LandBase(TierGateBase):
    def setUp(self):
        super().setUp()
        shutil.copy(KIT / "templates" / "HANDOFF.md", self.root / "program" / "HANDOFF.md")
        shutil.copy(KIT / "templates" / "controller-state.json", self.root / "program" / "controller-state.json")
        commit(self.root, "hand-off")
        sh(self.wt, "git", "merge", "-q", "main")

    def standard(self, done=True):
        """Make the fixture task a standard-tier task and, with `done`, one QA has accepted on its branch."""
        self.on_main(status_record(TASK, tier="standard"), FULL_PATHS,
                     files={f"program/tasks/{TASK}/SPEC.md": STANDARD_SPEC})
        if done:
            self.accept(self.wt, TASK)

    def accept(self, wt, task_id, tier="standard"):
        write_json(wt / "program" / "status" / f"{task_id}.json",
                   status_record(task_id, "done", tier=tier, role="qa", note=f"accepted at {self.head(wt)}"))
        return commit(wt, "accepted")

    def second_task(self):
        """A second standard-tier task on main with its own accepted branch; its worktree."""
        for name, text in (("SPEC.md", STANDARD_SPEC), ("PLAN.md", PLAN), ("review.md", REVIEW)):
            put(self.root, f"program/tasks/{T2}/{name}", text)
        write_json(self.root / "program" / "status" / f"{T2}.json", status_record(T2, tier="standard"))
        commit(self.root, "dispatch T2")
        wt = self.root / ".worktrees" / T2.lower()
        sh(self.root, "git", "worktree", "add", "-q", "-b", f"task/{T2}", str(wt))
        put(wt, "second.py", "VALUE = 2\n")
        commit(wt, "T2 work")
        self.accept(wt, T2)
        sh(self.wt, "git", "merge", "-q", "main")
        return wt

    def pk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(land, "find_root", return_value=self.root), \
                mock.patch.object(worktree, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def head(self, where=None):
        return sh(where or self.root, "git", "rev-parse", "HEAD").strip()

    def scans(self):
        return len(self.fake_log.read_text().splitlines()) if self.fake_log.exists() else 0


class ReviewWorktreeTests(LandBase):
    def test_the_reviewer_gets_a_detached_worktree_and_a_diff_file_while_the_implementer_goes_on(self):
        put(self.wt, f"program/status/{TASK}.json", "{}\n")
        put(self.wt, "generated.lock", "noise\n")
        reviewed = commit(self.wt, "ready for review")
        code, out, err = self.pk("worktree", "review", TASK, "--exclude", "generated.lock")
        self.assertEqual((code, err), (0, ""))
        path = self.root / ".worktrees" / f"{TASK.lower()}-review"
        self.assertEqual(out.splitlines()[0], str(path))
        self.assertEqual(self.head(path), reviewed)
        with self.assertRaises(Exception):  # detached: the reviewer is on no branch
            sh(path, "git", "symbolic-ref", "HEAD")
        diff = (self.root / ".worktrees" / f"{TASK.lower()}-review.diff").read_text()
        self.assertIn("def mul(a, b)", diff)
        self.assertNotIn("program/status", diff)  # status and evidence files are left out
        self.assertNotIn("generated.lock", diff)
        put(self.wt, "more.py", "X = 1\n")  # the implementer keeps committing in its own worktree
        commit(self.wt, "next work")
        self.assertEqual(self.head(path), reviewed)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")  # nothing tracked was touched

    def test_a_review_worktree_is_refused_twice_and_for_a_commit_off_the_branch(self):
        self.assertEqual(self.pk("worktree", "review", TASK)[0], 0)
        code, _, err = self.pk("worktree", "review", TASK)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        with self.assertRaisesRegex(KitError, "branch task/P9-M01-S01-T01 not found"):
            worktree.review(self.root, "P9-M01-S01-T01")
        put(self.root, "main-only.txt", "x\n")
        other = commit(self.root, "main moved")
        shutil.rmtree(self.root / ".worktrees" / f"{TASK.lower()}-review")
        sh(self.root, "git", "worktree", "prune")
        with self.assertRaisesRegex(KitError, "is not a commit of task/"):
            worktree.review(self.root, TASK, commit=other)

    def test_an_ad_hoc_task_gets_a_worktree_like_any_other(self):
        path = worktree.new(self.root, "AH-0001")
        self.assertEqual(path.name, "ah-0001")
        with self.assertRaisesRegex(KitError, "not a task id"):
            worktree.new(self.root, "AH-1")


class FinishTests(LandBase):
    def merged(self):
        sh(self.root, "git", "merge", "-q", "--no-ff", "-m", "merge", BRANCH)

    def test_finish_refuses_a_worktree_that_holds_work_and_removes_nothing(self):
        self.merged()
        worktree.review(self.root, TASK)
        (self.wt / "notes.txt").write_text("not committed\n")
        code, out, err = self.pk("worktree", "finish", TASK)
        self.assertEqual(code, 1)
        self.assertIn("holds uncommitted or untracked files: notes.txt", err)
        self.assertTrue(self.wt.is_dir())
        self.assertTrue((self.root / ".worktrees" / f"{TASK.lower()}-review").is_dir())
        self.assertIn(BRANCH, sh(self.root, "git", "branch"))

    def test_finish_never_removes_an_unmerged_branch(self):
        code, out, err = self.pk("worktree", "finish", TASK)
        self.assertEqual(code, 1)
        self.assertIn("is not merged into main", err)
        self.assertTrue(self.wt.is_dir())
        code, out, err = self.pk("worktree", "finish", TASK, "--keep-branch")  # an abandoned task
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(self.wt.exists())
        self.assertIn(f"kept: branch {BRANCH}", out)
        self.assertIn(BRANCH, sh(self.root, "git", "branch"))

    def test_finish_removes_the_named_symlinks_the_worktrees_and_the_merged_branch_and_says_so(self):
        self.merged()
        worktree.review(self.root, TASK)
        os.symlink(self.root / "calc.py", self.wt / "node_modules")
        code, out, err = self.pk("worktree", "finish", TASK)
        self.assertEqual(code, 1)  # a symlink nobody named is an untracked file like any other
        self.assertIn("node_modules", err)
        code, out, err = self.pk("worktree", "finish", TASK, "--symlink", "node_modules")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertIn(f"removed: symlink {self.wt / 'node_modules'}", lines)
        self.assertIn(f"removed: worktree {self.wt}", lines)
        self.assertIn(f"removed: branch {BRANCH}", lines)
        self.assertTrue(any(line.startswith("removed: review diff") for line in lines))
        self.assertFalse(self.wt.exists())
        self.assertNotIn(BRANCH, sh(self.root, "git", "branch"))
        self.assertTrue((self.root / "calc.py").is_file())  # the link went, not what it pointed at

    def test_a_named_path_that_is_a_real_file_is_not_treated_as_a_symlink(self):
        self.merged()
        (self.wt / "node_modules").write_text("a real file\n")
        code, _, err = self.pk("worktree", "finish", TASK, "--symlink", "node_modules")
        self.assertEqual(code, 1)
        self.assertTrue((self.wt / "node_modules").is_file())

    def test_finish_removes_only_the_containers_of_the_exact_compose_project_and_never_forces(self):
        self.merged()
        self.wrapper("docker", 'echo "$@" >> "$PK_FAKE_LOG"\n'
                               'case "$1 $2" in\n'
                               '  "ps -a") printf "t01-web-1\\nt01_db_1\\nt011-web-1\\nother\\n";;\n'
                               '  "volume ls") printf "t01_data\\nshared\\n";;\n'
                               '  "image ls") printf "t01-web:latest\\npostgres:16\\n";;\n'
                               'esac')
        code, out, err = self.pk("worktree", "finish", TASK, "--compose-project", "t01")
        self.assertEqual((code, err), (0, ""))
        removed = [line for line in out.splitlines() if line.split()[1] in ("container", "volume", "image")]
        self.assertEqual(removed, ["removed: container t01-web-1", "removed: container t01_db_1",
                                   "removed: volume t01_data", "removed: image t01-web:latest"])
        calls = self.fake_log.read_text().splitlines()
        self.assertIn("rm t01-web-1", calls)
        self.assertFalse([call for call in calls if "-f" in call.split() or "prune" in call or "t011" in call])
        with self.assertRaisesRegex(KitError, "not a usable compose project name"):
            worktree.finish(self.root, TASK, compose_project="t")

    def test_finish_runs_from_the_main_checkout_only(self):
        self.merged()
        with self.assertRaisesRegex(KitError, "from the main checkout"):
            worktree.finish(self.wt, TASK)
        self.assertTrue(self.wt.is_dir())


class MergeTests(LandBase):
    def test_a_branch_qa_has_not_accepted_is_never_merged(self):
        self.standard(done=False)
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("only a task QA has set done is merged", err)
        self.assertEqual(self.head(), before)

    def test_one_command_checks_merges_and_commits_the_bookkeeping(self):
        self.standard()
        write_yaml(self.root / "program" / "gates.yaml",
                   {"gates": {"G-P0": {"environment": "dev", "records": [{"task": TASK}]}}})
        write_yaml(self.root / "program" / "environments.yaml", {"environments": {"dev": {"exists": True}}})
        commit(self.root, "gates")
        sh(self.wt, "git", "merge", "-q", "main")
        tip = self.head(self.wt)
        code, out, err = self.pk("merge", TASK, "--trailer", "Co-Authored-By: Test Agent")
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        self.assertLess(len(lines), 10)
        self.assertEqual(lines[0], f"OK: {BRANCH} merges into main at {tip}")
        self.assertIn("claims: G-P0 rewritten", lines)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        log = sh(self.root, "git", "log", "--format=%s%n%(trailers)", "-2").splitlines()
        self.assertIn(f"chore(program): {TASK} merged (standard tier); hand-off and claims", log)
        self.assertIn(f"Merge {BRANCH} (standard tier)", log)
        self.assertEqual(log.count("Co-Authored-By: Test Agent"), 2)
        self.assertEqual(len(sh(self.root, "git", "rev-list", "--parents", "-n", "1", "HEAD~1").split()), 3)
        self.assertIn(f"program/claims/G-P0/{TASK}.json",
                      sh(self.root, "git", "show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(status.read(self.root, TASK)["status"], "done")

    def test_a_failed_merge_check_merges_nothing(self):
        self.standard(done=False)
        self.work("auth/policy.py")
        self.accept(self.wt, TASK)
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("the merge check did not pass", err)
        self.assertIn("changes full-tier paths: auth/policy.py", err)
        self.assertEqual(self.head(), before)

    def test_merge_runs_on_a_clean_main_checkout_and_takes_one_task(self):
        self.standard()
        put(self.root, "calc.py", "changed on main, not committed\n")
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes: calc.py", err)
        sh(self.root, "git", "checkout", "--", "calc.py")
        with self.assertRaisesRegex(KitError, "from the main checkout"):
            land.merge(self.wt, TASK)
        self.assertEqual(self.pk("merge", TASK, T2)[0], 1)  # merges are one at a time: one ID


class LightMergeTests(LandBase):
    LIGHT = {"full_paths": ["auth/"], "light_paths": ["web/"], "light_ruling": "R1"}

    def setUp(self):
        super().setUp()
        sh(self.root, "git", "worktree", "remove", "--force", str(self.wt))
        sh(self.root, "git", "branch", "-D", BRANCH)
        sh(self.root, "git", "worktree", "add", "-q", "-b", BRANCH, str(self.wt))
        self.work("web/page.txt")
        write_register(self.root, (TASK,))
        self.on_main(status_record(TASK, tier="light", fast=True, ruling="R1"), self.LIGHT, rulings_count=1,
                     files={f"program/tasks/{TASK}/BRIEF.md": BRIEF})
        self.tip = self.head(self.wt)

    def test_a_light_task_lands_only_after_the_controller_inspected_its_tip_and_is_recorded_merged(self):
        before = self.head()
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("FAILED: inspection", err)
        self.assertEqual(self.head(), before)
        code, out, err = self.pk("merge", TASK, "--inspected", self.tip)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"{TASK}: merged (light tier: no QA, nothing evidenced)", out.splitlines())
        record = read_json(self.root / "program" / "status" / f"{TASK}.json")
        self.assertEqual((record["status"], record["tier"], record["role"]), ("merged", "light", "controller"))
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        self.assertEqual(status.validate(self.root), [])

    def test_a_light_task_somebody_called_done_does_not_land(self):
        self.accept(self.wt, TASK, tier="light")
        code, _, err = self.pk("merge", TASK, "--inspected", self.head(self.wt))
        self.assertEqual(code, 1)
        self.assertIn("lands from in_progress, not done", err)


class RoundTests(LandBase):
    def setUp(self):
        super().setUp()
        self.standard()
        self.wt2 = self.second_task()

    def start_and_check(self):
        self.assertEqual(self.pk("integrate", "start", "r1", TASK, T2)[0], 0)
        code, out, err = self.pk("integrate", "check", "r1")
        self.assertEqual((code, err), (0, ""), out)
        return out

    def test_a_round_checks_the_combined_tree_once_and_the_members_land_without_another_suite_run(self):
        out = self.start_and_check()
        round_head = self.head(self.root / ".worktrees" / "round-r1")
        self.assertEqual(out.splitlines()[-1], f"OK: round/r1 passes the combined check at {round_head}")
        self.assertEqual(out.count("step test: ok"), 1)
        scans = self.scans()
        for task_id in (TASK, T2):  # one at a time
            code, out, err = self.pk("merge", task_id, "--round", "r1")
            self.assertEqual((code, err), (0, ""), out)
            self.assertNotIn("OK: ", out)  # no merge check ran, and none is claimed
        self.assertEqual(self.scans(), scans)  # the suite did not run again
        code, out, err = self.pk("integrate", "close", "r1")
        self.assertEqual((code, err), (0, ""), out)
        tree = read_json(self.root / "program" / "rounds" / "r1.json")["tree"]
        self.assertIn(f"round r1: main matches the tested tree {tree}", out)
        self.assertFalse((self.root / ".worktrees" / "round-r1").exists())
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        self.assertTrue((self.root / "second.py").is_file())

    def test_a_full_tier_task_is_never_a_member(self):
        sh(self.root, "git", "rm", "-q", f"program/status/{T2}.json")
        commit(self.root, "T2 has no tier on main: full")
        code, _, err = self.pk("integrate", "start", "r1", TASK, T2)
        self.assertEqual(code, 1)
        self.assertIn(f"{T2} is full-tier", err)
        self.assertFalse((self.root / "program" / "rounds").exists())
        self.assertFalse((self.root / ".worktrees" / "round-r1").exists())

    def test_a_member_must_be_accepted_before_the_round_starts(self):
        write_json(self.wt2 / "program" / "status" / f"{T2}.json", status_record(T2, "review", tier="standard"))
        commit(self.wt2, "back in review")
        code, _, err = self.pk("integrate", "start", "r1", TASK, T2)
        self.assertEqual(code, 1)
        self.assertIn("only a task QA has set done", err)

    def test_a_member_cannot_land_before_the_combined_check_passed(self):
        self.assertEqual(self.pk("integrate", "start", "r1", TASK, T2)[0], 0)
        code, _, err = self.pk("merge", TASK, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("has not passed its combined check", err)

    def test_a_failing_suite_fails_the_round_and_records_no_tested_tree(self):
        put(self.wt2, "test_second.py", "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                        "    def test_it(self):\n        self.fail('combined')\n")
        commit(self.wt2, "a failing test")
        self.accept(self.wt2, T2)
        self.assertEqual(self.pk("integrate", "start", "r1", TASK, T2)[0], 0)
        code, out, _ = self.pk("integrate", "check", "r1")
        self.assertEqual(code, 1)
        self.assertNotIn("OK: ", out)
        self.assertEqual(read_json(self.root / "program" / "rounds" / "r1.json")["tree"], "")

    def test_a_member_that_moved_after_the_check_is_not_covered_by_it(self):
        self.start_and_check()
        put(self.wt2, "second.py", "VALUE = 3\n")
        commit(self.wt2, "a late change")
        self.accept(self.wt2, T2)
        before = self.head()
        code, _, err = self.pk("merge", T2, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("moved since round r1 was checked", err)
        self.assertEqual(self.head(), before)

    def test_a_main_that_moved_outside_the_round_voids_it(self):
        self.start_and_check()
        put(self.root, "hotfix.py", "X = 1\n")
        commit(self.root, "a commit that is not a member")
        code, _, err = self.pk("merge", TASK, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("main moved outside round r1", err)

    def test_a_change_to_the_rules_on_main_voids_the_round(self):
        self.start_and_check()
        self.write_checks(tiers={"full_paths": ["auth/", "second.py"], "light_paths": [], "light_ruling": ""})
        commit(self.root, "second.py is full-tier now")  # checks.yaml is not a controller document
        before = self.head()
        code, _, err = self.pk("merge", T2, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("main moved outside round r1", err)
        self.assertEqual(self.head(), before)

    def test_close_fails_on_a_difference_from_the_tested_tree_until_it_is_rechecked(self):
        self.start_and_check()
        for task_id in (TASK, T2):
            self.assertEqual(self.pk("merge", task_id, "--round", "r1")[0], 0)
        put(self.root, "second.py", "VALUE = 4\n")
        commit(self.root, "a small fix after the round")
        code, _, err = self.pk("integrate", "close", "r1")
        self.assertEqual(code, 1)
        self.assertIn("changed since the tested tree: second.py", err)
        self.assertEqual(read_json(self.root / "program" / "rounds" / "r1.json")["closed_at"], "")
        scans = self.scans()
        code, out, err = self.pk("integrate", "close", "r1", "--recheck")
        self.assertEqual((code, err), (0, ""), out)
        self.assertGreater(self.scans(), scans)  # main's checks ran over the final tree
        self.assertEqual(read_json(self.root / "program" / "rounds" / "r1.json")["rechecked"], ["second.py"])

    def test_close_waits_for_every_member(self):
        self.start_and_check()
        self.assertEqual(self.pk("merge", TASK, "--round", "r1")[0], 0)
        code, _, err = self.pk("integrate", "close", "r1")
        self.assertEqual(code, 1)
        self.assertIn(f"not merged yet: {T2}", err)


if __name__ == "__main__":
    import unittest
    unittest.main()
