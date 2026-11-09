"""The evidence store: committed append-only records, the per-task pointers, history and the integrity checks."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pk
import store
from common import KitError, read_json, sha256_bytes


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        sh(self.root, "git", "init", "-q", "-b", "main")
        sh(self.root, "git", "config", "user.name", "Test")
        sh(self.root, "git", "config", "user.email", "test@example.com")
        (self.root / "a.txt").write_text("a\n")
        self.commit("init")

    def commit(self, message="change"):
        sh(self.root, "git", "add", "-A")
        sh(self.root, "git", "commit", "-q", "-m", message)
        return sh(self.root, "git", "rev-parse", "HEAD").strip()

    def edit(self, appended, text):
        """The planted defect: a record file edited in place."""
        appended["file"].chmod(0o644)
        appended["file"].write_text(text)


class InitAndAppend(Base):
    def test_a_pointer_appears_with_the_first_record_of_its_task(self):
        self.assertEqual(store.read_index(self.root), {"schema_version": 1, "tasks": {}})
        self.assertFalse((self.root / store.BASE).exists())
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40, "produced_at": "t"})
        self.assertEqual(store.pointer_path("T-1"), "program/evidence/T-1/latest.json")
        self.assertEqual(read_json(self.root / store.pointer_path("T-1")),
                         {"schema_version": 1, "task_id": "T-1", "results": {
                             "digest": appended["digest"], "head": "a" * 40, "path": appended["path"],
                             "produced_at": "t"}})

    def test_append_puts_the_record_in_the_task_directory_named_by_time_and_digest(self):
        first = store.append(self.root, "T-1", "results", {"x": 1})
        raw = first["file"].read_bytes()
        self.assertEqual(first["digest"], sha256_bytes(raw))
        self.assertRegex(first["path"], r"\Aprogram/evidence/T-1/results/\d{8}T\d{12}Z-" + first["digest"][:12]
                         + r"\.json\Z")
        self.assertEqual(first["file"], self.root / first["path"])
        self.assertEqual(first["file"].stat().st_mode & 0o222, 0)
        second = store.append(self.root, "T-1", "results", {"x": 1})  # the same bytes again: a new history entry
        self.assertLess(Path(first["path"]).name, Path(second["path"]).name)
        self.assertEqual(len(list(first["file"].parent.iterdir())), 2)  # no temporary file is left behind

    def test_append_keeps_exact_bytes(self):
        raw = b'{"task_id": "T-1",   "kept": "as is"}'
        self.assertEqual(store.append(self.root, "T-1", "results", raw)["file"].read_bytes(), raw)

    def test_append_never_replaces_an_existing_record(self):
        appended = store.append(self.root, "T-1", "results", {"x": 1})
        stamp = Path(appended["path"]).name.split("-")[0]
        with mock.patch.object(store, "_next_stamp", return_value=stamp):
            with self.assertRaisesRegex(KitError, "refusing to replace the existing record"):
                store.append(self.root, "T-1", "results", {"x": 1})
        self.assertEqual(appended["file"].read_bytes(), store.encode({"x": 1}))

    def test_bad_task_ids_and_kinds_are_refused(self):
        for task in ("../x", "a/b", "", ".hidden"):
            with self.assertRaises(KitError):
                store.append(self.root, task, "results", {})
        with self.assertRaisesRegex(KitError, "unknown record kind"):
            store.append(self.root, "T-1", "report", {})


class Load(Base):
    def test_load_refuses_a_record_whose_bytes_changed(self):
        appended = store.append(self.root, "T-1", "results", {"x": 1})
        self.assertEqual(store.load(self.root, appended["path"], appended["digest"]), {"x": 1})
        self.edit(appended, '{"x": 2}\n')
        with self.assertRaisesRegex(KitError, "immutable record altered"):
            store.load(self.root, appended["path"])

    def test_load_refuses_a_digest_other_than_the_indexed_one(self):
        appended = store.append(self.root, "T-1", "results", {"x": 1})
        with self.assertRaisesRegex(KitError, "not the pointed-at"):
            store.load(self.root, appended["path"], "0" * 64)

    def test_load_refuses_paths_outside_the_record_tree(self):
        for rel in ("program/evidence/T-1/latest.json", "program/evidence/T-1/results/../latest.json",
                    "program/evidence/T-1/notes/x.json", "/etc/x", "program/evidence/T-1/results.json"):
            with self.assertRaises(KitError):
                store.load(self.root, rel)


class Index(Base):
    def test_record_appends_and_points_the_index_at_the_latest(self):
        first = store.record(self.root, "T-1", "results", {"head": "a" * 40, "produced_at": "2026-01-01T00:00:00Z"})
        second = store.record(self.root, "T-1", "results", {"head": "b" * 40, "produced_at": "2026-01-02T00:00:00Z"})
        self.assertEqual(store.latest(store.read_index(self.root), "T-1", "results"),
                         {"digest": second["digest"], "path": second["path"], "head": "b" * 40,
                          "produced_at": "2026-01-02T00:00:00Z"})
        self.assertTrue(first["file"].is_file())  # history stays
        accepted = store.record(self.root, "T-1", "acceptance", {"decision": "accepted_development",
                                                                 "accepted_at": "2026-01-03T00:00:00Z",
                                                                 "evidence_sha256": second["digest"]})
        self.assertEqual(store.latest(store.read_index(self.root), "T-1", "acceptance")["results_digest"],
                         second["digest"])
        self.assertEqual(store.load_latest(self.root, "T-1", "acceptance")[0]["digest"], accepted["digest"])

    def test_an_append_writes_only_its_own_task_pointer(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        mine = self.root / store.pointer_path("T-1")
        before = mine.read_bytes()
        self.commit("T-1 evidence")
        store.record(self.root, "T-2", "results", {"head": "b" * 40})
        store.record(self.root, "T-2", "acceptance", {"decision": "rejected"})
        self.assertEqual(mine.read_bytes(), before)
        changed = sh(self.root, "git", "status", "--porcelain", "--untracked-files=all").split()
        self.assertEqual(sorted(p for p in changed if p != "??"),
                         sorted(str(p.relative_to(self.root)) for p in (self.root / "program/evidence/T-2").rglob("*")
                                if p.is_file()))  # nothing outside T-2's own directory changed

    def test_two_task_branches_merge_without_an_evidence_conflict(self):
        for task in ("T-1", "T-2"):  # neighbours in sort order, each on its own branch
            sh(self.root, "git", "checkout", "-q", "-b", f"task/{task}", "main")
            store.record(self.root, task, "results", {"head": "a" * 40})
            store.record(self.root, task, "acceptance", {"decision": "accepted_development"})
            self.commit(f"{task} evidence")
        sh(self.root, "git", "checkout", "-q", "main")
        sh(self.root, "git", "merge", "-q", "--no-ff", "-m", "merge T-1", "task/T-1")
        sh(self.root, "git", "merge", "-q", "--no-ff", "-m", "merge T-2", "task/T-2")  # check=True: no conflict
        self.assertEqual(sorted(store.read_index(self.root)["tasks"]), ["T-1", "T-2"])
        self.assertEqual(store.integrity(self.root), [])

    def test_a_pointer_is_written_atomically(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        pointer = self.root / store.pointer_path("T-1")
        before = pointer.read_bytes()
        with mock.patch("common.os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(KitError, "cannot write"):
                store.record(self.root, "T-1", "results", {"head": "b" * 40})
        self.assertEqual(pointer.read_bytes(), before)  # the old pointer, whole
        self.assertEqual([p.name for p in pointer.parent.iterdir() if p.is_file()], ["latest.json"])

    def test_a_malformed_pointer_is_refused(self):
        pointer = self.root / store.pointer_path("T-1")
        pointer.parent.mkdir(parents=True)
        for text in ('{"schema_version": 1, "task_id": "T-1"}', '{"schema_version": 1, "task_id": "T-2", "results": {}}',
                     '{"schema_version": 1, "task_id": "T-1", "report": {}}',
                     '{"schema_version": 1, "task_id": "T-1", "results": {"digest": "x"}}', "{"):
            pointer.write_text(text)
            with self.assertRaises(KitError):
                store.read_index(self.root)

    def test_an_entry_whose_path_does_not_carry_its_digest_is_refused(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        pointer = self.root / store.pointer_path("T-1")
        data = read_json(pointer)
        data["results"]["digest"] = "f" * 64  # the planted defect: a hand-edited pointer
        pointer.write_text(json.dumps(data))
        with self.assertRaisesRegex(KitError, "path does not name the record of its digest"):
            store.read_index(self.root)


class History(Base):
    def test_history_lists_every_record_newest_first_and_marks_the_current_ones(self):
        r1 = store.record(self.root, "T-1", "results", {"head": "a" * 40, "produced_at": "t1"})
        a1 = store.record(self.root, "T-1", "acceptance", {"decision": "rejected", "accepted_at": "t2",
                                                           "source_commit": "a" * 40, "evidence_sha256": r1["digest"]})
        r2 = store.record(self.root, "T-1", "results", {"head": "b" * 40, "produced_at": "t3"})
        rows = store.history(self.root, "T-1")
        self.assertEqual([row["digest"] for row in rows], [r2["digest"], a1["digest"], r1["digest"]])
        self.assertEqual([row["current"] for row in rows], [True, True, False])
        self.assertEqual((rows[1]["kind"], rows[1]["decision"], rows[1]["head"]), ("acceptance", "rejected", "a" * 40))
        self.assertEqual([row["altered"] for row in rows], [None, None, None])

    def test_history_names_an_altered_record(self):
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.edit(appended, "{}\n")
        self.assertIn("immutable record altered", store.history(self.root, "T-1")[0]["altered"])


class Integrity(Base):
    def test_committed_records_with_matching_names_and_pointers_are_intact(self):
        self.assertEqual(store.integrity(self.root), [])
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.commit("evidence")
        self.assertEqual(store.integrity(self.root), [])

    def test_a_record_whose_bytes_changed_is_named(self):
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.commit("evidence")
        self.edit(appended, '{"head": "b"}\n')
        problems = store.integrity(self.root)
        self.assertTrue(problems[0].startswith(f"immutable record altered: {appended['path']} "), problems)

    def test_a_commit_that_modifies_or_deletes_a_record_is_named(self):
        kept = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        gone = store.append(self.root, "T-2", "results", {"head": "b" * 40})
        base = self.commit("evidence")
        self.edit(kept, '{"head": "a"}\n')
        modified = self.commit("rewrite a record")
        sh(self.root, "git", "rm", "-q", gone["path"])
        deleted = self.commit("delete a record")
        self.edit(kept, store.encode({"head": "a" * 40}).decode())
        restored = self.commit("put the bytes back")
        problems = store.rewritten(self.root, base)
        self.assertIn(f"evidence history rewritten: {kept['path']} modified in {modified}", problems)
        self.assertIn(f"evidence history rewritten: {gone['path']} deleted in {deleted}", problems)
        self.assertEqual(store.rewritten(self.root, deleted),
                         [f"evidence history rewritten: {kept['path']} modified in {restored}"])
        self.assertEqual(len([p for p in store.integrity(self.root) if p.startswith("evidence history rewritten")]),
                         3)

    def test_the_pointer_and_other_files_may_change_freely(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        (self.root / "program" / "evidence" / "T-1" / "produce.py").write_text("# v1\n")
        base = self.commit("evidence")
        store.record(self.root, "T-1", "results", {"head": "b" * 40})
        (self.root / "program" / "evidence" / "T-1" / "produce.py").write_text("# v2\n")
        self.commit("refresh")
        self.assertEqual(store.rewritten(self.root, base), [])

    def test_every_pointer_entry_must_resolve_to_a_committed_record(self):
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.assertEqual(store.integrity(self.root), [f"{appended['path']}: not committed (pointed at by "
                                                      f"{store.pointer_path('T-1')})"])
        self.assertEqual(store.check(self.root), ([], [(appended["path"], store.pointer_path("T-1"))]))
        self.assertEqual(store.integrity(self.root, uncommitted={appended["path"]}), [])
        self.commit("evidence")
        appended["file"].chmod(0o644)
        appended["file"].unlink()  # the planted defect: the record the pointer names is gone
        problems = store.integrity(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("is missing", problems[0])

    def test_a_pointer_whose_full_digest_differs_from_its_record_is_named(self):
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.commit("evidence")
        pointer = self.root / store.pointer_path("T-1")
        data = read_json(pointer)
        data["results"]["digest"] = appended["digest"][:12] + "0" * 52  # the planted defect: same name prefix
        pointer.write_text(json.dumps(data))
        self.assertEqual(store.integrity(self.root),
                         [f"{store.pointer_path('T-1')}: results: immutable record altered: {appended['path']} hashes "
                          f"to {appended['digest']}, not the pointed-at {data['results']['digest']}"])

    def merge(self, branch, *extra):
        sh(self.root, "git", "merge", "-q", "--no-ff", *extra, "-m", f"merge {branch}", branch)

    def test_a_record_deleted_inside_a_merge_commit_is_named(self):
        kept = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.commit("T-1 evidence on main")
        sh(self.root, "git", "checkout", "-q", "-b", "task/T-2")
        store.record(self.root, "T-2", "results", {"head": "b" * 40})
        self.commit("T-2 evidence")
        sh(self.root, "git", "checkout", "-q", "main")
        self.merge("task/T-2", "--no-commit")
        kept["file"].chmod(0o644)
        sh(self.root, "git", "rm", "-q", kept["path"])  # the planted defect: the merge drops a record,
        store.record(self.root, "T-1", "results", {"head": "a" * 40, "forged": True})  # adds a re-digested one
        merge = self.commit("merge task/T-2")  # and repoints latest.json, all inside the merge commit
        self.assertEqual(len(sh(self.root, "git", "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        problems = store.integrity(self.root)
        self.assertEqual(problems, [f"evidence history rewritten: {kept['path']} deleted in {merge}"])

    def test_a_normal_workflow_with_merges_and_a_refresh_reports_nothing(self):
        for task in ("T-1", "T-2"):
            sh(self.root, "git", "checkout", "-q", "-b", f"task/{task}", "main")
            store.record(self.root, task, "results", {"head": "a" * 40})
            self.commit(f"{task} evidence")
        (self.root / "a.txt").write_text("main moves\n")
        sh(self.root, "git", "checkout", "-q", "main")
        self.commit("main moves")
        sh(self.root, "git", "checkout", "-q", "task/T-1")
        self.merge("main")  # main merged into the branch, twice
        store.record(self.root, "T-1", "results", {"head": "b" * 40})  # a refresh
        store.record(self.root, "T-1", "acceptance", {"decision": "accepted_development"})
        self.commit("T-1 refresh and acceptance")
        (self.root / "b.txt").write_text("more\n")
        sh(self.root, "git", "checkout", "-q", "main")
        self.commit("main moves again")
        sh(self.root, "git", "checkout", "-q", "task/T-1")
        self.merge("main")
        sh(self.root, "git", "checkout", "-q", "main")
        self.merge("task/T-1")
        self.merge("task/T-2")
        self.assertEqual(store.integrity(self.root), [])
        self.assertEqual(store.rewritten(self.root), [])

    def test_a_record_that_is_only_staged_is_pending_not_committed(self):
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        sh(self.root, "git", "add", "-A")  # staged, not committed
        self.assertEqual(store.check(self.root), ([], [(appended["path"], store.pointer_path("T-1"))]))

    def test_a_pointer_rolled_back_to_an_older_record_is_named(self):
        old = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        old_entry = store.read_pointer(self.root, "T-1")["results"]
        new = store.record(self.root, "T-1", "results", {"head": "b" * 40})
        self.commit("two records")
        store.set_entry(self.root, "T-1", "results", old_entry)  # the planted defect: "latest" rolled back
        self.commit("roll the pointer back")
        self.assertEqual(store.integrity(self.root),
                         [f"{store.pointer_path('T-1')}: results: names {old['path']}, not the newest results record "
                          f"{new['path']}"])

    def test_a_misnamed_file_in_a_record_directory_is_named(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        (self.root / "program" / "evidence" / "T-1" / "results" / "extra.json").write_text("{}")
        self.commit("evidence")
        self.assertIn("program/evidence/T-1/results/extra.json: not a record name (<utc>-<sha256[:12]>.json)",
                      store.integrity(self.root))

    def test_doctor_runs_the_integrity_checks(self):
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.commit("evidence")
        (self.root / "program" / "checks.yaml").write_text(
            "build:\n  - - 'true'\ntest:\n  - - 'true'\nscan:\n  - - 'true'\n")
        self.edit(appended, "{}\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), contextlib.chdir(self.root):
            code = pk.main(["doctor"])
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertIn(f"error: immutable record altered: {appended['path']} ", err.getvalue())


    def test_doctor_warns_about_a_record_pending_commit_and_passes(self):
        (self.root / "program" / "checks.yaml").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "program" / "checks.yaml").write_text(
            "build:\n  - - 'true'\ntest:\n  - - 'true'\nscan:\n  - - 'true'\n")
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})  # gated, not yet committed
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), contextlib.chdir(self.root):
            code = pk.main(["doctor"])
        self.assertEqual((code, out.getvalue(), err.getvalue()),
                         (0, "doctor: ok\n", f"warning: record pending commit: {appended['path']}\n"))
        appended["file"].chmod(0o644)
        appended["file"].write_text("{}\n")  # pending and altered: a failure, not a warning
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err2, \
                contextlib.chdir(self.root):
            self.assertEqual(pk.main(["doctor"]), 1)
        self.assertIn("immutable record altered", err2.getvalue())

EMPTY = sha256_bytes(b"")


def valid_results(task_id="T-1", head="a" * 40):
    """A results record the kit's schema accepts, as a producer writes one (KF-36 tests need records that validate)."""
    row = {"builds": True, "killed": True, "kill_reason": "test failed", "test_output_sha256": EMPTY,
           "path": "a.txt", "find": "a", "replace": "b", "build_argv": ["true"], "test_argv": ["false"]}
    return {"task_id": task_id, "head": head, "main": head, "merge_base": head, "produced_at": "2026-01-01T00:00:00Z",
            "inputs": [{"path": "a.txt", "sha256": EMPTY}],
            "commands": [{"label": "unit", "argv": ["true"], "exit": 0, "expected_exit": 0, "as_expected": True,
                          "derived": {}, "output_sha256": EMPTY, "output_tail": ""}],
            "planted_bugs": [dict(row, id="made-up"),
                             dict(row, id="self-test-noncompiling", builds=False, killed=False,
                                  kill_reason="build failed", test_output_sha256="")],
            "planted_bugs_all_killed": True, "all_commands_as_expected": True, "tree_unchanged_by_run": True,
            "blocked_by": [], "deviations": []}


class CheckTree(Base):
    """KF-36 (0.6.0): run outputs written into the kit's record folders are refused before they are committed."""

    def setUp(self):
        super().setUp()
        self.base = sh(self.root, "git", "rev-parse", "HEAD").strip()
        sh(self.root, "git", "checkout", "-q", "-b", "task/T-1")
        self.results = self.root / store.BASE / "T-1" / "results"

    def problems(self, **kwargs):
        return store.tree_problems(self.root, **kwargs)

    def test_records_the_kit_wrote_pass_committed_and_uncommitted(self):
        store.record(self.root, "T-1", "results", valid_results())
        self.assertEqual(self.problems(), [])  # the gate's new record and pointer, not committed yet
        self.commit("evidence")
        store.record(self.root, "T-1", "results", valid_results(head="b" * 40))
        self.commit("a second record")
        self.assertEqual(self.problems(), [])

    def test_a_run_output_in_results_is_refused_before_and_after_its_commit(self):
        self.results.mkdir(parents=True)
        (self.results / "run.log").write_text("output\n")
        rel = f"{store.BASE}/T-1/results/run.log"
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"{rel}: not a kit record (not a record name"), found)
        self.assertIn("in the uncommitted state", found[0])
        self.assertIn("outside the repository", found[0])
        sha = self.commit("run output")
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn(f"{rel}: not a kit record", found[0])
        self.assertIn(f"added in {sha}", found[0])
        self.assertIn(f"rebuild the branch from {self.base[:12]}", found[0])

    def test_a_file_in_a_subdirectory_or_a_digest_named_non_record_is_refused(self):
        raw = b'{"hello": "world"}\n'
        name = f"20000101T000000000000Z-{sha256_bytes(raw)[:12]}.json"
        (self.results / "sub").mkdir(parents=True)
        (self.results / "sub" / name).write_bytes(raw)
        (self.results / name).write_bytes(raw)
        (self.results / "20000101T000000000001Z-000000000000.json").write_bytes(raw)
        found = "\n".join(self.problems())
        self.assertIn(f"results/sub/{name}: not a kit record (in a subdirectory of results/", found)
        self.assertIn(f"results/{name}: not a kit record (not a valid results record: missing key", found)
        self.assertIn("results/20000101T000000000001Z-000000000000.json: not a kit record (its bytes hash to", found)

    def test_a_record_modified_or_deleted_is_refused_with_its_commit(self):
        appended = store.record(self.root, "T-1", "results", valid_results())
        self.commit("evidence")
        appended["file"].chmod(0o644)
        appended["file"].write_text("{}\n")
        self.assertEqual(self.problems(), [f"{appended['path']}: a committed record modified in the uncommitted "
                                           f"state: restore it with git checkout -- {appended['path']}"])
        sha = self.commit("rewrite")
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"evidence history rewritten: {appended['path']} modified in {sha}"))
        self.assertIn("the kit never changes a record", found[0])

    def test_a_changed_pointer_that_is_not_a_pointer_is_refused(self):
        store.record(self.root, "T-1", "results", valid_results())
        self.commit("evidence")
        pointer = self.root / store.pointer_path("T-1")
        pointer.write_text('{"results": "mine"}\n')
        found = self.problems()
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"{store.pointer_path('T-1')}: changed in the uncommitted state and "
                                            "does not read as a pointer"), found)
        sha = self.commit("hand-edited pointer")
        self.assertIn(f"changed in {sha} and does not read as a pointer", self.problems()[0])

    def test_a_task_id_limits_the_check_to_that_task(self):
        other = self.root / store.BASE / "T-2" / "results"
        other.mkdir(parents=True)
        (other / "run.log").write_text("x\n")
        self.assertEqual(self.problems(task_id="T-1"), [])
        self.assertEqual(len(self.problems()), 1)

    def test_the_check_and_the_reader_see_a_file_in_a_subdirectory(self):
        # the reader globbed direct children only: results/sub/ was invisible to pk evidence validate (a bug)
        raw = store.encode(valid_results())
        (self.results / "sub").mkdir(parents=True)
        (self.results / "sub" / f"20000101T000000000000Z-{sha256_bytes(raw)[:12]}.json").write_bytes(raw)
        problems, _ = store.check(self.root, schema=True)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("in a subdirectory of results/", problems[0])

    def test_the_command_prints_one_line_when_ok_and_one_per_problem(self):
        store.record(self.root, "T-1", "results", valid_results())
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("evidence.find_root", return_value=self.root), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = pk.main(["evidence", "check-tree", "T-1"])
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(out.getvalue(), f"evidence tree of T-1 ok: 0 commit(s) since {self.base[:12]} and the "
                                         "uncommitted state\n")
        (self.results / "run.log").write_text("x\n")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("evidence.find_root", return_value=self.root), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = pk.main(["evidence", "check-tree", "--base", "main"])
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
        self.assertTrue(err.getvalue().startswith(f"error: {store.BASE}/T-1/results/run.log: not a kit record"))


if __name__ == "__main__":
    unittest.main()
