"""The evidence store: committed append-only records, the pointer index, history and the integrity checks."""
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
    def test_init_creates_only_the_index_once(self):
        path, created = store.init(self.root)
        self.assertEqual((path, created), (self.root / store.INDEX, True))
        self.assertEqual(read_json(path), {"schema_version": 1, "tasks": {}})
        self.assertEqual(store.init(self.root), (path, False))
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["index.json"])

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
        with self.assertRaisesRegex(KitError, "not the indexed"):
            store.load(self.root, appended["path"], "0" * 64)

    def test_load_refuses_paths_outside_the_record_tree(self):
        for rel in ("program/evidence/index.json", "program/evidence/T-1/results/../../index.json",
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

    def test_an_append_changes_only_its_own_task_entry(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        before = store.read_index(self.root)["tasks"]["T-1"]
        store.record(self.root, "T-2", "results", {"head": "b" * 40})
        self.assertEqual(store.read_index(self.root)["tasks"]["T-1"], before)

    def test_index_is_written_atomically(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        index_file = self.root / store.INDEX
        before = index_file.read_bytes()
        with mock.patch("common.os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(KitError, "cannot write"):
                store.record(self.root, "T-2", "results", {"head": "b" * 40})
        self.assertEqual(index_file.read_bytes(), before)  # the old index, whole
        self.assertEqual([p.name for p in index_file.parent.iterdir() if p.is_file()], ["index.json"])

    def test_a_malformed_index_is_refused(self):
        index_file = self.root / store.INDEX
        index_file.parent.mkdir(parents=True)
        for text in ('{"tasks": {}}', '{"schema_version": 1, "tasks": {"T-1": {"report": {}}}}',
                     '{"schema_version": 1, "tasks": {"T-1": {"results": {"digest": "x"}}}}', "{"):
            index_file.write_text(text)
            with self.assertRaises(KitError):
                store.read_index(self.root)

    def test_an_entry_whose_path_does_not_carry_its_digest_is_refused(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        index = read_json(self.root / store.INDEX)
        index["tasks"]["T-1"]["results"]["digest"] = "f" * 64  # the planted defect: a hand-edited pointer
        (self.root / store.INDEX).write_text(json.dumps(index))
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
    def test_committed_records_with_matching_names_and_index_are_intact(self):
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

    def test_the_index_and_other_files_may_change_freely(self):
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        (self.root / "program" / "evidence" / "T-1" / "produce.py").write_text("# v1\n")
        base = self.commit("evidence")
        store.record(self.root, "T-1", "results", {"head": "b" * 40})
        (self.root / "program" / "evidence" / "T-1" / "produce.py").write_text("# v2\n")
        self.commit("refresh")
        self.assertEqual(store.rewritten(self.root, base), [])

    def test_every_index_entry_must_resolve_to_a_committed_record(self):
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.assertEqual(store.integrity(self.root), [f"{store.INDEX}: T-1 results: {appended['path']} is not "
                                                      "committed"])
        self.assertEqual(store.integrity(self.root, uncommitted={appended["path"]}), [])
        self.commit("evidence")
        appended["file"].chmod(0o644)
        appended["file"].unlink()  # the planted defect: the record the index points at is gone
        problems = store.integrity(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("is missing", problems[0])

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


if __name__ == "__main__":
    unittest.main()
