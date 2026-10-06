import _paths  # noqa: F401  (must come first: puts kit/scripts/ on sys.path)
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import change
import pk
import register as register_module
import trace as trace_module
from common import KitError, write_json, write_yaml
from test_register import reg, task
from test_trace import GATES, T1, T2, T3, full_matrix

KIT = Path(__file__).resolve().parent.parent
HEADINGS = ("Change", "Why", "Asked by", "Impact", "Approval", "Applied")


def put_cr(root, number, title="a change", why="because", asked_by="Ada", impact="", approval="", applied=""):
    """Write program/changes/CR-<number>.md in the layout `change.create` produces."""
    folder = Path(root) / "program" / "changes"
    folder.mkdir(parents=True, exist_ok=True)
    cr_id = f"CR-{number:03d}"
    text = f"# {cr_id}\n\n"
    for name, body in zip(HEADINGS, (title, why, asked_by, impact, approval, applied)):
        text += f"## {name}\n" + (f"\n{body}\n" if body else "") + ("\n" if name != "Applied" else "")
    (folder / f"{cr_id}.md").write_text(text)
    return cr_id


class ChangeBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.program = self.root / "program"
        self.program.mkdir()
        tasks = {T1: {"closes": ["REQ-001"]}, T2: {"closes": ["J1"]}, T3: {"closes": ["NFR-001"]}}
        write_yaml(self.program / "traceability.yaml", full_matrix(tasks=tasks))
        write_yaml(self.program / "gates.yaml", GATES)
        write_yaml(self.program / "register.yaml",
                   reg(task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]), task(T3, closes=["NFR-001"])))
        self.evidence = self.program / "evidence" / T1
        self.evidence.mkdir(parents=True)
        (self.evidence / "results.json").write_text('{"x": 1}\n')
        (self.evidence / "acceptance.json").write_text('{"y": 2}\n')
        self.before = self.evidence_digest()

    def evidence_digest(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((self.program / "evidence").rglob("*")) if p.is_file()}

    def put_status(self, task_id, state):
        record = {"task_id": task_id, "status": state, "role": "", "updated_at": "", "note": "",
                  "evidence": [], "blocked_by": [], "history": []}
        write_json(self.program / "status" / f"{task_id}.json", record)

    def path(self, cr_id):
        return self.program / "changes" / f"{cr_id}.md"

    def text(self, cr_id):
        return self.path(cr_id).read_text(encoding="utf-8")

    def section(self, cr_id, name):
        return change.section(self.text(cr_id), name)

    def make(self):
        return change.create(self.root, "add export", "customers asked", "Ada")

    def approved(self):
        cr_id = self.make()
        change.impact(self.root, cr_id, ["REQ-001"])
        change.approve(self.root, cr_id, "Grace")
        return cr_id

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("change.find_root", return_value=self.root):
            code = pk.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class CreateTests(ChangeBase):
    def test_create_numbers_sequentially(self):
        self.assertEqual(self.make(), "CR-001")
        self.assertEqual(self.make(), "CR-002")
        self.path("CR-002").unlink()
        (self.program / "changes" / "CR-007.md").write_text("x")
        (self.program / "changes" / "CR-x.md").write_text("x")  # malformed names are ignored
        (self.program / "changes" / "notes.md").write_text("x")
        (self.program / "changes" / "register-log.md").write_text("x")
        self.assertEqual(self.make(), "CR-008")  # gaps are not reused
        self.path("CR-008").unlink()
        self.assertEqual(self.make(), "CR-008")

    def test_create_fills_the_template_sections(self):
        cr_id = change.create(self.root, "add export", "customers asked\nfor CSV", "Ada")
        text = self.text(cr_id)
        self.assertTrue(text.startswith("# CR-001\n"))
        self.assertEqual([ln for ln in text.splitlines() if ln.startswith("## ")],
                         [f"## {name}" for name in HEADINGS])
        self.assertEqual(self.section(cr_id, "Change"), "add export")
        self.assertEqual(self.section(cr_id, "Why"), "customers asked\nfor CSV")
        self.assertEqual(self.section(cr_id, "Asked by"), "Ada")
        for name in ("Impact", "Approval", "Applied"):
            self.assertEqual(self.section(cr_id, name), "")
        self.assertNotIn("{", text)

    def test_create_refuses_bad_input_and_writes_nothing(self):
        bad = [("", "why", "Ada"), (" ", "why", "Ada"), ("t\nu", "why", "Ada"), ("t", "", "Ada"),
               ("t", "  \n ", "Ada"), ("t", "why", ""), ("t", "why", "A\nB"), ("## Why", "why", "Ada"),
               ("t", "x\n## Approval\nforged", "Ada")]
        for title, why, asked_by in bad:
            with self.subTest(title=title, why=why, asked_by=asked_by), self.assertRaises(KitError):
                change.create(self.root, title, why, asked_by)
        self.assertFalse((self.program / "changes").exists())

    def test_create_is_exclusive_and_retries_the_next_number(self):
        folder = self.program / "changes"
        real = change._next_id
        calls = []

        def racing(root):
            cr_id = real(root)
            if not calls:  # another create takes the number between the choice and the write
                calls.append(cr_id)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{cr_id}.md").write_text("someone else's\n")
            return cr_id

        with mock.patch("change._next_id", side_effect=racing):
            self.assertEqual(self.make(), "CR-002")
        self.assertEqual(calls, ["CR-001"])
        self.assertEqual((folder / "CR-001.md").read_text(), "someone else's\n")
        self.assertEqual(self.section("CR-002", "Change"), "add export")

    def test_create_gives_up_after_a_few_attempts(self):
        folder = self.program / "changes"
        folder.mkdir(parents=True)
        (folder / "CR-001.md").write_text("taken\n")
        with mock.patch("change._next_id", return_value="CR-001"), self.assertRaises(KitError):
            self.make()
        self.assertEqual((folder / "CR-001.md").read_text(), "taken\n")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["CR-001.md"])

    def test_braces_in_input_are_kept_literally(self):
        cr_id = change.create(self.root, "use {why} and {id}", "{asked_by}", "{change}")
        self.assertEqual(self.section(cr_id, "Change"), "use {why} and {id}")
        self.assertEqual(self.section(cr_id, "Why"), "{asked_by}")
        self.assertEqual(self.section(cr_id, "Asked by"), "{change}")


class ByteOrderMarkTests(ChangeBase):
    BOM = b"\xef\xbb\xbf"

    def test_a_change_request_saved_with_a_byte_order_mark_reads_like_one_without(self):
        cr_id = put_cr(self.root, 1)
        path = self.path(cr_id)
        text = path.read_bytes().replace(b"# CR-001\n\n", b"")  # starts on a heading, which a mark would hide
        path.write_bytes(self.BOM + text)
        self.assertEqual(change.section(change._read(self.root, cr_id)[1], "Change"), "a change")
        change.impact(self.root, cr_id, ["REQ-001"])
        change.approve(self.root, cr_id, "Grace")
        change.apply(self.root, cr_id, "amended")
        self.assertIn("amended", change.section(change._read(self.root, cr_id)[1], "Applied"))
        self.assertEqual([r["id"] for r in change.open_requests(self.root)], [])

    def test_a_template_with_a_byte_order_mark_does_not_put_one_in_the_request(self):
        marked = self.root / "change_request.md"
        marked.write_bytes(self.BOM + change.TEMPLATE.read_bytes())
        with mock.patch("change.TEMPLATE", marked):
            cr_id = self.make()
        self.assertFalse(self.path(cr_id).read_bytes().startswith(self.BOM))

    def test_an_unreadable_change_request_is_one_error_line(self):
        cr_id = put_cr(self.root, 1)
        self.path(cr_id).write_bytes(b"\xff\xfe")
        with self.assertRaises(KitError) as caught:
            change.approve(self.root, cr_id, "Grace")
        self.assertTrue(str(caught.exception).startswith("cannot read "), str(caught.exception))
        self.assertEqual(len(str(caught.exception).splitlines()), 1)


class ImpactTests(ChangeBase):
    def test_impact_written_from_trace(self):
        self.put_status(T1, "done")
        self.put_status(T2, "in_progress")
        cr_id = self.make()
        result = change.impact(self.root, cr_id, ["J1"])
        self.assertEqual(result, trace_module.impact(self.root, ["J1"]))
        body = self.section(cr_id, "Impact")
        self.assertEqual(body.splitlines()[0], "- Changed: J1")
        self.assertIn("- Requirements: REQ-001", body)
        self.assertIn("- Components: C1", body)
        self.assertIn("- Gates: G-P0", body)
        self.assertIn(f"- Done tasks: {T1}", body)
        self.assertIn(f"- In-flight tasks: {T2}", body)
        self.assertIn("- Not-started tasks: none", body)
        # the other sections and the heading order survive
        self.assertEqual(self.section(cr_id, "Why"), "customers asked")
        self.assertEqual([ln for ln in self.text(cr_id).splitlines() if ln.startswith("## ")],
                         [f"## {name}" for name in HEADINGS])
        # a second run replaces the first
        change.impact(self.root, cr_id, ["C2"])
        body = self.section(cr_id, "Impact")
        self.assertIn("- Changed: C2", body)
        self.assertNotIn("J1", body)
        self.assertEqual(self.text(cr_id).count("## Impact"), 1)

    def test_impact_refuses_unknown_ids_and_leaves_the_file(self):
        cr_id = self.make()
        before = self.text(cr_id)
        for ids in (["REQ-404"], []):
            with self.subTest(ids=ids), self.assertRaises(KitError):
                change.impact(self.root, cr_id, ids)
        self.assertEqual(self.text(cr_id), before)

    def test_impact_refused_after_approval(self):
        cr_id = self.approved()
        before = self.text(cr_id)
        with self.assertRaises(KitError) as caught:
            change.impact(self.root, cr_id, ["C2"])
        self.assertIn("approved", str(caught.exception))
        self.assertEqual(self.text(cr_id), before)

    def test_unknown_or_malformed_change_request(self):
        for cr_id in ("CR-404", "banana", "../CR-001", "CR-1"):
            with self.subTest(cr_id=cr_id), self.assertRaises(KitError):
                change.impact(self.root, cr_id, ["J1"])

    def test_missing_heading_is_an_error(self):
        cr_id = self.make()
        self.path(cr_id).write_text(self.text(cr_id).replace("## Impact\n", "## Effects\n"))
        with self.assertRaises(KitError) as caught:
            change.impact(self.root, cr_id, ["J1"])
        self.assertIn("Impact", str(caught.exception))
        self.path(cr_id).write_text(self.text(cr_id) + "\n## Why\nagain\n")
        with self.assertRaises(KitError):
            change.approve(self.root, cr_id, "Grace")


class ApproveTests(ChangeBase):
    def test_approve_fills_approval_with_date_and_name(self):
        cr_id = self.make()
        with self.assertRaises(KitError) as caught:
            change.approve(self.root, cr_id, "Grace")  # no impact yet
        self.assertIn("impact", str(caught.exception).lower())
        self.assertEqual(self.section(cr_id, "Approval"), "")
        change.impact(self.root, cr_id, ["REQ-001"])
        change.approve(self.root, cr_id, "Grace")
        self.assertRegex(self.section(cr_id, "Approval"), r"\A\d{4}-\d{2}-\d{2}: Grace\Z")
        with self.assertRaises(KitError):
            change.approve(self.root, cr_id, "Someone else")
        self.assertIn("Grace", self.section(cr_id, "Approval"))

    def test_approve_refuses_a_bad_name(self):
        cr_id = self.make()
        change.impact(self.root, cr_id, ["REQ-001"])
        for by in ("", "  ", "A\nB", "## Applied"):
            with self.subTest(by=by), self.assertRaises(KitError):
                change.approve(self.root, cr_id, by)
        self.assertEqual(self.section(cr_id, "Approval"), "")


class ApplyTests(ChangeBase):
    def test_apply_refuses_without_approval(self):
        cr_id = self.make()
        change.impact(self.root, cr_id, ["REQ-001"])
        with self.assertRaises(KitError) as caught:
            change.apply(self.root, cr_id, "amended SPEC")
        self.assertIn("approv", str(caught.exception))
        self.assertEqual(self.section(cr_id, "Applied"), "")
        self.assertFalse((self.program / "changes" / "register-log.md").exists())

    def test_apply_records_the_register_change_and_writes_applied(self):
        cr_id = self.approved()
        change.apply(self.root, cr_id, "amended SPEC section 3")
        self.assertRegex(self.section(cr_id, "Applied"), r"\A\d{4}-\d{2}-\d{2}: amended SPEC section 3\Z")
        log = (self.program / "changes" / "register-log.md").read_text()
        self.assertIn(f"{cr_id}: amended SPEC section 3", log)
        self.assertTrue((self.program / "briefs").is_dir())  # register.change regenerated the briefs
        with self.assertRaises(KitError) as caught:
            change.apply(self.root, cr_id, "again")
        self.assertIn("already", str(caught.exception))
        self.assertEqual(log, (self.program / "changes" / "register-log.md").read_text())

    def test_apply_with_trace_problems_writes_nothing(self):
        cr_id = self.start_from_a_logged_register()
        before, cr_text = self.snapshot(), self.text(cr_id)
        write_yaml(self.program / "register.yaml", reg(task(T1, closes=["REQ-001"]), task(T2, closes=["J1"])))
        trace_module.sync(self.root)  # NFR-001 is no longer closed by any task
        with self.assertRaises(KitError) as caught:
            change.apply(self.root, cr_id, "amended SPEC")
        self.assertIn("NFR-001", str(caught.exception))
        self.assertTrue(str(caught.exception).startswith(f"{cr_id} not applied: "))
        self.assertEqual(self.snapshot(), before)  # no log line, no regenerated brief
        self.assertEqual(self.text(cr_id), cr_text)
        write_yaml(self.program / "register.yaml",
                   reg(task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]), task(T3, closes=["NFR-001"])))
        trace_module.sync(self.root)
        change.apply(self.root, cr_id, "amended SPEC, fixed")  # can be applied once the matrix is valid
        self.assertIn("fixed", self.section(cr_id, "Applied"))

    def test_apply_with_a_register_the_briefs_cannot_render_writes_nothing(self):
        cr_id = self.start_from_a_logged_register()
        before, cr_text = self.snapshot(), self.text(cr_id)
        write_yaml(self.program / "register.yaml", reg(
            task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]), task(T3, closes=["NFR-001"], text=5)))
        with self.assertRaises(KitError):
            change.apply(self.root, cr_id, "amended")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.text(cr_id), cr_text)

    def test_apply_rolls_back_when_the_change_request_cannot_be_written(self):
        cr_id = self.start_from_a_logged_register()
        before, cr_text = self.snapshot(), self.text(cr_id)
        write_yaml(self.program / "register.yaml", reg(
            task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]), task(T3, closes=["NFR-001"], text="new text")))
        with mock.patch("change.write_text", side_effect=KitError("cannot write: disk full")), \
                self.assertRaises(KitError):
            change.apply(self.root, cr_id, "amended")
        self.assertEqual(self.snapshot(), before)  # the log line and the regenerated brief are taken back
        self.assertEqual(self.text(cr_id), cr_text)
        change.apply(self.root, cr_id, "amended")
        self.assertIn("new text", self.snapshot()["program/briefs/tasks/" + T3 + ".md"].decode())

    def snapshot(self):
        """Every file under program/changes (the log) and program/briefs, as {path: bytes}."""
        return {str(p.relative_to(self.root)): p.read_bytes() for folder in ("changes", "briefs")
                for p in sorted((self.program / folder).rglob("*")) if p.is_file()}

    def start_from_a_logged_register(self):
        """An approved change request on a register whose briefs and log exist; returns its id."""
        cr_id = self.approved()
        register_module.change(self.root, "baseline")
        return cr_id

    def test_apply_refuses_a_register_change_that_was_not_synced_and_writes_nothing(self):
        cr_id = self.start_from_a_logged_register()
        before, cr_text = self.snapshot(), self.text(cr_id)
        write_yaml(self.program / "register.yaml", reg(
            task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]), task(T3, closes=["NFR-001"]),
            task("P1-M01-S01-T04", closes=["REQ-001"])))  # the follow-up task, never synced
        with self.assertRaises(KitError) as caught:
            change.apply(self.root, cr_id, "added the follow-up")
        self.assertEqual(str(caught.exception),
                         f"{cr_id} not applied: program/traceability.yaml: tasks section differs from the "
                         "register; run pk trace sync")
        self.assertEqual(self.snapshot(), before)  # no log line, no regenerated brief
        self.assertEqual(self.text(cr_id), cr_text)
        trace_module.sync(self.root)
        change.apply(self.root, cr_id, "added the follow-up")
        self.assertIn("added the follow-up", self.section(cr_id, "Applied"))
        self.assertIn("P1-M01-S01-T04", self.snapshot()["program/briefs/CATALOG.md"].decode())

    def test_apply_refuses_an_invalid_register_and_writes_nothing(self):
        cr_id = self.start_from_a_logged_register()
        before = self.snapshot()
        write_yaml(self.program / "register.yaml", reg(
            task(T1, closes=["REQ-001"]), task(T2, closes=["J1"]), task(T3, closes=["NFR-001"], size="L")))
        with self.assertRaises(KitError) as caught:
            change.apply(self.root, cr_id, "amended")
        self.assertIn("program/register.yaml has 1 problem(s)", str(caught.exception))
        self.assertIn("pk register validate", str(caught.exception))
        self.assertEqual(len(str(caught.exception).splitlines()), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.section(cr_id, "Applied"), "")

    def test_removing_the_only_task_closing_a_requirement_is_refused_before_and_after_the_sync(self):
        cr_id = self.start_from_a_logged_register()
        before = self.snapshot()
        write_yaml(self.program / "register.yaml", reg(task(T1, closes=["REQ-001"]), task(T2, closes=["J1"])))
        with self.assertRaises(KitError) as caught:
            change.apply(self.root, cr_id, "dropped the task")
        self.assertIn("run pk trace sync", str(caught.exception))  # the sync guard comes first
        self.assertEqual(self.snapshot(), before)
        trace_module.sync(self.root)
        with self.assertRaises(KitError) as caught:
            change.apply(self.root, cr_id, "dropped the task")
        self.assertIn("NFR-001: first-release requirement is not closed by any task", str(caught.exception))
        self.assertEqual(self.snapshot(), before)  # still nothing written
        self.assertEqual(self.section(cr_id, "Applied"), "")
        code, out, err = self.run_cli("change", "apply", cr_id, "--note", "dropped the task")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith(f"error: {cr_id} not applied: ") and err.count("\n") == 1, err)
        self.assertEqual(self.snapshot(), before)

    def test_apply_refuses_a_bad_note(self):
        cr_id = self.approved()
        for note in ("", " ", "a\nb", "## Why"):
            with self.subTest(note=note), self.assertRaises(KitError):
                change.apply(self.root, cr_id, note)
        self.assertEqual(self.section(cr_id, "Applied"), "")
        self.assertFalse((self.program / "changes" / "register-log.md").exists())

    def test_a_change_request_never_touches_evidence(self):
        self.put_status(T1, "done")
        cr_id = self.approved()
        change.apply(self.root, cr_id, "amended SPEC")
        self.assertEqual(self.evidence_digest(), self.before)
        self.assertNotIn("program/evidence", Path(change.__file__).read_text())


class DocsTests(unittest.TestCase):
    def test_the_playbook_and_the_design_say_that_apply_checks_the_sync_before_writing(self):
        playbook = " ".join((KIT / "playbooks" / "10_change.md").read_text(encoding="utf-8").split())
        design = " ".join((KIT.parent / "docs" / "DESIGN.md").read_text(encoding="utf-8").split())
        self.assertIn("before it writes anything, when the register is invalid or the matrix's `tasks` section "
                      "differs from what `pk trace sync` would write", playbook)
        self.assertIn("Before it writes anything `pk change apply` checks that the register is valid, that the "
                      "matrix's `tasks` section is what `pk trace sync` would write", design)


class CliTests(ChangeBase):
    def test_cli_round_trip(self):
        code, out, err = self.run_cli("change", "create", "--title", "add export", "--why", "asked",
                                      "--asked-by", "Ada")
        self.assertEqual((code, out, err), (0, "CR-001\n", ""))
        code, out, err = self.run_cli("change", "impact", "CR-001", "REQ-001", "C1")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), trace_module.impact(self.root, ["REQ-001", "C1"]))
        self.assertEqual(out, json.dumps(trace_module.impact(self.root, ["REQ-001", "C1"]),
                                         indent=2, sort_keys=True) + "\n")
        self.assertEqual(self.run_cli("change", "approve", "CR-001", "--by", "Grace"),
                         (0, "CR-001 approved by Grace\n", ""))
        self.assertEqual(self.run_cli("change", "apply", "CR-001", "--note", "amended SPEC"),
                         (0, "CR-001 applied\n", ""))
        self.assertIn("amended SPEC", self.section("CR-001", "Applied"))

    def test_cli_errors_are_one_line(self):
        for argv in (("change",), ("change", "create", "--title", "t"), ("change", "impact", "CR-009", "J1"),
                     ("change", "apply", "CR-009", "--note", "x")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("error: ") and err.count("\n") == 1, err)

    def test_a_file_where_the_changes_directory_belongs_is_one_error_line(self):
        (self.program / "changes").write_text("not a directory\n")
        code, out, err = self.run_cli("change", "create", "--title", "t", "--why", "w", "--asked-by", "Ada")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: cannot create ") and err.count("\n") == 1, err)
        self.assertEqual((self.program / "changes").read_text(), "not a directory\n")


if __name__ == "__main__":
    unittest.main()
