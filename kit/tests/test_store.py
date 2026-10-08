"""The evidence store: location, init, append-only records, the pointer index, history and the doctor checks."""
import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import store
from common import KitError, read_json, sha256_bytes


def sh(cwd, *argv):
    return subprocess.run(list(argv), cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def make_repo(base: Path) -> Path:
    root = base / "repo"
    root.mkdir()
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.name", "Test")
    sh(root, "git", "config", "user.email", "test@example.com")
    (root / "a.txt").write_text("a\n")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = make_repo(self.base)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(store.ENV, None)


class Location(Base):
    def test_default_is_the_sibling_of_the_main_checkout_shared_by_worktrees(self):
        self.assertEqual(store.store_dir(self.root), self.base / "repo-evidence")
        wt = self.root / ".worktrees" / "t-1"
        sh(self.root, "git", "worktree", "add", "-q", "-b", "task/T-1", str(wt))
        self.assertEqual(store.store_dir(wt), self.base / "repo-evidence")

    def test_environment_variable_wins_over_program_yaml(self):
        (self.root / "program").mkdir()
        (self.root / "program" / "program.yaml").write_text("evidence_store: ../elsewhere\n")
        self.assertEqual(store.store_dir(self.root), self.base / "elsewhere")
        os.environ[store.ENV] = str(self.base / "from-env")
        self.assertEqual(store.store_dir(self.root), self.base / "from-env")

    def test_bad_program_yaml_value_is_refused(self):
        (self.root / "program").mkdir()
        (self.root / "program" / "program.yaml").write_text("evidence_store: ''\n")
        with self.assertRaisesRegex(KitError, "evidence_store must be a non-empty path"):
            store.store_dir(self.root)


class InitAndAppend(Base):
    def test_init_writes_store_json_once(self):
        path, created = store.init(self.root)
        self.assertTrue(created)
        marker = read_json(path / "STORE.json")
        self.assertEqual((marker["schema_version"], marker["repo"]), (1, "repo"))
        self.assertTrue((path / "records").is_dir())
        self.assertEqual(store.init(self.root), (path, False))
        self.assertEqual(read_json(path / "STORE.json"), marker)

    def test_append_needs_an_initialised_store(self):
        with self.assertRaisesRegex(KitError, "run pk evidence init"):
            store.append(self.root, "T-1", "results", {"x": 1})
        self.assertFalse(store.store_dir(self.root).exists())

    def test_append_names_the_file_by_time_and_digest_and_makes_it_read_only(self):
        store.init(self.root)
        first = store.append(self.root, "T-1", "results", {"x": 1})
        raw = first["file"].read_bytes()
        self.assertEqual(first["digest"], sha256_bytes(raw))
        self.assertRegex(first["path"], r"\Arecords/T-1/results/\d{8}T\d{12}Z-" + first["digest"][:12] + r"\.json\Z")
        self.assertEqual(first["file"].stat().st_mode & 0o222, 0)
        second = store.append(self.root, "T-1", "results", {"x": 1})  # the same bytes again: a new history entry
        self.assertNotEqual(first["path"], second["path"])
        self.assertLess(Path(first["path"]).name, Path(second["path"]).name)
        self.assertEqual(len(list(first["file"].parent.iterdir())), 2)  # no temporary file is left behind

    def test_append_keeps_exact_bytes(self):
        store.init(self.root)
        raw = b'{"task_id": "T-1",   "kept": "as is"}'
        appended = store.append(self.root, "T-1", "results", raw)
        self.assertEqual(appended["file"].read_bytes(), raw)

    def test_append_never_replaces_an_existing_record(self):
        store.init(self.root)
        appended = store.append(self.root, "T-1", "results", {"x": 1})
        name = Path(appended["path"]).name
        with mock.patch.object(store, "_next_stamp", return_value=name.split("-")[0]):
            with self.assertRaisesRegex(KitError, "refusing to replace the existing record"):
                store.append(self.root, "T-1", "results", {"x": 1})
        self.assertEqual(appended["file"].read_bytes(), store.encode({"x": 1}))

    def test_bad_task_ids_and_kinds_are_refused(self):
        store.init(self.root)
        for task in ("../x", "a/b", "", ".hidden"):
            with self.assertRaises(KitError):
                store.append(self.root, task, "results", {})
        with self.assertRaisesRegex(KitError, "unknown record kind"):
            store.append(self.root, "T-1", "report", {})


class Immutability(Base):
    def test_load_refuses_a_record_whose_bytes_changed(self):
        path, _ = store.init(self.root)
        appended = store.append(self.root, "T-1", "results", {"x": 1})
        self.assertEqual(store.load(path, appended["path"], appended["digest"]), {"x": 1})
        appended["file"].chmod(0o644)
        appended["file"].write_text('{"x": 2}\n')  # the planted defect: a record edited in place
        with self.assertRaisesRegex(KitError, "immutable record altered"):
            store.load(path, appended["path"])
        with self.assertRaisesRegex(KitError, "immutable record altered"):
            store.load(path, appended["path"], appended["digest"])

    def test_load_refuses_a_digest_other_than_the_indexed_one(self):
        path, _ = store.init(self.root)
        appended = store.append(self.root, "T-1", "results", {"x": 1})
        with self.assertRaisesRegex(KitError, "not the indexed"):
            store.load(path, appended["path"], "0" * 64)

    def test_load_refuses_paths_outside_the_record_tree(self):
        path, _ = store.init(self.root)
        for rel in ("STORE.json", "records/T-1/results/../../STORE.json", "records/T-1/notes/x.json", "/etc/x"):
            with self.assertRaises(KitError):
                store.load(path, rel)


class Index(Base):
    def test_record_appends_and_points_the_index_at_the_latest(self):
        store.init(self.root)
        first = store.record(self.root, "T-1", "results", {"head": "a" * 40, "produced_at": "2026-01-01T00:00:00Z"})
        second = store.record(self.root, "T-1", "results", {"head": "b" * 40, "produced_at": "2026-01-02T00:00:00Z"})
        index = store.read_index(self.root)
        self.assertEqual(store.latest(index, "T-1", "results"),
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
        store.init(self.root)
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        before = store.read_index(self.root)["tasks"]["T-1"]
        store.record(self.root, "T-2", "results", {"head": "b" * 40})
        self.assertEqual(store.read_index(self.root)["tasks"]["T-1"], before)

    def test_index_is_written_atomically(self):
        store.init(self.root)
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        index_file = self.root / store.INDEX
        before = index_file.read_bytes()
        with mock.patch("common.os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(KitError, "cannot write"):
                store.record(self.root, "T-2", "results", {"head": "b" * 40})
        self.assertEqual(index_file.read_bytes(), before)  # the old index, whole
        self.assertEqual(sorted(p.name for p in index_file.parent.iterdir()), ["index.json"])  # no temp file left

    def test_a_malformed_index_is_refused(self):
        index_file = self.root / store.INDEX
        index_file.parent.mkdir(parents=True)
        for text in ('{"tasks": {}}', '{"schema_version": 1, "tasks": {"T-1": {"report": {}}}}',
                     '{"schema_version": 1, "tasks": {"T-1": {"results": {"digest": "x"}}}}', "{"):
            index_file.write_text(text)
            with self.assertRaises(KitError):
                store.read_index(self.root)

    def test_an_entry_whose_path_does_not_carry_its_digest_is_refused(self):
        store.init(self.root)
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        index = read_json(self.root / store.INDEX)
        index["tasks"]["T-1"]["results"]["digest"] = "f" * 64  # the planted defect: a hand-edited pointer
        (self.root / store.INDEX).write_text(json.dumps(index))
        with self.assertRaisesRegex(KitError, "path does not name the record of its digest"):
            store.read_index(self.root)
        self.assertTrue(appended["file"].is_file())


class History(Base):
    def test_history_lists_every_record_newest_first_and_marks_the_current_ones(self):
        store.init(self.root)
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
        store.init(self.root)
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        appended["file"].chmod(0o644)
        appended["file"].write_text("{}\n")
        self.assertIn("immutable record altered", store.history(self.root, "T-1")[0]["altered"])


class Doctor(Base):
    def test_no_store_and_no_entries_is_fine(self):
        self.assertEqual(store.problems(self.root), [])

    def test_entries_without_a_store_are_reported(self):
        store.init(self.root)
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        os.environ[store.ENV] = str(self.base / "nowhere")
        self.assertRegex(store.problems(self.root)[0], r"\Aevidence store missing at .*nowhere: 1 index entries")

    def test_every_entry_must_resolve_to_its_record(self):
        store.init(self.root)
        appended = store.record(self.root, "T-1", "results", {"head": "a" * 40})
        self.assertEqual(store.problems(self.root), [])
        appended["file"].chmod(0o644)
        appended["file"].write_text("{}\n")
        self.assertEqual(len(store.problems(self.root)), 1)
        self.assertIn("immutable record altered", store.problems(self.root)[0])
        appended["file"].unlink()
        self.assertIn("is missing from the store", store.problems(self.root)[0])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anywhere")
    def test_a_read_only_store_is_reported(self):
        path, _ = store.init(self.root)
        store.record(self.root, "T-1", "results", {"head": "a" * 40})
        (path / "records").chmod(0o555)
        self.addCleanup((path / "records").chmod, 0o755)
        self.assertIn(f"evidence store {path} is not writable", store.problems(self.root))


if __name__ == "__main__":
    unittest.main()
