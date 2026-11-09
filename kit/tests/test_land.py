"""Landing work: the review worktree, the safe finish, `pk merge` and the integration round.

Each test of a shortcut tries the thing the shortcut must not allow: merging a branch nobody accepted, the
word `done` typed into a status file, a commit made after the reviewed commit, a merge commit that carries a
change of its own, a light merge nobody inspected, a round with a full-tier member, a member that moved after
the combined check, a round record written by hand, a main that moved outside the round, a tested tree that
no longer matches main, a finish that would lose work or reach another project's containers.

An accepted branch here is accepted for real: `produce.py` writes a results record with the kit's evidence
writer, the implementer's evidence commit follows, and QA's acceptance is appended with `pk evidence accept`
and `pk status set done`. Nothing in these tests writes `done` into a status file, except the tests of that
forgery.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
from _slow import skip_unless_slow
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evidence
import gate
import handoff
import land
import latework
import pk
import rulings
import status
import store
import worktree
from common import KitError, read_json, sha256_bytes, write_json, write_yaml
from test_gate import BRANCH, CALC, PLAN, PRODUCE, PY, REVIEW, TASK, commit, put, run_quiet, sh
from test_status import full_review, write_register
from test_tiers import FULL_PATHS, LIGHT_BRIEF, TierGateBase, status_record

KIT = Path(__file__).resolve().parent.parent
T2 = "P0-M01-S01-T02"
STANDARD_SPEC = "# Spec\n\nTier: standard\n"


class LandBase(TierGateBase):
    def setUp(self):
        super().setUp()
        shutil.copy(KIT / "templates" / "HANDOFF.md", self.root / "program" / "HANDOFF.md")
        shutil.copy(KIT / "templates" / "controller-state.json", self.root / "program" / "controller-state.json")
        write_register(self.root, (TASK, T2))
        commit(self.root, "hand-off")
        sh(self.wt, "git", "merge", "-q", "main")

    def standard(self, done=True):
        """Make the fixture task a standard-tier task and, with `done`, one QA has accepted on its branch."""
        self.on_main(status_record(TASK, tier="standard"), FULL_PATHS,
                     files={f"program/tasks/{TASK}/SPEC.md": STANDARD_SPEC})
        if done:
            self.accept(self.wt, TASK)

    def evidenced(self, wt, task_id):
        """The implementer's part: a results record at the tip, `review`, and the evidence commit; the record's head."""
        reviewed = self.head(wt)
        sh(wt, PY, f"program/evidence/{task_id}/produce.py")
        if status.read(wt, task_id)["status"] == "in_progress":
            status.transition(wt, task_id, "review", "implementer")
        commit(wt, "evidence")
        return reviewed

    def qa_accepts(self, wt, task_id, **review):
        """QA's part, in the task worktree: `pk evidence accept`, `done`, one commit; that commit."""
        entry, record = store.load_latest(wt, task_id, "results")
        evidence.accept(wt, task_id, {
            "task_id": task_id, "decision": "accepted_development", "evidence_sha256": entry["digest"],
            "source_commit": record["head"], "accepted_at": "2026-10-13T00:00:00Z", "role": "qa",
            "commands": [], "planted_bugs": [], "findings": [], "blocked_by": [], **review})
        if status.read(wt, task_id)["status"] != "done":
            status.transition(wt, task_id, "done", "qa")
        return commit(wt, "accepted")

    def accept(self, wt, task_id):
        self.evidenced(wt, task_id)
        return self.qa_accepts(wt, task_id)

    def typed_done(self, wt, task_id, tier="standard"):
        """The forgery: the word `done` written into the branch's status file by hand, with no evidence behind it."""
        write_json(wt / "program" / "status" / f"{task_id}.json",
                   status_record(task_id, "done", tier=tier, role="qa", note=f"accepted at {self.head(wt)}"))
        return commit(wt, "done, typed")

    def second_task(self, files=None):
        """A second standard-tier task on main with its own accepted branch; its worktree."""
        for name, text in (("SPEC.md", STANDARD_SPEC), ("PLAN.md", PLAN), ("review.md", REVIEW)):
            put(self.root, f"program/tasks/{T2}/{name}", text)
        write_json(self.root / "program" / "status" / f"{T2}.json", status_record(T2, tier="standard"))
        commit(self.root, "dispatch T2")
        wt = self.root / ".worktrees" / T2.lower()
        sh(self.root, "git", "worktree", "add", "-q", "-b", f"task/{T2}", str(wt))
        put(wt, "second.py", "VALUE = 2\n")
        put(wt, f"program/evidence/{T2}/produce.py", PRODUCE.replace(TASK, T2))
        for name, text in (files or {}).items():
            put(wt, name, text)
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

    def untouched(self, before):
        """A refusal merged nothing, wrote nothing and left no temporary worktree behind."""
        self.assertEqual(self.head(), before)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        self.assert_no_trial_left()


class ReviewWorktreeTests(LandBase):
    def test_the_reviewer_gets_a_detached_worktree_a_diff_file_and_what_the_diff_leaves_out(self):
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
        # what was left out is said, and the stat of the whole range still shows every path that changed
        self.assertIn("left out of the diff: program/evidence, program/status, generated.lock", out.splitlines())
        stat = (self.root / ".worktrees" / f"{TASK.lower()}-review.stat").read_text()
        self.assertIn(f"commit under review: {reviewed}", stat)
        self.assertIn("program/evidence, program/status, generated.lock", stat)
        for name in ("generated.lock", f"program/status/{TASK}.json", "calc.py"):
            self.assertRegex(stat, rf"(?m)^ {name} +\| ")
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")  # nothing tracked was touched
        self.assertEqual(out.splitlines()[-1], f"merge with main: no conflicts at {self.head()[:12]}")  # KF-38

    def test_a_second_review_moves_the_clean_worktree_to_the_new_commit(self):
        """The re-review after a fix round (M1, second form): one command puts the reviewer on the new commit."""
        first = worktree.review(self.root, TASK)["commit"]
        path = self.root / ".worktrees" / f"{TASK.lower()}-review"
        put(self.wt, "fix.py", "FIXED = True\n")
        second = commit(self.wt, "fix round")
        self.assertEqual(self.head(path), first)
        code, out, err = self.pk("worktree", "review", TASK)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], f"{path} (moved to the new commit)")
        self.assertIn(f"commit: {second}", out.splitlines())
        self.assertEqual(self.head(path), second)
        with self.assertRaises(Exception):  # still detached
            sh(path, "git", "symbolic-ref", "HEAD")
        self.assertIn("FIXED = True", (self.root / ".worktrees" / f"{TASK.lower()}-review.diff").read_text())
        self.assertIn(second, (self.root / ".worktrees" / f"{TASK.lower()}-review.stat").read_text())

    def test_a_review_worktree_that_holds_a_file_or_a_commit_of_its_own_is_never_moved(self):
        worktree.review(self.root, TASK)
        path = self.root / ".worktrees" / f"{TASK.lower()}-review"
        before = self.head(path)
        put(self.wt, "fix.py", "FIXED = True\n")
        commit(self.wt, "fix round")
        (path / "planted.txt").write_text("a planted bug nobody restored\n")
        code, _, err = self.pk("worktree", "review", TASK)
        self.assertEqual(code, 1)
        self.assertIn("holds uncommitted or untracked files: planted.txt", err)
        self.assertEqual(self.head(path), before)
        own = commit(path, "the reviewer's own commit, on no branch")
        code, _, err = self.pk("worktree", "review", TASK)
        self.assertEqual(code, 1)
        self.assertIn(f"stands on {own[:12]}, a commit no branch holds", err)
        self.assertEqual(self.head(path), own)

    def test_a_review_worktree_is_refused_for_an_unknown_branch_and_for_a_commit_off_the_branch(self):
        with self.assertRaisesRegex(KitError, "branch task/P9-M01-S01-T01 not found"):
            worktree.review(self.root, "P9-M01-S01-T01")
        put(self.root, "main-only.txt", "x\n")
        other = commit(self.root, "main moved")
        with self.assertRaisesRegex(KitError, "is not a commit of task/"):
            worktree.review(self.root, TASK, commit=other)
        self.assertFalse((self.root / ".worktrees" / f"{TASK.lower()}-review").exists())

    def test_an_ad_hoc_task_gets_a_worktree_like_any_other(self):
        path = worktree.new(self.root, "AH-0001")
        self.assertEqual(path.name, "ah-0001")
        with self.assertRaisesRegex(KitError, "not a task id"):
            worktree.new(self.root, "AH-1")


@skip_unless_slow()
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
        for kind in ("diff", "stat"):
            self.assertTrue(any(line.startswith(f"removed: review {kind}") for line in lines), kind)
            self.assertFalse((self.root / ".worktrees" / f"{TASK.lower()}-review.{kind}").exists())
        self.assertFalse(self.wt.exists())
        self.assertNotIn(BRANCH, sh(self.root, "git", "branch"))
        self.assertTrue((self.root / "calc.py").is_file())  # the link went, not what it pointed at

    def test_a_named_path_that_is_a_real_file_is_not_treated_as_a_symlink(self):
        self.merged()
        (self.wt / "node_modules").write_text("a real file\n")
        code, _, err = self.pk("worktree", "finish", TASK, "--symlink", "node_modules")
        self.assertEqual(code, 1)
        self.assertTrue((self.wt / "node_modules").is_file())

    # What the stand-in container tool holds: four compose projects whose names begin alike, and a container of
    # project `proj` that carries a name of its own. Asked with the label filter it answers as the real tool
    # does, with the exact project only; asked without it, it lists everything, so a selection by name would
    # be seen removing the siblings.
    TOOL = ('echo "$@" >> "$PK_FAKE_LOG"\n'
            'case "$*" in\n'
            '  "ps -a --filter label=com.docker.compose.project=proj --format "*)'
            ' printf "proj-web-1\\tproj\\nweb\\tproj\\n";;\n'
            '  "volume ls --filter label=com.docker.compose.project=proj --format "*)'
            ' printf "proj_data\\tproj\\n";;\n'
            '  "ps -a"*) printf "proj-web-1\\nweb\\nproj-dev-db-1\\nproj_e2e_db_1\\nproj2-web-1\\nother\\n";;\n'
            '  "volume ls"*) printf "proj_data\\nproj-dev_pgdata\\nproj_e2e_pgdata\\nproj2_data\\nshared\\n";;\n'
            '  "image ls"*) printf "proj-web:latest\\nproj-dev-api:latest\\npostgres:16\\n";;\n'
            '  "rm web") [ -z "$PK_RM_WEB" ] || { echo "Error: container web is running" >&2; exit 1; };;\n'
            'esac')

    def test_finish_removes_only_what_carries_the_exact_compose_project_label_and_never_forces(self):
        """The reviewer's sequence (M4): `proj` beside `proj-dev`, `proj_e2e` and `proj2`."""
        self.merged()
        self.wrapper("docker", self.TOOL)
        code, out, err = self.pk("worktree", "finish", TASK, "--compose-project", "proj")
        self.assertEqual((code, err), (0, ""))
        removed = [line for line in out.splitlines() if line.split()[1] in ("container", "volume", "image")]
        self.assertEqual(removed, ["removed: container proj-web-1", "removed: container web",
                                   "removed: volume proj_data"])
        calls = self.fake_log.read_text().splitlines()
        label = '{{.Label "com.docker.compose.project"}}'
        self.assertEqual(calls, ["ps -a --filter label=com.docker.compose.project=proj --format "
                                 "{{.Names}}\t" + label,
                                 "volume ls --filter label=com.docker.compose.project=proj --format "
                                 "{{.Name}}\t" + label,
                                 "rm proj-web-1", "rm web", "volume rm proj_data"])
        for sibling in ("proj-dev", "proj_e2e", "proj2", "image", "prune"):
            self.assertFalse([call for call in calls if sibling in call], sibling)
        self.assertFalse([call for call in calls if "-f" in call.split() or "--force" in call.split()])
        with self.assertRaisesRegex(KitError, "not a usable compose project name"):
            worktree.finish(self.root, TASK, compose_project="t")

    def test_finish_does_not_trust_a_tool_whose_filter_lets_a_similarly_named_project_through(self):
        """N8: a tool that matches the label by prefix answers with the siblings too; only the exact label is taken."""
        self.merged()
        self.wrapper("docker", 'echo "$@" >> "$PK_FAKE_LOG"\n'
                     'case "$*" in\n'
                     '  "ps -a"*) printf "proj-web-1\\tproj\\nproj-dev-db-1\\tproj-dev\\nproj2-web-1\\tproj2\\n'
                     'orphan\\t\\nbare\\n";;\n'
                     '  "volume ls"*) printf "proj_data\\tproj\\nproj_e2e_pgdata\\tproj_e2e\\n";;\n'
                     'esac')
        code, out, err = self.pk("worktree", "finish", TASK, "--compose-project", "proj")
        self.assertEqual((code, err), (0, ""))
        removed = [line for line in out.splitlines() if line.split()[1] in ("container", "volume")]
        self.assertEqual(removed, ["removed: container proj-web-1", "removed: volume proj_data"])
        calls = self.fake_log.read_text().splitlines()
        self.assertEqual(calls[2:], ["rm proj-web-1", "volume rm proj_data"])

    def test_finish_asks_the_container_tool_before_it_removes_anything(self):
        self.merged()
        self.wrapper("docker", 'echo "Cannot connect to the daemon" >&2\nexit 1')
        code, out, err = self.pk("worktree", "finish", TASK, "--compose-project", "proj")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Cannot connect to the daemon", err)
        self.assertTrue(self.wt.is_dir())  # nothing went: the listing failed before the first removal
        self.assertIn(BRANCH, sh(self.root, "git", "branch"))

    def test_a_container_in_use_stops_finish_and_what_went_and_what_stayed_is_said(self):
        self.merged()
        self.wrapper("docker", self.TOOL)
        with mock.patch.dict(os.environ, {"PK_RM_WEB": "1"}):
            code, out, err = self.pk("worktree", "finish", TASK, "--compose-project", "proj")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("removed: container proj-web-1", err)
        self.assertIn("container web is running", err)
        self.assertIn("not removed: container web, volume proj_data", err)
        self.assertNotIn("volume rm proj_data", self.fake_log.read_text())  # a volume is not taken from under it

    def test_finish_refuses_to_lose_a_commit_no_branch_holds(self):
        self.merged()
        worktree.review(self.root, TASK)
        review = self.root / ".worktrees" / f"{TASK.lower()}-review"
        put(review, "finding.py", "X = 1\n")
        own = commit(review, "a commit made in the review worktree")
        code, _, err = self.pk("worktree", "finish", TASK)
        self.assertEqual(code, 1)
        self.assertIn(f"stands on {own[:12]}, a commit no branch holds", err)
        self.assertTrue(review.is_dir())
        self.assertTrue(self.wt.is_dir())
        self.assertIn(BRANCH, sh(self.root, "git", "branch"))
        sh(self.root, "git", "branch", "kept", own)  # the way out the message names
        self.assertEqual(self.pk("worktree", "finish", TASK)[0], 0)
        self.assertIn(own, sh(self.root, "git", "rev-parse", "kept"))

    def test_finish_names_the_ignored_files_and_drops_them_only_when_told(self):
        self.merged()
        (self.wt / "__pycache__").mkdir()
        (self.wt / "__pycache__" / "local.pyc").write_text("a git-ignored file\n")
        code, _, err = self.pk("worktree", "finish", TASK)
        self.assertEqual(code, 1)
        self.assertIn("holds git-ignored files that would go with it: __pycache__/", err)
        self.assertTrue((self.wt / "__pycache__" / "local.pyc").is_file())
        code, out, err = self.pk("worktree", "finish", TASK, "--drop-ignored")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"dropped: ignored {self.wt / '__pycache__/'}", out.splitlines())
        self.assertFalse(self.wt.exists())

    def test_finish_runs_from_the_main_checkout_only(self):
        self.merged()
        with self.assertRaisesRegex(KitError, "from the main checkout"):
            worktree.finish(self.wt, TASK)
        self.assertTrue(self.wt.is_dir())


class SignalKillTests(unittest.TestCase):
    """KF-29 (0.5.5): a command the machine killed is named, and pk merge runs its merge check once more."""

    def test_a_command_killed_by_a_signal_is_named_directly_and_through_a_wrapper(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        kill = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
        passed, lines = gate._step_lines("test", [([PY, "-c", kill], 0)], tmp)
        self.assertFalse(passed)
        self.assertIn("(exit -9, killed by SIGKILL (often the machine running out of memory", lines[0])
        passed, lines = gate._step_lines("test", [(["sh", "-c", f"{PY} -c '{kill}'; exit $?"], 0)], tmp)
        self.assertIn("(exit 137, killed by SIGKILL", lines[0])
        passed, lines = gate._step_lines("test", [([PY, "-c", "raise SystemExit(3)"], 0)], tmp)
        self.assertEqual(lines[0], f"FAILED: test {PY} -c raise SystemExit(3) (exit 3)")  # an ordinary failure
        self.assertIsNone(gate.signal_name(130))  # 128+SIGINT: a program's own code in that range is not a kill
        self.assertEqual(gate.signal_name(143), "SIGTERM")

    def fake_checks(self, *runs):
        calls = []

        def merge_check(root, branch, inspected=None, suite=True):
            calls.append(branch)
            code, lines = runs[len(calls) - 1]
            for line in lines:
                print(line)
            return code

        return calls, mock.patch.object(land.gate, "merge_check", merge_check)

    def test_the_merge_check_runs_once_more_after_a_signal_kill_and_says_so(self):
        killed = (1, ["step build: ok", "FAILED: test pnpm test (exit 137, killed by SIGKILL (often ...))"])
        calls, patch = self.fake_checks(killed, (0, ["step test: ok", f"OK: {BRANCH} merges into main at abc"]))
        with patch:
            code, printed, retried = land._merge_check(Path("."), BRANCH, None, True)
        self.assertEqual((code, len(calls)), (0, 2))
        self.assertEqual(printed[-1], f"OK: {BRANCH} merges into main at abc")
        self.assertIn("the first run was killed by SIGKILL (test pnpm test (exit 137", retried)

    def test_any_other_failure_and_a_second_kill_are_final(self):
        calls, patch = self.fake_checks((1, ["FAILED: test pnpm test (exit 1)"]))
        with patch:
            self.assertEqual(land._merge_check(Path("."), BRANCH, None, True)[::2], (1, ""))
        self.assertEqual(len(calls), 1)  # an ordinary failure is the branch's: never run again
        killed = (1, ["FAILED: test pnpm test (exit -9, killed by SIGKILL)"])
        calls, patch = self.fake_checks(killed, killed)
        with patch:
            code, _, retried = land._merge_check(Path("."), BRANCH, None, True)
        self.assertEqual((code, len(calls)), (1, 2))  # once more, not in a loop
        self.assertTrue(retried)


@skip_unless_slow()
class MergeTests(LandBase):
    def test_a_branch_qa_has_not_accepted_is_never_merged(self):
        self.standard(done=False)
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("only a task QA has set done is merged", err)
        self.untouched(before)

    def typed_done_is_refused(self, tier):
        self.typed_done(self.wt, TASK, tier=tier)
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"{TASK}: done needs an acceptance record in program/evidence/{TASK}/latest.json", err)
        self.assertIn("its branch says done, but the evidence committed at its tip does not accept it", err)
        self.untouched(before)

    def test_the_word_done_typed_into_the_status_file_is_not_an_acceptance(self):
        """The reviewer's sequence (M2), full tier: `"status": "done", "role": "qa"` on the branch, no evidence."""
        self.typed_done_is_refused("full")

    def test_a_typed_done_is_refused_on_the_standard_tier_as_well(self):
        self.standard(done=False)
        self.typed_done_is_refused("standard")

    def test_a_typed_done_over_a_results_record_nobody_accepted_is_refused_too(self):
        self.standard(done=False)
        self.evidenced(self.wt, TASK)  # a real record, the evidence commit, and no acceptance
        self.typed_done(self.wt, TASK)
        before = self.head()
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("done needs an acceptance record", err)
        self.untouched(before)

    def tierless_done(self):
        """The re-review's N2 up to the merge: a standard-depth acceptance, then `done` typed with the tier deleted."""
        self.evidenced(self.wt, TASK)
        entry, record = store.load_latest(self.wt, TASK, "results")
        evidence.accept(self.wt, TASK, {
            "task_id": TASK, "decision": "accepted_development", "evidence_sha256": entry["digest"],
            "source_commit": record["head"], "accepted_at": "2026-10-13T00:00:00Z", "role": "qa",
            "commands": [], "planted_bugs": [], "findings": [], "blocked_by": []})
        with self.assertRaisesRegex(KitError, "accepted only on the full tier's review"):
            status.transition(self.wt, TASK, "done", "qa")  # the status machine refuses it
        path = self.wt / "program" / "status" / f"{TASK}.json"
        typed = dict(read_json(path), status="done", role="qa")
        for key in ("tier", "fast", "ruling"):
            typed.pop(key, None)
        for item in typed["history"]:
            item.pop("tier", None)
        write_json(path, typed)
        commit(self.wt, "done, typed, without the tier")

    def test_a_full_tier_branch_that_deletes_its_tier_is_not_exempt_from_the_full_review(self):
        self.on_main(status_record(TASK))  # dispatched on main at the full tier
        self.tierless_done()
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("a full-tier task is accepted only on the full tier's review", err)
        self.untouched(before)

    def test_a_record_written_before_tiers_still_lands_and_validates(self):
        bare = status_record(TASK)
        for key in ("tier", "fast", "ruling"):
            del bare[key]
        bare["history"][0].pop("tier")
        self.on_main(bare)  # dispatched before tiers existed: main's record names no tier
        self.tierless_done()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(status.validate(self.root), [])

    def late_commit(self):
        """The reviewer's sequence (M1) up to QA's acceptance; (the reviewed commit, the late commit)."""
        self.standard(done=False)
        rc, out = self.handback()  # the real gate: its OK line, and a results record whose head is the tip
        self.assertEqual(rc, 0, out)
        reviewed = self.head(self.wt)
        status.transition(self.wt, TASK, "review", "implementer")
        commit(self.wt, "evidence")
        made = worktree.review(self.root, TASK)  # the review worktree and the diff file QA is handed
        put(self.wt, "backdoor.py", "OPEN = True\n")  # the implementer goes on, after the diff was written
        put(self.wt, "calc.py", (self.wt / "calc.py").read_text() + "\n\ndef div(a, b):\n    return a / b\n")
        late = commit(self.wt, "more work, during the review")
        self.assertNotIn("backdoor", made["diff"].read_text())
        problems, said = run_quiet(gate.binding, self.root, TASK, BRANCH)  # qa.md step 2: it holds, and reports drift
        self.assertEqual(problems, [])
        self.assertIn("drifted since", said)
        with self.assertRaises(KitError) as ctx:  # 0.5.3: pk evidence accept sees the late work first
            self.qa_accepts(self.wt, TASK)
        self.assertIn(f"pk evidence accept: {TASK}: {late[:12]} was committed after the reviewed commit "
                      f"{reviewed[:12]} and changes backdoor.py, calc.py", str(ctx.exception))
        with mock.patch.object(latework, "early_problem", return_value=None):  # an acceptance made before 0.5.3
            self.qa_accepts(self.wt, TASK)  # qa.md step 7, in the task worktree, at whatever the tip is by now
        return reviewed, late

    def test_a_commit_made_after_the_reviewed_commit_does_not_reach_main(self):
        reviewed, late = self.late_commit()
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"{TASK}: {late[:12]} was committed after the reviewed commit {reviewed[:12]} and changes "
                      "backdoor.py, calc.py", err)
        self.assertIn(f"pk worktree review {TASK}", err)
        self.untouched(before)
        self.assertFalse((self.root / "backdoor.py").exists())

    def test_reverting_the_late_commit_does_not_make_it_reviewed(self):
        _, late = self.late_commit()
        sh(self.wt, "git", "revert", "--no-edit", late)
        before = self.head()
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("after the reviewed commit", err)
        self.untouched(before)

    def test_late_work_lands_once_the_gate_ran_on_it_and_qa_accepted_that_record(self):
        self.late_commit()
        rc, out = self.handback()  # a new results record that names the tip
        self.assertEqual(rc, 0, out)
        commit(self.wt, "evidence of the fix round")
        code, out, err = self.pk("worktree", "review", TASK)  # the reviewer moves to the commit it re-checks
        self.assertEqual((code, err), (0, ""))
        self.assertIn("backdoor.py", (self.root / ".worktrees" / f"{TASK.lower()}-review.diff").read_text())
        self.qa_accepts(self.wt, TASK)
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertTrue((self.root / "backdoor.py").is_file())  # reviewed now, so it lands
        self.assertEqual(status.validate(self.root), [])

    def late_evidence_file(self, name):
        """The re-review's N3: after QA's acceptance, a file under the task's evidence folder is committed."""
        self.standard()
        put(self.wt, f"program/evidence/{TASK}/{name}", "import os\n")
        late = commit(self.wt, f"late {name}")
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"{late[:12]} was committed after the reviewed commit", err)
        self.assertIn(f"changes program/evidence/{TASK}/{name}:", err)
        self.untouched(before)

    def test_a_conftest_added_to_the_evidence_folder_after_the_review_does_not_reach_main(self):
        self.late_evidence_file("conftest.py")

    def test_a_test_file_added_to_the_evidence_folder_after_the_review_does_not_reach_main(self):
        self.late_evidence_file("test_late.py")

    def test_a_late_file_under_results_is_one_the_kit_writes_and_lands(self):
        self.standard()
        entry, _ = store.load_latest(self.wt, TASK, "results")
        raw = (self.wt / entry["path"]).read_bytes()  # an older-named copy: the pointer still names the latest
        put(self.wt, f"program/evidence/{TASK}/results/20000101T000000000000Z-{entry['digest'][:12]}.json",
            raw.decode("utf-8"))
        commit(self.wt, "a late results file")
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)

    def test_qa_may_name_the_commit_that_added_only_the_report_as_its_source_commit(self):
        # KF-28 (0.5.5): AH-0067 and AH-0074 in the program this came from were refused for exactly this
        self.standard(done=False)
        reviewed = self.evidenced(self.wt, TASK)
        put(self.wt, f"program/evidence/{TASK}/report.md", "# Report\n")
        report = commit(self.wt, "the implementer's report")
        entry, record = store.load_latest(self.wt, TASK, "results")
        self.assertNotEqual(report, record["head"])
        accepted = {"task_id": TASK, "decision": "accepted_development", "evidence_sha256": entry["digest"],
                    "source_commit": report, "accepted_at": "2026-11-07T00:00:00Z", "role": "qa",
                    "commands": [], "planted_bugs": [], "findings": [], "blocked_by": []}
        evidence.accept(self.wt, TASK, accepted)
        status.transition(self.wt, TASK, "done", "qa")
        commit(self.wt, "accepted at the report commit")
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIsNone(latework.source_problem(self.root, TASK, record["head"], report))
        self.assertIn(reviewed[:12], latework.source_problem(self.root, TASK, report, record["head"]))

    def test_a_source_commit_after_work_nobody_reviewed_is_still_refused(self):
        self.standard(done=False)
        self.evidenced(self.wt, TASK)
        put(self.wt, "calc.py", (self.wt / "calc.py").read_text() + "\n\ndef div(a, b):\n    return a / b\n")
        late = commit(self.wt, "late work")
        entry, record = store.load_latest(self.wt, TASK, "results")
        accepted = {"task_id": TASK, "decision": "accepted_development", "evidence_sha256": entry["digest"],
                    "source_commit": late, "accepted_at": "2026-11-07T00:00:00Z", "role": "qa",
                    "commands": [], "planted_bugs": [], "findings": [], "blocked_by": []}
        with self.assertRaisesRegex(KitError, r"nor a later commit that adds only the task's own late files.*"
                                              r"changes calc.py"):
            evidence.accept(self.wt, TASK, accepted)

    def test_a_late_acceptance_record_is_one_the_kit_writes_and_lands(self):
        self.standard()
        entry, record = store.load_latest(self.wt, TASK, "results")
        evidence.accept(self.wt, TASK, {  # QA accepts the same record again: a genuine acceptance/ record
            "task_id": TASK, "decision": "accepted_development", "evidence_sha256": entry["digest"],
            "source_commit": record["head"], "accepted_at": "2026-10-14T00:00:00Z", "role": "qa",
            "commands": [], "planted_bugs": [], "findings": [], "blocked_by": []})
        commit(self.wt, "a late acceptance record")
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(status.validate(self.root), [])

    def test_a_file_that_is_not_a_record_under_results_or_acceptance_does_not_reach_main(self):
        """The re-review's R2-1: test files placed in results/ and acceptance/ after the review."""
        self.standard()
        names = [f"program/evidence/{TASK}/results/conftest.py", f"program/evidence/{TASK}/results/test_late.py",
                 f"program/evidence/{TASK}/acceptance/test_also.py",
                 f"program/evidence/{TASK}/results/sub/20000101T000000000000Z-000000000000.json"]
        for name in names:
            put(self.wt, name, "import os\n")
        late = commit(self.wt, "late files in the record folders")
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"{late[:12]} was committed after the reviewed commit", err)
        self.assertIn("changes " + ", ".join(sorted(names)) + ":", err)
        self.untouched(before)

    def test_a_record_shaped_file_whose_bytes_are_not_its_name_does_not_reach_main(self):
        """The re-review's R2-1, second form: a record name over bytes that do not hash to it."""
        self.standard()
        name = f"program/evidence/{TASK}/results/20000101T000000000000Z-000000000000.json"
        put(self.wt, name, "{}\n")
        commit(self.wt, "an impostor record")
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"fails the store's integrity check: immutable record altered: {name}", err)
        self.untouched(before)

    def test_a_digest_named_file_that_is_not_a_record_does_not_reach_main(self):
        """F-0355: a name that matches its own digest proves the bytes, not that they are a record."""
        self.standard()
        body = '{"hello": "world"}\n'
        name = f"program/evidence/{TASK}/results/20000101T000000000000Z-{sha256_bytes(body.encode())[:12]}.json"
        put(self.wt, name, body)
        commit(self.wt, "a file named after its own digest")
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"fails the store's integrity check: {name}: not a valid results record: missing key", err)
        self.untouched(before)

    def test_a_merge_commit_that_carries_a_change_of_its_own_does_not_reach_main(self):
        self.standard()
        put(self.root, "main-only.txt", "x\n")
        commit(self.root, "main moves")
        sh(self.wt, "git", "merge", "-q", "--no-commit", "--no-ff", "main")
        put(self.wt, "backdoor.py", "OPEN = True\n")
        merge = commit(self.wt, "Merge main")  # reads like any merge of main; its tree is not what git made
        before = self.head()
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn(f"{merge[:12]} is a merge that is not what git makes of its two parents", err)
        self.assertIn("changes backdoor.py", err)
        self.untouched(before)

    def test_a_conflict_resolved_by_hand_after_the_review_does_not_reach_main(self):
        self.standard()
        put(self.root, "calc.py", CALC + "\n\ndef neg(a):\n    return -a\n")  # where the branch added mul
        commit(self.root, "main changes calc.py")
        with self.assertRaises(subprocess.CalledProcessError):
            sh(self.wt, "git", "merge", "-q", "main")
        put(self.wt, "calc.py", CALC + "\n\ndef mul(a, b):\n    return a * b * 1\n")  # a resolution nobody reviewed
        merge = commit(self.wt, "Merge main")
        before = self.head()
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn(f"{merge[:12]} is a merge that is not what git makes of its two parents", err)
        self.assertIn("changes calc.py", err)
        self.untouched(before)

    def test_one_command_checks_merges_and_commits_the_bookkeeping(self):
        self.standard()
        write_yaml(self.root / "program" / "gates.yaml",
                   {"gates": {"G-P0": {"environment": "dev", "records": [{"task": TASK}]}}})
        write_yaml(self.root / "program" / "environments.yaml", {"environments": {"dev": {"exists": True}}})
        commit(self.root, "gates")
        sh(self.wt, "git", "merge", "-q", "main")  # a clean merge of main after the acceptance is not late work
        put(self.wt, f"program/evidence/{TASK}/report.md", "# QA report\n")  # nor is the task's own evidence folder
        commit(self.wt, "the report")
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
        self.assertEqual(status.validate(self.root), [])
        self.assert_no_trial_left()

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

    def test_merge_never_commits_a_ruling_somebody_rewrote(self):
        self.standard()
        rulings.add(self.root, "controller", "Which port?", "Port 8080.")
        commit(self.root, "R1")
        log = self.root / "program" / "rulings.md"
        log.write_text(log.read_text().replace("Port 8080.", "Port 9090."))  # R1 rewritten in the working tree
        before = self.head()
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("hard rule 23", err)
        self.assertEqual(self.head(), before)
        self.assertIn("Port 8080.", sh(self.root, "git", "show", "HEAD:program/rulings.md"))
        sh(self.root, "git", "checkout", "--", "program/rulings.md")
        rulings.add(self.root, "controller", "Which port, again?", "Port 9090; supersedes R1.")  # appended: fine
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)


STATUS = f"program/status/{TASK}.json"


@skip_unless_slow()
class StatusConflictTests(LandBase):
    """0.6.0, KF-37: a conflict on the task's own status file takes the branch's record, in the trial and the merge."""

    def main_moves_the_record(self, note="main's copy", **over):
        """Main changes the task's record after the branch forked: the conflict the program it came from saw."""
        write_json(self.root / STATUS, status_record(TASK, tier="standard", note=note, **over))
        return commit(self.root, "main changes the status record")

    def test_a_status_file_conflict_with_main_takes_the_branchs_record_and_lands(self):
        self.standard()
        self.main_moves_the_record()
        branch_record = sh(self.root, "git", "show", f"{BRANCH}:{STATUS}")
        tip = self.head(self.wt)
        code, out, err = self.pk("merge", TASK, "--trailer", "Co-Authored-By: Test Agent")
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        self.assertLess(len(lines), 10)
        self.assertEqual(lines[0], f"OK: {BRANCH} merges into main at {tip}")
        self.assertIn(f"resolved: {STATUS}: took {BRANCH}'s record (the task's own status file)", lines)
        self.assertEqual((self.root / STATUS).read_text(), branch_record)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        log = sh(self.root, "git", "log", "--format=%s%n%(trailers)", "-2").splitlines()
        self.assertIn(f"Merge {BRANCH} (standard tier)", log)
        self.assertEqual(log.count("Co-Authored-By: Test Agent"), 2)
        parents = sh(self.root, "git", "rev-list", "--parents", "-n", "1", "HEAD~1").split()
        self.assertEqual(parents[2], tip)  # a real merge of the tip
        self.assertEqual(status.read(self.root, TASK)["status"], "done")
        self.assertEqual(status.validate(self.root), [])
        self.assert_no_trial_left()

    def test_the_merge_check_resolves_the_status_file_in_its_trial_merge_and_says_so(self):
        self.standard()
        self.main_moves_the_record()
        rc, out = self.merge_check()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"resolved: {STATUS}: took {BRANCH}'s record (the task's own status file)", out.splitlines())

    def test_a_branch_record_that_changes_the_tier_is_still_refused_after_the_conflict_is_resolved(self):
        self.standard(done=False)
        write_json(self.wt / STATUS, status_record(TASK, tier="full", note="the branch's"))
        commit(self.wt, "the branch raises its own tier")
        self.main_moves_the_record()
        rc, out = self.merge_check()
        self.assertEqual(rc, 1)
        self.assertIn(f"FAILED: tier {STATUS} changes the tier, fast path or ruling recorded on main", out)
        self.assert_no_trial_left()

    def test_another_conflicted_path_still_stops_the_merge_and_is_named(self):
        self.standard(done=False)
        put(self.wt, "notes.txt", "the branch's notes\n")
        commit(self.wt, "notes on the branch")
        self.accept(self.wt, TASK)
        put(self.root, "notes.txt", "main's notes\n")
        self.main_moves_the_record()
        before = self.head()
        code, out, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("conflicts in: notes.txt", err)
        self.assertNotIn(f"conflicts in: {STATUS}", err)
        self.untouched(before)

    def test_a_done_record_copied_to_main_by_hand_no_longer_blocks_reopen(self):
        """The 0.5.x workaround: the controller copied the branch's done record to main; the branch is not merged."""
        self.standard()
        shutil.copy(self.wt / STATUS, self.root / STATUS)
        commit(self.root, "the branch's status, copied to main by hand")
        said = []
        record = status.reopen(self.wt, TASK, "late work after acceptance", said=said)
        self.assertEqual(record["status"], "review")
        self.assertEqual(said, [f"note: main's {STATUS} says done, but {BRANCH} is not merged into main: the record "
                                f"there is a copy, and pk merge takes the branch's record"])

    def test_reopen_is_refused_once_the_branch_is_merged_even_after_later_commits(self):
        self.standard()
        self.assertEqual(self.pk("merge", TASK)[0], 0)
        with self.assertRaisesRegex(KitError, f"{BRANCH} is merged into main: it has landed"):
            status.reopen(self.wt, TASK, "too late")
        put(self.wt, "more.py", "MORE = 1\n")
        commit(self.wt, "more work on the landed branch")
        with self.assertRaisesRegex(KitError, f"{BRANCH} is merged into main: it has landed"):
            status.reopen(self.wt, TASK, "too late")


@skip_unless_slow()
class DirtyMainTests(LandBase):
    """0.6.0, KF-39: an uncommitted change on main stops pk merge only where the merge or its bookkeeping writes."""

    def test_an_unrelated_uncommitted_file_on_main_stays_uncommitted_and_out_of_every_commit(self):
        self.standard()
        before = self.head()
        put(self.root, "notes.txt", "the user's own edit\n")  # tracked, and the branch never touches it
        put(self.root, "scratch.txt", "the user's own file\n")  # untracked, at a path the merge does not write
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").splitlines(), [" M notes.txt", "?? scratch.txt"])
        self.assertEqual((self.root / "notes.txt").read_text(), "the user's own edit\n")
        committed = sh(self.root, "git", "log", "--format=", "--name-only", "-m", f"{before}..HEAD").split()
        self.assertNotIn("notes.txt", committed)
        self.assertNotIn("scratch.txt", committed)
        self.assertEqual(sh(self.root, "git", "show", "HEAD:notes.txt"), "one\n")
        self.assertEqual(status.read(self.root, TASK)["status"], "done")

    def test_an_uncommitted_change_where_the_merge_writes_is_refused_and_named(self):
        self.standard()
        before = self.head()
        produce = f"program/evidence/{TASK}/produce.py"
        for name in ("calc.py", produce, f"program/status/{TASK}.json"):
            with self.subTest(name):
                put(self.root, name, "the user's own edit\n")  # calc.py: changed by the branch; produce.py: added
                put(self.root, "notes.txt", "an unrelated edit\n")
                code, out, err = self.pk("merge", TASK)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"pk merge: the main checkout has uncommitted changes: {name}, which this merge or its "
                              "bookkeeping writes", err)
                self.assertNotIn("notes.txt", err)
                self.assertEqual(self.head(), before)
                sh(self.root, "git", "checkout", "--", "notes.txt")
                if name == produce:
                    (self.root / name).unlink()
                else:
                    sh(self.root, "git", "checkout", "--", name)
        self.untouched(before)

    def test_a_staged_change_is_still_refused_wherever_it_is(self):
        self.standard()
        put(self.root, "notes.txt", "staged\n")
        sh(self.root, "git", "add", "notes.txt")
        code, _, err = self.pk("merge", TASK)
        self.assertEqual(code, 1)
        self.assertIn("unstage what is staged first", err)


JOURNAL = "db/migrations/journal.txt"


@skip_unless_slow()
class UnionMergeTests(LandBase):
    """0.6.0, KF-38: a file of `merge.union_files` both sides appended to lands as the union of their lines."""

    def setUp(self):
        super().setUp()
        self.standard(done=False)
        self.write_checks(tiers=FULL_PATHS, merge={"union_files": ["db/migrations/"]})
        put(self.root, JOURNAL, "0001 init\n")
        commit(self.root, "union files and the journal")
        sh(self.wt, "git", "merge", "-q", "main")
        put(self.wt, JOURNAL, "0001 init\n0002 the branch's table\n")
        commit(self.wt, "the branch's migration")
        self.accept(self.wt, TASK)
        put(self.root, JOURNAL, "0001 init\n0002 main's table\n")
        commit(self.root, "main's migration")

    def test_the_review_and_pk_merge_check_see_the_conflict_the_kit_resolves(self):
        code, out, err = self.pk("worktree", "review", TASK)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], f"merge with main: no conflicts at {self.head()[:12]} (1 resolved as pk "
                                               "merge resolves them)")
        code, out, err = self.pk("merge", "check", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [
            f"resolved: {JOURNAL}: merged as the line union of both sides (merge.union_files)",
            f"OK: {BRANCH} merges into main at {self.head()} without conflicts"])

    def test_pk_merge_lands_the_union_of_both_sides(self):
        code, out, err = self.pk("merge", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"resolved: {JOURNAL}: merged as the line union of both sides (merge.union_files)",
                      out.splitlines())
        self.assertEqual((self.root / JOURNAL).read_text(), "0001 init\n0002 main's table\n0002 the branch's table\n")
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        self.assertEqual(sh(self.root, "git", "log", "-1", "--format=%s", "HEAD~1").strip(),
                         f"Merge {BRANCH} (standard tier)")
        self.assert_no_trial_left()

    def test_a_conflict_outside_the_union_files_is_named_by_the_review_line(self):
        put(self.root, "calc.py", "main's calc\n")
        commit(self.root, "main rewrites calc.py")
        code, out, _ = self.pk("worktree", "review", TASK)
        self.assertEqual(code, 0)
        self.assertIn(f"merge with main: conflicts at {self.head()[:12]} in: calc.py (1 resolved as pk merge resolves "
                      f"them); merge main into the branch and re-run the hand-back gate before QA (pk merge check "
                      f"{TASK})", out.splitlines())
        code, out, _ = self.pk("merge", "check", TASK)
        self.assertEqual((code, out), (1, f"resolved: {JOURNAL}: merged as the line union of both sides "
                                          f"(merge.union_files)\nFAILED: merge {BRANCH} into main at {self.head()} "
                                          "conflicts in: calc.py\n"))


@skip_unless_slow()
class LightMergeTests(LandBase):
    LIGHT = {"full_paths": ["auth/"], "light_paths": ["web/"], "light_ruling": "R1"}

    def setUp(self):
        super().setUp()
        sh(self.root, "git", "worktree", "remove", "--force", str(self.wt))
        sh(self.root, "git", "branch", "-D", BRANCH)
        sh(self.root, "git", "worktree", "add", "-q", "-b", BRANCH, str(self.wt))
        self.work("web/page.txt")
        self.on_main(status_record(TASK, tier="light", fast=True, ruling="R1"), self.LIGHT, rulings_count=1,
                     files={f"program/tasks/{TASK}/BRIEF.md": LIGHT_BRIEF})
        self.tip = self.head(self.wt)

    def test_a_light_task_lands_only_after_the_controller_inspected_its_tip_and_is_recorded_merged(self):
        before = self.head()
        for argv in ((), ("--inspected", "0" * 40), ("--inspected", self.head(self.wt) + "x")):
            code, _, err = self.pk("merge", TASK, *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("FAILED: inspection", err)
            self.assertEqual(self.head(), before)
        code, out, err = self.pk("merge", TASK, "--inspected", self.tip)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"{TASK}: merged (light tier: no QA, nothing evidenced)", out.splitlines())
        record = read_json(self.root / "program" / "status" / f"{TASK}.json")
        self.assertEqual((record["status"], record["tier"], record["role"]), ("merged", "light", "controller"))
        self.assertEqual(record["inspected"], self.tip)  # the commit that was read is in the record
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        self.assertEqual(status.validate(self.root), [])
        self.assertEqual(self.pk("worktree", "finish", TASK)[0], 0)
        self.assertEqual(status.validate(self.root), [])  # the audit of merged outlives the branch

    def test_a_light_task_somebody_called_done_does_not_land(self):
        self.typed_done(self.wt, TASK, tier="light")
        code, _, err = self.pk("merge", TASK, "--inspected", self.head(self.wt))
        self.assertEqual(code, 1)
        self.assertIn("lands from in_progress, not done", err)

    def test_a_light_branch_merged_by_hand_round_the_merge_check_is_not_recorded_merged(self):
        """The reviewer's sequence (M3), through the commands: merge-check fails, git merge by hand, status set."""
        self.work("auth/policy.py")  # a tiers.full_paths path
        tip = self.head(self.wt)
        rc, out = self.merge_check(inspected=tip)
        self.assertEqual(rc, 1)
        self.assertIn(f"FAILED: tier {TASK} is light but changes full-tier paths: auth/policy.py", out)
        sh(self.root, "git", "merge", "-q", "--no-ff", "-m", "by hand", BRANCH)
        with mock.patch.object(status, "find_root", return_value=self.root):
            code, _, err = self.pk("status", "set", TASK, "merged", "--role", "controller")
            self.assertEqual(code, 1)
            self.assertIn("merged needs the commit the controller inspected", err)
            code, _, err = self.pk("status", "set", TASK, "merged", "--role", "controller", "--inspected", tip)
            self.assertEqual(code, 1)
            self.assertIn("changed full-tier paths: auth/policy.py", err)
        self.assertEqual(status.read(self.root, TASK)["status"], "in_progress")
        write_json(self.root / "program" / "status" / f"{TASK}.json",
                   status_record(TASK, "merged", tier="light", fast=True, ruling="R1", inspected=tip))
        self.assertIn("changed full-tier paths: auth/policy.py", " ".join(status.validate(self.root)))


@skip_unless_slow()
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
        self.assertIn("round r1 (open): checked, 0 of 2 members merged", handoff.render(self.root))
        for task_id, said in ((TASK, f"round r1: 1 member(s) still to merge ({T2})"),  # one at a time
                              (T2, "round r1: every member is merged; close it now: pk integrate close r1")):
            code, out, err = self.pk("merge", task_id, "--round", "r1")
            self.assertEqual((code, err), (0, ""), out)
            self.assertNotIn("OK: ", out)  # no merge check ran, and none is claimed
            self.assertIn(said, out.splitlines())
        self.assertEqual(self.scans(), scans)  # the suite did not run again
        self.assertIn("round r1 (open): checked, 2 of 2 members merged", handoff.render(self.root))  # until closed
        code, out, err = self.pk("integrate", "close", "r1")
        self.assertEqual((code, err), (0, ""), out)
        tree = read_json(self.root / "program" / "rounds" / "r1.json")["tree"]
        self.assertIn(f"round r1: main matches the tested tree {tree}", out)
        self.assertFalse((self.root / ".worktrees" / "round-r1").exists())
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")
        self.assertTrue((self.root / "second.py").is_file())
        self.assertNotIn("round r1", handoff.render(self.root).split("#### Tasks by status")[0])

    def test_an_unrelated_uncommitted_file_on_main_stays_out_of_the_whole_round(self):
        """0.6.0, KF-39: integrate start and close, and each merge, leave a file none of them writes alone."""
        put(self.root, "notes.txt", "the user's own edit\n")
        self.start_and_check()
        for task_id in (TASK, T2):
            code, out, err = self.pk("merge", task_id, "--round", "r1")
            self.assertEqual((code, err), (0, ""), out)
        code, out, err = self.pk("integrate", "close", "r1")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").splitlines(), [" M notes.txt"])

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

    def test_a_member_with_work_after_its_review_does_not_join_a_round(self):
        put(self.wt2, "second.py", "VALUE = 3\n")
        late = commit(self.wt2, "a late change")
        code, _, err = self.pk("integrate", "start", "r1", TASK, T2)
        self.assertEqual(code, 1)
        self.assertIn(f"{late[:12]} was committed after the reviewed commit", err)
        self.assertIn("before it joins a round", err)
        self.assertFalse((self.root / "program" / "rounds").exists())

    def test_a_round_record_written_by_hand_covers_nothing(self):
        """L11: a record with a `tree` value typed in, and no branch, no merges and no suite run behind it."""
        main = self.head()
        members = [{"id": task_id, "branch": f"task/{task_id}", "sha": self.head(wt), "tier": "standard"}
                   for task_id, wt in ((TASK, self.wt), (T2, self.wt2))]
        write_json(self.root / "program" / "rounds" / "x.json", {
            "name": "x", "base": main, "head": main, "members": members, "checked_at": "2026-10-13T00:00:00Z",
            "tree": sh(self.root, "git", "rev-parse", "HEAD^{tree}").strip(), "merged": [], "closed_at": "",
            "rechecked": []})
        commit(self.root, "a round nobody ran")
        before, scans = self.head(), self.scans()
        code, _, err = self.pk("merge", TASK, "--round", "x")
        self.assertEqual(code, 1)
        self.assertIn("round x: branch round/x not found", err)
        self.assertIn("start a new round", err)
        self.assertEqual((self.head(), self.scans()), (before, scans))

    def test_a_tested_tree_typed_into_a_started_round_is_not_a_combined_check(self):
        self.assertEqual(self.pk("integrate", "start", "r1", TASK, T2)[0], 0)
        path = self.root / "program" / "rounds" / "r1.json"
        data = read_json(path)
        write_json(path, dict(data, tree="4b825dc642cb6eb9a060e54bf8d69288fbee4904", checked_at="2026-10-13T00:00:00Z"))
        before = self.head()
        code, _, err = self.pk("merge", TASK, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("the tested tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904 is not the tree of round/r1", err)
        write_json(path, dict(data, tree=sh(self.root, "git", "rev-parse", "round/r1^{tree}").strip(),
                              members=data["members"][:1]))  # the right tree, a member dropped from the record
        code, _, err = self.pk("merge", TASK, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("round/r1 is 2 commit(s) from its base, the record lists 1 member(s)", err)
        self.assertEqual(self.head(), before)

    def test_a_member_cannot_land_before_the_combined_check_passed(self):
        self.assertEqual(self.pk("integrate", "start", "r1", TASK, T2)[0], 0)
        code, _, err = self.pk("merge", TASK, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("has not passed its combined check", err)

    def test_a_member_that_moved_after_the_check_is_not_covered_by_it(self):
        self.start_and_check()
        put(self.wt2, f"program/evidence/{T2}/report.md", "# Report, amended\n")  # its own evidence folder: not
        commit(self.wt2, "a late change")  # late work, but the tip is no longer the commit the round tested
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


@skip_unless_slow()
class RoundCombinedFailureTests(LandBase):
    def test_a_suite_that_fails_only_on_the_combined_tree_fails_the_round_and_records_no_tested_tree(self):
        self.standard()  # this branch adds calc.mul; the other one's test passes only while there is none
        self.second_task(files={"test_second.py": (
            "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
            "    def test_it(self):\n        self.assertFalse(hasattr(calc, 'mul'), 'combined')\n")})
        self.assertEqual(self.pk("integrate", "start", "r1", TASK, T2)[0], 0)
        code, out, _ = self.pk("integrate", "check", "r1")
        self.assertEqual(code, 1)
        self.assertIn("FAILED: test", out)
        self.assertNotIn("OK: ", out)
        self.assertEqual(read_json(self.root / "program" / "rounds" / "r1.json")["tree"], "")
        code, _, err = self.pk("merge", TASK, "--round", "r1")
        self.assertEqual(code, 1)
        self.assertIn("has not passed its combined check", err)


if __name__ == "__main__":
    import unittest
    unittest.main()
