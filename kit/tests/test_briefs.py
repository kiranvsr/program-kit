import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import briefs
import pk
import register
from common import KitError, sha256_bytes, write_yaml
from test_register import reg, task


def two_phase():
    """Two tasks in one step, plus a second phase with one task; exercises every list and null case."""
    r = reg(task("P1-M01-S01-T01", text="build it\nsecond line", tests_unit="unit: a | b", environments=["dev", "staging"],
                 closes=["REQ-001", "J1"], gate="G-ALPHA", tests_integration="integration: x", done="shipped"),
            task("P1-M01-S01-T02", depends_on=["P1-M01-S01-T01"]))
    r["phases"].append({"id": "P2", "title": "second", "gate": None, "milestones": [
        {"id": "P2-M01", "title": "m2", "gate": None, "steps": [
            {"id": "P2-M01-S01", "title": "s2", "tasks": [task("P2-M01-S01-T01")]}]}]})
    return r


class BriefsBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "program").mkdir()
        self.path = self.root / "program" / "register.yaml"
        write_yaml(self.path, two_phase())
        self.out = self.root / "program" / "briefs"

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.out.rglob("*")) if p.is_file()}

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("briefs.find_root", return_value=self.root), \
                mock.patch("register.find_root", return_value=self.root):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class BriefsTests(BriefsBase):
    def test_generate_is_deterministic(self):
        first_paths = briefs.generate(self.root)
        first = self.snapshot()
        shutil.rmtree(self.out)  # a fresh run must rewrite every byte identically, not skip matching files
        second_paths = briefs.generate(self.root)
        self.assertEqual(first_paths, second_paths)
        self.assertEqual(first_paths, sorted(first_paths))
        self.assertEqual(first, self.snapshot())
        rels = sorted(first)
        self.assertEqual(rels, [
            "program/briefs/CATALOG.md", "program/briefs/MANIFEST.json",
            "program/briefs/milestones/P1-M01.md", "program/briefs/milestones/P2-M01.md",
            "program/briefs/phases/P1.md", "program/briefs/phases/P2.md",
            "program/briefs/steps/P1-M01-S01.md", "program/briefs/steps/P2-M01-S01.md",
            "program/briefs/tasks/P1-M01-S01-T01.md", "program/briefs/tasks/P1-M01-S01-T02.md",
            "program/briefs/tasks/P2-M01-S01-T01.md"])
        manifest = json.loads(first["program/briefs/MANIFEST.json"])
        self.assertEqual(list(manifest), sorted(manifest))
        self.assertEqual(manifest, {rel: sha256_bytes(data) for rel, data in first.items()
                                    if not rel.endswith("MANIFEST.json")})
        catalog = first["program/briefs/CATALOG.md"].decode().splitlines()
        rows = [line for line in catalog if line.startswith("| P")]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], "| P1-M01-S01-T01 | t P1-M01-S01-T01 | backend | G-ALPHA | - |")
        self.assertEqual(rows[1], "| P1-M01-S01-T02 | t P1-M01-S01-T02 | backend | - | P1-M01-S01-T01 |")
        for data in first.values():  # no clock anywhere
            self.assertNotRegex(data.decode(), r"\d{4}-\d{2}-\d{2}")

    def test_task_brief_has_required_rows(self):
        briefs.generate(self.root)
        text = (self.out / "tasks" / "P1-M01-S01-T01.md").read_text()
        labels = ["Phase", "Milestone", "Step", "Agent role", "Size", "Environments", "Depends on",
                  "Target gate", "Closes"]
        rows = [line for line in text.splitlines() if line.startswith("| ") and not line.startswith("| Field")]
        self.assertEqual([row.split("|")[1].strip() for row in rows], labels)
        values = {row.split("|")[1].strip(): row.split("|")[2].strip() for row in rows}
        self.assertEqual(values, {"Phase": "P1", "Milestone": "P1-M01", "Step": "P1-M01-S01",
                                  "Agent role": "backend", "Size": "S", "Environments": "dev, staging",
                                  "Depends on": "-", "Target gate": "G-ALPHA", "Closes": "REQ-001, J1"})
        positions = [text.index(f"**{name}**") for name in
                     ("Task", "Unit tests required", "Integration tests required", "Definition of done")]
        self.assertEqual(positions, sorted(positions))
        self.assertGreater(positions[0], text.index("| Closes |"))
        self.assertIn("build it\nsecond line", text)
        self.assertIn("integration: x", text)
        self.assertIn("shipped", text)
        bare = (self.out / "tasks" / "P1-M01-S01-T02.md").read_text()
        self.assertIn("| Target gate | - |", bare)
        self.assertIn("| Depends on | P1-M01-S01-T01 |", bare)
        section = bare.split("**Unit tests required**")[1].split("**Integration")[0]
        self.assertEqual(section.strip(), "-")
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))

    def test_parent_briefs_list_their_children(self):
        briefs.generate(self.root)
        phase = (self.out / "phases" / "P1.md").read_text()
        self.assertIn("- P1-M01: m\n", phase)
        step = (self.out / "steps" / "P1-M01-S01.md").read_text()
        self.assertIn("- P1-M01-S01-T01: t P1-M01-S01-T01\n- P1-M01-S01-T02: t P1-M01-S01-T02\n", step)

    def test_check_flags_drift_after_edit(self):
        briefs.generate(self.root)
        self.assertEqual(briefs.check(self.root), [])
        victim = self.out / "tasks" / "P1-M01-S01-T01.md"
        victim.write_text(victim.read_text() + "edited\n")
        (self.out / "phases" / "P2.md").unlink()
        (self.out / "tasks" / "ORPHAN.md").write_text("x\n")
        problems = briefs.check(self.root)
        self.assertEqual(len(problems), 3, problems)
        joined = "\n".join(problems)
        for rel in ("tasks/P1-M01-S01-T01.md", "phases/P2.md", "tasks/ORPHAN.md"):
            self.assertIn(f"program/briefs/{rel}", joined)
        # a register edit shows up as drift too
        briefs.generate(self.root)
        data = two_phase()
        data["phases"][0]["milestones"][0]["steps"][0]["tasks"][1]["title"] = "renamed"
        write_yaml(self.path, data)
        self.assertTrue(any("T02.md" in p for p in briefs.check(self.root)))

    def test_check_requires_change_log_after_freeze(self):
        briefs.generate(self.root)
        approvals = self.root / "program" / "approvals"
        approvals.mkdir()
        approval = approvals / "stage-6.json"
        approval.write_text("{}\n")
        self.assertEqual(briefs.check(self.root), [])  # frozen but unchanged: nothing to refuse
        data = two_phase()
        data["phases"][0]["milestones"][0]["steps"][0]["tasks"][0]["text"] = "changed"
        write_yaml(self.path, data)
        problems = briefs.check(self.root)
        self.assertIn("briefs are frozen by program/approvals/stage-6.json; record a register change first", problems)
        self.assertTrue(any("T01.md" in p for p in problems))
        # a change log entry newer than the approval lifts the freeze message (drift is still drift)
        log = self.root / "program" / "changes" / "register-log.md"
        log.parent.mkdir()
        log.write_text("# Register change log\n")
        old = os.stat(approval).st_mtime_ns - 5_000_000_000
        os.utime(log, ns=(old, old))  # older than the approval: still frozen
        self.assertIn("frozen", "\n".join(briefs.check(self.root)))
        new = os.stat(approval).st_mtime_ns + 5_000_000_000
        os.utime(log, ns=(new, new))
        problems = briefs.check(self.root)
        self.assertFalse(any("frozen" in p for p in problems), problems)
        self.assertTrue(problems)

    def test_generate_removes_stale_briefs(self):
        briefs.generate(self.root)
        data = two_phase()
        del data["phases"][1]
        write_yaml(self.path, data)
        (self.out / "tasks" / "junk.txt").write_text("x")
        briefs.generate(self.root)
        self.assertFalse((self.out / "phases" / "P2.md").exists())
        self.assertFalse((self.out / "tasks" / "P2-M01-S01-T01.md").exists())
        self.assertFalse((self.out / "tasks" / "junk.txt").exists())
        self.assertEqual(briefs.check(self.root), [])

    def test_a_symlinked_brief_directory_is_one_problem_line_and_is_never_followed(self):
        briefs.generate(self.root)
        elsewhere = self.root / "elsewhere"
        (self.out / "tasks").rename(elsewhere)
        (elsewhere / "junk.md").write_text("x\n")
        (self.out / "tasks").symlink_to(elsewhere, target_is_directory=True)
        problems = briefs.check(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("program/briefs/tasks: ") and "symbolic link" in problems[0], problems)
        with self.assertRaises(KitError) as caught:
            briefs.generate(self.root)
        self.assertIn("program/briefs/tasks", str(caught.exception))
        self.assertTrue((elsewhere / "junk.md").exists())  # nothing under the link was removed or written
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()),
                         ["P1-M01-S01-T01.md", "P1-M01-S01-T02.md", "P2-M01-S01-T01.md", "junk.md"])
        code, out, err = self.run_cli("briefs", "check")
        self.assertEqual((code, out, len(err.splitlines())), (1, "", 1))
        approvals = self.root / "program" / "approvals"  # under an approved register it is still one line:
        approvals.mkdir(parents=True, exist_ok=True)     # a link is not drift, so no freeze hint follows it
        (approvals / "stage-6.json").write_text("{}\n")
        self.assertEqual(briefs.check(self.root), problems)

    def test_a_symlinked_brief_root_or_nested_directory_is_one_problem_line(self):
        briefs.generate(self.root)
        elsewhere = self.root / "elsewhere"
        (elsewhere / "inner").mkdir(parents=True)
        (self.out / "steps" / "nested").symlink_to(elsewhere, target_is_directory=True)
        problems = briefs.check(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("program/briefs/steps/nested"), problems)
        (self.out / "steps" / "nested").unlink()
        self.assertEqual(briefs.check(self.root), [])
        moved = self.root / "moved"
        self.out.rename(moved)
        self.out.symlink_to(moved, target_is_directory=True)
        problems = briefs.check(self.root)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("program/briefs: "), problems)

    def test_unknown_placeholder_is_a_kit_error_naming_the_template(self):
        bad = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(bad, ignore_errors=True))
        for name in ("phase_brief.md", "milestone_brief.md", "step_brief.md", "task_brief.md"):
            (bad / name).write_text((briefs.TEMPLATES / name).read_text())
        (bad / "task_brief.md").write_text("# $id $nonsense\n")
        with mock.patch("briefs.TEMPLATES", bad):
            with self.assertRaises(KitError) as caught:
                briefs.generate(self.root)
        self.assertIn("task_brief.md", str(caught.exception))
        self.assertIn("nonsense", str(caught.exception))
        self.assertFalse(self.out.exists(), "nothing is written when a template fails")

    def test_a_template_with_a_byte_order_mark_does_not_put_one_in_the_briefs(self):
        marked = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(marked, ignore_errors=True))
        for name in briefs.TEMPLATE_NAMES.values():
            (marked / name).write_bytes(b"\xef\xbb\xbf" + (briefs.TEMPLATES / name).read_bytes())
        plain = briefs.render(self.root)
        with mock.patch("briefs.TEMPLATES", marked):
            self.assertEqual(briefs.render(self.root), plain)
        (marked / "task_brief.md").write_bytes(b"\xff\xfe")
        with mock.patch("briefs.TEMPLATES", marked), self.assertRaises(KitError) as caught:
            briefs.render(self.root)
        self.assertTrue(str(caught.exception).startswith("cannot read "), str(caught.exception))

    def test_bad_register_writes_nothing(self):
        data = two_phase()
        data["phases"][0]["milestones"][0]["steps"][0]["tasks"][0]["id"] = "../../evil"
        write_yaml(self.path, data)
        with self.assertRaises(KitError):
            briefs.generate(self.root)
        self.assertFalse(self.out.exists())
        data = two_phase()
        data["phases"][1]["id"] = "P1"
        write_yaml(self.path, data)
        with self.assertRaises(KitError):
            briefs.generate(self.root)

    def test_cli_generate_and_check(self):
        code, out, err = self.run_cli("briefs", "generate")
        self.assertEqual((code, out, err), (0, "generated 9 briefs\n", ""))
        self.assertEqual(self.run_cli("briefs", "check"), (0, "briefs up to date\n", ""))
        (self.out / "phases" / "P1.md").write_text("x\n")
        code, out, err = self.run_cli("briefs", "check")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines(), ["error: program/briefs/phases/P1.md: differs from the register"])

    def test_cli_needs_an_action(self):
        code, out, err = self.run_cli("briefs")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)


class ChangeTests(BriefsBase):
    log = property(lambda self: self.root / "program" / "changes" / "register-log.md")

    def test_change_appends_and_regenerates(self):
        with mock.patch("common.utc_now", return_value="2026-10-06T12:00:00Z"), \
                mock.patch("register.utc_now", return_value="2026-10-06T12:00:00Z"):
            register.change(self.root, "  split T02  ")
            first = self.log.read_bytes()
            self.assertEqual(first, b"# Register change log\n\n- 2026-10-06: split T02\n")
            data = two_phase()
            data["phases"][0]["milestones"][0]["steps"][0]["tasks"][1]["title"] = "renamed"
            write_yaml(self.path, data)
            register.change(self.root, "rename T02")
        second = self.log.read_bytes()
        self.assertTrue(second.startswith(first))
        self.assertEqual(second[len(first):], b"- 2026-10-06: rename T02\n")
        self.assertIn("renamed", (self.out / "tasks" / "P1-M01-S01-T02.md").read_text())
        self.assertEqual(briefs.check(self.root), [])

    def test_change_returns_the_generated_paths_and_generates_once(self):
        with mock.patch("briefs.generate", wraps=briefs.generate) as generate:
            paths = register.change(self.root, "first")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(paths, briefs.generate(self.root))
        self.assertEqual(briefs.brief_count(paths), 9)

    def test_cli_change_generates_the_briefs_once(self):
        with mock.patch("briefs.generate", wraps=briefs.generate) as generate:
            code, out, err = self.run_cli("register", "change", "--note", "once")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(generate.call_count, 1)
        self.assertIn("generated 9 briefs", out)

    def test_change_refuses_bad_notes_and_writes_nothing(self):
        register.change(self.root, "first")
        before = self.log.read_bytes()
        snap = self.snapshot()
        for note in ("", "   \n ", "two\nlines", "a\rb", "a b"):
            with self.assertRaises(KitError, msg=repr(note)):
                register.change(self.root, note)
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(self.snapshot(), snap)

    def test_change_with_unrenderable_register_logs_nothing(self):
        write_yaml(self.path, {"phases": [{"title": "no id"}]})
        with self.assertRaises(KitError):
            register.change(self.root, "x")
        self.assertFalse(self.log.exists())

    def failing_second_write(self):
        """A write_text that lets the first file through and then fails, as a full disk would."""
        real, calls = briefs.write_text, []

        def write(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise KitError(f"cannot write {path}: No space left on device")
            real(path, text)

        return mock.patch("briefs.write_text", side_effect=write)

    def test_a_failed_change_leaves_the_log_and_the_briefs_as_they_were(self):
        with self.failing_second_write(), self.assertRaises(KitError):
            register.change(self.root, "first")
        self.assertFalse(self.log.exists())  # no log line, no partial brief tree
        self.assertFalse(self.out.exists())
        register.change(self.root, "baseline")
        log, snap = self.log.read_bytes(), self.snapshot()
        data = two_phase()
        data["phases"][0]["milestones"][0]["steps"][0]["tasks"][1]["title"] = "renamed"
        data["phases"][1]["id"] = "P3"
        data["phases"][1]["milestones"][0]["id"] = "P3-M01"
        data["phases"][1]["milestones"][0]["steps"][0]["id"] = "P3-M01-S01"
        data["phases"][1]["milestones"][0]["steps"][0]["tasks"][0]["id"] = "P3-M01-S01-T01"
        write_yaml(self.path, data)
        with self.failing_second_write(), self.assertRaises(KitError):
            register.change(self.root, "second")
        self.assertEqual(self.log.read_bytes(), log)
        self.assertEqual(self.snapshot(), snap)  # changed, added and removed briefs are all back
        with self.failing_second_write():
            code, out, err = self.run_cli("register", "change", "--note", "third")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        self.assertEqual(self.log.read_bytes(), log)
        self.assertEqual(self.snapshot(), snap)

    def test_cli_change(self):
        code, out, err = self.run_cli("register", "change", "--note", "add T03")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("generated 9 briefs", out)
        self.assertIn("add T03", self.log.read_text())
        code, out, err = self.run_cli("register", "change", "--note", "a\nb")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)
        code, out, err = self.run_cli("register", "change")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
