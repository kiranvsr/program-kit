"""The conflicts the kit resolves itself (0.6.0): `pk merge check` before QA and `merge.union_files` (KF-38).

Each case builds a small repository: main and a task branch that both change a file after the fork, then asks what
the merge makes of them, through `pk merge check`, the trial merge the gates use, and the late-work rule.
"""
import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import contextlib
import io
import json
import unittest
from unittest import mock

import conflicts
import gate
import land
import latework
import pk
from common import KitError, write_json, write_yaml
from test_gate import GitBase, commit, put, sh

TASK = "P0-M01-S01-T01"
BRANCH = f"task/{TASK}"
JOURNAL = "db/migrations/journal.txt"
INDEX = "db/migrations/index.json"


class ConflictBase(GitBase):
    def setUp(self):
        super().setUp()
        put(self.root, ".gitignore", ".worktrees/\n")
        put(self.root, JOURNAL, "0001 init\n")
        put(self.root, INDEX, '{"entries": [\n  "0001"\n]}\n')
        put(self.root, "notes.txt", "one\n")
        write_json(self.root / "program" / "status" / f"{TASK}.json", {"task_id": TASK, "status": "in_progress"})
        self.checks()
        commit(self.root, "init")
        self.wt = self.root / ".worktrees" / TASK.lower()
        sh(self.root, "git", "worktree", "add", "-q", "-b", BRANCH, str(self.wt))
        self.addCleanup(lambda: sh(self.root, "git", "worktree", "prune"))

    def checks(self, union=None):
        data = {"build": [["true"]], "test": [["true"]], "scan": [["true"]]}
        if union is not None:
            data["merge"] = {"union_files": union}
        write_yaml(self.root / "program" / "checks.yaml", data)

    def both_add_to_the_journal(self):
        put(self.wt, JOURNAL, "0001 init\n0002 the branch's table\n")
        commit(self.wt, "the branch's migration")
        put(self.root, JOURNAL, "0001 init\n0002 main's table\n")
        commit(self.root, "main's migration")

    def pk(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(land, "find_root", return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def main_sha(self):
        return sh(self.root, "git", "rev-parse", "main").strip()


class MergeCheckCommandTests(ConflictBase):
    def test_pk_merge_check_names_the_conflicting_files_and_exits_1(self):
        put(self.wt, "notes.txt", "the branch's\n")
        commit(self.wt, "notes")
        put(self.root, "notes.txt", "main's\n")
        commit(self.root, "notes on main")
        self.both_add_to_the_journal()
        code, out, err = self.pk("merge", "check", TASK)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out, f"FAILED: merge {BRANCH} into main at {self.main_sha()} conflicts in: {JOURNAL}, "
                              "notes.txt\n")

    def test_pk_merge_check_passes_a_clean_merge_and_prints_the_resolved_lines(self):
        code, out, err = self.pk("merge", "check", TASK)
        self.assertEqual((code, out, err), (0, f"OK: {BRANCH} merges into main at {self.main_sha()} without conflicts\n",
                                            ""))
        status = f"program/status/{TASK}.json"
        write_json(self.wt / status, {"task_id": TASK, "status": "done", "role": "qa"})
        commit(self.wt, "QA's record")
        write_json(self.root / status, {"task_id": TASK, "status": "blocked", "role": "controller"})
        commit(self.root, "main's record")
        code, out, err = self.pk("merge", "check", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [f"resolved: {status}: took {BRANCH}'s record (the task's own status file)",
                                            f"OK: {BRANCH} merges into main at {self.main_sha()} without conflicts"])
        self.assertEqual(sh(self.root, "git", "status", "--porcelain").strip(), "")  # nothing was written

    def test_pk_merge_with_one_id_is_unchanged_and_two_ids_are_still_refused(self):
        with mock.patch.object(land, "merge", return_value=["merged"]) as merged:
            self.assertEqual(self.pk("merge", TASK), (0, "merged\n", ""))
        self.assertEqual(merged.call_args.args[1], TASK)
        code, _, err = self.pk("merge", TASK, "P0-M01-S01-T02")
        self.assertEqual(code, 1)
        self.assertIn("one task at a time", err)
        code, _, err = self.pk("merge", "check")
        self.assertEqual(code, 1)
        self.assertIn("pk merge check <ID>", err)
        code, _, err = self.pk("merge", "check", TASK, "--inspected", "abc")
        self.assertEqual(code, 1)
        self.assertIn("pk merge check takes only the ID", err)


class UnionFileTests(ConflictBase):
    def test_a_conflict_in_a_union_file_merges_as_the_union_of_both_sides(self):
        self.checks(union=["db/migrations/*.txt"])
        commit(self.root, "union files")
        sh(self.wt, "git", "merge", "-q", "main")
        self.both_add_to_the_journal()
        code, out, err = self.pk("merge", "check", TASK)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[0],
                         f"resolved: {JOURNAL}: merged as the line union of both sides (merge.union_files)")
        said = []
        trial = gate.trial_merge(self.root, BRANCH, resolved=said)  # the gates' trial merge resolves it the same way
        try:
            self.assertEqual((trial / JOURNAL).read_text(), "0001 init\n0002 main's table\n0002 the branch's table\n")
            self.assertEqual(sh(trial, "git", "diff", "--name-only", "--diff-filter=U").strip(), "")
        finally:
            sh(self.root, "git", "worktree", "remove", "--force", str(trial))
        self.assertEqual(said, [out.splitlines()[0]])

    def test_union_files_are_off_by_default(self):
        self.both_add_to_the_journal()
        code, out, _ = self.pk("merge", "check", TASK)
        self.assertEqual(code, 1)
        self.assertIn(f"conflicts in: {JOURNAL}", out)
        with self.assertRaisesRegex(KitError, f"conflicts in: {JOURNAL}"):
            gate.trial_merge(self.root, BRANCH)

    def test_a_json_file_whose_union_does_not_parse_stays_a_conflict_and_says_why(self):
        self.checks(union=["db/migrations/"])
        commit(self.root, "union files")
        sh(self.wt, "git", "merge", "-q", "main")
        put(self.wt, INDEX, '{"entries": [\n  "0001",\n  "0002-branch"\n]}\n')
        commit(self.wt, "the branch's entry")
        put(self.root, INDEX, '{"entries": [\n  "0001",\n  "0002-main"\n]}\n')
        commit(self.root, "main's entry")
        code, out, _ = self.pk("merge", "check", TASK)
        self.assertEqual(code, 1)
        self.assertIn(f"conflicts in: {INDEX} (merge.union_files: the line union of both sides is not valid JSON)", out)
        with self.assertRaisesRegex(KitError, "is not valid JSON"):
            gate.trial_merge(self.root, BRANCH)



class LateUnionMergeTests(ConflictBase):
    """A merge of main into the branch after the reviewed commit, its union file resolved as the kit resolves it."""

    def reviewed_then_merge_main(self, resolve):
        self.checks(union=[JOURNAL])
        commit(self.root, "union files")
        sh(self.wt, "git", "merge", "-q", "main")
        self.both_add_to_the_journal()
        head = sh(self.wt, "git", "rev-parse", "HEAD").strip()
        with self.assertRaises(Exception):
            sh(self.wt, "git", "merge", "-q", "main")  # the journal conflicts
        put(self.wt, JOURNAL, resolve)
        sh(self.wt, "git", "add", JOURNAL)
        sh(self.wt, "git", "commit", "-q", "--no-edit")
        return head

    def test_a_merge_of_main_whose_union_file_is_the_line_union_is_not_late_work_while_union_files_say_so(self):
        head = self.reviewed_then_merge_main("0001 init\n0002 the branch's table\n0002 main's table\n")
        self.assertIsNone(latework.late_work(self.wt, TASK, head))
        self.checks()
        commit(self.root, "union files off again")
        self.assertIn(f"changes {JOURNAL}", latework.late_work(self.wt, TASK, head))

    def test_a_union_file_resolved_any_other_way_is_late_work(self):
        head = self.reviewed_then_merge_main("0001 init\n0002 the branch's table\n0003 something else\n")
        self.assertIn(f"is a merge that is not what git makes of its two parents; it was made after the reviewed "
                      f"commit {head[:12]} and changes {JOURNAL}", latework.late_work(self.wt, TASK, head))


class StageParsingTests(unittest.TestCase):
    def test_a_union_entry_follows_the_path_rules_of_the_other_lists(self):
        self.assertEqual(gate._parse_merge({"union_files": ["./db/migrations/"]}), {"union_files": ["db/migrations/"]})
        for bad in ({"union_files": "db/"}, {"union_files": [""]}, {"union": []}, ["db/"]):
            with self.assertRaises(KitError):
                gate._parse_merge(bad)
        self.assertEqual(gate._parse_merge(None), {"union_files": []})

    def test_stage_entries_of_ls_files_and_merge_tree_read_alike(self):
        fields = ["100644 " + "a" * 40 + " 1\tdb/j.txt", "100644 " + "b" * 40 + " 2\tdb/j.txt",
                  "100644 " + "c" * 40 + " 3\tdb/j.txt", "", "garbage"]
        self.assertEqual(conflicts.parse_stages(fields), {"db/j.txt": {1: ("100644", "a" * 40),
                                                                      2: ("100644", "b" * 40),
                                                                      3: ("100644", "c" * 40)}})
        self.assertEqual(json.loads(json.dumps(conflicts.task_of(BRANCH))), TASK)
        self.assertIsNone(conflicts.task_of("feature/x"))


if __name__ == "__main__":
    unittest.main()
